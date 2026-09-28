/**
 * IPC Channel names for communication between Electron main and renderer processes.
 * Using constants prevents typos and enables autocomplete.
 */

// ─── Authentication ───────────────────────────────────────────────
export const AUTH_LOGIN = 'auth:login';
export const AUTH_LOGOUT = 'auth:logout';
export const AUTH_STATUS = 'auth:status';
export const AUTH_SAVE_CREDENTIALS = 'auth:save-credentials';
export const AUTH_GET_CREDENTIALS = 'auth:get-credentials';

// ─── Orders ───────────────────────────────────────────────────────
export const ORDERS_PLACE = 'orders:place';
export const ORDERS_MODIFY = 'orders:modify';
export const ORDERS_CANCEL = 'orders:cancel';
export const ORDERS_GET_ALL = 'orders:get-all';
export const ORDERS_GET_TRADES = 'orders:get-trades';

// ─── Portfolio ────────────────────────────────────────────────────
export const PORTFOLIO_POSITIONS = 'portfolio:positions';
export const PORTFOLIO_HOLDINGS = 'portfolio:holdings';
export const PORTFOLIO_MARGINS = 'portfolio:margins';

// ─── Market Data ──────────────────────────────────────────────────
export const MARKET_QUOTE = 'market:quote';
export const MARKET_LTP = 'market:ltp';
export const MARKET_OHLC = 'market:ohlc';
export const MARKET_HISTORICAL = 'market:historical';
export const MARKET_INSTRUMENTS = 'market:instruments';
export const MARKET_SEARCH = 'market:search';

// ─── WebSocket / Ticker ───────────────────────────────────────────
export const TICKER_SUBSCRIBE = 'ticker:subscribe';
export const TICKER_UNSUBSCRIBE = 'ticker:unsubscribe';
export const TICKER_TICK = 'ticker:tick'; // Main → Renderer event
export const TICKER_STATUS = 'ticker:status';
export const TICKER_ORDER_UPDATE = 'ticker:order-update'; // Main → Renderer event

// ─── Trading Agent ────────────────────────────────────────────────
export const AGENT_START = 'agent:start';
export const AGENT_STOP = 'agent:stop';
export const AGENT_STATUS = 'agent:status';
export const AGENT_STATE_UPDATE = 'agent:state-update'; // Main → Renderer event
export const AGENT_SIGNAL = 'agent:signal'; // Main → Renderer event
export const AGENT_EXECUTE_SIGNAL = 'agent:execute-signal';
export const AGENT_DISMISS_SIGNAL = 'agent:dismiss-signal';
export const AGENT_SCAN_NOW = 'agent:scan-now';
export const AGENT_SET_MODE = 'agent:set-mode';
export const AGENT_CLOSE_POSITION = 'agent:close-position';
export const AGENT_EMERGENCY_FLATTEN = 'agent:emergency-flatten';

// ─── Activity Log ─────────────────────────────────────────────────
export const LOG_ENTRY = 'log:entry'; // Main → Renderer event
export const LOG_GET_ALL = 'log:get-all';
export const LOG_CLEAR = 'log:clear';

// ─── Settings ─────────────────────────────────────────────────────
export const SETTINGS_GET = 'settings:get';
export const SETTINGS_SAVE = 'settings:save';
export const SETTINGS_SAVE_LLM_KEY = 'settings:save-llm-key';
export const SETTINGS_DISCOVER_MODELS = 'settings:discover-models';
export const SETTINGS_RESET = 'settings:reset';

// ─── Watchlist ────────────────────────────────────────────────────
export const WATCHLIST_GET = 'watchlist:get';
export const WATCHLIST_ADD = 'watchlist:add';
export const WATCHLIST_REMOVE = 'watchlist:remove';
export const WATCHLIST_UPDATE = 'watchlist:update'; // Main → Renderer event

// ─── App Lifecycle ────────────────────────────────────────────────
export const APP_READY = 'app:ready';
export const APP_ERROR = 'app:error'; // Main → Renderer event
export const APP_PYTHON_STATUS = 'app:python-status';

// ─── Dashboard ────────────────────────────────────────────────────
export const DASHBOARD_SUMMARY = 'dashboard:summary';

