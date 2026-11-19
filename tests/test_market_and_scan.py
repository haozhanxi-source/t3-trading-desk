#!/usr/bin/env python3
"""Offline unit tests for market_snapshot (including cn_future/akshare) and scan_opportunities."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from market_snapshot import (  # noqa: E402
    fetch_cn_future_daily,
    get_snapshot,
    normalize_cn_future_frame,
    normalize_symbol,
    period_to_start_date,
)
from scan_opportunities import (  # noqa: E402
    build_scan_row,
    load_universes,
    main as scan_main,
    resolve_targets,
    scan_targets,
    sort_scan_rows,
)


def make_cn_sample_df(rows: int = 30, chinese_cols: bool = True) -> pd.DataFrame:
    dates = pd.date_range("2026-01-02", periods=rows, freq="B")
    closes = np.linspace(700.0, 760.0, rows)
    if chinese_cols:
        return pd.DataFrame({
            "日期": dates.strftime("%Y-%m-%d"),
            "开盘价": closes - 1.5,
            "最高价": closes + 3.0,
            "最低价": closes - 3.0,
            "收盘价": closes,
            "成交量": [120000] * rows,
            "持仓量": [350000] * rows,
            "动态结算价": closes,
        })
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": closes - 1.5,
        "high": closes + 3.0,
        "low": closes - 3.0,
        "close": closes,
        "volume": [120000] * rows,
        "hold": [350000] * rows,
        "settle": closes,
    })


class TestNormalizeSymbol(unittest.TestCase):
    def test_cn_future_codes_and_aliases(self):
        self.assertEqual(normalize_symbol("I0"), ("I0", "cn_future"))
        self.assertEqual(normalize_symbol("rb0"), ("RB0", "cn_future"))
        self.assertEqual(normalize_symbol("铁矿"), ("I0", "cn_future"))
        self.assertEqual(normalize_symbol("铁矿石"), ("I0", "cn_future"))
        self.assertEqual(normalize_symbol("TA0"), ("TA0", "cn_future"))
        self.assertEqual(normalize_symbol("PTA期货"), ("TA0", "cn_future"))
        self.assertEqual(normalize_symbol("CN:PTA"), ("TA0", "cn_future"))
        self.assertEqual(normalize_symbol("PVC期货"), ("V0", "cn_future"))
        self.assertEqual(normalize_symbol("CN:PVC"), ("V0", "cn_future"))
        self.assertEqual(normalize_symbol("碳酸锂"), ("LC0", "cn_future"))
        self.assertEqual(normalize_symbol("集运欧线"), ("EC0", "cn_future"))
        self.assertEqual(normalize_symbol("上海原油"), ("SC0", "cn_future"))

    def test_non_cn_future_assets_not_misclassified(self):
        self.assertEqual(normalize_symbol("AA"), ("AA", "us_stock_or_etf"))
        self.assertEqual(normalize_symbol("MP"), ("MP", "us_stock_or_etf"))
        self.assertEqual(normalize_symbol("PTA"), ("PTA", "us_stock_or_etf"))
        self.assertEqual(normalize_symbol("PVC"), ("PVC", "us_stock_or_etf"))
        self.assertEqual(normalize_symbol("US:PTA"), ("PTA", "us_stock_or_etf"))
        self.assertEqual(normalize_symbol("600519"), ("600519.SS", "a_share"))
        self.assertEqual(normalize_symbol("002050"), ("002050.SZ", "a_share"))
        self.assertEqual(normalize_symbol("920002"), ("920002.BJ", "a_share"))
        self.assertEqual(normalize_symbol("GC=F"), ("GC=F", "commodity"))
        self.assertEqual(normalize_symbol("HRC=F"), ("HRC=F", "commodity"))
        self.assertEqual(normalize_symbol("BTC"), ("BTC-USD", "crypto"))
        self.assertEqual(normalize_symbol("BTC/USDT"), ("BTC-USD", "crypto"))
        self.assertEqual(normalize_symbol("铜"), ("HG=F", "commodity"))
        self.assertEqual(normalize_symbol("咖啡"), ("KC=F", "commodity"))
        self.assertEqual(normalize_symbol("铝"), ("ALI=F", "commodity"))
        self.assertEqual(normalize_symbol("^VIX"), ("^VIX", "macro"))


class TestCnFutureFrameAndPeriod(unittest.TestCase):
    def test_period_to_start_date(self):
        anchor = "2026-03-15"
        self.assertEqual(period_to_start_date("6mo", today=anchor), "20250915")
        self.assertEqual(period_to_start_date("1y", today=anchor), "20250315")
        self.assertEqual(period_to_start_date("2y", today=anchor), "20240315")
        self.assertEqual(period_to_start_date("3y", today=anchor), "20230315")
        self.assertEqual(period_to_start_date("5y", today=anchor), "20210315")
        with self.assertRaises(ValueError):
            period_to_start_date("invalid_period", today=anchor)

    def test_normalize_cn_future_frame_chinese_columns(self):
        raw = pd.DataFrame({
            "日期": ["2026-01-05", "2026-01-03", "2026-01-04"],
            "开盘价": [102.0, 100.0, np.nan],
            "最高价": [105.0, 101.0, 103.0],
            "最低价": [101.0, 99.0, 100.0],
            "收盘价": [104.0, 100.5, 102.0],
            "成交量": [2000, 1000, 1500],
            "持仓量": [5000, 4800, 4900],
            "动态结算价": [103.5, 100.2, 101.8],
        })
        df = normalize_cn_future_frame(raw)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(len(df), 2)
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertEqual(df.index[0].strftime("%Y-%m-%d"), "2026-01-03")
        self.assertEqual(df["Close"].iloc[1], 104.0)
        self.assertEqual(df["Volume"].dtype, float)

    def test_normalize_cn_future_frame_english_columns(self):
        raw = pd.DataFrame({
            "date": ["2026-01-06", "2026-01-05"],
            "open": ["3500", "3480"],
            "high": ["3520", "3510"],
            "low": ["3490", "3470"],
            "close": ["3515", "3495"],
            "volume": ["80000", None],
            "hold": [100000, 99000],
            "settle": [3510, 3490],
        })
        df = normalize_cn_future_frame(raw)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.index[0].strftime("%Y-%m-%d"), "2026-01-05")
        self.assertEqual(df["Volume"].iloc[0], 0.0)
        self.assertEqual(df["Close"].iloc[1], 3515.0)

    def test_normalize_cn_future_frame_edge_dates_and_duplicates(self):
        # Missing Date column with default RangeIndex should return empty (not 1970 timestamps)
        no_date_raw = pd.DataFrame({
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
        })
        self.assertTrue(normalize_cn_future_frame(no_date_raw).empty)

        # Integer YYYYMMDD date column + unordered duplicate dates should preserve last raw occurrence
        dup_raw = pd.DataFrame({
            "date": [20260106, 20260105, 20260106],
            "open": [100.0, 90.0, 110.0],
            "high": [105.0, 95.0, 115.0],
            "low": [95.0, 85.0, 105.0],
            "close": [102.0, 92.0, 112.0],
            "volume": [1000, 900, 2000],
        })
        df_dup = normalize_cn_future_frame(dup_raw)
        self.assertEqual(len(df_dup), 2)
        self.assertEqual(df_dup.index[0].strftime("%Y-%m-%d"), "2026-01-05")
        self.assertEqual(df_dup.index[1].strftime("%Y-%m-%d"), "2026-01-06")
        self.assertEqual(df_dup["Close"].iloc[1], 112.0)

    def test_fetch_cn_future_daily_fallback(self):
        fallback_raw = make_cn_sample_df(rows=25, chinese_cols=False)
        with patch("market_snapshot.ak.futures_main_sina", side_effect=RuntimeError("main api error")) as mock_main, \
             patch("market_snapshot.ak.futures_zh_daily_sina", return_value=fallback_raw) as mock_fb:
            df = fetch_cn_future_daily("RB0", period="5y")
            self.assertEqual(mock_main.call_count, 1)
            self.assertEqual(mock_fb.call_count, 1)
            self.assertEqual(len(df), 25)
            self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])

    def test_get_snapshot_cn_future_skips_yfinance(self):
        sample_raw = make_cn_sample_df(rows=40, chinese_cols=True)
        with patch("market_snapshot.ak.futures_main_sina", return_value=sample_raw), \
             patch("market_snapshot.yf.Ticker", side_effect=AssertionError("yfinance should not be called for cn_future")):
            snap = get_snapshot("I0", period="1y", include_macro=False, include_fundamentals=True)
        self.assertNotIn("error", snap)
        self.assertEqual(snap["symbol"], "I0")
        self.assertEqual(snap["asset_class"], "cn_future")
        self.assertEqual(snap["data_source"], "akshare/sina 主力连续")
        self.assertEqual(snap["fundamentals"], {})
        self.assertIn("chanlun", snap)
        self.assertIn("technicals", snap)


class TestScanOpportunities(unittest.TestCase):
    def test_load_universes_has_expected_pools(self):
        universes = load_universes()
        self.assertIn("commodities_ferrous", universes)
        self.assertIn("commodities_chem_newenergy", universes)
        self.assertIn("shipping", universes)
        self.assertIn("us_power_grid", universes)
        for name, meta in universes.items():
            self.assertIn("description", meta, msg=name)
            self.assertIn("symbols", meta, msg=name)
            self.assertGreater(len(meta["symbols"]), 0, msg=name)

    def test_resolve_targets_dedup_and_unknown_universe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            u_path = os.path.join(tmpdir, "universes.json")
            with open(u_path, "w", encoding="utf-8") as f:
                json.dump({
                    "pool_a": {"description": "A", "symbols": ["I0", "NVDA"]},
                    "pool_b": {"description": "B", "symbols": ["i0", "铁矿", "RB0"]},
                }, f)

            targets = resolve_targets(
                universe_names=["pool_a", "pool_b"],
                symbols=["nvda", "GC=F"],
                universes_path=u_path,
            )
            self.assertEqual(
                targets,
                [("pool_a", "I0"), ("pool_a", "NVDA"), ("pool_b", "RB0"), ("adhoc", "GC=F")],
            )

            with self.assertRaises(ValueError) as ctx:
                resolve_targets(universe_names=["missing_pool"], universes_path=u_path)
            self.assertIn("missing_pool", str(ctx.exception))
            self.assertIn("pool_a", str(ctx.exception))

    def test_unknown_universe_cli_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            scan_main(["--universe", "definitely_unknown_pool"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_scan_targets_error_isolation_and_tier_sort(self):
        def fake_snap(tier: str, symbol: str):
            return {
                "symbol": symbol,
                "technicals": {
                    "last_close": 100.0,
                    "return_5d_pct": 1.0,
                    "return_20d_pct": 2.0,
                    "return_60d_pct": 3.0,
                    "atr14_pct": 2.5,
                },
                "chanlun": {
                    "signal_summary": {"tier": tier, "summary": f"s-{tier}", "action_bias": "bias"},
                    "daily": {
                        "macro_structure": "单中枢盘整",
                        "zhongshu_count": 1,
                        "latest_zhongshu": {"zd": 95.0, "zg": 105.0},
                        "price_vs_zhongshu": "inside_zs(50%)",
                        "latest_fractal": None,
                        "momentum_assessment": {"verdict": "neutral_extension_up"},
                        "signals": [],
                    },
                },
            }

        def fake_fetcher(sym, **kwargs):
            if sym == "BAD":
                raise RuntimeError("network timeout")
            if sym == "ERR_DICT":
                return {"error": "empty data"}
            if sym == "MALFORMED":
                return {}
            if sym == "S_Z":
                return fake_snap("中枢震荡", sym)
            if sym == "S_B":
                return fake_snap("买卖点信号", sym)
            return fake_snap("背驰观察", sym)

        rows = scan_targets(
            [("adhoc", "S_Z"), ("adhoc", "BAD"), ("adhoc", "S_B"), ("adhoc", "ERR_DICT"), ("adhoc", "MALFORMED"), ("adhoc", "S_D")],
            fetcher=fake_fetcher,
            sort_mode="tier",
        )
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["symbol"] for r in rows], ["S_B", "S_D", "S_Z", "BAD", "ERR_DICT", "MALFORMED"])
        self.assertIn("error", rows[3])
        self.assertIn("error", rows[4])
        self.assertIn("error", rows[5])

        # Verify build_scan_row handles normal and error snapshots
        self.assertEqual(build_scan_row("adhoc", "X", {"error": "boom"})["error"], "boom")
        sorted_none = sort_scan_rows(rows, sort_mode="none")
        self.assertEqual(len(sorted_none), 6)


if __name__ == "__main__":
    unittest.main()
