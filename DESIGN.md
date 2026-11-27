---
version: alpha
name: T3 Trading Desk
description: Precision quantitative terminal paired with institutional investment-committee memo clarity for 1Q-1Y Chanlun multi-asset research and execution.
colors:
  primary: "#5b95ff"
  primary-hover: "#79aaff"
  on-primary: "#080a0d"
  primary-light: "#1d4ed8"
  primary-hover-light: "#1e40af"
  on-primary-light: "#ffffff"
  bull: "#2ebd85"
  bull-light: "#046c4e"
  bear: "#f26b6b"
  bear-light: "#b91c1c"
  good: "#2ebd85"
  good-light: "#046c4e"
  warning: "#f59e0b"
  warning-light: "#92400e"
  serious: "#fb923c"
  serious-light: "#9a3412"
  critical: "#f26b6b"
  critical-light: "#b91c1c"
  obsidian-page: "#0c0e12"
  obsidian-sunken: "#080a0d"
  obsidian-surface: "#12151b"
  obsidian-paper: "#14171e"
  obsidian-elevated: "#191d26"
  obsidian-subtle: "#222733"
  obsidian-gridline: "#1b202a"
  obsidian-ink: "#eef2f6"
  obsidian-ink-secondary: "#b4becc"
  obsidian-muted: "#8e9bae"
  slate-page: "#f3f5f8"
  slate-sunken: "#e9edf2"
  slate-surface: "#ffffff"
  slate-paper: "#fcfbf8"
  slate-elevated: "#f8fafc"
  slate-subtle: "#e2e8f0"
  slate-gridline: "#e5e9f0"
  slate-ink: "#0f172a"
  slate-ink-secondary: "#3f4d63"
  slate-muted: "#515f73"
  viz-series-1: "#5b95ff"
  viz-series-2: "#2ebd85"
  viz-series-3: "#a78bfa"
  viz-series-4: "#fb923c"
  viz-series-1-light: "#1d4ed8"
  viz-series-2-light: "#046c4e"
  viz-series-3-light: "#6d28d9"
  viz-series-4-light: "#9a3412"
typography:
  headline-page:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.02em"
    fontFeature: '"cv02", "cv03", "cv04", "cv11"'
  headline-editorial:
    fontFamily: "Noto Serif SC, Source Han Serif SC, Georgia, serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.015em"
  title-section:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.01em"
  kpi-figure:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, monospace"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: "-0.025em"
  body-ui:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  body-editorial:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "14.5px"
    fontWeight: 400
    lineHeight: 1.8
  data-cell:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, monospace"
    fontSize: "12.5px"
    fontWeight: 500
    lineHeight: 1.45
  label-control:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.3
  label-meta:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.35
    letterSpacing: "0.02em"
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "12px"
  full: "9999px"
