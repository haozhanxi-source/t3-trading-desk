import { useCallback, useEffect, useState } from "react";
import {
  BookOpen,
  Command,
  Crosshair,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Radar,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { api } from "./api/client";
import type { HealthResponse, MacroQuote, ScreenId } from "./api/types";
import { CommandPalette } from "./components/shell/CommandPalette";
import { fmtFixed, fmtPct, toNum } from "./lib/format";
import { useTheme, type ThemeChoice } from "./lib/theme";
import { ArchiveView } from "./views/ArchiveView";
import { SentinelView } from "./views/SentinelView";
import { WarRoomView } from "./views/WarRoomView";
import { WorkbenchView } from "./views/WorkbenchView";

const NAV_ITEMS: {
  id: ScreenId;
  label: string;
  shortLabel: string;
  layerTag: string;
  Icon: typeof ShieldCheck;
}[] = [
  { id: "sentinel", label: "哨兵总控", shortLabel: "哨兵", layerTag: "L4 持仓防线", Icon: ShieldCheck },
  { id: "warroom", label: "双周期战情室", shortLabel: "战情室", layerTag: "L2+L4 缠论", Icon: Crosshair },
  { id: "archive", label: "研究档案室", shortLabel: "档案室", layerTag: "L3 硬数据", Icon: BookOpen },
  { id: "workbench", label: "投研任务台", shortLabel: "任务台", layerTag: "L1+L2 扫描", Icon: Radar },
];

function readInitialRoute(): { screen: ScreenId; symbol: string; market: string | null } {
  if (typeof window === "undefined") return { screen: "sentinel", symbol: "AUDUSD=X", market: null };
  const params = new URLSearchParams(window.location.search);
  const s = params.get("screen");
  const sym = params.get("symbol")?.trim() || "AUDUSD=X";
  const mkt = params.get("market")?.trim().toUpperCase() || null;
  const screen: ScreenId =
    s === "sentinel" || s === "warroom" || s === "archive" || s === "workbench" ? s : "sentinel";
  return { screen, symbol: sym, market: mkt };
}

function extractMacroQuote(raw: Record<string, unknown>, keys: string[]): MacroQuote | null {
  for (const k of keys) {
    const v = raw[k];
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      const last = toNum(o.last ?? o.close ?? o.value);
      if (last !== null) {
        return { last, chg_1d_pct: toNum(o.chg_1d_pct ?? o.change_pct) };
      }
    }
    const direct = toNum(v);
    if (direct !== null) return { last: direct };
  }
  return null;
}

