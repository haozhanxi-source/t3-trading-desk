#!/usr/bin/env python3
"""Chanlun (缠论) structural & momentum analysis engine.

Aligned with the 19-lesson practical Chanlun system + MACD confirmation:
1. K-line inclusion processing (K线包含处理)
2. Top/bottom fractal detection & confirmation strength (顶底分型及停顿确认)
3. Lesson 5 revised stroke construction (笔定义的修正: 严格分型 + 次级别上下上/下上下三段及急跌急拉连阴连阳成笔)
4. Lesson 10 strict 3-stroke core Zhongshu (笔中枢: 仅框最初前三笔重叠区域, 不无限拉长; 延伸/九段升级独立记录; 排除简单三笔单边走势)
5. Lesson 4/6 Daily Segments (日线线段辅助结构)
6. Lesson 9/14 Stroke Strength Classes 1-8 + MACD Divergence vs Non-Divergence Pullback (真背驰 vs 非背驰背离)
7. Lesson 8/11/13/15 Tactical Signals: Reverse-K Stroke End Trigger (破反向K线笔结束判定), 强弱关系(找大哥), 3出/4出/2+3买点, 首中枢三买限制
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class MergedBar:
    idx: int
    high: float
    low: float
    open: float
    close: float
    volume: float
    raw_start_idx: int
    raw_end_idx: int
    start_date: str
    end_date: str
    high_raw_idx: int
    high_date: str
    low_raw_idx: int
    low_date: str


@dataclass
class Fractal:
    kind: str  # "top" or "bottom"
    merged_idx: int
    high: float
    low: float
    price: float  # high for top, low for bottom
    raw_idx: int
    date: str
    confirmed_by_idx: int
    confirmed_date: str
    strength: str  # "strong", "standard", "weak"


@dataclass
class Stroke:
    direction: str  # "up" or "down"
    start_date: str
    end_date: str
    start_price: float
    end_price: float
    high: float
    low: float
    start_merged_idx: int
    end_merged_idx: int
    start_raw_idx: int
    end_raw_idx: int
    raw_bars: int
    pct_change: float
    macd_area: float
    macd_peak: float
    dif_extreme: float
    efficiency: float
    avg_vol_ratio: Optional[float]
    is_confirmed: bool
    strength_class: int = 4  # Lesson 9: 1 (strongest) .. 8 (weakest)
    strength_tier: str = "中"  # "强" (1-3), "中" (4-5), "弱" (6-8)


@dataclass
class Segment:
    seg_id: int
    direction: str  # "up" or "down"
    start_date: str
    end_date: str
    start_price: float
    end_price: float
    high: float
    low: float
    stroke_count: int
    is_confirmed: bool


@dataclass
class Zhongshu:
    zs_id: int
    direction: str  # "up", "down", or "neutral" (entering stroke direction)
    zd: float
    zg: float
    dd: float  # extreme low of the founding 3 strokes
    gg: float  # extreme high of the founding 3 strokes
    start_date: str
    end_date: str  # strictly end_date of the 3rd founding stroke (第10课: 只画前三笔重叠区域)
    stroke_count: int  # always 3 for the core Zhongshu box
    is_extended: bool  # whether subsequent strokes oscillated around [ZD, ZG]
    status: str  # "active", "broken_up", "broken_down"
    oscillation_strokes: int = 0  # count of overlapping oscillation strokes after the founding 3
    oscillation_end_date: Optional[str] = None
    oscillation_dd: Optional[float] = None
    oscillation_gg: Optional[float] = None
    is_nine_upgrade: bool = False  # 3 + oscillation_strokes >= 9 (九段升级提示)
    trend_zs_index: int = 1  # 1 = 首中枢 in current trend direction; >=2 = 第N个中枢
    relation_to_prev: str = "initial"  # initial | new_up | new_down | expansion_up | expansion_down | overlap
    extension_stage: str = "core"  # core (3 units) | extended (4-8 units) | upgraded_9 (>=9 units)
    nine_upgrade_date: Optional[str] = None  # end_date of the 9th unit when upgraded_9


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def _format_bar_timestamps(index, force_intraday: bool = False) -> list[str]:
    """Format bar timestamps:
    - tz-aware intraday: ISO 8601 with tz offset at minute precision (e.g. '2026-11-01T01:30-04:00')
    - naive intraday: 'YYYY-MM-DD HH:MM'
    - pure daily: 'YYYY-MM-DD'
    """
    if index is None or len(index) == 0:
        return []

    items = list(index)
    parsed: list[Optional[pd.Timestamp]] = []
    has_time = False
    seen_dates: set[str] = set()
    has_dup_dates = False

    for idx in items:
        ts: Optional[pd.Timestamp] = None
        if isinstance(idx, (pd.Timestamp, datetime)):
            ts = pd.Timestamp(idx)
        elif hasattr(idx, "year") and hasattr(idx, "month") and hasattr(idx, "day"):
            ts = pd.Timestamp(idx)
        else:
            try:
                cand = pd.Timestamp(idx)
                if pd.notna(cand):
                    ts = cand
            except Exception:
                ts = None

        parsed.append(ts)
        if ts is not None and pd.notna(ts):
            if ts.hour != 0 or ts.minute != 0 or ts.second != 0 or ts.microsecond != 0:
                has_time = True
            d_key = ts.strftime("%Y-%m-%d")
            if d_key in seen_dates:
                has_dup_dates = True
            else:
                seen_dates.add(d_key)

    is_intraday = bool(force_intraday or has_time or has_dup_dates)
    out: list[str] = []
    for raw_idx, ts in zip(items, parsed, strict=True):
        if ts is not None and pd.notna(ts):
            if is_intraday:
                if ts.tzinfo is not None:
                    out.append(ts.isoformat(timespec="minutes"))
                else:
                    out.append(ts.strftime("%Y-%m-%d %H:%M"))
            else:
                out.append(ts.strftime("%Y-%m-%d"))
        else:
            out.append(str(raw_idx))
    return out


def _ts_ge(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two formatted bar timestamp strings chronologically (handling ISO tz offsets)."""
    if not a or not b:
        return False
    if "T" in a and "T" in b:
        try:
            return datetime.fromisoformat(a) >= datetime.fromisoformat(b)
        except ValueError:
            pass
    return a >= b


def process_inclusion(df: pd.DataFrame) -> list[MergedBar]:
    """Perform standard Chanlun K-line inclusion processing from left to right."""
    if df.empty:
        return []

    highs = df["High"].astype(float).values
    lows = df["Low"].astype(float).values
    opens = df["Open"].astype(float).values if "Open" in df else df["Close"].astype(float).values
    closes = df["Close"].astype(float).values
    vols = df["Volume"].astype(float).values if "Volume" in df else np.zeros(len(df))
    dates = _format_bar_timestamps(df.index)

    merged: list[MergedBar] = []
    direction = 1

    for i in range(len(df)):
        h, l, o, c, v, d = float(highs[i]), float(lows[i]), float(opens[i]), float(closes[i]), float(vols[i]), dates[i]
        if not merged:
            merged.append(
                MergedBar(
                    idx=0,
                    high=h,
                    low=l,
                    open=o,
                    close=c,
                    volume=v,
                    raw_start_idx=i,
                    raw_end_idx=i,
                    start_date=d,
                    end_date=d,
                    high_raw_idx=i,
                    high_date=d,
                    low_raw_idx=i,
                    low_date=d,
                )
            )
            continue

        cur = merged[-1]
        is_included = (cur.high >= h and cur.low <= l) or (h >= cur.high and l <= cur.low)

        if is_included:
            if len(merged) >= 2:
                prev = merged[-2]
                if cur.high > prev.high and cur.low > prev.low:
                    direction = 1
                elif cur.high < prev.high and cur.low < prev.low:
                    direction = -1

            if direction == 1:
                if h >= cur.high:
                    cur.high = h
                    cur.high_raw_idx = i
                    cur.high_date = d
                if l >= cur.low:
                    cur.low = l
                    cur.low_raw_idx = i
                    cur.low_date = d
            else:
                if h <= cur.high:
                    cur.high = h
                    cur.high_raw_idx = i
                    cur.high_date = d
                if l <= cur.low:
                    cur.low = l
                    cur.low_raw_idx = i
                    cur.low_date = d

            cur.close = c
            cur.volume += v
            cur.raw_end_idx = i
            cur.end_date = d
        else:
            if h > cur.high and l > cur.low:
                direction = 1
            elif h < cur.high and l < cur.low:
                direction = -1

            merged.append(
                MergedBar(
                    idx=len(merged),
                    high=h,
                    low=l,
                    open=o,
                    close=c,
                    volume=v,
                    raw_start_idx=i,
                    raw_end_idx=i,
                    start_date=d,
                    end_date=d,
                    high_raw_idx=i,
                    high_date=d,
                    low_raw_idx=i,
                    low_date=d,
                )
            )

    return merged


def detect_fractals(merged: list[MergedBar]) -> list[Fractal]:
    """Detect all top and bottom fractals on inclusion-processed bars."""
    fractals: list[Fractal] = []
    n = len(merged)
    if n < 3:
        return fractals

    for i in range(1, n - 1):
        left, mid, right = merged[i - 1], merged[i], merged[i + 1]
        if mid.high > left.high and mid.high > right.high and mid.low > left.low and mid.low > right.low:
            if right.close < mid.low:
                strength = "strong"
            elif right.close < right.open or right.close < mid.close:
                strength = "standard"
            else:
                strength = "weak"
            fractals.append(
                Fractal(
                    kind="top",
                    merged_idx=i,
                    high=mid.high,
                    low=mid.low,
                    price=mid.high,
                    raw_idx=mid.high_raw_idx,
                    date=mid.high_date,
                    confirmed_by_idx=i + 1,
                    confirmed_date=right.end_date,
                    strength=strength,
                )
            )
        elif mid.low < left.low and mid.low < right.low and mid.high < left.high and mid.high < right.high:
            if right.close > mid.high:
                strength = "strong"
            elif right.close > right.open or right.close > mid.close:
                strength = "standard"
            else:
                strength = "weak"
            fractals.append(
                Fractal(
                    kind="bottom",
                    merged_idx=i,
                    high=mid.high,
                    low=mid.low,
                    price=mid.low,
                    raw_idx=mid.low_raw_idx,
                    date=mid.low_date,
                    confirmed_by_idx=i + 1,
                    confirmed_date=right.end_date,
                    strength=strength,
                )
            )
    return fractals


def _has_lesson5_substructure(
    raw_df: Optional[pd.DataFrame],
    last: Fractal,
    cand: Fractal,
) -> bool:
    """Lesson 5 stroke revision (笔定义的修正):
    Accepts 2 <= merged_gap < 4 when the raw bars between last and cand show either:
    (a) a 3-segment sub-structure (上-下-上 or 下-上-下, excluding fake yin/yang), or
    (b) a clear >=3-bar directional impulse (>=2 consecutive directional bars with >=3% swing).
    """
    lo_r = min(last.raw_idx, cand.raw_idx)
    hi_r = max(last.raw_idx, cand.raw_idx)
    if hi_r - lo_r + 1 < 3:
        return False

    if raw_df is None or hi_r >= len(raw_df):
        return False

    sub = raw_df.iloc[lo_r : hi_r + 1]
    opens = sub["Open"].astype(float).values
    closes = sub["Close"].astype(float).values
    highs = sub["High"].astype(float).values
    lows = sub["Low"].astype(float).values

    if cand.kind == "top":
        # Upward stroke: look for real bearish pullback bar (真阴线, strictly close < open AND close < prev_close)
        # followed by a bar making a higher high toward cand.high
        for k in range(1, len(sub) - 1):
            is_real_yin = (closes[k] < opens[k]) and (closes[k] < closes[k - 1])
            if is_real_yin and np.max(highs[k + 1 :]) > highs[k - 1]:
                return True
        # Or sharp consecutive rally >= 3% with >= 2 strictly consecutive up bars
        consec, max_consec = 0, 0
        for k in range(1, len(sub)):
            if closes[k] > opens[k] and closes[k] > closes[k - 1]:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0
        pct = (cand.price / last.price - 1.0) if last.price else 0.0
        if max_consec >= 2 and pct >= 0.03:
            return True
    else:
        # Downward stroke: look for real bullish rebound bar (真阳线, strictly close > open AND close > prev_close)
        # followed by a bar making a lower low toward cand.low
        for k in range(1, len(sub) - 1):
            is_real_yang = (closes[k] > opens[k]) and (closes[k] > closes[k - 1])
            if is_real_yang and np.min(lows[k + 1 :]) < lows[k - 1]:
                return True
        # Or sharp consecutive selloff <= -3% with >= 2 strictly consecutive down bars
        consec, max_consec = 0, 0
        for k in range(1, len(sub)):
            if closes[k] < opens[k] and closes[k] < closes[k - 1]:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0
        pct = (1.0 - cand.price / last.price) if last.price else 0.0
        if max_consec >= 2 and pct >= 0.03:
            return True

    return False


