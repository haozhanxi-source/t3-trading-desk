#!/usr/bin/env python3
"""Scan watchlist or custom universes/symbols across US stocks, A-shares, Commodities, CN Futures, and Crypto using Daily Chanlun structure & momentum."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
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
    "数据不足": 6,
}


def format_fractal_label(frac: dict | None) -> str:
    if not frac:
        return "无"
    kind_zh = "底分型" if frac["kind"] == "bottom" else "顶分型"
    fresh = "刚确认" if frac.get("just_confirmed") else f"{frac.get('confirmed_merged_bars_ago')}K前"
    return f"{kind_zh}({fresh}/{frac.get('strength', 'standard')})"


def format_momentum_label(mom: dict) -> str:
    verdict = mom.get("verdict", "insufficient_data")
    ar = mom.get("area_ratio_vs_prev")
    pr = mom.get("peak_ratio_vs_prev")
    ratio_str = f"area={ar}x,peak={pr}x" if ar is not None else "首笔样本"

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


def load_universes(universes_path: str | None = None) -> dict:
    path = universes_path or DEFAULT_UNIVERSES_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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
    seen: set[str] = set()
    for market, sym in raw_targets:
        s = str(sym).strip()
        if not s:
            continue
        try:
            dedup_key = normalize_symbol(s)[0]
        except Exception:
            dedup_key = s.upper()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        deduped.append((market, s))
    return deduped


def build_scan_row(market: str, sym: str, snap: dict) -> dict:
    if "error" in snap:
        return {"market": market, "symbol": sym, "error": snap["error"]}

    t = snap["technicals"]
    cl = snap["chanlun"]
    daily = cl["daily"]
    summary = cl["signal_summary"]
    zs = daily.get("latest_zhongshu")

    return {
        "market": market,
        "symbol": snap["symbol"],
        "close": t["last_close"],
        "ret_5d_%": t["return_5d_pct"],
        "ret_20d_%": t["return_20d_pct"],
        "ret_60d_%": t["return_60d_pct"],
        "atr_%": t["atr14_pct"],
        "signal_tier": summary.get("tier"),
        "daily_structure": daily.get("macro_structure"),
        "daily_zs_count": daily.get("zhongshu_count"),
        "daily_zs": [zs["zd"], zs["zg"]] if zs else None,
        "pos_vs_zs": daily.get("price_vs_zhongshu"),
        "daily_fractal": format_fractal_label(daily.get("latest_fractal")),
        "daily_momentum": format_momentum_label(daily.get("momentum_assessment", {})),
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
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    rows = scan_targets(targets, period=args.period, sort_mode=args.sort)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