spacing:
  xxs: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  container-max: "1680px"
  header-height: "52px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.on-primary}"
  button-primary-light:
    backgroundColor: "{colors.primary-light}"
    textColor: "{colors.on-primary-light}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  button-primary-light-hover:
    backgroundColor: "{colors.primary-hover-light}"
    textColor: "{colors.on-primary-light}"
  button-secondary:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  button-secondary-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  button-danger:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.critical}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  button-danger-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.critical-light}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "12px"
    height: "32px"
  input-field:
    backgroundColor: "{colors.obsidian-sunken}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.body-ui}"
    rounded: "{rounded.sm}"
    padding: "10px"
    height: "32px"
  input-field-light:
    backgroundColor: "{colors.slate-sunken}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.body-ui}"
    rounded: "{rounded.sm}"
    padding: "10px"
    height: "32px"
  card-panel:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.obsidian-ink}"
    rounded: "{rounded.lg}"
    padding: "16px"
  card-panel-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.lg}"
    padding: "16px"
  badge-neutral:
    backgroundColor: "{colors.obsidian-sunken}"
    textColor: "{colors.obsidian-ink-secondary}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-neutral-light:
    backgroundColor: "{colors.slate-sunken}"
    textColor: "{colors.slate-ink-secondary}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  chip-filter:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.obsidian-ink-secondary}"
    typography: "{typography.label-control}"
    rounded: "{rounded.sm}"
    padding: "9px"
    height: "26px"
  table-header:
    backgroundColor: "{colors.obsidian-sunken}"
    textColor: "{colors.obsidian-muted}"
    typography: "{typography.label-meta}"
    padding: "12px"
  table-header-light:
    backgroundColor: "{colors.slate-sunken}"
    textColor: "{colors.slate-muted}"
    typography: "{typography.label-meta}"
    padding: "12px"
  dialog-modal:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.obsidian-ink}"
    rounded: "{rounded.xl}"
    padding: "20px"
    width: "640px"
  page-canvas:
    backgroundColor: "{colors.obsidian-page}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.body-ui}"
  page-canvas-light:
    backgroundColor: "{colors.slate-page}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.body-ui}"
  paper-memo:
    backgroundColor: "{colors.obsidian-paper}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.body-editorial}"
    rounded: "{rounded.lg}"
    padding: "24px"
  paper-memo-light:
    backgroundColor: "{colors.slate-paper}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.body-editorial}"
    rounded: "{rounded.lg}"
    padding: "24px"
  table-row-hover:
    backgroundColor: "{colors.obsidian-elevated}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.data-cell}"
    padding: "12px"
  table-row-hover-light:
    backgroundColor: "{colors.slate-elevated}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.data-cell}"
    padding: "12px"
  pill-subtle:
    backgroundColor: "{colors.obsidian-subtle}"
    textColor: "{colors.obsidian-muted}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  pill-subtle-light:
    backgroundColor: "{colors.slate-subtle}"
    textColor: "{colors.slate-muted}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-bull:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.bull}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-bull-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.bull-light}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-bear:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.bear}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-bear-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.bear-light}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-good:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.good}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-good-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.good-light}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-warning:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.warning}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-warning-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.warning-light}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-serious:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.serious}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  badge-serious-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.serious-light}"
    typography: "{typography.label-meta}"
    rounded: "{rounded.xs}"
    padding: "8px"
  chart-surface:
    backgroundColor: "{colors.obsidian-gridline}"
    textColor: "{colors.obsidian-ink}"
    typography: "{typography.data-cell}"
  chart-surface-light:
    backgroundColor: "{colors.slate-gridline}"
    textColor: "{colors.slate-ink}"
    typography: "{typography.data-cell}"
  chart-legend-1:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.viz-series-1}"
    typography: "{typography.label-meta}"
  chart-legend-2:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.viz-series-2}"
    typography: "{typography.label-meta}"
  chart-legend-3:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.viz-series-3}"
    typography: "{typography.label-meta}"
  chart-legend-4:
    backgroundColor: "{colors.obsidian-surface}"
    textColor: "{colors.viz-series-4}"
    typography: "{typography.label-meta}"
  chart-legend-1-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.viz-series-1-light}"
    typography: "{typography.label-meta}"
  chart-legend-2-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.viz-series-2-light}"
    typography: "{typography.label-meta}"
  chart-legend-3-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.viz-series-3-light}"
    typography: "{typography.label-meta}"
  chart-legend-4-light:
    backgroundColor: "{colors.slate-surface}"
    textColor: "{colors.viz-series-4-light}"
    typography: "{typography.label-meta}"
---

# Design System: T3 Trading Desk

## Overview

**Creative North Star: "The Precision Quantitative Terminal × Institutional IC Memo"**

T3 Trading Desk is an authenticated Operate-mode workbench built for medium-to-long-horizon (1Q–1Y) multi-asset macro research, daily + 30-minute Chanlun structural analysis, and disciplined portfolio risk execution. The interface rejects marketing theatrics and retail-broker clutter in favor of instrument-grade clarity: dense where quantitative comparison demands scanability, calm and paper-warm where investment-committee theses (`thesis.md`) require sustained reading.

