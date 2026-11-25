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
TTL_COMPASS_MACRO_DEGRADED_SECONDS = 30.0
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
    # Commodities
    {"symbol": "GC=F", "name": "黄金", "group": "commodities", "group_label": "大宗商品锚"},
    {"symbol": "HG=F", "name": "COMEX铜", "group": "commodities", "group_label": "大宗商品锚"},
    {"symbol": "CL=F", "name": "WTI原油", "group": "commodities", "group_label": "大宗商品锚"},
    {"symbol": "ALI=F", "name": "COMEX铝", "group": "commodities", "group_label": "大宗商品锚"},
    # A-shares, FX & Crypto
    {"symbol": "510300.SS", "name": "沪深300", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "510880.SS", "name": "A股红利", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "USDJPY=X", "name": "美元/日元", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "USDCNY=X", "name": "美元/人民币", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
    {"symbol": "BTC-USD", "name": "比特币", "group": "cn_fx_crypto", "group_label": "A股·汇市·加密"},
)

_POOL_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Bounded non-blocking executor & single-flight state for free macro overview fetches
_MACRO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="compass-macro")
_MACRO_INFLIGHT_LOCK = threading.Lock()
_MACRO_INFLIGHT_FUTURE: concurrent.futures.Future[dict[str, Any]] | None = None
_LAST_GOOD_LOCK = threading.Lock()
_LAST_GOOD_MACRO_SOURCES: dict[str, dict[str, Any]] = {}

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


# ---------------------------------------------------------------------------
# Free Hard-Data Foundation (US-China Macro Dual-Core + Cross-Asset Anchors)
# ---------------------------------------------------------------------------
def fetch_cross_asset_anchors() -> dict[str, Any]:
    """Batch-fetch strict 5D / 20D / 60D relative returns for 17 global pricing anchors via yfinance."""
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

        if close_series is None or len(close_series) < 2:
            items.append(
                {
                    **meta,
                    "status": "unavailable",
                    "last": None,
                    "ret_5d_pct": None,
                    "ret_20d_pct": None,
                    "ret_60d_pct": None,
                    "as_of": None,
                }
            )
            continue

        n_bars = len(close_series)
        last = _to_float(close_series.iloc[-1])
        # Strict lookback windows: never substitute shorter history into a longer return window
        p5 = _to_float(close_series.iloc[-6]) if n_bars >= 6 else None
        p20 = _to_float(close_series.iloc[-21]) if n_bars >= 21 else None
        p60 = _to_float(close_series.iloc[-61]) if n_bars >= 61 else None
        as_of_raw = close_series.index[-1]
        as_of = as_of_raw.strftime("%Y-%m-%d") if hasattr(as_of_raw, "strftime") else str(as_of_raw)[:10]

        items.append(
            {
                **meta,
                "status": "ok",
                "last": round(last, 4) if last is not None else None,
                "ret_5d_pct": _safe_pct_change(last, p5),
                "ret_20d_pct": _safe_pct_change(last, p20),
                "ret_60d_pct": _safe_pct_change(last, p60),
                "as_of": as_of,
            }
        )
        ok_count += 1

    status = "ok" if ok_count == len(CROSS_ASSET_ANCHORS) else ("partial" if ok_count > 0 else "error")
    return {"status": status, "ok_count": ok_count, "total": len(CROSS_ASSET_ANCHORS), "items": items}


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


