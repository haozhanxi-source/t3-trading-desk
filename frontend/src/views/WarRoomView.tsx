import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, BookOpen, Compass, RefreshCw } from "lucide-react";
import { api, errorMessage } from "../api/client";
import type { MarketDualResponse } from "../api/types";
import { ChanlunChart } from "../components/chart/ChanlunChart";
import { OpenPositionDialog, type OpenPositionSeed } from "../components/portfolio/PortfolioDialogs";
import { DirectionBadge, StatusBadge } from "../components/ui/StatusBadge";
import { TradePlannerCard } from "../components/warroom/TradePlannerCard";
import { fmtPct, fmtPrice, shortTime } from "../lib/format";
import { formatSymbolWithCode } from "../lib/symbols";

const BENCHMARK_CHIPS = [
  { symbol: "SPY", label: "SPY 标普" },
  { symbol: "GC=F", label: "GC=F 黄金" },
  { symbol: "CU0", label: "CU0 沪铜" },
  { symbol: "AUDUSD=X", label: "AUDUSD 澳美" },
  { symbol: "BTC", label: "BTC 比特币" },
  { symbol: "600519", label: "600519 茅台" },
  { symbol: "PWR", label: "PWR 电网" },
  { symbol: "LC0", label: "LC0 碳酸锂" },
];

interface WarRoomViewProps {
  symbol: string;
  portfolioInitialized?: boolean;
  onSelectSymbol: (symbol: string) => void;
  onJumpArchive: (symbol: string) => void;
  onMacroContext?: (macro: Record<string, unknown>) => void;
  onPortfolioChanged?: () => void;
}

