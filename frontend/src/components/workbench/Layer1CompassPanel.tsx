import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BookmarkCheck,
  BookmarkPlus,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  Clock,
  Compass,
  Crosshair,
  History,
  Layers,
  Loader2,
  RefreshCw,
  Scale,
  ShieldAlert,
  ShieldCheck,
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
  CrossAssetAnchorItem,
  DeepResearchEngine,
  ResearchEnginesResponse,
  TaskEventEnvelope,
  TaskRecord,
} from "../../api/types";
import { ModalDialog } from "../ui/ModalDialog";
import { shortTime } from "../../lib/format";
import { MacroCockpit, signalLightTone } from "./MacroCockpit";
import { Layer1DetailDrawer, type Layer1DrawerState } from "./Layer1DetailDrawer";

type DirectionFilter = "all" | "long" | "short" | "pair";

export interface L2CardScanSummary {
  aligned: number;
  watch: number;
  total: number;
}

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
      return "加密";
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

function isHeadwindShortable(item: CompassAvoidSectorItem): boolean {
  if (typeof item.short_proxy_possible === "boolean") return item.short_proxy_possible;
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

function computeAnchorTemperature(
  card: CompassDirectionCard,
  anchorBySym: Map<string, CrossAssetAnchorItem>,
): { kind: "aligned" | "divergent" | "chase_warn" | "neutral"; label: string; detail: string } | null {
  const anchors = (card.reference_anchors ?? [])
    .map((sym) => anchorBySym.get(sym.toUpperCase()))
    .filter((it): it is CrossAssetAnchorItem => Boolean(it && it.status === "ok" && it.ret_20d_pct !== null));
  if (anchors.length === 0) return null;

  const avg20 = anchors.reduce((sum, it) => sum + (it.ret_20d_pct ?? 0), 0) / anchors.length;
  const broken = anchors.some((it) => it.chanlun_hint?.shield_state === "below_zd_breakdown");
  const lead = anchors[0]!;
  const leadTag =
    anchors.length === 1
      ? `${lead.symbol.replace(/=X$|-USD$|\.SS$/i, "")} ${lead.ret_20d_pct! >= 0 ? "+" : ""}${lead.ret_20d_pct!.toFixed(1)}%`
      : `均值 ${avg20 >= 0 ? "+" : ""}${avg20.toFixed(1)}%`;
  const summary = anchors
    .map((a) => `${a.name}(${a.symbol} 20D ${a.ret_20d_pct! >= 0 ? "+" : ""}${a.ret_20d_pct!.toFixed(1)}%)`)
    .join(" · ");

  if (card.direction === "long") {
    if (avg20 >= 0 && !broken) {
      return { kind: "aligned", label: `${leadTag} · 同向 ✓`, detail: `板块参照锚顺势 · ${summary}` };
    }
    return {
      kind: "divergent",
      label: `${leadTag} · 背离需L2甄别`,
      detail: `参照锚偏弱，切忌盲买宽基，需在 L2 甄别逆势真龙头或观望 · ${summary}`,
    };
  }
  if (card.direction === "short") {
    if (avg20 <= -15) {
      return {
        kind: "chase_warn",
        label: `${leadTag} · ⚠ 防深坑追空`,
        detail: `参照锚 20D 已深跌 ${avg20.toFixed(1)}%，严禁急跌末端盲目追空，务必在 L2 等反抽不过 ZD 的二卖/三卖 · ${summary}`,
      };
    }
    if (avg20 <= 0) {
      return { kind: "aligned", label: `${leadTag} · 空头顺势 ✓`, detail: `板块参照锚偏弱 · ${summary}` };
    }
    return {
      kind: "divergent",
      label: `${leadTag} · 仍在涨禁摸顶`,
      detail: `参照锚仍在上涨，严禁左侧摸顶，等 L2 日线跌破结构 · ${summary}`,
    };
  }
  return { kind: "neutral", label: `配对锚 ${leadTag}`, detail: summary };
}

export interface Layer1CompassPanelProps {
  activeL2CardId?: string | null;
  l2ScanSummaries?: Record<string, L2CardScanSummary>;
  openCardDetailSignal?: { card: CompassDirectionCard; seq: number } | null;
  logCardSignal?: { cardId: string; seq: number } | null;
  onSelectCardForL2: (card: CompassDirectionCard, runId?: string) => void;
  onShadowStateChange?: (state: {
    runId: string;
    loggedCardIds: string[];
    loggingCardId: string | null;
  }) => void;
  onJumpWarRoom: (symbol: string) => void;
}

export function Layer1CompassPanel({
  activeL2CardId = null,
  l2ScanSummaries,
  openCardDetailSignal = null,
  logCardSignal = null,
  onSelectCardForL2,
  onShadowStateChange,
  onJumpWarRoom,
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
  const [highlightFactor, setHighlightFactor] = useState<string | null>(null);

  /* Progressive disclosure non-modal right slide-over drawer */
  const [drawer, setDrawer] = useState<Layer1DrawerState>(null);

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

  /* Track C L1 -> Shadow Opportunity Ledger 1-click logging (hydrated + session map) */
  const [loggingOppKey, setLoggingOppKey] = useState<string | null>(null);
  const [loggedOppMap, setLoggedOppMap] = useState<Record<string, { symbol: string; idempotent?: boolean }>>({});
  const [oppLogFeedback, setOppLogFeedback] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

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
  const lastLogSeqRef = useRef<number>(0);

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

  const attachTaskStream = useCallback(
    (task: TaskRecord) => {
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
        if (esRef.current === es) esRef.current = null;
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
                setOppLogFeedback(null);
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
                /* fall through */
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
    },
    [loadMacro],
  );

  const loadLatest = useCallback(
    async (signal?: AbortSignal) => {
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
    },
    [attachTaskStream],
  );

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

  /* Sync external card-detail request from Layer2InlinePicker */
  useEffect(() => {
    if (openCardDetailSignal?.card) {
      setDrawer({ kind: "card", card: openCardDetailSignal.card });
    }
  }, [openCardDetailSignal]);

  const handleLogCompassOpportunity = useCallback(
    async (kind: "l1_direction_call" | "l1_abstention", cardId?: string) => {
      const runId = currentRun?.run_id;
      if (!runId || loggingOppKey) return;
      const opKey = `${runId}:${cardId || "abstain"}`;
      setLoggingOppKey(opKey);
      setOppLogFeedback(null);
      try {
        const res = await api.logCompassOpportunity(runId, {
          kind,
          ...(cardId ? { card_id: cardId } : {}),
        });
        if (!mountedRef.current) return;
        const sym = res.opportunity?.symbol || "";
        setLoggedOppMap((prev) => ({
          ...prev,
          [opKey]: { symbol: sym, idempotent: Boolean(res.idempotent_replay) },
        }));
        setOppLogFeedback({
          tone: "ok",
          text: res.idempotent_replay
            ? `已存在同条 L1 影子账本记录（${sym}，幂等复用）`
            : `已记入 Track C 影子机会账本：${sym}${res.stop_is_proxy ? "（8% 结构代理止损）" : ""}`,
        });
      } catch (ex) {
        if (!mountedRef.current) return;
        setOppLogFeedback({
          tone: "err",
          text: `记入影子账本失败：${errorMessage(ex)}`,
        });
      } finally {
        if (mountedRef.current) setLoggingOppKey(null);
      }
    },
    [currentRun?.run_id, loggingOppKey],
  );

  /* Sync external Track C log request from Layer2InlinePicker */
  useEffect(() => {
    if (!logCardSignal || logCardSignal.seq === lastLogSeqRef.current) return;
    lastLogSeqRef.current = logCardSignal.seq;
    void handleLogCompassOpportunity("l1_direction_call", logCardSignal.cardId);
  }, [logCardSignal, handleLogCompassOpportunity]);

  const isCardLogged = useCallback(
    (cardId: string): boolean => {
      const runId = currentRun?.run_id;
      if (!runId) return false;
      if (loggedOppMap[`${runId}:${cardId}`]) return true;
      return Boolean(currentRun?.logged_opportunities?.cards?.[cardId]);
    },
    [currentRun, loggedOppMap],
  );

  const isAbstentionLogged = useMemo(() => {
    const runId = currentRun?.run_id;
    if (!runId) return false;
    if (loggedOppMap[`${runId}:abstain`]) return true;
    return Boolean(currentRun?.logged_opportunities?.abstention_id);
  }, [currentRun, loggedOppMap]);

  /* Notify parent WorkbenchView of Track C hydrated state so Layer2InlinePicker reflects persistent status */
  useEffect(() => {
    if (!onShadowStateChange) return;
    const runId = currentRun?.run_id || "";
    const ids = new Set<string>(Object.keys(currentRun?.logged_opportunities?.cards ?? {}));
    for (const k of Object.keys(loggedOppMap)) {
      if (runId && k.startsWith(`${runId}:`)) {
        const cid = k.slice(runId.length + 1);
        if (cid && cid !== "abstain") ids.add(cid);
      }
    }
    const loggingCid =
      loggingOppKey && runId && loggingOppKey.startsWith(`${runId}:`)
        ? loggingOppKey.slice(runId.length + 1)
        : null;
    onShadowStateChange({
      runId,
      loggedCardIds: Array.from(ids),
      loggingCardId: loggingCid,
    });
  }, [currentRun, loggedOppMap, loggingOppKey, onShadowStateChange]);

  const handleSelectHistory = async (runId: string) => {
    if (!runId) return;
    if (historyAbortRef.current) historyAbortRef.current.abort();
    const ctrl = new AbortController();
    historyAbortRef.current = ctrl;
    const reqSeq = ++selectionReqSeqRef.current;
    viewingLatestRef.current = runId === latestRunIdRef.current;
    setSelectedRunId(runId);
    setLoadingRun(true);
    setDrawer(null);
    setHighlightFactor(null);
    try {
      const detail = await api.compassHistoryDetail(runId, ctrl.signal);
      if (!mountedRef.current || ctrl.signal.aborted || selectionReqSeqRef.current !== reqSeq) return;
      setCurrentRun(detail);
      setSelectedRunId(detail.run_id);
      setDirFilter("all");
      setOppLogFeedback(null);
    } catch {
      /* ignore aborted fetches */
    } finally {
      if (mountedRef.current && selectionReqSeqRef.current === reqSeq) setLoadingRun(false);
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

  const result = currentRun?.result ?? null;
  const directions = useMemo(() => result?.directions ?? [], [result]);

  const dirCounts = useMemo(() => {
    const c = { all: directions.length, long: 0, short: 0, pair: 0 };
    for (const d of directions) {
      if (d.direction === "long") c.long += 1;
      else if (d.direction === "short") c.short += 1;
      else if (d.direction === "pair") c.pair += 1;
    }
    return c;
  }, [directions]);

  const visibleDirections = useMemo(() => {
    if (dirFilter === "all") return directions;
    return directions.filter((d) => d.direction === dirFilter);
  }, [directions, dirFilter]);

  /* Anchor map for anchor-temperature comparison */
  const anchorBySym = useMemo(() => {
    const map = new Map<string, CrossAssetAnchorItem>();
    for (const it of macro?.anchors?.items ?? []) {
      map.set(it.symbol.toUpperCase(), it);
    }
    return map;
  }, [macro]);

  /* Factor concentration mapping: cardId -> shared concentrated factor label */
  const concentratedCardFactors = useMemo(() => {
    const map = new Map<string, string[]>();
    const fc = result?.factor_concentration;
    if (!fc?.concentrated) return map;
    for (const item of fc.dominant_factors ?? []) {
      const label = item.label || item.factor_id;
      for (const cid of item.card_ids ?? []) {
        const list = map.get(cid) ?? [];
        list.push(label);
        map.set(cid, list);
      }
    }
    return map;
  }, [result]);

  const concentratedFactors = useMemo(() => {
    const fc = result?.factor_concentration;
    if (!fc?.concentrated) return [];
    return (fc.dominant_factors ?? [])
      .filter((item) => (item.card_ids ?? []).length >= 2)
      .map((item) => ({
        name: item.label || item.factor_id,
        count: item.count || item.card_ids.length,
        ids: item.card_ids,
      }));
  }, [result]);

  const auditRejectedCount = result?.constitutional_audit?.rejected_reasons?.length ?? 0;
  const auditFlaggedCount = result?.constitutional_audit?.flagged_cards_count ?? 0;
  const signalLights = macro?.regime_signal_lights ?? [];

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-hairline bg-surface p-3.5 sm:p-4">
      {/* 1. Compact Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1.5">
            <Compass className="h-4 w-4 text-accent" aria-hidden="true" />
            <h2 className="text-[14px] font-semibold text-ink">第一层 · 宏观战略罗盘</h2>
          </div>
          {history.length > 0 ? (
            <div className="inline-flex items-center gap-1 rounded-md border border-hairline-soft bg-sunken px-2 py-0.5 text-[11px] focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent">
              <History className="h-3 w-3 text-muted" aria-hidden="true" />
              <select
                aria-label="切换历史 L1 推演记录"
                value={selectedRunId}
                onChange={(e) => void handleSelectHistory(e.target.value)}
                disabled={loadingRun}
                className="max-w-[220px] bg-transparent text-[11px] text-ink outline-none"
              >
                {history.map((h) => (
                  <option key={h.run_id} value={h.run_id}>
                    {shortTime(h.created_at_utc)} · {h.directions_count === 0 ? "战略留白" : `${h.directions_count}方向`} · {h.engine}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {(auditRejectedCount > 0 || auditFlaggedCount > 0) && result ? (
            <button
              type="button"
              onClick={() => setDrawer({ kind: "audit" })}
              className="inline-flex items-center gap-1 rounded-md border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] font-medium text-warning transition-colors hover:bg-warning/20"
              title="点击查看宪法护栏拦截与降档明细"
            >
              <ShieldAlert className="h-3 w-3" aria-hidden="true" />
              <span>
                护栏已拦截 {auditRejectedCount} 张{auditFlaggedCount > 0 ? ` · 标旗 ${auditFlaggedCount}` : ""}
              </span>
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            className="btn btn-sm"
            disabled={macroLoading}
            onClick={() => void loadMacro(true)}
            title="刷新免费宏观硬数据（FRED / 跨资产定价锚 / CFTC / Polymarket）"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${macroLoading ? "animate-spin" : ""}`} aria-hidden="true" />
            <span>刷新宏观底座</span>
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={isRunActive}
            onClick={() => {
              setLaunchErr(null);
              setLaunchOpen(true);
            }}
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{isRunActive ? "AI 推演运行中…" : "AI 盲推演新方向"}</span>
          </button>
        </div>
      </div>

      {/* Macro Fetch Error Banner */}
      {macroErr ? (
        <div className="rounded-md border border-bear/35 bg-bear/8 px-3 py-1.5 text-[11.5px] text-bear">
          宏观底座加载异常：{macroErr}
        </div>
      ) : null}

      {/* Active SSE Progress Strip */}
      {activeRun ? (
        <div className="rounded-lg border border-accent/35 bg-accent/[0.04] px-3 py-2.5 text-[11.5px]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {isRunActive ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" aria-hidden="true" />
              ) : activeRun.status === "completed" ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-bull" aria-hidden="true" />
              ) : (
                <XCircle className="h-3.5 w-3.5 text-bear" aria-hidden="true" />
              )}
              <span className="font-semibold text-ink">
                {isRunActive
                  ? "正在执行 L1 去锚定盲推演…"
                  : activeRun.status === "completed"
                    ? "L1 推演已完成"
                    : activeRun.error || "推演已结束"}
              </span>
              <span className="inline-flex items-center gap-1 font-mono text-[10.5px] text-muted">
                <Clock className="h-3 w-3" aria-hidden="true" />
                {formatElapsed((activeRun.finishedAtMs ?? nowMs) - activeRun.startedAtMs)} · {activeRun.engine}/{activeRun.effort}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {isRunActive ? (
                <button
                  type="button"
                  className="btn btn-sm !min-h-[24px] !px-2 text-bear"
                  disabled={cancelling}
                  onClick={() => void handleCancelCompass()}
                >
                  <Square className="h-3 w-3" aria-hidden="true" />
                  <span>终止</span>
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm !min-h-[24px] !px-2"
                  onClick={() => setActiveRun(null)}
                >
                  收起
                </button>
              )}
            </div>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-4">
            {COMPASS_STAGES.map((stg, idx) => {
              const cur = stageIndex(activeRun.stage, activeRun.done, activeRun.status);
              const done = idx < cur;
              const active = idx === cur && isRunActive;
              return (
                <div
                  key={stg.key}
                  className={`rounded border px-2 py-1 text-[10.5px] ${
                    done
                      ? "border-bull/35 bg-bull/10 text-bull"
                      : active
                        ? "border-accent/45 bg-accent/12 font-medium text-accent"
                        : "border-hairline-soft bg-surface/60 text-muted"
                  }`}
                >
                  {idx + 1}. {stg.label}
                </div>
              );
            })}
          </div>
          {activeRun.logs.length > 0 ? (
            <div className="mt-1.5 truncate font-mono text-[10.5px] text-muted">
              最新：{activeRun.logs[activeRun.logs.length - 1].message}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* 2. Dual-Pane Visual Macro Cockpit */}
      <MacroCockpit macro={macro} loading={macroLoading} onJumpWarRoom={onJumpWarRoom} />

      {/* 3. Deterministic 4-Pillar Regime Strip + Regime Summary + Clickable Avoid Sectors */}
      {(signalLights.length > 0 || result) ? (
        <div className="flex flex-col gap-2 rounded-lg border border-hairline-soft bg-sunken/60 px-3 py-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            {/* 4-Pillar Deterministic Signal Lights */}
            <div className="flex flex-wrap items-center gap-1.5">
              {signalLights.map((light) => {
                const tone = signalLightTone(light.level);
                const Icon = tone.Icon;
                const clickable = Boolean(result);
                if (!clickable) {
                  return (
                    <span
                      key={light.id}
                      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${tone.badgeBg}`}
                      title={`${light.label}: ${light.tag} (${light.metric})`}
                    >
                      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
                      <span className="font-medium text-ink">{light.label}</span>
                      <span className="font-semibold">{light.tag}</span>
                      <span className="hidden font-mono text-[10px] opacity-80 sm:inline">{light.metric}</span>
                    </span>
                  );
                }
                return (
                  <button
                    key={light.id}
                    type="button"
                    onClick={() => setDrawer({ kind: "macro_pillars" })}
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] transition-opacity hover:opacity-90 ${tone.badgeBg}`}
                    title={`${light.label}: ${light.tag} (${light.metric}) · 点击查看宏观四维研判`}
                  >
                    <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
                    <span className="font-medium text-ink">{light.label}</span>
                    <span className="font-semibold">{light.tag}</span>
                    <span className="hidden font-mono text-[10px] opacity-80 sm:inline">{light.metric}</span>
                  </button>
                );
              })}
            </div>

            {result && !result.regime_summary ? (
              <button
                type="button"
                onClick={() => setDrawer({ kind: "macro_pillars" })}
                className="inline-flex items-center gap-1 text-[11px] font-medium text-accent hover:underline"
              >
                <Layers className="h-3.5 w-3.5" aria-hidden="true" />
                <span>四维全文</span>
                <ChevronRight className="h-3 w-3" aria-hidden="true" />
              </button>
            ) : null}
          </div>

          {result?.regime_summary ? (
            <button
              type="button"
              onClick={() => setDrawer({ kind: "macro_pillars" })}
              className="group flex w-full items-center justify-between gap-2 rounded-md px-1 py-0.5 text-left transition-colors hover:bg-surface/80"
              title={`${result.regime_summary} · 点击展开四维全文与风险提示`}
            >
              <span className="line-clamp-1 min-w-0 flex-1 text-[12px] text-ink-2">
                <span className="mr-1.5 font-semibold text-ink">本轮定调：</span>
                {result.regime_summary}
              </span>
              <span className="inline-flex shrink-0 items-center gap-0.5 text-[11px] font-medium text-accent group-hover:underline">
                <span>四维全文</span>
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              </span>
            </button>
          ) : null}

          {/* Compact Avoid Sectors Chip Bar */}
          {result?.avoid_sectors && result.avoid_sectors.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5 border-t border-hairline-soft pt-2">
              <span className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-bear">
                <XCircle className="h-3 w-3" aria-hidden="true" />
                逆风规避：
              </span>
              {result.avoid_sectors.map((item, idx) => {
                const shortable = isHeadwindShortable(item);
                return (
                  <button
                    key={`${item.sector}-${idx}`}
                    type="button"
                    onClick={() => setDrawer({ kind: "avoid", item, index: idx })}
                    className="inline-flex items-center gap-1 rounded-md border border-bear/30 bg-bear/6 px-2 py-0.5 text-[11px] text-ink transition-colors hover:border-bear/60 hover:bg-bear/12"
                    title={`${item.reason} · 点击查看规避详情${shortable ? "或衍生做空" : ""}`}
                  >
                    <span>{item.sector}</span>
                    {shortable ? (
                      <span className="rounded bg-bear/16 px-1 py-px text-[11px] font-semibold text-bear">
                        可空
                      </span>
                    ) : null}
                    <ChevronRight className="h-3 w-3 text-muted" aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Track C Feedback Toast Banner */}
      {oppLogFeedback ? (
        <div
          className={`flex items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-[11.5px] ${
            oppLogFeedback.tone === "ok"
              ? "border-bull/35 bg-bull/10 text-bull"
              : "border-bear/35 bg-bear/10 text-bear"
          }`}
        >
          <span>{oppLogFeedback.text}</span>
          <button
            type="button"
            className="text-[10.5px] underline opacity-80 hover:opacity-100"
            onClick={() => setOppLogFeedback(null)}
          >
            关闭
          </button>
        </div>
      ) : null}

      {/* 4. Article 4 Strategic Abstention Hero Card (when abstained === true) */}
      {result?.abstained ? (
        <div className="rounded-xl border-2 border-warning/50 bg-warning/[0.06] p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1 rounded-full border border-warning/50 bg-warning/16 px-2.5 py-0.5 text-[11px] font-semibold text-warning">
                  <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  宪法第 4 条 · 本轮战略留白（全市场观望）
                </span>
                {isAbstentionLogged ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-bull/14 px-2 py-0.5 text-[10.5px] font-semibold text-bull">
                    <BookmarkCheck className="h-3 w-3" aria-hidden="true" />
                    已入 Track C 影子账本
                  </span>
                ) : null}
              </div>
              <h3 className="text-[15px] font-semibold text-ink">
                没有极度清晰的不对称赔率，坚决不硬凑机会卡片
              </h3>
              <p className="max-w-3xl text-[12.5px] leading-relaxed text-ink-2">
                {result.abstain_reason || result.regime_summary || "当前风险收益比不足，保持现金与耐心等待。"}
              </p>
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm shrink-0"
              disabled={loggingOppKey === `${currentRun?.run_id}:abstain`}
              onClick={() => void handleLogCompassOpportunity("l1_abstention")}
            >
              {isAbstentionLogged ? (
                <>
                  <BookmarkCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>已记录战略留白</span>
                </>
              ) : (
                <>
                  <BookmarkPlus className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>记入 Track C 留白纪律账本</span>
                </>
              )}
            </button>
          </div>
        </div>
      ) : null}

      {/* 5. Direction Filter + Factor Concentration Cross-Highlight Bar */}
      {directions.length > 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {(
              [
                { id: "all", label: `全部 (${dirCounts.all})` },
                { id: "long", label: `做多 (${dirCounts.long})` },
                { id: "short", label: `做空 (${dirCounts.short})` },
                { id: "pair", label: `配对 (${dirCounts.pair})` },
              ] as const
            ).map((f) => (
              <button
                key={f.id}
                type="button"
                className="chip !min-h-[24px] !px-2.5 !text-[11px]"
                aria-pressed={dirFilter === f.id}
                onClick={() => setDirFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* Factor Concentration Toggle Badges (Cross-Highlight Correlated Cards) */}
          {concentratedFactors.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-warning">
                <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                同源宏因子集中:
              </span>
              {concentratedFactors.map((cf) => {
                const active = highlightFactor === cf.name;
                return (
                  <button
                    key={cf.name}
                    type="button"
                    onClick={() => setHighlightFactor(active ? null : cf.name)}
                    className={`rounded-full border px-2 py-0.5 text-[10.5px] font-medium transition-colors ${
                      active
                        ? "border-warning bg-warning/20 text-warning"
                        : "border-warning/35 bg-warning/8 text-warning hover:bg-warning/16"
                    }`}
                    title="点击高亮共享此单一宏因子的关联方向卡片（警惕伪分散）"
                  >
                    {cf.name} ({cf.count}卡)
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* 6. Uniform-Height L1 Decision Cards Grid */}
      {visibleDirections.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visibleDirections.map((card) => {
            const isShort = card.direction === "short";
            const isPair = card.direction === "pair";
            const DirIcon = isShort ? TrendingDown : isPair ? Scale : TrendingUp;
            const dirCls = isShort
              ? "border-bear/40 bg-bear/12 text-bear"
              : isPair
                ? "border-warning/40 bg-warning/12 text-warning"
                : "border-bull/40 bg-bull/12 text-bull";
            const dirLabel = isShort ? "做空" : isPair ? "配对" : "做多";

            const flags = card.constitutional_flags ?? [];
            const cappedFlag = flags.find((f) => f.action === "cap_conviction_to_medium");
            const firstFlagMsg = cappedFlag?.message || flags[0]?.message || null;

            const sharedFactors = concentratedCardFactors.get(card.id) ?? [];
            const isFactorHighlighted = Boolean(highlightFactor && sharedFactors.includes(highlightFactor));
            const isActiveL2 = activeL2CardId === card.id;
            const logged = isCardLogged(card.id);
            const loggingThis = loggingOppKey === `${currentRun?.run_id}:${card.id}`;

            const headlinePositive =
              card.card_headline_gap || card.unpriced_gap || card.thesis || "点击查看核心预期差";
            const headlineNegative =
              card.invalidation_short || card.invalidation || card.priced_in_already || "查看证伪红线";

            const anchorTemp = computeAnchorTemperature(card, anchorBySym);
            const l2Summary = l2ScanSummaries?.[card.id];
            const isDrawerOpenForCard = drawer?.kind === "card" && drawer.card.id === card.id;

            return (
              <article
                key={card.id}
                className={`relative flex min-h-[244px] flex-col justify-between overflow-hidden rounded-lg border bg-surface p-3 transition-all ${
                  isActiveL2
                    ? "border-accent ring-1 ring-accent/40"
                    : isFactorHighlighted
                      ? "border-warning ring-1 ring-warning/45"
                      : "border-hairline hover:border-ink-2/40"
                }`}
              >
                {/* Shared Macro Factor Top Color Strip (Mandatory Redline #2) */}
                {sharedFactors.length > 0 ? (
                  <div
                    aria-hidden="true"
                    className="absolute inset-x-0 top-0 h-[3px]"
                    style={{ background: "var(--warning)" }}
                  />
                ) : null}

                {/* Clickable Card Body opens/closes progressive detail drawer */}
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() =>
                    setDrawer((prev) =>
                      prev?.kind === "card" && prev.card.id === card.id ? null : { kind: "card", card },
                    )
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setDrawer((prev) =>
                        prev?.kind === "card" && prev.card.id === card.id ? null : { kind: "card", card },
                      );
                    }
                  }}
                  aria-expanded={isDrawerOpenForCard}
                  className="group flex cursor-pointer flex-col gap-2 text-left outline-none focus-visible:ring-1 focus-visible:ring-accent"
                >
                  {/* Row 1: Badges + Conviction (with explicit cap strikethrough) + Persistent Track C */}
                  <div className="flex flex-wrap items-center justify-between gap-1.5">
                    <div className="flex flex-wrap items-center gap-1">
                      <span
                        className={`inline-flex items-center gap-0.5 rounded border px-1.5 py-px text-[11px] font-semibold ${dirCls}`}
                      >
                        <DirIcon className="h-3 w-3" aria-hidden="true" />
                        {dirLabel}
                      </span>
                      {cappedFlag ? (
                        <span
                          className="inline-flex items-center gap-1 rounded border border-warning/40 bg-warning/12 px-1.5 py-px text-[11px] font-semibold text-warning"
                          title={cappedFlag.message}
                        >
                          <span className="line-through">高</span>
                          <span>降档·中</span>
                        </span>
                      ) : (
                        <span className="rounded bg-subtle px-1.5 py-px text-[11px] font-medium text-ink-2">
                          {card.conviction === "high" ? "高确信" : card.conviction === "medium" ? "中确信" : "观察"}
                        </span>
                      )}
                      <span className="rounded bg-subtle px-1.5 py-px text-[11px] text-muted">
                        {assetScopeLabel(card.asset_scope)}
                      </span>
                      {sharedFactors.length > 0 ? (
                        <span className="rounded bg-warning/14 px-1.5 py-px text-[11px] font-medium text-warning">
                          共因子: {sharedFactors[0]}
                        </span>
                      ) : null}
                    </div>
                    {logged ? (
                      <span
                        className="inline-flex items-center gap-0.5 rounded bg-bull/14 px-1.5 py-px text-[11px] font-semibold text-bull"
                        title="已记入 Track C 影子机会账本"
                      >
                        <BookmarkCheck className="h-3 w-3" aria-hidden="true" />
                        已入影子
                      </span>
                    ) : null}
                  </div>

                  {/* Row 2: Title & Dual-Driver Tag */}
                  <div>
                    <h3
                      className="line-clamp-1 text-[13.5px] font-semibold text-ink transition-colors group-hover:text-accent"
                      title={card.title}
                    >
                      {card.title}
                    </h3>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-muted">
                      <span className="truncate">
                        {card.primary_driver?.dimension} + {card.secondary_driver?.dimension}
                      </span>
                      <span className="shrink-0 font-mono">· {card.horizon || "1Q~1Y"}</span>
                    </div>
                  </div>

                  {/* Row 3: Mandatory Constitutional Flag 1-Line Surface Banner (Redline #1) */}
                  {firstFlagMsg ? (
                    <div
                      className="flex items-center gap-1.5 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-[10.5px] font-medium text-warning"
                      title={firstFlagMsg}
                    >
                      <ShieldAlert className="h-3 w-3 shrink-0" aria-hidden="true" />
                      <span className="truncate">{firstFlagMsg}</span>
                    </div>
                  ) : null}

                  {/* Row 4: Strict 1 Positive + 1 Negative 2-Line Clamped Summary (Redline #3) */}
                  <div className="space-y-1.5 text-[11.5px] leading-snug">
                    <p className="line-clamp-2 text-ink" title={headlinePositive}>
                      <span className="mr-1 font-semibold text-bull">差:</span>
                      {headlinePositive}
                    </p>
                    <p className="line-clamp-2 text-bear" title={headlineNegative}>
                      <span className="mr-1 font-semibold">线:</span>
                      {headlineNegative}
                    </p>
                  </div>

                  {/* Row 5: Anchor Temperature (with real 20D% + chase warning) + Bidirectional L2 Scan Badge */}
                  <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                    {anchorTemp ? (
                      <span
                        title={anchorTemp.detail}
                        className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium ${
                          anchorTemp.kind === "aligned"
                            ? "bg-bull/10 text-bull"
                            : anchorTemp.kind === "chase_warn"
                              ? "border border-warning/45 bg-warning/14 text-warning"
                              : anchorTemp.kind === "divergent"
                                ? "bg-warning/14 text-warning"
                                : "bg-subtle text-muted"
                        }`}
                      >
                        <Crosshair className="h-2.5 w-2.5" aria-hidden="true" />
                        <span className="font-mono">{anchorTemp.label}</span>
                      </span>
                    ) : null}
                    {l2Summary ? (
                      <span
                        className="inline-flex items-center gap-1 rounded border border-accent/35 bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] font-medium text-accent"
                        title={`该 L1 轨道最近一次 L2 扫描共 ${l2Summary.total} 只标的`}
                      >
                        L2 已筛: {l2Summary.aligned}同向 · {l2Summary.watch}观察
                      </span>
                    ) : null}
                    {card.portfolio_overlap && card.portfolio_overlap.length > 0 ? (
                      <span
                        className="rounded bg-accent/12 px-1.5 py-0.5 text-accent"
                        title={`与当前持仓/板块重合：${card.portfolio_overlap.join("、")}`}
                      >
                        含现有持仓 ({card.portfolio_overlap.length})
                      </span>
                    ) : null}
                  </div>
                </div>

                {/* Row 6: Compact Action Footer */}
                <div className="mt-2.5 flex items-center justify-between gap-2 border-t border-hairline-soft pt-2">
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      className={`btn btn-sm !min-h-[26px] !px-2 !text-[11px] ${
                        isDrawerOpenForCard
                          ? "border-accent/45 bg-accent/12 font-medium text-accent"
                          : "btn-ghost"
                      }`}
                      aria-expanded={isDrawerOpenForCard}
                      onClick={() =>
                        setDrawer((prev) =>
                          prev?.kind === "card" && prev.card.id === card.id
                            ? null
                            : { kind: "card", card },
                        )
                      }
                    >
                      <BookOpen className="h-3 w-3" aria-hidden="true" />
                      <span>{isDrawerOpenForCard ? "收起" : "详情"}</span>
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm !min-h-[26px] !px-2 !text-[11px]"
                      disabled={loggingThis}
                      onClick={() => void handleLogCompassOpportunity("l1_direction_call", card.id)}
                      title={logged ? "已记入 Track C 影子账本（幂等可复用）" : "将本 L1 战略方向记入 Track C 影子账本"}
                    >
                      {logged ? (
                        <BookmarkCheck className="h-3.5 w-3.5 text-bull" aria-hidden="true" />
                      ) : (
                        <BookmarkPlus className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                      <span>{logged ? "已记" : "记影子"}</span>
                    </button>
                  </div>

                  <button
                    type="button"
                    className={`btn btn-sm !min-h-[26px] !px-2.5 !text-[11px] ${
                      isActiveL2 ? "border-accent bg-accent/14 font-semibold text-accent" : "btn-primary"
                    }`}
                    onClick={() => onSelectCardForL2(card, currentRun?.run_id)}
                  >
                    <span>{isActiveL2 ? "● 当前 L2 轨道" : "选龙头过筛"}</span>
                    {!isActiveL2 ? <ArrowRight className="h-3 w-3" aria-hidden="true" /> : null}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      ) : !result?.abstained && !loadingRun ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-hairline bg-sunken/40 py-7 text-center text-[12px] text-muted">
          <p>
            {result
              ? "当前筛选条件下无方向卡片，可切换筛选标签或重新推演。"
              : "尚未运行第一层 AI 宏观盲推演。点击右上角「AI 盲推演新方向」开启自上而下 7 维推演。"}
          </p>
        </div>
      ) : null}

      {/* Non-Modal Progressive Disclosure Right Slide-Over Drawer */}
      <Layer1DetailDrawer
        drawer={drawer}
        result={result}
        runId={currentRun?.run_id}
        isActiveL2={drawer?.kind === "card" ? activeL2CardId === drawer.card.id : false}
        onClose={() => setDrawer(null)}
        onActivateL2Card={(card) => onSelectCardForL2(card, currentRun?.run_id)}
        onLaunchAvoidShort={(item, idx) => {
          const shortCard = buildShortCardFromHeadwind(item, idx);
          onSelectCardForL2(shortCard, currentRun?.run_id);
        }}
        isCardLogged={isCardLogged}
        isLoggingCard={(cid) => loggingOppKey === `${currentRun?.run_id}:${cid}`}
        onLogCard={(cid) => void handleLogCompassOpportunity("l1_direction_call", cid)}
      />

      {/* AI Launch Dialog */}
      <ModalDialog
        open={launchOpen}
        onClose={() => setLaunchOpen(false)}
        title="发起 L1 宏观战略盲推演"
        subtitle="基于免费宏观双核与实时联网检索，严格遵守 5 条宪法护栏与去锚定原则"
        lightDismiss
      >
        <form onSubmit={handleStartCompass} className="flex flex-col gap-3.5 text-[12px]">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="font-medium text-ink">AI 推演引擎</span>
              <select
                value={selectedEngine}
                onChange={(e) => setSelectedEngine(e.target.value as DeepResearchEngine)}
                className="input h-9 px-2.5 text-[12px]"
              >
                {(enginesInfo?.engines ?? []).length > 0 ? (
                  enginesInfo!.engines.map((eng) => (
                    <option key={eng.id} value={eng.id} disabled={!eng.available}>
                      {eng.label} {!eng.available ? "(未就绪)" : ""}
                    </option>
                  ))
                ) : (
                  <>
                    <option value="claude">Claude</option>
                    <option value="codex">Codex</option>
                  </>
                )}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="font-medium text-ink">思考强度 (Effort)</span>
              <select
                value={effort}
                onChange={(e) => setEffort(e.target.value as "low" | "medium" | "high")}
                className="input h-9 px-2.5 text-[12px]"
              >
                <option value="low">Low（快速扫描）</option>
                <option value="medium">Medium（标准推荐）</option>
                <option value="high">High（深度盲推）</option>
              </select>
            </label>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-medium text-ink">定向聚焦领域（留空则跨全市场自动发掘）</span>
              {selectedFocusScopes.length > 0 ? (
                <button
                  type="button"
                  className="text-[11px] text-accent hover:underline"
                  onClick={() => setSelectedFocusScopes([])}
                >
                  清空已选 ({selectedFocusScopes.length})
                </button>
              ) : null}
            </div>
            <div className="space-y-2 rounded-md border border-hairline-soft bg-sunken/50 p-2.5">
              {FOCUS_SCOPE_GROUPS.map((grp) => (
                <div key={grp.group} className="space-y-1">
                  <div className="text-[10.5px] font-medium text-muted">{grp.group}</div>
                  <div className="flex flex-wrap gap-1.5">
                    {grp.items.map((it) => {
                      const active = selectedFocusScopes.includes(it.id);
                      return (
                        <button
                          key={it.id}
                          type="button"
                          onClick={() => toggleFocusScope(it.id)}
                          title={it.hint}
                          className={`chip !min-h-[24px] !px-2 !text-[11px]`}
                          aria-pressed={active}
                        >
                          {it.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <label className="flex flex-col gap-1">
            <span className="font-medium text-ink">补充研究指令（可选）</span>
            <textarea
              rows={2}
              value={focusPrompt}
              onChange={(e) => setFocusPrompt(e.target.value)}
              placeholder="例如：重点评估美债利率上行背景下商品空头与电力设备出海的预期差…"
              className="input px-2.5 py-2 text-[12px]"
            />
          </label>

          <label className="inline-flex cursor-pointer items-center gap-2 text-ink-2">
            <input
              type="checkbox"
              checked={refreshMacroOnRun}
              onChange={(e) => setRefreshMacroOnRun(e.target.checked)}
            />
            <span>推演前同步拉取最新 FRED / 定价锚 / CFTC / Polymarket 数据</span>
          </label>

          {launchErr ? <p className="text-bear">{launchErr}</p> : null}

          <div className="flex items-center justify-end gap-2 border-t border-hairline-soft pt-3">
            <button type="button" className="btn btn-sm" onClick={() => setLaunchOpen(false)}>
              取消
            </button>
            <button type="submit" className="btn btn-primary btn-sm" disabled={launching}>
              <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{launching ? "启动中…" : "开始 L1 推演"}</span>
            </button>
          </div>
        </form>
      </ModalDialog>
    </section>
  );
}
