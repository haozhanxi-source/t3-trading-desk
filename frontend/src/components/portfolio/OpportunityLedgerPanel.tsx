import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Activity,
  Edit3,
  EyeOff,
  PlusCircle,
  RefreshCw,
} from "lucide-react";
import { api, errorMessage } from "../../api/client";
import type {
  Direction,
  LogOpportunityBody,
  PortfolioAttributionAnalytics,
  ShadowOpportunity,
  ShadowOpportunityKind,
  ShadowOpportunityStatus,
  UpdateOpportunityBody,
} from "../../api/types";
import {
  fmtMoney,
  fmtPct,
  fmtPrice,
  optNum,
} from "../../lib/format";
import { ModalDialog } from "../ui/ModalDialog";
import {
  DirectionBadge,
  StatusBadge,
  signedToneClass,
  type Tone,
} from "../ui/StatusBadge";
import { SymbolLabel } from "../ui/SymbolLabel";
import {
  AttributionTagSelect,
  formatAttributionTagZh,
} from "./PortfolioDialogs";

export interface ShadowOpportunitySeed {
  nonce: number;
  symbol: string;
  market?: string;
  sector_theme?: string;
  direction?: Direction;
  kind?: ShadowOpportunityKind;
  anchor_price?: number | null;
  planned_stop_price?: number | null;
  stop_hint?: string | null;
  linked_position_id?: string | null;
  root_cause_tag?: string | null;
  lesson_note?: string;
}

type OpportunityMutationResponse = ShadowOpportunity & {
  opportunity?: ShadowOpportunity;
  attribution?: PortfolioAttributionAnalytics | null;
};

function exactNumInput(v: number | null | undefined): string {
  return v != null && Number.isFinite(v) ? String(v) : "";
}

function createOpportunityIdempotencyKey(): string {
  const uid =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `shadow-opp-${uid}`;
}

interface OpportunityLedgerPanelProps {
  workspaceRev?: number;
  baseCurrency?: string;
  prefillSeed?: ShadowOpportunitySeed | null;
  onPrefillConsumed?: () => void;
  onJumpWarRoom?: (symbol: string) => void;
}

const KIND_META: Record<string, { label: string; tone: Tone }> = {
  missed_entry: { label: "看对未上车", tone: "warning" },
  shaken_out: { label: "30m洗飞", tone: "critical" },
  premature_exit: { label: "过早止盈", tone: "info" },
  l1_direction_call: { label: "L1方向立项", tone: "good" },
  l1_abstention: { label: "L1战略留白", tone: "neutral" },
};

const TAG_CATEGORY_ZH: Record<string, { label: string; tone: Tone }> = {
  positive: { label: "正向执行", tone: "good" },
  pilot_friction: { label: "合规试错", tone: "neutral" },
  mistake: { label: "可避免漏损", tone: "critical" },
};

