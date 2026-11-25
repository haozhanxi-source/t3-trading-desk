"""Headless AI deep-research orchestrator for Trading Desk research archives.

Runs a structured 5-stage pipeline inside the backend TaskManager:
1. Collect live whitelist hard data (research_data) + dual-timeframe Chanlun (market_snapshot)
   and load/initialize the target archive.
2. Package a compact, zero-fabrication research brief JSON into an isolated temp directory.
3. Execute a read-only headless AI agent CLI (Claude Code or Codex) with web search access
   to perform Layer 3 research + independent Bull/Bear adversarial debate + Layer 4 execution
   guidance, streaming live progress events over SSE.
4. Parse, sanitize, and validate the structured JSON verdict (preserving ``holding`` status
   and enforcing valid L1-L4 evidence, sentinel trigger schemas, and event dates).
5. Atomically persist evidence, thesis body (preserving append-only revision logs), and
   structured archive state through ``research_archive``, then invalidate stale caches.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import daily_sentinel
import market_snapshot
import research_archive
import research_data

from .cache import CACHE, TTL_RESEARCH_SECONDS
from .serializers import sanitize

Emit = Callable[[str, dict[str, Any]], None]

VALID_MODES = ("standard", "adversarial")
VALID_ENGINES = ("claude", "codex")

DEFAULT_STAGE_TIMEOUT_SECONDS = 540
ADVERSARIAL_DEBATE_TIMEOUT_SECONDS = 300
MAX_STDERR_BYTES = 8192
MAX_BRIEF_TEXT_CHARS = 8000

SENTINEL_METRIC_HINTS = (
    "close, daily_zd, daily_zg, intraday_30m_zd, intraday_30m_zg, "
    "return_1d_pct, return_5d_pct, atr14, atr14_30m, "
    "basis_pct, spot_price, inventory_latest, inventory_chg_pct, "
    "roll_yield_near_pct, roll_yield_curve_pct, front_far_spread, curve_structure, "
    "cftc_mm_net, cftc_mm_net_pct_oi, "
    "rate_diff_10y_bp, rate_diff_2y_bp, tff_lev_money_net, tff_pair_aligned_net, "
    "pe_ttm, pb, pe_ttm_pctile_5y, pb_pctile_5y, forward_pe, short_pct_float, "
    "target_mean_price, days_to_earnings, "
    "etf_flow_5d_m, etf_flow_20d_m, stablecoin_chg_7d_pct, perp_oi_usd, perp_funding_ann_pct"
)

_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DeepResearchCancelledError(RuntimeError):
    """Raised internally when a deep-research task is cancelled by the user."""


def _candidate_bin_paths(name: str, env_var: str) -> list[str]:
    candidates: list[str] = []
    override = (os.environ.get(env_var) or "").strip()
    if override:
        candidates.append(override)
    which_hit = shutil.which(name)
    if which_hit:
        candidates.append(which_hit)
    home = os.path.expanduser("~")
    candidates.extend(
        [
            os.path.join(home, ".local", "bin", name),
            f"/usr/local/bin/{name}",
            f"/usr/bin/{name}",
        ]
    )
    seen: set[str] = set()
    unique: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def resolve_engine_binary(engine: str) -> str | None:
    eng = (engine or "").strip().lower()
    if eng == "claude":
        paths = _candidate_bin_paths("claude", "TRADING_DESK_CLAUDE_BIN")
    elif eng == "codex":
        paths = _candidate_bin_paths("codex", "TRADING_DESK_CODEX_BIN")
    else:
        return None
    for path in paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def detect_available_engines() -> dict[str, Any]:
    """Probe headless AI CLI binaries installed on the host."""
    claude_bin = resolve_engine_binary("claude")
    codex_bin = resolve_engine_binary("codex")
    engines = [
        {
            "id": "claude",
            "label": "Claude Code",
            "description": "默认主力引擎 · 原生联网检索 + 深度投研推演",
            "available": bool(claude_bin),
            "binary": claude_bin,
        },
        {
            "id": "codex",
            "label": "Codex",
            "description": "备选引擎 · 跨模型独立红蓝对抗反方主力",
            "available": bool(codex_bin),
            "binary": codex_bin,
        },
    ]
    default_engine = "claude" if claude_bin else ("codex" if codex_bin else None)
    adversarial_cross_model = bool(claude_bin and codex_bin)
    return {
        "engines": engines,
        "default_engine": default_engine,
        "any_available": bool(default_engine),
        "adversarial_cross_model": adversarial_cross_model,
    }


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    home = os.path.expanduser("~")
    extra_dirs = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".bun", "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    cur_path = env.get("PATH", "")
    parts = [p for p in cur_path.split(os.pathsep) if p]
    for d in extra_dirs:
        if d not in parts and os.path.isdir(d):
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _compact_chanlun_tf(tf: Any) -> dict[str, Any] | None:
    if not isinstance(tf, dict):
        return None
    zs_list = tf.get("zhongshu") or tf.get("zhongshus") or []
    strokes = tf.get("strokes") or []
    return {
        "direction": tf.get("direction"),
        "structure": tf.get("structure") or tf.get("trend_structure"),
        "latest_fractal": tf.get("latest_fractal"),
        "momentum": tf.get("momentum"),
        "momentum_reliable": tf.get("momentum_reliable"),
        "signals": tf.get("signals"),
        "recent_zhongshu": zs_list[-3:] if isinstance(zs_list, list) else [],
        "recent_strokes": strokes[-6:] if isinstance(strokes, list) else [],
    }


def _compact_snapshot(snap: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {}
    chanlun = snap.get("chanlun") if isinstance(snap.get("chanlun"), dict) else {}
    return {
        "symbol": snap.get("symbol"),
        "asset_class": snap.get("asset_class"),
        "data_source": snap.get("data_source"),
        "intraday_data_source": snap.get("intraday_data_source"),
        "fetched_at_utc": snap.get("fetched_at_utc"),
        "data_quality": snap.get("data_quality"),
        "intraday_data_quality": snap.get("intraday_data_quality"),
        "technicals": snap.get("technicals"),
        "intraday_technicals": snap.get("intraday_technicals"),
        "fundamentals": snap.get("fundamentals"),
        "crypto_perp_funding": snap.get("crypto_perp_funding"),
        "intraday_error": snap.get("intraday_error"),
        "chanlun": {
            "signal_summary": chanlun.get("signal_summary"),
            "intraday_signal_summary": chanlun.get("intraday_signal_summary"),
            "dual_alignment": chanlun.get("dual_alignment"),
            "daily": _compact_chanlun_tf(chanlun.get("daily")),
            "intraday": _compact_chanlun_tf(chanlun.get("intraday")),
        },
    }


def _compact_bundle(bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "symbol",
        "asset_class",
        "fetched_at_utc",
        "sources_status",
        "fundamentals_and_supply",
        "macro_dual_core",
    ):
        if key in bundle:
            out[key] = bundle[key]
    return out


def build_research_brief(
    *,
    symbol: str,
    market: str,
    asset_class: str,
    archive_loaded: dict[str, Any],
    research_bundle: dict[str, Any] | None,
    dual_snapshot: dict[str, Any] | None,
    focus_prompt: str = "",
) -> dict[str, Any]:
    """Assemble a compact, self-contained JSON brief for headless research agents."""
    state = archive_loaded.get("state") if isinstance(archive_loaded.get("state"), dict) else {}
    existing_md = str(archive_loaded.get("thesis_md") or "")
    if len(existing_md) > MAX_BRIEF_TEXT_CHARS:
        existing_md = existing_md[:MAX_BRIEF_TEXT_CHARS] + "\n...[truncated]..."

    return sanitize(
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "target": {
                "symbol": symbol,
                "market": market,
                "asset_class": asset_class,
                "name": (state.get("instrument") or {}).get("name") or symbol,
                "subtype": (state.get("instrument") or {}).get("subtype") or "",
            },
            "focus_prompt": focus_prompt.strip(),
            "existing_archive": {
                "revision": state.get("revision"),
                "status": state.get("status"),
                "direction_bias": state.get("direction_bias"),
                "thesis_summary": state.get("thesis_summary"),
                "layer1_drivers": state.get("layer1_drivers"),
                "triggers": state.get("triggers") or [],
                "events": state.get("events") or [],
                "recent_evidence": (archive_loaded.get("recent_evidence") or [])[:25],
                "existing_thesis_excerpt": existing_md,
            },
            "hard_data_bundle": _compact_bundle(research_bundle),
            "dual_timeframe_snapshot": _compact_snapshot(dual_snapshot),
        }
    )


def _asset_specific_rules(asset_class: str, market: str) -> str:
    ac = (asset_class or "").lower()
    mk = (market or "").upper()
    if ac in ("cn_future", "commodity") or mk in ("CN_FUTURES", "COMMODITIES"):
        return (
            "【大宗商品 / 期货专属研究要求（多空完全对称）】\n"
            "- 围绕当下与未来 Q+1~Q+4 供需平衡表（存量×开工 + 新产能投产爬坡 − 减产/资源国政策加权扰动 − 下游分拆需求）确立主方向。\n"
            "- 用硬数据验真：显性/隐性库存、现货基差、国内/海外期限结构与年化展期收益（Roll Yield）、内外跨市价差/进口比价、海运指数（若适用）、行业现金成本曲线与 CFTC 持仓（若适用）。\n"
            "- 强现实（大 B 结构 / Backwardation / 短期低库存）绝不是做空的硬否决条件；当市场定价未来过剩预期时必须客观评估“强现实 vs 弱预期”劈叉，并用展期成本指导近远月合约选择。\n"
            "- 资源国政策（如印尼镍矿 RKAB 配额、智利/刚果金矿业税、OPEC+ 等）必须区分“生效前抢出口短松”与“生效后永久减量长紧”，按落地概率量化。\n"
            "- 反方红蓝对抗必答 5 问：① 隐性库存黑箱是否背离账面缺口？② 政策生效前是否存在抢出口冲库错配？③ 高成本产能复产弹性与现金成本位？④ 高价引发的需求替代/负反馈？⑤ 供需平衡表与交易结构的关键证伪阈值？\n"
        )
    if ac == "fx" or mk == "FX":
        return (
            "【外汇 FX 专属研究要求（多空完全对称）】\n"
            "- 围绕双方 10Y/2Y 国债利差、央行货币政策路径、通胀预期与贸易/经常账户确立主方向。\n"
            "- 用 CFTC 金融期货持仓（TFF 杠杆基金/资管，注意反向报价换算）与套息交易拥挤度交叉验真。\n"
            "- 明确央行干预红线与未来议息/CPI/非农事件窗口。\n"
            "- 反方红蓝对抗必答 5 问：① 双方央行政策路径重定价 50bp 对公允锚的冲击？② 杠杆基金与套息头寸是否过度拥挤面临踩踏？③ 央行实盘干预或政策红线在哪？④ 能源/关税冲击是否逆转贸易与利差传导？⑤ 核心宏观与结构证伪条件？\n"
        )
    if ac == "crypto" or mk == "CRYPTO":
        return (
            "【加密货币专属研究要求（多空完全对称）】\n"
            "- 结合宏观流动性、稳定币总市值净流、BTC/ETH 现货 ETF 每日净流、永续合约 OI 与资金费率极值交叉验真。\n"
            "- 反方红蓝对抗必答 5 问：① 宏观紧缩或美元流动性回撤冲击？② ETF 申赎转向与机构筹码松动？③ 永续杠杆拥挤度与多空踩踏风险？④ 监管/解锁抛压？⑤ 核心链上与资金流证伪阈值？\n"
        )
    return (
        "【美股 / A 股专属研究要求（以做多为主，卖点用于止盈/风控/对冲）】\n"
        "- 先定型（A 成长 / B 强周期 / C 稳定现金流 / D 困境反转）并量化**受益纯度**（核心主逻辑贡献的真实收入占比，伪概念直接否决）与**瓶颈久期**（底层材料/工艺 5~10 年、客户验证 2~5 年且警惕大客户自研绕过、纯产能缺货 1~3 年一律按 B 型周期股处理，不可把短期缺货暴利乘以成长股 PE）。\n"
        "- 严格按未来 1~3 年远期预期折现定价，严禁死板套用静态当期 PE（强周期股牢记“买在高 PE/微利拐点、卖在低 PE/暴利顶点”）。\n"
        "- **订单与收入兑现审计**：将公告/合作按七阶段严格分级（科学验证 / 产品导入[多数战略合作止步于此] / 客户验证 / 具法律约束力采购订单 PO / 量产爬坡 / 稳定复购 / 未证实传闻），仅 PO 及以后阶段计入确定性收入预测；明确剥离一次性工程收入（NRE）与可规模化产品收入。\n"
        "- **分部拆解与周期顶部双面核验**：拆分高毛利新业务结构改善（Mix Shift）、衰退老业务单独按悲观扣减（Drag Segment）及下半年集中放量陷阱（H2 Weighting：按 H1 实绩倒推 H2 隐含环比增速）；核查存货与应收账款增速是否快于营收；对半导体/硬科技的交期拉长、Book-to-Bill > 1、长协与配给状态做**双面核验**（警惕 2021~22 式重复下单周期顶，同步追踪月度营收、现货价与“利好兑现不创新高”信号）。\n"
        "- **期后 8-K 流动性桥与稀释/回购审计**：结合 `brief.json` 中最新 10-Q/10-K 与期后 `8-K` 重大事件编制带截止日的净现金/净债务桥（扣减已交割收购现金对价、计入新增债务与受限现金）；审计 3~5 年期末流通股数变动、SBC、ATM 增发、可转债/Warrants 稀释；核验真实回购弹药（按美股 Rule 10b-18 单日 ≤ 日均成交额 25% 折算，且 CFO 最新资本配置表态高于历史回购规律）。\n"
        "- **情景压力测试与现价隐含预期拆解**：执行牛 / 基准 / 熊1（EPS 持平 + PE 回 5 年中枢）/ 熊2（EPS -10% + PE 压缩）/ 叙事崩塌五情景压力测试，严禁混算 GAAP 与 Adjusted 口径；可反推现价中的“常态基础价值 + 有限期周期增量折现 + 叙事/期权残差区间”用于刻画风险暴露，**严禁用主观概率加权目标价或 30% 溢价线直接决定买卖/仓位，严禁主张“因果链完好时杀估值就逆势加仓”**。\n"
        "- **反方红蓝对抗必答 7 问 + 红队 Pre-Mortem**：① EPS 持平、PE 回历史中枢跌多少？② 按订单七阶段剔除软订单/框架预估与 NRE 后，真实可放量增速剩多少（含 H2 放量与衰退老业务拖累）？③ 利润率 -1pp 对 EPS 冲击多大？④ 预期差与叙事溢价是否已被当前股价充分定价？⑤ 最大单一客户/需求链断供或自研替代后果？⑥ 资本结构、期后现金消耗或 ATM/SBC 稀释风险有多大？⑦ 历史最近一次证伪案例与核心领先证伪指标？**另附 3~5 句具体可证伪的红队 Pre-Mortem 失败路径**（假设 6 个月后大亏，写出具体触发链并映射可监控指标）。\n"
    )


def build_research_prompt(
    *,
    brief_path: str,
    symbol: str,
    market: str,
    asset_class: str,
    name: str,
    focus_prompt: str = "",
    adversarial_critique: str | None = None,
) -> str:
    """Build the master Layer 3 + Layer 4 research prompt requiring strict JSON output."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    focus_block = f"\n【用户指定的本次研究侧重点】\n{focus_prompt.strip()}\n" if focus_prompt.strip() else ""
    critique_block = (
        "\n【独立反方（跨模型魔鬼代言人）质询意见 —— 你必须在研报的红蓝对抗章节逐条正面回应，并据此修正方向、置信度与证伪阈值】\n"
        f"{adversarial_critique.strip()}\n"
        if adversarial_critique
        else ""
    )

    return (
        f"你是 T3 Trading Desk（中低频多智能体投研台 · 缠论版）的首席投研与风控系统。今天是 {today}。\n"
        f"请先使用 Read 工具读取当前目录下的研究简报文件 `{brief_path}`，然后结合必要的实时 WebSearch / WebFetch 联网检索，\n"
        f"针对标的 **{name}（{symbol}，市场 `{market}`，类别 `{asset_class}`）** 完成一份严谨、可执行的 1Q~1Y 深度投研报告与结构化档案数据。\n\n"
        "【硬性交易哲学与纪律（绝对不可违背）】\n"
        "1. 严禁使用 RSI 超买/超卖、布林带或均线回踩作为买卖依据；极端单边往往是资金方向高度一致（未背驰离开段）。\n"
        "2. 日线定主方向、中枢 `[ZD, ZG]` 与空间；30 分钟（30min）缠论结构诞生精确入场与紧凑战术止损。\n"
        "3. 0-编造红线：所有价格、缠论中枢、库存、基差、期限结构、利差、财报、宏观指标必须优先引用 `brief.json` 中的实测硬数据；联网检索补齐的政策/供需/产业事实必须标明来源与日期并严格分级（L1=官方/交易所/财报原表，L2=权威行业协会/公司指引，L3=卖方研报，L4=媒体/快讯）。未核验项写进 `pending_verifications`，绝不杜撰数字。\n"
        "4. 第三层（纯研究 + 红蓝对抗）与第四层（日线定方向 + 30min 执行表达 + 撤离红线）职责清晰分离，但在一份完整的 `thesis_md` 中分层呈现。\n\n"
        f"{_asset_specific_rules(asset_class, market)}"
        f"{focus_block}"
        f"{critique_block}\n"
        "【输出格式铁律】\n"
        "你的最终回复必须**仅包含一个合法的 JSON 对象**（可用 ```json ... ``` 包裹，JSON 外不要输出任何额外说明文字），严格遵循以下 schema：\n"
        "```json\n"
        "{\n"
        '  "thesis_summary": "一句话（<=120字）提炼 1Q~1Y 核心预期差与当前多空/观望结论",\n'
        '  "direction_bias": "long | short | neutral | pair",\n'
        '  "status": "watchlist | actionable_long | actionable_short | pair_trade | rejected",\n'
        '  "layer1_drivers": {\n'
        '    "primary": "主驱动维度（如：资源国政策叙事 / 供需平衡预期 / 宏观双核 / 技术产业迭代）+ 一句话要点",\n'
        '    "secondary": "次驱动维度 + 一句话要点"\n'
        "  },\n"
        '  "chanlun_anchors": {\n'
        '    "daily_direction": "多头 | 空头 | 震荡中枢",\n'
        '    "daily_zs": [0, 0],\n'
        '    "intraday_30m_signal": "30min 缠论状态与触发条件简述",\n'
        '    "tactical_stop_30m": null,\n'
        '    "structural_stop_daily": null\n'
        "  },\n"
        '  "valuation_or_balance": {\n'
        '    "regime": "简短定性（如：强现实弱预期劈叉 / 过剩压制 / 戴维斯双击 / 周期底部）",\n'
        '    "key_anchor": "核心锚定指标与数值（带单位）",\n'
        '    "upside_vs_downside": "牛/基准/熊情景空间简述",\n'
        '    "implied_expectation_breakdown": "可选（仅股票）：常态基础价值 + 周期折现 + 叙事残差区间（纯风险刻画，不作为入场门槛）"\n'
        "  },\n"
        '  "thesis_md": "完整的简体中文 Markdown 研报正文（不要包含底部『## 增量修订日志』章节，系统会自动保留并追加）。必须包含：\\n# 标题与核心信息卡\\n## 第三层：1Q~1Y 纯深度研究（含 7 维共振逻辑、供需平衡表/估值预期差、硬数据验真、情景分析）\\n## 第三层：红蓝对抗（逐条回答反方必答问、红队 Pre-Mortem 3~5 条具体失败路径并量化反向情景）\\n## 第三层研究裁决（方向、置信度、核心证伪条件）\\n## 第四层：交易表达与 30min 缠论执行框架（四象限终裁、日线主中枢 vs 30min 触发点、合约/工具与展期劈叉权衡、双层止损与撤离红线）",\n'
        '  "evidence": [\n'
        "    {\n"
        '      "metric": "指标或事实名称（清晰中文）",\n'
        '      "value": "数值或核心结论（带单位）",\n'
        '      "unit": "单位（可空）",\n'
        '      "period": "对应时间段/日期（如 2026-11 / Q4 2026）",\n'
        '      "basis": "口径说明（如 SHFE仓单 / 现货基差 / GAAP / 印尼能源部官方）",\n'
        '      "kind": "fact | external_forecast | assumption",\n'
        '      "level": "L1 | L2 | L3 | L4",\n'
        '      "published_at": "YYYY-MM-DD（可空）",\n'
        '      "source_url": "权威来源URL（可空）",\n'
        '      "source_locator": "来源名称或简报字段路径",\n'
        '      "note": "对多空论点的含义（1句）"\n'
        "    }\n"
        "  ],\n"
        '  "triggers": [\n'
        "    {\n"
        '      "id": "trig_unique_snake_case",\n'
        '      "kind": "chanlun | supply_demand | term_structure | rate_diff | fundamental | policy | spread | freight | macro",\n'
        '      "evaluation_mode": "auto（标准机审键）| ai_web（政策/事件等需AI联网核验的非标指标）",\n'
        '      "metric": "机审触发器必须精确使用哨兵标准键名（如 close / daily_zd / daily_zg / basis_pct / inventory_latest / roll_yield_near_pct / rate_diff_10y_bp）；政策/定性项可自定义英文snake_case并标 evaluation_mode=ai_web",\n'
        '      "operator": "> | >= | < | <= | == | contains | event",\n'
        '      "threshold": 12345,\n'
        '      "action": "exit_review | trim_or_exit | alert",\n'
        '      "status": "armed",\n'
        '      "description": "触发后的明确含义、数据核验口径与动作"\n'
        "    }\n"
        "  ],\n"
        '  "events": [\n'
        '    {"date": "YYYY-MM-DD", "title": "未来 1~4 季度关键政策/财报/议息/移仓催化事件"}\n'
        "  ],\n"
        '  "pending_verifications": ["尚待核验的硬数据缺口或跟踪项"],\n'
        '  "revision_note": "一句话总结本次深度研究核心结论与更新内容"\n'
        "}\n"
        "```\n"
        f"提示：哨兵自动支持的标准化 metric 键名包括：{SENTINEL_METRIC_HINTS}。"
        "请至少提供 5~10 条高质量 L1~L4 证据、3~6 条可执行撤离红线/监控触发器（量化价格/结构/库存/基差/曲线/利差类必须精确选用上述标准键名并标 evaluation_mode=auto；政策或突发事件类非标指标可标 evaluation_mode=ai_web 并写清核验口径）以及 2~5 条未来催化/移仓事件。"
    )


