"""In-memory async task manager (ring buffer) with per-task SSE event streaming."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import inspect
import json
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import daily_sentinel
import data_store
import market_snapshot
import portfolio_manager
import research_archive
import research_data
import scan_opportunities

from .cache import CACHE, TTL_RESEARCH_SECONDS, TTL_SENTINEL_SECONDS
from .serializers import sanitize

MAX_TASKS = 50
MAX_PERSISTED_SCANS = 10
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "cancelled"})
TASK_KINDS = ("scan", "research_refresh", "sentinel")

Emit = Callable[[str, dict[str, Any]], None]


class TaskCancelledError(RuntimeError):
    """Raised internally when a running task observes user cancellation."""


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def research_cache_key(symbol: str, include_macro: bool) -> tuple:
    return ("research", symbol.strip(), bool(include_macro))


def sentinel_cache_key(include_watchlist: bool, skip_research: bool, event_window_days: int) -> tuple:
    return ("sentinel", bool(include_watchlist), bool(skip_research), int(event_window_days))


def compute_current_sentinel_fingerprint(
    include_watchlist: bool,
    skip_research: bool,
    event_window_days: int,
) -> str:
    return data_store.DeskDataStore.compute_sentinel_fingerprint(
        portfolio_dir=portfolio_manager.DEFAULT_PORTFOLIO_DIR,
        research_root=research_archive.DEFAULT_RESEARCH_DIR,
        include_watchlist=include_watchlist,
        skip_research=skip_research,
        event_window_days=event_window_days,
    )


def run_sentinel_sync(
    include_watchlist: bool,
    skip_research: bool,
    event_window_days: int,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    fp_before = compute_current_sentinel_fingerprint(include_watchlist, skip_research, event_window_days)
    result = daily_sentinel.run_daily_sentinel(
        portfolio_dir=portfolio_manager.DEFAULT_PORTFOLIO_DIR,
        research_root=research_archive.DEFAULT_RESEARCH_DIR,
        include_watchlist=include_watchlist,
        skip_research=skip_research,
        event_window_days=event_window_days,
    )
    if data_store.is_store_enabled():
        fp_after = compute_current_sentinel_fingerprint(include_watchlist, skip_research, event_window_days)
        # Only tag with current fingerprint if inputs did not mutate mid-flight; otherwise store as audit-only
        stored_fp = fp_before if fp_after == fp_before else f"mutated:{fp_before}"
        try:
            data_store.get_store().record_sentinel_run(
                sanitize(result),
                input_fingerprint=stored_fp,
                include_watchlist=include_watchlist,
                skip_research=skip_research,
                event_window_days=event_window_days,
                task_id=task_id,
            )
        except Exception:
            pass
    return result


class Task:
    def __init__(self, kind: str, params: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "queued"
        self.created_at = utc_iso()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.finished_ns: int = 0
        self.progress: dict[str, Any] = {"done": 0, "total": None}
        self.events: list[dict[str, Any]] = []
        self.result: Any = None
        self.error: str | None = None
        self.cancel_event = threading.Event()
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
        self._persisted_scans: OrderedDict[str, Task] = OrderedDict()
        self._unflushed_scans: OrderedDict[str, Task] = OrderedDict()
        self._background: set[asyncio.Task] = set()
        self._hydrated_path: str | None = None
        self._hydrated_mtime_ns: int | None = None

    # ------------------------------------------------------------------ scan persistence
    @staticmethod
    def _scan_history_path() -> str:
        return os.path.join(portfolio_manager.DEFAULT_PORTFOLIO_DIR, "scan_history.json")

    @staticmethod
    @contextlib.contextmanager
    def _scan_history_file_lock(parent_dir: str):
        os.makedirs(parent_dir, exist_ok=True)
        lock_path = os.path.join(parent_dir, ".scan_history.lock")
        f = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    @staticmethod
    def _record_to_task(rec: dict[str, Any]) -> Task | None:
        tid = str(rec.get("id") or "").strip()
        if not tid:
            return None
        t = Task("scan", rec.get("params") if isinstance(rec.get("params"), dict) else {})
        t.id = tid
        t.status = str(rec.get("status") or "completed")
        t.created_at = str(rec.get("created_at") or utc_iso())
        t.started_at = rec.get("started_at")
        t.finished_at = rec.get("finished_at")
        t.finished_ns = int(rec.get("finished_ns") or 0)
        if isinstance(rec.get("progress"), dict):
            t.progress = dict(rec["progress"])
        t.result = rec.get("result")
        t.events = rec.get("events") if isinstance(rec.get("events"), list) else []
        return t

    @staticmethod
    def _task_to_record(t: Task) -> dict[str, Any]:
        return {
            "id": t.id,
            "kind": "scan",
            "status": t.status,
            "params": sanitize(t.params),
            "created_at": t.created_at,
            "started_at": t.started_at,
            "finished_at": t.finished_at,
            "finished_ns": t.finished_ns,
            "progress": dict(t.progress),
            "result": t.result,
            "events": t.events[-3:],
        }

    def _ensure_hydrated_unlocked(self) -> None:
        path = self._scan_history_path()
        if self._hydrated_path != path:
            self._persisted_scans.clear()
            self._unflushed_scans.clear()
            self._hydrated_path = None
            self._hydrated_mtime_ns = None

        if not os.path.exists(path):
            self._hydrated_path = path
            self._hydrated_mtime_ns = None
            return

        try:
            st = os.stat(path)
            if self._hydrated_path == path and self._hydrated_mtime_ns == st.st_mtime_ns:
                return
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if not isinstance(saved, list):
                return
            by_id: dict[str, Task] = {}
            for rec in saved:
                if not isinstance(rec, dict):
                    continue
                t = self._record_to_task(rec)
                if t is not None:
                    by_id[t.id] = t
            for tid, mem_t in self._unflushed_scans.items():
                by_id[tid] = mem_t
            ordered_tasks = sorted(
                by_id.values(),
                key=lambda item: (item.finished_ns, str(item.finished_at or item.created_at or ""), item.id),
            )[-MAX_PERSISTED_SCANS:]
            self._persisted_scans = OrderedDict((item.id, item) for item in ordered_tasks)
            # Evict any completed scan in _tasks that is no longer within the authoritative latest-10 persisted set
            for stale_tid in [
                tid
                for tid, t in self._tasks.items()
                if t.kind == "scan" and t.status == "completed" and tid not in self._persisted_scans
            ]:
                self._tasks.pop(stale_tid, None)
            self._hydrated_path = path
            self._hydrated_mtime_ns = st.st_mtime_ns
        except (OSError, ValueError, TypeError):
            # Do not mark hydrated on transient read/parse failure so future calls retry
            return

    def _record_completed_scan_unlocked(self, scan_task: Task) -> None:
        path = self._scan_history_path()
        parent = os.path.dirname(path)
        if self._hydrated_path not in (None, path):
            self._persisted_scans.clear()
            self._unflushed_scans.clear()
        new_rec = self._task_to_record(scan_task)

        try:
            with self._scan_history_file_lock(parent):
                merged: dict[str, dict[str, Any]] = {}
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as rf:
                            existing = json.load(rf)
                        if isinstance(existing, list):
                            for item in existing:
                                if isinstance(item, dict) and item.get("id"):
                                    merged[str(item["id"])] = item
                    except (OSError, ValueError, TypeError):
                        pass
                for tid, uf in self._unflushed_scans.items():
                    merged[tid] = self._task_to_record(uf)
                merged[scan_task.id] = new_rec
                ordered_records = sorted(
                    merged.values(),
                    key=lambda r: (
                        int(r.get("finished_ns") or 0),
                        str(r.get("finished_at") or r.get("created_at") or ""),
                        str(r.get("id") or ""),
                    ),
                )[-MAX_PERSISTED_SCANS:]

                fd, tmp = tempfile.mkstemp(prefix=".scan_hist_", suffix=".tmp", dir=parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as wf:
                        json.dump(ordered_records, wf, ensure_ascii=False, allow_nan=False)
                    os.replace(tmp, path)
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
                self._unflushed_scans.clear()
                self._hydrated_path = path
                self._hydrated_mtime_ns = os.stat(path).st_mtime_ns
                synced: OrderedDict[str, Task] = OrderedDict()
                for rec in ordered_records:
                    rid = str(rec["id"])
                    # Prefer disk-merged record unless it is the live task still carrying full in-memory replay events
                    mem_live = self._tasks.get(rid)
                    if mem_live is not None and len(mem_live.events) > len(rec.get("events") or []):
                        t_obj: Task | None = mem_live
                    else:
                        t_obj = self._record_to_task(rec)
                    if t_obj is not None:
                        synced[t_obj.id] = t_obj
                self._persisted_scans = synced
                for stale_tid in [
                    tid
                    for tid, t in self._tasks.items()
                    if t.kind == "scan" and t.status == "completed" and tid not in self._persisted_scans
                ]:
                    self._tasks.pop(stale_tid, None)
        except OSError:
            self._unflushed_scans.pop(scan_task.id, None)
            self._unflushed_scans[scan_task.id] = scan_task
            self._persisted_scans.pop(scan_task.id, None)
            self._persisted_scans[scan_task.id] = scan_task
            while len(self._persisted_scans) > MAX_PERSISTED_SCANS:
                self._persisted_scans.popitem(last=False)

    # ------------------------------------------------------------------ registry
    def _register(self, task: Task) -> None:
        with self._lock:
            self._ensure_hydrated_unlocked()
            while len(self._tasks) >= self.max_tasks:
                victim = next((tid for tid, t in self._tasks.items() if t.terminal), next(iter(self._tasks)))
                self._tasks.pop(victim)
            self._tasks[task.id] = task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            self._ensure_hydrated_unlocked()
            if task_id in self._persisted_scans:
                return self._persisted_scans[task_id]
            t = self._tasks.get(task_id)
            if t is not None and t.kind == "scan" and t.status == "completed":
                return None
            return t

    def detail(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_hydrated_unlocked()
            task = self.get(task_id)
            return task.detail() if task is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_hydrated_unlocked()
            combined: list[Task] = list(self._persisted_scans.values())
            for tid, t in self._tasks.items():
                if t.kind == "scan" and t.status == "completed":
                    continue
                combined.append(t)
            combined.sort(
                key=lambda item: (item.finished_ns, str(item.finished_at or item.created_at or ""), item.id),
                reverse=True,
            )
            return [t.summary() for t in combined]

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
            # Enqueue inside the lock so concurrent emitters strictly preserve [1, 2, ...] seq order!
            for loop, queue in list(task.subscribers):
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

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        """Atomically request cancellation and transition a non-terminal task to ``cancelled``."""
        with self._lock:
            self._ensure_hydrated_unlocked()
            task = self.get(task_id)
            if task is None:
                return None
            if task.terminal:
                return task.summary()
            task.cancel_event.set()
            task.status = "cancelled"
            task.error = "用户已终止该任务"
            task.finished_at = utc_iso()
            task.finished_ns = time.time_ns()
            self._emit(task, "cancelled", {"message": "用户已终止该任务"})
            return task.summary()

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
            if task.terminal:
                return
            task.status = "running"
            task.started_at = utc_iso()
        self._emit(task, "step", {"message": f"{task.kind} started", "stage": "start"})

        def emit(etype: str, data: dict[str, Any]) -> None:
            with self._lock:
                if task.terminal:
                    return
                self._emit(task, etype, data)

        runner_kwargs: dict[str, Any] = {}
        try:
            sig = inspect.signature(runner)
            if "cancel_event" in sig.parameters:
                runner_kwargs["cancel_event"] = task.cancel_event
            if "task_id" in sig.parameters:
                runner_kwargs["task_id"] = task.id
        except (TypeError, ValueError):
            pass

        try:
            result = await asyncio.to_thread(runner, task.params, emit, **runner_kwargs)
        except TaskCancelledError:
            with self._lock:
                if not task.terminal:
                    task.status = "cancelled"
                    task.error = "用户已终止该任务"
                    task.finished_at = utc_iso()
                    task.finished_ns = time.time_ns()
                    self._emit(task, "cancelled", {"message": "用户已终止该任务"})
            return
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            # Status flip and terminal event are one atomic step so late subscribers never miss it.
            with self._lock:
                if task.terminal:
                    return
                task.status = "failed"
                task.error = message
                task.finished_at = utc_iso()
                task.finished_ns = time.time_ns()
                self._emit(task, "error", {"message": message})
            return
        with self._lock:
            if task.terminal:
                return
            task.status = "completed"
            task.result = sanitize(result)
            task.finished_at = utc_iso()
            task.finished_ns = time.time_ns()
            self._emit(task, "done", {"result": result})
            if task.kind == "scan":
                self._record_completed_scan_unlocked(task)
                if data_store.is_store_enabled():
                    try:
                        data_store.get_store().record_scan_run(
                            run_id=task.id,
                            task_id=task.id,
                            created_at_utc=task.created_at,
                            finished_at_utc=task.finished_at,
                            status="completed",
                            universes=task.params.get("universes") or [],
                            symbols=task.params.get("symbols") or [],
                            period=str(task.params.get("period") or "3y"),
                            rows=task.result.get("rows") if isinstance(task.result, dict) else [],
                        )
                    except Exception:
                        pass


# ---------------------------------------------------------------------- runners
def _call_snapshot_for_scan(sym: str, period: str, refresh: bool) -> dict[str, Any]:
    sig = inspect.signature(market_snapshot.get_snapshot)
    kw: dict[str, Any] = {
        "period": period,
        "include_macro": False,
        "include_fundamentals": False,
    }
    if refresh and "refresh" in sig.parameters:
        kw["refresh"] = True
    return market_snapshot.get_snapshot(sym, **kw)


def run_scan(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    targets = [tuple(t) for t in params["targets"]]
    period = params.get("period", "3y")
    sort_mode = params.get("sort", "tier")
    refresh = bool(params.get("refresh", False))
    total = len(targets)
    emit("step", {"message": f"resolved {total} targets", "stage": "resolve", "total": total})
    emit("progress", {"done": 0, "total": total})

    # Partition targets by upstream rate-limit profile:
    # - Sina/AkShare (cn_future / a_share intraday): strictly serialized (1 worker) to prevent throttling
    # - yfinance / crypto: bounded 3-worker pool for fast parallel fetching
    sina_lock = threading.Lock()
    done_lock = threading.Lock()
    done_count = 0
    rows_by_idx: dict[int, dict[str, Any]] = {}

    def _scan_one(idx: int, market: str, sym: str) -> None:
        nonlocal done_count
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelledError("scan cancelled")
        try:
            _, ac = market_snapshot.normalize_symbol(sym)
        except Exception:
            ac = "unknown"

        emit("step", {"message": f"scanning {sym}", "stage": "scan", "symbol": sym, "market": market, "index": idx})
        try:
            if ac == "cn_future":
                with sina_lock:
                    if cancel_event is not None and cancel_event.is_set():
                        raise TaskCancelledError("scan cancelled")
                    snap = _call_snapshot_for_scan(sym, period=period, refresh=refresh)
            else:
                snap = _call_snapshot_for_scan(sym, period=period, refresh=refresh)
            row = scan_opportunities.build_scan_row(market, sym, snap)
        except TaskCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            row = scan_opportunities.build_scan_row(market, sym, {"error": str(exc)})

        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelledError("scan cancelled")

        with done_lock:
            rows_by_idx[idx] = row
            done_count += 1
            cur_done = done_count
        emit("row", {"row": row, "index": idx})
        emit("progress", {"done": cur_done, "total": total, "symbol": sym})

    if total <= 1:
        for idx, (market, sym) in enumerate(targets, start=1):
            _scan_one(idx, market, sym)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, total)) as pool:
            futures = [
                pool.submit(_scan_one, idx, market, sym)
                for idx, (market, sym) in enumerate(targets, start=1)
            ]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

    ordered_rows = [rows_by_idx[i] for i in range(1, total + 1) if i in rows_by_idx]
    sorted_rows = scan_opportunities.sort_scan_rows(ordered_rows, sort_mode=sort_mode)
    errors = sum(1 for r in ordered_rows if "error" in r)
    degraded = sum(1 for r in ordered_rows if r.get("quality_grade") == "degraded")
    return {"rows": sorted_rows, "count": len(ordered_rows), "errors": errors, "degraded": degraded}


def run_research_refresh(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    symbol = params["symbol"]
    market = params.get("market")
    include_macro = bool(params.get("include_macro", True))
    total = 3
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("research_refresh cancelled")
    emit("progress", {"done": 0, "total": total})
    emit("step", {"message": f"collecting research data for {symbol}", "stage": "collect"})
    captured_epoch = CACHE.get_epoch("research")

    rd_sig = inspect.signature(research_data.collect_research_data)
    rd_kw: dict[str, Any] = {"include_macro": include_macro}
    if "refresh" in rd_sig.parameters:
        rd_kw["refresh"] = True
    bundle = research_data.collect_research_data(symbol, **rd_kw)

    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("research_refresh cancelled")

    CACHE.set(
        research_cache_key(symbol, include_macro),
        bundle,
        TTL_RESEARCH_SECONDS,
        expected_epoch=captured_epoch,
    )
    emit("progress", {"done": 1, "total": total})

    emit("step", {"message": "loading research archive", "stage": "archive"})
    try:
        loaded = research_archive.load_archive(symbol, market_override=market, evidence_limit=0)
    except FileNotFoundError:
        emit("progress", {"done": total, "total": total})
        return {
            "symbol": symbol,
            "market": market,
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
        "market": loaded.get("market"),
        "archive_exists": True,
        "revision": state.get("revision"),
        "status": state.get("status"),
        "sources_status": bundle.get("sources_status"),
        "data_health_warnings": warnings,
        "triggers": results,
        "fired_count": sum(1 for t in results if t.get("fired")),
    }


def run_sentinel(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    include_watchlist = bool(params.get("include_watchlist", False))
    skip_research = bool(params.get("skip_research", False))
    window = int(params.get("event_window_days", 14))
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("sentinel cancelled")
    emit("progress", {"done": 0, "total": 1})
    emit("step", {"message": "running holding daily sentinel", "stage": "sentinel"})
    captured_epoch = CACHE.get_epoch("sentinel")
    result = run_sentinel_sync(include_watchlist, skip_research, window, task_id=task_id)
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("sentinel cancelled")
    base_key = sentinel_cache_key(include_watchlist, skip_research, window)
    cache_key = (
        (*base_key, compute_current_sentinel_fingerprint(include_watchlist, skip_research, window))
        if data_store.is_store_enabled()
        else base_key
    )
    CACHE.set(
        cache_key,
        result,
        TTL_SENTINEL_SECONDS,
        expected_epoch=captured_epoch,
        namespace="sentinel",
    )
    emit("progress", {"done": 1, "total": 1})
    return result


RUNNERS: dict[str, Callable[[dict[str, Any], Emit], Any]] = {
    "scan": run_scan,
    "research_refresh": run_research_refresh,
    "sentinel": run_sentinel,
}