The default visual world is **Obsidian Slate Dark**, engineered for long trading-desk sessions without glare, paired with an equally normative **Crisp Slate Light** counterpart that preserves identical semantic hierarchy and WCAG AA contrast ratios across daylight environments. Every pixel earns its place by answering one of four operational questions: What is the macro regime? Where is the Chanlun structure? Does the hard evidence hold? Should risk be held, scaled, or exited?

**Key Characteristics:**
- **Strict semantic decoupling**: Cobalt interactive accent (`primary`) is reserved exclusively for actions, active navigation, and focus states—never overloaded onto bullish, bearish, or risk-sentinel meaning.
- **Tonal layering over heavy shadows**: Depth comes from stepped obsidian/slate luminance planes and hairline borders rather than dramatic drop shadows.
- **Micro-radius precision**: Crisp 4px–10px corner geometry distinguishes read-only badges (4px), interactive controls (6px), tables/subtle wells (8px), and primary workspace panels (10px).
- **Tabular numerical discipline**: Prices, percentages, stop distances, and risk budgets always render in monospace with `tabular-nums` and right alignment in tables.
- **Paper-grade thesis reading**: Long-form research memos sit inside a 72ch measure with generous 1.8 line-height and warm paper surface tint (`obsidian-paper` / `slate-paper`).

## Colors

The palette pairs calibrated obsidian-dark and cool-slate-light neutral ramps with one cobalt interactive voice and an independent directional/risk semantic system, all tuned so every foreground-on-surface and text-on-tinted-badge pairing passes WCAG 2.1 AA (`>= 4.5:1` for normal text).

### Primary
- **Terminal Cobalt Dark** (`primary`, `#5b95ff`) paired with **Obsidian Ink On-Accent** (`on-primary`, `#080a0d`): Primary CTAs, active tabs, keyboard focus rings, and selection highlights in dark mode (`6.78:1` on primary button; `6.26:1` text on dark surface). Hover brightens to **Luminous Cobalt** (`primary-hover`, `#79aaff`, `8.50:1` with `on-primary`).
- **Institutional Cobalt Light** (`primary-light`, `#1d4ed8`) paired with **Pure White On-Accent** (`on-primary-light`, `#ffffff`): Primary CTAs, active underlines, and links in light mode (`6.70:1` on primary button; `5.70:1` text on sunken wells). Hover deepens to **Deep Cobalt** (`primary-hover-light`, `#1e40af`, `8.72:1`).

### Secondary (Directional & Risk Semantics)
- **Jade Bull / Healthy Hold** (`bull` / `good`: `#2ebd85` dark, `#046c4e` light): Confirmed bullish structures, positive P&L, and `GREEN_HOLD` sentinel status. Never used for primary navigation or generic buttons.
- **Signal Crimson Bear / Exit** (`bear` / `critical`: `#f26b6b` dark, `#b91c1c` light): Confirmed bearish structures, negative P&L, stop-loss lines, destructive actions, and `RED_EXIT` sentinel alerts.
- **Desk Amber Warning** (`warning`: `#f59e0b` dark, `#92400e` light): `YELLOW_LOOSENING` alerts, stale data isolation, watch-list states, and Chanlun segment-level (`线段`) highlights.
- **Ember Serious** (`serious`: `#fb923c` dark, `#9a3412` light): Elevated risk warnings such as chase-risk alerts, concentration warnings, and approaching roll/event windows.

