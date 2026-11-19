#!/usr/bin/env python3
"""Chanlun (缠论) structural & momentum analysis engine.

Implements:
1. K-line inclusion processing (K线包含处理)
2. Top/bottom fractal detection & confirmation strength (顶底分型及停顿确认)
3. Strict stroke construction (严格笔划分) + live extending stroke tracking
4. Zhongshu / pivot center detection & extension/trend classification (中枢识别与走势分类)
5. Momentum comparison via MACD area/peak/DIF & slope efficiency (力度对比: 资金方向一致 vs 背驰)
6. Chanlun buy/sell point & structural signal classification (一/二/三类买卖点、离开段与中枢震荡)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
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


@dataclass
class Zhongshu:
    zs_id: int
    direction: str  # "up", "down", or "neutral" (direction of entering stroke)
    zd: float
    zg: float
    dd: float
    gg: float
    start_date: str
    end_date: str
    stroke_count: int
    is_extended: bool
    status: str  # "active", "broken_up", "broken_down"


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2.0  # standard Chanlun/A-share MACD bar = 2*(DIF-DEA)
    return dif, dea, hist


def process_inclusion(df: pd.DataFrame) -> list[MergedBar]:
    """Perform standard Chanlun K-line inclusion processing from left to right."""
    if df.empty:
        return []

    highs = df["High"].astype(float).values
    lows = df["Low"].astype(float).values
    opens = df["Open"].astype(float).values if "Open" in df else df["Close"].astype(float).values
    closes = df["Close"].astype(float).values
    vols = df["Volume"].astype(float).values if "Volume" in df else np.zeros(len(df))
    dates = [str(idx.date()) for idx in df.index]

    merged: list[MergedBar] = []
    direction = 1  # default initial assumption until resolved

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
            # Determine direction from previous bar if possible
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
            # Top fractal strength: whether right bar closes below mid.low or left.low
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


def build_pivots(merged: list[MergedBar], fractals: list[Fractal]) -> list[Fractal]:
    """Select strict alternating Chanlun pivots (严格笔顶点)."""
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
                # Ensure no lower bottom violated our previous pivot before replacing
                pivots[-1] = f
            elif f.kind == "bottom" and f.low <= last.low:
                pivots[-1] = f
            continue

        # Opposite fractal: check strict Chanlun stroke rules
        if f.merged_idx - last.merged_idx < 4:
            continue

        lo_idx, hi_idx = last.merged_idx, f.merged_idx
        seg = merged[lo_idx : hi_idx + 1]
        seg_max_high = max(b.high for b in seg)
        seg_min_low = min(b.low for b in seg)

        if last.kind == "bottom" and f.kind == "top":
            if f.high > last.high and f.low > last.low and f.high >= seg_max_high and last.low <= seg_min_low:
                pivots.append(f)
        elif last.kind == "top" and f.kind == "bottom":
            if f.low < last.low and f.high < last.high and f.low <= seg_min_low and last.high >= seg_max_high:
                pivots.append(f)

    return pivots


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
            )
        )

    # Track live/unconfirmed extending stroke after the last confirmed pivot
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
                    )
                )

    return strokes


def detect_zhongshus(strokes: list[Stroke], last_close: float) -> list[Zhongshu]:
    """Detect Zhongshus (中枢) from consecutive strokes.

    A Zhongshu requires 3 consecutive strokes with overlapping range [ZD, ZG] where ZG > ZD.
    Subsequent strokes overlapping [ZD, ZG] extend the Zhongshu until a stroke leaves [ZD, ZG]
    and the following stroke fails to return into [ZD, ZG] (breakout / 3B or 3S).
    """
    zhongshus: list[Zhongshu] = []
    n = len(strokes)
    if n < 3:
        return zhongshus

    i = 0
    zs_id = 1
    while i <= n - 3:
        b1, b2, b3 = strokes[i], strokes[i + 1], strokes[i + 2]
        zg = min(b1.high, b2.high, b3.high)
        zd = max(b1.low, b2.low, b3.low)

        if zg <= zd:
            i += 1
            continue

        enter_dir = strokes[i - 1].direction if i > 0 else "neutral"
        gg = max(b1.high, b2.high, b3.high)
        dd = min(b1.low, b2.low, b3.low)
        start_date = b1.start_date
        end_date = b3.end_date
        count = 3
        j = i + 3
        status = "active"

        while j < n:
            bj = strokes[j]
            overlaps = (bj.low <= zg) and (bj.high >= zd)
            if overlaps:
                gg = max(gg, bj.high)
                dd = min(dd, bj.low)
                end_date = bj.end_date
                count += 1
                j += 1
            else:
                # Left Zhongshu completely; check if next stroke stays outside too
                if bj.low > zg:
                    status = "broken_up"
                elif bj.high < zd:
                    status = "broken_down"
                break

        if status == "active":
            if last_close > zg:
                # Price is currently above ZG, though last stroke still overlapped or just broke out
                pass

        zhongshus.append(
            Zhongshu(
                zs_id=zs_id,
                direction=enter_dir,
                zd=round(zd, 4),
                zg=round(zg, 4),
                dd=round(dd, 4),
                gg=round(gg, 4),
                start_date=start_date,
                end_date=end_date,
                stroke_count=count,
                is_extended=count >= 6,
                status=status,
            )
        )
        zs_id += 1
        i = j

    return zhongshus


def assess_momentum(strokes: list[Stroke]) -> dict:
    """Compare current/latest stroke against prior same-direction stroke.

    Distinguishes:
    - 资金方向一致 (Capital Consensus): new price extreme with non-diminished MACD area/peak/efficiency.
    - 力度衰减 / 背驰候选 (Divergence Candidate): new price extreme with clearly weaker MACD area/peak.
    """
    if not strokes:
        return {"verdict": "insufficient_data"}

    curr = strokes[-1]
    same_dir = [s for s in strokes[:-1] if s.direction == curr.direction]
    if not same_dir:
        return {
            "active_direction": curr.direction,
            "is_confirmed_stroke": curr.is_confirmed,
            "verdict": "single_stroke_no_comparison",
            "is_capital_consensus": False,
            "is_divergence_candidate": False,
        }

    prev = same_dir[-1]
    area_ratio = round(curr.macd_area / prev.macd_area, 2) if prev.macd_area > 0 else None
    peak_ratio = round(curr.macd_peak / prev.macd_peak, 2) if prev.macd_peak > 0 else None
    slope_ratio = round(curr.efficiency / prev.efficiency, 2) if prev.efficiency > 0 else None

    if curr.direction == "down":
        made_new_extreme = curr.end_price < prev.end_price
        dif_new_extreme = curr.dif_extreme <= prev.dif_extreme
    else:
        made_new_extreme = curr.end_price > prev.end_price
        dif_new_extreme = curr.dif_extreme >= prev.dif_extreme

    is_consensus = False
    is_divergence = False

    if made_new_extreme and area_ratio is not None and peak_ratio is not None:
        if area_ratio >= 0.85 or peak_ratio >= 0.90 or (slope_ratio is not None and slope_ratio >= 1.15 and peak_ratio >= 0.75):
            is_consensus = True
            verdict = "capital_consensus_down" if curr.direction == "down" else "capital_consensus_up"
        elif area_ratio < 0.78 and (peak_ratio < 0.88 or not dif_new_extreme):
            is_divergence = True
            verdict = "divergence_candidate_down" if curr.direction == "down" else "divergence_candidate_up"
        else:
            verdict = "neutral_extension_down" if curr.direction == "down" else "neutral_extension_up"
    else:
        if curr.direction == "down":
            verdict = "higher_low_pullback"  # did not break prior low
        else:
            verdict = "lower_high_rebound"  # did not break prior high

    return {
        "active_direction": curr.direction,
        "is_confirmed_stroke": curr.is_confirmed,
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
) -> tuple[str, str, list[str]]:
    """Produce macro structure label, price position vs Zhongshu, and actionable Chanlun signals."""
    signals: list[str] = []

    # 1. Macro structure from Zhongshu sequence
    if len(zhongshus) >= 2:
        z_prev, z_curr = zhongshus[-2], zhongshus[-1]
        if z_curr.zd > z_prev.zg:
            macro_structure = "双中枢上涨趋势"
        elif z_curr.zg < z_prev.zd:
            macro_structure = "双中枢下跌趋势"
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

    # 3. Latest fractal freshness
    latest_frac = fractals[-1] if fractals else None
    frac_just_confirmed = (
        latest_frac is not None and len(merged) > 0 and (len(merged) - 1 - latest_frac.confirmed_by_idx) <= 1
    )

    curr = strokes[-1] if strokes else None
    prev_same = [s for s in strokes[:-1] if curr and s.direction == curr.direction]

    # 4. Third Buy / Third Sell relative to latest Zhongshu
    if latest_zs and len(strokes) >= 2:
        if curr and curr.direction == "down":
            prev_up = strokes[-2]
            if prev_up.direction == "up" and prev_up.high > latest_zs.zg and curr.low > latest_zs.zg:
                if frac_just_confirmed and latest_frac.kind == "bottom":
                    signals.append(f"{timeframe_label}三买确认(回踩不破ZG={latest_zs.zg}+底分型)")
                else:
                    signals.append(f"{timeframe_label}准三买观察(回踩ZG={latest_zs.zg}上方)")
            elif not curr.is_confirmed and len(strokes) >= 3:
                prev_rebound = strokes[-2]
                prev_break = strokes[-3]
                if prev_rebound.direction == "up" and prev_break.direction == "down" and prev_break.low < latest_zs.zd and prev_rebound.high < latest_zs.zd:
                    signals.append(f"{timeframe_label}三卖确认(反抽不过ZD={latest_zs.zd}·顶分型下行)")
        elif curr and curr.direction == "up":
            prev_dn = strokes[-2]
            if prev_dn.direction == "down" and prev_dn.low < latest_zs.zd and curr.high < latest_zs.zd:
                if frac_just_confirmed and latest_frac.kind == "top":
                    signals.append(f"{timeframe_label}三卖确认(反抽不过ZD={latest_zs.zd}+顶分型)")
                else:
                    signals.append(f"{timeframe_label}准三卖观察(反抽受阻于ZD={latest_zs.zd}下方)")
            elif not curr.is_confirmed and len(strokes) >= 3:
                prev_pullback = strokes[-2]
                prev_break = strokes[-3]
                if prev_pullback.direction == "down" and prev_break.direction == "up" and prev_break.high > latest_zs.zg and prev_pullback.low > latest_zs.zg:
                    signals.append(f"{timeframe_label}三买确认(回踩不破ZG={latest_zs.zg}·底分型上攻)")

    # 5. Capital Consensus vs First Buy/Sell Divergence
    verdict = momentum.get("verdict")
    if verdict == "capital_consensus_down":
        ar = momentum.get("area_ratio_vs_prev")
        pr = momentum.get("peak_ratio_vs_prev")
        if latest_zs and last_close < latest_zs.zd:
            signals.append(f"{timeframe_label}向下离开段·资金方向一致(area={ar}x,peak={pr}x·未背驰勿盲目抄底)")
        else:
            signals.append(f"{timeframe_label}空头资金方向一致推进(area={ar}x,peak={pr}x·未见背驰)")
    elif verdict == "capital_consensus_up":
        ar = momentum.get("area_ratio_vs_prev")
        pr = momentum.get("peak_ratio_vs_prev")
        if latest_zs and last_close > latest_zs.zg:
            signals.append(f"{timeframe_label}向上离开段·资金方向一致(area={ar}x,peak={pr}x·趋势延续)")
        else:
            signals.append(f"{timeframe_label}多头资金方向一致推进(area={ar}x,peak={pr}x·动能充沛)")
    elif verdict == "divergence_candidate_down":
        ar = momentum.get("area_ratio_vs_prev")
        kind_label = "趋势背驰" if macro_structure == "双中枢下跌趋势" else "盘整背驰"
        if frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
            signals.append(f"{timeframe_label}{kind_label}+底分型确认(一买候选·area={ar}x)")
        elif pivots and pivots[-1].kind == "bottom" and (len(merged) - 1 - pivots[-1].confirmed_by_idx) <= 3:
            signals.append(f"{timeframe_label}{kind_label}+底分型结构(一买候选·area={ar}x)")
        else:
            signals.append(f"{timeframe_label}下跌力度衰减(疑似{kind_label}·area={ar}x·待底分型确认)")
    elif verdict == "divergence_candidate_up":
        ar = momentum.get("area_ratio_vs_prev")
        kind_label = "趋势背驰" if macro_structure == "双中枢上涨趋势" else "盘整背驰"
        if frac_just_confirmed and latest_frac and latest_frac.kind == "top":
            signals.append(f"{timeframe_label}{kind_label}+顶分型确认(一卖候选·area={ar}x)")
        else:
            signals.append(f"{timeframe_label}上涨力度衰减(疑似{kind_label}·area={ar}x·观察顶分型)")

    # 6. Fresh reversal launch right after completed stroke + Second Buy/Sell
    if curr and not curr.is_confirmed and curr.raw_bars <= 4 and len(strokes) >= 4:
        last_opp = strokes[-2]
        prev_opp = strokes[-4]
        if curr.direction == "up" and last_opp.direction == "down" and prev_opp.direction == "down":
            if last_opp.end_price < prev_opp.end_price and prev_opp.macd_area > 0:
                ar = round(last_opp.macd_area / prev_opp.macd_area, 2)
                if ar < 0.78:
                    kind_label = "趋势背驰" if macro_structure == "双中枢下跌趋势" else "盘整背驰"
                    signals.append(f"{timeframe_label}{kind_label}+底分型启动(一买/盘背·area={ar}x)")
            elif last_opp.end_price > prev_opp.end_price and frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
                signals.append(f"{timeframe_label}二买/类二买确认(回踩不破前低{prev_opp.end_price}+底分型启动)")
        elif curr.direction == "down" and last_opp.direction == "up" and prev_opp.direction == "up":
            if last_opp.end_price > prev_opp.end_price and prev_opp.macd_area > 0:
                ar = round(last_opp.macd_area / prev_opp.macd_area, 2)
                if ar < 0.78:
                    kind_label = "趋势背驰" if macro_structure == "双中枢上涨趋势" else "盘整背驰"
                    signals.append(f"{timeframe_label}{kind_label}+顶分型启动(一卖/盘背·area={ar}x)")
            elif last_opp.end_price < prev_opp.end_price and frac_just_confirmed and latest_frac and latest_frac.kind == "top":
                signals.append(f"{timeframe_label}二卖/类二卖确认(反弹不过前高{prev_opp.end_price}+顶分型启动)")

    if curr and prev_same:
        prev_s = prev_same[-1]
        if curr.direction == "down" and curr.end_price > prev_s.end_price:
            if frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
                signals.append(f"{timeframe_label}二买/类二买确认(回踩不破前低{prev_s.end_price}+底分型)")
            elif latest_zs and curr.end_price >= latest_zs.zd:
                signals.append(f"{timeframe_label}中枢内次低点蓄势(不破前低{prev_s.end_price})")
        elif curr.direction == "up" and curr.end_price < prev_s.end_price:
            if frac_just_confirmed and latest_frac and latest_frac.kind == "top":
                signals.append(f"{timeframe_label}二卖/类二卖确认(反弹不过前高{prev_s.end_price}+顶分型)")

    # 7. Zhongshu edge signals when inside Zhongshu
    if latest_zs and pos_vs_zs.startswith("inside_zs"):
        span = latest_zs.zg - latest_zs.zd
        rel = (last_close - latest_zs.zd) / span if span > 0 else 0.5
        if rel <= 0.25 and frac_just_confirmed and latest_frac and latest_frac.kind == "bottom":
            signals.append(f"{timeframe_label}中枢下沿ZD={latest_zs.zd}底分型企稳")
        elif rel >= 0.75 and frac_just_confirmed and latest_frac and latest_frac.kind == "top":
            signals.append(f"{timeframe_label}中枢上沿ZG={latest_zs.zg}顶分型承压")

    if not signals:
        if pos_vs_zs.startswith("inside_zs"):
            signals.append(f"{timeframe_label}中枢震荡中([ZD={latest_zs.zd}, ZG={latest_zs.zg}])")
        elif curr:
            dir_zh = "向上" if curr.direction == "up" else "向下"
            signals.append(f"{timeframe_label}{dir_zh}笔常规运行中")
        else:
            signals.append(f"{timeframe_label}结构样本不足")

    return macro_structure, pos_vs_zs, signals


def analyze_chanlun(df: pd.DataFrame, timeframe_label: str = "日线") -> dict:
    """Run full Chanlun analysis on an OHLCV DataFrame."""
    if not df.empty:
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty or len(df) < 15:
        return {"error": f"Insufficient bars ({len(df)}) for Chanlun analysis"}

    close = df["Close"].astype(float)
    last_close = float(close.iloc[-1])
    dif, dea, hist = compute_macd(close)

    merged = process_inclusion(df)
    fractals = detect_fractals(merged)
    pivots = build_pivots(merged, fractals)
    strokes = build_strokes(df, merged, pivots, dif, hist)
    zhongshus = detect_zhongshus(strokes, last_close)
    momentum = assess_momentum(strokes)

    macro_structure, pos_vs_zs, signals = classify_chanlun_signals(
        timeframe_label=timeframe_label,
        last_close=last_close,
        merged=merged,
        fractals=fractals,
        pivots=pivots,
        strokes=strokes,
        zhongshus=zhongshus,
        momentum=momentum,
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

    return {
        "timeframe": timeframe_label,
        "raw_bars": len(df),
        "merged_bars": len(merged),
        "macro_structure": macro_structure,
        "price_vs_zhongshu": pos_vs_zs,
        "latest_fractal": latest_frac,
        "latest_zhongshu": asdict(zhongshus[-1]) if zhongshus else None,
        "zhongshu_count": len(zhongshus),
        "recent_strokes": [asdict(s) for s in strokes[-5:]],
        "momentum_assessment": momentum,
        "signals": signals,
    }


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV DataFrame to weekly bars (week ending Friday)."""
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    }
    if "Volume" in df.columns:
        agg["Volume"] = "sum"
    weekly = df.resample("W-FRI").agg(agg).dropna(subset=["Open", "High", "Low", "Close"])
    return weekly


