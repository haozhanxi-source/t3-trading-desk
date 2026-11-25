import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { ZhongshuExtensionStage, ZhongshuRelation } from "../../api/types";
import type { ChartPalette } from "../../lib/theme";
import { fmtPrice } from "../../lib/format";

export interface ZhongshuBoxDrawItem {
  key: string;
  zsId: number;
  kind: "stroke" | "segment";
  label: string;
  compactCode: string;
  coreStartTime: Time | null;
  coreEndTime: Time | null;
  extStartTime: Time | null;
  extEndTime: Time | null;
  nineUpgradeTime?: Time | null;
  startLabel?: string;
  coreEndLabel?: string;
  extEndLabel?: string | null;
  nineUpgradeLabel?: string | null;
  clampedLeft?: boolean;
  clampedRight?: boolean;
  zd: number;
  zg: number;
  dd?: number;
  gg?: number;
  oscillationStrokes?: number;
  isExtended?: boolean;
  isNineUpgrade?: boolean;
  status?: string;
  trendZsIndex?: number;
  relationToPrev?: ZhongshuRelation;
  extensionStage?: ZhongshuExtensionStage;
}

type RenderTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];

interface PixelBox {
  coreX1: number | null;
  coreX2: number | null;
  extX1: number | null;
  extX2: number | null;
  nineX: number | null;
  yTop: number;
  yBottom: number;
  compactCode: string;
  rangeText: string;
  kind: "stroke" | "segment";
  oscillationStrokes: number;
  isExtended: boolean;
  isNineUpgrade: boolean;
  relationToPrev: ZhongshuRelation;
  clampedLeft: boolean;
  clampedRight: boolean;
}

function relationShortTag(rel: ZhongshuRelation): string {
  switch (rel) {
    case "new_up":
      return "↑新生";
    case "new_down":
      return "↓新生";
    case "expansion_up":
      return "↑扩张";
    case "expansion_down":
      return "↓扩张";
    case "overlap":
      return "扩展";
    default:
      return "";
  }
}

function relationTinyTag(rel: ZhongshuRelation): string {
  switch (rel) {
    case "new_up":
      return "↑新";
    case "new_down":
      return "↓新";
    case "expansion_up":
      return "↑扩";
    case "expansion_down":
      return "↓扩";
    case "overlap":
      return "叠";
    default:
      return "";
  }
}

function accentForBox(box: PixelBox, pal: ChartPalette): string | null {
  if (box.isNineUpgrade || box.relationToPrev === "expansion_up" || box.relationToPrev === "expansion_down") {
    return pal.warning;
  }
  if (box.relationToPrev === "new_up") return pal.bull;
  if (box.relationToPrev === "new_down") return pal.bear;
  if (box.relationToPrev === "overlap") return pal.muted;
  return null;
}

function buildPillText(box: PixelBox, totalVisibleW: number): string {
  const unit = box.kind === "segment" ? "段" : "笔";
  const totalUnits = 3 + Math.max(0, box.oscillationStrokes);
  const relShort = relationShortTag(box.relationToPrev);
  const relTiny = relationTinyTag(box.relationToPrev);

  if (totalVisibleW < 108) {
    if (box.isNineUpgrade) return `${box.compactCode}·九段`;
    if (box.isExtended) return `${box.compactCode}·延+${box.oscillationStrokes}`;
    if (relTiny) return `${box.compactCode}·${relTiny}`;
    return box.compactCode;
  }

  const parts = [box.compactCode];
  if (relShort) parts.push(relShort);
  if (box.isNineUpgrade) {
    parts.push(`九段(${totalUnits}${unit})`);
  } else if (box.isExtended) {
    parts.push(`延伸+${box.oscillationStrokes}${unit}`);
  }
  const semantic = parts.join("·");

  if (totalVisibleW >= 196) {
    return `${semantic} [${box.rangeText}]`;
  }
  return semantic;
}

