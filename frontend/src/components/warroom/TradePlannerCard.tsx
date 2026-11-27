import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Briefcase,
  Calculator,
  Calendar,
  CheckCircle2,
  Crosshair,
  Eye,
  GitBranch,
  Lock,
  PlusCircle,
  Rocket,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Wallet,
  X,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type {
  ContractSpec,
  Direction,
  MarketDualResponse,
  TradePlanBody,
  TradePlanResponse,
} from "../../api/types";
import {
  fmtInputPrice,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtRatio,
  inferAssetCurrency,
  optNum,
} from "../../lib/format";
import { isSnapshotIntradayUntrusted, selectTrustedLastQuote } from "../../lib/snapshotPrice";
import type { PlanOverlayLines } from "../chart/ChanlunChart";
import {
  isGenericSectorTheme,
  type OpenPositionReceipt,
  type OpenPositionSeed,
} from "../portfolio/PortfolioDialogs";
import { CrossCurrencyFxBanner } from "../ui/CrossCurrencyFxBanner";
import { StatusBadge, type Tone } from "../ui/StatusBadge";

export type PickPriceField = "entry" | "stop30m" | "target";

export interface PickedPriceUpdate {
  seq: number;
  field: PickPriceField;
  price: number;
}

export interface ArchiveJumpOptions {
  tab?: "thesis" | "evidence" | "hard_data" | "triggers";
  openEvidenceForm?: boolean;
  openMetaEditor?: boolean;
}

export interface ArchivePlanContext {
  forSymbol?: string;
  exists: boolean;
  market?: string | null;
  directionBias?: string | null;
  sectorTheme?: string | null;
  hasL1L2Evidence?: boolean;
  evidenceCount?: number;
}

type SizingDriver = "risk" | "amount" | "qty";

function quantizeQtyDown(rawQty: number, minOrderQty: number, lotStep: number, fractional: boolean): number {
  if (!Number.isFinite(rawQty) || rawQty + 1e-12 < minOrderQty) return 0;
  const k = Math.floor((rawQty - minOrderQty) / lotStep + 1e-12);
  if (k < 0) return 0;
  const q = minOrderQty + k * lotStep;
  return fractional ? Number(q.toFixed(6)) : Math.round(q);
}

function formatQtyDisplay(qty: number, fractional: boolean): string {
  if (!Number.isFinite(qty)) return "";
  if (!fractional) return String(Math.round(qty));
  return Number(qty.toFixed(4)).toString();
}

const _f64Buf = new ArrayBuffer(8);
const _f64View = new DataView(_f64Buf);

function splitIeee754(x: number): { neg: boolean; num: bigint; den: bigint } | null {
  if (!Number.isFinite(x)) return null;
  if (x === 0) return { neg: false, num: 0n, den: 1n };
  _f64View.setFloat64(0, x, false);
  const bits = _f64View.getBigUint64(0, false);
  const neg = (bits >> 63n) !== 0n;
  const expBits = Number((bits >> 52n) & 0x7ffn);
  const fracBits = bits & 0xfffffffffffffn;
  const mantissa = expBits === 0 ? fracBits : (1n << 52n) | fracBits;
  const exp = expBits === 0 ? -1074 : expBits - 1075;
  if (exp >= 0) {
    return { neg, num: mantissa << BigInt(exp), den: 1n };
  }
  return { neg, num: mantissa, den: 1n << BigInt(-exp) };
}

/** Exact bit-for-bit match to Python 3 `round(x, 6)` returned in 1e-6 micro-units. */
function roundPython6Micros(n: number): bigint | null {
  const parts = splitIeee754(n);
  if (!parts) return null;
  if (parts.num === 0n) return 0n;
  const scaledNum = parts.num * 1_000_000n;
  let q = scaledNum / parts.den;
  const rem = scaledNum % parts.den;
  const twiceRem = rem * 2n;
  if (twiceRem > parts.den || (twiceRem === parts.den && (q & 1n) === 1n)) {
    q += 1n;
  }
  return parts.neg ? -q : q;
}

/**
 * Compute the minimal 6-decimal target price string guaranteed to satisfy R:R >= 2.5
 * after backend 6-decimal quantization, rounding outward (ceiling for long, floor for short).
 */
function computeRr25TargetStr(
  entry: number,
  stop: number,
  direction: Direction,
): string | null {
  const eM = roundPython6Micros(entry);
  const sM = roundPython6Micros(stop);
  if (eM === null || sM === null || eM <= 0n || sM <= 0n) return null;
  const distM = direction === "long" ? eM - sM : sM - eM;
  if (distM <= 0n) return null;
  // Exact ceil(distM * 2.5) = ceil(distM * 5 / 2) = (distM * 5n + 1n) / 2n
  const reqRewardM = (distM * 5n + 1n) / 2n;
  const targetM = direction === "long" ? eM + reqRewardM : eM - reqRewardM;
  if (targetM <= 0n) return null;
  const SCALE = 1_000_000n;
  const intPart = targetM / SCALE;
  const fracPart = (targetM % SCALE).toString().padStart(6, "0").replace(/0+$/, "");
  return fracPart ? `${intPart}.${fracPart}` : `${intPart}`;
}

function fmtRrDisplay(
  rawVal: number | null | undefined,
  displayVal: number | null | undefined,
  threshold = 2.5,
): string {
  const effective = rawVal ?? displayVal;
  if (effective == null || !Number.isFinite(effective)) return "—";
  const std = fmtRatio(effective);
  const stdNum = Number(std.replace(/R$/, "").replace(/,/g, ""));
  if (effective < threshold && Number.isFinite(stdNum) && stdNum >= threshold) {
    for (const dp of [3, 4, 5, 6, 8]) {
      const s = effective.toFixed(dp);
      if (Number(s) < threshold) {
        return `${s.replace(/0+$/, "").replace(/\.$/, "")}R`;
      }
    }
    return `<${threshold.toFixed(2)}R`;
  }
  return std;
}

function assetUnitLabel(
  assetClass?: string | null,
  contractSpec?: ContractSpec | Record<string, unknown> | null,
): string {
  const specAc =
    contractSpec && typeof contractSpec.asset_class === "string"
      ? contractSpec.asset_class
      : "";
  const ac = (assetClass || specAc || "").trim().toLowerCase();
  if (ac === "cn_future" || ac === "commodity" || ac === "intl_commodity" || ac === "us_future" || ac === "commodity_us") {
    return "手";
  }
  if (ac === "fx") return "单位";
  if (ac === "crypto") return "枚";
  return "股";
}

export function formatAssetClassZh(assetClass?: string | null): string {
  const ac = (assetClass || "").trim().toLowerCase();
  const map: Record<string, string> = {
    us_stock_or_etf: "美股 / ETF",
    us_equity: "美股 / ETF",
    a_share: "A 股",
    cn_equity: "A 股",
    hk_stock: "港股",
    hk_equity: "港股",
    cn_future: "国内期货",
    commodity: "海外商品",
    intl_commodity: "海外商品",
    commodity_us: "海外商品",
    us_future: "海外期货",
    fx: "外汇",
    crypto: "加密货币",
    macro_index: "宏观指数",
    macro: "宏观指数",
  };
  return map[ac] ?? (ac ? ac : "股票 / 现货");
}

function formatCurveStructureZh(raw?: string | null): string {
  const s = (raw || "").trim().toLowerCase();
  if (!s) return "";
  if (s.includes("backwardation")) return "大 B 结构（现货升水）";
  if (s.includes("contango")) return "Contango（远月升水）";
  if (s.includes("flat")) return "平坦期限结构";
  if (s === "unknown") return "曲线待核验";
  return raw || "";
}

function formatExpressionRecZh(
  raw?: string | null,
  direction?: string | null,
  assetClass?: string | null,
  opts?: { pairTrade?: boolean; hedgeOnly?: boolean },
): string {
  const key = (raw || "").trim().toLowerCase();
  const dirZh = direction === "short" ? "做空" : "做多";
  const ac = (assetClass || "").trim().toLowerCase();
  const isEq = ac === "us_stock_or_etf" || ac === "a_share" || ac === "us_equity" || ac === "cn_equity";

  if (isEq) {
    if (opts?.pairTrade) return "同产业链多空配对";
    if (opts?.hedgeOnly) return "阶段性组合对冲";
  }

  const map: Record<string, string> = {
    cash_equity_staggered_long: "正股分批建仓（做多）",
    equity_hedge_or_pair_expression_only: "同产业链配对 / 阶段性对冲",
    prefer_far_month_short_or_bear_spread: "优先空远月合约或熊市价差",
    short_active_or_far_month_with_carry_budget: "主力或次远月做空（预留展期预算）",
    prefer_far_month_long_or_bull_spread: "优先多远月合约或跨期正套",
    long_active_or_far_month_with_carry_budget: "主力或次远月做多（计入展期成本）",
    long_active_contract_positive_carry: "主力合约做多（正展期顺风）",
    short_active_contract_positive_carry: "主力合约做空（正展期顺风）",
    long_active_contract_neutral_carry: "主力合约做多（展期中性）",
    short_active_contract_neutral_carry: "主力合约做空（展期中性）",
    long_active_contract_verify_curve: "主力合约做多（下单前核对月差）",
    short_active_contract_verify_curve: "主力合约做空（下单前核对月差）",
    long_fx_verify_carry_and_cross: "外汇做多（核对隔夜利差与交叉盘）",
    short_fx_verify_carry_and_cross: "外汇做空（核对隔夜利差与交叉盘）",
    long_direct_or_cross_positive_carry: "直盘 / 交叉盘做多（正利差顺风）",
    short_direct_or_cross_positive_carry: "直盘 / 交叉盘做空（正利差顺风）",
    long_prefer_cross_or_catalyst_window: "优先交叉盘做多或聚焦催化窗口",
    short_prefer_cross_or_catalyst_window: "优先交叉盘做空或聚焦催化窗口",
    long_direct_pair_manageable_carry: "直盘做多（负利差可控）",
    short_direct_pair_manageable_carry: "直盘做空（负利差可控）",
    long_crypto_verify_funding: "加密做多（核对永续资金费率）",
    short_crypto_verify_funding: "加密做空（核对永续资金费率）",
    long_spot_avoid_crowded_perp: "现货分批做多（规避高费率永续）",
    short_cautious_crowded_negative_funding: "低杠杆谨慎做空（警惕负费率逼空）",
    long_perp_or_spot_positive_carry: "现货 / 永续做多（资金费率顺风）",
    short_perp_or_spot_positive_carry: "永续合约做空（资金费率顺风）",
    long_spot_or_low_leverage_perp: "优先现货做多（减少费率磨损）",
    short_spot_or_low_leverage_perp: "低杠杆永续做空（控制费率磨损）",
    data_stale_refresh_required: "曲线数据陈旧，需刷新后再定合约",
  };
  if (key && map[key]) return map[key];
  if (isEq) {
    return direction === "short" ? "阶段性对冲 / 同产业链配对" : "正股分批建仓（做多）";
  }
  return raw ? sanitizePlanMessageZh(raw) : `${dirZh}主力流动合约`;
}

function sanitizePlanMessageZh(raw: string | null | undefined): string {
  if (!raw) return "";
  let s = raw.trim();
  const exactCodes: Record<string, string> = {
    intraday_bar_unclosed: "最新 30m K 线尚未收盘完结，暂停自动放行",
    stale_intraday_30m_snapshot: "30m 日内行情快照已过期，请刷新后再验算",
    stale_market_snapshot: "行情快照数据已过期，请刷新后再验算",
    daily_data_quality_degraded: "日线行情质量降级（存在高比例平K或异常坏点）",
    intraday_data_quality_degraded: "30m 日内行情质量降级（存在异常平K或坏点）",
    missing_daily_timestamp: "缺少日线时间戳，无法核验行情时效",
    missing_intraday_timestamp: "缺少 30m 日内时间戳，无法核验行情时效",
    daily_future_timestamp: "日线时间戳异常（晚于当前时间）",
    intraday_future_timestamp: "30m 日内时间戳异常（晚于当前时间）",
    curve_stale_or_error: "期货期限结构曲线数据异常或陈旧",
    missing_term_structure_data: "暂缺完整期货期限结构数据，请在下单前核对近远月升贴水",
    missing_fx_rate_differential: "暂缺国债/政策利差数据，请在下单前核对隔夜掉期利差",
    missing_crypto_funding: "暂缺永续资金费率数据，请核对资金费率与持仓量拥挤度",
  };
  if (exactCodes[s]) return exactCodes[s];

  if (s.includes("Live mode strictly enforces live 30min Chanlun stops")) {
    return "实时 30m 止损锁定模式下暂无当前方向的同侧 30m 缠论止损位；可等待 30m 结构就绪，或关闭「实时止损」开关进行手动推演。";
  }

  s = s
    .replace(/\bdual_alignment\.daily_bias\s*=\s*short\b/gi, "日线主方向为空头")
    .replace(/\bdual_alignment\.daily_bias\s*=\s*long\b/gi, "日线主方向为多头")
    .replace(/\bdual_alignment\.daily_bias\s*=\s*neutral\b/gi, "日线主方向为中性震荡")
    .replace(/\bdaily_bias\s*=\s*short\b/gi, "日线方向：空头")
    .replace(/\bdaily_bias\s*=\s*long\b/gi, "日线方向：多头")
    .replace(/\bdaily_bias\s*=\s*neutral\b/gi, "日线方向：中性")
    .replace(/\bdaily_bias\s*=\s*MISSING\b/gi, "日线方向缺失")
    .replace(/\bintraday_action\s*=\s*entry_long\b/gi, "30m 触发：做多入场")
    .replace(/\bintraday_action\s*=\s*entry_short\b/gi, "30m 触发：做空入场")
    .replace(/\bintraday_action\s*=\s*wait\b/gi, "30m 尚未触发")
    .replace(/\bintraday_action\s*=\s*MISSING\b/gi, "30m 触发状态缺失")
    .replace(/计划方向\s*=\s*long\b/gi, "计划方向：做多")
    .replace(/计划方向\s*=\s*short\b/gi, "计划方向：做空")
    .replace(/拟执行方向[：:]\s*long\b/gi, "拟执行方向：做多")
    .replace(/拟执行方向[：:]\s*short\b/gi, "拟执行方向：做空")
    .replace(/档案研究方向[：:]\s*long\b/gi, "档案研究方向：做多")
    .replace(/档案研究方向[：:]\s*short\b/gi, "档案研究方向：做空")
    .replace(/档案研究方向[：:]\s*neutral\b/gi, "档案研究方向：中性（待定性）")
    .replace(/档案研究方向[：:]\s*pair\b/gi, "档案研究方向：同板块配对")
    .replace(/[（(]\s*neutral\s*[)）]/gi, "（待定性）")
    .replace(/[（(]\s*pair\s*[)）]/gi, "")
    .replace(/[（(]\s*pair_leg_verified\s*=\s*True\s*[)）]/gi, "")
    .replace(/[（(]\s*daily_alignment\s*缺失\s*[)）]/gi, "（缺少大小级别对齐数据）")
    .replace(/[（(]\s*rr_ratio\s*=\s*None\s*[)）]/gi, "（尚未设定目标价）")
    .replace(/[（(]\s*total_quantity\s*=\s*None\s*[)）]/gi, "（尚未完成仓位测算）")
    .replace(/仅\s*'aligned_trigger'\s*可放行/g, "仅大小级别同向共振可放行")
    .replace(/\bactionable_long\b/g, "放行做多")
    .replace(/\bactionable_short\b/g, "放行做空")
    .replace(/\bcash_equity_staggered_long\b/g, "正股分批建仓（做多）")
    .replace(/\bequity_hedge_or_pair_expression_only\b/g, "阶段性对冲 / 同产业链配对")
    .replace(/\bus_stock_or_etf\b/g, "美股 / ETF")
    .replace(/\ba_share\b/g, "A 股")
    .replace(/\bcn_future\b/g, "国内期货")
    .replace(/\bintl_commodity\b/g, "海外商品")
    .replace(/\baligned_trigger\b/g, "大小级别同向共振")
    .replace(/\bdaily_opposes_direction\b/g, "日线方向相反")
    .replace(/\bdaily_ok_wait_30m\b/g, "日线顺向，等待 30m 触发")
    .replace(/\bneutral_wait\b/g, "中枢震荡观望")
    .replace(/\bintraday_bar_unclosed\b/g, "最新 30m K 线尚未收盘")
    .replace(/\bstale_intraday_30m_snapshot\b/g, "30m 日内快照已过期")
    .replace(/\bstale_market_snapshot\b/g, "行情快照已过期")
    .replace(/\bcurve_stale_or_error\b/g, "期限结构数据异常或陈旧")
    .replace(/\bterm_structure_(stale|error)\b/gi, "期限结构数据异常")
    .replace(/\b(rate_differential|fx_rate_differential)_(stale|error)\b/gi, "外汇利差数据异常")
    .replace(/\bsources_status_[a-z0-9_]+_(stale|error)\b/gi, "上游数据源状态异常")
    .replace(/\bcurve_data_warning:\s*/gi, "")
    .replace(/\bsteep_front_backwardation_squeeze_risk:\s*/gi, "近月大 B 挤仓警戒：")
    .replace(/\bsteep_front_contango_roll_drag:\s*/gi, "近月 Contango 展期损耗警戒：")
    .replace(/\bdual_alignment_error:\s*/gi, "大小级别对齐计算异常：")
    .replace(/\bresearch_error:\s*/gi, "研报硬数据抓取异常：");

  return s;
}

