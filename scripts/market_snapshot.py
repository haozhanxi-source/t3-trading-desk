#!/usr/bin/env python3
"""Multi-market snapshot & technical indicator tool for medium/low-frequency research.
Supports US stocks, A-shares, Commodities, Crypto (with perpetual funding rates), and Macro indices.
"""
import argparse
import inspect
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None
import ccxt
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from chanlun import (
    _format_bar_timestamps,
    analyze_chanlun,
    assess_dual_timeframe_alignment,
    compute_macd,
    evaluate_daily_signal_tier,
    evaluate_signal_tier,
)
from data_store import (
    SeriesKey,
    get_store,
    is_store_enabled,
    period_to_days,
    sanitize_and_grade_ohlcv,
    scoped_http_timeout,
)

CN_FUTURE_RE = re.compile(r"^[A-Z]{1,2}0$")
CN_FUTURE_ROOTS = frozenset({
    "CU", "AL", "ZN", "NI", "SN", "PB", "AU", "AG",
    "RB", "HC", "I", "J", "JM", "SF", "SM",
    "LC", "SI", "PS", "LH",
    "M", "Y", "P", "OI", "SR", "CF", "C",
    "TA", "MA", "V", "SA", "FG", "RU", "FU", "PG",
    "EC", "SC",
})
CN_FUTURE_CONTRACT_RE = re.compile(
    r"^(" + "|".join(sorted(CN_FUTURE_ROOTS, key=len, reverse=True)) + r")(\d{3,4})$"
)
OVERSEAS_FUTURE_CONTRACT_RE = re.compile(
    r"^[A-Z]{1,4}[FGHJKMNQUVXZ]\d{2}\.(NYM|CMX|CBT|NYB|CME|ICE)$"
)

INTRADAY_INTERVAL_LABELS = {
    "15m": "15分钟",
    "30m": "30分钟",
    "60m": "60分钟",
}
INTRADAY_INTERVAL_MINUTES = {
    "15m": "15",
    "30m": "30",
    "60m": "60",
}
CCXT_INTERVAL_MAP = {
    "15m": "15m",
    "30m": "30m",
    "60m": "1h",
}

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
    "沪铅": "PB0",
    "沪金": "AU0",
    "沪银": "AG0",
    "集运欧线": "EC0",
    "上海原油": "SC0",
    "燃料油": "FU0",
    "液化石油气": "PG0",
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
        if CN_FUTURE_RE.fullmatch(cn_upper) or CN_FUTURE_CONTRACT_RE.fullmatch(cn_upper):
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
    if OVERSEAS_FUTURE_CONTRACT_RE.fullmatch(upper):
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
    # Domestic futures continuous main contract (e.g., I0, RB0, TA0) or specific contract (e.g., CU2612, TA705)
    if CN_FUTURE_RE.fullmatch(upper) or CN_FUTURE_CONTRACT_RE.fullmatch(upper):
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

    m = re.fullmatch(r"(\d+)\s*(d|w|wk|mo|y)", p)
    if not m:
        raise ValueError(f"Unsupported period format: {period!r}")

    qty = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        start = anchor - pd.Timedelta(days=qty)
    elif unit in ("w", "wk"):
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
    date_priority = ("datetime", "timestamp", "时间", "day", "date", "日期")
    col_map = {
        "开盘价": "Open",
        "开盘": "Open",
        "最高价": "High",
        "最高": "High",
        "最低价": "Low",
        "最低": "Low",
        "收盘价": "Close",
        "收盘": "Close",
        "成交量": "Volume",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }

    renamed = {}
    used_targets: set[str] = set()
    # Select highest-resolution timestamp column first to avoid Date collisions
    for pref in date_priority:
        for c in df.columns:
            c_str = str(c).strip()
            if c_str == pref or c_str.lower() == pref:
                renamed[c] = "Date"
                used_targets.add("Date")
                break
        if "Date" in used_targets:
            break

    for c in df.columns:
        if c in renamed:
            continue
        c_str = str(c).strip()
        target = col_map.get(c_str) or col_map.get(c_str.lower())
        if target and target not in used_targets:
            renamed[c] = target
            used_targets.add(target)
    if "Date" in used_targets:
        drop_cols = [c for c in df.columns if c == "Date" and c not in renamed]
        if drop_cols:
            df = df.drop(columns=drop_cols)
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
    """Fetch domestic Chinese futures continuous main contract or specific delivery contract daily OHLCV via akshare."""
    if ak is None:
        raise RuntimeError("akshare is not installed; required for domestic futures (cn_future)")
    start_date = period_to_start_date(period)
    end_date = datetime.now().strftime("%Y%m%d")
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)

    is_continuous = bool(CN_FUTURE_RE.fullmatch(symbol.strip().upper()))
    last_err: Exception | None = None
    with scoped_http_timeout((5.0, 12.0)):
        if is_continuous:
            try:
                raw = ak.futures_main_sina(symbol=symbol, start_date=start_date, end_date=end_date)
                df = normalize_cn_future_frame(raw)
                if not df.empty:
                    df = df[(df.index >= start_ts) & (df.index <= end_ts)]
                if not df.empty:
                    df.attrs["provider_source"] = "akshare_sina_main"
                    return df
            except Exception as exc:
                last_err = exc

        try:
            raw_fb = ak.futures_zh_daily_sina(symbol=symbol)
            df_fb = normalize_cn_future_frame(raw_fb)
            if not df_fb.empty:
                df_fb = df_fb[(df_fb.index >= start_ts) & (df_fb.index <= end_ts)]
            if not df_fb.empty:
                df_fb.attrs["provider_source"] = "akshare_sina_daily_fb"
                return df_fb
        except Exception as exc:
            last_err = exc

    if last_err is not None:
        raise last_err
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def _validate_intraday_interval(interval: str) -> tuple[str, str, str]:
    iv = (interval or "30m").strip().lower()
    if iv not in INTRADAY_INTERVAL_LABELS:
        supported = ", ".join(sorted(INTRADAY_INTERVAL_LABELS))
        raise ValueError(f"Unsupported intraday interval: {interval!r} (supported: {supported})")
    return iv, INTRADAY_INTERVAL_MINUTES[iv], INTRADAY_INTERVAL_LABELS[iv]