class ZhongshuPaneRenderer implements IPrimitivePaneRenderer {
  private _boxes: PixelBox[] = [];
  private _palette: ChartPalette;

  constructor(palette: ChartPalette) {
    this._palette = palette;
  }

  update(boxes: PixelBox[], palette: ChartPalette): void {
    this._boxes = boxes;
    this._palette = palette;
  }

  draw(target: RenderTarget): void {
    if (this._boxes.length === 0) return;
    const pal = this._palette;

    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const w = mediaSize.width;
      const h = mediaSize.height;
      ctx.save();

      for (const box of this._boxes) {
        const top = Math.min(box.yTop, box.yBottom);
        const bottom = Math.max(box.yTop, box.yBottom);
        if (bottom < 0 || top > h) continue;
        const height = Math.max(2, bottom - top);

        const isSeg = box.kind === "segment";
        const coreFill = isSeg ? pal.zsSegFill : pal.zsStrokeFill;
        const coreBorder = isSeg ? pal.zsSegBorder : pal.zsStrokeBorder;
        const extFill = isSeg ? pal.zsSegExtFill : pal.zsStrokeExtFill;
        const extBorder = isSeg ? pal.zsSegExtBorder : pal.zsStrokeExtBorder;

        let visibleLeft: number | null = null;
        let visibleRight: number | null = null;
        let hasCoreVisible = false;
        let hasExtVisible = false;
        let coreRightClamped = 0;

        /* 1. Founding 3-unit core box: solid fill + crisp solid [ZD, ZG] borders */
        const hasExtCoords = box.extX1 !== null && box.extX2 !== null;
        if (box.coreX1 !== null && box.coreX2 !== null) {
          const cLeft = Math.max(0, Math.min(box.coreX1, box.coreX2));
          const cRight = Math.min(w, Math.max(box.coreX1, box.coreX2));
          if (cRight > 0 && cLeft < w) {
            const rawWidth = cRight - cLeft;
            const minCoreW = hasExtCoords ? 1 : 4;
            coreRightClamped = Math.min(w, cLeft + Math.max(minCoreW, rawWidth));
            const cWidth = coreRightClamped - cLeft;
            if (cWidth > 0) {
              hasCoreVisible = true;
              visibleLeft = cLeft;
              visibleRight = coreRightClamped;

              ctx.fillStyle = coreFill;
              ctx.fillRect(cLeft, top, cWidth, height);

              ctx.strokeStyle = coreBorder;
              ctx.lineWidth = isSeg ? 2.0 : box.isNineUpgrade ? 1.5 : 1.25;
              ctx.setLineDash([]);
              ctx.beginPath();
              ctx.moveTo(cLeft, top);
              ctx.lineTo(coreRightClamped, top);
              ctx.moveTo(cLeft, bottom);
              ctx.lineTo(coreRightClamped, bottom);
              ctx.stroke();

              /* Vertical left wall + right wall (divider when extension follows) */
              ctx.lineWidth = 1;
              ctx.setLineDash([3, 3]);
              ctx.beginPath();
              ctx.moveTo(cLeft, top);
              ctx.lineTo(cLeft, bottom);
              ctx.moveTo(coreRightClamped, top);
              ctx.lineTo(coreRightClamped, bottom);
              ctx.stroke();
              ctx.setLineDash([]);
            }
          }
        }

        /* 2. Genuine extension tail: lighter fill + dashed [ZD, ZG] borders */
        if (box.extX1 !== null && box.extX2 !== null) {
          const rawExtLeft = Math.min(box.extX1, box.extX2);
          const rawExtRight = Math.max(box.extX1, box.extX2);
          const eLeft = Math.max(hasCoreVisible ? coreRightClamped : 0, rawExtLeft);
          const eRight = Math.min(w, rawExtRight);
          if (eRight > eLeft + 1 && eRight > 0 && eLeft < w) {
            hasExtVisible = true;
            const eWidth = eRight - eLeft;
            visibleLeft = visibleLeft === null ? eLeft : Math.min(visibleLeft, eLeft);
            visibleRight = visibleRight === null ? eRight : Math.max(visibleRight, eRight);

            ctx.fillStyle = extFill;
            ctx.fillRect(eLeft, top, eWidth, height);

            ctx.strokeStyle = extBorder;
            ctx.lineWidth = isSeg ? 1.4 : 1.05;
            ctx.setLineDash([5, 4]);
            ctx.beginPath();
            ctx.moveTo(eLeft, top);
            ctx.lineTo(eRight, top);
            ctx.moveTo(eLeft, bottom);
            ctx.lineTo(eRight, bottom);
            ctx.stroke();

            /* Subtle closing dotted wall at extension end */
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 3]);
            ctx.beginPath();
            ctx.moveTo(eRight, top);
            ctx.lineTo(eRight, bottom);
            ctx.stroke();
            ctx.setLineDash([]);

            /* Optional 9-unit upgrade milestone tick inside the extension tail */
            if (box.nineX !== null && box.nineX > eLeft + 4 && box.nineX < eRight - 4) {
              ctx.strokeStyle = pal.warning;
              ctx.lineWidth = 1;
              ctx.setLineDash([2, 2]);
              ctx.beginPath();
              ctx.moveTo(box.nineX, top + 1);
              ctx.lineTo(box.nineX, bottom - 1);
              ctx.stroke();
              ctx.setLineDash([]);
            }
          }
        }

