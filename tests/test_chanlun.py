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
    _format_bar_timestamps,
    analyze_chanlun,
    assess_dual_timeframe_alignment,
    assess_momentum,
    build_pivots,
    build_strokes,
    compute_macd,
    detect_fractals,
    detect_zhongshus,
    evaluate_daily_signal_tier,
    evaluate_signal_tier,
    process_inclusion,
    select_active_zhongshus,
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

    def test_assess_momentum_reliability_flags(self):
        from chanlun import Stroke

        def mk_stroke(direction, sp, ep, raw_bars, macd_area, macd_peak=2.0, dif=-1.5, idx=0):
            pct = round(((ep / sp) - 1.0) * 100.0, 2)
            eff = round(abs(pct) / max(1, raw_bars), 3)
            return Stroke(
                direction=direction,
                start_date=f"2026-01-{idx * 2 + 1:02d}",
                end_date=f"2026-01-{idx * 2 + 2:02d}",
                start_price=sp,
                end_price=ep,
                high=max(sp, ep),
                low=min(sp, ep),
                start_merged_idx=idx * 5,
                end_merged_idx=idx * 5 + raw_bars,
                start_raw_idx=idx * 5,
                end_raw_idx=idx * 5 + raw_bars,
                raw_bars=raw_bars,
                pct_change=pct,
                macd_area=macd_area,
                macd_peak=macd_peak,
                dif_extreme=dif,
                efficiency=eff,
                avg_vol_ratio=1.0,
                is_confirmed=True,
            )

        # Insufficient data & single stroke branches
        m_empty = assess_momentum([])
        self.assertEqual(m_empty["verdict"], "insufficient_data")
        self.assertFalse(m_empty["momentum_reliable"])
        self.assertIsInstance(m_empty["momentum_reliability_note"], str)
        self.assertTrue(len(m_empty["momentum_reliability_note"]) > 0)

        s_single = mk_stroke("down", 120.0, 100.0, raw_bars=8, macd_area=12.0, idx=0)
        m_single = assess_momentum([s_single])
        self.assertEqual(m_single["verdict"], "single_stroke_no_comparison")
        self.assertFalse(m_single["momentum_reliable"])
        self.assertIsInstance(m_single["momentum_reliability_note"], str)

        s_mid_up = mk_stroke("up", 100.0, 110.0, raw_bars=6, macd_area=6.0, dif=1.0, idx=1)

        # Short current stroke (<= 3 bars)
        s_short_curr = mk_stroke("down", 110.0, 95.0, raw_bars=2, macd_area=4.0, idx=2)
        m_short_curr = assess_momentum([s_single, s_mid_up, s_short_curr])
        self.assertFalse(m_short_curr["momentum_reliable"])
        self.assertIn("仅2根K线", m_short_curr["momentum_reliability_note"])
        self.assertIn("失真", m_short_curr["momentum_reliability_note"])

        # Short previous stroke (<= 3 bars)
        s_short_prev = mk_stroke("down", 120.0, 105.0, raw_bars=3, macd_area=5.0, idx=0)
        s_norm_curr = mk_stroke("down", 110.0, 95.0, raw_bars=8, macd_area=10.0, idx=2)
        m_short_prev = assess_momentum([s_short_prev, s_mid_up, s_norm_curr])
        self.assertFalse(m_short_prev["momentum_reliable"])
        self.assertIn("仅3根K线", m_short_prev["momentum_reliability_note"])

        # Zero prev.macd_area
        s_zero_prev = mk_stroke("down", 120.0, 105.0, raw_bars=7, macd_area=0.0, idx=0)
        m_zero_prev = assess_momentum([s_zero_prev, s_mid_up, s_norm_curr])
        self.assertFalse(m_zero_prev["momentum_reliable"])
        self.assertIsNone(m_zero_prev["area_ratio_vs_prev"])
        self.assertIn("近零", m_zero_prev["momentum_reliability_note"])

        # Zero curr.macd_area
        s_zero_curr = mk_stroke("down", 110.0, 95.0, raw_bars=7, macd_area=0.0, idx=2)
        m_zero_curr = assess_momentum([s_single, s_mid_up, s_zero_curr])
        self.assertFalse(m_zero_curr["momentum_reliable"])
        self.assertIn("近零", m_zero_curr["momentum_reliability_note"])

        # Extreme ratio (>= 8.0x) on 4-bar stroke
        s_small_prev = mk_stroke("down", 120.0, 112.0, raw_bars=6, macd_area=0.5, macd_peak=0.2, idx=0)
        s_spike_curr = mk_stroke("down", 116.0, 98.0, raw_bars=4, macd_area=5.325, macd_peak=2.0, idx=2)
        m_spike = assess_momentum([s_small_prev, s_mid_up, s_spike_curr])
        self.assertEqual(m_spike["area_ratio_vs_prev"], 10.65)
        self.assertFalse(m_spike["momentum_reliable"])
        self.assertIn("10.65x", m_spike["momentum_reliability_note"])

        # Normal comparison (>= 6 bars, non-zero areas, reasonable ratio)
        s_ok_prev = mk_stroke("down", 120.0, 102.0, raw_bars=8, macd_area=14.0, macd_peak=2.5, dif=-2.0, idx=0)
        s_ok_curr = mk_stroke("down", 112.0, 96.0, raw_bars=7, macd_area=16.0, macd_peak=2.8, dif=-2.4, idx=2)
        m_ok = assess_momentum([s_ok_prev, s_mid_up, s_ok_curr])
        self.assertTrue(m_ok["momentum_reliable"])
        self.assertIsNone(m_ok["momentum_reliability_note"])
        self.assertTrue(m_ok["is_capital_consensus"])

    def test_analyze_chanlun_full_export_and_snapshot_flag(self):
        from unittest.mock import patch
        import market_snapshot

        points = [
            (120.0, 1), (95.0, 8), (112.0, 7), (102.0, 7), (132.0, 8),
            (108.0, 7), (122.0, 7), (100.0, 7),
            (126.0, 7), (110.0, 7), (135.0, 7),
            (114.0, 7), (124.0, 7), (108.0, 7), (121.0, 7), (109.0, 7), (120.0, 7), (110.0, 7), (119.0, 7), (106.0, 7),
            (128.0, 7), (115.0, 7), (136.0, 7), (125.0, 7),
        ]
        df = build_wave_df(points)

        full_keys = ("all_strokes", "all_segments", "all_stroke_zhongshus", "all_segment_zhongshus")

        res_default = analyze_chanlun(df, timeframe_label="日线")
        for key in full_keys:
            self.assertNotIn(key, res_default)

        res_full = analyze_chanlun(df, timeframe_label="日线", full_export=True)
        for key in full_keys:
            self.assertIn(key, res_full)
            self.assertIsInstance(res_full[key], list)

        self.assertGreater(len(res_full["all_strokes"]), len(res_full["recent_strokes"]))
        self.assertEqual(res_full["all_strokes"][-5:], res_full["recent_strokes"])
        self.assertEqual(len(res_full["all_segments"]), res_full["segment_count"])
        self.assertEqual(res_full["all_stroke_zhongshus"][-3:], res_full["stroke_zhongshus"])
        self.assertEqual(res_full["all_segment_zhongshus"][-3:], res_full["segment_zhongshus"])
        self.assertGreaterEqual(len(res_full["all_segment_zhongshus"]), 1)

        # Verify market_snapshot.get_snapshot forwards full_chanlun to analyze_chanlun
        with patch("market_snapshot.fetch_cn_future_daily", return_value=df):
            snap_default = market_snapshot.get_snapshot("I0", include_macro=False, include_fundamentals=False)
            snap_full = market_snapshot.get_snapshot(
                "I0", include_macro=False, include_fundamentals=False, full_chanlun=True
            )
        for key in full_keys:
            self.assertNotIn(key, snap_default["chanlun"]["daily"])
            self.assertIn(key, snap_full["chanlun"]["daily"])

        # Verify CLI --full-chanlun flag passes full_chanlun=True
        with patch("market_snapshot.get_snapshot", return_value={"ok": True}) as mock_snap, \
             patch("sys.argv", ["market_snapshot.py", "NVDA", "--no-macro", "--full-chanlun"]), \
             patch("builtins.print"):
            market_snapshot.main()
        mock_snap.assert_called_once_with("NVDA", period="3y", include_macro=False, full_chanlun=True)

    def test_intraday_timestamp_preservation_and_dynamic_labels(self):
        # 1) _format_bar_timestamps daily vs tz-aware daily vs intraday vs duplicate calendar dates
        daily_idx = pd.date_range("2026-01-05", periods=5, freq="B")
        self.assertEqual(_format_bar_timestamps(daily_idx)[0], "2026-01-05")
        daily_tz_idx = pd.date_range("2026-01-05", periods=5, freq="B", tz="America/New_York")
        self.assertEqual(_format_bar_timestamps(daily_tz_idx)[0], "2026-01-05")

        intra_idx = pd.date_range("2026-01-05 09:30", periods=4, freq="30min")
        self.assertEqual(
            _format_bar_timestamps(intra_idx),
            ["2026-01-05 09:30", "2026-01-05 10:00", "2026-01-05 10:30", "2026-01-05 11:00"],
        )
        dup_midnight_idx = pd.DatetimeIndex(["2026-01-05 00:00", "2026-01-05 00:00"])
        self.assertEqual(_format_bar_timestamps(dup_midnight_idx), ["2026-01-05 00:00", "2026-01-05 00:00"])

        # 2) Build intraday 30m wave DataFrame and verify timestamps + dynamic 30分钟 labels
        df_base = build_wave_df([
            (100.0, 1),
            (118.0, 8),
            (105.0, 8),
            (119.0, 8),
            (106.0, 8),
            (135.0, 10),
            (123.0, 7),
            (126.0, 3),
        ])
        df_30m = df_base.copy()
        df_30m.index = pd.date_range("2026-03-02 09:30", periods=len(df_30m), freq="30min")

        ind_30m = compute_indicators(df_30m)
        self.assertRegex(ind_30m["as_of_bar"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        ind_daily = compute_indicators(df_base)
        self.assertRegex(ind_daily["as_of_bar"], r"^\d{4}-\d{2}-\d{2}$")

        res_30m = analyze_chanlun(df_30m, timeframe_label="30分钟")
        self.assertNotIn("error", res_30m)
        self.assertEqual(res_30m["timeframe"], "30分钟")
        self.assertEqual(res_30m["zhongshu_level_label"], "30分钟笔中枢")
        self.assertNotIn("日线", res_30m["zhongshu_level_label"])
        for st in res_30m["recent_strokes"]:
            self.assertRegex(st["start_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            self.assertRegex(st["end_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(res_30m["latest_zhongshu"]["start_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(res_30m["latest_zhongshu"]["end_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(res_30m["latest_fractal"]["date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(res_30m["stroke_end_trigger"]["ref_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

        sig_30m = evaluate_signal_tier(res_30m)
        self.assertIn("30分钟", sig_30m["summary"])
        self.assertNotIn("日线", sig_30m["summary"])
        self.assertNotIn("日线", sig_30m["action_bias"])

        # Segment Zhongshu upgrade on 30m should carry "30分钟一级线段中枢(...)"
        seg_points = [
            (120.0, 1), (95.0, 8), (112.0, 7), (102.0, 7), (132.0, 8),
            (108.0, 7), (122.0, 7), (100.0, 7),
            (126.0, 7), (110.0, 7), (135.0, 7),
            (114.0, 7), (124.0, 7), (108.0, 7), (121.0, 7), (109.0, 7), (120.0, 7), (110.0, 7), (119.0, 7), (106.0, 7),
            (128.0, 7), (115.0, 7), (136.0, 7), (125.0, 7),
        ]
        df_seg_30m = build_wave_df(seg_points)
        df_seg_30m.index = pd.date_range("2026-03-02 09:30", periods=len(df_seg_30m), freq="30min")
        res_seg_30m = analyze_chanlun(df_seg_30m, timeframe_label="30分钟")
        self.assertEqual(res_seg_30m["zhongshu_level"], "segment")
        self.assertTrue(res_seg_30m["zhongshu_level_label"].startswith("30分钟一级线段中枢("))
        self.assertNotIn("日线", res_seg_30m["zhongshu_level_label"])

        # Direct select_active_zhongshus empty-case label check
        lvl, lbl, zss = select_active_zhongshus([], [], [], timeframe_label="15分钟")
        self.assertEqual((lvl, lbl, zss), ("stroke", "15分钟笔中枢", []))

        # evaluate_signal_tier explicit override & backward-compatible evaluate_daily_signal_tier
        mock_zs = {"price_vs_zhongshu": "inside_zs(50%)", "signals": []}
        self.assertIn("60分钟", evaluate_signal_tier(mock_zs, timeframe_label="60分钟")["summary"])
        self.assertIn("60分钟", evaluate_signal_tier(mock_zs, timeframe_label="60分钟")["action_bias"])
        self.assertIn("日线", evaluate_daily_signal_tier(mock_zs)["summary"])
        self.assertIn("30分钟", evaluate_daily_signal_tier({"timeframe": "30分钟", **mock_zs})["summary"])

    def test_assess_dual_timeframe_alignment_all_regimes(self):
        expected_keys = {
            "desired_direction",
            "daily_bias",
            "daily_tier",
            "daily_summary",
            "daily_signal_meta",
            "daily_zd",
            "daily_zg",
            "daily_tactical_stop",
            "daily_structural_stop",
            "daily_reverse_k_watch",
            "intraday_timeframe",
            "intraday_bias",
            "intraday_action",
            "intraday_tier",
            "intraday_summary",
            "intraday_signal_meta",
            "intraday_zd",
            "intraday_zg",
            "intraday_tactical_stop",
            "intraday_reverse_k_watch",
            "aligned",
            "alignment_status",
            "noise_reduction_pct",
            "notes",
        }

        daily_long = {
            "timeframe": "日线",
            "last_close": 120.0,
            "price_vs_zhongshu": "above_zg(+9.09%)",
            "latest_zhongshu": {"zd": 100.0, "zg": 110.0},
            "momentum_assessment": {"active_direction": "up", "is_capital_consensus": True},
            "stroke_end_trigger": {"direction": "up", "level": 108.0, "triggered": False},
            "recent_strokes": [{"direction": "up", "start_price": 106.0, "end_price": 120.0, "low": 106.0, "high": 120.0, "is_confirmed": False}],
            "signals": ["日线首中枢三买确认(回踩不破ZG=110.0+底分型)"],
        }
        intraday_long_3b = {
            "timeframe": "30分钟",
            "last_close": 120.0,
            "price_vs_zhongshu": "above_zg(+2.56%)",
            "latest_zhongshu": {"zd": 115.0, "zg": 117.0},
            "momentum_assessment": {"active_direction": "up", "is_capital_consensus": False},
            "stroke_end_trigger": {"direction": "up", "level": 117.6, "triggered": False},
            "recent_strokes": [{"direction": "up", "start_price": 117.2, "end_price": 120.0, "low": 117.2, "high": 120.0, "is_confirmed": False}],
            "signals": ["30分钟首中枢三买确认(回踩不破ZG=117.0+底分型)"],
        }

        # 1) Aligned long trigger: structural stops separated from Lesson 11 reverse-K watch
        #    noise_reduction_pct = (1 - (120.0 - 117.2) / (120.0 - 106.0)) * 100 = (1 - 2.8 / 14.0) * 100 = 80.0%
        a_long = assess_dual_timeframe_alignment(daily_long, intraday_long_3b, desired_direction="long")
        self.assertEqual(set(a_long.keys()), expected_keys)
        self.assertTrue(a_long["aligned"])
        self.assertEqual(a_long["alignment_status"], "aligned_trigger")
        self.assertEqual(a_long["daily_bias"], "long")
        self.assertEqual(a_long["intraday_bias"], "long")
        self.assertEqual(a_long["intraday_action"], "entry_long")
        self.assertTrue(a_long["intraday_signal_meta"]["confirmed"])
        self.assertEqual(a_long["intraday_signal_meta"]["signal_type"], "confirmed_buy")
        self.assertEqual(a_long["daily_zd"], 100.0)
        self.assertEqual(a_long["daily_zg"], 110.0)
        self.assertEqual(a_long["daily_tactical_stop"], 106.0)
        self.assertEqual(a_long["daily_structural_stop"], 100.0)
        self.assertEqual(a_long["daily_reverse_k_watch"], 108.0)
        self.assertEqual(a_long["intraday_zd"], 115.0)
        self.assertEqual(a_long["intraday_zg"], 117.0)
        self.assertEqual(a_long["intraday_tactical_stop"], 117.2)
        self.assertEqual(a_long["intraday_reverse_k_watch"], 117.6)
        self.assertEqual(a_long["noise_reduction_pct"], 80.0)

        # 2) Symmetric short alignment when desired_direction is None:
        #    daily_tactical_stop=110.0, daily_structural_stop=112.0 (ZG), daily_reverse_k_watch=108.0
        #    intraday_tactical_stop=102.0 -> noise_reduction_pct = (1 - 2.0 / 10.0) * 100 = 80.0%
        daily_short = {
            "timeframe": "日线",
            "last_close": 100.0,
            "price_vs_zhongshu": "below_zd(-5.0%)",
            "latest_zhongshu": {"zd": 105.0, "zg": 112.0},
            "momentum_assessment": {"active_direction": "down", "is_capital_consensus": True},
            "reverse_k_trigger": 108.0,
            "recent_strokes": [{"direction": "down", "start_price": 110.0, "end_price": 100.0, "low": 100.0, "high": 110.0, "is_confirmed": False}],
            "signals": ["日线向下离开段·资金方向一致"],
        }
        intraday_short_3s = {
            "timeframe": "30分钟",
            "last_close": 100.0,
            "price_vs_zhongshu": "below_zd(-1.5%)",
            "latest_zhongshu": {"zd": 101.5, "zg": 103.5},
            "momentum_assessment": {"active_direction": "down", "is_capital_consensus": False},
            "recent_strokes": [{"direction": "down", "start_price": 102.0, "end_price": 100.0, "low": 100.0, "high": 102.0, "is_confirmed": False}],
            "signals": ["30分钟首中枢三卖确认(反抽不过ZD=101.5+顶分型)"],
        }
        a_short = assess_dual_timeframe_alignment(daily_short, intraday_short_3s)
        self.assertTrue(a_short["aligned"])
        self.assertEqual(a_short["alignment_status"], "aligned_trigger")
        self.assertEqual(a_short["daily_bias"], "short")
        self.assertEqual(a_short["daily_signal_meta"]["signal_type"], "momentum_short")
        self.assertFalse(a_short["daily_signal_meta"]["confirmed"])
        self.assertEqual(a_short["intraday_action"], "entry_short")
        self.assertTrue(a_short["intraday_signal_meta"]["confirmed"])
        self.assertEqual(a_short["intraday_signal_meta"]["signal_type"], "confirmed_sell")
        self.assertEqual(a_short["daily_tactical_stop"], 110.0)
        self.assertEqual(a_short["daily_structural_stop"], 112.0)
        self.assertEqual(a_short["daily_reverse_k_watch"], 108.0)
        self.assertEqual(a_short["intraday_tactical_stop"], 102.0)
        self.assertIsNone(a_short["intraday_reverse_k_watch"])
        self.assertEqual(a_short["noise_reduction_pct"], 80.0)

        # 3) Daily opposes desired_direction -> vetoed even when 30m shows long signal
        a_oppose = assess_dual_timeframe_alignment(daily_short, intraday_long_3b, desired_direction="long")
        self.assertFalse(a_oppose["aligned"])
        self.assertEqual(a_oppose["alignment_status"], "daily_opposes_direction")

        # 4) Daily OK, 30m still oscillating -> daily_ok_wait_30m
        intraday_wait = {
            "timeframe": "30分钟",
            "last_close": 120.0,
            "price_vs_zhongshu": "inside_zs(50%)",
            "latest_zhongshu": {"zd": 118.0, "zg": 122.0},
            "momentum_assessment": {"active_direction": "down", "is_capital_consensus": False},
            "signals": ["30分钟中枢震荡中([ZD=118.0, ZG=122.0])"],
        }
        a_wait = assess_dual_timeframe_alignment(daily_long, intraday_wait, desired_direction="long")
        self.assertFalse(a_wait["aligned"])
        self.assertEqual(a_wait["alignment_status"], "daily_ok_wait_30m")
        self.assertEqual(a_wait["intraday_action"], "wait_structure")

        # 5) Exhaustion warnings: non-first ZS 3B and non-divergence counter-signal steep crash
        intraday_nonfirst_3b = {
            "timeframe": "30分钟",
            "last_close": 125.0,
            "price_vs_zhongshu": "above_zg(+2.0%)",
            "signals": ["30分钟第2个中枢·非首中枢三买确认(回踩不破ZG=122.0+底分型)"],
        }
        a_exh1 = assess_dual_timeframe_alignment(daily_long, intraday_nonfirst_3b, desired_direction="long")
        self.assertFalse(a_exh1["aligned"])
        self.assertEqual(a_exh1["intraday_action"], "exhaustion_warning")
        self.assertEqual(a_exh1["alignment_status"], "daily_ok_wait_30m")
        self.assertTrue(a_exh1["intraday_signal_meta"]["is_exhaustion"])
        self.assertFalse(a_exh1["intraday_signal_meta"]["confirmed"])

        intraday_steep_crash = {
            "timeframe": "30分钟",
            "last_close": 115.0,
            "price_vs_zhongshu": "below_zd(-2.0%)",
            "momentum_assessment": {"active_direction": "down", "is_non_divergence_power": True},
            "signals": ["30分钟非背驰背离(急跌2类笔·斜率2.5x,area=0.4x·仍有空头力量勿猜1买)"],
        }
        a_exh2 = assess_dual_timeframe_alignment(daily_long, intraday_steep_crash, desired_direction="long")
        self.assertFalse(a_exh2["aligned"])
        self.assertEqual(a_exh2["intraday_action"], "exhaustion_warning")
        self.assertEqual(a_exh2["intraday_signal_meta"]["signal_type"], "steep_non_div_down")
        self.assertFalse(a_exh2["intraday_signal_meta"]["confirmed"])

        # 6) Neutral daily -> neutral_wait
        daily_neutral = {
            "timeframe": "日线",
            "price_vs_zhongshu": "inside_zs(50%)",
            "signals": ["日线中枢震荡中([ZD=100.0, ZG=110.0])"],
        }
        a_neutral = assess_dual_timeframe_alignment(daily_neutral, intraday_long_3b, desired_direction="long")
        self.assertFalse(a_neutral["aligned"])
        self.assertEqual(a_neutral["alignment_status"], "neutral_wait")
        self.assertIsNone(a_neutral["noise_reduction_pct"])

        # 7) Invalid desired_direction raises ValueError
        with self.assertRaises(ValueError):
            assess_dual_timeframe_alignment(daily_long, intraday_long_3b, desired_direction="sideways")

    def test_codex_round1_dst_signal_meta_stops_and_unclamped_noise(self):
        # 1) DST fall-back timezone offset preservation: 01:30-04:00 vs 01:30-05:00 stay distinct
        dst_idx = pd.date_range("2026-11-01 00:30", periods=5, freq="30min", tz="America/New_York")
        formatted_dst = _format_bar_timestamps(dst_idx)
        self.assertEqual(
            formatted_dst,
            [
                "2026-11-01T00:30-04:00",
                "2026-11-01T01:00-04:00",
                "2026-11-01T01:30-04:00",
                "2026-11-01T01:00-05:00",
                "2026-11-01T01:30-05:00",
            ],
        )
        self.assertEqual(len(set(formatted_dst)), 5)

        # 2) Candidate ("一买候选" / "一卖候选" / "疑似" / "准") and bare 30m "多头资金方向一致推进"
        #    MUST have confirmed=False and produce wait_structure (NOT aligned_trigger)
        daily_cons_up = {
            "timeframe": "日线",
            "last_close": 100.0,
            "price_vs_zhongshu": "above_zg(+4.0%)",
            "latest_zhongshu": {"zd": 85.0, "zg": 96.0},
            "momentum_assessment": {"active_direction": "up", "is_capital_consensus": True},
            "recent_strokes": [{"direction": "up", "start_price": 92.0, "end_price": 100.0, "low": 92.0, "high": 100.0, "is_confirmed": False}],
            "signals": ["日线多头资金方向一致推进(area=1.3x,peak=1.2x·3类笔)"],
        }

        candidate_cases = [
            "30分钟盘整背驰+底分型结构(一买候选·5类笔·area=0.45x)",
            "30分钟盘整背驰+底分型确认(一买候选·6类弱笔·area=0.40x)",
            "30分钟下跌力度衰减(疑似盘整背驰·5类笔·area=0.50x·待底分型)",
            "30分钟首中枢准三买观察(回踩ZG=98.0上方)",
        ]
        for sig_text in candidate_cases:
            intra_cand = {
                "timeframe": "30分钟",
                "last_close": 100.0,
                "latest_fractal": {"kind": "bottom", "date": "2026-11-01T01:30-04:00", "confirmed_date": "2026-11-01T01:00-05:00", "just_confirmed": True},
                "signals": [sig_text],
            }
            sig_eval = evaluate_signal_tier(intra_cand, timeframe_label="30分钟")
            self.assertEqual(sig_eval["signal_meta"]["signal_type"], "candidate_buy", msg=sig_text)
            self.assertEqual(sig_eval["signal_meta"]["direction"], "long", msg=sig_text)
            self.assertFalse(sig_eval["signal_meta"]["confirmed"], msg=sig_text)
            self.assertEqual(sig_eval["signal_meta"]["signal_timestamp"], "2026-11-01T01:00-05:00")

            aln = assess_dual_timeframe_alignment(daily_cons_up, intra_cand, desired_direction="long")
            self.assertEqual(aln["daily_bias"], "long")
            self.assertEqual(aln["intraday_action"], "wait_structure", msg=sig_text)
            self.assertFalse(aln["aligned"], msg=sig_text)
            self.assertEqual(aln["alignment_status"], "daily_ok_wait_30m", msg=sig_text)

        # Candidate sell ("一卖候选") also stays confirmed=False
        intra_cand_sell = {
            "timeframe": "30分钟",
            "last_close": 100.0,
            "signals": ["30分钟趋势背驰+顶分型确认(一卖候选·6类弱笔·area=0.42x)"],
        }
        sell_cand_eval = evaluate_signal_tier(intra_cand_sell, timeframe_label="30分钟")
        self.assertEqual(sell_cand_eval["signal_meta"]["signal_type"], "candidate_sell")
        self.assertEqual(sell_cand_eval["signal_meta"]["direction"], "short")
        self.assertFalse(sell_cand_eval["signal_meta"]["confirmed"])

        # Bare 30m "多头资金方向一致推进" -> momentum_long, confirmed=False, wait_structure (NOT entry_long!)
        intra_bare_mom = {
            "timeframe": "30分钟",
            "last_close": 100.0,
            "price_vs_zhongshu": "above_zg(+2.0%)",
            "momentum_assessment": {"active_direction": "up", "is_capital_consensus": True},
            "signals": ["30分钟多头资金方向一致推进(area=1.2x,peak=1.1x·3类笔)"],
        }
        mom_eval = evaluate_signal_tier(intra_bare_mom, timeframe_label="30分钟")
        self.assertEqual(mom_eval["signal_meta"]["signal_type"], "momentum_long")
        self.assertEqual(mom_eval["signal_meta"]["direction"], "long")
        self.assertFalse(mom_eval["signal_meta"]["confirmed"])
        aln_mom = assess_dual_timeframe_alignment(daily_cons_up, intra_bare_mom, desired_direction="long")
        self.assertEqual(aln_mom["daily_bias"], "long")
        self.assertEqual(aln_mom["intraday_action"], "wait_structure")
        self.assertFalse(aln_mom["aligned"])
        self.assertEqual(aln_mom["alignment_status"], "daily_ok_wait_30m")

        # Weak daily running stroke ("常规运行中") MUST stay daily_bias == "neutral"
        daily_weak = {
            "timeframe": "日线",
            "last_close": 100.0,
            "price_vs_zhongshu": "no_zhongshu",
            "momentum_assessment": {"active_direction": "up", "verdict": "neutral_extension_up"},
            "signals": ["日线向上笔常规运行中(6类弱笔)"],
        }
        aln_weak = assess_dual_timeframe_alignment(daily_weak, intra_bare_mom, desired_direction="long")
        self.assertEqual(aln_weak["daily_bias"], "neutral")
        self.assertEqual(aln_weak["alignment_status"], "neutral_wait")

        # 3) Confirmed 30m 1B / 2B / 3B all give confirmed=True and aligned_trigger
        confirmed_buy_signals = [
            "30分钟盘整背驰+底分型启动(一买/盘背·5类笔·area=0.45x)",
            "30分钟3出(D3·首次底抬高)/二买确认(回踩不破前低95.0+找大哥(多强空弱))",
            "30分钟首中枢三买确认(回踩不破ZG=97.0+底分型)",
        ]
        for sig_text in confirmed_buy_signals:
            intra_conf = {
                "timeframe": "30分钟",
                "last_close": 100.0,
                "recent_strokes": [{"direction": "up", "start_price": 96.0, "end_price": 100.0, "low": 96.0, "high": 100.0, "is_confirmed": False}],
                "signals": [sig_text],
            }
            aln_conf = assess_dual_timeframe_alignment(daily_cons_up, intra_conf, desired_direction="long")
            self.assertTrue(aln_conf["intraday_signal_meta"]["confirmed"], msg=sig_text)
            self.assertEqual(aln_conf["intraday_signal_meta"]["signal_type"], "confirmed_buy", msg=sig_text)
            self.assertEqual(aln_conf["intraday_action"], "entry_long", msg=sig_text)
            self.assertTrue(aln_conf["aligned"], msg=sig_text)
            self.assertEqual(aln_conf["alignment_status"], "aligned_trigger", msg=sig_text)

        # 4) Untriggered 1-bar micro reverse-K (99 vs swing low 90 at price 100) goes to intraday_reverse_k_watch,
        #    NOT intraday_tactical_stop; daily_structural_stop extracts Daily ZD/ZG separately
        intra_micro_revk = {
            "timeframe": "30分钟",
            "last_close": 100.0,
            "latest_zhongshu": {"zd": 91.0, "zg": 95.0},
            "stroke_end_trigger": {
                "direction": "up",
                "trigger_kind": "break_prev_bar_low",
                "level": 99.0,
                "triggered": False,
            },
            "recent_strokes": [
                {"direction": "down", "start_price": 98.0, "end_price": 90.0, "low": 90.0, "high": 98.0, "is_confirmed": True},
                {"direction": "up", "start_price": 90.0, "end_price": 100.0, "low": 90.0, "high": 100.0, "is_confirmed": False},
            ],
            "signals": ["30分钟首中枢三买确认(回踩不破ZG=95.0+底分型)"],
        }
        aln_stop = assess_dual_timeframe_alignment(daily_cons_up, intra_micro_revk, desired_direction="long")
        self.assertEqual(aln_stop["intraday_tactical_stop"], 90.0)
        self.assertEqual(aln_stop["intraday_reverse_k_watch"], 99.0)
        self.assertEqual(aln_stop["daily_tactical_stop"], 92.0)
        self.assertEqual(aln_stop["daily_structural_stop"], 85.0)

        # Daily structural stop fallback when Zhongshu ZD is above ref_price (e.g. 1B bottom reversal)
        daily_no_prot_zs = {
            "timeframe": "日线",
            "last_close": 100.0,
            "latest_zhongshu": {"zd": 110.0, "zg": 120.0},
            "recent_strokes": [
                {"direction": "down", "start_price": 115.0, "end_price": 94.0, "low": 94.0, "high": 115.0, "is_confirmed": True},
                {"direction": "up", "start_price": 94.0, "end_price": 100.0, "low": 94.0, "high": 100.0, "is_confirmed": False},
            ],
            "signals": ["日线盘整背驰+底分型启动(一买/盘背·5类笔·area=0.4x)"],
        }
        aln_fb = assess_dual_timeframe_alignment(daily_no_prot_zs, intra_micro_revk, desired_direction="long")
        self.assertEqual(aln_fb["daily_structural_stop"], 94.0)

        # 5) Negative noise_reduction_pct is NOT clamped to -100.0:
        #    ref_price=100, daily_stop=95 (dist=5), intraday_stop=85 (dist=15 = 3x daily) -> -200.0%
        daily_tight = {
            "timeframe": "日线",
            "last_close": 100.0,
            "latest_zhongshu": {"zd": 90.0, "zg": 96.0},
            "recent_strokes": [{"direction": "up", "start_price": 95.0, "end_price": 100.0, "low": 95.0, "high": 100.0, "is_confirmed": False}],
            "signals": ["日线首中枢三买确认(回踩不破ZG=96.0+底分型)"],
        }
        intra_wide = {
            "timeframe": "30分钟",
            "last_close": 100.0,
            "latest_zhongshu": {"zd": 85.0, "zg": 92.0},
            "recent_strokes": [{"direction": "up", "start_price": 85.0, "end_price": 100.0, "low": 85.0, "high": 100.0, "is_confirmed": False}],
            "signals": ["30分钟首中枢三买确认(回踩不破ZG=92.0+底分型)"],
        }
        aln_neg = assess_dual_timeframe_alignment(daily_tight, intra_wide, desired_direction="long")
        self.assertEqual(aln_neg["daily_tactical_stop"], 95.0)
        self.assertEqual(aln_neg["intraday_tactical_stop"], 85.0)
        self.assertEqual(aln_neg["noise_reduction_pct"], -200.0)

    def test_codex_round2_neutral_extension_momentum_guard(self):
        # 1) Codex repro: daily signals contain "空头资金方向一致推进" while momentum_assessment regime is neutral_extension_down
        #    -> MUST downgrade to weak_momentum_short with confirmed=False, momentum_confirmed=False
        for mom_payload in (
            {"active_direction": "down", "regime": "neutral_extension_down"},
            {"active_direction": "down", "regime": "neutral_extension_down", "is_capital_consensus": False},
            {"active_direction": "down", "regime": "neutral_extension_down", "is_capital_consensus": True},
            {"active_direction": "down", "verdict": "neutral_extension_down", "is_capital_consensus": False},
            {"active_direction": "down", "regime": "single_stroke_no_comparison"},
            {"active_direction": "down", "regime": "weak_down_extension"},
        ):
            daily_neutral_down = {
                "timeframe": "日线",
                "last_close": 100.0,
                "price_vs_zhongshu": "inside_zs(50%)",
                "momentum_assessment": mom_payload,
                "signals": ["日线空头资金方向一致推进"],
            }
            ev = evaluate_signal_tier(daily_neutral_down, timeframe_label="日线")
            meta = ev["signal_meta"]
            self.assertEqual(meta["signal_type"], "weak_momentum_short", msg=mom_payload)
            self.assertEqual(meta["direction"], "short", msg=mom_payload)
            self.assertFalse(meta["confirmed"], msg=mom_payload)
            self.assertFalse(meta["momentum_confirmed"], msg=mom_payload)
            self.assertNotEqual(ev["tier"], "资金一致推进(勿逆势)", msg=mom_payload)

            # Dual alignment must NOT treat downgraded neutral extension as strong daily short bias
            intra_ok = {
                "timeframe": "30分钟",
                "last_close": 100.0,
                "price_vs_zhongshu": "inside_zs(50%)",
                "signals": ["30分钟中枢震荡中([ZD=98.0, ZG=102.0])"],
            }
            aln = assess_dual_timeframe_alignment(daily_neutral_down, intra_ok, desired_direction="long")
            self.assertEqual(aln["daily_bias"], "neutral", msg=mom_payload)
            self.assertEqual(aln["daily_signal_meta"]["signal_type"], "weak_momentum_short", msg=mom_payload)
            self.assertFalse(aln["daily_signal_meta"]["momentum_confirmed"], msg=mom_payload)

        # Symmetric bullish guard: neutral_extension_up + "多头资金方向一致推进" -> weak_momentum_long
        daily_neutral_up = {
            "timeframe": "日线",
            "last_close": 100.0,
            "price_vs_zhongshu": "inside_zs(50%)",
            "momentum_assessment": {"active_direction": "up", "regime": "neutral_extension_up"},
            "signals": ["日线多头资金方向一致推进"],
        }
        ev_up_weak = evaluate_signal_tier(daily_neutral_up, timeframe_label="日线")
        self.assertEqual(ev_up_weak["signal_meta"]["signal_type"], "weak_momentum_long")
        self.assertEqual(ev_up_weak["signal_meta"]["direction"], "long")
        self.assertFalse(ev_up_weak["signal_meta"]["confirmed"])
        self.assertFalse(ev_up_weak["signal_meta"]["momentum_confirmed"])

        # 2) Real capital consensus regimes -> momentum_confirmed=True and momentum_short / momentum_long
        for strong_down_mom in (
            {"active_direction": "down", "regime": "capital_consensus_down", "is_capital_consensus": True},
            {"regime": "capital_consensus_down"},
            {"regime": "strong_down_continuation"},
            {"active_direction": "down", "verdict": "capital_consensus_down", "is_capital_consensus": True},
        ):
            daily_strong_down = {
                "timeframe": "日线",
                "last_close": 90.0,
                "price_vs_zhongshu": "below_zd(-5.0%)",
                "momentum_assessment": strong_down_mom,
                "signals": ["日线空头资金方向一致推进"],
            }
            ev_sd = evaluate_signal_tier(daily_strong_down, timeframe_label="日线")
            self.assertEqual(ev_sd["signal_meta"]["signal_type"], "momentum_short", msg=strong_down_mom)
            self.assertEqual(ev_sd["signal_meta"]["direction"], "short", msg=strong_down_mom)
            self.assertFalse(ev_sd["signal_meta"]["confirmed"], msg=strong_down_mom)
            self.assertTrue(ev_sd["signal_meta"]["momentum_confirmed"], msg=strong_down_mom)

        for strong_up_mom in (
            {"active_direction": "up", "regime": "capital_consensus_up", "is_capital_consensus": True},
            {"regime": "strong_up_continuation"},
        ):
            daily_strong_up = {
                "timeframe": "日线",
                "last_close": 115.0,
                "price_vs_zhongshu": "above_zg(+5.0%)",
                "momentum_assessment": strong_up_mom,
                "signals": ["日线多头资金方向一致推进"],
            }
            ev_su = evaluate_signal_tier(daily_strong_up, timeframe_label="日线")
            self.assertEqual(ev_su["signal_meta"]["signal_type"], "momentum_long", msg=strong_up_mom)
            self.assertEqual(ev_su["signal_meta"]["direction"], "long", msg=strong_up_mom)
            self.assertFalse(ev_su["signal_meta"]["confirmed"], msg=strong_up_mom)
            self.assertTrue(ev_su["signal_meta"]["momentum_confirmed"], msg=strong_up_mom)

        # Insufficient data also exposes momentum_confirmed=False
        ev_err = evaluate_signal_tier({"error": "Insufficient bars"})
        self.assertFalse(ev_err["signal_meta"]["momentum_confirmed"])


if __name__ == "__main__":
    unittest.main()
