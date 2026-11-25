import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  BookOpen,
  Calculator,
  CheckCircle2,
  Crosshair,
  Eye,
  GitBranch,
  Lock,
  PlusCircle,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type {
  ContractSpec,
  Direction,
  MarketDualResponse,
  TradePlanBody,
  TradePlanResponse,
} from "../../api/types";
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
import { isSnapshotIntradayUntrusted, selectTrustedLastQuote } from "../../lib/snapshotPrice";
import type { PlanOverlayLines } from "../chart/ChanlunChart";
import type { OpenPositionSeed } from "../portfolio/PortfolioDialogs";
import { CrossCurrencyFxBanner } from "../ui/CrossCurrencyFxBanner";
import { GateDecisionBadge, StatusBadge } from "../ui/StatusBadge";

export type PickPriceField = "entry" | "stop30m" | "target";

export interface PickedPriceUpdate {
  seq: number;
  field: PickPriceField;
  price: number;
}

export interface ArchivePlanContext {
  forSymbol?: string;
  exists: boolean;
  market?: string | null;
  sectorTheme?: string | null;
  hasL1L2Evidence?: boolean;
  evidenceCount?: number;
}

type SizingDriver = "risk" | "amount" | "qty";

function quantizeQtyDown(rawQty: number, minOrderQty: number, lotStep: number, fractional: boolean): number {
  if (!Number.isFinite(rawQty) || rawQty + 1e-12 < minOrderQty) return 0;
  const k = Math.floor((rawQty - minOrderQty) / lotStep + 1e-12);
  if (k < 0) return 0;
  const q = minOrderQty + k * lotStep;
  return fractional ? Number(q.toFixed(6)) : Math.round(q);
}

function formatQtyDisplay(qty: number, fractional: boolean): string {
  if (!Number.isFinite(qty)) return "";
  if (!fractional) return String(Math.round(qty));
  return Number(qty.toFixed(4)).toString();
}

function assetUnitLabel(assetClass?: string | null): string {
  const ac = (assetClass || "").toLowerCase();
  if (ac === "cn_future" || ac === "commodity") return "手";
  if (ac === "fx") return "单位";
  if (ac === "crypto") return "枚";
  return "股";
}

interface TradePlannerCardProps {
  symbol: string;
  dualData: MarketDualResponse | null;
  snapshotLoading?: boolean;
  snapshotError?: string | null;
  intradayInterval: "15m" | "30m" | "60m";
  portfolioInitialized?: boolean;
  accountEquity?: number | null;
  accountCurrency?: string | null;
  archiveContext?: ArchivePlanContext | null;
  activePickField?: PickPriceField | null;
  pickedPrice?: PickedPriceUpdate | null;
  onRequestPickField?: (field: PickPriceField | null) => void;
  onOverlayChange?: (overlay: PlanOverlayLines | null) => void;
  onOpenPositionSeed: (seed: OpenPositionSeed) => void;
  onLedgerInitialized?: () => void;
  onJumpArchive?: (symbol: string, market?: string | null) => void;
}

