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
            "as_of": None,
        }
    n_bars = len(close_series)
    last = _to_float(close_series.iloc[-1])
    # Strict lookback windows: never substitute shorter history into a longer return window
    p5 = _to_float(close_series.iloc[-6]) if n_bars >= 6 else None
    p20 = _to_float(close_series.iloc[-21]) if n_bars >= 21 else None
    p60 = _to_float(close_series.iloc[-61]) if n_bars >= 61 else None
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
                item: dict[str, Any] = {
                    **meta,
                    "status": "stale" if is_stale else "ok",
                    "last": round(last, 4),
                    "ret_5d_pct": _to_float(tech.get("return_5d_pct")),
                    "ret_20d_pct": _to_float(tech.get("return_20d_pct")),
                    "ret_60d_pct": _to_float(tech.get("return_60d_pct")),
                    "as_of": as_of,
                }
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


def _fetch_macro_overview_uncached(deadline_seconds: float, *, refresh: bool = False) -> dict[str, Any]:
    """Execute one bounded parallel macro overview fetch without blocking on timed-out threads."""
    now_iso = _utc_iso()
    http_read_timeout = max(0.5, min(12.0, deadline_seconds))

    def _wrapped(fn: Callable[[], Any]) -> Any:
        with data_store.scoped_http_timeout((min(4.0, http_read_timeout), http_read_timeout)):
            return fn()

    cn_timeout = max(0.2, min(9.0, deadline_seconds * 0.9))
    pm_timeout = max(0.2, min(6.0, deadline_seconds * 0.85))
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
        )
        if info is not None
    }

    headline_metrics, regime_tags = _build_headline_metrics_and_tags(live_ctx, fred_macro, china_macro)
    core_fresh = all(sources_status.get(k) in ("ok", "partial") for k in CORE_MACRO_SOURCES)
    pm_fresh = sources_status.get("polymarket_odds") == "ok"
    # Core 5 sources keep full 600s TTL even if optional Polymarket Gamma API is degraded;
    # Polymarket maintains its own independent retry cache slot below.
    ttl = TTL_COMPASS_MACRO_SECONDS if core_fresh else TTL_COMPASS_MACRO_DEGRADED_SECONDS
    pm_ttl = TTL_COMPASS_MACRO_SECONDS if pm_fresh else TTL_COMPASS_POLYMARKET_DEGRADED_SECONDS

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
        }
    )
    CACHE.set(("compass_macro_overview",), bundle, ttl)
    CACHE.set(("compass_polymarket_odds",), (polymarket_odds, st_pm, stale_pm), pm_ttl)
    return bundle


