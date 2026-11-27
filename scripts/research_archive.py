#!/usr/bin/env python3
"""Incremental per-symbol Layer 3 research archive manager.

Stores persistent, append-friendly research files under:
    research/<MARKET>/<SAFE_SYMBOL>/
        - thesis.md       (human-readable thesis card + append-only revision log)
        - state.json      (structured triggers, Chanlun anchors, watermarks, optimistic revision)
        - evidence.jsonl  (append-only L1-L4 graded facts with deduplication & supersedes)
"""
# ruff: noqa: EXE001, TRY004, UP017
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(__file__))
from market_snapshot import normalize_symbol

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
DEFAULT_RESEARCH_DIR = os.path.join(ROOT_DIR, "research")

ASSET_CLASS_TO_MARKET = {
    "us_stock_or_etf": "US",
    "a_share": "A_SHARE",
    "cn_future": "CN_FUTURES",
    "commodity": "COMMODITIES",
    "fx": "FX",
    "crypto": "CRYPTO",
    "macro": "MACRO",
}

VALID_MARKETS = set(ASSET_CLASS_TO_MARKET.values())
VALID_EVIDENCE_LEVELS = {"L1", "L2", "L3", "L4"}
VALID_EVIDENCE_KINDS = {"fact", "external_forecast", "assumption"}
VALID_STATUSES = {
    "watchlist",
    "actionable_long",
    "actionable_short",
    "pair_trade",
    "holding",
    "rejected",
    "archived",
}
VALID_DIRECTIONS = {"long", "short", "neutral", "pair"}
VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}
VALID_CROWDING_ASSESSMENTS = {"underpriced", "fair", "crowded", "unknown"}
MAX_EXPECTATION_GAP_KPIS = 6
EXPECTATION_GAP_CANONICAL_KEYS = (
    "priced_in_consensus",
    "our_variant_view",
    "crowding_assessment",
    "leading_kpis",
)
EXPECTATION_GAP_KPI_OPTIONAL_KEYS = (
    "current_baseline",
    "expected_trajectory",
    "falsification_threshold",
    "cadence",
)
EXPECTATION_GAP_KPI_ALLOWED_KEYS = ("name",) + EXPECTATION_GAP_KPI_OPTIONAL_KEYS
VALID_OPERATORS = {">", ">=", "<", "<=", "==", "contains", "event"}
VALID_TRIGGER_KINDS = {
    "fundamental",
    "supply_demand",
    "policy",
    "chanlun",
    "macro",
    "term_structure",
    "freight",
    "spread",
    "rate_diff",
}
VALID_EVALUATION_MODES = {"auto", "ai_web", "manual"}
VALID_VERIFICATION_METHODS = {"ai_web", "manual"}
VALID_VERIFICATION_STATUSES = {"clear", "fired", "uncertain"}
DEFAULT_VERIFICATION_TTL_DAYS = 7
MAX_FUTURE_CLOCK_SKEW_SECONDS = 0

KNOWN_AUTO_METRICS: frozenset[str] = frozenset({
    # Price / Technicals / Chanlun
    "close",
    "contract_close",
    "continuous_close",
    "atr14",
    "atr14_30m",
    "return_1d_pct",
    "return_5d_pct",
    "return_1bar_30m_pct",
    "return_5bar_30m_pct",
    "daily_zd",
    "daily_zg",
    "intraday_30m_zd",
    "intraday_30m_zg",
    "daily_signal",
    "intraday_30m_signal",
    # Commodities
    "curve_structure",
    "roll_yield_near_pct",
    "roll_yield_curve_pct",
    "front_far_spread",
    "basis_pct",
    "spot_price",
    "inventory_latest",
    "inventory_chg_pct",
    "bdti",
    "bcti",
    "bdi",
    "bci",
    "bsi",
    "cftc_mm_net",
    "cftc_mm_net_pct_oi",
    # FX
    "rate_diff_10y_bp",
    "rate_diff_2y_bp",
    "tff_lev_money_net",
    "tff_pair_aligned_net",
    # Equities
    "pe_ttm_pctile_5y",
    "pb_pctile_5y",
    "pe_ttm",
    "pb",
    "forward_pe",
    "short_pct_float",
    "target_mean_price",
    "next_earnings_date",
    "days_to_earnings",
    # Crypto
    "etf_flow_5d_m",
    "etf_flow_20d_m",
    "stablecoin_chg_7d_pct",
    "perp_oi_usd",
    "perp_oi_contracts",
    "perp_funding_ann_pct",
    # Generic cross-market spread keys
    "cross_spread",
    "cross_spread_pct",
    "cross_ratio",
})

_UTC_SPACE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?\s+UTC$")
_ISO_TZ_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def is_known_auto_metric(metric: Any) -> bool:
    """Return True if ``metric`` is populated automatically by sentinel live observations."""
    if not isinstance(metric, str):
        return False
    key = metric.strip()
    if not key:
        return False
    if key in KNOWN_AUTO_METRICS:
        return True
    return "_vs_" in key or key.endswith(("_spread", "_spread_pct", "_ratio", "_import_margin"))


def is_trigger_auto_metric(trig: dict[str, Any]) -> bool:
    """Return True if ``trig`` represents a live machine-evaluated metric (never overridable by AI/manual)."""
    metric = trig.get("metric")
    if is_known_auto_metric(metric):
        return True
    return str(trig.get("evaluation_mode") or "").strip().lower() == "auto"


def is_valid_http_url(url: Any) -> bool:
    """Strictly validate an HTTP(S) URL with a non-empty hostname and no control/whitespace/userinfo/backslash chars."""
    if not isinstance(url, str):
        return False
    s = url.strip()
    if not s or len(s) > 2000:
        return False
    if "\\" in s:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return False
    try:
        parsed = urlsplit(s)
        _ = parsed.port  # raises ValueError on invalid port like :nope or out-of-range
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    host = parsed.hostname
    return bool(host and not host.startswith(".") and not host.endswith("."))


def parse_strict_utc_timestamp(ts: Any) -> datetime | None:
    """Parse strict timezone-aware UTC/ISO-8601 timestamp into aware UTC ``datetime``; reject garbage/naive."""
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    m_utc = _UTC_SPACE_TS_RE.fullmatch(s)
    if m_utc:
        d_part, hm_part, sec_part = m_utc.group(1), m_utc.group(2), m_utc.group(3) or "00"
        try:
            return datetime.fromisoformat(f"{d_part}T{hm_part}:{sec_part}+00:00").astimezone(timezone.utc)
        except ValueError:
            return None
    m_iso = _ISO_TZ_TS_RE.fullmatch(s)
    if m_iso:
        iso_norm = s.replace(" ", "T")
        if iso_norm.endswith("Z"):
            iso_norm = iso_norm[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso_norm)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    return None


def _parse_positive_ttl_days(value: Any) -> int | None:
    """Parse a positive integer TTL in days; return None for missing/bool/non-positive/invalid values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = int(s)
        except ValueError:
            return None
        return parsed if parsed >= 1 else None
    return None


def get_trigger_declared_ttl_days(trig: dict[str, Any]) -> int | None:
    """Return the strictest positive TTL declared on a trigger definition (supports verification_ttl_days & ttl_days)."""
    if not isinstance(trig, dict):
        return None
    candidates = [
        parsed
        for parsed in (
            _parse_positive_ttl_days(trig.get("verification_ttl_days")),
            _parse_positive_ttl_days(trig.get("ttl_days")),
        )
        if parsed is not None
    ]
    return min(candidates) if candidates else None


def resolve_effective_verification_ttl_days(
    trig: dict[str, Any],
    ver: dict[str, Any] | None = None,
) -> int:
    """Enforce the stricter (smaller) positive TTL between trigger definition and verification record."""
    trig_ttl = get_trigger_declared_ttl_days(trig)
    ver_ttl = _parse_positive_ttl_days(ver.get("ttl_days")) if isinstance(ver, dict) else None
    if trig_ttl is not None and ver_ttl is not None:
        return min(trig_ttl, ver_ttl)
    if trig_ttl is not None:
        return trig_ttl
    if ver_ttl is not None:
        return ver_ttl
    return DEFAULT_VERIFICATION_TTL_DAYS


def exact_threshold_key(val: Any) -> Any:
    """Build a strict, type- and insertion-order-preserving canonical key for trigger value identity."""
    if val is None:
        return ("none", None)
    if isinstance(val, bool):
        return ("bool", val)
    if isinstance(val, int):
        return ("int", val)
    if isinstance(val, float):
        if not math.isfinite(val):
            return ("nonfinite", str(val))
        return ("float", repr(val))
    if isinstance(val, str):
        return ("str", val)
    if isinstance(val, list):
        return ("list", tuple(exact_threshold_key(item) for item in val))
    if isinstance(val, tuple):
        return ("tuple", tuple(exact_threshold_key(item) for item in val))
    if isinstance(val, dict):
        return ("dict", tuple((exact_threshold_key(k), exact_threshold_key(v)) for k, v in val.items()))
    return (type(val).__qualname__, repr(val), str(val))


def trigger_definition_fingerprint(trig: dict[str, Any]) -> dict[str, Any]:
    """Return canonical definition snapshot used for optimistic conflict detection during async verification."""
    return {
        "kind": str(trig.get("kind") or "").strip().lower(),
        "evaluation_mode": str(trig.get("evaluation_mode") or "").strip().lower(),
        "metric": str(trig.get("metric") or "").strip(),
        "operator": str(trig.get("operator") or "").strip(),
        "threshold": exact_threshold_key(trig.get("threshold")),
        "action": str(trig.get("action") or "").strip(),
        "status": str(trig.get("status") or "armed").strip().lower(),
        "verification_ttl_days": get_trigger_declared_ttl_days(trig),
        "description": str(trig.get("description") or "").strip(),
    }


def utc_now_iso_seconds() -> str:
    """Return second-precision UTC ISO-8601 timestamp (e.g. '2026-11-25T06:50:12Z')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
def _archive_lock(folder: str):
    """Serialize both multi-threaded and multi-process writers for the same symbol archive."""
    os.makedirs(folder, exist_ok=True)
    lock_path = os.path.join(os.path.realpath(folder), ".archive.lock")
    tlock = _get_inproc_lock(lock_path)
    with tlock, open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_symbol_dir_name(norm_symbol: str) -> str:
    """Bijective (collision-free), filesystem-safe folder name for a normalized ticker.

    Keeps [A-Za-z0-9.-] literal and escapes '_' as '_5F_' and any other punctuation as '_{HEX}_',
    guaranteeing distinct normalized symbols (e.g. CL=F vs CL_F, X@Y vs X_X40_Y) never collide.
    """
    raw = (norm_symbol or "").strip().upper()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError(f"Unsafe or empty symbol for archive directory: {norm_symbol!r}")
    if raw in {".", ".."} or raw.startswith("."):
        raise ValueError(f"Reserved or hidden path segment not allowed as symbol: {norm_symbol!r}")

    encoded_chars: list[str] = []
    for ch in raw:
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in {".", "-"}:
            encoded_chars.append(ch)
        else:
            encoded_chars.append(f"_{ord(ch):02X}_")
    safe = "".join(encoded_chars)
    if not safe or safe in {".", ".."} or safe.startswith("."):
        raise ValueError(f"Symbol produced unsafe directory name: {norm_symbol!r} -> {safe!r}")
    return safe


