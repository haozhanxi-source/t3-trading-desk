import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Crosshair,
  Database,
  ExternalLink,
  FilePlus2,
  FileText,
  ListChecks,
  Plus,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../api/client";
import type { ArchiveDetail, ArchiveListItem, ArchiveStatus, EvidenceItem, ResearchDataResponse } from "../api/types";
import { HardDataVisuals } from "../components/archive/HardDataVisuals";
import { ModalDialog } from "../components/ui/ModalDialog";
import { DirectionBadge, StatusBadge } from "../components/ui/StatusBadge";
import { shortTime } from "../lib/format";
import { getSymbolName } from "../lib/symbols";

type ArchiveTab = "thesis" | "evidence" | "harddata" | "triggers";

interface ArchiveSelection {
  market: string | null;
  symbol: string;
}

function makeArchiveKey(market: string | null | undefined, symbol: string): string {
  return `${(market || "").trim().toUpperCase()}::${symbol.trim().toUpperCase()}`;
}

interface ArchiveViewProps {
  symbol: string;
  market?: string | null;
  onSelectSymbol: (symbol: string, market?: string | null) => void;
  onJumpWarRoom: (symbol: string) => void;
}

export function ArchiveView({ symbol, market = null, onSelectSymbol, onJumpWarRoom }: ArchiveViewProps) {
  const [archives, setArchives] = useState<ArchiveListItem[]>([]);
  const [selected, setSelected] = useState<ArchiveSelection>({ market, symbol });
  const prevPropKeyRef = useRef(makeArchiveKey(market, symbol));

  const [filterText, setFilterText] = useState("");
  const [detail, setDetail] = useState<ArchiveDetail | null>(null);
  const [notFoundSym, setNotFoundSym] = useState<string | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [tab, setTab] = useState<ArchiveTab>("thesis");
  const [levelFilter, setLevelFilter] = useState<"ALL" | "L1" | "L2" | "L3" | "L4">("ALL");

  /* Hard data lazy cache guarded by active key + AbortController + failed-key retry breaker */
  const [hardData, setHardData] = useState<ResearchDataResponse | null>(null);
  const [hardLoading, setHardLoading] = useState(false);
  const [hardErr, setHardErr] = useState<string | null>(null);
  const [failedHardKey, setFailedHardKey] = useState<string | null>(null);
  const hardAbortRef = useRef<AbortController | null>(null);
  const hardReqIdRef = useRef(0);
  const detailReqIdRef = useRef(0);

  /* Inline log append */
  const [logEntry, setLogEntry] = useState("");
  const [logBusy, setLogBusy] = useState(false);

  /* Modals */
  const [initModalOpen, setInitModalOpen] = useState(false);
  const [evidenceModalOpen, setEvidenceModalOpen] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);

  /* Sync external (market, symbol) jumps into selection tuple */
  useEffect(() => {
    const incomingKey = makeArchiveKey(market, symbol);
    if (incomingKey !== prevPropKeyRef.current) {
      prevPropKeyRef.current = incomingKey;
      const matched = archives.find(
        (a) =>
          a.symbol.toUpperCase() === symbol.trim().toUpperCase() &&
          (!market || a.market.toUpperCase() === market.trim().toUpperCase()),
      );
      setSelected({
        market: market?.trim().toUpperCase() ?? matched?.market ?? null,
        symbol: matched?.symbol ?? symbol.trim(),
      });
    }
  }, [symbol, market, archives]);

  /* Resolve market from archive catalog when initial route only had symbol */
  useEffect(() => {
    if (selected.market !== null || !selected.symbol.trim() || archives.length === 0) return;
    const matched = archives.find((a) => a.symbol.toUpperCase() === selected.symbol.trim().toUpperCase());
    if (matched) {
      setSelected((prev) =>
        prev.market === null && prev.symbol.toUpperCase() === matched.symbol.toUpperCase()
          ? { market: matched.market, symbol: matched.symbol }
          : prev,
      );
    }
  }, [archives, selected.market, selected.symbol]);

  const activeSym = selected.symbol.trim() || symbol.trim();
  const activeMarket = selected.market;
  const activeKey = makeArchiveKey(activeMarket, activeSym);
  const activeKeyRef = useRef(activeKey);
  activeKeyRef.current = activeKey;
  /* Monotonic session token bumped on every archive switch; immune to alias vs canonical symbol drift (e.g. US:SPY -> SPY) */
  const selectionTokenRef = useRef(0);

  const selectArchiveTuple = useCallback(
    (nextMarket: string | null, nextSymbol: string) => {
      const cleanSym = nextSymbol.trim().toUpperCase();
      const cleanMkt = nextMarket ? nextMarket.trim().toUpperCase() : null;
      prevPropKeyRef.current = makeArchiveKey(cleanMkt, cleanSym);
      setSelected({ market: cleanMkt, symbol: cleanSym });
      onSelectSymbol(cleanSym, cleanMkt);
    },
    [onSelectSymbol],
  );

  const loadList = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await api.archives(signal);
      const list = res.archives ?? [];
      setArchives(list);
      return list;
    } catch {
      return [];
    }
  }, []);

  const loadDetail = useCallback(
    async (
      targetSym: string,
      targetMarket: string | null | undefined,
      signal?: AbortSignal,
      expectedToken?: number,
    ) => {
      const cleanSym = targetSym.trim();
      if (!cleanSym) return;
      const token = expectedToken ?? selectionTokenRef.current;
      /* Never let a stale background reload from a previously selected archive clobber the active view */
      if (token !== selectionTokenRef.current) return;
      const reqId = ++detailReqIdRef.current;
      setLoadingDetail(true);
      setErr(null);
      setNotFoundSym(null);
      try {
        const d = await api.archive(cleanSym, targetMarket, signal);
        if (signal?.aborted || reqId !== detailReqIdRef.current || token !== selectionTokenRef.current) return;
        setDetail(d);
      } catch (ex) {
        if (signal?.aborted || (ex instanceof DOMException && ex.name === "AbortError")) return;
        if (reqId !== detailReqIdRef.current || token !== selectionTokenRef.current) return;
        if (ex instanceof ApiError && ex.status === 404) {
          setDetail(null);
          setNotFoundSym(cleanSym);
        } else {
          setErr(errorMessage(ex));
        }
      } finally {
        if (!signal?.aborted && reqId === detailReqIdRef.current && token === selectionTokenRef.current) {
          setLoadingDetail(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void loadList(ctrl.signal).then((list) => {
      if (!symbol && list.length > 0 && list[0]) {
        selectArchiveTuple(list[0].market, list[0].symbol);
      }
    });
    return () => ctrl.abort();
  }, [loadList, selectArchiveTuple, symbol]);

  /* Reset detail + abort/clear stale hard data immediately when (market, symbol) changes */
  useEffect(() => {
    const token = ++selectionTokenRef.current;
    const detailCtrl = new AbortController();
    setDetail(null);

    if (hardAbortRef.current) {
      hardAbortRef.current.abort();
      hardAbortRef.current = null;
    }
    hardReqIdRef.current += 1;
    setHardData(null);
    setHardErr(null);
    setHardLoading(false);
    setFailedHardKey(null);

    void loadDetail(activeSym, activeMarket, detailCtrl.signal, token);
    return () => {
      detailCtrl.abort();
    };
  }, [activeKey, activeSym, activeMarket, loadDetail]);

  /* Clean up any in-flight hardData request on unmount */
  useEffect(() => {
    return () => {
      hardAbortRef.current?.abort();
    };
  }, []);

  const loadHardData = useCallback(
    async (forceRefresh = false) => {
      const reqSym = activeSym;
      const reqKey = activeKey;
      if (!reqSym) return;

      if (hardAbortRef.current) {
        hardAbortRef.current.abort();
      }
      const ctrl = new AbortController();
      hardAbortRef.current = ctrl;
      const reqId = ++hardReqIdRef.current;

      if (forceRefresh) {
        setFailedHardKey(null);
      }
      setHardLoading(true);
      setHardErr(null);

      try {
        const res = await api.researchData(
          { symbol: reqSym, include_macro: true, refresh: forceRefresh },
          ctrl.signal,
        );
        if (ctrl.signal.aborted || reqId !== hardReqIdRef.current || reqKey !== activeKeyRef.current) return;
        setHardData(res);
        setFailedHardKey(null);
      } catch (ex) {
        if (ctrl.signal.aborted || (ex instanceof DOMException && ex.name === "AbortError")) return;
        if (reqId !== hardReqIdRef.current || reqKey !== activeKeyRef.current) return;
        setHardErr(errorMessage(ex));
        setFailedHardKey(reqKey);
      } finally {
        if (!ctrl.signal.aborted && reqId === hardReqIdRef.current) {
          setHardLoading(false);
        }
      }
    },
    [activeSym, activeKey],
  );

  useEffect(() => {
    if (tab === "harddata" && !hardData && !hardLoading && failedHardKey !== activeKey && activeSym) {
      void loadHardData(false);
    }
  }, [tab, hardData, hardLoading, failedHardKey, activeKey, activeSym, loadHardData]);

  const filteredArchives = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return archives;
    return archives.filter(
      (a) =>
        a.symbol.toLowerCase().includes(q) ||
        (a.name && a.name.toLowerCase().includes(q)) ||
        a.market.toLowerCase().includes(q),
    );
  }, [archives, filterText]);

  const filteredEvidence = useMemo(() => {
    const list = detail?.recent_evidence ?? [];
    if (levelFilter === "ALL") return list;
    return list.filter((e) => (e.level || "").toUpperCase() === levelFilter);
  }, [detail, levelFilter]);

  const effectiveMarket = detail?.market ?? activeMarket;

  const handleAppendLog = async (e: FormEvent) => {
    e.preventDefault();
    if (!detail || !logEntry.trim()) return;
    const targetSym = detail.symbol;
    const targetMarket = detail.market;
    const token = selectionTokenRef.current;
    setLogBusy(true);
    setErr(null);
    try {
      await api.archiveLog(targetSym, logEntry.trim(), targetMarket);
      if (selectionTokenRef.current === token) {
        setLogEntry("");
        await loadDetail(targetSym, targetMarket, undefined, token);
      }
      await loadList();
    } catch (ex) {
      if (selectionTokenRef.current === token) {
        setErr(errorMessage(ex));
      }
    } finally {
      setLogBusy(false);
    }
  };

  const handleStatusSwitch = async (nextStatus: ArchiveStatus) => {
    if (!detail || detail.state.status === nextStatus) return;
    const targetSym = detail.symbol;
    const targetMarket = detail.market;
    const targetRev = detail.state.revision;
    const token = selectionTokenRef.current;
    setStatusBusy(true);
    setErr(null);
    try {
      await api.archiveState(targetSym, { status: nextStatus }, targetRev, targetMarket);
      if (selectionTokenRef.current === token) {
        await loadDetail(targetSym, targetMarket, undefined, token);
      }
      await loadList();
    } catch (ex) {
      if (selectionTokenRef.current === token) {
        setErr(errorMessage(ex));
      }
    } finally {
      setStatusBusy(false);
    }
  };

  /* Determine active row uniquely by (market, symbol) tuple */
  const resolvedActiveMarket = useMemo(() => {
    if (activeMarket) return activeMarket.toUpperCase();
    const firstHit = filteredArchives.find((a) => a.symbol.toUpperCase() === activeSym.toUpperCase());
    return firstHit?.market.toUpperCase() ?? null;
  }, [activeMarket, activeSym, filteredArchives]);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Left Sidebar: Archive List */}
      <aside className="card flex flex-col overflow-hidden lg:col-span-3" aria-label="研究档案目录">
        <div className="flex items-center justify-between gap-2 border-b border-hairline px-3.5 py-3">
          <span className="text-sm font-semibold text-ink">研究档案库 ({archives.length})</span>
          <button type="button" className="btn btn-sm btn-primary" onClick={() => setInitModalOpen(true)}>
            <FilePlus2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>新建档案</span>
          </button>
        </div>

        <div className="border-b border-hairline p-2.5">
          <input
            className="input w-full"
            type="search"
            aria-label="筛选已建档标的"
            placeholder="筛选代码/名称…"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
        </div>

        <ul className="m-0 max-h-[68dvh] flex-1 list-none divide-y divide-hairline overflow-y-auto p-0">
          {filteredArchives.length === 0 ? (
            <li className="p-5 text-center text-xs text-muted">暂无匹配档案，可点击右上角新建</li>
          ) : (
            filteredArchives.map((a) => {
              const active =
                a.symbol.toUpperCase() === activeSym.toUpperCase() &&
                (resolvedActiveMarket === null || a.market.toUpperCase() === resolvedActiveMarket);
              const statusTone =
                a.status === "holding" ? "good" : a.status === "watchlist" ? "info" : "neutral";
              return (
                <li key={`${a.market}-${a.symbol}`}>
                  <button
                    type="button"
                    onClick={() => selectArchiveTuple(a.market, a.symbol)}
                    className={`flex w-full flex-col gap-1 px-3.5 py-2.5 text-left text-xs ${
                      active ? "bg-elevated text-ink" : "text-ink-2 hover:bg-elevated/60"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-ink">
                        {a.name || getSymbolName(a.symbol) || a.symbol}
                        {a.name || getSymbolName(a.symbol) ? (
                          <span className="font-mono text-[11px] font-normal text-muted"> · {a.symbol}</span>
                        ) : null}
                      </span>
                      <StatusBadge tone={statusTone} label={a.status} />
                    </div>
                    <div className="flex items-center justify-between gap-2 text-[11px] text-muted">
                      <span>
                        {a.market} · rev {a.revision}
                      </span>
                      <DirectionBadge direction={a.direction_bias} />
                    </div>
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </aside>

      {/* Main Content Area */}
      <div className="flex flex-col gap-4 lg:col-span-9">
        {/* Top Bar & Tabs */}
        <section className="card flex flex-col gap-3 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="m-0 text-base font-semibold text-ink">
                  {detail?.symbol || activeSym}
                  {detail?.state?.instrument?.name ? ` · ${detail.state.instrument.name}` : ""}
                </h2>
                {detail ? (
                  <>
                    <DirectionBadge direction={detail.state.direction_bias} />
                    <span className="rounded border border-hairline bg-page px-2 py-0.5 text-[11px] text-muted">
                      {detail.market} · rev {detail.state.revision} · 证据 {detail.evidence_count} 条
                    </span>
                  </>
                ) : effectiveMarket ? (
                  <span className="rounded border border-hairline bg-page px-2 py-0.5 text-[11px] text-muted">
                    {effectiveMarket}
                  </span>
                ) : null}
              </div>
              {detail?.state?.thesis_summary ? (
                <p className="m-0 mt-1 text-xs text-ink-2">{detail.state.thesis_summary}</p>
              ) : null}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {detail ? (
                <div className="flex items-center gap-1" role="group" aria-label="档案跟踪状态乐观锁切换">
                  {(["watchlist", "holding", "closed"] as const).map((st) => (
                    <button
                      key={st}
                      type="button"
                      disabled={statusBusy}
                      className="chip"
                      aria-pressed={detail.state.status === st}
                      onClick={() => void handleStatusSwitch(st)}
                    >
                      {st}
                    </button>
                  ))}
                </div>
              ) : null}

              <button type="button" className="btn btn-sm" onClick={() => onJumpWarRoom(detail?.symbol || activeSym)}>
                <Crosshair className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
                <span>打开战情室</span>
              </button>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => void loadDetail(activeSym, effectiveMarket)}
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loadingDetail ? "animate-spin" : ""}`} aria-hidden="true" />
                <span>刷新</span>
              </button>
            </div>
          </div>

          {/* Sub-navigation Tabs */}
          <div className="flex flex-wrap items-center gap-1.5 border-t border-hairline pt-3" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "thesis"}
              className="chip"
              aria-pressed={tab === "thesis"}
              onClick={() => setTab("thesis")}
            >
              <FileText className="h-3.5 w-3.5" aria-hidden="true" />
              <span>核心论点 (thesis.md)</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "evidence"}
              className="chip"
              aria-pressed={tab === "evidence"}
              onClick={() => setTab("evidence")}
            >
              <ListChecks className="h-3.5 w-3.5" aria-hidden="true" />
              <span>L1~L4 证据库 ({detail?.evidence_count ?? 0})</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "harddata"}
              className="chip"
              aria-pressed={tab === "harddata"}
              onClick={() => setTab("harddata")}
            >
              <Database className="h-3.5 w-3.5" aria-hidden="true" />
              <span>硬数据与结构图表</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "triggers"}
              className="chip"
              aria-pressed={tab === "triggers"}
              onClick={() => setTab("triggers")}
            >
              <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
              <span>撤离触发器 & 未来事件 ({detail?.state?.triggers?.length ?? 0})</span>
            </button>
          </div>
        </section>

        {err ? (
          <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-3 text-xs text-ink">
            {err}
          </div>
        ) : null}

        {/* Unarchived Symbol Prompt (still allows Hard Data tab!) */}
        {notFoundSym && tab !== "harddata" ? (
          <div className="card flex flex-wrap items-center justify-between gap-3 p-5">
            <div>
              <div className="text-sm font-semibold text-ink">标的 {notFoundSym} 尚未建立本地研究档案</div>
              <p className="m-0 mt-1 text-xs text-muted">
                可一键初始化第三层/第四层专用研究模板（股票 / 大宗商品 / 外汇自动匹配），或直接切到「硬数据与结构图表」查看实时数据。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className="btn" onClick={() => setTab("harddata")}>
                先看实时硬数据
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setInitModalOpen(true)}>
                初始化 {notFoundSym} 档案
              </button>
            </div>
          </div>
        ) : null}

        {/* TAB 1: Thesis Markdown + Append Log */}
        {tab === "thesis" && detail ? (
          <div className="flex flex-col gap-4">
            <article className="card p-5">
              <div className="prose-desk">
                <Markdown remarkPlugins={[remarkGfm]}>{detail.thesis_md || "_暂无正文_"}</Markdown>
              </div>
            </article>

            <form onSubmit={handleAppendLog} className="card flex flex-col gap-2.5 p-4">
              <label className="field">
                <span className="font-semibold text-ink">只追加修订日志（Append-Only Audit Log → thesis.md）</span>
                <textarea
                  className="input"
                  rows={2}
                  placeholder="记录最新财报核验、库存/基差边际变化或红蓝对抗裁决摘要…"
                  value={logEntry}
                  onChange={(e) => setLogEntry(e.target.value)}
                />
              </label>
              <div className="flex justify-end">
                <button type="submit" className="btn btn-primary" disabled={logBusy || !logEntry.trim()}>
                  {logBusy ? "正在追加…" : "追加修订记录"}
                </button>
              </div>
            </form>
          </div>
        ) : null}

        {/* TAB 2: L1~L4 Evidence Library */}
        {tab === "evidence" && detail ? (
          <section className="card flex flex-col gap-3 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1.5">
                {(["ALL", "L1", "L2", "L3", "L4"] as const).map((lv) => (
                  <button
                    key={lv}
                    type="button"
                    className="chip"
                    aria-pressed={levelFilter === lv}
                    onClick={() => setLevelFilter(lv)}
                  >
                    {lv === "ALL" ? "全部等级" : lv}
                  </button>
                ))}
              </div>
              <button type="button" className="btn btn-sm btn-primary" onClick={() => setEvidenceModalOpen(true)}>
                <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                <span>追加证据</span>
              </button>
            </div>

            <div tabIndex={0} role="region" aria-label="L1至L4分级事实证据库表" className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">等级</th>
                    <th scope="col">指标 / 事实项</th>
                    <th scope="col" className="num">
                      数值与单位
                    </th>
                    <th scope="col">报告期 / 口径</th>
                    <th scope="col">来源定位与说明</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredEvidence.length === 0 ? (
                    <tr>
                      <th scope="row">—</th>
                      <td data-label="指标 / 事实项">暂无该等级证据</td>
                      <td data-label="数值与单位" className="num">
                        —
                      </td>
                      <td data-label="报告期 / 口径">—</td>
                      <td data-label="来源定位与说明">核心论据须有 L1（官方原表）或 L2（管理层指引/行业协会）支撑</td>
                    </tr>
                  ) : (
                    filteredEvidence.map((ev, idx) => (
                      <tr key={ev.id || idx}>
                        <th scope="row">
                          <StatusBadge
                            tone={ev.level === "L1" ? "good" : ev.level === "L2" ? "info" : "warning"}
                            label={ev.level || "L3"}
                          />
                        </th>
                        <td data-label="指标 / 事实项" className="font-medium text-ink">
                          {ev.metric || ev.kind || "事实记录"}
                        </td>
                        <td data-label="数值与单位" className="num">
                          {ev.value !== undefined && ev.value !== null ? String(ev.value) : "—"}{" "}
                          {ev.unit || ""}
                        </td>
                        <td data-label="报告期 / 口径" className="text-muted">
                          {[ev.period, ev.basis].filter(Boolean).join(" · ") || "—"}
                        </td>
                        <td data-label="来源定位与说明">
                          <div className="flex flex-col gap-0.5">
                            <span>{ev.note || ev.summary || ev.source_locator || "—"}</span>
                            {ev.source_url ? (
                              <a
                                href={ev.source_url}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-1 text-[11px] text-slot1 hover:underline"
                              >
                                <span>查看原始出处</span>
                                <ExternalLink className="h-3 w-3" aria-hidden="true" />
                              </a>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}

        {/* TAB 3: Hard Data & Asset-Specific Visuals */}
        {tab === "harddata" ? (
          <HardDataVisuals
            symbol={detail?.symbol || activeSym}
            data={hardData}
            loading={hardLoading}
            error={hardErr}
            onRefresh={() => void loadHardData(true)}
          />
        ) : null}

        {/* TAB 4: Armed Exit Triggers & Upcoming Events */}
        {tab === "triggers" && detail ? (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
            <section className="flex flex-col gap-2.5 xl:col-span-8">
              <span className="section-title">已挂载撤离红线与监控触发器（state.json）</span>
              <div tabIndex={0} role="region" aria-label="档案撤离触发器配置表" className="table-wrapper">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">维度</th>
                      <th scope="col">监控指标 (metric)</th>
                      <th scope="col">条件阈值</th>
                      <th scope="col">触发动作</th>
                      <th scope="col">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail.state.triggers ?? []).length === 0 ? (
                      <tr>
                        <th scope="row">未挂载</th>
                        <td data-label="监控指标">—</td>
                        <td data-label="条件阈值">—</td>
                        <td data-label="触发动作">—</td>
                        <td data-label="状态">可在研究建档后写入多维撤离触发器</td>
                      </tr>
                    ) : (
                      (detail.state.triggers ?? []).map((tr, i) => (
                        <tr key={tr.id || i}>
                          <th scope="row">{tr.category || tr.kind || "chanlun"}</th>
                          <td data-label="监控指标">{tr.metric || tr.description || "—"}</td>
                          <td data-label="条件阈值" className="tnum font-mono">
                            {tr.operator || ""} {tr.threshold != null ? String(tr.threshold) : ""}
                          </td>
                          <td data-label="触发动作">{tr.action || "alert"}</td>
                          <td data-label="状态">
                            <StatusBadge
                              tone={tr.status === "fired" ? "critical" : "good"}
                              label={tr.status || "armed"}
                            />
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="flex flex-col gap-2.5 xl:col-span-4">
              <span className="section-title">关键催化与风险事件日历</span>
              <div className="card divide-y divide-hairline">
                {(detail.state.events ?? []).length === 0 ? (
                  <div className="p-4 text-xs text-muted">暂无已登记未来催化事件</div>
                ) : (
                  (detail.state.events ?? []).map((ev, idx) => (
                    <div key={idx} className="px-3.5 py-2.5 text-xs">
                      <div className="font-medium text-ink">{ev.title}</div>
                      <div className="tnum mt-0.5 text-[11px] text-muted">{ev.date}</div>
                    </div>
                  ))
                )}
                {detail.state.updated_at_utc ? (
                  <div className="px-3.5 py-2 text-[11px] text-muted">
                    状态最后更新：{shortTime(detail.state.updated_at_utc)} UTC
                  </div>
                ) : null}
              </div>
            </section>
          </div>
        ) : null}
      </div>

      {/* Init Archive Native <dialog> */}
      <InitArchiveDialog
        open={initModalOpen}
        defaultSymbol={notFoundSym || activeSym}
        defaultMarket={effectiveMarket}
        onClose={() => setInitModalOpen(false)}
        onCreated={(newSym, createdMarket) => {
          void loadList().then((list) => {
            const matched = list.find(
              (x) =>
                x.symbol.toUpperCase() === newSym.toUpperCase() &&
                (!createdMarket || x.market.toUpperCase() === createdMarket.toUpperCase()),
            );
            const resolvedMkt = createdMarket ?? matched?.market ?? null;
            selectArchiveTuple(resolvedMkt, newSym);
            void loadDetail(newSym, resolvedMkt);
          });
        }}
      />

      {/* Append Evidence Native <dialog> */}
      <AppendEvidenceDialog
        open={evidenceModalOpen}
        symbol={detail?.symbol || activeSym}
        market={effectiveMarket}
        captureToken={() => selectionTokenRef.current}
        onClose={() => setEvidenceModalOpen(false)}
        onAppended={(targetSym, targetMkt, submittedToken) => {
          if (submittedToken === selectionTokenRef.current) {
            void loadDetail(targetSym, targetMkt, undefined, submittedToken);
          }
          void loadList();
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------
 * Init Archive Native <dialog>
 * ------------------------------------------------------------------ */
function InitArchiveDialog({
  open,
  defaultSymbol,
  defaultMarket,
  onClose,
  onCreated,
}: {
  open: boolean;
  defaultSymbol: string;
  defaultMarket?: string | null;
  onClose: () => void;
  onCreated: (symbol: string, market: string | null) => void;
}) {
  const [sym, setSym] = useState(defaultSymbol);
  const [market, setMarket] = useState(defaultMarket || "");
  const [name, setName] = useState("");
  const [subtype, setSubtype] = useState("");
  const [primaryDriver, setPrimaryDriver] = useState("");
  const [secondaryDriver, setSecondaryDriver] = useState("");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSym(defaultSymbol);
    setMarket("");
    setName("");
    setSubtype("");
    setPrimaryDriver("");
    setSecondaryDriver("");
    setSummary("");
    setErr(null);
  }, [open, defaultSymbol]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!sym.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const mkt = market.trim().toUpperCase() || undefined;
      await api.archiveInit({
        symbol: sym.trim(),
        market: mkt,
        name: name.trim(),
        subtype: subtype.trim(),
        primary_driver: primaryDriver.trim(),
        secondary_driver: secondaryDriver.trim(),
        thesis_summary: summary.trim(),
      });
      onCreated(sym.trim().toUpperCase(), mkt ?? null);
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
      title="新建标的研究档案"
      subtitle="按品种自动套用股票 1~3 年预期差 / 商品供需平衡表 / 外汇利差专用模板"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <label className="field">
            <span>标的代码 *</span>
            <input
              className="input"
              required
              autoFocus
              placeholder="如 PWR / CU0 / USDJPY=X"
              value={sym}
              onChange={(e) => setSym(e.target.value)}
            />
          </label>
          <label className="field">
            <span>市场分区（可选）</span>
            <input
              className="input"
              placeholder="自动识别 (如 US / CN_FUTURE)"
              value={market}
              onChange={(e) => setMarket(e.target.value)}
            />
          </label>
          <label className="field">
            <span>中文/英文简称</span>
            <input
              className="input"
              placeholder="如 广达服务 / 沪铜主力"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="field">
            <span>定价子类型</span>
            <select className="input" value={subtype} onChange={(e) => setSubtype(e.target.value)}>
              <option value="">自动识别</option>
              <option value="A_growth">A 成长/技术迭代</option>
              <option value="B_cyclical">B 强周期 (PE悖论)</option>
              <option value="C_cashflow">C 稳定现金流</option>
              <option value="D_turnaround">D 困境反转</option>
            </select>
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>第一层主驱动维度</span>
            <input
              className="input"
              placeholder="如 技术与产业迭代：北美电网瓶颈"
              value={primaryDriver}
              onChange={(e) => setPrimaryDriver(e.target.value)}
            />
          </label>
          <label className="field">
            <span>第一层次驱动维度（双维共振）</span>
            <input
              className="input"
              placeholder="如 产业政策与财政基建资本开支"
              value={secondaryDriver}
              onChange={(e) => setSecondaryDriver(e.target.value)}
            />
          </label>
        </div>

        <label className="field">
          <span>1Q~1Y 核心预期差一句话摘要</span>
          <textarea
            className="input"
            rows={2}
            placeholder="概括核心多空论点与关键证伪锚点…"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
          />
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
            {busy ? "正在创建…" : "创建档案"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * Append L1~L4 Evidence Native <dialog>
 * ------------------------------------------------------------------ */
function AppendEvidenceDialog({
  open,
  symbol,
  market,
  captureToken,
  onClose,
  onAppended,
}: {
  open: boolean;
  symbol: string;
  market?: string | null;
  captureToken: () => number;
  onClose: () => void;
  onAppended: (symbol: string, market: string | null | undefined, submittedToken: number) => void;
}) {
  const [level, setLevel] = useState<"L1" | "L2" | "L3" | "L4">("L1");
  const [metric, setMetric] = useState("");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [period, setPeriod] = useState("");
  const [basis, setBasis] = useState("GAAP / 官方口径");
  const [sourceUrl, setSourceUrl] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLevel("L1");
    setMetric("");
    setValue("");
    setUnit("");
    setPeriod("");
    setBasis("GAAP / 官方口径");
    setSourceUrl("");
    setNote("");
    setErr(null);
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!metric.trim()) return;
    const targetSym = symbol;
    const targetMkt = market;
    const submittedToken = captureToken();
    setBusy(true);
    setErr(null);
    try {
      const item: EvidenceItem = {
        level,
        metric: metric.trim(),
        value: value.trim(),
        unit: unit.trim() || undefined,
        period: period.trim() || undefined,
        basis: basis.trim() || undefined,
        source_url: sourceUrl.trim() || undefined,
        note: note.trim() || undefined,
      };
      await api.archiveEvidence(targetSym, [item], targetMkt);
      onAppended(targetSym, targetMkt, submittedToken);
      if (submittedToken === captureToken()) {
        onClose();
      }
    } catch (ex) {
      if (submittedToken === captureToken()) {
        setErr(errorMessage(ex));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`追加分级事实证据 · ${symbol}${market ? ` (${market})` : ""}`}
      subtitle="L1(10-K/EIA/CFTC/统计局原表) > L2(管理层指引/行业协会) > L3(卖方研报) > L4(新闻)"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field">
            <span>证据等级 *</span>
            <select
              className="input"
              value={level}
              onChange={(e) => setLevel(e.target.value as "L1" | "L2" | "L3" | "L4")}
            >
              <option value="L1">L1 官方原表 / 监管披露</option>
              <option value="L2">L2 公司指引 / 行业协会</option>
              <option value="L3">L3 卖方深度研报</option>
              <option value="L4">L4 媒体 / 渠道草根</option>
            </select>
          </label>
          <label className="field sm:col-span-2">
            <span>指标 / 事实名称 *</span>
            <input
              className="input"
              required
              autoFocus
              placeholder="如 Backlog 硬合同金额 / LME 注册仓单库存"
              value={metric}
              onChange={(e) => setMetric(e.target.value)}
            />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="field">
            <span>数值 *</span>
            <input
              className="input"
              required
              placeholder="如 34.2"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
          <label className="field">
            <span>单位</span>
            <input
              className="input"
              placeholder="如 USD B / 万吨"
              value={unit}
              onChange={(e) => setUnit(e.target.value)}
            />
          </label>
          <label className="field">
            <span>报告期</span>
            <input
              className="input"
              placeholder="如 2026Q3 / 2026-11"
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
            />
          </label>
          <label className="field">
            <span>统计口径</span>
            <input className="input" value={basis} onChange={(e) => setBasis(e.target.value)} />
          </label>
        </div>

        <label className="field">
          <span>原始出处链接 (URL)</span>
          <input
            className="input"
            type="url"
            placeholder="https://..."
            value={sourceUrl}
            onChange={(e) => setSourceUrl(e.target.value)}
          />
        </label>

        <label className="field">
          <span>摘录定位与核验备注</span>
          <input
            className="input"
            placeholder="如 Form 10-Q Note 12，剔除框架估算后硬合同同比 +18%"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
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
            {busy ? "正在写入…" : "确认写入 evidence.jsonl"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
