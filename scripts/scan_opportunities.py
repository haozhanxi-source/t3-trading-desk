#!/usr/bin/env python3
"""Scan watchlist or custom universes/symbols across US stocks, A-shares, Commodities, CN Futures, and Crypto using Daily Chanlun structure & momentum."""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(__file__))
from data_store import get_store, is_store_enabled
from market_snapshot import get_snapshot, normalize_symbol

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
DEFAULT_WATCHLIST_PATH = os.path.join(ROOT_DIR, "watchlist.json")
DEFAULT_UNIVERSES_PATH = os.path.join(ROOT_DIR, "universes.json")
WATCHLIST_BUCKETS = ["us_stocks", "a_shares", "commodities", "crypto"]

TIER_PRIORITY = {
    "买卖点信号": 0,
    "背驰观察": 1,
    "资金一致推进(顺势)": 2,
    "资金一致推进(勿逆势)": 3,
    "中枢震荡": 4,
    "常规结构运行": 5,
    "数据质量降级": 6,
    "数据不足": 7,
}


def format_fractal_label(frac: dict | None) -> str:
    if not frac:
        return "无"
    kind_zh = "底分型" if frac["kind"] == "bottom" else "顶分型"
    fresh = "刚确认" if frac.get("just_confirmed") else f"{frac.get('confirmed_merged_bars_ago')}K前"
    return f"{kind_zh}({fresh}/{frac.get('strength', 'standard')})"


def format_momentum_label(mom: dict) -> str:
    verdict = mom.get("verdict", "insufficient_data")
    rel = mom.get("momentum_reliable")
    ar = mom.get("area_ratio_vs_prev")
    pr = mom.get("peak_ratio_vs_prev")
    if rel is False:
        cp = mom.get("curr_pct_change")
        pp = mom.get("prev_pct_change")
        if isinstance(cp, (int, float)) and isinstance(pp, (int, float)):
            ratio_str = f"幅度{cp:+.2f}% vs 前笔{pp:+.2f}%·短笔/零面积"
        else:
            ratio_str = "短笔/零面积·MACD比不参考"
    elif ar is not None and pr is not None:
        ratio_str = f"area={ar}x,peak={pr}x"
    elif ar is not None:
        ratio_str = f"area={ar}x"
    else:
        ratio_str = "首笔样本"

    verdict_map = {
        "capital_consensus_down": "空头资金方向一致",
        "capital_consensus_up": "多头资金方向一致",
        "divergence_candidate_down": "下跌力度衰减(疑似底背驰)",
        "divergence_candidate_up": "上涨力度衰减(疑似顶背驰)",
        "higher_low_pullback": "回踩不破前低",
        "lower_high_rebound": "反弹不过前高",
        "neutral_extension_down": "向下笔延伸(力度中性)",
        "neutral_extension_up": "向上笔延伸(力度中性)",
    }
    label = verdict_map.get(verdict, verdict)
    return f"{label} ({ratio_str})"


def collect_signals(snap: dict) -> list[str]:
    if "error" in snap:
        return [f"ERROR: {snap['error']}"]

    cl = snap.get("chanlun", {})
    daily = cl.get("daily", {})
    summary = cl.get("signal_summary", {})

    signals = []
    if summary.get("summary"):
        signals.append(f"【{summary['summary']}】")

    signals.extend(daily.get("signals", []))

    funding = snap.get("crypto_perp_funding", {})
    for ex, info in funding.items():
        ann = info.get("funding_annualized_pct")
        if ann is not None and (ann >= 15 or ann <= -5):
            side = "多头杠杆拥挤" if ann >= 15 else "空头资金费率一致看空"
            signals.append(f"{ex}永续年化费率{ann}%({side})")

    return signals or ["常规日线结构运行"]


_UNIVERSES_LOCKS_GUARD = threading.Lock()
_UNIVERSES_LOCKS: dict[str, threading.RLock] = {}


def _get_inproc_lock(key: str) -> threading.RLock:
    with _UNIVERSES_LOCKS_GUARD:
        lock = _UNIVERSES_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _UNIVERSES_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _universes_lock(universes_path: str):
    """Serialize both multi-threaded and multi-process read-modify-write updates to universes.json."""
    parent = os.path.dirname(os.path.abspath(universes_path))
    os.makedirs(parent, exist_ok=True)
    lock_path = os.path.join(os.path.realpath(parent), ".universes.lock")
    tlock = _get_inproc_lock(lock_path)
    with tlock, open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: str, obj: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".universes_", suffix=".tmp")
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