def build_pivots(
    merged: list[MergedBar],
    fractals: list[Fractal],
    raw_df: Optional[pd.DataFrame] = None,
) -> list[Fractal]:
    """Select alternating Chanlun pivots (Lesson 2 strict rule + Lesson 5连阴连阳/上下上 revision)."""
    if not fractals:
        return []

    pivots: list[Fractal] = []
    for f in fractals:
        if not pivots:
            pivots.append(f)
            continue

        last = pivots[-1]
        if f.kind == last.kind:
            if f.kind == "top" and f.high >= last.high:
                pivots[-1] = f
            elif f.kind == "bottom" and f.low <= last.low:
                pivots[-1] = f
            continue

        gap = f.merged_idx - last.merged_idx
        if gap < 2:
            continue

        lo_idx, hi_idx = last.merged_idx, f.merged_idx
        seg = merged[lo_idx : hi_idx + 1]
        seg_max_high = max(b.high for b in seg)
        seg_min_low = min(b.low for b in seg)

        valid_extreme = False
        if last.kind == "bottom" and f.kind == "top":
            valid_extreme = f.high > last.high and f.low > last.low and f.high >= seg_max_high and last.low <= seg_min_low
        elif last.kind == "top" and f.kind == "bottom":
            valid_extreme = f.low < last.low and f.high < last.high and f.low <= seg_min_low and last.high >= seg_max_high

        if not valid_extreme:
            continue

        if gap >= 4 or _has_lesson5_substructure(raw_df, last, f):
            pivots.append(f)

    return pivots


def _classify_stroke_strength(
    df: Optional[pd.DataFrame],
    direction: str,
    lo_r: int,
    hi_r: int,
    pct_change: float,
    raw_bars: int,
    atr_pct_series: Optional[pd.Series],
) -> tuple[int, str]:
    """Lesson 9: classify stroke itself into 1..8 energy grade.
    1-3 = 强, 4-5 = 中, 6-8 = 弱.
    Dimensions: (1) directional K-line continuity, (2) directional bar size, (3) counter bar size.
    """
    if df is None or hi_r >= len(df) or lo_r < 0:
        return 4, "中"

    sub = df.iloc[lo_r : hi_r + 1]
    opens = sub["Open"].astype(float).values
    closes = sub["Close"].astype(float).values
    highs = sub["High"].astype(float).values
    lows = sub["Low"].astype(float).values

    # Compare against local 20-day ATR% if available
    ref_atr_pct = 2.0
    if atr_pct_series is not None and len(atr_pct_series) > hi_r:
        val = atr_pct_series.iloc[hi_r]
        if pd.notna(val) and float(val) > 0:
            ref_atr_pct = float(val)

    dir_bodies: list[float] = []
    cnt_bodies: list[float] = []
    dir_count = 0
    max_cnt_run = 0
    cur_cnt_run = 0

    for k in range(len(sub)):
        o, c, h, l = opens[k], closes[k], highs[k], lows[k]
        prev_c = closes[k - 1] if k > 0 else o
        body_pct = abs(c - o) / o * 100.0 if o > 0 else 0.0
        range_pct = (h - l) / l * 100.0 if l > 0 else 0.0
        mag = max(body_pct, range_pct * 0.6)

        if direction == "up":
            is_dir = c >= o or (k > 0 and c > prev_c and h >= highs[k - 1])
        else:
            is_dir = c <= o or (k > 0 and c < prev_c and l <= lows[k - 1])

        if is_dir:
            dir_count += 1
            dir_bodies.append(mag)
            cur_cnt_run = 0
        else:
            cnt_bodies.append(mag)
            cur_cnt_run += 1
            max_cnt_run = max(max_cnt_run, cur_cnt_run)

    dir_ratio = dir_count / max(1, len(sub))
    continuity_good = dir_ratio >= 0.62 and max_cnt_run <= (1 if raw_bars <= 6 else 2)

    avg_dir_mag = float(np.mean(dir_bodies)) if dir_bodies else 0.0
    eff_per_bar = abs(pct_change) / max(1, raw_bars)
    body_large = (avg_dir_mag >= 0.80 * ref_atr_pct) or (eff_per_bar >= 1.10 * ref_atr_pct)

    max_cnt_mag = float(np.max(cnt_bodies)) if cnt_bodies else 0.0
    counter_small = (not cnt_bodies) or (max_cnt_mag <= 0.78 * max(avg_dir_mag, 0.01))

    table = {
        (True, True, True): 1,
        (True, True, False): 2,
        (True, False, True): 3,
        (True, False, False): 4,
        (False, True, True): 5,
        (False, True, False): 6,
        (False, False, True): 7,
        (False, False, False): 8,
    }
    grade = table[(continuity_good, body_large, counter_small)]
    tier = "强" if grade <= 3 else ("中" if grade <= 5 else "弱")
    return grade, tier


def _make_stroke(
    direction: str,
    start_date: str,
    end_date: str,
    start_price: float,
    end_price: float,
    start_merged_idx: int,
    end_merged_idx: int,
    start_raw_idx: int,
    end_raw_idx: int,
    dif: pd.Series,
    hist: pd.Series,
    vol_ratio_series: Optional[pd.Series],
    is_confirmed: bool,
    df: Optional[pd.DataFrame] = None,
    atr_pct_series: Optional[pd.Series] = None,
) -> Stroke:
    lo_r = min(start_raw_idx, end_raw_idx)
    hi_r = max(start_raw_idx, end_raw_idx)
    raw_bars = max(1, hi_r - lo_r + 1)

    hist_slice = hist.iloc[lo_r : hi_r + 1].astype(float)
    dif_slice = dif.iloc[lo_r : hi_r + 1].astype(float)

    if direction == "up":
        pos_hist = hist_slice.clip(lower=0)
        macd_area = float(pos_hist.sum())
        macd_peak = float(pos_hist.max()) if len(pos_hist) else 0.0
        dif_extreme = float(dif_slice.max()) if len(dif_slice) else 0.0
        high_p = max(start_price, end_price)
        low_p = min(start_price, end_price)
    else:
        neg_hist = (-hist_slice).clip(lower=0)
        macd_area = float(neg_hist.sum())
        macd_peak = float(neg_hist.max()) if len(neg_hist) else 0.0
        dif_extreme = float(dif_slice.min()) if len(dif_slice) else 0.0
        high_p = max(start_price, end_price)
        low_p = min(start_price, end_price)

    pct_change = ((end_price / start_price) - 1.0) * 100.0 if start_price else 0.0
    efficiency = abs(pct_change) / raw_bars

    avg_vol = None
    if vol_ratio_series is not None and len(vol_ratio_series) > hi_r:
        vr_slice = vol_ratio_series.iloc[lo_r : hi_r + 1].dropna()
        if len(vr_slice) > 0:
            avg_vol = round(float(vr_slice.mean()), 2)

    grade, tier = _classify_stroke_strength(
        df=df,
        direction=direction,
        lo_r=lo_r,
        hi_r=hi_r,
        pct_change=pct_change,
        raw_bars=raw_bars,
        atr_pct_series=atr_pct_series,
    )

    return Stroke(
        direction=direction,
        start_date=start_date,
        end_date=end_date,
        start_price=round(start_price, 4),
        end_price=round(end_price, 4),
        high=round(high_p, 4),
        low=round(low_p, 4),
        start_merged_idx=start_merged_idx,
        end_merged_idx=end_merged_idx,
        start_raw_idx=start_raw_idx,
        end_raw_idx=end_raw_idx,
        raw_bars=raw_bars,
        pct_change=round(pct_change, 2),
        macd_area=round(macd_area, 4),
        macd_peak=round(macd_peak, 4),
        dif_extreme=round(dif_extreme, 4),
        efficiency=round(efficiency, 3),
        avg_vol_ratio=avg_vol,
        is_confirmed=is_confirmed,
        strength_class=grade,
        strength_tier=tier,
    )


def build_strokes(
    df: pd.DataFrame,
    merged: list[MergedBar],
    pivots: list[Fractal],
    dif: pd.Series,
    hist: pd.Series,
) -> list[Stroke]:
    """Construct confirmed strokes plus live extending stroke if applicable."""
    if not pivots or len(merged) < 2:
        return []

    vol_ratio_series = None
    if "Volume" in df:
        vol = df["Volume"].astype(float)
        vol_ma20 = vol.rolling(20, min_periods=5).mean()
        vol_ratio_series = vol / vol_ma20.replace(0, np.nan)

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20, min_periods=5).mean()
    atr_pct_series = (atr20 / close.replace(0, np.nan)) * 100.0

    strokes: list[Stroke] = []
    for idx in range(len(pivots) - 1):
        p1, p2 = pivots[idx], pivots[idx + 1]
        direction = "up" if p1.kind == "bottom" else "down"
        strokes.append(
            _make_stroke(
                direction=direction,
                start_date=p1.date,
                end_date=p2.date,
                start_price=p1.price,
                end_price=p2.price,
                start_merged_idx=p1.merged_idx,
                end_merged_idx=p2.merged_idx,
                start_raw_idx=p1.raw_idx,
                end_raw_idx=p2.raw_idx,
                dif=dif,
                hist=hist,
                vol_ratio_series=vol_ratio_series,
                is_confirmed=True,
                df=df,
                atr_pct_series=atr_pct_series,
            )
        )

    last_p = pivots[-1]
    tail = merged[last_p.merged_idx :]
    if len(tail) >= 2:
        max_bar = max(tail[1:], key=lambda b: b.high)
        min_bar = min(tail[1:], key=lambda b: b.low)

        if last_p.kind == "top":
            if max_bar.high > last_p.high and strokes:
                prev_p = pivots[-2]
                strokes[-1] = _make_stroke(
                    direction="up",
                    start_date=prev_p.date,
                    end_date=max_bar.high_date,
                    start_price=prev_p.price,
                    end_price=max_bar.high,
                    start_merged_idx=prev_p.merged_idx,
                    end_merged_idx=max_bar.idx,
                    start_raw_idx=prev_p.raw_idx,
                    end_raw_idx=max_bar.high_raw_idx,
                    dif=dif,
                    hist=hist,
                    vol_ratio_series=vol_ratio_series,
                    is_confirmed=False,
                    df=df,
                    atr_pct_series=atr_pct_series,
                )
            elif min_bar.low < last_p.low or len(tail) >= 3:
                target_bar = min_bar if min_bar.low < last_p.low else tail[-1]
                strokes.append(
                    _make_stroke(
                        direction="down",
                        start_date=last_p.date,
                        end_date=target_bar.low_date,
                        start_price=last_p.price,
                        end_price=target_bar.low,
                        start_merged_idx=last_p.merged_idx,
                        end_merged_idx=target_bar.idx,
                        start_raw_idx=last_p.raw_idx,
                        end_raw_idx=target_bar.low_raw_idx,
                        dif=dif,
                        hist=hist,
                        vol_ratio_series=vol_ratio_series,
                        is_confirmed=False,
                        df=df,
                        atr_pct_series=atr_pct_series,
                    )
                )
        else:
            if min_bar.low < last_p.low and strokes:
                prev_p = pivots[-2]
                strokes[-1] = _make_stroke(
                    direction="down",
                    start_date=prev_p.date,
                    end_date=min_bar.low_date,
                    start_price=prev_p.price,
                    end_price=min_bar.low,
                    start_merged_idx=prev_p.merged_idx,
                    end_merged_idx=min_bar.idx,
                    start_raw_idx=prev_p.raw_idx,
                    end_raw_idx=min_bar.low_raw_idx,
                    dif=dif,
                    hist=hist,
                    vol_ratio_series=vol_ratio_series,
                    is_confirmed=False,
                    df=df,
                    atr_pct_series=atr_pct_series,
                )
            elif max_bar.high > last_p.high or len(tail) >= 3:
                target_bar = max_bar if max_bar.high > last_p.high else tail[-1]
                strokes.append(
                    _make_stroke(
                        direction="up",
                        start_date=last_p.date,
                        end_date=target_bar.high_date,
                        start_price=last_p.price,
                        end_price=target_bar.high,
                        start_merged_idx=last_p.merged_idx,
                        end_merged_idx=target_bar.idx,
                        start_raw_idx=last_p.raw_idx,
                        end_raw_idx=target_bar.high_raw_idx,
                        dif=dif,
                        hist=hist,
                        vol_ratio_series=vol_ratio_series,
                        is_confirmed=False,
                        df=df,
                        atr_pct_series=atr_pct_series,
                    )
                )

    return strokes


