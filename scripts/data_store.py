#!/usr/bin/env python3
"""Local SQLite data warehouse (``data/desk.db``) and OHLCV data-quality gatekeeper.

Shared across CLI scripts (``market_snapshot.py``, ``scan_opportunities.py``, ``daily_sentinel.py``,
``research_data.py``) and the FastAPI backend (``backend/``), physically isolated from the live
portfolio ledger (``portfolio/portfolio.db``).

Core safety invariants:
1. **5-tuple series identity**: ``(asset_class, symbol, interval, data_source, adjust_kind)`` —
   prevents cross-market collisions (e.g. ``US:CU0`` vs ``CU0``) and primary/fallback source mixing.
2. **Raw vs repaired OHLC separation**: preserves ``raw_high`` / ``raw_low`` and ``envelope_repaired``
   flags alongside envelope-valid ``high`` / ``low`` so downstream stop-breach checks never mistake a
   synthetic settlement-close wick for a real traded stop breach, and dropped corrupt rows leave
   explicit timestamp gap markers instead of silently bridging across missing bars.
3. **Fetch-time bar close provenance**: ``bar_is_closed`` is determined strictly by
   ``fetched_at_utc >= bar_end_utc`` (never recomputed solely from wall-clock ``now >= bar_end``), and
   crossing any bar-close boundary invalidates cached unclosed bars immediately.
4. **Overlap drift & CAS generation guard**: incremental merges compare closed overlap bars for
   price/key/volume drift (> 0.05% triggers atomic full-history rebuild) and commit under monotonic
   ``generation`` CAS so slower stale fetches never clobber newer data.
5. **Storage retention vs analysis window decoupling**: SQLite retains rolling history while readers
   slice strictly to the requested ``period`` / bar limit so Chanlun EMA & inclusion remain 100%
   deterministic.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, ClassVar
from zoneinfo import ZoneInfo

import pandas as pd
import requests.adapters

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DATA_DIR, "desk.db")

TTL_DAILY_FRESH_SECONDS = 900.0        # 15 minutes for 1d bars
TTL_INTRADAY_FRESH_SECONDS = 300.0     # 5 minutes for 15m/30m/60m bars
TTL_RESEARCH_FRESH_SECONDS = 14400.0   # 4 hours for browsing hard research bundles
TTL_RESEARCH_ERROR_SECONDS = 60.0      # 1 minute retry window when any source errored

DRIFT_REL_TOLERANCE = 5e-4             # 0.05% (5 bps) overlap close/OHL drift threshold
ENVELOPE_REPAIR_MAX_DEV = 0.12         # 12% max settlement-vs-pit envelope repair deviation
FLAT_DEGRADED_RATIO_60 = 0.30          # >=30% flat bars in last 60 -> degraded
FLAT_DEGRADED_RATIO_250 = 0.35         # >=35% flat bars in last 250 -> degraded
FLAT_CAUTION_RATIO_60 = 0.10           # 10%..30% flat bars in last 60 -> caution
MAX_INTRADAY_RETENTION_DAYS = 180
MAX_RESEARCH_SNAPSHOTS_PER_KEY = 10
MAX_SCAN_RUNS_RETENTION = 300
MAX_SENTINEL_RUNS_RETENTION = 300
MAX_COMPASS_RUNS_RETENTION = 60

# ---------------------------------------------------------------------------
# Scoped, context-local HTTP timeout wrapper (no global default side effects)
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT_VAR: contextvars.ContextVar[tuple[float, float] | float | None] = contextvars.ContextVar(
    "_HTTP_TIMEOUT_VAR", default=None
)
_ADAPTER_PATCH_LOCK = threading.Lock()
_ADAPTER_PATCHED = False


def _ensure_http_adapter_timeout_hook() -> None:
    global _ADAPTER_PATCHED
    if _ADAPTER_PATCHED:
        return
    with _ADAPTER_PATCH_LOCK:
        if _ADAPTER_PATCHED:
            return
        orig_send = requests.adapters.HTTPAdapter.send

        def _scoped_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
            if timeout is None:
                override = _HTTP_TIMEOUT_VAR.get()
                if override is not None:
                    timeout = override
            return orig_send(self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

        requests.adapters.HTTPAdapter.send = _scoped_send  # type: ignore[method-assign]
        _ADAPTER_PATCHED = True


@contextlib.contextmanager
def scoped_http_timeout(timeout: tuple[float, float] | float = (5.0, 12.0)):
    """Inject a default ``(connect, read)`` timeout into ``requests`` calls within this context only.

    Nested scopes clamp to the tighter of outer and inner budgets so an inner default
    (e.g. ``(5.0, 15.0)``) can never silently widen an outer deadline.
    """
    _ensure_http_adapter_timeout_hook()
    outer = _HTTP_TIMEOUT_VAR.get()
    effective = timeout
    if outer is not None:
        o_pair = (float(outer[0]), float(outer[1])) if isinstance(outer, tuple) else (float(outer), float(outer))
        i_pair = (float(timeout[0]), float(timeout[1])) if isinstance(timeout, tuple) else (float(timeout), float(timeout))
        effective = (min(o_pair[0], i_pair[0]), min(o_pair[1], i_pair[1]))
    token = _HTTP_TIMEOUT_VAR.set(effective)
    try:
        yield
    finally:
        _HTTP_TIMEOUT_VAR.reset(token)


# ---------------------------------------------------------------------------
# Store enablement & DB path resolution (auto-isolated under pytest)
# ---------------------------------------------------------------------------
def resolve_db_path(db_path: str | None = None) -> str:
    if db_path:
        return os.path.abspath(db_path)
    env_path = os.environ.get("TRADING_DESK_DB_PATH", "").strip()
    if env_path:
        return os.path.abspath(env_path)
    return DEFAULT_DB_PATH


def is_store_enabled(use_store: bool | None = None, db_path: str | None = None) -> bool:
    """Enable persistent SQLite store by default in CLI/server; auto-disable under pytest unless explicitly opted in."""
    if use_store is not None:
        return bool(use_store)
    if db_path or os.environ.get("TRADING_DESK_DB_PATH", "").strip():
        return True
    if os.environ.get("TRADING_DESK_DISABLE_STORE", "").strip() in ("1", "true", "yes"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return True


# ---------------------------------------------------------------------------
# Series Identity & Exchange Close Timing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeriesKey:
    asset_class: str
    symbol: str
    interval: str
    data_source: str
    adjust_kind: str

    @property
    def series_id(self) -> str:
        return f"{self.asset_class}|{self.symbol}|{self.interval}|{self.data_source}|{self.adjust_kind}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    target = dt.astimezone(timezone.utc) if (dt is not None and dt.tzinfo is not None) else (
        dt.replace(tzinfo=timezone.utc) if dt is not None else utc_now()
    )
    return target.isoformat(timespec="seconds")


def parse_utc_iso(val: str | None) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def period_to_days(period: str) -> int:
    """Convert period string (``6mo``, ``1y``, ``1wk``, ``1w``, ``3y``, ``max``) to calendar days."""
    p = (period or "3y").strip().lower()
    if p == "max":
        return 36500
    if p == "ytd":
        today = utc_now().date()
        return max(1, (today - date(today.year, 1, 1)).days + 1)
    m = re.fullmatch(r"(\d+)\s*(d|w|wk|mo|y)", p)
    if not m:
        return 1095
    qty = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return qty
    if unit in ("w", "wk"):
        return qty * 7
    if unit == "mo":
        return qty * 31
    return qty * 366


def default_exchange_tz_name(asset_class: str) -> str:
    ac = (asset_class or "").strip().lower()
    if ac in ("a_share", "cn_future"):
        return "Asia/Shanghai"
    if ac == "crypto":
        return "UTC"
    return "America/New_York"


def compute_daily_bar_end_utc(bar_date_str: str, asset_class: str) -> datetime | None:
    """Compute conservative official daily bar close timestamp in UTC for ``bar_is_closed`` provenance."""
    try:
        d = date.fromisoformat(bar_date_str[:10])
    except ValueError:
        return None
    ac = (asset_class or "").strip().lower()
    if ac == "crypto":
        next_d = d + timedelta(days=1)
        return datetime(next_d.year, next_d.month, next_d.day, 0, 0, 5, tzinfo=timezone.utc)
    if ac == "a_share":
        local_dt = datetime.combine(d, dt_time(15, 5), tzinfo=ZoneInfo("Asia/Shanghai"))
        return local_dt.astimezone(timezone.utc)
    if ac == "cn_future":
        local_dt = datetime.combine(d, dt_time(15, 15), tzinfo=ZoneInfo("Asia/Shanghai"))
        return local_dt.astimezone(timezone.utc)
    if ac == "us_stock_or_etf":
        local_dt = datetime.combine(d, dt_time(16, 10), tzinfo=ZoneInfo("America/New_York"))
        return local_dt.astimezone(timezone.utc)
    # Overseas futures (CME/ICE), FX (17:00 NY roll), macro indices
    local_dt = datetime.combine(d, dt_time(17, 10), tzinfo=ZoneInfo("America/New_York"))
    return local_dt.astimezone(timezone.utc)


def parse_interval_timedelta(interval: str) -> timedelta | None:
    iv = (interval or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|h)", iv)
    if not m:
        return None
    qty = int(m.group(1))
    if qty <= 0:
        return None
    return timedelta(hours=qty) if m.group(2) == "h" else timedelta(minutes=qty)


def compute_intraday_bar_end_utc(ts: pd.Timestamp, interval: str, timestamp_anchor: str) -> datetime | None:
    if ts is None or pd.isna(ts):
        return None
    ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    dur = parse_interval_timedelta(interval)
    if dur is None:
        return None
    py_dt = ts_utc.to_pydatetime()
    return py_dt if (timestamp_anchor or "start").strip().lower() == "end" else (py_dt + dur)


# ---------------------------------------------------------------------------
# OHLCV Sanitization & Data Quality Grading
# ---------------------------------------------------------------------------
def _bar_key_for_index(idx_val: Any, intraday: bool) -> tuple[str | None, str | None, pd.Timestamp | None]:
    try:
        ts = pd.Timestamp(idx_val)
    except Exception:
        return None, None, None
    if pd.isna(ts):
        return None, None, None
    if intraday:
        ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        key = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso = ts.isoformat()
        return key, iso, ts
    key = ts.strftime("%Y-%m-%d")
    iso = ts.isoformat() if ts.tzinfo is not None else key
    return key, iso, ts


def sanitize_and_grade_ohlcv(
    raw_df: pd.DataFrame | None,
    *,
    asset_class: str,
    interval: str = "1d",
    timestamp_anchor: str = "start",
    fetched_at: datetime | None = None,
    now_utc: datetime | None = None,
    check_staleness: bool = True,
    inherited_dropped_keys: list[str] | None = None,
    force_unclosed_bar_keys: set[str] | frozenset[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Sanitize an OHLCV DataFrame while preserving raw High/Low provenance and grading data quality.

    Returns ``(clean_df, quality_report)`` where ``clean_df`` contains standard ``Open, High, Low, Close, Volume``
    plus provenance columns ``_raw_high, _raw_low, _envelope_repaired, _is_flat, _bar_end_utc, _fetched_at_utc, _is_closed``.
    """
    cols = ["Open", "High", "Low", "Close", "Volume"]
    intraday = (interval or "1d").strip().lower() != "1d"
    unclosed_overrides: set[str] = set(str(k) for k in (force_unclosed_bar_keys or ()))
    if raw_df is not None and isinstance(getattr(raw_df, "attrs", None), dict):
        for k in (raw_df.attrs.get("unclosed_bar_keys") or ()):
            if k:
                unclosed_overrides.add(str(k))
    fetch_dt = (
        fetched_at.astimezone(timezone.utc)
        if (fetched_at is not None and fetched_at.tzinfo is not None)
        else (fetched_at.replace(tzinfo=timezone.utc) if fetched_at is not None else utc_now())
    )
    ref_now = (
        now_utc.astimezone(timezone.utc)
        if (now_utc is not None and now_utc.tzinfo is not None)
        else (now_utc.replace(tzinfo=timezone.utc) if now_utc is not None else fetch_dt)
    )
    fetch_iso = utc_iso(fetch_dt)

    empty_quality: dict[str, Any] = {
        "quality_grade": "degraded",
        "actionable_allowed": False,
        "bars_total": 0,
        "raw_flat_bars_60": 0,
        "raw_flat_ratio_60": 0.0,
        "raw_flat_ratio_250": 0.0,
        "repaired_ohlc_bars_total": 0,
        "repaired_ohlc_bars_60": 0,
        "max_repair_dev_pct": 0.0,
        "dropped_corrupt_bars": 0,
        "dropped_bar_keys": [],
        "zero_volume_bars_60": 0,
        "as_of_bar": None,
        "latest_bar_closed": None,
        "stale_days": None,
        "warnings": ["EMPTY_OHLCV"],
        "notes": ["未获取到有效 K 线数据"],
    }
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=cols), empty_quality

    for req_col in ("Open", "High", "Low", "Close"):
        if req_col not in raw_df.columns:
            return pd.DataFrame(columns=cols), empty_quality

    df = raw_df.copy()
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index(ascending=True, kind="mergesort")

    kept_indices: list[Any] = []
    rows_out: list[dict[str, Any]] = []
    dropped_keys: list[str] = []
    max_repair_dev = 0.0

    vol_series = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0) if "Volume" in df.columns else pd.Series(0.0, index=df.index)
    open_series = pd.to_numeric(df["Open"], errors="coerce")
    high_series = pd.to_numeric(df["High"], errors="coerce")
    low_series = pd.to_numeric(df["Low"], errors="coerce")
    close_series = pd.to_numeric(df["Close"], errors="coerce")

    for i, idx_val in enumerate(df.index):
        b_key, _, ts = _bar_key_for_index(idx_val, intraday=intraday)
        if b_key is None or ts is None:
            continue
        o = float(open_series.iloc[i]) if pd.notna(open_series.iloc[i]) else math.nan
        rh = float(high_series.iloc[i]) if pd.notna(high_series.iloc[i]) else math.nan
        rl = float(low_series.iloc[i]) if pd.notna(low_series.iloc[i]) else math.nan
        c = float(close_series.iloc[i]) if pd.notna(close_series.iloc[i]) else math.nan
        v = float(vol_series.iloc[i]) if pd.notna(vol_series.iloc[i]) and math.isfinite(float(vol_series.iloc[i])) else 0.0

        # Skip pure NaN rows (e.g. non-trading placeholder rows from providers) without counting as corrupt spikes
        if any(math.isnan(x) for x in (o, rh, rl, c)):
            continue

        # Corrupt row checks: non-finite, non-positive price, or inverted raw High < raw Low
        if (
            any(not math.isfinite(x) or x <= 0.0 for x in (o, rh, rl, c))
            or rh < rl
        ):
            dropped_keys.append(b_key)
            continue

        oc_max = max(o, c)
        oc_min = min(o, c)
        repaired = False
        h = rh
        l_ = rl
        if rh < oc_max or rl > oc_min:
            denom = max(abs(c), abs(o), 1e-9)
            dev = max(0.0, oc_max - rh, rl - oc_min) / denom
            if dev > ENVELOPE_REPAIR_MAX_DEV:
                # Wild outlier (>12% outside [raw_low, raw_high]) — quarantine instead of inventing a huge fake wick
                dropped_keys.append(b_key)
                continue
            h = max(o, rh, rl, c)
            l_ = min(o, rh, rl, c)
            repaired = True
            if dev > max_repair_dev:
                max_repair_dev = dev

        raw_flat = (rh == rl)
        if intraday:
            end_dt = compute_intraday_bar_end_utc(ts, interval=interval, timestamp_anchor=timestamp_anchor)
        else:
            end_dt = compute_daily_bar_end_utc(b_key, asset_class=asset_class)
        end_iso = utc_iso(end_dt) if end_dt is not None else None
        if b_key in unclosed_overrides:
            is_closed = False
        else:
            is_closed = bool(fetch_dt >= end_dt) if end_dt is not None else (not intraday)

        kept_indices.append(idx_val)
        rows_out.append({
            "Open": o,
            "High": h,
            "Low": l_,
            "Close": c,
            "Volume": max(0.0, v),
            "_raw_high": rh,
            "_raw_low": rl,
            "_envelope_repaired": repaired,
            "_is_flat": raw_flat,
            "_bar_end_utc": end_iso,
            "_fetched_at_utc": fetch_iso,
            "_is_closed": is_closed,
            "_bar_key": b_key,
        })

    if not rows_out:
        empty_quality["dropped_corrupt_bars"] = len(dropped_keys)
        empty_quality["dropped_bar_keys"] = dropped_keys[-20:]
        return pd.DataFrame(columns=cols), empty_quality

    out_index = pd.DatetimeIndex(kept_indices)
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None and out_index.tz is None:
        out_index = out_index.tz_localize(df.index.tz)
    out_df = pd.DataFrame(rows_out, index=out_index)
    out_df.index.name = df.index.name or "Date"

    n_total = len(out_df)
    tail60 = out_df.iloc[-min(60, n_total):]
    tail250 = out_df.iloc[-min(250, n_total):]

    flat60 = int(tail60["_is_flat"].sum())
    flat_ratio_60 = round(flat60 / len(tail60), 4) if len(tail60) > 0 else 0.0
    flat250 = int(tail250["_is_flat"].sum())
    flat_ratio_250 = round(flat250 / len(tail250), 4) if len(tail250) > 0 else 0.0

    rep_total = int(out_df["_envelope_repaired"].sum())
    rep60 = int(tail60["_envelope_repaired"].sum())
    zvol60 = int((tail60["Volume"] <= 0.0).sum())

    last_key = str(out_df["_bar_key"].iloc[-1])
    last_closed = bool(out_df["_is_closed"].iloc[-1])

    # Staleness in calendar days
    stale_days: float | None = None
    if intraday:
        last_ts = pd.Timestamp(out_df.index[-1])
        last_utc = (last_ts.tz_localize("UTC") if last_ts.tzinfo is None else last_ts.tz_convert("UTC")).to_pydatetime()
        stale_days = round(max(0.0, (ref_now - last_utc).total_seconds() / 86400.0), 2)
    else:
        try:
            d_last = date.fromisoformat(last_key[:10])
            stale_days = float(max(0, (ref_now.date() - d_last).days))
        except ValueError:
            stale_days = None

    warnings: list[str] = []
    notes: list[str] = []
    grade = "good"

    if n_total < 15:
        grade = "degraded"
        warnings.append("INSUFFICIENT_BARS")
        notes.append(f"有效 K 线仅 {n_total} 根（不足 15 根缠论最低样本）")
    elif n_total >= 20:
        if flat_ratio_60 >= FLAT_DEGRADED_RATIO_60 or (len(tail250) >= 60 and flat_ratio_250 >= FLAT_DEGRADED_RATIO_250):
            grade = "degraded"
            warnings.append("HIGH_FLAT_BAR_RATIO")
            pct_txt = round(max(flat_ratio_60, flat_ratio_250) * 100, 1)
            notes.append(
                f"近 60 根 K 线中 {flat60}/{len(tail60)} 根（{pct_txt}%）为无波幅平K（疑似单一结算价/中间价），缠论分型与中枢可靠性严重降级"
            )
        elif flat_ratio_60 >= FLAT_CAUTION_RATIO_60:
            grade = "caution"
            warnings.append("MODERATE_FLAT_BAR_RATIO")
            notes.append(f"近 60 根 K 线含 {flat60} 根无波幅平K（{round(flat_ratio_60 * 100, 1)}%），注意低流动性滑点")

    # Dropped corrupt bars in recent window (including quarantined bars inherited from prior syncs)
    if inherited_dropped_keys:
        dropped_keys = sorted(set([*(str(k) for k in inherited_dropped_keys if k), *dropped_keys]))
    recent_cutoff_key = str(tail60["_bar_key"].iloc[0]) if len(tail60) > 0 else ""
    recent_dropped = [k for k in dropped_keys if k >= recent_cutoff_key]
    if len(recent_dropped) >= 3:
        grade = "degraded"
        warnings.append("CORRUPT_BARS_DROPPED")
        notes.append(f"近 60 根窗口隔离剔除 {len(recent_dropped)} 根异常坏点 K 线，存在时间槽缺口")
    elif len(recent_dropped) > 0:
        if grade == "good":
            grade = "caution"
        warnings.append("CORRUPT_BARS_DROPPED")
        notes.append(f"隔离剔除 {len(recent_dropped)} 根异常坏点 K 线")

    if rep_total > 0:
        warnings.append("OHLC_ENVELOPE_REPAIRED")
        if rep60 >= 3 and max_repair_dev >= 0.002 and grade == "good":
            grade = "caution"
        notes.append(
            f"已自动修正 {rep_total} 根 OHLC 包络倒挂 K 线（近60根 {rep60} 根，最大修正偏离 {round(max_repair_dev * 100, 2)}%；原始高低点已独立保留用于止损校验）"
        )

    max_stale_limit = 3.0 if (asset_class or "").lower() == "crypto" else 6.0
    if check_staleness and stale_days is not None and stale_days > max_stale_limit:
        grade = "degraded"
        warnings.append("STALE_MARKET_DATA")
        notes.append(f"最新 K 线停留在 {last_key}（距今 {stale_days} 天），数据陈旧")

    quality = {
        "quality_grade": grade,
        "actionable_allowed": grade != "degraded",
        "bars_total": n_total,
        "raw_flat_bars_60": flat60,
        "raw_flat_ratio_60": flat_ratio_60,
        "raw_flat_ratio_250": flat_ratio_250,
        "repaired_ohlc_bars_total": rep_total,
        "repaired_ohlc_bars_60": rep60,
        "max_repair_dev_pct": round(max_repair_dev * 100, 3),
        "dropped_corrupt_bars": len(dropped_keys),
        "dropped_bar_keys": dropped_keys[-20:],
        "zero_volume_bars_60": zvol60 if (asset_class or "").lower() not in ("fx", "macro") else None,
        "as_of_bar": last_key,
        "latest_bar_closed": last_closed,
        "fetched_at_utc": fetch_iso,
        "stale_days": stale_days,
        "warnings": warnings,
        "notes": notes,
    }
    return out_df, quality


