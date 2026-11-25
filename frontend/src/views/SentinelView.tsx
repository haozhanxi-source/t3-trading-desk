import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeftRight,
  ArrowUpRight,
  BookOpen,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Crosshair,
  History,
  Loader2,
  PlusCircle,
  RefreshCw,
  RotateCcw,
  Scissors,
  Shield,
  Sparkles,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { api, errorMessage, parseTaskEvent } from "../api/client";
import type {
  PortfolioSummaryResponse,
  Position,
  ScaleInResponse,
  SentinelHolding,
  SentinelResponse,
  UpcomingEvent,
} from "../api/types";
import {
  canVoidMistakenPosition,
  ClosePositionDialog,
  InitPortfolioDialog,
  OpenPositionDialog,
  type OpenPositionSeed,
  RotatePositionDialog,
  TrailStopDialog,
  TrimPositionDialog,
  VoidPositionDialog,
} from "../components/portfolio/PortfolioDialogs";
import { ScaleInPositionDialog } from "../components/portfolio/ScaleInPositionDialog";
import {
  DirectionBadge,
  SentinelStatusBadge,
  signedToneClass,
  StatusBadge,
} from "../components/ui/StatusBadge";
import { SymbolLabel } from "../components/ui/SymbolLabel";
import {
  cleanSentinelText,
  fmtEventSource,
  fmtLeaderRole,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtPrice,
  shortTime,
  toNum,
} from "../lib/format";
import { formatSymbolWithCode, getSymbolName } from "../lib/symbols";

const JOURNAL_ACTION_ZH: Record<string, string> = {
  init_account: "初始化账本",
  open: "建仓入账",
  open_position: "建仓入账",
  add_on_profit: "浮盈顺势加仓",
  trail_stop: "收紧保护止损",
  trim: "分批减仓",
  trim_position: "分批减仓",
  close: "清仓了结",
  close_position: "清仓了结",
  void: "误录无损作废",
  void_position: "误录无损作废",
  rotate_leader: "原子龙头换仓",
  leader_rotation: "原子龙头换仓",
};

interface SentinelViewProps {
  workspaceRev?: number;
  onPortfolioMutated?: () => void;
  onJumpWarRoom: (symbol: string) => void;
  onJumpArchive: (symbol: string, market?: string | null) => void;
}