def _fetch_macro_overview_uncached(deadline_seconds: float) -> dict[str, Any]:
    """Execute one bounded parallel macro overview fetch without blocking on timed-out threads."""
    now_iso = _utc_iso()
    http_read_timeout = max(0.5, min(12.0, deadline_seconds))

    def _wrapped(fn: Callable[[], Any]) -> Any:
        with data_store.scoped_http_timeout((min(4.0, http_read_timeout), http_read_timeout)):
            return fn()

    fut_map: dict[concurrent.futures.Future[Any], str] = {
        _MACRO_EXECUTOR.submit(_wrapped, market_snapshot.fetch_macro_context): "live_macro",
        _MACRO_EXECUTOR.submit(_wrapped, research_data.fetch_fred_macro): "us_fred",
        _MACRO_EXECUTOR.submit(_wrapped, research_data.fetch_china_macro_core): "cn_macro",
        _MACRO_EXECUTOR.submit(_wrapped, fetch_cross_asset_anchors): "cross_asset_anchors",
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
            with _LAST_GOOD_LOCK:
                _LAST_GOOD_MACRO_SOURCES[name] = {"data": val, "fetched_at_utc": now_iso}
            st = str(val.get("status") or "ok") if isinstance(val, dict) and "status" in val else "ok"
            return val, st, None

        err_msg = errors.get(name) or (str(val.get("error")) if isinstance(val, dict) and val.get("error") else "fetch failed")
        with _LAST_GOOD_LOCK:
            prior = _LAST_GOOD_MACRO_SOURCES.get(name)
        if prior is not None:
            return prior["data"], "stale", {"last_good_at_utc": prior["fetched_at_utc"], "error": err_msg}
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
    anchors, st_anc, stale_anc = _resolve_source(
        "cross_asset_anchors",
        {"status": "error", "items": [], "error": errors.get("cross_asset_anchors", "unavailable")},
        lambda v: isinstance(v, dict) and str(v.get("status") or "") in ("ok", "partial"),
    )

    sources_status = {
        "live_macro": st_live,
        "us_fred": st_fred,
        "cn_macro": st_cn,
        "cross_asset_anchors": st_anc,
    }
    stale_sources = {
        k: info
        for k, info in (
            ("live_macro", stale_live),
            ("us_fred", stale_fred),
            ("cn_macro", stale_cn),
            ("cross_asset_anchors", stale_anc),
        )
        if info is not None
    }

    headline_metrics, regime_tags = _build_headline_metrics_and_tags(live_ctx, fred_macro, china_macro)
    all_fresh = all(v in ("ok", "partial") for v in sources_status.values())
    ttl = TTL_COMPASS_MACRO_SECONDS if all_fresh else TTL_COMPASS_MACRO_DEGRADED_SECONDS

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
        }
    )
    CACHE.set(("compass_macro_overview",), bundle, ttl)
    return bundle


