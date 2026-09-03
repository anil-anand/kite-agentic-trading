// ─── Authentication ───────────────────────────────────────────────

export interface KiteCredentials {
  apiKey: string;
  apiSecret: string;
  accessToken?: string;
  userId?: string;
  userName?: string;
  llmApiKey?: string;
}

export interface AuthState {
  isLoggedIn: boolean;
  credentials: KiteCredentials | null;
  loginUrl: string | null;
  error: string | null;
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
  | 'VALIDATION PENDING';

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
  price: number;
  averagePrice: number;
  triggerPrice: number;
  product: ProductType;
  orderType: OrderType;
  variety: string;
  status: OrderStatus;
  statusMessage: string;
  tag: string;
  orderTimestamp: string;
  exchangeTimestamp: string;
}

// ─── Positions & Holdings ─────────────────────────────────────────

export interface Position {
  tradingsymbol: string;
  exchange: string;
  instrumentToken: number;
  product: ProductType;
  quantity: number;
  overnightQuantity: number;
  averagePrice: number;
  lastPrice: number;
  closePrice: number;
  pnl: number;
  unrealised: number;
  realised: number;
  buyQuantity: number;
  sellQuantity: number;
  buyPrice: number;
  sellPrice: number;
  multiplier: number;
  value: number;
  dayBuyQuantity: number;
  daySellQuantity: number;
}

export interface Holding {
  tradingsymbol: string;
  exchange: string;
  instrumentToken: number;
  quantity: number;
  averagePrice: number;
  lastPrice: number;
  pnl: number;
  closePrice: number;
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

export type StrategyName = 'ema_crossover' | 'rsi_reversal' | 'vwap_bounce' | 'supertrend';
export type SignalDirection = 'BUY' | 'SELL';
export type AgentMode = 'auto' | 'confirm' | 'paper';

export interface Signal {
  id: string;
  tradingsymbol: string;
  exchange: string;
  strategy: StrategyName;
  direction: SignalDirection;
  confidence: number; // 0-100
  entryPrice: number;
  stopLoss: number;
  target: number;
  riskReward: number;
  reasoning: string;
  timestamp: string;
  indicators: Record<string, number>;
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
  status: 'idle' | 'scanning' | 'placing_order' | 'monitoring' | 'stopped' | 'error';
  statusMessage: string;
}

// ─── Risk Management ──────────────────────────────────────────────

export interface RiskConfig {
  maxCapitalPerTrade: number;
  maxDailyLoss: number;
  maxOpenPositions: number;
  noNewTradesAfter: string; // "14:30" format
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
  totalPnl: number;
  realisedPnl: number;
  unrealisedPnl: number;
  tradesToday: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  maxDrawdown: number;
  openPositionsCount: number;
  availableMargin: number;
  usedMargin: number;
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
  confidence: number | null;
  entry_price: number;
  quantity: number;
  stop_loss: number;
  target: number;
  entry_time: string;
  exit_price: number | null;
  exit_time: string | null;
  exit_reason: string | null;
  pnl: number | null;
  status: 'OPEN' | 'CLOSED';
  confluence_snapshot: string | null;
  indicator_snapshot: string | null;
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

export interface ConfidenceCalibration {
  confidence_bucket: string;
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

export interface LLMPostMortem {
  analysis?: string;
  error?: string;
}