export function WarRoomView({
  symbol,
  portfolioInitialized,
  onSelectSymbol,
  onJumpArchive,
  onMacroContext,
  onPortfolioChanged,
}: WarRoomViewProps) {
  const [draftSymbol, setDraftSymbol] = useState(symbol);
  const [interval, setInterval] = useState<"15m" | "30m" | "60m">("30m");
  const [data, setData] = useState<MarketDualResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openPosSeed, setOpenPosSeed] = useState<OpenPositionSeed | null>(null);
  const [ledgerInit, setLedgerInit] = useState<boolean | undefined>(portfolioInitialized);
  const [accountCurrency, setAccountCurrency] = useState<string | null>(null);

  useEffect(() => {
    if (portfolioInitialized !== undefined) setLedgerInit(portfolioInitialized);
  }, [portfolioInitialized]);

  const refreshPortfolioMeta = useCallback((signal?: AbortSignal) => {
    api
      .portfolioSummary(signal)
      .then((pf) => {
        setLedgerInit(pf.initialized);
        setAccountCurrency(pf.summary?.base_currency ?? null);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    refreshPortfolioMeta(ctrl.signal);
    return () => ctrl.abort();
  }, [refreshPortfolioMeta]);

  useEffect(() => {
    setDraftSymbol(symbol);
  }, [symbol]);

  const loadSnapshot = useCallback(
    async (targetSym: string, targetInterval: string, forceRefresh = false, signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.marketDual(
          {
            symbol: targetSym,
            period: "3y",
            intraday_interval: targetInterval,
            include_macro: true,
            refresh: forceRefresh,
          },
          signal,
        );
        setData(res);
        if (res.macro_context && onMacroContext) {
          onMacroContext(res.macro_context);
        }
      } catch (ex) {
        if (ex instanceof DOMException && ex.name === "AbortError") return;
        setError(errorMessage(ex));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [onMacroContext],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void loadSnapshot(symbol, interval, false, ctrl.signal);
    return () => ctrl.abort();
  }, [symbol, interval, loadSnapshot]);

  const handleSubmitSymbol = (e: FormEvent) => {
    e.preventDefault();
    const cleaned = draftSymbol.trim();
    if (!cleaned) return;
    if (cleaned.toUpperCase() === symbol.toUpperCase()) {
      void loadSnapshot(symbol, interval, true);
    } else {
      onSelectSymbol(cleaned);
    }
  };

  const dual = data?.chanlun?.dual_alignment;
  const daily = data?.chanlun?.daily;
  const intraday = data?.chanlun?.intraday;
  const dailyMom = daily?.momentum_assessment;
  const intraMom = intraday?.momentum_assessment;

  /* Strictly reflect the actually loaded snapshot interval, never relabel stale 15m/60m data as 30m while switching */
  const rawLoadedIv = data?.intraday_interval || dual?.intraday_timeframe || null;
  const loadedInterval: "15m" | "30m" | "60m" =
    rawLoadedIv === "15m" || rawLoadedIv === "15min"
      ? "15m"
      : rawLoadedIv === "60m" || rawLoadedIv === "60min"
        ? "60m"
        : rawLoadedIv === "30m" || rawLoadedIv === "30min"
          ? "30m"
          : interval;
  const isLoaded30m = rawLoadedIv === "30m" || rawLoadedIv === "30min";
  const switchingInterval = Boolean(loading && rawLoadedIv && loadedInterval !== interval);

  return (
    <div className="flex flex-col gap-4">
      {/* Top Symbol & Interval Switcher Bar */}
      <section className="card flex flex-wrap items-center justify-between gap-3 px-4 py-3" aria-label="战情室标的与周期切换">
        <div className="flex flex-wrap items-center gap-1.5">
          {BENCHMARK_CHIPS.map((b) => (
            <button
              key={b.symbol}
              type="button"
              className="chip"
              aria-pressed={symbol.toUpperCase() === b.symbol.toUpperCase()}
              onClick={() => onSelectSymbol(b.symbol)}
            >
              {b.label}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmitSymbol} className="flex flex-wrap items-center gap-2">
          <input
            className="input w-44"
            aria-label="输入任意标的代码或中文别名"
            placeholder="输入代码如 NVDA / RB0"
            value={draftSymbol}
            onChange={(e) => setDraftSymbol(e.target.value)}
          />
          <button type="submit" className="btn">
            载入
          </button>

          <div className="flex items-center gap-1" role="group" aria-label="日内小级别周期选择">
            {(["15m", "30m", "60m"] as const).map((iv) => (
              <button
                key={iv}
                type="button"
                className="chip"
                aria-pressed={interval === iv}
                onClick={() => setInterval(iv)}
              >
                {iv}
              </button>
            ))}
          </div>

          <button
            type="button"
            className="btn"
            disabled={loading}
            onClick={() => void loadSnapshot(symbol, interval, true)}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "计算中…" : "刷新"}</span>
          </button>

          <button type="button" className="btn" onClick={() => onJumpArchive(data?.symbol || symbol)}>
            <BookOpen className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
            <span>查看档案</span>
          </button>
        </form>
      </section>

      {error ? (
        <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-3 text-xs text-ink">
          <strong>行情快照拉取异常：</strong> {error}
        </div>
      ) : null}

      {/* Dual-Timeframe Alignment Banner */}
      <section className="card flex flex-col gap-2.5 px-4 py-3" aria-label="大小级别区间套联立状态">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Compass className="h-4 w-4 text-slot1" aria-hidden="true" />
            <span className="text-sm font-semibold text-ink">
              {formatSymbolWithCode(data?.symbol || symbol)} · 日线定方向 × {loadedInterval} 区间套联立
            </span>
            {switchingInterval ? (
              <StatusBadge tone="info" label={`正在切换至 ${interval}…`} />
            ) : null}
            <DirectionBadge direction={dual?.daily_bias} />
            {dual?.alignment_status ? (
              <StatusBadge
                tone={dual.aligned ? "good" : "warning"}
                label={`联立状态：${dual.alignment_status}`}
              />
            ) : null}
            {dual?.noise_reduction_pct != null ? (
              <StatusBadge
                tone="info"
                label={`止损噪点压缩比：${fmtPct(dual.noise_reduction_pct, 1)}`}
              />
            ) : null}
            {dailyMom?.momentum_reliable === false ? (
              <StatusBadge
                tone="warning"
                label={`日线力度比提示：${dailyMom.momentum_reliability_note || "短笔≤3K改看幅度与DIF"}`}
              />
            ) : null}
            {intraMom?.momentum_reliable === false ? (
              <StatusBadge
                tone="warning"
                label={`${loadedInterval}力度比提示：短笔或零面积改看幅度`}
              />
            ) : null}
          </div>

          <div className="tnum flex flex-wrap items-center gap-3 text-xs text-muted">
            {data?.asset_class ? <span>品种：{data.asset_class}</span> : null}
            {dual?.intraday_tactical_stop != null ? (
              <span>
                {loadedInterval} 战术止损：<strong className="text-ink">{fmtPrice(dual.intraday_tactical_stop)}</strong>
              </span>
            ) : null}
            {dual?.daily_structural_stop != null ? (
              <span>
                日线结构位：<strong className="text-ink">{fmtPrice(dual.daily_structural_stop)}</strong>
              </span>
            ) : null}
            {data?.fetched_at_utc ? <span>快照：{shortTime(data.fetched_at_utc)} UTC</span> : null}
          </div>
        </div>

        <div className="grid grid-cols-1 gap-2 text-xs text-ink-2 md:grid-cols-2">
          <div className="rounded-md border border-hairline bg-page/60 px-3 py-2">
            <span className="text-muted">日线大框架：</span>{" "}
            <strong className="font-medium text-ink">
              {data?.chanlun?.signal_summary?.summary || dual?.daily_summary || "等待加载…"}
            </strong>
          </div>
          <div className="rounded-md border border-hairline bg-page/60 px-3 py-2">
            <span className="text-muted">{loadedInterval} 精确触发：</span>{" "}
            <strong className="font-medium text-ink">
              {data?.intraday_error
                ? `日内源提示：${data.intraday_error}`
                : data?.chanlun?.intraday_signal_summary?.summary || dual?.intraday_summary || "等待加载…"}
            </strong>
          </div>
        </div>

        {dual?.notes && dual.notes.length > 0 ? (
          <ul className="m-0 flex flex-wrap gap-x-4 gap-y-1 pl-4 text-[11.5px] text-muted list-disc">
            {dual.notes.map((n, idx) => (
              <li key={idx}>{n}</li>
            ))}
          </ul>
        ) : null}

        {data?.intraday_error ? (
          <div className="flex items-center gap-2 rounded border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-ink">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
            <span>日内 K 线提示：{data.intraday_error}（日线缠论大结构仍正常展示）</span>
          </div>
        ) : null}
      </section>

      {/* Side-by-side 3Y Daily & Intraday Lightweight Charts v5 Canvases */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <ChanlunChart
          title={`3年日线缠论图 · ${data?.symbol || symbol}`}
          subtitle="定主方向、笔/段中枢 [ZD, ZG] 空间与 MACD 动力学对比"
          bars={data?.ohlcv_daily ?? []}
          chanlun={daily}
          signalSummary={data?.chanlun?.signal_summary}
          intraday={false}
          defenseLines={{
            structuralStopDaily: dual?.daily_structural_stop ?? daily?.latest_zhongshu?.zd,
            tacticalStop30m: isLoaded30m ? dual?.intraday_tactical_stop : undefined,
            tacticalStopLabel: "30m 战术止损",
            reverseKWatch: dual?.daily_reverse_k_watch ?? daily?.stroke_end_trigger?.level,
          }}
        />

        <ChanlunChart
          title={`${loadedInterval} 缠论执行图 · ${data?.symbol || symbol}`}
          subtitle="诞生精确 1/2/3 买卖点、第 11 课反向 K 线与紧凑战术止损"
          bars={data?.ohlcv_intraday ?? []}
          chanlun={intraday}
          signalSummary={data?.chanlun?.intraday_signal_summary}
          intraday
          defenseLines={{
            tacticalStop30m: dual?.intraday_tactical_stop,
            tacticalStopLabel: `${loadedInterval} 战术止损`,
            structuralStopDaily: dual?.daily_structural_stop,
            reverseKWatch: dual?.intraday_reverse_k_watch ?? intraday?.stroke_end_trigger?.level,
          }}
        />
      </div>

      {/* Layer 4 Trade Planner */}
      <TradePlannerCard
        symbol={data?.symbol || symbol}
        dualData={data}
        intradayInterval={loadedInterval}
        portfolioInitialized={ledgerInit}
        accountCurrency={accountCurrency}
        onOpenPositionSeed={(seed) => setOpenPosSeed(seed)}
      />

      <OpenPositionDialog
        open={openPosSeed !== null}
        onClose={() => setOpenPosSeed(null)}
        onSuccess={() => {
          setOpenPosSeed(null);
          refreshPortfolioMeta();
          onPortfolioChanged?.();
        }}
        seed={openPosSeed ?? undefined}
        accountCurrency={accountCurrency}
      />
    </div>
  );
}
