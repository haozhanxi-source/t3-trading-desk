"""Comprehensive unit tests for Track B-2:
1. 3-Stage Offensive Campaign Blueprint & L3 Expectation-Gap Awareness in scripts/trade_planner.py
2. Stage-Aware Anti-Shakeout Main-Wave Defense & Fail-Closed Regime Guards in scripts/daily_sentinel.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import daily_sentinel as ds
import research_archive as ra
import trade_planner as tp
from trade_planner import (
    CROWDED_EXPECTATION_GAP_WARNING,
    build_trade_plan,
    calculate_position_plan,
    evaluate_four_quadrant_gate,
)

UTC = timezone.utc  # noqa: UP017


def _make_armed_state(
    direction: str = "long",
    stop_threshold: float = 95.0,
    extra_triggers: list[dict] | None = None,
) -> dict:
    op = "<=" if direction == "long" else ">="
    trigs = [
        {
            "id": "t_close_guard",
            "kind": "chanlun",
            "metric": "close",
            "operator": op,
            "threshold": stop_threshold,
            "action": "跌破/突破核心防线撤离",
        }
    ]
    if extra_triggers:
        trigs.extend(extra_triggers)
    return {
        "status": "holding",
        "direction_bias": direction,
        "triggers": trigs,
        "events": [],
    }


def _make_campaign_snapshot(
    symbol: str = "PWR",
    price: float = 110.0,
    daily_zd: float = 103.0,
    daily_zg: float = 108.0,
    intra_zd: float = 106.0,
    intra_zg: float = 109.0,
    daily_bias: str = "long",
    intraday_bias: str = "long",
    intraday_action: str = "hold",
    daily_signals: list[str] | None = None,
    intraday_signals: list[str] | None = None,
    daily_signal_meta: dict | None = None,
    intraday_signal_meta: dict | None = None,
    recent_intra_bars: list[dict] | None = None,
    recent_daily_bars: list[dict] | None = None,
    daily_bar: str = "2026-11-24",
    intraday_bar: str = "2026-11-24T20:30:00+00:00",
    atr14_30m: float = 1.2,
    asset_class: str = "us_stock_or_etf",
) -> dict:
    d_summary_meta = daily_signal_meta or {
        "signal_type": "capital_consensus_up" if daily_bias == "long" else "capital_consensus_down",
        "direction": daily_bias,
        "confirmed": True,
        "momentum_confirmed": True,
        "is_exhaustion": False,
    }
    i_summary_meta = intraday_signal_meta or {
        "signal_type": "neutral",
        "direction": intraday_bias,
        "confirmed": False,
        "momentum_confirmed": False,
        "is_exhaustion": False,
    }
    chanlun_block = {
        "signal_summary": {
            "tier": "资金一致推进(顺势)",
            "summary": "日线多头资金方向一致推进" if daily_bias == "long" else "日线空头资金方向一致推进",
            "action_bias": "顺势持有",
            "signal_meta": d_summary_meta,
        },
        "daily": {
            "timeframe": "日线",
            "latest_zhongshu": {"zd": daily_zd, "zg": daily_zg},
            "signals": daily_signals
            if daily_signals is not None
            else (
                ["日线向上离开段·资金方向一致"]
                if daily_bias == "long"
                else ["日线向下离开段·资金方向一致"]
            ),
            "momentum_assessment": {
                "active_direction": "up" if daily_bias == "long" else "down",
                "verdict": "capital_consensus_up" if daily_bias == "long" else "capital_consensus_down",
                "is_capital_consensus": True,
                "is_divergence_candidate": False,
                "is_non_divergence_power": False,
            },
            "stroke_end_trigger": {"direction": "up" if daily_bias == "long" else "down", "triggered": False},
        },
        "intraday_30m": {
            "timeframe": "30min",
            "latest_zhongshu": {"zd": intra_zd, "zg": intra_zg},
            "signals": intraday_signals if intraday_signals is not None else ["30min中枢震荡中"],
            "momentum_assessment": {
                "active_direction": "up" if intraday_bias == "long" else "down",
                "verdict": "neutral_extension_up" if intraday_bias == "long" else "neutral_extension_down",
                "is_capital_consensus": False,
                "is_divergence_candidate": False,
                "is_non_divergence_power": False,
            },
            "stroke_end_trigger": {"direction": "up" if intraday_bias == "long" else "down", "triggered": False},
        },
        "intraday_signal_summary": {
            "tier": "常规结构运行",
            "summary": "30min 顺势运行",
            "action_bias": "继续持有",
            "signal_meta": i_summary_meta,
        },
        "dual_alignment": {
            "daily_bias": daily_bias,
            "intraday_bias": intraday_bias,
            "intraday_action": intraday_action,
            "daily_zd": daily_zd,
            "daily_zg": daily_zg,
            "intraday_zd": intra_zd,
            "intraday_zg": intra_zg,
            "intraday_tactical_stop": intra_zd if daily_bias == "long" else intra_zg,
            "daily_signal_meta": d_summary_meta,
            "intraday_signal_meta": i_summary_meta,
            "aligned": daily_bias == intraday_bias,
            "alignment_status": "aligned" if daily_bias == intraday_bias else "wait_intraday",
            "notes": [],
        },
    }

    intra_tech: dict = {
        "as_of_bar": intraday_bar,
        "as_of_bar_utc": intraday_bar,
        "last_close": price,
        "atr14": atr14_30m,
        "bar_is_closed": True,
        "return_1bar_pct": 0.35,
        "return_5bar_pct": 1.25,
    }
    if recent_intra_bars is not None:
        intra_tech["recent_bars_ohlc"] = recent_intra_bars

    daily_tech: dict = {
        "as_of_bar": daily_bar,
        "last_close": price,
        "atr14": 3.2,
        "bar_is_closed": True,
        "return_1d_pct": 1.15,
        "return_5d_pct": 4.2,
    }
    if recent_daily_bars is not None:
        daily_tech["recent_bars_ohlc"] = recent_daily_bars

    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "technicals": daily_tech,
        "intraday_technicals": intra_tech,
        "chanlun": chanlun_block,
    }


def _make_holding_position(
    symbol: str = "PWR",
    direction: str = "long",
    entry_price: float = 100.0,
    tactical_stop_30m: float = 95.0,
    structural_stop_daily: float = 92.0,
    target_price: float = 135.0,
    active_stop_regime: str | None = None,
    lifecycle_stage: str | None = None,
    opened_at_utc: str = "2026-11-20T15:00:00+00:00",
    **extra: object,
) -> dict:
    pos: dict = {
        "position_id": f"pos_{symbol.lower()}_camp",
        "symbol": symbol,
        "name": symbol,
        "market": "US",
        "asset_class": "us_stock_or_etf",
        "direction": direction,
        "sector_theme": "AI电网基础设施",
        "leader_role": "sole_leader",
        "contract_or_expression": "正股主升战役",
        "entry_price": entry_price,
        "quantity": 200,
        "tactical_stop_30m": tactical_stop_30m,
        "structural_stop_daily": structural_stop_daily,
        "target_price": target_price,
        "exit_red_lines": ["跌破核心防线撤离"],
        "archive_symbol": symbol,
        "opened_at_utc": opened_at_utc,
        "status": "open",
    }
    if active_stop_regime is not None:
        pos["active_stop_regime"] = active_stop_regime
    if lifecycle_stage is not None:
        pos["lifecycle_stage"] = lifecycle_stage
    pos.update(extra)
    return pos


class TestPlannerOffensiveCampaignAndExpectationGap(unittest.TestCase):
    def test_equity_long_3_stage_offensive_campaign_zero_principal_risk_math(self):
        # NVDA long: entry=100, stop30m=95 (1R dist=5), daily_stop=90, equity=100,000, risk=1.0% ($1,000)
        plan = calculate_position_plan(
            symbol="NVDA",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            structural_stop_daily=90.0,
            target_price=125.0,
            account_equity=100_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
        )
        # Existing 40/40/20 tranches remain 100% intact
        self.assertEqual(plan["total_quantity"], 200)
        self.assertEqual([t["quantity"] for t in plan["tranches"]], [80, 80, 40])

        camp = plan["offensive_campaign"]
        self.assertIsNotNone(camp)
        self.assertEqual(camp["campaign_1r_amount"], 1000.0)
        self.assertEqual(camp["unit_1r_price_distance"], 5.0)
        self.assertEqual(camp["full_pilot_qty"], 200)
        self.assertEqual(camp["full_pilot_risk_amount"], 1000.0)
        self.assertEqual(camp["split_pilot_qty"], 80)
        self.assertEqual(camp["breakeven_trigger_price"], 105.0)
        self.assertEqual(camp["pyramid_trigger_price"], 107.5)
        self.assertEqual(camp["main_wave_trigger_price"], 110.0)

        # At +1.5R (107.5): prior_cushion = 7.5 * 200 = $1,500; strict giveback cap = 1500 * 0.65 = $975.0
        # => q_add = floor(200 * 975 / 525) = 371 shares, post_add_total = 571,
        #    blended avg = (100*200 + 107.5*371)/571 = 104.873030, net principal risk == 0.0!
        self.assertEqual(camp["prior_profit_cushion_at_pyramid"], 1500.0)
        self.assertEqual(camp["added_giveback_cap_amount"], 975.0)
        self.assertLessEqual(camp["added_leg_giveback_amount"], camp["added_giveback_cap_amount"])
        self.assertEqual(camp["max_zero_risk_add_qty"], 371)
        self.assertEqual(camp["post_add_total_qty"], 571)
        self.assertEqual(camp["post_add_breakeven_stop"], 104.87303)
        self.assertEqual(camp["new_open_risk"], 0.0)
        # Stage 3 main-wave stop MUST be floored at post_add_breakeven_stop (NOT initial entry 100.0!)
        self.assertEqual(camp["initial_main_wave_stop"], 104.87303)
        self.assertGreaterEqual(camp["initial_main_wave_stop"], camp["post_add_breakeven_stop"])
        self.assertLess(camp["initial_main_wave_stop"], camp["main_wave_trigger_price"])

        # Verify net PnL at both post_add_breakeven_stop and initial_main_wave_stop has zero principal loss
        pilot_pnl = (camp["post_add_breakeven_stop"] - 100.0) * camp["full_pilot_qty"]
        add_pnl = (camp["post_add_breakeven_stop"] - camp["pyramid_trigger_price"]) * camp["max_zero_risk_add_qty"]
        self.assertGreaterEqual(pilot_pnl + add_pnl, 0.0)
        self.assertAlmostEqual(pilot_pnl + add_pnl, 0.0, places=3)

        # Verify 3-stage blueprint schema
        stages = camp["stages"]
        self.assertEqual([s["stage"] for s in stages], ["pilot", "breakeven_pyramid", "main_wave_handover"])
        self.assertEqual(stages[0]["recommended_qty"], 200)
        self.assertEqual(stages[0]["principal_risk_amount"], 1000.0)
        self.assertEqual(stages[1]["trigger_r"], 1.5)
        self.assertEqual(stages[1]["recommended_qty"], 371)
        self.assertEqual(stages[1]["stop_price"], 104.87303)
        self.assertEqual(stages[1]["principal_risk_amount"], 0.0)
        self.assertEqual(stages[2]["trigger_r"], 2.0)
        self.assertEqual(stages[2]["recommended_qty"], 571)
        self.assertEqual(stages[2]["stop_price"], 104.87303)
        self.assertEqual(stages[2]["principal_risk_amount"], 0.0)

    def test_futures_short_and_margin_cap_pyramid_and_extreme_short_clamp(self):
        # 1) CN Futures CU2703 short: mult=5, margin_rate=0.12, entry=95,000, stop30m=96,200 (dist=1,200 => 6,000/lot)
        # Equity=3,000,000 CNY, risk_pct=1.0 => budget=30,000 => pilot=5 lots
        fut_short = calculate_position_plan(
            symbol="CU2703",
            direction="short",
            entry_price=95_000.0,
            tactical_stop_30m=96_200.0,
            structural_stop_daily=98_500.0,
            target_price=88_000.0,
            account_equity=3_000_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
        )
        camp_s = fut_short["offensive_campaign"]
        self.assertEqual(camp_s["full_pilot_qty"], 5)
        self.assertEqual(camp_s["split_pilot_qty"], 2)
        self.assertEqual(camp_s["breakeven_trigger_price"], 93_800.0)
        self.assertEqual(camp_s["pyramid_trigger_price"], 93_200.0)
        self.assertEqual(camp_s["main_wave_trigger_price"], 92_600.0)
        # Prior cushion at 93,200 = 1,800 * 5 * 5 = 45,000 CNY; strict giveback cap = 45,000 * 0.65 = 29,250 CNY
        # => q_add = floor(5 * 29250 / 15750) = 9 lots, post_add_total = 14 lots,
        #    blended avg = (95000*5 + 93200*9)/14 = 93842.857142... -> floor 6dp = 93842.857142
        self.assertEqual(camp_s["added_giveback_cap_amount"], 29_250.0)
        self.assertLessEqual(camp_s["added_leg_giveback_amount"], 29_250.0)
        self.assertEqual(camp_s["max_zero_risk_add_qty"], 9)
        self.assertEqual(camp_s["post_add_total_qty"], 14)
        self.assertEqual(camp_s["post_add_breakeven_stop"], 93_842.857142)
        self.assertEqual(camp_s["initial_main_wave_stop"], 93_842.857142)
        self.assertLessEqual(camp_s["initial_main_wave_stop"], camp_s["post_add_breakeven_stop"])
        self.assertGreater(camp_s["initial_main_wave_stop"], camp_s["main_wave_trigger_price"])
        self.assertEqual(camp_s["stages"][2]["principal_risk_amount"], 0.0)

        # 2) Margin/Cash cap binds before giveback cap on cash equity:
        # Equity=25,000, risk_pct=4.0 ($1,000), entry=100, stop=90 => pilot=100 shares ($10,000 cash)
        # At add_price=115 (+1.5R): giveback allows 200 shares, but mark-to-add (100 + q_add)*115 <= 25,000 => q_add <= 117!
        margin_capped = calculate_position_plan(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=90.0,
            account_equity=25_000.0,
            account_currency="USD",
            risk_budget_pct=4.0,
        )
        camp_mc = margin_capped["offensive_campaign"]
        self.assertEqual(camp_mc["full_pilot_qty"], 100)
        self.assertEqual(camp_mc["max_zero_risk_add_qty"], 117)
        self.assertEqual(camp_mc["post_add_total_qty"], 217)
        self.assertLessEqual(camp_mc["post_add_total_qty"] * 115.0, 25_000.0)

        # 3) A-share 100-share lot quantization on pyramid add
        ashare = calculate_position_plan(
            symbol="600519",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=94.0,
            account_equity=1_000_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
        )
        self.assertEqual(ashare["offensive_campaign"]["max_zero_risk_add_qty"] % 100, 0)
        self.assertEqual(ashare["offensive_campaign"]["post_add_total_qty"] % 100, 0)

        # 4) Extreme short stop where entry - 1.5*dist <= 0 safely clamps milestone prices to None
        extreme_short = calculate_position_plan(
            symbol="CL=F",
            direction="short",
            entry_price=10.0,
            tactical_stop_30m=18.0,  # dist=8 => +1R=2.0, +1.5R=-2.0 (None), +2R=-6.0 (None)
            account_equity=1_000_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
        )
        camp_ext = extreme_short["offensive_campaign"]
        self.assertEqual(camp_ext["breakeven_trigger_price"], 2.0)
        self.assertIsNone(camp_ext["pyramid_trigger_price"])
        self.assertIsNone(camp_ext["main_wave_trigger_price"])
        self.assertEqual(camp_ext["max_zero_risk_add_qty"], 0)
        self.assertEqual(camp_ext["post_add_breakeven_stop"], 10.0)

    def test_l3_expectation_gap_crowded_warning_and_archive_integration(self):
        aligned_long = {
            "alignment_status": "aligned_trigger",
            "daily_bias": "long",
            "intraday_action": "entry_long",
        }
        wait_30m = {
            "alignment_status": "daily_ok_wait_30m",
            "daily_bias": "long",
            "intraday_action": "wait_structure",
        }
        crowded_state = {
            "direction_bias": "long",
            "expectation_gap": {
                "crowding_assessment": "crowded",
                "priced_in_consensus": "一致预期 2027 EPS +25%",
                "our_variant_view": "电网变压器交付排期超预期带动提价",
                "leading_kpis": [
                    {"name": "北美高压变压器交期周度指数", "falsification_threshold": "连续2周回落"},
                    {"name": "积压订单增速", "falsification_threshold": "YoY < 10%"},
                ],
            },
        }

        # 1) Crowded + unconfirmed 30m -> watchlist_wait_30m with explicit crowded expectation-gap reason & warning
        g_wait = evaluate_four_quadrant_gate(
            direction="long",
            asset_class="us_stock_or_etf",
            has_l1_l2_evidence=True,
            daily_alignment=wait_30m,
            rr_ratio=3.5,
            total_quantity=100,
            research_direction="long",
            archive_state=crowded_state,
        )
        self.assertEqual(g_wait["decision"], "watchlist_wait_30m")
        self.assertFalse(g_wait["actionable"])
        self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, g_wait["warnings"])
        self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, g_wait["reasons"])
        self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, g_wait["reason"])
        self.assertEqual(g_wait["expectation_gap_summary"]["crowding_assessment"], "crowded")
        self.assertEqual(g_wait["expectation_gap_summary"]["leading_kpis_count"], 2)

        # 2) Crowded + confirmed 30m aligned_trigger -> actionable_long WITH explicit warning preserved
        g_ok = evaluate_four_quadrant_gate(
            direction="long",
            asset_class="us_stock_or_etf",
            has_l1_l2_evidence=True,
            daily_alignment=aligned_long,
            rr_ratio=3.5,
            total_quantity=100,
            research_direction="long",
            archive_state=crowded_state,
        )
        self.assertEqual(g_ok["decision"], "actionable_long")
        self.assertTrue(g_ok["actionable"])
        self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, g_ok["warnings"])

        # 3) Crowded + loose/unconfirmed intraday_signal_meta even if caller passed aligned_trigger -> blocked!
        loose_align = {
            **aligned_long,
            "intraday_signal_meta": {"signal_type": "candidate_buy", "direction": "long", "confirmed": False},
        }
        g_loose = evaluate_four_quadrant_gate(
            direction="long",
            asset_class="us_stock_or_etf",
            has_l1_l2_evidence=True,
            daily_alignment=loose_align,
            rr_ratio=3.5,
            total_quantity=100,
            research_direction="long",
            archive_state=crowded_state,
        )
        self.assertEqual(g_loose["decision"], "watchlist_wait_30m")
        self.assertFalse(g_loose["actionable"])
        self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, g_loose["reasons"])

        # 4) End-to-end build_trade_plan / CLI loads expectation_gap from archive state.json
        with tempfile.TemporaryDirectory() as tmpdir:
            ra.init_archive("PWR", name="Quanta Services", research_root=tmpdir)
            ra.update_state(
                "PWR",
                {
                    "direction_bias": "long",
                    "expectation_gap": crowded_state["expectation_gap"],
                },
                research_root=tmpdir,
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tp.main([
                    "--symbol", "PWR",
                    "--direction", "long",
                    "--entry", "300",
                    "--stop", "290",
                    "--target", "335",
                    "--equity", "500000",
                    "--account-currency", "USD",
                    "--has-l1-l2",
                    "--alignment-status", "daily_ok_wait_30m",
                    "--research-root", tmpdir,
                ])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["expectation_gap_summary"]["crowding_assessment"], "crowded")
            self.assertEqual(out["expectation_gap_summary"]["leading_kpis_count"], 2)
            self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, out["four_quadrant_gate"]["warnings"])
            self.assertIn(CROWDED_EXPECTATION_GAP_WARNING, out["four_quadrant_gate"]["reasons"])

            # Absent expectation_gap defaults cleanly to "unknown" with zero warnings
            parser = tp.build_parser()
            args_plain = parser.parse_args([
                "--symbol", "NVDA", "--direction", "long",
                "--entry", "100", "--stop", "95", "--target", "120",
                "--has-l1-l2", "--alignment-status", "aligned_trigger",
            ])
            out_plain = build_trade_plan(args_plain)
            self.assertEqual(out_plain["expectation_gap_summary"]["crowding_assessment"], "unknown")
            self.assertEqual(out_plain["four_quadrant_gate"]["warnings"], [])


class TestSentinelDualRegimeAndCampaignStatus(unittest.TestCase):
    def test_standard_tactical_30m_mode_upgrade_prompts_and_stop_breach(self):
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
        state = _make_armed_state("long", stop_threshold=92.0)

        # Entry=100, tactical_stop_30m=95 (1R dist = 5.0), structural_stop_daily=92
        pos = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            structural_stop_daily=92.0,
        )

        # 1) At +1.1R (price=105.5): emits suggested_breakeven_stop, NOT yet suggested_regime_handover
        snap_11r = _make_campaign_snapshot(
            symbol="PWR",
            price=105.5,
            daily_zd=96.0,
            daily_zg=102.0,
            intra_zd=102.0,
            intra_zg=104.5,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 105.0, "high": 105.8, "low": 104.8, "close": 105.5}
            ],
        )
        res_11r = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_11r, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_11r["status"], ds.STATUS_GREEN)
        cs_11r = res_11r["campaign_status"]
        self.assertEqual(cs_11r["active_stop_regime"], "tactical_30m")
        self.assertEqual(cs_11r["unit_1r_price_dist"], 5.0)
        self.assertAlmostEqual(cs_11r["unrealized_r"], 1.1, places=4)
        self.assertFalse(cs_11r["is_breakeven_locked"])
        self.assertIsNotNone(cs_11r["suggested_breakeven_stop"])
        self.assertEqual(cs_11r["suggested_breakeven_stop"]["recommended_stop"], 100.0)
        self.assertEqual(cs_11r["suggested_breakeven_stop"]["stop_field"], "tactical_stop_30m")
        self.assertIsNone(cs_11r["suggested_regime_handover"])

        # 2) At +2.2R (price=111.0, daily_zd=104.0 >= entry=100.0): emits suggested_regime_handover
        snap_22r = _make_campaign_snapshot(
            symbol="PWR",
            price=111.0,
            daily_zd=104.0,
            daily_zg=109.0,
            intra_zd=107.0,
            intra_zg=110.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 110.2, "high": 111.5, "low": 110.0, "close": 111.0}
            ],
        )
        res_22r = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_22r, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_22r["status"], ds.STATUS_GREEN)
        cs_22r = res_22r["campaign_status"]
        self.assertAlmostEqual(cs_22r["unrealized_r"], 2.2, places=4)
        self.assertIsNotNone(cs_22r["suggested_regime_handover"])
        self.assertEqual(cs_22r["suggested_regime_handover"]["target_regime"], "daily_structure")
        self.assertEqual(cs_22r["suggested_regime_handover"]["recommended_daily_stop"], 104.0)

        # 3) In standard tactical_30m mode, breaking 30m stop (94.5 <= 95.0) STILL immediately fires RED_EXIT!
        snap_break_30m = _make_campaign_snapshot(
            symbol="PWR",
            price=94.5,
            daily_zd=92.0,
            daily_zg=100.0,
            intra_zd=95.0,
            intra_zg=98.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 95.8, "high": 96.0, "low": 94.2, "close": 94.5}
            ],
        )
        res_break = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_break_30m, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_break["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res_break["red_alerts"]})

    def test_daily_structure_main_wave_mode_anti_shakeout_pullback_stays_green(self):
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
        state = _make_armed_state("long", stop_threshold=100.0)

        # Main-wave position: entry=100, old tactical_stop_30m=108, structural_stop_daily=102 (>= entry!)
        pos_mw = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=102.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )

        # Price pulls back to 106.5 (crossing below old tactical_stop_30m=108.0, wick low=105.8)
        # AND 30min shows a counter-stroke / top-divergence sell signal, while daily trend & KPIs are clean!
        snap_pullback = _make_campaign_snapshot(
            symbol="PWR",
            price=106.5,
            daily_zd=104.0,
            daily_zg=109.0,
            intra_zd=107.0,
            intra_zg=110.0,
            daily_bias="long",
            intraday_bias="short",
            intraday_action="entry_short",
            intraday_signals=["30min一卖确认(顶背驰回调)"],
            intraday_signal_meta={
                "signal_type": "first_sell",
                "direction": "short",
                "confirmed": True,
                "momentum_confirmed": True,
                "is_exhaustion": False,
            },
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 108.2, "high": 108.5, "low": 105.8, "close": 106.5}
            ],
            recent_daily_bars=[
                {"bar": "2026-11-24", "open": 109.0, "high": 110.0, "low": 105.8, "close": 106.5}
            ],
        )
        res_mw = ds.evaluate_holding_health(
            "PWR", "long", position=pos_mw, archive_state=state, snapshot=snap_pullback, ref_date=ref, now_utc=now_utc
        )
        # Stays GREEN_HOLD: NO 30m stop RED, NO intraday_30m_weakening YELLOW!
        self.assertEqual(res_mw["status"], ds.STATUS_GREEN)
        self.assertEqual(res_mw["red_alerts"], [])
        self.assertEqual(res_mw["yellow_alerts"], [])

        cs = res_mw["campaign_status"]
        self.assertEqual(cs["active_stop_regime"], "daily_structure")
        self.assertEqual(cs["lifecycle_stage"], "main_wave")
        self.assertEqual(cs["effective_stop_price"], 102.0)
        self.assertTrue(cs["is_breakeven_locked"])
        self.assertTrue(cs["tactical_30m_reference_crossed"])
        self.assertIsNotNone(cs["main_wave_pullback_watch"])
        self.assertTrue(cs["main_wave_pullback_watch"]["active"])
        self.assertIn("主升期 30min 正常回踩观察", cs["main_wave_pullback_watch"]["message"])

        # Main-wave trailing stop anchors to Daily Zhongshu ZD (104.0 > 102.0)
        self.assertIsNotNone(res_mw["suggested_trail_stop"])
        self.assertEqual(res_mw["suggested_trail_stop"]["stop_field"], "structural_stop_daily")
        self.assertEqual(res_mw["suggested_trail_stop"]["anchor"], "daily_zhongshu_zd")
        self.assertEqual(res_mw["suggested_trail_stop"]["suggested_stop"], 104.0)

    def test_daily_structure_mode_hair_trigger_exit_on_real_reversals_and_resonance(self):
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
        pos_mw = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=102.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        base_state = _make_armed_state("long", stop_threshold=98.0)

        # 1) Close breach of structural_stop_daily (101.5 <= 102.0) -> RED_EXIT
        snap_close_breach = _make_campaign_snapshot(
            symbol="PWR",
            price=101.5,
            daily_zd=102.0,
            daily_zg=108.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 103.0, "high": 103.2, "low": 101.2, "close": 101.5}
            ],
        )
        res_cb = ds.evaluate_holding_health(
            "PWR", "long", position=pos_mw, archive_state=base_state, snapshot=snap_close_breach, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_cb["status"], ds.STATUS_RED)
        self.assertIn("structural_stop_daily_breached", {a["code"] for a in res_cb["red_alerts"]})

        # 2) Intrabar wick breach of structural_stop_daily (low=101.8 <= 102.0, close recovered to 105.0) -> RED_EXIT
        snap_wick_breach = _make_campaign_snapshot(
            symbol="PWR",
            price=105.0,
            daily_zd=102.0,
            daily_zg=108.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 104.0, "high": 105.5, "low": 101.8, "close": 105.0}
            ],
        )
        res_wb = ds.evaluate_holding_health(
            "PWR", "long", position=pos_mw, archive_state=base_state, snapshot=snap_wick_breach, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_wb["status"], ds.STATUS_RED)
        self.assertIn("structural_stop_daily_intraday_breached", {a["code"] for a in res_wb["red_alerts"]})

        # 3) Layer-3 red KPI trigger fires -> RED_EXIT even when price is high above stops!
        kpi_red_state = _make_armed_state(
            "long",
            stop_threshold=98.0,
            extra_triggers=[
                {
                    "id": "t_kpi_falsified",
                    "kind": "fundamental",
                    "severity": "red",
                    "metric": "forward_pe",
                    "operator": ">=",
                    "threshold": 40.0,
                    "action": "估值泡沫叠加指引下修果断撤离",
                }
            ],
        )
        bundle_kpi = {
            "symbol": "PWR",
            "fundamentals_and_supply": {
                "yfinance_consensus": {
                    "status": "ok",
                    "valuation_and_positioning": {"forward_pe": 45.0},
                }
            },
        }
        snap_high = _make_campaign_snapshot(
            symbol="PWR",
            price=112.0,
            daily_zd=104.0,
            daily_zg=109.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 111.5, "high": 112.5, "low": 111.0, "close": 112.0}
            ],
        )
        res_kpi = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_mw,
            archive_state=kpi_red_state,
            snapshot=snap_high,
            research_bundle=bundle_kpi,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_kpi["status"], ds.STATUS_RED)
        self.assertIn("trigger_red_t_kpi_falsified", {a["code"] for a in res_kpi["red_alerts"]})

        # 4) Daily reversal -> RED_EXIT
        snap_daily_rev = _make_campaign_snapshot(
            symbol="PWR",
            price=109.0,
            daily_zd=104.0,
            daily_zg=108.0,
            daily_bias="short",
            daily_signals=["日线一卖确认(顶背驰反转)"],
            daily_signal_meta={
                "signal_type": "first_sell",
                "direction": "short",
                "confirmed": True,
                "momentum_confirmed": True,
                "is_exhaustion": False,
            },
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 109.5, "high": 109.8, "low": 108.8, "close": 109.0}
            ],
        )
        res_drev = ds.evaluate_holding_health(
            "PWR", "long", position=pos_mw, archive_state=base_state, snapshot=snap_daily_rev, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_drev["status"], ds.STATUS_RED)
        self.assertIn("daily_direction_reversed", {a["code"] for a in res_drev["red_alerts"]})

        # 5) Daily loosening + 30m weakening resonance -> keeps BOTH daily_structure_loosening AND intraday_30m_weakening!
        snap_resonance = _make_campaign_snapshot(
            symbol="PWR",
            price=109.0,
            daily_zd=104.0,
            daily_zg=108.0,
            daily_bias="long",
            intraday_bias="short",
            intraday_action="entry_short",
            daily_signals=["日线潜在顶背驰预警(未确认)"],
            daily_signal_meta={
                "signal_type": "candidate_sell",
                "direction": "short",
                "confirmed": False,
                "momentum_confirmed": False,
                "is_exhaustion": True,
            },
            intraday_signals=["30min一卖确认"],
            intraday_signal_meta={
                "signal_type": "first_sell",
                "direction": "short",
                "confirmed": True,
                "momentum_confirmed": True,
                "is_exhaustion": False,
            },
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 109.5, "high": 109.8, "low": 108.8, "close": 109.0}
            ],
        )
        res_res = ds.evaluate_holding_health(
            "PWR", "long", position=pos_mw, archive_state=base_state, snapshot=snap_resonance, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_res["status"], ds.STATUS_YELLOW)
        y_codes = {a["code"] for a in res_res["yellow_alerts"]}
        self.assertIn("daily_structure_loosening", y_codes)
        self.assertIn("intraday_30m_weakening", y_codes)
        self.assertIsNone(res_res["campaign_status"]["main_wave_pullback_watch"])

    def test_fail_closed_invalid_daily_regime_fallback_long_and_short(self):
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
        state = _make_armed_state("long", stop_threshold=90.0)

        # Long holding manually configured with active_stop_regime="daily_structure"
        # BUT structural_stop_daily=95.0 < entry_price=100.0 (not on profit/breakeven side!)
        pos_bad_long = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=98.0,
            structural_stop_daily=95.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        snap_safe = _make_campaign_snapshot(
            symbol="PWR",
            price=103.0,
            daily_zd=95.0,
            daily_zg=101.0,
            intra_zd=99.0,
            intra_zg=102.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 102.5, "high": 103.5, "low": 102.0, "close": 103.0}
            ],
        )
        res_fb = ds.evaluate_holding_health(
            "PWR", "long", position=pos_bad_long, archive_state=state, snapshot=snap_safe, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_fb["status"], ds.STATUS_YELLOW)
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res_fb["yellow_alerts"]})
        self.assertEqual(res_fb["campaign_status"]["configured_stop_regime"], "daily_structure")
        self.assertEqual(res_fb["campaign_status"]["active_stop_regime"], "tactical_30m")

        # Furthermore, because it fell back to tactical_30m, breaching tactical_stop_30m=98.0 MUST fire RED_EXIT!
        snap_breach_30m = _make_campaign_snapshot(
            symbol="PWR",
            price=97.5,
            daily_zd=95.0,
            daily_zg=101.0,
            intra_zd=98.0,
            intra_zg=101.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 98.5, "high": 98.8, "low": 97.2, "close": 97.5}
            ],
        )
        res_fb_red = ds.evaluate_holding_health(
            "PWR", "long", position=pos_bad_long, archive_state=state, snapshot=snap_breach_30m, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_fb_red["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res_fb_red["red_alerts"]})

        # Short side symmetric check: structural_stop_daily=105.0 > entry_price=100.0 -> fail-closed fallback!
        state_short = _make_armed_state("short", stop_threshold=110.0)
        pos_bad_short = _make_holding_position(
            symbol="CL=F",
            direction="short",
            asset_class="commodity",
            contract_symbol="CLZ26.NYM",
            contract_or_expression="CLZ26.NYM",
            entry_price=100.0,
            tactical_stop_30m=102.0,
            structural_stop_daily=105.0,
            target_price=85.0,
            active_stop_regime="daily_structure",
        )
        snap_short = _make_campaign_snapshot(
            symbol="CLZ26.NYM",
            asset_class="commodity",
            price=96.0,
            daily_zd=95.0,
            daily_zg=99.0,
            intra_zd=95.5,
            intra_zg=98.0,
            daily_bias="short",
            intraday_bias="short",
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 96.5, "high": 96.8, "low": 95.8, "close": 96.0}
            ],
        )
        res_fb_short = ds.evaluate_holding_health(
            "CL=F", "short", position=pos_bad_short, archive_state=state_short, snapshot=snap_short, ref_date=ref, now_utc=now_utc
        )
        self.assertEqual(res_fb_short["status"], ds.STATUS_YELLOW)
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res_fb_short["yellow_alerts"]})
        self.assertEqual(res_fb_short["campaign_status"]["active_stop_regime"], "tactical_30m")

    def test_codex_critical1_missing_ledger_daily_stop_never_borrows_archive_anchor(self):
        """CRITICAL #1: Ledger position with structural_stop_daily=None must NOT borrow archive anchor 102."""
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)

        archive_with_anchor = {
            **_make_armed_state("long", stop_threshold=98.0),
            "chanlun_anchors": {"structural_stop_daily": 102.0},
        }
        pos_missing_daily = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=None,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )

        # 1) Close 106.5 (low 105.8) breaches tactical_stop_30m=108 -> fail-closed fallback + RED_EXIT!
        snap_breach = _make_campaign_snapshot(
            symbol="PWR",
            price=106.5,
            daily_zd=103.0,
            daily_zg=109.0,
            intra_zd=106.0,
            intra_zg=109.0,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 108.2, "high": 108.5, "low": 105.8, "close": 106.5}
            ],
        )
        res = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_missing_daily,
            archive_state=archive_with_anchor,
            snapshot=snap_breach,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res["status"], ds.STATUS_RED)
        self.assertEqual(res["campaign_status"]["active_stop_regime"], "tactical_30m")
        self.assertEqual(res["campaign_status"]["configured_stop_regime"], "daily_structure")
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res["yellow_alerts"]})
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res["red_alerts"]})

        # 2) Close recovered to 109.0, but intrabar wick dipped to 105.8 <= 108.0 -> STILL RED_EXIT!
        snap_wick_only = _make_campaign_snapshot(
            symbol="PWR",
            price=109.0,
            daily_zd=103.0,
            daily_zg=109.0,
            intra_zd=106.0,
            intra_zg=109.5,
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 108.5, "high": 109.2, "low": 105.8, "close": 109.0}
            ],
        )
        res_wick = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_missing_daily,
            archive_state=archive_with_anchor,
            snapshot=snap_wick_only,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_wick["status"], ds.STATUS_RED)
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res_wick["yellow_alerts"]})
        self.assertIn("tactical_stop_30m_intraday_breached", {a["code"] for a in res_wick["red_alerts"]})

        # 3) Campaign breakeven floor with negative partial_realized_pnl=-600 (qty=200 => floor=103.0):
        #    ledger structural_stop_daily=102.0 is above entry=100.0 but BELOW campaign floor 103.0 -> fails closed!
        pos_neg_partial = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            quantity=200,
            multiplier=1.0,
            fx_to_account=1.0,
            partial_realized_pnl=-600.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=102.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        res_neg = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_neg_partial,
            archive_state=_make_armed_state("long", stop_threshold=98.0),
            snapshot=snap_breach,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_neg["status"], ds.STATUS_RED)
        self.assertEqual(res_neg["campaign_status"]["active_stop_regime"], "tactical_30m")
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res_neg["yellow_alerts"]})
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res_neg["red_alerts"]})

        # Once ledger daily stop is raised to 103.5 (>= 103.0 floor), daily_structure is valid & stays GREEN_HOLD
        pos_neg_covered = {**pos_neg_partial, "structural_stop_daily": 103.5}
        res_cov = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_neg_covered,
            archive_state=_make_armed_state("long", stop_threshold=98.0),
            snapshot=snap_breach,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_cov["status"], ds.STATUS_GREEN)
        self.assertEqual(res_cov["campaign_status"]["active_stop_regime"], "daily_structure")

    def test_codex_high1_and_high6_planner_stage3_zero_principal_risk_long_and_short(self):
        """HIGH #1 & #6: Stage 3 main-wave stop honors post-add breakeven floor, protective room & strict 65% cap."""
        # Long: entry=100, stop30m=95 (+1.5R=107.5, +2.0R=110.0), daily_stop=90 (< blended floor)
        plan_l = calculate_position_plan(
            symbol="NVDA",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            structural_stop_daily=90.0,
            target_price=125.0,
            account_equity=100_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
        )
        cl = plan_l["offensive_campaign"]
        self.assertGreaterEqual(cl["initial_main_wave_stop"], cl["post_add_breakeven_stop"])
        self.assertLess(cl["initial_main_wave_stop"], cl["main_wave_trigger_price"])
        self.assertEqual(cl["stages"][2]["principal_risk_amount"], 0.0)
        # Net PnL of total position (pilot + add) if stopped at initial_main_wave_stop >= 0.0
        pnl_l = (
            (cl["initial_main_wave_stop"] - 100.0) * cl["full_pilot_qty"]
            + (cl["initial_main_wave_stop"] - cl["pyramid_trigger_price"]) * cl["max_zero_risk_add_qty"]
        )
        self.assertGreaterEqual(pnl_l, 0.0)

        # Long with zero Stage-2 add (cash margin fully utilized by pilot: 200 @ 100 = $20,000 == equity)
        # => stage3_breakeven_floor cleanly equals entry_price=100.0, still strictly behind +2R=110.0!
        plan_l_noadd = calculate_position_plan(
            symbol="NVDA",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            structural_stop_daily=90.0,
            target_price=125.0,
            account_equity=20_000.0,
            account_currency="USD",
            risk_budget_pct=5.0,
        )
        cl_na = plan_l_noadd["offensive_campaign"]
        self.assertEqual(cl_na["max_zero_risk_add_qty"], 0)
        self.assertEqual(cl_na["post_add_total_qty"], 200)
        self.assertEqual(cl_na["post_add_breakeven_stop"], 100.0)
        self.assertEqual(cl_na["initial_main_wave_stop"], 100.0)
        self.assertLess(cl_na["initial_main_wave_stop"], cl_na["main_wave_trigger_price"])
        self.assertEqual(cl_na["stages"][2]["principal_risk_amount"], 0.0)

        # Short: entry=100, stop30m=105 (+1.5R=92.5, +2.0R=90.0), daily_stop=110 (> blended floor)
        plan_s = calculate_position_plan(
            symbol="NVDA",
            direction="short",
            entry_price=100.0,
            tactical_stop_30m=105.0,
            structural_stop_daily=110.0,
            target_price=75.0,
            account_equity=100_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
        )
        cs = plan_s["offensive_campaign"]
        self.assertEqual(cs["prior_profit_cushion_at_pyramid"], 1500.0)
        self.assertEqual(cs["added_giveback_cap_amount"], 975.0)
        self.assertLessEqual(cs["added_leg_giveback_amount"], 975.0)
        self.assertEqual(cs["max_zero_risk_add_qty"], 371)
        self.assertEqual(cs["post_add_total_qty"], 571)
        self.assertEqual(cs["post_add_breakeven_stop"], 95.12697)
        self.assertEqual(cs["initial_main_wave_stop"], 95.12697)
        self.assertLessEqual(cs["initial_main_wave_stop"], cs["post_add_breakeven_stop"])
        self.assertGreater(cs["initial_main_wave_stop"], cs["main_wave_trigger_price"])
        self.assertEqual(cs["stages"][2]["principal_risk_amount"], 0.0)
        pnl_s = (
            (100.0 - cs["initial_main_wave_stop"]) * cs["full_pilot_qty"]
            + (cs["pyramid_trigger_price"] - cs["initial_main_wave_stop"]) * cs["max_zero_risk_add_qty"]
        )
        self.assertGreaterEqual(pnl_s, 0.0)
        # Verify conservative cost-basis notional respects equity/margin cap for short
        cost_notional_s = 100.0 * cs["full_pilot_qty"] + cs["pyramid_trigger_price"] * cs["max_zero_risk_add_qty"]
        self.assertLessEqual(cost_notional_s, 100_000.0)

    def test_codex_high3_unverified_trigger_or_degraded_data_blocks_handover_and_denoising(self):
        """HIGH #3: Unverified policy trigger or degraded/unclosed data blocks BOTH handover and 30m de-noising."""
        ref = date(2026, 11, 24)
        now_utc = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)

        # +2.6R holding (entry=100, initial_stop_30m=95 => 1R=5, price=113.0 => +2.6R) with 30m sell signal
        snap_26r_sell = _make_campaign_snapshot(
            symbol="PWR",
            price=113.0,
            daily_zd=106.0,
            daily_zg=111.0,
            intra_zd=110.0,
            intra_zg=114.0,
            daily_bias="long",
            intraday_bias="short",
            intraday_action="entry_short",
            intraday_signals=["30min一卖确认(顶背驰回调)"],
            intraday_signal_meta={
                "signal_type": "first_sell",
                "direction": "short",
                "confirmed": True,
                "momentum_confirmed": True,
                "is_exhaustion": False,
            },
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 113.5, "high": 113.8, "low": 112.5, "close": 113.0}
            ],
        )

        state_unverified_policy = _make_armed_state(
            "long",
            stop_threshold=98.0,
            extra_triggers=[
                {
                    "id": "t_policy_unverified",
                    "kind": "policy",
                    "metric": "grid_tariff_policy_change",
                    "operator": "==",
                    "threshold": True,
                    "action": "政策补贴退坡立即撤离",
                }
            ],
        )

        # 1a) Tactical 30m mode + unverified policy trigger -> MUST NOT emit suggested_regime_handover
        pos_tact = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=105.0,
            structural_stop_daily=102.0,
            active_stop_regime="tactical_30m",
        )
        res_tact = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_tact,
            archive_state=state_unverified_policy,
            snapshot=snap_26r_sell,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_tact["status"], ds.STATUS_YELLOW)
        self.assertAlmostEqual(res_tact["campaign_status"]["unrealized_r"], 2.6, places=4)
        self.assertIn("unevaluated_triggers", {a["code"] for a in res_tact["yellow_alerts"]})
        self.assertIsNone(res_tact["campaign_status"]["suggested_regime_handover"])

        # 1b) Daily structure mode + unverified policy trigger -> MUST keep intraday_30m_weakening & NOT emit pullback watch
        pos_daily = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=114.0,
            structural_stop_daily=104.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        res_daily = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_daily,
            archive_state=state_unverified_policy,
            snapshot=snap_26r_sell,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_daily["status"], ds.STATUS_YELLOW)
        y_codes = {a["code"] for a in res_daily["yellow_alerts"]}
        self.assertIn("unevaluated_triggers", y_codes)
        self.assertIn("intraday_30m_weakening", y_codes)
        self.assertIsNone(res_daily["campaign_status"]["main_wave_pullback_watch"])

        # 2) Degraded / unclosed 30m bar (bar_is_closed=False) also blocks BOTH handover and 30m de-noising!
        snap_unclosed = _make_campaign_snapshot(
            symbol="PWR",
            price=113.0,
            daily_zd=106.0,
            daily_zg=111.0,
            intra_zd=110.0,
            intra_zg=114.0,
            daily_bias="long",
            intraday_bias="short",
            intraday_action="entry_short",
            intraday_signals=["30min一卖确认(顶背驰回调)"],
            intraday_signal_meta={
                "signal_type": "first_sell",
                "direction": "short",
                "confirmed": True,
                "momentum_confirmed": True,
                "is_exhaustion": False,
            },
            recent_intra_bars=[
                {"bar": "2026-11-24T20:30:00+00:00", "open": 113.5, "high": 113.8, "low": 112.5, "close": 113.0}
            ],
        )
        snap_unclosed["intraday_technicals"]["bar_is_closed"] = False
        clean_state = _make_armed_state("long", stop_threshold=98.0)

        res_tact_unc = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_tact,
            archive_state=clean_state,
            snapshot=snap_unclosed,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_tact_unc["status"], ds.STATUS_YELLOW)
        self.assertIsNone(res_tact_unc["campaign_status"]["suggested_regime_handover"])

        res_daily_unc = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_daily,
            archive_state=clean_state,
            snapshot=snap_unclosed,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_daily_unc["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_weakening", {a["code"] for a in res_daily_unc["yellow_alerts"]})
        self.assertIsNone(res_daily_unc["campaign_status"]["main_wave_pullback_watch"])

    def test_codex_high5_return_to_tactical_30m_respects_tactical_stop_active_since_utc(self):
        """HIGH #5: Returning to unchanged tactical_stop_30m must NOT replay pre-switch daily_structure wicks."""
        ref = date(2026, 11, 26)
        now_utc = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
        state = _make_armed_state("long", stop_threshold=98.0)

        pos_returned = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=102.0,
            active_stop_regime="tactical_30m",
            lifecycle_stage="breakeven_locked",
            opened_at_utc="2026-11-20T15:00:00+00:00",
            stop_updated_at_utc="2026-11-24T15:00:00+00:00",
            structural_stop_updated_at_utc="2026-11-24T15:00:00+00:00",
            tactical_stop_active_since_utc="2026-11-26 21:00:00 UTC",
        )

        # 1) 20:30Z bar (before 21:00Z return) had low=106 <= 108; 21:30Z bar is clean (low=109, close=111) -> GREEN_HOLD!
        snap_post_clean = _make_campaign_snapshot(
            symbol="PWR",
            price=111.0,
            daily_zd=104.0,
            daily_zg=109.0,
            intra_zd=108.5,
            intra_zg=110.5,
            daily_bar="2026-11-26",
            intraday_bar="2026-11-26T21:30:00+00:00",
            recent_intra_bars=[
                {"bar": "2026-11-26T20:30:00+00:00", "open": 109.5, "high": 110.5, "low": 106.0, "close": 110.0},
                {"bar": "2026-11-26T21:00:00+00:00", "open": 110.0, "high": 111.2, "low": 109.2, "close": 110.8},
                {"bar": "2026-11-26T21:30:00+00:00", "open": 110.8, "high": 111.5, "low": 109.0, "close": 111.0},
            ],
        )
        res_clean = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_returned,
            archive_state=state,
            snapshot=snap_post_clean,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_clean["status"], ds.STATUS_GREEN)
        self.assertEqual(res_clean["red_alerts"], [])
        self.assertEqual(res_clean["yellow_alerts"], [])

        # 2) A bar AFTER 21:00Z dipping to low=107.0 <= 108.0 MUST fire RED_EXIT!
        snap_post_breach = _make_campaign_snapshot(
            symbol="PWR",
            price=111.0,
            daily_zd=104.0,
            daily_zg=109.0,
            intra_zd=108.5,
            intra_zg=110.5,
            daily_bar="2026-11-26",
            intraday_bar="2026-11-26T21:30:00+00:00",
            recent_intra_bars=[
                {"bar": "2026-11-26T20:30:00+00:00", "open": 109.5, "high": 110.5, "low": 109.0, "close": 110.0},
                {"bar": "2026-11-26T21:00:00+00:00", "open": 110.0, "high": 111.2, "low": 109.2, "close": 110.8},
                {"bar": "2026-11-26T21:30:00+00:00", "open": 110.8, "high": 111.5, "low": 107.0, "close": 111.0},
            ],
        )
        res_post_breach = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_returned,
            archive_state=state,
            snapshot=snap_post_breach,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_post_breach["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_intraday_breached", {a["code"] for a in res_post_breach["red_alerts"]})

        # 3) structural_stop_daily cutoff remains independent: a 20:30Z wick breaking daily stop 102.0 STILL fires RED_EXIT!
        snap_daily_breach = _make_campaign_snapshot(
            symbol="PWR",
            price=111.0,
            daily_zd=104.0,
            daily_zg=109.0,
            intra_zd=108.5,
            intra_zg=110.5,
            daily_bar="2026-11-26",
            intraday_bar="2026-11-26T21:30:00+00:00",
            recent_intra_bars=[
                {"bar": "2026-11-26T20:30:00+00:00", "open": 109.5, "high": 110.5, "low": 101.5, "close": 110.0},
                {"bar": "2026-11-26T21:00:00+00:00", "open": 110.0, "high": 111.2, "low": 109.2, "close": 110.8},
                {"bar": "2026-11-26T21:30:00+00:00", "open": 110.8, "high": 111.5, "low": 109.0, "close": 111.0},
            ],
        )
        res_daily_breach = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_returned,
            archive_state=state,
            snapshot=snap_daily_breach,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_daily_breach["status"], ds.STATUS_RED)
        self.assertIn("structural_stop_daily_intraday_breached", {a["code"] for a in res_daily_breach["red_alerts"]})

    def test_round2_archive_anchor_never_drives_ledger_hard_stop_and_6dec_daily_trail(self) -> None:
        """Round-2 HIGH #1 & MEDIUM #2: archive anchor cannot trigger hard stop on ledger positions, and daily trail keeps 6 decimals."""
        ref = date(2026, 11, 26)
        now_utc = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)

        # 1) Ledger daily_structure missing structural_stop_daily, tactical=108, close=108.5, low=108.3,
        #    archive anchor=109 (> close): must stay YELLOW_LOOSENING (fallback), NOT RED_EXIT from archive anchor!
        pos_no_daily = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=None,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        state_anchor_109 = _make_armed_state("long", stop_threshold=98.0)
        state_anchor_109["chanlun_anchors"] = {"structural_stop_daily": 109.0}
        snap_above_108 = _make_campaign_snapshot(
            symbol="PWR",
            price=108.5,
            daily_zd=103.0,
            daily_zg=107.0,
            intra_zd=106.5,
            intra_zg=108.0,
            recent_intra_bars=[
                {"bar": "2026-11-26T20:30:00+00:00", "open": 108.6, "high": 109.2, "low": 108.3, "close": 108.5},
            ],
        )
        res = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_no_daily,
            archive_state=state_anchor_109,
            snapshot=snap_above_108,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        self.assertEqual(res["red_alerts"], [])
        self.assertIn("invalid_daily_regime_fallback", {a["code"] for a in res["yellow_alerts"]})

        # 2) Daily trail suggestion preserves 6 decimal places (e.g., 103.123456)
        pos_daily = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=108.0,
            structural_stop_daily=102.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        snap_precise = _make_campaign_snapshot(
            symbol="PWR",
            price=103.123449,
            daily_zd=103.123448,
            daily_zg=109.0,
            intra_zd=108.5,
            intra_zg=111.0,
        )
        res_precise = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_daily,
            archive_state=_make_armed_state("long", stop_threshold=98.0),
            snapshot=snap_precise,
            ref_date=ref,
            now_utc=now_utc,
        )
        trail = res_precise["suggested_trail_stop"]
        self.assertIsNotNone(trail)
        self.assertEqual(trail["stop_field"], "structural_stop_daily")
        self.assertEqual(trail["suggested_stop"], 103.123448)
        self.assertEqual(res_precise["last_price"], 103.123449)
        self.assertGreater(res_precise["last_price"], trail["suggested_stop"])

    def test_round2_planner_non_actionable_stage3_unreachable_or_zero_room(self) -> None:
        """Round-2 MEDIUM #1: Stage 3 must be non-actionable when +2R trigger is unreachable or has no protective room."""
        # Short entry=100, tactical_stop=160 -> +2R = -20 (unreachable -> None)
        plan_short_wide = tp.calculate_position_plan(
            symbol="NVDA",
            direction="short",
            entry_price=100.0,
            tactical_stop_30m=160.0,
            structural_stop_daily=170.0,
            target_price=20.0,
            account_equity=100_000.0,
            risk_budget_pct=1.0,
            allow_equity_margin=True,
        )
        oc_wide = plan_short_wide["offensive_campaign"]
        self.assertIsNone(oc_wide["main_wave_trigger_price"])
        self.assertIsNone(oc_wide["initial_main_wave_stop"])
        s3_wide = oc_wide["stages"][2]
        self.assertFalse(s3_wide["actionable"])
        self.assertIsNone(s3_wide["trigger_price"])
        self.assertIsNone(s3_wide["stop_price"])
        self.assertEqual(s3_wide["recommended_qty"], 0)

        # Ultra-tight stop where 6-decimal rounding collapses room between trigger and floor
        plan_ultra_tight = tp.calculate_position_plan(
            symbol="NVDA",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=99.9999998,
            structural_stop_daily=99.999999,
            target_price=101.0,
            account_equity=100_000.0,
            risk_budget_pct=1.0,
        )
        oc_tight = plan_ultra_tight["offensive_campaign"]
        s1_tight, s2_tight, s3_tight = oc_tight["stages"]
        self.assertFalse(s1_tight["actionable"])
        self.assertEqual(s1_tight["recommended_qty"], 0)
        self.assertFalse(s2_tight["actionable"])
        self.assertEqual(s2_tight["recommended_qty"], 0)
        self.assertFalse(s3_tight["actionable"])
        self.assertIsNone(s3_tight["stop_price"])
        self.assertEqual(s3_tight["recommended_qty"], 0)
        self.assertEqual(plan_ultra_tight["total_quantity"], 0)
        self.assertEqual(plan_ultra_tight["sizing_binding_constraint"], "stop_precision_collapsed")

    def test_round4_main_gate_blocks_6dec_collapsed_entry_stop_and_sentinel_blocks_collapsed_trail(self) -> None:
        """Round-4 MEDIUM #1 & #2: main trade-plan gate rejects 6-dec collapsed stop; sentinel suppresses collapsed trail."""
        # 1) build_trade_plan with entry=0.5, stop=0.4999996 -> both serialize to 0.5 -> gate MUST reject!
        parser = tp.build_parser()
        args = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "0.5",
            "--stop", "0.4999996",
            "--target", "2.0",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out = tp.build_trade_plan(args)
        self.assertEqual(out["position_plan"]["entry_price"], 0.5)
        self.assertEqual(out["position_plan"]["tactical_stop_30m"], 0.5)
        self.assertEqual(out["position_plan"]["total_quantity"], 0)
        self.assertEqual(out["position_plan"]["sizing_binding_constraint"], "stop_precision_collapsed")
        self.assertFalse(out["four_quadrant_gate"]["actionable"])
        self.assertEqual(out["four_quadrant_gate"]["decision"], "rejected_sizing_or_capital")
        for st in out["position_plan"]["offensive_campaign"]["stages"]:
            self.assertFalse(st["actionable"])
            self.assertEqual(st["recommended_qty"], 0)

        # 2) Sentinel: last_price=103.1234494 and daily_zd=103.1234488 both serialize to 103.123449 -> NO trail!
        ref = date(2026, 11, 26)
        now_utc = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
        pos_daily = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=102.0,
            structural_stop_daily=102.0,
            active_stop_regime="daily_structure",
            lifecycle_stage="main_wave",
        )
        snap_collapsed_daily = _make_campaign_snapshot(
            symbol="PWR",
            price=103.1234494,
            daily_zd=103.1234488,
            daily_zg=109.0,
            intra_zd=103.1234488,
            intra_zg=111.0,
        )
        res_daily = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_daily,
            archive_state=_make_armed_state("long", stop_threshold=98.0),
            snapshot=snap_collapsed_daily,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_daily["last_price"], 103.123449)
        self.assertIsNone(res_daily["suggested_trail_stop"])

        # Same check for tactical_30m trail suggestion
        pos_tact = _make_holding_position(
            symbol="PWR",
            direction="long",
            entry_price=100.0,
            initial_entry_price=100.0,
            initial_stop_30m=95.0,
            tactical_stop_30m=102.0,
            structural_stop_daily=98.0,
            active_stop_regime="tactical_30m",
            lifecycle_stage="breakeven_locked",
        )
        res_tact = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_tact,
            archive_state=_make_armed_state("long", stop_threshold=98.0),
            snapshot=snap_collapsed_daily,
            ref_date=ref,
            now_utc=now_utc,
        )
        self.assertEqual(res_tact["last_price"], 103.123449)
        self.assertIsNone(res_tact["suggested_trail_stop"])

    def test_round5_trade_plan_blocks_zero_rounded_short_target_and_uses_6dec_rr(self) -> None:
        """Round-5 MEDIUM #2 & #3: short target rounding to 0.0 must fail closed; R:R must be computed from 6-dec persisted prices."""
        parser = tp.build_parser()

        # 1) Short target=0.0000004 rounds to 0.0 -> target_price omitted (None), rr_ratio=0.0, gate rejects!
        args_zero_tgt = parser.parse_args([
            "--symbol", "CL=F",
            "--direction", "short",
            "--entry", "0.5",
            "--stop", "0.55",
            "--target", "0.0000004",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "short",
            "--curve-structure", "flat",
            "--roll-yield", "0.0",
        ])
        out_zero = tp.build_trade_plan(args_zero_tgt)
        self.assertIsNone(out_zero["position_plan"]["target_price"])
        self.assertEqual(out_zero["position_plan"]["rr_ratio"], 0.0)
        self.assertFalse(out_zero["four_quadrant_gate"]["actionable"])
        self.assertNotEqual(out_zero["four_quadrant_gate"]["decision"], "actionable_short")

        # 2) entry=0.50000051, stop=0.49999949, target=0.5000031 -> persisted 0.500001 / 0.499999 / 0.500003 -> RR == 1.0 (< 2.5) -> gate rejects!
        args_6dec_rr = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "0.50000051",
            "--stop", "0.49999949",
            "--target", "0.5000031",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out_rr = tp.build_trade_plan(args_6dec_rr)
        self.assertEqual(out_rr["position_plan"]["entry_price"], 0.500001)
        self.assertEqual(out_rr["position_plan"]["tactical_stop_30m"], 0.499999)
        self.assertEqual(out_rr["position_plan"]["target_price"], 0.500003)
        self.assertEqual(out_rr["position_plan"]["stop_distance_30m"], 0.000002)
        self.assertEqual(out_rr["position_plan"]["rr_ratio"], 1.0)
        self.assertFalse(out_rr["four_quadrant_gate"]["actionable"])

    def test_round6_tiny_valid_price_spread_still_runs_carry_gate(self) -> None:
        """Round-6 MEDIUM #2: valid 6-dec price spreads that round to 0.0% at 4 decimals must NOT skip Carry gating."""
        parser = tp.build_parser()
        args = parser.parse_args([
            "--symbol", "BTC",
            "--direction", "long",
            "--entry", "80000",
            "--stop", "79999.99",
            "--target", "80000.03",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--horizon-days", "90",
            "--funding-ann-pct", "5.0",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out = tp.build_trade_plan(args)
        self.assertEqual(out["position_plan"]["rr_ratio"], 3.0)
        carry_rr = out["expression_and_carry"]["carry_adjusted_rr_ratio"]
        self.assertIsNotNone(carry_rr)
        # Unrounded horizon carry: -5.0 * (90/365) = -1.2328767123... -> (3.75e-5 - 1.2328767123...) / 1.25e-5 = -98627.137
        self.assertAlmostEqual(carry_rr, -98627.137, places=2)
        self.assertEqual(out["position_plan"]["carry_adjusted_rr_ratio"], carry_rr)
        self.assertFalse(out["four_quadrant_gate"]["actionable"])
        self.assertEqual(out["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")

        # Direct calculate_position_plan with explicit horizon_carry_pct must also use unrounded % from 6-dec prices
        direct_plan = tp.calculate_position_plan(
            symbol="BTC",
            direction="long",
            entry_price=80000.0,
            tactical_stop_30m=79999.99,
            target_price=80000.03,
            account_equity=100_000.0,
            risk_budget_pct=1.0,
            horizon_carry_pct=-1.2329,
        )
        self.assertAlmostEqual(direct_plan["carry_adjusted_rr_ratio"], -98629.0, places=1)

    def test_round7_tiny_carry_rate_not_erased_by_4dec_rounding(self) -> None:
        """Round-7 MEDIUM #3: small annualized carry/funding/rate-diff must NOT round hor to 0.0 before Carry gating."""
        parser = tp.build_parser()
        args = parser.parse_args([
            "--symbol", "BTC",
            "--direction", "long",
            "--entry", "80000",
            "--stop", "79999.99",
            "--target", "80000.03",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--horizon-days", "90",
            "--funding-ann-pct", "0.0001",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out = tp.build_trade_plan(args)
        self.assertEqual(out["position_plan"]["rr_ratio"], 3.0)
        self.assertEqual(out["expression_and_carry"]["horizon_carry_pct"], -0.0)
        # Unrounded horizon carry = -0.0001 * 90 / 365 = -2.4657534247e-5 -> (3.75e-5 - 2.4657534247e-5) / 1.25e-5 = 1.0274
        self.assertEqual(out["expression_and_carry"]["carry_adjusted_rr_ratio"], 1.0274)
        self.assertEqual(out["position_plan"]["carry_adjusted_rr_ratio"], 1.0274)
        self.assertFalse(out["four_quadrant_gate"]["actionable"])
        self.assertEqual(out["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")

        # Also check tiny upstream extractors: crypto 8h funding, FX bp diff, commodity roll yield
        res_crypto_8h = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="crypto",
            crypto_funding={"funding_rate_8h_pct": 0.0001 / (3.0 * 365.0)},
            holding_horizon_days=90,
            expected_move_pct=0.03 / 80000.0 * 100.0,
            stop_distance_pct=0.01 / 80000.0 * 100.0,
        )
        self.assertEqual(res_crypto_8h["carry_adjusted_rr_ratio"], 1.0274)

        res_fx_bp = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="fx",
            rate_diff={"rate_diff_bp": -0.01},  # -0.0001% annualized
            holding_horizon_days=90,
            expected_move_pct=0.03 / 80000.0 * 100.0,
            stop_distance_pct=0.01 / 80000.0 * 100.0,
        )
        self.assertEqual(res_fx_bp["carry_adjusted_rr_ratio"], 1.0274)

        res_comm = tp.evaluate_expression_and_carry(
            direction="short",
            asset_class="commodity",
            term_structure={"structure": "Backwardation", "annualized_roll_yield_curve_pct": 0.0001},
            holding_horizon_days=90,
            expected_move_pct=0.03 / 80000.0 * 100.0,
            stop_distance_pct=0.01 / 80000.0 * 100.0,
        )
        self.assertEqual(res_comm["carry_adjusted_rr_ratio"], 1.0274)

    def test_round8_gate_uses_unrounded_rr_and_carry_rr(self) -> None:
        """Round-8 BLOCK #1: raw R:R or carry-adjusted R:R below min_rr (e.g. 2.49997) must NOT pass after 4-dec rounding to 2.5000."""
        parser = tp.build_parser()
        # Exact Codex Round-8 repro: BTC long 80000/79000/82600, 90d, funding=0.5070965277777778%
        # Raw carry-adjusted R:R = 2.49997 < 2.5, even though 4-decimal display rounds to 2.5.
        args_carry = parser.parse_args([
            "--symbol", "BTC",
            "--direction", "long",
            "--entry", "80000",
            "--stop", "79000",
            "--target", "82600",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--horizon-days", "90",
            "--funding-ann-pct", "0.5070965277777778",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out_carry = tp.build_trade_plan(args_carry)
        self.assertEqual(out_carry["expression_and_carry"]["carry_adjusted_rr_ratio"], 2.5)
        self.assertLess(out_carry["expression_and_carry"]["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertFalse(out_carry["four_quadrant_gate"]["actionable"])
        self.assertEqual(out_carry["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")
        self.assertFalse(out_carry["four_quadrant_gate"]["checks"]["carry_adjusted_rr_ok"])
        self.assertIn("2.49997", out_carry["four_quadrant_gate"]["reason"])

        # Nominal R:R boundary: 2.49997 rounds to 2.5000 at 4 decimals, must still be blocked as watchlist_poor_rr.
        args_nom = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "100",
            "--stop", "99",
            "--target", "102.49997",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--has-l1-l2",
            "--alignment-status", "aligned_trigger",
            "--research-direction", "long",
        ])
        out_nom = tp.build_trade_plan(args_nom)
        self.assertEqual(out_nom["position_plan"]["rr_ratio"], 2.5)
        self.assertLess(out_nom["position_plan"]["rr_ratio_raw"], 2.5)
        self.assertFalse(out_nom["four_quadrant_gate"]["actionable"])
        self.assertEqual(out_nom["four_quadrant_gate"]["decision"], "watchlist_poor_rr")
        self.assertFalse(out_nom["four_quadrant_gate"]["checks"]["rr_ok"])
        self.assertIn("2.49997", out_nom["four_quadrant_gate"]["reason"])

    def test_round8_live_crypto_funding_collector_preserves_tiny_rate(self) -> None:
        """Round-8 BLOCK #2: market_snapshot.fetch_crypto_funding + --live build_trade_plan must NOT zero out 0.0001% annualized funding."""
        import market_snapshot as ms

        tiny_ann_pct = 0.0001
        tiny_rate_8h = tiny_ann_pct / (3.0 * 365.0 * 100.0)

        class _FakeExchange:
            def __init__(self, _cfg: dict) -> None:
                pass

            def fetch_ticker(self, _sym: str) -> dict:
                return {"last": 80000.0}

            def fetch_funding_rate(self, _sym: str) -> dict:
                return {"fundingRate": tiny_rate_8h}

        with (
            unittest.mock.patch.object(ms.ccxt, "okx", _FakeExchange),
            unittest.mock.patch.object(ms.ccxt, "bybit", _FakeExchange),
            unittest.mock.patch.object(ms.ccxt, "binance", _FakeExchange),
        ):
            live_funding = ms.fetch_crypto_funding("BTC")

        self.assertEqual(live_funding["okx"]["funding_annualized_pct"], 0.0)
        self.assertEqual(live_funding["okx"]["funding_rate_8h_pct"], 0.0)
        self.assertAlmostEqual(live_funding["okx"]["funding_annualized_pct_raw"], tiny_ann_pct, places=12)
        self.assertAlmostEqual(live_funding["okx"]["funding_rate_raw"], tiny_rate_8h, places=16)

        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        live_ctx = {
            "snapshot": {
                "symbol": "BTC",
                "asset_class": "crypto",
                "technicals": {"last_close": 80000.0, "atr14": 1500.0, "as_of_bar": now_iso, "as_of_bar_utc": now_iso},
                "intraday_technicals": {
                    "last_close": 80000.0,
                    "atr14": 0.01,
                    "as_of_bar": now_iso,
                    "as_of_bar_utc": now_iso,
                    "bar_is_closed": True,
                },
                "chanlun": {
                    "daily_30m_alignment": {
                        "alignment_status": "aligned_trigger",
                        "daily_bias": "long",
                        "intraday_action": "entry_long",
                        "intraday_tactical_stop": 79999.99,
                    }
                },
                "crypto_perp_funding": live_funding,
            },
            "research": {"fundamentals_and_supply": {}, "sources_status": {}},
            "errors": [],
        }

        parser = tp.build_parser()
        args = parser.parse_args([
            "--symbol", "BTC",
            "--direction", "long",
            "--live",
            "--entry", "80000",
            "--target", "80000.03",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--horizon-days", "90",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        with unittest.mock.patch.object(tp, "_fetch_live_context", return_value=live_ctx):
            out = tp.build_trade_plan(args)

        self.assertEqual(out["position_plan"]["rr_ratio"], 3.0)
        self.assertEqual(out["expression_and_carry"]["carry_adjusted_rr_ratio"], 1.0274)
        self.assertEqual(out["position_plan"]["carry_adjusted_rr_ratio"], 1.0274)
        self.assertFalse(out["four_quadrant_gate"]["actionable"])
        self.assertEqual(out["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")

    def test_misaligned_curve_nulls_raw_roll_and_does_not_block_live_short(self):
        import research_extensions as rext

        summary = rext.summarize_futures_curve(
            [
                {"contract": "TA2612", "expiry_ym": 202612, "price": 5900.0, "date": "2026-11-20", "volume": 1000},
                {"contract": "TA2701", "expiry_ym": 202701, "price": 5800.0, "date": "2026-11-19", "volume": 1000},
                {"contract": "TA2705", "expiry_ym": 202705, "price": 5800.0, "date": "2026-11-19", "volume": 1000},
            ],
            unit="CNY/吨",
        )
        summary["quote_dates_aligned"] = False
        rext._null_misaligned_curve_spreads(summary)

        self.assertIsNone(summary["annualized_roll_yield_near_pct"])
        self.assertIsNone(summary["annualized_roll_yield_near_pct_raw"])
        self.assertIsNone(summary["annualized_roll_yield_curve_pct"])
        self.assertIsNone(summary["annualized_roll_yield_curve_pct_raw"])
        self.assertEqual(summary["structure"], "unaligned_dates")

        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        live_ctx = {
            "snapshot": {
                "symbol": "TA0",
                "asset_class": "cn_future",
                "technicals": {"last_close": 5900.0, "atr14": 80.0, "as_of_bar": now_iso, "as_of_bar_utc": now_iso},
                "intraday_technicals": {
                    "last_close": 5900.0,
                    "atr14": 20.0,
                    "as_of_bar": now_iso,
                    "as_of_bar_utc": now_iso,
                    "bar_is_closed": True,
                },
                "chanlun": {
                    "daily_30m_alignment": {
                        "alignment_status": "aligned_trigger",
                        "daily_bias": "short",
                        "intraday_action": "entry_short",
                        "intraday_tactical_stop": 5950.0,
                    }
                },
            },
            "research": {
                "fundamentals_and_supply": {
                    "cn_futures_term_structure": {"status": "ok", **summary},
                },
                "sources_status": {"cn_futures_term_structure": "ok"},
            },
            "errors": [],
        }
        parser = tp.build_parser()
        args = parser.parse_args([
            "--symbol", "TA0",
            "--direction", "short",
            "--live",
            "--entry", "5900",
            "--target", "5770",
            "--equity", "500000",
            "--account-currency", "CNY",
            "--risk-pct", "1.0",
            "--horizon-days", "90",
            "--has-l1-l2",
            "--research-direction", "short",
        ])
        with unittest.mock.patch.object(tp, "_fetch_live_context", return_value=live_ctx):
            out = tp.build_trade_plan(args)

        self.assertEqual(out["position_plan"]["rr_ratio"], 2.6)
        self.assertIsNone(out["expression_and_carry"]["carry_adjusted_rr_ratio"])
        self.assertTrue(out["four_quadrant_gate"]["actionable"])
        self.assertEqual(out["four_quadrant_gate"]["decision"], "actionable_short")

        # Defensive guard: even if an upstream caller forgets to null *_raw when display is explicitly None
        expr_guarded = tp.evaluate_expression_and_carry(
            direction="short",
            asset_class="cn_future",
            term_structure={
                "status": "ok",
                "quote_dates_aligned": False,
                "structure": "unaligned_dates",
                "annualized_roll_yield_near_pct": None,
                "annualized_roll_yield_near_pct_raw": 20.69,
                "annualized_roll_yield_curve_pct": None,
                "annualized_roll_yield_curve_pct_raw": 4.14,
            },
            holding_horizon_days=90,
            expected_move_pct=2.2034,
            stop_distance_pct=0.8475,
            nominal_rr_ratio=2.6,
        )
        self.assertIsNone(expr_guarded["annualized_carry_pct"])
        self.assertIsNone(expr_guarded["carry_adjusted_rr_ratio_raw"])

    def test_exact_two_point_five_rr_not_rejected_by_float64_noise(self):
        parser = tp.build_parser()
        # Case 1: 100 / 99.99 / 100.025 (float64 gives 2.4999999999992895 without Decimal)
        args1 = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "100",
            "--stop", "99.99",
            "--target", "100.025",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--alignment-status", "aligned_trigger",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        out1 = tp.build_trade_plan(args1)
        self.assertEqual(out1["position_plan"]["rr_ratio_raw"], 2.5)
        self.assertEqual(out1["position_plan"]["rr_ratio"], 2.5)
        self.assertEqual(out1["expression_and_carry"]["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertTrue(out1["four_quadrant_gate"]["actionable"])
        self.assertEqual(out1["four_quadrant_gate"]["decision"], "actionable_long")

        # Case 2: 11 / 9 / 16 (zero carry via pct-conversion gave 2.4999999999999996 without nominal_rr Decimal)
        args2 = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "11",
            "--stop", "9",
            "--target", "16",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--alignment-status", "aligned_trigger",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        out2 = tp.build_trade_plan(args2)
        self.assertEqual(out2["position_plan"]["rr_ratio_raw"], 2.5)
        self.assertEqual(out2["expression_and_carry"]["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertTrue(out2["four_quadrant_gate"]["actionable"])
        self.assertEqual(out2["four_quadrant_gate"]["decision"], "actionable_long")

        # Case 3: Exact 2.5R after non-zero Carry (repeating nominal RR 30/11 + (-0.25) / (11/10) == 5/2 exact)
        args_fx_exact = parser.parse_args([
            "--symbol", "EURUSD=X",
            "--direction", "long",
            "--entry", "1",
            "--stop", "0.989",
            "--target", "1.03",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--rate-diff-pct", "-0.25",
            "--horizon-days", "365",
            "--alignment-status", "aligned_trigger",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        out_fx_exact = tp.build_trade_plan(args_fx_exact)
        self.assertEqual(out_fx_exact["expression_and_carry"]["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertEqual(out_fx_exact["expression_and_carry"]["carry_adjusted_rr_ratio"], 2.5)
        self.assertTrue(out_fx_exact["four_quadrant_gate"]["actionable"])
        self.assertEqual(out_fx_exact["four_quadrant_gate"]["decision"], "actionable_long")

        # Case 4: Infinitesimally worse non-zero Carry (-0.2500001) remains strictly blocked
        args_fx_below = parser.parse_args([
            "--symbol", "EURUSD=X",
            "--direction", "long",
            "--entry", "1",
            "--stop", "0.989",
            "--target", "1.03",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--rate-diff-pct", "-0.2500001",
            "--horizon-days", "365",
            "--alignment-status", "aligned_trigger",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        out_fx_below = tp.build_trade_plan(args_fx_below)
        self.assertLess(out_fx_below["expression_and_carry"]["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertFalse(out_fx_below["four_quadrant_gate"]["actionable"])
        self.assertEqual(out_fx_below["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")

        # Case 5: Genuine sub-threshold 2.4999 remains blocked
        args3 = parser.parse_args([
            "--symbol", "NVDA",
            "--direction", "long",
            "--entry", "100",
            "--stop", "99.99",
            "--target", "100.024999",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--alignment-status", "aligned_trigger",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        out3 = tp.build_trade_plan(args3)
        self.assertLess(out3["position_plan"]["rr_ratio_raw"], 2.5)
        self.assertFalse(out3["four_quadrant_gate"]["actionable"])
        self.assertEqual(out3["four_quadrant_gate"]["decision"], "watchlist_poor_rr")

    def test_unrounded_source_prices_and_fred_yields_preserve_tiny_carry_drag(self):
        import pandas as pd
        import research_data as rdata
        import research_extensions as rext

        # 1. Overseas futures curve: 100.0 vs 100.000017 (4dp rounding would collapse to 0)
        ov_summary = rext.summarize_futures_curve(
            [
                {"contract": "CLZ26", "expiry_ym": 202612, "price": 100.0, "date": "2026-11-20", "volume": 1000},
                {"contract": "CLF27", "expiry_ym": 202701, "price": 100.000017, "date": "2026-11-20", "volume": 1000},
            ],
            unit="USD/桶",
        )
        self.assertEqual(ov_summary["annualized_roll_yield_near_pct"], 0.0)
        self.assertIsNotNone(ov_summary["annualized_roll_yield_near_pct_raw"])
        self.assertLess(ov_summary["annualized_roll_yield_near_pct_raw"], -0.0002)
        self.assertNotIn("_raw_price", ov_summary["contracts"][0])

        expr_cl = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="commodity",
            term_structure={"status": "ok", **ov_summary},
            holding_horizon_days=90,
            expected_move_pct=2.50002,
            stop_distance_pct=1.0,
            nominal_rr_ratio=2.50002,
        )
        self.assertLess(expr_cl["carry_adjusted_rr_ratio_raw"], 2.5)
        gate_cl = tp.evaluate_four_quadrant_gate(
            direction="long",
            asset_class="commodity",
            has_l1_l2_evidence=True,
            daily_alignment={
                "alignment_status": "aligned_trigger",
                "daily_bias": "long",
                "intraday_action": "entry_long",
            },
            research_direction="long",
            rr_ratio=2.50002,
            carry_adjusted_rr_ratio=expr_cl["carry_adjusted_rr_ratio_raw"],
            total_quantity=1,
        )
        self.assertFalse(gate_cl["actionable"])
        self.assertEqual(gate_cl["decision"], "watchlist_poor_carry_adjusted_rr")

        # 2. CN futures curve: 100.0 vs 100.000017 via research_data.summarize_term_structure
        mock_df = pd.DataFrame(
            [
                {"symbol": "CU2612", "name": "沪铜2612", "trade": 100.0, "settlement": 100.0, "volume": 1000, "position": 500, "tradedate": "2026-11-20"},
                {"symbol": "CU2701", "name": "沪铜2701", "trade": 100.000017, "settlement": 100.000017, "volume": 1000, "position": 500, "tradedate": "2026-11-20"},
            ]
        )
        cn_summary = rdata.summarize_term_structure(mock_df)
        self.assertEqual(cn_summary["annualized_roll_yield_near_pct"], 0.0)
        self.assertIsNotNone(cn_summary["annualized_roll_yield_near_pct_raw"])
        self.assertLess(cn_summary["annualized_roll_yield_near_pct_raw"], -0.0002)
        self.assertNotIn("_raw_price", cn_summary["contracts"][0])

        # 3. FRED CSV: 1.99999951 vs 2.00000049 (6dp rounding would collapse both to 2.0)
        eur_obs = rext._parse_fred_csv("DATE,IRLTLT01DEM156N\n2026-10-01,1.99999951\n", "IRLTLT01DEM156N")
        usd_obs = rext._parse_fred_csv("DATE,DGS10\n2026-10-01,2.00000049\n", "DGS10")
        self.assertEqual(eur_obs["latest_value"], 2.0)
        self.assertEqual(usd_obs["latest_value"], 2.0)
        self.assertAlmostEqual(eur_obs["latest_value_raw"], 1.99999951, places=10)
        self.assertAlmostEqual(usd_obs["latest_value_raw"], 2.00000049, places=10)

        def _fake_fred(series_id: str) -> dict:
            return eur_obs if "DEM" in series_id else usd_obs

        with unittest.mock.patch.object(rext, "_fetch_fred_series", side_effect=_fake_fred):
            fx_rd = rext.fetch_fx_rate_differential("EURUSD=X")
        self.assertEqual(fx_rd["base_minus_quote_10y_pct"], 0.0)
        self.assertIsNotNone(fx_rd["base_minus_quote_10y_pct_raw"])
        self.assertLess(fx_rd["base_minus_quote_10y_pct_raw"], 0.0)

        expr_fx = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="fx",
            rate_diff=fx_rd,
            holding_horizon_days=90,
            expected_move_pct=2.5,
            stop_distance_pct=1.0,
            nominal_rr_ratio=2.5,
        )
        self.assertLess(expr_fx["carry_adjusted_rr_ratio_raw"], 2.5)
        gate_fx = tp.evaluate_four_quadrant_gate(
            direction="long",
            asset_class="fx",
            has_l1_l2_evidence=True,
            daily_alignment={
                "alignment_status": "aligned_trigger",
                "daily_bias": "long",
                "intraday_action": "entry_long",
            },
            research_direction="long",
            rr_ratio=2.5,
            carry_adjusted_rr_ratio=expr_fx["carry_adjusted_rr_ratio_raw"],
            total_quantity=1000,
        )
        self.assertFalse(gate_fx["actionable"])
        self.assertEqual(gate_fx["decision"], "watchlist_poor_carry_adjusted_rr")

    def test_fred_decimal_rate_diff_and_near_carry_stress_raw_precision(self):
        from fractions import Fraction
        import research_extensions as rext

        # 1. FRED 3.90% vs 4.15% must produce exact -0.25% (not -0.25000000000000044%)
        de_obs = rext._parse_fred_csv("DATE,IRLTLT01DEM156N\n2026-10-01,3.90\n", "IRLTLT01DEM156N")
        us_obs = rext._parse_fred_csv("DATE,DGS10\n2026-10-01,4.15\n", "DGS10")

        def _fake_fred(series_id: str) -> dict:
            return de_obs if "DEM" in series_id else us_obs

        with unittest.mock.patch.object(rext, "_fetch_fred_series", side_effect=_fake_fred):
            fx_rd = rext.fetch_fx_rate_differential("EURUSD=X")

        self.assertEqual(fx_rd["base_minus_quote_10y_pct_raw"], -0.25)
        self.assertEqual(fx_rd["base_minus_quote_10y_pct"], -0.25)

        # EURUSD=X long: entry=1.0, stop=0.989, target=1.03, horizon=365d -> exact 30/11 - 5/22 = 2.5
        plan_fx = tp.calculate_position_plan(
            symbol="EURUSD=X",
            direction="long",
            asset_class="fx",
            entry_price=1.0,
            tactical_stop_30m=0.989,
            target_price=1.03,
            account_equity=100000.0,
            risk_budget_pct=1.0,
        )
        expr_fx = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="fx",
            rate_diff=fx_rd,
            holding_horizon_days=365,
            expected_move_pct=Fraction(3, 1),
            stop_distance_pct=Fraction(11, 10),
            nominal_rr_ratio=Fraction(30, 11),
        )
        self.assertEqual(expr_fx["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertEqual(expr_fx["carry_adjusted_rr_ratio"], 2.5)
        gate_fx = tp.evaluate_four_quadrant_gate(
            direction="long",
            asset_class="fx",
            has_l1_l2_evidence=True,
            daily_alignment={
                "alignment_status": "aligned_trigger",
                "daily_bias": "long",
                "intraday_action": "entry_long",
            },
            research_direction="long",
            rr_ratio=plan_fx["rr_ratio_raw"],
            carry_adjusted_rr_ratio=expr_fx["carry_adjusted_rr_ratio_raw"],
            total_quantity=plan_fx["total_quantity"],
        )
        self.assertTrue(gate_fx["actionable"])
        self.assertEqual(gate_fx["decision"], "actionable_long")

        # 2. CL=F long: entry=100, stop=99, target=103, 365d, curve=0%, near=-0.50003% -> near stress raw=2.49997
        expr_cl = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="commodity",
            term_structure={
                "status": "ok",
                "structure": "Flat",
                "quote_dates_aligned": True,
                "annualized_roll_yield_curve_pct": 0.0,
                "annualized_roll_yield_curve_pct_raw": 0.0,
                "annualized_roll_yield_near_pct": -0.50003,
                "annualized_roll_yield_near_pct_raw": -0.50003,
            },
            holding_horizon_days=365,
            expected_move_pct=Fraction(3, 1),
            stop_distance_pct=Fraction(1, 1),
            nominal_rr_ratio=Fraction(3, 1),
        )
        self.assertEqual(expr_cl["curve_carry_adjusted_rr_ratio_raw"], 3.0)
        self.assertEqual(expr_cl["near_carry_stress_rr_ratio"], 2.5)
        self.assertIsNotNone(expr_cl["near_carry_stress_rr_ratio_raw"])
        self.assertLess(expr_cl["near_carry_stress_rr_ratio_raw"], 2.5)
        self.assertAlmostEqual(expr_cl["near_carry_stress_rr_ratio_raw"], 2.49997, places=7)

        # Verify stale branch also includes *_raw keys as None
        expr_stale = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="commodity",
            term_structure={"status": "stale"},
            holding_horizon_days=365,
            expected_move_pct=Fraction(3, 1),
            stop_distance_pct=Fraction(1, 1),
            nominal_rr_ratio=Fraction(3, 1),
        )
        self.assertIn("near_carry_stress_rr_ratio_raw", expr_stale)
        self.assertIn("curve_carry_adjusted_rr_ratio_raw", expr_stale)
        self.assertIsNone(expr_stale["near_carry_stress_rr_ratio_raw"])
        self.assertIsNone(expr_stale["curve_carry_adjusted_rr_ratio_raw"])

        # 3. Verify 1.0000005 entry quantized target 1.025004 passes >= 2.5R on backend
        plan_q = tp.calculate_position_plan(
            symbol="AAPL",
            direction="long",
            asset_class="us_stock_or_etf",
            entry_price=1.0000005,
            tactical_stop_30m=0.99,
            target_price=1.025004,
            account_equity=100000.0,
            risk_budget_pct=1.0,
        )
        self.assertEqual(plan_q["entry_price"], 1.000001)
        self.assertGreaterEqual(plan_q["rr_ratio_raw"], 2.5)

    def test_exact_futures_curve_roll_yield_and_zero_quantized_stop(self):
        import pandas as pd
        import research_data as rdata
        import research_extensions as rext

        # 1. Overseas curve CLZ26=99.50, CLZ27=100.00 (12m) -> exact -0.5% (no -0.5000000000000004%)
        ov_curve = rext.summarize_futures_curve(
            [
                {"contract": "CLZ26", "expiry_ym": 202612, "price": 99.50, "date": "2026-11-20", "volume": 1000},
                {"contract": "CLZ27", "expiry_ym": 202712, "price": 100.00, "date": "2026-11-20", "volume": 1000},
            ],
            unit="USD/桶",
        )
        self.assertEqual(ov_curve["annualized_roll_yield_curve_pct_raw"], -0.5)
        self.assertEqual(ov_curve["annualized_roll_yield_curve_pct_exact"], "-1/2")
        self.assertEqual(ov_curve["annualized_roll_yield_near_pct_raw"], -0.5)
        self.assertEqual(ov_curve["annualized_roll_yield_near_pct_exact"], "-1/2")

        expr_cl = tp.evaluate_expression_and_carry(
            direction="long",
            asset_class="commodity",
            term_structure={"status": "ok", **ov_curve},
            holding_horizon_days=365,
            expected_move_pct=3.0,
            stop_distance_pct=1.0,
            nominal_rr_ratio=3.0,
        )
        self.assertEqual(expr_cl["carry_adjusted_rr_ratio_raw"], 2.5)
        self.assertEqual(expr_cl["near_carry_stress_rr_ratio_raw"], 2.5)
        gate_cl = tp.evaluate_four_quadrant_gate(
            direction="long",
            asset_class="commodity",
            has_l1_l2_evidence=True,
            daily_alignment={
                "alignment_status": "aligned_trigger",
                "daily_bias": "long",
                "intraday_action": "entry_long",
            },
            research_direction="long",
            rr_ratio=3.0,
            carry_adjusted_rr_ratio=expr_cl["carry_adjusted_rr_ratio_raw"],
            total_quantity=1,
        )
        self.assertTrue(gate_cl["actionable"])
        self.assertEqual(gate_cl["decision"], "actionable_long")

        # Misaligned curve clears _exact too
        misaligned = dict(ov_curve)
        misaligned["contracts"] = [
            {**ov_curve["contracts"][0], "date": "2026-11-20"},
            {**ov_curve["contracts"][1], "date": "2026-11-19"},
        ]
        rext._null_misaligned_curve_spreads(misaligned)
        self.assertIsNone(misaligned["annualized_roll_yield_curve_pct_exact"])
        self.assertIsNone(misaligned["annualized_roll_yield_near_pct_exact"])

        # 2. CN futures curve CU2612=99.50, CU2712=100.00 (12m) -> exact -0.5%
        cn_df = pd.DataFrame(
            [
                {"symbol": "CU2612", "name": "沪铜2612", "trade": 99.50, "settlement": 99.50, "volume": 1000, "position": 500, "tradedate": "2026-11-20"},
                {"symbol": "CU2712", "name": "沪铜2712", "trade": 100.00, "settlement": 100.00, "volume": 1000, "position": 500, "tradedate": "2026-11-20"},
            ]
        )
        cn_curve = rdata.summarize_term_structure(cn_df)
        self.assertEqual(cn_curve["annualized_roll_yield_curve_pct_raw"], -0.5)
        self.assertEqual(cn_curve["annualized_roll_yield_curve_pct_exact"], "-1/2")

        # 3. Crypto funding exact rational arithmetic: 0.0001 -> exact 10.95%; [0.1, 0.2, 0.3] -> exact 0.2%
        ann_single, _ = tp._extract_crypto_annualized_funding({"funding_rate_raw": 0.0001})
        self.assertEqual(float(ann_single), 10.95)
        ann_avg, _ = tp._extract_crypto_annualized_funding(
            {
                "okx": {"funding_annualized_pct_raw": 0.1},
                "bybit": {"funding_annualized_pct_raw": 0.2},
                "binance": {"funding_annualized_pct_raw": 0.3},
            }
        )
        self.assertEqual(float(ann_avg), 0.2)

        # 4. Stop quantized to 0 (0.000010 / 0.0000004 / 0.000040) collapses quantity to 0
        plan_zero = tp.calculate_position_plan(
            symbol="BTC",
            direction="long",
            asset_class="crypto",
            entry_price=0.000010,
            tactical_stop_30m=0.0000004,
            target_price=0.000040,
            account_equity=100000.0,
            risk_budget_pct=1.0,
        )
        self.assertEqual(plan_zero["tactical_stop_30m"], 0.0)
        self.assertEqual(plan_zero["total_quantity"], 0)
        self.assertEqual(plan_zero["sizing_binding_constraint"], "stop_precision_collapsed")

        # 5. _opt_exact_fraction accepts numeric floats/Decimals/Ints and returns None on invalid inputs
        from fractions import Fraction
        self.assertEqual(tp._opt_exact_fraction(1.5), Fraction(3, 2))
        self.assertEqual(tp._opt_exact_fraction("-1/2"), Fraction(-1, 2))
        self.assertIsNone(tp._opt_exact_fraction(float("nan")))
        self.assertIsNone(tp._opt_exact_fraction("not-a-fraction"))

        # 6. Manual --roll-yield / --near-roll-yield overrides clear stale live *_exact & unaligned flags
        args_ov = tp.build_parser().parse_args([
            "--symbol", "CL=F",
            "--live",
            "--direction", "long",
            "--entry", "100",
            "--target", "103",
            "--equity", "100000",
            "--risk-pct", "1.0",
            "--horizon-days", "365",
            "--roll-yield", "-10.0",
            "--near-roll-yield", "-12.0",
            "--has-l1-l2",
            "--research-direction", "long",
        ])
        fake_live_ctx = {
            "errors": [],
            "snapshot": {
                "symbol": "CL=F",
                "asset_class": "commodity",
                "technicals": {"last_close": 100.0},
                "chanlun": {
                    "dual_alignment": {
                        "alignment_status": "aligned_trigger",
                        "daily_bias": "long",
                        "intraday_action": "entry_long",
                        "intraday_tactical_stop": 99.0,
                        "daily_structure_stop": 95.0,
                    },
                },
            },
            "research": {
                "sources_status": {"term_structure": "ok"},
                "fundamentals_and_supply": {
                    "term_structure": {
                        "status": "ok",
                        "structure": "unaligned_dates",
                        "quote_dates_aligned": False,
                        "annualized_roll_yield_curve_pct": -0.5,
                        "annualized_roll_yield_curve_pct_raw": -0.5,
                        "annualized_roll_yield_curve_pct_exact": "-1/2",
                        "annualized_roll_yield_near_pct": -0.5,
                        "annualized_roll_yield_near_pct_raw": -0.5,
                        "annualized_roll_yield_near_pct_exact": "-1/2",
                    }
                },
            },
        }
        with unittest.mock.patch.object(tp, "_fetch_live_context", return_value=fake_live_ctx):
            out_ov = tp.build_trade_plan(args_ov)
        self.assertEqual(out_ov["expression_and_carry"]["curve_annualized_carry_pct"], -10.0)
        self.assertEqual(out_ov["expression_and_carry"]["near_annualized_carry_pct"], -12.0)
        self.assertEqual(out_ov["expression_and_carry"]["annualized_carry_pct"], -10.0)


if __name__ == "__main__":
    unittest.main()
