import type {
  CandlestickData,
  HistogramData,
  LineData,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import type {
  ChanlunTimeframe,
  OhlcvDailyBar,
  OhlcvIntradayBar,
  SignalMeta,
  SignalSummary,
  Zhongshu,
} from "../../api/types";
import type { ChartPalette } from "../../lib/theme";
import type { ZhongshuBoxDrawItem } from "./ZhongshuBoxesPrimitive";

export type AnyBar = OhlcvDailyBar | OhlcvIntradayBar;

export interface IndexedBar {
  idx: number;
  time: Time;
  label: string;
  open: number;
  high: number;
  low: number;
  close: number;
  dif: number | null;
  dea: number | null;
  hist: number | null;
}

export interface PreparedChanlunSeries {
  bars: IndexedBar[];
  byTimeKey: Map<string, IndexedBar>;
  candles: CandlestickData<Time>[];
  macdHist: HistogramData<Time>[];
  macdDif: LineData<Time>[];
  macdDea: LineData<Time>[];
  confirmedStrokes: LineData<Time>[];
  unconfirmedStroke: LineData<Time>[];
  confirmedSegments: LineData<Time>[];
  unconfirmedSegments: LineData<Time>[];
  strokeZhongshuBoxes: ZhongshuBoxDrawItem[];
  segmentZhongshuBoxes: ZhongshuBoxDrawItem[];
  latestStrokeZhongshu: Zhongshu | null;
  latestSegmentZhongshu: Zhongshu | null;
  markers: SeriesMarker<Time>[];
}

function toChartTime(raw: string | number): Time {
  return typeof raw === "number" ? (raw as UTCTimestamp) : raw;
}

export function timeKey(t: Time): string {
  return typeof t === "object"
    ? `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`
    : String(t);
}

const EXPLICIT_TZ_RE = /(Z|[+-]\d{2}:?\d{2}|\bUTC)$/i;

function hasExplicitTimezone(raw: string): boolean {
  return EXPLICIT_TZ_RE.test(raw.trim());
}

/**
 * Normalize ONLY tz-naive timestamps without stripping timezone offsets or non-zero seconds.
 * Allows `"YYYY-MM-DDTHH:MM"` -> `"YYYY-MM-DD HH:MM"` and stripping trailing `:00` / `:00.000` only.
 */
function normalizeNaiveLabel(raw: string): string {
  return raw
    .trim()
    .replace(/^(\d{4}-\d{2}-\d{2})T/, "$1 ")
    .replace(/(\d{2}:\d{2}):00(\.0+)?$/, "$1")
    .trim();
}

/** Generate strict naive lookup keys (never strips timezone offsets or non-zero seconds). */
function naiveCandidateKeys(raw: string, intraday: boolean): string[] {
  const s = raw.trim();
  const keys: string[] = [s];
  if (hasExplicitTimezone(s)) {
    return keys;
  }
  const spaceNorm = normalizeNaiveLabel(s);
  if (spaceNorm && !keys.includes(spaceNorm)) keys.push(spaceNorm);
  /* Date-only key is ONLY exact for daily bars; never collapse intraday timestamps onto a date key */
  if (!intraday && /^\d{4}-\d{2}-\d{2}$/.test(spaceNorm)) {
    if (!keys.includes(spaceNorm)) keys.push(spaceNorm);
  } else if (!intraday && /^\d{4}-\d{2}-\d{2}\s+00:00$/.test(spaceNorm)) {
    const dayKey = spaceNorm.slice(0, 10);
    if (!keys.includes(dayKey)) keys.push(dayKey);
  }
  return keys;
}

function parseExactUtcEpochSec(raw: string): number | null {
  const s = raw.trim();
  if (/^\d{9,10}(\.0+)?$/.test(s)) {
    const n = Math.floor(Number(s));
    return Number.isFinite(n) ? n : null;
  }
  /* Only parse to UTC epoch seconds when explicit timezone is present */
  if (EXPLICIT_TZ_RE.test(s)) {
    const iso = s.replace(/\s*UTC$/i, "Z").replace(/^(\d{4}-\d{2}-\d{2})\s+/, "$1T");
    const ms = Date.parse(iso);
    if (Number.isFinite(ms)) return Math.floor(ms / 1000);
  }
  return null;
}

function barEpochSec(b: IndexedBar): number | null {
  if (typeof b.time === "number" && Number.isFinite(b.time)) {
    return Math.floor(b.time);
  }
  if (typeof b.time === "string" && /^\d{4}-\d{2}-\d{2}$/.test(b.time)) {
    const ms = Date.parse(`${b.time}T00:00:00Z`);
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
  }
  return null;
}

/**
 * Exact bar resolver:
 * 1) If rawDate carries an explicit timezone (Z / ±HH:MM / UTC): match ONLY by verbatim label or exact UTC epoch second.
 *    NEVER strip timezone offsets to collide e.g. `14:30Z` with `14:30+08:00`.
 * 2) If rawDate is tz-naive: match ONLY exact label (allowing T->space and exact `:00` zero-second trimming; NEVER strip non-zero seconds like `:42`).
 * 3) NEVER snaps out-of-range or missing timestamps to an arbitrary distant bar.
 */
function buildLabelResolver(
  bars: IndexedBar[],
  intraday: boolean,
): (rawDate: string | null | undefined) => IndexedBar | null {
  const exactRaw = new Map<string, IndexedBar>();
  const exactNaive = new Map<string, IndexedBar>();
  const byEpochSec = new Map<number, IndexedBar>();

  for (const b of bars) {
    const lbl = b.label.trim();
    exactRaw.set(lbl, b);
    exactRaw.set(String(b.time), b);
    if (!hasExplicitTimezone(lbl)) {
      const norm = normalizeNaiveLabel(lbl);
      if (norm && !exactNaive.has(norm)) exactNaive.set(norm, b);
      if (!intraday && /^\d{4}-\d{2}-\d{2}/.test(norm)) {
        const day = norm.slice(0, 10);
        if (!exactNaive.has(day)) exactNaive.set(day, b);
      }
    }
    const sec = barEpochSec(b);
    if (sec !== null && !byEpochSec.has(sec)) {
      byEpochSec.set(sec, b);
    }
  }

  return (rawDate: string | null | undefined): IndexedBar | null => {
    if (!rawDate) return null;
    const s = rawDate.trim();
    const verbatim = exactRaw.get(s);
    if (verbatim) return verbatim;

    if (hasExplicitTimezone(s) || /^\d{9,10}(\.0+)?$/.test(s)) {
      const targetSec = parseExactUtcEpochSec(s);
      return targetSec !== null ? (byEpochSec.get(targetSec) ?? null) : null;
    }

    for (const k of naiveCandidateKeys(s, intraday)) {
      const hit = exactNaive.get(k);
      if (hit) return hit;
    }
    return null;
  };
}

function comparableTimestampKey(raw: string | null | undefined, intraday: boolean): string | null {
  if (!raw) return null;
  const s = raw.trim();
  const epoch = parseExactUtcEpochSec(s);
  if (epoch !== null) {
    const iso = new Date(epoch * 1000).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "");
    return intraday ? iso : iso.slice(0, 10);
  }
  const norm = normalizeNaiveLabel(s);
  if (!/^\d{4}-\d{2}-\d{2}/.test(norm)) return null;
  return intraday ? norm : norm.slice(0, 10);
}

