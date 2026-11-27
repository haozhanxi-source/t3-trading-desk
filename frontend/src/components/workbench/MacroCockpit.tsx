import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  ExternalLink,
  Globe2,
  Scale,
  ShieldAlert,
  ShieldCheck,
  Table2,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import type {
  CftcCrowdingBenchmarkItem,
  CompassMacroOverview,
  CrossAssetAnchorItem,
  MacroHeadlineMetric,
  PolymarketEventItem,
} from "../../api/types";
import { fmtPct } from "../../lib/format";

interface MacroCockpitProps {
  macro: CompassMacroOverview | null;
  loading?: boolean;
  onJumpWarRoom?: (symbol: string) => void;
}

type RadarTabId = "anchors" | "cftc" | "polymarket";
type AnchorViewMode = "bars" | "scatter";

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

function fmtMetricVal(m?: MacroHeadlineMetric): string {
  if (!m || m.value === null || m.value === undefined || Number.isNaN(m.value)) return "—";
  if (m.unit === "$/t") return `$${Math.round(m.value).toLocaleString("en-US")}/t`;
  if (m.unit === "%") return `${m.value.toFixed(2)}%`;
  return m.value.toFixed(1);
}

function fmtDeltaShort(m?: MacroHeadlineMetric): string | null {
  if (!m || m.delta === null || m.delta === undefined || Number.isNaN(m.delta)) return null;
  const sign = m.delta >= 0 ? "+" : "";
  if (m.delta_unit === "%") return `${sign}${m.delta.toFixed(2)}%`;
  if (m.delta_unit === "pp") return `${sign}${m.delta.toFixed(2)}pp`;
  return `${sign}${m.delta.toFixed(2)}`;
}

function shieldColorVar(state?: string): string {
  if (state === "bullish_shield_intact") return "var(--viz-cat-2)";
  if (state === "high_sell_above_zd_watch") return "var(--viz-cat-4)";
  if (state === "below_zd_breakdown") return "var(--bear)";
  return "var(--viz-cat-1)";
}

function shieldShortLabel(state?: string): string {
  if (state === "bullish_shield_intact") return "护盾完好";
  if (state === "high_sell_above_zd_watch") return "高位警戒";
  if (state === "below_zd_breakdown") return "跌破ZD";
  return "结构待明";
}

function MicroRulerRow({
  label,
  valueText,
  deltaText,
  ratio,
  fillColor,
  ticks,
  tooltip,
  badgeText,
  badgeTone,
}: {
  label: string;
  valueText: string;
  deltaText?: string | null;
  ratio: number | null;
  fillColor: string;
  ticks?: { pos: number; label?: string }[];
  tooltip: string;
  badgeText?: string;
  badgeTone?: "good" | "warning" | "critical" | "neutral";
}) {
  const pct = ratio !== null ? clamp(ratio * 100, 2, 98) : null;
  const badgeCls =
    badgeTone === "critical"
      ? "bg-bear/14 text-bear"
      : badgeTone === "warning"
        ? "bg-warning/14 text-warning"
        : badgeTone === "good"
          ? "bg-bull/14 text-bull"
          : "bg-subtle text-muted";

  return (
    <div className="flex flex-col gap-1" title={tooltip}>
      <div className="flex items-baseline justify-between gap-2 text-[11px]">
        <span className="truncate text-muted">{label}</span>
        <div className="flex shrink-0 items-baseline gap-1.5">
          {badgeText ? (
            <span className={`rounded px-1 py-px text-[9px] font-medium ${badgeCls}`}>{badgeText}</span>
          ) : null}
          <span className="font-mono text-[12px] font-semibold tabular-nums text-ink">{valueText}</span>
          {deltaText ? (
            <span className="font-mono text-[10px] tabular-nums text-muted">{deltaText}</span>
          ) : null}
        </div>
      </div>
      <div className="relative h-2 w-full rounded-full bg-subtle/80">
        {ticks?.map((t, i) => (
          <span
            key={i}
            aria-hidden="true"
            className="absolute top-0 h-full w-px bg-hairline"
            style={{ left: `${clamp(t.pos * 100, 0, 100)}%` }}
          />
        ))}
        {pct !== null ? (
          <>
            <span
              className="absolute left-0 top-0 h-full rounded-l-full rounded-r-[4px] opacity-30"
              style={{ width: `${pct}%`, background: fillColor }}
            />
            <span
              className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface shadow-xs"
              style={{ left: `${pct}%`, background: fillColor }}
            />
          </>
        ) : null}
      </div>
    </div>
  );
}

