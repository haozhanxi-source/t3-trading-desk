"""Layer 1 Top-Down Strategic Compass (Macro Dual-Core + 7-Dimension AI Synthesis).

Bridges raw universe scanning (Layer 2) and the project's top-down philosophy:
1. Free hard-data foundation (zero AI cost): US-China macro dual-core (FRED + NBS/Customs +
   live VIX/US10Y/DXY) plus 17 cross-asset pricing anchors (strict 5D / 20D / 60D returns).
2. On-demand headless AI 7-dimension top-down synthesis (Claude Code / Codex with live web
   search) producing validated 1Q~1Y directional thesis cards (Long / Short / Pair),
   explicit adverse sectors to avoid, normalized single-sector true leaders, and
   direction-aware one-click handoff into Layer 2 daily Chanlun screening.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

import data_store
import market_snapshot
import portfolio_manager
import research_archive
import research_data
import research_extensions
import scan_opportunities

from . import deep_research
from .cache import CACHE
from .serializers import sanitize

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

Emit = Callable[[str, dict[str, Any]], None]
CommitResultFn = Callable[[Callable[[], Any]], Any]

TTL_COMPASS_MACRO_SECONDS = 600.0
TTL_COMPASS_POLYMARKET_DEGRADED_SECONDS = 60.0
TTL_COMPASS_CFTC_SECONDS = 3600.0
TTL_COMPASS_CFTC_DEGRADED_SECONDS = 120.0
TTL_COMPASS_MACRO_DEGRADED_SECONDS = 30.0
CORE_MACRO_SOURCES: tuple[str, ...] = (
    "live_macro",
    "us_fred",
    "cn_macro",
    "cross_asset_anchors",
    "cn_futures_anchors",
)
DEFAULT_MACRO_DEADLINE_SECONDS = 18.0
COMPASS_STAGE_TIMEOUT_SECONDS = 480

SEVEN_DIMENSIONS: tuple[str, ...] = (
    "宏观双核",
    "地缘与资源国政策",
    "技术与产业迭代",
    "商品供需平衡与库存周期",
    "产业政策与监管",
    "资金流与市场结构",
    "突发供给冲击",
)

VALID_COMPASS_DIRECTIONS = frozenset({"long", "short", "pair"})
VALID_CONVICTIONS = frozenset({"high", "medium"})
VALID_HORIZONS = frozenset({"1Q", "1Q~2Q", "2Q~1Y", "1Q~1Y"})
VALID_ASSET_SCOPES = frozenset({"commodities", "us_equities", "a_shares", "fx", "crypto", "cross_asset"})
EQUITY_ASSET_CLASSES = frozenset({"us_stock_or_etf", "a_share"})
# Option A policy: US equities/ETFs allow disciplined Davis double-kill shorting; A-shares remain strictly long-only.
LONG_ONLY_EQUITY_ASSET_CLASSES = frozenset({"a_share"})

CROSS_ASSET_ANCHORS: tuple[dict[str, str], ...] = (
    # US Equities & Sector Pricing Anchors
    {"symbol": "SPY", "name": "标普500", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "QQQ", "name": "纳指100", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "SMH", "name": "半导体", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "XLU", "name": "电力公用", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "XLE", "name": "能源", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "XLI", "name": "工业基建", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "XLB", "name": "基础材料", "group": "us_equities", "group_label": "美股行业锚"},
    {"symbol": "COPX", "name": "全球铜矿", "group": "us_equities", "group_label": "美股行业锚"},
    # Overseas Commodities
    {"symbol": "GC=F", "name": "黄金", "group": "commodities", "group_label": "海外大宗锚"},
    {"symbol": "HG=F", "name": "COMEX铜", "group": "commodities", "group_label": "海外大宗锚"},
    {"symbol": "CL=F", "name": "WTI原油", "group": "commodities", "group_label": "海外大宗锚"},
    {"symbol": "ALI=F", "name": "COMEX铝", "group": "commodities", "group_label": "海外大宗锚"},
    # A-shares, FX & Crypto
    {"symbol": "510300.SS", "name": "沪深300", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "510880.SS", "name": "A股红利", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "EURUSD=X", "name": "欧元/美元", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "USDJPY=X", "name": "美元/日元", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "AUDUSD=X", "name": "澳元/美元", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "USDCNY=X", "name": "美元/人民币", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "BTC-USD", "name": "比特币", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
)

CN_FUTURES_ANCHORS: tuple[dict[str, str], ...] = (
    {"symbol": "CU0", "name": "沪铜", "group": "cn_futures", "group_label": "国内核心期货锚"},
    {"symbol": "SN0", "name": "沪锡", "group": "cn_futures", "group_label": "国内核心期货锚"},
    {"symbol": "AL0", "name": "沪铝", "group": "cn_futures", "group_label": "国内核心期货锚"},
    {"symbol": "NI0", "name": "沪镍", "group": "cn_futures", "group_label": "国内核心期货锚"},
    {"symbol": "I0", "name": "铁矿石", "group": "cn_futures", "group_label": "国内核心期货锚"},
    {"symbol": "LC0", "name": "碳酸锂", "group": "cn_futures", "group_label": "国内核心期货锚"},
)

FOCUS_SCOPES_CATALOG: tuple[dict[str, str], ...] = (
    {
        "id": "commodities_energy_shipping",
        "label": "能源与航运",
        "group": "大宗商品（多空对称）",
        "prompt_hint": "全球原油、成品油裂解价差、天然气及集运/油轮/干散货航运的供需投产、地缘咽喉与运价周期（多空对称）",
    },
    {
        "id": "commodities_metals",
        "label": "有色与贵金属",
        "group": "大宗商品（多空对称）",
        "prompt_hint": "铜、铝、镍、锡、锌、黄金、白银、铂钯等金属的矿端投产/资源国政策、显隐性库存、强现实弱预期劈叉及跨金属配对（多空对称）",
    },
    {
        "id": "commodities_ferrous_chem",
        "label": "黑色与新能源化工",
        "group": "大宗商品（多空对称）",
        "prompt_hint": "铁矿石、钢铁双焦、碳酸锂/硅料及能化产业链的中国信用需求、新增产能爬坡过剩或产能出清逻辑（多空对称）",
    },
    {
        "id": "commodities_agri",
        "label": "农产油脂与畜牧",
        "group": "大宗商品（多空对称）",
        "prompt_hint": "大豆/棕榈油/谷物/糖/咖啡/可可/棉花及生猪养殖的天气气候、生物柴油政策与产能周期（多空对称）",
    },
    {
        "id": "fx_rates",
        "label": "外汇与利率周期",
        "group": "宏观汇率与加密（多空对称）",
        "prompt_hint": "美元指数及主要非美货币（日元、欧元、英镑、商品货币澳元/加元、人民币）的央行政策拐点、国债利差、贸易条件与套息拥挤度（多空对称）",
    },
    {
        "id": "crypto_liquidity",
        "label": "加密货币与流动性",
        "group": "宏观汇率与加密（多空对称）",
        "prompt_hint": "全球美元流动性、现货 ETF 资金流、稳定币扩容/收缩及监管法案驱动的加密多空周期（多空对称）",
    },
    {
        "id": "us_equities",
        "label": "美股板块与产业迭代",
        "group": "权益市场（美股支持双杀做空 · A股仅做多）",
        "prompt_hint": "美股具备 1~3 年远期预期差的产业迭代与瓶颈赛道（科技算力、电力电网、军工航天、医药、资源矿业股等；景气上行期做多，高利率紧缩+盈利/CapEx增速证伪戴维斯双杀期允许顺势做空板块/ETF）",
    },
    {
        "id": "a_shares",
        "label": "A 股主线与出海",
        "group": "权益市场（美股支持双杀做空 · A股仅做多）",
        "prompt_hint": "A 股高股息红利、制造业出海、资源周期与国内产业政策催化主线（严格仅做多，禁止做空）",
    },
)

_FOCUS_SCOPE_MAP: dict[str, dict[str, str]] = {item["id"]: item for item in FOCUS_SCOPES_CATALOG}


def normalize_focus_scopes(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        sid = str(item or "").strip().lower()
        if sid in _FOCUS_SCOPE_MAP and sid not in out:
            out.append(sid)
    return out


def format_focus_scopes_summary(scopes: list[str], focus_prompt: str = "") -> str:
    labels = [_FOCUS_SCOPE_MAP[s]["label"] for s in scopes if s in _FOCUS_SCOPE_MAP]
    parts: list[str] = []
    if labels:
        parts.append("专挖：" + " + ".join(labels))
    if focus_prompt.strip():
        parts.append(focus_prompt.strip())
    return " ｜ ".join(parts)

_POOL_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Bounded non-blocking executors & single-flight state for free macro overview fetches
_MACRO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="compass-macro")
_CN_ANCHOR_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="compass-cn-anchor")
_CN_ANCHOR_INFLIGHT_LOCK = threading.Lock()
_CN_ANCHOR_INFLIGHT: dict[str, concurrent.futures.Future[dict[str, Any]]] = {}
_MACRO_INFLIGHT_LOCK = threading.Lock()
_MACRO_INFLIGHT_FUTURE: concurrent.futures.Future[dict[str, Any]] | None = None
_PM_RETRY_INFLIGHT_LOCK = threading.Lock()
_PM_RETRY_INFLIGHT_FUTURE: concurrent.futures.Future[tuple[dict[str, Any], str, dict[str, Any] | None]] | None = None
_CFTC_RETRY_INFLIGHT_LOCK = threading.Lock()
_CFTC_RETRY_INFLIGHT_FUTURE: concurrent.futures.Future[tuple[dict[str, Any], str, dict[str, Any] | None]] | None = None
_LAST_GOOD_LOCK = threading.Lock()
_LAST_GOOD_MACRO_SOURCES: dict[str, dict[str, Any]] = {}
_LAST_GOOD_POLYMARKET_CATS: dict[str, dict[str, Any]] = {}

# Fallback memory ring buffer used when SQLite data_store is disabled (e.g., pytest)
_FALLBACK_LOCK = threading.RLock()
_FALLBACK_RUNS: list[dict[str, Any]] = []
_MAX_FALLBACK_RUNS = 20


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val) if val == val else None
    try:
        s = str(val).strip().replace(",", "").replace("%", "")
        if not s:
            return None
        f = float(s)
        return f if f == f else None
    except (ValueError, TypeError):
        return None


def _safe_pct_change(last: float | None, prev: float | None) -> float | None:
    if last is None or prev is None or prev == 0:
        return None
    return round((last / prev - 1.0) * 100.0, 2)


def _is_valid_iso_date(raw: Any) -> bool:
    s = str(raw or "").strip()[:10]
    if not _ISO_DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def clear_macro_source_cache() -> None:
    """Reset per-source last-known-good macro cache (primarily for isolated tests)."""
    with _LAST_GOOD_LOCK:
        _LAST_GOOD_MACRO_SOURCES.clear()
        _LAST_GOOD_POLYMARKET_CATS.clear()


# ---------------------------------------------------------------------------
# Free Hard-Data Foundation (US-China Macro Dual-Core + Cross-Asset Anchors)
# ---------------------------------------------------------------------------
def _is_bar_stale(as_of: str | None, *, max_age_days: int = 7) -> bool:
    if not as_of or not _is_valid_iso_date(as_of):
        return False
    try:
        bar_date = date.fromisoformat(as_of[:10])
        return (datetime.now(timezone.utc).date() - bar_date).days > max_age_days
    except ValueError:
        return False


def _anchor_item_from_close_series(
    meta: dict[str, str],
    close_series: Any,
    *,
    stale_fallback: bool = False,
) -> dict[str, Any]:
    if close_series is None or len(close_series) < 2:
        return {
            **meta,
            "status": "unavailable",
            "last": None,
            "ret_5d_pct": None,
            "ret_20d_pct": None,
            "ret_60d_pct": None,
            "range_pos_6m_pct": None,
            "as_of": None,
        }
    n_bars = len(close_series)
    last = _to_float(close_series.iloc[-1])
    # Strict lookback windows: never substitute shorter history into a longer return window
    p5 = _to_float(close_series.iloc[-6]) if n_bars >= 6 else None
    p20 = _to_float(close_series.iloc[-21]) if n_bars >= 21 else None
    p60 = _to_float(close_series.iloc[-61]) if n_bars >= 61 else None
    range_pos_6m: float | None = None
    if last is not None and n_bars >= 20:
        hi_6m = _to_float(close_series.max())
        lo_6m = _to_float(close_series.min())
        if hi_6m is not None and lo_6m is not None and hi_6m > lo_6m:
            range_pos_6m = round(max(0.0, min(100.0, (last - lo_6m) / (hi_6m - lo_6m) * 100.0)), 1)
    as_of_raw = close_series.index[-1]
    as_of = as_of_raw.strftime("%Y-%m-%d") if hasattr(as_of_raw, "strftime") else str(as_of_raw)[:10]
    is_stale = bool(stale_fallback) or _is_bar_stale(as_of)
    item: dict[str, Any] = {
        **meta,
        "status": "stale" if is_stale else "ok",
        "last": round(last, 4) if last is not None else None,
        "ret_5d_pct": _safe_pct_change(last, p5),
        "ret_20d_pct": _safe_pct_change(last, p20),
        "ret_60d_pct": _safe_pct_change(last, p60),
        "range_pos_6m_pct": range_pos_6m,
        "as_of": as_of,
    }
    if is_stale:
        item["stale_fallback"] = True
    return item


def fetch_cross_asset_anchors() -> dict[str, Any]:
    """Batch-fetch strict 5D / 20D / 60D relative returns for 19 global pricing anchors via yfinance."""
    if yf is None:
        return {"status": "error", "error": "yfinance is not installed", "items": []}

    tickers = [a["symbol"] for a in CROSS_ASSET_ANCHORS]
    try:
        with data_store.scoped_http_timeout((4.0, 12.0)):
            df = yf.download(
                tickers,
                period="6mo",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="ticker",
            )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc), "items": []}

    items: list[dict[str, Any]] = []
    ok_count = 0
    for meta in CROSS_ASSET_ANCHORS:
        sym = meta["symbol"]
        close_series = None
        try:
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels") and sym in df.columns.get_level_values(0):
                    sub = df[sym]
                    if "Close" in sub:
                        close_series = sub["Close"].dropna()
                elif "Close" in df.columns and len(tickers) == 1:
                    close_series = df["Close"].dropna()
        except Exception:
            close_series = None

        item = _anchor_item_from_close_series(meta, close_series)
        items.append(item)
        if item["status"] == "ok":
            ok_count += 1

    status = "ok" if ok_count == len(CROSS_ASSET_ANCHORS) else ("partial" if ok_count > 0 else "error")
    return {"status": status, "ok_count": ok_count, "total": len(CROSS_ASSET_ANCHORS), "items": items}


def _read_cached_cn_future_close(sym: str) -> Any:
    """Lock-free read-only SQLite fallback via load_cached_ohlcv regardless of TTL expiry."""
    if not data_store.is_store_enabled():
        return None
    try:
        store = data_store.get_store()
        key = data_store.SeriesKey("cn_future", sym, "1d", "akshare_sina_main", "raw_continuous")
        loaded = store.load_cached_ohlcv(
            key,
            period="6mo",
            timestamp_anchor="end",
            allowed_sources=("akshare_sina_main", "akshare_sina_daily_fb"),
        )
        if loaded is None:
            return None
        df_cached, _, _ = loaded
        if df_cached is not None and not df_cached.empty and "Close" in df_cached.columns:
            cs = df_cached["Close"].dropna()
            if len(cs) >= 2:
                return cs
        return None
    except Exception:
        return None


def _fetch_single_cn_future_anchor(
    meta: dict[str, str],
    *,
    refresh: bool = False,
    http_budget_seconds: float = 5.0,
) -> dict[str, Any]:
    sym = meta["symbol"]
    read_to = max(0.5, min(5.0, http_budget_seconds))
    conn_to = max(0.5, min(3.0, read_to))
    try:
        with data_store.scoped_http_timeout((conn_to, read_to)):
            snap = market_snapshot.get_snapshot(sym, period="6mo", refresh=refresh)
        if isinstance(snap, dict) and not snap.get("error"):
            tech = snap.get("technicals") or {}
            last = _to_float(tech.get("last_close"))
            as_of = str(tech.get("as_of_bar") or "")[:10] or None
            if last is not None:
                is_stale = _is_bar_stale(as_of)
                dq = snap.get("data_quality") if isinstance(snap.get("data_quality"), dict) else {}
                dq_degraded = bool(dq.get("quality_grade") == "degraded" or dq.get("actionable_allowed") is False)
                cl_raw = snap.get("chanlun") if isinstance(snap.get("chanlun"), dict) else {}
                chanlun_hint: dict[str, Any] | None = None
                if cl_raw and not cl_raw.get("error"):
                    sig_sum = cl_raw.get("signal_summary") if isinstance(cl_raw.get("signal_summary"), dict) else {}
                    st_meta = sig_sum.get("signal_meta") if isinstance(sig_sum.get("signal_meta"), dict) else {}
                    daily_cl = cl_raw.get("daily") if isinstance(cl_raw.get("daily"), dict) else {}
                    if dq_degraded or not daily_cl or daily_cl.get("error"):
                        chanlun_hint = {
                            "direction": "unknown",
                            "confirmed": False,
                            "zd": None,
                            "zg": None,
                            "shield_state": "unknown_structure",
                        }
                    else:
                        lz = daily_cl.get("latest_zhongshu") if isinstance(daily_cl.get("latest_zhongshu"), dict) else {}
                        zd_v = _to_float(lz.get("zd"))
                        zg_v = _to_float(lz.get("zg"))
                        cl_dir = str(st_meta.get("direction") or "neutral")
                        cl_conf = bool(st_meta.get("confirmed"))
                        if zd_v is None:
                            shield_state = "unknown_structure"
                        elif last < zd_v:
                            shield_state = "below_zd_breakdown"
                        elif cl_dir == "short" and cl_conf:
                            shield_state = "high_sell_above_zd_watch"
                        elif cl_dir == "long":
                            shield_state = "bullish_shield_intact"
                        else:
                            shield_state = "unknown_structure"
                        chanlun_hint = {
                            "direction": cl_dir,
                            "confirmed": cl_conf,
                            "zd": zd_v,
                            "zg": zg_v,
                            "shield_state": shield_state,
                        }
                item: dict[str, Any] = {
                    **meta,
                    "status": "stale" if is_stale else "ok",
                    "last": round(last, 4),
                    "ret_5d_pct": _to_float(tech.get("return_5d_pct")),
                    "ret_20d_pct": _to_float(tech.get("return_20d_pct")),
                    "ret_60d_pct": _to_float(tech.get("return_60d_pct")),
                    "range_pos_6m_pct": None,
                    "as_of": as_of,
                }
                if chanlun_hint is not None:
                    item["chanlun_hint"] = chanlun_hint
                if is_stale:
                    item["stale_fallback"] = True
                return item
    except Exception:
        pass

    cached_close = _read_cached_cn_future_close(sym)
    if cached_close is not None:
        return _anchor_item_from_close_series(meta, cached_close, stale_fallback=True)

    return _anchor_item_from_close_series(meta, None)


def _submit_cn_anchor_dedup(
    meta: dict[str, str],
    *,
    refresh: bool,
    http_budget_seconds: float,
) -> concurrent.futures.Future[dict[str, Any]]:
    """Reuse any still-running per-symbol future so overlapping refreshes never pile up on _CN_ANCHOR_EXECUTOR."""
    sym = meta["symbol"]
    with _CN_ANCHOR_INFLIGHT_LOCK:
        existing = _CN_ANCHOR_INFLIGHT.get(sym)
        if existing is not None and not existing.done():
            return existing
        fut = _CN_ANCHOR_EXECUTOR.submit(
            _fetch_single_cn_future_anchor,
            meta,
            refresh=refresh,
            http_budget_seconds=http_budget_seconds,
        )
        _CN_ANCHOR_INFLIGHT[sym] = fut

    # Register callback OUTSIDE _CN_ANCHOR_INFLIGHT_LOCK because add_done_callback runs
    # synchronously on the caller thread if fut has already finished!
    def _cleanup(_f: concurrent.futures.Future[dict[str, Any]], _s: str = sym) -> None:
        with _CN_ANCHOR_INFLIGHT_LOCK:
            if _CN_ANCHOR_INFLIGHT.get(_s) is _f:
                _CN_ANCHOR_INFLIGHT.pop(_s, None)

    fut.add_done_callback(_cleanup)
    return fut


def fetch_cn_futures_anchors(*, refresh: bool = False, timeout_seconds: float = 9.0) -> dict[str, Any]:
    """Fetch strict 5D / 20D / 60D returns for 6 China-priced futures anchors within a unified deadline."""
    budget = max(0.15, float(timeout_seconds))
    deadline = time.monotonic() + budget
    # Reserve a small slice at the end for fast local SQLite fallback reads on any timed-out symbols
    fallback_reserve = min(0.2, budget * 0.25)
    wait_budget = max(0.05, budget - fallback_reserve)
    http_budget = max(0.5, min(5.0, wait_budget))

    fut_map: dict[concurrent.futures.Future[dict[str, Any]], dict[str, str]] = {
        _submit_cn_anchor_dedup(meta, refresh=refresh, http_budget_seconds=http_budget): meta
        for meta in CN_FUTURES_ANCHORS
    }
    done, not_done = concurrent.futures.wait(list(fut_map.keys()), timeout=wait_budget)

    by_sym: dict[str, dict[str, Any]] = {}
    for fut in done:
        meta = fut_map[fut]
        try:
            by_sym[meta["symbol"]] = fut.result(timeout=0)
        except Exception:
            by_sym[meta["symbol"]] = _anchor_item_from_close_series(meta, None)

    for fut in not_done:
        meta = fut_map[fut]
        if time.monotonic() < deadline:
            cached_close = _read_cached_cn_future_close(meta["symbol"])
        else:
            cached_close = None
        by_sym[meta["symbol"]] = _anchor_item_from_close_series(
            meta,
            cached_close,
            stale_fallback=cached_close is not None,
        )

    items = [by_sym[m["symbol"]] for m in CN_FUTURES_ANCHORS]
    ok_count = sum(1 for it in items if it.get("status") == "ok")
    stale_count = sum(1 for it in items if it.get("status") == "stale")
    if ok_count == len(CN_FUTURES_ANCHORS):
        status = "ok"
    elif ok_count > 0:
        status = "partial"
    elif stale_count > 0:
        status = "stale"
    else:
        status = "error"
    return {
        "status": status,
        "ok_count": ok_count,
        "stale_count": stale_count,
        "total": len(CN_FUTURES_ANCHORS),
        "items": items,
    }


def compute_tape_movers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize extreme 20D/60D movers & market breadth strictly from FRESH ('ok') anchors.

    Each mover is assigned to at most one list (losers OR gainers) based on its dominant
    magnitude window (20D vs 60D) and carries explicit ``driver_window`` / ``driver_ret_pct``
    so UI badges always match the ranking sign.
    """
    fresh_items = [
        it
        for it in items
        if isinstance(it, dict)
        and it.get("status") == "ok"
        and not it.get("stale_fallback")
        and (it.get("ret_20d_pct") is not None or it.get("ret_60d_pct") is not None)
    ]

    def _dominant_move(it: dict[str, Any]) -> tuple[str, float] | None:
        r20 = _to_float(it.get("ret_20d_pct"))
        r60 = _to_float(it.get("ret_60d_pct"))
        if r20 is None and r60 is None:
            return None
        if r20 is not None and r60 is None:
            return ("20D", r20)
        if r60 is not None and r20 is None:
            return ("60D", r60)
        assert r20 is not None and r60 is not None
        # Choose the larger absolute move (20D wins ties for recency)
        return ("20D", r20) if abs(r20) >= abs(r60) else ("60D", r60)

    def _compact(it: dict[str, Any], window: str, driver_ret: float) -> dict[str, Any]:
        return {
            "symbol": it.get("symbol"),
            "name": it.get("name"),
            "group": it.get("group"),
            "last": it.get("last"),
            "ret_5d_pct": it.get("ret_5d_pct"),
            "ret_20d_pct": it.get("ret_20d_pct"),
            "ret_60d_pct": it.get("ret_60d_pct"),
            "driver_window": window,
            "driver_ret_pct": driver_ret,
            "as_of": it.get("as_of"),
        }

    losers_scored: list[tuple[float, dict[str, Any]]] = []
    gainers_scored: list[tuple[float, dict[str, Any]]] = []
    for it in fresh_items:
        dom = _dominant_move(it)
        if dom is None:
            continue
        win, val = dom
        if (win == "20D" and val <= -3.0) or (win == "60D" and val <= -6.0):
            losers_scored.append((val, _compact(it, win, val)))
        elif (win == "20D" and val >= 2.0) or (win == "60D" and val >= 5.0):
            gainers_scored.append((val, _compact(it, win, val)))

    losers_scored.sort(key=lambda pair: pair[0])
    gainers_scored.sort(key=lambda pair: pair[0], reverse=True)

    with_20d = [it for it in fresh_items if it.get("ret_20d_pct") is not None]
    total_20d = len(with_20d)
    declining_20d = sum(1 for it in with_20d if float(it["ret_20d_pct"]) < 0)
    advancing_20d = sum(1 for it in with_20d if float(it["ret_20d_pct"]) > 0)
    severe_drawdown = sum(
        1
        for it in fresh_items
        if (it.get("ret_20d_pct") is not None and float(it["ret_20d_pct"]) <= -10.0)
        or (it.get("ret_60d_pct") is not None and float(it["ret_60d_pct"]) <= -15.0)
    )
    strong_rally = sum(
        1
        for it in fresh_items
        if (it.get("ret_20d_pct") is not None and float(it["ret_20d_pct"]) >= 10.0)
        or (it.get("ret_60d_pct") is not None and float(it["ret_60d_pct"]) >= 15.0)
    )

    if total_20d >= 6 and (declining_20d / total_20d) >= 0.70 and severe_drawdown >= 5:
        breadth_label = "全面紧缩/普跌（空头主战场）"
    elif total_20d >= 6 and (advancing_20d / total_20d) >= 0.70 and strong_rally >= 5:
        breadth_label = "全面扩张/普涨（多头主战场）"
    else:
        breadth_label = "结构分化（多空双向择优）"

    return {
        "top_losers": [item for _, item in losers_scored[:8]],
        "top_gainers": [item for _, item in gainers_scored[:5]],
        "regime_breadth": {
            "total_available": total_20d,
            "declining_20d_count": declining_20d,
            "advancing_20d_count": advancing_20d,
            "severe_drawdown_count": severe_drawdown,
            "strong_rally_count": strong_rally,
            "breadth_label": breadth_label,
        },
    }


