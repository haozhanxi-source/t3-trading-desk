#!/usr/bin/env python3
"""Offline unit tests for Layer 3 incremental research archive manager."""
# ruff: noqa: EXE001
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from research_archive import (
    VALID_CROWDING_ASSESSMENTS,
    append_evidence,
    append_log,
    evaluate_triggers,
    init_archive,
    list_archives,
    load_archive,
    normalize_expectation_gap,
    record_trigger_verifications,
    resolve_archive_dir,
    update_state,
)
from research_archive import main as archive_main


class TestResearchArchive(unittest.TestCase):
    def test_init_load_and_templates_equity_vs_commodity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_eq = init_archive(
                "PWR",
                name="Quanta Services",
                subtype="A成长+B工程周期",
                thesis_summary="电网并网刚需驱动 1~3 年订单兑现",
                primary_driver="维度3技术迭代",
                secondary_driver="维度7订单积压",
                cik="0001050915",
                research_root=tmpdir,
            )
            self.assertEqual(res_eq["status"], "created")
            loaded_eq = load_archive("PWR", research_root=tmpdir)
            self.assertEqual(loaded_eq["market"], "US")
            self.assertEqual(loaded_eq["state"]["revision"], 1)
            self.assertIn("情景压力测试与现价隐含预期拆解", loaded_eq["thesis_md"])
            self.assertIn("一手财报与订单核验清单（10 项", loaded_eq["thesis_md"])
            self.assertIn("红蓝辩论反方必答 7 问 + 红队 Pre-Mortem", loaded_eq["thesis_md"])

            # Re-initializing without overwrite should not clobber
            res_dup = init_archive("PWR", thesis_summary="should not overwrite", research_root=tmpdir)
            self.assertEqual(res_dup["status"], "exists")
            self.assertEqual(res_dup["state"]["thesis_summary"], "电网并网刚需驱动 1~3 年订单兑现")

            # Commodity archive uses S&D balance & resource-nation policy template + safe dir name
            res_cmd = init_archive("LC0", name="碳酸锂", subtype="新能源材料", research_root=tmpdir)
            self.assertEqual(res_cmd["status"], "created")
            loaded_cmd = load_archive("碳酸锂", research_root=tmpdir)
            self.assertEqual(loaded_cmd["market"], "CN_FUTURES")
            self.assertIn("1Q–1Y 供需平衡表预期", loaded_cmd["thesis_md"])
            self.assertIn("资源国政策叙事追踪", loaded_cmd["thesis_md"])
            self.assertIn("红蓝辩论反方必答 5 问", loaded_cmd["thesis_md"])

            # Overseas commodity CL=F -> collision-free safe folder CL_3D_F (distinct from CL_F)
            folder, market, norm, _ = resolve_archive_dir("CL=F", research_root=tmpdir)
            self.assertEqual(market, "COMMODITIES")
            self.assertEqual(norm, "CL=F")
            self.assertTrue(folder.endswith(os.path.join("COMMODITIES", "CL_3D_F")))
            folder_u, _, _, _ = resolve_archive_dir("CL_F", research_root=tmpdir, market_override="COMMODITIES")
            self.assertNotEqual(folder, folder_u)
            f_at, _, _, _ = resolve_archive_dir("X@Y", research_root=tmpdir, market_override="US")
            f_enc, _, _, _ = resolve_archive_dir("X_X40_Y", research_root=tmpdir, market_override="US")
            self.assertNotEqual(f_at, f_enc)

    def test_append_evidence_dedup_and_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_archive("PWR", research_root=tmpdir)
            items = [
                {
                    "metric": "RPO_backlog",
                    "value": 63.7,
                    "unit": "USD_B",
                    "period": "2026Q3",
                    "basis": "GAAP_10Q",
                    "kind": "fact",
                    "level": "L1",
                    "source_url": "https://www.sec.gov/example",
                },
                {
                    "metric": "RPO_backlog",
                    "value": 63.7,
                    "unit": "USD_B",
                    "period": "2026Q3",
                    "basis": "GAAP_10Q",
                    "kind": "fact",
                    "level": "L1",
                    "source_url": "https://www.sec.gov/example",
                },
            ]
            out1 = append_evidence("PWR", items, research_root=tmpdir)
            self.assertEqual(out1["added"], 1)
            self.assertEqual(out1["skipped_duplicates"], 1)

            # Re-appending same item across calls is still deduplicated
            out2 = append_evidence("PWR", [items[0]], research_root=tmpdir)
            self.assertEqual(out2["added"], 0)
            self.assertEqual(out2["skipped_duplicates"], 1)

            with self.assertRaises(ValueError):
                append_evidence("PWR", [{"metric": "x", "level": "L9"}], research_root=tmpdir)

    def test_optimistic_revision_update_triggers_log_and_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_archive("PWR", name="Quanta Services", research_root=tmpdir)
            st2 = update_state(
                "PWR",
                {
                    "status": "watchlist",
                    "direction_bias": "long",
                    "chanlun_anchors": {"zd": 554.03, "zg": 724.68, "tactical_stop": 576.88},
                    "source_watermarks": {"sec_edgar": "2026-10-29"},
                    "triggers": [
                        {"id": "t1", "kind": "chanlun", "metric": "close", "operator": "<", "threshold": 554.03, "action": "三卖否决"},
                        {"id": "t2", "kind": "fundamental", "metric": "book_to_bill", "operator": ">=", "threshold": 1.1, "action": "维持多头"},
                    ],
                },
                expected_revision=1,
                research_root=tmpdir,
            )
            self.assertEqual(st2["revision"], 2)
            self.assertEqual(st2["chanlun_anchors"]["zd"], 554.03)
            self.assertEqual(st2["source_watermarks"]["sec_edgar"], "2026-10-29")

            # Stale expected_revision must fail
            with self.assertRaises(ValueError):
                update_state("PWR", {"status": "holding"}, expected_revision=1, research_root=tmpdir)

            # Invalid status must fail
            with self.assertRaises(ValueError):
                update_state("PWR", {"status": "invalid_status"}, research_root=tmpdir)

            # Evaluate triggers
            trigs = evaluate_triggers(st2, {"close": 540.0, "book_to_bill": 1.25})
            self.assertTrue(trigs[0]["fired"])
            self.assertTrue(trigs[1]["fired"])
            trigs_miss = evaluate_triggers(st2, {"close": 602.6})
            self.assertFalse(trigs_miss[0]["fired"])
            self.assertFalse(trigs_miss[1]["evaluated"])

            # Append log
            line = append_log("PWR", "2026Q3 财报后维持观察池，等待站稳 ZD。", research_root=tmpdir)
            self.assertIn("2026Q3 财报后维持观察池", line)
            loaded = load_archive("PWR", research_root=tmpdir)
            self.assertIn("2026Q3 财报后维持观察池", loaded["thesis_md"])

            # List archives
            listed = list_archives(research_root=tmpdir)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["symbol"], "PWR")
            self.assertEqual(listed[0]["revision"], 2)

            # Unknown archive exits non-zero in CLI
            with self.assertRaises(SystemExit) as ctx:
                archive_main(["--root", tmpdir, "load", "DOESNOTEXIST"])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_codex_archive_security_concurrency_atomicity_and_triggers(self):
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) Path boundary validation: reject '..', '.', invalid market_override
            for bad in ("..", ".", "../etc/passwd", "foo/bar/baz"):
                with self.assertRaises(ValueError):
                    resolve_archive_dir(bad, research_root=tmpdir)
            with self.assertRaises(ValueError):
                resolve_archive_dir("PWR", research_root=tmpdir, market_override="../escape")

            # 2) Concurrent update_state with same expected_revision: exactly ONE succeeds, ONE raises conflict
            init_archive("PWR", research_root=tmpdir)
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            lock = threading.Lock()

            def worker(tag: str):
                barrier.wait()
                try:
                    update_state("PWR", {"thesis_summary": f"t-{tag}"}, expected_revision=1, research_root=tmpdir)
                    with lock:
                        outcomes.append("ok")
                except ValueError:
                    with lock:
                        outcomes.append("conflict")

            t1 = threading.Thread(target=worker, args=("A",))
            t2 = threading.Thread(target=worker, args=("B",))
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(sorted(outcomes), ["conflict", "ok"])

            # 3) Batch append_evidence validates ALL before writing: invalid 2nd item writes 0 items
            with self.assertRaises(ValueError):
                append_evidence(
                    "PWR",
                    [
                        {"metric": "good_metric", "value": 1, "level": "L1"},
                        {"metric": "bad_metric", "value": 2, "level": "INVALID"},
                    ],
                    research_root=tmpdir,
                )
            self.assertEqual(load_archive("PWR", research_root=tmpdir)["evidence_count"], 0)

            # 4) source_locator differentiates distinct facts with same metric/value
            res_loc = append_evidence(
                "PWR",
                [
                    {"metric": "capex", "value": 100, "level": "L1", "source_locator": "Note 4"},
                    {"metric": "capex", "value": 100, "level": "L1", "source_locator": "Note 12"},
                ],
                research_root=tmpdir,
            )
            self.assertEqual(res_loc["added"], 2)

            # 5) State patch rejects triggers=None & non-string metric; evaluate_triggers rejects "Inf"
            with self.assertRaises(ValueError):
                update_state("PWR", {"triggers": None}, research_root=tmpdir)
            with self.assertRaises(ValueError):
                update_state(
                    "PWR",
                    {"triggers": [{"id": "bad", "metric": ["x"], "operator": ">", "threshold": 1, "action": "a"}]},
                    research_root=tmpdir,
                )
            st = update_state(
                "PWR",
                {
                    "triggers": [
                        {"id": "e1", "kind": "policy", "metric": "ban_effective", "operator": "event", "threshold": True, "action": "重估供给"},
                        {"id": "n1", "kind": "chanlun", "metric": "close", "operator": ">", "threshold": 550.0, "action": "突破"},
                    ]
                },
                research_root=tmpdir,
            )
            ev_res = evaluate_triggers(st, {"ban_effective": True, "close": "Inf"})
            self.assertTrue(ev_res[0]["evaluated"])
            self.assertTrue(ev_res[0]["fired"])
            self.assertFalse(ev_res[1]["evaluated"])
            self.assertFalse(ev_res[1]["fired"])
            self.assertIn("error", ev_res[1])

            # 6) init --overwrite rolls back thesis.md & evidence.jsonl if final state write fails
            from unittest.mock import patch

            import research_archive as ra
            append_log("PWR", "preserved-note", research_root=tmpdir)
            with patch.object(ra, "_atomic_write_json", side_effect=OSError("disk full")), self.assertRaises(OSError):
                init_archive("PWR", thesis_summary="broken overwrite", research_root=tmpdir, overwrite=True)
            after_fail = load_archive("PWR", research_root=tmpdir)
            self.assertIn("preserved-note", after_fail["thesis_md"])
            self.assertEqual(after_fail["evidence_count"], 2)

            # 7) FX archive template, commodity trading-structure section, and new trigger kinds
            init_archive("USDJPY=X", name="美元日元", thesis_summary="美日利差收窄测试", research_root=tmpdir)
            fx_loaded = load_archive("USDJPY=X", research_root=tmpdir)
            self.assertEqual(fx_loaded["market"], "FX")
            self.assertIn("利差与货币政策路径", fx_loaded["thesis_md"])
            self.assertIn("红蓝辩论反方必答 5 问", fx_loaded["thesis_md"])
            self.assertIn("第四层：交易表达、缠论执行与触发器监控", fx_loaded["thesis_md"])
            fx_st = update_state(
                "USDJPY=X",
                {
                    "triggers": [
                        {"id": "r1", "kind": "rate_diff", "metric": "us_jp_10y_bp", "operator": "<", "threshold": 200.0, "action": "利差收窄"},
                        {"id": "t1", "kind": "term_structure", "metric": "roll_yield_pct", "operator": "<", "threshold": 0.0, "action": "转Contango"},
                        {"id": "f1", "kind": "freight", "metric": "bdti", "operator": ">", "threshold": 5500.0, "action": "运费预警"},
                        {"id": "s1", "kind": "spread", "metric": "brent_wti", "operator": "<", "threshold": 4.0, "action": "跨区收窄"},
                    ]
                },
                research_root=tmpdir,
            )
            self.assertEqual(len(fx_st["triggers"]), 4)
            with self.assertRaises(ValueError):
                update_state(
                    "USDJPY=X",
                    {"triggers": [{"id": "badk", "kind": "unknown_kind", "metric": "x", "operator": ">", "threshold": 1, "action": "a"}]},
                    research_root=tmpdir,
                )
            with self.assertRaises(ValueError):
                update_state(
                    "USDJPY=X",
                    {"triggers": [{"id": "nullk", "kind": None, "metric": "x", "operator": ">", "threshold": 1, "action": "a"}]},
                    research_root=tmpdir,
                )
            self.assertIn("核心宏观与利差证伪阈值", fx_loaded["thesis_md"])
            self.assertIn("30min 缠论结构中诞生交易", fx_loaded["thesis_md"])
            self.assertIn("核心逻辑或关键指标一旦松动随时果断撤离", fx_loaded["thesis_md"])
            init_archive("CL=F", name="WTI原油", research_root=tmpdir)
            cl_loaded = load_archive("CL=F", research_root=tmpdir)
            self.assertIn("供需与交易结构证伪阈值", cl_loaded["thesis_md"])
            self.assertIn("大 B 结构绝非做空禁区", cl_loaded["thesis_md"])
            self.assertIn("单一板块龙头独占制", cl_loaded["thesis_md"])

    def test_init_and_update_state_direction_drivers_status_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) init_archive supports explicit direction_bias, layer1_primary, layer1_secondary, thesis_summary, confidence
            res = init_archive(
                "NVDA",
                name="NVIDIA",
                subtype="A成长",
                thesis_summary="AI 算力代际迭代驱动 1~3 年盈利上修",
                direction_bias="long",
                layer1_primary="维度3技术与产业迭代",
                layer1_secondary="维度1宏观流动性",
                confidence="high",
                research_root=tmpdir,
            )
            self.assertEqual(res["status"], "created")
            st = res["state"]
            self.assertEqual(st["direction_bias"], "long")
            self.assertEqual(st["layer1_drivers"]["primary"], "维度3技术与产业迭代")
            self.assertEqual(st["layer1_drivers"]["secondary"], "维度1宏观流动性")
            self.assertEqual(st["thesis_summary"], "AI 算力代际迭代驱动 1~3 年盈利上修")
            self.assertEqual(st["confidence"], "high")
            self.assertEqual(st["revision"], 1)

            loaded = load_archive("NVDA", research_root=tmpdir)
            self.assertIn("AI 算力代际迭代驱动 1~3 年盈利上修", loaded["thesis_md"])
            self.assertIn("维度3技术与产业迭代", loaded["thesis_md"])
            self.assertIn("维度1宏观流动性", loaded["thesis_md"])
            self.assertIn("做多 (long)", loaded["thesis_md"])
            self.assertNotIn("临时自选", loaded["thesis_md"])

            # Invalid direction_bias or confidence in init_archive MUST raise ValueError
            with self.assertRaises(ValueError):
                init_archive("AMD", direction_bias="bullish", research_root=tmpdir)
            with self.assertRaises(ValueError):
                init_archive("AMD", confidence="ultra", research_root=tmpdir)
            with self.assertRaises(ValueError):
                init_archive("AMD", confidence=1.5, research_root=tmpdir)
            with self.assertRaises(ValueError):
                init_archive("AMD", confidence=True, research_root=tmpdir)

            # 2) update_state supports atomic update of direction_bias, thesis_summary, layer1_primary/secondary,
            #    status (watchlist/holding/archived), confidence (enum & [0, 1]), updating revision & updated_at_utc
            st2 = update_state(
                "NVDA",
                {
                    "direction_bias": "short",
                    "thesis_summary": "更新后的核心论点",
                    "layer1_primary": "维度4供需拐点",
                    "status": "holding",
                    "confidence": 0.75,
                },
                expected_revision=1,
                research_root=tmpdir,
            )
            self.assertEqual(st2["revision"], 2)
            self.assertEqual(st2["direction_bias"], "short")
            self.assertEqual(st2["thesis_summary"], "更新后的核心论点")
            self.assertEqual(st2["layer1_drivers"]["primary"], "维度4供需拐点")
            # Existing secondary preserved when only layer1_primary is patched
            self.assertEqual(st2["layer1_drivers"]["secondary"], "维度1宏观流动性")
            self.assertNotIn("layer1_primary", st2)
            self.assertNotIn("layer1_secondary", st2)
            self.assertEqual(st2["status"], "holding")
            self.assertEqual(st2["confidence"], 0.75)
            self.assertTrue(bool(st2.get("updated_at_utc")))

            # Patching status -> archived, direction_bias -> neutral, layer1_secondary, confidence -> medium
            st3 = update_state(
                "NVDA",
                {
                    "status": "archived",
                    "direction_bias": "neutral",
                    "layer1_secondary": "维度6资金流拥挤",
                    "confidence": "medium",
                },
                expected_revision=2,
                research_root=tmpdir,
            )
            self.assertEqual(st3["revision"], 3)
            self.assertEqual(st3["status"], "archived")
            self.assertEqual(st3["direction_bias"], "neutral")
            self.assertEqual(st3["layer1_drivers"]["primary"], "维度4供需拐点")
            self.assertEqual(st3["layer1_drivers"]["secondary"], "维度6资金流拥挤")
            self.assertEqual(st3["confidence"], "medium")

            # Patching confidence=None clears it cleanly
            st4 = update_state("NVDA", {"confidence": None}, expected_revision=3, research_root=tmpdir)
            self.assertEqual(st4["revision"], 4)
            self.assertIsNone(st4["confidence"])

            # Invalid patches MUST be rejected atomically without bumping revision
            for bad_patch in (
                {"direction_bias": "bullish"},
                {"direction_bias": None},
                {"status": "deleted"},
                {"confidence": "extreme"},
                {"confidence": -0.1},
                {"confidence": 1.2},
                {"confidence": True},
                {"layer1_primary": 123},
                {"layer1_secondary": ["bad"]},
                {"thesis_summary": 999},
            ):
                with self.assertRaises(ValueError, msg=f"Should reject {bad_patch}"):
                    update_state("NVDA", bad_patch, research_root=tmpdir)
            self.assertEqual(load_archive("NVDA", research_root=tmpdir)["state"]["revision"], 4)

            # 3) CLI init supports --direction-bias, --layer1-primary, --layer1-secondary, --confidence
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                archive_main([
                    "--root", tmpdir,
                    "init", "CU0",
                    "--name", "沪铜",
                    "--direction-bias", "short",
                    "--layer1-primary", "维度4供需过剩预期",
                    "--layer1-secondary", "维度1中国地产偏弱",
                    "--thesis", "强现实弱预期劈叉做空远月",
                    "--confidence", "medium",
                ])
            cli_out = json.loads(buf.getvalue())
            self.assertEqual(cli_out["status"], "created")
            self.assertEqual(cli_out["state"]["direction_bias"], "short")
            self.assertEqual(cli_out["state"]["layer1_drivers"]["primary"], "维度4供需过剩预期")
            self.assertEqual(cli_out["state"]["layer1_drivers"]["secondary"], "维度1中国地产偏弱")
            self.assertEqual(cli_out["state"]["confidence"], "medium")

            # 4) P2-4: overwrite=True MUST monotonically increment revision (old_revision + 1), never reset to 1
            r1 = init_archive("PWR", name="Quanta", direction_bias="long", research_root=tmpdir)
            self.assertEqual(r1["state"]["revision"], 1)
            st2 = update_state("PWR", {"confidence": "high"}, expected_revision=1, research_root=tmpdir)
            self.assertEqual(st2["revision"], 2)

            r_over = init_archive(
                "PWR",
                name="Quanta Rebuilt",
                direction_bias="short",
                thesis_summary="Rebuilt thesis",
                overwrite=True,
                research_root=tmpdir,
            )
            self.assertEqual(r_over["status"], "created")
            self.assertEqual(r_over["state"]["revision"], 3)
            # Stale client holding expected_revision=1 MUST be rejected with Revision conflict
            with self.assertRaises(ValueError) as ctx_rev:
                update_state("PWR", {"confidence": "low"}, expected_revision=1, research_root=tmpdir)
            self.assertIn("Revision conflict", str(ctx_rev.exception))

    def test_expectation_gap_init_update_partial_reset_validation_and_legacy_compat(self):
        self.assertEqual(VALID_CROWDING_ASSESSMENTS, {"underpriced", "fair", "crowded", "unknown"})
        default_eg = {
            "priced_in_consensus": "",
            "our_variant_view": "",
            "crowding_assessment": "unknown",
            "leading_kpis": [],
        }
        self.assertEqual(normalize_expectation_gap(None), default_eg)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) init_archive sets clean default expectation_gap across asset classes
            for sym in ("NVDA", "NI0", "USDJPY=X"):
                res = init_archive(sym, research_root=tmpdir)
                self.assertEqual(res["state"]["expectation_gap"], default_eg)
                loaded = load_archive(sym, research_root=tmpdir)
                self.assertEqual(loaded["state"]["expectation_gap"], default_eg)

            # 2) Full update_state with leading_kpis (up to 6) and whitespace/case normalization
            st2 = update_state(
                "NI0",
                {
                    "expectation_gap": {
                        "priced_in_consensus": "  市场一致预期 Q1 印尼配额充裕  ",
                        "our_variant_view": "  雨季发运受阻叠加 RKAB 审批延迟带来阶段性错配  ",
                        "crowding_assessment": "  UNDERPRICED  ",
                        "leading_kpis": [
                            {
                                "name": " 菲律宾红土镍矿周度发运量 ",
                                "current_baseline": " 85 万湿吨/周 ",
                                "expected_trajectory": " 12 月降至 45 万湿吨以下 ",
                                "falsification_threshold": " 连续两周 > 90 万湿吨 ",
                                "cadence": " weekly ",
                            },
                            {
                                "name": "印尼镍铁冶炼厂矿石库存天数",
                            },
                        ],
                    }
                },
                expected_revision=1,
                research_root=tmpdir,
            )
            self.assertEqual(st2["revision"], 2)
            self.assertEqual(
                st2["expectation_gap"],
                {
                    "priced_in_consensus": "市场一致预期 Q1 印尼配额充裕",
                    "our_variant_view": "雨季发运受阻叠加 RKAB 审批延迟带来阶段性错配",
                    "crowding_assessment": "underpriced",
                    "leading_kpis": [
                        {
                            "name": "菲律宾红土镍矿周度发运量",
                            "current_baseline": "85 万湿吨/周",
                            "expected_trajectory": "12 月降至 45 万湿吨以下",
                            "falsification_threshold": "连续两周 > 90 万湿吨",
                            "cadence": "weekly",
                        },
                        {
                            "name": "印尼镍铁冶炼厂矿石库存天数",
                            "current_baseline": "",
                            "expected_trajectory": "",
                            "falsification_threshold": "",
                            "cadence": "",
                        },
                    ],
                },
            )

            # 3) Partial update preserves omitted canonical fields (e.g. leading_kpis) & strips stale keys
            folder, _, _, _ = resolve_archive_dir("NI0", research_root=tmpdir)
            state_path = os.path.join(folder, "state.json")
            with open(state_path, "r", encoding="utf-8") as f:
                raw_disk = json.load(f)
            raw_disk["expectation_gap"]["stale_legacy_field"] = "must_be_removed"
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(raw_disk, f)

            st3 = update_state(
                "NI0",
                {
                    "expectation_gap": {
                        "crowding_assessment": "fair",
                        "our_variant_view": "更新后的二阶预期差",
                    }
                },
                expected_revision=2,
                research_root=tmpdir,
            )
            self.assertEqual(st3["revision"], 3)
            self.assertNotIn("stale_legacy_field", st3["expectation_gap"])
            self.assertEqual(st3["expectation_gap"]["priced_in_consensus"], "市场一致预期 Q1 印尼配额充裕")
            self.assertEqual(st3["expectation_gap"]["our_variant_view"], "更新后的二阶预期差")
            self.assertEqual(st3["expectation_gap"]["crowding_assessment"], "fair")
            self.assertEqual(len(st3["expectation_gap"]["leading_kpis"]), 2)

            # Partial update of leading_kpis only preserves text fields and crowding_assessment
            six_kpis = [{"name": f"KPI_{i}", "cadence": "monthly"} for i in range(6)]
            st4 = update_state(
                "NI0",
                {"expectation_gap": {"leading_kpis": six_kpis}},
                expected_revision=3,
                research_root=tmpdir,
            )
            self.assertEqual(st4["expectation_gap"]["our_variant_view"], "更新后的二阶预期差")
            self.assertEqual(st4["expectation_gap"]["crowding_assessment"], "fair")
            self.assertEqual(len(st4["expectation_gap"]["leading_kpis"]), 6)

            # 4) Invalid patches rejected atomically without bumping revision
            for bad_eg in (
                "not-a-dict",
                [],
                123,
                {"priced_in_consensus": 123},
                {"priced_in_consensus": None},
                {"our_variant_view": ["bad"]},
                {"crowding_assessment": "bubble"},
                {"crowding_assessment": ""},
                {"crowding_assessment": None},
                {"crowding_assessment": 1},
                {"leading_kpis": "not-a-list"},
                {"leading_kpis": None},
                {"leading_kpis": ["not-a-dict"]},
                {"leading_kpis": [{}]},
                {"leading_kpis": [{"name": "   "}]},
                {"leading_kpis": [{"name": 123}]},
                {"leading_kpis": [{"name": "ok", "current_baseline": 999}]},
                {"leading_kpis": [{"name": f"k{i}"} for i in range(7)]},
            ):
                with self.assertRaises(ValueError, msg=f"Should reject expectation_gap={bad_eg!r}"):
                    update_state("NI0", {"expectation_gap": bad_eg}, research_root=tmpdir)
            self.assertEqual(load_archive("NI0", research_root=tmpdir)["state"]["revision"], 4)

            # 5) None resets expectation_gap cleanly to default empty structure
            st5 = update_state("NI0", {"expectation_gap": None}, expected_revision=4, research_root=tmpdir)
            self.assertEqual(st5["revision"], 5)
            self.assertEqual(st5["expectation_gap"], default_eg)

            # 6) Backward compatibility with legacy state.json lacking expectation_gap
            nvda_folder, _, _, _ = resolve_archive_dir("NVDA", research_root=tmpdir)
            nvda_state_path = os.path.join(nvda_folder, "state.json")
            with open(nvda_state_path, "r", encoding="utf-8") as f:
                legacy_state = json.load(f)
            legacy_state.pop("expectation_gap", None)
            with open(nvda_state_path, "w", encoding="utf-8") as f:
                json.dump(legacy_state, f)

            legacy_loaded = load_archive("NVDA", research_root=tmpdir)
            self.assertEqual(legacy_loaded["state"]["expectation_gap"], default_eg)
            exists_res = init_archive("NVDA", research_root=tmpdir)
            self.assertEqual(exists_res["status"], "exists")
            self.assertEqual(exists_res["state"]["expectation_gap"], default_eg)

            legacy_patched = update_state(
                "NVDA",
                {"expectation_gap": {"our_variant_view": "Blackwell 机柜交付超预期"}},
                expected_revision=1,
                research_root=tmpdir,
            )
            self.assertEqual(legacy_patched["expectation_gap"]["our_variant_view"], "Blackwell 机柜交付超预期")
            self.assertEqual(legacy_patched["expectation_gap"]["crowding_assessment"], "unknown")
            self.assertEqual(legacy_patched["expectation_gap"]["leading_kpis"], [])

            # 7) Strict write-path validation rejects misspelled/unknown keys without bumping revision
            rev_before = load_archive("NVDA", research_root=tmpdir)["state"]["revision"]
            for bad_unknown in (
                {"crowding_assesment": "underpriced"},
                {"our_variant_view": "valid", "unknown_top_key": "nope"},
                {"leading_kpis": [{"name": "KPI_1", "cadnce": "weekly"}]},
                {"leading_kpis": [{"name": "KPI_1", "extra_field": "bad"}]},
            ):
                with self.assertRaises(ValueError, msg=f"Should reject unknown key in {bad_unknown!r}"):
                    update_state("NVDA", {"expectation_gap": bad_unknown}, research_root=tmpdir)
            self.assertEqual(load_archive("NVDA", research_root=tmpdir)["state"]["revision"], rev_before)

            # strict=False remains tolerant for legacy disk reads, while strict=True rejects unknown keys
            self.assertEqual(
                normalize_expectation_gap({"crowding_assesment": "underpriced"}, strict=False)["crowding_assessment"],
                "unknown",
            )
            with self.assertRaises(ValueError):
                normalize_expectation_gap({"crowding_assesment": "underpriced"}, strict=True)
            with self.assertRaises(ValueError):
                normalize_expectation_gap({"leading_kpis": [{"name": "x", "typo": "y"}]}, strict=True)

    def test_trigger_verification_ttl_enforces_stricter_of_trigger_and_verification(self):
        ver_2d_ago = {
            "method": "ai_web",
            "status": "clear",
            "verified_at_utc": "2026-11-25T08:00:00Z",
            "ttl_days": 7,
            "summary": "核验未触发",
            "source_url": "https://www.esdm.go.id/en",
            "evidence_tier": "L1",
        }
        now_dt = datetime.fromisoformat("2026-11-27T09:00:00+00:00")

        # Shortening trigger verification_ttl_days or ttl_days to 1 MUST expire 2-day-old ver with ttl_days=7
        state = {
            "triggers": [
                {
                    "id": "trig_short_ver_ttl",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "export_ban",
                    "operator": "event",
                    "threshold": True,
                    "action": "exit_review",
                    "verification_ttl_days": 1,
                    "verification": ver_2d_ago,
                },
                {
                    "id": "trig_short_alias_ttl",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "export_ban_2",
                    "operator": "event",
                    "threshold": True,
                    "action": "exit_review",
                    "ttl_days": 1,
                    "verification": ver_2d_ago,
                },
                {
                    "id": "trig_ver_stricter",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "export_ban_3",
                    "operator": "event",
                    "threshold": True,
                    "action": "exit_review",
                    "verification_ttl_days": 7,
                    "verification": {**ver_2d_ago, "ttl_days": 1},
                },
                {
                    "id": "trig_still_fresh",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "export_ban_4",
                    "operator": "event",
                    "threshold": True,
                    "action": "exit_review",
                    "verification_ttl_days": 7,
                    "verification": ver_2d_ago,
                },
            ]
        }
        eval_by_id = {t["id"]: t for t in evaluate_triggers(state, {}, now_utc=now_dt)}
        for stale_id in ("trig_short_ver_ttl", "trig_short_alias_ttl", "trig_ver_stricter"):
            self.assertFalse(eval_by_id[stale_id]["evaluated"], msg=f"{stale_id} should be expired")
            self.assertTrue(eval_by_id[stale_id].get("verification_stale"), msg=f"{stale_id} should be marked stale")
        self.assertTrue(eval_by_id["trig_still_fresh"]["evaluated"])
        self.assertFalse(eval_by_id["trig_still_fresh"]["fired"])

        # Recording a verification with larger ttl_days=7 on a trigger declaring verification_ttl_days=1 caps to 1
        with tempfile.TemporaryDirectory() as tmpdir:
            init_archive("NI0", research_root=tmpdir)
            update_state(
                "NI0",
                {
                    "triggers": [
                        {
                            "id": "trig_cap",
                            "kind": "policy",
                            "evaluation_mode": "ai_web",
                            "metric": "quota_cap",
                            "operator": "event",
                            "threshold": True,
                            "action": "alert",
                            "verification_ttl_days": 1,
                        }
                    ]
                },
                research_root=tmpdir,
            )
            rec = record_trigger_verifications(
                "NI0",
                {"trig_cap": ver_2d_ago},
                research_root=tmpdir,
            )
            self.assertEqual(rec["state"]["triggers"][0]["verification"]["ttl_days"], 1)


if __name__ == "__main__":
    unittest.main()
