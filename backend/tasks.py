"""In-memory async task manager (ring buffer) with per-task SSE event streaming."""
from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import daily_sentinel
import market_snapshot
import portfolio_manager
import research_archive
import research_data
import scan_opportunities

from .cache import CACHE, TTL_RESEARCH_SECONDS, TTL_SENTINEL_SECONDS
from .serializers import sanitize

MAX_TASKS = 50
TERMINAL_STATUSES = frozenset({"completed", "failed"})
TERMINAL_EVENT_TYPES = frozenset({"done", "error"})
TASK_KINDS = ("scan", "research_refresh", "sentinel")

Emit = Callable[[str, dict[str, Any]], None]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def research_cache_key(symbol: str, include_macro: bool) -> tuple:
    return ("research", symbol.strip(), bool(include_macro))


def sentinel_cache_key(include_watchlist: bool, skip_research: bool, event_window_days: int) -> tuple:
    return ("sentinel", bool(include_watchlist), bool(skip_research), int(event_window_days))


def run_sentinel_sync(include_watchlist: bool, skip_research: bool, event_window_days: int) -> dict[str, Any]:
    return daily_sentinel.run_daily_sentinel(
        portfolio_dir=portfolio_manager.DEFAULT_PORTFOLIO_DIR,
        research_root=research_archive.DEFAULT_RESEARCH_DIR,
        include_watchlist=include_watchlist,
        skip_research=skip_research,
        event_window_days=event_window_days,
    )


class Task:
    def __init__(self, kind: str, params: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "queued"
        self.created_at = utc_iso()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.progress: dict[str, Any] = {"done": 0, "total": None}
        self.events: list[dict[str, Any]] = []
        self.result: Any = None
        self.error: str | None = None
        self.subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "params": self.params,
            "created_at": self.created_at,
            "created_at_utc": self.created_at,
            "started_at": self.started_at,
            "started_at_utc": self.started_at,
            "finished_at": self.finished_at,
            "finished_at_utc": self.finished_at,
            "progress": dict(self.progress),
            "event_count": len(self.events),
            "error": self.error,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "result": self.result, "events": list(self.events)}