function formatEarningsDateVal(raw: unknown): string | null {
  if (raw == null) return null;
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const parsed = formatEarningsDateVal(item);
      if (parsed) return parsed;
    }
    return null;
  }
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 1e8) {
    const ms = raw > 1e12 ? raw : raw * 1000;
    const d = new Date(ms);
    if (!Number.isNaN(d.getTime())) {
      return d.toISOString().slice(0, 10);
    }
  }
  if (typeof raw === "string") {
    const cleaned = raw.trim();
    if (!cleaned) return null;
    const iso = /^(\d{4}-\d{2}-\d{2})/.exec(cleaned);
    if (iso) return iso[1] ?? cleaned;
    return cleaned.slice(0, 16);
  }
  return null;
}

function normalizePctMetric(raw: unknown): number | null {
  const n = typeof raw === "number" ? raw : typeof raw === "string" && raw.trim() !== "" ? Number(raw) : NaN;
  if (!Number.isFinite(n) || n < 0) return null;
  /* yfinance sometimes returns dividendYield/shortPercentOfFloat as ratio (0.032) or percent (3.2) */
  return n <= 0.5 ? n * 100 : n;
}

function extractEquityFundamentals(fundamentals: Record<string, unknown> | null | undefined) {
  const f = fundamentals && typeof fundamentals === "object" ? fundamentals : {};
  const nextEarningsDate =
    formatEarningsDateVal(f.next_earnings_date) ??
    formatEarningsDateVal(f.earnings_date) ??
    formatEarningsDateVal(f.earningsDate) ??
    formatEarningsDateVal(f.earningsTimestampStart) ??
    formatEarningsDateVal(f.earningsTimestamp) ??
    null;

  const numField = (keys: string[]): number | null => {
    for (const k of keys) {
      const v = f[k];
      const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
      if (Number.isFinite(n) && n > 0) return n;
    }
    return null;
  };

  const forwardPe = numField(["forward_pe", "forwardPE"]);
  const trailingPe = numField(["trailing_pe", "trailingPE"]);
  const dividendYieldPct = normalizePctMetric(f.dividend_yield ?? f.dividendYield);
  const shortPctOfFloat = normalizePctMetric(f.short_percent_of_float ?? f.shortPercentOfFloat);
  const sector = typeof f.sector === "string" && f.sector.trim() ? f.sector.trim() : null;

  return {
    nextEarningsDate,
    forwardPe,
    trailingPe,
    dividendYieldPct,
    shortPctOfFloat,
    sector,
  };
}

function formatResearchDirectionZh(dir?: string | null): string {
  const d = (dir || "").trim().toLowerCase();
  if (d === "long") return "做多";
  if (d === "short") return "做空";
  if (d === "neutral") return "中性（待定性）";
  if (d === "pair") return "同板块配对";
  return "未定性";
}

function PlannerGateBadge({ decision, actionable }: { decision: string; actionable?: boolean }) {
  const key = (decision || "").trim().toLowerCase();
  if (key === "actionable_long" || key.includes("approve_long") || decision.includes("放行做多")) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--bull)_42%,transparent)] bg-[color:color-mix(in_srgb,var(--bull)_14%,transparent)] px-2 py-0.5 text-xs font-semibold text-bull whitespace-nowrap">
        <ArrowUpRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>放行做多</span>
      </span>
    );
  }
  if (key === "actionable_short" || key.includes("approve_short") || decision.includes("放行做空")) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_14%,transparent)] px-2 py-0.5 text-xs font-semibold text-accent whitespace-nowrap">
        <ArrowDownRight className="h-3.5 w-3.5 shrink-0 text-bear" aria-hidden="true" />
        <span className="text-ink">放行做空</span>
      </span>
    );
  }

  const DECISION_MAP: Record<string, { tone: Tone; label: string }> = {
    pair_trade: { tone: "info", label: "放行同板块配对" },
    rejected_direction_conflict: { tone: "critical", label: "方向冲突否决" },
    watchlist_research_pending: { tone: "warning", label: "研究方向待定性" },
    rejected_sizing_or_capital: { tone: "critical", label: "仓位/资金超限否决" },
    rejected: { tone: "critical", label: "否决建仓" },
    watchlist_wait_30m: { tone: "warning", label: "等待 30min 结构" },
    watchlist_poor_rr: { tone: "warning", label: "盈亏比不足 (<2.5R)" },
    watchlist_poor_carry_adjusted_rr: { tone: "warning", label: "展期后盈亏比不足" },
    watchlist_missing_rr: { tone: "warning", label: "待设定目标价" },
    watchlist_missing_sizing: { tone: "warning", label: "待完成算仓" },
    watchlist_pair_incomplete: { tone: "warning", label: "配对双腿待核验" },
    watchlist_upstream_error: { tone: "warning", label: "上游数据异常暂停" },
    watchlist_data_quality_degraded: { tone: "warning", label: "行情质量降级暂停" },
    watchlist_unverified: { tone: "warning", label: "对齐状态待核验" },
    watchlist: { tone: "warning", label: "逻辑待观察" },
  };

  const mapped = DECISION_MAP[key];
  if (mapped) {
    return <StatusBadge tone={mapped.tone} label={mapped.label} className="text-xs font-semibold" />;
  }

  let tone: Tone = "warning";
  let label = "逻辑待观察";
  if (actionable) {
    tone = "info";
    label = "放行执行";
  } else if (key.includes("reject") || key.includes("veto") || decision.includes("否决")) {
    tone = "critical";
    label = "否决建仓";
  } else if (key.includes("pair") || decision.includes("配对")) {
    tone = "info";
    label = "同板块配对";
  }
  return <StatusBadge tone={tone} label={label} className="text-xs font-semibold" />;
}

interface TradePlannerCardProps {
  symbol: string;
  dualData: MarketDualResponse | null;
  snapshotLoading?: boolean;
  snapshotError?: string | null;
  intradayInterval: "15m" | "30m" | "60m";
  portfolioInitialized?: boolean;
  accountEquity?: number | null;
  accountCurrency?: string | null;
  archiveContext?: ArchivePlanContext | null;
  openedReceipt?: OpenPositionReceipt | null;
  onDismissOpenedReceipt?: () => void;
  activePickField?: PickPriceField | null;
  pickedPrice?: PickedPriceUpdate | null;
  onRequestPickField?: (field: PickPriceField | null) => void;
  onOverlayChange?: (overlay: PlanOverlayLines | null) => void;
  onOpenPositionSeed: (seed: OpenPositionSeed) => void;
  onLedgerInitialized?: () => void;
  onJumpArchive?: (symbol: string, market?: string | null, opts?: ArchiveJumpOptions) => void;
  onJumpSentinel?: () => void;
}