export function SentinelView({
  workspaceRev = 0,
  onPortfolioMutated,
  onJumpWarRoom,
  onJumpArchive,
}: SentinelViewProps) {
  const [portfolio, setPortfolio] = useState<PortfolioSummaryResponse | null>(null);
  const [sentinel, setSentinel] = useState<SentinelResponse | null>(null);
  const [includeWatchlist, setIncludeWatchlist] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedAlerts, setExpandedAlerts] = useState<Record<string, boolean>>({});
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyTaskId, setVerifyTaskId] = useState<string | null>(null);
  const [verifyTargetLabel, setVerifyTargetLabel] = useState<string>("");
  const [verifyMessage, setVerifyMessage] = useState<string | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const verifyEsRef = useRef<EventSource | null>(null);
  const mountedRef = useRef(true);

  /* Dialog states */
  const [initOpen, setInitOpen] = useState(false);
  const [openPosModal, setOpenPosModal] = useState(false);
  const [openSeed, setOpenSeed] = useState<OpenPositionSeed | undefined>(undefined);
  const [trailTarget, setTrailTarget] = useState<{
    positionId: string;
    symbol: string;
    currentStop?: number | null;
    suggestedStop?: number | null;
    currentPrice?: number | null;
    rationale?: string;
  } | null>(null);
  const [trimTarget, setTrimTarget] = useState<{ position: Position; lastPrice?: number | null } | null>(null);
  const [scaleInTarget, setScaleInTarget] = useState<{ position: Position; currentPrice?: number | null } | null>(
    null,
  );
  const [actionBanner, setActionBanner] = useState<string | null>(null);
  const [rotateTarget, setRotateTarget] = useState<{ position: Position; lastPrice?: number | null } | null>(null);
  const [closeTarget, setCloseTarget] = useState<{
    position: Position;
    lastPrice?: number | null;
    reason?: string;
  } | null>(null);
  const [voidTarget, setVoidTarget] = useState<Position | null>(null);

  const loadAbortRef = useRef<AbortController | null>(null);
  const loadReqIdRef = useRef(0);
  const forceNextRefreshRef = useRef(false);

  const loadAll = useCallback(
    async (forceRefresh = false) => {
      if (loadAbortRef.current) {
        loadAbortRef.current.abort();
      }
      const ctrl = new AbortController();
      loadAbortRef.current = ctrl;
      const reqId = ++loadReqIdRef.current;
      let timedOut = false;
      const timeoutId = window.setTimeout(() => {
        timedOut = true;
        ctrl.abort();
      }, 60_000);

      setLoading(true);
      setError(null);
      try {
        const [pfRes, snRes] = await Promise.allSettled([
          api.portfolioSummary(ctrl.signal),
          api.sentinel(
            {
              include_watchlist: includeWatchlist,
              skip_research: false,
              event_window_days: 14,
              refresh: forceRefresh,
            },
            ctrl.signal,
          ),
        ]);
        if (!mountedRef.current || reqId !== loadReqIdRef.current) return;
        if (ctrl.signal.aborted && !timedOut) return;

        const errs: string[] = [];
        if (pfRes.status === "fulfilled") {
          setPortfolio(pfRes.value);
        } else if (timedOut) {
          errs.push("持仓账本连接超时（后端可能正在重启或响应缓慢）");
        } else {
          errs.push(`持仓账本：${errorMessage(pfRes.reason)}`);
        }

        if (snRes.status === "fulfilled") {
          setSentinel(snRes.value);
        } else if (timedOut) {
          errs.push("哨兵巡检请求超时（后端可能正在重启或行情源响应缓慢）");
        } else {
          errs.push(`哨兵巡检：${errorMessage(snRes.reason)}`);
        }

        if (errs.length > 0) {
          setError(errs.join("；"));
        }
      } catch (ex) {
        if (!mountedRef.current || reqId !== loadReqIdRef.current) return;
        if (!timedOut && (ctrl.signal.aborted || (ex instanceof DOMException && ex.name === "AbortError"))) {
          return;
        }
        setError(
          timedOut
            ? "请求超时（后端服务可能正在重启或网络异常），请点击重试"
            : errorMessage(ex),
        );
      } finally {
        window.clearTimeout(timeoutId);
        if (loadAbortRef.current === ctrl) {
          loadAbortRef.current = null;
        }
        if (mountedRef.current && reqId === loadReqIdRef.current) {
          setLoading(false);
        }
      }
    },
    [includeWatchlist],
  );

  useEffect(() => {
    const shouldForce = forceNextRefreshRef.current;
    forceNextRefreshRef.current = false;
    void loadAll(shouldForce);
    return () => {
      loadAbortRef.current?.abort();
    };
  }, [loadAll, workspaceRev]);

  const handleMutationSuccess = useCallback(() => {
    if (onPortfolioMutated) {
      forceNextRefreshRef.current = true;
      onPortfolioMutated();
    } else {
      void loadAll(true);
    }
  }, [loadAll, onPortfolioMutated]);

  const handleScaleInDone = useCallback(
    (res: ScaleInResponse) => {
      const symLabel = formatSymbolWithCode(res.position?.symbol);
      const addQtyStr = fmtNum(res.add_quantity, 6);
      const newAvgStr = fmtPrice(res.new_entry_price);
      const newQtyStr = fmtNum(res.new_quantity, 6);
      const stopPart =
        res.new_stop_loss != null &&
        res.old_stop_loss != null &&
        Math.abs(res.new_stop_loss - res.old_stop_loss) > 1e-9
          ? ` · 30m 止损收紧至 ${fmtPrice(res.new_stop_loss)}`
          : "";
      setActionBanner(
        `已执行浮盈加仓：${symLabel} +${addQtyStr}（新总仓位 ${newQtyStr} · 新均价 ${newAvgStr}${stopPart}）`,
      );
      handleMutationSuccess();
    },
    [handleMutationSuccess],
  );

  const summary = portfolio?.summary;
  const openPositionsById = useMemo(() => {
    const map = new Map<string, Position>();
    for (const p of portfolio?.positions ?? []) {
      if (p.status === "open" || !p.status) {
        map.set(p.position_id, p);
      }
    }
    return map;
  }, [portfolio]);

  const openPositionsBySym = useMemo(() => {
    const map = new Map<string, Position>();
    for (const p of portfolio?.positions ?? []) {
      if (p.status === "open" || !p.status) {
        map.set(p.symbol.toUpperCase(), p);
      }
    }
    return map;
  }, [portfolio]);

  const findPosition = (h: SentinelHolding): Position | null => {
    if (h.position_id && openPositionsById.has(h.position_id)) {
      return openPositionsById.get(h.position_id) ?? null;
    }
    return openPositionsBySym.get(h.symbol.toUpperCase()) ?? null;
  };

  const allUpcomingEvents = useMemo<(UpcomingEvent & { symbol: string })[]>(() => {
    const out: (UpcomingEvent & { symbol: string })[] = [];
    for (const h of sentinel?.holdings ?? []) {
      for (const ev of h.upcoming_events ?? []) {
        out.push({ ...ev, symbol: h.symbol });
      }
    }
    for (const w of sentinel?.watchlist ?? []) {
      for (const ev of w.upcoming_events ?? []) {
        out.push({ ...ev, symbol: w.symbol });
      }
    }
    out.sort((a, b) => a.days_until - b.days_until);
    return out;
  }, [sentinel]);

  const totalVerifiableTriggers = useMemo(() => {
    let n = 0;
    for (const h of sentinel?.holdings ?? []) {
      n += h.verifiable_trigger_ids?.length ?? 0;
    }
    return n;
  }, [sentinel]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (verifyEsRef.current) {
        verifyEsRef.current.close();
        verifyEsRef.current = null;
      }
    };
  }, []);

  const formatVerifySummary = (res: Record<string, unknown>): string => {
    const upd = typeof res.updated_count === "number" ? res.updated_count : 0;
    const fired = typeof res.fired_count === "number" ? res.fired_count : 0;
    const clear = typeof res.clear_count === "number" ? res.clear_count : 0;
    const uncertain = typeof res.uncertain_count === "number" ? res.uncertain_count : 0;
    const conflicts = typeof res.conflict_count === "number" ? res.conflict_count : 0;
    const missing = typeof res.missing_count === "number" ? res.missing_count : 0;
    if (upd === 0 && conflicts === 0 && missing === 0) {
      return "核验完成：暂无需要更新的触发器。";
    }
    const parts = [`更新 ${upd} 条`, `命中 ${fired}`, `确认未触发 ${clear}`, `待确认 ${uncertain}`];
    if (conflicts > 0) parts.push(`冲突跳过 ${conflicts}`);
    if (missing > 0) parts.push(`未返回 ${missing}`);
    return `AI 联网核验完成（${parts.join(" · ")}），正在刷新哨兵…`;
  };

  const runAiTriggerVerify = useCallback(
    async (opts?: { symbol?: string; market?: string | null; triggerIds?: string[]; label?: string }) => {
      if (verifyBusy) return;
      if (verifyEsRef.current) {
        verifyEsRef.current.close();
        verifyEsRef.current = null;
      }
      setVerifyBusy(true);
      setVerifyError(null);
      setVerifyTargetLabel(opts?.label || opts?.symbol || "全部在途持仓");
      setVerifyMessage("正在启动 AI 联网核验引擎…");

      try {
        const task = await api.startTriggerVerify({
          symbol: opts?.symbol ?? "",
          market: opts?.market ?? null,
          trigger_ids: opts?.triggerIds ?? [],
        });
        if (!mountedRef.current) return;
        setVerifyTaskId(task.id);
        const es = new EventSource(api.taskEventsUrl(task.id));
        verifyEsRef.current = es;
        let settled = false;

        const finish = (errMsg?: string | null, doneMsg?: string | null, shouldRefresh = false) => {
          if (settled) return;
          settled = true;
          es.close();
          if (verifyEsRef.current === es) verifyEsRef.current = null;
          if (!mountedRef.current) return;
          setVerifyBusy(false);
          setVerifyTaskId(null);
          if (errMsg) {
            setVerifyError(errMsg);
            setVerifyMessage(null);
          } else if (doneMsg) {
            setVerifyMessage(doneMsg);
          }
          if (shouldRefresh) {
            void loadAll(true);
            onPortfolioMutated?.();
          }
        };

        const handleEvent = (type: string) => (ev: MessageEvent) => {
          if (settled || !mountedRef.current) return;
          const rawData = typeof ev.data === "string" ? ev.data : "";
          // Browser transport error emits an 'error' event with empty data; let EventSource auto-reconnect.
          if (type === "error" && !rawData.trim()) {
            if (es.readyState === EventSource.CONNECTING) {
              setVerifyMessage("核验进度流短暂断连，正在自动重连…");
            }
            return;
          }
          const parsed = parseTaskEvent(rawData, type);
          const payload = (parsed?.data ?? parsed ?? {}) as Record<string, unknown>;
          const msg =
            (typeof payload.message === "string" && payload.message) ||
            (typeof payload.error === "string" && payload.error) ||
            (typeof parsed?.message === "string" && parsed.message) ||
            "";
          if (type === "step" || type === "log") {
            if (msg) setVerifyMessage(msg);
          } else if (type === "done") {
            const res = ((payload.result ?? parsed?.result) ?? {}) as Record<string, unknown>;
            finish(null, formatVerifySummary(res), true);
          } else if (type === "cancelled") {
            finish("已取消 AI 触发器核验任务（若已有部分落盘已自动刷新）", null, true);
          } else if (type === "error") {
            finish(msg || "AI 触发器核验失败", null, true);
          }
        };

        for (const t of ["step", "log", "done", "error", "cancelled"]) {
          es.addEventListener(t, handleEvent(t) as EventListener);
        }
        es.onerror = () => {
          if (settled || !mountedRef.current) return;
          if (es.readyState === EventSource.CONNECTING) {
            setVerifyMessage("核验进度流短暂断连，正在自动重连…");
            return;
          }
          if (es.readyState === EventSource.CLOSED) {
            void api
              .task(task.id)
              .then((t) => {
                if (t.status === "completed") {
                  finish(null, formatVerifySummary((t.result ?? {}) as Record<string, unknown>), true);
                } else if (t.status === "cancelled") {
                  finish("已取消 AI 触发器核验任务", null, true);
                } else if (t.status === "failed") {
                  finish(t.error || "AI 触发器核验失败", null, true);
                } else {
                  finish("核验进度连接已断开，后台任务可能仍在运行，请稍后刷新哨兵查看结果", null, true);
                }
              })
              .catch(() => {
                finish("核验进度连接已断开，请稍后刷新哨兵查看结果", null, true);
              });
          }
        };
      } catch (ex) {
        if (!mountedRef.current) return;
        setVerifyBusy(false);
        setVerifyTaskId(null);
        setVerifyMessage(null);
        setVerifyError(errorMessage(ex));
      }
    },
    [loadAll, onPortfolioMutated, verifyBusy],
  );

  const handleCancelVerify = useCallback(async () => {
    if (!verifyTaskId) return;
    try {
      await api.cancelTask(verifyTaskId);
    } catch {
      /* ignore */
    }
  }, [verifyTaskId]);

  const sectorEntries = Object.entries(summary?.sector_slots ?? {});
  const macroEntries = Object.entries(summary?.macro_factor_concentration ?? {});

  const riskUtilPct = useMemo(() => {
    if (!summary || !summary.max_total_open_risk_cap || summary.max_total_open_risk_cap <= 0) return 0;
    return Math.min(100, Math.max(0, (summary.total_open_risk_amount / summary.max_total_open_risk_cap) * 100));
  }, [summary]);

  const maxRiskCapPct = useMemo(() => {
    if (!summary || !summary.equity || summary.equity <= 0 || !summary.max_total_open_risk_cap) return null;
    return (summary.max_total_open_risk_cap / summary.equity) * 100;
  }, [summary]);

  const isPortfolioLoading = loading && portfolio === null;
  const isSentinelLoading = loading && sentinel === null;
  const hasPortfolioError = !loading && portfolio === null && Boolean(error);
  const hasSentinelError = !loading && sentinel === null && Boolean(error);

  return (
    <div className="flex flex-col gap-4">
      {/* Compact Precision Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="heading-page m-0 text-lg text-ink md:text-xl">哨兵总控与持仓纪律台</h1>
          <p className="m-0 mt-0.5 text-xs text-muted">
            每日巡检 30min/日线防线、核心触发器与 14 天事件窗口
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="chip"
            aria-pressed={includeWatchlist}
            onClick={() => setIncludeWatchlist((v) => !v)}
          >
            <span>巡检观察池</span>
          </button>
          {portfolio?.initialized ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setOpenSeed(undefined);
                setOpenPosModal(true);
              }}
            >
              <PlusCircle className="h-3.5 w-3.5" aria-hidden="true" />
              <span>新增持仓</span>
            </button>
          ) : null}
          {totalVerifiableTriggers > 0 ? (
            <button
              type="button"
              className="btn"
              disabled={verifyBusy}
              title="使用本机 AI 联网核验全部持仓中未完成的政策/非标触发器"
              onClick={() => void runAiTriggerVerify()}
            >
              <Sparkles className={`h-3.5 w-3.5 text-accent ${verifyBusy ? "animate-pulse" : ""}`} aria-hidden="true" />
              <span>{verifyBusy ? "AI 核验中…" : `AI 联网核验 (${totalVerifiableTriggers})`}</span>
            </button>
          ) : null}
          <button type="button" className="btn" disabled={loading} onClick={() => void loadAll(true)}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "巡检中…" : "立即重跑哨兵"}</span>
          </button>
        </div>
      </div>

      {actionBanner ? (
        <div
          role="status"
          className="card flex flex-wrap items-center justify-between gap-2 border-bull/45 bg-bull/10 px-4 py-2.5 text-xs text-ink"
        >
          <div className="flex items-center gap-2">
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull" aria-hidden="true" />
            <span>{actionBanner}</span>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm text-muted"
            onClick={() => setActionBanner(null)}
          >
            关闭
          </button>
        </div>
      ) : null}

      {verifyBusy || verifyMessage || verifyError ? (
        <div
          role={verifyError ? "alert" : "status"}
          className={`card flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-xs text-ink ${
            verifyError ? "border-critical/50 bg-critical/10" : "border-accent/40 bg-accent/10"
          }`}
        >
          <div className="flex items-center gap-2">
            <Sparkles className={`h-3.5 w-3.5 shrink-0 ${verifyError ? "text-critical" : "text-accent"}`} aria-hidden="true" />
            <span>
              <strong>{verifyTargetLabel ? `[${verifyTargetLabel}] ` : ""}</strong>
              {verifyError || verifyMessage}
            </span>
          </div>
          {verifyBusy && verifyTaskId ? (
            <button type="button" className="btn btn-sm" onClick={() => void handleCancelVerify()}>
              取消核验
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-ghost btn-sm text-muted"
              onClick={() => {
                setVerifyMessage(null);
                setVerifyError(null);
              }}
            >
              关闭
            </button>
          )}
        </div>
      ) : null}

      {error ? (
        <div
          role="alert"
          className="card flex flex-wrap items-center justify-between gap-3 border-critical/55 bg-critical/12 px-4 py-3 text-xs text-ink"
        >
          <div className="flex min-w-0 flex-1 items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-critical" aria-hidden="true" />
            <div className="min-w-0">
              <div className="font-semibold text-ink">哨兵总控连接异常（后端服务可能正在重启或网络中断）</div>
              <div className="mt-0.5 break-words text-ink-2">{cleanSentinelText(error)}</div>
            </div>
          </div>
          <button
            type="button"
            className="btn btn-sm btn-primary shrink-0"
            disabled={loading}
            onClick={() => void loadAll(true)}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "重连中…" : "重新连接 / 重试巡检"}</span>
          </button>
        </div>
      ) : null}

      {/* Unified Hairline KPI Summary Bar (replaces 4 separate boxes + pill soup) */}
      <section
        className="card grid grid-cols-1 divide-y divide-hairline sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4 lg:divide-x"
        aria-label="账户风险预算与哨兵统计指标"
      >
        <div className="flex flex-col justify-between p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="section-title">账户净值 & 风险缩放</span>
            {summary ? (
              <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.2 font-mono text-[10px] text-muted">
                {summary.base_currency}
              </span>
            ) : null}
          </div>
          <div className="kpi-value mt-2 text-ink">
            {summary
              ? fmtMoney(summary.equity, summary.base_currency)
              : isPortfolioLoading
                ? "连接账本中…"
                : hasPortfolioError
                  ? "连接中断"
                  : "未初始化"}
          </div>
          <div className="tnum mt-2 text-[11.5px] text-muted">
            {summary
              ? `回撤 ${fmtPct(summary.drawdown_pct, 2)} · 仓位档 ${fmtPct(summary.risk_scale * 100, 0)}`
              : isPortfolioLoading
                ? "正在同步本地持仓账本…"
                : hasPortfolioError
                  ? "服务暂不可达，请点击上方重新连接"
                  : "初始化后启用风险预算"}
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="section-title">总在途风险敞口</span>
            {summary ? (
              <span
                className="tnum rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 font-mono text-[10.5px] text-ink-2"
                title="已用在途风险占账户总风险上限的比例"
              >
                占上限 {fmtPct(riskUtilPct, 0)}
                {maxRiskCapPct != null ? ` · 上限 ${fmtPct(maxRiskCapPct, 0)}` : ""}
              </span>
            ) : null}
          </div>
          <div className="kpi-value mt-2 text-ink">
            {summary ? fmtPct(summary.total_open_risk_pct, 2) : isPortfolioLoading ? "…" : "—"}
          </div>
          <div className="mt-2 space-y-1.5">
            {summary ? (
              <div className="risk-meter" aria-hidden="true">
                <span style={{ inlineSize: `${riskUtilPct}%` }} />
              </div>
            ) : null}
            <div className="tnum text-[11.5px] text-muted">
              {summary
                ? `已用 ${fmtMoney(summary.total_open_risk_amount, summary.base_currency)} / 上限 ${fmtMoney(summary.max_total_open_risk_cap, summary.base_currency)}`
                : isPortfolioLoading
                  ? "正在计算在途风险敞口…"
                  : hasPortfolioError
                    ? "暂无法获取风险敞口"
                    : "过保本自动释放风险额"}
            </div>
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="section-title">单板块龙头风险上限</div>
          <div className="kpi-value mt-2 text-ink">
            {summary ? fmtMoney(summary.single_sector_risk_cap, summary.base_currency) : isPortfolioLoading ? "…" : "—"}
          </div>
          <div className="tnum mt-2 text-[11.5px] text-muted">
            {summary
              ? `在途 ${summary.open_positions_count} 笔 · 占用 ${sectorEntries.length} 个主驱动板块`
              : isPortfolioLoading
                ? "正在同步板块席位…"
                : hasPortfolioError
                  ? "暂无法获取板块席位"
                  : "每板块限 1 只真龙头"}
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="section-title">今日红 / 黄 / 绿哨兵计数</div>
          <div className="mt-2 grid grid-cols-3 gap-2">
            <div>
              <div className="kpi-value text-critical">
                {sentinel ? (sentinel.summary?.red_count ?? 0) : isSentinelLoading ? "…" : "—"}
              </div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-critical" aria-hidden="true" />
                <span>撤离</span>
              </div>
            </div>
            <div>
              <div className="kpi-value text-warning">
                {sentinel ? (sentinel.summary?.yellow_count ?? 0) : isSentinelLoading ? "…" : "—"}
              </div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden="true" />
                <span>警戒</span>
              </div>
            </div>
            <div>
              <div className="kpi-value text-bull">
                {sentinel ? (sentinel.summary?.green_count ?? 0) : isSentinelLoading ? "…" : "—"}
              </div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-bull" aria-hidden="true" />
                <span>持有</span>
              </div>
            </div>
          </div>
          <div className="tnum mt-1.5 font-mono text-[11px] text-muted">
            {sentinel?.checked_at_utc
              ? `巡检 ${shortTime(sentinel.checked_at_utc)} UTC`
              : isSentinelLoading
                ? "正在连接账本与哨兵巡检…"
                : hasSentinelError
                  ? "巡检连接中断，待重试"
                  : "等待巡检…"}
          </div>
        </div>
      </section>

      {/* Uninitialized Portfolio Onboarding Card */}
      {portfolio && !portfolio.initialized ? (
        <section className="callout-editorial flex flex-wrap items-center justify-between gap-4 p-4">
          <div className="max-w-2xl">
            <div className="flex items-center gap-2 text-sm font-semibold text-ink">
              <Shield className="h-4 w-4 text-accent" aria-hidden="true" />
              <span>尚未初始化本地持仓账本（portfolio/）</span>
            </div>
            <p className="m-0 mt-1 text-xs leading-relaxed text-ink-2">
              初始化后即可启用单一板块龙头独占风控、原子换仓、保本自动释放风险预算与宏大因子叠加监控。
            </p>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setInitOpen(true)}>
            初始化持仓账本
          </button>
        </section>
      ) : null}

      {/* Red / Yellow / Green Sentinel Holdings Matrix */}
      <section className="flex flex-col gap-2.5" aria-label="红黄绿持仓哨兵矩阵">
        <div className="flex items-center justify-between gap-2">
          <span className="section-title">
            在途持仓每日必检矩阵（
            {isSentinelLoading
              ? "巡检加载中…"
              : hasSentinelError
                ? "连接异常"
                : `${sentinel?.holdings?.length ?? 0} 笔`}
            ）
          </span>
          {sentinel?.discovery_errors && sentinel.discovery_errors.length > 0 ? (
            <span className="text-xs text-warning">
              发现提示：{sentinel.discovery_errors.map((e) => cleanSentinelText(e)).join("；")}
            </span>
          ) : null}
        </div>

        {isSentinelLoading ? (
          <div className="card flex flex-col gap-3 p-6">
            <div className="flex items-center gap-2 text-xs font-medium text-ink-2">
              <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden="true" />
              <span>正在连接账本与哨兵巡检引擎，拉取 30min/日线缠论防线与高频触发器…</span>
            </div>
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2" aria-hidden="true">
              <div className="h-36 animate-pulse rounded-lg border border-hairline-soft bg-sunken/70" />
              <div className="h-36 animate-pulse rounded-lg border border-hairline-soft bg-sunken/70" />
            </div>
          </div>
        ) : hasSentinelError ? (
          <div className="card flex flex-wrap items-center justify-between gap-3 border-warning/45 bg-warning/10 p-5 text-xs text-ink">
            <div className="space-y-1">
              <div className="font-semibold text-ink">暂无法获取持仓哨兵巡检结果</div>
              <div className="text-ink-2">
                后端服务可能正在重启或网络连接中断，当前不会误判为空仓或未初始化。
              </div>
            </div>
            <button
              type="button"
              className="btn btn-sm btn-primary shrink-0"
              disabled={loading}
              onClick={() => void loadAll(true)}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              <span>重新连接 / 重试巡检</span>
            </button>
          </div>
        ) : (sentinel?.holdings?.length ?? 0) === 0 ? (
          <div className="card p-6 text-center text-xs text-muted">
            暂无在途持仓标的。可在「缠论战情室」完成四象限算仓后录入持仓，或点击右上角「新增持仓」。
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {(sentinel?.holdings ?? []).map((h) => {
              const cardKey = `${h.symbol}-${h.position_id ?? "arch"}`;
              const pos = findPosition(h);
              const sug = h.suggested_trail_stop;
              const canTrail = Boolean(pos && sug?.suggested_stop != null);
              const isScaleInUnprofitable =
                typeof h.unrealized_pnl_pct === "number" &&
                Number.isFinite(h.unrealized_pnl_pct) &&
                h.unrealized_pnl_pct <= 0;
              const edgeCls =
                h.status === "RED_EXIT"
                  ? "border-l-[3px] border-l-critical"
                  : h.status === "YELLOW_LOOSENING"
                    ? "border-l-[3px] border-l-warning"
                    : "border-l-[3px] border-l-bull";

              const prioritizedAlerts = [
                ...h.red_alerts.map((alert, idx) => ({
                  key: `r-${idx}`,
                  severity: "red" as const,
                  alert,
                })),
                ...h.yellow_alerts.map((alert, idx) => ({
                  key: `y-${idx}`,
                  severity: "yellow" as const,
                  alert,
                })),
              ];
              const MAX_COLLAPSED_ALERTS = 2;
              const isAlertsExpanded = Boolean(expandedAlerts[cardKey]);
              const hiddenAlertsCount = Math.max(0, prioritizedAlerts.length - MAX_COLLAPSED_ALERTS);
              const visibleAlerts = isAlertsExpanded
                ? prioritizedAlerts
                : prioritizedAlerts.slice(0, MAX_COLLAPSED_ALERTS);
              const visibleInfoNotes = isAlertsExpanded
                ? h.info_notes
                : prioritizedAlerts.length >= MAX_COLLAPSED_ALERTS
                  ? []
                  : h.info_notes.slice(0, 2);
              const hiddenInfoCount = Math.max(0, h.info_notes.length - visibleInfoNotes.length);
              const canToggleAlerts = hiddenAlertsCount > 0 || hiddenInfoCount > 0 || isAlertsExpanded;

              return (
                <article
                  key={cardKey}
                  className={`card flex flex-col gap-3 p-4 ${edgeCls}`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <SymbolLabel symbol={h.symbol} name={h.name} />
                        <DirectionBadge direction={h.direction} />
                        {h.sector_theme ? (
                          <span className="rounded border border-hairline-soft bg-sunken px-2 py-0.5 text-[11px] text-ink-2">
                            {h.sector_theme}
                          </span>
                        ) : null}
                      </div>
                      {h.action_headline ? (
                        <p className="m-0 mt-1 text-xs font-medium text-ink-2">
                          {cleanSentinelText(h.action_headline)}
                        </p>
                      ) : null}
                    </div>
                    <SentinelStatusBadge status={h.status} labelZh={h.status_zh} />
                  </div>

                  {/* Clean Hairline Metric Strip (replaces nested box) */}
                  <div className="metric-strip grid-cols-2 sm:grid-cols-5">
                    <div className="metric-cell">
                      <span className="metric-label">最新价</span>
                      <span className="metric-val">{fmtPrice(h.last_price)}</span>
                    </div>
                    <div className="metric-cell">
                      <span className="metric-label">入场均价</span>
                      <span className="metric-val text-ink-2">{fmtPrice(h.entry_price)}</span>
                    </div>
                    <div className="metric-cell">
                      <span className="metric-label">浮动盈亏</span>
                      <span className={`metric-val ${signedToneClass(h.unrealized_pnl_pct)}`}>
                        {fmtPct(h.unrealized_pnl_pct, 2, true)}
                      </span>
                    </div>
                    <div className="metric-cell">
                      <span className="metric-label">30m 战术止损</span>
                      <span className="metric-val text-bear">{fmtPrice(h.tactical_stop_30m)}</span>
                    </div>
                    <div className="metric-cell">
                      <span className="metric-label">日线结构位</span>
                      <span className="metric-val text-ink-2">{fmtPrice(h.structural_stop_daily)}</span>
                    </div>
                  </div>

                  {/* Alerts (prioritized & collapsible when > 2) */}
                  {prioritizedAlerts.length > 0 || h.info_notes.length > 0 ? (
                    <div className="space-y-1.5 text-xs">
                      {visibleAlerts.map(({ key, severity, alert: a }) => {
                        const msgText = cleanSentinelText(a.message || a.detail || a.code);
                        const detailText =
                          a.detail && a.detail !== a.message ? cleanSentinelText(a.detail) : "";
                        if (severity === "red") {
                          return (
                            <div
                              key={key}
                              className="flex items-start gap-1.5 rounded border border-critical/45 bg-critical/10 px-2.5 py-1.5 text-ink"
                            >
                              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-critical" aria-hidden="true" />
                              <div className="min-w-0 flex-1">
                                <div>{msgText}</div>
                                {detailText && detailText !== msgText ? (
                                  <div className="mt-0.5 break-words text-[11px] text-muted">{detailText}</div>
                                ) : null}
                              </div>
                            </div>
                          );
                        }
                        const canAiVerifyHere =
                          a.code === "unevaluated_triggers" &&
                          Array.isArray(h.verifiable_trigger_ids) &&
                          h.verifiable_trigger_ids.length > 0;
                        return (
                          <div
                            key={key}
                            className="flex items-start justify-between gap-2 rounded border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-ink"
                          >
                            <div className="flex min-w-0 flex-1 items-start gap-1.5">
                              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                              <div className="min-w-0 flex-1">
                                <div>{msgText}</div>
                                {detailText && detailText !== msgText ? (
                                  <div className="mt-0.5 break-words text-[11px] text-muted">{detailText}</div>
                                ) : null}
                              </div>
                            </div>
                            {canAiVerifyHere ? (
                              <button
                                type="button"
                                className="btn btn-sm shrink-0"
                                disabled={verifyBusy}
                                title="使用 AI 联网核验未完成的政策/非标触发器"
                                onClick={() =>
                                  void runAiTriggerVerify({
                                    symbol: h.symbol,
                                    market: h.market ?? null,
                                    triggerIds: h.verifiable_trigger_ids,
                                    label: h.name || h.symbol,
                                  })
                                }
                              >
                                <Sparkles className="h-3 w-3 text-accent" aria-hidden="true" />
                                <span>{verifyBusy ? "核验中…" : "AI 核验"}</span>
                              </button>
                            ) : null}
                          </div>
                        );
                      })}
                      {visibleInfoNotes.map((n, i) => (
                        <div key={`i-${i}`} className="text-[11px] text-muted">
                          • {cleanSentinelText(n)}
                        </div>
                      ))}
                      {canToggleAlerts ? (
                        <button
                          type="button"
                          className="inline-flex cursor-pointer items-center gap-1 rounded border border-hairline-soft bg-sunken px-2.5 py-1 text-[11px] font-medium text-ink-2 transition-colors hover:bg-elevated hover:text-ink"
                          aria-expanded={isAlertsExpanded}
                          onClick={() =>
                            setExpandedAlerts((prev) => ({ ...prev, [cardKey]: !prev[cardKey] }))
                          }
                        >
                          {isAlertsExpanded ? (
                            <>
                              <span>收起告警</span>
                              <ChevronUp className="h-3 w-3" aria-hidden="true" />
                            </>
                          ) : (
                            <>
                              <span>
                                {hiddenAlertsCount > 0
                                  ? `展开其余 ${hiddenAlertsCount} 条告警${hiddenInfoCount > 0 ? `及 ${hiddenInfoCount} 条提示` : ""}`
                                  : `展开其余 ${hiddenInfoCount} 条提示`}
                              </span>
                              <ChevronDown className="h-3 w-3" aria-hidden="true" />
                            </>
                          )}
                        </button>
                      ) : null}
                    </div>
                  ) : null}

                  {/* Suggested Trailing Stop Banner */}
                  {sug?.suggested_stop != null ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-bull/35 bg-bull/10 px-3 py-2 text-xs text-ink">
                      <span>
                        <strong>浮盈推保护建议：</strong>将 30m 止损收紧至{" "}
                        <strong className="font-mono">{fmtPrice(sug.suggested_stop)}</strong>
                        {sug.rationale ? `（${cleanSentinelText(sug.rationale)}）` : ""}
                      </span>
                      {canTrail && pos ? (
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          onClick={() =>
                            setTrailTarget({
                              positionId: pos.position_id,
                              symbol: h.symbol,
                              currentStop: h.tactical_stop_30m,
                              suggestedStop: sug.suggested_stop,
                              currentPrice: h.last_price,
                              rationale: sug.rationale,
                            })
                          }
                        >
                          <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>一键上移止损</span>
                        </button>
                      ) : null}
                    </div>
                  ) : null}

                  {/* Actions Footer */}
                  <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <button type="button" className="btn btn-sm" onClick={() => onJumpWarRoom(h.symbol)}>
                        <Crosshair className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                        <span>战情室</span>
                      </button>
                      <button type="button" className="btn btn-sm" onClick={() => onJumpArchive(h.symbol, h.market)}>
                        <BookOpen className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                        <span>研究档案</span>
                      </button>
                    </div>

                    {pos ? (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() =>
                            setTrailTarget({
                              positionId: pos.position_id,
                              symbol: h.symbol,
                              currentStop: pos.tactical_stop_30m,
                              suggestedStop: sug?.suggested_stop ?? null,
                              currentPrice: h.last_price,
                            })
                          }
                        >
                          调止损
                        </button>
                        <button
                          type="button"
                          className={`btn btn-sm${isScaleInUnprofitable ? " text-muted opacity-65" : ""}`}
                          title={
                            isScaleInUnprofitable
                              ? "当前处于浮亏/平价，纪律禁止摊平（需严格浮盈确认后加仓；点击可查看纪律说明或输入更高触发价预演）"
                              : "严格浮盈确认后执行 T2/T3 顺势加仓，支持同步收紧 30m 保护止损"
                          }
                          onClick={() => setScaleInTarget({ position: pos, currentPrice: h.last_price })}
                        >
                          <PlusCircle
                            className={`h-3 w-3 ${isScaleInUnprofitable ? "text-muted" : "text-bull"}`}
                            aria-hidden="true"
                          />
                          <span>浮盈加仓</span>
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setTrimTarget({ position: pos, lastPrice: h.last_price })}
                        >
                          <Scissors className="h-3 w-3 text-muted" aria-hidden="true" />
                          <span>减仓</span>
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          title="原龙头掉队时原子切换至同板块新龙头"
                          onClick={() => setRotateTarget({ position: pos, lastPrice: h.last_price })}
                        >
                          <ArrowLeftRight className="h-3 w-3 text-muted" aria-hidden="true" />
                          <span>换龙头</span>
                        </button>
                        {canVoidMistakenPosition(pos) ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            title="误填价格或手数时无损作废重填（不结算虚假盈亏、不扣减本金）"
                            onClick={() => setVoidTarget(pos)}
                          >
                            <RotateCcw className="h-3 w-3 text-muted" aria-hidden="true" />
                            <span>误录重填</span>
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          onClick={() =>
                            setCloseTarget({
                              position: pos,
                              lastPrice: h.last_price,
                              reason: cleanSentinelText(h.red_alerts[0]?.message || h.action_headline),
                            })
                          }
                        >
                          撤离平仓
                        </button>
                      </div>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* Sector Leader Exclusivity Table & 14-Day Event Window */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        <section className="flex flex-col gap-2.5 xl:col-span-7" aria-label="单一板块龙头独占占用表">
          <span className="section-title">单一板块龙头独占占用表 & 跨板块宏大因子浓度</span>
          <div tabIndex={0} role="region" aria-label="板块龙头席位与风险占用明细表" className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">板块 / 主驱动主题</th>
                  <th scope="col">在途龙头标的</th>
                  <th scope="col">席位角色</th>
                  <th scope="col" className="num">
                    已用风险
                  </th>
                  <th scope="col" className="num">
                    板块上限
                  </th>
                  <th scope="col">宏大因子标签</th>
                </tr>
              </thead>
              <tbody>
                {sectorEntries.length === 0 ? (
                  <tr>
                    <th scope="row">
                      {isPortfolioLoading ? "加载中…" : hasPortfolioError ? "连接异常" : "暂无占用"}
                    </th>
                    <td data-label="在途龙头标的">—</td>
                    <td data-label="席位角色">
                      {isPortfolioLoading
                        ? "正在同步板块席位…"
                        : hasPortfolioError
                          ? "服务暂不可达，请重试连接"
                          : "空闲（每板块限 1 龙头或 2 双寡头）"}
                    </td>
                    <td data-label="已用风险" className="num">
                      {summary ? "0" : "—"}
                    </td>
                    <td data-label="板块上限" className="num">
                      {summary ? fmtMoney(summary.single_sector_risk_cap, summary.base_currency) : "—"}
                    </td>
                    <td data-label="宏大因子标签">—</td>
                  </tr>
                ) : (
                  sectorEntries.map(([theme, slot]) => {
                    const rolePos = slot.symbols
                      .map((s) => openPositionsBySym.get(s.toUpperCase()))
                      .filter((x): x is Position => Boolean(x));
                    const rawRoles = Array.from(new Set(rolePos.map((p) => p.leader_role || "sole_leader")));
                    const primaryRole = rawRoles.includes("co_leader") ? "co_leader" : (rawRoles[0] ?? "sole_leader");
                    const roleLabel = fmtLeaderRole(primaryRole, slot.count, slot.max_slots);
                    const macros = Array.from(new Set(rolePos.flatMap((p) => p.macro_factors ?? [])));
                    return (
                      <tr key={theme}>
                        <th scope="row">{theme}</th>
                        <td data-label="在途龙头标的">
                          <div className="flex flex-wrap gap-1">
                            {slot.symbols.map((sym) => {
                              const zh = getSymbolName(sym);
                              return (
                                <button
                                  key={sym}
                                  type="button"
                                  className="chip"
                                  onClick={() => onJumpWarRoom(sym)}
                                >
                                  <span>{zh || sym}</span>
                                  {zh ? <span className="font-mono text-[10.5px] text-muted">{sym}</span> : null}
                                  <ArrowUpRight className="h-3 w-3 text-muted" aria-hidden="true" />
                                </button>
                              );
                            })}
                          </div>
                        </td>
                        <td data-label="席位角色">{roleLabel}</td>
                        <td data-label="已用风险" className="num">
                          {fmtMoney(slot.open_risk_amount, summary?.base_currency)}
                        </td>
                        <td data-label="板块上限" className="num">
                          {fmtMoney(slot.sector_risk_cap, summary?.base_currency)}
                        </td>
                        <td data-label="宏大因子标签" className="text-muted">
                          {macros.length > 0 ? macros.join(", ") : "—"}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {macroEntries.length > 0 ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-muted">宏大因子暴露：</span>
              {macroEntries.map(([factor, m]) => (
                <StatusBadge
                  key={factor}
                  tone={m.overexposed ? "critical" : "info"}
                  label={`${factor} · ${fmtPct(m.open_risk_pct, 2)} (${m.symbols.join(",")})`}
                />
              ))}
            </div>
          ) : null}
        </section>

        {/* 14-Day Events & Rollover Timeline */}
        <section className="flex flex-col gap-2.5 xl:col-span-5" aria-label="未来 14 天事件与移仓窗口">
          <span className="section-title inline-flex items-center gap-1.5">
            <CalendarClock className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
            未来 14 天财报 / 议息 / 主力移仓展期窗口
          </span>
          <div className="card divide-y divide-hairline">
            {allUpcomingEvents.length === 0 ? (
              <div className="p-5 text-center text-xs text-muted">
                未来 14 天内暂无已登记的财报、央行议息或近月期货移仓窗口事件。
              </div>
            ) : (
              allUpcomingEvents.map((ev, idx) => (
                <div key={`${ev.symbol}-${ev.date}-${idx}`} className="flex items-center justify-between gap-3 px-3.5 py-2.5 text-xs">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        className="cursor-pointer border-0 bg-transparent p-0 text-left hover:underline"
                        onClick={() => onJumpWarRoom(ev.symbol)}
                      >
                        <SymbolLabel symbol={ev.symbol} size="sm" />
                      </button>
                      <span className="truncate text-ink">{ev.title}</span>
                    </div>
                    <div className="tnum mt-0.5 font-mono text-[11px] text-muted">
                      {ev.date} {ev.source ? `· ${fmtEventSource(ev.source)}` : ""}
                    </div>
                  </div>
                  <StatusBadge
                    tone={ev.days_until <= 3 || ev.imminent ? "critical" : ev.days_until <= 7 ? "warning" : "info"}
                    label={ev.days_until === 0 ? "今日" : `T-${ev.days_until}天`}
                  />
                </div>
              ))
            )}
          </div>

          {includeWatchlist && (sentinel?.watchlist?.length ?? 0) > 0 ? (
            <div className="card p-3.5">
              <div className="mb-2 text-xs font-semibold text-ink">
                观察池轻量刷新（共 {sentinel?.watchlist.length} 只 · 触发 {sentinel?.summary.watchlist_fired_count ?? 0} 只）
              </div>
              <div className="divide-y divide-hairline-soft">
                {sentinel?.watchlist.map((w) => (
                  <div key={w.symbol} className="flex items-center justify-between gap-2 py-1.5 text-xs first:pt-0 last:pb-0">
                    <button
                      type="button"
                      className="cursor-pointer border-0 bg-transparent p-0 text-left hover:underline"
                      onClick={() => onJumpWarRoom(w.symbol)}
                    >
                      <SymbolLabel symbol={w.symbol} name={w.name} size="sm" />
                    </button>
                    <StatusBadge
                      tone={(w.fired_triggers?.length ?? 0) > 0 ? "warning" : "neutral"}
                      label={
                        (w.fired_triggers?.length ?? 0) > 0
                          ? `触发 ${w.fired_triggers?.length} 项`
                          : "静默跟踪"
                      }
                    />
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </section>
      </div>

      {/* Recent Ledger Audit & Closed Trade Post-Mortem Journal */}
      {portfolio?.initialized ? (
        <section className="flex flex-col gap-2.5" aria-label="交易审计与平仓复盘流水">
          {(() => {
            const totalJournal = (portfolio.journal ?? []).length;
            const visibleJournal = [...(portfolio.journal ?? [])].reverse().slice(0, 20);
            return (
              <>
                <div className="flex items-center justify-between">
                  <span className="section-title inline-flex items-center gap-1.5">
                    <History className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                    交易审计与平仓复盘流水（显示 {visibleJournal.length} / {totalJournal} 条）
                  </span>
                  {summary ? (
                    <span className="font-mono text-[11px] text-muted">
                      累计了结 {summary.closed_positions_count} 笔
                    </span>
                  ) : null}
                </div>

                <div tabIndex={0} role="region" aria-label="持仓账本历史事务与复盘记录表" className="table-wrapper max-h-64">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">时间 (UTC)</th>
                        <th scope="col">事务动作</th>
                        <th scope="col">标的</th>
                        <th scope="col" className="num">
                          成交价 / 止损位
                        </th>
                        <th scope="col" className="num">
                          结算盈亏
                        </th>
                        <th scope="col">复盘归因与依据</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleJournal.length === 0 ? (
                        <tr>
                          <th scope="row">—</th>
                          <td data-label="事务动作">尚无交易流水</td>
                          <td data-label="标的">—</td>
                          <td data-label="成交价 / 止损位" className="num">
                            —
                          </td>
                          <td data-label="结算盈亏" className="num">
                            —
                          </td>
                          <td data-label="复盘归因与依据">建仓、推保护止损、换龙头与平仓归因将自动按序沉淀于此</td>
                        </tr>
                      ) : (
                        visibleJournal.map((j, idx) => {
                          const rawAct = String(j.action || j.event || "record");
                          const leg = typeof j.leg === "string" ? j.leg : "";
                          const legSuffix =
                            leg === "close_old_leader"
                              ? " · 平原龙头"
                              : leg === "open_new_leader"
                                ? " · 开新龙头"
                                : "";
                          const sym =
                            typeof j.symbol === "string"
                              ? j.symbol
                              : typeof j.old_symbol === "string"
                                ? `${j.old_symbol}→${String(j.new_symbol || "")}`
                                : "";
                          const px = toNum(
                            j.price ?? j.exit_price ?? j.trim_price ?? j.entry_price ?? j.new_stop_30m ?? j.stop_30m,
                          );
                          const stop30 = toNum(j.stop_30m ?? j.new_stop_30m);
                          const pnl = toNum(j.realized_pnl_account ?? j.realized_pnl);
                          const noteText = String(
                            j.exit_reason ??
                              j.reason ??
                              j.note ??
                              j.notes ??
                              (rawAct === "init_account"
                                ? `初始净值 ${fmtMoney(j.equity, String(j.base_currency || summary?.base_currency || ""))}`
                                : "—"),
                          );
                          return (
                            <tr key={`${j.timestamp_utc || idx}-${idx}`}>
                              <th scope="row" className="font-mono text-xs font-normal text-muted">
                                {shortTime(String(j.timestamp_utc || j.ts_utc || j.at_utc || ""))}
                              </th>
                              <td data-label="事务动作">
                                <StatusBadge
                                  tone={
                                    rawAct.includes("close") || leg === "close_old_leader"
                                      ? "serious"
                                      : rawAct.includes("open") || rawAct.includes("rotate")
                                        ? "info"
                                        : rawAct.includes("trail") || rawAct === "add_on_profit"
                                          ? "good"
                                          : "neutral"
                                  }
                                  label={`${JOURNAL_ACTION_ZH[rawAct] || rawAct}${legSuffix}`}
                                />
                              </td>
                              <td data-label="标的">
                                {sym && !sym.includes("→") ? (
                                  <SymbolLabel symbol={sym} size="sm" />
                                ) : (
                                  <span className="font-mono text-xs">{sym || "—"}</span>
                                )}
                              </td>
                              <td data-label="成交价 / 止损位" className="num">
                                {px !== null ? fmtPrice(px) : "—"}
                                {stop30 !== null && px !== null && stop30 !== px ? (
                                  <span className="ml-1 text-bear">/ 止 {fmtPrice(stop30)}</span>
                                ) : null}
                              </td>
                              <td
                                data-label="结算盈亏"
                                className={`num font-semibold ${pnl !== null ? signedToneClass(pnl) : "text-muted"}`}
                              >
                                {pnl !== null
                                  ? `${pnl > 0 ? "+" : ""}${fmtMoney(pnl, summary?.base_currency)}`
                                  : "—"}
                              </td>
                              <td data-label="复盘归因与依据" className="text-ink-2">
                                {cleanSentinelText(noteText)}
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              </>
            );
          })()}
        </section>
      ) : null}

      {/* Native <dialog> Modals */}
      <InitPortfolioDialog open={initOpen} onClose={() => setInitOpen(false)} onSuccess={handleMutationSuccess} />
      <OpenPositionDialog
        open={openPosModal}
        onClose={() => {
          setOpenPosModal(false);
          setOpenSeed(undefined);
        }}
        onSuccess={handleMutationSuccess}
        seed={openSeed}
        accountCurrency={summary?.base_currency}
      />
      <TrailStopDialog
        open={trailTarget !== null}
        onClose={() => setTrailTarget(null)}
        onSuccess={handleMutationSuccess}
        positionId={trailTarget?.positionId ?? ""}
        symbol={trailTarget?.symbol ?? ""}
        currentStop30m={trailTarget?.currentStop}
        suggestedStop30m={trailTarget?.suggestedStop}
        currentPrice={trailTarget?.currentPrice}
        rationale={trailTarget?.rationale}
      />
      <TrimPositionDialog
        open={trimTarget !== null}
        onClose={() => setTrimTarget(null)}
        onSuccess={handleMutationSuccess}
        position={trimTarget?.position ?? null}
        lastPrice={trimTarget?.lastPrice}
      />
      <ScaleInPositionDialog
        open={scaleInTarget !== null}
        onClose={() => setScaleInTarget(null)}
        onDone={handleScaleInDone}
        position={scaleInTarget?.position ?? null}
        currentPrice={scaleInTarget?.currentPrice}
        accountCurrency={summary?.base_currency}
      />
      <RotatePositionDialog
        open={rotateTarget !== null}
        onClose={() => setRotateTarget(null)}
        onSuccess={handleMutationSuccess}
        position={rotateTarget?.position ?? null}
        lastPrice={rotateTarget?.lastPrice}
        accountCurrency={summary?.base_currency}
      />
      <ClosePositionDialog
        open={closeTarget !== null}
        onClose={() => setCloseTarget(null)}
        onSuccess={handleMutationSuccess}
        position={closeTarget?.position ?? null}
        lastPrice={closeTarget?.lastPrice}
        defaultReason={closeTarget?.reason}
        onSwitchToVoid={(pos) => setVoidTarget(pos)}
      />
      <VoidPositionDialog
        open={voidTarget !== null}
        onClose={() => setVoidTarget(null)}
        onSuccess={handleMutationSuccess}
        position={voidTarget}
        onReopenSeed={(seed) => {
          setOpenSeed(seed);
          setOpenPosModal(true);
        }}
      />
    </div>
  );
}