def _extract_china_pmi_headline(china_macro: dict[str, Any]) -> dict[str, Any] | None:
    pmi = china_macro.get("pmi")
    if not isinstance(pmi, dict) or pmi.get("status") != "ok":
        return None
    recs = pmi.get("records")
    if not isinstance(recs, list) or not recs:
        return None
    first = recs[0] if isinstance(recs[0], dict) else {}
    month = None
    mfg_val = None
    non_mfg_val = None
    for k, v in first.items():
        ks = str(k)
        if "月份" in ks or "日期" in ks or "时间" in ks:
            month = str(v)
        elif "制造业" in ks and "非制造业" not in ks and ("指数" in ks or "PMI" in ks.upper() or mfg_val is None):
            if "同比" not in ks and "环比" not in ks:
                cand = _to_float(v)
                if cand is not None and 20.0 <= cand <= 80.0:
                    mfg_val = cand
        elif "非制造业" in ks and ("指数" in ks or "PMI" in ks.upper() or non_mfg_val is None):
            if "同比" not in ks and "环比" not in ks:
                cand = _to_float(v)
                if cand is not None and 20.0 <= cand <= 80.0:
                    non_mfg_val = cand
    if mfg_val is None:
        return None
    return {
        "value": round(mfg_val, 1),
        "non_mfg": round(non_mfg_val, 1) if non_mfg_val is not None else None,
        "period": month,
    }