def build_segments(strokes: list[Stroke]) -> list[Segment]:
    """Lesson 4 & 6: Build strictly alternating, gapless Daily Segments (线段) from strokes.

    Invariants:
    - An 'up' segment ALWAYS starts on an 'up' stroke and ends on an 'up' stroke (its highest peak).
    - A 'down' segment ALWAYS starts on a 'down' stroke and ends on a 'down' stroke (its lowest trough).
    - Consecutive segments strictly alternate direction with zero skipped strokes.
    - Termination requires a 3-stroke counter-move breaking prior stroke extreme (Lesson 4/6).
    """
    n = len(strokes)
    if n < 3:
        return []

    segments: list[Segment] = []
    seg_id = 1
    i = 0

    # Locate initial segment start where 3 strokes establish directional break
    while i <= n - 3:
        s0, s1, s2 = strokes[i], strokes[i + 1], strokes[i + 2]
        if s0.direction == "up" and s2.high > s0.high:
            break
        if s0.direction == "down" and s2.low < s0.low:
            break
        i += 1
    if i > n - 3:
        return []

    while i <= n - 3:
        seg_dir = strokes[i].direction
        seg_start = strokes[i].start_date
        seg_start_p = strokes[i].start_price

        if seg_dir == "up":
            peak_idx = i
            peak_val = strokes[i].high
            seg_low = strokes[i].low
            break_idx: Optional[int] = None

            j = i + 1
            while j < n:
                sj = strokes[j]
                if sj.direction == "up":
                    if sj.high >= peak_val:
                        peak_val = sj.high
                        peak_idx = j
                else:
                    seg_low = min(seg_low, sj.low)
                    # Only terminate once up segment has >= 3 strokes (peak_idx >= i + 2)
                    # and a 3-stroke CONFIRMED downward structure breaks after peak_idx
                    if peak_idx >= i + 2 and j > peak_idx and j + 2 < n:
                        b1, b2, b3 = strokes[j], strokes[j + 1], strokes[j + 2]
                        if (
                            b1.is_confirmed and b2.is_confirmed and b3.is_confirmed
                            and b2.high < strokes[j - 1].high and b3.low < b1.low
                        ):
                            break_idx = peak_idx
                            break
                j += 1

            end_idx = break_idx if break_idx is not None else peak_idx
            # Ensure end_idx is an up stroke >= i + 2 if 3 strokes exist, else i
            if end_idx < i:
                end_idx = i
            seg_slice = strokes[i : end_idx + 1]
            segments.append(
                Segment(
                    seg_id=seg_id,
                    direction="up",
                    start_date=seg_start,
                    end_date=strokes[end_idx].end_date,
                    start_price=round(seg_start_p, 4),
                    end_price=round(strokes[end_idx].end_price, 4),
                    high=round(max(s.high for s in seg_slice), 4),
                    low=round(min(s.low for s in seg_slice), 4),
                    stroke_count=end_idx - i + 1,
                    is_confirmed=(break_idx is not None),
                )
            )
            seg_id += 1
            if break_idx is None:
                break
            i = end_idx + 1
        else:
            trough_idx = i
            trough_val = strokes[i].low
            seg_high = strokes[i].high
            break_idx = None

            j = i + 1
            while j < n:
                sj = strokes[j]
                if sj.direction == "down":
                    if sj.low <= trough_val:
                        trough_val = sj.low
                        trough_idx = j
                else:
                    seg_high = max(seg_high, sj.high)
                    if trough_idx >= i + 2 and j > trough_idx and j + 2 < n:
                        b1, b2, b3 = strokes[j], strokes[j + 1], strokes[j + 2]
                        if (
                            b1.is_confirmed and b2.is_confirmed and b3.is_confirmed
                            and b2.low > strokes[j - 1].low and b3.high > b1.high
                        ):
                            break_idx = trough_idx
                            break
                j += 1

            end_idx = break_idx if break_idx is not None else trough_idx
            if end_idx < i:
                end_idx = i
            seg_slice = strokes[i : end_idx + 1]
            segments.append(
                Segment(
                    seg_id=seg_id,
                    direction="down",
                    start_date=seg_start,
                    end_date=strokes[end_idx].end_date,
                    start_price=round(seg_start_p, 4),
                    end_price=round(strokes[end_idx].end_price, 4),
                    high=round(max(s.high for s in seg_slice), 4),
                    low=round(min(s.low for s in seg_slice), 4),
                    stroke_count=end_idx - i + 1,
                    is_confirmed=(break_idx is not None),
                )
            )
            seg_id += 1
            if break_idx is None:
                break
            i = end_idx + 1

    return segments


def _is_valid_entering_unit(enter: Stroke | Segment, zd: float, zg: float) -> bool:
    """Project heuristic verifying the entering unit genuinely enters [ZD, ZG] from its side.

    Note: The original Chanlun texts define entering direction without a fixed midline threshold;
    this midline-penetration heuristic (`mid = (zd + zg) / 2`) prevents unrelated opposite-side
    swings from swallowing a major trend leg as `b1` of an inverted Zhongshu, while still
    accepting minor reversal overshoots.
    """
    mid = (zd + zg) / 2.0
    if enter.direction == "up":
        return enter.low < mid and enter.high >= mid
    if enter.direction == "down":
        return enter.high > mid and enter.low <= mid
    return False


def _effective_extremes(z: Zhongshu) -> tuple[float, float]:
    """Return effective [DD, GG] incorporating confirmed extension oscillations."""
    eff_dd = z.oscillation_dd if z.oscillation_dd is not None else z.dd
    eff_gg = z.oscillation_gg if z.oscillation_gg is not None else z.gg
    return eff_dd, eff_gg


def _classify_zhongshu_relation(
    prev_z: Zhongshu,
    curr_zd: float,
    curr_zg: float,
    curr_eff_dd: float,
    curr_eff_gg: float,
) -> str:
    """Classify strict Chanlun relation between two consecutive same-level Zhongshus.

    - new_up / new_down (中枢新生 / 趋势): [ZD, ZG] disjoint AND effective [DD, GG] disjoint.
    - expansion_up / expansion_down (中枢扩张 / 级别升级): [ZD, ZG] disjoint, effective [DD, GG] overlap.
    - overlap (中枢扩展 / 宽幅震荡): [ZD, ZG] overlap.
    """
    prev_dd, prev_gg = _effective_extremes(prev_z)
    if curr_zd > prev_z.zg:
        return "new_up" if curr_eff_dd > prev_gg else "expansion_up"
    if curr_zg < prev_z.zd:
        return "new_down" if curr_eff_gg < prev_dd else "expansion_down"
    return "overlap"


def _scan_zhongshu_extension(
    units: Sequence[Stroke | Segment],
    after_core_idx: int,
    zd: float,
    zg: float,
    dd: float,
    gg: float,
) -> tuple[int, str, int, Optional[str], Optional[float], Optional[float], str, Optional[str]]:
    """Scan confirmed units after the founding 3-unit core [ZD, ZG] for genuine Zhongshu extension.

    Chanlun distinction between Zhongshu extension (中枢延伸) and leaving unit (离开段):
    - Collect contiguous confirmed units overlapping [ZD, ZG] until the first disjoint confirmed unit
      (3rd buy: low > ZG -> broken_up; 3rd sell: high < ZD -> broken_down) or an unconfirmed/end unit.
    - When broken_up / broken_down is confirmed at u[k], u[k-1] (if after core) is the confirmed
      leaving unit (离开段) and also serves as the entering unit for the next Zhongshu at u[k];
      it must NOT be swallowed into extension count or stretch oscillation_end_date.
    - When still active (no confirmed 3B/3S yet), if the last confirmed overlapping unit ended
      strictly outside [ZD, ZG] (end_price > ZG or end_price < ZD), it is a live candidate leaving
      unit awaiting pullback confirmation and is excluded from confirmed extension until a subsequent
      confirmed unit pulls back into [ZD, ZG].
    """
    n = len(units)
    j = after_core_idx
    status = "active"
    overlapping: list[Stroke | Segment] = []

    while j < n:
        uj = units[j]
        if not uj.is_confirmed:
            break
        # Lesson 20 strict 3rd-buy/3rd-sell boundary rule:
        # - Upward breakout confirmed if next unit stays strictly above ZG OR is a downward retest
        #   touching ZG without breaking below it (low == ZG, high > ZG).
        # - Downward breakdown confirmed if next unit stays strictly below ZD OR is an upward retest
        #   touching ZD without breaking above it (high == ZD, low < ZD).
        is_3b_break = uj.low > zg or (uj.direction == "down" and uj.low == zg and uj.high > zg)
        is_3s_break = uj.high < zd or (uj.direction == "up" and uj.high == zd and uj.low < zd)
        if is_3b_break:
            status = "broken_up"
            break
        if is_3s_break:
            status = "broken_down"
            break
        overlapping.append(uj)
        j += 1

    if overlapping:
        if status in ("broken_up", "broken_down"):
            overlapping.pop()
        else:
            last_u = overlapping[-1]
            if last_u.end_price > zg or last_u.end_price < zd:
                overlapping.pop()

    osc_count = len(overlapping)
    if osc_count == 0:
        return j, status, 0, None, None, None, "core", None

    osc_end_date = overlapping[-1].end_date
    osc_dd = min(dd, *(u.low for u in overlapping))
    osc_gg = max(gg, *(u.high for u in overlapping))
    if osc_count >= 6:
        ext_stage = "upgraded_9"
        nine_date: Optional[str] = overlapping[5].end_date
    else:
        ext_stage = "extended"
        nine_date = None
    return j, status, osc_count, osc_end_date, round(osc_dd, 6), round(osc_gg, 6), ext_stage, nine_date


def _detect_unit_zhongshus(units: Sequence[Stroke | Segment]) -> list[Zhongshu]:
    """Unified strict Lesson 10 Zhongshu detector for Strokes or Segments.

    1. Requires a geometrically valid entering unit (`units[i - 1]`) penetrating the core midline.
    2. Core geometry [ZD, ZG], [DD, GG], [start_date, end_date] is strictly the founding 3 units.
    3. Records genuine extension (excluding leaving units), 9-unit upgrade milestone, and strict
       evolution relation (`new_up`/`new_down` vs `expansion_up`/`expansion_down` vs `overlap`).
    """
    zhongshus: list[Zhongshu] = []
    n = len(units)
    if n < 4:
        return zhongshus

    i = 1
    zs_id = 1
    last_trend_dir: Optional[str] = None
    while i <= n - 3:
        b1, b2, b3 = units[i], units[i + 1], units[i + 2]
        if not (b1.is_confirmed and b2.is_confirmed and b3.is_confirmed):
            break
        zg = min(b1.high, b2.high, b3.high)
        zd = max(b1.low, b2.low, b3.low)

        if zg <= zd:
            i += 1
            continue

        enter = units[i - 1]
        if not _is_valid_entering_unit(enter, zd, zg):
            i += 1
            continue

        enter_dir = enter.direction
        gg = max(b1.high, b2.high, b3.high)
        dd = min(b1.low, b2.low, b3.low)
        start_date = b1.start_date
        end_date = b3.end_date

        j, status, osc_count, osc_end_date, osc_dd, osc_gg, ext_stage, nine_date = _scan_zhongshu_extension(
            units, i + 3, zd, zg, dd, gg
        )

        # Normalize stored precision once (6 dp preserves FX sub-pip separation) before classifying relation
        zd_r = round(zd, 6)
        zg_r = round(zg, 6)
        dd_r = round(dd, 6)
        gg_r = round(gg, 6)
        curr_eff_dd = osc_dd if osc_dd is not None else dd_r
        curr_eff_gg = osc_gg if osc_gg is not None else gg_r

        trend_idx = 1
        relation = "initial"
        if zhongshus:
            prev_z = zhongshus[-1]
            relation = _classify_zhongshu_relation(prev_z, zd_r, zg_r, curr_eff_dd, curr_eff_gg)
            if relation == "new_up":
                trend_idx = (prev_z.trend_zs_index + 1) if last_trend_dir == "up" else 1
                last_trend_dir = "up"
            elif relation == "new_down":
                trend_idx = (prev_z.trend_zs_index + 1) if last_trend_dir == "down" else 1
                last_trend_dir = "down"
            elif relation == "expansion_up":
                trend_idx = 1
                last_trend_dir = "up"
            elif relation == "expansion_down":
                trend_idx = 1
                last_trend_dir = "down"
            else:
                trend_idx = 1
                last_trend_dir = enter_dir if enter_dir in ("up", "down") else None
        else:
            last_trend_dir = enter_dir if enter_dir in ("up", "down") else None

        zhongshus.append(
            Zhongshu(
                zs_id=zs_id,
                direction=enter_dir,
                zd=zd_r,
                zg=zg_r,
                dd=dd_r,
                gg=gg_r,
                start_date=start_date,
                end_date=end_date,
                stroke_count=3,
                is_extended=osc_count > 0,
                status=status,
                oscillation_strokes=osc_count,
                oscillation_end_date=osc_end_date,
                oscillation_dd=osc_dd,
                oscillation_gg=osc_gg,
                is_nine_upgrade=(3 + osc_count) >= 9,
                trend_zs_index=trend_idx,
                relation_to_prev=relation,
                extension_stage=ext_stage,
                nine_upgrade_date=nine_date,
            )
        )
        zs_id += 1
        i = j

    return zhongshus


def detect_zhongshus(strokes: list[Stroke], last_close: float) -> list[Zhongshu]:
    """Lesson 10 strict stroke-based Zhongshu (严格笔中枢)."""
    _ = last_close
    return _detect_unit_zhongshus(strokes)


def detect_segment_zhongshus(segments: list[Segment]) -> list[Zhongshu]:
    """Detect Segment-level Zhongshus (一级线段中枢 / 段中枢)."""
    return _detect_unit_zhongshus(segments)


def select_active_zhongshus(
    stroke_zss: list[Zhongshu],
    segment_zss: list[Zhongshu],
    strokes: list[Stroke],
    timeframe_label: str = "日线",
) -> tuple[str, str, list[Zhongshu]]:
    """Switch dynamically from Stroke Zhongshu to Segment Zhongshu when wide oscillation upgrades
    the structure (9+ strokes around the old box or >=6 strokes stale without a fresh stroke box)
    AND the latest Segment Zhongshu genuinely overlaps the active Stroke Zhongshu's price range.
    """
    tf = timeframe_label or "日线"
    if not stroke_zss and segment_zss:
        return "segment", f"{tf}一级线段中枢(段中枢)", segment_zss
    if not stroke_zss:
        return "stroke", f"{tf}笔中枢", []

    latest_st = stroke_zss[-1]
    strokes_since_box = sum(1 for s in strokes if s.is_confirmed and _ts_ge(s.start_date, latest_st.end_date))

    # Upgrade trigger: 9+ strokes around old box OR >=6 strokes elapsed since core box ended
    needs_upgrade = latest_st.is_nine_upgrade or strokes_since_box >= 6
    if needs_upgrade and segment_zss:
        latest_seg = segment_zss[-1]
        seg_reach = latest_seg.oscillation_end_date or latest_seg.end_date
        # Segment Zhongshu must cover at least the 9-stroke upgrade date / active extension horizon
        st_upgrade_anchor = (
            latest_st.nine_upgrade_date
            or latest_st.oscillation_end_date
            or latest_st.end_date
        )
        seg_dd, seg_gg = _effective_extremes(latest_seg)
        st_dd, st_gg = _effective_extremes(latest_st)
        price_overlaps = seg_dd <= st_gg and seg_gg >= st_dd
        if _ts_ge(seg_reach, st_upgrade_anchor) and price_overlaps:
            reason = "九段升级" if latest_st.is_nine_upgrade else "震荡拓宽升级"
            return "segment", f"{tf}一级线段中枢({reason})", segment_zss

    return "stroke", f"{tf}笔中枢", stroke_zss


