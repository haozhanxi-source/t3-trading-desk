/* Typed contract for the T3 Trading Desk FastAPI backend (127.0.0.1:8055).
 * Strictly aligned with backend/app.py (ConfigDict(extra="forbid")) and backend/tasks.py. */

export type Direction = "long" | "short";
export type ScreenId = "sentinel" | "warroom" | "archive" | "workbench";
export type Dict = Record<string, unknown>;

/* ---------------- Health & search ---------------- */
export interface HealthResponse {
  status: string;
  version: string;
  portfolio_initialized: boolean;
  archive_count: number;
  universe_count: number;
  now_utc: string;
}

export interface FxReferenceResponse {
  quote_currency: string;
  account_currency: string;
  available: boolean;
  fx_to_account: number | null;
  account_to_quote: number | null;
  usd_cny: number | null;
  cny_usd: number | null;
  as_of_date: string | null;
  source?: string | null;
  error?: string | null;
  fetched_at_utc?: string;
}

export interface SymbolSearchItem {
  symbol: string;
  label: string;
  asset_class: string;
  sources: string[];
  pool?: string | null;
  status?: string | null;
}

/* ---------------- Portfolio ---------------- */
export interface SectorSlot {
  count: number;
  max_slots: number;
  symbols: string[];
  position_ids: string[];
  open_risk_amount: number;
  sector_risk_cap: number;
  remaining_sector_risk_budget?: number;
}

export interface MacroFactorSlot {
  count: number;
  symbols: string[];
  open_risk_amount: number;
  open_risk_pct: number;
  cap: number;
  overexposed: boolean;
}

export interface Position {
  position_id: string;
  symbol: string;
  name?: string;
  market?: string;
  asset_class?: string;
  direction: Direction;
  sector_theme: string;
  leader_role?: "sole_leader" | "co_leader" | string;
  co_leader_reason?: string | null;
  entry_price?: number;
  avg_entry_price?: number;
  quantity: number;
  initial_quantity?: number;
  fx_to_account?: number;
  partial_realized_pnl?: number;
  tranches?: unknown[];
  tactical_stop_30m?: number | null;
  structural_stop_daily?: number | null;
  target_price?: number | null;
  macro_factors?: string[];
  exit_red_lines?: unknown[];
  current_open_risk_amount?: number;
  initial_risk_amount?: number;
  status?: string;
  opened_at_utc?: string;
  notes?: string | null;
  [k: string]: unknown;
}

export interface PortfolioSummary {
  base_currency: string;
  equity: number;
  insolvent?: boolean;
  peak_equity: number;
  drawdown_pct: number;
  risk_scale: number;
  single_sector_risk_cap: number;
  max_total_open_risk_cap: number;
  max_macro_factor_risk_cap: number;
  open_positions_count: number;
  closed_positions_count: number;
  total_open_risk_amount: number;
  total_open_risk_pct: number;
  total_estimated_margin_account?: number;
  margin_utilization_pct?: number;
  remaining_portfolio_risk_budget?: number;
  total_locked_profit_amount?: number;
  sector_slots: Record<string, SectorSlot>;
  macro_factor_concentration: Record<string, MacroFactorSlot>;
  open_positions?: Position[];
  revision?: number;
  updated_at_utc?: string | null;
}

export interface JournalEntry {
  ts_utc?: string;
  at_utc?: string;
  timestamp_utc?: string;
  action?: string;
  event?: string;
  symbol?: string;
  position_id?: string;
  detail?: unknown;
  [k: string]: unknown;
}

export interface PortfolioSummaryResponse {
  initialized: boolean;
  summary: PortfolioSummary | null;
  positions: Position[];
  journal: JournalEntry[];
}

export interface PortfolioInitBody {
  equity: number;
  base_currency: string;
  default_risk_per_trade_pct: number;
  max_total_open_risk_pct: number;
  max_macro_factor_risk_pct: number;
  overwrite?: boolean;
}

export interface OpenPositionBody {
  symbol: string;
  direction: Direction;
  sector_theme: string;
  entry_price: number;
  tactical_stop_30m: number;
  quantity: number;
  structural_stop_daily?: number;
  target_price?: number;
  fx_to_account?: number;
  leader_role?: string;
  co_leader_reason?: string;
  macro_factors?: string[];
  exit_red_lines?: string[];
  notes?: string;
  allow_equity_margin?: boolean;
}

