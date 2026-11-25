import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";
import { api } from "../../api/client";
import type { FxReferenceResponse } from "../../api/types";
import { fmtPlanNum } from "../../lib/format";

export interface CrossCurrencyFxBannerProps {
  symbol?: string | null;
  quoteCurrency: string;
  accountCurrency: string;
  value: string;
  onChange: (nextValue: string) => void;
  required?: boolean;
  enabled?: boolean;
}

export function CrossCurrencyFxBanner({
  symbol,
  quoteCurrency,
  accountCurrency,
  value,
  onChange,
  required = false,
  enabled = true,
}: CrossCurrencyFxBannerProps) {
  const inputId = useId();
  const hintId = useId();
  const qCcy = quoteCurrency.trim().toUpperCase();
  const aCcy = accountCurrency.trim().toUpperCase();
  const cleanSym = (symbol || "").trim().toUpperCase();

  const [fxRef, setFxRef] = useState<FxReferenceResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchRef = useCallback(
    async (refresh = false, signal?: AbortSignal) => {
      if (!enabled || !qCcy || !aCcy || qCcy === aCcy) return;
      setLoading(true);
      try {
        const res = await api.fxReference({ quote: qCcy, account: aCcy, refresh }, signal);
        if (!signal?.aborted) {
          setFxRef(res);
        }
      } catch {
        if (!signal?.aborted) {
          setFxRef(null);
        }
      } finally {
        if (!signal?.aborted) {
          setLoading(false);
        }
      }
    },
    [enabled, qCcy, aCcy],
  );

  useEffect(() => {
    if (!enabled || !qCcy || !aCcy || qCcy === aCcy) {
      setFxRef(null);
      return;
    }
    const ctrl = new AbortController();
    void fetchRef(false, ctrl.signal);
    return () => ctrl.abort();
  }, [enabled, qCcy, aCcy, fetchRef]);

  const refRateStr =
    fxRef?.available && fxRef.fx_to_account != null && fxRef.fx_to_account > 0
      ? fmtPlanNum(fxRef.fx_to_account, 6)
      : "";

  const usdCnyStr =
    fxRef?.usd_cny != null && fxRef.usd_cny > 0 ? fmtPlanNum(fxRef.usd_cny, 4) : "";
  const cnyUsdStr =
    fxRef?.cny_usd != null && fxRef.cny_usd > 0 ? fmtPlanNum(fxRef.cny_usd, 6) : "";

  const isUsdCnyPair =
    (qCcy === "CNY" && aCcy === "USD") || (qCcy === "USD" && aCcy === "CNY");

  const currentNum = Number(value);
  const refNum = Number(refRateStr);
  const appliedMatchesRef =
    Boolean(refRateStr) &&
    value.trim() !== "" &&
    Number.isFinite(currentNum) &&
    Number.isFinite(refNum) &&
    Math.abs(currentNum - refNum) < 1e-6;

  return (
    <div className="flex flex-wrap items-center justify-between gap-2.5 rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-ink">
      <div className="flex flex-col gap-0.5">
        <span>
          <strong>跨币种必填：</strong>
          {cleanSym ? <span className="font-mono">{cleanSym} </span> : null}
          报价币 <strong>{qCcy}</strong> → 账户币 <strong>{aCcy}</strong>
        </span>

        <span id={hintId} className="font-mono text-[11px] text-ink-2">
          {loading && !fxRef ? (
            "正在获取每日美元/人民币参考汇率…"
          ) : fxRef?.available && refRateStr ? (
            isUsdCnyPair && usdCnyStr && cnyUsdStr ? (
              <>
                每日参考{fxRef.as_of_date ? ` (${fxRef.as_of_date})` : ""}：1 USD ={" "}
                <strong className="text-ink">{usdCnyStr}</strong> CNY · 1 CNY ={" "}
                <strong className="text-ink">{cnyUsdStr}</strong> USD
              </>
            ) : (
              <>
                每日参考{fxRef.as_of_date ? ` (${fxRef.as_of_date})` : ""}：1 {qCcy} ={" "}
                <strong className="text-ink">{refRateStr}</strong> {aCcy}
                {usdCnyStr && cnyUsdStr
                  ? ` ｜ 美金/人民币：1 USD = ${usdCnyStr} CNY (1 CNY = ${cnyUsdStr} USD)`
                  : ""}
              </>
            )
          ) : usdCnyStr && cnyUsdStr ? (
            <>
              每日美金/人民币参考{fxRef?.as_of_date ? ` (${fxRef.as_of_date})` : ""}：1 USD ={" "}
              <strong className="text-ink">{usdCnyStr}</strong> CNY · 1 CNY ={" "}
              <strong className="text-ink">{cnyUsdStr}</strong> USD
            </>
          ) : (
            "暂未获取到实时参考汇率，请手动填写结算汇率"
          )}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {refRateStr ? (
          <div className="inline-flex items-center gap-1">
            <button
              type="button"
              className="chip py-0.5 font-mono text-[11px]"
              aria-pressed={appliedMatchesRef}
              onClick={() => onChange(refRateStr)}
              title={`一键填入今日参考折算汇率：1 ${qCcy} = ${refRateStr} ${aCcy}`}
            >
              {appliedMatchesRef ? `已用参考 ${refRateStr}` : `填入参考 ${refRateStr}`}
            </button>
            <button
              type="button"
              className="chip px-1.5 py-0.5 text-[11px]"
              disabled={loading}
              onClick={() => {
                void fetchRef(true);
              }}
              title="刷新每日实时参考汇率"
              aria-label="刷新每日实时参考汇率"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            </button>
          </div>
        ) : null}

        <div className="inline-flex items-center gap-1.5">
          <label htmlFor={inputId} className="font-mono text-[11px] text-muted">
            1 {qCcy} =
          </label>
          <input
            id={inputId}
            name="fx_to_account"
            className="input h-7 w-28 font-mono"
            type="number"
            inputMode="decimal"
            step="any"
            min="0.000001"
            required={required}
            aria-describedby={hintId}
            placeholder={refRateStr ? `参考 ${refRateStr}` : `? ${aCcy}`}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
          <span className="font-mono text-[11px] text-muted">{aCcy}</span>
        </div>
      </div>
    </div>
  );
}
