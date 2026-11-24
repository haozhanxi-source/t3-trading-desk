import { cn } from "../../lib/cn";
import { getSymbolName } from "../../lib/symbols";

interface SymbolLabelProps {
  symbol: string | null | undefined;
  name?: string | null;
  size?: "sm" | "md" | "lg";
  badgeTicker?: boolean;
  className?: string;
}

const HAS_CJK_RE = /[一-鿿]/;

/**
 * Unified instrument label across all 4 screens:
 * Canonical Chinese dictionary name takes priority over English official names
 * (unless `name` itself contains Chinese characters), ensuring consistent display.
 */
export function SymbolLabel({
  symbol,
  name,
  size = "md",
  badgeTicker = true,
  className,
}: SymbolLabelProps) {
  const code = (symbol || "").trim();
  if (!code) return <span className="text-muted">—</span>;

  const dictZh = getSymbolName(code);
  const candidate = (name || "").trim();
  const validCandidate = candidate && candidate.toUpperCase() !== code.toUpperCase() ? candidate : null;
  const displayName =
    validCandidate && HAS_CJK_RE.test(validCandidate)
      ? validCandidate
      : dictZh || validCandidate;
  const secondaryEn = dictZh && validCandidate && !HAS_CJK_RE.test(validCandidate) ? validCandidate : null;

  const nameCls =
    size === "lg"
      ? "text-lg md:text-xl font-semibold tracking-tight text-ink"
      : size === "sm"
        ? "text-xs font-semibold text-ink"
        : "text-sm font-semibold text-ink";

  const codeCls =
    size === "lg"
      ? "font-mono text-xs text-muted"
      : "font-mono text-[11px] text-muted";

  if (!displayName) {
    return <span className={cn("font-mono font-semibold text-ink", className)}>{code}</span>;
  }

  return (
    <span
      className={cn("inline-flex min-w-0 items-baseline gap-1.5", className)}
      title={secondaryEn ? `${displayName} · ${secondaryEn} (${code})` : undefined}
    >
      <span className={cn("truncate", nameCls)}>{displayName}</span>
      <span
        className={cn(
          "shrink-0",
          codeCls,
          badgeTicker && "rounded border border-hairline-soft bg-sunken px-1.5 py-0.2",
        )}
      >
        {code}
      </span>
    </span>
  );
}
