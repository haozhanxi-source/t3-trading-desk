import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AlertTriangle, BookOpen, RefreshCw, Search } from "lucide-react";
import { api, errorMessage } from "../api/client";
import type { ArchiveListItem, MarketDualResponse, Position, SymbolSearchItem } from "../api/types";
import { ChanlunChart, type PlanOverlayLines } from "../components/chart/ChanlunChart";
import { OpenPositionDialog, type OpenPositionSeed } from "../components/portfolio/PortfolioDialogs";
import { DirectionBadge, StatusBadge } from "../components/ui/StatusBadge";
import { SymbolLabel } from "../components/ui/SymbolLabel";
import {
  TradePlannerCard,
  type ArchivePlanContext,
  type PickedPriceUpdate,
  type PickPriceField,
} from "../components/warroom/TradePlannerCard";
import { fmtAlignmentStatus, fmtPct, fmtPrice, shortTime } from "../lib/format";
import { getSymbolName } from "../lib/symbols";

const FALLBACK_ANCHORS = ["SPY", "GC=F", "CU0", "AUDUSD=X", "BTC", "LC0"];

function matchCanonicalTicker(a: string | null | undefined, b: string | null | undefined): boolean {
  const clean = (s: string | null | undefined) => (s || "").trim().toUpperCase();
  const ca = clean(a);
  const cb = clean(b);
  if (!ca || !cb) return false;
  if (ca === cb) return true;
  /* Only equate 6-letter FX pairs with/without `=X` (never strip .SS/.SZ/.BJ exchange suffixes!) */
  if (/^[A-Z]{6}(=X)?$/.test(ca) && /^[A-Z]{6}(=X)?$/.test(cb)) {
    return ca.replace(/=X$/, "") === cb.replace(/=X$/, "");
  }
  return false;
}

interface QuickChipItem {
  symbol: string;
  tag: "holding" | "watchlist" | "anchor";
}

interface WarRoomViewProps {
  active?: boolean;
  symbol: string;
  workspaceRev?: number;
  portfolioInitialized?: boolean;
  onSelectSymbol: (symbol: string) => void;
  onJumpArchive: (symbol: string, market?: string | null) => void;
  onMacroContext?: (macro: Record<string, unknown>) => void;
  onPortfolioChanged?: () => void;
}

