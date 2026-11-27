import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AlertOctagon, ArrowRight, ShieldCheck } from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type { Position, ScaleInBody, ScaleInResponse } from "../../api/types";
import {
  fmtInputPrice,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtPlanNum,
  fmtPrice,
  inferAssetCurrency,
  optNum,
} from "../../lib/format";
import { formatSymbolWithCode } from "../../lib/symbols";
import { CrossCurrencyFxBanner } from "../ui/CrossCurrencyFxBanner";
import { ModalDialog } from "../ui/ModalDialog";
import { DirectionBadge, StatusBadge, signedToneClass } from "../ui/StatusBadge";

export interface ScaleInPositionDialogProps {
  open: boolean;
  onClose: () => void;
  onDone: (result: ScaleInResponse) => void;
  position: Position | null;
  currentPrice?: number | null;
  accountCurrency?: string | null;
}

interface PositionQtySpec {
  minOrderQty: number;
  lotStep: number;
  fractional: boolean;
}

function inferPositionQtySpec(pos: Position): PositionQtySpec {
  const rawSpec =
    pos.contract_spec && typeof pos.contract_spec === "object"
      ? (pos.contract_spec as Record<string, unknown>)
      : null;

  const specMin =
    rawSpec && typeof rawSpec.min_order_qty === "number" && Number.isFinite(rawSpec.min_order_qty) && rawSpec.min_order_qty > 0
      ? rawSpec.min_order_qty
      : null;
  const specStep =
    rawSpec && typeof rawSpec.lot_step === "number" && Number.isFinite(rawSpec.lot_step) && rawSpec.lot_step > 0
      ? rawSpec.lot_step
      : rawSpec && typeof rawSpec.lot_size === "number" && Number.isFinite(rawSpec.lot_size) && rawSpec.lot_size > 0
        ? rawSpec.lot_size
        : null;
  const specFractional =
    rawSpec && typeof rawSpec.fractional === "boolean" ? rawSpec.fractional : null;

  const ac = (pos.asset_class || "").trim().toLowerCase();
  const sym = (pos.symbol || "").trim().toUpperCase();

  let minOrderQty = 1;
  let lotStep = 1;
  let fractional = false;

  if (ac === "a_share" || ac === "cn_equity" || /^\d{6}(\.(SS|SZ|BJ))?$/.test(sym)) {
    const code = sym.split(".")[0] || "";
    if (code.startsWith("688") && (sym.endsWith(".SS") || !sym.includes("."))) {
      minOrderQty = 200;
      lotStep = 1;
    } else if (sym.endsWith(".BJ") || code.startsWith("4") || code.startsWith("8") || code.startsWith("92")) {
      minOrderQty = 100;
      lotStep = 1;
    } else {
      minOrderQty = 100;
      lotStep = 100;
    }
    fractional = false;
  } else if (ac === "fx" || ac === "forex" || /^[A-Z]{6}=X$/.test(sym)) {
    minOrderQty = 1000;
    lotStep = 1000;
    fractional = false;
  } else if (ac === "crypto" || /^(BTC|ETH|SOL)(-USD)?$/.test(sym)) {
    minOrderQty = 0.001;
    lotStep = 0.001;
    fractional = true;
  } else if (!Number.isInteger(pos.quantity)) {
    minOrderQty = 0.001;
    lotStep = 0.001;
    fractional = true;
  }

  return {
    minOrderQty: specMin ?? minOrderQty,
    lotStep: specStep ?? lotStep,
    fractional: specFractional ?? fractional,
  };
}

function isStepAligned(qty: number, spec: PositionQtySpec): boolean {
  if (!Number.isFinite(qty) || qty + 1e-9 < spec.minOrderQty) return false;
  if (!spec.fractional && Math.abs(qty - Math.round(qty)) > 1e-6) return false;
  const steps = (qty - spec.minOrderQty) / spec.lotStep;
  return steps >= -1e-6 && Math.abs(steps - Math.round(steps)) < 1e-6;
}

function computeValidScaledQuantity(pos: Position, ratio: number, spec: PositionQtySpec): number | null {
  const raw = pos.quantity * ratio;
  if (!Number.isFinite(raw) || raw + 1e-12 < spec.minOrderQty) {
    return null;
  }
  const k = Math.floor((raw - spec.minOrderQty) / spec.lotStep + 1e-12);
  if (k < 0) return null;
  const quantized = spec.minOrderQty + k * spec.lotStep;
  const result = spec.fractional ? Number(quantized.toFixed(6)) : Math.round(quantized);
  return isStepAligned(result, spec) ? result : null;
}

const _f64Buf = new ArrayBuffer(8);
const _f64View = new DataView(_f64Buf);

function splitIeee754(x: number): { neg: boolean; num: bigint; den: bigint } | null {
  if (!Number.isFinite(x)) return null;
  if (x === 0) return { neg: false, num: 0n, den: 1n };
  _f64View.setFloat64(0, x, false);
  const bits = _f64View.getBigUint64(0, false);
  const neg = (bits >> 63n) !== 0n;
  const expBits = Number((bits >> 52n) & 0x7ffn);
  const fracBits = bits & 0xfffffffffffffn;
  const mantissa = expBits === 0 ? fracBits : (1n << 52n) | fracBits;
  const exp = expBits === 0 ? -1074 : expBits - 1075;
  if (exp >= 0) {
    return { neg, num: mantissa << BigInt(exp), den: 1n };
  }
  return { neg, num: mantissa, den: 1n << BigInt(-exp) };
}

function scaledIntToNumber(q: bigint, neg: boolean, decimals: number): number {
  if (q === 0n) return 0;
  const s = q.toString().padStart(decimals + 1, "0");
  const intPart = s.slice(0, s.length - decimals);
  const fracPart = s.slice(s.length - decimals);
  return Number(`${neg ? "-" : ""}${intPart}.${fracPart}`);
}

/** Exact bit-for-bit match to Python 3 `round(x, decimals)` (half-to-even on exact IEEE-754 value, zero float pre-scaling). */
function roundPython(x: number, decimals: number): number {
  const parts = splitIeee754(x);
  if (!parts || parts.num === 0n) return 0;
  const scale = 10n ** BigInt(decimals);
  const scaledNum = parts.num * scale;
  let q = scaledNum / parts.den;
  const rem = scaledNum % parts.den;
  const twiceRem = rem * 2n;
  if (twiceRem > parts.den || (twiceRem === parts.den && (q & 1n) === 1n)) {
    q += 1n;
  }
  return scaledIntToNumber(q, parts.neg, decimals);
}

