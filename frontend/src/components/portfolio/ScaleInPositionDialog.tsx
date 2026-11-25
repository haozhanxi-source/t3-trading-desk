import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AlertOctagon, ArrowRight, ShieldCheck } from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type { Position, ScaleInBody, ScaleInResponse } from "../../api/types";
import {
  fmtInputPrice,
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
  const avgEntry = position ? (position.avg_entry_price ?? position.entry_price ?? null) : null;
  const currentStop = position?.tactical_stop_30m ?? null;
  const dailyStop = position?.structural_stop_daily ?? null;
  const currentTrancheCount =
    position && Array.isArray(position.tranches) && position.tranches.length > 0
      ? position.tranches.length
      : 1;
  const nextTrancheTag = `T${currentTrancheCount + 1}`;

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
    setFxRate(
      position.fx_to_account != null && position.fx_to_account > 0
        ? fmtPlanNum(position.fx_to_account, 6)
        : "",
    );
    setNote("");
    setErr(null);
  }, [open, position, currentPrice, currentTrancheCount, quickQtyFull, quickQtyHalf, qtySpec.minOrderQty]);

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
  const fxVal = optNum(fxRate);

  const isCurrentUnprofitable =
    currentPrice != null &&
    Number.isFinite(currentPrice) &&
    avgEntry != null &&
    (isLong ? currentPrice <= avgEntry : currentPrice >= avgEntry);

  const isAddPriceUnprofitable =
    addPriceNum !== undefined &&
    avgEntry != null &&
    (isLong ? addPriceNum <= avgEntry : addPriceNum >= avgEntry);

  const isDisciplineBlocked = isAddPriceUnprofitable || isCurrentUnprofitable;

  const stopValidationError = useMemo(() => {
    if (newStopLoss.trim() !== "") {
      if (newStopNum === undefined || newStopNum <= 0) {
        return "新 30m 止损位必须为大于 0 的有效数值";
      }
      if (currentStop != null) {
        if (isLong && newStopNum < currentStop) {
          return `移动止损只紧不松：多单新 30m 止损 (${fmtPrice(newStopNum)}) 不得低于原止损 (${fmtPrice(currentStop)})`;
        }
        if (!isLong && newStopNum > currentStop) {
          return `移动止损只紧不松：空单新 30m 止损 (${fmtPrice(newStopNum)}) 不得高于原止损 (${fmtPrice(currentStop)})`;
        }
      }
    }

    const effStop = newStopNum ?? currentStop;
    if (addPriceNum !== undefined && effStop != null) {
      if (isLong && addPriceNum <= effStop) {
        return `多单加仓成交价 (${fmtPrice(addPriceNum)}) 必须严格高于有效 30m 止损位 (${fmtPrice(effStop)})`;
      }
      if (!isLong && addPriceNum >= effStop) {
        return `空单加仓成交价 (${fmtPrice(addPriceNum)}) 必须严格低于有效 30m 止损位 (${fmtPrice(effStop)})`;
      }
    }

    if (addPriceNum !== undefined && dailyStop != null) {
      if (isLong && addPriceNum <= dailyStop) {
        return `多单加仓成交价 (${fmtPrice(addPriceNum)}) 必须严格高于日线结构止损 (${fmtPrice(dailyStop)})`;
      }
      if (!isLong && addPriceNum >= dailyStop) {
        return `空单加仓成交价 (${fmtPrice(addPriceNum)}) 必须严格低于日线结构止损 (${fmtPrice(dailyStop)})`;
      }
    }
    return null;
  }, [newStopLoss, newStopNum, currentStop, dailyStop, addPriceNum, isLong]);

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

  const preview = useMemo(() => {
    if (!position || avgEntry == null) return null;
    const oldQty = position.quantity;
    const validQty = addQtyNum !== undefined && addQtyNum > 0;
    const validPrice = addPriceNum !== undefined && addPriceNum > 0;
    const newTotalQty = validQty ? oldQty + addQtyNum : null;
    const newAvg =
      validQty && validPrice && newTotalQty != null && newTotalQty > 0
        ? (avgEntry * oldQty + addPriceNum * addQtyNum) / newTotalQty
        : null;
    const effectiveStop = newStopNum !== undefined && newStopNum > 0 ? newStopNum : currentStop;
    const isStopTightened =
      newStopNum !== undefined &&
      currentStop != null &&
      (isLong ? newStopNum > currentStop : newStopNum < currentStop);
    const isBreakevenProtected =
      effectiveStop != null &&
      newAvg != null &&
      (isLong ? effectiveStop >= newAvg : effectiveStop <= newAvg);
    return {
      oldQty,
      newTotalQty,
      newAvg,
      effectiveStop,
      isStopTightened,
      isBreakevenProtected,
    };
  }, [position, avgEntry, addQtyNum, addPriceNum, newStopNum, currentStop, isLong]);

  const canApplyOldAvgStop =
    avgEntry != null &&
    (currentStop == null || (isLong ? avgEntry > currentStop : avgEntry < currentStop)) &&
    (addPriceNum === undefined || (isLong ? addPriceNum > avgEntry : addPriceNum < avgEntry));

  const canApplyNewAvgStop =
    preview?.newAvg != null &&
    (avgEntry == null || Math.abs(preview.newAvg - avgEntry) > 1e-6) &&
    (currentStop == null || (isLong ? preview.newAvg > currentStop : preview.newAvg < currentStop)) &&
    (addPriceNum === undefined || (isLong ? addPriceNum > preview.newAvg : addPriceNum < preview.newAvg));

  const canSubmit =
    !busy &&
    position != null &&
    !isDisciplineBlocked &&
    !qtyValidationError &&
    !stopValidationError &&
    !fxValidationError &&
    addQtyNum !== undefined &&
    addQtyNum > 0 &&
    addPriceNum !== undefined &&
    addPriceNum > 0;

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
      };
      if (newStopNum !== undefined) {
        payload.new_stop_loss = newStopNum;
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
            <span className="metric-label">当前 30m 止损</span>
            <span className="metric-val text-bear">{fmtPrice(currentStop)}</span>
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

        {/* Optional Synchronized Stop-Loss Tightening */}
        <div className="flex flex-col gap-1.5">
          <label className="field">
            <span>同步收紧 30m 止损位（可选 · 只紧不松；上移至保本/浮盈区可自动释放风险预算）</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              placeholder={currentStop != null ? `不改留空（当前 ${fmtPrice(currentStop)}）` : "不改留空"}
              value={newStopLoss}
              onChange={(e) => {
                resetIdempotencyKey();
                setNewStopLoss(e.target.value);
              }}
            />
          </label>
          {canApplyOldAvgStop || canApplyNewAvgStop ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted">快捷保本止损：</span>
              {canApplyOldAvgStop && avgEntry != null ? (
                <button
                  type="button"
                  className="chip py-0.5 font-mono text-[11px]"
                  onClick={() => {
                    resetIdempotencyKey();
                    setNewStopLoss(fmtPlanNum(avgEntry, 6));
                  }}
                >
                  原均价保本 ({fmtPrice(avgEntry)})
                </button>
              ) : null}
              {canApplyNewAvgStop && preview?.newAvg != null ? (
                <button
                  type="button"
                  className="chip py-0.5 font-mono text-[11px]"
                  onClick={() => {
                    resetIdempotencyKey();
                    setNewStopLoss(fmtPlanNum(preview.newAvg, 6));
                  }}
                >
                  新均价保本 ({fmtPrice(preview.newAvg)})
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
                  <span>新止损已进入保本/浮盈保护区</span>
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
                <span className="text-[11px] text-muted">新加权均价</span>
                <div className="tnum flex items-center gap-1 font-mono font-semibold text-ink">
                  <span className="text-muted">{fmtPrice(avgEntry)}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                  <span>{preview.newAvg != null ? fmtPrice(preview.newAvg) : "—"}</span>
                </div>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted">30m 止损位变化</span>
                <div className="tnum flex items-center gap-1 font-mono font-semibold text-ink">
                  <span className="text-muted">{fmtPrice(currentStop)}</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted" aria-hidden="true" />
                  <span className={preview.isStopTightened ? "text-bull" : "text-ink"}>
                    {fmtPrice(preview.effectiveStop)}
                  </span>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {/* Inline Validation / Backend Risk Error */}
        {qtyValidationError || stopValidationError || fxValidationError || err ? (
          <div
            role="alert"
            className="break-words rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink"
          >
            {qtyValidationError || stopValidationError || fxValidationError || err}
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