def detect_stroke_end_trigger(df: Optional[pd.DataFrame], strokes: list[Stroke]) -> Optional[dict]:
    """Lesson 11: Reverse K-line break trigger for current stroke termination (笔结束判定)."""
    if df is None or df.empty or not strokes:
        return None

    curr = strokes[-1]
    lo_r = min(curr.start_raw_idx, curr.end_raw_idx)
    hi_r = max(curr.start_raw_idx, curr.end_raw_idx)
    if hi_r >= len(df):
        return None

    sub = df.iloc[lo_r : hi_r + 1]
    opens = sub["Open"].astype(float).values
    closes = sub["Close"].astype(float).values
    highs = sub["High"].astype(float).values
    lows = sub["Low"].astype(float).values
    all_dates = _format_bar_timestamps(df.index)
    dates = all_dates[lo_r : hi_r + 1]

    last_close = float(df["Close"].iloc[-1])
    last_high = float(df["High"].iloc[-1])
    last_low = float(df["Low"].iloc[-1])

    post_high = float(df["High"].iloc[hi_r + 1 :].max()) if hi_r < len(df) - 1 else None
    post_low = float(df["Low"].iloc[hi_r + 1 :].min()) if hi_r < len(df) - 1 else None
    post_close_max = float(df["Close"].iloc[hi_r + 1 :].max()) if hi_r < len(df) - 1 else None
    post_close_min = float(df["Close"].iloc[hi_r + 1 :].min()) if hi_r < len(df) - 1 else None

    if curr.direction == "down":
        # Find the most recent bullish (yang) counter-K-line cluster inside the down stroke
        rev_indices = [k for k in range(1, len(sub)) if closes[k] > opens[k]]
        if not rev_indices:
            lvl = float(highs[-1])
            triggered = (post_high is not None and post_high > lvl) or (post_close_max is not None and post_close_max > lvl)
            return {
                "direction": "down",
                "trigger_kind": "break_prev_bar_high",
                "level": round(lvl, 4),
                "ref_date": dates[-1],
                "triggered": triggered,
                "description": (
                    f"已上破下行笔末根K线高点 {round(lvl, 2)}（旧下行笔大概率结束）"
                    if triggered
                    else f"下行笔内无明显阳线，观察右侧站上 {round(lvl, 2)} 确认笔结束"
                ),
            }
        idx = rev_indices[-1]
        cluster_high = float(highs[idx])
        k = idx - 1
        while k >= 1 and closes[k] > opens[k]:
            cluster_high = max(cluster_high, float(highs[k]))
            k -= 1
        triggered = (
            (post_high is not None and post_high > cluster_high)
            or (post_close_max is not None and post_close_max > cluster_high)
            or last_close > cluster_high
            or last_high > cluster_high
        )
        return {
            "direction": "down",
            "trigger_kind": "break_reverse_yang_high",
            "level": round(cluster_high, 4),
            "ref_date": dates[idx],
            "triggered": triggered,
            "description": (
                f"已上破下行笔内最近反向阳线高点 {round(cluster_high, 2)}（旧下行笔大概率结束）"
                if triggered
                else f"待右侧放量站上反向阳线高点 {round(cluster_high, 2)} 确认下行笔结束"
            ),
        }
    else:
        rev_indices = [k for k in range(1, len(sub)) if closes[k] < opens[k]]
        if not rev_indices:
            lvl = float(lows[-1])
            triggered = (post_low is not None and post_low < lvl) or (post_close_min is not None and post_close_min < lvl)
            return {
                "direction": "up",
                "trigger_kind": "break_prev_bar_low",
                "level": round(lvl, 4),
                "ref_date": dates[-1],
                "triggered": triggered,
                "description": (
                    f"已下破上行笔末根K线低点 {round(lvl, 2)}（旧上行笔大概率结束）"
                    if triggered
                    else f"上行笔内无明显阴线，观察右侧跌破 {round(lvl, 2)} 确认笔结束"
                ),
            }
        idx = rev_indices[-1]
        cluster_low = float(lows[idx])
        k = idx - 1
        while k >= 1 and closes[k] < opens[k]:
            cluster_low = min(cluster_low, float(lows[k]))
            k -= 1
        triggered = (
            (post_low is not None and post_low < cluster_low)
            or (post_close_min is not None and post_close_min < cluster_low)
            or last_close < cluster_low
            or last_low < cluster_low
        )
        return {
            "direction": "up",
            "trigger_kind": "break_reverse_yin_low",
            "level": round(cluster_low, 4),
            "ref_date": dates[idx],
            "triggered": triggered,
            "description": (
                f"已下破上行笔内最近反向阴线低点 {round(cluster_low, 2)}（旧上行笔大概率结束）"
                if triggered
                else f"防守上行笔内最近反向阴线低点 {round(cluster_low, 2)}，跌破则防笔结束"
            ),
        }


def assess_momentum(strokes: list[Stroke]) -> dict:
    """Compare current stroke vs prior same-direction stroke combining MACD + Lesson 9/14 stroke class.

    Distinguishes:
    - 资金方向一致 (Capital Consensus): non-diminished MACD area/peak/efficiency.
    - 真背驰候选 (Divergence Candidate): new extreme + MACD weakened + NOT a steep 1-2 class violent crash/surge.
    - 非背驰背离 (Non-Divergence Pullback, 第14课): MACD area looks small only due to steep 1-3 class crash/surge ("还有力量").
    """
    if not strokes:
        return {
            "verdict": "insufficient_data",
            "momentum_reliable": False,
            "momentum_reliability_note": "笔结构样本不足，无法进行同向笔力度对比",
        }

    curr = strokes[-1]
    same_dir = [s for s in strokes[:-1] if s.direction == curr.direction]
    if not same_dir:
        return {
            "active_direction": curr.direction,
            "is_confirmed_stroke": curr.is_confirmed,
            "curr_strength_class": curr.strength_class,
            "curr_strength_tier": curr.strength_tier,
            "verdict": "single_stroke_no_comparison",
            "is_capital_consensus": False,
            "is_divergence_candidate": False,
            "momentum_reliable": False,
            "momentum_reliability_note": "无前序同向笔可对比，MACD面积比不适用",
        }

    prev = same_dir[-1]
    area_ratio = round(curr.macd_area / prev.macd_area, 2) if prev.macd_area > 0 else None
    peak_ratio = round(curr.macd_peak / prev.macd_peak, 2) if prev.macd_peak > 0 else None
    slope_ratio = round(curr.efficiency / prev.efficiency, 2) if prev.efficiency > 0 else None

    unreliable_reasons: list[str] = []
    if area_ratio is not None and area_ratio >= 8.0 and curr.raw_bars <= 4:
        unreliable_reasons.append(f"当前笔仅{curr.raw_bars}根K线且面积比达{area_ratio}x(小分母放大)")
    elif curr.raw_bars <= 3:
        unreliable_reasons.append(f"当前笔仅{curr.raw_bars}根K线")
    if prev.raw_bars <= 3:
        unreliable_reasons.append(f"前同向笔仅{prev.raw_bars}根K线")
    if prev.macd_area < 1e-6 and curr.macd_area < 1e-6:
        unreliable_reasons.append("前后笔MACD柱面积均近零")
    elif prev.macd_area < 1e-6:
        unreliable_reasons.append("前同向笔MACD柱面积近零")
    elif curr.macd_area < 1e-6:
        unreliable_reasons.append("当前笔MACD柱面积近零")

    momentum_reliable = len(unreliable_reasons) == 0
    momentum_reliability_note: Optional[str] = (
        None
        if momentum_reliable
        else f"{'、'.join(unreliable_reasons)}，MACD面积比失真，改看价格幅度与DIF极值"
    )

    if curr.direction == "down":
        made_new_extreme = curr.end_price < prev.end_price
        dif_new_extreme = curr.dif_extreme <= prev.dif_extreme
    else:
        made_new_extreme = curr.end_price > prev.end_price
        dif_new_extreme = curr.dif_extreme >= prev.dif_extreme

    is_consensus = False
    is_divergence = False
    is_non_divergence_power = False

    # Lesson 14 steep strong-bar guard: steep violent 1-2 class or <=4 bar 2x+ slope crash/surge is NOT weak divergence
    steep_strong_impulse = (
        curr.raw_bars <= 4
        and curr.strength_class <= 4
        and slope_ratio is not None
        and slope_ratio >= 2.0
    ) or (curr.strength_class in (1, 2) and slope_ratio is not None and slope_ratio >= 1.4 and curr.raw_bars <= 6)

    if made_new_extreme and area_ratio is not None and peak_ratio is not None:
        if area_ratio >= 0.85 or peak_ratio >= 0.90 or (slope_ratio is not None and slope_ratio >= 1.15 and peak_ratio >= 0.75):
            is_consensus = True
            verdict = "capital_consensus_down" if curr.direction == "down" else "capital_consensus_up"
        elif area_ratio < 0.78 and (peak_ratio < 0.88 or not dif_new_extreme):
            if steep_strong_impulse:
                is_non_divergence_power = True
                verdict = "non_divergence_down" if curr.direction == "down" else "non_divergence_up"
            else:
                is_divergence = True
                verdict = "divergence_candidate_down" if curr.direction == "down" else "divergence_candidate_up"
        else:
            verdict = "neutral_extension_down" if curr.direction == "down" else "neutral_extension_up"
    else:
        if curr.direction == "down":
            verdict = "higher_low_pullback"
        else:
            verdict = "lower_high_rebound"

    return {
        "active_direction": curr.direction,
        "is_confirmed_stroke": curr.is_confirmed,
        "curr_strength_class": curr.strength_class,
        "curr_strength_tier": curr.strength_tier,
        "made_new_extreme": made_new_extreme,
        "dif_new_extreme": dif_new_extreme,
        "area_ratio_vs_prev": area_ratio,
        "peak_ratio_vs_prev": peak_ratio,
        "slope_ratio_vs_prev": slope_ratio,
        "curr_pct_change": curr.pct_change,
        "prev_pct_change": prev.pct_change,
        "verdict": verdict,
        "is_capital_consensus": is_consensus,
        "is_divergence_candidate": is_divergence,
        "is_non_divergence_power": is_non_divergence_power,
        "momentum_reliable": momentum_reliable,
        "momentum_reliability_note": momentum_reliability_note,
    }


