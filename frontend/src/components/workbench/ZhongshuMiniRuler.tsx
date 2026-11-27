import { fmtNum } from "../../lib/format";

interface ZhongshuMiniRulerProps {
  last: number | null | undefined;
  zd: number | null | undefined;
  zg: number | null | undefined;
}

/**
 * Compact SVG box-and-dot ruler visualizing price position relative to the latest daily Zhongshu [ZD, ZG].
 * Guarantees a visible [ZD, ZG] band even when price is far away, and formats exact range cleanly below.
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
  const isAbove = price !== null && price > zg;
  const isBelow = price !== null && price < zd;
  const dotFill = isAbove ? "var(--bull)" : isBelow ? "var(--bear)" : "var(--accent)";

  /* Compute clean geometry with guaranteed readable [ZD, ZG] box (>= 18px) */
  const trackStart = 6;
  const trackEnd = 122;
  const trackW = trackEnd - trackStart;

  let xZd = trackStart + trackW * 0.32;
  let xZg = trackStart + trackW * 0.68;
  let xPrice: number | null = null;

  if (price !== null) {
    const span = zg - zd;
    const pad = Math.max(span * 0.45, Math.abs(zd) * 0.02);
    const rawMin = Math.min(zd, price) - pad;
    const rawMax = Math.max(zg, price) + pad;
    const totalSpan = Math.max(rawMax - rawMin, 1e-6);
    const mapX = (v: number) => trackStart + ((v - rawMin) / totalSpan) * trackW;
    xZd = mapX(zd);
    xZg = mapX(zg);
    xPrice = mapX(price);

    // Ensure [ZD, ZG] box stays clearly legible (>= 16px wide) without overlapping price dot
    const minBoxW = 16;
    if (xZg - xZd < minBoxW) {
      const center = (xZd + xZg) / 2;
      if (isBelow) {
        xZg = Math.min(trackEnd - 2, Math.max(center + minBoxW / 2, xPrice + 20 + minBoxW));
        xZd = Math.max(xPrice + 14, xZg - minBoxW);
      } else if (isAbove) {
        xZd = Math.max(trackStart + 2, Math.min(center - minBoxW / 2, xPrice - 20 - minBoxW));
        xZg = Math.min(xPrice - 14, xZd + minBoxW);
      } else {
        xZd = Math.max(trackStart + 2, center - minBoxW / 2);
        xZg = Math.min(trackEnd - 2, xZd + minBoxW);
      }
    }
  }

  const distZdPct = price !== null && zd > 0 ? (((price - zd) / zd) * 100).toFixed(1) : null;
  const distZgPct = price !== null && zg > 0 ? (((price - zg) / zg) * 100).toFixed(1) : null;
  const posLabel = isAbove ? "突破ZG上方" : isBelow ? "跌破ZD下方" : "中枢区间内";
  const tooltip = `日线中枢 [${fmtNum(zd, 2)}, ${fmtNum(zg, 2)}] · 现价 ${price !== null ? fmtNum(price, 2) : "—"} (${posLabel}${
    distZdPct !== null
      ? ` · 距ZD ${Number(distZdPct) >= 0 ? "+" : ""}${distZdPct}% / 距ZG ${Number(distZgPct) >= 0 ? "+" : ""}${distZgPct}%`
      : ""
  })`;

  const decimals = Math.abs(zd) >= 1000 ? 1 : 2;

  return (
    <div className="inline-flex w-[128px] flex-col gap-0.5" title={tooltip}>
      <svg
        width={128}
        height={18}
        viewBox="0 0 128 18"
        className="overflow-visible"
        role="img"
        aria-label={tooltip}
      >
        {/* Recessive baseline track */}
        <line
          x1={trackStart}
          y1={9}
          x2={trackEnd}
          y2={9}
          stroke="var(--viz-grid)"
          strokeWidth={1.5}
          strokeLinecap="round"
        />
        {/* Zhongshu [ZD, ZG] box */}
        <rect
          x={xZd}
          y={4}
          width={Math.max(8, xZg - xZd)}
          height={10}
          rx={2}
          fill="var(--accent)"
          opacity={0.22}
        />
        <line x1={xZd} y1={3} x2={xZd} y2={15} stroke="var(--accent)" strokeWidth={1.25} opacity={0.75} />
        <line x1={xZg} y1={3} x2={xZg} y2={15} stroke="var(--accent)" strokeWidth={1.25} opacity={0.75} />
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
      <div className="flex items-center justify-center gap-1 font-mono text-[11px] tabular-nums text-muted">
        <span>[{fmtNum(zd, decimals)}</span>
        <span>~</span>
        <span>{fmtNum(zg, decimals)}]</span>
      </div>
    </div>
  );
}
