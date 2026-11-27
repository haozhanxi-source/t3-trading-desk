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
  zsStrokeExtFill: string;
  zsStrokeExtBorder: string;
  zsSegFill: string;
  zsSegBorder: string;
  zsSegExtFill: string;
  zsSegExtBorder: string;
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
  muted: "#8e9bae",
  accent: "#5b95ff",
  bull: "#2ebd85",
  bear: "#f26b6b",
  slots: ["#5b95ff", "#f59e0b", "#2ebd85", "#fbbf24", "#f472b6", "#22d3ee", "#a78bfa", "#f26b6b"],
  good: "#2ebd85",
  warning: "#f59e0b",
  serious: "#fb923c",
  critical: "#f26b6b",
  candleUp: "#2ebd85",
  candleDown: "#f26b6b",
  strokeConfirmed: "rgba(180, 190, 204, 0.46)",
  strokeUnconfirmed: "rgba(180, 190, 204, 0.72)",
  segConfirmed: "#f59e0b",
  segUnconfirmed: "rgba(245, 158, 11, 0.78)",
  zsStrokeFill: "rgba(91, 149, 255, 0.085)",
  zsStrokeBorder: "rgba(91, 149, 255, 0.68)",
  zsStrokeExtFill: "rgba(91, 149, 255, 0.032)",
  zsStrokeExtBorder: "rgba(91, 149, 255, 0.42)",
  zsSegFill: "rgba(245, 158, 11, 0.095)",
  zsSegBorder: "rgba(245, 158, 11, 0.82)",
  zsSegExtFill: "rgba(245, 158, 11, 0.036)",
  zsSegExtBorder: "rgba(245, 158, 11, 0.48)",
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
  ink2: "#3f4d63",
  muted: "#515f73",
  accent: "#1d4ed8",
  bull: "#046c4e",
  bear: "#b91c1c",
  slots: ["#1d4ed8", "#b45309", "#046c4e", "#92400e", "#be185d", "#0e7490", "#6d28d9", "#b91c1c"],
  good: "#046c4e",
  warning: "#92400e",
  serious: "#9a3412",
  critical: "#b91c1c",
  candleUp: "#046c4e",
  candleDown: "#b91c1c",
  strokeConfirmed: "rgba(63, 77, 99, 0.44)",
  strokeUnconfirmed: "rgba(63, 77, 99, 0.68)",
  segConfirmed: "#b45309",
  segUnconfirmed: "rgba(180, 83, 9, 0.76)",
  zsStrokeFill: "rgba(29, 78, 216, 0.075)",
  zsStrokeBorder: "rgba(29, 78, 216, 0.64)",
  zsStrokeExtFill: "rgba(29, 78, 216, 0.026)",
  zsStrokeExtBorder: "rgba(29, 78, 216, 0.40)",
  zsSegFill: "rgba(180, 83, 9, 0.09)",
  zsSegBorder: "rgba(180, 83, 9, 0.80)",
  zsSegExtFill: "rgba(180, 83, 9, 0.030)",
  zsSegExtBorder: "rgba(180, 83, 9, 0.46)",
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