def resolve_archive_dir(
    raw_symbol: str,
    research_root: str | None = None,
    market_override: str | None = None,
) -> tuple[str, str, str, str]:
    """Return (archive_dir, market, norm_symbol, asset_class) with strict root containment checks."""
    raw_clean = (raw_symbol or "").strip()
    if not raw_clean or "\\" in raw_clean or ".." in raw_clean or raw_clean in {".", ".."}:
        raise ValueError(f"Unsafe or empty symbol: {raw_symbol!r}")
    if "/" in raw_clean and not re.fullmatch(r"[A-Za-z0-9]+/[A-Za-z0-9]+", raw_clean):
        raise ValueError(f"Path separator not allowed in symbol: {raw_symbol!r}")

    if market_override is not None:
        market = str(market_override).strip().upper()
        if market not in VALID_MARKETS:
            raise ValueError(f"Invalid market_override {market_override!r}; expected one of {sorted(VALID_MARKETS)}")
        if market == "US" and ":" not in raw_clean:
            norm_sym, asset_class = normalize_symbol(f"US:{raw_clean}")
        else:
            norm_sym, asset_class = normalize_symbol(raw_clean)
    else:
        norm_sym, asset_class = normalize_symbol(raw_clean)
        market = ASSET_CLASS_TO_MARKET.get(asset_class, "US")

    safe_sym = safe_symbol_dir_name(norm_sym)
    root = os.path.abspath(research_root or DEFAULT_RESEARCH_DIR)
    root_real = os.path.realpath(root)
    folder = os.path.join(root, market, safe_sym)
    folder_real = os.path.realpath(folder)
    if os.path.commonpath([root_real, folder_real]) != root_real or folder_real == root_real:
        raise ValueError(f"Resolved archive path escapes research root: {folder}")
    return folder, market, norm_sym, asset_class


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


def _atomic_write_json(path: str, obj: dict) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _apply_wal_unlocked(folder: str) -> None:
    """Idempotently roll forward any committed .thesis_wal.json under _archive_lock (never swallows I/O errors)."""
    wal_path = os.path.join(folder, ".thesis_wal.json")
    if not os.path.exists(wal_path):
        return
    with open(wal_path, "r", encoding="utf-8") as f:
        wal = json.load(f)
    if not isinstance(wal, dict) or not isinstance(wal.get("thesis_md"), str) or not isinstance(wal.get("state"), dict):
        raise ValueError(f"Corrupted thesis WAL at {wal_path}")
    _atomic_write_text(os.path.join(folder, "thesis.md"), wal["thesis_md"])
    _atomic_write_json(os.path.join(folder, "state.json"), wal["state"])
    os.unlink(wal_path)


_DIRECTION_LABELS = {
    "long": "做多 (long)",
    "short": "做空 (short)",
    "neutral": "待定 (neutral)",
    "pair": "配对 (pair)",
}


def _normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid status {value!r}; expected one of {sorted(VALID_STATUSES)}")
    norm = value.strip().lower()
    if norm not in VALID_STATUSES:
        raise ValueError(f"Invalid status {value!r}; expected one of {sorted(VALID_STATUSES)}")
    return norm


def _normalize_direction_bias(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid direction_bias {value!r}; expected one of {sorted(VALID_DIRECTIONS)}")
    norm = value.strip().lower()
    if norm not in VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction_bias {value!r}; expected one of {sorted(VALID_DIRECTIONS)}")
    return norm


def _normalize_confidence(value: Any) -> str | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            f"Invalid confidence {value!r}; expected one of {sorted(VALID_CONFIDENCE_LEVELS)} or a number in [0.0, 1.0]"
        )
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in VALID_CONFIDENCE_LEVELS:
            return norm
        raise ValueError(
            f"Invalid confidence {value!r}; expected one of {sorted(VALID_CONFIDENCE_LEVELS)} or a number in [0.0, 1.0]"
        )
    if isinstance(value, (int, float)):
        fval = float(value)
        if not math.isfinite(fval) or fval < 0.0 or fval > 1.0:
            raise ValueError(f"Invalid numeric confidence {value!r}; expected finite number in [0.0, 1.0]")
        return round(fval, 4)
    raise ValueError(
        f"Invalid confidence {value!r}; expected one of {sorted(VALID_CONFIDENCE_LEVELS)} or a number in [0.0, 1.0]"
    )


def _default_expectation_gap() -> dict[str, Any]:
    return {
        "priced_in_consensus": "",
        "our_variant_view": "",
        "crowding_assessment": "unknown",
        "leading_kpis": [],
    }


def normalize_expectation_gap(raw: Any, *, strict: bool = False) -> dict[str, Any]:
    """Validate and normalize cross-asset structured second-order expectation gap."""
    if raw is None:
        return _default_expectation_gap()
    if not isinstance(raw, dict):
        raise ValueError(f"Field 'expectation_gap' must be a JSON object or null, got {raw!r}")

    if strict:
        unknown_top = sorted(str(k) for k in raw if k not in EXPECTATION_GAP_CANONICAL_KEYS)
        if unknown_top:
            raise ValueError(
                f"expectation_gap contains unknown keys: {unknown_top}; "
                f"expected subset of {list(EXPECTATION_GAP_CANONICAL_KEYS)}"
            )

    if "priced_in_consensus" in raw:
        raw_consensus = raw["priced_in_consensus"]
        if not isinstance(raw_consensus, str):
            raise ValueError(
                f"Field 'expectation_gap.priced_in_consensus' must be a string, got {raw_consensus!r}"
            )
        priced_in_consensus = raw_consensus.strip()
    else:
        priced_in_consensus = ""

    if "our_variant_view" in raw:
        raw_variant = raw["our_variant_view"]
        if not isinstance(raw_variant, str):
            raise ValueError(
                f"Field 'expectation_gap.our_variant_view' must be a string, got {raw_variant!r}"
            )
        our_variant_view = raw_variant.strip()
    else:
        our_variant_view = ""

    if "crowding_assessment" in raw:
        raw_crowding = raw["crowding_assessment"]
        if not isinstance(raw_crowding, str):
            raise ValueError(
                f"Invalid expectation_gap.crowding_assessment {raw_crowding!r}; "
                f"expected one of {sorted(VALID_CROWDING_ASSESSMENTS)}"
            )
        crowding_assessment = raw_crowding.strip().lower()
        if crowding_assessment not in VALID_CROWDING_ASSESSMENTS:
            raise ValueError(
                f"Invalid expectation_gap.crowding_assessment {raw_crowding!r}; "
                f"expected one of {sorted(VALID_CROWDING_ASSESSMENTS)}"
            )
    else:
        crowding_assessment = "unknown"

    leading_kpis: list[dict[str, str]] = []
    if "leading_kpis" in raw:
        raw_kpis = raw["leading_kpis"]
        if not isinstance(raw_kpis, list):
            raise ValueError("Field 'expectation_gap.leading_kpis' must be a JSON array")
        if len(raw_kpis) > MAX_EXPECTATION_GAP_KPIS:
            raise ValueError(
                f"Field 'expectation_gap.leading_kpis' allows at most {MAX_EXPECTATION_GAP_KPIS} items, "
                f"got {len(raw_kpis)}"
            )
        for idx, item in enumerate(raw_kpis):
            if not isinstance(item, dict):
                raise ValueError(f"expectation_gap.leading_kpis[{idx}] must be a JSON object")
            if strict:
                unknown_kpi = sorted(str(k) for k in item if k not in EXPECTATION_GAP_KPI_ALLOWED_KEYS)
                if unknown_kpi:
                    raise ValueError(
                        f"expectation_gap.leading_kpis[{idx}] contains unknown keys: {unknown_kpi}; "
                        f"expected subset of {list(EXPECTATION_GAP_KPI_ALLOWED_KEYS)}"
                    )
            raw_name = item.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"expectation_gap.leading_kpis[{idx}].name must be a non-empty string")
            clean_kpi: dict[str, str] = {"name": raw_name.strip()}
            for opt_key in EXPECTATION_GAP_KPI_OPTIONAL_KEYS:
                if opt_key in item and item[opt_key] is not None:
                    opt_val = item[opt_key]
                    if not isinstance(opt_val, str):
                        raise ValueError(
                            f"expectation_gap.leading_kpis[{idx}].{opt_key} must be a string, got {opt_val!r}"
                        )
                    clean_kpi[opt_key] = opt_val.strip()
                else:
                    clean_kpi[opt_key] = ""
            leading_kpis.append(clean_kpi)

    return {
        "priced_in_consensus": priced_in_consensus,
        "our_variant_view": our_variant_view,
        "crowding_assessment": crowding_assessment,
        "leading_kpis": leading_kpis,
    }


def _format_header_meta_lines(
    direction_bias: str,
    primary_driver: str,
    secondary_driver: str,
    confidence: str | float | None,
) -> str:
    lines: list[str] = []
    if primary_driver or secondary_driver:
        lines.append(
            f"- **第一层驱动维度**：主驱动 `{primary_driver or '待补充'}` / 次驱动 `{secondary_driver or '待补充'}`"
        )
    if direction_bias != "neutral" or confidence is not None:
        dir_label = _DIRECTION_LABELS.get(direction_bias, direction_bias)
        conf_suffix = f" / 置信度 `{confidence}`" if confidence is not None else ""
        lines.append(f"- **初始研究方向**：`{dir_label}`{conf_suffix}")
    return ("\n" + "\n".join(lines)) if lines else ""


def _format_conclusion_direction_line(
    direction_bias: str,
    confidence: str | float | None,
    default_hint: str,
) -> str:
    if direction_bias == "neutral" and confidence is None:
        return default_hint
    dir_label = _DIRECTION_LABELS.get(direction_bias, direction_bias)
    conf_suffix = f"；置信度：{confidence}" if confidence is not None else ""
    return f"{dir_label}{conf_suffix}（核心 `L1/L2` 证据缺口清单待核验）"


