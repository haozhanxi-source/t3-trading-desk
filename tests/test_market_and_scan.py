#!/usr/bin/env python3
"""Offline unit tests for market_snapshot (including cn_future/akshare) and scan_opportunities."""
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from market_snapshot import (  # noqa: E402
    compute_indicators,
    fetch_a_share_intraday,
    fetch_cn_future_daily,
    fetch_cn_future_intraday,
    fetch_crypto_intraday,
    get_intraday_snapshot,
    get_snapshot,
    main as snapshot_main,
    normalize_cn_future_frame,
    normalize_symbol,
    period_to_start_date,
)
from scan_opportunities import (  # noqa: E402
    build_scan_row,
    load_universes,
    main as scan_main,
    resolve_targets,
    save_universe,
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
        self.assertEqual(normalize_symbol("DXY"), ("DX-Y.NYB", "macro"))
        self.assertEqual(normalize_symbol("美元指数"), ("DX-Y.NYB", "macro"))
        self.assertEqual(normalize_symbol("EURUSD=X"), ("EURUSD=X", "fx"))
        self.assertEqual(normalize_symbol("美元日元"), ("USDJPY=X", "fx"))
        self.assertEqual(normalize_symbol("USDCNY"), ("USDCNY=X", "fx"))


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
        self.assertIn("fx_majors", universes)
        self.assertIn("a_shares_etfs", universes)
        for name, meta in universes.items():
            self.assertIn("description", meta, msg=name)
            self.assertIn("symbols", meta, msg=name)
            self.assertGreater(len(meta["symbols"]), 0, msg=name)

    def test_save_universe_persists_deduped_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            u_path = os.path.join(tmpdir, "universes.json")
            saved = save_universe(
                name="a_shares_grid_export",
                symbols=["002028", "002028.SZ", "600406"],
                description="A股电网设备出海龙头",
                universes_path=u_path,
            )
            self.assertEqual(saved["description"], "A股电网设备出海龙头")
            self.assertEqual(saved["symbols"], ["002028", "600406"])
            loaded = load_universes(u_path)
            self.assertIn("a_shares_grid_export", loaded)
            self.assertEqual(loaded["a_shares_grid_export"]["symbols"], ["002028", "600406"])

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


def make_intraday_wave_df(periods: int = 80, freq: str = "30min", start: str = "2026-03-02 09:30") -> pd.DataFrame:
    dates = pd.date_range(start=start, periods=periods, freq=freq)
    x = np.linspace(0, 6 * np.pi, periods)
    closes = 100.0 + 0.15 * np.arange(periods) + 4.0 * np.sin(x)
    opens = np.concatenate(([closes[0]], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    return pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": np.full(periods, 50000.0),
        },
        index=dates,
    )


class TestIntradayFetchersAndDualSnapshot(unittest.TestCase):
    def test_fetch_cn_future_intraday(self):
        times = pd.date_range("2026-03-02 09:30", periods=30, freq="30min")
        closes = np.linspace(700.0, 730.0, 30)
        raw = pd.DataFrame({
            "datetime": times.strftime("%Y-%m-%d %H:%M:%S"),
            "open": closes - 1.0,
            "high": closes + 2.0,
            "low": closes - 2.0,
            "close": closes,
            "volume": [10000] * 30,
            "hold": [20000] * 30,
        })
        # Reverse order + inject duplicate timestamp to verify dedup(keep="last") & sort
        dup_row = raw.iloc[[0]].copy()
        dup_row["close"] = 999.0
        raw_shuffled = pd.concat([raw.iloc[::-1], dup_row], ignore_index=True)

        with patch("market_snapshot.ak.futures_zh_minute_sina", return_value=raw_shuffled) as mock_fn:
            df = fetch_cn_future_intraday("I0", interval="30m")

        mock_fn.assert_called_once_with(symbol="I0", period="30")
        self.assertEqual(len(df), 30)
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertEqual(df.index[0].strftime("%Y-%m-%d %H:%M"), "2026-03-02 09:30")
        self.assertEqual(df["Close"].iloc[0], 999.0)
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])

    def test_fetch_a_share_intraday_sina_primary_and_em_fallback(self):
        times = pd.date_range("2026-03-02 10:00", periods=25, freq="30min")
        closes = np.linspace(1500.0, 1540.0, 25)
        sina_raw = pd.DataFrame({
            "day": times.strftime("%Y-%m-%d %H:%M:%S"),
            "open": closes - 2.0,
            "high": closes + 4.0,
            "low": closes - 4.0,
            "close": closes,
            "volume": [8000] * 25,
            "amount": [1e7] * 25,
        })
        with patch("market_snapshot.ak.stock_zh_a_minute", return_value=sina_raw) as mock_sina, \
             patch("market_snapshot.ak.stock_zh_a_hist_min_em", side_effect=AssertionError("should not hit fallback")):
            df_sina = fetch_a_share_intraday("600519.SS", interval="30m")

        mock_sina.assert_called_once_with(symbol="sh600519", period="30", adjust="qfq")
        self.assertEqual(len(df_sina), 25)
        self.assertEqual(df_sina.index[-1].strftime("%Y-%m-%d %H:%M"), times[-1].strftime("%Y-%m-%d %H:%M"))

        # Test .SZ and .BJ prefix conversion + fallback to stock_zh_a_hist_min_em
        em_raw = pd.DataFrame({
            "时间": times.strftime("%Y-%m-%d %H:%M:%S"),
            "开盘": closes - 1.0,
            "收盘": closes,
            "最高": closes + 3.0,
            "最低": closes - 3.0,
            "成交量": [9000] * 25,
        })
        with patch("market_snapshot.ak.stock_zh_a_minute", side_effect=RuntimeError("sina blocked")) as mock_sina_sz, \
             patch("market_snapshot.ak.stock_zh_a_hist_min_em", return_value=em_raw) as mock_em_sz:
            df_em = fetch_a_share_intraday("000001.SZ", interval="15m")

        mock_sina_sz.assert_called_once_with(symbol="sz000001", period="15", adjust="qfq")
        mock_em_sz.assert_called_once_with(symbol="000001", period="15", adjust="qfq")
        self.assertEqual(len(df_em), 25)
        self.assertEqual(df_em["Close"].iloc[-1], closes[-1])

        with patch("market_snapshot.ak.stock_zh_a_minute", return_value=sina_raw) as mock_sina_bj:
            fetch_a_share_intraday("830001.BJ", interval="60m")
        mock_sina_bj.assert_called_once_with(symbol="bj830001", period="60", adjust="qfq")

    def test_fetch_crypto_intraday_pagination_and_fallback(self):
        # Build 500 bars split across 2 pages (OKX returns 300 newest, then 200 older via after=oldest_ts)
        base_ms = int(pd.Timestamp("2026-01-01 00:00:00", tz="UTC").timestamp() * 1000)
        step_ms = 30 * 60 * 1000
        all_bars = [
            [base_ms + i * step_ms, 50000.0 + i, 50050.0 + i, 49950.0 + i, 50020.0 + i, 100.0]
            for i in range(500)
        ]
        page_older = all_bars[:200]
        page_newer = all_bars[200:]

        okx_instance = MagicMock()
        okx_instance.fetch_ohlcv.side_effect = [page_newer, page_older]

        with patch("market_snapshot.ccxt.okx", return_value=okx_instance) as mock_okx_cls:
            df_btc = fetch_crypto_intraday("BTC-USD", interval="30m", limit=500)

        mock_okx_cls.assert_called_once_with({"timeout": 8000})
        self.assertEqual(okx_instance.fetch_ohlcv.call_count, 2)
        first_call = okx_instance.fetch_ohlcv.call_args_list[0]
        second_call = okx_instance.fetch_ohlcv.call_args_list[1]
        self.assertEqual(first_call.args, ("BTC/USDT",))
        self.assertEqual(first_call.kwargs, {"timeframe": "30m", "limit": 500})
        self.assertEqual(second_call.kwargs["params"], {"after": str(page_newer[0][0])})
        self.assertEqual(len(df_btc), 500)
        self.assertTrue(df_btc.index.is_monotonic_increasing)
        self.assertEqual(df_btc["Close"].iloc[0], 50020.0)
        self.assertEqual(df_btc["Close"].iloc[-1], 50519.0)

        # Verify 60m -> 1h ccxt mapping and okx -> bybit -> binance fallback
        okx_fail = MagicMock()
        okx_fail.fetch_ohlcv.side_effect = RuntimeError("okx timeout")
        bybit_empty = MagicMock()
        bybit_empty.fetch_ohlcv.return_value = []
        binance_ok = MagicMock()
        binance_ok.fetch_ohlcv.return_value = all_bars[-50:]

        with patch("market_snapshot.ccxt.okx", return_value=okx_fail), \
             patch("market_snapshot.ccxt.bybit", return_value=bybit_empty), \
             patch("market_snapshot.ccxt.binance", return_value=binance_ok):
            df_eth = fetch_crypto_intraday("ETH-USD", interval="60m", limit=50)

        binance_ok.fetch_ohlcv.assert_called_once_with("ETH/USDT", timeframe="1h", limit=50)
        self.assertEqual(len(df_eth), 50)

    def test_get_intraday_snapshot_routing_and_validation(self):
        df_30m = make_intraday_wave_df(periods=80, freq="30min")

        # US stock via yfinance
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df_30m
        with patch("market_snapshot.yf.Ticker", return_value=mock_ticker):
            snap_us = get_intraday_snapshot("NVDA", interval="30m")

        mock_ticker.history.assert_called_once_with(period="60d", interval="30m")
        self.assertNotIn("error", snap_us)
        self.assertEqual(snap_us["symbol"], "NVDA")
        self.assertEqual(snap_us["asset_class"], "us_stock_or_etf")
        self.assertEqual(snap_us["interval"], "30m")
        self.assertEqual(snap_us["timeframe_label"], "30分钟")
        self.assertEqual(snap_us["data_source"], "yfinance 分钟")
        self.assertRegex(snap_us["technicals"]["as_of_bar"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(snap_us["chanlun"]["intraday"]["timeframe"], "30分钟")
        self.assertNotIn("日线", snap_us["chanlun"]["signal_summary"]["summary"])

        # FX via yfinance
        mock_fx = MagicMock()
        mock_fx.history.return_value = df_30m
        with patch("market_snapshot.yf.Ticker", return_value=mock_fx):
            snap_fx = get_intraday_snapshot("USDJPY", interval="60m")
        self.assertEqual(snap_fx["asset_class"], "fx")
        self.assertEqual(snap_fx["timeframe_label"], "60分钟")
        mock_fx.history.assert_called_once_with(period="60d", interval="60m")

        # CN future via akshare (must NOT call yfinance)
        with patch("market_snapshot.fetch_cn_future_intraday", return_value=df_30m) as mock_cn, \
             patch("market_snapshot.yf.Ticker", side_effect=AssertionError("no yfinance for cn_future")):
            snap_cn = get_intraday_snapshot("铁矿", interval="30m")
        mock_cn.assert_called_once_with("I0", interval="30m")
        self.assertEqual(snap_cn["data_source"], "akshare/sina 分钟")

        # A-share via akshare
        with patch("market_snapshot.fetch_a_share_intraday", return_value=df_30m) as mock_ashare:
            snap_a = get_intraday_snapshot("600519", interval="15m")
        mock_ashare.assert_called_once_with("600519.SS", interval="15m")
        self.assertEqual(snap_a["timeframe_label"], "15分钟")

        # Crypto via ccxt
        with patch("market_snapshot.fetch_crypto_intraday", return_value=df_30m) as mock_crypto:
            snap_c = get_intraday_snapshot("BTC", interval="30m")
        mock_crypto.assert_called_once_with("BTC-USD", interval="30m")
        self.assertEqual(snap_c["data_source"], "ccxt 分钟")

        # Invalid interval returns error dict
        snap_bad = get_intraday_snapshot("NVDA", interval="5m")
        self.assertIn("error", snap_bad)
        self.assertIn("5m", snap_bad["error"])

    def test_get_snapshot_dual_alignment_and_error_isolation(self):
        df_daily = make_intraday_wave_df(periods=90, freq="B", start="2025-10-01")
        df_30m = make_intraday_wave_df(periods=80, freq="30min", start="2026-03-02 09:30")

        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [df_daily, df_30m]
        with patch("market_snapshot.yf.Ticker", return_value=mock_ticker):
            snap = get_snapshot(
                "NVDA",
                include_macro=False,
                include_fundamentals=False,
                include_intraday=True,
                intraday_interval="30m",
            )

        self.assertNotIn("error", snap)
        self.assertNotIn("intraday_error", snap)
        self.assertIn("intraday_technicals", snap)
        self.assertRegex(snap["technicals"]["as_of_bar"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(snap["intraday_technicals"]["as_of_bar"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIn("intraday", snap["chanlun"])
        self.assertIn("intraday_signal_summary", snap["chanlun"])
        self.assertIn("dual_alignment", snap["chanlun"])
        self.assertEqual(snap["chanlun"]["dual_alignment"]["intraday_timeframe"], "30分钟")

        # Error isolation: intraday failure must NOT break daily snapshot
        mock_ticker_daily_only = MagicMock()
        mock_ticker_daily_only.history.return_value = df_daily
        with patch("market_snapshot.yf.Ticker", return_value=mock_ticker_daily_only), \
             patch("market_snapshot.get_intraday_snapshot", return_value={"error": "30m feed unavailable"}):
            snap_err = get_snapshot(
                "NVDA",
                include_macro=False,
                include_fundamentals=False,
                include_intraday=True,
                intraday_interval="30m",
            )

        self.assertNotIn("error", snap_err)
        self.assertIn("daily", snap_err["chanlun"])
        self.assertIn("technicals", snap_err)
        self.assertEqual(snap_err["intraday_error"], "30m feed unavailable")
        self.assertNotIn("dual_alignment", snap_err["chanlun"])

    def test_market_snapshot_cli_interval_and_dual(self):
        with patch("market_snapshot.get_intraday_snapshot", return_value={"ok": "intra"}) as mock_intra, \
             patch("sys.argv", ["market_snapshot.py", "NVDA", "--interval", "30m", "--full-chanlun"]), \
             patch("builtins.print"):
            snapshot_main()
        mock_intra.assert_called_once_with("NVDA", interval="30m", full_chanlun=True)

        with patch("market_snapshot.get_snapshot", return_value={"ok": "dual"}) as mock_dual, \
             patch("sys.argv", ["market_snapshot.py", "NVDA", "--dual", "--no-macro"]), \
             patch("builtins.print"):
            snapshot_main()
        mock_dual.assert_called_once_with(
            "NVDA",
            period="3y",
            include_macro=False,
            full_chanlun=False,
            include_intraday=True,
            intraday_interval="30m",
        )

        with patch("market_snapshot.get_snapshot", return_value={"ok": "dual60"}) as mock_dual60, \
             patch("sys.argv", ["market_snapshot.py", "NVDA", "--dual", "--interval", "60m", "--no-macro"]), \
             patch("builtins.print"):
            snapshot_main()
        mock_dual60.assert_called_once_with(
            "NVDA",
            period="3y",
            include_macro=False,
            full_chanlun=False,
            include_intraday=True,
            intraday_interval="60m",
        )

    def test_codex_round1_specific_contracts_intraday_indicators_and_cli_exit(self):
        # 1) Specific CN & overseas futures contracts normalize accurately without hijacking stock tickers
        for sym in ("CU2612", "AL2701", "SC2612", "TA705", "RB2610", "LC2611", "CN:CU2612"):
            norm, cls = normalize_symbol(sym)
            self.assertEqual(cls, "cn_future", msg=sym)
            self.assertEqual(norm, sym.replace("CN:", "").upper())

        for sym in ("ALIZ26.CMX", "CLZ26.NYM", "GCZ26.CMX", "ZCZ26.CBT", "KCZ26.NYB", "LEZ26.CME", "BRNZ26.ICE"):
            self.assertEqual(normalize_symbol(sym), (sym, "commodity"), msg=sym)

        for stock in ("MA", "V", "SI", "I", "M", "Y", "P", "US:CU2612"):
            norm, cls = normalize_symbol(stock)
            self.assertEqual(cls, "us_stock_or_etf", msg=stock)

        # 2) fetch_cn_future_daily skips futures_main_sina for specific contracts like CU2612
        sample_raw = make_cn_sample_df(rows=30, chinese_cols=False)
        with patch(
            "market_snapshot.ak.futures_main_sina",
            side_effect=AssertionError("futures_main_sina must not be called for specific contract CU2612"),
        ), patch("market_snapshot.ak.futures_zh_daily_sina", return_value=sample_raw) as mock_daily:
            df_cu = fetch_cn_future_daily("CU2612", period="1y")
        mock_daily.assert_called_once_with(symbol="CU2612")
        self.assertEqual(len(df_cu), 30)

        # get_snapshot routes CU2612 as cn_future without yfinance
        with patch("market_snapshot.fetch_cn_future_daily", return_value=df_cu), \
             patch("market_snapshot.yf.Ticker", side_effect=AssertionError("no yfinance for CU2612")):
            snap_cu = get_snapshot("CU2612", period="1y", include_macro=False, include_fundamentals=False)
        self.assertNotIn("error", snap_cu)
        self.assertEqual(snap_cu["symbol"], "CU2612")
        self.assertEqual(snap_cu["asset_class"], "cn_future")
        self.assertEqual(snap_cu["data_source"], "akshare/sina 期货合约")

        # 3) 30m compute_indicators emits *_bar keys (NO return_1d_pct/return_5d_pct/return_20d_pct),
        #    preserves tz-aware ISO offset, and reports bar_is_closed + last_bar_high/low + recent_bars_ohlc
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        closed_start = now_utc - timedelta(minutes=30 * 25 + 45)
        closed_idx = pd.date_range(closed_start, periods=25, freq="30min").tz_convert("America/New_York")
        df_30m_closed = make_intraday_wave_df(periods=25, freq="30min")
        df_30m_closed.index = closed_idx
        ind_closed = compute_indicators(df_30m_closed, interval="30m")

        self.assertEqual(ind_closed["bar_interval"], "30m")
        self.assertRegex(ind_closed["as_of_bar"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertIsNotNone(ind_closed["as_of_bar_utc"])
        self.assertTrue(ind_closed["as_of_bar_utc"].endswith("+00:00"))
        self.assertTrue(ind_closed["bar_is_closed"])
        self.assertIn("return_1bar_pct", ind_closed)
        self.assertIn("return_5bar_pct", ind_closed)
        self.assertIn("return_20bar_pct", ind_closed)
        self.assertIn("return_60bar_pct", ind_closed)
        self.assertIn("high_20bar", ind_closed)
        self.assertIn("low_20bar", ind_closed)
        self.assertIn("high_60bar", ind_closed)
        self.assertIn("low_60bar", ind_closed)
        for daily_only_key in ("return_1d_pct", "return_5d_pct", "return_20d_pct", "return_60d_pct", "high_20d", "low_20d"):
            self.assertNotIn(daily_only_key, ind_closed)
        self.assertEqual(ind_closed["last_bar_high"], round(float(df_30m_closed["High"].iloc[-1]), 4))
        self.assertEqual(ind_closed["last_bar_low"], round(float(df_30m_closed["Low"].iloc[-1]), 4))
        self.assertEqual(len(ind_closed["recent_bars_ohlc"]), 25)
        self.assertEqual(ind_closed["recent_bars_ohlc"][-1]["bar"], ind_closed["as_of_bar"])
        self.assertEqual(ind_closed["recent_bars_ohlc"][-1]["bar_utc"], ind_closed["as_of_bar_utc"])
        self.assertEqual(set(ind_closed["recent_bars_ohlc"][0].keys()), {"bar", "bar_utc", "open", "high", "low", "close"})

        # Currently forming tz-aware 30m bar -> bar_is_closed is False
        forming_end = now_utc - timedelta(minutes=10)
        forming_idx = pd.date_range(end=forming_end, periods=25, freq="30min", tz="UTC")
        df_30m_forming = make_intraday_wave_df(periods=25, freq="30min")
        df_30m_forming.index = forming_idx
        ind_forming = compute_indicators(df_30m_forming, interval="30m")
        self.assertFalse(ind_forming["bar_is_closed"])

        # Naive 30m bar -> bar_is_closed is None, as_of_bar_utc is None
        df_30m_naive = make_intraday_wave_df(periods=25, freq="30min")
        ind_naive = compute_indicators(df_30m_naive, interval="30m")
        self.assertIsNone(ind_naive["bar_is_closed"])
        self.assertIsNone(ind_naive["as_of_bar_utc"])
        self.assertIsNone(ind_naive["recent_bars_ohlc"][0]["bar_utc"])
        self.assertNotIn("return_1d_pct", ind_naive)
        self.assertIn("return_1bar_pct", ind_naive)

        # Daily compute_indicators preserves all daily keys unchanged (last 10 bars in recent_bars_ohlc)
        df_daily = make_intraday_wave_df(periods=70, freq="B", start="2026-01-02")
        ind_daily = compute_indicators(df_daily, interval="1d")
        self.assertEqual(ind_daily["bar_interval"], "1d")
        self.assertIsNone(ind_daily["bar_is_closed"])
        for k in ("return_1d_pct", "return_5d_pct", "return_20d_pct", "return_60d_pct", "return_120d_pct", "high_20d", "low_20d", "high_60d", "low_60d", "volume_ratio_20d"):
            self.assertIn(k, ind_daily)
        self.assertNotIn("return_1bar_pct", ind_daily)
        self.assertEqual(len(ind_daily["recent_bars_ohlc"]), 10)

        # 4) CLI exits non-zero (1) when snapshot returns {"error": ...}, after printing JSON
        printed: list[str] = []
        with patch("market_snapshot.get_snapshot", return_value={"error": "upstream timeout"}), \
             patch("sys.argv", ["market_snapshot.py", "BADSYM", "--no-macro"]), \
             patch("builtins.print", side_effect=lambda s: printed.append(s)):
            with self.assertRaises(SystemExit) as ctx:
                snapshot_main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(len(printed), 1)
        self.assertEqual(json.loads(printed[0]), {"error": "upstream timeout"})

    def test_codex_round2_cn_tz_bar_anchor_64bar_window_and_dual_exit(self):
        # 1) CN future & A-share 30m fetchers localize naive domestic timestamps to Asia/Shanghai (+08:00)
        #    so as_of_bar carries +08:00, as_of_bar_utc is populated, and bar_is_closed is a real bool (never None)
        cn_times = pd.date_range("2026-03-02 10:00", periods=30, freq="30min")
        closes = np.linspace(700.0, 730.0, 30)
        cn_fut_raw = pd.DataFrame({
            "datetime": cn_times.strftime("%Y-%m-%d %H:%M:%S"),
            "open": closes - 1.0,
            "high": closes + 2.0,
            "low": closes - 2.0,
            "close": closes,
            "volume": [10000] * 30,
        })
        with patch("market_snapshot.ak.futures_zh_minute_sina", return_value=cn_fut_raw):
            df_cn_fut = fetch_cn_future_intraday("I0", interval="30m")
        self.assertIsNotNone(df_cn_fut.index.tz)
        self.assertEqual(str(df_cn_fut.index.tz), "Asia/Shanghai")
        self.assertEqual(df_cn_fut.index[0].utcoffset(), timedelta(hours=8))

        with patch("market_snapshot.ak.futures_zh_minute_sina", return_value=cn_fut_raw):
            snap_cn_fut = get_intraday_snapshot("I0", interval="30m")
        self.assertNotIn("error", snap_cn_fut)
        cn_tech = snap_cn_fut["technicals"]
        self.assertTrue(cn_tech["as_of_bar"].endswith("+08:00"))
        self.assertIsNotNone(cn_tech["as_of_bar_utc"])
        self.assertTrue(cn_tech["as_of_bar_utc"].endswith("+00:00"))
        self.assertEqual(cn_tech["bar_timestamp_anchor"], "end")
        self.assertIsInstance(cn_tech["bar_is_closed"], bool)
        self.assertTrue(cn_tech["bar_is_closed"])

        # A-share Sina primary and Eastmoney fallback both return Asia/Shanghai (+08:00)
        sina_raw = pd.DataFrame({
            "day": cn_times.strftime("%Y-%m-%d %H:%M:%S"),
            "open": closes - 1.0,
            "high": closes + 2.0,
            "low": closes - 2.0,
            "close": closes,
            "volume": [8000] * 30,
        })
        with patch("market_snapshot.ak.stock_zh_a_minute", return_value=sina_raw):
            df_a_sina = fetch_a_share_intraday("600519.SS", interval="30m")
        self.assertIsNotNone(df_a_sina.index.tz)
        self.assertEqual(str(df_a_sina.index.tz), "Asia/Shanghai")
        self.assertEqual(df_a_sina.index[-1].utcoffset(), timedelta(hours=8))

        with patch("market_snapshot.ak.stock_zh_a_minute", return_value=sina_raw):
            snap_a = get_intraday_snapshot("600519", interval="30m")
        self.assertTrue(snap_a["technicals"]["as_of_bar"].endswith("+08:00"))
        self.assertIsNotNone(snap_a["technicals"]["as_of_bar_utc"])
        self.assertEqual(snap_a["technicals"]["bar_timestamp_anchor"], "end")
        self.assertIsInstance(snap_a["technicals"]["bar_is_closed"], bool)

        em_raw = pd.DataFrame({
            "时间": cn_times.strftime("%Y-%m-%d %H:%M:%S"),
            "开盘": closes - 1.0,
            "收盘": closes,
            "最高": closes + 2.0,
            "最低": closes - 2.0,
            "成交量": [9000] * 30,
        })
        with patch("market_snapshot.ak.stock_zh_a_minute", side_effect=RuntimeError("sina blocked")), \
             patch("market_snapshot.ak.stock_zh_a_hist_min_em", return_value=em_raw):
            df_a_em = fetch_a_share_intraday("000001.SZ", interval="30m")
        self.assertIsNotNone(df_a_em.index.tz)
        self.assertEqual(str(df_a_em.index.tz), "Asia/Shanghai")

        # 2) Exact bar_is_closed semantics for timestamp_anchor="end" vs "start" (including future/incomplete bars)
        #    Bar timestamp: 2026-06-01 10:00+08:00 == 2026-06-01 02:00:00+00:00
        sh_idx = pd.date_range("2026-06-01 09:30", periods=2, freq="30min", tz="Asia/Shanghai")
        df_sh_2 = make_intraday_wave_df(periods=2, freq="30min")
        df_sh_2.index = sh_idx
        # End-anchored CN bar at 10:00 CST (02:00 UTC): unclosed at 01:45 UTC, closed at 02:00 UTC
        ind_end_forming = compute_indicators(
            df_sh_2,
            interval="30m",
            timestamp_anchor="end",
            now_utc=datetime(2026, 6, 1, 1, 45, tzinfo=timezone.utc),
        )
        self.assertEqual(ind_end_forming["bar_timestamp_anchor"], "end")
        self.assertEqual(ind_end_forming["as_of_bar"], "2026-06-01T10:00+08:00")
        self.assertEqual(ind_end_forming["as_of_bar_utc"], "2026-06-01T02:00:00+00:00")
        self.assertFalse(ind_end_forming["bar_is_closed"])

        ind_end_closed = compute_indicators(
            df_sh_2,
            interval="30m",
            timestamp_anchor="end",
            now_utc=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(ind_end_closed["bar_is_closed"])

        # Start-anchored Western bar at 10:00+08:00 (02:00 UTC, ends 02:30 UTC):
        # unclosed at 01:50 UTC (future), unclosed at 02:15 UTC (mid-bar), closed at 02:30 UTC
        ind_start_future = compute_indicators(
            df_sh_2,
            interval="30m",
            timestamp_anchor="start",
            now_utc=datetime(2026, 6, 1, 1, 50, tzinfo=timezone.utc),
        )
        self.assertEqual(ind_start_future["bar_timestamp_anchor"], "start")
        self.assertFalse(ind_start_future["bar_is_closed"])

        ind_start_mid = compute_indicators(
            df_sh_2,
            interval="30m",
            timestamp_anchor="start",
            now_utc=datetime(2026, 6, 1, 2, 15, tzinfo=timezone.utc),
        )
        self.assertFalse(ind_start_mid["bar_is_closed"])

        ind_start_closed = compute_indicators(
            df_sh_2,
            interval="30m",
            timestamp_anchor="start",
            now_utc=datetime(2026, 6, 1, 2, 30, tzinfo=timezone.utc),
        )
        self.assertTrue(ind_start_closed["bar_is_closed"])

        with self.assertRaises(ValueError):
            compute_indicators(df_sh_2, interval="30m", timestamp_anchor="middle")

        # 3) 12-bar and 64-bar (from 80-bar) intraday windows preserve ALL 12 / last 64 bars with bar_utc populated
        df_12 = make_intraday_wave_df(periods=12, freq="30min")
        df_12.index = pd.date_range("2026-06-01 09:30", periods=12, freq="30min", tz="Asia/Shanghai")
        ind_12 = compute_indicators(df_12, interval="30m", timestamp_anchor="end")
        self.assertEqual(len(ind_12["recent_bars_ohlc"]), 12)
        self.assertTrue(all(
            isinstance(item["bar_utc"], str) and item["bar_utc"].endswith("+00:00")
            for item in ind_12["recent_bars_ohlc"]
        ))
        self.assertEqual(ind_12["recent_bars_ohlc"][0]["bar"], "2026-06-01T09:30+08:00")
        self.assertEqual(ind_12["recent_bars_ohlc"][0]["bar_utc"], "2026-06-01T01:30:00+00:00")
        self.assertEqual(ind_12["recent_bars_ohlc"][-1]["bar_utc"], ind_12["as_of_bar_utc"])

        df_80 = make_intraday_wave_df(periods=80, freq="30min")
        df_80.index = pd.date_range("2026-06-01 09:30", periods=80, freq="30min", tz="America/New_York")
        ind_80 = compute_indicators(df_80, interval="30m", timestamp_anchor="start")
        self.assertEqual(len(ind_80["recent_bars_ohlc"]), 64)
        self.assertTrue(all(
            isinstance(item["bar_utc"], str) and item["bar_utc"].endswith("+00:00")
            for item in ind_80["recent_bars_ohlc"]
        ))
        self.assertEqual(ind_80["recent_bars_ohlc"][-1]["bar"], ind_80["as_of_bar"])
        self.assertEqual(ind_80["recent_bars_ohlc"][-1]["bar_utc"], ind_80["as_of_bar_utc"])

        # Crypto fetcher preserves UTC tz-awareness for accurate bar_is_closed
        base_ms = int(pd.Timestamp("2026-06-01 00:00:00", tz="UTC").timestamp() * 1000)
        crypto_bars = [
            [base_ms + i * 30 * 60 * 1000, 60000.0 + i, 60100.0 + i, 59900.0 + i, 60050.0 + i, 10.0]
            for i in range(20)
        ]
        okx_mock = MagicMock()
        okx_mock.fetch_ohlcv.return_value = crypto_bars
        with patch("market_snapshot.ccxt.okx", return_value=okx_mock):
            df_crypto = fetch_crypto_intraday("BTC-USD", interval="30m", limit=20)
        self.assertIsNotNone(df_crypto.index.tz)
        ind_crypto = compute_indicators(
            df_crypto,
            interval="30m",
            timestamp_anchor="start",
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(ind_crypto["bar_timestamp_anchor"], "start")
        self.assertTrue(ind_crypto["as_of_bar_utc"].endswith("+00:00"))
        self.assertTrue(ind_crypto["bar_is_closed"])

        # 4) CLI `market_snapshot.py PWR --dual` exits 1 when intraday_error is present (after printing JSON)
        dual_err_payload = {
            "symbol": "PWR",
            "technicals": {"last_close": 300.0},
            "chanlun": {"daily": {}},
            "intraday_error": "30m feed unavailable",
        }
        printed_dual: list[str] = []
        with patch("market_snapshot.get_snapshot", return_value=dual_err_payload), \
             patch("sys.argv", ["market_snapshot.py", "PWR", "--dual", "--no-macro"]), \
             patch("builtins.print", side_effect=lambda s: printed_dual.append(s)):
            with self.assertRaises(SystemExit) as ctx_dual:
                snapshot_main()
        self.assertEqual(ctx_dual.exception.code, 1)
        self.assertEqual(len(printed_dual), 1)
        self.assertEqual(json.loads(printed_dual[0])["intraday_error"], "30m feed unavailable")

        # Without error/intraday_error, --dual exits 0 (no SystemExit)
        with patch("market_snapshot.get_snapshot", return_value={"symbol": "PWR", "ok": True}), \
             patch("sys.argv", ["market_snapshot.py", "PWR", "--dual", "--no-macro"]), \
             patch("builtins.print"):
            snapshot_main()


if __name__ == "__main__":
    unittest.main()