// ─── Journal & Analytics ──────────────────────────────────────────
export const JOURNAL_GET_TRADES = 'journal:get-trades';
export const JOURNAL_GET_EVENTS = 'journal:get-events';
export const ANALYTICS_STRATEGY_EXPECTANCY = 'analytics:strategy-expectancy';
export const ANALYTICS_CONFLUENCE_VALIDATION = 'analytics:confluence-validation';
export const ANALYTICS_SIGNAL_SCORE_CALIBRATION = 'analytics:signal-score-calibration';
export const ANALYTICS_EXIT_REASON = 'analytics:exit-reason';
export const ANALYTICS_EXIT_QUALITY_REPORT = 'analytics:exit-quality-report';
export const ANALYTICS_EXIT_QUALITY_TRADE = 'analytics:exit-quality-trade';
export const ANALYTICS_EXIT_MANAGEMENT_REPLAY = 'analytics:exit-management-replay';
export const ANALYTICS_ACTIVE_POSITION_EXPLANATIONS = 'analytics:active-position-explanations';
export const ANALYTICS_TRADE_REPLAY = 'analytics:trade-replay';
export const ANALYTICS_WHAT_IF = 'analytics:what-if';
export const ANALYTICS_LLM_POST_MORTEM = 'analytics:llm-post-mortem';

// ─── Backtesting ──────────────────────────────────────────────────
export const BACKTEST_RUN = 'backtest:run';

// Explicit capability lists. Adding a channel requires choosing its direction.
export const INVOKE_CHANNELS = [
  AUTH_LOGIN, AUTH_LOGOUT, AUTH_STATUS,
  ORDERS_PLACE, ORDERS_MODIFY, ORDERS_CANCEL, ORDERS_GET_ALL, ORDERS_GET_TRADES,
  PORTFOLIO_POSITIONS, PORTFOLIO_HOLDINGS, PORTFOLIO_MARGINS,
  MARKET_QUOTE, MARKET_LTP, MARKET_OHLC, MARKET_HISTORICAL, MARKET_INSTRUMENTS, MARKET_SEARCH,
  TICKER_SUBSCRIBE, TICKER_UNSUBSCRIBE, TICKER_STATUS,
  AGENT_START, AGENT_STOP, AGENT_STATUS, AGENT_EXECUTE_SIGNAL, AGENT_DISMISS_SIGNAL,
  AGENT_SCAN_NOW, AGENT_SET_MODE, AGENT_CLOSE_POSITION, AGENT_EMERGENCY_FLATTEN,
  LOG_GET_ALL, LOG_CLEAR, SETTINGS_GET, SETTINGS_SAVE, SETTINGS_SAVE_LLM_KEY,
  SETTINGS_DISCOVER_MODELS, SETTINGS_RESET, WATCHLIST_GET, WATCHLIST_ADD, WATCHLIST_REMOVE,
  DASHBOARD_SUMMARY, JOURNAL_GET_TRADES, JOURNAL_GET_EVENTS,
  ANALYTICS_STRATEGY_EXPECTANCY, ANALYTICS_CONFLUENCE_VALIDATION,
  ANALYTICS_SIGNAL_SCORE_CALIBRATION, ANALYTICS_EXIT_REASON, ANALYTICS_EXIT_QUALITY_REPORT,
  ANALYTICS_EXIT_QUALITY_TRADE, ANALYTICS_EXIT_MANAGEMENT_REPLAY,
  ANALYTICS_ACTIVE_POSITION_EXPLANATIONS, ANALYTICS_TRADE_REPLAY, ANALYTICS_WHAT_IF,
  ANALYTICS_LLM_POST_MORTEM, BACKTEST_RUN,
] as const;
export const EVENT_CHANNELS = [
  TICKER_TICK, TICKER_STATUS, TICKER_ORDER_UPDATE, AGENT_STATE_UPDATE, AGENT_SIGNAL,
  LOG_ENTRY, WATCHLIST_UPDATE, APP_READY, APP_ERROR, APP_PYTHON_STATUS,
] as const;
export type InvokeChannel = typeof INVOKE_CHANNELS[number];
export type EventChannel = typeof EVENT_CHANNELS[number];