def _build_commodity_template(
    symbol: str,
    market: str,
    today: str,
    title_name: str,
    subtype: str,
    summary_line: str,
    direction_bias: str = "neutral",
    primary_driver: str = "",
    secondary_driver: str = "",
    confidence: str | float | None = None,
) -> str:
    extra_meta = _format_header_meta_lines(direction_bias, primary_driver, secondary_driver, confidence)
    conclusion_dir = _format_conclusion_direction_line(
        direction_bias, confidence, "待定（多 / 空 / 中性 / 配对；核心 `L1/L2` 证据缺口清单）"
    )
    return f"""# [{symbol}]{title_name} · 大宗商品深度投研与交易档案

- **市场 / 类别**：`{market}` / `{subtype or "待分类(能源/有色/黑色/农产/化工新材料/航运)"}`
- **建档日期**：`{today}`{extra_meta}
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1Q–1Y 供需平衡表与交易结构真相，完成独立红蓝对抗并给出客观研究裁决，**不决定入场价、合约选择与仓位大小**。

### 1Q–1Y 供需平衡表预期（新投产爬坡 vs 下游需求）
| 分项 | 上季(实际) | 当季(估) | Q+1(预) | Q+2(预) | Q+3–Q+4(预) | 关键假设 / 口径 / 来源 |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| 存量产能 × 开工率 | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | |
| (+) 新增投产 / 爬坡净增量 | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | 按重点项目逐一加总 |
| (-) 减产 / 资源国政策扰动 | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | 落地概率 × 影响量加权 |
| **总供给** | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | |
| 下游需求分拆 (A / B / C) | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | |
| **总需求** | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | |
| **供需差（供给 - 需求）** | 待检索 | 待检索 | 待检索 | 待检索 | 待检索 | 负=缺口 / 正=过剩 |

### 资源国政策叙事追踪（区分“抢出口短松”与“永久减量长紧”）
| 国家 | 政策类型 | 关键生效日期 | 供给影响估计 | 确定性 | 硬验证指标（海关/产量） |
| :--- | :--- | :--- | :--- | :---: | :--- |
| 待补充 | | | | | |

### 交易结构验真（库存 / 基差月差 / 国内外价差 / 海运费）
- **显性 / 隐性库存变化**：待拉取 `research_data.py`（交易所显性库存 vs 社会/保税区/在途隐性库存）
- **现货基差与年化展期收益/损耗（Roll Yield）**：待拉取（标注近远月 Back/Contango 结构及年化展期收益/损耗）
- **跨市价差与进口比价**：待拉取（全球联动品种核对汇率/税率折算后的内外盘价差与进口比价；纯内盘品种填“不适用”）
- **海运费（BDTI / BCTI / BDI / BCI / BSI）与 CFTC 持仓**：待拉取（海运依赖品种跟踪波罗的海运价领先信号；海外品种核对 CFTC 分类持仓净多空分位）
- **行业现金成本分位**：待核验（90% / 75% 分位现金成本线与高成本产能复产阈值）

### 红蓝辩论反方必答 5 问
1. **隐性库存黑箱**：交易所外社会/保税区库存是否与账面缺口背离？
2. **抢出口与节奏错配**：政策生效前是否存在抢出口冲库打压近月？
3. **高成本复产弹性**：价格涨到什么位置会触发停产产能快速复产？
4. **需求替代与负反馈**：高价是否会摧毁下游排产或引发替代？
5. **供需与交易结构证伪阈值**：库存趋势、现货基差/月差结构或行业现金成本支撑出现什么量化逆转即证伪原论点？（具体日线 `ZD/ZG` 止损位交由第四层设定）

### 第三层客观研究结论（不含下单与仓位决策）
- **研究方向与置信度**：{conclusion_dir}
- **核心风险与证伪条件**：待定

---

## 第四层：交易表达、缠论执行与触发器监控
> 定位：交易员 + 风控官层——以第二层**日线定方向**、第三层定纯研究结论，在 **30min 缠论结构中诞生交易**，落脚到工具选择、入场止损、龙头仓位风控与持仓每日监控（多空双向对称）。

- **四象限终裁与状态**：待同步 `state.json`（放行做多 / 放行做空 / 同板块配对 / 逻辑待观察 / 否决）
- **最优交易表达与合约选择**：待定（**大 B 结构绝非做空禁区**——显式识别“短期低库存/大 B 结构（强现实）vs 未来过剩（弱预期）”劈叉，量化展期成本与近月挤仓风险，在近远月合约、跨市价差或期权中择优）
- **日线定方向 + 30min 缠论执行计划**：日线主方向与 `[ZD, ZG]` 大框架 + **30min 缠论精确买卖点**、分批建仓位、30min 紧凑战术止损（减少日线噪点浮亏损耗）、日线结构失效位、目标区间（盈亏比 `>= 2.5`）与风险预算仓位
- **单一板块龙头独占制与事件纪律**：确认本标的为所属板块唯一真龙头/引领标的（不碰跟风二三线；仅龙头切换时换仓，或第一第二不明显时最多 2 只共享板块风险额度），并明确主力移仓展期与 OPEC+/WASDE 等重大事件规则
- **持仓每日监控与果断撤离（同步 `state.json`）**：覆盖日线/30min 缠论防线 + 期限结构(`term_structure`)/库存/海运费(`freight`)/跨市价差(`spread`) + 供需政策 KPI；**持仓标的每日必检，核心逻辑或关键指标一旦松动随时果断撤离**

---

## 增量修订日志（只追加，不重写）
- `{today}`：初始化商品研究档案。
"""


def _build_fx_template(
    symbol: str,
    market: str,
    today: str,
    title_name: str,
    subtype: str,
    summary_line: str,
    direction_bias: str = "neutral",
    primary_driver: str = "",
    secondary_driver: str = "",
    confidence: str | float | None = None,
) -> str:
    extra_meta = _format_header_meta_lines(direction_bias, primary_driver, secondary_driver, confidence)
    conclusion_dir = _format_conclusion_direction_line(
        direction_bias, confidence, "待定（多 / 空 / 中性 / 交叉盘配对；核心 `L1/L2` 证据缺口清单）"
    )
    return f"""# [{symbol}]{title_name} · 外汇（FX）深度投研与交易档案

- **市场 / 类别**：`{market}` / `{subtype or "待分类(G10利差/避险/商品货币/新兴市场)"}`
- **建档日期**：`{today}`{extra_meta}
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1Q–1Y 利差周期、持仓结构与宏观/贸易驱动逻辑，完成独立红蓝对抗并给出客观研究裁决，**不决定入场价、工具选择与仓位大小**。

### 利差与货币政策路径（10Y/2Y 利差与通胀预期）
| 维度 | 基准货币（左侧） | 计价货币（右侧） | 利差 / 相对变化 | 1Q–1Y 政策路径预期 | 口径 / 来源 |
| :--- | :---: | :---: | :---: | :--- | :--- |
| 政策利率与 OIS 隐含路径 | 待检索 | 待检索 | 待算 | 待检索 | 央行纪要 / L1 |
| 10Y / 2Y 国债收益率与利差 | 待拉取 | 待拉取 | 待算 | 利差走阔/收窄驱动 | FRED / 东财中美国债 |
| 实际利率与通胀预期（`DFII10` / `T10YIE`） | 待拉取 | 待检索 | 待算 | 核心通胀粘性与实际利差 | FRED |

### CFTC 金融期货持仓（TFF 杠杆基金/资管）与套息交易拥挤度
- **TFF 分类持仓净头寸与分位**：待拉取 `research_data.py`（核对 `FinFutWk` 杠杆基金 Leveraged Funds 与资管 Asset Managers 净多空；**反向报价货币对如 `USDJPY` / `USDCAD` 须按标注自动换算方向**）
- **套息交易（Carry Trade）拥挤度与平仓风险**：待核验（利差年化 Carry vs 波动率比、跨市场风险偏好与 VIX 飙升时的平仓踩踏风险）

### 贸易/经常账户与地缘催化
- **贸易条件（Terms of Trade）与经常账户**：待检索（大宗商品/能源净出口冲击、制造业顺差与跨境资本流）
- **央行干预与地缘事件日历**：待检索（财务省/央行外汇干预红线、关税与资本管制、FOMC/BOJ/ECB/CPI/非农时间表）

### 红蓝辩论反方必答 5 问（外汇版）
1. **利差预期差反转**：若双方央行未来 1 年政策路径反向重定价 50bp，汇率均衡锚将偏移多少？
2. **持仓拥挤与套息踩踏**：CFTC TFF 杠杆基金与套息头寸是否已处于历史极端分位，存在风险厌恶引发的反向踩踏？
3. **央行干预与政策红线**：汇价逼近哪些历史关口可能触发实盘干预、口头警示或货币政策意外转向？
4. **贸易与外部冲击**：能源/商品价格剧烈波动或关税壁垒是否会逆转经常账户与利差传导链条？
5. **核心宏观与利差证伪阈值**：10Y/2Y 利差、通胀路径或国际收支出现什么量化逆转即证伪原论点？（具体日线 `ZD/ZG` 止损位交由第四层设定）

### 第三层客观研究结论（不含下单与仓位决策）
- **研究方向与置信度**：{conclusion_dir}
- **核心风险与证伪条件**：待定

---

## 第四层：交易表达、缠论执行与触发器监控
> 定位：交易员 + 风控官层——以第二层**日线定方向**、第三层定纯研究结论，在 **30min 缠论结构中诞生交易**，落脚到直盘/交叉盘表达、紧凑止损、龙头仓位风控与持仓每日监控（多空双向对称）。

- **四象限终裁与状态**：待同步 `state.json`（放行做多 / 放行做空 / 配对 / 逻辑待观察 / 否决）
- **最优交易表达与货币对选择**：待定（直盘 vs 交叉盘剔除美元 Beta、隔夜掉期 Carry 正负损耗及期权结构评估）
- **日线定方向 + 30min 缠论执行计划**：日线主方向与 `[ZD, ZG]` 大框架 + **30min 缠论精确买卖点**、分批建仓位、30min 紧凑战术止损（减少日线噪点浮亏损耗）、日线结构失效位、目标区间（盈亏比 `>= 2.5`）与风险预算仓位
- **单一主线引领货币对制与宏大事件纪律**：同一货币/利差逻辑只选传导最通畅的核心货币对（不重复堆砌高度相关直盘），明确 FOMC/BOJ/ECB/CPI/非农等宏大事件前仓位规则
- **持仓每日监控与果断撤离（同步 `state.json`）**：覆盖日线/30min 缠论防线 + 10Y 利差(`rate_diff`) + CFTC TFF 持仓与政策事件；**持仓标的每日必检，核心逻辑或关键指标一旦松动随时果断撤离**

---

## 增量修订日志（只追加，不重写）
- `{today}`：初始化外汇研究档案。
"""


