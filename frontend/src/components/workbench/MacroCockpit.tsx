import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ExternalLink,
  Flame,
  Globe2,
  MinusCircle,
  Scale,
  ShieldAlert,
  Table2,
  TrendingUp,
} from "lucide-react";
import type {
  CftcCrowdingBenchmarkItem,
  CompassMacroOverview,
  CrossAssetAnchorItem,
  MacroHeadlineMetric,
  PolymarketEventItem,
  RegimeSignalLight,
} from "../../api/types";
import { fmtPct } from "../../lib/format";

export function signalLightTone(level: RegimeSignalLight["level"]) {
  if (level === "red") {
    return {
      badgeBg: "border-bear/40 bg-bear/12 text-bear",
      Icon: Flame,
    };
  }
  if (level === "amber") {
    return {
      badgeBg: "border-warning/40 bg-warning/12 text-warning",
      Icon: AlertTriangle,
    };
  }
  if (level === "green") {
    return {
      badgeBg: "border-bull/40 bg-bull/12 text-bull",
      Icon: CheckCircle2,
    };
  }
  return {
    badgeBg: "border-hairline bg-subtle text-muted",
    Icon: MinusCircle,
  };
}

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
  if (Math.abs(m.delta) < 0.005) return null;
  const sign = m.delta >= 0 ? "+" : "";
  if (m.delta_unit === "%") return `${sign}${m.delta.toFixed(1)}%`;
  if (m.delta_unit === "pp") return `${sign}${m.delta.toFixed(2)}pp`;
  if (m.unit === "$/t") return `${sign}${Math.round(m.delta)}`;
  return `${sign}${m.delta.toFixed(1)}`;
}

function shieldBadgeInfo(state?: string | null): { label: string; cls: string } | null {
  if (state === "below_zd_breakdown") {
    return { label: "破ZD", cls: "border-bear/40 bg-bear/14 text-bear" };
  }
  if (state === "high_sell_above_zd_watch") {
    return { label: "警戒", cls: "border-warning/40 bg-warning/14 text-warning" };
  }
  if (state === "bullish_shield_intact") {
    return { label: "护盾✓", cls: "border-bull/40 bg-bull/14 text-bull" };
  }
  return null;
}

function shieldShortLabel(state?: string | null): string {
  if (state === "bullish_shield_intact") return "护盾完好";
  if (state === "high_sell_above_zd_watch") return "高位警戒";
  if (state === "below_zd_breakdown") return "跌破ZD";
  return "未算护盾";
}

