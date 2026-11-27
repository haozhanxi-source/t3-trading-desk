import { AlertOctagon, ArrowDownRight, ArrowUpRight } from "lucide-react";
import type { SentinelStatus } from "../../api/types";
import { cn } from "../../lib/cn";

export type Tone = "good" | "warning" | "serious" | "critical" | "neutral" | "info";

const TONE_STYLES: Record<Tone, { border: string; bg: string; dotBg: string; text: string }> = {
  good: {
    border: "border-[color:color-mix(in_srgb,var(--good)_28%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--good)_11%,transparent)]",
    dotBg: "bg-good",
    text: "text-good",
  },
  warning: {
    border: "border-[color:color-mix(in_srgb,var(--warning)_30%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--warning)_11%,transparent)]",
    dotBg: "bg-warning",
    text: "text-warning",
  },
  serious: {
    border: "border-[color:color-mix(in_srgb,var(--serious)_32%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--serious)_11%,transparent)]",
    dotBg: "bg-serious",
    text: "text-serious",
  },
  critical: {
    border: "border-[color:color-mix(in_srgb,var(--critical)_42%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--critical)_13%,transparent)]",
    dotBg: "bg-critical",
    text: "text-critical",
  },
  info: {
    border: "border-[color:color-mix(in_srgb,var(--accent)_30%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--accent)_11%,transparent)]",
    dotBg: "bg-accent",
    text: "text-accent",
  },
  neutral: {
    border: "border-hairline",
    bg: "bg-sunken",
    dotBg: "bg-muted",
    text: "text-ink-2",
  },
};

export function StatusBadge({
  tone,
  label,
  className,
  title,
}: {
  tone: Tone;
  label: string;
  className?: string;
  title?: string;
}) {
  const s = TONE_STYLES[tone];
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-[11px] font-medium leading-snug whitespace-nowrap",
        s.border,
        s.bg,
        s.text,
        className,
      )}
    >
      {tone === "critical" ? (
        <AlertOctagon className="h-3 w-3 shrink-0 text-critical" aria-hidden="true" />
      ) : (
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", s.dotBg)} aria-hidden="true" />
      )}
      <span className="text-ink">{label}</span>
    </span>
  );
}

function stripStatusEmoji(text: string): string {
  return text.replace(/^[\u{1F534}\u{1F7E1}\u{1F7E2}\u{26AA}\u{26AB}\s]+/u, "").trim();
}

export function SentinelStatusBadge({ status, labelZh }: { status: SentinelStatus | string; labelZh?: string }) {
  const cleanLabel = labelZh ? stripStatusEmoji(labelZh) : "";
  if (status === "RED_EXIT") {
    return <StatusBadge tone="critical" label={cleanLabel || "果断撤离"} />;
  }
  if (status === "YELLOW_LOOSENING") {
    return <StatusBadge tone="warning" label={cleanLabel || "指标松动"} />;
  }
  if (status === "GREEN_HOLD") {
    return <StatusBadge tone="good" label={cleanLabel || "稳固持有"} />;
  }
  return <StatusBadge tone="neutral" label={cleanLabel || String(status || "未知")} />;
}

export function DirectionBadge({ direction }: { direction: string | null | undefined }) {
  const d = (direction || "").toLowerCase();
  if (d === "long" || d === "up" || d === "bullish" || d.includes("多")) {
    return (
      <span className="inline-flex items-center gap-0.5 rounded border border-[color:color-mix(in_srgb,var(--bull)_34%,transparent)] bg-[color:color-mix(in_srgb,var(--bull)_12%,transparent)] px-1.5 py-0.5 font-mono text-[11px] font-semibold text-bull whitespace-nowrap">
        <ArrowUpRight className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="font-sans">做多</span>
      </span>
    );
  }
  if (d === "short" || d === "down" || d === "bearish" || d.includes("空")) {
    return (
      <span className="inline-flex items-center gap-0.5 rounded border border-[color:color-mix(in_srgb,var(--bear)_34%,transparent)] bg-[color:color-mix(in_srgb,var(--bear)_12%,transparent)] px-1.5 py-0.5 font-mono text-[11px] font-semibold text-bear whitespace-nowrap">
        <ArrowDownRight className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="font-sans">做空</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded border border-hairline bg-sunken px-1.5 py-0.5 text-[11px] text-muted whitespace-nowrap">
      {direction || "中性"}
    </span>
  );
}

export function SourceStatusBadge({ status, name }: { status: string | undefined; name?: string }) {
  const st = (status || "unknown").toLowerCase();
  const tone: Tone =
    st === "ok"
      ? "good"
      : st === "partial" || st === "stale"
        ? "warning"
        : st === "error"
          ? "critical"
          : "neutral";
  const zh =
    st === "ok"
      ? "正常"
      : st === "partial"
        ? "部分可用"
        : st === "stale"
          ? "陈旧隔离"
          : st === "error"
            ? "源故障"
            : st === "skipped"
              ? "白名单跳过"
              : st;
  return <StatusBadge tone={tone} label={name ? `${name} · ${zh}` : zh} />;
}

export function GateDecisionBadge({ decision, actionable }: { decision: string; actionable?: boolean }) {
  const d = decision.toUpperCase();
  if (d.includes("APPROVE_LONG") || decision.includes("放行做多")) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--bull)_42%,transparent)] bg-[color:color-mix(in_srgb,var(--bull)_14%,transparent)] px-2 py-0.5 text-xs font-semibold text-bull whitespace-nowrap">
        <ArrowUpRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>放行做多</span>
      </span>
    );
  }
  if (d.includes("APPROVE_SHORT") || decision.includes("放行做空")) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color:color-mix(in_srgb,var(--accent)_14%,transparent)] px-2 py-0.5 text-xs font-semibold text-accent whitespace-nowrap">
        <ArrowDownRight className="h-3.5 w-3.5 shrink-0 text-bear" aria-hidden="true" />
        <span className="text-ink">放行做空</span>
      </span>
    );
  }
  let tone: Tone = "neutral";
  let zh = decision;
  if (d.includes("PAIR") || decision.includes("配对")) {
    tone = "info";
    zh = "同板块配对";
  } else if (d.includes("WATCH") || d.includes("WAIT") || decision.includes("观察")) {
    tone = "warning";
    zh = "逻辑待观察";
  } else if (d.includes("VETO") || d.includes("REJECT") || decision.includes("否决")) {
    tone = "critical";
    zh = "否决建仓";
  } else if (actionable) {
    tone = "info";
  }
  return <StatusBadge tone={tone} label={zh} className="text-xs font-semibold" />;
}

export function signedToneClass(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val) || val === 0) return "text-ink-2";
  return val > 0 ? "text-bull" : "text-bear";
}
