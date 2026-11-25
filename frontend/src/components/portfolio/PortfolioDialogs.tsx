import { useEffect, useState, type FormEvent } from "react";
import { AlertTriangle } from "lucide-react";
import { api, errorMessage } from "../../api/client";
import type { Direction, OpenPositionBody, Position, Tranche } from "../../api/types";
import { fmtInputPrice, fmtPlanNum, fmtPrice, inferAssetCurrency, optNum, parseSymbolList } from "../../lib/format";
import { formatSymbolWithCode } from "../../lib/symbols";
import { CrossCurrencyFxBanner } from "../ui/CrossCurrencyFxBanner";
import { ModalDialog } from "../ui/ModalDialog";

/* ------------------------------------------------------------------
 * 1. Initialize Portfolio Ledger Dialog
 * ------------------------------------------------------------------ */
export function InitPortfolioDialog({
  open,
  onClose,
  onSuccess,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [equity, setEquity] = useState("1000000");
  const [currency, setCurrency] = useState("USD");
  const [riskPerTrade, setRiskPerTrade] = useState("1.0");
  const [maxTotalRisk, setMaxTotalRisk] = useState("6.0");
  const [maxMacroRisk, setMaxMacroRisk] = useState("3.0");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (open) setErr(null);
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.portfolioInit({
        equity: Number(equity) || 1_000_000,
        base_currency: currency.trim().toUpperCase() || "USD",
        default_risk_per_trade_pct: Number(riskPerTrade) || 1.0,
        max_total_open_risk_pct: Number(maxTotalRisk) || 6.0,
        max_macro_factor_risk_pct: Number(maxMacroRisk) || 3.0,
      });
      onSuccess();
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title="初始化持仓账本"
      subtitle="创建本地 portfolio/ SQLite 风险预算账本（单一板块龙头独占制）"
    >
      <form onSubmit={submit} className="flex flex-col gap-4 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>初始账户净值</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              min="100"
              required
              autoFocus
              value={equity}
              onChange={(e) => setEquity(e.target.value)}
            />
          </label>
          <label className="field">
            <span>记账本位币</span>
            <select className="input" value={currency} onChange={(e) => setCurrency(e.target.value)}>
              <option value="USD">USD（美元）</option>
              <option value="CNY">CNY（人民币）</option>
              <option value="HKD">HKD（港币）</option>
            </select>
          </label>
          <label className="field">
            <span>单笔 / 单板块风险上限 (%)</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              min="0.01"
              max="10"
              required
              value={riskPerTrade}
              onChange={(e) => setRiskPerTrade(e.target.value)}
            />
          </label>
          <label className="field">
            <span>总敞口风险上限 (%)</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              min="0.5"
              max="30"
              required
              value={maxTotalRisk}
              onChange={(e) => setMaxTotalRisk(e.target.value)}
            />
          </label>
          <label className="field sm:col-span-2">
            <span>单一宏大因子（美元/利率等）叠加风险上限 (%)</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              min="0.5"
              max="20"
              required
              value={maxMacroRisk}
              onChange={(e) => setMaxMacroRisk(e.target.value)}
            />
          </label>
        </div>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "正在初始化…" : "确认初始化账本"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 2. Open Position Dialog (sole_leader / co_leader)
 * ------------------------------------------------------------------ */
const GENERIC_SECTOR_THEMES = new Set([
  "临时自选",
  "adhoc",
  "ad-hoc",
  "custom",
  "us_stock_or_etf",
  "a_share",
  "cn_future",
  "commodity",
  "fx",
  "crypto",
  "default",
  "watchlist",
  "未分类",
]);

export function isGenericSectorTheme(theme?: string | null): boolean {
  const norm = (theme || "").trim().toLowerCase();
  return norm.length > 0 && GENERIC_SECTOR_THEMES.has(norm);
}

export interface OpenPositionSeed {
  symbol?: string;
  direction?: Direction;
  sector_theme?: string;
  asset_class?: string | null;
  leader_role?: "sole_leader" | "co_leader";
  co_leader_reason?: string | null;
  entry_price?: number | null;
  tactical_stop_30m?: number | null;
  structural_stop_daily?: number | null;
  target_price?: number | null;
  quantity?: number | null;
  first_tranche_qty?: number | null;
  first_tranche_weight?: number | null;
  full_plan_qty?: number | null;
  tranches?: Tranche[];
  fx_to_account?: number | null;
  macro_factors?: string[];
  exit_red_lines?: string[];
  notes?: string | null;
}

export interface OpenPositionReceipt {
  symbol: string;
  direction: Direction;
  quantity: number;
  entry_price: number;
  sector_theme: string;
  asset_class?: string | null;
  position_id?: string | null;
  archive_synced?: boolean;
  tranche_label?: string | null;
  opened_at_ms: number;
}

export function canVoidMistakenPosition(pos: Position | null | undefined): boolean {
  if (!pos || (pos.status && pos.status !== "open")) return false;
  const tranches = Array.isArray(pos.tranches) ? pos.tranches : [];
  if (tranches.length > 1) return false;
  const partialPnl = typeof pos.partial_realized_pnl === "number" ? pos.partial_realized_pnl : 0;
  if (Math.abs(partialPnl) > 1e-9) return false;
  if (typeof pos.initial_quantity === "number" && Math.abs(pos.quantity - pos.initial_quantity) > 1e-9) {
    return false;
  }
  return true;
}

export function OpenPositionDialog({
  open,
  onClose,
  onSuccess,
  seed,
  accountCurrency,
  firstTrancheQty,
  firstTrancheWeight,
  fullPlanQty,
  tranches,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: (receipt?: OpenPositionReceipt) => void;
  seed?: OpenPositionSeed;
  accountCurrency?: string | null;
  firstTrancheQty?: number | null;
  firstTrancheWeight?: number | null;
  fullPlanQty?: number | null;
  tranches?: Tranche[];
}) {
  const [symbol, setSymbol] = useState("");
  const [direction, setDirection] = useState<Direction>("long");
  const [sectorTheme, setSectorTheme] = useState("");
  const [genericThemeHint, setGenericThemeHint] = useState<string | null>(null);
  const [leaderRole, setLeaderRole] = useState<"sole_leader" | "co_leader">("sole_leader");
  const [coLeaderReason, setCoLeaderReason] = useState("");
  const [entryPrice, setEntryPrice] = useState("");
  const [stop30m, setStop30m] = useState("");
  const [stopDaily, setStopDaily] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [qtyPreset, setQtyPreset] = useState<"t1" | "full" | "custom">("full");
  const [fxToAccount, setFxToAccount] = useState("");
  const [macroFactors, setMacroFactors] = useState("");
  const [redLines, setRedLines] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const resolvedTranches = tranches ?? seed?.tranches ?? [];
  const resolvedFullQty = fullPlanQty ?? seed?.full_plan_qty ?? seed?.quantity ?? null;
  const resolvedFirstQty =
    firstTrancheQty ?? seed?.first_tranche_qty ?? (resolvedTranches[0]?.quantity ?? null);
  const resolvedFirstWeight =
    firstTrancheWeight ?? seed?.first_tranche_weight ?? (resolvedTranches[0]?.weight ?? null);
  const hasSplitTranches = Boolean(
    resolvedFirstQty != null &&
      resolvedFirstQty > 0 &&
      resolvedFullQty != null &&
      resolvedFullQty > 0 &&
      resolvedFirstQty < resolvedFullQty - 1e-9,
  );
  const firstTranchePct = hasSplitTranches
    ? resolvedFirstWeight != null && resolvedFirstWeight > 0
      ? Math.round(resolvedFirstWeight * 100)
      : Math.round(((resolvedFirstQty ?? 0) / (resolvedFullQty ?? 1)) * 100)
    : null;

  useEffect(() => {
    if (!open) return;
    setSymbol(seed?.symbol ?? "");
    setDirection(seed?.direction ?? "long");
    const rawTheme = (seed?.sector_theme ?? "").trim();
    if (isGenericSectorTheme(rawTheme)) {
      setSectorTheme("");
      setGenericThemeHint(rawTheme);
    } else {
      setSectorTheme(rawTheme);
      setGenericThemeHint(null);
    }
    setLeaderRole(seed?.leader_role === "co_leader" ? "co_leader" : "sole_leader");
    setCoLeaderReason(seed?.co_leader_reason ?? "");
    setEntryPrice(seed?.entry_price != null ? fmtPlanNum(seed.entry_price, 6) : "");
    setStop30m(seed?.tactical_stop_30m != null ? fmtPlanNum(seed.tactical_stop_30m, 6) : "");
    setStopDaily(seed?.structural_stop_daily != null ? fmtPlanNum(seed.structural_stop_daily, 6) : "");
    setTargetPrice(seed?.target_price != null ? fmtPlanNum(seed.target_price, 6) : "");
    if (hasSplitTranches && resolvedFirstQty != null) {
      setQuantity(fmtPlanNum(resolvedFirstQty, 6));
      setQtyPreset("t1");
    } else if (resolvedFullQty != null && resolvedFullQty > 0) {
      setQuantity(fmtPlanNum(resolvedFullQty, 6));
      setQtyPreset("full");
    } else {
      setQuantity("");
      setQtyPreset("custom");
    }
    setFxToAccount(seed?.fx_to_account != null && seed.fx_to_account > 0 ? fmtPlanNum(seed.fx_to_account, 6) : "");
    setMacroFactors((seed?.macro_factors ?? []).join(", "));
    setRedLines((seed?.exit_red_lines ?? []).join("\n"));
    setNotes(seed?.notes ?? "");
    setErr(null);
  }, [open, seed, hasSplitTranches, resolvedFirstQty, resolvedFullQty]);

  const acctCcy = (accountCurrency || "USD").toUpperCase();
  const quoteCcy = inferAssetCurrency(symbol);
  const needsCrossFx = Boolean(quoteCcy && quoteCcy !== acctCcy);
  const currentThemeIsGeneric = isGenericSectorTheme(sectorTheme);
  const genericThemeLabel = currentThemeIsGeneric
    ? sectorTheme.trim()
    : !sectorTheme.trim() && genericThemeHint
      ? genericThemeHint
      : null;

  const handleSelectQtyPreset = (mode: "t1" | "full") => {
    if (mode === "t1" && resolvedFirstQty != null && resolvedFirstQty > 0) {
      setQuantity(fmtPlanNum(resolvedFirstQty, 6));
      setQtyPreset("t1");
    } else if (mode === "full" && resolvedFullQty != null && resolvedFullQty > 0) {
      setQuantity(fmtPlanNum(resolvedFullQty, 6));
      setQtyPreset("full");
    }
  };

  const handleQuantityChange = (val: string) => {
    setQuantity(val);
    if (!hasSplitTranches) {
      setQtyPreset("custom");
      return;
    }
    const parsed = optNum(val);
    if (parsed != null && resolvedFirstQty != null && Math.abs(parsed - resolvedFirstQty) <= 1e-6) {
      setQtyPreset("t1");
    } else if (parsed != null && resolvedFullQty != null && Math.abs(parsed - resolvedFullQty) <= 1e-6) {
      setQtyPreset("full");
    } else {
      setQtyPreset("custom");
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ep = optNum(entryPrice);
    const s30 = optNum(stop30m);
    const qty = optNum(quantity);
    if (!symbol.trim() || !sectorTheme.trim() || ep === undefined || s30 === undefined || qty === undefined) {
      setErr("请填写标的代码、所属主驱动板块、入场价、30min 战术止损与数量");
      return;
    }
    if (isGenericSectorTheme(sectorTheme)) {
      setErr(
        `所属板块不能使用「${sectorTheme.trim()}」等通用占位名，请填写具体板块主题名称，以免与其他无关标的在单一板块龙头独占制下互相锁死。`,
      );
      return;
    }
    const fxVal = optNum(fxToAccount);
    if (fxToAccount.trim() !== "" && (fxVal === undefined || fxVal <= 0)) {
      setErr("汇率折算 (fx_to_account) 必须为大于 0 的有效数值");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body: OpenPositionBody = {
        symbol: symbol.trim().toUpperCase(),
        direction,
        sector_theme: sectorTheme.trim(),
        entry_price: ep,
        tactical_stop_30m: s30,
        quantity: qty,
        leader_role: leaderRole,
      };
      const sd = optNum(stopDaily);
      const tp = optNum(targetPrice);
      if (sd !== undefined) body.structural_stop_daily = sd;
      if (tp !== undefined) body.target_price = tp;
      if (fxVal !== undefined) body.fx_to_account = fxVal;
      if (leaderRole === "co_leader" && coLeaderReason.trim()) {
        body.co_leader_reason = coLeaderReason.trim();
      }
      const macros = parseSymbolList(macroFactors);
      if (macros.length > 0) body.macro_factors = macros;
      const lines = redLines
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      if (lines.length > 0) body.exit_red_lines = lines;
      if (notes.trim()) body.notes = notes.trim();

      const rawRes = await api.openPosition(body);
      const resObj =
        rawRes && typeof rawRes === "object" ? (rawRes as Record<string, unknown>) : null;
      const posObj =
        resObj?.position && typeof resObj.position === "object"
          ? (resObj.position as Record<string, unknown>)
          : null;
      const archiveSyncObj =
        resObj?.archive_status_sync && typeof resObj.archive_status_sync === "object"
          ? (resObj.archive_status_sync as Record<string, unknown>)
          : null;
      const trancheLabel = hasSplitTranches
        ? qtyPreset === "t1"
          ? "T1 首仓"
          : qtyPreset === "full"
            ? "核定全仓"
            : null
        : null;
      const receipt: OpenPositionReceipt = {
        symbol: typeof posObj?.symbol === "string" ? posObj.symbol : body.symbol,
        direction: body.direction,
        quantity: body.quantity,
        entry_price: body.entry_price,
        sector_theme: body.sector_theme,
        asset_class:
          typeof posObj?.asset_class === "string" ? posObj.asset_class : (seed?.asset_class ?? null),
        position_id: typeof posObj?.position_id === "string" ? posObj.position_id : null,
        archive_synced: Boolean(archiveSyncObj?.synced),
        tranche_label: trancheLabel,
        opened_at_ms: Date.now(),
      };
      onSuccess(receipt);
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title="录入新持仓头寸"
      subtitle="单一板块仅允许 1 只真龙头（或双寡头共享单板块风险额度）"
    >
      <form onSubmit={submit} className="flex max-h-[76dvh] flex-col gap-3.5 overflow-y-auto p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field">
            <span>标的代码 *</span>
            <input
              className="input font-mono"
              required
              autoFocus={!seed?.symbol}
              placeholder="如 PWR / CU0"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
            />
          </label>
          <label className="field">
            <span>多空方向 *</span>
            <select
              className="input"
              value={direction}
              onChange={(e) => setDirection(e.target.value as Direction)}
            >
              <option value="long">做多 (Long)</option>
              <option value="short">做空 (Short)</option>
            </select>
          </label>
          <label className="field">
            <span>龙头角色 *</span>
            <select
              className="input"
              value={leaderRole}
              onChange={(e) => setLeaderRole(e.target.value as "sole_leader" | "co_leader")}
            >
              <option value="sole_leader">唯一真龙头 (sole_leader)</option>
              <option value="co_leader">双寡头并列 (co_leader)</option>
            </select>
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field sm:col-span-2">
            <span>所属板块 / 主驱动主题 *</span>
            <input
              className={`input ${genericThemeLabel ? "border-warning/70" : ""}`}
              required
              autoFocus={Boolean(
                seed?.symbol && (!seed?.sector_theme || isGenericSectorTheme(seed.sector_theme)),
              )}
              placeholder="如 北美电网基建 / 全球铜供需劈叉"
              value={sectorTheme}
              onChange={(e) => setSectorTheme(e.target.value)}
            />
          </label>
          <label className="field">
            <span>建仓数量（股 / 手 / 单位）*</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              min="0.0001"
              required
              value={quantity}
              onChange={(e) => handleQuantityChange(e.target.value)}
            />
          </label>
        </div>

        {hasSplitTranches && resolvedFirstQty != null && resolvedFullQty != null ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-hairline bg-sunken px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium text-ink-2">建仓仓位快切：</span>
              <div className="inline-flex items-center gap-1.5" role="group" aria-label="首仓与全仓切换">
                <button
                  type="button"
                  className="chip py-0.5 text-[11.5px] font-medium"
                  aria-pressed={qtyPreset === "t1"}
                  onClick={() => handleSelectQtyPreset("t1")}
                >
                  T1 首仓 ({firstTranchePct}% · {fmtPlanNum(resolvedFirstQty, 4)})
                </button>
                <button
                  type="button"
                  className="chip py-0.5 text-[11.5px] font-medium"
                  aria-pressed={qtyPreset === "full"}
                  onClick={() => handleSelectQtyPreset("full")}
                >
                  核定全仓 (100% · {fmtPlanNum(resolvedFullQty, 4)})
                </button>
              </div>
            </div>
            <span className="text-[11px] text-muted">
              默认按分批计划预填 T1 首仓，浮盈确认后可在哨兵总控继续顺势加仓
            </span>
          </div>
        ) : null}

        {genericThemeLabel ? (
          <div
            role="status"
            className="flex items-start gap-2 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-xs text-ink"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
            <span>
              <strong>板块防撞名提示：</strong>当前所属板块为「{genericThemeLabel}」，为避免与其他无关标的在单一板块龙头独占制下互相锁死，请填写具体板块主题名称（如「北美电网基建」「全球铜供需劈叉」）。
            </span>
          </div>
        ) : null}

        {needsCrossFx && quoteCcy ? (
          <CrossCurrencyFxBanner
            symbol={symbol}
            quoteCurrency={quoteCcy}
            accountCurrency={acctCcy}
            value={fxToAccount}
            onChange={setFxToAccount}
            required
            enabled={open}
          />
        ) : null}

        {leaderRole === "co_leader" ? (
          <label className="field">
            <span>双寡头并列理由 *</span>
            <input
              className="input"
              required
              placeholder="说明为何第一第二不明显需共享该板块风险额度"
              value={coLeaderReason}
              onChange={(e) => setCoLeaderReason(e.target.value)}
            />
          </label>
        ) : null}

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="field">
            <span>入场均价 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              autoFocus={Boolean(
                seed?.symbol &&
                  seed?.sector_theme &&
                  !isGenericSectorTheme(seed.sector_theme) &&
                  seed?.entry_price == null,
              )}
              value={entryPrice}
              onChange={(e) => setEntryPrice(e.target.value)}
            />
          </label>
          <label className="field">
            <span>30min 战术止损 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              value={stop30m}
              onChange={(e) => setStop30m(e.target.value)}
            />
          </label>
          <label className="field">
            <span>日线结构位</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              placeholder="可选 ZD/ZG"
              value={stopDaily}
              onChange={(e) => setStopDaily(e.target.value)}
            />
          </label>
          <label className="field">
            <span>目标位</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              placeholder="可选"
              value={targetPrice}
              onChange={(e) => setTargetPrice(e.target.value)}
            />
          </label>
        </div>

        <details className="disclosure">
          <summary>
            <span>补充撤离红线、跨板块宏大因子与交易备注（可选）</span>
          </summary>
          <div className="mt-2.5 flex flex-col gap-3">
            {!needsCrossFx ? (
              <label className="field">
                <span>汇率折算 (fx_to_account，同币种可留空)</span>
                <input
                  className="input font-mono"
                  type="number"
                  step="any"
                  min="0.000001"
                  value={fxToAccount}
                  onChange={(e) => setFxToAccount(e.target.value)}
                />
              </label>
            ) : null}
            <label className="field">
              <span>跨板块宏大因子标签（逗号分隔）</span>
              <input
                className="input"
                placeholder="如 USD_SHORT, AI_CAPEX, CN_REFLATION"
                value={macroFactors}
                onChange={(e) => setMacroFactors(e.target.value)}
              />
            </label>
            <label className="field">
              <span>撤离红线（每行一条）</span>
              <textarea
                className="input"
                rows={2}
                placeholder="如：30min 跌破三买中枢 ZG&#10;显性库存连续 3 周超季节性累库"
                value={redLines}
                onChange={(e) => setRedLines(e.target.value)}
              />
            </label>
            <label className="field">
              <span>交易备注</span>
              <input
                className="input"
                placeholder="合约月份或分批执行说明"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </label>
          </div>
        </details>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "正在写入…" : "确认录入持仓"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 3. Trail Stop Dialog (only tightens, never loosens)
 * ------------------------------------------------------------------ */
export function TrailStopDialog({
  open,
  onClose,
  onSuccess,
  positionId,
  symbol,
  currentStop30m,
  suggestedStop30m,
  currentPrice,
  rationale,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
  positionId: string;
  symbol: string;
  currentStop30m?: number | null;
  suggestedStop30m?: number | null;
  currentPrice?: number | null;
  rationale?: string;
}) {
  const [newStop, setNewStop] = useState("");
  const [newDailyStop, setNewDailyStop] = useState("");
  const [reason, setReason] = useState("trail_stop_30m_structure");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setNewStop(
      suggestedStop30m != null
        ? fmtInputPrice(suggestedStop30m)
        : currentStop30m != null
          ? fmtInputPrice(currentStop30m)
          : "",
    );
    setNewDailyStop("");
    setReason(rationale || "30min 结构上移推保护止损");
    setErr(null);
  }, [open, suggestedStop30m, currentStop30m, rationale]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const s = optNum(newStop);
    if (s === undefined) {
      setErr("请输入有效的新 30min 止损价");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.trailStop(positionId, {
        new_stop_30m: s,
        new_structural_stop_daily: optNum(newDailyStop),
        current_price: currentPrice ?? undefined,
        reason: reason.trim() || "trail_stop",
      });
      onSuccess();
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`上移/收紧止损 · ${formatSymbolWithCode(symbol)}`}
      subtitle="移动止损只紧不松；越过保本位后自动释放该板块已占用风险额度"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="metric-strip grid-cols-3">
          <div className="metric-cell">
            <span className="metric-label">当前市价</span>
            <span className="metric-val">{fmtPrice(currentPrice)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">原 30m 止损</span>
            <span className="metric-val">{fmtPrice(currentStop30m)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">哨兵建议止损</span>
            <span className="metric-val text-bull">{fmtPrice(suggestedStop30m)}</span>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>新 30min 战术止损价 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              autoFocus
              value={newStop}
              onChange={(e) => setNewStop(e.target.value)}
            />
          </label>
          <label className="field">
            <span>新日线结构位（可选）</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              placeholder="不改留空"
              value={newDailyStop}
              onChange={(e) => setNewDailyStop(e.target.value)}
            />
          </label>
        </div>

        <label className="field">
          <span>调止损依据</span>
          <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "正在更新…" : "确认收紧止损"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 4. Trim Position Dialog
 * ------------------------------------------------------------------ */
export function TrimPositionDialog({
  open,
  onClose,
  onSuccess,
  position,
  lastPrice,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
  position: Position | null;
  lastPrice?: number | null;
}) {
  const [trimPrice, setTrimPrice] = useState("");
  const [trimQty, setTrimQty] = useState("");
  const [reason, setReason] = useState("指标松动/分批止盈减仓");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !position) return;
    const entryRef = position.avg_entry_price ?? position.entry_price;
    setTrimPrice(lastPrice != null ? fmtInputPrice(lastPrice) : fmtPlanNum(entryRef, 6));
    setTrimQty(String(Math.max(1, Math.floor(position.quantity / 2))));
    setReason("指标松动/分批止盈减仓");
    setErr(null);
  }, [open, position, lastPrice]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!position) return;
    const p = optNum(trimPrice);
    const q = optNum(trimQty);
    if (p === undefined || q === undefined) {
      setErr("请输入有效的减仓价格与数量");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.trimPosition(position.position_id, {
        trim_price: p,
        trim_quantity: q,
        reason: reason.trim() || "partial_take_profit",
      });
      onSuccess();
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`分批减仓 · ${formatSymbolWithCode(position?.symbol)}`}
      subtitle={`当前持仓数量：${position?.quantity ?? "—"} · 入场均价：${fmtPrice(position?.avg_entry_price ?? position?.entry_price)}`}
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>减仓成交价 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              autoFocus
              value={trimPrice}
              onChange={(e) => setTrimPrice(e.target.value)}
            />
          </label>
          <label className="field">
            <span>减仓数量 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              value={trimQty}
              onChange={(e) => setTrimQty(e.target.value)}
            />
          </label>
        </div>

        <label className="field">
          <span>减仓原因</span>
          <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "正在提交…" : "确认减仓"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 5. Atomic Leader Rotation Dialog (/api/portfolio/positions/{id}/rotate)
 * ------------------------------------------------------------------ */
export function RotatePositionDialog({
  open,
  onClose,
  onSuccess,
  position,
  lastPrice,
  accountCurrency,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
  position: Position | null;
  lastPrice?: number | null;
  accountCurrency?: string | null;
}) {
  const [exitPrice, setExitPrice] = useState("");
  const [exitReason, setExitReason] = useState("原龙头动能弱化，原子切换至同板块新引领龙头");
  const [newSymbol, setNewSymbol] = useState("");
  const [newDirection, setNewDirection] = useState<Direction>("long");
  const [sectorTheme, setSectorTheme] = useState("");
  const [newEntry, setNewEntry] = useState("");
  const [newStop30m, setNewStop30m] = useState("");
  const [newStopDaily, setNewStopDaily] = useState("");
  const [newTarget, setNewTarget] = useState("");
  const [newQty, setNewQty] = useState("");
  const [fxToAccount, setFxToAccount] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !position) return;
    const entryRef = position.avg_entry_price ?? position.entry_price;
    setExitPrice(lastPrice != null ? fmtInputPrice(lastPrice) : fmtPlanNum(entryRef, 6));
    setExitReason("原龙头动能弱化，原子切换至同板块新引领龙头");
    setNewSymbol("");
    setNewDirection(position.direction || "long");
    setSectorTheme(position.sector_theme || "");
    setNewEntry("");
    setNewStop30m("");
    setNewStopDaily("");
    setNewTarget("");
    setNewQty("");
    setFxToAccount("");
    setErr(null);
  }, [open, position, lastPrice]);

  const acctCcy = (accountCurrency || "USD").toUpperCase();
  const quoteCcy = inferAssetCurrency(newSymbol);
  const needsCrossFx = Boolean(quoteCcy && quoteCcy !== acctCcy);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!position) return;
    const epOut = optNum(exitPrice);
    const epIn = optNum(newEntry);
    const s30 = optNum(newStop30m);
    const qty = optNum(newQty);
    if (
      epOut === undefined ||
      !newSymbol.trim() ||
      !sectorTheme.trim() ||
      epIn === undefined ||
      s30 === undefined ||
      qty === undefined
    ) {
      setErr("请完整填写旧龙头平仓价以及新龙头代码、入场价、30m 止损与数量");
      return;
    }
    const fxVal = optNum(fxToAccount);
    if (fxToAccount.trim() !== "" && (fxVal === undefined || fxVal <= 0)) {
      setErr("汇率折算必须为大于 0 的有效数值");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.rotatePosition(position.position_id, {
        exit_price: epOut,
        exit_reason: exitReason.trim() || "leader_rotation",
        exit_category: "leader_rotation",
        new_position: {
          symbol: newSymbol.trim().toUpperCase(),
          direction: newDirection,
          sector_theme: sectorTheme.trim(),
          entry_price: epIn,
          tactical_stop_30m: s30,
          quantity: qty,
          structural_stop_daily: optNum(newStopDaily),
          target_price: optNum(newTarget),
          fx_to_account: fxVal,
          leader_role: "sole_leader",
          macro_factors: position.macro_factors,
        },
      });
      onSuccess();
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`原子龙头换仓 · ${formatSymbolWithCode(position?.symbol)} → 新龙头`}
      subtitle="单事务结清旧龙头释放板块额度，并同步校验录入新龙头（严禁同板块叠加）"
    >
      <form onSubmit={submit} className="flex max-h-[76dvh] flex-col gap-3.5 overflow-y-auto p-5">
        <div className="rounded-md border border-hairline bg-sunken p-3">
          <div className="section-title mb-2">第 1 步：结清原龙头（{formatSymbolWithCode(position?.symbol)}）</div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="field">
              <span>原龙头平仓价 *</span>
              <input
                className="input bg-surface font-mono"
                type="number"
                step="any"
                required
                value={exitPrice}
                onChange={(e) => setExitPrice(e.target.value)}
              />
            </label>
            <label className="field sm:col-span-2">
              <span>切换原因 *</span>
              <input
                className="input bg-surface"
                required
                value={exitReason}
                onChange={(e) => setExitReason(e.target.value)}
              />
            </label>
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <div className="section-title">第 2 步：接棒新龙头头寸</div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="field">
              <span>新龙头代码 *</span>
              <input
                className="input font-mono"
                required
                autoFocus
                placeholder="如 GEV / HG=F"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
              />
            </label>
            <label className="field">
              <span>方向 *</span>
              <select
                className="input"
                value={newDirection}
                onChange={(e) => setNewDirection(e.target.value as Direction)}
              >
                <option value="long">做多 (Long)</option>
                <option value="short">做空 (Short)</option>
              </select>
            </label>
            <label className="field">
              <span>继承/归属板块 *</span>
              <input
                className="input"
                required
                value={sectorTheme}
                onChange={(e) => setSectorTheme(e.target.value)}
              />
            </label>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <label className="field">
              <span>新入场价 *</span>
              <input
                className="input font-mono"
                type="number"
                step="any"
                required
                value={newEntry}
                onChange={(e) => setNewEntry(e.target.value)}
              />
            </label>
            <label className="field">
              <span>30m 战术止损 *</span>
              <input
                className="input font-mono"
                type="number"
                step="any"
                required
                value={newStop30m}
                onChange={(e) => setNewStop30m(e.target.value)}
              />
            </label>
            <label className="field">
              <span>建仓数量 *</span>
              <input
                className="input font-mono"
                type="number"
                step="any"
                min="0.0001"
                required
                value={newQty}
                onChange={(e) => setNewQty(e.target.value)}
              />
            </label>
            <label className="field">
              <span>目标价（可选）</span>
              <input
                className="input font-mono"
                type="number"
                step="any"
                value={newTarget}
                onChange={(e) => setNewTarget(e.target.value)}
              />
            </label>
          </div>

          {needsCrossFx && quoteCcy ? (
            <CrossCurrencyFxBanner
              symbol={newSymbol}
              quoteCurrency={quoteCcy}
              accountCurrency={acctCcy}
              value={fxToAccount}
              onChange={setFxToAccount}
              required
              enabled={open}
            />
          ) : null}
        </div>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "正在原子换仓…" : "确认原子切换龙头"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 6. Close Position Confirmation Dialog
 * ------------------------------------------------------------------ */
export function ClosePositionDialog({
  open,
  onClose,
  onSuccess,
  position,
  lastPrice,
  defaultReason,
  onSwitchToVoid,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
  position: Position | null;
  lastPrice?: number | null;
  defaultReason?: string;
  onSwitchToVoid?: (pos: Position) => void;
}) {
  const [exitPrice, setExitPrice] = useState("");
  const [exitReason, setExitReason] = useState("");
  const [exitCategory, setExitCategory] = useState("red_line_exit");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !position) return;
    const entryRef = position.avg_entry_price ?? position.entry_price;
    setExitPrice(lastPrice != null ? fmtInputPrice(lastPrice) : fmtPlanNum(entryRef, 6));
    setExitReason(defaultReason || "触发撤离红线或逻辑松动果断平仓");
    setExitCategory("red_line_exit");
    setErr(null);
  }, [open, position, lastPrice, defaultReason]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!position) return;
    const p = optNum(exitPrice);
    if (p === undefined || !exitReason.trim()) {
      setErr("请填写平仓价格与撤离原因");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.closePosition(position.position_id, {
        exit_price: p,
        exit_reason: exitReason.trim(),
        exit_category: exitCategory,
      });
      onSuccess();
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`清仓了结头寸 · ${formatSymbolWithCode(position?.symbol)}`}
      subtitle="逻辑或关键指标松动绝不硬扛，结算盈亏并释放板块龙头席位"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        {canVoidMistakenPosition(position) && onSwitchToVoid && position ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-ink">
            <span>
              <strong>不小心填错入场价或手数了？</strong>请勿按市价平仓（会误扣账本净值），可无损作废后重填。
            </span>
            <button
              type="button"
              className="chip py-0.5 text-[11px] font-medium"
              onClick={() => {
                onClose();
                onSwitchToVoid(position);
              }}
            >
              改用误录作废 / 重填
            </button>
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>平仓结算价 *</span>
            <input
              className="input font-mono"
              type="number"
              step="any"
              required
              value={exitPrice}
              onChange={(e) => setExitPrice(e.target.value)}
            />
          </label>
          <label className="field">
            <span>平仓归因类别</span>
            <select className="input" value={exitCategory} onChange={(e) => setExitCategory(e.target.value)}>
              <option value="red_line_exit">触发撤离红线 (red_line_exit)</option>
              <option value="stop_loss_30m">30min 战术止损 (stop_loss_30m)</option>
              <option value="take_profit">目标位止盈 (take_profit)</option>
              <option value="thesis_loosening">逻辑松动主动撤离 (thesis_loosening)</option>
              <option value="manual">手动平仓 (manual)</option>
            </select>
          </label>
        </div>

        <label className="field">
          <span>平仓复盘原因 *</span>
          <input
            className="input"
            required
            value={exitReason}
            onChange={(e) => setExitReason(e.target.value)}
          />
        </label>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" autoFocus onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-danger" disabled={busy}>
            {busy ? "正在平仓…" : "确认撤离平仓"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * 7. Void Mistaken Entry Dialog (/api/portfolio/positions/{id}/void)
 * ------------------------------------------------------------------ */
export function VoidPositionDialog({
  open,
  onClose,
  onSuccess,
  onReopenSeed,
  position,
}: {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
  onReopenSeed?: (seed: OpenPositionSeed) => void;
  position: Position | null;
}) {
  const [reason, setReason] = useState("误录价格/手数，无损作废重填");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setReason("误录价格/手数，无损作废重填");
    setErr(null);
  }, [open, position]);

  const executeVoid = async (reopenForm: boolean) => {
    if (!position) return;
    setBusy(true);
    setErr(null);
    try {
      const seedSnapshot: OpenPositionSeed = {
        symbol: position.symbol,
        direction: position.direction,
        sector_theme: position.sector_theme,
        leader_role: position.leader_role === "co_leader" ? "co_leader" : "sole_leader",
        co_leader_reason: position.co_leader_reason ?? "",
        entry_price: null,
        tactical_stop_30m: null,
        structural_stop_daily: position.structural_stop_daily ?? null,
        target_price: position.target_price ?? null,
        quantity: position.quantity,
        fx_to_account: position.fx_to_account ?? null,
        macro_factors: position.macro_factors ?? [],
        exit_red_lines: (position.exit_red_lines ?? []).map((x) => String(x)),
        notes: position.notes ?? "",
      };
      await api.voidPosition(position.position_id, {
        reason: reason.trim() || "误录作废",
      });
      onSuccess();
      onClose();
      if (reopenForm && onReopenSeed) {
        onReopenSeed(seedSnapshot);
      }
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  const entryRef = position?.avg_entry_price ?? position?.entry_price;

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`误录无损作废 · ${formatSymbolWithCode(position?.symbol)}`}
      subtitle="仅限未发生加减仓的初始录入：不结算虚假盈亏、不扣减账本净值、不污染历史胜率"
    >
      <div className="flex flex-col gap-3.5 p-5">
        <div className="metric-strip grid-cols-3">
          <div className="metric-cell">
            <span className="metric-label">当前误填入场价</span>
            <span className="metric-val">{fmtPrice(entryRef)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">当前 30m 止损</span>
            <span className="metric-val">{fmtPrice(position?.tactical_stop_30m)}</span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">录入数量</span>
            <span className="metric-val">{position?.quantity ?? "—"}</span>
          </div>
        </div>

        <label className="field">
          <span>作废审计备注</span>
          <input
            className="input"
            name="void_reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex flex-wrap justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" autoFocus onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={() => void executeVoid(false)}
          >
            {busy ? "处理中…" : "仅作废移除"}
          </button>
          {onReopenSeed ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void executeVoid(true)}
            >
              {busy ? "处理中…" : "作废并重新填写价格"}
            </button>
          ) : null}
        </div>
      </div>
    </ModalDialog>
  );
}