export function MacroCockpit({ macro, loading, onJumpWarRoom }: MacroCockpitProps) {
  const baseId = useId();
  const [activeTab, setActiveTab] = useState<RadarTabId>("anchors");
  const [anchorView, setAnchorView] = useState<AnchorViewMode>("bars");
  const [anchorGroup, setAnchorGroup] = useState<string>("all");
  const [tableView, setTableView] = useState(false);
  const [hoveredAnchor, setHoveredAnchor] = useState<CrossAssetAnchorItem | null>(null);
  const [hoveredCftc, setHoveredCftc] = useState<CftcCrowdingBenchmarkItem | null>(null);
  const [hoveredPm, setHoveredPm] = useState<PolymarketEventItem | null>(null);
  const tabRefs = useRef<Record<RadarTabId, HTMLButtonElement | null>>({
    anchors: null,
    cftc: null,
    polymarket: null,
  });

  const metricByKey = useMemo(() => {
    const map = new Map<string, MacroHeadlineMetric>();
    for (const m of macro?.headline_metrics ?? []) {
      map.set(m.key, m);
    }
    return map;
  }, [macro]);

  const anchorItems = useMemo(
    () => (macro?.anchors?.items ?? []).filter((it) => it.status === "ok" && it.ret_20d_pct !== null),
    [macro],
  );

  const anchorGroups = useMemo(() => {
    const seen = new Map<string, string>();
    for (const it of anchorItems) {
      if (it.group && !seen.has(it.group)) {
        seen.set(it.group, it.group_label || it.group);
      }
    }
    return Array.from(seen.entries());
  }, [anchorItems]);

  const visibleAnchors = useMemo(() => {
    const filtered = anchorGroup === "all" ? anchorItems : anchorItems.filter((it) => it.group === anchorGroup);
    return [...filtered]
      .sort((a, b) => Math.abs(b.ret_20d_pct ?? 0) - Math.abs(a.ret_20d_pct ?? 0))
      .slice(0, anchorView === "bars" ? 12 : 25);
  }, [anchorItems, anchorGroup, anchorView]);

  const cftcItems = useMemo(
    () => (macro?.cftc_crowding?.items ?? []).filter((it) => it.percentile_52w !== null && it.percentile_52w !== undefined),
    [macro],
  );

  const cftcExtremesCount = useMemo(
    () =>
      cftcItems.filter(
        (it) =>
          !it.is_stale &&
          (it.crowding_state === "crowded_long" || it.crowding_state === "crowded_short"),
      ).length,
    [cftcItems],
  );

  const pmEvents = useMemo(() => {
    const raw = macro?.polymarket_odds?.headline_events?.length
      ? macro.polymarket_odds.headline_events
      : macro?.polymarket_odds?.events ?? [];
    return raw.slice(0, 6);
  }, [macro]);

  const pmBigShifts = useMemo(
    () => pmEvents.filter((ev) => Math.abs(ev.leading_chg_7d_pp ?? 0) >= 8).length,
    [pmEvents],
  );

  const brokenShieldsCount = useMemo(
    () => anchorItems.filter((it) => it.chanlun_hint?.shield_state === "below_zd_breakdown").length,
    [anchorItems],
  );

  // Semantic metrics extraction
  const mReal = metricByKey.get("real_yield_10y");
  const mNom = metricByKey.get("us10y");
  const mHy = metricByKey.get("hy_oas");
  const mDxy = metricByKey.get("dxy");
  const mVix = metricByKey.get("vix");
  const mFed = metricByKey.get("fed_funds");
  const mPmi = metricByKey.get("china_pmi");
  const mCopper = metricByKey.get("copper_global");

  const breadth = macro?.tape_movers?.regime_breadth;
  const advCount = breadth?.advancing_20d_count ?? 0;
  const decCount = breadth?.declining_20d_count ?? 0;
  const totalBreadth = Math.max(breadth?.total_available ?? advCount + decCount, 0);
  const flatCount = Math.max(0, totalBreadth - advCount - decCount);

  // Roving tab keyboard handler (modern-web-guidance tabs pattern: selection follows focus)
  const tabOrder: RadarTabId[] = ["anchors", "cftc", "polymarket"];
  function handleTabKeyDown(e: KeyboardEvent<HTMLButtonElement>, current: RadarTabId) {
    const idx = tabOrder.indexOf(current);
    let next: RadarTabId | null = null;
    if (e.key === "ArrowRight") next = tabOrder[(idx + 1) % tabOrder.length];
    else if (e.key === "ArrowLeft") next = tabOrder[(idx - 1 + tabOrder.length) % tabOrder.length];
    else if (e.key === "Home") next = tabOrder[0];
    else if (e.key === "End") next = tabOrder[tabOrder.length - 1];
    if (!next) return;
    e.preventDefault();
    setActiveTab(next);
    tabRefs.current[next]?.focus();
  }

  if (!macro && loading) {
    return (
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="h-[224px] animate-pulse rounded-lg border border-hairline-soft bg-sunken lg:col-span-5" />
        <div className="h-[224px] animate-pulse rounded-lg border border-hairline-soft bg-sunken lg:col-span-7" />
      </div>
    );
  }

  if (!macro) return null;

  const maxAbsRet20 = Math.max(
    10,
    ...visibleAnchors.map((a) => Math.abs(a.ret_20d_pct ?? 0)),
  );
  const maxAbsRet60 = Math.max(
    15,
    ...visibleAnchors.map((a) => Math.abs(a.ret_60d_pct ?? 0)),
  );

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
      {/* Left Pane (5/12): 3 Semantic Macro Micro-Ruler Groups */}
      <div className="flex flex-col justify-between gap-2.5 rounded-lg border border-hairline-soft bg-sunken/70 p-3 lg:col-span-5">
        {/* Group 1: US Rates & Credit */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
              <Scale className="h-3 w-3 text-accent" aria-hidden="true" />
              美债分母与信用地基
            </span>
            <span className="font-mono text-[10px] tabular-nums text-muted">
              US10Y {fmtMetricVal(mNom)} · DXY {fmtMetricVal(mDxy)}
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <MicroRulerRow
              label="10Y 实际利率"
              valueText={fmtMetricVal(mReal)}
              deltaText={fmtDeltaShort(mReal)}
              ratio={mReal?.value != null ? (mReal.value - -0.5) / 3.5 : null}
              fillColor={
                mReal?.value != null && mReal.value >= 2.0
                  ? "var(--warning)"
                  : mReal?.value != null && mReal.value >= 1.5
                    ? "var(--viz-cat-1)"
                    : "var(--viz-cat-2)"
              }
              ticks={[{ pos: (1.5 - -0.5) / 3.5 }, { pos: (2.0 - -0.5) / 3.5 }]}
              tooltip={`10Y 实际利率 (DFII10): ${fmtMetricVal(mReal)} · 截至 ${mReal?.as_of || "未知"} (${mReal?.source || "FRED"})`}
              badgeText={mReal?.value != null ? (mReal.value >= 2.0 ? "紧缩" : mReal.value >= 1.2 ? "中性" : "宽松") : undefined}
              badgeTone={mReal?.value != null ? (mReal.value >= 2.0 ? "warning" : "neutral") : "neutral"}
            />
            <MicroRulerRow
              label="高收益信用利差"
              valueText={fmtMetricVal(mHy)}
              deltaText={fmtDeltaShort(mHy)}
              ratio={mHy?.value != null ? (mHy.value - 2.0) / 5.5 : null}
              fillColor={
                mHy?.value != null && mHy.value >= 5.0
                  ? "var(--bear)"
                  : mHy?.value != null && mHy.value >= 4.0
                    ? "var(--warning)"
                    : "var(--viz-cat-2)"
              }
              ticks={[{ pos: (4.0 - 2.0) / 5.5 }, { pos: (5.0 - 2.0) / 5.5 }]}
              tooltip={`美高收益信用利差 HY OAS (BAMLH0A0HYM2): ${fmtMetricVal(mHy)} · 4%走阔 / 5%高压 · 截至 ${mHy?.as_of || "未知"}`}
              badgeText={
                mHy?.value != null
                  ? mHy.value >= 5.0
                    ? "高压"
                    : mHy.value >= 4.0
                      ? "走阔"
                      : "平稳"
                  : undefined
              }
              badgeTone={
                mHy?.value != null
                  ? mHy.value >= 5.0
                    ? "critical"
                    : mHy.value >= 4.0
                      ? "warning"
                      : "good"
                  : "neutral"
              }
            />
          </div>
        </div>

        <hr className="m-0 border-0 border-t border-hairline-soft" />

        {/* Group 2: Volatility & Breadth */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
              <Activity className="h-3 w-3 text-accent" aria-hidden="true" />
              恐慌摆锤与跨资产宽度
            </span>
            <span className="font-mono text-[10px] tabular-nums text-muted">
              FedFunds {fmtMetricVal(mFed)}
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <MicroRulerRow
              label="VIX 波动率"
              valueText={fmtMetricVal(mVix)}
              deltaText={fmtDeltaShort(mVix)}
              ratio={mVix?.value != null ? (mVix.value - 10) / 30 : null}
              fillColor={
                mVix?.value != null && mVix.value >= 30
                  ? "var(--bear)"
                  : mVix?.value != null && mVix.value >= 20
                    ? "var(--warning)"
                    : "var(--viz-cat-2)"
              }
              ticks={[{ pos: (20 - 10) / 30 }, { pos: (30 - 10) / 30 }]}
              tooltip={`VIX 恐慌指数: ${fmtMetricVal(mVix)} · ≥30 触发宪法第3条深坑禁追空红线 · 截至 ${mVix?.as_of || "未知"}`}
              badgeText={
                mVix?.value != null
                  ? mVix.value >= 30
                    ? "恐慌"
                    : mVix.value >= 20
                      ? "偏高"
                      : "温和"
                  : undefined
              }
              badgeTone={
                mVix?.value != null
                  ? mVix.value >= 30
                    ? "critical"
                    : mVix.value >= 20
                      ? "warning"
                      : "good"
                  : "neutral"
              }
            />
            {/* Market Breadth Segmented Thermometer (2px surface gap between segments) */}
            <div
              className="flex flex-col gap-1"
              title={`20D 跨资产定价锚宽度：上涨 ${advCount} / 持平 ${flatCount} / 下跌 ${decCount}（共 ${totalBreadth} 只）· ${breadth?.breadth_label || ""}`}
            >
              <div className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="truncate text-muted">20D 盘面宽度</span>
                <span className="font-mono text-[11px] font-semibold tabular-nums text-ink">
                  {totalBreadth > 0 ? `${advCount}涨 / ${decCount}跌` : "—"}
                </span>
              </div>
              <div className="flex h-2 w-full items-center gap-[2px] overflow-hidden rounded-full bg-subtle/80">
                {totalBreadth > 0 ? (
                  <>
                    {decCount > 0 ? (
                      <span
                        className="h-full rounded-l-full"
                        style={{
                          width: `${(decCount / totalBreadth) * 100}%`,
                          background: "var(--bear)",
                        }}
                      />
                    ) : null}
                    {flatCount > 0 ? (
                      <span
                        className="h-full"
                        style={{
                          width: `${(flatCount / totalBreadth) * 100}%`,
                          background: "var(--muted)",
                          opacity: 0.45,
                        }}
                      />
                    ) : null}
                    {advCount > 0 ? (
                      <span
                        className="h-full rounded-r-full"
                        style={{
                          width: `${(advCount / totalBreadth) * 100}%`,
                          background: "var(--viz-cat-2)",
                        }}
                      />
                    ) : null}
                  </>
                ) : null}
              </div>
            </div>
          </div>
        </div>

        <hr className="m-0 border-0 border-t border-hairline-soft" />

        {/* Group 3: China Cycle & Global Copper */}
        <div className="grid grid-cols-1 items-center gap-2 sm:grid-cols-2">
          <MicroRulerRow
            label="中国制造业 PMI"
            valueText={fmtMetricVal(mPmi)}
            deltaText={fmtDeltaShort(mPmi)}
            ratio={mPmi?.value != null ? (mPmi.value - 47) / 6 : null}
            fillColor={
              mPmi?.value != null && mPmi.value >= 50.5
                ? "var(--viz-cat-2)"
                : mPmi?.value != null && mPmi.value < 49.5
                  ? "var(--bear)"
                  : "var(--warning)"
            }
            ticks={[{ pos: 0.5 }]}
            tooltip={`中国官方制造业 PMI: ${fmtMetricVal(mPmi)} · 荣枯线 50.0 · 截至 ${mPmi?.as_of || "未知"}`}
            badgeText={
              mPmi?.value != null
                ? mPmi.value >= 50.5
                  ? "扩张"
                  : mPmi.value < 49.5
                    ? "收缩"
                    : "荣枯"
                : undefined
            }
            badgeTone={
              mPmi?.value != null
                ? mPmi.value >= 50.5
                  ? "good"
                  : mPmi.value < 49.5
                    ? "critical"
                    : "warning"
                : "neutral"
            }
          />
          <div
            className="flex items-center justify-between gap-2 rounded-md border border-hairline-soft bg-surface/80 px-2.5 py-1.5"
            title={`全球铜价（工业实物周期定价锚）：${fmtMetricVal(mCopper)} · 截至 ${mCopper?.as_of || "未知"}`}
          >
            <span className="inline-flex items-center gap-1 text-[11px] text-muted">
              <Globe2 className="h-3 w-3 text-accent" aria-hidden="true" />
              全球铜价
            </span>
            <div className="flex items-baseline gap-1.5">
              <span className="font-mono text-[12px] font-semibold tabular-nums text-ink">
                {fmtMetricVal(mCopper)}
              </span>
              {fmtDeltaShort(mCopper) ? (
                <span className="font-mono text-[10px] tabular-nums text-muted">
                  {fmtDeltaShort(mCopper)}
                </span>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      {/* Right Pane (7/12): 3-Tab Visual Radar (Anchors / CFTC / Polymarket) */}
      <div className="flex min-w-0 flex-col rounded-lg border border-hairline-soft bg-surface p-3 lg:col-span-7">
        {/* Accessible Tablist Header + View Controls */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="mwg-tablist" role="tablist" aria-label="宏观视觉雷达切换">
            <button
              ref={(el) => {
                tabRefs.current.anchors = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-anchors`}
              aria-selected={activeTab === "anchors"}
              tabIndex={activeTab === "anchors" ? 0 : -1}
              className="mwg-tab"
              onClick={() => setActiveTab("anchors")}
              onKeyDown={(e) => handleTabKeyDown(e, "anchors")}
            >
              <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />
              <span>跨资产动量 × 护盾</span>
              {brokenShieldsCount > 0 ? (
                <span className="rounded bg-bear/14 px-1 py-px font-mono text-[9px] font-semibold text-bear">
                  {brokenShieldsCount}破ZD
                </span>
              ) : null}
            </button>

            <button
              ref={(el) => {
                tabRefs.current.cftc = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-cftc`}
              aria-selected={activeTab === "cftc"}
              tabIndex={activeTab === "cftc" ? 0 : -1}
              className="mwg-tab"
              onClick={() => setActiveTab("cftc")}
              onKeyDown={(e) => handleTabKeyDown(e, "cftc")}
            >
              <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
              <span>CFTC 52W 拥挤</span>
              {cftcExtremesCount > 0 ? (
                <span className="rounded bg-warning/16 px-1 py-px font-mono text-[9px] font-semibold text-warning">
                  {cftcExtremesCount}极端
                </span>
              ) : null}
            </button>

            <button
              ref={(el) => {
                tabRefs.current.polymarket = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-polymarket`}
              aria-selected={activeTab === "polymarket"}
              tabIndex={activeTab === "polymarket" ? 0 : -1}
              className="mwg-tab"
              onClick={() => setActiveTab("polymarket")}
              onKeyDown={(e) => handleTabKeyDown(e, "polymarket")}
            >
              <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Polymarket 赔率</span>
              {pmBigShifts > 0 ? (
                <span className="rounded bg-accent/14 px-1 py-px font-mono text-[9px] font-semibold text-accent">
                  {pmBigShifts}异动
                </span>
              ) : null}
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            {activeTab === "anchors" && !tableView ? (
              <div className="segmented" role="group" aria-label="动量图视图切换">
                <button
                  type="button"
                  className="segmented-btn !min-h-[22px] !px-2 !text-[10px]"
                  aria-pressed={anchorView === "bars"}
                  onClick={() => setAnchorView("bars")}
                >
                  20D条图
                </button>
                <button
                  type="button"
                  className="segmented-btn !min-h-[22px] !px-2 !text-[10px]"
                  aria-pressed={anchorView === "scatter"}
                  onClick={() => setAnchorView("scatter")}
                >
                  20D×60D象限
                </button>
              </div>
            ) : null}
            <button
              type="button"
              className="btn btn-ghost btn-sm !min-h-[24px] !px-2 !text-[11px]"
              aria-pressed={tableView}
              onClick={() => setTableView((v) => !v)}
              title="切换图形视图与可访问数值表"
            >
              <Table2 className="h-3 w-3" aria-hidden="true" />
              <span>{tableView ? "图形" : "数值"}</span>
            </button>
          </div>
        </div>

        {/* Tab Panel 1: Cross-Asset Anchors Momentum x Chanlun Shield */}
        <div
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-anchors`}
          hidden={activeTab !== "anchors"}
          className="pt-2.5"
        >
          <div className="flex flex-col gap-2">
            {/* Filter & Legend Row */}
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px]">
              <div className="flex flex-wrap items-center gap-1">
                <button
                  type="button"
                  className="chip !min-h-[20px] !px-2 !py-0 !text-[10px]"
                  aria-pressed={anchorGroup === "all"}
                  onClick={() => setAnchorGroup("all")}
                >
                  全资产({anchorItems.length})
                </button>
                {anchorGroups.map(([gKey, gLabel]) => (
                  <button
                    key={gKey}
                    type="button"
                    className="chip !min-h-[20px] !px-2 !py-0 !text-[10px]"
                    aria-pressed={anchorGroup === gKey}
                    onClick={() => setAnchorGroup(gKey)}
                  >
                    {gLabel}
                  </button>
                ))}
              </div>
              {/* Mandatory Legend for >= 2 Series (Color + Shape/Text Token) */}
              <div className="flex flex-wrap items-center gap-2.5 text-muted">
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--viz-cat-2)" }} />
                  护盾完好
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--viz-cat-4)" }} />
                  高位警戒
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--bear)" }} />
                  跌破ZD
                </span>
              </div>
            </div>

            {visibleAnchors.length === 0 ? (
              <div className="flex h-[150px] items-center justify-center text-xs text-muted">
                暂无跨资产定价锚快照数据
              </div>
            ) : tableView ? (
              <div className="max-h-[172px] overflow-auto rounded-md border border-hairline-soft">
                <table className="w-full border-collapse text-[11px] tabular-nums">
                  <thead className="sticky top-0 bg-sunken text-muted">
                    <tr>
                      <th className="px-2 py-1 text-left font-medium">锚点</th>
                      <th className="px-2 py-1 text-right font-medium">20D</th>
                      <th className="px-2 py-1 text-right font-medium">60D</th>
                      <th className="px-2 py-1 text-left font-medium">日线护盾</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleAnchors.map((it) => (
                      <tr
                        key={it.symbol}
                        className="cursor-pointer border-t border-hairline-soft hover:bg-elevated/70"
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                      >
                        <td className="px-2 py-1 font-medium text-ink">
                          {it.name} <span className="font-mono text-[10px] text-muted">{it.symbol}</span>
                        </td>
                        <td className="px-2 py-1 text-right font-mono text-ink">{fmtPct(it.ret_20d_pct, 1)}</td>
                        <td className="px-2 py-1 text-right font-mono text-muted">{fmtPct(it.ret_60d_pct, 1)}</td>
                        <td className="px-2 py-1 text-ink-2">
                          <span
                            className="mr-1.5 inline-block h-2 w-2 rounded-full"
                            style={{ background: shieldColorVar(it.chanlun_hint?.shield_state) }}
                          />
                          {shieldShortLabel(it.chanlun_hint?.shield_state)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : anchorView === "bars" ? (
              /* Diverging Horizontal Bar Chart (bar <= 10px, 4px data-end radius, square baseline, hairline grid) */
              <div className="flex flex-col gap-1.5">
                <svg
                  viewBox={`0 0 540 ${Math.max(140, visibleAnchors.length * 16 + 14)}`}
                  className="max-h-[176px] w-full overflow-visible"
                  role="img"
                  aria-label="跨资产定价锚20日动量与日线护盾分布图"
                >
                  {/* Zero baseline at x=344 leaves dedicated zone [160..222] for negative labels with zero symbol overlap */}
                  <line x1={344} y1={4} x2={344} y2={visibleAnchors.length * 16 + 8} stroke="var(--viz-grid)" strokeWidth={1} />
                  {visibleAnchors.map((it, idx) => {
                    const y = idx * 16 + 6;
                    const r20 = it.ret_20d_pct ?? 0;
                    const barW = Math.max(2, (Math.abs(r20) / maxAbsRet20) * 122);
                    const isPos = r20 >= 0;
                    const barX = isPos ? 344 : 344 - barW;
                    const fill = shieldColorVar(it.chanlun_hint?.shield_state);
                    const isHover = hoveredAnchor?.symbol === it.symbol;
                    const r = Math.min(4, barW / 2);
                    // Rounded only at data end, square at baseline (344)
                    const pathD = isPos
                      ? `M ${barX},${y} L ${barX + barW - r},${y} Q ${barX + barW},${y} ${barX + barW},${y + r} L ${barX + barW},${y + 10 - r} Q ${barX + barW},${y + 10} ${barX + barW - r},${y + 10} L ${barX},${y + 10} Z`
                      : `M ${barX + barW},${y} L ${barX + r},${y} Q ${barX},${y} ${barX},${y + r} L ${barX},${y + 10 - r} Q ${barX},${y + 10} ${barX + r},${y + 10} L ${barX + barW},${y + 10} Z`;

                    return (
                      <g
                        key={it.symbol}
                        className="cursor-pointer"
                        onMouseEnter={() => setHoveredAnchor(it)}
                        onMouseLeave={() => setHoveredAnchor((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                      >
                        <rect
                          x={0}
                          y={y - 2}
                          width={540}
                          height={15}
                          rx={3}
                          fill={isHover ? "var(--elevated)" : "transparent"}
                        />
                        <text
                          x={4}
                          y={y + 8.5}
                          fill="var(--ink)"
                          fontSize={10}
                          fontWeight={isHover ? 600 : 500}
                        >
                          {it.name.slice(0, 7)}
                        </text>
                        <text x={86} y={y + 8.5} fill="var(--muted)" fontSize={9} fontFamily="var(--font-mono)">
                          {it.symbol.length > 10 ? `${it.symbol.slice(0, 9)}…` : it.symbol}
                        </text>
                        <path d={pathD} fill={fill} opacity={isHover ? 1 : 0.86} />
                        <text
                          x={isPos ? barX + barW + 5 : barX - 5}
                          y={y + 8.5}
                          textAnchor={isPos ? "start" : "end"}
                          fill="var(--ink-2)"
                          fontSize={9.5}
                          fontFamily="var(--font-mono)"
                          style={{ fontVariantNumeric: "tabular-nums" }}
                        >
                          {fmtPct(r20, 1)}
                        </text>
                      </g>
                    );
                  })}
                </svg>
                {/* Fixed-height inspector footer prevents vertical layout shift (CLS = 0) on hover */}
                <div className="flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] text-ink-2">
                  {hoveredAnchor ? (
                    <>
                      <span className="font-medium text-ink">
                        {hoveredAnchor.name} ({hoveredAnchor.symbol}) · {shieldShortLabel(hoveredAnchor.chanlun_hint?.shield_state)}
                      </span>
                      <span className="font-mono tabular-nums text-muted">
                        20D {fmtPct(hoveredAnchor.ret_20d_pct, 1)} · 60D {fmtPct(hoveredAnchor.ret_60d_pct, 1)}
                        {hoveredAnchor.chanlun_hint?.zd != null
                          ? ` · ZD ${hoveredAnchor.chanlun_hint.zd.toFixed(2)}`
                          : ""}
                        {" · 点击进战情室"}
                      </span>
                    </>
                  ) : (
                    <span className="text-[10.5px] text-muted">
                      悬停任意条查看 20D/60D 与日线 ZD 防线 · 点击锚点直达战情室
                    </span>
                  )}
                </div>
              </div>
            ) : (
              /* 20D x 60D Momentum Quadrant Scatter (r=4.5 + 2px surface ring) */
              <div className="flex flex-col gap-1.5">
                <svg
                  viewBox="0 0 540 166"
                  className="h-[166px] w-full overflow-visible"
                  role="img"
                  aria-label="跨资产20日与60日动量象限散点图"
                >
                  {/* Hairline solid quadrant axes */}
                  <line x1={270} y1={10} x2={270} y2={148} stroke="var(--viz-grid)" strokeWidth={1} />
                  <line x1={24} y1={79} x2={516} y2={79} stroke="var(--viz-grid)" strokeWidth={1} />
                  <text x={512} y={20} textAnchor="end" fill="var(--muted)" fontSize={9}>
                    主升共振 (20D+ / 60D+)
                  </text>
                  <text x={28} y={144} textAnchor="start" fill="var(--muted)" fontSize={9}>
                    双周期走弱 (20D- / 60D-)
                  </text>
                  <text x={512} y={144} textAnchor="end" fill="var(--muted)" fontSize={9}>
                    超跌反抽 (20D+ / 60D-)
                  </text>
                  <text x={28} y={20} textAnchor="start" fill="var(--muted)" fontSize={9}>
                    高位回撤 (20D- / 60D+)
                  </text>
                  {visibleAnchors.map((it) => {
                    const r20 = it.ret_20d_pct ?? 0;
                    const r60 = it.ret_60d_pct ?? 0;
                    const cx = 270 + (clamp(r20, -maxAbsRet20, maxAbsRet20) / maxAbsRet20) * 230;
                    const cy = 79 - (clamp(r60, -maxAbsRet60, maxAbsRet60) / maxAbsRet60) * 62;
                    const fill = shieldColorVar(it.chanlun_hint?.shield_state);
                    const isHover = hoveredAnchor?.symbol === it.symbol;
                    const showDirectLabel = isHover || Math.abs(r20) >= maxAbsRet20 * 0.55;
                    return (
                      <g
                        key={it.symbol}
                        className="cursor-pointer"
                        onMouseEnter={() => setHoveredAnchor(it)}
                        onMouseLeave={() => setHoveredAnchor((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                      >
                        {/* 2px surface ring per dataviz spacer spec */}
                        <circle
                          cx={cx}
                          cy={cy}
                          r={isHover ? 6.5 : 4.5}
                          fill={fill}
                          stroke="var(--surface)"
                          strokeWidth={2}
                        />
                        {showDirectLabel ? (
                          <text
                            x={cx + (cx > 440 ? -8 : 8)}
                            y={cy + 3}
                            textAnchor={cx > 440 ? "end" : "start"}
                            fill="var(--ink)"
                            fontSize={9.5}
                            fontWeight={isHover ? 600 : 500}
                          >
                            {it.name.slice(0, 6)}
                          </text>
                        ) : null}
                      </g>
                    );
                  })}
                </svg>
                <div className="flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] text-ink-2">
                  {hoveredAnchor ? (
                    <>
                      <span className="font-medium text-ink">
                        {hoveredAnchor.name} ({hoveredAnchor.symbol}) · {shieldShortLabel(hoveredAnchor.chanlun_hint?.shield_state)}
                      </span>
                      <span className="font-mono tabular-nums text-muted">
                        20D {fmtPct(hoveredAnchor.ret_20d_pct, 1)} · 60D {fmtPct(hoveredAnchor.ret_60d_pct, 1)} · 点击进战情室
                      </span>
                    </>
                  ) : (
                    <span className="text-[10.5px] text-muted">
                      横轴 20D 动量 · 纵轴 60D 动量 · 悬停圆点查看详情，点击进战情室
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Tab Panel 2: CFTC 52-Week Crowding Track */}
        <div
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-cftc`}
          hidden={activeTab !== "cftc"}
          className="pt-2.5"
        >
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted">
              <span>
                CFTC 52周净持仓分位（0% 极空 → 100% 极多）· 最新报告 {macro.cftc_crowding?.latest_report_date || "—"}
              </span>
              <div className="flex items-center gap-2.5">
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--bear)" }} />
                  ≤10% 极空
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--viz-cat-4)" }} />
                  ≥90% 极多
                </span>
              </div>
            </div>

            {cftcItems.length === 0 ? (
              <div className="flex h-[150px] items-center justify-center text-xs text-muted">
                CFTC 持仓数据暂不可用或未就绪
              </div>
            ) : tableView ? (
              <div className="max-h-[172px] overflow-auto rounded-md border border-hairline-soft">
                <table className="w-full border-collapse text-[11px] tabular-nums">
                  <thead className="sticky top-0 bg-sunken text-muted">
                    <tr>
                      <th className="px-2 py-1 text-left font-medium">品种</th>
                      <th className="px-2 py-1 text-right font-medium">52W分位</th>
                      <th className="px-2 py-1 text-right font-medium">净持仓/OI</th>
                      <th className="px-2 py-1 text-left font-medium">拥挤状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cftcItems.map((it) => (
                      <tr key={it.symbol} className="border-t border-hairline-soft">
                        <td className="px-2 py-1 font-medium text-ink">
                          {it.label} <span className="font-mono text-[10px] text-muted">{it.symbol}</span>
                        </td>
                        <td className="px-2 py-1 text-right font-mono text-ink">
                          {it.percentile_52w?.toFixed(0)}%
                        </td>
                        <td className="px-2 py-1 text-right font-mono text-muted">
                          {it.net_pct_oi != null ? `${it.net_pct_oi >= 0 ? "+" : ""}${it.net_pct_oi.toFixed(1)}%` : "—"}
                        </td>
                        <td className="px-2 py-1 text-ink-2">{it.crowding_state || "neutral"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div>
                <svg
                  viewBox={`0 0 540 ${Math.max(136, cftcItems.length * 20 + 18)}`}
                  className="max-h-[174px] w-full overflow-visible"
                  role="img"
                  aria-label="CFTC 52周衍生品持仓拥挤度分位轨"
                >
                  {/* Shaded extreme bands <=10% and >=90% */}
                  <rect x={135} y={4} width={33} height={cftcItems.length * 20 + 6} rx={3} fill="var(--bear)" opacity={0.1} />
                  <rect x={432} y={4} width={33} height={cftcItems.length * 20 + 6} rx={3} fill="var(--viz-cat-4)" opacity={0.12} />
                  {/* Hairline 50% midpoint */}
                  <line x1={300} y1={4} x2={300} y2={cftcItems.length * 20 + 10} stroke="var(--viz-grid)" strokeWidth={1} />
                  {cftcItems.map((it, idx) => {
                    const y = idx * 20 + 14;
                    const p = clamp(it.percentile_52w ?? 50, 0, 100);
                    const cx = 135 + (p / 100) * 330;
                    const isLongExt = it.crowding_state === "crowded_long";
                    const isShortExt = it.crowding_state === "crowded_short";
                    const dotColor = isLongExt
                      ? "var(--viz-cat-4)"
                      : isShortExt
                        ? "var(--bear)"
                        : "var(--viz-cat-1)";
                    const isHover = hoveredCftc?.symbol === it.symbol;
                    return (
                      <g
                        key={it.symbol}
                        className="cursor-pointer"
                        onMouseEnter={() => setHoveredCftc(it)}
                        onMouseLeave={() => setHoveredCftc((prev) => (prev?.symbol === it.symbol ? null : prev))}
                      >
                        <rect
                          x={0}
                          y={y - 9}
                          width={540}
                          height={18}
                          rx={3}
                          fill={isHover ? "var(--elevated)" : "transparent"}
                        />
                        <text x={4} y={y + 3.5} fill="var(--ink)" fontSize={10.5} fontWeight={isHover ? 600 : 500}>
                          {it.label}
                        </text>
                        <text x={82} y={y + 3.5} fill="var(--muted)" fontSize={9} fontFamily="var(--font-mono)">
                          {it.symbol}
                        </text>
                        <line x1={135} y1={y} x2={465} y2={y} stroke="var(--viz-grid)" strokeWidth={2} strokeLinecap="round" />
                        <circle
                          cx={cx}
                          cy={y}
                          r={isHover ? 6 : 5}
                          fill={dotColor}
                          stroke="var(--surface)"
                          strokeWidth={2}
                          opacity={it.is_stale ? 0.45 : 1}
                        />
                        <text
                          x={476}
                          y={y + 3.5}
                          fill="var(--ink)"
                          fontSize={10}
                          fontFamily="var(--font-mono)"
                          style={{ fontVariantNumeric: "tabular-nums" }}
                        >
                          {p.toFixed(0)}%
                        </text>
                      </g>
                    );
                  })}
                </svg>
                <div className="mt-1.5 flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] text-ink-2">
                  {hoveredCftc ? (
                    <>
                      <span className="font-medium text-ink">
                        {hoveredCftc.label} ({hoveredCftc.symbol}) · {hoveredCftc.cohort || "Managed Money"}
                      </span>
                      <span className="font-mono tabular-nums text-muted">
                        52W {hoveredCftc.percentile_52w?.toFixed(1)}% · 净持仓/OI{" "}
                        {hoveredCftc.net_pct_oi != null ? `${hoveredCftc.net_pct_oi.toFixed(1)}%` : "—"} ·{" "}
                        {hoveredCftc.latest_date || ""}
                      </span>
                    </>
                  ) : (
                    <span className="text-[10.5px] text-muted">
                      悬停任意轨道查看基金净持仓占比与最新 CFTC 报告日期
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Tab Panel 3: Polymarket Macro Odds Horizontal Bars */}
        <div
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-polymarket`}
          hidden={activeTab !== "polymarket"}
          className="pt-2.5"
        >
          {pmEvents.length === 0 ? (
            <div className="flex h-[156px] items-center justify-center text-xs text-muted">
              Polymarket 宏观事件概率暂不可用
            </div>
          ) : tableView ? (
            <div className="max-h-[172px] overflow-auto rounded-md border border-hairline-soft">
              <table className="w-full border-collapse text-[11px] tabular-nums">
                <thead className="sticky top-0 bg-sunken text-muted">
                  <tr>
                    <th className="px-2 py-1 text-left font-medium">事件</th>
                    <th className="px-2 py-1 text-left font-medium">领跑选项</th>
                    <th className="px-2 py-1 text-right font-medium">概率</th>
                    <th className="px-2 py-1 text-right font-medium">7D变化</th>
                  </tr>
                </thead>
                <tbody>
                  {pmEvents.map((ev) => (
                    <tr key={ev.id} className="border-t border-hairline-soft">
                      <td className="px-2 py-1 text-ink">{ev.title}</td>
                      <td className="px-2 py-1 text-ink-2">{ev.leading_option}</td>
                      <td className="px-2 py-1 text-right font-mono text-ink">{ev.leading_prob_pct.toFixed(0)}%</td>
                      <td className="px-2 py-1 text-right font-mono text-muted">
                        {ev.leading_chg_7d_pp != null
                          ? `${ev.leading_chg_7d_pp >= 0 ? "+" : ""}${ev.leading_chg_7d_pp.toFixed(1)}pp`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {pmEvents.map((ev) => {
                const prob = clamp(ev.leading_prob_pct, 0, 100);
                const chg7 = ev.leading_chg_7d_pp;
                const bigShift = chg7 != null && Math.abs(chg7) >= 8;
                const barColor =
                  ev.category === "geopolitics"
                    ? "var(--viz-cat-4)"
                    : ev.category === "trade_tariffs"
                      ? "var(--viz-cat-3)"
                      : "var(--viz-cat-1)";
                return (
                  <div
                    key={ev.id}
                    className="flex flex-col gap-1 rounded-md px-1.5 py-1 transition-colors hover:bg-elevated/60"
                    onMouseEnter={() => setHoveredPm(ev)}
                    onMouseLeave={() => setHoveredPm((prev) => (prev?.id === ev.id ? null : prev))}
                  >
                    <div className="flex items-center justify-between gap-2 text-[11px]">
                      <div className="flex min-w-0 items-center gap-1.5">
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ background: barColor }}
                          aria-hidden="true"
                        />
                        <span className="truncate font-medium text-ink" title={ev.title}>
                          {ev.title}
                        </span>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        <span className="max-w-[110px] truncate text-[10px] text-muted">{ev.leading_option}</span>
                        <span className="font-mono text-[11px] font-semibold tabular-nums text-ink">
                          {prob.toFixed(0)}%
                        </span>
                        {chg7 != null ? (
                          <span
                            className={`rounded px-1 py-px font-mono text-[9.5px] tabular-nums ${
                              bigShift ? "bg-accent/14 font-semibold text-accent" : "text-muted"
                            }`}
                          >
                            {chg7 >= 0 ? "+" : ""}
                            {chg7.toFixed(1)}pp
                          </span>
                        ) : null}
                        {ev.url ? (
                          <a
                            href={ev.url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-muted hover:text-accent"
                            aria-label={`打开 Polymarket 事件：${ev.title}`}
                          >
                            <ExternalLink className="h-3 w-3" aria-hidden="true" />
                          </a>
                        ) : null}
                      </div>
                    </div>
                    <div className="h-2 w-full rounded-r-[4px] bg-subtle/75">
                      <div
                        className="h-full rounded-r-[4px]"
                        style={{ width: `${Math.max(2, prob)}%`, background: barColor, opacity: 0.85 }}
                      />
                    </div>
                  </div>
                );
              })}
              <div className="flex min-h-[26px] flex-wrap items-center gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[10.5px] text-muted">
                {hoveredPm && hoveredPm.top_outcomes?.length > 1 ? (
                  <>
                    <span className="font-medium text-ink">{hoveredPm.category_label}:</span>
                    {hoveredPm.top_outcomes.slice(0, 3).map((o, i) => (
                      <span key={i} className="font-mono tabular-nums text-ink-2">
                        {o.label} {o.prob_pct.toFixed(0)}%
                      </span>
                    ))}
                  </>
                ) : (
                  <span>悬停事件条对比多选项赔率分布 · 点击外链查看 Polymarket 盘口</span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function signalLightTone(level: string): {
  dot: string;
  badgeBg: string;
  Icon: typeof ShieldCheck;
} {
  if (level === "red") {
    return { dot: "var(--bear)", badgeBg: "bg-bear/12 text-bear border-bear/30", Icon: AlertTriangle };
  }
  if (level === "amber") {
    return { dot: "var(--warning)", badgeBg: "bg-warning/12 text-warning border-warning/30", Icon: TrendingDown };
  }
  if (level === "green") {
    return { dot: "var(--bull)", badgeBg: "bg-bull/12 text-bull border-bull/30", Icon: ShieldCheck };
  }
  return { dot: "var(--muted)", badgeBg: "bg-subtle text-muted border-hairline-soft", Icon: Activity };
}
