import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { ChartPalette } from "../../lib/theme";
import { fmtPrice } from "../../lib/format";

export interface ZhongshuBoxDrawItem {
  key: string;
  kind: "stroke" | "segment";
  label: string;
  startTime: Time;
  endTime: Time;
  startLabel?: string;
  endLabel?: string;
  zd: number;
  zg: number;
  dd?: number;
  gg?: number;
  isExtended?: boolean;
  isNineUpgrade?: boolean;
}

type RenderTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];

interface PixelBox {
  x1: number;
  x2: number;
  yTop: number;
  yBottom: number;
  label: string;
  rangeText: string;
  kind: "stroke" | "segment";
  isNineUpgrade?: boolean;
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
        const left = Math.max(0, Math.min(box.x1, box.x2));
        const right = Math.min(w, Math.max(box.x1, box.x2));
        if (right <= 0 || left >= w) continue;

        const width = Math.max(6, right - left);
        const top = Math.min(box.yTop, box.yBottom);
        const bottom = Math.max(box.yTop, box.yBottom);
        if (bottom < 0 || top > h) continue;
        const height = Math.max(2, bottom - top);

        const isSeg = box.kind === "segment";
        const fill = isSeg ? pal.zsSegFill : pal.zsStrokeFill;
        const border = isSeg ? pal.zsSegBorder : pal.zsStrokeBorder;

        /* Translucent Zhongshu body */
        ctx.fillStyle = fill;
        ctx.fillRect(left, top, width, height);

        /* Crisp ZG (top) and ZD (bottom) horizontal boundaries */
        ctx.strokeStyle = border;
        ctx.lineWidth = isSeg || box.isNineUpgrade ? 1.75 : 1.35;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(left, top);
        ctx.lineTo(left + width, top);
        ctx.moveTo(left, bottom);
        ctx.lineTo(left + width, bottom);
        ctx.stroke();

        /* Subtle dashed left/right vertical walls */
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(left, top);
        ctx.lineTo(left, bottom);
        ctx.moveTo(left + width, top);
        ctx.lineTo(left + width, bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        /* Inline Zhongshu badge (`笔中枢 Z1` / `线段中枢 S1`) wearing primary ink token */
        if (width >= 34 && top >= -12 && top <= h - 6) {
          const text = width >= 118 ? `${box.label} [${box.rangeText}]` : box.label;
          ctx.font = '600 10.5px system-ui, -apple-system, "Segoe UI", sans-serif';
          const textW = ctx.measureText(text).width;
          const pillW = Math.min(width - 4, textW + 10);
          const pillH = 16;
          const pillX = left + 3;
          const pillY = Math.max(2, top - pillH - 2);

          ctx.fillStyle = pal.elevated;
          ctx.strokeStyle = border;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(pillX, pillY, pillW, pillH, 3);
          ctx.fill();
          ctx.stroke();

          ctx.save();
          ctx.beginPath();
          ctx.rect(pillX + 2, pillY, pillW - 4, pillH);
          ctx.clip();
          ctx.fillStyle = pal.ink;
          ctx.textBaseline = "middle";
          ctx.fillText(text, pillX + 5, pillY + pillH / 2 + 0.5);
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
 * directly on the candlestick pane canvas.
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
      const x1 = timeScale.timeToCoordinate(item.startTime);
      const x2 = timeScale.timeToCoordinate(item.endTime);
      const yZg = series.priceToCoordinate(item.zg);
      const yZd = series.priceToCoordinate(item.zd);
      if (x1 === null || x2 === null || yZg === null || yZd === null) continue;
      computed.push({
        x1,
        x2,
        yTop: yZg,
        yBottom: yZd,
        label: item.label,
        rangeText: `${fmtPrice(item.zd)}–${fmtPrice(item.zg)}`,
        kind: item.kind,
        isNineUpgrade: item.isNineUpgrade,
      });
    }

    this._paneView.update(computed, this._palette);
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this._paneViews;
  }
}