function fmtSignedR(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}R`;
}

export function OpportunityLedgerPanel({
  workspaceRev = 0,
  baseCurrency = "USD",
  prefillSeed = null,
  onPrefillConsumed,
  onJumpWarRoom,
}: OpportunityLedgerPanelProps) {
  const [attribution, setAttribution] = useState<PortfolioAttributionAnalytics | null>(null);
  const [opportunities, setOpportunities] = useState<ShadowOpportunity[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<"all" | ShadowOpportunityKind>("all");
  const [localRev, setLocalRev] = useState(0);

  /* Create / Update modal state */
  const [createOpen, setCreateOpen] = useState(false);
  const [createSeed, setCreateSeed] = useState<ShadowOpportunitySeed | null>(null);
  const [editTarget, setEditTarget] = useState<ShadowOpportunity | null>(null);

  const loadData = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setErr(null);
    try {
      const [attrRes, oppRes] = await Promise.allSettled([
        api.getPortfolioAttribution(signal),
        api.getPortfolioOpportunities({ limit: 100 }, signal),
      ]);
      if (signal?.aborted) return;

      const errs: string[] = [];
      if (attrRes.status === "fulfilled") {
        setAttribution(attrRes.value);
      } else {
        errs.push(`归因统计：${errorMessage(attrRes.reason)}`);
      }
      if (oppRes.status === "fulfilled") {
        setOpportunities(oppRes.value.opportunities ?? []);
      } else {
        errs.push(`影子账本：${errorMessage(oppRes.reason)}`);
      }
      if (errs.length > 0) setErr(errs.join("；"));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void loadData(ctrl.signal);
    return () => ctrl.abort();
  }, [loadData, workspaceRev, localRev]);

  useEffect(() => {
    if (!prefillSeed) return;
    setCreateSeed(prefillSeed);
    setCreateOpen(true);
    onPrefillConsumed?.();
  }, [prefillSeed, onPrefillConsumed]);

  const filteredOpps = useMemo(() => {
    if (kindFilter === "all") return opportunities;
    return opportunities.filter((o) => o.kind === kindFilter);
  }, [opportunities, kindFilter]);

  const bumpRefresh = useCallback(() => setLocalRev((v) => v + 1), []);

  const handleOpportunityMutationDone = useCallback(
    (res?: OpportunityMutationResponse | null) => {
      if (res?.attribution) {
        setAttribution(res.attribution);
      }
      // Independently trigger a best-effort refresh of attribution & opportunities
      setLocalRev((v) => v + 1);
    },
    [],
  );

  const skew = attribution?.skewness;
  const shadow = attribution?.shadow_summary;
  const byTag = attribution?.by_tag ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* Section 1: Asymmetric Payoff Scorecard */}
      <section className="card flex flex-col gap-3 p-4" aria-label="右偏收益体检看板">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Activity className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
            <span className="section-title">右偏收益体检看板（Asymmetric Payoff Scorecard）</span>
            {attribution ? (
              <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 font-mono text-[10.5px] text-muted">
                已了结 {attribution.closed_count} 笔 · 胜 {attribution.win_count} / 负 {attribution.loss_count}
              </span>
            ) : null}
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm px-2 text-muted"
            disabled={loading}
            onClick={bumpRefresh}
            title="刷新归因统计与影子账本"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
        </div>

        {err ? (
          <div role="alert" className="rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="metric-strip grid-cols-2 sm:grid-cols-4 lg:grid-cols-7">
          <div className="metric-cell">
            <span className="metric-label">胜率</span>
            <span className="metric-val">
              {attribution ? fmtPct(attribution.win_rate_pct, 1) : loading ? "…" : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">R 盈亏比</span>
            <span
              className={`metric-val ${
                attribution?.payoff_ratio_r != null && attribution.payoff_ratio_r >= 2.0
                  ? "text-bull"
                  : ""
              }`}
            >
              {attribution?.payoff_ratio_r != null
                ? `${attribution.payoff_ratio_r.toFixed(2)}×`
                : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">单笔数学期望</span>
            <span className={`metric-val ${signedToneClass(attribution?.expectancy_r)}`}>
              {attribution ? fmtSignedR(attribution.expectancy_r) : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">Top20% 盈利贡献</span>
            <span className="metric-val text-accent">
              {skew ? fmtPct(skew.top_20pct_profit_share_pct, 1) : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">合规试错摩擦 R</span>
            <span className="metric-val text-ink-2">
              {skew ? fmtSignedR(skew.pilot_friction_loss_r) : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">可避免错误漏损 R</span>
            <span
              className={`metric-val ${
                skew && skew.preventable_mistake_loss_r < 0 ? "text-critical" : "text-muted"
              }`}
            >
              {skew ? fmtSignedR(skew.preventable_mistake_loss_r) : "—"}
            </span>
          </div>
          <div className="metric-cell">
            <span className="metric-label">影子账本错失 R</span>
            <span
              className={`metric-val ${
                shadow && shadow.total_missed_r > 0 ? "text-warning" : "text-muted"
              }`}
            >
              {shadow ? fmtSignedR(shadow.total_missed_r) : "—"}
            </span>
          </div>
        </div>

        {/* By-Tag Attribution Breakdown */}
        {byTag.length > 0 ? (
          <div tabIndex={0} role="region" aria-label="逐笔归因标签分布表" className="table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">归因标签</th>
                  <th scope="col">分类</th>
                  <th scope="col" className="num">
                    笔数
                  </th>
                  <th scope="col" className="num">
                    累计 R
                  </th>
                  <th scope="col" className="num">
                    笔均 R
                  </th>
                  <th scope="col" className="num">
                    累计盈亏
                  </th>
                </tr>
              </thead>
              <tbody>
                {byTag.map((row) => {
                  const cat = TAG_CATEGORY_ZH[row.category] ?? {
                    label: row.category,
                    tone: "neutral" as Tone,
                  };
                  return (
                    <tr key={row.tag}>
                      <th scope="row" className="font-medium text-ink">
                        <span>{row.label || formatAttributionTagZh(row.tag)}</span>
                        <span className="ml-1.5 font-mono text-[10.5px] font-normal text-muted">
                          {row.tag}
                        </span>
                      </th>
                      <td data-label="分类">
                        <StatusBadge tone={cat.tone} label={cat.label} />
                      </td>
                      <td data-label="笔数" className="num">
                        {row.count}
                      </td>
                      <td data-label="累计 R" className={`num font-semibold ${signedToneClass(row.total_r)}`}>
                        {fmtSignedR(row.total_r)}
                      </td>
                      <td data-label="笔均 R" className={`num ${signedToneClass(row.avg_r)}`}>
                        {fmtSignedR(row.avg_r)}
                      </td>
                      <td data-label="累计盈亏" className={`num ${signedToneClass(row.total_pnl)}`}>
                        {row.total_pnl > 0 ? "+" : ""}
                        {fmtMoney(row.total_pnl, baseCurrency)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : attribution && attribution.closed_count > 0 ? (
          <div className="text-xs text-muted">
            尚无带归因标签的了结记录（当前 {skew?.untagged_closed_count ?? 0} 笔未归因；平仓或减仓时选择归因标签即可自动聚合）。
          </div>
        ) : null}
      </section>

      {/* Section 2: Shadow Opportunity Ledger */}
      <section className="card flex flex-col gap-3 p-4" aria-label="踏空与洗飞大机会影子账本">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <EyeOff className="h-3.5 w-3.5 text-warning" aria-hidden="true" />
            <span className="section-title">
              踏空 / 洗飞大机会影子账本（{filteredOpps.length} / {opportunities.length}）
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {(
              [
                ["all", "全部"],
                ["missed_entry", "看对未上车"],
                ["shaken_out", "30m洗飞"],
                ["premature_exit", "过早止盈"],
                ["l1_direction_call", "L1方向立项"],
                ["l1_abstention", "L1战略留白"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                className="chip py-0.5 text-[11px]"
                aria-pressed={kindFilter === k}
                onClick={() => setKindFilter(k)}
              >
                {label}
              </button>
            ))}
            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => {
                setCreateSeed(null);
                setCreateOpen(true);
              }}
            >
              <PlusCircle className="h-3.5 w-3.5" aria-hidden="true" />
              <span>登记错失/洗飞机会</span>
            </button>
          </div>
        </div>

        <div tabIndex={0} role="region" aria-label="踏空洗飞大机会影子账本明细表" className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">标的</th>
                <th scope="col">方向</th>
                <th scope="col">类型</th>
                <th scope="col" className="num">
                  锚定价
                </th>
                <th scope="col" className="num">
                  1R 止损价
                </th>
                <th scope="col" className="num">
                  后续极值
                </th>
                <th scope="col" className="num">
                  错失 R 倍数
                </th>
                <th scope="col">根因与教训</th>
                <th scope="col">状态</th>
                <th scope="col" className="num">
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
              {filteredOpps.length === 0 ? (
                <tr>
                  <th scope="row">—</th>
                  <td data-label="方向">—</td>
                  <td data-label="类型">暂无记录</td>
                  <td data-label="锚定价" className="num">—</td>
                  <td data-label="1R 止损价" className="num">—</td>
                  <td data-label="后续极值" className="num">—</td>
                  <td data-label="错失 R 倍数" className="num">—</td>
                  <td data-label="根因与教训" className="text-muted">
                    登记看对未上车、被 30m 噪音洗飞或小赚卖飞的大波段，量化改进右偏收益捕获率
                  </td>
                  <td data-label="状态">—</td>
                  <td data-label="操作" className="num">—</td>
                </tr>
              ) : (
                filteredOpps.map((opp) => {
                  const km = KIND_META[opp.kind] ?? { label: opp.kind, tone: "neutral" as Tone };
                  const rootLabel = opp.root_cause_tag
                    ? formatAttributionTagZh(opp.root_cause_tag)
                    : "";
                  return (
                    <tr key={opp.opportunity_id}>
                      <th scope="row">
                        {onJumpWarRoom ? (
                          <button
                            type="button"
                            className="cursor-pointer border-0 bg-transparent p-0 text-left hover:underline"
                            onClick={() => onJumpWarRoom(opp.symbol)}
                          >
                            <SymbolLabel symbol={opp.symbol} size="sm" />
                          </button>
                        ) : (
                          <SymbolLabel symbol={opp.symbol} size="sm" />
                        )}
                      </th>
                      <td data-label="方向">
                        <DirectionBadge direction={opp.direction} />
                      </td>
                      <td data-label="类型">
                        <StatusBadge tone={km.tone} label={km.label} />
                      </td>
                      <td data-label="锚定价" className="num">
                        {fmtPrice(opp.anchor_price)}
                      </td>
                      <td data-label="1R 止损价" className="num text-bear">
                        {fmtPrice(opp.planned_stop_price)}
                      </td>
                      <td data-label="后续极值" className="num">
                        {fmtPrice(opp.subsequent_extreme_price)}
                      </td>
                      <td
                        data-label="错失 R 倍数"
                        className={`num font-semibold ${
                          opp.missed_r_multiple != null && opp.missed_r_multiple > 0
                            ? "text-warning"
                            : "text-muted"
                        }`}
                      >
                        {fmtSignedR(opp.missed_r_multiple)}
                      </td>
                      <td data-label="根因与教训" className="max-w-xs text-ink-2">
                        {rootLabel ? (
                          <span className="mr-1.5 rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 text-[10.5px] font-medium text-ink">
                            {rootLabel}
                          </span>
                        ) : null}
                        <span className="text-[11.5px]">{opp.lesson_note || "—"}</span>
                      </td>
                      <td data-label="状态">
                        <StatusBadge
                          tone={opp.status === "reviewed" ? "good" : "info"}
                          label={opp.status === "reviewed" ? "已复盘" : "跟踪中"}
                        />
                      </td>
                      <td data-label="操作" className="num">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setEditTarget(opp)}
                        >
                          <Edit3 className="h-3 w-3 text-muted" aria-hidden="true" />
                          <span>更新极值/复盘</span>
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

      <LogOpportunityModal
        open={createOpen}
        seed={createSeed}
        onClose={() => {
          setCreateOpen(false);
          setCreateSeed(null);
        }}
        onSuccess={handleOpportunityMutationDone}
      />

      <UpdateOpportunityModal
        open={editTarget !== null}
        opportunity={editTarget}
        onClose={() => setEditTarget(null)}
        onSuccess={handleOpportunityMutationDone}
      />
    </div>
  );
}

/* ------------------------------------------------------------------
 * Create Shadow Opportunity Modal
 * ------------------------------------------------------------------ */
function LogOpportunityModal({
  open,
  seed,
  onClose,
  onSuccess,
}: {
  open: boolean;
  seed: ShadowOpportunitySeed | null;
  onClose: () => void;
  onSuccess: (res?: OpportunityMutationResponse | null) => void;
}) {
  const [symbol, setSymbol] = useState("");
  const [market, setMarket] = useState("US");
  const [sectorTheme, setSectorTheme] = useState("");
  const [direction, setDirection] = useState<Direction>("long");
  const [kind, setKind] = useState<ShadowOpportunityKind>("missed_entry");
  const [anchorPrice, setAnchorPrice] = useState("");
  const [plannedStop, setPlannedStop] = useState("");
  const [extremePrice, setExtremePrice] = useState("");
  const [rootCauseTag, setRootCauseTag] = useState("");
  const [lessonNote, setLessonNote] = useState("");
  const [linkedPosId, setLinkedPosId] = useState("");
  const [status, setStatus] = useState<ShadowOpportunityStatus>("tracking");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);

  const regenerateIdempotencyKey = () => {
    idempotencyKeyRef.current = createOpportunityIdempotencyKey();
  };

  useEffect(() => {
    if (!open) return;
    regenerateIdempotencyKey();
    setSymbol(seed?.symbol ?? "");
    setMarket(seed?.market ?? "US");
    setSectorTheme(seed?.sector_theme ?? "");
    setDirection(seed?.direction ?? "long");
    setKind(seed?.kind ?? "missed_entry");
    setAnchorPrice(exactNumInput(seed?.anchor_price));
    setPlannedStop(exactNumInput(seed?.planned_stop_price));
    setExtremePrice("");
    setRootCauseTag(seed?.root_cause_tag ?? (seed?.kind === "shaken_out" ? "shaken_out_30m_noise" : ""));
    setLessonNote(seed?.lesson_note ?? "");
    setLinkedPosId(seed?.linked_position_id ?? "");
    setStatus("tracking");
    setErr(null);
  }, [open, seed]);

  const missingTraceableStopHint =
    plannedStop.trim() === ""
      ? seed?.stop_hint ||
        (seed?.linked_position_id && seed.planned_stop_price == null
          ? "未找到可追溯的初始 1R 止损距，请手动填写当时 1R 止损价"
          : null)
      : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const anc = optNum(anchorPrice);
    const stp = optNum(plannedStop);
    const ext = optNum(extremePrice);
    if (!symbol.trim() || anc === undefined || anc <= 0 || stp === undefined || stp <= 0) {
      setErr("请填写标的代码以及大于 0 的锚定价与计划 1R 止损价");
      return;
    }
    if (direction === "long" && !(stp < anc)) {
      setErr(`做多计划 1R 止损价 (${fmtPrice(stp)}) 必须严格低于锚定价 (${fmtPrice(anc)})`);
      return;
    }
    if (direction === "short" && !(stp > anc)) {
      setErr(`做空计划 1R 止损价 (${fmtPrice(stp)}) 必须严格高于锚定价 (${fmtPrice(anc)})`);
      return;
    }
    if (extremePrice.trim() !== "" && (ext === undefined || ext <= 0)) {
      setErr("后续极值价必须为大于 0 的有效数值");
      return;
    }

    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = createOpportunityIdempotencyKey();
    }
    const idemKey = idempotencyKeyRef.current;

    setBusy(true);
    setErr(null);
    try {
      const body: LogOpportunityBody = {
        symbol: symbol.trim().toUpperCase(),
        market: market.trim().toUpperCase() || "US",
        sector_theme: sectorTheme.trim() || "未分类",
        direction,
        kind,
        anchor_price: anc,
        planned_stop_price: stp,
        ...(ext !== undefined ? { subsequent_extreme_price: ext } : {}),
        ...(rootCauseTag ? { root_cause_tag: rootCauseTag } : {}),
        ...(lessonNote.trim() ? { lesson_note: lessonNote.trim() } : {}),
        ...(linkedPosId.trim() ? { linked_position_id: linkedPosId.trim() } : {}),
        status,
        idempotency_key: idemKey,
      };
      const res = (await api.logOpportunity(body)) as OpportunityMutationResponse;
      idempotencyKeyRef.current = null;
      onSuccess(res);
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
      title="登记错失 / 洗飞大机会（影子账本）"
      subtitle="追踪看对未上车、30m 洗飞或过早止盈的大波段，以统一 1R 基准量化错失收益"
    >
      <form onSubmit={submit} className="flex max-h-[76dvh] flex-col gap-3.5 overflow-y-auto p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field">
            <span>标的代码 *</span>
            <input
              className="input font-mono"
              required
              autoFocus={!seed?.symbol}
              placeholder="如 NVDA / CU0"
              value={symbol}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setSymbol(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>多空方向 *</span>
            <select
              className="input"
              value={direction}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setDirection(e.target.value as Direction);
              }}
            >
              <option value="long">做多 (Long)</option>
              <option value="short">做空 (Short)</option>
            </select>
          </label>
          <label className="field">
            <span>错失类型 *</span>
            <select
              className="input"
              value={kind}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setKind(e.target.value as ShadowOpportunityKind);
              }}
            >
              <option value="missed_entry">看对未上车 (missed_entry)</option>
              <option value="shaken_out">30m噪音洗飞 (shaken_out)</option>
              <option value="premature_exit">过早止盈卖飞 (premature_exit)</option>
              <option value="l1_direction_call">L1方向立项 (l1_direction_call)</option>
              <option value="l1_abstention">L1战略留白 (l1_abstention)</option>
            </select>
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field">
            <span>市场分区</span>
            <input
              className="input font-mono"
              placeholder="US / CN_FUTURES / FX"
              value={market}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setMarket(e.target.value);
              }}
            />
          </label>
          <label className="field sm:col-span-2">
            <span>所属板块 / 主驱动主题</span>
            <input
              className="input"
              placeholder="如 北美电网基建 / 铜供需缺口"
              value={sectorTheme}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setSectorTheme(e.target.value);
              }}
            />
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="field">
            <span>锚定价 (入场/出场价) *</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              required
              value={anchorPrice}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setAnchorPrice(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>计划 1R 止损价 *</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              required
              autoFocus={Boolean(missingTraceableStopHint)}
              placeholder={direction === "long" ? "须严格低于锚定价" : "须严格高于锚定价"}
              value={plannedStop}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setPlannedStop(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>后续达到极值价（可选）</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              placeholder="可后续更新"
              value={extremePrice}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setExtremePrice(e.target.value);
              }}
            />
          </label>
        </div>

        {missingTraceableStopHint ? (
          <div className="rounded-md border border-warning/45 bg-warning/10 px-3 py-2 text-xs text-ink">
            {missingTraceableStopHint}
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <AttributionTagSelect
            value={rootCauseTag}
            onChange={(val) => {
              regenerateIdempotencyKey();
              setRootCauseTag(val);
            }}
            label="根因归因标签 (root_cause_tag)"
          />
          <label className="field">
            <span>跟踪状态</span>
            <select
              className="input"
              value={status}
              onChange={(e) => {
                regenerateIdempotencyKey();
                setStatus(e.target.value as ShadowOpportunityStatus);
              }}
            >
              <option value="tracking">跟踪极值中 (tracking)</option>
              <option value="reviewed">已完成复盘 (reviewed)</option>
            </select>
          </label>
        </div>

        <label className="field">
          <span>复盘教训与改进动作 (lesson_note)</span>
          <textarea
            className="input"
            rows={2}
            placeholder="例如：+2R 后未及时切至日线主升防线，导致被 30m 正常回踩洗出…"
            value={lessonNote}
            onChange={(e) => {
              regenerateIdempotencyKey();
              setLessonNote(e.target.value);
            }}
          />
        </label>

        {linkedPosId ? (
          <div className="text-[11px] font-mono text-muted">
            已关联历史持仓 ID：{linkedPosId}
          </div>
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
            {busy ? "正在登记…" : "确认记入影子账本"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}

/* ------------------------------------------------------------------
 * Update Shadow Opportunity Modal
 * ------------------------------------------------------------------ */
function UpdateOpportunityModal({
  open,
  opportunity,
  onClose,
  onSuccess,
}: {
  open: boolean;
  opportunity: ShadowOpportunity | null;
  onClose: () => void;
  onSuccess: (res?: OpportunityMutationResponse | null) => void;
}) {
  const [extremePrice, setExtremePrice] = useState("");
  const [status, setStatus] = useState<ShadowOpportunityStatus>("tracking");
  const [rootCauseTag, setRootCauseTag] = useState("");
  const [lessonNote, setLessonNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const initialRef = useRef<{
    extremePrice: number | null;
    status: ShadowOpportunityStatus;
    rootCauseTag: string | null;
    lessonNote: string;
  } | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const resetPatchIdempotencyKey = () => {
    idempotencyKeyRef.current = null;
  };

  useEffect(() => {
    if (!open || !opportunity) return;
    const initExtreme =
      opportunity.subsequent_extreme_price != null && Number.isFinite(opportunity.subsequent_extreme_price)
        ? opportunity.subsequent_extreme_price
        : null;
    const initStatus: ShadowOpportunityStatus =
      opportunity.status === "reviewed" ? "reviewed" : "tracking";
    const initRoot =
      typeof opportunity.root_cause_tag === "string" && opportunity.root_cause_tag.trim() !== ""
        ? opportunity.root_cause_tag.trim()
        : null;
    const initNote = (opportunity.lesson_note ?? "").trim();

    initialRef.current = {
      extremePrice: initExtreme,
      status: initStatus,
      rootCauseTag: initRoot,
      lessonNote: initNote,
    };
    idempotencyKeyRef.current = null;

    setExtremePrice(exactNumInput(initExtreme));
    setStatus(initStatus);
    setRootCauseTag(initRoot ?? "");
    setLessonNote(opportunity.lesson_note ?? "");
    setErr(null);
  }, [open, opportunity]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!opportunity) return;
    const ext = optNum(extremePrice);
    if (extremePrice.trim() !== "" && (ext === undefined || ext <= 0)) {
      setErr("后续极值价必须为大于 0 的有效数值");
      return;
    }

    const init = initialRef.current;
    const body: UpdateOpportunityBody = {};

    if (extremePrice.trim() === "") {
      if (init && init.extremePrice != null) {
        body.subsequent_extreme_price = null;
      }
    } else if (ext !== undefined) {
      if (!init || init.extremePrice == null || Math.abs(ext - init.extremePrice) > 1e-12) {
        body.subsequent_extreme_price = ext;
      }
    }

    if (!init || status !== init.status) {
      body.status = status;
    }

    const trimmedRoot = rootCauseTag.trim();
    if (trimmedRoot === "") {
      if (init && init.rootCauseTag != null) {
        body.root_cause_tag = null;
      }
    } else if (!init || trimmedRoot !== (init.rootCauseTag ?? "")) {
      body.root_cause_tag = trimmedRoot;
    }

    const trimmedNote = lessonNote.trim();
    if (!init || trimmedNote !== init.lessonNote) {
      body.lesson_note = trimmedNote;
    }

    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }

    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = createOpportunityIdempotencyKey();
    }
    body.idempotency_key = idempotencyKeyRef.current;

    setBusy(true);
    setErr(null);
    try {
      const res = (await api.updateOpportunity(
        opportunity.opportunity_id,
        body,
      )) as OpportunityMutationResponse;
      idempotencyKeyRef.current = null;
      onSuccess(res);
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
      title={`更新影子机会极值与复盘 · ${opportunity?.symbol ?? ""}`}
      subtitle={`锚定价 ${fmtPrice(opportunity?.anchor_price)} · 1R 止损 ${fmtPrice(opportunity?.planned_stop_price)} · 当前错失 ${fmtSignedR(opportunity?.missed_r_multiple)}`}
    >
      <form onSubmit={submit} className="flex flex-col gap-3.5 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="field">
            <span>后续达到极值价 (subsequent_extreme_price)</span>
            <input
              className="input font-mono"
              type="number"
              inputMode="decimal"
              step="any"
              autoFocus
              value={extremePrice}
              onChange={(e) => {
                resetPatchIdempotencyKey();
                setExtremePrice(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>复盘状态</span>
            <select
              className="input"
              value={status}
              onChange={(e) => {
                resetPatchIdempotencyKey();
                setStatus(e.target.value as ShadowOpportunityStatus);
              }}
            >
              <option value="tracking">跟踪极值中 (tracking)</option>
              <option value="reviewed">已完成复盘 (reviewed)</option>
            </select>
          </label>
        </div>

        <AttributionTagSelect
          value={rootCauseTag}
          onChange={(v) => {
            resetPatchIdempotencyKey();
            setRootCauseTag(v);
          }}
          label="根因归因标签 (root_cause_tag)"
        />

        <label className="field">
          <span>复盘教训 (lesson_note)</span>
          <textarea
            className="input"
            rows={3}
            value={lessonNote}
            onChange={(e) => {
              resetPatchIdempotencyKey();
              setLessonNote(e.target.value);
            }}
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
            {busy ? "正在更新…" : "保存更新"}
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
