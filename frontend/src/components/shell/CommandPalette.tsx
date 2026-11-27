import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { ArrowRight, BookOpen, Crosshair, Radar, Search, ShieldCheck } from "lucide-react";
import { api } from "../../api/client";
import type { ScreenId, SymbolSearchItem } from "../../api/types";
import { getSymbolName } from "../../lib/symbols";
import { getUniverseLabel } from "../../lib/universes";
import { ModalDialog } from "../ui/ModalDialog";
import { SymbolLabel } from "../ui/SymbolLabel";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onNavigateScreen: (screen: ScreenId) => void;
  onJumpSymbol: (symbol: string, target: "warroom" | "archive") => void;
}

interface PaletteEntry {
  key: string;
  kind: "screen" | "warroom" | "archive";
  symbol?: string;
  symbolName?: string | null;
  title: string;
  subtitle: string;
  badge?: string;
  run: () => void;
}

const SCREEN_ENTRIES: { id: ScreenId; title: string; subtitle: string; badge: string }[] = [
  { id: "sentinel", title: "1 · 哨兵总控", subtitle: "持仓红黄绿每日防线、板块龙头独占与原子换仓、14天事件窗口", badge: "Alt+1" },
  { id: "workbench", title: "2 · 机会扫描", subtitle: "板块龙头日线缠论过筛、多空分级过滤与一键收入观察池", badge: "Alt+2" },
  { id: "archive", title: "3 · 研报档案", subtitle: "thesis.md 深度研报、L1~L4 证据库、就地硬数据刷新与触发器", badge: "Alt+3" },
  { id: "warroom", title: "4 · 缠论战情室", subtitle: "日线 × 30min 区间套结构卡尺、点图取价与四象限算仓入账", badge: "Alt+4" },
];

