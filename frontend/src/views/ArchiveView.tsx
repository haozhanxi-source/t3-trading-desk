import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot,
  CalendarPlus,
  Crosshair,
  Database,
  Edit3,
  ExternalLink,
  FilePlus2,
  FileText,
  ListChecks,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  ShieldAlert,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api, ApiError, errorMessage, parseTaskEvent } from "../api/client";
import type {
  ArchiveDetail,
  ArchiveListItem,
  ArchiveStatus,
  ArchiveTrigger,
  DeepResearchEngine,
  DeepResearchMode,
  EvidenceItem,
  ResearchDataResponse,
  ResearchEnginesResponse,
  TaskRecord,
} from "../api/types";
import {
  DeepResearchDialog,
  DeepResearchProgressCard,
  type DeepResearchLogLine,
  type DeepResearchRunState,
} from "../components/archive/DeepResearchControls";
import { HardDataVisuals } from "../components/archive/HardDataVisuals";
import { ModalDialog } from "../components/ui/ModalDialog";
import { DirectionBadge, StatusBadge } from "../components/ui/StatusBadge";
import { SymbolLabel } from "../components/ui/SymbolLabel";
import { shortTime } from "../lib/format";
import { formatSymbolWithCode, getSymbolName } from "../lib/symbols";

type ArchiveTab = "thesis" | "evidence" | "harddata" | "triggers";

interface ArchiveSelection {
  market: string | null;
  symbol: string;
}

function makeArchiveKey(market: string | null | undefined, symbol: string): string {
  return `${(market || "").trim().toUpperCase()}::${symbol.trim().toUpperCase()}`;
}

const TRIGGER_KIND_OPTIONS: { value: string; label: string }[] = [
  { value: "chanlun", label: "缠论防线 (chanlun)" },
  { value: "supply_demand", label: "供需与库存 (supply_demand)" },
  { value: "term_structure", label: "期限与基差 (term_structure)" },
  { value: "rate_diff", label: "国债利差 (rate_diff)" },
  { value: "fundamental", label: "基本面预期 (fundamental)" },
  { value: "policy", label: "资源国与监管政策 (policy)" },
  { value: "spread", label: "内外跨市价差 (spread)" },
  { value: "freight", label: "海运指数 (freight)" },
  { value: "macro", label: "中美宏观双核 (macro)" },
];

const TRIGGER_KIND_ZH: Record<string, string> = {
  chanlun: "缠论防线",
  supply_demand: "供需库存",
  term_structure: "期限基差",
  rate_diff: "国债利差",
  fundamental: "基本面",
  policy: "产业/资源政策",
  spread: "跨市价差",
  freight: "海运费",
  macro: "宏观双核",
};

const TRIGGER_ACTION_ZH: Record<string, string> = {
  exit_review: "果断撤离重审",
  trim_or_exit: "减仓或撤离",
  alert: "指标松动预警",
};

const METRIC_ZH_HINTS: Record<string, string> = {
  vix: "VIX 恐慌指数",
  dxy: "美元指数 DXY",
  us10y_yield: "10Y 美债收益率",
  cn10y_yield: "10Y 中债收益率",
  dfii10: "10Y 美债实际利率",
  audusd_30m_close: "澳美 30m 收盘价",
  audusd_daily_close: "澳美日线收盘价",
  iron_ore_62_cfr: "铁矿 62% CFR 报价",
};

function formatTriggerMetric(metric: string | undefined): { primary: string; raw?: string } {
  const m = (metric || "").trim();
  if (!m) return { primary: "监控项" };
  const hint = METRIC_ZH_HINTS[m.toLowerCase()];
  if (hint) return { primary: hint, raw: m };
  return { primary: m };
}

const AUDIT_HEADING_LINE_RE =
  /^##\s*(?:增量修订日志|修订与核验日志|修订日志)(?:\s*(?:（[^）]*）|\([^)]*\)))?\s*$/;
const OPEN_FENCE_RE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const DATED_LOG_LINE_RE = /^-\s+`\d{4}-\d{2}-\d{2}`[：:]/;

/** CommonMark-compliant fence-aware extraction of editable thesis body */
function extractEditableBody(md: string): string {
  const raw = md || "";
  const lines = raw.split(/\r?\n/);
  let activeFence: { ch: string; len: number } | null = null;
  let splitIdx = -1;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    const fm = OPEN_FENCE_RE.exec(line);
    if (fm && fm[1]) {
      const delim = fm[1];
      const rest = fm[2] ?? "";
      const ch = delim[0] ?? "`";
      const len = delim.length;
      if (activeFence === null) {
        if (!(ch === "`" && rest.includes("`"))) {
          activeFence = { ch, len };
          continue;
        }
      } else if (ch === activeFence.ch && len >= activeFence.len && rest.trim() === "") {
        activeFence = null;
        continue;
      }
    }
    if (activeFence === null && AUDIT_HEADING_LINE_RE.test(line.trim())) {
      splitIdx = i;
    }
  }

  if (splitIdx >= 0) {
    let cut = splitIdx;
    let probe = splitIdx - 1;
    while (probe >= 0 && (lines[probe] ?? "").trim() === "") probe -= 1;
    if (probe >= 0 && (lines[probe] ?? "").trim() === "---") cut = probe;
    return lines.slice(0, cut).join("\n").trimEnd();
  }

  /* Rescue trailing headerless dated log bullets ONLY when EOF is not inside an unclosed code fence */
  if (activeFence === null) {
    let end = lines.length;
    while (end > 0 && (lines[end - 1] ?? "").trim() === "") end -= 1;
    let logStart = end;
    while (logStart > 0 && DATED_LOG_LINE_RE.test((lines[logStart - 1] ?? "").trim())) {
      logStart -= 1;
    }
    if (logStart < end) {
      return lines.slice(0, logStart).join("\n").trimEnd();
    }
  }
  return raw.trimEnd();
}

