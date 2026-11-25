import type {
  ArchiveDetail,
  ArchiveInitBody,
  ArchiveListItem,
  ArchiveState,
  CloseBody,
  DeepResearchRequest,
  EvidenceItem,
  FxReferenceResponse,
  HealthResponse,
  MarketDualResponse,
  OpenPositionBody,
  PortfolioInitBody,
  PortfolioSummaryResponse,
  PersistedScanRun,
  ResearchDataResponse,
  ResearchEnginesResponse,
  RotatePositionBody,
  SentinelResponse,
  SymbolSearchItem,
  TaskEventEnvelope,
  TaskRecord,
  TradePlanBody,
  TradePlanResponse,
  TrailStopBody,
  TrimBody,
  UniversePool,
  VoidBody,
  WarehouseSummary,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

function buildUrl(path: string, query?: Query): string {
  const url = new URL(`/api${path}`, window.location.origin);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }
  return url.pathname + url.search;
}

function extractMessage(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const d = b.detail ?? b.error ?? b.message;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((x) => {
          if (x && typeof x === "object" && "msg" in x) {
            const loc = "loc" in x && Array.isArray((x as { loc: unknown }).loc) ? `${(x as { loc: unknown[] }).loc.join(".")}: ` : "";
            return `${loc}${String((x as { msg: unknown }).msg)}`;
          }
          return JSON.stringify(x);
        })
        .join("; ");
    }
    if (d && typeof d === "object") {
      const inner = d as Record<string, unknown>;
      if (typeof inner.error === "string") return inner.error;
      if (typeof inner.message === "string") return inner.message;
      return JSON.stringify(d);
    }
  }
  if (typeof body === "string" && body.trim()) return body.slice(0, 400);
  return `HTTP ${status}`;
}

