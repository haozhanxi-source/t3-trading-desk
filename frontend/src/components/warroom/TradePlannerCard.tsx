import { useEffect, useRef, useState, type FormEvent } from "react";
import { AlertTriangle, Calculator, GitBranch, Lock, PlusCircle, ShieldAlert } from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type { Direction, MarketDualResponse, TradePlanBody, TradePlanResponse } from "../../api/types";
import {
  fmtInputPrice,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtRatio,
  inferAssetCurrency,
  optNum,
} from "../../lib/format";
import type { OpenPositionSeed } from "../portfolio/PortfolioDialogs";
import { GateDecisionBadge, StatusBadge } from "../ui/StatusBadge";

interface TradePlannerCardProps {
  symbol: string;
  dualData: MarketDualResponse | null;
  intradayInterval: "15m" | "30m" | "60m";
  portfolioInitialized?: boolean;
  accountCurrency?: string | null;
  onOpenPositionSeed: (seed: OpenPositionSeed) => void;
}

export function TradePlannerCard({
  symbol,
  dualData,
  intradayInterval,
  portfolioInitialized,
  accountCurrency,
  onOpenPositionSeed,
}: TradePlannerCardProps) {
  const align = dualData?.chanlun?.dual_alignment;
  const dailyZs = dualData?.chanlun?.daily?.latest_zhongshu;
  const lastClose =
    dualData?.ohlcv_intraday?.[dualData.ohlcv_intraday.length - 1]?.close ??
    dualData?.ohlcv_daily?.[dualData.ohlcv_daily.length - 1]?.close ??
    null;

  const snapInterval = dualData?.intraday_interval || align?.intraday_timeframe || intradayInterval;
  const is30mSnapshot = snapInterval === "30m" || snapInterval === "30min";

  const [direction, setDirection] = useState<Direction>("long");
  const [live, setLive] = useState(true);
  const [entryPrice, setEntryPrice] = useState("");
  const [stop30m, setStop30m] = useState("");
  const [stopDaily, setStopDaily] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [riskPct, setRiskPct] = useState("1.0");
  const [slotShare, setSlotShare] = useState("1.0");
  const [horizonDays, setHorizonDays] = useState("90");
  const [fxToAccount, setFxToAccount] = useState("");

  /* Uninitialized ledger & explicit simulation equity controls */
  const [ledgerMissingDetected, setLedgerMissingDetected] = useState(false);
  const ledgerUninitialized = portfolioInitialized === false || ledgerMissingDetected;
  const [useSimEquity, setUseSimEquity] = useState(portfolioInitialized === false);
  const [simEquity, setSimEquity] = useState("1000000");
  const [simCurrency, setSimCurrency] = useState("USD");

  /* Gate inputs (pair_leg_verified defaults strictly to false; never faked from pairTrade) */
  const [hasL1L2, setHasL1L2] = useState(true);
  const [pendingVerif, setPendingVerif] = useState("0");
  const [thesisFalsified, setThesisFalsified] = useState(false);
  const [pairTrade, setPairTrade] = useState(false);
  const [pairOppositeSym, setPairOppositeSym] = useState("");
  const [pairLegVerified, setPairLegVerified] = useState(false);
  const [hedgeOnly, setHedgeOnly] = useState(false);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<TradePlanResponse | null>(null);
  const [planStale, setPlanStale] = useState(false);

  const planReqIdRef = useRef(0);
  const planInputRevRef = useRef(0);
  const planAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      planAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (portfolioInitialized === false) {
      setUseSimEquity(true);
    } else if (portfolioInitialized === true) {
      setLedgerMissingDetected(false);
    }
  }, [portfolioInitialized]);

  const invalidateInFlightAndPlan = () => {
    planInputRevRef.current += 1;
    if (planAbortRef.current) {
      planAbortRef.current.abort();
      planAbortRef.current = null;
    }
    setBusy(false);
  };

  const markStale = () => {
    invalidateInFlightAndPlan();
    setPlanStale(true);
  };

  /* Auto-seed direction & stops when symbol or snapshot changes */
  useEffect(() => {
    invalidateInFlightAndPlan();
    setPlan(null);
    setPlanStale(false);
    setErr(null);
    setFxToAccount("");
    setPairTrade(false);
    setPairOppositeSym("");
    setPairLegVerified(false);

    const bias = (align?.daily_bias || "").toLowerCase();
    const nextDir: Direction =
      bias.includes("short") || bias.includes("bear") || bias.includes("down") || bias.includes("空")
        ? "short"
        : "long";
    setDirection(nextDir);

    const ep = lastClose;
    setEntryPrice(fmtInputPrice(ep));

    /* STRICT 30m discipline: NEVER prefill 15m/60m or daily stops into tactical_stop_30m */
    const s30 = is30mSnapshot ? (align?.intraday_tactical_stop ?? null) : null;
    setStop30m(fmtInputPrice(s30));

    const sDaily =
      align?.daily_structural_stop ?? (nextDir === "long" ? dailyZs?.zd : dailyZs?.zg) ?? null;
    setStopDaily(fmtInputPrice(sDaily));

    /* Default 3R illustrative target formatted cleanly without 16-digit float tails */
    if (ep != null && s30 != null && ep !== s30) {
      const dist = Math.abs(ep - s30);
      const tgt = nextDir === "long" ? ep + dist * 3 : Math.max(0.0001, ep - dist * 3);
      setTargetPrice(fmtInputPrice(tgt));
    } else {
      setTargetPrice("");
    }
  }, [
    symbol,
    is30mSnapshot,
    align?.daily_bias,
    align?.intraday_tactical_stop,
    align?.daily_structural_stop,
    dailyZs?.zd,
    dailyZs?.zg,
    lastClose,
  ]);

  const effectiveAcctCurrency = (useSimEquity ? simCurrency : accountCurrency || "USD").toUpperCase();
  const staticQuoteCurrency = inferAssetCurrency(symbol, dualData?.asset_class);
  const inferredQuoteCurrency =
    plan?.position_plan?.quote_currency?.toUpperCase() ?? staticQuoteCurrency;
  const isKnownSameCurrency = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency === effectiveAcctCurrency,
  );
  const needsCrossCurrencyFx = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency !== effectiveAcctCurrency,
  );

  /* Clear stale FX rate AND invalidate any in-flight/cached plan ONLY when account currency or symbol/asset quote changes (never on plan response hydration) */
  useEffect(() => {
    invalidateInFlightAndPlan();
    setFxToAccount("");
    setPlan(null);
    setPlanStale(true);
  }, [effectiveAcctCurrency, staticQuoteCurrency]);

  const canonicalSymKey = (raw: string): string => {
    const cleaned = raw.trim().toUpperCase().replace(/^(US|CN|FX|HK|SH|SZ|COMMODITY|CRYPTO):/, "");
    return cleaned.trim();
  };

  const cleanPairOppositeSym = pairOppositeSym.trim();
  const pairSameAsPrimary = Boolean(
    cleanPairOppositeSym &&
      canonicalSymKey(cleanPairOppositeSym) === canonicalSymKey(symbol),
  );

  const handleCalculate = async (e: FormEvent) => {
    e.preventDefault();
    if (ledgerUninitialized && !useSimEquity) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("本地持仓账本尚未初始化，请勾选「使用模拟本金测算」并确认模拟本金，或先前往哨兵总控初始化账本。");
      return;
    }
    const fxVal = isKnownSameCurrency ? undefined : optNum(fxToAccount);
    if (!isKnownSameCurrency && fxToAccount.trim() !== "" && (fxVal === undefined || fxVal <= 0)) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("汇率折算 (fx_to_account) 必须为大于 0 的有效数值");
      return;
    }
    if (useSimEquity) {
      const simEqNum = optNum(simEquity);
      if (simEqNum === undefined || simEqNum <= 0) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("模拟测算本金必须为大于 0 的有效数值");
        return;
      }
    }
    if (pairTrade && pairLegVerified && (!cleanPairOppositeSym || pairSameAsPrimary)) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("配对核验必须填写与当前主标的不同且有效的反向对冲腿代码。");
      return;
    }

    if (planAbortRef.current) {
      planAbortRef.current.abort();
    }
    const ctrl = new AbortController();
    planAbortRef.current = ctrl;
    const reqId = ++planReqIdRef.current;
    const capturedRev = planInputRevRef.current;

    setBusy(true);
    setErr(null);
    try {
      const effectivePairVerified = Boolean(
        pairTrade && pairLegVerified && cleanPairOppositeSym.length > 0 && !pairSameAsPrimary,
      );
      const body: TradePlanBody = {
        symbol,
        direction,
        live,
        entry_price: optNum(entryPrice),
        target_price: optNum(targetPrice),
        risk_budget_pct: optNum(riskPct) ?? 1.0,
        slot_risk_share: optNum(slotShare) ?? 1.0,
        horizon_days: Math.max(1, Math.round(optNum(horizonDays) ?? 90)),
        has_l1_l2: hasL1L2,
        pending_verifications: Math.max(0, Math.round(optNum(pendingVerif) ?? 0)),
        thesis_falsified: thesisFalsified,
        pair_trade: pairTrade,
        pair_leg_verified: effectivePairVerified,
        ...(effectivePairVerified ? { pair_leg_symbol: cleanPairOppositeSym } : {}),
        hedge_only: hedgeOnly,
      };

      /* Strict Live 30m stop discipline: NEVER send manual stops or alignment overrides when live === true */
      if (!live) {
        body.tactical_stop_30m = optNum(stop30m);
        body.structural_stop_daily = optNum(stopDaily);
        if (is30mSnapshot && align?.alignment_status) {
          body.alignment_status = align.alignment_status;
        }
      }

      if (useSimEquity) {
        body.account_equity = optNum(simEquity);
        body.account_currency = simCurrency.trim().toUpperCase() || "USD";
      } else {
        /* Always bind the currency context shown in the form so backend rejects stale-currency ledger submissions */
        body.account_currency = effectiveAcctCurrency;
      }

      if (fxVal !== undefined && !isKnownSameCurrency) {
        body.fx_to_account = fxVal;
      }

      const res = await api.tradePlan(body, ctrl.signal);
      if (
        ctrl.signal.aborted ||
        reqId !== planReqIdRef.current ||
        capturedRev !== planInputRevRef.current
      ) {
        return;
      }
      setPlan(res);
      setPlanStale(false);
    } catch (ex) {
      if (
        ctrl.signal.aborted ||
        (ex instanceof DOMException && ex.name === "AbortError") ||
        reqId !== planReqIdRef.current
      ) {
        return;
      }
      if (ex instanceof ApiError && ex.status === 409) {
        setLedgerMissingDetected(true);
      }
      setPlan(null);
      setPlanStale(true);
      setErr(errorMessage(ex));
    } finally {
      if (reqId === planReqIdRef.current) {
        if (planAbortRef.current === ctrl) {
          planAbortRef.current = null;
        }
        setBusy(false);
      }
    }
  };

  const gate = plan?.four_quadrant_gate;
  const carry = plan?.expression_and_carry;
  const pos = plan?.position_plan;

  const equitySourceRaw = plan?.equity_source ?? pos?.equity_source ?? null;
  const isRealLedgerPlan = equitySourceRaw === "portfolio_ledger";
  const stopSourceRaw = plan?.stop_source ?? pos?.stop_source ?? null;
  const stopTfRaw = plan?.stop_timeframe ?? pos?.stop_timeframe ?? null;

  const canOpenPosition = Boolean(!planStale && !busy && plan && gate?.actionable === true);

  const isBackwardationShort =
    (pos?.direction ?? direction) === "short" &&
    carry?.curve_structure &&
    carry.curve_structure.toLowerCase().includes("backwardation");

  const handleOpenLedgerFromPlan = () => {
    if (!canOpenPosition || !plan) return;
    const computedPos = plan.position_plan;
    const computedDir: Direction =
      plan.four_quadrant_gate.direction === "short" || computedPos.direction === "short"
        ? "short"
        : "long";
    const realLedgerCcy = (accountCurrency || computedPos.account_currency || "USD").toUpperCase();
    const planAcctCcy = (computedPos.account_currency || realLedgerCcy).toUpperCase();
    const inheritFx =
      planAcctCcy === realLedgerCcy ? (computedPos.fx_to_account ?? null) : null;

    onOpenPositionSeed({
      symbol: computedPos.symbol,
      direction: computedDir,
      entry_price: computedPos.entry_price ?? null,
      tactical_stop_30m: computedPos.tactical_stop_30m ?? null,
      structural_stop_daily: computedPos.structural_stop_daily ?? null,
      target_price: computedPos.target_price ?? null,
      quantity: computedPos.total_quantity ?? null,
      fx_to_account: inheritFx,
    });
  };

  return (
    <section className="card flex flex-col gap-4 p-4" aria-label="第四层四象限闸门与交易算仓器">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <Calculator className="h-4 w-4 text-slot1" aria-hidden="true" />
          <h2 className="m-0 text-sm font-semibold text-ink">
            第四层 · 四象限终裁 & 30min 风险预算算仓器（{symbol}）
          </h2>
          {live ? (
            <StatusBadge
              tone="info"
              label="Live 锁定实时 30min 结构止损（不可手工覆盖）"
            />
          ) : (
            <StatusBadge
              tone="warning"
              label={
                is30mSnapshot
                  ? "手动推演模式（可编辑 30m 止损）"
                  : `手动推演模式（当前视图 ${snapInterval}，未自动带入非 30m 止损）`
              }
            />
          )}
        </div>
        <span className="text-xs text-muted">
          铁律：30min 缠论诞生交易以压缩日线噪点 · 大 B 结构强现实 vs 弱预期劈叉永不否决做空
        </span>
      </div>

      {/* Uninitialized Ledger / Simulation Equity Banner */}
      {ledgerUninitialized || useSimEquity ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slot1/40 bg-elevated/60 px-3.5 py-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-ink">
              <input
                type="checkbox"
                checked={useSimEquity}
                onChange={(e) => {
                  setUseSimEquity(e.target.checked);
                  markStale();
                }}
              />
              <span>使用模拟本金测算</span>
            </label>
            {ledgerUninitialized ? (
              <span className="text-muted">
                （检测到本地持仓账本尚未初始化，已显式切换为模拟测算；实盘入账前请先初始化账本）
              </span>
            ) : (
              <span className="text-muted">（当前覆盖真实账本净值，仅作沙盘算仓测算）</span>
            )}
          </div>

          {useSimEquity ? (
            <div className="flex flex-wrap items-center gap-2">
              <label className="inline-flex items-center gap-1.5 text-ink-2">
                <span>模拟净值</span>
                <input
                  className="input h-7 w-32"
                  type="number"
                  step="any"
                  min="100"
                  aria-label="模拟账户净值"
                  value={simEquity}
                  onChange={(e) => {
                    setSimEquity(e.target.value);
                    markStale();
                  }}
                />
              </label>
              <label className="inline-flex items-center gap-1.5 text-ink-2">
                <span>账户币种</span>
                <select
                  className="input h-7 w-24"
                  aria-label="模拟账户本位币"
                  value={simCurrency}
                  onChange={(e) => {
                    setSimCurrency(e.target.value);
                    markStale();
                  }}
                >
                  <option value="USD">USD</option>
                  <option value="CNY">CNY</option>
                  <option value="HKD">HKD</option>
                </select>
              </label>
            </div>
          ) : null}
        </div>
      ) : null}

      <form onSubmit={handleCalculate} className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="flex flex-col gap-2 lg:col-span-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted">交易方向 & 实盘联立</span>
            {!ledgerUninitialized && !useSimEquity ? (
              <button
                type="button"
                className="text-[11px] text-slot1 hover:underline cursor-pointer"
                onClick={() => {
                  setUseSimEquity(true);
                  markStale();
                }}
              >
                改用模拟本金
              </button>
            ) : null}
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              className="chip flex-1 justify-center"
              aria-pressed={direction === "long"}
              onClick={() => {
                if (direction !== "long") {
                  setDirection("long");
                  markStale();
                }
              }}
            >
              做多 (Long)
            </button>
            <button
              type="button"
              className="chip flex-1 justify-center"
              aria-pressed={direction === "short"}
              onClick={() => {
                if (direction !== "short") {
                  setDirection("short");
                  markStale();
                }
              }}
            >
              做空 (Short)
            </button>
            <button
              type="button"
              className="chip"
              aria-pressed={live}
              onClick={() => {
                setLive((v) => !v);
                markStale();
              }}
              title="开启后由后端实时联立日线+30min缠论止损与期限曲线校验，禁止手工覆盖止损"
            >
              Live 联立
            </button>
          </div>
        </div>

        <label className="field lg:col-span-2">
          <span>计划入场价</span>
          <input
            className="input"
            type="number"
            step="any"
            placeholder="默认现价"
            value={entryPrice}
            onChange={(e) => {
              setEntryPrice(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-2">
          <span className="inline-flex items-center gap-1">
            <span>30min 战术止损</span>
            {live ? <Lock className="h-3 w-3 text-slot1" aria-hidden="true" /> : null}
          </span>
          <input
            className={`input ${live ? "cursor-not-allowed opacity-65" : ""}`}
            type="number"
            step="any"
            disabled={live}
            placeholder={live ? "Live 实时 30m 自动锁定" : is30mSnapshot ? "30m 止损位" : "请手填 30m 止损"}
            title={live ? "Live 锁定实时 30min 结构止损（不可手工覆盖）" : undefined}
            value={stop30m}
            onChange={(e) => {
              setStop30m(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-2">
          <span className="inline-flex items-center gap-1">
            <span>日线结构位 (ZD/ZG)</span>
            {live ? <Lock className="h-3 w-3 text-slot1" aria-hidden="true" /> : null}
          </span>
          <input
            className={`input ${live ? "cursor-not-allowed opacity-65" : ""}`}
            type="number"
            step="any"
            disabled={live}
            placeholder={live ? "Live 实时日线锁定" : "可选对照"}
            title={live ? "Live 锁定实时日线结构防线（不可手工覆盖）" : undefined}
            value={stopDaily}
            onChange={(e) => {
              setStopDaily(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-3">
          <span>1Q~1Y 目标价（验算盈亏比 ≥ 2.5R）</span>
          <input
            className="input"
            type="number"
            step="any"
            placeholder="目标价"
            value={targetPrice}
            onChange={(e) => {
              setTargetPrice(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-2">
          <span>单笔风险预算 (%)</span>
          <input
            className="input"
            type="number"
            step="0.1"
            min="0.1"
            max="10"
            value={riskPct}
            onChange={(e) => {
              setRiskPct(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-2">
          <span>龙头席位份额</span>
          <select
            className="input"
            value={slotShare}
            onChange={(e) => {
              setSlotShare(e.target.value);
              markStale();
            }}
          >
            <option value="1.0">100% 唯一真龙头</option>
            <option value="0.5">50% 双寡头平分</option>
          </select>
        </label>

        <label className="field lg:col-span-2">
          <span>预计持仓周期 (天)</span>
          <input
            className="input"
            type="number"
            min="5"
            max="365"
            value={horizonDays}
            onChange={(e) => {
              setHorizonDays(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-2">
          <span>未完成核验项数</span>
          <input
            className="input"
            type="number"
            min="0"
            max="10"
            value={pendingVerif}
            onChange={(e) => {
              setPendingVerif(e.target.value);
              markStale();
            }}
          />
        </label>

        <label className="field lg:col-span-4">
          <span>汇率折算 (fx_to_account: 1 报价币 = ? 账户币)</span>
          <input
            className="input"
            type="number"
            step="any"
            min="0.000001"
            disabled={isKnownSameCurrency}
            placeholder={
              needsCrossCurrencyFx
                ? `跨币种必填：1 ${inferredQuoteCurrency} = ? ${effectiveAcctCurrency}`
                : isKnownSameCurrency
                  ? `同币种 (${effectiveAcctCurrency}) 固定为 1.0`
                  : `跨币种填写折算汇率`
            }
            value={isKnownSameCurrency ? "" : fxToAccount}
            onChange={(e) => {
              setFxToAccount(e.target.value);
              markStale();
            }}
          />
        </label>

        {needsCrossCurrencyFx ? (
          <div className="rounded border border-warning/45 bg-warning/10 px-3 py-1.5 text-xs text-ink lg:col-span-12">
            跨币种算仓提示：识别到 <strong>{symbol}</strong> 报价币为{" "}
            <strong>{inferredQuoteCurrency}</strong>，当前算仓账户币为{" "}
            <strong>{effectiveAcctCurrency}</strong>。请填写折算汇率（<code>1 {inferredQuoteCurrency} = ? {effectiveAcctCurrency}</code>
            ，如 CNY→USD 约 <code>0.138</code>），未填时后端将触发跨币种 Fail-Closed 保护拒绝。
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-3 lg:col-span-12">
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={hasL1L2}
              onChange={(e) => {
                setHasL1L2(e.target.checked);
                markStale();
              }}
            />
            <span>具备 L1/L2 核心证据</span>
          </label>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={pairTrade}
              onChange={(e) => {
                const checked = e.target.checked;
                setPairTrade(checked);
                if (!checked) {
                  setPairLegVerified(false);
                  setPairOppositeSym("");
                }
                markStale();
              }}
            />
            <span>同板块配对</span>
          </label>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={hedgeOnly}
              onChange={(e) => {
                setHedgeOnly(e.target.checked);
                markStale();
              }}
            />
            <span>股票对冲卖点</span>
          </label>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={thesisFalsified}
              onChange={(e) => {
                setThesisFalsified(e.target.checked);
                markStale();
              }}
            />
            <span>L1 已证伪</span>
          </label>
          <button type="submit" className="btn btn-primary ml-auto" disabled={busy}>
            <Calculator className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{busy ? "正在算仓…" : "运行四象限算仓"}</span>
          </button>
        </div>

        {/* Pair-trade opposite-leg verification guard (never auto-sets pair_leg_verified = true!) */}
        {pairTrade ? (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warning/45 bg-warning/10 px-3.5 py-2.5 text-xs text-ink lg:col-span-12">
            <div className="max-w-2xl">
              <strong>配对单腿防绕过保护：</strong>勾选「同板块配对」默认保持{" "}
              <code>pair_leg_verified = false</code>，单腿提交将保留在观察池（防止股票空头腿未核验直接放行）。仅在独立完成反向对冲腿研究与缠论确认后方可勾选核验。
            </div>
            <div className="flex flex-wrap items-center gap-2.5">
              <input
                className="input h-7 w-36"
                placeholder="对冲腿代码 (如 ETN)"
                aria-label="已核验反向对冲腿标的代码"
                value={pairOppositeSym}
                onChange={(e) => {
                  setPairOppositeSym(e.target.value);
                  /* Changing the opposite-leg symbol always clears prior verification */
                  setPairLegVerified(false);
                  markStale();
                }}
              />
              <label className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-ink">
                <input
                  type="checkbox"
                  disabled={!cleanPairOppositeSym || pairSameAsPrimary}
                  checked={pairLegVerified && Boolean(cleanPairOppositeSym) && !pairSameAsPrimary}
                  onChange={(e) => {
                    setPairLegVerified(e.target.checked);
                    markStale();
                  }}
                />
                <span>已独立核验反向对冲腿</span>
              </label>
              {pairSameAsPrimary ? (
                <span className="text-[11px] font-medium text-critical">
                  反向腿不能与主标的 ({symbol}) 相同
                </span>
              ) : null}
            </div>
          </div>
        ) : null}
      </form>

      {err ? (
        <div role="alert" className="flex items-start gap-2 rounded-lg border border-critical/50 bg-critical/10 px-3.5 py-2.5 text-xs text-ink">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
          <div>
            <div className="font-semibold">算仓校验拒绝（Fail-Closed 实盘保护）</div>
            <div className="mt-0.5 text-ink-2">{err}</div>
          </div>
        </div>
      ) : null}

      {plan && planStale ? (
        <div className="flex items-center gap-2 rounded-md border border-warning/45 bg-warning/10 px-3 py-2 text-xs text-ink">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>算仓输入参数已变更，下方旧方案已锁定失效；请点击「运行四象限算仓」重新验算后方可录入账本。</span>
        </div>
      ) : null}

      {plan ? (
        <div className={`grid grid-cols-1 gap-3.5 xl:grid-cols-12 ${planStale ? "opacity-65" : ""}`}>
          {/* 1. Four-Quadrant Verdict Card */}
          <div className="flex flex-col gap-2.5 rounded-lg border border-hairline bg-page/60 p-3.5 xl:col-span-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="section-title">四象限最终闸门</span>
              {gate ? <GateDecisionBadge decision={gate.decision} actionable={gate.actionable} /> : null}
            </div>

            {/* Provenance Badges: Equity Source & Stop Source/Timeframe */}
            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge
                tone={isRealLedgerPlan ? "good" : "warning"}
                label={isRealLedgerPlan ? "真实账本" : "模拟测算"}
              />
              {stopSourceRaw || stopTfRaw ? (
                <StatusBadge
                  tone={stopSourceRaw === "live_30m_chanlun" ? "info" : "neutral"}
                  label={
                    stopSourceRaw === "live_30m_chanlun"
                      ? `Live 30m 缠论止损 (${stopTfRaw || "30m"})`
                      : `手动情景止损 (${stopTfRaw || "manual"})`
                  }
                />
              ) : null}
            </div>

            {gate?.reason ? <p className="m-0 text-xs font-medium text-ink">{gate.reason}</p> : null}
            {gate?.reasons && gate.reasons.length > 0 ? (
              <ul className="m-0 list-disc space-y-1 pl-4 text-xs text-ink-2">
                {gate.reasons.map((r, idx) => (
                  <li key={idx}>{r}</li>
                ))}
              </ul>
            ) : null}
            {plan.live_warnings && plan.live_warnings.length > 0 ? (
              <div className="mt-1 space-y-1 rounded border border-warning/40 bg-warning/10 p-2 text-[11.5px] text-ink">
                {plan.live_warnings.map((w, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                    <span>{w}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {/* 2. B-Structure Strong Reality vs Weak Expectation Card */}
          <div className="flex flex-col gap-2.5 rounded-lg border border-hairline bg-page/60 p-3.5 xl:col-span-4">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title inline-flex items-center gap-1.5">
                <GitBranch className="h-3.5 w-3.5 text-slot2" aria-hidden="true" />
                期限结构 & 展期损耗权衡
              </span>
              {carry?.curve_structure ? (
                <StatusBadge
                  tone={isBackwardationShort ? "warning" : "info"}
                  label={carry.curve_structure}
                />
              ) : (
                <StatusBadge tone="neutral" label={carry?.asset_class || "现货/正股"} />
              )}
            </div>

            {isBackwardationShort ? (
              <div className="rounded border border-warning/45 bg-warning/10 px-2.5 py-2 text-xs text-ink">
                <strong>大 B 结构强现实 vs 弱预期劈叉：</strong>
                近月升水/低库存反映当下现实，<strong>绝不否决做空方向</strong>；以下已量化展期损耗并给出近远月合约表达建议。
              </div>
            ) : null}

            <div className="tnum grid grid-cols-2 gap-2 text-xs">
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">近月年化展期 / Carry</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtPct(carry?.near_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">全曲线年化展期</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtPct(carry?.curve_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">计入 Carry 后盈亏比</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtRatio(carry?.carry_adjusted_rr_ratio ?? pos?.carry_adjusted_rr_ratio)}
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">近月极端移仓压力 R:R</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtRatio(carry?.near_carry_stress_rr_ratio)}
                </div>
              </div>
            </div>

            {carry?.expression_recommendation ? (
              <p className="m-0 text-xs text-ink-2">
                <strong className="text-ink">最优载体：</strong>
                {carry.expression_recommendation}
              </p>
            ) : null}
            {carry?.contract_guidance ? (
              <p className="m-0 text-xs text-muted">{carry.contract_guidance}</p>
            ) : null}
          </div>

          {/* 3. Sizing Summary & 40/40/20 Tranche Plan */}
          <div className="flex flex-col gap-2.5 rounded-lg border border-hairline bg-page/60 p-3.5 xl:col-span-4">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title">算仓结果 & 分批建仓（禁浮亏摊平）</span>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!canOpenPosition}
                title={
                  !gate?.actionable
                    ? "四象限闸门未放行（观察池或否决），禁止直接录入持仓账本"
                    : planStale
                      ? "输入已变更，请重新运行算仓"
                      : "将核定仓位参数带入持仓账本弹窗"
                }
                onClick={handleOpenLedgerFromPlan}
              >
                <PlusCircle className="h-3.5 w-3.5" aria-hidden="true" />
                <span>录入持仓账本</span>
              </button>
            </div>

            <div className="tnum grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">核定总仓位</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {pos?.total_quantity_str || fmtNum(pos?.total_quantity, 4)}
                </div>
                {pos?.total_lots != null &&
                pos?.total_quantity != null &&
                pos.total_lots !== pos.total_quantity ? (
                  <div className="mt-0.5 text-[10.5px] text-muted">
                    步进份数：{fmtNum(pos.total_lots, 0)} 份
                  </div>
                ) : null}
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">实际风险敞口</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtMoney(pos?.actual_risk_amount, pos?.account_currency)} ({fmtPct(pos?.actual_risk_pct, 2)})
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">名义盈亏比</div>
                <div className="mt-0.5 font-semibold text-ink">{fmtRatio(pos?.rr_ratio)}</div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">入场 / 30m 止损</div>
                <div className="mt-0.5 font-semibold text-ink">
                  {fmtPrice(pos?.entry_price)} / <span className="text-critical">{fmtPrice(pos?.tactical_stop_30m)}</span>
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">生效约束 & 保证金</div>
                <div className="mt-0.5 truncate font-medium text-ink-2">
                  {pos?.sizing_binding_constraint || "risk_budget"} · {fmtPct(pos?.margin_pct_of_equity, 1)}
                </div>
              </div>
              <div className="rounded border border-hairline bg-surface p-2">
                <div className="text-muted">30m 省下日线噪点</div>
                <div className="mt-0.5 font-semibold text-good">
                  {fmtMoney(pos?.noise_risk_saved_amount, pos?.account_currency)}
                </div>
              </div>
            </div>

            {pos?.oversized_single_lot ? (
              <div className="rounded border border-critical/50 bg-critical/10 px-2.5 py-1.5 text-xs text-ink">
                单手最小合约风险超出当前预算上限，已启动单手超预算保护（核定 0 手或需迷你合约表达）。
              </div>
            ) : null}

            {pos?.tranches && pos.tranches.length > 0 ? (
              <div tabIndex={0} role="region" aria-label="40/40/20 分批建仓计划表" className="table-wrapper">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">批次</th>
                      <th scope="col" className="num">
                        权重
                      </th>
                      <th scope="col" className="num">
                        数量
                      </th>
                      <th scope="col" className="num">
                        风险额
                      </th>
                      <th scope="col">执行纪律</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pos.tranches.map((tr) => (
                      <tr key={tr.tranche_index}>
                        <th scope="row">第 {tr.tranche_index} 批</th>
                        <td data-label="权重" className="num">
                          {fmtPct((tr.weight ?? 0) * 100, 0)}
                        </td>
                        <td data-label="数量" className="num">
                          <span>{tr.quantity_str || fmtNum(tr.quantity, 4)}</span>
                          {tr.lots != null && tr.quantity != null && tr.lots !== tr.quantity ? (
                            <span className="ml-1 text-[10.5px] text-muted">({fmtNum(tr.lots, 0)}份)</span>
                          ) : null}
                        </td>
                        <td data-label="风险额" className="num">
                          {fmtPrice(tr.risk_amount)}
                        </td>
                        <td data-label="执行纪律" className="text-muted">
                          {tr.rule}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
