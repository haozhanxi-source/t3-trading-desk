import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  AlertTriangle,
  BookmarkPlus,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Compass,
  Crosshair,
  Database,
  FolderPlus,
  History,
  Layers,
  Play,
  RefreshCw,
  Square,
  X,
  XCircle,
} from "lucide-react";
import { api, ApiError, errorMessage, parseTaskEvent } from "../api/client";
import type {
  ArchiveInitBody,
  CompassDirectionCard,
  CompassRunRecord,
  CompassScanContext,
  PersistedScanRun,
  ScanRow,
  TaskEventEnvelope,
  TaskRecord,
  UniversePool,
  WarehouseSummary,
} from "../api/types";
import { ModalDialog } from "../components/ui/ModalDialog";
import { signedToneClass, StatusBadge, type Tone } from "../components/ui/StatusBadge";
import { Layer1CompassPanel, type L2CardScanSummary } from "../components/workbench/Layer1CompassPanel";
import { Layer2InlinePicker } from "../components/workbench/Layer2InlinePicker";
import { ZhongshuMiniRuler } from "../components/workbench/ZhongshuMiniRuler";
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
import {
  classifyRowWithCompass,
  classifyScanRow,
  detectShortChaseWarning,
  resolveExpectedDirection,
  type CompassRowAlignment,
  type RowFilter,
} from "../lib/scanAlignment";
import { formatTaskStatus, getUniverseLabel, groupUniverses } from "../lib/universes";

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

function formatDirectionBiasZh(dir: string | null | undefined): string {
  const d = (dir || "").trim().toLowerCase();
  if (d === "long") return "做多";
  if (d === "short") return "做空";
  if (d === "pair") return "多空配对";
  return "中性观察";
}

function formatCompassSecondaryDriver(card: CompassDirectionCard | null): string {
  if (!card) return "";
  const pDim = (card.primary_driver?.dimension || "").trim();
  const pSum = (card.primary_driver?.summary || "").trim();
  const sDim = (card.secondary_driver?.dimension || "").trim();
  const sSum = (card.secondary_driver?.summary || "").trim();
  if (pDim && sDim) {
    return `${pDim} × ${sDim}${sSum ? `（${sSum}）` : ""}`;
  }
  if (sDim) {
    return sSum ? `${sDim}（${sSum}）` : sDim;
  }
  if (sSum) return sSum;
  if (pDim) return pSum ? `${pDim}（${pSum}）` : pDim;
  return pSum;
}

