#!/usr/bin/env python3
"""Layer-4 Holding Daily Sentinel ("持仓每日必检、核心逻辑或指标松动果断撤离").

Monitors active portfolio holdings (from ``portfolio/positions.json`` via
``portfolio_manager.load_positions`` and ``status == "holding"`` archives under
``research/``) plus optional watchlist light-refresh across 5 defense layers:

  1. Hard Stop / Price Defense (30min tactical stop + daily structural stop + proximity)
  2. Dual-Timeframe Chanlun Defense (daily bias/signals + 30min structure + trailing stop)
  3. Core Logic & Leading Indicator Triggers (``research_archive.evaluate_triggers`` +
     commodity B-structure squeeze/narrowing watch + red-line governance)
  4. Upcoming Event & Futures Roll-Window Alerts (archive events, earnings, contract LTD)
  5. Data Freshness & Integrity Guard (missing/failed price feed NEVER produces false green)
"""
from __future__ import annotations

import argparse
import calendar as cal_lib
import copy
import inspect
import json
import math
import os
import re
import statistics
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

sys.path.insert(0, os.path.dirname(__file__))
import chanlun as cl_mod
import market_snapshot as ms_mod
import research_archive as ra_mod
import research_data as rd_mod
import research_extensions as rext

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
DEFAULT_PORTFOLIO_DIR = os.path.join(ROOT_DIR, "portfolio")

STATUS_RED = "RED_EXIT"
STATUS_YELLOW = "YELLOW_LOOSENING"
STATUS_GREEN = "GREEN_HOLD"

STATUS_ZH_MAP = {
    STATUS_RED: "🔴 果断撤离",
    STATUS_YELLOW: "🟡 指标松动/警戒",
    STATUS_GREEN: "🟢 稳固持有",
}

STATUS_PRIORITY = {
    STATUS_RED: 0,
    STATUS_YELLOW: 1,
    STATUS_GREEN: 2,
}

MAX_DAILY_STALE_DAYS = 4
MAX_INTRADAY_STALE_DAYS = 4
MAX_CRYPTO_STALE_DAYS = 1
ROLL_INFO_WINDOW_DAYS = 10
ROLL_IMMINENT_DAYS = 5

RED_TRIGGER_KINDS = {"fundamental", "supply_demand", "policy", "chanlun"}
DISARMED_TRIGGER_STATUSES = {
    "disarmed",
    "disabled",
    "inactive",
    "paused",
    "muted",
    "archived",
    "done",
    "closed",
}
NON_EXIT_ACTION_PATTERNS = (
    "不平仓",
    "暂不平仓",
    "勿平仓",
    "不撤离",
    "暂不撤离",
    "勿撤离",
    "不清仓",
    "暂不清仓",
    "维持原止损",
    "继续持有",
    "不触发止损",
    "暂不止损",
    "不止损",
    "do not exit",
    "don't exit",
    "do not close",
    "don't close",
    "no exit",
)
EXIT_ACTION_KEYWORDS = (
    "撤离",
    "清仓",
    "止损",
    "平仓",
    "否决",
    "证伪",
    "exit",
    "close",
    "stop",
)

MONTH_CODE_TO_NUM: dict[str, int] = {code: num for num, code in rext.FUTURES_MONTH_CODES.items()}

KNOWN_OVERSEAS_FUTURES_ROOTS: set[str] = {
    spec["root"] for spec in rext.OVERSEAS_TERM_STRUCTURE_SPECS.values()
} | {
    "ALI",
    "PL",
    "PA",
    "HO",
    "RB",
    "LE",
    "HE",
    "CC",
    "ES",
    "NQ",
    "YM",
    "RTY",
    "ZM",
    "ZL",
    "GF",
    "MES",
    "MNQ",
    "MGC",
    "MCL",
}

SHFE_15TH_ROOTS: set[str] = {
    "CU", "AL", "ZN", "NI", "SN", "AU", "AG", "RB", "HC", "RU",
    "PB", "SS", "SP", "BU", "FU", "WR", "AO", "BR",
}
INE_PREV_MONTH_ROOTS: set[str] = {"SC"}
INE_EC_ROOTS: set[str] = {"EC"}
DCE_4TH_LAST_BDAY_ROOTS: set[str] = {"LH"}
DCE_10TH_BDAY_ROOTS: set[str] = {
    "I", "J", "JM", "M", "Y", "P", "V",
    "C", "CS", "A", "B", "L", "PP", "EG", "EB", "PG", "JD", "FB", "BB", "RR",
}
CZCE_10TH_BDAY_ROOTS: set[str] = {
    "TA", "MA", "SA", "FG", "OI", "SR", "CF", "SF", "SM",
    "AP", "CJ", "PK", "UR", "PF", "PX", "SH", "RM", "RS", "CY", "PM", "WH",
}
GFEX_10TH_BDAY_ROOTS: set[str] = {"LC", "SI", "PS"}

KNOWN_CN_FUTURES_ROOTS: set[str] = (
    {meta[1] for meta in rd_mod.CN_FUTURE_META.values()}
    | SHFE_15TH_ROOTS
    | INE_PREV_MONTH_ROOTS
    | INE_EC_ROOTS
    | DCE_4TH_LAST_BDAY_ROOTS
    | DCE_10TH_BDAY_ROOTS
    | CZCE_10TH_BDAY_ROOTS
    | GFEX_10TH_BDAY_ROOTS
)

OVERSEAS_CONTRACT_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,3})([FGHJKMNQUVXZ])(\d{2})(?:\.[A-Z]{2,4})?(?![A-Za-z0-9])"
)
CN_CONTRACT_4D_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3})(\d{4})(?![A-Za-z0-9])")
CN_CONTRACT_3D_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,3})(\d{3})(?![A-Za-z0-9])")
_FUTURES_CONTRACT_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"(?:[A-Z]{1,3}[FGHJKMNQUVXZ]\d{2}(?:\.[A-Z]{2,4})?)"
    r"|(?:[A-Z]{1,3}\d{4})"
    r"|(?:[A-Z]{2,3}\d{3})"
    r")(?![A-Za-z0-9])"
)

CZCE_3D_ROOTS = CZCE_10TH_BDAY_ROOTS
FUTURES_EXCHANGE_SUFFIXES = (".NYM", ".CMX", ".CBT", ".NYB", ".CME", ".ICE")


# ---------------------------------------------------------------------------
# Generic Helpers
# ---------------------------------------------------------------------------

def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        fval = float(val)
        return fval if math.isfinite(fval) else None
    s = str(val).strip().replace(",", "")
    if not s or s.lower() in {"nan", "inf", "-inf", "+inf", "none", "null", "n/a", "na", "-", "--"}:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        fval = float(s)
        return fval if math.isfinite(fval) else None
    except ValueError:
        return None


def _round_finite(val: float | None, ndigits: int = 4) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    out = round(float(val), ndigits)
    return out if math.isfinite(out) else None


def _parse_iso_date(val: Any) -> date | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m8 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m8:
        try:
            return date(int(m8.group(1)), int(m8.group(2)), int(m8.group(3)))
        except ValueError:
            return None
    return None


def _sanitize_for_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _safe_normalize_symbol(raw: str) -> tuple[str, str | None]:
    try:
        return ms_mod.normalize_symbol(raw)
    except (ValueError, TypeError, AttributeError):
        return str(raw).strip().upper(), None


def _normalize_direction(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in {"long", "bull", "bullish", "up", "buy", "多", "多头", "做多", "偏多"}:
        return "long"
    if s in {"short", "bear", "bearish", "down", "sell", "空", "空头", "做空", "偏空"}:
        return "short"
    if s in {"neutral", "flat", "range", "中性", "震荡", "观望"}:
        return "neutral"
    return None


def _get_source_block(bundle: dict[str, Any] | None, *candidate_keys: str) -> tuple[str | None, dict[str, Any] | None]:
    """Lookup a source block defensively across ``fundamentals_and_supply``, ``sources``, and top-level."""
    if not isinstance(bundle, dict):
        return None, None
    containers = [
        bundle.get("fundamentals_and_supply"),
        bundle.get("sources"),
        bundle,
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in candidate_keys:
            block = container.get(key)
            if isinstance(block, dict):
                return key, block
    return None, None


def _is_usable_source(block: dict[str, Any] | None) -> bool:
    if not isinstance(block, dict):
        return False
    st = str(block.get("status") or "").strip().lower()
    return st not in {"error", "skipped"}


def _get_intraday_chanlun(chanlun_dict: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(chanlun_dict, dict):
        return None
    for key in ("intraday_30m", "intraday"):
        val = chanlun_dict.get(key)
        if isinstance(val, dict) and "error" not in val:
            return val
    return None


def _default_tz_for_asset_class(asset_class: str | None) -> Any:
    """Return exchange-local timezone for naive intraday timestamps based on ``asset_class``."""
    ac = (asset_class or "").strip().lower()
    if ac in {"cn_future", "a_share"}:
        return ZoneInfo("Asia/Shanghai")
    if ac in {"us_stock_or_etf", "commodity"}:
        return ZoneInfo("America/New_York")
    return timezone.utc


def _parse_bar_timestamp(
    val: Any,
    asset_class: str | None = None,
) -> tuple[datetime | None, date | None, bool, bool]:
    """Parse bar/event timestamp into ``(dt_utc, local_date, has_time, has_tz)``.

    Distinguishes pure calendar dates (``YYYY-MM-DD``, ``has_time=False``) from real
    intraday timestamps (``has_time=True``). Preserves explicit ISO timezone offsets
    (``-05:00``, ``+08:00``, ``Z``, ``UTC``) and localizes naive time-bearing timestamps
    according to ``asset_class`` (e.g. ``Asia/Shanghai`` for ``cn_future``/``a_share``,
    ``America/New_York`` for ``us_stock_or_etf``/``commodity``, ``UTC`` for ``crypto``/``fx``).
    """
    if val is None or isinstance(val, bool):
        return None, None, False, False
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(timezone.utc), val.date(), True, True
        tz = _default_tz_for_asset_class(asset_class)
        localized = val.replace(tzinfo=tz)
        return localized.astimezone(timezone.utc), localized.date(), True, False
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc), val, False, False

    s = str(val).strip()
    if not s or s.lower() in {"none", "null", "nan", "nat", "n/a", "na"}:
        return None, None, False, False

    # Pure calendar dates (no time component)
    m_date = re.fullmatch(r"(\d{4})[-/](\d{2})[-/](\d{2})", s)
    if m_date:
        try:
            d = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), d, False, False
        except ValueError:
            return None, None, False, False
    m_date8 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m_date8:
        try:
            d = date(int(m_date8.group(1)), int(m_date8.group(2)), int(m_date8.group(3)))
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), d, False, False
        except ValueError:
            return None, None, False, False

    # Time-bearing strings: normalize trailing UTC/GMT/Z for Python 3.10 fromisoformat
    s_norm = s
    explicit_tz = False
    if re.search(r"\s+(?:UTC|GMT)$", s_norm, flags=re.IGNORECASE):
        s_norm = re.sub(r"\s+(?:UTC|GMT)$", "+00:00", s_norm, flags=re.IGNORECASE)
        explicit_tz = True
    elif s_norm.endswith(("Z", "z")):
        s_norm = s_norm[:-1] + "+00:00"
        explicit_tz = True

    # Support compact offset like +0800 / -0500
    m_compact_tz = re.search(r"([+-])(\d{2})(\d{2})$", s_norm)
    if m_compact_tz:
        s_norm = s_norm[: m_compact_tz.start()] + f"{m_compact_tz.group(1)}{m_compact_tz.group(2)}:{m_compact_tz.group(3)}"
        explicit_tz = True

    try:
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc), dt.date(), True, True
        tz = _default_tz_for_asset_class(asset_class)
        localized = dt.replace(tzinfo=tz)
        return localized.astimezone(timezone.utc), localized.date(), True, explicit_tz
    except ValueError:
        pass

    if pd is not None:
        try:
            ts = pd.Timestamp(s)
            if isinstance(ts, pd.Timestamp) and not bool(pd.isna(ts)):
                has_time_tok = bool(re.search(r"\d{1,2}:\d{2}", s))
                if ts.tzinfo is not None:
                    local_d = ts.date()
                    dt_utc = ts.tz_convert("UTC").to_pydatetime()
                    if isinstance(dt_utc, datetime) and isinstance(local_d, date):
                        return dt_utc, local_d, has_time_tok, True
                py_dt = ts.to_pydatetime()
                if isinstance(py_dt, datetime):
                    if not has_time_tok:
                        return datetime(py_dt.year, py_dt.month, py_dt.day, tzinfo=timezone.utc), py_dt.date(), False, False
                    tz = _default_tz_for_asset_class(asset_class)
                    localized = py_dt.replace(tzinfo=tz)
                    return localized.astimezone(timezone.utc), localized.date(), True, False
        except (ValueError, TypeError):
            return None, None, False, False

    return None, None, False, False


def _has_time_component(raw: Any) -> bool:
    """Return True iff ``raw`` carries an explicit hour/minute component (not date-only)."""
    _, _, has_time, _ = _parse_bar_timestamp(raw)
    return has_time


def _extract_tech_timestamp(
    tech: dict[str, Any] | None,
    asset_class: str | None = None,
) -> tuple[datetime | None, date | None, bool, bool, str | None]:
    """Extract ``(dt_utc, local_date, has_time, has_tz, raw_label)`` from a technicals block."""
    if not isinstance(tech, dict):
        return None, None, False, False, None

    raw_bar = tech.get("as_of_bar")
    raw_utc = tech.get("as_of_bar_utc")
    label = str(raw_bar or raw_utc).strip() if (raw_bar or raw_utc) else None

    bar_dt, bar_local_d, bar_has_time, bar_has_tz = _parse_bar_timestamp(raw_bar, asset_class=asset_class)
    utc_dt, utc_local_d, utc_has_time, utc_has_tz = _parse_bar_timestamp(raw_utc, asset_class=asset_class)

    if bar_dt is not None:
        if bar_has_time and bar_has_tz:
            return bar_dt, bar_local_d, True, True, label
        if bar_has_time and utc_dt is not None:
            return utc_dt, bar_local_d or utc_local_d, True, True, label
        return bar_dt, bar_local_d, bar_has_time, bar_has_tz, label
    if utc_dt is not None:
        return utc_dt, utc_local_d, utc_has_time, utc_has_tz, label
    return None, None, False, False, label


def _is_intraday_older_than_daily(
    daily_tech: dict[str, Any] | None,
    intra_tech: dict[str, Any] | None,
    asset_class: str | None = None,
    *,
    now_utc: datetime | None = None,
    symbol: str | None = None,
) -> bool:
    """Compare full UTC timestamps (and same-day official close for closed CN bars) to detect intraday lagging daily."""
    d_dt, d_local, d_has_time, _, _ = _extract_tech_timestamp(daily_tech, asset_class=asset_class)
    i_dt, i_local, i_has_time, _, _ = _extract_tech_timestamp(intra_tech, asset_class=asset_class)
    if d_dt is None or i_dt is None:
        return False
    if d_has_time and i_has_time:
        return i_dt < d_dt
    if not d_has_time:
        # Daily is a calendar date without time; intraday lags if its local trading date is earlier.
        if i_local is not None and d_local is not None:
            if i_local < d_local:
                return True
            if (
                i_local == d_local
                and i_has_time
                and isinstance(daily_tech, dict)
                and daily_tech.get("bar_is_closed") is True
            ):
                ac = (asset_class or "").strip().lower()
                if ac in {"cn_future", "a_share"}:
                    sh_tz = ZoneInfo("Asia/Shanghai")
                    is_bond = bool(re.match(r"^(T|TF|TS|TL)\d+", (symbol or "").strip().upper()))
                    close_hh, close_mm = (15, 15) if (ac == "cn_future" and is_bond) else (15, 0)
                    close_dt_utc = datetime(
                        d_local.year, d_local.month, d_local.day, close_hh, close_mm, tzinfo=sh_tz
                    ).astimezone(timezone.utc)
                    # Only enforce same-day post-close lag once wall-clock time has actually reached official close
                    if now_utc is None or now_utc.astimezone(timezone.utc) >= close_dt_utc:
                        anchor = str((intra_tech or {}).get("bar_timestamp_anchor") or "end").strip().lower()
                        iv_raw = str((intra_tech or {}).get("bar_interval") or "30m").strip().lower()
                        iv_m = re.fullmatch(r"(\d+)\s*(m|min|h)", iv_raw)
                        iv_mins = int(iv_m.group(1)) * (60 if iv_m.group(2) == "h" else 1) if iv_m else 30
                        i_end_utc = i_dt + timedelta(minutes=iv_mins) if anchor == "start" else i_dt
                        if i_end_utc < close_dt_utc:
                            return True
            return False
        return i_dt.date() < d_dt.date()
    # Daily has time, intraday is date-only
    if d_local is not None and i_local is not None:
        return i_local < d_local
    return i_dt < d_dt


def _bar_age_days(
    bar_dt_utc: datetime | None,
    bar_local_date: date | None,
    has_time: bool,
    anchor_date: date,
    now_utc: datetime | None = None,
) -> float | None:
    """Compute bar age in days using exact UTC delta when ``now_utc`` & time exist, else calendar days."""
    if bar_dt_utc is None and bar_local_date is None:
        return None
    if now_utc is not None and bar_dt_utc is not None and has_time:
        now_norm = now_utc.astimezone(timezone.utc) if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
        return (now_norm - bar_dt_utc).total_seconds() / 86400.0
    ref_d = bar_local_date or (bar_dt_utc.date() if bar_dt_utc is not None else None)
    if ref_d is None:
        return None
    return float((anchor_date - ref_d).days)


def _select_latest_price(
    snapshot: dict[str, Any] | None,
    asset_class: str | None = None,
    *,
    now_utc: datetime | None = None,
    symbol: str | None = None,
) -> tuple[float | None, str | None, str | None]:
    """Return ``(price, source_label, freshness_warning)`` preferring fresher 30m price over daily."""
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        return None, None, None

    eff_ac = asset_class or snapshot.get("asset_class")
    eff_sym = symbol or snapshot.get("symbol")
    daily_tech = snapshot.get("technicals") if isinstance(snapshot.get("technicals"), dict) else {}
    intra_tech = snapshot.get("intraday_technicals") if isinstance(snapshot.get("intraday_technicals"), dict) else {}

    daily_px = _to_float(daily_tech.get("last_close"))
    intra_px = _to_float(intra_tech.get("last_close"))

    if intra_px is not None and daily_px is not None:
        if _is_intraday_older_than_daily(
            daily_tech,
            intra_tech,
            asset_class=eff_ac,
            now_utc=now_utc,
            symbol=eff_sym,
        ):
            warn = (
                f"intraday_30m: 30min 行情时间戳({intra_tech.get('as_of_bar') or intra_tech.get('as_of_bar_utc')})滞后于日线"
                f"({daily_tech.get('as_of_bar') or daily_tech.get('as_of_bar_utc')})，现价已回退使用日线收盘价"
            )
            return daily_px, "daily", warn
        return intra_px, "intraday_30m", None
    if intra_px is not None:
        return intra_px, "intraday_30m", None
    if daily_px is not None:
        return daily_px, "daily", None
    return None, None, None


def _price_display_dp(price: float, asset_class: str | None = None) -> int:
    """Return canonical UI price display decimal places matching frontend ``fmtPrice``."""
    a = abs(price)
    if a == 0:
        return 2
    if a < 2:
        return 4
    if a < 20:
        return 3
    return 2


def _quantize_display_price(price: float, dp: int) -> Decimal | None:
    try:
        q = Decimal("1") if dp <= 0 else Decimal("1").scaleb(-dp)
        return Decimal(str(price)).quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _compute_raw_unrealized_pnl_pct(
    last_price: float | None,
    entry_price: float | None,
    direction: str | None,
) -> float | None:
    """Compute unrounded unrealized PnL percentage for risk & trailing-stop decisions."""
    if last_price is None or entry_price is None or entry_price <= 0:
        return None
    if direction not in {"long", "short"}:
        return None
    if not (math.isfinite(last_price) and math.isfinite(entry_price)):
        return None
    eps = max(abs(entry_price) * 1e-12, 1e-15)
    if abs(last_price - entry_price) < eps:
        return 0.0
    return (
        (last_price / entry_price - 1.0) * 100.0
        if direction == "long"
        else (1.0 - last_price / entry_price) * 100.0
    )


def _compute_unrealized_pnl_pct(
    last_price: float | None,
    entry_price: float | None,
    direction: str | None,
    asset_class: str | None = None,
) -> float | None:
    """Compute unrealized PnL percentage without phantom -0.01%/+0.01% sub-tick noise.

    When ``last_price`` and ``entry_price`` are equal within floating-point tolerance OR
    round to the exact same canonical display quote (e.g. yfinance half-cent 30m print
    ``84.605`` displayed as ``84.61`` against ``entry_price = 84.61``), returns ``0.0``.
    Never clamps ultra-low-price assets (< 0.2) to fixed 4dp so micro-price moves stay exact.
    """
    if last_price is None or entry_price is None or entry_price <= 0:
        return None
    if direction not in {"long", "short"}:
        return None
    if not (math.isfinite(last_price) and math.isfinite(entry_price)):
        return None
    if abs(last_price - entry_price) < 1e-9:
        return 0.0
    if abs(entry_price) >= 0.2:
        dp = _price_display_dp(entry_price, asset_class=asset_class)
        q_last = _quantize_display_price(last_price, dp)
        q_entry = _quantize_display_price(entry_price, dp)
        if q_last is not None and q_entry is not None and q_last == q_entry:
            return 0.0
    raw_pct = (
        (last_price / entry_price - 1.0) * 100.0
        if direction == "long"
        else (1.0 - last_price / entry_price) * 100.0
    )
    rounded = _round_finite(raw_pct, 2)
    if rounded is not None and abs(rounded) < 1e-9:
        return 0.0
    return rounded


def _combine_signal_text(cl_block: dict[str, Any] | None, summary_block: dict[str, Any] | None) -> str | None:
    parts: list[str] = []
    if isinstance(summary_block, dict):
        summ = str(summary_block.get("summary") or "").strip()
        if summ and summ not in parts:
            parts.append(summ)
    if isinstance(cl_block, dict):
        sigs = cl_block.get("signals")
        if isinstance(sigs, list):
            for s in sigs:
                s_clean = str(s or "").strip()
                if s_clean and s_clean not in parts:
                    parts.append(s_clean)
    return " | ".join(parts) if parts else None


def _extract_cross_spread_metrics(cms: dict[str, Any], symbol: str | None = None) -> dict[str, float]:
    """Extract stable leg-specific cross-market spread keys plus strict ``cross_spread_primary``.

    For ``SC0``, NEVER substitutes ``brent_wti_spread_usd`` when ``sc_minus_brent`` is missing.
    """
    out: dict[str, float] = {}
    grp = str(cms.get("group") or "").strip().lower()
    sym = (symbol or str(cms.get("symbol") or "")).strip().upper()
    inputs = cms.get("inputs") if isinstance(cms.get("inputs"), dict) else {}

    # 1. Stable leg-specific spread keys
    brent_wti = _to_float(cms.get("brent_minus_wti_usd_bbl"))
    if brent_wti is None:
        brent_wti = _to_float(cms.get("brent_wti_spread_usd"))
    if brent_wti is None:
        brent_wti = _to_float(cms.get("spread_brent_wti_usd"))
    if brent_wti is not None:
        out["spread_brent_wti_usd"] = brent_wti

    sc_brent = _to_float(cms.get("sc_minus_brent_usd_bbl"))
    if sc_brent is None:
        sc_brent = _to_float(cms.get("sc_minus_brent_usd"))
    if sc_brent is None:
        sc_brent = _to_float(cms.get("spread_sc_brent_usd"))
    if sc_brent is not None:
        out["spread_sc_brent_usd"] = sc_brent

    cu_hg_cny = _to_float(cms.get("spread_cu_hg_vat_excl_cny_t"))
    if cu_hg_cny is None:
        cu_hg_cny = _to_float(cms.get("shfe_over_comex_vat_excl_cny_t"))
    cu_hg_usd = _to_float(cms.get("spread_ex_vat_usd_mt"))
    if cu_hg_cny is None and cu_hg_usd is not None:
        fx_cny = _to_float(inputs.get("usdcny")) or _to_float(cms.get("usdcny"))
        if fx_cny is not None and fx_cny > 0:
            cu_hg_cny = _round_finite(cu_hg_usd * fx_cny, 2)
    if cu_hg_cny is not None:
        out["spread_cu_hg_vat_excl_cny_t"] = cu_hg_cny
    if cu_hg_usd is not None:
        out["spread_cu_hg_vat_excl_usd_t"] = cu_hg_usd

    au_gc = _to_float(cms.get("spread_au_gc_cny_g"))
    if au_gc is None:
        au_gc = _to_float(cms.get("shfe_minus_comex_cny_g"))
    if au_gc is not None:
        out["spread_au_gc_cny_g"] = au_gc

    ag_si = _to_float(cms.get("spread_ag_si_cny_kg"))
    if ag_si is None:
        ag_si = _to_float(cms.get("shfe_minus_comex_tax_incl_cny_kg"))
    if ag_si is None:
        ag_si = _to_float(cms.get("shfe_minus_comex_cny_kg"))
    if ag_si is not None:
        out["spread_ag_si_cny_kg"] = ag_si

    sr_sb = _to_float(cms.get("spread_sr_sb_cny_t"))
    if sr_sb is None:
        sr_sb = _to_float(cms.get("czce_minus_ice_raw_fob_cny_mt"))
    if sr_sb is None and (grp == "sugar" or sym in {"SR0", "SB=F"}):
        sr_sb = _to_float(cms.get("zce_minus_ice_cny_t"))
    if sr_sb is not None:
        out["spread_sr_sb_cny_t"] = sr_sb

    cf_ct = _to_float(cms.get("spread_cf_ct_cny_t"))
    if cf_ct is None:
        cf_ct = _to_float(cms.get("czce_minus_ice_futures_cny_mt"))
    if cf_ct is None and (grp == "cotton" or sym in {"CF0", "CT=F"}):
        cf_ct = _to_float(cms.get("zce_minus_ice_cny_t"))
    if cf_ct is not None:
        out["spread_cf_ct_cny_t"] = cf_ct

    # 2. Strict symbol-appropriate cross_spread_primary (NO cross-leg silent substitution for SC0)
    primary: float | None = None
    is_sc = sym == "SC0" or bool(re.fullmatch(r"SC\d{4}", sym))
    if grp == "crude_oil" or sym in {"CL=F", "BZ=F"} or is_sc:
        if is_sc:
            primary = sc_brent
        else:
            primary = brent_wti
    elif grp == "copper" or sym in {"CU0", "HG=F"}:
        for cand in (cu_hg_usd, cu_hg_cny, _to_float(cms.get("shfe_comex_ratio"))):
            if cand is not None:
                primary = cand
                break
    elif grp == "gold" or sym in {"AU0", "GC=F"}:
        primary = au_gc if au_gc is not None else _to_float(cms.get("spread_pct"))
    elif grp == "silver" or sym in {"AG0", "SI=F"}:
        primary = ag_si if ag_si is not None else _to_float(cms.get("spread_pct"))
    elif grp == "sugar" or sym in {"SR0", "SB=F"}:
        primary = sr_sb if sr_sb is not None else _to_float(cms.get("ratio"))
    elif grp == "cotton" or sym in {"CF0", "CT=F"}:
        primary = cf_ct if cf_ct is not None else _to_float(cms.get("ratio"))
    else:
        for cand in (
            _to_float(cms.get("cross_spread_primary")),
            brent_wti,
            sc_brent,
            cu_hg_usd,
            cu_hg_cny,
            au_gc,
            ag_si,
            sr_sb,
            cf_ct,
            _to_float(cms.get("ratio")),
            _to_float(cms.get("spread_pct")),
        ):
            if cand is not None:
                primary = cand
                break

    if primary is not None:
        out["cross_spread_primary"] = primary
    return out


