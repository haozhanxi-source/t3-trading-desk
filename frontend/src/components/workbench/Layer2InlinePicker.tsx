import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  BookmarkCheck,
  BookmarkPlus,
  BookOpen,
  Check,
  Compass,
  Play,
  Plus,
  Scale,
  Search,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../../api/client";
import type {
  CompassDirectionCard,
  CompassScanContext,
  SymbolSearchItem,
  UniversePool,
} from "../../api/types";
import { SymbolLabel } from "../ui/SymbolLabel";
import { parseSymbolList } from "../../lib/format";
import { getUniverseLabel, groupUniverses } from "../../lib/universes";

export interface Layer2InlinePickerProps {
  card: CompassDirectionCard;
  runId?: string;
  universes: Record<string, UniversePool>;
  scanBusy?: boolean;
  isLoggedInShadow?: boolean;
  loggingShadow?: boolean;
  onLogShadow?: () => void;
  onOpenCardDetail: () => void;
  onClearTrack: () => void;
  onLaunchScan: (opts: {
    symbols: string[];
    compassContext: CompassScanContext;
  }) => Promise<void> | void;
  onUniverseSaved?: () => void;
}

function dedupeUpper(list: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of list) {
    const s = raw.trim().toUpperCase();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

function isAShareCode(raw: string): boolean {
  const cleaned = raw.trim().toUpperCase().replace(/^US:/, "");
  return /^\d{6}(\.(SS|SH|SZ|BJ))?$/.test(cleaned);
}

/**
 * Inline two-stage Layer 2 leader picker anchored directly to the selected Layer 1 track.
 * Enforces anti-anchoring: separates [基准参照锚] (dashed baseline) from [候选真龙头槽] (solid user-picked leaders).
 */
export function Layer2InlinePicker({
  card,
  runId,
  universes,
  scanBusy = false,
  isLoggedInShadow = false,
  loggingShadow = false,
  onLogShadow,
  onOpenCardDetail,
  onClearTrack,
  onLaunchScan,
  onUniverseSaved,
}: Layer2InlinePickerProps) {
  const isPair = card.direction === "pair";
  const isShort = card.direction === "short";

  /* Stage 1: Baseline reference anchors (dashed pills, optional inclusion for comparison ONLY) */
  const referenceAnchors = useMemo(
    () => dedupeUpper(card.reference_anchors ?? []),
    [card.reference_anchors],
  );
  const [includedAnchors, setIncludedAnchors] = useState<string[]>([]);

  /* Stage 2: User-curated True Leader slots (initially empty unless explicit non-anchor leaders exist) */
  const [longLeaders, setLongLeaders] = useState<string[]>([]);
  const [shortLeaders, setShortLeaders] = useState<string[]>([]);
  const [activeLeg, setActiveLeg] = useState<"long" | "short">(isShort ? "short" : "long");

  /* Autocomplete search input */
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<SymbolSearchItem[]>([]);
  const [searching, setSearching] = useState(false);

  /* Optional save-as-pool for newly conceived themes */
  const [saveAsPool, setSaveAsPool] = useState(false);
  const [poolName, setPoolName] = useState("");
  const [poolDesc, setPoolDesc] = useState("");
  const [allowOverwrite, setAllowOverwrite] = useState(false);
  const [poolConflict, setPoolConflict] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  /* Reset when switching active L1 card — NEVER silently promote reference_anchors into true leader slots */
  useEffect(() => {
    const anchorSet = new Set(dedupeUpper(card.reference_anchors ?? []));
    if (card.direction === "pair") {
      setLongLeaders(
        dedupeUpper(card.leaders.filter((l) => l.leg === "long").map((l) => l.symbol)).filter(
          (s) => !anchorSet.has(s),
        ),
      );
      setShortLeaders(
        dedupeUpper(card.leaders.filter((l) => l.leg === "short").map((l) => l.symbol)).filter(
          (s) => !anchorSet.has(s),
        ),
      );
      setActiveLeg("long");
    } else if (card.direction === "short") {
      setLongLeaders([]);
      setShortLeaders(
        dedupeUpper(card.leaders.filter((l) => l.leg === "short").map((l) => l.symbol)).filter(
          (s) => !anchorSet.has(s),
        ),
      );
      setActiveLeg("short");
    } else {
      setLongLeaders(
        dedupeUpper(card.leaders.filter((l) => l.leg === "long").map((l) => l.symbol)).filter(
          (s) => !anchorSet.has(s),
        ),
      );
      setShortLeaders([]);
      setActiveLeg("long");
    }
    setIncludedAnchors([]);
    setQuery("");
    setSuggestions([]);
    setSaveAsPool(false);
    setPoolName(card.suggested_pool_name || `theme_${card.id}`);
    const driverLabel = [card.primary_driver?.dimension, card.secondary_driver?.dimension]
      .filter((d): d is string => Boolean(d && d.trim()))
      .join(" + ");
    setPoolDesc(
      card.suggested_pool_description ||
        (driverLabel ? `${card.title}（${driverLabel}）` : card.title),
    );
    setAllowOverwrite(false);
    setPoolConflict(false);
    setErr(null);
  }, [card]);

  /* Debounced symbol search */
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setSuggestions([]);
      setSearching(false);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      void api
        .searchSymbols(q, 8, ctrl.signal)
        .then((res) => {
          if (!ctrl.signal.aborted) setSuggestions(res.items ?? []);
        })
        .catch(() => {
          if (!ctrl.signal.aborted) setSuggestions([]);
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setSearching(false);
        });
    }, 150);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [query]);

  const referencePoolCandidates = useMemo(() => {
    const sections: { poolKey: string; label: string; symbols: string[] }[] = [];
    for (const key of card.recommended_universes ?? []) {
      const pool = universes[key];
      if (!pool || !Array.isArray(pool.symbols) || pool.symbols.length === 0) continue;
      sections.push({
        poolKey: key,
        label: getUniverseLabel(key, pool.description),
        symbols: pool.symbols,
      });
    }
    return sections;
  }, [card.recommended_universes, universes]);

  const customPools = useMemo(() => {
    const groups = groupUniverses(universes);
    return groups.find((g) => g.groupKey === "custom")?.items ?? [];
  }, [universes]);

  const targetLeg: "long" | "short" = isPair ? activeLeg : isShort ? "short" : "long";

  const addLeader = (raw: string, leg: "long" | "short" = targetLeg) => {
    const sym = raw.trim().toUpperCase();
    if (!sym) return;
    if (leg === "short" && isAShareCode(sym)) {
      setErr(`A 股严禁做空，已拦截加入空头腿：${sym}`);
      return;
    }
    if (leg === "short") {
      setShortLeaders((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
      if (isPair) setLongLeaders((prev) => prev.filter((x) => x !== sym));
    } else {
      setLongLeaders((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
      if (isPair) setShortLeaders((prev) => prev.filter((x) => x !== sym));
    }
    setErr(null);
  };

  const toggleLeaderOnActiveLeg = (raw: string) => {
    const sym = raw.trim().toUpperCase();
    if (!sym) return;
    const current = targetLeg === "short" ? shortLeaders : longLeaders;
    if (current.includes(sym)) {
      if (targetLeg === "short") {
        setShortLeaders((prev) => prev.filter((x) => x !== sym));
      } else {
        setLongLeaders((prev) => prev.filter((x) => x !== sym));
      }
    } else {
      addLeader(sym, targetLeg);
    }
  };

  const toggleBaselineAnchor = (sym: string) => {
    setIncludedAnchors((prev) =>
      prev.includes(sym) ? prev.filter((x) => x !== sym) : [...prev, sym],
    );
    setErr(null);
  };

  const handleAddFromInput = () => {
    const parsed = parseSymbolList(query);
    if (parsed.length === 0) return;
    for (const sym of parsed) addLeader(sym, targetLeg);
    setQuery("");
    setSuggestions([]);
    inputRef.current?.focus();
  };

  const pendingSymbols = useMemo(() => parseSymbolList(query), [query]);
  const customLeaderCount = useMemo(
    () => dedupeUpper([...longLeaders, ...shortLeaders, ...pendingSymbols]).length,
    [longLeaders, shortLeaders, pendingSymbols],
  );
  const onlyBaselineMode =
    !isPair && customLeaderCount === 0 && (includedAnchors.length > 0 || referenceAnchors.length > 0);
  const effectiveScanCount = useMemo(() => {
    if (onlyBaselineMode) {
      return includedAnchors.length > 0 ? includedAnchors.length : referenceAnchors.length;
    }
    return dedupeUpper([...longLeaders, ...shortLeaders, ...pendingSymbols, ...includedAnchors]).length;
  }, [onlyBaselineMode, includedAnchors, referenceAnchors, longLeaders, shortLeaders, pendingSymbols]);
  const pairIncomplete =
    isPair &&
    ((longLeaders.length === 0 && !(targetLeg === "long" && pendingSymbols.length > 0) && includedAnchors.length === 0) ||
      (shortLeaders.length === 0 && !(targetLeg === "short" && pendingSymbols.length > 0)));

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || scanBusy) return;

    let finalLong = [...longLeaders];
    let finalShort = [...shortLeaders];

    /* Flush typed input */
    if (pendingSymbols.length > 0) {
      if (targetLeg === "short") {
        finalShort = dedupeUpper([...finalShort, ...pendingSymbols]);
        if (isPair) {
          const sSet = new Set(finalShort);
          finalLong = finalLong.filter((s) => !sSet.has(s));
        }
      } else {
        finalLong = dedupeUpper([...finalLong, ...pendingSymbols]);
        if (isPair) {
          const lSet = new Set(finalLong);
          finalShort = finalShort.filter((s) => !lSet.has(s));
        }
      }
    }

    /* Include toggled baseline anchors (or fallback to all baseline anchors if user consciously submits baseline-only) */
    const baselinesToInclude =
      includedAnchors.length > 0
        ? includedAnchors
        : finalLong.length === 0 && finalShort.length === 0 && !isPair
          ? referenceAnchors
          : [];

    if (baselinesToInclude.length > 0) {
      if (isShort) {
        finalShort = dedupeUpper([...finalShort, ...baselinesToInclude]);
      } else {
        finalLong = dedupeUpper([...finalLong, ...baselinesToInclude]);
      }
    }

    const combined = dedupeUpper([...finalLong, ...finalShort]);
    if (combined.length === 0) {
      setErr("请在[候选真龙头槽]输入或挑选至少 1 只符合本 L1 逻辑的标的");
      return;
    }
    if (isPair && (finalLong.length === 0 || finalShort.length === 0)) {
      setErr("多空配对需分别为[多头腿]和[空头腿]选择至少 1 只标的");
      return;
    }
    const illegalAShareShorts = finalShort.filter(isAShareCode);
    if (illegalAShareShorts.length > 0) {
      setErr(`A 股严禁做空，请从空头腿移除：${illegalAShareShorts.join(", ")}`);
      return;
    }

    setSubmitting(true);
    setErr(null);
    try {
      if (saveAsPool) {
        const cleanName = poolName.trim();
        const cleanDesc = poolDesc.trim();
        if (!cleanName || !cleanDesc) {
          setErr("勾选沉淀专属池时，请填写英文池代号与逻辑说明");
          setSubmitting(false);
          return;
        }
        try {
          await api.saveUniverse({
            name: cleanName,
            description: cleanDesc,
            symbols: combined,
            overwrite: allowOverwrite,
          });
          onUniverseSaved?.();
        } catch (saveEx) {
          if (saveEx instanceof ApiError && saveEx.status === 409) setPoolConflict(true);
          setErr(errorMessage(saveEx));
          setSubmitting(false);
          return;
        }
      }

      const expected: Record<string, "long" | "short"> = {};
      for (const s of finalLong) expected[s] = "long";
      for (const s of finalShort) expected[s] = "short";

      const compassContext: CompassScanContext = {
        run_id: (runId || "").trim(),
        direction_id: (card.id || "").trim(),
        direction_title: (card.title || "").trim(),
        direction: card.direction,
        value_chain_focus: (card.value_chain_focus || "").trim(),
        expected_directions: expected,
      };

      setQuery("");
      setSuggestions([]);
      await onLaunchScan({ symbols: combined, compassContext });
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setSubmitting(false);
    }
  };

  const DirIcon = isShort ? TrendingDown : isPair ? Scale : TrendingUp;
  const dirBadgeCls = isShort
    ? "border-bear/40 bg-bear/12 text-bear"
    : isPair
      ? "border-warning/40 bg-warning/12 text-warning"
      : "border-bull/40 bg-bull/12 text-bull";
  const dirLabel = isShort ? "做空 SHORT" : isPair ? "多空配对 PAIR" : "做多 LONG";
  const invalidationLine = card.invalidation_short || card.invalidation;

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-3 rounded-lg border border-accent/35 bg-surface p-3.5 shadow-xs"
    >
      {/* Sticky L1 Context Strip */}
      <div className="flex flex-wrap items-start justify-between gap-2.5 border-b border-hairline-soft pb-2.5">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center gap-1 rounded bg-accent/14 px-1.5 py-0.5 text-[10px] font-semibold text-accent">
              <Compass className="h-3 w-3" aria-hidden="true" />
              当前 L1 激活轨道
            </span>
            <span
              className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${dirBadgeCls}`}
            >
              <DirIcon className="h-3 w-3" aria-hidden="true" />
              {dirLabel}
            </span>
            <span className="text-[13.5px] font-semibold text-ink">{card.title}</span>
          </div>
          {card.value_chain_focus ? (
            <p className="line-clamp-2 text-[11.5px] leading-snug text-ink-2" title={card.value_chain_focus}>
              <span className="font-medium text-ink">最受益环节：</span>
              {card.value_chain_focus}
            </p>
          ) : null}
          {invalidationLine ? (
            <p className="flex items-center gap-1 text-[11px] text-bear">
              <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
              <span className="truncate">证伪红线：{invalidationLine}</span>
            </p>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" className="btn btn-sm" onClick={onOpenCardDetail}>
            <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
            <span>查看 L1 详情</span>
          </button>
          {onLogShadow ? (
            <button
              type="button"
              className="btn btn-sm"
              disabled={loggingShadow}
              onClick={onLogShadow}
              title="筛完若无满足 30min/日线结构的标的，一键记入 Track C 影子账本保留纪律证据"
            >
              {isLoggedInShadow ? (
                <>
                  <BookmarkCheck className="h-3.5 w-3.5 text-bull" aria-hidden="true" />
                  <span>已入影子追踪</span>
                </>
              ) : (
                <>
                  <BookmarkPlus className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>暂不建仓 → Track C</span>
                </>
              )}
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost btn-sm !px-2"
            onClick={onClearTrack}
            title="退出当前 L1 轨道"
            aria-label="退出当前 L1 轨道"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Stage 1 & Stage 2 Two-Row Anti-Anchoring Layout */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        {/* Stage 1 (4/12): Baseline Reference Anchors (Dashed pills — comparison only) */}
        <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-hairline bg-sunken/60 p-2.5 lg:col-span-4">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-semibold text-ink-2">
              ① 基准参照锚 <span className="font-normal text-muted">(仅作板块温度对照)</span>
            </span>
            {includedAnchors.length > 0 ? (
              <span className="font-mono text-[10px] text-accent">已加对照 {includedAnchors.length}</span>
            ) : null}
          </div>
          {referenceAnchors.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {referenceAnchors.map((sym) => {
                const active = includedAnchors.includes(sym);
                return (
                  <button
                    key={sym}
                    type="button"
                    onClick={() => toggleBaselineAnchor(sym)}
                    className={`inline-flex items-center gap-1 rounded border border-dashed px-2 py-0.5 text-[11px] transition-colors ${
                      active
                        ? "border-accent bg-accent/12 text-accent"
                        : "border-hairline bg-surface text-muted hover:border-ink-2 hover:text-ink"
                    }`}
                    title="点击可将参照锚纳入 L2 扫描作为板块对比基线（不代表最终龙头）"
                  >
                    <SymbolLabel symbol={sym} size="sm" badgeTicker={false} />
                    <span className="text-[9.5px]">{active ? "✓对比" : "+对比"}</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="text-[11px] text-muted">本逻辑未附带固定基准锚，请直接在右侧填入真龙头。</p>
          )}
          <p className="text-[10px] text-muted">
            去锚定原则：参照锚不自动填入龙头槽；请按最受益环节亲选 1~3 只真龙头或双寡头。
          </p>
        </div>

        {/* Stage 2 (8/12): User-Curated True Leader Slot (Solid highlighted) */}
        <div className="flex flex-col gap-2 rounded-md border border-accent/35 bg-accent/[0.03] p-2.5 lg:col-span-8">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[11px] font-semibold text-ink">
              ② 候选真龙头槽 <span className="font-normal text-muted">(单板块只做真龙头或双寡头)</span>
            </span>
            {isPair ? (
              <div className="segmented" role="group" aria-label="多空腿选择">
                <button
                  type="button"
                  className="segmented-btn !min-h-[22px] !px-2 !text-[10.5px]"
                  aria-pressed={activeLeg === "long"}
                  onClick={() => setActiveLeg("long")}
                >
                  多头腿 ({longLeaders.length})
                </button>
                <button
                  type="button"
                  className="segmented-btn !min-h-[22px] !px-2 !text-[10.5px]"
                  aria-pressed={activeLeg === "short"}
                  onClick={() => setActiveLeg("short")}
                >
                  空头腿 ({shortLeaders.length})
                </button>
              </div>
            ) : null}
          </div>

          {/* Selected True Leader Chips */}
          <div className="flex min-h-[30px] flex-wrap items-center gap-1.5">
            {isPair ? (
              <>
                {longLeaders.map((sym) => (
                  <span
                    key={`L-${sym}`}
                    className="inline-flex items-center gap-1 rounded border border-bull/45 bg-bull/12 px-2 py-0.5 text-[11px] font-medium text-bull"
                  >
                    <span className="text-[9.5px]">多</span>
                    <SymbolLabel symbol={sym} size="sm" badgeTicker={false} />
                    <button
                      type="button"
                      onClick={() => setLongLeaders((prev) => prev.filter((x) => x !== sym))}
                      aria-label={`移除多头 ${sym}`}
                      className="hover:opacity-75"
                    >
                      <X className="h-3 w-3" aria-hidden="true" />
                    </button>
                  </span>
                ))}
                {shortLeaders.map((sym) => (
                  <span
                    key={`S-${sym}`}
                    className="inline-flex items-center gap-1 rounded border border-bear/45 bg-bear/12 px-2 py-0.5 text-[11px] font-medium text-bear"
                  >
                    <span className="text-[9.5px]">空</span>
                    <SymbolLabel symbol={sym} size="sm" badgeTicker={false} />
                    <button
                      type="button"
                      onClick={() => setShortLeaders((prev) => prev.filter((x) => x !== sym))}
                      aria-label={`移除空头 ${sym}`}
                      className="hover:opacity-75"
                    >
                      <X className="h-3 w-3" aria-hidden="true" />
                    </button>
                  </span>
                ))}
              </>
            ) : (
              (isShort ? shortLeaders : longLeaders).map((sym) => (
                <span
                  key={sym}
                  className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-medium ${
                    isShort
                      ? "border-bear/45 bg-bear/12 text-bear"
                      : "border-bull/45 bg-bull/12 text-bull"
                  }`}
                >
                  <SymbolLabel symbol={sym} size="sm" badgeTicker={false} />
                  <button
                    type="button"
                    onClick={() =>
                      isShort
                        ? setShortLeaders((prev) => prev.filter((x) => x !== sym))
                        : setLongLeaders((prev) => prev.filter((x) => x !== sym))
                    }
                    aria-label={`移除 ${sym}`}
                    className="hover:opacity-75"
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                  </button>
                </span>
              ))
            )}
            {longLeaders.length === 0 && shortLeaders.length === 0 ? (
              <span className="text-[11px] text-muted">
                尚未放入真龙头：下方输入代码/中文名，或从推荐参考池点选 1~3 只核心龙头
              </span>
            ) : null}
          </div>

          {/* Search & Quick-Add Row */}
          <div className="relative flex items-center gap-2">
            <div className="relative flex-1">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted"
                aria-hidden="true"
              />
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && query.trim()) {
                    e.preventDefault();
                    handleAddFromInput();
                  }
                }}
                placeholder={
                  targetLeg === "short"
                    ? "输入空头代码/中文名（美股/ETF/商品/外汇，A 股禁空）回车加入…"
                    : "输入真龙头代码/中文名（如 NVDA、PWR、CU0、600519）回车加入…"
                }
                className="input h-8 w-full pl-8 pr-3 text-[12px]"
              />
              {suggestions.length > 0 ? (
                <div className="absolute left-0 right-0 top-full z-30 mt-1 max-h-48 overflow-y-auto rounded-md border border-hairline bg-surface shadow-lg">
                  {suggestions.map((item) => (
                    <button
                      key={`${item.asset_class}-${item.symbol}`}
                      type="button"
                      onClick={() => {
                        addLeader(item.symbol, targetLeg);
                        setQuery("");
                        setSuggestions([]);
                      }}
                      className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-[11.5px] hover:bg-elevated"
                    >
                      <span className="font-medium text-ink">
                        {item.label}{" "}
                        <span className="font-mono text-[10.5px] text-muted">{item.symbol}</span>
                      </span>
                      <span className="rounded bg-subtle px-1.5 py-px text-[10px] text-muted">
                        {item.asset_class}
                      </span>
                    </button>
                  ))}
                </div>
              ) : searching ? (
                <div className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[10px] text-muted">
                  检索中…
                </div>
              ) : null}
            </div>
            <button
              type="button"
              className="btn btn-sm h-8 shrink-0"
              disabled={!query.trim()}
              onClick={handleAddFromInput}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              <span>加入</span>
            </button>
          </div>

          {/* Recommended Pool Quick Cherry-Pick Chips (deduplicated against Stage 1 reference anchors) */}
          {referencePoolCandidates.length > 0 || customPools.length > 0 ? (
            <div className="flex flex-col gap-1.5 pt-0.5">
              {referencePoolCandidates.slice(0, 2).map((sec) => {
                const anchorSet = new Set(referenceAnchors.map((a) => a.toUpperCase()));
                const deduped = sec.symbols.filter((s) => !anchorSet.has(s.toUpperCase()));
                const chips = (deduped.length > 0 ? deduped : sec.symbols).slice(0, 10);
                return (
                  <div key={sec.poolKey} className="flex flex-wrap items-center gap-1">
                    <span className="mr-1 text-[10px] font-medium text-muted">参考池 · {sec.label}:</span>
                    {chips.map((sym) => {
                      const active = (targetLeg === "short" ? shortLeaders : longLeaders).includes(
                        sym.toUpperCase(),
                      );
                      return (
                        <button
                          key={sym}
                          type="button"
                          onClick={() => toggleLeaderOnActiveLeg(sym)}
                          className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10.5px] transition-colors ${
                            active
                              ? "border-accent bg-accent/14 font-semibold text-accent"
                              : "border-hairline-soft bg-surface text-ink-2 hover:border-accent/55 hover:text-ink"
                          }`}
                        >
                          {active ? (
                            <Check className="h-2.5 w-2.5 shrink-0" aria-hidden="true" />
                          ) : (
                            <Plus className="h-2.5 w-2.5 shrink-0 text-muted" aria-hidden="true" />
                          )}
                          <SymbolLabel symbol={sym} size="sm" badgeTicker={false} />
                        </button>
                      );
                    })}
                  </div>
                );
              })}
              {customPools.length > 0 ? (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="mr-1 text-[10px] font-medium text-muted">我的主题池:</span>
                  {customPools.slice(0, 5).map((p) => (
                    <button
                      key={p.key}
                      type="button"
                      onClick={() => {
                        for (const s of p.symbols) addLeader(s, targetLeg);
                      }}
                      className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 text-[10.5px] text-ink-2 hover:border-accent hover:text-accent"
                      title={`一键导入主题池 ${p.label}（${p.symbols.join(", ")}）`}
                    >
                      + {p.label} ({p.symbols.length})
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      {/* Footer: Optional Save Pool + Smart Anti-Anchoring Submit CTA */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft pt-2.5">
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-muted hover:text-ink">
            <input
              type="checkbox"
              checked={saveAsPool}
              onChange={(e) => {
                setSaveAsPool(e.target.checked);
                setPoolConflict(false);
              }}
              className="rounded border-hairline"
            />
            <span>顺便沉淀为本地主题池</span>
          </label>
          {saveAsPool ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                type="text"
                value={poolName}
                onChange={(e) => setPoolName(e.target.value)}
                placeholder="英文代号 如 us_power_leaders"
                className="input h-7 w-40 px-2 font-mono text-[11px]"
              />
              <input
                type="text"
                value={poolDesc}
                onChange={(e) => setPoolDesc(e.target.value)}
                placeholder="产业链逻辑说明"
                className="input h-7 w-52 px-2 text-[11px]"
              />
              {poolConflict ? (
                <label className="inline-flex items-center gap-1 text-warning">
                  <input
                    type="checkbox"
                    checked={allowOverwrite}
                    onChange={(e) => setAllowOverwrite(e.target.checked)}
                  />
                  <span>覆盖已有同名池</span>
                </label>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="flex items-center gap-2">
          {err ? (
            <span className="text-[11px] font-medium text-bear">{err}</span>
          ) : pairIncomplete ? (
            <span className="text-[11px] text-muted">请分别为多头腿与空头腿至少选入 1 只标的</span>
          ) : effectiveScanCount === 0 ? (
            <span className="text-[11px] text-muted">请先选入 1~3 只真龙头</span>
          ) : null}
          <button
            type="submit"
            disabled={submitting || scanBusy || effectiveScanCount === 0 || pairIncomplete}
            className={`btn btn-sm ${
              onlyBaselineMode
                ? "border-warning/65 bg-warning/18 font-medium text-ink hover:bg-warning/28"
                : "btn-primary"
            }`}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            <span>
              {submitting || scanBusy
                ? "日线过筛中…"
                : onlyBaselineMode
                  ? `先扫基准锚 (${effectiveScanCount} 只 · 建议补入龙头)`
                  : `启动 L2 日线过筛 (${effectiveScanCount} 只)`}
            </span>
          </button>
        </div>
      </div>
    </form>
  );
}
