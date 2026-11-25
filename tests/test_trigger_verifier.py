"""Regression tests for Eastmoney inventory mapping, continuous roll guards,
fail-closed trigger verifications, conflict protection, TTL boundaries, and APIs."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import daily_sentinel as ds  # noqa: E402
import research_archive as ra  # noqa: E402
import research_data as rd  # noqa: E402
from backend import app as backend_app  # noqa: E402
from backend import deep_research, trigger_verifier  # noqa: E402
from backend.cache import CACHE  # noqa: E402
from tests.test_daily_sentinel import _make_healthy_snapshot  # noqa: E402


def _make_sample_inventory_df() -> pd.DataFrame:
    dates = pd.date_range("2026-11-01", periods=25, freq="D")
    return pd.DataFrame({
        "日期": dates,
        "库存": [80000 + i * 200 for i in range(25)],
    })


def _make_healthy_ni_snapshot(price: float = 116000.0) -> dict:
    snap = _make_healthy_snapshot(
        symbol="NI0",
        price=price,
        daily_zd=112000.0,
        daily_zg=115000.0,
        intra_zd=114500.0,
        intra_zg=115500.0,
        daily_bias="long",
        intraday_bias="long",
        daily_bar="2026-11-25",
        intraday_bar="2026-11-25 15:00",
        atr14_30m=400.0,
    )
    snap["asset_class"] = "cn_future"
    return snap


class TestInventoryEmMapping(unittest.TestCase):
    def test_inventory_symbol_mapping_and_fallback(self):
        seen_calls: list[str] = []

        def fake_em(symbol: str):
            seen_calls.append(symbol)
            if symbol == "自定义测试":
                raise ValueError("请输入正确的 symbol, 可选项为: {'镍': 'NI'}")
            return _make_sample_inventory_df()

        with patch.object(rd.ak, "futures_inventory_em", side_effect=fake_em):
            res_ni = rd.fetch_cn_future_inventory("沪镍", spot_code="NI")
            self.assertEqual(res_ni["em_symbol"], "镍")
            self.assertEqual(res_ni["latest_inventory"], 84800.0)

            res_cf = rd.fetch_cn_future_inventory("棉花", spot_code="CF")
            self.assertEqual(res_cf["em_symbol"], "郑棉")

            res_sa = rd.fetch_cn_future_inventory("纯碱", spot_code="SA")
            self.assertEqual(res_sa["em_symbol"], "纯碱")

            res_ec = rd.fetch_cn_future_inventory("集运欧线", spot_code="EC")
            self.assertEqual(res_ec["status"], "skipped")

            res_fb = rd.fetch_cn_future_inventory("自定义测试", spot_code="ZZ")
            self.assertEqual(res_fb["em_symbol"], "ZZ")
            self.assertIn("自定义测试", seen_calls)
            self.assertIn("ZZ", seen_calls)


class TestContinuousContractRollGuards(unittest.TestCase):
    def test_continuous_contract_emits_dedicated_roll_yellow_not_data_warning_and_blocks_green(self):
        ref = date(2026, 11, 25)
        bundle = {
            "symbol": "NI0",
            "sources_status": {
                "inventory_em": {"status": "ok"},
                "term_structure": {"status": "ok"},
            },
            "fundamentals_and_supply": {
                "inventory_em": {
                    "latest_date": "2026-11-24",
                    "latest_inventory": 85582.0,
                    "chg_5d_pct": -1.2,
                },
                "term_structure": {
                    "quote_date": "2026-11-25",
                    "structure": "Contango",
                    "front_contract": "NI2612",
                    "annualized_roll_yield_near_pct": -1.8,
                    "contracts": [
                        {"symbol": "NI2611", "open_interest": 999999, "volume": 999999},  # expired!
                        {"symbol": "CU2701", "open_interest": 888888, "volume": 888888},  # wrong root!
                        {"symbol": "NI2612", "open_interest": 34000, "volume": 40000},
                        {"symbol": "NI2701", "open_interest": 154000, "volume": 207000},
                    ],
                },
            },
        }
        pos = {
            "symbol": "NI0",
            "asset_class": "cn_future",
            "direction": "long",
            "entry_price": 115000.0,
            "tactical_stop_30m": 113500.0,
            "structural_stop_daily": 111000.0,
            "contract_symbol": "NI0",
        }
        state = {
            "status": "holding",
            "triggers": [
                {
                    "id": "trig_close",
                    "kind": "chanlun",
                    "evaluation_mode": "auto",
                    "metric": "close",
                    "operator": "<=",
                    "threshold": 111000.0,
                    "action": "exit_review",
                    "status": "armed",
                }
            ],
        }
        res = ds.evaluate_holding_health(
            "NI0",
            "long",
            position=pos,
            archive_state=state,
            snapshot=_make_healthy_ni_snapshot(116000.0),
            research_bundle=bundle,
            ref_date=ref,
        )
        # Must NOT masquerade as upstream data_source_degraded
        self.assertFalse(any("futures_roll_window" in w for w in res["data_warnings"]))
        yellow_codes = {a["code"] for a in res["yellow_alerts"]}
        self.assertNotIn("data_source_degraded", yellow_codes)
        # Must still emit dedicated roll-window yellow alert so continuous contract never gets a false GREEN
        self.assertIn("continuous_contract_unmapped", yellow_codes)
        self.assertNotIn("roll_window_imminent", yellow_codes)
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        roll_alert = [a for a in res["yellow_alerts"] if a["code"] == "continuous_contract_unmapped"][0]
        self.assertIn("NI2701", roll_alert["detail"])
        self.assertNotIn("NI2611", roll_alert["detail"])
        self.assertNotIn("CU2701", roll_alert["detail"])

    def test_zero_oi_missing_quote_date_and_garbage_quote_date_rejected(self):
        ref = date(2026, 11, 25)
        # Missing quote_date -> rejected
        b_no_qd = {
            "symbol": "NI0",
            "fundamentals_and_supply": {
                "term_structure": {
                    "front_contract": "NI2701",
                    "contracts": [{"symbol": "NI2701", "open_interest": 1000, "volume": 500}],
                }
            },
        }
        self.assertIsNone(ds._infer_dominant_contract_from_bundle("NI0", b_no_qd, ref))

        # Garbage suffix quote_date -> rejected
        b_bad_qd = {
            "symbol": "NI0",
            "fundamentals_and_supply": {
                "term_structure": {
                    "quote_date": "2026-11-25 garbage",
                    "contracts": [{"symbol": "NI2701", "open_interest": 1000, "volume": 500}],
                }
            },
        }
        self.assertIsNone(ds._infer_dominant_contract_from_bundle("NI0", b_bad_qd, ref))

        # Zero OI and zero volume (even with front_contract present) -> rejected
        b_zero = {
            "symbol": "NI0",
            "fundamentals_and_supply": {
                "term_structure": {
                    "quote_date": "2026-11-25",
                    "front_contract": "NI2701",
                    "contracts": [
                        {"symbol": "NI2701", "open_interest": 0, "volume": 0},
                        {"symbol": "NI2702", "open_interest": None, "volume": None},
                    ],
                }
            },
        }
        self.assertIsNone(ds._infer_dominant_contract_from_bundle("NI0", b_zero, ref))


class TestFailClosedTriggerVerificationAndTTL(unittest.TestCase):
    def test_fail_closed_clear_gates_and_auto_metric_protection(self):
        now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
        state = {
            "triggers": [
                # Valid fresh AI clear with numeric observed + provenance
                {
                    "id": "trig_valid_clear",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "indonesia_rkab_2027_quota_mt",
                    "operator": "<",
                    "threshold": 250,
                    "action": "exit_review",
                    "status": "armed",
                    "verification": {
                        "method": "ai_web",
                        "status": "clear",
                        "observed": 275,
                        "summary": "ESDM 官员称 2027 RKAB 初始方案维持约 2.75 亿湿吨",
                        "evidence_tier": "L2",
                        "source_title": "Reuters",
                        "source_url": "https://www.reuters.com/markets/commodities/nickel",
                        "verified_at_utc": "2026-11-24 10:00 UTC",
                        "ttl_days": 7,
                    },
                },
                # Unsubstantiated AI clear (no provenance, non-numeric observed for numeric op) -> MUST REJECT
                {
                    "id": "trig_bad_clear_no_source",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "indonesia_tax_rate_pct",
                    "operator": ">=",
                    "threshold": 15,
                    "action": "exit_review",
                    "status": "armed",
                    "verification": {
                        "method": "ai_web",
                        "status": "clear",
                        "observed": "unknown",
                        "summary": "好像没变化",
                        "verified_at_utc": "2026-11-25 10:00 UTC",
                        "ttl_days": 7,
                    },
                },
                # Auto metric with stored verification MUST NOT pass when live observation is missing
                {
                    "id": "trig_auto_close",
                    "kind": "chanlun",
                    "evaluation_mode": "auto",
                    "metric": "close",
                    "operator": "<=",
                    "threshold": 111000,
                    "action": "exit_review",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "observed": 116000,
                        "summary": "试图覆盖自动行情指标",
                        "verified_at_utc": "2026-11-25 10:00 UTC",
                        "ttl_days": 7,
                    },
                },
                # Manual numeric clear with only "无数据" and no observed/provenance -> MUST REJECT
                {
                    "id": "trig_manual_empty_numeric",
                    "kind": "policy",
                    "evaluation_mode": "manual",
                    "metric": "indonesia_ore_royalty_pct",
                    "operator": ">=",
                    "threshold": 14,
                    "action": "exit_review",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "summary": "无数据",
                        "verified_at_utc": "2026-11-25 10:00 UTC",
                        "ttl_days": 7,
                    },
                },
                # Auto metric contract_close (without explicit evaluation_mode) -> MUST NOT pass
                {
                    "id": "trig_auto_contract_close",
                    "kind": "chanlun",
                    "metric": "contract_close",
                    "operator": "<=",
                    "threshold": 111000,
                    "action": "exit_review",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "observed": 116000,
                        "summary": "试图覆盖 contract_close",
                        "verified_at_utc": "2026-11-25 10:00 UTC",
                        "ttl_days": 7,
                    },
                },
                # 4 minutes in the future -> MUST REJECT (zero future tolerance)
                {
                    "id": "trig_future_4m",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "policy_future_4m",
                    "operator": "event",
                    "threshold": True,
                    "action": "alert",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "summary": "未来4分钟测试",
                        "verified_at_utc": "2026-11-25 12:04 UTC",
                        "ttl_days": 7,
                    },
                },
                # Future timestamp + garbage suffix -> MUST REJECT
                {
                    "id": "trig_future_ts",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "policy_future",
                    "operator": "event",
                    "threshold": True,
                    "action": "alert",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "summary": "未来时间测试",
                        "verified_at_utc": "2030-01-01 00:00 UTC",
                        "ttl_days": 7,
                    },
                },
                {
                    "id": "trig_garbage_ts",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "policy_garbage",
                    "operator": "event",
                    "threshold": True,
                    "action": "alert",
                    "status": "armed",
                    "verification": {
                        "method": "manual",
                        "status": "clear",
                        "summary": "非法时间测试",
                        "verified_at_utc": "2026-11-25 nonsense",
                        "ttl_days": 7,
                    },
                },
            ]
        }
        res = {r["id"]: r for r in ra.evaluate_triggers(state, {}, now_utc=now)}
        self.assertTrue(res["trig_valid_clear"]["evaluated"])
        self.assertFalse(res["trig_valid_clear"]["fired"])
        self.assertFalse(res["trig_bad_clear_no_source"]["evaluated"])
        self.assertFalse(res["trig_auto_close"]["evaluated"])
        self.assertFalse(res["trig_manual_empty_numeric"]["evaluated"])
        self.assertFalse(res["trig_auto_contract_close"]["evaluated"])
        self.assertFalse(res["trig_future_4m"]["evaluated"])
        self.assertFalse(res["trig_future_ts"]["evaluated"])
        self.assertFalse(res["trig_garbage_ts"]["evaluated"])

    def test_exact_ttl_boundaries_and_cross_timezone_iso(self):
        base = {
            "id": "trig_ttl",
            "kind": "policy",
            "evaluation_mode": "manual",
            "metric": "custom_policy_flag",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "armed",
            "verification": {
                "method": "manual",
                "status": "clear",
                "summary": "人工确认暂未生效",
                # 2026-11-18 20:00:00+08:00 == 2026-11-18 12:00:00 UTC
                "verified_at_utc": "2026-11-18T20:00:00+08:00",
                "ttl_days": 7,
            },
        }
        # 1 second before expiry -> valid
        r_before = ra.evaluate_triggers(
            {"triggers": [base]}, {}, now_utc=datetime(2026, 11, 25, 11, 59, 59, tzinfo=timezone.utc)
        )[0]
        self.assertTrue(r_before["evaluated"])

        # Exact expiry moment -> valid
        r_exact = ra.evaluate_triggers(
            {"triggers": [base]}, {}, now_utc=datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
        )[0]
        self.assertTrue(r_exact["evaluated"])

        # 1 second after expiry -> stale
        r_after = ra.evaluate_triggers(
            {"triggers": [base]}, {}, now_utc=datetime(2026, 11, 25, 12, 0, 1, tzinfo=timezone.utc)
        )[0]
        self.assertFalse(r_after["evaluated"])
        self.assertTrue(r_after.get("verification_stale"))

    def test_find_verifiable_triggers_always_excludes_auto_metrics_even_when_explicitly_requested(self):
        state = {
            "triggers": [
                {
                    "id": "t_auto",
                    "kind": "chanlun",
                    "evaluation_mode": "auto",
                    "metric": "close",
                    "operator": "<=",
                    "threshold": 100,
                    "action": "exit_review",
                    "status": "armed",
                },
                {
                    "id": "t_policy",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "indonesia_rkab_2027_quota_mt",
                    "operator": "<",
                    "threshold": 250,
                    "action": "exit_review",
                    "status": "armed",
                },
            ]
        }
        found = trigger_verifier.find_verifiable_triggers(state, trigger_ids=["t_auto", "t_policy"])
        self.assertEqual([t["id"] for t in found], ["t_policy"])

    def test_stale_ai_result_cannot_overwrite_newer_manual_fired_or_edited_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("NI0", primary_driver="印尼政策", research_root=tmpdir)
            ra.update_state(
                "NI0",
                {
                    "status": "holding",
                    "triggers": [
                        {
                            "id": "trig_rkab",
                            "kind": "policy",
                            "evaluation_mode": "ai_web",
                            "metric": "indonesia_rkab_2027_quota_mt",
                            "operator": "<",
                            "threshold": 250,
                            "action": "exit_review",
                            "status": "armed",
                            "description": "印尼 2027 RKAB 配额低于 2.5 亿湿吨",
                        }
                    ],
                },
                research_root=tmpdir,
            )

            # Initialize with an existing minute-precision verification, then overwrite within the SAME minute!
            ra.record_trigger_verifications(
                "NI0",
                {
                    "trig_rkab": {
                        "method": "ai_web",
                        "status": "uncertain",
                        "summary": "初次待确认",
                        "verified_at_utc": "2026-11-25 07:00 UTC",
                        "ttl_days": 7,
                    }
                },
                research_root=tmpdir,
            )

            def fake_cli_during_which_human_marks_fired(**kwargs):
                del kwargs
                # Simulate operator manually marking fired within the EXACT SAME MINUTE!
                ra.record_trigger_verifications(
                    "NI0",
                    {
                        "trig_rkab": {
                            "method": "manual",
                            "status": "fired",
                            "observed": 220,
                            "summary": "人工确认印尼已官宣削减至2.2亿湿吨",
                            "verified_at_utc": "2026-11-25 07:00 UTC",
                            "ttl_days": 7,
                        }
                    },
                    research_root=tmpdir,
                )
                return (
                    '```json\n{"results": [{"id": "trig_rkab", "status": "clear", "observed": 280, '
                    '"summary": "旧消息称2.8亿湿吨", "evidence_tier": "L2", '
                    '"source_title": "Old News", "source_url": "https://example.com/old"}]}\n```',
                    {"engine": "claude"},
                )

            with (
                patch.object(deep_research, "detect_available_engines", return_value={"default_engine": "claude"}),
                patch.object(deep_research, "resolve_engine_binary", return_value="/usr/bin/claude"),
                patch.object(deep_research, "_run_agent_cli", side_effect=fake_cli_during_which_human_marks_fired),
            ):
                res = trigger_verifier.run_trigger_verification(
                    {"symbol": "NI0", "research_root": tmpdir},
                    lambda ev, data: None,
                )

            self.assertEqual(res["updated_count"], 0)
            self.assertEqual(res["conflict_count"], 1)
            loaded = ra.load_archive("NI0", research_root=tmpdir)
            rkab = loaded["state"]["triggers"][0]
            # Manual fired MUST remain intact
            self.assertEqual(rkab["verification"]["method"], "manual")
            self.assertEqual(rkab["verification"]["status"], "fired")
            self.assertEqual(rkab["verification"]["observed"], 220)

    def test_partial_batch_commit_immediately_invalidates_sentinel_cache_on_subsequent_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for sym in ("NI0", "SN0"):
                ra.init_archive(sym, primary_driver="资源政策", research_root=tmpdir)
                ra.update_state(
                    sym,
                    {
                        "status": "holding",
                        "triggers": [
                            {
                                "id": f"trig_{sym.lower()}",
                                "kind": "policy",
                                "evaluation_mode": "ai_web",
                                "metric": f"policy_{sym.lower()}",
                                "operator": "event",
                                "threshold": True,
                                "action": "exit_review",
                                "status": "armed",
                            }
                        ],
                    },
                    research_root=tmpdir,
                )

            calls = {"n": 0}

            def fake_cli(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return (
                        '```json\n{"results": ['
                        '{"id": "trig_ni0", "status": "fired", "summary": "触发", "evidence_tier": "L1", "source_title": "Gov", "source_url": "https://example.com/1"},'
                        '{"id": "trig_sn0", "status": "fired", "summary": "触发", "evidence_tier": "L1", "source_title": "Gov", "source_url": "https://example.com/2"}'
                        ']}\n```',
                        {"engine": "claude"},
                    )
                raise TimeoutError("second symbol timed out")

            before_epoch = CACHE.get_epoch("sentinel")
            with (
                patch.object(ds, "_load_all_positions", return_value=([], [], [])),
                patch.object(deep_research, "detect_available_engines", return_value={"default_engine": "claude"}),
                patch.object(deep_research, "resolve_engine_binary", return_value="/usr/bin/claude"),
                patch.object(deep_research, "_run_agent_cli", side_effect=fake_cli),
            ):
                with self.assertRaises(TimeoutError):
                    trigger_verifier.run_trigger_verification(
                        {"research_root": tmpdir},
                        lambda ev, data: None,
                    )
            after_epoch = CACHE.get_epoch("sentinel")
            self.assertGreater(after_epoch, before_epoch)

    def test_batch_discovery_follows_archive_symbol_mapping_for_watchlist_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("NI0", primary_driver="印尼政策", research_root=tmpdir)
            # NI0 archive stays 'watchlist' while open ledger position trades NI2701 mapped to archive_symbol='NI0'
            open_pos = [{"symbol": "NI2701", "archive_symbol": "NI0", "market": "CN_FUTURES", "status": "open"}]
            with patch.object(ds, "_load_all_positions", return_value=(open_pos, [], [])):
                archives, errs = trigger_verifier._discover_target_archives(
                    None, None, research_root=tmpdir
                )
            self.assertEqual(errs, [])
            self.assertEqual([a["symbol"] for a in archives], ["NI0"])

    def test_corrupt_evidence_in_first_holding_does_not_abort_discovery_of_healthy_holding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("NI0", primary_driver="印尼政策", research_root=tmpdir)
            ra.init_archive("SN0", primary_driver="缅甸复产", research_root=tmpdir)
            ni_folder, _, _, _ = ra.resolve_archive_dir("NI0", research_root=tmpdir)
            with open(os.path.join(ni_folder, "evidence.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("{corrupted json line\n")
            open_pos = [
                {"symbol": "NI0", "market": "CN_FUTURES", "status": "open"},
                {"symbol": "SN0", "market": "CN_FUTURES", "status": "open"},
            ]
            with patch.object(ds, "_load_all_positions", return_value=(open_pos, [], [])):
                archives, errs = trigger_verifier._discover_target_archives(None, None, research_root=tmpdir)
            self.assertEqual([a["symbol"] for a in archives], ["SN0"])
            self.assertTrue(any("NI0" in e for e in errs))

    def test_numeric_equality_trigger_rejects_unsubstantiated_clear_and_sentinel_preserves_fired(self):
        now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
        trig_eq = {
            "id": "trig_eq_num",
            "kind": "policy",
            "evaluation_mode": "manual",
            "metric": "export_quota_stage",
            "operator": "==",
            "threshold": 0,
            "action": "exit_review",
            "status": "armed",
            "verification": {
                "method": "manual",
                "status": "clear",
                "summary": "无来源L4口头确认",
                "evidence_tier": "L4",
                "verified_at_utc": "2026-11-25T10:00:00Z",
                "ttl_days": 7,
            },
        }
        ev_eq = ra.evaluate_triggers({"triggers": [trig_eq]}, {}, now_utc=now)[0]
        self.assertFalse(ev_eq["evaluated"])

        # Numeric '==' must coerce numeric string "0" to fired and reject non-numeric "invalid"
        ok_str_zero, fired_str_zero, _ = ra._compare_trigger_value("==", "0", 0)
        self.assertTrue(ok_str_zero)
        self.assertTrue(fired_str_zero)
        ok_invalid, fired_invalid, _ = ra._compare_trigger_value("==", "invalid", 0)
        self.assertFalse(ok_invalid)
        self.assertFalse(fired_invalid)

        # Sentinel archive writeback must preserve explicit qualitative 'fired' verification
        fired_trig = {
            "id": "trig_qual_fired",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "indonesia_rkab_2027_quota_mt",
            "operator": "<",
            "threshold": 250,
            "action": "exit_review",
            "status": "armed",
            "verification": {
                "method": "ai_web",
                "status": "fired",
                "observed": 260,
                "summary": "附加禁令已触发撤离红线",
                "evidence_tier": "L1",
                "source_title": "ESDM",
                "verified_at_utc": ra.utc_now_iso_seconds(),
                "ttl_days": 7,
            },
        }
        matched = ra.evaluate_triggers({"triggers": [fired_trig]}, {})[0]
        self.assertTrue(matched["fired"])
        patch_obj = ds._build_surgical_trigger_patch(
            {"triggers": [fired_trig]},
            [matched],
            checked_ts=ra.utc_now_str(),
        )
        self.assertEqual(patch_obj["triggers"][0]["status"], "fired")


class TestValidationAndApiContracts(unittest.TestCase):
    def test_duplicate_trigger_ids_and_malformed_urls_rejected_by_archive_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("NI0", primary_driver="印尼政策", research_root=tmpdir)
            with self.assertRaises(ValueError):
                ra.update_state(
                    "NI0",
                    {
                        "triggers": [
                            {"id": "dup", "metric": "m1", "operator": ">", "threshold": 1, "action": "alert"},
                            {"id": "dup", "metric": "m2", "operator": ">", "threshold": 2, "action": "alert"},
                        ]
                    },
                    research_root=tmpdir,
                )
            with self.assertRaises(ValueError):
                ra.update_state(
                    "NI0",
                    {
                        "triggers": [
                            {
                                "id": "t_bad_url",
                                "metric": "m1",
                                "operator": "event",
                                "threshold": True,
                                "action": "alert",
                                "verification": {
                                    "method": "manual",
                                    "status": "clear",
                                    "summary": "ok",
                                    "verified_at_utc": "2026-11-25 06:00 UTC",
                                    "source_url": "https://",
                                },
                            }
                        ]
                    },
                    research_root=tmpdir,
                )

    def test_manual_verify_endpoint_requires_explicit_status_summary_and_blocks_auto_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("NI0", primary_driver="印尼政策", research_root=tmpdir)
            ra.update_state(
                "NI0",
                {
                    "triggers": [
                        {
                            "id": "trig_close",
                            "kind": "chanlun",
                            "evaluation_mode": "auto",
                            "metric": "close",
                            "operator": "<=",
                            "threshold": 110000,
                            "action": "exit_review",
                            "status": "armed",
                        },
                        {
                            "id": "trig_rkab",
                            "kind": "policy",
                            "evaluation_mode": "ai_web",
                            "metric": "indonesia_rkab_2027_quota_mt",
                            "operator": "<",
                            "threshold": 250,
                            "action": "exit_review",
                            "status": "armed",
                        },
                    ]
                },
                research_root=tmpdir,
            )
            with patch.object(ra, "DEFAULT_RESEARCH_DIR", tmpdir):
                client = TestClient(backend_app.create_app())

                # Missing status/summary -> 422
                r_missing = client.post(
                    "/api/research/archive/trigger-verify-manual",
                    json={"symbol": "NI0", "market": "CN_FUTURES", "trigger_id": "trig_rkab"},
                )
                self.assertEqual(r_missing.status_code, 422)

                # Invalid URL (missing host or invalid port) -> 422
                for bad_url in ("https://", "https://example.com:nope/"):
                    r_bad_url = client.post(
                        "/api/research/archive/trigger-verify-manual",
                        json={
                            "symbol": "NI0",
                            "market": "CN_FUTURES",
                            "trigger_id": "trig_rkab",
                            "status": "clear",
                            "observed": 280,
                            "summary": "确认未触发",
                            "source_url": bad_url,
                        },
                    )
                    self.assertEqual(r_bad_url.status_code, 422, bad_url)

                # Numeric clear with no observed value and no provenance -> 422
                r_no_obs = client.post(
                    "/api/research/archive/trigger-verify-manual",
                    json={
                        "symbol": "NI0",
                        "market": "CN_FUTURES",
                        "trigger_id": "trig_rkab",
                        "status": "clear",
                        "summary": "无数据",
                    },
                )
                self.assertEqual(r_no_obs.status_code, 422)

                # Attempting to override auto metric -> 422
                r_auto = client.post(
                    "/api/research/archive/trigger-verify-manual",
                    json={
                        "symbol": "NI0",
                        "market": "CN_FUTURES",
                        "trigger_id": "trig_close",
                        "status": "clear",
                        "observed": 120000,
                        "summary": "试图覆盖自动行情",
                    },
                )
                self.assertEqual(r_auto.status_code, 422)

                # Valid manual verify -> 200 and GET /api/research/archive exposes authoritative trigger_evaluations
                r_ok = client.post(
                    "/api/research/archive/trigger-verify-manual",
                    json={
                        "symbol": "NI0",
                        "market": "CN_FUTURES",
                        "trigger_id": "trig_rkab",
                        "status": "clear",
                        "observed": 280,
                        "observed_text": "2.8亿湿吨",
                        "summary": "最新消息显示暂未削减至2.5亿湿吨以下",
                        "source_url": "https://example.com/rkab",
                        "evidence_tier": "L2",
                        "ttl_days": 7,
                    },
                )
                self.assertEqual(r_ok.status_code, 200, r_ok.text)
                self.assertEqual(r_ok.json()["updated_ids"], ["trig_rkab"])

                r_get = client.get("/api/research/archive", params={"symbol": "NI0", "market": "CN_FUTURES"})
                self.assertEqual(r_get.status_code, 200)
                evals = r_get.json().get("trigger_evaluations") or {}
                self.assertTrue(evals["trig_rkab"]["evaluated"])
                self.assertFalse(evals["trig_rkab"]["fired"])
                self.assertTrue(evals["trig_close"]["is_auto"])

    def test_deep_research_normalization_sets_evaluation_mode(self):
        payload = {
            "thesis_summary": "测试",
            "direction_bias": "long",
            "status": "watchlist",
            "thesis_md": "# 沪镍（NI0）深度研究测试正文\n\n" + ("印尼政策与镍产业链供需平衡表分析。" * 6),
            "triggers": [
                {
                    "id": "t_auto",
                    "kind": "chanlun",
                    "metric": "close",
                    "operator": "<=",
                    "threshold": 110000,
                    "action": "exit_review",
                },
                {
                    "id": "t_policy",
                    "kind": "policy",
                    "metric": "indonesia_rkab_2027_quota_mt",
                    "operator": "<",
                    "threshold": 250,
                    "action": "exit_review",
                },
            ],
        }
        norm = deep_research.normalize_research_output(
            payload, existing_state={"status": "watchlist"}, symbol="NI0"
        )
        by_id = {t["id"]: t for t in norm["triggers"]}
        self.assertEqual(by_id["t_auto"]["evaluation_mode"], "auto")
        self.assertEqual(by_id["t_policy"]["evaluation_mode"], "ai_web")


if __name__ == "__main__":
    unittest.main()
