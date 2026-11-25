import { useMemo, useState } from "react";
import { ExternalLink, RefreshCw, Table2 } from "lucide-react";
import type { Dict, ResearchDataResponse } from "../../api/types";
import { fmtCompact, fmtFixed, fmtNum, fmtPct, fmtPrice, shortTime, toNum } from "../../lib/format";
import { formatSymbolWithCode } from "../../lib/symbols";
import { useTheme } from "../../lib/theme";
import { SourceStatusBadge, StatusBadge } from "../ui/StatusBadge";

interface HardDataVisualsProps {
  symbol: string;
  data: ResearchDataResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

function asObj(v: unknown): Dict | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Dict) : null;
}

function asArr(v: unknown): Dict[] {
  return Array.isArray(v) ? (v.filter((x) => x && typeof x === "object") as Dict[]) : [];
}

const KEY_ZH_LABELS: Record<string, string> = {
  status: "状态",
  source: "数据源",
  date: "日期",
  report_date: "报告期",
  updated_at: "更新时间",
  spot_price: "现货报价",
  futures_price: "期货主力价",
  main_price: "主力合约价",
  basis: "现货基差",
  basis_pct: "基差率 (%)",
  structure: "基差结构",
  latest_inventory: "最新显性库存",
  inventory: "库存量",
  stock: "库存",
  unit: "单位",
  change: "环比变动",
  change_pct: "环比变动 (%)",
  yoy_pct: "同比变动 (%)",
  domestic_symbol: "内盘合约",
  domestic_price: "内盘价格",
  foreign_symbol: "外盘合约",
  foreign_price: "外盘价格",
  fx_usdcny: "美元兑人民币",
  raw_ratio: "内外比价",
  import_parity: "进口折算成本",
  import_margin_pct: "进口盈亏率 (%)",
  spread: "价差",
  bdi: "BDI 干散货指数",
  bci: "BCI 好望角型",
  bsi: "BSI 超灵便型",
  bdti: "BDTI 原油运输",
  bcti: "BCTI 成品油运输",
  latest_value: "最新值",
  prev_value: "前值",
  series_id: "序列代码",
  dfii10: "10Y 美债实际利率 (%)",
  dgs10: "10Y 美债名义收益率 (%)",
  t10yie: "10Y 盈亏平衡通胀 (%)",
  fedfunds: "联邦基金有效利率 (%)",
  m2sl: "美国 M2 货币供应",
  pcoppusdm: "IMF 全球铜基准价",
  manufacturing_pmi: "中国官方制造业 PMI",
  caixin_pmi: "财新制造业 PMI",
  exports_yoy_pct: "出口同比 (%)",
  imports_yoy_pct: "进口同比 (%)",
  stablecoin_mcap_usd: "稳定币总市值 (USD)",
  open_interest: "未平仓合约 (OI)",
  funding_rate_annualized_pct: "永续年化资金费率 (%)",
  net_flow_usd_m: "现货 ETF 净流入 ($M)",
};

const MODULE_ZH_TITLES: Record<string, string> = {
  cftc_disagg_cot: "CFTC 商品生产商与管理基金持仓",
  eia_weekly: "EIA 周度库存与产量",
  usda_wasde: "USDA WASDE 供需平衡表",
  nbs_monthly: "国家统计局月度产量与投资",
  defillama_stablecoins: "DefiLlama 稳定币流动性",
  crypto_derivatives: "加密永续 OI 与资金费率",
  farside_etf_flows: "Farside 现货 ETF 每日净流量",
  northbound_flow: "沪深港通资金流向",
  ths_financial_abstract: "同花顺核心财务摘要",
};

function humanizeKey(raw: string): string {
  const lower = raw.toLowerCase();
  if (KEY_ZH_LABELS[lower]) return KEY_ZH_LABELS[lower];
  return raw.replace(/_/g, " ");
}

function formatScalarValue(key: string, val: unknown): string {
  if (val === null || val === undefined || val === "") return "—";
  if (typeof val === "boolean") return val ? "是" : "否";
  if (typeof val === "number") {
    if (!Number.isFinite(val)) return "—";
    const lk = key.toLowerCase();
    if (lk.includes("pct") || lk.includes("percent") || lk.includes("rate") || lk.includes("yield")) {
      return `${val.toFixed(2)}%`;
    }
    if (Math.abs(val) >= 1_000_000) return fmtCompact(val);
    if (Number.isInteger(val)) return fmtNum(val, 0);
    return Math.abs(val) < 10 ? val.toFixed(4).replace(/\.?0+$/, "") : fmtFixed(val, 2);
  }
  if (Array.isArray(val)) {
    if (val.length === 0) return "—";
    if (val.every((x) => typeof x !== "object")) return val.slice(0, 6).map(String).join(", ");
    return `${val.length} 条记录`;
  }
  return String(val);
}

/* ------------------------------------------------------------------
 * Structured Key-Value & Mini-Table Card (eliminates raw <pre> JSON)
 * ------------------------------------------------------------------ */
