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

    def test_lesson5_stroke_revision_and_lesson10_compact_zhongshu(self):
        # Simple 3-stroke uptrend (no entering pullback consolidation) should NOT invent a Zhongshu
        df_simple = build_wave_df([(100.0, 1), (118.0, 8), (110.0, 7), (132.0, 9), (124.0, 7)])
        res_simple = analyze_chanlun(df_simple, timeframe_label="日线")
        self.assertEqual(res_simple["zhongshu_count"], 0)

        # Consolidation Zhongshu: core box must stay strictly 3 strokes even when 5 more strokes oscillate inside
        df_ext = build_wave_df([
            (95.0, 1),
            (120.0, 10),  # entering up stroke
            (105.0, 8),   # b1
            (118.0, 8),   # b2
            (106.0, 8),   # b3 -> core box ends here
            (117.0, 8),   # osc 1
            (107.0, 8),   # osc 2
            (116.0, 8),   # osc 3
            (108.0, 8),   # osc 4
            (115.0, 8),   # confirms osc 3 & 4
        ])
        res_ext = analyze_chanlun(df_ext, timeframe_label="日线")
        zs = res_ext["latest_zhongshu"]
        self.assertIsNotNone(zs)
        self.assertEqual(zs["stroke_count"], 3)
        self.assertTrue(zs["is_extended"])
        self.assertGreaterEqual(zs["oscillation_strokes"], 3)
        # Core box end_date must precede the final oscillation bar
        self.assertLess(zs["end_date"], str(df_ext.index[-1].date()))

    def test_lesson9_14_steep_crash_non_divergence_and_lesson11_trigger(self):
        # Prior slow drop 15 bars vs sharp 3-bar crash (-22%) -> should NOT be labeled as weak divergence
        rows = []
        # Initial rise to 130
        for p in np.linspace(100.0, 130.0, 12):
            rows.append((p - 0.5, p + 0.5, p - 0.8, p))
        # First down leg: 130 -> 105 over 14 bars
        for p in np.linspace(129.0, 105.0, 14):
            rows.append((p + 0.8, p + 1.0, p - 0.5, p))
        # Bounce: 105 -> 118 over 8 bars
        for p in np.linspace(106.0, 118.0, 8):
            rows.append((p - 0.6, p + 0.5, p - 0.8, p))
        # Sharp 3-bar crash: 118 -> 92
        rows.append((116.0, 117.0, 108.0, 109.0))
        rows.append((108.0, 109.0, 99.0, 100.0))
        rows.append((99.0, 100.0, 92.0, 93.0))
        # Rebound 2 bars to confirm bottom fractal & check Lesson 5/11
        rows.append((94.0, 98.0, 93.5, 97.5))
        rows.append((98.0, 104.0, 97.0, 103.0))
        df = make_df(rows, start="2026-01-01")
        res = analyze_chanlun(df, timeframe_label="日线")
        # Confirm 118->92 was captured by Lesson 5 revision as a confirmed down stroke with strong class
        down_strokes = [s for s in res["recent_strokes"] if s["direction"] == "down"]
        self.assertTrue(any(s["end_price"] <= 92.5 and s["is_confirmed"] for s in down_strokes))
        # Test Lesson 11 trigger when no reverse K-line exists inside down stroke, and later bar breaks its high
        df_no_rev = make_df([
            (115.0, 116.0, 110.0, 111.0),
            (111.0, 112.0, 106.0, 107.0),
            (107.0, 108.0, 100.0, 101.0),  # pure yin down stroke ending at high=108/101
            (102.0, 109.0, 101.0, 108.5),  # breaks above stroke's last bar high
        ], start="2026-02-01")
        from chanlun import Stroke, detect_stroke_end_trigger
        pure_dn = Stroke(
            direction="down", start_date="2026-02-02", end_date="2026-02-04",
            start_price=116.0, end_price=100.0, high=116.0, low=100.0,
            start_merged_idx=0, end_merged_idx=2, start_raw_idx=0, end_raw_idx=2,
            raw_bars=3, pct_change=-13.8, macd_area=5.0, macd_peak=2.0, dif_extreme=-2.0,
            efficiency=4.6, avg_vol_ratio=1.0, is_confirmed=True,
        )
        trig_no_rev = detect_stroke_end_trigger(df_no_rev, [pure_dn])
        self.assertIsNotNone(trig_no_rev)
        self.assertTrue(trig_no_rev["triggered"])

    def test_segment_zhongshu_upgrade_on_nine_strokes(self):
        # Build confirmed segments + 9-stroke oscillation so Stroke ZS upgrades cleanly to confirmed Segment ZS
        points = [
            (120.0, 1), (95.0, 8), (112.0, 7), (102.0, 7), (132.0, 8),  # Seg 1 up (95->132)
            (108.0, 7), (122.0, 7), (100.0, 7),                         # Seg 2 down (132->100)
            (126.0, 7), (110.0, 7), (135.0, 7),                         # Seg 3 up (100->135)
            (114.0, 7), (124.0, 7), (108.0, 7), (121.0, 7), (109.0, 7), (120.0, 7), (110.0, 7), (119.0, 7), (106.0, 7),  # Seg 4 down (9+ strokes)
            (128.0, 7), (115.0, 7), (136.0, 7), (125.0, 7),             # Seg 5 up confirms Seg 4 with all-confirmed strokes
        ]
        df = build_wave_df(points)
        res = analyze_chanlun(df, timeframe_label="日线")
        self.assertGreaterEqual(len(res["segment_zhongshus"]), 1)
        self.assertEqual(res["zhongshu_level"], "segment")
        self.assertIn("段中枢", res["zhongshu_level_label"])

    def test_codex_regressions_segments_tiers_fake_yin_and_fresh_divergence(self):
        from chanlun import Stroke, build_segments, evaluate_daily_signal_tier, _has_lesson5_substructure, Fractal

        def mk(d, sp, ep, idx):
            return Stroke(
                direction=d, start_date=f"2026-01-{idx*2+1:02d}", end_date=f"2026-01-{idx*2+2:02d}",
                start_price=sp, end_price=ep, high=max(sp, ep), low=min(sp, ep),
                start_merged_idx=idx*4, end_merged_idx=idx*4+4, start_raw_idx=idx*4, end_raw_idx=idx*4+4,
                raw_bars=5, pct_change=0.0, macd_area=10.0, macd_peak=2.0, dif_extreme=1.0,
                efficiency=1.0, avg_vol_ratio=1.0, is_confirmed=True,
            )

        # 1) Trailing counter stroke must not end an up segment; no skipped strokes / adjacent same-dir segments / 1-stroke confirmed segments
        segs1 = build_segments([mk("up", 100, 120, 0), mk("down", 120, 110, 1), mk("up", 110, 130, 2), mk("down", 130, 115, 3)])
        self.assertEqual([(s.direction, s.start_price, s.end_price) for s in segs1], [("up", 100, 130)])
        segs2 = build_segments([
            mk("up", 100, 120, 0), mk("down", 120, 110, 1), mk("up", 110, 130, 2),
            mk("down", 130, 95, 3), mk("up", 95, 135, 4), mk("down", 135, 105, 5), mk("up", 105, 145, 6),
        ])
        for a, b in zip(segs2, segs2[1:]):
            self.assertNotEqual(a.direction, b.direction)
        self.assertEqual(segs2[-1].end_price, 145)
        for seg in segs2:
            if seg.is_confirmed:
                self.assertGreaterEqual(seg.stroke_count, 3)

        # Unconfirmed right-edge stroke must NOT confirm prior segment break
        s_unconf = [
            mk("up", 100, 120, 0), mk("down", 120, 110, 1), mk("up", 110, 130, 2),
            mk("down", 130, 105, 3), mk("up", 105, 122, 4),
        ]
        s_last_unconf = mk("down", 122, 102, 5)
        s_last_unconf.is_confirmed = False
        segs_unconf = build_segments(s_unconf + [s_last_unconf])
        self.assertTrue(all(not s.is_confirmed for s in segs_unconf))

        # 2) Non-first 3B vs watch-only tier separation + "找大哥(多强空弱)" must NOT trigger sell branch
        t_non_first = evaluate_daily_signal_tier({"signals": ["日线非首中枢三买确认(回踩不破ZG=100+底分型)"], "price_vs_zhongshu": "above_zg(+1%)"})
        self.assertIn("非首中枢", t_non_first["summary"])
        t_watch = evaluate_daily_signal_tier({"signals": ["日线首中枢准三买观察(回踩ZG=100上方)"], "price_vs_zhongshu": "above_zg(+1%)"})
        self.assertNotEqual(t_watch["tier"], "买卖点信号")
        t_brother = evaluate_daily_signal_tier({"signals": ["日线3出(D3·首次底抬高)/二买确认(回踩不破前低95.0+找大哥(多强空弱))"], "price_vs_zhongshu": "inside_zs(50%)"})
        self.assertIn("建仓", t_brother["action_bias"])
        self.assertNotIn("做空", t_brother["action_bias"])

        # 3) Fake yin (closes > prev_close) must NOT pass Lesson 5 substructure as a real pullback
        df_fake = make_df([
            (100.0, 102.0, 99.5, 101.0),
            (103.0, 103.5, 101.5, 102.0),  # fake yin: close < open, BUT close 102.0 > prev_close 101.0!
            (102.2, 104.5, 102.0, 104.0),
        ], start="2026-03-01")
        f_bot = Fractal(kind="bottom", merged_idx=0, high=102.0, low=99.5, price=99.5, raw_idx=0, date="2026-03-01", confirmed_by_idx=1, confirmed_date="2026-03-02", strength="strong")
        f_top = Fractal(kind="top", merged_idx=2, high=104.5, low=102.0, price=104.5, raw_idx=2, date="2026-03-03", confirmed_by_idx=3, confirmed_date="2026-03-04", strength="strong")
        self.assertFalse(_has_lesson5_substructure(df_fake, f_bot, f_top))

        # 4) Fresh 2-bar rebound right after a slow divergent drop must preserve 1B divergence signal,
        # while high-peak (area_ratio=0.50, peak_ratio=1.50) capital-consensus must NOT trigger divergence 1B
        df_div = build_wave_df([(120.0, 1), (130.0, 8), (95.0, 8), (108.0, 8), (93.0, 18), (97.0, 3)])
        res_div = analyze_chanlun(df_div, timeframe_label="日线")
        self.assertIn("背驰", " ".join(res_div["signals"]))

        from chanlun import classify_chanlun_signals, MergedBar
        s_prev = Stroke("down", "2026-01-01", "2026-01-15", 120, 100, 120, 100, 0, 10, 0, 10, 11, -16.7, 20.0, 2.0, -2.0, 1.5, 1.0, True, 4, "中")
        s_up1 = Stroke("up", "2026-01-15", "2026-01-22", 100, 110, 110, 100, 10, 15, 10, 15, 6, 10.0, 5.0, 1.0, 0.5, 1.6, 1.0, True, 4, "中")
        s_curr_dn = Stroke("down", "2026-01-22", "2026-01-30", 110, 96, 110, 96, 15, 20, 15, 20, 6, -12.7, 10.0, 3.0, -2.5, 2.1, 1.0, True, 4, "中")
        s_bounce = Stroke("up", "2026-01-30", "2026-02-01", 96, 99, 99, 96, 20, 21, 20, 21, 2, 3.1, 1.0, 0.5, -1.5, 1.5, 1.0, False, 4, "中")
        _, _, sigs_cons = classify_chanlun_signals("日线", 99.0, [], [], [], [s_prev, s_up1, s_curr_dn, s_bounce], [], {"verdict": "lower_high_rebound"})
        self.assertNotIn("一买", " ".join(sigs_cons))


if __name__ == "__main__":
    unittest.main()
