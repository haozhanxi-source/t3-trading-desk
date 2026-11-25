const nf = (min: number, max: number) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });

const cache = new Map<string, Intl.NumberFormat>();
function fmtr(min: number, max: number): Intl.NumberFormat {
  const k = `${min}:${max}`;
  let f = cache.get(k);
  if (!f) {
    f = nf(min, max);
    cache.set(k, f);
  }
  return f;
}

export function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export function toNum(v: unknown): number | null {
  if (isNum(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** Price with adaptive precision (FX 4–5dp, futures/stocks 2dp). */
export function fmtPrice(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  const dp = a === 0 ? 2 : a < 2 ? 4 : a < 20 ? 3 : 2;
  return fmtr(dp, dp).format(n);
}

export function fmtNum(v: unknown, dp = 2): string {
  const n = toNum(v);
  return n === null ? "—" : fmtr(0, dp).format(n);
}

export function fmtFixed(v: unknown, dp = 2, signed = false): string {
  const n = toNum(v);
  if (n === null) return "—";
  if (!signed) return fmtr(dp, dp).format(n);
  const absFormatted = fmtr(dp, dp).format(Math.abs(n));
  if (!/[1-9]/.test(absFormatted)) return `+${absFormatted}`;
  return `${n < 0 ? "-" : "+"}${absFormatted}`;
}

export function fmtPct(v: unknown, dp = 2, signed = false): string {
  const n = toNum(v);
  if (n === null) return "—";
  const s = fmtr(dp, dp).format(n);
  return `${signed && n > 0 ? "+" : ""}${s}%`;
}

export function fmtMoney(v: unknown, ccy?: string): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  const body = a >= 1e9 ? `${fmtr(2, 2).format(n / 1e9)}B` : a >= 1e6 ? `${fmtr(2, 2).format(n / 1e6)}M` : fmtr(0, 2).format(n);
  return ccy ? `${body} ${ccy}` : body;
}

export function fmtCompact(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  if (a >= 1e12) return `${fmtr(2, 2).format(n / 1e12)}T`;
  if (a >= 1e9) return `${fmtr(2, 2).format(n / 1e9)}B`;
  if (a >= 1e6) return `${fmtr(2, 2).format(n / 1e6)}M`;
  if (a >= 1e3) return `${fmtr(1, 1).format(n / 1e3)}K`;
  return fmtr(0, 2).format(n);
}

export function fmtRatio(v: unknown): string {
  const n = toNum(v);
  return n === null ? "—" : `${fmtr(2, 2).format(n)}R`;
}

export function dirLabel(d: unknown): string {
  if (d === "long" || d === "up") return "多";
  if (d === "short" || d === "down") return "空";
  if (d === "neutral") return "中性";
  return d ? String(d) : "—";
}

export function shortTime(s: string | null | undefined): string {
  if (!s) return "—";
  const cleaned = s
    .trim()
    .replace(/(\d{4}-\d{2}-\d{2})T/, "$1 ")
    .replace(/\s*UTC$/i, "")
    .replace(/(Z|[+-]\d{2}:?\d{2})$/i, "")
    .replace(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\d{2}(\.\d+)?$/, "$1")
    .trim();
  return cleaned || "—";
}

const ALIGNMENT_ZH: Record<string, string> = {
  aligned_trigger: "区间套同向共振",
  daily_opposes_direction: "日线反向·禁逆势",
  daily_ok_wait_30m: "日线顺向·等小级别触发",
  neutral_wait: "中枢震荡·观望",
  data_quality_degraded: "数据质量降级·禁自动放行",
};

export function fmtAlignmentStatus(raw: string | null | undefined): string {
  if (!raw) return "";
  return ALIGNMENT_ZH[raw.trim()] ?? raw;
}

export function fmtPosVsZs(raw: string | null | undefined): string {
  if (!raw) return "—";
  const s = raw.trim();
  if (s === "no_zhongshu") return "未成中枢";
  const above = /^above_zg\(([^)]+)\)$/i.exec(s);
  if (above) return `中枢上方 (${above[1]})`;
  const below = /^below_zd\(([^)]+)\)$/i.exec(s);
  if (below) return `中枢下方 (${below[1]})`;
  const inside = /^inside_zs\(([^)]+)\)$/i.exec(s);
  if (inside) return `中枢内部 (${inside[1]})`;
  return s;
}

