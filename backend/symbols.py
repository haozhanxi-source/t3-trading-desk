"""Symbol catalog aggregation for the workbench search box."""
from __future__ import annotations

import re
from typing import Any

import market_snapshot
import portfolio_manager
import research_archive
import scan_opportunities

_HAS_CJK = re.compile(r"[一-鿿]")

SYMBOL_ZH_NAMES: dict[str, str] = {
    # 能源原油与天然气
    "CL=F": "WTI原油",
    "BZ=F": "布伦特原油",
    "NG=F": "美国天然气",
    "HO=F": "美国取暖油",
    "RB=F": "美国汽油",
    "SC0": "上海原油",
    "FU0": "燃料油",
    "PG0": "液化石油气(LPG)",
    # 贵金属
    "GC=F": "COMEX黄金",
    "SI=F": "COMEX白银",
    "PL=F": "NYMEX铂金",
    "PA=F": "NYMEX钯金",
    "AU0": "沪金",
    "AG0": "沪银",
    # 有色与基本金属
    "HG=F": "COMEX铜",
    "ALI=F": "COMEX铝",
    "CU0": "沪铜",
    "AL0": "沪铝",
    "ZN0": "沪锌",
    "NI0": "沪镍",
    "SN0": "沪锡",
    "PB0": "沪铅",
    # 黑色金属
    "I0": "铁矿石",
    "RB0": "螺纹钢",
    "HC0": "热卷",
    "J0": "焦炭",
    "JM0": "焦煤",
    "SF0": "硅铁",
    "SM0": "锰硅",
    "HRC=F": "美国热卷",
    # 谷物与油脂
    "ZC=F": "CBOT玉米",
    "ZS=F": "CBOT大豆",
    "ZW=F": "CBOT小麦",
    "ZM=F": "CBOT豆粕",
    "ZL=F": "CBOT豆油",
    "M0": "豆粕",
    "Y0": "豆油",
    "P0": "棕榈油",
    "OI0": "菜籽油",
    "C0": "连玉米",
    # 软商品
    "KC=F": "ICE咖啡",
    "CC=F": "ICE可可",
    "SB=F": "ICE原糖",
    "CT=F": "ICE美棉",
    "OJ=F": "ICE橙汁",
    "SR0": "郑白糖",
    "CF0": "郑棉花",
    # 畜牧与生猪
    "LE=F": "CME活牛",
    "HE=F": "CME瘦肉猪",
    "GF=F": "CME育肥牛",
    "LH0": "大商所生猪",
    # 化工与新能源金属
    "TA0": "PTA",
    "MA0": "甲醇",
    "V0": "PVC",
    "SA0": "纯碱",
    "FG0": "玻璃",
    "RU0": "天然橡胶",
    "LC0": "碳酸锂",
    "SI0": "工业硅",
    "PS0": "多晶硅",
    # 航运期货
    "EC0": "集运欧线",
    # 金属矿业与资源股
    "COPX": "全球铜矿ETF",
    "GDX": "黄金矿业ETF",
    "XME": "美国金属矿业ETF",
    "FCX": "自由港迈克墨伦(铜)",
    "SCCO": "南方铜业",
    "AA": "美国铝业",
    "NEM": "纽蒙特矿业(金)",
    "MP": "MP材料(稀土)",
    "512400.SS": "有色金属ETF",
    "601899.SS": "紫金矿业",
    "603993.SS": "洛阳钼业",
    "601600.SS": "中国铝业",
    # 美股宽基与行业 ETF
    "SPY": "标普500 ETF",
    "QQQ": "纳指100 ETF",
    "IWM": "罗素2000 ETF",
    "DBC": "综合商品指数ETF",
    "DBA": "农产品指数ETF",
    "XLK": "美股科技ETF",
    "SMH": "半导体ETF",
    "XLU": "公用事业ETF",
    "XLE": "能源ETF",
    "XLI": "工业ETF",
    "XLF": "金融ETF",
    "XLV": "医疗保健ETF",
    "ITA": "航空军工ETF",
    "XLB": "原材料ETF",
    "XLP": "必需消费ETF",
    "XLY": "可选消费ETF",
    # 美股电力与电网基建
    "CEG": "星座能源(核电)",
    "VST": "Vistra能源",
    "GEV": "GE Vernova(电力设备)",
    "ETN": "伊顿(电气设备)",
    "VRT": "维谛技术(供电/液冷)",
    "PWR": "广达服务(电网工程)",
    # 美股半导体与 AI 算力
    "NVDA": "英伟达",
    "AVGO": "博通",
    "TSM": "台积电",
    "MU": "美光科技",
    "MRVL": "迈威尔科技",
    "AMD": "超威半导体(AMD)",
    # 美股科技现金流巨头
    "AAPL": "苹果",
    "MSFT": "微软",
    "GOOGL": "谷歌",
    "AMZN": "亚马逊",
    "META": "Meta",
    # A 股宽基、ETF 与主题龙头
    "510300.SS": "沪深300 ETF",
    "588000.SS": "科创50 ETF",
    "510880.SS": "红利ETF",
    "512480.SS": "半导体ETF(A股)",
    "515220.SS": "煤炭ETF",
    "512660.SS": "军工ETF",
    "512010.SS": "医药ETF",
    "159928.SZ": "消费ETF",
    "512880.SS": "证券ETF",
    "300750.SZ": "宁德时代",
    "002050.SZ": "三花智控",
    "600519.SS": "贵州茅台",
    # 加密货币
    "BTC/USDT": "比特币",
    "ETH/USDT": "以太坊",
    "SOL/USDT": "Solana",
    "BTC": "比特币",
    "ETH": "以太坊",
    "SOL": "Solana",
    # 外汇与美元指数
    "DX-Y.NYB": "美元指数",
    "EURUSD=X": "欧元/美元",
    "USDJPY=X": "美元/日元",
    "GBPUSD=X": "英镑/美元",
    "AUDUSD=X": "澳元/美元",
    "USDCNY=X": "美元/人民币",
    "USDCAD=X": "美元/加元",
}


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
        zh_name = SYMBOL_ZH_NAMES.get(item["symbol"].upper())
        item["label"] = item["_name"] or zh_name or cjk or (item["_aliases"][0] if item["_aliases"] else item["symbol"])
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