export function App() {
  const { choice, setChoice } = useTheme();
  const initial = readInitialRoute();

  const [screen, setScreen] = useState<ScreenId>(initial.screen);
  const [activeSymbol, setActiveSymbol] = useState<string>(initial.symbol);
  const [activeMarket, setActiveMarket] = useState<string | null>(initial.market);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [macro, setMacro] = useState<Record<string, unknown>>({});

  /* Sync screen & symbol into URL query params cleanly */
  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("screen", screen);
    if (activeSymbol) url.searchParams.set("symbol", activeSymbol);
    if (screen === "archive" && activeMarket) {
      url.searchParams.set("market", activeMarket);
    } else {
      url.searchParams.delete("market");
    }
    window.history.replaceState(null, "", url.pathname + url.search);
  }, [screen, activeSymbol, activeMarket]);

  /* Global Cmd+K / Ctrl+K shortcut */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  /* Fetch backend health on mount */
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .health(ctrl.signal)
      .then((h) => setHealth(h))
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [screen]);

  const handleJumpWarRoom = useCallback((sym: string) => {
    setActiveSymbol(sym.trim().toUpperCase());
    setActiveMarket(null);
    setScreen("warroom");
  }, []);

  const handleJumpArchive = useCallback((sym: string, market?: string | null) => {
    setActiveSymbol(sym.trim().toUpperCase());
    setActiveMarket(market ? market.trim().toUpperCase() : null);
    setScreen("archive");
  }, []);

  const handlePaletteJump = useCallback((sym: string, target: "warroom" | "archive") => {
    setActiveSymbol(sym.trim().toUpperCase());
    setActiveMarket(null);
    setScreen(target);
  }, []);

  const vix = extractMacroQuote(macro, ["VIX", "^VIX", "vix"]);
  const us10y = extractMacroQuote(macro, ["US10Y_Yield", "US10Y", "^TNX", "us10y", "DGS10"]);
  const dxy = extractMacroQuote(macro, ["DXY_USD_Index", "DXY", "DX-Y.NYB", "dxy"]);

  const themeOptions: { id: ThemeChoice; label: string; Icon: typeof Moon }[] = [
    { id: "dark", label: "暗色", Icon: Moon },
    { id: "light", label: "亮色", Icon: Sun },
    { id: "system", label: "系统", Icon: Monitor },
  ];

  return (
    <div className="flex min-h-dvh flex-col bg-page text-ink">
      {/* Slim Top Global Shell Bar */}
      <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center justify-between gap-3 border-b border-hairline bg-surface px-3.5">
        <div className="flex min-w-0 items-center gap-2.5">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setRailCollapsed((c) => !c)}
            aria-label={railCollapsed ? "展开左侧导航栏" : "收起左侧导航栏"}
          >
            {railCollapsed ? (
              <PanelLeftOpen className="h-4 w-4 text-ink-2" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="h-4 w-4 text-ink-2" aria-hidden="true" />
            )}
          </button>

          <div className="flex items-baseline gap-2 truncate">
            <span className="text-sm font-semibold tracking-tight text-ink">T3 Trading Desk</span>
            <span className="hidden text-xs text-muted sm:inline">中低频投研工作台 · 日线定方向 + 30min 缠论落地</span>
          </div>
        </div>

        {/* Macro Strip (populated dynamically when War Room loads macro_context, zero noisy blinking) */}
        <div className="tnum hidden items-center gap-3 text-xs text-ink-2 xl:flex" aria-label="全球宏观底座快照">
          {vix ? (
            <span>
              VIX <strong className="text-ink">{fmtFixed(vix.last, 2)}</strong>{" "}
              {vix.chg_1d_pct != null ? <span className="text-muted">({fmtPct(vix.chg_1d_pct, 1, true)})</span> : null}
            </span>
          ) : null}
          {us10y ? (
            <span>
              US10Y <strong className="text-ink">{fmtFixed(us10y.last, 2)}%</strong>
            </span>
          ) : null}
          {dxy ? (
            <span>
              DXY <strong className="text-ink">{fmtFixed(dxy.last, 2)}</strong>{" "}
              {dxy.chg_1d_pct != null ? <span className="text-muted">({fmtPct(dxy.chg_1d_pct, 1, true)})</span> : null}
            </span>
          ) : null}
          {health ? (
            <span className="rounded border border-hairline bg-page px-2 py-0.5 text-[11px] text-muted">
              档案 {health.archive_count} · 主题池 {health.universe_count}
            </span>
          ) : null}
        </div>

        {/* Right Controls: Cmd+K Trigger + Theme Switcher */}
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            className="btn btn-sm bg-page text-ink-2"
            onClick={() => setPaletteOpen(true)}
            aria-keyshortcuts="Meta+K Control+K"
          >
            <Command className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
            <span>⌘K 跳转标的/视图</span>
          </button>

          <div className="flex items-center rounded-md border border-hairline bg-page p-0.5" role="group" aria-label="界面主题模式切换">
            {themeOptions.map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                aria-pressed={choice === id}
                onClick={() => setChoice(id)}
                title={`切换为${label}主题`}
                className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11.5px] cursor-pointer ${
                  choice === id ? "bg-elevated font-medium text-ink" : "text-muted hover:text-ink-2"
                }`}
              >
                <Icon className="h-3 w-3" aria-hidden="true" />
                <span className="hidden md:inline">{label}</span>
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* Body: Left Navigation Rail + Active Screen */}
      <div className="flex flex-1 min-w-0">
        <nav
          aria-label="四层投研工作流主导航"
          className={`shrink-0 border-r border-hairline bg-surface ${
            railCollapsed ? "w-14" : "w-14 md:w-52"
          }`}
        >
          <ul className="m-0 flex list-none flex-col gap-1 p-2">
            {NAV_ITEMS.map(({ id, label, shortLabel, layerTag, Icon }) => {
              const active = screen === id;
              return (
                <li key={id}>
                  <button
                    type="button"
                    aria-current={active ? "page" : undefined}
                    onClick={() => setScreen(id)}
                    title={`${label} (${layerTag})`}
                    className={`flex w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-xs ${
                      active
                        ? "bg-elevated font-semibold text-ink border border-hairline"
                        : "border border-transparent text-ink-2 hover:bg-elevated/60"
                    }`}
                  >
                    <Icon className={`h-4 w-4 shrink-0 ${active ? "text-slot1" : "text-muted"}`} aria-hidden="true" />
                    {railCollapsed ? (
                      <span className="sr-only">{label}</span>
                    ) : (
                      <div className="hidden min-w-0 flex-1 md:block">
                        <div className="truncate">{label}</div>
                        <div className="truncate text-[10.5px] font-normal text-muted">{layerTag}</div>
                      </div>
                    )}
                    {!railCollapsed ? <span className="sr-only md:hidden">{shortLabel}</span> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <main className="min-w-0 flex-1 p-3.5 md:p-5">
          {screen === "sentinel" ? (
            <SentinelView onJumpWarRoom={handleJumpWarRoom} onJumpArchive={handleJumpArchive} />
          ) : null}

          {screen === "warroom" ? (
            <WarRoomView
              symbol={activeSymbol}
              portfolioInitialized={health?.portfolio_initialized}
              onSelectSymbol={(s) => setActiveSymbol(s.trim().toUpperCase())}
              onJumpArchive={handleJumpArchive}
              onMacroContext={setMacro}
              onPortfolioChanged={() => {
                api
                  .health()
                  .then((h) => setHealth(h))
                  .catch(() => undefined);
              }}
            />
          ) : null}

          {screen === "archive" ? (
            <ArchiveView
              symbol={activeSymbol}
              market={activeMarket}
              onSelectSymbol={(s, mkt) => {
                setActiveSymbol(s.trim().toUpperCase());
                setActiveMarket(mkt ? mkt.trim().toUpperCase() : null);
              }}
              onJumpWarRoom={handleJumpWarRoom}
            />
          ) : null}

          {screen === "workbench" ? (
            <WorkbenchView
              defaultSymbol={activeSymbol}
              onJumpWarRoom={handleJumpWarRoom}
              onJumpArchive={handleJumpArchive}
            />
          ) : null}
        </main>
      </div>

      {/* Native <dialog closedby="any"> Command Palette */}
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNavigateScreen={(s) => setScreen(s)}
        onJumpSymbol={handlePaletteJump}
      />
    </div>
  );
}