export function CommandPalette({ open, onClose, onNavigateScreen, onJumpSymbol }: CommandPaletteProps) {
  const listboxId = useId();
  const [query, setQuery] = useState("");
  const [resultBundle, setResultBundle] = useState<{ q: string; items: SymbolSearchItem[] }>({
    q: "",
    items: [],
  });
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [targetMode, setTargetMode] = useState<"warroom" | "archive">("warroom");
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
  }, [open]);

  const trimmedQuery = query.trim();

  useEffect(() => {
    if (!open) return;
    const ctrl = new AbortController();
    const reqQuery = trimmedQuery;
    setLoading(true);
    const timer = window.setTimeout(() => {
      api
        .searchSymbols(reqQuery, 20, ctrl.signal)
        .then((res) => {
          if (ctrl.signal.aborted) return;
          setResultBundle({ q: reqQuery, items: res.items ?? [] });
          setActiveIndex(0);
        })
        .catch(() => {
          if (!ctrl.signal.aborted) {
            setResultBundle({ q: reqQuery, items: [] });
          }
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, 80);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [open, trimmedQuery]);

  /* Never expose stale symbol items belonging to a previous query while debounce/fetch is in-flight */
  const matchedItems = resultBundle.q === trimmedQuery ? resultBundle.items : [];

  const entries = useMemo<PaletteEntry[]>(() => {
    const out: PaletteEntry[] = [];
    const q = trimmedQuery.toLowerCase();

    if (q && /^[a-z0-9.=:-]{1,16}$/i.test(q)) {
      const exact = matchedItems.some((it) => it.symbol.toLowerCase() === q);
      if (!exact) {
        const rawSym = trimmedQuery.toUpperCase();
        out.push({
          key: `direct-${rawSym}`,
          kind: targetMode,
          symbol: rawSym,
          symbolName: getSymbolName(rawSym),
          title: `${rawSym} → ${targetMode === "warroom" ? "缠论战情室" : "研报档案"}`,
          subtitle: "直接输入任意代码拉取实时行情与缠论结构",
          badge: "直达",
          run: () => {
            onJumpSymbol(rawSym, targetMode);
            onClose();
          },
        });
      }
    }

    for (const s of SCREEN_ENTRIES) {
      if (!q || s.title.toLowerCase().includes(q) || s.subtitle.toLowerCase().includes(q) || s.id.includes(q)) {
        out.push({
          key: `screen-${s.id}`,
          kind: "screen",
          title: s.title,
          subtitle: s.subtitle,
          badge: s.badge,
          run: () => {
            onNavigateScreen(s.id);
            onClose();
          },
        });
      }
    }

    for (const it of matchedItems) {
      const zhName =
        (it.label && it.label.toUpperCase() !== it.symbol.toUpperCase() ? it.label : null) ??
        getSymbolName(it.symbol);
      out.push({
        key: `sym-${it.symbol}-${targetMode}`,
        kind: targetMode,
        symbol: it.symbol,
        symbolName: zhName,
        title: zhName ? `${zhName} ${it.symbol}` : it.symbol,
        subtitle: [
          it.asset_class,
          it.pool ? getUniverseLabel(it.pool) : null,
          targetMode === "warroom" ? "回车进入缠论战情室" : "回车打开研报档案",
        ]
          .filter(Boolean)
          .join(" · "),
        badge: it.status === "holding" ? "持仓中" : it.sources.includes("archive") ? "已建档" : it.asset_class,
        run: () => {
          onJumpSymbol(it.symbol, targetMode);
          onClose();
        },
      });
    }

    return out;
  }, [matchedItems, onClose, onJumpSymbol, onNavigateScreen, targetMode, trimmedQuery]);

  const safeIndex = entries.length === 0 ? -1 : Math.min(activeIndex, entries.length - 1);
  const activeOptionId = safeIndex >= 0 ? `${listboxId}-opt-${safeIndex}` : undefined;

  /* Keep active listbox option scrolled into view during keyboard navigation */
  useEffect(() => {
    if (safeIndex < 0 || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[id="${CSS.escape(`${listboxId}-opt-${safeIndex}`)}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [listboxId, safeIndex]);

  /* Scoped strictly to the combobox input; skips IME composition and preserves native Tab focus order */
  const handleInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (entries.length > 0) setActiveIndex((i) => (i + 1) % entries.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (entries.length > 0) setActiveIndex((i) => (i - 1 + entries.length) % entries.length);
    } else if (e.key === "Enter") {
      if (safeIndex >= 0 && entries[safeIndex]) {
        e.preventDefault();
        entries[safeIndex].run();
      }
    } else if (e.altKey && e.key.toLowerCase() === "m") {
      e.preventDefault();
      setTargetMode((m) => (m === "warroom" ? "archive" : "warroom"));
    }
  };

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title="快速跳转标的或工作台视图"
      lightDismiss
      hideHeader
      className="palette mx-auto mt-[10vh] mb-auto"
    >
      <div className="flex max-h-[72dvh] flex-col">
        <div className="flex items-center gap-2.5 border-b border-hairline px-4 py-2.5 transition-colors focus-within:border-accent focus-within:bg-elevated/40">
          <Search className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
          <input
            type="search"
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-activedescendant={activeOptionId}
            aria-autocomplete="list"
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="输入标的代码或中文名称（如 沪铜、CU0、NVDA、AUDUSD=X、600519）…"
            aria-label="搜索标的或工作台视图"
            className="w-full border-0 bg-transparent text-sm text-ink placeholder:text-muted focus:outline-none"
          />
          <div className="segmented shrink-0" role="group" aria-label="回车跳转目标模式">
            <button
              type="button"
              className="segmented-btn px-2 py-0.5 text-[11px]"
              aria-pressed={targetMode === "warroom"}
              onClick={() => setTargetMode("warroom")}
              title="切换回车跳转至战情室 (Alt+M)"
            >
              <Crosshair className="h-3 w-3" aria-hidden="true" />
              <span>战情室</span>
            </button>
            <button
              type="button"
              className="segmented-btn px-2 py-0.5 text-[11px]"
              aria-pressed={targetMode === "archive"}
              onClick={() => setTargetMode("archive")}
              title="切换回车跳转至档案室 (Alt+M)"
            >
              <BookOpen className="h-3 w-3" aria-hidden="true" />
              <span>档案室</span>
            </button>
          </div>
        </div>

        <ul
          id={listboxId}
          ref={listRef}
          role="listbox"
          aria-label="快速跳转候选列表"
          className="m-0 flex-1 list-none overflow-y-auto p-1.5"
        >
          {entries.length === 0 ? (
            <li className="px-3 py-8 text-center text-xs text-muted">
              {loading || resultBundle.q !== trimmedQuery
                ? "正在检索标的目录…"
                : "无匹配项，输入任意美股/A股/期货英文或数字代码回车即可直达"}
            </li>
          ) : (
            entries.map((entry, idx) => {
              const active = idx === safeIndex;
              const Icon =
                entry.kind === "screen"
                  ? entry.key.includes("sentinel")
                    ? ShieldCheck
                    : entry.key.includes("workbench")
                      ? Radar
                      : entry.key.includes("archive")
                        ? BookOpen
                        : Crosshair
                  : entry.kind === "archive"
                    ? BookOpen
                    : Crosshair;
              return (
                <li
                  id={`${listboxId}-opt-${idx}`}
                  key={entry.key}
                  role="option"
                  aria-selected={active}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onClick={entry.run}
                  className={`flex cursor-pointer items-center justify-between gap-3 rounded-md px-3 py-2 text-xs ${
                    active ? "bg-elevated text-ink" : "text-ink-2"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    <Icon
                      className={`h-4 w-4 shrink-0 ${active ? "text-accent" : "text-muted"}`}
                      aria-hidden="true"
                    />
                    <div className="min-w-0">
                      {entry.symbol ? (
                        <SymbolLabel symbol={entry.symbol} name={entry.symbolName} size="sm" />
                      ) : (
                        <div className="truncate font-medium text-ink">{entry.title}</div>
                      )}
                      <div className="truncate text-[11px] text-muted">{entry.subtitle}</div>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {entry.badge ? (
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[10.5px] ${
                          entry.badge === "持仓中"
                            ? "border-[color:color-mix(in_srgb,var(--bull)_35%,transparent)] bg-[color:color-mix(in_srgb,var(--bull)_12%,transparent)] text-bull"
                            : "border-hairline bg-sunken text-muted"
                        }`}
                      >
                        {entry.badge}
                      </span>
                    ) : null}
                    <ArrowRight className={`h-3.5 w-3.5 ${active ? "text-ink" : "text-muted/60"}`} aria-hidden="true" />
                  </div>
                </li>
              );
            })
          )}
        </ul>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline px-4 py-2 text-[11px] text-muted">
          <span>↑↓ 选择 · Enter 跳转 · Alt+M 切换战情室/档案室 · Tab 焦点流转</span>
          <span>Esc 关闭</span>
        </footer>
      </div>
    </ModalDialog>
  );
}