export interface TrailStopBody {
  new_stop_30m: number;
  new_structural_stop_daily?: number;
  current_price?: number;
  reason?: string;
}

export interface TrimBody {
  trim_price: number;
  trim_quantity: number;
  reason?: string;
}

export interface CloseBody {
  exit_price: number;
  exit_reason: string;
  exit_category?: string;
}

export interface VoidBody {
  reason?: string;
}

export interface RotateNewPositionBody {
  symbol: string;
  direction: Direction;
  sector_theme: string;
  entry_price: number;
  tactical_stop_30m: number;
  quantity: number;
  structural_stop_daily?: number;
  target_price?: number;
  leader_role?: string;
  co_leader_reason?: string;
  macro_factors?: string[];
  exit_red_lines?: string[];
  fx_to_account?: number;
  notes?: string;
  reason?: string;
  allow_equity_margin?: boolean;
}

export interface RotatePositionBody {
  exit_price: number;
  exit_reason: string;
  new_position: RotateNewPositionBody;
  exit_category?: string;
  enforce_macro_cap?: boolean;
}

/* ---------------- Sentinel ---------------- */
export type SentinelStatus = "RED_EXIT" | "YELLOW_LOOSENING" | "GREEN_HOLD";

export interface SentinelAlert {
  layer?: string;
  code?: string;
  message?: string;
  detail?: string;
  [k: string]: unknown;
}

export interface SuggestedTrailStop {
  current_stop: number | null;
  suggested_stop: number | null;
  improvement_pct?: number | null;
  buffer_to_price_pct?: number | null;
  anchor?: string;
  rationale?: string;
}

export interface UpcomingEvent {
  date: string;
  days_until: number;
  title: string;
  source?: string;
  imminent?: boolean;
  symbol?: string;
}

export interface SentinelHolding {
  symbol: string;
  name?: string;
  market?: string;
  asset_class?: string;
  direction: string;
  sector_theme?: string | null;
  leader_role?: string | null;
  contract_or_expression?: string | null;
  position_id?: string | null;
  status: SentinelStatus;
  status_zh: string;
  action_headline?: string;
  last_price?: number | null;
  price_source?: string | null;
  entry_price?: number | null;
  tactical_stop_30m?: number | null;
  structural_stop_daily?: number | null;
  target_price?: number | null;
  unrealized_pnl_pct?: number | null;
  red_alerts: SentinelAlert[];
  yellow_alerts: SentinelAlert[];
  info_notes: string[];
  suggested_trail_stop?: SuggestedTrailStop | null;
  trigger_results?: Dict[];
  verifiable_trigger_ids?: string[];
  upcoming_events?: UpcomingEvent[];
  data_warnings?: unknown[];
  checked_at_utc?: string;
  [k: string]: unknown;
}

export interface SentinelWatchItem {
  symbol: string;
  name?: string;
  market?: string;
  status?: string;
  direction_bias?: string;
  fired_triggers?: Dict[];
  upcoming_events?: UpcomingEvent[];
  [k: string]: unknown;
}

export interface SentinelResponse {
  checked_at_utc: string;
  ref_date: string;
  summary: {
    holdings_total: number;
    red_count: number;
    yellow_count: number;
    green_count: number;
    watchlist_total: number;
    watchlist_fired_count: number;
    discovery_errors_count: number;
    state_sync_warnings_count: number;
  };
  discovery_errors: string[];
  state_sync_warnings: unknown[];
  holdings: SentinelHolding[];
  watchlist: SentinelWatchItem[];
}

/* ---------------- Market / Chanlun ---------------- */
export interface OhlcvDailyBar {
  time: string;
  label: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  dif?: number | null;
  dea?: number | null;
  hist?: number | null;
}

export interface OhlcvIntradayBar extends Omit<OhlcvDailyBar, "time"> {
  time: number;
}

export interface ChanlunStroke {
  direction: "up" | "down";
  start_date: string;
  end_date: string;
  start_price: number;
  end_price: number;
  high: number;
  low: number;
  start_raw_idx?: number;
  end_raw_idx?: number;
  raw_bars?: number;
  pct_change?: number;
  macd_area?: number;
  is_confirmed: boolean;
  strength_class?: number;
  strength_tier?: string;
}