def _normalize_yfinance_frame(raw_df: pd.DataFrame | None) -> pd.DataFrame:
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=cols)
    df = raw_df.copy()
    for required in ("Open", "High", "Low", "Close"):
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


def _localize_df_index(df: pd.DataFrame, tz_name: str = "Asia/Shanghai") -> pd.DataFrame:
    """Localize a naive DatetimeIndex to ``tz_name`` (or convert if already tz-aware)."""
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    out = df.copy()
    if out.index.tz is not None:
        out.index = out.index.tz_convert(tz_name)
        return out
    try:
        out.index = out.index.tz_localize(tz_name, ambiguous="infer", nonexistent="shift_forward")
    except Exception:
        try:
            out.index = out.index.tz_localize(tz_name)
        except Exception:
            out.index = out.index.tz_localize(ZoneInfo(tz_name))
    return out


def fetch_cn_future_intraday(symbol: str, interval: str = "30m") -> pd.DataFrame:
    """Fetch domestic Chinese futures intraday OHLCV via akshare/sina (Asia/Shanghai +08:00)."""
    _, period_min, _ = _validate_intraday_interval(interval)
    if ak is None:
        raise RuntimeError("akshare is not installed; required for domestic futures intraday (cn_future)")
    last_exc: Exception | None = None
    raw = None
    for timeout_read in (12.0, 18.0):
        try:
            with scoped_http_timeout((5.0, timeout_read)):
                raw = ak.futures_zh_minute_sina(symbol=symbol, period=period_min)
            break
        except Exception as exc:
            last_exc = exc
    if raw is None:
        assert last_exc is not None
        raise last_exc
    df = normalize_cn_future_frame(raw)
    out = _localize_df_index(df, "Asia/Shanghai")
    out.attrs["provider_source"] = "akshare_sina_min"
    return out


