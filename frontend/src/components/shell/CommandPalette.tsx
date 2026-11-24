import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { ArrowRight, BookOpen, Crosshair, Radar, Search, ShieldCheck } from "lucide-react";
import { api } from "../../api/client";
import type { ScreenId, SymbolSearchItem } from "../../api/types";
import { getSymbolName } from "../../lib/symbols";
import { getUniverseLabel } from "../../lib/universes";
import { ModalDialog } from "../ui/ModalDialog";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onNavigateScreen: (screen: ScreenId) => void;
  onJumpSymbol: (symbol: string, target: "warroom" | "archive") => void;
}

interface PaletteEntry {
  key: string;
  kind: "screen" | "warroom" | "archive";
  title: string;
  subtitle: string;
  badge?: string;
  run: () => void;
}

const SCREEN_ENTRIES: { id: ScreenId; title: string; subtitle: string }[] = [
  { id: "sentinel", title: "1 · 哨兵总控", subtitle: "持仓红黄绿每日防线、单一板块龙头占用与 14 天事件窗口" },
  { id: "warroom", title: "2 · 双周期战情室", subtitle: "3 年日线定方向 + 30min 缠论中枢/笔/MACD 与四象限算仓" },
  { id: "archive", title: "3 · 研究档案室", subtitle: "L1~L4 证据库、thesis.md 论点与按品种精准路由硬数据图表" },
  { id: "workbench", title: "4 · 投研任务台", subtitle: "板块龙头缠论扫描、SSE 实时执行流与新主题池沉淀" },
];

export function CommandPalette({ open, onClose, onNavigateScreen, onJumpSymbol }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<SymbolSearchItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [targetMode, setTargetMode] = useState<"warroom" | "archive">("warroom");

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      api
        .searchSymbols(query.trim(), 20, ctrl.signal)
        .then((res) => {
          setItems(res.items ?? []);
          setActiveIndex(0);
        })
        .catch(() => {
          if (!ctrl.signal.aborted) setItems([]);
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, 90);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [open, query]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const out: PaletteEntry[] = [];
    const q = query.trim().toLowerCase();

    /* Direct custom symbol jump when user types a ticker not at top of catalog */
    if (q && /^[a-z0-9.=:-]{1,16}$/i.test(q)) {
      const exact = items.some((it) => it.symbol.toLowerCase() === q);
      if (!exact) {
        const rawSym = query.trim().toUpperCase();
        out.push({
          key: `direct-${rawSym}`,
          kind: targetMode,
          title: `${rawSym} → ${targetMode === "warroom" ? "双周期战情室" : "研究档案室"}`,
          subtitle: "直接输入代码跳转实时行情与缠论计算",
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
          badge: "视图",
          run: () => {
            onNavigateScreen(s.id);
            onClose();
          },
        });
      }
    }

    for (const it of items) {
      const zhName = (it.label && it.label.toUpperCase() !== it.symbol.toUpperCase() ? it.label : null) ?? getSymbolName(it.symbol);
      out.push({
        key: `sym-${it.symbol}-${targetMode}`,
        kind: targetMode,
        title: zhName ? `${zhName} · ${it.symbol}` : it.symbol,
        subtitle: [
          it.asset_class,
          it.pool ? `板块:${getUniverseLabel(it.pool)}` : null,
          it.status ? `状态:${it.status}` : null,
          targetMode === "warroom" ? "回车打开双周期战情室" : "回车打开研究档案室",
        ]
          .filter(Boolean)
          .join(" · "),
        badge: it.status === "holding" ? "持仓" : it.sources.includes("archive") ? "已建档" : it.asset_class,
        run: () => {
          onJumpSymbol(it.symbol, targetMode);
          onClose();
        },
      });
    }

    return out;
  }, [items, onClose, onJumpSymbol, onNavigateScreen, query, targetMode]);

  const safeIndex = entries.length === 0 ? 0 : Math.min(activeIndex, entries.length - 1);

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (entries.length > 0) setActiveIndex((i) => (i + 1) % entries.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (entries.length > 0) setActiveIndex((i) => (i - 1 + entries.length) % entries.length);
    } else if (e.key === "Enter") {
      const picked = entries[safeIndex];
      if (picked) {
        e.preventDefault();
        picked.run();
      }
    } else if (e.key === "Tab" && !e.shiftKey) {
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
      <div onKeyDown={handleKeyDown} className="flex max-h-[72dvh] flex-col">
        <div className="flex items-center gap-2.5 border-b border-hairline px-4 py-3">
          <Search className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
          <input
            type="search"
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索股票/期货/外汇/加密代码或中文别名（如 CU0、沪铜、NVDA、AUDUSD=X、600519）…"
            aria-label="搜索标的或工作台视图"
            className="w-full border-0 bg-transparent text-sm text-ink placeholder:text-muted focus:outline-none"
          />
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              className="chip"
              aria-pressed={targetMode === "warroom"}
              onClick={() => setTargetMode("warroom")}
              title="Tab 切换跳转目标"
            >
              <Crosshair className="h-3 w-3" aria-hidden="true" />
              <span>战情室</span>
            </button>
            <button
              type="button"
              className="chip"
              aria-pressed={targetMode === "archive"}
              onClick={() => setTargetMode("archive")}
              title="Tab 切换跳转目标"
            >
              <BookOpen className="h-3 w-3" aria-hidden="true" />
              <span>档案室</span>
            </button>
          </div>
        </div>

        <ul
          role="listbox"
          aria-label="快速跳转候选列表"
          className="m-0 flex-1 list-none overflow-y-auto p-2"
        >
          {entries.length === 0 ? (
            <li className="px-3 py-8 text-center text-xs text-muted">
              {loading ? "正在检索标的目录…" : "无匹配项，可直接输入任意美股/A股/期货代码回车跳转"}
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
                  key={entry.key}
                  role="option"
                  aria-selected={active}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onClick={entry.run}
                  className={`flex cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2 text-xs ${
                    active ? "bg-elevated text-ink" : "text-ink-2"
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    <Icon className="h-4 w-4 shrink-0 text-slot1" aria-hidden="true" />
                    <div className="min-w-0">
                      <div className="truncate font-medium text-ink">{entry.title}</div>
                      <div className="truncate text-[11px] text-muted">{entry.subtitle}</div>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {entry.badge ? (
                      <span className="rounded border border-hairline bg-page px-1.5 py-0.5 text-[11px] text-muted">
                        {entry.badge}
                      </span>
                    ) : null}
                    <ArrowRight className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
                  </div>
                </li>
              );
            })
          )}
        </ul>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline px-4 py-2 text-[11px] text-muted">
          <span>↑↓ 选择 · Enter 跳转 · Tab 切换目标（战情室 / 档案室）</span>
          <span>Esc 或点击遮罩关闭</span>
        </footer>
      </div>
    </ModalDialog>
  );
}