export function TradePlannerCard({
  symbol,
  dualData,
  snapshotLoading = false,
  snapshotError = null,
  intradayInterval,
  portfolioInitialized,
  accountEquity = null,
  accountCurrency,
  archiveContext,
  openedReceipt = null,
  onDismissOpenedReceipt,
  activePickField = null,
  pickedPrice = null,
  onRequestPickField,
  onOverlayChange,
  onOpenPositionSeed,
  onLedgerInitialized,
  onJumpArchive,
  onJumpSentinel,
}: TradePlannerCardProps) {
  const headingId = useId();
  const snapshotStale = isSnapshotIntradayUntrusted(dualData, snapshotError);
  const align = snapshotStale ? undefined : dualData?.chanlun?.dual_alignment;
  const dailyZs = dualData?.chanlun?.daily?.latest_zhongshu;
  const trustedQuote = selectTrustedLastQuote(dualData, snapshotError);
  const lastClose = trustedQuote.price;

  const snapInterval = dualData?.intraday_interval || align?.intraday_timeframe || intradayInterval;
  const is30mSnapshot = snapInterval === "30m" || snapInterval === "30min";

  const [direction, setDirection] = useState<Direction>("long");
  const [live, setLive] = useState(true);
  const [entryPrice, setEntryPrice] = useState("");
  const [stop30m, setStop30m] = useState("");
  const [stopDaily, setStopDaily] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [sizingDriver, setSizingDriver] = useState<SizingDriver>("risk");
  const [riskPct, setRiskPct] = useState("1.0");
  const [allocAmount, setAllocAmount] = useState("");
  const [manualQty, setManualQty] = useState("");
  const [slotShare, setSlotShare] = useState("1.0");
  const [horizonDays, setHorizonDays] = useState("90");
  const [fxToAccount, setFxToAccount] = useState("");
  const [showOverlay, setShowOverlay] = useState(false);

  /* Uninitialized ledger & explicit simulation equity controls */
  const [ledgerMissingDetected, setLedgerMissingDetected] = useState(false);
  const ledgerUninitialized = portfolioInitialized === false || ledgerMissingDetected;
  const [useSimEquity, setUseSimEquity] = useState(portfolioInitialized === false);
  const [simEquity, setSimEquity] = useState("1000000");
  const [simCurrency, setSimCurrency] = useState("USD");
  const [initBusy, setInitBusy] = useState(false);
  const [ledgerInitNotice, setLedgerInitNotice] = useState<string | null>(null);

  /* Gate inputs: strictly derived from verified research archive (never manually bypassed in UI) */
  const archiveVerifiedL1L2 = Boolean(archiveContext?.exists && archiveContext?.hasL1L2Evidence);
  const [hasL1L2, setHasL1L2] = useState(false);
  const [pendingVerif, setPendingVerif] = useState("0");
  const [thesisFalsified, setThesisFalsified] = useState(false);
  const [pairTrade, setPairTrade] = useState(false);
  const [pairOppositeSym, setPairOppositeSym] = useState("");
  const [pairLegVerified, setPairLegVerified] = useState(false);
  const [hedgeOnly, setHedgeOnly] = useState(false);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<TradePlanResponse | null>(null);
  const [planStale, setPlanStale] = useState(false);
  const [capOverride, setCapOverride] = useState<{
    maxMarginPct?: number;
    maxLeverage?: number;
  } | null>(null);

  const planReqIdRef = useRef(0);
  const planInputRevRef = useRef(0);
  const planAbortRef = useRef<AbortController | null>(null);
  const lastAppliedPickSeqRef = useRef(0);
  const prevArchiveSigRef = useRef<{ symbol: string; sig: string } | null>(null);

  useEffect(() => {
    return () => {
      planAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (portfolioInitialized === false) {
      setUseSimEquity(true);
    } else if (portfolioInitialized === true) {
      setLedgerMissingDetected(false);
    }
  }, [portfolioInitialized]);

  const invalidateInFlightAndPlan = () => {
    planInputRevRef.current += 1;
    if (planAbortRef.current) {
      planAbortRef.current.abort();
      planAbortRef.current = null;
    }
    setBusy(false);
  };

  const markStale = () => {
    invalidateInFlightAndPlan();
    onDismissOpenedReceipt?.();
    setCapOverride(null);
    setPlanStale(true);
  };

  /* Apply price picked interactively from chart click */
  useEffect(() => {
    if (!pickedPrice || pickedPrice.seq === lastAppliedPickSeqRef.current) return;
    lastAppliedPickSeqRef.current = pickedPrice.seq;
    const formatted = fmtInputPrice(pickedPrice.price);
    if (pickedPrice.field === "entry") {
      setEntryPrice(formatted);
      markStale();
    } else if (pickedPrice.field === "stop30m" && !live) {
      setStop30m(formatted);
      markStale();
    } else if (pickedPrice.field === "target") {
      setTargetPrice(formatted);
      markStale();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickedPrice, live]);

  /* Sync optional R:R ruler lines onto chart when enabled (strictly valid-side 30m stop in Live mode) */
  useEffect(() => {
    if (!onOverlayChange) return;
    if (!showOverlay) {
      onOverlayChange(null);
      return;
    }
    const validPlanPos = !planStale ? plan?.position_plan : null;
    const ep = optNum(entryPrice) ?? validPlanPos?.entry_price ?? null;
    let s30: number | null = null;
    if (live) {
      if (validPlanPos?.tactical_stop_30m != null) {
        s30 = validPlanPos.tactical_stop_30m;
      } else if (is30mSnapshot && align?.intraday_tactical_stop != null) {
        const cand = align.intraday_tactical_stop;
        const sideValid =
          ep == null || (direction === "long" ? cand < ep : cand > ep);
        s30 = sideValid ? cand : null;
      }
    } else {
      s30 = optNum(stop30m) ?? validPlanPos?.tactical_stop_30m ?? null;
    }
    const tp = optNum(targetPrice) ?? validPlanPos?.target_price ?? null;
    onOverlayChange({ entry: ep, stop30m: s30, target: tp });
  }, [
    showOverlay,
    entryPrice,
    stop30m,
    targetPrice,
    direction,
    live,
    is30mSnapshot,
    align?.intraday_tactical_stop,
    planStale,
    plan?.position_plan,
    onOverlayChange,
  ]);

  /* Reset symbol-scoped gate defaults (Fail-Closed hasL1L2 = false) whenever target symbol changes */
  useEffect(() => {
    prevArchiveSigRef.current = null;
    setHasL1L2(false);
    setPendingVerif("0");
    setThesisFalsified(false);
    setHedgeOnly(false);
    setSizingDriver("risk");
    setAllocAmount("");
    setManualQty("");
    setCapOverride(null);
  }, [symbol]);

  const pickValidDailyStop = (dir: Direction, refEntry: number | null | undefined): number | null => {
    const candidates = [
      align?.daily_structural_stop,
      dir === "long" ? dailyZs?.zd : dailyZs?.zg,
    ];
    for (const c of candidates) {
      if (typeof c !== "number" || !Number.isFinite(c) || c <= 0) continue;
      if (refEntry == null || !Number.isFinite(refEntry)) return c;
      if (dir === "long" && c < refEntry) return c;
      if (dir === "short" && c > refEntry) return c;
    }
    return null;
  };

  /* Auto-seed direction & stops when symbol or snapshot changes */
  useEffect(() => {
    invalidateInFlightAndPlan();
    setPlan(null);
    setPlanStale(false);
    setErr(null);
    setFxToAccount("");
    setPairTrade(false);
    setPairOppositeSym("");
    setPairLegVerified(false);
    onRequestPickField?.(null);

    const bias = (
      align?.daily_bias ||
      dualData?.chanlun?.signal_summary?.signal_meta?.direction ||
      ""
    ).toLowerCase();
    const nextDir: Direction =
      bias.includes("short") || bias.includes("bear") || bias.includes("down") || bias.includes("空")
        ? "short"
        : "long";
    setDirection(nextDir);

    if (snapshotStale) {
      // Never auto-prefill stale/untrusted intraday prices, stops, or derived targets
      setEntryPrice("");
      setStop30m("");
      setStopDaily("");
      setTargetPrice("");
      return;
    }

    const ep = lastClose;
    setEntryPrice(fmtInputPrice(ep));

    /* STRICT 30m discipline: NEVER prefill 15m/60m or daily stops into tactical_stop_30m */
    const s30 = is30mSnapshot ? (align?.intraday_tactical_stop ?? null) : null;
    setStop30m(fmtInputPrice(s30));

    const sDaily = pickValidDailyStop(nextDir, ep);
    setStopDaily(fmtInputPrice(sDaily));

    if (ep != null && s30 != null && ep !== s30) {
      const dist = Math.abs(ep - s30);
      const tgt = nextDir === "long" ? ep + dist * 3 : Math.max(0.0001, ep - dist * 3);
      setTargetPrice(fmtInputPrice(tgt));
    } else {
      setTargetPrice("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    symbol,
    snapshotStale,
    is30mSnapshot,
    align?.daily_bias,
    align?.intraday_tactical_stop,
    align?.daily_structural_stop,
    dualData?.chanlun?.signal_summary?.signal_meta?.direction,
    dailyZs?.zd,
    dailyZs?.zg,
    lastClose,
  ]);

  /* Hydrate L1/L2 & archive gate signature strictly from verified archive (ignore transient null during background refresh) */
  const archiveGateSignature = useMemo(() => {
    if (!archiveContext) return null;
    const existsFlag = archiveContext.exists ? "1" : "0";
    const mkt = (archiveContext.market || "").trim().toUpperCase();
    const bias = (archiveContext.directionBias || "").trim().toLowerCase();
    const l12Flag = archiveVerifiedL1L2 ? "1" : "0";
    return `${existsFlag}|${mkt}|${bias}|${l12Flag}`;
  }, [archiveContext, archiveVerifiedL1L2]);

  useEffect(() => {
    if (!archiveContext || archiveGateSignature === null) return;
    if (
      archiveContext.forSymbol &&
      archiveContext.forSymbol.trim().toUpperCase() !== symbol.trim().toUpperCase()
    ) {
      return;
    }
    const prev = prevArchiveSigRef.current;
    setHasL1L2(archiveVerifiedL1L2);
    if (prev && prev.symbol === symbol && prev.sig !== archiveGateSignature) {
      invalidateInFlightAndPlan();
      setPlanStale(true);
    }
    prevArchiveSigRef.current = { symbol, sig: archiveGateSignature };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archiveContext, archiveGateSignature, archiveVerifiedL1L2, symbol]);

  const effectiveAcctCurrency = (useSimEquity ? simCurrency : accountCurrency || "USD").toUpperCase();
  const staticQuoteCurrency = inferAssetCurrency(symbol, dualData?.asset_class);
  const inferredQuoteCurrency =
    plan?.position_plan?.quote_currency?.toUpperCase() ??
    dualData?.contract_spec?.quote_currency?.toUpperCase() ??
    staticQuoteCurrency;
  const isKnownSameCurrency = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency === effectiveAcctCurrency,
  );
  const needsCrossCurrencyFx = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency !== effectiveAcctCurrency,
  );

  useEffect(() => {
    invalidateInFlightAndPlan();
    setFxToAccount("");
    setCapOverride(null);
    setPlan(null);
    setPlanStale(false);
  }, [effectiveAcctCurrency, staticQuoteCurrency]);

  const canonicalSymKey = (raw: string): string => {
    const cleaned = raw.trim().toUpperCase().replace(/^(US|CN|FX|HK|SH|SZ|COMMODITY|CRYPTO):/, "");
    return cleaned.trim();
  };

  const cleanPairOppositeSym = pairOppositeSym.trim();
  const pairSameAsPrimary = Boolean(
    cleanPairOppositeSym &&
      canonicalSymKey(cleanPairOppositeSym) === canonicalSymKey(symbol),
  );

  /* Live contract specification & bidirectional sizing preview */
  const [initializedEquityHint, setInitializedEquityHint] = useState<number | null>(null);
  const resolvedEquity = useMemo(() => {
    if (useSimEquity) return optNum(simEquity) ?? null;
    return accountEquity ?? initializedEquityHint ?? null;
  }, [useSimEquity, simEquity, accountEquity, initializedEquityHint]);

  const resolvedAssetClass = (
    plan?.position_plan?.asset_class ||
    dualData?.contract_spec?.asset_class ||
    dualData?.asset_class ||
    "us_stock_or_etf"
  ).toLowerCase();
  const isEquityAsset = resolvedAssetClass === "us_stock_or_etf" || resolvedAssetClass === "a_share";
  const unitLabel = assetUnitLabel(
    resolvedAssetClass,
    (!planStale && plan?.position_plan?.contract_spec) || dualData?.contract_spec,
  );

  const activeSpec = useMemo(() => {
    const rawSpec = (
      (!planStale && plan?.position_plan?.contract_spec
        ? (plan.position_plan.contract_spec as ContractSpec)
        : null) ??
      dualData?.contract_spec ??
      null
    );
    const defaultCash = isEquityAsset || resolvedAssetClass === "crypto";
    const multiplier =
      rawSpec && typeof rawSpec.multiplier === "number" && rawSpec.multiplier > 0
        ? rawSpec.multiplier
        : defaultCash
          ? 1
          : null;
    const marginRate =
      rawSpec && typeof rawSpec.margin_rate === "number" && rawSpec.margin_rate > 0
        ? rawSpec.margin_rate
        : defaultCash
          ? 1
          : null;
    const minOrderQty =
      rawSpec && typeof rawSpec.min_order_qty === "number" && rawSpec.min_order_qty > 0
        ? rawSpec.min_order_qty
        : resolvedAssetClass === "a_share"
          ? 100
          : resolvedAssetClass === "crypto"
            ? 0.001
            : 1;
    const lotStep =
      rawSpec && typeof rawSpec.lot_step === "number" && rawSpec.lot_step > 0
        ? rawSpec.lot_step
        : minOrderQty;
    const fractional =
      rawSpec && typeof rawSpec.fractional === "boolean"
        ? rawSpec.fractional
        : resolvedAssetClass === "crypto";
    const knownSpec = rawSpec ? rawSpec.known_spec !== false : defaultCash;
    return { multiplier, marginRate, minOrderQty, lotStep, fractional, knownSpec };
  }, [planStale, plan?.position_plan?.contract_spec, dualData?.contract_spec, isEquityAsset, resolvedAssetClass]);

  const sizingPreview = useMemo(() => {
    const entry = optNum(entryPrice) ?? null;
    const target = optNum(targetPrice) ?? null;
    const equity = resolvedEquity != null && resolvedEquity > 0 ? resolvedEquity : null;
    const slot = optNum(slotShare) ?? 1.0;
    const fx = isKnownSameCurrency ? 1.0 : (optNum(fxToAccount) ?? null);

    let stop: number | null = null;
    if (live) {
      const liveCand =
        (!planStale ? plan?.position_plan?.tactical_stop_30m : null) ??
        (is30mSnapshot ? (align?.intraday_tactical_stop ?? null) : null);
      if (liveCand != null && (entry == null || (direction === "long" ? liveCand < entry : liveCand > entry))) {
        stop = liveCand;
      }
    } else {
      const manCand = optNum(stop30m) ?? null;
      if (manCand != null && (entry == null || (direction === "long" ? manCand < entry : manCand > entry))) {
        stop = manCand;
      }
    }

    const rawStopDist = entry != null && stop != null ? Math.abs(entry - stop) : null;
    const entryM = entry != null ? roundPython6Micros(entry) : null;
    const stopM = stop != null ? roundPython6Micros(stop) : null;
    const targetM = target != null ? roundPython6Micros(target) : null;
    const entryStopQuantizedValid =
      entryM != null &&
      stopM != null &&
      entryM > 0n &&
      stopM > 0n &&
      (direction === "long" ? entryM > stopM : entryM < stopM);
    const distM = entryStopQuantizedValid
      ? direction === "long"
        ? entryM - stopM
        : stopM - entryM
      : null;
    const targetQuantizedValid =
      entryM != null &&
      targetM != null &&
      entryM > 0n &&
      targetM > 0n &&
      (direction === "long" ? targetM > entryM : targetM < entryM);
    const rewardM = targetQuantizedValid
      ? direction === "long"
        ? targetM - entryM
        : entryM - targetM
      : null;
    const stopPrecisionCollapsed =
      rawStopDist != null && rawStopDist > 0 && entryM != null && stopM != null && !entryStopQuantizedValid;
    const stopDist = distM != null && distM > 0n ? Number(distM) / 1_000_000 : null;
    const stopDistPct =
      entryStopQuantizedValid && distM != null && distM > 0n
        ? (Number(distM) / Number(entryM)) * 100
        : null;
    const rrRatio =
      distM != null && distM > 0n && rewardM != null && rewardM > 0n
        ? Number(rewardM) / Number(distM)
        : null;
    const rrBelow25 =
      distM != null && distM > 0n && rewardM != null && rewardM > 0n
        ? rewardM * 2n < distM * 5n
        : false;
    const rr25TargetStr =
      entryStopQuantizedValid && entry != null && stop != null
        ? computeRr25TargetStr(entry, stop, direction)
        : null;
    const rr25Target = rr25TargetStr != null ? Number(rr25TargetStr) : null;

    const unitNotional =
      entry != null &&
      (entryM == null || entryM > 0n) &&
      activeSpec.multiplier != null &&
      fx != null &&
      fx > 0
        ? entry * activeSpec.multiplier * fx
        : null;
    const unitCost =
      unitNotional != null && activeSpec.marginRate != null
        ? unitNotional * activeSpec.marginRate
        : null;
    const unitRisk =
      stopDist != null && stopDist > 0 && activeSpec.multiplier != null && fx != null && fx > 0
        ? stopDist * activeSpec.multiplier * fx
        : null;

    let estQty: number | null = null;
    let estOutlay: number | null = null;
    let estNotional: number | null = null;
    let estRiskAmt: number | null = null;
    let actualRiskPct: number | null = null;
    let impliedBudgetRiskPct: number | null = null;
    let outlayPct: number | null = null;
    let cappedByCash = false;
    let exceedsEquity = false;
    let exceedsRiskCap = false;
    let belowMinLot = false;

    if (sizingDriver === "risk") {
      const rPct = optNum(riskPct);
      if (equity != null && rPct != null && rPct > 0 && unitRisk != null && unitRisk > 0 && unitCost != null && unitCost > 0) {
        const riskBudget = equity * (rPct / 100) * slot;
        const qtyByRisk = riskBudget / unitRisk;
        const maxMarginFraction =
          capOverride?.maxMarginPct != null ? capOverride.maxMarginPct / 100 : 1.0;
        const qtyByMarginCap = (equity * maxMarginFraction) / unitCost;
        const qtyByLevCap =
          capOverride?.maxLeverage != null && unitNotional != null && unitNotional > 0
            ? (equity * capOverride.maxLeverage) / unitNotional
            : Infinity;
        const qtyByCap = Math.min(qtyByMarginCap, qtyByLevCap);
        cappedByCash = qtyByCap + 1e-9 < qtyByRisk;
        const rawQty = Math.min(qtyByRisk, qtyByCap);
        estQty = quantizeQtyDown(rawQty, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        estOutlay = estQty * unitCost;
        estNotional = estQty * (unitNotional ?? unitCost);
        estRiskAmt = estQty * unitRisk;
        actualRiskPct = (estRiskAmt / equity) * 100;
        impliedBudgetRiskPct = rPct;
        outlayPct = (estOutlay / equity) * 100;
      }
    } else if (sizingDriver === "amount") {
      const amt = optNum(allocAmount);
      if (!stopPrecisionCollapsed && (entryM == null || entryM > 0n) && amt != null && amt > 0 && unitCost != null && unitCost > 0) {
        estQty = quantizeQtyDown(amt / unitCost, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        estOutlay = estQty * unitCost;
        estNotional = estQty * (unitNotional ?? unitCost);
        if (equity != null) {
          outlayPct = (estOutlay / equity) * 100;
          exceedsEquity = estOutlay > equity * 1.0001;
        }
        if (unitRisk != null && unitRisk > 0 && equity != null && estQty > 0) {
          estRiskAmt = estQty * unitRisk;
          actualRiskPct = (estRiskAmt / equity) * 100;
          impliedBudgetRiskPct = (estRiskAmt / (equity * slot)) * 100;
          exceedsRiskCap = impliedBudgetRiskPct > 10.0 + 1e-9;
        }
      }
    } else if (sizingDriver === "qty") {
      const qRaw = optNum(manualQty);
      if (!stopPrecisionCollapsed && (entryM == null || entryM > 0n) && qRaw != null && qRaw > 0) {
        estQty = quantizeQtyDown(qRaw, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        if (unitCost != null && unitCost > 0) {
          estOutlay = estQty * unitCost;
          estNotional = estQty * (unitNotional ?? unitCost);
          if (equity != null) {
            outlayPct = (estOutlay / equity) * 100;
            exceedsEquity = estOutlay > equity * 1.0001;
          }
        }
        if (unitRisk != null && unitRisk > 0 && equity != null && estQty > 0) {
          estRiskAmt = estQty * unitRisk;
          actualRiskPct = (estRiskAmt / equity) * 100;
          impliedBudgetRiskPct = (estRiskAmt / (equity * slot)) * 100;
          exceedsRiskCap = impliedBudgetRiskPct > 10.0 + 1e-9;
        }
      }
    }

    const notionalLeverage =
      estNotional != null && equity != null && equity > 0 ? estNotional / equity : null;

    return {
      entry,
      stop,
      target,
      equity,
      slot,
      fx,
      stopDist,
      stopDistPct,
      stopPrecisionCollapsed,
      rrRatio,
      rrBelow25,
      rr25Target,
      rr25TargetStr,
      unitNotional,
      unitCost,
      unitRisk,
      estQty,
      estOutlay,
      estNotional,
      notionalLeverage,
      estRiskAmt,
      actualRiskPct,
      impliedBudgetRiskPct,
      outlayPct,
      cappedByCash,
      exceedsEquity,
      exceedsRiskCap,
      belowMinLot,
    };
  }, [
    entryPrice,
    targetPrice,
    resolvedEquity,
    slotShare,
    isKnownSameCurrency,
    fxToAccount,
    live,
    planStale,
    plan?.position_plan?.tactical_stop_30m,
    is30mSnapshot,
    align?.intraday_tactical_stop,
    direction,
    stop30m,
    activeSpec,
    sizingDriver,
    riskPct,
    allocAmount,
    manualQty,
    capOverride,
  ]);

  const handleQuickInitLedger = async () => {
    const simEqNum = optNum(simEquity);
    if (simEqNum === undefined || simEqNum < 100) {
      setErr("初始化模拟账本需要填写不低于 100 的有效净值金额");
      return;
    }
    const ccy = simCurrency.trim().toUpperCase() || "USD";
    setInitBusy(true);
    setErr(null);
    setLedgerInitNotice(null);
    try {
      await api.portfolioInit({
        equity: simEqNum,
        base_currency: ccy,
        default_risk_per_trade_pct: 1.0,
        max_total_open_risk_pct: 6.0,
        max_macro_factor_risk_pct: 3.0,
      });
      setInitializedEquityHint(simEqNum);
      setLedgerMissingDetected(false);
      setUseSimEquity(false);
      setLedgerInitNotice(`已按 ${fmtMoney(simEqNum, ccy)} 创建本地模拟账本，算仓放行后可直接录入持仓。`);
      markStale();
      onLedgerInitialized?.();
    } catch (ex) {
      setErr(`初始化模拟账本失败：${errorMessage(ex)}`);
    } finally {
      setInitBusy(false);
    }
  };

  const applyQuickAllocPct = (pct: number) => {
    const eq = sizingPreview.equity;
    if (!eq || eq <= 0) return;
    setSizingDriver("amount");
    setAllocAmount(String(Math.round(eq * pct)));
    markStale();
  };

  const applyQuickRiskPct = (pct: number) => {
    setSizingDriver("risk");
    setRiskPct(String(pct));
    markStale();
  };

  const executeTradePlan = async (override?: {
    capOverride?: { maxMarginPct?: number; maxLeverage?: number } | null;
    forceRiskDriver?: boolean;
  }) => {
    onRequestPickField?.(null);
    onDismissOpenedReceipt?.();

    const effectiveCap =
      override && "capOverride" in override ? override.capOverride : capOverride;
    const effectiveDriver: SizingDriver = override?.forceRiskDriver ? "risk" : sizingDriver;

    if (snapshotStale && live) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("当前行情快照处于刷新失败或缓存降级状态，已暂停实时联立算仓自动预填；请先点击顶部「刷新」恢复实时行情后再试。");
      return;
    }

    if (ledgerUninitialized && !useSimEquity) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("本地持仓账本尚未初始化，请勾选「模拟本金测算」或直接点击「用此金额建模拟账本」。");
      return;
    }
    const fxVal = isKnownSameCurrency ? undefined : optNum(fxToAccount);
    if (!isKnownSameCurrency && fxToAccount.trim() !== "" && (fxVal === undefined || fxVal <= 0)) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("跨币种折算汇率必须为大于 0 的有效数值");
      return;
    }
    if (useSimEquity) {
      const simEqNum = optNum(simEquity);
      if (simEqNum === undefined || simEqNum <= 0) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("模拟测算本金必须为大于 0 的有效数值");
        return;
      }
    }
    if (pairTrade && pairLegVerified && (!cleanPairOppositeSym || pairSameAsPrimary)) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("配对核验必须填写与当前主标的不同且有效的反向对冲腿代码。");
      return;
    }

    let submitRiskPct = optNum(riskPct) ?? 1.0;
    if (effectiveDriver === "risk") {
      const parsedRisk = optNum(riskPct);
      if (riskPct.trim() !== "" && (parsedRisk === undefined || parsedRisk < 0.01 || parsedRisk > 10)) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("单笔风险 (%) 必须在 0.01% ~ 10% 之间");
        return;
      }
      submitRiskPct = parsedRisk ?? 1.0;
    } else if (effectiveDriver === "amount" || effectiveDriver === "qty") {
      if (!activeSpec.knownSpec || sizingPreview.unitCost == null) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前合约缺少规格或跨币种汇率，请先补全汇率或按「单笔风险 %」运行一次算仓。");
        return;
      }
      if (sizingPreview.estQty == null || sizingPreview.estQty <= 0) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr(`当前设定仓位小于最小下单单位（${activeSpec.minOrderQty} ${unitLabel}），请调大投入金额或数量。`);
        return;
      }
      if (sizingPreview.exceedsEquity) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前设定仓位所需资金超过账户净值上限，请调小投入金额/数量或提高本金。");
        return;
      }
      if (
        sizingPreview.stop == null ||
        sizingPreview.unitRisk == null ||
        sizingPreview.equity == null ||
        sizingPreview.impliedBudgetRiskPct == null
      ) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前尚未确定有效 30m 止损位（如方向与实时止损同侧），请先切回 30m 视图或按「单笔风险 %」验算获取止损。");
        return;
      }
      if (sizingPreview.exceedsRiskCap) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr(
          `按当前设定仓位与 30m 止损测算，单笔风险敞口达 ${sizingPreview.impliedBudgetRiskPct.toFixed(2)}%，超过系统 10% 风控上限；请减小仓位或收紧止损。`,
        );
        return;
      }
      const nextStepRiskPct =
        ((sizingPreview.estQty + activeSpec.lotStep) * sizingPreview.unitRisk) /
        (sizingPreview.equity * sizingPreview.slot) *
        100;
      const safeBump = Math.min(1e-3, Math.max(1e-6, (nextStepRiskPct - sizingPreview.impliedBudgetRiskPct) * 0.25));
      submitRiskPct = Math.min(10.0, sizingPreview.impliedBudgetRiskPct + safeBump);
    }

    if (planAbortRef.current) {
      planAbortRef.current.abort();
    }
    const ctrl = new AbortController();
    planAbortRef.current = ctrl;
    const reqId = ++planReqIdRef.current;
    const capturedRev = planInputRevRef.current;

    setBusy(true);
    setErr(null);
    try {
      const effectivePairVerified = Boolean(
        pairTrade && pairLegVerified && cleanPairOppositeSym.length > 0 && !pairSameAsPrimary,
      );
      const body: TradePlanBody = {
        symbol,
        direction,
        live,
        entry_price: optNum(entryPrice),
        target_price: optNum(targetPrice),
        risk_budget_pct: submitRiskPct,
        slot_risk_share: optNum(slotShare) ?? 1.0,
        ...(effectiveCap?.maxMarginPct != null ? { max_margin_pct: effectiveCap.maxMarginPct } : {}),
        ...(effectiveCap?.maxLeverage != null ? { max_leverage: effectiveCap.maxLeverage } : {}),
        horizon_days: Math.max(1, Math.round(optNum(horizonDays) ?? 90)),
        has_l1_l2: archiveVerifiedL1L2,
        pending_verifications: Math.max(0, Math.round(optNum(pendingVerif) ?? 0)),
        thesis_falsified: thesisFalsified,
        pair_trade: pairTrade,
        pair_leg_verified: effectivePairVerified,
        ...(effectivePairVerified ? { pair_leg_symbol: cleanPairOppositeSym } : {}),
        hedge_only: hedgeOnly,
      };

      /* Strict Live 30m stop discipline: NEVER send manual stops or alignment overrides when live === true */
      if (!live) {
        body.tactical_stop_30m = optNum(stop30m);
        body.structural_stop_daily = optNum(stopDaily);
        if (is30mSnapshot && align?.alignment_status) {
          body.alignment_status = align.alignment_status;
        }
      }

      if (useSimEquity) {
        body.account_equity = optNum(simEquity);
        body.account_currency = simCurrency.trim().toUpperCase() || "USD";
      } else {
        body.account_currency = effectiveAcctCurrency;
      }

      if (fxVal !== undefined && !isKnownSameCurrency) {
        body.fx_to_account = fxVal;
      }

      const res = await api.tradePlan(body, ctrl.signal);
      if (
        ctrl.signal.aborted ||
        reqId !== planReqIdRef.current ||
        capturedRev !== planInputRevRef.current
      ) {
        return;
      }
      setPlan(res);
      setPlanStale(false);
      if (effectiveCap && res.position_plan) {
        const computedOutlay =
          res.position_plan.estimated_margin_account ?? res.position_plan.notional_value_account;
        if (computedOutlay != null && Number.isFinite(computedOutlay)) {
          setAllocAmount(String(Math.round(computedOutlay)));
        }
        if (res.position_plan.total_quantity != null && Number.isFinite(res.position_plan.total_quantity)) {
          const isFrac =
            typeof res.position_plan.contract_spec?.fractional === "boolean"
              ? res.position_plan.contract_spec.fractional
              : activeSpec.fractional;
          setManualQty(formatQtyDisplay(res.position_plan.total_quantity, isFrac));
        }
      }
    } catch (ex) {
      if (
        ctrl.signal.aborted ||
        (ex instanceof DOMException && ex.name === "AbortError") ||
        reqId !== planReqIdRef.current
      ) {
        return;
      }
      if (ex instanceof ApiError && ex.status === 409) {
        setLedgerMissingDetected(true);
      }
      setPlan(null);
      setPlanStale(true);
      setErr(sanitizePlanMessageZh(errorMessage(ex)));
    } finally {
      if (reqId === planReqIdRef.current) {
        if (planAbortRef.current === ctrl) {
          planAbortRef.current = null;
        }
        setBusy(false);
      }
    }
  };

  const handleCalculate = (e: FormEvent) => {
    e.preventDefault();
    void executeTradePlan();
  };

  const gate = plan?.four_quadrant_gate;
  const carry = plan?.expression_and_carry;
  const pos = plan?.position_plan;
  const offensiveCampaign = pos?.offensive_campaign ?? null;
  const egSummary = plan?.expectation_gap_summary ?? gate?.expectation_gap_summary ?? null;
  const isCrowdedExpectationGap =
    String(egSummary?.crowding_assessment || "").trim().toLowerCase() === "crowded";

  const planAssetClass = (pos?.asset_class ?? carry?.asset_class ?? resolvedAssetClass).toLowerCase();
  const planIsEquity = planAssetClass === "us_stock_or_etf" || planAssetClass === "a_share";
  const planUnitLabel = assetUnitLabel(planAssetClass, pos?.contract_spec ?? dualData?.contract_spec);
  const planEquity = pos?.account_equity ?? sizingPreview.equity ?? null;
  const planNotional = pos?.notional_value_account ?? null;
  const planMargin = pos?.estimated_margin_account ?? null;
  const planNotionalLeverage =
    planNotional != null && planEquity != null && planEquity > 0 ? planNotional / planEquity : null;
  const planEquityOutlayPct =
    pos?.margin_pct_of_equity ??
    (planNotional != null && planEquity != null && planEquity > 0 ? (planNotional / planEquity) * 100 : null);
  const planStopDistPct =
    pos?.stop_distance_30m_pct ??
    (pos?.entry_price != null && pos.entry_price > 0 && pos?.tactical_stop_30m != null
      ? (Math.abs(pos.entry_price - pos.tactical_stop_30m) / pos.entry_price) * 100
      : sizingPreview.stopDistPct);
  const planFractional =
    typeof pos?.contract_spec?.fractional === "boolean"
      ? pos.contract_spec.fractional
      : activeSpec.fractional;

  const equityFundamentals = useMemo(
    () => extractEquityFundamentals(dualData?.fundamentals),
    [dualData?.fundamentals],
  );

  const planGuardrailAlerts = useMemo(() => {
    const items: { severity: "warning" | "critical"; text: string }[] = [];
    if (planIsEquity && planEquityOutlayPct != null && planEquityOutlayPct > 30) {
      items.push({
        severity: planEquityOutlayPct > 50 ? "critical" : "warning",
        text: `单股集中度偏高：按当前 30m 窄止损反推，资金占用达净值的 ${fmtPct(planEquityOutlayPct, 1)}（> 30% 警戒线），建议压低单标的资金上限。`,
      });
    }
    if (!planIsEquity && planNotionalLeverage != null && planNotionalLeverage > 3.0) {
      items.push({
        severity: planNotionalLeverage > 5.0 ? "critical" : "warning",
        text: `高名义杠杆警戒：当前名义敞口 ${fmtMoney(planNotional, pos?.account_currency)}，实际杠杆达 ${planNotionalLeverage.toFixed(2)}×（> 3.0× 警戒线）；保证金占比低不代表低风险，隔夜跳空将按名义敞口放大实际亏损。`,
      });
    }
    if (planStopDistPct != null && planStopDistPct < 0.5) {
      items.push({
        severity:
          (!planIsEquity && planNotionalLeverage != null && planNotionalLeverage > 3.0) ||
          (planIsEquity && planEquityOutlayPct != null && planEquityOutlayPct > 30)
            ? "critical"
            : "warning",
        text: `30m 止损距极窄（仅 ${fmtPct(planStopDistPct, 2)} < 0.50%）：极易因隔夜跳空、财报或宏观数据发布触发滑点穿仓，建议主动压低名义仓位或改用更宽结构防线。`,
      });
    }
    return items;
  }, [planIsEquity, planEquityOutlayPct, planNotionalLeverage, planNotional, pos?.account_currency, planStopDistPct]);

  const planGuardrailSevere = planGuardrailAlerts.some((a) => a.severity === "critical");

  const applyGuardrailCap = (mode: "equity_25pct" | "leverage_2x") => {
    const nextCap =
      mode === "equity_25pct" ? { maxMarginPct: 25 } : { maxLeverage: 2.0 };
    setCapOverride(nextCap);
    setSizingDriver("risk");
    setPlanStale(true);
    void executeTradePlan({ capOverride: nextCap, forceRiskDriver: true });
  };

  const equitySourceRaw = plan?.equity_source ?? pos?.equity_source ?? null;
  const isRealLedgerPlan = equitySourceRaw === "portfolio_ledger";
  const stopSourceRaw = plan?.stop_source ?? pos?.stop_source ?? null;
  const stopTfRaw = plan?.stop_timeframe ?? pos?.stop_timeframe ?? null;

  const canOpenPosition = Boolean(!planStale && !busy && plan && gate?.actionable === true);

  const isBackwardationShort =
    (pos?.direction ?? direction) === "short" &&
    carry?.curve_structure &&
    carry.curve_structure.toLowerCase().includes("backwardation");

  const isDirectionConflict = gate?.decision === "rejected_direction_conflict";
  const isResearchDirectionPending =
    gate?.decision === "watchlist_research_pending" ||
    (gate?.research_direction === "neutral" && gate?.direction_aligned === false);
  const needsArchiveDirectionConfirm = Boolean(
    gate && !gate.actionable && (isDirectionConflict || isResearchDirectionPending),
  );
  const archiveTargetMarket = plan?.archive_context?.market ?? archiveContext?.market;

  const handleOpenLedgerFromPlan = () => {
    if (!canOpenPosition || !plan) return;
    const computedPos = plan.position_plan;
    const computedDir: Direction =
      plan.four_quadrant_gate.direction === "short" || computedPos.direction === "short"
        ? "short"
        : "long";
    const realLedgerCcy = (accountCurrency || computedPos.account_currency || "USD").toUpperCase();
    const planAcctCcy = (computedPos.account_currency || realLedgerCcy).toUpperCase();
    const inheritFx =
      planAcctCcy === realLedgerCcy ? (computedPos.fx_to_account ?? null) : null;

    const planPrimaryTheme = (plan.archive_context?.layer1_drivers?.primary || "").trim();
    const ctxTheme = (archiveContext?.sectorTheme || "").trim();
    const preferredTheme =
      (planPrimaryTheme && !isGenericSectorTheme(planPrimaryTheme) ? planPrimaryTheme : "") ||
      (ctxTheme && !isGenericSectorTheme(ctxTheme) ? ctxTheme : "") ||
      planPrimaryTheme ||
      ctxTheme ||
      "";
    const tranches = Array.isArray(computedPos.tranches) ? computedPos.tranches : [];
    const firstTranche = tranches.length > 0 ? tranches[0] : null;
    const oc = computedPos.offensive_campaign ?? null;

    onOpenPositionSeed({
      symbol: computedPos.symbol,
      direction: computedDir,
      sector_theme: preferredTheme,
      asset_class: computedPos.asset_class ?? dualData?.asset_class ?? null,
      entry_price: computedPos.entry_price ?? null,
      tactical_stop_30m: computedPos.tactical_stop_30m ?? null,
      structural_stop_daily: computedPos.structural_stop_daily ?? null,
      target_price: computedPos.target_price ?? null,
      quantity: oc?.full_pilot_qty ?? computedPos.total_quantity ?? null,
      first_tranche_qty: oc?.split_pilot_qty ?? firstTranche?.quantity ?? null,
      first_tranche_weight: firstTranche?.weight ?? null,
      full_plan_qty: oc?.full_pilot_qty ?? computedPos.total_quantity ?? null,
      campaign_1r_amount: oc?.campaign_1r_amount ?? null,
      tranches,
      fx_to_account: inheritFx,
    });
  };

  const togglePick = (field: PickPriceField) => {
    if (!onRequestPickField) return;
    onRequestPickField(activePickField === field ? null : field);
  };

  const displayRiskValue =
    sizingDriver === "risk"
      ? riskPct
      : sizingPreview.impliedBudgetRiskPct != null
        ? sizingPreview.impliedBudgetRiskPct > 0 && sizingPreview.impliedBudgetRiskPct < 0.01
          ? sizingPreview.impliedBudgetRiskPct.toFixed(4)
          : sizingPreview.impliedBudgetRiskPct.toFixed(2)
        : "";
  const displayAmountValue =
    sizingDriver === "amount"
      ? allocAmount
      : sizingPreview.estOutlay != null
        ? String(Math.round(sizingPreview.estOutlay))
        : "";
  const displayQtyValue =
    sizingDriver === "qty"
      ? manualQty
      : sizingPreview.estQty != null
        ? formatQtyDisplay(sizingPreview.estQty, activeSpec.fractional)
        : "";

  return (
    <section className="card flex flex-col gap-3.5 p-4" aria-labelledby={headingId}>
      {/* Compact Planner Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Calculator className="h-4 w-4 text-muted" aria-hidden="true" />
          <h2 id={headingId} className="m-0 text-[13px] font-semibold tracking-tight text-ink">
            四象限终裁 & 30min 双向算仓
          </h2>
          {live ? (
            <StatusBadge tone="info" label="Live 30m 止损锁定" />
          ) : (
            <StatusBadge
              tone="warning"
              label={is30mSnapshot ? "手动推演模式" : `手动推演（当前视图 ${snapInterval}）`}
            />
          )}
          {archiveContext?.exists ? (
            <StatusBadge
              tone={archiveContext.hasL1L2Evidence ? "good" : "neutral"}
              label={`已联动研究档案 (${archiveContext.evidenceCount ?? 0} 条证据)`}
            />
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!ledgerUninitialized && !useSimEquity && resolvedEquity != null ? (
            <span className="tnum font-mono text-[11.5px] text-muted">
              当前账本 {fmtMoney(resolvedEquity, effectiveAcctCurrency)}
            </span>
          ) : null}
          <button
            type="button"
            className="chip"
            aria-pressed={showOverlay}
            onClick={() => setShowOverlay((v) => !v)}
            title="在上方 K 线图预览当前算仓的入场价 / 止损位 / 目标位虚线尺"
          >
            <Eye className="h-3 w-3" aria-hidden="true" />
            <span>图上预览计划尺</span>
          </button>
          {!ledgerUninitialized && !useSimEquity ? (
            <button
              type="button"
              className="chip"
              onClick={() => {
                setUseSimEquity(true);
                markStale();
              }}
            >
              切换模拟本金
            </button>
          ) : null}
        </div>
      </div>

      {/* Uninitialized Ledger / Simulation Equity Bar */}
      {ledgerUninitialized || useSimEquity ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-hairline bg-sunken px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-2.5">
            <label className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-ink">
              <input
                type="checkbox"
                name="use_sim_equity"
                checked={useSimEquity}
                onChange={(e) => {
                  setUseSimEquity(e.target.checked);
                  markStale();
                }}
              />
              <span>模拟本金测算</span>
            </label>
            <span className="text-muted">
              {ledgerUninitialized
                ? "本地账本未初始化，可直接按模拟净值试算，或一键用此金额建立模拟账本记账"
                : "沙盘试算模式（不改动真实账本净值）"}
            </span>
          </div>

          {useSimEquity ? (
            <div className="flex flex-wrap items-center gap-2">
              <label className="inline-flex items-center gap-1.5 text-ink-2">
                <span>净值</span>
                <input
                  className="input h-7 w-28 font-mono"
                  type="number"
                  inputMode="decimal"
                  name="sim_equity"
                  step="any"
                  min="100"
                  aria-label="模拟账户净值"
                  value={simEquity}
                  onChange={(e) => {
                    setSimEquity(e.target.value);
                    markStale();
                  }}
                />
              </label>
              <label className="inline-flex items-center gap-1.5 text-ink-2">
                <span>币种</span>
                <select
                  className="input h-7 w-20 font-mono"
                  name="sim_currency"
                  aria-label="模拟账户本位币"
                  value={simCurrency}
                  onChange={(e) => {
                    setSimCurrency(e.target.value);
                    markStale();
                  }}
                >
                  <option value="USD">USD</option>
                  <option value="CNY">CNY</option>
                  <option value="HKD">HKD</option>
                </select>
              </label>
              {ledgerUninitialized ? (
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={initBusy}
                  onClick={handleQuickInitLedger}
                  title="以当前填写的模拟净值直接初始化本地持仓账本，算仓通过后即可直接记录持仓"
                >
                  <Wallet className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{initBusy ? "建账中…" : "用此金额建模拟账本"}</span>
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {ledgerInitNotice ? (
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-md border border-good/40 bg-good/10 px-3 py-1.5 text-xs text-ink"
        >
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-good" aria-hidden="true" />
          <span>{ledgerInitNotice}</span>
        </div>
      ) : null}

      <form onSubmit={handleCalculate} aria-labelledby={headingId} className="flex flex-col gap-3">
        {/* Row 1: Direction, Entry, 30m Stop, Target & Submit */}
        <div className="grid grid-cols-1 items-end gap-2.5 sm:grid-cols-2 lg:grid-cols-12">
          <div className="field lg:col-span-3">
            <span>方向 & 止损模式</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                className="chip chip-bull flex-1 justify-center py-1"
                aria-pressed={direction === "long"}
                onClick={() => {
                  if (direction !== "long") {
                    setDirection("long");
                    if (!live) {
                      const curSd = optNum(stopDaily);
                      const refEp = optNum(entryPrice) ?? lastClose;
                      if (curSd != null && refEp != null && curSd >= refEp) {
                        setStopDaily(fmtInputPrice(pickValidDailyStop("long", refEp)));
                      }
                    }
                    markStale();
                  }
                }}
              >
                做多 ↑
              </button>
              <button
                type="button"
                className="chip chip-bear flex-1 justify-center py-1"
                aria-pressed={direction === "short"}
                onClick={() => {
                  if (direction !== "short") {
                    setDirection("short");
                    if (!live) {
                      const curSd = optNum(stopDaily);
                      const refEp = optNum(entryPrice) ?? lastClose;
                      if (curSd != null && refEp != null && curSd <= refEp) {
                        setStopDaily(fmtInputPrice(pickValidDailyStop("short", refEp)));
                      }
                    }
                    markStale();
                  }
                }}
              >
                做空 ↓
              </button>
              <button
                type="button"
                className="chip py-1"
                aria-pressed={live}
                onClick={() => {
                  const next = !live;
                  setLive(next);
                  if (next && activePickField === "stop30m") {
                    onRequestPickField?.(null);
                  }
                  markStale();
                }}
                title="开启后强制锁定后端实时 30min 结构止损；关闭可手动推演或点图拾取止损"
              >
                <Lock className="h-3 w-3" aria-hidden="true" />
                <span>Live</span>
              </button>
            </div>
          </div>

          <label className="field lg:col-span-2">
            <span className="flex items-center justify-between">
              <span>计划入场价</span>
              {onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("entry")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "entry" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取入场价"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              name="entry_price"
              step="any"
              placeholder="默认现价"
              value={entryPrice}
              onChange={(e) => {
                setEntryPrice(e.target.value);
                markStale();
              }}
            />
          </label>

          <label className="field lg:col-span-2">
            <span className="flex items-center justify-between">
              <span className="inline-flex items-center gap-1">
                <span>30m 战术止损</span>
                {live ? <Lock className="h-3 w-3 text-accent" aria-hidden="true" /> : null}
              </span>
              {!live && onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("stop30m")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "stop30m" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取手动止损位"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className={`input font-mono ${live ? "cursor-not-allowed opacity-65" : ""}`}
              type="number"
              inputMode="decimal"
              name="tactical_stop_30m"
              step="any"
              disabled={live}
              placeholder={live ? "Live 自动锁定" : is30mSnapshot ? "30m 止损" : "手填 30m 止损"}
              value={stop30m}
              onChange={(e) => {
                setStop30m(e.target.value);
                markStale();
              }}
            />
          </label>

          <label className="field lg:col-span-3">
            <span className="flex items-center justify-between">
              <span>目标价 (≥2.5R)</span>
              {onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("target")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "target" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取目标价"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              name="target_price"
              step="any"
              placeholder="1Q~1Y 目标位"
              value={targetPrice}
              onChange={(e) => {
                setTargetPrice(e.target.value);
                markStale();
              }}
            />
          </label>

          <div className="lg:col-span-2">
            <button
              type="submit"
              className="btn btn-primary w-full"
              disabled={busy || (live && (snapshotLoading || !dualData))}
              title={
                live && !dualData
                  ? "正在等待当前标的双周期缠论快照就绪（避免错配算仓）"
                  : undefined
              }
            >
              <Calculator className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{busy ? "验算中…" : snapshotLoading && live ? "等快照…" : "运行算仓"}</span>
            </button>
          </div>
        </div>

        {/* Row 2: Bidirectional Sizing Strip (Risk % <-> Outlay Amount <-> Quantity) + Live Preview */}
        <div className="flex flex-col gap-2.5 rounded-md border border-hairline bg-sunken/70 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11.5px]">
            <span className="font-medium text-ink">
              仓位 & 风险双向联动
              <span className="ml-1.5 font-normal text-muted">
                （任填一项，另外两项实时联动换算）
              </span>
            </span>
            <div className="flex flex-wrap items-center gap-1">
              {[0.5, 1, 2].map((rp) => (
                <button
                  key={`r-${rp}`}
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  aria-pressed={sizingDriver === "risk" && Number(riskPct) === rp}
                  onClick={() => applyQuickRiskPct(rp)}
                >
                  {rp}%险
                </button>
              ))}
              <span className="mx-1 text-hairline" aria-hidden="true">
                |
              </span>
              {[0.1, 0.2, 0.3, 0.5].map((ap) => (
                <button
                  key={`a-${ap}`}
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  disabled={!sizingPreview.equity}
                  onClick={() => applyQuickAllocPct(ap)}
                >
                  {Math.round(ap * 100)}%仓
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <label className="field">
              <span className="flex items-center justify-between">
                <span>单笔风险 (%)</span>
                {sizingDriver === "risk" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动反算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "risk" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="risk_budget_pct"
                step="any"
                min={sizingDriver === "risk" ? "0.01" : "0"}
                max={sizingDriver === "risk" ? "10" : undefined}
                placeholder="如 1.0"
                aria-invalid={sizingPreview.exceedsRiskCap ? true : undefined}
                value={displayRiskValue}
                onChange={(e) => {
                  setSizingDriver("risk");
                  setRiskPct(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field">
              <span className="flex items-center justify-between">
                <span>
                  {isEquityAsset ? "资金占用" : "占用保证金"} ({effectiveAcctCurrency})
                </span>
                {sizingDriver === "amount" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动换算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "amount" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="alloc_amount"
                step="any"
                min="0"
                placeholder={sizingPreview.equity ? `最高 ${Math.round(sizingPreview.equity)}` : "输入计划投入金额"}
                aria-invalid={sizingPreview.exceedsEquity ? true : undefined}
                value={displayAmountValue}
                onChange={(e) => {
                  setSizingDriver("amount");
                  setAllocAmount(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field">
              <span className="flex items-center justify-between">
                <span>
                  计划头寸 ({unitLabel}
                  {activeSpec.lotStep > 1 ? ` · 步进 ${activeSpec.lotStep}` : ""})
                </span>
                {sizingDriver === "qty" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动换算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "qty" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="manual_quantity"
                step="any"
                min="0"
                placeholder={`输入${unitLabel}数`}
                value={displayQtyValue}
                onChange={(e) => {
                  setSizingDriver("qty");
                  setManualQty(e.target.value);
                  markStale();
                }}
              />
            </label>
          </div>

          {/* Instant Plain-Chinese Preview Bar */}
          <div
            role="status"
            aria-live="polite"
            className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2 text-[11.5px] text-ink-2"
          >
            <div className="tnum flex flex-wrap items-center gap-x-3 gap-y-1">
              {sizingPreview.stopPrecisionCollapsed ? (
                <span className="text-critical">
                  入场价或 30m 止损价在 6 位小数精度下归零或重合，后端将拒绝算仓（数量归零），请调整价格或扩大止损间距。
                </span>
              ) : sizingPreview.estQty != null && sizingPreview.estQty > 0 ? (
                <>
                  <span>
                    预估仓位：
                    <strong className="font-mono text-ink">
                      {fmtNum(sizingPreview.estQty, activeSpec.fractional ? 4 : 0)} {unitLabel}
                    </strong>
                    {sizingPreview.outlayPct != null ? (
                      <span
                        className={
                          isEquityAsset && sizingPreview.outlayPct > 30
                            ? sizingPreview.outlayPct > 50
                              ? "font-medium text-critical"
                              : "font-medium text-warning"
                            : "text-muted"
                        }
                      >
                        （{isEquityAsset ? "资金占净值" : "保证金占净值"} {fmtPct(sizingPreview.outlayPct, 1)}）
                      </span>
                    ) : null}
                  </span>
                  {!isEquityAsset &&
                  sizingPreview.estNotional != null &&
                  sizingPreview.notionalLeverage != null ? (
                    <span>
                      名义敞口：
                      <strong
                        className={`font-mono ${
                          sizingPreview.notionalLeverage > 5.0
                            ? "text-critical"
                            : sizingPreview.notionalLeverage > 3.0
                              ? "text-warning"
                              : "text-ink"
                        }`}
                      >
                        {fmtMoney(sizingPreview.estNotional, effectiveAcctCurrency)} (
                        {sizingPreview.notionalLeverage.toFixed(2)}×)
                      </strong>
                    </span>
                  ) : null}
                  {sizingPreview.estRiskAmt != null && sizingPreview.actualRiskPct != null ? (
                    <span>
                      30m 止损亏：
                      <strong
                        className={`font-mono ${
                          sizingPreview.exceedsRiskCap ? "text-critical" : "text-bear"
                        }`}
                      >
                        -{fmtMoney(sizingPreview.estRiskAmt, effectiveAcctCurrency)} (
                        {fmtPct(sizingPreview.actualRiskPct, 2)})
                      </strong>
                      {sizingPreview.stopDistPct != null ? (
                        <span
                          className={
                            sizingPreview.stopDistPct < 0.5
                              ? "ml-1 font-medium text-warning"
                              : "ml-1 text-muted"
                          }
                        >
                          [距 {fmtPct(sizingPreview.stopDistPct, 2)}]
                        </span>
                      ) : null}
                    </span>
                  ) : null}
                  {sizingPreview.rrRatio != null ? (
                    <span>
                      名义盈亏比：
                      <strong
                        className={`font-mono ${
                          !sizingPreview.rrBelow25 ? "text-good" : "text-warning"
                        }`}
                      >
                        {fmtRrDisplay(sizingPreview.rrRatio, sizingPreview.rrRatio)}
                      </strong>
                    </span>
                  ) : null}
                </>
              ) : sizingPreview.belowMinLot ? (
                <span className="text-warning">
                  当前设定不足最小下单单位（{activeSpec.minOrderQty} {unitLabel}），请调大金额或风险预算。
                </span>
              ) : needsCrossCurrencyFx && !sizingPreview.fx ? (
                <span className="text-warning">填写下方跨币种汇率后即可实时预览仓位与止损风险。</span>
              ) : sizingPreview.stop == null ? (
                <span className="text-muted">
                  当前方向暂无同侧 30m 止损预览；点击「运行算仓」由后端校验实时 30m 结构或关闭实时止损手动推演。
                </span>
              ) : (
                <span className="text-muted">填写上方任一仓位或风险参数，即可实时预览仓位、资金占用与止损额。</span>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {capOverride ? (
                <span className="inline-flex items-center gap-1 rounded border border-accent/45 bg-accent/12 px-2 py-0.5 text-[11px] font-medium text-accent">
                  <span>
                    {capOverride.maxMarginPct != null
                      ? `后端硬封顶：≤${capOverride.maxMarginPct}% 资金上限`
                      : `后端硬封顶：≤${capOverride.maxLeverage}× 名义杠杆`}
                  </span>
                  <button
                    type="button"
                    className="ml-0.5 inline-flex cursor-pointer items-center text-accent/80 hover:text-ink"
                    aria-label="取消临时封顶"
                    title="取消临时封顶（修改任意算仓参数也会自动清除）"
                    onClick={() => {
                      markStale();
                    }}
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                  </button>
                </span>
              ) : null}
              {sizingPreview.cappedByCash && !capOverride ? (
                <span className="text-[11px] font-medium text-warning">
                  已触发现金 1.0× 满仓封顶（实际风险低于预算）
                </span>
              ) : null}
              {sizingPreview.exceedsEquity ? (
                <span className="text-[11px] font-medium text-critical">超出账户净值上限</span>
              ) : null}
              {sizingPreview.exceedsRiskCap ? (
                <span className="text-[11px] font-medium text-critical">单笔风险超过 10% 上限</span>
              ) : null}
              {isEquityAsset && sizingPreview.outlayPct != null && sizingPreview.outlayPct > 30 ? (
                <button
                  type="button"
                  className="chip py-0.5 text-[11px] text-warning"
                  onClick={() => applyGuardrailCap("equity_25pct")}
                >
                  压至 25% 资金上限
                </button>
              ) : null}
              {!isEquityAsset &&
              sizingPreview.notionalLeverage != null &&
              sizingPreview.notionalLeverage > 3.0 ? (
                <button
                  type="button"
                  className="chip py-0.5 text-[11px] text-warning"
                  onClick={() => applyGuardrailCap("leverage_2x")}
                >
                  压至 2× 名义杠杆
                </button>
              ) : null}
              {sizingPreview.rrRatio != null &&
              sizingPreview.rrBelow25 &&
              sizingPreview.rr25TargetStr != null ? (
                <button
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  onClick={() => {
                    setTargetPrice(sizingPreview.rr25TargetStr!);
                    markStale();
                  }}
                >
                  目标价调至 2.5R ({sizingPreview.rr25TargetStr})
                </button>
              ) : null}
            </div>
          </div>
        </div>

        {/* Cross-Currency Banner surfaced immediately when required */}
        {needsCrossCurrencyFx && inferredQuoteCurrency ? (
          <CrossCurrencyFxBanner
            symbol={symbol}
            quoteCurrency={inferredQuoteCurrency}
            accountCurrency={effectiveAcctCurrency}
            value={fxToAccount}
            onChange={(val) => {
              setFxToAccount(val);
              markStale();
            }}
          />
        ) : null}

        {/* Progressive Disclosure: Secondary Risk Gates, Pair-Trade & Horizon */}
        <details className="disclosure" open={pairTrade || thesisFalsified || hedgeOnly || Number(pendingVerif) > 0}>
          <summary>
            <span>
              高级门控、席位份额与配对参数（{slotShare === "0.5" ? "双寡头 50%" : "唯一真龙头 100%"} · {horizonDays}天
              {hasL1L2 ? " · 具备 L1/L2" : " · 缺 L1/L2"}）
            </span>
          </summary>

          <div className="mt-2.5 grid grid-cols-1 gap-3 pt-1 sm:grid-cols-2 lg:grid-cols-12">
            <label className="field lg:col-span-2">
              <span className="inline-flex items-center gap-1">
                <span>日线结构位 (ZD/ZG)</span>
                {live ? <Lock className="h-3 w-3 text-muted" aria-hidden="true" /> : null}
              </span>
              <input
                className={`input font-mono ${live ? "cursor-not-allowed opacity-65" : ""}`}
                type="number"
                step="any"
                disabled={live}
                placeholder={live ? "Live 自动锁定" : "可选对照"}
                value={stopDaily}
                onChange={(e) => {
                  setStopDaily(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field lg:col-span-3">
              <span>龙头席位份额</span>
              <select
                className="input"
                value={slotShare}
                onChange={(e) => {
                  setSlotShare(e.target.value);
                  markStale();
                }}
              >
                <option value="1.0">100% 唯一真龙头</option>
                <option value="0.5">50% 双寡头共享额度</option>
              </select>
            </label>

            <label className="field lg:col-span-2">
              <span>预计持仓周期 (天)</span>
              <input
                className="input font-mono"
                type="number"
                min="5"
                max="365"
                value={horizonDays}
                onChange={(e) => {
                  setHorizonDays(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field lg:col-span-2">
              <span>未完成核验项数</span>
              <input
                className="input font-mono"
                type="number"
                min="0"
                max="10"
                value={pendingVerif}
                onChange={(e) => {
                  setPendingVerif(e.target.value);
                  markStale();
                }}
              />
            </label>

            {!needsCrossCurrencyFx ? (
              <label className="field lg:col-span-3">
                <span>跨币种折算汇率</span>
                <input
                  className="input font-mono"
                  type="number"
                  step="any"
                  min="0.000001"
                  disabled={isKnownSameCurrency}
                  placeholder={
                    isKnownSameCurrency ? `同币种 (${effectiveAcctCurrency}) 1.0` : "跨币种折算汇率"
                  }
                  value={isKnownSameCurrency ? "" : fxToAccount}
                  onChange={(e) => {
                    setFxToAccount(e.target.value);
                    markStale();
                  }}
                />
              </label>
            ) : null}

            <div className="flex flex-wrap items-center gap-4 lg:col-span-12">
              <span
                className="inline-flex items-center gap-1.5 text-xs text-ink-2"
                title="由本地研报档案库自动核验，需在研报档案中录入至少 1 条 L1 或 L2 证据方可放行"
              >
                <input
                  type="checkbox"
                  checked={hasL1L2}
                  disabled
                  readOnly
                />
                <span>
                  L1/L2 核心证据（{hasL1L2 ? "档案已核验" : "需先在研报档案录入 L1/L2 证据"}）
                </span>
              </span>
              <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
                <input
                  type="checkbox"
                  checked={pairTrade}
                  onChange={(e) => {
                    const checked = e.target.checked;
                    setPairTrade(checked);
                    if (!checked) {
                      setPairLegVerified(false);
                      setPairOppositeSym("");
                    }
                    markStale();
                  }}
                />
                <span>同板块多空配对</span>
              </label>
              <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
                <input
                  type="checkbox"
                  checked={hedgeOnly}
                  onChange={(e) => {
                    setHedgeOnly(e.target.checked);
                    markStale();
                  }}
                />
                <span>股票阶段性对冲卖点</span>
              </label>
              <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
                <input
                  type="checkbox"
                  checked={thesisFalsified}
                  onChange={(e) => {
                    setThesisFalsified(e.target.checked);
                    markStale();
                  }}
                />
                <span>核心逻辑已被 L1 证伪</span>
              </label>
            </div>

            {pairTrade ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-ink lg:col-span-12">
                <div className="max-w-2xl">
                  <strong>配对单腿保护：</strong>未完成反向腿研究与缠论确认前默认保留在观察池。
                </div>
                <div className="flex flex-wrap items-center gap-2.5">
                  <input
                    className="input h-7 w-36 font-mono"
                    placeholder="反向腿代码 (如 ETN)"
                    aria-label="已核验反向对冲腿标的代码"
                    value={pairOppositeSym}
                    onChange={(e) => {
                      setPairOppositeSym(e.target.value);
                      setPairLegVerified(false);
                      markStale();
                    }}
                  />
                  <label className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-ink">
                    <input
                      type="checkbox"
                      disabled={!cleanPairOppositeSym || pairSameAsPrimary}
                      checked={pairLegVerified && Boolean(cleanPairOppositeSym) && !pairSameAsPrimary}
                      onChange={(e) => {
                        setPairLegVerified(e.target.checked);
                        markStale();
                      }}
                    />
                    <span>已独立核验反向腿</span>
                  </label>
                  {pairSameAsPrimary ? (
                    <span className="text-[11px] font-medium text-critical">
                      反向腿不能与主标的 ({symbol}) 相同
                    </span>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
        </details>
      </form>

      {err ? (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
          <div>
            <div className="font-semibold">算仓风控拦截</div>
            <div className="mt-0.5 text-ink-2">{sanitizePlanMessageZh(err)}</div>
          </div>
        </div>
      ) : null}

      {openedReceipt ? (
        <div
          role="status"
          aria-live="polite"
          className="flex flex-wrap items-center justify-between gap-2.5 rounded-md border border-good/50 bg-good/12 px-3.5 py-2.5 text-xs text-ink"
        >
          <div className="flex min-w-0 items-start gap-2">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-good" aria-hidden="true" />
            <div className="min-w-0">
              <span className="font-semibold text-ink">
                已录入持仓账本：{openedReceipt.symbol}{" "}
                {openedReceipt.direction === "short" ? "做空" : "做多"}{" "}
                {fmtNum(openedReceipt.quantity, 4)}{" "}
                {assetUnitLabel(openedReceipt.asset_class || resolvedAssetClass)} @{" "}
                {fmtPrice(openedReceipt.entry_price)}
              </span>
              {(() => {
                const extras = [
                  openedReceipt.tranche_label,
                  openedReceipt.archive_synced
                    ? "研报档案已自动同步为「持仓中」"
                    : archiveContext?.exists
                      ? "研报档案已同步「持仓中」"
                      : null,
                ].filter(Boolean);
                return extras.length > 0 ? (
                  <span className="text-ink-2">（{extras.join(" · ")}）</span>
                ) : null;
              })()}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {onJumpSentinel ? (
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={onJumpSentinel}
              >
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                <span>前往哨兵总控查看</span>
                <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            ) : null}
            {onDismissOpenedReceipt ? (
              <button
                type="button"
                className="btn btn-sm px-2 text-muted hover:text-ink"
                aria-label="关闭入账成功提示"
                onClick={onDismissOpenedReceipt}
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {plan && planStale && !openedReceipt ? (
        <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-ink">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>参数已修改，下方结果已锁定失效；请重新点击「运行算仓」后再录入账本。</span>
        </div>
      ) : null}

      {/* Narrow-Stop / Concentration / High-Leverage Risk Guardrail Banner */}
      {plan && planGuardrailAlerts.length > 0 ? (
        <div
          role="alert"
          className={`flex flex-wrap items-start justify-between gap-3 rounded-md border px-3.5 py-2.5 text-xs text-ink ${
            planGuardrailSevere
              ? "border-critical/50 bg-critical/10"
              : "border-warning/50 bg-warning/10"
          }`}
        >
          <div className="flex min-w-0 flex-1 items-start gap-2">
            {planGuardrailSevere ? (
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
            ) : (
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
            )}
            <div className="flex flex-col gap-1">
              <div className="font-semibold">
                {planIsEquity
                  ? "窄止损单股集中度护栏预警"
                  : "窄止损高杠杆 / 跳空风险护栏预警"}
              </div>
              <ul className="m-0 list-disc space-y-0.5 pl-4 text-ink-2">
                {planGuardrailAlerts.map((item, idx) => (
                  <li key={idx}>{item.text}</li>
                ))}
              </ul>
            </div>
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            {planIsEquity ? (
              <button
                type="button"
                className="btn btn-sm"
                disabled={busy}
                onClick={() => applyGuardrailCap("equity_25pct")}
                title="将单标的资金占用封顶至账户净值的 25%，反推对应股数与实际风险并立即重算"
              >
                <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
                <span>压至 25% 资金上限</span>
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-sm"
                disabled={busy}
                onClick={() => applyGuardrailCap("leverage_2x")}
                title="将保证金品种的名义敞口封顶至账户净值的 2×，反推对应头寸并立即重算"
              >
                <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
                <span>压至 2× 名义杠杆</span>
              </button>
            )}
          </div>
        </div>
      ) : null}

      {/* Clean Hairline-Divided 3-Column Output (zero box-in-box clutter) */}
      {plan ? (
        <div
          className={`grid grid-cols-1 divide-y divide-hairline border-t border-hairline pt-3.5 xl:grid-cols-12 xl:divide-x xl:divide-y-0 ${
            planStale && !openedReceipt ? "opacity-60" : ""
          }`}
        >
          {/* Column 1: Four-Quadrant Verdict */}
          <div className="flex flex-col gap-2.5 pb-3.5 xl:col-span-4 xl:pr-4 xl:pb-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="section-title">四象限最终闸门</span>
              {gate ? <PlannerGateBadge decision={gate.decision} actionable={gate.actionable} /> : null}
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge
                tone={isRealLedgerPlan ? "good" : "warning"}
                label={isRealLedgerPlan ? "真实账本净值" : "模拟测算净值"}
              />
              {stopSourceRaw || stopTfRaw ? (
                <StatusBadge
                  tone={stopSourceRaw === "live_30m_chanlun" ? "info" : "neutral"}
                  label={
                    stopSourceRaw === "live_30m_chanlun"
                      ? `实时 30m 止损`
                      : `手动推演止损`
                  }
                />
              ) : null}
              {gate?.research_direction ? (
                <StatusBadge
                  tone={
                    gate.direction_aligned === true
                      ? "good"
                      : isDirectionConflict
                        ? "critical"
                        : "warning"
                  }
                  label={`档案方向：${formatResearchDirectionZh(gate.research_direction)}`}
                />
              ) : null}
              {isCrowdedExpectationGap ? (
                <StatusBadge tone="critical" label="预期差拥挤警示" />
              ) : null}
            </div>

            {isCrowdedExpectationGap ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-critical/45 bg-critical/10 px-2.5 py-2 text-xs text-ink"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-critical" aria-hidden="true" />
                <div className="min-w-0">
                  <div className="font-semibold text-critical">共识拥挤警示 (Crowded)</div>
                  <div className="mt-0.5 text-[11px] text-ink-2">
                    研究档案评估该逻辑已进入拥挤区，禁止远离中枢追高，须等待 30min 结构完全确认且严守首仓 1R 纪律。
                  </div>
                </div>
              </div>
            ) : null}

            {needsArchiveDirectionConfirm && gate ? (
              <div
                role="alert"
                className={`rounded-md border px-3 py-2 text-xs text-ink ${
                  isDirectionConflict
                    ? "border-critical/45 bg-critical/10"
                    : "border-warning/45 bg-warning/10"
                }`}
              >
                <div className="flex items-start gap-2">
                  {isDirectionConflict ? (
                    <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-critical" aria-hidden="true" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                  )}
                  <div className="flex flex-col gap-1.5">
                    <div className="font-semibold">
                      {isDirectionConflict
                        ? `第三层研究方向（${formatResearchDirectionZh(gate.research_direction)}）与拟执行方向（${direction === "short" ? "做空" : "做多"}）冲突`
                        : gate.research_direction === "pair"
                          ? "研报档案研究方向为「同板块配对」"
                          : "研报档案研究方向仍为中性（待定性）"}
                    </div>
                    <div className="text-ink-2">
                      {isDirectionConflict
                        ? "四象限闸门已否决反向建仓。请先前往研报档案复核并更新研究方向，或切回与研究结论一致的执行方向。"
                        : gate.research_direction === "pair"
                          ? "请勾选高级门控中的「同板块多空配对」并核验反向腿，或前往研报档案明确单边做多/做空方向。"
                          : "第三层研究尚未定性做多或做空方向，四象限闸门已拦截建仓。请先在研报档案确认研究方向后再重新运行算仓。"}
                    </div>
                    {onJumpArchive ? (
                      <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() =>
                            onJumpArchive(symbol, archiveTargetMarket, {
                              tab: "thesis",
                              openMetaEditor: true,
                            })
                          }
                        >
                          <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>前往研报档案确认研究方向</span>
                          <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : null}

            {gate?.reason ? (
              <p className="m-0 text-xs font-medium text-ink">
                {sanitizePlanMessageZh(gate.reason)}
              </p>
            ) : null}
            {gate?.reasons && gate.reasons.length > 0 ? (
              <ul className="m-0 list-disc space-y-1 pl-4 text-xs text-ink-2">
                {gate.reasons.map((r, idx) => (
                  <li key={idx}>{sanitizePlanMessageZh(r)}</li>
                ))}
              </ul>
            ) : null}
            {gate && !gate.actionable ? (
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {planIsEquity && direction === "short" && !hedgeOnly && !pairTrade ? (
                  <>
                    <button
                      type="button"
                      className="chip py-0.5 text-[11px]"
                      onClick={() => {
                        setDirection("long");
                        markStale();
                      }}
                    >
                      切回做多
                    </button>
                    <button
                      type="button"
                      className="chip py-0.5 text-[11px]"
                      onClick={() => {
                        setHedgeOnly(true);
                        markStale();
                      }}
                    >
                      标记为股票对冲卖点
                    </button>
                  </>
                ) : null}
                {(() => {
                  const needs25Btn =
                    (((pos?.rr_ratio_raw ?? pos?.rr_ratio) != null &&
                      (pos!.rr_ratio_raw ?? pos!.rr_ratio)! < 2.5) ||
                      gate?.decision === "watchlist_poor_rr") &&
                    pos?.entry_price != null &&
                    pos?.tactical_stop_30m != null;
                  const target25Str = needs25Btn
                    ? computeRr25TargetStr(pos!.entry_price!, pos!.tactical_stop_30m!, direction)
                    : null;
                  if (!target25Str) return null;
                  return (
                    <button
                      type="button"
                      className="chip py-0.5 text-[11px]"
                      onClick={() => {
                        setTargetPrice(target25Str);
                        markStale();
                      }}
                    >
                      目标价设为 2.5R ({target25Str})
                    </button>
                  );
                })()}
                {!archiveVerifiedL1L2 && !needsArchiveDirectionConfirm && onJumpArchive ? (
                  <button
                    type="button"
                    className="chip py-0.5 text-[11px]"
                    onClick={() =>
                      onJumpArchive(symbol, archiveTargetMarket, {
                        tab: "evidence",
                        openEvidenceForm: true,
                      })
                    }
                  >
                    <BookOpen className="h-3 w-3" aria-hidden="true" />
                    <span>前往研报档案录入 L1/L2 证据</span>
                  </button>
                ) : null}
              </div>
            ) : null}
            {plan.live_warnings && plan.live_warnings.length > 0 ? (
              <div className="mt-1 space-y-1 rounded border border-warning/35 bg-warning/10 p-2 text-[11px] text-ink">
                {plan.live_warnings.map((w, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning" aria-hidden="true" />
                    <span>{sanitizePlanMessageZh(w)}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {/* Column 2: Equity Execution & Event Window vs Futures/FX/Crypto Curve & Carry */}
          {planIsEquity ? (
            <div className="flex flex-col gap-2.5 py-3.5 xl:col-span-4 xl:px-4 xl:py-0">
              <div className="flex items-center justify-between gap-2">
                <span className="section-title inline-flex items-center gap-1.5">
                  <Briefcase className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  股票执行与事件窗口
                </span>
                <StatusBadge tone="info" label={formatAssetClassZh(planAssetClass)} />
              </div>

              {(() => {
                const metricCells: { key: string; label: string; value: string; withCalendar?: boolean }[] = [];
                if (equityFundamentals.nextEarningsDate) {
                  metricCells.push({
                    key: "earnings",
                    label: "下次财报日",
                    value: equityFundamentals.nextEarningsDate,
                    withCalendar: true,
                  });
                }
                if (equityFundamentals.forwardPe != null) {
                  metricCells.push({
                    key: "forward_pe",
                    label: "前瞻 PE",
                    value: `${fmtNum(equityFundamentals.forwardPe, 1)}×`,
                  });
                } else if (equityFundamentals.trailingPe != null) {
                  metricCells.push({
                    key: "trailing_pe",
                    label: "静态 PE (TTM)",
                    value: `${fmtNum(equityFundamentals.trailingPe, 1)}×`,
                  });
                }
                if (equityFundamentals.dividendYieldPct != null) {
                  metricCells.push({
                    key: "dividend_yield",
                    label: "股息率",
                    value: fmtPct(equityFundamentals.dividendYieldPct, 2),
                  });
                }
                if (equityFundamentals.shortPctOfFloat != null) {
                  metricCells.push({
                    key: "short_pct",
                    label: "空头占流通比",
                    value: fmtPct(equityFundamentals.shortPctOfFloat, 2),
                  });
                }

                const missingEventOrShort =
                  !equityFundamentals.nextEarningsDate || equityFundamentals.shortPctOfFloat == null;

                return (
                  <>
                    {metricCells.length > 0 ? (
                      <div
                        className={`metric-strip ${
                          metricCells.length === 1 ? "grid-cols-1" : "grid-cols-2"
                        }`}
                      >
                        {metricCells.map((cell) => (
                          <div key={cell.key} className="metric-cell">
                            <span className="metric-label inline-flex items-center gap-1">
                              {cell.withCalendar ? (
                                <Calendar className="h-3 w-3 text-muted" aria-hidden="true" />
                              ) : null}
                              {cell.label}
                            </span>
                            <span className="metric-val">{cell.value}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}

                    {missingEventOrShort ? (
                      <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-hairline-soft bg-sunken/70 px-2.5 py-1.5 text-[11px] text-muted">
                        <span>完整财报日与空头数据请前往研报「实时硬数据」页查看</span>
                        {onJumpArchive ? (
                          <button
                            type="button"
                            className="inline-flex cursor-pointer items-center gap-0.5 font-medium text-accent hover:underline"
                            onClick={() =>
                              onJumpArchive(symbol, archiveTargetMarket, { tab: "hard_data" })
                            }
                          >
                            <span>查看硬数据</span>
                            <ArrowRight className="h-3 w-3" aria-hidden="true" />
                          </button>
                        ) : null}
                      </div>
                    ) : null}
                  </>
                );
              })()}

              <p className="m-0 text-xs text-ink-2">
                <strong className="text-ink">建议执行载体：</strong>
                {formatExpressionRecZh(
                  carry?.expression_recommendation,
                  pos?.direction ?? direction,
                  planAssetClass,
                  { pairTrade, hedgeOnly },
                )}
              </p>

              {carry?.contract_guidance ? (
                <p className="m-0 text-[11.5px] text-ink-2">
                  {sanitizePlanMessageZh(carry.contract_guidance)}
                </p>
              ) : null}

              <ul className="m-0 list-disc space-y-1 pl-4 text-[11px] text-muted">
                <li>定价纪律：交易未来 1~3 年远期预期折现，严禁死板套用静态当期 PE（强周期股切忌低 PE 接飞刀）。</li>
                <li>事件风控：临近财报窗口建议缩减首仓或配合保护性看跌/领口期权对冲，不逆日线方向硬扛。</li>
              </ul>
            </div>
          ) : (
            <div className="flex flex-col gap-2.5 py-3.5 xl:col-span-4 xl:px-4 xl:py-0">
              <div className="flex items-center justify-between gap-2">
                <span className="section-title inline-flex items-center gap-1.5">
                  <GitBranch className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  {planAssetClass === "fx"
                    ? "利差 Carry & 货币对表达"
                    : planAssetClass === "crypto"
                      ? "资金费率 & 合约表达"
                      : "期限结构 & 展期权衡"}
                </span>
                {carry?.curve_structure ? (
                  <StatusBadge
                    tone={isBackwardationShort ? "warning" : "info"}
                    label={formatCurveStructureZh(carry.curve_structure)}
                  />
                ) : (
                  <StatusBadge tone="neutral" label={formatAssetClassZh(carry?.asset_class || planAssetClass)} />
                )}
              </div>

              {isBackwardationShort ? (
                <div className="rounded border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-[11.5px] text-ink">
                  <strong>强现实 vs 弱预期劈叉：</strong>大 B 结构绝不否决做空，已量化展期损耗并推荐合约载体。
                </div>
              ) : null}

              <div className="metric-strip grid-cols-2">
                <div className="metric-cell">
                  <span className="metric-label">
                    {planAssetClass === "fx"
                      ? "年化利差 Carry"
                      : planAssetClass === "crypto"
                        ? "永续年化费率 Carry"
                        : "近月年化 Carry"}
                  </span>
                  <span className="metric-val">
                    {fmtPct(carry?.near_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                  </span>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">
                    {planAssetClass === "fx" || planAssetClass === "crypto"
                      ? `持有期 Carry (${carry?.holding_horizon_days ?? horizonDays}天)`
                      : "全曲线年化展期"}
                  </span>
                  <span className="metric-val">
                    {planAssetClass === "fx" || planAssetClass === "crypto"
                      ? fmtPct(carry?.horizon_carry_pct, 2, true)
                      : fmtPct(carry?.curve_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                  </span>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">计入 Carry 后 R:R</span>
                  <span className="metric-val">
                    {fmtRrDisplay(
                      carry?.carry_adjusted_rr_ratio_raw ?? pos?.carry_adjusted_rr_ratio_raw,
                      carry?.carry_adjusted_rr_ratio ?? pos?.carry_adjusted_rr_ratio,
                    )}
                  </span>
                </div>
                <div className="metric-cell">
                  <span className="metric-label">
                    {planAssetClass === "fx" || planAssetClass === "crypto"
                      ? "资产类别"
                      : "近月移仓压力 R:R"}
                  </span>
                  <span className="metric-val">
                    {planAssetClass === "fx" || planAssetClass === "crypto"
                      ? formatAssetClassZh(planAssetClass)
                      : fmtRrDisplay(
                          carry?.near_carry_stress_rr_ratio_raw,
                          carry?.near_carry_stress_rr_ratio,
                        )}
                  </span>
                </div>
              </div>

              {carry?.expression_recommendation ? (
                <p className="m-0 text-xs text-ink-2">
                  <strong className="text-ink">最优载体：</strong>
                  {formatExpressionRecZh(
                    carry.expression_recommendation,
                    pos?.direction ?? direction,
                    planAssetClass,
                  )}
                </p>
              ) : null}
              {carry?.contract_guidance ? (
                <p className="m-0 text-[11.5px] text-muted">
                  {sanitizePlanMessageZh(carry.contract_guidance)}
                </p>
              ) : null}
            </div>
          )}

          {/* Column 3: Position Sizing & Tranche Table */}
          <div className="flex flex-col gap-2.5 pt-3.5 xl:col-span-4 xl:pl-4 xl:pt-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <span className="section-title">核定仓位 & 40/40/20 分批计划</span>
                {openedReceipt && !planStale ? (
                  <StatusBadge tone="good" label="已录入账本" />
                ) : null}
              </div>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!canOpenPosition}
                title={
                  !gate?.actionable
                    ? "四象限闸门未放行，禁止直接录入持仓账本"
                    : planStale
                      ? "输入已变更，请重新运行算仓"
                      : "将核定仓位与止损带入持仓账本弹窗"
                }
                onClick={handleOpenLedgerFromPlan}
              >
                <PlusCircle className="h-3.5 w-3.5" aria-hidden="true" />
                <span>录入持仓账本</span>
              </button>
            </div>

            <div className="metric-strip grid-cols-2 sm:grid-cols-3">
              <div className="metric-cell">
                <span className="metric-label">核定总仓位</span>
                <span className="metric-val text-accent">
                  {pos?.total_quantity != null
                    ? `${fmtNum(pos.total_quantity, planFractional ? 4 : 0)} ${planUnitLabel}`
                    : pos?.total_quantity_str
                      ? `${pos.total_quantity_str} ${planUnitLabel}`
                      : "—"}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">实际风险敞口</span>
                <span className="metric-val">
                  {fmtMoney(pos?.actual_risk_amount, pos?.account_currency)} ({fmtPct(pos?.actual_risk_pct, 2)})
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">名义盈亏比</span>
                <span className="metric-val">{fmtRrDisplay(pos?.rr_ratio_raw, pos?.rr_ratio)}</span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">
                  入场 / 30m止损
                  {planStopDistPct != null ? (
                    <span
                      className={
                        planStopDistPct < 0.5 ? "ml-1 font-medium text-warning" : "ml-1"
                      }
                    >
                      ({fmtPct(planStopDistPct, 2)})
                    </span>
                  ) : null}
                </span>
                <span className="metric-val">
                  {fmtPrice(pos?.entry_price)} / <span className="text-bear">{fmtPrice(pos?.tactical_stop_30m)}</span>
                </span>
              </div>
              {planIsEquity ? (
                <>
                  <div className="metric-cell">
                    <span className="metric-label">资金占用</span>
                    <span
                      className={`metric-val ${
                        planEquityOutlayPct != null && planEquityOutlayPct > 50
                          ? "text-critical"
                          : planEquityOutlayPct != null && planEquityOutlayPct > 30
                            ? "text-warning"
                            : ""
                      }`}
                    >
                      {fmtMoney(planNotional ?? planMargin, pos?.account_currency)} (
                      {fmtPct(planEquityOutlayPct, 1)})
                    </span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">30m 省噪金额</span>
                    <span className="metric-val text-bull">
                      {fmtMoney(pos?.noise_risk_saved_amount, pos?.account_currency)}
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <div className="metric-cell">
                    <span className="metric-label">保证金占用</span>
                    <span className="metric-val">
                      {fmtMoney(planMargin, pos?.account_currency)} (占净值 {fmtPct(pos?.margin_pct_of_equity, 1)})
                    </span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">名义敞口 / 实际杠杆</span>
                    <span
                      className={`metric-val ${
                        planNotionalLeverage != null && planNotionalLeverage > 5.0
                          ? "text-critical"
                          : planNotionalLeverage != null && planNotionalLeverage > 3.0
                            ? "text-warning"
                            : ""
                      }`}
                    >
                      {fmtMoney(planNotional, pos?.account_currency)}{" "}
                      {planNotionalLeverage != null ? `(${planNotionalLeverage.toFixed(2)}×)` : ""}
                    </span>
                  </div>
                </>
              )}
            </div>

            {!planIsEquity && pos?.noise_risk_saved_amount != null ? (
              <div className="tnum flex items-center justify-between text-[11px] text-muted">
                <span>相对日线止损的 30m 省噪金额</span>
                <strong className="font-mono text-bull">
                  {fmtMoney(pos.noise_risk_saved_amount, pos?.account_currency)}
                </strong>
              </div>
            ) : null}

            {pos?.oversized_single_lot ? (
              <div className="rounded border border-critical/45 bg-critical/10 px-2.5 py-1.5 text-xs text-ink">
                单手最小合约风险超预算上限，已启动单手超预算保护（核定 0 手或改用迷你合约）。
              </div>
            ) : null}

            {pos?.tranches && pos.tranches.length > 0 ? (
              <div tabIndex={0} role="region" aria-label="40/40/20 分批建仓计划表" className="table-wrapper">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">批次</th>
                      <th scope="col" className="num">
                        权重
                      </th>
                      <th scope="col" className="num">
                        数量 ({planUnitLabel})
                      </th>
                      <th scope="col" className="num">
                        风险额
                      </th>
                      <th scope="col">执行纪律</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pos.tranches.map((tr) => (
                      <tr key={tr.tranche_index}>
                        <th scope="row">T{tr.tranche_index}</th>
                        <td data-label="权重" className="num">
                          {fmtPct((tr.weight ?? 0) * 100, 0)}
                        </td>
                        <td data-label={`数量 (${planUnitLabel})`} className="num">
                          <span>
                            {tr.quantity != null
                              ? fmtNum(tr.quantity, planFractional ? 4 : 0)
                              : tr.quantity_str || "—"}
                          </span>
                          {tr.lots != null && tr.quantity != null && tr.lots !== tr.quantity ? (
                            <span className="ml-1 font-sans text-[10px] text-muted">({fmtNum(tr.lots, 0)}份)</span>
                          ) : null}
                        </td>
                        <td data-label="风险额" className="num">
                          {fmtMoney(tr.risk_amount, pos?.account_currency)}
                        </td>
                        <td data-label="执行纪律" className="text-muted">
                          {tr.rule}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* 3-Stage Offensive Campaign Blueprint */}
      {plan && offensiveCampaign ? (
        <div
          className={`flex flex-col gap-3 border-t border-hairline pt-3.5 ${
            planStale && !openedReceipt ? "opacity-60" : ""
          }`}
          aria-label="三阶段进攻推仓蓝图"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Rocket className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
              <span className="section-title">三阶段进攻推仓蓝图（零新增本金风险推仓 + 日线主升持有）</span>
            </div>
            <div className="tnum flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted">
              <span>标准战役 1R = {fmtMoney(offensiveCampaign.campaign_1r_amount, pos?.account_currency)}</span>
              <span>·</span>
              <span>单位 1R 距 = {fmtPrice(offensiveCampaign.unit_1r_price_distance)}</span>
            </div>
          </div>

          <div className="metric-strip grid-cols-2 sm:grid-cols-4 lg:grid-cols-7">
            <div className="metric-cell">
              <span className="metric-label">满额 1R 首仓</span>
              <span className="metric-val text-accent">
                {fmtNum(offensiveCampaign.full_pilot_qty, planFractional ? 4 : 0)} {planUnitLabel}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">保守 40% 首仓</span>
              <span className="metric-val text-ink-2">
                {fmtNum(offensiveCampaign.split_pilot_qty, planFractional ? 4 : 0)} {planUnitLabel}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">+1.0R 保本移损价</span>
              <span className="metric-val text-bull">
                {fmtPrice(offensiveCampaign.breakeven_trigger_price)}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">+1.5R 最大零险推仓</span>
              <span className="metric-val text-bull">
                +{fmtNum(offensiveCampaign.max_zero_risk_add_qty, planFractional ? 4 : 0)} {planUnitLabel}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">推仓后综合保本位</span>
              <span className="metric-val">
                {fmtPrice(offensiveCampaign.post_add_breakeven_stop)}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">+2.0R 主升移交位</span>
              <span className="metric-val text-accent">
                {fmtPrice(offensiveCampaign.main_wave_trigger_price)}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">初始日线保本防线</span>
              <span className="metric-val">
                {fmtPrice(offensiveCampaign.initial_main_wave_stop)}
              </span>
            </div>
          </div>

          {Array.isArray(offensiveCampaign.stages) && offensiveCampaign.stages.length > 0 ? (
            <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
              {offensiveCampaign.stages.map((st, idx) => {
                const isPilot = st.stage === "pilot";
                const isPyramid = st.stage === "breakeven_pyramid";
                const stageAccent = isPilot
                  ? "border-t-2 border-t-accent"
                  : isPyramid
                    ? "border-t-2 border-t-bull"
                    : "border-t-2 border-t-good";
                return (
                  <div
                    key={st.stage || idx}
                    className={`flex flex-col gap-1.5 rounded-md border border-hairline bg-sunken/60 p-3 text-xs ${stageAccent}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-semibold text-ink">{st.title}</span>
                      <StatusBadge
                        tone={st.principal_risk_amount <= 0 ? "good" : "info"}
                        label={
                          st.principal_risk_amount <= 0
                            ? "零本金风险"
                            : `风险 ${fmtMoney(st.principal_risk_amount, pos?.account_currency)}`
                        }
                      />
                    </div>
                    <div className="tnum flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-2">
                      <span>
                        触发：<strong className="text-ink">{fmtPrice(st.trigger_price)}</strong>
                        {st.trigger_r > 0 ? ` (+${st.trigger_r.toFixed(1)}R)` : " (0R)"}
                      </span>
                      <span>
                        数量：<strong className="text-ink">
                          {isPyramid ? "+" : ""}
                          {fmtNum(st.recommended_qty, planFractional ? 4 : 0)} {planUnitLabel}
                        </strong>
                      </span>
                      <span>
                        防线：<strong className="text-bear">{fmtPrice(st.stop_price)}</strong>
                      </span>
                    </div>
                    <p className="m-0 text-[11px] leading-relaxed text-muted">{st.rule_summary}</p>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