export function WarRoomView({
  active = true,
  symbol,
  workspaceRev = 0,
  portfolioInitialized,
  onSelectSymbol,
  onJumpArchive,
  onMacroContext,
  onPortfolioChanged,
}: WarRoomViewProps) {
  const [draftSymbol, setDraftSymbol] = useState(symbol);
  const [suggestions, setSuggestions] = useState<SymbolSearchItem[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [interval, setInterval] = useState<"15m" | "30m" | "60m">("30m");
  const [snapBundle, setSnapBundle] = useState<{
    reqKey: string;
    data: MarketDualResponse;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const snapAbortRef = useRef<AbortController | null>(null);
  const snapReqIdRef = useRef(0);

  const [openPosSeed, setOpenPosSeed] = useState<OpenPositionSeed | null>(null);
  const [ledgerInit, setLedgerInit] = useState<boolean | undefined>(portfolioInitialized);
  const [accountEquity, setAccountEquity] = useState<number | null>(null);
  const [accountCurrency, setAccountCurrency] = useState<string | null>(null);
  const [openPositions, setOpenPositions] = useState<Position[]>([]);
  const [archives, setArchives] = useState<ArchiveListItem[]>([]);
  const [archivesLoaded, setArchivesLoaded] = useState(false);
  const [archiveCtx, setArchiveCtx] = useState<ArchivePlanContext | null>(null);

  /* Interactive Chart <-> Planner bridge + Dual-Timeframe Crosshair Sync */
  const [pickField, setPickField] = useState<PickPriceField | null>(null);
  const [pickedPrice, setPickedPrice] = useState<PickedPriceUpdate | null>(null);
  const [planOverlay, setPlanOverlay] = useState<PlanOverlayLines | null>(null);
  const [syncFromIntradayDate, setSyncFromIntradayDate] = useState<string | null>(null);
  const [syncFromDailyDate, setSyncFromDailyDate] = useState<string | null>(null);
  const pickSeqRef = useRef(0);

  useEffect(() => {
    if (portfolioInitialized !== undefined) setLedgerInit(portfolioInitialized);
  }, [portfolioInitialized]);

  const refreshWorkspaceContext = useCallback((signal?: AbortSignal) => {
    api
      .portfolioSummary(signal)
      .then((pf) => {
        setLedgerInit(pf.initialized);
        setAccountEquity(pf.summary?.equity ?? null);
        setAccountCurrency(pf.summary?.base_currency ?? null);
        setOpenPositions((pf.positions ?? []).filter((p) => !p.status || p.status === "open"));
      })
      .catch(() => undefined);
    api
      .archives(signal)
      .then((r) => {
        setArchives(r.archives ?? []);
        setArchivesLoaded(true);
      })
      .catch(() => {
        if (!signal?.aborted) setArchivesLoaded(true);
      });
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    refreshWorkspaceContext(ctrl.signal);
    return () => ctrl.abort();
  }, [refreshWorkspaceContext, workspaceRev]);

  const currentReqKey = symbol.trim().toUpperCase();
  const currentData = useMemo(
    () => (snapBundle && snapBundle.reqKey === currentReqKey ? snapBundle.data : null),
    [snapBundle, currentReqKey],
  );
  const canonicalSym = currentData?.symbol || symbol;

  const holdingSectorTheme = useMemo(
    () => openPositions.find((p) => matchCanonicalTicker(p.symbol, canonicalSym))?.sector_theme ?? null,
    [openPositions, canonicalSym],
  );

  const matchedArchiveEntry = useMemo(
    () => archives.find((a) => matchCanonicalTicker(a.symbol, canonicalSym)) ?? null,
    [archives, canonicalSym],
  );

  /* Fetch research archive metadata ONLY when canonical symbol exists in archive catalog (zero 404 spam) */
  useEffect(() => {
    const reqKey = currentReqKey;
    setArchiveCtx(null);
    if (!archivesLoaded) return;
    if (!matchedArchiveEntry) {
      setArchiveCtx({
        forSymbol: reqKey,
        exists: false,
        sectorTheme: holdingSectorTheme,
        hasL1L2Evidence: false,
      });
      return;
    }
    const ctrl = new AbortController();
    api
      .archive(matchedArchiveEntry.symbol, matchedArchiveEntry.market, ctrl.signal)
      .then((detail) => {
        if (ctrl.signal.aborted) return;
        const hasL1L2 = (detail.recent_evidence ?? []).some(
          (ev) => ev.level === "L1" || ev.level === "L2",
        );
        setArchiveCtx({
          forSymbol: reqKey,
          exists: true,
          market: detail.market,
          sectorTheme:
            holdingSectorTheme ||
            detail.state?.layer1_drivers?.primary ||
            detail.state?.instrument?.subtype ||
            null,
          hasL1L2Evidence: hasL1L2,
          evidenceCount: detail.evidence_count,
        });
      })
      .catch(() => {
        if (!ctrl.signal.aborted) {
          setArchiveCtx({
            forSymbol: reqKey,
            exists: false,
            sectorTheme: holdingSectorTheme,
            hasL1L2Evidence: false,
          });
        }
      });
    return () => ctrl.abort();
  }, [currentReqKey, archivesLoaded, matchedArchiveEntry, holdingSectorTheme, workspaceRev]);

  useEffect(() => {
    setDraftSymbol(symbol);
    setSuggestOpen(false);
  }, [symbol]);

  /* Autocomplete search suggestions for WarRoom inline symbol input */
  useEffect(() => {
    const q = draftSymbol.trim();
    if (!suggestOpen || !q || q.toUpperCase() === symbol.toUpperCase()) {
      setSuggestions([]);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      api
        .searchSymbols(q, 8, ctrl.signal)
        .then((r) => setSuggestions(r.items ?? []))
        .catch(() => {
          if (!ctrl.signal.aborted) setSuggestions([]);
        });
    }, 90);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [draftSymbol, suggestOpen, symbol]);

  /* Keyboard shortcuts inside active WarRoom: Esc cancels price pick, [ / ] switches interval */
  useEffect(() => {
    if (!active) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && pickField) {
        setPickField(null);
        return;
      }
      const el = e.target;
      if (
        el instanceof HTMLElement &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT")
      ) {
        return;
      }
      if (!e.metaKey && !e.ctrlKey && !e.altKey) {
        const order: ("15m" | "30m" | "60m")[] = ["15m", "30m", "60m"];
        if (e.key === "[") {
          setInterval((cur) => order[Math.max(0, order.indexOf(cur) - 1)]);
        } else if (e.key === "]") {
          setInterval((cur) => order[Math.min(order.length - 1, order.indexOf(cur) + 1)]);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, pickField]);

  const loadSnapshot = useCallback(
    async (targetSym: string, targetInterval: string, forceRefresh = false) => {
      if (snapAbortRef.current) {
        snapAbortRef.current.abort();
      }
      const ctrl = new AbortController();
      snapAbortRef.current = ctrl;
      const reqId = ++snapReqIdRef.current;
      const reqKey = targetSym.trim().toUpperCase();

      setLoading(true);
      setError(null);
      setSnapBundle((prev) => (prev && prev.reqKey === reqKey ? prev : null));

      try {
        const res = await api.marketDual(
          {
            symbol: targetSym,
            period: "3y",
            intraday_interval: targetInterval,
            include_macro: true,
            refresh: forceRefresh,
          },
          ctrl.signal,
        );
        if (ctrl.signal.aborted || reqId !== snapReqIdRef.current) return;
        setSnapBundle({ reqKey, data: res });
        if (res.macro_context && onMacroContext) {
          onMacroContext(res.macro_context);
        }
      } catch (ex) {
        if (ctrl.signal.aborted || (ex instanceof DOMException && ex.name === "AbortError") || reqId !== snapReqIdRef.current) {
          return;
        }
        setSnapBundle(null);
        setError(errorMessage(ex));
      } finally {
        if (reqId === snapReqIdRef.current) {
          if (snapAbortRef.current === ctrl) {
            snapAbortRef.current = null;
          }
          setLoading(false);
        }
      }
    },
    [onMacroContext],
  );

  useEffect(() => {
    void loadSnapshot(symbol, interval, false);
    return () => {
      snapAbortRef.current?.abort();
    };
  }, [symbol, interval, loadSnapshot]);

  const handleSubmitSymbol = (e: FormEvent) => {
    e.preventDefault();
    const cleaned = draftSymbol.trim();
    if (!cleaned) return;
    setSuggestOpen(false);
    if (cleaned.toUpperCase() === symbol.toUpperCase()) {
      void loadSnapshot(symbol, interval, true);
    } else {
      onSelectSymbol(cleaned);
    }
  };

  /* Prioritize user's real open holdings + watchlist leaders before fallback anchors */
  const quickChips = useMemo<QuickChipItem[]>(() => {
    const seen = new Set<string>();
    const out: QuickChipItem[] = [];
    for (const p of openPositions) {
      const s = p.symbol.toUpperCase();
      if (!seen.has(s)) {
        seen.add(s);
        out.push({ symbol: p.symbol, tag: "holding" });
      }
    }
    for (const a of archives) {
      const s = a.symbol.toUpperCase();
      if (!seen.has(s) && (a.status === "holding" || a.status === "watchlist")) {
        seen.add(s);
        out.push({ symbol: a.symbol, tag: a.status === "holding" ? "holding" : "watchlist" });
      }
      if (out.length >= 8) break;
    }
    for (const b of FALLBACK_ANCHORS) {
      if (out.length >= 8) break;
      const s = b.toUpperCase();
      if (!seen.has(s)) {
        seen.add(s);
        out.push({ symbol: b, tag: "anchor" });
      }
    }
    return out;
  }, [openPositions, archives]);

  const dual = currentData?.chanlun?.dual_alignment;
  const daily = currentData?.chanlun?.daily;
  const intraday = currentData?.chanlun?.intraday;
  const dailyMom = daily?.momentum_assessment;
  const intraMom = intraday?.momentum_assessment;
  const lastPrice =
    currentData?.ohlcv_intraday?.[currentData.ohlcv_intraday.length - 1]?.close ??
    currentData?.ohlcv_daily?.[currentData.ohlcv_daily.length - 1]?.close ??
    null;

  const rawLoadedIv = currentData?.intraday_interval || dual?.intraday_timeframe || null;
  const loadedInterval: "15m" | "30m" | "60m" =
    rawLoadedIv === "15m" || rawLoadedIv === "15min"
      ? "15m"
      : rawLoadedIv === "60m" || rawLoadedIv === "60min"
        ? "60m"
        : rawLoadedIv === "30m" || rawLoadedIv === "30min"
          ? "30m"
          : interval;
  const switchingInterval = Boolean(loading && rawLoadedIv && loadedInterval !== interval);

  const pickHintText =
    pickField === "entry"
      ? "正在拾取「计划入场价」"
      : pickField === "stop30m"
        ? "正在拾取「手动战术止损位」"
        : pickField === "target"
          ? "正在拾取「1Q~1Y 目标价」"
          : null;

  const handleChartPick = useCallback(
    (price: number) => {
      if (!pickField) return;
      pickSeqRef.current += 1;
      setPickedPrice({ seq: pickSeqRef.current, field: pickField, price });
      setPickField(null);
    },
    [pickField],
  );

  const resolvedSym = currentData?.symbol || symbol;
  const scopedArchiveCtx =
    archiveCtx && archiveCtx.forSymbol === currentReqKey ? archiveCtx : null;

  return (
    <div className="flex flex-col gap-3.5">
      {/* Unified Compact Cockpit Header (replaces 3 stacked editorial headers) */}
      <section className="card flex flex-col gap-2.5 px-4 py-3" aria-label="战情室行情与区间套联立总览">
        <div className="flex flex-wrap items-center justify-between gap-3">
          {/* Left: Instrument Identity + Live Quote + Alignment Badges */}
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <SymbolLabel symbol={resolvedSym} size="lg" />
            {lastPrice != null ? (
              <span className="tnum font-mono text-base font-semibold text-ink">
                {fmtPrice(lastPrice)}
              </span>
            ) : null}
            <DirectionBadge direction={dual?.daily_bias} />
            {switchingInterval ? (
              <StatusBadge tone="info" label={`切换 ${interval}…`} />
            ) : dual?.alignment_status ? (
              <StatusBadge
                tone={dual.aligned ? "good" : "warning"}
                label={`日线×${loadedInterval}：${fmtAlignmentStatus(dual.alignment_status)}`}
              />
            ) : null}
            {dual?.noise_reduction_pct != null ? (
              <StatusBadge
                tone="info"
                label={`止损噪点压缩 ${fmtPct(dual.noise_reduction_pct, 1)}`}
              />
            ) : null}
            {dailyMom?.momentum_reliable === false || intraMom?.momentum_reliable === false ? (
              <StatusBadge tone="warning" label="短笔≤3K改看幅度/DIF" />
            ) : null}
          </div>

          {/* Right: Inline Autocomplete Search + [15m/30m/60m] + Refresh + Archive Link */}
          <div className="flex flex-wrap items-center gap-2">
            <form onSubmit={handleSubmitSymbol} className="relative flex items-center gap-1.5">
              <div className="relative">
                <Search
                  className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted"
                  aria-hidden="true"
                />
                <input
                  className="input w-44 pl-8"
                  aria-label="输入标的代码或中文名称快速切换"
                  placeholder="代码或中文名…"
                  value={draftSymbol}
                  onFocus={() => setSuggestOpen(true)}
                  onBlur={() => window.setTimeout(() => setSuggestOpen(false), 140)}
                  onChange={(e) => {
                    setDraftSymbol(e.target.value);
                    setSuggestOpen(true);
                  }}
                />
                {suggestOpen && suggestions.length > 0 ? (
                  <ul
                    role="listbox"
                    className="absolute top-full right-0 z-30 mt-1 max-h-60 w-64 list-none overflow-y-auto rounded-md border border-hairline bg-surface p-1 shadow-lg"
                  >
                    {suggestions.map((it) => {
                      const zh =
                        it.label && it.label.toUpperCase() !== it.symbol.toUpperCase()
                          ? it.label
                          : getSymbolName(it.symbol);
                      return (
                        <li
                          key={it.symbol}
                          role="option"
                          aria-selected={false}
                          onMouseDown={(ev) => {
                            ev.preventDefault();
                            setSuggestOpen(false);
                            onSelectSymbol(it.symbol);
                          }}
                          className="flex cursor-pointer items-center justify-between gap-2 rounded px-2.5 py-1.5 text-xs hover:bg-elevated"
                        >
                          <span className="truncate font-medium text-ink">
                            {zh || it.symbol}
                            {zh ? (
                              <span className="ml-1.5 font-mono text-[11px] text-muted">{it.symbol}</span>
                            ) : null}
                          </span>
                          <span className="font-mono text-[10px] text-muted">{it.asset_class}</span>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
              </div>
              <button type="submit" className="btn btn-sm">
                载入
              </button>
            </form>

            <div className="segmented" role="group" aria-label="小级别执行周期 ([ / ] 键快切)">
              {(["15m", "30m", "60m"] as const).map((iv) => (
                <button
                  key={iv}
                  type="button"
                  className="segmented-btn px-2 py-0.5 font-mono text-[11px]"
                  aria-pressed={interval === iv}
                  title="可使用 [ 与 ] 键快速切换小级别周期"
                  onClick={() => setInterval(iv)}
                >
                  {iv}
                </button>
              ))}
            </div>

            <button
              type="button"
              className="btn btn-sm"
              disabled={loading}
              onClick={() => void loadSnapshot(symbol, interval, true)}
              title="强制绕过缓存重算双周期缠论结构"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              <span className="hidden sm:inline">{loading ? "计算中" : "刷新"}</span>
            </button>

            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => onJumpArchive(resolvedSym, scopedArchiveCtx?.market)}
            >
              <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
              <span>
                {scopedArchiveCtx?.exists
                  ? `研报档案 (${scopedArchiveCtx.evidenceCount ?? 0})`
                  : "建立档案"}
              </span>
            </button>
          </div>
        </div>

        {/* Dynamic Priority Quick-Switch Bar (Holdings -> Watchlist -> Benchmark Anchors) + Structural Summary */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2">
          <div className="flex flex-wrap items-center gap-1">
            {quickChips.map((item) => {
              const zh = getSymbolName(item.symbol);
              const isCurrent = symbol.toUpperCase() === item.symbol.toUpperCase();
              return (
                <button
                  key={item.symbol}
                  type="button"
                  className="chip"
                  aria-pressed={isCurrent}
                  onClick={() => onSelectSymbol(item.symbol)}
                  title={
                    item.tag === "holding"
                      ? "当前在途持仓标的"
                      : item.tag === "watchlist"
                        ? "研究观察池标的"
                        : "跨资产定价锚"
                  }
                >
                  {item.tag !== "anchor" ? (
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        item.tag === "holding" ? "bg-bull" : "bg-warning"
                      }`}
                      aria-hidden="true"
                    />
                  ) : null}
                  <span>{zh || item.symbol}</span>
                  {zh ? <span className="font-mono text-[10.5px] text-muted">{item.symbol}</span> : null}
                </button>
              );
            })}
          </div>

          <div className="tnum flex flex-wrap items-center gap-3 font-mono text-[11px] text-muted">
            {dual?.intraday_tactical_stop != null ? (
              <span>
                {loadedInterval}止损 <strong className="text-bear">{fmtPrice(dual.intraday_tactical_stop)}</strong>
              </span>
            ) : null}
            {dual?.daily_structural_stop != null ? (
              <span>
                日线防线 <strong className="text-ink-2">{fmtPrice(dual.daily_structural_stop)}</strong>
              </span>
            ) : null}
              {currentData?.fetched_at_utc ? <span>{shortTime(currentData.fetched_at_utc)} UTC</span> : null}
          </div>
        </div>

        {/* Concise Hairline Dual Summary */}
        <div className="grid grid-cols-1 gap-2 border-t border-hairline-soft pt-2 text-xs text-ink-2 md:grid-cols-2">
          <div className="truncate">
            <span className="text-muted">日线方向：</span>
            <strong className="font-medium text-ink">
              {currentData?.chanlun?.signal_summary?.summary || dual?.daily_summary || (error ? "加载失败" : "加载中…")}
            </strong>
          </div>
          <div className="truncate">
            <span className="text-muted">{loadedInterval} 触发：</span>
            <strong className="font-medium text-ink">
              {currentData?.intraday_error
                ? currentData.intraday_error
                : currentData?.chanlun?.intraday_signal_summary?.summary || dual?.intraday_summary || (error ? "加载失败" : "加载中…")}
            </strong>
          </div>
        </div>

        {currentData?.intraday_error ? (
          <div className="flex items-center gap-2 rounded border border-warning/40 bg-warning/10 px-2.5 py-1 text-xs text-ink">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
            <span>日内源提示：{currentData.intraday_error}（日线缠论结构仍正常可用）</span>
          </div>
        ) : null}
      </section>

      {error ? (
        <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-2.5 text-xs text-ink">
          <strong>行情快照拉取异常：</strong> {error}
        </div>
      ) : null}

      {/* Side-by-side 3Y Daily & Intraday Charts with Crosshair Date Sync */}
      <div className="grid grid-cols-1 gap-3.5 xl:grid-cols-2">
        <ChanlunChart
          title="3Y 日线结构"
          bars={currentData?.ohlcv_daily ?? []}
          chanlun={daily}
          signalSummary={currentData?.chanlun?.signal_summary}
          intraday={false}
          active={active}
          planOverlay={planOverlay}
          pickHint={pickHintText}
          onPickPrice={pickField ? handleChartPick : null}
          syncDate={syncFromIntradayDate}
          onHoverDate={setSyncFromDailyDate}
        />

        <ChanlunChart
          title={`${loadedInterval} 区间套执行`}
          bars={currentData?.ohlcv_intraday ?? []}
          chanlun={intraday}
          signalSummary={currentData?.chanlun?.intraday_signal_summary}
          intraday
          active={active}
          planOverlay={planOverlay}
          pickHint={pickHintText}
          onPickPrice={pickField ? handleChartPick : null}
          syncDate={syncFromDailyDate}
          onHoverDate={setSyncFromIntradayDate}
        />
      </div>

      {/* Layer 4 Trade Planner */}
      <TradePlannerCard
        symbol={resolvedSym}
        dualData={currentData}
        snapshotLoading={loading}
        intradayInterval={loadedInterval}
        portfolioInitialized={ledgerInit}
        accountEquity={accountEquity}
        accountCurrency={accountCurrency}
        archiveContext={scopedArchiveCtx}
        activePickField={pickField}
        pickedPrice={pickedPrice}
        onRequestPickField={setPickField}
        onOverlayChange={setPlanOverlay}
        onOpenPositionSeed={(seed) => setOpenPosSeed(seed)}
        onLedgerInitialized={() => {
          refreshWorkspaceContext();
          onPortfolioChanged?.();
        }}
        onJumpArchive={(sym, mkt) => onJumpArchive(sym, mkt)}
      />

      <OpenPositionDialog
        open={openPosSeed !== null}
        onClose={() => setOpenPosSeed(null)}
        onSuccess={() => {
          setOpenPosSeed(null);
          refreshWorkspaceContext();
          onPortfolioChanged?.();
        }}
        seed={openPosSeed ?? undefined}
        accountCurrency={accountCurrency}
      />
    </div>
  );
}
