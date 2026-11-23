#!/usr/bin/env python3
"""Incremental per-symbol Layer 3 research archive manager.

Stores persistent, append-friendly research files under:
    research/<MARKET>/<SAFE_SYMBOL>/
        - thesis.md       (human-readable thesis card + append-only revision log)
        - state.json      (structured triggers, Chanlun anchors, watermarks, optimistic revision)
        - evidence.jsonl  (append-only L1-L4 graded facts with deduplication & supersedes)
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
import sys
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

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
VALID_STATUSES = {"watchlist", "actionable_long", "actionable_short", "pair_trade", "holding", "rejected"}
VALID_DIRECTIONS = {"long", "short", "neutral", "pair"}
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
    with tlock:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
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

    norm_sym, asset_class = normalize_symbol(raw_clean)
    if market_override is not None:
        market = str(market_override).strip().upper()
        if market not in VALID_MARKETS:
            raise ValueError(f"Invalid market_override {market_override!r}; expected one of {sorted(VALID_MARKETS)}")
    else:
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


def _build_commodity_template(
    symbol: str,
    market: str,
    today: str,
    title_name: str,
    subtype: str,
    summary_line: str,
) -> str:
    return f"""# [{symbol}]{title_name} · 大宗商品深度投研与交易档案

- **市场 / 类别**：`{market}` / `{subtype or "待分类(能源/有色/黑色/农产/化工新材料/航运)"}`
- **建档日期**：`{today}`
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1Q~1Y 供需平衡表与交易结构真相，完成独立红蓝对抗并给出客观研究裁决，**不决定入场价、合约选择与仓位大小**。

### 1Q~1Y 供需平衡表预期（新投产爬坡 vs 下游需求）
| 分项 | 上季(实际) | 当季(估) | Q+1(预) | Q+2(预) | Q+3~Q+4(预) | 关键假设 / 口径 / 来源 |
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
- **研究方向与置信度**：待定（多 / 空 / 中性 / 配对；核心 `L1/L2` 证据缺口清单）
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
) -> str:
    return f"""# [{symbol}]{title_name} · 外汇（FX）深度投研与交易档案

- **市场 / 类别**：`{market}` / `{subtype or "待分类(G10利差/避险/商品货币/新兴市场)"}`
- **建档日期**：`{today}`
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1Q~1Y 利差周期、持仓结构与宏观/贸易驱动逻辑，完成独立红蓝对抗并给出客观研究裁决，**不决定入场价、工具选择与仓位大小**。

### 利差与货币政策路径（10Y/2Y 利差与通胀预期）
| 维度 | 基准货币（左侧） | 计价货币（右侧） | 利差 / 相对变化 | 1Q~1Y 政策路径预期 | 口径 / 来源 |
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
- **研究方向与置信度**：待定（多 / 空 / 中性 / 交叉盘配对；核心 `L1/L2` 证据缺口清单）
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
) -> str:
    return f"""# [{symbol}]{title_name} · 股票深度投研与交易档案（1~3 年预期差框架）

- **市场 / 资产分类**：`{market}` / `{subtype or "待分类(A成长/B强周期/C稳定现金流/D困境反转)"}`
- **建档日期**：`{today}`
- **一句话 Thesis**：{summary_line}

---

## 第三层：深度研究与红蓝对抗
> 定位：纯研究层——确立 1~3 年远期盈利与估值预期差，完成一手财报核验与独立红蓝对抗，输出客观研究裁决，**不决定入场价、表达方式与仓位大小**。

### 1~3 年远期预期拆解与预期差
- **估值锚选择**：（成长看前瞻 1~3Y EPS×PE 与二阶增速；强周期看中周期正常化盈利与产能拐点，严禁景气顶峰因静态低 PE 接飞刀；A 股结合 5 年 PE(TTM)/PB 分位，美股结合一致预期修正与空头比例）
- **收入驱动树**：待拆解（量 × 价 × 结构）
- **预期差来源**：一致预期 vs 我们的判断 vs 验证时间点（说不出预期差最多进观察池）

