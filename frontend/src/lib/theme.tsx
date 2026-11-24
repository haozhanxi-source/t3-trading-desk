import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeChoice = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export interface ChartPalette {
  mode: ResolvedTheme;
  page: string;
  sunken: string;
  surface: string;
  elevated: string;
  hairline: string;
  gridline: string;
  ink: string;
  ink2: string;
  muted: string;
  accent: string;
  bull: string;
  bear: string;
  slots: readonly [string, string, string, string, string, string, string, string];
  good: string;
  warning: string;
  serious: string;
  critical: string;
  candleUp: string;
  candleDown: string;
  /* Chanlun structural layer hierarchy: neutral silver strokes + steel-blue stroke ZS + amber segment & segment ZS */
  strokeConfirmed: string;
  strokeUnconfirmed: string;
  segConfirmed: string;
  segUnconfirmed: string;
  zsStrokeFill: string;
  zsStrokeBorder: string;
  zsSegFill: string;
  zsSegBorder: string;
}

export const DARK_CHART_PALETTE: ChartPalette = {
  mode: "dark",
  page: "#0c0e12",
  sunken: "#080a0d",
  surface: "#12151b",
  elevated: "#191d26",
  hairline: "rgba(255, 255, 255, 0.085)",
  gridline: "#1b202a",
  ink: "#eef2f6",
  ink2: "#b4becc",
  muted: "#7c889a",
  accent: "#4f8cff",
  bull: "#2ebd85",
  bear: "#f06565",
  slots: ["#60a5fa", "#f59e0b", "#2ebd85", "#fbbf24", "#f472b6", "#22d3ee", "#a78bfa", "#f06565"],
  good: "#2ebd85",
  warning: "#f59e0b",
  serious: "#fb923c",
  critical: "#f06565",
  candleUp: "#2ebd85",
  candleDown: "#f06565",
  strokeConfirmed: "rgba(180, 190, 204, 0.46)",
  strokeUnconfirmed: "rgba(180, 190, 204, 0.72)",
  segConfirmed: "#f59e0b",
  segUnconfirmed: "rgba(245, 158, 11, 0.78)",
  zsStrokeFill: "rgba(96, 165, 250, 0.075)",
  zsStrokeBorder: "rgba(96, 165, 250, 0.62)",
  zsSegFill: "rgba(245, 158, 11, 0.085)",
  zsSegBorder: "rgba(245, 158, 11, 0.80)",
};

export const LIGHT_CHART_PALETTE: ChartPalette = {
  mode: "light",
  page: "#f3f5f8",
  sunken: "#e9edf2",
  surface: "#ffffff",
  elevated: "#f8fafc",
  hairline: "rgba(15, 23, 42, 0.095)",
  gridline: "#e5e9f0",
  ink: "#0f172a",
  ink2: "#475569",
  muted: "#64748b",
  accent: "#2563eb",
  bull: "#0d9462",
  bear: "#dc2626",
  slots: ["#2563eb", "#d97706", "#0d9462", "#b45309", "#db2777", "#0891b2", "#7c3aed", "#dc2626"],
  good: "#0d9462",
  warning: "#b45309",
  serious: "#c2410c",
  critical: "#dc2626",
  candleUp: "#0d9462",
  candleDown: "#dc2626",
  strokeConfirmed: "rgba(71, 85, 105, 0.44)",
  strokeUnconfirmed: "rgba(71, 85, 105, 0.68)",
  segConfirmed: "#d97706",
  segUnconfirmed: "rgba(217, 119, 6, 0.76)",
  zsStrokeFill: "rgba(37, 99, 235, 0.065)",
  zsStrokeBorder: "rgba(37, 99, 235, 0.60)",
  zsSegFill: "rgba(217, 119, 6, 0.08)",
  zsSegBorder: "rgba(217, 119, 6, 0.78)",
};

interface ThemeContextValue {
  choice: ThemeChoice;
  resolved: ResolvedTheme;
  systemDark: boolean;
  palette: ChartPalette;
  setChoice: (next: ThemeChoice) => void;
  cycleChoice: () => void;
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

function syncMetaColorScheme(choice: ThemeChoice) {
  if (typeof document === "undefined") return;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
  if (meta) {
    meta.content = choice === "system" ? "light dark" : choice;
  }
}

const ThemeContext = createContext<ThemeContextValue>({
  choice: "dark",
  resolved: "dark",
  systemDark: true,
  palette: DARK_CHART_PALETTE,
  setChoice: () => undefined,
  cycleChoice: () => undefined,
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readInitialChoice);
  const [systemDark, setSystemDark] = useState<boolean>(readSystemDark);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (ev: MediaQueryListEvent) => setSystemDark(ev.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (ev: StorageEvent) => {
      if (ev.key !== STORAGE_KEY) return;
      const v = ev.newValue;
      if (v === "dark" || v === "light" || v === "system") {
        setChoiceState(v);
      } else if (v === null) {
        setChoiceState("system");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (choice === "dark" || choice === "light") {
      root.dataset.theme = choice;
    } else {
      delete root.dataset.theme;
    }
    syncMetaColorScheme(choice);
    try {
      window.localStorage.setItem(STORAGE_KEY, choice);
    } catch {
      /* ignore */
    }
  }, [choice]);

  const cycleChoice = useCallback(() => {
    setChoiceState((prev) => (prev === "dark" ? "light" : prev === "light" ? "system" : "dark"));
  }, []);

  const resolved: ResolvedTheme = choice === "system" ? (systemDark ? "dark" : "light") : choice;

  const value = useMemo<ThemeContextValue>(
    () => ({
      choice,
      resolved,
      systemDark,
      palette: resolved === "dark" ? DARK_CHART_PALETTE : LIGHT_CHART_PALETTE,
      setChoice: setChoiceState,
      cycleChoice,
    }),
    [choice, resolved, systemDark, cycleChoice],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