# ---------------------------------------------------------------------------
# SQLite DataStore
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ohlcv_series (
    series_id TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    data_source TEXT NOT NULL,
    adjust_kind TEXT NOT NULL,
    tz_name TEXT NOT NULL DEFAULT '',
    timestamp_anchor TEXT NOT NULL DEFAULT 'start',
    generation INTEGER NOT NULL DEFAULT 1,
    bar_count INTEGER NOT NULL DEFAULT 0,
    earliest_bar_key TEXT,
    latest_bar_key TEXT,
    latest_closed_bar_key TEXT,
    covered_days INTEGER NOT NULL DEFAULT 0,
    last_synced_at_utc TEXT NOT NULL,
    last_full_sync_at_utc TEXT,
    quality_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_series_lookup
    ON ohlcv_series (asset_class, symbol, interval);

CREATE TABLE IF NOT EXISTS ohlcv_bars (
    series_id TEXT NOT NULL,
    bar_key TEXT NOT NULL,
    bar_ts_iso TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    raw_high REAL NOT NULL,
    raw_low REAL NOT NULL,
    envelope_repaired INTEGER NOT NULL DEFAULT 0,
    is_flat INTEGER NOT NULL DEFAULT 0,
    bar_end_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    is_closed INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (series_id, bar_key),
    FOREIGN KEY (series_id) REFERENCES ohlcv_series(series_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    created_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    universes_json TEXT NOT NULL DEFAULT '[]',
    symbols_json TEXT NOT NULL DEFAULT '[]',
    period TEXT NOT NULL DEFAULT '3y',
    total_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    degraded_count INTEGER NOT NULL DEFAULT 0,
    actionable_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_finished
    ON scan_runs (finished_at_utc DESC);

CREATE TABLE IF NOT EXISTS scan_rows (
    run_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    as_of_bar TEXT,
    bar_is_closed INTEGER,
    close REAL,
    ret_20d_pct REAL,
    signal_tier TEXT,
    signal_direction TEXT,
    signal_type TEXT,
    daily_structure TEXT,
    daily_zs_level TEXT,
    daily_zs_count INTEGER,
    zs_zd REAL,
    zs_zg REAL,
    quality_grade TEXT,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, row_index),
    FOREIGN KEY (run_id) REFERENCES scan_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scan_rows_symbol
    ON scan_rows (symbol, run_id DESC);

CREATE TABLE IF NOT EXISTS sentinel_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    created_at_utc TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    include_watchlist INTEGER NOT NULL DEFAULT 0,
    skip_research INTEGER NOT NULL DEFAULT 0,
    event_window_days INTEGER NOT NULL DEFAULT 14,
    holdings_total INTEGER NOT NULL DEFAULT 0,
    red_count INTEGER NOT NULL DEFAULT 0,
    yellow_count INTEGER NOT NULL DEFAULT 0,
    green_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sentinel_runs_created
    ON sentinel_runs (created_at_utc DESC);

CREATE INDEX IF NOT EXISTS idx_sentinel_runs_fp
    ON sentinel_runs (input_fingerprint, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS research_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_class TEXT NOT NULL,
    symbol TEXT NOT NULL,
    include_macro INTEGER NOT NULL DEFAULT 1,
    fetched_at_utc TEXT NOT NULL,
    all_sources_ok INTEGER NOT NULL DEFAULT 1,
    sources_status_json TEXT NOT NULL DEFAULT '{}',
    bundle_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_snapshots_lookup
    ON research_snapshots (asset_class, symbol, include_macro, id DESC);

CREATE TABLE IF NOT EXISTS macro_compass_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    created_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'claude',
    effort TEXT NOT NULL DEFAULT 'medium',
    focus_prompt TEXT NOT NULL DEFAULT '',
    regime_title TEXT NOT NULL DEFAULT '',
    directions_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_macro_compass_finished
    ON macro_compass_runs (finished_at_utc DESC, run_id DESC);
"""


class DeskDataStore:
    """Thread-safe and multi-process-safe SQLite warehouse for OHLCV, scans, sentinel & research snapshots."""

    _INIT_LOCK: ClassVar[threading.Lock] = threading.Lock()
    _INITIALIZED_PATHS: ClassVar[set[str]] = set()
    _FLIGHT_GUARD: ClassVar[threading.Lock] = threading.Lock()
    _FLIGHT_LOCKS: ClassVar[dict[str, threading.Lock]] = {}

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = resolve_db_path(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=10000;")
        with contextlib.suppress(OSError):
            os.chmod(self.db_path, 0o600)
        return conn

    def _ensure_schema(self) -> None:
        if self.db_path in self._INITIALIZED_PATHS and os.path.exists(self.db_path):
            return
        with self._INIT_LOCK:
            if self.db_path in self._INITIALIZED_PATHS and os.path.exists(self.db_path):
                return
            conn = self._connect()
            try:
                # executescript() manages its own SQLite DDL commit lifecycle under isolation_level=None
                conn.executescript(_SCHEMA_SQL)
                self._INITIALIZED_PATHS.add(self.db_path)
            finally:
                conn.close()

    @classmethod
    def _flight_lock(cls, key: str) -> threading.Lock:
        with cls._FLIGHT_GUARD:
            lk = cls._FLIGHT_LOCKS.get(key)
            if lk is None:
                lk = threading.Lock()
                cls._FLIGHT_LOCKS[key] = lk
            return lk

    # ------------------------------------------------------------------ OHLCV Series Read / Sync
    @staticmethod
    def _extract_tz_name(index: pd.Index) -> str:
        if isinstance(index, pd.DatetimeIndex) and index.tz is not None:
            tz = index.tz
            return getattr(tz, "key", None) or getattr(tz, "zone", None) or str(tz)
        return ""

    @staticmethod
    def _reconstruct_dataframe(rows: list[sqlite3.Row], tz_name: str, intraday: bool) -> pd.DataFrame:
        cols = [
            "Open", "High", "Low", "Close", "Volume",
            "_raw_high", "_raw_low", "_envelope_repaired", "_is_flat",
            "_bar_end_utc", "_fetched_at_utc", "_is_closed", "_bar_key",
        ]
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        idx_list: list[pd.Timestamp] = []
        records: list[dict[str, Any]] = []
        target_tz: Any = None
        if tz_name:
            try:
                target_tz = ZoneInfo(tz_name)
            except Exception:
                target_tz = tz_name

        for r in rows:
            if intraday:
                ts_utc = pd.Timestamp(r["bar_key"])
                if ts_utc.tzinfo is None:
                    ts_utc = ts_utc.tz_localize("UTC")
                ts = ts_utc.tz_convert(target_tz) if target_tz is not None else ts_utc
            else:
                if target_tz is not None and "T" in str(r["bar_ts_iso"]):
                    ts_parsed = pd.Timestamp(r["bar_ts_iso"])
                    ts = ts_parsed.tz_convert(target_tz) if ts_parsed.tzinfo is not None else ts_parsed.tz_localize(target_tz)
                elif target_tz is not None:
                    ts = pd.Timestamp(r["bar_key"]).tz_localize(target_tz)
                else:
                    ts = pd.Timestamp(r["bar_key"])
            idx_list.append(ts)
            records.append({
                "Open": float(r["open"]),
                "High": float(r["high"]),
                "Low": float(r["low"]),
                "Close": float(r["close"]),
                "Volume": float(r["volume"]),
                "_raw_high": float(r["raw_high"]),
                "_raw_low": float(r["raw_low"]),
                "_envelope_repaired": bool(r["envelope_repaired"]),
                "_is_flat": bool(r["is_flat"]),
                "_bar_end_utc": r["bar_end_utc"],
                "_fetched_at_utc": r["fetched_at_utc"],
                "_is_closed": bool(r["is_closed"]),
                "_bar_key": r["bar_key"],
            })

        df = pd.DataFrame(records, index=pd.DatetimeIndex(idx_list, name="Date"))
        return df[cols]

    @staticmethod
    def _slice_analysis_window(
        df: pd.DataFrame,
        *,
        interval: str,
        period: str,
        max_bars: int | None = None,
    ) -> pd.DataFrame:
        """Decouple storage retention from analysis window so Chanlun EMA & inclusion stay deterministic."""
        if df.empty:
            return df
        intraday = (interval or "1d").strip().lower() != "1d"
        out = df
        if intraday:
            if max_bars is not None and len(out) > max_bars:
                out = out.iloc[-max_bars:]
            elif max_bars is None:
                # Default 60d window matching standard yfinance intraday horizon
                last_ts = out.index[-1]
                cutoff = last_ts - pd.Timedelta(days=60)
                out = out[out.index >= cutoff]
        else:
            p = (period or "3y").strip().lower()
            if p != "max":
                last_ts = out.index[-1]
                if p == "ytd":
                    cutoff = pd.Timestamp(year=last_ts.year, month=1, day=1, tz=last_ts.tzinfo)
                else:
                    m = re.fullmatch(r"(\d+)\s*(d|w|wk|mo|y)", p)
                    if m:
                        qty, unit = int(m.group(1)), m.group(2)
                        if unit == "d":
                            cutoff = last_ts - pd.Timedelta(days=qty)
                        elif unit in ("w", "wk"):
                            cutoff = last_ts - pd.Timedelta(weeks=qty)
                        elif unit == "mo":
                            cutoff = last_ts - pd.DateOffset(months=qty)
                        else:
                            cutoff = last_ts - pd.DateOffset(years=qty)
                    else:
                        cutoff = last_ts - pd.DateOffset(years=3)
                out = out[out.index >= cutoff]
        return out.copy()

    def _is_series_fresh(
        self,
        meta: sqlite3.Row,
        *,
        requested_days: int,
        ttl_seconds: float,
        now: datetime,
    ) -> tuple[bool, str]:
        if int(meta["bar_count"] or 0) <= 0:
            return False, "empty"
        if int(meta["covered_days"] or 0) + 5 < requested_days:
            return False, "insufficient_lookback_coverage"
        synced_dt = parse_utc_iso(meta["last_synced_at_utc"])
        if synced_dt is None:
            return False, "missing_sync_ts"
        age = (now - synced_dt).total_seconds()
        if age >= ttl_seconds:
            return False, "ttl_expired"

        # Cross-boundary guard (Codex P0-2): if the latest stored bar was unclosed at fetch time
        # and wall-clock time has now passed its bar_end_utc, force re-sync immediately!
        conn = self._connect()
        try:
            last_row = conn.execute(
                "SELECT bar_end_utc, is_closed FROM ohlcv_bars WHERE series_id = ? ORDER BY bar_key DESC LIMIT 1",
                (meta["series_id"],),
            ).fetchone()
        finally:
            conn.close()
        if last_row is not None and not bool(last_row["is_closed"]):
            end_dt = parse_utc_iso(last_row["bar_end_utc"])
            if end_dt is not None and now >= end_dt:
                return False, "crossed_bar_close_boundary"

        # Intraday interval boundary crossing check
        iv_dur = parse_interval_timedelta(meta["interval"])
        if iv_dur is not None:
            dur_s = int(iv_dur.total_seconds())
            if dur_s > 0 and (int(now.timestamp()) // dur_s) > (int(synced_dt.timestamp()) // dur_s):
                return False, "crossed_intraday_slot_boundary"

        return True, "fresh_hit"

    @staticmethod
    def _detect_overlap_drift(
        stored_by_key: dict[str, sqlite3.Row],
        incoming_df: pd.DataFrame,
    ) -> tuple[bool, str | None]:
        """Compare closed bars in the overlap window to catch splits/dividends/re-splicing/missing bars."""
        if incoming_df.empty or not stored_by_key:
            return False, None

        inc_keys = [str(k) for k in incoming_df["_bar_key"].tolist()]
        first_inc = inc_keys[0]
        last_inc = inc_keys[-1]

        # Check if stored had closed bars inside [first_inc, last_inc) that disappeared in incoming
        inc_key_set = set(inc_keys)
        for s_key, s_row in stored_by_key.items():
            if first_inc <= s_key < last_inc and bool(s_row["is_closed"]) and s_key not in inc_key_set:
                return True, f"missing_closed_bar_in_overlap:{s_key}"

        # Compare closed bars (excluding the very last bar which may be an updating session bar)
        for idx in range(max(0, len(incoming_df) - 1)):
            b_key = inc_keys[idx]
            s_row = stored_by_key.get(b_key)
            if s_row is None or not bool(s_row["is_closed"]):
                continue
            for col, db_col in (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close")):
                new_v = float(incoming_df[col].iloc[idx])
                old_v = float(s_row[db_col])
                denom = max(abs(old_v), 1e-9)
                if abs(new_v - old_v) / denom > DRIFT_REL_TOLERANCE:
                    return True, f"price_drift_{db_col}_at_{b_key}"
        return False, None

    def get_series_generation(self, key: SeriesKey) -> int:
        """Read current persisted generation for ``key`` (0 if series does not exist yet)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT generation FROM ohlcv_series WHERE series_id = ?",
                (key.series_id,),
            ).fetchone()
            return int(row["generation"]) if row is not None else 0
        finally:
            conn.close()

    def get_or_sync_ohlcv(
        self,
        key: SeriesKey,
        *,
        period: str = "3y",
        timestamp_anchor: str = "start",
        ttl_seconds: float | None = None,
        refresh: bool = False,
        max_bars: int | None = None,
        supports_incremental: bool = False,
        fetch_full: Callable[[], pd.DataFrame],
        fetch_tail: Callable[[str], pd.DataFrame] | None = None,
        now_utc: datetime | None = None,
        fallback_unclosed_sources: tuple[str, ...] | list[str] | None = None,
        pre_fetch_generation: int | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
        """Return ``(sliced_df, quality_report, sync_meta)`` backed by ``data/desk.db``."""
        intraday = key.interval != "1d"
        eff_ttl = ttl_seconds if ttl_seconds is not None else (
            TTL_INTRADAY_FRESH_SECONDS if intraday else TTL_DAILY_FRESH_SECONDS
        )
        req_days = 60 if intraday else period_to_days(period)
        ref_now = now_utc or utc_now()

        flight_key = f"{self.db_path}::{key.series_id}"
        with self._flight_lock(flight_key):
            conn = self._connect()
            try:
                meta = conn.execute(
                    "SELECT * FROM ohlcv_series WHERE series_id = ?",
                    (key.series_id,),
                ).fetchone()
            finally:
                conn.close()

            if meta is not None and not refresh:
                fresh, reason = self._is_series_fresh(
                    meta, requested_days=req_days, ttl_seconds=eff_ttl, now=ref_now
                )
                if fresh:
                    df_all, qual = self._load_full_series(key.series_id)
                    if not df_all.empty:
                        sliced = self._slice_analysis_window(
                            df_all, interval=key.interval, period=period, max_bars=max_bars
                        )
                        first_sliced_key = str(sliced["_bar_key"].iloc[0])
                        prior_dropped = [
                            str(k)
                            for k in (qual.get("dropped_bar_keys") or [])
                            if str(k) >= first_sliced_key
                        ]
                        unclosed_keys = {
                            str(k)
                            for k, cl in zip(sliced["_bar_key"].tolist(), sliced["_is_closed"].tolist(), strict=True)
                            if not bool(cl)
                        }
                        _, sliced_qual = sanitize_and_grade_ohlcv(
                            sliced[["Open", "High", "Low", "Close", "Volume"]].assign(
                                High=sliced["_raw_high"], Low=sliced["_raw_low"]
                            ),
                            asset_class=key.asset_class,
                            interval=key.interval,
                            timestamp_anchor=timestamp_anchor,
                            fetched_at=parse_utc_iso(str(sliced["_fetched_at_utc"].iloc[-1])),
                            now_utc=ref_now,
                            inherited_dropped_keys=prior_dropped,
                            force_unclosed_bar_keys=unclosed_keys,
                        )
                        return sliced, sliced_qual, {
                            "cache_status": "hit",
                            "sync_mode": "sqlite_hit",
                            "reason": reason,
                            "series_id": key.series_id,
                            "generation": int(meta["generation"]),
                            "last_synced_at_utc": meta["last_synced_at_utc"],
                        }

            expected_gen = int(meta["generation"]) if meta is not None else 0
            prior_qual: dict[str, Any] = {}
            if meta is not None:
                with contextlib.suppress(ValueError):
                    prior_qual = json.loads(meta["quality_json"] or "{}")

            can_tail = (
                supports_incremental
                and fetch_tail is not None
                and meta is not None
                and int(meta["bar_count"] or 0) >= 20
                and int(meta["covered_days"] or 0) + 5 >= req_days
                and bool(meta["latest_bar_key"])
            )

            sync_mode = "full"
            rebuild_reason: str | None = None
            fetch_ts = utc_now()

            if can_tail and fetch_tail is not None and meta is not None:
                # Overlap window: step back 18 calendar days from latest stored daily bar
                try:
                    last_d = date.fromisoformat(str(meta["latest_bar_key"])[:10])
                    overlap_start = (last_d - timedelta(days=18)).strftime("%Y-%m-%d")
                except ValueError:
                    overlap_start = ""

                if overlap_start:
                    try:
                        raw_tail = fetch_tail(overlap_start)
                        clean_tail, tail_qual = sanitize_and_grade_ohlcv(
                            raw_tail,
                            asset_class=key.asset_class,
                            interval=key.interval,
                            timestamp_anchor=timestamp_anchor,
                            fetched_at=fetch_ts,
                            now_utc=ref_now,
                        )
                        if not clean_tail.empty:
                            stored_map = self._load_bar_map(key.series_id)
                            drifted, drift_why = self._detect_overlap_drift(stored_map, clean_tail)
                            if not drifted:
                                ok_cas = self._upsert_tail_bars(
                                    key,
                                    clean_tail,
                                    timestamp_anchor=timestamp_anchor,
                                    expected_generation=expected_gen,
                                    synced_at_iso=utc_iso(fetch_ts),
                                    intraday=intraday,
                                )
                                if ok_cas:
                                    sync_mode = "incremental_tail"
                                    tail_start_key = str(clean_tail["_bar_key"].iloc[0])
                                    merged_dropped = sorted(set([
                                        *(str(k) for k in (prior_qual.get("dropped_bar_keys") or []) if str(k) < tail_start_key),
                                        *(str(k) for k in (tail_qual.get("dropped_bar_keys") or [])),
                                    ]))
                                    df_all, _ = self._load_full_series(key.series_id)
                                    _, full_series_qual = sanitize_and_grade_ohlcv(
                                        df_all[["Open", "High", "Low", "Close", "Volume"]].assign(
                                            High=df_all["_raw_high"], Low=df_all["_raw_low"]
                                        ),
                                        asset_class=key.asset_class,
                                        interval=key.interval,
                                        timestamp_anchor=timestamp_anchor,
                                        fetched_at=fetch_ts,
                                        now_utc=ref_now,
                                        inherited_dropped_keys=merged_dropped,
                                    )
                                    self._update_series_quality(key.series_id, full_series_qual)
                                    sliced = self._slice_analysis_window(
                                        df_all, interval=key.interval, period=period, max_bars=max_bars
                                    )
                                    first_sliced_k = str(sliced["_bar_key"].iloc[0])
                                    _, sliced_qual = sanitize_and_grade_ohlcv(
                                        sliced[["Open", "High", "Low", "Close", "Volume"]].assign(
                                            High=sliced["_raw_high"], Low=sliced["_raw_low"]
                                        ),
                                        asset_class=key.asset_class,
                                        interval=key.interval,
                                        timestamp_anchor=timestamp_anchor,
                                        fetched_at=fetch_ts,
                                        now_utc=ref_now,
                                        inherited_dropped_keys=[k for k in merged_dropped if k >= first_sliced_k],
                                    )
                                    return sliced, sliced_qual, {
                                        "cache_status": "miss",
                                        "sync_mode": sync_mode,
                                        "series_id": key.series_id,
                                        "generation": expected_gen + 1,
                                        "last_synced_at_utc": utc_iso(fetch_ts),
                                    }
                            else:
                                rebuild_reason = drift_why
                    except Exception:
                        rebuild_reason = "tail_fetch_failed_fallback_full"

            # Full fetch path (outside any SQLite transaction!)
            fetch_ts = utc_now()
            raw_full = fetch_full()
            clean_full, qual_full = sanitize_and_grade_ohlcv(
                raw_full,
                asset_class=key.asset_class,
                interval=key.interval,
                timestamp_anchor=timestamp_anchor,
                fetched_at=fetch_ts,
                now_utc=ref_now,
            )
            if clean_full.empty:
                # Never wipe good stored history with an empty upstream glitch!
                return clean_full, qual_full, {
                    "cache_status": "miss",
                    "sync_mode": "empty_upstream",
                    "series_id": key.series_id,
                }

            if intraday:
                # For intraday rolling retention (up to 180d), check overlap drift before merging;
                # if corporate action / qfq drift occurred, replace cleanly with the new generation.
                new_gen = self._commit_intraday_window(
                    key,
                    clean_full,
                    qual_full,
                    timestamp_anchor=timestamp_anchor,
                    covered_days=req_days,
                    synced_at_iso=utc_iso(fetch_ts),
                )
                df_all, stored_q = self._load_full_series(key.series_id)
                sliced = self._slice_analysis_window(
                    df_all if not df_all.empty else clean_full,
                    interval=key.interval,
                    period=period,
                    max_bars=max_bars,
                )
                qual_out = qual_full
                if not sliced.empty and stored_q.get("dropped_bar_keys"):
                    first_k = str(sliced["_bar_key"].iloc[0])
                    _, qual_out = sanitize_and_grade_ohlcv(
                        sliced[["Open", "High", "Low", "Close", "Volume"]].assign(
                            High=sliced["_raw_high"], Low=sliced["_raw_low"]
                        ),
                        asset_class=key.asset_class,
                        interval=key.interval,
                        timestamp_anchor=timestamp_anchor,
                        fetched_at=fetch_ts,
                        now_utc=ref_now,
                        inherited_dropped_keys=[str(k) for k in stored_q["dropped_bar_keys"] if str(k) >= first_k],
                    )
            else:
                prev_cov = int(meta["covered_days"] or 0) if meta is not None else 0
                eff_expected_gen = pre_fetch_generation if pre_fetch_generation is not None else expected_gen
                rt_bar_err = (
                    raw_full.attrs.get("realtime_bar_error")
                    if isinstance(getattr(raw_full, "attrs", None), dict)
                    else None
                )
                # Never let a narrow short-period refresh (e.g. 1mo or 3y over 5y) delete wider stored history unless drifted!
                if meta is not None and req_days < prev_cov and rebuild_reason is None and not rt_bar_err:
                    stored_map = self._load_bar_map(key.series_id)
                    drifted, drift_why = self._detect_overlap_drift(stored_map, clean_full)
                    if not drifted and self._upsert_tail_bars(
                        key,
                        clean_full,
                        timestamp_anchor=timestamp_anchor,
                        expected_generation=eff_expected_gen,
                        synced_at_iso=utc_iso(fetch_ts),
                        intraday=False,
                    ):
                        first_new_k = str(clean_full["_bar_key"].iloc[0])
                        merged_drop = sorted(set([
                            *(str(k) for k in (prior_qual.get("dropped_bar_keys") or []) if str(k) < first_new_k),
                            *(str(k) for k in (qual_full.get("dropped_bar_keys") or [])),
                        ]))
                        df_all, _ = self._load_full_series(key.series_id)
                        unclosed_all = {
                            str(k)
                            for k, cl in zip(df_all["_bar_key"].tolist(), df_all["_is_closed"].tolist(), strict=True)
                            if not bool(cl)
                        }
                        _, full_q = sanitize_and_grade_ohlcv(
                            df_all[["Open", "High", "Low", "Close", "Volume"]].assign(
                                High=df_all["_raw_high"], Low=df_all["_raw_low"]
                            ),
                            asset_class=key.asset_class,
                            interval=key.interval,
                            timestamp_anchor=timestamp_anchor,
                            fetched_at=fetch_ts,
                            now_utc=ref_now,
                            inherited_dropped_keys=merged_drop,
                            force_unclosed_bar_keys=unclosed_all,
                        )
                        self._update_series_quality(key.series_id, full_q)
                        sliced = self._slice_analysis_window(
                            df_all, interval=key.interval, period=period, max_bars=max_bars
                        )
                        return sliced, qual_full, {
                            "cache_status": "miss",
                            "sync_mode": "short_window_merge",
                            "series_id": key.series_id,
                            "generation": eff_expected_gen + 1,
                            "last_synced_at_utc": utc_iso(fetch_ts),
                        }
                    rebuild_reason = drift_why

                # Full table replacement: record exact req_days (NEVER claim wider old covered_days!)
                new_gen, applied, preserved_unclosed = self._replace_full_series_with_status(
                    key,
                    clean_full,
                    qual_full,
                    timestamp_anchor=timestamp_anchor,
                    covered_days=req_days,
                    synced_at_iso=utc_iso(fetch_ts),
                    expected_generation=eff_expected_gen,
                    preserve_newer_unclosed=bool(rt_bar_err),
                    fallback_unclosed_sources=fallback_unclosed_sources,
                )
                if not applied or preserved_unclosed:
                    # Either an out-of-order stale upstream response was rejected by CAS/closed-bar guard,
                    # or a newer provisional unclosed bar was preserved because realtime quote failed.
                    # Read winning meta + bars in one atomic read transaction.
                    win_meta, df_all, stored_q = self._load_series_snapshot_atomic(key.series_id)
                    if win_meta is not None and not df_all.empty:
                        sliced = self._slice_analysis_window(
                            df_all, interval=key.interval, period=period, max_bars=max_bars
                        )
                        first_k = str(sliced["_bar_key"].iloc[0])
                        prior_dropped = [
                            str(k) for k in (stored_q.get("dropped_bar_keys") or []) if str(k) >= first_k
                        ]
                        unclosed_keys = {
                            str(k)
                            for k, cl in zip(sliced["_bar_key"].tolist(), sliced["_is_closed"].tolist(), strict=True)
                            if not bool(cl)
                        }
                        _, qual_out = sanitize_and_grade_ohlcv(
                            sliced[["Open", "High", "Low", "Close", "Volume"]].assign(
                                High=sliced["_raw_high"], Low=sliced["_raw_low"]
                            ),
                            asset_class=key.asset_class,
                            interval=key.interval,
                            timestamp_anchor=str(win_meta["timestamp_anchor"] or timestamp_anchor),
                            fetched_at=parse_utc_iso(str(sliced["_fetched_at_utc"].iloc[-1])),
                            now_utc=ref_now,
                            inherited_dropped_keys=prior_dropped,
                            force_unclosed_bar_keys=unclosed_keys,
                        )
                        if preserved_unclosed:
                            qual_out = dict(qual_out)
                            w_list = list(qual_out.get("warnings") or [])
                            n_list = list(qual_out.get("notes") or [])
                            if "PROVISIONAL_DAILY_BAR_STALE_QUOTE" not in w_list:
                                w_list.append("PROVISIONAL_DAILY_BAR_STALE_QUOTE")
                            n_list.append(
                                f"当日实时补齐失败（{rt_bar_err}），暂保留本地未收盘临时日K（{qual_out.get('as_of_bar')}），待收盘归档确认"
                            )
                            qual_out["warnings"] = w_list
                            qual_out["notes"] = n_list
                            if qual_out.get("quality_grade") == "good":
                                qual_out["quality_grade"] = "caution"
                        return sliced, qual_out, {
                            "cache_status": "stale_fallback" if preserved_unclosed else "hit",
                            "sync_mode": (
                                "provisional_unclosed_preserved"
                                if preserved_unclosed
                                else "stale_upstream_rejected"
                            ),
                            "series_id": key.series_id,
                            "generation": int(win_meta["generation"]),
                            "last_synced_at_utc": win_meta["last_synced_at_utc"],
                        }
                sliced = self._slice_analysis_window(
                    clean_full, interval=key.interval, period=period, max_bars=max_bars
                )
                qual_out = qual_full

            return sliced, qual_out, {
                "cache_status": "miss",
                "sync_mode": "full_rebuild" if rebuild_reason else "full_sync",
                "rebuild_reason": rebuild_reason,
                "series_id": key.series_id,
                "generation": new_gen,
                "last_synced_at_utc": utc_iso(fetch_ts),
            }

    def load_cached_ohlcv(
        self,
        key: SeriesKey,
        *,
        period: str = "3y",
        timestamp_anchor: str = "start",
        max_bars: int | None = None,
        now_utc: datetime | None = None,
        allowed_sources: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]] | None:
        """Read-only fallback loader for stale local SQLite bars when an upstream refresh fails."""
        ref_now = now_utc or utc_now()
        src_allow_list = [str(s) for s in allowed_sources if s] if allowed_sources is not None else None
        if allowed_sources is not None and not src_allow_list:
            return None
        conn = self._connect()
        try:
            conn.execute("BEGIN;")
            meta = None
            if src_allow_list is None or key.data_source in src_allow_list:
                meta = conn.execute(
                    "SELECT * FROM ohlcv_series WHERE series_id = ? AND bar_count > 0",
                    (key.series_id,),
                ).fetchone()
            if meta is None and src_allow_list:
                placeholders = ",".join("?" for _ in src_allow_list)
                meta = conn.execute(
                    f"""
                    SELECT * FROM ohlcv_series
                    WHERE asset_class = ? AND symbol = ? AND interval = ? AND adjust_kind = ?
                      AND data_source IN ({placeholders}) AND bar_count > 0
                    ORDER BY last_synced_at_utc DESC
                    LIMIT 1
                    """,
                    (key.asset_class, key.symbol, key.interval, key.adjust_kind, *src_allow_list),
                ).fetchone()
            if meta is None:
                conn.execute("COMMIT;")
                return None
            resolved_sid = str(meta["series_id"])
            rows = conn.execute(
                "SELECT * FROM ohlcv_bars WHERE series_id = ? ORDER BY bar_key ASC",
                (resolved_sid,),
            ).fetchall()
            conn.execute("COMMIT;")
        finally:
            conn.close()

        intraday = str(meta["interval"]) != "1d"
        df_all = self._reconstruct_dataframe(rows, str(meta["tz_name"] or ""), intraday=intraday)
        if df_all.empty:
            return None
        try:
            qual = json.loads(meta["quality_json"] or "{}")
        except ValueError:
            qual = {}

        sliced = self._slice_analysis_window(
            df_all, interval=key.interval, period=period, max_bars=max_bars
        )
        if sliced.empty:
            return None

        first_sliced_key = str(sliced["_bar_key"].iloc[0])
        prior_dropped = [
            str(k) for k in (qual.get("dropped_bar_keys") or []) if str(k) >= first_sliced_key
        ]
        unclosed_keys = {
            str(k)
            for k, cl in zip(sliced["_bar_key"].tolist(), sliced["_is_closed"].tolist(), strict=True)
            if not bool(cl)
        }
        eff_anchor = str(meta["timestamp_anchor"] or timestamp_anchor)
        _, sliced_qual = sanitize_and_grade_ohlcv(
            sliced[["Open", "High", "Low", "Close", "Volume"]].assign(
                High=sliced["_raw_high"], Low=sliced["_raw_low"]
            ),
            asset_class=key.asset_class,
            interval=key.interval,
            timestamp_anchor=eff_anchor,
            fetched_at=parse_utc_iso(str(sliced["_fetched_at_utc"].iloc[-1])),
            now_utc=ref_now,
            inherited_dropped_keys=prior_dropped,
            force_unclosed_bar_keys=unclosed_keys,
        )
        return sliced, sliced_qual, {
            "cache_status": "stale_fallback",
            "sync_mode": "sqlite_stale_fallback",
            "series_id": resolved_sid,
            "data_source": str(meta["data_source"]),
            "timestamp_anchor": eff_anchor,
            "generation": int(meta["generation"]),
            "last_synced_at_utc": meta["last_synced_at_utc"],
        }

    def _load_bar_map(self, series_id: str) -> dict[str, sqlite3.Row]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ohlcv_bars WHERE series_id = ? ORDER BY bar_key ASC",
                (series_id,),
            ).fetchall()
            return {str(r["bar_key"]): r for r in rows}
        finally:
            conn.close()

    def _load_series_snapshot_atomic(
        self, series_id: str
    ) -> tuple[sqlite3.Row | None, pd.DataFrame, dict[str, Any]]:
        conn = self._connect()
        try:
            conn.execute("BEGIN;")
            meta = conn.execute(
                "SELECT * FROM ohlcv_series WHERE series_id = ?",
                (series_id,),
            ).fetchone()
            if meta is None:
                conn.execute("COMMIT;")
                return None, pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), {}
            rows = conn.execute(
                "SELECT * FROM ohlcv_bars WHERE series_id = ? ORDER BY bar_key ASC",
                (series_id,),
            ).fetchall()
            conn.execute("COMMIT;")
        finally:
            conn.close()
        intraday = str(meta["interval"]) != "1d"
        df = self._reconstruct_dataframe(rows, str(meta["tz_name"] or ""), intraday=intraday)
        try:
            qual = json.loads(meta["quality_json"] or "{}")
        except ValueError:
            qual = {}
        return meta, df, qual

    def _load_full_series(self, series_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        _, df, qual = self._load_series_snapshot_atomic(series_id)
        return df, qual

    def _update_series_quality(self, series_id: str, quality: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE ohlcv_series SET quality_json = ? WHERE series_id = ?",
                (json.dumps(quality, ensure_ascii=False), series_id),
            )
        finally:
            conn.close()

    @staticmethod
    def _df_to_bar_tuples(series_id: str, df: pd.DataFrame, intraday: bool) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        for i, idx_val in enumerate(df.index):
            b_key, b_iso, _ = _bar_key_for_index(idx_val, intraday=intraday)
            if b_key is None or b_iso is None:
                continue
            out.append((
                series_id,
                str(df["_bar_key"].iloc[i]),
                b_iso,
                float(df["Open"].iloc[i]),
                float(df["High"].iloc[i]),
                float(df["Low"].iloc[i]),
                float(df["Close"].iloc[i]),
                float(df["Volume"].iloc[i]),
                float(df["_raw_high"].iloc[i]),
                float(df["_raw_low"].iloc[i]),
                1 if bool(df["_envelope_repaired"].iloc[i]) else 0,
                1 if bool(df["_is_flat"].iloc[i]) else 0,
                df["_bar_end_utc"].iloc[i],
                str(df["_fetched_at_utc"].iloc[i]),
                1 if bool(df["_is_closed"].iloc[i]) else 0,
            ))
        return out

    def _upsert_tail_bars(
        self,
        key: SeriesKey,
        tail_df: pd.DataFrame,
        *,
        timestamp_anchor: str,
        expected_generation: int,
        synced_at_iso: str,
        intraday: bool,
    ) -> bool:
        bar_tuples = self._df_to_bar_tuples(key.series_id, tail_df, intraday=intraday)
        if not bar_tuples:
            return False
        tz_name = self._extract_tz_name(tail_df.index)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur_row = conn.execute(
                "SELECT generation FROM ohlcv_series WHERE series_id = ?",
                (key.series_id,),
            ).fetchone()
            if cur_row is None or int(cur_row["generation"]) != expected_generation:
                conn.execute("ROLLBACK;")
                return False

            conn.executemany(
                """
                INSERT INTO ohlcv_bars (
                    series_id, bar_key, bar_ts_iso, open, high, low, close, volume,
                    raw_high, raw_low, envelope_repaired, is_flat, bar_end_utc, fetched_at_utc, is_closed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id, bar_key) DO UPDATE SET
                    bar_ts_iso = excluded.bar_ts_iso,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    raw_high = excluded.raw_high,
                    raw_low = excluded.raw_low,
                    envelope_repaired = excluded.envelope_repaired,
                    is_flat = excluded.is_flat,
                    bar_end_utc = excluded.bar_end_utc,
                    fetched_at_utc = excluded.fetched_at_utc,
                    is_closed = excluded.is_closed
                """,
                bar_tuples,
            )
            stats = conn.execute(
                """
                SELECT COUNT(*) AS cnt, MIN(bar_key) AS min_k, MAX(bar_key) AS max_k,
                       MAX(CASE WHEN is_closed = 1 THEN bar_key ELSE NULL END) AS max_closed_k
                FROM ohlcv_bars WHERE series_id = ?
                """,
                (key.series_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE ohlcv_series
                SET generation = ?, bar_count = ?, earliest_bar_key = ?, latest_bar_key = ?,
                    latest_closed_bar_key = ?, last_synced_at_utc = ?,
                    tz_name = CASE WHEN ? != '' THEN ? ELSE tz_name END,
                    timestamp_anchor = ?
                WHERE series_id = ?
                """,
                (
                    expected_generation + 1,
                    int(stats["cnt"] or 0),
                    stats["min_k"],
                    stats["max_k"],
                    stats["max_closed_k"],
                    synced_at_iso,
                    tz_name,
                    tz_name,
                    timestamp_anchor,
                    key.series_id,
                ),
            )
            conn.execute("COMMIT;")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def _replace_full_series_with_status(
        self,
        key: SeriesKey,
        full_df: pd.DataFrame,
        quality: dict[str, Any],
        *,
        timestamp_anchor: str,
        covered_days: int,
        synced_at_iso: str,
        expected_generation: int | None = None,
        preserve_newer_unclosed: bool = False,
        fallback_unclosed_sources: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[int, bool, bool]:
        """Atomically replace a series and return ``(generation, applied, preserved_newer_unclosed)``."""
        intraday = key.interval != "1d"
        bar_tuples = self._df_to_bar_tuples(key.series_id, full_df, intraday=intraday)
        tz_name = self._extract_tz_name(full_df.index)
        closed_keys = [t[1] for t in bar_tuples if t[14] == 1]
        new_max_key = str(bar_tuples[-1][1]) if bar_tuples else None

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                "SELECT generation, latest_bar_key, latest_closed_bar_key FROM ohlcv_series WHERE series_id = ?",
                (key.series_id,),
            ).fetchone()
            next_gen = (int(cur["generation"]) + 1) if cur is not None else 1

            # Prevent an out-of-order stale response from overwriting newer closed bars
            new_max_closed = closed_keys[-1] if closed_keys else None
            if (
                cur is not None
                and cur["latest_closed_bar_key"]
                and new_max_closed
                and str(new_max_closed) < str(cur["latest_closed_bar_key"])
            ):
                conn.execute("ROLLBACK;")
                return int(cur["generation"]), False, False

            # Also prevent a racing stale fetch (older generation) from wiping either a newer
            # unclosed/provisional bar OR a fresher same-day quote committed by a winning concurrent writer,
            # unless this write strictly advances latest_closed_bar_key.
            advances_closed = bool(
                new_max_closed
                and (not cur["latest_closed_bar_key"] or str(new_max_closed) > str(cur["latest_closed_bar_key"]))
            ) if cur is not None else False
            if (
                cur is not None
                and expected_generation is not None
                and int(cur["generation"]) != expected_generation
                and not advances_closed
                and cur["latest_bar_key"]
                and new_max_key
                and str(new_max_key) <= str(cur["latest_bar_key"])
            ):
                conn.execute("ROLLBACK;")
                return int(cur["generation"]), False, False

            preserved_rows: list[tuple[Any, ...]] = []
            if preserve_newer_unclosed and new_max_key is not None:
                # Scan BOTH current series_id and all declared fallback_unclosed_sources so a newer
                # provisional bar (e.g. 11-26 in primary) is never shadowed by an older 11-25 bar in fallback.
                candidate_sources: list[str] = [key.data_source]
                for s in (fallback_unclosed_sources or ()):
                    s_str = str(s)
                    if s_str and s_str not in candidate_sources:
                        candidate_sources.append(s_str)
                placeholders = ",".join("?" for _ in candidate_sources)
                cand_rows = conn.execute(
                    f"""
                    SELECT b.bar_key, b.bar_ts_iso, b.open, b.high, b.low, b.close, b.volume,
                           b.raw_high, b.raw_low, b.envelope_repaired, b.is_flat,
                           b.bar_end_utc, b.fetched_at_utc, b.is_closed
                    FROM ohlcv_bars AS b
                    INNER JOIN ohlcv_series AS s ON s.series_id = b.series_id
                    WHERE s.asset_class = ? AND s.symbol = ? AND s.interval = ? AND s.adjust_kind = ?
                      AND s.data_source IN ({placeholders})
                      AND b.is_closed = 0 AND b.bar_key > ?
                    ORDER BY b.bar_key ASC, b.fetched_at_utc DESC, s.last_synced_at_utc DESC
                    """,
                    (key.asset_class, key.symbol, key.interval, key.adjust_kind, *candidate_sources, new_max_key),
                ).fetchall()
                by_bar_key: dict[str, tuple[Any, ...]] = {}
                for r in cand_rows:
                    bk = str(r["bar_key"])
                    if bk not in by_bar_key:
                        by_bar_key[bk] = (key.series_id, *tuple(r))
                preserved_rows = [by_bar_key[k] for k in sorted(by_bar_key)]

            final_tuples = [*bar_tuples, *preserved_rows]
            preserved_flag = bool(preserved_rows)

            conn.execute(
                """
                INSERT INTO ohlcv_series (
                    series_id, asset_class, symbol, interval, data_source, adjust_kind,
                    tz_name, timestamp_anchor, generation, bar_count,
                    earliest_bar_key, latest_bar_key, latest_closed_bar_key,
                    covered_days, last_synced_at_utc, last_full_sync_at_utc, quality_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    tz_name = excluded.tz_name,
                    timestamp_anchor = excluded.timestamp_anchor,
                    generation = excluded.generation,
                    bar_count = excluded.bar_count,
                    earliest_bar_key = excluded.earliest_bar_key,
                    latest_bar_key = excluded.latest_bar_key,
                    latest_closed_bar_key = excluded.latest_closed_bar_key,
                    covered_days = excluded.covered_days,
                    last_synced_at_utc = excluded.last_synced_at_utc,
                    last_full_sync_at_utc = excluded.last_full_sync_at_utc,
                    quality_json = excluded.quality_json
                """,
                (
                    key.series_id,
                    key.asset_class,
                    key.symbol,
                    key.interval,
                    key.data_source,
                    key.adjust_kind,
                    tz_name,
                    timestamp_anchor,
                    next_gen,
                    len(final_tuples),
                    final_tuples[0][1] if final_tuples else None,
                    final_tuples[-1][1] if final_tuples else None,
                    new_max_closed,
                    covered_days,
                    synced_at_iso,
                    synced_at_iso,
                    json.dumps(quality, ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM ohlcv_bars WHERE series_id = ?", (key.series_id,))
            conn.executemany(
                """
                INSERT INTO ohlcv_bars (
                    series_id, bar_key, bar_ts_iso, open, high, low, close, volume,
                    raw_high, raw_low, envelope_repaired, is_flat, bar_end_utc, fetched_at_utc, is_closed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                final_tuples,
            )
            conn.execute("COMMIT;")
            return next_gen, True, preserved_flag
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def _replace_full_series(
        self,
        key: SeriesKey,
        full_df: pd.DataFrame,
        quality: dict[str, Any],
        *,
        timestamp_anchor: str,
        covered_days: int,
        synced_at_iso: str,
    ) -> int:
        gen, _, _ = self._replace_full_series_with_status(
            key,
            full_df,
            quality,
            timestamp_anchor=timestamp_anchor,
            covered_days=covered_days,
            synced_at_iso=synced_at_iso,
        )
        return gen

    def _commit_intraday_window(
        self,
        key: SeriesKey,
        inc_df: pd.DataFrame,
        quality: dict[str, Any],
        *,
        timestamp_anchor: str,
        covered_days: int,
        synced_at_iso: str,
    ) -> int:
        """Merge intraday window into rolling 180d history, or replace cleanly if qfq/adjustment drifted."""
        stored_map = self._load_bar_map(key.series_id)
        drifted, _ = self._detect_overlap_drift(stored_map, inc_df)
        if drifted or not stored_map:
            return self._replace_full_series(
                key,
                inc_df,
                quality,
                timestamp_anchor=timestamp_anchor,
                covered_days=covered_days,
                synced_at_iso=synced_at_iso,
            )

        bar_tuples = self._df_to_bar_tuples(key.series_id, inc_df, intraday=True)
        tz_name = self._extract_tz_name(inc_df.index)
        cutoff_iso = (utc_now() - timedelta(days=MAX_INTRADAY_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                "SELECT generation FROM ohlcv_series WHERE series_id = ?",
                (key.series_id,),
            ).fetchone()
            next_gen = (int(cur["generation"]) + 1) if cur is not None else 1
            conn.executemany(
                """
                INSERT INTO ohlcv_bars (
                    series_id, bar_key, bar_ts_iso, open, high, low, close, volume,
                    raw_high, raw_low, envelope_repaired, is_flat, bar_end_utc, fetched_at_utc, is_closed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id, bar_key) DO UPDATE SET
                    bar_ts_iso = excluded.bar_ts_iso,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    raw_high = excluded.raw_high,
                    raw_low = excluded.raw_low,
                    envelope_repaired = excluded.envelope_repaired,
                    is_flat = excluded.is_flat,
                    bar_end_utc = excluded.bar_end_utc,
                    fetched_at_utc = excluded.fetched_at_utc,
                    is_closed = excluded.is_closed
                """,
                bar_tuples,
            )
            conn.execute(
                "DELETE FROM ohlcv_bars WHERE series_id = ? AND bar_key < ?",
                (key.series_id, cutoff_iso),
            )
            stats = conn.execute(
                """
                SELECT COUNT(*) AS cnt, MIN(bar_key) AS min_k, MAX(bar_key) AS max_k,
                       MAX(CASE WHEN is_closed = 1 THEN bar_key ELSE NULL END) AS max_closed_k
                FROM ohlcv_bars WHERE series_id = ?
                """,
                (key.series_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE ohlcv_series
                SET generation = ?, bar_count = ?, earliest_bar_key = ?, latest_bar_key = ?,
                    latest_closed_bar_key = ?, covered_days = MAX(covered_days, ?),
                    last_synced_at_utc = ?, tz_name = CASE WHEN ? != '' THEN ? ELSE tz_name END,
                    timestamp_anchor = ?, quality_json = ?
                WHERE series_id = ?
                """,
                (
                    next_gen,
                    int(stats["cnt"] or 0),
                    stats["min_k"],
                    stats["max_k"],
                    stats["max_closed_k"],
                    covered_days,
                    synced_at_iso,
                    tz_name,
                    tz_name,
                    timestamp_anchor,
                    json.dumps(quality, ensure_ascii=False),
                    key.series_id,
                ),
            )
            conn.execute("COMMIT;")
            return next_gen
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ Scan History
    def record_scan_run(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        created_at_utc: str | None = None,
        finished_at_utc: str | None = None,
        status: str = "completed",
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
        period: str = "3y",
        rows: list[dict[str, Any]],
        compass_context: dict[str, Any] | None = None,
    ) -> str:
        rid = run_id or task_id or uuid.uuid4().hex[:12]
        now_s = utc_iso()
        c_at = created_at_utc or now_s
        f_at = finished_at_utc or now_s

        err_cnt = sum(1 for r in rows if "error" in r)
        deg_cnt = sum(1 for r in rows if r.get("quality_grade") == "degraded")
        act_cnt = sum(1 for r in rows if r.get("signal_tier") == "买卖点信号")

        row_tuples: list[tuple[Any, ...]] = []
        for idx, r in enumerate(rows):
            zs = r.get("daily_zs")
            zd = float(zs[0]) if isinstance(zs, list) and len(zs) == 2 and zs[0] is not None else None
            zg = float(zs[1]) if isinstance(zs, list) and len(zs) == 2 and zs[1] is not None else None
            b_closed = r.get("bar_is_closed")
            row_tuples.append((
                rid,
                idx,
                str(r.get("market") or ""),
                str(r.get("symbol") or ""),
                r.get("asset_class"),
                r.get("as_of_bar"),
                None if b_closed is None else (1 if b_closed else 0),
                r.get("close"),
                r.get("ret_20d_%"),
                r.get("signal_tier"),
                r.get("signal_direction"),
                r.get("signal_type"),
                r.get("daily_structure"),
                r.get("daily_zs_level"),
                r.get("daily_zs_count"),
                zd,
                zg,
                r.get("quality_grade"),
                json.dumps(r, ensure_ascii=False),
            ))

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO scan_runs (
                    run_id, task_id, created_at_utc, finished_at_utc, status,
                    universes_json, symbols_json, period, total_count,
                    error_count, degraded_count, actionable_count, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at_utc = excluded.finished_at_utc,
                    status = excluded.status,
                    total_count = excluded.total_count,
                    error_count = excluded.error_count,
                    degraded_count = excluded.degraded_count,
                    actionable_count = excluded.actionable_count,
                    summary_json = excluded.summary_json
                """,
                (
                    rid,
                    task_id,
                    c_at,
                    f_at,
                    status,
                    json.dumps(universes or [], ensure_ascii=False),
                    json.dumps(symbols or [], ensure_ascii=False),
                    period,
                    len(rows),
                    err_cnt,
                    deg_cnt,
                    act_cnt,
                    json.dumps(
                        {
                            "count": len(rows),
                            "errors": err_cnt,
                            "degraded": deg_cnt,
                            "actionable": act_cnt,
                            **({"compass_context": compass_context} if compass_context else {}),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute("DELETE FROM scan_rows WHERE run_id = ?", (rid,))
            if row_tuples:
                conn.executemany(
                    """
                    INSERT INTO scan_rows (
                        run_id, row_index, market, symbol, asset_class, as_of_bar, bar_is_closed,
                        close, ret_20d_pct, signal_tier, signal_direction, signal_type,
                        daily_structure, daily_zs_level, daily_zs_count, zs_zd, zs_zg,
                        quality_grade, row_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row_tuples,
                )
            # Bounded retention pruning
            conn.execute(
                """
                DELETE FROM scan_runs WHERE run_id NOT IN (
                    SELECT run_id FROM scan_runs ORDER BY finished_at_utc DESC, run_id DESC LIMIT ?
                )
                """,
                (MAX_SCAN_RUNS_RETENTION,),
            )
            conn.execute("COMMIT;")
            return rid
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def list_scan_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scan_runs ORDER BY finished_at_utc DESC, run_id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                summary_obj = json.loads(r["summary_json"] or "{}")
            except ValueError:
                summary_obj = {}
            out.append({
                "run_id": r["run_id"],
                "task_id": r["task_id"],
                "created_at_utc": r["created_at_utc"],
                "finished_at_utc": r["finished_at_utc"],
                "status": r["status"],
                "universes": json.loads(r["universes_json"] or "[]"),
                "symbols": json.loads(r["symbols_json"] or "[]"),
                "period": r["period"],
                "total_count": int(r["total_count"]),
                "error_count": int(r["error_count"]),
                "degraded_count": int(r["degraded_count"]),
                "actionable_count": int(r["actionable_count"]),
                "compass_context": summary_obj.get("compass_context") if isinstance(summary_obj, dict) else None,
            })
        return out

    def get_scan_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            meta = conn.execute("SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)).fetchone()
            if meta is None:
                return None
            r_rows = conn.execute(
                "SELECT row_json FROM scan_rows WHERE run_id = ? ORDER BY row_index ASC",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        try:
            summary_obj = json.loads(meta["summary_json"] or "{}")
        except ValueError:
            summary_obj = {}
        return {
            "run_id": meta["run_id"],
            "task_id": meta["task_id"],
            "created_at_utc": meta["created_at_utc"],
            "finished_at_utc": meta["finished_at_utc"],
            "status": meta["status"],
            "universes": json.loads(meta["universes_json"] or "[]"),
            "compass_context": summary_obj.get("compass_context") if isinstance(summary_obj, dict) else None,
            "symbols": json.loads(meta["symbols_json"] or "[]"),
            "period": meta["period"],
            "total_count": int(meta["total_count"]),
            "error_count": int(meta["error_count"]),
            "degraded_count": int(meta["degraded_count"]),
            "actionable_count": int(meta["actionable_count"]),
            "rows": [json.loads(x["row_json"]) for x in r_rows],
        }

    # ------------------------------------------------------------------ Sentinel History & Fingerprint
    @staticmethod
    def compute_sentinel_fingerprint(
        *,
        portfolio_dir: str,
        research_root: str,
        include_watchlist: bool,
        skip_research: bool,
        event_window_days: int,
        ref_date: str | None = None,
    ) -> str:
        """Deterministic SHA-256 fingerprint over portfolio state + research archive revisions + query params."""
        h = hashlib.sha256()
        d_str = ref_date or utc_now().strftime("%Y-%m-%d")
        h.update(f"v1|{d_str}|{int(bool(include_watchlist))}|{int(bool(skip_research))}|{int(event_window_days)}\n".encode())

        for fname in ("account.json", "positions.json"):
            fpath = os.path.join(portfolio_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "rb") as f:
                        h.update(hashlib.sha256(f.read()).digest())
                except OSError:
                    h.update(b"ERR")
            else:
                h.update(b"MISSING")

        if os.path.isdir(research_root):
            state_entries: list[str] = []
            for root_p, _, files in os.walk(research_root):
                if "state.json" in files:
                    sp = os.path.join(root_p, "state.json")
                    try:
                        st = os.stat(sp)
                        rel = os.path.relpath(sp, research_root)
                        state_entries.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
                    except OSError:
                        pass
            for item in sorted(state_entries):
                h.update(item.encode("utf-8") + b"\n")
        return h.hexdigest()

    def record_sentinel_run(
        self,
        result: dict[str, Any],
        *,
        input_fingerprint: str,
        include_watchlist: bool,
        skip_research: bool,
        event_window_days: int,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or task_id or uuid.uuid4().hex[:12]
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO sentinel_runs (
                    run_id, task_id, created_at_utc, input_fingerprint,
                    include_watchlist, skip_research, event_window_days,
                    holdings_total, red_count, yellow_count, green_count, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    result_json = excluded.result_json
                """,
                (
                    rid,
                    task_id,
                    utc_iso(),
                    input_fingerprint,
                    1 if include_watchlist else 0,
                    1 if skip_research else 0,
                    int(event_window_days),
                    int(summary.get("holdings_total") or 0),
                    int(summary.get("red_exit_count") or 0),
                    int(summary.get("yellow_loosening_count") or 0),
                    int(summary.get("green_hold_count") or 0),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                DELETE FROM sentinel_runs WHERE run_id NOT IN (
                    SELECT run_id FROM sentinel_runs ORDER BY created_at_utc DESC, run_id DESC LIMIT ?
                )
                """,
                (MAX_SENTINEL_RUNS_RETENTION,),
            )
            conn.execute("COMMIT;")
            return rid
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def get_fresh_sentinel_run(
        self,
        *,
        input_fingerprint: str,
        max_age_seconds: float,
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT created_at_utc, result_json FROM sentinel_runs
                WHERE input_fingerprint = ?
                ORDER BY created_at_utc DESC LIMIT 1
                """,
                (input_fingerprint,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        created_dt = parse_utc_iso(row["created_at_utc"])
        if created_dt is None or (utc_now() - created_dt).total_seconds() >= max_age_seconds:
            return None
        try:
            return json.loads(row["result_json"])
        except ValueError:
            return None

    def list_sentinel_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT run_id, task_id, created_at_utc, include_watchlist, skip_research,
                       event_window_days, holdings_total, red_count, yellow_count, green_count
                FROM sentinel_runs
                ORDER BY created_at_utc DESC, run_id DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_sentinel_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM sentinel_runs WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "created_at_utc": row["created_at_utc"],
            "include_watchlist": bool(row["include_watchlist"]),
            "skip_research": bool(row["skip_research"]),
            "event_window_days": int(row["event_window_days"]),
            "result": json.loads(row["result_json"]),
        }

    # ------------------------------------------------------------------ Research Bundle Snapshots
    def get_fresh_research_snapshot(
        self,
        *,
        asset_class: str,
        symbol: str,
        include_macro: bool,
        max_age_seconds: float = TTL_RESEARCH_FRESH_SECONDS,
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT fetched_at_utc, all_sources_ok, bundle_json
                FROM research_snapshots
                WHERE asset_class = ? AND symbol = ? AND include_macro = ?
                ORDER BY id DESC LIMIT 1
                """,
                (asset_class, symbol, 1 if include_macro else 0),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        fetched_dt = parse_utc_iso(row["fetched_at_utc"])
        if fetched_dt is None:
            return None
        eff_ttl = max_age_seconds if bool(row["all_sources_ok"]) else min(max_age_seconds, TTL_RESEARCH_ERROR_SECONDS)
        if (utc_now() - fetched_dt).total_seconds() >= eff_ttl:
            return None
        try:
            bundle = json.loads(row["bundle_json"])
            if isinstance(bundle, dict):
                bundle.setdefault("cache_meta", {
                    "source": "sqlite_store",
                    "fetched_at_utc": row["fetched_at_utc"],
                    "all_sources_ok": bool(row["all_sources_ok"]),
                })
            return bundle
        except ValueError:
            return None

    def record_research_snapshot(
        self,
        *,
        asset_class: str,
        symbol: str,
        include_macro: bool,
        bundle: dict[str, Any],
    ) -> None:
        sources_status = bundle.get("sources_status") if isinstance(bundle.get("sources_status"), dict) else {}
        has_err = any(
            isinstance(v, dict) and str(v.get("status") or "").lower() in ("error", "stale")
            for v in sources_status.values()
        )
        all_ok = not has_err
        inc_int = 1 if include_macro else 0

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO research_snapshots (
                    asset_class, symbol, include_macro, fetched_at_utc,
                    all_sources_ok, sources_status_json, bundle_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_class,
                    symbol,
                    inc_int,
                    utc_iso(),
                    1 if all_ok else 0,
                    json.dumps(sources_status, ensure_ascii=False),
                    json.dumps(bundle, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                DELETE FROM research_snapshots
                WHERE asset_class = ? AND symbol = ? AND include_macro = ?
                  AND id NOT IN (
                      SELECT id FROM research_snapshots
                      WHERE asset_class = ? AND symbol = ? AND include_macro = ?
                      ORDER BY id DESC LIMIT ?
                  )
                """,
                (
                    asset_class, symbol, inc_int,
                    asset_class, symbol, inc_int,
                    MAX_RESEARCH_SNAPSHOTS_PER_KEY,
                ),
            )
            conn.execute("COMMIT;")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ Layer 1 Macro Compass Runs
    def record_compass_run(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        created_at_utc: str | None = None,
        finished_at_utc: str | None = None,
        status: str = "completed",
        engine: str = "claude",
        effort: str = "medium",
        focus_prompt: str = "",
        result: dict[str, Any],
    ) -> str:
        rid = run_id or task_id or uuid.uuid4().hex[:12]
        now_s = utc_iso()
        c_at = created_at_utc or now_s
        f_at = finished_at_utc or now_s
        regime_title = str(result.get("regime_title") or "")
        dirs = result.get("directions")
        dir_count = len(dirs) if isinstance(dirs, list) else 0

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO macro_compass_runs (
                    run_id, task_id, created_at_utc, finished_at_utc, status,
                    engine, effort, focus_prompt, regime_title, directions_count, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at_utc = excluded.finished_at_utc,
                    status = excluded.status,
                    engine = excluded.engine,
                    effort = excluded.effort,
                    focus_prompt = excluded.focus_prompt,
                    regime_title = excluded.regime_title,
                    directions_count = excluded.directions_count,
                    result_json = excluded.result_json
                """,
                (
                    rid,
                    task_id,
                    c_at,
                    f_at,
                    status,
                    engine,
                    effort,
                    focus_prompt,
                    regime_title,
                    dir_count,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                DELETE FROM macro_compass_runs WHERE run_id NOT IN (
                    SELECT run_id FROM macro_compass_runs ORDER BY finished_at_utc DESC, run_id DESC LIMIT ?
                )
                """,
                (MAX_COMPASS_RUNS_RETENTION,),
            )
            conn.execute("COMMIT;")
            return rid
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def list_compass_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT run_id, task_id, created_at_utc, finished_at_utc, status,
                       engine, effort, focus_prompt, regime_title, directions_count
                FROM macro_compass_runs
                ORDER BY finished_at_utc DESC, run_id DESC LIMIT ?
                """,
                (max(1, min(limit, 60)),),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_compass_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM macro_compass_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "created_at_utc": row["created_at_utc"],
            "finished_at_utc": row["finished_at_utc"],
            "status": row["status"],
            "engine": row["engine"],
            "effort": row["effort"],
            "focus_prompt": row["focus_prompt"],
            "regime_title": row["regime_title"],
            "directions_count": int(row["directions_count"]),
            "result": json.loads(row["result_json"]),
        }

    def get_latest_compass_run(self) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM macro_compass_runs
                WHERE status = 'completed'
                ORDER BY finished_at_utc DESC, run_id DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "created_at_utc": row["created_at_utc"],
            "finished_at_utc": row["finished_at_utc"],
            "status": row["status"],
            "engine": row["engine"],
            "effort": row["effort"],
            "focus_prompt": row["focus_prompt"],
            "regime_title": row["regime_title"],
            "directions_count": int(row["directions_count"]),
            "result": json.loads(row["result_json"]),
        }

    # ------------------------------------------------------------------ Warehouse Overview & Diagnostics
    def warehouse_summary(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            series_rows = conn.execute(
                """
                SELECT series_id, asset_class, symbol, interval, data_source,
                       bar_count, earliest_bar_key, latest_bar_key, latest_closed_bar_key,
                       last_synced_at_utc, quality_json
                FROM ohlcv_series
                ORDER BY last_synced_at_utc DESC
                """
            ).fetchall()
            total_bars = int(
                (conn.execute("SELECT COUNT(*) AS c FROM ohlcv_bars").fetchone() or {"c": 0})["c"]
            )
            scan_cnt = int(
                (conn.execute("SELECT COUNT(*) AS c FROM scan_runs").fetchone() or {"c": 0})["c"]
            )
            sentinel_cnt = int(
                (conn.execute("SELECT COUNT(*) AS c FROM sentinel_runs").fetchone() or {"c": 0})["c"]
            )
            research_cnt = int(
                (conn.execute("SELECT COUNT(*) AS c FROM research_snapshots").fetchone() or {"c": 0})["c"]
            )
        finally:
            conn.close()

        grade_counts = {"good": 0, "caution": 0, "degraded": 0}
        flagged_series: list[dict[str, Any]] = []
        for r in series_rows:
            try:
                q = json.loads(r["quality_json"] or "{}")
            except ValueError:
                q = {}
            g = str(q.get("quality_grade") or "good")
            grade_counts[g] = grade_counts.get(g, 0) + 1
            if g in ("caution", "degraded"):
                flagged_series.append({
                    "series_id": r["series_id"],
                    "symbol": r["symbol"],
                    "asset_class": r["asset_class"],
                    "interval": r["interval"],
                    "quality_grade": g,
                    "as_of_bar": r["latest_bar_key"],
                    "last_synced_at_utc": r["last_synced_at_utc"],
                    "warnings": q.get("warnings", []),
                    "notes": q.get("notes", []),
                })

        db_size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "db_path": self.db_path,
            "db_size_bytes": db_size_bytes,
            "series_count": len(series_rows),
            "total_ohlcv_bars": total_bars,
            "scan_runs_count": scan_cnt,
            "sentinel_runs_count": sentinel_cnt,
            "research_snapshots_count": research_cnt,
            "quality_counts": grade_counts,
            "flagged_series": flagged_series[:50],
        }


_STORE_CACHE_LOCK = threading.Lock()
_STORES_BY_PATH: dict[str, DeskDataStore] = {}


def get_store(db_path: str | None = None) -> DeskDataStore:
    resolved = resolve_db_path(db_path)
    with _STORE_CACHE_LOCK:
        st = _STORES_BY_PATH.get(resolved)
        if st is None:
            st = DeskDataStore(resolved)
            _STORES_BY_PATH[resolved] = st
        return st