### Neutral
- **Obsidian Canvas & Layers (Dark Mode)**: **Obsidian Void** (`obsidian-sunken`, `#080a0d`) for recessed inputs, table headers, and segmented tracks; **Obsidian Base** (`obsidian-page`, `#0c0e12`) for the viewport backdrop; **Obsidian Surface** (`obsidian-surface`, `#12151b`) for cards, modals, and drawers; **Obsidian Paper** (`obsidian-paper`, `#14171e`) for `thesis.md` reading panes; **Obsidian Elevated** (`obsidian-elevated`, `#191d26`) for hover rows and lifted pills; **Obsidian Subtle** (`obsidian-subtle`, `#222733`) for meter tracks and secondary tags.
- **Obsidian Ink Scale (Dark Mode)**: **Frost Ink** (`obsidian-ink`, `#eef2f6`, `>= 13.28:1`), **Slate Silver** (`obsidian-ink-secondary`, `#b4becc`, `>= 7.95:1`), and **Muted Steel** (`obsidian-muted`, `#8e9bae`, `>= 5.30:1` even on `obsidian-subtle`).
- **Crisp Slate Layers (Light Mode)**: **Cool Mist** (`slate-page`, `#f3f5f8`) viewport, **Recessed Fog** (`slate-sunken`, `#e9edf2`) inputs/table headers, **Pure Card White** (`slate-surface`, `#ffffff`) panels, **Warm Memo Paper** (`slate-paper`, `#fcfbf8`) editorial panes, **Hover Alabaster** (`slate-elevated`, `#f8fafc`), and **Track Slate** (`slate-subtle`, `#e2e8f0`).
- **Slate Ink Scale (Light Mode)**: **Deep Navy Ink** (`slate-ink`, `#0f172a`, `>= 14.48:1`), **Graphite Slate** (`slate-ink-secondary`, `#3f4d63`, `>= 6.94:1`), and **Legible Pewter** (`slate-muted`, `#515f73`, `>= 5.27:1` across every light surface including `slate-subtle`).

### Named Rules
**The Decoupled Cobalt Rule.** Cobalt (`primary`) communicates interaction and focus only; green/red/amber communicate market direction and risk only. Never color a neutral action button green or use cobalt to imply a bullish call.

**The Dual-Theme Contrast Floor Rule.** Every foreground token—including muted metadata and tinted semantic badges—must hold at least `4.5:1` contrast against every surface it can sit on (`surface`, `page`, `sunken`, `subtle`, and `14%` semantic tints) in both Obsidian Dark and Crisp Slate Light. Never drop text legibility below `4.5:1` via raw `opacity-*` stacking on already-muted text.

**The Zero-Hardcoded-Palette Rule.** UI components reference semantic CSS custom properties (`var(--accent)`, `var(--warning)`, `var(--muted)`, etc.) and never hardcode Tailwind utility hues like `text-amber-400` or `border-amber-500/60`. Canvas/SVG charts read exclusively from the synchronized `ChartPalette` in `theme.tsx`.

## Typography

**UI & Heading Font:** `Inter` with system CJK fallbacks (`PingFang SC`, `Hiragino Sans GB`, `Microsoft YaHei`, sans-serif) and OpenType alternates `"cv02", "cv03", "cv04", "cv11"`.
**Editorial Memo Font:** `Noto Serif SC` / `Source Han Serif SC` / `Georgia` (strictly reserved for long-form research thesis titles).
**Numerical & Ticker Font:** `JetBrains Mono` with `tabular-nums` (`font-variant-numeric: tabular-nums`).

**Character:** Tight, high-density operational typography with a disciplined fixed scale (no fluid `clamp()` jumps) so multi-column tables, KPI strips, and side drawers stay aligned across wide trading monitors.