def _extract_primary_cross_spread(cms: dict[str, Any], symbol: str | None = None) -> float | None:
    return _extract_cross_spread_metrics(cms, symbol=symbol).get("cross_spread_primary")


def _collect_data_health_warnings(
    research_bundle: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    snapshot_override_healthy: bool = False,
) -> list[str]:
    warnings: list[str] = []
    seen_sources: set[str] = set()

    def _add(source_name: str, status: str, detail: str | None = None) -> None:
        key = source_name.strip().lower()
        if key in seen_sources:
            return
        seen_sources.add(key)
        msg = f"{source_name}: status={status}"
        if detail:
            msg += f" ({detail})"
        warnings.append(msg)

    if isinstance(research_bundle, dict):
        sources_status = research_bundle.get("sources_status")
        if isinstance(sources_status, dict):
            for sname, meta in sources_status.items():
                if snapshot_override_healthy and str(sname).strip().lower() == "market_snapshot":
                    continue
                if not isinstance(meta, dict):
                    continue
                st = str(meta.get("status") or "").strip().lower()
                if st in {"error", "partial", "stale"}:
                    detail = str(meta.get("error") or meta.get("reason") or "").strip() or None
                    _add(str(sname), st, detail)

        for container_key in ("fundamentals_and_supply", "sources", "macro_dual_core"):
            container = research_bundle.get(container_key)
            if not isinstance(container, dict):
                continue
            for sname, block in container.items():
                if not isinstance(block, dict):
                    continue
                st = str(block.get("status") or "").strip().lower()
                if st in {"error", "partial", "stale"}:
                    detail = (
                        str(
                            block.get("error")
                            or block.get("quote_date_mismatch_warning")
                            or block.get("alignment_warning")
                            or block.get("reason")
                            or ""
                        ).strip()
                        or None
                    )
                    _add(str(sname), st, detail)
                else:
                    # Inspect nested sub-sources (e.g. perp_open_interest -> okx / binance)
                    for sub_name, sub_block in block.items():
                        if isinstance(sub_block, dict):
                            sub_st = str(sub_block.get("status") or "").strip().lower()
                            if sub_st in {"error", "partial", "stale"}:
                                detail = str(sub_block.get("error") or sub_block.get("reason") or "").strip() or None
                                _add(f"{sname}.{sub_name}", sub_st, detail)

    if isinstance(snapshot, dict):
        snap_st = str(snapshot.get("status") or "").strip().lower()
        if snapshot.get("error"):
            _add("market_snapshot", "error", str(snapshot.get("error")))
        elif snap_st in {"error", "partial", "stale"}:
            _add("market_snapshot", snap_st, str(snapshot.get("reason") or "").strip() or None)

        intra_err = snapshot.get("intraday_error")
        if not intra_err and isinstance(snapshot.get("chanlun"), dict):
            intra_err = snapshot["chanlun"].get("intraday_error")
            intra_obj = snapshot["chanlun"].get("intraday_30m") or snapshot["chanlun"].get("intraday")
            if not intra_err and isinstance(intra_obj, dict) and intra_obj.get("error"):
                intra_err = intra_obj.get("error")
        if intra_err:
            _add("intraday_30m", "error", str(intra_err))

        daily_cl = (snapshot.get("chanlun") or {}).get("daily") if isinstance(snapshot.get("chanlun"), dict) else None
        if isinstance(daily_cl, dict) and daily_cl.get("error"):
            _add("chanlun_daily", "error", str(daily_cl.get("error")))

    return warnings


# ---------------------------------------------------------------------------
# 1. Metric Extractor
# ---------------------------------------------------------------------------

_CONTRACT_PRICE_OBS_KEYS: tuple[str, ...] = (
    "close",
    "contract_close",
    "return_1d_pct",
    "return_5d_pct",
    "return_1bar_30m_pct",
    "return_5bar_30m_pct",
    "atr14",
    "atr14_30m",
    "intraday_30m_zd",
    "intraday_30m_zg",
)


KNOWN_AUTO_METRICS: frozenset[str] = ra_mod.KNOWN_AUTO_METRICS


def is_known_auto_metric(metric: str | None) -> bool:
    """Return True if ``metric`` is populated automatically by ``extract_sentinel_observations``."""
    return ra_mod.is_known_auto_metric(metric)