def load_universes(universes_path: str | None = None) -> dict:
    path = universes_path or DEFAULT_UNIVERSES_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_universe(
    name: str,
    symbols: list[str],
    description: str = "",
    universes_path: str | None = None,
    *,
    allow_overwrite: bool = True,
) -> dict:
    """Persist or update a top-down discovered sector universe in universes.json atomically."""
    pool_name = (name or "").strip()
    if not pool_name:
        raise ValueError("Universe name cannot be empty")
    cleaned: list[str] = []
    seen: set[tuple[str, str]] = set()
    for sym in symbols:
        s = str(sym).strip()
        if not s:
            continue
        norm_s, ac = normalize_symbol(s)
        dedup_key = (ac, norm_s)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        cleaned.append(s)
    if not cleaned:
        raise ValueError(f"Cannot save empty symbol list for universe {pool_name!r}")

    path = os.path.abspath(universes_path or DEFAULT_UNIVERSES_PATH)
    with _universes_lock(path):
        universes = load_universes(path) if os.path.exists(path) else {}
        if not allow_overwrite and pool_name in universes:
            raise FileExistsError(f"主题池 '{pool_name}' 已存在，请更换代号或显式确认覆盖")
        existing_desc = universes.get(pool_name, {}).get("description", "")
        final_desc = description.strip() or existing_desc or f"自上而下投研沉淀标的池 ({pool_name})"
        universes[pool_name] = {
            "description": final_desc,
            "symbols": cleaned,
        }
        _atomic_write_json(path, universes)
        return dict(universes[pool_name])


def resolve_targets(
    universe_names: list[str] | None = None,
    symbols: list[str] | None = None,
    watchlist_path: str | None = None,
    universes_path: str | None = None,
) -> list[tuple[str, str]]:
    """Resolve (market, symbol) targets from watchlist or universes/symbols with deduplication."""
    raw_targets: list[tuple[str, str]] = []

    if universe_names or symbols:
        if universe_names:
            universes = load_universes(universes_path)
            unknown = [name for name in universe_names if name not in universes]
            if unknown:
                available = ", ".join(universes.keys())
                raise ValueError(
                    f"Unknown universe(s): {', '.join(unknown)}. Available universes: {available}"
                )
            for name in universe_names:
                for sym in universes[name].get("symbols", []):
                    raw_targets.append((name, sym))
        if symbols:
            for sym in symbols:
                raw_targets.append(("adhoc", sym))
    else:
        wl_path = watchlist_path or DEFAULT_WATCHLIST_PATH
        with open(wl_path, "r", encoding="utf-8") as f:
            wl = json.load(f)
        for bucket in WATCHLIST_BUCKETS:
            for sym in wl.get(bucket, []):
                raw_targets.append((bucket, sym))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for market, sym in raw_targets:
        s = str(sym).strip()
        if not s:
            continue
        try:
            norm_s, ac = normalize_symbol(s)
            dedup_key = (ac, norm_s)
        except Exception:
            dedup_key = ("unknown", s.upper())
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        deduped.append((market, s))
    return deduped


def build_scan_row(market: str, sym: str, snap: dict) -> dict:
    if not isinstance(snap, dict):
        return {"market": market, "symbol": sym, "error": "Invalid snapshot payload"}
    if "error" in snap:
        return {"market": market, "symbol": sym, "error": snap["error"]}
    if (
        not isinstance(snap.get("technicals"), dict)
        or not isinstance(snap.get("chanlun"), dict)
        or "last_close" not in snap["technicals"]
    ):
        return {"market": market, "symbol": sym, "error": "Malformed snapshot: missing technicals/chanlun"}

    t = snap.get("technicals") if isinstance(snap.get("technicals"), dict) else {}
    cl = snap.get("chanlun") if isinstance(snap.get("chanlun"), dict) else {}
    daily = cl.get("daily") if isinstance(cl.get("daily"), dict) else {}
    summary = cl.get("signal_summary") if isinstance(cl.get("signal_summary"), dict) else {}
    meta = summary.get("signal_meta") if isinstance(summary.get("signal_meta"), dict) else {}
    zs = daily.get("latest_zhongshu") if isinstance(daily.get("latest_zhongshu"), dict) else None
    mom = daily.get("momentum_assessment") if isinstance(daily.get("momentum_assessment"), dict) else {}
    dq = snap.get("data_quality") if isinstance(snap.get("data_quality"), dict) else (
        daily.get("data_quality") if isinstance(daily.get("data_quality"), dict) else {}
    )
    sync_meta = snap.get("sync_meta") if isinstance(snap.get("sync_meta"), dict) else {}

    ret20 = t.get("return_20d_pct")
    if ret20 is None:
        ret20 = t.get("ret_20d_pct")

    return {
        "market": market,
        "symbol": snap.get("symbol", sym),
        "asset_class": snap.get("asset_class"),
        "as_of_bar": t.get("as_of_bar"),
        "bar_is_closed": t.get("bar_is_closed"),
        "close": t.get("last_close"),
        "ret_5d_%": t.get("return_5d_pct"),
        "ret_20d_%": ret20,
        "ret_60d_%": t.get("return_60d_pct"),
        "atr_%": t.get("atr14_pct"),
        "signal_tier": summary.get("tier"),
        "signal_direction": meta.get("direction"),
        "signal_type": meta.get("signal_type"),
        "signal_confirmed": meta.get("confirmed"),
        "daily_structure": daily.get("macro_structure"),
        "daily_zs_level": daily.get("zhongshu_level"),
        "daily_zs_level_label": daily.get("zhongshu_level_label"),
        "daily_zs_count": daily.get("zhongshu_count"),
        "daily_zs": [zs.get("zd"), zs.get("zg")] if zs and zs.get("zd") is not None and zs.get("zg") is not None else None,
        "pos_vs_zs": daily.get("price_vs_zhongshu"),
        "daily_fractal": format_fractal_label(daily.get("latest_fractal")),
        "daily_momentum": format_momentum_label(mom),
        "momentum_reliable": mom.get("momentum_reliable"),
        "quality_grade": dq.get("quality_grade"),
        "quality_warnings": dq.get("warnings", []),
        "quality_notes": dq.get("notes", []),
        "cache_status": sync_meta.get("cache_status"),
        "action_bias": summary.get("action_bias"),
        "signals": collect_signals(snap),
    }