def classify_chanlun_signals(
    timeframe_label: str,
    last_close: float,
    merged: list[MergedBar],
    fractals: list[Fractal],
    pivots: list[Fractal],
    strokes: list[Stroke],
    zhongshus: list[Zhongshu],
    momentum: dict,
    stroke_end_trigger: Optional[dict] = None,
    segments: Optional[list[Segment]] = None,
    zhongshu_level: str = "stroke",
    zhongshu_level_label: Optional[str] = None,
) -> tuple[str, str, list[str]]:
    """Produce macro structure, Zhongshu position, and 19-lesson tactical Chanlun signals."""
    if not zhongshu_level_label:
        zhongshu_level_label = f"{timeframe_label}笔中枢"
    signals: list[str] = []

    # 1. Macro structure from Zhongshu sequence (reuse detector's stored relation for single source of truth)
    if len(zhongshus) >= 2:
        z_prev, z_curr = zhongshus[-2], zhongshus[-1]
        if z_curr.relation_to_prev in ("new_up", "new_down", "expansion_up", "expansion_down", "overlap"):
            rel = z_curr.relation_to_prev
        else:
            curr_dd, curr_gg = _effective_extremes(z_curr)
            rel = _classify_zhongshu_relation(z_prev, z_curr.zd, z_curr.zg, curr_dd, curr_gg)
        if rel == "new_up":
            macro_structure = "双中枢上涨趋势"
        elif rel == "expansion_up":
            macro_structure = "上涨中枢扩张(震荡升级)"
        elif rel == "new_down":
            macro_structure = "双中枢下跌趋势"
        elif rel == "expansion_down":
            macro_structure = "下跌中枢扩张(震荡升级)"
        else:
            macro_structure = "中枢扩展/宽幅震荡"
    elif len(zhongshus) == 1:
        macro_structure = "单中枢盘整"
    else:
        macro_structure = "趋势构建中(未形成中枢)"

    # 2. Price vs latest Zhongshu
    pos_vs_zs = "no_zhongshu"
    latest_zs = zhongshus[-1] if zhongshus else None
    if latest_zs:
        if last_close > latest_zs.zg:
            pct = (last_close / latest_zs.zg - 1) * 100
            pos_vs_zs = f"above_zg(+{pct:.2f}%)"
        elif last_close < latest_zs.zd:
            pct = (last_close / latest_zs.zd - 1) * 100
            pos_vs_zs = f"below_zd({pct:.2f}%)"
        else:
            span = latest_zs.zg - latest_zs.zd
            rel = (last_close - latest_zs.zd) / span if span > 0 else 0.5
            pos_vs_zs = f"inside_zs({rel*100:.0f}%)"

    latest_frac = fractals[-1] if fractals else None
    frac_just_confirmed = (
        latest_frac is not None and len(merged) > 0 and (len(merged) - 1 - latest_frac.confirmed_by_idx) <= 1
    )

    curr = strokes[-1] if strokes else None
    prev_same = [s for s in strokes[:-1] if curr and s.direction == curr.direction]

    # 3. Lesson 14 Direction-aware First-Zhongshu Third Buy / Third Sell check (Lesson 20 boundary-touch inclusive)
    is_nonfirst_3b = False
    is_nonfirst_3s = False
    if latest_zs:
        is_nonfirst_3b = latest_zs.trend_zs_index >= 2 and latest_zs.relation_to_prev in ("new_up", "initial")
        is_nonfirst_3s = latest_zs.trend_zs_index >= 2 and latest_zs.relation_to_prev in ("new_down", "initial")
        zs_tag_3b = f"第{latest_zs.trend_zs_index}个中枢·非首中枢" if is_nonfirst_3b else "首中枢"
        zs_tag_3s = f"第{latest_zs.trend_zs_index}个中枢·非首中枢" if is_nonfirst_3s else "首中枢"
    else:
        zs_tag_3b = ""
        zs_tag_3s = ""
    level_prefix = "段" if zhongshu_level == "segment" else ""
    has_3b = False
    has_3s = False

    unit_seq = segments if (zhongshu_level == "segment" and segments) else strokes
    if latest_zs and len(unit_seq) >= 2:
        u_curr = unit_seq[-1]
        if u_curr.direction == "down":
            prev_up = unit_seq[-2]
            if prev_up.direction == "up" and prev_up.high > latest_zs.zg and u_curr.low >= latest_zs.zg:
                has_3b = True
                if frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3b}三买确认(回踩不破ZG={latest_zs.zg}+底分型)")
                else:
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3b}准三买观察(回踩ZG={latest_zs.zg}上方)")
            elif not u_curr.is_confirmed and len(unit_seq) >= 3:
                prev_rebound = unit_seq[-2]
                prev_break = unit_seq[-3]
                if prev_rebound.direction == "up" and prev_break.direction == "down" and prev_break.low < latest_zs.zd and prev_rebound.high <= latest_zs.zd:
                    has_3s = True
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3s}三卖确认(反抽不过ZD={latest_zs.zd}·顶分型下行)")
        elif u_curr.direction == "up":
            prev_dn = unit_seq[-2]
            if prev_dn.direction == "down" and prev_dn.low < latest_zs.zd and u_curr.high <= latest_zs.zd:
                has_3s = True
                if frac_just_confirmed and latest_frac and latest_frac.kind == "top":
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3s}三卖确认(反抽不过ZD={latest_zs.zd}+顶分型)")
                else:
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3s}准三卖观察(反抽受阻于ZD={latest_zs.zd}下方)")
            elif not u_curr.is_confirmed and len(unit_seq) >= 3:
                prev_pullback = unit_seq[-2]
                prev_break = unit_seq[-3]
                if prev_pullback.direction == "down" and prev_break.direction == "up" and prev_break.high > latest_zs.zg and prev_pullback.low >= latest_zs.zg:
                    has_3b = True
                    signals.append(f"{timeframe_label}{level_prefix}{zs_tag_3b}三买确认(回踩不破ZG={latest_zs.zg}·底分型上攻)")

    # 4. Capital Consensus vs True Divergence vs Non-Divergence Power (Lesson 9 & 14)
    def _mom_ratio_tag(m_obj: dict, *, with_peak: bool = False, with_slope: bool = False) -> str:
        rel = m_obj.get("momentum_reliable")
        ar_v = m_obj.get("area_ratio_vs_prev")
        pr_v = m_obj.get("peak_ratio_vs_prev")
        sr_v = m_obj.get("slope_ratio_vs_prev")
        if rel is False:
            cp = m_obj.get("curr_pct_change")
            pp = m_obj.get("prev_pct_change")
            if isinstance(cp, (int, float)) and isinstance(pp, (int, float)):
                base = f"幅度{cp:+.2f}%vs前笔{pp:+.2f}%·短笔/零面积"
            else:
                base = "短笔/零面积(MACD比失真)"
            if with_slope and sr_v is not None:
                return f"斜率{sr_v}x,{base}"
            return base
        parts: list[str] = []
        if with_slope and sr_v is not None:
            parts.append(f"斜率{sr_v}x")
        if ar_v is not None:
            parts.append(f"area={ar_v}x")
        if with_peak and pr_v is not None:
            parts.append(f"peak={pr_v}x")
        return ",".join(parts) if parts else "力度待核"

    verdict = momentum.get("verdict")
    if verdict == "capital_consensus_down":
        m_tag = _mom_ratio_tag(momentum, with_peak=True)
        if latest_zs and last_close < latest_zs.zd:
            signals.append(f"{timeframe_label}向下离开段·资金方向一致({m_tag}·{curr.strength_class}类笔未背驰)")
        else:
            signals.append(f"{timeframe_label}空头资金方向一致推进({m_tag}·{curr.strength_class}类笔)")
    elif verdict == "capital_consensus_up":
        m_tag = _mom_ratio_tag(momentum, with_peak=True)
        if latest_zs and last_close > latest_zs.zg:
            signals.append(f"{timeframe_label}向上离开段·资金方向一致({m_tag}·{curr.strength_class}类笔趋势延续)")
        else:
            signals.append(f"{timeframe_label}多头资金方向一致推进({m_tag}·{curr.strength_class}类笔)")
    elif verdict == "non_divergence_down":
        m_tag = _mom_ratio_tag(momentum, with_slope=True)
        signals.append(f"{timeframe_label}非背驰背离(急跌{curr.strength_class}类笔·{m_tag}·仍有空头力量勿猜1买)")
    elif verdict == "non_divergence_up":
        m_tag = _mom_ratio_tag(momentum, with_slope=True)
        signals.append(f"{timeframe_label}非背驰背离(急拉{curr.strength_class}类笔·{m_tag}·仍有多头力量勿猜1卖)")
    elif verdict == "divergence_candidate_down":
        m_tag = _mom_ratio_tag(momentum)
        kind_label = "趋势背驰" if macro_structure == "双中枢下跌趋势" else "盘整背驰"
        if frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
            signals.append(f"{timeframe_label}{kind_label}+底分型确认(一买候选·{curr.strength_class}类弱笔·{m_tag})")
        elif pivots and pivots[-1].kind == "bottom" and (len(merged) - 1 - pivots[-1].confirmed_by_idx) <= 3:
            signals.append(f"{timeframe_label}{kind_label}+底分型结构(一买候选·{curr.strength_class}类笔·{m_tag})")
        else:
            signals.append(f"{timeframe_label}下跌力度衰减(疑似{kind_label}·{curr.strength_class}类笔·{m_tag}·待底分型)")
    elif verdict == "divergence_candidate_up":
        m_tag = _mom_ratio_tag(momentum)
        kind_label = "趋势背驰" if macro_structure == "双中枢上涨趋势" else "盘整背驰"
        if frac_just_confirmed and latest_frac and latest_frac.kind == "top":
            signals.append(f"{timeframe_label}{kind_label}+顶分型确认(一卖候选·{curr.strength_class}类弱笔·{m_tag})")
        else:
            signals.append(f"{timeframe_label}上涨力度衰减(疑似{kind_label}·{curr.strength_class}类笔·{m_tag}·观察顶分型)")

    # 5. Lesson 8/13/15: 恃强凌弱 (找大哥) + 3出 / 4出 / 2+3 + 2买/2卖
    if len(strokes) >= 3 and curr:
        if curr.direction == "down" and prev_same and not (has_3b and is_nonfirst_3b):
            prev_dn = prev_same[-1]
            prev_up = strokes[-2]
            # Higher low pullback (底抬高) -> potential D3 (3出) or D4 (4出) / 二买
            if curr.end_price > prev_dn.end_price:
                # Check if previous up stroke was strong ("找大哥": 3 > 1)
                prior_up = [s for s in strokes[:-2] if s.direction == "up"]
                brother_strong = bool(prior_up and (prev_up.high > prior_up[-1].high or abs(prev_up.pct_change) > abs(curr.pct_change)))
                # Distinguish D3 (first higher low after down leg) vs D4 (2nd higher low / post-ZS)
                is_d4 = bool(latest_zs and _ts_ge(latest_zs.end_date, prev_dn.start_date) and len(prev_same) >= 2 and prev_same[-1].end_price > prev_same[-2].end_price)
                out_tag = "4出(D4)" if is_d4 else "3出(D3·首次底抬高)"
                mode_tag = "2+3买" if (has_3b and not is_nonfirst_3b) else "二买"
                trig_ok = bool(stroke_end_trigger and stroke_end_trigger.get("triggered"))
                if (frac_just_confirmed and latest_frac and latest_frac.kind == "bottom") or trig_ok:
                    strong_note = "+找大哥(多强空弱)" if brother_strong else ""
                    signals.append(f"{timeframe_label}{out_tag}/{mode_tag}确认(回踩不破前低{prev_dn.end_price}{strong_note})")
                elif latest_zs and curr.end_price >= latest_zs.zd:
                    signals.append(f"{timeframe_label}{out_tag}蓄势(不破前低{prev_dn.end_price}·等底分型/破反向阳线)")
        elif curr.direction == "up" and prev_same and not (has_3s and is_nonfirst_3s):
            prev_up = prev_same[-1]
            prev_dn = strokes[-2]
            if curr.end_price < prev_up.end_price:
                prior_dn = [s for s in strokes[:-2] if s.direction == "down"]
                brother_strong = bool(prior_dn and (prev_dn.low < prior_dn[-1].low or abs(prev_dn.pct_change) > abs(curr.pct_change)))
                is_d4 = bool(latest_zs and _ts_ge(latest_zs.end_date, prev_up.start_date) and len(prev_same) >= 2 and prev_same[-1].end_price < prev_same[-2].end_price)
                out_tag = "4出(D4)空" if is_d4 else "3出(D3·首次顶降低)"
                mode_tag = "2+3卖" if (has_3s and not is_nonfirst_3s) else "二卖"
                trig_ok = bool(stroke_end_trigger and stroke_end_trigger.get("triggered"))
                if (frac_just_confirmed and latest_frac and latest_frac.kind == "top") or trig_ok:
                    strong_note = "+空强多弱" if brother_strong else ""
                    signals.append(f"{timeframe_label}{out_tag}/{mode_tag}确认(反弹不过前高{prev_up.end_price}{strong_note})")

    # 6. Fresh reversal launch right after completed stroke (reuse assess_momentum on strokes[:-1] for 1B/1S)
    if curr and not curr.is_confirmed and curr.raw_bars <= 4 and len(strokes) >= 4:
        last_opp = strokes[-2]
        prev_opp = strokes[-4]
        prev_mom = assess_momentum(strokes[:-1])
        if curr.direction == "up" and last_opp.direction == "down" and prev_opp.direction == "down":
            if prev_mom.get("is_divergence_candidate") and prev_mom.get("active_direction") == "down":
                pm_tag = _mom_ratio_tag(prev_mom)
                kind_label = "趋势背驰" if macro_structure == "双中枢下跌趋势" else "盘整背驰"
                signals.append(f"{timeframe_label}{kind_label}+底分型启动(一买/盘背·{last_opp.strength_class}类笔·{pm_tag})")
            elif last_opp.end_price > prev_opp.end_price and (frac_just_confirmed and latest_frac and latest_frac.kind == "bottom") and not (has_3b and is_nonfirst_3b):
                if not any("3出" in s or "4出" in s or "二买" in s for s in signals):
                    signals.append(f"{timeframe_label}3出(D3)/二买启动(不破前低{prev_opp.end_price}+底分型)")
        elif curr.direction == "down" and last_opp.direction == "up" and prev_opp.direction == "up":
            if prev_mom.get("is_divergence_candidate") and prev_mom.get("active_direction") == "up":
                pm_tag = _mom_ratio_tag(prev_mom)
                kind_label = "趋势背驰" if macro_structure == "双中枢上涨趋势" else "盘整背驰"
                signals.append(f"{timeframe_label}{kind_label}+顶分型启动(一卖/盘背·{last_opp.strength_class}类笔·{pm_tag})")
            elif last_opp.end_price < prev_opp.end_price and (frac_just_confirmed and latest_frac and latest_frac.kind == "top") and not (has_3s and is_nonfirst_3s):
                if not any("3出" in s or "4出" in s or "二卖" in s for s in signals):
                    signals.append(f"{timeframe_label}3出(D3)/二卖启动(不过前高{prev_opp.end_price}+顶分型)")

    # 7. Zhongshu edge & 9-stroke upgrade alerts
    if latest_zs:
        if zhongshu_level == "segment":
            signals.append(f"{timeframe_label}已切换为【{zhongshu_level_label}】([ZD={latest_zs.zd}, ZG={latest_zs.zg}])")
        elif latest_zs.is_nine_upgrade:
            signals.append(f"{timeframe_label}原中枢已延伸{3 + latest_zs.oscillation_strokes}笔(九段升级大级别震荡)")
        if pos_vs_zs.startswith("inside_zs"):
            span = latest_zs.zg - latest_zs.zd
            rel = (last_close - latest_zs.zd) / span if span > 0 else 0.5
            if rel <= 0.25 and frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
                signals.append(f"{timeframe_label}中枢下沿ZD={latest_zs.zd}底分型企稳")
            elif rel >= 0.75 and frac_just_confirmed and latest_frac and latest_frac.kind == "top":
                signals.append(f"{timeframe_label}中枢上沿ZG={latest_zs.zg}顶分型承压")

    if not signals:
        if pos_vs_zs.startswith("inside_zs") and latest_zs:
            signals.append(f"{timeframe_label}中枢震荡中([ZD={latest_zs.zd}, ZG={latest_zs.zg}])")
        elif curr:
            dir_zh = "向上" if curr.direction == "up" else "向下"
            signals.append(f"{timeframe_label}{dir_zh}笔常规运行中({curr.strength_class}类{curr.strength_tier}笔)")
        else:
            signals.append(f"{timeframe_label}结构样本不足")

    return macro_structure, pos_vs_zs, signals


