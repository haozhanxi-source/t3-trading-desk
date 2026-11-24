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
import { Layers, Maximize2, Table2 } from "lucide-react";
import type { ChanlunTimeframe, SignalSummary } from "../../api/types";
import { fmtFixed, fmtPct, fmtPrice } from "../../lib/format";
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

export interface ChanlunChartProps {
  title: string;
  subtitle?: string;
  bars: AnyBar[];
  chanlun: ChanlunTimeframe | undefined;
  signalSummary?: SignalSummary;
  intraday?: boolean;
  height?: number;
}

export function ChanlunChart({
  title,
  subtitle,
  bars,
  chanlun,
  signalSummary,
  intraday = false,
  height = 460,
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
  const segmentRef = useRef<ISeriesApi<"Line", Time> | null>(null);
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
        fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
        fontSize: 11,
        panes: {
          separatorColor: palette.gridline,
          separatorHoverColor: palette.slots[0],
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

    /* Custom ISeriesPrimitive for Zhongshu [ZD, ZG] translucent boxes */
    const zsPrimitive = new ZhongshuBoxesPrimitive(palette);
    candleSeries.attachPrimitive(zsPrimitive);

    /* Confirmed strokes overlay (slot1 blue, crisp 2px solid) */
    const strokeConfirmed = chart.addSeries(LineSeries, {
      color: palette.slots[0],
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    /* Active unconfirmed latest stroke (slot2 orange, 2px dashed) */
    const strokeUnconfirmed = chart.addSeries(LineSeries, {
      color: palette.slots[1],
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    /* Optional Chanlun segments (线段, slot7 violet) */
    const segmentSeries = chart.addSeries(LineSeries, {
      color: palette.slots[6],
      lineWidth: 2,
      lineStyle: LineStyle.LargeDashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    /* Series markers v5 plugin */
    const markersPlugin = createSeriesMarkers(candleSeries, [], { zOrder: "aboveSeries" });

    /* Synchronized MACD sub-pane (Lightweight Charts v5 native multi-pane) */
    const macdPane = chart.addPane();
    chart.panes()[0]?.setStretchFactor(0.73);
    macdPane.setStretchFactor(0.27);

    const macdHist = macdPane.addSeries(HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
      base: 0,
    });
    const macdDif = macdPane.addSeries(LineSeries, {
      color: palette.slots[0],
      lineWidth: 1,
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
      if (!param.time) {
        setHoveredBar(null);
        return;
      }
      const hit = byTimeKeyRef.current.get(timeKey(param.time)) ?? null;
      setHoveredBar(hit);
    };
    chart.subscribeCrosshairMove(onCrosshair);

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
    segmentRef.current = segmentSeries;
    macdHistRef.current = macdHist;
    macdDifRef.current = macdDif;
    macdDeaRef.current = macdDea;
    markersRef.current = markersPlugin;
    zsPrimitiveRef.current = zsPrimitive;

    return () => {
      ro.disconnect();
      chart.unsubscribeCrosshairMove(onCrosshair);
      markersPlugin.detach();
      candleSeries.detachPrimitive(zsPrimitive);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      strokeConfirmedRef.current = null;
      strokeUnconfirmedRef.current = null;
      segmentRef.current = null;
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

  /* Sync theme palette dynamically on Dark/Light/System toggle & OS prefers-color-scheme change */
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: palette.surface },
        textColor: palette.muted,
        panes: {
          separatorColor: palette.gridline,
          separatorHoverColor: palette.slots[0],
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
    strokeConfirmedRef.current?.applyOptions({ color: palette.slots[0] });
    strokeUnconfirmedRef.current?.applyOptions({ color: palette.slots[1] });
    segmentRef.current?.applyOptions({ color: palette.slots[6] });
    macdDifRef.current?.applyOptions({ color: palette.slots[0] });
    macdDeaRef.current?.applyOptions({ color: palette.slots[3] });
    zsPrimitiveRef.current?.setPalette(palette);
  }, [palette]);

  /* Push base candle & MACD data and fit visible window when data or chart instance changes */
  useEffect(() => {
    const candle = candleRef.current;
    if (!candle) return;

    /* Apply dynamic decimal precision so FX (e.g. AUDUSD=X 0.6658) never rounds to 2dp */
    candle.applyOptions({ priceFormat: dynamicFormats.price });
    strokeConfirmedRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    strokeUnconfirmedRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    segmentRef.current?.applyOptions({ priceFormat: dynamicFormats.price });
    macdHistRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });
    macdDifRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });
    macdDeaRef.current?.applyOptions({ priceFormat: dynamicFormats.macd });

    candle.setData(prepared.candles);
    macdHistRef.current?.setData(prepared.macdHist);
    macdDifRef.current?.setData(prepared.macdDif);
    macdDeaRef.current?.setData(prepared.macdDea);

    /* Fit or scroll cleanly to recent structure when symbol/timeframe/chart instance changes */
    const firstKey = prepared.bars[0] ? timeKey(prepared.bars[0].time) : "";
    if (prepared.bars.length > 0 && (firstKey !== prevFirstTimeRef.current || Math.abs(prepared.bars.length - prevBarCountRef.current) > 5)) {
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

  /* Push Chanlun structural overlays independently so toggling layers never reloads candles/MACD */
  useEffect(() => {
    if (!candleRef.current) return;
    strokeConfirmedRef.current?.setData(showStrokes ? prepared.confirmedStrokes : []);
    strokeUnconfirmedRef.current?.setData(showStrokes ? prepared.unconfirmedStroke : []);
    segmentRef.current?.setData(showSegments ? prepared.segments : []);

    zsPrimitiveRef.current?.setBoxes(visibleZhongshuBoxes);
    zsPrimitiveRef.current?.setVisible(visibleZhongshuBoxes.length > 0);
    markersRef.current?.setMarkers(showMarkers ? prepared.markers : []);
  }, [prepared, showStrokes, showSegments, visibleZhongshuBoxes, showMarkers, intraday, height]);

  /* Update stroke / segment Zhongshu ZG & ZD reference PriceLines from canonical classified latest Zhongshu */
  useEffect(() => {
    const candle = candleRef.current;
    if (!candle) return;
    for (const pl of priceLinesRef.current) {
      candle.removePriceLine(pl);
    }
    priceLinesRef.current = [];

    const addLine = (price: number | null | undefined, lineTitle: string, color: string, style: LineStyle, lineWidth: 1 | 2 = 1) => {
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
      addLine(latestStrokeZs.zg, "笔中枢 ZG", palette.zsStrokeBorder, LineStyle.Dotted, 1);
      addLine(latestStrokeZs.zd, "笔中枢 ZD", palette.zsStrokeBorder, LineStyle.Dotted, 1);
    }

    const latestSegmentZs = prepared.latestSegmentZhongshu;
    if (showSegmentZhongshu && latestSegmentZs) {
      addLine(latestSegmentZs.zg, "线段中枢 ZG", palette.zsSegBorder, LineStyle.Dotted, 1);
      addLine(latestSegmentZs.zd, "线段中枢 ZD", palette.zsSegBorder, LineStyle.Dotted, 1);
    }
  }, [
    prepared.latestStrokeZhongshu,
    prepared.latestSegmentZhongshu,
    palette,
    showStrokeZhongshu,
    showSegmentZhongshu,
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

  return (
    <section className="card flex flex-col overflow-hidden" aria-label={title}>
      {/* Header & Interactive Toggles */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline px-3.5 py-2.5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="m-0 text-sm font-semibold text-ink">{title}</h2>
            {chanlun?.macro_structure ? (
              <span className="rounded border border-hairline bg-elevated px-2 py-0.5 text-[11px] text-ink-2">
                {chanlun.macro_structure}
              </span>
            ) : null}
            {chanlun?.zhongshu_level_label ? (
              <span className="rounded border border-hairline bg-page px-2 py-0.5 text-[11px] text-muted">
                {chanlun.zhongshu_level_label}
              </span>
            ) : null}
          </div>
          {subtitle ? <p className="m-0 mt-0.5 text-[11px] text-muted">{subtitle}</p> : null}
        </div>

        <div className="flex flex-wrap items-center gap-1" role="group" aria-label={`${title} 图层开关`}>
          <button type="button" className="chip" aria-pressed={showStrokes} onClick={() => setShowStrokes((v) => !v)}>
            <span className="inline-block h-2 w-2 rounded-full bg-slot1" aria-hidden="true" />
            <span>笔</span>
          </button>
          <button
            type="button"
            className="chip"
            aria-pressed={showStrokeZhongshu}
            onClick={() => setShowStrokeZhongshu((v) => !v)}
            title={`显示/隐藏笔中枢（当前 ${prepared.strokeZhongshuBoxes.length} 个）`}
          >
            <Layers className="h-3 w-3" style={{ color: palette.zsStrokeBorder }} aria-hidden="true" />
            <span>笔中枢</span>
          </button>
          <button type="button" className="chip" aria-pressed={showSegments} onClick={() => setShowSegments((v) => !v)}>
            <span className="inline-block h-2 w-2 rounded-full bg-slot7" aria-hidden="true" />
            <span>线段</span>
          </button>
          <button
            type="button"
            className="chip"
            aria-pressed={showSegmentZhongshu}
            onClick={() => setShowSegmentZhongshu((v) => !v)}
            title={`显示/隐藏线段中枢（当前 ${prepared.segmentZhongshuBoxes.length} 个）`}
          >
            <Layers className="h-3 w-3" style={{ color: palette.zsSegBorder }} aria-hidden="true" />
            <span>线段中枢</span>
          </button>
          <button type="button" className="chip" aria-pressed={showMarkers} onClick={() => setShowMarkers((v) => !v)}>
            <span>买卖点/分型</span>
          </button>
          <button
            type="button"
            className="chip"
            aria-pressed={showTable}
            onClick={() => setShowTable((v) => !v)}
            title="切换缠论笔与中枢坐标无障碍表格视图"
          >
            <Table2 className="h-3 w-3" aria-hidden="true" />
            <span>坐标表</span>
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={handleResetZoom}
            aria-label="重置视图缩放"
            title="回到最近关键结构区间"
          >
            <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Crosshair Readout & Legend Strip (text wears ink tokens; colored marks carry identity) */}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-hairline bg-page/60 px-3.5 py-1.5 text-[11.5px]">
        {displayBar ? (
          <div className="tnum flex flex-wrap items-center gap-x-3 gap-y-0.5 text-ink-2">
            <span className="font-mono text-muted">{displayBar.label}</span>
            <span>
              开 <strong className="font-medium text-ink">{fmtPrice(displayBar.open)}</strong>
            </span>
            <span>
              高 <strong className="font-medium text-ink">{fmtPrice(displayBar.high)}</strong>
            </span>
            <span>
              低 <strong className="font-medium text-ink">{fmtPrice(displayBar.low)}</strong>
            </span>
            <span>
              收 <strong className="font-semibold text-ink">{fmtPrice(displayBar.close)}</strong>
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-slot1" aria-hidden="true" />
              DIF {fmtFixed(displayBar.dif, dynamicFormats.macdDp)}
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-slot4" aria-hidden="true" />
              DEA {fmtFixed(displayBar.dea, dynamicFormats.macdDp)}
            </span>
            <span>MACD {fmtFixed(displayBar.hist, dynamicFormats.macdDp)}</span>
          </div>
        ) : (
          <span className="text-muted">暂无 K 线数据</span>
        )}

        <div className="tnum flex flex-wrap items-center gap-x-3 text-[11px] text-muted">
          {latestZs ? (
            <span>
              最新中枢 [ZD <strong className="text-ink-2">{fmtPrice(latestZs.zd)}</strong>, ZG{" "}
              <strong className="text-ink-2">{fmtPrice(latestZs.zg)}</strong>] · {chanlun?.price_vs_zhongshu || "—"}
            </span>
          ) : (
            <span>未形成标准中枢</span>
          )}
          {mom?.verdict ? <span>动力学：{mom.verdict}</span> : null}
        </div>
      </div>

      {/* Canvas Container */}
      <div className="relative w-full">
        <div ref={containerRef} className="w-full" style={{ height: `${height}px` }} />
        {prepared.bars.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center bg-surface/90 p-6 text-center text-xs text-muted">
            {chanlun?.error ? `行情加载提示：${chanlun.error}` : "当前周期暂无可用 K 线样本"}
          </div>
        ) : null}
      </div>

      {/* Accessible Coordinate Table View (dataviz rule: table alternative for canvas geometry) */}
      {showTable ? (
        <div className="border-t border-hairline p-3">
          <div className="mb-2 flex items-center justify-between text-xs text-muted">
            <span>中枢与近期严格笔坐标明细（无障碍数据对照表）</span>
            <span>
              笔中枢 {prepared.strokeZhongshuBoxes.length} 个 · 线段中枢 {prepared.segmentZhongshuBoxes.length} 个 ·{" "}
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
                        <td data-label="起止时间" className="text-muted">
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
                  {(chanlun?.all_strokes ?? []).slice(-8).reverse().map((s, idx) => (
                    <tr key={`${s.start_date}-${idx}`}>
                      <th scope="row">
                        {s.direction === "up" ? "↑ 向上笔" : "↓ 向下笔"}
                        {!s.is_confirmed ? " (未完)" : ""}
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