#### 三情景估值与压缩压力测试（EPS × PE，强制标注 GAAP / Adjusted 口径）
| 情景 | 盈利假设 (EPS / 口径) | 估值倍数 (PE) | 目标价 | 相对现价空间 |
| :--- | :--- | :---: | :---: | :---: |
| **牛情景** | 待核验 | 待核验 | 待算 | |
| **基准情景** | 待核验 | 待核验 | 待算 | |
| **熊1（估值回归）** | EPS 持平 | 回到 5 年中枢 | 待算 | |
| **熊2（戴维斯双杀）** | EPS -10% | 进一步压缩 | 待算 | |

### 一手财报核验清单（7 项，缺项标“待核验”）
1. **指引与 EPS 口径（GAAP vs Adjusted）**：待核验
2. **在手订单 / RPO 硬合同 vs 框架预估拆分**：待核验
3. **合同类型（固定总价占比与利润率 ±1pp 敏感度）**：待核验
4. **客户与单一需求链集中度**：待核验
5. **现金流质量（FCF/净利、应收与合同资产变化）**：待核验
6. **并购商誉与股权稀释（SBC）**：待核验
7. **债务到期结构与利率敏感度**：待核验

### 红蓝辩论反方必答 7 问（核心论据须有 L1/L2 证据）
1. EPS 持平、PE 回归历史中枢跌多少？
2. 剔除软订单/框架预估后真实增速剩多少？
3. 利润率压缩 1pp 对 EPS 冲击多大？
4. 预期差是否已被当前股价/一致目标价充分定价？
5. 最大单一客户或需求链断供会怎样？
6. 核心基本面/估值证伪指标与预期差失效阈值在哪？（具体日线/30min 止损位交由第四层设定）
7. 该论点历史上最近一次被证伪的案例是什么？