class TaskManager:
    """Runs blocking work via ``asyncio.to_thread`` and fans events out to SSE subscribers."""

    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self.max_tasks = max_tasks
        self._lock = threading.RLock()
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ registry
    def _register(self, task: Task) -> None:
        with self._lock:
            while len(self._tasks) >= self.max_tasks:
                victim = next((tid for tid, t in self._tasks.items() if t.terminal), next(iter(self._tasks)))
                self._tasks.pop(victim)
            self._tasks[task.id] = task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def detail(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.detail() if task is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.summary() for t in reversed(self._tasks.values())]

    # ------------------------------------------------------------------ events
    def _emit(self, task: Task, etype: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ts = utc_iso()
            clean_data = sanitize(data)
            event: dict[str, Any] = {
                "seq": len(task.events) + 1,
                "type": etype,
                "ts": ts,
                "ts_utc": ts,
                "data": clean_data,
            }
            if isinstance(clean_data, dict):
                for key in ("message", "row", "result", "done", "total"):
                    if key in clean_data:
                        event[key] = clean_data[key]
            if etype == "progress" and isinstance(clean_data, dict):
                task.progress = {k: clean_data.get(k, task.progress.get(k)) for k in ("done", "total")}
            task.events.append(event)
            subs = list(task.subscribers)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # subscriber loop already closed
                pass
        return event

    def subscribe(self, task: Task) -> tuple[asyncio.Queue | None, list[dict[str, Any]]]:
        """Atomically snapshot replay events and register a live queue (``None`` if already terminal)."""
        with self._lock:
            replay = list(task.events)
            if task.terminal:
                return None, replay
            queue: asyncio.Queue = asyncio.Queue()
            task.subscribers.append((asyncio.get_running_loop(), queue))
            return queue, replay

    def unsubscribe(self, task: Task, queue: asyncio.Queue | None) -> None:
        if queue is None:
            return
        with self._lock:
            task.subscribers = [(lp, q) for lp, q in task.subscribers if q is not queue]

    # ------------------------------------------------------------------ execution
    def start(self, kind: str, params: dict[str, Any]) -> Task:
        """Create the task and schedule it on the running loop. Must be called from async context."""
        if kind not in TASK_KINDS:
            raise ValueError(f"Unknown task kind: {kind!r}")
        task = Task(kind, params)
        self._register(task)
        bg = asyncio.get_running_loop().create_task(self._run(task))
        self._background.add(bg)
        bg.add_done_callback(self._background.discard)
        return task

    async def _run(self, task: Task) -> None:
        runner = RUNNERS[task.kind]
        with self._lock:
            task.status = "running"
            task.started_at = utc_iso()
        self._emit(task, "step", {"message": f"{task.kind} started", "stage": "start"})

        def emit(etype: str, data: dict[str, Any]) -> None:
            self._emit(task, etype, data)

        try:
            result = await asyncio.to_thread(runner, task.params, emit)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            # Status flip and terminal event are one atomic step so late subscribers never miss it.
            with self._lock:
                task.status = "failed"
                task.error = message
                task.finished_at = utc_iso()
                self._emit(task, "error", {"message": message})
            return
        with self._lock:
            task.status = "completed"
            task.result = sanitize(result)
            task.finished_at = utc_iso()
            self._emit(task, "done", {"result": result})


# ---------------------------------------------------------------------- runners
def run_scan(params: dict[str, Any], emit: Emit) -> dict[str, Any]:
    targets = [tuple(t) for t in params["targets"]]
    period = params.get("period", "3y")
    sort_mode = params.get("sort", "tier")
    total = len(targets)
    emit("step", {"message": f"resolved {total} targets", "stage": "resolve", "total": total})
    emit("progress", {"done": 0, "total": total})

    rows: list[dict[str, Any]] = []
    for idx, (market, sym) in enumerate(targets, start=1):
        emit("step", {"message": f"scanning {sym}", "stage": "scan", "symbol": sym, "market": market, "index": idx})
        try:
            snap = market_snapshot.get_snapshot(sym, period=period, include_macro=False, include_fundamentals=False)
            row = scan_opportunities.build_scan_row(market, sym, snap)
        except Exception as exc:  # noqa: BLE001
            row = scan_opportunities.build_scan_row(market, sym, {"error": str(exc)})
        rows.append(row)
        emit("row", {"row": row, "index": idx})
        emit("progress", {"done": idx, "total": total, "symbol": sym})

    sorted_rows = scan_opportunities.sort_scan_rows(rows, sort_mode=sort_mode)
    errors = sum(1 for r in rows if "error" in r)
    return {"rows": sorted_rows, "count": len(rows), "errors": errors}


def run_research_refresh(params: dict[str, Any], emit: Emit) -> dict[str, Any]:
    symbol = params["symbol"]
    include_macro = bool(params.get("include_macro", True))
    total = 3
    emit("progress", {"done": 0, "total": total})
    emit("step", {"message": f"collecting research data for {symbol}", "stage": "collect"})
    captured_epoch = CACHE.get_epoch("research")
    bundle = research_data.collect_research_data(symbol, include_macro=include_macro)
    CACHE.set(
        research_cache_key(symbol, include_macro),
        bundle,
        TTL_RESEARCH_SECONDS,
        expected_epoch=captured_epoch,
    )
    emit("progress", {"done": 1, "total": total})

    emit("step", {"message": "loading research archive", "stage": "archive"})
    try:
        loaded = research_archive.load_archive(symbol, evidence_limit=0)
    except FileNotFoundError:
        emit("progress", {"done": total, "total": total})
        return {
            "symbol": symbol,
            "archive_exists": False,
            "sources_status": bundle.get("sources_status"),
            "triggers": [],
            "fired_count": 0,
        }
    emit("progress", {"done": 2, "total": total})

    emit("step", {"message": "evaluating archive triggers", "stage": "triggers"})
    raw_obs = daily_sentinel.extract_sentinel_observations(bundle, None)
    warnings = list(raw_obs.get("data_health_warnings") or [])
    observations = {k: v for k, v in raw_obs.items() if k != "data_health_warnings"}
    state = loaded["state"]
    armed = [t for t in (state.get("triggers") or []) if daily_sentinel._is_trigger_armed(t)]
    results = research_archive.evaluate_triggers({"triggers": armed}, observations) if armed else []
    emit("progress", {"done": total, "total": total})
    return {
        "symbol": loaded.get("symbol", symbol),
        "archive_exists": True,
        "revision": state.get("revision"),
        "status": state.get("status"),
        "sources_status": bundle.get("sources_status"),
        "data_health_warnings": warnings,
        "triggers": results,
        "fired_count": sum(1 for t in results if t.get("fired")),
    }


def run_sentinel(params: dict[str, Any], emit: Emit) -> dict[str, Any]:
    include_watchlist = bool(params.get("include_watchlist", False))
    skip_research = bool(params.get("skip_research", False))
    window = int(params.get("event_window_days", 14))
    emit("progress", {"done": 0, "total": 1})
    emit("step", {"message": "running holding daily sentinel", "stage": "sentinel"})
    captured_epoch = CACHE.get_epoch("sentinel")
    result = run_sentinel_sync(include_watchlist, skip_research, window)
    CACHE.set(
        sentinel_cache_key(include_watchlist, skip_research, window),
        result,
        TTL_SENTINEL_SECONDS,
        expected_epoch=captured_epoch,
    )
    emit("progress", {"done": 1, "total": 1})
    return result


RUNNERS: dict[str, Callable[[dict[str, Any], Emit], Any]] = {
    "scan": run_scan,
    "research_refresh": run_research_refresh,
    "sentinel": run_sentinel,
}