def _build_headline_metrics_and_tags(
    live_ctx: dict[str, Any],
    fred_macro: dict[str, Any],
    china_macro: dict[str, Any],
    cftc_crowding: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Assemble compact headline KPI tiles with single-source observation provenance."""
    series = fred_macro.get("series") if isinstance(fred_macro.get("series"), dict) else {}

    def _fred_val(sid: str) -> tuple[float | None, float | None, str | None]:
        row = series.get(sid)
        if not isinstance(row, dict) or row.get("status") != "ok":
            return None, None, None
        raw_v = row.get("latest_value")
        if raw_v is None:
            raw_v = row.get("latest")
        return _to_float(raw_v), _to_float(row.get("change")), row.get("latest_date")

    dfii10, dfii10_chg, dfii10_dt = _fred_val("DFII10")
    t10yie, t10yie_chg, t10yie_dt = _fred_val("T10YIE")
    dgs10, dgs10_chg, dgs10_dt = _fred_val("DGS10")
    hy_oas, hy_oas_chg, hy_oas_dt = _fred_val("BAMLH0A0HYM2")
    fedfunds, _, fed_dt = _fred_val("FEDFUNDS")
    copper_m, copper_chg, copper_dt = _fred_val("PCOPPUSDM")

    live_us10y = live_ctx.get("US10Y_Yield") if isinstance(live_ctx.get("US10Y_Yield"), dict) else {}
    live_dxy = live_ctx.get("DXY_USD_Index") if isinstance(live_ctx.get("DXY_USD_Index"), dict) else {}
    live_vix = live_ctx.get("VIX") if isinstance(live_ctx.get("VIX"), dict) else {}

    live_us10y_val = _to_float(live_us10y.get("last"))
    if live_us10y_val is not None:
        us10y_metric = {
            "key": "US10Y",
            "label": "美债10Y名义",
            "value": round(live_us10y_val, 2),
            "unit": "%",
            "delta": _to_float(live_us10y.get("chg_1d_pct")),
            "delta_unit": "%",
            "as_of": live_us10y.get("as_of"),
            "source": "yfinance",
        }
    else:
        us10y_metric = {
            "key": "US10Y",
            "label": "美债10Y名义",
            "value": round(dgs10, 2) if dgs10 is not None else None,
            "unit": "%",
            "delta": round(dgs10_chg, 2) if dgs10_chg is not None else None,
            "delta_unit": "pp",
            "as_of": dgs10_dt,
            "source": "FRED",
        }

    dxy_val = _to_float(live_dxy.get("last"))
    vix_val = _to_float(live_vix.get("last"))
    cn_pmi = _extract_china_pmi_headline(china_macro)

    metrics: list[dict[str, Any]] = [
        {
            "key": "DFII10",
            "label": "美债10Y实际利率",
            "value": round(dfii10, 2) if dfii10 is not None else None,
            "unit": "%",
            "delta": round(dfii10_chg, 2) if dfii10_chg is not None else None,
            "delta_unit": "pp",
            "as_of": dfii10_dt,
            "source": "FRED",
        },
        {
            "key": "T10YIE",
            "label": "10Y通胀预期",
            "value": round(t10yie, 2) if t10yie is not None else None,
            "unit": "%",
            "delta": round(t10yie_chg, 2) if t10yie_chg is not None else None,
            "delta_unit": "pp",
            "as_of": t10yie_dt,
            "source": "FRED",
        },
        us10y_metric,
        {
            "key": "DXY",
            "label": "美元指数 DXY",
            "value": round(dxy_val, 2) if dxy_val is not None else None,
            "unit": "",
            "delta": _to_float(live_dxy.get("chg_1d_pct")),
            "delta_unit": "%",
            "as_of": live_dxy.get("as_of"),
            "source": "yfinance",
        },
        {
            "key": "VIX",
            "label": "波动率 VIX",
            "value": round(vix_val, 2) if vix_val is not None else None,
            "unit": "",
            "delta": _to_float(live_vix.get("chg_1d_pct")),
            "delta_unit": "%",
            "as_of": live_vix.get("as_of"),
            "source": "yfinance",
        },
        {
            "key": "CN_PMI",
            "label": "中国制造业PMI",
            "value": cn_pmi["value"] if cn_pmi else None,
            "unit": "",
            "delta": None,
            "delta_unit": "",
            "as_of": cn_pmi["period"] if cn_pmi else None,
            "source": "国家统计局",
        },
        {
            "key": "FEDFUNDS",
            "label": "联邦基金利率",
            "value": round(fedfunds, 2) if fedfunds is not None else None,
            "unit": "%",
            "delta": None,
            "delta_unit": "",
            "as_of": fed_dt,
            "source": "FRED",
        },
        {
            "key": "HY_OAS",
            "label": "高收益信用利差",
            "value": round(hy_oas, 2) if hy_oas is not None else None,
            "unit": "%",
            "delta": round(hy_oas_chg, 2) if hy_oas_chg is not None else None,
            "delta_unit": "pp",
            "as_of": hy_oas_dt,
            "source": "FRED",
        },
        {
            "key": "PCOPPUSDM",
            "label": "全球铜价锚",
            "value": round(copper_m, 0) if copper_m is not None else None,
            "unit": "$/t",
            "delta": round(copper_chg, 0) if copper_chg is not None else None,
            "delta_unit": "",
            "as_of": copper_dt,
            "source": "FRED",
        },
    ]

    tags: list[dict[str, str]] = []
    if dfii10 is not None:
        if dfii10 >= 2.0:
            tags.append({"label": f"实际利率偏紧 ({dfii10:.2f}%)", "tone": "warning"})
        elif dfii10 <= 1.0:
            tags.append({"label": f"实际利率宽松 ({dfii10:.2f}%)", "tone": "good"})
        else:
            tags.append({"label": f"实际利率中性 ({dfii10:.2f}%)", "tone": "neutral"})
    if hy_oas is not None:
        if hy_oas >= 5.0:
            tags.append({"label": f"信用利差高压 ({hy_oas:.2f}%)", "tone": "critical"})
        elif hy_oas >= 4.0:
            tags.append({"label": f"信用利差走阔 ({hy_oas:.2f}%)", "tone": "warning"})
        elif hy_oas <= 3.2:
            tags.append({"label": f"信用利差紧致 ({hy_oas:.2f}%)", "tone": "good"})
    if vix_val is not None:
        if vix_val >= 25.0:
            tags.append({"label": f"VIX高波避险 ({vix_val:.1f})", "tone": "critical"})
        elif vix_val >= 18.0:
            tags.append({"label": f"VIX温和波动 ({vix_val:.1f})", "tone": "warning"})
        else:
            tags.append({"label": f"VIX低波风险偏好 ({vix_val:.1f})", "tone": "good"})
    if dxy_val is not None:
        if dxy_val >= 104.0:
            tags.append({"label": f"强美元压制 ({dxy_val:.1f})", "tone": "warning"})
        elif dxy_val <= 99.0:
            tags.append({"label": f"弱美元顺风 ({dxy_val:.1f})", "tone": "good"})
        else:
            tags.append({"label": f"美元中性区间 ({dxy_val:.1f})", "tone": "neutral"})
    if cn_pmi is not None:
        v = cn_pmi["value"]
        if v >= 50.0:
            tags.append({"label": f"中国PMI扩张 ({v:.1f})", "tone": "good"})
        else:
            tags.append({"label": f"中国PMI收缩 ({v:.1f})", "tone": "warning"})
    if isinstance(cftc_crowding, dict) and cftc_crowding.get("status") in ("ok", "partial") and not cftc_crowding.get("stale_fallback"):
        c_long = [str(it.get("label") or it.get("symbol") or "") for it in (cftc_crowding.get("crowded_long") or []) if isinstance(it, dict)]
        c_short = [str(it.get("label") or it.get("symbol") or "") for it in (cftc_crowding.get("crowded_short") or []) if isinstance(it, dict)]
        if c_long:
            tags.append({"label": f"CFTC多头拥挤 ({'/'.join(c_long[:2])})", "tone": "warning"})
        if c_short:
            tags.append({"label": f"CFTC空头拥挤 ({'/'.join(c_short[:2])})", "tone": "warning"})

    return metrics, tags


def _merge_anchor_items_preserving_fresher(
    new_items: list[dict[str, Any]],
    prior_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge per-symbol anchor rows so a stale/unavailable item never clobbers a fresher last-known-good row."""
    prior_by_sym = {
        str(it.get("symbol") or ""): it
        for it in prior_items
        if isinstance(it, dict) and it.get("symbol")
    }
    rank_status = {"ok": 2, "stale": 1, "unavailable": 0}
    merged: list[dict[str, Any]] = []
    for it in new_items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol") or "")
        prev = prior_by_sym.get(sym)
        if prev is None:
            merged.append(it)
            continue
        new_st = str(it.get("status") or "unavailable")
        prev_st = str(prev.get("status") or "unavailable")
        new_asof = str(it.get("as_of") or "")
        prev_asof = str(prev.get("as_of") or "")
        # Prefer higher freshness status when new is degraded, or strictly newer as_of
        if rank_status.get(new_st, 0) < rank_status.get(prev_st, 0) and prev_asof >= new_asof:
            # Mark carried-over item as stale so live breadth never treats prior cache as newly fetched
            merged.append({**prev, "status": "stale", "stale_fallback": True})
        else:
            merged.append(it)
    return merged


def _resolve_polymarket_source(
    raw_val: Any,
    err_str: str | None,
    now_iso: str,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Resolve Polymarket odds per-category so partial failures never clobber complete snapshots.

    Guarantees outer ``sources_status["polymarket_odds"]`` and inner ``polymarket_odds["status"]``
    stay strictly consistent across ok / partial / stale / error states.
    """
    spec_keys = [str(s["key"]) for s in research_extensions.POLYMARKET_CATEGORY_SPECS]
    total_cats = len(spec_keys)
    val = raw_val if isinstance(raw_val, dict) else {}
    raw_status = str(val.get("status") or "")
    cat_errors: dict[str, str] = {}
    if isinstance(val.get("category_errors"), dict):
        cat_errors.update({str(k): str(v) for k, v in val["category_errors"].items()})
    if err_str:
        cat_errors.setdefault("_all", err_str)

    # Extract freshly fetched categories from raw_val (either categories_by_key or grouped from events)
    fresh_by_cat: dict[str, list[dict[str, Any]]] = {}
    if not err_str and raw_status in ("ok", "partial"):
        raw_by_key = val.get("categories_by_key")
        if isinstance(raw_by_key, dict):
            for ck in spec_keys:
                if ck in raw_by_key and isinstance(raw_by_key[ck], list) and ck not in cat_errors:
                    fresh_by_cat[ck] = [ev for ev in raw_by_key[ck] if isinstance(ev, dict)]
        else:
            # Backward compatibility for simple mocked payloads without categories_by_key
            raw_events = val.get("events") if isinstance(val.get("events"), list) else []
            grouped: dict[str, list[dict[str, Any]]] = {}
            for ev in raw_events:
                if isinstance(ev, dict):
                    ck = str(ev.get("category") or "")
                    if ck in spec_keys:
                        grouped.setdefault(ck, []).append(ev)
            if raw_status == "ok":
                for ck in spec_keys:
                    if ck not in cat_errors:
                        fresh_by_cat[ck] = grouped.get(ck, [])
            else:
                for ck, evs in grouped.items():
                    if ck not in cat_errors:
                        fresh_by_cat[ck] = evs

    merged_by_cat: dict[str, list[dict[str, Any]]] = {}
    stale_categories: list[str] = []
    oldest_good_at: str | None = None
    newest_good_at: str | None = None

    with _LAST_GOOD_LOCK:
        for ck, evs in fresh_by_cat.items():
            _LAST_GOOD_POLYMARKET_CATS[ck] = {
                "events": [dict(ev) for ev in evs],
                "fetched_at_utc": str(val.get("fetched_at_utc") or now_iso),
            }

        for ck in spec_keys:
            if ck in fresh_by_cat:
                merged_by_cat[ck] = [{**ev, "stale_fallback": False} for ev in fresh_by_cat[ck]]
            else:
                prior_cat = _LAST_GOOD_POLYMARKET_CATS.get(ck)
                if prior_cat is not None:
                    prior_ts = str(prior_cat.get("fetched_at_utc") or "")
                    if prior_ts:
                        oldest_good_at = prior_ts if oldest_good_at is None else min(oldest_good_at, prior_ts)
                        newest_good_at = prior_ts if newest_good_at is None else max(newest_good_at, prior_ts)
                    merged_by_cat[ck] = [
                        {**ev, "stale_fallback": True}
                        for ev in (prior_cat.get("events") or [])
                        if isinstance(ev, dict)
                    ]
                    stale_categories.append(ck)

    # Re-apply cross-category deduplication by specificity (trade_tariffs > geopolitics > macro_rates)
    # so merging fresh lower-specificity categories with cached higher-specificity categories can NEVER emit duplicate IDs.
    fresh_event_by_id: dict[str, dict[str, Any]] = {}
    for ck in fresh_by_cat:
        for ev in merged_by_cat.get(ck) or []:
            ev_id = str(ev.get("id") or "")
            if ev_id and ev_id not in fresh_event_by_id:
                fresh_event_by_id[ev_id] = ev

    specs_by_specificity = sorted(
        research_extensions.POLYMARKET_CATEGORY_SPECS,
        key=lambda s: -int(s.get("specificity") or 0),
    )
    dedup_seen_ids: set[str] = set()
    deduped_by_cat: dict[str, list[dict[str, Any]]] = {}
    for spec in specs_by_specificity:
        ck = str(spec["key"])
        cat_label = str(spec["label"])
        cat_list: list[dict[str, Any]] = []
        for ev in merged_by_cat.get(ck) or []:
            ev_id = str(ev.get("id") or "")
            if not ev_id or ev_id in dedup_seen_ids:
                continue
            dedup_seen_ids.add(ev_id)
            # If a cached higher-specificity entry also arrived freshly in a broader tag, refresh its live odds while preserving category ownership
            if ev.get("stale_fallback") and ev_id in fresh_event_by_id:
                fresher = fresh_event_by_id[ev_id]
                cat_list.append(
                    {
                        **fresher,
                        "category": ck,
                        "category_label": cat_label,
                        "stale_fallback": False,
                    }
                )
            else:
                cat_list.append(ev)
        deduped_by_cat[ck] = cat_list

    ordered_events: list[dict[str, Any]] = []
    for ck in spec_keys:
        ordered_events.extend(deduped_by_cat.get(ck) or [])
    headlines = research_extensions._build_polymarket_headlines(deduped_by_cat, ordered_events)

    fresh_count = len(fresh_by_cat)
    err_summary = err_str or (
        "; ".join(f"{k}: {v}" for k, v in cat_errors.items())
        if cat_errors
        else (str(val.get("error")) if val.get("error") else "fetch failed")
    )

    if fresh_count == total_cats and not cat_errors:
        resolved_status = "ok"
        stale_info: dict[str, Any] | None = None
    elif fresh_count > 0:
        resolved_status = "partial"
        stale_info = {
            "last_good_at_utc": newest_good_at,
            "error": err_summary,
            "stale_categories": stale_categories,
        }
    elif stale_categories:
        resolved_status = "stale"
        stale_info = {
            "last_good_at_utc": newest_good_at,
            "error": err_summary,
            "stale_categories": stale_categories,
        }
    else:
        resolved_status = "error"
        stale_info = {
            "last_good_at_utc": None,
            "error": err_summary,
        }

    out: dict[str, Any] = {
        "status": resolved_status,
        "source": "Polymarket Gamma API",
        "fetched_at_utc": str(val.get("fetched_at_utc") or now_iso) if fresh_count > 0 else (newest_good_at or now_iso),
        "total_events": len(ordered_events),
        "categories_ok": fresh_count,
        "categories_total": total_cats,
        "stale_categories": stale_categories,
        "stale_fallback": resolved_status == "stale",
        "headline_events": headlines,
        "events": ordered_events,
    }
    if resolved_status in ("partial", "stale") and oldest_good_at:
        out["last_good_at_utc"] = oldest_good_at
    if cat_errors:
        out["category_errors"] = cat_errors
    if resolved_status == "error":
        out["error"] = err_summary
    return out, resolved_status, stale_info


def fetch_polymarket_macro_odds(*, timeout_seconds: float = 6.0) -> dict[str, Any]:
    """Fetch keyless Polymarket Gamma API odds for macro rates, tariffs and geopolitics."""
    return research_extensions.fetch_polymarket_macro_odds(timeout_seconds=timeout_seconds)


def fetch_cftc_crowding_overview(*, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Fetch CFTC 52-week weekly COT archives (Disagg + TFF) for derivatives crowding telemetry."""
    return research_extensions.fetch_cftc_crowding_overview(timeout_seconds=timeout_seconds)


def _resolve_cftc_crowding_source(
    raw_val: Any,
    err_str: str | None,
    now_iso: str,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Resolve optional CFTC crowding telemetry with last-known-good stale fallback."""
    val = raw_val if isinstance(raw_val, dict) else {}
    raw_status = str(val.get("status") or "")
    if err_str is None and raw_status in ("ok", "partial") and isinstance(val.get("items"), list) and len(val["items"]) > 0:
        clean_val = {**val, "stale_fallback": False}
        with _LAST_GOOD_LOCK:
            _LAST_GOOD_MACRO_SOURCES["cftc_crowding"] = {"data": clean_val, "fetched_at_utc": now_iso}
        stale_info = (
            {"last_good_at_utc": now_iso, "error": "; ".join(f"{k}: {v}" for k, v in (val.get("source_errors") or {}).items())}
            if raw_status == "partial" and val.get("source_errors")
            else None
        )
        return clean_val, raw_status, stale_info

    err_msg = err_str or (str(val.get("error")) if val.get("error") else "fetch failed")
    with _LAST_GOOD_LOCK:
        prior = _LAST_GOOD_MACRO_SOURCES.get("cftc_crowding")
    if prior is not None and isinstance(prior.get("data"), dict):
        stale_data = {
            **prior["data"],
            "status": "stale",
            "stale_fallback": True,
            "last_good_at_utc": prior["fetched_at_utc"],
        }
        return stale_data, "stale", {"last_good_at_utc": prior["fetched_at_utc"], "error": err_msg}

    empty_err = {
        "status": "error",
        "source": "CFTC Weekly COT Annual Archives (Disagg + TFF)",
        "fetched_at_utc": now_iso,
        "latest_report_date": None,
        "window_weeks": 52,
        "benchmarks_ok": 0,
        "benchmarks_total": len(research_extensions.CFTC_CROWDING_BENCHMARKS),
        "items": [],
        "crowded_long": [],
        "crowded_short": [],
        "stale_symbols": [],
        "stale_fallback": False,
        "error": err_msg,
    }
    return empty_err, "error", {"last_good_at_utc": None, "error": err_msg}


def _fetch_macro_overview_uncached(deadline_seconds: float, *, refresh: bool = False) -> dict[str, Any]:
    """Execute one bounded parallel macro overview fetch without blocking on timed-out threads."""
    now_iso = _utc_iso()
    http_read_timeout = max(0.5, min(12.0, deadline_seconds))

    def _wrapped(fn: Callable[[], Any]) -> Any:
        with data_store.scoped_http_timeout((min(4.0, http_read_timeout), http_read_timeout)):
            return fn()

    cn_timeout = max(0.2, min(9.0, deadline_seconds * 0.9))
    pm_timeout = max(0.2, min(6.0, deadline_seconds * 0.85))
    cftc_timeout = max(0.5, min(8.0, deadline_seconds * 0.85))
    reused_cftc_tuple: tuple[dict[str, Any], str, dict[str, Any] | None] | None = None
    if not refresh:
        hit_cftc_cached, cached_cftc_val = CACHE.get(("compass_cftc_crowding",))
        if (
            hit_cftc_cached
            and isinstance(cached_cftc_val, tuple)
            and len(cached_cftc_val) == 3
            and cached_cftc_val[1] == "ok"
        ):
            reused_cftc_tuple = cached_cftc_val

    fut_map: dict[concurrent.futures.Future[Any], str] = {
        _MACRO_EXECUTOR.submit(_wrapped, market_snapshot.fetch_macro_context): "live_macro",
        _MACRO_EXECUTOR.submit(_wrapped, research_data.fetch_fred_macro): "us_fred",
        _MACRO_EXECUTOR.submit(_wrapped, research_data.fetch_china_macro_core): "cn_macro",
        _MACRO_EXECUTOR.submit(_wrapped, fetch_cross_asset_anchors): "cross_asset_anchors",
        _MACRO_EXECUTOR.submit(
            lambda: fetch_cn_futures_anchors(refresh=refresh, timeout_seconds=cn_timeout)
        ): "cn_futures_anchors",
        _MACRO_EXECUTOR.submit(
            lambda: fetch_polymarket_macro_odds(timeout_seconds=pm_timeout)
        ): "polymarket_odds",
    }
    if reused_cftc_tuple is None:
        fut_map[
            _MACRO_EXECUTOR.submit(
                lambda: fetch_cftc_crowding_overview(timeout_seconds=cftc_timeout)
            )
        ] = "cftc_crowding"

    done, not_done = concurrent.futures.wait(
        list(fut_map.keys()),
        timeout=max(0.01, deadline_seconds),
    )

    raw_results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for fut in done:
        name = fut_map[fut]
        try:
            raw_results[name] = fut.result(timeout=0)
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)

    for fut in not_done:
        name = fut_map[fut]
        fut.cancel()
        errors[name] = f"timeout after {deadline_seconds:.2f}s"

    def _resolve_source(name: str, default_empty: Any, ok_check: Callable[[Any], bool]) -> tuple[Any, str, dict[str, Any] | None]:
        val = raw_results.get(name)
        if name not in errors and ok_check(val):
            st = str(val.get("status") or "ok") if isinstance(val, dict) and "status" in val else "ok"
            with _LAST_GOOD_LOCK:
                prior = _LAST_GOOD_MACRO_SOURCES.get(name)
                if st == "partial" and prior is not None and isinstance(val, dict) and isinstance(val.get("items"), list):
                    prior_items = (prior.get("data") or {}).get("items") or []
                    merged_sub = _merge_anchor_items_preserving_fresher(val["items"], prior_items)
                    val = {
                        **val,
                        "items": merged_sub,
                        "ok_count": sum(1 for it in merged_sub if it.get("status") == "ok"),
                    }
                    # Only promote to last-known-good if it doesn't shrink fresh ok_count
                    prev_ok = int((prior.get("data") or {}).get("ok_count") or 0)
                    if int(val.get("ok_count") or 0) >= prev_ok:
                        _LAST_GOOD_MACRO_SOURCES[name] = {"data": val, "fetched_at_utc": now_iso}
                else:
                    _LAST_GOOD_MACRO_SOURCES[name] = {"data": val, "fetched_at_utc": now_iso}
            return val, st, None

        # If source returned stale fallback items (e.g. SQLite cached bars on Sina timeout),
        # never clobber _LAST_GOOD_MACRO_SOURCES and mark degraded stale!
        err_msg = errors.get(name) or (str(val.get("error")) if isinstance(val, dict) and val.get("error") else "fetch failed")
        with _LAST_GOOD_LOCK:
            prior = _LAST_GOOD_MACRO_SOURCES.get(name)
        if isinstance(val, dict) and str(val.get("status") or "") == "stale":
            if prior is not None and isinstance(val.get("items"), list):
                prior_items = (prior.get("data") or {}).get("items") or []
                merged_sub = _merge_anchor_items_preserving_fresher(val["items"], prior_items)
                val = {**val, "items": merged_sub, "ok_count": 0}
                return val, "stale", {"last_good_at_utc": prior["fetched_at_utc"], "error": err_msg or "stale sqlite fallback"}
            return val, "stale", {"last_good_at_utc": None, "error": err_msg or "stale sqlite fallback"}
        if prior is not None:
            prior_data = prior["data"]
            if isinstance(prior_data, dict) and isinstance(prior_data.get("items"), list):
                stale_items = [
                    ({**it, "status": "stale", "stale_fallback": True} if it.get("status") == "ok" else it)
                    for it in prior_data["items"]
                    if isinstance(it, dict)
                ]
                prior_data = {**prior_data, "status": "stale", "ok_count": 0, "items": stale_items}
            return prior_data, "stale", {"last_good_at_utc": prior["fetched_at_utc"], "error": err_msg}
        return default_empty, "error", {"last_good_at_utc": None, "error": err_msg}

    live_ctx, st_live, stale_live = _resolve_source(
        "live_macro",
        {},
        lambda v: isinstance(v, dict) and len(v) > 0,
    )
    fred_macro, st_fred, stale_fred = _resolve_source(
        "us_fred",
        {"status": "error", "error": errors.get("us_fred", "unavailable")},
        lambda v: isinstance(v, dict) and str(v.get("status") or "") in ("ok", "partial"),
    )
    china_macro, st_cn, stale_cn = _resolve_source(
        "cn_macro",
        {"status": "error", "error": errors.get("cn_macro", "unavailable")},
        lambda v: isinstance(v, dict) and str(v.get("status") or "") in ("ok", "partial"),
    )
    yf_anchors, st_anc, stale_anc = _resolve_source(
        "cross_asset_anchors",
        {"status": "error", "items": [], "ok_count": 0, "total": len(CROSS_ASSET_ANCHORS), "error": errors.get("cross_asset_anchors", "unavailable")},
        lambda v: isinstance(v, dict) and str(v.get("status") or "") in ("ok", "partial"),
    )
    cn_anchors, st_cn_anc, stale_cn_anc = _resolve_source(
        "cn_futures_anchors",
        {"status": "error", "items": [], "ok_count": 0, "total": len(CN_FUTURES_ANCHORS), "error": errors.get("cn_futures_anchors", "unavailable")},
        lambda v: isinstance(v, dict) and str(v.get("status") or "") in ("ok", "partial"),
    )
    polymarket_odds, st_pm, stale_pm = _resolve_polymarket_source(
        raw_results.get("polymarket_odds"),
        errors.get("polymarket_odds"),
        now_iso,
    )
    if reused_cftc_tuple is not None:
        cftc_crowding, st_cftc, stale_cftc = reused_cftc_tuple
    else:
        cftc_crowding, st_cftc, stale_cftc = _resolve_cftc_crowding_source(
            raw_results.get("cftc_crowding"),
            errors.get("cftc_crowding"),
            now_iso,
        )

    merged_items: list[dict[str, Any]] = [
        *(yf_anchors.get("items") or []),
        *(cn_anchors.get("items") or []),
    ]
    merged_ok = sum(1 for it in merged_items if isinstance(it, dict) and it.get("status") == "ok")
    declared_total = int(yf_anchors.get("total") or len(yf_anchors.get("items") or [])) + int(
        cn_anchors.get("total") or len(cn_anchors.get("items") or [])
    )
    merged_total = max(len(merged_items), declared_total)
    merged_status = "ok" if (merged_total > 0 and merged_ok == merged_total) else ("partial" if merged_ok > 0 else "error")
    anchors = {
        "status": merged_status,
        "ok_count": merged_ok,
        "total": merged_total,
        "items": merged_items,
    }
    tape_movers = compute_tape_movers(merged_items)

    sources_status = {
        "live_macro": st_live,
        "us_fred": st_fred,
        "cn_macro": st_cn,
        "cross_asset_anchors": st_anc,
        "cn_futures_anchors": st_cn_anc,
        "polymarket_odds": st_pm,
        "cftc_crowding": st_cftc,
    }
    stale_sources = {
        k: info
        for k, info in (
            ("live_macro", stale_live),
            ("us_fred", stale_fred),
            ("cn_macro", stale_cn),
            ("cross_asset_anchors", stale_anc),
            ("cn_futures_anchors", stale_cn_anc),
            ("polymarket_odds", stale_pm),
            ("cftc_crowding", stale_cftc),
        )
        if info is not None
    }

    headline_metrics, regime_tags = _build_headline_metrics_and_tags(
        live_ctx,
        fred_macro,
        china_macro,
        cftc_crowding=cftc_crowding,
    )
    core_fresh = all(sources_status.get(k) in ("ok", "partial") for k in CORE_MACRO_SOURCES)
    pm_fresh = sources_status.get("polymarket_odds") == "ok"
    cftc_fresh = sources_status.get("cftc_crowding") == "ok"
    # Core 5 sources keep full 600s TTL even if optional Polymarket/CFTC sources are degraded;
    # optional sources maintain their own independent retry cache slots below.
    ttl = TTL_COMPASS_MACRO_SECONDS if core_fresh else TTL_COMPASS_MACRO_DEGRADED_SECONDS
    pm_ttl = TTL_COMPASS_MACRO_SECONDS if pm_fresh else TTL_COMPASS_POLYMARKET_DEGRADED_SECONDS
    cftc_ttl = TTL_COMPASS_CFTC_SECONDS if cftc_fresh else TTL_COMPASS_CFTC_DEGRADED_SECONDS

    bundle = sanitize(
        {
            "fetched_at_utc": now_iso,
            "cache_hit": False,
            "sources_status": sources_status,
            "stale_sources": stale_sources,
            "regime_tags": regime_tags,
            "headline_metrics": headline_metrics,
            "live_macro": live_ctx,
            "us_fred": fred_macro,
            "china_macro": china_macro,
            "anchors": anchors,
            "tape_movers": tape_movers,
            "polymarket_odds": polymarket_odds,
            "cftc_crowding": cftc_crowding,
        }
    )
    bundle["regime_signal_lights"] = compute_regime_signal_lights(bundle)
    CACHE.set(("compass_macro_overview",), bundle, ttl)
    CACHE.set(("compass_polymarket_odds",), (polymarket_odds, st_pm, stale_pm), pm_ttl)
    if reused_cftc_tuple is None:
        CACHE.set(("compass_cftc_crowding",), (cftc_crowding, st_cftc, stale_cftc), cftc_ttl)
    return bundle


def _overlay_optional_sources_on_overview(
    base: dict[str, Any],
    pm_tuple: tuple[dict[str, Any], str, dict[str, Any] | None] | None = None,
    cftc_tuple: tuple[dict[str, Any], str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Return a fresh, atomically consistent overview dict combining immutable core snapshot + optional sources."""
    out = dict(base)
    new_sources_status = dict(base.get("sources_status") or {})
    new_stale = dict(base.get("stale_sources") or {})
    if pm_tuple is not None:
        pm_odds, st_pm, stale_pm = pm_tuple
        new_sources_status["polymarket_odds"] = st_pm
        if stale_pm is not None:
            new_stale["polymarket_odds"] = stale_pm
        else:
            new_stale.pop("polymarket_odds", None)
        out["polymarket_odds"] = pm_odds
    if cftc_tuple is not None:
        cftc_data, st_cftc, stale_cftc = cftc_tuple
        new_sources_status["cftc_crowding"] = st_cftc
        if stale_cftc is not None:
            new_stale["cftc_crowding"] = stale_cftc
        else:
            new_stale.pop("cftc_crowding", None)
        out["cftc_crowding"] = cftc_data
        metrics, tags = _build_headline_metrics_and_tags(
            base.get("live_macro") if isinstance(base.get("live_macro"), dict) else {},
            base.get("us_fred") if isinstance(base.get("us_fred"), dict) else {},
            base.get("china_macro") if isinstance(base.get("china_macro"), dict) else {},
            cftc_crowding=cftc_data,
        )
        out["headline_metrics"] = metrics
        out["regime_tags"] = tags
    out["cache_hit"] = True
    out["sources_status"] = new_sources_status
    out["stale_sources"] = new_stale
    out["regime_signal_lights"] = compute_regime_signal_lights(out)
    return sanitize(out)


def _overlay_polymarket_on_overview(
    base: dict[str, Any],
    pm_tuple: tuple[dict[str, Any], str, dict[str, Any] | None],
) -> dict[str, Any]:
    return _overlay_optional_sources_on_overview(base, pm_tuple=pm_tuple)


def _refresh_cftc_crowding_singleflight(
    *,
    timeout_seconds: float = 8.0,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Coalesce concurrent independent CFTC crowding retries into a single upstream fetch."""
    global _CFTC_RETRY_INFLIGHT_FUTURE
    cftc_cache_key = ("compass_cftc_crowding",)
    hit, existing_cftc = CACHE.get(cftc_cache_key)
    if hit and isinstance(existing_cftc, tuple) and len(existing_cftc) == 3:
        return existing_cftc

    leader = False
    with _CFTC_RETRY_INFLIGHT_LOCK:
        hit2, existing_cftc2 = CACHE.get(cftc_cache_key)
        if hit2 and isinstance(existing_cftc2, tuple) and len(existing_cftc2) == 3:
            return existing_cftc2
        if _CFTC_RETRY_INFLIGHT_FUTURE is not None:
            shared_future = _CFTC_RETRY_INFLIGHT_FUTURE
        else:
            shared_future = concurrent.futures.Future()
            _CFTC_RETRY_INFLIGHT_FUTURE = shared_future
            leader = True

    if not leader:
        return shared_future.result(timeout=max(0.5, timeout_seconds + 2.0))

    try:
        now_iso = _utc_iso()
        try:
            raw_cftc = fetch_cftc_crowding_overview(timeout_seconds=min(8.0, max(0.5, timeout_seconds)))
            err_cftc = None
        except Exception as exc:  # noqa: BLE001
            raw_cftc = None
            err_cftc = str(exc)

        cftc_tuple = _resolve_cftc_crowding_source(raw_cftc, err_cftc, now_iso)
        _, st_cftc, _ = cftc_tuple
        cftc_ttl = TTL_COMPASS_CFTC_SECONDS if st_cftc == "ok" else TTL_COMPASS_CFTC_DEGRADED_SECONDS
        with _CFTC_RETRY_INFLIGHT_LOCK:
            CACHE.set(cftc_cache_key, cftc_tuple, cftc_ttl)
            if not shared_future.done():
                shared_future.set_result(cftc_tuple)
            if _CFTC_RETRY_INFLIGHT_FUTURE is shared_future:
                _CFTC_RETRY_INFLIGHT_FUTURE = None
        return cftc_tuple
    except BaseException as exc:
        with _CFTC_RETRY_INFLIGHT_LOCK:
            if not shared_future.done():
                shared_future.set_exception(exc)
            if _CFTC_RETRY_INFLIGHT_FUTURE is shared_future:
                _CFTC_RETRY_INFLIGHT_FUTURE = None
        raise


def _refresh_polymarket_singleflight(
    *,
    timeout_seconds: float = 6.0,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Coalesce concurrent independent Polymarket retries into a single upstream fetch without mutating cached core data."""
    global _PM_RETRY_INFLIGHT_FUTURE
    pm_cache_key = ("compass_polymarket_odds",)
    hit, existing_pm = CACHE.get(pm_cache_key)
    if hit and isinstance(existing_pm, tuple) and len(existing_pm) == 3:
        return existing_pm

    leader = False
    with _PM_RETRY_INFLIGHT_LOCK:
        # Linearizable check + publish under _PM_RETRY_INFLIGHT_LOCK:
        # leader writes CACHE, completes shared_future, and clears _PM_RETRY_INFLIGHT_FUTURE
        # atomically under this same lock, so a contender sees either the populated cache
        # OR the live in-flight future (never a stale miss followed by a done future).
        hit2, existing_pm2 = CACHE.get(pm_cache_key)
        if hit2 and isinstance(existing_pm2, tuple) and len(existing_pm2) == 3:
            return existing_pm2
        if _PM_RETRY_INFLIGHT_FUTURE is not None:
            shared_future = _PM_RETRY_INFLIGHT_FUTURE
        else:
            shared_future = concurrent.futures.Future()
            _PM_RETRY_INFLIGHT_FUTURE = shared_future
            leader = True

    if not leader:
        return shared_future.result(timeout=max(0.5, timeout_seconds + 2.0))

    try:
        now_iso = _utc_iso()
        try:
            raw_pm = fetch_polymarket_macro_odds(timeout_seconds=min(6.0, max(0.2, timeout_seconds)))
            err_pm = None
        except Exception as exc:  # noqa: BLE001
            raw_pm = None
            err_pm = str(exc)

        pm_tuple = _resolve_polymarket_source(raw_pm, err_pm, now_iso)
        _, st_pm, _ = pm_tuple
        pm_ttl = TTL_COMPASS_MACRO_SECONDS if st_pm == "ok" else TTL_COMPASS_POLYMARKET_DEGRADED_SECONDS
        with _PM_RETRY_INFLIGHT_LOCK:
            CACHE.set(pm_cache_key, pm_tuple, pm_ttl)
            if not shared_future.done():
                shared_future.set_result(pm_tuple)
            if _PM_RETRY_INFLIGHT_FUTURE is shared_future:
                _PM_RETRY_INFLIGHT_FUTURE = None
        return pm_tuple
    except BaseException as exc:
        with _PM_RETRY_INFLIGHT_LOCK:
            if not shared_future.done():
                shared_future.set_exception(exc)
            if _PM_RETRY_INFLIGHT_FUTURE is shared_future:
                _PM_RETRY_INFLIGHT_FUTURE = None
        raise


def collect_macro_overview(
    *,
    refresh: bool = False,
    deadline_seconds: float = DEFAULT_MACRO_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Collect and cache free US-China macro dual-core + 25 cross-asset & CN futures anchors with single-flight coalescing."""
    global _MACRO_INFLIGHT_FUTURE
    cache_key = ("compass_macro_overview",)
    if not refresh:
        hit, cached = CACHE.get(cache_key)
        if hit and isinstance(cached, dict):
            pm_hit, pm_tuple = CACHE.get(("compass_polymarket_odds",))
            resolved_pm: tuple[dict[str, Any], str, dict[str, Any] | None] | None = None
            if pm_hit and isinstance(pm_tuple, tuple) and len(pm_tuple) == 3:
                resolved_pm = pm_tuple
            elif (cached.get("sources_status") or {}).get("polymarket_odds") != "ok":
                resolved_pm = _refresh_polymarket_singleflight(
                    timeout_seconds=min(6.0, deadline_seconds * 0.85),
                )

            cftc_hit, cftc_tuple = CACHE.get(("compass_cftc_crowding",))
            resolved_cftc: tuple[dict[str, Any], str, dict[str, Any] | None] | None = None
            if cftc_hit and isinstance(cftc_tuple, tuple) and len(cftc_tuple) == 3:
                resolved_cftc = cftc_tuple
            elif (cached.get("sources_status") or {}).get("cftc_crowding") not in ("ok", None):
                resolved_cftc = _refresh_cftc_crowding_singleflight(
                    timeout_seconds=min(8.0, deadline_seconds * 0.85),
                )

            if resolved_pm is not None or resolved_cftc is not None:
                return _overlay_optional_sources_on_overview(
                    cached,
                    pm_tuple=resolved_pm,
                    cftc_tuple=resolved_cftc,
                )
            return {**cached, "cache_hit": True}

    leader = False
    with _MACRO_INFLIGHT_LOCK:
        if _MACRO_INFLIGHT_FUTURE is not None and not _MACRO_INFLIGHT_FUTURE.done():
            shared_future = _MACRO_INFLIGHT_FUTURE
        else:
            shared_future = concurrent.futures.Future()
            _MACRO_INFLIGHT_FUTURE = shared_future
            leader = True

    if not leader:
        try:
            res = shared_future.result(timeout=max(0.5, deadline_seconds + 2.0))
            return {**res, "cache_hit": True}
        except Exception:
            hit, cached = CACHE.get(cache_key)
            if hit and isinstance(cached, dict):
                return {**cached, "cache_hit": True}
            raise

    try:
        bundle = _fetch_macro_overview_uncached(deadline_seconds=deadline_seconds, refresh=refresh)
        shared_future.set_result(bundle)
        return bundle
    except BaseException as exc:
        if not shared_future.done():
            shared_future.set_exception(exc)
        raise
    finally:
        with _MACRO_INFLIGHT_LOCK:
            if _MACRO_INFLIGHT_FUTURE is shared_future:
                _MACRO_INFLIGHT_FUTURE = None


# ---------------------------------------------------------------------------
# Layer 1 AI 7-Dimension Strategic Synthesis (Blind Top-Down Sector First)
# ---------------------------------------------------------------------------
_UNIVERSE_MATCH_RULES: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "commodities_energy": (
        {"commodities", "cross_asset"},
        ("原油", "石油", "天然气", "成品油", "柴油", "燃料油", "裂解", "oil", "crude", "natural gas", "diesel", "opec"),
    ),
    "commodities_precious": (
        {"commodities", "cross_asset"},
        ("黄金", "白银", "贵金属", "铂金", "钯金", "gold", "silver", "platinum", "palladium", "precious"),
    ),
    "commodities_base": (
        {"commodities", "cross_asset"},
        ("铜", "铝", "电解铝", "锌", "镍", "锡", "铅", "有色", "基本金属", "工业金属", "copper", "aluminum", "nickel", "zinc", "tin"),
    ),
    "commodities_ferrous": (
        {"commodities", "cross_asset"},
        ("铁矿", "螺纹", "热卷", "钢铁", "焦炭", "焦煤", "黑色", "硅铁", "锰硅", "iron ore", "steel", "ferrous", "coking"),
    ),
    "commodities_grains_oilseeds": (
        {"commodities", "cross_asset"},
        ("大豆", "豆粕", "豆油", "棕榈油", "菜油", "玉米", "小麦", "谷物", "油脂", "grain", "soy", "palm", "corn", "wheat"),
    ),
    "commodities_softs": (
        {"commodities", "cross_asset"},
        ("咖啡", "可可", "白糖", "原糖", "棉花", "软商品", "coffee", "cocoa", "sugar", "cotton"),
    ),
    "commodities_livestock": (
        {"commodities", "cross_asset"},
        ("生猪", "猪周期", "活牛", "畜牧", "养殖", "hog", "cattle", "livestock"),
    ),
    # Strictly specific to commodities actually inside this pool — NEVER match broad "化工"/"chemical" (e.g. phosphate/sulfur/pharma)!
    "commodities_chem_newenergy": (
        {"commodities", "cross_asset"},
        ("碳酸锂", "锂盐", "锂矿", "工业硅", "多晶硅", "光伏硅料", "纯碱", "玻璃", "甲醇", "pta", "pvc", "天然橡胶", "橡胶", "lithium", "polysilicon", "soda ash", "rubber"),
    ),
    "shipping": (
        {"commodities", "cross_asset"},
        ("集运", "欧线", "集装箱运价", "航运期货", "container freight", "ec0"),
    ),
    "metals_equities": (
        {"us_equities", "a_shares", "cross_asset"},
        ("矿业股", "铜矿股", "金矿股", "有色矿业", "稀土矿", "mining", "miners"),
    ),
    "us_power_grid": (
        {"us_equities", "cross_asset"},
        ("电力", "电网", "核电", "公用事业", "变压器", "燃气轮机", "power grid", "utilities", "nuclear"),
    ),
    "us_semis_ai": (
        {"us_equities", "cross_asset"},
        ("半导体", "芯片", "ai算力", "存储芯片", "光模块", "晶圆", "semiconductor", "memory", "hbm"),
    ),
    "us_megacap_cashflow": (
        {"us_equities", "cross_asset"},
        ("大型科技", "科技巨头", "云厂商", "超大规模", "megacap", "hyperscaler"),
    ),
    "us_sectors": (
        {"us_equities", "cross_asset"},
        ("美股军工", "航天军工", "美股医药", "美股金融", "美股工业", "defense", "xlu", "xle", "xlk", "xli", "xlb", "xlv", "xlf"),
    ),
    "a_shares_etfs": (
        {"a_shares", "cross_asset"},
        ("红利", "高股息", "沪深300", "科创50", "券商", "煤炭etf", "dividend"),
    ),
    "a_shares_themes": (
        {"a_shares", "cross_asset"},
        ("a股机器人", "人形机器人", "国产替代", "宁德时代", "贵州茅台"),
    ),
    "fx_majors": (
        {"fx", "cross_asset"},
        ("外汇", "汇率", "美元", "日元", "欧元", "英镑", "澳元", "人民币", "加元", "套息", "fx", "currency", "yen", "eur", "aud", "cny", "usd"),
    ),
    "crypto_majors": (
        {"crypto", "cross_asset"},
        ("加密", "比特币", "以太坊", "数字资产", "稳定币", "crypto", "bitcoin", "btc", "eth", "stablecoin"),
    ),
}


def map_sector_to_universes(
    *,
    title: str,
    asset_scope: str,
    keywords: list[str],
    reference_anchors: list[str],
    universes: dict[str, Any],
) -> list[str]:
    """Deterministically map a blind Layer 1 sector thesis to existing universes.json pools post-hoc."""
    haystack = " ".join([title, *keywords]).lower()
    anchor_upper = {s.strip().upper() for s in reference_anchors if s.strip()}

    scored: list[tuple[int, str]] = []
    for key, pool in universes.items():
        if not isinstance(pool, dict):
            continue
        score = 0
        pool_syms = {str(s).strip().upper() for s in (pool.get("symbols") or [])}
        if anchor_upper and (anchor_upper & pool_syms):
            score += 4

        rule = _UNIVERSE_MATCH_RULES.get(key)
        if rule is not None:
            allowed_scopes, tokens = rule
            if asset_scope in allowed_scopes:
                hits = sum(1 for tok in tokens if tok in haystack)
                if hits > 0:
                    score += 2 * hits
        else:
            # Custom user-saved universes: match by pool description / key tokens
            desc_l = f"{key} {pool.get('description') or ''}".lower()
            kw_hits = sum(1 for kw in keywords if len(kw.strip()) >= 2 and kw.strip().lower() in desc_l)
            if kw_hits > 0:
                score += 3 * kw_hits

        if score >= 2:
            scored.append((score, key))

    scored.sort(key=lambda item: (-item[0], item[1]))
    # Keep up to 2 most specific pools; drop broad ETF anchor pools if a specific thematic pool already matched
    top_keys = [k for _, k in scored[:3]]
    specific = [k for k in top_keys if k not in ("us_sectors", "a_shares_etfs")]
    if specific:
        return specific[:2]
    return top_keys[:2]


def _load_open_positions_for_post_check() -> list[dict[str, str]]:
    """Read current holdings strictly AFTER AI synthesis to surface informational overlap badges."""
    holdings: list[dict[str, str]] = []
    try:
        summary = portfolio_manager.portfolio_summary()
        for pos in summary.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            holdings.append(
                {
                    "symbol": str(pos.get("symbol") or "").strip(),
                    "direction": str(pos.get("direction") or "").strip().lower(),
                    "sector_theme": str(pos.get("sector_theme") or "").strip(),
                }
            )
    except Exception:
        pass
    return holdings


def _detect_portfolio_overlap(
    *,
    title: str,
    keywords: list[str],
    matched_universes: list[str],
    universes: dict[str, Any],
    open_positions: list[dict[str, str]],
) -> list[str]:
    if not open_positions:
        return []
    haystack = " ".join([title, *keywords]).lower()
    pool_syms: set[str] = set()
    for u in matched_universes:
        pool = universes.get(u)
        if isinstance(pool, dict):
            for s in pool.get("symbols") or []:
                pool_syms.add(str(s).strip().upper())

    overlaps: list[str] = []
    for pos in open_positions:
        sym = pos["symbol"].upper()
        theme = pos["sector_theme"]
        dir_lbl = "空" if pos["direction"] == "short" else "多"
        hit = False
        if sym and sym in pool_syms:
            hit = True
        elif theme and len(theme) >= 2 and (theme.lower() in haystack or any(kw.lower() in theme.lower() for kw in keywords if len(kw) >= 2)):
            hit = True
        if hit:
            label = f"{pos['symbol']}({dir_lbl}·{theme})" if theme else f"{pos['symbol']}({dir_lbl})"
            if label not in overlaps:
                overlaps.append(label)
    return overlaps


_RESOLVED_CHANLUN_SHIELD_STATES = frozenset(
    {"bullish_shield_intact", "high_sell_above_zd_watch", "below_zd_breakdown"}
)
MAX_COMPASS_DIRECTIONS = 5


def compute_constitutional_diagnostics(
    *,
    macro_overview: dict[str, Any],
    anchor_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive deterministic, zero-latency telemetry for the 4 Ratified Constitutional Articles."""
    sources_status = macro_overview.get("sources_status") if isinstance(macro_overview.get("sources_status"), dict) else {}
    stale_sources = macro_overview.get("stale_sources") if isinstance(macro_overview.get("stale_sources"), dict) else {}
    fred_is_stale = ("us_fred" in stale_sources) or (sources_status.get("us_fred") == "stale")
    live_is_stale = ("live_macro" in stale_sources) or (sources_status.get("live_macro") == "stale")

    fred_series = ((macro_overview.get("us_fred") or {}).get("series") or {}) if isinstance(macro_overview.get("us_fred"), dict) else {}
    live_macro = macro_overview.get("live_macro") if isinstance(macro_overview.get("live_macro"), dict) else {}

    def _is_obs_fresh(obs_dt: str | None, *, max_age_days: int = 10) -> bool:
        if not obs_dt or not _is_valid_iso_date(obs_dt):
            return False
        return not _is_bar_stale(obs_dt[:10], max_age_days=max_age_days)

    def _fval(sid: str) -> tuple[float | None, float | None, str | None]:
        row = fred_series.get(sid)
        if not isinstance(row, dict) or row.get("status") != "ok":
            return None, None, None
        v = _to_float(row.get("latest_value") if row.get("latest_value") is not None else row.get("latest"))
        dt = str(row.get("latest_date") or "").strip()[:10] or None
        return v, _to_float(row.get("change")), dt

    dfii10, dfii10_chg, dfii10_dt = _fval("DFII10")
    t10yie, _, t10yie_dt = _fval("T10YIE")
    dgs10, dgs10_chg, dgs10_dt = _fval("DGS10")
    hy_oas, hy_oas_chg, hy_oas_dt = _fval("BAMLH0A0HYM2")
    fedfunds, _, fedfunds_dt = _fval("FEDFUNDS")

    dfii10_fresh = (dfii10 is not None) and (not fred_is_stale) and _is_obs_fresh(dfii10_dt, max_age_days=10)
    t10yie_fresh = (t10yie is not None) and (not fred_is_stale) and _is_obs_fresh(t10yie_dt, max_age_days=10)
    dgs10_fresh = (dgs10 is not None) and (not fred_is_stale) and _is_obs_fresh(dgs10_dt, max_age_days=10)
    hy_oas_fresh = (hy_oas is not None) and (not fred_is_stale) and _is_obs_fresh(hy_oas_dt, max_age_days=10)
    fedfunds_fresh = (fedfunds is not None) and (not fred_is_stale) and _is_obs_fresh(fedfunds_dt, max_age_days=45)
    dfii10_status = "ok" if dfii10_fresh else ("stale" if dfii10 is not None else "missing")
    hy_oas_status = "ok" if hy_oas_fresh else ("stale" if hy_oas is not None else "missing")

    vix_obj = live_macro.get("VIX") if isinstance(live_macro.get("VIX"), dict) else {}
    us10y_obj = live_macro.get("US10Y_Yield") if isinstance(live_macro.get("US10Y_Yield"), dict) else {}
    dxy_obj = live_macro.get("DXY_USD_Index") if isinstance(live_macro.get("DXY_USD_Index"), dict) else {}

    vix_live = _to_float(vix_obj.get("last"))
    vix_as_of = str(vix_obj.get("as_of") or "")[:10] or None
    vix_fresh = (vix_live is not None) and (not live_is_stale) and _is_obs_fresh(vix_as_of, max_age_days=7)

    us10y_live = _to_float(us10y_obj.get("last"))
    us10y_as_of = str(us10y_obj.get("as_of") or "")[:10] or None
    us10y_fresh = (us10y_live is not None) and (not live_is_stale) and _is_obs_fresh(us10y_as_of, max_age_days=7)

    dxy_chg_1d = _to_float(dxy_obj.get("chg_1d_pct"))
    dxy_as_of = str(dxy_obj.get("as_of") or "")[:10] or None
    dxy_fresh = (dxy_chg_1d is not None) and (not live_is_stale) and _is_obs_fresh(dxy_as_of, max_age_days=7)

    if us10y_fresh:
        nom_10y = us10y_live
        nom_10y_as_of = us10y_as_of
        nom_10y_stale = False
    elif dgs10_fresh:
        nom_10y = dgs10
        nom_10y_as_of = dgs10_dt
        nom_10y_stale = False
    elif us10y_live is not None:
        nom_10y = us10y_live
        nom_10y_as_of = us10y_as_of
        nom_10y_stale = True
    elif dgs10 is not None:
        nom_10y = dgs10
        nom_10y_as_of = dgs10_dt
        nom_10y_stale = True
    else:
        nom_10y = None
        nom_10y_as_of = None
        nom_10y_stale = False

    # Article 1: Denominator Truth — explicitly distinguish missing, partial, stale, and fresh states
    if t10yie is None or nom_10y is None:
        art1_data_status = "missing"
        art1_state = "unknown_macro_inputs"
        art1_guidance = "通胀预期或名义美债数据缺失：严禁凭缺省值假定通胀稳定或失锚，请通过实时检索独立核验"
    else:
        unanchored_inflation = bool(
            t10yie >= 2.75
            and (nom_10y >= 4.35 or (dgs10_fresh and dgs10_chg is not None and dgs10_chg >= 0.15))
        )
        if (not t10yie_fresh) or nom_10y_stale:
            art1_data_status = "stale"
            art1_state = (
                "stale_unanchored_inflation_warning"
                if unanchored_inflation
                else "stale_anchored_inflation_regime"
            )
            art1_guidance = (
                "【数据源陈旧回退 · 须实时核验】历史快照显示通胀预期偏高叠加名义美债上行，请先联网核实最新美债与通胀预期"
                if unanchored_inflation
                else "【数据源陈旧回退 · 须实时核验】历史快照未见通胀失锚，请联网核实最新美债与通胀预期后再做裁决"
            )
        elif not dfii10_fresh:
            art1_data_status = "stale" if dfii10 is not None else "partial"
            art1_state = (
                "unanchored_inflation_nominal_pressure"
                if unanchored_inflation
                else "anchored_inflation_real_yield_unavailable"
            )
            art1_guidance = (
                "通胀预期偏高叠加名义美债上行：警惕因高通胀压缩算出的低/负实际利率假象（注：10Y实际利率观测陈旧或缺失已置空，请联网复核）"
                if unanchored_inflation
                else "通胀预期未失锚，但 10Y 实际利率（DFII10）缺失或观测陈旧已置空，请联网核实最新实际利率后再对比分子与分母量级"
            )
        else:
            art1_data_status = "ok"
            art1_state = (
                "unanchored_inflation_nominal_pressure"
                if unanchored_inflation
                else "anchored_inflation_regime"
            )
            art1_guidance = (
                "通胀预期偏高叠加名义美债上行：警惕因高通胀压缩算出的低/负实际利率假象，严禁误判为货币宽松去买类债防御或高估值久期权益"
                if unanchored_inflation
                else "通胀预期未失锚：可正常对比分子端盈利/主权买盘与分母端实际利率量级"
            )

    # Article 2 & 3: Pricing Bellwether Shields + Panic / Deep-Hole Short-Chase Ban
    fresh_anchors = [
        it
        for it in anchor_items
        if isinstance(it, dict) and it.get("status") == "ok" and not it.get("stale_fallback")
    ]
    bellwether_shields: list[dict[str, Any]] = []
    deep_hole_ban_symbols: list[dict[str, Any]] = []
    resilient_divergence_clues: list[dict[str, Any]] = []

    tightening_backdrop = bool(
        (dfii10_fresh and dfii10 is not None and dfii10 >= 1.80)
        or (dfii10_fresh and dfii10_chg is not None and dfii10_chg >= 0.15)
        or (dxy_fresh and dxy_chg_1d is not None and dxy_chg_1d >= 0.5)
    )

    for it in fresh_anchors:
        sym = str(it.get("symbol") or "")
        name = str(it.get("name") or sym)
        r20 = _to_float(it.get("ret_20d_pct"))
        r60 = _to_float(it.get("ret_60d_pct"))
        pos6m = _to_float(it.get("range_pos_6m_pct"))
        cl_hint = it.get("chanlun_hint") if isinstance(it.get("chanlun_hint"), dict) else None
        cl_shield = str(cl_hint.get("shield_state") or "") if cl_hint else ""

        if cl_shield in _RESOLVED_CHANLUN_SHIELD_STATES:
            posture = cl_shield
        elif r60 is not None and r60 >= 8.0 and r20 is not None and r20 <= -5.0:
            posture = "high_altitude_pullback_watch"
        elif (r60 is not None and r60 <= -15.0) or (r20 is not None and r20 <= -12.0):
            posture = "deep_hole_crash_zone"
        elif r60 is not None and r60 <= -8.0 and (r20 is None or r20 < 0):
            posture = "sustained_breakdown_pressure"
        elif r60 is not None and r60 > 0 and (r20 is None or r20 >= -4.0):
            posture = "shield_intact_uptrend"
        else:
            posture = "neutral_range"

        bellwether_shields.append(
            {
                "symbol": sym,
                "name": name,
                "ret_20d_pct": r20,
                "ret_60d_pct": r60,
                "range_pos_6m_pct": pos6m,
                "shield_posture": posture,
                **({"chanlun_hint": cl_hint} if cl_hint else {}),
            }
        )

        if (r60 is not None and r60 <= -15.0) or (r20 is not None and r20 <= -12.0):
            deep_hole_ban_symbols.append(
                {
                    "symbol": sym,
                    "name": name,
                    "ret_20d_pct": r20,
                    "ret_60d_pct": r60,
                }
            )

        if tightening_backdrop and ((r20 is not None and r20 >= 5.0) or (r60 is not None and r60 >= 10.0)):
            resilient_divergence_clues.append(
                {
                    "symbol": sym,
                    "name": name,
                    "ret_20d_pct": r20,
                    "ret_60d_pct": r60,
                    "audit_note": "逆紧缩走强侦探线索：须核查信贷与顺周期地基是否稳健，排查晚周期滞后补跌风险，严禁免检追多",
                }
            )

    if vix_live is None:
        vix_data_status = "missing"
        systemic_panic = False
    elif not vix_fresh:
        vix_data_status = "stale"
        systemic_panic = False
    else:
        vix_data_status = "ok"
        systemic_panic = bool(vix_live >= 30.0)

    # Article 2 Credit Spread & CFTC 52-Week Derivatives Crowding Telemetry
    if not hy_oas_fresh or hy_oas is None:
        credit_stress_level = hy_oas_status
    elif hy_oas >= 5.0:
        credit_stress_level = "stress_crisis"
    elif hy_oas >= 4.0 or (hy_oas_chg is not None and hy_oas_chg >= 0.35):
        credit_stress_level = "stress_widening"
    elif hy_oas <= 3.2:
        credit_stress_level = "complacent_tight"
    else:
        credit_stress_level = "normal"

    cftc_raw = macro_overview.get("cftc_crowding") if isinstance(macro_overview.get("cftc_crowding"), dict) else None
    cftc_is_stale_source = (
        (sources_status.get("cftc_crowding") == "stale")
        or bool(cftc_raw and cftc_raw.get("stale_fallback"))
        or str((cftc_raw or {}).get("status") or "") == "stale"
    )
    crowded_long_90pct: list[dict[str, Any]] = []
    crowded_short_10pct: list[dict[str, Any]] = []
    cftc_latest_date: str | None = None
    if cftc_raw is None or not isinstance(cftc_raw.get("items"), list) or len(cftc_raw.get("items") or []) == 0:
        cftc_status = "missing"
    elif cftc_is_stale_source or str(cftc_raw.get("status") or "") not in ("ok", "partial"):
        cftc_status = "stale" if str(cftc_raw.get("status") or "") == "stale" or cftc_is_stale_source else "missing"
        cftc_latest_date = cftc_raw.get("latest_report_date")
    else:
        cftc_status = str(cftc_raw.get("status"))
        cftc_latest_date = cftc_raw.get("latest_report_date")
        for it in cftc_raw.get("items") or []:
            if not isinstance(it, dict) or it.get("is_stale"):
                continue
            compact = {
                "symbol": it.get("symbol"),
                "label": it.get("label"),
                "cohort": it.get("cohort"),
                "net_pct_oi": it.get("net_pct_oi"),
                "percentile_52w": it.get("percentile_52w"),
                "latest_date": it.get("latest_date"),
            }
            if it.get("crowding_state") == "crowded_long":
                crowded_long_90pct.append(compact)
            elif it.get("crowding_state") == "crowded_short":
                crowded_short_10pct.append(compact)

    credit_crowding_notes: list[str] = []
    if credit_stress_level == "stress_crisis":
        credit_crowding_notes.append(f"美高收益信用利差处于高压区({hy_oas:.2f}%)，严防晚周期顺周期权益补跌，谨慎高确信度做多风险资产")
    elif credit_stress_level == "stress_widening":
        credit_crowding_notes.append(f"美高收益信用利差走阔({hy_oas:.2f}%)，须核验实体信用地基再决定做多豁免权")
    elif credit_stress_level in ("missing", "stale"):
        credit_crowding_notes.append("美高收益信用利差(BAMLH0A0HYM2)观测缺失或陈旧，已置空避免误判，须联网核实信贷利差")
    if crowded_long_90pct:
        syms = "/".join(str(x.get("label") or x.get("symbol")) for x in crowded_long_90pct[:3])
        credit_crowding_notes.append(f"CFTC 52周净多头极端拥挤(≥90%分位且净多): {syms}，触及宪法第2条一段护盾警戒，禁止高确信度追多")
    if crowded_short_10pct:
        syms = "/".join(str(x.get("label") or x.get("symbol")) for x in crowded_short_10pct[:3])
        credit_crowding_notes.append(f"CFTC 52周净空头极端拥挤(≤10%分位且净空): {syms}，警惕空头踩踏轧空，谨慎高确信度追空")
    if not credit_crowding_notes:
        credit_crowding_notes.append("信贷利差与CFTC衍生品拥挤度暂未触发极端红线")

    return {
        "article1_denominator_truth": {
            "data_status": art1_data_status,
            "state": art1_state,
            "real_yield_10y_pct": dfii10 if dfii10_fresh else None,
            "real_yield_10y_status": dfii10_status,
            "real_yield_10y_as_of": dfii10_dt,
            "breakeven_10y_pct": t10yie,
            "breakeven_10y_as_of": t10yie_dt,
            "nominal_10y_pct": nom_10y,
            "nominal_10y_as_of": nom_10y_as_of,
            "fedfunds_pct": fedfunds if fedfunds_fresh else None,
            "guidance": art1_guidance,
        },
        "article2_bellwether_shields": bellwether_shields,
        "article2_credit_and_crowding": {
            "hy_oas_status": hy_oas_status,
            "hy_oas_pct": hy_oas if hy_oas_fresh else None,
            "hy_oas_chg_pp": hy_oas_chg if hy_oas_fresh else None,
            "hy_oas_as_of": hy_oas_dt,
            "credit_stress_level": credit_stress_level,
            "cftc_status": cftc_status,
            "cftc_latest_date": cftc_latest_date,
            "crowded_long_90pct": crowded_long_90pct,
            "crowded_short_10pct": crowded_short_10pct,
            "guidance": "；".join(credit_crowding_notes),
        },
        "article3_panic_pendulum_gate": {
            "vix_data_status": vix_data_status,
            "vix_last": vix_live,
            "systemic_panic_active": systemic_panic,
            "deep_hole_short_chase_ban": deep_hole_ban_symbols,
            "put_long_unlock_rule": "深坑区无条件禁直接追空；反手做多流动性兜底必须同时满足：①央行未被高通胀绑手(短端利率停止上行/转向降息) + ②标的未处实质破ZD自由落体",
        },
        "subordinate_evidentiary_clues": {
            "resilient_under_tightening": resilient_divergence_clues[:6],
            "physical_supply_rule": "严禁凭过去涨跌幅臆测商品稀缺或产能过剩；商品多空须联网检索核实未来1Q~1Y真实新增产能投产/减产数据",
        },
    }


def compute_regime_signal_lights(
    macro_overview: dict[str, Any],
    constitutional_diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically compute 4 macro signal lights from hard diagnostics (zero LLM hallucination)."""
    anchor_items = (macro_overview.get("anchors") or {}).get("items") or []
    diag = (
        constitutional_diagnostics
        if isinstance(constitutional_diagnostics, dict)
        else compute_constitutional_diagnostics(macro_overview=macro_overview, anchor_items=anchor_items)
    )
    art1 = diag.get("article1_denominator_truth") if isinstance(diag.get("article1_denominator_truth"), dict) else {}
    art2_cc = diag.get("article2_credit_and_crowding") if isinstance(diag.get("article2_credit_and_crowding"), dict) else {}
    art3 = diag.get("article3_panic_pendulum_gate") if isinstance(diag.get("article3_panic_pendulum_gate"), dict) else {}

    # 1) US Rates & Credit
    hy_level = str(art2_cc.get("credit_stress_level") or "missing")
    hy_val = _to_float(art2_cc.get("hy_oas_pct"))
    real_y = _to_float(art1.get("real_yield_10y_pct"))
    art1_state = str(art1.get("state") or "")
    art1_status = str(art1.get("data_status") or "missing")
    if hy_level == "stress_crisis" or art1_state == "unanchored_inflation_nominal_pressure":
        us_level = "red"
        us_tag = "信用高压/通胀失锚" if hy_level == "stress_crisis" else "通胀预期失锚"
    elif hy_level == "stress_widening" or (real_y is not None and real_y >= 2.0):
        us_level = "amber"
        us_tag = "利差走阔/高实际利率" if hy_level == "stress_widening" else "实际利率紧缩"
    elif art1_status == "ok" and hy_level in ("calm", "normal"):
        us_level = "green"
        us_tag = "分母受控 · 信用平稳"
    else:
        us_level = "neutral"
        us_tag = "部分陈旧/待核验"
    us_parts: list[str] = []
    if real_y is not None:
        us_parts.append(f"实际利率 {real_y:.2f}%")
    if hy_val is not None:
        us_parts.append(f"HY OAS {hy_val:.2f}%")
    us_metric = " · ".join(us_parts) if us_parts else "数据待核验"

    # 2) Volatility & Breadth
    vix_val = _to_float(art3.get("vix_last"))
    vix_status = str(art3.get("vix_data_status") or "missing")
    breadth = (macro_overview.get("tape_movers") or {}).get("regime_breadth") or {}
    adv_20 = breadth.get("advancing_20d_count")
    dec_20 = breadth.get("declining_20d_count")
    total_20 = (int(adv_20) + int(dec_20)) if isinstance(adv_20, (int, float)) and isinstance(dec_20, (int, float)) else 0
    adv_ratio = (float(adv_20) / total_20) if total_20 > 0 else None
    if bool(art3.get("systemic_panic_active")) or (vix_val is not None and vix_status == "ok" and vix_val >= 30.0):
        vol_level = "red"
        vol_tag = "VIX 恐慌极值"
    elif (vix_val is not None and vix_status == "ok" and vix_val >= 20.0) or (adv_ratio is not None and adv_ratio < 0.35):
        vol_level = "amber"
        vol_tag = "波动抬升/宽度偏弱"
    elif vix_val is not None and vix_status == "ok" and vix_val < 20.0:
        vol_level = "green"
        vol_tag = "波动温和"
    else:
        vol_level = "neutral"
        vol_tag = "波动观测待核验"
    vol_parts: list[str] = []
    if vix_val is not None:
        vol_parts.append(f"VIX {vix_val:.1f}")
    if total_20 > 0:
        vol_parts.append(f"20D {int(adv_20)}涨/{int(dec_20)}跌")
    vol_metric = " · ".join(vol_parts) if vol_parts else "数据待核验"

    # 3) Derivatives Crowding (CFTC 52W)
    cftc_status = str(art2_cc.get("cftc_status") or "missing")
    crowded_l = art2_cc.get("crowded_long_90pct") or []
    crowded_s = art2_cc.get("crowded_short_10pct") or []
    ext_cnt = len(crowded_l) + len(crowded_s)
    if cftc_status not in ("ok", "partial"):
        cftc_level = "neutral"
        cftc_tag = "持仓观测陈旧/缺失"
        cftc_metric = "CFTC 待更新"
    elif ext_cnt >= 2:
        cftc_level = "red"
        cftc_tag = f"{ext_cnt} 品种极端拥挤"
        names = [str(x.get("label") or x.get("symbol")) for x in [*crowded_l, *crowded_s][:3]]
        cftc_metric = " / ".join(names)
    elif ext_cnt == 1:
        cftc_level = "amber"
        first = crowded_l[0] if crowded_l else crowded_s[0]
        side = "极多" if crowded_l else "极空"
        cftc_tag = f"1 品种{side}拥挤"
        cftc_metric = f"{first.get('label') or first.get('symbol')} ({first.get('percentile_52w')}%)"
    else:
        cftc_level = "green"
        cftc_tag = "拥挤度中性"
        cftc_metric = "0 极端品种"

    # 4) China Cycle (Manufacturing PMI)
    cn_raw = macro_overview.get("china_macro") if isinstance(macro_overview.get("china_macro"), dict) else {}
    pmi_obj = cn_raw.get("pmi_manufacturing") if isinstance(cn_raw.get("pmi_manufacturing"), dict) else {}
    pmi_val = _to_float(pmi_obj.get("latest") if pmi_obj.get("latest") is not None else pmi_obj.get("latest_value"))
    if pmi_val is None:
        pmi_block = cn_raw.get("pmi") if isinstance(cn_raw.get("pmi"), dict) else {}
        pmi_records = pmi_block.get("records") if isinstance(pmi_block.get("records"), list) else []
        if pmi_records and isinstance(pmi_records[0], dict):
            pmi_val = _to_float(
                pmi_records[0].get("制造业-指数")
                or pmi_records[0].get("manufacturing_pmi")
                or pmi_records[0].get("value")
            )
    if pmi_val is None:
        for hm in macro_overview.get("headline_metrics") or []:
            if isinstance(hm, dict) and str(hm.get("key") or "").upper() in ("CN_PMI", "CHINA_PMI"):
                pmi_val = _to_float(hm.get("value"))
                if pmi_val is not None:
                    break
    stale_sources = macro_overview.get("stale_sources") or {}
    sources_status = macro_overview.get("sources_status") or {}
    cn_stale = (
        ("china_macro" in stale_sources)
        or ("cn_macro" in stale_sources)
        or (sources_status.get("china_macro") == "stale")
        or (sources_status.get("cn_macro") == "stale")
    )
    if pmi_val is None or cn_stale:
        cn_level = "neutral"
        cn_tag = "PMI 待核验" if pmi_val is None else "PMI 陈旧回退"
        cn_metric = f"PMI {pmi_val:.1f}" if pmi_val is not None else "暂无"
    elif pmi_val >= 50.5:
        cn_level = "green"
        cn_tag = "景气扩张"
        cn_metric = f"PMI {pmi_val:.1f}"
    elif pmi_val >= 49.5:
        cn_level = "amber"
        cn_tag = "荣枯线震荡"
        cn_metric = f"PMI {pmi_val:.1f}"
    else:
        cn_level = "red"
        cn_tag = "景气收缩"
        cn_metric = f"PMI {pmi_val:.1f}"

    return [
        {"id": "us_rates_credit", "label": "美债与信用", "level": us_level, "tag": us_tag, "metric": us_metric},
        {"id": "volatility_breadth", "label": "波动与宽度", "level": vol_level, "tag": vol_tag, "metric": vol_metric},
        {"id": "derivatives_crowding", "label": "衍生品拥挤", "level": cftc_level, "tag": cftc_tag, "metric": cftc_metric},
        {"id": "china_cycle", "label": "中国周期", "level": cn_level, "tag": cn_tag, "metric": cn_metric},
    ]


def build_compass_brief(
    *,
    macro_overview: dict[str, Any],
    focus_prompt: str = "",
    focus_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble a strictly unanchored macro brief (NO existing_universes, NO current holdings)."""
    clean_scopes = normalize_focus_scopes(focus_scopes or [])
    anchor_items = (macro_overview.get("anchors") or {}).get("items") or []
    tape_movers = macro_overview.get("tape_movers") or compute_tape_movers(anchor_items)
    constitutional_diagnostics = compute_constitutional_diagnostics(
        macro_overview=macro_overview,
        anchor_items=anchor_items,
    )
    pm_raw = macro_overview.get("polymarket_odds")
    pm_compact: dict[str, Any] = {
        "status": str(pm_raw.get("status") or "unavailable") if isinstance(pm_raw, dict) else "unavailable",
        "fetched_at_utc": pm_raw.get("fetched_at_utc") if isinstance(pm_raw, dict) else None,
        "stale_fallback": bool(pm_raw.get("stale_fallback")) if isinstance(pm_raw, dict) else False,
        "stale_categories": list(pm_raw.get("stale_categories") or []) if isinstance(pm_raw, dict) else [],
        "events": [
            {
                "category": ev.get("category"),
                "category_label": ev.get("category_label"),
                "title": ev.get("title"),
                "market_type": ev.get("market_type"),
                "end_date": ev.get("end_date"),
                "leading_end_date": ev.get("leading_end_date"),
                "end_date_passed": bool(ev.get("end_date_passed")),
                "volume_24h_usd": ev.get("volume_24h_usd"),
                "volume_total_usd": ev.get("volume_total_usd"),
                "liquidity_usd": ev.get("liquidity_usd"),
                "consensus_band": ev.get("consensus_band") or ev.get("priced_in_band"),
                "leading_option": ev.get("leading_option"),
                "leading_prob_pct": ev.get("leading_prob_pct"),
                "leading_chg_1d_pp": ev.get("leading_chg_1d_pp"),
                "leading_chg_7d_pp": ev.get("leading_chg_7d_pp"),
                "stale_fallback": bool(ev.get("stale_fallback")),
                "top_outcomes": [
                    {
                        "label": o.get("label"),
                        "question": o.get("question"),
                        "prob_pct": o.get("prob_pct"),
                        "chg_1d_pp": o.get("chg_1d_pp"),
                        "chg_7d_pp": o.get("chg_7d_pp"),
                        "end_date": o.get("end_date"),
                        "volume_24h_usd": o.get("volume_24h_usd"),
                    }
                    for o in (ev.get("top_outcomes") or [])
                    if isinstance(o, dict)
                ],
                "url": ev.get("url") or "",
            }
            for ev in ((pm_raw.get("events") or []) if isinstance(pm_raw, dict) else [])
            if isinstance(ev, dict)
        ],
    }
    cftc_raw = macro_overview.get("cftc_crowding")
    cftc_compact: dict[str, Any] = {
        "status": str(cftc_raw.get("status") or "unavailable") if isinstance(cftc_raw, dict) else "unavailable",
        "latest_report_date": cftc_raw.get("latest_report_date") if isinstance(cftc_raw, dict) else None,
        "stale_fallback": bool(cftc_raw.get("stale_fallback")) if isinstance(cftc_raw, dict) else False,
        "items": [
            {
                "symbol": it.get("symbol"),
                "label": it.get("label"),
                "cohort": it.get("cohort"),
                "net_pct_oi": it.get("net_pct_oi"),
                "percentile_52w": it.get("percentile_52w"),
                "crowding_state": it.get("crowding_state"),
                "latest_date": it.get("latest_date"),
                "is_stale": bool(it.get("is_stale")),
            }
            for it in ((cftc_raw.get("items") or []) if isinstance(cftc_raw, dict) else [])
            if isinstance(it, dict)
        ],
    }
    return sanitize(
        {
            "generated_at_utc": _utc_iso(),
            "focus_scopes": [
                {"id": sid, "label": _FOCUS_SCOPE_MAP[sid]["label"]}
                for sid in clean_scopes
            ],
            "focus_prompt": focus_prompt.strip(),
            "seven_dimensions": list(SEVEN_DIMENSIONS),
            "tape_movers": tape_movers,
            "constitutional_diagnostics": constitutional_diagnostics,
            "polymarket_odds": pm_compact,
            "cftc_crowding": cftc_compact,
            "macro_overview": {
                "fetched_at_utc": macro_overview.get("fetched_at_utc"),
                "sources_status": macro_overview.get("sources_status"),
                "stale_sources": macro_overview.get("stale_sources") or {},
                "regime_tags": macro_overview.get("regime_tags"),
                "headline_metrics": macro_overview.get("headline_metrics"),
                "china_macro": macro_overview.get("china_macro"),
                "cross_asset_anchors": anchor_items,
            },
        }
    )


def build_compass_prompt(
    *,
    brief_path: str,
    focus_prompt: str = "",
    focus_scopes: list[str] | None = None,
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    clean_scopes = normalize_focus_scopes(focus_scopes or [])
    scopes_block = ""
    if clean_scopes:
        lines = [
            f"- **{ _FOCUS_SCOPE_MAP[sid]['label'] }**：{_FOCUS_SCOPE_MAP[sid]['prompt_hint']}"
            for sid in clean_scopes
        ]
        scopes_block = (
            "\n【用户锁定的重点深挖战场（多选 · 必须严格聚焦）】\n"
            "用户本次明确指定只深挖以下战场，请将全部方向卡片集中分配在这些战场内（仅输出真正过线的高把握方向卡 1~5 张，不要把名额浪费在未勾选市场；若当前信号严重冲突、拥挤度极端或缺乏真正高赔率预期差，允许行使宪法第 4 条全市场战略留白权输出空数组并填写充分的 `abstain_reason` 与 `avoid_sectors`）：\n"
            + "\n".join(lines)
            + "\n"
        )
    focus_block = f"\n【用户补充说明】\n{focus_prompt.strip()}\n" if focus_prompt.strip() else ""
    return (
        f"你是 T3 Trading Desk（中低频多智能体投研台 · 缠论版）的第一层（Layer 1）全球自上而下首席策略师。今天是 {today}。\n"
        f"请先使用 Read 工具读取当前目录下的简报文件 `{brief_path}`，再以**空白大脑（无任何预设标的池锚定）**执行实时 WebSearch / WebFetch，\n"
        "从当下真实世界正在发生的最大级别宏观、地缘、政策、供需与产业变化出发，只回答第一层最核心的战略问题：\n"
        "**“在当前环境下，未来 1 个季度至 1 年（1Q~1Y）最有逻辑的多空方向和核心板块（Sector / Theme）是什么？产业链该切哪个环节？又该回避哪些逆风板块？”**\n\n"
        "【最高准则：Layer 1 思考宪法（跨半世纪正反周期违宪审查核准 · 享有最高裁决权）】\n"
        "请结合 `brief.json` 中的 `constitutional_diagnostics` 诊断字段，严格按以下 4 条最高宪法与 2 条取证规程推演，拒绝死背静态教科书口诀：\n"
        "- **宪法第 1 条（分母真伪辨识律 · 剥离恶性通胀与温和宽松）**：评判实际利率前先核查通胀锚是否失控——温和通胀下的低/负实际利率利好权益久期；但**因恶性高通胀失锚叠加名义长债飙升压缩算出的低/负实际利率，通杀股票与类债防御板块，此时唯独实物能源与贵金属受益**。\n"
        "- **宪法第 2 条（统治权归属与真龙头两段式护盾律）**：先比分子与分母量级——当板块具备非线性分子爆发（如资本开支与盈利二阶加速）或主权外储刚性买盘、信贷利差平稳且**板块基准定价锚护盾完好（未出日线确认卖点，实物储备须同时处于跨年正趋势）**时，分子统治分母，豁免高利率利空；**一段护盾（高位分歧退观望）**：若基准定价锚在大中枢 `ZD` 上方首现日线确认卖点或衍生品持仓极端拥挤（>90%分位），**立即收回做多豁免权退居观望（列入 `avoid_sectors` 或留白，绝不硬推做多，也绝不在 `ZD` 上方左侧摸顶做空）**；**二段破位警报与分层空头权限**：一旦基准定价锚实质跌破大中枢 `ZD`，哪怕央行正在降息也无条件严禁凭降息惯性抄底（列入回避观察）；至于能否反手输出 `short` 或充当 `pair` 空头腿，必须严守跨资产权限与分层边界——**大宗商品/外汇/加密**在供需过剩或利差逻辑共振下可顺势做空，**美股/美股行业 ETF** 唯有同时具备“高实际利率/金融条件紧缩压制估值 + 未来 1~3 年盈利/CapEx 增长故事证伪或强周期景气见顶反转”的戴维斯双杀基本面前提方可输出空头（具体真龙头日线与 30min 缠论结构交由 Layer 2/4 独立验证），**A 股在任何情形下均严格禁止直接做空及充当 `pair` 配对空头腿**。\n"
        "- **宪法第 3 条（恐慌反应函数二分律 · 禁空绝对化、抄底条件化）**：**上半句（无条件铁律）**：在 `VIX ≥ 30` 恐慌极值或标的已落入深坑暴跌区（见 `deep_hole_short_chase_ban`），**无条件禁止直接市价追空**（若中期仍过剩，必须在 `value_chain_focus` 注明等反抽二卖/三卖）；**下半句（双前提解锁）**：唯有同时满足“① 央行未被高通胀绑手（短端利率停止上行/转向降息）+ ② 标的未处实质破 `ZD` 自由落体”双前提时，才解锁流动性兜底做多，缺一则持币观望。\n"
        "- **宪法第 4 条（认知谦逊与全市场战略留白权 · 废除强凑满额方向卡）**：真正高赔率的主矛盾方向在多数时期只有 **1~4 个**（上限 5 个）。只输出确信度真正过硬的方向卡（`0~5` 张）；在全面紧缩或无主线震荡期宁可只出 **1~2 张** 狙击卡，**严禁为凑满名额塞入二流凑数多头或左侧摸顶空头**！当全市场或所选战场均处于宏观信号剧烈撕裂、共识高度拥挤（如 `article2_credit_and_crowding` 显示信贷高压/CFTC 极端拥挤）或缺乏清晰 1Q~1Y 非对称预期差时，**任何市场范围均可主动行使战略留白权**，输出 `\"directions\": []` 并必填充分的 `\"abstain_reason\"`（≥15字讲清为何持币优于硬开仓）与至少 1 条 `\"avoid_sectors\"`（禁止无理由偷懒留白）。\n"
        "- **二级溯源与取证规程（严禁走捷径）**：① **反常劈叉仅作侦探警报**：对“逆古典宏观走强”的品种，必须先核查全市场顺周期权益与信贷地基——信贷与顺周期企稳才是真新主线；若顺周期权益与信贷已崩盘、仅剩下游高烧商品独强，必须警惕晚周期需求坍塌补跌诱多（Catch-down），严禁免检追涨；② **物理供给严禁靠 K 线臆测**：绝不凭过去涨跌幅猜“稀缺”或“过剩”，商品多空必须联网查实未来 `1Q~1Y` 真实物理新增产能投产表与成本曲线；③ **定价主权归属**：中国内需定价品种（A股/黑色建材/内盘化工）锚定国内信用与产业政策，不机械外推美联储。\n\n"
        "【Layer 1 职责边界与铁律（严格遵守）】\n"
        "1. **极端盘面异动强制归因（Tape-to-Macro Radar · 警报器而非选品清单！）**：\n"
        "   - 简报中的月度宏观数据（如 `CN_PMI`、`FEDFUNDS`、`PCOPPUSDM`）天然滞后 1~2 个月，仅作背景参考；**实时市场定价（`tape_movers` 极端领涨/领跌榜、`cross_asset_anchors` 含国内核心期货锚的近 5D/20D/60D 异动、`VIX`/`US10Y`/`DXY`）与你通过 WebSearch 检索到的近 1~2 周最新变化享有绝对最高优先级**。\n"
        "   - **强制动作**：先逐一审视 `brief.json` 里的 `tape_movers.top_losers` 和 `top_gainers`！凡是 20D/60D 出现 **≥15% 单边走势**的品种/板块（无论海外资产还是国内期货锚），背后往往是**资金方向的高度一致（未背驰主趋势段）**，必须主动联网查清多维度真实驱动，彻底打破脑海里残留的卖方旧共识钢印（例如“某金属长期短缺”或“某赛道长期高增”）。\n"
        "   - **警惕两种极端**：① 严禁凭“跌多了就该涨”的均值回归思维机械否定正在延续的主趋势；② 同样严禁**后视镜线性外推**——把过去 60 天跌幅榜前几名原样照搬成未来 1 年的结论！查清驱动后，必须前瞻追问：**“市场已经计价了什么（`priced_in_already`）？未来 1Q~1Y 还有哪些尚未被充分定价的增量预期差或二阶传导（`unpriced_gap`）？”**\n"
        "   - **预测市场事件概率参考（`polymarket_odds` · 不等同于资产已计价！）**：`brief.json` 附带 Polymarket 真实资金盘口对宏观利率、关税贸易与地缘事件的发生概率及 1D/7D 赔率变动（`pp`）。其中 `market_type == \"date_ladder\"` 表示多期限阶梯（每个子盘口对应独立截止日期 `end_date`，严禁将近期截止日的高概率混同为远期截止日）。请将高概率/显著 7D 重定价作为**事件发生概率的盘口共识与边际预期变化线索**，**严禁把盘口高概率（如 ≥80%）直接等同于相关资产价格已经充分计价**——判断 `priced_in_already` 与 `unpriced_gap` 时，必须用相关资产价格走势、利率/期货期限结构、估值分位或事件窗口市场反应独立核验，且留意标注了 `stale_fallback` 或较低 24h 成交量的盘口。\n"
        "2. **只选方向与板块，不代用户硬选具体个股/唯一龙头**：Layer 1 负责把宏观大局、多空方向、核心板块及产业链选龙头抓手（`value_chain_focus`）讲透；具体挑哪只真龙头并做日线缠论验证，交由用户在下方 Layer 2 自主完成。你可以可选填 0~2 个板块基准定价锚（如一级行业 ETF 或主流商品/汇率基准代码），但绝不要越俎代庖替用户锁定某一只个股。\n"
        "3. **大逻辑先行（严禁凭 RSI/超买超卖逆势猜底摸顶）**：不使用 RSI/布林带/均值回归作为推荐依据，只做真正具备 1Q~1Y 基本面预期差、戴维斯双击/双杀、供需失衡或宏观流动性共振的板块。\n"
        "4. **7 维驱动共振门槛（强制 ≥2 个不同维度共振，且均须有非空摘要）**：每个板块必须明确标注主驱动维度 + 不同的次驱动维度，规范名限定为：\n"
        "   - `宏观双核`（美国利率/美债/美元流动性 + 中国财政/地产/信用周期）\n"
        "   - `地缘与资源国政策`（地缘冲突、贸易与航运咽喉、资源国出口/配额/矿业税/收储政策、关税壁垒）\n"
        "   - `技术与产业迭代`（核心技术代际切换、卡脖子基础设施瓶颈、商业化景气拐点或 CapEx 证伪下修）\n"
        "   - `商品供需平衡与库存周期`（未来1Q~1Y新增产能投产爬坡 vs 下游需求增量或坍塌、产能出清、气候冲击）\n"
        "   - `产业政策与监管`（财政补贴、限产/反内卷与产能整合、行业监管或法案审批）\n"
        "   - `资金流与市场结构`（机构配置/ETF申赎、衍生品持仓拥挤度踩踏、套息交易平仓）\n"
        "   - `突发供给冲击`（主产区不可抗力、停产、物流封锁或突发禁令）\n"
        "5. **前瞻因子分散 + 跨资产分层权限 + 产业链传导（拒绝伪平衡，也拒绝多张卡全赌单一因子！）**：\n"
        "   - **多空服从未来预期差，同时避免单一宏观因子过度拥挤**：绝不要为了显得“攻守平衡”硬凑弱逻辑多头，更**严禁把 20D/60D 正在深跌（如跌幅超 -10%）的弱势资产硬塞进 `pair` 的多头腿去对冲空头利润**！但同样严禁把多张方向卡重复押在同一个宏观因子（例如全部都在赌“美联储持续鹰派紧缩”）上——主趋势中继方向只保留赔率最锋利、增量预期差最明确的代表，其余名额仅在存在**低相关独立供给/政策周期**或正在酝酿的**下一季度宏观二阶传导/拐点方向**时分配（若无则果断少出卡，行使宪法第 4 条留白权）。\n"
        "   - **跨资产分层权限（美股双杀可空 · A股严禁做空 · 商品/外汇/加密全对称）**：\n"
        "     - **美股 / 美股行业 ETF**：景气上行期做多；而在**高实际利率压制估值 + 未来 1~3 年盈利/CapEx 增长故事证伪（戴维斯双杀）**的熊市主跌段，**允许直接输出 `short` 做空方向卡（或作为 `pair` 空头腿）**！\n"
        "     - **A 股**：受限于融券制度，**严格禁止做空（含配对空头腿）**，仅允许 `long`。\n"
        "     - **大宗商品（海外 + 国内期货）、外汇（FX）、加密货币**：多空完全对称，`short` 做空与 `long` 同等重要！商品即使短期低库存呈大 B 结构，强现实弱预期劈叉照样可以做空。\n"
        "   - **强制追踪跨资产产业链传导**：每当某个超大权重权益主线进入戴维斯双杀或宏观收缩（例如算力硬件/半导体 CapEx 放缓、或中国地产/信用收缩），必须顺藤摸瓜同时审视其**上游工业/电子金属（如电子焊料金属、数据中心用铜、黑色原料）与商品货币**的联动空头机会——最强的主跌空头直接升格进 `directions`！\n"
        "   - **每一条 `avoid_sectors` 逆风点都必须延伸出可落地策略（`derived_strategy`）**：说明能否直接通过美股/ETF、商品期货、外汇或加密空头表达（A 股严禁直接做空，但可延伸至上游商品/外汇代理空头或反向受益轮动），并如实填写 `short_proxy_possible` 与 `short_proxy_hint`。\n"
        "6. **0-编造红线与硬证据底线**：引用宏观数据必须与 `brief.json`（含 `article2_credit_and_crowding` 信用利差与 CFTC 52周持仓拥挤度）或最新联网来源一致（且标明日期）；每个板块必须给出 **≥2 条**真实联网证据，且**至少包含 1 条 `L1` 或 `L2` 级别硬事实/官方数据/权威指引证据**（严禁仅凭 `L3/L4` 传闻或研报成卡），含精确 `YYYY-MM-DD` 日期、来源机构名与可验证的 `https://` 链接。\n"
        f"{scopes_block}"
        f"{focus_block}\n"
        "【输出格式铁律】\n"
        "你的最终回复必须**仅包含一个合法的 JSON 对象**（可用 ```json ... ``` 包裹，JSON 外不要输出任何额外说明文字），严格遵循以下 schema：\n"
        "```json\n"
        "{\n"
        '  "regime_title": "15~32字精炼概括当前全球宏观总定调",\n'
        '  "regime_summary": "2~4句清晰判断当下美国利率/美元 + 中国信用/财政双核所处象限，结合盘面宽度判断未来1Q~1Y资金主战场与整体攻守姿态",\n'
        '  "macro_signals": {\n'
        '    "us_macro": "美国利率/通胀/美元流动性现状与未来1~2季路径（1句）",\n'
        '    "china_macro": "中国财政/地产/PMI/出口脉动与对周期及股市的含义（1句）",\n'
        '    "geopolitics_supply": "当前全球最关键的地缘/关税/资源国供给焦点（1句）",\n'
        '    "tech_industry": "当前科技与制造业产业链最核心的景气迭代或CapEx证伪主线（1句）"\n'
        "  },\n"
        '  "avoid_sectors": [\n'
        "    {\n"
        '      "sector": "当下处于逆风的板块/方向",\n'
        '      "reason": "1句讲清为何处于逆风或预期证伪",\n'
        '      "derived_strategy": "1~2句延伸策略：说明如何通过美股ETF/商品期货/外汇/加密空头表达，或A股规避+跨资产代理做空/反向轮动",\n'
        '      "short_proxy_possible": true,\n'
        '      "short_proxy_hint": "若存在合规空头/代理（A股严禁直接做空；美股ETF/商品/外汇/加密均可），简写工具方向；无则空字符串"\n'
        "    }\n"
        "  ],\n"
        '  "directions": [\n'
        "    {\n"
        '      "id": "sector_unique_snake_case",\n'
        '      "rank": 1,\n'
        '      "title": "核心板块/赛道名称（聚焦板块而非单只股票，<=24字）",\n'
        '      "direction": "long | short | pair",\n'
        '      "horizon": "1Q | 1Q~2Q | 2Q~1Y | 1Q~1Y",\n'
        '      "conviction": "high | medium",\n'
        '      "asset_scope": "commodities | us_equities | a_shares | fx | crypto | cross_asset",\n'
        '      "primary_driver": {"dimension": "7维规范名之一", "summary": "主驱动逻辑核心要点（1句，必填）"},\n'
        '      "secondary_driver": {"dimension": "另1个不同的7维规范名", "summary": "次驱动共振要点（1句，必填）"},\n'
        '      "thesis": "2~3句讲透未来1Q~1Y该板块的核心预期差、供需/盈利传导链与为什么现在是最优方向（若对应盘面大幅异动须讲清资金一致性驱动）",\n'
        '      "card_headline_gap": "1句极简核心预期差摘要（<=28字，用于主屏卡片单行展示）",\n'
        '      "priced_in_already": "1句客观交代当下市场已经计价了哪些共识/涨跌幅（防止后视镜线性外推）",\n'
        '      "unpriced_gap": "1句明确指出未来1Q~1Y尚未被充分定价的增量预期差、二阶传导或催化",\n'
        '      "value_chain_focus": "1~2句明确产业链最该切入的环节及 Layer 2 选真龙头的硬标准（如上游资源壁垒/成本曲线左端/估值泡沫最脆弱或订单兑现纯度；若该方向20D/60D已暴跌超25%，必须明确提醒 Layer 2 等待反抽二卖/三卖或30min结构卖点，严禁远离中枢盲目追空）",\n'
        '      "long_leg_sector": "仅 pair 配对必填：做多哪一细分板块/品种环节（必须自身具备独立走强逻辑，严禁塞入深跌资产）",\n'
        '      "short_leg_sector": "仅 pair 配对必填（A股严禁做空）：做空哪一过剩/双杀/弱势环节",\n'
        '      "sector_keywords": ["板块中英文核心关键词1", "关键词2", "关键词3"],\n'
        '      "reference_anchors": ["可选0~2个板块基准定价锚代码，用于辅助定位，可空数组"],\n'
        '      "key_catalyst": "未来1~3个月具体催化事件与时间节点",\n'
        '      "invalidation": "明确可证伪红线（什么数据/政策/事件出现即放弃该板块）",\n'
        '      "invalidation_short": "1句极简硬证伪红线（<=22字，用于主屏卡片常显红线）",\n'
        '      "evidence": [\n'
        "        {\n"
        '          "fact": "具体可核验的近期硬事实或数据（带数字/时间）",\n'
        '          "level": "L1 | L2 | L3 | L4",\n'
        '          "date": "YYYY-MM-DD",\n'
        '          "source": "来源机构/媒体名",\n'
        '          "url": "https://...（无则空字符串）"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ],\n"
        '  "abstain_reason": "常规有方向卡时填空字符串；仅当行使宪法第4条战略留白(directions为空数组)时必填1~2句持币观望理由",\n'
        '  "risk_warnings": ["1~3条当前宏观与跨资产层面最需警惕的尾部风险或因子重叠提醒"]\n'
        "}\n"
        "```\n"
        "要求：严格遵循《Layer 1 思考宪法》，`directions` 按真实机会多寡动态输出 **0~5 个**按优先级排序（`rank` 1..N）的高共振板块方向（宁缺毋滥，全面紧缩或震荡期可仅输出 1~2 个最强主矛盾方向，绝不凑数；当缺乏清晰 1Q~1Y 非对称预期差或宏观拥挤撕裂时，任意市场均可输出 `directions: []` 配合充分的 `abstain_reason`（≥15字）与至少 1 条 `avoid_sectors`），必须以**未来 1Q~1Y 尚未充分定价的增量预期差**为核心，每个板块至少 2 条带 `YYYY-MM-DD` 日期与来源的真实证据且至少 1 条为 `L1/L2` 硬证据，严格遵守 A 股禁空铁律（美股双杀、商品、外汇、加密均允许做空）。"
    )


_A_SHARE_MARKET_TOKENS = (
    "a股",
    "沪深",
    "创业板",
    "科创板",
    "北交所",
    "a-share",
    "ashare",
    "上证",
    "深证",
    "中证",
    "中国股票",
    "chinastock",
)
_A_SHARE_NEGATION_CLAUSE_RE = re.compile(
    r"(?:(?:严禁|禁止|避免|不要|切勿|不可|不得|不建议|不能)(?:直接)?(?:做空|空)(?:a股|沪深\d*|创业板|科创板|北交所|中国股票|a-?shares?))"
    r"|(?:(?:a股|沪深\d*|创业板|科创板|北交所)(?:严禁|禁止|不可|不得)?(?:直接)?(?:禁空|做空|严禁做空|禁止做空))"
    r"|(?:do\s*not\s*short\s*a-?shares?)",
    re.IGNORECASE,
)


def _compact_lower(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _mentions_a_share_market(text: str) -> bool:
    norm = _compact_lower(text)
    if not norm:
        return False
    return any(tok in norm for tok in _A_SHARE_MARKET_TOKENS)


def _sanitize_avoid_short_proxy_hint(hint: str) -> tuple[str, bool]:
    """Validate an avoid_sectors short_proxy_hint under Option A (A-share shorts forbidden).

    Returns ``(clean_hint, rejected_as_a_share_short)``.
    Compliant hints that include a negation reminder alongside a valid non-A-share proxy
    (e.g. "避免做空A股，改空铁矿") are preserved; affirmative A-share short hints
    (e.g. "做空 A 股白酒") or bare A-share prohibitions with no alternative proxy are cleared.
    """
    raw = str(hint or "").strip()
    if not raw:
        return "", False
    norm = _compact_lower(raw)
    residual = _A_SHARE_NEGATION_CLAUSE_RE.sub("", norm)
    # Strip punctuation/connectors to check whether any compliant non-A-share proxy remains
    residual_meaningful = re.sub(r"[，,。.;；:：()（）\[\]【】/\\|、\-—\s]+", "", residual)
    if any(tok in residual for tok in _A_SHARE_MARKET_TOKENS):
        return "", True
    if norm != residual and not residual_meaningful:
        return "", True
    return raw, False


def _match_canonical_dimension(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s in SEVEN_DIMENSIONS:
        return s
    for dim in SEVEN_DIMENSIONS:
        if dim in s:
            return dim
    if any(k in s for k in ("宏观", "利率", "美元", "中国", "流动性", "财政")):
        return "宏观双核"
    if any(k in s for k in ("地缘", "资源国", "关税", "出口", "配额", "战争")):
        return "地缘与资源国政策"
    if any(k in s for k in ("技术", "AI", "算力", "电力", "电网", "芯片", "迭代")):
        return "技术与产业迭代"
    if any(k in s for k in ("供需", "库存", "投产", "过剩", "缺口", "天气", "周期")):
        return "商品供需平衡与库存周期"
    if any(k in s for k in ("政策", "监管", "补贴", "反内卷", "限产", "法案")):
        return "产业政策与监管"
    if any(k in s for k in ("资金", "ETF", "CFTC", "套息", "费率", "持仓")):
        return "资金流与市场结构"
    if any(k in s for k in ("矿难", "罢工", "封锁", "突发", "冲击", "疫病")):
        return "突发供给冲击"
    return None


def _parse_strict_driver(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, dict):
        dim = _match_canonical_dimension(raw.get("dimension") or raw.get("dim"))
        summary = str(raw.get("summary") or raw.get("detail") or raw.get("note") or "").strip()
        if not dim or not summary:
            return None
        return {"dimension": dim, "summary": summary}
    text = str(raw or "").strip()
    if "：" in text or ":" in text:
        sep = "：" if "：" in text else ":"
        head, tail = text.split(sep, 1)
        dim = _match_canonical_dimension(head)
        summary = tail.strip()
        if dim and summary:
            return {"dimension": dim, "summary": summary}
    return None


def _sanitize_pool_slug(raw: str) -> str:
    s = _POOL_SLUG_RE.sub("_", str(raw or "").strip().lower()).strip("_")
    return s[:48]


def _make_non_colliding_slug(base_slug: str, existing_keys: set[str]) -> str:
    slug = _sanitize_pool_slug(base_slug) or "new_theme"
    if slug not in existing_keys:
        return slug
    cand = f"{slug[:40]}_theme"
    idx = 2
    while cand in existing_keys:
        cand = f"{slug[:36]}_theme_{idx}"
        idx += 1
    return cand


_PULLBACK_DISCIPLINE_RE = re.compile(
    r"(?:反抽|反弹|二卖|三卖|30min|30分钟|不追空|严禁.{0,6}追空|等待.{0,8}(?:结构|卖点|反抽|反弹)|回抽|pullback|bounce|wait\s+for)",
    re.IGNORECASE,
)
_BOTTOM_CONFIRM_DISCIPLINE_RE = re.compile(
    r"(?:一买|二买|底背驰|底部结构|左侧严禁|等待.{0,8}(?:底|买点|企稳|放量|背驰)|bottom\s+divergence|1st\s+buy|2nd\s+buy)",
    re.IGNORECASE,
)
_RATE_SENSITIVE_DURATION_RE = re.compile(
    r"(?:公用事业|红利|地产|长债|高估值|长久期|软件|saas|reit|xlu|tlt|bond\s*proxy|duration|utilities|real\s*estate)",
    re.IGNORECASE,
)
_BENCHMARK_NAME_ALIAS_TO_SYMBOL: dict[str, str] = {
    "标普500": "SPY",
    "纳指100": "QQQ",
    "半导体": "SMH",
    "电力公用": "XLU",
    "comex黄金": "GC=F",
    "comex白银": "SI=F",
    "comex铜": "HG=F",
    "wti原油": "CL=F",
    "美国天然气": "NG=F",
    "沪铜": "CU0",
    "沪锡": "SN0",
    "沪铝": "AL0",
    "沪镍": "NI0",
    "铁矿石": "I0",
    "碳酸锂": "LC0",
    "欧元/美元": "EURUSD=X",
    "美元/日元": "USDJPY=X",
    "澳元/美元": "AUDUSD=X",
    "美元/人民币": "USDCNY=X",
}

_MACRO_FACTOR_BUCKETS: tuple[dict[str, Any], ...] = (
    {
        "id": "usd_rates_tightening",
        "label_long": "强美元/紧缩利率顺风",
        "label_short": "强美元/高实际利率压制",
        "anchors": frozenset({"DXY", "DX-Y.NYB", "USDJPY=X", "USDCNY=X", "US10Y", "DFII10"}),
        "keywords": ("强美元", "美元走强", "鹰派", "加息", "缩表", "利率上行", "实际利率上行", "美债上行", "紧缩", "tightening", "hawkish", "strong usd", "higher for longer"),
    },
    {
        "id": "china_credit_cycle",
        "label_long": "中国信用/内需周期顺风",
        "label_short": "中国信用/地产内需收缩",
        "anchors": frozenset({"510300.SS", "510880.SS", "I0", "RB0", "HC0", "JM0", "J0", "CU0", "AL0", "NI0", "LC0", "AUDUSD=X"}),
        "keywords": ("中国", "内需", "地产", "社融", "财政刺激", "反内卷", "黑色", "铁矿", "螺纹", "a股", "china", "pmi"),
    },
    {
        "id": "ai_capex_tech",
        "label_long": "AI算力/科技资本开支扩张",
        "label_short": "AI算力/科技资本开支放缓或双杀",
        "anchors": frozenset({"QQQ", "SMH", "NVDA", "SN0", "XLU"}),
        "keywords": ("ai", "算力", "半导体", "光模块", "数据中心", "gpu", "hbm", "capex", "电网", "电力", "机器人"),
    },
    {
        "id": "energy_geopolitics",
        "label_long": "能源供给/地缘溢价",
        "label_short": "能源过剩/地缘溢价回吐",
        "anchors": frozenset({"CL=F", "BZ=F", "NG=F", "SC0", "XLE", "EC0"}),
        "keywords": ("原油", "石油", "天然气", "能源", "opec", "霍尔木兹", "红海", "油运", "航运", "crude", "oil", "lng"),
    },
    {
        "id": "precious_monetary_hedge",
        "label_long": "黄金/主权去美元避险",
        "label_short": "贵金属/避险溢价回落",
        "anchors": frozenset({"GC=F", "SI=F", "AU0", "AG0", "BTC-USD", "BTC"}),
        "keywords": ("黄金", "白银", "贵金属", "央行购金", "去美元", "主权外储", "避险", "gold", "silver", "bitcoin", "比特币"),
    },
    {
        "id": "industrial_metals_supply",
        "label_long": "工业金属供给约束/再通胀",
        "label_short": "工业金属过剩/需求下修",
        "anchors": frozenset({"HG=F", "ALI=F", "COPX", "XLB", "CU0", "AL0", "NI0", "SN0", "LC0"}),
        "keywords": ("铜", "铝", "镍", "锡", "锌", "锂", "碳酸锂", "有色", "矿产", "冶炼", "copper", "aluminum", "nickel", "lithium"),
    },
)


def _extract_card_referenced_symbols(
    *,
    reference_anchors: list[str],
    leaders: list[dict[str, Any]],
    sector_keywords: list[str],
    title: str,
) -> set[str]:
    syms: set[str] = set()
    for a in reference_anchors:
        s = str(a or "").strip().upper()
        if s:
            syms.add(s)
    for ld in leaders:
        if isinstance(ld, dict):
            s = str(ld.get("symbol") or "").strip().upper()
            if s:
                syms.add(s)
    blob_low = " ".join([title, *sector_keywords]).lower()
    for alias, sym in _BENCHMARK_NAME_ALIAS_TO_SYMBOL.items():
        if alias in blob_low:
            syms.add(sym)
    return syms


def _evaluate_card_constitutional_guardrails(
    *,
    title: str,
    direction: str,
    conviction: str,
    asset_scope: str,
    thesis: str,
    value_chain_focus: str,
    invalidation: str,
    sector_keywords: list[str],
    reference_anchors: list[str],
    leaders: list[dict[str, Any]],
    constitutional_diagnostics: dict[str, Any] | None,
) -> tuple[str | None, str, list[dict[str, str]]]:
    """Enforce programmatic constitutional guardrails against macro_overview diagnostics.

    Returns ``(reject_reason_or_none, adjusted_conviction, constitutional_flags)``.
    """
    if not isinstance(constitutional_diagnostics, dict):
        return None, conviction, []

    flags: list[dict[str, str]] = []
    adj_conviction = conviction
    card_syms = _extract_card_referenced_symbols(
        reference_anchors=reference_anchors,
        leaders=leaders,
        sector_keywords=sector_keywords,
        title=title,
    )
    text_blob = f"{title} {thesis} {value_chain_focus} {invalidation} {' '.join(sector_keywords)}"

    # Article 3: Panic / Deep-Hole Short-Chase Guardrail
    art3 = constitutional_diagnostics.get("article3_panic_pendulum_gate")
    if isinstance(art3, dict) and direction == "short":
        systemic_panic = bool(art3.get("systemic_panic_active"))
        deep_hole_syms = {
            str(x.get("symbol") or "").strip().upper()
            for x in (art3.get("deep_hole_short_chase_ban") or [])
            if isinstance(x, dict) and x.get("symbol")
        }
        hit_deep = sorted(card_syms & deep_hole_syms)
        has_pullback_rule = bool(_PULLBACK_DISCIPLINE_RE.search(f"{value_chain_focus} {thesis} {invalidation}"))
        if hit_deep:
            if not has_pullback_rule:
                return (
                    f"{title}: 触发宪法第3条深坑禁直接追空红线（{', '.join(hit_deep)}），"
                    "未明确要求等待反抽二卖/三卖或30min结构卖点",
                    adj_conviction,
                    flags,
                )
            flags.append(
                {
                    "article": "article_3",
                    "severity": "info",
                    "code": "deep_hole_pullback_verified",
                    "message": f"涉及深坑区锚点({', '.join(hit_deep)})，已明确要求等待反抽结构卖点而非市价追空",
                }
            )
        if systemic_panic:
            if not has_pullback_rule:
                adj_conviction = "medium"
                flags.append(
                    {
                        "article": "article_3",
                        "severity": "warning",
                        "action": "cap_conviction_to_medium",
                        "code": "panic_short_pullback_required",
                        "message": "VIX≥30 恐慌极值区间做空，须等待反抽二卖/三卖结构确认（确信度自动封顶 medium）",
                    }
                )
            else:
                flags.append(
                    {
                        "article": "article_3",
                        "severity": "info",
                        "code": "panic_short_pullback_verified",
                        "message": "VIX≥30 恐慌极值区间做空，已附带反抽结构卖点纪律",
                    }
                )

    # Article 2: Bellwether < ZD Breakdown Long Rejection
    shields = constitutional_diagnostics.get("article2_bellwether_shields")
    if isinstance(shields, list) and direction == "long":
        broken_zd_syms: set[str] = set()
        for sh in shields:
            if not isinstance(sh, dict):
                continue
            posture = str(sh.get("shield_posture") or "")
            cl_hint = sh.get("chanlun_hint") if isinstance(sh.get("chanlun_hint"), dict) else {}
            if posture == "broken_below_zd_breakdown" or cl_hint.get("shield_state") == "broken_below_zd_breakdown":
                s = str(sh.get("symbol") or "").strip().upper()
                if s:
                    broken_zd_syms.add(s)
        hit_broken = sorted(card_syms & broken_zd_syms)
        if hit_broken:
            has_bottom_rule = bool(_BOTTOM_CONFIRM_DISCIPLINE_RE.search(f"{value_chain_focus} {thesis} {invalidation}"))
            if not has_bottom_rule:
                return (
                    f"{title}: 触发宪法第2条实质破ZD防线禁逆势抄底（{', '.join(hit_broken)}），"
                    "未明确等待日线底背驰一买/二买结构",
                    adj_conviction,
                    flags,
                )
            adj_conviction = "medium"
            flags.append(
                {
                    "article": "article_2",
                    "severity": "warning",
                    "action": "cap_conviction_to_medium",
                    "code": "broken_zd_bottom_confirmation_only",
                    "message": f"基准锚({', '.join(hit_broken)})仍处破ZD区，仅限等待日线底背驰一买/二买（确信度封顶 medium）",
                }
            )

    # Article 1: Unanchored Inflation vs Long Duration Equities
    art1 = constitutional_diagnostics.get("article1_denominator_truth")
    if (
        isinstance(art1, dict)
        and art1.get("data_status") == "ok"
        and art1.get("state") == "unanchored_inflation_nominal_pressure"
        and direction == "long"
        and asset_scope in ("us_equities", "a_shares", "cross_asset")
        and _RATE_SENSITIVE_DURATION_RE.search(text_blob)
    ):
        adj_conviction = "medium"
        flags.append(
            {
                "article": "article_1",
                "severity": "warning",
                "action": "cap_conviction_to_medium",
                "code": "unanchored_inflation_duration_risk",
                "message": "通胀预期失锚叠加名义长债上行，长久期/类债防御多头受分母压制（确信度自动封顶 medium）",
            }
        )

    # Article 2: Credit Spread Stress & CFTC 52-Week Crowding
    art2_cc = constitutional_diagnostics.get("article2_credit_and_crowding")
    if isinstance(art2_cc, dict):
        credit_level = str(art2_cc.get("credit_stress_level") or "")
        if credit_level == "stress_crisis" and direction == "long" and asset_scope in ("us_equities", "a_shares", "crypto"):
            adj_conviction = "medium"
            hy_v = art2_cc.get("hy_oas_pct")
            flags.append(
                {
                    "article": "article_2",
                    "severity": "warning",
                    "action": "cap_conviction_to_medium",
                    "code": "hy_credit_stress_warning",
                    "message": f"美高收益信用利差高压({hy_v}%)，风险资产做多须防范顺周期晚周期补跌（确信度封顶 medium）",
                }
            )

        if direction == "long":
            crowded_long_map = {
                str(x.get("symbol") or "").strip().upper(): x
                for x in (art2_cc.get("crowded_long_90pct") or [])
                if isinstance(x, dict) and x.get("symbol")
            }
            hit_cl = sorted(card_syms & set(crowded_long_map.keys()))
            if hit_cl:
                adj_conviction = "medium"
                desc = "、".join(
                    f"{crowded_long_map[s].get('label') or s}({crowded_long_map[s].get('percentile_52w')}%)"
                    for s in hit_cl
                )
                flags.append(
                    {
                        "article": "article_2",
                        "severity": "warning",
                        "action": "cap_conviction_to_medium",
                        "code": "cftc_crowded_long_90pct",
                        "message": f"CFTC 52周净多头极端拥挤({desc})，触发一段护盾警戒（确信度封顶 medium）",
                    }
                )
        elif direction == "short":
            crowded_short_map = {
                str(x.get("symbol") or "").strip().upper(): x
                for x in (art2_cc.get("crowded_short_10pct") or [])
                if isinstance(x, dict) and x.get("symbol")
            }
            hit_cs = sorted(card_syms & set(crowded_short_map.keys()))
            if hit_cs:
                adj_conviction = "medium"
                desc = "、".join(
                    f"{crowded_short_map[s].get('label') or s}({crowded_short_map[s].get('percentile_52w')}%)"
                    for s in hit_cs
                )
                flags.append(
                    {
                        "article": "article_2",
                        "severity": "warning",
                        "action": "cap_conviction_to_medium",
                        "code": "cftc_crowded_short_10pct",
                        "message": f"CFTC 52周净空头极端拥挤({desc})，警惕空头踩踏轧空（确信度封顶 medium）",
                    }
                )

    return None, adj_conviction, flags


def analyze_macro_factor_concentration(directions: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically detect cross-card macro beta stacking across directional Layer 1 cards."""
    directional_cards = [d for d in directions if isinstance(d, dict) and d.get("direction") in ("long", "short")]
    pair_count = sum(1 for d in directions if isinstance(d, dict) and d.get("direction") == "pair")
    if len(directional_cards) < 2:
        return {
            "concentrated": False,
            "directional_cards_count": len(directional_cards),
            "hedged_pair_count": pair_count,
            "dominant_factors": [],
            "warnings": [],
        }

    bucket_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
    driver_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for card in directional_cards:
        c_dir = str(card.get("direction") or "")
        p_dim = str((card.get("primary_driver") or {}).get("dimension") or "").strip()
        if p_dim:
            driver_hits.setdefault((p_dim, c_dir), []).append(card)

        card_syms = _extract_card_referenced_symbols(
            reference_anchors=[str(x) for x in (card.get("reference_anchors") or [])],
            leaders=card.get("leaders") if isinstance(card.get("leaders"), list) else [],
            sector_keywords=[str(x) for x in (card.get("sector_keywords") or [])],
            title=str(card.get("title") or ""),
        )
        blob = " ".join(
            [
                str(card.get("title") or ""),
                str(card.get("thesis") or ""),
                str((card.get("primary_driver") or {}).get("summary") or ""),
                str((card.get("secondary_driver") or {}).get("summary") or ""),
                *[str(k) for k in (card.get("sector_keywords") or [])],
            ]
        ).lower()

        for spec in _MACRO_FACTOR_BUCKETS:
            anchor_match = bool(card_syms & spec["anchors"])
            kw_hits = sum(1 for kw in spec["keywords"] if kw in blob)
            if anchor_match or kw_hits >= 2:
                bucket_hits.setdefault((str(spec["id"]), c_dir), []).append(card)

    spec_by_id = {str(s["id"]): s for s in _MACRO_FACTOR_BUCKETS}
    dominant_factors: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_dir = len(directional_cards)

    for (bid, c_dir), cards in sorted(bucket_hits.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(cards) < 2:
            continue
        spec = spec_by_id[bid]
        label = spec["label_long"] if c_dir == "long" else spec["label_short"]
        share_pct = round((len(cards) / total_dir) * 100.0, 1)
        titles = [str(c.get("title") or c.get("id")) for c in cards]
        dominant_factors.append(
            {
                "factor_id": f"{bid}:{c_dir}",
                "label": label,
                "direction": c_dir,
                "count": len(cards),
                "share_pct": share_pct,
                "card_ids": [str(c.get("id") or "") for c in cards],
                "card_titles": titles,
            }
        )
        dir_cn = "做多" if c_dir == "long" else "做空"
        warnings.append(
            f"跨卡宏观因子重叠警戒：{len(cards)}/{total_dir} 张方向卡同向押注【{label}】"
            f"（{dir_cn}：{'、'.join(titles)}），请共享同一份宏观因子风险预算，严禁叠加满仓"
        )

    # Also catch >=3 directional cards stacking the exact same primary dimension & direction
    for (p_dim, c_dir), cards in sorted(driver_hits.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(cards) < 3:
            continue
        share_pct = round((len(cards) / total_dir) * 100.0, 1)
        titles = [str(c.get("title") or c.get("id")) for c in cards]
        dominant_factors.append(
            {
                "factor_id": f"driver:{p_dim}:{c_dir}",
                "label": f"同主驱动({p_dim})",
                "direction": c_dir,
                "count": len(cards),
                "share_pct": share_pct,
                "card_ids": [str(c.get("id") or "") for c in cards],
                "card_titles": titles,
            }
        )
        dir_cn = "做多" if c_dir == "long" else "做空"
        warnings.append(
            f"主驱动维度集中提醒：{len(cards)}/{total_dir} 张方向卡均以【{p_dim}】为同向{dir_cn}主驱动"
            f"（{'、'.join(titles)}），建议优中选优保留赔率最高代表"
        )

    return {
        "concentrated": len(dominant_factors) > 0,
        "directional_cards_count": total_dir,
        "hedged_pair_count": pair_count,
        "dominant_factors": dominant_factors[:5],
        "warnings": warnings[:4],
    }


def normalize_compass_output(
    payload: dict[str, Any],
    *,
    valid_universes: set[str],
    universes: dict[str, Any] | None = None,
    open_positions: list[dict[str, str]] | None = None,
    focus_scopes: list[str] | None = None,
    constitutional_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate blind Layer 1 sector output and perform post-hoc universe/portfolio mapping."""
    if not isinstance(payload, dict):
        raise ValueError("战略推演输出必须是 JSON 对象")

    regime_title = str(payload.get("regime_title") or "").strip()
    regime_summary = str(payload.get("regime_summary") or "").strip()
    if not regime_title or not regime_summary:
        raise ValueError("战略推演缺少核心宏观定调（regime_title / regime_summary）")

    raw_signals = payload.get("macro_signals") if isinstance(payload.get("macro_signals"), dict) else {}
    macro_signals = {
        "us_macro": str(raw_signals.get("us_macro") or "").strip(),
        "china_macro": str(raw_signals.get("china_macro") or "").strip(),
        "geopolitics_supply": str(raw_signals.get("geopolitics_supply") or "").strip(),
        "tech_industry": str(raw_signals.get("tech_industry") or "").strip(),
    }

    raw_avoid_input = payload.get("avoid_sectors")
    avoid_iter = raw_avoid_input if isinstance(raw_avoid_input, list) else []
    avoid_sectors: list[dict[str, Any]] = []
    for item in avoid_iter:
        if isinstance(item, dict):
            sec = str(item.get("sector") or item.get("name") or "").strip()
            rsn = str(item.get("reason") or "").strip()
            derived = str(item.get("derived_strategy") or "").strip()
            proxy_hint, proxy_rejected = _sanitize_avoid_short_proxy_hint(str(item.get("short_proxy_hint") or ""))
            raw_poss = item.get("short_proxy_possible")
            if proxy_rejected:
                short_proxy_possible = False
            else:
                short_proxy_possible = bool(raw_poss) if isinstance(raw_poss, bool) else bool(proxy_hint)
            if sec:
                avoid_sectors.append(
                    {
                        "sector": sec,
                        "reason": rsn,
                        "derived_strategy": derived,
                        "short_proxy_possible": short_proxy_possible,
                        "short_proxy_hint": proxy_hint,
                    }
                )
        elif isinstance(item, str) and item.strip():
            avoid_sectors.append(
                {
                    "sector": item.strip(),
                    "reason": "",
                    "derived_strategy": "",
                    "short_proxy_possible": False,
                    "short_proxy_hint": "",
                }
            )

    clean_scopes = normalize_focus_scopes(focus_scopes or [])
    raw_abstain = payload.get("abstain_reason")
    abstain_reason = raw_abstain.strip() if isinstance(raw_abstain, str) else ""
    raw_dirs = payload.get("directions")
    # Article 4 Full-Market Strategic Abstention Right (with strict anti-laziness guards:
    # explicit empty directions=[], substantive abstain_reason >= 15 chars, and >= 1 avoid_sector).
    can_abstain_zero_cards = (
        isinstance(raw_dirs, list)
        and len(raw_dirs) == 0
        and len(abstain_reason) >= 15
        and isinstance(raw_avoid_input, list)
        and len(avoid_sectors) >= 1
    )
    if can_abstain_zero_cards:
        raw_warn_abstain = payload.get("risk_warnings")
        warn_iter_abstain = raw_warn_abstain if isinstance(raw_warn_abstain, list) else []
        risk_warnings_abstain = [
            str(x).strip()
            for x in warn_iter_abstain
            if isinstance(x, str) and x.strip()
        ][:6]
        return sanitize(
            {
                "regime_title": regime_title,
                "regime_summary": regime_summary,
                "macro_signals": macro_signals,
                "avoid_sectors": avoid_sectors[:6],
                "directions": [],
                "abstained": True,
                "abstain_reason": abstain_reason,
                "abstain_scope": clean_scopes if clean_scopes else ["global_all_markets"],
                "rejected_cards_count": 0,
                "risk_warnings": risk_warnings_abstain,
                "factor_concentration": analyze_macro_factor_concentration([]),
                "constitutional_audit": {
                    "enforced": True,
                    "diagnostics_attached": isinstance(constitutional_diagnostics, dict),
                    "rejected_count": 0,
                    "rejected_reasons": [],
                    "flagged_cards_count": 0,
                    "conviction_caps_applied": 0,
                },
            }
        )
    if not isinstance(raw_dirs, list) or len(raw_dirs) == 0:
        if isinstance(raw_dirs, list) and len(raw_dirs) == 0:
            raise ValueError(
                "战略留白（directions 为空）未通过反偷懒校验：必须填写至少 15 字的实质 abstain_reason 且列出至少 1 条 avoid_sectors"
            )
        raise ValueError("战略推演未输出任何有效板块方向卡片（directions 为空）")

    universes_map = universes if isinstance(universes, dict) else {k: {"symbols": [], "description": ""} for k in valid_universes}
    effective_valid_univs = set(valid_universes) | set(universes_map.keys())
    positions_list = open_positions if open_positions is not None else []

    directions: list[dict[str, Any]] = []
    rejected_reasons: list[str] = []
    seen_ids: set[str] = set()
    capped_card_ids: set[str] = set()

    for idx, raw_d in enumerate(raw_dirs[:10], start=1):
        if not isinstance(raw_d, dict):
            continue
        title = str(raw_d.get("title") or "").strip()
        thesis = str(raw_d.get("thesis") or "").strip()
        if not title or not thesis:
            rejected_reasons.append(f"#{idx}: 缺少板块名称或核心论点")
            continue

        raw_dir = str(raw_d.get("direction") or "").strip().lower()
        if raw_dir not in VALID_COMPASS_DIRECTIONS:
            rejected_reasons.append(f"{title}: 非法方向 {raw_dir!r}")
            continue
        direction = raw_dir

        raw_scope = str(raw_d.get("asset_scope") or "cross_asset").strip().lower()
        asset_scope = raw_scope if raw_scope in VALID_ASSET_SCOPES else "cross_asset"

        # Option A policy: A-shares strictly forbidden as pure short or pure A-share pair; US equities/ETFs allowed
        if direction == "short" and asset_scope == "a_shares":
            rejected_reasons.append(f"{title}: 违反 A 股禁空铁律")
            continue
        if direction == "short" and _mentions_a_share_market(title):
            rejected_reasons.append(f"{title}: 做空标题指向 A 股，违反 A 股禁空铁律")
            continue
        if direction == "pair" and asset_scope == "a_shares":
            rejected_reasons.append(f"{title}: 纯 A 股配对违反 A 股禁空铁律")
            continue

        # Enforce 2 DISTINCT valid driving dimensions with non-empty summaries
        primary_driver = _parse_strict_driver(raw_d.get("primary_driver"))
        secondary_driver = _parse_strict_driver(raw_d.get("secondary_driver"))
        if not primary_driver or not secondary_driver:
            rejected_reasons.append(f"{title}: 缺少合法的 7 维主/次驱动或摘要为空")
            continue
        if primary_driver["dimension"] == secondary_driver["dimension"]:
            rejected_reasons.append(f"{title}: 主次驱动维度相同（未达成 >=2 维共振）")
            continue

        value_chain_focus = str(raw_d.get("value_chain_focus") or "").strip()
        long_leg_sector = str(raw_d.get("long_leg_sector") or "").strip()
        short_leg_sector = str(raw_d.get("short_leg_sector") or "").strip()

        # Optional reference anchors (normalized; A-share short rejects the card outright instead of silent stripping)
        reference_anchors: list[str] = []
        seen_anchors: set[tuple[str, str]] = set()
        has_forbidden_a_share_anchor = False
        for raw_a in raw_d.get("reference_anchors") or []:
            s = str(raw_a or "").strip()
            if not s:
                continue
            try:
                norm_s, ac = market_snapshot.normalize_symbol(s)
            except Exception:
                continue
            if direction == "short" and ac in LONG_ONLY_EQUITY_ASSET_CLASSES:
                has_forbidden_a_share_anchor = True
                break
            key = (ac, norm_s.upper())
            if key in seen_anchors:
                continue
            seen_anchors.add(key)
            reference_anchors.append(norm_s)
            if len(reference_anchors) >= 2:
                break
        if has_forbidden_a_share_anchor:
            rejected_reasons.append(f"{title}: 做空方向包含 A 股参考锚，违反 A 股禁空铁律")
            continue

        # Optional legacy/hint leaders (strictly validated if present, NEVER required in sector-first mode)
        raw_leaders_input = raw_d.get("leaders") or []
        long_leaders: list[dict[str, str]] = []
        short_leaders: list[dict[str, str]] = []
        seen_canonical: set[tuple[str, str]] = set()
        has_forbidden_a_share_short_leader = False
        for raw_l in raw_leaders_input:
            if not isinstance(raw_l, dict):
                continue
            raw_sym = str(raw_l.get("symbol") or "").strip()
            if not raw_sym:
                continue
            try:
                norm_sym, ac = market_snapshot.normalize_symbol(raw_sym)
            except Exception:
                continue
            canonical_key = (ac, norm_sym.upper())
            if canonical_key in seen_canonical:
                continue

            leg_raw = str(raw_l.get("leg") or ("short" if direction == "short" else "long")).strip().lower()
            if leg_raw not in ("long", "short"):
                continue
            if leg_raw == "short" and ac in LONG_ONLY_EQUITY_ASSET_CLASSES:
                has_forbidden_a_share_short_leader = True
                continue
            if direction == "long" and leg_raw != "long":
                continue
            if direction == "short" and leg_raw != "short":
                continue

            role_raw = str(raw_l.get("role") or "sole_leader").strip().lower()
            role = "co_leader" if role_raw == "co_leader" else "sole_leader"
            entry = {
                "symbol": norm_sym,
                "asset_class": ac,
                "name": str(raw_l.get("name") or norm_sym).strip(),
                "leg": leg_raw,
                "role": role,
                "reason": str(raw_l.get("reason") or "").strip(),
            }
            if leg_raw == "long":
                if len(long_leaders) >= 2:
                    continue
                if len(long_leaders) == 1 and role != "co_leader":
                    entry["role"] = "co_leader"
                seen_canonical.add(canonical_key)
                long_leaders.append(entry)
            else:
                if len(short_leaders) >= 2:
                    continue
                if len(short_leaders) == 1 and role != "co_leader":
                    entry["role"] = "co_leader"
                seen_canonical.add(canonical_key)
                short_leaders.append(entry)

        if has_forbidden_a_share_short_leader:
            rejected_reasons.append(f"{title}: 空头腿包含 A 股标的，违反 A 股禁空铁律")
            continue

        # If caller/model explicitly provided leaders, enforce strict leg hygiene; otherwise allow sector-only cards
        if raw_leaders_input:
            if direction == "long" and not long_leaders:
                rejected_reasons.append(f"{title}: 做多方向无有效多头标的")
                continue
            if direction == "short" and not short_leaders:
                rejected_reasons.append(f"{title}: 做空方向无有效合规空头标的（A 股禁空）")
                continue
            if direction == "pair" and (not long_leaders or not short_leaders):
                rejected_reasons.append(f"{title}: 配对方向缺少多头或合规空头腿（A 股禁空）")
                continue
            leaders = [*long_leaders[:1], *short_leaders[:1]] if direction == "pair" else (long_leaders if direction == "long" else short_leaders)
        else:
            leaders = []
            if direction == "pair":
                if not long_leg_sector or not short_leg_sector:
                    rejected_reasons.append(f"{title}: 配对板块必须明确多头腿与合规空头腿")
                    continue
                if _mentions_a_share_market(short_leg_sector):
                    rejected_reasons.append(f"{title}: 配对空头腿禁止为 A 股")
                    continue

        # Enforce >=2 valid evidence items with ISO date + non-empty source + strict URL hygiene
        evidence: list[dict[str, str]] = []
        for raw_e in raw_d.get("evidence") or []:
            if not isinstance(raw_e, dict):
                continue
            fact = str(raw_e.get("fact") or raw_e.get("metric") or "").strip()
            source = str(raw_e.get("source") or "").strip()
            ev_date = str(raw_e.get("date") or raw_e.get("published_at") or "").strip()[:10]
            if not fact or not source or not _is_valid_iso_date(ev_date):
                continue
            level = str(raw_e.get("level") or "").strip().upper()
            if level not in research_archive.VALID_EVIDENCE_LEVELS:
                continue
            raw_url = str(raw_e.get("url") or raw_e.get("source_url") or "").strip()
            clean_url = raw_url if research_archive.is_valid_http_url(raw_url) else ""
            evidence.append(
                {
                    "fact": fact,
                    "level": level,
                    "date": ev_date,
                    "source": source,
                    "url": clean_url,
                    "verification_status": "ai_cited_unverified",
                }
            )
            if len(evidence) >= 5:
                break

        if len(evidence) < 2:
            rejected_reasons.append(f"{title}: 有效证据不足 2 条（需含有效日期、来源与 L1~L4 等级）")
            continue

        if not any(ev["level"] in ("L1", "L2") for ev in evidence):
            rejected_reasons.append(f"{title}: 缺少 L1/L2 一手硬证据支撑（禁止仅凭 L3/L4 传闻或研报成卡）")
            continue

        did = _sanitize_pool_slug(str(raw_d.get("id") or f"sector_{idx}")) or f"sector_{idx}"
        while did in seen_ids:
            did = f"{did}_{idx}"
        seen_ids.add(did)

        raw_hor = str(raw_d.get("horizon") or "1Q~2Q").strip().upper()
        horizon = raw_hor if raw_hor in VALID_HORIZONS else "1Q~2Q"

        raw_conv = str(raw_d.get("conviction") or "medium").strip().lower()
        conviction = raw_conv if raw_conv in VALID_CONVICTIONS else "medium"

        sector_keywords = [
            str(k).strip()
            for k in (raw_d.get("sector_keywords") or [])
            if str(k or "").strip()
        ][:8]

        invalidation_str = str(raw_d.get("invalidation") or "").strip()
        guard_reject, adjusted_conviction, constitutional_flags = _evaluate_card_constitutional_guardrails(
            title=title,
            direction=direction,
            conviction=conviction,
            asset_scope=asset_scope,
            thesis=thesis,
            value_chain_focus=value_chain_focus,
            invalidation=invalidation_str,
            sector_keywords=sector_keywords,
            reference_anchors=reference_anchors,
            leaders=leaders,
            constitutional_diagnostics=constitutional_diagnostics,
        )
        if guard_reject:
            rejected_reasons.append(guard_reject)
            continue
        if conviction == "high" and adjusted_conviction != "high":
            capped_card_ids.add(did)
        conviction = adjusted_conviction

        # Post-hoc deterministic mapping to existing universes.json (AI never saw existing_universes!)
        rec_univs: list[str] = []
        for u in raw_d.get("recommended_universes") or []:
            us = str(u or "").strip()
            if us and us in effective_valid_univs and us not in rec_univs:
                rec_univs.append(us)
        if not rec_univs and universes_map:
            anchor_syms = [*reference_anchors, *(leader["symbol"] for leader in leaders)]
            rec_univs = map_sector_to_universes(
                title=title,
                asset_scope=asset_scope,
                keywords=[*sector_keywords, long_leg_sector, short_leg_sector],
                reference_anchors=anchor_syms,
                universes=universes_map,
            )

        is_new_theme = bool(raw_d.get("is_new_theme")) or len(rec_univs) == 0
        raw_slug = str(raw_d.get("suggested_pool_name") or "").strip()
        suggested_pool_name = (
            _make_non_colliding_slug(raw_slug or f"theme_{did}", effective_valid_univs)
            if is_new_theme
            else ""
        )
        suggested_pool_description = str(raw_d.get("suggested_pool_description") or "").strip()
        if is_new_theme and not suggested_pool_description:
            suggested_pool_description = f"{title}（{primary_driver['dimension']} + {secondary_driver['dimension']}）"

        portfolio_overlap = _detect_portfolio_overlap(
            title=title,
            keywords=sector_keywords,
            matched_universes=rec_univs,
            universes=universes_map,
            open_positions=positions_list,
        )

        rank_val = raw_d.get("rank")
        rank = int(rank_val) if isinstance(rank_val, (int, float)) and rank_val >= 1 else idx

        directions.append(
            {
                "id": did,
                "rank": rank,
                "title": title,
                "direction": direction,
                "horizon": horizon,
                "conviction": conviction,
                "asset_scope": asset_scope,
                "primary_driver": primary_driver,
                "secondary_driver": secondary_driver,
                "thesis": thesis,
                "card_headline_gap": str(raw_d.get("card_headline_gap") or "").strip()[:60],
                "priced_in_already": str(raw_d.get("priced_in_already") or "").strip(),
                "unpriced_gap": str(raw_d.get("unpriced_gap") or "").strip(),
                "value_chain_focus": value_chain_focus,
                "long_leg_sector": long_leg_sector,
                "short_leg_sector": short_leg_sector,
                "sector_keywords": sector_keywords,
                "reference_anchors": reference_anchors,
                "key_catalyst": str(raw_d.get("key_catalyst") or "").strip(),
                "invalidation": invalidation_str,
                "invalidation_short": str(raw_d.get("invalidation_short") or "").strip()[:48],
                "recommended_universes": rec_univs,
                "is_new_theme": is_new_theme,
                "suggested_pool_name": suggested_pool_name,
                "suggested_pool_description": suggested_pool_description,
                "portfolio_overlap": portfolio_overlap,
                "constitutional_flags": constitutional_flags,
                "leaders": leaders,
                "evidence": evidence,
            }
        )

    if not directions:
        detail = "；".join(rejected_reasons[:4]) if rejected_reasons else "无合法板块方向卡片"
        raise ValueError(f"战略推演输出未通过纪律校验：{detail}")

    directions.sort(key=lambda d: (d["rank"], d["id"]))
    directions = directions[:MAX_COMPASS_DIRECTIONS]
    for i, d in enumerate(directions, start=1):
        d["rank"] = i

    factor_concentration = analyze_macro_factor_concentration(directions)

    raw_warn_input = payload.get("risk_warnings")
    warn_iter = raw_warn_input if isinstance(raw_warn_input, list) else []
    risk_warnings = [
        str(x).strip()
        for x in warn_iter
        if isinstance(x, str) and x.strip()
    ][:6]
    for fw in factor_concentration.get("warnings") or []:
        if fw not in risk_warnings and len(risk_warnings) < 8:
            risk_warnings.append(fw)

    flagged_cards_count = sum(1 for d in directions if d.get("constitutional_flags"))
    conviction_caps_applied = sum(1 for d in directions if d["id"] in capped_card_ids)

    return sanitize(
        {
            "regime_title": regime_title,
            "regime_summary": regime_summary,
            "macro_signals": macro_signals,
            "avoid_sectors": avoid_sectors[:6],
            "directions": directions,
            "abstained": False,
            "abstain_reason": "",
            "rejected_cards_count": len(rejected_reasons),
            "risk_warnings": risk_warnings,
            "factor_concentration": factor_concentration,
            "constitutional_audit": {
                "enforced": True,
                "diagnostics_attached": isinstance(constitutional_diagnostics, dict),
                "rejected_count": len(rejected_reasons),
                "rejected_reasons": rejected_reasons[:8],
                "flagged_cards_count": flagged_cards_count,
                "conviction_caps_applied": conviction_caps_applied,
            },
        }
    )


# ---------------------------------------------------------------------------
# Persistence Helpers (SQLite primary + in-memory fallback for isolated tests)
# ---------------------------------------------------------------------------
def persist_compass_run(
    *,
    run_id: str | None,
    task_id: str | None,
    created_at_utc: str,
    finished_at_utc: str,
    engine: str,
    effort: str,
    focus_prompt: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    rid = run_id or task_id or f"cmp_{int(time.time())}"
    record = sanitize(
        {
            "run_id": rid,
            "task_id": task_id or rid,
            "created_at_utc": created_at_utc,
            "finished_at_utc": finished_at_utc,
            "status": "completed",
            "engine": engine,
            "effort": effort,
            "focus_prompt": focus_prompt,
            "regime_title": str(result.get("regime_title") or ""),
            "directions_count": len(result.get("directions") or []),
            "result": result,
        }
    )
    if data_store.is_store_enabled():
        data_store.get_store().record_compass_run(
            run_id=rid,
            task_id=task_id,
            created_at_utc=created_at_utc,
            finished_at_utc=finished_at_utc,
            status="completed",
            engine=engine,
            effort=effort,
            focus_prompt=focus_prompt,
            result=result,
        )
    else:
        with _FALLBACK_LOCK:
            _FALLBACK_RUNS[:] = [r for r in _FALLBACK_RUNS if r["run_id"] != rid]
            _FALLBACK_RUNS.insert(0, record)
            del _FALLBACK_RUNS[_MAX_FALLBACK_RUNS:]
    return record


def get_latest_compass_run() -> dict[str, Any] | None:
    if data_store.is_store_enabled():
        return data_store.get_store().get_latest_compass_run()
    with _FALLBACK_LOCK:
        return _FALLBACK_RUNS[0] if _FALLBACK_RUNS else None


def list_compass_runs(limit: int = 20) -> list[dict[str, Any]]:
    if data_store.is_store_enabled():
        return data_store.get_store().list_compass_runs(limit=limit)
    with _FALLBACK_LOCK:
        return [
            {k: v for k, v in r.items() if k != "result"}
            for r in _FALLBACK_RUNS[: max(1, min(limit, 60))]
        ]


def get_compass_run(run_id: str) -> dict[str, Any] | None:
    if data_store.is_store_enabled():
        return data_store.get_store().get_compass_run(run_id)
    with _FALLBACK_LOCK:
        for r in _FALLBACK_RUNS:
            if r["run_id"] == run_id:
                return r
    return None


def clear_fallback_runs() -> None:
    """Test helper to reset in-memory compass runs when SQLite store is disabled."""
    with _FALLBACK_LOCK:
        _FALLBACK_RUNS.clear()


# ---------------------------------------------------------------------------
# Task Runner Entry Point (4-Stage SSE Pipeline)
# ---------------------------------------------------------------------------
def run_macro_compass(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
    task_id: str | None = None,
    commit_result: CommitResultFn | None = None,
) -> dict[str, Any]:
    """Execute Layer 1 Top-Down Strategic Compass synthesis inside TaskManager."""
    engine = str(params.get("engine") or "claude").strip().lower()
    if engine not in deep_research.VALID_ENGINES:
        raise ValueError(f"Unsupported engine: {engine!r}")
    effort = str(params.get("effort") or "medium").strip().lower()
    if effort not in ("low", "medium", "high"):
        effort = "medium"
    focus_prompt = str(params.get("focus_prompt") or "").strip()
    focus_scopes = normalize_focus_scopes(params.get("focus_scopes") or [])
    focus_summary = format_focus_scopes_summary(focus_scopes, focus_prompt)
    refresh_macro = bool(params.get("refresh_macro", True))

    created_at = _utc_iso()
    total_steps = 4

    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise deep_research.DeepResearchCancelledError("用户已终止 Layer 1 战略推演任务")

    _check_cancel()
    emit("progress", {"done": 0, "total": total_steps})
    emit("step", {"stage": "collect_macro", "message": "采集实时市场定价锚与宏观双核背景数据…"})

    macro_overview = collect_macro_overview(refresh=refresh_macro)
    _check_cancel()
    emit("progress", {"done": 1, "total": total_steps})

    emit("step", {"stage": "build_brief", "message": "封装无锚定推演简报与多选战场范围…"})
    brief = build_compass_brief(
        macro_overview=macro_overview,
        focus_prompt=focus_prompt,
        focus_scopes=focus_scopes,
    )
    _check_cancel()
    emit("progress", {"done": 2, "total": total_steps})

    emit(
        "step",
        {
            "stage": "agent_synthesize",
            "message": f"启动 {engine.upper()} 联网检索，聚焦{focus_summary or '全市场 7 维框架'}推演 1Q~1Y 板块…",
        },
    )

    with tempfile.TemporaryDirectory(prefix="t3_layer1_compass_") as tmpdir:
        brief_path = os.path.join(tmpdir, "compass_brief.json")
        with open(brief_path, "w", encoding="utf-8") as fh:
            json.dump(brief, fh, ensure_ascii=False, indent=2)

        prompt = build_compass_prompt(
            brief_path=brief_path,
            focus_prompt=focus_prompt,
            focus_scopes=focus_scopes,
        )
        raw_text, run_meta = deep_research._run_agent_cli(
            engine=engine,
            prompt=prompt,
            cwd=tmpdir,
            effort=effort,
            emit=emit,
            stage="agent_synthesize",
            timeout_seconds=COMPASS_STAGE_TIMEOUT_SECONDS,
            cancel_event=cancel_event,
        )

    _check_cancel()
    emit("progress", {"done": 3, "total": total_steps})
    emit("step", {"stage": "validate_and_persist", "message": "校验板块逻辑、自动映射本地池与持仓重叠并落盘…"})

    universes = scan_opportunities.load_universes()
    open_positions = _load_open_positions_for_post_check()
    raw_payload = deep_research.extract_json_payload(raw_text)
    normalized = normalize_compass_output(
        raw_payload,
        valid_universes=set(universes.keys()),
        universes=universes,
        open_positions=open_positions,
        focus_scopes=focus_scopes,
        constitutional_diagnostics=brief.get("constitutional_diagnostics"),
    )

    finished_at = _utc_iso()
    full_result = sanitize(
        {
            **normalized,
            "generated_at_utc": finished_at,
            "engine": engine,
            "effort": effort,
            "focus_scopes": focus_scopes,
            "focus_prompt": focus_summary,
            "raw_focus_prompt": focus_prompt,
            "macro_snapshot_summary": {
                "fetched_at_utc": macro_overview.get("fetched_at_utc"),
                "regime_tags": macro_overview.get("regime_tags") or [],
                "headline_metrics": macro_overview.get("headline_metrics") or [],
                "anchors_items": (macro_overview.get("anchors") or {}).get("items") or [],
            },
            "run_meta": run_meta,
        }
    )

    def _do_persist() -> dict[str, Any]:
        return persist_compass_run(
            run_id=task_id,
            task_id=task_id,
            created_at_utc=created_at,
            finished_at_utc=finished_at,
            engine=engine,
            effort=effort,
            focus_prompt=focus_summary,
            result=full_result,
        )

    if commit_result is not None:
        saved = commit_result(_do_persist)
    else:
        _check_cancel()
        saved = _do_persist()

    emit("progress", {"done": total_steps, "total": total_steps})
    done_msg = (
        "Layer 1 战略推演已完成：触发宪法第 4 条战略留白观望并落盘"
        if full_result.get("abstained")
        else f"Layer 1 战略推演已完成：锁定 {len(full_result['directions'])} 个高共振方向并落盘"
    )
    emit(
        "log",
        {
            "stage": "validate_and_persist",
            "message": done_msg,
        },
    )
    return saved
