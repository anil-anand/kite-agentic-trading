// ─── Authentication ───────────────────────────────────────────────

export interface KiteCredentials {
  apiKey: string;
  apiSecret: string;
  redirectUrl?: string;
  accessToken?: string;
  userId?: string;
  userName?: string;
}

export type OpenCodePlan = 'zen' | 'go';

export interface LLMSettings {
  provider: 'OpenAI' | 'Anthropic' | 'Gemini' | 'OpenRouter' | 'Ollama' | 'OpenCode';
  baseUrl: string;
  model: string;
  openCodePlan?: OpenCodePlan;
  apiKey: string;
  apiKeyConfigured?: boolean;
  temperature?: number;
  maxTokens?: number;
}

export interface AuthState {
  isLoggedIn: boolean;
  credentials: { userId?: string; userName?: string } | null;
  loginUrl: string | null;
  error: string | null;
}

export interface BackendStatus {
  running: boolean;
  ready: boolean;
  error: string | null;
  generation?: string;
  tradingReady?: boolean;
  sessionValid?: boolean;
  supervision?: Partial<AgentState>;
}

// ─── Market Data ──────────────────────────────────────────────────

export interface Tick {
  instrumentToken: number;
  tradingsymbol: string;
  lastPrice: number;
  change: number;
  changePercent: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  close: number;
  buyQuantity: number;
  sellQuantity: number;
  ohlc: OHLC;
  timestamp: string;
}

