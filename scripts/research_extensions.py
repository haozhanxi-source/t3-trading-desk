#!/usr/bin/env python3
"""Layer-3 research data extensions with strict per-symbol whitelist routing.

Self-contained zero-key data modules designed to be imported by ``research_data.py``
without circular imports:
  1. Overseas commodity term structure & annualized roll yield (+ CN daily SC/TA curves)
  2. Cross-market spreads & import parity (crude oil, copper, gold, silver, sugar, cotton)
  3. Baltic freight indices (BDI / BCI / BSI / BDTI / BCTI) with strict symbol routing
  4. FX dedicated data: CFTC TFF positioning (with sign alignment) & sovereign rate differentials
  5. A-share 5Y valuation percentiles, US valuation/positioning, and BTC/ETH Farside ETF flows

Every public fetcher enforces its whitelist BEFORE any network call so unrelated symbols
return ``{"status": "skipped", ...}`` immediately with zero wasted requests.
"""
from __future__ import annotations

import calendar as cal_lib
import concurrent.futures
import csv
import html as html_lib
import io
import json
import math
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None


HTTP_TIMEOUT = 15
DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

# Unit conversion constants
LB_PER_MT = 2204.62262
GRAMS_PER_TROY_OZ = 31.1034768
TROY_OZ_PER_KG = 32.1507466
CN_VAT_MULTIPLIER = 1.13

# ---------------------------------------------------------------------------
# Whitelist Specifications
# ---------------------------------------------------------------------------

FUTURES_MONTH_CODES: dict[int, str] = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}