const MOMENTUM_VERDICT_ZH: Record<string, string> = {
  capital_consensus_up: "多头资金一致",
  capital_consensus_down: "空头资金一致",
  divergence_candidate_up: "上涨背驰观察",
  divergence_candidate_down: "下跌背驰观察",
  non_divergence_up: "急拉非背驰",
  non_divergence_down: "急跌非背驰",
  higher_low_pullback: "次低点回踩",
  lower_high_rebound: "次高点反抽",
  neutral_extension_up: "向上延伸",
  neutral_extension_down: "向下延伸",
};

export function fmtMomentumVerdict(raw: string | null | undefined): string {
  if (!raw) return "";
  const s = raw.trim();
  return MOMENTUM_VERDICT_ZH[s] ?? s;
}

export function fmtFractalText(raw: string | null | undefined): string {
  if (!raw) return "—";
  return raw
    .replace(/\/strong\b/gi, "·强")
    .replace(/\/standard\b/gi, "·标准")
    .replace(/\/weak\b/gi, "·弱")
    .replace(/\bstrong\b/gi, "强")
    .replace(/\bstandard\b/gi, "标准")
    .replace(/\bweak\b/gi, "弱");
}

/** Clean input string for prefilled chart/snapshot prices (no 16-digit IEEE-754 float tails). */
export function fmtInputPrice(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "";
  const a = Math.abs(n);
  const dp = a === 0 ? 2 : a < 2 ? 4 : a < 20 ? 3 : 2;
  return String(Number(n.toFixed(dp)));
}

/** Preserve up to 6dp from backend computed plans while stripping IEEE-754 binary float noise. */
export function fmtPlanNum(v: unknown, maxDp = 6): string {
  const n = toNum(v);
  if (n === null) return "";
  return String(Number(n.toFixed(maxDp)));
}

/** Infer asset quote currency for cross-currency fx_to_account hints. */
export function inferAssetCurrency(symbol: string | null | undefined, assetClass?: string | null): string | null {
  const sym = (symbol || "").trim().toUpperCase();
  const ac = (assetClass || "").trim().toLowerCase();
  if (!sym) return null;
  if (ac === "a_share" || ac === "cn_equity" || /^\d{6}(\.(SS|SZ|BJ))?$/.test(sym)) {
    return "CNY";
  }
  if (ac === "cn_future" || sym.startsWith("CN:") || /^[A-Z]{1,2}0$/.test(sym)) {
    return "CNY";
  }
  if (sym.endsWith(".HK")) {
    return "HKD";
  }
  const fxMatch = /^([A-Z]{3})([A-Z]{3})=X$/.exec(sym);
  if (fxMatch) {
    return fxMatch[2] ?? null;
  }
  if (
    sym === "DX-Y.NYB" ||
    ac === "us_equity" ||
    ac === "us_future" ||
    ac === "commodity_us" ||
    ac === "crypto" ||
    /^[A-Z]{1,5}(=F)?$/.test(sym)
  ) {
    return "USD";
  }
  return null;
}

