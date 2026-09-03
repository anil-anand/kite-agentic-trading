import { ipcMain } from 'electron';
import * as channels from '../shared/ipc-channels';
import { pythonBridge } from './python-bridge';
import { authManager } from './auth-manager';
import { AppSettings } from '../shared/types';

export function setupIpcHandlers() {
  
  // ─── Authentication ───────────────────────────────────────────────
  
  ipcMain.handle(channels.AUTH_LOGIN, async (_, creds: { apiKey: string, apiSecret: string }) => {
    console.log('[IPC] Received AUTH_LOGIN request:', creds ? 'Has Creds' : 'No Creds');
    try {
      const result = await authManager.startLogin(creds.apiKey, creds.apiSecret);
      console.log('[IPC] AUTH_LOGIN success:', result.isLoggedIn);
      return result;
    } catch (error: any) {
      return { isLoggedIn: false, credentials: null, loginUrl: null, error: error.message };
    }
  });

  ipcMain.handle(channels.AUTH_LOGOUT, async () => {
    try {
      await authManager.logout();
      return true;
    } catch (error) {
      return false;
    }
  });

  ipcMain.handle(channels.AUTH_STATUS, async () => {
    return await authManager.checkSession();
  });

  // ─── Orders ───────────────────────────────────────────────────────
  
  ipcMain.handle(channels.ORDERS_PLACE, async (_, orderParams: any) => {
    return await pythonBridge.call('place_order', orderParams);
  });
  
  ipcMain.handle(channels.ORDERS_MODIFY, async (_, orderParams: any) => {
    return await pythonBridge.call('modify_order', orderParams);
  });
  
  ipcMain.handle(channels.ORDERS_CANCEL, async (_, orderId: string, variety: string) => {
    return await pythonBridge.call('cancel_order', { orderId, variety });
  });
  
  ipcMain.handle(channels.ORDERS_GET_ALL, async () => {
    return await pythonBridge.call('get_orders');
  });

  ipcMain.handle(channels.ORDERS_GET_TRADES, async () => {
    return await pythonBridge.call('get_trades');
  });

  // ─── Portfolio ────────────────────────────────────────────────────
  
  ipcMain.handle(channels.PORTFOLIO_POSITIONS, async () => {
    return await pythonBridge.call('get_positions');
  });
  
  ipcMain.handle(channels.PORTFOLIO_HOLDINGS, async () => {
    return await pythonBridge.call('get_holdings');
  });
  
  ipcMain.handle(channels.PORTFOLIO_MARGINS, async () => {
    return await pythonBridge.call('get_margins');
  });

  // ─── Market Data ──────────────────────────────────────────────────
  
  ipcMain.handle(channels.MARKET_QUOTE, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_quote', { instruments });
  });
  
  ipcMain.handle(channels.MARKET_LTP, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_ltp', { instruments });
  });
  
  ipcMain.handle(channels.MARKET_OHLC, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_ohlc', { instruments });
  });
  
  ipcMain.handle(channels.MARKET_HISTORICAL, async (_, params: any) => {
    return await pythonBridge.call('get_historical', params);
  });
  
  ipcMain.handle(channels.MARKET_INSTRUMENTS, async (_, exchange: string) => {
    return await pythonBridge.call('get_instruments', { exchange });
  });
  
  ipcMain.handle(channels.MARKET_SEARCH, async (_, query: string) => {
    return await pythonBridge.call('search_instruments', { query });
  });

  // ─── Ticker ───────────────────────────────────────────────────────
  
  ipcMain.handle(channels.TICKER_SUBSCRIBE, async (_, tokens: number[]) => {
    return await pythonBridge.call('ticker_subscribe', { tokens });
  });
  
  ipcMain.handle(channels.TICKER_UNSUBSCRIBE, async (_, tokens: number[]) => {
    return await pythonBridge.call('ticker_unsubscribe', { tokens });
  });
  
  ipcMain.handle(channels.TICKER_STATUS, async () => {
    return await pythonBridge.call('ticker_status');
  });

  // ─── Trading Agent ────────────────────────────────────────────────
  
  ipcMain.handle(channels.AGENT_START, async (_, params) => {
    return await pythonBridge.call('start_agent', params);
  });
  
  ipcMain.handle(channels.AGENT_STOP, async () => {
    return await pythonBridge.call('stop_agent');
  });
  
  ipcMain.handle(channels.AGENT_STATUS, async () => {
    return await pythonBridge.call('agent_status');
  });
  
  ipcMain.handle(channels.AGENT_EXECUTE_SIGNAL, async (_, signal: any) => {
    return await pythonBridge.call('execute_signal', { signal });
  });
  
  ipcMain.handle(channels.AGENT_DISMISS_SIGNAL, async (_, signalId: string) => {
    return await pythonBridge.call('agent_dismiss_signal', { signalId });
  });
  
  ipcMain.handle(channels.AGENT_SCAN_NOW, async () => {
    return await pythonBridge.call('agent_scan_now');
  });

  // ─── Activity Log ─────────────────────────────────────────────────
  
  ipcMain.handle(channels.LOG_GET_ALL, async () => {
    return await pythonBridge.call('log_get_all');
  });
  
  ipcMain.handle(channels.LOG_CLEAR, async () => {
    return await pythonBridge.call('log_clear');
  });

  // ─── Settings ─────────────────────────────────────────────────────
  
  ipcMain.handle(channels.SETTINGS_GET, async () => {
    return await pythonBridge.call('get_settings');
  });
  
  ipcMain.handle(channels.SETTINGS_SAVE, async (_, settings: AppSettings) => {
    return await pythonBridge.call('save_settings', settings as unknown as Record<string, unknown>);
  });
  
  ipcMain.handle(channels.SETTINGS_SAVE_LLM_KEY, async (_, llmApiKey: string) => {
    return await pythonBridge.call('save_llm_api_key', { llmApiKey });
  });

  ipcMain.handle(channels.SETTINGS_DISCOVER_MODELS, async (_, params: any) => {
    return await pythonBridge.call('discover_models', params);
  });
  
  ipcMain.handle(channels.SETTINGS_RESET, async () => {
    return await pythonBridge.call('settings_reset');
  });

  // ─── Watchlist ────────────────────────────────────────────────────
  
  ipcMain.handle(channels.WATCHLIST_GET, async () => {
    return await pythonBridge.call('watchlist_get');
  });
  
  ipcMain.handle(channels.WATCHLIST_ADD, async (_, symbol: string) => {
    return await pythonBridge.call('watchlist_add', { symbol });
  });
  
  ipcMain.handle(channels.WATCHLIST_REMOVE, async (_, symbol: string) => {
    return await pythonBridge.call('watchlist_remove', { symbol });
  });

  // ─── Dashboard ────────────────────────────────────────────────────
  
  ipcMain.handle(channels.DASHBOARD_SUMMARY, async () => {
    return await pythonBridge.call('dashboard_summary');
  });

  // ─── Journal & Analytics ──────────────────────────────────────────

  ipcMain.handle(channels.JOURNAL_GET_TRADES, async () => {
    return await pythonBridge.call('journal_get_trades');
  });

  ipcMain.handle(channels.JOURNAL_GET_EVENTS, async (_, trade_id: string) => {
    return await pythonBridge.call('journal_get_events', { trade_id });
  });

  ipcMain.handle(channels.ANALYTICS_STRATEGY_EXPECTANCY, async () => {
    return await pythonBridge.call('analytics_strategy_expectancy');
  });

  ipcMain.handle(channels.ANALYTICS_CONFLUENCE_VALIDATION, async () => {
    return await pythonBridge.call('analytics_confluence_validation');
  });

  ipcMain.handle(channels.ANALYTICS_CONFIDENCE_CALIBRATION, async () => {
    return await pythonBridge.call('analytics_confidence_calibration');
  });

  ipcMain.handle(channels.ANALYTICS_EXIT_REASON, async () => {
    return await pythonBridge.call('analytics_exit_reason_effectiveness');
  });

  ipcMain.handle(channels.ANALYTICS_TRADE_REPLAY, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_trade_replay', { trade_id });
  });

  ipcMain.handle(channels.ANALYTICS_WHAT_IF, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_what_if', { trade_id });
  });

  ipcMain.handle(channels.ANALYTICS_LLM_POST_MORTEM, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_llm_post_mortem', { trade_id });
  });
}
