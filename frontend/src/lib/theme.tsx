import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeChoice = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export interface ChartPalette {
  mode: ResolvedTheme;
  page: string;
  surface: string;
  elevated: string;
  hairline: string;
  gridline: string;
  ink: string;
  ink2: string;
  muted: string;
  slots: readonly [string, string, string, string, string, string, string, string];
  good: string;
  warning: string;
  serious: string;
  critical: string;
  candleUp: string;
  candleDown: string;
  zsStrokeFill: string;
  zsStrokeBorder: string;
  zsSegFill: string;
  zsSegBorder: string;
}

export const DARK_CHART_PALETTE: ChartPalette = {
  mode: "dark",
  page: "#0d0d0d",
  surface: "#1a1a19",
  elevated: "#222220",
  hairline: "rgba(255, 255, 255, 0.10)",
  gridline: "#2c2c2a",
  ink: "#f0efec",
  ink2: "#c3c2b7",
  muted: "#898781",
  slots: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"],
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
  /* Muted CVD-safe up/down candles (aqua-teal up vs warm terracotta-red down) */
  candleUp: "#199e70",
  candleDown: "#e66767",
  zsStrokeFill: "rgba(57, 135, 229, 0.12)",
  zsStrokeBorder: "rgba(57, 135, 229, 0.78)",
  zsSegFill: "rgba(144, 133, 233, 0.12)",
  zsSegBorder: "rgba(144, 133, 233, 0.82)",
};

export const LIGHT_CHART_PALETTE: ChartPalette = {
  mode: "light",
  page: "#f9f9f7",
  surface: "#fcfcfb",
  elevated: "#f3f2ee",
  hairline: "rgba(11, 11, 11, 0.10)",
  gridline: "#e1e0d9",
  ink: "#0b0b0b",
  ink2: "#52514e",
  muted: "#898781",
  slots: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
  candleUp: "#1baf7a",
  candleDown: "#e34948",
  zsStrokeFill: "rgba(42, 120, 214, 0.11)",
  zsStrokeBorder: "rgba(42, 120, 214, 0.78)",
  zsSegFill: "rgba(98, 80, 214, 0.10)",
  zsSegBorder: "rgba(98, 80, 214, 0.80)",
};

interface ThemeContextValue {
  choice: ThemeChoice;
  resolved: ResolvedTheme;
  palette: ChartPalette;
  setChoice: (next: ThemeChoice) => void;
}

const STORAGE_KEY = "t3desk.theme";

function readInitialChoice(): ThemeChoice {
  if (typeof window === "undefined") return "dark";
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "dark" || v === "light" || v === "system") return v;
  } catch {
    /* ignore storage block */
  }
  return "dark";
}

function readSystemDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

const ThemeContext = createContext<ThemeContextValue>({
  choice: "dark",
  resolved: "dark",
  palette: DARK_CHART_PALETTE,
  setChoice: () => undefined,
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readInitialChoice);
  const [systemDark, setSystemDark] = useState<boolean>(readSystemDark);

  /* Listen to OS prefers-color-scheme change events dynamically */
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (ev: MediaQueryListEvent) => setSystemDark(ev.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  /* Sync data-theme attribute on :root (html), NOT body */
  useEffect(() => {
    const root = document.documentElement;
    if (choice === "dark" || choice === "light") {
      root.dataset.theme = choice;
    } else {
      delete root.dataset.theme;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, choice);
    } catch {
      /* ignore */
    }
  }, [choice]);

  const resolved: ResolvedTheme = choice === "system" ? (systemDark ? "dark" : "light") : choice;

  const value = useMemo<ThemeContextValue>(
    () => ({
      choice,
      resolved,
      palette: resolved === "dark" ? DARK_CHART_PALETTE : LIGHT_CHART_PALETTE,
      setChoice: setChoiceState,
    }),
    [choice, resolved],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