export interface ChanlunSegment {
  seg_id: number;
  direction: "up" | "down";
  start_date: string;
  end_date: string;
  start_price: number;
  end_price: number;
  high: number;
  low: number;
  stroke_count: number;
  is_confirmed: boolean;
}

export type ZhongshuRelation =
  | "initial"
  | "new_up"
  | "new_down"
  | "expansion_up"
  | "expansion_down"
  | "overlap";

export type ZhongshuExtensionStage = "core" | "extended" | "upgraded_9";

export interface Zhongshu {
  zs_id: number;
  direction: "up" | "down" | "neutral" | string;
  zd: number;
  zg: number;
  dd?: number;
  gg?: number;
  start_date: string;
  end_date: string;
  stroke_count?: number;
  is_extended?: boolean;
  status?: string;
  oscillation_strokes?: number;
  oscillation_end_date?: string | null;
  oscillation_dd?: number | null;
  oscillation_gg?: number | null;
  is_nine_upgrade?: boolean;
  trend_zs_index?: number;
  relation_to_prev?: ZhongshuRelation | string | null;
  extension_stage?: ZhongshuExtensionStage | string | null;
  nine_upgrade_date?: string | null;
}

export interface SignalMeta {
  signal_type?: string;
  direction?: string | null;
  confirmed?: boolean;
  momentum_confirmed?: boolean;
  is_exhaustion?: boolean;
  signal_timestamp?: string | null;
}

export interface SignalSummary {
  tier?: string;
  summary?: string;
  action_bias?: string;
  signal_meta?: SignalMeta;
}

export interface MomentumAssessment {
  active_direction?: string;
  verdict?: string;
  curr_strength_tier?: string;
  area_ratio_vs_prev?: number | null;
  peak_ratio_vs_prev?: number | null;
  is_capital_consensus?: boolean;
  is_divergence_candidate?: boolean;
  is_non_divergence_power?: boolean;
  momentum_reliable?: boolean;
  momentum_reliability_note?: string | null;
  [k: string]: unknown;
}

export interface ChanlunTimeframe {
  timeframe?: string;
  last_close?: number;
  macro_structure?: string;
  zhongshu_level?: string;
  zhongshu_level_label?: string;
  zhongshu_count?: number;
  price_vs_zhongshu?: string;
  latest_fractal?: {
    kind: "top" | "bottom";
    date: string;
    price: number;
    confirmed_date?: string;
    just_confirmed?: boolean;
    strength?: string;
  } | null;
  latest_zhongshu?: Zhongshu | null;
  recent_zhongshus?: Zhongshu[];
  stroke_zhongshus?: Zhongshu[];
  segment_zhongshus?: Zhongshu[];
  all_strokes?: ChanlunStroke[];
  all_segments?: ChanlunSegment[];
  all_stroke_zhongshus?: Zhongshu[];
  all_segment_zhongshus?: Zhongshu[];
  stroke_end_trigger?: {
    direction?: string;
    trigger_kind?: string;
    level?: number | null;
    ref_date?: string;
    triggered?: boolean;
    description?: string;
  } | null;
  momentum_assessment?: MomentumAssessment | null;
  signals?: string[];
  error?: string;
}

export interface DualAlignment {
  desired_direction?: string | null;
  daily_bias?: string | null;
  daily_tier?: string;
  daily_summary?: string;
  daily_zd?: number | null;
  daily_zg?: number | null;
  daily_tactical_stop?: number | null;
  daily_structural_stop?: number | null;
  daily_reverse_k_watch?: number | null;
  intraday_timeframe?: string;
  intraday_bias?: string | null;
  intraday_action?: string | null;
  intraday_tier?: string;
  intraday_summary?: string;
  intraday_zd?: number | null;
  intraday_zg?: number | null;
  intraday_tactical_stop?: number | null;
  intraday_reverse_k_watch?: number | null;
  aligned?: boolean;
  alignment_status?: string;
  noise_reduction_pct?: number | null;
  notes?: string[];
}

export interface MacroQuote {
  last: number | null;
  chg_1d_pct?: number | null;
}

