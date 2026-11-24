import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type Time,
} from "lightweight-charts";
import { Crosshair, Layers, Maximize2, Table2 } from "lucide-react";
import type { ChanlunTimeframe, SignalSummary } from "../../api/types";
import { fmtFixed, fmtMomentumVerdict, fmtPct, fmtPosVsZs, fmtPrice } from "../../lib/format";
import { useTheme } from "../../lib/theme";
import { ZhongshuBoxesPrimitive } from "./ZhongshuBoxesPrimitive";
import { prepareChanlunSeries, timeKey, type AnyBar, type IndexedBar } from "./chanlunUtils";

function computeSeriesPriceFormat(bars: IndexedBar[]) {
  const closes = bars
    .map((b) => b.close)
    .filter((v) => typeof v === "number" && Number.isFinite(v) && v > 0)
    .sort((a, b) => a - b);
  const medianPrice = closes.length > 0 ? (closes[Math.floor(closes.length / 2)] ?? 100) : 100;
  if (medianPrice < 2) {
    return {
      price: { type: "price" as const, precision: 4, minMove: 0.0001 },
      macd: { type: "price" as const, precision: 5, minMove: 0.00001 },
      macdDp: 5,
    };
  }
  if (medianPrice < 20) {
    return {
      price: { type: "price" as const, precision: 3, minMove: 0.001 },
      macd: { type: "price" as const, precision: 4, minMove: 0.0001 },
      macdDp: 4,
    };
  }
  return {
    price: { type: "price" as const, precision: 2, minMove: 0.01 },
    macd: { type: "price" as const, precision: 3, minMove: 0.001 },
    macdDp: 3,
  };
}

export interface PlanOverlayLines {
  entry?: number | null;
  stop30m?: number | null;
  target?: number | null;
}

export interface ChanlunChartProps {
  title: string;
  bars: AnyBar[];
  chanlun: ChanlunTimeframe | undefined;
  signalSummary?: SignalSummary;
  intraday?: boolean;
  active?: boolean;
  height?: number;
  planOverlay?: PlanOverlayLines | null;
  pickHint?: string | null;
  onPickPrice?: ((price: number, bar: IndexedBar | null) => void) | null;
  syncDate?: string | null;
  onHoverDate?: ((ymd: string | null) => void) | null;
}

