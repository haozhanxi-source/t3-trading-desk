import type { CompassScanContext, ScanRow } from "../api/types";

export type RowFilter =
  | "all"
  | "long"
  | "short"
  | "watch"
  | "aligned"
  | "counter"
  | "pool_unverified"
  | "degraded";

export type CompassRowAlignment =
  | "aligned"
  | "counter"
  | "watch"
  | "pool_unverified"
  | "degraded";

export type ScanDirectionClass = "long" | "short" | "watch";
export type ScanSignalKind = "signal" | "momentum" | "watch";

export interface ScanRowClassification {
  direction: ScanDirectionClass;
  kind: ScanSignalKind;
}

export interface ShortChaseWarning {
  label: string;
  detail: string;
}

export interface CompassRowAlignmentResult {
  status: CompassRowAlignment;
  alignKind: "signal" | "momentum" | null;
  expected: "long" | "short" | null;
  actual: ScanDirectionClass;
  boundToL1: boolean;
  chaseWarning: ShortChaseWarning | null;
}

const STRONG_MOMENTUM_LONG_TYPES = new Set(["momentum_long", "steep_non_div_up"]);
const STRONG_MOMENTUM_SHORT_TYPES = new Set(["momentum_short", "steep_non_div_down"]);

export function resolveExpectedDirection(
  row: ScanRow,
  ctx: CompassScanContext,
): { expected: "long" | "short" | null; boundToL1: boolean } {
  const sym = (row.symbol || "").trim().toUpperCase();
  if (sym && ctx.expected_directions[sym]) {
    return { expected: ctx.expected_directions[sym], boundToL1: true };
  }
  const stripped = sym.replace(/\.(SS|SH|SZ|BJ)$/, "");
  if (stripped && stripped !== sym && ctx.expected_directions[stripped]) {
    return { expected: ctx.expected_directions[stripped], boundToL1: true };
  }
  if (/^\d{6}$/.test(stripped)) {
    for (const [k, v] of Object.entries(ctx.expected_directions)) {
      if (k.replace(/\.(SS|SH|SZ|BJ)$/, "") === stripped) {
        return { expected: v, boundToL1: true };
      }
    }
  }
  /* Only treat symbols explicitly entered by the user (market === "adhoc") as bound to the staged L1 sector thesis;
     do NOT blindly broadcast a narrow L1 thesis (e.g. phosphate/sulfur) onto unrelated members of a broad pool (e.g. rubber RU0)! */
  const isUserCustom = (row.market || "").trim().toLowerCase() === "adhoc";
  if (isUserCustom && (ctx.direction === "long" || ctx.direction === "short")) {
    return { expected: ctx.direction, boundToL1: true };
  }
  return { expected: null, boundToL1: false };
}

/**
 * Classifies a daily Chanlun scan row into direction (`long` | `short` | `watch`)
 * and strength kind (`signal` = confirmed discrete 1/2/3 buy/sell point;
 * `momentum` = strong non-divergent capital consensus; `watch` = unconfirmed / neutral / exhaustion).
 */
export function classifyScanRowDetail(row: ScanRow): ScanRowClassification {
  const sigType = (row.signal_type || "").trim().toLowerCase();
  const tierTxt = (row.signal_tier || "").trim();
  const explicitDir = (row.signal_direction || "").trim().toLowerCase();

  /* 0. Hard exclusions: errors, degraded/insufficient feeds, candidates, weak momentum, divergence watch, exhaustion warnings */
  if (
    row.error ||
    row.quality_grade === "degraded" ||
    sigType === "data_quality_degraded" ||
    sigType === "insufficient_data" ||
    sigType.includes("candidate") ||
    sigType.includes("weak_momentum") ||
    sigType.includes("divergence_watch") ||
    sigType.includes("exhaustion") ||
    /(?:观察|待确认|勿追|警告|数据质量降级|数据不足)/.test(tierTxt)
  ) {
    return { direction: "watch", kind: "watch" };
  }

  /* 1. Strong capital consensus / steep non-divergent leaving stroke
        Backend sets confirmed=False (no discrete 1/2/3 point yet) with momentum_confirmed=True.
        Per Layer 2 rules, strong consensus IS valid daily directional alignment (distinct from confirmed buy/sell points). */
  if (STRONG_MOMENTUM_LONG_TYPES.has(sigType)) {
    return { direction: "long", kind: "momentum" };
  }
  if (STRONG_MOMENTUM_SHORT_TYPES.has(sigType)) {
    return { direction: "short", kind: "momentum" };
  }
  if (!sigType && tierTxt.startsWith("资金一致推进")) {
    if (explicitDir === "long" || explicitDir === "bullish" || explicitDir === "up" || tierTxt.includes("顺势")) {
      return { direction: "long", kind: "momentum" };
    }
    if (explicitDir === "short" || explicitDir === "bearish" || explicitDir === "down" || tierTxt.includes("勿逆势")) {
      return { direction: "short", kind: "momentum" };
    }
  }

  /* 2. All other unconfirmed signals stay watch */
  if (row.signal_confirmed === false) {
    return { direction: "watch", kind: "watch" };
  }

  /* 3. Authoritative confirmed backend signal_meta.direction */
  if (explicitDir === "long" || explicitDir === "bullish" || explicitDir === "up") {
    return { direction: "long", kind: "signal" };
  }
  if (explicitDir === "short" || explicitDir === "bearish" || explicitDir === "down") {
    return { direction: "short", kind: "signal" };
  }
  if (explicitDir === "neutral") {
    return { direction: "watch", kind: "watch" };
  }

  /* 4. Conservative fallback for persisted legacy rows without signal_meta */
  const cleaned = `${tierTxt} ${row.action_bias || ""}`
    .replace(/买卖点信号/g, "")
    .replace(/(?:严禁|禁止|切勿|勿|不可|非|待确认|观察)[^，,。；;]*/g, "")
    .toLowerCase();

  const isShort = /(?:一卖|二卖|三卖|1卖|2卖|3卖)/.test(cleaned);
  const isLong = /(?:一买|二买|三买|1买|2买|3买)/.test(cleaned);
  if (isShort && !isLong) return { direction: "short", kind: "signal" };
  if (isLong && !isShort) return { direction: "long", kind: "signal" };
  if (/空头资金一致/.test(cleaned) && !/多头资金一致/.test(cleaned)) {
    return { direction: "short", kind: "momentum" };
  }
  if (/多头资金一致/.test(cleaned) && !/空头资金一致/.test(cleaned)) {
    return { direction: "long", kind: "momentum" };
  }
  return { direction: "watch", kind: "watch" };
}

