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
export const AGENT_STRATEGY_SELECTION_GET = 'agent:strategy-selection-get';
export const AGENT_STRATEGY_REEVALUATE = 'agent:strategy-reevaluate';
export const AGENT_STRATEGY_OVERRIDE = 'agent:strategy-override';
export const AGENT_STRATEGY_SELECTION = 'agent:strategy-selection'; // Main → Renderer event

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
export const ANALYTICS_CONFIDENCE_CALIBRATION = 'analytics:confidence-calibration';
export const ANALYTICS_EXIT_REASON = 'analytics:exit-reason';
export const ANALYTICS_TRADE_REPLAY = 'analytics:trade-replay';
export const ANALYTICS_WHAT_IF = 'analytics:what-if';
export const ANALYTICS_LLM_POST_MORTEM = 'analytics:llm-post-mortem';
