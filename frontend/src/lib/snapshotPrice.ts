import type { MarketDualResponse } from "../api/types";

export interface TrustedQuoteSelection {
  price: number | null;
  timestamp: string | null;
  source: "intraday" | "daily" | null;
}

const CFFEX_BOND_ROOT_RE = /^(T|TF|TS|TL)\d+/i;

function parseClockMinutes(label: string | null | undefined): number | null {
  if (!label) return null;
  const m = label.match(/[T ](\d{2}):(\d{2})/);
  if (!m) return null;
  const hh = Number(m[1]);
  const mm = Number(m[2]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return null;
  return hh * 60 + mm;
}

function parseIntervalMinutes(data: MarketDualResponse): number {
  const raw = String(
    data.intraday_interval ||
      (data.intraday_technicals?.bar_interval as string | undefined) ||
      "30m",
  )
    .trim()
    .toLowerCase();
  const m = raw.match(/^(\d+)\s*(m|min|h)$/);
  if (!m) return 30;
  const qty = Number(m[1]);
  if (!Number.isFinite(qty) || qty <= 0) return 30;
  return m[2] === "h" ? qty * 60 : qty;
}

function resolveIntradayAnchor(data: MarketDualResponse): "start" | "end" {
  const fromTech = String(data.intraday_technicals?.bar_timestamp_anchor || "")
    .trim()
    .toLowerCase();
  if (fromTech === "start" || fromTech === "end") return fromTech;
  const ac = (data.asset_class || "").toLowerCase();
  return ac === "cn_future" || ac === "a_share" ? "end" : "start";
}

function parseIsoUtcDate(isoDate: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) {
    return null;
  }
  return dt;
}

/**
 * Deterministic NYSE/Nasdaq scheduled 13:00 ET early-close calendar check:
 * 1. July 3 when Mon-Thu (when July 3 is Friday, July 4 falls on Saturday and July 3 is an observed full-day holiday).
 * 2. Day after Thanksgiving (Friday following the 4th Thursday of November, i.e., Nov 23..29 on Friday).
 * 3. Christmas Eve (Dec 24 when Mon-Thu; when Dec 24 is Friday, Dec 25 falls on Saturday and Dec 24 is an observed full-day holiday).
 */
export function isUsEquityEarlyCloseDate(isoDate: string): boolean {
  const dt = parseIsoUtcDate(isoDate);
  if (!dt) return false;
  const month = dt.getUTCMonth() + 1;
  const day = dt.getUTCDate();
  const dow = dt.getUTCDay(); // 0=Sun .. 5=Fri, 6=Sat
  if (month === 7 && day === 3) return dow >= 1 && dow <= 4;
  if (month === 11 && dow === 5 && day >= 23 && day <= 29) return true;
  if (month === 12 && day === 24) return dow >= 1 && dow <= 4;
  return false;
}

function isCnFutureNightBarForProvisionalDaily(
  data: MarketDualResponse,
  dailyDate: string,
  intraLabel: string | null,
): boolean {
  if ((data.asset_class || "").toLowerCase() !== "cn_future") return false;
  if (data.data_quality?.latest_bar_closed !== false) return false;
  if (!dailyDate || !intraLabel) return false;
  const intraDate = intraLabel.slice(0, 10);
  const dDt = parseIsoUtcDate(dailyDate);
  const iDt = parseIsoUtcDate(intraDate);
  if (!dDt || !iDt) return false;
  const gapDays = Math.round((dDt.getTime() - iDt.getTime()) / 86_400_000);
  const mins = parseClockMinutes(intraLabel);
  if (mins === null) return false;
  const iDow = iDt.getUTCDay(); // 0=Sun, 1=Mon .. 5=Fri, 6=Sat

  // Evening night session (20:50-23:59): Mon-Thu maps to next calendar day (gap=1); Fri maps to Monday (gap=3).
  if (mins >= 20 * 60 + 50) {
    if (iDow >= 1 && iDow <= 4) return gapDays === 1;
    if (iDow === 5) return gapDays === 3;
    return false;
  }
  // Post-midnight night session (00:00-02:35): Tue-Fri belong to SAME calendar day (gap=0, handled by intraDate>=dailyDate);
  // ONLY Saturday 00:00-02:35 belongs to Monday's trading date (gap=2).
  if (mins <= 2 * 60 + 35) {
    return iDow === 6 && gapDays === 2;
  }
  return false;
}

function isSameDayIntradayBeforeDailyClose(
  data: MarketDualResponse,
  dailyDate: string,
  intraLabel: string | null,
): boolean {
  if (!dailyDate || !intraLabel) return false;
  if (intraLabel.slice(0, 10) !== dailyDate) return false;
  if (data.data_quality?.latest_bar_closed !== true) return false;
  const mins = parseClockMinutes(intraLabel);
  if (mins === null) return false;

  const anchor = resolveIntradayAnchor(data);
  const barMins = parseIntervalMinutes(data);
  const barEndMins = anchor === "start" ? mins + barMins : mins;
  const ac = (data.asset_class || "").toLowerCase();

  if (ac === "cn_future") {
    const closeMins = CFFEX_BOND_ROOT_RE.test((data.symbol || "").trim()) ? 15 * 60 + 15 : 15 * 60;
    return barEndMins < closeMins;
  }
  if (ac === "a_share") {
    return barEndMins < 15 * 60;
  }
  if (ac === "us_stock_or_etf") {
    const closeMins = isUsEquityEarlyCloseDate(dailyDate) ? 13 * 60 : 16 * 60;
    return barEndMins < closeMins;
  }
  return false;
}

