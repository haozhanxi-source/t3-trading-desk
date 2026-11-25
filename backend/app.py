"""FastAPI gateway for the T3 Trading Desk 4-screen workbench (thin wrapper over ``scripts/``)."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any, Literal

import data_health_check
import data_store
import market_snapshot
import portfolio_manager as pm
import research_archive as ra
import research_data
import scan_opportunities as so
import trade_planner as tp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from . import ROOT_DIR, __version__, deep_research, macro_compass
from .cache import (
    CACHE,
    TTL_MARKET_SECONDS,
    TTL_RESEARCH_SECONDS,
    TTL_SENTINEL_SECONDS,
)
from .cf_access import CloudflareAccessMiddleware
from .serializers import SafeJSONResponse, sanitize
from .symbols import search_symbols
from .tasks import (
    TERMINAL_EVENT_TYPES,
    TaskManager,
    TaskStoppingConflictError,
    compute_current_sentinel_fingerprint,
    research_cache_key,
    run_sentinel_sync,
    sentinel_cache_key,
)

CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8055",
    "http://localhost:8055",
]
SSE_HEARTBEAT_SECONDS = 15.0
TTL_FX_REFERENCE_SECONDS = 3600.0
FRONTEND_DIST = os.path.join(ROOT_DIR, "frontend", "dist")
PERIOD_PATTERN = r"^(\d+(d|wk|mo|y)|ytd|max)$"
_USD_BASE_FX_CCYS = {"CNY", "CNH", "HKD", "JPY", "CAD", "CHF", "SGD", "KRW", "TWD", "MXN", "INR"}
_CCY_BASE_FX_CCYS = {"EUR", "GBP", "AUD", "NZD"}

Direction = Literal["long", "short"]

# CLI flag names embedded in trade_planner input errors -> API field names
_PLAN_FLAG_FIELDS = {
    "--alignment-status": "alignment_status",
    "--entry": "entry_price",
    "--daily-stop": "structural_stop_daily",
    "--stop": "tactical_stop_30m",
    "--live": "live",
}


# ---------------------------------------------------------------------------
# Request models (extra="forbid": a typo'd optional risk field must fail loudly, not be ignored)
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortfolioInitReq(_Strict):
    equity: float = 1_000_000.0
    base_currency: str = "USD"
    default_risk_per_trade_pct: float = 1.0
    max_total_open_risk_pct: float = 6.0
    max_macro_factor_risk_pct: float = 3.0
    drawdown_steps: list[dict[str, float]] | None = None
    overwrite: bool = False


class OpenPositionReq(_Strict):
    symbol: str
    direction: Direction
    sector_theme: str
    entry_price: float
    tactical_stop_30m: float
    quantity: float
    structural_stop_daily: float | None = None
    target_price: float | None = None
    leader_role: str = "sole_leader"
    co_leader_reason: str = ""
    macro_factors: list[str] | None = None
    exit_red_lines: list[str] | None = None
    contract_symbol: str | None = None
    multiplier: float | None = None
    margin_rate: float | None = None
    fx_to_account: float | None = None
    asset_class: str | None = None
    notes: str = ""
    reason: str = "initial_entry"
    enforce_macro_cap: bool = True
    allow_equity_margin: bool = False
    idempotency_key: str | None = None


class TrailStopReq(_Strict):
    new_stop_30m: float
    new_structural_stop_daily: float | None = None
    current_price: float | None = None
    reason: str = "trail_stop"
    idempotency_key: str | None = None


class TrimReq(_Strict):
    trim_price: float
    trim_quantity: float
    reason: str = "partial_take_profit"
    idempotency_key: str | None = None


class ScaleInPositionRequest(_Strict):
    add_quantity: float
    add_price: float
    new_stop_loss: float | None = Field(
        None, validation_alias=AliasChoices("new_stop_loss", "new_stop_30m")
    )
    fx_rate_to_base: float | None = Field(
        None, validation_alias=AliasChoices("fx_rate_to_base", "fx_to_account")
    )
    note: str = ""
    enforce_macro_cap: bool = True
    idempotency_key: str | None = None


class CloseReq(_Strict):
    exit_price: float
    exit_reason: str
    exit_category: str = "manual"
    update_account_equity: bool = True
    idempotency_key: str | None = None


class VoidReq(_Strict):
    reason: str = "误录作废"
    idempotency_key: str | None = None


class RotateNewPositionReq(_Strict):
    symbol: str
    direction: Direction
    sector_theme: str
    entry_price: float
    tactical_stop_30m: float
    quantity: float
    structural_stop_daily: float | None = None
    target_price: float | None = None
    leader_role: str = "sole_leader"
    co_leader_reason: str = ""
    macro_factors: list[str] | None = None
    exit_red_lines: list[str] | None = None
    contract_symbol: str | None = None
    multiplier: float | None = None
    margin_rate: float | None = None
    fx_to_account: float | None = None
    asset_class: str | None = None
    notes: str = ""
    reason: str = "leader_rotation_entry"
    allow_equity_margin: bool = False


class RotateReq(_Strict):
    exit_price: float
    exit_reason: str
    new_position: RotateNewPositionReq
    exit_category: str = "leader_rotation"
    enforce_macro_cap: bool = True
    idempotency_key: str | None = None


class TradePlanReq(_Strict):
    symbol: str
    direction: Direction
    entry_price: float | None = None
    tactical_stop_30m: float | None = None
    structural_stop_daily: float | None = None
    target_price: float | None = None
    account_equity: float | None = None
    account_currency: str | None = None
    risk_budget_pct: float | None = None
    slot_risk_share: float = 1.0
    max_margin_pct: float = 100.0
    max_leverage: float | None = None
    tranche_weights: list[float] | str | None = None
    atr14_30m: float | None = None
    atr14_daily: float | None = None
    multiplier: float | None = None
    margin_rate: float | None = None
    allow_equity_margin: bool = False
    fx_to_account: float | None = None
    asset_class: str | None = None
    horizon_days: int = Field(90, validation_alias=AliasChoices("holding_horizon_days", "horizon_days"))
    curve_structure: str | None = None
    roll_yield_pct: float | None = None
    near_roll_yield_pct: float | None = None
    rate_diff_pct: float | None = None
    funding_ann_pct: float | None = None
    has_l1_l2: bool = Field(False, validation_alias=AliasChoices("has_l1_l2_evidence", "has_l1_l2"))
    pending_verifications: int = Field(0, validation_alias=AliasChoices("pending_verifications_count", "pending_verifications"))
    thesis_falsified: bool = False
    hedge_only: bool = False
    pair_trade: bool = Field(False, validation_alias=AliasChoices("is_pair_trade", "pair_trade"))
    pair_leg_verified: bool = False
    pair_leg_symbol: str | None = None
    min_rr: float = 2.5
    alignment_status: str | None = None
    live: bool = False
    research_direction_bias: Literal["long", "short", "neutral", "pair"] | None = Field(
        None,
        validation_alias=AliasChoices("research_direction", "research_direction_bias", "direction_bias"),
    )
    archive_market: str | None = Field(
        None, validation_alias=AliasChoices("archive_market", "market", "market_override")
    )


class _ArchiveMarketReq(_Strict):
    market: str | None = None
    market_override: str | None = None

    def resolved_market(self) -> str | None:
        if (
            self.market is not None
            and self.market_override is not None
            and self.market.strip().upper() != self.market_override.strip().upper()
        ):
            raise HTTPException(status_code=422, detail="Conflicting market and market_override fields")
        return self.market if self.market is not None else self.market_override


class ArchiveInitReq(_ArchiveMarketReq):
    symbol: str
    name: str = ""
    subtype: str = ""
    thesis_summary: str = ""
    primary_driver: str = ""
    secondary_driver: str = ""
    layer1_primary: str | None = None
    layer1_secondary: str | None = None
    direction_bias: Literal["long", "short", "neutral", "pair"] | None = None
    confidence: str | float | None = None
    cik: str | None = None
    overwrite: bool = False


class ArchiveEvidenceReq(_ArchiveMarketReq):
    symbol: str
    items: list[dict[str, Any]]


class ArchiveStateReq(_ArchiveMarketReq):
    symbol: str
    patch: dict[str, Any] | None = None
    expected_revision: int | None = None
    direction_bias: str | None = None
    thesis_summary: str | None = None
    layer1_primary: str | None = None
    layer1_secondary: str | None = None
    layer1_drivers: dict[str, Any] | None = None
    status: str | None = None
    confidence: str | float | None = None


class ArchiveLogReq(_ArchiveMarketReq):
    symbol: str
    entry: str


class ArchiveThesisReq(_ArchiveMarketReq):
    symbol: str = Field(..., min_length=1)
    body_md: str = Field(..., min_length=1)
    expected_revision: int = Field(..., ge=1)
    note: str = ""


class UniverseReq(_Strict):
    name: str
    description: str = ""
    symbols: list[str]
    overwrite: bool = True


class CompassScanContextReq(_Strict):
    run_id: str = ""
    direction_id: str = ""
    direction_title: str = ""
    direction: Literal["long", "short", "pair"] = "long"
    value_chain_focus: str = ""
    expected_directions: dict[str, Direction] = Field(default_factory=dict)


class ScanTaskReq(_Strict):
    universes: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    period: str = Field("3y", pattern=PERIOD_PATTERN)
    sort: Literal["tier", "none"] = Field("tier", validation_alias=AliasChoices("sort_mode", "sort"))
    refresh: bool = False
    compass_context: CompassScanContextReq | None = None


class DataHealthCheckReq(_Strict):
    universes: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    refresh: bool = False


class ResearchRefreshTaskReq(_ArchiveMarketReq):
    symbol: str
    include_macro: bool = True


class DeepResearchTaskReq(_ArchiveMarketReq):
    symbol: str = Field(..., min_length=1)
    mode: Literal["standard", "adversarial"] = "standard"
    engine: Literal["claude", "codex"] = "claude"
    focus_prompt: str = Field("", max_length=1200)
    include_macro: bool = True


class SentinelTaskReq(_Strict):
    include_watchlist: bool = False
    skip_research: bool = False
    event_window_days: int = Field(14, ge=1, le=90)


class TriggerManualVerifyReq(_ArchiveMarketReq):
    symbol: str = Field(..., min_length=1)
    trigger_id: str = Field(..., min_length=1)
    status: Literal["clear", "fired", "uncertain"]
    observed: Any = None
    observed_text: str = Field("", max_length=300)
    summary: str = Field(..., min_length=1, max_length=400)
    source_title: str = Field("", max_length=200)
    source_url: str = Field("", max_length=500)
    evidence_tier: Literal["L1", "L2", "L3", "L4"] = "L2"
    ttl_days: int = Field(7, ge=1, le=90)


class TriggerVerifyTaskReq(_ArchiveMarketReq):
    symbol: str = ""
    trigger_ids: list[str] = Field(default_factory=list)
    engine: Literal["claude", "codex", ""] = ""
    force: bool = False
    ttl_days: int = Field(7, ge=1, le=90)


class MacroCompassTaskReq(_Strict):
    engine: Literal["claude", "codex", ""] = ""
    effort: Literal["low", "medium", "high"] = "medium"
    focus_scopes: list[str] = Field(default_factory=list, max_length=12)
    focus_prompt: str = Field("", max_length=1200)
    refresh_macro: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ok(data: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> SafeJSONResponse:
    return SafeJSONResponse(data, status_code=status_code, headers=headers)


def _cache_headers(hit: bool) -> dict[str, str]:
    return {"X-Cache": "HIT" if hit else "MISS"}


def _get_fx_usd_leg(currency: str, *, refresh: bool = False) -> dict[str, Any]:
    """Return USD value per 1 unit of ``currency`` from cached daily FX bars."""
    ccy = currency.strip().upper()
    if ccy == "USD":
        return {
            "currency": "USD",
            "available": True,
            "usd_per_unit": 1.0,
            "units_per_usd": 1.0,
            "as_of_date": None,
            "pair_symbol": "USD",
            "source": "identity",
        }

    key = ("fx_usd_leg", ccy)
    if not refresh:
        hit, cached = CACHE.get(key)
        if hit and isinstance(cached, dict):
            return cached

    if ccy in _USD_BASE_FX_CCYS:
        pair_symbol = f"USD{ccy}=X"
        invert = True
    elif ccy in _CCY_BASE_FX_CCYS:
        pair_symbol = f"{ccy}USD=X"
        invert = False
    else:
        return {"currency": ccy, "available": False, "error": f"Unsupported FX reference currency: {ccy}"}

    snap_kw: dict[str, Any] = {
        "period": "1mo",
        "include_macro": False,
        "include_fundamentals": False,
        "full_chanlun": False,
        "include_intraday": False,
    }
    if refresh and "refresh" in inspect.signature(market_snapshot.get_snapshot).parameters:
        snap_kw["refresh"] = True

    snap = market_snapshot.get_snapshot(pair_symbol, **snap_kw)
    tech = snap.get("technicals") if isinstance(snap, dict) else None
    raw_close = tech.get("last_close") if isinstance(tech, dict) else None
    try:
        spot = float(raw_close) if raw_close is not None else float("nan")
    except (TypeError, ValueError):
        spot = float("nan")

    if not math.isfinite(spot) or spot <= 0:
        err_msg = snap.get("error") if isinstance(snap, dict) else None
        out = {
            "currency": ccy,
            "available": False,
            "pair_symbol": pair_symbol,
            "error": str(err_msg or f"Unavailable spot quote for {pair_symbol}"),
        }
        CACHE.set(key, out, 15.0)
        return out

    usd_per_unit = (1.0 / spot) if invert else spot
    units_per_usd = spot if invert else (1.0 / spot)
    as_of = str(tech.get("as_of_bar") or "").strip()[:10] or None
    source_raw = str(snap.get("data_source") or "market_snapshot")
    out = {
        "currency": ccy,
        "available": True,
        "usd_per_unit": usd_per_unit,
        "units_per_usd": units_per_usd,
        "as_of_date": as_of,
        "pair_symbol": pair_symbol,
        "source": f"{source_raw} ({pair_symbol})",
    }
    CACHE.set(key, out, TTL_FX_REFERENCE_SECONDS)
    return out


def _build_fx_reference(quote_currency: str, account_currency: str, *, refresh: bool = False) -> dict[str, Any]:
    q = (quote_currency or "CNY").strip().upper()
    a = (account_currency or "USD").strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cny_leg = _get_fx_usd_leg("CNY", refresh=refresh)
    usd_cny = round(float(cny_leg["units_per_usd"]), 4) if cny_leg.get("available") else None
    cny_usd = round(float(cny_leg["usd_per_unit"]), 6) if cny_leg.get("available") else None
    usd_cny_date = cny_leg.get("as_of_date")

    if q == a:
        return {
            "quote_currency": q,
            "account_currency": a,
            "available": True,
            "fx_to_account": 1.0,
            "account_to_quote": 1.0,
            "usd_cny": usd_cny,
            "cny_usd": cny_usd,
            "as_of_date": usd_cny_date,
            "source": "identity",
            "fetched_at_utc": now_iso,
        }

    q_leg = cny_leg if q == "CNY" else _get_fx_usd_leg(q, refresh=refresh)
    a_leg = cny_leg if a == "CNY" else _get_fx_usd_leg(a, refresh=refresh)
    if not q_leg.get("available") or not a_leg.get("available"):
        return {
            "quote_currency": q,
            "account_currency": a,
            "available": False,
            "fx_to_account": None,
            "account_to_quote": None,
            "usd_cny": usd_cny,
            "cny_usd": cny_usd,
            "as_of_date": usd_cny_date,
            "source": cny_leg.get("source") if cny_leg.get("available") else None,
            "error": q_leg.get("error") or a_leg.get("error") or "FX reference unavailable",
            "fetched_at_utc": now_iso,
        }

    raw_fx = float(q_leg["usd_per_unit"]) / float(a_leg["usd_per_unit"])
    raw_inv = float(a_leg["usd_per_unit"]) / float(q_leg["usd_per_unit"])
    dates = [d for d in (q_leg.get("as_of_date"), a_leg.get("as_of_date"), usd_cny_date) if d]
    sources = [
        str(s)
        for s in (q_leg.get("source"), a_leg.get("source"))
        if s and s != "identity"
    ]
    return {
        "quote_currency": q,
        "account_currency": a,
        "available": True,
        "fx_to_account": round(raw_fx, 6),
        "account_to_quote": round(raw_inv, 4),
        "usd_cny": usd_cny,
        "cny_usd": cny_usd,
        "as_of_date": max(dates) if dates else None,
        "source": " / ".join(dict.fromkeys(sources)) if sources else "identity",
        "fetched_at_utc": now_iso,
    }


def portfolio_initialized() -> bool:
    try:
        pm.load_account()
        return True
    except FileNotFoundError:
        return False


def portfolio_call(fn: Callable[..., Any], **kwargs: Any) -> SafeJSONResponse:
    """Run a portfolio_manager mutation and map its domain errors onto HTTP status codes."""
    try:
        result = fn(**kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(status_code=404 if "not found" in msg.lower() else 422, detail=msg) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    CACHE.invalidate_prefix("sentinel")
    return ok(result)


def archive_call(fn: Callable[..., Any], *, invalidate_sentinel: bool = False, **kwargs: Any) -> Any:
    try:
        res = fn(**kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(status_code=409 if "revision conflict" in msg.lower() else 422, detail=msg) from exc
    if invalidate_sentinel:
        CACHE.invalidate_prefix("sentinel")
    return res


def _plan_args(req: TradePlanReq) -> tuple[Any, str, dict[str, Any]]:
    # Validate request-level invariants BEFORE consulting portfolio state so invalid payloads always return 422.
    if req.pair_leg_verified:
        if not req.pair_trade:
            raise HTTPException(
                status_code=422,
                detail="pair_leg_verified=true 仅可在 pair_trade=true 时使用。",
            )
        opp_raw = (req.pair_leg_symbol or "").strip()
        if not opp_raw:
            raise HTTPException(
                status_code=422,
                detail="配对核验放行 (pair_leg_verified=true) 必须提供有效的反向对冲腿标的代码 (pair_leg_symbol)。",
            )
        primary_norm, _ = market_snapshot.normalize_symbol(req.symbol)
        opp_norm, _ = market_snapshot.normalize_symbol(opp_raw)
        if primary_norm.upper() == opp_norm.upper() or req.symbol.strip().upper() == opp_raw.upper():
            raise HTTPException(
                status_code=422,
                detail=f"配对反向对冲腿 ({opp_norm}) 不能与当前主标的 ({primary_norm}) 相同。",
            )

    parser = tp.build_parser()
    args = parser.parse_args([f"--symbol={req.symbol}", f"--direction={req.direction}"])

    equity = req.account_equity
    currency = req.account_currency
    risk_pct = req.risk_budget_pct
    if equity is None:
        try:
            account = pm.load_account()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail="本地持仓账本尚未初始化，请先初始化账本或显式传入模拟账户净值 (account_equity)。",
            ) from exc
        ledger_ccy = str(account.get("base_currency") or "").strip().upper()
        if currency is not None and ledger_ccy and currency.strip().upper() != ledger_ccy:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"请求的账户币种 ({currency.strip().upper()}) 与本地持仓账本本位币 ({ledger_ccy}) 不一致，"
                    "请刷新账本状态并按账本本位币重新确认折算汇率。"
                ),
            )
        equity = float(account["equity"])
        currency = ledger_ccy or currency
        if risk_pct is None:
            risk_pct = account.get("default_risk_per_trade_pct")
        equity_source = "portfolio_ledger"
    else:
        equity_source = "manual_simulation"
    args.equity = equity
    if currency:
        args.account_currency = currency
    if risk_pct is not None:
        args.risk_pct = float(risk_pct)

    tranches = req.tranche_weights
    if isinstance(tranches, list):
        tranches = ",".join(str(w) for w in tranches)
    if tranches:
        args.tranches = tranches

    optional_map = {
        "entry": req.entry_price,
        "stop": req.tactical_stop_30m,
        "daily_stop": req.structural_stop_daily,
        "target": req.target_price,
        "max_leverage": req.max_leverage,
        "atr30m": req.atr14_30m,
        "atr_daily": req.atr14_daily,
        "multiplier": req.multiplier,
        "margin_rate": req.margin_rate,
        "fx_to_account": req.fx_to_account,
        "asset_class": req.asset_class,
        "curve_structure": req.curve_structure,
        "roll_yield": req.roll_yield_pct,
        "near_roll_yield": req.near_roll_yield_pct,
        "rate_diff_pct": req.rate_diff_pct,
        "funding_ann_pct": req.funding_ann_pct,
        "alignment_status": req.alignment_status,
    }
    for name, value in optional_map.items():
        if value is not None:
            setattr(args, name, value)
    args.slot_risk_share = req.slot_risk_share
    args.max_margin_pct = req.max_margin_pct
    args.allow_equity_margin = req.allow_equity_margin
    args.horizon_days = req.horizon_days
    # Server-side research archive on disk is the authoritative source of truth for L1/L2 gate, US-short verdict & direction_bias
    archive_ctx: dict[str, Any] = {
        "exists": False,
        "market": None,
        "status": None,
        "direction_bias": req.research_direction_bias,
        "layer1_drivers": None,
        "has_l1_l2": False,
    }
    try:
        arch = ra.load_archive(req.symbol, market_override=req.archive_market, evidence_limit=0)
        has_l1_l2 = any(
            isinstance(ev, dict) and ev.get("level") in ("L1", "L2")
            for ev in (arch.get("recent_evidence") or [])
        )
        args.has_l1_l2 = has_l1_l2
        arch_state = arch.get("state") if isinstance(arch, dict) else None
        raw_bias = (
            str(arch_state.get("direction_bias") or "").strip().lower()
            if isinstance(arch_state, dict)
            else ""
        )
        args.us_short_thesis_verified = bool(has_l1_l2 and raw_bias == "short")
        eff_bias = raw_bias if raw_bias in ra.VALID_DIRECTIONS else req.research_direction_bias
        archive_ctx = {
            "exists": True,
            "market": arch.get("market"),
            "status": arch_state.get("status") if isinstance(arch_state, dict) else None,
            "direction_bias": eff_bias,
            "layer1_drivers": arch_state.get("layer1_drivers") if isinstance(arch_state, dict) else None,
            "has_l1_l2": has_l1_l2,
        }
    except (FileNotFoundError, ValueError, OSError):
        args.has_l1_l2 = False
        args.us_short_thesis_verified = False

    eff_direction_bias = archive_ctx["direction_bias"]
    args.research_direction = eff_direction_bias
    args.research_root = ra.DEFAULT_RESEARCH_DIR
    for attr in ("research_direction_bias", "direction_bias", "archive_direction_bias", "thesis_direction_bias"):
        if hasattr(args, attr) or attr in ("research_direction_bias", "direction_bias"):
            setattr(args, attr, eff_direction_bias)
    args.pending_verifications = req.pending_verifications
    args.thesis_falsified = req.thesis_falsified
    args.hedge_only = req.hedge_only
    args.pair_trade = req.pair_trade
    args.pair_leg_verified = req.pair_leg_verified
    args.pair_leg_asset_class = None
    args.pair_leg_short_thesis_verified = None
    opp_raw_leg = (req.pair_leg_symbol or "").strip()
    if req.pair_trade and opp_raw_leg:
        try:
            args.pair_leg_asset_class = tp.get_contract_spec(opp_raw_leg)["asset_class"]
        except ValueError:
            _, inferred_opp_ac = market_snapshot.normalize_symbol(opp_raw_leg)
            args.pair_leg_asset_class = inferred_opp_ac
        try:
            opp_arch = ra.load_archive(opp_raw_leg, evidence_limit=0)
            opp_has_l1_l2 = any(
                isinstance(ev, dict) and ev.get("level") in ("L1", "L2")
                for ev in (opp_arch.get("recent_evidence") or [])
            )
            opp_state = opp_arch.get("state") if isinstance(opp_arch, dict) else None
            opp_bias = (
                str(opp_state.get("direction_bias") or "").strip().lower()
                if isinstance(opp_state, dict)
                else ""
            )
            args.pair_leg_short_thesis_verified = bool(opp_has_l1_l2 and opp_bias == "short")
        except (FileNotFoundError, ValueError, OSError):
            args.pair_leg_short_thesis_verified = False
    args.min_rr = req.min_rr
    args.live = req.live
    return args, equity_source, archive_ctx


def _sse(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {payload}\n\n"


def _safe_static_path(root: str, rel: str) -> str | None:
    root_real = os.path.realpath(root)
    cand = os.path.realpath(os.path.join(root_real, rel))
    if os.path.commonpath([root_real, cand]) != root_real:
        return None
    return cand if os.path.isfile(cand) else None


# ---------------------------------------------------------------------------
# Compression & static caching helpers
# ---------------------------------------------------------------------------
class _SafeGZipMiddleware:
    """GZip HTTP responses while bypassing SSE endpoints so streams never buffer."""

    def __init__(self, app: ASGIApp, minimum_size: int = 500, compresslevel: int = 6) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").endswith("/events"):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


class _ImmutableStaticFiles(StaticFiles):
    """Serve Vite content-hashed /assets/* with immutable long-term caching."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(task_manager: TaskManager | None = None, frontend_dist: str | None = None) -> FastAPI:
    app = FastAPI(title="T3 Trading Desk API", version=__version__, default_response_class=SafeJSONResponse)
    cf_team = os.environ.get("TRADING_DESK_CF_ACCESS_TEAM", "").strip()
    if cf_team:
        cf_hosts = {
            h.strip()
            for h in os.environ.get("TRADING_DESK_CF_ACCESS_HOSTS", "").split(",")
            if h.strip()
        }
        cf_aud = os.environ.get("TRADING_DESK_CF_ACCESS_AUD", "").strip() or None
        app.add_middleware(
            CloudflareAccessMiddleware,
            team_domain=cf_team,
            protected_hosts=cf_hosts,
            audience=cf_aud,
        )
    app.add_middleware(_SafeGZipMiddleware, minimum_size=500, compresslevel=6)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Cache"],
    )
    tasks = task_manager or TaskManager()
    app.state.tasks = tasks

    # ----------------------------------------------------------------- health & search
    @app.get("/api/health")
    def health():
        try:
            universe_count = len(so.load_universes())
        except (OSError, ValueError):
            universe_count = 0
        return ok({
            "status": "ok",
            "version": __version__,
            "portfolio_initialized": portfolio_initialized(),
            "archive_count": len(ra.list_archives()),
            "universe_count": universe_count,
            "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    @app.get("/api/symbols/search")
    def symbols_search(q: str = "", limit: int = Query(30, ge=1, le=200)):
        return ok({"items": search_symbols(q, limit)})

    @app.get("/api/macro/snapshot")
    def macro_snapshot(refresh: bool = False):
        def fetch_bundle() -> dict[str, Any]:
            ctx = market_snapshot.fetch_macro_context()
            return {
                "macro_context": ctx,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        key = ("macro_context",)
        if not refresh:
            hit, cached = CACHE.get(key)
            if hit:
                return ok(cached, headers=_cache_headers(True))

        bundle = fetch_bundle()
        ctx_dict = bundle.get("macro_context")
        has_values = isinstance(ctx_dict, dict) and len(ctx_dict) > 0
        ttl = TTL_MARKET_SECONDS if has_values else 15.0
        CACHE.set(key, bundle, ttl)
        return ok(bundle, headers=_cache_headers(False))

    @app.get("/api/fx/reference")
    def fx_reference(
        quote: str = Query("CNY", min_length=3, max_length=6),
        account: str = Query("USD", min_length=3, max_length=6),
        refresh: bool = False,
    ):
        q_ccy = quote.strip().upper()
        a_ccy = account.strip().upper()
        key = ("fx_reference", q_ccy, a_ccy)
        if not refresh:
            hit, cached = CACHE.get(key)
            if hit:
                return ok(cached, headers=_cache_headers(True))

        bundle = _build_fx_reference(q_ccy, a_ccy, refresh=refresh)
        ttl = TTL_FX_REFERENCE_SECONDS if bundle.get("available") else 15.0
        CACHE.set(key, bundle, ttl)
        return ok(bundle, headers=_cache_headers(False))

    # ----------------------------------------------------------------- screen 1: portfolio & sentinel
    @app.get("/api/portfolio/summary")
    def portfolio_summary():
        try:
            summary = pm.summarize_portfolio()
            positions = pm.load_positions()
            journal = pm.load_journal(limit=50)
        except FileNotFoundError:
            return ok({"initialized": False, "summary": None, "positions": [], "journal": []})
        return ok({"initialized": True, "summary": summary, "positions": positions, "journal": journal})

    @app.post("/api/portfolio/init")
    def portfolio_init(req: PortfolioInitReq):
        return portfolio_call(pm.init_portfolio, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/open")
    def portfolio_open(req: OpenPositionReq):
        return portfolio_call(
            pm.open_position,
            research_dir=ra.DEFAULT_RESEARCH_DIR,
            **req.model_dump(exclude_none=True),
        )

    @app.post("/api/portfolio/positions/{position_id}/scale-in")
    def portfolio_scale_in(position_id: str, req: ScaleInPositionRequest):
        try:
            result = pm.scale_in_position(
                position_id=position_id,
                add_quantity=req.add_quantity,
                add_price=req.add_price,
                new_stop_loss=req.new_stop_loss,
                fx_rate_to_base=req.fx_rate_to_base,
                note=req.note,
                enforce_macro_cap=req.enforce_macro_cap,
                idempotency_key=req.idempotency_key,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            raise HTTPException(status_code=404 if "not found" in msg.lower() else 400, detail=msg) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        CACHE.invalidate_prefix("sentinel")
        summary = pm.summarize_portfolio()
        return ok({**result, "summary": summary})

    @app.post("/api/portfolio/positions/{position_id}/trail-stop")
    def portfolio_trail_stop(position_id: str, req: TrailStopReq):
        return portfolio_call(pm.trail_stop, position_id=position_id, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/{position_id}/trim")
    def portfolio_trim(position_id: str, req: TrimReq):
        return portfolio_call(pm.trim_position, position_id=position_id, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/{position_id}/close")
    def portfolio_close(position_id: str, req: CloseReq):
        return portfolio_call(
            pm.close_position,
            position_id=position_id,
            research_dir=ra.DEFAULT_RESEARCH_DIR,
            **req.model_dump(exclude_none=True),
        )

    @app.post("/api/portfolio/positions/{position_id}/void")
    def portfolio_void(position_id: str, req: VoidReq):
        return portfolio_call(
            pm.void_position,
            position_id=position_id,
            research_dir=ra.DEFAULT_RESEARCH_DIR,
            **req.model_dump(exclude_none=True),
        )

    @app.post("/api/portfolio/positions/{position_id}/rotate")
    def portfolio_rotate(position_id: str, req: RotateReq):
        return portfolio_call(
            pm.rotate_leader,
            old_position_id=position_id,
            exit_price=req.exit_price,
            exit_reason=req.exit_reason,
            new_position_kwargs=req.new_position.model_dump(exclude_none=True),
            exit_category=req.exit_category,
            enforce_macro_cap=req.enforce_macro_cap,
            idempotency_key=req.idempotency_key,
            research_dir=ra.DEFAULT_RESEARCH_DIR,
        )

    @app.get("/api/sentinel/run")
    def sentinel_run(
        include_watchlist: bool = False,
        skip_research: bool = False,
        event_window_days: int = Query(14, ge=1, le=90),
        refresh: bool = False,
    ):
        store_on = data_store.is_store_enabled()
        fp = (
            compute_current_sentinel_fingerprint(include_watchlist, skip_research, event_window_days)
            if store_on
            else None
        )
        base_key = sentinel_cache_key(include_watchlist, skip_research, event_window_days)
        key = (*base_key, fp) if fp is not None else base_key

        if not refresh:
            mem_hit, mem_val = CACHE.get(key)
            if mem_hit:
                return ok(mem_val, headers=_cache_headers(True))
            if store_on and fp is not None:
                captured_epoch = CACHE.get_epoch("sentinel")
                persisted = data_store.get_store().get_fresh_sentinel_run(
                    input_fingerprint=fp,
                    max_age_seconds=TTL_SENTINEL_SECONDS,
                )
                if persisted is not None:
                    fp_after = compute_current_sentinel_fingerprint(
                        include_watchlist, skip_research, event_window_days
                    )
                    if fp_after == fp:
                        CACHE.set(
                            key,
                            persisted,
                            TTL_SENTINEL_SECONDS,
                            expected_epoch=captured_epoch,
                            namespace="sentinel",
                        )
                        return ok(persisted, headers=_cache_headers(True))

        try:
            result, hit = CACHE.get_or_compute(
                key,
                TTL_SENTINEL_SECONDS,
                lambda: run_sentinel_sync(include_watchlist, skip_research, event_window_days),
                refresh=refresh,
                namespace="sentinel",
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(result, headers=_cache_headers(hit))

    # ----------------------------------------------------------------- screen 2: dual war room & planner
    @app.get("/api/market/dual")
    def market_dual(
        symbol: str = Query(..., min_length=1),
        period: str = Query("3y", pattern=PERIOD_PATTERN),
        intraday_interval: Literal["15m", "30m", "60m"] = "30m",
        include_macro: bool = True,
        refresh: bool = False,
    ):
        def fetch() -> dict[str, Any]:
            snap_kw: dict[str, Any] = {
                "period": period,
                "include_macro": include_macro,
                "include_fundamentals": True,
                "full_chanlun": True,
                "include_intraday": True,
                "intraday_interval": intraday_interval,
                "include_ohlcv": True,
            }
            if refresh and "refresh" in inspect.signature(market_snapshot.get_snapshot).parameters:
                snap_kw["refresh"] = True
            snap = market_snapshot.get_snapshot(symbol, **snap_kw)
            if "error" in snap:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "snapshot_error", "message": snap["error"], "symbol": symbol},
                )
            try:
                snap["contract_spec"] = tp.get_contract_spec(
                    snap.get("symbol") or symbol,
                    snap.get("asset_class"),
                )
            except ValueError:
                snap["contract_spec"] = None
            return snap

        key = ("dual", symbol.strip(), period, intraday_interval, include_macro)
        snap, hit = CACHE.get_or_compute(
            key,
            TTL_MARKET_SECONDS,
            fetch,
            refresh=refresh,
            ttl_for_value=lambda v: 0.0 if isinstance(v, dict) and v.get("intraday_error") else TTL_MARKET_SECONDS,
        )
        return ok(snap, headers=_cache_headers(hit))

    @app.post("/api/trade/plan")
    def trade_plan(req: TradePlanReq):
        args, equity_source, archive_ctx = _plan_args(req)
        try:
            output = tp.build_trade_plan(args)
        except tp.PlanInputError as exc:
            msg = str(exc)
            for flag, field in _PLAN_FLAG_FIELDS.items():
                msg = msg.replace(flag, field)
            raise HTTPException(status_code=422, detail=msg) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        output["equity_source"] = equity_source
        output["research_direction_bias"] = archive_ctx["direction_bias"]
        output["archive_context"] = archive_ctx
        output.setdefault("live_warnings", [])
        return ok(output)

    # ----------------------------------------------------------------- screen 3: research archive & data
    @app.get("/api/research/archives")
    def research_archives():
        return ok({"archives": ra.list_archives()})

    @app.get("/api/research/archive")
    def research_archive_get(
        symbol: str = Query(..., min_length=1),
        market: str | None = Query(None),
        market_override: str | None = Query(None),
    ):
        if (
            market is not None
            and market_override is not None
            and market.strip().upper() != market_override.strip().upper()
        ):
            raise HTTPException(status_code=422, detail="Conflicting market and market_override query parameters")
        resolved_market = market if market is not None else market_override
        loaded = archive_call(
            ra.load_archive,
            raw_symbol=symbol,
            market_override=resolved_market,
            evidence_limit=0,
        )
        # Attach authoritative server-side verification evaluation per trigger ID
        state_obj = loaded.get("state") if isinstance(loaded, dict) else None
        triggers = state_obj.get("triggers") if isinstance(state_obj, dict) else None
        trig_evals: dict[str, dict[str, Any]] = {}
        if isinstance(triggers, list):
            now_dt = datetime.now(timezone.utc)
            for item in ra.evaluate_triggers({"triggers": triggers}, {}, now_utc=now_dt):
                tid = str(item.get("id") or "").strip()
                if not tid:
                    continue
                trig_evals[tid] = {
                    "is_auto": ra.is_trigger_auto_metric(item),
                    "evaluated": bool(item.get("evaluated")),
                    "fired": bool(item.get("fired")),
                    "verification_stale": bool(item.get("verification_stale")),
                    "verification_status": item.get("verification_status"),
                    "error": item.get("error"),
                }
        if isinstance(loaded, dict):
            loaded = {**loaded, "trigger_evaluations": trig_evals}
        return ok(loaded)

    @app.post("/api/research/archive/init")
    def research_archive_init(req: ArchiveInitReq):
        resolved_market = req.resolved_market()
        p_legacy = req.primary_driver.strip()
        p_alias = req.layer1_primary.strip() if req.layer1_primary is not None else ""
        if p_legacy and p_alias and p_legacy != p_alias:
            raise HTTPException(
                status_code=422,
                detail="Conflicting primary_driver and layer1_primary fields",
            )
        eff_primary = p_alias or p_legacy

        s_legacy = req.secondary_driver.strip()
        s_alias = req.layer1_secondary.strip() if req.layer1_secondary is not None else ""
        if s_legacy and s_alias and s_legacy != s_alias:
            raise HTTPException(
                status_code=422,
                detail="Conflicting secondary_driver and layer1_secondary fields",
            )
        eff_secondary = s_alias or s_legacy

        init_params = inspect.signature(ra.init_archive).parameters
        init_kwargs: dict[str, Any] = {
            "raw_symbol": req.symbol,
            "name": req.name,
            "subtype": req.subtype,
            "thesis_summary": req.thesis_summary,
            "primary_driver": eff_primary,
            "secondary_driver": eff_secondary,
            "overwrite": req.overwrite,
        }
        if "layer1_primary" in init_params:
            init_kwargs["layer1_primary"] = eff_primary
        if "layer1_secondary" in init_params:
            init_kwargs["layer1_secondary"] = eff_secondary
        if req.cik is not None:
            init_kwargs["cik"] = req.cik
        if resolved_market is not None:
            init_kwargs["market_override"] = resolved_market
        if req.direction_bias is not None and "direction_bias" in init_params:
            init_kwargs["direction_bias"] = req.direction_bias
        if req.confidence is not None and "confidence" in init_params:
            init_kwargs["confidence"] = (
                req.confidence.strip() if isinstance(req.confidence, str) else req.confidence
            )

        res = archive_call(ra.init_archive, invalidate_sentinel=True, **init_kwargs)
        return ok(res)

    @app.post("/api/research/archive/evidence")
    def research_archive_evidence(req: ArchiveEvidenceReq):
        return ok(archive_call(
            ra.append_evidence,
            invalidate_sentinel=True,
            raw_symbol=req.symbol,
            items=req.items,
            market_override=req.resolved_market(),
        ))

    @app.patch("/api/research/archive/state")
    def research_archive_state(req: ArchiveStateReq):
        merged_patch: dict[str, Any] = dict(req.patch) if req.patch is not None else {}

        # Extract & reconcile top-level convenience fields with nested patch keys
        def _merge_scalar(field: str, top_val: Any) -> None:
            if top_val is None:
                return
            if field in merged_patch and merged_patch[field] is not None and merged_patch[field] != top_val:
                raise HTTPException(
                    status_code=422,
                    detail=f"Conflicting top-level {field} and patch.{field}",
                )
            merged_patch[field] = top_val

        _merge_scalar("direction_bias", req.direction_bias)
        _merge_scalar("thesis_summary", req.thesis_summary)
        _merge_scalar("status", req.status)
        _merge_scalar("confidence", req.confidence)

        # Reconcile layer1_primary / layer1_secondary / layer1_drivers into canonical state.layer1_drivers
        patch_l1_primary = merged_patch.pop("layer1_primary", None)
        patch_l1_secondary = merged_patch.pop("layer1_secondary", None)
        if patch_l1_primary is not None and not isinstance(patch_l1_primary, str):
            raise HTTPException(status_code=422, detail="layer1_primary 必须为字符串")
        if patch_l1_secondary is not None and not isinstance(patch_l1_secondary, str):
            raise HTTPException(status_code=422, detail="layer1_secondary 必须为字符串")
        if (
            req.layer1_primary is not None
            and patch_l1_primary is not None
            and req.layer1_primary.strip() != patch_l1_primary.strip()
        ):
            raise HTTPException(
                status_code=422,
                detail="Conflicting top-level layer1_primary and patch.layer1_primary",
            )
        if (
            req.layer1_secondary is not None
            and patch_l1_secondary is not None
            and req.layer1_secondary.strip() != patch_l1_secondary.strip()
        ):
            raise HTTPException(
                status_code=422,
                detail="Conflicting top-level layer1_secondary and patch.layer1_secondary",
            )
        eff_l1_primary = req.layer1_primary if req.layer1_primary is not None else patch_l1_primary
        eff_l1_secondary = req.layer1_secondary if req.layer1_secondary is not None else patch_l1_secondary

        if req.layer1_drivers is not None or eff_l1_primary is not None or eff_l1_secondary is not None:
            raw_drivers = merged_patch.get("layer1_drivers")
            if raw_drivers is not None and not isinstance(raw_drivers, dict):
                raise HTTPException(status_code=422, detail="layer1_drivers 必须为对象")
            drivers = dict(raw_drivers or {})
            if req.layer1_drivers is not None:
                drivers.update(req.layer1_drivers)
            if eff_l1_primary is not None:
                drivers["primary"] = eff_l1_primary.strip()
            if eff_l1_secondary is not None:
                drivers["secondary"] = eff_l1_secondary.strip()
            merged_patch["layer1_drivers"] = drivers

        if not merged_patch:
            raise HTTPException(status_code=422, detail="patch 不能为空，请至少提供一个可更新字段")

        # Explicit normalization & clear validation for key thesis metadata fields
        if "direction_bias" in merged_patch:
            if not isinstance(merged_patch["direction_bias"], str):
                raise HTTPException(status_code=422, detail="direction_bias 必须为字符串（long/short/neutral/pair）")
            norm_bias = merged_patch["direction_bias"].strip().lower()
            if norm_bias not in ra.VALID_DIRECTIONS:
                raise HTTPException(
                    status_code=422,
                    detail=f"无效的 direction_bias {merged_patch['direction_bias']!r}，仅允许 {sorted(ra.VALID_DIRECTIONS)}",
                )
            merged_patch["direction_bias"] = norm_bias

        if "status" in merged_patch:
            if not isinstance(merged_patch["status"], str):
                raise HTTPException(status_code=422, detail="status 必须为字符串")
            norm_status = merged_patch["status"].strip().lower()
            if norm_status not in ra.VALID_STATUSES:
                raise HTTPException(
                    status_code=422,
                    detail=f"无效的 status {merged_patch['status']!r}，仅允许 {sorted(ra.VALID_STATUSES)}",
                )
            merged_patch["status"] = norm_status

        if "thesis_summary" in merged_patch:
            if not isinstance(merged_patch["thesis_summary"], str):
                raise HTTPException(status_code=422, detail="thesis_summary 必须为字符串")
            merged_patch["thesis_summary"] = merged_patch["thesis_summary"].strip()

        if "confidence" in merged_patch and merged_patch["confidence"] is not None:
            conf_val = merged_patch["confidence"]
            if isinstance(conf_val, str):
                merged_patch["confidence"] = conf_val.strip().lower()
            elif isinstance(conf_val, bool) or not isinstance(conf_val, (int, float)):
                raise HTTPException(
                    status_code=422,
                    detail="confidence 必须为 'low'/'medium'/'high' 或 [0.0, 1.0] 数值",
                )

        return ok(archive_call(
            ra.update_state,
            invalidate_sentinel=True,
            raw_symbol=req.symbol,
            patch=merged_patch,
            expected_revision=req.expected_revision,
            market_override=req.resolved_market(),
        ))

    @app.post("/api/research/archive/log")
    def research_archive_log(req: ArchiveLogReq):
        line = archive_call(
            ra.append_log,
            invalidate_sentinel=True,
            raw_symbol=req.symbol,
            entry=req.entry,
            market_override=req.resolved_market(),
        )
        return ok({"appended": line})

    @app.put("/api/research/archive/thesis")
    def research_archive_thesis_put(req: ArchiveThesisReq):
        res = archive_call(
            ra.update_thesis_body,
            invalidate_sentinel=False,
            raw_symbol=req.symbol,
            body_md=req.body_md,
            expected_revision=req.expected_revision,
            note=req.note,
            market_override=req.resolved_market(),
        )
        return ok(res)

    @app.post("/api/research/archive/trigger-verify-manual")
    def research_archive_trigger_verify_manual(req: TriggerManualVerifyReq):
        src_url = req.source_url.strip()
        if src_url and not ra.is_valid_http_url(src_url):
            raise HTTPException(
                status_code=422,
                detail="source_url must be a valid http:// or https:// URL with a hostname",
            )
        summary_clean = req.summary.strip()
        if not summary_clean:
            raise HTTPException(status_code=422, detail="summary must be non-empty")
        ver_payload: dict[str, Any] = {
            "method": "manual",
            "status": req.status,
            "verified_at_utc": ra.utc_now_iso_seconds(),
            "ttl_days": req.ttl_days,
            "evidence_tier": req.evidence_tier,
            "summary": summary_clean,
        }
        if req.observed is not None and req.observed != "":
            ver_payload["observed"] = req.observed
        if req.observed_text.strip():
            ver_payload["observed_text"] = req.observed_text.strip()
        if req.source_title.strip():
            ver_payload["source_title"] = req.source_title.strip()
        if src_url:
            ver_payload["source_url"] = src_url

        res = archive_call(
            ra.record_trigger_verifications,
            invalidate_sentinel=True,
            raw_symbol=req.symbol.strip(),
            verifications={req.trigger_id.strip(): ver_payload},
            market_override=req.resolved_market(),
            reject_auto_metrics=True,
            enforce_clear_gate=True,
        )
        if not res.get("updated_ids"):
            conflicts = res.get("skipped_conflicts") or []
            reason = conflicts[0].get("reason") if conflicts else "未找到可更新的目标触发器"
            raise HTTPException(status_code=422, detail=reason)
        return ok(res)

    @app.get("/api/research/engines")
    def research_engines_get():
        return ok(deep_research.detect_available_engines())

    @app.get("/api/research/data")
    def research_data_get(symbol: str = Query(..., min_length=1), include_macro: bool = True, refresh: bool = False):
        def fetch() -> dict[str, Any]:
            rd_kw: dict[str, Any] = {"include_macro": include_macro}
            if refresh and "refresh" in inspect.signature(research_data.collect_research_data).parameters:
                rd_kw["refresh"] = True
            return research_data.collect_research_data(symbol, **rd_kw)

        try:
            bundle, hit = CACHE.get_or_compute(
                research_cache_key(symbol, include_macro), TTL_RESEARCH_SECONDS, fetch, refresh=refresh
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(bundle, headers=_cache_headers(hit))

    # ----------------------------------------------------------------- screen 4: universes & tasks
    @app.get("/api/universes")
    def universes_get():
        try:
            return ok({"universes": so.load_universes()})
        except FileNotFoundError:
            return ok({"universes": {}})

    @app.post("/api/universes")
    def universes_save(req: UniverseReq):
        try:
            saved = so.save_universe(
                req.name,
                req.symbols,
                description=req.description,
                allow_overwrite=req.overwrite,
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok({"name": req.name.strip(), **saved})

    @app.get("/api/compass/macro-overview")
    async def compass_macro_overview(refresh: bool = False):
        bundle = await asyncio.to_thread(macro_compass.collect_macro_overview, refresh=refresh)
        return ok(bundle, headers=_cache_headers(bool(bundle.get("cache_hit"))))

    @app.get("/api/compass/latest")
    def compass_latest():
        latest = macro_compass.get_latest_compass_run()
        history = macro_compass.list_compass_runs(limit=15)
        active = tasks.get_active_macro_compass()
        return ok(
            {
                "latest": latest,
                "history": history,
                "active_task": active,
            }
        )

    @app.get("/api/compass/history")
    def compass_history(limit: int = Query(20, ge=1, le=60)):
        return ok({"runs": macro_compass.list_compass_runs(limit=limit)})

    @app.get("/api/compass/history/{run_id}")
    def compass_history_detail(run_id: str):
        detail = macro_compass.get_compass_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Compass run not found: {run_id}")
        return ok(detail)

    @app.post("/api/tasks/macro-compass", status_code=202)
    async def task_macro_compass(req: MacroCompassTaskReq):
        eng_info = deep_research.detect_available_engines()
        chosen_engine = req.engine or (eng_info.get("default_engine") or "")
        if not chosen_engine or not deep_research.resolve_engine_binary(chosen_engine):
            raise HTTPException(
                status_code=503,
                detail="本机未检测到可用 AI 推演引擎（Claude Code 或 Codex CLI），请检查 CLI 安装与登录状态。",
            )
        params = {
            "engine": chosen_engine,
            "effort": req.effort,
            "focus_scopes": macro_compass.normalize_focus_scopes(req.focus_scopes),
            "focus_prompt": req.focus_prompt.strip(),
            "refresh_macro": req.refresh_macro,
        }
        try:
            task, already_running = tasks.start_macro_compass(params)
        except TaskStoppingConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        summary = {**task.summary(), "already_running": already_running}
        return ok(summary, status_code=202)

    @app.post("/api/tasks/scan", status_code=202)
    async def task_scan(req: ScanTaskReq):
        try:
            targets = await asyncio.to_thread(so.resolve_targets, universe_names=req.universes, symbols=req.symbols)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not targets:
            raise HTTPException(status_code=422, detail="No scan targets resolved")
        dumped = req.model_dump()
        if req.compass_context is not None:
            # Canonicalize expected_directions keys and strictly enforce A-share short ban on explicit & implicit short legs
            norm_expected: dict[str, str] = {}
            forbidden_a_share_shorts: list[str] = []
            for raw_sym, exp_dir in req.compass_context.expected_directions.items():
                s = str(raw_sym or "").strip()
                if not s:
                    continue
                norm_expected[s.upper()] = exp_dir
                try:
                    canon_sym, ac = market_snapshot.normalize_symbol(s)
                    norm_expected[canon_sym.upper()] = exp_dir
                    if exp_dir == "short" and ac == "a_share" and canon_sym not in forbidden_a_share_shorts:
                        forbidden_a_share_shorts.append(canon_sym)
                except Exception:
                    pass
            # Also check implicit short targets when the Compass card direction itself is "short"
            if req.compass_context.direction == "short":
                for _mkt, tgt_sym in targets:
                    try:
                        canon_sym, ac = market_snapshot.normalize_symbol(str(tgt_sym))
                        explicit_dir = norm_expected.get(canon_sym.upper()) or norm_expected.get(str(tgt_sym).upper())
                        if ac == "a_share" and explicit_dir != "long" and canon_sym not in forbidden_a_share_shorts:
                            forbidden_a_share_shorts.append(canon_sym)
                    except Exception:
                        pass
            if forbidden_a_share_shorts:
                raise HTTPException(
                    status_code=422,
                    detail=f"A 股严禁作为做空标的（请改用美股/ETF、商品期货或外汇空头代理）：{', '.join(forbidden_a_share_shorts)}",
                )
            dumped["compass_context"] = {
                **req.compass_context.model_dump(),
                "expected_directions": norm_expected,
            }
        params = {**dumped, "targets": [list(t) for t in targets]}
        return ok(tasks.start("scan", params).summary(), status_code=202)

    @app.post("/api/tasks/research-refresh", status_code=202)
    async def task_research_refresh(req: ResearchRefreshTaskReq):
        if not req.symbol.strip():
            raise HTTPException(status_code=422, detail="symbol must be non-empty")
        params = {
            "symbol": req.symbol.strip(),
            "include_macro": req.include_macro,
            "market": req.resolved_market(),
        }
        return ok(tasks.start("research_refresh", params).summary(), status_code=202)

    @app.post("/api/tasks/deep-research", status_code=202)
    async def task_deep_research(req: DeepResearchTaskReq):
        if not req.symbol.strip():
            raise HTTPException(status_code=422, detail="symbol must be non-empty")
        try:
            _, resolved_market, norm_symbol, _ = ra.resolve_archive_dir(
                req.symbol.strip(),
                market_override=req.resolved_market(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not deep_research.resolve_engine_binary(req.engine):
            raise HTTPException(
                status_code=503,
                detail=f"本机未检测到可用的 `{req.engine}` 研究引擎，请在弹窗中切换其他可用引擎或检查 CLI 安装。",
            )

        params = {
            "symbol": norm_symbol,
            "market": resolved_market,
            "mode": req.mode,
            "engine": req.engine,
            "focus_prompt": req.focus_prompt.strip(),
            "include_macro": req.include_macro,
        }
        task, already_running = tasks.start_deep_research(params)
        summary = {**task.summary(), "already_running": already_running}
        return ok(summary, status_code=202)

    @app.post("/api/tasks/sentinel", status_code=202)
    async def task_sentinel(req: SentinelTaskReq):
        return ok(tasks.start("sentinel", req.model_dump()).summary(), status_code=202)

    @app.post("/api/tasks/verify-triggers", status_code=202)
    async def task_verify_triggers(req: TriggerVerifyTaskReq):
        eng_info = deep_research.detect_available_engines()
        chosen_engine = req.engine or (eng_info.get("default_engine") or "")
        if not chosen_engine or not deep_research.resolve_engine_binary(chosen_engine):
            raise HTTPException(
                status_code=503,
                detail="本机未检测到可用 AI 核验引擎（Claude Code 或 Codex CLI），请先检查 CLI 安装或使用手动核验。",
            )
        norm_symbol = ""
        resolved_market = req.resolved_market()
        if req.symbol.strip():
            try:
                _, resolved_market, norm_symbol, _ = ra.resolve_archive_dir(
                    req.symbol.strip(),
                    market_override=resolved_market,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        params = {
            "symbol": norm_symbol,
            "market": resolved_market,
            "trigger_ids": [t.strip() for t in req.trigger_ids if t.strip()],
            "engine": chosen_engine,
            "force": req.force,
            "ttl_days": req.ttl_days,
        }
        return ok(tasks.start("trigger_verify", params).summary(), status_code=202)

    @app.get("/api/tasks")
    def task_list():
        return ok({"tasks": tasks.list()})

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        detail = tasks.detail(task_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return ok(detail)

    @app.post("/api/tasks/{task_id}/cancel")
    def task_cancel(task_id: str):
        cancelled = tasks.cancel(task_id)
        if cancelled is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return ok(cancelled)

    # ----------------------------------------------------------------- local SQLite warehouse & history
    @app.get("/api/data/warehouse")
    def data_warehouse():
        return ok(data_store.get_store().warehouse_summary())

    @app.post("/api/data/health-check")
    async def data_health_check_post(req: DataHealthCheckReq):
        try:
            report = await asyncio.to_thread(
                data_health_check.run_data_health_check,
                universes=req.universes or None,
                symbols=req.symbols or None,
                refresh=req.refresh,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(report)

    @app.get("/api/history/scans")
    def history_scans_list(limit: int = Query(30, ge=1, le=200)):
        return ok({"runs": data_store.get_store().list_scan_runs(limit=limit)})

    @app.get("/api/history/scans/{run_id}")
    def history_scan_detail(run_id: str):
        rec = data_store.get_store().get_scan_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Scan run not found: {run_id}")
        return ok(rec)

    @app.get("/api/history/sentinel")
    def history_sentinel_list(limit: int = Query(20, ge=1, le=100)):
        return ok({"runs": data_store.get_store().list_sentinel_runs(limit=limit)})

    @app.get("/api/history/sentinel/{run_id}")
    def history_sentinel_detail(run_id: str):
        rec = data_store.get_store().get_sentinel_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Sentinel run not found: {run_id}")
        return ok(rec)

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request):
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        try:
            last_id = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            last_id = 0

        async def stream() -> AsyncIterator[str]:
            queue, replay = tasks.subscribe(task)
            try:
                last_seq = last_id
                for ev in replay:
                    if ev["seq"] > last_seq:
                        last_seq = ev["seq"]
                        yield _sse(ev)
                if queue is None:
                    return
                while True:
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
                        continue
                    if ev["seq"] <= last_seq:
                        continue
                    last_seq = ev["seq"]
                    yield _sse(ev)
                    if ev["type"] in TERMINAL_EVENT_TYPES:
                        return
            finally:
                tasks.unsubscribe(task, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.api_route("/api/{rest_of_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest_of_path: str):
        raise HTTPException(status_code=404, detail=f"Unknown API route: /api/{rest_of_path}")

    # ----------------------------------------------------------------- static SPA
    dist = frontend_dist if frontend_dist is not None else FRONTEND_DIST
    index_path = os.path.join(dist, "index.html")
    if os.path.isfile(index_path):
        assets_dir = os.path.join(dist, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", _ImmutableStaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            static = _safe_static_path(dist, full_path) if full_path else None
            if static:
                return FileResponse(static)
            return FileResponse(index_path, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()

__all__ = ["app", "create_app", "sanitize"]