export interface OHLC {
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface Candle {
  time: number; // Unix timestamp in seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Instrument {
  instrumentToken: number;
  exchangeToken: string;
  tradingsymbol: string;
  name: string;
  lastPrice: number;
  tickSize: number;
  lotSize: number;
  instrumentType: string;
  segment: string;
  exchange: string;
}

// ─── Orders ───────────────────────────────────────────────────────

export type OrderType = 'MARKET' | 'LIMIT' | 'SL' | 'SL-M';
export type TransactionType = 'BUY' | 'SELL';
export type ProductType = 'MIS' | 'CNC' | 'NRML';
export type OrderVariety = 'regular' | 'amo' | 'co' | 'iceberg';
export type OrderValidity = 'DAY' | 'IOC' | 'TTL';

export type OrderStatus =
  | 'OPEN'
  | 'COMPLETE'
  | 'CANCELLED'
  | 'REJECTED'
  | 'TRIGGER PENDING'
  | 'MODIFY PENDING'
  | 'CANCEL PENDING'
  | 'PUT ORDER REQ RECEIVED'
  | 'VALIDATION PENDING'
  | 'OPEN PENDING'
  | 'MODIFY VALIDATION PENDING'
  | 'AMO REQ RECEIVED'
  | (string & {});

export interface OrderRequest {
  tradingsymbol: string;
  exchange: string;
  transactionType: TransactionType;
  quantity: number;
  product: ProductType;
  orderType: OrderType;
  price?: number;
  triggerPrice?: number;
  validity?: OrderValidity;
  tag?: string;
  variety?: OrderVariety;
}

export interface Order {
  orderId: string;
  isAppOrder?: boolean;
  tradingsymbol: string;
  exchange: string;
  transactionType: TransactionType;
  quantity: number;
  filledQuantity: number;
  pendingQuantity: number;
  price: number | null;
  averagePrice: number | null;
  triggerPrice: number | null;
  product: ProductType;
  orderType: OrderType;
  variety: string;
  status: OrderStatus;
  statusMessage: string | null;
  isWorking?: boolean;
  tag: string | null;
  isArchived?: boolean;
  snapshotQuality?: 'COMPLETE' | 'PARTIAL' | 'STALE' | 'UNAVAILABLE';
  orderTimestamp: string | null;
  exchangeTimestamp: string | null;
}

export interface OrderSnapshot {
  orders: Order[];
  snapshotQuality: 'COMPLETE' | 'PARTIAL' | 'STALE' | 'UNAVAILABLE';
  snapshotId: string;
  fetchedAt: string;
  errors?: string[];
}

// ─── Positions & Holdings ─────────────────────────────────────────

export interface Position {
  positionKey?: string;
  namespace?: string;
  accountId?: string;
  tradingsymbol: string;
  exchange: string;
  instrumentToken: number | string;
  product: ProductType;
  quantity: number;
  overnightQuantity: number;
  averagePrice: number | null;
  lastPrice: number | null;
  closePrice: number | null;
  pnl: number | null;
  unrealised: number | null;
  realised: number | null;
  buyQuantity: number;
  sellQuantity: number;
  buyPrice: number | null;
  sellPrice: number | null;
  multiplier: number | null;
  value: number | null;
  dayBuyQuantity: number;
  daySellQuantity: number;
  markTime?: string | null;
}

export interface Holding {
  tradingsymbol: string;
  exchange: string;
  instrumentToken: number | string;
  quantity: number;
  averagePrice: number | null;
  lastPrice: number | null;
  pnl: number | null;
  closePrice: number | null;
}

// ─── Margins ──────────────────────────────────────────────────────

export interface Margins {
  enabled: boolean;
  net: number;
  available: {
    cash: number;
    collateral: number;
    intradayPayin: number;
    adhocMargin: number;
    liveBalance: number;
  };
  utilised: {
    debits: number;
    exposure: number;
    m2mRealised: number;
    m2mUnrealised: number;
    optionPremium: number;
    payout: number;
    span: number;
    holdingSales: number;
    turnover: number;
  };
}

// ─── Trading Signals & Strategy ───────────────────────────────────

export type StrategyName =
  // Individual scanner strategies (matches Scanner.strategies in scanner.py)
  | 'ema_crossover'
  | 'rsi_reversal'
  | 'vwap_bounce'
  | 'supertrend'
  | 'macd_cross'
  | 'bollinger_breakout'
  | 'stochastic_reversal'
  | 'adx_momentum'
  | 'psar_trend'
  | 'donchian_breakout'
  | 'cci_reversal'
  | 'williams_r'
  | 'mfi_exhaustion'
  | 'keltner_breakout'
  | 'awesome_oscillator'
  | 'tsi_cross'
  | 'stoc_rsi'
  // Playbook aggregates (matches TrendPullbackPlaybook.get_name() etc.)
  | 'Trend Pullback'
  | 'Breakout'
  | 'Mean Reversion'
  // Family-level aggregated signals
  | 'family_trend'
  | 'family_mean_reversion'
  | 'family_breakout'
  // LLM agent gateway
  | 'llm_agent';
export type SignalDirection = 'BUY' | 'SELL';
export type AgentMode = 'auto' | 'confirm';

export interface Signal {
  id: string;
  tradingsymbol: string;
  exchange: string;
  strategy: StrategyName;
  direction: SignalDirection;
  signal_score: number;
  estimated_probability?: number;
  calibration_sample_size?: number; // count of historical trades used for calibration
  strategy_count?: number;
  entryPrice: number;
  stopLoss: number;
  target: number;
  riskReward: number;
  reasoning: string;
  timestamp: string;
  analysisOnly?: boolean;
  analysisAsOf?: string;
  indicators: Record<string, number>;
}

export interface ScanProgress {
  phase: 'preparing' | 'screening' | 'loading_instruments' | 'scanning' | 'completed' | 'error';
  analysisOnly: boolean;
  startedAt: string;
  completedAt: string | null;
  nextScanAt: string | null;
  universeSize: number | null;
  totalSymbols: number;
  completedSymbols: number;
  evaluatedSymbols: number;
  skippedSymbols: number;
  failedSymbols: number;
  signalsFound: number;
  signalsPublished: number;
  enabledStrategies: string[];
  queuedSymbols: string[];
  workers: {
    id: number;
    symbol: string | null;
    stage: 'idle' | 'waiting_for_symbol' | 'fetching_candles' | 'building_context' | 'evaluating_strategies';
    startedAt: string | null;
    updatedAt: string | null;
  }[];
  results: {
    symbol: string;
    outcome: 'signals' | 'no_match' | 'unchanged' | 'unavailable' | 'unknown_symbol' | 'error';
    detail: string;
    signals: number;
    candleTime: string | null;
  }[];
  message: string | null;
}

export interface AgentState {
  running: boolean;
  mode: AgentMode;
  enabledStrategies: StrategyName[];
  tradesToday: number;
  signalsGenerated: number;
  currentPnl: number;
  maxDrawdownToday: number;
  lastScanTime: string | null;
  status: 'idle' | 'scanning' | 'placing_order' | 'monitoring' | 'supervising' | 'stopped' | 'error';
  statusMessage: string;
  effectiveMode?: AgentMode | 'paused' | 'scan_only';
  scanOnly?: boolean;
  scanProgress?: ScanProgress | null;
  marketSession?: { isOpen: boolean; isTradingDay: boolean; isWeekend: boolean };
  entryBlockReasons?: string[];
  entryPaused?: boolean;
  supervisionActive?: boolean;
  protectionFailureHalt?: boolean;
  reconciliationPending?: boolean;
  lifecycleRecoveryPending?: boolean;
  controlStateInvalid?: boolean;
  supervisionGeneration?: number;
  hardFlattenReason?: string | null;
  hardFlattenPending?: boolean;
  pendingClosePositionKeys?: string[];
}

// ─── Risk Management ──────────────────────────────────────────────

export interface RiskConfig {
  maxCapitalPerTrade: number;
  maxDailyLoss: number;
  maxOpenPositions: number;
  noNewTradesAfter: string; // "14:30" format
  startTradeAfter: string; // "09:45" format
  autoSquareOff: boolean;
  squareOffTime: string; // "15:10" format
  defaultStopLossPercent: number;
  defaultTargetPercent: number;
  trailingStopEnabled: boolean;
  trailingStopPercent: number;
}

// ─── Strategy Configuration ───────────────────────────────────────

export interface StrategyConfig {
  ema_crossover: {
    fastPeriod: number;
    slowPeriod: number;
    volumeConfirmation: boolean;
    volumePeriod: number;
  };
  rsi_reversal: {
    period: number;
    oversold: number;
    overbought: number;
    useVwapConfirmation: boolean;
  };
  vwap_bounce: {
    atrPeriod: number;
    atrMultiplier: number;
    rsiFloor: number;
  };
  supertrend: {
    period: number;
    multiplier: number;
    adxThreshold: number;
    useTrailingStop: boolean;
  };
}

// ─── Activity Log ─────────────────────────────────────────────────

export type LogLevel = 'info' | 'signal' | 'order' | 'warning' | 'error' | 'success';

export interface ActivityLogEntry {
  id: string;
  timestamp: string;
  level: LogLevel;
  message: string;
  details?: Record<string, unknown>;
  strategy?: StrategyName;
  tradingsymbol?: string;
}

// ─── Watchlist ────────────────────────────────────────────────────

export interface WatchlistItem {
  tradingsymbol: string;
  exchange: string;
  instrumentToken: number;
  lastPrice: number;
  change: number;
  changePercent: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  activeSignals: Signal[];
}

// ─── App Settings ─────────────────────────────────────────────────

export interface AppSettings {
  credentials: KiteCredentials;
  llm: LLMSettings;
  risk: RiskConfig;
  strategies: StrategyConfig;
  watchlist: string[]; // ["NSE:RELIANCE", "NSE:INFY", ...]
  agentMode: AgentMode;
  enabledStrategies: StrategyName[];
  scanIntervalSeconds: number;
  candleInterval: string; // "5minute", "15minute", etc.
  theme: 'dark' | 'light';
  notifications: {
    soundEnabled: boolean;
    desktopNotifications: boolean;
    notifyOnSignal: boolean;
    notifyOnOrder: boolean;
    notifyOnStopLoss: boolean;
  };
}

// ─── Python Backend RPC ───────────────────────────────────────────

export interface RPCRequest {
  id: number;
  method: string;
  params: Record<string, unknown>;
}

export interface RPCResponse {
  id: number;
  result?: unknown;
  error?: {
    code: number;
    message: string;
    data?: unknown;
  };
}

export interface RPCEvent {
  event: string;
  data: unknown;
}

// ─── Dashboard Summary ────────────────────────────────────────────

export interface DashboardSummary {
  totalPnl: number | null;
  netPnl: number | null;
  realisedPnl: number | null;
  unrealisedPnl: number | null;
  tradesToday: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  maxDrawdown: number;
  openPositionsCount: number;
  availableMargin: number | null;
  usedMargin: number | null;
  reconciliationStatus?: string;
  killSwitchActive?: boolean;
}

// ─── Journal & Analytics ──────────────────────────────────────────

export interface JournalTrade {
  id: string;
  tradingsymbol: string;
  exchange: string;
  direction: 'BUY' | 'SELL';
  product: string;
  strategy: string;
  signal_id: string | null;
  reasoning: string | null;
  signal_score: number | null;
  estimated_probability?: number | null;
  calibration_sample_size?: number | null;
  entry_price: number;
  quantity: number;
  stop_loss: number;
  target: number;
  entry_time: string;
  exit_price: number | null;
  exit_time: string | null;
  exit_reason: string | null;
  pnl: number | null;
  gross_pnl: number | null;
  net_pnl: number | null;
  brokerage: number | null;
  taxes: number | null;
  exchange_charges: number | null;
  other_fees: number | null;
  slippage: number | null;
  signal_entry_price: number | null;
  status: 'OPEN' | 'CLOSED' | 'RECONCILIATION_PENDING';
  financial_quality?: 'RECONCILED' | 'ESTIMATED' | 'UNAVAILABLE' | null;
  financial_provenance?: string | null;
  accounting_policy_version?: string | null;
  cost_model_version?: string | null;
  rounding_version?: string | null;
  confluence_snapshot: string | null;
  indicator_snapshot: string | null;
  market_regime?: string | null;
  strategy_family?: string | null;
  production_playbook?: string | null;
  raw_evidence?: string | null;
  feature_values?: string | null;
  signal_time?: string | null;
  candle_time?: string | null;
  entry_quote?: number | null;
  exit_quote?: number | null;
  stop_distance?: number | null;
  target_distance?: number | null;
  initial_r?: number | null;
  realized_r?: number | null;
  mae?: number | null;
  mfe?: number | null;
  holding_time_seconds?: number | null;
  screener_score?: number | null;
  strategy_version?: string | null;
}

export interface TradeEvent {
  id: string;
  trade_id: string;
  timestamp: string;
  event_type: string;
  details: string; // JSON string
}

export interface StrategyExpectancy {
  strategy: string;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number | null;
  avg_r_multiple: number;
  avg_hold_time_mins: number;
}

export interface ConfluenceValidation {
  confluence_count: number;
  total_trades: number;
  win_rate_pct: number;
  total_pnl: number;
}

export interface SignalScoreCalibration {
  signal_score_bucket: string;
  total_trades: number;
  actual_win_rate_pct: number;
}

export interface ExitReasonEffectiveness {
  exit_reason: string;
  total_trades: number;
  win_rate_pct: number;
  total_pnl: number;
}

export interface TradeReplayData {
  trade: JournalTrade;
  candles: Candle[];
}

export interface WhatIfAnalysis {
  eod_pnl: number;
  target_hit: boolean;
  target_hit_time: string | null;
  wider_stop_price: number;
  wider_stop_hit: boolean;
  wider_stop_pnl: number;
  actual_pnl: number;
}

export interface ExitQualityMetrics {
  risk_per_share_price: number | null;
  initial_risk_currency: number | null;
  mfe_price: number | null;
  mae_price: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  captured_gross: number | null;
  captured_net: number | null;
  captured_gross_r: number | null;
  captured_net_r: number | null;
  mfe_capture_pct: number | null;
  r_given_back: number | null;
  exposure_peak_r: number | null;
  exposure_aware_r_given_back: number | null;
  holding_time_seconds: number | null;
  decision_to_intent_seconds: number | null;
  intent_to_fill_seconds: number | null;
}

export interface ExitQualityRecord {
  trade_id: string;
  eligible: boolean;
  exclusion_reason: string | null;
  quality: string;
  reason_code: string | null;
  execution_outcome_code: string | null;
  replay_status: string;
  retained_input_available: boolean;
  metrics: ExitQualityMetrics;
  coverage: {
    available: Record<string, boolean>;
    available_count: number;
    total_count: number;
    extrema_quality: string;
  };
  hold_n?: {
    status: string;
    censor_reason?: string;
    message?: string;
  };
}

export interface ExitDecisionReplay {
  trade_id?: string;
  available: boolean;
  reason?: string;
  thesis?: { payload?: Record<string, unknown> | null; payload_corrupt?: boolean } | null;
  position?: { position_key?: string; state?: Record<string, unknown>; state_corrupt?: boolean };
  decisions: Array<{ decision_id: string; payload: Record<string, unknown> | null; payload_corrupt?: boolean }>;
  intents?: Array<Record<string, unknown>>;
  checkpoints?: Array<Record<string, unknown>>;
  attempts?: Array<Record<string, unknown>>;
  fills?: Array<Record<string, unknown>>;
  execution_events?: Array<Record<string, unknown>>;
  exact_replay_complete?: boolean;
  verification?: Array<{ decision_id: string; status: string; action?: string; reason_code?: string; message?: string }>;
  replayability?: { reproduced: number; total: number; uses_current_market_data: boolean };
}

export interface ActivePositionExplanation {
  position_key: string;
  broker_position_key?: string;
  namespace?: string | null;
  account_id?: string | null;
  exchange?: string | null;
  product?: string | null;
  instrument_id?: string | number | null;
  symbol: string | null;
  state_corrupt: boolean;
  policy_mode?: string | null;
  thesis: { strategy?: string | null; playbook?: string | null; setup_variant?: string | null; reasoning?: string | null; expected_behavior?: string | null; original_boundary?: number | null; initial_stop?: number | null; entry_price?: number | null };
  health: string | null;
  development: string | null;
  exposure: string | null;
  protection: { quality?: string | null; confirmed_stop?: number | null; requested_stop?: number | null; protected_quantity?: number | null; confirmed_stop_order_id?: string | null };
  residual_quantity?: number | null;
  pending_intent?: { intent_id?: string; intent_type?: string; status?: string; quantity?: number; reason?: string } | null;
  context?: Record<string, unknown> | null;
  quality?: Record<string, unknown> | null;
  management: { u_r?: number | null; mfe_r?: number | null; mae_r?: number | null; giveback_r?: number | null; session_remaining_minutes?: number | null; last_bar_end?: string | null };
  latest_decision: Record<string, unknown> | null;
}

export interface ExitQualityReport {
  records_total: number;
  records_eligible: number;
  records_excluded: number;
  records: ExitQualityRecord[];
  averages: Record<string, number | null>;
  coverage: Record<string, { available: number; eligible: number }>;
  reason_distribution: Array<{ initiating_reason_code: string; execution_outcome_code: string; count: number }>;
  cohorts: Array<{ dimension: string; value: string; count: number; average_net_r: number | null }>;
  research_label: string;
}

export interface LLMPostMortem {
  analysis?: string;
  error?: string;
}
