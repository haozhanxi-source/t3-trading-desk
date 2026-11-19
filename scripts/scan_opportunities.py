#!/usr/bin/env python3
"""Scan watchlist across US stocks, A-shares, Commodities, and Crypto using Daily Chanlun structure & momentum."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from market_snapshot import get_snapshot


def format_fractal_label(frac: dict | None) -> str:
    if not frac:
        return "无"
    kind_zh = "底分型" if frac["kind"] == "bottom" else "顶分型"
    fresh = "刚确认" if frac.get("just_confirmed") else f"{frac.get('confirmed_merged_bars_ago')}K前"
    return f"{kind_zh}({fresh}/{frac.get('strength', 'standard')})"


def format_momentum_label(mom: dict) -> str:
    verdict = mom.get("verdict", "insufficient_data")
    ar = mom.get("area_ratio_vs_prev")
    pr = mom.get("peak_ratio_vs_prev")
    ratio_str = f"area={ar}x,peak={pr}x" if ar is not None else "首笔样本"

    verdict_map = {
        "capital_consensus_down": "空头资金方向一致",
        "capital_consensus_up": "多头资金方向一致",
        "divergence_candidate_down": "下跌力度衰减(疑似底背驰)",
        "divergence_candidate_up": "上涨力度衰减(疑似顶背驰)",
        "higher_low_pullback": "回踩不破前低",
        "lower_high_rebound": "反弹不过前高",
        "neutral_extension_down": "向下笔延伸(力度中性)",
        "neutral_extension_up": "向上笔延伸(力度中性)",
    }
    label = verdict_map.get(verdict, verdict)
    return f"{label} ({ratio_str})"


def collect_signals(snap: dict) -> list[str]:
    if "error" in snap:
        return [f"ERROR: {snap['error']}"]

    cl = snap.get("chanlun", {})
    daily = cl.get("daily", {})
    summary = cl.get("signal_summary", {})

    signals = []
    if summary.get("summary"):
        signals.append(f"【{summary['summary']}】")

    signals.extend(daily.get("signals", []))

    funding = snap.get("crypto_perp_funding", {})
    for ex, info in funding.items():
        ann = info.get("funding_annualized_pct")
        if ann is not None and (ann >= 15 or ann <= -5):
            side = "多头杠杆拥挤" if ann >= 15 else "空头资金费率一致看空"
            signals.append(f"{ex}永续年化费率{ann}%({side})")

    return signals or ["常规日线结构运行"]


def main():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "watchlist.json"), "r", encoding="utf-8") as f:
        wl = json.load(f)

    rows = []
    for bucket in ["us_stocks", "a_shares", "commodities", "crypto"]:
        for sym in wl.get(bucket, []):
            snap = get_snapshot(sym, period="3y", include_macro=False, include_fundamentals=False)
            if "error" in snap:
                rows.append({"market": bucket, "symbol": sym, "error": snap["error"]})
                continue

            t = snap["technicals"]
            cl = snap["chanlun"]
            daily = cl["daily"]
            summary = cl["signal_summary"]
            zs = daily.get("latest_zhongshu")

            rows.append({
                "market": bucket,
                "symbol": snap["symbol"],
                "close": t["last_close"],
                "ret_5d_%": t["return_5d_pct"],
                "ret_20d_%": t["return_20d_pct"],
                "ret_60d_%": t["return_60d_pct"],
                "atr_%": t["atr14_pct"],
                "signal_tier": summary.get("tier"),
                "daily_structure": daily.get("macro_structure"),
                "daily_zs_count": daily.get("zhongshu_count"),
                "daily_zs": [zs["zd"], zs["zg"]] if zs else None,
                "pos_vs_zs": daily.get("price_vs_zhongshu"),
                "daily_fractal": format_fractal_label(daily.get("latest_fractal")),
                "daily_momentum": format_momentum_label(daily.get("momentum_assessment", {})),
                "action_bias": summary.get("action_bias"),
                "signals": collect_signals(snap),
            })

    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
