#!/usr/bin/env python3
"""Multi-market snapshot & technical indicator tool for medium/low-frequency research.
Supports US stocks, A-shares, Commodities, Crypto (with perpetual funding rates), and Macro indices.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None
import ccxt
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from chanlun import analyze_chanlun, compute_macd, evaluate_daily_signal_tier

CN_FUTURE_RE = re.compile(r"^[A-Z]{1,2}0$")

COMMODITY_ALIASES = {
    # Overseas futures (yfinance)
    "GOLD": "GC=F", "XAU": "GC=F", "黄金": "GC=F",
    "SILVER": "SI=F", "XAG": "SI=F", "白银": "SI=F",
    "PLATINUM": "PL=F", "铂金": "PL=F",
    "PALLADIUM": "PA=F", "钯金": "PA=F",
    "OIL": "CL=F", "WTI": "CL=F", "原油": "CL=F",
    "BRENT": "BZ=F", "布伦特": "BZ=F",
    "COPPER": "HG=F", "铜": "HG=F",
    "ALUMINUM": "ALI=F", "ALUMINIUM": "ALI=F", "铝": "ALI=F",
    "NATGAS": "NG=F", "GAS": "NG=F", "天然气": "NG=F",
    "CORN": "ZC=F", "玉米": "ZC=F",
    "WHEAT": "ZW=F", "小麦": "ZW=F",
    "SOYBEAN": "ZS=F", "大豆": "ZS=F",
    "COFFEE": "KC=F", "咖啡": "KC=F",
    "COCOA": "CC=F", "可可": "CC=F",
    "SUGAR": "SB=F", "原糖": "SB=F",
    "COTTON": "CT=F", "美棉": "CT=F",
    "LIVE_CATTLE": "LE=F", "活牛": "LE=F",
    "LEAN_HOGS": "HE=F", "瘦肉猪": "HE=F",
    # Domestic futures (akshare / sina 主力连续)
    "铁矿": "I0", "铁矿石": "I0",
    "螺纹": "RB0", "螺纹钢": "RB0",
    "热卷": "HC0",
    "焦炭": "J0",
    "焦煤": "JM0",
    "硅铁": "SF0",
    "锰硅": "SM0",
    "碳酸锂": "LC0",
    "工业硅": "SI0",
    "多晶硅": "PS0",
    "生猪": "LH0",
    "豆粕": "M0",
    "豆油": "Y0",
    "棕榈油": "P0",
    "菜油": "OI0",
    "白糖": "SR0",
    "棉花": "CF0",
    "PTA期货": "TA0", "精对苯二甲酸": "TA0",
    "甲醇": "MA0",
    "PVC期货": "V0", "聚氯乙烯": "V0",
    "纯碱": "SA0",
    "玻璃": "FG0",
    "橡胶": "RU0",
    "沪铜": "CU0",
    "沪铝": "AL0",
    "沪锌": "ZN0",
    "沪镍": "NI0",
    "沪锡": "SN0",
    "沪金": "AU0",
    "沪银": "AG0",
    "集运欧线": "EC0",
    "上海原油": "SC0",
}

CRYPTO_BASE = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON", "SUI", "ARB", "OP"}


CN_PREFIX_ALIASES = {
    "PTA": "TA0",
    "PVC": "V0",
}

FX_ALIASES = {
    "DXY": "DX-Y.NYB", "美元指数": "DX-Y.NYB",
    "EURUSD": "EURUSD=X", "欧元美元": "EURUSD=X", "欧元": "EURUSD=X",
    "USDJPY": "USDJPY=X", "美元日元": "USDJPY=X", "日元": "USDJPY=X",
    "GBPUSD": "GBPUSD=X", "英镑美元": "GBPUSD=X", "英镑": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X", "澳元美元": "AUDUSD=X", "澳元": "AUDUSD=X",
    "USDCNY": "USDCNY=X", "美元人民币": "USDCNY=X", "人民币": "USDCNY=X",
    "USDCAD": "USDCAD=X", "美元加元": "USDCAD=X", "加元": "USDCAD=X",
}


def normalize_symbol(raw: str):
    s = raw.strip()
    upper = s.upper()
    if upper.startswith("US:") and len(upper) > 3:
        return upper[3:].strip(), "us_stock_or_etf"
    if upper.startswith("CN:") and len(upper) > 3:
        cn_key = s[3:].strip()
        cn_upper = cn_key.upper()
        if cn_key in COMMODITY_ALIASES:
            return COMMODITY_ALIASES[cn_key], "cn_future"
        if cn_upper in COMMODITY_ALIASES:
            return COMMODITY_ALIASES[cn_upper], "cn_future"
        if cn_upper in CN_PREFIX_ALIASES:
            return CN_PREFIX_ALIASES[cn_upper], "cn_future"
        if CN_FUTURE_RE.fullmatch(cn_upper):
            return cn_upper, "cn_future"
    if s in FX_ALIASES:
        target = FX_ALIASES[s]
        return target, ("macro" if target == "DX-Y.NYB" else "fx")
    if upper in FX_ALIASES:
        target = FX_ALIASES[upper]
        return target, ("macro" if target == "DX-Y.NYB" else "fx")
    if s in COMMODITY_ALIASES:
        target = COMMODITY_ALIASES[s]
        asset_class = "cn_future" if CN_FUTURE_RE.fullmatch(target) else "commodity"
        return target, asset_class
    if upper in COMMODITY_ALIASES:
        target = COMMODITY_ALIASES[upper]
        asset_class = "cn_future" if CN_FUTURE_RE.fullmatch(target) else "commodity"
        return target, asset_class
    if upper.endswith("=F"):
        return upper, "commodity"
    if upper.endswith("=X"):
        return upper, "fx"
    if upper.startswith("^") or upper == "DX-Y.NYB":
        return upper, "macro"
    # A-share 6-digit code
    if re.fullmatch(r"\d{6}", upper):
        if upper.startswith(("4", "8", "92")):
            suffix = ".BJ"
        elif upper.startswith(("5", "6", "9")):
            suffix = ".SS"
        else:
            suffix = ".SZ"
        return f"{upper}{suffix}", "a_share"
    if upper.endswith((".SS", ".SZ", ".BJ")):
        return upper, "a_share"
    # Crypto
    if "/" in upper or upper.endswith("-USD"):
        base = upper.split("/")[0].replace("-USD", "")
        return f"{base}-USD", "crypto"
    if upper in CRYPTO_BASE:
        return f"{upper}-USD", "crypto"
    # Domestic futures continuous main contract (e.g., I0, RB0, TA0, V0, M0)
    if CN_FUTURE_RE.fullmatch(upper):
        return upper, "cn_future"
    return upper, "us_stock_or_etf"


def period_to_start_date(period: str, today: date | datetime | str | None = None) -> str:
    """Convert yfinance-style period string (e.g. 6mo, 1y, 3y, 5y) to YYYYMMDD start date."""
    if today is None:
        anchor = pd.Timestamp(datetime.now().date())
    else:
        anchor = pd.Timestamp(today).normalize()

    p = (period or "3y").strip().lower()
    if p == "max":
        return "19900101"
    if p == "ytd":
        return f"{anchor.year}0101"

    m = re.fullmatch(r"(\d+)\s*(d|w|mo|y)", p)
    if not m:
        raise ValueError(f"Unsupported period format: {period!r}")

    qty = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        start = anchor - pd.Timedelta(days=qty)
    elif unit == "w":
        start = anchor - pd.Timedelta(weeks=qty)
    elif unit == "mo":
        start = anchor - pd.DateOffset(months=qty)
    else:  # "y"
        start = anchor - pd.DateOffset(years=qty)
    return start.strftime("%Y%m%d")


def normalize_cn_future_frame(raw_df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize akshare futures DataFrame (Chinese or English columns) into standard OHLCV DataFrame."""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=cols)

    df = raw_df.copy()
    col_map = {
        "日期": "Date",
        "开盘价": "Open",
        "最高价": "High",
        "最低价": "Low",
        "收盘价": "Close",
        "成交量": "Volume",
        "date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }

    renamed = {}
    for c in df.columns:
        c_str = str(c).strip()
        if c_str in col_map:
            renamed[c] = col_map[c_str]
        elif c_str.lower() in col_map:
            renamed[c] = col_map[c_str.lower()]
    df = df.rename(columns=renamed)

    def _parse_date_series(values) -> pd.DatetimeIndex:
        s_val = pd.Series(values)
        if pd.api.types.is_integer_dtype(s_val) or pd.api.types.is_float_dtype(s_val):
            as_str = s_val.dropna().astype(int).astype(str)
            if as_str.empty or not as_str.str.fullmatch(r"\d{8}").all():
                return pd.DatetimeIndex([pd.NaT] * len(s_val))
            return pd.DatetimeIndex(pd.to_datetime(s_val.astype("Int64").astype(str), format="%Y%m%d", errors="coerce"))
        return pd.DatetimeIndex(pd.to_datetime(s_val, errors="coerce"))

    if "Date" in df.columns:
        parsed_dates = _parse_date_series(df["Date"])
        df = df.assign(Date=parsed_dates).dropna(subset=["Date"]).set_index("Date")
    elif isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
        df = df[df.index.notna()]
        df.index.name = "Date"
    elif not isinstance(df.index, pd.RangeIndex):
        parsed_idx = _parse_date_series(df.index)
        if parsed_idx.notna().any():
            df.index = parsed_idx
            df = df[df.index.notna()]
            df.index.name = "Date"
        else:
            return pd.DataFrame(columns=cols)
    else:
        return pd.DataFrame(columns=cols)

    for required in ["Open", "High", "Low", "Close"]:
        if required not in df.columns:
            return pd.DataFrame(columns=cols)
        df[required] = pd.to_numeric(df[required], errors="coerce")

    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).astype(float)
    else:
        df["Volume"] = 0.0

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return pd.DataFrame(columns=cols)

    for c in cols:
        df[c] = df[c].astype(float)

    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index(ascending=True, kind="mergesort")
    return df[cols]