function StructuredDataCard({ title, raw }: { title: string; raw: unknown }) {
  const obj = asObj(raw);
  if (!obj) {
    return (
      <div className="card p-3.5 text-xs">
        <div className="section-title mb-2">{title}</div>
        <div className="font-mono text-ink-2">{formatScalarValue(title, raw)}</div>
      </div>
    );
  }

  const errText = typeof obj.error === "string" ? obj.error : null;
  const scalarPairs: { key: string; label: string; value: string }[] = [];
  const nestedObjects: { key: string; label: string; data: Dict }[] = [];
  const arrayTables: { key: string; label: string; rows: Dict[] }[] = [];

  for (const [k, v] of Object.entries(obj)) {
    if (k === "status" && v === "ok") continue;
    if (k === "error") continue;
    if (Array.isArray(v)) {
      const objRows = asArr(v);
      if (objRows.length > 0) {
        arrayTables.push({ key: k, label: humanizeKey(k), rows: objRows.slice(0, 8) });
      } else if (v.length > 0) {
        scalarPairs.push({ key: k, label: humanizeKey(k), value: formatScalarValue(k, v) });
      }
      continue;
    }
    const sub = asObj(v);
    if (sub) {
      /* Unwrap multi-series dictionary such as us_fred.series.{DFII10, DGS10, ...} */
      if (k === "series") {
        for (const [sid, sVal] of Object.entries(sub)) {
          const sObj = asObj(sVal);
          if (!sObj) continue;
          const lbl = String(sObj.label || humanizeKey(sid));
          if (sObj.status === "error" || typeof sObj.error === "string") {
            scalarPairs.push({
              key: `series-${sid}`,
              label: lbl,
              value: `源故障: ${String(sObj.error || "不可用").slice(0, 36)}`,
            });
          } else {
            const numPart = formatScalarValue(sid, sObj.latest_value ?? sObj.value ?? sObj.last);
            const unitPart = typeof sObj.unit === "string" && !numPart.endsWith("%") ? ` ${sObj.unit}` : "";
            const dtPart = sObj.date ?? sObj.period ?? "";
            scalarPairs.push({
              key: `series-${sid}`,
              label: lbl,
              value: `${numPart}${unitPart}${dtPart ? ` (${String(dtPart)})` : ""}`,
            });
          }
        }
        continue;
      }

      /* Flatten nested module with records[] or error (e.g. china_macro.pmi / trade) */
      const subRecords = asArr(sub.records ?? sub.items ?? sub.rows);
      if (subRecords.length > 0) {
        arrayTables.push({ key: k, label: humanizeKey(k), rows: subRecords.slice(0, 8) });
        continue;
      }
      if (sub.status === "error" || typeof sub.error === "string") {
        scalarPairs.push({
          key: k,
          label: humanizeKey(k),
          value: `源提示: ${String(sub.error || "暂不可用").slice(0, 42)}`,
        });
        continue;
      }

      /* Flatten simple latest-point series objects like { latest_value, date } */
      if ("latest_value" in sub || "value" in sub || "last" in sub) {
        const numPart = formatScalarValue(k, sub.latest_value ?? sub.value ?? sub.last);
        const dtPart = sub.date ?? sub.period ?? sub.end ?? "";
        scalarPairs.push({
          key: k,
          label: humanizeKey(String(sub.label ?? sub.name ?? k)),
          value: dtPart ? `${numPart} (${String(dtPart)})` : numPart,
        });
      } else {
        nestedObjects.push({ key: k, label: humanizeKey(k), data: sub });
      }
      continue;
    }
    if (v !== null && v !== undefined && v !== "") {
      scalarPairs.push({ key: k, label: humanizeKey(k), value: formatScalarValue(k, v) });
    }
  }

  return (
    <div className="card flex flex-col gap-2.5 p-3.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="section-title">{title}</span>
        {typeof obj.status === "string" && obj.status !== "ok" ? (
          <SourceStatusBadge status={obj.status} />
        ) : null}
      </div>

      {errText ? (
        <div className="rounded border border-warning/35 bg-warning/10 px-2.5 py-1.5 text-ink-2">
          数据源提示：{errText}
        </div>
      ) : null}

      {scalarPairs.length > 0 ? (
        <dl className="m-0 grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2">
          {scalarPairs.map((item) => (
            <div
              key={item.key}
              className="flex items-baseline justify-between gap-2 border-b border-hairline-soft pb-1"
            >
              <dt className="truncate text-muted">{item.label}</dt>
              <dd className="tnum m-0 shrink-0 font-mono font-medium text-ink">{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {nestedObjects.map((sub) => {
        const entries = Object.entries(sub.data).filter(([, sv]) => sv !== null && sv !== undefined && typeof sv !== "object");
        if (entries.length === 0) return null;
        return (
          <div key={sub.key} className="pt-1">
            <div className="mb-1 text-[11px] font-semibold text-ink-2">{sub.label}</div>
            <dl className="m-0 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
              {entries.slice(0, 8).map(([sk, sv]) => (
                <div
                  key={sk}
                  className="flex items-baseline justify-between gap-2 border-b border-hairline-soft pb-1"
                >
                  <dt className="truncate text-muted">{humanizeKey(sk)}</dt>
                  <dd className="tnum m-0 shrink-0 font-mono text-ink">{formatScalarValue(sk, sv)}</dd>
                </div>
              ))}
            </dl>
          </div>
        );
      })}

      {arrayTables.map((tbl) => {
        const cols = Array.from(
          new Set(tbl.rows.flatMap((r) => Object.keys(r).filter((ck) => typeof r[ck] !== "object"))),
        ).slice(0, 5);
        if (cols.length === 0) return null;
        return (
          <div key={tbl.key} className="table-wrapper max-h-48">
            <table className="data-table">
              <thead>
                <tr>
                  {cols.map((c) => (
                    <th key={c} scope="col">
                      {humanizeKey(c)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tbl.rows.map((r, rIdx) => (
                  <tr key={rIdx}>
                    {cols.map((c, cIdx) =>
                      cIdx === 0 ? (
                        <th key={c} scope="row">
                          {formatScalarValue(c, r[c])}
                        </th>
                      ) : (
                        <td key={c} data-label={humanizeKey(c)} className={typeof r[c] === "number" ? "num" : ""}>
                          {formatScalarValue(c, r[c])}
                        </td>
                      ),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}

      {scalarPairs.length === 0 && nestedObjects.length === 0 && arrayTables.length === 0 && !errText ? (
        <div className="text-muted">暂无有效结构化字段</div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------
 * 1. Futures Term Structure Curve (Single Y-axis SVG + Hover + Table)
 * ------------------------------------------------------------------ */
function FuturesCurveCard({ raw }: { raw: Dict }) {
  const { palette } = useTheme();
  const [showTable, setShowTable] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const points = useMemo(() => {
    const arr = asArr(raw.contracts ?? raw.points);
    return arr
      .map((c) => {
        const code = String(c.contract ?? c.symbol ?? "");
        const month = String(c.expiry_ym ?? c.month_code ?? code);
        const price = toNum(c.price);
        const volume = toNum(c.volume);
        const oi = toNum(c.open_interest);
        return price !== null && code ? { code, month, price, volume, oi } : null;
      })
      .filter((x): x is NonNullable<typeof x> => x !== null);
  }, [raw]);

  const structure = String(raw.curve_structure ?? raw.structure ?? "—");
  const nearRoll = toNum(raw.near_annualized_roll_yield_pct ?? raw.annualized_roll_yield_near_pct ?? raw.annualized_roll_yield_pct);
  const curveRoll = toNum(raw.annualized_roll_yield_curve_pct ?? raw.annualized_roll_yield_pct);
  const mismatchWarn = typeof raw.quote_date_mismatch_warning === "string" ? raw.quote_date_mismatch_warning : null;

  const W = 620;
  const H = 205;
  const pad = { l: 58, r: 24, t: 20, b: 32 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const minP = points.length > 0 ? Math.min(...points.map((p) => p.price)) : 0;
  const maxP = points.length > 0 ? Math.max(...points.map((p) => p.price)) : 1;
  const span = maxP - minP || Math.max(1, maxP * 0.02);
  const yLow = minP - span * 0.14;
  const yHigh = maxP + span * 0.16;

  const xAt = (i: number) => (points.length <= 1 ? pad.l + plotW / 2 : pad.l + (i / (points.length - 1)) * plotW);
  const yAt = (v: number) => pad.t + (1 - (v - yLow) / (yHigh - yLow)) * plotH;

  const pathD = points.map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(p.price).toFixed(1)}`).join(" ");
  const activePt = hoverIdx !== null ? points[hoverIdx] : null;

  return (
    <div className="card flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="m-0 text-sm font-semibold text-ink">期货活跃合约期限结构曲线</h3>
          <StatusBadge
            tone={structure.toLowerCase().includes("backwardation") ? "warning" : "info"}
            label={structure}
          />
        </div>
        <button type="button" className="chip" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
          <Table2 className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{showTable ? "返回曲线" : "合约表"}</span>
        </button>
      </div>

      {/* Hairline Roll Yield Strip */}
      <div className="metric-strip grid-cols-2 sm:grid-cols-4">
        <div className="metric-cell">
          <span className="metric-label">近月合约</span>
          <span className="metric-val">
            {String(raw.front_contract ?? points[0]?.code ?? "—")} · {fmtPrice(raw.front_price ?? points[0]?.price)}
          </span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">远月合约</span>
          <span className="metric-val">
            {String(raw.far_contract ?? raw.back_contract ?? points[points.length - 1]?.code ?? "—")} ·{" "}
            {fmtPrice(raw.far_price ?? raw.back_price ?? points[points.length - 1]?.price)}
          </span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">近月年化展期收益</span>
          <span className="metric-val">{fmtPct(nearRoll, 2, true)}</span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">全曲线年化展期收益</span>
          <span className="metric-val">{fmtPct(curveRoll, 2, true)}</span>
        </div>
      </div>

      {mismatchWarn ? (
        <div className="rounded border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-ink">
          {mismatchWarn}
        </div>
      ) : null}

      {points.length === 0 ? (
        <div className="py-6 text-center text-xs text-muted">暂无可绘制的期限曲线点位</div>
      ) : showTable ? (
        <div tabIndex={0} role="region" aria-label="期货期限结构各合约报价表" className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">合约代码</th>
                <th scope="col">交割月份</th>
                <th scope="col" className="num">
                  报价
                </th>
                <th scope="col" className="num">
                  成交量
                </th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.code}>
                  <th scope="row">{p.code}</th>
                  <td data-label="交割月份">{p.month}</td>
                  <td data-label="报价" className="num">
                    {fmtPrice(p.price)}
                  </td>
                  <td data-label="成交量" className="num">
                    {p.volume != null ? fmtCompact(p.volume) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="relative">
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="w-full select-none overflow-visible"
            role="img"
            aria-label="期货期限结构价格曲线图"
            onMouseLeave={() => setHoverIdx(null)}
          >
            {[0, 0.5, 1].map((t) => {
              const val = yLow + t * (yHigh - yLow);
              const y = yAt(val);
              return (
                <g key={t}>
                  <line x1={pad.l} x2={W - pad.r} y1={y} y2={y} stroke={palette.gridline} strokeDasharray="3 3" />
                  <text
                    x={pad.l - 8}
                    y={y + 4}
                    textAnchor="end"
                    fontSize="10.5"
                    fill={palette.muted}
                    style={{ fontVariantNumeric: "tabular-nums", fontFamily: "var(--font-mono)" }}
                  >
                    {fmtPrice(val)}
                  </text>
                </g>
              );
            })}

            <path d={pathD} fill="none" stroke={palette.accent} strokeWidth={2} />

            {hoverIdx !== null && points[hoverIdx] ? (
              <line
                x1={xAt(hoverIdx)}
                x2={xAt(hoverIdx)}
                y1={pad.t}
                y2={H - pad.b}
                stroke={palette.muted}
                strokeDasharray="3 3"
              />
            ) : null}

            {points.map((p, i) => {
              const cx = xAt(i);
              const cy = yAt(p.price);
              const showTick =
                points.length <= 8 || i === 0 || i === points.length - 1 || i % Math.ceil(points.length / 6) === 0;
              return (
                <g key={p.code}>
                  <circle
                    cx={cx}
                    cy={cy}
                    r={hoverIdx === i ? 6 : 4}
                    fill={palette.accent}
                    stroke={palette.surface}
                    strokeWidth={2}
                  />
                  {i === 0 || i === points.length - 1 ? (
                    <text
                      x={cx}
                      y={cy - 10}
                      textAnchor={i === 0 ? "start" : "end"}
                      fontSize="10.5"
                      fontWeight="600"
                      fill={palette.ink}
                      style={{ fontVariantNumeric: "tabular-nums", fontFamily: "var(--font-mono)" }}
                    >
                      {fmtPrice(p.price)}
                    </text>
                  ) : null}
                  {showTick ? (
                    <text
                      x={cx}
                      y={H - 8}
                      textAnchor="middle"
                      fontSize="10"
                      fill={palette.muted}
                      style={{ fontFamily: "var(--font-mono)" }}
                    >
                      {p.code}
                    </text>
                  ) : null}
                  <circle
                    cx={cx}
                    cy={cy}
                    r={16}
                    fill="transparent"
                    className="cursor-pointer"
                    onMouseEnter={() => setHoverIdx(i)}
                  />
                </g>
              );
            })}
          </svg>

          {activePt ? (
            <div className="tnum mt-1 flex flex-wrap items-center gap-3 rounded border border-hairline bg-sunken px-3 py-1 font-mono text-[11px] text-ink">
              <strong>{activePt.code}</strong>
              <span>报价 {fmtPrice(activePt.price)}</span>
              {activePt.volume != null ? <span>量 {fmtCompact(activePt.volume)}</span> : null}
              {activePt.oi != null ? <span>OI {fmtCompact(activePt.oi)}</span> : null}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------
 * 2. FX Sovereign Rate Differential & CFTC TFF Positioning Card
 * ------------------------------------------------------------------ */
function FxHardDataCard({ rateDiff, cot }: { rateDiff: Dict | null; cot: Dict | null }) {
  const { palette } = useTheme();
  const [showTable, setShowTable] = useState(false);
  const [hoverRow, setHoverRow] = useState<string | null>(null);

  const buckets = useMemo(() => {
    if (!cot) return [];
    const list: { key: string; label: string; net: number; pctOi: number | null; long?: number; short?: number }[] = [];
    const add = (key: string, label: string, b: unknown) => {
      const obj = asObj(b);
      const net = toNum(obj?.net);
      if (net === null) return;
      list.push({
        key,
        label,
        net,
        pctOi: toNum(obj?.net_pct_oi),
        long: toNum(obj?.long) ?? undefined,
        short: toNum(obj?.short) ?? undefined,
      });
    };
    add("dealer", "Dealer 交易商 (原始期货)", cot.dealer);
    add("asset_manager", "Asset Mgr 资管机构 (原始期货)", cot.asset_manager);
    add("leveraged_funds", "Lev Money 杠杆基金 (原始期货)", cot.leveraged_funds);
    const alignedLev = toNum(cot.pair_aligned_lev_money_net);
    if (alignedLev !== null) {
      list.push({
        key: "aligned_lev",
        label: "杠杆基金净头寸（已对齐货币对方向）",
        net: alignedLev,
        pctOi: toNum(cot.pair_aligned_lev_money_net_pct_oi),
      });
    }
    return list;
  }, [cot]);

  const maxAbs = Math.max(1, ...buckets.map((b) => Math.abs(b.net)));

  return (
    <div className="card flex flex-col gap-3.5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0 text-sm font-semibold text-ink">主权国债利差 & CFTC 金融期货持仓结构（TFF）</h3>
        {buckets.length > 0 ? (
          <button type="button" className="chip" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
            <Table2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{showTable ? "返回图形" : "COT 明细表"}</span>
          </button>
        ) : null}
      </div>

      {rateDiff ? (
        <div className="metric-strip grid-cols-2 sm:grid-cols-4">
          <div className="metric-cell">
            <span className="metric-label">10Y 利差 (Base − Quote)</span>
            <span className="metric-val">
              {rateDiff.base_minus_quote_10y_bp != null
                ? `${fmtFixed(rateDiff.base_minus_quote_10y_bp, 1)} bp`
                : fmtPct(rateDiff.base_minus_quote_10y_pct, 2, true)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">基准端收益率</span>
            <span className="metric-val">
              {fmtPct(asObj(rateDiff.base)?.latest_value ?? asObj(rateDiff.us10y)?.latest_value, 2)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">计价端收益率</span>
            <span className="metric-val">
              {fmtPct(asObj(rateDiff.quote)?.latest_value ?? asObj(rateDiff.cn10y)?.latest_value, 2)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">数据来源</span>
            <span className="metric-val font-sans text-xs text-ink-2">
              {String(rateDiff.formula || rateDiff.source || "FRED / 东财")}
            </span>
          </div>
        </div>
      ) : null}

      {cot?.direction_note ? (
        <div className="rounded border border-accent/30 bg-accent/8 px-3 py-1.5 text-xs text-ink">
          <strong>报价换算说明：</strong> {String(cot.direction_note)}（报告期：{String(cot.report_date ?? "—")} · 总 OI：{fmtCompact(cot.open_interest)}）
        </div>
      ) : null}

      {buckets.length === 0 ? null : showTable ? (
        <div tabIndex={0} role="region" aria-label="CFTC TFF 金融期货分类持仓明细表" className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">交易者类别</th>
                <th scope="col" className="num">
                  多头手数
                </th>
                <th scope="col" className="num">
                  空头手数
                </th>
                <th scope="col" className="num">
                  净头寸
                </th>
                <th scope="col" className="num">
                  占总 OI%
                </th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.key}>
                  <th scope="row">{b.label}</th>
                  <td data-label="多头手数" className="num">
                    {b.long != null ? fmtNum(b.long, 0) : "—"}
                  </td>
                  <td data-label="空头手数" className="num">
                    {b.short != null ? fmtNum(b.short, 0) : "—"}
                  </td>
                  <td data-label="净头寸" className="num">
                    {fmtNum(b.net, 0)}
                  </td>
                  <td data-label="占总 OI%" className="num">
                    {fmtPct(b.pctOi, 2, true)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="space-y-2">
          {buckets.map((b) => {
            const pctWidth = Math.min(50, (Math.abs(b.net) / maxAbs) * 48);
            const positive = b.net >= 0;
            const barColor = positive ? palette.bull : palette.bear;
            return (
              <div
                key={b.key}
                onMouseEnter={() => setHoverRow(b.key)}
                onMouseLeave={() => setHoverRow(null)}
                className="py-1"
              >
                <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                  <span className="font-medium text-ink">{b.label}</span>
                  <span className="tnum font-mono text-[11.5px] text-ink-2">
                    净头寸 <strong className={positive ? "text-bull" : "text-bear"}>{fmtNum(b.net, 0)}</strong>
                    {b.pctOi != null ? ` (${fmtPct(b.pctOi, 1, true)} OI)` : ""}
                  </span>
                </div>
                <div className="relative h-3.5 w-full rounded bg-sunken">
                  <div className="absolute inset-y-0 left-1/2 w-px bg-muted/60" aria-hidden="true" />
                  <div
                    style={{
                      width: `${pctWidth}%`,
                      left: positive ? "50%" : `${50 - pctWidth}%`,
                      backgroundColor: barColor,
                      borderRadius: positive ? "0 3px 3px 0" : "3px 0 0 3px",
                    }}
                    className="absolute inset-y-0.5"
                  />
                </div>
                {hoverRow === b.key && (b.long != null || b.short != null) ? (
                  <div className="tnum mt-1 font-mono text-[11px] text-muted">
                    多头 {fmtNum(b.long, 0)} 手 · 空头 {fmtNum(b.short, 0)} 手
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------
 * 3. Equity Valuation, Consensus & SEC EDGAR GAAP Card
 * ------------------------------------------------------------------ */
const SEC_GAAP_METRIC_LABELS: Record<string, string> = {
  revenue: "营业收入 (Revenue)",
  net_income: "净利润 (Net Income)",
  eps_diluted: "稀释 EPS (Diluted EPS)",
  operating_cash_flow: "经营现金流 (OCF)",
  capex: "资本开支 (CapEx)",
  remaining_performance_obligation: "剩余履约义务 (RPO)",
  cash_and_equivalents: "现金及等价物 (Cash)",
  short_term_investments: "短期投资 (ST Inv)",
  long_term_debt: "长期债务 (LT Debt)",
  short_term_debt: "短期/当期债务 (ST Debt)",
  stock_based_compensation: "股权激励费用 (SBC)",
  share_repurchases: "实际股票回购 (Buybacks)",
  shares_outstanding_ending: "期末流通股数 (Ending Shares)",
  shares_diluted_weighted: "稀释加权股数 (Diluted Shares)",
  inventory: "存货 (Inventory)",
  accounts_receivable: "应收账款 (AR)",
};

function EquityHardDataCard({
  sec,
  yf,
  valEm,
  thsForecast,
}: {
  sec: Dict | null;
  yf: Dict | null;
  valEm: Dict | null;
  thsForecast: Dict | null;
}) {
  const filings = asArr(sec?.recent_filings);
  const eightKFilings = asArr(sec?.recent_8k_filings);
  const post8kSummary = asObj(sec?.post_report_8k_summary);
  const capSnap = asObj(sec?.capital_structure_snapshot);
  const gaap = asObj(sec?.gaap_metrics);
  const valPos = asObj(yf?.valuation_and_positioning);
  const targets = asObj(yf?.analyst_price_targets);
  const capCash = asObj(capSnap?.cash_and_equivalents);
  const capStInv = asObj(capSnap?.short_term_investments);
  const capLtDebt = asObj(capSnap?.long_term_debt);
  const capStDebt = asObj(capSnap?.short_term_debt);
  const capShares = asObj(capSnap?.shares_outstanding_ending);
  const capSbc = asObj(capSnap?.stock_based_compensation);
  const capBuyback = asObj(capSnap?.share_repurchases);
  const sharesYoyPct = toNum(capSnap?.ending_shares_yoy_pct);
  const hasCapSnap = Boolean(capCash || capLtDebt || capShares || capSbc || capBuyback);

  return (
    <div className="card flex flex-col gap-3.5 p-4">
      <h3 className="m-0 text-sm font-semibold text-ink">股票远期预期、估值分位与官方财报核验</h3>

      {valPos || targets ? (
        <div className="metric-strip grid-cols-2 sm:grid-cols-4">
          <div className="metric-cell">
            <span className="metric-label">Trailing / Forward PE</span>
            <span className="metric-val">
              {fmtFixed(valPos?.trailing_pe ?? valPos?.trailingPE, 1)}x /{" "}
              {fmtFixed(valPos?.forward_pe ?? valPos?.forwardPE, 1)}x
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">空头占比 (Short % Float)</span>
            <span className="metric-val">
              {(() => {
                const ratio = toNum(valPos?.short_percent_of_float ?? valPos?.shortPercentOfFloat);
                return ratio !== null ? fmtPct(ratio * 100, 2) : "—";
              })()}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">一致预期目标价均值 / 中位</span>
            <span className="metric-val">
              {fmtPrice(targets?.mean)} / {fmtPrice(targets?.median)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">下次财报窗口</span>
            <span className="metric-val font-sans text-xs text-ink-2">
              {String(valPos?.next_earnings_date ?? "待定")}
            </span>
          </div>
        </div>
      ) : null}

      {valEm ? (
        <div className="metric-strip grid-cols-2 sm:grid-cols-4">
          <div className="metric-cell">
            <span className="metric-label">PE(TTM) & 5Y 分位</span>
            <span className="metric-val">
              {fmtFixed(valEm.pe_ttm ?? valEm.pe_ttm_latest, 1)}x ·{" "}
              {fmtPct(valEm.pe_ttm_5y_percentile ?? valEm.pe_ttm_pct_5y ?? valEm.pe_percentile_5y, 1)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">PE 5Y 中位数</span>
            <span className="metric-val">{fmtFixed(valEm.pe_ttm_5y_median, 1)}x</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">PB & 5Y 分位</span>
            <span className="metric-val">
              {fmtFixed(valEm.pb ?? valEm.pb_latest, 2)}x ·{" "}
              {fmtPct(valEm.pb_5y_percentile ?? valEm.pb_pct_5y ?? valEm.pb_percentile_5y, 1)}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">PB 5Y 中位数</span>
            <span className="metric-val">{fmtFixed(valEm.pb_5y_median, 2)}x</span>
          </div>
        </div>
      ) : null}

      {hasCapSnap ? (
        <div className="flex flex-col gap-1.5">
          <span className="section-title">
            SEC L1 资本结构与稀释快照（截至最新 10-Q/10-K，请对照期后 8-K 核验净现金桥）
          </span>
          <div className="metric-strip grid-cols-2 sm:grid-cols-4">
            <div className="metric-cell">
              <span className="metric-label">现金 / 短期投资</span>
              <span className="metric-val">
                {fmtCompact(capCash?.val)} / {fmtCompact(capStInv?.val)}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">长期债务 / 短期债务</span>
              <span className="metric-val">
                {fmtCompact(capLtDebt?.val)} / {fmtCompact(capStDebt?.val)}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">期末股数 / YoY 稀释</span>
              <span className="metric-val">
                {fmtCompact(capShares?.val)}
                {sharesYoyPct !== null ? ` (${sharesYoyPct > 0 ? "+" : ""}${sharesYoyPct.toFixed(2)}%)` : ""}
              </span>
            </div>
            <div className="metric-cell">
              <span className="metric-label">SBC / 实际回购支出</span>
              <span className="metric-val">
                {fmtCompact(capSbc?.val)} / {fmtCompact(capBuyback?.val)}
              </span>
            </div>
          </div>
        </div>
      ) : null}

      {thsForecast ? <StructuredDataCard title="同花顺机构盈利预测 (2026~2028)" raw={thsForecast} /> : null}

      {filings.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <span className="section-title">SEC EDGAR 官方 L1 财报原件（{String(sec?.entity_name ?? "")}）</span>
          <div className="flex flex-wrap gap-2">
            {filings.map((f, idx) => (
              <a
                key={idx}
                href={typeof f.url === "string" ? f.url : "#"}
                target="_blank"
                rel="noreferrer"
                className="btn btn-sm"
              >
                <span className="font-mono">
                  {String(f.form ?? "10-Q")} · {String(f.filingDate ?? "")}
                </span>
                <ExternalLink className="h-3 w-3 text-muted" aria-hidden="true" />
              </a>
            ))}
          </div>
        </div>
      ) : null}

      {eightKFilings.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <span className="section-title">
            SEC 8-K 重大事件索引
            {post8kSummary
              ? `（最新定期报告日 ${String(post8kSummary.latest_periodic_report_date ?? "—")} 后共 ${String(
                  post8kSummary.post_report_8k_count ?? 0,
                )} 份，其中资本/并购/融资类 ${String(post8kSummary.material_capital_8k_count ?? 0)} 份）`
              : ""}
          </span>
          <div className="flex flex-wrap gap-2">
            {eightKFilings.slice(0, 8).map((f, idx) => {
              const isPost = Boolean(f.post_latest_periodic_report);
              const isCap = Boolean(f.is_material_capital_event);
              const items = Array.isArray(f.items) ? (f.items as unknown[]).map(String).join("/") : "";
              return (
                <a
                  key={idx}
                  href={typeof f.url === "string" ? f.url : "#"}
                  target="_blank"
                  rel="noreferrer"
                  className={`btn btn-sm ${isPost && isCap ? "border-amber-500/60" : ""}`}
                  title={Array.isArray(f.item_labels) ? (f.item_labels as unknown[]).map(String).join(" · ") : undefined}
                >
                  <span className="font-mono">
                    {isPost ? "[期后] " : ""}
                    {String(f.form ?? "8-K")} · {String(f.filingDate ?? "")}
                    {items ? ` (${items})` : ""}
                  </span>
                  <ExternalLink className="h-3 w-3 text-muted" aria-hidden="true" />
                </a>
              );
            })}
          </div>
        </div>
      ) : null}

      {gaap && Object.keys(gaap).length > 0 ? (
        <div tabIndex={0} role="region" aria-label="SEC EDGAR GAAP 核心财务指标表" className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">GAAP / 资本结构指标</th>
                <th scope="col" className="num">
                  最新披露值
                </th>
                <th scope="col">报告期末</th>
                <th scope="col">口径 / 表单</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(gaap).map(([metric, val]) => {
                const vObj = asObj(val);
                const recent = asArr(vObj?.records ?? vObj?.recent ?? vObj?.values)[0] ?? vObj;
                const unitStr = String(recent?.unit ?? vObj?.unit ?? "");
                const periodType = String(recent?.period_type ?? "");
                return (
                  <tr key={metric}>
                    <th scope="row">{SEC_GAAP_METRIC_LABELS[metric] ?? metric}</th>
                    <td data-label="最新披露值" className="num">
                      {fmtCompact(recent?.val ?? recent?.value)}
                      {unitStr === "USD/shares" ? " /股" : unitStr === "shares" ? " 股" : ""}
                    </td>
                    <td data-label="报告期末" className="font-mono text-xs">
                      {String(recent?.end ?? recent?.period ?? "—")}
                    </td>
                    <td data-label="口径 / 表单" className="text-muted">
                      {String(recent?.form ?? "10-K/10-Q")}
                      {periodType ? ` · ${periodType}` : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------
 * Main HardDataVisuals Entry
 * ------------------------------------------------------------------ */
export function HardDataVisuals({ symbol, data, loading, error, onRefresh }: HardDataVisualsProps) {
  const fs = asObj(data?.fundamentals_and_supply) ?? {};
  const macro = asObj(data?.macro_dual_core) ?? {};
  const sources = asObj(data?.sources_status) ?? {};

  const termStructure = asObj(fs.term_structure);
  const spotBasis = asObj(fs.spot_and_basis);
  const inventory = asObj(fs.inventory_em);
  const crossSpread = asObj(fs.cross_market_spread);
  const freight = asObj(fs.baltic_freight);
  const rateDiff = asObj(fs.rate_differential);
  const fxCot = asObj(fs.cftc_fx_cot ?? fs.cftc_cot);
  const secEdgar = asObj(fs.sec_edgar);
  const yfConsensus = asObj(fs.yfinance_consensus);
  const valEm = asObj(fs.valuation_5y_em);
  const thsForecast = asObj(fs.ths_profit_forecast);

  const extraEntries = Object.entries(fs).filter(
    ([k]) =>
      ![
        "term_structure",
        "spot_and_basis",
        "inventory_em",
        "cross_market_spread",
        "baltic_freight",
        "rate_differential",
        "cftc_fx_cot",
        "cftc_cot",
        "sec_edgar",
        "yfinance_consensus",
        "valuation_5y_em",
        "ths_profit_forecast",
      ].includes(k),
  );

  return (
    <div className="flex flex-col gap-3.5">
      {/* Header & Source Health Badges */}
      <div className="card flex flex-col gap-2.5 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <span className="text-sm font-semibold text-ink">
              {formatSymbolWithCode(symbol)} · 白名单路由实时硬数据（{data?.asset_class || "自动识别"}）
            </span>
            {data?.fetched_at_utc ? (
              <span className="ml-2 font-mono text-[11px] text-muted">{shortTime(data.fetched_at_utc)} UTC</span>
            ) : null}
          </div>
          <button type="button" className="btn btn-sm" disabled={loading} onClick={onRefresh}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "拉取中…" : "重新抓取实时源"}</span>
          </button>
        </div>

        {Object.keys(sources).length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            {Object.entries(sources).map(([srcName, meta]) => {
              const m = asObj(meta);
              const st = typeof meta === "string" ? meta : String(m?.status ?? "ok");
              return <SourceStatusBadge key={srcName} name={srcName} status={st} />;
            })}
          </div>
        ) : null}
      </div>

      {error ? (
        <div
          role="alert"
          className="card flex flex-wrap items-center justify-between gap-3 border-critical/50 bg-critical/10 p-3.5 text-xs text-ink"
        >
          <span>硬数据拉取失败：{error}</span>
          <button type="button" className="btn btn-sm" disabled={loading} onClick={onRefresh}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>重试拉取</span>
          </button>
        </div>
      ) : null}

      {loading && !data ? (
        <div className="card p-8 text-center text-xs text-muted">正在按品种白名单并发拉取官方免费硬数据…</div>
      ) : null}

      {termStructure && termStructure.status !== "skipped" && !termStructure.error ? (
        <FuturesCurveCard raw={termStructure} />
      ) : null}

      {(rateDiff && rateDiff.status !== "skipped") || (fxCot && fxCot.status !== "skipped") ? (
        <FxHardDataCard rateDiff={rateDiff} cot={fxCot} />
      ) : null}

      {secEdgar || yfConsensus || valEm ? (
        <EquityHardDataCard sec={secEdgar} yf={yfConsensus} valEm={valEm} thsForecast={thsForecast} />
      ) : null}

      {spotBasis || inventory || crossSpread || freight ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {spotBasis ? <StructuredDataCard title="现货与基差（生意社）" raw={spotBasis} /> : null}
          {inventory ? <StructuredDataCard title="显性期货库存（东财）" raw={inventory} /> : null}
          {crossSpread ? <StructuredDataCard title="内外盘跨市价差与进口比价" raw={crossSpread} /> : null}
          {freight ? <StructuredDataCard title="波罗的海海运指数联动" raw={freight} /> : null}
        </div>
      ) : null}

      {extraEntries.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {extraEntries.map(([k, v]) => (
            <StructuredDataCard key={k} title={MODULE_ZH_TITLES[k] || humanizeKey(k)} raw={v} />
          ))}
        </div>
      ) : null}

      {Object.keys(macro).length > 0 && macro.status !== "skipped" ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {macro.us_fred ? (
            <StructuredDataCard title="美国宏观与实际利率底座（FRED）" raw={macro.us_fred} />
          ) : null}
          {macro.china_macro ? (
            <StructuredDataCard title="中国信用与景气周期（PMI / 进出口）" raw={macro.china_macro} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