def extract_sentinel_observations(
    research_bundle: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    ref_date: date | None = None,
    *,
    include_price_metrics: bool = True,
    asset_class: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Flatten live ``research_data.py`` + ``market_snapshot.py`` payloads for trigger evaluation.

    Never crashes on partial, stale, errored, or missing sources; always populates
    ``data_health_warnings: list[str]``. When ``include_price_metrics=False`` (e.g. contract
    quote mismatch), strictly withholds price/stop/30m observations from both ``snapshot`` and
    ``research_bundle['snapshot']`` to guarantee zero cross-contract contamination.
    """
    anchor = ref_date or datetime.now(timezone.utc).date()
    effective_snap = snapshot if isinstance(snapshot, dict) else (
        research_bundle.get("snapshot")
        if isinstance(research_bundle, dict) and isinstance(research_bundle.get("snapshot"), dict)
        else None
    )
    snap_override_healthy = (
        isinstance(snapshot, dict)
        and not snapshot.get("error")
        and str(snapshot.get("status") or "ok").strip().lower() not in {"error", "partial", "stale"}
    )
    obs: dict[str, Any] = {}
    warnings = _collect_data_health_warnings(
        research_bundle,
        effective_snap,
        snapshot_override_healthy=snap_override_healthy,
    )

    # --- Price / Technicals / Chanlun ---
    if isinstance(effective_snap, dict) and not effective_snap.get("error"):
        daily_tech = effective_snap.get("technicals") if isinstance(effective_snap.get("technicals"), dict) else {}
        intra_tech = (
            effective_snap.get("intraday_technicals")
            if isinstance(effective_snap.get("intraday_technicals"), dict)
            else {}
        )
        eff_ac = asset_class or effective_snap.get("asset_class")
        if include_price_metrics:
            px, _, px_warn = _select_latest_price(
                effective_snap,
                asset_class=eff_ac,
                now_utc=now_utc,
                symbol=effective_snap.get("symbol"),
            )
            if px_warn and px_warn not in warnings:
                warnings.append(px_warn)
            if px is not None:
                obs["close"] = px

            atr14 = _to_float(daily_tech.get("atr14"))
            if atr14 is not None:
                obs["atr14"] = atr14
            atr14_30m = _to_float(intra_tech.get("atr14"))
            if atr14_30m is not None:
                obs["atr14_30m"] = atr14_30m

            # Strictly daily returns — NEVER copy 30m return_1bar_pct into return_1d_pct!
            ret1 = _to_float(daily_tech.get("return_1d_pct"))
            if ret1 is not None:
                obs["return_1d_pct"] = ret1
            ret5 = _to_float(daily_tech.get("return_5d_pct"))
            if ret5 is not None:
                obs["return_5d_pct"] = ret5

            ret1_30m = _to_float(intra_tech.get("return_1bar_pct"))
            if ret1_30m is not None:
                obs["return_1bar_30m_pct"] = ret1_30m
            ret5_30m = _to_float(intra_tech.get("return_5bar_pct"))
            if ret5_30m is not None:
                obs["return_5bar_30m_pct"] = ret5_30m

        cl = effective_snap.get("chanlun") if isinstance(effective_snap.get("chanlun"), dict) else {}
        daily_cl = cl.get("daily") if isinstance(cl.get("daily"), dict) else {}
        intra_cl = _get_intraday_chanlun(cl) or {}
        dual = cl.get("dual_alignment") if isinstance(cl.get("dual_alignment"), dict) else {}

        daily_zs = daily_cl.get("latest_zhongshu") if isinstance(daily_cl.get("latest_zhongshu"), dict) else {}
        daily_zd = _to_float(daily_zs.get("zd"))
        if daily_zd is None:
            daily_zd = _to_float(dual.get("daily_zd"))
        daily_zg = _to_float(daily_zs.get("zg"))
        if daily_zg is None:
            daily_zg = _to_float(dual.get("daily_zg"))
        if daily_zd is not None:
            obs["daily_zd"] = daily_zd
        if daily_zg is not None:
            obs["daily_zg"] = daily_zg

        if include_price_metrics:
            intra_zs = intra_cl.get("latest_zhongshu") if isinstance(intra_cl.get("latest_zhongshu"), dict) else {}
            intra_zd = _to_float(intra_zs.get("zd"))
            if intra_zd is None:
                intra_zd = _to_float(dual.get("intraday_zd"))
            intra_zg = _to_float(intra_zs.get("zg"))
            if intra_zg is None:
                intra_zg = _to_float(dual.get("intraday_zg"))
            if intra_zd is not None:
                obs["intraday_30m_zd"] = intra_zd
            if intra_zg is not None:
                obs["intraday_30m_zg"] = intra_zg

        d_sig = _combine_signal_text(daily_cl, cl.get("signal_summary"))
        if d_sig:
            obs["daily_signal"] = d_sig
        if include_price_metrics:
            i_sig = _combine_signal_text(intra_cl, cl.get("intraday_signal_summary"))
            if i_sig:
                obs["intraday_30m_signal"] = i_sig

    sym_hint = None
    if isinstance(research_bundle, dict) and research_bundle.get("symbol"):
        sym_hint = str(research_bundle["symbol"])
    elif isinstance(effective_snap, dict) and effective_snap.get("symbol"):
        sym_hint = str(effective_snap["symbol"])

    # --- Commodities ---
    _, ts = _get_source_block(research_bundle, "term_structure")
    if _is_usable_source(ts):
        assert ts is not None
        if ts.get("structure") is not None:
            obs["curve_structure"] = str(ts["structure"])
        ry_near = _to_float(ts.get("annualized_roll_yield_near_pct"))
        if ry_near is not None:
            obs["roll_yield_near_pct"] = ry_near
        ry_curve = _to_float(ts.get("annualized_roll_yield_curve_pct"))
        if ry_curve is not None:
            obs["roll_yield_curve_pct"] = ry_curve
        ff_spread = _to_float(ts.get("front_far_spread"))
        if ff_spread is None:
            ff_spread = _to_float(ts.get("near_minus_far_spread"))
        if ff_spread is not None:
            obs["front_far_spread"] = ff_spread

    _, sb = _get_source_block(research_bundle, "spot_and_basis", "spot_basis")
    if _is_usable_source(sb):
        assert sb is not None
        basis_pct = _to_float(sb.get("dominant_basis_rate_pct"))
        if basis_pct is None:
            raw_rate = _to_float(sb.get("dominant_basis_rate"))
            basis_pct = _round_finite(raw_rate * 100.0, 4) if raw_rate is not None else _to_float(sb.get("basis_pct"))
        if basis_pct is not None:
            obs["basis_pct"] = basis_pct
        spot_px = _to_float(sb.get("spot_price"))
        if spot_px is not None:
            obs["spot_price"] = spot_px

    _, inv = _get_source_block(research_bundle, "inventory_em", "cn_inventory")
    if _is_usable_source(inv):
        assert inv is not None
        inv_latest = _to_float(inv.get("latest_inventory"))
        if inv_latest is None:
            inv_latest = _to_float(inv.get("inventory_latest"))
        if inv_latest is not None:
            obs["inventory_latest"] = inv_latest
        inv_chg = _to_float(inv.get("chg_5d_pct"))
        if inv_chg is None:
            inv_chg = _to_float(inv.get("inventory_chg_pct"))
        if inv_chg is None:
            inv_chg = _to_float(inv.get("chg_20d_pct"))
        if inv_chg is not None:
            obs["inventory_chg_pct"] = inv_chg

    _, freight = _get_source_block(research_bundle, "baltic_freight")
    if _is_usable_source(freight):
        assert freight is not None
        idx_map = freight.get("indices") if isinstance(freight.get("indices"), dict) else freight
        for fkey in ("BDTI", "BCTI", "BDI", "BCI", "BSI"):
            entry = idx_map.get(fkey)
            if entry is None:
                entry = idx_map.get(fkey.lower())
            val = _to_float(entry.get("latest_value")) if isinstance(entry, dict) else _to_float(entry)
            if val is not None:
                obs[fkey.lower()] = val

    _, cms = _get_source_block(research_bundle, "cross_market_spread")
    if _is_usable_source(cms):
        assert cms is not None
        spread_metrics = _extract_cross_spread_metrics(cms, symbol=sym_hint)
        obs.update(spread_metrics)

    _, cftc = _get_source_block(research_bundle, "cftc_cot", "cftc_disagg")
    if _is_usable_source(cftc):
        assert cftc is not None
        mm = cftc.get("managed_money") if isinstance(cftc.get("managed_money"), dict) else {}
        mm_net = _to_float(mm.get("net"))
        if mm_net is None:
            mm_net = _to_float(cftc.get("cftc_mm_net"))
        mm_pct = _to_float(mm.get("net_pct_oi"))
        if mm_pct is None:
            mm_pct = _to_float(cftc.get("cftc_mm_net_pct_oi"))
        if mm_net is not None:
            obs["cftc_mm_net"] = round(mm_net) if float(mm_net).is_integer() else mm_net
        if mm_pct is not None:
            obs["cftc_mm_net_pct_oi"] = mm_pct

    # --- FX ---
    _, rdiff = _get_source_block(research_bundle, "rate_differential", "fx_rate_differential")
    if _is_usable_source(rdiff):
        assert rdiff is not None
        diff10 = None
        for k10 in ("base_minus_quote_10y_bp", "us_minus_cn_10y_bp", "rate_diff_10y_bp", "us_minus_de_10y_bp"):
            diff10 = _to_float(rdiff.get(k10))
            if diff10 is not None:
                break
        if diff10 is not None:
            obs["rate_diff_10y_bp"] = diff10
        diff2 = None
        for k2 in ("base_minus_quote_2y_bp", "us_minus_cn_2y_bp", "rate_diff_2y_bp"):
            diff2 = _to_float(rdiff.get(k2))
            if diff2 is not None:
                break
        if diff2 is not None:
            obs["rate_diff_2y_bp"] = diff2

    _, tff = _get_source_block(research_bundle, "cftc_fx_cot")
    if _is_usable_source(tff):
        assert tff is not None
        lev = tff.get("leveraged_funds") if isinstance(tff.get("leveraged_funds"), dict) else {}
        lev_net = _to_float(lev.get("net"))
        if lev_net is None:
            lev_net = _to_float(tff.get("tff_lev_money_net"))
        aligned_net = _to_float(tff.get("pair_aligned_lev_money_net"))
        if aligned_net is None:
            aligned_net = _to_float(tff.get("tff_pair_aligned_net"))
        if lev_net is not None:
            obs["tff_lev_money_net"] = round(lev_net) if float(lev_net).is_integer() else lev_net
        if aligned_net is not None:
            obs["tff_pair_aligned_net"] = round(aligned_net) if float(aligned_net).is_integer() else aligned_net

    # --- Equities ---
    _, val5y = _get_source_block(research_bundle, "valuation_5y_em")
    if _is_usable_source(val5y):
        assert val5y is not None
        pe_pct = _to_float(val5y.get("pe_ttm_5y_percentile"))
        if pe_pct is None:
            pe_pct = _to_float(val5y.get("pe_ttm_pctile_5y"))
        pb_pct = _to_float(val5y.get("pb_5y_percentile"))
        if pb_pct is None:
            pb_pct = _to_float(val5y.get("pb_pctile_5y"))
        pe_ttm = _to_float(val5y.get("pe_ttm"))
        pb = _to_float(val5y.get("pb"))
        if pe_pct is not None:
            obs["pe_ttm_pctile_5y"] = pe_pct
        if pb_pct is not None:
            obs["pb_pctile_5y"] = pb_pct
        if pe_ttm is not None:
            obs["pe_ttm"] = pe_ttm
        if pb is not None:
            obs["pb"] = pb

    _, yfc = _get_source_block(research_bundle, "yfinance_consensus")
    snap_fund = (
        effective_snap.get("fundamentals")
        if isinstance(effective_snap, dict) and isinstance(effective_snap.get("fundamentals"), dict)
        else {}
    )
    if _is_usable_source(yfc):
        assert yfc is not None
        val_pos = yfc.get("valuation_and_positioning") if isinstance(yfc.get("valuation_and_positioning"), dict) else {}
        targets = yfc.get("analyst_price_targets") if isinstance(yfc.get("analyst_price_targets"), dict) else {}

        fwd_pe = _to_float(val_pos.get("forward_pe"))
        if fwd_pe is None:
            fwd_pe = _to_float(yfc.get("forward_pe"))
        if fwd_pe is None:
            fwd_pe = _to_float(snap_fund.get("forwardPE"))
        if fwd_pe is not None:
            obs["forward_pe"] = fwd_pe

        short_pct = _to_float(val_pos.get("short_percent_of_float"))
        if short_pct is None:
            short_pct = _to_float(val_pos.get("short_pct_float"))
        if short_pct is None:
            short_pct = _to_float(yfc.get("short_pct_float"))
        if short_pct is not None:
            obs["short_pct_float"] = short_pct

        tgt_mean = _to_float(targets.get("mean"))
        if tgt_mean is None:
            tgt_mean = _to_float(targets.get("target_mean_price"))
        if tgt_mean is None:
            tgt_mean = _to_float(yfc.get("target_mean_price"))
        if tgt_mean is None:
            tgt_mean = _to_float(snap_fund.get("targetMeanPrice"))
        if tgt_mean is not None:
            obs["target_mean_price"] = tgt_mean

        earn_raw = val_pos.get("next_earnings_date") or yfc.get("next_earnings_date")
        earn_dt = _parse_iso_date(earn_raw)
        if earn_dt is not None:
            obs["next_earnings_date"] = earn_dt.isoformat()
            obs["days_to_earnings"] = (earn_dt - anchor).days
    elif snap_fund:
        fwd_pe = _to_float(snap_fund.get("forwardPE"))
        if fwd_pe is not None:
            obs["forward_pe"] = fwd_pe
        tgt_mean = _to_float(snap_fund.get("targetMeanPrice"))
        if tgt_mean is not None:
            obs["target_mean_price"] = tgt_mean

    # --- Crypto ---
    _, etf = _get_source_block(research_bundle, "farside_btc_etf", "farside_eth_etf")
    if _is_usable_source(etf):
        assert etf is not None
        f5 = _to_float(etf.get("rolling_5d_usd_m"))
        if f5 is None:
            f5 = _to_float(etf.get("recent_total_net_flow_musd"))
        if f5 is None:
            f5 = _to_float(etf.get("etf_flow_5d_m"))
        f20 = _to_float(etf.get("rolling_20d_usd_m"))
        if f20 is None:
            f20 = _to_float(etf.get("etf_flow_20d_m"))
        if f5 is not None:
            obs["etf_flow_5d_m"] = f5
        if f20 is not None:
            obs["etf_flow_20d_m"] = f20

    _, stable = _get_source_block(research_bundle, "stablecoins_defillama", "stablecoins")
    if _is_usable_source(stable):
        assert stable is not None
        sc7 = _to_float(stable.get("total_change_7d_pct"))
        if sc7 is None:
            sc7 = _to_float(stable.get("stablecoin_chg_7d_pct"))
        if sc7 is not None:
            obs["stablecoin_chg_7d_pct"] = sc7

    _, perp_oi = _get_source_block(research_bundle, "perp_open_interest")
    if isinstance(perp_oi, dict) and _is_usable_source(perp_oi):
        direct_usd = _to_float(
            perp_oi.get("perp_oi_usd")
            or perp_oi.get("total_open_interest_usd")
            or perp_oi.get("open_interest_value_usd")
        )
        direct_contracts = _to_float(
            perp_oi.get("perp_oi_contracts") or perp_oi.get("open_interest_contracts")
        )
        if direct_contracts is None:
            direct_unit = str(perp_oi.get("amount_unit") or "").strip().lower()
            if direct_unit in {"", "contracts", "contract", "张"}:
                direct_contracts = _to_float(perp_oi.get("open_interest_amount"))

        if direct_usd is not None:
            obs["perp_oi_usd"] = _round_finite(direct_usd, 2)
        if direct_contracts is not None:
            obs["perp_oi_contracts"] = _round_finite(direct_contracts, 4)

        if direct_usd is None or direct_contracts is None:
            oi_usd_vals: list[float] = []
            oi_contract_vals: list[float] = []
            for ex_name, ex_data in perp_oi.items():
                if ex_name in {"status", "error", "reason"} or not isinstance(ex_data, dict):
                    continue
                if not _is_usable_source(ex_data) or ex_data.get("error"):
                    continue
                v_usd = _to_float(ex_data.get("open_interest_value_usd"))
                if v_usd is None:
                    v_usd = _to_float(ex_data.get("oi_usd"))
                if v_usd is not None:
                    oi_usd_vals.append(v_usd)
                v_ct = _to_float(ex_data.get("open_interest_contracts"))
                if v_ct is None:
                    unit = str(ex_data.get("amount_unit") or "").strip().lower()
                    if unit in {"", "contracts", "contract", "张"}:
                        v_ct = _to_float(ex_data.get("open_interest_amount"))
                if v_ct is not None:
                    oi_contract_vals.append(v_ct)
            if direct_usd is None and oi_usd_vals:
                obs["perp_oi_usd"] = _round_finite(sum(oi_usd_vals), 2)
            if direct_contracts is None and oi_contract_vals:
                obs["perp_oi_contracts"] = _round_finite(sum(oi_contract_vals), 4)

    perp_funding = None
    if isinstance(effective_snap, dict) and isinstance(effective_snap.get("crypto_perp_funding"), dict):
        perp_funding = effective_snap.get("crypto_perp_funding")
    else:
        _, perp_funding = _get_source_block(research_bundle, "crypto_perp_funding")
    if isinstance(perp_funding, dict) and _is_usable_source(perp_funding):
        rates: list[float] = []
        ex_errors = 0
        ex_count = 0
        direct_rate = _to_float(perp_funding.get("funding_annualized_pct"))
        if direct_rate is not None:
            rates.append(direct_rate)
        else:
            for ex_name, ex_data in perp_funding.items():
                if ex_name in {"status", "error", "reason"} or not isinstance(ex_data, dict):
                    continue
                ex_count += 1
                if ex_data.get("error"):
                    ex_errors += 1
                    continue
                r = _to_float(ex_data.get("funding_annualized_pct"))
                if r is not None:
                    rates.append(r)
        if rates:
            obs["perp_funding_ann_pct"] = _round_finite(float(statistics.median(rates)), 4)
        elif ex_count > 0 and ex_errors == ex_count:
            warn_msg = "crypto_perp_funding: status=error (all exchanges failed)"
            if warn_msg not in warnings:
                warnings.append(warn_msg)

    obs["data_health_warnings"] = warnings
    return obs


# ---------------------------------------------------------------------------
# 2. Chanlun, Futures Contract & Trigger Helpers for Holding Health Evaluation
# ---------------------------------------------------------------------------

_STRONG_MOMENTUM_LONG_TYPES = {"momentum_long", "capital_consensus_up", "non_divergence_up", "steep_non_div_up"}
_STRONG_MOMENTUM_SHORT_TYPES = {"momentum_short", "capital_consensus_down", "non_divergence_down", "steep_non_div_down"}
_WEAK_MOMENTUM_TYPES = {"weak_momentum_short", "weak_momentum_long"}
_UNCONFIRMED_EXACT_REGIMES = {
    "neutral",
    "insufficient_data",
    "single_stroke_no_comparison",
    "higher_low_pullback",
    "lower_high_rebound",
    "divergence_candidate_down",
    "divergence_candidate_up",
}


def _is_unconfirmed_or_neutral_regime(raw_regime: Any) -> bool:
    """Return True if a Chanlun momentum regime/verdict represents unconfirmed/neutral extension."""
    r = str(raw_regime or "").strip().lower()
    if not r:
        return False
    return r.startswith(("neutral_", "single_stroke", "weak")) or r in _UNCONFIRMED_EXACT_REGIMES


def _is_candidate_or_unconfirmed_signal_text(sig: str) -> bool:
    return any(
        tok in sig
        for tok in ("候选", "疑似", "准三买", "准三卖", "准买点", "准卖点", "蓄势", "观察", "待确认")
    )


def _extract_structured_signal_meta(
    cl_block: dict[str, Any] | None,
    summary_block: dict[str, Any] | None,
    dual_signal_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    for cand in (
        dual_signal_meta,
        summary_block.get("signal_meta") if isinstance(summary_block, dict) else None,
        cl_block.get("signal_meta") if isinstance(cl_block, dict) else None,
    ):
        if isinstance(cand, dict) and ("signal_type" in cand or "confirmed" in cand):
            return cand
    return None


def _classify_chanlun_signals(
    cl_block: dict[str, Any] | None,
    summary_block: dict[str, Any] | None,
    direction: str,
    signal_meta: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(strong_against_reasons, loosening_reasons)`` from a single timeframe's Chanlun output.

    Prefers structured ``signal_meta`` (``signal_type``, ``direction``, ``confirmed``,
    ``momentum_confirmed``, ``is_exhaustion``) when present. Only confirmed opposite structural
    signals or confirmed strong opposite capital consensus / non-divergence impulses qualify as
    ``strong_against``. Candidate signals (``一买候选``/``一卖候选``/``准三卖``/``疑似``),
    ``weak_momentum_*``, ``momentum_confirmed=False``, and ``neutral_*`` /
    ``single_stroke_no_comparison`` regimes NEVER produce ``strong_against`` (only ``loosening``),
    even when legacy signal text contains ``空头资金方向一致推进`` / ``多头资金方向一致推进``.
    """
    if not isinstance(cl_block, dict) or cl_block.get("error"):
        return [], []
    if direction not in {"long", "short"}:
        return [], []

    strong_against: list[str] = []
    loosening: list[str] = []

    opp_dir = "short" if direction == "long" else "long"
    opp_stroke_dir = "down" if direction == "long" else "up"
    our_stroke_dir = "up" if direction == "long" else "down"

    sigs = [str(s) for s in (cl_block.get("signals") or []) if s]
    summ_tier = str((summary_block or {}).get("tier") or "")
    summ_text = str((summary_block or {}).get("summary") or "")
    mom = cl_block.get("momentum_assessment") if isinstance(cl_block.get("momentum_assessment"), dict) else {}
    active_dir = str(mom.get("active_direction") or "").lower()
    verdict = str(mom.get("verdict") or "").strip().lower()
    raw_regime = str(mom.get("regime") or "").strip().lower()
    regimes_present = {r for r in (raw_regime, verdict) if r}
    has_unconfirmed_regime = any(_is_unconfirmed_or_neutral_regime(r) for r in regimes_present)
    is_cons = bool(mom.get("is_capital_consensus"))
    is_non_div = bool(mom.get("is_non_divergence_power"))
    is_div = bool(mom.get("is_divergence_candidate"))
    stroke_trig = cl_block.get("stroke_end_trigger") if isinstance(cl_block.get("stroke_end_trigger"), dict) else {}

    meta = _extract_structured_signal_meta(cl_block, summary_block, signal_meta)
    meta_mom_confirmed = meta.get("momentum_confirmed") if isinstance(meta, dict) else None
    meta_sig_type = str(meta.get("signal_type") or "").strip().lower() if isinstance(meta, dict) else ""
    momentum_unconfirmed = (
        has_unconfirmed_regime
        or (meta_mom_confirmed is False)
        or (meta_sig_type in _WEAK_MOMENTUM_TYPES)
    )

    # Strong momentum / non-divergence impulse against holding direction ONLY when momentum is not unconfirmed/neutral
    if not momentum_unconfirmed:
        if (
            (is_cons and active_dir == opp_stroke_dir)
            or verdict == f"capital_consensus_{opp_stroke_dir}"
            or raw_regime in {f"capital_consensus_{opp_stroke_dir}", f"strong_{opp_stroke_dir}_continuation"}
        ):
            strong_against.append("空头资金方向一致推进" if direction == "long" else "多头资金方向一致推进")
        if (
            (is_non_div and active_dir == opp_stroke_dir)
            or verdict == f"non_divergence_{opp_stroke_dir}"
            or raw_regime == f"non_divergence_{opp_stroke_dir}"
        ):
            strong_against.append("非背驰急跌（仍有空头力量）" if direction == "long" else "非背驰急拉（仍有多头力量）")

    if meta is not None:
        sig_type = meta_sig_type
        meta_dir = _normalize_direction(meta.get("direction"))
        confirmed = bool(meta.get("confirmed"))
        is_exhaustion = bool(meta.get("is_exhaustion"))

        is_strong_opp_momentum = (
            not momentum_unconfirmed
            and (
                (direction == "long" and (sig_type in _STRONG_MOMENTUM_SHORT_TYPES or (sig_type.startswith("momentum") and meta_dir == "short")))
                or (direction == "short" and (sig_type in _STRONG_MOMENTUM_LONG_TYPES or (sig_type.startswith("momentum") and meta_dir == "long")))
            )
        )

        if confirmed and meta_dir == opp_dir:
            strong_against.append(f"确认反向缠论信号({sig_type or 'confirmed'}, dir={meta_dir})")
        elif is_strong_opp_momentum:
            strong_against.append(f"反向资金方向一致推进({sig_type})")

        if not strong_against:
            if sig_type in _WEAK_MOMENTUM_TYPES and meta_dir == opp_dir:
                loosening.append(
                    f"反向动能未确认({sig_type}, regime={raw_regime or verdict or 'unconfirmed'})"
                )
            elif momentum_unconfirmed and meta_dir == opp_dir and (
                sig_type in (_STRONG_MOMENTUM_SHORT_TYPES | _STRONG_MOMENTUM_LONG_TYPES)
                or sig_type.startswith("momentum")
                or any(
                    ("空头资金方向一致" in s or "向下离开段·资金方向一致" in s)
                    if direction == "long"
                    else ("多头资金方向一致" in s or "向上离开段·资金方向一致" in s)
                    for s in sigs
                )
            ):
                loosening.append(
                    f"反向动能未确认({sig_type or 'momentum'}, regime={raw_regime or verdict or 'unconfirmed'})"
                )
            elif not confirmed and meta_dir == opp_dir:
                loosening.append(f"反向候选/未确认信号({sig_type or 'candidate'}, dir={meta_dir}, confirmed=False)")
            if is_exhaustion and meta_dir in {direction, None, "neutral"}:
                loosening.append(f"同向末端/背驰警戒({sig_type or 'exhaustion'})")
            if (is_div and active_dir == our_stroke_dir) or verdict == f"divergence_candidate_{our_stroke_dir}":
                loosening.append("上涨笔出现背驰观察/力度衰减" if direction == "long" else "下跌笔出现背驰观察/力度衰减")
            if stroke_trig.get("triggered") is True and str(stroke_trig.get("direction") or "").lower() == our_stroke_dir:
                default_desc = (
                    "触发第11课破反向阴线低点（上行笔或已结束）"
                    if direction == "long"
                    else "触发第11课破反向阳线高点（下行笔或已结束）"
                )
                loosening.append(stroke_trig.get("description") or default_desc)

        return list(dict.fromkeys(strong_against)), list(dict.fromkeys(loosening))

    # --- Legacy text fallback (strictly rejects 候选 / 疑似 / 准 as confirmed reversals) ---
    def _is_non_div_guard(sig: str) -> bool:
        return "非背驰" in sig or "勿猜1买" in sig or "勿猜1卖" in sig

    def _has_confirmed_buy(sig: str) -> bool:
        if _is_non_div_guard(sig) or _is_candidate_or_unconfirmed_signal_text(sig):
            return False
        if "三买确认" in sig or "2+3买确认" in sig:
            return True
        if ("一买" in sig or "二买" in sig) and any(tok in sig for tok in ("确认", "启动")):
            return True
        return (
            ("3出(D3" in sig or "4出(D4" in sig)
            and any(tok in sig for tok in ("确认", "启动"))
            and not any(neg in sig for neg in ("4出(D4)空", "顶降低", "二卖", "2+3卖", "空强多弱"))
        )

    def _has_confirmed_sell(sig: str) -> bool:
        if _is_non_div_guard(sig) or _is_candidate_or_unconfirmed_signal_text(sig):
            return False
        if "三卖确认" in sig or "2+3卖确认" in sig:
            return True
        if ("一卖" in sig or "二卖" in sig) and any(tok in sig for tok in ("确认", "启动")):
            return True
        return (
            ("3出(D3" in sig or "4出(D4" in sig)
            and any(tok in sig for tok in ("确认", "启动"))
            and any(neg in sig for neg in ("4出(D4)空", "顶降低", "二卖", "2+3卖", "空强多弱"))
        )

    if direction == "long":
        for s in sigs:
            if _has_confirmed_sell(s):
                strong_against.append(s)

        if not strong_against:
            if has_unconfirmed_regime and (
                any(("空头资金方向一致" in s or "向下离开段·资金方向一致" in s) for s in sigs)
                or "空头资金方向一致" in summ_text
            ):
                loosening.append(
                    f"反向动能未确认(regime={raw_regime or verdict or 'unconfirmed'}，不作确认反转)"
                )
            if (is_div and active_dir == "up") or verdict == "divergence_candidate_up":
                loosening.append("上涨笔出现背驰观察/力度衰减")
            for s in sigs:
                if _is_non_div_guard(s):
                    continue
                if any(tok in s for tok in ("准三卖", "准卖点", "上涨力度衰减", "顶分型承压")) or (
                    _is_candidate_or_unconfirmed_signal_text(s)
                    and any(tok in s for tok in ("一卖", "二卖", "三卖", "卖点", "顶分型"))
                ):
                    loosening.append(s)
                elif "非首中枢" in s and "三买" in s:
                    loosening.append(f"多头处于非首中枢后段({s})")
            if ("准卖点" in summ_text or ("背驰观察" in summ_tier and active_dir == "up")) and not loosening:
                loosening.append(summ_text or summ_tier)
            if stroke_trig.get("triggered") is True and str(stroke_trig.get("direction") or "").lower() == "up":
                loosening.append(stroke_trig.get("description") or "触发第11课破反向阴线低点（上行笔或已结束）")

    elif direction == "short":
        for s in sigs:
            if _has_confirmed_buy(s):
                strong_against.append(s)

        if not strong_against:
            if has_unconfirmed_regime and (
                any(("多头资金方向一致" in s or "向上离开段·资金方向一致" in s) for s in sigs)
                or "多头资金方向一致" in summ_text
            ):
                loosening.append(
                    f"反向动能未确认(regime={raw_regime or verdict or 'unconfirmed'}，不作确认反转)"
                )
            if (is_div and active_dir == "down") or verdict == "divergence_candidate_down":
                loosening.append("下跌笔出现背驰观察/力度衰减")
            for s in sigs:
                if _is_non_div_guard(s):
                    continue
                if any(tok in s for tok in ("准三买", "准买点", "下跌力度衰减", "底分型企稳")) or (
                    _is_candidate_or_unconfirmed_signal_text(s)
                    and any(tok in s for tok in ("一买", "二买", "三买", "买点", "底分型"))
                ):
                    loosening.append(s)
                elif "非首中枢" in s and "三卖" in s:
                    loosening.append(f"空头处于非首中枢后段({s})")
            if ("准买点" in summ_text or ("背驰观察" in summ_tier and active_dir == "down")) and not loosening:
                loosening.append(summ_text or summ_tier)
            if stroke_trig.get("triggered") is True and str(stroke_trig.get("direction") or "").lower() == "down":
                loosening.append(stroke_trig.get("description") or "触发第11课破反向阳线高点（下行笔或已结束）")

    return list(dict.fromkeys(strong_against)), list(dict.fromkeys(loosening))


def _date_range_inclusive(start: date, end: date) -> set[date]:
    days: set[date] = set()
    cur = start
    while cur <= end:
        days.add(cur)
        cur += timedelta(days=1)
    return days


# Deterministic China statutory exchange closures (2025-2028) covering New Year, Spring Festival,
# Tomb Sweeping (Qingming), Labor Day (May 1-5), Dragon Boat, Mid-Autumn, and National Day (Oct 1-7/8).
# Chinese futures exchanges are strictly closed on weekends (no weekend make-up trading days).
_CN_STATUTORY_HOLIDAY_RANGES: dict[int, tuple[tuple[tuple[int, int], tuple[int, int]], ...]] = {
    2025: (
        ((1, 1), (1, 1)),      # New Year's Day
        ((1, 28), (2, 4)),     # Spring Festival
        ((4, 4), (4, 6)),      # Tomb Sweeping
        ((5, 1), (5, 5)),      # Labor Day
        ((5, 31), (6, 2)),     # Dragon Boat
        ((10, 1), (10, 8)),    # National Day & Mid-Autumn
    ),
    2026: (
        ((1, 1), (1, 3)),      # New Year's Day
        ((2, 15), (2, 23)),    # Spring Festival
        ((4, 4), (4, 6)),      # Tomb Sweeping
        ((5, 1), (5, 5)),      # Labor Day
        ((6, 19), (6, 21)),    # Dragon Boat
        ((9, 25), (9, 27)),    # Mid-Autumn
        ((10, 1), (10, 7)),    # National Day (market resumes 2026-10-08)
    ),
    2027: (
        ((1, 1), (1, 3)),      # New Year's Day
        ((2, 5), (2, 12)),     # Spring Festival
        ((4, 3), (4, 5)),      # Tomb Sweeping
        ((5, 1), (5, 5)),      # Labor Day
        ((6, 9), (6, 9)),      # Dragon Boat (2027 State Council: June 9 only)
        ((9, 15), (9, 17)),    # Mid-Autumn
        ((10, 1), (10, 7)),    # National Day
    ),
    2028: (
        ((1, 1), (1, 3)),      # New Year's Day
        ((1, 25), (2, 1)),     # Spring Festival
        ((4, 2), (4, 4)),      # Tomb Sweeping
        ((5, 1), (5, 5)),      # Labor Day
        ((5, 27), (5, 29)),    # Dragon Boat
        ((10, 1), (10, 8)),    # National Day & Mid-Autumn
    ),
}


def _cn_exchange_holidays(year: int) -> set[date]:
    """Return statutory closure dates for Chinese futures exchanges in ``year``."""
    ranges = _CN_STATUTORY_HOLIDAY_RANGES.get(year)
    holidays: set[date] = set()
    if ranges is not None:
        for (sm, sd), (em, ed) in ranges:
            holidays |= _date_range_inclusive(date(year, sm, sd), date(year, em, ed))
        return holidays
    # Fixed-date statutory closures fallback for years outside 2025-2028
    holidays.add(date(year, 1, 1))
    holidays |= _date_range_inclusive(date(year, 5, 1), date(year, 5, 5))
    holidays |= _date_range_inclusive(date(year, 10, 1), date(year, 10, 7))
    return holidays


def _is_cn_trading_day(d: date) -> bool:
    """Return True iff ``d`` is a Mon-Fri weekday and not a China statutory exchange closure."""
    return d.weekday() < 5 and d not in _cn_exchange_holidays(d.year)


def _nth_weekday_of_month(year: int, month: int, n: int) -> date:
    """Return the n-th weekday (Mon-Fri) of ``(year, month)`` (positive from start, negative from end)."""
    last_day = cal_lib.monthrange(year, month)[1]
    weekdays = [date(year, month, day) for day in range(1, last_day + 1) if date(year, month, day).weekday() < 5]
    if n > 0:
        return weekdays[min(n - 1, len(weekdays) - 1)]
    return weekdays[max(0, len(weekdays) + n)]


def _nth_cn_trading_day_of_month(year: int, month: int, n: int) -> date:
    """Return the n-th China futures trading day of ``(year, month)`` (positive from start, negative from end)."""
    last_day = cal_lib.monthrange(year, month)[1]
    tdays = [
        date(year, month, day)
        for day in range(1, last_day + 1)
        if _is_cn_trading_day(date(year, month, day))
    ]
    if not tdays:
        return _nth_weekday_of_month(year, month, n)
    if n > 0:
        return tdays[min(n - 1, len(tdays) - 1)]
    return tdays[max(0, len(tdays) + n)]


def _estimate_cn_future_ltd(root: str, year: int, month: int) -> date | None:
    """Exact Chinese futures exchange Last Trading Day rules with statutory holiday calendar.

    - SHFE (CU, AL, ZN, NI, SN, AU, AG, RB, HC, RU, ...): 15th of delivery month (roll forward to next CN trading day)
    - INE SC: last CN trading day of month BEFORE delivery month
    - DCE LH (Live Hog): 4th-to-last CN trading day of delivery month (交割月倒数第 4 个交易日)
    - DCE (other) / CZCE / GFEX (LC, SI, PS): 10th CN trading day of delivery month
    - INE EC: last Monday of delivery month (roll forward to next CN trading day if statutory holiday)
    """
    r = (root or "").strip().upper()
    if not (1 <= month <= 12):
        return None

    if r in SHFE_15TH_ROOTS:
        d = date(year, month, 15)
        while not _is_cn_trading_day(d):
            d += timedelta(days=1)
        return d

    if r in INE_PREV_MONTH_ROOTS:
        py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
        return _nth_cn_trading_day_of_month(py, pm, -1)

    if r in DCE_4TH_LAST_BDAY_ROOTS:
        return _nth_cn_trading_day_of_month(year, month, -4)

    if r in (DCE_10TH_BDAY_ROOTS | CZCE_10TH_BDAY_ROOTS | GFEX_10TH_BDAY_ROOTS):
        return _nth_cn_trading_day_of_month(year, month, 10)

    if r in INE_EC_ROOTS:
        last_day = cal_lib.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() != 0:  # Monday == 0
            d -= timedelta(days=1)
        while not _is_cn_trading_day(d):
            d += timedelta(days=1)
        return d

    return None


def _estimate_futures_ltd(root: str, year: int, month: int, *, prefer_cn: bool = False) -> date | None:
    """Resolve Last Trading Day for CN or overseas futures roots."""
    r = (root or "").strip().upper()
    if prefer_cn:
        cn_ltd = _estimate_cn_future_ltd(r, year, month)
        if cn_ltd is not None:
            return cn_ltd
    if r in {"ALI", "PL", "PA"}:
        # COMEX/NYMEX precious & aluminum futures: 3rd-to-last US business day of delivery month
        if hasattr(rext, "_nth_business_day_of_month") and hasattr(rext, "_us_exchange_holidays"):
            return rext._nth_business_day_of_month(year, month, -3, rext._us_exchange_holidays(year))
        return _nth_weekday_of_month(year, month, -3)
    if r in KNOWN_OVERSEAS_FUTURES_ROOTS:
        return rext.estimate_contract_last_trading_day(r, year, month)
    cn_ltd = _estimate_cn_future_ltd(r, year, month)
    if cn_ltd is not None:
        return cn_ltd
    return None


def _extract_futures_contracts(text: str | None, ref_date: date) -> list[dict[str, Any]]:
    """Extract recognizable dated futures contract codes (1-3 letter roots) from expression text."""
    if not text or not isinstance(text, str):
        return []
    upper = text.upper()
    found: dict[str, dict[str, Any]] = {}

    for m in OVERSEAS_CONTRACT_RE.finditer(upper):
        root, mcode, yy_str = m.group(1), m.group(2), m.group(3)
        if root not in KNOWN_OVERSEAS_FUTURES_ROOTS:
            continue
        month = MONTH_CODE_TO_NUM.get(mcode)
        if not month:
            continue
        year = 2000 + int(yy_str)
        code = m.group(0)
        ltd = _estimate_futures_ltd(root, year, month, prefer_cn=False)
        if ltd is None:
            continue
        found[code] = {
            "contract": code,
            "root": root,
            "year": year,
            "month": month,
            "market_kind": "overseas",
            "est_last_trading_day": ltd,
        }

    for m in CN_CONTRACT_4D_RE.finditer(upper):
        root, yymm = m.group(1), m.group(2)
        if root not in KNOWN_CN_FUTURES_ROOTS:
            continue
        yy, mm = int(yymm[:2]), int(yymm[2:])
        if not (1 <= mm <= 12):
            continue
        year = 2000 + yy
        code = m.group(0)
        ltd = _estimate_futures_ltd(root, year, mm, prefer_cn=True)
        if ltd is None:
            continue
        found[code] = {
            "contract": code,
            "root": root,
            "year": year,
            "month": mm,
            "market_kind": "cn",
            "est_last_trading_day": ltd,
        }

    for m in CN_CONTRACT_3D_RE.finditer(upper):
        root, ymm = m.group(1), m.group(2)
        if root not in CZCE_3D_ROOTS:
            continue
        y_digit, mm = int(ymm[0]), int(ymm[1:])
        if not (1 <= mm <= 12):
            continue
        decade = (ref_date.year // 10) * 10
        candidates = [decade - 10 + y_digit, decade + y_digit, decade + 10 + y_digit]
        year = min(candidates, key=lambda y: (abs(y - ref_date.year), -y))
        code = m.group(0)
        ltd = _estimate_futures_ltd(root, year, mm, prefer_cn=True)
        if ltd is None:
            continue
        found[code] = {
            "contract": code,
            "root": root,
            "year": year,
            "month": mm,
            "market_kind": "cn",
            "est_last_trading_day": ltd,
        }

    return list(found.values())


def _looks_like_futures_contract(code: str | None, asset_class: str | None = None) -> bool:
    """Return True if ``code`` appears to denote a futures contract (has digits or ``=F``) rather than equity/FX/crypto."""
    if not code or not isinstance(code, str):
        return False
    s = code.strip().upper()
    if not s:
        return False
    if re.fullmatch(r"\d{6}(?:\.(?:SS|SZ|BJ))?", s):
        return False
    if s.endswith(("=X", "-USD")) or "/" in s:
        return False
    if s.endswith("=F"):
        return True
    if any(s.endswith(suf) for suf in FUTURES_EXCHANGE_SUFFIXES):
        return True
    if asset_class in {"commodity", "cn_future"} and any(ch.isdigit() for ch in s):
        return True
    return bool(
        asset_class not in {"us_stock_or_etf", "a_share", "fx", "crypto"}
        and (
            re.fullmatch(r"[A-Z]{1,4}\d{3,4}", s)
            or re.fullmatch(r"[A-Z]{1,4}[FGHJKMNQUVXZ]\d{2}(?:\.[A-Z]{2,4})?", s)
        )
    )


def _continuous_futures_root(code: str | None) -> str | None:
    if not code or not isinstance(code, str):
        return None
    s = code.strip().upper()
    if s.endswith("=F") and len(s) > 2:
        root = s[:-2]
        return root if root.isalpha() else None
    m = re.fullmatch(r"([A-Z]{1,3})0", s)
    return m.group(1) if m else None


def _is_continuous_futures_code(code: str | None, asset_class: str | None = None) -> bool:
    """Return True if ``code`` represents a continuous futures symbol (e.g. ``NI0``, ``CL=F``)."""
    del asset_class
    return _continuous_futures_root(code) is not None


def _infer_dominant_contract_from_bundle(
    raw_contract_sym: str,
    research_bundle: dict[str, Any] | None,
    ref_date: date,
) -> dict[str, Any] | None:
    """Infer reference active dominant dated contract from fresh ``term_structure`` for the same commodity root.

    Strictly filters out:
    - stale term-structure quotes (>7 days old or >1 day in the future),
    - contracts belonging to a different commodity root,
    - already-expired contracts (est_last_trading_day < ref_date),
    - rows without positive open_interest or volume (never blindly picks row 0 with 0 OI/vol).
    """
    if not isinstance(research_bundle, dict):
        return None
    target_root = _continuous_futures_root(raw_contract_sym)
    if not target_root:
        return None
    _, ts = _get_source_block(research_bundle, "term_structure")
    if not _is_usable_source(ts) or not isinstance(ts, dict):
        return None

    raw_qd = ts.get("quote_date")
    if not isinstance(raw_qd, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_qd.strip()):
        return None
    try:
        quote_date = date.fromisoformat(raw_qd.strip())
    except ValueError:
        return None
    age = (ref_date - quote_date).days
    if age > 7 or age < -1:
        return None

    def _valid_candidate(sym_str: str) -> dict[str, Any] | None:
        parsed = _extract_futures_contracts(sym_str, ref_date=ref_date)
        for info in parsed:
            if str(info.get("root") or "").upper() != target_root:
                continue
            ltd = info.get("est_last_trading_day")
            if isinstance(ltd, date) and ltd >= ref_date:
                return info
        return None

    best_by_oi: tuple[tuple[float, float], dict[str, Any]] | None = None
    best_by_vol: tuple[float, dict[str, Any]] | None = None
    raw_contracts = ts.get("contracts")
    if isinstance(raw_contracts, list):
        for row in raw_contracts:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or row.get("contract") or "").strip()
            if not sym:
                continue
            cinfo = _valid_candidate(sym)
            if cinfo is None:
                continue
            oi = _to_float(row.get("open_interest")) or 0.0
            vol = _to_float(row.get("volume")) or 0.0
            if oi > 0.0:
                rank = (oi, vol)
                if best_by_oi is None or rank > best_by_oi[0]:
                    best_by_oi = (
                        rank,
                        {**cinfo, "inferred_from": raw_contract_sym, "selection_basis": "open_interest"},
                    )
            elif vol > 0.0:
                if best_by_vol is None or vol > best_by_vol[0]:
                    best_by_vol = (
                        vol,
                        {**cinfo, "inferred_from": raw_contract_sym, "selection_basis": "volume"},
                    )
    if best_by_oi is not None:
        return best_by_oi[1]
    if best_by_vol is not None:
        return best_by_vol[1]
    return None


def _resolve_traded_contract(
    norm_symbol: str,
    position: dict[str, Any] | None,
    archive_state: dict[str, Any] | None,
    ref_date: date,
) -> str | None:
    """Return actual traded contract code if distinct from ``norm_symbol``, else None."""
    pos = position if isinstance(position, dict) else {}
    state = archive_state if isinstance(archive_state, dict) else {}
    vb = state.get("valuation_or_balance") if isinstance(state.get("valuation_or_balance"), dict) else {}
    base_up = norm_symbol.strip().upper()

    explicit_cs = str(pos.get("contract_symbol") or vb.get("contract_symbol") or "").strip().upper()
    if explicit_cs and explicit_cs != base_up:
        return explicit_cs

    expr = str(pos.get("contract_or_expression") or vb.get("contract_or_expression") or "").strip()
    if expr:
        contracts = _extract_futures_contracts(expr, ref_date=ref_date)
        if contracts:
            cand = str(contracts[0]["contract"]).strip().upper()
            if cand and cand != base_up:
                return cand
    return None


def _contract_symbols_match(expected: str, actual: str | None, ref_date: date) -> bool:
    """Strict symbol match for traded futures contracts (never matches continuous CU0/CL=F to dated contract)."""
    if not expected or not actual:
        return False
    e = expected.strip().upper()
    a = actual.strip().upper()
    if e == a:
        return True

    # Strip optional exchange suffix ONLY when one side omitted it (never match differing suffixes)
    e_parts = e.split(".", 1)
    a_parts = a.split(".", 1)
    if len(e_parts) == 2 and len(a_parts) == 2 and e_parts[1] != a_parts[1]:
        return False
    if e_parts[0] == a_parts[0] and (len(e_parts) == 1 or len(a_parts) == 1):
        return True

    # Handle equivalent CZCE 3-digit vs 4-digit contract notation (e.g. TA705 == TA2705)
    ec = _extract_futures_contracts(e, ref_date=ref_date)
    ac = _extract_futures_contracts(a, ref_date=ref_date)
    if len(ec) == 1 and len(ac) == 1:
        c1, c2 = ec[0], ac[0]
        if (
            c1["root"] == c2["root"]
            and c1["year"] == c2["year"]
            and c1["month"] == c2["month"]
            and c1["market_kind"] == c2["market_kind"]
        ):
            return True
    return False


def _is_trigger_armed(trig: dict[str, Any]) -> bool:
    if not isinstance(trig, dict):
        return False
    if trig.get("enabled") is False:
        return False
    st = str(trig.get("status") or "armed").strip().lower()
    return st not in DISARMED_TRIGGER_STATUSES


def _is_red_trigger(trig_result: dict[str, Any]) -> bool:
    """Determine if a fired trigger escalates to RED_EXIT.

    Explicit ``severity`` ALWAYS takes precedence. When absent, negation patterns
    (e.g. ``维持原止损，不平仓``) are checked BEFORE exit keywords / trigger kinds.
    """
    sev = str(trig_result.get("severity") or "").strip().lower()
    if sev == "red":
        return True
    if sev in {"yellow", "info", "warn", "warning"}:
        return False

    action = str(trig_result.get("action") or "").strip().lower()
    if action and any(pat in action for pat in NON_EXIT_ACTION_PATTERNS):
        return False

    kind = str(trig_result.get("kind") or "").strip().lower()
    if kind in RED_TRIGGER_KINDS:
        return True
    return any(kw in action for kw in EXIT_ACTION_KEYWORDS)


def _resolve_effective_stop_ts(
    opened_at_utc: Any,
    stop_updated_at_utc: Any,
    asset_class: str | None = None,
) -> tuple[datetime | None, Any]:
    """Return ``(effective_dt_utc, raw_effective_value)`` as ``max(opened_at, stop_updated_at)``."""
    o_dt, _, _, _ = _parse_bar_timestamp(opened_at_utc, asset_class=asset_class)
    u_dt, _, _, _ = _parse_bar_timestamp(stop_updated_at_utc, asset_class=asset_class)
    if o_dt is not None and u_dt is not None:
        if u_dt >= o_dt:
            return u_dt, stop_updated_at_utc
        return o_dt, opened_at_utc
    if u_dt is not None:
        return u_dt, stop_updated_at_utc
    return o_dt, opened_at_utc


def _parse_intraday_bar_duration_minutes(bar_interval: Any) -> int:
    s = str(bar_interval or "30m").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mh])", s)
    if not m:
        return 30
    qty = int(m.group(1))
    return qty * 60 if m.group(2) == "h" else qty