/** Strip redundant top `# [SYMBOL] ...` H1 during read-mode since card header already displays SymbolLabel */
function cleanThesisDisplayMarkdown(md: string): string {
  return (md || "").replace(/^#\s+\[[^\]]+\][^\n]*\n+/, "");
}

function statusBadgeLabel(st: string | undefined): string {
  if (st === "holding") return "持有中";
  if (st === "watchlist") return "逻辑观察";
  if (st === "actionable_long") return "放行做多";
  if (st === "actionable_short") return "放行做空";
  if (st === "pair_trade") return "配对候选";
  if (st === "rejected" || st === "closed") return "已归档";
  return st || "观察池";
}

interface ArchiveViewProps {
  active?: boolean;
  symbol: string;
  market?: string | null;
  workspaceRev?: number;
  onArchiveMutated?: () => void;
  onSelectSymbol: (symbol: string, market?: string | null) => void;
  onJumpWarRoom: (symbol: string) => void;
}

export function ArchiveView({
  active = true,
  symbol,
  market = null,
  workspaceRev = 0,
  onArchiveMutated,
  onSelectSymbol,
  onJumpWarRoom,
}: ArchiveViewProps) {
  const [archives, setArchives] = useState<ArchiveListItem[]>([]);
  const [archivesLoaded, setArchivesLoaded] = useState(false);
  const [selected, setSelected] = useState<ArchiveSelection>({ market, symbol });
  const prevPropKeyRef = useRef(makeArchiveKey(market, symbol));

  const [filterText, setFilterText] = useState("");
  const [detail, setDetail] = useState<ArchiveDetail | null>(null);
  const [notFoundSym, setNotFoundSym] = useState<string | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [tab, setTab] = useState<ArchiveTab>("thesis");
  const [levelFilter, setLevelFilter] = useState<"ALL" | "L1" | "L2" | "L3" | "L4">("ALL");

  /* Online thesis editor state pinned to immutable base revision captured on open */
  const [editSession, setEditSession] = useState<{
    symbol: string;
    market: string;
    baseRevision: number;
  } | null>(null);
  const editingThesis = editSession !== null;
  const [draftThesisBody, setDraftThesisBody] = useState("");
  const [editNote, setEditNote] = useState("");
  const [savingThesis, setSavingThesis] = useState(false);

  /* Visual Trigger & Event builder states */
  const [trigKind, setTrigKind] = useState("chanlun");
  const [trigMetric, setTrigMetric] = useState("");
  const [trigOp, setTrigOp] = useState("<");
  const [trigThreshold, setTrigThreshold] = useState("");
  const [trigAction, setTrigAction] = useState("exit_review");
  const [trigDesc, setTrigDesc] = useState("");
  const [trigBusy, setTrigBusy] = useState(false);

  const [evDate, setEvDate] = useState("");
  const [evTitle, setEvTitle] = useState("");
  const [evBusy, setEvBusy] = useState(false);

  /* Hard data lazy cache */
  const [hardData, setHardData] = useState<ResearchDataResponse | null>(null);
  const [hardLoading, setHardLoading] = useState(false);
  const [hardErr, setHardErr] = useState<string | null>(null);
  const [failedHardKey, setFailedHardKey] = useState<string | null>(null);
  const hardAbortRef = useRef<AbortController | null>(null);
  const hardReqIdRef = useRef(0);
  const detailReqIdRef = useRef(0);
  const loadHardDataRef = useRef<(forceRefresh?: boolean) => Promise<void>>(async () => {});

  /* In-place Hard Data + Trigger Check Refresh Action (per-key runId map prevents cross-archive collision) */
  const [checkingRuns, setCheckingRuns] = useState<Record<string, number>>({});
  const checkRunSeqRef = useRef(0);
  const [checkFeedback, setCheckFeedback] = useState<string | null>(null);

  /* Inline log append */
  const [logEntry, setLogEntry] = useState("");
  const [logBusy, setLogBusy] = useState(false);

  /* Modals */
  const [initModalOpen, setInitModalOpen] = useState(false);
  const [evidenceModalOpen, setEvidenceModalOpen] = useState(false);
  const [deepDialogOpen, setDeepDialogOpen] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);

  /* AI Deep Research engine probe + per-archive SSE task map */
  const [enginesInfo, setEnginesInfo] = useState<ResearchEnginesResponse | null>(null);
  const [deepRuns, setDeepRuns] = useState<Record<string, DeepResearchRunState>>({});
  const [cancellingTasks, setCancellingTasks] = useState<Record<string, boolean>>({});
  const deepStreamsRef = useRef<Map<string, EventSource>>(new Map());

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
  const checkBusy = Boolean(checkingRuns[activeKey]);
  const activeKeyRef = useRef(activeKey);
  activeKeyRef.current = activeKey;
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
      setArchivesLoaded(true);
      return list;
    } catch {
      if (!signal?.aborted) setArchivesLoaded(true);
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
  }, [loadList, selectArchiveTuple, symbol, workspaceRev]);

  const loadedKeyRef = useRef<string>("");
  const prevWorkspaceRevRef = useRef(workspaceRev);

  useEffect(() => {
    if (workspaceRev !== prevWorkspaceRevRef.current) {
      prevWorkspaceRevRef.current = workspaceRev;
      /* Invalidate cached detail so active or re-activated ArchiveView re-verifies fresh state */
      loadedKeyRef.current = "";
    }
  }, [workspaceRev]);

  useEffect(() => {
    if (!active) return;
    if (loadedKeyRef.current === activeKey && (detail || notFoundSym)) return;

    const token = ++selectionTokenRef.current;
    const detailCtrl = new AbortController();
    const isDifferentArchive =
      !editSession ||
      makeArchiveKey(editSession.market, editSession.symbol) !== activeKey;
    loadedKeyRef.current = activeKey;
    if (isDifferentArchive) {
      setDetail(null);
      setEditSession(null);
    }
    setCheckFeedback(null);

    if (hardAbortRef.current) {
      hardAbortRef.current.abort();
      hardAbortRef.current = null;
    }
    hardReqIdRef.current += 1;
    setHardData(null);
    setHardErr(null);
    setHardLoading(false);
    setFailedHardKey(null);

    if (
      archivesLoaded &&
      !activeSym.includes(":") &&
      !archives.some((a) => a.symbol.toUpperCase() === activeSym.toUpperCase())
    ) {
      setNotFoundSym(activeSym);
      setLoadingDetail(false);
      return () => detailCtrl.abort();
    }

    void loadDetail(activeSym, activeMarket, detailCtrl.signal, token);
    return () => {
      detailCtrl.abort();
    };
  }, [active, activeKey, activeSym, activeMarket, archivesLoaded, archives, detail, notFoundSym, editSession, workspaceRev, loadDetail]);

  useEffect(() => {
    const streams = deepStreamsRef.current;
    return () => {
      hardAbortRef.current?.abort();
      for (const es of streams.values()) {
        es.close();
      }
      streams.clear();
    };
  }, []);

  const formatLogClock = (iso?: string): string => {
    const d = iso ? new Date(iso) : new Date();
    if (Number.isNaN(d.getTime())) return new Date().toTimeString().slice(0, 8);
    return d.toTimeString().slice(0, 8);
  };

  const attachDeepResearchStream = useCallback(
    (task: TaskRecord, fallbackSym: string, fallbackMarket: string | null) => {
      const params = (task.params || {}) as Record<string, unknown>;
      const sym = String(params.symbol || fallbackSym || "").trim().toUpperCase();
      const mktRaw = params.market ? String(params.market).trim().toUpperCase() : fallbackMarket?.trim().toUpperCase() ?? null;
      if (!sym) return;
      const runKey = makeArchiveKey(mktRaw, sym);
      const taskId = task.id || task.task_id || "";
      if (!taskId) return;

      const mode: DeepResearchMode = params.mode === "adversarial" ? "adversarial" : "standard";
      const engine: DeepResearchEngine = params.engine === "codex" ? "codex" : "claude";
      const parsedStart = task.started_at || task.created_at ? Date.parse(String(task.started_at || task.created_at)) : NaN;
      const startedAtMs = Number.isFinite(parsedStart) ? parsedStart : Date.now();

      const progObj =
        task.progress && typeof task.progress === "object" ? task.progress : null;
      setDeepRuns((prev) => {
        const cur = prev[runKey];
        if (cur && cur.taskId === taskId && (cur.status === "queued" || cur.status === "running")) {
          return prev;
        }
        return {
          ...prev,
          [runKey]: {
            taskId,
            symbol: sym,
            market: mktRaw,
            mode,
            engine,
            status: task.status === "queued" ? "queued" : "running",
            stage: "collect_data",
            done: progObj?.done ?? 0,
            total: progObj?.total ?? 5,
            startedAtMs,
            logs: [],
          },
        };
      });

      if (deepStreamsRef.current.has(taskId)) return;

      const es = new EventSource(api.taskEventsUrl(taskId));
      deepStreamsRef.current.set(taskId, es);

      const closeStream = () => {
        es.close();
        if (deepStreamsRef.current.get(taskId) === es) {
          deepStreamsRef.current.delete(taskId);
        }
      };

      const appendLog = (stage: string | undefined, message: string, ts?: string) => {
        const clean = message.trim();
        if (!clean) return;
        const entry: DeepResearchLogLine = { ts: formatLogClock(ts), stage, message: clean };
        setDeepRuns((prev) => {
          const cur = prev[runKey];
          if (!cur || cur.taskId !== taskId) return prev;
          const prevLogs = cur.logs;
          const last = prevLogs[prevLogs.length - 1];
          if (last && last.message === entry.message) return prev;
          return {
            ...prev,
            [runKey]: {
              ...cur,
              status: "running",
              stage: stage || cur.stage,
              logs: [...prevLogs.slice(-39), entry],
            },
          };
        });
      };

      es.addEventListener("step", (ev) => {
        const parsed = typeof (ev as MessageEvent).data === "string" ? parseTaskEvent((ev as MessageEvent).data, "step") : null;
        if (!parsed) return;
        const stage = typeof parsed.data?.stage === "string" ? parsed.data.stage : undefined;
        const msg = String(parsed.data?.message || parsed.message || "");
        if (msg && msg !== "deep_research started") {
          appendLog(stage, msg, parsed.ts);
        } else if (stage) {
          setDeepRuns((prev) => {
            const cur = prev[runKey];
            if (!cur || cur.taskId !== taskId) return prev;
            return { ...prev, [runKey]: { ...cur, status: "running", stage } };
          });
        }
      });

      es.addEventListener("log", (ev) => {
        const parsed = typeof (ev as MessageEvent).data === "string" ? parseTaskEvent((ev as MessageEvent).data, "log") : null;
        if (!parsed) return;
        const stage = typeof parsed.data?.stage === "string" ? parsed.data.stage : undefined;
        const msg = String(parsed.data?.message || parsed.message || "");
        appendLog(stage, msg, parsed.ts);
      });

      es.addEventListener("progress", (ev) => {
        const parsed = typeof (ev as MessageEvent).data === "string" ? parseTaskEvent((ev as MessageEvent).data, "progress") : null;
        if (!parsed) return;
        const flat = parsed as unknown as Record<string, unknown>;
        const done =
          typeof parsed.data?.done === "number"
            ? parsed.data.done
            : typeof flat.done === "number"
              ? flat.done
              : undefined;
        const total =
          typeof parsed.data?.total === "number"
            ? parsed.data.total
            : typeof flat.total === "number"
              ? flat.total
              : undefined;
        setDeepRuns((prev) => {
          const cur = prev[runKey];
          if (!cur || cur.taskId !== taskId) return prev;
          return {
            ...prev,
            [runKey]: {
              ...cur,
              status: "running",
              done: done ?? cur.done,
              total: total ?? cur.total,
            },
          };
        });
      });

      es.addEventListener("done", (ev) => {
        closeStream();
        const parsed = typeof (ev as MessageEvent).data === "string" ? parseTaskEvent((ev as MessageEvent).data, "done") : null;
        const res = (parsed?.data?.result ?? parsed?.result ?? {}) as Record<string, unknown>;
        const rev = res.revision ? `v${res.revision}` : "";
        const dir = res.direction_bias ? `方向 ${String(res.direction_bias).toUpperCase()}` : "";
        const evAdded = typeof res.evidence_added === "number" ? `新增 ${res.evidence_added} 条证据` : "";
        const trigCnt = typeof res.triggers_count === "number" ? `${res.triggers_count} 项触发器` : "";
        const evCnt = typeof res.events_count === "number" ? `${res.events_count} 条催化事件` : "";
        const summary = [rev, dir, evAdded, trigCnt, evCnt].filter(Boolean).join(" · ") || "研报已成功写回档案";

        setDeepRuns((prev) => {
          const cur = prev[runKey];
          if (!cur || cur.taskId !== taskId) return prev;
          return {
            ...prev,
            [runKey]: {
              ...cur,
              status: "completed",
              stage: "persist_archive",
              done: cur.total,
              finishedAtMs: Date.now(),
              resultSummary: summary,
            },
          };
        });

        loadedKeyRef.current = "";
        void loadList();
        if (activeKeyRef.current === runKey || makeArchiveKey(null, sym) === activeKeyRef.current) {
          void loadDetail(sym, mktRaw);
          void loadHardDataRef.current(false);
        }
        onArchiveMutated?.();
      });

      es.addEventListener("cancelled", (ev) => {
        closeStream();
        const rawData = typeof (ev as MessageEvent).data === "string" ? (ev as MessageEvent).data : "";
        const parsed = rawData ? parseTaskEvent(rawData, "cancelled") : null;
        const msg = String(parsed?.data?.message || parsed?.message || "用户已终止该研究任务");
        setDeepRuns((prev) => {
          const cur = prev[runKey];
          if (!cur || cur.taskId !== taskId) return prev;
          return {
            ...prev,
            [runKey]: {
              ...cur,
              status: "cancelled",
              finishedAtMs: Date.now(),
              error: msg,
            },
          };
        });
      });

      es.addEventListener("error", (ev) => {
        const rawData = typeof (ev as MessageEvent).data === "string" ? (ev as MessageEvent).data : "";
        if (rawData) {
          closeStream();
          const parsed = parseTaskEvent(rawData, "error");
          const msg = String(parsed?.data?.message || parsed?.message || "深度研究执行异常");
          setDeepRuns((prev) => {
            const cur = prev[runKey];
            if (!cur || cur.taskId !== taskId) return prev;
            return {
              ...prev,
              [runKey]: {
                ...cur,
                status: "failed",
                finishedAtMs: Date.now(),
                error: msg,
              },
            };
          });
          return;
        }
        /* Native EventSource network error: verify actual server task status before failing */
        void api
          .task(taskId)
          .then((latestTask) => {
            if (latestTask.status === "completed") {
              closeStream();
              setDeepRuns((prev) => {
                const cur = prev[runKey];
                if (!cur || cur.taskId !== taskId) return prev;
                return {
                  ...prev,
                  [runKey]: {
                    ...cur,
                    status: "completed",
                    done: cur.total,
                    finishedAtMs: Date.now(),
                    resultSummary: "深度研究已在后台完成并写回档案",
                  },
                };
              });
              loadedKeyRef.current = "";
              void loadList();
              if (activeKeyRef.current === runKey) void loadDetail(sym, mktRaw);
              onArchiveMutated?.();
            } else if (latestTask.status === "failed" || latestTask.status === "cancelled") {
              closeStream();
              setDeepRuns((prev) => {
                const cur = prev[runKey];
                if (!cur || cur.taskId !== taskId) return prev;
                return {
                  ...prev,
                  [runKey]: {
                    ...cur,
                    status: latestTask.status === "cancelled" ? "cancelled" : "failed",
                    finishedAtMs: Date.now(),
                    error: latestTask.error || "研究任务已结束",
                  },
                };
              });
            }
          })
          .catch(() => {
            /* Keep EventSource auto-reconnect alive on transient proxy hiccups */
          });
      });
    },
    [loadDetail, loadList, onArchiveMutated],
  );

  useEffect(() => {
    if (!active) return;
    const ctrl = new AbortController();
    void api
      .researchEngines(ctrl.signal)
      .then((res) => {
        if (!ctrl.signal.aborted) setEnginesInfo(res);
      })
      .catch(() => {});

    void api
      .tasks(ctrl.signal)
      .then((res) => {
        if (ctrl.signal.aborted) return;
        for (const t of res.tasks ?? []) {
          if (t.kind === "deep_research" && (t.status === "queued" || t.status === "running")) {
            const p = (t.params || {}) as Record<string, unknown>;
            attachDeepResearchStream(t, String(p.symbol || ""), p.market ? String(p.market) : null);
          }
        }
      })
      .catch(() => {});

    return () => ctrl.abort();
  }, [active, attachDeepResearchStream]);

  const handleLaunchDeepResearch = useCallback(
    async (opts: {
      symbol?: string;
      market?: string | null;
      mode: DeepResearchMode;
      engine: DeepResearchEngine;
      focus_prompt: string;
    }) => {
      const targetSym = (opts.symbol || detail?.symbol || activeSym).trim();
      const targetMkt = opts.market !== undefined ? opts.market : (detail?.market ?? activeMarket);
      if (!targetSym) return;
      const task = await api.startDeepResearch({
        symbol: targetSym,
        market: targetMkt,
        mode: opts.mode,
        engine: opts.engine,
        focus_prompt: opts.focus_prompt,
        include_macro: true,
      });
      attachDeepResearchStream(task, targetSym, targetMkt);
    },
    [activeMarket, activeSym, attachDeepResearchStream, detail?.market, detail?.symbol],
  );

  const handleCancelDeepResearch = useCallback(async (run: DeepResearchRunState) => {
    setCancellingTasks((prev) => ({ ...prev, [run.taskId]: true }));
    try {
      await api.cancelTask(run.taskId);
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setCancellingTasks((prev) => {
        const next = { ...prev };
        delete next[run.taskId];
        return next;
      });
    }
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
  loadHardDataRef.current = loadHardData;

  useEffect(() => {
    if (tab === "harddata" && !hardData && !hardLoading && failedHardKey !== activeKey && activeSym) {
      void loadHardData(false);
    }
  }, [tab, hardData, hardLoading, failedHardKey, activeKey, activeSym, loadHardData]);

  const handleRunInPlaceRefresh = async () => {
    if (!activeSym || checkBusy) return;
    const targetSym = detail?.symbol || activeSym;
    const targetMkt = detail?.market ?? activeMarket;
    const reqKey = activeKey;
    const myRunId = ++checkRunSeqRef.current;
    const capturedToken = selectionTokenRef.current;
    setCheckingRuns((prev) => ({ ...prev, [reqKey]: myRunId }));
    setCheckFeedback("正在后台拉取实时硬数据并核验档案触发器…");

    try {
      const task = await api.startResearchRefresh({
        symbol: targetSym,
        include_macro: true,
        market: targetMkt,
      });
      const outcome = await new Promise<
        | { kind: "done"; result?: Record<string, unknown> }
        | { kind: "error"; message: string }
        | { kind: "timeout" }
      >((resolve) => {
        const es = new EventSource(api.taskEventsUrl(task.id));
        let settled = false;
        const finish = (val: { kind: "done"; result?: Record<string, unknown> } | { kind: "error"; message: string } | { kind: "timeout" }) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          es.close();
          resolve(val);
        };
        const timer = window.setTimeout(() => finish({ kind: "timeout" }), 45_000);
        es.addEventListener("done", (ev) => {
          const parsed = typeof (ev as MessageEvent).data === "string" ? parseTaskEvent((ev as MessageEvent).data, "done") : null;
          const resObj = (parsed?.data?.result ?? parsed?.result) as Record<string, unknown> | undefined;
          finish({ kind: "done", result: resObj });
        });
        es.addEventListener("error", (ev) => {
          const rawData = typeof (ev as MessageEvent).data === "string" ? (ev as MessageEvent).data : "";
          const parsed = rawData ? parseTaskEvent(rawData, "error") : null;
          const msg = parsed?.data?.message || parsed?.message || "后台核验流连接中断或任务异常";
          finish({ kind: "error", message: String(msg) });
        });
        es.addEventListener("cancelled", (ev) => {
          const rawData = typeof (ev as MessageEvent).data === "string" ? (ev as MessageEvent).data : "";
          const parsed = rawData ? parseTaskEvent(rawData, "cancelled") : null;
          const msg = parsed?.data?.message || parsed?.message || "任务已终止";
          finish({ kind: "error", message: String(msg) });
        });
      });

      if (outcome.kind === "timeout") {
        if (capturedToken === selectionTokenRef.current) {
          setCheckFeedback("核验等待超时（后台任务仍在继续，可稍后点击刷新查看结果）");
        }
        return;
      }
      if (outcome.kind === "error") {
        if (capturedToken === selectionTokenRef.current) {
          setCheckFeedback(`核验失败：${outcome.message}`);
        }
        return;
      }

      if (capturedToken === selectionTokenRef.current) {
        /* Read freshly populated server cache via forceRefresh=false instead of redundant re-scrape */
        await Promise.all([
          loadDetail(targetSym, targetMkt, undefined, capturedToken),
          loadHardData(false),
        ]);
        if (capturedToken === selectionTokenRef.current) {
          const fired = typeof outcome.result?.fired_count === "number" ? outcome.result.fired_count : 0;
          const exists = outcome.result?.archive_exists !== false;
          setCheckFeedback(
            exists
              ? `已完成实时硬数据更新与触发器核验（触发 ${fired} 项）`
              : "已拉取实时硬数据（当前标的尚未建立本地档案）",
          );
        }
      }
      void loadList();
      onArchiveMutated?.();
    } catch (ex) {
      if (capturedToken === selectionTokenRef.current) {
        setCheckFeedback(`刷新提示：${errorMessage(ex)}`);
      }
    } finally {
      setCheckingRuns((prev) => {
        if (prev[reqKey] !== myRunId) return prev;
        const next = { ...prev };
        delete next[reqKey];
        return next;
      });
    }
  };

  const handleStartEditThesis = () => {
    if (!detail) return;
    setDraftThesisBody(extractEditableBody(detail.thesis_md));
    setEditNote("");
    setEditSession({
      symbol: detail.symbol,
      market: detail.market,
      baseRevision: detail.state.revision,
    });
  };

  const handleSaveThesis = async (e: FormEvent) => {
    e.preventDefault();
    if (!editSession || !draftThesisBody.trim()) return;
    const { symbol: targetSym, market: targetMkt, baseRevision } = editSession;
    const token = selectionTokenRef.current;
    setSavingThesis(true);
    setErr(null);
    try {
      await api.archiveUpdateThesis(
        targetSym,
        draftThesisBody,
        baseRevision,
        editNote.trim() || undefined,
        targetMkt,
      );
      if (selectionTokenRef.current === token) {
        setEditSession(null);
        await loadDetail(targetSym, targetMkt, undefined, token);
      }
      await loadList();
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) {
        /* Keep draft open on revision conflict so user never loses typed edits */
        setErr(`${errorMessage(ex)}（已保留当前编辑草稿，可先复制草稿后刷新比对最新版本）`);
      }
    } finally {
      setSavingThesis(false);
    }
  };

  const handleAddTrigger = async (e: FormEvent) => {
    e.preventDefault();
    if (!detail || !trigMetric.trim() || !trigThreshold.trim()) return;
    const targetSym = detail.symbol;
    const targetMkt = detail.market;
    const token = selectionTokenRef.current;
    const rawTh = trigThreshold.trim();
    const numTh = Number(rawTh);
    const parsedThreshold: number | string =
      rawTh !== "" && Number.isFinite(numTh) && trigOp !== "contains" && trigOp !== "event"
        ? numTh
        : rawTh;

    const newItem: ArchiveTrigger = {
      id: `trig_${Date.now().toString(36)}`,
      kind: trigKind,
      metric: trigMetric.trim(),
      operator: trigOp,
      threshold: parsedThreshold,
      action: trigAction,
      status: "armed",
      ...(trigDesc.trim() ? { description: trigDesc.trim() } : {}),
    };
    const nextTriggers = [...(detail.state.triggers ?? []), newItem];

    setTrigBusy(true);
    setErr(null);
    try {
      await api.archiveState(targetSym, { triggers: nextTriggers }, detail.state.revision, targetMkt);
      if (selectionTokenRef.current === token) {
        setTrigMetric("");
        setTrigThreshold("");
        setTrigDesc("");
        await loadDetail(targetSym, targetMkt, undefined, token);
      }
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) setErr(errorMessage(ex));
    } finally {
      setTrigBusy(false);
    }
  };

  const handleRemoveTrigger = async (idxToRemove: number) => {
    if (!detail) return;
    const targetSym = detail.symbol;
    const targetMkt = detail.market;
    const token = selectionTokenRef.current;
    const nextTriggers = (detail.state.triggers ?? []).filter((_, idx) => idx !== idxToRemove);
    setTrigBusy(true);
    setErr(null);
    try {
      await api.archiveState(targetSym, { triggers: nextTriggers }, detail.state.revision, targetMkt);
      if (selectionTokenRef.current === token) {
        await loadDetail(targetSym, targetMkt, undefined, token);
      }
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) setErr(errorMessage(ex));
    } finally {
      setTrigBusy(false);
    }
  };

  const handleAddEvent = async (e: FormEvent) => {
    e.preventDefault();
    if (!detail || !evDate.trim() || !evTitle.trim()) return;
    const targetSym = detail.symbol;
    const targetMkt = detail.market;
    const token = selectionTokenRef.current;
    const nextEvents = [...(detail.state.events ?? []), { date: evDate.trim(), title: evTitle.trim() }].sort(
      (a, b) => a.date.localeCompare(b.date),
    );
    setEvBusy(true);
    setErr(null);
    try {
      await api.archiveState(targetSym, { events: nextEvents }, detail.state.revision, targetMkt);
      if (selectionTokenRef.current === token) {
        setEvDate("");
        setEvTitle("");
        await loadDetail(targetSym, targetMkt, undefined, token);
      }
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) setErr(errorMessage(ex));
    } finally {
      setEvBusy(false);
    }
  };

  const handleRemoveEvent = async (idxToRemove: number) => {
    if (!detail) return;
    const targetSym = detail.symbol;
    const targetMkt = detail.market;
    const token = selectionTokenRef.current;
    const nextEvents = (detail.state.events ?? []).filter((_, idx) => idx !== idxToRemove);
    setEvBusy(true);
    setErr(null);
    try {
      await api.archiveState(targetSym, { events: nextEvents }, detail.state.revision, targetMkt);
      if (selectionTokenRef.current === token) {
        await loadDetail(targetSym, targetMkt, undefined, token);
      }
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) setErr(errorMessage(ex));
    } finally {
      setEvBusy(false);
    }
  };

  const filteredArchives = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return archives;
    return archives.filter((a) => {
      const zh = a.name || getSymbolName(a.symbol) || "";
      return (
        a.symbol.toLowerCase().includes(q) ||
        zh.toLowerCase().includes(q) ||
        a.market.toLowerCase().includes(q)
      );
    });
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
      onArchiveMutated?.();
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
      onArchiveMutated?.();
    } catch (ex) {
      if (selectionTokenRef.current === token) {
        setErr(errorMessage(ex));
      }
    } finally {
      setStatusBusy(false);
    }
  };

  const resolvedActiveMarket = useMemo(() => {
    if (activeMarket) return activeMarket.toUpperCase();
    const firstHit = filteredArchives.find((a) => a.symbol.toUpperCase() === activeSym.toUpperCase());
    return firstHit?.market.toUpperCase() ?? null;
  }, [activeMarket, activeSym, filteredArchives]);

  const holdingArchives = useMemo(
    () => filteredArchives.filter((a) => a.status === "holding"),
    [filteredArchives],
  );
  const otherArchives = useMemo(
    () => filteredArchives.filter((a) => a.status !== "holding"),
    [filteredArchives],
  );

  const showCompanion = Boolean(detail && tab !== "harddata" && tab !== "triggers");

  const activeDeepRun = useMemo(() => {
    const exact = deepRuns[makeArchiveKey(effectiveMarket, detail?.symbol || activeSym)];
    if (exact) return exact;
    const symUpper = (detail?.symbol || activeSym).trim().toUpperCase();
    if (!symUpper) return undefined;
    return Object.values(deepRuns).find((r) => r.symbol.toUpperCase() === symUpper);
  }, [activeSym, deepRuns, detail?.symbol, effectiveMarket]);

  const isDeepRunning =
    activeDeepRun?.status === "queued" || activeDeepRun?.status === "running";

  const isBlankTemplate = Boolean(
    detail &&
      (detail.evidence_count ?? 0) === 0 &&
      (!detail.state.thesis_summary ||
        detail.state.thesis_summary.includes("待补充") ||
        (detail.thesis_md || "").includes("待补充：")),
  );

  const renderArchiveRow = (a: ArchiveListItem) => {
    const activeRow =
      a.symbol.toUpperCase() === activeSym.toUpperCase() &&
      (resolvedActiveMarket === null || a.market.toUpperCase() === resolvedActiveMarket);
    const rowRun = deepRuns[makeArchiveKey(a.market, a.symbol)];
    const rowResearching = rowRun?.status === "queued" || rowRun?.status === "running";
    const dotClass = rowResearching
      ? "bg-accent animate-ping"
      : a.status === "holding"
        ? "bg-bull"
        : a.status === "watchlist"
          ? "bg-warning"
          : "bg-muted";
    return (
      <li key={`${a.market}-${a.symbol}`}>
        <button
          type="button"
          onClick={() => selectArchiveTuple(a.market, a.symbol)}
          className={`flex w-full cursor-pointer flex-col gap-1 rounded-md px-2.5 py-2 text-left text-xs transition-colors ${
            activeRow
              ? "bg-elevated text-ink ring-1 ring-accent/40"
              : "text-ink-2 hover:bg-elevated/60 hover:text-ink"
          }`}
        >
          <div className="flex items-center justify-between gap-2">
            <SymbolLabel symbol={a.symbol} name={a.name} size="sm" badgeTicker={false} />
            <span className="flex shrink-0 items-center gap-1.5">
              {rowResearching ? (
                <span className="rounded bg-accent/15 px-1 py-0.5 text-[10px] font-medium text-accent">
                  研究中
                </span>
              ) : null}
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotClass}`} title={a.status} aria-hidden="true" />
            </span>
          </div>
          <div className="flex items-center justify-between gap-2 text-[11px] text-muted">
            <span className="font-mono">
              {a.market} · v{a.revision}
            </span>
            <DirectionBadge direction={a.direction_bias} />
          </div>
        </button>
      </li>
    );
  };

  return (
    <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-12">
      {/* Left Column: Archive Catalog */}
      <aside
        className="card flex flex-col overflow-hidden xl:sticky xl:top-16 xl:col-span-3"
        aria-label="研究档案目录"
      >
        <div className="flex items-center justify-between gap-2 border-b border-hairline px-3.5 py-2.5">
          <div>
            <span className="text-[13px] font-semibold text-ink">研究档案库</span>
            <span className="ml-1.5 font-mono text-xs text-muted">({archives.length})</span>
          </div>
          <button type="button" className="btn btn-sm btn-primary" onClick={() => setInitModalOpen(true)}>
            <FilePlus2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>新建</span>
          </button>
        </div>

        <div className="border-b border-hairline-soft p-2.5">
          <input
            className="input w-full"
            type="search"
            aria-label="筛选已建档标的"
            placeholder="搜索代码、中文名或市场…"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
        </div>

        <div className="max-h-[72dvh] flex-1 space-y-3.5 overflow-y-auto p-2">
          {filteredArchives.length === 0 ? (
            <div className="p-6 text-center text-xs text-muted">暂无匹配档案，点击右上角新建</div>
          ) : (
            <>
              {holdingArchives.length > 0 ? (
                <div>
                  <div className="section-title mb-1 px-2">在途持仓 ({holdingArchives.length})</div>
                  <ul className="m-0 flex list-none flex-col gap-0.5 p-0">{holdingArchives.map(renderArchiveRow)}</ul>
                </div>
              ) : null}

              {otherArchives.length > 0 ? (
                <div>
                  <div className="section-title mb-1 px-2">观察与归档 ({otherArchives.length})</div>
                  <ul className="m-0 flex list-none flex-col gap-0.5 p-0">{otherArchives.map(renderArchiveRow)}</ul>
                </div>
              ) : null}
            </>
          )}
        </div>
      </aside>

      {/* Center Column: Memo Reader / Editor, Evidence Table, Wide Hard-Data or Trigger Builder */}
      <div className={`flex min-w-0 flex-col gap-3.5 ${showCompanion ? "xl:col-span-6" : "xl:col-span-9"}`}>
        <section className="card flex flex-col gap-3 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1 space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <SymbolLabel
                  symbol={detail?.symbol || activeSym}
                  name={detail?.state?.instrument?.name}
                  size="lg"
                />
                {detail ? (
                  <StatusBadge
                    tone={
                      detail.state.status === "holding"
                        ? "good"
                        : detail.state.status === "watchlist"
                          ? "info"
                          : "neutral"
                    }
                    label={statusBadgeLabel(detail.state.status)}
                  />
                ) : null}
                {detail ? <DirectionBadge direction={detail.state.direction_bias} /> : null}
                {detail ? (
                  <span className="rounded border border-hairline-soft bg-sunken px-2 py-0.5 font-mono text-[11px] text-muted">
                    {detail.market} · v{detail.state.revision}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!activeSym || isDeepRunning}
                onClick={() => setDeepDialogOpen(true)}
                title="调用后台 AI 投研引擎完成第三层深度研究、红蓝对抗与第四层缠论执行框架并写回档案"
              >
                {isDeepRunning ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Bot className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                <span>{isDeepRunning ? "AI 研究中…" : "AI 深度研究"}</span>
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={checkBusy}
                onClick={() => void handleRunInPlaceRefresh()}
                title="就地运行 research_data.py 刷新白名单硬数据并自动核验触发器"
              >
                <Sparkles className={`h-3.5 w-3.5 text-accent ${checkBusy ? "animate-pulse" : ""}`} aria-hidden="true" />
                <span>{checkBusy ? "核验中…" : "刷新硬数据与触发器"}</span>
              </button>
              <button type="button" className="btn btn-sm" onClick={() => onJumpWarRoom(detail?.symbol || activeSym)}>
                <Crosshair className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                <span>战情室</span>
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm px-2"
                title="重新读取本地档案文件"
                onClick={() => {
                  loadedKeyRef.current = "";
                  void loadDetail(activeSym, effectiveMarket);
                }}
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loadingDetail ? "animate-spin" : ""}`} aria-hidden="true" />
              </button>
            </div>
          </div>

          {checkFeedback ? (
            <div className="rounded border border-accent/30 bg-accent/8 px-3 py-1.5 text-xs text-ink">
              {checkFeedback}
            </div>
          ) : null}

          {detail?.state?.thesis_summary ? (
            <div className="callout-editorial text-[12.5px] leading-relaxed text-ink-2">
              <strong className="font-semibold text-ink">核心预期差：</strong>
              {detail.state.thesis_summary}
            </div>
          ) : null}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2.5">
            <div className="segmented flex-wrap" role="tablist" aria-label="研究档案视图切换">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "thesis"}
                className="segmented-btn"
                onClick={() => setTab("thesis")}
              >
                <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                <span>研报正文</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "evidence"}
                className="segmented-btn"
                onClick={() => setTab("evidence")}
              >
                <ListChecks className="h-3.5 w-3.5" aria-hidden="true" />
                <span>L1~L4 证据 ({detail?.evidence_count ?? 0})</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "harddata"}
                className="segmented-btn"
                onClick={() => setTab("harddata")}
              >
                <Database className="h-3.5 w-3.5" aria-hidden="true" />
                <span>实时硬数据</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "triggers"}
                className="segmented-btn"
                onClick={() => setTab("triggers")}
              >
                <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                <span>触发器与日历 ({(detail?.state?.triggers?.length ?? 0) + (detail?.state?.events?.length ?? 0)})</span>
              </button>
            </div>

            {detail ? (
              <div className="flex items-center gap-1.5">
                {tab === "thesis" && !editingThesis ? (
                  <button type="button" className="btn btn-sm" onClick={handleStartEditThesis}>
                    <Edit3 className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                    <span>编辑正文</span>
                  </button>
                ) : null}
                <button type="button" className="btn btn-sm btn-primary" onClick={() => setEvidenceModalOpen(true)}>
                  <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>记证据</span>
                </button>
              </div>
            ) : null}
          </div>
        </section>

        {err ? (
          <div role="alert" className="card border-critical/50 bg-critical/10 px-4 py-3 text-xs text-ink">
            {err}
          </div>
        ) : null}

        {activeDeepRun ? (
          <DeepResearchProgressCard
            run={activeDeepRun}
            cancelling={Boolean(cancellingTasks[activeDeepRun.taskId])}
            onCancel={() => void handleCancelDeepResearch(activeDeepRun)}
            onDismiss={() => {
              const targetKey = makeArchiveKey(activeDeepRun.market, activeDeepRun.symbol);
              setDeepRuns((prev) => {
                const next = { ...prev };
                delete next[targetKey];
                return next;
              });
            }}
          />
        ) : null}

        {isBlankTemplate && !activeDeepRun && tab === "thesis" ? (
          <section className="card flex flex-wrap items-center justify-between gap-3 border-accent/40 bg-accent/8 px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-ink">
                该档案尚未完成 AI 深度研究（当前为空白研究模板）
              </div>
              <p className="m-0 mt-0.5 text-[11.5px] leading-relaxed text-muted">
                点击右侧一键启动，后台将自动拉取白名单硬数据与日线/30m 缠论，执行联网验真与红蓝对抗，并将完整研报、L1~L4 证据、撤离红线与事件日历写回此档案。
              </p>
            </div>
            <button
              type="button"
              className="btn btn-sm btn-primary shrink-0"
              onClick={() => setDeepDialogOpen(true)}
            >
              <Bot className="h-3.5 w-3.5" aria-hidden="true" />
              <span>立即 AI 深度研究</span>
            </button>
          </section>
        ) : null}

        {notFoundSym && tab !== "harddata" ? (
          <div className="card flex flex-wrap items-center justify-between gap-3 p-5">
            <div>
              <div className="text-sm font-semibold text-ink">
                {formatSymbolWithCode(notFoundSym)} 尚未建立本地研究档案
              </div>
              <p className="m-0 mt-1 text-xs text-muted">
                可一键初始化第三层/第四层专用研究模板，或切到「实时硬数据」直接查看白名单数据。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className="btn" onClick={() => setTab("harddata")}>
                先看实时硬数据
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setInitModalOpen(true)}>
                建立 {notFoundSym} 档案
              </button>
            </div>
          </div>
        ) : null}

        {/* TAB 1: Warm-Paper Thesis Markdown Reader / Safe Body Editor + Append Log */}
        {tab === "thesis" && detail ? (
          <div className="flex flex-col gap-3.5">
            {editingThesis ? (
              <form onSubmit={handleSaveThesis} className="card flex flex-col gap-3 p-4">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-ink">
                    在线编辑研报主体（底部「增量修订日志」受后端锁保护，保存后自动追加审计记录）
                  </span>
                  <span className="font-mono text-[11px] text-muted">
                    基准版本 v{editSession?.baseRevision ?? detail.state.revision}
                  </span>
                </div>
                <textarea
                  className="input min-h-[420px] font-mono text-xs leading-relaxed"
                  value={draftThesisBody}
                  onChange={(e) => setDraftThesisBody(e.target.value)}
                  required
                />
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <input
                    className="input flex-1"
                    placeholder="本次修订说明（可选，将自动记入底部增量修订日志）"
                    value={editNote}
                    onChange={(e) => setEditNote(e.target.value)}
                  />
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={savingThesis}
                      onClick={() => setEditSession(null)}
                    >
                      取消
                    </button>
                    <button type="submit" className="btn btn-sm btn-primary" disabled={savingThesis}>
                      <Save className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{savingThesis ? "正在保存…" : "保存并升级版本"}</span>
                    </button>
                  </div>
                </div>
              </form>
            ) : (
              <article className="card bg-paper px-6 py-6 md:px-8 md:py-7">
                <div className="prose-desk mx-auto">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {cleanThesisDisplayMarkdown(detail.thesis_md) || "_暂无正文_"}
                  </Markdown>
                </div>
              </article>
            )}

            <form onSubmit={handleAppendLog} className="card flex flex-col gap-2.5 p-4">
              <label className="field">
                <span className="font-semibold text-ink">追加增量修订记录（Append-Only → thesis.md）</span>
                <textarea
                  className="input"
                  rows={2}
                  placeholder="记录最新财报核验、库存/基差边际变化或红蓝对抗裁决摘要…"
                  value={logEntry}
                  onChange={(e) => setLogEntry(e.target.value)}
                />
              </label>
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted">按日期永久追加，完整保留历史判断轨迹</span>
                <button type="submit" className="btn btn-sm btn-primary" disabled={logBusy || !logEntry.trim()}>
                  {logBusy ? "正在追加…" : "追加修订记录"}
                </button>
              </div>
            </form>
          </div>
        ) : null}

        {/* TAB 2: L1~L4 Evidence Library */}
        {tab === "evidence" && detail ? (
          <section className="card flex flex-col gap-3.5 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1">
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
                      <td data-label="来源定位与说明">核心论据须有 L1 或 L2 一手数据支撑</td>
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
                                className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
                              >
                                <span>原始出处</span>
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

        {/* TAB 4: Visual Trigger & Catalyst Calendar Manager */}
        {tab === "triggers" && detail ? (
          <div className="flex flex-col gap-4">
            {/* Section A: Armed Exit Triggers */}
            <section className="card flex flex-col gap-3 p-4">
              <div className="flex items-center justify-between">
                <span className="section-title">撤离红线触发器配置（同步每日哨兵巡检）</span>
                <span className="font-mono text-[11px] text-muted">共 {detail.state.triggers?.length ?? 0} 条</span>
              </div>

              <form onSubmit={handleAddTrigger} className="grid grid-cols-1 items-end gap-2.5 sm:grid-cols-2 lg:grid-cols-12">
                <label className="field lg:col-span-2">
                  <span>监控维度</span>
                  <select className="input" value={trigKind} onChange={(e) => setTrigKind(e.target.value)}>
                    {TRIGGER_KIND_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field lg:col-span-3">
                  <span>指标键名 (metric) *</span>
                  <input
                    className="input font-mono"
                    required
                    placeholder="如 us10y_yield / vix / close_30m"
                    value={trigMetric}
                    onChange={(e) => setTrigMetric(e.target.value)}
                  />
                </label>
                <label className="field lg:col-span-1">
                  <span>条件</span>
                  <select className="input font-mono" value={trigOp} onChange={(e) => setTrigOp(e.target.value)}>
                    <option value="<">&lt;</option>
                    <option value="<=">&lt;=</option>
                    <option value=">">&gt;</option>
                    <option value=">=">&gt;=</option>
                    <option value="==">==</option>
                    <option value="contains">包含</option>
                  </select>
                </label>
                <label className="field lg:col-span-2">
                  <span>阈值 *</span>
                  <input
                    className="input font-mono"
                    required
                    placeholder="如 4.6"
                    value={trigThreshold}
                    onChange={(e) => setTrigThreshold(e.target.value)}
                  />
                </label>
                <label className="field lg:col-span-2">
                  <span>触发动作</span>
                  <select className="input" value={trigAction} onChange={(e) => setTrigAction(e.target.value)}>
                    <option value="exit_review">果断撤离重审</option>
                    <option value="trim_or_exit">减仓或撤离</option>
                    <option value="alert">指标松动预警</option>
                  </select>
                </label>
                <div className="lg:col-span-2">
                  <button type="submit" className="btn btn-primary w-full" disabled={trigBusy}>
                    <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>添加触发器</span>
                  </button>
                </div>
              </form>

              <div tabIndex={0} role="region" aria-label="档案撤离触发器配置表" className="table-wrapper">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">维度</th>
                      <th scope="col">监控指标</th>
                      <th scope="col">条件阈值</th>
                      <th scope="col">触发动作</th>
                      <th scope="col">状态</th>
                      <th scope="col" className="num">
                        操作
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail.state.triggers ?? []).length === 0 ? (
                      <tr>
                        <th scope="row">未挂载</th>
                        <td data-label="监控指标">—</td>
                        <td data-label="条件阈值">—</td>
                        <td data-label="触发动作">—</td>
                        <td data-label="状态">在上方添加撤离红线后自动纳入每日哨兵</td>
                        <td data-label="操作" className="num">
                          —
                        </td>
                      </tr>
                    ) : (
                      (detail.state.triggers ?? []).map((tr, i) => {
                        const mDisp = formatTriggerMetric(tr.metric || tr.description);
                        const rawKind = String(tr.category || tr.kind || "chanlun");
                        const rawAct = String(tr.action || "alert");
                        return (
                          <tr key={tr.id || i}>
                            <th scope="row">{TRIGGER_KIND_ZH[rawKind] || rawKind}</th>
                            <td data-label="监控指标">
                              <span className="font-medium text-ink">{mDisp.primary}</span>
                              {mDisp.raw ? (
                                <span className="ml-1.5 font-mono text-[11px] text-muted">{mDisp.raw}</span>
                              ) : null}
                            </td>
                            <td data-label="条件阈值" className="tnum font-mono">
                              {tr.operator || ""} {tr.threshold != null ? String(tr.threshold) : ""}
                            </td>
                            <td data-label="触发动作">{TRIGGER_ACTION_ZH[rawAct] || rawAct}</td>
                            <td data-label="状态">
                              <StatusBadge
                                tone={tr.status === "fired" ? "critical" : "good"}
                                label={tr.status === "fired" ? "已触发" : "在线监控"}
                              />
                            </td>
                            <td data-label="操作" className="num">
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm px-1.5 text-muted hover:text-critical"
                                disabled={trigBusy}
                                title="移除该触发器"
                                onClick={() => void handleRemoveTrigger(i)}
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            {/* Section B: Upcoming Catalyst & Rollover Events Calendar */}
            <section className="card flex flex-col gap-3 p-4">
              <div className="flex items-center justify-between">
                <span className="section-title">未来催化与风控事件日历（财报 / 央行议息 / 主力移仓窗口）</span>
                <span className="text-[11px] text-muted">未来 14 天内事件将自动联动至「哨兵总控」</span>
              </div>

              <form onSubmit={handleAddEvent} className="grid grid-cols-1 items-end gap-2.5 sm:grid-cols-12">
                <label className="field sm:col-span-3">
                  <span>事件日期 *</span>
                  <input
                    className="input font-mono"
                    type="date"
                    required
                    value={evDate}
                    onChange={(e) => setEvDate(e.target.value)}
                  />
                </label>
                <label className="field sm:col-span-7">
                  <span>事件与风控说明 *</span>
                  <input
                    className="input"
                    required
                    placeholder="如 FOMC 利率决议 & 点阵图 / Q3 财报发布 / 沪铜近月移仓窗口"
                    value={evTitle}
                    onChange={(e) => setEvTitle(e.target.value)}
                  />
                </label>
                <div className="sm:col-span-2">
                  <button type="submit" className="btn btn-primary w-full" disabled={evBusy}>
                    <CalendarPlus className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>登记事件</span>
                  </button>
                </div>
              </form>

              <div className="divide-y divide-hairline-soft rounded-md border border-hairline">
                {(detail.state.events ?? []).length === 0 ? (
                  <div className="p-4 text-center text-xs text-muted">暂无登记的未来催化或移仓风控节点</div>
                ) : (
                  (detail.state.events ?? []).map((ev, idx) => (
                    <div key={`${ev.date}-${idx}`} className="flex items-center justify-between gap-3 px-3.5 py-2 text-xs">
                      <div className="flex items-baseline gap-3">
                        <span className="font-mono text-muted">{ev.date}</span>
                        <span className="font-medium text-ink">{ev.title}</span>
                      </div>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm px-1.5 text-muted hover:text-critical"
                        disabled={evBusy}
                        title="移除该事件"
                        onClick={() => void handleRemoveEvent(idx)}
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </div>
                  ))
                )}
              </div>
            </section>
          </div>
        ) : null}
      </div>

      {/* Right Column: Persistent Research Companion Inspector */}
      {showCompanion && detail ? (
        <aside className="flex flex-col gap-3.5 xl:sticky xl:top-16 xl:col-span-3" aria-label="研报伴随检视栏">
          <section className="card flex flex-col gap-3 p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title">跟踪状态切换</span>
              {detail.state.updated_at_utc ? (
                <span className="font-mono text-[11px] text-muted">{shortTime(detail.state.updated_at_utc)}</span>
              ) : null}
            </div>

            <div className="flex flex-wrap items-center gap-1" role="group" aria-label="档案跟踪状态切换">
              {(["watchlist", "holding", "rejected"] as const).map((st) => (
                <button
                  key={st}
                  type="button"
                  disabled={statusBusy}
                  className="chip flex-1 justify-center"
                  aria-pressed={detail.state.status === st}
                  onClick={() => void handleStatusSwitch(st)}
                >
                  {st === "holding" ? "持有中" : st === "watchlist" ? "观察池" : "已归档"}
                </button>
              ))}
            </div>

            <button
              type="button"
              className="btn w-full justify-between"
              onClick={() => onJumpWarRoom(detail.symbol)}
            >
              <span className="inline-flex items-center gap-1.5">
                <Crosshair className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
                <span>跳转缠论战情室算仓</span>
              </span>
              <span className="text-xs text-muted">↗</span>
            </button>
          </section>

          <section className="card flex flex-col gap-2.5 p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title">撤离红线触发器 ({detail.state.triggers?.length ?? 0})</span>
              <button
                type="button"
                className="cursor-pointer border-0 bg-transparent p-0 text-[11px] font-medium text-accent hover:underline"
                onClick={() => setTab("triggers")}
              >
                管理
              </button>
            </div>

            {(detail.state.triggers ?? []).length === 0 ? (
              <div className="text-xs text-muted">暂未配置自动化撤离阈值</div>
            ) : (
              <ul className="m-0 flex list-none flex-col divide-y divide-hairline-soft p-0">
                {(detail.state.triggers ?? []).slice(0, 5).map((tr, i) => {
                  const mDisp = formatTriggerMetric(tr.metric || tr.description || tr.category);
                  return (
                    <li key={tr.id || i} className="flex items-start justify-between gap-2 py-2 first:pt-0 last:pb-0">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-medium text-ink">{mDisp.primary}</div>
                        <div className="tnum mt-0.5 font-mono text-[11px] text-muted">
                          {mDisp.raw ? `${mDisp.raw} ` : ""}
                          {tr.operator || ""} {tr.threshold != null ? String(tr.threshold) : "—"}
                        </div>
                      </div>
                      <StatusBadge
                        tone={tr.status === "fired" ? "critical" : "good"}
                        label={tr.status === "fired" ? "已触发" : "在线"}
                      />
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="card flex flex-col gap-2.5 p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="section-title">未来催化与风控节点 ({detail.state.events?.length ?? 0})</span>
              <button
                type="button"
                className="cursor-pointer border-0 bg-transparent p-0 text-[11px] font-medium text-accent hover:underline"
                onClick={() => setTab("triggers")}
              >
                登记
              </button>
            </div>
            {(detail.state.events ?? []).length === 0 ? (
              <div className="text-xs text-muted">暂无登记的财报 / 议息 / 移仓事件</div>
            ) : (
              <ul className="m-0 flex list-none flex-col divide-y divide-hairline-soft p-0">
                {(detail.state.events ?? []).slice(0, 5).map((ev, idx) => (
                  <li key={idx} className="py-2 text-xs first:pt-0 last:pb-0">
                    <div className="font-medium text-ink">{ev.title}</div>
                    <div className="tnum mt-0.5 font-mono text-[11px] text-muted">{ev.date}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {tab === "thesis" ? (
            <section className="card flex flex-col gap-2.5 p-3.5">
              <div className="flex items-center justify-between gap-2">
                <span className="section-title">最新 L1~L4 证据 ({detail.evidence_count})</span>
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0 text-[11px] font-medium text-accent hover:underline"
                  onClick={() => setTab("evidence")}
                >
                  全部
                </button>
              </div>
              {(detail.recent_evidence ?? []).length === 0 ? (
                <div className="text-xs text-muted">尚未录入分级事实证据</div>
              ) : (
                <div className="divide-y divide-hairline-soft">
                  {(detail.recent_evidence ?? []).slice(0, 3).map((ev, idx) => (
                    <div key={ev.id || idx} className="py-2 text-xs first:pt-0 last:pb-0">
                      <div className="flex items-center justify-between gap-2">
                        <StatusBadge
                          tone={ev.level === "L1" ? "good" : ev.level === "L2" ? "info" : "warning"}
                          label={ev.level || "L3"}
                        />
                        <span className="tnum truncate font-mono text-[11px] text-muted">
                          {ev.value !== undefined && ev.value !== null ? `${ev.value} ${ev.unit || ""}` : ev.period || ""}
                        </span>
                      </div>
                      <div className="mt-1 font-medium text-ink">{ev.metric || ev.kind || "事实记录"}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ) : null}
        </aside>
      ) : null}

      <InitArchiveDialog
        open={initModalOpen}
        defaultSymbol={notFoundSym || activeSym}
        defaultMarket={effectiveMarket}
        canAutoResearch={Boolean(enginesInfo?.any_available ?? true)}
        onClose={() => setInitModalOpen(false)}
        onCreated={(newSym, createdMarket, autoResearch) => {
          loadedKeyRef.current = "";
          onArchiveMutated?.();
          void loadList().then((list) => {
            const matched = list.find(
              (x) =>
                x.symbol.toUpperCase() === newSym.toUpperCase() &&
                (!createdMarket || x.market.toUpperCase() === createdMarket.toUpperCase()),
            );
            const resolvedMkt = createdMarket ?? matched?.market ?? null;
            selectArchiveTuple(resolvedMkt, newSym);
            void loadDetail(newSym, resolvedMkt);
            if (autoResearch) {
              void handleLaunchDeepResearch({
                symbol: newSym,
                market: resolvedMkt,
                mode: "standard",
                engine: enginesInfo?.default_engine ?? "claude",
                focus_prompt: "",
              }).catch((ex) => setErr(errorMessage(ex)));
            }
          });
        }}
      />

      <DeepResearchDialog
        open={deepDialogOpen}
        symbol={detail?.symbol || activeSym}
        name={detail?.state?.instrument?.name}
        market={effectiveMarket}
        isHolding={detail?.state?.status === "holding"}
        enginesInfo={enginesInfo}
        onClose={() => setDeepDialogOpen(false)}
        onLaunch={async (opts) => {
          await handleLaunchDeepResearch(opts);
        }}
      />

      <AppendEvidenceDialog
        open={evidenceModalOpen}
        symbol={detail?.symbol || activeSym}
        market={effectiveMarket}
        captureToken={() => selectionTokenRef.current}
        onClose={() => setEvidenceModalOpen(false)}
        onAppended={(targetSym, targetMkt, submittedToken) => {
          loadedKeyRef.current = "";
          onArchiveMutated?.();
          if (submittedToken === selectionTokenRef.current) {
            void loadDetail(targetSym, targetMkt, undefined, submittedToken);
          }
          void loadList();
        }}
      />
    </div>
  );
}

function InitArchiveDialog({
  open,
  defaultSymbol,
  defaultMarket,
  canAutoResearch = true,
  onClose,
  onCreated,
}: {
  open: boolean;
  defaultSymbol: string;
  defaultMarket?: string | null;
  canAutoResearch?: boolean;
  onClose: () => void;
  onCreated: (symbol: string, market: string | null, autoResearch: boolean) => void;
}) {
  const [sym, setSym] = useState(defaultSymbol);
  const [market, setMarket] = useState(defaultMarket || "");
  const [name, setName] = useState("");
  const [subtype, setSubtype] = useState("");
  const [primaryDriver, setPrimaryDriver] = useState("");
  const [secondaryDriver, setSecondaryDriver] = useState("");
  const [summary, setSummary] = useState("");
  const [autoResearch, setAutoResearch] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSym(defaultSymbol);
    setMarket("");
    setName(getSymbolName(defaultSymbol) || "");
    setSubtype("");
    setPrimaryDriver("");
    setSecondaryDriver("");
    setSummary("");
    setAutoResearch(canAutoResearch);
    setErr(null);
  }, [open, defaultSymbol, canAutoResearch]);

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
      onCreated(sym.trim().toUpperCase(), mkt ?? null, autoResearch && canAutoResearch);
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
      subtitle="自动匹配股票 1~3 年预期差 / 商品供需平衡表 / 外汇利差模板"
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <label className="field">
            <span>标的代码 *</span>
            <input
              className="input font-mono"
              required
              autoFocus
              placeholder="如 PWR / CU0"
              value={sym}
              onChange={(e) => setSym(e.target.value)}
            />
          </label>
          <label className="field">
            <span>市场分区（可选）</span>
            <input
              className="input font-mono"
              placeholder="自动识别"
              value={market}
              onChange={(e) => setMarket(e.target.value)}
            />
          </label>
          <label className="field">
            <span>中文简称</span>
            <input
              className="input"
              placeholder="如 沪铜 / 广达服务"
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
              placeholder="如 北美电网设备瓶颈"
              value={primaryDriver}
              onChange={(e) => setPrimaryDriver(e.target.value)}
            />
          </label>
          <label className="field">
            <span>第一层次驱动维度（双维共振）</span>
            <input
              className="input"
              placeholder="如 财政基建与算力资本开支"
              value={secondaryDriver}
              onChange={(e) => setSecondaryDriver(e.target.value)}
            />
          </label>
        </div>

        <label className="field">
          <span>1Q~1Y 核心预期差摘要</span>
          <textarea
            className="input"
            rows={2}
            placeholder="概括核心多空论点与关键证伪锚点…"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
          />
        </label>

        {canAutoResearch ? (
          <label className="flex cursor-pointer items-center gap-2 rounded-md border border-hairline-soft bg-sunken/50 px-3 py-2 text-xs text-ink-2">
            <input
              type="checkbox"
              className="accent-accent"
              checked={autoResearch}
              onChange={(e) => setAutoResearch(e.target.checked)}
            />
            <span>创建档案后立即在后台启动 AI 深度研究（自动填满研报、证据与撤离红线）</span>
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
            {busy ? "正在创建…" : "创建档案"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

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
      title={`追加分级事实证据 · ${formatSymbolWithCode(symbol)}`}
      subtitle="L1(10-K/EIA/CFTC/统计局原表) > L2(管理层指引/行业协会) > L3(研报) > L4(资讯)"
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
              className="input font-mono"
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
              className="input font-mono"
              placeholder="如 2026Q3"
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
            {busy ? "正在写入…" : "确认写入证据库"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
