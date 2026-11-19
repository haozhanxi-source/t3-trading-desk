#!/usr/bin/env python3
"""Multi-market snapshot & technical indicator tool for medium/low-frequency research.
Supports US stocks, A-shares, Commodities, Crypto (with perpetual funding rates), and Macro indices.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from chanlun import analyze_chanlun, compute_macd, evaluate_daily_signal_tier

COMMODITY_ALIASES = {
    "GOLD": "GC=F", "XAU": "GC=F", "黄金": "GC=F",
    "SILVER": "SI=F", "XAG": "SI=F", "白银": "SI=F",
    "OIL": "CL=F", "WTI": "CL=F", "原油": "CL=F",
    "BRENT": "BZ=F", "布伦特": "BZ=F",
    "COPPER": "HG=F", "铜": "HG=F",
    "NATGAS": "NG=F", "GAS": "NG=F", "天然气": "NG=F",
    "CORN": "ZC=F", "玉米": "ZC=F",
    "WHEAT": "ZW=F", "小麦": "ZW=F",
    "SOYBEAN": "ZS=F", "大豆": "ZS=F",
}

CRYPTO_BASE = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON", "SUI", "ARB", "OP"}


def normalize_symbol(raw: str):
    s = raw.strip()
    upper = s.upper()
    if s in COMMODITY_ALIASES:
        return COMMODITY_ALIASES[s], "commodity"
    if upper in COMMODITY_ALIASES:
        return COMMODITY_ALIASES[upper], "commodity"
    if upper.endswith("=F"):
        return upper, "commodity"
    if upper.startswith("^") or upper == "DX-Y.NYB":
        return upper, "macro"
    # A-share 6-digit code
    if re.fullmatch(r"\d{6}", upper):
        suffix = ".SS" if upper.startswith(("5", "6", "9")) else ".SZ"
        return f"{upper}{suffix}", "a_share"
    if upper.endswith((".SS", ".SZ", ".BJ")):
        return upper, "a_share"
    # Crypto
    if "/" in upper or upper.endswith("-USD"):
        base = upper.split("/")[0].replace("-USD", "")
        return f"{base}-USD", "crypto"
    if upper in CRYPTO_BASE:
        return f"{upper}-USD", "crypto"
    return upper, "us_stock_or_etf"


def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df else pd.Series(0.0, index=df.index)

    dif, dea, macd_hist = compute_macd(close)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean()

    last_close = float(close.iloc[-1])
    last_atr = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else None

    def ret(n):
        if len(close) > n:
            return round((last_close / float(close.iloc[-1 - n]) - 1) * 100, 2)
        return None

    roll_max_60 = close.tail(60).cummax()
    dd_60 = ((close.tail(60) / roll_max_60) - 1).min() * 100

    vol_20_avg = float(vol.tail(20).mean()) if len(vol) >= 20 else None
    vol_ratio = round(float(vol.iloc[-1]) / vol_20_avg, 2) if vol_20_avg and vol_20_avg > 0 else None

    return {
        "as_of_bar": str(df.index[-1].date()),
        "bars": len(df),
        "last_close": round(last_close, 4),
        "return_1d_pct": ret(1),
        "return_5d_pct": ret(5),
        "return_20d_pct": ret(20),
        "return_60d_pct": ret(60),
        "return_120d_pct": ret(120),
        "max_drawdown_60d_pct": round(float(dd_60), 2),
        "macd_dif": round(float(dif.iloc[-1]), 4),
        "macd_dea": round(float(dea.iloc[-1]), 4),
        "macd_hist": round(float(macd_hist.iloc[-1]), 4),
        "atr14": round(last_atr, 4) if last_atr else None,
        "atr14_pct": round((last_atr / last_close) * 100, 2) if last_atr and last_close else None,
        "high_20d": round(float(high.tail(20).max()), 4),
        "low_20d": round(float(low.tail(20).min()), 4),
        "high_60d": round(float(high.tail(60).max()), 4),
        "low_60d": round(float(low.tail(60).min()), 4),
        "volume_ratio_20d": vol_ratio,
    }


def fetch_crypto_funding(base: str) -> dict:
    perp_sym = f"{base}/USDT:USDT"
    spot_sym = f"{base}/USDT"
    out = {}
    for eid in ["okx", "bybit", "binance"]:
        try:
            ex = getattr(ccxt, eid)({"timeout": 8000})
            ticker = ex.fetch_ticker(spot_sym)
            fr = ex.fetch_funding_rate(perp_sym)
            rate = fr.get("fundingRate")
            ann = round(rate * 3 * 365 * 100, 2) if rate is not None else None
            out[eid] = {
                "spot_last": ticker.get("last"),
                "funding_rate_8h_pct": round(rate * 100, 5) if rate is not None else None,
                "funding_annualized_pct": ann,
            }
        except Exception as e:
            out[eid] = {"error": str(e)[:100]}
    return out


def fetch_macro_context() -> dict:
    ctx = {}
    for sym, label in [("^VIX", "VIX"), ("^TNX", "US10Y_Yield"), ("DX-Y.NYB", "DXY_USD_Index")]:
        try:
            df = yf.Ticker(sym).history(period="10d").dropna(subset=["Close"])
            if len(df) >= 2:
                last = float(df["Close"].iloc[-1])
                prev = float(df["Close"].iloc[-2])
                ctx[label] = {
                    "last": round(last, 2),
                    "chg_1d_pct": round((last / prev - 1) * 100, 2),
                }
        except Exception:
            pass
    return ctx


def get_snapshot(
    raw_symbol: str,
    period: str = "3y",
    include_macro: bool = True,
    include_fundamentals: bool = True,
) -> dict:
    yf_sym, asset_class = normalize_symbol(raw_symbol)
    ticker = yf.Ticker(yf_sym)
    df = ticker.history(period=period, interval="1d")
    if not df.empty:
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return {"error": f"No OHLCV data returned for {raw_symbol} (normalized: {yf_sym})"}

    indicators = compute_indicators(df)
    daily_cl = analyze_chanlun(df, timeframe_label="日线")
    signal_summary = evaluate_daily_signal_tier(daily_cl)

    fundamentals = {}
    if include_fundamentals:
        try:
            info = ticker.info or {}
            fields = [
                "shortName", "sector", "industry", "currency", "marketCap",
                "trailingPE", "forwardPE", "priceToBook", "dividendYield",
                "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
                "returnOnEquity", "debtToEquity", "freeCashflow",
                "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "recommendationKey", "targetMeanPrice",
            ]
            fundamentals = {k: info.get(k) for k in fields if info.get(k) is not None}
        except Exception:
            pass

    result = {
        "query": raw_symbol,
        "symbol": yf_sym,
        "asset_class": asset_class,
        "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "technicals": indicators,
        "chanlun": {
            "signal_summary": signal_summary,
            "daily": daily_cl,
        },
        "fundamentals": fundamentals,
    }

    if asset_class == "crypto":
        base = yf_sym.replace("-USD", "")
        result["crypto_perp_funding"] = fetch_crypto_funding(base)

    if include_macro:
        result["macro_context"] = fetch_macro_context()

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch multi-market snapshot & Chanlun indicators")
    parser.add_argument("symbol", help="Ticker e.g. NVDA, 600519, BTC, GC=F, 黄金, 原油")
    parser.add_argument("--period", default="3y", help="History period (default: 3y)")
    parser.add_argument("--no-macro", action="store_true", help="Skip macro context fetch")
    args = parser.parse_args()
    data = get_snapshot(args.symbol, period=args.period, include_macro=not args.no_macro)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