def _is_known_exchange_intraday_break(
    prev_start_utc: datetime,
    curr_start_utc: datetime,
    bar_dur: timedelta,
    asset_class: str | None,
) -> bool:
    """Return True iff the gap between two same-day intraday bars matches an official exchange break.

    Excludes official scheduled breaks only:
      - ``a_share`` (Shanghai): lunch 11:30 -> 13:00
      - ``cn_future`` (Shanghai): morning break (10:15 -> 10:30 or Sina merged 10:00 -> 10:45),
        lunch break (11:30 -> 13:30 or Sina merged 11:15 -> 13:45),
        night-to-day (00:00/00:30/01:00/02:00/02:30 -> 09:00), day-to-night (15:00 -> 21:00)
      - ``commodity`` / ``fx`` (New York): CME daily maintenance 17:00 -> 18:00 ET
    """
    ac_norm = (asset_class or "").strip().lower()
    if ac_norm in {"a_share", "cn_future"}:
        sh_tz = ZoneInfo("Asia/Shanghai")
        prev_s = prev_start_utc.astimezone(sh_tz)
        prev_e = (prev_start_utc + bar_dur).astimezone(sh_tz)
        curr_s = curr_start_utc.astimezone(sh_tz)
        if prev_s.date() != curr_s.date():
            return False
        prev_s_hm = (prev_s.hour, prev_s.minute)
        prev_e_hm = (prev_e.hour, prev_e.minute)
        curr_s_hm = (curr_s.hour, curr_s.minute)
        if ac_norm == "a_share":
            return curr_s_hm == (13, 0) and (prev_e_hm == (11, 30) or prev_s_hm == (11, 30))
        # cn_future: support both standard exchange clocks and Sina bar-end merged-stub timestamps
        # Morning 10:15-10:30 break (standard 10:15->10:30, or Sina 10:00 end -> 10:45 end i.e. 10:15 start)
        if (curr_s_hm == (10, 30) and (prev_e_hm == (10, 15) or prev_s_hm == (10, 15))) or (
            curr_s_hm == (10, 15) and (prev_e_hm == (10, 0) or prev_s_hm == (10, 0))
        ):
            return True
        # Lunch 11:30-13:30 break (standard 11:30->13:30, or Sina 11:15 end -> 13:45 end i.e. 13:15 start)
        if curr_s_hm in {(13, 15), (13, 30)} and (
            prev_e_hm in {(11, 15), (11, 30)} or prev_s_hm in {(11, 15), (11, 30)}
        ):
            return True
        # Night-to-day break (SHFE/INE/DCE/CZCE night close -> 09:00 day open)
        if curr_s_hm == (9, 0) and (
            prev_e_hm in {(0, 0), (0, 30), (1, 0), (2, 0), (2, 30)}
            or prev_s_hm in {(0, 0), (0, 30), (1, 0), (2, 0), (2, 30)}
        ):
            return True
        # Day-to-night break (15:00 day close -> 21:00 night open)
        if curr_s_hm == (21, 0) and (prev_e_hm == (15, 0) or prev_s_hm == (15, 0)):
            return True
        return False

    if ac_norm in {"commodity", "fx"}:
        ny_tz = ZoneInfo("America/New_York")
        prev_s = prev_start_utc.astimezone(ny_tz)
        prev_e = (prev_start_utc + bar_dur).astimezone(ny_tz)
        curr_s = curr_start_utc.astimezone(ny_tz)
        if prev_s.date() != curr_s.date():
            return False
        prev_e_hm = (prev_e.hour, prev_e.minute)
        curr_s_hm = (curr_s.hour, curr_s.minute)
        return curr_s_hm == (18, 0) and prev_e_hm == (17, 0)

    return False


def _collect_eligible_ohlc_extrema(
    snapshot: dict[str, Any] | None,
    opened_at_utc: Any = None,
    *,
    effective_ts_utc: Any = None,
    asset_class: str | None = None,
    now_utc: datetime | None = None,
) -> tuple[
    float | None,
    dict[str, Any] | None,
    float | None,
    dict[str, Any] | None,
    str | None,
    list[dict[str, Any]],
]:
    """Inspect ``last_bar_low``/``last_bar_high`` and ``recent_bars_ohlc`` on both 30m and daily snapshots.

    Uses ``effective_ts_utc`` (falling back to ``opened_at_utc``) so trailing stop updates never
    retroactively evaluate historical bars before ``stop_updated_at_utc``:
      - Future intraday bars (``bar_dt > now_utc + 5m``) are excluded so corrupted future timestamps
        never trigger false stop breaches.
      - Daily OHLC bars are included ONLY when their trading date is strictly AFTER ``effective_ts``'s
        calendar date (``bar_date > effective_date``); same-day entries rely on intraday ``recent_bars_ohlc``.
      - Intraday 30m bars are included when their bar start timestamp is ``>= effective_ts`` (handling
        ``bar_utc``, exchange-localized naive timestamps, and ``bar_timestamp_anchor == "end"``).
      - Straddling bars (``bar_start < effective_ts < bar_end``) are separated into ``straddle_bars``
        because intra-bar extreme ordering relative to ``effective_ts`` is ambiguous.
      - Coverage & internal gap guard: checks both earliest visible bar coverage on the same day AND
        internal same-session missing bars (excluding official CN exchange intra-day breaks).

    Returns ``(min_low, min_low_meta, max_high, max_high_meta, gap_detail, straddle_bars)``.
    """
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        return None, None, None, None, None, []

    eff_ac = asset_class or snapshot.get("asset_class")
    ex_tz = _default_tz_for_asset_class(eff_ac)

    eff_now: datetime | None = None
    if now_utc is not None:
        eff_now = (
            now_utc.astimezone(timezone.utc)
            if now_utc.tzinfo is not None
            else now_utc.replace(tzinfo=timezone.utc)
        )

    def _is_future_dt(dt_utc: datetime | None) -> bool:
        return eff_now is not None and dt_utc is not None and dt_utc > eff_now + timedelta(minutes=5)

    raw_eff = effective_ts_utc if effective_ts_utc is not None else opened_at_utc
    eff_dt_utc, eff_local_d, eff_has_time, _ = _parse_bar_timestamp(raw_eff, asset_class=eff_ac)
    eff_exchange_date: date | None = (
        eff_dt_utc.astimezone(ex_tz).date()
        if (eff_dt_utc is not None and eff_has_time)
        else eff_local_d
    )

    min_low: float | None = None
    min_low_meta: dict[str, Any] | None = None
    max_high: float | None = None
    max_high_meta: dict[str, Any] | None = None
    gap_details: list[str] = []
    straddle_bars: list[dict[str, Any]] = []
    seen_straddle_keys: set[tuple[str, float | None, float | None]] = set()

    def _consider(low_val: float | None, high_val: float | None, bar_label: str, tf_label: str) -> None:
        nonlocal min_low, min_low_meta, max_high, max_high_meta
        if low_val is not None and (min_low is None or low_val < min_low):
            min_low = low_val
            min_low_meta = {"bar": bar_label, "tf": tf_label, "low": low_val}
        if high_val is not None and (max_high is None or high_val > max_high):
            max_high = high_val
            max_high_meta = {"bar": bar_label, "tf": tf_label, "high": high_val}

    def _record_straddle(
        low_val: float | None,
        high_val: float | None,
        bar_label: str,
        tf_label: str,
        bar_start: datetime,
        bar_end: datetime,
    ) -> None:
        key = (bar_start.isoformat(), low_val, high_val)
        if key in seen_straddle_keys:
            return
        seen_straddle_keys.add(key)
        straddle_bars.append({
            "bar": bar_label,
            "tf": tf_label,
            "low": low_val,
            "high": high_val,
            "bar_start_utc": bar_start.isoformat(),
            "bar_end_utc": bar_end.isoformat(),
        })

    # --- 1. Intraday 30m OHLC ---
    intra_tech = snapshot.get("intraday_technicals")
    if isinstance(intra_tech, dict):
        anchor_mode = str(intra_tech.get("bar_timestamp_anchor") or "start").strip().lower()
        bar_dur = timedelta(minutes=_parse_intraday_bar_duration_minutes(intra_tech.get("bar_interval")))
        tech_dt_utc, tech_local_d, _, _, tech_label = _extract_tech_timestamp(intra_tech, asset_class=eff_ac)

        def _bar_start_utc(bar_dt: datetime) -> datetime:
            return (bar_dt - bar_dur) if anchor_mode == "end" else bar_dt

        def _bar_end_utc(bar_dt: datetime) -> datetime:
            return bar_dt if anchor_mode == "end" else (bar_dt + bar_dur)

        # 1a. Latest bar low/high (exclude corrupted future-timestamp bars; prefer raw un-repaired extrema)
        recent_intra = intra_tech.get("recent_bars_ohlc")
        last_raw_override = (
            recent_intra[-1]
            if isinstance(recent_intra, list) and recent_intra and isinstance(recent_intra[-1], dict)
            else None
        )
        last_low = (
            _to_float(last_raw_override.get("raw_low"))
            if last_raw_override is not None and last_raw_override.get("raw_low") is not None
            else _to_float(intra_tech.get("last_bar_low"))
        )
        last_high = (
            _to_float(last_raw_override.get("raw_high"))
            if last_raw_override is not None and last_raw_override.get("raw_high") is not None
            else _to_float(intra_tech.get("last_bar_high"))
        )
        if (last_low is not None or last_high is not None) and not _is_future_dt(tech_dt_utc):
            if eff_dt_utc is not None and tech_dt_utc is not None:
                t_start = _bar_start_utc(tech_dt_utc)
                t_end = _bar_end_utc(tech_dt_utc)
                if t_start < eff_dt_utc < t_end:
                    _record_straddle(last_low, last_high, tech_label or "latest_bar", "intraday_30m", t_start, t_end)
                elif t_start >= eff_dt_utc:
                    _consider(last_low, last_high, tech_label or "latest_bar", "intraday_30m")
            else:
                _consider(last_low, last_high, tech_label or "latest_bar", "intraday_30m")

        # 1b. recent_bars_ohlc (up to 64 bars; exclude corrupted future-timestamp bars)
        parsed_intra: list[tuple[int, dict[str, Any], datetime | None, date | None, str]] = []
        if isinstance(recent_intra, list):
            for idx, item in enumerate(recent_intra):
                if not isinstance(item, dict):
                    continue
                raw_bar_utc = item.get("bar_utc")
                raw_bar_local = item.get("bar")
                if raw_bar_utc is not None:
                    b_dt_utc, b_local_d, _, _ = _parse_bar_timestamp(raw_bar_utc, asset_class=eff_ac)
                else:
                    b_dt_utc, b_local_d, _, _ = _parse_bar_timestamp(raw_bar_local, asset_class=eff_ac)
                if raw_bar_local is not None:
                    _, b_local_only, _, _ = _parse_bar_timestamp(raw_bar_local, asset_class=eff_ac)
                    if b_local_only is not None:
                        b_local_d = b_local_only
                if _is_future_dt(b_dt_utc):
                    continue
                lbl = str(raw_bar_local or raw_bar_utc or f"bar_{idx}")
                parsed_intra.append((idx, item, b_dt_utc, b_local_d, lbl))

        intra_dts = [p[2] for p in parsed_intra if p[2] is not None]
        valid_tech_dt = tech_dt_utc if not _is_future_dt(tech_dt_utc) else None
        if valid_tech_dt is not None:
            latest_intra_dt = max([valid_tech_dt, *intra_dts]) if intra_dts else valid_tech_dt
        else:
            latest_intra_dt = max(intra_dts) if intra_dts else None

        session_date: date | None = tech_local_d if valid_tech_dt is not None else None
        if session_date is None:
            for _, _, _, ld, _ in reversed(parsed_intra):
                if ld is not None:
                    session_date = ld
                    break

        eligible_bar_starts: list[datetime] = []
        for pos_idx, (_, item, b_dt_utc, b_local_d, lbl) in enumerate(parsed_intra):
            b_low = _to_float(item.get("raw_low")) if item.get("raw_low") is not None else _to_float(item.get("low"))
            b_high = _to_float(item.get("raw_high")) if item.get("raw_high") is not None else _to_float(item.get("high"))
            if eff_dt_utc is not None:
                if b_dt_utc is None:
                    continue
                b_start = _bar_start_utc(b_dt_utc)
                b_end = _bar_end_utc(b_dt_utc)
                if b_end > eff_dt_utc:
                    eligible_bar_starts.append(b_start)
                if b_low is None and b_high is None:
                    continue
                if b_start < eff_dt_utc < b_end:
                    _record_straddle(b_low, b_high, lbl, "intraday_30m", b_start, b_end)
                    continue
                if b_start < eff_dt_utc:
                    continue
            else:
                in_session = (
                    (session_date is not None and b_local_d is not None and b_local_d == session_date)
                    or (
                        latest_intra_dt is not None
                        and b_dt_utc is not None
                        and 0.0 <= (latest_intra_dt - b_dt_utc).total_seconds() <= 18 * 3600
                    )
                    or (session_date is None and latest_intra_dt is None and pos_idx == len(parsed_intra) - 1)
                )
                if not in_session:
                    continue
                if b_dt_utc is not None:
                    eligible_bar_starts.append(_bar_start_utc(b_dt_utc))
                if b_low is None and b_high is None:
                    continue
            _consider(b_low, b_high, lbl, "intraday_30m")

        # 1c. Same-day coverage check when effective_ts is on the same calendar day as latest intraday bar
        if eff_dt_utc is not None and eff_has_time and latest_intra_dt is not None:
            latest_ex_date = latest_intra_dt.astimezone(ex_tz).date()
            same_calendar_day = (
                (eff_exchange_date is not None and eff_exchange_date == latest_ex_date)
                or (eff_dt_utc.date() == latest_intra_dt.date())
            )
            if same_calendar_day:
                if intra_dts:
                    earliest_cov_start = min(_bar_start_utc(dt) for dt in intra_dts)
                    if earliest_cov_start > eff_dt_utc:
                        gap_details.append(
                            f"earliest visible 30m bar {earliest_cov_start.isoformat()} "
                            f"> effective_ts {eff_dt_utc.isoformat()}"
                        )
                elif _bar_start_utc(latest_intra_dt) > eff_dt_utc:
                    gap_details.append(
                        f"recent_bars_ohlc 缺失且最新 30m K 线 {latest_intra_dt.isoformat()} "
                        f"> effective_ts {eff_dt_utc.isoformat()}"
                    )

        # 1d. Internal same-session missing bar gap detection (excluding official exchange breaks)
        sorted_starts = sorted(set(eligible_bar_starts))
        for prev_start, curr_start in zip(sorted_starts, sorted_starts[1:], strict=False):
            if prev_start.astimezone(ex_tz).date() != curr_start.astimezone(ex_tz).date():
                continue
            delta = curr_start - prev_start
            if delta > bar_dur:
                if _is_known_exchange_intraday_break(prev_start, curr_start, bar_dur, eff_ac):
                    continue
                gap_details.append(
                    f"intraday_internal_bar_gap: missing {bar_dur} bar between "
                    f"{prev_start.isoformat()} and {curr_start.isoformat()}"
                )
                break

        # 1e. Explicit dropped corrupt intraday bar gap markers
        dropped_intra = intra_tech.get("dropped_bar_keys")
        if isinstance(dropped_intra, list) and dropped_intra:
            eff_iso_cut = eff_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if eff_dt_utc is not None else ""
            post_eff_dropped = [str(k) for k in dropped_intra if str(k) >= eff_iso_cut]
            if post_eff_dropped:
                gap_details.append(f"intraday_internal_bar_gap: quarantined_corrupt_bars={','.join(post_eff_dropped[:5])}")

    # --- 2. Daily OHLC ---
    daily_tech = snapshot.get("technicals")
    if isinstance(daily_tech, dict):
        d_dt_utc, d_local_d, _, _, d_label = _extract_tech_timestamp(daily_tech, asset_class=eff_ac)
        d_bar_date = d_local_d or (d_dt_utc.astimezone(ex_tz).date() if d_dt_utc is not None else None)

        # 2a. Latest daily bar low/high (prefer raw un-repaired extrema so settlement O/C never fakes a wick stop)
        recent_daily = daily_tech.get("recent_bars_ohlc")
        last_daily_override = (
            recent_daily[-1]
            if isinstance(recent_daily, list) and recent_daily and isinstance(recent_daily[-1], dict)
            else None
        )
        last_low = (
            _to_float(last_daily_override.get("raw_low"))
            if last_daily_override is not None and last_daily_override.get("raw_low") is not None
            else _to_float(daily_tech.get("last_bar_low"))
        )
        last_high = (
            _to_float(last_daily_override.get("raw_high"))
            if last_daily_override is not None and last_daily_override.get("raw_high") is not None
            else _to_float(daily_tech.get("last_bar_high"))
        )
        if last_low is not None or last_high is not None:
            if eff_dt_utc is not None:
                eligible_daily_last = (
                    d_bar_date is not None
                    and eff_exchange_date is not None
                    and d_bar_date > eff_exchange_date
                )
            else:
                eligible_daily_last = True
            if eligible_daily_last:
                _consider(last_low, last_high, d_label or "latest_daily_bar", "daily")

        # 2b. Daily recent_bars_ohlc
        if isinstance(recent_daily, list) and recent_daily:
            daily_session_date: date | None = d_bar_date
            if daily_session_date is None:
                for item in reversed(recent_daily):
                    if isinstance(item, dict):
                        bdt, bld, _, _ = _parse_bar_timestamp(item.get("bar") or item.get("bar_utc"), asset_class=eff_ac)
                        daily_session_date = bld or (bdt.astimezone(ex_tz).date() if bdt is not None else None)
                        if daily_session_date is not None:
                            break

            for idx, item in enumerate(recent_daily):
                if not isinstance(item, dict):
                    continue
                b_low = _to_float(item.get("raw_low")) if item.get("raw_low") is not None else _to_float(item.get("low"))
                b_high = _to_float(item.get("raw_high")) if item.get("raw_high") is not None else _to_float(item.get("high"))
                if b_low is None and b_high is None:
                    continue
                raw_bar = item.get("bar") or item.get("bar_utc")
                b_dt_utc, b_local_d, _, _ = _parse_bar_timestamp(raw_bar, asset_class=eff_ac)
                b_bar_date = b_local_d or (b_dt_utc.astimezone(ex_tz).date() if b_dt_utc is not None else None)

                if eff_dt_utc is not None:
                    # Strictly AFTER effective_ts's calendar date; same-day relies on 30m intraday bars
                    if b_bar_date is None or eff_exchange_date is None or b_bar_date <= eff_exchange_date:
                        continue
                else:
                    if daily_session_date is not None and b_bar_date is not None:
                        if b_bar_date != daily_session_date:
                            continue
                    elif idx != len(recent_daily) - 1:
                        continue
                _consider(b_low, b_high, str(raw_bar or f"daily_bar_{idx}"), "daily")

        dropped_daily = daily_tech.get("dropped_bar_keys")
        if isinstance(dropped_daily, list) and dropped_daily:
            eff_d_cut = eff_exchange_date.isoformat() if eff_exchange_date is not None else ""
            post_d_dropped = [str(k) for k in dropped_daily if str(k) > eff_d_cut]
            if post_d_dropped:
                gap_details.append(f"daily_quarantined_corrupt_bars:{','.join(post_d_dropped[:5])}")

    gap_detail = "；".join(dict.fromkeys(gap_details)) if gap_details else None
    return min_low, min_low_meta, max_high, max_high_meta, gap_detail, straddle_bars


# ---------------------------------------------------------------------------
# 3. Core Holding Health Evaluator (100% Pure & Offline Testable)
# ---------------------------------------------------------------------------