def sort_scan_rows(rows: list[dict], sort_mode: str = "none") -> list[dict]:
    if sort_mode != "tier":
        return list(rows)

    def sort_key(row: dict) -> int:
        if "error" in row:
            return 99
        return TIER_PRIORITY.get(row.get("signal_tier") or "", 50)

    return sorted(rows, key=sort_key)


def scan_targets(
    targets: list[tuple[str, str]],
    fetcher=None,
    period: str = "3y",
    sort_mode: str = "none",
) -> list[dict]:
    fetch_fn = fetcher or get_snapshot
    rows: list[dict] = []
    for market, sym in targets:
        try:
            snap = fetch_fn(sym, period=period, include_macro=False, include_fundamentals=False)
            row = build_scan_row(market, sym, snap)
        except Exception as exc:
            row = build_scan_row(market, sym, {"error": str(exc)})
        rows.append(row)
    return sort_scan_rows(rows, sort_mode=sort_mode)


def format_universes_list(universes: dict) -> str:
    lines = []
    for name, meta in universes.items():
        desc = meta.get("description", "")
        count = len(meta.get("symbols", []))
        lines.append(f"{name}\t({count})\t{desc}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan watchlist or specified universes/symbols using Daily Chanlun structure & momentum."
    )
    parser.add_argument(
        "--universe",
        nargs="+",
        metavar="NAME",
        help="Universe pool name(s) from universes.json (e.g. commodities_ferrous us_semis_ai)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYM",
        help="Ad-hoc symbol list to scan (e.g. NVDA I0 螺纹 BTC)",
    )
    parser.add_argument(
        "--list-universes",
        action="store_true",
        help="List all available universes with description and symbol count, then exit",
    )
    parser.add_argument(
        "--sort",
        choices=["none", "tier"],
        default="none",
        help="Sort output rows (default: none; 'tier' prioritizes actionable Chanlun signal tiers)",
    )
    parser.add_argument(
        "--period",
        default="3y",
        help="History period passed to snapshot fetcher (default: 3y)",
    )
    parser.add_argument(
        "--save-universe",
        metavar="NAME",
        help="Save or update the resolved symbol list into universes.json under NAME for future reuse",
    )
    parser.add_argument(
        "--description",
        default="",
        help="Description used when --save-universe is specified",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force upstream refresh bypassing local SQLite freshness window",
    )
    return parser


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_universes:
        universes = load_universes()
        print(format_universes_list(universes))
        return

    try:
        targets = resolve_targets(universe_names=args.universe, symbols=args.symbols)
        if args.save_universe:
            pool_name = args.save_universe.strip()
            save_universe(
                name=pool_name,
                symbols=[sym for _, sym in targets],
                description=args.description,
            )
            targets = [(pool_name, sym) for _, sym in targets]
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    fetcher = (lambda s, **kw: get_snapshot(s, refresh=True, **kw)) if args.refresh else None
    rows = scan_targets(targets, fetcher=fetcher, period=args.period, sort_mode=args.sort)
    if is_store_enabled():
        try:
            get_store().record_scan_run(
                universes=args.universe or [],
                symbols=args.symbols or [],
                period=args.period,
                rows=rows,
            )
        except Exception:
            pass
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