export function isSnapshotIntradayUntrusted(
  data: MarketDualResponse | null | undefined,
  snapshotError?: string | null,
  staleAfterError?: boolean,
): boolean {
  if (snapshotError || staleAfterError) return true;
  if (!data) return false;
  if (data.intraday_error || data.intraday_stale_fallback) return true;
  const intraWarnings = data.intraday_data_quality?.warnings ?? [];
  if (intraWarnings.includes("STALE_MARKET_DATA")) return true;
  const dailyBars = data.ohlcv_daily;
  const intraBars = data.ohlcv_intraday;
  const dailyLast = dailyBars && dailyBars.length > 0 ? dailyBars[dailyBars.length - 1] : null;
  const intraLast = intraBars && intraBars.length > 0 ? intraBars[intraBars.length - 1] : null;
  if (dailyLast && intraLast) {
    const dailyDate = (dailyLast.label || dailyLast.time || "").slice(0, 10);
    const intraLabel = intraLast.label || null;
    if (dailyDate && intraLabel) {
      const intraDate = intraLabel.slice(0, 10);
      // Regardless of whether the daily bar is closed or provisional, a prior-day intraday bar
      // (that is NOT a valid night-session bar belonging to dailyDate) MUST be marked untrusted!
      if (intraDate < dailyDate && !isCnFutureNightBarForProvisionalDaily(data, dailyDate, intraLabel)) {
        return true;
      }
      if (
        data.data_quality?.latest_bar_closed === true &&
        isSameDayIntradayBeforeDailyClose(data, dailyDate, intraLabel)
      ) {
        return true;
      }
    }
  }
  return false;
}

/**
 * Pick the most recent trusted quote from a dual-timeframe snapshot.
 *
 * Rules:
 * 1. When intraday is errored/fallback/stale or lags behind the daily bar (closed or provisional), prefer daily close.
 * 2. When a Chinese future has a provisional unclosed daily bar tagged with the next trading date
 *    AND the intraday bar is a matching night-session bar (Mon-Fri 20:50-23:59 or Sat 00:00-02:35), prefer intraday.
 * 3. Otherwise compare calendar dates and prefer intraday only when intraDate >= dailyDate.
 */
export function selectTrustedLastQuote(
  data: MarketDualResponse | null | undefined,
  snapshotError?: string | null,
  staleAfterError?: boolean,
): TrustedQuoteSelection {
  if (!data) {
    return { price: null, timestamp: null, source: null };
  }
  const dailyBars = data.ohlcv_daily;
  const intraBars = data.ohlcv_intraday;
  const dailyLast = dailyBars && dailyBars.length > 0 ? dailyBars[dailyBars.length - 1] : null;
  const intraLast = intraBars && intraBars.length > 0 ? intraBars[intraBars.length - 1] : null;
  const intradayUntrusted = isSnapshotIntradayUntrusted(data, snapshotError, staleAfterError);

  const dailyLabel = dailyLast ? (dailyLast.label || dailyLast.time || null) : null;
  const intraLabel = intraLast ? (intraLast.label || null) : null;

  if (intradayUntrusted) {
    if (dailyLast && Number.isFinite(dailyLast.close)) {
      return { price: dailyLast.close, timestamp: dailyLabel, source: "daily" };
    }
    if (intraLast && Number.isFinite(intraLast.close)) {
      return { price: intraLast.close, timestamp: intraLabel, source: "intraday" };
    }
    return { price: null, timestamp: null, source: null };
  }

  const dailyDate = (dailyLabel || "").slice(0, 10);
  const intraDate = (intraLabel || "").slice(0, 10);

  if (
    intraLast &&
    Number.isFinite(intraLast.close) &&
    isCnFutureNightBarForProvisionalDaily(data, dailyDate, intraLabel)
  ) {
    return { price: intraLast.close, timestamp: intraLabel, source: "intraday" };
  }

  if (intraLast && Number.isFinite(intraLast.close) && (!dailyLast || !dailyDate || intraDate >= dailyDate)) {
    return { price: intraLast.close, timestamp: intraLabel, source: "intraday" };
  }
  if (dailyLast && Number.isFinite(dailyLast.close)) {
    return { price: dailyLast.close, timestamp: dailyLabel, source: "daily" };
  }
  if (intraLast && Number.isFinite(intraLast.close)) {
    return { price: intraLast.close, timestamp: intraLabel, source: "intraday" };
  }
  return { price: null, timestamp: null, source: null };
}