export interface ContractSpec {
  symbol?: string;
  asset_class?: string;
  multiplier?: number | null;
  min_order_qty?: number;
  lot_step?: number;
  lot_size?: number;
  currency?: string | null;
  quote_currency?: string | null;
  margin_rate?: number | null;
  fractional?: boolean;
  known_spec?: boolean;
  unit_note?: string;
  warning?: string;
}

export interface DataQualityReport {
  quality_grade?: "good" | "caution" | "degraded" | string;
  actionable_allowed?: boolean;
  bars_total?: number;
  raw_flat_bars_60?: number;
  raw_flat_ratio_60?: number;
  repaired_ohlc_bars_total?: number;
  repaired_ohlc_bars_60?: number;
  dropped_corrupt_bars?: number;
  as_of_bar?: string | null;
  latest_bar_closed?: boolean | null;
  warnings?: string[];
  notes?: string[];
}

export interface SyncMeta {
  cache_status?: "hit" | "miss" | "bypass" | string;
  sync_mode?: string;
  generation?: number;
  last_synced_at_utc?: string;
}

export interface MarketDualResponse {
  symbol: string;
  asset_class: string;
  contract_spec?: ContractSpec | null;
  data_source?: string;
  intraday_data_source?: string;
  intraday_interval?: string;
  fetched_at_utc?: string;
  data_quality?: DataQualityReport | null;
  intraday_data_quality?: DataQualityReport | null;
  sync_meta?: SyncMeta | null;
  intraday_sync_meta?: SyncMeta | null;
  technicals?: Dict;
  intraday_technicals?: Dict;
  macro_context?: Record<string, MacroQuote | unknown>;
  fundamentals?: Dict;
  crypto_perp_funding?: unknown;
  intraday_error?: string | null;
  ohlcv_daily: OhlcvDailyBar[];
  ohlcv_intraday: OhlcvIntradayBar[];
  chanlun: {
    signal_summary?: SignalSummary;
    daily?: ChanlunTimeframe;
    intraday?: ChanlunTimeframe;
    intraday_signal_summary?: SignalSummary;
    dual_alignment?: DualAlignment;
  };
}

/* ---------------- Trade planner (matches backend/app.py TradePlanReq) ---------------- */
export interface TradePlanBody {
  symbol: string;
  direction: Direction;
  live: boolean;
  entry_price?: number;
  tactical_stop_30m?: number;
  structural_stop_daily?: number;
  target_price?: number;
  account_equity?: number;
  account_currency?: string;
  fx_to_account?: number;
  risk_budget_pct?: number;
  slot_risk_share?: number;
  horizon_days?: number;
  has_l1_l2?: boolean;
  pending_verifications?: number;
  thesis_falsified?: boolean;
  pair_trade?: boolean;
  pair_leg_verified?: boolean;
  pair_leg_symbol?: string;
  hedge_only?: boolean;
  min_rr?: number;
  alignment_status?: string;
}

export interface FourQuadrantGate {
  decision: string;
  actionable: boolean;
  direction?: string;
  asset_class?: string;
  reason?: string;
  reasons?: string[];
  checks?: Record<string, unknown>;
}

export interface ExpressionAndCarry {
  asset_class?: string;
  direction?: string;
  vetoed?: boolean;
  curve_data_status?: string;
  curve_data_warning?: string | null;
  curve_structure?: string | null;
  divergence_regime?: string | null;
  annualized_carry_pct?: number | null;
  near_annualized_carry_pct?: number | null;
  curve_annualized_carry_pct?: number | null;
  holding_horizon_days?: number;
  horizon_carry_pct?: number | null;
  near_horizon_carry_pct?: number | null;
  curve_horizon_carry_pct?: number | null;
  carry_adjusted_rr_ratio?: number | null;
  curve_carry_adjusted_rr_ratio?: number | null;
  near_carry_stress_rr_ratio?: number | null;
  expression_recommendation?: string | null;
  contract_guidance?: string | null;
  monitoring_points?: string[];
  warnings?: string[];
  front_contract?: string | null;
  far_contract?: string | null;
}

export interface Tranche {
  tranche_index: number;
  weight: number;
  lots?: number;
  quantity: number;
  quantity_str?: string;
  risk_amount: number;
  notional_account?: number;
  rule: string;
}