def collect_macro_overview(
    *,
    refresh: bool = False,
    deadline_seconds: float = DEFAULT_MACRO_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Collect and cache free US-China macro dual-core + 17 cross-asset anchors with single-flight coalescing."""
    global _MACRO_INFLIGHT_FUTURE
    cache_key = ("compass_macro_overview",)
    if not refresh:
        hit, cached = CACHE.get(cache_key)
        if hit and isinstance(cached, dict):
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
        bundle = _fetch_macro_overview_uncached(deadline_seconds=deadline_seconds)
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
# Layer 1 AI 7-Dimension Strategic Synthesis
# ---------------------------------------------------------------------------
def _load_portfolio_and_archive_context() -> dict[str, Any]:
    """Read compact current holdings and watchlist themes so Layer 1 avoids blind duplication."""
    holdings: list[dict[str, Any]] = []
    try:
        summary = portfolio_manager.portfolio_summary()
        for pos in summary.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            holdings.append(
                {
                    "symbol": pos.get("symbol"),
                    "direction": pos.get("direction"),
                    "sector_theme": pos.get("sector_theme"),
                    "leader_role": pos.get("leader_role"),
                }
            )
    except Exception:
        pass

    archives: list[dict[str, Any]] = []
    try:
        for item in research_archive.list_archives()[:30]:
            if not isinstance(item, dict):
                continue
            archives.append(
                {
                    "symbol": item.get("symbol"),
                    "market": item.get("market"),
                    "status": item.get("status"),
                    "direction_bias": item.get("direction_bias"),
                    "thesis_summary": str(item.get("thesis_summary") or "")[:120],
                }
            )
    except Exception:
        pass

    return {"open_positions": holdings, "existing_archives": archives}


def build_compass_brief(
    *,
    macro_overview: dict[str, Any],
    universes: dict[str, Any],
    focus_prompt: str = "",
) -> dict[str, Any]:
    """Assemble the zero-fabrication brief JSON passed into the headless Layer 1 strategist."""
    compact_universes = {
        key: {
            "description": str((pool or {}).get("description") or ""),
            "symbols": list((pool or {}).get("symbols") or []),
        }
        for key, pool in universes.items()
        if isinstance(pool, dict)
    }
    return sanitize(
        {
            "generated_at_utc": _utc_iso(),
            "focus_prompt": focus_prompt.strip(),
            "seven_dimensions": list(SEVEN_DIMENSIONS),
            "macro_overview": {
                "fetched_at_utc": macro_overview.get("fetched_at_utc"),
                "sources_status": macro_overview.get("sources_status"),
                "stale_sources": macro_overview.get("stale_sources") or {},
                "regime_tags": macro_overview.get("regime_tags"),
                "headline_metrics": macro_overview.get("headline_metrics"),
                "china_macro": macro_overview.get("china_macro"),
                "cross_asset_anchors": (macro_overview.get("anchors") or {}).get("items") or [],
            },
            "existing_universes": compact_universes,
            "portfolio_context": _load_portfolio_and_archive_context(),
        }
    )


def build_compass_prompt(*, brief_path: str, focus_prompt: str = "") -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    focus_block = f"\n【用户指定的本次推演侧重点】\n{focus_prompt.strip()}\n" if focus_prompt.strip() else ""
    return (
        f"你是 T3 Trading Desk（中低频多智能体投研台 · 缠论版）的第一层（Layer 1）全球自上而下首席策略师。今天是 {today}。\n"
        f"请先使用 Read 工具读取当前目录下的宏观简报文件 `{brief_path}`，再使用 WebSearch / WebFetch 检索近 2~4 周全球最新的宏观政策、地缘事件、资源国动态、大宗商品供需平衡与科技产业迭代，\n"
        "严格遵循本系统的自上而下投资哲学，为用户回答：**“在当下真实宏观环境下，未来 1 个季度至 1 年（1Q~1Y）最有逻辑、最值得优先聚焦的多空方向和核心板块是什么？该重点盯哪只真龙头？又该回避哪些逆风板块？”**\n\n"
        "【不可违背的第一层交易哲学铁律】\n"
        "1. **大逻辑先行（严禁凭短线涨跌或超买超卖倒推逻辑）**：不使用 RSI/布林带/均值回归作为推荐依据，只做真正具备 1Q~1Y 基本面预期差、供需失衡或宏观流动性共振的方向。\n"
        "2. **7 维驱动共振门槛（强制 ≥2 个不同维度共振，且均须有非空摘要）**：每个推荐方向必须明确标注主驱动维度 + 不同的次驱动维度，且必须从以下 7 维规范名中选取：\n"
        "   - `宏观双核`（美国利率/美债/美元流动性 + 中国财政/地产/信用周期）\n"
        "   - `地缘与资源国政策`（地缘冲突、航运咽喉、资源国出口禁令/配额/矿业税/收储、关税壁垒）\n"
        "   - `技术与产业迭代`（AI算力/存储代际切换、电力电网瓶颈、机器人/智驾、新能源迭代）\n"
        "   - `商品供需平衡与库存周期`（未来1Q~1Y投产爬坡 vs 需求增量/坍塌、产能出清、天气气候）\n"
        "   - `产业政策与监管`（财政补贴、国内限产反内卷、生物柴油政策、法案/ETF审批）\n"
        "   - `资金流与市场结构`（ETF申赎、CFTC极端持仓、套息平仓、资金费率极值）\n"
        "   - `突发供给冲击`（矿难、罢工、港口封锁、疫病、主产国突发禁令）\n"
        "3. **跨资产多空非对称 + 大 B 结构不否决做空**：\n"
        "   - **股票（美股 / A股）严格禁止做空（包括配对中的空头腿）**，仅允许做多（`long`）或配对中的多头腿；\n"
        "   - **大宗商品（海外+国内期货）、外汇（FX）、加密货币**多空完全对称，`short` 做空与 `pair` 同板块多空配对与 `long` 同等重要！若当前存在明确的供需过剩/弱预期品种（即使当下低库存呈大 B 结构），必须敢于给出做空或配对方向，绝不只堆砌清一色做多。\n"
        "4. **单一板块龙头独占制（源头控制相关性）**：\n"
        "   - 单向方向（`long` / `short`）只给 **1 只唯一龙头或最多 2 只双寡头**（且必须全同向）；`pair` 配对必须严格包含**多头腿 1 只 + 非股票空头腿 1 只**。\n"
        "   - 标的代码必须使用本系统可执行的真实代码格式（美股如 `PWR`/`NVDA`/`COPX`，A股如 `601899.SS`/`300750.SZ`，国内期货主力连续如 `CU0`/`LC0`/`RB0`/`EC0`/`SC0`，海外期货如 `GC=F`/`HG=F`/`CL=F`，外汇如 `USDJPY=X`/`AUDUSD=X`，加密如 `BTC/USDT`）。\n"
        "   - 优先映射 `brief.json` 中 `existing_universes` 的准确池名到 `recommended_universes`；**若推演出极具价值但 `existing_universes` 尚未收录的新细分板块，将 `is_new_theme` 设为 `true`，并提供不与已有池重名的英文下划线 `suggested_pool_name` 和中文 `suggested_pool_description`**。\n"
        "5. **0-编造红线**：引用宏观数据必须与 `brief.json` 一致；每个方向必须给出 **≥2 条**真实证据，含精确 `YYYY-MM-DD` 日期、来源机构名称、`L1~L4` 证据等级与可验证的 `https://` 链接。\n"
        f"{focus_block}\n"
        "【输出格式铁律】\n"
        "你的最终回复必须**仅包含一个合法的 JSON 对象**（可用 ```json ... ``` 包裹，JSON 外不要输出任何额外说明文字），严格遵循以下 schema：\n"
        "```json\n"
        "{\n"
        '  "regime_title": "15~32字精炼概括当前全球宏观总定调",\n'
        '  "regime_summary": "2~4句清晰判断当下美国利率/美元 + 中国信用/财政双核所处象限，以及未来1Q~1Y资金主战场与整体仓位姿态",\n'
        '  "macro_signals": {\n'
        '    "us_macro": "美国利率/通胀/美元流动性现状与未来1~2季路径（1句）",\n'
        '    "china_macro": "中国财政/地产/PMI/出口脉动与对黑色化工有色的含义（1句）",\n'
        '    "geopolitics_supply": "地缘冲突/关税/资源国政策核心焦点（1句）",\n'
        '    "tech_industry": "AI算力/电力基建/核心产业迭代所处阶段（1句）"\n'
        "  },\n"
        '  "avoid_sectors": [\n'
        '    {"sector": "当下最应回避的逆风板块/方向", "reason": "1句讲清为何处于逆风或预期证伪，避免白费精力"}\n'
        "  ],\n"
        '  "directions": [\n'
        "    {\n"
        '      "id": "dir_unique_snake_case",\n'
        '      "rank": 1,\n'
        '      "title": "板块/主线名称（简洁有力，<=24字）",\n'
        '      "direction": "long | short | pair",\n'
        '      "horizon": "1Q | 1Q~2Q | 2Q~1Y | 1Q~1Y",\n'
        '      "conviction": "high | medium",\n'
        '      "asset_scope": "commodities | us_equities | a_shares | fx | crypto | cross_asset",\n'
        '      "primary_driver": {"dimension": "7维规范名之一", "summary": "主驱动逻辑核心要点（1句，必填）"},\n'
        '      "secondary_driver": {"dimension": "另1个不同的7维规范名", "summary": "次驱动共振要点（1句，必填）"},\n'
        '      "thesis": "2~3句讲透未来1Q~1Y核心预期差、供需/盈利传导链与为什么现在是最优方向",\n'
        '      "key_catalyst": "未来1~3个月具体催化事件与时间节点",\n'
        '      "invalidation": "明确可证伪红线（什么数据/政策/事件出现即放弃此方向）",\n'
        '      "recommended_universes": ["从 existing_universes 中精准选取的池名，可空数组"],\n'
        '      "is_new_theme": false,\n'
        '      "suggested_pool_name": "",\n'
        '      "suggested_pool_description": "",\n'
        '      "leaders": [\n'
        "        {\n"
        '          "symbol": "CU0",\n'
        '          "name": "沪铜",\n'
        '          "leg": "long | short",\n'
        '          "role": "sole_leader | co_leader",\n'
        '          "reason": "1句说明为何是该板块定价权最强/受益最纯的真龙头"\n'
        "        }\n"
        "      ],\n"
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
        "要求：`directions` 输出 **4~6 个**按优先级排序（`rank` 1..N）的高共振方向，务必体现全球跨资产视野（覆盖真正有逻辑的做多、商品/外汇做空或同板块多空配对），每个方向至少 2 条带 `YYYY-MM-DD` 日期与来源的真实证据，且严格遵守股票禁空与单一板块龙头独占制。"
    )


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
) -> dict[str, Any]:
    """Strictly validate and sanitize parsed AI output against CLAUDE.md Layer 1 iron rules."""
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

    avoid_sectors: list[dict[str, str]] = []
    for item in payload.get("avoid_sectors") or []:
        if isinstance(item, dict):
            sec = str(item.get("sector") or item.get("name") or "").strip()
            rsn = str(item.get("reason") or "").strip()
            if sec:
                avoid_sectors.append({"sector": sec, "reason": rsn})
        elif isinstance(item, str) and item.strip():
            avoid_sectors.append({"sector": item.strip(), "reason": ""})

    raw_dirs = payload.get("directions")
    if not isinstance(raw_dirs, list) or len(raw_dirs) == 0:
        raise ValueError("战略推演未输出任何有效方向卡片（directions 为空）")

    directions: list[dict[str, Any]] = []
    rejected_reasons: list[str] = []
    seen_ids: set[str] = set()

    for idx, raw_d in enumerate(raw_dirs[:10], start=1):
        if not isinstance(raw_d, dict):
            continue
        title = str(raw_d.get("title") or "").strip()
        thesis = str(raw_d.get("thesis") or "").strip()
        if not title or not thesis:
            rejected_reasons.append(f"#{idx}: 缺少标题或核心论点")
            continue

        raw_dir = str(raw_d.get("direction") or "").strip().lower()
        if raw_dir not in VALID_COMPASS_DIRECTIONS:
            rejected_reasons.append(f"{title}: 非法方向 {raw_dir!r}")
            continue
        direction = raw_dir

        raw_scope = str(raw_d.get("asset_scope") or "cross_asset").strip().lower()
        asset_scope = raw_scope if raw_scope in VALID_ASSET_SCOPES else "cross_asset"

        # Equities (US / A-shares) NEVER allowed as pure short direction
        if direction == "short" and asset_scope in ("us_equities", "a_shares"):
            rejected_reasons.append(f"{title}: 违反股票禁空铁律")
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

        # Normalize & validate leaders by canonical (asset_class, normalized_symbol)
        long_leaders: list[dict[str, str]] = []
        short_leaders: list[dict[str, str]] = []
        seen_canonical: set[tuple[str, str]] = set()
        for raw_l in raw_d.get("leaders") or []:
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

            # CLAUDE.md iron rule: stocks (US / A-share) strictly prohibited on short legs
            if leg_raw == "short" and ac in EQUITY_ASSET_CLASSES:
                continue

            # Unidirectional cards only accept matching-direction leaders
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
                    # Second leader in same direction is only allowed as co-leader
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

        if direction == "long":
            if not long_leaders:
                rejected_reasons.append(f"{title}: 做多方向无有效多头龙头")
                continue
            leaders = long_leaders
        elif direction == "short":
            if not short_leaders:
                rejected_reasons.append(f"{title}: 做空方向无有效非股票空头龙头")
                continue
            leaders = short_leaders
        else:  # pair
            if not long_leaders or not short_leaders:
                rejected_reasons.append(f"{title}: 配对方向缺少多头或非股票空头腿")
                continue
            leaders = [*long_leaders[:1], *short_leaders[:1]]

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

        did = _sanitize_pool_slug(str(raw_d.get("id") or f"dir_{idx}")) or f"dir_{idx}"
        while did in seen_ids:
            did = f"{did}_{idx}"
        seen_ids.add(did)

        raw_hor = str(raw_d.get("horizon") or "1Q~2Q").strip().upper()
        horizon = raw_hor if raw_hor in VALID_HORIZONS else "1Q~2Q"

        raw_conv = str(raw_d.get("conviction") or "medium").strip().lower()
        conviction = raw_conv if raw_conv in VALID_CONVICTIONS else "medium"

        # Recommended universes: strictly keep existing valid keys
        rec_univs: list[str] = []
        for u in raw_d.get("recommended_universes") or []:
            us = str(u or "").strip()
            if us and us in valid_universes and us not in rec_univs:
                rec_univs.append(us)

        is_new_theme = bool(raw_d.get("is_new_theme")) or len(rec_univs) == 0
        raw_slug = str(raw_d.get("suggested_pool_name") or "").strip()
        suggested_pool_name = (
            _make_non_colliding_slug(raw_slug or f"theme_{did}", valid_universes)
            if is_new_theme
            else ""
        )
        suggested_pool_description = str(raw_d.get("suggested_pool_description") or "").strip()
        if is_new_theme and not suggested_pool_description:
            suggested_pool_description = f"{title}（{primary_driver['dimension']} + {secondary_driver['dimension']}）"

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
                "key_catalyst": str(raw_d.get("key_catalyst") or "").strip(),
                "invalidation": str(raw_d.get("invalidation") or "").strip(),
                "recommended_universes": rec_univs,
                "is_new_theme": is_new_theme,
                "suggested_pool_name": suggested_pool_name,
                "suggested_pool_description": suggested_pool_description,
                "leaders": leaders,
                "evidence": evidence,
            }
        )

    if not directions:
        detail = "；".join(rejected_reasons[:4]) if rejected_reasons else "无合法方向卡片"
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
    refresh_macro = bool(params.get("refresh_macro", True))

    created_at = _utc_iso()
    total_steps = 4

    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise deep_research.DeepResearchCancelledError("用户已终止 Layer 1 战略推演任务")

    _check_cancel()
    emit("progress", {"done": 0, "total": total_steps})
    emit("step", {"stage": "collect_macro", "message": "采集全球中美宏观双核与 17 个跨资产定价锚…"})

    macro_overview = collect_macro_overview(refresh=refresh_macro)
    universes = scan_opportunities.load_universes()
    _check_cancel()
    emit("progress", {"done": 1, "total": total_steps})

    emit("step", {"stage": "build_brief", "message": "封装 0-编造宏观简报与现有板块池目录…"})
    brief = build_compass_brief(
        macro_overview=macro_overview,
        universes=universes,
        focus_prompt=focus_prompt,
    )
    _check_cancel()
    emit("progress", {"done": 2, "total": total_steps})

    emit(
        "step",
        {
            "stage": "agent_synthesize",
            "message": f"启动 {engine.upper()} 联网检索并按 7 维框架推演 1Q~1Y 最优多空板块…",
        },
    )

    with tempfile.TemporaryDirectory(prefix="t3_layer1_compass_") as tmpdir:
        brief_path = os.path.join(tmpdir, "compass_brief.json")
        with open(brief_path, "w", encoding="utf-8") as fh:
            json.dump(brief, fh, ensure_ascii=False, indent=2)

        prompt = build_compass_prompt(brief_path=brief_path, focus_prompt=focus_prompt)
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
    emit("step", {"stage": "validate_and_persist", "message": "校验 7 维共振结构、股票禁空与真龙头代码并落盘…"})

    raw_payload = deep_research.extract_json_payload(raw_text)
    normalized = normalize_compass_output(
        raw_payload,
        valid_universes=set(universes.keys()),
    )

    finished_at = _utc_iso()
    full_result = sanitize(
        {
            **normalized,
            "generated_at_utc": finished_at,
            "engine": engine,
            "effort": effort,
            "focus_prompt": focus_prompt,
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
            focus_prompt=focus_prompt,
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
