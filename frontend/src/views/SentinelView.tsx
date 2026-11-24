import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowUpRight,
  BookOpen,
  CalendarClock,
  CheckCircle2,
  Crosshair,
  PlusCircle,
  RefreshCw,
  Scissors,
  Shield,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { api, errorMessage } from "../api/client";
import type { PortfolioSummaryResponse, Position, SentinelHolding, SentinelResponse, UpcomingEvent } from "../api/types";
import {
  ClosePositionDialog,
  InitPortfolioDialog,
  OpenPositionDialog,
  TrailStopDialog,
  TrimPositionDialog,
} from "../components/portfolio/PortfolioDialogs";
import { DirectionBadge, SentinelStatusBadge, StatusBadge } from "../components/ui/StatusBadge";
import { fmtMoney, fmtPct, fmtPrice, shortTime } from "../lib/format";
import { formatSymbolWithCode, getSymbolName } from "../lib/symbols";

interface SentinelViewProps {
  onJumpWarRoom: (symbol: string) => void;
  onJumpArchive: (symbol: string, market?: string | null) => void;
}

export function SentinelView({ onJumpWarRoom, onJumpArchive }: SentinelViewProps) {
  const [portfolio, setPortfolio] = useState<PortfolioSummaryResponse | null>(null);
  const [sentinel, setSentinel] = useState<SentinelResponse | null>(null);
  const [includeWatchlist, setIncludeWatchlist] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /* Dialog states */
  const [initOpen, setInitOpen] = useState(false);
  const [openPosModal, setOpenPosModal] = useState(false);
  const [trailTarget, setTrailTarget] = useState<{
    positionId: string;
    symbol: string;
    currentStop?: number | null;
    suggestedStop?: number | null;
    currentPrice?: number | null;
    rationale?: string;
  } | null>(null);
  const [trimTarget, setTrimTarget] = useState<{ position: Position; lastPrice?: number | null } | null>(null);
  const [closeTarget, setCloseTarget] = useState<{
    position: Position;
    lastPrice?: number | null;
    reason?: string;
  } | null>(null);

  const loadAll = useCallback(
    async (forceRefresh = false, signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const [pf, sn] = await Promise.all([
          api.portfolioSummary(signal),
          api.sentinel(
            {
              include_watchlist: includeWatchlist,
              skip_research: false,
              event_window_days: 14,
              refresh: forceRefresh,
            },
            signal,
          ),
        ]);
        setPortfolio(pf);
        setSentinel(sn);
      } catch (ex) {
        if (ex instanceof DOMException && ex.name === "AbortError") return;
        setError(errorMessage(ex));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [includeWatchlist],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void loadAll(false, ctrl.signal);
    return () => ctrl.abort();
  }, [loadAll]);

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

  /* Aggregate 14-day upcoming events across holdings + watchlist */
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

  return (
    <div className="flex flex-col gap-4">
      {/* Top Action & Refresh Header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="m-0 text-base font-semibold text-ink">今日哨兵与持仓总控台</h2>
          <p className="m-0 text-xs text-muted">
            每日双轨必检（30min/日线缠论防线 + 核心高频触发器 + 14 天事件/移仓窗口）· 逻辑松动果断撤离
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="chip"
            aria-pressed={includeWatchlist}
            onClick={() => setIncludeWatchlist((v) => !v)}
          >
            <span>附加观察池轻量刷新</span>
          </button>
          {portfolio?.initialized ? (
            <button type="button" className="btn" onClick={() => setOpenPosModal(true)}>
              <PlusCircle className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
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
        <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-3 text-xs text-ink">
          <strong>哨兵总控加载异常：</strong> {error}
        </div>
      ) : null}

      {/* Top KPI Strip */}
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="账户风险预算与哨兵统计指标">
        <div className="card p-3.5">
          <div className="section-title">账户净值 & 回撤降仓档</div>
          <div className="kpi-value mt-1.5 text-ink">
            {summary ? fmtMoney(summary.equity, summary.base_currency) : "未初始化"}
          </div>
          <div className="tnum mt-1 text-xs text-muted">
            {summary
              ? `回撤 ${fmtPct(summary.drawdown_pct, 2)} · 风险缩放 ${fmtPct(summary.risk_scale * 100, 0)}`
              : "初始化后启用风险预算算仓"}
          </div>
        </div>

        <div className="card p-3.5">
          <div className="section-title">总敞口风险 vs 上限</div>
          <div className="kpi-value mt-1.5 text-ink">
            {summary ? fmtPct(summary.total_open_risk_pct, 2) : "—"}
          </div>
          <div className="tnum mt-1 text-xs text-muted">
            {summary
              ? `${fmtMoney(summary.total_open_risk_amount, summary.base_currency)} / 上限 ${fmtMoney(summary.max_total_open_risk_cap, summary.base_currency)}`
              : "单笔越过保本自动释放风险额"}
          </div>
        </div>

        <div className="card p-3.5">
          <div className="section-title">单板块龙头风险上限</div>
          <div className="kpi-value mt-1.5 text-ink">
            {summary ? fmtMoney(summary.single_sector_risk_cap, summary.base_currency) : "—"}
          </div>
          <div className="tnum mt-1 text-xs text-muted">
            {summary
              ? `在途 ${summary.open_positions_count} 笔 · 已占用 ${sectorEntries.length} 个主驱动板块`
              : "单一板块只允许 1 只真龙头（或双寡头共享）"}
          </div>
        </div>

        <div className="card p-3.5">
          <div className="section-title">今日红 / 黄 / 绿哨兵计数</div>
          <div className="tnum mt-2 flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-[color:color-mix(in_srgb,var(--critical)_48%,transparent)] bg-[color:color-mix(in_srgb,var(--critical)_15%,transparent)] px-2.5 py-0.5 text-xs font-medium text-ink whitespace-nowrap">
              <AlertOctagon className="h-3.5 w-3.5 shrink-0 text-critical" aria-hidden="true" />
              <span>撤离 {sentinel?.summary?.red_count ?? 0}</span>
            </span>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-[color:color-mix(in_srgb,var(--warning)_45%,transparent)] bg-[color:color-mix(in_srgb,var(--warning)_14%,transparent)] px-2.5 py-0.5 text-xs font-medium text-ink whitespace-nowrap">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
              <span>警戒 {sentinel?.summary?.yellow_count ?? 0}</span>
            </span>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-[color:color-mix(in_srgb,var(--good)_42%,transparent)] bg-[color:color-mix(in_srgb,var(--good)_13%,transparent)] px-2.5 py-0.5 text-xs font-medium text-ink whitespace-nowrap">
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-good" aria-hidden="true" />
              <span>持有 {sentinel?.summary?.green_count ?? 0}</span>
            </span>
          </div>
          <div className="tnum mt-1.5 text-xs text-muted">
            {sentinel?.checked_at_utc ? `最近巡检：${shortTime(sentinel.checked_at_utc)} UTC` : "等待巡检结果…"}
          </div>
        </div>
      </section>

      {/* Uninitialized Portfolio Onboarding Card */}
      {portfolio && !portfolio.initialized ? (
        <section className="card flex flex-wrap items-center justify-between gap-4 border-slot1/40 bg-elevated/50 p-4">
          <div className="max-w-2xl">
            <div className="flex items-center gap-2 text-sm font-semibold text-ink">
              <Shield className="h-4 w-4 text-slot1" aria-hidden="true" />
              <span>尚未初始化本地持仓账本（portfolio/）</span>
            </div>
            <p className="m-0 mt-1 text-xs text-ink-2">
              初始化后即可启用单一板块龙头独占风控、保本自动释放风险预算、移动止损只紧不松与跨板块宏大因子叠加预警。下方仍会正常巡检研究档案中标记为{" "}
              <code>holding</code> 的持仓标的。
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
            持仓每日必检矩阵（共 {sentinel?.holdings?.length ?? 0} 笔在途监控）
          </span>
          {sentinel?.discovery_errors && sentinel.discovery_errors.length > 0 ? (
            <span className="text-xs text-warning">发现提示：{sentinel.discovery_errors.join("；")}</span>
          ) : null}
        </div>

        {(sentinel?.holdings?.length ?? 0) === 0 ? (
          <div className="card p-6 text-center text-xs text-muted">
            当前暂无 <code>holding</code> 状态的持仓标的。可在「双周期战情室」完成四象限算仓后录入持仓，或在上方点击「新增持仓」。
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {(sentinel?.holdings ?? []).map((h) => {
              const pos = findPosition(h);
              const sug = h.suggested_trail_stop;
              const canTrail = Boolean(pos && sug?.suggested_stop != null);
              return (
                <article key={`${h.symbol}-${h.position_id ?? "arch"}`} className="card flex flex-col gap-3 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        {(() => {
                          const zh = h.name || getSymbolName(h.symbol);
                          return (
                            <>
                              <span className="text-base font-semibold text-ink">{zh || h.symbol}</span>
                              {zh ? <span className="font-mono text-xs text-muted">{h.symbol}</span> : null}
                            </>
                          );
                        })()}
                        <DirectionBadge direction={h.direction} />
                        {h.sector_theme ? (
                          <span className="rounded border border-hairline bg-elevated px-2 py-0.5 text-[11px] text-ink-2">
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

                  {/* Price vs Defense Stops Strip */}
                  <div className="tnum grid grid-cols-2 gap-2 rounded-lg border border-hairline bg-page/60 p-2.5 text-xs sm:grid-cols-5">
                    <div>
                      <div className="text-muted">最新价</div>
                      <div className="mt-0.5 font-semibold text-ink">{fmtPrice(h.last_price)}</div>
                    </div>
                    <div>
                      <div className="text-muted">入场均价</div>
                      <div className="mt-0.5 font-medium text-ink-2">{fmtPrice(h.entry_price)}</div>
                    </div>
                    <div>
                      <div className="text-muted">浮动盈亏</div>
                      <div className="mt-0.5 font-semibold text-ink">{fmtPct(h.unrealized_pnl_pct, 2, true)}</div>
                    </div>
                    <div>
                      <div className="text-muted">30m 战术止损</div>
                      <div className="mt-0.5 font-semibold text-critical">{fmtPrice(h.tactical_stop_30m)}</div>
                    </div>
                    <div>
                      <div className="text-muted">日线结构防线</div>
                      <div className="mt-0.5 font-medium text-serious">{fmtPrice(h.structural_stop_daily)}</div>
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
                          className="flex items-start gap-1.5 rounded border border-warning/45 bg-warning/10 px-2.5 py-1.5 text-ink"
                        >
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                          <span>{a.message || a.detail || a.code}</span>
                        </div>
                      ))}
                      {h.info_notes.slice(0, 2).map((n, i) => (
                        <div key={`i-${i}`} className="text-[11.5px] text-muted">
                          • {n}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {/* Suggested Trailing Stop Banner */}
                  {sug?.suggested_stop != null ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-good/40 bg-good/10 px-3 py-2 text-xs text-ink">
                      <span>
                        <strong>浮盈推保护建议：</strong>将 30m 止损收紧至{" "}
                        <strong className="tnum">{fmtPrice(sug.suggested_stop)}</strong>
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
                  <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-hairline pt-2.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <button type="button" className="btn btn-sm" onClick={() => onJumpWarRoom(h.symbol)}>
                        <Crosshair className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
                        <span>双周期战情室</span>
                      </button>
                      <button type="button" className="btn btn-sm" onClick={() => onJumpArchive(h.symbol, h.market)}>
                        <BookOpen className="h-3.5 w-3.5 text-slot3" aria-hidden="true" />
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
                          <Scissors className="h-3 w-3" aria-hidden="true" />
                          <span>减仓</span>
                        </button>
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
                            {slot.symbols.map((sym) => (
                              <button
                                key={sym}
                                type="button"
                                className="chip"
                                onClick={() => onJumpWarRoom(sym)}
                              >
                                <span>{sym}</span>
                                <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                              </button>
                            ))}
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
            <CalendarClock className="h-3.5 w-3.5 text-slot4" aria-hidden="true" />
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
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        className="font-semibold text-slot1 hover:underline"
                        onClick={() => onJumpWarRoom(ev.symbol)}
                      >
                        {formatSymbolWithCode(ev.symbol)}
                      </button>
                      <span className="truncate text-ink">{ev.title}</span>
                    </div>
                    <div className="tnum mt-0.5 text-[11px] text-muted">
                      日期：{ev.date} {ev.source ? `· 来源：${ev.source}` : ""}
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

          {/* Watchlist Light Refresh Section when enabled */}
          {includeWatchlist && (sentinel?.watchlist?.length ?? 0) > 0 ? (
            <div className="card p-3.5">
              <div className="mb-2 text-xs font-semibold text-ink">
                观察池轻量刷新（共 {sentinel?.watchlist.length} 只 · 触发 {sentinel?.summary.watchlist_fired_count ?? 0} 只）
              </div>
              <div className="space-y-1.5">
                {sentinel?.watchlist.map((w) => (
                  <div key={w.symbol} className="flex items-center justify-between gap-2 text-xs">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        className="font-semibold text-slot1 hover:underline"
                        onClick={() => onJumpWarRoom(w.symbol)}
                      >
                        {w.name || getSymbolName(w.symbol) || w.symbol}
                      </button>
                      <span className="font-mono text-[11px] text-muted">
                        {w.name || getSymbolName(w.symbol) ? w.symbol : w.market}
                      </span>
                    </div>
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

      {/* Native <dialog> Modals */}
      <InitPortfolioDialog open={initOpen} onClose={() => setInitOpen(false)} onSuccess={() => void loadAll(true)} />
      <OpenPositionDialog
        open={openPosModal}
        onClose={() => setOpenPosModal(false)}
        onSuccess={() => void loadAll(true)}
        accountCurrency={summary?.base_currency}
      />
      <TrailStopDialog
        open={trailTarget !== null}
        onClose={() => setTrailTarget(null)}
        onSuccess={() => void loadAll(true)}
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
        onSuccess={() => void loadAll(true)}
        position={trimTarget?.position ?? null}
        lastPrice={trimTarget?.lastPrice}
      />
      <ClosePositionDialog
        open={closeTarget !== null}
        onClose={() => setCloseTarget(null)}
        onSuccess={() => void loadAll(true)}
        position={closeTarget?.position ?? null}
        lastPrice={closeTarget?.lastPrice}
        defaultReason={closeTarget?.reason}
      />
    </div>
  );
}