### Hierarchy
- **Page Headline** (`headline-page`: 650 weight, 20px, 1.25 line-height, -0.02em tracking): Primary screen title at the top of each of the four views.
- **Editorial Headline** (`headline-editorial`: 700 weight, 22px, 1.30 line-height): Reserved exclusively for `thesis.md` research report headers inside ArchiveView.
- **Section Title** (`title-section`: 600 weight, 15px, 1.35 line-height): Modal headers, drawer titles, and primary panel headings.
- **KPI Figure** (`kpi-figure`: 600 weight, 22px, 1.15 line-height, JetBrains Mono): Top-level portfolio equity, risk budget utilization, and headline macro figures.
- **UI Body** (`body-ui`: 400 weight, 13px, 1.50 line-height): Default table rows, form labels, status descriptions, and card body copy.
- **Editorial Body** (`body-editorial`: 400 weight, 14.5px, 1.80 line-height, max 72ch): Long-form thesis prose inside `.prose-desk`.
- **Data Cell** (`data-cell`: 500 weight, 12.5px, 1.45 line-height, JetBrains Mono): Numeric columns, Chanlun `[ZD, ZG]` price bands, stop distances, and ratios.
- **Control Label** (`label-control`: 500 weight, 12px, 1.30 line-height): Buttons, chips, tabs, and segmented navigation items.
- **Meta Label** (`label-meta`: 500 weight, 11px, 1.35 line-height, 0.02em tracking): Table column headers, badges, keyboard hints, and timestamps.

### Named Rules
**The 11px Floor Rule.** No legible UI text—especially Chinese characters or financial numerals—may drop below `11px` (`0.6875rem`). Sub-11px (`9px`, `9.5px`, `10px`) text is banned from labels, pills, legends, and tables.

**The No-Uppercase-CJK-Eyebrow Rule.** Never apply `uppercase tracking-wider` eyebrow kickers above section headings or Chinese labels. Headings carry their own visual weight through size, weight, and spacing.

**The Tabular Monospace Rule.** Monospace is strictly functional—used for tickers, prices, dates, percentages, R-multiples, and code identifiers, never as decorative styling for prose.

## Layout

The application uses a **Full-Width Capped Container** (`max-width: 1680px`, horizontal padding `16px` mobile / `24px` desktop) anchored by a single-row sticky navigation bar (`52px` height on desktop) housing brand mark, ordered four-stage workflow tabs (`Alt+1`–`4`), persistent live macro strip (`VIX` / `10Y` / `DXY`), global symbol command palette (`⌘K`), and theme cycler.

Spacing follows a **4px micro / 8px base rhythm** (`2px`, `4px`, `8px`, `12px`, `16px`, `24px`). Related operational controls sit tight (`4px–8px`), while distinct analytical sections separate cleanly (`16px–24px`). Responsive adaptation is structural: navigation collapses to compact labels on small screens, desktop side-by-side drawers (`460px` wide below the `52px` navbar) turn into full-height sheets on mobile, and `.data-table` inside `.table-wrapper` uses `@container (width < 600px)` to pivot wide multi-column tables into labeled stacked cards without horizontal clipping.

## Elevation & Depth

T3 Trading Desk is **flat-by-default and tonally layered**. Primary hierarchy is communicated by stepping between `sunken` → `page` → `surface` → `elevated` combined with `1px` hairline borders (`rgba(255,255,255,0.085)` dark, `rgba(15,23,42,0.095)` light).

Shadows are strictly restrained to three functional roles:
- **Card Grounding**: Subtle surface grounding (`0 1px 2px rgba(15,23,42,0.04), 0 4px 12px rgba(15,23,42,0.025)` light; `inset 0 1px 0 rgba(255,255,255,0.035), 0 2px 10px rgba(0,0,0,0.28)` dark).
- **Detail Drawer Overlay**: Directional lateral shadow (`-14px 0 40px rgba(0,0,0,0.28)`) separating the non-modal right inspection drawer from the underlying workbench.
- **Native Modal Dialog**: Deep ambient shadow (`0 24px 64px rgba(0,0,0,0.48)`) paired with a `rgba(6,9,14,0.62)` backdrop blur (`4px`).