export function TradePlannerCard({
  symbol,
  dualData,
  snapshotLoading = false,
  snapshotError = null,
  intradayInterval,
  portfolioInitialized,
  accountEquity = null,
  accountCurrency,
  archiveContext,
  activePickField = null,
  pickedPrice = null,
  onRequestPickField,
  onOverlayChange,
  onOpenPositionSeed,
  onLedgerInitialized,
  onJumpArchive,
}: TradePlannerCardProps) {
  const headingId = useId();
  const snapshotStale = isSnapshotIntradayUntrusted(dualData, snapshotError);
  const align = snapshotStale ? undefined : dualData?.chanlun?.dual_alignment;
  const dailyZs = dualData?.chanlun?.daily?.latest_zhongshu;
  const trustedQuote = selectTrustedLastQuote(dualData, snapshotError);
  const lastClose = trustedQuote.price;

  const snapInterval = dualData?.intraday_interval || align?.intraday_timeframe || intradayInterval;
  const is30mSnapshot = snapInterval === "30m" || snapInterval === "30min";

  const [direction, setDirection] = useState<Direction>("long");
  const [live, setLive] = useState(true);
  const [entryPrice, setEntryPrice] = useState("");
  const [stop30m, setStop30m] = useState("");
  const [stopDaily, setStopDaily] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [sizingDriver, setSizingDriver] = useState<SizingDriver>("risk");
  const [riskPct, setRiskPct] = useState("1.0");
  const [allocAmount, setAllocAmount] = useState("");
  const [manualQty, setManualQty] = useState("");
  const [slotShare, setSlotShare] = useState("1.0");
  const [horizonDays, setHorizonDays] = useState("90");
  const [fxToAccount, setFxToAccount] = useState("");
  const [showOverlay, setShowOverlay] = useState(false);

  /* Uninitialized ledger & explicit simulation equity controls */
  const [ledgerMissingDetected, setLedgerMissingDetected] = useState(false);
  const ledgerUninitialized = portfolioInitialized === false || ledgerMissingDetected;
  const [useSimEquity, setUseSimEquity] = useState(portfolioInitialized === false);
  const [simEquity, setSimEquity] = useState("1000000");
  const [simCurrency, setSimCurrency] = useState("USD");
  const [initBusy, setInitBusy] = useState(false);
  const [ledgerInitNotice, setLedgerInitNotice] = useState<string | null>(null);

  /* Gate inputs: strictly derived from verified research archive (never manually bypassed in UI) */
  const archiveVerifiedL1L2 = Boolean(archiveContext?.exists && archiveContext?.hasL1L2Evidence);
  const [hasL1L2, setHasL1L2] = useState(false);
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
  const lastAppliedPickSeqRef = useRef(0);

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

  /* Apply price picked interactively from chart click */
  useEffect(() => {
    if (!pickedPrice || pickedPrice.seq === lastAppliedPickSeqRef.current) return;
    lastAppliedPickSeqRef.current = pickedPrice.seq;
    const formatted = fmtInputPrice(pickedPrice.price);
    if (pickedPrice.field === "entry") {
      setEntryPrice(formatted);
      markStale();
    } else if (pickedPrice.field === "stop30m" && !live) {
      setStop30m(formatted);
      markStale();
    } else if (pickedPrice.field === "target") {
      setTargetPrice(formatted);
      markStale();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickedPrice, live]);

  /* Sync optional R:R ruler lines onto chart when enabled (strictly valid-side 30m stop in Live mode) */
  useEffect(() => {
    if (!onOverlayChange) return;
    if (!showOverlay) {
      onOverlayChange(null);
      return;
    }
    const validPlanPos = !planStale ? plan?.position_plan : null;
    const ep = optNum(entryPrice) ?? validPlanPos?.entry_price ?? null;
    let s30: number | null = null;
    if (live) {
      if (validPlanPos?.tactical_stop_30m != null) {
        s30 = validPlanPos.tactical_stop_30m;
      } else if (is30mSnapshot && align?.intraday_tactical_stop != null) {
        const cand = align.intraday_tactical_stop;
        const sideValid =
          ep == null || (direction === "long" ? cand < ep : cand > ep);
        s30 = sideValid ? cand : null;
      }
    } else {
      s30 = optNum(stop30m) ?? validPlanPos?.tactical_stop_30m ?? null;
    }
    const tp = optNum(targetPrice) ?? validPlanPos?.target_price ?? null;
    onOverlayChange({ entry: ep, stop30m: s30, target: tp });
  }, [
    showOverlay,
    entryPrice,
    stop30m,
    targetPrice,
    direction,
    live,
    is30mSnapshot,
    align?.intraday_tactical_stop,
    planStale,
    plan?.position_plan,
    onOverlayChange,
  ]);

  /* Reset symbol-scoped gate defaults (Fail-Closed hasL1L2 = false) whenever target symbol changes */
  useEffect(() => {
    setHasL1L2(false);
    setPendingVerif("0");
    setThesisFalsified(false);
    setHedgeOnly(false);
    setSizingDriver("risk");
    setAllocAmount("");
    setManualQty("");
  }, [symbol]);

  const pickValidDailyStop = (dir: Direction, refEntry: number | null | undefined): number | null => {
    const candidates = [
      align?.daily_structural_stop,
      dir === "long" ? dailyZs?.zd : dailyZs?.zg,
    ];
    for (const c of candidates) {
      if (typeof c !== "number" || !Number.isFinite(c) || c <= 0) continue;
      if (refEntry == null || !Number.isFinite(refEntry)) return c;
      if (dir === "long" && c < refEntry) return c;
      if (dir === "short" && c > refEntry) return c;
    }
    return null;
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
    onRequestPickField?.(null);

    const bias = (
      align?.daily_bias ||
      dualData?.chanlun?.signal_summary?.signal_meta?.direction ||
      ""
    ).toLowerCase();
    const nextDir: Direction =
      bias.includes("short") || bias.includes("bear") || bias.includes("down") || bias.includes("空")
        ? "short"
        : "long";
    setDirection(nextDir);

    if (snapshotStale) {
      // Never auto-prefill stale/untrusted intraday prices, stops, or derived targets
      setEntryPrice("");
      setStop30m("");
      setStopDaily("");
      setTargetPrice("");
      return;
    }

    const ep = lastClose;
    setEntryPrice(fmtInputPrice(ep));

    /* STRICT 30m discipline: NEVER prefill 15m/60m or daily stops into tactical_stop_30m */
    const s30 = is30mSnapshot ? (align?.intraday_tactical_stop ?? null) : null;
    setStop30m(fmtInputPrice(s30));

    const sDaily = pickValidDailyStop(nextDir, ep);
    setStopDaily(fmtInputPrice(sDaily));

    if (ep != null && s30 != null && ep !== s30) {
      const dist = Math.abs(ep - s30);
      const tgt = nextDir === "long" ? ep + dist * 3 : Math.max(0.0001, ep - dist * 3);
      setTargetPrice(fmtInputPrice(tgt));
    } else {
      setTargetPrice("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    symbol,
    snapshotStale,
    is30mSnapshot,
    align?.daily_bias,
    align?.intraday_tactical_stop,
    align?.daily_structural_stop,
    dualData?.chanlun?.signal_summary?.signal_meta?.direction,
    dailyZs?.zd,
    dailyZs?.zg,
    lastClose,
  ]);

  /* Hydrate L1/L2 gate strictly from verified archive (unarchived/failed stays false) */
  useEffect(() => {
    setHasL1L2((prev) => {
      if (prev !== archiveVerifiedL1L2) {
        invalidateInFlightAndPlan();
        setPlanStale(true);
      }
      return archiveVerifiedL1L2;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archiveVerifiedL1L2, symbol]);

  const effectiveAcctCurrency = (useSimEquity ? simCurrency : accountCurrency || "USD").toUpperCase();
  const staticQuoteCurrency = inferAssetCurrency(symbol, dualData?.asset_class);
  const inferredQuoteCurrency =
    plan?.position_plan?.quote_currency?.toUpperCase() ??
    dualData?.contract_spec?.quote_currency?.toUpperCase() ??
    staticQuoteCurrency;
  const isKnownSameCurrency = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency === effectiveAcctCurrency,
  );
  const needsCrossCurrencyFx = Boolean(
    inferredQuoteCurrency && inferredQuoteCurrency !== effectiveAcctCurrency,
  );

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

  /* Live contract specification & bidirectional sizing preview */
  const [initializedEquityHint, setInitializedEquityHint] = useState<number | null>(null);
  const resolvedEquity = useMemo(() => {
    if (useSimEquity) return optNum(simEquity) ?? null;
    return accountEquity ?? initializedEquityHint ?? null;
  }, [useSimEquity, simEquity, accountEquity, initializedEquityHint]);

  const resolvedAssetClass = (
    plan?.position_plan?.asset_class ||
    dualData?.contract_spec?.asset_class ||
    dualData?.asset_class ||
    "us_stock_or_etf"
  ).toLowerCase();
  const isEquityAsset = resolvedAssetClass === "us_stock_or_etf" || resolvedAssetClass === "a_share";
  const isMarginAsset =
    resolvedAssetClass === "cn_future" ||
    resolvedAssetClass === "commodity" ||
    resolvedAssetClass === "fx";
  const unitLabel = assetUnitLabel(resolvedAssetClass);

  const activeSpec = useMemo(() => {
    const rawSpec = (
      (!planStale && plan?.position_plan?.contract_spec
        ? (plan.position_plan.contract_spec as ContractSpec)
        : null) ??
      dualData?.contract_spec ??
      null
    );
    const defaultCash = isEquityAsset || resolvedAssetClass === "crypto";
    const multiplier =
      rawSpec && typeof rawSpec.multiplier === "number" && rawSpec.multiplier > 0
        ? rawSpec.multiplier
        : defaultCash
          ? 1
          : null;
    const marginRate =
      rawSpec && typeof rawSpec.margin_rate === "number" && rawSpec.margin_rate > 0
        ? rawSpec.margin_rate
        : defaultCash
          ? 1
          : null;
    const minOrderQty =
      rawSpec && typeof rawSpec.min_order_qty === "number" && rawSpec.min_order_qty > 0
        ? rawSpec.min_order_qty
        : resolvedAssetClass === "a_share"
          ? 100
          : resolvedAssetClass === "crypto"
            ? 0.001
            : 1;
    const lotStep =
      rawSpec && typeof rawSpec.lot_step === "number" && rawSpec.lot_step > 0
        ? rawSpec.lot_step
        : minOrderQty;
    const fractional =
      rawSpec && typeof rawSpec.fractional === "boolean"
        ? rawSpec.fractional
        : resolvedAssetClass === "crypto";
    const knownSpec = rawSpec ? rawSpec.known_spec !== false : defaultCash;
    return { multiplier, marginRate, minOrderQty, lotStep, fractional, knownSpec };
  }, [planStale, plan?.position_plan?.contract_spec, dualData?.contract_spec, isEquityAsset, resolvedAssetClass]);

  const sizingPreview = useMemo(() => {
    const entry = optNum(entryPrice) ?? null;
    const target = optNum(targetPrice) ?? null;
    const equity = resolvedEquity != null && resolvedEquity > 0 ? resolvedEquity : null;
    const slot = optNum(slotShare) ?? 1.0;
    const fx = isKnownSameCurrency ? 1.0 : (optNum(fxToAccount) ?? null);

    let stop: number | null = null;
    if (live) {
      const liveCand =
        (!planStale ? plan?.position_plan?.tactical_stop_30m : null) ??
        (is30mSnapshot ? (align?.intraday_tactical_stop ?? null) : null);
      if (liveCand != null && (entry == null || (direction === "long" ? liveCand < entry : liveCand > entry))) {
        stop = liveCand;
      }
    } else {
      const manCand = optNum(stop30m) ?? null;
      if (manCand != null && (entry == null || (direction === "long" ? manCand < entry : manCand > entry))) {
        stop = manCand;
      }
    }

    const stopDist = entry != null && stop != null ? Math.abs(entry - stop) : null;
    const rewardDist =
      entry != null && target != null
        ? direction === "long"
          ? target - entry
          : entry - target
        : null;
    const rrRatio =
      stopDist != null && stopDist > 0 && rewardDist != null && rewardDist > 0
        ? rewardDist / stopDist
        : null;
    const rr25Target =
      entry != null && stopDist != null && stopDist > 0
        ? direction === "long"
          ? entry + stopDist * 2.5
          : Math.max(0.0001, entry - stopDist * 2.5)
        : null;

    const unitNotional =
      entry != null && activeSpec.multiplier != null && fx != null && fx > 0
        ? entry * activeSpec.multiplier * fx
        : null;
    const unitCost =
      unitNotional != null && activeSpec.marginRate != null
        ? unitNotional * activeSpec.marginRate
        : null;
    const unitRisk =
      stopDist != null && stopDist > 0 && activeSpec.multiplier != null && fx != null && fx > 0
        ? stopDist * activeSpec.multiplier * fx
        : null;

    let estQty: number | null = null;
    let estOutlay: number | null = null;
    let estNotional: number | null = null;
    let estRiskAmt: number | null = null;
    let actualRiskPct: number | null = null;
    let impliedBudgetRiskPct: number | null = null;
    let outlayPct: number | null = null;
    let cappedByCash = false;
    let exceedsEquity = false;
    let exceedsRiskCap = false;
    let belowMinLot = false;

    if (sizingDriver === "risk") {
      const rPct = optNum(riskPct);
      if (equity != null && rPct != null && rPct > 0 && unitRisk != null && unitRisk > 0 && unitCost != null && unitCost > 0) {
        const riskBudget = equity * (rPct / 100) * slot;
        const qtyByRisk = riskBudget / unitRisk;
        const qtyByCap = equity / unitCost;
        cappedByCash = qtyByCap + 1e-9 < qtyByRisk;
        const rawQty = Math.min(qtyByRisk, qtyByCap);
        estQty = quantizeQtyDown(rawQty, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        estOutlay = estQty * unitCost;
        estNotional = estQty * (unitNotional ?? unitCost);
        estRiskAmt = estQty * unitRisk;
        actualRiskPct = (estRiskAmt / equity) * 100;
        impliedBudgetRiskPct = rPct;
        outlayPct = (estOutlay / equity) * 100;
      }
    } else if (sizingDriver === "amount") {
      const amt = optNum(allocAmount);
      if (amt != null && amt > 0 && unitCost != null && unitCost > 0) {
        estQty = quantizeQtyDown(amt / unitCost, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        estOutlay = estQty * unitCost;
        estNotional = estQty * (unitNotional ?? unitCost);
        if (equity != null) {
          outlayPct = (estOutlay / equity) * 100;
          exceedsEquity = estOutlay > equity * 1.0001;
        }
        if (unitRisk != null && unitRisk > 0 && equity != null && estQty > 0) {
          estRiskAmt = estQty * unitRisk;
          actualRiskPct = (estRiskAmt / equity) * 100;
          impliedBudgetRiskPct = (estRiskAmt / (equity * slot)) * 100;
          exceedsRiskCap = impliedBudgetRiskPct > 10.0 + 1e-9;
        }
      }
    } else if (sizingDriver === "qty") {
      const qRaw = optNum(manualQty);
      if (qRaw != null && qRaw > 0) {
        estQty = quantizeQtyDown(qRaw, activeSpec.minOrderQty, activeSpec.lotStep, activeSpec.fractional);
        belowMinLot = estQty === 0;
        if (unitCost != null && unitCost > 0) {
          estOutlay = estQty * unitCost;
          estNotional = estQty * (unitNotional ?? unitCost);
          if (equity != null) {
            outlayPct = (estOutlay / equity) * 100;
            exceedsEquity = estOutlay > equity * 1.0001;
          }
        }
        if (unitRisk != null && unitRisk > 0 && equity != null && estQty > 0) {
          estRiskAmt = estQty * unitRisk;
          actualRiskPct = (estRiskAmt / equity) * 100;
          impliedBudgetRiskPct = (estRiskAmt / (equity * slot)) * 100;
          exceedsRiskCap = impliedBudgetRiskPct > 10.0 + 1e-9;
        }
      }
    }

    return {
      entry,
      stop,
      target,
      equity,
      slot,
      fx,
      stopDist,
      rrRatio,
      rr25Target,
      unitCost,
      unitRisk,
      estQty,
      estOutlay,
      estNotional,
      estRiskAmt,
      actualRiskPct,
      impliedBudgetRiskPct,
      outlayPct,
      cappedByCash,
      exceedsEquity,
      exceedsRiskCap,
      belowMinLot,
    };
  }, [
    entryPrice,
    targetPrice,
    resolvedEquity,
    slotShare,
    isKnownSameCurrency,
    fxToAccount,
    live,
    planStale,
    plan?.position_plan?.tactical_stop_30m,
    is30mSnapshot,
    align?.intraday_tactical_stop,
    direction,
    stop30m,
    activeSpec,
    sizingDriver,
    riskPct,
    allocAmount,
    manualQty,
  ]);

  const handleQuickInitLedger = async () => {
    const simEqNum = optNum(simEquity);
    if (simEqNum === undefined || simEqNum < 100) {
      setErr("初始化模拟账本需要填写不低于 100 的有效净值金额");
      return;
    }
    const ccy = simCurrency.trim().toUpperCase() || "USD";
    setInitBusy(true);
    setErr(null);
    setLedgerInitNotice(null);
    try {
      await api.portfolioInit({
        equity: simEqNum,
        base_currency: ccy,
        default_risk_per_trade_pct: 1.0,
        max_total_open_risk_pct: 6.0,
        max_macro_factor_risk_pct: 3.0,
      });
      setInitializedEquityHint(simEqNum);
      setLedgerMissingDetected(false);
      setUseSimEquity(false);
      setLedgerInitNotice(`已按 ${fmtMoney(simEqNum, ccy)} 创建本地模拟账本，算仓放行后可直接录入持仓。`);
      markStale();
      onLedgerInitialized?.();
    } catch (ex) {
      setErr(`初始化模拟账本失败：${errorMessage(ex)}`);
    } finally {
      setInitBusy(false);
    }
  };

  const applyQuickAllocPct = (pct: number) => {
    const eq = sizingPreview.equity;
    if (!eq || eq <= 0) return;
    setSizingDriver("amount");
    setAllocAmount(String(Math.round(eq * pct)));
    markStale();
  };

  const applyQuickRiskPct = (pct: number) => {
    setSizingDriver("risk");
    setRiskPct(String(pct));
    markStale();
  };

  const handleCalculate = async (e: FormEvent) => {
    e.preventDefault();
    onRequestPickField?.(null);

    if (snapshotStale && live) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("当前行情快照处于刷新失败或缓存降级状态，已暂停实时联立算仓自动预填；请先点击顶部「刷新」恢复实时行情后再试。");
      return;
    }

    if (ledgerUninitialized && !useSimEquity) {
      invalidateInFlightAndPlan();
      setPlan(null);
      setPlanStale(true);
      setErr("本地持仓账本尚未初始化，请勾选「模拟本金测算」或直接点击「用此金额建模拟账本」。");
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

    let submitRiskPct = optNum(riskPct) ?? 1.0;
    if (sizingDriver === "amount" || sizingDriver === "qty") {
      if (!activeSpec.knownSpec || sizingPreview.unitCost == null) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前合约缺少规格或跨币种汇率，请先补全汇率或按「单笔风险 %」运行一次算仓。");
        return;
      }
      if (sizingPreview.estQty == null || sizingPreview.estQty <= 0) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr(`当前设定仓位小于最小下单单位（${activeSpec.minOrderQty} ${unitLabel}），请调大投入金额或数量。`);
        return;
      }
      if (sizingPreview.exceedsEquity) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前设定仓位所需资金超过账户净值上限，请调小投入金额/数量或提高本金。");
        return;
      }
      if (
        sizingPreview.stop == null ||
        sizingPreview.unitRisk == null ||
        sizingPreview.equity == null ||
        sizingPreview.impliedBudgetRiskPct == null
      ) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr("当前尚未确定有效 30m 止损位（如方向与实时止损同侧），请先切回 30m 视图或按「单笔风险 %」验算获取止损。");
        return;
      }
      if (sizingPreview.exceedsRiskCap) {
        invalidateInFlightAndPlan();
        setPlan(null);
        setPlanStale(true);
        setErr(
          `按当前设定仓位与 30m 止损测算，单笔风险敞口达 ${sizingPreview.impliedBudgetRiskPct.toFixed(2)}%，超过系统 10% 风控上限；请减小仓位或收紧止损。`,
        );
        return;
      }
      const nextStepRiskPct =
        ((sizingPreview.estQty + activeSpec.lotStep) * sizingPreview.unitRisk) /
        (sizingPreview.equity * sizingPreview.slot) *
        100;
      const safeBump = Math.min(1e-3, Math.max(1e-6, (nextStepRiskPct - sizingPreview.impliedBudgetRiskPct) * 0.25));
      submitRiskPct = Math.min(10.0, sizingPreview.impliedBudgetRiskPct + safeBump);
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
        risk_budget_pct: submitRiskPct,
        slot_risk_share: optNum(slotShare) ?? 1.0,
        horizon_days: Math.max(1, Math.round(optNum(horizonDays) ?? 90)),
        has_l1_l2: archiveVerifiedL1L2,
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
      sector_theme: archiveContext?.sectorTheme || dualData?.asset_class || "",
      entry_price: computedPos.entry_price ?? null,
      tactical_stop_30m: computedPos.tactical_stop_30m ?? null,
      structural_stop_daily: computedPos.structural_stop_daily ?? null,
      target_price: computedPos.target_price ?? null,
      quantity: computedPos.total_quantity ?? null,
      fx_to_account: inheritFx,
    });
  };

  const togglePick = (field: PickPriceField) => {
    if (!onRequestPickField) return;
    onRequestPickField(activePickField === field ? null : field);
  };

  const displayRiskValue =
    sizingDriver === "risk"
      ? riskPct
      : sizingPreview.impliedBudgetRiskPct != null
        ? sizingPreview.impliedBudgetRiskPct.toFixed(2)
        : "";
  const displayAmountValue =
    sizingDriver === "amount"
      ? allocAmount
      : sizingPreview.estOutlay != null
        ? String(Math.round(sizingPreview.estOutlay))
        : "";
  const displayQtyValue =
    sizingDriver === "qty"
      ? manualQty
      : sizingPreview.estQty != null
        ? formatQtyDisplay(sizingPreview.estQty, activeSpec.fractional)
        : "";

  return (
    <section className="card flex flex-col gap-3.5 p-4" aria-labelledby={headingId}>
      {/* Compact Planner Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Calculator className="h-4 w-4 text-muted" aria-hidden="true" />
          <h2 id={headingId} className="m-0 text-[13px] font-semibold tracking-tight text-ink">
            四象限终裁 & 30min 双向算仓
          </h2>
          {live ? (
            <StatusBadge tone="info" label="Live 30m 止损锁定" />
          ) : (
            <StatusBadge
              tone="warning"
              label={is30mSnapshot ? "手动推演模式" : `手动推演（当前视图 ${snapInterval}）`}
            />
          )}
          {archiveContext?.exists ? (
            <StatusBadge
              tone={archiveContext.hasL1L2Evidence ? "good" : "neutral"}
              label={`已联动研究档案 (${archiveContext.evidenceCount ?? 0} 条证据)`}
            />
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!ledgerUninitialized && !useSimEquity && resolvedEquity != null ? (
            <span className="tnum font-mono text-[11.5px] text-muted">
              当前账本 {fmtMoney(resolvedEquity, effectiveAcctCurrency)}
            </span>
          ) : null}
          <button
            type="button"
            className="chip"
            aria-pressed={showOverlay}
            onClick={() => setShowOverlay((v) => !v)}
            title="在上方 K 线图预览当前算仓的入场价 / 止损位 / 目标位虚线尺"
          >
            <Eye className="h-3 w-3" aria-hidden="true" />
            <span>图上预览计划尺</span>
          </button>
          {!ledgerUninitialized && !useSimEquity ? (
            <button
              type="button"
              className="chip"
              onClick={() => {
                setUseSimEquity(true);
                markStale();
              }}
            >
              切换模拟本金
            </button>
          ) : null}
        </div>
      </div>

      {/* Uninitialized Ledger / Simulation Equity Bar */}
      {ledgerUninitialized || useSimEquity ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-hairline bg-sunken px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-2.5">
            <label className="inline-flex cursor-pointer items-center gap-1.5 font-medium text-ink">
              <input
                type="checkbox"
                name="use_sim_equity"
                checked={useSimEquity}
                onChange={(e) => {
                  setUseSimEquity(e.target.checked);
                  markStale();
                }}
              />
              <span>模拟本金测算</span>
            </label>
            <span className="text-muted">
              {ledgerUninitialized
                ? "本地账本未初始化，可直接按模拟净值试算，或一键用此金额建立模拟账本记账"
                : "沙盘试算模式（不改动真实账本净值）"}
            </span>
          </div>

          {useSimEquity ? (
            <div className="flex flex-wrap items-center gap-2">
              <label className="inline-flex items-center gap-1.5 text-ink-2">
                <span>净值</span>
                <input
                  className="input h-7 w-28 font-mono"
                  type="number"
                  inputMode="decimal"
                  name="sim_equity"
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
                <span>币种</span>
                <select
                  className="input h-7 w-20 font-mono"
                  name="sim_currency"
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
              {ledgerUninitialized ? (
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={initBusy}
                  onClick={handleQuickInitLedger}
                  title="以当前填写的模拟净值直接初始化本地持仓账本，算仓通过后即可直接记录持仓"
                >
                  <Wallet className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{initBusy ? "建账中…" : "用此金额建模拟账本"}</span>
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {ledgerInitNotice ? (
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-md border border-good/40 bg-good/10 px-3 py-1.5 text-xs text-ink"
        >
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-good" aria-hidden="true" />
          <span>{ledgerInitNotice}</span>
        </div>
      ) : null}

      <form onSubmit={handleCalculate} aria-labelledby={headingId} className="flex flex-col gap-3">
        {/* Row 1: Direction, Entry, 30m Stop, Target & Submit */}
        <div className="grid grid-cols-1 items-end gap-2.5 sm:grid-cols-2 lg:grid-cols-12">
          <div className="field lg:col-span-3">
            <span>方向 & 止损模式</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                className="chip chip-bull flex-1 justify-center py-1"
                aria-pressed={direction === "long"}
                onClick={() => {
                  if (direction !== "long") {
                    setDirection("long");
                    if (!live) {
                      const curSd = optNum(stopDaily);
                      const refEp = optNum(entryPrice) ?? lastClose;
                      if (curSd != null && refEp != null && curSd >= refEp) {
                        setStopDaily(fmtInputPrice(pickValidDailyStop("long", refEp)));
                      }
                    }
                    markStale();
                  }
                }}
              >
                做多 ↑
              </button>
              <button
                type="button"
                className="chip chip-bear flex-1 justify-center py-1"
                aria-pressed={direction === "short"}
                onClick={() => {
                  if (direction !== "short") {
                    setDirection("short");
                    if (!live) {
                      const curSd = optNum(stopDaily);
                      const refEp = optNum(entryPrice) ?? lastClose;
                      if (curSd != null && refEp != null && curSd <= refEp) {
                        setStopDaily(fmtInputPrice(pickValidDailyStop("short", refEp)));
                      }
                    }
                    markStale();
                  }
                }}
              >
                做空 ↓
              </button>
              <button
                type="button"
                className="chip py-1"
                aria-pressed={live}
                onClick={() => {
                  const next = !live;
                  setLive(next);
                  if (next && activePickField === "stop30m") {
                    onRequestPickField?.(null);
                  }
                  markStale();
                }}
                title="开启后强制锁定后端实时 30min 结构止损；关闭可手动推演或点图拾取止损"
              >
                <Lock className="h-3 w-3" aria-hidden="true" />
                <span>Live</span>
              </button>
            </div>
          </div>

          <label className="field lg:col-span-2">
            <span className="flex items-center justify-between">
              <span>计划入场价</span>
              {onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("entry")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "entry" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取入场价"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              name="entry_price"
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
            <span className="flex items-center justify-between">
              <span className="inline-flex items-center gap-1">
                <span>30m 战术止损</span>
                {live ? <Lock className="h-3 w-3 text-accent" aria-hidden="true" /> : null}
              </span>
              {!live && onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("stop30m")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "stop30m" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取手动止损位"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className={`input font-mono ${live ? "cursor-not-allowed opacity-65" : ""}`}
              type="number"
              inputMode="decimal"
              name="tactical_stop_30m"
              step="any"
              disabled={live}
              placeholder={live ? "Live 自动锁定" : is30mSnapshot ? "30m 止损" : "手填 30m 止损"}
              value={stop30m}
              onChange={(e) => {
                setStop30m(e.target.value);
                markStale();
              }}
            />
          </label>

          <label className="field lg:col-span-3">
            <span className="flex items-center justify-between">
              <span>目标价 (≥2.5R)</span>
              {onRequestPickField ? (
                <button
                  type="button"
                  onClick={() => togglePick("target")}
                  className={`inline-flex cursor-pointer items-center gap-0.5 text-[11px] ${
                    activePickField === "target" ? "font-semibold text-accent" : "text-muted hover:text-ink"
                  }`}
                  title="从上方 K 线图点击拾取目标价"
                >
                  <Crosshair className="h-3 w-3" aria-hidden="true" />
                  <span>点图</span>
                </button>
              ) : null}
            </span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              name="target_price"
              step="any"
              placeholder="1Q~1Y 目标位"
              value={targetPrice}
              onChange={(e) => {
                setTargetPrice(e.target.value);
                markStale();
              }}
            />
          </label>

          <div className="lg:col-span-2">
            <button
              type="submit"
              className="btn btn-primary w-full"
              disabled={busy || (live && (snapshotLoading || !dualData))}
              title={
                live && !dualData
                  ? "正在等待当前标的双周期缠论快照就绪（避免错配算仓）"
                  : undefined
              }
            >
              <Calculator className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{busy ? "验算中…" : snapshotLoading && live ? "等快照…" : "运行算仓"}</span>
            </button>
          </div>
        </div>

        {/* Row 2: Bidirectional Sizing Strip (Risk % <-> Outlay Amount <-> Quantity) + Live Preview */}
        <div className="flex flex-col gap-2.5 rounded-md border border-hairline bg-sunken/70 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11.5px]">
            <span className="font-medium text-ink">
              仓位 & 风险双向联动
              <span className="ml-1.5 font-normal text-muted">
                （任填一项，另外两项实时联动换算）
              </span>
            </span>
            <div className="flex flex-wrap items-center gap-1">
              {[0.5, 1, 2].map((rp) => (
                <button
                  key={`r-${rp}`}
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  aria-pressed={sizingDriver === "risk" && Number(riskPct) === rp}
                  onClick={() => applyQuickRiskPct(rp)}
                >
                  {rp}%险
                </button>
              ))}
              <span className="mx-1 text-hairline" aria-hidden="true">
                |
              </span>
              {[0.1, 0.2, 0.3, 0.5].map((ap) => (
                <button
                  key={`a-${ap}`}
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  disabled={!sizingPreview.equity}
                  onClick={() => applyQuickAllocPct(ap)}
                >
                  {Math.round(ap * 100)}%仓
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <label className="field">
              <span className="flex items-center justify-between">
                <span>单笔风险 (%)</span>
                {sizingDriver === "risk" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动反算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "risk" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="risk_budget_pct"
                step="0.1"
                min="0.01"
                max="10"
                placeholder="如 1.0"
                aria-invalid={sizingPreview.exceedsRiskCap ? true : undefined}
                value={displayRiskValue}
                onChange={(e) => {
                  setSizingDriver("risk");
                  setRiskPct(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field">
              <span className="flex items-center justify-between">
                <span>
                  {isMarginAsset ? "占用保证金" : "投入金额"} ({effectiveAcctCurrency})
                </span>
                {sizingDriver === "amount" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动换算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "amount" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="alloc_amount"
                step="any"
                min="0"
                placeholder={sizingPreview.equity ? `最高 ${Math.round(sizingPreview.equity)}` : "输入计划投入金额"}
                aria-invalid={sizingPreview.exceedsEquity ? true : undefined}
                value={displayAmountValue}
                onChange={(e) => {
                  setSizingDriver("amount");
                  setAllocAmount(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field">
              <span className="flex items-center justify-between">
                <span>
                  计划头寸 ({unitLabel}
                  {activeSpec.lotStep > 1 ? ` · 步进 ${activeSpec.lotStep}` : ""})
                </span>
                {sizingDriver === "qty" ? (
                  <span className="text-[10.5px] font-medium text-accent">当前驱动</span>
                ) : (
                  <span className="text-[10.5px] text-muted">自动换算</span>
                )}
              </span>
              <input
                className={`input font-mono ${sizingDriver === "qty" ? "border-accent/60" : ""}`}
                type="number"
                inputMode="decimal"
                name="manual_quantity"
                step={activeSpec.fractional ? "any" : activeSpec.lotStep}
                min="0"
                placeholder={`输入${unitLabel}数`}
                value={displayQtyValue}
                onChange={(e) => {
                  setSizingDriver("qty");
                  setManualQty(e.target.value);
                  markStale();
                }}
              />
            </label>
          </div>

          {/* Instant Plain-Chinese Preview Bar */}
          <div
            role="status"
            aria-live="polite"
            className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2 text-[11.5px] text-ink-2"
          >
            <div className="tnum flex flex-wrap items-center gap-x-3 gap-y-1">
              {sizingPreview.estQty != null && sizingPreview.estQty > 0 ? (
                <>
                  <span>
                    预估仓位：
                    <strong className="font-mono text-ink">
                      {formatQtyDisplay(sizingPreview.estQty, activeSpec.fractional)} {unitLabel}
                    </strong>
                    {sizingPreview.outlayPct != null ? (
                      <span className="text-muted">（占净值 {fmtPct(sizingPreview.outlayPct, 1)}）</span>
                    ) : null}
                  </span>
                  {sizingPreview.estRiskAmt != null && sizingPreview.actualRiskPct != null ? (
                    <span>
                      30m 止损亏：
                      <strong
                        className={`font-mono ${
                          sizingPreview.exceedsRiskCap ? "text-critical" : "text-bear"
                        }`}
                      >
                        -{fmtMoney(sizingPreview.estRiskAmt, effectiveAcctCurrency)} (
                        {fmtPct(sizingPreview.actualRiskPct, 2)})
                      </strong>
                    </span>
                  ) : null}
                  {sizingPreview.rrRatio != null ? (
                    <span>
                      名义盈亏比：
                      <strong
                        className={`font-mono ${
                          sizingPreview.rrRatio >= 2.5 ? "text-good" : "text-warning"
                        }`}
                      >
                        {fmtRatio(sizingPreview.rrRatio)}
                      </strong>
                    </span>
                  ) : null}
                </>
              ) : sizingPreview.belowMinLot ? (
                <span className="text-warning">
                  当前设定不足最小下单单位（{activeSpec.minOrderQty} {unitLabel}），请调大金额或风险预算。
                </span>
              ) : needsCrossCurrencyFx && !sizingPreview.fx ? (
                <span className="text-warning">填写下方跨币种汇率后即可实时预览仓位与止损风险。</span>
              ) : sizingPreview.stop == null ? (
                <span className="text-muted">
                  当前方向暂无同侧 30m 止损预览；点击「运行算仓」由后端校验实时 30m 结构或关闭 Live 手动推演。
                </span>
              ) : (
                <span className="text-muted">填写上方任一仓位或风险参数，即可实时预览股数、资金占比与止损额。</span>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {sizingPreview.cappedByCash ? (
                <span className="text-[11px] font-medium text-warning">
                  已触发现金 1.0x 满仓封顶（实际风险低于预算）
                </span>
              ) : null}
              {sizingPreview.exceedsEquity ? (
                <span className="text-[11px] font-medium text-critical">超出账户净值上限</span>
              ) : null}
              {sizingPreview.exceedsRiskCap ? (
                <span className="text-[11px] font-medium text-critical">单笔风险超过 10% 上限</span>
              ) : null}
              {sizingPreview.rrRatio != null &&
              sizingPreview.rrRatio < 2.5 &&
              sizingPreview.rr25Target != null ? (
                <button
                  type="button"
                  className="chip py-0.5 text-[11px]"
                  onClick={() => {
                    setTargetPrice(fmtInputPrice(sizingPreview.rr25Target));
                    markStale();
                  }}
                >
                  目标价调至 2.5R ({fmtInputPrice(sizingPreview.rr25Target)})
                </button>
              ) : null}
            </div>
          </div>
        </div>

        {/* Cross-Currency Banner surfaced immediately when required */}
        {needsCrossCurrencyFx && inferredQuoteCurrency ? (
          <CrossCurrencyFxBanner
            symbol={symbol}
            quoteCurrency={inferredQuoteCurrency}
            accountCurrency={effectiveAcctCurrency}
            value={fxToAccount}
            onChange={(val) => {
              setFxToAccount(val);
              markStale();
            }}
          />
        ) : null}

        {/* Progressive Disclosure: Secondary Risk Gates, Pair-Trade & Horizon */}
        <details className="disclosure" open={pairTrade || thesisFalsified || hedgeOnly || Number(pendingVerif) > 0}>
          <summary>
            <span>
              高级门控、席位份额与配对参数（{slotShare === "0.5" ? "双寡头 50%" : "唯一真龙头 100%"} · {horizonDays}天
              {hasL1L2 ? " · 具备 L1/L2" : " · 缺 L1/L2"}）
            </span>
          </summary>

          <div className="mt-2.5 grid grid-cols-1 gap-3 pt-1 sm:grid-cols-2 lg:grid-cols-12">
            <label className="field lg:col-span-2">
              <span className="inline-flex items-center gap-1">
                <span>日线结构位 (ZD/ZG)</span>
                {live ? <Lock className="h-3 w-3 text-muted" aria-hidden="true" /> : null}
              </span>
              <input
                className={`input font-mono ${live ? "cursor-not-allowed opacity-65" : ""}`}
                type="number"
                step="any"
                disabled={live}
                placeholder={live ? "Live 自动锁定" : "可选对照"}
                value={stopDaily}
                onChange={(e) => {
                  setStopDaily(e.target.value);
                  markStale();
                }}
              />
            </label>

            <label className="field lg:col-span-3">
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
                <option value="0.5">50% 双寡头共享额度</option>
              </select>
            </label>

            <label className="field lg:col-span-2">
              <span>预计持仓周期 (天)</span>
              <input
                className="input font-mono"
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
                className="input font-mono"
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

            {!needsCrossCurrencyFx ? (
              <label className="field lg:col-span-3">
                <span>汇率折算 (fx_to_account)</span>
                <input
                  className="input font-mono"
                  type="number"
                  step="any"
                  min="0.000001"
                  disabled={isKnownSameCurrency}
                  placeholder={
                    isKnownSameCurrency ? `同币种 (${effectiveAcctCurrency}) 1.0` : "跨币种折算汇率"
                  }
                  value={isKnownSameCurrency ? "" : fxToAccount}
                  onChange={(e) => {
                    setFxToAccount(e.target.value);
                    markStale();
                  }}
                />
              </label>
            ) : null}

            <div className="flex flex-wrap items-center gap-4 lg:col-span-12">
              <span
                className="inline-flex items-center gap-1.5 text-xs text-ink-2"
                title="由本地研报档案库自动核验，需在研报档案中录入至少 1 条 L1 或 L2 证据方可放行"
              >
                <input
                  type="checkbox"
                  checked={hasL1L2}
                  disabled
                  readOnly
                />
                <span>
                  L1/L2 核心证据（{hasL1L2 ? "档案已核验" : "需先在研报档案录入 L1/L2 证据"}）
                </span>
              </span>
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
                <span>同板块多空配对</span>
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
                <span>股票阶段性对冲卖点</span>
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
                <span>核心逻辑已被 L1 证伪</span>
              </label>
            </div>

            {pairTrade ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-ink lg:col-span-12">
                <div className="max-w-2xl">
                  <strong>配对单腿保护：</strong>未完成反向腿研究与缠论确认前默认保留在观察池。
                </div>
                <div className="flex flex-wrap items-center gap-2.5">
                  <input
                    className="input h-7 w-36 font-mono"
                    placeholder="反向腿代码 (如 ETN)"
                    aria-label="已核验反向对冲腿标的代码"
                    value={pairOppositeSym}
                    onChange={(e) => {
                      setPairOppositeSym(e.target.value);
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
                    <span>已独立核验反向腿</span>
                  </label>
                  {pairSameAsPrimary ? (
                    <span className="text-[11px] font-medium text-critical">
                      反向腿不能与主标的 ({symbol}) 相同
                    </span>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
        </details>
      </form>

      {err ? (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
          <div>
            <div className="font-semibold">算仓校验拒绝（Fail-Closed）</div>
            <div className="mt-0.5 text-ink-2">{err}</div>
          </div>
        </div>
      ) : null}

      {plan && planStale ? (
        <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-ink">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>参数已修改，下方结果已锁定失效；请重新点击「运行算仓」后再录入账本。</span>
        </div>
      ) : null}

      {/* Clean Hairline-Divided 3-Column Output (zero box-in-box clutter) */}
      {plan ? (
        <div
          className={`grid grid-cols-1 divide-y divide-hairline border-t border-hairline pt-3.5 xl:grid-cols-12 xl:divide-x xl:divide-y-0 ${
            planStale ? "opacity-60" : ""
          }`}
        >
          {/* Column 1: Four-Quadrant Verdict */}
          <div className="flex flex-col gap-2.5 pb-3.5 xl:col-span-4 xl:pr-4 xl:pb-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="section-title">四象限最终闸门</span>
              {gate ? <GateDecisionBadge decision={gate.decision} actionable={gate.actionable} /> : null}
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge
                tone={isRealLedgerPlan ? "good" : "warning"}
                label={isRealLedgerPlan ? "真实账本净值" : "模拟测算净值"}
              />
              {stopSourceRaw || stopTfRaw ? (
                <StatusBadge
                  tone={stopSourceRaw === "live_30m_chanlun" ? "info" : "neutral"}
                  label={
                    stopSourceRaw === "live_30m_chanlun"
                      ? `Live 30m 止损 (${stopTfRaw || "30m"})`
                      : `手动止损 (${stopTfRaw || "manual"})`
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
            {gate && !gate.actionable ? (
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {isEquityAsset && direction === "short" && !hedgeOnly && !pairTrade ? (
                  <>
                    <button
                      type="button"
                      className="chip py-0.5 text-[11px]"
                      onClick={() => {
                        setDirection("long");
                        markStale();
                      }}
                    >
                      切回做多 (Long)
                    </button>
                    <button
                      type="button"
                      className="chip py-0.5 text-[11px]"
                      onClick={() => {
                        setHedgeOnly(true);
                        markStale();
                      }}
                    >
                      标记为股票对冲卖点
                    </button>
                  </>
                ) : null}
                {pos?.rr_ratio != null &&
                pos.rr_ratio < 2.5 &&
                pos.entry_price != null &&
                pos.tactical_stop_30m != null ? (
                  <button
                    type="button"
                    className="chip py-0.5 text-[11px]"
                    onClick={() => {
                      const dist = Math.abs(pos.entry_price! - pos.tactical_stop_30m!);
                      const t25 =
                        direction === "long"
                          ? pos.entry_price! + dist * 2.5
                          : Math.max(0.0001, pos.entry_price! - dist * 2.5);
                      setTargetPrice(fmtInputPrice(t25));
                      markStale();
                    }}
                  >
                    目标价设为 2.5R (
                    {fmtInputPrice(
                      direction === "long"
                        ? pos.entry_price + Math.abs(pos.entry_price - pos.tactical_stop_30m) * 2.5
                        : Math.max(
                            0.0001,
                            pos.entry_price - Math.abs(pos.entry_price - pos.tactical_stop_30m) * 2.5,
                          ),
                    )}
                    )
                  </button>
                ) : null}
                {!archiveVerifiedL1L2 && onJumpArchive ? (
                  <button
                    type="button"
                    className="chip py-0.5 text-[11px]"
                    onClick={() => onJumpArchive(symbol, archiveContext?.market)}
                  >
                    <BookOpen className="h-3 w-3" aria-hidden="true" />
                    <span>前往研报档案录入 L1/L2 证据</span>
                  </button>
                ) : null}
              </div>
            ) : null}
            {plan.live_warnings && plan.live_warnings.length > 0 ? (
              <div className="mt-1 space-y-1 rounded border border-warning/35 bg-warning/10 p-2 text-[11px] text-ink">
                {plan.live_warnings.map((w, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-warning" aria-hidden="true" />
                    <span>{w}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {/* Column 2: B-Structure & Roll Yield */}
          <div className="flex flex-col gap-2.5 py-3.5 xl:col-span-4 xl:px-4 xl:py-0">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title inline-flex items-center gap-1.5">
                <GitBranch className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                期限结构 & 展期权衡
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
              <div className="rounded border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-[11.5px] text-ink">
                <strong>强现实 vs 弱预期劈叉：</strong>大 B 结构绝不否决做空，已量化展期损耗并推荐合约载体。
              </div>
            ) : null}

            <div className="metric-strip grid-cols-2">
              <div className="metric-cell">
                <span className="metric-label">近月年化 Carry</span>
                <span className="metric-val">
                  {fmtPct(carry?.near_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">全曲线年化展期</span>
                <span className="metric-val">
                  {fmtPct(carry?.curve_annualized_carry_pct ?? carry?.annualized_carry_pct, 2, true)}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">计入 Carry 后 R:R</span>
                <span className="metric-val">
                  {fmtRatio(carry?.carry_adjusted_rr_ratio ?? pos?.carry_adjusted_rr_ratio)}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">近月移仓压力 R:R</span>
                <span className="metric-val">{fmtRatio(carry?.near_carry_stress_rr_ratio)}</span>
              </div>
            </div>

            {carry?.expression_recommendation ? (
              <p className="m-0 text-xs text-ink-2">
                <strong className="text-ink">最优载体：</strong>
                {carry.expression_recommendation}
              </p>
            ) : null}
            {carry?.contract_guidance ? (
              <p className="m-0 text-[11.5px] text-muted">{carry.contract_guidance}</p>
            ) : null}
          </div>

          {/* Column 3: Position Sizing & Tranche Table */}
          <div className="flex flex-col gap-2.5 pt-3.5 xl:col-span-4 xl:pl-4 xl:pt-0">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title">核定仓位 & 40/40/20 分批计划</span>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!canOpenPosition}
                title={
                  !gate?.actionable
                    ? "四象限闸门未放行，禁止直接录入持仓账本"
                    : planStale
                      ? "输入已变更，请重新运行算仓"
                      : "将核定仓位与止损带入持仓账本弹窗"
                }
                onClick={handleOpenLedgerFromPlan}
              >
                <PlusCircle className="h-3.5 w-3.5" aria-hidden="true" />
                <span>录入持仓账本</span>
              </button>
            </div>

            <div className="metric-strip grid-cols-2 sm:grid-cols-3">
              <div className="metric-cell">
                <span className="metric-label">核定总仓位</span>
                <span className="metric-val text-accent">
                  {pos?.total_quantity_str || fmtNum(pos?.total_quantity, 4)}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">实际风险敞口</span>
                <span className="metric-val">
                  {fmtMoney(pos?.actual_risk_amount, pos?.account_currency)} ({fmtPct(pos?.actual_risk_pct, 2)})
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">名义盈亏比</span>
                <span className="metric-val">{fmtRatio(pos?.rr_ratio)}</span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">入场 / 30m止损</span>
                <span className="metric-val">
                  {fmtPrice(pos?.entry_price)} / <span className="text-bear">{fmtPrice(pos?.tactical_stop_30m)}</span>
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">保证金占用</span>
                <span className="metric-val">{fmtPct(pos?.margin_pct_of_equity, 1)}</span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">30m 省噪金额</span>
                <span className="metric-val text-bull">
                  {fmtMoney(pos?.noise_risk_saved_amount, pos?.account_currency)}
                </span>
              </div>
            </div>

            {pos?.oversized_single_lot ? (
              <div className="rounded border border-critical/45 bg-critical/10 px-2.5 py-1.5 text-xs text-ink">
                单手最小合约风险超预算上限，已启动单手超预算保护（核定 0 手或改用迷你合约）。
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
                        <th scope="row">T{tr.tranche_index}</th>
                        <td data-label="权重" className="num">
                          {fmtPct((tr.weight ?? 0) * 100, 0)}
                        </td>
                        <td data-label="数量" className="num">
                          <span>{tr.quantity_str || fmtNum(tr.quantity, 4)}</span>
                          {tr.lots != null && tr.quantity != null && tr.lots !== tr.quantity ? (
                            <span className="ml-1 font-sans text-[10px] text-muted">({fmtNum(tr.lots, 0)}份)</span>
                          ) : null}
                        </td>
                        <td data-label="风险额" className="num">
                          {fmtMoney(tr.risk_amount, pos?.account_currency)}
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
