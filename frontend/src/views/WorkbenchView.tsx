import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  BookmarkPlus,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Crosshair,
  Database,
  FolderPlus,
  Play,
  RefreshCw,
  Square,
  XCircle,
} from "lucide-react";
import { api, ApiError, errorMessage, parseTaskEvent } from "../api/client";
import type {
  CompassScanContext,
  PersistedScanRun,
  ScanRow,
  TaskEventEnvelope,
  TaskRecord,
  UniversePool,
  WarehouseSummary,
} from "../api/types";
import { ModalDialog } from "../components/ui/ModalDialog";
import { signedToneClass, StatusBadge } from "../components/ui/StatusBadge";
import { SymbolLabel } from "../components/ui/SymbolLabel";
import { Layer1CompassPanel } from "../components/workbench/Layer1CompassPanel";
import {
  fmtFractalText,
  fmtMomentumVerdict,
  fmtPct,
  fmtPosVsZs,
  fmtPrice,
  parseSymbolList,
  shortTime,
} from "../lib/format";
import { formatSymbolWithCode, getSymbolName } from "../lib/symbols";
import { formatTaskStatus, getUniverseLabel, groupUniverses } from "../lib/universes";

type RowFilter = "all" | "long" | "short" | "watch" | "aligned" | "counter" | "pool_unverified" | "degraded";
type CompassRowAlignment = "aligned" | "counter" | "watch" | "pool_unverified" | "degraded";

function extractCompassContext(task: TaskRecord | null): CompassScanContext | null {
  const raw = task?.params?.compass_context;
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const dir = String(obj.direction || "").toLowerCase();
  if (dir !== "long" && dir !== "short" && dir !== "pair") return null;
  const expRaw = obj.expected_directions;
  const expected: Record<string, "long" | "short"> = {};
  if (expRaw && typeof expRaw === "object") {
    for (const [k, v] of Object.entries(expRaw as Record<string, unknown>)) {
      if (v === "long" || v === "short") {
        expected[k.trim().toUpperCase()] = v;
      }
    }
  }
  return {
    run_id: typeof obj.run_id === "string" ? obj.run_id : "",
    direction_id: typeof obj.direction_id === "string" ? obj.direction_id : "",
    direction_title: typeof obj.direction_title === "string" ? obj.direction_title : "",
    direction: dir,
    value_chain_focus: typeof obj.value_chain_focus === "string" ? obj.value_chain_focus : "",
    expected_directions: expected,
  };
}

function resolveExpectedDirection(
  row: ScanRow,
  ctx: CompassScanContext,
): { expected: "long" | "short" | null; boundToL1: boolean } {
  const sym = (row.symbol || "").trim().toUpperCase();
  if (sym && ctx.expected_directions[sym]) {
    return { expected: ctx.expected_directions[sym], boundToL1: true };
  }
  /* Only treat symbols explicitly entered by the user (market === "adhoc") as bound to the staged L1 sector thesis;
     do NOT blindly broadcast a narrow L1 thesis (e.g. phosphate/sulfur) onto unrelated members of a broad pool (e.g. rubber RU0)! */
  const isUserCustom = (row.market || "").trim().toLowerCase() === "adhoc";
  if (isUserCustom && (ctx.direction === "long" || ctx.direction === "short")) {
    return { expected: ctx.direction, boundToL1: true };
  }
  return { expected: null, boundToL1: false };
}

function classifyRowWithCompass(
  row: ScanRow,
  ctx: CompassScanContext,
): { status: CompassRowAlignment; expected: "long" | "short" | null; actual: "long" | "short" | "watch"; boundToL1: boolean } {
  const { expected, boundToL1 } = resolveExpectedDirection(row, ctx);
  const sigType = (row.signal_type || "").trim().toLowerCase();
  if (row.error || row.quality_grade === "degraded" || sigType === "data_quality_degraded") {
    return { status: "degraded", expected, actual: "watch", boundToL1 };
  }
  const actual = classifyScanRow(row);
  if (!boundToL1 || !expected) {
    return { status: "pool_unverified", expected: null, actual, boundToL1: false };
  }
  if (actual === expected) {
    return { status: "aligned", expected, actual, boundToL1: true };
  }
  if ((actual === "long" && expected === "short") || (actual === "short" && expected === "long")) {
    return { status: "counter", expected, actual, boundToL1: true };
  }
  return { status: "watch", expected, actual, boundToL1: true };
}

