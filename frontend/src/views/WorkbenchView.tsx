import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  BookmarkPlus,
  CheckCircle2,
  Clock,
  Crosshair,
  Database,
  Play,
  Radar,
  RefreshCw,
  ShieldCheck,
  Terminal,
  XCircle,
} from "lucide-react";
import { api, errorMessage, parseTaskEvent } from "../api/client";
import type { ScanRow, TaskEventEnvelope, TaskRecord, UniversePool } from "../api/types";
import { ModalDialog } from "../components/ui/ModalDialog";
import { StatusBadge } from "../components/ui/StatusBadge";
import { fmtPct, fmtPrice, parseSymbolList, shortTime } from "../lib/format";
import { formatSymbolWithCode, getSymbolName } from "../lib/symbols";
import { formatTaskKind, formatTaskStatus, getUniverseLabel, groupUniverses } from "../lib/universes";

type LauncherMode = "scan" | "refresh" | "sentinel";

function isTerminalTaskStatus(status: string | null | undefined): boolean {
  return status === "completed" || status === "failed";
}

function mergeMonotonicTask(prev: TaskRecord | null, incoming: TaskRecord): TaskRecord {
  if (!prev || prev.id !== incoming.id) return incoming;
  if (isTerminalTaskStatus(prev.status) && !isTerminalTaskStatus(incoming.status)) {
    return {
      ...incoming,
      ...prev,
      status: prev.status,
      result: prev.result ?? incoming.result,
      error: prev.error ?? incoming.error,
      finished_at: prev.finished_at ?? incoming.finished_at,
    };
  }
  return {
    ...prev,
    ...incoming,
    result: incoming.result ?? prev.result,
    error: incoming.error ?? prev.error,
  };
}

function mergeScanRows(prev: ScanRow[], nextRows: ScanRow[]): ScanRow[] {
  if (nextRows.length === 0) return prev;
  const map = new Map<string, ScanRow>();
  for (const r of prev) {
    map.set(`${r.market || ""}::${r.symbol}`, r);
  }
  for (const r of nextRows) {
    map.set(`${r.market || ""}::${r.symbol}`, r);
  }
  return Array.from(map.values());
}

interface WorkbenchViewProps {
  defaultSymbol: string;
  onJumpWarRoom: (symbol: string) => void;
  onJumpArchive: (symbol: string) => void;
}

