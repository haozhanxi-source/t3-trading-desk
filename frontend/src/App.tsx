import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  BookOpen,
  Crosshair,
  Monitor,
  Moon,
  Radar,
  Search,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { api } from "./api/client";
import type { HealthResponse, MacroQuote, ScreenId } from "./api/types";
import { CommandPalette } from "./components/shell/CommandPalette";
import { signedToneClass } from "./components/ui/StatusBadge";
import { fmtFixed, fmtPct, shortTime, toNum } from "./lib/format";
import { getSymbolName } from "./lib/symbols";
import { useTheme } from "./lib/theme";
import { SentinelView } from "./views/SentinelView";

const WorkbenchView = lazy(() =>
  import("./views/WorkbenchView").then((m) => ({ default: m.WorkbenchView })),
);
const ArchiveView = lazy(() =>
  import("./views/ArchiveView").then((m) => ({ default: m.ArchiveView })),
);
const WarRoomView = lazy(() =>
  import("./views/WarRoomView").then((m) => ({ default: m.WarRoomView })),
);

const NAV_ITEMS: {
  id: ScreenId;
  num: string;
  label: string;
  shortLabel: string;
  Icon: typeof ShieldCheck;
}[] = [
  { id: "sentinel", num: "1", label: "哨兵总控", shortLabel: "哨兵", Icon: ShieldCheck },
  { id: "workbench", num: "2", label: "机会扫描", shortLabel: "扫描", Icon: Radar },
  { id: "archive", num: "3", label: "研报档案", shortLabel: "档案", Icon: BookOpen },
  { id: "warroom", num: "4", label: "缠论战情室", shortLabel: "战情", Icon: Crosshair },
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

function isEditableElement(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

export function App() {
  const { choice, systemDark, cycleChoice } = useTheme();
  const initial = readInitialRoute();

  const [screen, setScreenState] = useState<ScreenId>(initial.screen);
  const [visitedScreens, setVisitedScreens] = useState<Record<ScreenId, boolean>>({
    sentinel: initial.screen === "sentinel",
    workbench: initial.screen === "workbench",
    archive: initial.screen === "archive",
    warroom: initial.screen === "warroom",
  });
  const [activeSymbol, setActiveSymbol] = useState<string>(initial.symbol);
  const [activeMarket, setActiveMarket] = useState<string | null>(initial.market);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [macro, setMacro] = useState<Record<string, unknown>>({});
  const [macroUpdatedAt, setMacroUpdatedAt] = useState<string | null>(null);
  const [macroLoading, setMacroLoading] = useState(false);
  const [workspaceRev, setWorkspaceRev] = useState(0);

  const setScreen = useCallback((next: ScreenId) => {
    setScreenState(next);
    setVisitedScreens((prev) => (prev[next] ? prev : { ...prev, [next]: true }));
  }, []);

  const bumpWorkspaceRev = useCallback(() => {
    setWorkspaceRev((r) => r + 1);
    api
      .health()
      .then((h) => setHealth(h))
      .catch(() => undefined);
  }, []);

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

  /* Global Cmd+K / Ctrl+K + Alt+1~4 screen hotkeys */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
        const idx = Number(e.key) - 1;
        if (idx >= 0 && idx < NAV_ITEMS.length) {
          e.preventDefault();
          setScreen(NAV_ITEMS[idx].id);
          return;
        }
      }
      if (!e.metaKey && !e.ctrlKey && !e.altKey && !paletteOpen && !isEditableElement(e.target)) {
        if (e.key === "/") {
          e.preventDefault();
          setPaletteOpen(true);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [paletteOpen, setScreen]);

  const refreshMacroStrip = useCallback((force = false, signal?: AbortSignal) => {
    setMacroLoading(true);
    api
      .macroSnapshot(force, signal)
      .then((res) => {
        if (res.macro_context && Object.keys(res.macro_context).length > 0) {
          setMacro(res.macro_context);
        }
        if (res.fetched_at_utc) {
          setMacroUpdatedAt(res.fetched_at_utc);
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!signal?.aborted) setMacroLoading(false);
      });
  }, []);

  /* Fetch backend health + persistent macro strip on initial mount */
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .health(ctrl.signal)
      .then((h) => setHealth(h))
      .catch(() => undefined);
    refreshMacroStrip(false, ctrl.signal);
    return () => ctrl.abort();
  }, [refreshMacroStrip]);

  const handleJumpWarRoom = useCallback(
    (sym: string) => {
      setActiveSymbol(sym.trim().toUpperCase());
      setActiveMarket(null);
      setScreen("warroom");
    },
    [setScreen],
  );

  const handleJumpArchive = useCallback(
    (sym: string, market?: string | null) => {
      setActiveSymbol(sym.trim().toUpperCase());
      setActiveMarket(market ? market.trim().toUpperCase() : null);
      setScreen("archive");
    },
    [setScreen],
  );

  const handlePaletteJump = useCallback(
    (sym: string, target: "warroom" | "archive") => {
      setActiveSymbol(sym.trim().toUpperCase());
      setActiveMarket(null);
      setScreen(target);
    },
    [setScreen],
  );

  const vix = extractMacroQuote(macro, ["VIX", "^VIX", "vix"]);
  const us10y = extractMacroQuote(macro, ["US10Y_Yield", "US10Y", "^TNX", "us10y", "DGS10"]);
  const dxy = extractMacroQuote(macro, ["DXY_USD_Index", "DXY", "DX-Y.NYB", "dxy"]);
  const hasMacro = Boolean(vix || us10y || dxy);

  const activeSymZh = getSymbolName(activeSymbol);
  const ThemeIcon = choice === "dark" ? Moon : choice === "light" ? Sun : Monitor;
  const themeTitle =
    choice === "dark"
      ? "当前：暗色曜石主题（点击切换至亮色）"
      : choice === "light"
        ? "当前：亮色冷雾主题（点击切换至跟随系统）"
        : `当前：跟随系统（${systemDark ? "暗色" : "亮色"}，点击切换至固定暗色）`;

  return (
    <div className="flex min-h-dvh flex-col bg-page text-ink">
      {/* Compact Single-Row Precision Header */}
      <header className="sticky top-0 z-20 border-b border-hairline bg-surface/94 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1680px] flex-wrap items-center justify-between gap-2 px-4 py-2 md:px-6">
          {/* Left: Brand Mark */}
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent font-mono text-[11px] font-bold tracking-tight text-accent-fg"
              aria-hidden="true"
            >
              T3
            </span>
            <div className="flex min-w-0 items-baseline gap-2 truncate">
              <span className="text-[13px] font-semibold tracking-tight text-ink">Trading Desk</span>
              <span className="hidden text-[11px] text-muted md:inline">缠论投研台</span>
            </div>
          </div>

          {/* Center: Ordered Workflow Navigation (Alt+1~4) */}
          <nav
            aria-label="四层投研工作流主导航"
            className="segmented order-3 w-full justify-between overflow-x-auto sm:order-2 sm:w-auto"
          >
            {NAV_ITEMS.map(({ id, num, label, shortLabel, Icon }) => {
              const active = screen === id;
              return (
                <button
                  key={id}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  title={`${label} (Alt+${num})`}
                  onClick={() => setScreen(id)}
                  className="segmented-btn flex-1 justify-center sm:flex-initial"
                >
                  <Icon
                    className={`h-3.5 w-3.5 shrink-0 ${active ? "text-accent" : "text-muted"}`}
                    aria-hidden="true"
                  />
                  <span className="hidden sm:inline">{label}</span>
                  <span className="sm:hidden">{shortLabel}</span>
                </button>
              );
            })}
          </nav>

          {/* Right: Persistent Macro Strip + Active Symbol Context Slot + Theme Cycle */}
          <div className="order-2 flex shrink-0 items-center gap-2 sm:order-3">
            {hasMacro ? (
              <button
                type="button"
                onClick={() => refreshMacroStrip(true)}
                disabled={macroLoading}
                title={`全球宏观底座快照${macroUpdatedAt ? `（抓取时间 ${shortTime(macroUpdatedAt)} UTC）` : ""} · 点击刷新`}
                className="tnum hidden cursor-pointer items-center gap-3 rounded-md border border-hairline-soft bg-sunken px-2.5 py-1 font-mono text-[11px] text-ink-2 transition-colors hover:bg-elevated lg:flex"
                aria-label="全球宏观底座快照，点击刷新"
              >
                {vix ? (
                  <span>
                    <span className="text-muted">VIX</span>{" "}
                    <strong className="font-semibold text-ink">{fmtFixed(vix.last, 2)}</strong>
                    {vix.chg_1d_pct != null ? (
                      <span className={`ml-1 ${signedToneClass(vix.chg_1d_pct)}`}>
                        {fmtPct(vix.chg_1d_pct, 1, true)}
                      </span>
                    ) : null}
                  </span>
                ) : null}
                {us10y ? (
                  <span>
                    <span className="text-muted">10Y</span>{" "}
                    <strong className="font-semibold text-ink">{fmtFixed(us10y.last, 2)}%</strong>
                  </span>
                ) : null}
                {dxy ? (
                  <span>
                    <span className="text-muted">DXY</span>{" "}
                    <strong className="font-semibold text-ink">{fmtFixed(dxy.last, 2)}</strong>
                  </span>
                ) : null}
              </button>
            ) : (
              <button
                type="button"
                onClick={() => refreshMacroStrip(true)}
                disabled={macroLoading}
                title="点击重试拉取全球宏观底座快照 (VIX / 10Y / DXY)"
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-hairline-soft bg-sunken px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-ink"
              >
                <span>{macroLoading ? "宏观拉取中…" : "宏观刷新"}</span>
              </button>
            )}

            {/* Global Active Symbol Context & Command Palette Trigger */}
            <button
              type="button"
              className="btn btn-sm bg-sunken text-ink-2 hover:bg-elevated"
              onClick={() => setPaletteOpen(true)}
              aria-keyshortcuts="Meta+K Control+K /"
              title="切换焦点标的或跳转视图 (⌘K / Ctrl+K / 按 / 键)"
            >
              <Search className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
              <span className="max-w-[140px] truncate text-xs font-medium text-ink">
                {activeSymZh || activeSymbol}
              </span>
              {activeSymZh ? (
                <span className="hidden font-mono text-[11px] text-muted sm:inline">{activeSymbol}</span>
              ) : null}
              <kbd className="rounded border border-hairline bg-surface px-1.5 py-0.2 font-mono text-[10px] text-muted">
                ⌘K
              </kbd>
            </button>

            <button
              type="button"
              onClick={cycleChoice}
              title={themeTitle}
              aria-label={themeTitle}
              className="btn btn-sm px-2 text-ink-2"
            >
              <ThemeIcon className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </header>

      {/* Keep-Alive Main Workspace: preserves scan results, chart viewports, and planner state across tabs */}
      <main className="mx-auto w-full max-w-[1680px] flex-1 px-4 py-4 md:px-6 md:py-5">
        {visitedScreens.sentinel ? (
          <div className={screen === "sentinel" ? "block" : "hidden"} aria-hidden={screen !== "sentinel"}>
            <SentinelView
              workspaceRev={workspaceRev}
              onPortfolioMutated={bumpWorkspaceRev}
              onJumpWarRoom={handleJumpWarRoom}
              onJumpArchive={handleJumpArchive}
            />
          </div>
        ) : null}

        <Suspense
          fallback={
            <div className="card flex min-h-[260px] items-center justify-center p-8 text-xs text-muted">
              正在加载工作台模块…
            </div>
          }
        >
          {visitedScreens.workbench ? (
            <div className={screen === "workbench" ? "block" : "hidden"} aria-hidden={screen !== "workbench"}>
              <WorkbenchView
                defaultSymbol={activeSymbol}
                workspaceRev={workspaceRev}
                onJumpWarRoom={handleJumpWarRoom}
                onJumpArchive={handleJumpArchive}
                onArchiveMutated={bumpWorkspaceRev}
              />
            </div>
          ) : null}

          {visitedScreens.archive ? (
            <div className={screen === "archive" ? "block" : "hidden"} aria-hidden={screen !== "archive"}>
              <ArchiveView
                active={screen === "archive"}
                symbol={activeSymbol}
                market={activeMarket}
                workspaceRev={workspaceRev}
                onArchiveMutated={bumpWorkspaceRev}
                onSelectSymbol={(s, mkt) => {
                  setActiveSymbol(s.trim().toUpperCase());
                  setActiveMarket(mkt ? mkt.trim().toUpperCase() : null);
                }}
                onJumpWarRoom={handleJumpWarRoom}
              />
            </div>
          ) : null}

          {visitedScreens.warroom ? (
            <div className={screen === "warroom" ? "block" : "hidden"} aria-hidden={screen !== "warroom"}>
              <WarRoomView
                active={screen === "warroom"}
                symbol={activeSymbol}
                workspaceRev={workspaceRev}
                portfolioInitialized={health?.portfolio_initialized}
                onSelectSymbol={(s) => setActiveSymbol(s.trim().toUpperCase())}
                onJumpArchive={handleJumpArchive}
                onMacroContext={setMacro}
                onPortfolioChanged={bumpWorkspaceRev}
              />
            </div>
          ) : null}
        </Suspense>
      </main>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNavigateScreen={(s) => setScreen(s)}
        onJumpSymbol={handlePaletteJump}
      />
    </div>
  );
}