function isTerminalTaskStatus(status: string | null | undefined): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function mergeMonotonicTask(prev: TaskRecord | null, incoming: TaskRecord): TaskRecord {
  if (!prev || prev.id !== incoming.id) return incoming;
  if (isTerminalTaskStatus(prev.status) && !isTerminalTaskStatus(incoming.status)) {
    return {
      ...incoming,
      status: prev.status,
      result: prev.result ?? incoming.result,
      error: prev.error ?? incoming.error,
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
    map.set(`${r.market}::${r.symbol}`, r);
  }
  for (const r of nextRows) {
    map.set(`${r.market}::${r.symbol}`, r);
  }
  return Array.from(map.values());
}

function classifyScanRow(row: ScanRow): "long" | "short" | "watch" {
  const sigType = (row.signal_type || "").trim().toLowerCase();
  const tierTxt = (row.signal_tier || "").trim();

  /* Exclude degraded quality feeds, unconfirmed candidates, weak-momentum warnings, exhaustion alerts, and watch tiers */
  if (
    row.quality_grade === "degraded" ||
    sigType === "data_quality_degraded" ||
    row.signal_confirmed === false ||
    sigType.includes("candidate") ||
    sigType.includes("weak_momentum") ||
    sigType.includes("divergence_watch") ||
    sigType.includes("exhaustion") ||
    /(?:观察|待确认|勿追|警告|数据质量降级)/.test(tierTxt)
  ) {
    return "watch";
  }

  /* 1. Authoritative confirmed backend signal_meta.direction */
  const explicitDir = (row.signal_direction || "").trim().toLowerCase();
  if (explicitDir === "long" || explicitDir === "bullish" || explicitDir === "up") return "long";
  if (explicitDir === "short" || explicitDir === "bearish" || explicitDir === "down") return "short";
  if (explicitDir === "neutral") return "watch";

  /* 2. Conservative fallback for persisted legacy rows without signal_meta */
  const cleaned = `${tierTxt} ${row.action_bias || ""}`
    .replace(/买卖点信号/g, "")
    .replace(/(?:严禁|禁止|切勿|勿|不可|非|待确认|观察)[^，,。；;]*/g, "")
    .toLowerCase();

  const isShort = /(?:一卖|二卖|三卖|1卖|2卖|3卖|空头资金一致)/.test(cleaned);
  const isLong = /(?:一买|二买|三买|1买|2买|3买|多头资金一致)/.test(cleaned);
  if (isShort && !isLong) return "short";
  if (isLong && !isShort) return "long";
  return "watch";
}

function describeScanTaskScope(t: TaskRecord): string {
  const p = t.params;
  if (!p || typeof p !== "object") return "板块龙头扫描";
  const univs = Array.isArray(p.universes) ? (p.universes as unknown[]).map(String).filter(Boolean) : [];
  const syms = Array.isArray(p.symbols) ? (p.symbols as unknown[]).map(String).filter(Boolean) : [];
  const parts: string[] = [];
  if (univs.length > 0) {
    parts.push(univs.slice(0, 2).map((u) => getUniverseLabel(u)).join("、") + (univs.length > 2 ? ` 等${univs.length}池` : ""));
  }
  if (syms.length > 0) {
    parts.push(syms.slice(0, 3).join(", ") + (syms.length > 3 ? ` +${syms.length - 3}` : ""));
  }
  return parts.length > 0 ? parts.join(" + ") : "板块龙头扫描";
}

interface WorkbenchViewProps {
  defaultSymbol: string;
  workspaceRev?: number;
  onJumpWarRoom: (symbol: string) => void;
  onJumpArchive: (symbol: string) => void;
  onArchiveMutated?: () => void;
}

export function WorkbenchView({
  workspaceRev = 0,
  onJumpWarRoom,
  onJumpArchive,
  onArchiveMutated,
}: WorkbenchViewProps) {
  const [universes, setUniverses] = useState<Record<string, UniversePool>>({});
  const [selectedPools, setSelectedPools] = useState<string[]>([]);
  const [activeGroupKey, setActiveGroupKey] = useState<string>("all");
  const [pickerExpanded, setPickerExpanded] = useState(false);
  const [customSymbolsText, setCustomSymbolsText] = useState("");
  const [sortMode, setSortMode] = useState<"tier" | "none">("tier");
  const [forceRefresh, setForceRefresh] = useState(false);

  /* Tasks, SQLite history & SSE stream */
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [warehouse, setWarehouse] = useState<WarehouseSummary | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<TaskRecord | null>(null);
  const [liveEvents, setLiveEvents] = useState<TaskEventEnvelope[]>([]);
  const [scanRows, setScanRows] = useState<ScanRow[]>([]);
  const [rowFilter, setRowFilter] = useState<RowFilter>("all");
  const [progress, setProgress] = useState<{ done: number; total: number | null }>({ done: 0, total: null });
  const [launching, setLaunching] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  /* Quick add-to-watchlist (archiveInit) tracking */
  const [archivedSyms, setArchivedSyms] = useState<Set<string>>(new Set());
  const [addingWatchSym, setAddingWatchSym] = useState<string | null>(null);

  /* Save new universe modal */
  const [savePoolOpen, setSavePoolOpen] = useState(false);
  const [savePoolPrefill, setSavePoolPrefill] = useState<{
    name: string;
    description: string;
    symbols: string[];
  } | null>(null);

  const esRef = useRef<EventSource | null>(null);
  const layer2Ref = useRef<HTMLDivElement | null>(null);
  const launchingRef = useRef(false);
  const scanReqSeqRef = useRef(0);

  const loadUniverses = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await api.universes(signal);
      setUniverses(res.universes ?? {});
    } catch {
      /* ignore */
    }
  }, []);

  const loadArchivesSet = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await api.archives(signal);
      setArchivedSyms(new Set((res.archives ?? []).map((a) => a.symbol.toUpperCase())));
    } catch {
      /* ignore */
    }
  }, []);

  const loadWarehouse = useCallback(async (signal?: AbortSignal) => {
    try {
      const w = await api.warehouse(signal);
      setWarehouse(w);
    } catch {
      /* ignore */
    }
  }, []);

  const loadTasks = useCallback(async (signal?: AbortSignal) => {
    try {
      const [res, histRes] = await Promise.all([
        api.tasks(signal),
        api.scanHistory(30, signal).catch(() => ({ runs: [] as PersistedScanRun[] })),
      ]);
      const liveScans = (res.tasks ?? []).filter((t) => t.kind === "scan");
      const seenIds = new Set(liveScans.map((t) => t.id));
      const archivedAsTasks: TaskRecord[] = (histRes.runs ?? [])
        .filter((r) => !seenIds.has(r.run_id))
        .map((r) => ({
          id: r.run_id,
          task_id: r.run_id,
          kind: "scan",
          status: r.status || "completed",
          params: {
            universes: r.universes,
            symbols: r.symbols,
            period: r.period,
            ...(r.compass_context ? { compass_context: r.compass_context } : {}),
          },
          created_at: r.created_at_utc,
          finished_at: r.finished_at_utc,
          progress: { done: r.total_count, total: r.total_count },
        }));
      const list = [...liveScans, ...archivedAsTasks];
      setTasks((prev) => {
        const prevById = new Map(prev.map((t) => [t.id, t]));
        return list.map((item) => mergeMonotonicTask(prevById.get(item.id) ?? null, item));
      });
      void loadWarehouse(signal);
      return list;
    } catch {
      return [];
    }
  }, [loadWarehouse]);

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

  /* Keep archived badge set synchronized whenever archives change in other screens */
  useEffect(() => {
    const ctrl = new AbortController();
    void loadArchivesSet(ctrl.signal);
    return () => ctrl.abort();
  }, [loadArchivesSet, workspaceRev]);

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
        } else if (t === "error" || t === "cancelled") {
          sawTerminal = true;
          const nextStatus = t === "cancelled" ? "cancelled" : "failed";
          const errMsg = d.message || ev.message || (t === "cancelled" ? "已终止扫描任务" : "任务执行失败");
          setActiveTask((prev) =>
            prev
              ? { ...prev, status: nextStatus, error: errMsg }
              : { id: activeTaskId, task_id: activeTaskId, kind: "scan", status: nextStatus, error: errMsg },
          );
          setTasks((prev) =>
            prev.map((item) => (item.id === activeTaskId ? { ...item, status: nextStatus, error: errMsg } : item)),
          );
        }
      }

      if (addedRows.length > 0) {
        setScanRows((prev) => mergeScanRows(prev, addedRows));
        setPickerExpanded(false);
      }

      if (sawTerminal) {
        void loadTasks();
        es.close();
      }
    };

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
        if (resObj && Array.isArray(resObj.rows) && resObj.rows.length > 0) {
          setScanRows((prev) => mergeScanRows(prev, resObj.rows ?? []));
          setPickerExpanded(false);
        }
        if (isTerminalTaskStatus(detail.status)) {
          es.close();
        }
      })
      .catch(async () => {
        /* Fallback to long-term SQLite scan_runs archive when task_id was evicted from 10-slot active ring */
        es.close();
        try {
          const archived = await api.scanHistoryDetail(activeTaskId, detailCtrl.signal);
          if (cancelled || detailCtrl.signal.aborted) return;
          setActiveTask({
            id: archived.run_id,
            task_id: archived.run_id,
            kind: "scan",
            status: archived.status || "completed",
            params: {
              universes: archived.universes,
              symbols: archived.symbols,
              period: archived.period,
              ...(archived.compass_context ? { compass_context: archived.compass_context } : {}),
            },
            created_at: archived.created_at_utc,
            finished_at: archived.finished_at_utc,
            progress: { done: archived.total_count, total: archived.total_count },
            result: { rows: archived.rows ?? [], count: archived.total_count, errors: archived.error_count },
          });
          setProgress({ done: archived.total_count, total: archived.total_count });
          if (archived.rows && archived.rows.length > 0) {
            setScanRows(archived.rows);
            setPickerExpanded(false);
          }
        } catch {
          /* ignore */
        }
      });

    const handleEnvelope = (evType: string) => (msg: MessageEvent) => {
      if (cancelled || typeof msg.data !== "string") return;
      const parsed = parseTaskEvent(msg.data, evType);
      if (!parsed) return;
      const added = commitEvents([parsed]);
      if (added.length === 0) return;
      applyBusinessEvents(added);
    };

    for (const evtName of ["step", "progress", "row", "done", "error", "cancelled", "message"]) {
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

  const launchScanRequest = useCallback(
    async (opts: {
      universes: string[];
      symbols: string[];
      compassContext?: CompassScanContext | null;
      syncInputs?: boolean;
      scrollToResults?: boolean;
    }) => {
      if (launchingRef.current) return;
      if (opts.universes.length === 0 && opts.symbols.length === 0) {
        setErr("请至少勾选 1 个板块龙头池，或输入自定义龙头标的代码");
        return;
      }
      launchingRef.current = true;
      const reqSeq = ++scanReqSeqRef.current;
      setLaunching(true);
      setErr(null);
      try {
        const created = await api.startScan({
          universes: opts.universes,
          symbols: opts.symbols,
          sort: sortMode,
          period: "3y",
          refresh: forceRefresh,
          compass_context: opts.compassContext ?? null,
        });
        if (scanReqSeqRef.current !== reqSeq) return;
        if (opts.syncInputs) {
          setSelectedPools(opts.universes);
          setCustomSymbolsText(opts.symbols.join(", "));
        }
        setRowFilter("all");
        setActiveTask(created);
        setActiveTaskId(created.id);
        setPickerExpanded(false);
        void loadTasks();
        if (opts.scrollToResults) {
          window.setTimeout(() => {
            layer2Ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
          }, 60);
        }
      } catch (ex) {
        if (scanReqSeqRef.current === reqSeq) {
          setErr(errorMessage(ex));
        }
      } finally {
        if (scanReqSeqRef.current === reqSeq) {
          launchingRef.current = false;
          setLaunching(false);
        }
      }
    },
    [sortMode, forceRefresh, loadTasks],
  );

  const handleLaunch = async (e: FormEvent) => {
    e.preventDefault();
    const customSyms = parseSymbolList(customSymbolsText);
    /* Manual scan stays strictly manual (no L1 thesis coupling) */
    await launchScanRequest({
      universes: selectedPools,
      symbols: customSyms,
      compassContext: null,
    });
  };

  const handleLaunchCompassScan = useCallback(
    async (opts: { symbols: string[]; compassContext: CompassScanContext }) => {
      await launchScanRequest({
        universes: [],
        symbols: opts.symbols,
        compassContext: opts.compassContext,
        syncInputs: false,
        scrollToResults: true,
      });
    },
    [launchScanRequest],
  );

  const handleCancelActiveTask = async () => {
    if (!activeTaskId || cancelling) return;
    setCancelling(true);
    try {
      const updated = await api.cancelTask(activeTaskId);
      setActiveTask((prev) => mergeMonotonicTask(prev, updated));
      void loadTasks();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setCancelling(false);
    }
  };

  const handleQuickAddWatchlist = async (row: ScanRow) => {
    const sym = row.symbol.trim().toUpperCase();
    if (archivedSyms.has(sym)) {
      onJumpArchive(sym);
      return;
    }
    setAddingWatchSym(sym);
    try {
      await api.archiveInit({
        symbol: sym,
        name: getSymbolName(sym) || undefined,
        primary_driver: row.market ? getUniverseLabel(row.market) : undefined,
        thesis_summary: `自扫描台收入观察池（日线结构：${row.daily_structure || "—"}，信号：${row.signal_tier || row.action_bias || "待观察"}）`,
      });
      setArchivedSyms((prev) => new Set(prev).add(sym));
      onArchiveMutated?.();
    } catch {
      /* If already exists on disk under another alias, jump directly */
      onJumpArchive(sym);
    } finally {
      setAddingWatchSym(null);
    }
  };

  const progressPct = useMemo(() => {
    if (progress.total && progress.total > 0) {
      return Math.min(100, Math.round((progress.done / progress.total) * 100));
    }
    if (activeTask?.status === "completed") return 100;
    return 0;
  }, [progress, activeTask]);

  const candidateSaveSymbols = useMemo(() => {
    const custom = parseSymbolList(customSymbolsText);
    if (custom.length > 0) return custom;
    return scanRows.map((r) => r.symbol);
  }, [customSymbolsText, scanRows]);

  const groupedPools = useMemo(() => groupUniverses(universes), [universes]);
  const visiblePoolSections = useMemo(
    () =>
      activeGroupKey === "all"
        ? groupedPools
        : groupedPools.filter((g) => g.groupKey === activeGroupKey),
    [groupedPools, activeGroupKey],
  );

  const activeCompassContext = useMemo(() => extractCompassContext(activeTask), [activeTask]);

  /* Reset incompatible filter when switching between Compass-guided and manual scans */
  useEffect(() => {
    if (activeCompassContext && (rowFilter === "long" || rowFilter === "short")) {
      setRowFilter("all");
    } else if (
      !activeCompassContext &&
      (rowFilter === "aligned" || rowFilter === "counter" || rowFilter === "pool_unverified" || rowFilter === "degraded")
    ) {
      setRowFilter("all");
    }
  }, [activeCompassContext, rowFilter]);

  const filterCounts = useMemo(() => {
    let longCount = 0;
    let shortCount = 0;
    let watchCount = 0;
    let alignedCount = 0;
    let counterCount = 0;
    let poolUnverifiedCount = 0;
    let degradedCount = 0;
    for (const r of scanRows) {
      if (activeCompassContext) {
        const res = classifyRowWithCompass(r, activeCompassContext);
        if (res.status === "aligned") alignedCount += 1;
        else if (res.status === "counter") counterCount += 1;
        else if (res.status === "pool_unverified") poolUnverifiedCount += 1;
        else if (res.status === "degraded") degradedCount += 1;
        else watchCount += 1;
      } else {
        const c = classifyScanRow(r);
        if (c === "long") longCount += 1;
        else if (c === "short") shortCount += 1;
        else watchCount += 1;
      }
    }
    return {
      all: scanRows.length,
      long: longCount,
      short: shortCount,
      watch: watchCount,
      aligned: alignedCount,
      counter: counterCount,
      pool_unverified: poolUnverifiedCount,
      degraded: degradedCount,
    };
  }, [scanRows, activeCompassContext]);

  const pairVerdict = useMemo(() => {
    if (!activeCompassContext || activeCompassContext.direction !== "pair" || scanRows.length === 0) {
      return null;
    }
    let longAligned = 0;
    let shortAligned = 0;
    let longTotal = 0;
    let shortTotal = 0;
    let counterAny = 0;
    let degradedAny = 0;
    for (const r of scanRows) {
      const res = classifyRowWithCompass(r, activeCompassContext);
      if (res.expected === "long") {
        longTotal += 1;
        if (res.status === "aligned") longAligned += 1;
      } else if (res.expected === "short") {
        shortTotal += 1;
        if (res.status === "aligned") shortAligned += 1;
      }
      if (res.status === "counter") counterAny += 1;
      if (res.status === "degraded") degradedAny += 1;
    }
    if (longTotal === 0 || shortTotal === 0) return null;
    if (longAligned > 0 && shortAligned > 0) {
      return {
        tone: "good" as const,
        label: "配对双腿同向通过",
        detail: `多头腿 ${longAligned}/${longTotal}、空头腿 ${shortAligned}/${shortTotal} 均获日线同向确认，可进入 Layer 3/4 联立。`,
      };
    }
    if (counterAny > 0) {
      return {
        tone: "critical" as const,
        label: "配对方向冲突 · 严禁强行配对",
        detail: `检测到 ${counterAny} 只腿出现与 Layer 1 反向的日线信号，先留待观察。`,
      };
    }
    return {
      tone: "warning" as const,
      label: degradedAny > 0 ? "配对数据降级/未齐 · 待观察" : "配对单腿未成熟 · 待观察",
      detail: `多头腿同向 ${longAligned}/${longTotal} · 空头腿同向 ${shortAligned}/${shortTotal}，等待双腿日线结构共振。`,
    };
  }, [activeCompassContext, scanRows]);

  const filteredScanRows = useMemo(() => {
    if (rowFilter === "all") return scanRows;
    if (activeCompassContext) {
      return scanRows.filter((r) => classifyRowWithCompass(r, activeCompassContext).status === rowFilter);
    }
    return scanRows.filter((r) => classifyScanRow(r) === rowFilter);
  }, [scanRows, rowFilter, activeCompassContext]);

  const selectedSummaryText = useMemo(() => {
    const poolLabels = selectedPools.map((k) => getUniverseLabel(k));
    const custom = parseSymbolList(customSymbolsText);
    const parts: string[] = [];
    if (poolLabels.length > 0) parts.push(poolLabels.join("、"));
    if (custom.length > 0) parts.push(`自定义 ${custom.join(", ")}`);
    return parts.length > 0 ? parts.join(" + ") : "未选板块池";
  }, [selectedPools, customSymbolsText]);

  return (
    <div className="flex flex-col gap-4">
      {/* Compact Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="heading-page m-0 text-lg text-ink md:text-xl">自上而下机会雷达 · 战略定调与日线过筛</h1>
          <p className="m-0 mt-0.5 text-xs text-muted">
            Layer 1 宏观双核与 7 维推演锁定最优多空方向 → 单一板块真龙头 → Layer 2 日线缠论过筛 → 下钻研报档案或 30min 战情室
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {warehouse ? (
            <span
              className="inline-flex items-center gap-1.5 rounded-md border border-hairline bg-surface px-2.5 py-1 font-mono text-[11px] text-muted"
              title={`本地 SQLite 仓库：${warehouse.db_path}（已落盘 ${warehouse.series_count} 条行情序列 / ${warehouse.total_ohlcv_bars} 根 K 线，历史扫描留存 ${warehouse.scan_runs_count} 批）`}
            >
              <Database className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
              <span>
                本地仓 {warehouse.series_count} 序列 · {warehouse.scan_runs_count} 批留存
              </span>
              {(warehouse.quality_counts?.degraded ?? 0) > 0 ? (
                <span className="text-warning">
                  · {warehouse.quality_counts.degraded} 项降级
                </span>
              ) : null}
            </span>
          ) : null}
          <button
            type="button"
            className="btn"
            onClick={() => {
              setSavePoolPrefill(null);
              setSavePoolOpen(true);
            }}
            title="将自上而下推演出的新板块核心龙头沉淀至 universes.json"
          >
            <BookmarkPlus className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
            <span>沉淀新主题池</span>
          </button>
        </div>
      </div>

      {/* Layer 1: Top-Down Macro Dual-Core + 7-Dimension Strategic Compass */}
      <Layer1CompassPanel
        universes={universes}
        scanBusy={launching}
        onLaunchCompassScan={handleLaunchCompassScan}
        onUniverseSaved={() => void loadUniverses()}
        onJumpWarRoom={onJumpWarRoom}
        onJumpArchive={onJumpArchive}
      />

      {/* Layer 2 Section Divider & Daily Chanlun Screening */}
      <div ref={layer2Ref} className="flex flex-col gap-3 scroll-mt-16">
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center rounded border border-hairline bg-elevated px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-2">
              Layer 2
            </span>
            <h2 className="m-0 text-sm font-semibold text-ink">日线缠论过筛结果 & 手动扫描台</h2>
            <span className="text-xs text-muted">
              上方 L1 专属小池过筛与下方手动大类池扫描完全独立，互不干扰
            </span>
          </div>
        </div>

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-12">
        {/* Main Column: Collapsible Pool Launcher + Wide Scan Results Table */}
        <div className="flex flex-col gap-3.5 xl:col-span-9">
          <section className="card flex flex-col gap-3 p-4" aria-label="手动板块龙头池选择器">
            {/* Collapsible Summary Bar (Collapsed by default so Layer 1 leads) */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => setPickerExpanded((v) => !v)}
                aria-expanded={pickerExpanded}
                className="inline-flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 text-left text-[13px] font-semibold text-ink"
              >
                {pickerExpanded ? (
                  <ChevronDown className="h-4 w-4 text-muted" aria-hidden="true" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted" aria-hidden="true" />
                )}
                <span>手动选池 / 自定义龙头：{selectedSummaryText}</span>
              </button>

              <div className="flex items-center gap-2">
                {!pickerExpanded ? (
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setPickerExpanded(true)}
                  >
                    展开 {Object.keys(universes).length || 18} 个本地池
                  </button>
                ) : selectedPools.length > 0 ? (
                  <button
                    type="button"
                    className="text-xs text-accent hover:underline cursor-pointer bg-transparent border-0 p-0"
                    onClick={() => setSelectedPools([])}
                  >
                    清空已选 ({selectedPools.length})
                  </button>
                ) : null}

                {!pickerExpanded && (selectedPools.length > 0 || parseSymbolList(customSymbolsText).length > 0) ? (
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    disabled={launching}
                    onClick={(ev) => void handleLaunch(ev as unknown as FormEvent)}
                  >
                    <Play className="h-3 w-3" aria-hidden="true" />
                    <span>{launching ? "启动中…" : "重新扫描"}</span>
                  </button>
                ) : null}
              </div>
            </div>

            {pickerExpanded ? (
              <form onSubmit={handleLaunch} className="flex flex-col gap-3 border-t border-hairline-soft pt-3">
                {/* Category Filter Tabs to prevent giant pill wall */}
                <div className="flex flex-wrap items-center gap-1">
                  <button
                    type="button"
                    className="chip"
                    aria-pressed={activeGroupKey === "all"}
                    onClick={() => setActiveGroupKey("all")}
                  >
                    全部类别
                  </button>
                  {groupedPools.map((g) => (
                    <button
                      key={g.groupKey}
                      type="button"
                      className="chip"
                      aria-pressed={activeGroupKey === g.groupKey}
                      onClick={() => setActiveGroupKey(g.groupKey)}
                    >
                      {g.title}
                    </button>
                  ))}
                </div>

                <div className="flex flex-col gap-2">
                  {visiblePoolSections.map((section) => (
                    <div key={section.groupKey} className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-2.5">
                      {activeGroupKey === "all" ? (
                        <span className="w-24 shrink-0 pt-1 text-[11px] font-medium text-muted">
                          {section.title}
                        </span>
                      ) : null}
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
                            <span className="font-mono text-[10px] text-muted">{item.symbols.length}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>

                <div className="grid grid-cols-1 items-end gap-2.5 border-t border-hairline-soft pt-2.5 md:grid-cols-12">
                  <label className="field md:col-span-5">
                    <span>临时切入新细分板块真龙头（空格或逗号分隔）</span>
                    <input
                      className="input"
                      placeholder="如 NVDA, PWR, CU0, AUDUSD=X"
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
                  <label
                    className="flex cursor-pointer items-center gap-1.5 pb-2 text-xs text-ink-2 md:col-span-2"
                    title="默认 15 分钟内优先复用本地 SQLite 增量行情库，勾选后强制逐标的重连上游"
                  >
                    <input
                      type="checkbox"
                      checked={forceRefresh}
                      onChange={(e) => setForceRefresh(e.target.checked)}
                    />
                    <span>强制重拉上游</span>
                  </label>
                  <div className="md:col-span-2">
                    <button type="submit" className="btn btn-primary w-full" disabled={launching}>
                      <Play className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{launching ? "启动中…" : "启动扫描"}</span>
                    </button>
                  </div>
                </div>
              </form>
            ) : null}

            {err ? (
              <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
                {err}
              </div>
            ) : null}
          </section>

          {/* Results & Live Progress Card */}
          <section className="card flex flex-col gap-3 p-4" aria-label="扫描结果与实时进度">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-semibold text-ink">
                  日线缠论过筛结果
                </span>
                {activeTask ? (
                  <StatusBadge
                    tone={
                      activeTask.status === "completed"
                        ? "good"
                        : activeTask.status === "failed"
                          ? "critical"
                          : activeTask.status === "cancelled"
                            ? "warning"
                            : "info"
                    }
                    label={formatTaskStatus(activeTask.status)}
                  />
                ) : null}
                {progress.total != null ? (
                  <span className="tnum font-mono text-[11px] text-muted">
                    {progress.done}/{progress.total} ({progressPct}%)
                  </span>
                ) : null}
                {activeTask && !isTerminalTaskStatus(activeTask.status) ? (
                  <button
                    type="button"
                    className="btn btn-sm text-critical"
                    disabled={cancelling}
                    onClick={() => void handleCancelActiveTask()}
                    title="立即终止当前扫描流，已完成的行情仍保留在本地库"
                  >
                    <Square className="h-3 w-3" aria-hidden="true" />
                    <span>{cancelling ? "终止中…" : "终止"}</span>
                  </button>
                ) : null}
              </div>

              {/* Directional Filter Chips */}
              {scanRows.length > 0 ? (
                <div className="flex flex-wrap items-center gap-1" role="group" aria-label="多空与待观察信号筛选">
                  <button
                    type="button"
                    className="chip"
                    aria-pressed={rowFilter === "all"}
                    onClick={() => setRowFilter("all")}
                  >
                    全部 ({filterCounts.all})
                  </button>
                  {activeCompassContext ? (
                    <>
                      <button
                        type="button"
                        className="chip chip-bull"
                        aria-pressed={rowFilter === "aligned"}
                        onClick={() => setRowFilter("aligned")}
                      >
                        同向通过 ({filterCounts.aligned})
                      </button>
                      <button
                        type="button"
                        className="chip chip-bear"
                        aria-pressed={rowFilter === "counter"}
                        onClick={() => setRowFilter("counter")}
                      >
                        反向冲突 ({filterCounts.counter})
                      </button>
                      <button
                        type="button"
                        className="chip"
                        aria-pressed={rowFilter === "watch"}
                        onClick={() => setRowFilter("watch")}
                      >
                        待成熟观察 ({filterCounts.watch})
                      </button>
                      {filterCounts.pool_unverified > 0 ? (
                        <button
                          type="button"
                          className="chip"
                          aria-pressed={rowFilter === "pool_unverified"}
                          onClick={() => setRowFilter("pool_unverified")}
                          title="来自综合大池且未显式归属本 L1 细分逻辑的标的，需先人工确认基本面相关性"
                        >
                          池内未定归属 ({filterCounts.pool_unverified})
                        </button>
                      ) : null}
                      {filterCounts.degraded > 0 ? (
                        <button
                          type="button"
                          className="chip"
                          aria-pressed={rowFilter === "degraded"}
                          onClick={() => setRowFilter("degraded")}
                        >
                          数据不足 ({filterCounts.degraded})
                        </button>
                      ) : null}
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="chip chip-bull"
                        aria-pressed={rowFilter === "long"}
                        onClick={() => setRowFilter("long")}
                      >
                        多头候选 ({filterCounts.long})
                      </button>
                      <button
                        type="button"
                        className="chip chip-bear"
                        aria-pressed={rowFilter === "short"}
                        onClick={() => setRowFilter("short")}
                      >
                        空头候选 ({filterCounts.short})
                      </button>
                      <button
                        type="button"
                        className="chip"
                        aria-pressed={rowFilter === "watch"}
                        onClick={() => setRowFilter("watch")}
                      >
                        逻辑观察 ({filterCounts.watch})
                      </button>
                    </>
                  )}
                </div>
              ) : null}
            </div>

            {/* Layer 1 Directional Alignment Context Banner */}
            {activeCompassContext ? (
              <div className="flex flex-col gap-1 rounded-md border border-accent/30 bg-accent/8 px-3 py-2 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded border border-accent/35 bg-surface px-1.5 py-0.5 font-mono text-[10px] font-semibold text-accent">
                      Layer 1 联立校验
                    </span>
                    <span className="font-semibold text-ink">
                      {activeCompassContext.direction_title || "战略方向过筛"}
                    </span>
                    <span className="text-muted">
                      预定方向：
                      {activeCompassContext.direction === "short"
                        ? "做空 SHORT"
                        : activeCompassContext.direction === "pair"
                          ? "多空配对 PAIR"
                          : "做多 LONG"}
                    </span>
                  </div>
                  {pairVerdict ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusBadge tone={pairVerdict.tone} label={pairVerdict.label} />
                      <span className="text-[11px] text-ink-2">{pairVerdict.detail}</span>
                    </div>
                  ) : null}
                </div>
                {filterCounts.pool_unverified > 0 ? (
                  <div className="text-[11px] text-warning">
                    已隔离 {filterCounts.pool_unverified} 只综合大池标的：L1 细分逻辑不会自动套用到整池无关品种，仅对你手动填入的真龙头或显式基准锚自动判定「L1同向通过」。
                  </div>
                ) : null}
              </div>
            ) : null}

            {activeTask && !isTerminalTaskStatus(activeTask.status) ? (
              <div className="progress-bar h-1 w-full overflow-hidden rounded-full bg-sunken">
                <span
                  className="block h-full rounded-full bg-accent"
                  style={{ inlineSize: `${progressPct}%` }}
                />
              </div>
            ) : null}

            {scanRows.length > 0 ? (
              <div tabIndex={0} role="region" aria-label="板块龙头日线缠论扫描结果表" className="table-wrapper max-h-[66dvh]">
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
                      <th scope="col">信号与方向建议</th>
                      <th scope="col" className="num">
                        流转动作
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredScanRows.map((row) => {
                      const symUpper = row.symbol.toUpperCase();
                      const isArchived = archivedSyms.has(symUpper);
                      const marketLabel = row.market
                        ? getUniverseLabel(row.market, universes[row.market]?.description)
                        : null;
                      const rowClass = classifyScanRow(row);
                      const compassAlign = activeCompassContext
                        ? classifyRowWithCompass(row, activeCompassContext)
                        : null;

                      return (
                        <tr
                          key={`${row.market}-${row.symbol}`}
                          className="clickable"
                          onClick={() => onJumpWarRoom(row.symbol)}
                        >
                          <th scope="row">
                            <div className="flex flex-col gap-0.5">
                              <SymbolLabel symbol={row.symbol} size="sm" />
                              {marketLabel ? (
                                <span className="text-[10.5px] font-normal text-muted">{marketLabel}</span>
                              ) : null}
                            </div>
                          </th>
                          {row.error ? (
                            <td colSpan={6} data-label="异常" className="text-critical">
                              行情错误：{row.error}
                            </td>
                          ) : (
                            <>
                              <td data-label="最新收盘" className="num">
                                <div>{fmtPrice(row.close)}</div>
                                {row.as_of_bar ? (
                                  <div className="font-mono text-[10.5px] font-normal text-muted">
                                    {row.as_of_bar}
                                    {row.bar_is_closed === false ? " · 盘中" : ""}
                                    {row.cache_status === "hit" ? " · 本地" : ""}
                                  </div>
                                ) : null}
                              </td>
                              <td
                                data-label="20D 涨跌"
                                className={`num font-semibold ${signedToneClass(row["ret_20d_%"])}`}
                              >
                                {fmtPct(row["ret_20d_%"], 2, true)}
                              </td>
                              <td data-label="日线结构 & 中枢数">
                                <div>{row.daily_structure || "—"}</div>
                                <div className="text-[11px] text-muted">
                                  {row.daily_zs_count ?? 0}{" "}
                                  {row.daily_zs_level === "segment" ? "线段中枢" : "笔中枢"} ·{" "}
                                  {fmtPosVsZs(row.pos_vs_zs)} · {fmtFractalText(row.daily_fractal)}
                                  {row.daily_momentum ? ` · ${fmtMomentumVerdict(row.daily_momentum)}` : ""}
                                </div>
                              </td>
                              <td data-label="最新中枢 [ZD, ZG]" className="num">
                                {row.daily_zs ? `[${fmtPrice(row.daily_zs[0])}, ${fmtPrice(row.daily_zs[1])}]` : "—"}
                              </td>
                              <td data-label="信号与方向建议">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  {compassAlign ? (
                                    <StatusBadge
                                      tone={
                                        compassAlign.status === "aligned"
                                          ? "good"
                                          : compassAlign.status === "counter"
                                            ? "critical"
                                            : compassAlign.status === "degraded"
                                              ? "warning"
                                              : "neutral"
                                      }
                                      label={
                                        compassAlign.status === "aligned"
                                          ? `L1同向通过 (${compassAlign.expected === "short" ? "空" : "多"})`
                                          : compassAlign.status === "counter"
                                            ? `L1反向冲突 (预期${compassAlign.expected === "short" ? "空" : "多"})`
                                            : compassAlign.status === "pool_unverified"
                                              ? "池内参考·需核实归属"
                                              : compassAlign.status === "degraded"
                                                ? "数据不足"
                                                : `L1待成熟 (预期${compassAlign.expected === "short" ? "空" : compassAlign.expected === "long" ? "多" : "—"})`
                                      }
                                    />
                                  ) : null}
                                  {row.signal_tier ? (
                                    <StatusBadge
                                      tone={
                                        row.quality_grade === "degraded" || row.signal_tier === "数据质量降级"
                                          ? "warning"
                                          : rowClass === "long"
                                            ? "good"
                                            : rowClass === "short"
                                              ? "critical"
                                              : "neutral"
                                      }
                                      label={row.signal_tier}
                                    />
                                  ) : null}
                                  {row.quality_grade === "caution" ? (
                                    <span
                                      className="inline-flex items-center gap-0.5 rounded bg-warning/15 px-1.5 py-0.5 text-[10.5px] text-warning"
                                      title={(row.quality_notes ?? []).join("；") || "数据源存在轻微平K或包络修正"}
                                    >
                                      <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                                      <span>质量提示</span>
                                    </span>
                                  ) : null}
                                  <span
                                    className="text-xs text-ink-2"
                                    title={(row.quality_notes ?? []).join("；") || undefined}
                                  >
                                    {row.action_bias || ""}
                                  </span>
                                </div>
                              </td>
                              <td
                                data-label="流转动作"
                                className="num"
                                onClick={(ev) => ev.stopPropagation()}
                              >
                                <div className="inline-flex items-center justify-end gap-1">
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    title="进入缠论战情室下钻 30min 触发与算仓"
                                    onClick={() => onJumpWarRoom(row.symbol)}
                                  >
                                    <Crosshair className="h-3 w-3 text-accent" aria-hidden="true" />
                                    <span>战情</span>
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    disabled={addingWatchSym === symUpper}
                                    title={
                                      isArchived
                                        ? "打开已建研究档案"
                                        : "一键建立研究档案并收入观察池"
                                    }
                                    onClick={() => void handleQuickAddWatchlist(row)}
                                  >
                                    {isArchived ? (
                                      <>
                                        <Check className="h-3 w-3 text-bull" aria-hidden="true" />
                                        <span>档案</span>
                                      </>
                                    ) : (
                                      <>
                                        <FolderPlus className="h-3 w-3 text-muted" aria-hidden="true" />
                                        <span>{addingWatchSym === symUpper ? "…" : "入池"}</span>
                                      </>
                                    )}
                                  </button>
                                </div>
                              </td>
                            </>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="py-10 text-center text-xs text-muted">
                选择上方共振板块池并点击「启动扫描」，此处实时流式呈现日线缠论定方向结果。
              </div>
            )}

            {/* Collapsible Execution Log */}
            <details className="disclosure">
              <summary>
                <span>SSE 执行流明细日志（{liveEvents.length} 条）</span>
              </summary>
              <div className="mt-2 max-h-40 overflow-y-auto rounded-md border border-hairline bg-sunken px-3 py-2 font-mono text-[11px] text-ink-2">
                {liveEvents.length === 0 ? (
                  <div className="py-2 text-center font-sans text-xs text-muted">暂无日志事件</div>
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
                            ? "扫描任务完毕"
                            : "事件更新");
                    return (
                      <div key={ev.seq ?? i} className="py-0.5">
                        <span className="text-muted">[{shortTime(ev.ts) || `#${ev.seq ?? i + 1}`}]</span>{" "}
                        <span className="text-accent">{ev.type}</span> · <span>{text}</span>
                      </div>
                    );
                  })
                )}
              </div>
            </details>
          </section>
        </div>

        {/* Right Sidebar: Transparent Scan History (shows pool names instead of opaque hashes) */}
        <aside className="card flex flex-col overflow-hidden xl:col-span-3" aria-label="近期扫描记录">
          <div className="flex items-center justify-between border-b border-hairline px-3.5 py-2.5">
            <span className="text-[13px] font-semibold text-ink">近期扫描记录 ({tasks.length})</span>
            <button
              type="button"
              className="btn btn-ghost btn-sm px-2"
              onClick={() => void loadTasks()}
              aria-label="刷新记录"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
          <ul className="m-0 max-h-[68dvh] flex-1 list-none divide-y divide-hairline-soft overflow-y-auto p-0">
            {tasks.length === 0 ? (
              <li className="p-5 text-center text-xs text-muted">暂无本轮扫描历史</li>
            ) : (
              tasks.map((t) => {
                const selected = t.id === activeTaskId;
                const Icon =
                  t.status === "completed"
                    ? CheckCircle2
                    : t.status === "failed"
                      ? XCircle
                      : t.status === "cancelled"
                        ? Square
                        : Clock;
                const iconCls =
                  t.status === "completed"
                    ? "text-bull"
                    : t.status === "failed"
                      ? "text-critical"
                      : t.status === "cancelled"
                        ? "text-warning"
                        : "text-accent";
                return (
                  <li key={t.id}>
                    <button
                      type="button"
                      onClick={() => setActiveTaskId(t.id)}
                      className={`flex w-full cursor-pointer flex-col gap-1 px-3.5 py-2.5 text-left text-xs ${
                        selected ? "bg-elevated text-ink" : "text-ink-2 hover:bg-elevated/60"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium text-ink">
                          {describeScanTaskScope(t)}
                        </span>
                        <Icon className={`h-3.5 w-3.5 shrink-0 ${iconCls}`} aria-hidden="true" />
                      </div>
                      <div className="flex items-center justify-between font-mono text-[10.5px] text-muted">
                        <span>{shortTime(t.created_at)} UTC</span>
                        <span>#{t.id.slice(0, 6)}</span>
                      </div>
                    </button>
                  </li>
                );
              })
            )}
          </ul>
          <div className="border-t border-hairline-soft bg-sunken/50 px-3.5 py-2 text-[11px] text-muted">
            <span className="inline-flex items-center gap-1">
              <BookOpen className="h-3 w-3" aria-hidden="true" />
              <span>单标硬数据刷新已就地集成至「研报档案」</span>
            </span>
          </div>
        </aside>
      </div>
      </div>

      <SaveUniverseDialog
        open={savePoolOpen}
        defaultName={savePoolPrefill?.name}
        defaultDescription={savePoolPrefill?.description}
        defaultSymbols={savePoolPrefill?.symbols ?? candidateSaveSymbols}
        onClose={() => {
          setSavePoolOpen(false);
          setSavePoolPrefill(null);
        }}
        onSaved={() => void loadUniverses()}
      />
    </div>
  );
}

function SaveUniverseDialog({
  open,
  defaultName,
  defaultDescription,
  defaultSymbols,
  onClose,
  onSaved,
}: {
  open: boolean;
  defaultName?: string;
  defaultDescription?: string;
  defaultSymbols: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [symbolsText, setSymbolsText] = useState("");
  const [allowOverwrite, setAllowOverwrite] = useState(false);
  const [conflictDetected, setConflictDetected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(defaultName || "");
    setDesc(defaultDescription || "");
    setSymbolsText(defaultSymbols.join(", "));
    setAllowOverwrite(false);
    setConflictDetected(false);
    setErr(null);
  }, [open, defaultName, defaultDescription, defaultSymbols]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const syms = parseSymbolList(symbolsText);
    if (!name.trim() || !desc.trim() || syms.length === 0) {
      setErr("请填写池代号、1Q~1Y 驱动逻辑说明与至少 1 只真龙头代码");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.saveUniverse({
        name: name.trim(),
        description: desc.trim(),
        symbols: syms,
        overwrite: allowOverwrite,
      });
      onSaved();
      onClose();
    } catch (ex) {
      if (ex instanceof ApiError && ex.status === 409) {
        setConflictDetected(true);
      }
      setErr(errorMessage(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title="沉淀新主题龙头池（universes.json）"
      subtitle="默认仅新建不覆盖已有池；每个细分环节只保留真正龙头（或双寡头最多 2 只）"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <label className="field">
          <span>主题池英文代号 *</span>
          <input
            className="input font-mono"
            required
            autoFocus
            placeholder="如 cn_grid_export / us_glp1_leaders"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setConflictDetected(false);
              setAllowOverwrite(false);
            }}
          />
        </label>

        <label className="field">
          <span>1Q~1Y 驱动逻辑与产业链说明 *</span>
          <input
            className="input"
            required
            placeholder="如 北美与欧洲电网变压器缺口 + 国内特高压龙头出海共振"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
          />
        </label>

        <label className="field">
          <span>核心真龙头列表（逗号或空格分隔）*</span>
          <textarea
            className="input font-mono"
            rows={2}
            required
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
          />
        </label>

        {conflictDetected ? (
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <input
              type="checkbox"
              checked={allowOverwrite}
              onChange={(e) => setAllowOverwrite(e.target.checked)}
              className="accent-warning"
            />
            <span>确认覆盖已存在的同名主题池「{name.trim()}」</span>
          </label>
        ) : null}

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
            {busy ? "正在保存…" : allowOverwrite ? "确认覆盖已有池" : "保存至 universes.json"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