function scatterQuadrantColor(r20: number, r60: number): string {
  if (r20 >= 0 && r60 >= 0) return "var(--bull)";
  if (r20 < 0 && r60 < 0) return "var(--bear)";
  return "var(--warning)";
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
  const pct = ratio !== null ? clamp(ratio * 100, 3, 97) : null;
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
      <div className="flex items-baseline justify-between gap-1.5 text-[11px]">
        <span className="truncate text-muted">{label}</span>
        <div className="flex shrink-0 items-baseline gap-1">
          {badgeText ? (
            <span className={`rounded px-1 py-px text-[11px] font-medium ${badgeCls}`}>{badgeText}</span>
          ) : null}
          <span className="font-mono text-[12px] font-semibold tabular-nums text-ink">{valueText}</span>
          {deltaText ? (
            <span className="font-mono text-[11px] tabular-nums text-muted">{deltaText}</span>
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
              className="absolute left-0 top-0 h-full rounded-l-full rounded-r-[4px] opacity-35"
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
  const [anchorGroup, setAnchorGroup] = useState<string>("macro_core");
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
      map.set(m.key.toUpperCase(), m);
      map.set(m.key.toLowerCase(), m);
    }
    return map;
  }, [macro]);

  const getMetric = (...keys: string[]): MacroHeadlineMetric | undefined => {
    for (const k of keys) {
      const hit = metricByKey.get(k) || metricByKey.get(k.toUpperCase()) || metricByKey.get(k.toLowerCase());
      if (hit) return hit;
    }
    return undefined;
  };

  const anchorItems = useMemo(
    () => (macro?.anchors?.items ?? []).filter((it) => it.status === "ok" && it.ret_20d_pct !== null),
    [macro],
  );

  const macroCoreItems = useMemo(() => {
    const core = anchorItems.filter((it) => it.tier === "macro_core");
    return core.length > 0 ? core : anchorItems;
  }, [anchorItems]);

  const hasTierHierarchy = useMemo(
    () => anchorItems.some((it) => it.tier === "macro_core"),
    [anchorItems],
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

  const activeAnchorPool = useMemo(
    () =>
      anchorGroup === "macro_core"
        ? macroCoreItems
        : anchorItems.filter((it) => it.group === anchorGroup),
    [anchorItems, macroCoreItems, anchorGroup],
  );

  /* Balanced selection for 20D bar chart: surface top gainers AND biggest losers cleanly within 8 visible rows */
  const barAnchors = useMemo(() => {
    const filtered = activeAnchorPool;
    if (anchorGroup !== "macro_core" || filtered.length <= 8) {
      return [...filtered].sort((a, b) => (b.ret_20d_pct ?? 0) - (a.ret_20d_pct ?? 0));
    }
    const pos = filtered
      .filter((it) => (it.ret_20d_pct ?? 0) > 0)
      .sort((a, b) => (b.ret_20d_pct ?? 0) - (a.ret_20d_pct ?? 0));
    const neg = filtered
      .filter((it) => (it.ret_20d_pct ?? 0) <= 0)
      .sort((a, b) => (a.ret_20d_pct ?? 0) - (b.ret_20d_pct ?? 0));
    const topPos = pos.slice(0, 3);
    const negBudget = Math.max(5, 8 - topPos.length);
    const topNeg = neg.slice(0, negBudget).reverse(); // milder drops above, biggest drop at bottom
    return [...topPos, ...topNeg];
  }, [activeAnchorPool, anchorGroup]);

  const scatterAnchors = useMemo(() => {
    const filtered = activeAnchorPool;
    return [...filtered].sort((a, b) => Math.abs(b.ret_20d_pct ?? 0) - Math.abs(a.ret_20d_pct ?? 0)).slice(0, 26);
  }, [activeAnchorPool]);

  const tableAnchors = useMemo(() => {
    const filtered = activeAnchorPool;
    return [...filtered].sort((a, b) => (b.ret_20d_pct ?? 0) - (a.ret_20d_pct ?? 0));
  }, [activeAnchorPool]);

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
    return raw.slice(0, 5);
  }, [macro]);

  const pmBigShifts = useMemo(
    () => pmEvents.filter((ev) => Math.abs(ev.leading_chg_7d_pp ?? 0) >= 8).length,
    [pmEvents],
  );

  const brokenShieldsCount = useMemo(
    () => anchorItems.filter((it) => it.chanlun_hint?.shield_state === "below_zd_breakdown").length,
    [anchorItems],
  );

  /* Canonical backend headline_metrics keys (uppercase) with lowercase fallbacks */
  const mReal = getMetric("DFII10", "real_yield_10y");
  const mBreakeven = getMetric("T10YIE", "breakeven_10y");
  const mNom = getMetric("US10Y", "DGS10", "us10y");
  const mHy = getMetric("HY_OAS", "BAMLH0A0HYM2", "hy_oas");
  const mDxy = getMetric("DXY", "dxy");
  const mVix = getMetric("VIX", "vix");
  const mFed = getMetric("FEDFUNDS", "fed_funds");
  const mPmi = getMetric("CN_PMI", "china_pmi");
  const mCopper = getMetric("PCOPPUSDM", "copper_global");

  const breadth = macro?.tape_movers?.regime_breadth;
  const advCount = breadth?.advancing_20d_count ?? 0;
  const decCount = breadth?.declining_20d_count ?? 0;
  const totalBreadth = Math.max(breadth?.total_available ?? advCount + decCount, 0);
  const flatCount = Math.max(0, totalBreadth - advCount - decCount);

  /* Diverging bar chart linear scale: allocate proportional track width without distorting bar lengths */
  const { negScale, posScale, zeroPct } = useMemo(() => {
    const maxNeg = Math.max(0, ...barAnchors.map((a) => ((a.ret_20d_pct ?? 0) < 0 ? Math.abs(a.ret_20d_pct ?? 0) : 0)));
    const maxPos = Math.max(0, ...barAnchors.map((a) => Math.max(a.ret_20d_pct ?? 0, 0)));
    const nScale = Math.max(maxNeg, 4);
    const pScale = Math.max(maxPos, 4);
    const total = nScale + pScale;
    const zPct = clamp((nScale / total) * 100, 16, 84);
    return { negScale: nScale, posScale: pScale, zeroPct: zPct };
  }, [barAnchors]);

  /* Scatter layout & deterministic collision-avoiding direct labels */
  const scatterPlot = useMemo(() => {
    const W = 660;
    const H = 184;
    const padX = 28;
    const padTop = 22;
    const padBot = 24;
    const plotW = W - padX * 2;
    const plotH = H - padTop - padBot;

    const r20Vals = scatterAnchors.map((a) => a.ret_20d_pct ?? 0);
    const r60Vals = scatterAnchors.map((a) => a.ret_60d_pct ?? 0);
    const maxAbsX = Math.max(10, ...r20Vals.map((v) => Math.abs(v))) * 1.08;
    const maxAbsY = Math.max(15, ...r60Vals.map((v) => Math.abs(v))) * 1.1;

    const midX = padX + plotW / 2;
    const midY = padTop + plotH / 2;

    const points = scatterAnchors.map((it) => {
      const r20 = it.ret_20d_pct ?? 0;
      const r60 = it.ret_60d_pct ?? 0;
      const cx = midX + (clamp(r20, -maxAbsX, maxAbsX) / maxAbsX) * (plotW / 2 - 12);
      const cy = midY - (clamp(r60, -maxAbsY, maxAbsY) / maxAbsY) * (plotH / 2 - 8);
      const radial = Math.hypot(r20 / maxAbsX, r60 / maxAbsY);
      return { it, r20, r60, cx, cy, radial };
    });

    /* Reserve bounding boxes for the 4 quadrant corner titles AND all plotted dots so labels never overlap OTHER dots */
    const occupied: { x1: number; y1: number; x2: number; y2: number; self?: string }[] = [
      { x1: padX - 4, y1: 4, x2: padX + 150, y2: 24 },
      { x1: W - padX - 150, y1: 4, x2: W - padX + 4, y2: 24 },
      { x1: padX - 4, y1: H - 24, x2: padX + 158, y2: H },
      { x1: W - padX - 150, y1: H - 24, x2: W - padX + 4, y2: H },
      ...points.map((pt) => ({ x1: pt.cx - 6, y1: pt.cy - 6, x2: pt.cx + 6, y2: pt.cy + 6, self: pt.it.symbol })),
    ];

    const directLabels = new Map<string, { x: number; y: number; anchor: "start" | "end" }>();
    const candidates = [...points].sort((a, b) => b.radial - a.radial);
    for (const pt of candidates) {
      if (directLabels.size >= 6) break;
      const labelText = pt.it.name.slice(0, 6);
      const estW = labelText.length * 10.5 + 8;
      const tryPositions: { x: number; y: number; anchor: "start" | "end"; x1: number; y1: number; x2: number; y2: number }[] = [
        { x: pt.cx + 8, y: pt.cy + 3, anchor: "start", x1: pt.cx + 7, y1: pt.cy - 8, x2: pt.cx + 7 + estW, y2: pt.cy + 6 },
        { x: pt.cx - 8, y: pt.cy + 3, anchor: "end", x1: pt.cx - 7 - estW, y1: pt.cy - 8, x2: pt.cx - 7, y2: pt.cy + 6 },
        { x: pt.cx + 8, y: pt.cy - 9, anchor: "start", x1: pt.cx + 7, y1: pt.cy - 20, x2: pt.cx + 7 + estW, y2: pt.cy - 6 },
        { x: pt.cx - 8, y: pt.cy - 9, anchor: "end", x1: pt.cx - 7 - estW, y1: pt.cy - 20, x2: pt.cx - 7, y2: pt.cy - 6 },
      ];
      for (const pos of tryPositions) {
        if (pos.x1 < 6 || pos.x2 > W - 6 || pos.y1 < 6 || pos.y2 > H - 4) continue;
        const collides = occupied.some(
          (b) =>
            b.self !== pt.it.symbol &&
            !(pos.x2 + 2 < b.x1 || pos.x1 - 2 > b.x2 || pos.y2 + 2 < b.y1 || pos.y1 - 2 > b.y2),
        );
        if (!collides) {
          occupied.push({ x1: pos.x1, y1: pos.y1, x2: pos.x2, y2: pos.y2 });
          directLabels.set(pt.it.symbol, { x: pos.x, y: pos.y, anchor: pos.anchor });
          break;
        }
      }
    }

    return { W, H, padX, padTop, padBot, midX, midY, points, directLabels };
  }, [scatterAnchors]);

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
        <div className="h-[248px] animate-pulse rounded-lg border border-hairline-soft bg-sunken lg:col-span-5" />
        <div className="h-[248px] animate-pulse rounded-lg border border-hairline-soft bg-sunken lg:col-span-7" />
      </div>
    );
  }

  if (!macro) return null;

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
      {/* Left Pane (5/12): 3 Semantic Macro Micro-Ruler Groups */}
      <div className="flex flex-col justify-between gap-2.5 rounded-lg border border-hairline-soft bg-sunken/70 p-3 lg:col-span-5">
        {/* Group 1: US Rates & Credit */}
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-1.5">
            <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-muted">
              <Scale className="h-3 w-3 text-accent" aria-hidden="true" />
              美债分母与信用地基
            </span>
            <span className="font-mono text-[11px] tabular-nums text-muted">
              10Y {fmtMetricVal(mNom)} · 通胀 {fmtMetricVal(mBreakeven)} · DXY {fmtMetricVal(mDxy)}
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
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
                    : "var(--bull)"
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
                    : "var(--bull)"
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
            <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-muted">
              <Activity className="h-3 w-3 text-accent" aria-hidden="true" />
              恐慌摆锤与跨资产宽度
            </span>
            <span className="font-mono text-[11px] tabular-nums text-muted">
              FedFunds {fmtMetricVal(mFed)}
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
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
                    : "var(--bull)"
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
            {/* Market Breadth Segmented Thermometer: Adv (green) on LEFT matching "3涨 / 11跌" */}
            <div
              className="flex flex-col gap-1"
              title={`${breadth?.scope === "macro_core" ? "宏观大类 " : ""}20D 跨资产定价锚宽度：上涨 ${advCount} / 持平 ${flatCount} / 下跌 ${decCount}（共 ${totalBreadth} 只）· ${breadth?.breadth_label || ""}`}
            >
              <div className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="truncate text-muted">20D 盘面宽度</span>
                <span className="font-mono text-[11px] font-semibold tabular-nums">
                  {totalBreadth > 0 ? (
                    <>
                      <span className="text-bull">{advCount}涨</span>
                      <span className="mx-1 text-muted">/</span>
                      <span className="text-bear">{decCount}跌</span>
                    </>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </span>
              </div>
              <div className="flex h-2 w-full items-center gap-[2px] overflow-hidden rounded-full bg-subtle/80">
                {totalBreadth > 0 ? (
                  <>
                    {advCount > 0 ? (
                      <span
                        className="h-full rounded-l-full"
                        style={{
                          width: `${(advCount / totalBreadth) * 100}%`,
                          background: "var(--bull)",
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
                    {decCount > 0 ? (
                      <span
                        className="h-full rounded-r-full"
                        style={{
                          width: `${(decCount / totalBreadth) * 100}%`,
                          background: "var(--bear)",
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
        <div className="grid grid-cols-1 items-center gap-2.5 sm:grid-cols-2">
          <MicroRulerRow
            label="中国制造业 PMI"
            valueText={fmtMetricVal(mPmi)}
            deltaText={fmtDeltaShort(mPmi)}
            ratio={mPmi?.value != null ? (mPmi.value - 47) / 6 : null}
            fillColor={
              mPmi?.value != null && mPmi.value >= 50.5
                ? "var(--bull)"
                : mPmi?.value != null && mPmi.value < 49.5
                  ? "var(--bear)"
                  : "var(--warning)"
            }
            ticks={[{ pos: 0.5 }]}
            tooltip={`中国官方制造业 PMI: ${fmtMetricVal(mPmi)} · 荣枯线 50.0 · 截至 ${mPmi?.as_of || "未知"} (${mPmi?.source || "国家统计局"})`}
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
            title={`全球铜价（工业实物周期定价锚）：${fmtMetricVal(mCopper)} · 截至 ${mCopper?.as_of || "未知"} (${mCopper?.source || "FRED"})`}
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
                <span className="font-mono text-[11px] tabular-nums text-muted">
                  {fmtDeltaShort(mCopper)}
                </span>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      {/* Right Pane (7/12): 3-Tab Visual Radar (Anchors / CFTC / Polymarket) */}
      <div className="flex min-w-0 flex-col rounded-lg border border-hairline-soft bg-surface p-3 lg:col-span-7">
        {/* Compact Single-Row Tablist Header + View Controls (fits cleanly at 1280px) */}
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
              className="mwg-tab !px-2"
              onClick={() => setActiveTab("anchors")}
              onKeyDown={(e) => handleTabKeyDown(e, "anchors")}
            >
              <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />
              <span>跨资产动量</span>
              {brokenShieldsCount > 0 ? (
                <span className="rounded bg-bear/14 px-1 py-px font-mono text-[11px] font-semibold text-bear">
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
              className="mwg-tab !px-2"
              onClick={() => setActiveTab("cftc")}
              onKeyDown={(e) => handleTabKeyDown(e, "cftc")}
            >
              <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
              <span>CFTC 52W</span>
              {cftcExtremesCount > 0 ? (
                <span className="rounded bg-warning/16 px-1 py-px font-mono text-[11px] font-semibold text-warning">
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
              className="mwg-tab !px-2"
              onClick={() => setActiveTab("polymarket")}
              onKeyDown={(e) => handleTabKeyDown(e, "polymarket")}
            >
              <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Polymarket</span>
              {pmBigShifts > 0 ? (
                <span className="rounded bg-accent/14 px-1 py-px font-mono text-[11px] font-semibold text-accent">
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
                  className="segmented-btn !min-h-[22px] !px-2 !text-[11px]"
                  aria-pressed={anchorView === "bars"}
                  onClick={() => setAnchorView("bars")}
                >
                  20D排行
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
          className="pt-2"
        >
          <div className="flex flex-col gap-2">
            {/* Filter & Honest Legend Row */}
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px]">
              <div className="flex flex-wrap items-center gap-1">
                <button
                  type="button"
                  className="chip !min-h-[20px] !px-2 !py-0 !text-[10px]"
                  aria-pressed={anchorGroup === "macro_core"}
                  onClick={() => setAnchorGroup("macro_core")}
                >
                  {hasTierHierarchy ? `宏观大类(${macroCoreItems.length})` : `全资产(${anchorItems.length})`}
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
              {anchorView === "bars" ? (
                <div className="flex flex-wrap items-center gap-2 text-muted">
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: "var(--bull)" }} />
                    20D上涨
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: "var(--bear)" }} />
                    20D下跌
                  </span>
                  <span className="text-[11px] text-muted">· 破ZD = 跌破日线中枢</span>
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2 text-muted">
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: "var(--bull)" }} />
                    主升
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: "var(--warning)" }} />
                    切换
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: "var(--bear)" }} />
                    双弱
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="h-2.5 w-2.5 rounded-full border border-dashed border-bear" />
                    破ZD
                  </span>
                </div>
              )}
            </div>

            {barAnchors.length === 0 ? (
              <div className="flex h-[156px] items-center justify-center text-xs text-muted">
                暂无跨资产定价锚快照数据
              </div>
            ) : tableView ? (
              <div className="max-h-[196px] overflow-auto rounded-md border border-hairline-soft">
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
                    {tableAnchors.map((it) => {
                      const badge = shieldBadgeInfo(it.chanlun_hint?.shield_state);
                      const r20 = it.ret_20d_pct ?? 0;
                      return (
                        <tr
                          key={it.symbol}
                          tabIndex={0}
                          className="cursor-pointer border-t border-hairline-soft hover:bg-elevated/70"
                          onClick={() => onJumpWarRoom?.(it.symbol)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              onJumpWarRoom?.(it.symbol);
                            }
                          }}
                        >
                          <td className="px-2 py-1 font-medium text-ink">
                            {it.name} <span className="font-mono text-[11px] text-muted">{it.symbol}</span>
                          </td>
                          <td
                            className={`px-2 py-1 text-right font-mono font-medium ${
                              r20 >= 0 ? "text-bull" : "text-bear"
                            }`}
                          >
                            {fmtPct(it.ret_20d_pct, 1)}
                          </td>
                          <td className="px-2 py-1 text-right font-mono text-muted">
                            {fmtPct(it.ret_60d_pct, 1)}
                          </td>
                          <td className="px-2 py-1 text-ink-2">
                            {badge ? (
                              <span className={`rounded border px-1 py-px text-[11px] font-medium ${badge.cls}`}>
                                {badge.label}
                              </span>
                            ) : (
                              <span className="text-[11px] text-muted">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : anchorView === "bars" ? (
              /* Full-width crisp HTML diverging bar chart (zero SVG letterboxing, balanced gainers & decliners) */
              <div className="flex flex-col gap-1">
                <div className="flex max-h-[192px] flex-col gap-[2px] overflow-y-auto pr-0.5">
                  {barAnchors.map((it) => {
                    const r20 = it.ret_20d_pct ?? 0;
                    const isPos = r20 >= 0;
                    const badge = shieldBadgeInfo(it.chanlun_hint?.shield_state);
                    const negRatio = !isPos ? clamp((Math.abs(r20) / negScale) * 100, 3, 100) : 0;
                    const posRatio = isPos ? clamp((r20 / posScale) * 100, 4, 100) : 0;
                    const isHover = hoveredAnchor?.symbol === it.symbol;

                    return (
                      <button
                        key={it.symbol}
                        type="button"
                        onMouseEnter={() => setHoveredAnchor(it)}
                        onMouseLeave={() => setHoveredAnchor((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                        className={`flex w-full items-center gap-2 rounded px-1.5 py-[2.5px] text-left transition-colors ${
                          isHover ? "bg-elevated" : "hover:bg-elevated/60"
                        }`}
                      >
                        <div className="flex w-[124px] shrink-0 items-baseline justify-between gap-1">
                          <span className="truncate text-[11px] font-medium text-ink">{it.name}</span>
                          <span className="shrink-0 font-mono text-[11px] text-muted">
                            {it.symbol.replace(/=X$|-USD$|\.SS$/i, "")}
                          </span>
                        </div>

                        {/* Proportional diverging bar track with linear scale */}
                        <div className="relative flex h-3.5 flex-1 items-center">
                          {/* Negative half */}
                          <div
                            className="flex h-full items-center justify-end pr-[1px]"
                            style={{ width: `${zeroPct}%` }}
                          >
                            {!isPos ? (
                              <span
                                className="h-[8px] rounded-l-[4px] transition-opacity"
                                style={{
                                  width: `${negRatio}%`,
                                  background: "var(--bear)",
                                  opacity: isHover ? 1 : 0.85,
                                }}
                              />
                            ) : null}
                          </div>
                          {/* Center zero hairline */}
                          <span className="h-full w-px shrink-0 bg-hairline" aria-hidden="true" />
                          {/* Positive half */}
                          <div
                            className="flex h-full items-center justify-start pl-[1px]"
                            style={{ width: `${100 - zeroPct}%` }}
                          >
                            {isPos ? (
                              <span
                                className="h-[8px] rounded-r-[4px] transition-opacity"
                                style={{
                                  width: `${posRatio}%`,
                                  background: "var(--bull)",
                                  opacity: isHover ? 1 : 0.88,
                                }}
                              />
                            ) : null}
                          </div>
                        </div>

                        <div className="flex w-[94px] shrink-0 items-center justify-end gap-1.5">
                          {badge ? (
                            <span className={`rounded border px-1 py-px text-[11px] font-semibold leading-none ${badge.cls}`}>
                              {badge.label}
                            </span>
                          ) : null}
                          <span
                            className={`font-mono text-[11px] font-medium tabular-nums ${
                              isPos ? "text-bull" : "text-bear"
                            }`}
                          >
                            {fmtPct(r20, 1)}
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>

                {/* Fixed-height inspector footer prevents vertical layout shift (CLS = 0) */}
                <div className="mt-0.5 flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] text-ink-2">
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
                      {anchorGroup === "all" && anchorItems.length > barAnchors.length
                        ? `展示最强领涨 + 最大领跌共 ${barAnchors.length}/${anchorItems.length} 锚 · 悬停看 60D/ZD，点击进战情室`
                        : "悬停任意锚点查看 20D/60D 与日线 ZD 防线 · 点击直达战情室"}
                    </span>
                  )}
                </div>
              </div>
            ) : (
              /* Full-width 20D x 60D Quadrant Scatter with collision-free labels */
              <div className="flex flex-col gap-1.5">
                <svg
                  viewBox={`0 0 ${scatterPlot.W} ${scatterPlot.H}`}
                  className="h-[184px] w-full overflow-visible"
                  role="img"
                  aria-label="跨资产20日与60日动量象限散点图"
                >
                  {/* Hairline solid quadrant axes */}
                  <line
                    x1={scatterPlot.midX}
                    y1={scatterPlot.padTop}
                    x2={scatterPlot.midX}
                    y2={scatterPlot.H - scatterPlot.padBot}
                    stroke="var(--viz-grid)"
                    strokeWidth={1}
                  />
                  <line
                    x1={scatterPlot.padX}
                    y1={scatterPlot.midY}
                    x2={scatterPlot.W - scatterPlot.padX}
                    y2={scatterPlot.midY}
                    stroke="var(--viz-grid)"
                    strokeWidth={1}
                  />
                  {/* Subtle corner quadrant labels tucked cleanly outside point bounds */}
                  <text x={scatterPlot.W - scatterPlot.padX} y={15} textAnchor="end" fill="var(--muted)" fontSize={9.5}>
                    主升共振 (20D+ / 60D+)
                  </text>
                  <text x={scatterPlot.padX} y={15} textAnchor="start" fill="var(--muted)" fontSize={9.5}>
                    高位回撤 (20D- / 60D+)
                  </text>
                  <text x={scatterPlot.padX} y={scatterPlot.H - 7} textAnchor="start" fill="var(--muted)" fontSize={9.5}>
                    双周期走弱 (20D- / 60D-)
                  </text>
                  <text x={scatterPlot.W - scatterPlot.padX} y={scatterPlot.H - 7} textAnchor="end" fill="var(--muted)" fontSize={9.5}>
                    超跌反抽 (20D+ / 60D-)
                  </text>

                  {scatterPlot.points.map(({ it, r20, r60, cx, cy }) => {
                    const fill = scatterQuadrantColor(r20, r60);
                    const broken = it.chanlun_hint?.shield_state === "below_zd_breakdown";
                    const isHover = hoveredAnchor?.symbol === it.symbol;
                    const labelPos = scatterPlot.directLabels.get(it.symbol);

                    return (
                      <g
                        key={it.symbol}
                        role="button"
                        tabIndex={0}
                        aria-label={`${it.name} (${it.symbol}) 跳转战情室`}
                        className="cursor-pointer"
                        onMouseEnter={() => setHoveredAnchor(it)}
                        onMouseLeave={() => setHoveredAnchor((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onFocus={() => setHoveredAnchor(it)}
                        onBlur={() => setHoveredAnchor((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onJumpWarRoom?.(it.symbol);
                          }
                        }}
                      >
                        {broken ? (
                          <circle
                            cx={cx}
                            cy={cy}
                            r={isHover ? 9.5 : 7.5}
                            fill="none"
                            stroke="var(--bear)"
                            strokeWidth={1.2}
                            strokeDasharray="2 1.5"
                            opacity={0.85}
                          />
                        ) : null}
                        <circle
                          cx={cx}
                          cy={cy}
                          r={isHover ? 6.5 : 4.5}
                          fill={fill}
                          stroke="var(--surface)"
                          strokeWidth={2}
                        />
                        {labelPos && !isHover ? (
                          <text
                            x={labelPos.x}
                            y={labelPos.y}
                            textAnchor={labelPos.anchor}
                            fill="var(--ink-2)"
                            fontSize={11}
                            fontWeight={500}
                          >
                            {it.name.slice(0, 6)}
                          </text>
                        ) : null}
                      </g>
                    );
                  })}

                  {/* Hovered dot floating label always on top with surface pill halo */}
                  {hoveredAnchor
                    ? (() => {
                        const hit = scatterPlot.points.find((p) => p.it.symbol === hoveredAnchor.symbol);
                        if (!hit) return null;
                        const label = `${hit.it.name.slice(0, 7)} ${fmtPct(hit.r20, 1)}`;
                        const flipLeft = hit.cx > scatterPlot.W - 140;
                        const tx = flipLeft ? hit.cx - 10 : hit.cx + 10;
                        return (
                          <g pointerEvents="none">
                            <text
                              x={tx}
                              y={hit.cy + 3.5}
                              textAnchor={flipLeft ? "end" : "start"}
                              fill="var(--ink)"
                              stroke="var(--surface)"
                              strokeWidth={3.5}
                              paintOrder="stroke"
                              fontSize={10.5}
                              fontWeight={600}
                            >
                              {label}
                            </text>
                          </g>
                        );
                      })()
                    : null}
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
                      横轴 20D 动量 · 纵轴 60D 动量 · 虚线外环代表日线破 ZD · 悬停圆点看详情
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Tab Panel 2: CFTC 52-Week Crowding Track (Full-width crisp HTML rows, zero SVG letterboxing) */}
        <div
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-cftc`}
          hidden={activeTab !== "cftc"}
          className="pt-2"
        >
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted">
              <span>
                52周净持仓分位（0% 极空 → 100% 极多）· 最新报告 {macro.cftc_crowding?.latest_report_date || "—"}
              </span>
              <div className="flex items-center gap-2.5">
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--bear)" }} />
                  ≤10% 极空
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: "var(--warning)" }} />
                  ≥90% 极多
                </span>
              </div>
            </div>

            {cftcItems.length === 0 ? (
              <div className="flex h-[156px] items-center justify-center text-xs text-muted">
                CFTC 持仓数据暂不可用或未就绪
              </div>
            ) : tableView ? (
              <div className="max-h-[196px] overflow-auto rounded-md border border-hairline-soft">
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
              <div className="flex flex-col gap-1">
                <div className="flex max-h-[190px] flex-col gap-[3px] overflow-y-auto pr-0.5">
                  {cftcItems.map((it) => {
                    const p = clamp(it.percentile_52w ?? 50, 0, 100);
                    const isLongExt = it.crowding_state === "crowded_long";
                    const isShortExt = it.crowding_state === "crowded_short";
                    const dotColor = isLongExt
                      ? "var(--warning)"
                      : isShortExt
                        ? "var(--bear)"
                        : "var(--viz-cat-1)";
                    const isHover = hoveredCftc?.symbol === it.symbol;

                    return (
                      <button
                        key={it.symbol}
                        type="button"
                        onMouseEnter={() => setHoveredCftc(it)}
                        onMouseLeave={() => setHoveredCftc((prev) => (prev?.symbol === it.symbol ? null : prev))}
                        onClick={() => onJumpWarRoom?.(it.symbol)}
                        className={`flex w-full cursor-pointer items-center gap-2.5 rounded px-1.5 py-1 text-left transition-colors ${
                          isHover ? "bg-elevated" : "hover:bg-elevated/60"
                        } ${it.is_stale ? "opacity-80" : ""}`}
                      >
                        <div className="flex w-[124px] shrink-0 items-baseline justify-between gap-1">
                          <span className="truncate text-[11px] font-medium text-ink">{it.label}</span>
                          <span className="shrink-0 font-mono text-[11px] text-muted">
                            {it.symbol.replace(/=X$/i, "")}
                          </span>
                        </div>

                        {/* Full-width 0..100% crowding track */}
                        <div className="relative flex h-3.5 flex-1 items-center">
                          <span
                            aria-hidden="true"
                            className="absolute inset-y-0 left-0 rounded-l bg-bear/12"
                            style={{ width: "10%" }}
                          />
                          <span
                            aria-hidden="true"
                            className="absolute inset-y-0 right-0 rounded-r bg-warning/14"
                            style={{ width: "10%" }}
                          />
                          <span
                            aria-hidden="true"
                            className="absolute inset-x-0 top-1/2 h-[2px] -translate-y-1/2 rounded-full bg-hairline"
                          />
                          <span
                            aria-hidden="true"
                            className="absolute left-1/2 top-0.5 bottom-0.5 w-px bg-hairline"
                          />
                          <span
                            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface shadow-xs transition-transform"
                            style={{
                              left: `${clamp(p, 2, 98)}%`,
                              background: dotColor,
                              transform: `translate(-50%, -50%) scale(${isHover ? 1.18 : 1})`,
                            }}
                          />
                        </div>

                        <div className="flex w-[92px] shrink-0 items-center justify-end gap-1.5">
                          {it.is_stale ? (
                            <span className="rounded bg-subtle px-1 py-px text-[11px] text-muted" title="该品种 CFTC 报告日期滞后，未计入极端拥挤警报">
                              滞后
                            </span>
                          ) : isShortExt ? (
                            <span className="rounded bg-bear/14 px-1 py-px text-[11px] font-semibold text-bear">
                              极空
                            </span>
                          ) : isLongExt ? (
                            <span className="rounded bg-warning/16 px-1 py-px text-[11px] font-semibold text-warning">
                              极多
                            </span>
                          ) : null}
                          <span className="font-mono text-[11px] font-medium tabular-nums text-ink">
                            {p.toFixed(0)}%
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>

                <div className="mt-0.5 flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] text-ink-2">
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
                      悬停任意轨道查看基金净持仓占比与最新 CFTC 报告日期 · 点击直达战情室
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
          className="pt-2"
        >
          {pmEvents.length === 0 ? (
            <div className="flex h-[156px] items-center justify-center text-xs text-muted">
              Polymarket 宏观事件概率暂不可用
            </div>
          ) : tableView ? (
            <div className="max-h-[196px] overflow-auto rounded-md border border-hairline-soft">
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
            <div className="flex flex-col gap-1.5">
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
                    className="flex flex-col gap-1 rounded-md px-1.5 py-0.5 transition-colors hover:bg-elevated/60"
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
                        <span className="max-w-[110px] truncate text-[11px] text-muted">{ev.leading_option}</span>
                        <span className="font-mono text-[11px] font-semibold tabular-nums text-ink">
                          {prob.toFixed(0)}%
                        </span>
                        {chg7 != null ? (
                          <span
                            className={`rounded px-1 py-px font-mono text-[11px] tabular-nums ${
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
                            rel="noreferrer noopener"
                            className="text-muted hover:text-accent"
                            title="在 Polymarket 打开原市场"
                            aria-label={`打开 ${ev.title}`}
                          >
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        ) : null}
                      </div>
                    </div>
                    <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-subtle/80">
                      <span
                        className="block h-full rounded-r-[4px]"
                        style={{ width: `${prob}%`, background: barColor, opacity: 0.85 }}
                      />
                    </div>
                  </div>
                );
              })}

              <div className="mt-0.5 flex min-h-[26px] flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 text-[10.5px] text-muted">
                {hoveredPm ? (
                  <>
                    <span className="truncate font-medium text-ink">{hoveredPm.title}</span>
                    <span className="shrink-0 font-mono tabular-nums">
                      领跑：{hoveredPm.leading_option} ({hoveredPm.leading_prob_pct.toFixed(1)}%)
                    </span>
                  </>
                ) : (
                  <span>真金白银预测市场隐含赔率 · 7D 变化 ≥ 8pp 标亮异动</span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