def analyze_chanlun(df: pd.DataFrame, timeframe_label: str = "日线", full_export: bool = False) -> dict:
    """Run full 19-lesson practical Chanlun + MACD analysis on an OHLCV DataFrame."""
    if not df.empty:
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty or len(df) < 15:
        return {"error": f"Insufficient bars ({len(df)}) for Chanlun analysis"}

    close = df["Close"].astype(float)
    last_close = float(close.iloc[-1])
    dif, dea, hist = compute_macd(close)

    merged = process_inclusion(df)
    fractals = detect_fractals(merged)
    pivots = build_pivots(merged, fractals, raw_df=df)
    strokes = build_strokes(df, merged, pivots, dif, hist)
    segments = build_segments(strokes)
    stroke_zss = detect_zhongshus(strokes, last_close)
    segment_zss = detect_segment_zhongshus(segments)
    zs_level, zs_level_label, zhongshus = select_active_zhongshus(
        stroke_zss, segment_zss, strokes, timeframe_label=timeframe_label
    )
    momentum = assess_momentum(strokes)
    stroke_end_trigger = detect_stroke_end_trigger(df, strokes)

    macro_structure, pos_vs_zs, signals = classify_chanlun_signals(
        timeframe_label=timeframe_label,
        last_close=last_close,
        merged=merged,
        fractals=fractals,
        pivots=pivots,
        strokes=strokes,
        zhongshus=zhongshus,
        momentum=momentum,
        stroke_end_trigger=stroke_end_trigger,
        segments=segments,
        zhongshu_level=zs_level,
        zhongshu_level_label=zs_level_label,
    )

    latest_frac = None
    if fractals:
        lf = fractals[-1]
        bars_ago = (len(merged) - 1) - lf.confirmed_by_idx
        latest_frac = {
            "kind": lf.kind,
            "date": lf.date,
            "price": round(lf.price, 4),
            "confirmed_date": lf.confirmed_date,
            "confirmed_merged_bars_ago": bars_ago,
            "just_confirmed": bars_ago <= 1,
            "strength": lf.strength,
        }

    result = {
        "timeframe": timeframe_label,
        "last_close": round(last_close, 4),
        "raw_bars": len(df),
        "merged_bars": len(merged),
        "macro_structure": macro_structure,
        "zhongshu_level": zs_level,
        "zhongshu_level_label": zs_level_label,
        "price_vs_zhongshu": pos_vs_zs,
        "latest_fractal": latest_frac,
        "latest_zhongshu": asdict(zhongshus[-1]) if zhongshus else None,
        "recent_zhongshus": [asdict(z) for z in zhongshus[-3:]],
        "zhongshu_count": len(zhongshus),
        "stroke_zhongshus": [asdict(z) for z in stroke_zss[-3:]],
        "segment_zhongshus": [asdict(z) for z in segment_zss[-3:]],
        "latest_segment": asdict(segments[-1]) if segments else None,
        "segment_count": len(segments),
        "recent_strokes": [asdict(s) for s in strokes[-5:]],
        "stroke_end_trigger": stroke_end_trigger,
        "momentum_assessment": momentum,
        "signals": signals,
    }
    if full_export:
        result["all_strokes"] = [asdict(s) for s in strokes]
        result["all_segments"] = [asdict(s) for s in segments]
        result["all_stroke_zhongshus"] = [asdict(z) for z in stroke_zss]
        result["all_segment_zhongshus"] = [asdict(z) for z in segment_zss]
    return result


def _extract_signal_timestamp(cl: dict) -> Optional[str]:
    """Extract the most relevant bar timestamp associated with the current Chanlun signal."""
    lf = cl.get("latest_fractal") if isinstance(cl.get("latest_fractal"), dict) else None
    if lf and lf.get("just_confirmed"):
        ts = lf.get("confirmed_date") or lf.get("date")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    trig = cl.get("stroke_end_trigger") if isinstance(cl.get("stroke_end_trigger"), dict) else None
    if trig and trig.get("triggered"):
        ts = trig.get("ref_date")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    strokes = cl.get("recent_strokes") or []
    if strokes and isinstance(strokes[-1], dict):
        ts = strokes[-1].get("end_date")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    if lf:
        ts = lf.get("confirmed_date") or lf.get("date")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    lz = cl.get("latest_zhongshu") if isinstance(cl.get("latest_zhongshu"), dict) else None
    if lz:
        ts = lz.get("oscillation_end_date") or lz.get("end_date")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    return None


def evaluate_signal_tier(chanlun_result: dict, timeframe_label: Optional[str] = None) -> dict:
    """Classify actionable tier, bias, and structured signal_meta from Chanlun structure on any timeframe."""
    if not isinstance(chanlun_result, dict) or "error" in chanlun_result:
        err = chanlun_result.get("error", "数据不足") if isinstance(chanlun_result, dict) else "数据不足"
        return {
            "tier": "数据不足",
            "summary": err,
            "action_bias": "跳过",
            "signal_meta": {
                "signal_type": "insufficient_data",
                "direction": "neutral",
                "confirmed": False,
                "momentum_confirmed": False,
                "is_exhaustion": False,
                "signal_timestamp": None,
            },
        }

    tf = timeframe_label or chanlun_result.get("timeframe") or "日线"
    d_mom = chanlun_result.get("momentum_assessment", {}) or {}
    d_dir = d_mom.get("active_direction")
    d_cons = bool(d_mom.get("is_capital_consensus", False))
    d_div = bool(d_mom.get("is_divergence_candidate", False))
    d_non_div = bool(d_mom.get("is_non_divergence_power", False))
    raw_regime = str(d_mom.get("regime") or "").strip().lower()
    raw_verdict = str(d_mom.get("verdict") or "").strip().lower()
    regime = raw_regime or raw_verdict
    regimes_present = {r for r in (raw_regime, raw_verdict) if r}

    strong_down_regimes = {"capital_consensus_down", "strong_down_continuation"}
    strong_up_regimes = {"capital_consensus_up", "strong_up_continuation"}
    weak_or_neutral_exact = {
        "neutral",
        "insufficient_data",
        "higher_low_pullback",
        "lower_high_rebound",
        "divergence_candidate_down",
        "divergence_candidate_up",
    }

    def _is_weak_or_neutral_regime(r: str) -> bool:
        return bool(r) and (
            r.startswith(("neutral_", "single_stroke", "weak"))
            or r in weak_or_neutral_exact
        )

    has_neutral_or_weak_regime = any(_is_weak_or_neutral_regime(r) for r in regimes_present)
    regime_strong_down = any(r in strong_down_regimes for r in regimes_present) and not has_neutral_or_weak_regime
    regime_strong_up = any(r in strong_up_regimes for r in regimes_present) and not has_neutral_or_weak_regime
    cons_explicitly_false = d_mom.get("is_capital_consensus") is False

    pos = chanlun_result.get("price_vs_zhongshu", "") or ""
    sigs = [str(s) for s in (chanlun_result.get("signals", []) or [])]
    sig_ts = _extract_signal_timestamp(chanlun_result)

    # Candidate/unconfirmed markers MUST never be promoted to confirmed 1/2/3 buy/sell
    unconfirmed_tokens = ("候选", "疑似", "准", "观察", "蓄势")

    def _has_unconfirmed(sig: str) -> bool:
        return any(tok in sig for tok in unconfirmed_tokens)

    # Confirmed actionable signals require explicit "确认" or "启动" AND zero unconfirmed markers
    has_first_zs_3b = any(
        "三买" in s and ("确认" in s or "启动" in s) and "非首中枢" not in s and not _has_unconfirmed(s)
        for s in sigs
    )
    has_first_zs_3s = any(
        "三卖" in s and ("确认" in s or "启动" in s) and "非首中枢" not in s and not _has_unconfirmed(s)
        for s in sigs
    )
    has_2p3_buy = any(
        "2+3买" in s and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )
    has_2p3_sell = any(
        "2+3卖" in s and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )
    has_nonfirst_3b = any(
        "非首中枢三买" in s and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )
    has_nonfirst_3s = any(
        "非首中枢三卖" in s and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )
    has_d3_d4_buy = any(
        ("3出(D3" in s or "4出(D4" in s)
        and ("确认" in s or "启动" in s)
        and not _has_unconfirmed(s)
        and "4出(D4)空" not in s
        and "顶降低" not in s
        and "二卖" not in s
        and "2+3卖" not in s
        for s in sigs
    )
    has_d3_d4_sell = any(
        ("3出(D3" in s or "4出(D4" in s)
        and ("确认" in s or "启动" in s)
        and not _has_unconfirmed(s)
        and ("4出(D4)空" in s or "顶降低" in s or "二卖" in s or "2+3卖" in s or "空强多弱" in s)
        for s in sigs
    )
    has_1b_2b = any(
        ("一买" in s or "二买" in s) and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )
    has_1s_2s = any(
        ("一卖" in s or "二卖" in s) and ("确认" in s or "启动" in s) and not _has_unconfirmed(s)
        for s in sigs
    )

    has_non_div_down = (
        (d_non_div and d_dir == "down")
        or ("non_divergence_down" in regimes_present)
        or any("非背驰" in s and "急跌" in s for s in sigs)
    ) and not has_neutral_or_weak_regime
    has_non_div_up = (
        (d_non_div and d_dir == "up")
        or ("non_divergence_up" in regimes_present)
        or any("非背驰" in s and "急拉" in s for s in sigs)
    ) and not has_neutral_or_weak_regime

    has_consensus_down_text = any(
        ("空头资金方向一致" in s or "向下离开段·资金方向一致" in s) for s in sigs
    )
    has_consensus_up_text = any(
        ("多头资金方向一致" in s or "向上离开段·资金方向一致" in s) for s in sigs
    )
    has_strong_consensus_down = (
        regime_strong_down
        or (d_cons and d_dir == "down" and not has_neutral_or_weak_regime)
        or (has_consensus_down_text and not has_neutral_or_weak_regime and not cons_explicitly_false)
    )
    has_strong_consensus_up = (
        regime_strong_up
        or (d_cons and d_dir == "up" and not has_neutral_or_weak_regime)
        or (has_consensus_up_text and not has_neutral_or_weak_regime and not cons_explicitly_false)
    )
    has_weak_consensus_down = (
        not has_strong_consensus_down
        and (has_consensus_down_text or (d_cons and d_dir == "down"))
    )
    has_weak_consensus_up = (
        not has_strong_consensus_up
        and (has_consensus_up_text or (d_cons and d_dir == "up"))
    )

    buy_cand_tokens = ("一买", "二买", "三买", "2+3买", "底分型", "底抬高", "下跌力度衰减", "不破前低")
    sell_cand_tokens = ("一卖", "二卖", "三卖", "2+3卖", "顶分型", "顶降低", "上涨力度衰减", "不过前高")
    has_candidate_buy = any(
        _has_unconfirmed(s)
        and any(tok in s for tok in buy_cand_tokens)
        and not any(tok in s for tok in sell_cand_tokens)
        for s in sigs
    )
    has_candidate_sell = any(
        _has_unconfirmed(s) and any(tok in s for tok in sell_cand_tokens)
        for s in sigs
    )
    has_watch_only = has_candidate_buy or has_candidate_sell or any(_has_unconfirmed(s) for s in sigs)

    confirmed = False
    momentum_confirmed = False
    is_exhaustion = False
    signal_type = "neutral"
    direction = "neutral"

    if (has_2p3_buy and not has_nonfirst_3b) or has_first_zs_3b:
        tier = "买卖点信号"
        summary = f"{tf}首中枢三买 / 2+3买结构"
        action_bias = f"回踩不破首中枢上沿 ZG 配合{tf}底分型/破反向阳线，顺势做多"
        signal_type = "confirmed_buy"
        direction = "long"
        confirmed = True
        momentum_confirmed = bool(regime_strong_up or (d_cons and d_dir == "up" and not has_neutral_or_weak_regime))
    elif (has_2p3_sell and not has_nonfirst_3s) or has_first_zs_3s:
        tier = "买卖点信号"
        summary = f"{tf}首中枢三卖 / 2+3卖结构"
        action_bias = "反抽不过首中枢下沿 ZD 配合顶分型/破反向阴线，顺势减仓或做空"
        signal_type = "confirmed_sell"
        direction = "short"
        confirmed = True
        momentum_confirmed = bool(regime_strong_down or (d_cons and d_dir == "down" and not has_neutral_or_weak_regime))
    elif (has_d3_d4_sell or has_1s_2s) and not has_nonfirst_3s:
        tier = "买卖点信号"
        summary = f"{tf} 3出/4出/一二卖结构触发"
        action_bias = "反弹不过前高配合顶分型或破反向阴线，顺势减仓/做空"
        signal_type = "confirmed_sell"
        direction = "short"
        confirmed = True
        momentum_confirmed = bool(regime_strong_down or (d_cons and d_dir == "down" and not has_neutral_or_weak_regime))
    elif (has_d3_d4_buy or has_1b_2b) and not has_nonfirst_3b:
        tier = "买卖点信号"
        summary = f"{tf} 3出/4出/一二买结构触发"
        action_bias = "围绕底抬高与破反向阳线触发，结合前低防守评估中长线建仓"
        signal_type = "confirmed_buy"
        direction = "long"
        confirmed = True
        momentum_confirmed = bool(regime_strong_up or (d_cons and d_dir == "up" and not has_neutral_or_weak_regime))
    elif has_nonfirst_3b:
        tier = "买卖点信号"
        summary = f"{tf}非首中枢三买(防追高)"
        action_bias = "已属多中枢后段三买，注意仓位克制，防延伸背驰"
        signal_type = "exhaustion_buy_warn"
        direction = "long"
        confirmed = False
        momentum_confirmed = bool(regime_strong_up or (d_cons and d_dir == "up" and not has_neutral_or_weak_regime))
        is_exhaustion = True
    elif has_nonfirst_3s:
        tier = "买卖点信号"
        summary = f"{tf}非首中枢三卖(防追空)"
        action_bias = "已属多中枢后段三卖，谨防末端急跌出清后反抽"
        signal_type = "exhaustion_sell_warn"
        direction = "short"
        confirmed = False
        momentum_confirmed = bool(regime_strong_down or (d_cons and d_dir == "down" and not has_neutral_or_weak_regime))
        is_exhaustion = True
    elif has_non_div_down:
        tier = "资金一致推进(勿逆势)"
        summary = f"{tf}非背驰急跌(仍有空头力量)"
        action_bias = "虽MACD面积缩但属急跌强笔，严禁当一买盲目抄底，等底抬高3出"
        signal_type = "steep_non_div_down"
        direction = "short"
        confirmed = False
        momentum_confirmed = True
    elif has_non_div_up:
        tier = "资金一致推进(顺势)"
        summary = f"{tf}非背驰急拉(仍有多头力量)"
        action_bias = "急拉强笔不猜顶，等回调弱势3出/4出再评估"
        signal_type = "steep_non_div_up"
        direction = "long"
        confirmed = False
        momentum_confirmed = True
    elif has_strong_consensus_down:
        tier = "资金一致推进(勿逆势)"
        summary = f"{tf}空头资金方向一致推进"
        action_bias = "向下离开段未见背驰，尊重空头主导资金方向，严禁逆势接飞刀"
        signal_type = "momentum_short"
        direction = "short"
        confirmed = False
        momentum_confirmed = True
    elif has_strong_consensus_up:
        tier = "资金一致推进(顺势)"
        summary = f"{tf}多头资金方向一致推进"
        action_bias = f"向上离开段动能充沛未背驰，顺势持有或等待{tf}回踩买点"
        signal_type = "momentum_long"
        direction = "long"
        confirmed = False
        momentum_confirmed = True
    elif has_weak_consensus_down:
        tier = "常规结构运行"
        summary = f"{tf}弱空头动能延伸(未确认资金一致)"
        action_bias = f"动能评估为中性/未确认({regime or '未满足强一致标准'})，不视作强资金一致推进，继续跟踪{tf}结构确认"
        signal_type = "weak_momentum_short"
        direction = "short"
        confirmed = False
        momentum_confirmed = False
    elif has_weak_consensus_up:
        tier = "常规结构运行"
        summary = f"{tf}弱多头动能延伸(未确认资金一致)"
        action_bias = f"动能评估为中性/未确认({regime or '未满足强一致标准'})，不视作强资金一致推进，继续跟踪{tf}结构确认"
        signal_type = "weak_momentum_long"
        direction = "long"
        confirmed = False
        momentum_confirmed = False
    elif d_div or has_watch_only:
        tier = "背驰观察"
        summary = f"{tf}准买卖点/背驰观察(待确认)"
        action_bias = "等待破反向K线或底/顶分型停顿确认后再行动，勿提前抢跑"
        if has_candidate_sell and not has_candidate_buy:
            signal_type = "candidate_sell"
            direction = "short"
        elif has_candidate_buy and not has_candidate_sell:
            signal_type = "candidate_buy"
            direction = "long"
        elif d_div and d_dir == "down":
            signal_type = "candidate_buy"
            direction = "long"
        elif d_div and d_dir == "up":
            signal_type = "candidate_sell"
            direction = "short"
        elif has_candidate_buy:
            signal_type = "candidate_buy"
            direction = "long"
        elif has_candidate_sell:
            signal_type = "candidate_sell"
            direction = "short"
        else:
            signal_type = "neutral"
            direction = "neutral"
        confirmed = False
        momentum_confirmed = False
    elif pos.startswith("inside_zs"):
        tier = "中枢震荡"
        summary = f"{tf}中枢内部震荡"
        action_bias = f"观察{tf}中枢下沿 ZD 支撑与上沿 ZG 突破方向"
        signal_type = "consolidation"
        direction = "neutral"
        confirmed = False
        momentum_confirmed = False
    else:
        tier = "常规结构运行"
        summary = sigs[0] if sigs else f"{tf}结构运行中"
        action_bias = f"跟踪当前{tf}笔延伸及第11课反向K线破位触发"
        signal_type = "neutral"
        direction = "neutral"
        confirmed = False
        momentum_confirmed = False

    dq = chanlun_result.get("data_quality") if isinstance(chanlun_result.get("data_quality"), dict) else None
    quality_degraded = bool(dq and dq.get("quality_grade") == "degraded")
    if quality_degraded:
        dq_notes = dq.get("notes") if isinstance(dq, dict) and isinstance(dq.get("notes"), list) else []
        dq_hint = dq_notes[0] if dq_notes else "数据源存在高比例结算平K/陈旧/坏点"
        tier = "数据质量降级"
        summary = f"{tf}数据质量降级({summary})"
        action_bias = f"{dq_hint}；禁止凭该序列直接触发买卖，请改用同板块高流动性主力锚核验"
        signal_type = "data_quality_degraded"
        confirmed = False
        momentum_confirmed = False

    return {
        "tier": tier,
        "summary": summary,
        "action_bias": action_bias,
        "signal_meta": {
            "signal_type": signal_type,
            "direction": direction,
            "confirmed": confirmed,
            "momentum_confirmed": momentum_confirmed,
            "is_exhaustion": is_exhaustion,
            "quality_degraded": quality_degraded,
            "signal_timestamp": sig_ts,
        },
    }