export function WorkbenchView({ defaultSymbol, onJumpWarRoom, onJumpArchive }: WorkbenchViewProps) {
  const [mode, setMode] = useState<LauncherMode>("scan");

  /* Universes */
  const [universes, setUniverses] = useState<Record<string, UniversePool>>({});
  const [selectedPools, setSelectedPools] = useState<string[]>(["commodities_base"]);
  const [customSymbolsText, setCustomSymbolsText] = useState("");
  const [sortMode, setSortMode] = useState<"tier" | "none">("tier");

  /* Single symbol refresh */
  const [refreshSymbol, setRefreshSymbol] = useState(defaultSymbol || "AUDUSD=X");
  const [includeMacro, setIncludeMacro] = useState(true);

  /* Sentinel task */
  const [sentinelWatchlist, setSentinelWatchlist] = useState(true);
  const [eventWindowDays, setEventWindowDays] = useState("14");

  /* Tasks & SSE stream */
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<TaskRecord | null>(null);
  const [liveEvents, setLiveEvents] = useState<TaskEventEnvelope[]>([]);
  const [scanRows, setScanRows] = useState<ScanRow[]>([]);
  const [progress, setProgress] = useState<{ done: number; total: number | null }>({ done: 0, total: null });
  const [launching, setLaunching] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  /* Save new universe modal */
  const [savePoolOpen, setSavePoolOpen] = useState(false);

  const esRef = useRef<EventSource | null>(null);

  const loadUniverses = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await api.universes(signal);
      setUniverses(res.universes ?? {});
    } catch {
      /* ignore */
    }
  }, []);

  const loadTasks = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await api.tasks(signal);
      const list = res.tasks ?? [];
      setTasks((prev) => {
        const prevById = new Map(prev.map((t) => [t.id, t]));
        return list.map((item) => mergeMonotonicTask(prevById.get(item.id) ?? null, item));
      });
      return list;
    } catch {
      return [];
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void loadUniverses(ctrl.signal);
    void loadTasks(ctrl.signal).then((list) => {
      if (list.length > 0 && list[0]) {
        setActiveTaskId((prev) => prev ?? list[0].id);
      }
    });
    return () => ctrl.abort();
  }, [loadUniverses, loadTasks]);

  /* Subscribe to SSE stream & hydrate task detail via monotonic seq-keyed deduplicator */
  useEffect(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    if (!activeTaskId) return;

    let cancelled = false;
    const detailCtrl = new AbortController();
    setActiveTask((prev) => (prev && prev.id === activeTaskId ? prev : null));
    setLiveEvents([]);
    setScanRows([]);
    setProgress({ done: 0, total: null });

    const eventsBySeq = new Map<number, TaskEventEnvelope>();
    let syntheticSeq = 1_000_000;

    const commitEvents = (incoming: TaskEventEnvelope[]): TaskEventEnvelope[] => {
      const newlyAdded: TaskEventEnvelope[] = [];
      for (const ev of incoming) {
        if (typeof ev.seq === "number" && Number.isFinite(ev.seq)) {
          if (eventsBySeq.has(ev.seq)) continue;
          eventsBySeq.set(ev.seq, ev);
          newlyAdded.push(ev);
        } else {
          eventsBySeq.set(++syntheticSeq, ev);
          newlyAdded.push(ev);
        }
      }
      if (newlyAdded.length > 0) {
        const sorted = Array.from(eventsBySeq.entries())
          .sort((a, b) => a[0] - b[0])
          .map(([, item]) => item)
          .slice(-200);
        setLiveEvents(sorted);
      }
      return newlyAdded;
    };

    const es = new EventSource(api.taskEventsUrl(activeTaskId));
    esRef.current = es;

    /* Single unified reducer applied to ALL newly accepted events (whether from detail replay OR live SSE) */
    const applyBusinessEvents = (events: TaskEventEnvelope[]) => {
      const addedRows: ScanRow[] = [];
      let sawTerminal = false;

      for (const ev of events) {
        const d = ev.data ?? {};
        const t = ev.type;

        if (t === "progress") {
          setProgress((prev) => ({
            done: typeof d.done === "number" ? Math.max(prev.done, d.done) : prev.done,
            total: d.total !== undefined ? d.total : prev.total,
          }));
        } else if (t === "row") {
          const row = d.row ?? ev.row;
          if (row && row.symbol) {
            addedRows.push(row);
          }
        } else if (t === "done") {
          sawTerminal = true;
          const finalResult = (d.result ?? ev.result) as { rows?: ScanRow[] } | undefined;
          if (finalResult && Array.isArray(finalResult.rows)) {
            setScanRows(finalResult.rows);
          }
          setActiveTask((prev) =>
            prev
              ? { ...prev, status: "completed", result: finalResult ?? prev.result }
              : { id: activeTaskId, task_id: activeTaskId, kind: "scan", status: "completed", result: finalResult },
          );
          setTasks((prev) =>
            prev.map((item) =>
              item.id === activeTaskId
                ? { ...item, status: "completed", result: finalResult ?? item.result }
                : item,
            ),
          );
        } else if (t === "error") {
          sawTerminal = true;
          const errMsg = d.message || ev.message || "任务执行失败";
          setActiveTask((prev) =>
            prev
              ? { ...prev, status: "failed", error: errMsg }
              : { id: activeTaskId, task_id: activeTaskId, kind: "scan", status: "failed", error: errMsg },
          );
          setTasks((prev) =>
            prev.map((item) => (item.id === activeTaskId ? { ...item, status: "failed", error: errMsg } : item)),
          );
        }
      }

      if (addedRows.length > 0 && !sawTerminal) {
        setScanRows((prev) => mergeScanRows(prev, addedRows));
      } else if (addedRows.length > 0 && sawTerminal) {
        setScanRows((prev) => mergeScanRows(prev, addedRows));
      }

      if (sawTerminal) {
        void loadTasks();
        es.close();
      }
    };

    /* Hydrate initial task detail without clobbering newer SSE events or swallowing event business updates */
    void api
      .task(activeTaskId, detailCtrl.signal)
      .then((detail) => {
        if (cancelled || detailCtrl.signal.aborted) return;
        setActiveTask((prev) => mergeMonotonicTask(prev, detail));
        if (detail.progress && typeof detail.progress === "object") {
          const dDone = detail.progress.done ?? 0;
          const dTotal = detail.progress.total ?? null;
          setProgress((prev) => ({
            done: Math.max(prev.done, dDone),
            total: dTotal ?? prev.total,
          }));
        }
        if (detail.events && detail.events.length > 0) {
          const added = commitEvents(detail.events);
          if (added.length > 0) {
            applyBusinessEvents(added);
          }
        }
        const resObj = detail.result as { rows?: ScanRow[] } | null;
        if (resObj && Array.isArray(resObj.rows)) {
          setScanRows((prev) => mergeScanRows(prev, resObj.rows ?? []));
        }
        if (isTerminalTaskStatus(detail.status)) {
          es.close();
        }
      })
      .catch(() => undefined);

    const handleEnvelope = (evType: string) => (msg: MessageEvent) => {
      if (cancelled || typeof msg.data !== "string") return;
      const parsed = parseTaskEvent(msg.data, evType);
      if (!parsed) return;
      const added = commitEvents([parsed]);
      if (added.length === 0) return;
      applyBusinessEvents(added);
    };

    for (const evtName of ["step", "progress", "row", "done", "error", "message"]) {
      es.addEventListener(evtName, handleEnvelope(evtName) as EventListener);
    }

    return () => {
      cancelled = true;
      detailCtrl.abort();
      es.close();
      esRef.current = null;
    };
  }, [activeTaskId, loadTasks]);

  const togglePool = (name: string) => {
    setSelectedPools((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]));
  };

  const handleLaunch = async (e: FormEvent) => {
    e.preventDefault();
    setLaunching(true);
    setErr(null);
    try {
      let created: TaskRecord;
      if (mode === "scan") {
        const customList = parseSymbolList(customSymbolsText);
        if (selectedPools.length === 0 && customList.length === 0) {
          setErr("请至少勾选 1 个主题池或输入自定义龙头代码");
          setLaunching(false);
          return;
        }
        created = await api.startScan({
          universes: selectedPools,
          symbols: customList,
          sort: sortMode,
          period: "3y",
        });
      } else if (mode === "refresh") {
        if (!refreshSymbol.trim()) {
          setErr("请输入需要刷新硬数据与校验触发器的标的代码");
          setLaunching(false);
          return;
        }
        created = await api.startResearchRefresh({
          symbol: refreshSymbol.trim(),
          include_macro: includeMacro,
        });
      } else {
        created = await api.startSentinelTask({
          include_watchlist: sentinelWatchlist,
          skip_research: false,
          event_window_days: Math.max(1, Math.min(90, Number(eventWindowDays) || 14)),
        });
      }
      setActiveTask(created);
      setActiveTaskId(created.id);
      await loadTasks();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setLaunching(false);
    }
  };

  const progressPct = useMemo(() => {
    if (activeTask?.status === "completed") return 100;
    if (progress.total && progress.total > 0) {
      return Math.min(100, Math.round((progress.done / progress.total) * 100));
    }
    return activeTask?.status === "running" ? 15 : 0;
  }, [activeTask?.status, progress]);

  const candidateSaveSymbols = useMemo(() => {
    const fromCustom = parseSymbolList(customSymbolsText);
    if (fromCustom.length > 0) return fromCustom;
    return scanRows.filter((r) => !r.error).map((r) => r.symbol);
  }, [customSymbolsText, scanRows]);

  const groupedPools = useMemo(() => groupUniverses(universes), [universes]);

  const selectedPoolDetails = useMemo(
    () =>
      selectedPools
        .map((key) => {
          const meta = universes[key];
          if (!meta) return null;
          return {
            key,
            label: getUniverseLabel(key, meta.description),
            description: meta.description || "",
            symbols: meta.symbols ?? [],
          };
        })
        .filter((x): x is { key: string; label: string; description: string; symbols: string[] } => x !== null),
    [selectedPools, universes],
  );

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
      {/* Main Column: Launcher + Live SSE Stream */}
      <div className="flex flex-col gap-4 xl:col-span-9">
        {/* Launcher Card */}
        <section className="card flex flex-col gap-3.5 p-4" aria-label="投研异步任务启动器">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-3">
            <div className="flex flex-wrap items-center gap-1.5" role="tablist">
              <button
                type="button"
                className="chip"
                aria-pressed={mode === "scan"}
                onClick={() => setMode("scan")}
              >
                <Radar className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
                <span>板块龙头缠论扫描</span>
              </button>
              <button
                type="button"
                className="chip"
                aria-pressed={mode === "refresh"}
                onClick={() => setMode("refresh")}
              >
                <Database className="h-3.5 w-3.5 text-slot3" aria-hidden="true" />
                <span>单标硬数据与触发器刷新</span>
              </button>
              <button
                type="button"
                className="chip"
                aria-pressed={mode === "sentinel"}
                onClick={() => setMode("sentinel")}
              >
                <ShieldCheck className="h-3.5 w-3.5 text-slot4" aria-hidden="true" />
                <span>全量哨兵后台巡检</span>
              </button>
            </div>

            {mode === "scan" ? (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setSavePoolOpen(true)}
                title="将自上而下推演出的新板块核心龙头沉淀至 universes.json"
              >
                <BookmarkPlus className="h-3.5 w-3.5 text-slot1" aria-hidden="true" />
                <span>沉淀新标的池</span>
              </button>
            ) : null}
          </div>

          <form onSubmit={handleLaunch} className="flex flex-col gap-3">
            {mode === "scan" ? (
              <>
                <div className="flex flex-col gap-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
                    <span>1) 选择已沉淀主题/品种池（严禁每天盲扫全市场，只对第一层共振板块验真）：</span>
                    {selectedPools.length > 0 ? (
                      <button
                        type="button"
                        className="text-[11.5px] text-slot1 underline-offset-2 hover:underline"
                        onClick={() => setSelectedPools([])}
                      >
                        清空已选 ({selectedPools.length})
                      </button>
                    ) : null}
                  </div>
                  <div className="flex flex-col gap-2">
                    {groupedPools.map((section) => (
                      <div key={section.groupKey} className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-2.5">
                        <span className="w-28 shrink-0 pt-1 text-[11px] font-medium text-muted">
                          {section.title}
                        </span>
                        <div className="flex flex-1 flex-wrap gap-1.5">
                          {section.items.map((item) => (
                            <button
                              key={item.key}
                              type="button"
                              className="chip"
                              aria-pressed={selectedPools.includes(item.key)}
                              onClick={() => togglePool(item.key)}
                              title={
                                [
                                  item.description || item.label,
                                  item.symbols.length > 0
                                    ? `包含：${item.symbols.map((s) => formatSymbolWithCode(s)).join("、")}`
                                    : "",
                                ]
                                  .filter(Boolean)
                                  .join(" · ")
                              }
                            >
                              <span>{item.label}</span>
                              <span className="text-[10.5px] text-muted">({item.symbols.length})</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>

                  {selectedPoolDetails.length > 0 ? (
                    <div className="rounded-lg border border-hairline bg-page/70 px-3 py-2 text-xs">
                      <div className="flex flex-col gap-2">
                        {selectedPoolDetails.map((pool) => (
                          <div key={pool.key} className="flex flex-col gap-1">
                            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 leading-relaxed">
                              <span className="font-semibold text-ink">{pool.label}</span>
                              {pool.description ? (
                                <span className="text-ink-2">{pool.description}</span>
                              ) : null}
                            </div>
                            {pool.symbols.length > 0 ? (
                              <div className="flex flex-wrap gap-1">
                                {pool.symbols.map((sym) => {
                                  const zh = getSymbolName(sym);
                                  return (
                                    <span
                                      key={sym}
                                      className="inline-flex items-baseline gap-1 rounded border border-hairline bg-surface/80 px-1.5 py-0.5 text-[11px]"
                                    >
                                      {zh ? (
                                        <>
                                          <span className="text-ink-2">{zh}</span>
                                          <span className="font-mono text-[10px] text-muted">{sym}</span>
                                        </>
                                      ) : (
                                        <span className="font-mono text-ink-2">{sym}</span>
                                      )}
                                    </span>
                                  );
                                })}
                              </div>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>

                <div className="grid grid-cols-1 gap-3 md:grid-cols-12">
                  <label className="field md:col-span-7">
                    <span>2) 或现场切入新细分板块真龙头（空格或逗号分隔）</span>
                    <input
                      className="input"
                      placeholder="如 NVDA、PWR、CU0（沪铜）、AUDUSD=X（澳元）"
                      value={customSymbolsText}
                      onChange={(e) => setCustomSymbolsText(e.target.value)}
                    />
                  </label>
                  <label className="field md:col-span-3">
                    <span>排序规则</span>
                    <select
                      className="input"
                      value={sortMode}
                      onChange={(e) => setSortMode(e.target.value as "tier" | "none")}
                    >
                      <option value="tier">按买卖点信号优先级</option>
                      <option value="none">保持原池顺序</option>
                    </select>
                  </label>
                  <div className="flex items-end md:col-span-2">
                    <button type="submit" className="btn btn-primary w-full" disabled={launching}>
                      <Play className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{launching ? "启动中…" : "启动扫描"}</span>
                    </button>
                  </div>
                </div>
              </>
            ) : null}

            {mode === "refresh" ? (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-12">
                <label className="field md:col-span-6">
                  <span>标的代码（运行 research_data.py 白名单路由 + 校验档案触发器）</span>
                  <input
                    className="input"
                    required
                    placeholder="如 AUDUSD=X / PWR / CU0"
                    value={refreshSymbol}
                    onChange={(e) => setRefreshSymbol(e.target.value)}
                  />
                </label>
                <div className="flex items-end md:col-span-3">
                  <label className="inline-flex h-8 cursor-pointer items-center gap-2 text-xs text-ink-2">
                    <input
                      type="checkbox"
                      checked={includeMacro}
                      onChange={(e) => setIncludeMacro(e.target.checked)}
                    />
                    <span>同步拉取中美宏观双核</span>
                  </label>
                </div>
                <div className="flex items-end md:col-span-3">
                  <button type="submit" className="btn btn-primary w-full" disabled={launching}>
                    <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{launching ? "启动中…" : "启动刷新核验"}</span>
                  </button>
                </div>
              </div>
            ) : null}

            {mode === "sentinel" ? (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-12">
                <label className="field md:col-span-4">
                  <span>事件与移仓预警窗口 (天)</span>
                  <input
                    className="input"
                    type="number"
                    min="1"
                    max="90"
                    value={eventWindowDays}
                    onChange={(e) => setEventWindowDays(e.target.value)}
                  />
                </label>
                <div className="flex items-end md:col-span-5">
                  <label className="inline-flex h-8 cursor-pointer items-center gap-2 text-xs text-ink-2">
                    <input
                      type="checkbox"
                      checked={sentinelWatchlist}
                      onChange={(e) => setSentinelWatchlist(e.target.checked)}
                    />
                    <span>同时对 watchlist 观察池执行轻量触发器巡检</span>
                  </label>
                </div>
                <div className="flex items-end md:col-span-3">
                  <button type="submit" className="btn btn-primary w-full" disabled={launching}>
                    <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{launching ? "启动中…" : "启动哨兵任务"}</span>
                  </button>
                </div>
              </div>
            ) : null}
          </form>

          {err ? (
            <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
              {err}
            </div>
          ) : null}
        </section>

        {/* Live SSE Stream & Results Panel */}
        <section className="card flex flex-col gap-3.5 p-4" aria-label="SSE 实时任务执行流">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Terminal className="h-4 w-4 text-slot1" aria-hidden="true" />
              <span className="text-sm font-semibold text-ink">
                实时执行流 {activeTask ? `· ${formatTaskKind(activeTask.kind)} #${activeTask.id.slice(0, 6)}` : ""}
              </span>
              {activeTask ? (
                <StatusBadge
                  tone={
                    activeTask.status === "completed"
                      ? "good"
                      : activeTask.status === "failed"
                        ? "critical"
                        : "info"
                  }
                  label={formatTaskStatus(activeTask.status)}
                />
              ) : null}
            </div>
            <span className="tnum text-xs text-muted">
              {progress.total != null ? `进度 ${progress.done} / ${progress.total} (${progressPct}%)` : ""}
            </span>
          </div>

          {/* Progress bar */}
          <div className="progress-bar h-1.5 w-full overflow-hidden rounded-full bg-elevated">
            <span
              className="block h-full rounded-full bg-slot1"
              style={{ inlineSize: `${progressPct}%` }}
            />
          </div>

          {/* Incremental Scan Results Table (click row -> War Room!) */}
          {scanRows.length > 0 ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between text-xs text-muted">
                <span>日线缠论定方向扫描结果（点击任意行直达「双周期战情室」下钻 30min 触发点）</span>
                <span>已返回 {scanRows.length} 只标的</span>
              </div>
              <div tabIndex={0} role="region" aria-label="板块龙头日线缠论扫描结果表" className="table-wrapper max-h-[420px]">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">标的</th>
                      <th scope="col" className="num">
                        最新收盘
                      </th>
                      <th scope="col" className="num">
                        20D 涨跌
                      </th>
                      <th scope="col">日线结构 & 中枢数</th>
                      <th scope="col" className="num">
                        最新中枢 [ZD, ZG]
                      </th>
                      <th scope="col">最新分型 & 动力学</th>
                      <th scope="col">信号层级 / 方向建议</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scanRows.map((row) => (
                      <tr
                        key={`${row.market}-${row.symbol}`}
                        className="clickable"
                        onClick={() => onJumpWarRoom(row.symbol)}
                      >
                        <th scope="row">
                          {(() => {
                            const zh = getSymbolName(row.symbol);
                            const marketLabel = row.market
                              ? getUniverseLabel(row.market, universes[row.market]?.description)
                              : null;
                            return (
                              <div className="flex flex-col gap-0.5">
                                <div className="flex items-baseline gap-1.5">
                                  <span className="font-semibold text-slot1">{zh || row.symbol}</span>
                                  {zh ? (
                                    <span className="font-mono text-[11px] font-normal text-muted">{row.symbol}</span>
                                  ) : null}
                                </div>
                                {marketLabel ? (
                                  <span className="text-[11px] font-normal text-muted">{marketLabel}</span>
                                ) : null}
                              </div>
                            );
                          })()}
                        </th>
                        {row.error ? (
                          <td colSpan={6} data-label="异常" className="text-critical">
                            行情错误：{row.error}
                          </td>
                        ) : (
                          <>
                            <td data-label="最新收盘" className="num">
                              {fmtPrice(row.close)}
                            </td>
                            <td data-label="20D 涨跌" className="num">
                              {fmtPct(row["ret_20d_%"], 2, true)}
                            </td>
                            <td data-label="日线结构 & 中枢数">
                              {row.daily_structure || "—"} ({row.daily_zs_count ?? 0} 中枢 · {row.pos_vs_zs || "—"})
                            </td>
                            <td data-label="最新中枢 [ZD, ZG]" className="num">
                              {row.daily_zs ? `[${fmtPrice(row.daily_zs[0])}, ${fmtPrice(row.daily_zs[1])}]` : "—"}
                            </td>
                            <td data-label="最新分型 & 动力学" className="text-ink-2">
                              {row.daily_fractal || "—"} · {row.daily_momentum || "—"}
                            </td>
                            <td data-label="信号层级 / 方向建议">
                              <div className="flex flex-wrap items-center gap-1.5">
                                {row.signal_tier ? (
                                  <StatusBadge
                                    tone={
                                      row.signal_tier.startsWith("A") || row.signal_tier.includes("买")
                                        ? "good"
                                        : row.signal_tier.includes("卖")
                                          ? "serious"
                                          : "info"
                                    }
                                    label={row.signal_tier}
                                  />
                                ) : null}
                                <span className="text-xs text-ink-2">{row.action_bias || ""}</span>
                                <Crosshair className="ml-auto h-3.5 w-3.5 text-muted" aria-hidden="true" />
                              </div>
                            </td>
                          </>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          {/* Non-scan task result summary (research_refresh / sentinel) */}
          {activeTask?.result && activeTask.kind !== "scan" ? (
            <div className="rounded-lg border border-hairline bg-page/60 p-3 text-xs">
              <div className="mb-1.5 flex items-center justify-between">
                <span className="font-semibold text-ink">任务返回摘要</span>
                {typeof (activeTask.result as { symbol?: unknown }).symbol === "string" ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => onJumpArchive(String((activeTask.result as { symbol: string }).symbol))}
                  >
                    打开该标的研究档案
                  </button>
                ) : null}
              </div>
              <pre className="m-0 max-h-56 overflow-auto font-mono text-[11px] text-ink-2">
                {JSON.stringify(activeTask.result, null, 2)}
              </pre>
            </div>
          ) : null}

          {/* Live Step Log */}
          <div>
            <div className="mb-1 text-xs text-muted">实时执行日志（{liveEvents.length} 条）</div>
            <div className="max-h-44 overflow-y-auto rounded-lg border border-hairline bg-page px-3 py-2 font-mono text-[11.5px] text-ink-2">
              {liveEvents.length === 0 ? (
                <div className="py-3 text-center font-sans text-xs text-muted">
                  在上方启动任意扫描、硬数据刷新或哨兵巡检后，此处实时流式展示执行步骤。
                </div>
              ) : (
                liveEvents.map((ev, i) => {
                  const d = ev.data ?? {};
                  const text =
                    d.message ||
                    ev.message ||
                    (ev.type === "row" && d.row
                      ? `✓ ${formatSymbolWithCode(d.row.symbol)} → ${d.row.signal_tier || d.row.error || "完成"}`
                      : ev.type === "progress"
                        ? `进度 ${d.done ?? 0}/${d.total ?? "?"} ${d.symbol ? `(${formatSymbolWithCode(d.symbol)})` : ""}`
                        : ev.type === "done"
                          ? "任务执行完毕"
                          : JSON.stringify(d));
                  return (
                    <div key={ev.seq ?? i} className="py-0.5">
                      <span className="text-muted">[{shortTime(ev.ts) || `#${ev.seq ?? i + 1}`}]</span>{" "}
                      <span className="text-slot1">{ev.type}</span> · <span>{text}</span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </section>
      </div>

      {/* Right Sidebar: Recent Task History */}
      <aside className="card flex flex-col overflow-hidden xl:col-span-3" aria-label="近期任务历史记录">
        <div className="flex items-center justify-between border-b border-hairline px-3.5 py-3">
          <span className="text-sm font-semibold text-ink">近期任务历史 ({tasks.length})</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void loadTasks()} aria-label="刷新任务列表">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
        <ul className="m-0 max-h-[72dvh] flex-1 list-none divide-y divide-hairline overflow-y-auto p-0">
          {tasks.length === 0 ? (
            <li className="p-5 text-center text-xs text-muted">暂无历史任务</li>
          ) : (
            tasks.map((t) => {
              const selected = t.id === activeTaskId;
              const Icon =
                t.status === "completed" ? CheckCircle2 : t.status === "failed" ? XCircle : Clock;
              const iconCls =
                t.status === "completed"
                  ? "text-good"
                  : t.status === "failed"
                    ? "text-critical"
                    : "text-slot1";
              return (
                <li key={t.id}>
                  <button
                    type="button"
                    onClick={() => setActiveTaskId(t.id)}
                    className={`flex w-full flex-col gap-1 px-3.5 py-2.5 text-left text-xs ${
                      selected ? "bg-elevated text-ink" : "text-ink-2 hover:bg-elevated/60"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-ink">
                        {formatTaskKind(t.kind)} · #{t.id.slice(0, 6)}
                      </span>
                      <span className="inline-flex items-center gap-1 text-[11px]">
                        <Icon className={`h-3.5 w-3.5 ${iconCls}`} aria-hidden="true" />
                        <span>{formatTaskStatus(t.status)}</span>
                      </span>
                    </div>
                    <div className="tnum text-[11px] text-muted">{shortTime(t.created_at)} UTC</div>
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </aside>

      <SaveUniverseDialog
        open={savePoolOpen}
        defaultSymbols={candidateSaveSymbols}
        onClose={() => setSavePoolOpen(false)}
        onSaved={() => void loadUniverses()}
      />
    </div>
  );
}

/* ------------------------------------------------------------------
 * Save New Universe Native <dialog>
 * ------------------------------------------------------------------ */
function SaveUniverseDialog({
  open,
  defaultSymbols,
  onClose,
  onSaved,
}: {
  open: boolean;
  defaultSymbols: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [symbolsStr, setSymbolsStr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName("");
    setDescription("");
    setSymbolsStr(defaultSymbols.join(", "));
    setErr(null);
  }, [open, defaultSymbols]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const syms = parseSymbolList(symbolsStr);
    if (!name.trim() || syms.length === 0) {
      setErr("请填写新池英文标识（如 a_grid_export）及至少 1 只核心龙头代码");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.saveUniverse({
        name: name.trim(),
        description: description.trim(),
        symbols: syms,
      });
      onSaved();
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
      title="沉淀新主题龙头池至 universes.json"
      subtitle="随行情轮动积累第一层 1Q~1Y 产业链逻辑与核心真龙头（严守单一板块龙头独占制）"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <label className="field">
          <span>标的池标识名 *（建议英文下划线，如 us_lng_chain / a_grid_export）</span>
          <input
            className="input"
            required
            autoFocus
            placeholder="如 a_grid_export"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>

        <label className="field">
          <span>1Q~1Y 产业链与双维驱动逻辑说明 *</span>
          <input
            className="input"
            required
            placeholder="如 欧美电网变压器更新周期 + 国内高压设备龙头海外订单高增"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>

        <label className="field">
          <span>核心龙头代码列表 *（逗号或空格分隔）</span>
          <textarea
            className="input"
            rows={2}
            required
            value={symbolsStr}
            onChange={(e) => setSymbolsStr(e.target.value)}
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
            {busy ? "正在保存…" : "保存至 universes.json"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