/** Deduplicate and strictly sort LineData by bar index (Lightweight Charts requires strictly ascending times). */
function toSortedLine(points: { idx: number; time: Time; value: number }[]): LineData<Time>[] {
  const byIdx = new Map<number, { idx: number; time: Time; value: number }>();
  for (const p of points) {
    if (!Number.isFinite(p.value)) continue;
    byIdx.set(p.idx, p);
  }
  return Array.from(byIdx.values())
    .sort((a, b) => a.idx - b.idx)
    .map((p) => ({ time: p.time, value: p.value }));
}

function mapZhongshus(
  list: Zhongshu[] | undefined,
  kind: "stroke" | "segment",
  resolve: (d: string | null | undefined) => IndexedBar | null,
  firstBar: IndexedBar | undefined,
  lastBar: IndexedBar | undefined,
  intraday: boolean,
): ZhongshuBoxDrawItem[] {
  if (!list || list.length === 0 || !firstBar || !lastBar) return [];
  const firstCmp = comparableTimestampKey(firstBar.label, intraday);
  const lastCmp = comparableTimestampKey(lastBar.label, intraday);
  const out: ZhongshuBoxDrawItem[] = [];

  for (const zs of list) {
    let startBar = resolve(zs.start_date);
    const endRaw = zs.oscillation_end_date || zs.end_date;
    let endBar = resolve(endRaw);
    let clampedLeft = false;
    let clampedRight = false;

    /* Clamp ONLY when the Zhongshu genuinely spans across the visible bar window boundary */
    if (!startBar && endBar && firstCmp) {
      const sCmp = comparableTimestampKey(zs.start_date, intraday);
      if (sCmp && sCmp < firstCmp && endBar.idx >= firstBar.idx) {
        startBar = firstBar;
        clampedLeft = true;
      }
    }
    if (startBar && !endBar && lastCmp) {
      const eCmp = comparableTimestampKey(endRaw, intraday);
      if (eCmp && eCmp > lastCmp && startBar.idx <= lastBar.idx) {
        endBar = lastBar;
        clampedRight = true;
      }
    }

    if (!startBar || !endBar) continue;
    const [b1, b2] = startBar.idx <= endBar.idx ? [startBar, endBar] : [endBar, startBar];
    const prefix = kind === "segment" ? `线段中枢 S${zs.zs_id}` : `笔中枢 Z${zs.zs_id}`;
    const suffix = zs.is_nine_upgrade ? "·九段" : zs.is_extended ? "·扩展" : "";
    const truncTag = clampedLeft ? "·左截断" : clampedRight ? "·右截断" : "";
    out.push({
      key: `${kind}-${zs.zs_id}-${b1.idx}`,
      kind,
      label: `${prefix}${suffix}${truncTag}`,
      startTime: b1.time,
      endTime: b2.time,
      startLabel: b1.label,
      endLabel: b2.label,
      zd: zs.zd,
      zg: zs.zg,
      dd: zs.dd,
      gg: zs.gg,
      isExtended: zs.is_extended,
      isNineUpgrade: zs.is_nine_upgrade,
    });
  }
  return out;
}

