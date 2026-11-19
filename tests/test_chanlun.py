#!/usr/bin/env python3
"""Unit tests for Chanlun engine and snapshot/scanner integration."""
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from chanlun import (
    analyze_chanlun,
    assess_momentum,
    build_pivots,
    build_strokes,
    compute_macd,
    detect_fractals,
    detect_zhongshus,
    evaluate_daily_signal_tier,
    process_inclusion,
)
from market_snapshot import compute_indicators


def make_df(ohlc_rows: list[tuple[float, float, float, float]], start: str = "2026-01-01") -> pd.DataFrame:
    dates = pd.date_range(start=start, periods=len(ohlc_rows), freq="B")
    data = {
        "Open": [r[0] for r in ohlc_rows],
        "High": [r[1] for r in ohlc_rows],
        "Low": [r[2] for r in ohlc_rows],
        "Close": [r[3] for r in ohlc_rows],
        "Volume": [1_000_000.0 for _ in ohlc_rows],
    }
    return pd.DataFrame(data, index=dates)


def build_wave_df(turning_points: list[tuple[float, int]], start: str = "2025-01-01") -> pd.DataFrame:
    """Build synthetic OHLCV series interpolating cleanly between price turning points."""
    closes = []
    for i in range(len(turning_points) - 1):
        p0, _ = turning_points[i]
        p1, steps = turning_points[i + 1]
        seg = np.linspace(p0, p1, steps, endpoint=(i == len(turning_points) - 2))
        closes.extend(seg.tolist())

    rows = []
    for idx, c in enumerate(closes):
        prev_c = closes[idx - 1] if idx > 0 else c
        hi = max(c, prev_c) + 0.4
        lo = min(c, prev_c) - 0.4
        rows.append((prev_c, hi, lo, c))
    return make_df(rows, start=start)


