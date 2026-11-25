#!/usr/bin/env python3
"""Layer-4 transactional local portfolio & risk manager with single-sector leader exclusivity.

Manages persistent portfolio state under ``portfolio_dir`` (default: ``<repo>/portfolio``):
  - ``portfolio.db``   (canonical SQLite WAL + ``synchronous=FULL`` single-transaction ledger
                        storing ``account``, ``positions``, ``meta``, and ``journal``)
  - ``account.json``   (atomically exported read-only mirror of account equity & risk caps)
  - ``positions.json`` (atomically exported read-only mirror of revision + positions)
  - ``journal.jsonl``  (atomically exported read-only mirror of append-only audit trail)

Core risk & survival rules enforced atomically under cross-thread + cross-process lock
within a single SQLite ``BEGIN IMMEDIATE ... COMMIT`` transaction:
  1. Single-sector leader exclusivity (单一板块龙头独占制):
     - 0 open in sector -> new sole leader (full sector risk slot).
     - 1 open in sector -> REJECT followers unless either:
         (a) ``rotate_leader`` atomically closes old leader, books realized PnL into
             ledger equity (recomputing drawdown risk scaling), and opens new leader, OR
         (b) explicit ``leader_role='co_leader'`` with non-empty ``co_leader_reason``
             AND combined sector open risk <= single-sector risk cap.
     - >= 2 open in sector -> HARD REJECT.
  2. Strict no-averaging-down & stop consistency: ``add_position_tranche`` rejects adds
     unless strictly on the profit side of BOTH ``avg_entry_price`` AND effective
     ``tactical_stop_30m``, and checks both combined open risk and added-tranche risk.
  3. Strict tightening-only stops: ``trail_stop`` rejects widening stops, optionally checks
     ``current_price`` against already-breached stops, and frees open risk down to 0.0.
  4. Portfolio margin/capital ceiling, cross-currency FX protection, exact quantity
     granularity (``min_order_qty`` / ``lot_step``), drawdown step-down scaling, total
     portfolio open risk cap, and cross-sector macro factor overlap checks.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from trade_planner import (  # noqa: E402
    _BARE_OVERSEAS_FUTURE_RE,
    VALID_DIRECTIONS,
    _require_finite_float,
    get_contract_spec,
    resolve_fx_to_account,
    validate_order_quantity,
)

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
DEFAULT_PORTFOLIO_DIR = os.path.join(ROOT_DIR, "portfolio")
DEFAULT_RESEARCH_DIR = os.path.join(ROOT_DIR, "research")

DEFAULT_DRAWDOWN_STEPS: list[dict[str, float]] = [
    {"drawdown_pct": 5.0, "risk_scale": 0.75},
    {"drawdown_pct": 10.0, "risk_scale": 0.50},
]

VALID_LEADER_ROLES = {"sole_leader", "co_leader"}
VALID_JOURNAL_ACTIONS = {"open", "add_on_profit", "trail_stop", "rotate_leader", "trim", "close", "void"}

_INPROC_LOCKS_GUARD = threading.Lock()
_INPROC_LOCKS: dict[str, threading.RLock] = {}


def _get_inproc_lock(key: str) -> threading.RLock:
    with _INPROC_LOCKS_GUARD:
        lock = _INPROC_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _INPROC_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _portfolio_lock(portfolio_dir: str):
    """Serialize both multi-threaded and multi-process writers for the same portfolio directory."""
    os.makedirs(portfolio_dir, exist_ok=True)
    lock_path = os.path.join(os.path.realpath(portfolio_dir), ".portfolio.lock")
    tlock = _get_inproc_lock(lock_path)
    with tlock, open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_portfolio_dir(portfolio_dir: str | None) -> str:
    return os.path.abspath(portfolio_dir or DEFAULT_PORTFOLIO_DIR)


def _db_path(portfolio_dir: str) -> str:
    return os.path.join(portfolio_dir, "portfolio.db")


def _paths(portfolio_dir: str) -> tuple[str, str, str]:
    return (
        os.path.join(portfolio_dir, "account.json"),
        os.path.join(portfolio_dir, "positions.json"),
        os.path.join(portfolio_dir, "journal.jsonl"),
    )


def _atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _atomic_write_text(path: str, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: str, obj: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


# ---------------------------------------------------------------------------
# SQLite Single-Transaction Store + Read-Only JSON Mirrors
# ---------------------------------------------------------------------------

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            sector_theme TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            action TEXT NOT NULL,
            position_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            request_hash TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    idem_cols = {row[1] for row in conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()}
    if "request_hash" not in idem_cols:
        conn.execute("ALTER TABLE idempotency_keys ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")


def _connect_db(pdir: str) -> sqlite3.Connection:
    os.makedirs(pdir, exist_ok=True)
    conn = sqlite3.connect(_db_path(pdir), isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _db_has_state(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone()
    return row is not None


def _db_read_account(conn: sqlite3.Connection, pdir: str) -> dict[str, Any]:
    row = conn.execute("SELECT payload_json FROM account WHERE id = 1").fetchone()
    if row is None:
        raise FileNotFoundError(
            f"Portfolio not initialized (missing account state in {_db_path(pdir)}). Run init_portfolio first."
        )
    return json.loads(row[0])


def _db_read_positions_doc(conn: sqlite3.Connection, pdir: str) -> dict[str, Any]:
    meta_rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if "revision" not in meta_rows:
        raise FileNotFoundError(
            f"Portfolio not initialized (missing positions metadata in {_db_path(pdir)}). Run init_portfolio first."
        )
    pos_rows = conn.execute("SELECT payload_json FROM positions ORDER BY seq ASC").fetchall()
    return {
        "schema_version": int(meta_rows.get("schema_version", "1")),
        "revision": int(meta_rows["revision"]),
        "positions": [json.loads(r[0]) for r in pos_rows],
        "updated_at_utc": meta_rows.get("updated_at_utc", ""),
    }


def _db_read_journal(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM journal ORDER BY id ASC").fetchall()
    return [json.loads(r[0]) for r in rows]


def _db_upsert_account(conn: sqlite3.Connection, account: dict[str, Any]) -> None:
    payload = json.dumps(account, ensure_ascii=False, allow_nan=False)
    conn.execute(
        """
        INSERT INTO account (id, payload_json, updated_at_utc)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at_utc = excluded.updated_at_utc
        """,
        (payload, str(account.get("updated_at_utc") or "")),
    )


def _db_upsert_position(conn: sqlite3.Connection, pos: dict[str, Any]) -> None:
    payload = json.dumps(pos, ensure_ascii=False, allow_nan=False)
    conn.execute(
        """
        INSERT INTO positions (position_id, status, sector_theme, symbol, payload_json, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(position_id) DO UPDATE SET
            status = excluded.status,
            sector_theme = excluded.sector_theme,
            symbol = excluded.symbol,
            payload_json = excluded.payload_json,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            str(pos["position_id"]),
            str(pos["status"]),
            str(pos["sector_theme"]),
            str(pos["symbol"]),
            payload,
            str(pos.get("updated_at_utc") or ""),
        ),
    )


def _db_update_meta(
    conn: sqlite3.Connection,
    revision: int,
    updated_at_utc: str,
    schema_version: int = 1,
) -> None:
    for k, v in (
        ("schema_version", str(int(schema_version))),
        ("revision", str(int(revision))),
        ("updated_at_utc", str(updated_at_utc)),
    ):
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (k, v),
        )


def _db_insert_journal_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    for rec in records:
        payload = json.dumps(rec, ensure_ascii=False, allow_nan=False)
        conn.execute(
            """
            INSERT INTO journal (timestamp_utc, action, position_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(rec.get("timestamp_utc") or ""),
                str(rec.get("action") or ""),
                str(rec.get("position_id") or ""),
                payload,
            ),
        )


def _maybe_migrate_legacy_json_in_tx(conn: sqlite3.Connection, pdir: str) -> None:
    """If portfolio.db is empty but legacy JSON files exist, import them inside the active transaction."""
    if _db_has_state(conn):
        return
    account_path, positions_path, journal_path = _paths(pdir)
    if not (os.path.exists(account_path) and os.path.exists(positions_path)):
        return
    with open(account_path, "r", encoding="utf-8") as f:
        acc = json.load(f)
    with open(positions_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    _db_upsert_account(conn, acc)
    _db_update_meta(
        conn,
        revision=int(doc.get("revision") or 1),
        updated_at_utc=str(doc.get("updated_at_utc") or acc.get("updated_at_utc") or utc_now_str()),
        schema_version=int(doc.get("schema_version") or 1),
    )
    for pos in doc.get("positions") or []:
        _db_upsert_position(conn, pos)
    if os.path.exists(journal_path):
        legacy_rows: list[dict[str, Any]] = []
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    legacy_rows.append(json.loads(s))
        if legacy_rows:
            _db_insert_journal_records(conn, legacy_rows)


def _normalize_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be a non-empty string when provided")
    return idempotency_key.strip()


def _canonicalize_for_hash(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int, float)):
        fval = float(obj)
        return fval if math.isfinite(fval) else repr(obj)
    if isinstance(obj, Decimal):
        return str(obj.normalize())
    if isinstance(obj, (list, tuple)):
        return [_canonicalize_for_hash(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _canonicalize_for_hash(v) for k, v in obj.items()}
    return str(obj)


def _compute_request_hash(operation: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": _canonicalize_for_hash(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _db_lookup_idempotency(
    conn: sqlite3.Connection,
    key: str | None,
    expected_operation: str,
    expected_request_hash: str = "",
) -> dict[str, Any] | None:
    if key is None:
        return None
    row = conn.execute(
        "SELECT operation, request_hash, result_json FROM idempotency_keys WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    stored_op, stored_hash, stored_json = row
    if stored_op != expected_operation or (
        expected_request_hash and stored_hash != expected_request_hash
    ):
        raise ValueError(
            f"idempotency_key_conflict: key {key!r} already used for operation {stored_op!r} "
            f"with different or unverifiable legacy parameters (requested {expected_operation!r})"
        )
    return json.loads(stored_json)


def _db_store_idempotency(
    conn: sqlite3.Connection,
    key: str | None,
    operation: str,
    request_hash: str,
    result_payload: dict[str, Any],
    now: str,
) -> None:
    if key is None:
        return
    conn.execute(
        """
        INSERT INTO idempotency_keys (key, operation, request_hash, result_json, created_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            key,
            operation,
            request_hash,
            json.dumps(result_payload, ensure_ascii=False, allow_nan=False),
            now,
        ),
    )


def _export_json_mirrors_unlocked(pdir: str) -> None:
    """Export read-only JSON mirrors (account.json, positions.json, journal.jsonl) from one consistent DB snapshot."""
    account_path, positions_path, journal_path = _paths(pdir)
    conn = _connect_db(pdir)
    try:
        conn.execute("BEGIN DEFERRED")
        try:
            account = _db_read_account(conn, pdir)
            positions_doc = _db_read_positions_doc(conn, pdir)
            journal_rows = _db_read_journal(conn)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    finally:
        conn.close()

    _atomic_write_json(account_path, account)
    _atomic_write_json(positions_path, positions_doc)
    journal_text = "".join(
        json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in journal_rows
    )
    _atomic_write_text(journal_path, journal_text)


def _safe_export_mirrors_unlocked(pdir: str) -> tuple[bool, str | None]:
    """Best-effort post-commit mirror export; NEVER re-raises after SQLite COMMIT succeeds."""
    try:
        _export_json_mirrors_unlocked(pdir)
        return True, None
    except Exception as exc:  # noqa: BLE001
        err_msg = f"{type(exc).__name__}: {exc}"
        print(
            f"[portfolio_manager] WARNING: SQLite commit succeeded, but JSON mirror export failed ({err_msg}); "
            "canonical state remains safely committed in portfolio.db. Call sync_mirrors() to resync.",
            file=sys.stderr,
        )
        return False, err_msg


def _resolve_sync_research_dir(
    portfolio_dir: str | None,
    research_dir: str | None,
) -> str | None:
    """Resolve target research directory for archive status sync with strict anti-pollution isolation.

    Only returns a directory when:
      1. ``research_dir`` is explicitly provided and exists on disk, OR
      2. ``portfolio_dir`` resolves to ``DEFAULT_PORTFOLIO_DIR`` AND we are not crossing from a
         non-repo temporary portfolio directory into the real repository ``research/`` directory.
    """
    if research_dir is not None:
        rdir = os.path.abspath(research_dir)
        return rdir if os.path.isdir(rdir) else None

    pdir = _resolve_portfolio_dir(portfolio_dir)
    if os.path.realpath(pdir) != os.path.realpath(DEFAULT_PORTFOLIO_DIR):
        return None

    import research_archive as ra  # lazy import to avoid module-level circular dependency

    candidate = os.path.abspath(ra.DEFAULT_RESEARCH_DIR)
    repo_portfolio = os.path.realpath(os.path.join(ROOT_DIR, "portfolio"))
    repo_research = os.path.realpath(DEFAULT_RESEARCH_DIR)
    if os.path.realpath(candidate) == repo_research and os.path.realpath(pdir) != repo_portfolio:
        return None
    return candidate if os.path.isdir(candidate) else None


_PRICE_PERSIST_DECIMALS = 6
_FX_PERSIST_DECIMALS = 10

_ASSET_CLASS_TO_ARCHIVE_MARKET: dict[str, str] = {
    "us_stock_or_etf": "US",
    "a_share": "A_SHARE",
    "cn_future": "CN_FUTURES",
    "commodity": "COMMODITIES",
    "intl_commodity": "COMMODITIES",
    "fx": "FX",
    "crypto": "CRYPTO",
    "macro": "MACRO",
}


def _asset_class_to_archive_market(asset_class: str | None) -> str | None:
    """Map portfolio position asset_class to canonical research_archive market folder."""
    if not asset_class:
        return None
    return _ASSET_CLASS_TO_ARCHIVE_MARKET.get(str(asset_class).strip().lower())


def _position_archive_key(pos: dict[str, Any]) -> tuple[str | None, str]:
    """Return canonical (archive_market, UPPER_SYMBOL) key for cross-market isolation."""
    return (
        _asset_class_to_archive_market(pos.get("asset_class")),
        str(pos.get("symbol") or "").strip().upper(),
    )


