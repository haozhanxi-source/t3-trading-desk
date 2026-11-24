import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  Clock,
  HelpCircle,
  MinusCircle,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import type { SentinelStatus } from "../../api/types";
import { cn } from "../../lib/cn";

export type Tone = "good" | "warning" | "serious" | "critical" | "neutral" | "info";

const TONE_STYLES: Record<Tone, { border: string; bg: string; dot: string }> = {
  good: {
    border: "border-[color:color-mix(in_srgb,var(--good)_42%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--good)_13%,transparent)]",
    dot: "text-good",
  },
  warning: {
    border: "border-[color:color-mix(in_srgb,var(--warning)_45%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--warning)_14%,transparent)]",
    dot: "text-warning",
  },
  serious: {
    border: "border-[color:color-mix(in_srgb,var(--serious)_45%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--serious)_14%,transparent)]",
    dot: "text-serious",
  },
  critical: {
    border: "border-[color:color-mix(in_srgb,var(--critical)_48%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--critical)_15%,transparent)]",
    dot: "text-critical",
  },
  info: {
    border: "border-[color:color-mix(in_srgb,var(--slot1)_45%,transparent)]",
    bg: "bg-[color:color-mix(in_srgb,var(--slot1)_13%,transparent)]",
    dot: "text-slot1",
  },
  neutral: {
    border: "border-hairline",
    bg: "bg-elevated",
    dot: "text-muted",
  },
};

export function StatusBadge({
  tone,
  label,
  className,
}: {
  tone: Tone;
  label: string;
  className?: string;
}) {
  const s = TONE_STYLES[tone];
  const Icon =
    tone === "good"
      ? CheckCircle2
      : tone === "warning"
        ? AlertTriangle
        : tone === "serious"
          ? ShieldAlert
          : tone === "critical"
            ? AlertOctagon
            : tone === "info"
              ? Clock
              : MinusCircle;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium text-ink whitespace-nowrap",
        s.border,
        s.bg,
        className,
      )}
    >
      <Icon className={cn("h-3.5 w-3.5 shrink-0", s.dot)} aria-hidden="true" />
      <span>{label}</span>
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
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--good)_40%,transparent)] bg-[color:color-mix(in_srgb,var(--good)_12%,transparent)] px-1.5 py-0.5 text-[11px] font-medium text-ink">
        <TrendingUp className="h-3 w-3 text-good" aria-hidden="true" />
        <span>做多</span>
      </span>
    );
  }
  if (d === "short" || d === "down" || d === "bearish" || d.includes("空")) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-[color:color-mix(in_srgb,var(--serious)_42%,transparent)] bg-[color:color-mix(in_srgb,var(--serious)_12%,transparent)] px-1.5 py-0.5 text-[11px] font-medium text-ink">
        <TrendingDown className="h-3 w-3 text-serious" aria-hidden="true" />
        <span>做空</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded border border-hairline bg-elevated px-1.5 py-0.5 text-[11px] text-muted">
      <HelpCircle className="h-3 w-3" aria-hidden="true" />
      <span>{direction || "中性"}</span>
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
  let tone: Tone = "neutral";
  let zh = decision;
  if (d.includes("APPROVE_LONG") || decision.includes("放行做多")) {
    tone = "good";
    zh = "放行做多";
  } else if (d.includes("APPROVE_SHORT") || decision.includes("放行做空")) {
    tone = "good";
    zh = "放行做空";
  } else if (d.includes("PAIR") || decision.includes("配对")) {
    tone = "info";
    zh = "同板块配对";
  } else if (d.includes("WATCH") || d.includes("WAIT") || decision.includes("观察")) {
    tone = "warning";
    zh = "逻辑待观察";
  } else if (d.includes("VETO") || d.includes("REJECT") || decision.includes("否决")) {
    tone = "critical";
    zh = "否决建仓";
  } else if (actionable) {
    tone = "good";
  }
  return <StatusBadge tone={tone} label={zh} className="text-xs font-semibold" />;
}
