import { useMemo, useState } from "react";
import { ExternalLink, RefreshCw, Table2 } from "lucide-react";
import type { Dict, ResearchDataResponse } from "../../api/types";
import { fmtCompact, fmtFixed, fmtNum, fmtPct, fmtPrice, shortTime, toNum } from "../../lib/format";
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
  const H = 210;
  const pad = { l: 58, r: 24, t: 22, b: 34 };
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
        <div>
          <div className="flex items-center gap-2">
            <h3 className="m-0 text-sm font-semibold text-ink">期货活跃合约期限结构曲线</h3>
            <StatusBadge
              tone={structure.toLowerCase().includes("backwardation") ? "warning" : "info"}
              label={structure}
            />
          </div>
          <p className="m-0 mt-0.5 text-xs text-muted">
            单纵轴合约价格结构 · 鼠标悬停查看单腿报价与成交量（大 B 结构不否决做空）
          </p>
        </div>
        <button type="button" className="chip" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
          <Table2 className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{showTable ? "返回曲线" : "合约表"}</span>
        </button>
      </div>

      {/* Roll Yield KPI Tiles */}
      <div className="tnum grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <div className="rounded border border-hairline bg-page/60 p-2.5">
          <div className="text-muted">近月合约</div>
          <div className="mt-0.5 font-semibold text-ink">
            {String(raw.front_contract ?? points[0]?.code ?? "—")} · {fmtPrice(raw.front_price ?? points[0]?.price)}
          </div>
        </div>
        <div className="rounded border border-hairline bg-page/60 p-2.5">
          <div className="text-muted">远月合约</div>
          <div className="mt-0.5 font-semibold text-ink">
            {String(raw.far_contract ?? raw.back_contract ?? points[points.length - 1]?.code ?? "—")} ·{" "}
            {fmtPrice(raw.far_price ?? raw.back_price ?? points[points.length - 1]?.price)}
          </div>
        </div>
        <div className="rounded border border-hairline bg-page/60 p-2.5">
          <div className="text-muted">近月年化展期收益</div>
          <div className="mt-0.5 font-semibold text-ink">{fmtPct(nearRoll, 2, true)}</div>
        </div>
        <div className="rounded border border-hairline bg-page/60 p-2.5">
          <div className="text-muted">全曲线年化展期收益</div>
          <div className="mt-0.5 font-semibold text-ink">{fmtPct(curveRoll, 2, true)}</div>
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
            {/* Recessive horizontal gridlines (3 ticks) */}
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
                    style={{ fontVariantNumeric: "tabular-nums" }}
                  >
                    {fmtPrice(val)}
                  </text>
                </g>
              );
            })}

            {/* 2px primary curve line */}
            <path d={pathD} fill="none" stroke={palette.slots[0]} strokeWidth={2} />

            {/* Hover crosshair */}
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

            {/* Marks (>= 8px diameter with 2px surface ring) + larger invisible hit targets */}
            {points.map((p, i) => {
              const cx = xAt(i);
              const cy = yAt(p.price);
              const showTick = points.length <= 8 || i === 0 || i === points.length - 1 || i % Math.ceil(points.length / 6) === 0;
              return (
                <g key={p.code}>
                  <circle
                    cx={cx}
                    cy={cy}
                    r={hoverIdx === i ? 6 : 4.5}
                    fill={palette.slots[0]}
                    stroke={palette.surface}
                    strokeWidth={2}
                  />
                  {/* Selective direct labels on front and back contracts */}
                  {i === 0 || i === points.length - 1 ? (
                    <text
                      x={cx}
                      y={cy - 10}
                      textAnchor={i === 0 ? "start" : "end"}
                      fontSize="10.5"
                      fontWeight="600"
                      fill={palette.ink}
                      style={{ fontVariantNumeric: "tabular-nums" }}
                    >
                      {fmtPrice(p.price)}
                    </text>
                  ) : null}
                  {showTick ? (
                    <text x={cx} y={H - 10} textAnchor="middle" fontSize="10.5" fill={palette.muted}>
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
            <div className="tnum mt-1 flex flex-wrap items-center gap-3 rounded border border-hairline bg-elevated px-3 py-1.5 text-xs text-ink">
              <strong>{activePt.code}</strong>
              <span>报价：{fmtPrice(activePt.price)}</span>
              {activePt.volume != null ? <span>成交量：{fmtCompact(activePt.volume)}</span> : null}
              {activePt.oi != null ? <span>持仓量：{fmtCompact(activePt.oi)}</span> : null}
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
        <div>
          <h3 className="m-0 text-sm font-semibold text-ink">主权国债利差 & CFTC 金融期货持仓结构（TFF）</h3>
          <p className="m-0 mt-0.5 text-xs text-muted">
            零轴双向净头寸条形图（自动标注反向报价换算）· 悬停查看多空明细
          </p>
        </div>
        {buckets.length > 0 ? (
          <button type="button" className="chip" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
            <Table2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{showTable ? "返回图形" : "COT 明细表"}</span>
          </button>
        ) : null}
      </div>

      {/* Rate Differential KPI Strip */}
      {rateDiff ? (
        <div className="tnum grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">10Y 利差 (Base − Quote)</div>
            <div className="mt-0.5 font-semibold text-ink">
              {rateDiff.base_minus_quote_10y_bp != null
                ? `${fmtFixed(rateDiff.base_minus_quote_10y_bp, 1)} bp`
                : fmtPct(rateDiff.base_minus_quote_10y_pct, 2, true)}
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">基准端收益率</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtPct(asObj(rateDiff.base)?.latest_value ?? asObj(rateDiff.us10y)?.latest_value, 2)}
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">计价端收益率</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtPct(asObj(rateDiff.quote)?.latest_value ?? asObj(rateDiff.cn10y)?.latest_value, 2)}
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">口径与频率提示</div>
            <div className="mt-0.5 truncate font-medium text-ink-2">
              {String(rateDiff.formula || rateDiff.source || "FRED / 东财")}
            </div>
          </div>
        </div>
      ) : null}

      {cot?.direction_note ? (
        <div className="rounded border border-slot1/35 bg-slot1/10 px-3 py-1.5 text-xs text-ink">
          <strong>报价方向换算说明：</strong> {String(cot.direction_note)}（报告期：{String(cot.report_date ?? "—")} · 总持仓 OI：{fmtCompact(cot.open_interest)}）
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
        <div className="space-y-2.5">
          {buckets.map((b) => {
            const pctWidth = Math.min(50, (Math.abs(b.net) / maxAbs) * 48);
            const positive = b.net >= 0;
            const barColor = positive ? palette.slots[2] : palette.slots[1];
            return (
              <div
                key={b.key}
                onMouseEnter={() => setHoverRow(b.key)}
                onMouseLeave={() => setHoverRow(null)}
                className="rounded-md border border-hairline bg-page/50 p-2.5"
              >
                <div className="mb-1.5 flex items-center justify-between gap-2 text-xs">
                  <span className="font-medium text-ink">{b.label}</span>
                  <span className="tnum text-ink-2">
                    净头寸 <strong className="text-ink">{fmtNum(b.net, 0)}</strong>
                    {b.pctOi != null ? ` (${fmtPct(b.pctOi, 1, true)} OI)` : ""}
                  </span>
                </div>
                <div className="relative h-4 w-full rounded bg-elevated">
                  <div className="absolute inset-y-0 left-1/2 w-px bg-muted/60" aria-hidden="true" />
                  <div
                    style={{
                      width: `${pctWidth}%`,
                      left: positive ? "50%" : `${50 - pctWidth}%`,
                      backgroundColor: barColor,
                      borderRadius: positive ? "0 4px 4px 0" : "4px 0 0 4px",
                    }}
                    className="absolute inset-y-0.5"
                  />
                </div>
                {hoverRow === b.key && (b.long != null || b.short != null) ? (
                  <div className="tnum mt-1.5 text-[11px] text-muted">
                    多头：{fmtNum(b.long, 0)} 手 · 空头：{fmtNum(b.short, 0)} 手
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
function EquityHardDataCard({ sec, yf, valEm, thsForecast }: { sec: Dict | null; yf: Dict | null; valEm: Dict | null; thsForecast: Dict | null }) {
  const filings = asArr(sec?.recent_filings);
  const gaap = asObj(sec?.gaap_metrics);
  const valPos = asObj(yf?.valuation_and_positioning);
  const targets = asObj(yf?.analyst_price_targets);

  return (
    <div className="card flex flex-col gap-4 p-4">
      <div>
        <h3 className="m-0 text-sm font-semibold text-ink">股票远期预期、估值分位与官方财报核验</h3>
        <p className="m-0 mt-0.5 text-xs text-muted">
          严格区分 SEC GAAP 原表与卖方一致预期（Adjusted 口径需交叉核验，禁死板套用静态当期 PE）
        </p>
      </div>

      {/* US Valuation & Short Interest (snake_case aligned with scripts/research_extensions.py) */}
      {valPos || targets ? (
        <div className="tnum grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">Trailing / Forward PE (yfinance)</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtFixed(valPos?.trailing_pe ?? valPos?.trailingPE, 1)}x /{" "}
              {fmtFixed(valPos?.forward_pe ?? valPos?.forwardPE, 1)}x
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">空头占流通盘 (Short % Float)</div>
            <div className="mt-0.5 font-semibold text-ink">
              {(() => {
                const ratio = toNum(valPos?.short_percent_of_float ?? valPos?.shortPercentOfFloat);
                return ratio !== null ? fmtPct(ratio * 100, 2) : "—";
              })()}
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">一致预期目标价均值 / 中位</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtPrice(targets?.mean)} / {fmtPrice(targets?.median)}
            </div>
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">下次财报窗口</div>
            <div className="mt-0.5 font-medium text-ink-2">
              {String(valPos?.next_earnings_date ?? "待定")}
            </div>
          </div>
        </div>
      ) : null}

      {/* A-Share 5Y Valuation Percentiles (snake_case aligned with compute_ashare_valuation_percentiles) */}
      {valEm ? (
        <div className="tnum grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">PE(TTM) & 5年历史分位</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtFixed(valEm.pe_ttm ?? valEm.pe_ttm_latest, 1)}x · 分位{" "}
              {fmtPct(valEm.pe_ttm_5y_percentile ?? valEm.pe_ttm_pct_5y ?? valEm.pe_percentile_5y, 1)}
            </div>
            {valEm.pe_ttm_5y_median != null ? (
              <div className="mt-0.5 text-[10.5px] text-muted">
                5年中位数：{fmtFixed(valEm.pe_ttm_5y_median, 1)}x
              </div>
            ) : null}
          </div>
          <div className="rounded border border-hairline bg-page/60 p-2.5">
            <div className="text-muted">PB & 5年历史分位</div>
            <div className="mt-0.5 font-semibold text-ink">
              {fmtFixed(valEm.pb ?? valEm.pb_latest, 2)}x · 分位{" "}
              {fmtPct(valEm.pb_5y_percentile ?? valEm.pb_pct_5y ?? valEm.pb_percentile_5y, 1)}
            </div>
            {valEm.pb_5y_median != null ? (
              <div className="mt-0.5 text-[10.5px] text-muted">
                5年中位数：{fmtFixed(valEm.pb_5y_median, 2)}x
              </div>
            ) : null}
          </div>
          {thsForecast ? (
            <div className="rounded border border-hairline bg-page/60 p-2.5 sm:col-span-2">
              <div className="text-muted">同花顺机构盈利预测</div>
              <div className="mt-0.5 truncate font-medium text-ink-2">
                {JSON.stringify(thsForecast).slice(0, 110)}…
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* SEC EDGAR L1 Filings */}
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
                <span>
                  {String(f.form ?? "10-Q")} · {String(f.filingDate ?? "")}
                </span>
                <ExternalLink className="h-3 w-3 text-slot1" aria-hidden="true" />
              </a>
            ))}
          </div>
        </div>
      ) : null}

      {/* SEC GAAP Metrics Table */}
      {gaap && Object.keys(gaap).length > 0 ? (
        <div tabIndex={0} role="region" aria-label="SEC EDGAR GAAP 核心财务指标表" className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">GAAP 指标</th>
                <th scope="col" className="num">
                  最新披露值
                </th>
                <th scope="col">报告期末</th>
                <th scope="col">表单来源</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(gaap).map(([metric, val]) => {
                const vObj = asObj(val);
                const recent = asArr(vObj?.recent ?? vObj?.values)[0] ?? vObj;
                return (
                  <tr key={metric}>
                    <th scope="row">{metric}</th>
                    <td data-label="最新披露值" className="num">
                      {fmtCompact(recent?.val ?? recent?.value)}
                    </td>
                    <td data-label="报告期末">{String(recent?.end ?? recent?.period ?? "—")}</td>
                    <td data-label="表单来源" className="text-muted">
                      {String(recent?.form ?? "10-K/10-Q")}
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

  /* Remaining structured cards rendered cleanly as key-value summary tiles */
  const extraEntries = Object.entries(fs).filter(
    ([k]) =>
      ![
        "term_structure",
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
    <div className="flex flex-col gap-4">
      {/* Header & Source Health Badges */}
      <div className="card flex flex-col gap-2.5 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <span className="text-sm font-semibold text-ink">
              {symbol} · 按品种白名单精准路由硬数据（{data?.asset_class || "自动识别"}）
            </span>
            {data?.fetched_at_utc ? (
              <span className="ml-2 text-xs text-muted">抓取时间：{shortTime(data.fetched_at_utc)} UTC</span>
            ) : null}
          </div>
          <button type="button" className="btn btn-sm" disabled={loading} onClick={onRefresh}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "拉取硬数据中…" : "重新抓取实时源"}</span>
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

      {/* Visual 1: Futures Term Structure */}
      {termStructure && termStructure.status !== "skipped" && !termStructure.error ? (
        <FuturesCurveCard raw={termStructure} />
      ) : null}

      {/* Visual 2: FX Rate Diff & CFTC COT */}
      {(rateDiff && rateDiff.status !== "skipped") || (fxCot && fxCot.status !== "skipped") ? (
        <FxHardDataCard rateDiff={rateDiff} cot={fxCot} />
      ) : null}

      {/* Visual 3: Equity Valuation & SEC EDGAR */}
      {secEdgar || yfConsensus || valEm ? (
        <EquityHardDataCard sec={secEdgar} yf={yfConsensus} valEm={valEm} thsForecast={thsForecast} />
      ) : null}

      {/* Commodity Spot/Basis, Inventory, Cross-Market Spread & Freight Summary */}
      {(spotBasis || inventory || crossSpread || freight) ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {spotBasis ? (
            <div className="card p-3.5 text-xs">
              <div className="section-title mb-2">现货与基差（生意社）</div>
              <pre className="m-0 max-h-44 overflow-auto font-mono text-[11.5px] text-ink-2">
                {JSON.stringify(spotBasis, null, 2)}
              </pre>
            </div>
          ) : null}
          {inventory ? (
            <div className="card p-3.5 text-xs">
              <div className="section-title mb-2">显性期货库存（东财）</div>
              <pre className="m-0 max-h-44 overflow-auto font-mono text-[11.5px] text-ink-2">
                {JSON.stringify(inventory, null, 2)}
              </pre>
            </div>
          ) : null}
          {crossSpread ? (
            <div className="card p-3.5 text-xs">
              <div className="section-title mb-2">内外盘跨市价差与进口比价</div>
              <pre className="m-0 max-h-44 overflow-auto font-mono text-[11.5px] text-ink-2">
                {JSON.stringify(crossSpread, null, 2)}
              </pre>
            </div>
          ) : null}
          {freight ? (
            <div className="card p-3.5 text-xs">
              <div className="section-title mb-2">波罗的海海运指数联动</div>
              <pre className="m-0 max-h-44 overflow-auto font-mono text-[11.5px] text-ink-2">
                {JSON.stringify(freight, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Remaining asset-specific modules (Crypto ETF / Stablecoins / EIA / USDA) */}
      {extraEntries.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {extraEntries
            .filter(([k]) => !["spot_and_basis", "inventory_em", "cross_market_spread", "baltic_freight"].includes(k))
            .map(([k, v]) => (
              <div key={k} className="card p-3.5 text-xs">
                <div className="section-title mb-2">{k}</div>
                <pre className="m-0 max-h-48 overflow-auto font-mono text-[11.5px] text-ink-2">
                  {JSON.stringify(v, null, 2)}
                </pre>
              </div>
            ))}
        </div>
      ) : null}

      {/* Macro Dual-Core Summary */}
      {Object.keys(macro).length > 0 && macro.status !== "skipped" ? (
        <div className="card p-3.5 text-xs">
          <div className="section-title mb-2">中美宏观双核底座（FRED 免 Key 利率通胀 + 中国 PMI/进出口）</div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {macro.us_fred ? (
              <pre className="m-0 max-h-44 overflow-auto rounded border border-hairline bg-page/60 p-2.5 font-mono text-[11px] text-ink-2">
                {JSON.stringify(macro.us_fred, null, 2)}
              </pre>
            ) : null}
            {macro.china_macro ? (
              <pre className="m-0 max-h-44 overflow-auto rounded border border-hairline bg-page/60 p-2.5 font-mono text-[11px] text-ink-2">
                {JSON.stringify(macro.china_macro, null, 2)}
              </pre>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
