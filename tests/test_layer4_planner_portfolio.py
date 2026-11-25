#!/usr/bin/env python3
"""Offline unit tests for Layer-4 trade_planner and portfolio_manager."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import portfolio_manager as pm  # noqa: E402
from portfolio_manager import (  # noqa: E402
    add_position_tranche,
    check_sector_slot_admission,
    close_position,
    effective_risk_scale,
    init_portfolio,
    load_account,
    load_journal,
    load_positions,
    main as portfolio_main,
    open_position,
    rotate_leader,
    summarize_portfolio,
    sync_mirrors,
    trail_stop,
    trim_position,
    update_account,
)
from trade_planner import (  # noqa: E402
    CN_FUTURE_SPECS,
    CONTRACT_SPECS,
    OVERSEAS_FUTURE_SPECS,
    calculate_position_plan,
    evaluate_expression_and_carry,
    evaluate_four_quadrant_gate,
    get_contract_spec,
    main as planner_main,
)


class TestContractSpecs(unittest.TestCase):
    def test_all_cn_futures_specs_and_contract_month_stripping(self):
        expected_cn = {
            "CU": (5.0, 0.12), "AL": (5.0, 0.12), "ZN": (5.0, 0.12),
            "NI": (1.0, 0.14), "SN": (1.0, 0.14), "PB": (5.0, 0.12),
            "AU": (1000.0, 0.10), "AG": (15.0, 0.14),
            "RB": (10.0, 0.12), "HC": (10.0, 0.12),
            "I": (100.0, 0.15), "J": (100.0, 0.15), "JM": (60.0, 0.15),
            "SF": (5.0, 0.12), "SM": (5.0, 0.12),
            "LC": (1.0, 0.15), "SI": (5.0, 0.14), "PS": (3.0, 0.14),
            "SC": (1000.0, 0.12), "TA": (5.0, 0.10), "MA": (10.0, 0.10),
            "V": (5.0, 0.10), "SA": (20.0, 0.12), "FG": (20.0, 0.12),
            "RU": (10.0, 0.12), "FU": (10.0, 0.12), "PG": (20.0, 0.12),
            "LH": (16.0, 0.12), "M": (10.0, 0.10),
            "Y": (10.0, 0.10), "P": (10.0, 0.10), "OI": (10.0, 0.10),
            "SR": (10.0, 0.10), "CF": (5.0, 0.10), "C": (10.0, 0.10),
            "EC": (50.0, 0.18),
        }
        self.assertEqual(len(CN_FUTURE_SPECS), len(expected_cn))
        for root, (mult, margin) in expected_cn.items():
            self.assertIn(root, CONTRACT_SPECS)
            spec_cont = get_contract_spec(f"{root}0")
            self.assertTrue(spec_cont["known_spec"])
            self.assertEqual(spec_cont["asset_class"], "cn_future")
            self.assertEqual(spec_cont["multiplier"], mult)
            self.assertEqual(spec_cont["margin_rate"], margin)
            self.assertEqual(spec_cont["currency"], "CNY")
            self.assertEqual(spec_cont["lot_size"], 1)
            self.assertFalse(spec_cont["fractional"])

            # 4-digit dated contract strips cleanly to root (e.g., CU2701 -> CU)
            spec_dated = get_contract_spec(f"{root}2701")
            self.assertTrue(spec_dated["known_spec"])
            self.assertEqual(spec_dated["asset_class"], "cn_future")
            self.assertEqual(spec_dated["root"], root)
            self.assertEqual(spec_dated["multiplier"], mult)

        # Chinese alias and CZCE 3-digit contract month
        self.assertEqual(get_contract_spec("沪铜")["multiplier"], 5.0)
        self.assertEqual(get_contract_spec("TA705")["multiplier"], 5.0)
        self.assertEqual(get_contract_spec("SI0")["multiplier"], 5.0)
        self.assertEqual(get_contract_spec("SI0")["currency"], "CNY")

        # US tickers colliding with bare CN letters stay US stocks when no 0/month suffix
        self.assertEqual(get_contract_spec("V")["asset_class"], "us_stock_or_etf")
        self.assertEqual(get_contract_spec("V")["currency"], "USD")
        self.assertEqual(get_contract_spec("MA")["asset_class"], "us_stock_or_etf")

    def test_overseas_futures_exact_quote_unit_multipliers(self):
        expected_overseas = {
            "CL=F": (1000.0, 0.10),
            "BZ=F": (1000.0, 0.10),
            "NG=F": (10000.0, 0.15),
            "HG=F": (25000.0, 0.10),
            "GC=F": (100.0, 0.08),
            "SI=F": (5000.0, 0.12),
            "ALI=F": (25.0, 0.10),
            "ZC=F": (50.0, 0.08),    # USc/bu * 5000 bu = 50 USD/pt
            "ZS=F": (50.0, 0.08),    # USc/bu * 5000 bu = 50 USD/pt
            "ZW=F": (50.0, 0.08),    # USc/bu * 5000 bu = 50 USD/pt
            "SB=F": (1120.0, 0.10),  # USc/lb * 112000 lb = 1120 USD/pt
            "CT=F": (500.0, 0.10),   # USc/lb * 50000 lb = 500 USD/pt
            "KC=F": (375.0, 0.12),   # USc/lb * 37500 lb = 375 USD/pt
            "CC=F": (10.0, 0.15),    # USD/mt * 10 mt = 10 USD/pt
            "LE=F": (400.0, 0.08),   # USc/lb * 40000 lb = 400 USD/pt
            "HE=F": (400.0, 0.08),   # USc/lb * 40000 lb = 400 USD/pt
        }
        self.assertEqual(len(OVERSEAS_FUTURE_SPECS), len(expected_overseas))
        for sym, (mult, margin) in expected_overseas.items():
            spec = get_contract_spec(sym)
            self.assertTrue(spec["known_spec"])
            self.assertEqual(spec["asset_class"], "commodity")
            self.assertEqual(spec["multiplier"], mult)
            self.assertEqual(spec["margin_rate"], margin)
            self.assertEqual(spec["currency"], "USD")
            if sym in {"ZC=F", "ZS=F", "ZW=F", "SB=F", "CT=F", "KC=F", "LE=F", "HE=F"}:
                self.assertIn("cents", spec["unit_note"].lower())

        # Dated overseas contracts (2-letter and 3-letter roots with exchange suffix)
        cl_dated = get_contract_spec("CLZ26.NYM")
        self.assertTrue(cl_dated["known_spec"])
        self.assertEqual(cl_dated["multiplier"], 1000.0)

        ali_dated = get_contract_spec("ALIZ26.CMX")
        self.assertTrue(ali_dated["known_spec"])
        self.assertEqual(ali_dated["asset_class"], "commodity")
        self.assertEqual(ali_dated["multiplier"], 25.0)

        # Bare overseas futures codes WITHOUT exchange suffix MUST NOT fall through to us_stock_or_etf
        # even when caller explicitly passes asset_class="us_stock_or_etf"!
        for bare_code in ("CLZ26", "BZG27", "HGZ26", "ALIZ26"):
            for ac_override in (None, "us_stock_or_etf", "commodity"):
                bare_spec = get_contract_spec(bare_code, ac_override)
                self.assertFalse(bare_spec["known_spec"], (bare_code, ac_override))
                self.assertIsNone(bare_spec["multiplier"], (bare_code, ac_override))
                self.assertEqual(bare_spec["asset_class"], "commodity", (bare_code, ac_override))
            with self.assertRaises(ValueError):
                calculate_position_plan(bare_code, "long", entry_price=80.0, tactical_stop_30m=78.0)
            with self.assertRaises(ValueError):
                calculate_position_plan(
                    bare_code, "long", entry_price=80.0, tactical_stop_30m=78.0, asset_class="us_stock_or_etf"
                )

    def test_equities_fx_crypto_granularity_and_unknown_futures(self):
        # Main Board & ChiNext A-shares: min_order_qty=100, lot_step=100
        a_spec = get_contract_spec("600519")
        self.assertEqual(a_spec["asset_class"], "a_share")
        self.assertEqual(a_spec["multiplier"], 1.0)
        self.assertEqual(a_spec["min_order_qty"], 100)
        self.assertEqual(a_spec["lot_step"], 100)
        self.assertEqual(a_spec["lot_size"], 100)
        self.assertEqual(a_spec["currency"], "CNY")
        self.assertEqual(a_spec["quote_currency"], "CNY")
        self.assertFalse(a_spec["fractional"])

        chinext_spec = get_contract_spec("300750.SZ")
        self.assertEqual(chinext_spec["min_order_qty"], 100)
        self.assertEqual(chinext_spec["lot_step"], 100)

        # STAR Market (688xxx.SS / 688xxx): min_order_qty=200, lot_step=1
        star_spec = get_contract_spec("688001")
        self.assertEqual(star_spec["asset_class"], "a_share")
        self.assertEqual(star_spec["min_order_qty"], 200)
        self.assertEqual(star_spec["lot_step"], 1)
        self.assertEqual(star_spec["lot_size"], 1)
        self.assertEqual(star_spec["quote_currency"], "CNY")

        # BSE (4/8/92xxxx.BJ): min_order_qty=100, lot_step=1
        for bse_sym in ("430047.BJ", "830799", "920001"):
            bse_spec = get_contract_spec(bse_sym)
            self.assertEqual(bse_spec["asset_class"], "a_share")
            self.assertEqual(bse_spec["min_order_qty"], 100)
            self.assertEqual(bse_spec["lot_step"], 1)

        us_spec = get_contract_spec("NVDA")
        self.assertEqual(us_spec["asset_class"], "us_stock_or_etf")
        self.assertEqual(us_spec["min_order_qty"], 1)
        self.assertEqual(us_spec["lot_step"], 1)
        self.assertEqual(us_spec["lot_size"], 1)
        self.assertEqual(us_spec["currency"], "USD")
        self.assertEqual(us_spec["quote_currency"], "USD")

        # FX exact quote currencies
        fx_expected = {
            "EURUSD=X": "USD",
            "USDJPY": "JPY",
            "USDCNY=X": "CNY",
            "USDCAD=X": "CAD",
        }
        for fx_sym, exp_quote in fx_expected.items():
            fx_spec = get_contract_spec(fx_sym)
            self.assertEqual(fx_spec["asset_class"], "fx")
            self.assertEqual(fx_spec["min_order_qty"], 1000)
            self.assertEqual(fx_spec["lot_step"], 1000)
            self.assertEqual(fx_spec["lot_size"], 1000)
            self.assertEqual(fx_spec["currency"], exp_quote)
            self.assertEqual(fx_spec["quote_currency"], exp_quote)
            self.assertEqual(fx_spec["margin_rate"], 0.05)

        cry_spec = get_contract_spec("BTC")
        self.assertEqual(cry_spec["asset_class"], "crypto")
        self.assertEqual(cry_spec["min_order_qty"], 0.001)
        self.assertEqual(cry_spec["lot_step"], 0.001)
        self.assertEqual(cry_spec["lot_size"], 0.001)
        self.assertTrue(cry_spec["fractional"])

        unk_ov = get_contract_spec("ZZZ=F")
        self.assertFalse(unk_ov["known_spec"])
        self.assertIsNone(unk_ov["multiplier"])

        unk_cn = get_contract_spec("ZZ0")
        self.assertFalse(unk_cn["known_spec"])
        self.assertIsNone(unk_cn["multiplier"])


class TestExpressionCarryAndGate(unittest.TestCase):
    def test_backwardation_never_vetoes_short_and_flags_strong_reality_weak_expectation(self):
        # Test exact research_extensions.py string format ("Backwardation (现货升水/多头正展期)")
        res = evaluate_expression_and_carry(
            direction="short",
            asset_class="cn_future",
            term_structure={
                "structure": "Backwardation (现货升水/多头正展期)",
                "annualized_roll_yield_near_pct": 14.0,
                "annualized_roll_yield_curve_pct": 12.0,
                "front_contract": "CU2612",
                "far_contract": "CU2706",
            },
            holding_horizon_days=90,
            expected_move_pct=15.0,
            stop_distance_pct=3.0,
        )
        self.assertFalse(res["vetoed"])
        self.assertEqual(res["curve_structure"], "Backwardation")
        self.assertEqual(res["divergence_regime"], "strong_reality_weak_expectation")
        # Survival Rule #1: primary medium-frequency carry uses full-curve (-12.0%);
        # near-month squeeze (-14.0%) is exposed separately as near_carry_stress_rr_ratio!
        self.assertEqual(res["curve_annualized_carry_pct"], -12.0)
        self.assertEqual(res["near_annualized_carry_pct"], -14.0)
        self.assertEqual(res["annualized_carry_pct"], -12.0)
        self.assertAlmostEqual(res["curve_horizon_carry_pct"], -12.0 * (90.0 / 365.0), places=3)
        self.assertAlmostEqual(res["near_horizon_carry_pct"], -14.0 * (90.0 / 365.0), places=3)
        self.assertAlmostEqual(res["horizon_carry_pct"], -12.0 * (90.0 / 365.0), places=3)
        self.assertAlmostEqual(res["carry_to_target_ratio"], res["horizon_carry_pct"] / 15.0, places=3)
        self.assertAlmostEqual(
            res["carry_adjusted_rr_ratio"],
            (15.0 + res["curve_horizon_carry_pct"]) / 3.0,
            places=3,
        )
        self.assertAlmostEqual(
            res["near_carry_stress_rr_ratio"],
            (15.0 + res["near_horizon_carry_pct"]) / 3.0,
            places=3,
        )
        self.assertEqual(res["expression_recommendation"], "prefer_far_month_short_or_bear_spread")
        self.assertIn("空远月合约", res["contract_guidance"])

        # Real-world CU0 survival-rule proof: -39.8% near-month squeeze vs -5.4% full-curve Back
        # MUST NOT veto a 3.5x raw R:R far-month short (curve carry-adj R:R = +2.89 >= 2.5)!
        cu_live_expr = evaluate_expression_and_carry(
            direction="short",
            asset_class="cn_future",
            term_structure={
                "status": "ok",
                "structure": "Backwardation",
                "annualized_roll_yield_near_pct": 39.8,
                "annualized_roll_yield_curve_pct": 5.4,
            },
            holding_horizon_days=90,
            expected_move_pct=7.61,
            stop_distance_pct=2.17,
        )
        self.assertFalse(cu_live_expr["vetoed"])
        self.assertGreater(cu_live_expr["carry_adjusted_rr_ratio"], 2.5)
        self.assertLess(cu_live_expr["near_carry_stress_rr_ratio"], 0.0)
        self.assertEqual(cu_live_expr["expression_recommendation"], "prefer_far_month_short_or_bear_spread")
        cu_live_gate = evaluate_four_quadrant_gate(
            direction="short",
            asset_class="cn_future",
            has_l1_l2_evidence=True,
            daily_alignment={"alignment_status": "aligned_trigger", "daily_bias": "short", "intraday_action": "entry_short"},
            rr_ratio=3.5,
            carry_adjusted_rr_ratio=cu_live_expr["carry_adjusted_rr_ratio"],
            total_quantity=1,
            within_capital_limits=True,
        )
        self.assertTrue(cu_live_gate["actionable"])
        self.assertEqual(cu_live_gate["decision"], "actionable_short")

        # Flat full-curve + steep near-month Backwardation (+24% roll -> -24% short near carry)
        res_flat_steep_front = evaluate_expression_and_carry(
            direction="short",
            asset_class="cn_future",
            term_structure={
                "status": "ok",
                "structure": "Flat",
                "annualized_roll_yield_curve_pct": 0.0,
                "annualized_roll_yield_near_pct": 24.0,
            },
            holding_horizon_days=90,
            expected_move_pct=15.0,
            stop_distance_pct=3.0,
        )
        self.assertFalse(res_flat_steep_front["vetoed"])
        self.assertEqual(res_flat_steep_front["expression_recommendation"], "prefer_far_month_short_or_bear_spread")
        self.assertEqual(res_flat_steep_front["near_annualized_carry_pct"], -24.0)
        self.assertEqual(res_flat_steep_front["curve_horizon_carry_pct"], 0.0)
        self.assertAlmostEqual(res_flat_steep_front["near_horizon_carry_pct"], -24.0 * (90.0 / 365.0), places=3)
        self.assertAlmostEqual(res_flat_steep_front["horizon_carry_pct"], 0.0, places=3)
        self.assertAlmostEqual(res_flat_steep_front["carry_adjusted_rr_ratio"], 5.0, places=3)
        self.assertAlmostEqual(
            res_flat_steep_front["near_carry_stress_rr_ratio"],
            (15.0 + res_flat_steep_front["near_horizon_carry_pct"]) / 3.0,
            places=3,
        )
        self.assertTrue(any("squeeze" in w or "挤仓" in w for w in res_flat_steep_front["warnings"]))

        # Stale / error curve data MUST emit curve_data_warning, require refresh, and NEVER emit carry metrics
        res_stale = evaluate_expression_and_carry(
            direction="short",
            asset_class="commodity",
            term_structure={
                "status": "stale",
                "structure": "Backwardation",
                "annualized_roll_yield_near_pct": 14.0,
                "annualized_roll_yield_curve_pct": 10.0,
            },
            holding_horizon_days=90,
            expected_move_pct=15.0,
            stop_distance_pct=3.0,
        )
        self.assertIsNotNone(res_stale["curve_data_warning"])
        self.assertTrue(any("curve_data_warning" in w for w in res_stale["warnings"]))
        self.assertEqual(res_stale["expression_recommendation"], "data_stale_refresh_required")
        self.assertEqual(res_stale["curve_structure"], "unknown")
        self.assertIsNone(res_stale["annualized_carry_pct"])
        self.assertIsNone(res_stale["near_annualized_carry_pct"])
        self.assertIsNone(res_stale["curve_annualized_carry_pct"])
        self.assertIsNone(res_stale["horizon_carry_pct"])
        self.assertIsNone(res_stale["carry_adjusted_rr_ratio"])

        res_err = evaluate_expression_and_carry(
            direction="short",
            asset_class="commodity",
            term_structure={"status": "error", "error": "upstream timeout", "annualized_roll_yield_curve_pct": 10.0},
            holding_horizon_days=90,
            expected_move_pct=15.0,
            stop_distance_pct=3.0,
        )
        self.assertIsNotNone(res_err["curve_data_warning"])
        self.assertEqual(res_err["expression_recommendation"], "data_stale_refresh_required")
        self.assertEqual(res_err["curve_structure"], "unknown")
        self.assertIsNone(res_err["annualized_carry_pct"])
        self.assertIsNone(res_err["near_annualized_carry_pct"])
        self.assertIsNone(res_err["curve_annualized_carry_pct"])
        self.assertIsNone(res_err["horizon_carry_pct"])
        self.assertIsNone(res_err["carry_adjusted_rr_ratio"])

        # Long in Contango flags weak_reality_strong_expectation without veto
        res_long_cont = evaluate_expression_and_carry(
            direction="long",
            asset_class="commodity",
            term_structure={
                "structure": "Contango",
                "annualized_roll_yield_curve_pct": -10.0,
            },
            holding_horizon_days=180,
            expected_move_pct=20.0,
        )
        self.assertFalse(res_long_cont["vetoed"])
        self.assertEqual(res_long_cont["divergence_regime"], "weak_reality_strong_expectation")
        self.assertEqual(res_long_cont["annualized_carry_pct"], -10.0)

        # Short in Contango enjoys positive carry tailwind
        res_short_cont = evaluate_expression_and_carry(
            direction="short",
            asset_class="cn_future",
            term_structure={"structure": "Contango", "annualized_roll_yield_curve_pct": -9.0},
        )
        self.assertFalse(res_short_cont["vetoed"])
        self.assertEqual(res_short_cont["annualized_carry_pct"], 9.0)

    def test_fx_and_crypto_carry_expression(self):
        # USDJPY short pays negative carry when US10Y - JP10Y = +3.5%
        fx_res = evaluate_expression_and_carry(
            direction="short",
            asset_class="fx",
            rate_diff={"base_minus_quote_10y_pct": 3.5},
            holding_horizon_days=90,
            expected_move_pct=8.0,
        )
        self.assertFalse(fx_res["vetoed"])
        self.assertEqual(fx_res["annualized_carry_pct"], -3.5)
        self.assertIn("steep_negative_fx_carry", fx_res["warnings"])
        self.assertIn("交叉盘", fx_res["contract_guidance"])

        # Crypto perp funding: positive funding -> long pays, short collects
        cry_long = evaluate_expression_and_carry(
            direction="long",
            asset_class="crypto",
            crypto_funding={
                "okx": {"funding_annualized_pct": 20.0},
                "binance": {"funding_annualized_pct": 24.0},
            },
            holding_horizon_days=30,
        )
        self.assertEqual(cry_long["annualized_carry_pct"], -22.0)
        self.assertIn("crowded_long_perp_funding", cry_long["warnings"])

        cry_short = evaluate_expression_and_carry(
            direction="short",
            asset_class="crypto",
            crypto_funding={"funding_annualized_pct": 18.0},
        )
        self.assertEqual(cry_short["annualized_carry_pct"], 18.0)

    def test_four_quadrant_gate_fail_closed_all_branches(self):
        aligned_short = {
            "alignment_status": "aligned_trigger",
            "daily_bias": "short",
            "intraday_action": "entry_short",
        }
        aligned_long = {
            "alignment_status": "aligned_trigger",
            "daily_bias": "long",
            "intraday_action": "entry_long",
        }

        # 1) Falsified thesis -> rejected
        g1 = evaluate_four_quadrant_gate("long", "commodity", True, thesis_falsified=True)
        self.assertEqual(g1["decision"], "rejected")
        self.assertFalse(g1["actionable"])

        # 2) Equity outright short blocked unless hedge_only=True
        g2 = evaluate_four_quadrant_gate("short", "us_stock_or_etf", True, daily_alignment=aligned_short, rr_ratio=3.0)
        self.assertEqual(g2["decision"], "rejected")
        self.assertIn("股票中长线以做多为主", g2["reason"])

        g2_a = evaluate_four_quadrant_gate("short", "a_share", True, daily_alignment=aligned_short, rr_ratio=3.0)
        self.assertEqual(g2_a["decision"], "rejected")

        g2_hedge = evaluate_four_quadrant_gate(
            "short", "us_stock_or_etf", True,
            daily_alignment=aligned_short, rr_ratio=3.0, total_quantity=100, hedge_only=True,
        )
        self.assertEqual(g2_hedge["decision"], "actionable_short")
        self.assertTrue(g2_hedge["actionable"])

        # 3) Upstream errors or curve_stale_or_error -> fail-closed watchlist_upstream_error
        g_up = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_short, rr_ratio=4.0, total_quantity=2,
            upstream_errors=["snapshot_error: timeout"],
        )
        self.assertEqual(g_up["decision"], "watchlist_upstream_error")
        self.assertFalse(g_up["actionable"])

        g_curve_stale = evaluate_four_quadrant_gate(
            "short", "commodity", True,
            daily_alignment=aligned_short, rr_ratio=4.0, total_quantity=2,
            curve_stale_or_error=True,
        )
        self.assertEqual(g_curve_stale["decision"], "watchlist_upstream_error")
        self.assertFalse(g_curve_stale["actionable"])

        # 4) Sizing == 0, sizing omitted (None), or capital breach -> fail closed!
        g_zero = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_short, rr_ratio=4.0, total_quantity=0,
        )
        self.assertEqual(g_zero["decision"], "rejected_sizing_or_capital")
        self.assertFalse(g_zero["actionable"])

        g_none_qty = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_short, rr_ratio=4.0, total_quantity=None,
        )
        self.assertEqual(g_none_qty["decision"], "watchlist_missing_sizing")
        self.assertFalse(g_none_qty["actionable"])
        self.assertFalse(g_none_qty["checks"]["sizing_ok"])

        g_cap = evaluate_four_quadrant_gate(
            "long", "us_stock_or_etf", True,
            daily_alignment=aligned_long, rr_ratio=4.0, total_quantity=100, within_capital_limits=False,
        )
        self.assertEqual(g_cap["decision"], "rejected_sizing_or_capital")
        self.assertFalse(g_cap["actionable"])

        # 5) Missing L1/L2 evidence or >=2 pending verifications -> watchlist
        g3 = evaluate_four_quadrant_gate("long", "us_stock_or_etf", has_l1_l2_evidence=False)
        self.assertEqual(g3["decision"], "watchlist")
        g3b = evaluate_four_quadrant_gate("long", "us_stock_or_etf", has_l1_l2_evidence=True, pending_verifications_count=2)
        self.assertEqual(g3b["decision"], "watchlist")

        # 6) Fail-closed alignment: None, unknown status, missing daily_bias/intraday_action, or direction mismatch
        g_none_align = evaluate_four_quadrant_gate("long", "us_stock_or_etf", True, daily_alignment=None, rr_ratio=3.5, total_quantity=10)
        self.assertEqual(g_none_align["decision"], "watchlist_unverified")
        self.assertFalse(g_none_align["actionable"])

        g_unk_align = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment={"alignment_status": "aligned_short"},
            rr_ratio=3.5, total_quantity=2,
        )
        self.assertEqual(g_unk_align["decision"], "watchlist_unverified")
        self.assertFalse(g_unk_align["actionable"])

        # Omitting daily_bias or intraday_action MUST fail closed (no bypass via partial alignment dict!)
        for incomplete_align in (
            {"alignment_status": "aligned_trigger"},
            {"alignment_status": "aligned_trigger", "daily_bias": "short"},
            {"alignment_status": "aligned_trigger", "intraday_action": "entry_short"},
            {"alignment_status": "aligned_trigger", "daily_bias": None, "intraday_action": "entry_short"},
            {"alignment_status": "aligned_trigger", "daily_bias": "short", "intraday_action": None},
        ):
            g_inc = evaluate_four_quadrant_gate(
                "short", "cn_future", True,
                daily_alignment=incomplete_align,
                rr_ratio=3.5, total_quantity=2,
            )
            self.assertEqual(g_inc["decision"], "watchlist_unverified", incomplete_align)
            self.assertFalse(g_inc["actionable"], incomplete_align)

        g_mismatch_align = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_long,
            rr_ratio=3.5, total_quantity=2,
        )
        self.assertEqual(g_mismatch_align["decision"], "watchlist_unverified")
        self.assertFalse(g_mismatch_align["actionable"])

        # 7) Daily opposes direction -> watchlist (no catching falling knife)
        g4 = evaluate_four_quadrant_gate(
            "long", "us_stock_or_etf", True,
            daily_alignment={"alignment_status": "daily_opposes_direction"},
            rr_ratio=4.0, total_quantity=10,
        )
        self.assertEqual(g4["decision"], "watchlist")
        self.assertIn("日线方向相反", g4["reason"])

        # 8) Daily OK wait 30m -> watchlist_wait_30m
        g5 = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment={"alignment_status": "daily_ok_wait_30m"},
            rr_ratio=3.5, total_quantity=2,
        )
        self.assertEqual(g5["decision"], "watchlist_wait_30m")

        # 9) Missing R:R -> watchlist_missing_rr; Poor R:R -> watchlist_poor_rr; Poor carry-adjusted R:R -> blocked!
        g_no_rr = evaluate_four_quadrant_gate(
            "short", "cn_future", True, daily_alignment=aligned_short, rr_ratio=None, total_quantity=2,
        )
        self.assertEqual(g_no_rr["decision"], "watchlist_missing_rr")
        self.assertFalse(g_no_rr["actionable"])

        g6 = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_short,
            rr_ratio=2.1,
            min_rr=2.5,
            total_quantity=2,
        )
        self.assertEqual(g6["decision"], "watchlist_poor_rr")

        # CL=F short raw R:R 3.0 + steep Backwardation carry-adjusted R:R -0.6986 -> blocked!
        g_carry_rr = evaluate_four_quadrant_gate(
            "short", "commodity", True,
            daily_alignment=aligned_short,
            rr_ratio=3.0,
            carry_adjusted_rr_ratio=-0.6986,
            min_rr=2.5,
            total_quantity=1,
        )
        self.assertEqual(g_carry_rr["decision"], "watchlist_poor_carry_adjusted_rr")
        self.assertFalse(g_carry_rr["actionable"])
        self.assertFalse(g_carry_rr["checks"]["carry_adjusted_rr_ok"])

        # 10) Pair trade requires pair_leg_verified=True (single-leg pair rejected!)
        g_pair_unverified = evaluate_four_quadrant_gate(
            "long", "us_stock_or_etf", True,
            daily_alignment=aligned_long, rr_ratio=3.0, total_quantity=100,
            is_pair_trade=True, pair_leg_verified=False,
        )
        self.assertEqual(g_pair_unverified["decision"], "watchlist_pair_incomplete")
        self.assertFalse(g_pair_unverified["actionable"])

        g_pair_ok = evaluate_four_quadrant_gate(
            "long", "us_stock_or_etf", True,
            daily_alignment=aligned_long, rr_ratio=3.0, total_quantity=100,
            is_pair_trade=True, pair_leg_verified=True,
        )
        self.assertEqual(g_pair_ok["decision"], "pair_trade")
        self.assertTrue(g_pair_ok["actionable"])

        # 11) All pass -> actionable_long / actionable_short
        g7 = evaluate_four_quadrant_gate(
            "short", "cn_future", True,
            daily_alignment=aligned_short,
            rr_ratio=3.2,
            total_quantity=2,
            within_capital_limits=True,
        )
        self.assertEqual(g7["decision"], "actionable_short")
        self.assertTrue(g7["actionable"])


class TestPositionPlanner(unittest.TestCase):
    def test_cn_future_sizing_30m_vs_daily_noise_savings_and_tranches(self):
        # CU0 short: mult=5, entry=95000, stop30m=96200 (dist=1200 -> 6000 CNY/lot), daily=98500 (dist=3500)
        # Equity=1,000,000 CNY, risk_pct=1.0 -> budget=10,000 CNY -> floor(10000/6000) = 1 lot
        plan1 = calculate_position_plan(
            symbol="CU0",
            direction="short",
            entry_price=95000.0,
            tactical_stop_30m=96200.0,
            structural_stop_daily=98500.0,
            target_price=89000.0,
            account_equity=1_000_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
            atr14_30m=450.0,
            atr14_daily=1800.0,
            horizon_carry_pct=-3.0,
        )
        self.assertEqual(plan1["total_quantity"], 1)
        self.assertFalse(plan1["oversized_single_lot"])
        self.assertEqual(plan1["actual_risk_amount"], 6000.0)
        self.assertEqual(plan1["daily_structural_risk_amount"], 17500.0)
        self.assertEqual(plan1["noise_risk_saved_amount"], 11500.0)
        self.assertEqual(plan1["rr_ratio"], 5.0)
        self.assertAlmostEqual(
            plan1["carry_adjusted_rr_ratio"],
            (plan1["expected_move_pct"] - 3.0) / plan1["stop_distance_30m_pct"],
            places=3,
        )
        self.assertEqual(sum(t["quantity"] for t in plan1["tranches"]), plan1["total_quantity"])
        self.assertIn("严禁浮亏摊平", plan1["tranches"][1]["rule"])
        self.assertIn("严禁浮亏摊平", plan1["tranches"][2]["rule"])

        # Larger budget (30,000 -> 5 lots) splits cleanly across (0.4, 0.4, 0.2) -> [2, 2, 1]
        plan5 = calculate_position_plan(
            symbol="CU2703",
            direction="short",
            entry_price=95000.0,
            tactical_stop_30m=96200.0,
            account_equity=3_000_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
        )
        self.assertEqual(plan5["total_quantity"], 5)
        self.assertEqual([t["quantity"] for t in plan5["tranches"]], [2, 2, 1])

    def test_joint_capital_margin_leverage_cap_and_star_tranches(self):
        # Critical #4: Equity $10,000, stock entry $100, stop $99.9 -> risk says 1,000 shares ($100k notional),
        # cash/leverage cap says 100 shares ($10k notional) -> MUST cap at 100 shares!
        capped = calculate_position_plan(
            symbol="NVDA",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=99.9,
            target_price=105.0,
            account_equity=10_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
        )
        self.assertEqual(capped["total_quantity"], 100)
        self.assertEqual(capped["notional_value_account"], 10_000.0)
        self.assertEqual(capped["estimated_margin_account"], 10_000.0)
        self.assertTrue(capped["within_capital_limits"])
        self.assertIn(capped["sizing_binding_constraint"], {"margin_cap", "leverage_cap"})
        self.assertEqual(sum(t["quantity"] for t in capped["tranches"]), 100)

        # Explicit max_leverage on margin futures binds before margin cap
        fut_lev = calculate_position_plan(
            symbol="CL=F",
            direction="long",
            entry_price=70.0,
            tactical_stop_30m=69.9,  # $100/lot risk -> risk says 100 lots; notional=$70k/lot, 2x lev on $100k -> 2 lots
            account_equity=100_000.0,
            account_currency="USD",
            risk_budget_pct=1.0,
            max_leverage=2.0,
        )
        self.assertEqual(fut_lev["total_quantity"], 2)
        self.assertEqual(fut_lev["sizing_binding_constraint"], "leverage_cap")
        self.assertTrue(fut_lev["within_capital_limits"])

        # STAR Market 688001 (min_order_qty=200, lot_step=1):
        # 1) 250 shares with (0.4, 0.4, 0.2) -> tranche 1 MUST be >= 200 and every non-zero tranche executable!
        star_250 = calculate_position_plan(
            symbol="688001",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=96.0,  # dist=4; risk budget=1000 CNY -> 250 shares
            account_equity=100_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
            tranche_weights=(0.4, 0.4, 0.2),
        )
        self.assertEqual(star_250["total_quantity"], 250)
        self.assertEqual(star_250["min_order_qty"], 200)
        self.assertEqual(star_250["lot_step"], 1)
        self.assertGreaterEqual(star_250["tranches"][0]["quantity"], 200)
        self.assertEqual(sum(t["quantity"] for t in star_250["tranches"]), 250)
        for t in star_250["tranches"]:
            self.assertTrue(t["quantity"] == 0 or t["quantity"] >= 200)

        # 2) STAR Market raw qty = 150 (< 200 min_order_qty) -> MUST return 0, NEVER 150 or 100!
        star_below_min = calculate_position_plan(
            symbol="688001.SS",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=96.0,
            account_equity=60_000.0,  # risk budget=600 -> 150 shares < 200
            account_currency="CNY",
            risk_budget_pct=1.0,
        )
        self.assertEqual(star_below_min["total_quantity"], 0)
        self.assertTrue(star_below_min["oversized_single_lot"])
        self.assertEqual(star_below_min["sizing_binding_constraint"], "oversized_single_lot")

        # 3) STAR Market 450 shares -> 2 executable tranches [225, 225, 0], sum=450
        star_450 = calculate_position_plan(
            symbol="688001",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=96.0,
            account_equity=180_000.0,  # risk budget=1800 -> 450 shares
            account_currency="CNY",
            risk_budget_pct=1.0,
            tranche_weights=(0.4, 0.4, 0.2),
        )
        self.assertEqual(star_450["total_quantity"], 450)
        self.assertEqual([t["quantity"] for t in star_450["tranches"]], [225, 225, 0])

    def test_cross_currency_fx_protection(self):
        # USDJPY=X (quote=JPY) in USD account without fx_to_account MUST fail closed
        with self.assertRaisesRegex(ValueError, "Cross-currency trade"):
            calculate_position_plan(
                symbol="USDJPY=X",
                direction="short",
                entry_price=155.0,
                tactical_stop_30m=157.0,
                account_equity=100_000.0,
                account_currency="USD",
            )
        # With explicit fx_to_account (JPY -> USD = 1/155) succeeds cleanly
        jpy_ok = calculate_position_plan(
            symbol="USDJPY=X",
            direction="short",
            entry_price=155.0,
            tactical_stop_30m=157.0,
            account_equity=100_000.0,
            account_currency="USD",
            fx_to_account=1.0 / 155.0,
        )
        self.assertEqual(jpy_ok["quote_currency"], "JPY")
        self.assertGreater(jpy_ok["total_quantity"], 0)

        # US stock in CNY account without fx_to_account MUST fail closed
        with self.assertRaisesRegex(ValueError, "Cross-currency trade"):
            calculate_position_plan(
                symbol="NVDA",
                direction="long",
                entry_price=100.0,
                tactical_stop_30m=95.0,
                account_equity=1_000_000.0,
                account_currency="CNY",
            )

        # CN future in default USD account without fx_to_account MUST fail closed
        with self.assertRaisesRegex(ValueError, "Cross-currency trade"):
            calculate_position_plan(
                symbol="CU0",
                direction="short",
                entry_price=95000.0,
                tactical_stop_30m=96200.0,
                account_equity=1_000_000.0,
            )

    def test_oversized_single_lot_never_rounds_up_and_multi_asset_rounding(self):
        # Single GC=F contract with $150 stop = $15,000 risk > $10,000 budget -> MUST return 0, NOT 1!
        over = calculate_position_plan(
            symbol="GC=F",
            direction="long",
            entry_price=2800.0,
            tactical_stop_30m=2650.0,
            account_equity=1_000_000.0,
            risk_budget_pct=1.0,
        )
        self.assertTrue(over["oversized_single_lot"])
        self.assertEqual(over["total_quantity"], 0)
        self.assertEqual(over["actual_risk_amount"], 0.0)
        self.assertEqual(over["single_lot_risk_amount"], 15000.0)
        self.assertTrue(len(over["warnings"]) >= 1)

        # Cents-quoted CBOT Corn ZC=F (multiplier=50): entry=450, stop=430 (20c -> $1,000/contract)
        corn = calculate_position_plan(
            symbol="ZC=F",
            direction="long",
            entry_price=450.0,
            tactical_stop_30m=430.0,
            target_price=520.0,
            account_equity=1_000_000.0,
            risk_budget_pct=1.0,
        )
        self.assertEqual(corn["total_quantity"], 10)
        self.assertEqual(corn["actual_risk_amount"], 10000.0)
        self.assertEqual(corn["notional_value_account"], 450.0 * 50.0 * 10)

        # A-share 100-share lot rounding: entry=100, stop=94 (dist=6 -> 600 CNY/lot), budget=10,000 -> 16 lots = 1600 shares
        ashare = calculate_position_plan(
            symbol="600519",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=94.0,
            account_equity=1_000_000.0,
            account_currency="CNY",
            risk_budget_pct=1.0,
        )
        self.assertEqual(ashare["total_quantity"], 1600)
        self.assertEqual(ashare["total_quantity"] % 100, 0)
        self.assertEqual(sum(t["quantity"] for t in ashare["tranches"]), 1600)
        for t in ashare["tranches"]:
            self.assertEqual(t["quantity"] % 100, 0)

        # Crypto fractional lot_size=0.001 exact sum
        btc = calculate_position_plan(
            symbol="BTC",
            direction="long",
            entry_price=90000.0,
            tactical_stop_30m=87300.0,  # dist=2700 -> risk/0.001 = $2.70; 10000/2.7 = 3703 lots -> 3.703 BTC
            account_equity=1_000_000.0,
            risk_budget_pct=1.0,
        )
        self.assertEqual(btc["total_quantity"], 3.703)
        tranche_sum = sum(Decimal(str(t["quantity"])) for t in btc["tranches"])
        self.assertEqual(float(tranche_sum), btc["total_quantity"])

        # Unknown future without explicit multiplier MUST raise ValueError
        with self.assertRaises(ValueError):
            calculate_position_plan(
                symbol="UNKNOWN=F",
                direction="long",
                entry_price=100.0,
                tactical_stop_30m=95.0,
            )
        # With explicit multiplier succeeds
        custom = calculate_position_plan(
            symbol="UNKNOWN=F",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            multiplier=100.0,
            account_equity=1_000_000.0,
            risk_budget_pct=1.0,
        )
        self.assertEqual(custom["total_quantity"], 20)

    def test_planner_validation_and_cli(self):
        # Invalid stop side
        with self.assertRaises(ValueError):
            calculate_position_plan("NVDA", "long", entry_price=100.0, tactical_stop_30m=105.0)
        with self.assertRaises(ValueError):
            calculate_position_plan("CU0", "short", entry_price=95000.0, tactical_stop_30m=94000.0, account_currency="CNY")
        # Non-finite / bad tranche sum (including tightened 1e-6 tolerance!)
        with self.assertRaises(ValueError):
            calculate_position_plan("NVDA", "long", entry_price=float("nan"), tactical_stop_30m=95.0)
        with self.assertRaises(ValueError):
            calculate_position_plan("NVDA", "long", entry_price=100.0, tactical_stop_30m=95.0, tranche_weights=(0.5, 0.3))
        with self.assertRaises(ValueError):
            calculate_position_plan("NVDA", "long", entry_price=100.0, tactical_stop_30m=95.0, tranche_weights=(0.4, 0.4005, 0.2))

        # CLI cross-currency without --fx-to-account fails cleanly with exit code 2
        err_fx = io.StringIO()
        with redirect_stderr(err_fx):
            rc_fx = planner_main([
                "--symbol", "CU0", "--direction", "short",
                "--entry", "95000", "--stop", "96200", "--target", "89000",
            ])
        self.assertEqual(rc_fx, 2)
        self.assertIn("Cross-currency", err_fx.getvalue())

        # CLI default --has-l1-l2 is False (fail-closed!)
        buf_def = io.StringIO()
        with redirect_stdout(buf_def):
            rc_def = planner_main([
                "--symbol", "CU0",
                "--direction", "short",
                "--entry", "95000",
                "--stop", "96200",
                "--target", "89000",
                "--equity", "1000000",
                "--account-currency", "CNY",
                "--alignment-status", "aligned_trigger",
            ])
        self.assertEqual(rc_def, 0)
        payload_def = json.loads(buf_def.getvalue())
        self.assertEqual(payload_def["four_quadrant_gate"]["decision"], "watchlist")
        self.assertFalse(payload_def["four_quadrant_gate"]["actionable"])

        # CLI explicit --has-l1-l2 + --alignment-status aligned_trigger passes
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = planner_main([
                "--symbol", "CU0",
                "--direction", "short",
                "--entry", "95000",
                "--stop", "96200",
                "--daily-stop", "98500",
                "--target", "89000",
                "--equity", "1000000",
                "--account-currency", "CNY",
                "--risk-pct", "1.0",
                "--curve-structure", "Backwardation",
                "--roll-yield", "12.0",
                "--has-l1-l2",
                "--alignment-status", "aligned_trigger",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["four_quadrant_gate"]["decision"], "actionable_short")
        self.assertFalse(payload["expression_and_carry"]["vetoed"])
        self.assertEqual(payload["expression_and_carry"]["divergence_regime"], "strong_reality_weak_expectation")
        self.assertEqual(payload["position_plan"]["total_quantity"], 1)
        self.assertIsNotNone(payload["position_plan"]["carry_adjusted_rr_ratio"])

        # Mocked --live mode with intraday_30m + term_structure + CLI roll-yield override
        now_utc = datetime.now(timezone.utc)
        fresh_daily_bar = now_utc.strftime("%Y-%m-%d")
        fresh_intra_bar = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S+00:00")
        fake_live = {
            "snapshot": {
                "technicals": {"last_close": 95000.0, "atr14": 1800.0, "as_of_bar": fresh_daily_bar},
                "intraday_30m": {
                    "technicals": {
                        "atr14": 420.0,
                        "as_of_bar": fresh_intra_bar,
                        "as_of_bar_utc": fresh_intra_bar,
                        "bar_is_closed": True,
                    }
                },
                "chanlun": {
                    "daily_30m_alignment": {
                        "alignment_status": "daily_ok_wait_30m",
                        "intraday_tactical_stop": 96200.0,
                    }
                },
            },
            "research": {
                "fundamentals_and_supply": {
                    "term_structure": {
                        "structure": "Backwardation",
                        "annualized_roll_yield_curve_pct": 10.0,
                        "front_contract": "CU2612",
                        "back_contract": "CU2706",
                    }
                }
            },
            "errors": [],
        }
        import trade_planner as tp

        live_buf = io.StringIO()
        with patch.object(tp, "_fetch_live_context", return_value=fake_live), redirect_stdout(live_buf):
            rc_live = planner_main([
                "--symbol", "CU0",
                "--direction", "short",
                "--target", "89000",
                "--equity", "1000000",
                "--account-currency", "CNY",
                "--roll-yield", "15.0",
                "--has-l1-l2",
                "--live",
            ])
        self.assertEqual(rc_live, 0)
        live_payload = json.loads(live_buf.getvalue())
        self.assertEqual(live_payload["four_quadrant_gate"]["decision"], "watchlist_wait_30m")
        self.assertEqual(live_payload["expression_and_carry"]["annualized_carry_pct"], -15.0)
        self.assertEqual(live_payload["position_plan"]["entry_price"], 95000.0)
        self.assertEqual(live_payload["position_plan"]["tactical_stop_30m"], 96200.0)
        self.assertEqual(live_payload["stop_source"], "live_30m_chanlun")
        self.assertEqual(live_payload["stop_timeframe"], "30m")
        self.assertIsNotNone(live_payload["position_plan"]["stop_vs_atr30m"])

        # --live with manual --stop or --daily-stop MUST be rejected by CLI argparse (exit 2)
        for bad_flag, bad_val in (("--stop", "96200"), ("--daily-stop", "99000")):
            err_stop = io.StringIO()
            with redirect_stderr(err_stop), self.assertRaises(SystemExit) as cm_stop:
                planner_main([
                    "--symbol", "CU0", "--direction", "short", "--live",
                    bad_flag, bad_val,
                ])
            self.assertEqual(cm_stop.exception.code, 2)
            self.assertIn("live mode strictly enforces live 30m Chanlun stops", err_stop.getvalue())

        # Real Worker-1 dual snapshot shape: intraday_technicals + chanlun.dual_alignment + auto-filled --stop
        w1_live_ctx = {
            "snapshot": {
                "technicals": {
                    "last_close": 92400.0,
                    "atr14": 2131.0,
                    "as_of_bar": fresh_daily_bar,
                },
                "intraday_technicals": {
                    "last_close": 91900.0,
                    "atr14": 480.0,
                    "as_of_bar": fresh_intra_bar,
                    "as_of_bar_utc": fresh_intra_bar,
                    "bar_is_closed": True,
                },
                "chanlun": {
                    "dual_alignment": {
                        "alignment_status": "aligned_trigger",
                        "daily_bias": "short",
                        "intraday_action": "entry_short",
                        "intraday_tactical_stop": 93100.0,
                        "daily_tactical_stop": 98500.0,
                    }
                },
            },
            "research": {
                "sources_status": {
                    "market_snapshot": {"status": "ok"},
                    "term_structure": {"status": "ok"},
                },
                "fundamentals_and_supply": {
                    "term_structure": {
                        "status": "ok",
                        "structure": "Backwardation",
                        "annualized_roll_yield_curve_pct": 12.0,
                        "annualized_roll_yield_near_pct": 12.0,
                    }
                },
            },
            "errors": [],
        }
        buf3 = io.StringIO()
        with patch.object(tp, "_fetch_live_context", return_value=w1_live_ctx), redirect_stdout(buf3):
            rc3 = tp.main([
                "--symbol", "CU0", "--direction", "short", "--live",
                "--target", "86000", "--equity", "1000000", "--account-currency", "CNY",
                "--risk-pct", "1.0", "--confirm-l1-l2",
            ])
        self.assertEqual(rc3, 0)
        w1_payload = json.loads(buf3.getvalue())
        self.assertEqual(w1_payload["position_plan"]["entry_price"], 91900.0)
        self.assertEqual(w1_payload["position_plan"]["tactical_stop_30m"], 93100.0)
        self.assertEqual(w1_payload["position_plan"]["structural_stop_daily"], 98500.0)
        self.assertEqual(w1_payload["four_quadrant_gate"]["decision"], "actionable_short")
        self.assertEqual(w1_payload["four_quadrant_gate"]["checks"]["daily_alignment_status"], "aligned_trigger")

        # Survival Rule #1: steep near-month squeeze ONLY (curve=12%, near=30%) MUST NOT veto a far-month short;
        # it emits near_carry_stress_rr_ratio + squeeze warning while full-curve steep Back (curve=30%) blocks!
        w1_near_squeeze = json.loads(json.dumps(w1_live_ctx))
        w1_near_squeeze["research"]["fundamentals_and_supply"]["term_structure"]["annualized_roll_yield_near_pct"] = 30.0
        buf_near = io.StringIO()
        with patch.object(tp, "_fetch_live_context", return_value=w1_near_squeeze), redirect_stdout(buf_near):
            rc_near = tp.main([
                "--symbol", "CU0", "--direction", "short", "--live",
                "--target", "86000", "--equity", "1000000", "--account-currency", "CNY",
                "--risk-pct", "1.0", "--confirm-l1-l2",
            ])
        self.assertEqual(rc_near, 0)
        near_payload = json.loads(buf_near.getvalue())
        self.assertEqual(near_payload["four_quadrant_gate"]["decision"], "actionable_short")
        self.assertTrue(near_payload["four_quadrant_gate"]["actionable"])
        self.assertLess(near_payload["expression_and_carry"]["near_carry_stress_rr_ratio"], 2.5)
        self.assertEqual(
            near_payload["expression_and_carry"]["expression_recommendation"],
            "prefer_far_month_short_or_bear_spread",
        )

        # Steep FULL-CURVE Backwardation (curve=30%) pushing curve carry-adjusted R:R below min_rr blocks!
        w1_steep_carry = json.loads(json.dumps(w1_live_ctx))
        w1_steep_carry["research"]["fundamentals_and_supply"]["term_structure"]["annualized_roll_yield_curve_pct"] = 30.0
        w1_steep_carry["research"]["fundamentals_and_supply"]["term_structure"]["annualized_roll_yield_near_pct"] = 30.0
        buf_steep = io.StringIO()
        with patch.object(tp, "_fetch_live_context", return_value=w1_steep_carry), redirect_stdout(buf_steep):
            rc_steep = tp.main([
                "--symbol", "CU0", "--direction", "short", "--live",
                "--target", "86000", "--equity", "1000000", "--account-currency", "CNY",
                "--risk-pct", "1.0", "--confirm-l1-l2",
            ])
        self.assertEqual(rc_steep, 0)
        steep_payload = json.loads(buf_steep.getvalue())
        self.assertEqual(steep_payload["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")
        self.assertFalse(steep_payload["four_quadrant_gate"]["actionable"])

        # Failed --live fetch (snapshot intraday_error + stale term_structure) MUST NEVER produce actionable_*
        bad_live_ctx = {
            "snapshot": {
                "intraday_error": "30m feed unavailable",
                "technicals": {"last_close": 92400.0, "as_of_bar": fresh_daily_bar},
                "chanlun": {
                    "dual_alignment": {
                        "alignment_status": "aligned_trigger",
                        "daily_bias": "short",
                        "intraday_action": "entry_short",
                        "intraday_tactical_stop": 93100.0,
                    }
                },
            },
            "research": {
                "fundamentals_and_supply": {
                    "term_structure": {"status": "stale", "warning": "cached 48h ago"}
                }
            },
            "errors": ["research_data_partial_failure"],
        }
        buf_bad = io.StringIO()
        with patch.object(tp, "_fetch_live_context", return_value=bad_live_ctx), redirect_stdout(buf_bad):
            rc_bad = tp.main([
                "--symbol", "CU0", "--direction", "short", "--live",
                "--target", "86000", "--equity", "1000000", "--account-currency", "CNY",
                "--has-l1-l2",
            ])
        self.assertEqual(rc_bad, 0)
        bad_payload = json.loads(buf_bad.getvalue())
        self.assertEqual(bad_payload["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(bad_payload["four_quadrant_gate"]["actionable"])
        self.assertTrue(len(bad_payload.get("live_warnings") or []) >= 2)
        self.assertIsNotNone(bad_payload["expression_and_carry"]["curve_data_warning"])
        self.assertEqual(bad_payload["expression_and_carry"]["expression_recommendation"], "data_stale_refresh_required")

        # --live with stale_fallback cached 30m bars MUST NOT recompute alignment or derive cached entry/stop
        cached_fb_ctx = {
            "snapshot": {
                "symbol": "CU0",
                "asset_class": "cn_future",
                "intraday_error": "Read timed out",
                "intraday_stale_fallback": True,
                "technicals": {"last_close": 92400.0, "as_of_bar": fresh_daily_bar},
                "intraday_technicals": {"last_close": 103.0, "as_of_bar": "2026-11-20T15:00+08:00"},
                "chanlun": {
                    "daily": {
                        "latest_zhongshu": {"zd": 90000.0, "zg": 93000.0},
                        "signals": [],
                        "momentum_assessment": {"verdict": "neutral_extension_down"},
                    },
                    "intraday": {
                        "latest_zhongshu": {"zd": 100.0, "zg": 105.0},
                        "signals": [],
                        "momentum_assessment": {"verdict": "neutral_extension_up"},
                    },
                },
            },
            "research": {"fundamentals_and_supply": {}},
            "errors": [],
        }
        with patch.object(tp, "_fetch_live_context", return_value=cached_fb_ctx):
            parser = tp.build_parser()
            args_fb = parser.parse_args([
                "--symbol", "CU0", "--direction", "short", "--live",
                "--target", "86000", "--equity", "1000000", "--account-currency", "CNY",
                "--has-l1-l2",
            ])
            with self.assertRaises(tp.PlanInputError) as ctx_fb:
                tp.build_trade_plan(args_fb)
            self.assertIn("no valid intraday_tactical_stop", str(ctx_fb.exception))

    def test_round2_planner_live_guards_carry_gate_and_decimal_tranches(self):
        import trade_planner as tp

        # 1) BTC 10.001 exact Decimal tranche sum (no IEEE-754 drift vs total_quantity) & exact steps sum
        btc_plan = calculate_position_plan(
            symbol="BTC",
            direction="long",
            entry_price=100_000.0,
            tactical_stop_30m=99_000.0,  # $1,000/BTC risk
            target_price=105_000.0,
            account_equity=1_000_100.0,  # 1% risk budget = $10,001 -> 10.001 BTC (10,001 steps of 0.001)
            account_currency="USD",
            risk_budget_pct=1.0,
            tranche_weights=(0.4, 0.4, 0.2),
        )
        self.assertEqual(btc_plan["total_lots"], 10001)
        self.assertEqual(btc_plan["total_quantity"], 10.001)
        self.assertEqual(btc_plan["total_quantity_str"], "10.001")
        self.assertEqual(btc_plan["tranche_steps_sum"], btc_plan["total_lots"])
        self.assertTrue(btc_plan["tranche_qty_sum_exact"])
        self.assertEqual(
            sum(Decimal(str(t["quantity"])) for t in btc_plan["tranches"]),
            Decimal("10.001"),
        )
        self.assertEqual(
            sum(Decimal(t["quantity_str"]) for t in btc_plan["tranches"]),
            Decimal("10.001"),
        )

        # 2) CLI CL=F short raw R:R 3.0 + steep Backwardation carry-adjusted R:R -0.6986 -> blocked!
        buf_cl = io.StringIO()
        with redirect_stdout(buf_cl):
            rc_cl = tp.main([
                "--symbol", "CL=F",
                "--direction", "short",
                "--entry", "100",
                "--stop", "101",
                "--target", "97",
                "--equity", "1000000",
                "--account-currency", "USD",
                "--risk-pct", "1.0",
                "--curve-structure", "Backwardation",
                "--roll-yield", "15.0",
                "--horizon-days", "90",
                "--has-l1-l2",
                "--alignment-status", "aligned_trigger",
            ])
        self.assertEqual(rc_cl, 0)
        cl_payload = json.loads(buf_cl.getvalue())
        self.assertEqual(cl_payload["position_plan"]["rr_ratio"], 3.0)
        self.assertAlmostEqual(cl_payload["expression_and_carry"]["carry_adjusted_rr_ratio"], -0.6986, places=4)
        self.assertEqual(cl_payload["four_quadrant_gate"]["decision"], "watchlist_poor_carry_adjusted_rr")
        self.assertFalse(cl_payload["four_quadrant_gate"]["actionable"])

        # 3) CLI --live + --alignment-status MUST be rejected by argparse!
        err_override = io.StringIO()
        with redirect_stderr(err_override):
            with self.assertRaises(SystemExit) as cm_exit:
                tp.main([
                    "--symbol", "CU0", "--direction", "short", "--live",
                    "--alignment-status", "aligned_trigger",
                ])
        self.assertEqual(cm_exit.exception.code, 2)
        self.assertIn("--alignment-status cannot override live dual alignment in --live mode", err_override.getvalue())

        # Helper to build a clean fresh live context for targeted fault injection
        now_utc = datetime.now(timezone.utc)
        fresh_daily = now_utc.strftime("%Y-%m-%d")
        fresh_intra = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S+00:00")

        def _base_live_ctx() -> dict:
            return {
                "snapshot": {
                    "asset_class": "cn_future",
                    "technicals": {"last_close": 92000.0, "atr14": 1800.0, "as_of_bar": fresh_daily},
                    "intraday_technicals": {
                        "last_close": 92000.0,
                        "atr14": 450.0,
                        "as_of_bar": fresh_intra,
                        "as_of_bar_utc": fresh_intra,
                        "bar_is_closed": True,
                    },
                    "chanlun": {
                        "dual_alignment": {
                            "alignment_status": "aligned_trigger",
                            "daily_bias": "short",
                            "intraday_action": "entry_short",
                            "intraday_tactical_stop": 93000.0,
                            "daily_tactical_stop": 97000.0,
                        }
                    },
                },
                "research": {
                    "sources_status": {
                        "market_snapshot": {"status": "ok"},
                        "term_structure": {"status": "ok"},
                    },
                    "fundamentals_and_supply": {
                        "term_structure": {
                            "status": "ok",
                            "structure": "Contango",
                            "annualized_roll_yield_curve_pct": -5.0,
                            "annualized_roll_yield_near_pct": -5.0,
                        }
                    },
                },
                "errors": [],
            }

        def _run_live(ctx: dict, symbol: str = "CU0", target: str = "86000", ccy: str = "CNY") -> dict:
            out_s = io.StringIO()
            with patch.object(tp, "_fetch_live_context", return_value=ctx), redirect_stdout(out_s):
                rc_x = tp.main([
                    "--symbol", symbol, "--direction", "short", "--live",
                    "--target", target, "--equity", "1000000", "--account-currency", ccy,
                    "--risk-pct", "1.0", "--has-l1-l2",
                ])
            self.assertEqual(rc_x, 0)
            return json.loads(out_s.getvalue())

        # Control: clean fresh live context passes as actionable_short
        ok_live = _run_live(_base_live_ctx())
        self.assertEqual(ok_live["four_quadrant_gate"]["decision"], "actionable_short")
        self.assertTrue(ok_live["four_quadrant_gate"]["actionable"])

        # 4) --live with bar_is_closed=False -> intraday_bar_unclosed -> blocked!
        unclosed_ctx = _base_live_ctx()
        unclosed_ctx["snapshot"]["intraday_technicals"]["bar_is_closed"] = False
        unclosed_res = _run_live(unclosed_ctx)
        self.assertEqual(unclosed_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(unclosed_res["four_quadrant_gate"]["actionable"])
        self.assertIn("intraday_bar_unclosed", unclosed_res["live_warnings"])

        # 5) --live with stale daily snapshot (> 4 calendar days) -> stale_market_snapshot -> blocked!
        stale_daily_ctx = _base_live_ctx()
        stale_daily_ctx["snapshot"]["technicals"]["as_of_bar"] = (now_utc - timedelta(days=6)).strftime("%Y-%m-%d")
        stale_daily_res = _run_live(stale_daily_ctx)
        self.assertEqual(stale_daily_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(stale_daily_res["four_quadrant_gate"]["actionable"])
        self.assertIn("stale_market_snapshot", stale_daily_res["live_warnings"])

        # 6) --live with date-only intraday as_of_bar string -> stale_market_snapshot -> blocked!
        date_only_intra_ctx = _base_live_ctx()
        date_only_intra_ctx["snapshot"]["intraday_technicals"]["as_of_bar"] = fresh_daily
        date_only_intra_ctx["snapshot"]["intraday_technicals"].pop("as_of_bar_utc", None)
        date_only_res = _run_live(date_only_intra_ctx)
        self.assertEqual(date_only_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(date_only_res["four_quadrant_gate"]["actionable"])
        self.assertIn("stale_market_snapshot", date_only_res["live_warnings"])

        # 7) --live with stale crypto snapshot (> 1 day) -> stale_market_snapshot -> blocked!
        stale_btc_ctx = _base_live_ctx()
        stale_btc_ctx["snapshot"]["asset_class"] = "crypto"
        stale_btc_ctx["snapshot"]["technicals"]["as_of_bar"] = (now_utc - timedelta(days=2)).strftime("%Y-%m-%d")
        stale_btc_ctx["snapshot"]["intraday_technicals"]["as_of_bar"] = (now_utc - timedelta(hours=30)).strftime(
            "%Y-%m-%d %H:%M:%S+00:00"
        )
        stale_btc_ctx["snapshot"]["intraday_technicals"]["as_of_bar_utc"] = (
            stale_btc_ctx["snapshot"]["intraday_technicals"]["as_of_bar"]
        )
        stale_btc_res = _run_live(stale_btc_ctx, symbol="BTC", target="80000", ccy="USD")
        self.assertEqual(stale_btc_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(stale_btc_res["four_quadrant_gate"]["actionable"])
        self.assertIn("stale_market_snapshot", stale_btc_res["live_warnings"])

        # 8) --live with sources_status.market_snapshot.status == "error" (and string "error") -> blocked!
        ss_err_ctx = _base_live_ctx()
        ss_err_ctx["research"]["sources_status"]["market_snapshot"] = {"status": "error", "error": "yf 503"}
        ss_err_res = _run_live(ss_err_ctx)
        self.assertEqual(ss_err_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(ss_err_res["four_quadrant_gate"]["actionable"])
        self.assertTrue(any("market_snapshot_error" in w for w in ss_err_res["live_warnings"]))

        ss_str_ctx = _base_live_ctx()
        ss_str_ctx["research"]["sources_status"]["term_structure"] = "stale"
        ss_str_res = _run_live(ss_str_ctx)
        self.assertEqual(ss_str_res["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(ss_str_res["four_quadrant_gate"]["actionable"])
        self.assertEqual(ss_str_res["expression_and_carry"]["expression_recommendation"], "data_stale_refresh_required")

    def test_round3_live_30m_freshness_timestamps_cn_futures_guard_and_equity_margin(self):
        import trade_planner as tp

        now_utc = datetime.now(timezone.utc)
        fresh_daily = now_utc.strftime("%Y-%m-%d")
        fresh_intra = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S+00:00")

        def _base_live_ctx() -> dict:
            return {
                "snapshot": {
                    "asset_class": "cn_future",
                    "technicals": {"last_close": 92000.0, "atr14": 1800.0, "as_of_bar": fresh_daily},
                    "intraday_technicals": {
                        "last_close": 92000.0,
                        "atr14": 450.0,
                        "as_of_bar": fresh_intra,
                        "as_of_bar_utc": fresh_intra,
                        "bar_is_closed": True,
                    },
                    "chanlun": {
                        "dual_alignment": {
                            "alignment_status": "aligned_trigger",
                            "daily_bias": "short",
                            "intraday_action": "entry_short",
                            "intraday_tactical_stop": 93000.0,
                            "daily_tactical_stop": 97000.0,
                        }
                    },
                },
                "research": {
                    "sources_status": {
                        "market_snapshot": {"status": "ok"},
                        "term_structure": {"status": "ok"},
                    },
                    "fundamentals_and_supply": {
                        "term_structure": {
                            "status": "ok",
                            "structure": "Contango",
                            "annualized_roll_yield_curve_pct": -5.0,
                            "annualized_roll_yield_near_pct": -5.0,
                        }
                    },
                },
                "errors": [],
            }

        def _run_live(ctx: dict, symbol: str = "CU0", target: str = "86000", ccy: str = "CNY") -> dict:
            out_s = io.StringIO()
            with patch.object(tp, "_fetch_live_context", return_value=ctx), redirect_stdout(out_s):
                rc_x = tp.main([
                    "--symbol", symbol, "--direction", "short", "--live",
                    "--target", target, "--equity", "1000000", "--account-currency", ccy,
                    "--risk-pct", "1.0", "--has-l1-l2",
                ])
            self.assertEqual(rc_x, 0)
            return json.loads(out_s.getvalue())

        # 1a) --live with missing 30m timestamp -> missing_intraday_timestamp -> blocked!
        missing_intra_ctx = _base_live_ctx()
        missing_intra_ctx["snapshot"]["intraday_technicals"].pop("as_of_bar", None)
        missing_intra_ctx["snapshot"]["intraday_technicals"].pop("as_of_bar_utc", None)
        res_missing_intra = _run_live(missing_intra_ctx)
        self.assertEqual(res_missing_intra["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(res_missing_intra["four_quadrant_gate"]["actionable"])
        self.assertIn("missing_intraday_timestamp", res_missing_intra["four_quadrant_gate"]["checks"]["upstream_errors"])

        # 1b) --live with missing daily timestamp -> missing_daily_timestamp -> blocked!
        missing_daily_ctx = _base_live_ctx()
        missing_daily_ctx["snapshot"]["technicals"].pop("as_of_bar", None)
        missing_daily_ctx["snapshot"]["technicals"].pop("as_of_bar_utc", None)
        res_missing_daily = _run_live(missing_daily_ctx)
        self.assertEqual(res_missing_daily["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(res_missing_daily["four_quadrant_gate"]["actionable"])
        self.assertIn("missing_daily_timestamp", res_missing_daily["four_quadrant_gate"]["checks"]["upstream_errors"])

        # 1c) --live with future 30m timestamp (+1h, even with bar_is_closed=True) -> intraday_future_timestamp -> blocked!
        future_intra_str = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S+00:00")
        future_intra_ctx = _base_live_ctx()
        future_intra_ctx["snapshot"]["intraday_technicals"]["as_of_bar"] = future_intra_str
        future_intra_ctx["snapshot"]["intraday_technicals"]["as_of_bar_utc"] = future_intra_str
        future_intra_ctx["snapshot"]["intraday_technicals"]["bar_is_closed"] = True
        res_future_intra = _run_live(future_intra_ctx)
        self.assertEqual(res_future_intra["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(res_future_intra["four_quadrant_gate"]["actionable"])
        self.assertIn("intraday_future_timestamp", res_future_intra["four_quadrant_gate"]["checks"]["upstream_errors"])

        # 1d) --live with future daily timestamp (+3 days) -> daily_future_timestamp -> blocked!
        future_daily_ctx = _base_live_ctx()
        future_daily_ctx["snapshot"]["technicals"]["as_of_bar"] = (now_utc + timedelta(days=3)).strftime("%Y-%m-%d")
        res_future_daily = _run_live(future_daily_ctx)
        self.assertEqual(res_future_daily["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(res_future_daily["four_quadrant_gate"]["actionable"])
        self.assertIn("daily_future_timestamp", res_future_daily["four_quadrant_gate"]["checks"]["upstream_errors"])

        # 1e) During active CN trading session (Mon 14:30 Shanghai = 06:30 UTC), a 5-hour-old 30m bar
        #     (> 4h threshold) MUST be blocked as stale_intraday_30m_snapshot!
        in_session_cn_utc = datetime(2026, 11, 23, 6, 30, tzinfo=timezone.utc)  # Mon 14:30 Asia/Shanghai
        stale_5h_snap = {
            "technicals": {"as_of_bar": "2026-11-23"},
            "intraday_technicals": {
                "as_of_bar": "2026-11-23T01:30:00+00:00",  # 5h ago (09:30 Shanghai)
                "as_of_bar_utc": "2026-11-23T01:30:00+00:00",
                "bar_is_closed": True,
            },
        }
        errs_in_session, _, _ = tp._check_live_upstream_health(
            stale_5h_snap, {"sources_status": {"market_snapshot": {"status": "ok"}}}, "cn_future",
            now_utc=in_session_cn_utc,
        )
        self.assertIn("stale_intraday_30m_snapshot", errs_in_session)

        # 1e-2) Outside market hours (Saturday morning US / evening after close), the latest closed Friday
        #       30m bar (< 4 calendar days) MUST be allowed for after-hours/weekend trade planning!
        saturday_utc = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)  # Sat 10:00 ET (closed)
        friday_close_snap = {
            "technicals": {"as_of_bar": "2026-11-20"},
            "intraday_technicals": {
                "as_of_bar": "2026-11-20T15:30:00-05:00",  # Fri last 30m bar (~18.5h ago)
                "as_of_bar_utc": "2026-11-20T20:30:00+00:00",
                "bar_is_closed": True,
            },
        }
        errs_weekend, _, _ = tp._check_live_upstream_health(
            friday_close_snap, {"sources_status": {"market_snapshot": {"status": "ok"}}}, "us_stock_or_etf",
            now_utc=saturday_utc,
        )
        self.assertEqual(errs_weekend, [])
        # But > 4 calendar days old even on a weekend MUST still be rejected!
        old_weekend_snap = {
            "technicals": {"as_of_bar": "2026-11-20"},
            "intraday_technicals": {
                "as_of_bar": "2026-11-16T15:30:00-05:00",  # 5 days ago
                "as_of_bar_utc": "2026-11-16T20:30:00+00:00",
                "bar_is_closed": True,
            },
        }
        errs_old_weekend, _, _ = tp._check_live_upstream_health(
            old_weekend_snap, {"sources_status": {"market_snapshot": {"status": "ok"}}}, "us_stock_or_etf",
            now_utc=saturday_utc,
        )
        self.assertIn("stale_intraday_30m_snapshot", errs_old_weekend)

        # 1f) --live with 3-hour-old crypto 30m bar (> 2h threshold for crypto) -> stale_intraday_30m_snapshot -> blocked!
        stale_3h_btc_str = (now_utc - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S+00:00")
        stale_3h_btc_ctx = _base_live_ctx()
        stale_3h_btc_ctx["snapshot"]["asset_class"] = "crypto"
        stale_3h_btc_ctx["snapshot"]["intraday_technicals"]["as_of_bar"] = stale_3h_btc_str
        stale_3h_btc_ctx["snapshot"]["intraday_technicals"]["as_of_bar_utc"] = stale_3h_btc_str
        res_stale_3h_btc = _run_live(stale_3h_btc_ctx, symbol="BTC", target="80000", ccy="USD")
        self.assertEqual(res_stale_3h_btc["four_quadrant_gate"]["decision"], "watchlist_upstream_error")
        self.assertFalse(res_stale_3h_btc["four_quadrant_gate"]["actionable"])
        self.assertIn("stale_intraday_30m_snapshot", res_stale_3h_btc["four_quadrant_gate"]["checks"]["upstream_errors"])

        # 2) CN futures misclassification under asset_class="us_stock_or_etf" MUST fail closed!
        for cn_sym in ("CU2612", "CU0", "AL2701", "TA705"):
            bad_spec = get_contract_spec(cn_sym, "us_stock_or_etf")
            self.assertFalse(bad_spec["known_spec"], cn_sym)
            self.assertIsNone(bad_spec["multiplier"], cn_sym)
            self.assertEqual(bad_spec["asset_class"], "cn_future", cn_sym)
            self.assertEqual(bad_spec["currency"], "CNY", cn_sym)
            self.assertIn("Conflicting asset_class", bad_spec["unit_note"])
            with self.assertRaises(ValueError):
                calculate_position_plan(
                    cn_sym, "short", entry_price=95000.0, tactical_stop_30m=96000.0, asset_class="us_stock_or_etf"
                )
        # Plain US stock tickers colliding with CN roots remain valid us_stock_or_etf
        for us_sym in ("MA", "V", "SI"):
            us_spec = get_contract_spec(us_sym, "us_stock_or_etf")
            self.assertTrue(us_spec["known_spec"], us_sym)
            self.assertEqual(us_spec["asset_class"], "us_stock_or_etf", us_sym)
            self.assertEqual(us_spec["multiplier"], 1.0, us_sym)

        # 3) Cash-equity margin/leverage guard in calculate_position_plan & CLI
        with self.assertRaisesRegex(ValueError, "equity_margin_forbidden"):
            calculate_position_plan(
                symbol="PWR",
                direction="long",
                entry_price=100.0,
                tactical_stop_30m=99.5,
                target_price=105.0,
                account_equity=10_000.0,
                account_currency="USD",
                margin_rate=0.1,
                max_leverage=2.0,
            )
        # CLI without --allow-equity-margin returns exit code 2 with equity_margin_forbidden
        err_margin = io.StringIO()
        with redirect_stderr(err_margin):
            rc_margin_blocked = planner_main([
                "--symbol", "PWR", "--direction", "long",
                "--entry", "100", "--stop", "99.5", "--target", "105",
                "--equity", "10000", "--margin-rate", "0.1", "--max-leverage", "2",
            ])
        self.assertEqual(rc_margin_blocked, 2)
        self.assertIn("equity_margin_forbidden", err_margin.getvalue())

        # CLI WITH explicit --allow-equity-margin succeeds cleanly
        out_margin_ok = io.StringIO()
        with redirect_stdout(out_margin_ok):
            rc_margin_ok = planner_main([
                "--symbol", "PWR", "--direction", "long",
                "--entry", "100", "--stop", "99.5", "--target", "105",
                "--equity", "10000", "--margin-rate", "0.1", "--max-leverage", "2",
                "--allow-equity-margin",
            ])
        self.assertEqual(rc_margin_ok, 0)
        margin_payload = json.loads(out_margin_ok.getvalue())
        self.assertEqual(margin_payload["position_plan"]["total_quantity"], 200)
        self.assertEqual(margin_payload["position_plan"]["notional_value_account"], 20_000.0)
        self.assertEqual(margin_payload["position_plan"]["estimated_margin_account"], 2_000.0)


class TestPortfolioManager(unittest.TestCase):
    def test_single_sector_leader_exclusivity_co_leader_and_max_two(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0, default_risk_per_trade_pct=1.0)

            # Open sole leader NVDA in ai_compute with risk = $5,000 (0.5%)
            r1 = open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="AI Compute",
                entry_price=150.0,
                tactical_stop_30m=140.0,
                quantity=500,  # risk = $5,000
                exit_red_lines=["30m 跌破 140 止损", "云厂商 CapEx 增速转负"],
                portfolio_dir=tmpdir,
            )
            self.assertEqual(r1["status"], "opened")
            self.assertEqual(r1["position"]["current_open_risk_amount"], 5000.0)

            # Same symbol duplicate rejected
            with self.assertRaisesRegex(ValueError, "duplicate_symbol_use_add_tranche"):
                open_position(
                    symbol="NVDA",
                    direction="long",
                    sector_theme="ai compute",
                    entry_price=155.0,
                    tactical_stop_30m=145.0,
                    quantity=200,
                    portfolio_dir=tmpdir,
                )

            # Follower stock in same sector as sole_leader MUST BE REJECTED
            with self.assertRaisesRegex(ValueError, "sector_leader_occupied"):
                open_position(
                    symbol="SMCI",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=50.0,
                    tactical_stop_30m=45.0,
                    quantity=400,
                    leader_role="sole_leader",
                    portfolio_dir=tmpdir,
                )

            # co_leader WITHOUT reason MUST BE REJECTED
            with self.assertRaisesRegex(ValueError, "sector_leader_occupied"):
                open_position(
                    symbol="AVGO",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=200.0,
                    tactical_stop_30m=190.0,
                    quantity=400,
                    leader_role="co_leader",
                    co_leader_reason="   ",
                    portfolio_dir=tmpdir,
                )

            # co_leader WITH reason exceeding shared sector cap ($5,000 + $6,000 > $10,000) MUST BE REJECTED
            acc = load_account(tmpdir)
            pos = load_positions(tmpdir)
            adm_over = check_sector_slot_admission(
                pos, acc, "ai_compute", "AVGO", 6000.0,
                leader_role="co_leader",
                co_leader_reason="GPU与ASIC定制芯片双寡头难分伯仲",
            )
            self.assertFalse(adm_over["allowed"])
            self.assertEqual(adm_over["reason_code"], "co_leader_sector_risk_exceeded")
            self.assertEqual(adm_over["remaining_sector_risk_budget"], 5000.0)

            with self.assertRaisesRegex(ValueError, "co_leader_sector_risk_exceeded"):
                open_position(
                    symbol="AVGO",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=200.0,
                    tactical_stop_30m=190.0,
                    quantity=600,
                    leader_role="co_leader",
                    co_leader_reason="GPU与ASIC定制芯片双寡头难分伯仲",
                    portfolio_dir=tmpdir,
                )

            # co_leader WITH reason fitting shared cap ($5,000 + $5,000 <= $10,000) ALLOWED
            r2 = open_position(
                symbol="AVGO",
                direction="long",
                sector_theme="ai_compute",
                entry_price=200.0,
                tactical_stop_30m=190.0,
                quantity=500,  # risk = $5,000
                leader_role="co_leader",
                co_leader_reason="GPU与ASIC定制芯片双寡头难分伯仲",
                exit_red_lines=["30m 跌破 190 止损"],
                portfolio_dir=tmpdir,
            )
            self.assertEqual(r2["status"], "opened")
            self.assertEqual(r2["admission"]["mode"], "co_leader_shared_slot")

            # 3rd stock in same sector HARD REJECTED even with tiny risk
            with self.assertRaisesRegex(ValueError, "sector_max_two_co_leaders_reached"):
                open_position(
                    symbol="AMD",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=150.0,
                    tactical_stop_30m=149.0,
                    quantity=10,
                    leader_role="co_leader",
                    co_leader_reason="trying 3rd",
                    portfolio_dir=tmpdir,
                )

    def test_atomic_rotate_leader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0)
            r1 = open_position(
                symbol="PWR",
                direction="long",
                sector_theme="power_grid",
                entry_price=300.0,
                tactical_stop_30m=280.0,
                quantity=400,  # risk = $8,000
                exit_red_lines=["30m 跌破 280"],
                portfolio_dir=tmpdir,
            )
            old_id = r1["position"]["position_id"]

            rot = rotate_leader(
                old_position_id=old_id,
                exit_price=340.0,
                exit_reason="GEV 订单增速与燃气轮机定价权成为板块新真龙头，切换不叠加",
                new_position_kwargs={
                    "symbol": "GEV",
                    "direction": "long",
                    "entry_price": 400.0,
                    "tactical_stop_30m": 380.0,
                    "quantity": 450,  # risk = $9,000
                    "exit_red_lines": ["30m 跌破 380"],
                },
                portfolio_dir=tmpdir,
            )
            self.assertEqual(rot["status"], "rotated")
            self.assertEqual(rot["closed_position"]["status"], "closed")
            self.assertEqual(rot["closed_position"]["realized_pnl"], 16000.0)
            self.assertEqual(rot["closed_position"]["r_multiple"], 2.0)
            self.assertEqual(rot["opened_position"]["symbol"], "GEV")
            self.assertEqual(rot["opened_position"]["status"], "open")

            open_pos = load_positions(tmpdir, status="open")
            self.assertEqual(len(open_pos), 1)
            self.assertEqual(open_pos[0]["symbol"], "GEV")

            journal = load_journal(tmpdir)
            rot_entries = [j for j in journal if j["action"] == "rotate_leader"]
            self.assertEqual(len(rot_entries), 2)
            self.assertEqual(rot_entries[0]["rotation_id"], rot_entries[1]["rotation_id"])
            self.assertEqual({rot_entries[0]["leg"], rot_entries[1]["leg"]}, {"close_old_leader", "open_new_leader"})

            # Rotation also succeeds when portfolio total open risk is already at ceiling (replaced risk netted)
            update_account(tmpdir, max_total_open_risk_pct=1.0)  # ceiling = $10,000, current GEV = $9,000
            gev_id = rot["opened_position"]["position_id"]
            rot2 = rotate_leader(
                old_position_id=gev_id,
                exit_price=420.0,
                exit_reason="换回新领涨龙头",
                new_position_kwargs={
                    "symbol": "ETN",
                    "direction": "long",
                    "entry_price": 350.0,
                    "tactical_stop_30m": 330.0,
                    "quantity": 500,  # risk = $10,000 == cap (would fail if old $9,000 wasn't netted)
                    "exit_red_lines": ["30m 跌破 330"],
                },
                portfolio_dir=tmpdir,
            )
            self.assertEqual(rot2["status"], "rotated")
            self.assertEqual(rot2["opened_position"]["symbol"], "ETN")

    def test_add_tranche_forbids_floating_loss_and_trail_stop_frees_risk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0, base_currency="CNY")
            # Short CU0: mult=5, entry=95000, stop=96200, qty=1 -> risk=6,000 CNY
            opened = open_position(
                symbol="CU0",
                direction="short",
                sector_theme="copper",
                entry_price=95000.0,
                tactical_stop_30m=96200.0,
                structural_stop_daily=98500.0,
                quantity=1,
                exit_red_lines=["30m 反抽突破 96200"],
                portfolio_dir=tmpdir,
            )
            pid = opened["position"]["position_id"]

            # Averaging up on floating loss (short add_price >= avg_entry) MUST FAIL
            with self.assertRaisesRegex(ValueError, "floating_loss_add_forbidden"):
                add_position_tranche(pid, add_price=95500.0, add_quantity=1, portfolio_dir=tmpdir)
            with self.assertRaisesRegex(ValueError, "floating_loss_add_forbidden"):
                add_position_tranche(pid, add_price=95000.0, add_quantity=1, portfolio_dir=tmpdir)

            # Profitable add WITHOUT tightening stop exceeds 10,000 sector cap (avg=94000, stop=96200, qty=2 -> 22,000)
            with self.assertRaisesRegex(ValueError, "add_tranche_sector_risk_exceeded"):
                add_position_tranche(pid, add_price=93000.0, add_quantity=1, portfolio_dir=tmpdir)

            # Profitable add WITH tightened stop (94800): avg=94000, qty=2 -> risk=(94800-94000)*5*2=8,000 <= 10,000
            added = add_position_tranche(
                pid,
                add_price=93000.0,
                add_quantity=1,
                new_stop_30m=94800.0,
                reason="30m 三卖确认浮盈加仓",
                portfolio_dir=tmpdir,
            )
            self.assertEqual(added["position"]["quantity"], 2)
            self.assertEqual(added["position"]["avg_entry_price"], 94000.0)
            self.assertEqual(added["position"]["current_open_risk_amount"], 8000.0)

            # Widening stop on short (95200 > 94800) MUST FAIL
            with self.assertRaisesRegex(ValueError, "stop_widening_forbidden"):
                trail_stop(pid, new_stop_30m=95200.0, portfolio_dir=tmpdir)

            # Trail stop past breakeven (93500 < avg 94000) -> open risk becomes 0.0, locks 5,000 profit
            trailed = trail_stop(
                pid,
                new_stop_30m=93500.0,
                new_structural_stop_daily=96000.0,
                reason="30m 新中枢下移，止损移至保本锁利",
                portfolio_dir=tmpdir,
            )
            self.assertEqual(trailed["position"]["current_open_risk_amount"], 0.0)
            self.assertEqual(trailed["position"]["locked_profit_amount"], 5000.0)
            self.assertEqual(trailed["freed_risk_amount"], 8000.0)

            # Partial trim then full close checks realized PnL, R-multiple, and exact non-double-counted equity update
            trimmed = trim_position(pid, trim_price=90000.0, trim_quantity=1, reason="部分止盈", portfolio_dir=tmpdir)
            self.assertEqual(trimmed["partial_realized_pnl"], 20000.0)
            self.assertEqual(trimmed["position"]["quantity"], 1)
            self.assertEqual(load_account(tmpdir)["equity"], 1_020_000.0)
            self.assertEqual(load_account(tmpdir)["peak_equity"], 1_020_000.0)

            closed = close_position(
                pid,
                exit_price=89000.0,
                exit_reason="达到日线目标位且库存拐点出现",
                exit_category="target",
                portfolio_dir=tmpdir,
            )
            # Total PnL = 20,000 (first contract) + (94000-89000)*5*1 (25,000) = 45,000; initial_risk = 6,000 -> 7.5R
            self.assertEqual(closed["realized_pnl"], 45000.0)
            self.assertEqual(closed["r_multiple"], 7.5)
            # Ledger equity must reflect 1,000,000 + 20,000 + 25,000 = 1,045,000 (NOT double-counting 20,000!)
            self.assertEqual(load_account(tmpdir)["equity"], 1_045_000.0)
            self.assertEqual(load_account(tmpdir)["peak_equity"], 1_045_000.0)

    def test_sqlite_atomic_rollback_and_post_loss_rotate_leader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=10_000.0, default_risk_per_trade_pct=1.0)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "portfolio.db")))

            # Open old leader: 100 shares @ $100, stop=$99 -> risk=$100 (1% of $10k), notional=$10,000
            r_old = open_position(
                symbol="PWR",
                direction="long",
                sector_theme="power_grid",
                entry_price=100.0,
                tactical_stop_30m=99.0,
                quantity=100,
                exit_red_lines=["30m 跌破 99"],
                portfolio_dir=tmpdir,
            )
            old_id = r_old["position"]["position_id"]
            journal_before = load_journal(tmpdir)
            self.assertEqual(len(journal_before), 1)

            # 1) Post-loss rotate_leader: old exits at $90 -> -$1,000 loss -> post_equity=$9,000 (10% drawdown -> scale=0.50)
            # Post-loss single_sector_cap = $9,000 * 1% * 0.50 = $45.00!
            # New leader with risk=$80 (<= old $100 cap, > new $45 cap) MUST BE REJECTED and roll back completely!
            with self.assertRaisesRegex(ValueError, "single_sector_risk_exceeded"):
                rotate_leader(
                    old_position_id=old_id,
                    exit_price=90.0,
                    exit_reason="原龙头跳空破位切换",
                    new_position_kwargs={
                        "symbol": "GEV",
                        "direction": "long",
                        "entry_price": 100.0,
                        "tactical_stop_30m": 99.0,
                        "quantity": 80,  # risk=$80 > post-loss $45 cap
                    },
                    portfolio_dir=tmpdir,
                )
            self.assertEqual(load_account(tmpdir)["equity"], 10_000.0)
            self.assertEqual(len(load_positions(tmpdir, status="open")), 1)
            self.assertEqual(load_positions(tmpdir, status="open")[0]["symbol"], "PWR")
            self.assertEqual(len(load_journal(tmpdir)), len(journal_before))

            # 2) Simulated crash between leg 1 (close old) and leg 2 (open new) of rotate_leader -> FULL SQLite rollback!
            real_upsert = pm._db_upsert_position
            call_count = {"n": 0}

            def _fail_on_second_upsert(conn, pos):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise OSError("simulated disk failure on 2nd leg")
                return real_upsert(conn, pos)

            with patch.object(pm, "_db_upsert_position", side_effect=_fail_on_second_upsert):
                with self.assertRaises(OSError):
                    rotate_leader(
                        old_position_id=old_id,
                        exit_price=90.0,
                        exit_reason="原龙头跳空破位切换",
                        new_position_kwargs={
                            "symbol": "GEV",
                            "direction": "long",
                            "entry_price": 100.0,
                            "tactical_stop_30m": 99.0,
                            "quantity": 40,  # risk=$40 <= post-loss $45 cap
                        },
                        portfolio_dir=tmpdir,
                    )
            # ZERO stale positions, ZERO ghost journal rows, equity UNCHANGED!
            self.assertEqual(load_account(tmpdir)["equity"], 10_000.0)
            open_after_crash = load_positions(tmpdir, status="open")
            self.assertEqual(len(open_after_crash), 1)
            self.assertEqual(open_after_crash[0]["symbol"], "PWR")
            self.assertEqual(len(load_journal(tmpdir)), len(journal_before))

            # 3) Simulated crash on journal insert during rotate_leader -> FULL SQLite rollback!
            with patch.object(pm, "_db_insert_journal_records", side_effect=OSError("journal insert failed")):
                with self.assertRaises(OSError):
                    rotate_leader(
                        old_position_id=old_id,
                        exit_price=90.0,
                        exit_reason="原龙头跳空破位切换",
                        new_position_kwargs={
                            "symbol": "GEV",
                            "direction": "long",
                            "entry_price": 100.0,
                            "tactical_stop_30m": 99.0,
                            "quantity": 40,
                        },
                        portfolio_dir=tmpdir,
                    )
            self.assertEqual(load_account(tmpdir)["equity"], 10_000.0)
            self.assertEqual(load_positions(tmpdir, status="open")[0]["symbol"], "PWR")
            self.assertEqual(len(load_journal(tmpdir)), len(journal_before))

            # 4) Valid post-loss rotation fitting $45 cap succeeds atomically and updates equity to $9,000!
            rot_ok = rotate_leader(
                old_position_id=old_id,
                exit_price=90.0,
                exit_reason="原龙头跳空破位切换",
                new_position_kwargs={
                    "symbol": "GEV",
                    "direction": "long",
                    "entry_price": 100.0,
                    "tactical_stop_30m": 99.0,
                    "quantity": 40,  # risk=$40 <= $45
                },
                portfolio_dir=tmpdir,
            )
            self.assertEqual(rot_ok["status"], "rotated")
            self.assertEqual(load_account(tmpdir)["equity"], 9_000.0)
            self.assertEqual(load_account(tmpdir)["peak_equity"], 10_000.0)
            self.assertEqual(effective_risk_scale(load_account(tmpdir)), 0.50)
            self.assertEqual(load_positions(tmpdir, status="open")[0]["symbol"], "GEV")

    def test_account_bounds_stop_consistency_granularity_fx_and_capital_caps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=100_000.0, base_currency="USD")

            # update_account shares bounds validator -> rejects 900% risk limits!
            with self.assertRaises(ValueError):
                update_account(tmpdir, max_total_open_risk_pct=900.0)
            with self.assertRaises(ValueError):
                update_account(tmpdir, max_macro_factor_risk_pct=900.0)
            with self.assertRaises(ValueError):
                update_account(tmpdir, default_risk_per_trade_pct=25.0)

            # Cross-currency open_position without fx_to_account MUST fail closed
            with self.assertRaisesRegex(ValueError, "Cross-currency"):
                open_position(
                    symbol="USDJPY=X",
                    direction="short",
                    sector_theme="fx_jpy",
                    entry_price=155.0,
                    tactical_stop_30m=156.0,
                    quantity=10_000,
                    portfolio_dir=tmpdir,
                )

            # Portfolio margin / capital ceiling enforced on open_position
            # Equity=$100k, entry=$100, stop=$99.5, qty=1500 -> risk=$750 <= $1k, BUT cash notional=$150k > $100k!
            with self.assertRaisesRegex(ValueError, "portfolio_capital_limit_exceeded"):
                open_position(
                    symbol="NVDA",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=100.0,
                    tactical_stop_30m=99.5,
                    quantity=1500,
                    portfolio_dir=tmpdir,
                )

            # Open valid long position: entry=100, stop=95, qty=100 -> risk=$500, margin=$10,000
            r_nvda = open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=100.0,
                tactical_stop_30m=95.0,
                quantity=100,
                exit_red_lines=["30m 跌破 95"],
                portfolio_dir=tmpdir,
            )
            pid = r_nvda["position"]["position_id"]

            # trail_stop with current_price rejects already-breached stops
            with self.assertRaisesRegex(ValueError, "stop_breached_by_current_price"):
                trail_stop(pid, new_stop_30m=112.0, current_price=110.0, portfolio_dir=tmpdir)
            with self.assertRaisesRegex(ValueError, "stop_breached_by_current_price"):
                trail_stop(pid, new_stop_30m=110.0, current_price=110.0, portfolio_dir=tmpdir)

            # Valid trail up to 110 when current_price=115
            trail_stop(pid, new_stop_30m=110.0, current_price=115.0, portfolio_dir=tmpdir)

            # add_position_tranche MUST reject adding across already-trailed stop (entry=100, stop=110, add=105)!
            with self.assertRaisesRegex(ValueError, "stop_consistency_violation"):
                add_position_tranche(pid, add_price=105.0, add_quantity=50, portfolio_dir=tmpdir)

            # add_position_tranche MUST also check added tranche's own risk from add_price to eff_stop!
            # add at 130 with stop=110, qty=60 -> added tranche risk = (130-110)*60 = $1,200 > $1,000 sector cap
            # (even though blended avg=(100*100+130*60)/160=111.25 -> combined open risk=$200!)
            with self.assertRaisesRegex(ValueError, "add_tranche_sector_risk_exceeded"):
                add_position_tranche(pid, add_price=130.0, add_quantity=60, portfolio_dir=tmpdir)

            # Valid add with standalone tranche risk <= cap passes: (125-110)*50 = $750 <= $1,000
            add_ok = add_position_tranche(pid, add_price=125.0, add_quantity=50, portfolio_dir=tmpdir)
            self.assertEqual(add_ok["status"], "added")
            self.assertEqual(add_ok["added_tranche_risk_amount"], 750.0)

        # CNY account tests for quantity granularity (CU0 0.5 contracts rejected; STAR 688001 min=200 step=1)
        with tempfile.TemporaryDirectory() as cny_dir:
            init_portfolio(portfolio_dir=cny_dir, equity=1_000_000.0, base_currency="CNY")

            # Reject fractional 0.5 contracts of CU0
            with self.assertRaisesRegex(ValueError, "invalid_quantity_granularity"):
                open_position(
                    symbol="CU0",
                    direction="short",
                    sector_theme="copper",
                    entry_price=95000.0,
                    tactical_stop_30m=96000.0,
                    quantity=0.5,
                    portfolio_dir=cny_dir,
                )

            # Reject 100 shares of STAR Market 688001 (< min_order_qty=200)
            with self.assertRaisesRegex(ValueError, "invalid_quantity_granularity"):
                open_position(
                    symbol="688001",
                    direction="long",
                    sector_theme="star_semi",
                    entry_price=50.0,
                    tactical_stop_30m=45.0,
                    quantity=100,
                    portfolio_dir=cny_dir,
                )

            # Accept 201 shares of STAR Market 688001 (min_order_qty=200, lot_step=1)
            star_open = open_position(
                symbol="688001",
                direction="long",
                sector_theme="star_semi",
                entry_price=50.0,
                tactical_stop_30m=45.0,
                quantity=201,
                exit_red_lines=["30m 跌破 45"],
                portfolio_dir=cny_dir,
            )
            self.assertEqual(star_open["position"]["quantity"], 201)

            # US stock in CNY account without fx_to_account rejected
            with self.assertRaisesRegex(ValueError, "Cross-currency"):
                open_position(
                    symbol="NVDA",
                    direction="long",
                    sector_theme="us_ai",
                    entry_price=100.0,
                    tactical_stop_30m=95.0,
                    quantity=10,
                    portfolio_dir=cny_dir,
                )

    def test_drawdown_scaling_total_risk_and_macro_factor_overlap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(
                portfolio_dir=tmpdir,
                equity=1_000_000.0,
                default_risk_per_trade_pct=1.0,
                max_total_open_risk_pct=6.0,
                max_macro_factor_risk_pct=2.5,
            )
            acc = load_account(tmpdir)
            self.assertEqual(effective_risk_scale(acc), 1.0)

            # 6% drawdown -> risk_scale = 0.75
            acc_dd6 = update_account(tmpdir, equity=940_000.0)
            self.assertEqual(effective_risk_scale(acc_dd6), 0.75)

            # 12% drawdown -> risk_scale = 0.50 -> single sector cap = 880,000 * 1% * 0.5 = 4,400
            acc_dd12 = update_account(tmpdir, equity=880_000.0)
            self.assertEqual(effective_risk_scale(acc_dd12), 0.50)
            with self.assertRaisesRegex(ValueError, "single_sector_risk_exceeded"):
                open_position(
                    symbol="NVDA",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=100.0,
                    tactical_stop_30m=90.0,
                    quantity=500,  # risk = $5,000 > $4,400 scaled cap
                    portfolio_dir=tmpdir,
                )

            # Reset to 1M for macro factor overlap test (cap = 2.5% = $25,000)
            update_account(tmpdir, equity=1_000_000.0, peak_equity=1_000_000.0)
            open_position(
                symbol="NVDA", direction="long", sector_theme="sec1",
                entry_price=100.0, tactical_stop_30m=90.0, quantity=1000,  # $10k
                macro_factors=["usd_liquidity"], portfolio_dir=tmpdir,
            )
            open_position(
                symbol="GC=F", direction="long", sector_theme="sec2",
                entry_price=2800.0, tactical_stop_30m=2700.0, quantity=1,  # $10k
                macro_factors=["usd_liquidity"], portfolio_dir=tmpdir,
            )
            # 3rd position sharing usd_liquidity adds $10k -> $30k > $25k cap -> macro_factor_overexposed
            with self.assertRaisesRegex(ValueError, "macro_factor_overexposed"):
                open_position(
                    symbol="EURUSD=X", direction="long", sector_theme="sec3",
                    entry_price=1.10, tactical_stop_30m=1.08, quantity=500000,  # $10k
                    macro_factors=["usd_liquidity"], portfolio_dir=tmpdir,
                )

            summary = summarize_portfolio(tmpdir)
            self.assertEqual(summary["open_positions_count"], 2)
            self.assertEqual(summary["total_open_risk_amount"], 20000.0)
            self.assertEqual(summary["macro_factor_concentration"]["usd_liquidity"]["open_risk_amount"], 20000.0)

    def test_concurrent_sector_admission_locking_and_journal_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0)
            barrier = threading.Barrier(2)
            results: list[str] = []
            lock = threading.Lock()

            def _try_open(sym: str):
                barrier.wait()
                try:
                    open_position(
                        symbol=sym,
                        direction="long",
                        sector_theme="power_grid",
                        entry_price=100.0,
                        tactical_stop_30m=90.0,
                        quantity=800,
                        exit_red_lines=["30m 跌破 90"],
                        portfolio_dir=tmpdir,
                    )
                    with lock:
                        results.append("ok")
                except ValueError as exc:
                    with lock:
                        results.append("sector_occupied" if "sector_leader_occupied" in str(exc) else str(exc))

            t1 = threading.Thread(target=_try_open, args=("PWR",))
            t2 = threading.Thread(target=_try_open, args=("GEV",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(sorted(results), ["ok", "sector_occupied"])
            self.assertEqual(len(load_positions(tmpdir, status="open")), 1)

            # Rollback safety: if SQLite journal insert raises OSError, positions & journal stay completely untouched
            before_pos = load_positions(tmpdir)
            before_journal = load_journal(tmpdir)
            with patch.object(pm, "_db_insert_journal_records", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    open_position(
                        symbol="NVDA",
                        direction="long",
                        sector_theme="ai_compute",
                        entry_price=100.0,
                        tactical_stop_30m=90.0,
                        quantity=500,
                        portfolio_dir=tmpdir,
                    )
            after_pos = load_positions(tmpdir)
            after_journal = load_journal(tmpdir)
            self.assertEqual(len(after_pos), len(before_pos))
            self.assertEqual(len(after_journal), len(before_journal))

    def test_portfolio_cli_end_to_end_and_schema_valid_json(self):
        schema_path = os.path.join(ROOT, "portfolio", "schema.json")
        self.assertTrue(os.path.exists(schema_path))
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        self.assertIn("$defs", schema)
        self.assertIn("account", schema["$defs"])
        self.assertIn("positions_document", schema["$defs"])
        self.assertIn("journal_entry", schema["$defs"])
        self.assertIn("portfolio.db", schema["description"])

        with tempfile.TemporaryDirectory() as tmpdir:
            def run_cli(argv: list[str]) -> tuple[int, dict]:
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = portfolio_main(["--portfolio-dir", tmpdir, *argv])
                raw = out_buf.getvalue().strip() or err_buf.getvalue().strip()
                return rc, json.loads(raw)

            rc, res = run_cli(["init", "--equity", "1000000"])
            self.assertEqual(rc, 0)
            self.assertEqual(res["status"], "created")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "portfolio.db")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "account.json")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "positions.json")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "journal.jsonl")))

            rc, res = run_cli([
                "open", "--symbol", "PWR", "--direction", "long", "--sector", "power_grid",
                "--entry", "300", "--stop", "285", "--quantity", "500",
                "--red-line", "30m 跌破 285",
            ])
            self.assertEqual(rc, 0)
            pid = res["position"]["position_id"]

            # Follower rejected via CLI with non-zero exit code
            rc, err_res = run_cli([
                "open", "--symbol", "ETN", "--direction", "long", "--sector", "power_grid",
                "--entry", "350", "--stop", "335", "--quantity", "200",
            ])
            self.assertEqual(rc, 2)
            self.assertIn("sector_leader_occupied", err_res["error"])

            rc, adm = run_cli([
                "check-admission", "--sector", "power_grid", "--symbol", "GEV",
                "--risk-amount", "7000", "--allow-rotate",
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(adm["allowed"])
            self.assertEqual(adm["mode"], "rotate_leader")

            rc, trail = run_cli(["trail-stop", "--id", pid, "--new-stop", "300", "--current-price", "320"])
            self.assertEqual(rc, 0)
            self.assertEqual(trail["position"]["current_open_risk_amount"], 0.0)

            # CLI trim subcommand end-to-end: trim 200 shares @ 330 (+$6,000 partial realized PnL)
            rc, trimmed = run_cli([
                "trim", "--id", pid, "--price", "330", "--quantity", "200", "--reason", "分批止盈",
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(trimmed["status"], "trimmed")
            self.assertEqual(trimmed["partial_realized_pnl"], 6000.0)
            self.assertEqual(trimmed["position"]["quantity"], 300)
            self.assertEqual(trimmed["account"]["equity"], 1_006_000.0)

            rc, summary = run_cli(["summary"])
            self.assertEqual(rc, 0)
            self.assertEqual(summary["open_positions_count"], 1)
            self.assertEqual(summary["total_open_risk_amount"], 0.0)
            self.assertEqual(summary["equity"], 1_006_000.0)

            # Close remaining 300 shares @ 345 (+$13,500 rem PnL -> total $19,500; initial_risk=$7,500 -> 2.6R)
            rc, closed = run_cli(["close", "--id", pid, "--exit-price", "345", "--exit-reason", "目标兑现"])
            self.assertEqual(rc, 0)
            self.assertEqual(closed["realized_pnl"], 19500.0)
            self.assertEqual(closed["r_multiple"], 2.6)
            self.assertEqual(closed["account"]["equity"], 1_019_500.0)

    def test_round2_post_commit_mirror_resilience_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            init_res = init_portfolio(portfolio_dir=tmpdir, equity=100_000.0)
            self.assertTrue(init_res["mirror_sync_ok"])
            self.assertIsNone(init_res["mirror_sync_error"])

            opened = open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=100.0,
                tactical_stop_30m=95.0,
                quantity=10,
                exit_red_lines=["30m 跌破 95"],
                portfolio_dir=tmpdir,
                idempotency_key="open-nvda-1",
            )
            self.assertTrue(opened["mirror_sync_ok"])
            pid = opened["position"]["position_id"]

            # Inject post-commit OSError ONLY when exporting journal.jsonl
            real_atomic_write_text = pm._atomic_write_text

            def _fail_journal_mirror(path: str, text: str) -> None:
                if path.endswith("journal.jsonl"):
                    raise OSError("simulated journal.jsonl disk error")
                return real_atomic_write_text(path, text)

            err_buf = io.StringIO()
            with patch.object(pm, "_atomic_write_text", side_effect=_fail_journal_mirror), redirect_stderr(err_buf):
                # 1) Post-commit mirror OSError MUST NOT raise as a failed trade!
                add_res = add_position_tranche(
                    pid,
                    add_price=110.0,
                    add_quantity=2,
                    reason="30m 浮盈加仓",
                    portfolio_dir=tmpdir,
                    idempotency_key="add-nvda-1",
                )
                self.assertEqual(add_res["status"], "added")
                self.assertEqual(add_res["position"]["quantity"], 12)
                self.assertFalse(add_res["mirror_sync_ok"])
                self.assertIn("OSError", add_res["mirror_sync_error"])
                self.assertIn("WARNING", err_buf.getvalue())

                # Canonical DB has 12 shares and 2 journal rows even though journal.jsonl mirror lagged
                self.assertEqual(load_positions(tmpdir, status="open")[0]["quantity"], 12)
                self.assertEqual(len(load_journal(tmpdir)), 2)

                # 2) Retrying with same idempotency_key MUST return cached result and NEVER double-add!
                retry_res = add_position_tranche(
                    pid,
                    add_price=110.0,
                    add_quantity=2,
                    reason="30m 浮盈加仓",
                    portfolio_dir=tmpdir,
                    idempotency_key="add-nvda-1",
                )
                self.assertTrue(retry_res["idempotent_replay"])
                self.assertEqual(retry_res["position"]["quantity"], 12)
                self.assertFalse(retry_res["mirror_sync_ok"])
                self.assertEqual(load_positions(tmpdir, status="open")[0]["quantity"], 12)
                self.assertEqual(len(load_journal(tmpdir)), 2)

            # Reusing an idempotency key on a DIFFERENT operation MUST raise ValueError
            with self.assertRaisesRegex(ValueError, "already used for operation"):
                trim_position(pid, trim_price=115.0, trim_quantity=2, portfolio_dir=tmpdir, idempotency_key="add-nvda-1")

            # 3) Explicit sync_mirrors() heals all 3 JSON mirrors cleanly from canonical portfolio.db!
            sync_res = sync_mirrors(tmpdir)
            self.assertEqual(sync_res["status"], "synced")
            self.assertTrue(sync_res["mirror_sync_ok"])
            self.assertEqual(sync_res["journal_count"], 2)

            with open(os.path.join(tmpdir, "positions.json"), "r", encoding="utf-8") as f:
                pos_mirror = json.load(f)
            self.assertEqual(pos_mirror["positions"][0]["quantity"], 12)
            with open(os.path.join(tmpdir, "journal.jsonl"), "r", encoding="utf-8") as f:
                journal_lines = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(journal_lines), 2)
            self.assertEqual(journal_lines[1]["action"], "add_on_profit")
            self.assertEqual(journal_lines[1]["total_quantity"], 12)

    def test_round2_contract_identity_cash_equity_margin_daily_stop_and_sse_rule_6_7(self):
        # 1) Contract identity validation + cash-equity margin floor (USD account)
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=10_000.0, base_currency="USD", default_risk_per_trade_pct=2.0)

            # Mismatched equity contract_symbol (PWR vs QQQ) MUST be rejected!
            with self.assertRaisesRegex(ValueError, "contract_symbol 'QQQ' does not match underlying symbol 'PWR'"):
                open_position(
                    symbol="PWR",
                    direction="long",
                    sector_theme="power_grid",
                    entry_price=100.0,
                    tactical_stop_30m=99.0,
                    quantity=10,
                    contract_symbol="QQQ",
                    portfolio_dir=tmpdir,
                )

            # Cash equity margin floor: $10k equity + margin_rate=0.1 + $20k stock exposure MUST be rejected!
            with self.assertRaisesRegex(ValueError, "equity_margin_forbidden"):
                open_position(
                    symbol="AAPL",
                    direction="long",
                    sector_theme="tech_cash",
                    entry_price=100.0,
                    tactical_stop_30m=99.5,
                    quantity=200,  # $20,000 exposure on $10,000 equity
                    margin_rate=0.1,
                    portfolio_dir=tmpdir,
                )

            # Without allow_equity_margin and default margin_rate=1.0 -> portfolio_capital_limit_exceeded
            with self.assertRaisesRegex(ValueError, "portfolio_capital_limit_exceeded"):
                open_position(
                    symbol="AAPL",
                    direction="long",
                    sector_theme="tech_cash",
                    entry_price=100.0,
                    tactical_stop_30m=99.5,
                    quantity=200,
                    portfolio_dir=tmpdir,
                )

            # Bare future as contract_symbol (CL=F + CLZ26) MUST be rejected!
            update_account(tmpdir, equity=1_000_000.0, peak_equity=1_000_000.0)
            with self.assertRaisesRegex(ValueError, "bare overseas futures"):
                open_position(
                    symbol="CL=F",
                    direction="short",
                    sector_theme="energy",
                    entry_price=70.0,
                    tactical_stop_30m=72.0,
                    quantity=1,
                    contract_symbol="CLZ26",
                    portfolio_dir=tmpdir,
                )

            # Valid exchange-suffixed dated overseas future (CL=F + CLZ26.NYM) succeeds!
            cl_ok = open_position(
                symbol="CL=F",
                direction="short",
                sector_theme="energy",
                entry_price=70.0,
                tactical_stop_30m=72.0,
                quantity=1,
                contract_symbol="CLZ26.NYM",
                exit_red_lines=["30m 突破 72"],
                portfolio_dir=tmpdir,
            )
            self.assertEqual(cl_ok["position"]["contract_symbol"], "CLZ26.NYM")

            # 2) Daily structural stop consistency on add_position_tranche + stop_updated_at_utc stamping
            nvda_open = open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=100.0,
                tactical_stop_30m=95.0,
                structural_stop_daily=90.0,
                quantity=100,
                exit_red_lines=["30m 跌破 95", "日线跌破 90"],
                portfolio_dir=tmpdir,
            )
            pid = nvda_open["position"]["position_id"]
            self.assertIsNotNone(nvda_open["position"]["stop_updated_at_utc"])
            self.assertIsNotNone(nvda_open["position"]["structural_stop_updated_at_utc"])
            self.assertEqual(nvda_open["position"]["stop_updated_at_utc"], nvda_open["position"]["opened_at_utc"])

            # Trail tactical stop to 100 and structural_stop_daily to 109 (at current_price=112)
            with patch.object(pm, "utc_now_str", return_value="2099-01-01 00:00:00 UTC"):
                trailed = trail_stop(
                    pid,
                    new_stop_30m=100.0,
                    new_structural_stop_daily=109.0,
                    current_price=112.0,
                    portfolio_dir=tmpdir,
                )
            self.assertEqual(trailed["position"]["stop_updated_at_utc"], "2099-01-01 00:00:00 UTC")
            self.assertEqual(trailed["position"]["structural_stop_updated_at_utc"], "2099-01-01 00:00:00 UTC")

            # Adding at 108 (> entry 100, > tactical 100, BUT <= structural_stop_daily 109) MUST FAIL!
            with self.assertRaisesRegex(ValueError, "add_price crosses structural_stop_daily"):
                add_position_tranche(pid, add_price=108.0, add_quantity=10, portfolio_dir=tmpdir)

            # Adding at 112 (> entry 100, > tactical 100, > daily 109) WITH tightened 30m stop stamps stop_updated_at_utc!
            with patch.object(pm, "utc_now_str", return_value="2099-01-02 00:00:00 UTC"):
                add_ok = add_position_tranche(
                    pid,
                    add_price=112.0,
                    add_quantity=10,
                    new_stop_30m=105.0,
                    portfolio_dir=tmpdir,
                )
            self.assertEqual(add_ok["position"]["stop_updated_at_utc"], "2099-01-02 00:00:00 UTC")
            self.assertEqual(add_ok["position"]["structural_stop_updated_at_utc"], "2099-01-01 00:00:00 UTC")

        # 3) CNY account: futures root validation (CU0 vs AL2612) + STAR 688981 & Main Board SSE Rule 6.7 on trim_position
        with tempfile.TemporaryDirectory() as cny_dir:
            init_portfolio(portfolio_dir=cny_dir, equity=1_000_000.0, base_currency="CNY")

            # Mismatched CN futures root (CU0 + AL2612) rejected; matching (CU0 + CU2612) accepted
            with self.assertRaisesRegex(ValueError, "does not match underlying symbol"):
                open_position(
                    symbol="CU0",
                    direction="short",
                    sector_theme="copper",
                    entry_price=95000.0,
                    tactical_stop_30m=96200.0,
                    quantity=1,
                    contract_symbol="AL2612",
                    portfolio_dir=cny_dir,
                )
            cu_ok = open_position(
                symbol="CU0",
                direction="short",
                sector_theme="copper",
                entry_price=95000.0,
                tactical_stop_30m=96200.0,
                quantity=1,
                contract_symbol="CU2612",
                exit_red_lines=["30m 突破 96200"],
                portfolio_dir=cny_dir,
            )
            self.assertEqual(cu_ok["position"]["contract_symbol"], "CU2612")

            # STAR Market 688981 (min_order_qty=200, lot_step=1): 200 shares -> trim_position(..., 1) REJECTED!
            star_200 = open_position(
                symbol="688981",
                direction="long",
                sector_theme="star_foundry",
                entry_price=50.0,
                tactical_stop_30m=45.0,
                quantity=200,
                exit_red_lines=["30m 跌破 45"],
                portfolio_dir=cny_dir,
            )
            star_pid = star_200["position"]["position_id"]
            with self.assertRaisesRegex(ValueError, "invalid_quantity_granularity"):
                trim_position(star_pid, trim_price=55.0, trim_quantity=1, portfolio_dir=cny_dir)

            # Add 200 shares on profit -> 400 shares total; trimming 201 leaves 199 (<200) -> REJECTED (SSE Rule 6.7)!
            add_position_tranche(star_pid, add_price=55.0, add_quantity=200, new_stop_30m=50.0, portfolio_dir=cny_dir)
            with self.assertRaisesRegex(ValueError, "SSE Rule 6.7"):
                trim_position(star_pid, trim_price=60.0, trim_quantity=201, portfolio_dir=cny_dir)

            # Trimming 200 from 400 leaves 200 (>=200) -> ALLOWED!
            star_trimmed = trim_position(star_pid, trim_price=60.0, trim_quantity=200, portfolio_dir=cny_dir)
            self.assertEqual(star_trimmed["position"]["quantity"], 200)

            # Main board A-share 600519 (min_order_qty=100, lot_step=100): trim 50 rejected; trim 100 from 200 allowed
            moutai = open_position(
                symbol="600519",
                direction="long",
                sector_theme="baijiu",
                entry_price=1500.0,
                tactical_stop_30m=1460.0,
                quantity=200,
                exit_red_lines=["30m 跌破 1460"],
                portfolio_dir=cny_dir,
            )
            mt_pid = moutai["position"]["position_id"]
            with self.assertRaisesRegex(ValueError, "invalid_quantity_granularity"):
                trim_position(mt_pid, trim_price=1600.0, trim_quantity=50, portfolio_dir=cny_dir)
            mt_trimmed = trim_position(mt_pid, trim_price=1600.0, trim_quantity=100, portfolio_dir=cny_dir)
            self.assertEqual(mt_trimmed["position"]["quantity"], 100)

    def test_round3_cn_future_override_idempotency_fingerprint_and_gap_loss_insolvency(self):
        # 1) open_position("CU2612", ..., asset_class="us_stock_or_etf") MUST be rejected!
        with tempfile.TemporaryDirectory() as tmpdir:
            init_portfolio(portfolio_dir=tmpdir, equity=100_000.0, base_currency="USD")
            with self.assertRaisesRegex(ValueError, "Unknown contract specification"):
                open_position(
                    symbol="CU2612",
                    direction="short",
                    sector_theme="copper",
                    entry_price=95000.0,
                    tactical_stop_30m=96000.0,
                    quantity=1,
                    asset_class="us_stock_or_etf",
                    portfolio_dir=tmpdir,
                )

            # 2) Idempotency key payload fingerprint:
            #    - identical PWR retry replays cleanly
            #    - reusing same key for QQQ raises ValueError("idempotency_key_conflict")
            p1 = open_position(
                symbol="PWR",
                direction="long",
                sector_theme="power_grid",
                entry_price=300.0,
                tactical_stop_30m=290.0,
                quantity=50,
                exit_red_lines=["30m 跌破 290"],
                portfolio_dir=tmpdir,
                idempotency_key="idem-open-001",
            )
            self.assertEqual(p1["status"], "opened")
            p1_replay = open_position(
                symbol="PWR",
                direction="long",
                sector_theme="power_grid",
                entry_price=300.0,
                tactical_stop_30m=290.0,
                quantity=50,
                exit_red_lines=["30m 跌破 290"],
                portfolio_dir=tmpdir,
                idempotency_key="idem-open-001",
            )
            self.assertTrue(p1_replay["idempotent_replay"])
            self.assertEqual(p1_replay["position"]["position_id"], p1["position"]["position_id"])

            with self.assertRaisesRegex(ValueError, "idempotency_key_conflict"):
                open_position(
                    symbol="QQQ",
                    direction="long",
                    sector_theme="nasdaq",
                    entry_price=500.0,
                    tactical_stop_30m=490.0,
                    quantity=20,
                    portfolio_dir=tmpdir,
                    idempotency_key="idem-open-001",
                )

            # Different quantity on same symbol with same idempotency key also conflicts
            with self.assertRaisesRegex(ValueError, "idempotency_key_conflict"):
                open_position(
                    symbol="PWR",
                    direction="long",
                    sector_theme="power_grid",
                    entry_price=300.0,
                    tactical_stop_30m=290.0,
                    quantity=60,
                    exit_red_lines=["30m 跌破 290"],
                    portfolio_dir=tmpdir,
                    idempotency_key="idem-open-001",
                )

            # Also verify parameter conflict on trail_stop
            pwr_pid = p1["position"]["position_id"]
            t1 = trail_stop(pwr_pid, new_stop_30m=295.0, portfolio_dir=tmpdir, idempotency_key="idem-trail-001")
            self.assertEqual(t1["status"], "trailed")
            t1_replay = trail_stop(pwr_pid, new_stop_30m=295.0, portfolio_dir=tmpdir, idempotency_key="idem-trail-001")
            self.assertTrue(t1_replay["idempotent_replay"])
            with self.assertRaisesRegex(ValueError, "idempotency_key_conflict"):
                trail_stop(pwr_pid, new_stop_30m=298.0, portfolio_dir=tmpdir, idempotency_key="idem-trail-001")

        # 3) Gap loss realized close even when equity <= 0 ($10k short 1 CL=F @ 60, closed @ 71 -> equity = -$1,000)
        with tempfile.TemporaryDirectory() as gap_dir:
            init_portfolio(
                portfolio_dir=gap_dir,
                equity=10_000.0,
                base_currency="USD",
                default_risk_per_trade_pct=10.0,
                max_total_open_risk_pct=20.0,
            )
            cl_open = open_position(
                symbol="CL=F",
                direction="short",
                sector_theme="crude_oil",
                entry_price=60.0,
                tactical_stop_30m=61.0,  # risk = $1,000 (10%), margin = $6,000 <= $10,000
                quantity=1,
                exit_red_lines=["30m 突破 61"],
                portfolio_dir=gap_dir,
            )
            cl_pid = cl_open["position"]["position_id"]

            # Market gaps to 71 -> -$11,000 realized loss -> equity = -$1,000 -> MUST SUCCEED and record insolvency!
            closed = close_position(
                cl_pid,
                exit_price=71.0,
                exit_reason="周末地缘冲突跳空穿仓强平",
                exit_category="stop_loss_gap",
                portfolio_dir=gap_dir,
            )
            self.assertEqual(closed["status"], "closed")
            self.assertEqual(closed["position"]["status"], "closed")
            self.assertEqual(closed["realized_pnl"], -11_000.0)
            self.assertEqual(closed["account"]["equity"], -1_000.0)
            self.assertTrue(closed["account"]["insolvent"])
            self.assertTrue(closed["account_insolvent"])
            self.assertTrue(any("account_insolvent" in w for w in closed["warnings"]))

            # summarize_portfolio() and effective_risk_scale() must work cleanly without crashing!
            acc_insolvent = load_account(gap_dir)
            self.assertEqual(acc_insolvent["equity"], -1_000.0)
            self.assertEqual(effective_risk_scale(acc_insolvent), 0.0)
            summary = summarize_portfolio(gap_dir)
            self.assertEqual(summary["equity"], -1_000.0)
            self.assertTrue(summary["insolvent"])
            self.assertEqual(summary["drawdown_pct"], 100.0)
            self.assertEqual(summary["risk_scale"], 0.0)
            self.assertEqual(summary["open_positions_count"], 0)
            self.assertEqual(summary["closed_positions_count"], 1)

            # Subsequent open_position MUST be rejected due to non-positive equity!
            with self.assertRaisesRegex(ValueError, "account_equity_non_positive"):
                open_position(
                    symbol="NVDA",
                    direction="long",
                    sector_theme="ai_compute",
                    entry_price=100.0,
                    tactical_stop_30m=99.0,
                    quantity=1,
                    portfolio_dir=gap_dir,
                )

            # Recapitalizing via update_account(equity > 0) clears insolvent flag and restores positive equity
            recap = update_account(gap_dir, equity=20_000.0)
            self.assertEqual(recap["equity"], 20_000.0)
            self.assertFalse(recap["insolvent"])

            # Gap-loss trim_position leaving equity <= 0 ALSO succeeds and blocks subsequent add_position_tranche
            cl2 = open_position(
                symbol="CL=F",
                direction="short",
                sector_theme="crude_oil",
                entry_price=60.0,
                tactical_stop_30m=61.0,  # 2 lots -> risk=$2,000 (10% of $20k), margin=$12,000 <= $20k
                quantity=2,
                exit_red_lines=["30m 突破 61"],
                portfolio_dir=gap_dir,
            )
            cl2_pid = cl2["position"]["position_id"]
            # Trim 1 contract @ 85 -> -$25,000 partial realized loss -> equity = -$5,000
            trimmed_gap = trim_position(cl2_pid, trim_price=85.0, trim_quantity=1, reason="跳空减半仓", portfolio_dir=gap_dir)
            self.assertEqual(trimmed_gap["status"], "trimmed")
            self.assertEqual(trimmed_gap["account"]["equity"], -5_000.0)
            self.assertTrue(trimmed_gap["account_insolvent"])
            self.assertTrue(any("account_insolvent" in w for w in trimmed_gap["warnings"]))

            with self.assertRaisesRegex(ValueError, "account_equity_non_positive"):
                add_position_tranche(cl2_pid, add_price=55.0, add_quantity=1, portfolio_dir=gap_dir)

            # Codex Round-4 repro: legacy SQLite idempotency_keys table without request_hash column MUST fail closed
            # on mismatched parameters after ALTER TABLE migration (empty request_hash cannot silently replay PWR for QQQ!)
            legacy_dir = os.path.join(tmpdir, "legacy_idem")
            init_portfolio(portfolio_dir=legacy_dir, equity=100_000.0, base_currency="USD")
            open_position(
                symbol="PWR",
                direction="long",
                sector_theme="power_grid",
                entry_price=300.0,
                tactical_stop_30m=290.0,
                quantity=10,
                exit_red_lines=["30m 跌破 290"],
                portfolio_dir=legacy_dir,
                idempotency_key="legacy-key-1",
            )
            # Simulate legacy row with empty request_hash (as left by pre-Round-3 migration)
            import sqlite3 as _sqlite3
            db_file = os.path.join(legacy_dir, "portfolio.db")
            conn = _sqlite3.connect(db_file)
            conn.execute("UPDATE idempotency_keys SET request_hash = '' WHERE key = 'legacy-key-1'")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(ValueError, "idempotency_key_conflict"):
                open_position(
                    symbol="QQQ",
                    direction="long",
                    sector_theme="us_tech",
                    entry_price=500.0,
                    tactical_stop_30m=490.0,
                    quantity=10,
                    exit_red_lines=["30m 跌破 490"],
                    portfolio_dir=legacy_dir,
                    idempotency_key="legacy-key-1",
                )


if __name__ == "__main__":
    unittest.main()