### Named Rules
**The No-Nested-Cards Rule.** Never nest `.card` inside `.card` or wrap KPI cells in individual bordered boxes inside a card. Use `.metric-strip` (hairline top/bottom dividers + subtle vertical indicator rules) or `.card-subtle` (`sunken` background + soft hairline, zero shadow) for secondary sub-regions.

**The Single-Elevation-Source Rule.** Overlay containers like `.detail-drawer-sheet` define their positioning, border, and shadow once in CSS; never stack conflicting Tailwind `shadow-2xl` or duplicate `fixed` utilities on the same element.

## Shapes

The system speaks in **Engineered Micro-Radius Geometry**, assigning a distinct radius to every component tier so interactive affordances can be read at a glance:
- **4px (`xs`)**: Read-only status badges, direction badges, inline code spans, keyboard `<kbd>` chips.
- **6px (`sm`)**: Interactive buttons (`.btn`), filter/toggle chips (`.chip`), text inputs (`.input`), segmented buttons (`.segmented-btn`).
- **8px (`md`)**: Segmented control tracks (`.segmented`), table wrappers (`.table-wrapper`), subtle recessed wells (`.card-subtle`), callout blocks (`.callout-editorial`).
- **10px (`lg`)**: Primary workspace panels and cards (`.card`, `.panel`).
- **12px (`xl`)**: Native `<dialog>` modals (`.modal-dialog`).
- **9999px (`full`)**: Strictly reserved for `4px`-tall risk progress bars (`.risk-meter`) and `6px` status indicator dots—never pill-shaped action buttons or filter chips.

### Named Rules
**The No-Side-Tab-Bar Rule.** Never place a thick one-sided accent bar (`border-left: 3px`, `border-inline-start: 3px`) along the edge of a rounded card, callout, or blockquote. Communicate status or editorial emphasis via a crisp full perimeter border (`1px`), low-opacity semantic tint, or leading badge/icon.

## Components

Every interactive component ships with complete visual states (`default`, `hover`, `focus-visible`, `active`, `disabled`) and fast, functional transitions (`120ms–180ms ease`). Browser surfaces—text `::selection`, `caret-color`, `accent-color`, thin scrollbars, and `2px solid var(--accent)` `:focus-visible` rings—are themed globally from the token palette.

### Buttons
- **Shape**: Crisp micro-radius (`6px`), minimum height `32px` (`.btn-sm` `26px`), horizontal padding `12px` (`9px` small).
- **Primary (`.btn-primary`)**: Solid `primary` fill (`#5b95ff` dark / `#1d4ed8` light) with high-contrast `on-primary` text (`#080a0d` dark / `#ffffff` light). Hover shifts to `primary-hover`. Reserved for the primary commitment action per section.
- **Secondary (`.btn`)**: `surface` background, `1px` hairline border, `ink` text; hover lifts to `elevated` with strengthened border.
- **Ghost (`.btn-ghost`)**: Transparent resting state, `ink-2` text; hover reveals `elevated` fill and `ink` text.
- **Danger (`.btn-danger`)**: Transparent fill with `critical` border and text; hover applies `11%` critical tint.
- **Copy rule**: Name the concrete action without trailing decorative arrows (`→`).

### Chips & Segmented Controls
- **Filter Chips (`.chip`)**: `6px` radius, `26px` height, transparent fill with hairline border. Selected state (`aria-pressed="true"`) applies `14%` accent tint (or `16%` bull/bear tint for directional chips) with strengthened semantic border. Never use `rounded-full` pill shapes for interactive filters.
- **Segmented Controls (`.segmented` / `.segmented-btn`)**: Recessed `sunken` track (`8px` radius, `2px` padding) containing `6px` buttons; active item (`aria-current="page"` / `aria-pressed="true"`) lifts onto `surface` with `600` weight.

