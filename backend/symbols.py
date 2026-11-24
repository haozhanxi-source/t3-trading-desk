"""Symbol catalog aggregation for the workbench search box."""
from __future__ import annotations

import re
from typing import Any

import market_snapshot
import portfolio_manager
import research_archive
import scan_opportunities

_HAS_CJK = re.compile(r"[一-鿿]")


def _norm(raw: str) -> tuple[str, str]:
    try:
        sym, asset_class = market_snapshot.normalize_symbol(raw)
        return sym, asset_class
    except Exception:  # noqa: BLE001
        return raw.strip().upper(), "unknown"


def _alias_tables() -> list[dict[str, str]]:
    """Alias dicts exposed by market_snapshot (alias -> raw target symbol)."""
    tables: list[dict[str, str]] = []
    for name in ("CN_FUTURE_ALIASES", "COMMODITY_ALIASES", "FX_ALIASES", "CRYPTO_ALIASES", "CN_PREFIX_ALIASES"):
        tbl = getattr(market_snapshot, name, None)
        if isinstance(tbl, dict):
            tables.append(tbl)
    return tables


def build_catalog() -> dict[str, dict[str, Any]]:
    """Return ``normalized_symbol -> entry`` merged from universes, archives, holdings and alias tables."""
    catalog: dict[str, dict[str, Any]] = {}

    def entry(raw: str) -> dict[str, Any]:
        sym, asset_class = _norm(raw)
        item = catalog.get(sym)
        if item is None:
            item = {
                "symbol": sym,
                "label": sym,
                "asset_class": asset_class,
                "sources": [],
                "pool": None,
                "status": None,
                "_aliases": [],
                "_name": "",
            }
            catalog[sym] = item
        return item

    def add_source(item: dict[str, Any], source: str) -> None:
        if source not in item["sources"]:
            item["sources"].append(source)

    try:
        universes = scan_opportunities.load_universes()
    except (OSError, ValueError):
        universes = {}
    for pool, meta in universes.items():
        for raw in (meta or {}).get("symbols", []):
            item = entry(str(raw))
            add_source(item, "universe")
            if item["pool"] is None:
                item["pool"] = pool

    for arch in research_archive.list_archives():
        if arch.get("error") or not arch.get("symbol"):
            continue
        item = entry(str(arch["symbol"]))
        add_source(item, "archive")
        item["status"] = arch.get("status") or item["status"]
        item["_name"] = arch.get("name") or item["_name"]

    try:
        positions = portfolio_manager.load_positions(status="open")
    except FileNotFoundError:
        positions = []
    for pos in positions:
        if not pos.get("symbol"):
            continue
        item = entry(str(pos["symbol"]))
        add_source(item, "portfolio")
        item["status"] = "holding"

    for table in _alias_tables():
        for alias, target in table.items():
            sym, _ = _norm(str(target))
            item = entry(str(target))
            add_source(item, "alias")
            if alias.upper() != sym and alias not in item["_aliases"]:
                item["_aliases"].append(alias)

    for base in sorted(getattr(market_snapshot, "CRYPTO_BASE", ())):
        add_source(entry(base), "alias")

    for item in catalog.values():
        cjk = next((a for a in item["_aliases"] if _HAS_CJK.search(a)), None)
        item["label"] = item["_name"] or cjk or (item["_aliases"][0] if item["_aliases"] else item["symbol"])
    return catalog


def _rank(item: dict[str, Any], q: str) -> tuple[int, int, str]:
    sym = item["symbol"].lower()
    aliases = [a.lower() for a in item["_aliases"]]
    if sym == q or q in aliases:
        score = 0
    elif sym.startswith(q) or any(a.startswith(q) for a in aliases):
        score = 1
    elif q in sym or q in item["label"].lower():
        score = 2
    else:
        score = 3
    sources = item["sources"]
    owned = 0 if "portfolio" in sources else 1 if "archive" in sources else 2 if "universe" in sources else 3
    return score, owned, item["symbol"]


def search_symbols(q: str, limit: int = 30) -> list[dict[str, Any]]:
    needle = (q or "").strip().lower()
    items = list(build_catalog().values())
    if needle:
        items = [
            it for it in items
            if needle in it["symbol"].lower()
            or needle in it["label"].lower()
            or (it["pool"] and needle in it["pool"].lower())
            or any(needle in a.lower() for a in it["_aliases"])
        ]
        items.sort(key=lambda it: _rank(it, needle))
    else:
        items.sort(key=lambda it: (_rank(it, "")[1], it["symbol"]))
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items[: max(limit, 0)]]