def evaluate_holding_health(
    symbol: str,
    direction: str | None,
    position: dict[str, Any] | None = None,
    archive_state: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    research_bundle: dict[str, Any] | None = None,
    ref_date: date | None = None,
    event_window_days: int = 14,
    contract_snapshot: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
    allow_manual_red_lines: bool = False,
) -> dict[str, Any]:
    """Evaluate holding health across 5 defense layers and emit RED_EXIT / YELLOW_LOOSENING / GREEN_HOLD.

    Enforces strict positive preconditions for ``GREEN_HOLD`` so stale bars, failed 30m feeds,
    unevaluated triggers, manual-only governance (unless opted in), contract quote mismatches,
    or degraded upstream sources NEVER produce a false green.
    """
    eff_now_utc: datetime | None = None
    if now_utc is not None:
        eff_now_utc = (
            now_utc.astimezone(timezone.utc)
            if now_utc.tzinfo is not None
            else now_utc.replace(tzinfo=timezone.utc)
        )
    elif ref_date is None:
        eff_now_utc = datetime.now(timezone.utc)

    anchor = ref_date or (eff_now_utc.date() if eff_now_utc is not None else datetime.now(timezone.utc).date())
    pos = position if isinstance(position, dict) else {}
    state = archive_state if isinstance(archive_state, dict) else {}
    inst = state.get("instrument") if isinstance(state.get("instrument"), dict) else {}
    anchors = state.get("chanlun_anchors") if isinstance(state.get("chanlun_anchors"), dict) else {}
    vb = state.get("valuation_or_balance") if isinstance(state.get("valuation_or_balance"), dict) else {}

    norm_sym, detected_ac = _safe_normalize_symbol(symbol)
    asset_class = pos.get("asset_class") or inst.get("asset_class") or detected_ac

    base_snap = snapshot if isinstance(snapshot, dict) else (
        research_bundle.get("snapshot")
        if isinstance(research_bundle, dict) and isinstance(research_bundle.get("snapshot"), dict)
        else None
    )
    if not asset_class and isinstance(base_snap, dict):
        asset_class = base_snap.get("asset_class")
    if not asset_class and isinstance(contract_snapshot, dict):
        asset_class = contract_snapshot.get("asset_class")

    norm_dir = (
        _normalize_direction(direction)
        or _normalize_direction(pos.get("direction"))
        or _normalize_direction(state.get("direction_bias"))
    )

    red_alerts: list[dict[str, Any]] = []
    yellow_alerts: list[dict[str, Any]] = []
    info_notes: list[str] = []
    upcoming_events: list[dict[str, Any]] = []
    suggested_trail_stop: dict[str, Any] | None = None

    if norm_dir not in {"long", "short"}:
        yellow_alerts.append({
            "layer": "governance",
            "code": "unclear_direction",
            "message": "持仓方向不明确，无法精确判定多空防线",
            "detail": f"direction={direction!r}",
        })

    # ------------------------------------------------------------------
    # Actual Traded Contract Routing & Quote Verification
    # ------------------------------------------------------------------
    traded_contract_sym = _resolve_traded_contract(norm_sym, pos, state, ref_date=anchor)
    explicit_contract_sym = str(pos.get("contract_symbol") or vb.get("contract_symbol") or "").strip().upper() or None
    expected_contract = traded_contract_sym or explicit_contract_sym or norm_sym
    contract_quote_mismatch = False
    price_snap: dict[str, Any] | None
    chanlun_snap: dict[str, Any] | None

    quote_snapshot = contract_snapshot if isinstance(contract_snapshot, dict) else base_snap
    if traded_contract_sym:
        snap_sym = quote_snapshot.get("symbol") if isinstance(quote_snapshot, dict) else None
        snap_err = quote_snapshot.get("error") if isinstance(quote_snapshot, dict) else None
        if (
            not isinstance(quote_snapshot, dict)
            or snap_err
            or not _contract_symbols_match(expected_contract, str(snap_sym) if snap_sym else None, ref_date=anchor)
        ):
            contract_quote_mismatch = True
            price_snap = None
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "contract_quote_mismatch",
                "message": "实盘合约行情获取失败或与持仓合约不匹配，无法确认止损安全",
                "detail": (
                    f"持仓实盘合约={expected_contract}, 实际返回标的={snap_sym!r}"
                    + (f", error={snap_err}" if snap_err else "")
                ),
            })
        else:
            price_snap = quote_snapshot

        # Continuous/base snapshot carries 3y history for daily Chanlun direction when available & matching base symbol
        base_sym = base_snap.get("symbol") if isinstance(base_snap, dict) else None
        base_matches_norm = (
            base_sym is None or _contract_symbols_match(norm_sym, str(base_sym), ref_date=anchor)
        )
        if isinstance(base_snap, dict) and not base_snap.get("error") and base_matches_norm:
            chanlun_snap = base_snap
        else:
            chanlun_snap = price_snap if price_snap is not None else None
    elif isinstance(contract_snapshot, dict):
        snap_sym = contract_snapshot.get("symbol")
        snap_err = contract_snapshot.get("error")
        if snap_err or not _contract_symbols_match(expected_contract, str(snap_sym) if snap_sym else None, ref_date=anchor):
            contract_quote_mismatch = True
            price_snap = None
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "contract_quote_mismatch",
                "message": "实盘合约行情获取失败或与持仓合约不匹配，无法确认止损安全",
                "detail": f"持仓合约={expected_contract}, 实际返回标的={snap_sym!r}" + (f", error={snap_err}" if snap_err else ""),
            })
        else:
            price_snap = contract_snapshot
        base_sym = base_snap.get("symbol") if isinstance(base_snap, dict) else None
        base_matches_norm = (
            base_sym is None or _contract_symbols_match(norm_sym, str(base_sym), ref_date=anchor)
        )
        chanlun_snap = (
            base_snap
            if (isinstance(base_snap, dict) and not base_snap.get("error") and base_matches_norm)
            else price_snap
        )
    else:
        # ALWAYS verify quote_snapshot["symbol"] against expected_contract whenever quote_snapshot
        # is present — even when position["symbol"] == position["contract_symbol"]!
        if isinstance(base_snap, dict):
            snap_sym = base_snap.get("symbol")
            snap_err = base_snap.get("error")
            sym_mismatch = (
                (snap_sym is not None and not _contract_symbols_match(expected_contract, str(snap_sym), ref_date=anchor))
                or (explicit_contract_sym is not None and not snap_err and snap_sym is None)
            )
            explicit_contract_failed = (
                explicit_contract_sym is not None
                and bool(snap_err)
                and _looks_like_futures_contract(explicit_contract_sym, asset_class)
            )
            if sym_mismatch or explicit_contract_failed:
                contract_quote_mismatch = True
                price_snap = None
                chanlun_snap = None
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "contract_quote_mismatch",
                    "message": "实盘合约行情获取失败或与持仓合约不匹配，无法确认止损安全",
                    "detail": (
                        f"持仓实盘合约={expected_contract}, 实际返回标的={snap_sym!r}"
                        + (f", error={snap_err}" if snap_err else "")
                    ),
                })
            else:
                price_snap = base_snap
                chanlun_snap = base_snap
        else:
            price_snap = base_snap
            chanlun_snap = base_snap

    # Extract flattened live observations & data health warnings.
    # ZERO cross-contamination: when contract_quote_mismatch is True, withhold ALL price/stop/30m
    # observations from chanlun_snap and research_bundle["snapshot"] so triggers like `close <= 95`
    # evaluate as evaluated=False (YELLOW_LOOSENING) instead of false RED_EXIT from CU0's price!
    raw_obs = extract_sentinel_observations(
        research_bundle,
        chanlun_snap,
        ref_date=anchor,
        include_price_metrics=not contract_quote_mismatch,
        asset_class=asset_class,
        now_utc=eff_now_utc,
    )
    data_warnings: list[str] = list(raw_obs.get("data_health_warnings") or [])
    observations = {k: v for k, v in raw_obs.items() if k != "data_health_warnings"}

    if contract_quote_mismatch:
        for pk in _CONTRACT_PRICE_OBS_KEYS:
            observations.pop(pk, None)
        cm_warn = f"contract_quote: 持仓合约 {expected_contract} 行情缺失或标的不匹配"
        if cm_warn not in data_warnings:
            data_warnings.append(cm_warn)
    elif price_snap is not None and price_snap is not chanlun_snap:
        for cw in _collect_data_health_warnings(None, price_snap):
            if cw not in data_warnings:
                data_warnings.append(cw)

    last_price, price_source, px_warn = _select_latest_price(
        price_snap,
        asset_class=asset_class,
        now_utc=eff_now_utc,
        symbol=(price_snap.get("symbol") if isinstance(price_snap, dict) else None) or symbol,
    )
    if px_warn and px_warn not in data_warnings:
        data_warnings.append(px_warn)

    if not contract_quote_mismatch and price_snap is not None and price_snap is not chanlun_snap:
        if "close" in observations:
            observations["continuous_close"] = observations["close"]
        price_only_obs = extract_sentinel_observations(
            None,
            price_snap,
            ref_date=anchor,
            include_price_metrics=True,
            asset_class=asset_class,
            now_utc=eff_now_utc,
        )
        for pk in _CONTRACT_PRICE_OBS_KEYS:
            if pk in price_only_obs:
                observations[pk] = price_only_obs[pk]
            elif pk != "contract_close":
                observations.pop(pk, None)
        if last_price is not None:
            observations["close"] = last_price
            observations["contract_close"] = last_price

    price_daily_tech = (
        price_snap.get("technicals")
        if isinstance(price_snap, dict) and isinstance(price_snap.get("technicals"), dict)
        else {}
    )
    price_intra_tech = (
        price_snap.get("intraday_technicals")
        if isinstance(price_snap, dict) and isinstance(price_snap.get("intraday_technicals"), dict)
        else {}
    )
    atr14_30m = _to_float(price_intra_tech.get("atr14"))

    entry_price = _to_float(pos.get("entry_price"))
    if entry_price is None:
        entry_price = _to_float(pos.get("avg_entry_price"))
    if entry_price is None:
        entry_price = _to_float(pos.get("initial_entry_price"))
    target_price = _to_float(pos.get("target_price"))

    tactical_stop_30m = _to_float(pos.get("tactical_stop_30m"))
    if tactical_stop_30m is None:
        tactical_stop_30m = _to_float(pos.get("tactical_stop"))
    if tactical_stop_30m is None:
        tactical_stop_30m = _to_float(anchors.get("tactical_stop_30m"))
    if tactical_stop_30m is None:
        tactical_stop_30m = _to_float(anchors.get("tactical_stop"))

    structural_stop_daily = _to_float(pos.get("structural_stop_daily"))
    if structural_stop_daily is None:
        structural_stop_daily = _to_float(pos.get("structural_invalidation"))
    if structural_stop_daily is None:
        structural_stop_daily = _to_float(anchors.get("structural_stop_daily"))
    if structural_stop_daily is None:
        structural_stop_daily = _to_float(anchors.get("structural_invalidation"))

    raw_unrealized_pnl_pct = _compute_raw_unrealized_pnl_pct(
        last_price,
        entry_price,
        norm_dir,
    )
    unrealized_pnl_pct = _compute_unrealized_pnl_pct(
        last_price,
        entry_price,
        norm_dir,
        asset_class=asset_class,
    )

    # ------------------------------------------------------------------
    # Layer 5 (early): Price Feed, Daily/30m Freshness & Chanlun Completeness
    # ------------------------------------------------------------------
    max_daily_days = MAX_CRYPTO_STALE_DAYS if asset_class == "crypto" else MAX_DAILY_STALE_DAYS
    max_intraday_days = MAX_CRYPTO_STALE_DAYS if asset_class == "crypto" else MAX_INTRADAY_STALE_DAYS
    daily_bar_missing = False
    daily_bar_stale = False
    intraday_missing_or_failed = False
    intraday_lags_daily = False
    intraday_stale = False
    intraday_timestamp_invalid = False
    intraday_future_timestamp = False
    intraday_bar_unclosed = False

    if not contract_quote_mismatch:
        if last_price is None:
            err_detail = (
                str(price_snap.get("error"))
                if isinstance(price_snap, dict) and price_snap.get("error")
                else "snapshot 缺失或无有效收盘价"
            )
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "price_feed_missing",
                "message": "行情抓取失败，无法确认安全 — 禁止误报绿灯",
                "detail": err_detail,
            })
            if not any("market_snapshot" in w for w in data_warnings):
                data_warnings.append(f"market_snapshot: status=error ({err_detail})")
        else:
            # Daily bar freshness check
            d_dt, d_local, d_has_time, _, d_lbl = _extract_tech_timestamp(price_daily_tech, asset_class=asset_class)
            d_close = _to_float(price_daily_tech.get("last_close"))
            if d_close is None or (d_dt is None and d_local is None):
                daily_bar_missing = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "daily_bar_missing",
                    "message": "缺少有效日线 K 线数据或时间戳，禁止误报绿灯",
                    "detail": f"technicals.as_of_bar={d_lbl!r}, last_close={d_close!r}",
                })
                data_warnings.append("daily_bar: status=error (missing daily bar or timestamp)")
            else:
                d_age = _bar_age_days(d_dt, d_local, d_has_time, anchor, eff_now_utc)
                if d_age is not None and d_age > max_daily_days:
                    daily_bar_stale = True
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "daily_bar_stale",
                        "message": f"日线行情已过期（滞后 {d_age:.1f} 天 > {max_daily_days} 天），禁止误报绿灯",
                        "detail": f"as_of_bar={d_lbl}, ref_date={anchor.isoformat()}",
                    })
                    data_warnings.append(
                        f"daily_bar: status=stale (as_of={d_lbl}, age={d_age:.1f}d > {max_daily_days}d)"
                    )

            # 30min intraday freshness, timestamp integrity & bar_is_closed check
            intra_err = price_snap.get("intraday_error") if isinstance(price_snap, dict) else None
            if not intra_err and isinstance(price_snap, dict) and isinstance(price_snap.get("chanlun"), dict):
                p_cl = price_snap["chanlun"]
                intra_err = p_cl.get("intraday_error")
                p_icl = p_cl.get("intraday_30m") or p_cl.get("intraday")
                if not intra_err and isinstance(p_icl, dict) and p_icl.get("error"):
                    intra_err = p_icl.get("error")

            i_dt, i_local, i_has_time, _, i_lbl = _extract_tech_timestamp(price_intra_tech, asset_class=asset_class)
            i_close = _to_float(price_intra_tech.get("last_close"))
            if intra_err or i_close is None or (i_dt is None and i_local is None):
                intraday_missing_or_failed = True
                reason = str(intra_err) if intra_err else f"as_of_bar={i_lbl!r}, last_close={i_close!r}"
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_unavailable",
                    "message": "30min 行情缺失或抓取失败，禁止误报绿灯",
                    "detail": reason,
                })
                if not any("intraday_30m" in w for w in data_warnings):
                    data_warnings.append(f"intraday_30m: status=error ({reason})")
            else:
                if not i_has_time:
                    intraday_timestamp_invalid = True
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "intraday_30m_missing_time",
                        "message": "30min 行情缺少分钟级时间戳，无法验证日内新鲜度",
                        "detail": f"as_of_bar={i_lbl!r}",
                    })
                    data_warnings.append(f"intraday_30m: missing time component (as_of={i_lbl!r})")

                is_future_intra = (
                    (eff_now_utc is not None and i_dt is not None and i_has_time and i_dt > eff_now_utc + timedelta(minutes=5))
                    or (
                        eff_now_utc is None
                        and i_dt is not None
                        and i_dt.date() > anchor
                        and (i_local is None or i_local > anchor)
                    )
                )
                if is_future_intra:
                    intraday_timestamp_invalid = True
                    intraday_future_timestamp = True
                    now_txt = eff_now_utc.isoformat() if eff_now_utc is not None else anchor.isoformat()
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "intraday_30m_future_timestamp",
                        "message": "30min 行情时间戳晚于当前时间，疑似时钟/数据异常",
                        "detail": f"as_of_bar={i_lbl} ({i_dt.isoformat() if i_dt else None}), now={now_txt}",
                    })
                    data_warnings.append(f"intraday_30m: future timestamp (as_of={i_lbl} > now={now_txt})")

                if price_intra_tech.get("bar_is_closed") is False:
                    intraday_bar_unclosed = True
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "intraday_30m_bar_unclosed",
                        "message": "最新 30min K 线尚未收盘，暂不确认绿灯",
                        "detail": f"as_of_bar={i_lbl}, bar_is_closed=False",
                    })
                    data_warnings.append(f"intraday_30m: latest 30m bar unclosed (as_of={i_lbl})")

                if _is_intraday_older_than_daily(
                    price_daily_tech,
                    price_intra_tech,
                    asset_class=asset_class,
                    now_utc=eff_now_utc,
                    symbol=price_snap.get("symbol") or symbol,
                ):
                    intraday_lags_daily = True
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "intraday_30m_lags_daily",
                        "message": "30min 行情滞后于日线收盘，禁止误报绿灯",
                        "detail": f"30m={i_lbl}, daily={d_lbl}",
                    })
                i_age = _bar_age_days(i_dt, i_local, i_has_time, anchor, eff_now_utc)
                if i_age is not None and i_age > max_intraday_days:
                    intraday_stale = True
                    yellow_alerts.append({
                        "layer": "data_integrity",
                        "code": "intraday_30m_stale",
                        "message": f"30min 行情已过期（滞后 {i_age:.1f} 天 > {max_intraday_days} 天），禁止误报绿灯",
                        "detail": f"as_of_bar={i_lbl}, ref_date={anchor.isoformat()}",
                    })
                    data_warnings.append(
                        f"intraday_30m: status=stale (as_of={i_lbl}, age={i_age:.1f}d > {max_intraday_days}d)"
                    )

    # Also verify freshness of continuous/base Chanlun snapshot when separate from traded contract snapshot
    if isinstance(chanlun_snap, dict) and chanlun_snap is not price_snap and not chanlun_snap.get("error"):
        cl_d_tech = chanlun_snap.get("technicals") if isinstance(chanlun_snap.get("technicals"), dict) else {}
        cl_i_tech = (
            chanlun_snap.get("intraday_technicals")
            if isinstance(chanlun_snap.get("intraday_technicals"), dict)
            else {}
        )
        cd_dt, cd_local, cd_has_time, _, cd_lbl = _extract_tech_timestamp(cl_d_tech, asset_class=asset_class)
        if cd_dt is not None or cd_local is not None:
            cd_age = _bar_age_days(cd_dt, cd_local, cd_has_time, anchor, eff_now_utc)
            if cd_age is not None and cd_age > max_daily_days:
                daily_bar_stale = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "daily_bar_stale",
                    "message": f"主连日线行情已过期（滞后 {cd_age:.1f} 天 > {max_daily_days} 天），禁止误报绿灯",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={cd_lbl}",
                })
                data_warnings.append(
                    f"daily_bar: status=stale (base={chanlun_snap.get('symbol')}, as_of={cd_lbl}, age={cd_age:.1f}d)"
                )
        ci_dt, ci_local, ci_has_time, _, ci_lbl = _extract_tech_timestamp(cl_i_tech, asset_class=asset_class)
        if ci_dt is None and ci_local is None:
            intraday_timestamp_invalid = True
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "intraday_30m_missing_time",
                "message": "主连 30min 行情缺少有效时间戳，无法验证日内新鲜度",
                "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={ci_lbl!r}",
            })
            data_warnings.append(f"intraday_30m: invalid/missing timestamp (base={chanlun_snap.get('symbol')}, as_of={ci_lbl!r})")
        else:
            if not ci_has_time:
                intraday_timestamp_invalid = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_missing_time",
                    "message": "30min 行情缺少分钟级时间戳，无法验证日内新鲜度",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={ci_lbl!r}",
                })
                data_warnings.append(f"intraday_30m: missing time component (base={chanlun_snap.get('symbol')}, as_of={ci_lbl!r})")
            if eff_now_utc is not None and ci_dt is not None and ci_has_time and ci_dt > eff_now_utc + timedelta(minutes=5):
                intraday_timestamp_invalid = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_future_timestamp",
                    "message": "30min 行情时间戳晚于当前时间，疑似时钟/数据异常",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={ci_lbl}, now={eff_now_utc.isoformat()}",
                })
                data_warnings.append(f"intraday_30m: future timestamp (base={chanlun_snap.get('symbol')}, as_of={ci_lbl})")
            if cl_i_tech.get("bar_is_closed") is False:
                intraday_bar_unclosed = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_bar_unclosed",
                    "message": "最新 30min K 线尚未收盘，暂不确认绿灯",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={ci_lbl}, bar_is_closed=False",
                })
                data_warnings.append(f"intraday_30m: latest 30m bar unclosed (base={chanlun_snap.get('symbol')}, as_of={ci_lbl})")
            if _is_intraday_older_than_daily(
                cl_d_tech,
                cl_i_tech,
                asset_class=asset_class,
                now_utc=eff_now_utc,
                symbol=chanlun_snap.get("symbol") or symbol,
            ):
                intraday_lags_daily = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_lags_daily",
                    "message": "主连 30min 行情滞后于主连日线收盘，禁止误报绿灯",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, 30m={ci_lbl}, daily={cd_lbl}",
                })
                data_warnings.append(
                    f"intraday_30m: lags daily (base={chanlun_snap.get('symbol')}, 30m={ci_lbl}, daily={cd_lbl})"
                )
            ci_age = _bar_age_days(ci_dt, ci_local, ci_has_time, anchor, eff_now_utc)
            if ci_age is not None and ci_age > max_intraday_days:
                intraday_stale = True
                yellow_alerts.append({
                    "layer": "data_integrity",
                    "code": "intraday_30m_stale",
                    "message": f"主连 30min 行情已过期（滞后 {ci_age:.1f} 天 > {max_intraday_days} 天），禁止误报绿灯",
                    "detail": f"base_symbol={chanlun_snap.get('symbol')}, as_of_bar={ci_lbl}",
                })
                data_warnings.append(
                    f"intraday_30m: status=stale (base={chanlun_snap.get('symbol')}, as_of={ci_lbl}, age={ci_age:.1f}d)"
                )

    # ------------------------------------------------------------------
    # Layer 1: Hard Stop / Price Defense (Close + Intraday High/Low Breach)
    # ------------------------------------------------------------------
    if tactical_stop_30m is None and structural_stop_daily is None:
        yellow_alerts.append({
            "layer": "price_stop",
            "code": "missing_stop_levels",
            "message": "持仓未设置 30min 战术止损位或日线结构失效位，请立即补齐硬止损",
            "detail": "tactical_stop_30m=None, structural_stop_daily=None",
        })

    if last_price is not None and norm_dir in {"long", "short"}:
        daily_breached = False
        tactical_breached = False

        # Resolve per-stop effective timestamps BEFORE comparing last_price against stops
        opened_at = pos.get("opened_at_utc") or pos.get("entry_timestamp_utc")
        tactical_eff_dt, tactical_eff_raw = _resolve_effective_stop_ts(
            opened_at, pos.get("stop_updated_at_utc"), asset_class=asset_class
        )
        structural_eff_dt, structural_eff_raw = _resolve_effective_stop_ts(
            opened_at, pos.get("structural_stop_updated_at_utc"), asset_class=asset_class
        )

        # Determine timestamp & bar end of whichever technicals block supplied last_price
        price_bar_dt_utc: datetime | None = None
        price_bar_end_utc: datetime | None = None
        price_bar_has_time = False
        price_bar_label: str | None = None
        if price_source == "intraday_30m":
            p_dt, _, p_has_time, _, p_lbl = _extract_tech_timestamp(price_intra_tech, asset_class=asset_class)
            price_bar_dt_utc = p_dt
            price_bar_has_time = p_has_time
            price_bar_label = p_lbl
            if p_dt is not None and p_has_time:
                p_anchor = str(price_intra_tech.get("bar_timestamp_anchor") or "start").strip().lower()
                p_dur = timedelta(minutes=_parse_intraday_bar_duration_minutes(price_intra_tech.get("bar_interval")))
                price_bar_end_utc = p_dt if p_anchor == "end" else (p_dt + p_dur)
        elif price_source == "daily":
            p_dt, p_local_d, p_has_time, _, p_lbl = _extract_tech_timestamp(price_daily_tech, asset_class=asset_class)
            price_bar_dt_utc = p_dt
            price_bar_has_time = p_has_time
            price_bar_label = p_lbl
            if p_dt is not None and p_has_time:
                price_bar_end_utc = p_dt
            elif p_local_d is not None:
                ex_tz_px = _default_tz_for_asset_class(asset_class)
                price_bar_end_utc = datetime(
                    p_local_d.year, p_local_d.month, p_local_d.day, 23, 59, 59, tzinfo=ex_tz_px
                ).astimezone(timezone.utc)

        # Future timestamp guard: corrupted future price MUST NOT emit false RED_EXIT
        price_is_future = (
            (price_source == "intraday_30m" and intraday_future_timestamp)
            or (
                eff_now_utc is not None
                and price_bar_dt_utc is not None
                and price_bar_has_time
                and price_bar_dt_utc > eff_now_utc + timedelta(minutes=5)
            )
        )
        if price_is_future and price_source == "intraday_30m":
            observations.pop("close", None)
            observations.pop("contract_close", None)

        # Pre-effective close guard: never flag RED_EXIT from a bar that closed on/before stop effective_ts
        structural_price_allowed = (
            structural_eff_dt is None
            or price_bar_end_utc is None
            or price_bar_end_utc > structural_eff_dt
        )
        tactical_price_allowed = (
            tactical_eff_dt is None
            or price_bar_end_utc is None
            or price_bar_end_utc > tactical_eff_dt
        )
        predate_details: list[str] = []

        if not price_is_future:
            if structural_stop_daily is not None:
                if not structural_price_allowed and structural_eff_dt is not None and price_bar_end_utc is not None:
                    predate_details.append(
                        f"日线结构失效位生效于 {structural_eff_dt.isoformat()}，"
                        f"最新行情收盘于 {price_bar_end_utc.isoformat()} ({price_bar_label or price_source})"
                    )
                elif (norm_dir == "long" and last_price <= structural_stop_daily) or (
                    norm_dir == "short" and last_price >= structural_stop_daily
                ):
                    daily_breached = True
                    cmp_op = "<=" if norm_dir == "long" else ">="
                    red_alerts.append({
                        "layer": "price_stop",
                        "code": "structural_stop_daily_breached",
                        "message": "触及日线结构失效位，果断撤离",
                        "detail": f"现价 {last_price} {cmp_op} 日线失效位 {structural_stop_daily} ({price_source})",
                    })

            if tactical_stop_30m is not None:
                if not tactical_price_allowed and tactical_eff_dt is not None and price_bar_end_utc is not None:
                    predate_details.append(
                        f"30min 战术止损生效于 {tactical_eff_dt.isoformat()}，"
                        f"最新行情收盘于 {price_bar_end_utc.isoformat()} ({price_bar_label or price_source})"
                    )
                elif (norm_dir == "long" and last_price <= tactical_stop_30m) or (
                    norm_dir == "short" and last_price >= tactical_stop_30m
                ):
                    tactical_breached = True
                    cmp_op = "<=" if norm_dir == "long" else ">="
                    red_alerts.append({
                        "layer": "price_stop",
                        "code": "tactical_stop_30m_breached",
                        "message": "触及 30min 战术止损位，果断撤离",
                        "detail": f"现价 {last_price} {cmp_op} 30min 战术止损 {tactical_stop_30m} ({price_source})",
                    })

            if predate_details:
                yellow_alerts.append({
                    "layer": "price_stop",
                    "code": "price_predates_stop_effective_ts",
                    "message": "最新行情时间早于止损生效时刻，暂不按旧收盘价误判触损，等待新 K 线确认",
                    "detail": "；".join(dict.fromkeys(predate_details)),
                })

        # Intraday / Bar High-Low Stop Breach Detection (even if close recovered!)
        # Per-stop effective timestamps guard against retroactively flagging pre-trail bars;
        # now_utc excludes corrupted future bars from OHLC breach evaluation.
        ohlc_cache: dict[
            datetime | None,
            tuple[
                float | None,
                dict[str, Any] | None,
                float | None,
                dict[str, Any] | None,
                str | None,
                list[dict[str, Any]],
            ],
        ] = {}

        def _get_ohlc_for(
            eff_dt: datetime | None,
            eff_raw: Any,
        ) -> tuple[
            float | None,
            dict[str, Any] | None,
            float | None,
            dict[str, Any] | None,
            str | None,
            list[dict[str, Any]],
        ]:
            if eff_dt not in ohlc_cache:
                ohlc_cache[eff_dt] = _collect_eligible_ohlc_extrema(
                    price_snap,
                    effective_ts_utc=eff_raw,
                    asset_class=asset_class,
                    now_utc=eff_now_utc,
                )
            return ohlc_cache[eff_dt]

        def _route_gap_text(prefix: str, gap_txt: str | None) -> None:
            if not gap_txt:
                return
            for part in gap_txt.split("；"):
                p = part.strip()
                if not p:
                    continue
                if p.startswith("intraday_internal_bar_gap:"):
                    internal_gap_warnings.append(f"{prefix}: {p}")
                else:
                    window_gap_warnings.append(f"{prefix}: {p}")

        def _check_straddle_touch(
            prefix: str,
            stop_val: float,
            s_bars: list[dict[str, Any]],
        ) -> None:
            for sb in s_bars:
                sb_low = _to_float(sb.get("low"))
                sb_high = _to_float(sb.get("high"))
                sb_bar = sb.get("bar") or sb.get("bar_start_utc") or "straddle_bar"
                if norm_dir == "long" and sb_low is not None and sb_low <= stop_val:
                    straddle_warnings.append(
                        f"{prefix}: bar={sb_bar} low={sb_low} <= {stop_val}"
                    )
                elif norm_dir == "short" and sb_high is not None and sb_high >= stop_val:
                    straddle_warnings.append(
                        f"{prefix}: bar={sb_bar} high={sb_high} >= {stop_val}"
                    )

        window_gap_warnings: list[str] = []
        internal_gap_warnings: list[str] = []
        straddle_warnings: list[str] = []

        if structural_stop_daily is not None and not daily_breached:
            s_min_low, s_low_meta, s_max_high, s_high_meta, s_gap, s_straddle = _get_ohlc_for(
                structural_eff_dt, structural_eff_raw
            )
            if norm_dir == "long" and s_min_low is not None and s_min_low <= structural_stop_daily:
                daily_breached = True
                meta_txt = (
                    f"bar={s_low_meta['bar']}, tf={s_low_meta['tf']}"
                    if isinstance(s_low_meta, dict)
                    else "ohlc"
                )
                red_alerts.append({
                    "layer": "price_stop",
                    "code": "structural_stop_daily_intraday_breached",
                    "message": "盘中最低价触及止损防线（即便收盘拉回仍判定触损）",
                    "detail": (
                        f"盘中最低价 {s_min_low} <= 日线结构失效位 {structural_stop_daily} "
                        f"({meta_txt}, 现价={last_price})"
                    ),
                })
            elif norm_dir == "short" and s_max_high is not None and s_max_high >= structural_stop_daily:
                daily_breached = True
                meta_txt = (
                    f"bar={s_high_meta['bar']}, tf={s_high_meta['tf']}"
                    if isinstance(s_high_meta, dict)
                    else "ohlc"
                )
                red_alerts.append({
                    "layer": "price_stop",
                    "code": "structural_stop_daily_intraday_breached",
                    "message": "盘中最高价触及止损防线（即便收盘拉回仍判定触损）",
                    "detail": (
                        f"盘中最高价 {s_max_high} >= 日线结构失效位 {structural_stop_daily} "
                        f"({meta_txt}, 现价={last_price})"
                    ),
                })
            else:
                _check_straddle_touch("structural_stop", structural_stop_daily, s_straddle)
                _route_gap_text("structural_stop", s_gap)

        if tactical_stop_30m is not None and not tactical_breached:
            t_min_low, t_low_meta, t_max_high, t_high_meta, t_gap, t_straddle = _get_ohlc_for(
                tactical_eff_dt, tactical_eff_raw
            )
            if norm_dir == "long" and t_min_low is not None and t_min_low <= tactical_stop_30m:
                tactical_breached = True
                meta_txt = (
                    f"bar={t_low_meta['bar']}, tf={t_low_meta['tf']}"
                    if isinstance(t_low_meta, dict)
                    else "ohlc"
                )
                red_alerts.append({
                    "layer": "price_stop",
                    "code": "tactical_stop_30m_intraday_breached",
                    "message": "盘中最低价触及止损防线（即便收盘拉回仍判定触损）",
                    "detail": (
                        f"盘中最低价 {t_min_low} <= 30min 战术止损 {tactical_stop_30m} "
                        f"({meta_txt}, 现价={last_price})"
                    ),
                })
            elif norm_dir == "short" and t_max_high is not None and t_max_high >= tactical_stop_30m:
                tactical_breached = True
                meta_txt = (
                    f"bar={t_high_meta['bar']}, tf={t_high_meta['tf']}"
                    if isinstance(t_high_meta, dict)
                    else "ohlc"
                )
                red_alerts.append({
                    "layer": "price_stop",
                    "code": "tactical_stop_30m_intraday_breached",
                    "message": "盘中最高价触及止损防线（即便收盘拉回仍判定触损）",
                    "detail": (
                        f"盘中最高价 {t_max_high} >= 30min 战术止损 {tactical_stop_30m} "
                        f"({meta_txt}, 现价={last_price})"
                    ),
                })
            else:
                _check_straddle_touch("tactical_stop", tactical_stop_30m, t_straddle)
                _route_gap_text("tactical_stop", t_gap)

        if straddle_warnings and not daily_breached and not tactical_breached:
            yellow_alerts.append({
                "layer": "price_stop",
                "code": "straddling_bar_stop_ambiguity",
                "message": "跨止损生效时刻的 30min K 线盘中触及止损价，无法确认触发先后，需核查更细粒度分时明细",
                "detail": "；".join(dict.fromkeys(straddle_warnings)),
            })

        # Same-day window coverage gap: block GREEN_HOLD if no stop breach was found in visible bars
        if window_gap_warnings and not daily_breached and not tactical_breached:
            gap_detail_txt = "；".join(dict.fromkeys(window_gap_warnings))
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "intraday_ohlc_window_gap_since_effective_ts",
                "message": "当日 30min K 线回溯窗口未能覆盖开仓/止损生效时间，无法确认盘中未触损",
                "detail": gap_detail_txt,
            })
            data_warnings.append(f"intraday_ohlc_window_gap_since_effective_ts: {gap_detail_txt}")

        # Internal same-session missing bar gap: block GREEN_HOLD when an intra-session 30m bar is missing
        if internal_gap_warnings and not daily_breached and not tactical_breached:
            igap_detail_txt = "；".join(dict.fromkeys(internal_gap_warnings))
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "intraday_internal_bar_gap",
                "message": "当日 30min K 线序列存在盘中缺失缺口，无法确认盘中未触损",
                "detail": igap_detail_txt,
            })
            data_warnings.append(f"intraday_internal_bar_gap: {igap_detail_txt}")

        # Proximity check for non-breached stops (skip corrupted future or pre-effective prices)
        if not price_is_future:
            proximity_hits: list[str] = []
            stop_checks = [
                ("30min 战术止损", tactical_stop_30m, tactical_breached, tactical_price_allowed),
                ("日线结构失效位", structural_stop_daily, daily_breached, structural_price_allowed),
            ]
            for label, stop_val, breached, price_allowed in stop_checks:
                if stop_val is None or breached or (not price_allowed) or last_price <= 0:
                    continue
                dist = (last_price - stop_val) if norm_dir == "long" else (stop_val - last_price)
                if dist <= 0:
                    continue
                dist_pct = (dist / last_price) * 100.0
                near_pct = dist_pct <= 1.0
                near_atr = atr14_30m is not None and atr14_30m > 0 and dist <= 0.5 * atr14_30m
                if near_pct or near_atr:
                    atr_note = f", 约 {dist / atr14_30m:.2f}x 30m-ATR14" if (atr14_30m and atr14_30m > 0) else ""
                    proximity_hits.append(f"距{label} {stop_val} 仅 {dist_pct:.2f}%{atr_note}")
            if proximity_hits:
                yellow_alerts.append({
                    "layer": "price_stop",
                    "code": "stop_proximity_warning",
                    "message": "逼近止损防线，高度警戒",
                    "detail": "；".join(proximity_hits),
                })

            # Target reached informational note
            if target_price is not None and (
                (norm_dir == "long" and last_price >= target_price)
                or (norm_dir == "short" and last_price <= target_price)
            ):
                info_notes.append(
                    f"现价 {last_price} 已达目标位 {target_price}，可结合 30min 缠论结构分批止盈或收紧追踪止损。"
                )

    # ------------------------------------------------------------------
    # Layer 2: Dual-Timeframe Chanlun Defense (日线方向 + 30min 结构)
    # ------------------------------------------------------------------
    cl_container = (
        chanlun_snap.get("chanlun")
        if isinstance(chanlun_snap, dict) and isinstance(chanlun_snap.get("chanlun"), dict)
        else {}
    )
    daily_cl = cl_container.get("daily") if isinstance(cl_container.get("daily"), dict) else None
    intra_cl = _get_intraday_chanlun(cl_container)
    dual = cl_container.get("dual_alignment") if isinstance(cl_container.get("dual_alignment"), dict) else None

    daily_cl_valid = isinstance(daily_cl, dict) and not daily_cl.get("error") and bool(daily_cl)
    intra_cl_valid = isinstance(intra_cl, dict) and not intra_cl.get("error") and bool(intra_cl)

    if not daily_cl_valid:
        d_err = daily_cl.get("error") if isinstance(daily_cl, dict) and daily_cl.get("error") else "日线缠论结构缺失"
        yellow_alerts.append({
            "layer": "data_integrity",
            "code": "chanlun_daily_missing",
            "message": "缺少有效日线缠论结构，禁止误报绿灯",
            "detail": str(d_err),
        })
        if not any("chanlun_daily" in w for w in data_warnings):
            data_warnings.append(f"chanlun_daily: status=error ({d_err})")

    if not intra_cl_valid:
        i_err = (
            cl_container.get("intraday_error")
            or (chanlun_snap.get("intraday_error") if isinstance(chanlun_snap, dict) else None)
            or "30min 缠论结构缺失"
        )
        yellow_alerts.append({
            "layer": "data_integrity",
            "code": "chanlun_30m_missing",
            "message": "缺少有效 30min 缠论结构，禁止误报绿灯",
            "detail": str(i_err),
        })
        if not any("intraday_30m" in w or "chanlun_30m" in w for w in data_warnings):
            data_warnings.append(f"chanlun_30m: status=error ({i_err})")

    chanlun_intra_untrusted = bool(
        intraday_missing_or_failed
        or intraday_stale
        or intraday_lags_daily
        or intraday_timestamp_invalid
        or (isinstance(chanlun_snap, dict) and (chanlun_snap.get("intraday_error") or chanlun_snap.get("intraday_stale_fallback")))
        or (isinstance(price_snap, dict) and (price_snap.get("intraday_error") or price_snap.get("intraday_stale_fallback")))
    )

    # If dual_alignment wasn't precomputed but both daily & intraday exist and intraday is fresh/valid, call it
    if (
        dual is None
        and daily_cl_valid
        and intra_cl_valid
        and not chanlun_intra_untrusted
        and isinstance(daily_cl, dict)
        and isinstance(intra_cl, dict)
        and hasattr(cl_mod, "assess_dual_timeframe_alignment")
    ):
        try:
            dual = cl_mod.assess_dual_timeframe_alignment(daily_cl, intra_cl, desired_direction=norm_dir)
        except (ValueError, TypeError, KeyError):
            dual = None

    if norm_dir in {"long", "short"}:
        # Daily Chanlun assessment (prefer structured signal_meta; NEVER false-RED on candidates/weak strokes)
        daily_rev_reasons: list[str] = []
        daily_loosen_reasons: list[str] = []

        daily_meta_from_dual = dual.get("daily_signal_meta") if isinstance(dual, dict) else None
        effective_daily_meta = _extract_structured_signal_meta(
            daily_cl, cl_container.get("signal_summary"), daily_meta_from_dual
        )
        d_strong, d_loosen = _classify_chanlun_signals(
            daily_cl,
            cl_container.get("signal_summary"),
            norm_dir,
            signal_meta=daily_meta_from_dual,
        )
        daily_rev_reasons.extend(d_strong)
        daily_loosen_reasons.extend(d_loosen)

        if isinstance(dual, dict):
            d_bias = _normalize_direction(dual.get("daily_bias"))
            if d_bias in {"long", "short"} and d_bias != norm_dir:
                bias_msg = f"dual_alignment.daily_bias={dual.get('daily_bias')} 与持仓方向 {norm_dir} 相反"
                if daily_rev_reasons:
                    daily_rev_reasons.append(bias_msg)
                elif effective_daily_meta is not None:
                    # Structured signal_meta exists and was NOT confirmed/strong opposite -> loosening only!
                    daily_loosen_reasons.append(bias_msg)
                else:
                    # Legacy payload without signal_meta: do NOT escalate candidate signals or weak running strokes to RED
                    d_sigs = [str(s) for s in ((daily_cl or {}).get("signals") or []) if s]
                    d_mom = (
                        (daily_cl or {}).get("momentum_assessment")
                        if isinstance((daily_cl or {}).get("momentum_assessment"), dict)
                        else {}
                    )
                    d_verdict = str(d_mom.get("verdict") or "").strip().lower()
                    d_regime = str(d_mom.get("regime") or "").strip().lower()
                    d_summ = str((cl_container.get("signal_summary") or {}).get("summary") or "")
                    has_candidate_only = (
                        bool(daily_loosen_reasons)
                        or any(_is_candidate_or_unconfirmed_signal_text(s) for s in d_sigs)
                        or _is_candidate_or_unconfirmed_signal_text(d_summ)
                        or _is_unconfirmed_or_neutral_regime(d_regime)
                        or _is_unconfirmed_or_neutral_regime(d_verdict)
                    )
                    if has_candidate_only:
                        daily_loosen_reasons.append(bias_msg)
                    else:
                        daily_rev_reasons.append(bias_msg)

        if daily_rev_reasons:
            red_alerts.append({
                "layer": "chanlun_daily",
                "code": "daily_direction_reversed",
                "message": "日线主方向反转，果断撤离",
                "detail": "；".join(dict.fromkeys(daily_rev_reasons)),
            })
        elif daily_loosen_reasons:
            yellow_alerts.append({
                "layer": "chanlun_daily",
                "code": "daily_structure_loosening",
                "message": "日线结构出现松动信号，警惕趋势转折",
                "detail": "；".join(dict.fromkeys(daily_loosen_reasons)),
            })

        # 30min Chanlun assessment (only when 30min feed is valid & non-stale; historical breach replay runs separately)
        intra_weak_reasons: list[str] = []
        if not chanlun_intra_untrusted:
            intraday_meta_from_dual = dual.get("intraday_signal_meta") if isinstance(dual, dict) else None
            if isinstance(dual, dict):
                i_bias = _normalize_direction(dual.get("intraday_bias"))
                if i_bias in {"long", "short"} and i_bias != norm_dir:
                    intra_weak_reasons.append(f"30min 方向偏 {dual.get('intraday_bias')}（逆持仓方向）")
                i_action = str(dual.get("intraday_action") or "").strip().lower()
                if any(tok in i_action for tok in ("exit", "reduce", "tighten", "stop", "counter", "exhaustion", "撤离", "减仓")):
                    intra_weak_reasons.append(f"30min 动作提示={dual.get('intraday_action')}")
                elif (
                    (norm_dir == "long" and i_action in {"sell", "short", "enter_short", "entry_short"})
                    or (norm_dir == "short" and i_action in {"buy", "long", "enter_long", "entry_long"})
                ):
                    intra_weak_reasons.append(f"30min 触发反向动作={dual.get('intraday_action')}")

            i_strong, i_loosen = _classify_chanlun_signals(
                intra_cl,
                cl_container.get("intraday_signal_summary"),
                norm_dir,
                signal_meta=intraday_meta_from_dual,
            )
            intra_weak_reasons.extend(i_strong)
            intra_weak_reasons.extend(i_loosen)

        if intra_weak_reasons:
            yellow_alerts.append({
                "layer": "chanlun_30m",
                "code": "intraday_30m_weakening",
                "message": "30min 缠论结构走弱，收紧止损或减仓",
                "detail": "；".join(dict.fromkeys(intra_weak_reasons)),
            })

        # Trailing Stop Suggestion: ONLY from valid, fresh traded price_snap's 30m Zhongshu (never stale cache or cross-contract!)
        price_cl = (
            price_snap.get("chanlun")
            if isinstance(price_snap, dict) and isinstance(price_snap.get("chanlun"), dict)
            else {}
        )
        price_intra_cl = _get_intraday_chanlun(price_cl) or {}
        price_intra_zs = (
            price_intra_cl.get("latest_zhongshu")
            if isinstance(price_intra_cl.get("latest_zhongshu"), dict)
            else {}
        )
        price_dual = (
            price_cl.get("dual_alignment")
            if isinstance(price_cl.get("dual_alignment"), dict)
            else {}
        )
        intra_zd = _to_float(price_intra_zs.get("zd"))
        if intra_zd is None:
            intra_zd = _to_float(price_dual.get("intraday_zd"))
        intra_zg = _to_float(price_intra_zs.get("zg"))
        if intra_zg is None:
            intra_zg = _to_float(price_dual.get("intraday_zg"))

        if (
            not chanlun_intra_untrusted
            and last_price is not None
            and entry_price is not None
            and tactical_stop_30m is not None
        ):
            pnl_label = (
                f"+{raw_unrealized_pnl_pct:.4f}%"
                if (raw_unrealized_pnl_pct is not None and 0.0 < raw_unrealized_pnl_pct < 0.01)
                else f"+{(unrealized_pnl_pct if unrealized_pnl_pct is not None else (raw_unrealized_pnl_pct or 0.0)):.2f}%"
            )
            if (
                norm_dir == "long"
                and last_price > entry_price
                and (raw_unrealized_pnl_pct is None or raw_unrealized_pnl_pct > 0)
                and intra_zd is not None
                and tactical_stop_30m < intra_zd < last_price
            ):
                suggested_trail_stop = {
                    "current_stop": _round_finite(tactical_stop_30m, 4),
                    "suggested_stop": _round_finite(intra_zd, 4),
                    "improvement_pct": _round_finite((intra_zd / tactical_stop_30m - 1.0) * 100.0, 2)
                    if tactical_stop_30m > 0
                    else None,
                    "buffer_to_price_pct": _round_finite((1.0 - intra_zd / last_price) * 100.0, 2),
                    "anchor": "30m_zhongshu_zd",
                    "rationale": (
                        f"多头浮盈 {pnl_label}，最新 30min 中枢下沿 ZD={intra_zd} "
                        f"严格高于原战术止损 {tactical_stop_30m} 且位于现价 {last_price} 下方，建议上移追踪止损锁定浮盈。"
                    ),
                }
                info_notes.append(suggested_trail_stop["rationale"])
            elif (
                norm_dir == "short"
                and last_price < entry_price
                and (raw_unrealized_pnl_pct is None or raw_unrealized_pnl_pct > 0)
                and intra_zg is not None
                and last_price < intra_zg < tactical_stop_30m
            ):
                suggested_trail_stop = {
                    "current_stop": _round_finite(tactical_stop_30m, 4),
                    "suggested_stop": _round_finite(intra_zg, 4),
                    "improvement_pct": _round_finite((1.0 - intra_zg / tactical_stop_30m) * 100.0, 2)
                    if tactical_stop_30m > 0
                    else None,
                    "buffer_to_price_pct": _round_finite((intra_zg / last_price - 1.0) * 100.0, 2),
                    "anchor": "30m_zhongshu_zg",
                    "rationale": (
                        f"空头浮盈 {pnl_label}，最新 30min 中枢上沿 ZG={intra_zg} "
                        f"严格低于原战术止损 {tactical_stop_30m} 且位于现价 {last_price} 上方，建议下移追踪止损锁定浮盈。"
                    ),
                }
                info_notes.append(suggested_trail_stop["rationale"])

    # ------------------------------------------------------------------
    # Layer 3: Core Logic & Leading Indicator Triggers + B-Structure Watch
    # ------------------------------------------------------------------
    triggers_list = state.get("triggers") if isinstance(state.get("triggers"), list) else []
    armed_triggers = [t for t in triggers_list if _is_trigger_armed(t)]

    raw_red_lines = pos.get("exit_red_lines")
    normalized_red_lines: list[str] = []
    if isinstance(raw_red_lines, list):
        normalized_red_lines = [str(x).strip() for x in raw_red_lines if str(x).strip()]
    elif isinstance(raw_red_lines, str) and raw_red_lines.strip():
        normalized_red_lines = [raw_red_lines.strip()]
    has_red_lines = len(normalized_red_lines) > 0

    if len(armed_triggers) == 0 and not has_red_lines:
        yellow_alerts.append({
            "layer": "governance",
            "code": "no_exit_red_lines",
            "message": "持仓未绑定任何撤离红线/触发器，请立即补齐",
            "detail": "state.json armed_triggers=0 且 position.exit_red_lines=0",
        })
    elif len(armed_triggers) == 0 and has_red_lines:
        if not allow_manual_red_lines:
            yellow_alerts.append({
                "layer": "governance",
                "code": "manual_red_lines_only",
                "message": "仅配置人工文本红线、缺少机器可执行触发器，需人工复核",
                "detail": (
                    f"exit_red_lines={len(normalized_red_lines)}条, armed_triggers=0"
                    "（人工文本红线无法自动校验，如需放行请补齐 state.json 触发器或显式设置 allow_manual_red_lines=True）"
                ),
            })
        else:
            info_notes.append(
                f"已显式允许人工文本红线放行（exit_red_lines={len(normalized_red_lines)}条），仍建议尽快补充机器可执行触发器。"
            )

    trigger_results: list[dict[str, Any]] = []
    unevaluated_triggers: list[str] = []
    verifiable_trigger_ids: list[str] = []
    if armed_triggers:
        eval_now_utc = now_utc if now_utc is not None else (None if ref_date is not None else datetime.now(timezone.utc))
        trigger_results = ra_mod.evaluate_triggers(
            {"triggers": armed_triggers},
            observations,
            ref_date=anchor,
            now_utc=eval_now_utc,
        )
        for tr in trigger_results:
            tid = str(tr.get("id") or "?")
            metric = tr.get("metric")
            is_auto = ra_mod.is_trigger_auto_metric(tr)
            if tr.get("error"):
                # Only surface hard evaluation errors as upstream data warnings when auto metric failed;
                # stale/uncertain policy verifications are handled cleanly under unevaluated_triggers.
                if is_auto and not tr.get("verification_stale"):
                    data_warnings.append(f"trigger[{tid}]: {tr['error']}")
            if tr.get("evaluated") is not True:
                if not is_auto:
                    if tid != "?" and tid not in verifiable_trigger_ids:
                        verifiable_trigger_ids.append(tid)
                    err_txt = f": {tr['error']}" if tr.get("error") else ": 需 AI 联网或人工核验"
                else:
                    err_txt = f": {tr['error']}" if tr.get("error") else ": 指标缺失"
                unevaluated_triggers.append(f"{tid}({metric}{err_txt})")
                continue
            if not tr.get("fired"):
                continue
            kind = tr.get("kind") or "unspecified"
            op = tr.get("operator")
            thr = tr.get("threshold")
            obs_v = tr.get("observed")
            act = tr.get("action") or "触发告警"
            detail = f"[{tid}/{kind}] {metric}={obs_v} {op} {thr} -> {act}"
            if _is_red_trigger(tr):
                red_alerts.append({
                    "layer": "trigger",
                    "code": f"trigger_red_{tid}",
                    "message": f"核心逻辑/结构触发器命中：{act}",
                    "detail": detail,
                })
            else:
                yellow_alerts.append({
                    "layer": "trigger",
                    "code": f"trigger_yellow_{tid}",
                    "message": f"领先指标触发松动预警：{act}",
                    "detail": detail,
                })

    if unevaluated_triggers:
        yellow_alerts.append({
            "layer": "trigger_integrity",
            "code": "unevaluated_triggers",
            "message": "核心触发器未完成核验，禁止误报绿灯",
            "detail": "；".join(unevaluated_triggers),
        })

    # Commodity Backwardation / Contango Shift Watch (short holdings in B-structure)
    is_commodity = asset_class in {"commodity", "cn_future"} or "curve_structure" in observations
    if is_commodity and norm_dir == "short" and observations.get("curve_structure"):
        c_struct = str(observations["curve_structure"])
        c_lower = c_struct.lower()
        ry_near = _to_float(observations.get("roll_yield_near_pct"))
        ry_curve = _to_float(observations.get("roll_yield_curve_pct"))

        base_ry_near = (
            _to_float(pos.get("baseline_roll_yield_near_pct"))
            or _to_float(pos.get("entry_roll_yield_near_pct"))
            or _to_float(vb.get("baseline_roll_yield_near_pct"))
            or _to_float(vb.get("entry_roll_yield_near_pct"))
        )
        base_ry_curve = (
            _to_float(pos.get("baseline_roll_yield_curve_pct"))
            or _to_float(pos.get("entry_roll_yield_curve_pct"))
            or _to_float(vb.get("baseline_roll_yield_curve_pct"))
            or _to_float(vb.get("entry_roll_yield_curve_pct"))
        )
        base_struct = str(
            pos.get("baseline_curve_structure")
            or pos.get("entry_curve_structure")
            or vb.get("baseline_curve_structure")
            or vb.get("entry_curve_structure")
            or ""
        ).lower()

        if "backwardation" in c_lower:
            delta_near = (ry_near - base_ry_near) if (ry_near is not None and base_ry_near is not None) else None
            delta_curve = (ry_curve - base_ry_curve) if (ry_curve is not None and base_ry_curve is not None) else None

            widened_violently = (
                (delta_near is not None and delta_near >= 5.0)
                or (delta_curve is not None and delta_curve >= 5.0)
                or (ry_near is not None and ry_near >= 20.0 and (delta_near is None or delta_near >= 0.0))
            )
            narrowed = (
                (delta_near is not None and delta_near <= -2.0)
                or (delta_curve is not None and delta_curve <= -2.0)
            )

            if widened_violently:
                parts = [f"结构={c_struct}"]
                if ry_near is not None:
                    parts.append(f"近月年化展期={ry_near:+.2f}%")
                if delta_near is not None:
                    parts.append(f"较基线走阔 {delta_near:+.2f}pp")
                elif delta_curve is not None:
                    parts.append(f"全曲线较基线走阔 {delta_curve:+.2f}pp")
                yellow_alerts.append({
                    "layer": "term_structure",
                    "code": "backwardation_squeeze_warning",
                    "message": "大 B 结构急剧走阔，警惕近月挤仓与高展期成本风险",
                    "detail": "，".join(parts) + "（大B结构非空头硬否决，但需评估移仓远月或收缩仓位）",
                })
            elif narrowed:
                chg_txt = f"{delta_near:+.2f}pp" if delta_near is not None else f"{delta_curve:+.2f}pp"
                info_notes.append(
                    f"大 B 结构较基线明显收窄（{chg_txt}，当前近月年化展期 {ry_near}%），"
                    "印证「强现实 vs 弱预期」劈叉正朝空头逻辑兑现。"
                )
            else:
                ry_txt = f"近月年化展期 {ry_near:+.2f}%" if ry_near is not None else "展期率待补"
                info_notes.append(
                    f"空头处于大 B 结构（{ry_txt}）：属强现实 vs 弱预期劈叉（非做空否决项），持续盯防近月月差与展期成本。"
                )
        elif "contango" in c_lower:
            if "backwardation" in base_struct:
                info_notes.append(
                    f"期限结构已由 Backwardation 转为 {c_struct}，空头弱预期全面兑现且转为正展期顺风。"
                )
            else:
                info_notes.append(f"当前呈 {c_struct}，空头享有正向展期顺风。")
        elif "flat" in c_lower and "backwardation" in base_struct:
            info_notes.append("期限结构已由 Backwardation 收敛至平坦，空头弱预期逐步兑现。")

    # ------------------------------------------------------------------
    # Layer 4: Upcoming Events & Futures Roll-Window Alerts
    # ------------------------------------------------------------------
    seen_event_keys: set[tuple[str, str]] = set()
    raw_events = state.get("events") if isinstance(state.get("events"), list) else []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        ev_status = str(ev.get("status") or "upcoming").strip().lower()
        if ev_status != "upcoming":
            continue
        ev_date = _parse_iso_date(ev.get("date"))
        title = str(ev.get("title") or "").strip()
        if ev_date is None or not title:
            continue
        days_until = (ev_date - anchor).days
        if 0 <= days_until <= event_window_days:
            imminent = days_until <= 3
            upcoming_events.append({
                "date": ev_date.isoformat(),
                "days_until": days_until,
                "title": title,
                "source": "archive_event",
                "imminent": imminent,
            })
            seen_event_keys.add((ev_date.isoformat(), title.lower()))
            if imminent:
                yellow_alerts.append({
                    "layer": "event",
                    "code": "event_imminent",
                    "message": f"重大事件临近（{days_until}天后）：{title}",
                    "detail": f"日期 {ev_date.isoformat()}（<=3天窗口，请执行事件前风控）",
                })
            else:
                info_notes.append(f"近期事件提醒（{days_until}天后 · {ev_date.isoformat()}）：{title}")

    # Next earnings date from yfinance_consensus
    earn_iso = observations.get("next_earnings_date")
    earn_days = observations.get("days_to_earnings")
    if earn_iso and isinstance(earn_days, int) and 0 <= earn_days <= event_window_days:
        already_listed = any(
            d == earn_iso and ("财报" in t or "earning" in t)
            for d, t in seen_event_keys
        )
        if not already_listed:
            imminent = earn_days <= 3
            upcoming_events.append({
                "date": earn_iso,
                "days_until": earn_days,
                "title": "财报发布日 (Earnings)",
                "source": "yfinance_earnings",
                "imminent": imminent,
            })
            if imminent:
                yellow_alerts.append({
                    "layer": "event",
                    "code": "event_imminent",
                    "message": f"财报窗口临近（{earn_days}天后）：{earn_iso}",
                    "detail": "距财报发布 <=3 天，请严格执行财报前仓位/对冲风控规则",
                })
            else:
                info_notes.append(f"近期财报提醒（{earn_days}天后 · {earn_iso}）")

    # Futures contract expiry / roll-window check
    raw_contract_sym = str(pos.get("contract_symbol") or vb.get("contract_symbol") or "").strip() or None
    contract_expr = pos.get("contract_or_expression") or raw_contract_sym or vb.get("contract_or_expression")

    extracted_contracts_map: dict[str, dict[str, Any]] = {}
    for src_txt in (raw_contract_sym, contract_expr):
        if isinstance(src_txt, str) and src_txt.strip():
            for ci in _extract_futures_contracts(src_txt, ref_date=anchor):
                extracted_contracts_map.setdefault(ci["contract"], ci)

    if (
        raw_contract_sym
        and _looks_like_futures_contract(raw_contract_sym, asset_class)
        and not _extract_futures_contracts(raw_contract_sym, ref_date=anchor)
    ):
        is_cont = _is_continuous_futures_code(raw_contract_sym, asset_class)
        inferred_dom = (
            _infer_dominant_contract_from_bundle(raw_contract_sym, research_bundle, anchor)
            if is_cont
            else None
        )
        if is_cont:
            # Never add inferred contract to extracted_contracts_map (don't fake real-position expiry alerts)
            # and never silently grant GREEN for an unmapped continuous holding.
            ref_hint = (
                f"当前曲线参考主力 {inferred_dom['contract']}（预计最后交易日 "
                f"{inferred_dom['est_last_trading_day'].isoformat()}，依据 {inferred_dom['selection_basis']}）；"
                "若实盘持有近月交割合约，需以实际合约到期日为准"
                if inferred_dom is not None
                else "当前未能从有效期限曲线推断参考主力，请在账本填写具体交割月合约代码以盯防移仓"
            )
            yellow_alerts.append({
                "layer": "roll_window",
                "code": "continuous_contract_unmapped",
                "message": f"持仓登记为连续合约 {raw_contract_sym}，请补充具体交割月合约以精确盯防移仓",
                "detail": ref_hint,
            })
            if inferred_dom is None:
                ltd_warn = (
                    f"futures_roll_window: 无法解析持仓合约 {raw_contract_sym!r} 的最后交易日(LTD)，"
                    "请填写具体交割合约代码并人工核对移仓窗口"
                )
                if ltd_warn not in data_warnings:
                    data_warnings.append(ltd_warn)
        else:
            ltd_warn = (
                f"futures_roll_window: 无法解析持仓合约 {raw_contract_sym!r} 的最后交易日(LTD)，"
                "请填写具体交割合约代码并人工核对移仓窗口"
            )
            if ltd_warn not in data_warnings:
                data_warnings.append(ltd_warn)
    elif (
        not extracted_contracts_map
        and asset_class in {"commodity", "cn_future"}
        and _is_continuous_futures_code(norm_sym, asset_class)
    ):
        inferred_dom = _infer_dominant_contract_from_bundle(norm_sym, research_bundle, anchor)
        if inferred_dom is not None:
            info_notes.append(
                f"期货持仓 {norm_sym} 未登记具体交割月合约（参考当前曲线主力 {inferred_dom['contract']}，"
                f"预计最后交易日 {inferred_dom['est_last_trading_day'].isoformat()}），"
                "建议在账本补充实盘交割合约代码以启用移仓红线"
            )
        else:
            info_notes.append(
                f"期货持仓 {norm_sym} 未登记具体交割月合约，建议在账本补充实盘合约代码以启用移仓红线"
            )

    roll_lookahead = max(ROLL_INFO_WINDOW_DAYS, event_window_days)
    for cinfo in extracted_contracts_map.values():
        ltd: date = cinfo["est_last_trading_day"]
        days_to_ltd = (ltd - anchor).days
        code = cinfo["contract"]
        if days_to_ltd < 0:
            yellow_alerts.append({
                "layer": "roll_window",
                "code": "contract_expired_or_roll_overdue",
                "message": f"合约 {code} 已过预估最后交易日，请立即核对移仓状态",
                "detail": f"预估最后交易日 {ltd.isoformat()}（已过 {-days_to_ltd} 天）",
            })
        elif days_to_ltd <= roll_lookahead:
            imminent = days_to_ltd <= ROLL_IMMINENT_DAYS
            upcoming_events.append({
                "date": ltd.isoformat(),
                "days_until": days_to_ltd,
                "title": f"期货移仓/到期窗口 ({code})",
                "source": "futures_roll_window",
                "imminent": imminent,
            })
            if imminent:
                yellow_alerts.append({
                    "layer": "roll_window",
                    "code": "roll_window_imminent",
                    "message": f"期货合约 {code} 临近最后交易日（{days_to_ltd}天后），立即执行移仓展期",
                    "detail": f"预估最后交易日 {ltd.isoformat()}（<={ROLL_IMMINENT_DAYS}天红线）",
                })
            elif days_to_ltd <= ROLL_INFO_WINDOW_DAYS or days_to_ltd <= event_window_days:
                info_notes.append(
                    f"期货移仓窗口提醒：{code} 预估最后交易日 {ltd.isoformat()}（{days_to_ltd}天后，建议提前规划移仓）"
                )

    upcoming_events.sort(key=lambda e: (e.get("days_until", 9999), e.get("date", "")))

    # ------------------------------------------------------------------
    # Positive Precondition 5: Any degraded source in data_warnings blocks GREEN_HOLD
    # ------------------------------------------------------------------
    data_warnings = list(dict.fromkeys(data_warnings))
    if data_warnings and not any(a.get("code") == "data_source_degraded" for a in yellow_alerts):
        # Always emit data_source_degraded if no data_integrity alert exists yet, OR if any upstream
        # research/futures warning exists beyond already-emitted price/bar alerts
        has_integrity_alert = any(a.get("layer") == "data_integrity" for a in yellow_alerts)
        non_bar_warns = [
            w for w in data_warnings
            if not w.startswith((
                "market_snapshot:",
                "daily_bar:",
                "intraday_30m:",
                "chanlun_daily:",
                "chanlun_30m:",
                "contract_quote:",
                "intraday_ohlc_window_gap_since_effective_ts:",
                "intraday_internal_bar_gap:",
            ))
        ]
        if (not has_integrity_alert) or non_bar_warns:
            yellow_alerts.append({
                "layer": "data_integrity",
                "code": "data_source_degraded",
                "message": "存在数据源异常/部分缺失/过期，禁止误报绿灯",
                "detail": "；".join(non_bar_warns if (has_integrity_alert and non_bar_warns) else data_warnings),
            })

    # ------------------------------------------------------------------
    # Traffic-Light Verdict + Positive Preconditions Gate (Zero False Greens)
    # ------------------------------------------------------------------
    green_preconditions = {
        "valid_fresh_price": (last_price is not None) and (not daily_bar_missing) and (not daily_bar_stale),
        "fresh_intraday_30m": (
            (not intraday_missing_or_failed)
            and (not intraday_lags_daily)
            and (not intraday_stale)
            and (not intraday_timestamp_invalid)
            and (not intraday_bar_unclosed)
        ),
        "chanlun_both_present": daily_cl_valid and intra_cl_valid,
        "all_armed_triggers_evaluated": len(unevaluated_triggers) == 0,
        "governance_satisfied": len(armed_triggers) > 0 or (allow_manual_red_lines and has_red_lines),
        "no_data_warnings": len(data_warnings) == 0,
        "contract_quote_verified": not contract_quote_mismatch,
        "valid_direction": norm_dir in {"long", "short"},
        "stops_configured": (tactical_stop_30m is not None or structural_stop_daily is not None),
    }

    if not red_alerts and not yellow_alerts and not all(green_preconditions.values()):
        failed_keys = [k for k, ok in green_preconditions.items() if not ok]
        yellow_alerts.append({
            "layer": "data_integrity",
            "code": "green_precondition_failed",
            "message": "绿灯前置条件未全部满足，禁止误报绿灯",
            "detail": f"未通过项: {', '.join(failed_keys)}",
        })

    if red_alerts:
        status = STATUS_RED
        action_headline = red_alerts[0]["message"]
    elif yellow_alerts:
        status = STATUS_YELLOW
        action_headline = yellow_alerts[0]["message"]
    else:
        status = STATUS_GREEN
        action_headline = "日线与 30min 结构稳固、核心触发器已核验且未松动，按计划持有"

    return _sanitize_for_json({
        "symbol": norm_sym,
        "name": str(pos.get("name") or inst.get("name") or ""),
        "market": str(pos.get("market") or inst.get("market") or ""),
        "asset_class": asset_class or "",
        "direction": norm_dir or "unknown",
        "sector_theme": pos.get("sector_theme"),
        "leader_role": pos.get("leader_role"),
        "contract_or_expression": contract_expr,
        "traded_contract_symbol": traded_contract_sym or explicit_contract_sym,
        "quote_symbol": (price_snap.get("symbol") if isinstance(price_snap, dict) else None),
        "position_id": pos.get("position_id"),
        "exit_red_lines": normalized_red_lines,
        "status": status,
        "status_zh": STATUS_ZH_MAP[status],
        "action_headline": action_headline,
        "last_price": _round_finite(last_price, 4),
        "price_source": price_source,
        "entry_price": _round_finite(entry_price, 4),
        "tactical_stop_30m": _round_finite(tactical_stop_30m, 4),
        "structural_stop_daily": _round_finite(structural_stop_daily, 4),
        "target_price": _round_finite(target_price, 4),
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "red_alerts": red_alerts,
        "yellow_alerts": yellow_alerts,
        "info_notes": info_notes,
        "suggested_trail_stop": suggested_trail_stop,
        "trigger_results": trigger_results,
        "verifiable_trigger_ids": verifiable_trigger_ids,
        "upcoming_events": upcoming_events,
        "data_warnings": data_warnings,
        "green_preconditions": green_preconditions,
        "observations_summary": observations,
        "checked_at_utc": ra_mod.utc_now_str(),
    })


