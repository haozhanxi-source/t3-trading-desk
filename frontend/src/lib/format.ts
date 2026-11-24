const nf = (min: number, max: number) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });

const cache = new Map<string, Intl.NumberFormat>();
function fmtr(min: number, max: number): Intl.NumberFormat {
  const k = `${min}:${max}`;
  let f = cache.get(k);
  if (!f) {
    f = nf(min, max);
    cache.set(k, f);
  }
  return f;
}

export function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

export function toNum(v: unknown): number | null {
  if (isNum(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** Price with adaptive precision (FX 4–5dp, futures/stocks 2dp). */
export function fmtPrice(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  const dp = a === 0 ? 2 : a < 2 ? 4 : a < 20 ? 3 : 2;
  return fmtr(dp, dp).format(n);
}

export function fmtNum(v: unknown, dp = 2): string {
  const n = toNum(v);
  return n === null ? "—" : fmtr(0, dp).format(n);
}

export function fmtFixed(v: unknown, dp = 2): string {
  const n = toNum(v);
  return n === null ? "—" : fmtr(dp, dp).format(n);
}

export function fmtPct(v: unknown, dp = 2, signed = false): string {
  const n = toNum(v);
  if (n === null) return "—";
  const s = fmtr(dp, dp).format(n);
  return `${signed && n > 0 ? "+" : ""}${s}%`;
}

export function fmtMoney(v: unknown, ccy?: string): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  const body = a >= 1e9 ? `${fmtr(2, 2).format(n / 1e9)}B` : a >= 1e6 ? `${fmtr(2, 2).format(n / 1e6)}M` : fmtr(0, 2).format(n);
  return ccy ? `${body} ${ccy}` : body;
}

export function fmtCompact(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "—";
  const a = Math.abs(n);
  if (a >= 1e12) return `${fmtr(2, 2).format(n / 1e12)}T`;
  if (a >= 1e9) return `${fmtr(2, 2).format(n / 1e9)}B`;
  if (a >= 1e6) return `${fmtr(2, 2).format(n / 1e6)}M`;
  if (a >= 1e3) return `${fmtr(1, 1).format(n / 1e3)}K`;
  return fmtr(0, 2).format(n);
}

export function fmtRatio(v: unknown): string {
  const n = toNum(v);
  return n === null ? "—" : `${fmtr(2, 2).format(n)}R`;
}

export function dirLabel(d: unknown): string {
  if (d === "long" || d === "up") return "多";
  if (d === "short" || d === "down") return "空";
  if (d === "neutral") return "中性";
  return d ? String(d) : "—";
}

export function shortTime(s: string | null | undefined): string {
  if (!s) return "—";
  const cleaned = s
    .trim()
    .replace(/(\d{4}-\d{2}-\d{2})T/, "$1 ")
    .replace(/\s*UTC$/i, "")
    .replace(/:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/, "")
    .replace(/(Z|[+-]\d{2}:?\d{2})$/i, "")
    .trim();
  return cleaned || "—";
}

/** Clean input string for prefilled chart/snapshot prices (no 16-digit IEEE-754 float tails). */
export function fmtInputPrice(v: unknown): string {
  const n = toNum(v);
  if (n === null) return "";
  const a = Math.abs(n);
  const dp = a === 0 ? 2 : a < 2 ? 4 : a < 20 ? 3 : 2;
  return String(Number(n.toFixed(dp)));
}

/** Preserve up to 6dp from backend computed plans while stripping IEEE-754 binary float noise. */
export function fmtPlanNum(v: unknown, maxDp = 6): string {
  const n = toNum(v);
  if (n === null) return "";
  return String(Number(n.toFixed(maxDp)));
}

/** Infer asset quote currency for cross-currency fx_to_account hints. */
export function inferAssetCurrency(symbol: string | null | undefined, assetClass?: string | null): string | null {
  const sym = (symbol || "").trim().toUpperCase();
  const ac = (assetClass || "").trim().toLowerCase();
  if (!sym) return null;
  if (ac === "a_share" || ac === "cn_equity" || /^\d{6}(\.(SS|SZ|BJ))?$/.test(sym)) {
    return "CNY";
  }
  if (ac === "cn_future" || sym.startsWith("CN:") || /^[A-Z]{1,2}0$/.test(sym)) {
    return "CNY";
  }
  if (sym.endsWith(".HK")) {
    return "HKD";
  }
  const fxMatch = /^([A-Z]{3})([A-Z]{3})=X$/.exec(sym);
  if (fxMatch) {
    return fxMatch[2] ?? null;
  }
  if (
    sym === "DX-Y.NYB" ||
    ac === "us_equity" ||
    ac === "us_future" ||
    ac === "commodity_us" ||
    ac === "crypto" ||
    /^[A-Z]{1,5}(=F)?$/.test(sym)
  ) {
    return "USD";
  }
  return null;
}

export function parseSymbolList(s: string): string[] {
  return Array.from(
    new Set(
      s
        .split(/[\s,，;；、]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  );
}

export function optNum(s: string): number | undefined {
  if (s.trim() === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}