class TestChanlunEngine(unittest.TestCase):
    def test_inclusion_up_and_down(self):
        # Upward movement followed by an inside bar -> takes higher high, higher low
        up_rows = [
            (10.0, 11.0, 9.5, 10.8),
            (10.8, 13.0, 10.5, 12.8),  # up bar establishes direction = 1
            (12.5, 12.6, 11.2, 11.8),  # contained inside bar[1]
            (11.9, 14.0, 12.0, 13.8),
        ]
        merged = process_inclusion(make_df(up_rows))
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[1].high, 13.0)
        self.assertEqual(merged[1].low, 11.2)

        # Downward movement followed by an outside bar -> takes lower high, lower low
        dn_rows = [
            (15.0, 15.5, 14.0, 14.2),
            (14.1, 14.3, 12.0, 12.2),  # down bar establishes direction = -1
            (12.3, 14.8, 11.5, 13.0),  # engulfs bar[1]
            (12.9, 13.0, 10.5, 10.8),
        ]
        merged_dn = process_inclusion(make_df(dn_rows))
        self.assertEqual(len(merged_dn), 3)
        self.assertEqual(merged_dn[1].high, 14.3)
        self.assertEqual(merged_dn[1].low, 11.5)

    def test_strict_strokes_and_zhongshu(self):
        # Construct an oscillation creating at least 4 strokes -> 1 Zhongshu
        # 100 -> 120 -> 105 -> 122 -> 108 -> 125
        df = build_wave_df([
            (100.0, 1),
            (120.0, 8),
            (105.0, 8),
            (122.0, 8),
            (108.0, 8),
            (125.0, 8),
            (123.0, 3),
        ])
        res = analyze_chanlun(df, timeframe_label="日线")
        self.assertNotIn("error", res)
        self.assertGreaterEqual(len(res["recent_strokes"]), 4)
        self.assertGreaterEqual(res["zhongshu_count"], 1)
        zs = res["latest_zhongshu"]
        self.assertIsNotNone(zs)
        self.assertGreater(zs["zg"], zs["zd"])

    def test_capital_consensus_down_vs_divergence(self):
        # Consensus down: gentle drop 120->112, bounce to 116, then violent accelerating crash 116->75
        df_consensus = build_wave_df([
            (100.0, 1),
            (120.0, 10),
            (112.0, 8),
            (116.0, 6),
            (75.0, 12),
        ])
        res_cons = analyze_chanlun(df_consensus, timeframe_label="日线")
        mom_cons = res_cons["momentum_assessment"]
        self.assertEqual(mom_cons["active_direction"], "down")
        self.assertTrue(mom_cons["is_capital_consensus"])
        self.assertFalse(mom_cons["is_divergence_candidate"])
        self.assertIn("资金方向一致", " ".join(res_cons["signals"]))

        # Divergence down: sharp crash 130->95, bounce to 105, then slow shallow drift to 93 + bounce
        df_div = build_wave_df([
            (120.0, 1),
            (130.0, 8),
            (95.0, 8),
            (108.0, 8),
            (93.0, 18),
            (97.0, 3),
        ])
        res_div = analyze_chanlun(df_div, timeframe_label="日线")
        mom_div = res_div["momentum_assessment"]
        self.assertLess(mom_div["area_ratio_vs_prev"], 0.85)
        self.assertTrue(mom_div["is_divergence_candidate"] or mom_div["verdict"] != "capital_consensus_down")

    def test_third_buy_detection(self):
        # Form Zhongshu around [105, 118], break out to 132, pull back to 122 (> ZG) with bounce
        df = build_wave_df([
            (100.0, 1),
            (118.0, 8),
            (105.0, 8),
            (119.0, 8),
            (106.0, 8),
            (135.0, 10),
            (123.0, 7),
            (126.0, 3),
        ])
        res = analyze_chanlun(df, timeframe_label="日线")
        sigs = " ".join(res["signals"])
        self.assertIn("三买", sigs)

    def test_no_rsi_in_snapshot_indicators(self):
        df = build_wave_df([(100.0, 1), (110.0, 15), (102.0, 15), (115.0, 15)])
        ind = compute_indicators(df)
        self.assertNotIn("rsi14", ind)
        self.assertNotIn("ema20", ind)
        self.assertNotIn("bb_upper", ind)
        self.assertIn("atr14", ind)
        self.assertIn("macd_dif", ind)

    def test_daily_signal_tier_consensus_and_nan_guard(self):
        daily = {
            "momentum_assessment": {"active_direction": "down", "is_capital_consensus": True, "is_divergence_candidate": False},
            "price_vs_zhongshu": "below_zd(-8.0%)",
            "latest_fractal": {"kind": "top", "confirmed_merged_bars_ago": 4},
            "signals": ["日线向下离开段·资金方向一致"],
        }
        res = evaluate_daily_signal_tier(daily)
        self.assertIn("资金方向一致", res["summary"])
        self.assertIn("勿逆势", res["tier"])

        # Verify NaN placeholder rows at right edge are dropped cleanly
        df = build_wave_df([(100.0, 1), (110.0, 10), (102.0, 10)])
        nan_row = pd.DataFrame(
            {"Open": [np.nan], "High": [np.nan], "Low": [np.nan], "Close": [np.nan], "Volume": [0.0]},
            index=[df.index[-1] + pd.Timedelta(days=1)],
        )
        df_with_nan = pd.concat([df, nan_row])
        cl = analyze_chanlun(df_with_nan, timeframe_label="日线")
        self.assertEqual(cl["raw_bars"], len(df))

    def test_right_edge_stroke_extension(self):
        # Prior down 120->105, bounce to 112 (6 bars), minor pause 102->104 (2 bars), then crash to 82 at right edge
        df = build_wave_df([
            (100.0, 1),
            (120.0, 8),
            (105.0, 7),
            (112.0, 6),
            (102.0, 6),
            (104.0, 2),
            (82.0, 8),
        ])
        res = analyze_chanlun(df, timeframe_label="日线")
        last_stroke = res["recent_strokes"][-1]
        self.assertEqual(last_stroke["direction"], "down")
        self.assertFalse(last_stroke["is_confirmed"])
        self.assertLess(last_stroke["end_price"], 85.0)
        self.assertTrue(res["momentum_assessment"]["is_capital_consensus"])


if __name__ == "__main__":
    unittest.main()