export function parseSymbolList(s: string): string[] {
  return Array.from(
    new Set(
      s
        .split(/[\s,，;；、]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  );
}

export function optNum(s: string): number | undefined {
  if (s.trim() === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

export const TRIGGER_KIND_ZH: Record<string, string> = {
  chanlun: "缠论结构触发器",
  supply_demand: "供需库存触发器",
  term_structure: "期限基差触发器",
  rate_diff: "国债利差触发器",
  fundamental: "基本面触发器",
  policy: "产业/资源政策触发器",
  spread: "跨市价差触发器",
  freight: "海运指数触发器",
  macro: "宏观双核触发器",
  unspecified: "自定义触发器",
};

export function fmtTriggerKind(raw: string | null | undefined): string {
  const key = (raw || "").trim().toLowerCase();
  if (!key) return "自定义触发器";
  return TRIGGER_KIND_ZH[key] ?? raw!.trim();
}

export const ASSET_CLASS_ZH: Record<string, string> = {
  us_stock_or_etf: "美股/ETF",
  us_equity: "美股/ETF",
  a_share: "A股",
  cn_equity: "A股",
  cn_future: "国内期货",
  commodity: "海外大宗商品",
  commodity_us: "海外大宗商品",
  us_future: "海外期货",
  fx: "外汇",
  crypto: "加密货币",
  macro: "宏观指标",
};

export function fmtAssetClass(raw: string | null | undefined): string {
  const key = (raw || "").trim().toLowerCase();
  if (!key) return "—";
  return ASSET_CLASS_ZH[key] ?? raw!.trim();
}

export function fmtDirectionBias(raw: string | null | undefined): string {
  const key = (raw || "").trim().toLowerCase();
  if (key === "long" || key === "up" || key === "bullish") return "做多";
  if (key === "short" || key === "down" || key === "bearish") return "做空";
  if (key === "pair") return "多空配对";
  if (key === "neutral" || !key) return "中性观察";
  return raw!.trim();
}

export function fmtLeaderRole(
  rawRole: string | null | undefined,
  count?: number,
  maxSlots?: number,
): string {
  const norm = (rawRole || "sole_leader").trim().toLowerCase();
  const hasSlotCounts =
    typeof count === "number" &&
    Number.isFinite(count) &&
    typeof maxSlots === "number" &&
    Number.isFinite(maxSlots) &&
    maxSlots > 0;

  if (norm.includes("co_leader")) {
    return hasSlotCounts ? `双寡头并行（${count}/${maxSlots} 席）` : "双寡头并行";
  }
  if (norm === "sole_leader" || !norm) {
    if (hasSlotCounts) {
      return maxSlots > 1
        ? `单一龙头独占（${count} 只，双寡头上限 ${maxSlots}）`
        : `单一龙头独占（已占 ${count} 席）`;
    }
    return "单一龙头独占";
  }
  return hasSlotCounts ? `${rawRole}（${count}/${maxSlots}）` : String(rawRole);
}

const EVENT_SOURCE_ZH: Record<string, string> = {
  archive_state: "研报事件日历",
  archive: "研报事件日历",
  yfinance: "市场财报日历",
  earnings: "市场财报日历",
  futures_rollover: "主力移仓推算",
  rollover: "主力移仓推算",
};

export function fmtEventSource(raw: string | null | undefined): string {
  const key = (raw || "").trim().toLowerCase();
  if (!key) return "";
  return EVENT_SOURCE_ZH[key] ?? raw!.trim();
}

const TRIGGER_ACTION_TEXT_ZH: Record<string, string> = {
  exit_review: "果断撤离重审",
  trim_or_exit: "减仓或撤离",
  alert: "指标松动预警",
};

const SENTINEL_PRECOND_ZH: Record<string, string> = {
  all_armed_triggers_evaluated: "全部撤离触发器已完成核验",
  governance_satisfied: "已绑定有效撤离红线",
  market_fresh: "行情数据新鲜有效",
  chanlun_valid: "日线/30m 缠论结构完整",
  no_stop_breach: "未触及止损防线",
  data_integrity_ok: "数据源完整性校验通过",
  direction_known: "持仓方向明确",
  stops_configured: "已配置有效止损位",
};

const SENTINEL_METRIC_HINT_ZH: Record<string, string> = {
  close: "最新价",
  contract_close: "合约最新价",
  continuous_close: "主连最新价",
  daily_zd: "日线中枢下沿 ZD",
  daily_zg: "日线中枢上沿 ZG",
  intraday_30m_zd: "30m 中枢下沿 ZD",
  intraday_30m_zg: "30m 中枢上沿 ZG",
  daily_signal: "日线缠论信号",
  intraday_30m_signal: "30m 缠论信号",
  basis_pct: "现货基差率",
  inventory_chg_pct: "显性库存环比",
  inventory_latest: "最新显性库存",
  roll_yield_near_pct: "近月年化展期收益",
  roll_yield_curve_pct: "全曲线年化展期收益",
  rate_diff_10y_bp: "10Y 国债利差",
  rate_diff_2y_bp: "2Y 国债利差",
  forward_pe: "前瞻 PE",
  pe_ttm_pctile_5y: "5 年 PE 分位",
  pb_pctile_5y: "5 年 PB 分位",
  short_pct_float: "空头占流通股比",
};

/** Clean raw backend debug tokens & internal enums in sentinel alerts/notes into readable Chinese. */
export function cleanSentinelText(raw: string | null | undefined): string {
  if (!raw) return "";
  let text = String(raw).trim();
  if (!text) return "";

  /* 1. Governance / trigger configuration debug strings */
  text = text.replace(
    /state\.json\s+armed_triggers=0\s*且\s*position\.exit_red_lines=0/gi,
    "研报档案尚未配置自动撤离红线触发器，且持仓未登记人工红线",
  );
  text = text.replace(
    /exit_red_lines=(\d+)条\s*,\s*armed_triggers=0/gi,
    "已登记 $1 条人工文本红线，研报档案尚未配置自动撤离触发器",
  );
  text = text.replace(
    /(?:state\.json\s+)?armed_triggers=0/gi,
    "研报档案尚未配置自动撤离红线触发器",
  );
  text = text.replace(/position\.exit_red_lines=0/gi, "持仓未登记人工红线");
  text = text.replace(/exit_red_lines=(\d+)条/gi, "已登记 $1 条人工文本红线");
  text = text.replace(/allow_manual_red_lines=True/gi, "允许人工红线放行");
  text = text.replace(
    /tactical_stop_30m=None\s*,\s*structural_stop_daily=None/gi,
    "30m 战术止损位与日线结构失效位均未设置",
  );

  /* 2. Bracketed trigger tags like [trig_daily_zd_breakdown_confirm/chanlun] -> [缠论结构] */
  text = text.replace(
    /\[(?:trig_[a-zA-Z0-9_-]+\/([a-zA-Z0-9_-]+)|[a-zA-Z0-9_-]+\/(chanlun|term_structure|freight|spread|rate_diff|fundamental|supply_demand|policy|macro|event|price|manual|ai_web))\]/gi,
    (_m, trigKind: string | undefined, knownKind: string | undefined) => {
      return `[${fmtTriggerKind(trigKind ?? knownKind ?? "")}]`;
    },
  );

  /* 3. Unevaluated trigger raw entries like trig_xxx(metric: reason) */
  text = text.replace(/\btrig_[a-zA-Z0-9_]+\(([^)]+)\)/g, (_m, inner: string) => {
    const colonIdx = inner.indexOf(":");
    if (colonIdx > 0) {
      const mKey = inner.slice(0, colonIdx).trim();
      const rest = inner.slice(colonIdx + 1).trim();
      const mZh = SENTINEL_METRIC_HINT_ZH[mKey.toLowerCase()] ?? mKey;
      return `${mZh}（${rest}）`;
    }
    return inner.trim();
  });

  /* 4. Trigger action slugs */
  text = text.replace(
    /(->|→|：|:)\s*(exit_review|trim_or_exit|alert)\b/gi,
    (_m, sep: string, act: string) => {
      const zh = TRIGGER_ACTION_TEXT_ZH[act.toLowerCase()] ?? act;
      const cleanSep = sep === "->" ? " → " : sep === "→" ? " → " : sep;
      return `${cleanSep}${zh}`;
    },
  );

  /* 5. Common key=value debug fragments */
  text = text.replace(/\bbar_is_closed=False\b/gi, "最新 K 线尚未收盘");
  text = text.replace(/\bdirection=(?:'neutral'|"neutral"|None)\b/gi, "持仓方向未明确");
  text = text.replace(/\bas_of_bar=/gi, "最新K线时间 ");
  text = text.replace(/\bref_date=/gi, "参考日期 ");
  text = text.replace(/\bbase_symbol=/gi, "主连标的 ");
  text = text.replace(/\bmarket_snapshot:\s*status=error\b/gi, "行情快照抓取失败");

  /* 6. Known metric keys in `metric=val` pairs */
  text = text.replace(
    /\b([a-z][a-z0-9_]{2,})=/g,
    (full, key: string) => {
      const zh = SENTINEL_METRIC_HINT_ZH[key.toLowerCase()];
      return zh ? `${zh} ` : full;
    },
  );

  /* 7. Precondition failure keys & raw English enums */
  for (const [k, zh] of Object.entries(SENTINEL_PRECOND_ZH)) {
    text = text.replace(new RegExp(`\\b${k}\\b`, "g"), zh);
  }
  for (const [k, zh] of Object.entries(ASSET_CLASS_ZH)) {
    if (k.includes("_")) {
      text = text.replace(new RegExp(`\\b${k}\\b`, "gi"), zh);
    }
  }
  text = text.replace(/\bsole_leader\b/gi, "单一龙头独占");
  text = text.replace(/\bco_leader\b/gi, "双寡头并行");
  text = text.replace(/\bneutral\b/gi, "中性观察");

  return text;
}
