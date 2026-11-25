import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Clock,
  Compass,
  Crosshair,
  ExternalLink,
  History,
  Loader2,
  RefreshCw,
  Scale,
  ShieldAlert,
  Sparkles,
  Square,
  TrendingDown,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { api, ApiError, errorMessage, parseTaskEvent } from "../../api/client";
import type {
  CompassAvoidSectorItem,
  CompassDirectionCard,
  CompassMacroOverview,
  CompassRunRecord,
  CompassRunSummary,
  CompassScanContext,
  CrossAssetAnchorItem,
  DeepResearchEngine,
  ResearchEnginesResponse,
  TaskEventEnvelope,
  TaskRecord,
  UniversePool,
} from "../../api/types";
import { ModalDialog } from "../ui/ModalDialog";
import { signedToneClass } from "../ui/StatusBadge";
import { SymbolLabel } from "../ui/SymbolLabel";
import { fmtPct, shortTime } from "../../lib/format";
import { Layer1LeaderPickerModal } from "./Layer1LeaderPickerModal";

type DirectionFilter = "all" | "long" | "short" | "pair";

interface CompassLogLine {
  ts: string;
  stage?: string;
  message: string;
}

interface ActiveCompassRun {
  taskId: string;
  engine: string;
  effort: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  stage: string;
  done: number;
  total: number;
  startedAtMs: number;
  finishedAtMs?: number;
  logs: CompassLogLine[];
  error?: string;
}

const COMPASS_STAGES = [
  { key: "collect_macro", label: "采集宏观双核与定价锚" },
  { key: "build_brief", label: "封装纯宏观简报(去锚定)" },
  { key: "agent_synthesize", label: "AI 盲推演 7 维板块" },
  { key: "validate_and_persist", label: "校验板块并自动映池" },
];

const FOCUS_SCOPE_GROUPS: {
  group: string;
  items: { id: string; label: string; hint: string }[];
}[] = [
  {
    group: "大宗商品（多空对称）",
    items: [
      { id: "commodities_energy_shipping", label: "能源与航运", hint: "原油/成品油裂解/天然气/航运" },
      { id: "commodities_metals", label: "有色与贵金属", hint: "铜/铝/镍/锡/金/银/铂钯" },
      { id: "commodities_ferrous_chem", label: "黑色与新能源化工", hint: "铁矿/双焦/钢铁/碳酸锂/硅/化工" },
      { id: "commodities_agri", label: "农产油脂与畜牧", hint: "大豆/棕榈油/糖/咖啡/生猪" },
    ],
  },
  {
    group: "宏观汇率与加密（多空对称）",
    items: [
      { id: "fx_rates", label: "外汇与利率周期", hint: "美元/日元/欧元/澳元/加元/人民币" },
      { id: "crypto_liquidity", label: "加密货币与流动性", hint: "BTC/ETH/ETF流/稳定币" },
    ],
  },
  {
    group: "权益市场（美股支持双杀做空 · A 股仅做多）",
    items: [
      { id: "us_equities", label: "美股板块与产业迭代", hint: "科技/电力/军工/医药/矿业（双杀可空）" },
      { id: "a_shares", label: "A 股主线与出海", hint: "红利/周期资源/制造出海（严格仅做多）" },
    ],
  },
];

function assetScopeLabel(scope: string): string {
  switch (scope) {
    case "commodities":
      return "大宗商品";
    case "us_equities":
      return "美股";
    case "a_shares":
      return "A 股";
    case "fx":
      return "外汇 FX";
    case "crypto":
      return "加密货币";
    case "cross_asset":
      return "跨资产";
    default:
      return scope || "跨资产";
  }
}

function stageIndex(stage: string, done: number, status: ActiveCompassRun["status"]): number {
  if (status === "completed") return COMPASS_STAGES.length;
  const idx = COMPASS_STAGES.findIndex((s) => s.key === stage);
  if (idx >= 0) return idx;
  return Math.max(0, Math.min(COMPASS_STAGES.length - 1, done));
}

function formatElapsed(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

function formatHeadlineValue(value: number | null, unit: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (unit === "$/t") return `$${Math.round(value).toLocaleString("en-US")}/t`;
  return `${value.toFixed(unit === "%" ? 2 : 1)}${unit}`;
}

const MONTHLY_MACRO_KEYS = new Set(["PCOPPUSDM", "FEDFUNDS", "M2SL", "CN_PMI"]);
const MACRO_LAG_THRESHOLD_DAYS = 14;

interface MacroAsOfInfo {
  raw: string | null;
  shortLabel: string;
  lagDays: number | null;
  isLagged: boolean;
  isMonthly: boolean;
}

function isValidUtcDate(year: number, month: number, day: number): boolean {
  if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) return false;
  if (month < 1 || month > 12 || day < 1 || day > 31) return false;
  const maxDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return day <= maxDay;
}

function parseMacroAsOf(
  asOf: string | null | undefined,
  key: string,
  refNowMs: number,
): MacroAsOfInfo {
  const raw = (asOf || "").trim();
  const keyUpper = (key || "").trim().toUpperCase();
  const isKnownMonthly = MONTHLY_MACRO_KEYS.has(keyUpper);
  if (!raw) {
    return { raw: null, shortLabel: "", lagDays: null, isLagged: false, isMonthly: isKnownMonthly };
  }

  const refYear = new Date(refNowMs).getUTCFullYear();
  const isoDay = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(raw);
  if (isoDay) {
    const y = Number(isoDay[1]);
    const m = Number(isoDay[2]);
    const d = Number(isoDay[3]);
    if (!isValidUtcDate(y, m, d)) {
      return {
        raw,
        shortLabel: raw.slice(0, 12),
        lagDays: null,
        isLagged: false,
        isMonthly: isKnownMonthly,
      };
    }
    // Known monthly series (e.g. PCOPPUSDM, FEDFUNDS, M2SL, CN_PMI) report month-start dates
    // representing the whole month; unify lag calculation on month-end to match "YYYY年M月份".
    const obsMs = isKnownMonthly ? Date.UTC(y, m, 0) : Date.UTC(y, m - 1, d);
    const lagDays = Number.isFinite(obsMs)
      ? Math.max(0, Math.floor((refNowMs - obsMs) / 86_400_000))
      : null;
    const mm = String(m).padStart(2, "0");
    const dd = String(d).padStart(2, "0");
    const shortLabel = isKnownMonthly
      ? y === refYear
        ? `${mm}月`
        : `${y}-${mm}`
      : y === refYear
        ? `${mm}-${dd}`
        : `${y}-${mm}-${dd}`;
    return {
      raw,
      shortLabel,
      lagDays,
      isLagged: lagDays !== null && lagDays > MACRO_LAG_THRESHOLD_DAYS,
      isMonthly: isKnownMonthly,
    };
  }

  const ym = /^(\d{4})[年\-/](\d{1,2})(?:月份?)?/.exec(raw);
  if (ym) {
    const y = Number(ym[1]);
    const m = Number(ym[2]);
    if (!isValidUtcDate(y, m, 1)) {
      return {
        raw,
        shortLabel: raw.slice(0, 12),
        lagDays: null,
        isLagged: false,
        isMonthly: isKnownMonthly,
      };
    }
    // Month-period observations (e.g. NBS PMI "2026年10月份") represent the whole month; measure lag from month-end.
    const monthEndMs = Date.UTC(y, m, 0);
    const lagDays = Number.isFinite(monthEndMs)
      ? Math.max(0, Math.floor((refNowMs - monthEndMs) / 86_400_000))
      : null;
    const mm = String(m).padStart(2, "0");
    const shortLabel = y === refYear ? `${mm}月` : `${y}-${mm}`;
    return {
      raw,
      shortLabel,
      lagDays,
      isLagged: lagDays !== null && lagDays > MACRO_LAG_THRESHOLD_DAYS,
      isMonthly: true,
    };
  }

  return {
    raw,
    shortLabel: raw.slice(0, 12),
    lagDays: null,
    isLagged: false,
    isMonthly: isKnownMonthly,
  };
}