export interface PositionPlan {
  symbol: string;
  asset_class?: string;
  direction?: string;
  account_currency?: string;
  quote_currency?: string;
  fx_to_account?: number | null;
  stop_source?: string | null;
  stop_timeframe?: string | null;
  equity_source?: string | null;
  contract_spec?: Dict;
  entry_price?: number | null;
  tactical_stop_30m?: number | null;
  structural_stop_daily?: number | null;
  target_price?: number | null;
  stop_distance_30m_pct?: number | null;
  account_equity?: number;
  risk_budget_pct?: number;
  risk_budget_amount?: number;
  total_lots?: number;
  total_quantity?: number;
  total_quantity_str?: string;
  sizing_binding_constraint?: string;
  within_capital_limits?: boolean;
  oversized_single_lot?: boolean;
  actual_risk_amount?: number;
  actual_risk_pct?: number;
  notional_value_account?: number;
  estimated_margin_account?: number;
  margin_pct_of_equity?: number;
  rr_ratio?: number | null;
  carry_adjusted_rr_ratio?: number | null;
  expected_move_pct?: number | null;
  daily_structural_risk_amount?: number | null;
  daily_vs_30m_risk_ratio?: number | null;
  noise_risk_saved_amount?: number | null;
  tranches?: Tranche[];
  warnings?: string[];
}

export interface TradePlanResponse {
  four_quadrant_gate: FourQuadrantGate;
  expression_and_carry: ExpressionAndCarry;
  position_plan: PositionPlan;
  equity_source?: string | null;
  stop_source?: string | null;
  stop_timeframe?: string | null;
  live_warnings?: string[];
}

/* ---------------- Research archive ---------------- */
export type ArchiveStatus = "holding" | "watchlist" | "closed" | string;

export interface ArchiveListItem {
  market: string;
  symbol: string;
  name?: string;
  status: ArchiveStatus;
  direction_bias?: string | null;
  revision: number;
  updated_at_utc?: string | null;
  archive_dir?: string;
}

export interface TriggerVerification {
  method: "ai_web" | "manual" | string;
  status: "clear" | "fired" | "uncertain" | string;
  observed?: number | string | boolean | null;
  observed_text?: string;
  summary?: string;
  evidence_tier?: "L1" | "L2" | "L3" | "L4" | string;
  source_title?: string;
  source_url?: string;
  verified_at_utc?: string;
  ttl_days?: number;
}

export interface ArchiveTrigger {
  id?: string;
  category?: string;
  kind?: string;
  evaluation_mode?: "auto" | "ai_web" | "manual" | string;
  verification_ttl_days?: number;
  verification?: TriggerVerification | null;
  last_checked_at?: string | null;
  last_value?: unknown;
  metric?: string;
  operator?: string;
  threshold?: number | string | null;
  action?: string;
  status?: string;
  description?: string;
  [k: string]: unknown;
}

export interface ArchiveState {
  schema_version?: number;
  revision: number;
  instrument?: {
    market?: string;
    symbol?: string;
    name?: string;
    asset_class?: string;
    subtype?: string;
  };
  status: ArchiveStatus;
  direction_bias?: string | null;
  layer1_drivers?: { primary?: string; secondary?: string };
  thesis_summary?: string;
  chanlun_anchors?: Dict;
  valuation_or_balance?: Dict;
  triggers?: ArchiveTrigger[];
  pending_verifications?: unknown[];
  events?: { date: string; title: string; [k: string]: unknown }[];
  updated_at_utc?: string;
  [k: string]: unknown;
}

export interface EvidenceItem {
  id?: string;
  metric?: string;
  value?: unknown;
  unit?: string;
  period?: string;
  basis?: string;
  kind?: string;
  level?: "L1" | "L2" | "L3" | "L4" | string;
  published_at?: string;
  fetched_at?: string;
  source_url?: string;
  source_locator?: string;
  note?: string;
  summary?: string;
  [k: string]: unknown;
}

export interface TriggerServerEvaluation {
  is_auto?: boolean;
  evaluated?: boolean;
  fired?: boolean;
  verification_stale?: boolean;
  verification_status?: string | null;
  error?: string | null;
}