### Status & Directional Badges
- **Shape**: `4px` radius (visually distinct from `6px` interactive chips), `11px` font size, compact `2px 8px` padding.
- **Contrast pattern**: `StatusBadge` renders a semantic status dot/icon with high-contrast `ink` text over a `11%–13%` tinted background. `DirectionBadge` and `GateDecisionBadge` use semantic foreground tokens calibrated to `>= 5.0:1` over their `14%` tinted surfaces in both themes.

### Inputs & Fields
- **Style**: Recessed `sunken` fill, `1px` hairline border, `6px` radius, `32px` min-height, `tabular-nums` enabled by default.
- **Focus**: `2px solid var(--accent)` outline with `surface` background transition. Bare `<input>` or `<select>` elements inside composite wrappers must wire `:focus-within` on the wrapper so keyboard focus is never invisible.

### Tables & Metric Strips
- **Data Tables (`.table-wrapper` + `.data-table`)**: Sticky `sunken` header (`11.5px` muted text), `surface` rows with soft hairline dividers, `elevated` hover state, right-aligned monospace `.num` cells, and automatic `@container (width < 600px)` stacked layout. Clickable rows expose `tabIndex={0}` and `Enter`/`Space` keyboard activation.
- **Metric Strips (`.metric-strip` / `.metric-cell`)**: Horizontal hairline top/bottom framing with vertical `2px` hairline column dividers, replacing nested KPI boxes.

### Tabs, Drawers & Native Dialogs
- **Tabs (`.mwg-tablist` + `.mwg-tab`)**: Single-line scrollable tablist with `2.5px` bottom accent underline via `::after` (survives forced-colors with zero layout shift).
- **Right Inspection Drawer (`.detail-drawer-sheet`)**: Non-modal desktop side panel (`min(460px, 92vw)`, pinned below the `52px` sticky header on `lg+`) supporting side-by-side scanning without dimming the workbench.
- **Modals (`ModalDialog`)**: Native HTML `<dialog>` opened via `showModal()`, `12px` radius, `640px` max-width (`680px` for Command Palette), semantic `<h1>` `aria-labelledby`, and `closedby` support.

## Do's and Don'ts

### Do:
- **Do** keep Cobalt (`primary`) strictly for interactive controls, active tabs, and focus rings, and reserve green/red/amber solely for market direction and sentinel risk.
- **Do** validate every text/surface combination against WCAG AA (`>= 4.5:1`) in both Obsidian Dark and Crisp Slate Light using `npx @google/design.md lint DESIGN.md`.
- **Do** use `11px` (`0.6875rem`) as the hard minimum font size across all badges, chart legends, table headers, and metadata.
- **Do** format all prices, percentages, stop distances, and risk figures with `font-mono` and `tabular-nums` (`.tnum` / `td.num`).
- **Do** ensure every clickable card, table row, SVG data point, and composite input has full keyboard focus (`:focus-visible` / `:focus-within`) and `Enter`/`Space` handling.
- **Do** use `.metric-strip` and `.card-subtle` for internal hierarchy inside panels instead of stacking bordered cards inside cards.

### Don't:
- **Don't** put thick one-sided accent bars (`border-l-[3px]`, `border-inline-start: 3px solid`) on rounded cards, callouts, or blockquotes.
- **Don't** hardcode Tailwind color utilities (`text-amber-400`, `border-amber-500/60`, `bg-blue-500`) in components; always consume semantic CSS tokens.
- **Don't** put decorative trailing arrows (`→`) inside CTA buttons or `uppercase tracking-wider` eyebrow kickers above Chinese headings.
- **Don't** stack `opacity-45`/`opacity-60` on already-muted text (`text-muted`), which collapses contrast well below WCAG AA.
- **Don't** mix `rounded-full` filter pills with `6px` `.chip` controls or bypass `.data-table` / `.btn` / `.input` primitives without an explicit domain reason.
- **Don't** use decorative gradients, gratuitous page-load animations, or emojis as replacement icons when `lucide-react` strokes belong there.