function regimeTagClasses(tone: string): string {
  switch (tone) {
    case "good":
      return "border-bull/35 bg-bull/10 text-bull";
    case "critical":
      return "border-critical/40 bg-critical/10 text-critical";
    case "warning":
      return "border-warning/40 bg-warning/10 text-warning";
    default:
      return "border-hairline bg-elevated text-ink-2";
  }
}

function directionMeta(direction: CompassDirectionCard["direction"]) {
  if (direction === "short") {
    return {
      label: "做空方向",
      code: "SHORT",
      borderClass: "border-l-bear",
      badgeClass: "border-bear/40 bg-bear/12 text-bear",
      Icon: TrendingDown,
    };
  }
  if (direction === "pair") {
    return {
      label: "多空配对",
      code: "PAIR",
      borderClass: "border-l-warning",
      badgeClass: "border-warning/40 bg-warning/12 text-warning",
      Icon: Scale,
    };
  }
  return {
    label: "做多方向",
    code: "LONG",
    borderClass: "border-l-bull",
    badgeClass: "border-bull/40 bg-bull/12 text-bull",
    Icon: TrendingUp,
  };
}

function safeExternalUrl(raw: string | undefined | null): string | null {
  const s = (raw || "").trim();
  if (!s || s.includes("\\") || s.length > 2000) return null;
  try {
    const parsed = new URL(s);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password) return null;
    if (!parsed.hostname) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function isLegacyHeadwindShortable(item: CompassAvoidSectorItem): boolean {
  if (typeof item.short_proxy_possible === "boolean") {
    return item.short_proxy_possible;
  }
  const text = `${item.sector} ${item.reason}`.toLowerCase();
  return /(美股|美国|ai|算力|芯片|科技|数据中心|纳斯达克|原油|石油|金属|锂|铝|铜|锡|镍|铁矿|黑色|地产|加密|比特币|btc|eth|外汇|汇率|商品|农产品|生猪)/.test(
    text,
  );
}

function buildShortCardFromHeadwind(item: CompassAvoidSectorItem, index: number): CompassDirectionCard {
  const hint =
    item.short_proxy_hint ||
    item.derived_strategy ||
    "可选美股/行业 ETF、商品期货、外汇或加密货币空头/跨资产代理（A 股严禁直接做空）";
  return {
    id: `headwind_short_${index + 1}`,
    rank: index + 1,
    title: `逆风衍生做空：${item.sector}`,
    direction: "short",
    horizon: "1Q~2Q",
    conviction: "medium",
    asset_scope: "cross_asset",
    primary_driver: { dimension: "宏观双核", summary: item.reason || item.sector },
    secondary_driver: { dimension: "商品供需平衡与库存周期", summary: "逆风逻辑延伸合规空头/代理表达" },
    thesis: item.reason || item.sector,
    value_chain_focus: hint,
    key_catalyst: "",
    invalidation: "",
    recommended_universes: [],
    is_new_theme: true,
    suggested_pool_name: `short_headwind_${index + 1}`,
    suggested_pool_description: `逆风衍生做空：${item.sector}`,
    leaders: [],
    evidence: [],
  };
}

export interface Layer1CompassPanelProps {
  universes: Record<string, UniversePool>;
  scanBusy?: boolean;
  onLaunchCompassScan: (opts: {
    symbols: string[];
    compassContext: CompassScanContext;
  }) => Promise<void> | void;
  onUniverseSaved?: () => void;
  onJumpWarRoom: (symbol: string) => void;
  onJumpArchive: (symbol: string) => void;
}

export function Layer1CompassPanel({
  universes,
  scanBusy = false,
  onLaunchCompassScan,
  onUniverseSaved,
  onJumpWarRoom,
  onJumpArchive,
}: Layer1CompassPanelProps) {
  /* Free hard-data macro foundation */
  const [macro, setMacro] = useState<CompassMacroOverview | null>(null);
  const [macroLoading, setMacroLoading] = useState(false);
  const [macroErr, setMacroErr] = useState<string | null>(null);

  /* Persisted Layer 1 synthesis runs */
  const [currentRun, setCurrentRun] = useState<CompassRunRecord | null>(null);
  const [history, setHistory] = useState<CompassRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>("");
  const [loadingRun, setLoadingRun] = useState(false);
  const [dirFilter, setDirFilter] = useState<DirectionFilter>("all");

  /* AI engine & launch modal */
  const [enginesInfo, setEnginesInfo] = useState<ResearchEnginesResponse | null>(null);
  const [launchOpen, setLaunchOpen] = useState(false);
  const [selectedEngine, setSelectedEngine] = useState<DeepResearchEngine>("claude");
  const [effort, setEffort] = useState<"low" | "medium" | "high">("medium");
  const [selectedFocusScopes, setSelectedFocusScopes] = useState<string[]>([]);
  const [focusPrompt, setFocusPrompt] = useState("");
  const [refreshMacroOnRun, setRefreshMacroOnRun] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [launchErr, setLaunchErr] = useState<string | null>(null);
  const [pickerCard, setPickerCard] = useState<CompassDirectionCard | null>(null);

  /* Live SSE task progress & concurrency/unmount guards */
  const [activeRun, setActiveRun] = useState<ActiveCompassRun | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const esRef = useRef<EventSource | null>(null);
  const mountedRef = useRef(true);
  const selectionReqSeqRef = useRef(0);
  const historyAbortRef = useRef<AbortController | null>(null);
  const latestRunIdRef = useRef<string>("");
  const viewingLatestRef = useRef<boolean>(true);

  const loadMacro = useCallback(async (refresh = false, signal?: AbortSignal) => {
    setMacroLoading(true);
    setMacroErr(null);
    try {
      const data = await api.compassMacroOverview(refresh, signal);
      if (!mountedRef.current || signal?.aborted) return;
      setMacro(data);
    } catch (ex) {
      if (!mountedRef.current || (ex instanceof DOMException && ex.name === "AbortError")) return;
      setMacroErr(errorMessage(ex));
    } finally {
      if (mountedRef.current) setMacroLoading(false);
    }
  }, []);

  const attachTaskStream = useCallback((task: TaskRecord) => {
    if (!mountedRef.current) return;
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    const taskId = task.id;
    const params = task.params ?? {};
    const startedMs = task.started_at ? Date.parse(task.started_at) || Date.now() : Date.now();

    setActiveRun({
      taskId,
      engine: String(params.engine || "claude"),
      effort: String(params.effort || "medium"),
      status: (task.status as ActiveCompassRun["status"]) || "running",
      stage: "collect_macro",
      done: typeof task.progress === "object" && task.progress ? task.progress.done : 0,
      total: typeof task.progress === "object" && task.progress && task.progress.total ? task.progress.total : 4,
      startedAtMs: startedMs,
      logs: [],
    });

    const seenSeq = new Set<number>();
    let closed = false;
    let probingError = false;
    const es = new EventSource(api.taskEventsUrl(taskId));
    esRef.current = es;

    const closeStream = () => {
      if (closed) return;
      closed = true;
      es.close();
      if (esRef.current === es) {
        esRef.current = null;
      }
    };

    const syncCompletedRun = (saved?: CompassRunRecord) => {
      const reqAtDone = selectionReqSeqRef.current;
      if (saved && saved.result && Array.isArray(saved.result.directions)) {
        latestRunIdRef.current = saved.run_id;
        if (viewingLatestRef.current && selectionReqSeqRef.current === reqAtDone) {
          setCurrentRun(saved);
          setSelectedRunId(saved.run_id);
        }
      }
      void api
        .compassLatest()
        .then((res) => {
          if (!mountedRef.current) return;
          setHistory(res.history ?? []);
          if (res.latest) {
            latestRunIdRef.current = res.latest.run_id;
            if (viewingLatestRef.current && selectionReqSeqRef.current === reqAtDone) {
              setCurrentRun(res.latest);
              setSelectedRunId(res.latest.run_id);
            }
          }
        })
        .catch(() => undefined);
      void loadMacro(false);
    };

    const applyEvents = (events: TaskEventEnvelope[]) => {
      if (!mountedRef.current) return;
      for (const ev of events) {
        if (typeof ev.seq === "number") {
          if (seenSeq.has(ev.seq)) continue;
          seenSeq.add(ev.seq);
        }
        const d = ev.data ?? {};
        const t = ev.type;
        if (t === "step" || t === "log") {
          const msg = String(d.message || ev.message || "").trim();
          const stg = d.stage ? String(d.stage) : undefined;
          setActiveRun((prev) => {
            if (!prev || prev.taskId !== taskId) return prev;
            const nextLogs = msg
              ? [...prev.logs, { ts: ev.ts || new Date().toISOString(), stage: stg, message: msg }].slice(-40)
              : prev.logs;
            return {
              ...prev,
              status: prev.status === "queued" ? "running" : prev.status,
              stage: stg || prev.stage,
              logs: nextLogs,
            };
          });
        } else if (t === "progress") {
          setActiveRun((prev) =>
            prev && prev.taskId === taskId
              ? {
                  ...prev,
                  done: typeof d.done === "number" ? Math.max(prev.done, d.done) : prev.done,
                  total: typeof d.total === "number" && d.total > 0 ? d.total : prev.total,
                }
              : prev,
          );
        } else if (t === "done") {
          const saved = (d.result ?? ev.result) as CompassRunRecord | undefined;
          setActiveRun((prev) =>
            prev && prev.taskId === taskId
              ? { ...prev, status: "completed", done: prev.total, finishedAtMs: Date.now() }
              : prev,
          );
          closeStream();
          syncCompletedRun(saved);
        } else if (t === "error" || t === "cancelled") {
          const nextStatus = t === "cancelled" ? "cancelled" : "failed";
          const errMsg = String(d.message || ev.message || (t === "cancelled" ? "已终止推演" : "推演失败"));
          setActiveRun((prev) =>
            prev && prev.taskId === taskId
              ? { ...prev, status: nextStatus, error: errMsg, finishedAtMs: Date.now() }
              : prev,
          );
          closeStream();
        }
      }
    };

    const handler = (evType: string) => (msg: MessageEvent) => {
      if (closed || !mountedRef.current || typeof msg.data !== "string") return;
      const parsed = parseTaskEvent(msg.data, evType);
      if (parsed) applyEvents([parsed]);
    };

    for (const evtName of ["step", "log", "progress", "done", "error", "cancelled", "message"]) {
      es.addEventListener(evtName, handler(evtName) as EventListener);
    }

    /* Native transport error recovery (e.g. backend restart -> SSE 404 without data payload) */
    es.onerror = () => {
      if (closed || probingError || !mountedRef.current) return;
      probingError = true;
      void api
        .task(taskId)
        .then((detail) => {
          if (!mountedRef.current || closed) return;
          if (detail.status === "completed") {
            setActiveRun((prev) =>
              prev && prev.taskId === taskId
                ? { ...prev, status: "completed", done: prev.total, finishedAtMs: Date.now() }
                : prev,
            );
            closeStream();
            syncCompletedRun(detail.result as CompassRunRecord | undefined);
          } else if (detail.status === "failed" || detail.status === "cancelled") {
            const errMsg = detail.error || (detail.status === "cancelled" ? "已终止推演" : "推演失败");
            setActiveRun((prev) =>
              prev && prev.taskId === taskId
                ? {
                    ...prev,
                    status: detail.status === "cancelled" ? "cancelled" : "failed",
                    error: errMsg,
                    finishedAtMs: Date.now(),
                  }
                : prev,
            );
            closeStream();
          }
        })
        .catch(async (ex) => {
          if (!mountedRef.current || closed) return;
          const isMissing = ex instanceof ApiError && ex.status === 404;
          if (isMissing || es.readyState === EventSource.CLOSED) {
            closeStream();
            try {
              const latestRes = await api.compassLatest();
              if (!mountedRef.current) return;
              setHistory(latestRes.history ?? []);
              const matched = latestRes.latest && latestRes.latest.run_id === taskId ? latestRes.latest : null;
              if (matched) {
                setActiveRun((prev) =>
                  prev && prev.taskId === taskId
                    ? { ...prev, status: "completed", done: prev.total, finishedAtMs: Date.now() }
                    : prev,
                );
                syncCompletedRun(matched);
                return;
              }
            } catch {
              /* fall through to disconnect state */
            }
            setActiveRun((prev) =>
              prev && prev.taskId === taskId
                ? {
                    ...prev,
                    status: "failed",
                    error: isMissing ? "推演任务连接已失联（服务可能已重启），可重新运行" : "SSE 连接中断，请重试",
                    finishedAtMs: Date.now(),
                  }
                : prev,
            );
          }
        })
        .finally(() => {
          probingError = false;
        });
    };
  }, [loadMacro]);

  const loadLatest = useCallback(async (signal?: AbortSignal) => {
    const reqSeq = ++selectionReqSeqRef.current;
    try {
      const res = await api.compassLatest(signal);
      if (!mountedRef.current || signal?.aborted) return;
      setHistory(res.history ?? []);
      if (res.latest) {
        latestRunIdRef.current = res.latest.run_id;
        if (selectionReqSeqRef.current === reqSeq && viewingLatestRef.current) {
          setCurrentRun(res.latest);
          setSelectedRunId(res.latest.run_id);
        }
      }
      if (res.active_task && (res.active_task.status === "queued" || res.active_task.status === "running")) {
        attachTaskStream(res.active_task);
      }
    } catch {
      /* ignore initial fetch error */
    }
  }, [attachTaskStream]);

  useEffect(() => {
    mountedRef.current = true;
    const ctrl = new AbortController();
    void loadMacro(false, ctrl.signal);
    void loadLatest(ctrl.signal);
    void api
      .researchEngines(ctrl.signal)
      .then((info) => {
        if (!mountedRef.current || ctrl.signal.aborted) return;
        setEnginesInfo(info);
        if (info.default_engine) setSelectedEngine(info.default_engine);
      })
      .catch(() => undefined);
    return () => {
      mountedRef.current = false;
      ctrl.abort();
      if (historyAbortRef.current) {
        historyAbortRef.current.abort();
        historyAbortRef.current = null;
      }
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, [loadMacro, loadLatest]);

  const isRunActive = activeRun?.status === "queued" || activeRun?.status === "running";
  useEffect(() => {
    if (!isRunActive) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [isRunActive]);

  const handleSelectHistory = async (runId: string) => {
    if (!runId) return;
    if (historyAbortRef.current) {
      historyAbortRef.current.abort();
    }
    const ctrl = new AbortController();
    historyAbortRef.current = ctrl;
    const reqSeq = ++selectionReqSeqRef.current;
    viewingLatestRef.current = runId === latestRunIdRef.current;
    setSelectedRunId(runId);
    setLoadingRun(true);
    try {
      const detail = await api.compassHistoryDetail(runId, ctrl.signal);
      if (!mountedRef.current || ctrl.signal.aborted || selectionReqSeqRef.current !== reqSeq) return;
      setCurrentRun(detail);
      setSelectedRunId(detail.run_id);
      setDirFilter("all");
    } catch {
      /* ignore aborted/stale history fetches */
    } finally {
      if (mountedRef.current && selectionReqSeqRef.current === reqSeq) {
        setLoadingRun(false);
      }
    }
  };

  const toggleFocusScope = (scopeId: string) => {
    setSelectedFocusScopes((prev) =>
      prev.includes(scopeId) ? prev.filter((x) => x !== scopeId) : [...prev, scopeId],
    );
  };

  const handleStartCompass = async (e: FormEvent) => {
    e.preventDefault();
    if (launching) return;
    setLaunching(true);
    setLaunchErr(null);
    try {
      const task = await api.startMacroCompass({
        engine: selectedEngine,
        effort,
        focus_scopes: selectedFocusScopes,
        focus_prompt: focusPrompt.trim(),
        refresh_macro: refreshMacroOnRun,
      });
      if (!mountedRef.current) return;
      viewingLatestRef.current = true;
      setLaunchOpen(false);
      attachTaskStream(task);
    } catch (ex) {
      if (!mountedRef.current) return;
      setLaunchErr(errorMessage(ex));
    } finally {
      if (mountedRef.current) setLaunching(false);
    }
  };

  const handleCancelCompass = async () => {
    if (!activeRun || cancelling) return;
    setCancelling(true);
    try {
      await api.cancelTask(activeRun.taskId);
    } catch {
      /* ignore */
    } finally {
      setCancelling(false);
    }
  };

  const anchorGroups = useMemo(() => {
    const items = macro?.anchors?.items ?? [];
    const map = new Map<string, { label: string; rows: CrossAssetAnchorItem[] }>();
    for (const item of items) {
      const g = map.get(item.group) ?? { label: item.group_label, rows: [] };
      g.rows.push(item);
      map.set(item.group, g);
    }
    return Array.from(map.values());
  }, [macro]);

  const payload = currentRun?.result ?? null;
  const directions = payload?.directions ?? [];

  const directionCounts = useMemo(() => {
    let long = 0;
    let short = 0;
    let pair = 0;
    for (const d of directions) {
      if (d.direction === "short") short += 1;
      else if (d.direction === "pair") pair += 1;
      else long += 1;
    }
    return { all: directions.length, long, short, pair };
  }, [directions]);

  const filteredDirections = useMemo(() => {
    if (dirFilter === "all") return directions;
    return directions.filter((d) => d.direction === dirFilter);
  }, [directions, dirFilter]);

  return (
    <section className="flex flex-col gap-3.5" aria-label="Layer 1 自上而下战略罗盘">
      {/* 1. Free Hard-Data Macro Dual-Core Strip */}
      <div className="card flex flex-col gap-2.5 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink">
              <Compass className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
              <span>中美宏观双核与市场定价锚</span>
            </span>
            {(macro?.regime_tags ?? []).map((tag) => (
              <span
                key={tag.label}
                className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-medium ${regimeTagClasses(tag.tone)}`}
              >
                {tag.label}
              </span>
            ))}
            {macro?.stale_sources && Object.keys(macro.stale_sources).length > 0 ? (
              <span
                className="inline-flex items-center gap-1 rounded border border-warning/35 bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning"
                title={Object.entries(macro.stale_sources)
                  .map(([k, v]) => `${k}: ${v.error || "stale"}${v.last_good_at_utc ? ` (沿用 ${shortTime(v.last_good_at_utc)})` : ""}`)
                  .join(" | ")}
              >
                <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                <span>{Object.keys(macro.stale_sources).length} 源降级/陈旧</span>
              </span>
            ) : null}
          </div>
          <div className="flex items-center gap-2 text-[11px] text-muted">
            {macro?.fetched_at_utc ? (
              <span className="font-mono">更新 {shortTime(macro.fetched_at_utc)}</span>
            ) : null}
            <button
              type="button"
              className="btn btn-sm"
              disabled={macroLoading}
              onClick={() => void loadMacro(true)}
              title="重新拉取 FRED / 国家统计局 / 25 个跨资产与国内期货定价锚（免费硬数据，零 AI 消耗）"
            >
              <RefreshCw className={`h-3 w-3 ${macroLoading ? "animate-spin" : ""}`} aria-hidden="true" />
              <span>刷新宏观</span>
            </button>
          </div>
        </div>

        {macroErr ? (
          <div className="flex items-center gap-1.5 rounded-md border border-warning/35 bg-warning/8 px-2.5 py-1.5 text-xs text-warning">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>宏观硬数据部分异常：{macroErr}</span>
          </div>
        ) : null}

        {/* Compact Hairline Metric Strip */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
          {(macro?.headline_metrics ?? []).map((m) => {
            const fetchedMs = macro?.fetched_at_utc ? Date.parse(macro.fetched_at_utc) : NaN;
            const refMs = Number.isFinite(fetchedMs) ? Math.max(Date.now(), fetchedMs) : Date.now();
            const asOfInfo = parseMacroAsOf(m.as_of, m.key, refMs);
            const lagNote =
              asOfInfo.isLagged && asOfInfo.lagDays !== null
                ? ` · 已滞后约 ${asOfInfo.lagDays} 天（非当日实时价）`
                : "";
            return (
              <div
                key={m.key}
                className={`flex flex-col justify-between gap-1 rounded-md border px-2.5 py-1.5 ${
                  asOfInfo.isLagged
                    ? "border-warning/35 bg-warning/[0.06]"
                    : "border-hairline-soft bg-sunken/70"
                }`}
                title={`${m.label} · 来源 ${m.source || "—"}${m.as_of ? ` · 截至 ${m.as_of}` : ""}${lagNote}`}
              >
                <span className="truncate text-[10px] text-muted">{m.label}</span>
                <div className="flex items-baseline justify-between gap-1 font-mono tabular-nums">
                  <span className="text-[13px] font-semibold text-ink">
                    {formatHeadlineValue(m.value, m.unit)}
                  </span>
                  {m.delta !== null && m.delta !== undefined ? (
                    <span className={`text-[10px] ${signedToneClass(m.delta)}`}>
                      {m.delta > 0 ? "+" : ""}
                      {m.delta}
                      {m.delta_unit || ""}
                    </span>
                  ) : null}
                </div>
                <div className="flex min-w-0 items-center">
                  {asOfInfo.isLagged ? (
                    <span className="inline-flex max-w-full items-center gap-1 rounded border border-warning/35 bg-warning/12 px-1 py-0.2 font-mono text-[9.5px] font-medium leading-tight text-warning">
                      <Clock className="h-2.5 w-2.5 shrink-0" aria-hidden="true" />
                      <span className="truncate">
                        {asOfInfo.isMonthly ? "月度滞后" : "滞后"} · {asOfInfo.shortLabel}
                      </span>
                    </span>
                  ) : asOfInfo.shortLabel ? (
                    <span className="truncate font-mono text-[9.5px] leading-tight text-muted">
                      截至 {asOfInfo.shortLabel}
                    </span>
                  ) : (
                    <span className="font-mono text-[9.5px] leading-tight text-muted">—</span>
                  )}
                </div>
              </div>
            );
          })}
          {!macro && macroLoading ? (
            <div className="col-span-full flex items-center gap-2 py-2 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" aria-hidden="true" />
              <span>正在拉取 FRED 美债/通胀、中国 PMI 与 25 个跨资产/国内期货定价锚…</span>
            </div>
          ) : null}
        </div>

        {/* Compact Tape-to-Macro Extreme Movers Radar Strip */}
        {macro?.tape_movers &&
        (macro.tape_movers.top_losers.length > 0 || macro.tape_movers.top_gainers.length > 0) ? (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-hairline-soft bg-sunken/40 px-2.5 py-1.5 text-[11px]">
            <span className="font-semibold text-ink-2">
              盘面极端雷达 · {macro.tape_movers.regime_breadth.breadth_label}
            </span>
            {macro.tape_movers.top_losers.slice(0, 5).map((mv) => (
              <button
                key={`lose-${mv.symbol}`}
                type="button"
                onClick={() => onJumpWarRoom(mv.symbol)}
                className="inline-flex cursor-pointer items-center gap-1 rounded border border-bear/30 bg-bear/8 px-1.5 py-0.5 font-mono text-[10.5px] text-bear hover:border-bear/60"
                title={`20D ${fmtPct(mv.ret_20d_pct, 1)} / 60D ${fmtPct(mv.ret_60d_pct, 1)} · 点击进入战情室`}
              >
                <span className="font-sans">{mv.name}</span>
                {mv.driver_window ? <span className="text-[9.5px] opacity-75">{mv.driver_window}</span> : null}
                <span>{fmtPct(mv.driver_ret_pct ?? mv.ret_20d_pct ?? mv.ret_60d_pct, 1)}</span>
              </button>
            ))}
            {macro.tape_movers.top_gainers.slice(0, 2).map((mv) => (
              <button
                key={`gain-${mv.symbol}`}
                type="button"
                onClick={() => onJumpWarRoom(mv.symbol)}
                className="inline-flex cursor-pointer items-center gap-1 rounded border border-bull/30 bg-bull/8 px-1.5 py-0.5 font-mono text-[10.5px] text-bull hover:border-bull/60"
                title={`20D ${fmtPct(mv.ret_20d_pct, 1)} / 60D ${fmtPct(mv.ret_60d_pct, 1)} · 点击进入战情室`}
              >
                <span className="font-sans">{mv.name}</span>
                {mv.driver_window ? <span className="text-[9.5px] opacity-75">{mv.driver_window}</span> : null}
                <span>{fmtPct(mv.driver_ret_pct ?? mv.ret_20d_pct ?? mv.ret_60d_pct, 1)}</span>
              </button>
            ))}
          </div>
        ) : null}

        {/* On-demand Native Disclosure: 25 Cross-Asset & CN Futures Pricing Anchors (20D / 60D Relative Strength) */}
        {anchorGroups.length > 0 ? (
          <details className="border-t border-hairline-soft pt-2">
            <summary className="cursor-pointer select-none text-[11px] font-medium text-muted hover:text-ink">
              展开跨资产与国内期货 20D / 60D 相对强弱定价锚（{macro?.anchors?.ok_count ?? 0}/{macro?.anchors?.total ?? 0} 实时可用）
            </summary>
            <div className="mt-2.5 grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-4">
              {anchorGroups.map((grp) => (
                <div key={grp.label} className="rounded-md border border-hairline-soft bg-sunken/50 p-2.5">
                  <div className="mb-1.5 text-[11px] font-semibold text-ink-2">{grp.label}</div>
                  <div className="flex flex-col divide-y divide-hairline-soft">
                    {grp.rows.map((r) => (
                      <div
                        key={r.symbol}
                        className="grid grid-cols-12 items-center gap-1 py-1 text-[11px] font-mono tabular-nums"
                      >
                        <button
                          type="button"
                          onClick={() => onJumpWarRoom(r.symbol)}
                          className="col-span-5 cursor-pointer truncate border-0 bg-transparent p-0 text-left font-sans text-[11px] text-ink hover:text-accent"
                          title={`打开 ${r.name} (${r.symbol}) 缠论战情室`}
                        >
                          {r.name} <span className="font-mono text-[10px] text-muted">{r.symbol}</span>
                        </button>
                        <span className="col-span-3 text-right text-ink-2">
                          {r.last !== null ? r.last.toLocaleString("en-US", { maximumFractionDigits: 2 }) : "—"}
                        </span>
                        <span className={`col-span-2 text-right ${signedToneClass(r.ret_20d_pct)}`} title="20日涨跌幅">
                          {fmtPct(r.ret_20d_pct, 1)}
                        </span>
                        <span className={`col-span-2 text-right ${signedToneClass(r.ret_60d_pct)}`} title="60日涨跌幅">
                          {fmtPct(r.ret_60d_pct, 1)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </div>

      {/* 2. Layer 1 AI 7-Dimension Top-Down Strategic Compass */}
      <div className="card flex flex-col gap-3.5 p-4">
        {/* Header Bar */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded border border-accent/35 bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-accent">
                Layer 1
              </span>
              <h2 className="m-0 text-[15px] font-semibold text-ink">
                7 维自上而下板块罗盘 · 最优多空方向与板块
              </h2>
            </div>
            <p className="m-0 mt-0.5 text-xs text-muted">
              去标的池锚定盲推演：先选出未来 1Q~1Y 最具逻辑的多空板块与产业链切入点 → 带入下方 Layer 2 由你自主挑池/选龙头跑日线缠论
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {history.length > 0 ? (
              <div className="inline-flex items-center gap-1.5">
                <History className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                <select
                  className="input h-8 py-0 text-xs"
                  aria-label="切换历史战略推演期次"
                  value={selectedRunId}
                  onChange={(e) => void handleSelectHistory(e.target.value)}
                  disabled={loadingRun}
                >
                  {history.map((h) => (
                    <option key={h.run_id} value={h.run_id}>
                      {shortTime(h.finished_at_utc)} · {h.regime_title.slice(0, 18)} ({h.directions_count}向)
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            {payload || isRunActive ? (
              <button
                type="button"
                className="btn btn-primary"
                disabled={isRunActive}
                onClick={() => {
                  setLaunchErr(null);
                  setLaunchOpen(true);
                }}
              >
                {isRunActive ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                <span>{isRunActive ? "战略推演运行中…" : "重新运行战略推演"}</span>
              </button>
            ) : null}
          </div>
        </div>

        {/* Live SSE Task Progress Banner */}
        {activeRun ? (
          <div
            className={`flex flex-col gap-2.5 rounded-lg border p-3 ${
              activeRun.status === "completed"
                ? "border-bull/40 bg-bull/8"
                : activeRun.status === "failed" || activeRun.status === "cancelled"
                  ? "border-critical/40 bg-critical/8"
                  : "border-accent/40 bg-accent/8"
            }`}
            aria-live="polite"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                {isRunActive ? (
                  <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden="true" />
                ) : activeRun.status === "completed" ? (
                  <CheckCircle2 className="h-4 w-4 text-bull" aria-hidden="true" />
                ) : (
                  <XCircle className="h-4 w-4 text-critical" aria-hidden="true" />
                )}
                <span className="font-semibold text-ink">
                  {isRunActive
                    ? "Layer 1 7 维自上而下战略推演进行中"
                    : activeRun.status === "completed"
                      ? "Layer 1 战略推演已完成并落盘"
                      : activeRun.status === "cancelled"
                        ? "已终止本次战略推演"
                        : "战略推演执行失败"}
                </span>
                <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 font-mono text-[10px] text-muted">
                  {activeRun.engine.toUpperCase()} · {formatElapsed((activeRun.finishedAtMs ?? nowMs) - activeRun.startedAtMs)}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                {isRunActive ? (
                  <button
                    type="button"
                    className="btn btn-sm border-critical/40 text-critical"
                    disabled={cancelling}
                    onClick={() => void handleCancelCompass()}
                  >
                    <Square className="h-3 w-3" aria-hidden="true" />
                    <span>{cancelling ? "终止中…" : "终止"}</span>
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => setActiveRun(null)}
                  >
                    关闭提示
                  </button>
                )}
              </div>
            </div>

            {/* 4-Stage Stepper */}
            <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
              {COMPASS_STAGES.map((st, idx) => {
                const curIdx = stageIndex(activeRun.stage, activeRun.done, activeRun.status);
                const done = idx < curIdx || activeRun.status === "completed";
                const current = idx === curIdx && isRunActive;
                return (
                  <div
                    key={st.key}
                    className={`flex items-center gap-1.5 rounded border px-2 py-1 text-[11px] ${
                      done
                        ? "border-bull/30 bg-bull/10 text-bull"
                        : current
                          ? "border-accent/45 bg-surface text-ink font-medium"
                          : "border-hairline-soft bg-sunken/60 text-muted"
                    }`}
                  >
                    <span className="font-mono text-[10px]">{idx + 1}.</span>
                    <span className="truncate">{st.label}</span>
                  </div>
                );
              })}
            </div>

            {activeRun.error ? (
              <div className="text-xs text-critical">{activeRun.error}</div>
            ) : null}

            {activeRun.logs.length > 0 ? (
              <div className="max-h-28 overflow-y-auto rounded border border-hairline-soft bg-sunken/80 px-2.5 py-1.5 font-mono text-[11px] text-ink-2">
                {activeRun.logs.slice(-6).map((l, i) => (
                  <div key={`${l.ts}-${i}`} className="truncate py-0.5">
                    <span className="mr-2 text-muted">{shortTime(l.ts)}</span>
                    <span>{l.message}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Empty State (First-time Guidance) */}
        {!payload ? (
          <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-hairline bg-sunken/40 px-6 py-10 text-center">
            <Compass className="h-8 w-8 text-accent/80" aria-hidden="true" />
            <div className="max-w-xl">
              <div className="text-sm font-semibold text-ink">
                尚未生成本期 Layer 1 自上而下战略定调
              </div>
              <p className="m-0 mt-1 text-xs leading-relaxed text-muted">
                不提前喂入本地标的池与持仓，彻底避免锚定偏差。点击下方按钮，AI 将纯基于上方中美宏观双核与实时全网检索，按 7 维驱动框架筛出未来 1Q~1Y 最具逻辑的多空板块与产业链切入点；选定板块后，具体真龙头交由你在下方 Layer 2 自主筛选与日线过筛。
              </p>
            </div>
            {!isRunActive ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  setLaunchErr(null);
                  setLaunchOpen(true);
                }}
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                <span>运行 Layer 1 战略推演</span>
              </button>
            ) : null}
          </div>
        ) : (
          <>
            {/* Regime Summary + Avoid Sectors Banner */}
            <div className="grid grid-cols-1 gap-3 rounded-lg border border-hairline bg-elevated/60 p-3.5 lg:grid-cols-12">
              <div className="flex flex-col gap-2 lg:col-span-8">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-semibold text-ink">{payload.regime_title}</span>
                  <span className="font-mono text-[10px] text-muted">
                    {shortTime(currentRun?.finished_at_utc)} · {(currentRun?.engine || "claude").toUpperCase()}
                  </span>
                  {currentRun?.focus_prompt ? (
                    <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                      侧重：{currentRun.focus_prompt.slice(0, 26)}
                      {currentRun.focus_prompt.length > 26 ? "…" : ""}
                    </span>
                  ) : null}
                </div>
                <p className="m-0 text-xs leading-relaxed text-ink-2">{payload.regime_summary}</p>

                {/* Collapsible 4-Pillar Macro Breakdown */}
                {payload.macro_signals &&
                Object.values(payload.macro_signals).some((v) => Boolean(v && v.trim())) ? (
                  <details className="mt-0.5">
                    <summary className="cursor-pointer select-none text-[11px] font-medium text-accent hover:underline">
                      展开四维宏观与产业脉动细项
                    </summary>
                    <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                      {payload.macro_signals.us_macro ? (
                        <div className="rounded border border-hairline-soft bg-surface px-2.5 py-1.5 text-[11px]">
                          <span className="font-semibold text-ink">美国宏观与流动性：</span>
                          <span className="text-ink-2">{payload.macro_signals.us_macro}</span>
                        </div>
                      ) : null}
                      {payload.macro_signals.china_macro ? (
                        <div className="rounded border border-hairline-soft bg-surface px-2.5 py-1.5 text-[11px]">
                          <span className="font-semibold text-ink">中国财政与信用：</span>
                          <span className="text-ink-2">{payload.macro_signals.china_macro}</span>
                        </div>
                      ) : null}
                      {payload.macro_signals.geopolitics_supply ? (
                        <div className="rounded border border-hairline-soft bg-surface px-2.5 py-1.5 text-[11px]">
                          <span className="font-semibold text-ink">地缘与资源国政策：</span>
                          <span className="text-ink-2">{payload.macro_signals.geopolitics_supply}</span>
                        </div>
                      ) : null}
                      {payload.macro_signals.tech_industry ? (
                        <div className="rounded border border-hairline-soft bg-surface px-2.5 py-1.5 text-[11px]">
                          <span className="font-semibold text-ink">技术与产业迭代：</span>
                          <span className="text-ink-2">{payload.macro_signals.tech_industry}</span>
                        </div>
                      ) : null}
                    </div>
                  </details>
                ) : null}
              </div>

              {/* Adverse / Avoid Sectors & Derived Short/Rotation Strategies */}
              <div className="flex flex-col gap-1.5 border-t border-hairline-soft pt-2.5 lg:col-span-4 lg:border-l lg:border-t-0 lg:pl-3.5 lg:pt-0">
                <div className="flex items-center justify-between gap-2 text-[11px] font-semibold text-warning">
                  <span className="inline-flex items-center gap-1.5">
                    <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>当下逆风 · 回避与衍生策略</span>
                  </span>
                </div>
                {(payload.avoid_sectors ?? []).length > 0 ? (
                  <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                    {(payload.avoid_sectors ?? []).map((av, i) => {
                      const canDeriveShort = isLegacyHeadwindShortable(av);
                      return (
                        <li
                          key={`${av.sector}-${i}`}
                          className="flex flex-col gap-1 rounded border border-hairline-soft bg-surface/80 px-2.5 py-1.5 text-[11px]"
                        >
                          <div>
                            <span className="font-semibold text-ink">{av.sector}</span>
                            {av.reason ? <span className="text-muted"> — {av.reason}</span> : null}
                          </div>
                          {av.derived_strategy ? (
                            <div className="text-[10.5px] leading-snug text-ink-2">
                              <span className="font-semibold text-accent">延伸策略：</span>
                              <span>{av.derived_strategy}</span>
                            </div>
                          ) : null}
                          {canDeriveShort ? (
                            <div className="flex flex-wrap items-center justify-between gap-1.5 pt-0.5">
                              {av.short_proxy_hint ? (
                                <span className="text-[10px] text-bear/90">
                                  空头代理：{av.short_proxy_hint}
                                </span>
                              ) : (
                                <span className="text-[10px] text-muted">
                                  可用美股/ETF、商品/外汇/加密空头或代理表达（A 股禁空）
                                </span>
                              )}
                              <button
                                type="button"
                                className="btn btn-sm border-bear/35 px-2 py-0.5 text-[10.5px] text-bear"
                                disabled={scanBusy}
                                onClick={() => setPickerCard(buildShortCardFromHeadwind(av, i))}
                                title="按此逆风逻辑挑选合规空头/代理标的并跑日线缠论过筛（A 股禁空）"
                              >
                                <TrendingDown className="h-2.5 w-2.5" aria-hidden="true" />
                                <span>衍生做空选标</span>
                              </button>
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <div className="text-[11px] text-muted">本期未列出显著逆风板块</div>
                )}
              </div>
            </div>

            {/* Direction Filter Tabs */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="按多空方向筛选">
                {(
                  [
                    { key: "all", label: `全部高共振 (${directionCounts.all})` },
                    { key: "long", label: `做多方向 (${directionCounts.long})` },
                    { key: "short", label: `做空方向 (${directionCounts.short})` },
                    { key: "pair", label: `多空配对 (${directionCounts.pair})` },
                  ] as const
                ).map((tab) => (
                  <button
                    key={tab.key}
                    type="button"
                    className="chip"
                    aria-pressed={dirFilter === tab.key}
                    onClick={() => setDirFilter(tab.key)}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
              <span className="text-[11px] text-muted">
                点击卡片「自选标的过筛」即可为该 L1 逻辑挑选专属小池标的，与下方手动大类池扫描完全隔离
              </span>
            </div>

            {/* High-Resonance Direction Cards Grid */}
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {filteredDirections.map((card) => {
                const dm = directionMeta(card.direction);
                const DirIcon = dm.Icon;

                return (
                  <article
                    key={card.id}
                    className={`flex flex-col justify-between gap-3 rounded-lg border border-hairline border-l-2 bg-surface p-3.5 transition-colors hover:border-accent/40 ${dm.borderClass}`}
                  >
                    <div className="flex flex-col gap-2.5">
                      {/* Card Top Badges */}
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-mono text-xs font-semibold text-muted">#{card.rank}</span>
                          <span
                            className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-semibold ${dm.badgeClass}`}
                          >
                            <DirIcon className="h-3 w-3" aria-hidden="true" />
                            <span>{dm.label}</span>
                          </span>
                          <span className="rounded border border-hairline bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-ink-2">
                            {assetScopeLabel(card.asset_scope)}
                          </span>
                          <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 font-mono text-[10px] text-muted">
                            {card.horizon}
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {card.is_new_theme ? (
                            <span className="rounded border border-accent/35 bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                              新挖掘主题
                            </span>
                          ) : null}
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                              card.conviction === "high"
                                ? "bg-accent/15 text-accent"
                                : "bg-sunken text-muted"
                            }`}
                          >
                            {card.conviction === "high" ? "高确信度" : "中等确信度"}
                          </span>
                        </div>
                      </div>

                      {/* Title */}
                      <h3 className="m-0 text-[14px] font-semibold leading-snug text-ink">{card.title}</h3>

                      {/* 2-Dimension Resonance Pills */}
                      <div className="flex flex-col gap-1 rounded-md border border-hairline-soft bg-sunken/60 p-2 text-[11px]">
                        <div className="flex items-start gap-1.5">
                          <span className="shrink-0 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold text-accent">
                            主 · {card.primary_driver.dimension}
                          </span>
                          <span className="text-ink-2">{card.primary_driver.summary}</span>
                        </div>
                        <div className="flex items-start gap-1.5">
                          <span className="shrink-0 rounded bg-subtle px-1.5 py-0.5 text-[10px] font-semibold text-ink-2">
                            次 · {card.secondary_driver.dimension}
                          </span>
                          <span className="text-muted">{card.secondary_driver.summary}</span>
                        </div>
                      </div>

                      {/* Core Thesis */}
                      <p className="m-0 text-xs leading-relaxed text-ink">{card.thesis}</p>

                      {/* Forward Expectation Gap (Priced-In vs Unpriced 1Q~1Y Delta) */}
                      {card.priced_in_already || card.unpriced_gap ? (
                        <div className="flex flex-col gap-1 rounded-md border border-hairline-soft bg-sunken/45 px-2.5 py-2 text-[11px] leading-snug">
                          {card.priced_in_already ? (
                            <div className="text-muted">
                              <span className="font-semibold text-ink-2">已计价：</span>
                              <span>{card.priced_in_already}</span>
                            </div>
                          ) : null}
                          {card.unpriced_gap ? (
                            <div className="text-ink-2">
                              <span className="font-semibold text-accent">未计价预期差：</span>
                              <span>{card.unpriced_gap}</span>
                            </div>
                          ) : null}
                        </div>
                      ) : null}

                      {/* Pair Legs Breakdown (for pair sector cards) */}
                      {card.direction === "pair" && (card.long_leg_sector || card.short_leg_sector) ? (
                        <div className="grid grid-cols-1 gap-1.5 text-[11px] sm:grid-cols-2">
                          {card.long_leg_sector ? (
                            <div className="rounded border border-bull/30 bg-bull/8 px-2.5 py-1.5">
                              <span className="font-semibold text-bull">做多板块腿：</span>
                              <span className="text-ink-2">{card.long_leg_sector}</span>
                            </div>
                          ) : null}
                          {card.short_leg_sector ? (
                            <div className="rounded border border-bear/30 bg-bear/8 px-2.5 py-1.5">
                              <span className="font-semibold text-bear">做空板块腿：</span>
                              <span className="text-ink-2">{card.short_leg_sector}</span>
                            </div>
                          ) : null}
                        </div>
                      ) : null}

                      {/* Value Chain Focus & Leader-Picking Criteria */}
                      {card.value_chain_focus ? (
                        <div className="rounded-md border border-accent/25 bg-accent/6 px-2.5 py-2 text-[11px]">
                          <span className="font-semibold text-accent">产业链切入与选龙头抓手：</span>
                          <span className="text-ink">{card.value_chain_focus}</span>
                        </div>
                      ) : null}

                      {/* Post-hoc Portfolio Overlap Notice (strictly informational, never fed to AI) */}
                      {(card.portfolio_overlap ?? []).length > 0 ? (
                        <div className="flex flex-wrap items-center gap-1.5 rounded border border-warning/30 bg-warning/8 px-2.5 py-1 text-[11px] text-warning">
                          <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
                          <span>当前已有相关持仓：{(card.portfolio_overlap ?? []).join("、")}（注意单板块龙头独占）</span>
                        </div>
                      ) : null}

                      {/* Catalyst & Invalidation */}
                      <div className="grid grid-cols-1 gap-1.5 text-[11px] sm:grid-cols-2">
                        {card.key_catalyst ? (
                          <div className="rounded border border-hairline-soft bg-elevated/50 px-2 py-1.5">
                            <span className="font-semibold text-ink-2">近期催化：</span>
                            <span className="text-muted">{card.key_catalyst}</span>
                          </div>
                        ) : null}
                        {card.invalidation ? (
                          <div className="rounded border border-critical/25 bg-critical/5 px-2 py-1.5">
                            <span className="font-semibold text-critical">证伪红线：</span>
                            <span className="text-ink-2">{card.invalidation}</span>
                          </div>
                        ) : null}
                      </div>

                      {/* Optional Reference Anchors (pricing reference only) */}
                      {(card.reference_anchors ?? []).length > 0 ? (
                        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                          <span className="text-muted">板块基准参考锚：</span>
                          {(card.reference_anchors ?? []).map((sym) => (
                            <button
                              key={`${card.id}-anc-${sym}`}
                              type="button"
                              className="chip py-0.5 text-[11px]"
                              onClick={() => onJumpWarRoom(sym)}
                              title={`查看 ${sym} 日线与 30min 缠论基准走势`}
                            >
                              <SymbolLabel symbol={sym} size="sm" />
                            </button>
                          ))}
                        </div>
                      ) : null}

                      {/* Legacy Leaders Section (only shown if historical run populated leaders) */}
                      {card.leaders.length > 0 ? (
                        <div className="flex flex-col gap-1.5">
                          <div className="text-[11px] font-semibold text-ink-2">
                            历史记录参考标的（{card.leaders.length}）
                          </div>
                          <div className="flex flex-col gap-1.5">
                            {card.leaders.map((leader) => (
                              <div
                                key={`${card.id}-${leader.symbol}`}
                                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-hairline-soft bg-sunken/70 px-2.5 py-1.5"
                              >
                                <div className="min-w-0 flex-1">
                                  <div className="flex flex-wrap items-center gap-1.5">
                                    <span
                                      className={`rounded px-1 py-0.2 font-mono text-[10px] font-semibold ${
                                        leader.leg === "short"
                                          ? "bg-bear/15 text-bear"
                                          : "bg-bull/15 text-bull"
                                      }`}
                                    >
                                      {leader.leg === "short" ? "空" : "多"}
                                    </span>
                                    <SymbolLabel symbol={leader.symbol} name={leader.name} size="sm" />
                                    <span className="text-[10px] text-muted">
                                      {leader.role === "co_leader" ? "双寡头" : "唯一龙头"}
                                    </span>
                                  </div>
                                  {leader.reason ? (
                                    <div className="mt-0.5 text-[11px] text-muted">{leader.reason}</div>
                                  ) : null}
                                </div>
                                <div className="flex shrink-0 items-center gap-1">
                                  <button
                                    type="button"
                                    className="btn btn-sm px-2"
                                    onClick={() => onJumpWarRoom(leader.symbol)}
                                    title="打开 30min + 日线缠论战情室"
                                  >
                                    <Crosshair className="h-3 w-3 text-accent" aria-hidden="true" />
                                    <span>战情</span>
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-sm px-2"
                                    onClick={() => onJumpArchive(leader.symbol)}
                                    title="进入研报档案（Layer 3 深度研究）"
                                  >
                                    <BookOpen className="h-3 w-3" aria-hidden="true" />
                                    <span>档案</span>
                                  </button>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : null}

                      {/* Evidence Disclosure (Native <details>, no heading inside <summary>) */}
                      {card.evidence.length > 0 ? (
                        <details className="text-[11px]">
                          <summary className="cursor-pointer select-none font-medium text-muted hover:text-ink">
                            查看 L1~L4 联网证据溯源（{card.evidence.length} 条）
                          </summary>
                          <ul className="m-0 mt-1.5 flex list-none flex-col gap-1.5 pl-0">
                            {card.evidence.map((ev, i) => {
                              const href = safeExternalUrl(ev.url);
                              return (
                                <li
                                  key={`${card.id}-ev-${i}`}
                                  className="rounded border border-hairline-soft bg-sunken/50 px-2 py-1.5 text-ink-2"
                                >
                                  <div className="flex items-start gap-1.5">
                                    <span className="shrink-0 rounded border border-hairline bg-surface px-1 font-mono text-[10px] font-semibold text-accent">
                                      {ev.level}
                                    </span>
                                    <span className="flex-1">{ev.fact}</span>
                                  </div>
                                  <div className="mt-1 flex flex-wrap items-center gap-2 pl-6 font-mono text-[10px] text-muted">
                                    {ev.date ? <span>{ev.date}</span> : null}
                                    {ev.source ? <span className="font-sans">{ev.source}</span> : null}
                                    {ev.verification_status === "ai_cited_unverified" ? (
                                      <span className="rounded bg-elevated px-1 font-sans text-[9.5px] text-muted">
                                        AI 引用·待核验
                                      </span>
                                    ) : null}
                                    {href ? (
                                      <a
                                        href={href}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-0.5 text-accent hover:underline"
                                      >
                                        <span>原文</span>
                                        <ExternalLink className="h-2.5 w-2.5" aria-hidden="true" />
                                      </a>
                                    ) : null}
                                  </div>
                                </li>
                              );
                            })}
                          </ul>
                        </details>
                      ) : null}
                    </div>

                    {/* Card Action Footer: Dedicated L1 Mini-Pool Leader Picker */}
                    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2.5">
                      <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        disabled={scanBusy}
                        onClick={() => setPickerCard(card)}
                        title="在专属小池中自主挑选符合本 L1 逻辑的 1~3 只标的并做日线缠论联立过筛"
                      >
                        <span>自选标的过筛</span>
                        <ArrowRight className="h-3 w-3" aria-hidden="true" />
                      </button>
                      <span className="text-[10.5px] text-muted">
                        专属小池独立校验 · 不混入大类杂池
                      </span>
                    </div>
                  </article>
                );
              })}
            </div>

            {/* Global Risk Warnings */}
            {(payload.risk_warnings ?? []).length > 0 ? (
              <div className="flex flex-wrap items-start gap-2 rounded-md border border-warning/30 bg-warning/8 px-3 py-2 text-[11px] text-ink-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                <div className="flex flex-col gap-0.5">
                  <span className="font-semibold text-warning">跨资产宏观风险与因子重叠提示</span>
                  {(payload.risk_warnings ?? []).map((w, i) => (
                    <span key={i}>• {w}</span>
                  ))}
                </div>
              </div>
            ) : null}
          </>
        )}
      </div>

      {/* Launch Layer 1 Synthesis Modal */}
      <ModalDialog
        open={launchOpen}
        onClose={() => setLaunchOpen(false)}
        title="运行 Layer 1 · 7 维自上而下板块盲推演"
        subtitle="不喂本地标的池与持仓，纯从宏观与全网催化推演 1Q~1Y 最优多空板块"
        lightDismiss
      >
        <form onSubmit={handleStartCompass} className="flex flex-col gap-3.5 p-5">
          <p className="m-0 text-xs leading-relaxed text-muted">
            后端仅向 AI 提供客观中美宏观双核与大类基准硬数据（不传入现有标的池和持仓，彻底杜绝锚定偏差），由 AI 实时全网检索并按 7 维框架选出 1Q~1Y 最优多空板块与产业链切入点；推演完成后由程序自动匹配本地池，具体真龙头留给你在下方 Layer 2 自主筛选。
          </p>

          {/* Engine Selector */}
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-ink">AI 推演引擎</span>
            <div className="grid grid-cols-2 gap-2">
              {(enginesInfo?.engines ?? [
                { id: "claude" as DeepResearchEngine, label: "Claude Code", description: "主力联网推演", available: true },
                { id: "codex" as DeepResearchEngine, label: "Codex", description: "备选跨模型引擎", available: true },
              ]).map((eng) => {
                const active = selectedEngine === eng.id;
                return (
                  <button
                    key={eng.id}
                    type="button"
                    disabled={!eng.available}
                    onClick={() => setSelectedEngine(eng.id)}
                    className={`flex flex-col items-start gap-0.5 rounded-md border p-2.5 text-left transition-colors ${
                      active
                        ? "border-accent bg-accent/10 text-ink"
                        : "border-hairline bg-sunken/60 text-ink-2 hover:border-accent/40"
                    } ${!eng.available ? "cursor-not-allowed opacity-45" : "cursor-pointer"}`}
                  >
                    <span className="text-xs font-semibold">{eng.label}</span>
                    <span className="text-[11px] text-muted">
                      {eng.available ? eng.description : "本机未检测到 CLI"}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Reasoning Effort */}
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-ink">推理深度</span>
            <div className="flex items-center gap-1.5">
              {(
                [
                  { id: "low", label: "快速 (Low)" },
                  { id: "medium", label: "均衡推荐 (Medium)" },
                  { id: "high", label: "深度推演 (High)" },
                ] as const
              ).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  className="chip"
                  aria-pressed={effort === opt.id}
                  onClick={() => setEffort(opt.id)}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* Multi-select Target Battlefields */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium text-ink">
                重点深挖战场（可多选；不选则全市场均衡）
              </span>
              {selectedFocusScopes.length > 0 ? (
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0 text-[11px] text-accent hover:underline"
                  onClick={() => setSelectedFocusScopes([])}
                >
                  清空（恢复全市场）
                </button>
              ) : (
                <span className="text-[11px] text-muted">当前：全市场均衡</span>
              )}
            </div>

            <div className="flex flex-col gap-2 rounded-md border border-hairline-soft bg-sunken/50 p-2.5">
              {FOCUS_SCOPE_GROUPS.map((grp) => (
                <div key={grp.group} className="flex flex-col gap-1">
                  <span className="text-[10.5px] font-medium text-muted">{grp.group}</span>
                  <div className="flex flex-wrap gap-1.5">
                    {grp.items.map((item) => {
                      const active = selectedFocusScopes.includes(item.id);
                      return (
                        <button
                          key={item.id}
                          type="button"
                          className="chip text-[11px]"
                          aria-pressed={active}
                          title={item.hint}
                          onClick={() => toggleFocusScope(item.id)}
                        >
                          {item.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Optional Free-Text Supplement */}
          <div className="flex flex-col gap-1">
            <label htmlFor="compass-focus-prompt" className="text-xs font-medium text-ink">
              补充推演要求（可选）
            </label>
            <textarea
              id="compass-focus-prompt"
              rows={2}
              className="input resize-none text-xs"
              placeholder="如：重点对比铜/铝/镍内部强弱，以及商品货币澳元/加元是否具备右侧做空条件"
              value={focusPrompt}
              onChange={(e) => setFocusPrompt(e.target.value)}
            />
          </div>

          <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={refreshMacroOnRun}
              onChange={(e) => setRefreshMacroOnRun(e.target.checked)}
              className="accent-accent"
            />
            <span>推演前强制刷新最新宏观与跨资产定价锚硬数据</span>
          </label>

          {launchErr ? (
            <div className="rounded-md border border-critical/40 bg-critical/10 px-3 py-2 text-xs text-critical">
              {launchErr}
            </div>
          ) : null}

          <div className="flex items-center justify-end gap-2 border-t border-hairline-soft pt-3">
            <button type="button" className="btn" onClick={() => setLaunchOpen(false)}>
              取消
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={launching || (enginesInfo !== null && !enginesInfo.any_available)}
            >
              <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{launching ? "正在启动…" : "开始 7 维推演"}</span>
            </button>
          </div>
        </form>
      </ModalDialog>

      {/* Dedicated L1 Mini-Pool Leader Picker Modal */}
      <Layer1LeaderPickerModal
        open={pickerCard !== null}
        card={pickerCard}
        runId={currentRun?.run_id}
        universes={universes}
        scanBusy={scanBusy}
        onClose={() => setPickerCard(null)}
        onLaunchScan={onLaunchCompassScan}
        onUniverseSaved={onUniverseSaved}
      />
    </section>
  );
}
