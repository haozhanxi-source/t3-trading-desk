import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  BookmarkPlus,
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
import { ModalDialog } from "../ui/ModalDialog";
import { SymbolLabel } from "../ui/SymbolLabel";
import { parseSymbolList } from "../../lib/format";
import { getUniverseLabel, groupUniverses } from "../../lib/universes";

export interface Layer1LeaderPickerModalProps {
  open: boolean;
  card: CompassDirectionCard | null;
  runId?: string;
  universes: Record<string, UniversePool>;
  scanBusy?: boolean;
  onClose: () => void;
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

export function Layer1LeaderPickerModal({
  open,
  card,
  runId,
  universes,
  scanBusy = false,
  onClose,
  onLaunchScan,
  onUniverseSaved,
}: Layer1LeaderPickerModalProps) {
  const isPair = card?.direction === "pair";
  const [longSymbols, setLongSymbols] = useState<string[]>([]);
  const [shortSymbols, setShortSymbols] = useState<string[]>([]);
  const [activeLeg, setActiveLeg] = useState<"long" | "short">("long");

  /* Search & quick-add input */
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<SymbolSearchItem[]>([]);
  const [searching, setSearching] = useState(false);

  /* Optional save-as-my-own-pool toggle */
  const [saveAsPool, setSaveAsPool] = useState(false);
  const [poolName, setPoolName] = useState("");
  const [poolDesc, setPoolDesc] = useState("");
  const [allowOverwrite, setAllowOverwrite] = useState(false);
  const [poolConflict, setPoolConflict] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  /* Reset state whenever modal opens for a card */
  useEffect(() => {
    if (!open || !card) return;
    if (card.direction === "pair") {
      const initLong = dedupeUpper(
        card.leaders.filter((l) => l.leg === "long").map((l) => l.symbol),
      );
      const initShort = dedupeUpper(
        card.leaders.filter((l) => l.leg === "short").map((l) => l.symbol),
      );
      setLongSymbols(initLong);
      setShortSymbols(initShort);
      setActiveLeg("long");
    } else if (card.direction === "short") {
      const initShort = dedupeUpper([
        ...card.leaders.filter((l) => l.leg === "short").map((l) => l.symbol),
        ...(card.leaders.length === 0 ? (card.reference_anchors ?? []) : []),
      ]);
      setLongSymbols([]);
      setShortSymbols(initShort);
      setActiveLeg("short");
    } else {
      const initLong = dedupeUpper([
        ...card.leaders.filter((l) => l.leg === "long").map((l) => l.symbol),
        ...(card.leaders.length === 0 ? (card.reference_anchors ?? []) : []),
      ]);
      setLongSymbols(initLong);
      setShortSymbols([]);
      setActiveLeg("long");
    }
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
    window.setTimeout(() => inputRef.current?.focus(), 60);
  }, [open, card]);

  /* Debounced symbol search against local dictionary */
  useEffect(() => {
    if (!open) return;
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
          if (!ctrl.signal.aborted) {
            setSuggestions(res.items ?? []);
          }
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
  }, [open, query]);

  /* User's own saved custom pools (separate from the 18 built-in broad category pools) */
  const customPools = useMemo(() => {
    const groups = groupUniverses(universes);
    return groups.find((g) => g.groupKey === "custom")?.items ?? [];
  }, [universes]);

  /* Candidate individual symbols from matched pools (NEVER auto-selected as whole pool!) */
  const referencePoolCandidates = useMemo(() => {
    if (!card) return [];
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
  }, [card, universes]);

  const targetLeg: "long" | "short" = isPair
    ? activeLeg
    : card?.direction === "short"
      ? "short"
      : "long";

  const addSymbolToLeg = (raw: string, leg: "long" | "short" = targetLeg) => {
    const sym = raw.trim().toUpperCase();
    if (!sym) return;
    if (leg === "short") {
      setShortSymbols((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
      if (isPair) setLongSymbols((prev) => prev.filter((x) => x !== sym));
    } else {
      setLongSymbols((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
      if (isPair) setShortSymbols((prev) => prev.filter((x) => x !== sym));
    }
    setErr(null);
  };

  const toggleSymbolOnActiveLeg = (raw: string) => {
    const sym = raw.trim().toUpperCase();
    if (!sym) return;
    const current = targetLeg === "short" ? shortSymbols : longSymbols;
    if (current.includes(sym)) {
      if (targetLeg === "short") {
        setShortSymbols((prev) => prev.filter((x) => x !== sym));
      } else {
        setLongSymbols((prev) => prev.filter((x) => x !== sym));
      }
    } else {
      addSymbolToLeg(sym, targetLeg);
    }
  };

  const handleAddFromInput = () => {
    const parsed = parseSymbolList(query);
    if (parsed.length === 0) return;
    for (const sym of parsed) {
      addSymbolToLeg(sym, targetLeg);
    }
    setQuery("");
    setSuggestions([]);
  };

  const allSelectedSymbols = useMemo(
    () => dedupeUpper([...longSymbols, ...shortSymbols]),
    [longSymbols, shortSymbols],
  );

  const effectiveSymbolCount = useMemo(() => {
    const pending = parseSymbolList(query);
    if (pending.length === 0) return allSelectedSymbols.length;
    return dedupeUpper([...allSelectedSymbols, ...pending]).length;
  }, [allSelectedSymbols, query]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!card || submitting || scanBusy) return;

    /* Flush any typed symbol left in the input box on submit */
    let finalLong = [...longSymbols];
    let finalShort = [...shortSymbols];
    const pendingInput = parseSymbolList(query);
    if (pendingInput.length > 0) {
      if (targetLeg === "short") {
        finalShort = dedupeUpper([...finalShort, ...pendingInput]);
        if (isPair) {
          const shortSet = new Set(finalShort);
          finalLong = finalLong.filter((s) => !shortSet.has(s));
        }
      } else {
        finalLong = dedupeUpper([...finalLong, ...pendingInput]);
        if (isPair) {
          const longSet = new Set(finalLong);
          finalShort = finalShort.filter((s) => !longSet.has(s));
        }
      }
    }

    const combined = dedupeUpper([...finalLong, ...finalShort]);
    if (combined.length === 0) {
      setErr("请至少挑选或输入 1 只符合本 L1 逻辑的标的");
      return;
    }
    if (isPair && (finalLong.length === 0 || finalShort.length === 0)) {
      setErr("多空配对需要分别选择至少 1 只多头腿标的和 1 只空头腿标的");
      return;
    }
    const illegalAShareShorts = finalShort.filter(isAShareCode);
    if (illegalAShareShorts.length > 0) {
      setErr(`A 股严禁做空，请从空头标的移除：${illegalAShareShorts.join(", ")}（可选美股/ETF、商品期货或外汇空头）`);
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
          if (saveEx instanceof ApiError && saveEx.status === 409) {
            setPoolConflict(true);
          }
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

      await onLaunchScan({ symbols: combined, compassContext });
      onClose();
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setSubmitting(false);
    }
  };

  if (!card) return null;

  const DirIcon = card.direction === "short" ? TrendingDown : card.direction === "pair" ? Scale : TrendingUp;
  const dirBadgeCls =
    card.direction === "short"
      ? "border-bear/40 bg-bear/12 text-bear"
      : card.direction === "pair"
        ? "border-warning/40 bg-warning/12 text-warning"
        : "border-bull/40 bg-bull/12 text-bull";
  const dirLabel = card.direction === "short" ? "做空 SHORT" : card.direction === "pair" ? "多空配对 PAIR" : "做多 LONG";

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`L1 专属选标过筛 · ${card.title}`}
      subtitle="只在你为本逻辑亲选的小池内做日线缠论联立校验，绝不混入大类池无关品种"
      lightDismiss
      className="max-w-2xl"
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-3.5 p-5">
        {/* L1 Thesis & Leader-Picking Criteria Summary */}
        <div className="flex flex-col gap-1.5 rounded-md border border-hairline bg-elevated/60 p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-semibold ${dirBadgeCls}`}>
              <DirIcon className="h-3 w-3" aria-hidden="true" />
              <span>{dirLabel}</span>
            </span>
            <span className="text-[11px] text-muted">
              {[card.primary_driver?.dimension, card.secondary_driver?.dimension]
                .filter((d): d is string => Boolean(d && d.trim()))
                .join(" + ")}
            </span>
          </div>
          {card.value_chain_focus ? (
            <div className="text-[11.5px] leading-relaxed text-ink">
              <span className="font-semibold text-accent">选龙头抓手：</span>
              <span>{card.value_chain_focus}</span>
            </div>
          ) : null}
          {isPair && (card.long_leg_sector || card.short_leg_sector) ? (
            <div className="grid grid-cols-1 gap-1.5 pt-0.5 text-[11px] sm:grid-cols-2">
              {card.long_leg_sector ? (
                <div className="rounded border border-bull/30 bg-bull/8 px-2 py-1 text-ink-2">
                  <span className="font-semibold text-bull">多腿：</span>
                  {card.long_leg_sector}
                </div>
              ) : null}
              {card.short_leg_sector ? (
                <div className="rounded border border-bear/30 bg-bear/8 px-2 py-1 text-ink-2">
                  <span className="font-semibold text-bear">空腿：</span>
                  {card.short_leg_sector}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* Selected Dedicated Mini-Pool Slots */}
        {isPair ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div
              onClick={() => setActiveLeg("long")}
              className={`flex cursor-pointer flex-col gap-1.5 rounded-md border p-2.5 transition-colors ${
                activeLeg === "long" ? "border-bull bg-bull/8" : "border-hairline bg-sunken/50"
              }`}
            >
              <div className="flex items-center justify-between text-[11px]">
                <span className="font-semibold text-bull">多头腿标的 ({longSymbols.length})</span>
                {activeLeg === "long" ? <span className="text-[10px] text-bull">正在添加至此腿</span> : null}
              </div>
              <div className="flex min-h-7 flex-wrap items-center gap-1.5">
                {longSymbols.length === 0 ? (
                  <span className="text-[11px] text-muted">点击选中后在下方搜索/点选添加</span>
                ) : (
                  longSymbols.map((sym) => (
                    <span
                      key={`long-${sym}`}
                      className="inline-flex items-center gap-1 rounded border border-bull/40 bg-surface px-2 py-0.5 text-xs"
                    >
                      <SymbolLabel symbol={sym} size="sm" />
                      <button
                        type="button"
                        aria-label={`移除 ${sym}`}
                        className="cursor-pointer border-0 bg-transparent p-0 text-muted hover:text-critical"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          setLongSymbols((prev) => prev.filter((x) => x !== sym));
                        }}
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    </span>
                  ))
                )}
              </div>
            </div>

            <div
              onClick={() => setActiveLeg("short")}
              className={`flex cursor-pointer flex-col gap-1.5 rounded-md border p-2.5 transition-colors ${
                activeLeg === "short" ? "border-bear bg-bear/8" : "border-hairline bg-sunken/50"
              }`}
            >
              <div className="flex items-center justify-between text-[11px]">
                <span className="font-semibold text-bear">空头腿标的 ({shortSymbols.length}) · A股禁空</span>
                {activeLeg === "short" ? <span className="text-[10px] text-bear">正在添加至此腿</span> : null}
              </div>
              <div className="flex min-h-7 flex-wrap items-center gap-1.5">
                {shortSymbols.length === 0 ? (
                  <span className="text-[11px] text-muted">点击选中后在下方搜索/点选添加</span>
                ) : (
                  shortSymbols.map((sym) => (
                    <span
                      key={`short-${sym}`}
                      className="inline-flex items-center gap-1 rounded border border-bear/40 bg-surface px-2 py-0.5 text-xs"
                    >
                      <SymbolLabel symbol={sym} size="sm" />
                      <button
                        type="button"
                        aria-label={`移除 ${sym}`}
                        className="cursor-pointer border-0 bg-transparent p-0 text-muted hover:text-critical"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          setShortSymbols((prev) => prev.filter((x) => x !== sym));
                        }}
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    </span>
                  ))
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-1.5 rounded-md border border-hairline bg-sunken/50 p-2.5">
            <div className="flex items-center justify-between text-[11px]">
              <span className="font-semibold text-ink">
                已选专属标的（{allSelectedSymbols.length}）· 建议聚焦 1~3 只真龙头
              </span>
              {allSelectedSymbols.length > 0 ? (
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0 text-[11px] text-muted hover:text-ink"
                  onClick={() => {
                    setLongSymbols([]);
                    setShortSymbols([]);
                  }}
                >
                  清空
                </button>
              ) : null}
            </div>
            <div className="flex min-h-7 flex-wrap items-center gap-1.5">
              {allSelectedSymbols.length === 0 ? (
                <span className="text-[11px] text-muted">
                  尚未选择标的 — 在下方输入中文名/代码搜索添加，或从你的自建池点选
                </span>
              ) : (
                allSelectedSymbols.map((sym) => (
                  <span
                    key={`sel-${sym}`}
                    className="inline-flex items-center gap-1 rounded border border-accent/40 bg-surface px-2 py-0.5 text-xs"
                  >
                    <SymbolLabel symbol={sym} size="sm" />
                    <button
                      type="button"
                      aria-label={`移除 ${sym}`}
                      className="cursor-pointer border-0 bg-transparent p-0 text-muted hover:text-critical"
                      onClick={() => {
                        setLongSymbols((prev) => prev.filter((x) => x !== sym));
                        setShortSymbols((prev) => prev.filter((x) => x !== sym));
                      }}
                    >
                      <X className="h-3 w-3" aria-hidden="true" />
                    </button>
                  </span>
                ))
              )}
            </div>
          </div>
        )}

        {/* Fast Search & Arbitrary Ticker Input */}
        <div className="flex flex-col gap-1.5">
          <label htmlFor="l1-leader-search" className="text-xs font-medium text-ink">
            搜索中文名或直接输入任意代码（回车添加{isPair ? `到${activeLeg === "short" ? "空头腿" : "多头腿"}` : ""}）
          </label>
          <div className="flex gap-2">
            <div className="input flex flex-1 items-center gap-2 focus-within:border-accent focus-within:bg-surface">
              <Search
                className="h-3.5 w-3.5 shrink-0 text-muted"
                aria-hidden="true"
              />
              <input
                ref={inputRef}
                id="l1-leader-search"
                type="text"
                className="w-full min-w-0 border-0 bg-transparent p-0 text-xs text-ink outline-none placeholder:text-muted"
                style={{ outline: "none" }}
                placeholder="如：MOS, NTR, 600096.SS / 铝 / 日元 / 澳元（逗号分隔可一次加多只）"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleAddFromInput();
                  }
                }}
              />
            </div>
            <button
              type="button"
              className="btn btn-sm shrink-0"
              disabled={!query.trim()}
              onClick={handleAddFromInput}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              <span>添加</span>
            </button>
          </div>

