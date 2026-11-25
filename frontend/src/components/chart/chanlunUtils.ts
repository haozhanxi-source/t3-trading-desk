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
  ZhongshuExtensionStage,
  ZhongshuRelation,
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

export interface SignalCalloutItem {
  time: Time;
  price: number;
  position: "aboveBar" | "belowBar";
  text: string;
  color: string;
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
  confirmedSegmentChains: LineData<Time>[][];
  unconfirmedSegments: LineData<Time>[];
  strokeZhongshuBoxes: ZhongshuBoxDrawItem[];
  segmentZhongshuBoxes: ZhongshuBoxDrawItem[];
  latestStrokeZhongshu: Zhongshu | null;
  latestSegmentZhongshu: Zhongshu | null;
  markers: SeriesMarker<Time>[];
  signalCallout: SignalCalloutItem | null;
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

const VALID_RELATIONS = new Set<ZhongshuRelation>([
  "initial",
  "new_up",
  "new_down",
  "expansion_up",
  "expansion_down",
  "overlap",
]);

function resolveZhongshuRelation(curr: Zhongshu, prev: Zhongshu | undefined): ZhongshuRelation {
  if (curr.relation_to_prev && VALID_RELATIONS.has(curr.relation_to_prev as ZhongshuRelation)) {
    return curr.relation_to_prev as ZhongshuRelation;
  }
  if (!prev) return "initial";
  const prevDD = prev.oscillation_dd ?? prev.dd ?? prev.zd;
  const prevGG = prev.oscillation_gg ?? prev.gg ?? prev.zg;
  const currDD = curr.oscillation_dd ?? curr.dd ?? curr.zd;
  const currGG = curr.oscillation_gg ?? curr.gg ?? curr.zg;
  if (curr.zd > prev.zg) {
    return currDD > prevGG ? "new_up" : "expansion_up";
  }
  if (curr.zg < prev.zd) {
    return currGG < prevDD ? "new_down" : "expansion_down";
  }
  return "overlap";
}

function resolveExtensionStage(zs: Zhongshu): ZhongshuExtensionStage {
  if (zs.extension_stage === "core" || zs.extension_stage === "extended" || zs.extension_stage === "upgraded_9") {
    return zs.extension_stage;
  }
  if (zs.is_nine_upgrade || (zs.oscillation_strokes ?? 0) >= 6) return "upgraded_9";
  if (zs.is_extended || (zs.oscillation_strokes ?? 0) > 0) return "extended";
  return "core";
}

/**
 * Compute price position relative to a Zhongshu [ZD, ZG] matching backend `chanlun.py` format:
 * `above_zg(+X.XX%)` | `below_zd(-X.XX%)` | `inside_zs(XX%)`.
 */
export function computePosVsZs(lastClose: number | null | undefined, zs: Zhongshu | null | undefined): string | null {
  if (lastClose === null || lastClose === undefined || !Number.isFinite(lastClose) || !zs) return null;
  if (lastClose > zs.zg && zs.zg > 0) {
    const pct = (lastClose / zs.zg - 1) * 100;
    return `above_zg(+${pct.toFixed(2)}%)`;
  }
  if (lastClose < zs.zd && zs.zd > 0) {
    const pct = (lastClose / zs.zd - 1) * 100;
    return `below_zd(${pct.toFixed(2)}%)`;
  }
  const span = zs.zg - zs.zd;
  const rel = span > 0 ? (lastClose - zs.zd) / span : 0.5;
  return `inside_zs(${Math.round(rel * 100)}%)`;
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

  for (let idx = 0; idx < list.length; idx++) {
    const zs = list[idx];
    const prev = idx > 0 ? list[idx - 1] : undefined;
    const relationToPrev = resolveZhongshuRelation(zs, prev);
    const extensionStage = resolveExtensionStage(zs);

    const sCmp = comparableTimestampKey(zs.start_date, intraday);
    const cCmp = comparableTimestampKey(zs.end_date, intraday);
    const hasExtDate = Boolean(zs.oscillation_end_date && zs.oscillation_end_date !== zs.end_date);
    const eCmp = hasExtDate ? comparableTimestampKey(zs.oscillation_end_date, intraday) : null;

    let coreStartBar = resolve(zs.start_date);
    let coreEndBar = resolve(zs.end_date);
    let extEndBar = hasExtDate ? resolve(zs.oscillation_end_date) : null;
    const nineBar = zs.nine_upgrade_date ? resolve(zs.nine_upgrade_date) : null;

    let clampedLeft = false;
    let clampedRight = false;

    /* Clamp core [start_date, end_date] strictly only when it genuinely crosses the visible bar window */
    const coreBeforeLeft = Boolean(sCmp && firstCmp && sCmp < firstCmp);
    const coreEndBeforeLeft = Boolean(cCmp && firstCmp && cCmp < firstCmp);
    const coreAfterRight = Boolean(cCmp && lastCmp && cCmp > lastCmp);

    if (!coreStartBar && coreEndBar && coreBeforeLeft && coreEndBar.idx > firstBar.idx) {
      coreStartBar = firstBar;
      clampedLeft = true;
    }
    if (coreStartBar && !coreEndBar && coreAfterRight && coreStartBar.idx < lastBar.idx) {
      coreEndBar = lastBar;
      clampedRight = true;
    }
    if (!coreStartBar && !coreEndBar && coreBeforeLeft && coreAfterRight) {
      coreStartBar = firstBar;
      coreEndBar = lastBar;
      clampedLeft = true;
      clampedRight = true;
    }

    const hasCore = Boolean(coreStartBar && coreEndBar);
    const [cb1, cb2] =
      coreStartBar && coreEndBar
        ? coreStartBar.idx <= coreEndBar.idx
          ? [coreStartBar, coreEndBar]
          : [coreEndBar, coreStartBar]
        : [null, null];

    /* Resolve extension tail [end_date, oscillation_end_date] independently without fabricating a core */
    let extStartBar: IndexedBar | null = null;
    if (hasExtDate) {
      if (!extEndBar && eCmp && lastCmp && eCmp > lastCmp) {
        if (cb2 || (coreEndBeforeLeft && firstCmp && eCmp >= firstCmp)) {
          extEndBar = lastBar;
          clampedRight = true;
        }
      }
      if (extEndBar) {
        if (cb2) {
          extStartBar = cb2;
        } else if (coreEndBeforeLeft && extEndBar.idx >= firstBar.idx) {
          extStartBar = firstBar;
          clampedLeft = true;
        }
      }
    }

    const hasExt = Boolean(extStartBar && extEndBar && extEndBar.idx > extStartBar.idx);
    if (!hasCore && !hasExt) continue;

    const anchorIdx = cb1 ? cb1.idx : (extStartBar?.idx ?? firstBar.idx);
    const compactCode = kind === "segment" ? `S${zs.zs_id}` : `Z${zs.zs_id}`;
    const fullPrefix = kind === "segment" ? `线段中枢 ${compactCode}` : `笔中枢 ${compactCode}`;
    const truncTag = clampedLeft && clampedRight ? "·双端截断" : clampedLeft ? "·左截断" : clampedRight ? "·右截断" : "";

    out.push({
      key: `${kind}-${zs.zs_id}-${anchorIdx}`,
      zsId: zs.zs_id,
      kind,
      label: `${fullPrefix}${truncTag}`,
      compactCode,
      coreStartTime: cb1 ? cb1.time : null,
      coreEndTime: cb2 ? cb2.time : null,
      extStartTime: hasExt && extStartBar ? extStartBar.time : null,
      extEndTime: hasExt && extEndBar ? extEndBar.time : null,
      nineUpgradeTime: nineBar ? nineBar.time : null,
      startLabel: cb1 ? cb1.label : zs.start_date,
      coreEndLabel: cb2 ? cb2.label : zs.end_date,
      extEndLabel: hasExt && extEndBar ? extEndBar.label : (zs.oscillation_end_date ?? null),
      nineUpgradeLabel: zs.nine_upgrade_date ?? null,
      clampedLeft,
      clampedRight,
      zd: zs.zd,
      zg: zs.zg,
      dd: zs.dd,
      gg: zs.gg,
      oscillationStrokes: zs.oscillation_strokes ?? 0,
      isExtended: Boolean(zs.is_extended),
      isNineUpgrade: Boolean(zs.is_nine_upgrade),
      status: zs.status,
      trendZsIndex: zs.trend_zs_index,
      relationToPrev,
      extensionStage,
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
  let prevHist: number | null = null;
  const isDark = palette.mode === "dark";
  for (const b of bars) {
    if (b.hist !== null) {
      /* 4-tone MACD divergence histogram: solid when expanding, muted translucent when converging */
      const pos = b.hist >= 0;
      const expanding =
        prevHist === null || (pos ? b.hist >= prevHist : b.hist <= prevHist);
      const color = pos
        ? expanding
          ? isDark
            ? "rgba(46, 189, 133, 0.84)"
            : "rgba(13, 148, 98, 0.82)"
          : isDark
            ? "rgba(46, 189, 133, 0.34)"
            : "rgba(13, 148, 98, 0.32)"
        : expanding
          ? isDark
            ? "rgba(240, 101, 101, 0.84)"
            : "rgba(220, 38, 38, 0.82)"
          : isDark
            ? "rgba(240, 101, 101, 0.34)"
            : "rgba(220, 38, 38, 0.32)";
      macdHist.push({
        time: b.time,
        value: b.hist,
        color,
      });
      prevHist = b.hist;
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

  /* Segments (线段): preserve ALL contiguous confirmed segment chains across gaps without false bridges */
  type SegmentChain = {
    chains: LineData<Time>[][];
    data: LineData<Time>[];
    lastIdx: number | null;
    hadGap: boolean;
  };
  const confirmedChain: SegmentChain = { chains: [], data: [], lastIdx: null, hadGap: false };
  const unconfirmedChain: SegmentChain = { chains: [], data: [], lastIdx: null, hadGap: false };

  const flushChain = (chain: SegmentChain) => {
    if (chain.data.length >= 2) {
      chain.chains.push([...chain.data]);
    }
    chain.data = [];
  };

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
      /* Preserve completed chain before starting a separate contiguous chain across the gap */
      flushChain(chain);
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

  /* Markers: clean micro-dots for recent fractal pivots; text labels reserved strictly for 1/2/3 Buy/Sell signals */
  const rawMarkers: { idx: number; marker: SeriesMarker<Time> }[] = [];

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
        color: isTop ? palette.bear : palette.bull,
        size: 0.42,
      },
    });
  }

  /* Latest fractal subtle square pivot (no cluttering text) */
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
          size: 0.65,
        },
      });
    }
  }

  /* Directional Buy / Sell marker strictly governed by signal_meta.direction & signal_meta.confirmed */
  let signalCallout: SignalCalloutItem | null = null;
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
        const sigColor = isLong ? palette.bull : palette.bear;
        const sigPosition: "belowBar" | "aboveBar" = isLong ? "belowBar" : "aboveBar";
        rawMarkers.push({
          idx: sigBar.idx,
          marker: {
            time: sigBar.time,
            position: sigPosition,
            shape: isLong ? "arrowUp" : "arrowDown",
            color: sigColor,
            size: 1.25,
          },
        });
        signalCallout = {
          time: sigBar.time,
          price: isLong ? sigBar.low : sigBar.high,
          position: sigPosition,
          text: `${conciseLabel}${isConfirmed ? "·已确认" : "(待确认)"}`,
          color: sigColor,
        };
      }
    }
  }

  rawMarkers.sort((a, b) => a.idx - b.idx);

  const allConfirmedSegmentChains = [
    ...confirmedChain.chains,
    ...(confirmedChain.data.length >= 2 ? [confirmedChain.data] : []),
  ];

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
    confirmedSegmentChains: allConfirmedSegmentChains,
    unconfirmedSegments: unconfirmedChain.data,
    strokeZhongshuBoxes,
    segmentZhongshuBoxes,
    latestStrokeZhongshu,
    latestSegmentZhongshu,
    markers: rawMarkers.map((x) => x.marker),
    signalCallout,
  };
}