### 第三层客观研究结论（不含下单与仓位决策）
- **研究方向与置信度**：待定（中长线以做多为主；列明 `L1/L2` 证据支撑与待核验项）
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
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_name = f" ({name})" if name else ""
    summary_line = thesis_summary or "待补充：一句话核心交易论点与 1Q~1Y 预期差来源。"

    if market == "FX" or asset_class == "fx":
        return _build_fx_template(
            symbol=symbol,
            market=market,
            today=today,
            title_name=title_name,
            subtype=subtype,
            summary_line=summary_line,
        )

    if asset_class in ("commodity", "cn_future") or market in ("COMMODITIES", "CN_FUTURES"):
        return _build_commodity_template(
            symbol=symbol,
            market=market,
            today=today,
            title_name=title_name,
            subtype=subtype,
            summary_line=summary_line,
        )

    return _build_equity_template(
        symbol=symbol,
        market=market,
        today=today,
        title_name=title_name,
        subtype=subtype,
        summary_line=summary_line,
    )


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
) -> dict:
    """Create research/<MARKET>/<SYMBOL>/{thesis.md, evidence.jsonl, state.json} atomically under lock."""
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
            return {"status": "exists", "archive_dir": folder, "state": existing_state}

        now = utc_now_str()
        state = {
            "schema_version": 1,
            "revision": 1,
            "instrument": {
                "market": market,
                "symbol": norm_sym,
                "name": name,
                "asset_class": asset_class,
                "subtype": subtype,
                "cik": cik,
            },
            "status": "watchlist",
            "direction_bias": "neutral",
            "layer1_drivers": {
                "primary": primary_driver,
                "secondary": secondary_driver,
            },
            "thesis_summary": thesis_summary or "待补充核心论点",
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
                    thesis_summary=thesis_summary,
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
                except Exception:
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

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

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
    if "status" in patch and patch["status"] not in VALID_STATUSES:
        raise ValueError(f"Invalid status {patch['status']!r}; expected one of {sorted(VALID_STATUSES)}")
    if "direction_bias" in patch and patch["direction_bias"] not in VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction_bias {patch['direction_bias']!r}; expected one of {sorted(VALID_DIRECTIONS)}")
    for dict_key in ("instrument", "layer1_drivers", "chanlun_anchors", "valuation_or_balance", "source_watermarks"):
        if dict_key in patch and not isinstance(patch[dict_key], dict):
            raise ValueError(f"Field {dict_key!r} must be a JSON object")
    for list_key in ("triggers", "pending_verifications", "events"):
        if list_key in patch and not isinstance(patch[list_key], list):
            raise ValueError(f"Field {list_key!r} must be a JSON array")
    if "triggers" in patch:
        for trig in patch["triggers"]:
            if not isinstance(trig, dict):
                raise ValueError("Each trigger must be a JSON object")
            for req in ("id", "metric", "operator", "action"):
                val = trig.get(req)
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(f"Trigger field {req!r} must be a non-empty string")
            if trig["operator"] not in VALID_OPERATORS:
                raise ValueError(f"Invalid trigger operator {trig['operator']!r}; expected one of {sorted(VALID_OPERATORS)}")
            if "kind" in trig:
                if not isinstance(trig["kind"], str) or trig["kind"] not in VALID_TRIGGER_KINDS:
                    raise ValueError(
                        f"Invalid trigger kind {trig['kind']!r}; expected one of {sorted(VALID_TRIGGER_KINDS)}"
                    )
            if "threshold" not in trig:
                raise ValueError("Trigger missing required field 'threshold'")
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
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        cur_rev = int(state.get("revision", 1))
        if expected_revision is not None and expected_revision != cur_rev:
            raise ValueError(
                f"Revision conflict for {raw_symbol}: expected revision {expected_revision}, actual {cur_rev}"
            )

        dict_merge_keys = {"instrument", "layer1_drivers", "chanlun_anchors", "valuation_or_balance", "source_watermarks"}
        for k, v in patch.items():
            if k in ("schema_version", "revision", "created_at_utc", "updated_at_utc"):
                continue
            if k in dict_merge_keys and isinstance(v, dict) and isinstance(state.get(k), dict):
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
        with open(thesis_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content and not content.endswith("\n"):
            content += "\n"
        _atomic_write_text(thesis_path, content + line)
    return line.strip()


def evaluate_triggers(state: dict, observations: dict[str, Any]) -> list[dict]:
    """Evaluate state['triggers'] against latest metric observations with explicit error separation."""
    triggers = state.get("triggers")
    if not isinstance(triggers, list):
        raise ValueError("Invalid archive state: 'triggers' must be a list")

    results: list[dict] = []
    for trig in triggers:
        metric = trig.get("metric")
        op = trig.get("operator")
        thresh = trig.get("threshold")
        if not isinstance(metric, str) or not metric:
            results.append({**trig, "evaluated": False, "fired": False, "observed": None, "error": "Invalid metric name"})
            continue
        if metric not in observations or observations[metric] is None:
            results.append({**trig, "evaluated": False, "fired": False, "observed": None})
            continue
        obs = observations[metric]

        if op in (">", ">=", "<", "<="):
            try:
                obs_f = float(obs)
                thr_f = float(thresh)
            except (TypeError, ValueError):
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": obs,
                    "error": f"Non-numeric comparison for operator {op!r}",
                })
                continue
            if not (math.isfinite(obs_f) and math.isfinite(thr_f)):
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": obs,
                    "error": f"Non-finite numeric comparison for operator {op!r}",
                })
                continue
            if op == ">":
                fired = obs_f > thr_f
            elif op == ">=":
                fired = obs_f >= thr_f
            elif op == "<":
                fired = obs_f < thr_f
            else:
                fired = obs_f <= thr_f
            results.append({**trig, "evaluated": True, "fired": fired, "observed": obs})
        elif op == "==":
            def _is_nonfinite_token(v: Any) -> bool:
                if isinstance(v, float):
                    return not math.isfinite(v)
                if isinstance(v, str) and v.strip().lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
                    return True
                return False

            if _is_nonfinite_token(obs) or _is_nonfinite_token(thresh):
                results.append({
                    **trig,
                    "evaluated": False,
                    "fired": False,
                    "observed": obs,
                    "error": "Non-finite value rejected for '==' trigger",
                })
                continue
            results.append({**trig, "evaluated": True, "fired": obs == thresh, "observed": obs})
        elif op == "contains":
            results.append({**trig, "evaluated": True, "fired": str(thresh) in str(obs), "observed": obs})
        elif op == "event":
            if isinstance(obs, bool):
                fired = obs if (thresh in (None, True, "")) else (obs == bool(thresh))
            elif thresh in (None, True, ""):
                fired = bool(obs)
            else:
                fired = str(thresh).lower() in str(obs).lower()
            results.append({**trig, "evaluated": True, "fired": fired, "observed": obs})
        else:
            results.append({
                **trig,
                "evaluated": False,
                "fired": False,
                "observed": obs,
                "error": f"Unsupported trigger operator: {op!r}",
            })
    return results


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
        except Exception as exc:
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
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