def build_adversarial_prompt(
    *,
    brief_path: str,
    symbol: str,
    market: str,
    asset_class: str,
    name: str,
    draft_summary: str,
    draft_direction: str,
    draft_thesis_md: str,
) -> str:
    """Build the independent Red-Team (Devil's Advocate) prompt for cross-model debate."""
    excerpt = draft_thesis_md[:6000] + ("\n...[truncated]..." if len(draft_thesis_md) > 6000 else "")
    return (
        f"你是独立的反方魔鬼代言人（Red Team / Bear or Counter-Thesis Auditor）。\n"
        f"请读取 `{brief_path}` 中的实测硬数据，审查主研究员针对 **{name}（{symbol} · {market}）** 给出的初稿结论：\n"
        f"- 主研方向倾向：`{draft_direction}`\n"
        f"- 主研一句话论点：{draft_summary}\n\n"
        f"【主研初稿摘录】\n{excerpt}\n\n"
        f"{_asset_specific_rules(asset_class, market)}\n"
        "【你的任务】\n"
        "1. 站在严格反方立场，逐条攻击主研论点中最脆弱的假设、数据缺口、隐性库存/预期差陷阱或缠论结构冲突。\n"
        "2. 必须逐条回答上述对应资产类别的「反方必答问」，并给出至少 1 个**可量化的反向情景**（给出具体下跌/反向幅度估算与触发机制）。\n"
        "3. **红队 Pre-Mortem（事前验尸）**：假设 6 个月后这笔研究/头寸亏了大钱，写出 3~5 条具体、可证伪的失败路径（写明具体哪家客户砍单/哪个验证卡壳/哪项政策或供需逆转/哪笔期后融资稀释导致失败），每条绑定一个可监控的领先证伪指标。\n"
        "4. 指出哪些关键撤离红线阈值设得过宽或遗漏。\n"
        "请直接输出清晰、犀利、结构化的简体中文 Markdown 反方质询报告（不需要 JSON，800~1500 字）。"
    )