def _build_equity_template(
    symbol: str,
    market: str,
    today: str,
    title_name: str,
    subtype: str,
    summary_line: str,
    direction_bias: str = "neutral",
    primary_driver: str = "",
    secondary_driver: str = "",
    confidence: str | float | None = None,
) -> str:
    extra_meta = _format_header_meta_lines(direction_bias, primary_driver, secondary_driver, confidence)
    conclusion_dir = _format_conclusion_direction_line(
        direction_bias, confidence, "待定（中长线以做多为主；列明 `L1/L2` 证据支撑与待核验项）"
    )
    return f"""# [{symbol}]{title_name} · 股票深度投研与交易档案（1–3 年预期差框架）

- **市场 / 资产分类**：`{market}` / `{subtype or "待分类(A成长/B强周期/C稳定现金流/D困境反转)"}`
- **建档日期**：`{today}`{extra_meta}
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1–3 年远期盈利与估值预期差，完成一手财报核验与独立红蓝对抗，输出客观研究裁决，**不决定入场价、表达方式与仓位大小**。

### 1–3 年远期预期拆解与预期差
- **定型、受益纯度与瓶颈久期**：待定（A 成长 / B 强周期 / C 稳定现金流 / D 困境反转；量化核心逻辑真实收入占比；区分材料/工艺 5–10 年、客户验证 2–5 年[防大客户自研绕过]、纯产能缺货 1–3 年[按 B 型周期股处理]）
- **估值锚选择**：（成长看前瞻 1–3Y EPS×PE 与二阶增速；强周期看中周期正常化盈利与产能拐点，严禁景气顶峰因静态低 PE 接飞刀；A 股结合 5 年 PE(TTM)/PB 分位，美股结合一致预期修正与空头比例）
- **收入驱动树与分部三拆**：待拆解（量 × 价 × 高毛利 Mix Shift − 衰退老业务 Drag Segment；如指引压向 H2 放量须按 H1 实绩倒推 H2 隐含环比增速）
- **预期差来源**：一致预期 vs 我们的判断 vs 验证时间点（说不出预期差最多进观察池）

#### 情景压力测试与现价隐含预期拆解（EPS × PE，强制标注 GAAP / Adjusted 口径）
| 情景 | 盈利假设 (EPS / 口径) | 估值倍数 (PE) | 目标价 | 相对现价空间 |
| :--- | :--- | :---: | :---: | :---: |
| **牛情景** | 待核验 | 待核验 | 待算 | |
| **基准情景** | 待核验 | 待核验 | 待算 | |
| **熊1（估值回归）** | EPS 持平 | 回到 5 年中枢 | 待算 | |
| **熊2（戴维斯双杀）** | EPS -10% | 进一步压缩 | 待算 | |
| **叙事崩塌** | 逻辑证伪/订单取消 | 周期谷底估值 | 待算 | |

- **现价隐含预期拆解（仅作风险暴露描述，非入场门槛，严禁替代第四层缠论决策）**：常态基础价值区间 + 有限期周期超额利润折现（不可乘成长股 PE） + 叙事/期权残差区间。

### 一手财报与订单核验清单（10 项，缺项标“待核验”）
1. **指引与 EPS 口径（GAAP vs Adjusted）**：待核验
2. **订单七阶段审计与 NRE 剥离**：待核验（科学验证 / 产品导入 / 客户验证 / 具约束力 PO / 量产爬坡 / 稳定复购 / 未证实传闻；剥离一次性工程费 NRE 与可放量产品收入；拆分硬 RPO 与框架预估）
3. **分部结构（Mix Shift / Drag Segment）与 H2 放量指引桥**：待核验
4. **合同类型（固定总价占比与利润率 ±1pp 敏感度）**：待核验
5. **客户/单一需求链集中度与自研替代风险**：待核验
6. **现金流质量与营运资金（FCF/净利、存货与应收相对营收增速）**：待核验
7. **期后 8-K 净现金与净债务桥**：待核验（核对季报截止日后的并购交割、新增债务、受限现金与流动性跑道）
8. **股本稀释与真实回购弹药**：待核验（3–5 年期末股数趋势、SBC、ATM/可转债/Warrants；回购按 Rule 10b-18 日均成交额 ≤25% 折算，CFO 最新表态高于历史规律）
9. **周期顶部双面核验（如适用）**：待核验（交期/Book-to-Bill/长协/配给是否隐含重复下单，交叉核对月度营收、现货价与“利好不创新高”）
10. **债务到期结构与利率敏感度**：待核验

### 红蓝辩论反方必答 7 问 + 红队 Pre-Mortem（核心论据须有 L1/L2 证据）
1. EPS 持平、PE 回归历史中枢跌多少？
2. 按订单七阶段剔除软订单/框架预估与 NRE 后，真实可放量增速剩多少（含衰退老业务与 H2 放量风险）？
3. 利润率压缩 1pp 对 EPS 冲击多大？
4. 预期差与现价叙事溢价是否已被当前股价充分定价？
5. 最大单一客户或需求链断供/自研替代会怎样？
6. 期后现金消耗、债务或 ATM/SBC 股本稀释是否侵蚀股东权益？
7. 该论点历史上最近一次被证伪的案例是什么？核心领先证伪指标与预期差失效阈值在哪？（具体日线/30min 止损位交由第四层设定）

#### 红队 Pre-Mortem（假设 6 个月后此笔头寸大亏，提前写死 3–5 条具体失败路径）
- 失败路径 1：待填写（具体触发事件 → 传导后果 → 对应领先监控指标）
- 失败路径 2：待填写
- 失败路径 3：待填写

### 第三层客观研究结论（不含下单与仓位决策）
- **研究方向与置信度**：{conclusion_dir}
- **核心风险与证伪条件**：待定

---

## 第四层：交易表达、缠论执行与触发器监控
> 定位：交易员 + 风控官层——以第二层**日线定方向**、第三层定纯研究结论，在 **30min 缠论结构中诞生交易**，落脚到龙头表达、紧凑止损、仓位风控与持仓每日监控。

- **四象限终裁与状态**：待同步 `state.json`（放行做多 / 逻辑待观察 / 同板块配对 / 否决）
- **最优交易表达**：待定（正股分批建仓 vs 同产业链龙头配对 vs 财报前后风控对冲）
- **日线定方向 + 30min 缠论执行计划**：日线主方向与 `[ZD, ZG]` 大框架 + **30min 缠论精确买卖点**、分批建仓位、30min 紧凑战术止损（减少日线噪点浮亏损耗）、日线结构失效位、目标区间（盈亏比 `>= 2.5`）与风险预算仓位
- **单一板块龙头独占制与财报事件纪律**：确认本标的为所属板块唯一真龙头/引领标的（二三线跟风一律不碰不看；仅龙头切换时换仓，或第一第二不明显时最多 2 只共享板块风险额度），并明确下次财报日窗口风控规则
- **持仓每日监控与果断撤离（同步 `state.json`）**：覆盖日线/30min 缠论防线 + 季度核心 KPI/一致预期修正；**持仓标的每日必检，核心逻辑或关键指标一旦松动随时果断撤离**

---

## 增量修订日志（只追加，不重写）
- `{today}`：初始化股票研究档案。
"""


