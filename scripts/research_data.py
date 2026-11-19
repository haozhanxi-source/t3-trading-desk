#!/usr/bin/env python3
"""Layer-3 unified free data aggregator for multi-market trading research.

Supports US stocks (SEC EDGAR + yfinance), A-shares (AkShare THS + HSGT),
Domestic CN futures (inventory + spot/basis + term structure + NBS output),
Overseas commodities (CFTC COT + EIA + USDA WASDE), Crypto (DefiLlama +
OKX/Binance OI + Farside BTC ETF flows), and US-China macro dual core (FRED + NBS/PMI/Customs).

Every external source is strictly isolated with try/except so a single timeout
or upstream change never crashes the pipeline.
"""
from __future__ import annotations

import argparse
import csv
import html as html_lib
import inspect
import io
import json
import math
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
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
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None

sys.path.insert(0, os.path.dirname(__file__))
from market_snapshot import get_snapshot, normalize_symbol  # noqa: E402

HTTP_TIMEOUT = 15
SEC_HEADERS = {
    "User-Agent": "T3TradingDeskResearch/1.0 (research@example.com)",
    "Accept-Encoding": "gzip, deflate",
}
DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

_SEC_TICKER_CACHE: dict[str, str] = {}

CN_FUTURE_META: dict[str, tuple[str, str, str]] = {
    "I0": ("铁矿石", "I", "ferrous"),
    "RB0": ("螺纹钢", "RB", "ferrous"),
    "HC0": ("热轧卷板", "HC", "ferrous"),
    "JM0": ("焦煤", "JM", "ferrous"),
    "J0": ("焦炭", "J", "ferrous"),
    "SF0": ("硅铁", "SF", "ferrous"),
    "SM0": ("锰硅", "SM", "ferrous"),
    "CU0": ("沪铜", "CU", "nonferrous"),
    "AL0": ("沪铝", "AL", "nonferrous"),
    "ZN0": ("沪锌", "ZN", "nonferrous"),
    "NI0": ("沪镍", "NI", "nonferrous"),
    "SN0": ("沪锡", "SN", "nonferrous"),
    "AU0": ("沪金", "AU", "precious"),
    "AG0": ("沪银", "AG", "precious"),
    "LC0": ("碳酸锂", "LC", "new_energy"),
    "SI0": ("工业硅", "SI", "new_energy"),
    "PS0": ("多晶硅", "PS", "new_energy"),
    "FG0": ("玻璃", "FG", "building"),
    "SA0": ("纯碱", "SA", "chemical"),
    "TA0": ("PTA", "TA", "chemical"),
    "MA0": ("甲醇", "MA", "chemical"),
    "V0": ("PVC", "V", "chemical"),
    "RU0": ("天然橡胶", "RU", "chemical"),
    "M0": ("豆粕", "M", "agri"),
    "Y0": ("豆油", "Y", "agri"),
    "P0": ("棕榈油", "P", "agri"),
    "OI0": ("菜油", "OI", "agri"),
    "SR0": ("白糖", "SR", "agri"),
    "CF0": ("棉花", "CF", "agri"),
    "LH0": ("生猪", "LH", "agri"),
    "EC0": ("集运欧线", "EC", "shipping"),
    "SC0": ("原油", "SC", "energy"),
}

INVENTORY_EM_BLACKLIST = {"原油", "燃油", "低硫燃料油", "纯碱"}
TERM_STRUCTURE_SKIP = {"PTA", "原油"}

CFTC_CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "HG=F": {"code": "085692", "patterns": ["COPPER- #1 - COMMODITY EXCHANGE", "COPPER- #1"]},
    "CL=F": {"code": "067651", "patterns": ["WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE"]},
    "BZ=F": {"code": "06765T", "patterns": ["BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE"]},
    "GC=F": {"code": "088691", "patterns": ["GOLD - COMMODITY EXCHANGE INC."]},
    "SI=F": {"code": "084691", "patterns": ["SILVER - COMMODITY EXCHANGE INC."]},
    "NG=F": {"code": "023651", "patterns": ["NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE"]},
    "ZC=F": {"code": "002602", "patterns": ["CORN - CHICAGO BOARD OF TRADE"]},
    "ZS=F": {"code": "005602", "patterns": ["SOYBEANS - CHICAGO BOARD OF TRADE"]},
    "ZW=F": {"code": "001602", "patterns": ["WHEAT-SRW - CHICAGO BOARD OF TRADE"]},
    "CT=F": {"code": "033661", "patterns": ["COTTON NO. 2 - ICE FUTURES U.S."]},
    "SB=F": {"code": "080732", "patterns": ["SUGAR NO. 11 - ICE FUTURES U.S."]},
    "KC=F": {"code": "083731", "patterns": ["COFFEE C - ICE FUTURES U.S."]},
    "CC=F": {"code": "073732", "patterns": ["COCOA - ICE FUTURES U.S."]},
    "LE=F": {"code": "057642", "patterns": ["LIVE CATTLE - CHICAGO MERCANTILE EXCHANGE"]},
    "HE=F": {"code": "054642", "patterns": ["LEAN HOGS - CHICAGO MERCANTILE EXCHANGE"]},
}
CFTC_MARKET_PATTERNS: dict[str, list[str]] = {
    sym: spec["patterns"] for sym, spec in CFTC_CONTRACT_SPECS.items()
}

EIA_WPSR_SYMBOLS = {"CL=F", "BZ=F", "HO=F", "RB=F"}
EIA_NGS_SYMBOLS = {"NG=F"}
USDA_WASDE_SYMBOLS = {
    "ZC=F": {"commodity": "Corn", "subreports": ["sr12", "sr22"]},
    "ZS=F": {"commodity": "Soybeans", "subreports": ["sr15", "sr28"]},
    "ZW=F": {"commodity": "Wheat", "subreports": ["sr11", "sr18"]},
    "ZM=F": {"commodity": "Soybean Meal", "subreports": ["sr15", "sr28"]},
    "ZL=F": {"commodity": "Soybean Oil", "subreports": ["sr15", "sr28"]},
    "CT=F": {"commodity": "Cotton", "subreports": ["sr17", "sr26"]},
}

FRED_SERIES: list[tuple[str, str]] = [
    ("DFII10", "US 10Y Real Yield (%)"),
    ("T10YIE", "US 10Y Breakeven Inflation (%)"),
    ("DGS10", "US 10Y Nominal Treasury Yield (%)"),
    ("FEDFUNDS", "Effective Federal Funds Rate (%)"),
    ("M2SL", "US M2 Money Stock (Bil. USD)"),
    ("PCOPPUSDM", "Global Price of Copper (USD/MT)"),
]


def _clean_scalar(val: Any) -> Any:
    """Convert numpy/pandas/datetime/NaN scalars into JSON-safe Python primitives."""
    if val is None:
        return None
    if isinstance(val, (pd.Timestamp, datetime, date)):
        return val.isoformat()
    if isinstance(val, (np.bool_, bool)):
        return bool(val)
    if isinstance(val, (np.integer, int)):
        return int(val)
    if isinstance(val, (np.floating, float)):
        fval = float(val)
        if math.isnan(fval) or math.isinf(fval):
            return None
        return round(fval, 6)
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    if isinstance(val, str):
        return val.strip()
    return str(val)