export interface ArchiveDetail {
  market: string;
  symbol: string;
  asset_class?: string;
  state: ArchiveState;
  evidence_count: number;
  recent_evidence: EvidenceItem[];
  thesis_md: string;
  trigger_evaluations?: Record<string, TriggerServerEvaluation>;
}

export interface ArchiveInitBody {
  symbol: string;
  market?: string;
  name?: string;
  subtype?: string;
  thesis_summary?: string;
  primary_driver?: string;
  secondary_driver?: string;
}

export interface ResearchDataResponse {
  symbol: string;
  asset_class: string;
  fetched_at_utc?: string;
  fundamentals_and_supply?: Dict;
  macro_dual_core?: Dict;
  sources_status?: Record<string, unknown>;
  [k: string]: unknown;
}

/* ---------------- Universes & tasks (matches backend/tasks.py) ---------------- */
export interface UniversePool {
  description?: string;
  symbols: string[];
}

export interface ScanRow {
  market: string;
  symbol: string;
  asset_class?: string | null;
  as_of_bar?: string | null;
  bar_is_closed?: boolean | null;
  close?: number | null;
  "ret_5d_%"?: number | null;
  "ret_20d_%"?: number | null;
  "ret_60d_%"?: number | null;
  "atr_%"?: number | null;
  signal_tier?: string | null;
  signal_direction?: string | null;
  signal_type?: string | null;
  signal_confirmed?: boolean | null;
  daily_structure?: string | null;
  daily_zs_level?: string | null;
  daily_zs_level_label?: string | null;
  daily_zs_count?: number | null;
  daily_zs?: [number, number] | null;
  pos_vs_zs?: string | null;
  daily_fractal?: string | null;
  daily_momentum?: string | null;
  momentum_reliable?: boolean | null;
  quality_grade?: "good" | "caution" | "degraded" | string | null;
  quality_warnings?: string[];
  quality_notes?: string[];
  cache_status?: string | null;
  action_bias?: string | null;
  signals?: string[];
  error?: string;
}

export type TaskKind =
  | "scan"
  | "research_refresh"
  | "research-refresh"
  | "deep_research"
  | "sentinel"
  | "trigger_verify"
  | "macro_compass"
  | string;
export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | string;

export type DeepResearchMode = "standard" | "adversarial";
export type DeepResearchEngine = "claude" | "codex";

export interface ResearchEngineInfo {
  id: DeepResearchEngine;
  label: string;
  description: string;
  available: boolean;
  binary?: string | null;
}

export interface ResearchEnginesResponse {
  engines: ResearchEngineInfo[];
  default_engine: DeepResearchEngine | null;
  any_available: boolean;
  adversarial_cross_model: boolean;
}

export interface DeepResearchRequest {
  symbol: string;
  market?: string | null;
  mode?: DeepResearchMode;
  engine?: DeepResearchEngine;
  focus_prompt?: string;
  include_macro?: boolean;
}

export interface TaskProgress {
  done: number;
  total: number | null;
  symbol?: string;
}

export interface TaskEventEnvelope {
  seq?: number;
  type?: string;
  ts?: string;
  data?: {
    message?: string;
    stage?: string;
    done?: number;
    total?: number | null;
    symbol?: string;
    market?: string;
    index?: number;
    row?: ScanRow;
    result?: unknown;
    [k: string]: unknown;
  };
  /* Flat fallbacks */
  message?: string;
  progress?: number | TaskProgress;
  row?: ScanRow;
  result?: unknown;
  status?: TaskStatus;
}

export interface TaskRecord {
  id: string;
  task_id?: string;
  kind: TaskKind;
  status: TaskStatus;
  params?: Dict;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  progress?: TaskProgress | number | null;
  event_count?: number;
  error?: string | null;
  result?: unknown;
  events?: TaskEventEnvelope[];
}

export interface WarehouseSummary {
  db_path: string;
  db_size_bytes: number;
  series_count: number;
  total_ohlcv_bars: number;
  scan_runs_count: number;
  sentinel_runs_count: number;
  research_snapshots_count: number;
  quality_counts: Record<string, number>;
  flagged_series: {
    series_id: string;
    symbol: string;
    asset_class: string;
    interval: string;
    quality_grade: string;
    as_of_bar?: string | null;
    last_synced_at_utc?: string;
    warnings?: string[];
    notes?: string[];
  }[];
}