export function ChanlunChart({
  title,
  bars,
  chanlun,
  signalSummary,
  intraday = false,
  active = true,
  height = 420,
  planOverlay = null,
  pickHint = null,
  onPickPrice = null,
  syncDate = null,
  onHoverDate = null,
}: ChanlunChartProps) {
  const { palette } = useTheme();
  const containerRef = useRef<HTMLDivElement | null>(null);

  /* Interactive layer visibility toggles (stroke <-> 笔中枢, segment <-> 线段中枢) */
  const [showStrokes, setShowStrokes] = useState(true);
  const [showStrokeZhongshu, setShowStrokeZhongshu] = useState(true);
  const [showSegments, setShowSegments] = useState(false);
  const [showSegmentZhongshu, setShowSegmentZhongshu] = useState(false);
  const [showMarkers, setShowMarkers] = useState(true);
  const [showTable, setShowTable] = useState(false);

  const prepared = useMemo(
    () => prepareChanlunSeries(bars, chanlun, signalSummary, palette, intraday),
    [bars, chanlun, signalSummary, palette, intraday],
  );
  const visibleZhongshuBoxes = useMemo(
    () => [
      ...(showStrokeZhongshu ? prepared.strokeZhongshuBoxes : []),
      ...(showSegmentZhongshu ? prepared.segmentZhongshuBoxes : []),
    ],
    [prepared.strokeZhongshuBoxes, prepared.segmentZhongshuBoxes, showStrokeZhongshu, showSegmentZhongshu],
  );
  const tableZhongshuBoxes = useMemo(
    () => [
      ...prepared.strokeZhongshuBoxes.slice(-6),
      ...prepared.segmentZhongshuBoxes.slice(-6),
    ],
    [prepared.strokeZhongshuBoxes, prepared.segmentZhongshuBoxes],
  );
  const dynamicFormats = useMemo(() => computeSeriesPriceFormat(prepared.bars), [prepared.bars]);

  const [hoveredBar, setHoveredBar] = useState<IndexedBar | null>(null);
  const displayBar = hoveredBar ?? prepared.bars[prepared.bars.length - 1] ?? null;

  /* Persistent Lightweight Charts v5 refs */
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick", Time> | null>(null);
  const strokeConfirmedRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const strokeUnconfirmedRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const segmentConfirmedPoolRef = useRef<ISeriesApi<"Line", Time>[]>([]);
  const segmentUnconfirmedRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const macdHistRef = useRef<ISeriesApi<"Histogram", Time> | null>(null);
  const macdDifRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const macdDeaRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const zsPrimitiveRef = useRef<ZhongshuBoxesPrimitive | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const prevBarCountRef = useRef<number>(0);
  const prevFirstTimeRef = useRef<string>("");
  const byTimeKeyRef = useRef(prepared.byTimeKey);
  byTimeKeyRef.current = prepared.byTimeKey;
  const onPickPriceRef = useRef(onPickPrice);
  onPickPriceRef.current = onPickPrice;
  const onHoverDateRef = useRef(onHoverDate);
  onHoverDateRef.current = onHoverDate;
  const programmaticCrosshairRef = useRef(false);

  /* Map YYYY-MM-DD -> latest bar of that date for Daily <-> Intraday crosshair linkage */
  const barByDateMap = useMemo(() => {
    const m = new Map<string, IndexedBar>();
    for (const b of prepared.bars) {
      const ymd = b.label.slice(0, 10);
      if (/^\d{4}-\d{2}-\d{2}$/.test(ymd)) {
        m.set(ymd, b);
      }
    }
    return m;
  }, [prepared.bars]);

  /* Mount Lightweight Charts v5 instance once per canvas container */
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: el.clientWidth || 600,
      height,
      layout: {
        background: { type: ColorType.Solid, color: palette.surface },
        textColor: palette.muted,
        fontFamily: '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace',
        fontSize: 10.5,
        panes: {
          separatorColor: palette.gridline,
          separatorHoverColor: palette.accent,
          enableResize: true,
        },
      },
      grid: {
        vertLines: { color: palette.gridline, style: LineStyle.Dotted },
        horzLines: { color: palette.gridline, style: LineStyle.Dotted },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: palette.muted, width: 1, style: LineStyle.Dashed, labelBackgroundColor: palette.elevated },
        horzLine: { color: palette.muted, width: 1, style: LineStyle.Dashed, labelBackgroundColor: palette.elevated },
      },
      rightPriceScale: {
        borderColor: palette.gridline,
        scaleMargins: { top: 0.08, bottom: 0.08 },
      },
      timeScale: {
        borderColor: palette.gridline,
        timeVisible: intraday,
        secondsVisible: false,
        rightOffset: 4,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: palette.candleUp,
      downColor: palette.candleDown,
      wickUpColor: palette.candleUp,
      wickDownColor: palette.candleDown,
      borderVisible: false,
      priceLineVisible: true,
      lastValueVisible: true,
    });

    const zsPrimitive = new ZhongshuBoxesPrimitive(palette);
    candleSeries.attachPrimitive(zsPrimitive);

    /* Strokes (笔): neutral understated silver skeleton so Zhongshu & Segments own color hierarchy */
    const strokeConfirmed = chart.addSeries(LineSeries, {
      color: palette.strokeConfirmed,
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const strokeUnconfirmed = chart.addSeries(LineSeries, {
      color: palette.strokeUnconfirmed,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    /* Segments (线段): dynamic pool of confirmed segment chains + uncertain dashed tail */
    const segmentUnconfirmed = chart.addSeries(LineSeries, {
      color: palette.segUnconfirmed,
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const markersPlugin = createSeriesMarkers(candleSeries, [], { zOrder: "aboveSeries" });

    const macdPane = chart.addPane();
    chart.panes()[0]?.setStretchFactor(0.74);
    macdPane.setStretchFactor(0.26);

    const macdHist = macdPane.addSeries(HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
      base: 0,
    });
    const macdDif = macdPane.addSeries(LineSeries, {
      color: palette.slots[0],
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const macdDea = macdPane.addSeries(LineSeries, {
      color: palette.slots[3],
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const onCrosshair = (param: MouseEventParams<Time>) => {
      if (programmaticCrosshairRef.current) return;
      if (!param.time || !param.point) {
        setHoveredBar(null);
        onHoverDateRef.current?.(null);
        return;
      }
      const hit = byTimeKeyRef.current.get(timeKey(param.time)) ?? null;
      setHoveredBar(hit);
      if (hit) {
        const ymd = hit.label.slice(0, 10);
        onHoverDateRef.current?.(/^\d{4}-\d{2}-\d{2}$/.test(ymd) ? ymd : null);
      } else {
        onHoverDateRef.current?.(null);
      }
    };
    chart.subscribeCrosshairMove(onCrosshair);

    const onClick = (param: MouseEventParams<Time>) => {
      const cb = onPickPriceRef.current;
      if (!cb || !param.point) return;
      const bar = param.time ? (byTimeKeyRef.current.get(timeKey(param.time)) ?? null) : null;
      const coordPrice = candleSeries.coordinateToPrice(param.point.y);
      const picked =
        typeof coordPrice === "number" && Number.isFinite(coordPrice) && coordPrice > 0
          ? coordPrice
          : bar?.close ?? null;
      if (picked !== null && picked > 0) {
        cb(picked, bar);
      }
    };
    chart.subscribeClick(onClick);

    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0) {
        chart.applyOptions({ width: Math.floor(rect.width) });
      }
    });
    ro.observe(el);

    chartRef.current = chart;
    candleRef.current = candleSeries;
    strokeConfirmedRef.current = strokeConfirmed;
    strokeUnconfirmedRef.current = strokeUnconfirmed;
    segmentConfirmedPoolRef.current = [];
    segmentUnconfirmedRef.current = segmentUnconfirmed;
    macdHistRef.current = macdHist;
    macdDifRef.current = macdDif;
    macdDeaRef.current = macdDea;
    markersRef.current = markersPlugin;
    zsPrimitiveRef.current = zsPrimitive;

    return () => {
      ro.disconnect();
      chart.unsubscribeCrosshairMove(onCrosshair);
      chart.unsubscribeClick(onClick);
      markersPlugin.detach();
      candleSeries.detachPrimitive(zsPrimitive);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      strokeConfirmedRef.current = null;
      strokeUnconfirmedRef.current = null;
      segmentConfirmedPoolRef.current = [];
      segmentUnconfirmedRef.current = null;
      macdHistRef.current = null;
      macdDifRef.current = null;
      macdDeaRef.current = null;
      markersRef.current = null;
      zsPrimitiveRef.current = null;
      priceLinesRef.current = [];
      prevBarCountRef.current = 0;
      prevFirstTimeRef.current = "";
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intraday, height]);

  /* Recalculate width cleanly when Keep-Alive screen transitions back to visible */
  useEffect(() => {
    if (!active) return;
    const el = containerRef.current;
    const chart = chartRef.current;
    if (el && chart && el.clientWidth > 0) {
      chart.applyOptions({ width: Math.floor(el.clientWidth) });
    }
  }, [active]);

  /* Sync theme palette dynamically */
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: palette.surface },
        textColor: palette.muted,
        panes: {
          separatorColor: palette.gridline,
          separatorHoverColor: palette.accent,
        },
      },
      grid: {
        vertLines: { color: palette.gridline },
        horzLines: { color: palette.gridline },
      },
      rightPriceScale: { borderColor: palette.gridline },
      timeScale: { borderColor: palette.gridline },
      crosshair: {
        vertLine: { color: palette.muted, labelBackgroundColor: palette.elevated },
        horzLine: { color: palette.muted, labelBackgroundColor: palette.elevated },
      },
    });
    candleRef.current?.applyOptions({
      upColor: palette.candleUp,
      downColor: palette.candleDown,
      wickUpColor: palette.candleUp,
      wickDownColor: palette.candleDown,
    });
    strokeConfirmedRef.current?.applyOptions({ color: palette.strokeConfirmed });
    strokeUnconfirmedRef.current?.applyOptions({ color: palette.strokeUnconfirmed });
    for (const segSeries of segmentConfirmedPoolRef.current) {
      segSeries.applyOptions({ color: palette.segConfirmed });
    }
    segmentUnconfirmedRef.current?.applyOptions({ color: palette.segUnconfirmed });
    macdDifRef.current?.applyOptions({ color: palette.slots[0] });
    macdDeaRef.current?.applyOptions({ color: palette.slots[3] });
    zsPrimitiveRef.current?.setPalette(palette);
  }, [palette]);

  /* Push base candle & MACD data */
  useEffect(() => {
    const candle = candleRef.current;
    if (!candle) return;

    candle.applyOptions({ priceFormat: dynamicFormats.price });
    strokeConfirmedRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    strokeUnconfirmedRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    for (const segSeries of segmentConfirmedPoolRef.current) {
      segSeries.applyOptions({ priceFormat: dynamicFormats.price });
    }
    segmentUnconfirmedRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    macdHistRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });
    macdDifRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });
    macdDeaRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });

    candle.setData(prepared.candles);
    macdHistRef.current?.setData(prepared.macdHist);
    macdDifRef.current?.setData(prepared.macdDif);
    macdDeaRef.current?.setData(prepared.macdDea);

    const firstKey = prepared.bars[0] ? timeKey(prepared.bars[0].time) : "";
    if (
      prepared.bars.length > 0 &&
      (firstKey !== prevFirstTimeRef.current || Math.abs(prepared.bars.length - prevBarCountRef.current) > 5)
    ) {
      prevFirstTimeRef.current = firstKey;
      prevBarCountRef.current = prepared.bars.length;
      const total = prepared.bars.length;
      const visibleWindow = intraday ? Math.min(total, 150) : Math.min(total, 220);
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: Math.max(0, total - visibleWindow),
        to: total + 4,
      });
    }
  }, [prepared, dynamicFormats, intraday, height]);

  /* Push Chanlun structural overlays independently (multi-chain confirmed segments across gaps) */
  useEffect(() => {
    const chart = chartRef.current;
    if (!candleRef.current || !chart) return;
    strokeConfirmedRef.current?.setData(showStrokes ? prepared.confirmedStrokes : []);
    strokeUnconfirmedRef.current?.setData(showStrokes ? prepared.unconfirmedStroke : []);

    const chains = showSegments ? prepared.confirmedSegmentChains : [];
    const pool = segmentConfirmedPoolRef.current;
    while (pool.length < chains.length) {
      pool.push(
        chart.addSeries(LineSeries, {
          color: palette.segConfirmed,
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          priceFormat: dynamicFormats.price,
        }),
      );
    }
    for (let i = 0; i < pool.length; i++) {
      pool[i]?.setData(chains[i] ?? []);
    }

    segmentUnconfirmedRef.current?.setData(showSegments ? prepared.unconfirmedSegments : []);

    zsPrimitiveRef.current?.setBoxes(visibleZhongshuBoxes);
    zsPrimitiveRef.current?.setVisible(visibleZhongshuBoxes.length > 0);
    markersRef.current?.setMarkers(showMarkers ? prepared.markers : []);
  }, [prepared, palette.segConfirmed, dynamicFormats.price, showStrokes, showSegments, visibleZhongshuBoxes, showMarkers, intraday, height]);

  /* Apply synchronized crosshair AFTER candle data is pushed; clear stale crosshair when date is absent */
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle) return;
    programmaticCrosshairRef.current = true;
    try {
      const matched = syncDate ? (barByDateMap.get(syncDate) ?? null) : null;
      if (!matched) {
        chart.clearCrosshairPosition();
        setHoveredBar(null);
      } else {
        chart.setCrosshairPosition(matched.close, matched.time, candle);
        setHoveredBar(matched);
      }
    } finally {
      programmaticCrosshairRef.current = false;
    }
  }, [syncDate, barByDateMap, prepared.candles]);

  /* Update Zhongshu ZG/ZD reference lines + optional user-requested Trade Plan R:R ruler */
  useEffect(() => {
    const candle = candleRef.current;
    if (!candle) return;
    for (const pl of priceLinesRef.current) {
      candle.removePriceLine(pl);
    }
    priceLinesRef.current = [];

    const addLine = (
      price: number | null | undefined,
      lineTitle: string,
      color: string,
      style: LineStyle,
      lineWidth: 1 | 2 = 1,
    ) => {
      if (typeof price !== "number" || !Number.isFinite(price) || price <= 0) return;
      priceLinesRef.current.push(
        candle.createPriceLine({
          price,
          color,
          lineWidth,
          lineStyle: style,
          axisLabelVisible: true,
          title: lineTitle,
        }),
      );
    };

    const latestStrokeZs = prepared.latestStrokeZhongshu;
    if (showStrokeZhongshu && latestStrokeZs) {
      addLine(latestStrokeZs.zg, "Z·ZG", palette.zsStrokeBorder, LineStyle.Dotted, 1);
      addLine(latestStrokeZs.zd, "Z·ZD", palette.zsStrokeBorder, LineStyle.Dotted, 1);
    }

    const latestSegmentZs = prepared.latestSegmentZhongshu;
    if (showSegmentZhongshu && latestSegmentZs) {
      addLine(latestSegmentZs.zg, "S·ZG", palette.zsSegBorder, LineStyle.Dotted, 1);
      addLine(latestSegmentZs.zd, "S·ZD", palette.zsSegBorder, LineStyle.Dotted, 1);
    }

    if (planOverlay) {
      addLine(planOverlay.entry, "计划入场", palette.accent, LineStyle.Dashed, 1);
      addLine(planOverlay.stop30m, "战术止损", palette.bear, LineStyle.Dashed, 1);
      addLine(planOverlay.target, "目标位", palette.bull, LineStyle.Dashed, 1);
    }
  }, [
    prepared.latestStrokeZhongshu,
    prepared.latestSegmentZhongshu,
    palette,
    showStrokeZhongshu,
    showSegmentZhongshu,
    planOverlay,
    intraday,
    height,
  ]);

  const handleResetZoom = () => {
    const total = prepared.bars.length;
    if (total === 0) return;
    const visibleWindow = intraday ? Math.min(total, 150) : Math.min(total, 220);
    chartRef.current?.timeScale().setVisibleLogicalRange({
      from: Math.max(0, total - visibleWindow),
      to: total + 4,
    });
  };

  const latestZs = chanlun?.latest_zhongshu;
  const mom = chanlun?.momentum_assessment;
  const latestStroke = chanlun?.all_strokes?.[chanlun.all_strokes.length - 1];

  return (
    <section
      className={`card flex flex-col overflow-hidden ${pickHint ? "ring-1 ring-accent" : ""}`}
      aria-label={title}
    >
      {/* Single-Row Precision Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <h2 className="m-0 text-[13px] font-semibold tracking-tight text-ink">{title}</h2>
          {chanlun?.macro_structure ? (
            <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 text-[10.5px] text-ink-2">
              {chanlun.macro_structure}
            </span>
          ) : null}
          {chanlun?.zhongshu_level_label ? (
            <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 text-[10.5px] text-muted">
              {chanlun.zhongshu_level_label}
            </span>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1" role="group" aria-label={`${title} 图层开关`}>
          <button type="button" className="chip px-2" aria-pressed={showStrokes} onClick={() => setShowStrokes((v) => !v)}>
            <span
              className="inline-block h-1.5 w-2 rounded-xs"
              style={{ backgroundColor: palette.strokeUnconfirmed }}
              aria-hidden="true"
            />
            <span>笔</span>
          </button>
          <button
            type="button"
            className="chip px-2"
            aria-pressed={showStrokeZhongshu}
            onClick={() => setShowStrokeZhongshu((v) => !v)}
            title={`显示/隐藏笔中枢（当前 ${prepared.strokeZhongshuBoxes.length} 个）`}
          >
            <Layers className="h-3 w-3" style={{ color: palette.zsStrokeBorder }} aria-hidden="true" />
            <span>笔枢</span>
          </button>
          <button type="button" className="chip px-2" aria-pressed={showSegments} onClick={() => setShowSegments((v) => !v)}>
            <span
              className="inline-block h-1.5 w-2 rounded-xs"
              style={{ backgroundColor: palette.segConfirmed }}
              aria-hidden="true"
            />
            <span>线段</span>
          </button>
          <button
            type="button"
            className="chip px-2"
            aria-pressed={showSegmentZhongshu}
            onClick={() => setShowSegmentZhongshu((v) => !v)}
            title={`显示/隐藏线段中枢（当前 ${prepared.segmentZhongshuBoxes.length} 个）`}
          >
            <Layers className="h-3 w-3" style={{ color: palette.zsSegBorder }} aria-hidden="true" />
            <span>段枢</span>
          </button>
          <button type="button" className="chip px-2" aria-pressed={showMarkers} onClick={() => setShowMarkers((v) => !v)}>
            <span>信号</span>
          </button>
          <button
            type="button"
            className="chip px-2"
            aria-pressed={showTable}
            onClick={() => setShowTable((v) => !v)}
            title="切换缠论笔与中枢坐标无障碍对照表"
          >
            <Table2 className="h-3 w-3" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm px-1.5"
            onClick={handleResetZoom}
            aria-label="重置视图缩放"
            title="重置缩放至最近结构视口"
          >
            <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Compact Monospace Readout & Structural Telemetry Strip */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-hairline-soft bg-sunken/70 px-3 py-1 font-mono text-[11px]">
        {displayBar ? (
          <div className="tnum flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-ink-2">
            <span className="text-muted">{displayBar.label}</span>
            <span>
              O <strong className="font-medium text-ink">{fmtPrice(displayBar.open)}</strong>
            </span>
            <span>
              H <strong className="font-medium text-ink">{fmtPrice(displayBar.high)}</strong>
            </span>
            <span>
              L <strong className="font-medium text-ink">{fmtPrice(displayBar.low)}</strong>
            </span>
            <span>
              C <strong className="font-semibold text-ink">{fmtPrice(displayBar.close)}</strong>
            </span>
            <span className="text-slot1">DIF {fmtFixed(displayBar.dif, dynamicFormats.macdDp)}</span>
            <span className="text-slot4">DEA {fmtFixed(displayBar.dea, dynamicFormats.macdDp)}</span>
            <span
              className={
                displayBar.hist != null && displayBar.hist >= 0 ? "text-bull" : "text-bear"
              }
            >
              HIST {fmtFixed(displayBar.hist, dynamicFormats.macdDp)}
            </span>
          </div>
        ) : (
          <span className="font-sans text-muted">暂无 K 线数据</span>
        )}

        <div className="tnum flex flex-wrap items-center gap-x-2.5 font-sans text-[11px] text-muted">
          {latestZs ? (
            <span>
              中枢{" "}
              <span className="font-mono text-ink-2">
                [{fmtPrice(latestZs.zd)}, {fmtPrice(latestZs.zg)}]
              </span>{" "}
              · {fmtPosVsZs(chanlun?.price_vs_zhongshu)}
            </span>
          ) : (
            <span>未成中枢</span>
          )}
          {latestStroke?.strength_tier ? (
            <span>
              近笔：{latestStroke.direction === "up" ? "↑" : "↓"}
              {latestStroke.strength_tier}
            </span>
          ) : null}
          {mom?.verdict ? <span>{fmtMomentumVerdict(mom.verdict)}</span> : null}
        </div>
      </div>

      {/* Canvas Container */}
      <div className={`relative w-full ${onPickPrice ? "cursor-crosshair" : ""}`}>
        {pickHint ? (
          <div className="pointer-events-none absolute top-2 left-3 z-10 inline-flex items-center gap-1.5 rounded border border-accent/45 bg-surface/92 px-2.5 py-1 text-[11px] font-medium text-ink shadow-sm backdrop-blur-xs">
            <Crosshair className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
            <span>{pickHint}（点击 K 线画布取价 · Esc 取消）</span>
          </div>
        ) : null}
        <div ref={containerRef} className="w-full" style={{ height: `${height}px` }} />
        {prepared.bars.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center bg-surface/90 p-6 text-center text-xs text-muted">
            {chanlun?.error ? `行情加载提示：${chanlun.error}` : "当前周期暂无可用 K 线样本"}
          </div>
        ) : null}
      </div>

      {/* Accessible Coordinate Table View */}
      {showTable ? (
        <div className="border-t border-hairline p-3">
          <div className="mb-2 flex items-center justify-between text-xs text-muted">
            <span>中枢与近期严格笔坐标明细</span>
            <span className="font-mono text-[11px]">
              笔中枢 {prepared.strokeZhongshuBoxes.length} · 线段中枢 {prepared.segmentZhongshuBoxes.length} ·{" "}
              {(chanlun?.all_strokes ?? []).length} 笔 · {(chanlun?.all_segments ?? []).length} 线段
            </span>
          </div>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <div tabIndex={0} role="region" aria-label={`${title} 中枢区间列表`} className="table-wrapper max-h-52">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">中枢标识</th>
                    <th scope="col" className="num">
                      ZD 下沿
                    </th>
                    <th scope="col" className="num">
                      ZG 上沿
                    </th>
                    <th scope="col">起止时间</th>
                  </tr>
                </thead>
                <tbody>
                  {tableZhongshuBoxes.length === 0 ? (
                    <tr>
                      <th scope="row">无</th>
                      <td data-label="ZD 下沿" className="num">
                        —
                      </td>
                      <td data-label="ZG 上沿" className="num">
                        —
                      </td>
                      <td data-label="起止时间">尚未形成中枢</td>
                    </tr>
                  ) : (
                    tableZhongshuBoxes.map((z) => (
                      <tr key={z.key}>
                        <th scope="row">{z.label}</th>
                        <td data-label="ZD 下沿" className="num">
                          {fmtPrice(z.zd)}
                        </td>
                        <td data-label="ZG 上沿" className="num">
                          {fmtPrice(z.zg)}
                        </td>
                        <td data-label="起止时间" className="font-mono text-[11px] text-muted">
                          {z.startLabel ?? String(z.startTime)} → {z.endLabel ?? String(z.endTime)}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <div tabIndex={0} role="region" aria-label={`${title} 最近严格笔列表`} className="table-wrapper max-h-52">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">方向/状态</th>
                    <th scope="col" className="num">
                      起点→终点
                    </th>
                    <th scope="col" className="num">
                      幅度
                    </th>
                    <th scope="col">笔强弱</th>
                  </tr>
                </thead>
                <tbody>
                  {(chanlun?.all_strokes ?? [])
                    .slice(-8)
                    .reverse()
                    .map((s, idx) => (
                      <tr key={`${s.start_date}-${idx}`}>
                        <th scope="row">
                          <span className={s.direction === "up" ? "text-bull" : "text-bear"}>
                            {s.direction === "up" ? "↑ 向上笔" : "↓ 向下笔"}
                          </span>
                          {!s.is_confirmed ? <span className="text-muted"> (未完)</span> : null}
                        </th>
                        <td data-label="起点→终点" className="num">
                          {fmtPrice(s.start_price)} → {fmtPrice(s.end_price)}
                        </td>
                        <td data-label="幅度" className="num">
                          {fmtPct(s.pct_change, 2, true)}
                        </td>
                        <td data-label="笔强弱" className="text-muted">
                          {s.strength_tier || `${s.raw_bars ?? "—"}K`}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