          {suggestions.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 rounded-md border border-hairline-soft bg-sunken/70 p-2">
              {suggestions.map((item) => {
                const isSelected = (targetLeg === "short" ? shortSymbols : longSymbols).includes(
                  item.symbol.toUpperCase(),
                );
                return (
                  <button
                    key={item.symbol}
                    type="button"
                    className="chip text-[11px]"
                    aria-pressed={isSelected}
                    onClick={() => {
                      addSymbolToLeg(item.symbol, targetLeg);
                      setQuery("");
                      setSuggestions([]);
                    }}
                  >
                    <SymbolLabel symbol={item.symbol} name={item.label} size="sm" />
                  </button>
                );
              })}
            </div>
          ) : searching ? (
            <div className="text-[11px] text-muted">检索本地标的词典中…</div>
          ) : null}
        </div>

        {/* User's Own Saved Custom Pools (One-tap pick individual symbols or whole custom pool) */}
        {customPools.length > 0 ? (
          <div className="flex flex-col gap-1.5 rounded-md border border-hairline-soft bg-elevated/40 p-2.5">
            <span className="text-[11px] font-semibold text-ink-2">我的自建主题池（点击单个标的加入）</span>
            <div className="flex flex-col gap-2">
              {customPools.map((pool) => (
                <div key={pool.key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="font-medium text-ink">{pool.label}</span>
                    <button
                      type="button"
                      className="cursor-pointer border-0 bg-transparent p-0 text-[10.5px] text-accent hover:underline"
                      onClick={() => {
                        for (const s of pool.symbols) addSymbolToLeg(s, targetLeg);
                      }}
                    >
                      全选此自建池 ({pool.symbols.length})
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {pool.symbols.map((sym) => {
                      const active = (targetLeg === "short" ? shortSymbols : longSymbols).includes(sym.toUpperCase());
                      return (
                        <button
                          key={`${pool.key}-${sym}`}
                          type="button"
                          className="chip py-0.5 text-[11px]"
                          aria-pressed={active}
                          onClick={() => toggleSymbolOnActiveLeg(sym)}
                        >
                          <SymbolLabel symbol={sym} size="sm" />
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {/* Optional Individual Candidates from Reference Anchors / Matched Built-in Pool (collapsed if large, single-pick only!) */}
        {(card.reference_anchors?.length || referencePoolCandidates.length > 0) ? (
          <details className="rounded-md border border-hairline-soft bg-sunken/40 p-2.5" open={!customPools.length}>
            <summary className="cursor-pointer select-none text-[11px] font-medium text-muted hover:text-ink">
              点选单个参考标的（仅作单标点选，绝不自动整池全选）
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              {(card.reference_anchors ?? []).length > 0 ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10.5px] text-muted">基准参考锚：</span>
                  {(card.reference_anchors ?? []).map((sym) => {
                    const active = (targetLeg === "short" ? shortSymbols : longSymbols).includes(sym.toUpperCase());
                    return (
                      <button
                        key={`ref-${sym}`}
                        type="button"
                        className="chip py-0.5 text-[11px]"
                        aria-pressed={active}
                        onClick={() => toggleSymbolOnActiveLeg(sym)}
                      >
                        <SymbolLabel symbol={sym} size="sm" />
                      </button>
                    );
                  })}
                </div>
              ) : null}
              {referencePoolCandidates.map((sec) => (
                <div key={sec.poolKey} className="flex flex-col gap-1">
                  <span className="text-[10.5px] text-muted">{sec.label}（请只挑符合本逻辑的 1~2 只）：</span>
                  <div className="flex flex-wrap gap-1">
                    {sec.symbols.map((sym) => {
                      const active = (targetLeg === "short" ? shortSymbols : longSymbols).includes(sym.toUpperCase());
                      return (
                        <button
                          key={`${sec.poolKey}-${sym}`}
                          type="button"
                          className="chip py-0.5 text-[11px]"
                          aria-pressed={active}
                          onClick={() => toggleSymbolOnActiveLeg(sym)}
                        >
                          <SymbolLabel symbol={sym} size="sm" />
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </details>
        ) : null}

        {/* Optional: Simultaneously save picked symbols as user's own custom theme pool */}
        <div className="flex flex-col gap-2 rounded-md border border-hairline-soft bg-sunken/40 px-3 py-2">
          <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={saveAsPool}
              onChange={(e) => {
                setSaveAsPool(e.target.checked);
                setPoolConflict(false);
                setAllowOverwrite(false);
              }}
              className="accent-accent"
            />
            <BookmarkPlus className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
            <span>同时沉淀为我的专属主题池（保存至 universes.json，下次直接复用）</span>
          </label>
          {saveAsPool ? (
            <div className="grid grid-cols-1 gap-2 pt-1 sm:grid-cols-2">
              <input
                className="input font-mono text-xs"
                placeholder="英文池代号，如 phosphate_sulfur_leaders"
                value={poolName}
                onChange={(e) => {
                  setPoolName(e.target.value);
                  setPoolConflict(false);
                  setAllowOverwrite(false);
                }}
              />
              <input
                className="input text-xs"
                placeholder="1Q~1Y 板块逻辑说明"
                value={poolDesc}
                onChange={(e) => setPoolDesc(e.target.value)}
              />
              {poolConflict ? (
                <label className="col-span-full inline-flex cursor-pointer items-center gap-2 rounded border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-[11px] text-warning">
                  <input
                    type="checkbox"
                    checked={allowOverwrite}
                    onChange={(e) => setAllowOverwrite(e.target.checked)}
                    className="accent-warning"
                  />
                  <span>池代号「{poolName.trim()}」已存在，确认覆盖？</span>
                </label>
              ) : null}
            </div>
          ) : null}
        </div>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-critical">
            {err}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-hairline-soft pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={submitting || scanBusy || (allSelectedSymbols.length === 0 && !query.trim())}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            <span>
              {submitting || scanBusy
                ? "正在启动日线过筛…"
                : `日线缠论过筛（${effectiveSymbolCount} 只专属标的）`}
            </span>
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
