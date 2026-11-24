"""FastAPI gateway for the T3 Trading Desk 4-screen workbench (thin wrapper over ``scripts/``)."""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any, Literal

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

from . import ROOT_DIR, __version__
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
FRONTEND_DIST = os.path.join(ROOT_DIR, "frontend", "dist")
PERIOD_PATTERN = r"^(\d+(d|wk|mo|y)|ytd|max)$"

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


class CloseReq(_Strict):
    exit_price: float
    exit_reason: str
    exit_category: str = "manual"
    update_account_equity: bool = True
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
    cik: str | None = None
    overwrite: bool = False


class ArchiveEvidenceReq(_ArchiveMarketReq):
    symbol: str
    items: list[dict[str, Any]]


class ArchiveStateReq(_ArchiveMarketReq):
    symbol: str
    patch: dict[str, Any]
    expected_revision: int | None = None


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


class ScanTaskReq(_Strict):
    universes: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    period: str = Field("3y", pattern=PERIOD_PATTERN)
    sort: Literal["tier", "none"] = Field("tier", validation_alias=AliasChoices("sort_mode", "sort"))


class ResearchRefreshTaskReq(_ArchiveMarketReq):
    symbol: str
    include_macro: bool = True


class SentinelTaskReq(_Strict):
    include_watchlist: bool = False
    skip_research: bool = False
    event_window_days: int = Field(14, ge=1, le=90)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ok(data: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> SafeJSONResponse:
    return SafeJSONResponse(data, status_code=status_code, headers=headers)


def _cache_headers(hit: bool) -> dict[str, str]:
    return {"X-Cache": "HIT" if hit else "MISS"}


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


def _plan_args(req: TradePlanReq) -> tuple[Any, str]:
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
    # Server-side research archive on disk is the sole source of truth for L1/L2 evidence gate
    try:
        arch = ra.load_archive(req.symbol, evidence_limit=0)
        args.has_l1_l2 = any(
            isinstance(ev, dict) and ev.get("level") in ("L1", "L2")
            for ev in (arch.get("recent_evidence") or [])
        )
    except (FileNotFoundError, ValueError, OSError):
        args.has_l1_l2 = False
    args.pending_verifications = req.pending_verifications
    args.thesis_falsified = req.thesis_falsified
    args.hedge_only = req.hedge_only
    args.pair_trade = req.pair_trade
    args.pair_leg_verified = req.pair_leg_verified
    args.min_rr = req.min_rr
    args.live = req.live
    return args, equity_source


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
        return portfolio_call(pm.open_position, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/{position_id}/trail-stop")
    def portfolio_trail_stop(position_id: str, req: TrailStopReq):
        return portfolio_call(pm.trail_stop, position_id=position_id, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/{position_id}/trim")
    def portfolio_trim(position_id: str, req: TrimReq):
        return portfolio_call(pm.trim_position, position_id=position_id, **req.model_dump(exclude_none=True))

    @app.post("/api/portfolio/positions/{position_id}/close")
    def portfolio_close(position_id: str, req: CloseReq):
        return portfolio_call(pm.close_position, position_id=position_id, **req.model_dump(exclude_none=True))

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
        )

    @app.get("/api/sentinel/run")
    def sentinel_run(
        include_watchlist: bool = False,
        skip_research: bool = False,
        event_window_days: int = Query(14, ge=1, le=90),
        refresh: bool = False,
    ):
        key = sentinel_cache_key(include_watchlist, skip_research, event_window_days)
        try:
            result, hit = CACHE.get_or_compute(
                key,
                TTL_SENTINEL_SECONDS,
                lambda: run_sentinel_sync(include_watchlist, skip_research, event_window_days),
                refresh=refresh,
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
            snap = market_snapshot.get_snapshot(
                symbol,
                period=period,
                include_macro=include_macro,
                include_fundamentals=True,
                full_chanlun=True,
                include_intraday=True,
                intraday_interval=intraday_interval,
                include_ohlcv=True,
            )
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
        snap, hit = CACHE.get_or_compute(key, TTL_MARKET_SECONDS, fetch, refresh=refresh)
        return ok(snap, headers=_cache_headers(hit))

    @app.post("/api/trade/plan")
    def trade_plan(req: TradePlanReq):
        args, equity_source = _plan_args(req)
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
        return ok(
            archive_call(
                ra.load_archive,
                raw_symbol=symbol,
                market_override=resolved_market,
                evidence_limit=0,
            )
        )

    @app.post("/api/research/archive/init")
    def research_archive_init(req: ArchiveInitReq):
        resolved_market = req.resolved_market()
        data = req.model_dump(exclude_none=True, exclude={"market", "market_override"})
        data["raw_symbol"] = data.pop("symbol")
        data["thesis_summary"] = data.get("thesis_summary", "")
        if resolved_market is not None:
            data["market_override"] = resolved_market
        return ok(archive_call(ra.init_archive, invalidate_sentinel=True, **data))

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
        return ok(archive_call(
            ra.update_state,
            invalidate_sentinel=True,
            raw_symbol=req.symbol,
            patch=req.patch,
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

    @app.get("/api/research/data")
    def research_data_get(symbol: str = Query(..., min_length=1), include_macro: bool = True, refresh: bool = False):
        def fetch() -> dict[str, Any]:
            return research_data.collect_research_data(symbol, include_macro=include_macro)

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
            saved = so.save_universe(req.name, req.symbols, description=req.description)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok({"name": req.name.strip(), **saved})

    @app.post("/api/tasks/scan", status_code=202)
    async def task_scan(req: ScanTaskReq):
        try:
            targets = await asyncio.to_thread(so.resolve_targets, universe_names=req.universes, symbols=req.symbols)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not targets:
            raise HTTPException(status_code=422, detail="No scan targets resolved")
        params = {**req.model_dump(), "targets": [list(t) for t in targets]}
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

    @app.post("/api/tasks/sentinel", status_code=202)
    async def task_sentinel(req: SentinelTaskReq):
        return ok(tasks.start("sentinel", req.model_dump()).summary(), status_code=202)

    @app.get("/api/tasks")
    def task_list():
        return ok({"tasks": tasks.list()})

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        detail = tasks.detail(task_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return ok(detail)

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