def render_thesis_template(
    symbol: str,
    market: str,
    asset_class: str,
    name: str = "",
    subtype: str = "",
    thesis_summary: str = "",
    direction_bias: str = "neutral",
    primary_driver: str = "",
    secondary_driver: str = "",
    confidence: str | float | None = None,
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_name = f" ({name})" if name else ""
    summary_line = thesis_summary or "待补充：一句话核心交易论点与 1Q–1Y 预期差来源。"

    if market == "FX" or asset_class == "fx":
        return _build_fx_template(
            symbol=symbol,
            market=market,
            today=today,
            title_name=title_name,
            subtype=subtype,
            summary_line=summary_line,
            direction_bias=direction_bias,
            primary_driver=primary_driver,
            secondary_driver=secondary_driver,
            confidence=confidence,
        )

    if asset_class in ("commodity", "cn_future") or market in ("COMMODITIES", "CN_FUTURES"):
        return _build_commodity_template(
            symbol=symbol,
            market=market,
            today=today,
            title_name=title_name,
            subtype=subtype,
            summary_line=summary_line,
            direction_bias=direction_bias,
            primary_driver=primary_driver,
            secondary_driver=secondary_driver,
            confidence=confidence,
        )

    return _build_equity_template(
        symbol=symbol,
        market=market,
        today=today,
        title_name=title_name,
        subtype=subtype,
        summary_line=summary_line,
        direction_bias=direction_bias,
        primary_driver=primary_driver,
        secondary_driver=secondary_driver,
        confidence=confidence,
    )


def _default_state(
    *,
    next_revision: int,
    market: str,
    norm_sym: str,
    name: str,
    asset_class: str,
    subtype: str,
    cik: str | None,
    norm_dir: str,
    norm_conf: str | float | None,
    eff_primary: str,
    eff_secondary: str,
    clean_summary: str,
    now: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": next_revision,
        "instrument": {
            "market": market,
            "symbol": norm_sym,
            "name": name,
            "asset_class": asset_class,
            "subtype": subtype,
            "cik": cik,
        },
        "status": "watchlist",
        "direction_bias": norm_dir,
        "confidence": norm_conf,
        "layer1_drivers": {
            "primary": eff_primary,
            "secondary": eff_secondary,
        },
        "thesis_summary": clean_summary or "待补充核心论点",
        "expectation_gap": _default_expectation_gap(),
        "chanlun_anchors": {
            "as_of_bar": None,
            "zhongshu_level": None,
            "zd": None,
            "zg": None,
            "tactical_stop": None,
            "structural_invalidation": None,
            "target_zone": None,
        },
        "valuation_or_balance": {},
        "triggers": [],
        "pending_verifications": [],
        "events": [],
        "source_watermarks": {},
        "created_at_utc": now,
        "updated_at_utc": now,
    }


def init_archive(
    raw_symbol: str,
    name: str = "",
    subtype: str = "",
    thesis_summary: str = "",
    primary_driver: str = "",
    secondary_driver: str = "",
    cik: str | None = None,
    research_root: str | None = None,
    market_override: str | None = None,
    overwrite: bool = False,
    direction_bias: str = "neutral",
    layer1_primary: str | None = None,
    layer1_secondary: str | None = None,
    confidence: str | float | None = None,
) -> dict:
    """Create research/<MARKET>/<SYMBOL>/{thesis.md, evidence.jsonl, state.json} atomically under lock."""
    norm_dir = _normalize_direction_bias(direction_bias)
    norm_conf = _normalize_confidence(confidence)
    if layer1_primary is not None and not isinstance(layer1_primary, str):
        raise ValueError(f"Field 'layer1_primary' must be a string, got {layer1_primary!r}")
    if layer1_secondary is not None and not isinstance(layer1_secondary, str):
        raise ValueError(f"Field 'layer1_secondary' must be a string, got {layer1_secondary!r}")
    if thesis_summary is not None and not isinstance(thesis_summary, str):
        raise ValueError(f"Field 'thesis_summary' must be a string, got {thesis_summary!r}")

    eff_primary = (
        layer1_primary.strip()
        if isinstance(layer1_primary, str) and layer1_primary.strip()
        else str(primary_driver or "").strip()
    )
    eff_secondary = (
        layer1_secondary.strip()
        if isinstance(layer1_secondary, str) and layer1_secondary.strip()
        else str(secondary_driver or "").strip()
    )
    clean_summary = (thesis_summary or "").strip()

    folder, market, norm_sym, asset_class = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    state_path = os.path.join(folder, "state.json")
    thesis_path = os.path.join(folder, "thesis.md")
    evidence_path = os.path.join(folder, "evidence.jsonl")

    with _archive_lock(folder):
        if os.path.exists(state_path) and os.path.exists(thesis_path) and os.path.exists(evidence_path) and not overwrite:
            with open(state_path, "r", encoding="utf-8") as f:
                existing_state = json.load(f)
            if isinstance(existing_state, dict):
                try:
                    existing_state["expectation_gap"] = normalize_expectation_gap(
                        existing_state.get("expectation_gap")
                    )
                except ValueError:
                    existing_state["expectation_gap"] = _default_expectation_gap()
            return {"status": "exists", "archive_dir": folder, "state": existing_state}

        next_revision = 1
        if overwrite and os.path.exists(state_path):
            try:
                _apply_wal_unlocked(folder)
                with open(state_path, "r", encoding="utf-8") as f:
                    prev_state = json.load(f)
                prev_rev = prev_state.get("revision") if isinstance(prev_state, dict) else None
                if isinstance(prev_rev, int) and not isinstance(prev_rev, bool) and prev_rev >= 1:
                    next_revision = prev_rev + 1
            except (OSError, ValueError, TypeError):
                next_revision = 1

        now = utc_now_str()
        state = _default_state(
            next_revision=next_revision,
            market=market,
            norm_sym=norm_sym,
            name=name,
            asset_class=asset_class,
            subtype=subtype,
            cik=cik,
            norm_dir=norm_dir,
            norm_conf=norm_conf,
            eff_primary=eff_primary,
            eff_secondary=eff_secondary,
            clean_summary=clean_summary,
            now=now,
        )
        # Backup raw bytes (preserves CRLF/exact bytes) and attempt every file rollback independently
        backups: dict[str, bytes | None] = {}
        for p in (thesis_path, evidence_path, state_path):
            if os.path.exists(p):
                with open(p, "rb") as bf:
                    backups[p] = bf.read()
            else:
                backups[p] = None

        try:
            _atomic_write_text(
                thesis_path,
                render_thesis_template(
                    symbol=norm_sym,
                    market=market,
                    asset_class=asset_class,
                    name=name,
                    subtype=subtype,
                    thesis_summary=clean_summary,
                    direction_bias=norm_dir,
                    primary_driver=eff_primary,
                    secondary_driver=eff_secondary,
                    confidence=norm_conf,
                ),
            )
            if not os.path.exists(evidence_path) or overwrite:
                _atomic_write_text(evidence_path, "")
            _atomic_write_json(state_path, state)
        except Exception:
            for p, old_bytes in backups.items():
                try:
                    if old_bytes is None:
                        if os.path.exists(p):
                            os.unlink(p)
                    else:
                        _atomic_write_bytes(p, old_bytes)
                except Exception:  # noqa: BLE001, S110
                    pass
            raise

        return {"status": "created", "archive_dir": folder, "state": state}


def load_archive(
    raw_symbol: str,
    research_root: str | None = None,
    market_override: str | None = None,
    evidence_limit: int = 50,
) -> dict:
    """Load state.json, recent evidence items, and thesis.md path/text."""
    folder, market, norm_sym, asset_class = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    state_path = os.path.join(folder, "state.json")
    thesis_path = os.path.join(folder, "thesis.md")
    evidence_path = os.path.join(folder, "evidence.jsonl")

    if not os.path.exists(state_path):
        raise FileNotFoundError(f"Research archive not found for {raw_symbol} at {folder}")

    with _archive_lock(folder):
        _apply_wal_unlocked(folder)
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            try:
                state["expectation_gap"] = normalize_expectation_gap(state.get("expectation_gap"))
            except ValueError:
                state["expectation_gap"] = _default_expectation_gap()

        evidence: list[dict] = []
        if os.path.exists(evidence_path):
            with open(evidence_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        evidence.append(json.loads(s))

        thesis_text = ""
        if os.path.exists(thesis_path):
            with open(thesis_path, "r", encoding="utf-8") as f:
                thesis_text = f.read()

    return {
        "archive_dir": folder,
        "market": market,
        "symbol": norm_sym,
        "asset_class": asset_class,
        "state": state,
        "evidence_count": len(evidence),
        "recent_evidence": evidence[-evidence_limit:] if evidence_limit > 0 else evidence,
        "thesis_path": thesis_path,
        "thesis_md": thesis_text,
    }


def _evidence_fingerprint(item: dict) -> str:
    key_fields = {
        "metric": item.get("metric"),
        "value": item.get("value"),
        "unit": item.get("unit"),
        "period": item.get("period"),
        "basis": item.get("basis"),
        "kind": item.get("kind"),
        "level": item.get("level"),
        "source_url": item.get("source_url"),
        "source_locator": item.get("source_locator"),
    }
    raw = json.dumps(key_fields, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def append_evidence(
    raw_symbol: str,
    items: list[dict],
    research_root: str | None = None,
    market_override: str | None = None,
) -> dict:
    """Validate all items first, then atomically append deduplicated L1-L4 items under lock."""
    folder, _, _, _ = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    state_path = os.path.join(folder, "state.json")
    evidence_path = os.path.join(folder, "evidence.jsonl")
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"Archive not initialized at {folder}")

    # Phase 1: strictly validate ALL items in memory before any I/O mutation
    normalized_inputs: list[dict] = []
    now = utc_now_str()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("Each evidence item must be a JSON object")
        level = str(raw_item.get("level", "L4")).upper()
        if level not in VALID_EVIDENCE_LEVELS:
            raise ValueError(f"Invalid evidence level {level!r}; expected one of {sorted(VALID_EVIDENCE_LEVELS)}")
        kind = str(raw_item.get("kind", "fact")).lower()
        if kind not in VALID_EVIDENCE_KINDS:
            raise ValueError(f"Invalid evidence kind {kind!r}; expected one of {sorted(VALID_EVIDENCE_KINDS)}")
        metric = str(raw_item.get("metric") or "").strip()
        if not metric:
            raise ValueError("Evidence item missing required 'metric'")
        rec_proto = {
            "id": raw_item.get("id"),
            "metric": metric,
            "value": raw_item.get("value"),
            "unit": str(raw_item.get("unit", "")),
            "period": str(raw_item.get("period", "")),
            "basis": str(raw_item.get("basis", "unspecified")),
            "kind": kind,
            "level": level,
            "published_at": str(raw_item.get("published_at", "")),
            "fetched_at": str(raw_item.get("fetched_at") or now),
            "source_url": str(raw_item.get("source_url", "")),
            "source_locator": str(raw_item.get("source_locator", "")),
            "supersedes": raw_item.get("supersedes"),
            "note": str(raw_item.get("note", "")),
        }
        # Verify strict JSON serializability before acquiring lock
        json.dumps(rec_proto, ensure_ascii=False, allow_nan=False)
        normalized_inputs.append(rec_proto)

    # Phase 2: lock, read existing fingerprints, assign IDs, and write atomically
    with _archive_lock(folder):
        existing_lines: list[str] = []
        existing_fps: set[str] = set()
        if os.path.exists(evidence_path):
            with open(evidence_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    existing_lines.append(s)
                    try:
                        obj = json.loads(s)
                        existing_fps.add(obj.get("fingerprint") or _evidence_fingerprint(obj))
                    except json.JSONDecodeError:
                        continue

        added: list[dict] = []
        skipped = 0
        new_lines: list[str] = []
        base_idx = len(existing_lines)
        for proto in normalized_inputs:
            record = dict(proto)
            fp = _evidence_fingerprint(record)
            if fp in existing_fps:
                skipped += 1
                continue
            record["id"] = record["id"] or f"ev_{base_idx + len(added) + 1:04d}"
            record["fingerprint"] = fp
            existing_fps.add(fp)
            new_lines.append(json.dumps(record, ensure_ascii=False, allow_nan=False))
            added.append(record)

        if new_lines:
            all_text = "\n".join(existing_lines + new_lines) + "\n"
            _atomic_write_text(evidence_path, all_text)

    return {"added": len(added), "skipped_duplicates": skipped, "items": added}


def _validate_state_patch(patch: dict[str, Any]) -> None:
    if not isinstance(patch, dict):
        raise ValueError("Patch must be a JSON object")
    if "status" in patch:
        _normalize_status(patch["status"])
    if "direction_bias" in patch:
        _normalize_direction_bias(patch["direction_bias"])
    if "confidence" in patch:
        _normalize_confidence(patch["confidence"])
    if "thesis_summary" in patch and not isinstance(patch["thesis_summary"], str):
        raise ValueError(f"Field 'thesis_summary' must be a string, got {patch['thesis_summary']!r}")
    if "expectation_gap" in patch:
        normalize_expectation_gap(patch["expectation_gap"], strict=True)
    for shorthand_key in ("layer1_primary", "layer1_secondary"):
        if shorthand_key in patch and not isinstance(patch[shorthand_key], str):
            raise ValueError(f"Field {shorthand_key!r} must be a string, got {patch[shorthand_key]!r}")
    for dict_key in ("instrument", "layer1_drivers", "chanlun_anchors", "valuation_or_balance", "source_watermarks"):
        if dict_key in patch and not isinstance(patch[dict_key], dict):
            raise ValueError(f"Field {dict_key!r} must be a JSON object")
    if "layer1_drivers" in patch:
        for drv_k in ("primary", "secondary"):
            if drv_k in patch["layer1_drivers"] and not isinstance(patch["layer1_drivers"][drv_k], str):
                raise ValueError(
                    f"Field 'layer1_drivers.{drv_k}' must be a string, got {patch['layer1_drivers'][drv_k]!r}"
                )
    for list_key in ("triggers", "pending_verifications", "events"):
        if list_key in patch and not isinstance(patch[list_key], list):
            raise ValueError(f"Field {list_key!r} must be a JSON array")
    if "triggers" in patch:
        seen_trig_ids: set[str] = set()
        for trig in patch["triggers"]:
            if not isinstance(trig, dict):
                raise ValueError("Each trigger must be a JSON object")
            for req in ("id", "metric", "operator", "action"):
                val = trig.get(req)
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(f"Trigger field {req!r} must be a non-empty string")
            tid_clean = trig["id"].strip()
            if tid_clean in seen_trig_ids:
                raise ValueError(f"Duplicate trigger id {tid_clean!r} in triggers list")
            seen_trig_ids.add(tid_clean)
            if trig["operator"] not in VALID_OPERATORS:
                raise ValueError(f"Invalid trigger operator {trig['operator']!r}; expected one of {sorted(VALID_OPERATORS)}")
            if "kind" in trig and (not isinstance(trig["kind"], str) or trig["kind"] not in VALID_TRIGGER_KINDS):
                raise ValueError(
                    f"Invalid trigger kind {trig['kind']!r}; expected one of {sorted(VALID_TRIGGER_KINDS)}"
                )
            if "threshold" not in trig:
                raise ValueError("Trigger missing required field 'threshold'")
            if (
                "evaluation_mode" in trig
                and trig["evaluation_mode"] is not None
                and trig["evaluation_mode"] not in VALID_EVALUATION_MODES
            ):
                raise ValueError(
                    f"Invalid trigger evaluation_mode {trig['evaluation_mode']!r}; "
                    f"expected one of {sorted(VALID_EVALUATION_MODES)}"
                )
            for ttl_field in ("verification_ttl_days", "ttl_days"):
                if ttl_field in trig and trig[ttl_field] is not None:
                    ttl = trig[ttl_field]
                    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
                        raise ValueError(f"Trigger field {ttl_field!r} must be a positive integer")
            if "verification" in trig and trig["verification"] is not None:
                ver = trig["verification"]
                if not isinstance(ver, dict):
                    raise ValueError("Trigger field 'verification' must be a JSON object or null")
                if "ttl_days" in ver and ver["ttl_days"] is not None:
                    ver_ttl = ver["ttl_days"]
                    if isinstance(ver_ttl, bool) or not isinstance(ver_ttl, int) or ver_ttl < 1:
                        raise ValueError("Trigger verification field 'ttl_days' must be a positive integer")
                if ver.get("method") not in VALID_VERIFICATION_METHODS:
                    raise ValueError(
                        f"Invalid verification.method {ver.get('method')!r}; "
                        f"expected one of {sorted(VALID_VERIFICATION_METHODS)}"
                    )
                if ver.get("status") not in VALID_VERIFICATION_STATUSES:
                    raise ValueError(
                        f"Invalid verification.status {ver.get('status')!r}; "
                        f"expected one of {sorted(VALID_VERIFICATION_STATUSES)}"
                    )
                if parse_strict_utc_timestamp(ver.get("verified_at_utc")) is None:
                    raise ValueError(
                        "Trigger verification requires valid timezone-aware UTC 'verified_at_utc' "
                        "(e.g. 'YYYY-MM-DD HH:MM UTC' or ISO-8601 with timezone)"
                    )
                if (
                    "evidence_tier" in ver
                    and ver["evidence_tier"] not in (None, "")
                    and ver["evidence_tier"] not in VALID_EVIDENCE_LEVELS
                ):
                    raise ValueError(f"Invalid verification.evidence_tier {ver['evidence_tier']!r}")
                if (
                    "source_url" in ver
                    and ver["source_url"] not in (None, "")
                    and not is_valid_http_url(ver["source_url"])
                ):
                    raise ValueError(f"Invalid verification.source_url {ver['source_url']!r}")
    if "events" in patch:
        for ev in patch["events"]:
            if not isinstance(ev, dict) or not ev.get("date") or not ev.get("title"):
                raise ValueError("Each event must be an object with 'date' and 'title'")
    # Ensure strict JSON compatibility (no NaN/Inf)
    json.dumps(patch, ensure_ascii=False, allow_nan=False)


def update_state(
    raw_symbol: str,
    patch: dict[str, Any],
    expected_revision: int | None = None,
    research_root: str | None = None,
    market_override: str | None = None,
) -> dict:
    """Atomically apply a validated partial patch to state.json under per-archive lock."""
    _validate_state_patch(patch)
    folder, _, _, _ = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    state_path = os.path.join(folder, "state.json")
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"Archive not initialized at {folder}")

    with _archive_lock(folder):
        _apply_wal_unlocked(folder)
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        cur_rev = int(state.get("revision", 1))
        if expected_revision is not None and expected_revision != cur_rev:
            raise ValueError(
                f"Revision conflict for {raw_symbol}: expected revision {expected_revision}, actual {cur_rev}"
            )

        dict_merge_keys = {"instrument", "layer1_drivers", "chanlun_anchors", "valuation_or_balance", "source_watermarks"}
        normalized_patch: dict[str, Any] = {}
        shorthand_drivers: dict[str, str] = {}
        for k, v in patch.items():
            if k in ("schema_version", "revision", "created_at_utc", "updated_at_utc"):
                continue
            if k == "layer1_primary":
                shorthand_drivers["primary"] = v.strip()
            elif k == "layer1_secondary":
                shorthand_drivers["secondary"] = v.strip()
            elif k == "status":
                normalized_patch[k] = _normalize_status(v)
            elif k == "direction_bias":
                normalized_patch[k] = _normalize_direction_bias(v)
            elif k == "confidence":
                normalized_patch[k] = _normalize_confidence(v)
            elif k == "thesis_summary":
                normalized_patch[k] = v.strip()
            elif k == "layer1_drivers":
                clean_drv = {dk: (dv.strip() if isinstance(dv, str) else dv) for dk, dv in v.items()}
                normalized_patch[k] = clean_drv
            else:
                normalized_patch[k] = v

        if shorthand_drivers:
            base_drv = normalized_patch.get("layer1_drivers")
            merged_drv = dict(base_drv) if isinstance(base_drv, dict) else {}
            merged_drv.update(shorthand_drivers)
            normalized_patch["layer1_drivers"] = merged_drv

        for k, v in normalized_patch.items():
            if k == "expectation_gap":
                if v is None:
                    state["expectation_gap"] = normalize_expectation_gap(None, strict=True)
                else:
                    normalize_expectation_gap(v, strict=True)
                    try:
                        base_eg = normalize_expectation_gap(state.get("expectation_gap"))
                    except ValueError:
                        base_eg = _default_expectation_gap()
                    merged_eg = dict(base_eg)
                    for eg_k in EXPECTATION_GAP_CANONICAL_KEYS:
                        if eg_k in v:
                            merged_eg[eg_k] = v[eg_k]
                    state["expectation_gap"] = normalize_expectation_gap(merged_eg, strict=True)
            elif k in dict_merge_keys and isinstance(v, dict) and isinstance(state.get(k), dict):
                state[k] = {**state[k], **v}
            else:
                state[k] = v

        state["revision"] = cur_rev + 1
        state["updated_at_utc"] = utc_now_str()
        _atomic_write_json(state_path, state)
        return state


def append_log(
    raw_symbol: str,
    entry: str,
    research_root: str | None = None,
    market_override: str | None = None,
) -> str:
    """Append a dated revision entry to thesis.md safely under per-archive lock."""
    folder, _, _, _ = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    thesis_path = os.path.join(folder, "thesis.md")
    if not os.path.exists(thesis_path):
        raise FileNotFoundError(f"thesis.md not found at {folder}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    clean_entry = entry.strip().replace("\n", " ")
    if not clean_entry:
        raise ValueError("Log entry cannot be empty")
    line = f"- `{today}`：{clean_entry}\n"

    with _archive_lock(folder):
        _apply_wal_unlocked(folder)
        with open(thesis_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content and not content.endswith("\n"):
            content += "\n"
        _atomic_write_text(thesis_path, content + line)
    return line.strip()


_AUDIT_HEADING_RE = re.compile(
    r"^##\s*(?:增量修订日志|修订与核验日志|修订日志)(?:\s*(?:（[^）]*）|\([^)]*\)))?\s*$"
)
_OPEN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_DATED_LOG_LINE_RE = re.compile(r"^-\s+`\d{4}-\d{2}-\d{2}`[：:]")


def _update_fence_state(line: str, active: tuple[str, int] | None) -> tuple[tuple[str, int] | None, bool]:
    """CommonMark-compliant fence tracker returning (next_state, is_fence_delimiter_line)."""
    stripped = line.rstrip("\r\n")
    m = _OPEN_FENCE_RE.match(stripped)
    if not m:
        return active, False
    delim, rest = m.group(1), m.group(2)
    ch = delim[0]
    length = len(delim)
    if active is None:
        # Backtick fence info strings cannot contain backticks per CommonMark
        if ch == "`" and "`" in rest:
            return None, False
        return (ch, length), True
    open_ch, open_len = active
    if ch == open_ch and length >= open_len and rest.strip() == "":
        return None, True
    return active, False


def split_thesis_and_log(markdown_text: str) -> tuple[str, str]:
    """CommonMark fence-aware split of thesis.md into (editable_body, append_only_audit_log_section)."""
    raw = markdown_text or ""
    lines = raw.splitlines(keepends=True)
    fence_state: tuple[str, int] | None = None
    split_line_idx: int | None = None

    for idx, line in enumerate(lines):
        fence_state, is_delim = _update_fence_state(line, fence_state)
        if is_delim:
            continue
        if fence_state is None and _AUDIT_HEADING_RE.match(line.rstrip("\r\n")):
            split_line_idx = idx

    if split_line_idx is not None:
        # Include an immediately preceding horizontal rule `---` (and blank lines around it) in the tail
        cut = split_line_idx
        probe = split_line_idx - 1
        while probe >= 0 and lines[probe].strip() == "":
            probe -= 1
        if probe >= 0 and lines[probe].strip() == "---":
            cut = probe
        body = "".join(lines[:cut]).rstrip()
        tail = "".join(lines[cut:]).lstrip("\r\n")
        return body, tail

    # Fallback: preserve trailing headerless dated audit log bullets ONLY when EOF is NOT inside an unclosed fence
    if fence_state is None:
        end_idx = len(lines)
        while end_idx > 0 and lines[end_idx - 1].strip() == "":
            end_idx -= 1
        log_start = end_idx
        while log_start > 0 and _DATED_LOG_LINE_RE.match(lines[log_start - 1].strip()):
            log_start -= 1
        if log_start < end_idx:
            rescued_lines = "".join(lines[log_start:end_idx])
            body = "".join(lines[:log_start]).rstrip()
            tail = f"---\n\n## 增量修订日志（只追加，不重写）\n{rescued_lines}"
            return body, tail

    return raw.rstrip(), ""


def update_thesis_body(
    raw_symbol: str,
    body_md: str,
    expected_revision: int | None = None,
    note: str = "",
    research_root: str | None = None,
    market_override: str | None = None,
) -> dict[str, Any]:
    """Atomically update thesis body + increment revision while preserving append-only audit logs."""
    if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError("expected_revision (positive integer) is required when updating thesis body")

    clean_body, _ = split_thesis_and_log(body_md)
    if not clean_body.strip():
        raise ValueError("Thesis body cannot be empty")

    folder, market, norm_sym, _ = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    thesis_path = os.path.join(folder, "thesis.md")
    state_path = os.path.join(folder, "state.json")
    wal_path = os.path.join(folder, ".thesis_wal.json")
    if not os.path.exists(thesis_path) or not os.path.exists(state_path):
        raise FileNotFoundError(f"Archive not initialized at {folder}")

    with _archive_lock(folder):
        _apply_wal_unlocked(folder)

        with open(state_path, "r", encoding="utf-8") as f:
            orig_state = json.load(f)

        cur_rev = int(orig_state.get("revision", 1))
        if expected_revision != cur_rev:
            raise ValueError(
                f"Revision conflict for {raw_symbol}: expected revision {expected_revision}, actual {cur_rev}"
            )

        with open(thesis_path, "r", encoding="utf-8") as f:
            existing_md = f.read()

        _, preserved_log = split_thesis_and_log(existing_md)
        next_rev = cur_rev + 1
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        clean_note = note.strip().replace("\n", " ") or f"在线修订研报主体卡片（升级至 v{next_rev}）"
        audit_line = f"- `{today}`：{clean_note}\n"

        if preserved_log:
            if not preserved_log.endswith("\n"):
                preserved_log += "\n"
            if not preserved_log.startswith("---"):
                preserved_log = "---\n\n" + preserved_log
            merged_md = f"{clean_body.rstrip()}\n\n{preserved_log}{audit_line}"
        else:
            merged_md = (
                f"{clean_body.rstrip()}\n\n---\n\n"
                f"## 增量修订日志（只追加，不重写）\n{audit_line}"
            )

        next_state = {
            **orig_state,
            "revision": next_rev,
            "updated_at_utc": utc_now_str(),
        }

        # Single atomic commit point: once .thesis_wal.json is replaced, the transaction is durable
        _atomic_write_json(wal_path, {"thesis_md": merged_md, "state": next_state})
        _apply_wal_unlocked(folder)

        return {"market": market, "symbol": norm_sym, "revision": next_rev, "thesis_md": merged_md}


def is_numeric_trigger(op: Any, thresh: Any) -> bool:
    """Return True if trigger performs numeric threshold comparison (including numeric '==')."""
    if op in (">", ">=", "<", "<="):
        return True
    return bool(op == "==" and isinstance(thresh, (int, float)) and not isinstance(thresh, bool))


def _compare_trigger_value(op: Any, obs: Any, thresh: Any) -> tuple[bool, bool, str | None]:
    """Compare observed value against threshold for operator. Returns (evaluated, fired, error)."""
    if op in (">", ">=", "<", "<="):
        try:
            obs_f = float(obs)
            thr_f = float(thresh)
        except (TypeError, ValueError):
            return False, False, f"Non-numeric comparison for operator {op!r}"
        if not (math.isfinite(obs_f) and math.isfinite(thr_f)):
            return False, False, f"Non-finite numeric comparison for operator {op!r}"
        if op == ">":
            fired = obs_f > thr_f
        elif op == ">=":
            fired = obs_f >= thr_f
        elif op == "<":
            fired = obs_f < thr_f
        else:
            fired = obs_f <= thr_f
        return True, fired, None

    if op == "==":
        def _is_nonfinite_token(v: Any) -> bool:
            if isinstance(v, float):
                return not math.isfinite(v)
            return bool(
                isinstance(v, str)
                and v.strip().lower()
                in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
            )

        if _is_nonfinite_token(obs) or _is_nonfinite_token(thresh):
            return False, False, "Non-finite value rejected for '==' trigger"
        if is_numeric_trigger(op, thresh):
            if isinstance(obs, bool):
                return False, False, "Boolean value rejected for numeric '==' trigger"
            try:
                obs_f = float(obs)
                thr_f = float(thresh)
            except (TypeError, ValueError):
                return False, False, "Non-numeric value rejected for numeric '==' trigger"
            if not (math.isfinite(obs_f) and math.isfinite(thr_f)):
                return False, False, "Non-finite value rejected for numeric '==' trigger"
            return True, obs_f == thr_f, None
        return True, obs == thresh, None

    if op == "contains":
        return True, str(thresh) in str(obs), None

    if op == "event":
        if isinstance(obs, bool):
            fired = obs if (thresh in (None, True, "")) else (obs == bool(thresh))
        elif thresh in (None, True, ""):
            fired = bool(obs)
        else:
            fired = str(thresh).lower() in str(obs).lower()
        return True, fired, None

    return False, False, f"Unsupported trigger operator: {op!r}"


def evaluate_triggers(
    state: dict,
    observations: dict[str, Any],
    *,
    ref_date: date | None = None,
    now_utc: datetime | None = None,
) -> list[dict]:
    """Evaluate state['triggers'] against live observations or strictly validated AI/manual verifications."""
    triggers = state.get("triggers")
    if not isinstance(triggers, list):
        raise ValueError("Invalid archive state: 'triggers' must be a list")

    if now_utc is not None:
        anchor_dt = now_utc.astimezone(timezone.utc) if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
    elif ref_date is not None:
        anchor_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 23, 59, 59, tzinfo=timezone.utc)
    else:
        anchor_dt = datetime.now(timezone.utc)

    results: list[dict] = []
    for trig in triggers:
        metric = trig.get("metric")
        op = trig.get("operator")
        thresh = trig.get("threshold")
        if not isinstance(metric, str) or not metric:
            results.append({**trig, "evaluated": False, "fired": False, "observed": None, "error": "Invalid metric name"})
            continue

        # 1) Live machine observation always takes precedence
        if metric in observations and observations[metric] is not None:
            obs = observations[metric]
            evaluated, fired, err = _compare_trigger_value(op, obs, thresh)
            item = {
                **trig,
                "evaluated": evaluated,
                "fired": fired,
                "observed": obs,
                "evaluation_source": "live",
            }
            if err:
                item["error"] = err
            results.append(item)
            continue

        # 2) Machine-observable auto metrics MUST NEVER be overridden by persisted AI/manual verifications
        if is_trigger_auto_metric(trig):
            results.append({**trig, "evaluated": False, "fired": False, "observed": None})
            continue

        # 3) Persisted AI web-search or manual verification with strict timestamp, provenance & numeric gates
        ver = trig.get("verification")
        if isinstance(ver, dict):
            method = str(ver.get("method") or "manual").strip().lower()
            v_status = str(ver.get("status") or "uncertain").strip().lower()
            v_ts = ver.get("verified_at_utc")
            ver_dt = parse_strict_utc_timestamp(v_ts)
            ttl_days = resolve_effective_verification_ttl_days(trig, ver)

            if ver_dt is None:
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": ver.get("observed"),
                    "evaluation_source": method,
                    "error": "核验时间戳缺失或格式无效，请重新核验",
                })
                continue

            if ver_dt > anchor_dt + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": ver.get("observed"),
                    "evaluation_source": method,
                    "verified_at_utc": v_ts,
                    "error": "核验时间戳位于未来，已拒绝放行",
                })
                continue

            elapsed = anchor_dt - ver_dt
            if elapsed > timedelta(days=ttl_days):
                age_days = max(ttl_days, int(elapsed.total_seconds() // 86400))
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": ver.get("observed"),
                    "evaluation_source": method,
                    "verification_stale": True,
                    "verified_at_utc": v_ts,
                    "error": f"核验已过期({age_days}天>{ttl_days}天)，请重新核验",
                })
                continue

            if v_status not in {"clear", "fired"}:
                summary_txt = str(ver.get("summary") or ver.get("observed_text") or "待补充一手信源").strip()
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": ver.get("observed"),
                    "evaluation_source": method,
                    "verification_status": v_status,
                    "verified_at_utc": v_ts,
                    "verification_summary": summary_txt,
                    "error": f"核验结论不确定：{summary_txt[:80]}",
                })
                continue

            ver_obs = ver.get("observed")
            summary_txt = str(ver.get("summary") or "").strip()
            obs_txt = str(ver.get("observed_text") or "").strip()
            src_url = str(ver.get("source_url") or "").strip()
            src_title = str(ver.get("source_title") or "").strip()
            has_valid_url = is_valid_http_url(src_url)
            has_provenance = has_valid_url or bool(src_title)
            has_conclusion = bool(summary_txt or obs_txt)

            # Fail-closed checks for "clear" (never allow unsubstantiated clear to pass green!)
            fired_flag = (v_status == "fired")
            if ver_obs is not None and op in (">", ">=", "<", "<=", "==", "contains"):
                ok_cmp, cmp_fired, cmp_err = _compare_trigger_value(op, ver_obs, thresh)
                if ok_cmp:
                    fired_flag = cmp_fired or fired_flag
                elif not fired_flag:
                    results.append({
                        **trig,
                        "evaluated": False,
                        "fired": False,
                        "observed": ver_obs,
                        "evaluation_source": method,
                        "verified_at_utc": v_ts,
                        "error": f"核验观测值无法与阈值比较({cmp_err})，已拒绝放行",
                    })
                    continue

            if not fired_flag:
                if not has_conclusion:
                    results.append({
                        **trig,
                        "evaluated": False,
                        "fired": False,
                        "observed": ver_obs,
                        "evaluation_source": method,
                        "verified_at_utc": v_ts,
                        "error": "核验缺少明确结论摘要，已拒绝放行",
                    })
                    continue
                if method == "ai_web" and not has_provenance:
                    results.append({
                        **trig,
                        "evaluated": False,
                        "fired": False,
                        "observed": ver_obs,
                        "evaluation_source": method,
                        "verified_at_utc": v_ts,
                        "error": "AI 核验缺少可追溯来源（标题或URL），已拒绝放行",
                    })
                    continue
                if is_numeric_trigger(op, thresh) and ver_obs is None:
                    tier = str(ver.get("evidence_tier") or "").strip().upper()
                    if (not has_provenance) or (tier not in {"L1", "L2"}):
                        results.append({
                            **trig,
                            "evaluated": False,
                            "fired": False,
                            "observed": None,
                            "evaluation_source": method,
                            "verified_at_utc": v_ts,
                            "error": "数值触发器未提供可比观测值，且缺少 L1/L2 可追溯来源佐证，已拒绝放行",
                        })
                        continue

            results.append({
                **trig,
                "evaluated": True,
                "fired": fired_flag,
                "observed": ver_obs if ver_obs is not None else (obs_txt or v_status),
                "evaluation_source": method,
                "verification_status": "fired" if fired_flag else v_status,
                "verified_at_utc": v_ts,
                "verification_summary": summary_txt or obs_txt,
                "verification_source_url": src_url if has_valid_url else None,
                "verification_evidence_tier": ver.get("evidence_tier"),
            })
            continue

        # 4) Neither live observation nor stored verification exists
        item = {**trig, "evaluated": False, "fired": False, "observed": None}
        if trig.get("evaluation_mode") in {"ai_web", "manual"}:
            item["needs_verification"] = True
        results.append(item)
    return results


