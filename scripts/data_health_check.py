#!/usr/bin/env python3
"""Data warehouse & upstream OHLCV health checker (`scripts/data_health_check.py`).

Inspects local ``data/desk.db`` series health and optionally probes specified symbols/universes,
reporting flat settlement-bar ratios, envelope repairs, quarantined corrupt bars, and staleness.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_store import get_store
from market_snapshot import get_snapshot
from scan_opportunities import resolve_targets


def run_data_health_check(
    *,
    universes: list[str] | None = None,
    symbols: list[str] | None = None,
    refresh: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    store = get_store(db_path)
    probed: list[dict[str, Any]] = []
    if universes or symbols:
        targets = resolve_targets(universe_names=universes, symbols=symbols)
        for market, sym in targets:
            snap = get_snapshot(
                sym,
                period="3y",
                include_macro=False,
                include_fundamentals=False,
                refresh=refresh,
                use_store=True,
                db_path=store.db_path,
            )
            if "error" in snap:
                probed.append({
                    "market": market,
                    "symbol": sym,
                    "quality_grade": "error",
                    "error": snap["error"],
                })
                continue
            dq = snap.get("data_quality") or {}
            probed.append({
                "market": market,
                "symbol": snap.get("symbol", sym),
                "asset_class": snap.get("asset_class"),
                "as_of_bar": (snap.get("technicals") or {}).get("as_of_bar"),
                "bar_is_closed": (snap.get("technicals") or {}).get("bar_is_closed"),
                "quality_grade": dq.get("quality_grade", "good"),
                "raw_flat_bars_60": dq.get("raw_flat_bars_60", 0),
                "raw_flat_ratio_60": dq.get("raw_flat_ratio_60", 0.0),
                "repaired_ohlc_bars_total": dq.get("repaired_ohlc_bars_total", 0),
                "dropped_corrupt_bars": dq.get("dropped_corrupt_bars", 0),
                "warnings": dq.get("warnings", []),
                "notes": dq.get("notes", []),
                "cache_status": (snap.get("sync_meta") or {}).get("cache_status"),
            })

    summary = store.warehouse_summary()
    return {
        "warehouse": summary,
        "probed": probed,
        "probed_count": len(probed),
        "probed_degraded": sum(1 for p in probed if p.get("quality_grade") in ("degraded", "error")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local SQLite data warehouse & symbol OHLCV quality")
    parser.add_argument("--universe", nargs="+", help="Optional universe pool(s) to probe")
    parser.add_argument("--symbols", nargs="+", help="Optional symbol(s) to probe")
    parser.add_argument("--refresh", action="store_true", help="Force upstream refresh when probing")
    args = parser.parse_args(argv)

    report = run_data_health_check(
        universes=args.universe,
        symbols=args.symbols,
        refresh=args.refresh,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["probed_degraded"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