/**
 * Extract concise label ONLY from signal strings matching `signal_meta.direction`.
 * Rejects negated cautionary phrases (e.g. "严禁当一买") and opposite-direction signals.
 */
function extractDirectionalLabel(
  meta: SignalMeta,
  summary: string | undefined,
  signals: string[] | undefined,
): string | null {
  const dir = meta.direction;
  if (dir !== "long" && dir !== "short") return null;

  const candidates = [summary, ...(signals ?? [])].filter(
    (x): x is string => typeof x === "string" && x.trim().length > 0,
  );

  if (dir === "long") {
    for (const raw of candidates) {
      if (/严禁|切勿|勿逆势|不视作/.test(raw)) continue;
      const s = raw.toLowerCase();
      if (/[一二三123]卖|first_sell|second_sell|third_sell|sell_[123]/.test(s)) continue;
      if (raw.includes("一买") || /\b1b\b|first_buy|buy_1/.test(s)) return "1买";
      if (raw.includes("二买") || /\b2b\b|second_buy|buy_2/.test(s)) return "2买";
      if (raw.includes("非首中枢三买")) return "非首3买";
      if (raw.includes("三买") || /\b3b\b|third_buy|buy_3/.test(s)) return "3买";
      if (raw.includes("2+3") && !/空|跌|卖|down/.test(s)) return "2+3买";
      if ((raw.includes("3出") || /\bd3\b/.test(s)) && !/空|跌|卖|down/.test(s)) return "3出";
      if ((raw.includes("4出") || /\bd4\b/.test(s)) && !/空|跌|卖|down/.test(s)) return "4出";
    }
    if (meta.signal_type === "confirmed_buy") return "买点";
    if (meta.signal_type === "exhaustion_buy_warn") return "非首3买";
    if (meta.signal_type === "candidate_buy") return "准买点";
    return null;
  }

  for (const raw of candidates) {
    if (/严禁|切勿|勿逆势|不视作/.test(raw)) continue;
    const s = raw.toLowerCase();
    if (/[一二三123]买|first_buy|second_buy|third_buy|buy_[123]/.test(s)) continue;
    if (raw.includes("一卖") || /\b1s\b|first_sell|sell_1/.test(s)) return "1卖";
    if (raw.includes("二卖") || /\b2s\b|second_sell|sell_2/.test(s)) return "2卖";
    if (raw.includes("非首中枢三卖")) return "非首3卖";
    if (raw.includes("三卖") || /\b3s\b|third_sell|sell_3/.test(s)) return "3卖";
    if ((raw.includes("3出") || /\bd3\b/.test(s)) && /空|跌|下|卖|down/.test(s)) return "3出空";
    if ((raw.includes("4出") || /\bd4\b/.test(s)) && /空|跌|下|卖|down/.test(s)) return "4出空";
  }
  if (meta.signal_type === "confirmed_sell") return "卖点";
  if (meta.signal_type === "exhaustion_sell_warn") return "非首3卖";
  if (meta.signal_type === "candidate_sell") return "准卖点";
  return null;
}