def evaluate_daily_signal_tier(daily: dict) -> dict:
    """Backward-compatible wrapper delegating to evaluate_signal_tier."""
    tf = daily.get("timeframe") if isinstance(daily, dict) else None
    return evaluate_signal_tier(daily, timeframe_label=tf or "日线")


def _as_finite_float(val) -> Optional[float]:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float, np.floating, np.integer)):
        fval = float(val)
        return fval if np.isfinite(fval) else None
    return None


def _infer_structure_bias(cl: Optional[dict], sig_tier: dict) -> str:
    """Infer 'long' / 'short' / 'neutral' directional bias from Chanlun structure & signal_meta.

    Capital-consensus momentum (多头/空头资金方向一致推进) and confirmed/exhaustion directional
    signals establish bias, while weak/neutral running strokes (常规运行中) stay 'neutral'.
    """
    if not isinstance(cl, dict) or "error" in cl:
        return "neutral"

    meta = sig_tier.get("signal_meta") if isinstance(sig_tier, dict) else None
    if isinstance(meta, dict):
        sig_type = meta.get("signal_type")
        sig_dir = meta.get("direction")
        if sig_type in (
            "confirmed_buy",
            "confirmed_sell",
            "exhaustion_buy_warn",
            "exhaustion_sell_warn",
            "momentum_long",
            "momentum_short",
            "steep_non_div_up",
            "steep_non_div_down",
        ) and sig_dir in ("long", "short"):
            return sig_dir

    pos = cl.get("price_vs_zhongshu", "") or ""
    mom = cl.get("momentum_assessment", {}) or {}
    verdict = str(mom.get("regime") or mom.get("verdict") or "").strip().lower()

    if pos.startswith("above_zg"):
        if verdict in ("divergence_candidate_up", "capital_consensus_down", "strong_down_continuation", "non_divergence_down"):
            return "neutral"
        return "long"
    if pos.startswith("below_zd"):
        if verdict in ("divergence_candidate_down", "capital_consensus_up", "strong_up_continuation", "non_divergence_up"):
            return "neutral"
        return "short"
    if pos.startswith("inside_zs"):
        return "neutral"

    if verdict in ("capital_consensus_up", "strong_up_continuation", "non_divergence_up"):
        return "long"
    if verdict in ("capital_consensus_down", "strong_down_continuation", "non_divergence_down"):
        return "short"

    macro = cl.get("macro_structure", "") or ""
    if macro == "双中枢上涨趋势":
        return "long"
    if macro == "双中枢下跌趋势":
        return "short"
    return "neutral"


def _extract_ref_price(daily_cl: Optional[dict], intraday_cl: Optional[dict]) -> Optional[float]:
    for cl in (intraday_cl, daily_cl):
        if not isinstance(cl, dict):
            continue
        lc = _as_finite_float(cl.get("last_close"))
        if lc is not None:
            return lc
    for cl in (intraday_cl, daily_cl):
        if not isinstance(cl, dict):
            continue
        strokes = cl.get("recent_strokes") or []
        if strokes and isinstance(strokes[-1], dict):
            ep = _as_finite_float(strokes[-1].get("end_price"))
            if ep is not None:
                return ep
    return None


def _extract_reverse_k_watch(
    cl: Optional[dict],
    trade_dir: Optional[str],
    ref_price: Optional[float],
) -> Optional[float]:
    """Extract Lesson 11 reverse-K micro-watch level separately from structural tactical stops."""
    if not isinstance(cl, dict) or "error" in cl:
        return None

    def _side_ok(level: float) -> bool:
        if ref_price is None or trade_dir not in ("long", "short"):
            return True
        if trade_dir == "long":
            return level < ref_price
        return level > ref_price

    for key in ("reverse_k_trigger", "stroke_end_trigger"):
        trig = cl.get(key)
        if trig is None:
            continue
        direct_val = _as_finite_float(trig)
        if direct_val is not None and _side_ok(direct_val):
            return round(direct_val, 4)
        if isinstance(trig, dict):
            lvl = _as_finite_float(trig.get("level"))
            t_dir = trig.get("direction")
            dir_matches = (
                t_dir is None
                or trade_dir is None
                or (trade_dir == "long" and t_dir == "up")
                or (trade_dir == "short" and t_dir == "down")
            )
            if lvl is not None and dir_matches and _side_ok(lvl):
                return round(lvl, 4)
    return None


