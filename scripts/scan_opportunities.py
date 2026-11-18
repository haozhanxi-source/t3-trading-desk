#!/usr/bin/env python3
"""Scan watchlist across US stocks, A-shares, Commodities, and Crypto for medium/low-frequency setups."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from market_snapshot import get_snapshot


def classify_signals(snap: dict) -> list[str]:
    if "error" in snap:
        return [f"ERROR: {snap['error']}"]
    t = snap["technicals"]
    signals = []
    rsi = t.get("rsi14")
    regime = t.get("trend_regime")
    d20 = t.get("dist_ema20_pct", 0)
    d200 = t.get("dist_ema200_pct", 0)
    last = t.get("last_close")
    h20 = t.get("high_20d")
    l20 = t.get("low_20d")

    if regime in ("strong_uptrend", "uptrend") and -3.5 <= d20 <= 1.5 and rsi is not None and rsi < 55:
        signals.append("上升趋势回踩EMA20关注区")
    if d200 > 0 and rsi is not None and rsi <= 38:
        signals.append("多头结构超卖回调(RSI<=38)")
    if rsi is not None and rsi <= 30:
        signals.append("极度超卖(RSI<=30)")
    elif rsi is not None and rsi >= 72:
        signals.append("短期超买/注意止盈风险(RSI>=72)")
    if last and h20 and last >= h20 * 0.995:
        signals.append("创20日新高/突破观察")
    if last and l20 and last <= l20 * 1.005:
        signals.append("测试20日低点支撑")

    funding = snap.get("crypto_perp_funding", {})
    for ex, info in funding.items():
        ann = info.get("funding_annualized_pct")
        if ann is not None and (ann >= 15 or ann <= -5):
            signals.append(f"{ex}永续年化资金费率异动({ann}%)")

    return signals or ["常规震荡/无极端信号"]


def main():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "watchlist.json"), "r", encoding="utf-8") as f:
        wl = json.load(f)

    rows = []
    for bucket in ["us_stocks", "a_shares", "commodities", "crypto"]:
        for sym in wl.get(bucket, []):
            snap = get_snapshot(sym, period="1y", include_macro=False)
            if "error" in snap:
                rows.append({"market": bucket, "symbol": sym, "error": snap["error"]})
                continue
            t = snap["technicals"]
            rows.append({
                "market": bucket,
                "symbol": snap["symbol"],
                "close": t["last_close"],
                "ret_5d_%": t["return_5d_pct"],
                "ret_20d_%": t["return_20d_pct"],
                "rsi14": t["rsi14"],
                "atr_%": t["atr14_pct"],
                "regime": t["trend_regime"],
                "signals": classify_signals(snap),
            })

    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