function roundPython6(x: number): number {
  return roundPython(x, 6);
}

/** Format stop/price values without hiding sub-cent precision needed by 6-decimal ledger validation (e.g. 83.000001 vs 83.000002). */
function fmtStopPrice(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return "—";
  const p = roundPython6(val);
  const abs = Math.abs(p);
  const fixed6 = p.toFixed(6).replace(/0+$/, "");
  const fracLen = fixed6.includes(".") ? fixed6.split(".")[1]!.length : 0;
  const minFrac = abs >= 1 ? 2 : 4;
  const digits = Math.max(minFrac, fracLen);
  return p.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function createScaleInIdempotencyKey(positionId: string): string {
  const uid =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `scalein-${positionId}-${uid}`;
}

export function ScaleInPositionDialog({
  open,
  onClose,
  onDone,
  position,
  currentPrice,
  accountCurrency,
}: ScaleInPositionDialogProps) {
  const [addQty, setAddQty] = useState("");
  const [addPrice, setAddPrice] = useState("");
  const [newStopLoss, setNewStopLoss] = useState("");
  const [newDailyStop, setNewDailyStop] = useState("");
  const [requireBreakevenLock, setRequireBreakevenLock] = useState(true);
  const [maxCushionRatio, setMaxCushionRatio] = useState("0.65");
  const [fxRate, setFxRate] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);

  const resetIdempotencyKey = () => {
    idempotencyKeyRef.current = null;
  };

  const direction = position?.direction === "short" ? "short" : "long";
  const isLong = direction === "long";
  const isDailyRegime = position?.active_stop_regime === "daily_structure";
  const avgEntry = position ? (position.avg_entry_price ?? position.entry_price ?? null) : null;
  const currentStop = position?.tactical_stop_30m ?? null;
  const dailyStop = position?.structural_stop_daily ?? null;
  const activeOldStop = isDailyRegime ? dailyStop : currentStop;
  const partialRealized = Number(position?.partial_realized_pnl ?? 0);
  const hasPriorRealizedLoss = Number.isFinite(partialRealized) && partialRealized < -1e-9;
  const currentTrancheCount =
    position && Array.isArray(position.tranches) && position.tranches.length > 0
      ? position.tranches.length
      : 1;
  const nextTrancheTag = `T${currentTrancheCount + 1}`;

  const contractMult = useMemo(() => {
    if (!position) return 1;
    if (typeof position.multiplier === "number" && Number.isFinite(position.multiplier) && position.multiplier > 0) {
      return position.multiplier;
    }
    const rawSpec =
      position.contract_spec && typeof position.contract_spec === "object"
        ? (position.contract_spec as Record<string, unknown>)
        : null;
    if (
      rawSpec &&
      typeof rawSpec.multiplier === "number" &&
      Number.isFinite(rawSpec.multiplier) &&
      rawSpec.multiplier > 0
    ) {
      return rawSpec.multiplier;
    }
    return 1;
  }, [position]);

  const qtySpec = useMemo<PositionQtySpec>(
    () => (position ? inferPositionQtySpec(position) : { minOrderQty: 1, lotStep: 1, fractional: false }),
    [position],
  );
  const quickQtyFull = useMemo(
    () => (position ? computeValidScaledQuantity(position, 1, qtySpec) : null),
    [position, qtySpec],
  );
  const quickQtyHalf = useMemo(
    () => (position ? computeValidScaledQuantity(position, 0.5, qtySpec) : null),
    [position, qtySpec],
  );

  useEffect(() => {
    resetIdempotencyKey();
    if (!open || !position) return;
    const defaultQty =
      (currentTrancheCount >= 2 ? quickQtyHalf : quickQtyFull) ??
      quickQtyFull ??
      quickQtyHalf ??
      qtySpec.minOrderQty;
    setAddQty(defaultQty > 0 ? fmtPlanNum(defaultQty, 6) : "");
    setAddPrice(currentPrice != null && Number.isFinite(currentPrice) ? fmtInputPrice(currentPrice) : "");
    setNewStopLoss("");
    setNewDailyStop("");
    const profitableNow =
      currentPrice != null &&
      Number.isFinite(currentPrice) &&
      avgEntry != null &&
      (isLong ? currentPrice > avgEntry : currentPrice < avgEntry);
    setRequireBreakevenLock(isDailyRegime || profitableNow || currentPrice == null);
    setMaxCushionRatio("0.65");
    setFxRate(
      position.fx_to_account != null && position.fx_to_account > 0
        ? fmtPlanNum(position.fx_to_account, 6)
        : "",
    );
    setNote("");
    setErr(null);
  }, [open, position, currentPrice, avgEntry, isLong, isDailyRegime, currentTrancheCount, quickQtyFull, quickQtyHalf, qtySpec.minOrderQty]);

  const acctCcy = (accountCurrency || "USD").toUpperCase();
  const quoteCcy = inferAssetCurrency(position?.symbol, position?.asset_class);
  const needsCrossFx = Boolean(quoteCcy && quoteCcy !== acctCcy);

  const currentPnlPct = useMemo(() => {
    if (currentPrice == null || !Number.isFinite(currentPrice) || avgEntry == null || avgEntry <= 0) {
      return null;
    }
    return isLong ? ((currentPrice - avgEntry) / avgEntry) * 100 : ((avgEntry - currentPrice) / avgEntry) * 100;
  }, [currentPrice, avgEntry, isLong]);

  const addQtyNum = optNum(addQty);
  const addPriceNum = optNum(addPrice);
  const newStopNum = optNum(newStopLoss);
  const newDailyStopNum = optNum(newDailyStop);
  const cushionRatioNum = optNum(maxCushionRatio);
  const fxVal = optNum(fxRate);

  // Align all inputs & position prices with backend ledger persistence precision (_PRICE_PERSIST_DECIMALS = 6, _FX_PERSIST_DECIMALS = 10)
  const persistedAvgEntry = avgEntry != null && Number.isFinite(avgEntry) ? roundPython6(avgEntry) : null;
  const persistedCurrentStop = currentStop != null && Number.isFinite(currentStop) ? roundPython6(currentStop) : null;
  const persistedDailyStop = dailyStop != null && Number.isFinite(dailyStop) ? roundPython6(dailyStop) : null;
  const persistedActiveOldStop = isDailyRegime ? persistedDailyStop : persistedCurrentStop;
  const persistedAddPrice = addPriceNum !== undefined && addPriceNum > 0 ? roundPython6(addPriceNum) : undefined;
  const persistedNewStop = newStopNum !== undefined && newStopNum > 0 ? roundPython6(newStopNum) : undefined;
  const persistedNewDailyStop =
    newDailyStopNum !== undefined && newDailyStopNum > 0 ? roundPython6(newDailyStopNum) : undefined;

  const effectiveFx = useMemo(() => {
    if (fxVal !== undefined && fxVal > 0) return roundPython(fxVal, 10);
    if (position?.fx_to_account != null && Number.isFinite(position.fx_to_account) && position.fx_to_account > 0) {
      return roundPython(position.fx_to_account, 10);
    }
    return 1;
  }, [fxVal, position]);

  const isCurrentUnprofitable =
    currentPrice != null &&
    Number.isFinite(currentPrice) &&
    persistedAvgEntry != null &&
    (isLong ? currentPrice <= persistedAvgEntry : currentPrice >= persistedAvgEntry);

  const isAddPriceUnprofitable =
    persistedAddPrice !== undefined &&
    persistedAddPrice > 0 &&
    persistedAvgEntry != null &&
    (isLong ? persistedAddPrice <= persistedAvgEntry : persistedAddPrice >= persistedAvgEntry);

  const isDisciplineBlocked = isAddPriceUnprofitable || isCurrentUnprofitable;

  const stopValidationError = useMemo(() => {
    if (addPrice.trim() !== "") {
      if (addPriceNum === undefined || addPriceNum <= 0 || persistedAddPrice === undefined || persistedAddPrice <= 0) {
        return "加仓成交价在 6 位记账精度下必须大于 0";
      }
    }

    if (newDailyStop.trim() !== "") {
      if (
        newDailyStopNum === undefined ||
        newDailyStopNum <= 0 ||
        persistedNewDailyStop === undefined ||
        persistedNewDailyStop <= 0
      ) {
        return "新日线结构止损位在 6 位记账精度下必须大于 0";
      }
      if (persistedDailyStop != null) {
        if (isLong && persistedNewDailyStop < persistedDailyStop) {
          return `移动止损只紧不松：多单新日线结构止损 (${fmtStopPrice(persistedNewDailyStop)}) 不得低于原日线止损 (${fmtStopPrice(persistedDailyStop)})`;
        }
        if (!isLong && persistedNewDailyStop > persistedDailyStop) {
          return `移动止损只紧不松：空单新日线结构止损 (${fmtStopPrice(persistedNewDailyStop)}) 不得高于原日线止损 (${fmtStopPrice(persistedDailyStop)})`;
        }
      }
    }

    if (newStopLoss.trim() !== "") {
      if (newStopNum === undefined || newStopNum <= 0 || persistedNewStop === undefined || persistedNewStop <= 0) {
        return "新 30m 止损位在 6 位记账精度下必须大于 0";
      }
      if (persistedCurrentStop != null) {
        if (isLong && persistedNewStop < persistedCurrentStop) {
          return `移动止损只紧不松：多单新 30m 止损 (${fmtStopPrice(persistedNewStop)}) 不得低于原止损 (${fmtStopPrice(persistedCurrentStop)})`;
        }
        if (!isLong && persistedNewStop > persistedCurrentStop) {
          return `移动止损只紧不松：空单新 30m 止损 (${fmtStopPrice(persistedNewStop)}) 不得高于原止损 (${fmtStopPrice(persistedCurrentStop)})`;
        }
      }
    }

    const effDailyStop = persistedNewDailyStop ?? persistedDailyStop;
    if (isDailyRegime) {
      if (effDailyStop == null) {
        return "当前处于日线主升防线模式，请填写有效的日线主升防线 (new_daily_stop)";
      }
      if (persistedAddPrice !== undefined && persistedAddPrice > 0) {
        if (isLong && persistedAddPrice <= effDailyStop) {
          return `多单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格高于有效日线主升防线 (${fmtStopPrice(effDailyStop)})`;
        }
        if (!isLong && persistedAddPrice >= effDailyStop) {
          return `空单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格低于有效日线主升防线 (${fmtStopPrice(effDailyStop)})`;
        }
      }
      // In daily_structure regime, only validate add_price vs 30m stop if the user explicitly entered a new 30m stop.
      if (persistedNewStop !== undefined && persistedAddPrice !== undefined && persistedAddPrice > 0) {
        if (isLong && persistedAddPrice <= persistedNewStop) {
          return `多单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格高于所填 30m 止损位 (${fmtStopPrice(persistedNewStop)})`;
        }
        if (!isLong && persistedAddPrice >= persistedNewStop) {
          return `空单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格低于所填 30m 止损位 (${fmtStopPrice(persistedNewStop)})`;
        }
      }
    } else {
      const effStop30m = persistedNewStop ?? persistedCurrentStop;
      if (persistedAddPrice !== undefined && persistedAddPrice > 0 && effStop30m != null) {
        if (isLong && persistedAddPrice <= effStop30m) {
          return `多单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格高于有效 30m 止损位 (${fmtStopPrice(effStop30m)})`;
        }
        if (!isLong && persistedAddPrice >= effStop30m) {
          return `空单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格低于有效 30m 止损位 (${fmtStopPrice(effStop30m)})`;
        }
      }
      if (persistedAddPrice !== undefined && persistedAddPrice > 0 && effDailyStop != null) {
        if (isLong && persistedAddPrice <= effDailyStop) {
          return `多单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格高于有效日线结构止损 (${fmtStopPrice(effDailyStop)})`;
        }
        if (!isLong && persistedAddPrice >= effDailyStop) {
          return `空单加仓成交价 (${fmtStopPrice(persistedAddPrice)}) 必须严格低于有效日线结构止损 (${fmtStopPrice(effDailyStop)})`;
        }
      }
    }
    return null;
  }, [
    addPrice,
    addPriceNum,
    persistedAddPrice,
    isDailyRegime,
    newStopLoss,
    newStopNum,
    persistedNewStop,
    newDailyStop,
    newDailyStopNum,
    persistedNewDailyStop,
    persistedCurrentStop,
    persistedDailyStop,
    isLong,
  ]);

  const qtyValidationError = useMemo(() => {
    if (addQty.trim() === "") return null;
    if (addQtyNum === undefined || addQtyNum <= 0) {
      return "加仓数量必须为大于 0 的有效数值";
    }
    if (addQtyNum + 1e-9 < qtySpec.minOrderQty) {
      return `加仓数量 (${fmtNum(addQtyNum, 6)}) 低于该品种最小下单单位 (${fmtNum(qtySpec.minOrderQty, 6)})`;
    }
    if (!isStepAligned(addQtyNum, qtySpec)) {
      return `加仓数量 (${fmtNum(addQtyNum, 6)}) 不符合合约步进要求（最小 ${fmtNum(qtySpec.minOrderQty, 6)}，步进 ${fmtNum(qtySpec.lotStep, 6)}）`;
    }
    return null;
  }, [addQty, addQtyNum, qtySpec]);

  const fxValidationError = useMemo(() => {
    if (needsCrossFx && fxRate.trim() === "") {
      return `跨币种持仓请填写 ${quoteCcy} → ${acctCcy} 结算汇率`;
    }
    if (fxRate.trim() !== "" && (fxVal === undefined || fxVal <= 0)) {
      return "跨币种折算汇率必须为大于 0 的有效数值";
    }
    return null;
  }, [needsCrossFx, fxRate, fxVal, quoteCcy, acctCcy]);

  const effectiveRequireBreakeven = requireBreakevenLock || isDailyRegime;
  const effectiveCushionRatio =
    effectiveRequireBreakeven && cushionRatioNum !== undefined && cushionRatioNum > 0 && cushionRatioNum <= 1
      ? cushionRatioNum
      : 0.65;

  const preview = useMemo(() => {
    if (!position || persistedAvgEntry == null) return null;
    const oldQty = position.quantity;
    const validQty = addQtyNum !== undefined && addQtyNum > 0;
    const validPrice = persistedAddPrice !== undefined && persistedAddPrice > 0;
    const newTotalQty = validQty
      ? qtySpec.fractional
        ? roundPython(oldQty + addQtyNum, 8)
        : Math.round(oldQty + addQtyNum)
      : null;
    const rawNewAvg =
      validQty && validPrice && newTotalQty != null && newTotalQty > 0
        ? (persistedAvgEntry * oldQty + persistedAddPrice * addQtyNum) / newTotalQty
        : null;
    const newAvg = rawNewAvg != null ? roundPython6(rawNewAvg) : null;
    const activeNewStopInput = isDailyRegime ? persistedNewDailyStop : persistedNewStop;
    const effectiveStop =
      activeNewStopInput !== undefined && activeNewStopInput > 0 ? activeNewStopInput : persistedActiveOldStop;
    const isStopTightened =
      activeNewStopInput !== undefined &&
      persistedActiveOldStop != null &&
      (isLong ? activeNewStopInput > persistedActiveOldStop : activeNewStopInput < persistedActiveOldStop);

    const satisfiesBreakevenFor = (stopCand: number, avg: number, qty: number): boolean => {
      if (!(qty > 0)) return false;
      const effStop = roundPython6(stopCand);
      if (!(effStop > 0)) return false;
      const remOk = isLong ? effStop >= avg - 1e-9 : effStop <= avg + 1e-9;
      if (!remOk) return false;
      const remPnl =
        (isLong ? effStop - avg : avg - effStop) * qty * contractMult * effectiveFx;
      const netPnlRaw = partialRealized + remPnl;
      const campOpenRisk = roundPython(Math.max(0, -netPnlRaw), 2);
      return campOpenRisk <= 1e-6 && netPnlRaw >= -1e-6;
    };

    const satisfiesBackendBreakeven = (stopCand: number): boolean => {
      if (newAvg == null || newTotalQty == null || newTotalQty <= 0) return false;
      return satisfiesBreakevenFor(stopCand, newAvg, newTotalQty);
    };

    const priorUnrealizedProfit = validPrice
      ? Math.max(
          0,
          (isLong ? persistedAddPrice - persistedAvgEntry : persistedAvgEntry - persistedAddPrice) *
            oldQty *
            contractMult *
            effectiveFx,
        )
      : 0;
    const netProfitCushionRaw = Math.max(0, priorUnrealizedProfit + partialRealized);
    const allowedGivebackCeilingRaw = netProfitCushionRaw * effectiveCushionRatio;
    const addDenom = validQty ? addQtyNum * contractMult * effectiveFx : 0;

    const satisfiesCushionCeiling = (stopCand: number): boolean => {
      if (!validPrice || !validQty || !(addDenom > 0)) return true;
      const effStop = roundPython6(stopCand);
      if (!(effStop > 0)) return false;
      const addedTrancheRiskRaw =
        Math.max(0, isLong ? persistedAddPrice - effStop : effStop - persistedAddPrice) * addDenom;
      return addedTrancheRiskRaw <= allowedGivebackCeilingRaw + 1e-9;
    };

    const findMinimalStopForPredicate = (
      rawStart: number | null,
      predicate: (stopCand: number) => boolean,
    ): number | null => {
      if (rawStart == null) return null;
      let cand = roundPython6(rawStart);
      if (!(cand > 0)) return null;
      const stepMoreProtective = isLong ? 1e-6 : -1e-6;
      const stepLessProtective = isLong ? -1e-6 : 1e-6;
      if (!predicate(cand)) {
        for (let i = 0; i < 4; i += 1) {
          const nextStop = roundPython6(cand + stepMoreProtective);
          if (!(nextStop > 0)) return null;
          cand = nextStop;
          if (predicate(cand)) break;
        }
        if (!predicate(cand)) return null;
      } else {
        for (let i = 0; i < 4; i += 1) {
          const relaxed = roundPython6(cand + stepLessProtective);
          if (relaxed > 0 && predicate(relaxed)) {
            cand = relaxed;
          } else {
            break;
          }
        }
      }
      return cand;
    };

    let rawCampaignBe = newAvg;
    let coversPriorLoss = false;
    if (newAvg != null && newTotalQty != null && newTotalQty > 0 && hasPriorRealizedLoss) {
      const denom = newTotalQty * contractMult * effectiveFx;
      if (denom > 0) {
        const offset = Math.max(0, (Math.abs(partialRealized) - 1e-6) / denom);
        rawCampaignBe = isLong ? newAvg + offset : newAvg - offset;
        coversPriorLoss = true;
      }
    }

    let rawCushionBound: number | null = null;
    if (validPrice && validQty && addDenom > 0) {
      const maxAddDist = (allowedGivebackCeilingRaw + 1e-9) / addDenom;
      rawCushionBound = isLong ? persistedAddPrice - maxAddDist : persistedAddPrice + maxAddDist;
    }

    let rawCombinedBound = rawCampaignBe;
    if (rawCombinedBound != null && rawCushionBound != null) {
      if (isLong ? rawCushionBound > rawCombinedBound : rawCushionBound < rawCombinedBound) {
        rawCombinedBound = rawCushionBound;
      }
    }

    const breakevenOnlyStop = findMinimalStopForPredicate(rawCampaignBe, satisfiesBackendBreakeven);
    const protectiveBreakevenStop = findMinimalStopForPredicate(
      rawCombinedBound,
      (stopCand) => satisfiesBackendBreakeven(stopCand) && satisfiesCushionCeiling(stopCand),
    );
    const isCushionTightened =
      breakevenOnlyStop != null &&
      protectiveBreakevenStop != null &&
      (isLong ? protectiveBreakevenStop > breakevenOnlyStop : protectiveBreakevenStop < breakevenOnlyStop);

    const isBreakevenProtected =
      effectiveStop != null && satisfiesBackendBreakeven(effectiveStop);
    const isCushionProtected =
      effectiveStop != null && satisfiesCushionCeiling(effectiveStop);

    let oldAvgBePrice = persistedAvgEntry;
    if (hasPriorRealizedLoss && oldQty > 0) {
      const denomOld = oldQty * contractMult * effectiveFx;
      if (denomOld > 0) {
        const offsetOld = Math.max(0, (Math.abs(partialRealized) - 1e-6) / denomOld);
        oldAvgBePrice = isLong ? persistedAvgEntry + offsetOld : persistedAvgEntry - offsetOld;
      }
    }
    const oldProtectiveBeStop = findMinimalStopForPredicate(oldAvgBePrice, (stopCand) =>
      satisfiesBreakevenFor(stopCand, persistedAvgEntry, oldQty),
    );
    const oldStopViolatesCushion =
      oldProtectiveBeStop != null &&
      satisfiesBackendBreakeven(oldProtectiveBeStop) &&
      !satisfiesCushionCeiling(oldProtectiveBeStop);

    return {
      oldQty,
      newTotalQty,
      newAvg,
      rawCampaignBe,
      coversPriorLoss,
      breakevenOnlyStop,
      protectiveBreakevenStop,
      isCushionTightened,
      oldProtectiveBeStop,
      oldStopViolatesCushion,
      effectiveStop,
      isStopTightened,
      isBreakevenProtected,
      isCushionProtected,
    };
  }, [
    position,
    persistedAvgEntry,
    addQtyNum,
    persistedAddPrice,
    qtySpec.fractional,
    isDailyRegime,
    persistedNewDailyStop,
    persistedNewStop,
    persistedActiveOldStop,
    isLong,
    hasPriorRealizedLoss,
    partialRealized,
    contractMult,
    effectiveFx,
    effectiveCushionRatio,
  ]);

  const breakevenLockError = useMemo(() => {
    if (!preview || preview.newAvg == null) return null;
    const stopFieldLabel = isDailyRegime ? "有效日线主升防线" : "有效 30m 止损";
    const hasStrictValidAutoStop =
      preview.protectiveBreakevenStop != null &&
      (persistedAddPrice === undefined ||
        (isLong
          ? persistedAddPrice > preview.protectiveBreakevenStop
          : persistedAddPrice < preview.protectiveBreakevenStop));
    const cushionPctLabel = `${Math.round(effectiveCushionRatio * 10000) / 100}%`;
    const beTargetLabel = preview.isCushionTightened
      ? `保本与浮盈垫(${cushionPctLabel})上限止损位 ${fmtStopPrice(preview.protectiveBreakevenStop)}`
      : preview.coversPriorLoss
        ? `战役级保本线 ${fmtStopPrice(preview.protectiveBreakevenStop)}（含弥补既有减仓亏损 ${fmtMoney(partialRealized, acctCcy)}）`
        : `综合成本保本位 ${fmtStopPrice(preview.protectiveBreakevenStop)}`;

    if (effectiveRequireBreakeven) {
      if (preview.effectiveStop == null) {
        return `${isDailyRegime ? "日线主升模式" : "零新增本金风险推仓"}要求锁定保本：请填入覆及 ${beTargetLabel} 的${stopFieldLabel}`;
      }
      if (!preview.isBreakevenProtected) {
        if (!hasStrictValidAutoStop) {
          return `${isDailyRegime ? "日线主升防线保本底线" : "零新增本金风险推仓"}校验未通过：当前加仓价与既有损益下不存在严格位于加仓价保护侧的有效保本止损位，请缩减加仓数量或等待更高浮盈垫。`;
        }
        return `${isDailyRegime ? "日线主升防线保本底线" : "零新增本金风险推仓"}校验未通过：${stopFieldLabel} (${fmtStopPrice(preview.effectiveStop)}) 未覆盖 ${beTargetLabel}，请点击「自动填入保本止损位」锁死本金。`;
      }
    }

    if (preview.isBreakevenProtected && !preview.isCushionProtected) {
      if (!hasStrictValidAutoStop) {
        return `浮盈垫回吐上限（${cushionPctLabel}）校验未通过：当前加仓数量与净浮盈垫下，不存在严格位于加仓价保护侧的有效止损位，请缩减加仓数量或提高浮盈垫后再推仓。`;
      }
      return `浮盈垫回吐上限（${cushionPctLabel}）校验未通过：${stopFieldLabel} (${fmtStopPrice(preview.effectiveStop)}) 使加仓腿回吐超出净浮盈垫上限，需至少收紧至 ${fmtStopPrice(preview.protectiveBreakevenStop)}（可点击「自动填入保本止损位」）。`;
    }
    return null;
  }, [effectiveRequireBreakeven, effectiveCushionRatio, preview, isDailyRegime, persistedAddPrice, isLong, partialRealized, acctCcy]);

  const applyActiveBreakevenStop = (val: number) => {
    resetIdempotencyKey();
    const formatted = fmtPlanNum(val, 6);
    if (isDailyRegime) {
      setNewDailyStop(formatted);
    } else {
      setNewStopLoss(formatted);
    }
  };

  const canApplyOldAvgStop =
    preview?.oldProtectiveBeStop != null &&
    !preview.oldStopViolatesCushion &&
    (persistedActiveOldStop == null ||
      (isLong ? preview.oldProtectiveBeStop > persistedActiveOldStop : preview.oldProtectiveBeStop < persistedActiveOldStop)) &&
    (persistedAddPrice === undefined ||
      (isLong ? persistedAddPrice > preview.oldProtectiveBeStop : persistedAddPrice < preview.oldProtectiveBeStop));

  const canApplyNewAvgStop =
    preview?.protectiveBreakevenStop != null &&
    (persistedActiveOldStop == null ||
      (isLong ? preview.protectiveBreakevenStop >= persistedActiveOldStop : preview.protectiveBreakevenStop <= persistedActiveOldStop)) &&
    (persistedAddPrice === undefined ||
      (isLong ? persistedAddPrice > preview.protectiveBreakevenStop : persistedAddPrice < preview.protectiveBreakevenStop));

  const canSubmit =
    !busy &&
    position != null &&
    !isDisciplineBlocked &&
    !qtyValidationError &&
    !stopValidationError &&
    !breakevenLockError &&
    !fxValidationError &&
    addQtyNum !== undefined &&
    addQtyNum > 0 &&
    persistedAddPrice !== undefined &&
    persistedAddPrice > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!position) return;
    if (isDisciplineBlocked) {
      setErr("纪律禁止浮亏或平价摊平：仅允许在严格浮盈确认后执行 T2/T3 加仓");
      return;
    }
    if (qtyValidationError) {
      setErr(qtyValidationError);
      return;
    }
    if (stopValidationError) {
      setErr(stopValidationError);
      return;
    }
    if (breakevenLockError) {
      setErr(breakevenLockError);
      return;
    }
    if (fxValidationError) {
      setErr(fxValidationError);
      return;
    }
    if (addQtyNum === undefined || addQtyNum <= 0 || addPriceNum === undefined || addPriceNum <= 0) {
      setErr("请填写大于 0 的加仓数量与加仓成交价");
      return;
    }

    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = createScaleInIdempotencyKey(position.position_id);
    }
    const idemKey = idempotencyKeyRef.current;

    setBusy(true);
    setErr(null);
    try {
      const payload: ScaleInBody = {
        add_quantity: addQtyNum,
        add_price: addPriceNum,
        idempotency_key: idemKey,
        require_breakeven_lock: effectiveRequireBreakeven,
      };
      if (effectiveRequireBreakeven && cushionRatioNum !== undefined && cushionRatioNum > 0 && cushionRatioNum <= 1) {
        payload.max_profit_cushion_ratio = cushionRatioNum;
      }
      if (newStopNum !== undefined) {
        payload.new_stop_loss = newStopNum;
      }
      if (newDailyStopNum !== undefined && newDailyStopNum > 0) {
        payload.new_daily_stop = newDailyStopNum;
      }
      if (fxVal !== undefined) {
        payload.fx_rate_to_base = fxVal;
      }
      if (note.trim()) {
        payload.note = note.trim();
      }
      const res = await api.scaleInPosition(position.position_id, payload);
      resetIdempotencyKey();
      onDone(res);
      onClose();
    } catch (ex) {
      if (ex instanceof ApiError && ex.status >= 400 && ex.status < 500) {
        resetIdempotencyKey();
      }
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  const fullMatches =
    quickQtyFull != null && addQtyNum !== undefined && Math.abs(addQtyNum - quickQtyFull) < 1e-6;
  const halfMatches =
    quickQtyHalf != null && addQtyNum !== undefined && Math.abs(addQtyNum - quickQtyHalf) < 1e-6;

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`浮盈顺势加仓 (${nextTrancheTag}) · ${formatSymbolWithCode(position?.symbol)}`}
      subtitle="仅允许在严格浮盈确认后分批加仓（T2/T3），严禁浮亏或平价摊平"
    >
      <form onSubmit={submit} className="flex max-h-[78dvh] flex-col gap-3.5 overflow-y-auto p-5">
        {/* Top Position Status Summary */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <DirectionBadge direction={direction} />
            <span className="rounded border border-hairline-soft bg-sunken px-2 py-0.5 font-mono text-[11px] text-ink-2">
              当前第 {currentTrancheCount} 批 → 拟加仓 {nextTrancheTag}
            </span>
            <span
              className={`rounded border px-2 py-0.5 text-[11px] font-medium ${
                isDailyRegime
                  ? "border-bull/45 bg-bull/10 text-bull"
                  : "border-hairline-soft bg-sunken text-ink-2"
              }`}
            >
              {isDailyRegime ? "日线主升防线主控" : "30m 战术防线主控"}
            </span>
            {position?.sector_theme ? (
              <span className="rounded border border-hairline-soft bg-sunken px-2 py-0.5 text-[11px] text-muted">
                {position.sector_theme}
              </span>
            ) : null}
          </div>
          {currentPnlPct !== null ? (
            <StatusBadge
              tone={currentPnlPct > 0 ? "good" : "critical"}
              label={
                currentPnlPct > 0
                  ? `当前浮盈 ${fmtPct(currentPnlPct, 2, true)}`
                  : currentPnlPct < 0
                    ? `当前浮亏 ${fmtPct(currentPnlPct, 2, true)}`
                    : "当前平价 (0.00%)"
              }
            />
          ) : (
            <StatusBadge tone="neutral" label="暂无实时参考价" />
          )}
        </div>

        <div className="metric-strip grid-cols-2 sm:grid-cols-4">
          <div className="metric-cell">
            <span className="metric-label">当前持仓均价</span>
            <span className="metric-val">{fmtPrice(avgEntry)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">当前持仓数量</span>
            <span className="metric-val">{position ? fmtNum(position.quantity, 6) : "—"}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">{isDailyRegime ? "当前日线主升防线" : "当前 30m 止损"}</span>
            <span className="metric-val text-bear">{fmtPrice(activeOldStop)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">参考最新价</span>
            <span className={`metric-val ${signedToneClass(currentPnlPct)}`}>{fmtPrice(currentPrice)}</span>
          </div>
        </div>

        {/* Hard Discipline Block Banner */}
        {isDisciplineBlocked ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-critical/55 bg-critical/10 px-3 py-2.5 text-xs text-ink"
          >
            <AlertOctagon className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
            <div className="min-w-0 flex-1 space-y-0.5">
              <div className="font-semibold text-critical">
                纪律禁止浮亏或平价摊平：仅允许在严格浮盈确认后执行 T2/T3 加仓
              </div>
              <div className="text-[11px] text-ink-2">
                {isAddPriceUnprofitable
                  ? `${isLong ? "多单加仓成交价必须严格高于" : "空单加仓成交价必须严格低于"}当前持仓均价 ${fmtPrice(avgEntry)}。`
                  : `当前参考市价 (${fmtPrice(currentPrice)}) 尚未进入严格浮盈区，下方测算仅作触发价预演；若最新市价已转盈请先刷新哨兵行情。`}
              </div>
            </div>
          </div>
        ) : null}

        {/* Quantity + Price Inputs */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>
              加仓数量 *
              {qtySpec.minOrderQty > 1 || qtySpec.lotStep > 1
                ? `（最小 ${fmtNum(qtySpec.minOrderQty, 6)} · 步进 ${fmtNum(qtySpec.lotStep, 6)}）`
                : ""}
            </span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step={qtySpec.fractional ? "any" : qtySpec.lotStep}
              min={qtySpec.minOrderQty}
              required
              autoFocus
              value={addQty}
              onChange={(e) => {
                resetIdempotencyKey();
                setAddQty(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>加仓成交价 *</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              min="0.000001"
              required
              value={addPrice}
              onChange={(e) => {
                resetIdempotencyKey();
                setAddPrice(e.target.value);
              }}
            />
          </label>
        </div>

        {/* Quick T2 / T3 Quantity Helper Chips */}
        {position ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted">快捷分批：</span>
            <button
              type="button"
              className="chip py-0.5 text-[11px]"
              disabled={quickQtyFull == null}
              aria-pressed={fullMatches}
              title={
                quickQtyFull == null
                  ? `当前持仓等量 (${fmtNum(position.quantity, 6)}) 不满足最小下单单位 ${fmtNum(qtySpec.minOrderQty, 6)} 或步进 ${fmtNum(qtySpec.lotStep, 6)}`
                  : undefined
              }
              onClick={() => {
                if (quickQtyFull == null) return;
                resetIdempotencyKey();
                setAddQty(fmtPlanNum(quickQtyFull, 6));
              }}
            >
              {quickQtyFull != null
                ? `等量加仓 (+100% · T1→T2 · ${fmtNum(quickQtyFull, 6)})`
                : `等量加仓 (+100% · 低于最小单位 ${fmtNum(qtySpec.minOrderQty, 6)})`}
            </button>
            <button
              type="button"
              className="chip py-0.5 text-[11px]"
              disabled={quickQtyHalf == null}
              aria-pressed={halfMatches}
              title={
                quickQtyHalf == null
                  ? `半量 (${fmtNum(position.quantity * 0.5, 6)}) 小于最小下单单位 ${fmtNum(qtySpec.minOrderQty, 6)} 或不符合步进 ${fmtNum(qtySpec.lotStep, 6)}`
                  : undefined
              }
              onClick={() => {
                if (quickQtyHalf == null) return;
                resetIdempotencyKey();
                setAddQty(fmtPlanNum(quickQtyHalf, 6));
              }}
            >
              {quickQtyHalf != null
                ? `半量加仓 (+50% · T3 · ${fmtNum(quickQtyHalf, 6)})`
                : `半量加仓 (+50% · 不足最小单位 ${fmtNum(qtySpec.minOrderQty, 6)})`}
            </button>
          </div>
        ) : null}

        {/* Zero-Principal-Risk Pyramid Lock Toggle */}
        <div className="flex flex-col gap-2 rounded-md border border-bull/40 bg-bull/8 p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="inline-flex cursor-pointer items-center gap-2 font-semibold text-ink">
              <input
                type="checkbox"
                className="accent-accent"
                checked={effectiveRequireBreakeven}
                disabled={isDailyRegime}
                onChange={(e) => {
                  resetIdempotencyKey();
                  setRequireBreakevenLock(e.target.checked);
                }}
              />
              <span>
                零新增本金风险推仓（强制战役级保本锁死
                {isDailyRegime ? " · 日线主升模式强制启用" : ""}）
              </span>
            </label>
            {canApplyNewAvgStop && preview?.protectiveBreakevenStop != null ? (
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => applyActiveBreakevenStop(preview.protectiveBreakevenStop!)}
              >
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                <span>
                  自动填入保本止损位 ({fmtStopPrice(preview.protectiveBreakevenStop)}
                  {isDailyRegime ? " → 日线防线" : " → 30m 止损"})
                </span>
              </button>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-2">
            <span>
              开启后，后端强制校验推仓后战役本金风险为 0（{isDailyRegime ? "日线主升防线" : "30m 止损"}必须覆及
              {preview?.isCushionTightened
                ? "保本与浮盈垫上限位 "
                : preview?.coversPriorLoss
                  ? "战役保本线 "
                  : "综合均价 "}
              <strong className="font-mono text-ink">
                {fmtStopPrice(
                  preview?.isCushionTightened || preview?.coversPriorLoss
                    ? preview.protectiveBreakevenStop
                    : preview?.newAvg,
                )}
              </strong>
              {preview?.coversPriorLoss
                ? `，已计入既往已实现减仓亏损 ${fmtMoney(partialRealized, acctCcy)}`
                : ""}
              ）；在零本金风险锁死下，加仓腿止损回吐严格封顶于「净浮盈垫 × 该比例」（严格硬上限，绝不会被板块风险预算绕过）。
            </span>
            {effectiveRequireBreakeven ? (
              <label className="inline-flex items-center gap-1.5 whitespace-nowrap">
                <span className="text-muted">浮盈垫最大允许回吐比例（严格硬上限）</span>
                <input
                  className="input h-6 w-16 px-1.5 font-mono text-[11px]"
                  type="number"
                  step="0.05"
                  min="0.1"
                  max="1"
                  aria-label="浮盈垫最大允许回吐比例（严格硬上限）"
                  value={maxCushionRatio}
                  onChange={(e) => {
                    resetIdempotencyKey();
                    setMaxCushionRatio(e.target.value);
                  }}
                />
              </label>
            ) : null}
          </div>
        </div>

        {/* Optional Synchronized Stop-Loss Tightening */}
        <div className="flex flex-col gap-1.5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {isDailyRegime ? (
              <>
                <label className="field">
                  <span>同步收紧日线主升防线 (new_daily_stop) *</span>
                  <input
                    className="input font-mono"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    placeholder={dailyStop != null ? `当前日线 ${fmtStopPrice(dailyStop)}` : "输入新日线防线"}
                    value={newDailyStop}
                    onChange={(e) => {
                      resetIdempotencyKey();
                      setNewDailyStop(e.target.value);
                    }}
                  />
                </label>
                <label className="field">
                  <span>30m 战术参考止损（主升期非主控 · 可选）</span>
                  <input
                    className="input font-mono"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    placeholder={currentStop != null ? `参考 30m ${fmtStopPrice(currentStop)}（不改留空）` : "不改留空"}
                    value={newStopLoss}
                    onChange={(e) => {
                      resetIdempotencyKey();
                      setNewStopLoss(e.target.value);
                    }}
                  />
                </label>
              </>
            ) : (
              <>
                <label className="field">
                  <span>
                    同步收紧 30m 止损位{requireBreakevenLock ? " *" : "（可选 · 只紧不松）"}
                  </span>
                  <input
                    className="input font-mono"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    placeholder={currentStop != null ? `当前 ${fmtStopPrice(currentStop)}` : "输入新 30m 止损"}
                    value={newStopLoss}
                    onChange={(e) => {
                      resetIdempotencyKey();
                      setNewStopLoss(e.target.value);
                    }}
                  />
                </label>
                <label className="field">
                  <span>同步上移日线结构防线（new_daily_stop，可选）</span>
                  <input
                    className="input font-mono"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    placeholder={dailyStop != null ? `当前日线 ${fmtStopPrice(dailyStop)}` : "不改留空"}
                    value={newDailyStop}
                    onChange={(e) => {
                      resetIdempotencyKey();
                      setNewDailyStop(e.target.value);
                    }}
                  />
                </label>
              </>
            )}
          </div>
          {canApplyOldAvgStop || canApplyNewAvgStop ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted">
                快捷保本止损（填入{isDailyRegime ? "日线主升防线" : " 30m 止损"}）：
              </span>
              {canApplyOldAvgStop && preview?.oldProtectiveBeStop != null ? (
                <button
                  type="button"
                  className="chip py-0.5 font-mono text-[11px]"
                  onClick={() => applyActiveBreakevenStop(preview.oldProtectiveBeStop!)}
                >
                  {hasPriorRealizedLoss ? "原仓位战役保本" : "原均价保本"} ({fmtStopPrice(preview.oldProtectiveBeStop)})
                </button>
              ) : null}
              {canApplyNewAvgStop && preview?.protectiveBreakevenStop != null ? (
                <button
                  type="button"
                  className="chip py-0.5 font-mono text-[11px]"
                  onClick={() => applyActiveBreakevenStop(preview.protectiveBreakevenStop!)}
                >
                  {preview.isCushionTightened
                    ? "保本与浮盈垫上限"
                    : preview.coversPriorLoss
                      ? "推仓后战役保本"
                      : "综合均价保本"}{" "}
                  ({fmtStopPrice(preview.protectiveBreakevenStop)})
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* Cross-Currency FX Banner */}
        {needsCrossFx && quoteCcy ? (
          <CrossCurrencyFxBanner
            symbol={position?.symbol}
            quoteCurrency={quoteCcy}
            accountCurrency={acctCcy}
            value={fxRate}
            onChange={(val) => {
              resetIdempotencyKey();
              setFxRate(val);
            }}
            required
            enabled={open}
          />
        ) : null}

        {/* Optional Note */}
        <label className="field">
          <span>加仓备注（可选）</span>
          <input
            className="input"
            placeholder={`如：30m 三买确认浮盈加仓 ${nextTrancheTag}`}
            value={note}
            onChange={(e) => {
              resetIdempotencyKey();
              setNote(e.target.value);
            }}
          />
        </label>

        {/* Live Post-Scale-In Preview */}
        {preview ? (
          <div className="rounded-md border border-hairline bg-sunken p-3">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <span className="section-title">实时加仓后测算预览</span>
              {preview.isBreakevenProtected ? (
                <span className="inline-flex items-center gap-1 text-[11px] font-medium text-bull">
                  <ShieldCheck className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <span>
                    {preview.coversPriorLoss
                      ? "新止损已覆及战役保本线（含弥补既往减仓亏损）"
                      : "新止损已进入保本/浮盈保护区"}
                  </span>
                </span>
              ) : null}
            </div>
            <div className="grid grid-cols-1 gap-2.5 text-xs sm:grid-cols-3">
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted">新总仓位</span>
                <div className="tnum flex items-center gap-1 font-mono font-semibold text-ink">
                  <span className="text-muted">{fmtNum(preview.oldQty, 6)}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                  <span>{preview.newTotalQty != null ? fmtNum(preview.newTotalQty, 6) : "—"}</span>
                </div>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted">
                  {preview.coversPriorLoss ? "新加权均价 / 战役保本线" : "新加权均价"}
                </span>
                <div className="tnum flex items-center gap-1 font-mono font-semibold text-ink">
                  <span className="text-muted">{fmtPrice(avgEntry)}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                  <span>{preview.newAvg != null ? fmtPrice(preview.newAvg) : "—"}</span>
                  {preview.coversPriorLoss && preview.protectiveBreakevenStop != null ? (
                    <span className="text-[11px] text-warning">
                      (保本 {fmtPrice(preview.protectiveBreakevenStop)})
                    </span>
                  ) : null}
                </div>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted">
                  {isDailyRegime ? "日线主升防线变化" : "30m 止损位变化"}
                </span>
                <div className="tnum flex items-center gap-1 font-mono font-semibold text-ink">
                  <span className="text-muted">{fmtPrice(activeOldStop)}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                  <span className={preview.isStopTightened ? "text-bull" : "text-ink"}>
                    {fmtPrice(preview.effectiveStop)}
                  </span>
                </div>
              </div>
            </div>
            {preview.coversPriorLoss ? (
              <div className="mt-2 border-t border-hairline-soft pt-1.5 text-[11px] text-ink-2">
                既往已实现减仓亏损{" "}
                <strong className="font-mono text-bear">{fmtMoney(partialRealized, acctCcy)}</strong>{" "}
                已折算进战役级保本止损位{" "}
                <strong className="font-mono text-ink">{fmtPrice(preview.protectiveBreakevenStop)}</strong>
                （综合均价 {fmtPrice(preview.newAvg)}）。
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Inline Validation / Backend Risk Error */}
        {qtyValidationError || stopValidationError || breakevenLockError || fxValidationError || err ? (
          <div
            role="alert"
            className="break-words rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink"
          >
            {qtyValidationError || stopValidationError || breakevenLockError || fxValidationError || err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {busy ? "正在提交加仓…" : `确认浮盈加仓 (${nextTrancheTag})`}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