export function prepareChanlunSeries(
  rawBars: AnyBar[],
  tf: ChanlunTimeframe | undefined,
  signalSummary: SignalSummary | undefined,
  palette: ChartPalette,
  intraday = false,
): PreparedChanlunSeries {
  /* Deduplicate raw bars by chart time and sort ascending */
  const seen = new Set<string>();
  const bars: IndexedBar[] = [];
  const byTimeKey = new Map<string, IndexedBar>();

  for (const b of rawBars) {
    if (!Number.isFinite(b.open) || !Number.isFinite(b.close)) continue;
    const t = toChartTime(b.time);
    const k = timeKey(t);
    if (seen.has(k)) continue;
    seen.add(k);
    const item: IndexedBar = {
      idx: bars.length,
      time: t,
      label: b.label || String(b.time),
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      dif: typeof b.dif === "number" && Number.isFinite(b.dif) ? b.dif : null,
      dea: typeof b.dea === "number" && Number.isFinite(b.dea) ? b.dea : null,
      hist: typeof b.hist === "number" && Number.isFinite(b.hist) ? b.hist : null,
    };
    bars.push(item);
    byTimeKey.set(k, item);
  }

  const candles: CandlestickData<Time>[] = bars.map((b) => ({
    time: b.time,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
  }));

  const macdHist: HistogramData<Time>[] = [];
  const macdDif: LineData<Time>[] = [];
  const macdDea: LineData<Time>[] = [];
  for (const b of bars) {
    if (b.hist !== null) {
      macdHist.push({
        time: b.time,
        value: b.hist,
        color:
          b.hist >= 0
            ? palette.mode === "dark"
              ? "rgba(25, 158, 112, 0.72)"
              : "rgba(27, 175, 122, 0.72)"
            : palette.mode === "dark"
              ? "rgba(230, 103, 103, 0.72)"
              : "rgba(227, 73, 72, 0.72)",
      });
    }
    if (b.dif !== null) macdDif.push({ time: b.time, value: b.dif });
    if (b.dea !== null) macdDea.push({ time: b.time, value: b.dea });
  }

  const resolve = buildLabelResolver(bars, intraday);
  const firstBar = bars[0];
  const lastBar = bars[bars.length - 1];

  /* Confirmed strokes vs active unconfirmed stroke (skip unmapped endpoints) */
  const strokes = tf?.all_strokes ?? [];
  const confirmedPts: { idx: number; time: Time; value: number }[] = [];
  let unconfirmedPts: { idx: number; time: Time; value: number }[] = [];

  for (let i = 0; i < strokes.length; i++) {
    const s = strokes[i];
    if (!s) continue;
    const sb = resolve(s.start_date);
    const eb = resolve(s.end_date);
    if (!sb || !eb) continue;
    if (s.is_confirmed || i < strokes.length - 1) {
      confirmedPts.push({ idx: sb.idx, time: sb.time, value: s.start_price });
      confirmedPts.push({ idx: eb.idx, time: eb.time, value: s.end_price });
    } else {
      unconfirmedPts = [
        { idx: sb.idx, time: sb.time, value: s.start_price },
        { idx: eb.idx, time: eb.time, value: s.end_price },
      ];
    }
  }

  /* Segments (线段): confirmed segments are solid; uncertain trailing segment / extension to latest price is dashed */
  type SegmentChain = {
    data: LineData<Time>[];
    lastIdx: number | null;
    hadGap: boolean;
  };
  const confirmedChain: SegmentChain = { data: [], lastIdx: null, hadGap: false };
  const unconfirmedChain: SegmentChain = { data: [], lastIdx: null, hadGap: false };

  const appendToChain = (
    chain: SegmentChain,
    sb: IndexedBar,
    startPrice: number,
    eb: IndexedBar,
    endPrice: number,
  ): boolean => {
    if (!Number.isFinite(startPrice) || !Number.isFinite(endPrice) || sb.idx >= eb.idx) {
      chain.hadGap = true;
      return false;
    }
    if (chain.lastIdx === null) {
      chain.data.push({ time: sb.time, value: startPrice }, { time: eb.time, value: endPrice });
    } else if (sb.idx < chain.lastIdx) {
      chain.hadGap = true;
      return false;
    } else if (!chain.hadGap && sb.idx === chain.lastIdx) {
      chain.data.push({ time: eb.time, value: endPrice });
    } else {
      /* Lightweight Charts LineSeries connects all non-whitespace points continuously; restart at the latest contiguous chain across any gap */
      chain.data.length = 0;
      chain.data.push({ time: sb.time, value: startPrice }, { time: eb.time, value: endPrice });
    }
    chain.lastIdx = eb.idx;
    chain.hadGap = false;
    return true;
  };

  const allSegments = tf?.all_segments ?? [];
  let hasAnyUnconfirmedSeg = false;
  let lastSegResolvedAndConfirmed = false;
  let lastSegEnd: { idx: number; time: Time; value: number } | null = null;

  for (let i = 0; i < allSegments.length; i++) {
    const seg = allSegments[i];
    if (!seg) continue;
    const isLast = i === allSegments.length - 1;
    if (!seg.is_confirmed) hasAnyUnconfirmedSeg = true;
    const sb = resolve(seg.start_date);
    const eb = resolve(seg.end_date);
    if (!sb || !eb) {
      if (seg.is_confirmed) confirmedChain.hadGap = true;
      else unconfirmedChain.hadGap = true;
      continue;
    }
    const ok = seg.is_confirmed
      ? appendToChain(confirmedChain, sb, seg.start_price, eb, seg.end_price)
      : appendToChain(unconfirmedChain, sb, seg.start_price, eb, seg.end_price);
    if (ok && isLast) {
      lastSegResolvedAndConfirmed = seg.is_confirmed;
      lastSegEnd = { idx: eb.idx, time: eb.time, value: seg.end_price };
    }
  }

  /* Extend uncertain dashed tail to latest bar close ONLY when the true last segment resolved cleanly */
  if (lastBar && lastSegEnd && lastSegEnd.idx < lastBar.idx) {
    if (!hasAnyUnconfirmedSeg && lastSegResolvedAndConfirmed) {
      unconfirmedChain.data.push(
        { time: lastSegEnd.time, value: lastSegEnd.value },
        { time: lastBar.time, value: lastBar.close },
      );
    } else if (hasAnyUnconfirmedSeg && !unconfirmedChain.hadGap && unconfirmedChain.lastIdx === lastSegEnd.idx) {
      unconfirmedChain.data.push({ time: lastBar.time, value: lastBar.close });
    }
  }

  /* Zhongshu boxes: strictly separate stroke zhongshus (笔中枢) and segment zhongshus (线段中枢) */
  const strokeZsSrc =
    tf?.all_stroke_zhongshus !== undefined
      ? tf.all_stroke_zhongshus
      : tf?.stroke_zhongshus !== undefined
        ? tf.stroke_zhongshus
        : tf?.zhongshu_level !== "segment"
          ? tf?.recent_zhongshus && tf.recent_zhongshus.length > 0
            ? tf.recent_zhongshus
            : tf?.latest_zhongshu
              ? [tf.latest_zhongshu]
              : []
          : [];
  const segmentZsSrc =
    tf?.all_segment_zhongshus !== undefined
      ? tf.all_segment_zhongshus
      : tf?.segment_zhongshus !== undefined
        ? tf.segment_zhongshus
        : tf?.zhongshu_level === "segment"
          ? tf?.recent_zhongshus && tf.recent_zhongshus.length > 0
            ? tf.recent_zhongshus
            : tf?.latest_zhongshu
              ? [tf.latest_zhongshu]
              : []
          : [];
  const strokeZhongshuBoxes = mapZhongshus(strokeZsSrc, "stroke", resolve, firstBar, lastBar, intraday);
  const segmentZhongshuBoxes = mapZhongshus(segmentZsSrc, "segment", resolve, firstBar, lastBar, intraday);
  const latestStrokeZhongshu = strokeZsSrc.length > 0 ? (strokeZsSrc[strokeZsSrc.length - 1] ?? null) : null;
  const latestSegmentZhongshu = segmentZsSrc.length > 0 ? (segmentZsSrc[segmentZsSrc.length - 1] ?? null) : null;

  /* Markers: latest fractal + recent stroke turning points + strict signal_meta buy/sell marker */
  const rawMarkers: { idx: number; marker: SeriesMarker<Time> }[] = [];

  /* Recent 8 strokes' fractal endpoints (subtle circle markers) */
  const recentStrokes = strokes.slice(-8);
  for (const st of recentStrokes) {
    const eb = resolve(st.end_date);
    if (!eb) continue;
    const isTop = st.direction === "up";
    rawMarkers.push({
      idx: eb.idx,
      marker: {
        time: eb.time,
        position: isTop ? "aboveBar" : "belowBar",
        shape: "circle",
        color: isTop ? palette.slots[7] : palette.slots[2],
        text: st.strength_tier ? `${isTop ? "顶" : "底"}·${st.strength_tier.slice(0, 2)}` : isTop ? "顶" : "底",
        size: 0.7,
      },
    });
  }

  /* Latest fractal highlight */
  if (tf?.latest_fractal) {
    const fb = resolve(tf.latest_fractal.date);
    if (fb) {
      const isTop = tf.latest_fractal.kind === "top";
      rawMarkers.push({
        idx: fb.idx,
        marker: {
          time: fb.time,
          position: isTop ? "aboveBar" : "belowBar",
          shape: "square",
          color: palette.warning,
          text: isTop ? "最新顶分" : "最新底分",
          size: 0.9,
        },
      });
    }
  }

  /* Directional Buy / Sell marker strictly governed by signal_meta.direction & signal_meta.confirmed */
  const meta = signalSummary?.signal_meta;
  if (meta && (meta.direction === "long" || meta.direction === "short")) {
    const conciseLabel = extractDirectionalLabel(meta, signalSummary?.summary, tf?.signals);
    if (conciseLabel) {
      const isLong = meta.direction === "long";
      const isConfirmed = meta.confirmed === true;
      const sigBar = meta.signal_timestamp
        ? resolve(meta.signal_timestamp)
        : resolve(tf?.latest_fractal?.date);
      if (sigBar) {
        rawMarkers.push({
          idx: sigBar.idx,
          marker: {
            time: sigBar.time,
            position: isLong ? "belowBar" : "aboveBar",
            shape: isLong ? "arrowUp" : "arrowDown",
            color: isLong ? palette.good : palette.critical,
            text: `${conciseLabel}${isConfirmed ? "·已确认" : "(待确认)"}`,
            size: 1.4,
          },
        });
      }
    }
  }

  rawMarkers.sort((a, b) => a.idx - b.idx);

  return {
    bars,
    byTimeKey,
    candles,
    macdHist,
    macdDif,
    macdDea,
    confirmedStrokes: toSortedLine(confirmedPts),
    unconfirmedStroke: toSortedLine(unconfirmedPts),
    confirmedSegments: confirmedChain.data,
    unconfirmedSegments: unconfirmedChain.data,
    strokeZhongshuBoxes,
    segmentZhongshuBoxes,
    latestStrokeZhongshu,
    latestSegmentZhongshu,
    markers: rawMarkers.map((x) => x.marker),
  };
}