def fetch_a_share_intraday(norm_sym: str, interval: str = "30m") -> pd.DataFrame:
    """Fetch A-share intraday OHLCV (qfq) via akshare Sina primary, Eastmoney fallback (Asia/Shanghai +08:00)."""
    _, period_min, _ = _validate_intraday_interval(interval)
    if ak is None:
        raise RuntimeError("akshare is not installed; required for A-share intraday (a_share)")

    upper = norm_sym.strip().upper()
    if upper.endswith(".SS"):
        code6 = upper[:-3]
        sina_sym = f"sh{code6}"
    elif upper.endswith(".SZ"):
        code6 = upper[:-3]
        sina_sym = f"sz{code6}"
    elif upper.endswith(".BJ"):
        code6 = upper[:-3]
        sina_sym = f"bj{code6}"
    elif re.fullmatch(r"\d{6}", upper):
        code6 = upper
        if upper.startswith(("4", "8", "92")):
            sina_sym = f"bj{code6}"
        elif upper.startswith(("5", "6", "9")):
            sina_sym = f"sh{code6}"
        else:
            sina_sym = f"sz{code6}"
    else:
        code6 = re.sub(r"^(SH|SZ|BJ)", "", upper)
        sina_sym = upper.lower()

    last_err: Exception | None = None
    with scoped_http_timeout((5.0, 12.0)):
        try:
            raw_sina = ak.stock_zh_a_minute(symbol=sina_sym, period=period_min, adjust="qfq")
            df_sina = normalize_cn_future_frame(raw_sina)
            if not df_sina.empty:
                out_sina = _localize_df_index(df_sina, "Asia/Shanghai")
                out_sina.attrs["provider_source"] = "akshare_sina_min"
                return out_sina
        except Exception as exc:
            last_err = exc

        try:
            raw_em = ak.stock_zh_a_hist_min_em(symbol=code6, period=period_min, adjust="qfq")
            df_em = normalize_cn_future_frame(raw_em)
            if not df_em.empty:
                out_em = _localize_df_index(df_em, "Asia/Shanghai")
                out_em.attrs["provider_source"] = "akshare_em_min"
                return out_em
        except Exception as exc:
            last_err = exc

    if last_err is not None:
        raise last_err
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def fetch_crypto_intraday(norm_sym: str, interval: str = "30m", limit: int = 500) -> pd.DataFrame:
    """Fetch crypto intraday OHLCV via ccxt (okx -> bybit -> binance fallback, max 1 backward page)."""
    iv, period_min, _ = _validate_intraday_interval(interval)
    ccxt_tf = CCXT_INTERVAL_MAP[iv]
    base = norm_sym.strip().upper().split("/")[0].replace("-USD", "")
    ccxt_sym = f"{base}/USDT"
    cols = ["Open", "High", "Low", "Close", "Volume"]

    last_err: Exception | None = None
    for eid in ("okx", "bybit", "binance"):
        try:
            ex = getattr(ccxt, eid)({"timeout": 8000})
            bars = ex.fetch_ohlcv(ccxt_sym, timeframe=ccxt_tf, limit=limit)
            if not bars:
                continue

            if len(bars) < limit:
                valid_ts = [int(b[0]) for b in bars if b and len(b) >= 6 and b[0] is not None]
                if valid_ts:
                    oldest_ts = min(valid_ts)
                    need = limit - len(bars)
                    bar_ms = int(period_min) * 60 * 1000
                    try:
                        if eid == "okx":
                            older = ex.fetch_ohlcv(
                                ccxt_sym,
                                timeframe=ccxt_tf,
                                limit=need,
                                params={"after": str(oldest_ts)},
                            )
                        else:
                            since_ms = max(0, oldest_ts - need * bar_ms)
                            older = ex.fetch_ohlcv(
                                ccxt_sym,
                                timeframe=ccxt_tf,
                                since=since_ms,
                                limit=need,
                            )
                        if older:
                            bars = list(older) + list(bars)
                    except Exception:
                        pass

            cleaned_rows = [b[:6] for b in bars if b and len(b) >= 6]
            if not cleaned_rows:
                continue
            df = pd.DataFrame(cleaned_rows, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
            dt_idx = pd.to_datetime(df["Timestamp"], unit="ms", utc=True, errors="coerce")
            df = df.assign(Date=dt_idx).dropna(subset=["Date"]).set_index("Date")
            for c in cols:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["Volume"] = df["Volume"].fillna(0.0).astype(float)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                continue
            for c in cols:
                df[c] = df[c].astype(float)
            df = df[~df.index.duplicated(keep="last")]
            df = df.sort_index(ascending=True, kind="mergesort")
            if len(df) > limit:
                df = df.iloc[-limit:]
            out_crypto = df[cols]
            out_crypto.attrs["provider_source"] = f"ccxt:{eid}"
            return out_crypto
        except Exception as exc:
            last_err = exc

    if last_err is not None:
        raise last_err
    return pd.DataFrame(columns=cols)


def _index_is_intraday(index) -> bool:
    """Detect intraday bar series from timestamps (non-zero H/M/S/us or duplicate calendar dates)."""
    if index is None or len(index) == 0:
        return False
    seen_dates: set[str] = set()
    for idx in index:
        ts = None
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
        if ts is not None and pd.notna(ts):
            if ts.hour != 0 or ts.minute != 0 or ts.second != 0 or ts.microsecond != 0:
                return True
            d_key = ts.strftime("%Y-%m-%d")
            if d_key in seen_dates:
                return True
            seen_dates.add(d_key)
    return False


def _parse_bar_duration(interval: str) -> pd.Timedelta | None:
    iv = (interval or "").strip().lower()
    if iv in INTRADAY_INTERVAL_MINUTES:
        return pd.Timedelta(minutes=int(INTRADAY_INTERVAL_MINUTES[iv]))
    m = re.fullmatch(r"(\d+)\s*(m|min|h)", iv)
    if not m:
        return None
    qty = int(m.group(1))
    if qty <= 0:
        return None
    return pd.Timedelta(hours=qty) if m.group(2) == "h" else pd.Timedelta(minutes=qty)


def _as_timestamp(val) -> pd.Timestamp | None:
    if isinstance(val, (pd.Timestamp, datetime)):
        ts = pd.Timestamp(val)
        return ts if pd.notna(ts) else None
    if hasattr(val, "year") and hasattr(val, "month") and hasattr(val, "day"):
        ts = pd.Timestamp(val)
        return ts if pd.notna(ts) else None
    try:
        cand = pd.Timestamp(val)
        return cand if pd.notna(cand) else None
    except Exception:
        return None


def compute_indicators(
    df: pd.DataFrame,
    interval: str = "1d",
    timestamp_anchor: str = "start",
    now_utc: datetime | None = None,
) -> dict:
    norm_interval = (interval or "1d").strip().lower()
    norm_anchor = (timestamp_anchor or "start").strip().lower()
    if norm_anchor not in ("start", "end"):
        raise ValueError(f"Unsupported timestamp_anchor: {timestamp_anchor!r} (expected 'start' or 'end')")
    is_intraday = (norm_interval != "1d") or _index_is_intraday(df.index)

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    open_s = df["Open"].astype(float) if "Open" in df else close
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
    last_bar_high = round(float(high.iloc[-1]), 4)
    last_bar_low = round(float(low.iloc[-1]), 4)
    last_atr = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else None

    def ret(n):
        if len(close) > n:
            return round((last_close / float(close.iloc[-1 - n]) - 1) * 100, 2)
        return None

    roll_max_60 = close.tail(60).cummax()
    dd_60 = ((close.tail(60) / roll_max_60) - 1).min() * 100

    vol_20_avg = float(vol.tail(20).mean()) if len(vol) >= 20 else None
    vol_ratio = round(float(vol.iloc[-1]) / vol_20_avg, 2) if vol_20_avg and vol_20_avg > 0 else None

    as_of_bar = _format_bar_timestamps([df.index[-1]], force_intraday=is_intraday)[0]
    ts_last = _as_timestamp(df.index[-1])
    as_of_bar_utc: str | None = None
    bar_is_closed: bool | None = None
    if ts_last is not None and ts_last.tzinfo is not None:
        ts_last_utc = ts_last.tz_convert("UTC")
        as_of_bar_utc = ts_last_utc.isoformat(timespec="seconds")
        bar_duration = _parse_bar_duration(norm_interval)
        if "_is_closed" in df.columns and now_utc is None:
            bar_is_closed = bool(df["_is_closed"].iloc[-1])
        elif is_intraday and bar_duration is not None:
            ref_now = (
                now_utc.astimezone(timezone.utc)
                if (now_utc is not None and now_utc.tzinfo is not None)
                else (now_utc.replace(tzinfo=timezone.utc) if now_utc is not None else datetime.now(timezone.utc))
            )
            bar_ts_py = ts_last_utc.to_pydatetime()
            bar_end_utc = bar_ts_py if norm_anchor == "end" else (bar_ts_py + bar_duration)
            bar_is_closed = ref_now >= bar_end_utc
    elif "_is_closed" in df.columns and now_utc is None:
        bar_is_closed = bool(df["_is_closed"].iloc[-1])

    max_recent_bars = 64 if is_intraday else 10
    tail_n = min(max_recent_bars, len(df))
    tail_idx = df.index[-tail_n:]
    tail_ts = _format_bar_timestamps(tail_idx, force_intraday=is_intraday)
    tail_utc: list[str | None] = []
    for raw_idx in tail_idx:
        ts_item = _as_timestamp(raw_idx)
        if ts_item is not None and ts_item.tzinfo is not None:
            tail_utc.append(ts_item.tz_convert("UTC").isoformat(timespec="seconds"))
        else:
            tail_utc.append(None)
    tail_open = open_s.iloc[-tail_n:].values
    tail_high = high.iloc[-tail_n:].values
    tail_low = low.iloc[-tail_n:].values
    tail_close = close.iloc[-tail_n:].values
    has_raw_hl = "_raw_high" in df.columns and "_raw_low" in df.columns
    tail_raw_high = df["_raw_high"].astype(float).iloc[-tail_n:].values if has_raw_hl else None
    tail_raw_low = df["_raw_low"].astype(float).iloc[-tail_n:].values if has_raw_hl else None
    tail_repaired = df["_envelope_repaired"].astype(bool).iloc[-tail_n:].values if "_envelope_repaired" in df.columns else None
    recent_bars_ohlc = []
    for i in range(tail_n):
        bar_entry: dict = {
            "bar": tail_ts[i],
            "bar_utc": tail_utc[i],
            "open": round(float(tail_open[i]), 4),
            "high": round(float(tail_high[i]), 4),
            "low": round(float(tail_low[i]), 4),
            "close": round(float(tail_close[i]), 4),
        }
        if tail_raw_high is not None and tail_raw_low is not None:
            bar_entry["raw_high"] = round(float(tail_raw_high[i]), 4)
            bar_entry["raw_low"] = round(float(tail_raw_low[i]), 4)
            bar_entry["envelope_repaired"] = bool(tail_repaired[i]) if tail_repaired is not None else False
        recent_bars_ohlc.append(bar_entry)

    common_head = {
        "as_of_bar": as_of_bar,
        "as_of_bar_utc": as_of_bar_utc,
        "bar_interval": norm_interval,
        "bar_timestamp_anchor": norm_anchor,
        "bar_is_closed": bar_is_closed,
        "bars": len(df),
        "last_close": round(last_close, 4),
        "last_bar_high": last_bar_high,
        "last_bar_low": last_bar_low,
        "recent_bars_ohlc": recent_bars_ohlc,
    }
    common_osc = {
        "macd_dif": round(float(dif.iloc[-1]), 4),
        "macd_dea": round(float(dea.iloc[-1]), 4),
        "macd_hist": round(float(macd_hist.iloc[-1]), 4),
        "atr14": round(last_atr, 4) if last_atr else None,
        "atr14_pct": round((last_atr / last_close) * 100, 2) if last_atr and last_close else None,
    }

    if is_intraday:
        return {
            **common_head,
            "return_1bar_pct": ret(1),
            "return_5bar_pct": ret(5),
            "return_20bar_pct": ret(20),
            "return_60bar_pct": ret(60),
            "max_drawdown_60bar_pct": round(float(dd_60), 2),
            **common_osc,
            "high_20bar": round(float(high.tail(20).max()), 4),
            "low_20bar": round(float(low.tail(20).min()), 4),
            "high_60bar": round(float(high.tail(60).max()), 4),
            "low_60bar": round(float(low.tail(60).min()), 4),
            "volume_ratio_20bar": vol_ratio,
        }

    return {
        **common_head,
        "return_1d_pct": ret(1),
        "return_5d_pct": ret(5),
        "return_20d_pct": ret(20),
        "return_60d_pct": ret(60),
        "return_120d_pct": ret(120),
        "max_drawdown_60d_pct": round(float(dd_60), 2),
        **common_osc,
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
    with scoped_http_timeout((5.0, 10.0)):
        for sym, label in [("^VIX", "VIX"), ("^TNX", "US10Y_Yield"), ("DX-Y.NYB", "DXY_USD_Index")]:
            try:
                df = yf.Ticker(sym).history(period="10d").dropna(subset=["Close"])
                if len(df) >= 2:
                    last = float(df["Close"].iloc[-1])
                    prev = float(df["Close"].iloc[-2])
                    idx_last = df.index[-1]
                    as_of = idx_last.strftime("%Y-%m-%d") if hasattr(idx_last, "strftime") else str(idx_last)[:10]
                    ctx[label] = {
                        "last": round(last, 2),
                        "chg_1d_pct": round((last / prev - 1) * 100, 2),
                        "as_of": as_of,
                    }
            except Exception:
                pass
    return ctx


def _finite_or_none(val) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def serialize_ohlcv_bars(df: pd.DataFrame, intraday: bool) -> list[dict]:
    """Serialize OHLCV + MACD bars for charting (strictly ascending ``time``, deduplicated keep-last).

    ``time`` is ``YYYY-MM-DD`` for daily bars and integer UTC unix seconds for intraday bars;
    ``label`` matches the ``chanlun.py`` bar timestamp strings exactly.
    """
    if df is None or df.empty:
        return []
    close = df["Close"].astype(float)
    open_s = df["Open"].astype(float) if "Open" in df else close
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df else pd.Series(0.0, index=df.index)
    dif, dea, hist = compute_macd(close)
    labels = _format_bar_timestamps(df.index, force_intraday=intraday)

    by_time: dict = {}
    for i, raw_idx in enumerate(df.index):
        ts = _as_timestamp(raw_idx)
        if ts is None:
            continue
        if intraday:
            ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            t_key = int(ts_utc.timestamp())
        else:
            t_key = ts.strftime("%Y-%m-%d")
        o, h, l_, c = (_finite_or_none(x) for x in (open_s.iloc[i], high.iloc[i], low.iloc[i], close.iloc[i]))
        if None in (o, h, l_, c):
            continue
        by_time[t_key] = {
            "time": t_key,
            "label": labels[i],
            "open": o,
            "high": h,
            "low": l_,
            "close": c,
            "volume": _finite_or_none(vol.iloc[i]) or 0.0,
            "dif": _finite_or_none(dif.iloc[i]),
            "dea": _finite_or_none(dea.iloc[i]),
            "hist": _finite_or_none(hist.iloc[i]),
        }
    return [by_time[k] for k in sorted(by_time)]


def _ticker_supports_start(history_fn) -> bool:
    try:
        sig = inspect.signature(history_fn)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    return "start" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def get_intraday_snapshot(
    raw_symbol: str,
    interval: str = "30m",
    full_chanlun: bool = False,
    include_ohlcv: bool = False,
    *,
    refresh: bool = False,
    use_store: bool | None = None,
    db_path: str | None = None,
    ttl_seconds: float | None = None,
) -> dict:
    """Fetch intraday OHLCV (15m/30m/60m) and run Chanlun analysis across all 6 asset classes."""
    try:
        iv, _, tf_label = _validate_intraday_interval(interval)
        norm_sym, asset_class = normalize_symbol(raw_symbol)
        store_on = is_store_enabled(use_store, db_path)

        if asset_class == "cn_future":
            data_source = "akshare/sina 分钟"
            primary_src_id = "akshare_sina_min"
            adjust_kind = "raw_continuous" if CN_FUTURE_RE.fullmatch(norm_sym) else "raw_contract"
            timestamp_anchor = "end"
            max_bars: int | None = 1000

            def fetch_fn() -> pd.DataFrame:
                return fetch_cn_future_intraday(norm_sym, interval=iv)
        elif asset_class == "a_share":
            data_source = "akshare 分钟(qfq)"
            primary_src_id = "akshare_sina_min"
            adjust_kind = "qfq"
            timestamp_anchor = "end"
            max_bars = 1000

            def fetch_fn() -> pd.DataFrame:
                return fetch_a_share_intraday(norm_sym, interval=iv)
        elif asset_class == "crypto":
            data_source = "ccxt 分钟"
            primary_src_id = "ccxt:okx"
            adjust_kind = "spot_usdt"
            timestamp_anchor = "start"
            max_bars = 500

            def fetch_fn() -> pd.DataFrame:
                return fetch_crypto_intraday(norm_sym, interval=iv)
        else:
            data_source = "yfinance 分钟"
            primary_src_id = "yfinance_intraday"
            adjust_kind = "auto_adjust" if asset_class == "us_stock_or_etf" else "raw_quote"
            timestamp_anchor = "start"
            max_bars = None

            def fetch_fn() -> pd.DataFrame:
                with scoped_http_timeout((5.0, 12.0)):
                    raw_df = yf.Ticker(norm_sym).history(period="60d", interval=iv)
                out = _normalize_yfinance_frame(raw_df)
                out.attrs["provider_source"] = "yfinance_intraday"
                return out

        sync_meta: dict = {"cache_status": "bypass"}
        if store_on:
            store = get_store(db_path)
            primary_key = SeriesKey(asset_class, norm_sym, iv, primary_src_id, adjust_kind)
            # Fast path: check primary series cache hit without hitting network
            if not refresh:
                df_cached, qual_cached, meta_cached = store.get_or_sync_ohlcv(
                    primary_key,
                    period="60d",
                    timestamp_anchor=timestamp_anchor,
                    ttl_seconds=ttl_seconds,
                    refresh=False,
                    max_bars=max_bars,
                    supports_incremental=False,
                    fetch_full=lambda: pd.DataFrame(),
                )
                if meta_cached.get("cache_status") == "hit" and not df_cached.empty:
                    df, intra_quality, sync_meta = df_cached, qual_cached, meta_cached
                else:
                    df_cached = pd.DataFrame()
            else:
                df_cached = pd.DataFrame()

            if df_cached.empty:
                try:
                    raw_fetched = fetch_fn()
                except Exception as fetch_exc:
                    # Graceful stale-cache fallback when upstream times out / glitches
                    fb_df, fb_qual, fb_meta = store.get_or_sync_ohlcv(
                        primary_key,
                        period="60d",
                        timestamp_anchor=timestamp_anchor,
                        ttl_seconds=10**9,
                        refresh=False,
                        max_bars=max_bars,
                        supports_incremental=False,
                        fetch_full=lambda: pd.DataFrame(),
                    )
                    if fb_df is not None and not fb_df.empty:
                        df = fb_df
                        intra_quality = {**fb_qual, "stale_fallback": True}
                        sync_meta = {
                            **fb_meta,
                            "cache_status": "stale_fallback",
                            "upstream_error": str(fetch_exc),
                        }
                    else:
                        raise
                else:
                    actual_src_id = str(raw_fetched.attrs.get("provider_source") or primary_src_id)
                    actual_key = SeriesKey(asset_class, norm_sym, iv, actual_src_id, adjust_kind)
                    df, intra_quality, sync_meta = store.get_or_sync_ohlcv(
                        actual_key,
                        period="60d",
                        timestamp_anchor=timestamp_anchor,
                        ttl_seconds=ttl_seconds,
                        refresh=True,
                        max_bars=max_bars,
                        supports_incremental=False,
                        fetch_full=lambda: raw_fetched,
                    )
        else:
            raw_fetched = fetch_fn()
            df, intra_quality = sanitize_and_grade_ohlcv(
                raw_fetched,
                asset_class=asset_class,
                interval=iv,
                timestamp_anchor=timestamp_anchor,
                check_staleness=False,
            )

        if df is None or df.empty:
            return {"error": f"No {iv} OHLCV data returned for {raw_symbol} (normalized: {norm_sym})"}

        indicators = compute_indicators(df, interval=iv, timestamp_anchor=timestamp_anchor)
        if intra_quality.get("dropped_bar_keys"):
            indicators["dropped_bar_keys"] = intra_quality["dropped_bar_keys"]
        intraday_cl = analyze_chanlun(df, timeframe_label=tf_label, full_export=full_chanlun)
        if isinstance(intraday_cl, dict) and "error" not in intraday_cl:
            intraday_cl["data_quality"] = intra_quality
        signal_summary = evaluate_signal_tier(intraday_cl, timeframe_label=tf_label)

        intraday_result = {
            "query": raw_symbol,
            "symbol": norm_sym,
            "asset_class": asset_class,
            "interval": iv,
            "timeframe_label": tf_label,
            "data_source": data_source,
            "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "data_quality": intra_quality,
            "sync_meta": sync_meta,
            "technicals": indicators,
            "chanlun": {
                "signal_summary": signal_summary,
                "intraday": intraday_cl,
            },
        }
        if include_ohlcv:
            intraday_result["ohlcv_intraday"] = serialize_ohlcv_bars(df, intraday=True)
        return intraday_result
    except Exception as exc:
        return {"error": f"Failed to fetch intraday ({interval}) snapshot for {raw_symbol}: {exc}"}


def get_snapshot(
    raw_symbol: str,
    period: str = "3y",
    include_macro: bool = True,
    include_fundamentals: bool = True,
    full_chanlun: bool = False,
    include_intraday: bool = False,
    intraday_interval: str = "30m",
    include_ohlcv: bool = False,
    *,
    refresh: bool = False,
    use_store: bool | None = None,
    db_path: str | None = None,
    ttl_seconds: float | None = None,
) -> dict:
    try:
        norm_sym, asset_class = normalize_symbol(raw_symbol)
        fundamentals = {}
        ticker = None
        store_on = is_store_enabled(use_store, db_path)
        sync_meta: dict = {"cache_status": "bypass"}

        if asset_class == "cn_future":
            is_cont = bool(CN_FUTURE_RE.fullmatch(norm_sym))
            data_source = "akshare/sina 主力连续" if is_cont else "akshare/sina 期货合约"
            primary_src_id = "akshare_sina_main" if is_cont else "akshare_sina_daily_fb"
            adjust_kind = "raw_continuous" if is_cont else "raw_contract"

            if store_on:
                store = get_store(db_path)
                primary_key = SeriesKey(asset_class, norm_sym, "1d", primary_src_id, adjust_kind)
                df_cached = pd.DataFrame()
                if not refresh:
                    df_cached, daily_quality, sync_meta = store.get_or_sync_ohlcv(
                        primary_key,
                        period=period,
                        timestamp_anchor="end",
                        ttl_seconds=ttl_seconds,
                        refresh=False,
                        supports_incremental=False,
                        fetch_full=lambda: pd.DataFrame(),
                    )
                if df_cached.empty or sync_meta.get("cache_status") != "hit":
                    # Sina downloads full history anyway; always sync at least 3y into SQLite so short queries never truncate 3y coverage
                    fetch_period = "3y" if period_to_days(period) < 1095 else period
                    raw_cn = fetch_cn_future_daily(norm_sym, period=fetch_period)
                    actual_src_id = str(raw_cn.attrs.get("provider_source") or primary_src_id)
                    actual_key = SeriesKey(asset_class, norm_sym, "1d", actual_src_id, adjust_kind)
                    _, _, sync_meta = store.get_or_sync_ohlcv(
                        actual_key,
                        period=fetch_period,
                        timestamp_anchor="end",
                        ttl_seconds=ttl_seconds,
                        refresh=True,
                        supports_incremental=False,
                        fetch_full=lambda: raw_cn,
                    )
                    df, daily_quality, _ = store.get_or_sync_ohlcv(
                        actual_key,
                        period=period,
                        timestamp_anchor="end",
                        ttl_seconds=ttl_seconds,
                        refresh=False,
                        supports_incremental=False,
                        fetch_full=lambda: raw_cn,
                    )
                else:
                    df = df_cached
            else:
                raw_cn = fetch_cn_future_daily(norm_sym, period=period)
                df, daily_quality = sanitize_and_grade_ohlcv(
                    raw_cn,
                    asset_class=asset_class,
                    interval="1d",
                    timestamp_anchor="end",
                    check_staleness=False,
                )
        else:
            data_source = "yfinance"
            ticker = yf.Ticker(norm_sym)
            adjust_kind = (
                "auto_adjust"
                if asset_class in ("us_stock_or_etf", "a_share")
                else ("spot_usd" if asset_class == "crypto" else "raw_quote")
            )

            def _fetch_yf_full() -> pd.DataFrame:
                with scoped_http_timeout((5.0, 12.0)):
                    return ticker.history(period=period, interval="1d")

            def _fetch_yf_tail(overlap_start: str) -> pd.DataFrame:
                with scoped_http_timeout((5.0, 12.0)):
                    if _ticker_supports_start(ticker.history):
                        return ticker.history(start=overlap_start, interval="1d")
                    return ticker.history(period=period, interval="1d")

            if store_on:
                store = get_store(db_path)
                yf_key = SeriesKey(asset_class, norm_sym, "1d", "yfinance", adjust_kind)
                df, daily_quality, sync_meta = store.get_or_sync_ohlcv(
                    yf_key,
                    period=period,
                    timestamp_anchor="start",
                    ttl_seconds=ttl_seconds,
                    refresh=refresh,
                    supports_incremental=True,
                    fetch_full=_fetch_yf_full,
                    fetch_tail=_fetch_yf_tail,
                )
            else:
                raw_yf = _fetch_yf_full()
                df, daily_quality = sanitize_and_grade_ohlcv(
                    raw_yf,
                    asset_class=asset_class,
                    interval="1d",
                    timestamp_anchor="start",
                    check_staleness=False,
                )

        if df is not None and not df.empty:
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df is None or df.empty:
            return {"error": f"No OHLCV data returned for {raw_symbol} (normalized: {norm_sym})"}

        indicators = compute_indicators(df, interval="1d")
        if daily_quality.get("dropped_bar_keys"):
            indicators["dropped_bar_keys"] = daily_quality["dropped_bar_keys"]
        daily_cl = analyze_chanlun(df, timeframe_label="日线", full_export=full_chanlun)
        if isinstance(daily_cl, dict) and "error" not in daily_cl:
            daily_cl["data_quality"] = daily_quality
        signal_summary = evaluate_daily_signal_tier(daily_cl)

        if include_fundamentals and asset_class not in ("cn_future", "fx") and ticker is not None:
            try:
                with scoped_http_timeout((5.0, 8.0)):
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
            "data_quality": daily_quality,
            "sync_meta": sync_meta,
            "technicals": indicators,
            "chanlun": {
                "signal_summary": signal_summary,
                "daily": daily_cl,
            },
            "fundamentals": fundamentals,
        }

        if include_ohlcv:
            result["ohlcv_daily"] = serialize_ohlcv_bars(df, intraday=False)

        if include_intraday:
            try:
                intraday_kwargs: dict = {
                    "refresh": refresh,
                    "use_store": use_store,
                    "db_path": db_path,
                    "ttl_seconds": ttl_seconds,
                }
                if include_ohlcv:
                    intraday_kwargs["include_ohlcv"] = True
                intra_snap = get_intraday_snapshot(
                    raw_symbol,
                    interval=intraday_interval,
                    full_chanlun=full_chanlun,
                    **intraday_kwargs,
                )
            except Exception as intra_exc:
                intra_snap = {"error": f"Failed to fetch intraday ({intraday_interval}) snapshot: {intra_exc}"}

            if "error" in intra_snap:
                result["intraday_error"] = intra_snap["error"]
                if include_ohlcv:
                    result["ohlcv_intraday"] = []
            else:
                intraday_cl = intra_snap["chanlun"]["intraday"]
                result["intraday_interval"] = intra_snap["interval"]
                result["intraday_data_source"] = intra_snap["data_source"]
                result["intraday_data_quality"] = intra_snap.get("data_quality")
                result["intraday_sync_meta"] = intra_snap.get("sync_meta")
                result["intraday_technicals"] = intra_snap["technicals"]
                result["chanlun"]["intraday"] = intraday_cl
                result["chanlun"]["intraday_signal_summary"] = intra_snap["chanlun"]["signal_summary"]
                result["chanlun"]["dual_alignment"] = assess_dual_timeframe_alignment(daily_cl, intraday_cl)
                if include_ohlcv:
                    result["ohlcv_intraday"] = intra_snap.get("ohlcv_intraday", [])

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
    parser.add_argument(
        "--interval",
        default="1d",
        choices=["1d", "15m", "30m", "60m"],
        help="Bar interval: 1d (default) or intraday 15m/30m/60m",
    )
    parser.add_argument(
        "--dual",
        action="store_true",
        help="Run dual-timeframe daily + 30m (or specified intraday --interval) alignment snapshot",
    )
    parser.add_argument("--no-macro", action="store_true", help="Skip macro context fetch")
    parser.add_argument(
        "--full-chanlun",
        action="store_true",
        help="Include full strokes, segments, and Zhongshu lists in Chanlun output",
    )
    parser.add_argument("--refresh", action="store_true", help="Force upstream refresh bypassing local freshness window")
    parser.add_argument("--no-cache", action="store_true", help="Bypass local SQLite store completely")
    args = parser.parse_args()
    extra_cli_kw: dict = {}
    if args.refresh:
        extra_cli_kw["refresh"] = True
    if args.no_cache:
        extra_cli_kw["use_store"] = False
    if args.dual:
        intraday_iv = "30m" if args.interval == "1d" else args.interval
        data = get_snapshot(
            args.symbol,
            period=args.period,
            include_macro=not args.no_macro,
            full_chanlun=args.full_chanlun,
            include_intraday=True,
            intraday_interval=intraday_iv,
            **extra_cli_kw,
        )
    elif args.interval != "1d":
        data = get_intraday_snapshot(
            args.symbol,
            interval=args.interval,
            full_chanlun=args.full_chanlun,
            **extra_cli_kw,
        )
    else:
        data = get_snapshot(
            args.symbol,
            period=args.period,
            include_macro=not args.no_macro,
            full_chanlun=args.full_chanlun,
            **extra_cli_kw,
        )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(data, dict) and ("error" in data or (args.dual and "intraday_error" in data)):
        sys.exit(1)


if __name__ == "__main__":
    main()