OVERSEAS_TERM_STRUCTURE_SPECS: dict[str, dict[str, Any]] = {
    "CL=F": {
        "root": "CL",
        "suffix": ".NYM",
        "months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "front_offset_before_cutoff": 1,
        "roll_cutoff_day": 18,
        "unit": "USD/bbl",
    },
    "BZ=F": {
        # CME Rule 698: Brent Last Day Financial stops trading last business day of M-2
        "root": "BZ",
        "suffix": ".NYM",
        "months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "front_offset_before_cutoff": 2,
        "roll_cutoff_day": 25,
        "unit": "USD/bbl",
    },
    "NG=F": {
        "root": "NG",
        "suffix": ".NYM",
        "months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "front_offset_before_cutoff": 1,
        "roll_cutoff_day": 23,
        "unit": "USD/MMBtu",
    },
    "HG=F": {
        "root": "HG",
        "suffix": ".CMX",
        "months": [3, 5, 7, 9, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 22,
        "unit": "USD/lb",
    },
    "GC=F": {
        "root": "GC",
        "suffix": ".CMX",
        "months": [2, 4, 6, 8, 10, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 22,
        "unit": "USD/oz",
    },
    "SI=F": {
        "root": "SI",
        "suffix": ".CMX",
        "months": [3, 5, 7, 9, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 22,
        "unit": "USD/oz",
    },
    "ZC=F": {
        "root": "ZC",
        "suffix": ".CBT",
        "months": [3, 5, 7, 9, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 10,
        "unit": "USc/bu",
    },
    "ZS=F": {
        "root": "ZS",
        "suffix": ".CBT",
        "months": [1, 3, 5, 7, 8, 9, 11],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 10,
        "unit": "USc/bu",
    },
    "ZW=F": {
        "root": "ZW",
        "suffix": ".CBT",
        "months": [3, 5, 7, 9, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 10,
        "unit": "USc/bu",
    },
    "SB=F": {
        # ICE Sugar No. 11 expires last business day of M-1
        "root": "SB",
        "suffix": ".NYB",
        "months": [3, 5, 7, 10],
        "front_offset_before_cutoff": 1,
        "roll_cutoff_day": 24,
        "unit": "USc/lb",
    },
    "KC=F": {
        "root": "KC",
        "suffix": ".NYB",
        "months": [3, 5, 7, 9, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 14,
        "unit": "USc/lb",
    },
    "CT=F": {
        # ICE Cotton No. 2 expires 17 business days from end of delivery month M
        "root": "CT",
        "suffix": ".NYB",
        "months": [3, 5, 7, 10, 12],
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 5,
        "unit": "USc/lb",
    },
}

CN_DAILY_TERM_STRUCTURE_DEFAULTS: dict[str, dict[str, Any]] = {
    "SC": {
        # INE SC trades monthly and expires last trading day of M-1
        "front_offset_before_cutoff": 1,
        "roll_cutoff_day": 25,
        "monthly_offsets": [0, 1, 2, 3, 6],
        "active_months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "max_contracts": 5,
        "unit": "CNY/bbl",
    },
    "TA": {
        # CZCE PTA lists all 12 calendar months (expires 10th trading day of M); sample active front + 1/5/9 mains
        "front_offset_before_cutoff": 0,
        "roll_cutoff_day": 10,
        "monthly_offsets": None,
        "include_front_active_month": True,
        "active_months": [1, 5, 9],
        "max_contracts": 5,
        "unit": "CNY/MT",
    },
}

CROSS_MARKET_SPREAD_SPECS: dict[str, dict[str, Any]] = {
    "CL=F": {"group": "crude_oil"},
    "BZ=F": {"group": "crude_oil"},
    "SC0": {"group": "crude_oil"},
    "CU0": {"group": "copper"},
    "HG=F": {"group": "copper"},
    "AU0": {"group": "gold"},
    "GC=F": {"group": "gold"},
    "AG0": {"group": "silver"},
    "SI=F": {"group": "silver"},
    "SR0": {"group": "sugar"},
    "SB=F": {"group": "sugar"},
    "CF0": {"group": "cotton"},
    "CT=F": {"group": "cotton"},
}

FREIGHT_ROUTING: dict[str, list[str]] = {
    "CL=F": ["BDTI", "BCTI"],
    "BZ=F": ["BDTI", "BCTI"],
    "SC0": ["BDTI", "BCTI"],
    "I0": ["BCI", "BDI"],
    "JM0": ["BCI", "BDI"],
    "J0": ["BCI", "BDI"],
    "ZS=F": ["BDI", "BSI"],
    "ZC=F": ["BDI", "BSI"],
    "ZW=F": ["BDI", "BSI"],
    "SB=F": ["BDI", "BSI"],
    "M0": ["BDI", "BSI"],
    "Y0": ["BDI", "BSI"],
    "P0": ["BDI", "BSI"],
    "OI0": ["BDI", "BSI"],
    "SR0": ["BDI", "BSI"],
    "EC0": ["BDI", "BCI", "BSI", "BDTI", "BCTI"],
}

FREIGHT_COLUMN_MAP: dict[str, str] = {
    "BDI": "波罗的海综合运价指数BDI",
    "BCI": "波罗的海好望角型船运价指数BCI",
    "BSI": "波罗的海超级大灵便型船BSI指数",
    "BDTI": "油轮运价指数原油运价指数BDTI",
    "BCTI": "油轮运价指数成品油运价指数BCTI",
}

CFTC_TFF_FX_SPECS: dict[str, dict[str, Any]] = {
    "EURUSD=X": {
        "code": "099741",
        "patterns": ["EURO FX - CHICAGO MERCANTILE EXCHANGE"],
        "futures_direction": "same",
        "note": "CME欧元期货报价为EUR/USD，与EURUSD=X同向",
    },
    "USDJPY=X": {
        "code": "097741",
        "patterns": ["JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE"],
        "futures_direction": "inverse",
        "note": "CME日元期货报价为JPY/USD，与USDJPY=X反向：期货净多=做多日元/做空USDJPY",
    },
    "GBPUSD=X": {
        "code": "096742",
        "patterns": ["BRITISH POUND - CHICAGO MERCANTILE EXCHANGE"],
        "futures_direction": "same",
        "note": "CME英镑期货报价为GBP/USD，与GBPUSD=X同向",
    },
    "AUDUSD=X": {
        "code": "232741",
        "patterns": ["AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
        "futures_direction": "same",
        "note": "CME澳元期货报价为AUD/USD，与AUDUSD=X同向",
    },
    "USDCAD=X": {
        "code": "090741",
        "patterns": ["CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
        "futures_direction": "inverse",
        "note": "CME加元期货报价为CAD/USD，与USDCAD=X反向：期货净多=做多加元/做空USDCAD",
    },
    "DX-Y.NYB": {
        "code": "098662",
        "patterns": ["USD INDEX - ICE FUTURES U.S."],
        "futures_direction": "same",
        "note": "ICE美元指数期货，与DXY同向",
    },
}

FX_RATE_DIFF_SPECS: dict[str, dict[str, Any]] = {
    "USDCNY=X": {
        "source": "akshare_bond_zh_us_rate",
        "base_label": "US10Y",
        "quote_label": "CN10Y",
        "formula": "US - CN (10Y & 2Y)",
        "frequency_note": "中美国债收益率均为日频数据（中国债券信息网/东财与美国财政部），节假日可能导致单边滞后1~3日",
    },
    "EURUSD=X": {
        "source": "fred_pair",
        "base_series": "IRLTLT01DEM156N",
        "base_label": "DE10Y",
        "base_freq": "monthly (OECD)",
        "quote_series": "DGS10",
        "quote_label": "US10Y",
        "quote_freq": "daily",
        "formula": "DE10Y - US10Y",
        "frequency_note": "注意：FRED 德国10Y(IRLTLT01DEM156N)为OECD月度均值，美债DGS10为日频，两者存在发布时滞",
    },
    "USDJPY=X": {
        "source": "fred_pair",
        "base_series": "DGS10",
        "base_label": "US10Y",
        "base_freq": "daily",
        "quote_series": "IRLTLT01JPM156N",
        "quote_label": "JP10Y",
        "quote_freq": "monthly (OECD)",
        "formula": "US10Y - JP10Y",
        "frequency_note": "注意：FRED 日本10Y(IRLTLT01JPM156N)为OECD月度均值，美债DGS10为日频，两者存在发布时滞",
    },
    "GBPUSD=X": {
        "source": "fred_pair",
        "base_series": "IRLTLT01GBM156N",
        "base_label": "UK10Y",
        "base_freq": "monthly (OECD)",
        "quote_series": "DGS10",
        "quote_label": "US10Y",
        "quote_freq": "daily",
        "formula": "UK10Y - US10Y",
        "frequency_note": "注意：FRED 英国10Y(IRLTLT01GBM156N)为OECD月度均值，美债DGS10为日频，两者存在发布时滞",
    },
    "AUDUSD=X": {
        "source": "fred_pair",
        "base_series": "IRLTLT01AUM156N",
        "base_label": "AU10Y",
        "base_freq": "monthly (OECD)",
        "quote_series": "DGS10",
        "quote_label": "US10Y",
        "quote_freq": "daily",
        "formula": "AU10Y - US10Y",
        "frequency_note": "注意：FRED 澳洲10Y(IRLTLT01AUM156N)为OECD月度均值，美债DGS10为日频，两者存在发布时滞",
    },
    "USDCAD=X": {
        "source": "fred_pair",
        "base_series": "DGS10",
        "base_label": "US10Y",
        "base_freq": "daily",
        "quote_series": "IRLTLT01CAM156N",
        "quote_label": "CA10Y",
        "quote_freq": "monthly (OECD)",
        "formula": "US10Y - CA10Y",
        "frequency_note": "注意：FRED 加拿大10Y(IRLTLT01CAM156N)为OECD月度均值，美债DGS10为日频，两者存在发布时滞",
    },
    "DX-Y.NYB": {
        "source": "fred_dxy_basket",
        "series": {
            "US10Y": ("DGS10", "daily"),
            "US2Y": ("DGS2", "daily"),
            "US10Y_REAL": ("DFII10", "daily"),
            "DE10Y": ("IRLTLT01DEM156N", "monthly (OECD)"),
            "JP10Y": ("IRLTLT01JPM156N", "monthly (OECD)"),
        },
        "frequency_note": "注意：美债DGS10/DGS2/DFII10为日频，德日10Y(OECD)为月度均值，跨国利差存在月度发布时滞",
    },
}

FARSIDE_ETF_SPECS: dict[str, dict[str, Any]] = {
    "BTC": {
        "url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "fallback_url": "https://farside.co.uk/btc/",
        "anchor_tickers": ("IBIT", "FBTC", "GBTC"),
    },
    "ETH": {
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "fallback_url": "https://farside.co.uk/eth/",
        "anchor_tickers": ("ETHA", "FETH", "ETHE"),
    },
}


# ---------------------------------------------------------------------------
# Self-Contained Utility & Timeout Helpers
# ---------------------------------------------------------------------------

def _to_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, (bool, np.bool_)):
        return None
    if isinstance(raw, (int, float, np.integer, np.floating)):
        fval = float(raw)
        return fval if math.isfinite(fval) else None
    s = str(raw).strip().replace(",", "")
    if not s or s in {".", "N/A", "NA", "-", "--", "nan", "NaN", "None", "null", "inf", "-inf"}:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        val = float(s)
        if not math.isfinite(val):
            return None
        return -val if neg else val
    except ValueError:
        return None


def _round_finite(val: float | None, ndigits: int = 4) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    out = round(float(val), ndigits)
    return out if math.isfinite(out) else None


def _to_int(raw: Any) -> int | None:
    f = _to_float(raw)
    if f is None:
        return None
    return int(round(f))


def _to_iso_date(val: Any) -> str | None:
    if val is None or isinstance(val, (bool, np.bool_)):
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, pd.Timestamp):
        return val.date().isoformat()
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, (int, float, np.integer, np.floating)):
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "nat", "none", "null", "-", "--"}:
        return None
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        return None


def _parse_date_obj(val: Any) -> date | None:
    iso = _to_iso_date(val)
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively ensure all floats are finite and all dates/numpy scalars are JSON-safe."""
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.date().isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        fval = float(obj)
        return fval if math.isfinite(fval) else None
    return obj


_STDOUT_FILTER_LOCK = threading.Lock()
_SILENCED_THREAD_IDS: set[int] = set()
_ORIGINAL_STDOUT: Any = None


class _ThreadFilteredStdout:
    """Drop stdout writes ONLY from explicitly silenced worker threads."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def write(self, data: str) -> int:
        if threading.get_ident() in _SILENCED_THREAD_IDS:
            return len(data)
        return self._target.write(data)

    def flush(self) -> None:
        if threading.get_ident() not in _SILENCED_THREAD_IDS:
            self._target.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _install_stdout_filter_if_needed() -> None:
    global _ORIGINAL_STDOUT
    if not isinstance(sys.stdout, _ThreadFilteredStdout):
        _ORIGINAL_STDOUT = sys.stdout
        sys.stdout = _ThreadFilteredStdout(_ORIGINAL_STDOUT)


def _restore_stdout_if_idle() -> None:
    global _ORIGINAL_STDOUT
    if (
        not _SILENCED_THREAD_IDS
        and isinstance(sys.stdout, _ThreadFilteredStdout)
        and _ORIGINAL_STDOUT is not None
    ):
        sys.stdout = _ORIGINAL_STDOUT
        _ORIGINAL_STDOUT = None


def _run_with_timeout(fn: Any, timeout_s: float = 15.0, silence_stdout: bool = False) -> Any:
    """Run ``fn`` in a daemon thread with bounded wall-clock wait (never blocks process exit)."""
    result_box: dict[str, Any] = {}

    def _worker() -> None:
        tid = threading.get_ident()
        if silence_stdout:
            with _STDOUT_FILTER_LOCK:
                _install_stdout_filter_if_needed()
                _SILENCED_THREAD_IDS.add(tid)
        try:
            result_box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = exc
        finally:
            if silence_stdout:
                with _STDOUT_FILTER_LOCK:
                    _SILENCED_THREAD_IDS.discard(tid)
                    _restore_stdout_if_idle()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"Operation timed out after {timeout_s}s")
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + offset
    return total // 12, (total % 12) + 1


def _months_diff(ym1: int, ym2: int) -> int:
    y1, m1 = divmod(ym1, 100)
    y2, m2 = divmod(ym2, 100)
    return (y2 - y1) * 12 + (m2 - m1)


def _parse_fred_csv(csv_text: str, series_id: str | None = None) -> dict[str, Any]:
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty FRED CSV content")
    rows = list(csv.reader(io.StringIO(csv_text.strip())))
    if len(rows) < 2:
        raise ValueError("FRED CSV has fewer than 2 rows")
    header = [c.strip() for c in rows[0]]
    col_id = series_id or (header[1] if len(header) > 1 else "VALUE")
    observations: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        obs_date = row[0].strip()
        val = _to_float(row[1])
        if obs_date and val is not None:
            observations.append((obs_date, val))
    if not observations:
        raise ValueError(f"No valid observations in FRED CSV for {col_id}")
    latest_date, latest_val = observations[-1]
    prev_date, prev_val = observations[-2] if len(observations) >= 2 else (None, None)
    return {
        "series_id": col_id,
        "latest_date": latest_date,
        "latest_value": _round_finite(latest_val, 6),
        "previous_date": prev_date,
        "previous_value": _round_finite(prev_val, 6) if prev_val is not None else None,
        "observations_count": len(observations),
    }


def _fetch_fred_series(series_id: str, timeout_s: float = HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return _parse_fred_csv(resp.text, series_id=series_id)


# ---------------------------------------------------------------------------
# 1. Overseas Commodity Term Structure & Annualized Roll Yield (+ CN SC/TA)
# ---------------------------------------------------------------------------

def _observed_holiday(dt: date) -> date:
    if dt.weekday() == 5:
        return dt - timedelta(days=1)
    if dt.weekday() == 6:
        return dt + timedelta(days=1)
    return dt


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return n-th (positive) or last (n=-1) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    last_day = cal_lib.monthrange(year, month)[1]
    matches = [date(year, month, d) for d in range(1, last_day + 1) if date(year, month, d).weekday() == weekday]
    return matches[n - 1] if n > 0 else matches[n]


def _good_friday(year: int) -> date:
    # Anonymous Gregorian algorithm for Easter Sunday, minus 2 days
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _us_exchange_holidays(year: int) -> set[date]:
    """Deterministic US futures exchange holiday set (NYSE/CME/ICE US core schedule)."""
    holidays = {
        _observed_holiday(date(year, 1, 1)),            # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),           # MLK Day
        _nth_weekday_of_month(year, 2, 0, 3),           # Presidents' Day
        _good_friday(year),                             # Good Friday
        _nth_weekday_of_month(year, 5, 0, -1),          # Memorial Day
        _observed_holiday(date(year, 6, 19)),           # Juneteenth
        _observed_holiday(date(year, 7, 4)),            # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),           # Labor Day
        _nth_weekday_of_month(year, 11, 3, 4),          # Thanksgiving
        _observed_holiday(date(year, 12, 25)),          # Christmas Day
    }
    # Handle New Year's Day observed on Dec 31 of the preceding year
    if date(year + 1, 1, 1).weekday() == 5:
        holidays.add(date(year, 12, 31))
    return holidays


def _is_business_day(dt: date, holidays: set[date]) -> bool:
    return dt.weekday() < 5 and dt not in holidays


def _nth_business_day_of_month(year: int, month: int, n: int, holidays: set[date] | None = None) -> date:
    """Return the n-th business day of (year, month): positive 1-indexed from start, negative from end."""
    hol = holidays if holidays is not None else set()
    last_day = cal_lib.monthrange(year, month)[1]
    bdays = [date(year, month, d) for d in range(1, last_day + 1) if _is_business_day(date(year, month, d), hol)]
    return bdays[n - 1] if n > 0 else bdays[n]


def _prev_business_days(dt: date, n: int, holidays: set[date] | None = None) -> date:
    hol = holidays if holidays is not None else set()
    cur = dt
    remaining = n
    while remaining > 0:
        cur -= timedelta(days=1)
        if _is_business_day(cur, hol):
            remaining -= 1
    return cur


def estimate_contract_last_trading_day(root: str, year: int, month: int) -> date:
    """Compute exchange-rule last trading day (holiday-aware for US/ICE/CME) for (root, year, month)."""
    r = (root or "").strip().upper()
    if r == "BZ":
        # CME Rule 698 / ICE Brent: last business day of M - 2; except if that day is the business day
        # immediately preceding New Year's Day (Dec 31 when weekday), trading ends the preceding business day.
        y2, m2 = _add_months(year, month, -2)
        hol = _us_exchange_holidays(y2)
        last_bday = _nth_business_day_of_month(y2, m2, -1, hol)
        if m2 == 12 and last_bday == date(y2, 12, 31):
            return _prev_business_days(last_bday, 1, hol)
        return last_bday
    if r == "CL":
        # NYMEX WTI: 3rd US business day prior to 25th calendar day of M - 1 (or prior bday if 25th non-bday)
        y1, m1 = _add_months(year, month, -1)
        hol = _us_exchange_holidays(y1)
        d25 = date(y1, m1, 25)
        if not _is_business_day(d25, hol):
            d25 = _prev_business_days(d25, 1, hol)
        return _prev_business_days(d25, 3, hol)
    if r == "NG":
        # NYMEX Henry Hub: 3rd-to-last US business day of M - 1
        y1, m1 = _add_months(year, month, -1)
        return _nth_business_day_of_month(y1, m1, -3, _us_exchange_holidays(y1))
    if r == "SB":
        # ICE Sugar No. 11: last US business day of M - 1
        y1, m1 = _add_months(year, month, -1)
        return _nth_business_day_of_month(y1, m1, -1, _us_exchange_holidays(y1))
    if r in {"HG", "GC", "SI"}:
        # COMEX metals: 3rd-to-last US business day of delivery month M
        return _nth_business_day_of_month(year, month, -3, _us_exchange_holidays(year))
    if r in {"ZC", "ZS", "ZW"}:
        # CBOT grains: US business day prior to 15th calendar day of delivery month M
        return _prev_business_days(date(year, month, 15), 1, _us_exchange_holidays(year))
    if r == "KC":
        # ICE Coffee C: 1 business day prior to Last Notice Day (which is 7 bdays before last bday) -> -9 US bday
        return _nth_business_day_of_month(year, month, -9, _us_exchange_holidays(year))
    if r == "CT":
        # ICE Cotton No. 2: 17th US business day from end of delivery month M
        return _nth_business_day_of_month(year, month, -17, _us_exchange_holidays(year))
    if r == "SC":
        # INE Crude SC: last trading day of M - 1 (baseline weekday schedule; CN holiday buffer handled in fetcher)
        y1, m1 = _add_months(year, month, -1)
        return _nth_business_day_of_month(y1, m1, -1)
    if r == "TA":
        # CZCE PTA: 10th trading day of delivery month M (baseline weekday schedule; CN holiday buffer handled in fetcher)
        return _nth_business_day_of_month(year, month, 10)
    return _nth_business_day_of_month(year, month, -1)


def build_overseas_contract_candidates(
    symbol: str,
    ref_date: date | None = None,
    max_contracts: int = 6,
) -> list[dict[str, Any]]:
    """Generate unexpired upcoming overseas futures contract tickers using exchange last-trading-day rules.

    First filters listed delivery months by ``estimate_contract_last_trading_day(root, y, m) >= anchor``,
    then samples offsets ``[0, 1, 2, 3, 6, 12]`` from the earliest active month for monthly contracts
    (CL/BZ/NG) or takes the next ``max_contracts`` liquid cycle months for seasonal/metals contracts.
    """
    sym = (symbol or "").strip().upper()
    spec = OVERSEAS_TERM_STRUCTURE_SPECS.get(sym)
    if not spec or max_contracts <= 0:
        return []

    if ref_date is None:
        anchor = datetime.now(timezone.utc).date()
    elif isinstance(ref_date, datetime):
        anchor = ref_date.date()
    else:
        anchor = ref_date

    root = spec["root"]
    suffix = spec["suffix"]
    months_cycle = sorted(set(int(m) for m in spec["months"] if 1 <= int(m) <= 12))
    if not months_cycle:
        return []

    # Find the earliest unexpired contract month according to exchange rulebook
    cur_y, cur_m = anchor.year, anchor.month
    first_active: tuple[int, int] | None = None
    for _ in range(36):
        if cur_m in months_cycle and estimate_contract_last_trading_day(root, cur_y, cur_m) >= anchor:
            first_active = (cur_y, cur_m)
            break
        cur_y, cur_m = _add_months(cur_y, cur_m, 1)
    if first_active is None:
        return []

    start_y, start_m = first_active
    ym_pairs: list[tuple[int, int]] = []
    if len(months_cycle) == 12:
        offsets = [0, 1, 2, 3, 6, 12]
        next_off = 15
        while len(offsets) < max_contracts:
            offsets.append(next_off)
            next_off += 3
        for off in offsets[:max_contracts]:
            ym_pairs.append(_add_months(start_y, start_m, off))
    else:
        cy, cm = start_y, start_m
        guard = 0
        while len(ym_pairs) < max_contracts and guard < 120:
            if cm in months_cycle and estimate_contract_last_trading_day(root, cy, cm) >= anchor:
                ym_pairs.append((cy, cm))
            cy, cm = _add_months(cy, cm, 1)
            guard += 1

    candidates: list[dict[str, Any]] = []
    for y, m in ym_pairs:
        mcode = FUTURES_MONTH_CODES[m]
        yy = y % 100
        short_code = f"{root}{mcode}{yy:02d}"
        ticker = f"{short_code}{suffix}"
        candidates.append(
            {
                "ticker": ticker,
                "contract": ticker,
                "short_code": short_code,
                "root": root,
                "year": y,
                "month": m,
                "month_code": mcode,
                "expiry_ym": y * 100 + m,
                "est_last_trading_day": estimate_contract_last_trading_day(root, y, m).isoformat(),
            }
        )
    return candidates


def summarize_futures_curve(
    points: list[dict[str, Any]],
    unit: str | None = None,
) -> dict[str, Any]:
    """Summarize futures term structure, spreads, and annualized roll yield.

    Each input point must include ``contract``, ``expiry_ym`` (int ``YYYYMM``),
    ``price`` (positive finite float), ``date`` (str), and optional ``volume``.
    Positive roll yield = Backwardation (long roll tailwind / short roll drag).
    Negative roll yield = Contango (short roll tailwind / long roll drag).
    """
    if not isinstance(points, list) or not points:
        raise ValueError("Futures curve points must be a non-empty list")

    dedup: dict[int, dict[str, Any]] = {}
    for pt in points:
        if not isinstance(pt, dict):
            continue
        contract = str(pt.get("contract") or "").strip()
        expiry_ym = _to_int(pt.get("expiry_ym"))
        price = _to_float(pt.get("price"))
        if not contract or expiry_ym is None or price is None or price <= 0:
            continue
        year_part, month_part = divmod(expiry_ym, 100)
        if year_part < 1990 or not (1 <= month_part <= 12):
            continue
        pt_date = _to_iso_date(pt.get("date")) or (str(pt.get("date")).strip() if pt.get("date") else None)
        vol = _to_float(pt.get("volume"))
        cleaned_pt = {
            "contract": contract,
            "expiry_ym": expiry_ym,
            "price": _round_finite(price, 4),
            "date": pt_date,
            "volume": _round_finite(vol, 2) if vol is not None else None,
        }
        existing = dedup.get(expiry_ym)
        if existing is None or (pt_date or "") >= (existing.get("date") or ""):
            dedup[expiry_ym] = cleaned_pt

    if not dedup:
        raise ValueError("No valid futures curve points with positive finite prices")

    ordered = [dedup[k] for k in sorted(dedup.keys())]
    front = ordered[0]
    front_price = float(front["price"])

    if len(ordered) < 2:
        return _sanitize_for_json(
            {
                "unit": unit,
                "contracts_count": len(ordered),
                "structure": "insufficient_contracts",
                "front_contract": front["contract"],
                "front_price": front["price"],
                "second_contract": None,
                "second_price": None,
                "far_contract": None,
                "far_price": None,
                "front_second_spread": None,
                "front_second_spread_pct": None,
                "front_far_spread": None,
                "front_far_spread_pct": None,
                "months_front_to_second": None,
                "months_front_to_far": None,
                "annualized_roll_yield_near_pct": None,
                "annualized_roll_yield_curve_pct": None,
                "roll_yield_note": "有效合约不足2个，无法判断期限结构与年化展期收益。",
                "contracts": ordered,
            }
        )

    second = ordered[1]
    far = ordered[-1]
    second_price = float(second["price"])
    far_price = float(far["price"])

    months_near = _months_diff(front["expiry_ym"], second["expiry_ym"])
    months_far = _months_diff(front["expiry_ym"], far["expiry_ym"])

    front_second_spread = _round_finite(front_price - second_price, 4)
    front_second_spread_pct = (
        _round_finite((front_price / second_price - 1.0) * 100.0, 4) if second_price > 0 else None
    )
    front_far_spread = _round_finite(front_price - far_price, 4)
    front_far_spread_pct = (
        _round_finite((front_price / far_price - 1.0) * 100.0, 4) if far_price > 0 else None
    )

    ann_roll_near = (
        _round_finite(((front_price / second_price) - 1.0) * (12.0 / months_near) * 100.0, 3)
        if second_price > 0 and months_near > 0
        else None
    )
    ann_roll_curve = (
        _round_finite(((front_price / far_price) - 1.0) * (12.0 / months_far) * 100.0, 3)
        if far_price > 0 and months_far > 0
        else None
    )

    if front_price > far_price * 1.002:
        structure = "Backwardation (现货升水/多头正展期)"
        rate_txt = f"约 +{ann_roll_curve:.2f}%/年" if ann_roll_curve is not None else "正值"
        near_txt = f"（近月段年化 {ann_roll_near:+.2f}%）" if ann_roll_near is not None else ""
        roll_note = (
            f"首尾合约呈近高远低现货升水结构，全曲线年化展期收益{rate_txt}{near_txt}："
            "对多头构成正展期顺风（移仓赚贴水），对空头构成持续展期成本。"
        )
    elif front_price < far_price * 0.998:
        structure = "Contango (远月升水/空头正展期)"
        rate_txt = f"约 {ann_roll_curve:.2f}%/年" if ann_roll_curve is not None else "负值"
        near_txt = f"（近月段年化 {ann_roll_near:+.2f}%）" if ann_roll_near is not None else ""
        roll_note = (
            f"首尾合约呈近低远高远月升水结构，全曲线年化展期收益{rate_txt}{near_txt}："
            "对多头构成负展期损耗（移仓亏升水），对空头构成正展期顺风。"
        )
    else:
        structure = "Flat (首尾平坦)"
        if ann_roll_near is not None and abs(ann_roll_near) >= 5.0:
            roll_note = (
                f"首尾合约价差在±0.2%以内（远端整体平坦），但近月段存在明显结构倾斜"
                f"（近月年化展期 {ann_roll_near:+.2f}%），近月移仓损益不可忽视。"
            )
        else:
            roll_note = "首尾合约价差在±0.2%以内且近月段温和，多空展期成本/收益影响轻微。"

    return _sanitize_for_json(
        {
            "unit": unit,
            "contracts_count": len(ordered),
            "structure": structure,
            "front_contract": front["contract"],
            "front_price": front["price"],
            "second_contract": second["contract"],
            "second_price": second["price"],
            "far_contract": far["contract"],
            "far_price": far["price"],
            "front_second_spread": front_second_spread,
            "front_second_spread_pct": front_second_spread_pct,
            "front_far_spread": front_far_spread,
            "front_far_spread_pct": front_far_spread_pct,
            "months_front_to_second": months_near,
            "months_front_to_far": months_far,
            "annualized_roll_yield_near_pct": ann_roll_near,
            "annualized_roll_yield_curve_pct": ann_roll_curve,
            "roll_yield_note": roll_note,
            "contracts": ordered,
        }
    )


MAX_CURVE_STALE_DAYS = 10


def _extract_fresh_yf_history(
    hist: pd.DataFrame | None,
    anchor: date,
    max_stale_days: int = MAX_CURVE_STALE_DAYS,
    allow_next_day_tz: bool = False,
) -> dict[date, tuple[float, float | None]]:
    """Return {bar_date: (close, volume)} filtered strictly to fresh bars relative to anchor."""
    if hist is None or not isinstance(hist, pd.DataFrame) or hist.empty or "Close" not in hist.columns:
        return {}
    cutoff = anchor - timedelta(days=max_stale_days)
    upper = anchor + timedelta(days=1) if allow_next_day_tz else anchor
    bars: dict[date, tuple[float, float | None]] = {}
    for idx, row in hist.iterrows():
        dt_obj = _parse_date_obj(idx)
        if dt_obj is None or dt_obj < cutoff or dt_obj > upper:
            continue
        price = _to_float(row.get("Close"))
        if price is None or price <= 0:
            continue
        vol = _to_float(row.get("Volume")) if "Volume" in hist.columns else None
        bars[dt_obj] = (price, vol)
    return bars


def _extract_fresh_sina_history(
    df: pd.DataFrame | None,
    anchor: date,
    max_stale_days: int = MAX_CURVE_STALE_DAYS,
    allow_next_day_tz: bool = False,
) -> dict[date, tuple[float, float | None]]:
    """Return {bar_date: (close, volume)} from Sina daily DataFrame filtered to fresh bars."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    cutoff = anchor - timedelta(days=max_stale_days)
    upper = anchor + timedelta(days=1) if allow_next_day_tz else anchor
    tail = df.tail(15)
    bars: dict[date, tuple[float, float | None]] = {}
    for _, row in tail.iterrows():
        dt_obj = _parse_date_obj(row.get("date"))
        if dt_obj is None or dt_obj < cutoff or dt_obj > upper:
            continue
        price = _to_float(row.get("close")) or _to_float(row.get("settle"))
        if price is None or price <= 0:
            continue
        vol = _to_float(row.get("volume"))
        bars[dt_obj] = (price, vol)
    return bars


def _align_curve_legs(
    legs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, bool, str | None]:
    """Align fresh contract legs to the latest common trading date covering the active front leg.

    Never silently drops an active front leg just because back legs share an older date.
    When no common date covers the front leg plus >=1 other leg, preserves the front leg
    at its own date, marks ``is_aligned=False``, and emits a warning so cross-date spreads
    are nulled out rather than published as valid same-day spreads.
    """
    valid_legs = [lg for lg in legs if lg.get("bars")]
    if not valid_legs:
        return [], None, False, None

    valid_legs.sort(key=lambda lg: lg["expiry_ym"])
    front_leg = valid_legs[0]
    front_dates = set(front_leg["bars"].keys())

    date_counts: dict[date, list[dict[str, Any]]] = {}
    for lg in valid_legs:
        for dt_obj in lg["bars"]:
            date_counts.setdefault(dt_obj, []).append(lg)

    # First priority: common trading dates with >=2 legs that INCLUDE the active front leg
    front_common = [dt for dt in front_dates if len(date_counts.get(dt, [])) >= 2]
    if front_common:
        recent_common = sorted(front_common, reverse=True)[:3]
        best_date = max(recent_common, key=lambda dt: (len(date_counts[dt]), dt))
        points: list[dict[str, Any]] = []
        for lg in date_counts[best_date]:
            px, vol = lg["bars"][best_date]
            points.append(
                {
                    "contract": lg["contract"],
                    "expiry_ym": lg["expiry_ym"],
                    "price": px,
                    "date": best_date.isoformat(),
                    "volume": vol,
                }
            )
        return points, best_date.isoformat(), True, None

    # Front leg has NO shared trading date with any other leg: keep front leg + align back legs,
    # flag misaligned so cross-date near/curve spreads & roll yields are nulled out safely.
    points = []
    dates_used: set[date] = set()
    front_dt = max(front_leg["bars"].keys())
    front_px, front_vol = front_leg["bars"][front_dt]
    points.append(
        {
            "contract": front_leg["contract"],
            "expiry_ym": front_leg["expiry_ym"],
            "price": front_px,
            "date": front_dt.isoformat(),
            "volume": front_vol,
        }
    )
    dates_used.add(front_dt)

    other_legs = valid_legs[1:]
    if other_legs:
        other_counts: dict[date, list[dict[str, Any]]] = {}
        for lg in other_legs:
            for dt_obj in lg["bars"]:
                other_counts.setdefault(dt_obj, []).append(lg)
        other_common = [dt for dt, lgs in other_counts.items() if len(lgs) >= 2]
        if other_common:
            back_best = max(sorted(other_common, reverse=True)[:3], key=lambda dt: (len(other_counts[dt]), dt))
            for lg in other_counts[back_best]:
                px, vol = lg["bars"][back_best]
                points.append(
                    {
                        "contract": lg["contract"],
                        "expiry_ym": lg["expiry_ym"],
                        "price": px,
                        "date": back_best.isoformat(),
                        "volume": vol,
                    }
                )
                dates_used.add(back_best)
        else:
            for lg in other_legs:
                latest_dt = max(lg["bars"].keys())
                px, vol = lg["bars"][latest_dt]
                points.append(
                    {
                        "contract": lg["contract"],
                        "expiry_ym": lg["expiry_ym"],
                        "price": px,
                        "date": latest_dt.isoformat(),
                        "volume": vol,
                    }
                )
                dates_used.add(latest_dt)

    newest_dt = max(dates_used)
    is_aligned = len(dates_used) == 1
    warn = None
    if not is_aligned:
        span_days = (newest_dt - min(dates_used)).days
        warn = (
            f"有效近月合约 {front_leg['contract']}({front_dt.isoformat()}) 与其余合约无共同交易日"
            f"（最大相差 {span_days} 天），已置空跨日期价差与展期收益"
        )
    return points, newest_dt.isoformat(), is_aligned, warn


def _null_misaligned_curve_spreads(summary: dict[str, Any]) -> None:
    """Clear cross-date spread & roll-yield metrics when front vs second/far quote dates differ."""
    contracts = summary.get("contracts") or []
    if len(contracts) < 2:
        return
    front_dt = contracts[0].get("date")
    second_dt = contracts[1].get("date")
    far_dt = contracts[-1].get("date")
    if front_dt != second_dt:
        summary["front_second_spread"] = None
        summary["front_second_spread_pct"] = None
        summary["annualized_roll_yield_near_pct"] = None
    if front_dt != far_dt:
        summary["front_far_spread"] = None
        summary["front_far_spread_pct"] = None
        summary["annualized_roll_yield_curve_pct"] = None
        summary["structure"] = "unaligned_dates"
        summary["roll_yield_note"] = "首尾合约交易日期不一致，无法计算同日跨期价差与年化展期收益。"


def fetch_overseas_term_structure(
    symbol: str,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Fetch sampled overseas futures term structure and annualized roll yield via yfinance."""
    sym = (symbol or "").strip().upper()
    spec = OVERSEAS_TERM_STRUCTURE_SPECS.get(sym)
    if not spec:
        return {"status": "skipped", "reason": f"No overseas term structure spec for {symbol}"}
    if yf is None:
        raise RuntimeError("yfinance is not installed")

    explicit_ref = ref_date is not None
    if ref_date is None:
        anchor = datetime.now(timezone.utc).date()
    elif isinstance(ref_date, datetime):
        anchor = ref_date.date()
    else:
        anchor = ref_date

    candidates = build_overseas_contract_candidates(sym, ref_date=anchor, max_contracts=6)
    expected_front = candidates[0]["ticker"] if candidates else None
    legs: list[dict[str, Any]] = []
    for cand in candidates:
        ticker = cand["ticker"]
        try:
            hist = _run_with_timeout(
                lambda t=ticker: yf.Ticker(t).history(period="10d"),
                timeout_s=10.0,
            )
            bars = _extract_fresh_yf_history(hist, anchor=anchor, allow_next_day_tz=not explicit_ref)
            if not bars:
                continue
            legs.append({"contract": ticker, "expiry_ym": cand["expiry_ym"], "bars": bars})
        except Exception:  # noqa: BLE001
            continue

    points, aligned_date, is_aligned, mismatch_warn = _align_curve_legs(legs)
    if not points:
        raise ValueError(
            f"No fresh active contract quotes (within {MAX_CURVE_STALE_DAYS}d of {anchor.isoformat()}) "
            f"fetched for overseas term structure {sym}"
        )

    summary = summarize_futures_curve(points, unit=spec["unit"])
    if not is_aligned:
        _null_misaligned_curve_spreads(summary)
    summary["symbol"] = sym
    summary["quote_date"] = aligned_date
    summary["quote_dates_aligned"] = is_aligned
    summary["curve_sampling"] = "sampled_active_contracts"
    summary["candidates_checked"] = len(candidates)

    warnings: list[str] = []
    if mismatch_warn:
        warnings.append(mismatch_warn)
    if expected_front and summary.get("front_contract") != expected_front:
        summary["missing_front_contract"] = expected_front
        warnings.append(
            f"预期活跃近月合约 {expected_front} 缺少新鲜报价，曲线首腿已顺延至 {summary.get('front_contract')}，"
            "近月展期收益与首行月差未涵盖真正近月"
        )
    if warnings:
        summary["status"] = "partial"
        summary["quote_date_mismatch_warning"] = "；".join(warnings)
    return _sanitize_for_json(summary)


def fetch_cn_daily_term_structure(
    prefix: str,
    active_months: list[int] | None = None,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Fetch sampled domestic CN futures term structure via Sina daily contract history (whitelisted: SC, TA)."""
    pref = (prefix or "").strip().upper()
    default_cfg = CN_DAILY_TERM_STRUCTURE_DEFAULTS.get(pref)
    if default_cfg is None:
        return {"status": "skipped", "reason": f"CN daily term structure prefix not in whitelist: {prefix}"}
    if ak is None:
        raise RuntimeError("akshare is not installed")

    explicit_ref = ref_date is not None
    if ref_date is None:
        anchor = datetime.now(timezone.utc).date()
    elif isinstance(ref_date, datetime):
        anchor = ref_date.date()
    else:
        anchor = ref_date

    unit = default_cfg["unit"]
    # Find earliest strictly unexpired contract (expected_front) and allow a 10-day China public holiday
    # lookback buffer (e.g. National Day Oct 1-7 / Spring Festival shifting TA 10th trading day) so a still-trading
    # delivery month like TA2610 after National Day is checked on real exchange trading bars.
    cur_y, cur_m = anchor.year, anchor.month
    first_active: tuple[int, int] | None = None
    holiday_boundary_ym: tuple[int, int] | None = None
    for _ in range(24):
        est_ltd = estimate_contract_last_trading_day(pref, cur_y, cur_m)
        if est_ltd >= anchor:
            first_active = (cur_y, cur_m)
            break
        if pref == "TA" and cur_m in {1, 2, 5, 10} and (anchor - est_ltd).days <= 10:
            holiday_boundary_ym = (cur_y, cur_m)
        cur_y, cur_m = _add_months(cur_y, cur_m, 1)
    if first_active is None:
        return {"status": "skipped", "reason": f"Unable to determine active contract month for {pref}"}
    start_y, start_m = first_active
    expected_front = f"{pref}{start_y % 100:02d}{start_m:02d}"

    ym_pairs: list[tuple[int, int]] = []
    if holiday_boundary_ym is not None:
        ym_pairs.append(holiday_boundary_ym)
    if active_months is None and default_cfg.get("monthly_offsets"):
        for off in default_cfg["monthly_offsets"]:
            pair = _add_months(start_y, start_m, int(off))
            if pair not in ym_pairs:
                ym_pairs.append(pair)
    else:
        months_list = sorted(
            set(int(m) for m in (active_months if active_months is not None else default_cfg["active_months"]) if 1 <= int(m) <= 12)
        )
        if not months_list:
            return {"status": "skipped", "reason": f"No valid active months for {pref}"}
        max_legs = int(default_cfg.get("max_contracts", 5)) + (1 if holiday_boundary_ym else 0)
        if default_cfg.get("include_front_active_month") and (start_y, start_m) not in ym_pairs:
            ym_pairs.append((start_y, start_m))
        cy, cm = start_y, start_m
        guard = 0
        while len(ym_pairs) < max_legs and guard < 60:
            if cm in months_list and (cy, cm) not in ym_pairs and estimate_contract_last_trading_day(pref, cy, cm) >= anchor:
                ym_pairs.append((cy, cm))
            cy, cm = _add_months(cy, cm, 1)
            guard += 1

    boundary_sym = f"{pref}{holiday_boundary_ym[0] % 100:02d}{holiday_boundary_ym[1]:02d}" if holiday_boundary_ym else None
    legs: list[dict[str, Any]] = []
    for y, m in ym_pairs:
        contract_sym = f"{pref}{y % 100:02d}{m:02d}"
        try:
            df = _run_with_timeout(
                lambda s=contract_sym: ak.futures_zh_daily_sina(symbol=s),
                timeout_s=10.0,
                silence_stdout=True,
            )
            bars = _extract_fresh_sina_history(df, anchor=anchor, allow_next_day_tz=not explicit_ref)
            if not bars:
                continue
            legs.append({"contract": contract_sym, "expiry_ym": y * 100 + m, "bars": bars})
        except Exception:  # noqa: BLE001
            continue

    # If a holiday-boundary candidate exists (weekday LTD < anchor), keep it ONLY if it actually traded
    # on the same latest date as the rest of the curve (proving it is still actively trading after holidays).
    if boundary_sym and len(legs) >= 2:
        non_boundary_dates = [max(lg["bars"].keys()) for lg in legs if lg["contract"] != boundary_sym]
        if non_boundary_dates:
            latest_market_dt = max(non_boundary_dates)
            legs = [
                lg for lg in legs
                if lg["contract"] != boundary_sym or latest_market_dt in lg["bars"]
            ]

    points, aligned_date, is_aligned, mismatch_warn = _align_curve_legs(legs)
    if not points:
        raise ValueError(
            f"No fresh active Sina daily contracts (within {MAX_CURVE_STALE_DAYS}d of {anchor.isoformat()}) "
            f"fetched for CN prefix {pref}"
        )

    summary = summarize_futures_curve(points, unit=unit)
    if not is_aligned:
        _null_misaligned_curve_spreads(summary)
    summary["prefix"] = pref
    summary["source"] = "sina_futures_zh_daily"
    summary["quote_date"] = aligned_date
    summary["quote_dates_aligned"] = is_aligned
    summary["curve_sampling"] = "sampled_active_contracts"
    summary["candidates_checked"] = len(ym_pairs)

    warnings: list[str] = []
    if mismatch_warn:
        warnings.append(mismatch_warn)
    # Expected front is satisfied if either expected_front itself OR an actively trading holiday-extended boundary front is front_contract
    valid_fronts = {expected_front}
    if boundary_sym:
        valid_fronts.add(boundary_sym)
    if summary.get("front_contract") not in valid_fronts:
        summary["missing_front_contract"] = expected_front
        warnings.append(
            f"预期活跃近月合约 {expected_front} 缺少新鲜报价，曲线首腿已顺延至 {summary.get('front_contract')}，"
            "近月展期收益与首行月差未涵盖真正近月"
        )
    if warnings:
        summary["status"] = "partial"
        summary["quote_date_mismatch_warning"] = "；".join(warnings)
    return _sanitize_for_json(summary)


# ---------------------------------------------------------------------------
# 2. Cross-Market Spread & Import Parity (Strict Whitelist)
# ---------------------------------------------------------------------------

def _pick_positive_price(prices: dict[str, Any], *keys: str) -> float | None:
    if not isinstance(prices, dict):
        return None
    lower_map = {str(k).strip().upper(): v for k, v in prices.items()}
    for key in keys:
        cand = lower_map.get(key.upper())
        val = _to_float(cand)
        if val is not None and val > 0:
            return val
    return None


def compute_cross_market_spread(group: str, prices: dict[str, float]) -> dict[str, Any]:
    """Pure calculator for cross-market commodity spreads & China import parity."""
    grp = (group or "").strip().lower()
    if not isinstance(prices, dict):
        raise ValueError("prices must be a dictionary of symbol -> positive float")

    usdcny = _pick_positive_price(prices, "USDCNY=X", "USDCNY", "usdcny")

    if grp == "crude_oil":
        wti = _pick_positive_price(prices, "CL=F", "WTI", "cl")
        brent = _pick_positive_price(prices, "BZ=F", "BRENT", "bz")
        sc = _pick_positive_price(prices, "SC0", "SC", "sc")
        if wti is None and brent is None and sc is None:
            raise ValueError("Missing crude oil prices for cross-market spread")
        brent_minus_wti = _round_finite(brent - wti, 4) if (brent is not None and wti is not None) else None
        sc_usd_bbl = _round_finite(sc / usdcny, 4) if (sc is not None and usdcny is not None) else None
        sc_minus_brent = _round_finite(sc_usd_bbl - brent, 4) if (sc_usd_bbl is not None and brent is not None) else None
        sc_minus_wti = _round_finite(sc_usd_bbl - wti, 4) if (sc_usd_bbl is not None and wti is not None) else None
        sc_minus_brent_pct = (
            _round_finite((sc_usd_bbl / brent - 1.0) * 100.0, 3)
            if (sc_usd_bbl is not None and brent is not None and brent > 0)
            else None
        )
        if brent_minus_wti is None and sc_minus_brent is None and sc_minus_wti is None:
            raise ValueError("Insufficient crude oil legs/FX to compute any cross-market spread")
        return _sanitize_for_json(
            {
                "group": "crude_oil",
                "inputs": {
                    "wti_usd_bbl": _round_finite(wti, 4),
                    "brent_usd_bbl": _round_finite(brent, 4),
                    "sc_cny_bbl": _round_finite(sc, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "brent_minus_wti_usd_bbl": brent_minus_wti,
                "sc_usd_bbl": sc_usd_bbl,
                "sc_minus_brent_usd_bbl": sc_minus_brent,
                "sc_minus_wti_usd_bbl": sc_minus_wti,
                "sc_minus_brent_pct": sc_minus_brent_pct,
                "basis_note": "INE上海原油SC为保税交割（不含增值税与关税），折美元后可直接与Brent/WTI比较地域升贴水及运费差。",
            }
        )

    if grp == "copper":
        cu = _pick_positive_price(prices, "CU0", "CU", "cu_cny_mt")
        hg = _pick_positive_price(prices, "HG=F", "HG", "hg_usd_lb")
        if cu is None or hg is None or usdcny is None:
            raise ValueError("Copper spread requires positive CU0, HG=F, and USDCNY=X prices")
        comex_usd_mt = hg * LB_PER_MT
        shfe_usd_mt_tax_incl = cu / usdcny
        shfe_usd_mt_ex_vat = cu / CN_VAT_MULTIPLIER / usdcny
        spread_ex_vat_usd_mt = shfe_usd_mt_ex_vat - comex_usd_mt
        spread_ex_vat_pct = (shfe_usd_mt_ex_vat / comex_usd_mt - 1.0) * 100.0
        shfe_comex_ratio = cu / comex_usd_mt
        return _sanitize_for_json(
            {
                "group": "copper",
                "inputs": {
                    "cu_cny_mt": _round_finite(cu, 2),
                    "hg_usd_lb": _round_finite(hg, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "comex_usd_mt": _round_finite(comex_usd_mt, 2),
                "shfe_usd_mt_tax_incl": _round_finite(shfe_usd_mt_tax_incl, 2),
                "shfe_usd_mt_ex_vat": _round_finite(shfe_usd_mt_ex_vat, 2),
                "spread_ex_vat_usd_mt": _round_finite(spread_ex_vat_usd_mt, 2),
                "spread_ex_vat_pct": _round_finite(spread_ex_vat_pct, 3),
                "shfe_comex_ratio": _round_finite(shfe_comex_ratio, 4),
                "tax_note": "国内沪铜含13%增值税，未计运费/升贴水/清关杂费",
            }
        )

    if grp == "gold":
        au = _pick_positive_price(prices, "AU0", "AU", "au_cny_g")
        gc = _pick_positive_price(prices, "GC=F", "GC", "gc_usd_oz")
        if au is None or gc is None or usdcny is None:
            raise ValueError("Gold spread requires positive AU0, GC=F, and USDCNY=X prices")
        comex_cny_g = (gc / GRAMS_PER_TROY_OZ) * usdcny
        shfe_minus_comex_cny_g = au - comex_cny_g
        spread_pct = (au / comex_cny_g - 1.0) * 100.0
        return _sanitize_for_json(
            {
                "group": "gold",
                "inputs": {
                    "au_cny_g": _round_finite(au, 4),
                    "gc_usd_oz": _round_finite(gc, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "comex_cny_g": _round_finite(comex_cny_g, 4),
                "shfe_minus_comex_cny_g": _round_finite(shfe_minus_comex_cny_g, 4),
                "spread_pct": _round_finite(spread_pct, 3),
                "tax_note": "沪金期货盘面交易报价不含增值税（实物交割出库按投资性/非投资性用途适用财政部及税务总局黄金税收政策），价差反映境内外供需与进口配额松紧",
            }
        )

    if grp == "silver":
        ag = _pick_positive_price(prices, "AG0", "AG", "ag_cny_kg")
        si = _pick_positive_price(prices, "SI=F", "SI", "si_usd_oz")
        if ag is None or si is None or usdcny is None:
            raise ValueError("Silver spread requires positive AG0, SI=F, and USDCNY=X prices")
        comex_cny_kg_ex_vat = si * TROY_OZ_PER_KG * usdcny
        comex_cny_kg_tax_incl = comex_cny_kg_ex_vat * CN_VAT_MULTIPLIER
        shfe_minus_comex_tax_incl_cny_kg = ag - comex_cny_kg_tax_incl
        spread_pct = (ag / comex_cny_kg_tax_incl - 1.0) * 100.0
        return _sanitize_for_json(
            {
                "group": "silver",
                "inputs": {
                    "ag_cny_kg": _round_finite(ag, 2),
                    "si_usd_oz": _round_finite(si, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "comex_cny_kg_ex_vat": _round_finite(comex_cny_kg_ex_vat, 2),
                "comex_cny_kg_tax_incl": _round_finite(comex_cny_kg_tax_incl, 2),
                "shfe_minus_comex_tax_incl_cny_kg": _round_finite(shfe_minus_comex_tax_incl_cny_kg, 2),
                "spread_pct": _round_finite(spread_pct, 3),
                "tax_note": "国内沪银含13%增值税，已将COMEX折算价乘以1.13作含税对比，未计运费与升贴水",
            }
        )

    if grp == "sugar":
        sr = _pick_positive_price(prices, "SR0", "SR", "sr_cny_mt")
        sb = _pick_positive_price(prices, "SB=F", "SB", "sb_usc_lb")
        if sr is None or sb is None or usdcny is None:
            raise ValueError("Sugar spread requires positive SR0, SB=F, and USDCNY=X prices")
        ice_raw_fob_usd_mt = sb * 0.01 * LB_PER_MT
        ice_raw_fob_cny_mt = ice_raw_fob_usd_mt * usdcny
        czce_minus_ice_raw_fob_cny_mt = sr - ice_raw_fob_cny_mt
        ratio = sr / ice_raw_fob_cny_mt
        return _sanitize_for_json(
            {
                "group": "sugar",
                "inputs": {
                    "sr_cny_mt": _round_finite(sr, 2),
                    "sb_usc_lb": _round_finite(sb, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "ice_raw_fob_usd_mt": _round_finite(ice_raw_fob_usd_mt, 2),
                "ice_raw_fob_cny_mt": _round_finite(ice_raw_fob_cny_mt, 2),
                "czce_minus_ice_raw_fob_cny_mt": _round_finite(czce_minus_ice_raw_fob_cny_mt, 2),
                "ratio": _round_finite(ratio, 4),
                "cny_usd_ratio": _round_finite(sr / ice_raw_fob_usd_mt, 4),
                "basis_note": "仅作ICE11号原糖FOB期货汇率折算参考，未含巴西海运费、15%配额内/50%配额外关税、加工费及13%增值税",
            }
        )

    if grp == "cotton":
        cf = _pick_positive_price(prices, "CF0", "CF", "cf_cny_mt")
        ct = _pick_positive_price(prices, "CT=F", "CT", "ct_usc_lb")
        if cf is None or ct is None or usdcny is None:
            raise ValueError("Cotton spread requires positive CF0, CT=F, and USDCNY=X prices")
        ice_futures_usd_mt = ct * 0.01 * LB_PER_MT
        ice_futures_cny_mt = ice_futures_usd_mt * usdcny
        czce_minus_ice_futures_cny_mt = cf - ice_futures_cny_mt
        ratio = cf / ice_futures_cny_mt
        return _sanitize_for_json(
            {
                "group": "cotton",
                "inputs": {
                    "cf_cny_mt": _round_finite(cf, 2),
                    "ct_usc_lb": _round_finite(ct, 4),
                    "usdcny": _round_finite(usdcny, 5),
                },
                "ice_futures_usd_mt": _round_finite(ice_futures_usd_mt, 2),
                "ice_futures_cny_mt": _round_finite(ice_futures_cny_mt, 2),
                "czce_minus_ice_futures_cny_mt": _round_finite(czce_minus_ice_futures_cny_mt, 2),
                "ratio": _round_finite(ratio, 4),
                "cny_usd_ratio": _round_finite(cf / ice_futures_usd_mt, 4),
                "basis_note": "仅作ICE美棉2号期货（美国指定交割库实物交割）汇率折算参考，未含海运费、1%配额内/滑准税及9%增值税",
            }
        )

    raise ValueError(f"Unsupported cross-market spread group: {group}")


MAX_CROSS_MARKET_STALE_DAYS = 5


def _fetch_latest_yf_close(ticker: str, ref_date: date | None = None) -> tuple[float, str]:
    """Return latest yfinance close with date <= ref_date (or today+1d for live TZ), leaving staleness gating to caller."""
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    explicit_ref = ref_date is not None
    anchor = ref_date or datetime.now(timezone.utc).date()
    max_upper = anchor if explicit_ref else (anchor + timedelta(days=1))
    hist = _run_with_timeout(
        lambda: yf.Ticker(ticker).history(period="1mo"),
        timeout_s=10.0,
    )
    if hist is None or not isinstance(hist, pd.DataFrame) or hist.empty or "Close" not in hist.columns:
        raise ValueError(f"No yfinance close history found for {ticker}")
    best_dt: date | None = None
    best_px: float | None = None
    for idx, row in hist.iterrows():
        dt_obj = _parse_date_obj(idx)
        if dt_obj is None or dt_obj > max_upper:
            continue
        px = _to_float(row.get("Close"))
        if px is None or px <= 0:
            continue
        if best_dt is None or dt_obj >= best_dt:
            best_dt = dt_obj
            best_px = px
    if best_dt is None or best_px is None:
        raise ValueError(f"No valid yfinance close <= {max_upper.isoformat()} found for {ticker}")
    return best_px, best_dt.isoformat()


def _select_latest_sina_row(df: pd.DataFrame | None, max_upper: date) -> tuple[float, str] | None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    best_dt: date | None = None
    best_px: float | None = None
    date_col = "日期" if "日期" in df.columns else ("date" if "date" in df.columns else None)
    if date_col is None:
        return None
    for _, row in df.iterrows():
        dt_obj = _parse_date_obj(row.get(date_col))
        if dt_obj is None or dt_obj > max_upper:
            continue
        px = (
            _to_float(row.get("收盘价"))
            or _to_float(row.get("动态结算价"))
            or _to_float(row.get("close"))
            or _to_float(row.get("settle"))
        )
        if px is None or px <= 0:
            continue
        if best_dt is None or dt_obj >= best_dt:
            best_dt = dt_obj
            best_px = px
    if best_dt is None or best_px is None:
        return None
    return best_px, best_dt.isoformat()


def _fetch_latest_sina_main_close(cn_symbol: str, ref_date: date | None = None) -> tuple[float, str]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    explicit_ref = ref_date is not None
    anchor = ref_date or datetime.now(timezone.utc).date()
    max_upper = anchor if explicit_ref else (anchor + timedelta(days=1))
    start_str = (anchor - timedelta(days=30)).strftime("%Y%m%d")
    end_str = max_upper.strftime("%Y%m%d")

    last_err: Exception | None = None
    try:
        df = _run_with_timeout(
            lambda: ak.futures_main_sina(symbol=cn_symbol, start_date=start_str, end_date=end_str),
            timeout_s=12.0,
            silence_stdout=True,
        )
        selected = _select_latest_sina_row(df, max_upper=max_upper)
        if selected is not None:
            return selected
    except Exception as exc:  # noqa: BLE001
        last_err = exc

    try:
        df_fb = _run_with_timeout(
            lambda: ak.futures_zh_daily_sina(symbol=cn_symbol),
            timeout_s=12.0,
            silence_stdout=True,
        )
        selected_fb = _select_latest_sina_row(df_fb, max_upper=max_upper)
        if selected_fb is not None:
            return selected_fb
    except Exception as exc:  # noqa: BLE001
        last_err = exc

    if last_err is not None:
        raise last_err
    raise ValueError(f"No valid Sina main continuous close <= {max_upper.isoformat()} found for {cn_symbol}")


_GROUP_FETCH_LEGS: dict[str, tuple[list[str], list[str]]] = {
    "crude_oil": (["CL=F", "BZ=F", "USDCNY=X"], ["SC0"]),
    "copper": (["HG=F", "USDCNY=X"], ["CU0"]),
    "gold": (["GC=F", "USDCNY=X"], ["AU0"]),
    "silver": (["SI=F", "USDCNY=X"], ["AG0"]),
    "sugar": (["SB=F", "USDCNY=X"], ["SR0"]),
    "cotton": (["CT=F", "USDCNY=X"], ["CF0"]),
}


def fetch_cross_market_spread(
    symbol: str,
    known_prices: dict[str, float] | None = None,
    known_dates: dict[str, str] | None = None,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Fetch cross-market spread & import parity for whitelisted linked commodities only.

    Pass ``known_prices`` and ``known_dates`` (e.g. from ``market_snapshot``) to avoid
    redundantly re-fetching a fresh target leg. Stale ``known_prices`` (> 5d old relative
    to ``ref_date``) are ignored so legs are never mixed across months.
    """
    sym = (symbol or "").strip().upper()
    spec = CROSS_MARKET_SPREAD_SPECS.get(sym)
    if not spec:
        return {"status": "skipped", "reason": f"No cross-market spread mapping for {symbol}"}

    explicit_ref = ref_date is not None
    anchor = ref_date or datetime.now(timezone.utc).date()
    max_upper = anchor if explicit_ref else (anchor + timedelta(days=1))
    group = spec["group"]
    yf_legs, cn_legs = _GROUP_FETCH_LEGS[group]
    prices: dict[str, float] = {}
    quote_dates: dict[str, str] = {}
    leg_errors: dict[str, str] = {}
    stale_legs: dict[str, str] = {}

    known_px_upper = {str(k).strip().upper(): _to_float(v) for k, v in (known_prices or {}).items()}
    known_dt_upper = {str(k).strip().upper(): _parse_date_obj(v) for k, v in (known_dates or {}).items()}

    def _is_leg_fresh(leg_dt: date | None) -> bool:
        if leg_dt is None:
            return False
        if leg_dt > max_upper:
            return False
        return (anchor - leg_dt).days <= MAX_CROSS_MARKET_STALE_DAYS

    def _usable_known(leg_key: str) -> tuple[float, str] | None:
        ku = leg_key.upper()
        k_px = known_px_upper.get(ku)
        k_dt = known_dt_upper.get(ku)
        if k_px is None or k_px <= 0 or not _is_leg_fresh(k_dt):
            return None
        return k_px, k_dt.isoformat()

    for ticker in yf_legs:
        reused = _usable_known(ticker)
        if reused is not None:
            prices[ticker], quote_dates[ticker] = reused
            continue
        try:
            px, dt_iso = _fetch_latest_yf_close(ticker, ref_date=ref_date)
            dt_obj = _parse_date_obj(dt_iso)
            if not _is_leg_fresh(dt_obj):
                stale_legs[ticker] = f"quote date {dt_iso} exceeds {MAX_CROSS_MARKET_STALE_DAYS}d freshness limit vs {anchor.isoformat()}"
                continue
            prices[ticker] = px
            quote_dates[ticker] = dt_iso
        except Exception as exc:  # noqa: BLE001
            leg_errors[ticker] = str(exc)

    for cn_sym in cn_legs:
        reused = _usable_known(cn_sym)
        if reused is not None:
            prices[cn_sym], quote_dates[cn_sym] = reused
            continue
        try:
            px, dt_iso = _fetch_latest_sina_main_close(cn_sym, ref_date=ref_date)
            dt_obj = _parse_date_obj(dt_iso)
            if not _is_leg_fresh(dt_obj):
                stale_legs[cn_sym] = f"quote date {dt_iso} exceeds {MAX_CROSS_MARKET_STALE_DAYS}d freshness limit vs {anchor.isoformat()}"
                continue
            prices[cn_sym] = px
            quote_dates[cn_sym] = dt_iso
        except Exception as exc:  # noqa: BLE001
            leg_errors[cn_sym] = str(exc)

    computed = compute_cross_market_spread(group, prices)
    computed["symbol"] = sym
    computed["quote_dates"] = quote_dates
    computed["contract_basis"] = "continuous_front_or_dominant_approx"

    parsed_dates = [d for d in (_parse_date_obj(v) for v in quote_dates.values()) if d is not None]
    date_lag_days = (max(parsed_dates) - min(parsed_dates)).days if len(parsed_dates) >= 2 else None
    computed["quote_date_lag_days"] = date_lag_days

    warnings: list[str] = []
    if stale_legs:
        computed["stale_legs"] = stale_legs
        warnings.append(f"以下腿报价超过 {MAX_CROSS_MARKET_STALE_DAYS} 天时效上限已被剔除：{', '.join(stale_legs.keys())}")
    if date_lag_days is not None and date_lag_days > MAX_CROSS_MARKET_STALE_DAYS:
        warnings.append(
            f"各腿报价日期最大相差 {date_lag_days} 天（超过{MAX_CROSS_MARKET_STALE_DAYS}天），跨市价差仅作参考，不可直接作为实盘套利依据"
        )
    if len(parsed_dates) < len(prices):
        warnings.append("部分腿缺少有效报价日期，未能验证日期一致性")

    if leg_errors:
        computed["status"] = "partial"
        computed["leg_errors"] = leg_errors
    if warnings:
        computed["status"] = "partial"
        computed["alignment_warning"] = "；".join(warnings)
    computed["contract_alignment_note"] = (
        "当前基于海内外主力/近月连续合约近似比价（交割月及交易时段可能存在小幅错位），"
        "第四层实盘跨市套利须严格对齐同交割月合约与同时点汇率。"
    )
    return _sanitize_for_json(computed)


# ---------------------------------------------------------------------------
# 3. Baltic Freight Indices (Strict Whitelist Routing)
# ---------------------------------------------------------------------------

def parse_freight_index_df(df: pd.DataFrame, keys: list[str]) -> dict[str, Any]:
    """Parse AkShare Baltic freight index DataFrame (newest- or oldest-first) for requested keys."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("Empty Baltic freight index DataFrame")
    if not keys:
        raise ValueError("Requested freight keys must be non-empty")

    date_col = "截止日期" if "截止日期" in df.columns else df.columns[0]
    work = df.copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work = work.dropna(subset=[date_col]).sort_values(date_col, ascending=True)
    work = work.drop_duplicates(subset=[date_col], keep="last").reset_index(drop=True)
    if work.empty:
        raise ValueError("No valid dated rows in Baltic freight index DataFrame")

    out: dict[str, Any] = {}
    for raw_key in keys:
        key = str(raw_key).strip().upper()
        col_name = FREIGHT_COLUMN_MAP.get(key)
        if not col_name or col_name not in work.columns:
            continue
        series_num = pd.to_numeric(work[col_name], errors="coerce")
        valid_mask = series_num.notna() & np.isfinite(series_num.to_numpy(dtype=float, na_value=np.nan))
        sub = work.loc[valid_mask, [date_col]].copy()
        sub["val"] = series_num.loc[valid_mask].astype(float)
        sub = sub.reset_index(drop=True)
        if sub.empty:
            continue

        latest_date = sub[date_col].iloc[-1].date().isoformat()
        latest_val = float(sub["val"].iloc[-1])

        def _pct_chg(lookback: int) -> float | None:
            if len(sub) > lookback:
                prev = float(sub["val"].iloc[-1 - lookback])
                if math.isfinite(prev) and prev != 0:
                    return _round_finite((latest_val / prev - 1.0) * 100.0, 2)
            return None

        out[key] = {
            "column": col_name,
            "latest_date": latest_date,
            "latest_value": _round_finite(latest_val, 4),
            "chg_5d_pct": _pct_chg(5),
            "chg_20d_pct": _pct_chg(20),
            "chg_60d_pct": _pct_chg(60),
            "observations": len(sub),
        }

    if not out:
        raise ValueError(f"No valid Baltic freight index observations parsed for keys={keys}")
    return _sanitize_for_json(out)


def fetch_freight_indices(symbol: str) -> dict[str, Any]:
    """Fetch only the Baltic freight indices mapped to ``symbol`` (zero call if unmapped)."""
    sym = (symbol or "").strip().upper()
    keys = FREIGHT_ROUTING.get(sym)
    if not keys:
        return {"status": "skipped", "reason": f"No Baltic freight routing for {symbol}"}
    if ak is None:
        raise RuntimeError("akshare is not installed")

    df = _run_with_timeout(
        lambda: ak.macro_china_freight_index(),
        timeout_s=HTTP_TIMEOUT,
        silence_stdout=True,
    )
    indices = parse_freight_index_df(df, keys)
    return _sanitize_for_json(
        {
            "symbol": sym,
            "requested_keys": list(keys),
            "indices": indices,
        }
    )


# ---------------------------------------------------------------------------
# 4. FX Dedicated Research Data (CFTC TFF + Sovereign Rate Differentials)
# ---------------------------------------------------------------------------

def parse_cftc_tff(
    csv_text: str,
    market_query: str | list[str] | None = None,
    contract_code: str | None = None,
    futures_direction: str = "same",
    note: str | None = None,
) -> dict[str, Any]:
    """Parse CFTC Traders in Financial Futures (FinFutWk.txt) with FX pair sign alignment.

    Verified FinFutWk column indices:
      [0] Market_and_Exchange_Names, [2] Report_Date_as_YYYY-MM-DD, [3] CFTC_Contract_Market_Code,
      [7] Open_Interest_All, [8]/[9] Dealer_Long/Short, [11]/[12] Asset_Mgr_Long/Short,
      [14]/[15] Lev_Money_Long/Short.
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty CFTC TFF CSV text")

    rows = [
        r
        for r in csv.reader(io.StringIO(csv_text))
        if len(r) >= 16 and r[0].strip().upper() != "MARKET_AND_EXCHANGE_NAMES"
    ]
    candidates: list[list[str]] = []

    if contract_code:
        code_upper = contract_code.strip().upper()
        candidates = [r for r in rows if r[3].strip().upper() == code_upper]
        if not candidates:
            raise ValueError(
                f"Requested CFTC TFF contract_code={contract_code!r} not found in FinFutWk data "
                "(refusing silent fallback to a different contract code)"
            )
    elif market_query:
        patterns = [market_query] if isinstance(market_query, str) else list(market_query)
        patterns_upper = [p.strip().upper() for p in patterns if p and p.strip()]
        for pat in patterns_upper:
            pat_matches = [r for r in rows if pat in r[0].strip().upper()]
            if pat_matches:
                candidates = pat_matches
                break

    if not candidates:
        raise ValueError(f"No CFTC TFF match found for code={contract_code!r}, query={market_query!r}")

    candidates.sort(key=lambda r: r[2].strip(), reverse=True)
    matched = candidates[0]

    oi = _to_int(matched[7])
    dealer_long = _to_int(matched[8])
    dealer_short = _to_int(matched[9])
    am_long = _to_int(matched[11])
    am_short = _to_int(matched[12])
    lev_long = _to_int(matched[14])
    lev_short = _to_int(matched[15])

    def _bucket(lng: int | None, sht: int | None) -> dict[str, Any]:
        net = (lng - sht) if (lng is not None and sht is not None) else None
        net_pct = _round_finite(net / oi * 100.0, 2) if (net is not None and oi) else None
        return {
            "long": lng,
            "short": sht,
            "net": net,
            "net_pct_oi": net_pct,
        }

    dealer = _bucket(dealer_long, dealer_short)
    asset_mgr = _bucket(am_long, am_short)
    lev_funds = _bucket(lev_long, lev_short)

    direction = (futures_direction or "same").strip().lower()
    sign = -1 if direction == "inverse" else 1
    lev_net = lev_funds["net"]
    am_net = asset_mgr["net"]
    pair_aligned_lev_net = (lev_net * sign) if lev_net is not None else None
    pair_aligned_lev_net_pct = (
        _round_finite(pair_aligned_lev_net / oi * 100.0, 2)
        if (pair_aligned_lev_net is not None and oi)
        else None
    )
    pair_aligned_am_net = (am_net * sign) if am_net is not None else None

    return _sanitize_for_json(
        {
            "report_type": "CFTC Traders in Financial Futures (TFF Futures-Only)",
            "contract_code": matched[3].strip(),
            "market_name": matched[0].strip(),
            "report_date": matched[2].strip(),
            "open_interest": oi,
            "futures_direction": direction,
            "direction_note": note,
            "dealer": dealer,
            "asset_manager": asset_mgr,
            "leveraged_funds": lev_funds,
            "pair_aligned_lev_money_net": pair_aligned_lev_net,
            "pair_aligned_lev_money_net_pct_oi": pair_aligned_lev_net_pct,
            "pair_aligned_asset_mgr_net": pair_aligned_am_net,
        }
    )


def fetch_cftc_fx_cot(symbol: str) -> dict[str, Any]:
    """Fetch CFTC TFF weekly FX positioning for whitelisted G10 FX / DXY contracts."""
    sym = (symbol or "").strip().upper()
    spec = CFTC_TFF_FX_SPECS.get(sym)
    if not spec:
        return {"status": "skipped", "reason": f"No CFTC TFF FX mapping for {symbol}"}

    resp = requests.get(
        "https://www.cftc.gov/dea/newcot/FinFutWk.txt",
        headers=DEFAULT_BROWSER_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    parsed = parse_cftc_tff(
        resp.text,
        market_query=spec["patterns"],
        contract_code=spec["code"],
        futures_direction=spec["futures_direction"],
        note=spec["note"],
    )
    parsed["symbol"] = sym
    return _sanitize_for_json(parsed)


def parse_bond_zh_us_rate_df(df: pd.DataFrame) -> dict[str, Any]:
    """Pure parser for ``ak.bond_zh_us_rate`` computing US-CN 10Y & 2Y rate differentials."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("Empty bond_zh_us_rate DataFrame")

    date_col = "日期" if "日期" in df.columns else df.columns[0]
    work = df.copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work = work.dropna(subset=[date_col]).sort_values(date_col, ascending=True).reset_index(drop=True)
    if work.empty:
        raise ValueError("No valid dated rows in bond_zh_us_rate DataFrame")

    def _latest_col(col_name: str) -> tuple[float | None, date | None]:
        if col_name not in work.columns:
            return None, None
        s = pd.to_numeric(work[col_name], errors="coerce")
        valid = work.loc[s.notna() & np.isfinite(s.to_numpy(dtype=float, na_value=np.nan))]
        if valid.empty:
            return None, None
        idx = valid.index[-1]
        return float(s.loc[idx]), work.loc[idx, date_col].date()

    cn10_val, cn10_dt = _latest_col("中国国债收益率10年")
    cn2_val, cn2_dt = _latest_col("中国国债收益率2年")
    us10_val, us10_dt = _latest_col("美国国债收益率10年")
    us2_val, us2_dt = _latest_col("美国国债收益率2年")

    if (cn10_val is None or us10_val is None) and (cn2_val is None or us2_val is None):
        raise ValueError("Missing CN/US 10Y and 2Y yields in bond_zh_us_rate DataFrame")

    diff_10y_pct = _round_finite(us10_val - cn10_val, 4) if (us10_val is not None and cn10_val is not None) else None
    diff_10y_bp = _round_finite((us10_val - cn10_val) * 100.0, 2) if diff_10y_pct is not None else None
    diff_2y_pct = _round_finite(us2_val - cn2_val, 4) if (us2_val is not None and cn2_val is not None) else None
    diff_2y_bp = _round_finite((us2_val - cn2_val) * 100.0, 2) if diff_2y_pct is not None else None

    date_lag_10y = abs((us10_dt - cn10_dt).days) if (us10_dt and cn10_dt) else None
    latest_dates = [d for d in (cn10_dt, cn2_dt, us10_dt, us2_dt) if d is not None]

    return _sanitize_for_json(
        {
            "source": "akshare_bond_zh_us_rate",
            "latest_date": max(latest_dates).isoformat() if latest_dates else None,
            "cn10y": {"latest_date": cn10_dt.isoformat() if cn10_dt else None, "latest_value": _round_finite(cn10_val, 4)},
            "cn2y": {"latest_date": cn2_dt.isoformat() if cn2_dt else None, "latest_value": _round_finite(cn2_val, 4)},
            "us10y": {"latest_date": us10_dt.isoformat() if us10_dt else None, "latest_value": _round_finite(us10_val, 4)},
            "us2y": {"latest_date": us2_dt.isoformat() if us2_dt else None, "latest_value": _round_finite(us2_val, 4)},
            "us_minus_cn_10y_pct": diff_10y_pct,
            "us_minus_cn_10y_bp": diff_10y_bp,
            "us_minus_cn_2y_pct": diff_2y_pct,
            "us_minus_cn_2y_bp": diff_2y_bp,
            "base_minus_quote_10y_pct": diff_10y_pct,
            "base_minus_quote_10y_bp": diff_10y_bp,
            "date_lag_days": date_lag_10y,
        }
    )


def fetch_fx_rate_differential(symbol: str) -> dict[str, Any]:
    """Fetch sovereign yield differentials for whitelisted FX pairs and DXY."""
    sym = (symbol or "").strip().upper()
    spec = FX_RATE_DIFF_SPECS.get(sym)
    if not spec:
        return {"status": "skipped", "reason": f"No sovereign rate differential mapping for {symbol}"}

    src = spec["source"]
    if src == "akshare_bond_zh_us_rate":
        if ak is None:
            raise RuntimeError("akshare is not installed")
        start_str = (datetime.now(timezone.utc).date() - timedelta(days=60)).strftime("%Y%m%d")
        df = _run_with_timeout(
            lambda: ak.bond_zh_us_rate(start_date=start_str),
            timeout_s=HTTP_TIMEOUT,
            silence_stdout=True,
        )
        parsed = parse_bond_zh_us_rate_df(df)
        parsed["symbol"] = sym
        parsed["formula"] = spec["formula"]
        parsed["frequency_note"] = spec["frequency_note"]
        return _sanitize_for_json(parsed)

    if src == "fred_pair":
        base_obs = _fetch_fred_series(spec["base_series"])
        quote_obs = _fetch_fred_series(spec["quote_series"])
        base_val = _to_float(base_obs.get("latest_value"))
        quote_val = _to_float(quote_obs.get("latest_value"))
        if base_val is None or quote_val is None:
            raise ValueError(f"Missing FRED yield value for {sym}")
        diff_pct = _round_finite(base_val - quote_val, 4)
        diff_bp = _round_finite((base_val - quote_val) * 100.0, 2)
        d_base = _parse_date_obj(base_obs.get("latest_date"))
        d_quote = _parse_date_obj(quote_obs.get("latest_date"))
        lag_days = abs((d_base - d_quote).days) if (d_base and d_quote) else None
        return _sanitize_for_json(
            {
                "symbol": sym,
                "source": "fred",
                "formula": spec["formula"],
                "base": {
                    "label": spec["base_label"],
                    "frequency": spec["base_freq"],
                    **base_obs,
                },
                "quote": {
                    "label": spec["quote_label"],
                    "frequency": spec["quote_freq"],
                    **quote_obs,
                },
                "base_minus_quote_10y_pct": diff_pct,
                "base_minus_quote_10y_bp": diff_bp,
                "date_lag_days": lag_days,
                "frequency_note": spec["frequency_note"],
            }
        )

    if src == "fred_dxy_basket":
        series_data: dict[str, Any] = {}
        ok_count = 0
        for label, (sid, freq) in spec["series"].items():
            try:
                obs = _fetch_fred_series(sid)
                series_data[label] = {"status": "ok", "frequency": freq, **obs}
                ok_count += 1
            except Exception as exc:  # noqa: BLE001
                series_data[label] = {"status": "error", "series_id": sid, "frequency": freq, "error": str(exc)}

        if ok_count == 0:
            raise ValueError("All FRED series failed for DX-Y.NYB rate differential basket")

        def _val_and_date(label: str) -> tuple[float | None, date | None]:
            item = series_data.get(label) or {}
            if item.get("status") != "ok":
                return None, None
            return _to_float(item.get("latest_value")), _parse_date_obj(item.get("latest_date"))

        us10, us10_dt = _val_and_date("US10Y")
        us2, _ = _val_and_date("US2Y")
        us10_real, _ = _val_and_date("US10Y_REAL")
        de10, de10_dt = _val_and_date("DE10Y")
        jp10, jp10_dt = _val_and_date("JP10Y")

        us_2s10s_pct = _round_finite(us10 - us2, 4) if (us10 is not None and us2 is not None) else None
        us_2s10s_bp = _round_finite((us10 - us2) * 100.0, 2) if us_2s10s_pct is not None else None
        us_de_pct = _round_finite(us10 - de10, 4) if (us10 is not None and de10 is not None) else None
        us_de_bp = _round_finite((us10 - de10) * 100.0, 2) if us_de_pct is not None else None
        us_jp_pct = _round_finite(us10 - jp10, 4) if (us10 is not None and jp10 is not None) else None
        us_jp_bp = _round_finite((us10 - jp10) * 100.0, 2) if us_jp_pct is not None else None

        status = "ok" if ok_count == len(spec["series"]) else "partial"
        return _sanitize_for_json(
            {
                "status": status,
                "symbol": sym,
                "source": "fred_dxy_basket",
                "us_2s10s_pct": us_2s10s_pct,
                "us_2s10s_bp": us_2s10s_bp,
                "us10y_real_yield_pct": _round_finite(us10_real, 4),
                "us_minus_de_10y_pct": us_de_pct,
                "us_minus_de_10y_bp": us_de_bp,
                "us_minus_jp_10y_pct": us_jp_pct,
                "us_minus_jp_10y_bp": us_jp_bp,
                "date_lags_days": {
                    "us_vs_de": abs((us10_dt - de10_dt).days) if (us10_dt and de10_dt) else None,
                    "us_vs_jp": abs((us10_dt - jp10_dt).days) if (us10_dt and jp10_dt) else None,
                },
                "series": series_data,
                "frequency_note": spec["frequency_note"],
            }
        )

    raise ValueError(f"Unsupported FX rate differential source: {src}")


# ---------------------------------------------------------------------------
# 5. A-Share 5Y Valuation Percentiles, US Valuation/Positioning, Farside ETF
# ---------------------------------------------------------------------------

MIN_VALUATION_SAMPLE_DAYS = 60


def _positive_percentile_stats(
    series: pd.Series,
    latest_val: float | None,
    min_days: int = MIN_VALUATION_SAMPLE_DAYS,
) -> tuple[float | None, float | None, int, str | None]:
    num = pd.to_numeric(series, errors="coerce")
    arr = num.to_numpy(dtype=float, na_value=np.nan)
    pos = arr[np.isfinite(arr) & (arr > 0)]
    count = int(pos.size)
    if count == 0:
        return None, None, 0, "5年窗口内无正值样本，无法计算分位"
    median_val = _round_finite(float(np.median(pos)), 4)
    if count < min_days:
        return None, median_val, count, f"有效正值样本仅 {count} 日（低于最小阈值 {min_days} 日），分位不具统计意义"
    if latest_val is None or not math.isfinite(latest_val) or latest_val <= 0:
        return None, median_val, count, "最新估值为负或缺失，不计算正值分位"
    percentile = _round_finite(float(np.mean(pos <= latest_val)) * 100.0, 2)
    return percentile, median_val, count, None


def compute_ashare_valuation_percentiles(
    df: pd.DataFrame,
    min_sample_days: int = MIN_VALUATION_SAMPLE_DAYS,
) -> dict[str, Any]:
    """Compute trailing 5 calendar-year valuation percentiles from ``stock_value_em``."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("Empty A-share valuation DataFrame")

    date_col = "数据日期" if "数据日期" in df.columns else df.columns[0]
    work = df.copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work = work.dropna(subset=[date_col])
    # Stable mergesort preserves original row order on duplicate dates so keep="last" picks the latest appended entry
    work = work.sort_values(date_col, ascending=True, kind="mergesort")
    work = work.drop_duplicates(subset=[date_col], keep="last").reset_index(drop=True)
    if work.empty:
        raise ValueError("No valid dated rows in A-share valuation DataFrame")

    latest_ts = work[date_col].iloc[-1]
    cutoff_ts = latest_ts - pd.DateOffset(years=5)
    window = work.loc[work[date_col] >= cutoff_ts].reset_index(drop=True)
    latest = window.iloc[-1]

    pe_ttm = _to_float(latest.get("PE(TTM)"))
    pe_static = _to_float(latest.get("PE(静)"))
    pb = _to_float(latest.get("市净率"))
    ps = _to_float(latest.get("市销率"))
    pcf = _to_float(latest.get("市现率"))
    market_cap = _to_float(latest.get("总市值"))

    pe_pct, pe_med, pe_pos_days, pe_note = (
        _positive_percentile_stats(window["PE(TTM)"], pe_ttm, min_days=min_sample_days)
        if "PE(TTM)" in window.columns
        else (None, None, 0, "缺少PE(TTM)列")
    )
    pb_pct, pb_med, pb_pos_days, pb_note = (
        _positive_percentile_stats(window["市净率"], pb, min_days=min_sample_days)
        if "市净率" in window.columns
        else (None, None, 0, "缺少市净率列")
    )

    out: dict[str, Any] = {
        "latest_date": window[date_col].iloc[-1].date().isoformat(),
        "window_start_date": window[date_col].iloc[0].date().isoformat(),
        "sample_days": int(len(window)),
        "min_sample_days_required": int(min_sample_days),
        "pe_ttm": _round_finite(pe_ttm, 4),
        "pe_static": _round_finite(pe_static, 4),
        "pb": _round_finite(pb, 4),
        "ps": _round_finite(ps, 4),
        "pcf": _round_finite(pcf, 4),
        "market_cap": _round_finite(market_cap, 2),
        "pe_ttm_5y_percentile": pe_pct,
        "pe_ttm_5y_median": pe_med,
        "pe_ttm_positive_days": pe_pos_days,
        "pb_5y_percentile": pb_pct,
        "pb_5y_median": pb_med,
        "pb_positive_days": pb_pos_days,
    }
    if pe_note:
        out["pe_ttm_coverage_note"] = pe_note
    if pb_note:
        out["pb_coverage_note"] = pb_note
    return _sanitize_for_json(out)


def fetch_ashare_valuation_percentiles(code6: str) -> dict[str, Any]:
    """Fetch Eastmoney A-share historical valuation & compute trailing 5Y percentiles."""
    raw = str(code6 or "").strip().upper()
    m = re.search(r"(\d{6})", raw)
    if not m:
        return {"status": "skipped", "reason": f"Invalid A-share 6-digit code: {code6}"}
    clean_code = m.group(1)
    if ak is None:
        raise RuntimeError("akshare is not installed")

    df = _run_with_timeout(
        lambda: ak.stock_value_em(symbol=clean_code),
        timeout_s=HTTP_TIMEOUT,
        silence_stdout=True,
    )
    res = compute_ashare_valuation_percentiles(df)
    res["symbol"] = clean_code
    return _sanitize_for_json(res)


def _flatten_calendar_candidates(obj: Any) -> list[Any]:
    if obj is None:
        return []
    if isinstance(obj, (date, datetime, pd.Timestamp, str)):
        return [obj]
    if isinstance(obj, (list, tuple, set)):
        items: list[Any] = []
        for elem in obj:
            items.extend(_flatten_calendar_candidates(elem))
        return items
    if isinstance(obj, (pd.Series, np.ndarray)):
        items = []
        for elem in obj.tolist():
            items.extend(_flatten_calendar_candidates(elem))
        return items
    return []


def _extract_next_earnings_date(calendar: Any, ref_date: date | None = None) -> str | None:
    raw_candidates: list[Any] = []
    anchor_iso = (ref_date or datetime.now(timezone.utc).date()).isoformat()

    if calendar is None:
        return None
    if isinstance(calendar, dict):
        for k, v in calendar.items():
            key_norm = str(k).strip().lower().replace("_", " ")
            if key_norm in {"earnings date", "earningsdate", "next earnings date"}:
                raw_candidates.extend(_flatten_calendar_candidates(v))
    elif isinstance(calendar, pd.DataFrame) and not calendar.empty:
        matched = False
        for idx in calendar.index:
            idx_norm = str(idx).strip().lower().replace("_", " ")
            if idx_norm in {"earnings date", "earningsdate"}:
                raw_candidates.extend(_flatten_calendar_candidates(calendar.loc[idx]))
                matched = True
        if not matched:
            for col in calendar.columns:
                col_norm = str(col).strip().lower().replace("_", " ")
                if col_norm in {"earnings date", "earningsdate"}:
                    raw_candidates.extend(_flatten_calendar_candidates(calendar[col]))
    else:
        raw_candidates.extend(_flatten_calendar_candidates(calendar))

    upcoming_dates: list[str] = []
    for cand in raw_candidates:
        iso = _to_iso_date(cand)
        if iso and iso >= anchor_iso and iso not in upcoming_dates:
            upcoming_dates.append(iso)

    if not upcoming_dates:
        return None
    upcoming_dates.sort()
    return upcoming_dates[0]


def extract_us_valuation_positioning(
    info: dict[str, Any] | None,
    calendar: Any = None,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Safely extract US stock valuation, short interest, ownership, and upcoming earnings date."""
    src = info if isinstance(info, dict) else {}
    anchor = ref_date or datetime.now(timezone.utc).date()
    anchor_iso = anchor.isoformat()

    next_earnings = _extract_next_earnings_date(calendar, ref_date=anchor)
    if next_earnings is None:
        for ts_key in ("earningsTimestampStart", "earningsTimestamp"):
            ts_val = _to_float(src.get(ts_key))
            if ts_val is not None and ts_val > 1e8:
                try:
                    cand_iso = datetime.fromtimestamp(ts_val, tz=timezone.utc).date().isoformat()
                    if cand_iso >= anchor_iso:
                        next_earnings = cand_iso
                        break
                except (OverflowError, OSError, ValueError):
                    continue

    return _sanitize_for_json(
        {
            "market_cap": _round_finite(_to_float(src.get("marketCap")), 2),
            "trailing_pe": _round_finite(_to_float(src.get("trailingPE")), 4),
            "forward_pe": _round_finite(_to_float(src.get("forwardPE")), 4),
            "ev_to_ebitda": _round_finite(_to_float(src.get("enterpriseToEbitda")), 4),
            "short_percent_of_float": _round_finite(_to_float(src.get("shortPercentOfFloat")), 6),
            "short_ratio": _round_finite(_to_float(src.get("shortRatio")), 4),
            "held_percent_institutions": _round_finite(_to_float(src.get("heldPercentInstitutions")), 6),
            "held_percent_insiders": _round_finite(_to_float(src.get("heldPercentInsiders")), 6),
            "next_earnings_date": next_earnings,
        }
    )


def parse_farside_etf_html(
    html_text: str,
    asset: str = "BTC",
    recent_limit: int = 10,
) -> dict[str, Any]:
    """Parse Farside Investors daily spot ETF flow HTML table for BTC or ETH."""
    asset_key = (asset or "BTC").strip().upper()
    spec = FARSIDE_ETF_SPECS.get(asset_key)
    if not spec:
        raise ValueError(f"Unsupported Farside ETF asset: {asset}")
    if not html_text or not html_text.strip():
        raise ValueError(f"Empty Farside {asset_key} ETF HTML")

    anchors: tuple[str, ...] = spec["anchor_tickers"]
    tables = re.findall(r"<table.*?>.*?</table>", html_text, re.S | re.I)
    target_table = None
    for tb in tables:
        if "Total" in tb and any(anchor in tb for anchor in anchors):
            target_table = tb
            break
    if target_table is None:
        raise ValueError(f"{asset_key} ETF flow table not found in Farside HTML")

    raw_rows = re.findall(r"<tr.*?>(.*?)</tr>", target_table, re.S | re.I)
    parsed_rows: list[list[str]] = []
    for tr in raw_rows:
        cells = re.findall(r"<t[hd].*?>(.*?)</t[hd]>", tr, re.S | re.I)
        cleaned = [html_lib.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        if cleaned:
            parsed_rows.append(cleaned)

    headers: list[str] = []
    total_col_idx: int | None = None
    launched_col_indices: set[int] = set()
    anchor_set = {a.upper() for a in anchors}
    date_rows: list[dict[str, Any]] = []
    cumulative_total: float | None = None
    date_re = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$|^\d{4}-\d{2}-\d{2}$")

    # Handle both single-row headers (full table) and two-row headers (short pages where 'Total' is on row 0
    # and ETF tickers like 'IBIT'/'ETHA' are on row 1) strictly BEFORE the first date row.
    pre_date_rows: list[list[str]] = []
    for row in parsed_rows:
        first = row[0]
        if not date_rows and not date_re.match(first):
            pre_date_rows.append(row)
            if not headers and any(anchor in row for anchor in anchors):
                headers = [h if h else ("Date" if idx == 0 else f"col_{idx}") for idx, h in enumerate(row)]
                for idx, h in enumerate(headers):
                    if h.strip().lower() == "total":
                        total_col_idx = idx
                        break
            if total_col_idx is None:
                for idx, cell in enumerate(row):
                    if cell.strip().lower() == "total":
                        # Verify column count compatibility if headers already seen
                        if not headers or len(row) == len(headers):
                            total_col_idx = idx
                            break
            continue
        if date_re.match(first):
            if total_col_idx is None and headers:
                # Re-scan pre-date rows with exact same width as ticker row for explicit 'Total' header
                for hrow in pre_date_rows:
                    if len(hrow) == len(headers):
                        for idx, cell in enumerate(hrow):
                            if cell.strip().lower() == "total":
                                total_col_idx = idx
                                break
                    if total_col_idx is not None:
                        break
            if total_col_idx is None:
                continue
            if len(row) <= total_col_idx:
                # Preserve malformed/short date row as partial so contiguous rolling windows NEVER skip over it
                date_rows.append(
                    {
                        "date": first,
                        "total_usd_m": None,
                        "is_partial_report": True,
                        "unreported_funds": ["MISSING_TOTAL_COLUMN"],
                        "reported_funds_count": 0,
                        "etf_flows_usd_m": {},
                    }
                )
                continue
            total_val = _to_float(row[total_col_idx])
            etf_flows: dict[str, float | None] = {}
            unreported_funds: list[str] = []
            reported_funds = 0
            for idx in range(1, total_col_idx):
                h_name = headers[idx] if idx < len(headers) else f"col_{idx}"
                raw_cell = row[idx].strip() if idx < len(row) else ""
                parsed_cell = _to_float(raw_cell)
                etf_flows[h_name] = _round_finite(parsed_cell, 2)
                if parsed_cell is not None:
                    reported_funds += 1
                    launched_col_indices.add(idx)
                else:
                    # Missing cell on a previously launched ETF or core anchor ETF means unreported/partial day
                    # (pre-launch '-' on a newer ETF in historical rows is NOT treated as partial).
                    if idx in launched_col_indices or h_name.upper() in anchor_set:
                        unreported_funds.append(h_name)
            is_partial = bool(unreported_funds) or (reported_funds == 0)
            date_rows.append(
                {
                    "date": first,
                    "total_usd_m": _round_finite(total_val, 2),
                    "is_partial_report": is_partial,
                    "unreported_funds": unreported_funds,
                    "reported_funds_count": reported_funds,
                    "etf_flows_usd_m": etf_flows,
                }
            )
        elif first.lower() == "total" and total_col_idx is not None and len(row) > total_col_idx:
            cumulative_total = _round_finite(_to_float(row[total_col_idx]), 2)

    if total_col_idx is None:
        raise ValueError(f"Explicit 'Total' header column not found in Farside {asset_key} ETF table")
    if not date_rows:
        raise ValueError(f"No daily {asset_key} ETF flow rows parsed from Farside HTML")

    # Keep EVERY date row present in the table when checking contiguous rolling windows so neither
    # malformed short rows nor all-missing zero-total rows can be silently bridged over.
    complete_indices = [
        idx for idx, r in enumerate(date_rows) if r["total_usd_m"] is not None and not r["is_partial_report"]
    ]
    latest_row = date_rows[-1]
    latest_complete_idx = complete_indices[-1] if complete_indices else None
    latest_complete = date_rows[latest_complete_idx] if latest_complete_idx is not None else None
    latest_partial = latest_row if (latest_row["is_partial_report"] or latest_row["total_usd_m"] is None) else None

    def _contiguous_rolling(window_size: int) -> tuple[float | None, bool]:
        """Require exact contiguous date_rows window ending at latest_complete_idx with zero interior holes."""
        if latest_complete_idx is None or (latest_complete_idx + 1) < window_size:
            return None, False
        window = date_rows[latest_complete_idx - window_size + 1 : latest_complete_idx + 1]
        if any(r["is_partial_report"] or r["total_usd_m"] is None for r in window):
            return None, True
        return _round_finite(sum(float(r["total_usd_m"]) for r in window), 2), False

    complete_count = len(complete_indices)
    rolling_5d, gap_5d = _contiguous_rolling(5)
    rolling_20d, gap_20d = _contiguous_rolling(20)

    keep_n = max(1, int(recent_limit))
    clean_recent = [
        {k: v for k, v in r.items() if k != "reported_funds_count"} for r in date_rows[-keep_n:]
    ]
    clean_partial = (
        {k: v for k, v in latest_partial.items() if k != "reported_funds_count"}
        if latest_partial is not None
        else None
    )

    out: dict[str, Any] = {
        "asset": asset_key,
        "unit": "USD_million",
        "latest_date": latest_complete["date"] if latest_complete else None,
        "latest_total_usd_m": latest_complete["total_usd_m"] if latest_complete else None,
        "latest_partial_report": clean_partial,
        "complete_days_available": complete_count,
        "rolling_5d_usd_m": rolling_5d,
        "rolling_20d_usd_m": rolling_20d,
        "cumulative_total_usd_m": cumulative_total,
        "total_trading_days": len(date_rows),
        "recent_daily": clean_recent,
    }
    notes: list[str] = []
    if clean_partial is not None:
        notes.append(
            f"最新日期 {clean_partial['date']} 仍有基金未齐报（{','.join(clean_partial['unreported_funds']) or '全空'}），已与齐报主序列隔离"
        )
    if gap_5d:
        notes.append("近5个连续交易日内存在未齐报日，rolling_5d_usd_m 置空（拒绝跨日补数）")
    if gap_20d:
        notes.append("近20个连续交易日内存在未齐报日，rolling_20d_usd_m 置空（拒绝跨日补数）")
    elif complete_count < 20:
        notes.append(f"表格仅覆盖 {complete_count} 个齐报交易日（不足20日），rolling_20d_usd_m 置空")
    if cumulative_total is None:
        notes.append("表格未提供官方 Total 汇总行，cumulative_total_usd_m 置空")
    if notes:
        out["coverage_note"] = "；".join(notes)
    return _sanitize_for_json(out)


def _fetch_html_with_fallback(url: str) -> str:
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, impersonate="chrome", timeout=HTTP_TIMEOUT)
            if resp.status_code == 200 and "<table" in resp.text:
                return resp.text
        except Exception:  # noqa: BLE001
            pass
    resp = requests.get(url, headers=DEFAULT_BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def fetch_farside_etf_flows(asset: str = "BTC") -> dict[str, Any]:
    """Fetch Farside Investors daily spot ETF flows for BTC or ETH (skipped for other coins)."""
    asset_key = (asset or "").strip().upper()
    spec = FARSIDE_ETF_SPECS.get(asset_key)
    if not spec:
        return {"status": "skipped", "reason": f"Farside spot ETF flow not available for {asset}"}

    last_err: Exception | None = None
    for url in (spec["url"], spec["fallback_url"]):
        try:
            html_text = _fetch_html_with_fallback(url)
            parsed = parse_farside_etf_html(html_text, asset=asset_key)
            parsed["source_url"] = url
            return _sanitize_for_json(parsed)
        except Exception as exc:  # noqa: BLE001
            last_err = exc

    assert last_err is not None
    raise last_err


# ============================================================================
# 6. Polymarket Public Gamma API: Macro Rates, Tariffs & Geopolitics Odds
# ============================================================================

POLYMARKET_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

POLYMARKET_CATEGORY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "macro_rates",
        "label": "宏观利率与经济",
        "tag_slug": "economy",
        "fetch_limit": 18,
        "max_events": 5,
        "min_volume_total": 50_000.0,
        "min_volume_24h": 5_000.0,
        "specificity": 1,
    },
    {
        "key": "trade_tariffs",
        "label": "关税与贸易政策",
        "tag_slug": "tariffs",
        "fetch_limit": 10,
        "max_events": 3,
        "min_volume_total": 10_000.0,
        "min_volume_24h": 1_500.0,
        "specificity": 3,
    },
    {
        "key": "geopolitics",
        "label": "地缘与能源咽喉",
        "tag_slug": "geopolitics",
        "fetch_limit": 16,
        "max_events": 4,
        "min_volume_total": 100_000.0,
        "min_volume_24h": 15_000.0,
        "specificity": 2,
    },
)

_POLYMARKET_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_POLYMARKET_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_POLYMARKET_DATE_LABEL_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"q[1-4]|20\d{2}"
    r")\b",
    re.IGNORECASE,
)
_POLYMARKET_LADDER_TITLE_RE = re.compile(
    r"(continues?\s+through|by\s*\.\.\.|before\s*\.\.\.|through\s*\.\.\.)",
    re.IGNORECASE,
)
# Exclude non-macro entertainment/sports/gossip/single-stock-cap noise without blocking policy events (e.g. TikTok ban)
_POLYMARKET_NOISE_RE = re.compile(
    r"\b("
    r"largest company|market cap|stock close|share price|earnings beat|"
    r"ceo|affair|tweets?|aliens?|ufo|gta|oscar|grammy|emmy|spotify|youtube|"
    r"nba|nfl|nhl|mlb|ufc|fifa|world cup|super bowl|premier league|counter-strike|esports|"
    r"celebrity|divorce|wedding|movie|box office|album|song|taylor swift|mrbeast|"
    r"mayor"
    r")\b",
    re.IGNORECASE,
)

# Bounded non-blocking executor & per-tag single-flight dedup for Polymarket Gamma fetches
_POLYMARKET_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=6,
    thread_name_prefix="polymarket-gamma",
)
_POLYMARKET_INFLIGHT_LOCK = threading.Lock()
_POLYMARKET_INFLIGHT: dict[str, concurrent.futures.Future[list[dict[str, Any]]]] = {}


def _pm_to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    try:
        s = str(val).strip()
        if not s:
            return None
        f = float(s)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _pm_parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _pm_extract_iso_date(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if len(s) >= 10 and _POLYMARKET_ISO_DATE_RE.match(s[:10]):
        return s[:10]
    return None


def _pm_extract_yes_prob(
    outcomes: list[Any],
    prices: list[Any],
    *,
    require_yes: bool = False,
) -> tuple[str | None, float | None, bool]:
    """Strictly extract primary outcome label, probability in [0, 1], and whether index 0 was used.

    Validates 1-to-1 alignment between ``outcomes`` and ``prices`` so malformed or reversed
    vectors can never turn a 'No' price into a 'Yes' probability.
    """
    if len(outcomes) < 2 or len(outcomes) != len(prices):
        return None, None, False
    yes_idx: int | None = None
    for idx, label in enumerate(outcomes):
        if str(label or "").strip().lower() == "yes":
            yes_idx = idx
            break
    if require_yes and yes_idx is None:
        return None, None, False

    target_idx = yes_idx if yes_idx is not None else 0
    lbl = str(outcomes[target_idx] or "").strip()
    if not lbl:
        return None, None, False
    raw_prob = _pm_to_float(prices[target_idx])
    if raw_prob is None or raw_prob < 0.0 or raw_prob > 1.0:
        return None, None, False
    return lbl, raw_prob, (target_idx == 0)


def _pm_valid_delta_pp(raw_delta: Any, *, trust_sign: bool) -> float | None:
    if not trust_sign:
        return None
    val = _pm_to_float(raw_delta)
    if val is None or val < -1.0 or val > 1.0:
        return None
    return round(val * 100.0, 1)


def _pm_consensus_band(prob_pct: float, *, market_type: str = "multi") -> str:
    if market_type == "date_ladder":
        return "ladder"
    if prob_pct >= 80.0:
        return "consensus"
    if prob_pct >= 30.0:
        return "contested"
    return "tail_risk"


def _pm_detect_date_ladder(
    raw_event: dict[str, Any],
    parsed_outcomes: list[dict[str, Any]],
) -> bool:
    """Identify multi-deadline non-mutually-exclusive date ladders (e.g. ceasefire through Nov 30 / Dec 15 / Dec 31)."""
    if len(parsed_outcomes) < 2:
        return False
    neg_risk = raw_event.get("negRisk")
    if neg_risk is True:
        return False
    unique_dates = {o["end_date"] for o in parsed_outcomes if o.get("end_date")}
    date_label_hits = sum(1 for o in parsed_outcomes if _POLYMARKET_DATE_LABEL_RE.search(str(o.get("label") or "")))
    prob_sum = sum(float(o["prob_pct"]) for o in parsed_outcomes)
    title = str(raw_event.get("title") or "")
    if len(unique_dates) >= 2 and (date_label_hits >= 2 or neg_risk is False or prob_sum > 115.0):
        return True
    if neg_risk is False and date_label_hits >= 2 and (_POLYMARKET_LADDER_TITLE_RE.search(title) or prob_sum > 115.0):
        return True
    return False


def _parse_single_polymarket_event(
    raw_event: dict[str, Any],
    *,
    category: str,
    category_label: str,
    min_volume_total: float,
    min_volume_24h: float,
    today_utc: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    if raw_event.get("closed") is True or raw_event.get("archived") is True:
        return None
    if raw_event.get("active") is False:
        return None

    title = str(raw_event.get("title") or "").strip()
    if not title or _POLYMARKET_NOISE_RE.search(title):
        return None

    vol_24h = _pm_to_float(raw_event.get("volume24hr")) or 0.0
    vol_total = _pm_to_float(raw_event.get("volume")) or 0.0
    # Require genuine recent 24h trading activity AND reasonable cumulative depth (never pass stale dead pools)
    if vol_24h < min_volume_24h or vol_total < min_volume_total:
        return None

    raw_markets = raw_event.get("markets")
    if not isinstance(raw_markets, list) or not raw_markets:
        return None

    active_markets = [
        m
        for m in raw_markets
        if isinstance(m, dict)
        and m.get("closed") is not True
        and m.get("active") is not False
        and m.get("archived") is not True
        and m.get("acceptingOrders") is not False
    ]
    if not active_markets:
        return None

    # Distinguish true single binary events from grouped events (even when only 1 active sub-market remains!)
    has_group_titles = any(
        bool(str(m.get("groupItemTitle") or "").strip())
        and str(m.get("groupItemTitle") or "").strip().lower() not in ("yes", "no")
        for m in active_markets
    )
    is_grouped = len(raw_markets) > 1 or len(active_markets) > 1 or has_group_titles

    parsed_outcomes: list[dict[str, Any]] = []
    for m in active_markets:
        outcomes_list = _pm_parse_json_list(m.get("outcomes"))
        prices_list = _pm_parse_json_list(m.get("outcomePrices"))
        primary_lbl, prob, is_idx_zero = _pm_extract_yes_prob(
            outcomes_list,
            prices_list,
            require_yes=is_grouped,
        )
        if primary_lbl is None or prob is None:
            continue
        prob_pct = round(prob * 100.0, 1)
        m_vol = _pm_to_float(m.get("volumeNum"))
        if m_vol is None:
            m_vol = _pm_to_float(m.get("volume")) or 0.0
        m_vol_24h = _pm_to_float(m.get("volume24hr"))
        m_liq = _pm_to_float(m.get("liquidityNum"))
        if m_liq is None:
            m_liq = _pm_to_float(m.get("liquidity"))
        m_end_date = _pm_extract_iso_date(m.get("endDateIso")) or _pm_extract_iso_date(m.get("endDate"))
        m_question = str(m.get("question") or "").strip()[:160]

        if not is_grouped:
            opt_label = primary_lbl
        else:
            opt_label = str(m.get("groupItemTitle") or "").strip() or m_question or primary_lbl

        parsed_outcomes.append(
            {
                "label": opt_label[:64],
                "question": m_question,
                "prob_pct": prob_pct,
                "chg_1d_pp": _pm_valid_delta_pp(m.get("oneDayPriceChange"), trust_sign=is_idx_zero),
                "chg_7d_pp": _pm_valid_delta_pp(m.get("oneWeekPriceChange"), trust_sign=is_idx_zero),
                "volume_usd": round(m_vol, 0),
                "volume_24h_usd": round(m_vol_24h, 0) if m_vol_24h is not None else None,
                "liquidity_usd": round(m_liq, 0) if m_liq is not None else None,
                "end_date": m_end_date,
                "accepting_orders": m.get("acceptingOrders") is not False,
            }
        )

    if not parsed_outcomes:
        return None

    is_date_ladder = is_grouped and _pm_detect_date_ladder(raw_event, parsed_outcomes)
    if not is_grouped:
        market_type = "binary"
        top_outcomes = parsed_outcomes[:1]
        leading = top_outcomes[0]
    elif is_date_ladder:
        market_type = "date_ladder"
        # Preserve chronological deadline order so term-structure semantics stay intact
        parsed_outcomes.sort(
            key=lambda o: (
                o.get("end_date") or "9999-99-99",
                -float(o.get("volume_24h_usd") or 0.0),
                -float(o["prob_pct"]),
            )
        )
        # Drop dead tail nodes (<1% with negligible 24h flow) when >=2 meaningful deadlines exist
        meaningful = [
            o
            for o in parsed_outcomes
            if float(o["prob_pct"]) >= 1.0 or (o.get("volume_24h_usd") is not None and float(o["volume_24h_usd"]) >= 500.0)
        ]
        pool_nodes = meaningful if meaningful else parsed_outcomes
        # Keep up to 3 evenly representative / nearest-to-furthest deadlines
        if len(pool_nodes) <= 3:
            top_outcomes = pool_nodes
        else:
            top_outcomes = [pool_nodes[0], pool_nodes[len(pool_nodes) // 2], pool_nodes[-1]]
        # Headline representative for a date ladder: pick furthest active deadline in contested zone [15%, 90%], else furthest displayed deadline
        contested_nodes = [o for o in top_outcomes if 15.0 <= float(o["prob_pct"]) <= 90.0]
        leading = contested_nodes[-1] if contested_nodes else top_outcomes[-1]
    else:
        market_type = "multi"
        parsed_outcomes.sort(
            key=lambda o: (
                -float(o["prob_pct"]),
                -float(o.get("volume_24h_usd") or 0.0),
                -float(o.get("volume_usd") or 0.0),
            )
        )
        trimmed: list[dict[str, Any]] = []
        for item in parsed_outcomes[:6]:
            if len(trimmed) >= 3:
                break
            if len(trimmed) >= 2 and float(item["prob_pct"]) < 0.5:
                continue
            if (
                len(trimmed) >= 1
                and float(item["prob_pct"]) < 1.0
                and item.get("volume_24h_usd") is not None
                and float(item["volume_24h_usd"]) < 100.0
            ):
                continue
            trimmed.append(item)
        top_outcomes = trimmed or parsed_outcomes[:1]
        leading = top_outcomes[0]

    slug = str(raw_event.get("slug") or "").strip()
    url = f"https://polymarket.com/event/{slug}" if slug and _POLYMARKET_SLUG_RE.match(slug) else ""
    event_end_date = _pm_extract_iso_date(raw_event.get("endDate"))
    leading_end_date = leading.get("end_date") or event_end_date
    end_date_passed = bool(event_end_date and event_end_date < today_utc)
    event_liq = _pm_to_float(raw_event.get("liquidity"))
    raw_tags = [
        str(t.get("slug") or t.get("label") or "").strip()
        for t in (raw_event.get("tags") or [])
        if isinstance(t, dict) and str(t.get("slug") or t.get("label") or "").strip()
    ]
    band = _pm_consensus_band(float(leading["prob_pct"]), market_type=market_type)

    return {
        "id": str(raw_event.get("id") or slug or title[:32]),
        "category": category,
        "category_label": category_label,
        "title": title[:140],
        "slug": slug,
        "url": url,
        "end_date": event_end_date,
        "leading_end_date": leading_end_date,
        "end_date_passed": end_date_passed,
        "accepting_orders": True,
        "market_type": market_type,
        "volume_24h_usd": round(vol_24h, 0),
        "volume_total_usd": round(vol_total, 0),
        "liquidity_usd": round(event_liq, 0) if event_liq is not None else None,
        "leading_option": leading["label"],
        "leading_prob_pct": leading["prob_pct"],
        "leading_chg_1d_pp": leading.get("chg_1d_pp"),
        "leading_chg_7d_pp": leading.get("chg_7d_pp"),
        "consensus_band": band,
        "priced_in_band": band,
        "top_outcomes": top_outcomes,
        "source_tags": raw_tags[:8],
    }


def _build_polymarket_headlines(
    by_cat: dict[str, list[dict[str, Any]]],
    ordered_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select up to 4 headline events ensuring cross-category coverage with explicit 24h volume ordering."""
    headline: list[dict[str, Any]] = []
    headline_ids: set[str] = set()
    for spec in POLYMARKET_CATEGORY_SPECS:
        cat_list = by_cat.get(spec["key"]) or []
        if cat_list:
            top = cat_list[0]
            headline.append(top)
            headline_ids.add(top["id"])
    remaining = sorted(
        [ev for ev in ordered_events if ev["id"] not in headline_ids],
        key=lambda e: (-float(e.get("volume_24h_usd") or 0.0), -float(e.get("volume_total_usd") or 0.0), str(e.get("id") or "")),
    )
    for ev in remaining:
        if len(headline) >= 4:
            break
        headline.append(ev)
        headline_ids.add(ev["id"])
    return headline


def parse_polymarket_events(
    raw_by_category: dict[str, list[dict[str, Any]]],
    *,
    category_errors: dict[str, str] | None = None,
    fetched_at_utc: str | None = None,
) -> dict[str, Any]:
    """Normalize, deduplicate (by category specificity), sort and filter Polymarket Gamma API event payloads."""
    now_iso = fetched_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    today_utc = now_iso[:10] if len(now_iso) >= 10 and _POLYMARKET_ISO_DATE_RE.match(now_iso[:10]) else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Assign overlapping events to the MORE SPECIFIC category first (trade_tariffs > geopolitics > macro_rates)
    specs_by_specificity = sorted(
        POLYMARKET_CATEGORY_SPECS,
        key=lambda s: -int(s.get("specificity") or 0),
    )
    seen_ids: set[str] = set()
    by_cat: dict[str, list[dict[str, Any]]] = {}
    ok_cats = 0
    errors = dict(category_errors or {})

    for spec in specs_by_specificity:
        cat_key = spec["key"]
        raw_list = raw_by_category.get(cat_key)
        if raw_list is None:
            continue
        ok_cats += 1
        candidates: list[dict[str, Any]] = []
        local_seen: set[str] = set()
        for raw_ev in raw_list:
            parsed = _parse_single_polymarket_event(
                raw_ev,
                category=cat_key,
                category_label=spec["label"],
                min_volume_total=float(spec["min_volume_total"]),
                min_volume_24h=float(spec["min_volume_24h"]),
                today_utc=today_utc,
            )
            if parsed is None or parsed["id"] in seen_ids or parsed["id"] in local_seen:
                continue
            local_seen.add(parsed["id"])
            candidates.append(parsed)
        # Explicit deterministic sort by 24h volume then cumulative volume (never rely on upstream order)
        candidates.sort(
            key=lambda e: (
                -float(e.get("volume_24h_usd") or 0.0),
                -float(e.get("volume_total_usd") or 0.0),
                str(e.get("id") or ""),
            )
        )
        selected = candidates[: int(spec["max_events"])]
        for ev in selected:
            seen_ids.add(ev["id"])
        by_cat[cat_key] = selected

    # Emit final events in canonical display order (macro_rates -> trade_tariffs -> geopolitics)
    events: list[dict[str, Any]] = []
    for spec in POLYMARKET_CATEGORY_SPECS:
        events.extend(by_cat.get(spec["key"]) or [])

    headline = _build_polymarket_headlines(by_cat, events)
    total_specs = len(POLYMARKET_CATEGORY_SPECS)
    if ok_cats == 0:
        status = "error"
    elif ok_cats == total_specs and not errors:
        status = "ok"
    elif ok_cats > 0:
        status = "partial"
    else:
        status = "error"

    out: dict[str, Any] = {
        "status": status,
        "source": "Polymarket Gamma API",
        "fetched_at_utc": now_iso,
        "total_events": len(events),
        "categories_ok": ok_cats,
        "categories_total": total_specs,
        "categories_by_key": by_cat,
        "stale_categories": [],
        "headline_events": headline,
        "events": events,
    }
    if errors:
        out["category_errors"] = errors
    return _sanitize_for_json(out)


def _submit_polymarket_tag_dedup(
    spec: dict[str, Any],
    *,
    http_budget_seconds: float,
) -> concurrent.futures.Future[list[dict[str, Any]]]:
    """Submit a bounded Gamma API request reusing any still-running in-flight future for the same tag."""
    tag_slug = str(spec["tag_slug"])
    read_to = max(0.1, min(8.0, float(http_budget_seconds)))
    conn_to = max(0.1, min(2.5, read_to))

    def _fetch_one() -> list[dict[str, Any]]:
        params = {
            "active": "true",
            "closed": "false",
            "tag_slug": tag_slug,
            "limit": int(spec["fetch_limit"]),
            "order": "volume24hr",
            "ascending": "false",
        }
        resp = requests.get(
            POLYMARKET_GAMMA_EVENTS_URL,
            params=params,
            headers=DEFAULT_BROWSER_HEADERS,
            timeout=(conn_to, read_to),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise ValueError(f"Unexpected Gamma API response type for {tag_slug}")
        return payload

    with _POLYMARKET_INFLIGHT_LOCK:
        existing = _POLYMARKET_INFLIGHT.get(tag_slug)
        if existing is not None and not existing.done():
            return existing
        fut = _POLYMARKET_EXECUTOR.submit(_fetch_one)
        _POLYMARKET_INFLIGHT[tag_slug] = fut

    def _cleanup(_f: concurrent.futures.Future[list[dict[str, Any]]], _t: str = tag_slug) -> None:
        with _POLYMARKET_INFLIGHT_LOCK:
            if _POLYMARKET_INFLIGHT.get(_t) is _f:
                _POLYMARKET_INFLIGHT.pop(_t, None)

    fut.add_done_callback(_cleanup)
    return fut


def fetch_polymarket_macro_odds(timeout_seconds: float = 6.0) -> dict[str, Any]:
    """Fetch keyless real-money prediction market odds from Polymarket Gamma API within a strict monotonic deadline."""
    budget = max(0.05, min(10.0, float(timeout_seconds)))
    deadline = time.monotonic() + budget

    fut_map: dict[concurrent.futures.Future[list[dict[str, Any]]], dict[str, Any]] = {
        _submit_polymarket_tag_dedup(spec, http_budget_seconds=budget): spec
        for spec in POLYMARKET_CATEGORY_SPECS
    }
    remaining = max(0.01, deadline - time.monotonic())
    done, not_done = concurrent.futures.wait(list(fut_map.keys()), timeout=remaining)

    raw_by_cat: dict[str, list[dict[str, Any]]] = {}
    cat_errors: dict[str, str] = {}
    for fut in done:
        spec = fut_map[fut]
        try:
            raw_by_cat[spec["key"]] = fut.result(timeout=0)
        except Exception as exc:  # noqa: BLE001
            cat_errors[spec["key"]] = str(exc)
    for fut in not_done:
        spec = fut_map[fut]
        cat_errors[spec["key"]] = f"timeout after {budget:.2f}s"

    return parse_polymarket_events(raw_by_cat, category_errors=cat_errors)


__all__ = [
    "HTTP_TIMEOUT",
    "FUTURES_MONTH_CODES",
    "OVERSEAS_TERM_STRUCTURE_SPECS",
    "CN_DAILY_TERM_STRUCTURE_DEFAULTS",
    "CROSS_MARKET_SPREAD_SPECS",
    "FREIGHT_ROUTING",
    "FREIGHT_COLUMN_MAP",
    "CFTC_TFF_FX_SPECS",
    "FX_RATE_DIFF_SPECS",
    "FARSIDE_ETF_SPECS",
    "POLYMARKET_GAMMA_EVENTS_URL",
    "POLYMARKET_CATEGORY_SPECS",
    "estimate_contract_last_trading_day",
    "build_overseas_contract_candidates",
    "summarize_futures_curve",
    "fetch_overseas_term_structure",
    "fetch_cn_daily_term_structure",
    "compute_cross_market_spread",
    "fetch_cross_market_spread",
    "parse_freight_index_df",
    "fetch_freight_indices",
    "parse_cftc_tff",
    "fetch_cftc_fx_cot",
    "parse_bond_zh_us_rate_df",
    "fetch_fx_rate_differential",
    "compute_ashare_valuation_percentiles",
    "fetch_ashare_valuation_percentiles",
    "extract_us_valuation_positioning",
    "parse_farside_etf_html",
    "fetch_farside_etf_flows",
    "parse_polymarket_events",
    "fetch_polymarket_macro_odds",
]