def _overlay_polymarket_on_overview(
    base: dict[str, Any],
    pm_tuple: tuple[dict[str, Any], str, dict[str, Any] | None],
) -> dict[str, Any]:
    """Return a fresh, atomically consistent overview dict combining immutable core snapshot + Polymarket tuple."""
    pm_odds, st_pm, stale_pm = pm_tuple
    new_sources_status = {**(base.get("sources_status") or {}), "polymarket_odds": st_pm}
    new_stale = dict(base.get("stale_sources") or {})
    if stale_pm is not None:
        new_stale["polymarket_odds"] = stale_pm
    else:
        new_stale.pop("polymarket_odds", None)
    return sanitize(
        {
            **base,
            "cache_hit": True,
            "sources_status": new_sources_status,
            "stale_sources": new_stale,
            "polymarket_odds": pm_odds,
        }
    )


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
        # Double-check inside the lock so a caller paused between the outer cache miss
        # and acquiring the lock reuses the snapshot populated by an earlier winner.
        hit2, existing_pm2 = CACHE.get(pm_cache_key)
        if hit2 and isinstance(existing_pm2, tuple) and len(existing_pm2) == 3:
            return existing_pm2
        if _PM_RETRY_INFLIGHT_FUTURE is not None and not _PM_RETRY_INFLIGHT_FUTURE.done():
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
        CACHE.set(pm_cache_key, pm_tuple, pm_ttl)
        shared_future.set_result(pm_tuple)
        return pm_tuple
    except BaseException as exc:
        if not shared_future.done():
            shared_future.set_exception(exc)
        raise
    finally:
        with _PM_RETRY_INFLIGHT_LOCK:
            if _PM_RETRY_INFLIGHT_FUTURE is shared_future:
                _PM_RETRY_INFLIGHT_FUTURE = None


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
            if pm_hit and isinstance(pm_tuple, tuple) and len(pm_tuple) == 3:
                return _overlay_polymarket_on_overview(cached, pm_tuple)
            if (cached.get("sources_status") or {}).get("polymarket_odds") != "ok":
                fresh_pm_tuple = _refresh_polymarket_singleflight(
                    timeout_seconds=min(6.0, deadline_seconds * 0.85),
                )
                return _overlay_polymarket_on_overview(cached, fresh_pm_tuple)
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
            "polymarket_odds": pm_compact,
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
            "用户本次明确指定只深挖以下战场，请将全部 4~6 张方向卡片集中分配在这些战场内（确保每个勾选战场至少产出 1 张高质量方向卡，对比战场内部强弱分化与多空/配对机会，不要把名额浪费在未勾选市场）：\n"
            + "\n".join(lines)
            + "\n"
        )
    focus_block = f"\n【用户补充说明】\n{focus_prompt.strip()}\n" if focus_prompt.strip() else ""
    return (
        f"你是 T3 Trading Desk（中低频多智能体投研台 · 缠论版）的第一层（Layer 1）全球自上而下首席策略师。今天是 {today}。\n"
        f"请先使用 Read 工具读取当前目录下的简报文件 `{brief_path}`，再以**空白大脑（无任何预设标的池锚定）**执行实时 WebSearch / WebFetch，\n"
        "从当下真实世界正在发生的最大级别宏观、地缘、政策、供需与产业变化出发，只回答第一层最核心的战略问题：\n"
        "**“在当前环境下，未来 1 个季度至 1 年（1Q~1Y）最有逻辑的多空方向和核心板块（Sector / Theme）是什么？产业链该切哪个环节？又该回避哪些逆风板块？”**\n\n"
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
        "5. **前瞻因子分散 + 跨资产分层权限 + 产业链传导（拒绝伪平衡，也拒绝 6 张卡全赌单一因子！）**：\n"
        "   - **多空服从未来预期差，同时避免单一宏观因子过度拥挤**：绝不要为了显得“攻守平衡”硬凑弱逻辑多头，更**严禁把 20D/60D 正在深跌（如跌幅超 -10%）的弱势资产硬塞进 `pair` 的多头腿去对冲空头利润**！但同样严禁把全部 4~6 张卡都押在同一个宏观因子（例如全部都在赌“美联储持续鹰派紧缩”）上——主趋势中继方向只保留赔率最锋利、增量预期差最明确的代表，其余名额优先分配给**低相关独立供给/政策周期**，或正在酝酿的**下一季度宏观二阶传导/拐点方向**（例如油价过剩压低通胀后的政策边际变化）。\n"
        "   - **跨资产分层权限（美股双杀可空 · A股严禁做空 · 商品/外汇/加密全对称）**：\n"
        "     - **美股 / 美股行业 ETF**：景气上行期做多；而在**高实际利率压制估值 + 未来 1~3 年盈利/CapEx 增长故事证伪（戴维斯双杀）**的熊市主跌段，**允许直接输出 `short` 做空方向卡（或作为 `pair` 空头腿）**！\n"
        "     - **A 股**：受限于融券制度，**严格禁止做空（含配对空头腿）**，仅允许 `long`。\n"
        "     - **大宗商品（海外 + 国内期货）、外汇（FX）、加密货币**：多空完全对称，`short` 做空与 `long` 同等重要！商品即使短期低库存呈大 B 结构，强现实弱预期劈叉照样可以做空。\n"
        "   - **强制追踪跨资产产业链传导**：每当某个超大权重权益主线进入戴维斯双杀或宏观收缩（例如算力硬件/半导体 CapEx 放缓、或中国地产/信用收缩），必须顺藤摸瓜同时审视其**上游工业/电子金属（如电子焊料金属、数据中心用铜、黑色原料）与商品货币**的联动空头机会——最强的主跌空头直接升格进 `directions`！\n"
        "   - **每一条 `avoid_sectors` 逆风点都必须延伸出可落地策略（`derived_strategy`）**：说明能否直接通过美股/ETF、商品期货、外汇或加密空头表达（A 股严禁直接做空，但可延伸至上游商品/外汇代理空头或反向受益轮动），并如实填写 `short_proxy_possible` 与 `short_proxy_hint`。\n"
        "6. **0-编造红线**：引用宏观数据必须与 `brief.json` 或最新联网来源一致（且标明日期）；每个板块必须给出 **≥2 条**真实联网证据，含精确 `YYYY-MM-DD` 日期、来源机构名、`L1~L4` 证据等级与可验证的 `https://` 链接。\n"
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
        '      "priced_in_already": "1句客观交代当下市场已经计价了哪些共识/涨跌幅（防止后视镜线性外推）",\n'
        '      "unpriced_gap": "1句明确指出未来1Q~1Y尚未被充分定价的增量预期差、二阶传导或催化",\n'
        '      "value_chain_focus": "1~2句明确产业链最该切入的环节及 Layer 2 选真龙头的硬标准（如上游资源壁垒/成本曲线左端/估值泡沫最脆弱或订单兑现纯度；若该方向20D/60D已暴跌超25%，必须明确提醒 Layer 2 等待反抽二卖/三卖或30min结构卖点，严禁远离中枢盲目追空）",\n'
        '      "long_leg_sector": "仅 pair 配对必填：做多哪一细分板块/品种环节（必须自身具备独立走强逻辑，严禁塞入深跌资产）",\n'
        '      "short_leg_sector": "仅 pair 配对必填（A股严禁做空）：做空哪一过剩/双杀/弱势环节",\n'
        '      "sector_keywords": ["板块中英文核心关键词1", "关键词2", "关键词3"],\n'
        '      "reference_anchors": ["可选0~2个板块基准定价锚代码，用于辅助定位，可空数组"],\n'
        '      "key_catalyst": "未来1~3个月具体催化事件与时间节点",\n'
        '      "invalidation": "明确可证伪红线（什么数据/政策/事件出现即放弃该板块）",\n'
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
        '  "risk_warnings": ["1~3条当前宏观与跨资产层面最需警惕的尾部风险或因子重叠提醒"]\n'
        "}\n"
        "```\n"
        "要求：`directions` 输出 **4~6 个**按优先级排序（`rank` 1..N）的高共振板块方向，必须以**未来 1Q~1Y 尚未充分定价的增量预期差**为核心（兼顾强趋势中继与低相关独立周期/下一季度潜在二阶传导，避免 6 张卡全押同一宏观因子），每个板块至少 2 条带 `YYYY-MM-DD` 日期与来源的真实证据，严格遵守 A 股禁空铁律（美股双杀、商品、外汇、加密均允许做空）。"
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


def normalize_compass_output(
    payload: dict[str, Any],
    *,
    valid_universes: set[str],
    universes: dict[str, Any] | None = None,
    open_positions: list[dict[str, str]] | None = None,
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

    avoid_sectors: list[dict[str, Any]] = []
    for item in payload.get("avoid_sectors") or []:
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

    raw_dirs = payload.get("directions")
    if not isinstance(raw_dirs, list) or len(raw_dirs) == 0:
        raise ValueError("战略推演未输出任何有效板块方向卡片（directions 为空）")

    universes_map = universes if isinstance(universes, dict) else {k: {"symbols": [], "description": ""} for k in valid_universes}
    effective_valid_univs = set(valid_universes) | set(universes_map.keys())
    positions_list = open_positions if open_positions is not None else []

    directions: list[dict[str, Any]] = []
    rejected_reasons: list[str] = []
    seen_ids: set[str] = set()

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
                "priced_in_already": str(raw_d.get("priced_in_already") or "").strip(),
                "unpriced_gap": str(raw_d.get("unpriced_gap") or "").strip(),
                "value_chain_focus": value_chain_focus,
                "long_leg_sector": long_leg_sector,
                "short_leg_sector": short_leg_sector,
                "sector_keywords": sector_keywords,
                "reference_anchors": reference_anchors,
                "key_catalyst": str(raw_d.get("key_catalyst") or "").strip(),
                "invalidation": str(raw_d.get("invalidation") or "").strip(),
                "recommended_universes": rec_univs,
                "is_new_theme": is_new_theme,
                "suggested_pool_name": suggested_pool_name,
                "suggested_pool_description": suggested_pool_description,
                "portfolio_overlap": portfolio_overlap,
                "leaders": leaders,
                "evidence": evidence,
            }
        )

    if not directions:
        detail = "；".join(rejected_reasons[:4]) if rejected_reasons else "无合法板块方向卡片"
        raise ValueError(f"战略推演输出未通过纪律校验：{detail}")

    directions.sort(key=lambda d: (d["rank"], d["id"]))
    for i, d in enumerate(directions, start=1):
        d["rank"] = i

    risk_warnings = [
        str(x).strip()
        for x in (payload.get("risk_warnings") or [])
        if str(x or "").strip()
    ][:6]

    return sanitize(
        {
            "regime_title": regime_title,
            "regime_summary": regime_summary,
            "macro_signals": macro_signals,
            "avoid_sectors": avoid_sectors[:6],
            "directions": directions,
            "rejected_cards_count": len(rejected_reasons),
            "risk_warnings": risk_warnings,
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
    emit(
        "log",
        {
            "stage": "validate_and_persist",
            "message": f"Layer 1 战略推演已完成：锁定 {len(full_result['directions'])} 个高共振方向并落盘",
        },
    )
    return saved