        if ((!hasCoreVisible && !hasExtVisible) || visibleLeft === null || visibleRight === null) {
          continue;
        }

        const totalVisibleW = Math.max(0, visibleRight - visibleLeft);

        /* 3. Watermark-style monospace pill tag: stroke ZS top-left, segment ZS bottom-left */
        if (totalVisibleW >= 38 && bottom >= 12 && top <= h - 6) {
          const text = buildPillText(box, totalVisibleW);
          ctx.font = '500 10px "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace';
          const textW = ctx.measureText(text).width;
          const accent = accentForBox(box, pal);
          const padLeft = accent ? 7 : 4;
          const tagW = Math.min(totalVisibleW - 4, textW + padLeft + 5);
          if (tagW < 18) continue;

          const tagH = 15;
          const tagX = visibleLeft + 3;
          const insideFits = height >= 19;
          const rawTagY = isSeg
            ? insideFits
              ? bottom - tagH - 2
              : bottom + 2
            : insideFits
              ? top + 2
              : top - tagH - 2;
          const tagY = Math.max(2, Math.min(Math.max(2, h - tagH - 2), rawTagY));

          ctx.fillStyle = pal.mode === "dark" ? "rgba(12, 14, 18, 0.80)" : "rgba(255, 255, 255, 0.88)";
          ctx.beginPath();
          ctx.roundRect(tagX, tagY, tagW, tagH, 2.5);
          ctx.fill();

          if (accent) {
            ctx.fillStyle = accent;
            ctx.fillRect(tagX + 1.5, tagY + 2, 2, tagH - 4);
          }

          ctx.save();
          ctx.beginPath();
          ctx.rect(tagX + padLeft - 1, tagY, tagW - padLeft - 1, tagH);
          ctx.clip();
          ctx.fillStyle = coreBorder;
          ctx.textBaseline = "middle";
          ctx.fillText(text, tagX + padLeft, tagY + tagH / 2 + 0.5);
          ctx.restore();
        }
      }

      ctx.restore();
    });
  }
}

class ZhongshuPaneView implements IPrimitivePaneView {
  private readonly _renderer: ZhongshuPaneRenderer;

  constructor(palette: ChartPalette) {
    this._renderer = new ZhongshuPaneRenderer(palette);
  }