def parse_claude_stream_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line from ``claude -p --output-format stream-json --verbose``."""
    raw = (line or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    etype = obj.get("type")
    if etype == "assistant":
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        activities: list[str] = []
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            ctype = item.get("type")
            if ctype == "tool_use":
                tname = str(item.get("name") or "")
                tinput = item.get("input") if isinstance(item.get("input"), dict) else {}
                if tname == "WebSearch":
                    q = str(tinput.get("query") or "").strip()
                    activities.append(f"联网检索：{q[:80]}" if q else "正在执行联网检索…")
                elif tname == "WebFetch":
                    u = str(tinput.get("url") or "").strip()
                    activities.append(f"抓取权威来源：{u[:80]}" if u else "正在读取网页来源…")
                elif tname == "Read":
                    activities.append("读取硬数据与缠论简报…")
                elif tname:
                    activities.append(f"调用工具：{tname}")
            elif ctype == "text":
                txt = str(item.get("text") or "").strip()
                if txt:
                    text_parts.append(txt)
        return {
            "kind": "assistant",
            "activities": activities,
            "text": "\n".join(text_parts),
        }

    if etype == "result":
        return {
            "kind": "result",
            "is_error": bool(obj.get("is_error")),
            "subtype": obj.get("subtype"),
            "result": str(obj.get("result") or ""),
            "duration_ms": obj.get("duration_ms"),
            "total_cost_usd": obj.get("total_cost_usd"),
        }
    return None


def parse_codex_stream_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line from ``codex exec --json``."""
    raw = (line or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    etype = obj.get("type")
    if etype in ("item.started", "item.completed"):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = str(item.get("type") or "")
        if itype == "agent_message" and etype == "item.completed":
            return {"kind": "agent_message", "text": str(item.get("text") or "")}
        if itype == "web_search":
            q = str(item.get("query") or "").strip()
            return {
                "kind": "activity",
                "activities": [f"联网检索：{q[:80]}" if q else "正在执行联网检索…"],
            }
        if itype == "command_execution" and etype == "item.started":
            return {"kind": "activity", "activities": ["读取硬数据与缠论简报…"]}
        if itype == "reasoning" and etype == "item.completed":
            txt = str(item.get("text") or "").strip()
            if txt:
                first_line = txt.splitlines()[0].strip()[:80]
                return {"kind": "activity", "activities": [f"深度推演：{first_line}"]}
        if itype == "error":
            return {"kind": "warning", "message": str(item.get("message") or "")}
    if etype in ("error", "turn.failed"):
        msg = obj.get("message") or (obj.get("error") or {}).get("message") or str(obj)
        return {"kind": "error", "message": str(msg)}
    return None


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.08)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_agent_cli(
    *,
    engine: str,
    prompt: str,
    cwd: str,
    effort: str,
    emit: Emit,
    stage: str,
    timeout_seconds: int = DEFAULT_STAGE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run headless Claude or Codex CLI in a read-only isolated directory with live streaming."""
    binary = resolve_engine_binary(engine)
    if not binary:
        raise RuntimeError(f"本机未找到可用研究引擎 `{engine}`（请确认已安装并登录对应 CLI）")

    if engine == "claude":
        cmd = [
            binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
            "--effort",
            effort,
            "--allowedTools",
            "Read,WebSearch,WebFetch",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit",
            "--strict-mcp-config",
            prompt,
        ]
    elif engine == "codex":
        cmd = [
            binary,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-C",
            cwd,
            prompt,
        ]
    else:
        raise ValueError(f"Unsupported research engine: {engine!r}")

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    stderr_chunks: list[str] = []
    stderr_size = 0
    last_text = ""
    result_text = ""
    result_meta: dict[str, Any] = {"engine": engine}
    cli_error: str | None = None
    start_ts = time.monotonic()
    last_heartbeat = start_ts

    sel = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

    try:
        while sel.get_map():
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process_group(proc)
                raise DeepResearchCancelledError("用户已终止深度研究任务")

            elapsed = time.monotonic() - start_ts
            if elapsed > timeout_seconds:
                _terminate_process_group(proc)
                raise TimeoutError(f"研究引擎 `{engine}` 执行超过 {timeout_seconds}s 超时")

            if time.monotonic() - last_heartbeat >= 12.0:
                last_heartbeat = time.monotonic()
                emit(
                    "log",
                    {
                        "stage": stage,
                        "engine": engine,
                        "message": f"{engine.upper()} 持续研究推演中…（已耗时 {int(elapsed)}s）",
                    },
                )

            events = sel.select(timeout=0.25)
            for key, _ in events:
                stream = key.fileobj
                line = stream.readline()  # type: ignore[union-attr]
                if not line:
                    sel.unregister(stream)
                    continue
                if key.data == "stderr":
                    if stderr_size < MAX_STDERR_BYTES:
                        stderr_chunks.append(line)
                        stderr_size += len(line)
                    continue

                if engine == "claude":
                    parsed = parse_claude_stream_line(line)
                    if not parsed:
                        continue
                    if parsed["kind"] == "assistant":
                        for act in parsed.get("activities") or []:
                            last_heartbeat = time.monotonic()
                            emit("log", {"stage": stage, "engine": engine, "message": act})
                        if parsed.get("text"):
                            last_text = parsed["text"]
                    elif parsed["kind"] == "result":
                        result_meta["duration_ms"] = parsed.get("duration_ms")
                        result_meta["total_cost_usd"] = parsed.get("total_cost_usd")
                        if parsed.get("is_error"):
                            cli_error = parsed.get("result") or f"Claude CLI error ({parsed.get('subtype')})"
                        else:
                            result_text = parsed.get("result") or ""
                else:
                    parsed = parse_codex_stream_line(line)
                    if not parsed:
                        continue
                    if parsed["kind"] == "activity":
                        for act in parsed.get("activities") or []:
                            last_heartbeat = time.monotonic()
                            emit("log", {"stage": stage, "engine": engine, "message": act})
                    elif parsed["kind"] == "agent_message":
                        if parsed.get("text"):
                            last_text = parsed["text"]
                            result_text = parsed["text"]
                    elif parsed["kind"] == "error":
                        cli_error = parsed.get("message") or "Codex execution failed"

        rc = proc.wait(timeout=5)
    finally:
        sel.close()
        _terminate_process_group(proc)

    if cancel_event is not None and cancel_event.is_set():
        raise DeepResearchCancelledError("用户已终止深度研究任务")

    final_text = (result_text or last_text).strip()
    if cli_error:
        raise RuntimeError(f"研究引擎 `{engine}` 返回错误：{cli_error[:400]}")
    if rc != 0 and not final_text:
        err_tail = "".join(stderr_chunks).strip()[-400:]
        raise RuntimeError(f"研究引擎 `{engine}` 异常退出 (code={rc})：{err_tail or '无错误输出'}")
    if not final_text:
        raise RuntimeError(f"研究引擎 `{engine}` 未返回有效研究输出")

    result_meta["elapsed_s"] = round(time.monotonic() - start_ts, 1)
    return final_text, result_meta


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Extract a JSON object from raw agent output (handles fenced blocks and preamble text)."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("研究输出为空，无法解析结构化结果")

    # 1. Direct JSON parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2. Try fenced ```json ... ``` blocks (prefer the last valid dict block)
    matches = _FENCE_JSON_RE.findall(text)
    for block in reversed(matches):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # 3. Balanced brace scan from outermost '{' to '}'
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError("无法从研究输出中提取合法的 JSON 结构化结果")


def normalize_research_output(
    payload: dict[str, Any],
    *,
    existing_state: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    """Validate and sanitize parsed AI output into strict research_archive structures."""
    if not isinstance(payload, dict):
        raise ValueError("研究输出必须是 JSON 对象")

    thesis_md = str(payload.get("thesis_md") or "").strip()
    if len(thesis_md) < 80:
        raise ValueError("生成的研报正文过短或为空，已中止写入以保护原档案")

    # Direction bias
    raw_dir = str(payload.get("direction_bias") or "neutral").strip().lower()
    direction_bias = raw_dir if raw_dir in research_archive.VALID_DIRECTIONS else "neutral"

    # Status: NEVER downgrade an active 'holding' position!
    existing_status = str(existing_state.get("status") or "watchlist").strip().lower()
    raw_status = str(payload.get("status") or "watchlist").strip().lower()
    if existing_status == "holding":
        status = "holding"
    elif raw_status in research_archive.VALID_STATUSES and raw_status != "holding":
        status = raw_status
    else:
        status = "watchlist"

    thesis_summary = str(payload.get("thesis_summary") or "").strip()
    if not thesis_summary:
        thesis_summary = f"{symbol} 完成 AI 深度研究（方向倾向：{direction_bias}）"

    # Layer 1 drivers
    raw_l1 = payload.get("layer1_drivers") if isinstance(payload.get("layer1_drivers"), dict) else {}
    layer1_drivers = {
        "primary": str(raw_l1.get("primary") or "").strip(),
        "secondary": str(raw_l1.get("secondary") or "").strip(),
    }

    chanlun_anchors = payload.get("chanlun_anchors") if isinstance(payload.get("chanlun_anchors"), dict) else {}
    valuation_or_balance = (
        payload.get("valuation_or_balance") if isinstance(payload.get("valuation_or_balance"), dict) else {}
    )

    # Evidence normalization
    clean_evidence: list[dict[str, Any]] = []
    for raw_ev in payload.get("evidence") or []:
        if not isinstance(raw_ev, dict):
            continue
        metric = str(raw_ev.get("metric") or "").strip()
        if not metric:
            continue
        level = str(raw_ev.get("level") or "L3").strip().upper()
        if level not in research_archive.VALID_EVIDENCE_LEVELS:
            level = "L4"
        kind = str(raw_ev.get("kind") or "fact").strip().lower()
        if kind not in research_archive.VALID_EVIDENCE_KINDS:
            kind = "fact"
        val = raw_ev.get("value")
        if val is None or val == "":
            continue
        clean_evidence.append(
            {
                "metric": metric,
                "value": val,
                "unit": str(raw_ev.get("unit") or "").strip(),
                "period": str(raw_ev.get("period") or "").strip(),
                "basis": str(raw_ev.get("basis") or "unspecified").strip(),
                "kind": kind,
                "level": level,
                "published_at": str(raw_ev.get("published_at") or "").strip(),
                "source_url": str(raw_ev.get("source_url") or "").strip(),
                "source_locator": str(raw_ev.get("source_locator") or "AI Deep Research").strip(),
                "note": str(raw_ev.get("note") or "").strip(),
            }
        )

    # Triggers normalization
    clean_triggers: list[dict[str, Any]] = []
    seen_tids: set[str] = set()
    for idx, raw_trig in enumerate(payload.get("triggers") or [], start=1):
        if not isinstance(raw_trig, dict):
            continue
        metric = str(raw_trig.get("metric") or "").strip()
        operator = str(raw_trig.get("operator") or "").strip()
        if not metric or operator not in research_archive.VALID_OPERATORS:
            continue
        if "threshold" not in raw_trig or raw_trig.get("threshold") is None:
            continue
        tid = str(raw_trig.get("id") or f"trig_ai_{idx}").strip()
        if not tid or tid in seen_tids:
            tid = f"trig_ai_{idx}"
        seen_tids.add(tid)
        kind = str(raw_trig.get("kind") or "fundamental").strip().lower()
        if kind not in research_archive.VALID_TRIGGER_KINDS:
            kind = "fundamental"
        action = str(raw_trig.get("action") or "alert").strip()
        if not action:
            action = "alert"
        raw_mode = str(raw_trig.get("evaluation_mode") or "").strip().lower()
        if daily_sentinel.is_known_auto_metric(metric):
            eval_mode = "auto"
        elif raw_mode in {"ai_web", "manual"}:
            eval_mode = raw_mode
        else:
            eval_mode = "ai_web"
        trig_obj: dict[str, Any] = {
            "id": tid,
            "kind": kind,
            "evaluation_mode": eval_mode,
            "metric": metric,
            "operator": operator,
            "threshold": raw_trig["threshold"],
            "action": action,
            "status": str(raw_trig.get("status") or "armed").strip() or "armed",
        }
        desc = str(raw_trig.get("description") or "").strip()
        if desc:
            trig_obj["description"] = desc
        clean_triggers.append(trig_obj)

    # Events normalization
    clean_events: list[dict[str, Any]] = []
    seen_events: set[tuple[str, str]] = set()
    for raw_ev in payload.get("events") or []:
        if not isinstance(raw_ev, dict):
            continue
        dt = str(raw_ev.get("date") or "").strip()
        title = str(raw_ev.get("title") or "").strip()
        if not dt or not title or not _ISO_DATE_RE.match(dt):
            continue
        key = (dt, title)
        if key in seen_events:
            continue
        seen_events.add(key)
        clean_events.append({"date": dt, "title": title})
    clean_events.sort(key=lambda x: x["date"])

    # Pending verifications
    pending = [
        str(item).strip()
        for item in (payload.get("pending_verifications") or [])
        if str(item).strip()
    ]

    revision_note = str(payload.get("revision_note") or "").strip()
    if not revision_note:
        revision_note = f"AI 深度研究完成（方向：{direction_bias}，状态：{status}）"

    return sanitize(
        {
            "thesis_summary": thesis_summary,
            "direction_bias": direction_bias,
            "status": status,
            "layer1_drivers": layer1_drivers,
            "chanlun_anchors": chanlun_anchors,
            "valuation_or_balance": valuation_or_balance,
            "thesis_md": thesis_md,
            "evidence": clean_evidence,
            "triggers": clean_triggers,
            "events": clean_events,
            "pending_verifications": pending,
            "revision_note": revision_note,
        }
    )


def run_deep_research(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute the 5-stage AI deep-research workflow and persist results into the archive."""
    raw_symbol = str(params["symbol"]).strip()
    market_override = params.get("market")
    mode = str(params.get("mode") or "standard").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported deep-research mode: {mode!r}")

    requested_engine = str(params.get("engine") or "claude").strip().lower()
    if requested_engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported deep-research engine: {requested_engine!r}")
    if not resolve_engine_binary(requested_engine):
        raise RuntimeError(f"研究引擎 `{requested_engine}` 当前不可用，请切换其他可用引擎")

    focus_prompt = str(params.get("focus_prompt") or "").strip()
    include_macro = bool(params.get("include_macro", True))
    total_steps = 5

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DeepResearchCancelledError("用户已终止深度研究任务")

    check_cancelled()
    emit("progress", {"done": 0, "total": total_steps, "symbol": raw_symbol})
    emit(
        "step",
        {
            "stage": "collect_data",
            "symbol": raw_symbol,
            "message": f"① 正在采集 {raw_symbol} 白名单硬数据与双周期缠论结构…",
        },
    )

    # Ensure archive exists so revision and metadata are consistent
    try:
        loaded = research_archive.load_archive(raw_symbol, market_override=market_override, evidence_limit=30)
    except FileNotFoundError:
        research_archive.init_archive(raw_symbol, market_override=market_override)
        loaded = research_archive.load_archive(raw_symbol, market_override=market_override, evidence_limit=30)

    norm_symbol = str(loaded.get("symbol") or raw_symbol)
    market = str(loaded.get("market") or market_override or "")
    asset_class = str(loaded.get("asset_class") or (loaded.get("state") or {}).get("instrument", {}).get("asset_class") or "")
    inst_name = str((loaded.get("state") or {}).get("instrument", {}).get("name") or norm_symbol)

    # Collect fresh hard data + dual Chanlun snapshot (soft-degrade individually if one source has issues)
    captured_epoch = CACHE.get_epoch("research")
    research_bundle: dict[str, Any] | None = None
    dual_snapshot: dict[str, Any] | None = None
    try:
        rd_sig = inspect.signature(research_data.collect_research_data)
        rd_kw: dict[str, Any] = {"include_macro": include_macro}
        if "refresh" in rd_sig.parameters:
            rd_kw["refresh"] = True
        research_bundle = research_data.collect_research_data(norm_symbol, **rd_kw)
        CACHE.set(
            ("research", norm_symbol.strip(), include_macro),
            research_bundle,
            TTL_RESEARCH_SECONDS,
            expected_epoch=captured_epoch,
        )
    except Exception as exc:  # noqa: BLE001
        emit("log", {"stage": "collect_data", "message": f"硬数据采集部分受限（将继续研究）：{exc}"})

    check_cancelled()
    try:
        dual_snapshot = market_snapshot.get_snapshot(
            norm_symbol,
            period="3y",
            include_macro=False,
            include_fundamentals=True,
            full_chanlun=True,
            include_intraday=True,
            intraday_interval="30m",
            refresh=True,
        )
    except Exception as exc:  # noqa: BLE001
        emit("log", {"stage": "collect_data", "message": f"双周期缠论采集部分受限：{exc}"})

    check_cancelled()
    emit("progress", {"done": 1, "total": total_steps, "symbol": norm_symbol})
    emit(
        "step",
        {
            "stage": "build_brief",
            "symbol": norm_symbol,
            "message": "② 正在封装 0-编造硬数据与缠论简报…",
        },
    )

    brief = build_research_brief(
        symbol=norm_symbol,
        market=market,
        asset_class=asset_class,
        archive_loaded=loaded,
        research_bundle=research_bundle,
        dual_snapshot=dual_snapshot,
        focus_prompt=focus_prompt,
    )

    with tempfile.TemporaryDirectory(prefix="t3_deep_research_") as workdir:
        brief_path = os.path.join(workdir, "brief.json")
        with open(brief_path, "w", encoding="utf-8") as f:
            json.dump(brief, f, ensure_ascii=False, indent=2)

        emit("progress", {"done": 2, "total": total_steps, "symbol": norm_symbol})
        check_cancelled()

        engine_runs: list[dict[str, Any]] = []
        if mode == "adversarial":
            # Stage 3A: Primary research draft
            emit(
                "step",
                {
                    "stage": "agent_research",
                    "symbol": norm_symbol,
                    "message": f"③-A 启动主研引擎（{requested_engine.upper()}）执行第三层深研与联网验真…",
                },
            )
            draft_prompt = build_research_prompt(
                brief_path=brief_path,
                symbol=norm_symbol,
                market=market,
                asset_class=asset_class,
                name=inst_name,
                focus_prompt=focus_prompt,
            )
            draft_raw, draft_meta = _run_agent_cli(
                engine=requested_engine,
                prompt=draft_prompt,
                cwd=workdir,
                effort="medium",
                emit=emit,
                stage="agent_research",
                cancel_event=cancel_event,
            )
            engine_runs.append({"phase": "primary_draft", **draft_meta})
            check_cancelled()

            try:
                draft_parsed = extract_json_payload(draft_raw)
                draft_summary = str(draft_parsed.get("thesis_summary") or "")
                draft_dir = str(draft_parsed.get("direction_bias") or "neutral")
                draft_md = str(draft_parsed.get("thesis_md") or draft_raw)
            except ValueError:
                draft_summary = ""
                draft_dir = "neutral"
                draft_md = draft_raw

            # Stage 3B: Independent Devil's Advocate (prefer cross-model if available)
            red_engine = "codex" if (requested_engine == "claude" and resolve_engine_binary("codex")) else (
                "claude" if (requested_engine == "codex" and resolve_engine_binary("claude")) else requested_engine
            )
            emit(
                "step",
                {
                    "stage": "adversarial_debate",
                    "symbol": norm_symbol,
                    "message": f"③-B 启动独立反方引擎（{red_engine.upper()}）执行红蓝对抗质询…",
                },
            )
            adv_prompt = build_adversarial_prompt(
                brief_path=brief_path,
                symbol=norm_symbol,
                market=market,
                asset_class=asset_class,
                name=inst_name,
                draft_summary=draft_summary,
                draft_direction=draft_dir,
                draft_thesis_md=draft_md,
            )
            critique_text, adv_meta = _run_agent_cli(
                engine=red_engine,
                prompt=adv_prompt,
                cwd=workdir,
                effort="medium",
                emit=emit,
                stage="adversarial_debate",
                timeout_seconds=ADVERSARIAL_DEBATE_TIMEOUT_SECONDS,
                cancel_event=cancel_event,
            )
            engine_runs.append({"phase": "red_team", **adv_meta})
            check_cancelled()

            # Stage 3C: Final synthesis incorporating red-team critique
            emit(
                "step",
                {
                    "stage": "agent_synthesis",
                    "symbol": norm_symbol,
                    "message": f"③-C 主研引擎（{requested_engine.upper()}）吸收反方质询并定稿裁决…",
                },
            )
            final_prompt = build_research_prompt(
                brief_path=brief_path,
                symbol=norm_symbol,
                market=market,
                asset_class=asset_class,
                name=inst_name,
                focus_prompt=focus_prompt,
                adversarial_critique=critique_text,
            )
            raw_output, synth_meta = _run_agent_cli(
                engine=requested_engine,
                prompt=final_prompt,
                cwd=workdir,
                effort="medium",
                emit=emit,
                stage="agent_synthesis",
                cancel_event=cancel_event,
            )
            engine_runs.append({"phase": "final_synthesis", **synth_meta})
        else:
            emit(
                "step",
                {
                    "stage": "agent_research",
                    "symbol": norm_symbol,
                    "message": f"③ 启动 {requested_engine.upper()} 深度研究与红蓝对抗（含实时联网检索）…",
                },
            )
            prompt = build_research_prompt(
                brief_path=brief_path,
                symbol=norm_symbol,
                market=market,
                asset_class=asset_class,
                name=inst_name,
                focus_prompt=focus_prompt,
            )
            raw_output, meta = _run_agent_cli(
                engine=requested_engine,
                prompt=prompt,
                cwd=workdir,
                effort="medium",
                emit=emit,
                stage="agent_research",
                cancel_event=cancel_event,
            )
            engine_runs.append({"phase": "standard", **meta})

    check_cancelled()
    emit("progress", {"done": 3, "total": total_steps, "symbol": norm_symbol})
    emit(
        "step",
        {
            "stage": "parse_and_validate",
            "symbol": norm_symbol,
            "message": "④ 正在校验研报结构、L1~L4 证据与自动化撤离红线…",
        },
    )

    payload = extract_json_payload(raw_output)
    # Re-read latest archive state right before writing so concurrent edits are respected
    latest = research_archive.load_archive(norm_symbol, market_override=market, evidence_limit=0)
    latest_state = latest["state"]
    normalized = normalize_research_output(payload, existing_state=latest_state, symbol=norm_symbol)

    check_cancelled()
    emit("progress", {"done": 4, "total": total_steps, "symbol": norm_symbol})
    emit(
        "step",
        {
            "stage": "persist_archive",
            "symbol": norm_symbol,
            "message": "⑤ 正在原子写回研报正文、证据库、触发器与催化日历…",
        },
    )

    # 1. Append L1-L4 evidence (deduplicated)
    ev_res = {"added": 0, "skipped_duplicates": 0}
    if normalized["evidence"]:
        ev_res = research_archive.append_evidence(
            norm_symbol,
            normalized["evidence"],
            market_override=market,
        )

    # 2. Atomically update thesis body (preserves append-only revision log & bumps revision)
    cur_rev = int(latest_state.get("revision", 1))
    mode_tag = "跨模型红蓝对抗" if mode == "adversarial" else "标准AI深研"
    note = f"[{mode_tag}/{requested_engine.upper()}] {normalized['revision_note']}"
    thesis_res = research_archive.update_thesis_body(
        norm_symbol,
        normalized["thesis_md"],
        expected_revision=cur_rev,
        note=note,
        market_override=market,
    )
    next_rev = int(thesis_res.get("revision", cur_rev + 1))

    # 3. Atomically patch structured state (keep existing triggers if AI returned empty list)
    state_patch: dict[str, Any] = {
        "status": normalized["status"],
        "direction_bias": normalized["direction_bias"],
        "thesis_summary": normalized["thesis_summary"],
        "layer1_drivers": normalized["layer1_drivers"],
        "chanlun_anchors": normalized["chanlun_anchors"],
        "valuation_or_balance": normalized["valuation_or_balance"],
        "pending_verifications": normalized["pending_verifications"],
    }
    if normalized["triggers"]:
        state_patch["triggers"] = normalized["triggers"]
    if normalized["events"]:
        state_patch["events"] = normalized["events"]

    final_state = research_archive.update_state(
        norm_symbol,
        state_patch,
        expected_revision=next_rev,
        market_override=market,
    )

    # Invalidate Sentinel cache so updated thesis/triggers take effect immediately
    CACHE.invalidate_prefix("sentinel")

    emit("progress", {"done": total_steps, "total": total_steps, "symbol": norm_symbol})
    return {
        "symbol": norm_symbol,
        "market": market,
        "mode": mode,
        "engine": requested_engine,
        "revision": final_state.get("revision"),
        "status": final_state.get("status"),
        "direction_bias": final_state.get("direction_bias"),
        "thesis_summary": final_state.get("thesis_summary"),
        "evidence_added": ev_res.get("added", 0),
        "evidence_skipped": ev_res.get("skipped_duplicates", 0),
        "triggers_count": len(final_state.get("triggers") or []),
        "events_count": len(final_state.get("events") or []),
        "engine_runs": engine_runs,
    }
