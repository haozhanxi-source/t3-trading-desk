import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeftRight,
  ArrowUpRight,
  BookOpen,
  CalendarClock,
  Crosshair,
  History,
  PlusCircle,
  RefreshCw,
  RotateCcw,
  Scissors,
  Shield,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { api, errorMessage } from "../api/client";
import type { PortfolioSummaryResponse, Position, SentinelHolding, SentinelResponse, UpcomingEvent } from "../api/types";
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
import {
  DirectionBadge,
  SentinelStatusBadge,
  signedToneClass,
  StatusBadge,
} from "../components/ui/StatusBadge";
import { SymbolLabel } from "../components/ui/SymbolLabel";
import { fmtMoney, fmtPct, fmtPrice, shortTime, toNum } from "../lib/format";
import { getSymbolName } from "../lib/symbols";

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

      setLoading(true);
      setError(null);
      try {
        const [pf, sn] = await Promise.all([
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
        if (ctrl.signal.aborted || reqId !== loadReqIdRef.current) return;
        setPortfolio(pf);
        setSentinel(sn);
      } catch (ex) {
        if (
          ctrl.signal.aborted ||
          (ex instanceof DOMException && ex.name === "AbortError") ||
          reqId !== loadReqIdRef.current
        ) {
          return;
        }
        setError(errorMessage(ex));
      } finally {
        if (reqId === loadReqIdRef.current) {
          if (loadAbortRef.current === ctrl) {
            loadAbortRef.current = null;
          }
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

  const sectorEntries = Object.entries(summary?.sector_slots ?? {});
  const macroEntries = Object.entries(summary?.macro_factor_concentration ?? {});

  const riskUtilPct = useMemo(() => {
    if (!summary || !summary.max_total_open_risk_cap || summary.max_total_open_risk_cap <= 0) return 0;
    return Math.min(100, Math.max(0, (summary.total_open_risk_amount / summary.max_total_open_risk_cap) * 100));
  }, [summary]);

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
          <button type="button" className="btn" disabled={loading} onClick={() => void loadAll(true)}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>{loading ? "巡检中…" : "立即重跑哨兵"}</span>
          </button>
        </div>
      </div>

      {error ? (
        <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-2.5 text-xs text-ink">
          <strong>哨兵总控加载异常：</strong> {error}
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
            {summary ? fmtMoney(summary.equity, summary.base_currency) : "未初始化"}
          </div>
          <div className="tnum mt-2 text-[11.5px] text-muted">
            {summary
              ? `回撤 ${fmtPct(summary.drawdown_pct, 2)} · 仓位档 ${fmtPct(summary.risk_scale * 100, 0)}`
              : "初始化后启用风险预算"}
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="section-title">总敞口风险占用</span>
            {summary ? <span className="font-mono text-[11px] text-muted">{fmtPct(riskUtilPct, 0)}</span> : null}
          </div>
          <div className="kpi-value mt-2 text-ink">
            {summary ? fmtPct(summary.total_open_risk_pct, 2) : "—"}
          </div>
          <div className="mt-2 space-y-1.5">
            {summary ? (
              <div className="risk-meter" aria-hidden="true">
                <span style={{ inlineSize: `${riskUtilPct}%` }} />
              </div>
            ) : null}
            <div className="tnum text-[11.5px] text-muted">
              {summary
                ? `${fmtMoney(summary.total_open_risk_amount, summary.base_currency)} / 上限 ${fmtMoney(summary.max_total_open_risk_cap, summary.base_currency)}`
                : "过保本自动释放风险额"}
            </div>
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="section-title">单板块龙头风险上限</div>
          <div className="kpi-value mt-2 text-ink">
            {summary ? fmtMoney(summary.single_sector_risk_cap, summary.base_currency) : "—"}
          </div>
          <div className="tnum mt-2 text-[11.5px] text-muted">
            {summary
              ? `在途 ${summary.open_positions_count} 笔 · 占用 ${sectorEntries.length} 个主驱动板块`
              : "每板块限 1 只真龙头"}
          </div>
        </div>

        <div className="flex flex-col justify-between p-4">
          <div className="section-title">今日红 / 黄 / 绿哨兵计数</div>
          <div className="mt-2 grid grid-cols-3 gap-2">
            <div>
              <div className="kpi-value text-critical">{sentinel?.summary?.red_count ?? 0}</div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-critical" aria-hidden="true" />
                <span>撤离</span>
              </div>
            </div>
            <div>
              <div className="kpi-value text-warning">{sentinel?.summary?.yellow_count ?? 0}</div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden="true" />
                <span>警戒</span>
              </div>
            </div>
            <div>
              <div className="kpi-value text-bull">{sentinel?.summary?.green_count ?? 0}</div>
              <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-bull" aria-hidden="true" />
                <span>持有</span>
              </div>
            </div>
          </div>
          <div className="tnum mt-1.5 font-mono text-[11px] text-muted">
            {sentinel?.checked_at_utc ? `巡检 ${shortTime(sentinel.checked_at_utc)} UTC` : "等待巡检…"}
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
        <div className="flex items-center justify-between">
          <span className="section-title">
            在途持仓每日必检矩阵（{sentinel?.holdings?.length ?? 0} 笔）
          </span>
          {sentinel?.discovery_errors && sentinel.discovery_errors.length > 0 ? (
            <span className="text-xs text-warning">发现提示：{sentinel.discovery_errors.join("；")}</span>
          ) : null}
        </div>

        {(sentinel?.holdings?.length ?? 0) === 0 ? (
          <div className="card p-6 text-center text-xs text-muted">
            暂无 <code>holding</code> 持仓标的。可在「缠论战情室」完成四象限算仓后录入持仓，或点击右上角「新增持仓」。
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {(sentinel?.holdings ?? []).map((h) => {
              const pos = findPosition(h);
              const sug = h.suggested_trail_stop;
              const canTrail = Boolean(pos && sug?.suggested_stop != null);
              const edgeCls =
                h.status === "RED_EXIT"
                  ? "border-l-[3px] border-l-critical"
                  : h.status === "YELLOW_LOOSENING"
                    ? "border-l-[3px] border-l-warning"
                    : "border-l-[3px] border-l-bull";

              return (
                <article
                  key={`${h.symbol}-${h.position_id ?? "arch"}`}
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
                        <p className="m-0 mt-1 text-xs font-medium text-ink-2">{h.action_headline}</p>
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

                  {/* Alerts */}
                  {h.red_alerts.length > 0 || h.yellow_alerts.length > 0 || h.info_notes.length > 0 ? (
                    <div className="space-y-1.5 text-xs">
                      {h.red_alerts.map((a, i) => (
                        <div
                          key={`r-${i}`}
                          className="flex items-start gap-1.5 rounded border border-critical/45 bg-critical/10 px-2.5 py-1.5 text-ink"
                        >
                          <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-critical" aria-hidden="true" />
                          <span>{a.message || a.detail || a.code}</span>
                        </div>
                      ))}
                      {h.yellow_alerts.map((a, i) => (
                        <div
                          key={`y-${i}`}
                          className="flex items-start gap-1.5 rounded border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-ink"
                        >
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                          <span>{a.message || a.detail || a.code}</span>
                        </div>
                      ))}
                      {h.info_notes.slice(0, 2).map((n, i) => (
                        <div key={`i-${i}`} className="text-[11px] text-muted">
                          • {n}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {/* Suggested Trailing Stop Banner */}
                  {sug?.suggested_stop != null ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-bull/35 bg-bull/10 px-3 py-2 text-xs text-ink">
                      <span>
                        <strong>浮盈推保护建议：</strong>将 30m 止损收紧至{" "}
                        <strong className="font-mono">{fmtPrice(sug.suggested_stop)}</strong>
                        {sug.rationale ? `（${sug.rationale}）` : ""}
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
                              reason: h.red_alerts[0]?.message || h.action_headline,
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
                    <th scope="row">暂无占用</th>
                    <td data-label="在途龙头标的">—</td>
                    <td data-label="席位角色">空闲（每板块限 1 龙头或 2 双寡头）</td>
                    <td data-label="已用风险" className="num">
                      0
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
                    const roles = Array.from(new Set(rolePos.map((p) => p.leader_role || "sole_leader"))).join(", ");
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
                        <td data-label="席位角色">
                          {roles} ({slot.count}/{slot.max_slots})
                        </td>
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
                      {ev.date} {ev.source ? `· ${ev.source}` : ""}
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
                                        : rawAct.includes("trail")
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
                                {noteText}
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