export interface CompassScanContext {
  run_id?: string;
  direction_id?: string;
  direction_title?: string;
  direction: "long" | "short" | "pair";
  expected_directions: Record<string, "long" | "short">;
}

export interface PersistedScanRun {
  run_id: string;
  task_id?: string | null;
  created_at_utc: string;
  finished_at_utc: string;
  status: string;
  universes: string[];
  symbols: string[];
  period: string;
  total_count: number;
  error_count: number;
  degraded_count: number;
  actionable_count: number;
  compass_context?: CompassScanContext | null;
  rows?: ScanRow[];
}

/* ---------------- Layer 1 Top-Down Strategic Compass ---------------- */
export interface MacroHeadlineMetric {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  delta?: number | null;
  delta_unit?: string;
  as_of?: string | null;
  source?: string;
}

export interface MacroRegimeTag {
  label: string;
  tone: "good" | "warning" | "critical" | "neutral" | string;
}

export interface CrossAssetAnchorItem {
  symbol: string;
  name: string;
  group: string;
  group_label: string;
  status: "ok" | "unavailable" | string;
  last: number | null;
  ret_5d_pct: number | null;
  ret_20d_pct: number | null;
  ret_60d_pct: number | null;
  as_of?: string | null;
}

export interface CompassMacroOverview {
  fetched_at_utc: string;
  cache_hit?: boolean;
  sources_status: Record<string, string>;
  stale_sources?: Record<string, { last_good_at_utc?: string | null; error?: string }>;
  regime_tags: MacroRegimeTag[];
  headline_metrics: MacroHeadlineMetric[];
  anchors?: {
    status: string;
    ok_count?: number;
    total?: number;
    items: CrossAssetAnchorItem[];
  };
}

export interface CompassDriverItem {
  dimension: string;
  summary: string;
}

export interface CompassLeaderItem {
  symbol: string;
  asset_class?: string;
  name: string;
  leg: "long" | "short";
  role: "sole_leader" | "co_leader" | string;
  reason: string;
}

export interface CompassEvidenceItem {
  fact: string;
  level: "L1" | "L2" | "L3" | "L4" | string;
  date?: string;
  source?: string;
  url?: string;
  verification_status?: string;
}

export interface CompassDirectionCard {
  id: string;
  rank: number;
  title: string;
  direction: "long" | "short" | "pair";
  horizon: string;
  conviction: "high" | "medium" | string;
  asset_scope: string;
  primary_driver: CompassDriverItem;
  secondary_driver: CompassDriverItem;
  thesis: string;
  value_chain_focus?: string;
  long_leg_sector?: string;
  short_leg_sector?: string;
  sector_keywords?: string[];
  reference_anchors?: string[];
  key_catalyst: string;
  invalidation: string;
  recommended_universes: string[];
  is_new_theme: boolean;
  suggested_pool_name: string;
  suggested_pool_description: string;
  portfolio_overlap?: string[];
  leaders: CompassLeaderItem[];
  evidence: CompassEvidenceItem[];
}

export interface CompassResultPayload {
  regime_title: string;
  regime_summary: string;
  macro_signals?: {
    us_macro?: string;
    china_macro?: string;
    geopolitics_supply?: string;
    tech_industry?: string;
  };
  avoid_sectors?: { sector: string; reason: string }[];
  directions: CompassDirectionCard[];
  risk_warnings?: string[];
  generated_at_utc?: string;
  engine?: string;
  effort?: string;
  focus_prompt?: string;
}

export interface CompassRunSummary {
  run_id: string;
  task_id?: string | null;
  created_at_utc: string;
  finished_at_utc: string;
  status: string;
  engine: string;
  effort?: string;
  focus_prompt?: string;
  regime_title: string;
  directions_count: number;
}

export interface CompassRunRecord extends CompassRunSummary {
  result: CompassResultPayload;
}

export interface CompassLatestResponse {
  latest: CompassRunRecord | null;
  history: CompassRunSummary[];
  active_task: TaskRecord | null;
}

export interface MacroCompassRequest {
  engine?: DeepResearchEngine | "";
  effort?: "low" | "medium" | "high";
  focus_prompt?: string;
  refresh_macro?: boolean;
}