def _sync_research_archive_status(
    symbol: str,
    target_status: str,
    research_dir: str | None = None,
    *,
    portfolio_dir: str | None = None,
    only_if_current_status: str | None = None,
    market_override: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Best-effort atomic sync of an existing research archive's ``state.json`` status.

    Never creates a new empty archive, skips disk writes if already at ``target_status``,
    and NEVER raises so committed SQLite portfolio mutations cannot be disrupted.
    """
    try:
        eff_market = market_override or _asset_class_to_archive_market(asset_class)
        eff_rdir = _resolve_sync_research_dir(portfolio_dir, research_dir)
        if eff_rdir is None:
            return {
                "synced": False,
                "skipped_reason": "research_dir_isolated",
                "symbol": symbol,
                "market": eff_market,
                "target_status": target_status,
            }

        import research_archive as ra  # lazy import

        if target_status not in ra.VALID_STATUSES:
            return {
                "synced": False,
                "symbol": symbol,
                "market": eff_market,
                "target_status": target_status,
                "error": f"Invalid target_status {target_status!r}",
            }

        folder, market, norm_sym, _ = ra.resolve_archive_dir(
            symbol,
            research_root=eff_rdir,
            market_override=eff_market,
        )
        state_path = os.path.join(folder, "state.json")
        if not os.path.exists(state_path):
            return {
                "synced": False,
                "skipped_reason": "archive_not_found",
                "symbol": norm_sym,
                "market": market,
                "target_status": target_status,
            }

        with ra._archive_lock(folder):
            ra._apply_wal_unlocked(folder)
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            cur_status = state.get("status")
            if only_if_current_status is not None and cur_status != only_if_current_status:
                return {
                    "synced": False,
                    "skipped_reason": "status_mismatch",
                    "symbol": norm_sym,
                    "market": market,
                    "previous_status": cur_status,
                    "current_status": cur_status,
                    "target_status": target_status,
                }
            if cur_status == target_status:
                return {
                    "synced": False,
                    "skipped_reason": "already_target_status",
                    "symbol": norm_sym,
                    "market": market,
                    "previous_status": cur_status,
                    "current_status": cur_status,
                    "target_status": target_status,
                }

            state["status"] = target_status
            state["revision"] = int(state.get("revision", 1)) + 1
            state["updated_at_utc"] = ra.utc_now_str()
            ra._atomic_write_json(state_path, state)
            return {
                "synced": True,
                "symbol": norm_sym,
                "market": market,
                "previous_status": cur_status,
                "current_status": target_status,
                "revision": state["revision"],
                "archive_dir": folder,
            }
    except Exception as exc:  # noqa: BLE001
        err_msg = f"{type(exc).__name__}: {exc}"
        print(
            f"[portfolio_manager] WARNING: research archive status sync failed for {symbol!r} -> "
            f"{target_status!r} ({err_msg}); portfolio transaction remains committed.",
            file=sys.stderr,
        )
        return {
            "synced": False,
            "symbol": symbol,
            "market": market_override or _asset_class_to_archive_market(asset_class),
            "target_status": target_status,
            "error": err_msg,
        }


def _reconcile_archive_status_unlocked(
    pdir: str,
    symbol: str,
    asset_class: str | None,
    research_dir: str | None = None,
) -> dict[str, Any]:
    """Reconcile a symbol's archive status against live open positions in the ledger (idempotent replay safe)."""
    eff_market = _asset_class_to_archive_market(asset_class)
    target_key = (eff_market, str(symbol or "").strip().upper())
    doc = _read_positions_doc_unlocked(pdir)
    live_positions: list[dict[str, Any]] = list(doc.get("positions") or [])
    has_live_open = any(
        p.get("status") == "open" and _position_archive_key(p) == target_key
        for p in live_positions
    )
    if has_live_open:
        return _sync_research_archive_status(
            symbol,
            "holding",
            research_dir=research_dir,
            portfolio_dir=pdir,
            market_override=eff_market,
        )
    return _sync_research_archive_status(
        symbol,
        "watchlist",
        research_dir=research_dir,
        portfolio_dir=pdir,
        only_if_current_status="holding",
        market_override=eff_market,
    )


def sync_mirrors(portfolio_dir: str | None = None) -> dict[str, Any]:
    """Explicitly resync account.json, positions.json, and journal.jsonl from canonical portfolio.db."""
    pdir = _resolve_portfolio_dir(portfolio_dir)
    with _portfolio_lock(pdir):
        _export_json_mirrors_unlocked(pdir)
        account = _read_account_unlocked(pdir)
        doc = _read_positions_doc_unlocked(pdir)
        journal_rows = _read_journal_unlocked(pdir)
    return {
        "status": "synced",
        "portfolio_dir": pdir,
        "mirror_sync_ok": True,
        "mirror_sync_error": None,
        "revision": int(doc.get("revision") or 0),
        "positions_count": len(doc.get("positions") or []),
        "journal_count": len(journal_rows),
        "equity": float(account["equity"]),
    }


@contextlib.contextmanager
def _sqlite_write_tx(pdir: str):
    """Open a single SQLite BEGIN IMMEDIATE transaction with automatic ROLLBACK on any failure."""
    conn = _connect_db(pdir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _normalize_tag(raw: str | None) -> str:
    return re.sub(r"\s+", "_", (raw or "").strip().lower())


def _normalize_macro_factors(macro_factors: list[str] | tuple[str, ...] | None) -> list[str]:
    if not macro_factors:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in macro_factors:
        tag = _normalize_tag(str(item))
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _normalize_red_lines(exit_red_lines: list[str] | tuple[str, ...] | None) -> list[str]:
    if not exit_red_lines:
        return []
    return [str(x).strip() for x in exit_red_lines if str(x).strip()]


def _validate_drawdown_steps(steps: list[dict[str, Any]]) -> list[dict[str, float]]:
    if not isinstance(steps, list):
        raise ValueError("drawdown_steps must be a list of {drawdown_pct, risk_scale} dicts")
    cleaned: list[dict[str, float]] = []
    for item in steps:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid drawdown step: {item!r}")
        dd = _require_finite_float(item.get("drawdown_pct"), "drawdown_pct", positive=True)
        rs = _require_finite_float(item.get("risk_scale"), "risk_scale", positive=True)
        if dd >= 100.0:
            raise ValueError(f"drawdown_pct must be < 100, got {dd}")
        if rs > 1.0:
            raise ValueError(f"risk_scale must be <= 1.0, got {rs}")
        cleaned.append({"drawdown_pct": round(dd, 4), "risk_scale": round(rs, 4)})
    cleaned.sort(key=lambda x: x["drawdown_pct"])
    return cleaned


def _compute_drawdown_pct(account: dict[str, Any]) -> float:
    equity = _require_finite_float(account.get("equity"), "equity")
    if equity <= 0:
        return 100.0
    peak = _require_finite_float(account.get("peak_equity", equity), "peak_equity", positive=True)
    if equity >= peak:
        return 0.0
    return round(((peak - equity) / peak) * 100.0, 4)


def effective_risk_scale(account: dict[str, Any]) -> float:
    """Compute effective risk multiplier [0.0, 1.0] after applying account drawdown step-downs."""
    equity = _require_finite_float(account.get("equity"), "equity")
    if equity <= 0:
        return 0.0
    dd_pct = _compute_drawdown_pct(account)
    steps = account.get("drawdown_steps")
    if not steps:
        return 1.0
    scale = 1.0
    for step in sorted(steps, key=lambda x: float(x["drawdown_pct"])):
        threshold = float(step["drawdown_pct"])
        step_scale = float(step["risk_scale"])
        if dd_pct + 1e-12 >= threshold:
            scale = min(scale, step_scale)
    return round(scale, 6)


def _compute_open_risk_amount(
    direction: str,
    avg_entry_price: float,
    stop_price: float,
    quantity: float,
    multiplier: float,
    fx_to_account: float,
) -> float:
    """Compute open risk in account currency. Returns 0.0 once stop reaches breakeven or profit."""
    if direction == "long":
        adverse_dist = max(0.0, avg_entry_price - stop_price)
    else:
        adverse_dist = max(0.0, stop_price - avg_entry_price)
    return round(adverse_dist * float(quantity) * float(multiplier) * float(fx_to_account), 2)


def _compute_locked_profit_amount(
    direction: str,
    avg_entry_price: float,
    stop_price: float,
    quantity: float,
    multiplier: float,
    fx_to_account: float,
) -> float:
    if direction == "long":
        favorable_dist = max(0.0, stop_price - avg_entry_price)
    else:
        favorable_dist = max(0.0, avg_entry_price - stop_price)
    return round(favorable_dist * float(quantity) * float(multiplier) * float(fx_to_account), 2)


def _compute_realized_pnl(
    direction: str,
    avg_entry_price: float,
    exit_price: float,
    quantity: float,
    multiplier: float,
    fx_to_account: float,
) -> float:
    diff = (exit_price - avg_entry_price) if direction == "long" else (avg_entry_price - exit_price)
    return round(diff * float(quantity) * float(multiplier) * float(fx_to_account), 2)


def _generate_position_id(symbol: str, existing_ids: set[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", symbol.upper()).strip("_") or "SYM"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    for _ in range(32):
        suffix = uuid.uuid4().hex[:6].upper()
        cand = f"pos_{day}_{safe}_{suffix}"
        if cand not in existing_ids:
            return cand
    raise RuntimeError("Failed to generate unique position_id")


def _validate_account_bounds(
    *,
    equity: float,
    peak_equity: float,
    default_risk_per_trade_pct: float,
    max_total_open_risk_pct: float,
    max_macro_factor_risk_pct: float,
    drawdown_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Unified bounds validator shared by init_portfolio and update_account."""
    eq = _require_finite_float(equity, "equity", positive=True)
    pk = _require_finite_float(peak_equity, "peak_equity", positive=True)
    pk = max(pk, eq)
    def_risk = _require_finite_float(default_risk_per_trade_pct, "default_risk_per_trade_pct", positive=True)
    if def_risk > 10.0:
        raise ValueError(f"default_risk_per_trade_pct must be <= 10.0, got {def_risk}")
    max_total = _require_finite_float(max_total_open_risk_pct, "max_total_open_risk_pct", positive=True)
    if max_total > 50.0:
        raise ValueError(f"max_total_open_risk_pct must be <= 50.0, got {max_total}")
    max_macro = _require_finite_float(max_macro_factor_risk_pct, "max_macro_factor_risk_pct", positive=True)
    if max_macro > 50.0:
        raise ValueError(f"max_macro_factor_risk_pct must be <= 50.0, got {max_macro}")
    steps = _validate_drawdown_steps(drawdown_steps)
    return {
        "equity": round(eq, 2),
        "peak_equity": round(pk, 2),
        "default_risk_per_trade_pct": round(def_risk, 4),
        "max_total_open_risk_pct": round(max_total, 4),
        "max_macro_factor_risk_pct": round(max_macro, 4),
        "drawdown_steps": steps,
    }


def _position_estimated_margin_account(pos: dict[str, Any]) -> float:
    qty = float(pos["quantity"])
    entry = float(pos["avg_entry_price"])
    mult = float(pos["multiplier"])
    fx_rate = float(pos.get("fx_to_account", 1.0))
    margin_rate = float(pos.get("margin_rate", 1.0))
    return round(qty * entry * mult * fx_rate * margin_rate, 2)


def _read_account_unlocked(pdir: str) -> dict[str, Any]:
    db_file = _db_path(pdir)
    if os.path.exists(db_file):
        conn = _connect_db(pdir)
        try:
            if _db_has_state(conn):
                return _db_read_account(conn, pdir)
        finally:
            conn.close()
    account_path, _, _ = _paths(pdir)
    if not os.path.exists(account_path):
        raise FileNotFoundError(f"Portfolio not initialized (missing {db_file} / {account_path}). Run init_portfolio first.")
    with open(account_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_positions_doc_unlocked(pdir: str) -> dict[str, Any]:
    db_file = _db_path(pdir)
    if os.path.exists(db_file):
        conn = _connect_db(pdir)
        try:
            if _db_has_state(conn):
                return _db_read_positions_doc(conn, pdir)
        finally:
            conn.close()
    _, positions_path, _ = _paths(pdir)
    if not os.path.exists(positions_path):
        raise FileNotFoundError(f"Portfolio not initialized (missing {db_file} / {positions_path}). Run init_portfolio first.")
    with open(positions_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_journal_unlocked(pdir: str) -> list[dict[str, Any]]:
    db_file = _db_path(pdir)
    if os.path.exists(db_file):
        conn = _connect_db(pdir)
        try:
            if _db_has_state(conn):
                return _db_read_journal(conn)
        finally:
            conn.close()
    _, _, journal_path = _paths(pdir)
    if not os.path.exists(journal_path):
        return []
    rows: list[dict[str, Any]] = []
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                rows.append(json.loads(s))
    return rows


def _account_caps(account: dict[str, Any]) -> tuple[float, float, float, float, float]:
    equity = _require_finite_float(account["equity"], "equity")
    dd_pct = _compute_drawdown_pct(account)
    scale = effective_risk_scale(account)
    if equity <= 0:
        return dd_pct, 0.0, 0.0, 0.0, 0.0
    single_sector_cap = round(equity * (float(account["default_risk_per_trade_pct"]) / 100.0) * scale, 2)
    total_open_cap = round(equity * (float(account["max_total_open_risk_pct"]) / 100.0) * scale, 2)
    macro_factor_cap = round(equity * (float(account["max_macro_factor_risk_pct"]) / 100.0) * scale, 2)
    return dd_pct, scale, single_sector_cap, total_open_cap, macro_factor_cap


# ---------------------------------------------------------------------------
# Initialization & Loaders
# ---------------------------------------------------------------------------

def init_portfolio(
    portfolio_dir: str | None = None,
    equity: float = 1_000_000.0,
    base_currency: str = "USD",
    default_risk_per_trade_pct: float = 1.0,
    max_total_open_risk_pct: float = 6.0,
    max_macro_factor_risk_pct: float = 3.0,
    drawdown_steps: list[dict[str, float]] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Initialize portfolio.db and read-only JSON mirrors atomically inside a single SQLite transaction."""
    pdir = _resolve_portfolio_dir(portfolio_dir)
    ccy = (base_currency or "USD").strip().upper()
    if not ccy:
        raise ValueError("base_currency must be non-empty")
    bounds = _validate_account_bounds(
        equity=equity,
        peak_equity=equity,
        default_risk_per_trade_pct=default_risk_per_trade_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_macro_factor_risk_pct=max_macro_factor_risk_pct,
        drawdown_steps=drawdown_steps if drawdown_steps is not None else list(DEFAULT_DRAWDOWN_STEPS),
    )

    with _portfolio_lock(pdir):
        with _sqlite_write_tx(pdir) as conn:
            if not overwrite:
                _maybe_migrate_legacy_json_in_tx(conn, pdir)
                if _db_has_state(conn):
                    existing_acc = _db_read_account(conn, pdir)
                    return {
                        "status": "exists",
                        "portfolio_dir": pdir,
                        "account": existing_acc,
                        "mirror_sync_ok": True,
                        "mirror_sync_error": None,
                    }

            now = utc_now_str()
            account = {
                "schema_version": 1,
                "base_currency": ccy,
                "insolvent": False,
                **bounds,
                "updated_at_utc": now,
            }
            if overwrite:
                conn.execute("DELETE FROM idempotency_keys")
                conn.execute("DELETE FROM journal")
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM account")
                conn.execute("DELETE FROM meta")
            _db_upsert_account(conn, account)
            _db_update_meta(conn, revision=1, updated_at_utc=now, schema_version=1)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        return {
            "status": "created",
            "portfolio_dir": pdir,
            "account": account,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def load_account(portfolio_dir: str | None = None) -> dict[str, Any]:
    pdir = _resolve_portfolio_dir(portfolio_dir)
    with _portfolio_lock(pdir):
        return _read_account_unlocked(pdir)


def update_account(
    portfolio_dir: str | None = None,
    equity: float | None = None,
    peak_equity: float | None = None,
    default_risk_per_trade_pct: float | None = None,
    max_total_open_risk_pct: float | None = None,
    max_macro_factor_risk_pct: float | None = None,
    drawdown_steps: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Update account parameters atomically with full bounds validation shared with init_portfolio."""
    pdir = _resolve_portfolio_dir(portfolio_dir)
    with _portfolio_lock(pdir):
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            acc = _db_read_account(conn, pdir)

            cand_equity = acc["equity"] if equity is None else equity
            if peak_equity is not None:
                cand_peak = peak_equity
            elif equity is not None:
                eq_val = _require_finite_float(equity, "equity", positive=True)
                cand_peak = max(float(acc.get("peak_equity") or 0.0), eq_val)
            else:
                cand_peak = acc.get("peak_equity", acc["equity"])

            bounds = _validate_account_bounds(
                equity=cand_equity,
                peak_equity=cand_peak,
                default_risk_per_trade_pct=(
                    acc["default_risk_per_trade_pct"]
                    if default_risk_per_trade_pct is None
                    else default_risk_per_trade_pct
                ),
                max_total_open_risk_pct=(
                    acc["max_total_open_risk_pct"]
                    if max_total_open_risk_pct is None
                    else max_total_open_risk_pct
                ),
                max_macro_factor_risk_pct=(
                    acc["max_macro_factor_risk_pct"]
                    if max_macro_factor_risk_pct is None
                    else max_macro_factor_risk_pct
                ),
                drawdown_steps=(
                    list(acc.get("drawdown_steps") or []) if drawdown_steps is None else drawdown_steps
                ),
            )
            acc.update(bounds)
            acc["insolvent"] = False
            acc["updated_at_utc"] = utc_now_str()
            _db_upsert_account(conn, acc)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        out = dict(acc)
        out["mirror_sync_ok"] = mirror_ok
        out["mirror_sync_error"] = mirror_err
        return out


def load_positions(portfolio_dir: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    pdir = _resolve_portfolio_dir(portfolio_dir)
    if status is not None and status not in {"open", "closed"}:
        raise ValueError(f"status filter must be 'open', 'closed', or None, got {status!r}")
    with _portfolio_lock(pdir):
        doc = _read_positions_doc_unlocked(pdir)
        positions = list(doc.get("positions") or [])
    if status is not None:
        positions = [p for p in positions if p.get("status") == status]
    return positions


def load_journal(portfolio_dir: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    pdir = _resolve_portfolio_dir(portfolio_dir)
    with _portfolio_lock(pdir):
        rows = _read_journal_unlocked(pdir)
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


# ---------------------------------------------------------------------------
# Single-Sector Leader Slot Admission & Portfolio Risk Checks
# ---------------------------------------------------------------------------

def check_sector_slot_admission(
    positions: list[dict[str, Any]],
    account: dict[str, Any],
    sector_theme: str,
    symbol: str,
    new_risk_amount: float,
    leader_role: str = "sole_leader",
    co_leader_reason: str = "",
    macro_factors: list[str] | tuple[str, ...] | None = None,
    allow_rotate: bool = False,
    enforce_macro_cap: bool = True,
) -> dict[str, Any]:
    """Check single-sector leader exclusivity, shared duopoly cap, total open risk, and macro factor overlap."""
    sector_norm = _normalize_tag(sector_theme)
    if not sector_norm:
        raise ValueError("sector_theme must be a non-empty string")
    if not (symbol or "").strip():
        raise ValueError("symbol must be a non-empty string")
    role = str(leader_role or "sole_leader").strip().lower()
    if role not in VALID_LEADER_ROLES:
        raise ValueError(f"leader_role must be one of {sorted(VALID_LEADER_ROLES)}, got {leader_role!r}")
    new_risk = _require_finite_float(new_risk_amount, "new_risk_amount", non_negative=True)

    spec = get_contract_spec(symbol)
    norm_sym = spec["symbol"].upper()
    norm_factors = _normalize_macro_factors(macro_factors)
    equity_val = _require_finite_float(account["equity"], "equity")
    dd_pct, risk_scale, single_sector_cap, max_total_cap, max_macro_cap = _account_caps(account)

    open_positions = [p for p in positions if p.get("status") == "open"]
    same_sector = [p for p in open_positions if _normalize_tag(p.get("sector_theme")) == sector_norm]
    existing_sector_risk = round(sum(float(p.get("current_open_risk_amount") or 0.0) for p in same_sector), 2)
    remaining_sector_budget = round(max(0.0, single_sector_cap - existing_sector_risk), 2)

    existing_summary = [
        {
            "position_id": p.get("position_id"),
            "symbol": p.get("symbol"),
            "direction": p.get("direction"),
            "leader_role": p.get("leader_role"),
            "current_open_risk_amount": float(p.get("current_open_risk_amount") or 0.0),
        }
        for p in same_sector
    ]

    allowed = True
    mode = "new_sole_leader"
    reason_code = "ok"
    reason = "通过单一板块龙头独占制与组合风控准入"
    slot_risk_share = 1.0
    replaces_position_id: str | None = None
    replaced_risk = 0.0
    violations: list[str] = []
    warnings: list[str] = []

    # 0. Insolvent / non-positive equity guard: forbid opening or rotating into ANY position
    if equity_val <= 0:
        allowed = False
        mode = "rejected"
        reason_code = "account_equity_non_positive"
        reason = (
            f"account_equity_non_positive: 账户权益 ({equity_val:,.2f}) <= 0（已穿仓/资不抵债），"
            "严禁开新仓、加仓或换仓，请先补充保证金并通过 update_account 恢复正权益"
        )
        violations.append(reason_code)

    # 1. Duplicate open symbol check (same symbol must use add_position_tranche on profit)
    if allowed and any(str(p.get("symbol") or "").upper() == norm_sym for p in open_positions):
        allowed = False
        mode = "rejected"
        reason_code = "duplicate_symbol_use_add_tranche"
        reason = f"{norm_sym} 已有在途持仓，请使用 add_position_tranche（仅限浮盈顺势加仓）而非重复开仓"
        violations.append(reason_code)

    # 2. Single-sector leader slot rule
    if allowed:
        if len(same_sector) == 0:
            if new_risk > single_sector_cap + 1e-6:
                allowed = False
                mode = "rejected"
                reason_code = "single_sector_risk_exceeded"
                reason = (
                    f"单板块风险 {new_risk:,.2f} 超过当前单板块风险上限 {single_sector_cap:,.2f} "
                    f"(回撤缩放系数 {risk_scale:.2f})"
                )
                violations.append(reason_code)
            else:
                mode = "new_sole_leader"
                slot_risk_share = 1.0
        elif len(same_sector) == 1:
            existing_pos = same_sector[0]
            if allow_rotate:
                replaces_position_id = str(existing_pos.get("position_id"))
                replaced_risk = float(existing_pos.get("current_open_risk_amount") or 0.0)
                if new_risk > single_sector_cap + 1e-6:
                    allowed = False
                    mode = "rejected"
                    reason_code = "single_sector_risk_exceeded"
                    reason = (
                        f"换仓新龙头风险 {new_risk:,.2f} 超过当前单板块风险上限 {single_sector_cap:,.2f} "
                        f"(回撤缩放系数 {risk_scale:.2f})"
                    )
                    violations.append(reason_code)
                else:
                    mode = "rotate_leader"
                    slot_risk_share = 1.0
                    reason = f"触发板块龙头切换：原子平掉原龙头 {existing_pos.get('symbol')} 并切入新龙头 {norm_sym}"
            elif role == "co_leader" and (co_leader_reason or "").strip():
                combined_sector_risk = round(existing_sector_risk + new_risk, 2)
                if combined_sector_risk > single_sector_cap + 1e-6:
                    allowed = False
                    mode = "rejected"
                    reason_code = "co_leader_sector_risk_exceeded"
                    reason = (
                        f"双寡头并行必须共享同一份板块风险额度：现有风险 {existing_sector_risk:,.2f} + "
                        f"新增 {new_risk:,.2f} = {combined_sector_risk:,.2f} > 上限 {single_sector_cap:,.2f} "
                        f"(剩余板块额度 {remaining_sector_budget:,.2f})"
                    )
                    violations.append(reason_code)
                else:
                    mode = "co_leader_shared_slot"
                    slot_risk_share = round(new_risk / single_sector_cap, 4) if single_sector_cap > 0 else 0.0
                    reason = (
                        f"双寡头例外放行（共享板块风险额度 {combined_sector_risk:,.2f}/{single_sector_cap:,.2f}）："
                        f"{co_leader_reason.strip()}"
                    )
            else:
                allowed = False
                mode = "rejected"
                reason_code = "sector_leader_occupied"
                reason = (
                    "sector_leader_occupied: 单一板块已有龙头持仓，跟风二三线一律不碰不看；"
                    "仅龙头切换或第一第二不明显时例外"
                )
                violations.append(reason_code)
        else:
            allowed = False
            mode = "rejected"
            reason_code = "sector_max_two_co_leaders_reached"
            reason = (
                f"板块 {sector_norm!r} 已达双寡头上限（当前 {len(same_sector)} 只在途持仓），"
                "硬拒绝新增标的，须先平仓腾出名额"
            )
            violations.append(reason_code)

    # 3. Total open risk check across portfolio (net of replaced leader when rotating)
    total_open_before = round(sum(float(p.get("current_open_risk_amount") or 0.0) for p in open_positions), 2)
    total_open_after = round(total_open_before - replaced_risk + new_risk, 2)
    if total_open_after > max_total_cap + 1e-6:
        violations.append("total_open_risk_exceeded")
        if allowed:
            allowed = False
            mode = "rejected"
            reason_code = "total_open_risk_exceeded"
            reason = (
                f"组合总在途风险将达 {total_open_after:,.2f}，超过当前上限 {max_total_cap:,.2f} "
                f"(回撤缩放系数 {risk_scale:.2f})"
            )

    # 4. Cross-sector macro factor overlap check
    macro_exposure: dict[str, dict[str, Any]] = {}
    overexposed_factors: list[str] = []
    for tag in norm_factors:
        before_tag = round(
            sum(
                float(p.get("current_open_risk_amount") or 0.0)
                for p in open_positions
                if p.get("position_id") != replaces_position_id
                and tag in _normalize_macro_factors(p.get("macro_factors"))
            ),
            2,
        )
        after_tag = round(before_tag + new_risk, 2)
        over = after_tag > max_macro_cap + 1e-6
        macro_exposure[tag] = {
            "risk_before": before_tag,
            "risk_after": after_tag,
            "cap": max_macro_cap,
            "overexposed": over,
        }
        if over:
            overexposed_factors.append(tag)

    if overexposed_factors:
        warn_msg = (
            f"macro_factor_overexposed: 跨板块宏大因子 {overexposed_factors} 叠加风险超过上限 {max_macro_cap:,.2f}"
        )
        warnings.append(warn_msg)
        violations.append("macro_factor_overexposed")
        if enforce_macro_cap and allowed:
            allowed = False
            mode = "rejected"
            reason_code = "macro_factor_overexposed"
            reason = warn_msg

    return {
        "allowed": allowed,
        "mode": mode,
        "reason_code": reason_code,
        "reason": reason,
        "sector_theme": sector_norm,
        "symbol": norm_sym,
        "leader_role": role,
        "slot_risk_share": slot_risk_share,
        "replaces_position_id": replaces_position_id,
        "existing_sector_positions": existing_summary,
        "existing_sector_risk_amount": existing_sector_risk,
        "remaining_sector_risk_budget": remaining_sector_budget,
        "single_sector_risk_cap": single_sector_cap,
        "drawdown_pct": dd_pct,
        "risk_scale": risk_scale,
        "total_open_risk_before": total_open_before,
        "total_open_risk_after": total_open_after,
        "max_total_open_risk_cap": max_total_cap,
        "macro_factor_exposure": macro_exposure,
        "violations": violations,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Position Lifecycle Operations (Single SQLite Atomic Transaction Under Lock)
# ---------------------------------------------------------------------------

def _validate_contract_matches_symbol(
    symbol: str,
    contract_symbol: str | None,
    asset_class: str | None,
) -> str:
    """Validate that contract_symbol matches underlying symbol and is not an ambiguous bare future."""
    sym_spec = get_contract_spec(symbol, asset_class=asset_class)
    eff_ac = sym_spec["asset_class"]
    norm_sym = sym_spec["symbol"].upper()
    sym_root = str(sym_spec.get("root") or norm_sym).upper()

    if contract_symbol is None or not str(contract_symbol).strip():
        return sym_spec["symbol"]

    raw_cs = str(contract_symbol).strip()
    raw_cs_upper = raw_cs.upper()

    if _BARE_OVERSEAS_FUTURE_RE.fullmatch(raw_cs_upper):
        raise ValueError(
            f"contract_symbol {raw_cs!r} is a bare overseas futures code without exchange suffix; "
            "specify an exchange-suffixed contract code (e.g. CLZ26.NYM)."
        )

    cs_spec = get_contract_spec(raw_cs)

    if eff_ac in {"us_stock_or_etf", "a_share", "fx", "crypto"}:
        if not cs_spec.get("known_spec") or cs_spec.get("multiplier") is None:
            raise ValueError(
                f"contract_symbol {raw_cs!r} does not match underlying symbol {sym_spec['symbol']!r} "
                "(unknown or ambiguous contract specification)"
            )
        if cs_spec["asset_class"] != eff_ac or cs_spec["symbol"].upper() != norm_sym:
            raise ValueError(
                f"contract_symbol {raw_cs!r} does not match underlying symbol {sym_spec['symbol']!r}"
            )
        return cs_spec["symbol"]

    if eff_ac in {"cn_future", "commodity"}:
        if not cs_spec.get("known_spec") and raw_cs_upper != norm_sym:
            raise ValueError(
                f"contract_symbol {raw_cs!r} has unknown contract specification for underlying {sym_spec['symbol']!r}"
            )
        if cs_spec["asset_class"] != eff_ac:
            raise ValueError(
                f"contract_symbol {raw_cs!r} (asset_class={cs_spec['asset_class']}) "
                f"does not match underlying symbol {sym_spec['symbol']!r} (asset_class={eff_ac})"
            )
        cs_root = str(cs_spec.get("root") or cs_spec["symbol"]).upper()
        if cs_root != sym_root:
            raise ValueError(
                f"contract_symbol {raw_cs!r} (root={cs_root}) "
                f"does not match underlying symbol {sym_spec['symbol']!r} (root={sym_root})"
            )
        return cs_spec["symbol"]

    if cs_spec["symbol"].upper() != norm_sym:
        raise ValueError(
            f"contract_symbol {raw_cs!r} does not match underlying symbol {sym_spec['symbol']!r}"
        )
    return cs_spec["symbol"]


def _build_new_position(
    *,
    symbol: str,
    direction: str,
    sector_theme: str,
    entry_price: float,
    tactical_stop_30m: float,
    quantity: float,
    structural_stop_daily: float | None,
    target_price: float | None,
    leader_role: str,
    co_leader_reason: str,
    macro_factors: list[str] | tuple[str, ...] | None,
    exit_red_lines: list[str] | tuple[str, ...] | None,
    contract_symbol: str | None,
    multiplier: float | None,
    margin_rate: float | None,
    fx_to_account: float | None,
    account_currency: str,
    asset_class: str | None,
    notes: str,
    reason: str,
    existing_ids: set[str],
    now: str,
    allow_equity_margin: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    norm_dir = str(direction or "").strip().lower()
    if norm_dir not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    entry = _require_finite_float(entry_price, "entry_price", positive=True)
    stop_30m = _require_finite_float(tactical_stop_30m, "tactical_stop_30m", positive=True)

    if norm_dir == "long" and not (entry > stop_30m):
        raise ValueError(f"Long open requires entry_price ({entry}) > tactical_stop_30m ({stop_30m})")
    if norm_dir == "short" and not (entry < stop_30m):
        raise ValueError(f"Short open requires entry_price ({entry}) < tactical_stop_30m ({stop_30m})")

    stop_daily: float | None = None
    if structural_stop_daily is not None:
        stop_daily = _require_finite_float(structural_stop_daily, "structural_stop_daily", positive=True)
        if norm_dir == "long" and not (stop_daily < entry):
            raise ValueError(f"Long structural_stop_daily ({stop_daily}) must be < entry_price ({entry})")
        if norm_dir == "short" and not (stop_daily > entry):
            raise ValueError(f"Short structural_stop_daily ({stop_daily}) must be > entry_price ({entry})")

    target: float | None = None
    if target_price is not None:
        target = _require_finite_float(target_price, "target_price", positive=True)
        if norm_dir == "long" and not (target > entry):
            raise ValueError(f"Long target_price ({target}) must be > entry_price ({entry})")
        if norm_dir == "short" and not (target < entry):
            raise ValueError(f"Short target_price ({target}) must be < entry_price ({entry})")

    spec = get_contract_spec(symbol, asset_class=asset_class)
    if multiplier is not None:
        eff_mult = _require_finite_float(multiplier, "multiplier", positive=True)
    else:
        if not spec.get("known_spec") or spec.get("multiplier") is None:
            raise ValueError(
                f"Unknown contract specification for {symbol!r}; provide exchange suffix or explicit multiplier."
            )
        eff_mult = float(spec["multiplier"])

    # Validate contract_symbol matches underlying symbol (rejects QQQ on PWR, AL2612 on CU0, bare CLZ26, etc.)
    validated_contract_sym = _validate_contract_matches_symbol(symbol, contract_symbol, asset_class)

    if margin_rate is not None:
        eff_margin = _require_finite_float(margin_rate, "margin_rate", positive=True)
        if eff_margin > 1.0:
            raise ValueError("margin_rate must be <= 1.0")
    else:
        eff_margin = float(spec["margin_rate"]) if spec.get("margin_rate") is not None else 1.0

    # Enforce cash-equity margin floor (100% cash) unless caller explicitly opts in with allow_equity_margin=True
    if spec["asset_class"] in {"us_stock_or_etf", "a_share"} and eff_margin < 1.0 and not allow_equity_margin:
        raise ValueError(
            f"equity_margin_forbidden: 现金股票 {spec['symbol']!r} (asset_class={spec['asset_class']}) "
            f"要求 margin_rate >= 1.0 (收到 {eff_margin})，严禁在现金股票上隐式加融资杠杆；"
            "若确需融资保证金请显式传 allow_equity_margin=True"
        )

    # Cross-currency FX protection
    fx_rate = round(resolve_fx_to_account(spec, account_currency, fx_to_account), _FX_PERSIST_DECIMALS)
    quote_ccy = str(spec.get("quote_currency") or spec.get("currency") or "").strip().upper()

    # Exact quantity granularity check (min_order_qty + k * lot_step)
    clean_qty = validate_order_quantity(quantity, spec, field_name="quantity", enforce_min_order_qty=True)

    sector_norm = _normalize_tag(sector_theme)
    if not sector_norm:
        raise ValueError("sector_theme must be non-empty")

    role = str(leader_role or "sole_leader").strip().lower()
    if role not in VALID_LEADER_ROLES:
        raise ValueError(f"leader_role must be one of {sorted(VALID_LEADER_ROLES)}, got {leader_role!r}")

    norm_factors = _normalize_macro_factors(macro_factors)
    red_lines = _normalize_red_lines(exit_red_lines)
    warnings: list[str] = []
    if not red_lines:
        warnings.append("missing_exit_red_lines: 建议为每笔持仓绑定明确的日线/30m与基本面撤离红线触发器")

    init_risk = _compute_open_risk_amount(norm_dir, entry, stop_30m, clean_qty, eff_mult, fx_rate)
    est_margin = round(float(clean_qty) * entry * eff_mult * fx_rate * eff_margin, 2)

    pos_id = _generate_position_id(spec["symbol"], existing_ids)
    pos = {
        "position_id": pos_id,
        "symbol": spec["symbol"],
        "raw_symbol": symbol,
        "asset_class": spec["asset_class"],
        "quote_currency": quote_ccy,
        "direction": norm_dir,
        "status": "open",
        "sector_theme": sector_norm,
        "leader_role": role,
        "co_leader_reason": (co_leader_reason or "").strip(),
        "macro_factors": norm_factors,
        "contract_symbol": validated_contract_sym,
        "multiplier": eff_mult,
        "margin_rate": eff_margin,
        "fx_to_account": fx_rate,
        "estimated_margin_account": est_margin,
        "quantity": clean_qty,
        "initial_quantity": clean_qty,
        "avg_entry_price": round(entry, 6),
        "initial_entry_price": round(entry, 6),
        "tactical_stop_30m": round(stop_30m, 6),
        "initial_stop_30m": round(stop_30m, 6),
        "stop_updated_at_utc": now,
        "structural_stop_daily": round(stop_daily, 6) if stop_daily is not None else None,
        "structural_stop_updated_at_utc": now if stop_daily is not None else None,
        "target_price": round(target, 6) if target is not None else None,
        "initial_risk_amount": init_risk,
        "current_open_risk_amount": init_risk,
        "locked_profit_amount": 0.0,
        "slot_risk_share": 1.0,
        "exit_red_lines": red_lines,
        "tranches": [
            {
                "tranche_index": 1,
                "timestamp_utc": now,
                "price": round(entry, 6),
                "quantity": clean_qty,
                "reason": (reason or "initial_entry").strip(),
            }
        ],
        "partial_realized_pnl": 0.0,
        "notes": (notes or "").strip(),
        "opened_at_utc": now,
        "updated_at_utc": now,
        "closed_at_utc": None,
        "exit_price": None,
        "exit_reason": None,
        "exit_category": None,
        "realized_pnl": None,
        "r_multiple": None,
    }
    return pos, warnings


def open_position(
    symbol: str,
    direction: str,
    sector_theme: str,
    entry_price: float,
    tactical_stop_30m: float,
    quantity: float,
    structural_stop_daily: float | None = None,
    target_price: float | None = None,
    leader_role: str = "sole_leader",
    co_leader_reason: str = "",
    macro_factors: list[str] | tuple[str, ...] | None = None,
    exit_red_lines: list[str] | tuple[str, ...] | None = None,
    contract_symbol: str | None = None,
    multiplier: float | None = None,
    margin_rate: float | None = None,
    fx_to_account: float | None = None,
    asset_class: str | None = None,
    notes: str = "",
    reason: str = "initial_entry",
    portfolio_dir: str | None = None,
    enforce_macro_cap: bool = True,
    allow_equity_margin: bool = False,
    idempotency_key: str | None = None,
    research_dir: str | None = None,
) -> dict[str, Any]:
    """Open a new position atomically after enforcing exclusivity, granularity, FX, capital, and risk caps."""
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "open_position",
            {
                "symbol": symbol,
                "direction": direction,
                "sector_theme": sector_theme,
                "entry_price": entry_price,
                "tactical_stop_30m": tactical_stop_30m,
                "quantity": quantity,
                "structural_stop_daily": structural_stop_daily,
                "target_price": target_price,
                "leader_role": leader_role,
                "co_leader_reason": co_leader_reason,
                "macro_factors": list(macro_factors) if macro_factors is not None else None,
                "exit_red_lines": list(exit_red_lines) if exit_red_lines is not None else None,
                "contract_symbol": contract_symbol,
                "multiplier": multiplier,
                "margin_rate": margin_rate,
                "fx_to_account": fx_to_account,
                "asset_class": asset_class,
                "notes": notes,
                "reason": reason,
                "enforce_macro_cap": enforce_macro_cap,
                "allow_equity_margin": allow_equity_margin,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "open_position", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                account = _db_read_account(conn, pdir)
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                existing_ids = {str(p.get("position_id")) for p in positions}
                now = utc_now_str()

                pos, warnings = _build_new_position(
                    symbol=symbol,
                    direction=direction,
                    sector_theme=sector_theme,
                    entry_price=entry_price,
                    tactical_stop_30m=tactical_stop_30m,
                    quantity=quantity,
                    structural_stop_daily=structural_stop_daily,
                    target_price=target_price,
                    leader_role=leader_role,
                    co_leader_reason=co_leader_reason,
                    macro_factors=macro_factors,
                    exit_red_lines=exit_red_lines,
                    contract_symbol=contract_symbol,
                    multiplier=multiplier,
                    margin_rate=margin_rate,
                    fx_to_account=fx_to_account,
                    account_currency=str(account["base_currency"]),
                    asset_class=asset_class,
                    notes=notes,
                    reason=reason,
                    existing_ids=existing_ids,
                    now=now,
                    allow_equity_margin=allow_equity_margin,
                )

                admission = check_sector_slot_admission(
                    positions=positions,
                    account=account,
                    sector_theme=pos["sector_theme"],
                    symbol=pos["symbol"],
                    new_risk_amount=pos["initial_risk_amount"],
                    leader_role=pos["leader_role"],
                    co_leader_reason=pos["co_leader_reason"],
                    macro_factors=pos["macro_factors"],
                    allow_rotate=False,
                    enforce_macro_cap=enforce_macro_cap,
                )
                if not admission["allowed"]:
                    raise ValueError(f"{admission['reason_code']}: {admission['reason']}")

                # Portfolio margin / capital ceiling check
                open_positions = [p for p in positions if p.get("status") == "open"]
                existing_margin = round(sum(_position_estimated_margin_account(p) for p in open_positions), 2)
                new_margin = _position_estimated_margin_account(pos)
                total_margin_after = round(existing_margin + new_margin, 2)
                equity_val = float(account["equity"])
                if total_margin_after > equity_val + 1e-6:
                    raise ValueError(
                        f"portfolio_capital_limit_exceeded: 开仓后组合预计保证金/资金占用 "
                        f"{total_margin_after:,.2f} 超过账户权益 {equity_val:,.2f}"
                    )

                pos["slot_risk_share"] = admission["slot_risk_share"]
                warnings.extend(admission.get("warnings") or [])

                new_revision = int(doc.get("revision") or 0) + 1
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "open",
                    "position_id": pos["position_id"],
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": pos["direction"],
                    "leader_role": pos["leader_role"],
                    "price": pos["avg_entry_price"],
                    "stop_30m": pos["tactical_stop_30m"],
                    "quantity": pos["quantity"],
                    "risk_delta": pos["initial_risk_amount"],
                    "reason": (reason or "initial_entry").strip(),
                }

                result_payload = {
                    "status": "opened",
                    "revision": new_revision,
                    "position": pos,
                    "admission": admission,
                    "warnings": warnings,
                }
                _db_upsert_position(conn, pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "open_position", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            replay_pos = replayed_result.get("position") or {}
            archive_sync = _reconcile_archive_status_unlocked(
                pdir,
                str(replay_pos.get("symbol") or ""),
                replay_pos.get("asset_class"),
                research_dir=research_dir,
            )
            out_warnings = list(replayed_result.get("warnings") or [])
            if archive_sync.get("error"):
                out_warnings.append(f"archive_status_sync_failed: {archive_sync['error']}")
            return {
                **replayed_result,
                "warnings": out_warnings,
                "archive_status_sync": archive_sync,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        opened_pos = result_payload["position"]
        archive_sync = _sync_research_archive_status(
            opened_pos["symbol"],
            "holding",
            research_dir=research_dir,
            portfolio_dir=pdir,
            market_override=_asset_class_to_archive_market(opened_pos.get("asset_class")),
        )
        out_warnings = list(result_payload.get("warnings") or [])
        if archive_sync.get("error"):
            out_warnings.append(f"archive_status_sync_failed: {archive_sync['error']}")
        return {
            **result_payload,
            "warnings": out_warnings,
            "archive_status_sync": archive_sync,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def rotate_leader(
    old_position_id: str,
    exit_price: float,
    exit_reason: str,
    new_position_kwargs: dict[str, Any],
    exit_category: str = "leader_rotation",
    portfolio_dir: str | None = None,
    enforce_macro_cap: bool = True,
    idempotency_key: str | None = None,
    research_dir: str | None = None,
) -> dict[str, Any]:
    """Atomically close outgoing leader, update realized ledger equity, and admit/open new leader in ONE SQLite tx."""
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "rotate_leader",
            {
                "old_position_id": old_position_id,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "new_position_kwargs": new_position_kwargs,
                "exit_category": exit_category,
                "enforce_macro_cap": enforce_macro_cap,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        old_has_other_open = False
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "rotate_leader", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                if not isinstance(new_position_kwargs, dict):
                    raise ValueError("new_position_kwargs must be a dict")
                ex_price = _require_finite_float(exit_price, "exit_price", positive=True)
                ex_reason = (exit_reason or "").strip()
                if not ex_reason:
                    raise ValueError("exit_reason must be non-empty")

                account = _db_read_account(conn, pdir)
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                target_idx = next(
                    (i for i, p in enumerate(positions) if p.get("position_id") == old_position_id),
                    None,
                )
                if target_idx is None:
                    raise ValueError(f"Position not found: {old_position_id}")
                old_pos = dict(positions[target_idx])
                if old_pos.get("status") != "open":
                    raise ValueError(f"Position {old_position_id} is not open (status={old_pos.get('status')})")

                kwargs = dict(new_position_kwargs)
                kwargs.setdefault("sector_theme", old_pos["sector_theme"])
                if _normalize_tag(kwargs["sector_theme"]) != _normalize_tag(old_pos["sector_theme"]):
                    raise ValueError(
                        f"rotate_leader requires same sector_theme: old={old_pos['sector_theme']!r}, "
                        f"new={kwargs['sector_theme']!r}"
                    )
                kwargs["leader_role"] = "sole_leader"
                kwargs.setdefault("co_leader_reason", "")
                kwargs.setdefault("reason", f"rotate_from:{old_pos['symbol']}")

                required_new = ("symbol", "direction", "entry_price", "tactical_stop_30m", "quantity")
                missing = [k for k in required_new if k not in kwargs or kwargs[k] is None]
                if missing:
                    raise ValueError(f"new_position_kwargs missing required keys: {missing}")

                existing_ids = {str(p.get("position_id")) for p in positions}
                now = utc_now_str()

                # Step 1: Compute outgoing leader's realized PnL & update ledger equity BEFORE new leader admission
                old_open_risk = float(old_pos.get("current_open_risk_amount") or 0.0)
                rem_pnl = _compute_realized_pnl(
                    old_pos["direction"],
                    float(old_pos["avg_entry_price"]),
                    ex_price,
                    float(old_pos["quantity"]),
                    float(old_pos["multiplier"]),
                    float(old_pos.get("fx_to_account", 1.0)),
                )
                total_pnl = round(float(old_pos.get("partial_realized_pnl") or 0.0) + rem_pnl, 2)
                init_risk_old = float(old_pos.get("initial_risk_amount") or 0.0)
                r_mult = round(total_pnl / init_risk_old, 4) if init_risk_old > 0 else None

                post_equity = round(float(account["equity"]) + rem_pnl, 2)
                post_insolvent = post_equity <= 0
                post_account = dict(account)
                post_account["equity"] = post_equity
                post_account["insolvent"] = post_insolvent
                prev_peak = account.get("peak_equity")
                post_account["peak_equity"] = (
                    round(max(float(prev_peak), post_equity), 2) if prev_peak is not None else post_equity
                )
                post_account["updated_at_utc"] = now

                # Step 2: Build and validate incoming leader (granularity, contract identity, margin floor, FX)
                new_pos, warnings = _build_new_position(
                    symbol=kwargs["symbol"],
                    direction=kwargs["direction"],
                    sector_theme=kwargs["sector_theme"],
                    entry_price=kwargs["entry_price"],
                    tactical_stop_30m=kwargs["tactical_stop_30m"],
                    quantity=kwargs["quantity"],
                    structural_stop_daily=kwargs.get("structural_stop_daily"),
                    target_price=kwargs.get("target_price"),
                    leader_role="sole_leader",
                    co_leader_reason="",
                    macro_factors=kwargs.get("macro_factors"),
                    exit_red_lines=kwargs.get("exit_red_lines"),
                    contract_symbol=kwargs.get("contract_symbol"),
                    multiplier=kwargs.get("multiplier"),
                    margin_rate=kwargs.get("margin_rate"),
                    fx_to_account=kwargs.get("fx_to_account", None),
                    account_currency=str(post_account["base_currency"]),
                    asset_class=kwargs.get("asset_class"),
                    notes=kwargs.get("notes", ""),
                    reason=kwargs["reason"],
                    existing_ids=existing_ids,
                    now=now,
                    allow_equity_margin=bool(kwargs.get("allow_equity_margin", False)),
                )

                if new_pos["symbol"].upper() == str(old_pos["symbol"]).upper():
                    raise ValueError(
                        f"rotate_leader target symbol {new_pos['symbol']} matches outgoing leader; "
                        "use add_position_tranche or trail_stop instead."
                    )

                # Step 3: Run risk admission using POST-LOSS equity & recomputed drawdown risk_scale
                admission = check_sector_slot_admission(
                    positions=positions,
                    account=post_account,
                    sector_theme=new_pos["sector_theme"],
                    symbol=new_pos["symbol"],
                    new_risk_amount=new_pos["initial_risk_amount"],
                    leader_role="sole_leader",
                    co_leader_reason="",
                    macro_factors=new_pos["macro_factors"],
                    allow_rotate=True,
                    enforce_macro_cap=enforce_macro_cap,
                )
                if not admission["allowed"]:
                    raise ValueError(f"{admission['reason_code']}: {admission['reason']}")
                if admission.get("replaces_position_id") != old_position_id:
                    raise ValueError(
                        f"Rotation mismatch: expected to replace {old_position_id}, "
                        f"found {admission.get('replaces_position_id')}"
                    )

                # Step 4: Portfolio margin/capital ceiling after netting out replaced leader
                other_open = [
                    p for p in positions if p.get("status") == "open" and p.get("position_id") != old_position_id
                ]
                other_margin = round(sum(_position_estimated_margin_account(p) for p in other_open), 2)
                new_margin = _position_estimated_margin_account(new_pos)
                total_margin_after = round(other_margin + new_margin, 2)
                if total_margin_after > post_equity + 1e-6:
                    raise ValueError(
                        f"portfolio_capital_limit_exceeded: 换仓后组合预计保证金/资金占用 "
                        f"{total_margin_after:,.2f} 超过平仓后账户权益 {post_equity:,.2f}"
                    )

                # Step 5: Mutate old_pos, new_pos, post_account, meta, and journal inside ONE atomic SQLite tx
                old_pos["status"] = "closed"
                old_pos["current_open_risk_amount"] = 0.0
                old_pos["estimated_margin_account"] = 0.0
                old_pos["exit_price"] = round(ex_price, 6)
                old_pos["exit_reason"] = ex_reason
                old_pos["exit_category"] = (exit_category or "leader_rotation").strip()
                old_pos["realized_pnl"] = total_pnl
                old_pos["r_multiple"] = r_mult
                old_pos["closed_at_utc"] = now
                old_pos["updated_at_utc"] = now

                rotation_id = f"rot_{uuid.uuid4().hex[:8].upper()}"
                old_pos["rotated_to_position_id"] = new_pos["position_id"]
                new_pos["rotated_from_position_id"] = old_position_id
                new_pos["slot_risk_share"] = admission["slot_risk_share"]
                warnings.extend(admission.get("warnings") or [])

                new_revision = int(doc.get("revision") or 0) + 1
                journal_pair = [
                    {
                        "timestamp_utc": now,
                        "action": "rotate_leader",
                        "leg": "close_old_leader",
                        "rotation_id": rotation_id,
                        "position_id": old_pos["position_id"],
                        "symbol": old_pos["symbol"],
                        "sector_theme": old_pos["sector_theme"],
                        "direction": old_pos["direction"],
                        "price": old_pos["exit_price"],
                        "quantity": old_pos["quantity"],
                        "realized_pnl": total_pnl,
                        "equity_after": post_equity,
                        "r_multiple": r_mult,
                        "risk_delta": round(-old_open_risk, 2),
                        "reason": ex_reason,
                    },
                    {
                        "timestamp_utc": now,
                        "action": "rotate_leader",
                        "leg": "open_new_leader",
                        "rotation_id": rotation_id,
                        "position_id": new_pos["position_id"],
                        "symbol": new_pos["symbol"],
                        "sector_theme": new_pos["sector_theme"],
                        "direction": new_pos["direction"],
                        "price": new_pos["avg_entry_price"],
                        "stop_30m": new_pos["tactical_stop_30m"],
                        "quantity": new_pos["quantity"],
                        "risk_delta": new_pos["initial_risk_amount"],
                        "reason": new_pos["tranches"][0]["reason"],
                    },
                ]

                old_key = _position_archive_key(old_pos)
                old_has_other_open = any(
                    p.get("status") == "open"
                    and p.get("position_id") != old_position_id
                    and _position_archive_key(p) == old_key
                    for p in positions
                )

                result_payload = {
                    "status": "rotated",
                    "rotation_id": rotation_id,
                    "revision": new_revision,
                    "account": post_account,
                    "account_insolvent": post_insolvent,
                    "closed_position": old_pos,
                    "opened_position": new_pos,
                    "admission": admission,
                    "warnings": warnings,
                }
                _db_upsert_account(conn, post_account)
                _db_upsert_position(conn, old_pos)
                _db_upsert_position(conn, new_pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, journal_pair)
                _db_store_idempotency(conn, idem_key, "rotate_leader", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            replay_closed = replayed_result.get("closed_position") or {}
            replay_opened = replayed_result.get("opened_position") or {}
            old_sync = _reconcile_archive_status_unlocked(
                pdir,
                str(replay_closed.get("symbol") or ""),
                replay_closed.get("asset_class"),
                research_dir=research_dir,
            )
            new_sync = _reconcile_archive_status_unlocked(
                pdir,
                str(replay_opened.get("symbol") or ""),
                replay_opened.get("asset_class"),
                research_dir=research_dir,
            )
            out_warnings = list(replayed_result.get("warnings") or [])
            for sync_item in (old_sync, new_sync):
                if sync_item.get("error"):
                    out_warnings.append(
                        f"archive_status_sync_failed ({sync_item.get('symbol')}): {sync_item['error']}"
                    )
            return {
                **replayed_result,
                "warnings": out_warnings,
                "archive_status_sync": {
                    "closed_symbol": old_sync,
                    "opened_symbol": new_sync,
                },
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        closed_pos_res = result_payload["closed_position"]
        opened_pos_res = result_payload["opened_position"]
        closed_sym = closed_pos_res["symbol"]
        opened_sym = opened_pos_res["symbol"]
        closed_market = _asset_class_to_archive_market(closed_pos_res.get("asset_class"))
        opened_market = _asset_class_to_archive_market(opened_pos_res.get("asset_class"))
        if not old_has_other_open:
            old_sync = _sync_research_archive_status(
                closed_sym,
                "watchlist",
                research_dir=research_dir,
                portfolio_dir=pdir,
                only_if_current_status="holding",
                market_override=closed_market,
            )
        else:
            old_sync = {
                "synced": False,
                "skipped_reason": "other_open_position_exists",
                "symbol": closed_sym,
                "market": closed_market,
                "target_status": "watchlist",
            }
        new_sync = _sync_research_archive_status(
            opened_sym,
            "holding",
            research_dir=research_dir,
            portfolio_dir=pdir,
            market_override=opened_market,
        )
        out_warnings = list(result_payload.get("warnings") or [])
        for sync_item in (old_sync, new_sync):
            if sync_item.get("error"):
                out_warnings.append(f"archive_status_sync_failed ({sync_item.get('symbol')}): {sync_item['error']}")
        return {
            **result_payload,
            "warnings": out_warnings,
            "archive_status_sync": {
                "closed_symbol": old_sync,
                "opened_symbol": new_sync,
            },
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def add_position_tranche(
    position_id: str,
    add_price: float,
    add_quantity: float,
    new_stop_30m: float | None = None,
    reason: str = "profit_pyramid",
    portfolio_dir: str | None = None,
    enforce_macro_cap: bool = True,
    idempotency_key: str | None = None,
    fx_to_account: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Add a pyramid tranche ONLY on confirmed floating profit, strictly on the profit side of avg_entry,
    effective tactical_stop_30m, AND effective structural_stop_daily (if set).
    """
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "add_position_tranche",
            {
                "position_id": position_id,
                "add_price": add_price,
                "add_quantity": add_quantity,
                "new_stop_30m": new_stop_30m,
                "reason": reason,
                "enforce_macro_cap": enforce_macro_cap,
                "fx_to_account": fx_to_account,
                "note": note,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "add_position_tranche", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                price = round(_require_finite_float(add_price, "add_price", positive=True), _PRICE_PERSIST_DECIMALS)
                if price <= 0:
                    raise ValueError(
                        f"add_price ({add_price}) rounds to <= 0 at {_PRICE_PERSIST_DECIMALS}-decimal precision"
                    )
                account = _db_read_account(conn, pdir)
                equity_val = float(account["equity"])
                if equity_val <= 0:
                    raise ValueError(
                        f"account_equity_non_positive: 账户权益 ({equity_val:,.2f}) <= 0（已穿仓/资不抵债），严禁加仓"
                    )
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                idx = next((i for i, p in enumerate(positions) if p.get("position_id") == position_id), None)
                if idx is None:
                    raise ValueError(f"Position not found: {position_id}")
                pos = dict(positions[idx])
                if pos.get("status") != "open":
                    raise ValueError(f"Position {position_id} is not open (status={pos.get('status')})")

                spec = get_contract_spec(pos["symbol"], asset_class=pos.get("asset_class"))
                clean_add_qty = validate_order_quantity(
                    add_quantity, spec, field_name="add_quantity", enforce_min_order_qty=True
                )
                qty_add = float(clean_add_qty)

                direction = pos["direction"]
                old_avg = round(float(pos["avg_entry_price"]), _PRICE_PERSIST_DECIMALS)
                old_qty_orig = pos["quantity"]
                old_qty = float(old_qty_orig)
                old_stop = round(float(pos["tactical_stop_30m"]), _PRICE_PERSIST_DECIMALS)
                old_risk = float(pos.get("current_open_risk_amount") or 0.0)
                mult = float(pos["multiplier"])
                if fx_to_account is not None:
                    fx_rate = round(
                        resolve_fx_to_account(spec, str(account["base_currency"]), fx_to_account),
                        _FX_PERSIST_DECIMALS,
                    )
                else:
                    fx_rate = round(float(pos.get("fx_to_account", 1.0)), _FX_PERSIST_DECIMALS)

                # HARD RULE 1: Strictly forbid floating-loss or breakeven averaging down
                if direction == "long" and not (price > old_avg):
                    raise ValueError(
                        f"floating_loss_add_forbidden: 纪律禁止浮亏或平价摊平：多头加仓价 ({price}) "
                        f"必须严格高于当前均价 ({old_avg})，仅允许浮盈确认后顺势加仓"
                    )
                if direction == "short" and not (price < old_avg):
                    raise ValueError(
                        f"floating_loss_add_forbidden: 纪律禁止浮亏或平价摊平：空头加仓价 ({price}) "
                        f"必须严格低于当前均价 ({old_avg})，仅允许浮盈确认后顺势加仓"
                    )

                eff_stop = old_stop
                if new_stop_30m is not None:
                    cand_stop = round(
                        _require_finite_float(new_stop_30m, "new_stop_30m", positive=True),
                        _PRICE_PERSIST_DECIMALS,
                    )
                    if cand_stop <= 0:
                        raise ValueError(
                            f"new_stop_30m ({new_stop_30m}) rounds to <= 0 at {_PRICE_PERSIST_DECIMALS}-decimal precision"
                        )
                    if direction == "long" and cand_stop < old_stop:
                        raise ValueError(
                            f"stop_widening_forbidden: 移动止损只紧不松：加仓时多头止损 ({cand_stop}) 不得低于原止损 ({old_stop})"
                        )
                    if direction == "short" and cand_stop > old_stop:
                        raise ValueError(
                            f"stop_widening_forbidden: 移动止损只紧不松：加仓时空头止损 ({cand_stop}) 不得高于原止损 ({old_stop})"
                        )
                    eff_stop = cand_stop

                # HARD RULE 2a: Stop consistency — add_price must be strictly on profit side of effective tactical_stop_30m!
                if direction == "long" and not (price > eff_stop):
                    raise ValueError(
                        f"stop_consistency_violation: 多头加仓价 ({price}) 必须严格高于有效 30min 止损 ({eff_stop})，"
                        "禁止跨过已上移止损位加仓"
                    )
                if direction == "short" and not (price < eff_stop):
                    raise ValueError(
                        f"stop_consistency_violation: 空头加仓价 ({price}) 必须严格低于有效 30min 止损 ({eff_stop})，"
                        "禁止跨过已下移止损位加仓"
                    )

                # HARD RULE 2b: Stop consistency — add_price must ALSO be strictly on profit side of structural_stop_daily!
                eff_daily_raw = pos.get("structural_stop_daily")
                if eff_daily_raw is not None:
                    eff_daily_stop = float(eff_daily_raw)
                    if direction == "long" and not (price > eff_daily_stop):
                        raise ValueError(
                            f"stop_consistency_violation: add_price crosses structural_stop_daily — "
                            f"多头加仓价 ({price}) 必须严格高于有效日线结构止损 ({eff_daily_stop})"
                        )
                    if direction == "short" and not (price < eff_daily_stop):
                        raise ValueError(
                            f"stop_consistency_violation: add_price crosses structural_stop_daily — "
                            f"空头加仓价 ({price}) 必须严格低于有效日线结构止损 ({eff_daily_stop})"
                        )

                dec_new_qty = Decimal(str(pos["quantity"])) + Decimal(str(clean_add_qty))
                is_int_qty = not bool(spec.get("fractional", False))
                new_qty: int | float = int(dec_new_qty) if is_int_qty else round(float(dec_new_qty), 8)
                new_avg = round((old_avg * old_qty + price * qty_add) / float(new_qty), 6)
                new_open_risk = _compute_open_risk_amount(direction, new_avg, eff_stop, new_qty, mult, fx_rate)
                added_tranche_risk = _compute_open_risk_amount(direction, price, eff_stop, qty_add, mult, fx_rate)
                locked_profit = _compute_locked_profit_amount(direction, new_avg, eff_stop, new_qty, mult, fx_rate)
                old_risk_pct = round((old_risk / equity_val) * 100.0, 4)
                new_risk_pct = round((new_open_risk / equity_val) * 100.0, 4)

                # Check BOTH combined position open risk AND added tranche's standalone stop risk against all risk caps
                effective_risk_for_caps = max(new_open_risk, added_tranche_risk)
                _, risk_scale, single_sector_cap, max_total_cap, max_macro_cap = _account_caps(account)
                open_others = [
                    p for p in positions if p.get("status") == "open" and p.get("position_id") != position_id
                ]
                sector_norm = _normalize_tag(pos.get("sector_theme"))
                other_sector_risk = round(
                    sum(
                        float(p.get("current_open_risk_amount") or 0.0)
                        for p in open_others
                        if _normalize_tag(p.get("sector_theme")) == sector_norm
                    ),
                    2,
                )
                if other_sector_risk + effective_risk_for_caps > single_sector_cap + 1e-6:
                    raise ValueError(
                        f"add_tranche_sector_risk_exceeded: 加仓后板块风险约束 "
                        f"{other_sector_risk + effective_risk_for_caps:,.2f} "
                        f"(合并在途={new_open_risk:,.2f}, 新增笔风险={added_tranche_risk:,.2f}) "
                        f"超过单板块上限 {single_sector_cap:,.2f} (回撤缩放系数 {risk_scale:.2f})，"
                        "可先将止损上移至保本/浮盈区释放风险额度后再加仓，或减小加仓手数"
                    )

                other_total_risk = round(
                    sum(float(p.get("current_open_risk_amount") or 0.0) for p in open_others), 2
                )
                if other_total_risk + effective_risk_for_caps > max_total_cap + 1e-6:
                    raise ValueError(
                        f"add_tranche_total_open_risk_exceeded: 加仓后组合总在途风险 "
                        f"{other_total_risk + effective_risk_for_caps:,.2f} 超过上限 {max_total_cap:,.2f}，"
                        "可先将止损上移至保本/浮盈区释放风险额度后再加仓"
                    )

                if enforce_macro_cap:
                    for tag in _normalize_macro_factors(pos.get("macro_factors")):
                        other_tag_risk = round(
                            sum(
                                float(p.get("current_open_risk_amount") or 0.0)
                                for p in open_others
                                if tag in _normalize_macro_factors(p.get("macro_factors"))
                            ),
                            2,
                        )
                        if other_tag_risk + effective_risk_for_caps > max_macro_cap + 1e-6:
                            raise ValueError(
                                f"macro_factor_overexposed: 加仓后宏大因子 {tag!r} 风险 "
                                f"{other_tag_risk + effective_risk_for_caps:,.2f} 超过上限 {max_macro_cap:,.2f}，"
                                "可先将止损上移至保本/浮盈区释放风险额度后再加仓"
                            )

                # Portfolio margin / capital ceiling check
                new_pos_margin = round(
                    float(new_qty) * new_avg * mult * fx_rate * float(pos.get("margin_rate", 1.0)), 2
                )
                other_margin = round(sum(_position_estimated_margin_account(p) for p in open_others), 2)
                if other_margin + new_pos_margin > equity_val + 1e-6:
                    raise ValueError(
                        f"portfolio_capital_limit_exceeded: 加仓后组合预计保证金/资金占用 "
                        f"{other_margin + new_pos_margin:,.2f} 超过账户权益 {equity_val:,.2f}"
                    )

                now = utc_now_str()
                clean_note = (note or "").strip()
                clean_reason = (reason or "").strip() or (clean_note or "profit_pyramid")
                tranches = list(pos.get("tranches") or [])
                tranches.append(
                    {
                        "tranche_index": len(tranches) + 1,
                        "timestamp_utc": now,
                        "price": round(price, 6),
                        "quantity": clean_add_qty,
                        "reason": clean_reason,
                    }
                )
                eff_stop_rounded = eff_stop
                pos["quantity"] = new_qty
                pos["avg_entry_price"] = new_avg
                pos["tactical_stop_30m"] = eff_stop_rounded
                pos["fx_to_account"] = fx_rate
                if new_stop_30m is not None and eff_stop_rounded != old_stop:
                    pos["stop_updated_at_utc"] = now
                pos["current_open_risk_amount"] = new_open_risk
                pos["locked_profit_amount"] = locked_profit
                pos["estimated_margin_account"] = new_pos_margin
                pos["tranches"] = tranches
                pos["updated_at_utc"] = now

                new_revision = int(doc.get("revision") or 0) + 1
                risk_delta = round(new_open_risk - old_risk, 2)
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "add_on_profit",
                    "event": "scale_in",
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": direction,
                    "price": round(price, 6),
                    "add_price": round(price, 6),
                    "old_entry_price": round(old_avg, 6),
                    "new_avg_entry_price": new_avg,
                    "new_entry_price": new_avg,
                    "old_stop_loss": round(old_stop, 6),
                    "new_stop_loss": eff_stop_rounded,
                    "stop_30m": eff_stop_rounded,
                    "old_quantity": old_qty_orig,
                    "add_quantity": clean_add_qty,
                    "new_quantity": new_qty,
                    "total_quantity": new_qty,
                    "risk_delta": risk_delta,
                    "old_risk_amount": old_risk,
                    "new_risk_amount": new_open_risk,
                    "old_risk_pct": old_risk_pct,
                    "new_risk_pct": new_risk_pct,
                    "added_tranche_risk_amount": added_tranche_risk,
                    "current_open_risk_amount": new_open_risk,
                    "note": clean_note,
                    "reason": clean_reason,
                }

                result_payload = {
                    "status": "added",
                    "revision": new_revision,
                    "position": pos,
                    "old_quantity": old_qty_orig,
                    "add_quantity": clean_add_qty,
                    "new_quantity": new_qty,
                    "old_entry_price": round(old_avg, 6),
                    "add_price": round(price, 6),
                    "new_entry_price": new_avg,
                    "old_stop_loss": round(old_stop, 6),
                    "new_stop_loss": eff_stop_rounded,
                    "old_risk_amount": old_risk,
                    "new_risk_amount": new_open_risk,
                    "old_risk_pct": old_risk_pct,
                    "new_risk_pct": new_risk_pct,
                    "risk_delta": risk_delta,
                    "added_tranche_risk_amount": added_tranche_risk,
                    "tranche_count": len(tranches),
                    "note": clean_note,
                }
                _db_upsert_position(conn, pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "add_position_tranche", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            return {
                **replayed_result,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        return {
            **result_payload,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def scale_in_position(
    position_id: str,
    add_quantity: float,
    add_price: float,
    new_stop_loss: float | None = None,
    fx_rate_to_base: float | None = None,
    note: str = "",
    portfolio_dir: str | None = None,
    enforce_macro_cap: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Scale into an existing open position strictly on confirmed floating profit (T2/T3 pyramid)."""
    eff_reason = (note or "").strip() or "profit_pyramid"
    return add_position_tranche(
        position_id=position_id,
        add_price=add_price,
        add_quantity=add_quantity,
        new_stop_30m=new_stop_loss,
        reason=eff_reason,
        portfolio_dir=portfolio_dir,
        enforce_macro_cap=enforce_macro_cap,
        idempotency_key=idempotency_key,
        fx_to_account=fx_rate_to_base,
        note=note,
    )


def trail_stop(
    position_id: str,
    new_stop_30m: float,
    new_structural_stop_daily: float | None = None,
    current_price: float | None = None,
    reason: str = "trail_stop",
    portfolio_dir: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Tighten stop-loss in the favorable direction ONLY; reject already-breached stops when current_price provided."""
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "trail_stop",
            {
                "position_id": position_id,
                "new_stop_30m": new_stop_30m,
                "new_structural_stop_daily": new_structural_stop_daily,
                "current_price": current_price,
                "reason": reason,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "trail_stop", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                cand_stop = _require_finite_float(new_stop_30m, "new_stop_30m", positive=True)
                cur_price = (
                    _require_finite_float(current_price, "current_price", positive=True)
                    if current_price is not None
                    else None
                )
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                idx = next((i for i, p in enumerate(positions) if p.get("position_id") == position_id), None)
                if idx is None:
                    raise ValueError(f"Position not found: {position_id}")
                pos = dict(positions[idx])
                if pos.get("status") != "open":
                    raise ValueError(f"Position {position_id} is not open (status={pos.get('status')})")

                direction = pos["direction"]
                old_stop = float(pos["tactical_stop_30m"])
                old_daily = pos.get("structural_stop_daily")
                old_risk = float(pos.get("current_open_risk_amount") or 0.0)

                # HARD RULE 1: Never widen stop against the position
                if direction == "long" and cand_stop < old_stop:
                    raise ValueError(
                        f"stop_widening_forbidden: 多头止损只能向上收紧 ({cand_stop} < 原止损 {old_stop})，严禁放宽止损"
                    )
                if direction == "short" and cand_stop > old_stop:
                    raise ValueError(
                        f"stop_widening_forbidden: 空头止损只能向下收紧 ({cand_stop} > 原止损 {old_stop})，严禁放宽止损"
                    )

                # HARD RULE 2: Reject stops already breached by current_price
                if cur_price is not None:
                    if direction == "long" and not (cand_stop < cur_price):
                        raise ValueError(
                            f"stop_breached_by_current_price: 多头新止损 ({cand_stop}) 必须严格低于当前价 ({cur_price})，"
                            "若已被击穿请果断平仓而非移动止损"
                        )
                    if direction == "short" and not (cand_stop > cur_price):
                        raise ValueError(
                            f"stop_breached_by_current_price: 空头新止损 ({cand_stop}) 必须严格高于当前价 ({cur_price})，"
                            "若已被击穿请果断平仓而非移动止损"
                        )

                updated_daily = old_daily
                if new_structural_stop_daily is not None:
                    cand_daily = _require_finite_float(
                        new_structural_stop_daily, "new_structural_stop_daily", positive=True
                    )
                    if old_daily is not None:
                        if direction == "long" and cand_daily < float(old_daily):
                            raise ValueError(
                                f"stop_widening_forbidden: 多头日线结构止损不得下移 ({cand_daily} < {old_daily})"
                            )
                        if direction == "short" and cand_daily > float(old_daily):
                            raise ValueError(
                                f"stop_widening_forbidden: 空头日线结构止损不得上移 ({cand_daily} > {old_daily})"
                            )
                    if cur_price is not None:
                        if direction == "long" and not (cand_daily < cur_price):
                            raise ValueError(
                                f"stop_breached_by_current_price: 多头日线结构止损 ({cand_daily}) 必须严格低于当前价 ({cur_price})"
                            )
                        if direction == "short" and not (cand_daily > cur_price):
                            raise ValueError(
                                f"stop_breached_by_current_price: 空头日线结构止损 ({cand_daily}) 必须严格高于当前价 ({cur_price})"
                            )
                    updated_daily = round(cand_daily, 6)

                new_open_risk = _compute_open_risk_amount(
                    direction,
                    float(pos["avg_entry_price"]),
                    cand_stop,
                    float(pos["quantity"]),
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )
                locked_profit = _compute_locked_profit_amount(
                    direction,
                    float(pos["avg_entry_price"]),
                    cand_stop,
                    float(pos["quantity"]),
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )

                now = utc_now_str()
                new_stop_rounded = round(cand_stop, 6)
                old_stop_rounded = round(old_stop, 6)
                old_daily_rounded = round(float(old_daily), 6) if old_daily is not None else None

                pos["tactical_stop_30m"] = new_stop_rounded
                if new_stop_rounded != old_stop_rounded or not pos.get("stop_updated_at_utc"):
                    pos["stop_updated_at_utc"] = now
                pos["structural_stop_daily"] = updated_daily
                if updated_daily is not None and (
                    updated_daily != old_daily_rounded or not pos.get("structural_stop_updated_at_utc")
                ):
                    pos["structural_stop_updated_at_utc"] = now
                pos["current_open_risk_amount"] = new_open_risk
                pos["locked_profit_amount"] = locked_profit
                pos["updated_at_utc"] = now

                new_revision = int(doc.get("revision") or 0) + 1
                risk_delta = round(new_open_risk - old_risk, 2)
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "trail_stop",
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": direction,
                    "old_stop_30m": old_stop,
                    "new_stop_30m": new_stop_rounded,
                    "new_structural_stop_daily": updated_daily,
                    "risk_delta": risk_delta,
                    "current_open_risk_amount": new_open_risk,
                    "locked_profit_amount": locked_profit,
                    "reason": (reason or "trail_stop").strip(),
                }
                if cur_price is not None:
                    journal_entry["price"] = round(cur_price, 6)

                result_payload = {
                    "status": "trailed",
                    "revision": new_revision,
                    "position": pos,
                    "risk_delta": risk_delta,
                    "freed_risk_amount": round(max(0.0, old_risk - new_open_risk), 2),
                }
                _db_upsert_position(conn, pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "trail_stop", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            return {
                **replayed_result,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        return {
            **result_payload,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def trim_position(
    position_id: str,
    trim_price: float,
    trim_quantity: float,
    reason: str = "partial_take_profit",
    portfolio_dir: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Partially reduce an open position, enforcing min_order_qty & SSE Rule 6.7 and booking partial realized PnL."""
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "trim_position",
            {
                "position_id": position_id,
                "trim_price": trim_price,
                "trim_quantity": trim_quantity,
                "reason": reason,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "trim_position", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                price = _require_finite_float(trim_price, "trim_price", positive=True)
                account = _db_read_account(conn, pdir)
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                idx = next((i for i, p in enumerate(positions) if p.get("position_id") == position_id), None)
                if idx is None:
                    raise ValueError(f"Position not found: {position_id}")
                pos = dict(positions[idx])
                if pos.get("status") != "open":
                    raise ValueError(f"Position {position_id} is not open (status={pos.get('status')})")

                spec = get_contract_spec(pos["symbol"], asset_class=pos.get("asset_class"))
                clean_trim_qty = validate_order_quantity(
                    trim_quantity, spec, field_name="trim_quantity", enforce_min_order_qty=True
                )
                qty_trim = float(clean_trim_qty)

                dec_old_qty = Decimal(str(pos["quantity"]))
                dec_trim_qty = Decimal(str(clean_trim_qty))
                dec_rem_qty = dec_old_qty - dec_trim_qty
                if dec_rem_qty <= 0:
                    raise ValueError(
                        f"trim_quantity ({qty_trim}) >= current quantity ({pos['quantity']}); use close_position instead."
                    )

                min_order_qty = spec.get("min_order_qty", spec.get("lot_step", spec.get("lot_size", 1)))
                dec_min_qty = Decimal(str(min_order_qty))
                if dec_rem_qty < dec_min_qty:
                    if spec.get("asset_class") == "a_share":
                        raise ValueError(
                            f"invalid_remaining_quantity (SSE Rule 6.7): partial trim of {clean_trim_qty} from "
                            f"{pos['quantity']} leaves {dec_rem_qty} shares (< min_order_qty={min_order_qty}) for "
                            f"{pos['symbol']!r}; A-share odd-lot balance below board minimum must be exited in full "
                            "via close_position."
                        )
                    raise ValueError(
                        f"invalid_remaining_quantity: partial trim of {clean_trim_qty} from {pos['quantity']} "
                        f"leaves {dec_rem_qty} (< min_order_qty={min_order_qty}) for {pos['symbol']!r}; "
                        "use close_position to exit the full balance."
                    )

                old_risk = float(pos.get("current_open_risk_amount") or 0.0)
                is_int_qty = not bool(spec.get("fractional", False))
                rem_qty: int | float = int(dec_rem_qty) if is_int_qty else round(float(dec_rem_qty), 8)

                part_pnl = _compute_realized_pnl(
                    pos["direction"],
                    float(pos["avg_entry_price"]),
                    price,
                    qty_trim,
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )
                new_equity = round(float(account["equity"]) + part_pnl, 2)
                is_insolvent = new_equity <= 0
                warnings: list[str] = []
                if is_insolvent:
                    warnings.append(
                        f"account_insolvent: 减仓已实现亏损导致账户权益降至 {new_equity:,.2f} <= 0（穿仓/资不抵债），"
                        "账本已如实记账并自动禁止后续新开仓与加仓"
                    )

                new_open_risk = _compute_open_risk_amount(
                    pos["direction"],
                    float(pos["avg_entry_price"]),
                    float(pos["tactical_stop_30m"]),
                    rem_qty,
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )
                locked_profit = _compute_locked_profit_amount(
                    pos["direction"],
                    float(pos["avg_entry_price"]),
                    float(pos["tactical_stop_30m"]),
                    rem_qty,
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )

                now = utc_now_str()
                account["equity"] = new_equity
                account["insolvent"] = is_insolvent
                prev_peak = account.get("peak_equity")
                account["peak_equity"] = (
                    round(max(float(prev_peak), new_equity), 2) if prev_peak is not None else new_equity
                )
                account["updated_at_utc"] = now

                pos["quantity"] = rem_qty
                pos["partial_realized_pnl"] = round(float(pos.get("partial_realized_pnl") or 0.0) + part_pnl, 2)
                pos["current_open_risk_amount"] = new_open_risk
                pos["locked_profit_amount"] = locked_profit
                pos["estimated_margin_account"] = _position_estimated_margin_account(pos)
                pos["updated_at_utc"] = now

                new_revision = int(doc.get("revision") or 0) + 1
                risk_delta = round(new_open_risk - old_risk, 2)
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "trim",
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": pos["direction"],
                    "price": round(price, 6),
                    "trim_quantity": clean_trim_qty,
                    "remaining_quantity": rem_qty,
                    "partial_realized_pnl": part_pnl,
                    "equity_after": new_equity,
                    "account_insolvent": is_insolvent,
                    "risk_delta": risk_delta,
                    "reason": (reason or "partial_take_profit").strip(),
                }

                result_payload = {
                    "status": "trimmed",
                    "revision": new_revision,
                    "account": account,
                    "account_insolvent": is_insolvent,
                    "position": pos,
                    "partial_realized_pnl": part_pnl,
                    "risk_delta": risk_delta,
                    "warnings": warnings,
                }
                _db_upsert_account(conn, account)
                _db_upsert_position(conn, pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "trim_position", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            return {
                **replayed_result,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        return {
            **result_payload,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def close_position(
    position_id: str,
    exit_price: float,
    exit_reason: str,
    exit_category: str = "manual",
    portfolio_dir: str | None = None,
    update_account_equity: bool = True,
    idempotency_key: str | None = None,
    research_dir: str | None = None,
) -> dict[str, Any]:
    """Close an open position, record realized PnL & R-multiple, and update ledger equity atomically."""
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "close_position",
            {
                "position_id": position_id,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "exit_category": exit_category,
                "update_account_equity": update_account_equity,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        has_other_open_for_symbol = False
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "close_position", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                ex_price = _require_finite_float(exit_price, "exit_price", positive=True)
                ex_reason = (exit_reason or "").strip()
                if not ex_reason:
                    raise ValueError("exit_reason must be non-empty")

                account = _db_read_account(conn, pdir)
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                idx = next((i for i, p in enumerate(positions) if p.get("position_id") == position_id), None)
                if idx is None:
                    raise ValueError(f"Position not found: {position_id}")
                pos = dict(positions[idx])
                if pos.get("status") != "open":
                    raise ValueError(f"Position {position_id} is already closed")

                pos_key = _position_archive_key(pos)
                has_other_open_for_symbol = any(
                    p.get("status") == "open"
                    and p.get("position_id") != position_id
                    and _position_archive_key(p) == pos_key
                    for p in positions
                )

                old_risk = float(pos.get("current_open_risk_amount") or 0.0)
                rem_pnl = _compute_realized_pnl(
                    pos["direction"],
                    float(pos["avg_entry_price"]),
                    ex_price,
                    float(pos["quantity"]),
                    float(pos["multiplier"]),
                    float(pos.get("fx_to_account", 1.0)),
                )
                total_pnl = round(float(pos.get("partial_realized_pnl") or 0.0) + rem_pnl, 2)
                init_risk = float(pos.get("initial_risk_amount") or 0.0)
                r_mult = round(total_pnl / init_risk, 4) if init_risk > 0 else None

                now = utc_now_str()
                pos["status"] = "closed"
                pos["current_open_risk_amount"] = 0.0
                pos["estimated_margin_account"] = 0.0
                pos["exit_price"] = round(ex_price, 6)
                pos["exit_reason"] = ex_reason
                pos["exit_category"] = (exit_category or "manual").strip()
                pos["realized_pnl"] = total_pnl
                pos["r_multiple"] = r_mult
                pos["closed_at_utc"] = now
                pos["updated_at_utc"] = now

                is_insolvent = bool(account.get("insolvent", False)) or (float(account["equity"]) <= 0)
                warnings: list[str] = []
                if update_account_equity:
                    # Apply rem_pnl (remaining quantity PnL) since any prior trim_position already booked partial_realized_pnl
                    # Always record real gap losses even if equity becomes <= 0 so the ledger stays truthful.
                    new_eq = round(float(account["equity"]) + rem_pnl, 2)
                    is_insolvent = new_eq <= 0
                    account["equity"] = new_eq
                    account["insolvent"] = is_insolvent
                    prev_peak = account.get("peak_equity")
                    account["peak_equity"] = (
                        round(max(float(prev_peak), new_eq), 2) if prev_peak is not None else new_eq
                    )
                    account["updated_at_utc"] = now
                    _db_upsert_account(conn, account)
                if is_insolvent:
                    warnings.append(
                        f"account_insolvent: 平仓已实现亏损导致账户权益降至 {float(account['equity']):,.2f} <= 0（穿仓/资不抵债），"
                        "账本已如实记账并自动禁止后续新开仓与加仓"
                    )

                new_revision = int(doc.get("revision") or 0) + 1
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "close",
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": pos["direction"],
                    "avg_entry_price": pos["avg_entry_price"],
                    "price": round(ex_price, 6),
                    "quantity": pos["quantity"],
                    "realized_pnl": total_pnl,
                    "equity_after": account["equity"],
                    "account_insolvent": is_insolvent,
                    "r_multiple": r_mult,
                    "exit_category": pos["exit_category"],
                    "risk_delta": round(-old_risk, 2),
                    "reason": ex_reason,
                }

                result_payload = {
                    "status": "closed",
                    "revision": new_revision,
                    "account": account,
                    "account_insolvent": is_insolvent,
                    "position": pos,
                    "realized_pnl": total_pnl,
                    "r_multiple": r_mult,
                    "warnings": warnings,
                }
                _db_upsert_position(conn, pos)
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "close_position", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            replay_pos = replayed_result.get("position") or {}
            archive_sync = _reconcile_archive_status_unlocked(
                pdir,
                str(replay_pos.get("symbol") or ""),
                replay_pos.get("asset_class"),
                research_dir=research_dir,
            )
            out_warnings = list(replayed_result.get("warnings") or [])
            if archive_sync.get("error"):
                out_warnings.append(f"archive_status_sync_failed: {archive_sync['error']}")
            return {
                **replayed_result,
                "warnings": out_warnings,
                "archive_status_sync": archive_sync,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        closed_pos_res = result_payload["position"]
        closed_sym = closed_pos_res["symbol"]
        closed_market = _asset_class_to_archive_market(closed_pos_res.get("asset_class"))
        if not has_other_open_for_symbol:
            archive_sync = _sync_research_archive_status(
                closed_sym,
                "watchlist",
                research_dir=research_dir,
                portfolio_dir=pdir,
                only_if_current_status="holding",
                market_override=closed_market,
            )
        else:
            archive_sync = {
                "synced": False,
                "skipped_reason": "other_open_position_exists",
                "symbol": closed_sym,
                "market": closed_market,
                "target_status": "watchlist",
            }
        out_warnings = list(result_payload.get("warnings") or [])
        if archive_sync.get("error"):
            out_warnings.append(f"archive_status_sync_failed: {archive_sync['error']}")
        return {
            **result_payload,
            "warnings": out_warnings,
            "archive_status_sync": archive_sync,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


def void_position(
    position_id: str,
    reason: str = "误录作废",
    portfolio_dir: str | None = None,
    idempotency_key: str | None = None,
    research_dir: str | None = None,
) -> dict[str, Any]:
    """Void a mistakenly entered open position without booking fake PnL or closed-trade history.

    Strictly restricted to untrimmed, single-tranche open positions whose ``partial_realized_pnl``
    is zero, ensuring ``account.equity`` and ``peak_equity`` were never mutated by this record.
    """
    idem_key = _normalize_idempotency_key(idempotency_key)
    req_hash = (
        _compute_request_hash(
            "void_position",
            {
                "position_id": position_id,
                "reason": reason,
            },
        )
        if idem_key is not None
        else ""
    )
    pdir = _resolve_portfolio_dir(portfolio_dir)

    with _portfolio_lock(pdir):
        replayed_result: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None
        has_other_open_for_symbol = False
        with _sqlite_write_tx(pdir) as conn:
            _maybe_migrate_legacy_json_in_tx(conn, pdir)
            cached = _db_lookup_idempotency(conn, idem_key, "void_position", req_hash)
            if cached is not None:
                replayed_result = cached
            else:
                clean_reason = (reason or "").strip() or "误录作废"
                account = _db_read_account(conn, pdir)
                doc = _db_read_positions_doc(conn, pdir)
                positions: list[dict[str, Any]] = list(doc.get("positions") or [])
                idx = next((i for i, p in enumerate(positions) if p.get("position_id") == position_id), None)
                if idx is None:
                    raise ValueError(f"Position not found: {position_id}")
                pos = dict(positions[idx])
                if pos.get("status") != "open":
                    raise ValueError(f"Position {position_id} is already closed and cannot be voided")

                tranches = list(pos.get("tranches") or [])
                partial_pnl = float(pos.get("partial_realized_pnl") or 0.0)
                qty_now = float(pos.get("quantity") or 0.0)
                qty_init = float(pos.get("initial_quantity") or qty_now)
                if len(tranches) > 1 or abs(partial_pnl) > 1e-9 or abs(qty_now - qty_init) > 1e-9:
                    raise ValueError(
                        "void_requires_untrimmed_initial_position: "
                        "该头寸已发生分批加仓或减仓结算，无法按误录无损作废，请使用平仓了结"
                    )

                pos_key = _position_archive_key(pos)
                has_other_open_for_symbol = any(
                    p.get("status") == "open"
                    and p.get("position_id") != position_id
                    and _position_archive_key(p) == pos_key
                    for p in positions
                )

                old_risk = float(pos.get("current_open_risk_amount") or 0.0)
                now = utc_now_str()
                new_revision = int(doc.get("revision") or 0) + 1
                journal_entry = {
                    "timestamp_utc": now,
                    "action": "void",
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "sector_theme": pos["sector_theme"],
                    "direction": pos["direction"],
                    "price": pos["avg_entry_price"],
                    "stop_30m": pos["tactical_stop_30m"],
                    "quantity": pos["quantity"],
                    "equity_after": account["equity"],
                    "risk_delta": round(-old_risk, 2),
                    "reason": clean_reason,
                }
                result_payload = {
                    "status": "voided",
                    "revision": new_revision,
                    "account": account,
                    "voided_position": pos,
                    "freed_risk_amount": round(old_risk, 2),
                }
                conn.execute("DELETE FROM positions WHERE position_id = ?", (position_id,))
                _db_update_meta(conn, revision=new_revision, updated_at_utc=now)
                _db_insert_journal_records(conn, [journal_entry])
                _db_store_idempotency(conn, idem_key, "void_position", req_hash, result_payload, now)

        mirror_ok, mirror_err = _safe_export_mirrors_unlocked(pdir)
        if replayed_result is not None:
            replay_voided = replayed_result.get("voided_position") or {}
            archive_sync = _reconcile_archive_status_unlocked(
                pdir,
                str(replay_voided.get("symbol") or ""),
                replay_voided.get("asset_class"),
                research_dir=research_dir,
            )
            return {
                **replayed_result,
                "archive_status_sync": archive_sync,
                "idempotent_replay": True,
                "mirror_sync_ok": mirror_ok,
                "mirror_sync_error": mirror_err,
            }
        assert result_payload is not None
        voided_pos_res = result_payload["voided_position"]
        voided_sym = voided_pos_res["symbol"]
        voided_market = _asset_class_to_archive_market(voided_pos_res.get("asset_class"))
        if not has_other_open_for_symbol:
            archive_sync = _sync_research_archive_status(
                voided_sym,
                "watchlist",
                research_dir=research_dir,
                portfolio_dir=pdir,
                only_if_current_status="holding",
                market_override=voided_market,
            )
        else:
            archive_sync = {
                "synced": False,
                "skipped_reason": "other_open_position_exists",
                "symbol": voided_sym,
                "market": voided_market,
                "target_status": "watchlist",
            }
        return {
            **result_payload,
            "archive_status_sync": archive_sync,
            "mirror_sync_ok": mirror_ok,
            "mirror_sync_error": mirror_err,
        }


# ---------------------------------------------------------------------------
# Portfolio Summary
# ---------------------------------------------------------------------------

def summarize_portfolio(portfolio_dir: str | None = None) -> dict[str, Any]:
    """Return full account risk summary, margin utilization, sector slot occupancy, macro overlap, and open positions."""
    pdir = _resolve_portfolio_dir(portfolio_dir)
    with _portfolio_lock(pdir):
        account = _read_account_unlocked(pdir)
        doc = _read_positions_doc_unlocked(pdir)

    positions: list[dict[str, Any]] = list(doc.get("positions") or [])
    open_positions = [p for p in positions if p.get("status") == "open"]
    closed_positions = [p for p in positions if p.get("status") == "closed"]

    equity = float(account["equity"])
    dd_pct, risk_scale, single_sector_cap, max_total_cap, max_macro_cap = _account_caps(account)
    total_open_risk = round(sum(float(p.get("current_open_risk_amount") or 0.0) for p in open_positions), 2)
    total_open_risk_pct = round((total_open_risk / equity) * 100.0, 4) if equity > 0 else 0.0
    total_locked_profit = round(sum(float(p.get("locked_profit_amount") or 0.0) for p in open_positions), 2)
    total_est_margin = round(sum(_position_estimated_margin_account(p) for p in open_positions), 2)
    margin_util_pct = round((total_est_margin / equity) * 100.0, 4) if equity > 0 else 0.0

    sector_slots: dict[str, dict[str, Any]] = {}
    for p in open_positions:
        sec = _normalize_tag(p.get("sector_theme"))
        slot = sector_slots.setdefault(
            sec,
            {
                "count": 0,
                "max_slots": 2,
                "symbols": [],
                "position_ids": [],
                "open_risk_amount": 0.0,
                "sector_risk_cap": single_sector_cap,
            },
        )
        slot["count"] += 1
        slot["symbols"].append(p["symbol"])
        slot["position_ids"].append(p["position_id"])
        slot["open_risk_amount"] = round(
            slot["open_risk_amount"] + float(p.get("current_open_risk_amount") or 0.0), 2
        )

    for slot in sector_slots.values():
        slot["remaining_sector_risk_budget"] = round(max(0.0, single_sector_cap - slot["open_risk_amount"]), 2)
        slot["slot_status"] = "sole_leader" if slot["count"] == 1 else "co_leader_duopoly_full"
        slot["sector_cap_breached"] = slot["open_risk_amount"] > single_sector_cap + 1e-6

    macro_concentration: dict[str, dict[str, Any]] = {}
    for p in open_positions:
        p_risk = float(p.get("current_open_risk_amount") or 0.0)
        for tag in _normalize_macro_factors(p.get("macro_factors")):
            entry = macro_concentration.setdefault(
                tag,
                {
                    "count": 0,
                    "symbols": [],
                    "open_risk_amount": 0.0,
                    "open_risk_pct": 0.0,
                    "cap": max_macro_cap,
                    "overexposed": False,
                },
            )
            entry["count"] += 1
            entry["symbols"].append(p["symbol"])
            entry["open_risk_amount"] = round(entry["open_risk_amount"] + p_risk, 2)

    for entry in macro_concentration.values():
        entry["open_risk_pct"] = round((entry["open_risk_amount"] / equity) * 100.0, 4) if equity > 0 else 0.0
        entry["overexposed"] = entry["open_risk_amount"] > max_macro_cap + 1e-6

    return {
        "portfolio_dir": pdir,
        "base_currency": account["base_currency"],
        "equity": equity,
        "insolvent": bool(account.get("insolvent", False)) or (equity <= 0),
        "peak_equity": float(account["peak_equity"]),
        "drawdown_pct": dd_pct,
        "risk_scale": risk_scale,
        "single_sector_risk_cap": single_sector_cap,
        "max_total_open_risk_cap": max_total_cap,
        "max_macro_factor_risk_cap": max_macro_cap,
        "open_positions_count": len(open_positions),
        "closed_positions_count": len(closed_positions),
        "total_open_risk_amount": total_open_risk,
        "total_open_risk_pct": total_open_risk_pct,
        "total_estimated_margin_account": total_est_margin,
        "margin_utilization_pct": margin_util_pct,
        "remaining_portfolio_risk_budget": round(max(0.0, max_total_cap - total_open_risk), 2),
        "total_locked_profit_amount": total_locked_profit,
        "sector_slots": sector_slots,
        "macro_factor_concentration": macro_concentration,
        "open_positions": open_positions,
        "revision": int(doc.get("revision") or 0),
        "updated_at_utc": doc.get("updated_at_utc"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Layer-4 transactional local portfolio & risk manager (single-sector leader exclusivity)"
    )
    parser.add_argument(
        "--portfolio-dir",
        default=None,
        help="Portfolio directory (default: <repo>/portfolio)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize portfolio database and JSON mirrors")
    p_init.add_argument("--equity", type=float, default=1_000_000.0)
    p_init.add_argument("--base-currency", default="USD")
    p_init.add_argument("--default-risk-pct", type=float, default=1.0)
    p_init.add_argument("--max-total-risk-pct", type=float, default=6.0)
    p_init.add_argument("--max-macro-risk-pct", type=float, default=3.0)
    p_init.add_argument("--overwrite", action="store_true")

    # summary
    sub.add_parser("summary", help="Show portfolio risk summary, sector slots, and macro exposure")

    # sync-mirrors
    sub.add_parser("sync-mirrors", help="Explicitly resync account.json, positions.json, and journal.jsonl from portfolio.db")

    # list
    p_list = sub.add_parser("list", help="List positions")
    p_list.add_argument("--status", choices=["open", "closed"], default=None)

    # check-admission
    p_adm = sub.add_parser("check-admission", help="Dry-run sector leader & risk admission check")
    p_adm.add_argument("--sector", required=True)
    p_adm.add_argument("--symbol", required=True)
    p_adm.add_argument("--risk-amount", type=float, required=True)
    p_adm.add_argument("--leader-role", choices=["sole_leader", "co_leader"], default="sole_leader")
    p_adm.add_argument("--co-leader-reason", default="")
    p_adm.add_argument("--macro-factors", default="")
    p_adm.add_argument("--allow-rotate", action="store_true")

    # open
    p_open = sub.add_parser("open", help="Open a new position")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--direction", required=True, choices=["long", "short"])
    p_open.add_argument("--sector", required=True)
    p_open.add_argument("--entry", type=float, required=True)
    p_open.add_argument("--stop", type=float, required=True)
    p_open.add_argument("--daily-stop", type=float, default=None)
    p_open.add_argument("--target", type=float, default=None)
    p_open.add_argument("--quantity", type=float, required=True)
    p_open.add_argument("--leader-role", choices=["sole_leader", "co_leader"], default="sole_leader")
    p_open.add_argument("--co-leader-reason", default="")
    p_open.add_argument("--macro-factors", default="")
    p_open.add_argument("--red-line", action="append", default=[])
    p_open.add_argument("--contract-symbol", default=None)
    p_open.add_argument("--multiplier", type=float, default=None)
    p_open.add_argument("--margin-rate", type=float, default=None)
    p_open.add_argument("--allow-equity-margin", action="store_true", default=False)
    p_open.add_argument("--fx-to-account", type=float, default=None)
    p_open.add_argument("--asset-class", default=None)
    p_open.add_argument("--reason", default="initial_entry")
    p_open.add_argument("--notes", default="")
    p_open.add_argument("--idempotency-key", default=None)

    # rotate
    p_rot = sub.add_parser("rotate", help="Atomically close old sector leader and open new leader")
    p_rot.add_argument("--old-id", required=True)
    p_rot.add_argument("--exit-price", type=float, required=True)
    p_rot.add_argument("--exit-reason", required=True)
    p_rot.add_argument("--symbol", required=True)
    p_rot.add_argument("--direction", required=True, choices=["long", "short"])
    p_rot.add_argument("--entry", type=float, required=True)
    p_rot.add_argument("--stop", type=float, required=True)
    p_rot.add_argument("--daily-stop", type=float, default=None)
    p_rot.add_argument("--target", type=float, default=None)
    p_rot.add_argument("--quantity", type=float, required=True)
    p_rot.add_argument("--macro-factors", default="")
    p_rot.add_argument("--red-line", action="append", default=[])
    p_rot.add_argument("--contract-symbol", default=None)
    p_rot.add_argument("--multiplier", type=float, default=None)
    p_rot.add_argument("--margin-rate", type=float, default=None)
    p_rot.add_argument("--allow-equity-margin", action="store_true", default=False)
    p_rot.add_argument("--fx-to-account", type=float, default=None)
    p_rot.add_argument("--asset-class", default=None)
    p_rot.add_argument("--idempotency-key", default=None)

    # add
    p_add = sub.add_parser("add", help="Add tranche strictly on floating profit")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--price", type=float, required=True)
    p_add.add_argument("--quantity", type=float, required=True)
    p_add.add_argument("--new-stop", type=float, default=None)
    p_add.add_argument("--reason", default="profit_pyramid")
    p_add.add_argument("--idempotency-key", default=None)

    # scale-in
    p_scale = sub.add_parser("scale-in", help="Scale into an open position strictly on floating profit (T2/T3)")
    p_scale.add_argument("--id", required=True)
    p_scale.add_argument("--quantity", "--add-quantity", dest="quantity", type=float, required=True)
    p_scale.add_argument("--price", "--add-price", dest="price", type=float, required=True)
    p_scale.add_argument("--new-stop", "--new-stop-loss", dest="new_stop", type=float, default=None)
    p_scale.add_argument("--fx-rate-to-base", "--fx-to-account", dest="fx_rate_to_base", type=float, default=None)
    p_scale.add_argument("--note", default="")
    p_scale.add_argument("--idempotency-key", default=None)

    # trail-stop
    p_trail = sub.add_parser("trail-stop", help="Tighten 30m (and optional daily) stop")
    p_trail.add_argument("--id", required=True)
    p_trail.add_argument("--new-stop", type=float, required=True)
    p_trail.add_argument("--new-daily-stop", type=float, default=None)
    p_trail.add_argument("--current-price", type=float, default=None)
    p_trail.add_argument("--reason", default="trail_stop")
    p_trail.add_argument("--idempotency-key", default=None)

    # trim
    p_trim = sub.add_parser("trim", help="Partially reduce an open position and book partial realized PnL")
    p_trim.add_argument("--id", required=True)
    p_trim.add_argument("--price", "--trim-price", dest="price", type=float, required=True)
    p_trim.add_argument("--quantity", "--trim-quantity", dest="quantity", type=float, required=True)
    p_trim.add_argument("--reason", default="partial_take_profit")
    p_trim.add_argument("--idempotency-key", default=None)

    # close
    p_close = sub.add_parser("close", help="Close an open position")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--exit-price", type=float, required=True)
    p_close.add_argument("--exit-reason", required=True)
    p_close.add_argument("--exit-category", default="manual")
    p_close.add_argument("--update-equity", dest="update_equity", action="store_true", default=True)
    p_close.add_argument("--no-update-equity", dest="update_equity", action="store_false")
    p_close.add_argument("--idempotency-key", default=None)

    # void
    p_void = sub.add_parser("void", help="Void a mistakenly entered open position without booking PnL")
    p_void.add_argument("--id", required=True)
    p_void.add_argument("--reason", default="误录作废")
    p_void.add_argument("--idempotency-key", default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            res = init_portfolio(
                portfolio_dir=args.portfolio_dir,
                equity=args.equity,
                base_currency=args.base_currency,
                default_risk_per_trade_pct=args.default_risk_pct,
                max_total_open_risk_pct=args.max_total_risk_pct,
                max_macro_factor_risk_pct=args.max_macro_risk_pct,
                overwrite=args.overwrite,
            )
        elif args.command == "summary":
            res = summarize_portfolio(portfolio_dir=args.portfolio_dir)
        elif args.command == "sync-mirrors":
            res = sync_mirrors(portfolio_dir=args.portfolio_dir)
        elif args.command == "list":
            res = {"positions": load_positions(portfolio_dir=args.portfolio_dir, status=args.status)}
        elif args.command == "check-admission":
            account = load_account(portfolio_dir=args.portfolio_dir)
            positions = load_positions(portfolio_dir=args.portfolio_dir)
            res = check_sector_slot_admission(
                positions=positions,
                account=account,
                sector_theme=args.sector,
                symbol=args.symbol,
                new_risk_amount=args.risk_amount,
                leader_role=args.leader_role,
                co_leader_reason=args.co_leader_reason,
                macro_factors=_split_csv(args.macro_factors),
                allow_rotate=args.allow_rotate,
            )
        elif args.command == "open":
            res = open_position(
                symbol=args.symbol,
                direction=args.direction,
                sector_theme=args.sector,
                entry_price=args.entry,
                tactical_stop_30m=args.stop,
                quantity=args.quantity,
                structural_stop_daily=args.daily_stop,
                target_price=args.target,
                leader_role=args.leader_role,
                co_leader_reason=args.co_leader_reason,
                macro_factors=_split_csv(args.macro_factors),
                exit_red_lines=args.red_line,
                contract_symbol=args.contract_symbol,
                multiplier=args.multiplier,
                margin_rate=args.margin_rate,
                fx_to_account=args.fx_to_account,
                asset_class=args.asset_class,
                notes=args.notes,
                reason=args.reason,
                portfolio_dir=args.portfolio_dir,
                allow_equity_margin=args.allow_equity_margin,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "rotate":
            res = rotate_leader(
                old_position_id=args.old_id,
                exit_price=args.exit_price,
                exit_reason=args.exit_reason,
                new_position_kwargs={
                    "symbol": args.symbol,
                    "direction": args.direction,
                    "entry_price": args.entry,
                    "tactical_stop_30m": args.stop,
                    "structural_stop_daily": args.daily_stop,
                    "target_price": args.target,
                    "quantity": args.quantity,
                    "macro_factors": _split_csv(args.macro_factors),
                    "exit_red_lines": args.red_line,
                    "contract_symbol": args.contract_symbol,
                    "multiplier": args.multiplier,
                    "margin_rate": args.margin_rate,
                    "allow_equity_margin": args.allow_equity_margin,
                    "fx_to_account": args.fx_to_account,
                    "asset_class": args.asset_class,
                },
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "add":
            res = add_position_tranche(
                position_id=args.id,
                add_price=args.price,
                add_quantity=args.quantity,
                new_stop_30m=args.new_stop,
                reason=args.reason,
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "scale-in":
            res = scale_in_position(
                position_id=args.id,
                add_quantity=args.quantity,
                add_price=args.price,
                new_stop_loss=args.new_stop,
                fx_rate_to_base=args.fx_rate_to_base,
                note=args.note,
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "trail-stop":
            res = trail_stop(
                position_id=args.id,
                new_stop_30m=args.new_stop,
                new_structural_stop_daily=args.new_daily_stop,
                current_price=args.current_price,
                reason=args.reason,
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "trim":
            res = trim_position(
                position_id=args.id,
                trim_price=args.price,
                trim_quantity=args.quantity,
                reason=args.reason,
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "close":
            res = close_position(
                position_id=args.id,
                exit_price=args.exit_price,
                exit_reason=args.exit_reason,
                exit_category=args.exit_category,
                portfolio_dir=args.portfolio_dir,
                update_account_equity=args.update_equity,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "void":
            res = void_position(
                position_id=args.id,
                reason=args.reason,
                portfolio_dir=args.portfolio_dir,
                idempotency_key=args.idempotency_key,
            )
        else:  # pragma: no cover
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