async function request<T>(
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  opts: { query?: Query; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const res = await fetch(buildUrl(path, opts.query), {
    method,
    headers:
      opts.body !== undefined
        ? { "Content-Type": "application/json", Accept: "application/json" }
        : { Accept: "application/json" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  const text = await res.text();
  let parsed: unknown = text;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, extractMessage(res.status, parsed), parsed);
  return parsed as T;
}

const get = <T,>(path: string, query?: Query, signal?: AbortSignal) => request<T>("GET", path, { query, signal });
const post = <T,>(path: string, body: unknown) => request<T>("POST", path, { body });

export function normalizeTask(raw: TaskRecord): TaskRecord {
  const id = raw.id || raw.task_id || "";
  return { ...raw, id, task_id: id };
}

export const api = {
  health: (signal?: AbortSignal) => get<HealthResponse>("/health", undefined, signal),
  macroSnapshot: (refresh = false, signal?: AbortSignal) =>
    get<{ macro_context: Record<string, unknown>; fetched_at_utc?: string }>("/macro/snapshot", { refresh }, signal),
  fxReference: (q: { quote: string; account: string; refresh?: boolean }, signal?: AbortSignal) =>
    get<FxReferenceResponse>(
      "/fx/reference",
      {
        quote: q.quote,
        account: q.account,
        refresh: q.refresh ?? false,
      },
      signal,
    ),
  searchSymbols: (q: string, limit = 30, signal?: AbortSignal) =>
    get<{ items: SymbolSearchItem[] }>("/symbols/search", { q, limit }, signal),

  /* Screen 1: Sentinel & Portfolio */
  portfolioSummary: (signal?: AbortSignal) => get<PortfolioSummaryResponse>("/portfolio/summary", undefined, signal),
  portfolioInit: (body: PortfolioInitBody) => post<unknown>("/portfolio/init", body),
  openPosition: (body: OpenPositionBody) => post<unknown>("/portfolio/positions/open", body),
  trailStop: (id: string, body: TrailStopBody) =>
    post<unknown>(`/portfolio/positions/${encodeURIComponent(id)}/trail-stop`, body),
  trimPosition: (id: string, body: TrimBody) =>
    post<unknown>(`/portfolio/positions/${encodeURIComponent(id)}/trim`, body),
  closePosition: (id: string, body: CloseBody) =>
    post<unknown>(`/portfolio/positions/${encodeURIComponent(id)}/close`, body),
  voidPosition: (id: string, body: VoidBody = {}) =>
    post<unknown>(`/portfolio/positions/${encodeURIComponent(id)}/void`, body),
  rotatePosition: (id: string, body: RotatePositionBody) =>
    post<unknown>(`/portfolio/positions/${encodeURIComponent(id)}/rotate`, body),
  sentinel: (
    q: { include_watchlist: boolean; skip_research?: boolean; event_window_days?: number; refresh?: boolean },
    signal?: AbortSignal,
  ) =>
    get<SentinelResponse>(
      "/sentinel/run",
      {
        include_watchlist: q.include_watchlist,
        skip_research: q.skip_research ?? false,
        event_window_days: q.event_window_days ?? 14,
        refresh: q.refresh ?? false,
      },
      signal,
    ),

  /* Screen 2: Chanlun War Room & Trade Planner */
  marketDual: (
    q: { symbol: string; period?: string; intraday_interval?: string; include_macro?: boolean; refresh?: boolean },
    signal?: AbortSignal,
  ) =>
    get<MarketDualResponse>(
      "/market/dual",
      {
        symbol: q.symbol,
        period: q.period ?? "3y",
        intraday_interval: q.intraday_interval ?? "30m",
        include_macro: q.include_macro ?? true,
        refresh: q.refresh ?? false,
      },
      signal,
    ),
  tradePlan: (body: TradePlanBody, signal?: AbortSignal) =>
    request<TradePlanResponse>("POST", "/trade/plan", { body, signal }),

  /* Screen 3: Research Archive & Hard Data */
  archives: (signal?: AbortSignal) => get<{ archives: ArchiveListItem[] }>("/research/archives", undefined, signal),
  archive: (symbol: string, market?: string | null, signal?: AbortSignal) =>
    get<ArchiveDetail>("/research/archive", { symbol, market: market || undefined }, signal),
  archiveInit: (body: ArchiveInitBody) => post<unknown>("/research/archive/init", body),
  archiveEvidence: (symbol: string, items: EvidenceItem[], market?: string | null) =>
    post<unknown>("/research/archive/evidence", { symbol, items, ...(market ? { market } : {}) }),
  archiveState: (symbol: string, patch: Partial<ArchiveState>, expected_revision: number, market?: string | null) =>
    request<unknown>("PATCH", "/research/archive/state", {
      body: { symbol, patch, expected_revision, ...(market ? { market } : {}) },
    }),
  archiveLog: (symbol: string, entry: string, market?: string | null) =>
    post<unknown>("/research/archive/log", { symbol, entry, ...(market ? { market } : {}) }),
  archiveUpdateThesis: (
    symbol: string,
    body_md: string,
    expected_revision: number,
    note?: string,
    market?: string | null,
  ) =>
    request<{ revision: number; thesis_md: string }>("PUT", "/research/archive/thesis", {
      body: {
        symbol,
        body_md,
        expected_revision,
        ...(note ? { note } : {}),
        ...(market ? { market } : {}),
      },
    }),
  researchEngines: (signal?: AbortSignal) => get<ResearchEnginesResponse>("/research/engines", undefined, signal),
  researchData: (q: { symbol: string; include_macro?: boolean; refresh?: boolean }, signal?: AbortSignal) =>
    get<ResearchDataResponse>(
      "/research/data",
      { symbol: q.symbol, include_macro: q.include_macro ?? true, refresh: q.refresh ?? false },
      signal,
    ),

  /* Screen 4: Universes & SSE Tasks */
  universes: (signal?: AbortSignal) => get<{ universes: Record<string, UniversePool> }>("/universes", undefined, signal),
  saveUniverse: (body: { name: string; description: string; symbols: string[] }) => post<unknown>("/universes", body),
  startScan: async (body: {
    universes?: string[];
    symbols?: string[];
    sort?: "tier" | "none";
    period?: string;
    refresh?: boolean;
  }) =>
    normalizeTask(
      await post<TaskRecord>("/tasks/scan", {
        universes: body.universes ?? [],
        symbols: body.symbols ?? [],
        sort: body.sort ?? "tier",
        period: body.period ?? "3y",
        refresh: body.refresh ?? false,
      }),
    ),
  startResearchRefresh: async (body: { symbol: string; include_macro: boolean; market?: string | null }) =>
    normalizeTask(
      await post<TaskRecord>("/tasks/research-refresh", {
        symbol: body.symbol,
        include_macro: body.include_macro,
        ...(body.market ? { market: body.market } : {}),
      }),
    ),
  startDeepResearch: async (body: DeepResearchRequest) => {
    const raw = await post<TaskRecord & { already_running?: boolean }>("/tasks/deep-research", {
      symbol: body.symbol,
      mode: body.mode ?? "standard",
      engine: body.engine ?? "claude",
      focus_prompt: body.focus_prompt ?? "",
      include_macro: body.include_macro ?? true,
      ...(body.market ? { market: body.market } : {}),
    });
    return { ...normalizeTask(raw), already_running: Boolean(raw.already_running) };
  },
  startSentinelTask: async (body: { include_watchlist: boolean; skip_research: boolean; event_window_days: number }) =>
    normalizeTask(await post<TaskRecord>("/tasks/sentinel", body)),
  tasks: async (signal?: AbortSignal) => {
    const r = await get<{ tasks: TaskRecord[] }>("/tasks", undefined, signal);
    return { tasks: (r.tasks ?? []).map(normalizeTask) };
  },
  task: async (id: string, signal?: AbortSignal) =>
    normalizeTask(await get<TaskRecord>(`/tasks/${encodeURIComponent(id)}`, undefined, signal)),
  cancelTask: async (id: string) =>
    normalizeTask(await post<TaskRecord>(`/tasks/${encodeURIComponent(id)}/cancel`, {})),
  taskEventsUrl: (id: string) => buildUrl(`/tasks/${encodeURIComponent(id)}/events`),

  /* Local SQLite Data Warehouse & Persisted History */
  warehouse: (signal?: AbortSignal) => get<WarehouseSummary>("/data/warehouse", undefined, signal),
  scanHistory: (limit = 30, signal?: AbortSignal) =>
    get<{ runs: PersistedScanRun[] }>("/history/scans", { limit }, signal),
  scanHistoryDetail: (runId: string, signal?: AbortSignal) =>
    get<PersistedScanRun>(`/history/scans/${encodeURIComponent(runId)}`, undefined, signal),
};

export function parseTaskEvent(raw: string, fallbackType?: string): TaskEventEnvelope | null {
  try {
    const v: unknown = JSON.parse(raw);
    if (v && typeof v === "object") {
      const ev = v as TaskEventEnvelope;
      return { ...ev, type: ev.type ?? fallbackType };
    }
    return { type: fallbackType, message: String(v) };
  } catch {
    return raw ? { type: fallbackType, message: raw } : null;
  }
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.status} · ${err.message}`;
  if (err instanceof DOMException && err.name === "AbortError") return "请求已取消";
  if (err instanceof Error) return err.message;
  return String(err);
}