def record_trigger_verifications(
    raw_symbol: str,
    verifications: dict[str, dict[str, Any]],
    *,
    research_root: str | None = None,
    market_override: str | None = None,
    expected_fingerprints: dict[str, dict[str, Any]] | None = None,
    expected_prev_verified_at: dict[str, str | None] | None = None,
    expected_prev_verifications: dict[str, dict[str, Any] | None] | None = None,
    reject_auto_metrics: bool = True,
    enforce_clear_gate: bool = False,
    cancel_check: Any = None,
) -> dict:
    """Atomically attach AI/manual verification payloads onto matching triggers with optimistic conflict checks."""
    if not isinstance(verifications, dict) or not verifications:
        raise ValueError("verifications must be a non-empty dict keyed by trigger id")

    folder, _, _, _ = resolve_archive_dir(
        raw_symbol, research_root=research_root, market_override=market_override
    )
    state_path = os.path.join(folder, "state.json")
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"Archive not initialized at {folder}")

    with _archive_lock(folder):
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("Trigger verification cancelled before commit")
        _apply_wal_unlocked(folder)
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        triggers = state.get("triggers")
        if not isinstance(triggers, list):
            raise ValueError("Archive state has invalid 'triggers' field")

        updated_ids: list[str] = []
        skipped_conflicts: list[dict[str, str]] = []
        matched_ids: set[str] = set()
        new_triggers: list[dict[str, Any]] = []
        for trig in triggers:
            if not isinstance(trig, dict):
                new_triggers.append(trig)
                continue
            tid = str(trig.get("id") or "")
            if tid in verifications:
                matched_ids.add(tid)
                if reject_auto_metrics and is_trigger_auto_metric(trig):
                    skipped_conflicts.append({
                        "id": tid,
                        "reason": "自动机审指标不允许通过 AI/人工核验覆盖",
                    })
                    new_triggers.append(trig)
                    continue
                if expected_fingerprints and tid in expected_fingerprints:
                    cur_fp = trigger_definition_fingerprint(trig)
                    if cur_fp != expected_fingerprints[tid]:
                        skipped_conflicts.append({
                            "id": tid,
                            "reason": "核验期间触发器口径/阈值/状态已被修改，已拒绝覆盖",
                        })
                        new_triggers.append(trig)
                        continue
                cur_ver = trig.get("verification") if isinstance(trig.get("verification"), dict) else None
                if (
                    expected_prev_verifications is not None
                    and tid in expected_prev_verifications
                    and exact_threshold_key(cur_ver) != exact_threshold_key(expected_prev_verifications[tid])
                ):
                    skipped_conflicts.append({
                        "id": tid,
                        "reason": "核验期间已有更新的人工/AI 核验记录写入，已拒绝覆盖",
                    })
                    new_triggers.append(trig)
                    continue
                if expected_prev_verified_at and tid in expected_prev_verified_at:
                    cur_ts = cur_ver.get("verified_at_utc") if isinstance(cur_ver, dict) else None
                    if cur_ts != expected_prev_verified_at[tid]:
                        skipped_conflicts.append({
                            "id": tid,
                            "reason": "核验期间已有更新的人工/AI 核验记录写入，已拒绝覆盖",
                        })
                        new_triggers.append(trig)
                        continue

                raw_v = verifications[tid]
                if not isinstance(raw_v, dict):
                    raise ValueError(f"Verification for trigger {tid!r} must be a dict")
                method = str(raw_v.get("method") or "manual").strip()
                status = str(raw_v.get("status") or "uncertain").strip().lower()
                verified_at = str(raw_v.get("verified_at_utc") or utc_now_iso_seconds()).strip()
                if parse_strict_utc_timestamp(verified_at) is None:
                    raise ValueError(f"Invalid verified_at_utc for trigger {tid!r}: {verified_at!r}")
                ttl_days = resolve_effective_verification_ttl_days(trig, raw_v)
                clean_v: dict[str, Any] = {
                    "method": method,
                    "status": status,
                    "verified_at_utc": verified_at,
                    "ttl_days": ttl_days,
                }
                if "observed" in raw_v and raw_v["observed"] is not None:
                    clean_v["observed"] = raw_v["observed"]
                for txt_k in ("observed_text", "summary", "source_title", "evidence_tier"):
                    val = raw_v.get(txt_k)
                    if isinstance(val, str) and val.strip():
                        clean_v[txt_k] = val.strip()
                raw_url = raw_v.get("source_url")
                if isinstance(raw_url, str) and raw_url.strip():
                    if not is_valid_http_url(raw_url):
                        raise ValueError(f"Invalid source_url for trigger {tid!r}: {raw_url!r}")
                    clean_v["source_url"] = raw_url.strip()

                if enforce_clear_gate and status == "clear":
                    op = trig.get("operator")
                    thresh = trig.get("threshold")
                    if is_numeric_trigger(op, thresh):
                        if "observed" in clean_v:
                            ok_cmp, cmp_fired, cmp_err = _compare_trigger_value(op, clean_v["observed"], thresh)
                            if not ok_cmp:
                                raise ValueError(f"数值触发器的观测值无法与阈值比较：{cmp_err}")
                            if cmp_fired:
                                raise ValueError("观测值已满足触发阈值条件，请将核验判定选择为 fired（已触发）")
                        else:
                            has_prov = bool(clean_v.get("source_url") or clean_v.get("source_title"))
                            tier = str(clean_v.get("evidence_tier") or "").strip().upper()
                            if not has_prov or tier not in {"L1", "L2"}:
                                raise ValueError(
                                    "数值触发器选择未触发时，必须填写可比数值观测值，或提供 L1/L2 来源标题/URL 佐证"
                                )

                merged = {
                    **trig,
                    "verification": clean_v,
                    "last_checked_at": verified_at,
                }
                if "observed" in clean_v:
                    merged["last_value"] = clean_v["observed"]
                elif "observed_text" in clean_v:
                    merged["last_value"] = clean_v["observed_text"]
                if not merged.get("evaluation_mode"):
                    merged["evaluation_mode"] = method
                new_triggers.append(merged)
                updated_ids.append(tid)
            else:
                new_triggers.append(trig)

        for req_tid in verifications:
            if req_tid not in matched_ids:
                skipped_conflicts.append({
                    "id": req_tid,
                    "reason": "触发器在核验期间已被删除或重命名",
                })

        if not updated_ids and not skipped_conflicts:
            raise ValueError(f"No matching trigger ids found for {raw_symbol}: {sorted(verifications.keys())}")
        if not updated_ids:
            return {"state": state, "updated_ids": [], "skipped_conflicts": skipped_conflicts}

        _validate_state_patch({"triggers": new_triggers})
        cur_rev = int(state.get("revision", 1))
        state["triggers"] = new_triggers
        state["revision"] = cur_rev + 1
        state["updated_at_utc"] = utc_now_str()
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("Trigger verification cancelled before commit")
        _atomic_write_json(state_path, state)
        return {"state": state, "updated_ids": updated_ids, "skipped_conflicts": skipped_conflicts}