  update(boxes: PixelBox[], palette: ChartPalette): void {
    this._renderer.update(boxes, palette);
  }

  zOrder(): PrimitivePaneViewZOrder {
    return "normal";
  }

  renderer(): IPrimitivePaneRenderer {
    return this._renderer;
  }
}

/**
 * Custom Lightweight Charts v5 ISeriesPrimitive drawing Chanlun Zhongshu (`[ZD, ZG]`) boxes
 * with strict visual separation between the founding 3-unit core and genuine extension tails.
 */
export class ZhongshuBoxesPrimitive implements ISeriesPrimitive<Time> {
  private _attached: SeriesAttachedParameter<Time, SeriesType> | null = null;
  private _items: ZhongshuBoxDrawItem[] = [];
  private _visible = true;
  private _palette: ChartPalette;
  private readonly _paneView: ZhongshuPaneView;
  private readonly _paneViews: readonly IPrimitivePaneView[];

  constructor(palette: ChartPalette) {
    this._palette = palette;
    this._paneView = new ZhongshuPaneView(palette);
    /* Lightweight Charts caches by array reference; keep stable array */
    this._paneViews = [this._paneView];
  }

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this._attached = param;
    this.updateAllViews();
  }

  detached(): void {
    this._attached = null;
  }

  setBoxes(items: ZhongshuBoxDrawItem[]): void {
    this._items = items;
    this.updateAllViews();
    this._attached?.requestUpdate();
  }

  setVisible(visible: boolean): void {
    this._visible = visible;
    this.updateAllViews();
    this._attached?.requestUpdate();
  }

  setPalette(palette: ChartPalette): void {
    this._palette = palette;
    this.updateAllViews();
    this._attached?.requestUpdate();
  }

  updateAllViews(): void {
    if (!this._attached || !this._visible || this._items.length === 0) {
      this._paneView.update([], this._palette);
      return;
    }
    const { chart, series } = this._attached;
    const timeScale = chart.timeScale();
    const computed: PixelBox[] = [];

    for (const item of this._items) {
      const yZg = series.priceToCoordinate(item.zg);
      const yZd = series.priceToCoordinate(item.zd);
      if (yZg === null || yZd === null) continue;

      const coreX1 = item.coreStartTime !== null ? timeScale.timeToCoordinate(item.coreStartTime) : null;
      const coreX2 = item.coreEndTime !== null ? timeScale.timeToCoordinate(item.coreEndTime) : null;
      const extX1 = item.extStartTime !== null ? timeScale.timeToCoordinate(item.extStartTime) : null;
      const extX2 = item.extEndTime !== null ? timeScale.timeToCoordinate(item.extEndTime) : null;
      const nineX =
        item.nineUpgradeTime !== null && item.nineUpgradeTime !== undefined
          ? timeScale.timeToCoordinate(item.nineUpgradeTime)
          : null;

      const hasCore = coreX1 !== null && coreX2 !== null;
      const hasExt = extX1 !== null && extX2 !== null;
      if (!hasCore && !hasExt) continue;

      computed.push({
        coreX1: hasCore ? coreX1 : null,
        coreX2: hasCore ? coreX2 : null,
        extX1: hasExt ? extX1 : null,
        extX2: hasExt ? extX2 : null,
        nineX,
        yTop: yZg,
        yBottom: yZd,
        compactCode: item.compactCode,
        rangeText: `${fmtPrice(item.zd)}–${fmtPrice(item.zg)}`,
        kind: item.kind,
        oscillationStrokes: item.oscillationStrokes ?? 0,
        isExtended: Boolean(item.isExtended),
        isNineUpgrade: Boolean(item.isNineUpgrade),
        relationToPrev: item.relationToPrev ?? "initial",
        clampedLeft: Boolean(item.clampedLeft),
        clampedRight: Boolean(item.clampedRight),
      });
    }

    this._paneView.update(computed, this._palette);
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this._paneViews;
  }
}