export function classifyScanRow(row: ScanRow): ScanDirectionClass {
  return classifyScanRowDetail(row).direction;
}

/**
 * Flags shorts that have already plunged deep below daily ZD or collapsed sharply over 20D,
 * preventing blind chasing far below the zhongshu. (Asymmetric by design: longs follow
 * anti-acrophobia offense rules and rely on backend exhaustion_buy_warn for late 3rd buys.)
 */
export function detectShortChaseWarning(row: ScanRow): ShortChaseWarning | null {
  const close = typeof row.close === "number" && Number.isFinite(row.close) ? row.close : null;
  const zd =
    Array.isArray(row.daily_zs) &&
    typeof row.daily_zs[0] === "number" &&
    Number.isFinite(row.daily_zs[0]) &&
    row.daily_zs[0] > 0
      ? row.daily_zs[0]
      : null;
  const distBelowZdPct =
    close !== null && zd !== null && close < zd ? ((close - zd) / zd) * 100 : null;
  const ret20 =
    typeof row["ret_20d_%"] === "number" && Number.isFinite(row["ret_20d_%"])
      ? row["ret_20d_%"]
      : null;
  const posRaw = (row.pos_vs_zs || "").toLowerCase();
  const isBelowZs =
    (distBelowZdPct !== null && distBelowZdPct < 0) ||
    posRaw.includes("below") ||
    posRaw.includes("下");

  const deepBelowZd = distBelowZdPct !== null && distBelowZdPct <= -20;
  const steepDrop20d = ret20 !== null && ret20 <= -25;
  const brokenAndPlunged = isBelowZs && ret20 !== null && ret20 <= -15;

  if (!deepBelowZd && !steepDrop20d && !brokenAndPlunged) {
    return null;
  }

  const parts: string[] = [];
  if (distBelowZdPct !== null) {
    parts.push(`低于中枢ZD ${Math.abs(distBelowZdPct).toFixed(1)}%`);
  }
  if (ret20 !== null && ret20 < 0) {
    parts.push(`20D已跌 ${Math.abs(ret20).toFixed(1)}%`);
  }
  return {
    label: "防深坑追空",
    detail: `${parts.join(" · ")}${parts.length > 0 ? "，" : ""}远离中枢勿盲目追空，等反抽30min卖点再评估`,
  };
}

export function classifyRowWithCompass(
  row: ScanRow,
  ctx: CompassScanContext,
): CompassRowAlignmentResult {
  const { expected, boundToL1 } = resolveExpectedDirection(row, ctx);
  const sigType = (row.signal_type || "").trim().toLowerCase();
  if (row.error || row.quality_grade === "degraded" || sigType === "data_quality_degraded") {
    return {
      status: "degraded",
      alignKind: null,
      expected,
      actual: "watch",
      boundToL1,
      chaseWarning: null,
    };
  }
  const detail = classifyScanRowDetail(row);
  if (!boundToL1 || !expected) {
    return {
      status: "pool_unverified",
      alignKind: null,
      expected: null,
      actual: detail.direction,
      boundToL1: false,
      chaseWarning: detail.direction === "short" ? detectShortChaseWarning(row) : null,
    };
  }
  if (detail.direction === expected) {
    return {
      status: "aligned",
      alignKind: detail.kind === "momentum" ? "momentum" : "signal",
      expected,
      actual: detail.direction,
      boundToL1: true,
      chaseWarning: expected === "short" ? detectShortChaseWarning(row) : null,
    };
  }
  if (
    (detail.direction === "long" && expected === "short") ||
    (detail.direction === "short" && expected === "long")
  ) {
    return {
      status: "counter",
      alignKind: detail.kind === "momentum" ? "momentum" : "signal",
      expected,
      actual: detail.direction,
      boundToL1: true,
      chaseWarning: null,
    };
  }
  return {
    status: "watch",
    alignKind: null,
    expected,
    actual: detail.direction,
    boundToL1: true,
    chaseWarning: null,
  };
}