def fetch_cn_future_daily(symbol: str, period: str = "3y") -> pd.DataFrame:
    """Fetch domestic Chinese futures continuous main contract daily OHLCV via akshare."""
    if ak is None:
        raise RuntimeError("akshare is not installed; required for domestic futures (cn_future)")
    start_date = period_to_start_date(period)
    end_date = datetime.now().strftime("%Y%m%d")
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)

    last_err: Exception | None = None
    try:
        raw = ak.futures_main_sina(symbol=symbol, start_date=start_date, end_date=end_date)
        df = normalize_cn_future_frame(raw)
        if not df.empty:
            df = df[(df.index >= start_ts) & (df.index <= end_ts)]
        if not df.empty:
            return df
    except Exception as exc:
        last_err = exc

    try:
        raw_fb = ak.futures_zh_daily_sina(symbol=symbol)
        df_fb = normalize_cn_future_frame(raw_fb)
        if not df_fb.empty:
            df_fb = df_fb[(df_fb.index >= start_ts) & (df_fb.index <= end_ts)]
        if not df_fb.empty:
            return df_fb
    except Exception as exc:
        last_err = exc

    if last_err is not None:
        raise last_err
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


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
    try:
        norm_sym, asset_class = normalize_symbol(raw_symbol)
        fundamentals = {}
        ticker = None

        if asset_class == "cn_future":
            data_source = "akshare/sina 主力连续"
            df = fetch_cn_future_daily(norm_sym, period=period)
        else:
            data_source = "yfinance"
            ticker = yf.Ticker(norm_sym)
            df = ticker.history(period=period, interval="1d")

        if df is not None and not df.empty:
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df is None or df.empty:
            return {"error": f"No OHLCV data returned for {raw_symbol} (normalized: {norm_sym})"}

        indicators = compute_indicators(df)
        daily_cl = analyze_chanlun(df, timeframe_label="日线")
        signal_summary = evaluate_daily_signal_tier(daily_cl)

        if include_fundamentals and asset_class not in ("cn_future", "fx") and ticker is not None:
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
            "symbol": norm_sym,
            "asset_class": asset_class,
            "data_source": data_source,
            "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "technicals": indicators,
            "chanlun": {
                "signal_summary": signal_summary,
                "daily": daily_cl,
            },
            "fundamentals": fundamentals,
        }

        if asset_class == "crypto":
            base = norm_sym.replace("-USD", "")
            result["crypto_perp_funding"] = fetch_crypto_funding(base)

        if include_macro:
            result["macro_context"] = fetch_macro_context()

        return result
    except Exception as exc:
        return {"error": f"Failed to fetch snapshot for {raw_symbol}: {exc}"}


def main():
    parser = argparse.ArgumentParser(description="Fetch multi-market snapshot & Chanlun indicators")
    parser.add_argument("symbol", help="Ticker e.g. NVDA, 600519, BTC, GC=F, I0, 铁矿, 黄金, 原油")
    parser.add_argument("--period", default="3y", help="History period (default: 3y)")
    parser.add_argument("--no-macro", action="store_true", help="Skip macro context fetch")
    args = parser.parse_args()
    data = get_snapshot(args.symbol, period=args.period, include_macro=not args.no_macro)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