def evaluate_mtf_resonance(weekly: dict, daily: dict) -> dict:
    """Combine Weekly and Daily Chanlun structures into a multi-timeframe (区间套) reading."""
    if "error" in weekly or "error" in daily:
        return {"resonance": "单周期结构", "summary": daily.get("signals", ["数据不足"])[0]}

    w_mom = weekly.get("momentum_assessment", {})
    d_mom = daily.get("momentum_assessment", {})
    w_dir = w_mom.get("active_direction")
    d_dir = d_mom.get("active_direction")

    w_cons = w_mom.get("is_capital_consensus", False)
    d_cons = d_mom.get("is_capital_consensus", False)
    w_div = w_mom.get("is_divergence_candidate", False)
    d_div = d_mom.get("is_divergence_candidate", False)

    d_sigs_str = " ".join(daily.get("signals", []))
    w_frac = weekly.get("latest_fractal") or {}

    if w_dir == "down" and d_dir == "down" and (w_cons or d_cons) and not d_div:
        resonance = "周日共振向下·资金方向一致"
        action_bias = "顺势空头/持币观望，严禁逆势接飞刀"
        tier = "资金一致推进(勿逆势)"
    elif w_dir == "up" and d_dir == "up" and (w_cons or d_cons) and not d_div:
        resonance = "周日共振向上·资金方向一致"
        action_bias = "主升/向上离开段延续，持仓观察或顺势跟进"
        tier = "资金一致推进(顺势)"
    elif "三买" in d_sigs_str and w_dir == "up":
        resonance = "周线多头+日线三买共振"
        action_bias = "中低频高胜率顺势买点观察区"
        tier = "买卖点信号"
    elif "三卖" in d_sigs_str and w_dir == "down":
        resonance = "周线空头+日线三卖共振"
        action_bias = "中低频顺势减仓/做空观察区"
        tier = "买卖点信号"
    elif d_div and d_dir == "down" and (w_div or (w_frac.get("kind") == "bottom" and w_frac.get("confirmed_merged_bars_ago", 99) <= 2) or "above_zg" in weekly.get("price_vs_zhongshu", "")):
        resonance = "大级别支撑/减速+日线底背驰(区间套)"
        action_bias = "关注日线底分型确认后的一买/盘背买点"
        tier = "区间套买卖点"
    elif d_div and d_dir == "up" and (w_div or (w_frac.get("kind") == "top" and w_frac.get("confirmed_merged_bars_ago", 99) <= 2)):
        resonance = "周线承压/减速+日线顶背驰(区间套)"
        action_bias = "注意止盈与减仓，观察日线顶分型确认"
        tier = "区间套买卖点"
    elif "二买" in d_sigs_str or "三买" in d_sigs_str or "一买" in d_sigs_str or "二卖" in d_sigs_str or "三卖" in d_sigs_str or "一卖" in d_sigs_str:
        resonance = "日线买卖点结构触发"
        action_bias = "结合周线中枢位置控制仓位与止损"
        tier = "买卖点信号"
    elif d_div:
        resonance = "日线力度背驰观察"
        action_bias = "等待分型停顿确认，勿提前抢跑"
        tier = "背驰观察"
    elif d_cons:
        dir_label = "向下" if d_dir == "down" else "向上"
        resonance = f"日线{dir_label}资金方向一致(周线缓冲)"
        action_bias = "尊重当前日线主导资金方向，等待结构转折信号"
        tier = "资金一致推进"
    else:
        resonance = "周日结构分化/中枢震荡"
        action_bias = "按中枢下沿支撑/上沿阻力高抛低吸或观望"
        tier = "中枢震荡"

    return {
        "tier": tier,
        "resonance": resonance,
        "action_bias": action_bias,
    }