# ---------------------------------------------------------------------------
# 4. Portfolio & Archive Discovery + Batch Runner
# ---------------------------------------------------------------------------

def _load_all_positions(
    portfolio_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Load both ``open`` and ``closed`` positions via ``portfolio_manager.load_positions(status=None)``.

    Returns ``(open_positions, closed_positions, discovery_errors)`` so sentinel can distinguish
    legitimate archive-only holdings from stale ``holding`` archives whose ledger position is closed.
    """
    errors: list[str] = []
    items: list[dict[str, Any]] | None = None

    try:
        import portfolio_manager as pm_mod  # type: ignore[import-not-found]
        if hasattr(pm_mod, "load_positions"):
            loaded = pm_mod.load_positions(portfolio_dir=portfolio_dir, status=None)
            items = list(loaded) if isinstance(loaded, list) else []
    except ImportError:
        items = None
    except FileNotFoundError:
        return [], [], []
    except Exception as exc:  # noqa: BLE001 — catch sqlite3.DatabaseError & any corrupt DB/JSON error
        errors.append(f"portfolio_positions: failed to load positions ({type(exc).__name__}: {exc})")
        return [], [], errors

    if items is None:
        pdir = os.path.abspath(portfolio_dir or DEFAULT_PORTFOLIO_DIR)
        pos_path = os.path.join(pdir, "positions.json")
        if not os.path.exists(pos_path):
            return [], [], []
        try:
            with open(pos_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"portfolio_positions ({pos_path}): failed to parse JSON ({type(exc).__name__}: {exc})")
            return [], [], errors
        if isinstance(raw, dict):
            raw_items = raw.get("positions")
            items = list(raw_items) if isinstance(raw_items, list) else []
        elif isinstance(raw, list):
            items = list(raw)
        else:
            errors.append(f"portfolio_positions ({pos_path}): unexpected root type {type(raw).__name__}")
            return [], [], errors

    open_pos: list[dict[str, Any]] = []
    closed_pos: list[dict[str, Any]] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        st = str(p.get("status") or "open").strip().lower()
        if st == "closed":
            closed_pos.append(p)
        elif st == "open":
            open_pos.append(p)
    return open_pos, closed_pos, errors


def _load_open_positions(portfolio_dir: str | None = None) -> list[dict[str, Any]]:
    """Backward-compatible wrapper returning open positions."""
    open_pos, _, _ = _load_all_positions(portfolio_dir=portfolio_dir)
    return open_pos


def _default_snapshot_fetcher(symbol: str) -> dict[str, Any]:
    try:
        sig = inspect.signature(ms_mod.get_snapshot)
        kwargs: dict[str, Any] = {
            "period": "3y",
            "include_macro": False,
            "include_fundamentals": True,
        }
        if "full_chanlun" in sig.parameters:
            kwargs["full_chanlun"] = False
        if "include_intraday" in sig.parameters:
            kwargs["include_intraday"] = True
        if "intraday_interval" in sig.parameters:
            kwargs["intraday_interval"] = "30m"
        if "refresh" in sig.parameters:
            kwargs["refresh"] = True
        return ms_mod.get_snapshot(symbol, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Snapshot fetch failed for {symbol}: {exc}"}


def _default_research_fetcher(symbol: str) -> dict[str, Any]:
    try:
        rd_sig = inspect.signature(rd_mod.collect_research_data)
        rd_kwargs: dict[str, Any] = {
            "raw_symbol": symbol,
            "period": "3y",
            "include_macro": False,
            "full_chanlun": False,
        }
        if "max_age_seconds" in rd_sig.parameters:
            rd_kwargs["max_age_seconds"] = 120.0
        return rd_mod.collect_research_data(**rd_kwargs)
    except Exception as exc:  # noqa: BLE001
        return {
            "symbol": symbol,
            "fundamentals_and_supply": {},
            "sources_status": {"research_data": {"status": "error", "error": str(exc)}},
        }


def _safe_load_archive_state(
    raw_symbol: str,
    research_root: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Load archive state; distinguishes missing archive (no error) from corrupted/unreadable archive."""
    try:
        loaded = ra_mod.load_archive(raw_symbol, research_root=research_root, evidence_limit=0)
        return loaded.get("state"), loaded.get("symbol"), None
    except FileNotFoundError:
        return None, None, None
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return None, None, f"research_archive ({raw_symbol}): failed to load archive ({exc})"


def _build_surgical_trigger_patch(
    fresh_state: dict[str, Any],
    trigger_results: list[dict[str, Any]],
    checked_ts: str,
) -> dict[str, Any]:
    """Merge ONLY ``last_checked_at``, ``last_value``, and ``status`` onto matching trigger IDs
    of ``fresh_state``, preserving concurrent edits to ``threshold``/``action``/``operator`` and
    newly added triggers.
    """
    patch: dict[str, Any] = {
        "source_watermarks": {"daily_sentinel": checked_ts},
    }
    fresh_triggers = fresh_state.get("triggers") if isinstance(fresh_state, dict) else None
    if isinstance(fresh_triggers, list) and fresh_triggers:
        res_by_id: dict[str, dict[str, Any]] = {}
        for tr in trigger_results:
            if isinstance(tr, dict) and tr.get("id") is not None:
                tid_key = str(tr["id"]).strip()
                if tid_key:
                    res_by_id[tid_key] = tr

        updated_triggers: list[dict[str, Any]] = []
        for raw_t in fresh_triggers:
            if not isinstance(raw_t, dict):
                continue
            t_copy = copy.deepcopy(raw_t)
            tid = str(t_copy.get("id") or "").strip()
            matched = res_by_id.get(tid) if tid else None
            metric_matches = (
                isinstance(matched, dict)
                and (not matched.get("metric") or t_copy.get("metric") == matched.get("metric"))
            )
            if _is_trigger_armed(t_copy) and isinstance(matched, dict) and metric_matches:
                t_copy["last_checked_at"] = checked_ts
                if matched.get("evaluated") is True:
                    obs_val = matched.get("observed")
                    t_copy["last_value"] = _sanitize_for_json(obs_val)
                    # Re-evaluate against fresh threshold/operator in case they changed concurrently
                    try:
                        eval_src = matched.get("evaluation_source")
                        fresh_obs = (
                            {str(t_copy.get("metric") or ""): obs_val}
                            if eval_src in (None, "live")
                            else {}
                        )
                        fresh_eval = ra_mod.evaluate_triggers(
                            {"triggers": [t_copy]},
                            fresh_obs,
                        )
                        if fresh_eval and fresh_eval[0].get("evaluated") is True:
                            t_copy["status"] = "fired" if fresh_eval[0].get("fired") else "armed"
                        else:
                            t_copy["status"] = "fired" if matched.get("fired") else "unevaluated"
                    except Exception:  # noqa: BLE001
                        t_copy["status"] = "fired" if matched.get("fired") else "armed"
                else:
                    if "last_value" not in t_copy:
                        t_copy["last_value"] = None
                    t_copy["status"] = "unevaluated"
            updated_triggers.append(_sanitize_for_json(t_copy))

        if updated_triggers:
            patch["triggers"] = updated_triggers

    return patch


def _update_archive_state_from_sentinel(
    arch_sym: str,
    archive_state: dict[str, Any] | None,
    trigger_results: list[dict[str, Any]],
    checked_ts: str,
    research_root: str | None = None,
) -> dict[str, Any]:
    """Update ``state.json`` triggers (``last_checked_at``, ``last_value``, ``status``) and
    ``source_watermarks['daily_sentinel']`` atomically via ``research_archive.update_state``.

    Concurrency-safe: re-loads fresh ``state.json`` immediately before writing, surgically merges
    ONLY ``last_checked_at`` / ``last_value`` / ``status`` onto matching trigger IDs, passes
    ``expected_revision=fresh_state["revision"]``, and retries once on revision conflict.
    """
    for attempt in range(2):
        loaded = ra_mod.load_archive(arch_sym, research_root=research_root, evidence_limit=0)
        fresh_state = (
            loaded.get("state")
            if isinstance(loaded, dict) and isinstance(loaded.get("state"), dict)
            else (archive_state if isinstance(archive_state, dict) else {})
        )
        raw_rev = fresh_state.get("revision")
        expected_rev = int(raw_rev) if raw_rev is not None else None
        patch = _build_surgical_trigger_patch(fresh_state, trigger_results, checked_ts)

        try:
            return ra_mod.update_state(
                arch_sym,
                patch,
                expected_revision=expected_rev,
                research_root=research_root,
            )
        except ValueError as exc:
            if "revision conflict" in str(exc).lower():
                if attempt == 0:
                    continue
                raise
            # Fallback to watermark-only update if legacy archive has non-standard trigger schema
            try:
                return ra_mod.update_state(
                    arch_sym,
                    {"source_watermarks": {"daily_sentinel": checked_ts}},
                    expected_revision=expected_rev,
                    research_root=research_root,
                )
            except ValueError as wm_exc:
                if "revision conflict" in str(wm_exc).lower() and attempt == 0:
                    continue
                raise

    raise RuntimeError(f"Failed to update archive state for {arch_sym} after retry")


def _evaluate_watchlist_item(
    symbol: str,
    archive_state: dict[str, Any],
    snapshot: dict[str, Any] | None,
    research_bundle: dict[str, Any] | None,
    ref_date: date,
    event_window_days: int = 14,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Lightweight refresh summary for a ``watchlist`` archive (kept separate from holdings)."""
    inst = archive_state.get("instrument") if isinstance(archive_state.get("instrument"), dict) else {}
    raw_obs = extract_sentinel_observations(
        research_bundle,
        snapshot,
        ref_date=ref_date,
        now_utc=now_utc,
    )
    data_warnings = list(raw_obs.get("data_health_warnings") or [])
    observations = {k: v for k, v in raw_obs.items() if k != "data_health_warnings"}

    triggers_list = archive_state.get("triggers") if isinstance(archive_state.get("triggers"), list) else []
    armed_triggers = [t for t in triggers_list if _is_trigger_armed(t)]
    trig_results = ra_mod.evaluate_triggers({"triggers": armed_triggers}, observations) if armed_triggers else []
    fired = [t for t in trig_results if t.get("fired")]

    upcoming: list[dict[str, Any]] = []
    for ev in (archive_state.get("events") or []):
        if not isinstance(ev, dict):
            continue
        if str(ev.get("status") or "upcoming").strip().lower() != "upcoming":
            continue
        ev_dt = _parse_iso_date(ev.get("date"))
        title = str(ev.get("title") or "").strip()
        if ev_dt is not None and title:
            days = (ev_dt - ref_date).days
            if 0 <= days <= event_window_days:
                upcoming.append({
                    "date": ev_dt.isoformat(),
                    "days_until": days,
                    "title": title,
                    "imminent": days <= 3,
                })

    return _sanitize_for_json({
        "symbol": inst.get("symbol") or symbol,
        "name": inst.get("name") or "",
        "market": inst.get("market") or "",
        "direction_bias": archive_state.get("direction_bias") or "neutral",
        "last_price": observations.get("close"),
        "daily_signal": observations.get("daily_signal"),
        "intraday_30m_signal": observations.get("intraday_30m_signal"),
        "fired_triggers": fired,
        "trigger_results": trig_results,
        "upcoming_events": upcoming,
        "data_warnings": data_warnings,
        "observations_summary": observations,
        "checked_at_utc": ra_mod.utc_now_str(),
    })


def run_daily_sentinel(
    portfolio_dir: str | None = None,
    research_root: str | None = None,
    symbols: list[str] | None = None,
    include_watchlist: bool = False,
    skip_research: bool = False,
    update_archive: bool = False,
    log_green: bool = False,
    ref_date: date | None = None,
    event_window_days: int = 14,
    snapshot_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    research_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    now_utc: datetime | None = None,
    allow_manual_red_lines: bool = False,
) -> dict[str, Any]:
    """Run Holding Daily Sentinel across discovered holdings and optional watchlist."""
    explicit_now_utc: datetime | None = None
    if isinstance(now_utc, datetime):
        explicit_now_utc = (
            now_utc.astimezone(timezone.utc)
            if now_utc.tzinfo is not None
            else now_utc.replace(tzinfo=timezone.utc)
        )

    def _resolve_eval_clock() -> tuple[date, datetime | None]:
        if explicit_now_utc is not None:
            return (ref_date or explicit_now_utc.date()), explicit_now_utc
        if ref_date is not None:
            return ref_date, None
        live_now = datetime.now(timezone.utc)
        return live_now.date(), live_now

    anchor, _ = _resolve_eval_clock()
    snap_fn = snapshot_fetcher or _default_snapshot_fetcher
    res_fn = research_fetcher or _default_research_fetcher

    open_positions, closed_positions, discovery_errors = _load_all_positions(portfolio_dir=portfolio_dir)
    all_archives = ra_mod.list_archives(research_root=research_root)
    state_sync_warnings: list[dict[str, Any]] = []

    # Surface ALL corrupted archives from list_archives — NEVER silently swallow!
    for arch_meta in all_archives:
        if arch_meta.get("error"):
            discovery_errors.append(
                f"research_archive ({arch_meta.get('archive_dir', '?')}): {arch_meta['error']}"
            )

    # Build lookup maps of open vs closed-only positions
    pos_by_norm: dict[str, list[dict[str, Any]]] = {}
    open_syms: set[str] = set()
    for p in open_positions:
        raw_s = str(p.get("symbol") or "").strip()
        if not raw_s:
            continue
        ns, _ = _safe_normalize_symbol(raw_s)
        open_syms.add(ns)
        open_syms.add(raw_s.upper())
        pos_by_norm.setdefault(ns, []).append(p)
        arch_s = str(p.get("archive_symbol") or "").strip()
        if arch_s:
            na, _ = _safe_normalize_symbol(arch_s)
            open_syms.add(na)
            open_syms.add(arch_s.upper())
            if na != ns:
                pos_by_norm.setdefault(na, []).append(p)
        cs = str(p.get("contract_symbol") or "").strip().upper()
        if cs:
            open_syms.add(cs)

    closed_only_syms: set[str] = set()
    for cp in closed_positions:
        for field in ("symbol", "archive_symbol"):
            raw_cs = str(cp.get(field) or "").strip()
            if not raw_cs:
                continue
            ncs, _ = _safe_normalize_symbol(raw_cs)
            if ncs not in open_syms and raw_cs.upper() not in open_syms:
                closed_only_syms.add(ncs)
                closed_only_syms.add(raw_cs.upper())

    # Assemble holding targets (deduplicated between positions.json and research/ holding archives)
    holding_targets: list[dict[str, Any]] = []
    covered_archive_syms: set[str] = set()

    if symbols:
        seen_explicit: set[str] = set()
        for raw_s in symbols:
            ns, _ = _safe_normalize_symbol(raw_s)
            if ns in seen_explicit:
                continue
            seen_explicit.add(ns)
            matched_pos = (pos_by_norm.get(ns) or [None])[0]
            arch_lookup = (matched_pos.get("archive_symbol") if matched_pos else None) or ns
            arch_state, arch_sym, load_err = _safe_load_archive_state(arch_lookup, research_root=research_root)
            if load_err:
                discovery_errors.append(load_err)
            direction = (
                (matched_pos.get("direction") if matched_pos else None)
                or (arch_state.get("direction_bias") if arch_state else None)
                or "long"
            )
            holding_targets.append({
                "symbol": ns,
                "research_symbol": arch_sym or ns,
                "archive_symbol": arch_sym,
                "direction": direction,
                "position": matched_pos,
                "archive_state": arch_state,
                "source": "explicit",
            })
            covered_archive_syms.add(ns)
            if arch_sym:
                covered_archive_syms.add(arch_sym)
    else:
        seen_pos_keys: set[tuple[str, str]] = set()
        for p in open_positions:
            raw_s = str(p.get("symbol") or "").strip()
            if not raw_s:
                continue
            ns, _ = _safe_normalize_symbol(raw_s)
            pid = str(p.get("position_id") or "")
            if (ns, pid) in seen_pos_keys:
                continue
            seen_pos_keys.add((ns, pid))

            arch_lookup = str(p.get("archive_symbol") or ns).strip()
            norm_arch, _ = _safe_normalize_symbol(arch_lookup)
            arch_state, arch_sym, load_err = _safe_load_archive_state(arch_lookup, research_root=research_root)
            if load_err:
                discovery_errors.append(load_err)

            covered_archive_syms.add(ns)
            covered_archive_syms.add(norm_arch)
            if arch_sym:
                covered_archive_syms.add(arch_sym)

            holding_targets.append({
                "symbol": ns,
                "research_symbol": arch_sym or norm_arch,
                "archive_symbol": arch_sym,
                "direction": p.get("direction") or (arch_state.get("direction_bias") if arch_state else None),
                "position": p,
                "archive_state": arch_state,
                "source": "portfolio+archive" if arch_state is not None else "portfolio",
            })

        for arch_meta in all_archives:
            if arch_meta.get("error") or arch_meta.get("status") != "holding":
                continue
            raw_sym = str(arch_meta.get("symbol") or "").strip()
            if not raw_sym:
                continue
            ns, _ = _safe_normalize_symbol(raw_sym)
            if ns in covered_archive_syms or raw_sym.upper() in covered_archive_syms:
                continue

            # Ledger closed vs stale archive holding -> record sync warning, do NOT monitor as ghost holding!
            if ns in closed_only_syms or raw_sym.upper() in closed_only_syms:
                covered_archive_syms.add(ns)
                covered_archive_syms.add(raw_sym.upper())
                state_sync_warnings.append({
                    "symbol": ns,
                    "code": "closed_position_stale_holding_archive",
                    "message": "账本已平仓但研究档案仍标记 holding，请同步更新档案状态",
                    "detail": (
                        f"{ns}: portfolio 账本已平仓（无在途 open 持仓），"
                        "但 research/ 档案仍为 status=holding；本次已跳过幽灵持仓巡检"
                    ),
                    "archive_dir": arch_meta.get("archive_dir"),
                })
                continue

            arch_state, arch_sym, load_err = _safe_load_archive_state(raw_sym, research_root=research_root)
            if load_err:
                discovery_errors.append(load_err)
                continue
            if arch_state is None:
                continue
            covered_archive_syms.add(ns)
            if arch_sym:
                covered_archive_syms.add(arch_sym)
            holding_targets.append({
                "symbol": arch_sym or ns,
                "research_symbol": arch_sym or ns,
                "archive_symbol": arch_sym or ns,
                "direction": arch_state.get("direction_bias") or "long",
                "position": None,
                "archive_state": arch_state,
                "source": "archive_holding",
            })

    # Cache fetched data by symbol within a single run
    snap_cache: dict[str, dict[str, Any] | None] = {}
    res_cache: dict[str, dict[str, Any] | None] = {}

    def _fetch_snap(sym: str) -> dict[str, Any] | None:
        if sym not in snap_cache:
            try:
                snap_cache[sym] = snap_fn(sym)
            except Exception as exc:  # noqa: BLE001
                snap_cache[sym] = {"error": str(exc)}
        return snap_cache.get(sym)

    def _fetch_res(sym: str) -> dict[str, Any] | None:
        if skip_research:
            return None
        if sym not in res_cache:
            try:
                res_cache[sym] = res_fn(sym)
            except Exception as exc:  # noqa: BLE001
                res_cache[sym] = {
                    "symbol": sym,
                    "fundamentals_and_supply": {},
                    "sources_status": {"research_data": {"status": "error", "error": str(exc)}},
                }
        return res_cache.get(sym)

    holding_reports: list[dict[str, Any]] = []
    for tgt in holding_targets:
        sym = tgt["symbol"]
        research_sym = tgt.get("research_symbol") or sym
        traded_sym = _resolve_traded_contract(sym, tgt["position"], tgt["archive_state"], ref_date=anchor)

        base_snap = _fetch_snap(research_sym)
        rbundle = _fetch_res(research_sym)

        contract_snap: dict[str, Any] | None = None
        if traded_sym and traded_sym != research_sym.upper():
            contract_snap = _fetch_snap(traded_sym)
        elif sym.upper() != research_sym.upper():
            contract_snap = _fetch_snap(sym)

        item_anchor, item_now_utc = _resolve_eval_clock()
        report = evaluate_holding_health(
            symbol=sym,
            direction=tgt["direction"],
            position=tgt["position"],
            archive_state=tgt["archive_state"],
            snapshot=base_snap,
            research_bundle=rbundle,
            ref_date=item_anchor,
            event_window_days=event_window_days,
            contract_snapshot=contract_snap,
            now_utc=item_now_utc,
            allow_manual_red_lines=allow_manual_red_lines,
        )
        report["discovery_source"] = tgt["source"]

        # Optional archive state + revision log update
        arch_sym = tgt.get("archive_symbol")
        if update_archive and arch_sym:
            checked_ts = report.get("checked_at_utc") or ra_mod.utc_now_str()
            try:
                _update_archive_state_from_sentinel(
                    arch_sym=arch_sym,
                    archive_state=tgt.get("archive_state"),
                    trigger_results=report.get("trigger_results") or [],
                    checked_ts=checked_ts,
                    research_root=research_root,
                )
                report["archive_state_updated"] = True
            except Exception as exc:  # noqa: BLE001
                report["archive_state_error"] = str(exc)

            should_log = (report["status"] in {STATUS_RED, STATUS_YELLOW}) or log_green
            if should_log:
                px_str = f"{report['last_price']}" if report["last_price"] is not None else "N/A"
                alert_count = len(report["red_alerts"]) + len(report["yellow_alerts"])
                log_entry = (
                    f"[每日哨兵 {report['status_zh']}] 现价={px_str} | "
                    f"告警数={alert_count} | {report['action_headline']}"
                )
                try:
                    appended_line = ra_mod.append_log(arch_sym, log_entry, research_root=research_root)
                    report["archive_log_appended"] = appended_line
                except Exception as exc:  # noqa: BLE001
                    report["archive_log_error"] = str(exc)

        holding_reports.append(report)

    holding_reports.sort(key=lambda r: (STATUS_PRIORITY.get(r["status"], 9), r["symbol"]))

    # Optional Watchlist light refresh (strictly separated from holdings)
    watchlist_reports: list[dict[str, Any]] = []
    if include_watchlist and not symbols:
        for arch_meta in all_archives:
            if arch_meta.get("error") or arch_meta.get("status") != "watchlist":
                continue
            raw_sym = str(arch_meta.get("symbol") or "").strip()
            if not raw_sym:
                continue
            ns, _ = _safe_normalize_symbol(raw_sym)
            if ns in covered_archive_syms or raw_sym.upper() in covered_archive_syms:
                continue
            arch_state, arch_sym, load_err = _safe_load_archive_state(raw_sym, research_root=research_root)
            if load_err:
                discovery_errors.append(load_err)
                continue
            if arch_state is None:
                continue
            target_sym = arch_sym or ns
            snap = _fetch_snap(target_sym)
            rbundle = _fetch_res(target_sym)
            wl_anchor, wl_now_utc = _resolve_eval_clock()
            wl_item = _evaluate_watchlist_item(
                symbol=target_sym,
                archive_state=arch_state,
                snapshot=snap,
                research_bundle=rbundle,
                ref_date=wl_anchor,
                event_window_days=event_window_days,
                now_utc=wl_now_utc,
            )

            if update_archive and (arch_sym or target_sym):
                wl_arch_sym = arch_sym or target_sym
                checked_ts = wl_item.get("checked_at_utc") or ra_mod.utc_now_str()
                try:
                    _update_archive_state_from_sentinel(
                        arch_sym=wl_arch_sym,
                        archive_state=arch_state,
                        trigger_results=wl_item.get("trigger_results") or [],
                        checked_ts=checked_ts,
                        research_root=research_root,
                    )
                    wl_item["archive_state_updated"] = True
                except Exception as exc:  # noqa: BLE001
                    wl_item["archive_state_error"] = str(exc)

                px_str = f"{wl_item['last_price']}" if wl_item.get("last_price") is not None else "N/A"
                fired_list = wl_item.get("fired_triggers") or []
                total_trigs = len(wl_item.get("trigger_results") or [])
                fired_ids = ",".join(str(t.get("id") or t.get("metric")) for t in fired_list) if fired_list else "无"
                wl_log = (
                    f"[观察池轻刷] 现价={px_str} | 命中触发器={len(fired_list)}/{total_trigs}({fired_ids}) | "
                    f"日线={wl_item.get('daily_signal') or '-'}"
                )
                try:
                    wl_item["archive_log_appended"] = ra_mod.append_log(
                        wl_arch_sym, wl_log, research_root=research_root
                    )
                except Exception as exc:  # noqa: BLE001
                    wl_item["archive_log_error"] = str(exc)

            watchlist_reports.append(wl_item)
        watchlist_reports.sort(key=lambda r: (0 if r.get("fired_triggers") else 1, r.get("symbol", "")))

    discovery_errors = list(dict.fromkeys(discovery_errors))
    red_count = sum(1 for r in holding_reports if r["status"] == STATUS_RED)
    yellow_count = sum(1 for r in holding_reports if r["status"] == STATUS_YELLOW)
    green_count = sum(1 for r in holding_reports if r["status"] == STATUS_GREEN)
    wl_fired = sum(1 for r in watchlist_reports if r.get("fired_triggers"))

    return _sanitize_for_json({
        "checked_at_utc": ra_mod.utc_now_str(),
        "ref_date": anchor.isoformat(),
        "summary": {
            "holdings_total": len(holding_reports),
            "red_count": red_count,
            "yellow_count": yellow_count,
            "green_count": green_count,
            "watchlist_total": len(watchlist_reports),
            "watchlist_fired_count": wl_fired,
            "discovery_errors_count": len(discovery_errors),
            "state_sync_warnings_count": len(state_sync_warnings),
        },
        "discovery_errors": discovery_errors,
        "state_sync_warnings": state_sync_warnings,
        "holdings": holding_reports,
        "watchlist": watchlist_reports,
    })


# ---------------------------------------------------------------------------
# 5. Markdown Formatter
# ---------------------------------------------------------------------------

def _fmt_num(val: Any, suffix: str = "") -> str:
    if val is None:
        return "-"
    return f"{val}{suffix}"


def format_sentinel_markdown(report: dict[str, Any]) -> str:
    """Render structured sentinel report into clean, scannable Chinese Markdown."""
    summary = report.get("summary") or {}
    holdings = report.get("holdings") or []
    watchlist = report.get("watchlist") or []
    discovery_errors = report.get("discovery_errors") or []
    state_sync_warnings = report.get("state_sync_warnings") or []

    overview_line = (
        f"- **持仓总览**：共 `{summary.get('holdings_total', len(holdings))}` 笔 "
        f"| 🔴 撤离 `{summary.get('red_count', 0)}` "
        f"| 🟡 警戒 `{summary.get('yellow_count', 0)}` "
        f"| 🟢 稳固 `{summary.get('green_count', 0)}`"
    )
    if discovery_errors or state_sync_warnings:
        overview_line += (
            f" | 档案异常 `{len(discovery_errors)}` "
            f"| 状态同步告警 `{len(state_sync_warnings)}`"
        )

    lines: list[str] = [
        f"# 持仓每日哨兵巡检报告 ({report.get('ref_date', '')})",
        "",
        f"- **巡检时间**：`{report.get('checked_at_utc', '')}`",
        overview_line,
        "",
    ]

    if discovery_errors or state_sync_warnings:
        lines.append("## 【档案发现与状态同步告警】")
        lines.append("")
        for err in discovery_errors:
            lines.append(f"- **档案损坏/读取失败**：`{err}`")
        for sw in state_sync_warnings:
            if isinstance(sw, dict):
                lines.append(f"- **状态不同步 (`{sw.get('symbol', '?')}`)**：{sw.get('message')}（{sw.get('detail')}）")
            else:
                lines.append(f"- **状态不同步**：{sw}")
        lines.append("")

    lines.extend([
        "## 【持仓每日强制巡检】",
        "",
    ])

    if not holdings:
        lines.append("当前无活跃持仓（`portfolio/positions.json` 与 `research/` holding 档案均为空）。")
    else:
        lines.append("| 标的 | 方向 | 状态 | 现价 / 浮盈 | 防线 (30m / 日线) | 核心执行指令 |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :--- |")
        for h in holdings:
            sym_lbl = f"`{h['symbol']}`" + (f" {h['name']}" if h.get("name") else "")
            if h.get("traded_contract_symbol") and h["traded_contract_symbol"] != h["symbol"]:
                sym_lbl += f" [{h['traded_contract_symbol']}]"
            dir_lbl = "做多" if h.get("direction") == "long" else ("做空" if h.get("direction") == "short" else str(h.get("direction")))
            pnl = h.get("unrealized_pnl_pct")
            pnl_txt = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "-"
            px_pnl = f"{_fmt_num(h.get('last_price'))} ({pnl_txt})"
            stops = f"{_fmt_num(h.get('tactical_stop_30m'))} / {_fmt_num(h.get('structural_stop_daily'))}"
            lines.append(
                f"| {sym_lbl} | {dir_lbl} | {h.get('status_zh', h.get('status'))} | {px_pnl} | {stops} | {h.get('action_headline', '')} |"
            )

        lines.append("")
        for h in holdings:
            sym_title = f"`{h['symbol']}`" + (f" ({h['name']})" if h.get("name") else "")
            lines.append(f"### {sym_title} · {h.get('status_zh', h.get('status'))}")
            meta_bits = [
                f"方向=`{h.get('direction')}`",
                f"现价=`{_fmt_num(h.get('last_price'))}`",
            ]
            if h.get("traded_contract_symbol"):
                meta_bits.append(f"实盘合约=`{h['traded_contract_symbol']}`")
            if h.get("entry_price") is not None:
                meta_bits.append(f"成本=`{h['entry_price']}`")
            if h.get("unrealized_pnl_pct") is not None:
                meta_bits.append(f"浮盈=`{h['unrealized_pnl_pct']:+.2f}%`")
            if h.get("contract_or_expression"):
                meta_bits.append(f"表达=`{h['contract_or_expression']}`")
            if h.get("sector_theme"):
                meta_bits.append(f"主线=`{h['sector_theme']}`")
            lines.append("- " + " | ".join(meta_bits))

            for a in h.get("red_alerts") or []:
                lines.append(f"- **🔴 撤离红线**：{a.get('message')}（{a.get('detail')}）")
            for a in h.get("yellow_alerts") or []:
                lines.append(f"- **🟡 警戒松动**：{a.get('message')}（{a.get('detail')}）")

            red_lines = h.get("exit_red_lines") or []
            if red_lines:
                lines.append(f"- **人工撤离红线**：{'；'.join(str(x) for x in red_lines)}")

            trail = h.get("suggested_trail_stop")
            if isinstance(trail, dict):
                lines.append(
                    f"- **追踪止损建议**：`{trail.get('current_stop')}` -> `{trail.get('suggested_stop')}`"
                    f"（{trail.get('rationale')}）"
                )

            for note in h.get("info_notes") or []:
                if isinstance(trail, dict) and note == trail.get("rationale"):
                    continue
                lines.append(f"- 备注：{note}")

            for ev in h.get("upcoming_events") or []:
                tag = "临近" if ev.get("imminent") else "提醒"
                lines.append(f"- 事件[{tag}]：`{ev.get('date')}`（{ev.get('days_until')}天后）{ev.get('title')}")

            for dw in h.get("data_warnings") or []:
                lines.append(f"- 数据完整性告警：`{dw}`")
            lines.append("")

    if watchlist:
        lines.append("## 【观察池轻量刷新】")
        lines.append("")
        lines.append("| 标的 | 方向倾向 | 现价 | 命中触发器 | 日线 / 30m 信号摘要 |")
        lines.append("| :--- | :---: | :---: | :---: | :--- |")
        for w in watchlist:
            sym_lbl = f"`{w['symbol']}`" + (f" {w['name']}" if w.get("name") else "")
            fired = w.get("fired_triggers") or []
            fired_txt = ", ".join(str(t.get("id") or t.get("metric")) for t in fired) if fired else "无"
            sig_parts = [p for p in (w.get("daily_signal"), w.get("intraday_30m_signal")) if p]
            sig_txt = " / ".join(sig_parts) if sig_parts else "-"
            lines.append(
                f"| {sym_lbl} | {w.get('direction_bias', 'neutral')} | {_fmt_num(w.get('last_price'))} | {fired_txt} | {sig_txt} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 6. CLI Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Layer-4 Holding Daily Sentinel (持仓每日必检、逻辑或指标松动果断撤离)."
    )
    parser.add_argument("--portfolio-dir", default=None, help="Override portfolio directory containing positions.json")
    parser.add_argument("--research-root", default=None, help="Override research archive root directory")
    parser.add_argument("--symbols", nargs="+", default=None, help="Explicitly inspect specific symbols")
    parser.add_argument("--include-watchlist", action="store_true", help="Also run light refresh on watchlist archives")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")
    parser.add_argument("--skip-research", action="store_true", help="Skip Layer-3 research_data fetch (price/Chanlun only)")
    parser.add_argument(
        "--update-archive",
        action="store_true",
        help="Update state.json watermarks/triggers and append revision log to thesis.md (holdings & watchlist)",
    )
    parser.add_argument("--log-green", action="store_true", help="Also append archive log for GREEN holdings when --update-archive is set")
    parser.add_argument(
        "--allow-manual-red-lines",
        action="store_true",
        help="Allow free-text exit_red_lines alone (without armed machine triggers) to pass governance for GREEN_HOLD",
    )
    parser.add_argument(
        "--fail-on-red",
        action="store_true",
        help="Exit with code 2 when any holding evaluates to RED_EXIT or archive discovery errors occur",
    )
    parser.add_argument("--event-window-days", type=int, default=14, help="Upcoming event/roll lookahead window in days (default: 14)")
    parser.add_argument("--ref-date", default=None, help="Optional reference date YYYY-MM-DD for deterministic evaluation")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    ref_dt: date | None = None
    if args.ref_date:
        ref_dt = _parse_iso_date(args.ref_date)
        if ref_dt is None:
            print(f"Error: invalid --ref-date {args.ref_date!r}; expected YYYY-MM-DD", file=sys.stderr)
            return 1

    report = run_daily_sentinel(
        portfolio_dir=args.portfolio_dir,
        research_root=args.research_root,
        symbols=args.symbols,
        include_watchlist=args.include_watchlist,
        skip_research=args.skip_research,
        update_archive=args.update_archive,
        log_green=args.log_green,
        ref_date=ref_dt,
        event_window_days=args.event_window_days,
        allow_manual_red_lines=args.allow_manual_red_lines,
    )

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_sentinel_markdown(report), end="")

    summary = report.get("summary") or {}
    disc_errs = report.get("discovery_errors") or []
    if disc_errs:
        print(
            f"[daily_sentinel] WARNING: {len(disc_errs)} discovery error(s): " + "; ".join(disc_errs),
            file=sys.stderr,
        )

    if args.fail_on_red and (summary.get("red_count", 0) > 0 or len(disc_errs) > 0):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