def list_archives(research_root: str | None = None) -> list[dict]:
    """List all initialized symbol archives under research_root."""
    root = research_root or DEFAULT_RESEARCH_DIR
    if not os.path.isdir(root):
        return []
    out: list[dict] = []
    for dirpath, _, filenames in os.walk(root):
        if "state.json" not in filenames:
            continue
        state_path = os.path.join(dirpath, "state.json")
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            inst = st.get("instrument", {})
            out.append({
                "market": inst.get("market"),
                "symbol": inst.get("symbol"),
                "name": inst.get("name", ""),
                "status": st.get("status"),
                "direction_bias": st.get("direction_bias"),
                "revision": st.get("revision"),
                "updated_at_utc": st.get("updated_at_utc"),
                "archive_dir": dirpath,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"archive_dir": dirpath, "error": str(exc)})
    out.sort(key=lambda r: (r.get("market") or "", r.get("symbol") or ""))
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage incremental Layer 3 symbol research archives")
    parser.add_argument("--root", default=None, help="Override research archive root directory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize archive for a symbol")
    p_init.add_argument("symbol")
    p_init.add_argument("--name", default="")
    p_init.add_argument("--subtype", default="")
    p_init.add_argument("--thesis", default="")
    p_init.add_argument("--primary-driver", default="")
    p_init.add_argument("--secondary-driver", default="")
    p_init.add_argument("--layer1-primary", default=None)
    p_init.add_argument("--layer1-secondary", default=None)
    p_init.add_argument("--direction-bias", default="neutral", choices=sorted(VALID_DIRECTIONS))
    p_init.add_argument("--confidence", default=None)
    p_init.add_argument("--cik", default=None)
    p_init.add_argument("--overwrite", action="store_true")

    p_load = sub.add_parser("load", help="Load archive state & recent evidence")
    p_load.add_argument("symbol")
    p_load.add_argument("--evidence-limit", type=int, default=30)

    p_ev = sub.add_parser("append-evidence", help="Append JSON evidence object or array from --json or stdin")
    p_ev.add_argument("symbol")
    p_ev.add_argument("--json", dest="json_str", default=None)

    p_upd = sub.add_parser("update-state", help="Patch state.json with optional optimistic revision check")
    p_upd.add_argument("symbol")
    p_upd.add_argument("--patch-json", required=True)
    p_upd.add_argument("--expected-revision", type=int, default=None)

    p_log = sub.add_parser("append-log", help="Append a revision note to thesis.md")
    p_log.add_argument("symbol")
    p_log.add_argument("entry")

    p_chk = sub.add_parser("check-triggers", help="Evaluate triggers against JSON observations")
    p_chk.add_argument("symbol")
    p_chk.add_argument("--obs-json", required=True)

    sub.add_parser("list", help="List all research archives")
    return parser


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "init":
            raw_conf = args.confidence
            parsed_conf: str | float | None = raw_conf
            if isinstance(raw_conf, str):
                try:
                    parsed_conf = float(raw_conf)
                except ValueError:
                    parsed_conf = raw_conf
            res = init_archive(
                args.symbol,
                name=args.name,
                subtype=args.subtype,
                thesis_summary=args.thesis,
                primary_driver=args.primary_driver,
                secondary_driver=args.secondary_driver,
                cik=args.cik,
                research_root=args.root,
                overwrite=args.overwrite,
                direction_bias=args.direction_bias,
                layer1_primary=args.layer1_primary,
                layer1_secondary=args.layer1_secondary,
                confidence=parsed_conf,
            )
        elif args.cmd == "load":
            res = load_archive(args.symbol, research_root=args.root, evidence_limit=args.evidence_limit)
        elif args.cmd == "append-evidence":
            raw = args.json_str if args.json_str is not None else sys.stdin.read()
            payload = json.loads(raw)
            items = payload if isinstance(payload, list) else [payload]
            res = append_evidence(args.symbol, items, research_root=args.root)
        elif args.cmd == "update-state":
            patch = json.loads(args.patch_json)
            res = update_state(
                args.symbol, patch, expected_revision=args.expected_revision, research_root=args.root
            )
        elif args.cmd == "append-log":
            line = append_log(args.symbol, args.entry, research_root=args.root)
            res = {"appended": line}
        elif args.cmd == "check-triggers":
            loaded = load_archive(args.symbol, research_root=args.root, evidence_limit=0)
            obs = json.loads(args.obs_json)
            res = {"symbol": loaded["symbol"], "triggers": evaluate_triggers(loaded["state"], obs)}
        elif args.cmd == "list":
            res = list_archives(research_root=args.root)
        else:
            raise ValueError(f"Unknown command: {args.cmd}")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
