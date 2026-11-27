import { fmtNum } from "../../lib/format";

interface ZhongshuMiniRulerProps {
  last: number | null | undefined;
  zd: number | null | undefined;
  zg: number | null | undefined;
}

/**
 * Compact SVG box-and-dot ruler visualizing price position relative to the latest daily Zhongshu [ZD, ZG].
 * Follows dataviz mark specs: recessive 1px track, subtle band fill, >=8px marker with 2px surface ring.
 */
export function ZhongshuMiniRuler({ last, zd, zg }: ZhongshuMiniRulerProps) {
  if (
    zd === null ||
    zd === undefined ||
    zg === null ||
    zg === undefined ||
    Number.isNaN(zd) ||
    Number.isNaN(zg) ||
    zg <= zd
  ) {
    return <span className="font-mono text-[11px] text-muted">—</span>;
  }

  const price = typeof last === "number" && !Number.isNaN(last) ? last : null;
  const span = zg - zd;
  const pad = Math.max(span * 0.45, Math.abs(zd) * 0.02);
  const rawMin = price !== null ? Math.min(zd, price) - pad : zd - pad;
  const rawMax = price !== null ? Math.max(zg, price) + pad : zg + pad;
  const totalSpan = Math.max(rawMax - rawMin, 1e-6);

  const mapX = (v: number) => 6 + ((v - rawMin) / totalSpan) * 104;
  const xZd = mapX(zd);
  const xZg = mapX(zg);
  const xPrice = price !== null ? mapX(price) : null;

  const isAbove = price !== null && price > zg;
  const isBelow = price !== null && price < zd;
  const dotFill = isAbove ? "var(--bull)" : isBelow ? "var(--bear)" : "var(--accent)";

  const distZdPct = price !== null && zd > 0 ? (((price - zd) / zd) * 100).toFixed(1) : null;
  const distZgPct = price !== null && zg > 0 ? (((price - zg) / zg) * 100).toFixed(1) : null;
  const posLabel = isAbove ? "突破ZG上方" : isBelow ? "跌破ZD下方" : "中枢区间内";
  const tooltip = `日线中枢 [${fmtNum(zd, 2)}, ${fmtNum(zg, 2)}] · 现价 ${price !== null ? fmtNum(price, 2) : "—"} (${posLabel}${
    distZdPct !== null ? ` · 距ZD ${Number(distZdPct) >= 0 ? "+" : ""}${distZdPct}% / 距ZG ${Number(distZgPct) >= 0 ? "+" : ""}${distZgPct}%` : ""
  })`;

  return (
    <div className="inline-flex flex-col gap-0.5" title={tooltip}>
      <svg
        width={116}
        height={18}
        viewBox="0 0 116 18"
        className="overflow-visible"
        role="img"
        aria-label={tooltip}
      >
        {/* Recessive baseline track */}
        <line x1={6} y1={9} x2={110} y2={9} stroke="var(--viz-grid)" strokeWidth={1.5} strokeLinecap="round" />
        {/* Zhongshu [ZD, ZG] box */}
        <rect
          x={xZd}
          y={4}
          width={Math.max(4, xZg - xZd)}
          height={10}
          rx={2}
          fill="var(--accent)"
          opacity={0.18}
        />
        <line x1={xZd} y1={3} x2={xZd} y2={15} stroke="var(--muted)" strokeWidth={1.2} />
        <line x1={xZg} y1={3} x2={xZg} y2={15} stroke="var(--muted)" strokeWidth={1.2} />
        {/* Price marker with 2px surface ring */}
        {xPrice !== null ? (
          <circle
            cx={xPrice}
            cy={9}
            r={4}
            fill={dotFill}
            stroke="var(--surface)"
            strokeWidth={2}
          />
        ) : null}
      </svg>
      <div className="flex items-center justify-between gap-1 font-mono text-[10px] tabular-nums text-muted">
        <span>{fmtNum(zd, 2)}</span>
        <span>{fmtNum(zg, 2)}</span>
      </div>
    </div>
  );
}