def _df_to_records_clean(df: pd.DataFrame | None, orient: str = "records") -> Any:
    """Convert DataFrame to clean JSON-serializable structure without NaN/Timestamp issues."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return [] if orient == "records" else {}
    if orient == "index":
        out: dict[str, dict[str, Any]] = {}
        for idx, row in df.iterrows():
            idx_key = _clean_scalar(idx)
            out[str(idx_key)] = {str(_clean_scalar(col)): _clean_scalar(row[col]) for col in df.columns}
        return out
    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        records.append({str(_clean_scalar(col)): _clean_scalar(row[col]) for col in df.columns})
    return records


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float, np.integer, np.floating)):
        f = float(raw)
        return None if (math.isnan(f) or math.isinf(f)) else f
    s = str(raw).strip().replace(",", "")
    if not s or s in {".", "N/A", "NA", "-", "--", "nan", "None"}:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        val = float(s)
        if math.isnan(val) or math.isinf(val):
            return None
        return -val if neg else val
    except ValueError:
        return None


def _to_int(raw: Any) -> int | None:
    f = _to_float(raw)
    if f is None:
        return None
    return int(round(f))


_STDOUT_FILTER_LOCK = threading.Lock()
_SILENCED_THREAD_IDS: set[int] = set()
_ORIGINAL_STDOUT: Any = None


class _ThreadFilteredStdout:
    """Drop stdout writes ONLY from explicitly silenced worker threads; never block main thread stdout."""

    def __init__(self, target):
        self._target = target

    def write(self, data: str) -> int:
        if threading.get_ident() in _SILENCED_THREAD_IDS:
            return len(data)
        return self._target.write(data)

    def flush(self) -> None:
        if threading.get_ident() not in _SILENCED_THREAD_IDS:
            self._target.flush()

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively replace non-finite float values (NaN/Inf) with None so allow_nan=False never crashes."""
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        fval = float(obj)
        return None if (math.isnan(fval) or math.isinf(fval)) else fval
    if isinstance(obj, (np.integer, int)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj


def _install_stdout_filter_if_needed() -> None:
    global _ORIGINAL_STDOUT
    if not isinstance(sys.stdout, _ThreadFilteredStdout):
        _ORIGINAL_STDOUT = sys.stdout
        sys.stdout = _ThreadFilteredStdout(_ORIGINAL_STDOUT)


def _restore_stdout_if_idle() -> None:
    global _ORIGINAL_STDOUT
    if not _SILENCED_THREAD_IDS and isinstance(sys.stdout, _ThreadFilteredStdout) and _ORIGINAL_STDOUT is not None:
        sys.stdout = _ORIGINAL_STDOUT
        _ORIGINAL_STDOUT = None


def _run_with_timeout(fn, timeout_s: float = 15.0, silence_stdout: bool = False):
    """Run fn in a daemon thread with bounded wall-clock wait.

    Daemon threads never block Python interpreter exit even if an underlying third-party
    HTTP call ignores socket timeouts, and thread-local stdout filtering restores sys.stdout when idle.
    """
    result_box: dict[str, Any] = {}

    def _worker() -> None:
        tid = threading.get_ident()
        if silence_stdout:
            with _STDOUT_FILTER_LOCK:
                _install_stdout_filter_if_needed()
                _SILENCED_THREAD_IDS.add(tid)
        try:
            result_box["value"] = fn()
        except Exception as exc:
            result_box["error"] = exc
        finally:
            if silence_stdout:
                with _STDOUT_FILTER_LOCK:
                    _SILENCED_THREAD_IDS.discard(tid)
                    _restore_stdout_if_idle()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise TimeoutError(f"Operation timed out after {timeout_s}s")
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


def _run_source(source_name: str, fn, sources_status: dict[str, Any]) -> dict[str, Any]:
    try:
        res = fn()
        if isinstance(res, dict) and "status" in res:
            st = res["status"]
            meta: dict[str, Any] = {"status": st}
            if "error" in res:
                meta["error"] = res["error"]
            if "reason" in res:
                meta["reason"] = res["reason"]
            sources_status[source_name] = meta
            return res
        sources_status[source_name] = {"status": "ok"}
        if isinstance(res, dict):
            return {"status": "ok", **res}
        return {"status": "ok", "data": res}
    except Exception as exc:
        err = str(exc)
        sources_status[source_name] = {"status": "error", "error": err}
        return {"status": "error", "error": err}


# ---------------------------------------------------------------------------
# Pure Parsers (Offline Unit Testable)
# ---------------------------------------------------------------------------

def parse_fred_csv(csv_text: str, series_id: str | None = None) -> dict[str, Any]:
    """Parse FRED fredgraph CSV text into latest observation and period change."""
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty FRED CSV content")
    reader = csv.reader(io.StringIO(csv_text.strip()))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("FRED CSV has fewer than 2 rows")

    header = [c.strip() for c in rows[0]]
    col_id = series_id or (header[1] if len(header) > 1 else "VALUE")
    observations: list[tuple[str, float]] = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        obs_date = r[0].strip()
        val = _to_float(r[1])
        if obs_date and val is not None:
            observations.append((obs_date, val))

    if not observations:
        raise ValueError(f"No valid observations in FRED CSV for {col_id}")

    latest_date, latest_val = observations[-1]
    prev_date, prev_val = observations[-2] if len(observations) >= 2 else (None, None)
    change = round(latest_val - prev_val, 6) if prev_val is not None else None
    change_pct = (
        round((latest_val / prev_val - 1.0) * 100.0, 4)
        if prev_val is not None and prev_val != 0
        else None
    )
    return {
        "series_id": col_id,
        "latest_date": latest_date,
        "latest_value": round(latest_val, 6),
        "previous_date": prev_date,
        "previous_value": round(prev_val, 6) if prev_val is not None else None,
        "change": change,
        "change_pct": change_pct,
        "observations_count": len(observations),
    }


def parse_cftc_disagg(
    csv_text: str,
    market_query: str | list[str] | None = None,
    contract_code: str | None = None,
) -> dict[str, Any]:
    """Parse CFTC disaggregated futures-only COT text (f_disagg.txt) by exact contract code or patterns.

    When multiple dates exist for the same contract, selects the latest report_date (YYYY-MM-DD).
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty CFTC COT CSV text")

    rows = [
        r for r in csv.reader(io.StringIO(csv_text))
        if len(r) >= 16 and r[0].strip().upper() != "MARKET_AND_EXCHANGE_NAMES"
    ]
    candidates: list[list[str]] = []

    if contract_code:
        code_upper = contract_code.strip().upper()
        candidates = [r for r in rows if r[3].strip().upper() == code_upper]

    if not candidates and market_query:
        patterns = [market_query] if isinstance(market_query, str) else list(market_query)
        patterns_upper = [p.strip().upper() for p in patterns if p and p.strip()]
        for pat in patterns_upper:
            pat_matches = [r for r in rows if pat in r[0].strip().upper()]
            if pat_matches:
                candidates = pat_matches
                break

    if not candidates:
        raise ValueError(f"No CFTC COT match found for code={contract_code!r}, query={market_query!r}")

    # Pick latest report_date (column 2: YYYY-MM-DD)
    candidates.sort(key=lambda r: r[2].strip(), reverse=True)
    matched_row = candidates[0]

    oi = _to_int(matched_row[7])
    prod_long = _to_int(matched_row[8])
    prod_short = _to_int(matched_row[9])
    swap_long = _to_int(matched_row[10])
    swap_short = _to_int(matched_row[11])
    mm_long = _to_int(matched_row[13])
    mm_short = _to_int(matched_row[14])
    mm_spread = _to_int(matched_row[15])

    mm_net = (mm_long - mm_short) if (mm_long is not None and mm_short is not None) else None
    prod_net = (prod_long - prod_short) if (prod_long is not None and prod_short is not None) else None
    swap_net = (swap_long - swap_short) if (swap_long is not None and swap_short is not None) else None

    return {
        "contract_code": matched_row[3].strip(),
        "market_name": matched_row[0].strip(),
        "report_date": matched_row[2].strip(),
        "open_interest": oi,
        "managed_money": {
            "long": mm_long,
            "short": mm_short,
            "spread": mm_spread,
            "net": mm_net,
            "net_pct_oi": round(mm_net / oi * 100.0, 2) if (mm_net is not None and oi) else None,
        },
        "prod_merc": {
            "long": prod_long,
            "short": prod_short,
            "net": prod_net,
            "net_pct_oi": round(prod_net / oi * 100.0, 2) if (prod_net is not None and oi) else None,
        },
        "swap_dealers": {
            "long": swap_long,
            "short": swap_short,
            "net": swap_net,
        },
    }


def parse_eia_wpsr_csv(csv_text: str) -> dict[str, Any]:
    """Parse EIA Weekly Petroleum Status Report table1.csv."""
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty EIA WPSR CSV")
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("\ufeff"))))
    if len(rows) < 2:
        raise ValueError("EIA WPSR CSV too short")

    stocks_date = rows[0][1].strip() if len(rows[0]) > 1 else None
    prev_date = rows[0][2].strip() if len(rows[0]) > 2 else None

    target_labels = {
        "Commercial (Excluding SPR)": "commercial_crude_stocks_mb",
        "Strategic Petroleum Reserve (SPR)": "spr_crude_stocks_mb",
        "Total Motor Gasoline": "gasoline_stocks_mb",
        "Distillate Fuel Oil": "distillate_stocks_mb",
        "Total Stocks (Excluding SPR)": "total_stocks_excl_spr_mb",
    }

    parsed: dict[str, Any] = {
        "report_date": stocks_date,
        "previous_date": prev_date,
        "stocks_unit": "Million Barrels",
        "production_unit": "Thousand Barrels per Day",
        "metrics": {},
    }

    in_supply_section = False
    supply_date = stocks_date
    for row in rows[1:]:
        if not row:
            continue
        col0 = row[0].strip()
        if col0 == "STUB_1" and len(row) >= 5 and row[1].strip() == "STUB_2":
            in_supply_section = True
            supply_date = row[2].strip() if len(row) > 2 else stocks_date
            continue

        if not in_supply_section:
            if col0 in target_labels and len(row) >= 5:
                key = target_labels[col0]
                parsed["metrics"][key] = {
                    "label": col0,
                    "current": _to_float(row[1]),
                    "previous_week": _to_float(row[2]),
                    "weekly_change": _to_float(row[3]),
                    "weekly_change_pct": _to_float(row[4]),
                    "year_ago": _to_float(row[5]) if len(row) > 5 else None,
                    "yoy_change": _to_float(row[6]) if len(row) > 6 else None,
                }
        else:
            col1 = row[1].strip() if len(row) > 1 else ""
            if "Domestic Production" in col1 and len(row) >= 5:
                parsed["metrics"]["us_crude_domestic_production_kbd"] = {
                    "label": col1,
                    "date": supply_date,
                    "current": _to_float(row[2]),
                    "previous_week": _to_float(row[3]),
                    "weekly_change": _to_float(row[4]),
                    "year_ago": _to_float(row[5]) if len(row) > 5 else None,
                    "yoy_change": _to_float(row[6]) if len(row) > 6 else None,
                }
            elif "Net Imports (Including SPR)" in col1 and len(row) >= 5:
                parsed["metrics"]["us_crude_net_imports_kbd"] = {
                    "label": col1,
                    "date": supply_date,
                    "current": _to_float(row[2]),
                    "previous_week": _to_float(row[3]),
                    "weekly_change": _to_float(row[4]),
                }

    if not parsed["metrics"]:
        raise ValueError("No recognized EIA WPSR metrics found")
    return parsed


def parse_eia_ngs_csv(csv_text: str) -> dict[str, Any]:
    """Parse EIA Weekly Natural Gas Storage Report wngsr.csv."""
    if not csv_text or not csv_text.strip():
        raise ValueError("Empty EIA NGS CSV")
    cleaned = csv_text.lstrip("\ufeff").lstrip("ï»¿")
    rows = list(csv.reader(io.StringIO(cleaned)))

    released_info = None
    week_ending_col_label = None
    regions: dict[str, Any] = {}
    total_row: dict[str, Any] | None = None

    for row in rows:
        joined = " ".join(c.strip() for c in row if c and c.strip())
        if "Released:" in joined:
            released_info = joined
        if len(row) > 1 and re.search(r"\d{2}-[A-Za-z]{3}-\d{2}", row[1].strip()):
            week_ending_col_label = row[1].strip()
        label = row[0].strip() if row else ""
        if label in {"East", "Midwest", "Mountain", "Pacific", "South Central", "Total"}:
            # Columns in wngsr.csv:
            # 1: current week stocks, 4: prev week stocks, 7: net change (Bcf),
            # 10: implied flow (Bcf), 13: year ago stocks, 16: yoy % change,
            # 19: 5-yr avg stocks, 22: vs 5-yr avg % change
            cur = _to_float(row[1]) if len(row) > 1 else None
            prev = _to_float(row[4]) if len(row) > 4 else None
            net_chg = _to_float(row[7]) if len(row) > 7 else None
            imp_flow = _to_float(row[10]) if len(row) > 10 else None
            yoy_stocks = _to_float(row[13]) if len(row) > 13 else None
            yoy_pct = _to_float(row[16]) if len(row) > 16 else None
            avg5_stocks = _to_float(row[19]) if len(row) > 19 else None
            avg5_pct = _to_float(row[22]) if len(row) > 22 else None
            avg5_diff = round(cur - avg5_stocks, 2) if (cur is not None and avg5_stocks is not None) else None
            entry = {
                "current_bcf": cur,
                "previous_week_bcf": prev,
                "net_change_bcf": net_chg,
                "implied_flow_bcf": imp_flow,
                "year_ago_bcf": yoy_stocks,
                "vs_year_ago_pct": yoy_pct,
                "five_year_avg_bcf": avg5_stocks,
                "vs_five_year_avg_bcf": avg5_diff,
                "vs_five_year_avg_pct": avg5_pct,
            }
            if label == "Total":
                total_row = entry
            else:
                regions[label] = entry

    if total_row is None:
        raise ValueError("Total working gas row not found in EIA NGS CSV")

    return {
        "report_date": week_ending_col_label,
        "released_info": released_info,
        "unit": "Bcf",
        "total_working_gas": total_row,
        "regions": regions,
    }


def _classify_xbrl_period_type(period_days: int | None) -> str:
    if period_days is None:
        return "instant"
    if period_days <= 105:
        return "quarterly"
    if period_days <= 300:
        return "ytd_or_multi_quarter"
    return "annual"


def _extract_xbrl_metric(
    us_gaap: dict[str, Any],
    candidate_tags: list[str],
    limit: int = 8,
    note: str | None = None,
    basis: str | None = None,
) -> dict[str, Any] | None:
    """Merge observations across all matching candidate tags so tag switches never drop newer periods."""
    merged: dict[tuple[str | None, str], dict[str, Any]] = {}
    tags_found: list[str] = []
    primary_unit: str | None = None

    for tag_rank, tag in enumerate(candidate_tags):
        tag_obj = us_gaap.get(tag)
        if not isinstance(tag_obj, dict):
            continue
        units = tag_obj.get("units") or {}
        if not units:
            continue
        unit_name = next(iter(units.keys()))
        for preferred_unit in ("USD", "USD/shares", "shares"):
            if preferred_unit in units:
                unit_name = preferred_unit
                break
        raw_items = units.get(unit_name) or []
        tag_had_valid = False
        for item in raw_items:
            form = str(item.get("form") or "")
            if form not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
                continue
            end = item.get("end")
            val = item.get("val")
            if not end or val is None:
                continue
            cleaned_val = _clean_scalar(val)
            if cleaned_val is None:
                continue
            tag_had_valid = True
            start = item.get("start")
            key = (start, str(end))
            filed = str(item.get("filed") or "")
            existing = merged.get(key)
            # Higher-priority tag (lower tag_rank) always wins for the same period;
            # within the same tag_rank, the newer filing date wins (restatement / amendment).
            should_replace = (
                existing is None
                or tag_rank < existing["_tag_rank"]
                or (tag_rank == existing["_tag_rank"] and filed > str(existing.get("filed") or ""))
            )
            if should_replace:
                period_days = None
                if start:
                    try:
                        d_start = date.fromisoformat(str(start))
                        d_end = date.fromisoformat(str(end))
                        period_days = (d_end - d_start).days + 1
                    except ValueError:
                        period_days = None
                merged[key] = {
                    "_tag_rank": tag_rank,
                    "start": start,
                    "end": str(end),
                    "period_days": period_days,
                    "period_type": _classify_xbrl_period_type(period_days),
                    "val": cleaned_val,
                    "unit": unit_name,
                    "tag": tag,
                    "form": form,
                    "fy": item.get("fy"),
                    "fp": item.get("fp"),
                    "filed": filed,
                }
        if tag_had_valid:
            tags_found.append(tag)
            if primary_unit is None:
                primary_unit = unit_name

    if not merged:
        return None

    records = sorted(
        merged.values(),
        key=lambda x: (
            str(x.get("end") or ""),
            str(x.get("filed") or ""),
            -(x.get("period_days") or 0),
        ),
        reverse=True,
    )[:limit]
    for r in records:
        r.pop("_tag_rank", None)
    out: dict[str, Any] = {
        "tag": records[0]["tag"],
        "tags_used": tags_found,
        "unit": records[0]["unit"] or primary_unit,
        "records": records,
    }
    if basis:
        out["basis"] = basis
    if note:
        out["note"] = note
    return out


def extract_sec_companyfacts(facts_json: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    """Extract core US-GAAP metrics from SEC EDGAR companyfacts JSON."""
    if not isinstance(facts_json, dict):
        raise ValueError("companyfacts payload must be a JSON object")
    us_gaap = (facts_json.get("facts") or {}).get("us-gaap") or {}
    if not us_gaap:
        raise ValueError("No us-gaap section found in SEC companyfacts")

    metrics: dict[str, Any] = {}
    specs = [
        (
            "revenue",
            [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
                "SalesRevenueServicesNet",
            ],
            None,
            None,
        ),
        ("net_income", ["NetIncomeLoss"], None, None),
        ("eps_diluted", ["EarningsPerShareDiluted"], None, None),
        (
            "operating_cash_flow",
            ["NetCashProvidedByUsedInOperatingActivities"],
            "GAAP_XBRL_reported_may_be_YTD",
            None,
        ),
        (
            "capex",
            [
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "PaymentsToAcquireProductiveAssets",
            ],
            "GAAP_XBRL_reported_may_be_YTD",
            None,
        ),
        (
            "remaining_performance_obligation",
            ["RevenueRemainingPerformanceObligation"],
            "Point-in-time contract backlog",
            "Hard contract backlog / RPO reference",
        ),
    ]
    for metric_key, tags, basis, note in specs:
        extracted = _extract_xbrl_metric(us_gaap, tags, limit=limit, note=note, basis=basis)
        if extracted is not None:
            metrics[metric_key] = extracted

    return {
        "entity_name": facts_json.get("entityName"),
        "cik": str(facts_json.get("cik") or "").zfill(10) if facts_json.get("cik") is not None else None,
        "metrics": metrics,
    }


def _parse_contract_month(symbol: str, ref_year: int | None = None) -> tuple[int, str] | None:
    """Extract chronological YYYYMM sort key from 4-digit (YYMM) or 3-digit CZCE (YMM) contract symbols."""
    m = re.search(r"(\d+)$", (symbol or "").strip().upper())
    if not m:
        return None
    digits = m.group(1)
    if digits == "0" or len(digits) < 3 or len(digits) > 4:
        return None
    cur_year = ref_year or datetime.now(timezone.utc).year
    if len(digits) == 4:
        yy, mm = int(digits[:2]), int(digits[2:])
        if not (1 <= mm <= 12):
            return None
        full_year = 2000 + yy
    else:
        y_digit, mm = int(digits[0]), int(digits[1:])
        if not (1 <= mm <= 12):
            return None
        decade = (cur_year // 10) * 10
        candidates = [decade - 10 + y_digit, decade + y_digit, decade + 10 + y_digit]
        # Pick candidate closest to cur_year (handles decade wraps e.g. CF912 in 2030 -> 202912)
        full_year = min(candidates, key=lambda y: (abs(y - cur_year), -y))
    return full_year * 100 + mm, f"{full_year % 100:02d}{mm:02d}"


def summarize_term_structure(raw_data: pd.DataFrame | list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize full-curve futures term structure (Backwardation / Contango / Flat / insufficient_contracts)."""
    if isinstance(raw_data, pd.DataFrame):
        rows = _df_to_records_clean(raw_data)
    elif isinstance(raw_data, list):
        rows = raw_data
    else:
        raise ValueError("Term structure input must be DataFrame or list of dicts")

    contracts: list[dict[str, Any]] = []
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        parsed_month = _parse_contract_month(sym)
        if parsed_month is None:
            continue
        sort_month, month_code = parsed_month
        price = None
        price_basis = None
        for col in ("trade", "close", "settlement", "prevsettlement"):
            cand = _to_float(row.get(col))
            if cand is not None and cand > 0:
                price = cand
                price_basis = col
                break
        if price is None:
            continue
        contracts.append(
            {
                "symbol": sym,
                "name": row.get("name"),
                "month_code": month_code,
                "_sort_month": sort_month,
                "price": round(price, 4),
                "price_basis": price_basis,
                "settlement": _to_float(row.get("settlement")),
                "volume": _to_int(row.get("volume")),
                "open_interest": _to_int(row.get("position") or row.get("open_interest")),
                "tradedate": row.get("tradedate"),
            }
        )

    if not contracts:
        raise ValueError("No valid dated futures contracts found for term structure")

    contracts.sort(key=lambda c: c["_sort_month"])
    for c in contracts:
        c.pop("_sort_month", None)

    front = contracts[0]
    back = contracts[-1]
    spread_near_minus_far = round(front["price"] - back["price"], 4) if len(contracts) >= 2 else None
    spread_far_minus_near = round(back["price"] - front["price"], 4) if len(contracts) >= 2 else None
    spread_pct = (
        round((back["price"] / front["price"] - 1.0) * 100.0, 3)
        if len(contracts) >= 2 and front["price"] > 0
        else None
    )

    if len(contracts) < 2:
        structure = "insufficient_contracts"
    elif abs(spread_near_minus_far) <= front["price"] * 1e-4:
        structure = "Flat"
    elif front["price"] > back["price"]:
        structure = "Backwardation"
    else:
        structure = "Contango"

    return {
        "contracts_count": len(contracts),
        "structure": structure,
        "quote_date": front.get("tradedate"),
        "front_contract": front["symbol"],
        "front_price": front["price"],
        "back_contract": back["symbol"] if len(contracts) >= 2 else None,
        "back_price": back["price"] if len(contracts) >= 2 else None,
        "near_minus_far_spread": spread_near_minus_far,
        "far_minus_near_spread": spread_far_minus_near,
        "far_vs_near_pct": spread_pct,
        "contracts": contracts,
    }


def parse_farside_btc_etf_html(html_text: str, last_n: int = 5) -> dict[str, Any]:
    """Parse Farside Investors BTC Spot ETF daily flow HTML table."""
    if not html_text or not html_text.strip():
        raise ValueError("Empty Farside HTML")
    tables = re.findall(r"<table.*?>.*?</table>", html_text, re.S | re.I)
    target_table = None
    for tb in tables:
        if "Total" in tb and ("IBIT" in tb or "FBTC" in tb or "GBTC" in tb):
            target_table = tb
            break
    if target_table is None:
        raise ValueError("BTC ETF flow table not found in Farside HTML")

    raw_rows = re.findall(r"<tr.*?>(.*?)</tr>", target_table, re.S | re.I)
    parsed_rows: list[list[str]] = []
    for tr in raw_rows:
        cells = re.findall(r"<t[hd].*?>(.*?)</t[hd]>", tr, re.S | re.I)
        cleaned = [
            html_lib.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in cells
        ]
        if cleaned:
            parsed_rows.append(cleaned)

    headers: list[str] = []
    date_rows: list[dict[str, Any]] = []
    summary_total: dict[str, Any] | None = None

    date_re = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$|^\d{4}-\d{2}-\d{2}$")
    for r in parsed_rows:
        first = r[0]
        if not headers and ("IBIT" in r or "FBTC" in r):
            headers = [h if h else (f"col_{i}" if i > 0 else "Date") for i, h in enumerate(r)]
            if headers and headers[-1].startswith("col_"):
                headers[-1] = "Total"
            continue
        if date_re.match(first):
            total_val = _to_float(r[-1])
            etf_flows: dict[str, float | None] = {}
            if headers and len(headers) == len(r):
                for h_name, cell_val in zip(headers[1:-1], r[1:-1]):
                    etf_flows[h_name] = _to_float(cell_val)
            date_rows.append(
                {
                    "date": first,
                    "total_net_flow_musd": total_val,
                    "etf_flows_musd": etf_flows,
                }
            )
        elif first.lower() == "total":
            summary_total = {"cumulative_net_flow_musd": _to_float(r[-1])}

    if not date_rows:
        raise ValueError("No daily BTC ETF flow rows parsed from Farside HTML")

    recent = date_rows[-last_n:]
    non_null_flows = [r["total_net_flow_musd"] for r in recent if r["total_net_flow_musd"] is not None]
    recent_sum = round(sum(non_null_flows), 2) if non_null_flows else None
    return {
        "unit": "USD Millions",
        "recent_days_count": len(recent),
        "recent_valid_days_count": len(non_null_flows),
        "recent_total_net_flow_musd": recent_sum,
        "recent_daily_flows": recent,
        "cumulative_summary": summary_total,
    }


# ---------------------------------------------------------------------------
# Live Fetchers (Each Source Independently Isolated)
# ---------------------------------------------------------------------------

def _get_sec_cik(symbol: str) -> str:
    sym_upper = symbol.strip().upper()
    candidates = [sym_upper, sym_upper.replace(".", "-"), sym_upper.replace("-", ".")]
    for c in candidates:
        if c in _SEC_TICKER_CACHE:
            return _SEC_TICKER_CACHE[c]

    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=SEC_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    for item in payload.values():
        t = str(item.get("ticker") or "").strip().upper()
        cik = str(item.get("cik_str") or "").zfill(10)
        if t and cik:
            _SEC_TICKER_CACHE[t] = cik

    for c in candidates:
        if c in _SEC_TICKER_CACHE:
            return _SEC_TICKER_CACHE[c]
    raise KeyError(f"SEC CIK not found for ticker {symbol}")


def fetch_sec_edgar(symbol: str) -> dict[str, Any]:
    cik = _get_sec_cik(symbol)
    cik_int = int(cik)

    sub_resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers=SEC_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    sub_resp.raise_for_status()
    sub_json = sub_resp.json()
    recent = (sub_json.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    accessions = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []

    filings: list[dict[str, Any]] = []
    for idx, form in enumerate(forms):
        if form not in ("10-Q", "10-K"):
            continue
        acc = accessions[idx] if idx < len(accessions) else ""
        doc = primary_docs[idx] if idx < len(primary_docs) else ""
        acc_nodash = str(acc).replace("-", "")
        html_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
            if acc_nodash and doc
            else None
        )
        filings.append(
            {
                "form": form,
                "filingDate": filing_dates[idx] if idx < len(filing_dates) else None,
                "reportDate": report_dates[idx] if idx < len(report_dates) else None,
                "accessionNumber": acc,
                "primaryDocument": doc,
                "url": html_url,
            }
        )
        if len(filings) >= 4:
            break

    facts_resp = requests.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        headers=SEC_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    facts_resp.raise_for_status()
    facts_summary = extract_sec_companyfacts(facts_resp.json(), limit=8)

    return {
        "cik": cik,
        "entity_name": sub_json.get("name") or facts_summary.get("entity_name"),
        "sic_description": sub_json.get("sicDescription"),
        "recent_filings": filings,
        "gaap_metrics": facts_summary.get("metrics", {}),
    }


def _extract_stmt_rows(df: pd.DataFrame | None, row_candidates: dict[str, list[str]], n_cols: int = 4) -> dict[str, Any]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    cols = list(df.columns)[:n_cols]
    sub = df[cols]
    out: dict[str, Any] = {}
    idx_lookup = {str(i).strip(): i for i in sub.index}
    for out_key, names in row_candidates.items():
        matched_idx = None
        for cand in names:
            if cand in idx_lookup:
                matched_idx = idx_lookup[cand]
                break
        if matched_idx is None:
            continue
        series = sub.loc[matched_idx]
        out[out_key] = {
            str(_clean_scalar(c))[:10]: _clean_scalar(series[c])
            for c in cols
        }
    return out


def fetch_yfinance_consensus(symbol: str) -> dict[str, Any]:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    tk = yf.Ticker(symbol)

    def _safe_attr(fn):
        try:
            return fn()
        except Exception as exc:
            return {"error": str(exc)}

    earnings_est = _safe_attr(lambda: _df_to_records_clean(tk.earnings_estimate, orient="index"))
    revenue_est = _safe_attr(lambda: _df_to_records_clean(tk.revenue_estimate, orient="index"))
    eps_trend = _safe_attr(lambda: _df_to_records_clean(tk.eps_trend, orient="index"))

    def _get_targets():
        t = tk.analyst_price_targets
        if isinstance(t, dict):
            return {str(k): _clean_scalar(v) for k, v in t.items()}
        if isinstance(t, pd.DataFrame):
            return _df_to_records_clean(t, orient="records")
        return _clean_scalar(t)

    price_targets = _safe_attr(_get_targets)

    q_inc = _safe_attr(
        lambda: _extract_stmt_rows(
            tk.quarterly_income_stmt,
            {
                "total_revenue": ["Total Revenue", "Operating Revenue"],
                "operating_income": ["Operating Income", "EBIT"],
                "net_income": ["Net Income", "Net Income Common Stockholders"],
            },
            n_cols=4,
        )
    )
    q_cf = _safe_attr(
        lambda: _extract_stmt_rows(
            tk.quarterly_cashflow,
            {
                "operating_cash_flow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
                "capital_expenditure": ["Capital Expenditure", "Purchase Of PPE"],
                "free_cash_flow": ["Free Cash Flow"],
            },
            n_cols=4,
        )
    )

    sub_sections = {
        "analyst_price_targets": price_targets,
        "earnings_estimate": earnings_est,
        "revenue_estimate": revenue_est,
        "eps_trend": eps_trend,
        "quarterly_income_stmt": q_inc,
        "quarterly_cashflow": q_cf,
    }

    def _section_has_data(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, dict):
            if "error" in v or not v:
                return False
            return any(_section_has_data(child) for child in v.values())
        if isinstance(v, list):
            return any(_section_has_data(child) for child in v)
        return True

    err_count = sum(1 for v in sub_sections.values() if isinstance(v, dict) and "error" in v)
    valid_count = sum(1 for v in sub_sections.values() if _section_has_data(v))
    if valid_count == 0:
        status = "error"
    elif err_count == 0 and valid_count == len(sub_sections):
        status = "ok"
    else:
        status = "partial"
    res: dict[str, Any] = {
        "status": status,
        "basis_warning": "yfinance forward estimates are typically Non-GAAP/Adjusted; compare with SEC GAAP",
        **sub_sections,
    }
    if status == "error":
        res["error"] = "No non-empty yfinance consensus data available"
    return res


def fetch_ashare_financial_abstract(code6: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    df = _run_with_timeout(
        lambda: ak.stock_financial_abstract_ths(symbol=code6, indicator="按报告期"),
        timeout_s=HTTP_TIMEOUT,
    )
    if df is None or df.empty:
        raise ValueError(f"Empty THS financial abstract for {code6}")
    if "报告期" in df.columns:
        df = df.sort_values("报告期", ascending=False)
    latest = df.head(6)
    return {
        "symbol": code6,
        "periods_count": len(latest),
        "records": _df_to_records_clean(latest),
    }


def fetch_ashare_profit_forecast(code6: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    df = _run_with_timeout(
        lambda: ak.stock_profit_forecast_ths(symbol=code6, indicator="预测年报每股收益"),
        timeout_s=HTTP_TIMEOUT,
    )
    if df is None or df.empty:
        raise ValueError(f"Empty THS profit forecast for {code6}")
    return {
        "symbol": code6,
        "forecast_eps": _df_to_records_clean(df),
    }


def fetch_ashare_hsgt_flow() -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    df = _run_with_timeout(lambda: ak.stock_hsgt_fund_flow_summary_em(), timeout_s=HTTP_TIMEOUT)
    if df is None or df.empty:
        raise ValueError("Empty HSGT fund flow summary")
    records = _df_to_records_clean(df)
    northbound = [r for r in records if r.get("资金方向") == "北向"]
    nb_net_values = [
        _to_float(r.get("成交净买额")) or _to_float(r.get("资金净流入")) or 0.0
        for r in northbound
    ]
    all_zero_nb = bool(northbound) and all(abs(v) < 1e-9 for v in nb_net_values)
    out: dict[str, Any] = {
        "northbound": northbound,
        "northbound_net_buy_disclosed": bool(northbound) and not all_zero_nb,
        "all_channels": records,
    }
    if not northbound or all_zero_nb:
        out["disclosure_note"] = (
            "HKEX discontinued daily northbound net buy/sell disclosure in Aug 2024; "
            "missing or 0 net-buy values reflect non-disclosure rather than zero capital flow."
        )
    return out


def fetch_cn_future_inventory(cn_name: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    if cn_name in INVENTORY_EM_BLACKLIST:
        return {"status": "skipped", "reason": f"Blacklisted inventory symbol: {cn_name}"}
    df = _run_with_timeout(lambda: ak.futures_inventory_em(symbol=cn_name), timeout_s=HTTP_TIMEOUT)
    if df is None or df.empty:
        raise ValueError(f"Empty futures_inventory_em for {cn_name}")
    df = df.copy()
    date_col = "日期" if "日期" in df.columns else df.columns[0]
    inv_col = "库存" if "库存" in df.columns else df.columns[1]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[inv_col] = pd.to_numeric(df[inv_col], errors="coerce")
    df = df.dropna(subset=[date_col, inv_col]).sort_values(date_col, ascending=True).reset_index(drop=True)
    if df.empty:
        raise ValueError(f"No valid inventory rows for {cn_name}")

    latest_date = df[date_col].iloc[-1].date().isoformat()
    latest_inv = float(df[inv_col].iloc[-1])

    def _chg(n: int) -> tuple[float | None, float | None]:
        if len(df) > n:
            prev = float(df[inv_col].iloc[-1 - n])
            diff = round(latest_inv - prev, 4)
            pct = round((latest_inv / prev - 1.0) * 100.0, 2) if prev != 0 else None
            return diff, pct
        return None, None

    chg_5d, chg_5d_pct = _chg(5)
    chg_20d, chg_20d_pct = _chg(20)
    return {
        "symbol_cn": cn_name,
        "latest_date": latest_date,
        "latest_inventory": round(latest_inv, 4),
        "chg_5d": chg_5d,
        "chg_5d_pct": chg_5d_pct,
        "chg_20d": chg_20d,
        "chg_20d_pct": chg_20d_pct,
    }


def _call_spot_price_with_timeout(date_str: str, spot_code: str, timeout_s: float = 10.0) -> pd.DataFrame:
    return _run_with_timeout(
        lambda: ak.futures_spot_price(date=date_str, vars_list=[spot_code]),
        timeout_s=timeout_s,
        silence_stdout=True,
    )


def fetch_cn_future_spot_basis(spot_code: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    today = datetime.now().date()
    bdays = pd.bdate_range(end=today, periods=5)[::-1]
    last_err: Exception | None = None
    for idx, dt in enumerate(bdays):
        date_str = dt.strftime("%Y%m%d")
        try:
            per_call_timeout = 8.0 if idx == 0 else 10.0
            df = _call_spot_price_with_timeout(date_str, spot_code, timeout_s=per_call_timeout)
            if df is None or df.empty:
                continue
            matched = df[df["symbol"].astype(str).str.upper() == spot_code.upper()]
            if matched.empty:
                continue
            row = matched.iloc[0]
            raw_rate = _to_float(row.get("dom_basis_rate"))
            return {
                "date": _clean_scalar(row.get("date")) or date_str,
                "spot_code": spot_code,
                "spot_price": _to_float(row.get("spot_price")),
                "dominant_contract": _clean_scalar(row.get("dominant_contract")),
                "dominant_contract_price": _to_float(row.get("dominant_contract_price")),
                "dominant_basis": _to_float(row.get("dom_basis")),
                "dominant_basis_rate": raw_rate,
                "dominant_basis_rate_pct": round(raw_rate * 100.0, 3) if raw_rate is not None else None,
                "near_contract": _clean_scalar(row.get("near_contract")),
                "near_contract_price": _to_float(row.get("near_contract_price")),
                "near_basis": _to_float(row.get("near_basis")),
            }
        except Exception as exc:
            last_err = exc
    if last_err is not None:
        raise last_err
    raise ValueError(f"No spot/basis data found for {spot_code} in last 5 business days")


def fetch_cn_future_term_structure(cn_name: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    if cn_name in TERM_STRUCTURE_SKIP:
        return {"status": "skipped", "reason": f"Skipped timeout-prone realtime symbol: {cn_name}"}
    df = _run_with_timeout(lambda: ak.futures_zh_realtime(symbol=cn_name), timeout_s=HTTP_TIMEOUT)
    if df is None or df.empty:
        raise ValueError(f"Empty futures_zh_realtime for {cn_name}")
    summary = summarize_term_structure(df)
    summary["symbol_cn"] = cn_name
    return summary


def _fetch_nbs_monthly_series(path: str) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    df = _run_with_timeout(
        lambda: ak.macro_china_nbs_nation(kind="月度数据", path=path, period="LAST6"),
        timeout_s=HTTP_TIMEOUT,
    )
    if df is None or df.empty:
        raise ValueError(f"Empty NBS response for path: {path}")
    return {
        "path": path,
        "periods": [_clean_scalar(c) for c in df.columns],
        "series": _df_to_records_clean(df, orient="index"),
    }


def fetch_china_nbs_sector(macro_sector: str) -> dict[str, Any]:
    if macro_sector == "ferrous":
        paths = [
            ("crude_steel", "工业 > 工业主要产品产量 > 粗钢"),
            ("real_estate_investment", "房地产 > 房地产开发投资情况"),
        ]
    elif macro_sector == "nonferrous":
        paths = [
            ("primary_aluminum", "工业 > 工业主要产品产量 > 原铝（电解铝）"),
            ("ten_nonferrous_metals", "工业 > 工业主要产品产量 > 十种有色金属"),
        ]
    elif macro_sector in {"energy", "chemical"}:
        paths = [
            ("power_generation", "能源 > 能源主要产品产量 > 发电量"),
        ]
    else:
        return {"status": "skipped", "reason": f"No sector-specific NBS mapping for sector={macro_sector}"}

    indicators: dict[str, Any] = {}
    ok_count = 0
    for key, path in paths:
        try:
            indicators[key] = {"status": "ok", **_fetch_nbs_monthly_series(path)}
            ok_count += 1
        except Exception as exc:
            indicators[key] = {"status": "error", "error": str(exc), "path": path}

    status = "ok" if ok_count == len(paths) else ("partial" if ok_count > 0 else "error")
    out: dict[str, Any] = {"status": status, "sector": macro_sector, "indicators": indicators}
    if status == "error":
        out["error"] = f"All NBS sector indicators failed for {macro_sector}"
    return out


def fetch_cftc_cot(symbol: str) -> dict[str, Any]:
    spec = CFTC_CONTRACT_SPECS.get(symbol.upper())
    if not spec:
        return {"status": "skipped", "reason": f"No CFTC COT mapping for {symbol}"}
    resp = requests.get(
        "https://www.cftc.gov/dea/newcot/f_disagg.txt",
        headers=DEFAULT_BROWSER_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return parse_cftc_disagg(resp.text, market_query=spec["patterns"], contract_code=spec["code"])


def fetch_eia_data(symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    if sym in EIA_WPSR_SYMBOLS:
        resp = requests.get("https://ir.eia.gov/wpsr/table1.csv", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = parse_eia_wpsr_csv(resp.text)
        data["report_type"] = "EIA Weekly Petroleum Status Report (WPSR)"
        return data
    if sym in EIA_NGS_SYMBOLS:
        resp = requests.get("https://ir.eia.gov/ngs/wngsr.csv", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = parse_eia_ngs_csv(resp.text)
        data["report_type"] = "EIA Weekly Natural Gas Storage Report"
        return data
    return {"status": "skipped", "reason": f"No EIA weekly CSV applicable for {symbol}"}


def _first_attr_with_prefix(attrib: dict[str, str], prefix: str) -> str | None:
    for k, v in attrib.items():
        if k.startswith(prefix) and v is not None:
            return re.sub(r"\s+", " ", v).strip()
    return None


def parse_usda_wasde_xml(xml_text: str, commodity: str, subreports: list[str]) -> dict[str, Any]:
    """Parse USDA WASDE monthly XML across all attributeN / mN_region_groupN layouts including Ending Stocks."""
    root = ET.fromstring(xml_text)
    extracted_tables: list[dict[str, Any]] = []
    report_month = None
    total_rows = 0

    attr_tag_re = re.compile(r"^attribute\d+$")
    year_tag_re = re.compile(r"^m\d+_year_group\d*$")
    month_tag_re = re.compile(r"^m\d+_month_group\d*$")
    matrix_tag_re = re.compile(r"^matrix\d+$")
    region_tag_re = re.compile(r"^m\d+_region_group\d*$")
    mat_attr_tag_re = re.compile(r"^m\d+_attribute_group\d*$")

    for sr_tag in subreports:
        sr_el = root.find(f".//{sr_tag}")
        if sr_el is None:
            continue
        report_el = sr_el.find("Report")
        if report_el is None:
            report_el = sr_el
        r_month = report_el.attrib.get("Report_Month")
        if r_month and not report_month:
            report_month = r_month
        sub_title = report_el.attrib.get("sub_report_title")
        subtitle = report_el.attrib.get("sub_report_subtitle")

        rows_summary: list[dict[str, Any]] = []

        # 1) US supply & use style tables: attribute1 / attribute2 / attribute4 / attribute5 / attribute6
        for elem in sr_el.iter():
            if not attr_tag_re.match(elem.tag):
                continue
            attr_name = _first_attr_with_prefix(elem.attrib, "attribute")
            if not attr_name:
                continue
            vals: list[dict[str, Any]] = []
            for yg in elem.iter():
                if not year_tag_re.match(yg.tag):
                    continue
                myear = _first_attr_with_prefix(yg.attrib, "market_year") or ""
                m_groups = [mg for mg in yg.iter() if month_tag_re.match(mg.tag)]
                if m_groups:
                    for mg in m_groups:
                        fmonth = _first_attr_with_prefix(mg.attrib, "forecast_month") or ""
                        cell = mg.find("Cell")
                        cval = _first_attr_with_prefix(cell.attrib, "cell_value") if cell is not None else None
                        if cval is not None:
                            num = _to_float(cval)
                            vals.append({
                                "market_year": myear,
                                "forecast_month": fmonth,
                                "value": num if num is not None else cval,
                            })
                else:
                    for cell in yg.findall(".//Cell"):
                        cval = _first_attr_with_prefix(cell.attrib, "cell_value")
                        if cval is not None:
                            num = _to_float(cval)
                            vals.append({"market_year": myear, "forecast_month": "", "value": num if num is not None else cval})
            if vals:
                rows_summary.append({"attribute": attr_name, "values": vals[-4:]})

        # 2) World region-matrix tables (sr18/sr22/sr26/sr28): extract ALL columns (incl. Ending Stocks) across matrices
        if not rows_summary:
            matrices = [m for m in sr_el.iter() if matrix_tag_re.match(m.tag)]
            if not matrices:
                matrices = [sr_el]
            for mat in matrices:
                myear = _first_attr_with_prefix(mat.attrib, "region_header") or ""
                for reg_el in mat.iter():
                    if not region_tag_re.match(reg_el.tag):
                        continue
                    reg_name = _first_attr_with_prefix(reg_el.attrib, "region")
                    if not reg_name:
                        continue
                    metrics_map: dict[str, Any] = {}
                    for ag in reg_el.iter():
                        if not mat_attr_tag_re.match(ag.tag):
                            continue
                        aname = _first_attr_with_prefix(ag.attrib, "attribute")
                        cell = ag.find(".//Cell")
                        cval = _first_attr_with_prefix(cell.attrib, "cell_value") if cell is not None else None
                        if aname and cval is not None:
                            num = _to_float(cval)
                            metrics_map[aname] = num if num is not None else cval
                    if metrics_map:
                        rows_summary.append({"market_year": myear, "region": reg_name, "metrics": metrics_map})
                    else:
                        cells = [c.attrib for c in reg_el.findall(".//Cell") if c.attrib]
                        if cells:
                            rows_summary.append({"market_year": myear, "region": reg_name, "cells": cells})

        total_rows += len(rows_summary)
        extracted_tables.append(
            {
                "subreport": sr_tag,
                "title": sub_title,
                "unit": subtitle,
                "rows_count": len(rows_summary),
                "rows": rows_summary,
            }
        )

    if not extracted_tables or total_rows == 0:
        raise ValueError(f"No non-empty WASDE rows parsed from {subreports} for {commodity}")

    return {
        "commodity": commodity,
        "report_month": report_month,
        "tables": extracted_tables,
    }


def fetch_usda_wasde(symbol: str) -> dict[str, Any]:
    spec = USDA_WASDE_SYMBOLS.get(symbol.upper())
    if not spec:
        return {"status": "skipped", "reason": f"USDA WASDE not applicable for {symbol}"}
    now = datetime.now(timezone.utc).date()
    candidate_dates = [now, now.replace(day=1) - timedelta(days=1), now.replace(day=1) - timedelta(days=32)]
    seen_codes: list[str] = []
    for dt in candidate_dates:
        code = dt.strftime("%m%y")
        if code not in seen_codes:
            seen_codes.append(code)

    last_err: Exception | None = None
    for code in seen_codes:
        url = f"https://www.usda.gov/oce/commodity/wasde/wasde{code}.xml"
        try:
            resp = requests.get(url, headers=DEFAULT_BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            parsed = parse_usda_wasde_xml(resp.text, spec["commodity"], spec["subreports"])
            parsed["source_url"] = url
            return parsed
        except Exception as exc:
            last_err = exc
    if last_err is not None:
        raise last_err
    raise ValueError(f"USDA WASDE XML unavailable for codes {seen_codes}")


def fetch_defillama_stablecoins() -> dict[str, Any]:
    resp = requests.get(
        "https://stablecoins.llama.fi/stablecoins?includePrices=false",
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    assets = payload.get("peggedAssets") or []
    if not assets:
        raise ValueError("No peggedAssets returned from DefiLlama")

    total_now = 0.0
    total_week = 0.0
    total_month = 0.0
    ranked: list[dict[str, Any]] = []

    for item in assets:
        cur = _to_float((item.get("circulating") or {}).get("peggedUSD")) or 0.0
        prev_w = _to_float((item.get("circulatingPrevWeek") or {}).get("peggedUSD")) or 0.0
        prev_m = _to_float((item.get("circulatingPrevMonth") or {}).get("peggedUSD")) or 0.0
        total_now += cur
        total_week += prev_w
        total_month += prev_m
        ranked.append(
            {
                "name": item.get("name"),
                "symbol": item.get("symbol"),
                "circulating_usd": round(cur, 2),
                "change_7d_usd": round(cur - prev_w, 2) if prev_w > 0 else None,
                "change_7d_pct": round((cur / prev_w - 1.0) * 100.0, 2) if prev_w > 0 else None,
                "change_30d_usd": round(cur - prev_m, 2) if prev_m > 0 else None,
                "change_30d_pct": round((cur / prev_m - 1.0) * 100.0, 2) if prev_m > 0 else None,
            }
        )

    ranked.sort(key=lambda x: x["circulating_usd"], reverse=True)
    return {
        "total_circulating_usd": round(total_now, 2),
        "total_change_7d_usd": round(total_now - total_week, 2) if total_week > 0 else None,
        "total_change_7d_pct": round((total_now / total_week - 1.0) * 100.0, 2) if total_week > 0 else None,
        "total_change_30d_usd": round(total_now - total_month, 2) if total_month > 0 else None,
        "total_change_30d_pct": round((total_now / total_month - 1.0) * 100.0, 2) if total_month > 0 else None,
        "top5_stablecoins": ranked[:5],
    }


def fetch_crypto_exchange_oi(exchange_id: str, base: str) -> dict[str, Any]:
    if ccxt is None:
        raise RuntimeError("ccxt is not installed")
    perp_sym = f"{base}/USDT:USDT"
    ex = getattr(ccxt, exchange_id)({"timeout": 8000})
    oi = ex.fetch_open_interest(perp_sym)
    info = oi.get("info") if isinstance(oi.get("info"), dict) else {}
    if exchange_id == "okx":
        amount_unit = "contracts"
        oi_usd = _clean_scalar(oi.get("openInterestValue")) or _to_float(info.get("oiUsd"))
    else:
        amount_unit = base.upper()
        oi_usd = _clean_scalar(oi.get("openInterestValue"))
    return {
        "exchange": exchange_id,
        "symbol": oi.get("symbol") or perp_sym,
        "open_interest_amount": _clean_scalar(oi.get("openInterestAmount")),
        "amount_unit": amount_unit,
        "open_interest_value_usd": oi_usd,
        "comparison_note": "Use open_interest_value_usd for cross-exchange comparison (amount_unit varies by exchange)",
        "datetime": oi.get("datetime"),
    }


def fetch_farside_btc_etf_flows() -> dict[str, Any]:
    url = "https://farside.co.uk/btc/"
    html_text = None
    if curl_requests is not None:
        try:
            r = curl_requests.get(url, impersonate="chrome", timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and "<table" in r.text:
                html_text = r.text
        except Exception:
            html_text = None
    if html_text is None:
        r = requests.get(url, headers=DEFAULT_BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        html_text = r.text
    return parse_farside_btc_etf_html(html_text, last_n=5)


def fetch_fred_macro() -> dict[str, Any]:
    """Fetch US macro series from FRED no-key CSV endpoints with per-series isolation."""
    series_out: dict[str, Any] = {}
    ok_count = 0
    for sid, label in FRED_SERIES:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            parsed = parse_fred_csv(resp.text, series_id=sid)
            parsed["label"] = label
            series_out[sid] = {"status": "ok", **parsed}
            ok_count += 1
        except Exception as exc:
            series_out[sid] = {"status": "error", "series_id": sid, "label": label, "error": str(exc)}

    status = "ok" if ok_count == len(FRED_SERIES) else ("partial" if ok_count > 0 else "error")
    out: dict[str, Any] = {"status": status, "series": series_out}
    if status == "error":
        out["error"] = "All FRED macro series failed"
    return out


def fetch_china_macro_core() -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("akshare is not installed")
    out: dict[str, Any] = {}
    ok_count = 0

    try:
        pmi_df = _run_with_timeout(lambda: ak.macro_china_pmi(), timeout_s=HTTP_TIMEOUT)
        out["pmi"] = {
            "status": "ok",
            "records": _df_to_records_clean(pmi_df.head(3)),
        }
        ok_count += 1
    except Exception as exc:
        out["pmi"] = {"status": "error", "error": str(exc)}

    try:
        hg_df = _run_with_timeout(lambda: ak.macro_china_hgjck(), timeout_s=HTTP_TIMEOUT)
        out["customs_trade"] = {
            "status": "ok",
            "records": _df_to_records_clean(hg_df.head(3)),
        }
        ok_count += 1
    except Exception as exc:
        out["customs_trade"] = {"status": "error", "error": str(exc)}

    status = "ok" if ok_count == 2 else ("partial" if ok_count > 0 else "error")
    res: dict[str, Any] = {"status": status, **out}
    if status == "error":
        res["error"] = "All China macro core endpoints failed"
    return res


# ---------------------------------------------------------------------------
# Aggregator Orchestration
# ---------------------------------------------------------------------------

def _call_get_snapshot(raw_symbol: str, period: str, full_chanlun: bool) -> dict[str, Any]:
    sig = inspect.signature(get_snapshot)
    kwargs: dict[str, Any] = {
        "period": period,
        "include_macro": False,
        "include_fundamentals": True,
    }
    if "full_chanlun" in sig.parameters:
        kwargs["full_chanlun"] = full_chanlun
    return get_snapshot(raw_symbol, **kwargs)


def collect_research_data(
    raw_symbol: str,
    period: str = "3y",
    include_macro: bool = True,
    full_chanlun: bool = False,
) -> dict[str, Any]:
    norm_sym, asset_class = normalize_symbol(raw_symbol)
    sources_status: dict[str, Any] = {}

    snapshot = _run_source(
        "market_snapshot",
        lambda: _run_with_timeout(
            lambda: _call_get_snapshot(raw_symbol, period=period, full_chanlun=full_chanlun),
            timeout_s=30.0,
        ),
        sources_status,
    )
    if snapshot.get("error"):
        snapshot["status"] = "error"
        sources_status["market_snapshot"] = {"status": "error", "error": snapshot["error"]}
    if snapshot.get("symbol"):
        norm_sym = snapshot["symbol"]
    if snapshot.get("asset_class"):
        asset_class = snapshot["asset_class"]

    fundamentals_and_supply: dict[str, Any] = {}

    if asset_class == "us_stock_or_etf":
        fundamentals_and_supply["sec_edgar"] = _run_source(
            "sec_edgar", lambda: fetch_sec_edgar(norm_sym), sources_status
        )
        fundamentals_and_supply["yfinance_consensus"] = _run_source(
            "yfinance_consensus", lambda: fetch_yfinance_consensus(norm_sym), sources_status
        )

    elif asset_class == "a_share":
        m = re.search(r"(\d{6})", norm_sym)
        code6 = m.group(1) if m else norm_sym.split(".")[0]
        fundamentals_and_supply["ths_financial_abstract"] = _run_source(
            "ths_financial_abstract", lambda: fetch_ashare_financial_abstract(code6), sources_status
        )
        fundamentals_and_supply["ths_profit_forecast"] = _run_source(
            "ths_profit_forecast", lambda: fetch_ashare_profit_forecast(code6), sources_status
        )
        fundamentals_and_supply["hsgt_fund_flow"] = _run_source(
            "hsgt_fund_flow", fetch_ashare_hsgt_flow, sources_status
        )

    elif asset_class == "cn_future":
        meta = CN_FUTURE_META.get(norm_sym)
        if meta is not None:
            cn_name, spot_code, macro_sector = meta
            fundamentals_and_supply["inventory_em"] = _run_source(
                "inventory_em", lambda: fetch_cn_future_inventory(cn_name), sources_status
            )
            fundamentals_and_supply["spot_and_basis"] = _run_source(
                "spot_and_basis", lambda: fetch_cn_future_spot_basis(spot_code), sources_status
            )
            fundamentals_and_supply["term_structure"] = _run_source(
                "term_structure", lambda: fetch_cn_future_term_structure(cn_name), sources_status
            )
            if include_macro:
                fundamentals_and_supply["nbs_sector_output"] = _run_source(
                    "nbs_sector_output", lambda: fetch_china_nbs_sector(macro_sector), sources_status
                )
        else:
            fundamentals_and_supply["cn_future_meta"] = {
                "status": "skipped",
                "reason": f"Unmapped cn_future symbol: {norm_sym}",
            }
            sources_status["cn_future_meta"] = {"status": "skipped", "reason": f"Unmapped symbol: {norm_sym}"}

    elif asset_class == "commodity":
        fundamentals_and_supply["cftc_cot"] = _run_source(
            "cftc_cot", lambda: fetch_cftc_cot(norm_sym), sources_status
        )
        fundamentals_and_supply["eia"] = _run_source(
            "eia", lambda: fetch_eia_data(norm_sym), sources_status
        )
        if norm_sym.upper() in USDA_WASDE_SYMBOLS:
            fundamentals_and_supply["usda_wasde"] = _run_source(
                "usda_wasde", lambda: fetch_usda_wasde(norm_sym), sources_status
            )

    elif asset_class == "crypto":
        base = norm_sym.replace("-USD", "")
        fundamentals_and_supply["stablecoins_defillama"] = _run_source(
            "stablecoins_defillama", fetch_defillama_stablecoins, sources_status
        )
        fundamentals_and_supply["perp_open_interest"] = {
            "okx": _run_source(
                "perp_oi_okx", lambda: fetch_crypto_exchange_oi("okx", base), sources_status
            ),
            "binance": _run_source(
                "perp_oi_binance", lambda: fetch_crypto_exchange_oi("binance", base), sources_status
            ),
        }
        if base == "BTC":
            fundamentals_and_supply["farside_btc_etf"] = _run_source(
                "farside_btc_etf", fetch_farside_btc_etf_flows, sources_status
            )

    macro_dual_core: dict[str, Any]
    if include_macro:
        macro_dual_core = {
            "us_fred": _run_source("us_fred", fetch_fred_macro, sources_status),
            "china_macro": _run_source("china_macro", fetch_china_macro_core, sources_status),
        }
    else:
        macro_dual_core = {"status": "skipped", "reason": "include_macro=False"}
        sources_status["us_fred"] = {"status": "skipped", "reason": "include_macro=False"}
        sources_status["china_macro"] = {"status": "skipped", "reason": "include_macro=False"}

    return {
        "query": raw_symbol,
        "symbol": norm_sym,
        "asset_class": asset_class,
        "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "snapshot": snapshot,
        "fundamentals_and_supply": fundamentals_and_supply,
        "macro_dual_core": macro_dual_core,
        "sources_status": sources_status,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Layer-3 unified free data aggregator for multi-market trading research."
    )
    parser.add_argument("symbol", help="Target symbol e.g. PWR, 600519, LC0, CU0, RB0, CL=F, NG=F, HG=F, ZC=F, BTC")
    parser.add_argument("--period", default="3y", help="History period passed to market_snapshot (default: 3y)")
    parser.add_argument("--no-macro", action="store_true", help="Skip US FRED and China macro dual-core fetch")
    parser.add_argument("--full-chanlun", action="store_true", help="Request full Chanlun detail if supported")
    parser.add_argument("--output", help="Optional path to save output JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = collect_research_data(
        raw_symbol=args.symbol,
        period=args.period,
        include_macro=not args.no_macro,
        full_chanlun=args.full_chanlun,
    )
    result = _sanitize_for_json(result)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    print(payload)
    statuses = [v.get("status") for v in (result.get("sources_status") or {}).values() if isinstance(v, dict)]
    if result.get("snapshot", {}).get("status") == "error" and not any(s in ("ok", "partial") for s in statuses):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