function findCompassCardInRun(
  run: CompassRunRecord | null | undefined,
  ctx: CompassScanContext,
): CompassDirectionCard | null {
  const directions = run?.result?.directions;
  if (Array.isArray(directions) && directions.length > 0) {
    if (ctx.direction_id) {
      const byId = directions.find((d) => d.id === ctx.direction_id);
      if (byId) return byId;
    }
    const title = (ctx.direction_title || "").trim();
    if (title) {
      const byTitle = directions.find((d) => (d.title || "").trim() === title);
      if (byTitle) return byTitle;
    }
  }
  const avoidSectors = run?.result?.avoid_sectors;
  if (Array.isArray(avoidSectors) && avoidSectors.length > 0) {
    const idMatch = /^headwind_short_(\d+)$/.exec((ctx.direction_id || "").trim());
    const idx = idMatch ? Number(idMatch[1]) - 1 : -1;
    const title = (ctx.direction_title || "").trim();
    const hit =
      (idx >= 0 && idx < avoidSectors.length ? avoidSectors[idx] : undefined) ??
      (title
        ? avoidSectors.find(
            (item) =>
              `逆风衍生做空：${item.sector}` === title || item.sector === title,
          )
        : undefined);
    if (hit) {
      const focus =
        hit.short_proxy_hint ||
        hit.derived_strategy ||
        ctx.value_chain_focus ||
        "";
      return {
        id: ctx.direction_id || `headwind_short_${idx + 1}`,
        rank: idx >= 0 ? idx + 1 : 1,
        title: title || `逆风衍生做空：${hit.sector}`,
        direction: "short",
        horizon: "1Q~2Q",
        conviction: "medium",
        asset_scope: "cross_asset",
        primary_driver: { dimension: "宏观双核", summary: hit.reason || hit.sector },
        secondary_driver: {
          dimension: "商品供需平衡与库存周期",
          summary: "逆风逻辑延伸合规空头/代理表达",
        },
        thesis: hit.reason || hit.sector,
        value_chain_focus: focus,
        key_catalyst: "",
        invalidation: "",
        recommended_universes: [],
        is_new_theme: true,
        suggested_pool_name: ctx.direction_id || "short_headwind",
        suggested_pool_description: title || `逆风衍生做空：${hit.sector}`,
        leaders: [],
        evidence: [],
      };
    }
  }
  return null;
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

function signalTierRank(row: ScanRow): number {
  if (row.error) return 99;
  const st = (row.signal_type || "").trim().toLowerCase();
  if (row.quality_grade === "degraded" || st === "data_quality_degraded") return 80;
  if (st === "insufficient_data") return 85;
  if (st === "confirmed_buy" || st === "confirmed_sell") return 0;
  if (
    st === "momentum_long" ||
    st === "momentum_short" ||
    st === "steep_non_div_up" ||
    st === "steep_non_div_down"
  ) {
    return 10;
  }
  if (st === "exhaustion_buy_warn" || st === "exhaustion_sell_warn") return 20;
  if (st === "candidate_buy" || st === "candidate_sell") return 30;
  if (st === "weak_momentum_long" || st === "weak_momentum_short") return 40;
  if (st === "consolidation") return 50;
  const tier = (row.signal_tier || "").trim();
  if (tier === "买卖点信号") return 5;
  if (tier.startsWith("资金一致推进")) return 15;
  if (tier === "背驰观察") return 32;
  if (tier === "中枢震荡") return 50;
  if (tier === "常规结构运行") return 60;
  return 70;
}

function directionalReturnScore(val: number | null | undefined, dir: "long" | "short" | null): number {
  if (typeof val !== "number" || !Number.isFinite(val)) return -1e9;
  return dir === "short" ? -val : val;
}

const UNCONFIRMED_SIG_RE = /(?:候选|疑似|准|观察|蓄势)/;

function resolveSignalBadge(row: ScanRow): { label: string; tone: Tone } | null {
  const sigType = (row.signal_type || "").trim().toLowerCase();
  const tier = (row.signal_tier || "").trim();
  const dir = (row.signal_direction || "").trim().toLowerCase();
  const bias = (row.action_bias || "").trim();
  const rawSignals = Array.isArray(row.signals) ? row.signals.map((s) => String(s || "")) : [];
  // Exclude the synthetic bracketed summary entry `【...】` (which bundles slashes like "三买 / 2+3买")
  // so exact signal parsing prioritizes concrete daily.signals items first.
  const detailedSignals = rawSignals.filter((s) => !/^【.*】$/.test(s.trim()));
  const confirmedSignals = detailedSignals.filter((s) => !UNCONFIRMED_SIG_RE.test(s));
  const confirmedBlob = confirmedSignals.join(" ");
  const allBlob = `${rawSignals.join(" ")} ${bias}`;

  if (row.quality_grade === "degraded" || sigType === "data_quality_degraded" || tier === "数据质量降级") {
    return { label: "数据降级", tone: "warning" };
  }
  if (sigType === "insufficient_data" || tier === "数据不足") {
    return { label: "数据不足", tone: "neutral" };
  }

  /* 1. Exhaustion warnings (non-first zhongshu 3rd buy/sell) */
  if (sigType === "exhaustion_buy_warn" || /非首中枢三买/.test(allBlob)) {
    return { label: "后段三买·防追", tone: "warning" };
  }
  if (sigType === "exhaustion_sell_warn" || /非首中枢三卖/.test(allBlob)) {
    return { label: "后段三卖·防追", tone: "serious" };
  }

  /* 2. Confirmed buy points: prioritize 4出/3出 composite signals before generic 2/3/1 buy */
  const isConfirmedBuy =
    sigType === "confirmed_buy" ||
    (tier === "买卖点信号" && (dir === "long" || dir === "bullish" || dir === "up"));
  if (isConfirmedBuy) {
    if (/2\+3买/.test(confirmedBlob)) return { label: "日线2+3买", tone: "good" };
    if (/4出(?:\(D4)?/.test(confirmedBlob)) {
      if (/三买/.test(confirmedBlob) && !/非首中枢三买/.test(confirmedBlob)) return { label: "日线四出·三买", tone: "good" };
      if (/二买/.test(confirmedBlob)) return { label: "日线四出·二买", tone: "good" };
      if (/一买/.test(confirmedBlob)) return { label: "日线四出·一买", tone: "good" };
      return { label: "日线四出买", tone: "good" };
    }
    if (/3出(?:\(D3)?/.test(confirmedBlob)) {
      if (/三买/.test(confirmedBlob) && !/非首中枢三买/.test(confirmedBlob)) return { label: "日线三出·三买", tone: "good" };
      if (/二买/.test(confirmedBlob)) return { label: "日线三出·二买", tone: "good" };
      if (/一买/.test(confirmedBlob)) return { label: "日线三出·一买", tone: "good" };
      return { label: "日线三出买", tone: "good" };
    }
    if (/三买/.test(confirmedBlob) && !/非首中枢三买/.test(confirmedBlob)) return { label: "日线三买", tone: "good" };
    if (/二买/.test(confirmedBlob)) return { label: "日线二买", tone: "good" };
    if (/一买/.test(confirmedBlob)) return { label: "日线一买", tone: "good" };
    if (/2\+3买/.test(allBlob)) return { label: "日线2+3买", tone: "good" };
    if (/4出(?:\(D4)?/.test(allBlob)) {
      if (/首中枢上沿\s*ZG|首中枢三买/.test(allBlob)) return { label: "日线四出·三买", tone: "good" };
      if (/二买/.test(allBlob)) return { label: "日线四出·二买", tone: "good" };
      if (/一买/.test(allBlob)) return { label: "日线四出·一买", tone: "good" };
      return { label: "日线四出买", tone: "good" };
    }
    if (/3出(?:\(D3)?/.test(allBlob)) {
      if (/首中枢上沿\s*ZG|首中枢三买/.test(allBlob)) return { label: "日线三出·三买", tone: "good" };
      if (/二买/.test(allBlob)) return { label: "日线三出·二买", tone: "good" };
      if (/一买/.test(allBlob)) return { label: "日线三出·一买", tone: "good" };
      return { label: "日线三出买", tone: "good" };
    }
    if (/首中枢上沿\s*ZG|首中枢三买/.test(allBlob)) return { label: "日线三买", tone: "good" };
    if (/二买/.test(allBlob)) return { label: "日线二买", tone: "good" };
    if (/一买/.test(allBlob)) return { label: "日线一买", tone: "good" };
    return { label: "日线买点", tone: "good" };
  }

  /* 3. Confirmed sell points: prioritize 4出/3出 composite signals before generic 2/3/1 sell */
  const isConfirmedSell =
    sigType === "confirmed_sell" ||
    (tier === "买卖点信号" && (dir === "short" || dir === "bearish" || dir === "down"));
  if (isConfirmedSell) {
    if (/2\+3卖/.test(confirmedBlob)) return { label: "日线2+3卖", tone: "critical" };
    if (/4出(?:\(D4)?/.test(confirmedBlob)) {
      if (/三卖/.test(confirmedBlob) && !/非首中枢三卖/.test(confirmedBlob)) return { label: "日线四出·三卖", tone: "critical" };
      if (/二卖/.test(confirmedBlob)) return { label: "日线四出·二卖", tone: "critical" };
      if (/一卖/.test(confirmedBlob)) return { label: "日线四出·一卖", tone: "critical" };
      return { label: "日线四出卖", tone: "critical" };
    }
    if (/3出(?:\(D3)?/.test(confirmedBlob)) {
      if (/三卖/.test(confirmedBlob) && !/非首中枢三卖/.test(confirmedBlob)) return { label: "日线三出·三卖", tone: "critical" };
      if (/二卖/.test(confirmedBlob)) return { label: "日线三出·二卖", tone: "critical" };
      if (/一卖/.test(confirmedBlob)) return { label: "日线三出·一卖", tone: "critical" };
      return { label: "日线三出卖", tone: "critical" };
    }
    if (/三卖/.test(confirmedBlob) && !/非首中枢三卖/.test(confirmedBlob)) return { label: "日线三卖", tone: "critical" };
    if (/二卖/.test(confirmedBlob)) return { label: "日线二卖", tone: "critical" };
    if (/一卖/.test(confirmedBlob)) return { label: "日线一卖", tone: "critical" };
    if (/2\+3卖/.test(allBlob)) return { label: "日线2+3卖", tone: "critical" };
    if (/4出(?:\(D4)?/.test(allBlob)) {
      if (/首中枢下沿\s*ZD|首中枢三卖/.test(allBlob)) return { label: "日线四出·三卖", tone: "critical" };
      if (/二卖/.test(allBlob)) return { label: "日线四出·二卖", tone: "critical" };
      if (/一卖/.test(allBlob)) return { label: "日线四出·一卖", tone: "critical" };
      return { label: "日线四出卖", tone: "critical" };
    }
    if (/3出(?:\(D3)?/.test(allBlob)) {
      if (/首中枢下沿\s*ZD|首中枢三卖/.test(allBlob)) return { label: "日线三出·三卖", tone: "critical" };
      if (/二卖/.test(allBlob)) return { label: "日线三出·二卖", tone: "critical" };
      if (/一卖/.test(allBlob)) return { label: "日线三出·一卖", tone: "critical" };
      return { label: "日线三出卖", tone: "critical" };
    }
    if (/首中枢下沿\s*ZD|首中枢三卖/.test(allBlob)) return { label: "日线三卖", tone: "critical" };
    if (/二卖/.test(allBlob)) return { label: "日线二卖", tone: "critical" };
    if (/一卖/.test(allBlob)) return { label: "日线一卖", tone: "critical" };
    return { label: "日线卖点", tone: "critical" };
  }

  /* 4. Capital consensus & steep non-divergence */
  if (sigType === "steep_non_div_up") return { label: "多头急拉一致", tone: "good" };
  if (sigType === "momentum_long") return { label: "多头一致", tone: "good" };
  if (sigType === "steep_non_div_down") return { label: "空头急跌一致", tone: "critical" };
  if (sigType === "momentum_short") return { label: "空头一致", tone: "critical" };

  /* 5. Unconfirmed candidate / divergence watch */
  if (sigType === "candidate_buy") return { label: "准买点·待确认", tone: "info" };
  if (sigType === "candidate_sell") return { label: "准卖点·待确认", tone: "serious" };
  if (tier === "背驰观察") return { label: "背驰观察", tone: "warning" };

  /* 6. Weak momentum / consolidation / neutral */
  if (sigType === "weak_momentum_long") return { label: "弱多延续", tone: "neutral" };
  if (sigType === "weak_momentum_short") return { label: "弱空延续", tone: "neutral" };
  if (sigType === "consolidation" || tier === "中枢震荡") return { label: "中枢震荡", tone: "neutral" };

  /* Legacy fallback without signal_type */
  if (tier === "买卖点信号") {
    const cls = classifyScanRow(row);
    if (cls === "long") return { label: "日线买点", tone: "good" };
    if (cls === "short") return { label: "日线卖点", tone: "critical" };
  }
  if (tier.includes("资金一致推进")) {
    if (tier.includes("顺势") || dir === "long") return { label: "多头一致", tone: "good" };
    if (tier.includes("勿逆势") || dir === "short") return { label: "空头一致", tone: "critical" };
  }
  if (tier) return { label: tier, tone: "neutral" };
  return null;
}

function shortBarDate(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (!s) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (!m) return s.slice(0, 10);
  const curYear = String(new Date().getFullYear());
  return m[1] === curYear ? `${m[2]}-${m[3]}` : `${m[1]}-${m[2]}-${m[3]}`;
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

  /* Quick add-to-watchlist (archiveInit) tracking + Toast feedback */
  const [archivedSyms, setArchivedSyms] = useState<Set<string>>(new Set());
  const [addingWatchSym, setAddingWatchSym] = useState<string | null>(null);
  const [archiveToast, setArchiveToast] = useState<{
    symbol: string;
    directionBias: string;
    theme: string;
    existed?: boolean;
  } | null>(null);

  /* Save new universe modal */
  const [savePoolOpen, setSavePoolOpen] = useState(false);
  const [savePoolPrefill, setSavePoolPrefill] = useState<{
    name: string;
    description: string;
    symbols: string[];
  } | null>(null);

  /* L1 <-> L2 seamless inline coordination & bidirectional scan summary badges */
  const l2TabBaseId = useId();
  const [selectedL1Track, setSelectedL1Track] = useState<{
    card: CompassDirectionCard;
    runId?: string;
  } | null>(null);
  const [l2Mode, setL2Mode] = useState<"l1_track" | "manual">("l1_track");
  const [l2CardSummaries, setL2CardSummaries] = useState<Record<string, L2CardScanSummary>>({});
  const [shadowState, setShadowState] = useState<{
    runId: string;
    loggedCardIds: string[];
    loggingCardId: string | null;
  }>({ runId: "", loggedCardIds: [], loggingCardId: null });
  const [openCardDetailSignal, setOpenCardDetailSignal] = useState<{
    card: CompassDirectionCard;
    seq: number;
  } | null>(null);
  const [logCardSignal, setLogCardSignal] = useState<{ cardId: string; seq: number } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showPriorScanForMismatch, setShowPriorScanForMismatch] = useState(false);

  const l2TabRefs = useRef<Record<"l1_track" | "manual", HTMLButtonElement | null>>({
    l1_track: null,
    manual: null,
  });
  const historyMenuRef = useRef<HTMLDivElement | null>(null);

  const esRef = useRef<EventSource | null>(null);
  const layer2Ref = useRef<HTMLDivElement | null>(null);
  const launchingRef = useRef(false);
  const scanReqSeqRef = useRef(0);
  const compassRunCacheRef = useRef<Map<string, CompassRunRecord>>(new Map());

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

  const resolveCompassCard = useCallback(
    async (ctx: CompassScanContext, signal?: AbortSignal): Promise<CompassDirectionCard | null> => {
      const runId = (ctx.run_id || "").trim();
      if (runId) {
        const cached = compassRunCacheRef.current.get(runId);
        if (cached && cached.run_id === runId) {
          return findCompassCardInRun(cached, ctx);
        }
        try {
          const fetched = await api.compassHistoryDetail(runId, signal);
          if (fetched?.run_id === runId) {
            compassRunCacheRef.current.set(fetched.run_id, fetched);
            return findCompassCardInRun(fetched, ctx);
          }
        } catch {
          /* Never fall back to latest run when a historical run_id fails or mismatches */
        }
        return null;
      }
      try {
        const latestRes = await api.compassLatest(signal);
        if (latestRes.latest) {
          compassRunCacheRef.current.set(latestRes.latest.run_id, latestRes.latest);
          return findCompassCardInRun(latestRes.latest, ctx);
        }
      } catch {
        /* ignore */
      }
      return null;
    },
    [],
  );

  const handleQuickAddWatchlist = async (row: ScanRow) => {
    const sym = row.symbol.trim().toUpperCase();
    if (archivedSyms.has(sym)) {
      onJumpArchive(sym);
      return;
    }
    setAddingWatchSym(sym);
    setErr(null);
    try {
      const scanCompassCtx = extractCompassContext(activeTask);
      const expectedInfo = scanCompassCtx ? resolveExpectedDirection(row, scanCompassCtx) : null;
      let payload: ArchiveInitBody;
      let toastDirection = "neutral";
      let toastTheme = "";

      if (scanCompassCtx && expectedInfo?.boundToL1) {
        const card = await resolveCompassCard(scanCompassCtx);
        const directionBias: "long" | "short" | "pair" | "neutral" =
          expectedInfo.expected ?? scanCompassCtx.direction;

        const rawPrimaryTheme =
          (card?.title || "").trim() ||
          (scanCompassCtx.direction_title || "").trim() ||
          (card?.primary_driver?.dimension || "").trim();
        const fallbackPoolLabel =
          row.market && !["adhoc", "custom"].includes(row.market.toLowerCase())
            ? getUniverseLabel(row.market, universes[row.market]?.description)
            : "";
        const primaryTheme = rawPrimaryTheme || fallbackPoolLabel || "Layer 1 战略主题";
        const secondaryDriver = formatCompassSecondaryDriver(card);

        const dailyBrief = `日线初筛：${row.daily_structure || "—"}（${row.signal_tier || row.action_bias || "待观察"}）`;
        const thesisParts: string[] = [];
        if (card?.thesis?.trim()) {
          thesisParts.push(card.thesis.trim());
        } else {
          thesisParts.push(`Layer 1 战略主题「${primaryTheme}」（方向：${formatDirectionBiasZh(directionBias)}）`);
        }
        const valueChainFocus = (card?.value_chain_focus || scanCompassCtx.value_chain_focus || "").trim();
        if (valueChainFocus) {
          thesisParts.push(`产业链切入：${valueChainFocus}`);
        }
        thesisParts.push(dailyBrief);

        const rawConviction = (card?.conviction || "").trim().toLowerCase();
        const confidence =
          rawConviction === "high" || rawConviction === "medium" || rawConviction === "low"
            ? rawConviction
            : undefined;

        payload = {
          symbol: sym,
          name: getSymbolName(sym) || undefined,
          direction_bias: directionBias,
          primary_driver: primaryTheme,
          layer1_primary: primaryTheme,
          ...(secondaryDriver
            ? { secondary_driver: secondaryDriver, layer1_secondary: secondaryDriver }
            : {}),
          thesis_summary: thesisParts.join("｜"),
          ...(confidence ? { confidence } : {}),
        };
        toastDirection = directionBias;
        toastTheme = primaryTheme;
      } else {
        /* Independent manual scan: keep strictly isolated from any unrelated Layer 1 card */
        const poolLabel = row.market
          ? getUniverseLabel(row.market, universes[row.market]?.description)
          : "";
        payload = {
          symbol: sym,
          name: getSymbolName(sym) || undefined,
          primary_driver: poolLabel || undefined,
          thesis_summary: `自扫描台收入观察池（日线结构：${row.daily_structure || "—"}，信号：${row.signal_tier || row.action_bias || "待观察"}）`,
        };
        toastDirection = "neutral";
        toastTheme = poolLabel || "观察池";
      }

      const initRes = (await api.archiveInit(payload)) as { status?: string } | null;
      setArchivedSyms((prev) => new Set(prev).add(sym));
      setArchiveToast({
        symbol: sym,
        directionBias: toastDirection,
        theme: toastTheme,
        existed: initRes?.status === "exists",
      });
      onArchiveMutated?.();
    } catch (ex) {
      if (ex instanceof ApiError && ex.status === 409) {
        setArchivedSyms((prev) => new Set(prev).add(sym));
        setArchiveToast({
          symbol: sym,
          directionBias: "neutral",
          theme: "已存在本地档案",
          existed: true,
        });
      } else {
        setErr(`入池建档失败：${errorMessage(ex)}`);
      }
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

  /* Warm full Layer 1 direction card metadata for the active Compass-guided scan */
  useEffect(() => {
    if (!activeCompassContext) return;
    const ctrl = new AbortController();
    void resolveCompassCard(activeCompassContext, ctrl.signal);
    return () => ctrl.abort();
  }, [activeCompassContext, resolveCompassCard]);

  /* Auto-dismiss archive creation toast after 4.5 seconds */
  useEffect(() => {
    if (!archiveToast) return;
    const timer = window.setTimeout(() => setArchiveToast(null), 4500);
    return () => window.clearTimeout(timer);
  }, [archiveToast]);

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
    let alignedSignalCount = 0;
    let alignedMomentumCount = 0;
    let counterCount = 0;
    let poolUnverifiedCount = 0;
    let degradedCount = 0;
    for (const r of scanRows) {
      if (activeCompassContext) {
        const res = classifyRowWithCompass(r, activeCompassContext);
        if (res.status === "aligned") {
          alignedCount += 1;
          if (res.alignKind === "momentum") alignedMomentumCount += 1;
          else alignedSignalCount += 1;
        } else if (res.status === "counter") counterCount += 1;
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
      alignedSignal: alignedSignalCount,
      alignedMomentum: alignedMomentumCount,
      counter: counterCount,
      pool_unverified: poolUnverifiedCount,
      degraded: degradedCount,
    };
  }, [scanRows, activeCompassContext]);

  /* Bidirectional L2 -> L1 scan summary badge sync */
  useEffect(() => {
    const dirId = activeCompassContext?.direction_id?.trim();
    if (!dirId || scanRows.length === 0) return;
    setL2CardSummaries((prev) => ({
      ...prev,
      [dirId]: {
        aligned: filterCounts.aligned,
        watch: filterCounts.watch,
        total: filterCounts.all,
      },
    }));
  }, [activeCompassContext?.direction_id, filterCounts.aligned, filterCounts.watch, filterCounts.all, scanRows.length]);

  /* Close Layer 2 history popover on outside click / Escape */
  useEffect(() => {
    if (!historyOpen) return;
    const onPointerDown = (ev: MouseEvent) => {
      if (historyMenuRef.current && !historyMenuRef.current.contains(ev.target as Node)) {
        setHistoryOpen(false);
      }
    };
    const onKeyDown = (ev: globalThis.KeyboardEvent) => {
      if (ev.key === "Escape") setHistoryOpen(false);
    };
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [historyOpen]);

  const handleSelectCardForL2 = useCallback(
    (card: CompassDirectionCard, runId?: string) => {
      setSelectedL1Track({ card, runId });
      setL2Mode("l1_track");
      setShowPriorScanForMismatch(false);

      // Automatically surface matching L2 scan history for this L1 track if one already exists
      const exactMatch = tasks.find((t) => {
        const ctx = extractCompassContext(t);
        if (!ctx || ctx.direction_id !== card.id) return false;
        return runId ? ctx.run_id === runId : true;
      });
      const fallbackMatch =
        exactMatch ??
        tasks.find((t) => {
          const ctx = extractCompassContext(t);
          return Boolean(ctx && ctx.direction_id === card.id);
        });
      if (fallbackMatch) {
        setActiveTaskId(fallbackMatch.id);
      }

      window.setTimeout(() => {
        const el = layer2Ref.current;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        // Only scroll gently if Layer 2 picker is below the visible viewport fold
        if (rect.top > window.innerHeight - 180 || rect.top < 0) {
          el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }, 40);
    },
    [tasks],
  );

  const handleL2TabKeyDown = (e: KeyboardEvent<HTMLButtonElement>, current: "l1_track" | "manual") => {
    const order: ("l1_track" | "manual")[] = ["l1_track", "manual"];
    const idx = order.indexOf(current);
    let next: "l1_track" | "manual" | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") next = order[(idx + 1) % order.length];
    else if (e.key === "Home") next = order[0];
    else if (e.key === "End") next = order[1];
    if (!next) return;
    e.preventDefault();
    setL2Mode(next);
    l2TabRefs.current[next]?.focus();
  };

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
    const base =
      rowFilter === "all"
        ? scanRows
        : activeCompassContext
          ? scanRows.filter((r) => classifyRowWithCompass(r, activeCompassContext).status === rowFilter)
          : scanRows.filter((r) => classifyScanRow(r) === rowFilter);

    if (activeCompassContext) {
      const ALIGN_RANK: Record<CompassRowAlignment, number> = {
        aligned: 0,
        watch: 1,
        pool_unverified: 2,
        counter: 3,
        degraded: 4,
      };
      return [...base].sort((a, b) => {
        const ca = classifyRowWithCompass(a, activeCompassContext);
        const cb = classifyRowWithCompass(b, activeCompassContext);
        const alignDiff = ALIGN_RANK[ca.status] - ALIGN_RANK[cb.status];
        if (alignDiff !== 0) return alignDiff;

        const tierDiff = signalTierRank(a) - signalTierRank(b);
        if (tierDiff !== 0) return tierDiff;

        const chaseDiff = (ca.chaseWarning ? 1 : 0) - (cb.chaseWarning ? 1 : 0);
        if (chaseDiff !== 0) return chaseDiff;

        const dirA = ca.expected ?? (ca.actual === "short" ? "short" : "long");
        const dirB = cb.expected ?? (cb.actual === "short" ? "short" : "long");
        const r20Diff =
          directionalReturnScore(b["ret_20d_%"], dirB) -
          directionalReturnScore(a["ret_20d_%"], dirA);
        if (r20Diff !== 0) return r20Diff;

        return (
          directionalReturnScore(b["ret_60d_%"], dirB) -
          directionalReturnScore(a["ret_60d_%"], dirA)
        );
      });
    }

    if (sortMode === "tier") {
      return [...base].sort((a, b) => {
        const tierDiff = signalTierRank(a) - signalTierRank(b);
        if (tierDiff !== 0) return tierDiff;
        const dirA = classifyScanRow(a) === "short" ? "short" : "long";
        const dirB = classifyScanRow(b) === "short" ? "short" : "long";
        const chaseA = dirA === "short" && detectShortChaseWarning(a) ? 1 : 0;
        const chaseB = dirB === "short" && detectShortChaseWarning(b) ? 1 : 0;
        if (chaseA !== chaseB) return chaseA - chaseB;
        const r20Diff =
          directionalReturnScore(b["ret_20d_%"], dirB) -
          directionalReturnScore(a["ret_20d_%"], dirA);
        if (r20Diff !== 0) return r20Diff;
        return (
          directionalReturnScore(b["ret_60d_%"], dirB) -
          directionalReturnScore(a["ret_60d_%"], dirA)
        );
      });
    }

    return base;
  }, [scanRows, rowFilter, activeCompassContext, sortMode]);

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
        activeL2CardId={selectedL1Track?.card.id ?? null}
        l2ScanSummaries={l2CardSummaries}
        openCardDetailSignal={openCardDetailSignal}
        logCardSignal={logCardSignal}
        onSelectCardForL2={handleSelectCardForL2}
        onShadowStateChange={setShadowState}
        onJumpWarRoom={onJumpWarRoom}
      />

      {/* Layer 2 Section Divider & Daily Chanlun Screening (100% Full Width) */}
      <div ref={layer2Ref} className="flex flex-col gap-3 scroll-mt-16">
        <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-hairline-soft pb-1 pt-1">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-1.5">
              <span className="inline-flex items-center rounded border border-hairline bg-elevated px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-2">
                Layer 2
              </span>
              <h2 className="m-0 text-sm font-semibold text-ink">日线缠论过筛台</h2>
            </div>

            <div className="mwg-tablist !border-b-0" role="tablist" aria-label="第二层日线扫描模式">
              <button
                ref={(el) => {
                  l2TabRefs.current.l1_track = el;
                }}
                type="button"
                role="tab"
                id={`${l2TabBaseId}-tab-l1`}
                aria-selected={l2Mode === "l1_track"}
                tabIndex={l2Mode === "l1_track" ? 0 : -1}
                className="mwg-tab"
                onClick={() => setL2Mode("l1_track")}
                onKeyDown={(e) => handleL2TabKeyDown(e, "l1_track")}
              >
                <Compass className="h-3.5 w-3.5" aria-hidden="true" />
                <span>按 L1 轨道过筛</span>
                {selectedL1Track ? (
                  <span className="max-w-[140px] truncate rounded bg-accent/14 px-1.5 py-px text-[10px] font-medium text-accent">
                    {selectedL1Track.card.title}
                  </span>
                ) : null}
              </button>

              <button
                ref={(el) => {
                  l2TabRefs.current.manual = el;
                }}
                type="button"
                role="tab"
                id={`${l2TabBaseId}-tab-manual`}
                aria-selected={l2Mode === "manual"}
                tabIndex={l2Mode === "manual" ? 0 : -1}
                className="mwg-tab"
                onClick={() => {
                  setL2Mode("manual");
                  setPickerExpanded(true);
                }}
                onKeyDown={(e) => handleL2TabKeyDown(e, "manual")}
              >
                <Layers className="h-3.5 w-3.5" aria-hidden="true" />
                <span>手动自由选池</span>
              </button>
            </div>
          </div>

          {/* Compact History Dropdown Popover (replaces 248-292px right sidebar so L2 table gets 100% width) */}
          <div ref={historyMenuRef} className="relative">
            <button
              type="button"
              className="btn btn-sm"
              aria-expanded={historyOpen}
              aria-haspopup="menu"
              onClick={() => setHistoryOpen((v) => !v)}
            >
              <History className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
              <span>近期扫描记录 ({tasks.length})</span>
              <ChevronDown className="h-3 w-3 text-muted" aria-hidden="true" />
            </button>
            {historyOpen ? (
              <div
                role="menu"
                aria-label="近期扫描记录列表"
                className="absolute right-0 top-full z-40 mt-1.5 w-[320px] max-w-[92vw] overflow-hidden rounded-lg border border-hairline bg-surface shadow-xl"
              >
                <div className="flex items-center justify-between border-b border-hairline-soft bg-sunken/70 px-3 py-2">
                  <span className="text-[12px] font-semibold text-ink">历史 L2 扫描记录</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm !min-h-[22px] !px-1.5"
                    onClick={() => void loadTasks()}
                    aria-label="刷新记录"
                  >
                    <RefreshCw className="h-3 w-3" aria-hidden="true" />
                  </button>
                </div>
                <ul className="m-0 max-h-[340px] list-none divide-y divide-hairline-soft overflow-y-auto p-0">
                  {tasks.length === 0 ? (
                    <li className="p-4 text-center text-xs text-muted">暂无本轮扫描历史</li>
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
                            role="menuitem"
                            onClick={() => {
                              setActiveTaskId(t.id);
                              setHistoryOpen(false);
                            }}
                            className={`flex w-full cursor-pointer flex-col gap-0.5 px-3 py-2 text-left text-xs ${
                              selected ? "bg-elevated text-ink" : "text-ink-2 hover:bg-elevated/60"
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate font-medium text-ink">{describeScanTaskScope(t)}</span>
                              <Icon className={`h-3.5 w-3.5 shrink-0 ${iconCls}`} aria-hidden="true" />
                            </div>
                            <div className="flex items-center justify-between font-mono text-[10px] text-muted">
                              <span>{shortTime(t.created_at)} UTC</span>
                              <span>#{t.id.slice(0, 6)}</span>
                            </div>
                          </button>
                        </li>
                      );
                    })
                  )}
                </ul>
              </div>
            ) : null}
          </div>
        </div>

      <div className="flex min-w-0 flex-col gap-3.5">
        {/* Tab Panel A: Inline L1-Track Two-Stage Leader Picker */}
        <div
          role="tabpanel"
          aria-labelledby={`${l2TabBaseId}-tab-l1`}
          hidden={l2Mode !== "l1_track"}
        >
          {selectedL1Track ? (
            <Layer2InlinePicker
              card={selectedL1Track.card}
              runId={selectedL1Track.runId}
              universes={universes}
              scanBusy={launching}
              isLoggedInShadow={
                Boolean(selectedL1Track.runId) &&
                selectedL1Track.runId === shadowState.runId &&
                shadowState.loggedCardIds.includes(selectedL1Track.card.id)
              }
              loggingShadow={
                Boolean(selectedL1Track.runId) &&
                selectedL1Track.runId === shadowState.runId &&
                shadowState.loggingCardId === selectedL1Track.card.id
              }
              onLogShadow={
                selectedL1Track.runId && !selectedL1Track.card.id.startsWith("headwind_short_")
                  ? () =>
                      setLogCardSignal((prev) => ({
                        cardId: selectedL1Track.card.id,
                        seq: (prev?.seq ?? 0) + 1,
                      }))
                  : undefined
              }
              onOpenCardDetail={() =>
                setOpenCardDetailSignal((prev) => ({
                  card: selectedL1Track.card,
                  seq: (prev?.seq ?? 0) + 1,
                }))
              }
              onClearTrack={() => setSelectedL1Track(null)}
              onLaunchScan={handleLaunchCompassScan}
              onUniverseSaved={() => void loadUniverses()}
            />
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed border-hairline bg-sunken/40 px-4 py-3 text-xs text-muted">
              <span>
                在上方第一层任意方向卡片点击 <strong className="text-ink">「选龙头过筛」</strong>，此处将就地展开两段式（基准参照锚 + 候选真龙头槽）日线联立验证台。
              </span>
              <button
                type="button"
                className="text-accent hover:underline"
                onClick={() => {
                  setL2Mode("manual");
                  setPickerExpanded(true);
                }}
              >
                或切换到「手动自由选池」→
              </button>
            </div>
          )}
        </div>

        {/* Tab Panel B: Manual Free Universe / Symbol Picker */}
        <div
          role="tabpanel"
          aria-labelledby={`${l2TabBaseId}-tab-manual`}
          hidden={l2Mode !== "manual"}
        >
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
        </div>

          {/* Results & Live Progress Card */}
          {(() => {
            const isMismatchedWithSelectedTrack =
              l2Mode === "l1_track" &&
              Boolean(selectedL1Track) &&
              (!activeCompassContext || activeCompassContext.direction_id !== selectedL1Track!.card.id);
            const hideUnrelatedTable = isMismatchedWithSelectedTrack && !showPriorScanForMismatch;

            if (hideUnrelatedTable && selectedL1Track) {
              const rawSyms = Array.isArray(activeTask?.params?.symbols)
                ? (activeTask.params.symbols as string[])
                : [];
              const priorLabel =
                activeCompassContext?.direction_title ||
                (rawSyms.length > 0 ? rawSyms.slice(0, 3).join(", ") : "上一次扫描");
              return (
                <section className="card flex flex-col gap-3 p-4" aria-label="当前轨道尚未过筛">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-semibold text-ink">日线缠论过筛结果</span>
                      <span className="rounded bg-accent/12 px-2 py-0.5 text-[11px] font-medium text-accent">
                        {selectedL1Track.card.title}
                      </span>
                    </div>
                    {activeTask && scanRows.length > 0 ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm !min-h-[26px] !text-[11px]"
                        onClick={() => setShowPriorScanForMismatch(true)}
                      >
                        <History className="h-3.5 w-3.5" aria-hidden="true" />
                        <span>
                          展开上一条无关扫描：{priorLabel} ({scanRows.length} 只)
                        </span>
                      </button>
                    ) : null}
                  </div>
                  <div className="flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-hairline bg-sunken/40 py-7 text-center text-[12px] text-muted">
                    <p className="font-medium text-ink">
                      当前 L1 轨道「{selectedL1Track.card.title}」尚未运行日线缠论过筛
                    </p>
                    <p>请在上方选入 1~3 只细分真龙头（或勾选基准参照锚），点击「启动 L2 日线过筛」查看日线中枢与同向结构。</p>
                  </div>
                </section>
              );
            }

            return (
          <section className="card flex flex-col gap-3 p-4" aria-label="扫描结果与实时进度">
            {isMismatchedWithSelectedTrack && selectedL1Track ? (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-[11.5px] text-warning">
                <span>
                  提示：下方展示的是上一条历史扫描，尚未针对当前轨道「{selectedL1Track.card.title}」运行过筛。
                </span>
                <button
                  type="button"
                  className="text-[11px] font-medium underline"
                  onClick={() => setShowPriorScanForMismatch(false)}
                >
                  收起无关历史
                </button>
              </div>
            ) : null}
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
                        title={
                          filterCounts.aligned > 0
                            ? `确认买卖点 ${filterCounts.alignedSignal} 只 · 趋势推进 ${filterCounts.alignedMomentum} 只`
                            : undefined
                        }
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
                  ) : activeTask?.status === "completed" &&
                    filterCounts.all > 0 &&
                    filterCounts.aligned === 0 &&
                    activeCompassContext.direction_id &&
                    !activeCompassContext.direction_id.startsWith("headwind_short_") &&
                    activeCompassContext.run_id &&
                    activeCompassContext.run_id === shadowState.runId ? (
                    <button
                      type="button"
                      className="btn btn-sm !min-h-[24px] !px-2 !text-[11px] border-warning/45 bg-warning/10 text-warning hover:bg-warning/20"
                      disabled={shadowState.loggingCardId === activeCompassContext.direction_id}
                      onClick={() =>
                        setLogCardSignal((prev) => ({
                          cardId: activeCompassContext.direction_id!,
                          seq: (prev?.seq ?? 0) + 1,
                        }))
                      }
                    >
                      <BookmarkPlus className="h-3 w-3" aria-hidden="true" />
                      <span>
                        {shadowState.loggedCardIds.includes(activeCompassContext.direction_id)
                          ? "0通过 · 已入 Track C 影子账本"
                          : "0只同向通过 · 记入 Track C 放弃归因"}
                      </span>
                    </button>
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
              <div
                tabIndex={0}
                role="region"
                aria-label="板块龙头日线缠论扫描结果表"
                className={`table-wrapper ${filteredScanRows.length > 30 ? "max-h-[78dvh]" : ""}`}
              >
                <table className="data-table">
                  <thead style={{ zIndex: 3 }}>
                    <tr>
                      <th scope="col" style={{ paddingInline: 8 }}>
                        标的
                      </th>
                      <th scope="col" className="num" style={{ paddingInline: 6 }}>
                        最新收盘
                      </th>
                      <th scope="col" className="num" style={{ paddingInline: 6 }}>
                        20D
                      </th>
                      <th scope="col" style={{ paddingInline: 6 }}>
                        日线结构 & 中枢
                      </th>
                      <th scope="col" className="num" style={{ paddingInline: 6 }}>
                        中枢 [ZD, ZG]
                      </th>
                      <th scope="col" style={{ paddingInline: 6 }}>
                        信号与方向建议
                      </th>
                      <th
                        scope="col"
                        className="num sticky right-0 z-[2] bg-sunken shadow-[-1px_0_0_var(--hairline)]"
                        style={{ paddingInline: 8 }}
                      >
                        操作
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredScanRows.map((row) => {
                      const symUpper = row.symbol.toUpperCase();
                      const isArchived = archivedSyms.has(symUpper);
                      const zhName = getSymbolName(row.symbol);
                      const marketLabel = row.market
                        ? getUniverseLabel(row.market, universes[row.market]?.description)
                        : null;
                      const fullSymTitle = [
                        row.symbol,
                        zhName || null,
                        marketLabel ? `(${marketLabel})` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ");
                      const compassAlign = activeCompassContext
                        ? classifyRowWithCompass(row, activeCompassContext)
                        : null;
                      const chaseWarning = compassAlign
                        ? compassAlign.chaseWarning
                        : classifyScanRow(row) === "short"
                          ? detectShortChaseWarning(row)
                          : null;
                      const signalBadge = resolveSignalBadge(row);
                      const structureSubline = [
                        `${row.daily_zs_count ?? 0}${row.daily_zs_level === "segment" ? "段" : "笔"}枢`,
                        fmtPosVsZs(row.pos_vs_zs),
                        fmtFractalText(row.daily_fractal),
                        row.daily_momentum ? fmtMomentumVerdict(row.daily_momentum) : "",
                      ]
                        .filter(Boolean)
                        .join(" · ");
                      const rawSignalsTitle =
                        Array.isArray(row.signals) && row.signals.length > 0
                          ? row.signals.join("\n")
                          : undefined;

                      return (
                        <tr
                          key={`${row.market}-${row.symbol}`}
                          className={`clickable ${
                            compassAlign?.status === "aligned" ? "bg-bull/[0.03]" : ""
                          }`}
                          onClick={() => onJumpWarRoom(row.symbol)}
                        >
                          <th scope="row" style={{ paddingInline: 8 }}>
                            <div
                              className="flex w-full max-w-[144px] flex-col gap-0.5 overflow-hidden"
                              title={fullSymTitle}
                            >
                              <div className="flex min-w-0 items-center gap-1">
                                <span className="shrink-0 rounded border border-hairline-soft bg-sunken px-1.5 py-0.2 font-mono text-[11px] font-semibold text-ink">
                                  {row.symbol}
                                </span>
                                {marketLabel ? (
                                  <span className="truncate text-[10px] font-normal text-muted">
                                    {marketLabel}
                                  </span>
                                ) : null}
                              </div>
                              {zhName ? (
                                <span className="truncate text-[11.5px] font-medium text-ink-2">
                                  {zhName}
                                </span>
                              ) : null}
                            </div>
                          </th>
                          {row.error ? (
                            <td colSpan={6} data-label="异常" className="text-critical" style={{ paddingInline: 8 }}>
                              行情错误：{row.error}
                            </td>
                          ) : (
                            <>
                              <td
                                data-label="最新收盘"
                                className="num"
                                style={{ paddingInline: 6 }}
                                title={row.as_of_bar ? `行情截至 ${row.as_of_bar}` : undefined}
                              >
                                <div className="text-[12.5px] font-medium text-ink">{fmtPrice(row.close)}</div>
                                {row.as_of_bar ? (
                                  <div className="font-mono text-[10px] font-normal text-muted">
                                    {shortBarDate(row.as_of_bar)}
                                    {row.bar_is_closed === false ? "·盘中" : ""}
                                    {row.cache_status === "hit" ? "·本地" : ""}
                                  </div>
                                ) : null}
                              </td>
                              <td
                                data-label="20D 涨跌"
                                className={`num font-semibold ${signedToneClass(row["ret_20d_%"])}`}
                                style={{ paddingInline: 6 }}
                                title={
                                  typeof row["ret_60d_%"] === "number"
                                    ? `20D: ${fmtPct(row["ret_20d_%"], 2, true)} · 60D: ${fmtPct(row["ret_60d_%"], 2, true)}`
                                    : undefined
                                }
                              >
                                {fmtPct(row["ret_20d_%"], 1, true)}
                              </td>
                              <td data-label="日线结构 & 中枢" style={{ paddingInline: 6 }}>
                                <div className="max-w-[168px]" title={`${row.daily_structure || "—"} | ${structureSubline}`}>
                                  <div className="truncate text-xs text-ink">{row.daily_structure || "—"}</div>
                                  <div className="truncate text-[10.5px] text-muted">{structureSubline}</div>
                                </div>
                              </td>
                              <td data-label="中枢 [ZD, ZG]" className="num text-[11px]" style={{ paddingInline: 6 }}>
                                <ZhongshuMiniRuler
                                  last={row.close}
                                  zd={row.daily_zs?.[0]}
                                  zg={row.daily_zs?.[1]}
                                />
                              </td>
                              <td data-label="信号与方向建议" style={{ paddingInline: 6 }}>
                                <div className="flex min-w-[155px] flex-col gap-1" title={rawSignalsTitle}>
                                  <div className="flex flex-wrap items-center gap-1">
                                    {compassAlign ? (
                                      <StatusBadge
                                        tone={
                                          compassAlign.status === "aligned"
                                            ? compassAlign.alignKind === "momentum"
                                              ? compassAlign.chaseWarning
                                                ? "warning"
                                                : "info"
                                              : "good"
                                            : compassAlign.status === "counter"
                                              ? "critical"
                                              : compassAlign.status === "degraded"
                                                ? "warning"
                                                : "neutral"
                                        }
                                        title={
                                          compassAlign.status === "aligned" && compassAlign.alignKind === "momentum"
                                            ? compassAlign.chaseWarning
                                              ? `日线已顺向进入资金一致推进，但${compassAlign.chaseWarning.detail}`
                                              : "日线已顺向进入资金一致推进，去 30min 等回踩/反抽结构精确落地"
                                            : compassAlign.status === "counter" && compassAlign.alignKind === "momentum"
                                              ? "日线处于反向资金一致推进，严禁逆势对抗"
                                              : undefined
                                        }
                                        label={
                                          compassAlign.status === "aligned"
                                            ? compassAlign.alignKind === "momentum"
                                              ? `L1同向·趋势推进(${compassAlign.expected === "short" ? "空" : "多"})`
                                              : `L1同向通过(${compassAlign.expected === "short" ? "空" : "多"})`
                                            : compassAlign.status === "counter"
                                              ? compassAlign.alignKind === "momentum"
                                                ? `L1反向·趋势对抗(${compassAlign.expected === "short" ? "空" : "多"})`
                                                : `L1反向(${compassAlign.expected === "short" ? "空" : "多"})`
                                              : compassAlign.status === "pool_unverified"
                                                ? "池内参考·需核实归属"
                                                : compassAlign.status === "degraded"
                                                  ? "数据不足"
                                                  : `L1待成熟(${compassAlign.expected === "short" ? "空" : compassAlign.expected === "long" ? "多" : "—"})`
                                        }
                                      />
                                    ) : null}
                                    {signalBadge ? (
                                      <StatusBadge tone={signalBadge.tone} label={signalBadge.label} />
                                    ) : null}
                                    {chaseWarning ? (
                                      <StatusBadge
                                        tone="warning"
                                        label={chaseWarning.label}
                                        title={chaseWarning.detail}
                                      />
                                    ) : null}
                                    {row.quality_grade === "caution" ? (
                                      <span
                                        className="inline-flex items-center gap-0.5 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning"
                                        title={(row.quality_notes ?? []).join("；") || "数据源存在轻微平K或包络修正"}
                                      >
                                        <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                                        <span>质量提示</span>
                                      </span>
                                    ) : null}
                                  </div>
                                  {row.action_bias ? (
                                    <span
                                      className="line-clamp-2 text-[11px] leading-snug text-ink-2"
                                      title={
                                        [row.action_bias, (row.quality_notes ?? []).join("；")]
                                          .filter(Boolean)
                                          .join(" | ")
                                      }
                                    >
                                      {row.action_bias}
                                    </span>
                                  ) : null}
                                </div>
                              </td>
                              <td
                                data-label="操作"
                                className="num sticky right-0 z-[1] bg-surface shadow-[-1px_0_0_var(--hairline-soft)]"
                                style={{ paddingInline: 8 }}
                                onClick={(ev) => ev.stopPropagation()}
                              >
                                <div className="inline-flex items-center justify-end gap-1">
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    style={{ paddingInline: 7 }}
                                    title="进入缠论战情室下钻 30min 触发与算仓"
                                    onClick={() => onJumpWarRoom(row.symbol)}
                                  >
                                    <Crosshair className="h-3 w-3 text-accent" aria-hidden="true" />
                                    <span>战情</span>
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    style={{ paddingInline: 7 }}
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
            );
          })()}
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

      {archiveToast ? (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-5 right-5 z-50 flex max-w-md items-start gap-2.5 rounded-lg border border-bull/45 bg-surface px-3.5 py-3 shadow-lg"
        >
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-bull" aria-hidden="true" />
          <div className="min-w-0 flex-1 text-xs">
            <div className="font-semibold text-ink">
              {archiveToast.existed
                ? `${formatSymbolWithCode(archiveToast.symbol)} 已存在于研究档案库`
                : `已将 ${formatSymbolWithCode(archiveToast.symbol)} 入池建档`}
            </div>
            <div className="mt-0.5 text-[11px] text-muted">
              {archiveToast.existed
                ? "可直接打开现有研报档案继续深研或修订"
                : `方向：${formatDirectionBiasZh(archiveToast.directionBias)} · 主题：${archiveToast.theme || "观察池"}`}
            </div>
            <div className="mt-2 flex items-center gap-2">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => {
                  const sym = archiveToast.symbol;
                  setArchiveToast(null);
                  onJumpArchive(sym);
                }}
              >
                <BookOpen className="h-3 w-3" aria-hidden="true" />
                <span>查看研报档案</span>
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm text-muted"
                onClick={() => setArchiveToast(null)}
              >
                稍后
              </button>
            </div>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm px-1 text-muted hover:text-ink"
            aria-label="关闭入池提示"
            onClick={() => setArchiveToast(null)}
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      ) : null}
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