def _extract_tactical_stop(
    cl: Optional[dict],
    trade_dir: Optional[str],
    ref_price: Optional[float],
) -> Optional[float]:
    """Extract structural tactical stop from stroke extreme, confirmed opposite fractal, or Zhongshu ZD/ZG.

    Intentionally excludes Lesson 11 1-bar micro reverse-K levels (exposed separately via
    _extract_reverse_k_watch) so tactical stops anchor on real Chanlun structure.
    """
    if not isinstance(cl, dict) or "error" in cl:
        return None

    def _side_ok(level: float) -> bool:
        if ref_price is None or trade_dir not in ("long", "short"):
            return True
        if trade_dir == "long":
            return level < ref_price
        return level > ref_price

    strokes = [s for s in (cl.get("recent_strokes") or []) if isinstance(s, dict)]
    latest_frac = cl.get("latest_fractal") if isinstance(cl.get("latest_fractal"), dict) else None
    latest_zs = cl.get("latest_zhongshu") if isinstance(cl.get("latest_zhongshu"), dict) else None

    candidates: list[float] = []
    if trade_dir == "long":
        if strokes:
            curr = strokes[-1]
            if curr.get("direction") == "up":
                for field in ("start_price", "low"):
                    v = _as_finite_float(curr.get(field))
                    if v is not None:
                        candidates.append(v)
            elif curr.get("direction") == "down":
                for field in ("low", "end_price"):
                    v = _as_finite_float(curr.get(field))
                    if v is not None:
                        candidates.append(v)
        for s in reversed(strokes):
            if s.get("direction") == "down" and s.get("is_confirmed", True):
                for field in ("end_price", "low"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        if latest_frac and latest_frac.get("kind") == "bottom":
            v = _as_finite_float(latest_frac.get("price"))
            if v is not None:
                candidates.append(v)
        for s in reversed(strokes):
            for field in ("low", "start_price"):
                v = _as_finite_float(s.get(field))
                if v is not None:
                    candidates.append(v)
        if latest_zs:
            v = _as_finite_float(latest_zs.get("zd"))
            if v is not None:
                candidates.append(v)
    elif trade_dir == "short":
        if strokes:
            curr = strokes[-1]
            if curr.get("direction") == "down":
                for field in ("start_price", "high"):
                    v = _as_finite_float(curr.get(field))
                    if v is not None:
                        candidates.append(v)
            elif curr.get("direction") == "up":
                for field in ("high", "end_price"):
                    v = _as_finite_float(curr.get(field))
                    if v is not None:
                        candidates.append(v)
        for s in reversed(strokes):
            if s.get("direction") == "up" and s.get("is_confirmed", True):
                for field in ("end_price", "high"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        if latest_frac and latest_frac.get("kind") == "top":
            v = _as_finite_float(latest_frac.get("price"))
            if v is not None:
                candidates.append(v)
        for s in reversed(strokes):
            for field in ("high", "start_price"):
                v = _as_finite_float(s.get(field))
                if v is not None:
                    candidates.append(v)
        if latest_zs:
            v = _as_finite_float(latest_zs.get("zg"))
            if v is not None:
                candidates.append(v)
    else:
        if strokes:
            v = _as_finite_float(strokes[-1].get("start_price"))
            if v is not None:
                candidates.append(v)

    for cand in candidates:
        if _side_ok(cand):
            return round(cand, 4)
    return None


def _extract_structural_stop(
    cl: Optional[dict],
    trade_dir: Optional[str],
    ref_price: Optional[float],
) -> Optional[float]:
    """Extract structural invalidation anchor: Zhongshu ZD (long) / ZG (short) on protective side,
    falling back to confirmed swing extreme when Zhongshu edge is absent or not protective.
    """
    if not isinstance(cl, dict) or "error" in cl:
        return None

    def _side_ok(level: float) -> bool:
        if ref_price is None or trade_dir not in ("long", "short"):
            return True
        if trade_dir == "long":
            return level < ref_price
        return level > ref_price

    latest_zs = cl.get("latest_zhongshu") if isinstance(cl.get("latest_zhongshu"), dict) else None
    if latest_zs is not None:
        if trade_dir == "long":
            zd = _as_finite_float(latest_zs.get("zd"))
            if zd is not None and _side_ok(zd):
                return round(zd, 4)
        elif trade_dir == "short":
            zg = _as_finite_float(latest_zs.get("zg"))
            if zg is not None and _side_ok(zg):
                return round(zg, 4)

    strokes = [s for s in (cl.get("recent_strokes") or []) if isinstance(s, dict)]
    latest_frac = cl.get("latest_fractal") if isinstance(cl.get("latest_fractal"), dict) else None
    candidates: list[float] = []

    if trade_dir == "long":
        for s in reversed(strokes):
            if s.get("is_confirmed", True) and s.get("direction") == "down":
                for field in ("low", "end_price"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        for s in reversed(strokes):
            if s.get("is_confirmed", True):
                for field in ("low", "start_price"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        if latest_frac and latest_frac.get("kind") == "bottom":
            v = _as_finite_float(latest_frac.get("price"))
            if v is not None:
                candidates.append(v)
        if latest_zs is not None:
            for field in ("dd", "oscillation_dd"):
                v = _as_finite_float(latest_zs.get(field))
                if v is not None:
                    candidates.append(v)
        for s in reversed(strokes):
            for field in ("low", "start_price"):
                v = _as_finite_float(s.get(field))
                if v is not None:
                    candidates.append(v)
    elif trade_dir == "short":
        for s in reversed(strokes):
            if s.get("is_confirmed", True) and s.get("direction") == "up":
                for field in ("high", "end_price"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        for s in reversed(strokes):
            if s.get("is_confirmed", True):
                for field in ("high", "start_price"):
                    v = _as_finite_float(s.get(field))
                    if v is not None:
                        candidates.append(v)
        if latest_frac and latest_frac.get("kind") == "top":
            v = _as_finite_float(latest_frac.get("price"))
            if v is not None:
                candidates.append(v)
        if latest_zs is not None:
            for field in ("gg", "oscillation_gg"):
                v = _as_finite_float(latest_zs.get(field))
                if v is not None:
                    candidates.append(v)
        for s in reversed(strokes):
            for field in ("high", "start_price"):
                v = _as_finite_float(s.get(field))
                if v is not None:
                    candidates.append(v)

    for cand in candidates:
        if _side_ok(cand):
            return round(cand, 4)
    return None


def assess_dual_timeframe_alignment(
    daily_cl: dict,
    intraday_cl: dict,
    desired_direction: Optional[str] = None,
) -> dict:
    """Assess dual-timeframe Chanlun alignment ('日线定方向 + 30min 诞生交易')."""
    norm_dir: Optional[str] = None
    if desired_direction is not None:
        cleaned = str(desired_direction).strip().lower()
        if cleaned:
            if cleaned not in ("long", "short"):
                raise ValueError(
                    f"Unsupported desired_direction: {desired_direction!r}; expected 'long', 'short', or None"
                )
            norm_dir = cleaned

    daily_tf = (daily_cl.get("timeframe") if isinstance(daily_cl, dict) else None) or "日线"
    intraday_tf = (intraday_cl.get("timeframe") if isinstance(intraday_cl, dict) else None) or "30分钟"

    daily_sig = evaluate_signal_tier(daily_cl, timeframe_label=daily_tf)
    intraday_sig = evaluate_signal_tier(intraday_cl, timeframe_label=intraday_tf)
    daily_signal_meta = daily_sig.get("signal_meta") or {
        "signal_type": "insufficient_data",
        "direction": "neutral",
        "confirmed": False,
        "momentum_confirmed": False,
        "is_exhaustion": False,
        "signal_timestamp": None,
    }
    intraday_signal_meta = intraday_sig.get("signal_meta") or {
        "signal_type": "insufficient_data",
        "direction": "neutral",
        "confirmed": False,
        "momentum_confirmed": False,
        "is_exhaustion": False,
        "signal_timestamp": None,
    }

    daily_bias = _infer_structure_bias(daily_cl, daily_sig)
    intraday_bias = _infer_structure_bias(intraday_cl, intraday_sig)

    intraday_summary = intraday_sig.get("summary", "") or ""
    intraday_tier = intraday_sig.get("tier", "数据不足")
    ref_dir = norm_dir or (daily_bias if daily_bias in ("long", "short") else None)

    intra_type = intraday_signal_meta.get("signal_type", "neutral")
    intra_dir = intraday_signal_meta.get("direction", "neutral")
    intra_confirmed = bool(intraday_signal_meta.get("confirmed", False))
    intra_exhaustion = bool(intraday_signal_meta.get("is_exhaustion", False))

    if (
        not isinstance(intraday_cl, dict)
        or "error" in intraday_cl
        or intraday_tier == "数据不足"
        or intra_type == "insufficient_data"
    ):
        intraday_action = "wait_structure"
    elif intra_exhaustion or intra_type in ("exhaustion_buy_warn", "exhaustion_sell_warn"):
        intraday_action = "exhaustion_warning"
    elif intra_type == "steep_non_div_down":
        intraday_action = "exhaustion_warning" if ref_dir != "short" else "wait_structure"
    elif intra_type == "steep_non_div_up":
        intraday_action = "exhaustion_warning" if ref_dir != "long" else "wait_structure"
    elif intra_confirmed and intra_dir == "long":
        intraday_action = "entry_long"
    elif intra_confirmed and intra_dir == "short":
        intraday_action = "entry_short"
    else:
        intraday_action = "wait_structure"

    daily_zs = daily_cl.get("latest_zhongshu") if isinstance(daily_cl, dict) else None
    intraday_zs = intraday_cl.get("latest_zhongshu") if isinstance(intraday_cl, dict) else None
    daily_zd = _as_finite_float(daily_zs.get("zd")) if isinstance(daily_zs, dict) else None
    daily_zg = _as_finite_float(daily_zs.get("zg")) if isinstance(daily_zs, dict) else None
    intraday_zd = _as_finite_float(intraday_zs.get("zd")) if isinstance(intraday_zs, dict) else None
    intraday_zg = _as_finite_float(intraday_zs.get("zg")) if isinstance(intraday_zs, dict) else None

    stop_dir = ref_dir or (intraday_bias if intraday_bias in ("long", "short") else None)
    if stop_dir is None and isinstance(intraday_cl, dict):
        act = (intraday_cl.get("momentum_assessment") or {}).get("active_direction")
        if act == "up":
            stop_dir = "long"
        elif act == "down":
            stop_dir = "short"

    ref_price = _extract_ref_price(daily_cl, intraday_cl)
    daily_tactical_stop = _extract_tactical_stop(daily_cl, stop_dir, ref_price)
    daily_structural_stop = _extract_structural_stop(daily_cl, stop_dir, ref_price)
    daily_reverse_k_watch = _extract_reverse_k_watch(daily_cl, stop_dir, ref_price)
    intraday_tactical_stop = _extract_tactical_stop(intraday_cl, stop_dir, ref_price)
    intraday_reverse_k_watch = _extract_reverse_k_watch(intraday_cl, stop_dir, ref_price)

    if norm_dir is not None:
        opposite = "short" if norm_dir == "long" else "long"
        if daily_bias == opposite:
            aligned = False
            alignment_status = "daily_opposes_direction"
        elif daily_bias == norm_dir and intraday_action == f"entry_{norm_dir}":
            aligned = True
            alignment_status = "aligned_trigger"
        elif daily_bias == norm_dir:
            aligned = False
            alignment_status = "daily_ok_wait_30m"
        else:
            aligned = False
            alignment_status = "neutral_wait"
    else:
        if daily_bias == "long" and intraday_action == "entry_long":
            aligned = True
            alignment_status = "aligned_trigger"
        elif daily_bias == "short" and intraday_action == "entry_short":
            aligned = True
            alignment_status = "aligned_trigger"
        elif daily_bias in ("long", "short"):
            aligned = False
            alignment_status = "daily_ok_wait_30m"
        else:
            aligned = False
            alignment_status = "neutral_wait"

    daily_dq_bad = (
        isinstance(daily_cl, dict)
        and isinstance(daily_cl.get("data_quality"), dict)
        and daily_cl["data_quality"].get("quality_grade") == "degraded"
    ) or daily_sig.get("tier") == "数据质量降级"
    intraday_dq_bad = (
        isinstance(intraday_cl, dict)
        and isinstance(intraday_cl.get("data_quality"), dict)
        and intraday_cl["data_quality"].get("quality_grade") == "degraded"
    ) or intraday_sig.get("tier") == "数据质量降级"

    if daily_dq_bad or intraday_dq_bad:
        aligned = False
        alignment_status = "data_quality_degraded"
        intraday_action = "wait_data_quality"

    noise_reduction_pct: Optional[float] = None
    if ref_price is not None and daily_tactical_stop is not None and intraday_tactical_stop is not None:
        both_below = daily_tactical_stop < ref_price and intraday_tactical_stop < ref_price
        both_above = daily_tactical_stop > ref_price and intraday_tactical_stop > ref_price
        if both_below or both_above:
            daily_dist = abs(ref_price - daily_tactical_stop)
            intraday_dist = abs(ref_price - intraday_tactical_stop)
            if daily_dist > 0:
                raw_pct = (1.0 - intraday_dist / daily_dist) * 100.0
                noise_reduction_pct = round(min(100.0, raw_pct), 2)

    notes: list[str] = []
    if alignment_status == "data_quality_degraded":
        bad_parts = []
        if daily_dq_bad:
            bad_parts.append("日线")
        if intraday_dq_bad:
            bad_parts.append(intraday_tf)
        notes.append(f"{'/'.join(bad_parts)}行情数据质量降级（高比例结算平K或坏点），阻断自动联立放行，请切换高流动性主力合约验证")
    elif alignment_status == "aligned_trigger":
        notes.append(f"大小级别共振：日线定方向({daily_bias}) + {intraday_tf}诞生交易({intraday_action})")
    elif alignment_status == "daily_opposes_direction":
        notes.append(f"日线方向({daily_bias})与目标方向({norm_dir})相悖，严禁逆日线建仓")
    elif alignment_status == "daily_ok_wait_30m":
        if intraday_action == "exhaustion_warning":
            notes.append(f"日线方向({daily_bias})顺向，但{intraday_tf}出现末端追击/非背驰警告({intraday_summary})，耐心等次级别回踩再切入")
        else:
            notes.append(f"日线方向({daily_bias})已确认，等待{intraday_tf}走出明确结构触发点(当前:{intraday_summary})")
    else:
        notes.append(f"日线处于中性/震荡状态({daily_sig.get('summary', '')})，暂不强行开仓")

    if noise_reduction_pct is not None:
        if noise_reduction_pct >= 0:
            notes.append(
                f"止损噪点压缩 {noise_reduction_pct:.2f}% (日线防守 {daily_tactical_stop} -> {intraday_tf}紧凑防守 {intraday_tactical_stop})"
            )
        else:
            notes.append(
                f"{intraday_tf}止损距离宽于日线 (噪点压缩 {noise_reduction_pct:.2f}%，日线防守 {daily_tactical_stop} vs {intraday_tf}防守 {intraday_tactical_stop})"
            )

    return {
        "desired_direction": norm_dir,
        "daily_bias": daily_bias,
        "daily_tier": daily_sig.get("tier", "数据不足"),
        "daily_summary": daily_sig.get("summary", ""),
        "daily_signal_meta": daily_signal_meta,
        "daily_zd": daily_zd,
        "daily_zg": daily_zg,
        "daily_tactical_stop": daily_tactical_stop,
        "daily_structural_stop": daily_structural_stop,
        "daily_reverse_k_watch": daily_reverse_k_watch,
        "intraday_timeframe": intraday_tf,
        "intraday_bias": intraday_bias,
        "intraday_action": intraday_action,
        "intraday_tier": intraday_tier,
        "intraday_summary": intraday_summary,
        "intraday_signal_meta": intraday_signal_meta,
        "intraday_zd": intraday_zd,
        "intraday_zg": intraday_zg,
        "intraday_tactical_stop": intraday_tactical_stop,
        "intraday_reverse_k_watch": intraday_reverse_k_watch,
        "aligned": aligned,
        "alignment_status": alignment_status,
        "noise_reduction_pct": noise_reduction_pct,
        "notes": notes,
    }
