#!/usr/bin/env python3
"""Offline unit tests for Layer-4 Holding Daily Sentinel (scripts/daily_sentinel.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import daily_sentinel as ds
import research_archive as ra


def _make_armed_state(
    direction: str = "long",
    stop_threshold: float = 95.0,
    extra_triggers: list[dict] | None = None,
    events: list[dict] | None = None,
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
        "events": events or [],
    }


def _make_healthy_snapshot(
    symbol: str = "PWR",
    price: float = 110.0,
    daily_zd: float = 95.0,
    daily_zg: float = 105.0,
    intra_zd: float = 106.0,
    intra_zg: float = 109.0,
    daily_bias: str = "long",
    intraday_bias: str = "long",
    intraday_action: str = "hold",
    daily_signals: list[str] | None = None,
    intraday_signals: list[str] | None = None,
    daily_momentum: dict | None = None,
    intraday_momentum: dict | None = None,
    intraday_stroke_trigger: dict | None = None,
    intraday_key: str = "intraday_30m",
    daily_bar: str = "2026-11-23",
    intraday_bar: str = "2026-11-23 15:30",
    atr14_30m: float = 1.2,
    include_dual: bool = True,
) -> dict:
    chanlun_block: dict = {
        "signal_summary": {
            "tier": "资金一致推进(顺势)",
            "summary": "日线多头资金方向一致推进" if daily_bias == "long" else "日线空头资金方向一致推进",
            "action_bias": "顺势持有",
        },
        "daily": {
            "timeframe": "日线",
            "latest_zhongshu": {"zd": daily_zd, "zg": daily_zg},
            "signals": daily_signals
            if daily_signals is not None
            else (
                ["日线向上离开段·资金方向一致(area=1.2x,peak=1.1x·2类笔趋势延续)"]
                if daily_bias == "long"
                else ["日线向下离开段·资金方向一致(area=1.2x,peak=1.1x·2类笔未背驰)"]
            ),
            "momentum_assessment": daily_momentum
            if daily_momentum is not None
            else {
                "active_direction": "up" if daily_bias == "long" else "down",
                "verdict": "capital_consensus_up" if daily_bias == "long" else "capital_consensus_down",
                "is_capital_consensus": True,
                "is_divergence_candidate": False,
                "is_non_divergence_power": False,
            },
            "stroke_end_trigger": {"direction": "up" if daily_bias == "long" else "down", "triggered": False},
        },
        intraday_key: {
            "timeframe": "30min",
            "latest_zhongshu": {"zd": intra_zd, "zg": intra_zg},
            "signals": intraday_signals
            if intraday_signals is not None
            else ["30min中枢震荡中"],
            "momentum_assessment": intraday_momentum
            if intraday_momentum is not None
            else {
                "active_direction": "up" if intraday_bias == "long" else "down",
                "verdict": "neutral_extension_up" if intraday_bias == "long" else "neutral_extension_down",
                "is_capital_consensus": False,
                "is_divergence_candidate": False,
                "is_non_divergence_power": False,
            },
            "stroke_end_trigger": intraday_stroke_trigger
            if intraday_stroke_trigger is not None
            else {"direction": "up" if intraday_bias == "long" else "down", "triggered": False},
        },
        "intraday_signal_summary": {
            "tier": "常规结构运行",
            "summary": "30min 顺势运行",
            "action_bias": "继续持有",
        },
    }
    if include_dual:
        chanlun_block["dual_alignment"] = {
            "daily_bias": daily_bias,
            "intraday_bias": intraday_bias,
            "intraday_action": intraday_action,
            "daily_zd": daily_zd,
            "daily_zg": daily_zg,
            "intraday_zd": intra_zd,
            "intraday_zg": intra_zg,
            "intraday_tactical_stop": intra_zd if daily_bias == "long" else intra_zg,
            "aligned": daily_bias == intraday_bias,
            "alignment_status": "aligned" if daily_bias == intraday_bias else "wait_intraday",
            "notes": [],
        }

    return {
        "symbol": symbol,
        "asset_class": "us_stock_or_etf",
        "technicals": {
            "as_of_bar": daily_bar,
            "last_close": price - 0.5,
            "atr14": 3.2,
            "return_1d_pct": 1.15,
            "return_5d_pct": 4.2,
        },
        "intraday_technicals": {
            "as_of_bar": intraday_bar,
            "last_close": price,
            "atr14": atr14_30m,
            "return_1bar_pct": 0.45,
            "return_5bar_pct": 1.35,
        },
        "chanlun": chanlun_block,
    }


def _make_base_position(
    symbol: str = "PWR",
    direction: str = "long",
    entry_price: float = 100.0,
    tactical_stop_30m: float = 102.0,
    structural_stop_daily: float = 95.0,
    target_price: float = 130.0,
    contract_or_expression: str = "正股分批建仓",
    exit_red_lines: list[str] | None = None,
) -> dict:
    return {
        "position_id": f"pos_{symbol.lower()}_01",
        "symbol": symbol,
        "name": "Quanta Services" if symbol == "PWR" else symbol,
        "market": "US",
        "asset_class": "us_stock_or_etf",
        "direction": direction,
        "sector_theme": "AI电网基础设施",
        "leader_role": "sole_leader",
        "contract_or_expression": contract_or_expression,
        "entry_price": entry_price,
        "quantity": 100,
        "tactical_stop_30m": tactical_stop_30m,
        "structural_stop_daily": structural_stop_daily,
        "target_price": target_price,
        "exit_red_lines": exit_red_lines if exit_red_lines is not None else ["跌破30m中枢下沿止损"],
        "archive_symbol": symbol,
        "status": "open",
    }


class TestExtractSentinelObservations(unittest.TestCase):
    def test_commodity_cn_and_overseas_extraction(self):
        snap = _make_healthy_snapshot(
            symbol="CU0",
            price=78500.0,
            daily_zd=74000.0,
            daily_zg=77000.0,
            intra_zd=77200.0,
            intra_zg=78200.0,
            intraday_key="intraday",  # Defensive alias test
        )
        bundle = {
            "symbol": "CU0",
            "asset_class": "cn_future",
            "fundamentals_and_supply": {
                "term_structure": {
                    "status": "ok",
                    "structure": "Backwardation",
                    "annualized_roll_yield_near_pct": 6.25,
                    "annualized_roll_yield_curve_pct": 4.10,
                    "near_minus_far_spread": 850.0,
                },
                "spot_and_basis": {
                    "status": "ok",
                    "spot_price": 78900.0,
                    "dominant_basis_rate": 0.0051,
                    "dominant_basis_rate_pct": 0.51,
                },
                "inventory_em": {
                    "status": "ok",
                    "latest_inventory": 124500.0,
                    "chg_5d_pct": -4.8,
                    "chg_20d_pct": -12.3,
                },
                "baltic_freight": {
                    "status": "ok",
                    "indices": {
                        "BDTI": {"latest_value": 1120.5},
                        "BCTI": {"latest_value": 640.0},
                        "BDI": {"latest_value": 1850.0},
                        "BCI": {"latest_value": 2910.0},
                        "BSI": {"latest_value": 1230.0},
                    },
                },
                "cross_market_spread": {
                    "status": "ok",
                    "group": "copper",
                    "spread_ex_vat_usd_mt": -185.4,
                },
                "cftc_cot": {
                    "status": "ok",
                    "managed_money": {"net": 42100, "net_pct_oi": 15.6},
                },
            },
            "sources_status": {
                "term_structure": {"status": "ok"},
                "inventory_em": {"status": "ok"},
            },
        }
        obs = ds.extract_sentinel_observations(bundle, snap, ref_date=date(2026, 11, 23))
        self.assertEqual(obs["close"], 78500.0)
        self.assertEqual(obs["atr14"], 3.2)
        self.assertEqual(obs["return_1d_pct"], 1.15)
        self.assertEqual(obs["return_5d_pct"], 4.2)
        self.assertEqual(obs["daily_zd"], 74000.0)
        self.assertEqual(obs["daily_zg"], 77000.0)
        self.assertEqual(obs["intraday_30m_zd"], 77200.0)
        self.assertEqual(obs["intraday_30m_zg"], 78200.0)
        self.assertIn("资金方向一致", obs["daily_signal"])
        self.assertIn("30min", obs["intraday_30m_signal"])
        self.assertEqual(obs["curve_structure"], "Backwardation")
        self.assertEqual(obs["roll_yield_near_pct"], 6.25)
        self.assertEqual(obs["roll_yield_curve_pct"], 4.10)
        self.assertEqual(obs["front_far_spread"], 850.0)
        self.assertEqual(obs["basis_pct"], 0.51)
        self.assertEqual(obs["spot_price"], 78900.0)
        self.assertEqual(obs["inventory_latest"], 124500.0)
        self.assertEqual(obs["inventory_chg_pct"], -4.8)
        self.assertEqual(obs["bdti"], 1120.5)
        self.assertEqual(obs["bcti"], 640.0)
        self.assertEqual(obs["bdi"], 1850.0)
        self.assertEqual(obs["bci"], 2910.0)
        self.assertEqual(obs["bsi"], 1230.0)
        self.assertEqual(obs["cross_spread_primary"], -185.4)
        self.assertEqual(obs["cftc_mm_net"], 42100)
        self.assertEqual(obs["cftc_mm_net_pct_oi"], 15.6)
        self.assertEqual(obs["data_health_warnings"], [])

    def test_fx_ashare_us_and_crypto_extraction(self):
        ref = date(2026, 11, 23)
        # FX
        fx_bundle = {
            "symbol": "USDJPY=X",
            "fundamentals_and_supply": {
                "rate_differential": {
                    "status": "ok",
                    "base_minus_quote_10y_bp": 315.5,
                    "base_minus_quote_2y_bp": 290.0,
                },
                "cftc_fx_cot": {
                    "status": "ok",
                    "leveraged_funds": {"net": -65000},
                    "pair_aligned_lev_money_net": 65000,
                },
            },
        }
        fx_obs = ds.extract_sentinel_observations(fx_bundle, None, ref_date=ref)
        self.assertEqual(fx_obs["rate_diff_10y_bp"], 315.5)
        self.assertEqual(fx_obs["rate_diff_2y_bp"], 290.0)
        self.assertEqual(fx_obs["tff_lev_money_net"], -65000)
        self.assertEqual(fx_obs["tff_pair_aligned_net"], 65000)

        # A-share 5Y valuation
        cn_bundle = {
            "symbol": "600519.SS",
            "fundamentals_and_supply": {
                "valuation_5y_em": {
                    "status": "ok",
                    "pe_ttm_5y_percentile": 18.4,
                    "pb_5y_percentile": 12.1,
                    "pe_ttm": 21.5,
                    "pb": 6.8,
                }
            },
        }
        cn_obs = ds.extract_sentinel_observations(cn_bundle, None, ref_date=ref)
        self.assertEqual(cn_obs["pe_ttm_pctile_5y"], 18.4)
        self.assertEqual(cn_obs["pb_pctile_5y"], 12.1)
        self.assertEqual(cn_obs["pe_ttm"], 21.5)
        self.assertEqual(cn_obs["pb"], 6.8)

        # US stock consensus + earnings countdown
        us_bundle = {
            "symbol": "PWR",
            "fundamentals_and_supply": {
                "yfinance_consensus": {
                    "status": "ok",
                    "valuation_and_positioning": {
                        "forward_pe": 31.2,
                        "short_percent_of_float": 0.0245,
                        "next_earnings_date": "2026-11-26",
                    },
                    "analyst_price_targets": {"mean": 420.5},
                }
            },
        }
        us_obs = ds.extract_sentinel_observations(us_bundle, None, ref_date=ref)
        self.assertEqual(us_obs["forward_pe"], 31.2)
        self.assertEqual(us_obs["short_pct_float"], 0.0245)
        self.assertEqual(us_obs["target_mean_price"], 420.5)
        self.assertEqual(us_obs["next_earnings_date"], "2026-11-26")
        self.assertEqual(us_obs["days_to_earnings"], 3)

        # Crypto ETF + stablecoins + perp funding median
        crypto_snap = _make_healthy_snapshot(symbol="BTC-USD", price=95000.0)
        crypto_snap["crypto_perp_funding"] = {
            "okx": {"funding_annualized_pct": 10.5},
            "bybit": {"error": "timeout"},
            "binance": {"funding_annualized_pct": 14.5},
        }
        crypto_bundle = {
            "symbol": "BTC-USD",
            "fundamentals_and_supply": {
                "farside_btc_etf": {
                    "status": "ok",
                    "rolling_5d_usd_m": 1250.4,
                    "rolling_20d_usd_m": 3890.2,
                },
                "stablecoins_defillama": {
                    "status": "ok",
                    "total_change_7d_pct": 1.85,
                },
            },
        }
        cr_obs = ds.extract_sentinel_observations(crypto_bundle, crypto_snap, ref_date=ref)
        self.assertEqual(cr_obs["etf_flow_5d_m"], 1250.4)
        self.assertEqual(cr_obs["etf_flow_20d_m"], 3890.2)
        self.assertEqual(cr_obs["stablecoin_chg_7d_pct"], 1.85)
        self.assertEqual(cr_obs["perp_funding_ann_pct"], 12.5)

    def test_partial_error_stale_sources_and_stale_intraday_warning(self):
        snap = _make_healthy_snapshot(
            symbol="CL=F",
            price=72.0,
            daily_bar="2026-11-23",
            intraday_bar="2026-11-20 14:00",  # Intraday lags daily!
        )
        snap["technicals"]["last_close"] = 74.5
        bundle = {
            "symbol": "CL=F",
            "sources": {
                "term_structure": {
                    "status": "partial",
                    "structure": "Backwardation",
                    "annualized_roll_yield_near_pct": 8.0,
                    "front_far_spread": 2.4,
                    "quote_date_mismatch_warning": "预期活跃近月缺失",
                },
                "baltic_freight": {"status": "error", "error": "HTTP 503"},
                "cftc_disagg": {"status": "stale", "reason": "report older than 14d"},
            },
            "sources_status": {
                "term_structure": {"status": "partial"},
                "baltic_freight": {"status": "error", "error": "HTTP 503"},
                "cftc_disagg": {"status": "stale", "reason": "report older than 14d"},
            },
        }
        obs = ds.extract_sentinel_observations(bundle, snap, ref_date=date(2026, 11, 23))
        # Must fallback to fresher daily close 74.5 instead of lagging 30m 72.0
        self.assertEqual(obs["close"], 74.5)
        # Partial term_structure data is still extracted
        self.assertEqual(obs["roll_yield_near_pct"], 8.0)
        self.assertEqual(obs["front_far_spread"], 2.4)
        # Errored freight is not extracted
        self.assertNotIn("bdti", obs)
        warns_text = " | ".join(obs["data_health_warnings"])
        self.assertIn("term_structure", warns_text)
        self.assertIn("baltic_freight", warns_text)
        self.assertIn("cftc_disagg", warns_text)
        self.assertIn("intraday_30m", warns_text)

        # Must never crash on completely None inputs
        empty_obs = ds.extract_sentinel_observations(None, None)
        self.assertIsInstance(empty_obs["data_health_warnings"], list)

    def test_cross_market_spread_all_groups_and_eth_etf(self):
        cases = [
            ("CL=F", {"group": "crude_oil", "brent_minus_wti_usd_bbl": 4.2, "sc_minus_brent_usd_bbl": 1.1}, 4.2),
            ("SC0", {"group": "crude_oil", "brent_minus_wti_usd_bbl": 4.2, "sc_minus_brent_usd_bbl": 1.1}, 1.1),
            ("AU0", {"group": "gold", "shfe_minus_comex_cny_g": 8.35}, 8.35),
            ("AG0", {"group": "silver", "shfe_minus_comex_tax_incl_cny_kg": -120.5}, -120.5),
            ("SR0", {"group": "sugar", "czce_minus_ice_raw_fob_cny_mt": 1620.0}, 1620.0),
            ("CF0", {"group": "cotton", "czce_minus_ice_futures_cny_mt": 2840.0}, 2840.0),
        ]
        for sym, cms_payload, expected in cases:
            obs = ds.extract_sentinel_observations(
                {"symbol": sym, "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", **cms_payload}}},
                None,
                ref_date=date(2026, 11, 23),
            )
            self.assertEqual(obs["cross_spread_primary"], expected, msg=f"Failed for {sym}")

        eth_obs = ds.extract_sentinel_observations(
            {
                "symbol": "ETH-USD",
                "fundamentals_and_supply": {
                    "farside_eth_etf": {"status": "ok", "rolling_5d_usd_m": -85.2, "rolling_20d_usd_m": 210.4}
                },
            },
            None,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(eth_obs["etf_flow_5d_m"], -85.2)
        self.assertEqual(eth_obs["etf_flow_20d_m"], 210.4)


class TestEvaluateHoldingHealth(unittest.TestCase):
    def test_green_hold_when_aligned_and_governed(self):
        snap = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0, intra_zg=108.0)
        pos = _make_base_position(symbol="PWR", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0)
        state = {
            "status": "holding",
            "direction_bias": "long",
            "triggers": [
                {"id": "t_close", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 95.0, "action": "破日线中枢撤离"},
            ],
            "events": [
                {"date": "2026-12-20", "title": "远期股东会", "status": "upcoming"},
            ],
        }
        res = ds.evaluate_holding_health(
            symbol="PWR",
            direction="long",
            position=pos,
            archive_state=state,
            snapshot=snap,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res["status"], ds.STATUS_GREEN)
        self.assertEqual(res["status_zh"], "🟢 稳固持有")
        self.assertEqual(res["unrealized_pnl_pct"], 10.0)
        self.assertEqual(res["red_alerts"], [])
        self.assertEqual(res["yellow_alerts"], [])
        # intra_zd=101 <= tactical_stop_30m=102 -> no trail stop
        self.assertIsNone(res["suggested_trail_stop"])

    def test_red_exit_on_30m_tactical_stop_breach_long_and_short(self):
        # Long breach
        snap_long = _make_healthy_snapshot(symbol="PWR", price=101.5, daily_bias="long", intraday_bias="long")
        pos_long = _make_base_position(symbol="PWR", direction="long", tactical_stop_30m=102.0, structural_stop_daily=95.0)
        res_long = ds.evaluate_holding_health(
            "PWR", "long", position=pos_long, snapshot=snap_long, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_long["status"], ds.STATUS_RED)
        codes_l = {a["code"] for a in res_long["red_alerts"]}
        self.assertIn("tactical_stop_30m_breached", codes_l)
        self.assertNotIn("structural_stop_daily_breached", codes_l)

        # Short breach (also test fallback to archive_state["chanlun_anchors"])
        snap_short = _make_healthy_snapshot(
            symbol="CL=F", price=76.0, daily_zd=70.0, daily_zg=74.0, intra_zd=71.0, intra_zg=73.5,
            daily_bias="short", intraday_bias="short"
        )
        state_short = {
            "status": "holding",
            "direction_bias": "short",
            "chanlun_anchors": {"tactical_stop": 75.5, "structural_invalidation": 80.0},
            "triggers": [{"id": "t1", "kind": "chanlun", "metric": "close", "operator": ">=", "threshold": 80.0, "action": "止损"}],
        }
        res_short = ds.evaluate_holding_health(
            "CL=F", "short", position=None, archive_state=state_short, snapshot=snap_short, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_short["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res_short["red_alerts"]})

    def test_red_exit_on_daily_structural_stop_breach(self):
        snap = _make_healthy_snapshot(symbol="PWR", price=94.0, daily_bias="long", intraday_bias="long")
        pos = _make_base_position(symbol="PWR", direction="long", tactical_stop_30m=102.0, structural_stop_daily=95.0)
        res = ds.evaluate_holding_health("PWR", "long", position=pos, snapshot=snap, ref_date=date(2026, 11, 23))
        self.assertEqual(res["status"], ds.STATUS_RED)
        codes = {a["code"] for a in res["red_alerts"]}
        self.assertIn("structural_stop_daily_breached", codes)
        self.assertIn("tactical_stop_30m_breached", codes)

    def test_red_exit_on_daily_direction_reversal_and_non_divergence_guard(self):
        # Case 1: dual_alignment daily_bias reversed + confirmed 3-sell against long
        snap_rev = _make_healthy_snapshot(
            symbol="PWR",
            price=108.0,
            daily_bias="short",
            intraday_bias="long",
            daily_signals=["日线首中枢三卖确认(反抽不过ZD=112+顶分型)"],
        )
        pos = _make_base_position(symbol="PWR", direction="long", tactical_stop_30m=102.0, structural_stop_daily=95.0)
        res = ds.evaluate_holding_health("PWR", "long", position=pos, snapshot=snap_rev, ref_date=date(2026, 11, 23))
        self.assertEqual(res["status"], ds.STATUS_RED)
        self.assertIn("daily_direction_reversed", {a["code"] for a in res["red_alerts"]})

        # Case 2: Substring guard — "非背驰背离(急跌...勿猜1买)" is BEARISH, so a SHORT holding must remain GREEN_HOLD!
        snap_short_ok = _make_healthy_snapshot(
            symbol="CU0",
            price=72000.0,
            daily_zd=74000.0,
            daily_zg=76000.0,
            intra_zd=73000.0,
            intra_zg=74000.0,
            daily_bias="short",
            intraday_bias="short",
            daily_signals=["日线非背驰背离(急跌2类笔·斜率2.4x,area=0.55x·仍有空头力量勿猜1买)"],
            daily_momentum={
                "active_direction": "down",
                "verdict": "non_divergence_down",
                "is_capital_consensus": False,
                "is_divergence_candidate": False,
                "is_non_divergence_power": True,
            },
        )
        pos_short = _make_base_position(
            symbol="CU0",
            direction="short",
            entry_price=75000.0,
            tactical_stop_30m=74500.0,
            structural_stop_daily=76500.0,
            target_price=68000.0,
        )
        res_short = ds.evaluate_holding_health(
            "CU0",
            "short",
            position=pos_short,
            archive_state=_make_armed_state(direction="short", stop_threshold=76500.0),
            snapshot=snap_short_ok,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res_short["status"], ds.STATUS_GREEN)

    def test_red_exit_on_core_logic_trigger_and_yellow_on_early_indicator_trigger(self):
        snap = _make_healthy_snapshot(symbol="CL=F", price=75.0, daily_zd=70.0, daily_zg=73.0, intra_zd=71.0, intra_zg=74.0)
        pos = _make_base_position(
            symbol="CL=F", direction="long", entry_price=72.0, tactical_stop_30m=71.5, structural_stop_daily=69.0
        )
        # Core logic (supply_demand) trigger fires -> RED_EXIT
        bundle_red = {
            "symbol": "CL=F",
            "fundamentals_and_supply": {
                "inventory_em": {"status": "ok", "latest_inventory": 500.0, "chg_5d_pct": 18.5},
            },
        }
        state_red = {
            "triggers": [
                {
                    "id": "t_sd",
                    "kind": "supply_demand",
                    "metric": "inventory_chg_pct",
                    "operator": ">=",
                    "threshold": 15.0,
                    "action": "库存大幅累库，核心紧缺逻辑证伪",
                }
            ]
        }
        res_red = ds.evaluate_holding_health(
            "CL=F", "long", position=pos, archive_state=state_red, snapshot=snap, research_bundle=bundle_red, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_red["status"], ds.STATUS_RED)
        self.assertIn("trigger_red_t_sd", {a["code"] for a in res_red["red_alerts"]})

        # Early leading indicator (freight) trigger without exit keywords -> YELLOW_LOOSENING
        bundle_yel = {
            "symbol": "CL=F",
            "fundamentals_and_supply": {
                "baltic_freight": {"status": "ok", "indices": {"BDTI": {"latest_value": 820.0}}},
            },
        }
        state_yel = {
            "triggers": [
                {
                    "id": "t_freight",
                    "kind": "freight",
                    "metric": "bdti",
                    "operator": "<",
                    "threshold": 900.0,
                    "action": "油轮运价走弱，密切跟踪现货实货紧度",
                }
            ]
        }
        res_yel = ds.evaluate_holding_health(
            "CL=F", "long", position=pos, archive_state=state_yel, snapshot=snap, research_bundle=bundle_yel, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_yel["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_yel["red_alerts"], [])
        self.assertIn("trigger_yellow_t_freight", {a["code"] for a in res_yel["yellow_alerts"]})

    def test_yellow_on_30m_weakening_and_lesson11_reverse_k(self):
        # 30m Lesson 11 reverse-K triggered on an up stroke against a LONG holding
        snap = _make_healthy_snapshot(
            symbol="PWR",
            price=110.0,
            daily_bias="long",
            intraday_bias="long",
            intraday_stroke_trigger={
                "direction": "up",
                "trigger_kind": "break_reverse_yin_low",
                "level": 110.5,
                "triggered": True,
                "description": "已下破上行笔内最近反向阴线低点 110.5",
            },
        )
        pos = _make_base_position(symbol="PWR", direction="long", tactical_stop_30m=102.0, structural_stop_daily=95.0)
        res = ds.evaluate_holding_health("PWR", "long", position=pos, snapshot=snap, ref_date=date(2026, 11, 23))
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_weakening", {a["code"] for a in res["yellow_alerts"]})

    def test_yellow_on_missing_governance_or_missing_price_never_false_green(self):
        # Missing triggers AND exit_red_lines -> YELLOW
        snap = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        pos_no_redlines = _make_base_position(symbol="PWR", exit_red_lines=[])
        res_gov = ds.evaluate_holding_health(
            "PWR", "long", position=pos_no_redlines, archive_state={"triggers": []}, snapshot=snap, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_gov["status"], ds.STATUS_YELLOW)
        self.assertIn("no_exit_red_lines", {a["code"] for a in res_gov["yellow_alerts"]})

        # Missing both stop levels -> YELLOW
        pos_no_stops = _make_base_position(symbol="PWR", tactical_stop_30m=None, structural_stop_daily=None)  # type: ignore[arg-type]
        pos_no_stops["tactical_stop_30m"] = None
        pos_no_stops["structural_stop_daily"] = None
        res_nostop = ds.evaluate_holding_health("PWR", "long", position=pos_no_stops, snapshot=snap, ref_date=date(2026, 11, 23))
        self.assertEqual(res_nostop["status"], ds.STATUS_YELLOW)
        self.assertIn("missing_stop_levels", {a["code"] for a in res_nostop["yellow_alerts"]})

        # Missing / errored price feed -> YELLOW (never false green!)
        pos_ok = _make_base_position(symbol="PWR")
        res_nopx = ds.evaluate_holding_health(
            "PWR", "long", position=pos_ok, snapshot={"error": "yfinance timeout"}, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_nopx["status"], ds.STATUS_YELLOW)
        self.assertIn("price_feed_missing", {a["code"] for a in res_nopx["yellow_alerts"]})
        self.assertTrue(len(res_nopx["data_warnings"]) >= 1)

    def test_trailing_stop_suggestion_strict_profit_and_tightening(self):
        # Long in profit + intra_zd (106) > tactical_stop (102) and < price (112) -> emitted!
        snap_long_win = _make_healthy_snapshot(symbol="PWR", price=112.0, intra_zd=106.0, intra_zg=110.0)
        pos_long = _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0)
        res_win = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_long,
            archive_state=_make_armed_state("long", 95.0),
            snapshot=snap_long_win,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res_win["status"], ds.STATUS_GREEN)
        self.assertIsNotNone(res_win["suggested_trail_stop"])
        self.assertEqual(res_win["suggested_trail_stop"]["suggested_stop"], 106.0)
        self.assertEqual(res_win["suggested_trail_stop"]["anchor"], "30m_zhongshu_zd")

        # Long underwater (price 99 < entry 100) -> MUST NOT emit even if intra_zd > stop!
        snap_under = _make_healthy_snapshot(symbol="PWR", price=99.0, intra_zd=97.0, intra_zg=98.5)
        pos_under = _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=96.0, structural_stop_daily=92.0)
        res_under = ds.evaluate_holding_health(
            "PWR",
            "long",
            position=pos_under,
            archive_state=_make_armed_state("long", 92.0),
            snapshot=snap_under,
            ref_date=date(2026, 11, 23),
        )
        self.assertIsNone(res_under["suggested_trail_stop"])

        # Short in profit + intra_zg (72.5) < tactical_stop (75.0) and > price (70.0) -> emitted!
        snap_short_win = _make_healthy_snapshot(
            symbol="CL=F", price=70.0, daily_zd=73.0, daily_zg=76.0, intra_zd=70.5, intra_zg=72.5,
            daily_bias="short", intraday_bias="short"
        )
        pos_short = _make_base_position(
            symbol="CL=F", direction="short", entry_price=74.0, tactical_stop_30m=75.0, structural_stop_daily=78.0, target_price=64.0
        )
        res_swin = ds.evaluate_holding_health(
            "CL=F",
            "short",
            position=pos_short,
            archive_state=_make_armed_state("short", 78.0),
            snapshot=snap_short_win,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res_swin["status"], ds.STATUS_GREEN)
        self.assertIsNotNone(res_swin["suggested_trail_stop"])
        self.assertEqual(res_swin["suggested_trail_stop"]["suggested_stop"], 72.5)
        self.assertEqual(res_swin["suggested_trail_stop"]["anchor"], "30m_zhongshu_zg")

    def test_commodity_short_backwardation_watch(self):
        snap_base = _make_healthy_snapshot(
            symbol="CU0", price=73000.0, daily_zd=74000.0, daily_zg=76000.0,
            intra_zd=73200.0, intra_zg=74500.0, daily_bias="short", intraday_bias="short"
        )
        snap_contract = _make_healthy_snapshot(
            symbol="CU2703", price=73000.0, daily_zd=74000.0, daily_zg=76000.0,
            intra_zd=73200.0, intra_zg=74500.0, daily_bias="short", intraday_bias="short"
        )
        pos = _make_base_position(
            symbol="CU0", direction="short", entry_price=75000.0, tactical_stop_30m=75200.0,
            structural_stop_daily=77000.0, target_price=68000.0, contract_or_expression="CU2703 空单"
        )
        pos["asset_class"] = "cn_future"
        pos["baseline_roll_yield_near_pct"] = 8.0
        armed = _make_armed_state("short", 77000.0)

        # Narrowed B-structure (8% -> 4%) -> GREEN + confirmation info note
        bundle_narrow = {
            "symbol": "CU0",
            "fundamentals_and_supply": {
                "term_structure": {"status": "ok", "structure": "Backwardation", "annualized_roll_yield_near_pct": 4.0}
            },
        }
        res_n = ds.evaluate_holding_health(
            "CU0",
            "short",
            position=pos,
            archive_state=armed,
            snapshot=snap_base,
            contract_snapshot=snap_contract,
            research_bundle=bundle_narrow,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res_n["status"], ds.STATUS_GREEN)
        self.assertTrue(any("收窄" in n for n in res_n["info_notes"]))

        # Violently widened B-structure (8% -> 16%) -> YELLOW squeeze warning
        bundle_wide = {
            "symbol": "CU0",
            "fundamentals_and_supply": {
                "term_structure": {"status": "ok", "structure": "Backwardation", "annualized_roll_yield_near_pct": 16.0}
            },
        }
        res_w = ds.evaluate_holding_health(
            "CU0",
            "short",
            position=pos,
            archive_state=armed,
            snapshot=snap_base,
            contract_snapshot=snap_contract,
            research_bundle=bundle_wide,
            ref_date=date(2026, 11, 23),
        )
        self.assertEqual(res_w["status"], ds.STATUS_YELLOW)
        self.assertIn("backwardation_squeeze_warning", {a["code"] for a in res_w["yellow_alerts"]})

    def test_upcoming_events_earnings_and_futures_roll_window(self):
        ref = date(2026, 11, 23)
        snap = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        pos = _make_base_position(symbol="PWR")

        # Imminent earnings (2 days away) -> YELLOW event_imminent
        bundle_imm = {
            "symbol": "PWR",
            "fundamentals_and_supply": {
                "yfinance_consensus": {
                    "status": "ok",
                    "valuation_and_positioning": {"next_earnings_date": "2026-11-25"},
                }
            },
        }
        res_imm = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=_make_armed_state("long", 95.0),
            snapshot=snap, research_bundle=bundle_imm, ref_date=ref
        )
        self.assertEqual(res_imm["status"], ds.STATUS_YELLOW)
        self.assertIn("event_imminent", {a["code"] for a in res_imm["yellow_alerts"]})
        self.assertEqual(res_imm["upcoming_events"][0]["days_until"], 2)

        # Event 10 days away (4-14d) -> info only, stays GREEN
        state_10d = _make_armed_state(
            "long",
            95.0,
            events=[
                {"date": "2026-12-03", "title": "OPEC+ 部长级会议", "status": "upcoming"},
                {"date": "2026-11-24", "title": "已完成历史事件", "status": "done"},
            ],
        )
        res_10d = ds.evaluate_holding_health("PWR", "long", position=pos, archive_state=state_10d, snapshot=snap, ref_date=ref)
        self.assertEqual(res_10d["status"], ds.STATUS_GREEN)
        self.assertEqual(len(res_10d["upcoming_events"]), 1)
        self.assertFalse(res_10d["upcoming_events"][0]["imminent"])
        self.assertTrue(any("OPEC+" in n for n in res_10d["info_notes"]))

        # Futures contract roll alert: CLF27 (Jan 2027 WTI) expires 3 US bdays before Dec 25, 2026 -> Dec 21, 2026
        # Set ref_date to 2026-12-19 (2 days before LTD 2026-12-21) -> imminent roll YELLOW!
        pos_fut = _make_base_position(
            symbol="CL=F", direction="long", entry_price=70.0, tactical_stop_30m=68.0, structural_stop_daily=65.0,
            contract_or_expression="做多 CLF27.NYM 近月主力"
        )
        snap_fut_base = _make_healthy_snapshot(
            symbol="CL=F", price=75.0, daily_zd=66.0, daily_zg=72.0, intra_zd=67.0, intra_zg=73.0,
            daily_bar="2026-12-18", intraday_bar="2026-12-18 15:30"
        )
        snap_fut_ct = _make_healthy_snapshot(
            symbol="CLF27.NYM", price=75.0, daily_zd=66.0, daily_zg=72.0, intra_zd=67.0, intra_zg=73.0,
            daily_bar="2026-12-18", intraday_bar="2026-12-18 15:30"
        )
        res_roll = ds.evaluate_holding_health(
            "CL=F", "long", position=pos_fut, archive_state=_make_armed_state("long", 65.0),
            snapshot=snap_fut_base, contract_snapshot=snap_fut_ct, ref_date=date(2026, 12, 19)
        )
        self.assertEqual(res_roll["status"], ds.STATUS_YELLOW)
        self.assertTrue(any(e["source"] == "futures_roll_window" and e["imminent"] for e in res_roll["upcoming_events"]))

    def test_stop_proximity_daily_loosening_and_worker1_dual_alignment_actions(self):
        # 1) Stop proximity <= 1.0% -> YELLOW stop_proximity_warning
        snap_prox = _make_healthy_snapshot(symbol="PWR", price=102.8, intra_zd=100.0, atr14_30m=0.8)
        pos_prox = _make_base_position(symbol="PWR", direction="long", tactical_stop_30m=102.0, structural_stop_daily=95.0)
        res_prox = ds.evaluate_holding_health("PWR", "long", position=pos_prox, snapshot=snap_prox, ref_date=date(2026, 11, 23))
        self.assertEqual(res_prox["status"], ds.STATUS_YELLOW)
        self.assertIn("stop_proximity_warning", {a["code"] for a in res_prox["yellow_alerts"]})

        # 2) Daily loosening (准三卖观察 / 非首中枢三买) -> YELLOW daily_structure_loosening
        snap_loose = _make_healthy_snapshot(
            symbol="PWR",
            price=112.0,
            intra_zd=101.0,
            daily_bias="long",
            intraday_bias="long",
            daily_signals=["日线第2个中枢·非首中枢三买确认(回踩不破ZG=105+底分型)"],
            daily_momentum={
                "active_direction": "up",
                "verdict": "neutral_extension_up",
                "is_capital_consensus": False,
                "is_divergence_candidate": False,
                "is_non_divergence_power": False,
            },
        )
        res_loose = ds.evaluate_holding_health("PWR", "long", position=pos_prox, snapshot=snap_loose, ref_date=date(2026, 11, 23))
        self.assertEqual(res_loose["status"], ds.STATUS_YELLOW)
        self.assertIn("daily_structure_loosening", {a["code"] for a in res_loose["yellow_alerts"]})

        # 3) Worker 1 dual_alignment exact action enums: entry_short / exhaustion_warning -> YELLOW; wait_structure -> GREEN
        snap_w1_short = _make_healthy_snapshot(
            symbol="PWR", price=112.0, intra_zd=101.0, daily_bias="long", intraday_bias="neutral", intraday_action="entry_short"
        )
        res_w1_s = ds.evaluate_holding_health("PWR", "long", position=pos_prox, snapshot=snap_w1_short, ref_date=date(2026, 11, 23))
        self.assertEqual(res_w1_s["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_weakening", {a["code"] for a in res_w1_s["yellow_alerts"]})

        snap_w1_exh = _make_healthy_snapshot(
            symbol="PWR", price=112.0, intra_zd=101.0, daily_bias="long", intraday_bias="long", intraday_action="exhaustion_warning"
        )
        res_w1_e = ds.evaluate_holding_health("PWR", "long", position=pos_prox, snapshot=snap_w1_exh, ref_date=date(2026, 11, 23))
        self.assertEqual(res_w1_e["status"], ds.STATUS_YELLOW)

        snap_w1_wait = _make_healthy_snapshot(
            symbol="PWR", price=112.0, intra_zd=101.0, daily_bias="long", intraday_bias="neutral", intraday_action="wait_structure"
        )
        res_w1_w = ds.evaluate_holding_health(
            "PWR", "long", position=pos_prox, archive_state=_make_armed_state("long", 95.0),
            snapshot=snap_w1_wait, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_w1_w["status"], ds.STATUS_GREEN)


class TestBatchRunnerFormatterAndCLI(unittest.TestCase):
    def test_discovery_dedup_watchlist_archive_update_and_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            port_dir = os.path.join(tmpdir, "portfolio")
            os.makedirs(port_dir, exist_ok=True)

            # 1) PWR exists in BOTH positions.json AND research/ (status="holding") -> MUST be deduplicated!
            ra.init_archive("PWR", name="Quanta Services", research_root=res_root)
            ra.update_state(
                "PWR",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "chanlun_anchors": {"tactical_stop": 102.0, "structural_invalidation": 95.0},
                    "triggers": [
                        {"id": "t_pwr_red", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 95.0, "action": "破位撤离"}
                    ],
                },
                research_root=res_root,
            )

            # 2) CL=F exists ONLY in research/ (status="holding") -> MUST still be discovered!
            ra.init_archive("CL=F", name="WTI原油", research_root=res_root)
            ra.update_state(
                "CL=F",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "chanlun_anchors": {"tactical_stop": 72.0, "structural_invalidation": 68.0},
                    "triggers": [
                        {"id": "t_cl", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 68.0, "action": "跌破止损"}
                    ],
                },
                research_root=res_root,
            )

            # 3) NVDA exists in research/ as status="watchlist" -> ONLY in watchlist section when requested!
            ra.init_archive("NVDA", name="NVIDIA", research_root=res_root)
            ra.update_state(
                "NVDA",
                {
                    "status": "watchlist",
                    "direction_bias": "long",
                    "triggers": [
                        {"id": "t_nvda_watch", "kind": "chanlun", "metric": "close", "operator": ">=", "threshold": 140.0, "action": "放量突破关注"}
                    ],
                },
                research_root=res_root,
            )

            # Write positions.json in portfolio_manager.py schema containing PWR (duplicate of archive) + closed position (ignored)
            positions_doc = {
                "schema_version": 1,
                "revision": 1,
                "positions": [
                    _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0),
                    {**_make_base_position(symbol="MSFT"), "status": "closed"},
                ],
            }
            with open(os.path.join(port_dir, "positions.json"), "w", encoding="utf-8") as fh:
                json.dump(positions_doc, fh, ensure_ascii=False, indent=2)

            # Offline fetchers: PWR breaches 30m stop (RED_EXIT), CL=F is healthy (GREEN_HOLD), NVDA fires watchlist trigger
            snaps = {
                "PWR": _make_healthy_snapshot(symbol="PWR", price=100.5, daily_bias="long", intraday_bias="long"),
                "CL=F": _make_healthy_snapshot(symbol="CL=F", price=76.0, daily_zd=69.0, daily_zg=73.0, intra_zd=71.0, intra_zg=74.5),
                "NVDA": _make_healthy_snapshot(symbol="NVDA", price=145.0, daily_zd=125.0, daily_zg=138.0),
            }

            report = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                include_watchlist=True,
                skip_research=True,
                update_archive=True,
                log_green=False,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=lambda s: snaps[s],
            )

            # Deduplication check: exactly 2 holdings (PWR + CL=F), NOT 3!
            self.assertEqual(report["summary"]["holdings_total"], 2)
            self.assertEqual(report["summary"]["red_count"], 1)
            self.assertEqual(report["summary"]["green_count"], 1)
            self.assertEqual(report["summary"]["watchlist_total"], 1)
            self.assertEqual(report["summary"]["watchlist_fired_count"], 1)

            # Sorted RED first, then GREEN
            self.assertEqual([h["symbol"] for h in report["holdings"]], ["PWR", "CL=F"])
            self.assertEqual(report["holdings"][0]["status"], ds.STATUS_RED)
            self.assertEqual(report["holdings"][0]["discovery_source"], "portfolio+archive")
            self.assertEqual(report["holdings"][1]["status"], ds.STATUS_GREEN)
            self.assertEqual(report["holdings"][1]["discovery_source"], "archive_holding")

            # Archive log appended for RED (PWR), NOT appended for GREEN (CL=F) because log_green=False
            pwr_thesis = ra.load_archive("PWR", research_root=res_root)["thesis_md"]
            cl_thesis = ra.load_archive("CL=F", research_root=res_root)["thesis_md"]
            self.assertIn("每日哨兵", pwr_thesis)
            self.assertNotIn("每日哨兵", cl_thesis)

            # Markdown formatting check
            md = ds.format_sentinel_markdown(report)
            self.assertIn("【持仓每日强制巡检】", md)
            self.assertIn("【观察池轻量刷新】", md)
            self.assertIn("PWR", md)
            self.assertIn("CL=F", md)
            self.assertIn("NVDA", md)
            self.assertIn("果断撤离", md)

            # CLI exit code test: --fail-on-red returns 2 when RED present, 0 when all GREEN
            buf_red = io.StringIO()
            with (
                mock.patch.object(ds, "_default_snapshot_fetcher", side_effect=lambda s: snaps[s]),
                contextlib.redirect_stdout(buf_red),
            ):
                rc_red = ds.main([
                    "--portfolio-dir", port_dir,
                    "--research-root", res_root,
                    "--skip-research",
                    "--format", "json",
                    "--fail-on-red",
                    "--ref-date", "2026-11-23",
                ])
            self.assertEqual(rc_red, 2)
            parsed_cli = json.loads(buf_red.getvalue())
            self.assertEqual(parsed_cli["summary"]["red_count"], 1)

            # Now make PWR healthy too -> --fail-on-red returns 0, and --update-archive --log-green logs CL=F
            snaps_green = dict(snaps)
            snaps_green["PWR"] = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
            buf_ok = io.StringIO()
            with (
                mock.patch.object(ds, "_default_snapshot_fetcher", side_effect=lambda s: snaps_green[s]),
                contextlib.redirect_stdout(buf_ok),
            ):
                rc_ok = ds.main([
                    "--portfolio-dir", port_dir,
                    "--research-root", res_root,
                    "--skip-research",
                    "--format", "markdown",
                    "--fail-on-red",
                    "--update-archive",
                    "--log-green",
                    "--ref-date", "2026-11-23",
                ])
            self.assertEqual(rc_ok, 0)
            self.assertIn("【持仓每日强制巡检】", buf_ok.getvalue())
            self.assertIn("每日哨兵", ra.load_archive("CL=F", research_root=res_root)["thesis_md"])

    def test_missing_positions_file_still_monitors_research_holdings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            missing_port = os.path.join(tmpdir, "nonexistent_portfolio")
            ra.init_archive("GC=F", name="黄金", research_root=res_root)
            ra.update_state(
                "GC=F",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "chanlun_anchors": {"tactical_stop": 2600.0, "structural_invalidation": 2520.0},
                    "triggers": [{"id": "t_gc", "kind": "chanlun", "metric": "close", "operator": "<", "threshold": 2520.0, "action": "撤离"}],
                },
                research_root=res_root,
            )
            snap_gc = _make_healthy_snapshot(symbol="GC=F", price=2710.0, daily_zd=2550.0, daily_zg=2650.0, intra_zd=2580.0, intra_zg=2690.0)
            report = ds.run_daily_sentinel(
                portfolio_dir=missing_port,
                research_root=res_root,
                skip_research=True,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=lambda _s: snap_gc,
            )
            self.assertEqual(report["summary"]["holdings_total"], 1)
            self.assertEqual(report["holdings"][0]["symbol"], "GC=F")
            self.assertEqual(report["holdings"][0]["status"], ds.STATUS_GREEN)

    def test_portfolio_manager_real_schema_avg_entry_and_contract_symbol(self):
        # Verify exact field compatibility with Worker 2's portfolio_manager.py records
        pm_position = {
            "position_id": "pos_20261219_cl",
            "symbol": "CL=F",
            "asset_class": "commodity",
            "direction": "long",
            "status": "open",
            "sector_theme": "energy_crude",
            "leader_role": "sole_leader",
            "contract_symbol": "CLF27.NYM",
            "quantity": 2,
            "avg_entry_price": 70.0,
            "initial_entry_price": 70.0,
            "tactical_stop_30m": 68.0,
            "structural_stop_daily": 65.0,
            "target_price": 85.0,
            "exit_red_lines": ["跌破30m战术止损"],
        }
        snap_base = _make_healthy_snapshot(
            symbol="CL=F", price=77.0, daily_zd=66.0, daily_zg=72.0, intra_zd=71.0, intra_zg=75.0,
            daily_bar="2026-12-18", intraday_bar="2026-12-18 15:30"
        )
        snap_ct = _make_healthy_snapshot(
            symbol="CLF27.NYM", price=77.0, daily_zd=66.0, daily_zg=72.0, intra_zd=71.0, intra_zg=75.0,
            daily_bar="2026-12-18", intraday_bar="2026-12-18 15:30"
        )
        res = ds.evaluate_holding_health(
            "CL=F",
            "long",
            position=pm_position,
            archive_state=_make_armed_state("long", 65.0),
            snapshot=snap_base,
            contract_snapshot=snap_ct,
            ref_date=date(2026, 12, 19),
        )
        # avg_entry_price=70, price=77 -> +10.0% PnL and trailing stop suggested (71 > 68)
        self.assertEqual(res["entry_price"], 70.0)
        self.assertEqual(res["unrealized_pnl_pct"], 10.0)
        self.assertIsNotNone(res["suggested_trail_stop"])
        self.assertEqual(res["suggested_trail_stop"]["suggested_stop"], 71.0)
        # contract_symbol="CLF27.NYM" -> imminent roll alert (2026-12-21 is 2 days after 2026-12-19)
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        self.assertTrue(any(e["source"] == "futures_roll_window" and e["imminent"] for e in res["upcoming_events"]))


class TestRound1Hardening(unittest.TestCase):
    def test_positive_preconditions_zero_false_green_stale_bars_and_failed_30m(self):
        pos = _make_base_position(symbol="PWR", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0)
        state = _make_armed_state("long", 95.0)

        # 1) Stale daily bar (5 calendar days old > 4d threshold for US equity) -> YELLOW, NEVER GREEN
        snap_stale_d = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0,
            daily_bar="2026-11-18", intraday_bar="2026-11-23 15:30"
        )
        res_sd = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_stale_d, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_sd["status"], ds.STATUS_YELLOW)
        self.assertIn("daily_bar_stale", {a["code"] for a in res_sd["yellow_alerts"]})
        self.assertFalse(res_sd["green_preconditions"]["valid_fresh_price"])

        # 2) Crypto stale bar threshold is 1 day (2 days old -> YELLOW)
        snap_btc_stale = _make_healthy_snapshot(
            symbol="BTC-USD", price=95000.0, daily_zd=88000.0, daily_zg=92000.0,
            intra_zd=90000.0, intra_zg=94000.0, daily_bar="2026-11-21", intraday_bar="2026-11-21 12:00"
        )
        snap_btc_stale["asset_class"] = "crypto"
        pos_btc = _make_base_position(
            symbol="BTC-USD", entry_price=90000.0, tactical_stop_30m=91000.0, structural_stop_daily=87000.0, target_price=120000.0
        )
        pos_btc["asset_class"] = "crypto"
        res_btc = ds.evaluate_holding_health(
            "BTC-USD", "long", position=pos_btc, archive_state=_make_armed_state("long", 87000.0),
            snapshot=snap_btc_stale, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_btc["status"], ds.STATUS_YELLOW)
        self.assertIn("daily_bar_stale", {a["code"] for a in res_btc["yellow_alerts"]})

        # 3) Failed 30m (intraday_error) -> YELLOW, NEVER GREEN
        snap_err_30m = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        snap_err_30m["intraday_error"] = "30m upstream timeout"
        snap_err_30m["chanlun"].pop("intraday_30m", None)
        res_err30 = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_err_30m, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_err30["status"], ds.STATUS_YELLOW)
        codes_30 = {a["code"] for a in res_err30["yellow_alerts"]}
        self.assertIn("intraday_30m_unavailable", codes_30)
        self.assertIn("chanlun_30m_missing", codes_30)

        # 4) Missing 30m intraday_technicals -> YELLOW, NEVER GREEN
        snap_no_30m = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        snap_no_30m["intraday_technicals"] = {}
        res_no30 = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_no_30m, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_no30["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_unavailable", {a["code"] for a in res_no30["yellow_alerts"]})

        # 5) Degraded upstream source in research_bundle (data_warnings non-empty) blocks GREEN -> YELLOW
        snap_ok = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        bundle_degraded = {
            "symbol": "PWR",
            "sources_status": {"yfinance_consensus": {"status": "error", "error": "HTTP 429"}},
        }
        res_deg = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_ok,
            research_bundle=bundle_degraded, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_deg["status"], ds.STATUS_YELLOW)
        self.assertIn("data_source_degraded", {a["code"] for a in res_deg["yellow_alerts"]})

    def test_unevaluated_armed_trigger_blocks_green_and_preserves_red_priority(self):
        snap = _make_healthy_snapshot(symbol="CU0", price=73000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=74500.0, daily_bias="short", intraday_bias="short")
        pos = _make_base_position(symbol="CU0", direction="short", entry_price=75000.0, tactical_stop_30m=75200.0, structural_stop_daily=77000.0, target_price=68000.0)
        state = _make_armed_state(
            "short",
            77000.0,
            extra_triggers=[
                {
                    "id": "t_inv_missing",
                    "kind": "supply_demand",
                    "metric": "inventory_chg_pct",
                    "operator": "<=",
                    "threshold": -10.0,
                    "action": "库存去化证伪过剩逻辑撤离",
                }
            ],
        )
        # Inventory metric missing (e.g. inventory_em failed) -> evaluated=False -> MUST be YELLOW_LOOSENING!
        res_uneval = ds.evaluate_holding_health(
            "CU0", "short", position=pos, archive_state=state, snapshot=snap, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_uneval["status"], ds.STATUS_YELLOW)
        self.assertIn("unevaluated_triggers", {a["code"] for a in res_uneval["yellow_alerts"]})
        self.assertFalse(res_uneval["green_preconditions"]["all_armed_triggers_evaluated"])

        # Preserve RED_EXIT priority when price ALSO breaches stop while trigger is unevaluated!
        snap_red = _make_healthy_snapshot(symbol="CU0", price=76000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=75500.0, daily_bias="short", intraday_bias="short")
        res_red = ds.evaluate_holding_health(
            "CU0", "short", position=pos, archive_state=state, snapshot=snap_red, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_red["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_breached", {a["code"] for a in res_red["red_alerts"]})
        self.assertIn("unevaluated_triggers", {a["code"] for a in res_red["yellow_alerts"]})

    def test_manual_exit_red_lines_only_blocks_green_unless_explicitly_allowed(self):
        snap = _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0)
        pos = _make_base_position(
            symbol="PWR", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0,
            exit_red_lines=["人工盯盘：核心客户削减CapEx即清仓"]
        )
        # Without armed triggers, manual text red lines alone CANNOT yield GREEN_HOLD
        res_manual = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state={"triggers": []}, snapshot=snap, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_manual["status"], ds.STATUS_YELLOW)
        self.assertIn("manual_red_lines_only", {a["code"] for a in res_manual["yellow_alerts"]})
        self.assertEqual(res_manual["exit_red_lines"], ["人工盯盘：核心客户削减CapEx即清仓"])

        # Markdown renders exit_red_lines clearly
        md = ds.format_sentinel_markdown({"ref_date": "2026-11-23", "checked_at_utc": "now", "holdings": [res_manual], "watchlist": []})
        self.assertIn("人工盯盘：核心客户削减CapEx即清仓", md)

        # Explicitly allowing manual red lines permits GREEN_HOLD
        res_allowed = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state={"triggers": []}, snapshot=snap,
            ref_date=date(2026, 11, 23), allow_manual_red_lines=True
        )
        self.assertEqual(res_allowed["status"], ds.STATUS_GREEN)

    def test_intraday_high_low_stop_breach_and_opened_at_utc_filtering(self):
        state = _make_armed_state("long", 95.0)
        pos = _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=95.0, structural_stop_daily=92.0)

        # 1) Long stop=95, intraday low=90, close recovered to 100 -> MUST be RED_EXIT!
        snap_wick = _make_healthy_snapshot(symbol="PWR", price=100.0, daily_zd=90.0, daily_zg=98.0, intra_zd=94.0, intra_zg=99.0)
        snap_wick["intraday_technicals"]["last_bar_low"] = 90.0
        snap_wick["intraday_technicals"]["last_bar_high"] = 101.0
        res_wick = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_wick, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_wick["status"], ds.STATUS_RED)
        red_codes = {a["code"] for a in res_wick["red_alerts"]}
        self.assertIn("tactical_stop_30m_intraday_breached", red_codes)
        self.assertIn("structural_stop_daily_intraday_breached", red_codes)

        # 2) Short stop=80, intraday high=82, close recovered to 77 -> MUST be RED_EXIT!
        pos_s = _make_base_position(symbol="CL=F", direction="short", entry_price=78.0, tactical_stop_30m=80.0, structural_stop_daily=83.0, target_price=68.0)
        snap_s = _make_healthy_snapshot(
            symbol="CL=F", price=77.0, daily_zd=70.0, daily_zg=76.0, intra_zd=75.0, intra_zg=79.0,
            daily_bias="short", intraday_bias="short"
        )
        snap_s["intraday_technicals"]["recent_bars_ohlc"] = [
            {"bar": "2026-11-23 11:00", "open": 78.0, "high": 82.0, "low": 77.5, "close": 78.5},
            {"bar": "2026-11-23 15:30", "open": 78.5, "high": 79.0, "low": 76.8, "close": 77.0},
        ]
        res_s = ds.evaluate_holding_health(
            "CL=F", "short", position=pos_s, archive_state=_make_armed_state("short", 83.0),
            snapshot=snap_s, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_s["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_intraday_breached", {a["code"] for a in res_s["red_alerts"]})

        # 3) opened_at_utc filtering: pre-entry wick ignored; post-entry wick triggers RED_EXIT
        pos_timed = _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=95.0, structural_stop_daily=92.0)
        pos_timed["opened_at_utc"] = "2026-11-23 14:00:00 UTC"

        snap_pre_only = _make_healthy_snapshot(
            symbol="PWR", price=105.0, intra_zd=94.0, intra_zg=104.0,
            daily_bar="2026-11-23", intraday_bar="2026-11-23T15:30:00Z"
        )
        snap_pre_only["technicals"]["last_bar_low"] = 90.0  # Daily bar starts 00:00Z < 14:00Z (pre-entry)
        snap_pre_only["intraday_technicals"]["recent_bars_ohlc"] = [
            {"bar": "2026-11-23T10:00:00Z", "open": 98.0, "high": 99.0, "low": 90.0, "close": 97.0},  # Pre-entry
            {"bar": "2026-11-23T15:00:00Z", "open": 100.0, "high": 105.0, "low": 99.5, "close": 104.0},  # Post-entry OK
            {"bar": "2026-11-23T15:30:00Z", "open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0},
        ]
        res_pre = ds.evaluate_holding_health(
            "PWR", "long", position=pos_timed, archive_state=state, snapshot=snap_pre_only, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_pre["status"], ds.STATUS_GREEN)

        snap_post_hit = _make_healthy_snapshot(
            symbol="PWR", price=105.0, intra_zd=94.0, intra_zg=104.0,
            daily_bar="2026-11-23", intraday_bar="2026-11-23T15:30:00Z"
        )
        snap_post_hit["intraday_technicals"]["recent_bars_ohlc"] = [
            {"bar": "2026-11-23T15:00:00Z", "open": 100.0, "high": 101.0, "low": 94.5, "close": 102.0},  # Post-entry breach!
            {"bar": "2026-11-23T15:30:00Z", "open": 102.0, "high": 106.0, "low": 101.0, "close": 105.0},
        ]
        res_post = ds.evaluate_holding_health(
            "PWR", "long", position=pos_timed, archive_state=state, snapshot=snap_post_hit, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_post["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_intraday_breached", {a["code"] for a in res_post["red_alerts"]})

    def test_actual_traded_contract_quote_and_mismatch_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            port_dir = os.path.join(tmpdir, "portfolio")
            os.makedirs(port_dir, exist_ok=True)

            ra.init_archive("CU0", name="沪铜", research_root=res_root)
            ra.update_state(
                "CU0",
                {
                    "status": "holding",
                    "direction_bias": "short",
                    "triggers": [
                        {"id": "t_cu", "kind": "chanlun", "metric": "close", "operator": ">=", "threshold": 78000.0, "action": "破位止损"}
                    ],
                },
                research_root=res_root,
            )
            pos_cu = _make_base_position(
                symbol="CU0", direction="short", entry_price=75000.0,
                tactical_stop_30m=76000.0, structural_stop_daily=78000.0, target_price=68000.0,
                contract_or_expression="CU2612 远月空单"
            )
            pos_cu["asset_class"] = "cn_future"
            pos_cu["contract_symbol"] = "CU2612"
            with open(os.path.join(port_dir, "positions.json"), "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "revision": 1, "positions": [pos_cu]}, fh)

            # 1) CU2612 breaches stop (76500 >= 76000) while CU0 is still 74000 -> MUST fetch CU2612 & trigger RED_EXIT!
            fetched_snap_syms: list[str] = []
            fetched_res_syms: list[str] = []

            def _snap_fetch(s: str) -> dict:
                fetched_snap_syms.append(s)
                if s == "CU2612":
                    return _make_healthy_snapshot(
                        symbol="CU2612", price=76500.0, daily_zd=74000.0, daily_zg=77000.0,
                        intra_zd=74500.0, intra_zg=76800.0, daily_bias="short", intraday_bias="short"
                    )
                return _make_healthy_snapshot(
                    symbol="CU0", price=74000.0, daily_zd=74000.0, daily_zg=77000.0,
                    intra_zd=73500.0, intra_zg=74500.0, daily_bias="short", intraday_bias="short"
                )

            def _res_fetch(s: str) -> dict:
                fetched_res_syms.append(s)
                return {"symbol": s, "fundamentals_and_supply": {}}

            rep = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=_snap_fetch,
                research_fetcher=_res_fetch,
            )
            self.assertIn("CU2612", fetched_snap_syms)
            self.assertIn("CU0", fetched_res_syms)
            self.assertNotIn("CU2612", fetched_res_syms)
            h = rep["holdings"][0]
            self.assertEqual(h["traded_contract_symbol"], "CU2612")
            self.assertEqual(h["last_price"], 76500.0)
            self.assertEqual(h["status"], ds.STATUS_RED)
            self.assertIn("tactical_stop_30m_breached", {a["code"] for a in h["red_alerts"]})

            # 2) Contract quote mismatch (returns CU0 instead of CU2612) -> YELLOW_LOOSENING, NEVER evaluate CU0 against CU2612 stop!
            rep_mis = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                skip_research=True,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=lambda _s: _make_healthy_snapshot(
                    symbol="CU0", price=73000.0, daily_zd=74000.0, daily_zg=77000.0,
                    intra_zd=73200.0, intra_zg=74500.0, daily_bias="short", intraday_bias="short"
                ),
            )
            h_mis = rep_mis["holdings"][0]
            self.assertEqual(h_mis["status"], ds.STATUS_YELLOW)
            self.assertIsNone(h_mis["last_price"])
            self.assertIn("contract_quote_mismatch", {a["code"] for a in h_mis["yellow_alerts"]})

    def test_structured_chanlun_signal_meta_and_no_candidate_false_red(self):
        pos = _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0)
        state = _make_armed_state("long", 95.0)

        # 1) Structured candidate opposite signal (confirmed=False) -> YELLOW_LOOSENING, NOT RED_EXIT!
        snap_cand = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
            daily_signals=["日线顶分型结构(一卖候选)"],
            daily_momentum={"active_direction": "down", "verdict": "neutral_extension_down", "is_capital_consensus": False, "is_non_divergence_power": False, "is_divergence_candidate": False},
        )
        snap_cand["chanlun"]["dual_alignment"]["daily_signal_meta"] = {
            "signal_type": "candidate_1s",
            "direction": "short",
            "confirmed": False,
            "is_exhaustion": False,
            "signal_timestamp": "2026-11-23",
        }
        res_cand = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_cand, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_cand["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_cand["red_alerts"], [])
        self.assertIn("daily_structure_loosening", {a["code"] for a in res_cand["yellow_alerts"]})

        # 2) Legacy text candidate without signal_meta ("一买候选" against short / "顶分型结构(一卖候选)" against long) -> NOT RED_EXIT!
        snap_leg_cand = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
            daily_signals=["日线顶分型结构(一卖候选)", "日线疑似三卖"],
            daily_momentum={"active_direction": "down", "verdict": "neutral_extension_down", "is_capital_consensus": False, "is_non_divergence_power": False, "is_divergence_candidate": False},
        )
        res_leg = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_leg_cand, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_leg["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_leg["red_alerts"], [])

        # 3) Structured confirmed opposite signal (confirmed=True) -> RED_EXIT!
        snap_conf = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
            daily_signals=["日线首中枢三卖确认"],
            daily_momentum={"active_direction": "down", "verdict": "neutral_extension_down", "is_capital_consensus": False, "is_non_divergence_power": False, "is_divergence_candidate": False},
        )
        snap_conf["chanlun"]["dual_alignment"]["daily_signal_meta"] = {
            "signal_type": "confirmed_3s",
            "direction": "short",
            "confirmed": True,
            "is_exhaustion": False,
            "signal_timestamp": "2026-11-23",
        }
        res_conf = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_conf, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_conf["status"], ds.STATUS_RED)
        self.assertIn("daily_direction_reversed", {a["code"] for a in res_conf["red_alerts"]})

        # 4) Structured strong opposite momentum (momentum_short, confirmed momentum regime) -> RED_EXIT!
        snap_mom = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
            daily_signals=["日线空头资金方向一致推进"],
            daily_momentum={
                "active_direction": "down",
                "regime": "strong_down_continuation",
                "verdict": "capital_consensus_down",
                "is_capital_consensus": False,
                "is_non_divergence_power": False,
                "is_divergence_candidate": False,
            },
        )
        snap_mom["chanlun"]["signal_summary"]["signal_meta"] = {
            "signal_type": "momentum_short",
            "direction": "short",
            "confirmed": False,
            "momentum_confirmed": True,
            "is_exhaustion": False,
            "signal_timestamp": "2026-11-23",
        }
        res_mom = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_mom, ref_date=date(2026, 11, 23)
        )
        self.assertEqual(res_mom["status"], ds.STATUS_RED)
        self.assertIn("daily_direction_reversed", {a["code"] for a in res_mom["red_alerts"]})

    def test_futures_ltd_calendar_3letter_roots_and_unresolved_contract_warning(self):
        ref = date(2026, 11, 23)

        # SHFE: 15th of delivery month (weekend -> next Monday; NEVER Dec 31!)
        self.assertEqual(ds._estimate_cn_future_ltd("CU", 2026, 12), date(2026, 12, 15))
        self.assertEqual(ds._estimate_cn_future_ltd("CU", 2026, 8), date(2026, 8, 17))   # Aug 15 is Sat -> Mon 17
        self.assertEqual(ds._estimate_cn_future_ltd("CU", 2026, 11), date(2026, 11, 16))  # Nov 15 is Sun -> Mon 16
        # INE SC: last CN trading day of previous month
        self.assertEqual(ds._estimate_cn_future_ltd("SC", 2026, 12), date(2026, 11, 30))
        self.assertEqual(ds._estimate_cn_future_ltd("SC", 2027, 1), date(2026, 12, 31))
        # DCE / CZCE / GFEX: 10th CN trading day of delivery month (skipping statutory holidays e.g. May 1-5 Labor Day)
        self.assertEqual(ds._estimate_cn_future_ltd("I", 2026, 9), date(2026, 9, 14))
        self.assertEqual(ds._estimate_cn_future_ltd("TA", 2027, 5), date(2027, 5, 19))
        self.assertEqual(ds._estimate_cn_future_ltd("LC", 2026, 11), date(2026, 11, 13))
        # INE EC: last Monday of delivery month
        self.assertEqual(ds._estimate_cn_future_ltd("EC", 2026, 12), date(2026, 12, 28))

        # Regex parses CU2612, TA705, and 3-letter root ALIZ26.CMX cleanly
        extracted = {c["contract"]: c for c in ds._extract_futures_contracts("持有 CU2612 + TA705 + ALIZ26.CMX", ref_date=ref)}
        self.assertEqual(extracted["CU2612"]["est_last_trading_day"], date(2026, 12, 15))
        self.assertEqual(extracted["TA705"]["est_last_trading_day"], date(2027, 5, 19))
        self.assertIn("ALIZ26.CMX", extracted)
        self.assertEqual(extracted["ALIZ26.CMX"]["root"], "ALI")
        self.assertEqual(extracted["ALIZ26.CMX"]["year"], 2026)
        self.assertEqual(extracted["ALIZ26.CMX"]["month"], 12)
        self.assertEqual(extracted["ALIZ26.CMX"]["est_last_trading_day"], date(2026, 12, 29))

        # Roll window thresholds: 8 days before CU2612 LTD (2026-12-07) -> GREEN info alert; 5 days before (2026-12-10) -> YELLOW imminent roll!
        pos_cu = _make_base_position(
            symbol="CU0", direction="short", entry_price=75000.0, tactical_stop_30m=76000.0, structural_stop_daily=78000.0, target_price=68000.0
        )
        pos_cu["asset_class"] = "cn_future"
        pos_cu["contract_symbol"] = "CU2612"
        snap_base_8d = _make_healthy_snapshot(
            symbol="CU0", price=73000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=74500.0,
            daily_bias="short", intraday_bias="short", daily_bar="2026-12-07", intraday_bar="2026-12-07 15:00"
        )
        snap_ct_8d = _make_healthy_snapshot(
            symbol="CU2612", price=73000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=74500.0,
            daily_bias="short", intraday_bias="short", daily_bar="2026-12-07", intraday_bar="2026-12-07 15:00"
        )
        res_8d = ds.evaluate_holding_health(
            "CU0", "short", position=pos_cu, archive_state=_make_armed_state("short", 78000.0),
            snapshot=snap_base_8d, contract_snapshot=snap_ct_8d, ref_date=date(2026, 12, 7)
        )
        self.assertEqual(res_8d["status"], ds.STATUS_GREEN)
        self.assertTrue(any(e["source"] == "futures_roll_window" and not e["imminent"] for e in res_8d["upcoming_events"]))

        snap_base_5d = _make_healthy_snapshot(
            symbol="CU0", price=73000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=74500.0,
            daily_bias="short", intraday_bias="short", daily_bar="2026-12-10", intraday_bar="2026-12-10 15:00"
        )
        snap_ct_5d = _make_healthy_snapshot(
            symbol="CU2612", price=73000.0, daily_zd=74000.0, daily_zg=76000.0, intra_zd=73200.0, intra_zg=74500.0,
            daily_bias="short", intraday_bias="short", daily_bar="2026-12-10", intraday_bar="2026-12-10 15:00"
        )
        res_5d = ds.evaluate_holding_health(
            "CU0", "short", position=pos_cu, archive_state=_make_armed_state("short", 78000.0),
            snapshot=snap_base_5d, contract_snapshot=snap_ct_5d, ref_date=date(2026, 12, 10)
        )
        self.assertEqual(res_5d["status"], ds.STATUS_YELLOW)
        self.assertIn("roll_window_imminent", {a["code"] for a in res_5d["yellow_alerts"]})

        # Unresolvable futures-like contract_symbol ("CL=F" continuous or unknown "XYZ2612") -> explicit data_warning & blocks GREEN!
        pos_unres = _make_base_position(
            symbol="CL=F", direction="long", entry_price=70.0, tactical_stop_30m=68.0, structural_stop_daily=65.0
        )
        pos_unres["asset_class"] = "commodity"
        pos_unres["contract_symbol"] = "CL=F"
        snap_cl = _make_healthy_snapshot(symbol="CL=F", price=75.0, daily_zd=66.0, daily_zg=72.0, intra_zd=67.0, intra_zg=73.0)
        res_unres = ds.evaluate_holding_health(
            "CL=F", "long", position=pos_unres, archive_state=_make_armed_state("long", 65.0),
            snapshot=snap_cl, ref_date=ref
        )
        self.assertEqual(res_unres["status"], ds.STATUS_YELLOW)
        self.assertTrue(any("futures_roll_window" in w for w in res_unres["data_warnings"]))

    def test_stable_metrics_perp_oi_sc0_spread_severity_negation_and_30m_return(self):
        ref = date(2026, 11, 23)

        # 1) perp_open_interest -> perp_oi_usd (sum across OKX+Binance) & perp_oi_contracts (contracts unit only)
        crypto_bundle = {
            "symbol": "BTC-USD",
            "fundamentals_and_supply": {
                "perp_open_interest": {
                    "okx": {
                        "status": "ok",
                        "open_interest_amount": 120000.0,
                        "amount_unit": "contracts",
                        "open_interest_value_usd": 3500000000.0,
                    },
                    "binance": {
                        "status": "ok",
                        "open_interest_amount": 80000.0,
                        "amount_unit": "BTC",
                        "open_interest_value_usd": 7600000000.0,
                    },
                }
            },
        }
        cr_obs = ds.extract_sentinel_observations(crypto_bundle, None, ref_date=ref)
        self.assertEqual(cr_obs["perp_oi_usd"], 11100000000.0)
        self.assertEqual(cr_obs["perp_oi_contracts"], 120000.0)

        # 2) SC0 missing sc_minus_brent MUST NOT substitute brent_wti into cross_spread_primary!
        sc_bundle_no_sc = {
            "symbol": "SC0",
            "fundamentals_and_supply": {
                "cross_market_spread": {
                    "status": "ok",
                    "group": "crude_oil",
                    "brent_minus_wti_usd_bbl": 4.25,
                }
            },
        }
        sc_obs = ds.extract_sentinel_observations(sc_bundle_no_sc, None, ref_date=ref)
        self.assertEqual(sc_obs["spread_brent_wti_usd"], 4.25)
        self.assertNotIn("spread_sc_brent_usd", sc_obs)
        self.assertNotIn("cross_spread_primary", sc_obs)

        # All stable leg-specific spread keys emitted cleanly
        cu_obs = ds.extract_sentinel_observations(
            {"symbol": "CU0", "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", "group": "copper", "spread_ex_vat_usd_mt": -200.0, "inputs": {"usdcny": 7.2}}}},
            None, ref_date=ref
        )
        self.assertEqual(cu_obs["spread_cu_hg_vat_excl_cny_t"], -1440.0)
        au_obs = ds.extract_sentinel_observations(
            {"symbol": "AU0", "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", "group": "gold", "shfe_minus_comex_cny_g": 6.5}}},
            None, ref_date=ref
        )
        self.assertEqual(au_obs["spread_au_gc_cny_g"], 6.5)
        ag_obs = ds.extract_sentinel_observations(
            {"symbol": "AG0", "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", "group": "silver", "shfe_minus_comex_tax_incl_cny_kg": -90.0}}},
            None, ref_date=ref
        )
        self.assertEqual(ag_obs["spread_ag_si_cny_kg"], -90.0)
        sr_obs = ds.extract_sentinel_observations(
            {"symbol": "SR0", "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", "group": "sugar", "czce_minus_ice_raw_fob_cny_mt": 1500.0}}},
            None, ref_date=ref
        )
        self.assertEqual(sr_obs["spread_sr_sb_cny_t"], 1500.0)
        cf_obs = ds.extract_sentinel_observations(
            {"symbol": "CF0", "fundamentals_and_supply": {"cross_market_spread": {"status": "ok", "group": "cotton", "czce_minus_ice_futures_cny_mt": 2200.0}}},
            None, ref_date=ref
        )
        self.assertEqual(cf_obs["spread_cf_ct_cny_t"], 2200.0)

        # 3) _is_red_trigger: explicit severity beats kind; negation beats exit keywords!
        self.assertFalse(ds._is_red_trigger({"kind": "fundamental", "severity": "yellow", "action": "平仓撤离"}))
        self.assertFalse(ds._is_red_trigger({"kind": "supply_demand", "severity": "warning", "action": "止损"}))
        self.assertTrue(ds._is_red_trigger({"kind": "freight", "severity": "red", "action": "仅观察"}))
        self.assertFalse(ds._is_red_trigger({"kind": "term_structure", "action": "维持原止损，不平仓"}))
        self.assertFalse(ds._is_red_trigger({"kind": "fundamental", "action": "维持原止损，不平仓"}))
        self.assertFalse(ds._is_red_trigger({"kind": "chanlun", "action": "暂不平仓，继续持有观察"}))
        self.assertTrue(ds._is_red_trigger({"kind": "freight", "action": "运价崩塌，立即平仓"}))

        # 4) 30m return_1bar_pct NEVER copied into return_1d_pct!
        snap_ret = _make_healthy_snapshot(symbol="PWR", price=110.0)
        snap_ret["technicals"].pop("return_1d_pct", None)
        snap_ret["technicals"].pop("return_5d_pct", None)
        snap_ret["intraday_technicals"]["return_1bar_pct"] = 0.88
        snap_ret["intraday_technicals"]["return_5bar_pct"] = 2.15
        ret_obs = ds.extract_sentinel_observations(None, snap_ret, ref_date=ref)
        self.assertNotIn("return_1d_pct", ret_obs)
        self.assertNotIn("return_5d_pct", ret_obs)
        self.assertEqual(ret_obs["return_1bar_30m_pct"], 0.88)
        self.assertEqual(ret_obs["return_5bar_30m_pct"], 2.15)

    def test_full_timestamp_offset_comparison_not_date_truncated(self):
        # Cross-timezone test: daily=2026-11-23T16:00:00-05:00 (21:00 UTC) vs intraday=2026-11-24T01:00:00+08:00 (17:00 UTC)
        # Naive date truncation [:10] would falsely think 2026-11-24 > 2026-11-23; UTC comparison catches 17:00Z < 21:00Z!
        snap_tz_lag = _make_healthy_snapshot(
            symbol="PWR", price=108.0,
            daily_bar="2026-11-23T16:00:00-05:00",
            intraday_bar="2026-11-24T01:00:00+08:00",
        )
        snap_tz_lag["technicals"]["last_close"] = 111.5
        px, src, warn = ds._select_latest_price(snap_tz_lag)
        self.assertEqual(px, 111.5)
        self.assertEqual(src, "daily")
        self.assertIsNotNone(warn)

        # Same-calendar-day hour lag: daily=16:00-05:00 (21:00Z) vs intraday=14:00-05:00 (19:00Z)
        snap_same_day_lag = _make_healthy_snapshot(
            symbol="PWR", price=108.0,
            daily_bar="2026-11-23T16:00:00-05:00",
            intraday_bar="2026-11-23T14:00:00-05:00",
        )
        snap_same_day_lag["technicals"]["last_close"] = 112.0
        px2, src2, warn2 = ds._select_latest_price(snap_same_day_lag)
        self.assertEqual(px2, 112.0)
        self.assertEqual(src2, "daily")
        self.assertIsNotNone(warn2)

        # Intraday truly newer: daily=16:00-05:00 (21:00Z) vs intraday=21:30Z -> selects intraday!
        snap_newer = _make_healthy_snapshot(
            symbol="PWR", price=113.2,
            daily_bar="2026-11-23T16:00:00-05:00",
            intraday_bar="2026-11-23T21:30:00Z",
        )
        snap_newer["technicals"]["last_close"] = 112.0
        px3, src3, warn3 = ds._select_latest_price(snap_newer)
        self.assertEqual(px3, 113.2)
        self.assertEqual(src3, "intraday_30m")
        self.assertIsNone(warn3)

        # Exact sub-day UTC staleness for crypto (> 24h vs now_utc -> YELLOW_LOOSENING)
        snap_btc_26h = _make_healthy_snapshot(
            symbol="BTC-USD", price=95000.0, daily_zd=88000.0, daily_zg=92000.0,
            intra_zd=90000.0, intra_zg=94000.0,
            daily_bar="2026-11-22T12:00:00Z",
            intraday_bar="2026-11-22T12:00:00Z",
        )
        snap_btc_26h["asset_class"] = "crypto"
        pos_btc = _make_base_position(
            symbol="BTC-USD", entry_price=90000.0, tactical_stop_30m=91000.0, structural_stop_daily=87000.0, target_price=120000.0
        )
        pos_btc["asset_class"] = "crypto"
        res_26h = ds.evaluate_holding_health(
            "BTC-USD", "long", position=pos_btc, archive_state=_make_armed_state("long", 87000.0),
            snapshot=snap_btc_26h, now_utc=datetime(2026, 11, 23, 14, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(res_26h["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_stale", {a["code"] for a in res_26h["yellow_alerts"]})

    def test_corrupted_archive_closed_position_sync_and_upgraded_update_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            port_dir = os.path.join(tmpdir, "portfolio")
            os.makedirs(port_dir, exist_ok=True)

            # 1) Active holding PWR
            ra.init_archive("PWR", name="Quanta Services", research_root=res_root)
            ra.update_state(
                "PWR",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "triggers": [
                        {"id": "t_pwr", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 95.0, "action": "破位撤离"}
                    ],
                },
                research_root=res_root,
            )

            # 2) Stale holding archive MSFT whose portfolio ledger position is CLOSED!
            ra.init_archive("MSFT", name="Microsoft", research_root=res_root)
            ra.update_state(
                "MSFT",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "triggers": [{"id": "t_msft", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 400.0, "action": "止损"}],
                },
                research_root=res_root,
            )

            # 3) Watchlist NVDA
            ra.init_archive("NVDA", name="NVIDIA", research_root=res_root)
            ra.update_state(
                "NVDA",
                {
                    "status": "watchlist",
                    "direction_bias": "long",
                    "triggers": [{"id": "t_nvda", "kind": "chanlun", "metric": "close", "operator": ">=", "threshold": 140.0, "action": "突破关注"}],
                },
                research_root=res_root,
            )

            # 4) Corrupted archive JSON!
            broken_dir = os.path.join(res_root, "US", "BROKEN")
            os.makedirs(broken_dir, exist_ok=True)
            with open(os.path.join(broken_dir, "state.json"), "w", encoding="utf-8") as fh:
                fh.write("{corrupted json!!!")

            # Portfolio ledger has PWR open and MSFT closed
            positions_doc = {
                "schema_version": 1,
                "revision": 2,
                "positions": [
                    _make_base_position(symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0),
                    {**_make_base_position(symbol="MSFT"), "status": "closed"},
                ],
            }
            with open(os.path.join(port_dir, "positions.json"), "w", encoding="utf-8") as fh:
                json.dump(positions_doc, fh, ensure_ascii=False, indent=2)

            snaps = {
                "PWR": _make_healthy_snapshot(symbol="PWR", price=110.0, intra_zd=101.0),
                "NVDA": _make_healthy_snapshot(symbol="NVDA", price=145.0, daily_zd=125.0, daily_zg=138.0),
            }

            report = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                include_watchlist=True,
                skip_research=True,
                update_archive=True,
                log_green=True,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=lambda s: snaps[s],
            )

            # MSFT MUST NOT appear as a ghost holding; must appear in state_sync_warnings!
            self.assertEqual([h["symbol"] for h in report["holdings"]], ["PWR"])
            self.assertEqual(report["summary"]["state_sync_warnings_count"], 1)
            self.assertEqual(report["state_sync_warnings"][0]["symbol"], "MSFT")
            self.assertEqual(report["state_sync_warnings"][0]["code"], "closed_position_stale_holding_archive")

            # Corrupted archive MUST be surfaced in discovery_errors!
            self.assertEqual(report["summary"]["discovery_errors_count"], 1)
            self.assertTrue(any("BROKEN" in e for e in report["discovery_errors"]))

            # --update-archive updated state.json watermarks + trigger metadata AND thesis.md logs for BOTH holding & watchlist!
            pwr_loaded = ra.load_archive("PWR", research_root=res_root)
            self.assertIn("daily_sentinel", pwr_loaded["state"]["source_watermarks"])
            pwr_trig = pwr_loaded["state"]["triggers"][0]
            self.assertTrue(pwr_trig.get("last_checked_at"))
            self.assertEqual(pwr_trig.get("last_value"), 110.0)
            self.assertEqual(pwr_trig.get("status"), "armed")
            self.assertIn("每日哨兵", pwr_loaded["thesis_md"])

            nvda_loaded = ra.load_archive("NVDA", research_root=res_root)
            self.assertIn("daily_sentinel", nvda_loaded["state"]["source_watermarks"])
            nvda_trig = nvda_loaded["state"]["triggers"][0]
            self.assertTrue(nvda_trig.get("last_checked_at"))
            self.assertEqual(nvda_trig.get("last_value"), 145.0)
            self.assertEqual(nvda_trig.get("status"), "fired")
            self.assertIn("观察池轻刷", nvda_loaded["thesis_md"])

            # Markdown displays discovery_errors & state_sync_warnings prominently
            md = ds.format_sentinel_markdown(report)
            self.assertIn("【档案发现与状态同步告警】", md)
            self.assertIn("BROKEN", md)
            self.assertIn("账本已平仓但研究档案仍标记 holding", md)

            # CLI --fail-on-red MUST return 2 and warn on stderr when discovery_errors exist (even if red_count==0)!
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with (
                mock.patch.object(ds, "_default_snapshot_fetcher", side_effect=lambda s: snaps[s]),
                contextlib.redirect_stdout(out_buf),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = ds.main([
                    "--portfolio-dir", port_dir,
                    "--research-root", res_root,
                    "--skip-research",
                    "--format", "json",
                    "--fail-on-red",
                    "--ref-date", "2026-11-23",
                ])
            self.assertEqual(rc, 2)
            self.assertIn("discovery error", err_buf.getvalue().lower())


class TestRound2Hardening(unittest.TestCase):
    def test_contract_quote_isolation_zero_cross_contamination_and_same_symbol_mismatch(self):
        ref = date(2026, 11, 23)
        state = _make_armed_state("long", stop_threshold=95.0)

        # 1) CU0 + contract_symbol="CU2612" with failed CU2612 quote + CU0 close=90 + trigger close <= 95
        #    MUST yield YELLOW_LOOSENING + contract_quote_mismatch, trigger evaluated=False, NEVER false RED_EXIT!
        pos_cu = _make_base_position(
            symbol="CU0",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=96.0,
            structural_stop_daily=95.0,
            target_price=120.0,
            contract_or_expression="CU2612 远月多单",
        )
        pos_cu["asset_class"] = "cn_future"
        pos_cu["contract_symbol"] = "CU2612"

        cu0_snap = _make_healthy_snapshot(
            symbol="CU0",
            price=90.0,
            daily_zd=88.0,
            daily_zg=94.0,
            intra_zd=89.0,
            intra_zg=92.0,
            daily_bias="long",
            intraday_bias="long",
            daily_bar="2026-11-23",
            intraday_bar="2026-11-23 15:00",
        )
        cu0_snap["asset_class"] = "cn_future"
        bundle_with_cu0 = {"symbol": "CU0", "snapshot": cu0_snap, "fundamentals_and_supply": {}}

        res_fail = ds.evaluate_holding_health(
            "CU0",
            "long",
            position=pos_cu,
            archive_state=state,
            snapshot=cu0_snap,
            research_bundle=bundle_with_cu0,
            contract_snapshot={"error": "CU2612 quote unavailable"},
            ref_date=ref,
        )
        self.assertEqual(res_fail["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_fail["red_alerts"], [])
        self.assertIsNone(res_fail["last_price"])
        yellow_codes = {a["code"] for a in res_fail["yellow_alerts"]}
        self.assertIn("contract_quote_mismatch", yellow_codes)
        self.assertIn("unevaluated_triggers", yellow_codes)
        self.assertEqual(len(res_fail["trigger_results"]), 1)
        self.assertFalse(res_fail["trigger_results"][0]["evaluated"])
        self.assertFalse(res_fail["trigger_results"][0]["fired"])
        # All mismatched price/stop/30m observations MUST be withheld!
        for forbidden_key in (
            "close",
            "return_1d_pct",
            "return_5d_pct",
            "return_1bar_30m_pct",
            "atr14",
            "intraday_30m_zd",
            "intraday_30m_zg",
        ):
            self.assertNotIn(forbidden_key, res_fail["observations_summary"])

        # 2) symbol == contract_symbol == "CU2612" with quote returning symbol="CU0" (close=90)
        #    MUST STILL trigger contract_quote_mismatch and refuse to check stops or triggers against CU0!
        pos_same = _make_base_position(
            symbol="CU2612",
            direction="long",
            entry_price=100.0,
            tactical_stop_30m=96.0,
            structural_stop_daily=95.0,
            target_price=120.0,
            contract_or_expression="CU2612",
        )
        pos_same["asset_class"] = "cn_future"
        pos_same["contract_symbol"] = "CU2612"

        res_same = ds.evaluate_holding_health(
            "CU2612",
            "long",
            position=pos_same,
            archive_state=state,
            snapshot=cu0_snap,
            contract_snapshot=None,
            ref_date=ref,
        )
        self.assertEqual(res_same["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_same["red_alerts"], [])
        self.assertIsNone(res_same["last_price"])
        self.assertIn("contract_quote_mismatch", {a["code"] for a in res_same["yellow_alerts"]})
        self.assertFalse(res_same["trigger_results"][0]["evaluated"])
        self.assertNotIn("close", res_same["observations_summary"])

    def test_same_day_12bar_stop_breach_window_gap_and_stop_updated_at_guard(self):
        ref = date(2026, 11, 23)
        state = _make_armed_state("long", stop_threshold=95.0)

        # Build a 12-bar 30m US session: 09:30 EST (14:30Z) .. 15:00 EST (20:00Z)
        start_utc = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
        bars_12 = []
        for i in range(12):
            b_dt = start_utc + timedelta(minutes=30 * i)
            b_low = 98.0 if i == 1 else (105.0 + 0.2 * i)  # Bar #2 breaches tactical stop 102!
            b_close = 110.0 if i == 11 else (106.0 + 0.2 * i)
            bars_12.append({
                "bar": b_dt.isoformat(),
                "bar_utc": b_dt.isoformat(),
                "open": 106.0,
                "high": 111.0,
                "low": b_low,
                "close": b_close,
            })

        # 1) 12-bar 30m session where bar #2 breaches stop 102 (low=98) and bar #12 closes 110 -> RED_EXIT!
        pos_12 = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos_12["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        snap_12 = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars_12[-1]["bar_utc"],
        )
        snap_12["intraday_technicals"]["recent_bars_ohlc"] = bars_12
        res_12 = ds.evaluate_holding_health(
            "PWR", "long", position=pos_12, archive_state=state, snapshot=snap_12, ref_date=ref
        )
        self.assertEqual(res_12["status"], ds.STATUS_RED)
        self.assertIn("tactical_stop_30m_intraday_breached", {a["code"] for a in res_12["red_alerts"]})

        # 2) Truncated 8-bar window missing bars after open (last 8 bars only, all >= 105) -> YELLOW_LOOSENING (NOT GREEN!)
        snap_trunc = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars_12[-1]["bar_utc"],
        )
        snap_trunc["intraday_technicals"]["recent_bars_ohlc"] = bars_12[-8:]  # starts at 16:30Z > opened 14:30Z!
        res_trunc = ds.evaluate_holding_health(
            "PWR", "long", position=pos_12, archive_state=state, snapshot=snap_trunc, ref_date=ref
        )
        self.assertEqual(res_trunc["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_trunc["red_alerts"], [])
        self.assertTrue(
            any("intraday_ohlc_window_gap_since_effective_ts" in w for w in res_trunc["data_warnings"])
        )
        self.assertIn(
            "intraday_ohlc_window_gap_since_effective_ts",
            {a["code"] for a in res_trunc["yellow_alerts"]},
        )

        # 3) Bar low=101 BEFORE stop_updated_at_utc when stop was trailed up to 102 -> NOT retroactively RED_EXIT (GREEN_HOLD!)
        bars_trail = []
        for i in range(12):
            b_dt = start_utc + timedelta(minutes=30 * i)
            # Bar #2 (15:00Z) had low=101.0 (below new trailed stop 102, above structural stop 95)
            # Stop trailed to 102 at 18:00Z (bar #8); bars >= 18:00Z all stay >= 105.0
            b_low = 101.0 if i == 1 else (105.0 + 0.2 * i)
            b_close = 110.0 if i == 11 else (106.0 + 0.2 * i)
            bars_trail.append({
                "bar": b_dt.isoformat(),
                "bar_utc": b_dt.isoformat(),
                "open": 106.0,
                "high": 111.0,
                "low": b_low,
                "close": b_close,
            })
        pos_trailed = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos_trailed["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        pos_trailed["stop_updated_at_utc"] = "2026-11-23T18:00:00+00:00"

        snap_trailed = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars_trail[-1]["bar_utc"],
        )
        snap_trailed["technicals"]["last_bar_low"] = 101.0  # Same-day daily bar low=101 must NOT trigger trailed stop!
        snap_trailed["intraday_technicals"]["recent_bars_ohlc"] = bars_trail

        res_trailed = ds.evaluate_holding_health(
            "PWR", "long", position=pos_trailed, archive_state=state, snapshot=snap_trailed, ref_date=ref
        )
        self.assertEqual(res_trailed["status"], ds.STATUS_GREEN)
        self.assertEqual(res_trailed["red_alerts"], [])
        self.assertEqual(res_trailed["yellow_alerts"], [])

    def test_30m_timestamp_freshness_date_only_future_unclosed_and_cn_naive_tz(self):
        pos = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        state = _make_armed_state("long", 95.0)
        now_utc = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)

        # 1) Date-only 30m as_of_bar="2026-11-23" -> YELLOW_LOOSENING (never GREEN_HOLD!)
        self.assertFalse(ds._has_time_component("2026-11-23"))
        self.assertTrue(ds._has_time_component("2026-11-23 15:30"))
        snap_date_only = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0,
            daily_bar="2026-11-23", intraday_bar="2026-11-23",
        )
        res_date_only = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_date_only, now_utc=now_utc
        )
        self.assertEqual(res_date_only["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_missing_time", {a["code"] for a in res_date_only["yellow_alerts"]})
        self.assertFalse(res_date_only["green_preconditions"]["fresh_intraday_30m"])

        # 2) Future 30m timestamp (now_utc + 1h) -> YELLOW_LOOSENING (never GREEN_HOLD!)
        future_ts = (now_utc + timedelta(hours=1)).isoformat()
        snap_future = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0,
            daily_bar="2026-11-23", intraday_bar=future_ts,
        )
        res_future = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_future, now_utc=now_utc
        )
        self.assertEqual(res_future["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_future_timestamp", {a["code"] for a in res_future["yellow_alerts"]})
        self.assertFalse(res_future["green_preconditions"]["fresh_intraday_30m"])

        # 3) bar_is_closed=False -> YELLOW_LOOSENING (never GREEN_HOLD!)
        snap_unclosed = _make_healthy_snapshot(
            symbol="PWR", price=110.0, intra_zd=101.0,
            daily_bar="2026-11-23", intraday_bar="2026-11-23T17:30:00Z",
        )
        snap_unclosed["intraday_technicals"]["bar_is_closed"] = False
        res_unclosed = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_unclosed, now_utc=now_utc
        )
        self.assertEqual(res_unclosed["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_30m_bar_unclosed", {a["code"] for a in res_unclosed["yellow_alerts"]})
        self.assertFalse(res_unclosed["green_preconditions"]["fresh_intraday_30m"])

        # 4) Naive CN 11:15 MUST localize to Asia/Shanghai (+08:00 -> 03:15 UTC, NOT 11:15 UTC!)
        cn_dt_utc, cn_local, cn_has_time, cn_has_tz = ds._parse_bar_timestamp(
            "2026-11-23 11:15", asset_class="cn_future"
        )
        self.assertEqual(cn_dt_utc, datetime(2026, 11, 23, 3, 15, tzinfo=timezone.utc))
        self.assertEqual(cn_local, date(2026, 11, 23))
        self.assertTrue(cn_has_time)
        self.assertFalse(cn_has_tz)

        a_dt_utc, _, _, _ = ds._parse_bar_timestamp("2026-11-23 11:15", asset_class="a_share")
        self.assertEqual(a_dt_utc, datetime(2026, 11, 23, 3, 15, tzinfo=timezone.utc))

        # At 03:45 UTC (11:45 CST), naive CN bar "2026-11-23 11:15" is 30min ago (NOT 7.5h in the future) -> GREEN_HOLD!
        pos_cn = _make_base_position(
            symbol="CU0", direction="long", entry_price=72000.0, tactical_stop_30m=73000.0,
            structural_stop_daily=70000.0, target_price=82000.0,
        )
        pos_cn["asset_class"] = "cn_future"
        snap_cn = _make_healthy_snapshot(
            symbol="CU0", price=75000.0, daily_zd=70000.0, daily_zg=73500.0, intra_zd=72500.0, intra_zg=74500.0,
            daily_bias="long", intraday_bias="long",
            daily_bar="2026-11-23", intraday_bar="2026-11-23 11:15",
        )
        snap_cn["asset_class"] = "cn_future"
        res_cn = ds.evaluate_holding_health(
            "CU0", "long", position=pos_cn, archive_state=_make_armed_state("long", 70000.0),
            snapshot=snap_cn, now_utc=datetime(2026, 11, 23, 3, 45, tzinfo=timezone.utc),
        )
        self.assertEqual(res_cn["status"], ds.STATUS_GREEN)

    def test_no_false_daily_red_on_neutral_extension_momentum_text(self):
        ref = date(2026, 11, 23)
        pos = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        state = _make_armed_state("long", 95.0)

        # 1) Codex repro: daily signal text says "空头资金方向一致推进" with regime="neutral_extension_down"
        #    (both via real chanlun.assess_dual_timeframe_alignment AND pre-built dual_alignment) -> YELLOW_LOOSENING, NOT RED_EXIT!
        for include_dual in (False, True):
            snap_neu = _make_healthy_snapshot(
                symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
                daily_signals=["日线空头资金方向一致推进"],
                daily_momentum={
                    "active_direction": "down",
                    "regime": "neutral_extension_down",
                    "verdict": "neutral_extension_down",
                    "is_capital_consensus": False,
                    "is_non_divergence_power": False,
                    "is_divergence_candidate": False,
                },
                include_dual=include_dual,
            )
            res_neu = ds.evaluate_holding_health(
                "PWR", "long", position=pos, archive_state=state, snapshot=snap_neu, ref_date=ref
            )
            self.assertEqual(res_neu["status"], ds.STATUS_YELLOW, msg=f"include_dual={include_dual}")
            self.assertEqual(res_neu["red_alerts"], [], msg=f"include_dual={include_dual}")
            self.assertIn("daily_structure_loosening", {a["code"] for a in res_neu["yellow_alerts"]})

        # 2) Explicit weak_momentum_short / momentum_confirmed=False / single_stroke_no_comparison -> YELLOW_LOOSENING!
        for sig_meta_case, mom_case in (
            (
                {"signal_type": "weak_momentum_short", "direction": "short", "confirmed": False, "momentum_confirmed": False},
                {"active_direction": "down", "regime": "neutral_extension_down"},
            ),
            (
                {"signal_type": "momentum_short", "direction": "short", "confirmed": False, "momentum_confirmed": False},
                {"active_direction": "down", "regime": "single_stroke_no_comparison"},
            ),
        ):
            snap_wk = _make_healthy_snapshot(
                symbol="PWR", price=110.0, intra_zd=101.0, daily_bias="short", intraday_bias="long",
                daily_signals=["日线空头资金方向一致推进"],
                daily_momentum=mom_case,
            )
            snap_wk["chanlun"]["dual_alignment"]["daily_signal_meta"] = sig_meta_case
            res_wk = ds.evaluate_holding_health(
                "PWR", "long", position=pos, archive_state=state, snapshot=snap_wk, ref_date=ref
            )
            self.assertEqual(res_wk["status"], ds.STATUS_YELLOW)
            self.assertEqual(res_wk["red_alerts"], [])
            self.assertIn("daily_structure_loosening", {a["code"] for a in res_wk["yellow_alerts"]})

    def test_cn_futures_ltd_product_rules_and_statutory_holidays(self):
        # Statutory holiday checks (Oct 1-7 National Day 2026 closed; Oct 8 open)
        for day in range(1, 8):
            self.assertFalse(ds._is_cn_trading_day(date(2026, 10, day)))
        self.assertTrue(ds._is_cn_trading_day(date(2026, 10, 8)))

        # Codex exact LTD assertions
        self.assertEqual(ds._estimate_cn_future_ltd("LH", 2026, 11), date(2026, 11, 25))  # DCE LH: 4th-to-last trading day
        self.assertEqual(ds._estimate_cn_future_ltd("LC", 2026, 10), date(2026, 10, 21))  # GFEX LC: 10th trading day after Oct 1-7
        self.assertEqual(ds._estimate_cn_future_ltd("CU", 2026, 12), date(2026, 12, 15))  # SHFE CU: 15th
        self.assertEqual(ds._estimate_cn_future_ltd("CU", 2026, 2), date(2026, 2, 24))    # SHFE CU: Feb 15-23 Spring Festival -> Feb 24
        self.assertEqual(ds._estimate_cn_future_ltd("SC", 2025, 2), date(2025, 1, 27))    # INE SC: Jan 28-31 Spring Festival -> Jan 27

        # Extract via contract text
        ref = date(2026, 10, 1)
        extracted = {c["contract"]: c for c in ds._extract_futures_contracts("LH2611 + LC2610 + CU2612", ref_date=ref)}
        self.assertEqual(extracted["LH2611"]["est_last_trading_day"], date(2026, 11, 25))
        self.assertEqual(extracted["LC2610"]["est_last_trading_day"], date(2026, 10, 21))
        self.assertEqual(extracted["CU2612"]["est_last_trading_day"], date(2026, 12, 15))

    def test_concurrent_update_archive_preserves_edits_and_retries_on_revision_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            port_dir = os.path.join(tmpdir, "portfolio")
            os.makedirs(port_dir, exist_ok=True)

            ra.init_archive("PWR", name="Quanta Services", research_root=res_root)
            ra.update_state(
                "PWR",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "triggers": [
                        {
                            "id": "t_pwr",
                            "kind": "chanlun",
                            "metric": "close",
                            "operator": "<=",
                            "threshold": 95.0,
                            "action": "破位撤离",
                        }
                    ],
                },
                research_root=res_root,
            )

            pos = _make_base_position(
                symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
            )
            with open(os.path.join(port_dir, "positions.json"), "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "revision": 1, "positions": [pos]}, fh)

            # 1) Simulate concurrent edit (threshold 95 -> 105 + new trigger t_new) occurring during snapshot fetch!
            def _snap_with_concurrent_edit(sym: str) -> dict:
                ra.update_state(
                    "PWR",
                    {
                        "triggers": [
                            {
                                "id": "t_pwr",
                                "kind": "chanlun",
                                "metric": "close",
                                "operator": "<=",
                                "threshold": 105.0,
                                "action": "上移后的破位撤离",
                            },
                            {
                                "id": "t_new",
                                "kind": "fundamental",
                                "metric": "forward_pe",
                                "operator": ">=",
                                "threshold": 45.0,
                                "action": "估值极端预警",
                            },
                        ]
                    },
                    research_root=res_root,
                )
                return _make_healthy_snapshot(symbol=sym, price=110.0, intra_zd=101.0)

            rep = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                skip_research=True,
                update_archive=True,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=_snap_with_concurrent_edit,
            )
            self.assertTrue(rep["holdings"][0].get("archive_state_updated"))
            after = ra.load_archive("PWR", research_root=res_root)["state"]
            by_id = {t["id"]: t for t in after["triggers"]}
            self.assertIn("t_pwr", by_id)
            self.assertIn("t_new", by_id)
            self.assertEqual(by_id["t_pwr"]["threshold"], 105.0)
            self.assertEqual(by_id["t_pwr"]["action"], "上移后的破位撤离")
            self.assertEqual(by_id["t_pwr"]["last_value"], 110.0)
            self.assertEqual(by_id["t_pwr"]["status"], "armed")
            self.assertTrue(by_id["t_pwr"].get("last_checked_at"))

            # 2) Simulate mid-write revision conflict: update_state sees concurrent write between load & write -> retries once!
            real_update = ra.update_state
            seen_expected_revs: list[int | None] = []
            injected = {"done": False}

            def _conflict_once_update(raw_symbol, patch, expected_revision=None, **kwargs):
                seen_expected_revs.append(expected_revision)
                if not injected["done"]:
                    injected["done"] = True
                    # Concurrently bump threshold 105 -> 108 on disk right before sentinel's first write!
                    cur = ra.load_archive(raw_symbol, research_root=res_root)["state"]
                    trigs = list(cur["triggers"])
                    for t in trigs:
                        if t["id"] == "t_pwr":
                            t["threshold"] = 108.0
                    real_update(raw_symbol, {"triggers": trigs}, research_root=res_root)
                return real_update(raw_symbol, patch, expected_revision=expected_revision, **kwargs)

            with mock.patch.object(ds.ra_mod, "update_state", side_effect=_conflict_once_update):
                ds._update_archive_state_from_sentinel(
                    arch_sym="PWR",
                    archive_state=None,
                    trigger_results=[{"id": "t_pwr", "metric": "close", "evaluated": True, "fired": False, "observed": 112.0}],
                    checked_ts="2026-11-23 16:00:00 UTC",
                    research_root=res_root,
                )

            # Verify expected_revision was passed on both attempts and concurrent threshold=108.0 survived retry!
            self.assertEqual(len(seen_expected_revs), 2)
            self.assertIsNotNone(seen_expected_revs[0])
            self.assertEqual(seen_expected_revs[1], seen_expected_revs[0] + 1)
            final_state = ra.load_archive("PWR", research_root=res_root)["state"]
            final_by_id = {t["id"]: t for t in final_state["triggers"]}
            self.assertEqual(final_by_id["t_pwr"]["threshold"], 108.0)
            self.assertEqual(final_by_id["t_pwr"]["last_value"], 112.0)
            self.assertIn("t_new", final_by_id)

    def test_corrupted_portfolio_sqlite_db_does_not_crash_and_audits_research_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_root = os.path.join(tmpdir, "research")
            port_dir = os.path.join(tmpdir, "portfolio")
            os.makedirs(port_dir, exist_ok=True)

            # Write corrupted garbage bytes into portfolio.db
            with open(os.path.join(port_dir, "portfolio.db"), "wb") as fh:
                fh.write(b"CORRUPTED_NOT_A_SQLITE_DATABASE_0123456789")

            # Valid research/ holding archive that MUST still be discovered and audited!
            ra.init_archive("GC=F", name="黄金", research_root=res_root)
            ra.update_state(
                "GC=F",
                {
                    "status": "holding",
                    "direction_bias": "long",
                    "chanlun_anchors": {"tactical_stop_30m": 2600.0, "structural_stop_daily": 2520.0},
                    "triggers": [
                        {"id": "t_gc", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 2520.0, "action": "破位撤离"}
                    ],
                },
                research_root=res_root,
            )

            snap_gc = _make_healthy_snapshot(
                symbol="GC=F", price=2710.0, daily_zd=2550.0, daily_zg=2650.0, intra_zd=2580.0, intra_zg=2690.0
            )
            snap_gc["asset_class"] = "commodity"

            report = ds.run_daily_sentinel(
                portfolio_dir=port_dir,
                research_root=res_root,
                skip_research=True,
                ref_date=date(2026, 11, 23),
                snapshot_fetcher=lambda _s: snap_gc,
            )

            self.assertGreaterEqual(report["summary"]["discovery_errors_count"], 1)
            self.assertTrue(
                any("portfolio_positions" in e and "DatabaseError" in e for e in report["discovery_errors"]),
                msg=f"discovery_errors={report['discovery_errors']}",
            )
            self.assertEqual([h["symbol"] for h in report["holdings"]], ["GC=F"])
            self.assertEqual(report["holdings"][0]["status"], ds.STATUS_GREEN)


class TestRound3Hardening(unittest.TestCase):
    def test_straddling_bar_stop_ambiguity_blocks_green_without_false_red(self):
        ref = date(2026, 11, 23)
        state = _make_armed_state("long", stop_threshold=95.0)
        start_utc = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)

        # Codex Round-3 repro: stop=102 effective at 19:10Z; 19:00-19:30Z bar has low=99;
        # later 19:30-20:00Z bar closes 110 -> YELLOW_LOOSENING (straddling_bar_stop_ambiguity), NOT GREEN_HOLD!
        bars = []
        for i in range(12):  # 14:30Z .. 20:00Z
            b_dt = start_utc + timedelta(minutes=30 * i)
            is_straddle = (b_dt == datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc))
            bars.append({
                "bar": b_dt.isoformat(),
                "bar_utc": b_dt.isoformat(),
                "open": 106.0,
                "high": 111.0,
                "low": 99.0 if is_straddle else 106.0,
                "close": 110.0 if i == 11 else 107.0,
            })

        pos = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        pos["stop_updated_at_utc"] = "2026-11-23T19:10:00+00:00"

        snap = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars[-1]["bar_utc"],
        )
        snap["intraday_technicals"]["recent_bars_ohlc"] = bars

        res = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap, ref_date=ref
        )
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        self.assertEqual(res["red_alerts"], [])
        self.assertIn("straddling_bar_stop_ambiguity", {a["code"] for a in res["yellow_alerts"]})

        # Control: straddling 19:00-19:30Z bar does NOT touch stop (low=104 > 102) -> GREEN_HOLD!
        bars_safe = [dict(b) for b in bars]
        for b in bars_safe:
            if b["bar_utc"] == "2026-11-23T19:00:00+00:00":
                b["low"] = 104.0
        snap_safe = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars_safe[-1]["bar_utc"],
        )
        snap_safe["intraday_technicals"]["recent_bars_ohlc"] = bars_safe
        res_safe = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_safe, ref_date=ref
        )
        self.assertEqual(res_safe["status"], ds.STATUS_GREEN)

    def test_internal_same_session_bar_gap_detection_and_cn_lunch_exclusion(self):
        ref = date(2026, 11, 23)
        state = _make_armed_state("long", stop_threshold=95.0)

        # 1) Codex Round-3 repro: missing 19:30Z bar (19:00Z -> 20:00Z) inside US active session -> YELLOW_LOOSENING!
        start_utc = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
        bars_gap = []
        for i in range(12):  # 14:30Z .. 20:00Z
            b_dt = start_utc + timedelta(minutes=30 * i)
            if b_dt == datetime(2026, 11, 23, 19, 30, tzinfo=timezone.utc):
                continue  # drop 19:30Z bar -> 19:00Z -> 20:00Z gap!
            bars_gap.append({
                "bar": b_dt.isoformat(),
                "bar_utc": b_dt.isoformat(),
                "open": 106.0,
                "high": 111.0,
                "low": 105.5,
                "close": 110.0 if i == 11 else 107.0,
            })

        pos = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        snap_gap = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=bars_gap[-1]["bar_utc"],
        )
        snap_gap["intraday_technicals"]["recent_bars_ohlc"] = bars_gap

        res_gap = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap_gap, ref_date=ref
        )
        self.assertEqual(res_gap["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_gap["red_alerts"], [])
        self.assertIn("intraday_internal_bar_gap", {a["code"] for a in res_gap["yellow_alerts"]})
        self.assertTrue(any("intraday_internal_bar_gap" in w for w in res_gap["data_warnings"]))

        # 2) CN futures lunch break (11:30 -> 13:30 CST, and 11:00 -> 13:30 CST start-anchored) MUST NOT false-alarm!
        pos_cn = _make_base_position(
            symbol="CU0", direction="long", entry_price=72000.0, tactical_stop_30m=73000.0,
            structural_stop_daily=70000.0, target_price=82000.0,
        )
        pos_cn["asset_class"] = "cn_future"
        pos_cn["opened_at_utc"] = "2026-11-23 09:00:00+08:00"

        for cn_times in (
            ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30"],
            ["09:00", "09:30", "10:00", "10:30", "11:00", "13:30", "14:00", "14:30"],
        ):
            cn_bars = [
                {
                    "bar": f"2026-11-23 {t}",
                    "open": 74500.0,
                    "high": 75500.0,
                    "low": 74200.0,
                    "close": 75000.0,
                }
                for t in cn_times
            ]
            snap_cn = _make_healthy_snapshot(
                symbol="CU0", price=75000.0, daily_zd=70000.0, daily_zg=73500.0, intra_zd=72500.0, intra_zg=74500.0,
                daily_bias="long", intraday_bias="long",
                daily_bar="2026-11-23", intraday_bar=cn_bars[-1]["bar"],
            )
            snap_cn["asset_class"] = "cn_future"
            snap_cn["intraday_technicals"]["recent_bars_ohlc"] = cn_bars
            res_cn = ds.evaluate_holding_health(
                "CU0", "long", position=pos_cn, archive_state=_make_armed_state("long", 70000.0),
                snapshot=snap_cn, ref_date=ref,
            )
            self.assertEqual(res_cn["status"], ds.STATUS_GREEN, msg=f"cn_times={cn_times}, alerts={res_cn['yellow_alerts']}")

        # 3) CN futures missing afternoon bar (13:30 -> 14:30, missing 14:00) STILL flags intraday_internal_bar_gap!
        cn_bars_missing = [
            {"bar": f"2026-11-23 {t}", "open": 74500.0, "high": 75500.0, "low": 74200.0, "close": 75000.0}
            for t in ["09:00", "09:30", "10:00", "10:30", "11:00", "13:30", "14:30"]
        ]
        snap_cn_gap = _make_healthy_snapshot(
            symbol="CU0", price=75000.0, daily_zd=70000.0, daily_zg=73500.0, intra_zd=72500.0, intra_zg=74500.0,
            daily_bias="long", intraday_bias="long",
            daily_bar="2026-11-23", intraday_bar=cn_bars_missing[-1]["bar"],
        )
        snap_cn_gap["asset_class"] = "cn_future"
        snap_cn_gap["intraday_technicals"]["recent_bars_ohlc"] = cn_bars_missing
        res_cn_gap = ds.evaluate_holding_health(
            "CU0", "long", position=pos_cn, archive_state=_make_armed_state("long", 70000.0),
            snapshot=snap_cn_gap, ref_date=ref,
        )
        self.assertEqual(res_cn_gap["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_internal_bar_gap", {a["code"] for a in res_cn_gap["yellow_alerts"]})

    def test_pre_effective_close_guard_and_future_timestamp_red_guard(self):
        state = _make_armed_state("long", stop_threshold=95.0)

        # 1) Codex Round-3 repro: 20:30Z closed price=101 vs 20:40Z stop effective=102
        #    -> YELLOW_LOOSENING (price_predates_stop_effective_ts), NOT RED_EXIT!
        #    Test both start-anchored (20:00Z..20:30Z) and end-anchored (20:30Z) representations.
        pos_pre = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos_pre["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        pos_pre["stop_updated_at_utc"] = "2026-11-23T20:40:00+00:00"

        for anchor_mode, bar_ts in (
            ("start", "2026-11-23T20:00:00+00:00"),  # bar [20:00Z, 20:30Z] ends at 20:30Z <= 20:40Z
            ("end", "2026-11-23T20:30:00+00:00"),    # bar ending at 20:30Z <= 20:40Z
        ):
            snap_pre = _make_healthy_snapshot(
                symbol="PWR", price=101.0, daily_zd=95.0, daily_zg=105.0, intra_zd=100.0, intra_zg=105.0,
                daily_bar="2026-11-23", intraday_bar=bar_ts,
            )
            snap_pre["intraday_technicals"]["bar_timestamp_anchor"] = anchor_mode
            snap_pre["intraday_technicals"]["last_bar_low"] = 100.5
            snap_pre["intraday_technicals"]["recent_bars_ohlc"] = [
                {"bar": "2026-11-23T14:30:00+00:00", "bar_utc": "2026-11-23T14:30:00+00:00", "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0},
                {"bar": bar_ts, "bar_utc": bar_ts, "open": 101.5, "high": 102.0, "low": 100.5, "close": 101.0},
            ]
            res_pre = ds.evaluate_holding_health(
                "PWR", "long", position=pos_pre, archive_state=state, snapshot=snap_pre,
                now_utc=datetime(2026, 11, 23, 20, 45, tzinfo=timezone.utc),
            )
            self.assertEqual(res_pre["status"], ds.STATUS_YELLOW, msg=f"anchor_mode={anchor_mode}")
            self.assertEqual(res_pre["red_alerts"], [], msg=f"anchor_mode={anchor_mode}, reds={res_pre['red_alerts']}")
            self.assertIn("price_predates_stop_effective_ts", {a["code"] for a in res_pre["yellow_alerts"]})

        # 2) Codex Round-3 repro: future bar +1h ahead of now_utc (bar_is_closed=False, low=99)
        #    -> YELLOW_LOOSENING (intraday_30m_future_timestamp), NOT RED_EXIT!
        now_utc = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
        future_ts = (now_utc + timedelta(hours=1)).isoformat()  # 19:00Z > 18:00Z + 5m
        pos_fut = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=102.0, structural_stop_daily=95.0
        )
        pos_fut["opened_at_utc"] = "2026-11-23T14:30:00+00:00"

        snap_fut = _make_healthy_snapshot(
            symbol="PWR", price=99.5, daily_zd=95.0, daily_zg=105.0, intra_zd=101.0, intra_zg=108.0,
            daily_bar="2026-11-23", intraday_bar=future_ts,
        )
        snap_fut["intraday_technicals"]["bar_is_closed"] = False
        snap_fut["intraday_technicals"]["last_bar_low"] = 99.0
        snap_fut["intraday_technicals"]["recent_bars_ohlc"] = [
            {"bar": "2026-11-23T17:30:00+00:00", "bar_utc": "2026-11-23T17:30:00+00:00", "open": 107.0, "high": 109.0, "low": 106.5, "close": 108.0},
            {"bar": future_ts, "bar_utc": future_ts, "open": 108.0, "high": 108.5, "low": 99.0, "close": 99.5},
        ]
        res_fut = ds.evaluate_holding_health(
            "PWR", "long", position=pos_fut, archive_state=state, snapshot=snap_fut, now_utc=now_utc
        )
        self.assertEqual(res_fut["status"], ds.STATUS_YELLOW)
        self.assertEqual(res_fut["red_alerts"], [])
        self.assertIn("intraday_30m_future_timestamp", {a["code"] for a in res_fut["yellow_alerts"]})

    def test_2027_dragon_boat_holiday_and_lc2706_ltd(self):
        # 2027 State Council official notice: Dragon Boat Festival holiday is June 9 only (June 10-11 are trading days)
        self.assertFalse(ds._is_cn_trading_day(date(2027, 6, 9)))
        self.assertTrue(ds._is_cn_trading_day(date(2027, 6, 10)))
        self.assertTrue(ds._is_cn_trading_day(date(2027, 6, 11)))

        # GFEX LC2706: 10th CN trading day of June 2027 skipping June 9 -> 2027-06-15!
        self.assertEqual(ds._estimate_cn_future_ltd("LC", 2027, 6), date(2027, 6, 15))
        extracted = {c["contract"]: c for c in ds._extract_futures_contracts("LC2706", ref_date=date(2027, 5, 1))}
        self.assertEqual(extracted["LC2706"]["est_last_trading_day"], date(2027, 6, 15))

    def test_round4_long_same_session_5bar_gap_blocks_green(self):
        # Codex Round-4 repro: PWR missing 5 consecutive 30m bars (15:00Z -> 18:00Z, 3h gap) MUST block GREEN_HOLD!
        pos = _make_base_position(
            symbol="PWR", direction="long", entry_price=100.0, tactical_stop_30m=96.0, structural_stop_daily=92.0
        )
        pos["opened_at_utc"] = "2026-11-23T14:30:00+00:00"
        state = {
            "instrument": {"symbol": "PWR", "market": "US"},
            "status": "holding",
            "triggers": [{"id": "t1", "kind": "chanlun", "metric": "close", "operator": "<=", "threshold": 96.0, "action": "止损", "status": "armed"}],
            "events": [],
        }
        snap = _make_healthy_snapshot(
            symbol="PWR", price=110.0, daily_zd=95.0, daily_zg=105.0, intra_zd=106.0, intra_zg=112.0,
            daily_bar="2026-11-23", intraday_bar="2026-11-23T20:30:00+00:00",
        )
        snap["intraday_technicals"]["recent_bars_ohlc"] = [
            {"bar": "2026-11-23T14:30:00+00:00", "bar_utc": "2026-11-23T14:30:00+00:00", "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0},
            {"bar": "2026-11-23T15:00:00+00:00", "bar_utc": "2026-11-23T15:00:00+00:00", "open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0},
            # Missing 15:30, 16:00, 16:30, 17:00, 17:30 (delta == 3h)
            {"bar": "2026-11-23T18:00:00+00:00", "bar_utc": "2026-11-23T18:00:00+00:00", "open": 105.0, "high": 108.0, "low": 104.5, "close": 107.0},
            {"bar": "2026-11-23T18:30:00+00:00", "bar_utc": "2026-11-23T18:30:00+00:00", "open": 107.0, "high": 109.0, "low": 106.5, "close": 108.0},
            {"bar": "2026-11-23T19:00:00+00:00", "bar_utc": "2026-11-23T19:00:00+00:00", "open": 108.0, "high": 110.0, "low": 107.5, "close": 109.0},
            {"bar": "2026-11-23T19:30:00+00:00", "bar_utc": "2026-11-23T19:30:00+00:00", "open": 109.0, "high": 110.5, "low": 108.5, "close": 109.5},
            {"bar": "2026-11-23T20:00:00+00:00", "bar_utc": "2026-11-23T20:00:00+00:00", "open": 109.5, "high": 110.8, "low": 109.0, "close": 110.0},
            {"bar": "2026-11-23T20:30:00+00:00", "bar_utc": "2026-11-23T20:30:00+00:00", "open": 110.0, "high": 111.0, "low": 109.5, "close": 110.0},
        ]
        res = ds.evaluate_holding_health(
            "PWR", "long", position=pos, archive_state=state, snapshot=snap,
            now_utc=datetime(2026, 11, 23, 21, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(res["status"], ds.STATUS_YELLOW)
        self.assertIn("intraday_internal_bar_gap", {a["code"] for a in res["yellow_alerts"]})


if __name__ == "__main__":
    unittest.main()
