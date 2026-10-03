import { ipcMain, BrowserWindow, IpcMainInvokeEvent } from 'electron';
import { validateIpcSender } from './ipc-boundary';
import * as channels from '../shared/ipc-channels';
import { pythonBridge } from './python-bridge';
import { authManager } from './auth-manager';
import { AppSettings } from '../shared/types';
import { secureStorage } from './secure-storage';
import { credentialReplacements } from '../shared/credential-boundary';

export function setupIpcHandlers(getWindow: () => BrowserWindow | null, trustedUrl: string) {
  const registerHandle = (channel: channels.InvokeChannel, listener: (event: IpcMainInvokeEvent, ...args: any[]) => any) => {
    if (!(channels.INVOKE_CHANNELS as readonly string[]).includes(channel)) throw new Error('Unsupported IPC channel');
    ipcMain.handle(channel, (event, ...args) => {
      validateIpcSender(event, getWindow(), trustedUrl);
      return listener(event, ...args);
    });
  };

  registerHandle(channels.APP_GET_PYTHON_STATUS, () => pythonBridge.getStatus());
  
  // ─── Authentication ───────────────────────────────────────────────
  
  registerHandle(channels.AUTH_LOGIN, async (_, creds: { apiKey: string, apiSecret: string, redirectUrl: string }) => {
    console.log('[IPC] Received AUTH_LOGIN request:', creds ? 'Has Creds' : 'No Creds');
    try {
      const result = await authManager.startLogin(creds?.apiKey, creds?.apiSecret, creds?.redirectUrl);
      console.log('[IPC] AUTH_LOGIN success:', result.isLoggedIn);
      return result;
    } catch (error: any) {
      return { isLoggedIn: false, credentials: null, loginUrl: null, error: 'Unable to start login. Check credentials and redirect URL.' };
    }
  });

  registerHandle(channels.AUTH_LOGOUT, async () => {
    await authManager.logout();
    return true;
  });

  registerHandle(channels.AUTH_STATUS, async () => {
    return await authManager.checkSession();
  });

  // ─── Orders ───────────────────────────────────────────────────────
  
  registerHandle(channels.ORDERS_PLACE, async (_, orderParams: any) => {
    return await pythonBridge.call('place_order', orderParams);
  });
  
  registerHandle(channels.ORDERS_MODIFY, async (_, orderParams: any) => {
    return await pythonBridge.call('modify_order', orderParams);
  });
  
  registerHandle(channels.ORDERS_CANCEL, async (_, orderId: string, variety: string) => {
    return await pythonBridge.call('cancel_order', { orderId, variety });
  });
  
  registerHandle(channels.ORDERS_GET_ALL, async () => {
    return await pythonBridge.call('get_orders');
  });

  registerHandle(channels.ORDERS_GET_TRADES, async () => {
    return await pythonBridge.call('get_trades');
  });

  // ─── Portfolio ────────────────────────────────────────────────────
  
  registerHandle(channels.PORTFOLIO_POSITIONS, async () => {
    return await pythonBridge.call('get_positions');
  });
  
  registerHandle(channels.PORTFOLIO_HOLDINGS, async () => {
    return await pythonBridge.call('get_holdings');
  });
  
  registerHandle(channels.PORTFOLIO_MARGINS, async () => {
    return await pythonBridge.call('get_margins');
  });

  // ─── Market Data ──────────────────────────────────────────────────
  
  registerHandle(channels.MARKET_QUOTE, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_quote', { instruments });
  });
  
  registerHandle(channels.MARKET_LTP, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_ltp', { instruments });
  });
  
  registerHandle(channels.MARKET_OHLC, async (_, instruments: string[]) => {
    return await pythonBridge.call('get_ohlc', { instruments });
  });
  
  registerHandle(channels.MARKET_HISTORICAL, async (_, params: any) => {
    return await pythonBridge.call('get_historical', params);
  });
  
  registerHandle(channels.MARKET_INSTRUMENTS, async (_, exchange: string) => {
    return await pythonBridge.call('get_instruments', { exchange });
  });
  
  registerHandle(channels.MARKET_SEARCH, async (_, query: string) => {
    return await pythonBridge.call('search_instruments', { query });
  });

  // ─── Ticker ───────────────────────────────────────────────────────
  
  registerHandle(channels.TICKER_SUBSCRIBE, async (_, tokens: number[]) => {
    return await pythonBridge.call('ticker_subscribe', { tokens });
  });
  
  registerHandle(channels.TICKER_UNSUBSCRIBE, async (_, tokens: number[]) => {
    return await pythonBridge.call('ticker_unsubscribe', { tokens });
  });
  
  registerHandle(channels.TICKER_STATUS, async () => {
    return await pythonBridge.call('ticker_status');
  });

  // ─── Trading Agent ────────────────────────────────────────────────
  
  registerHandle(channels.AGENT_START, async (_, params) => {
    return await pythonBridge.call('start_agent', params);
  });
  
  registerHandle(channels.AGENT_STOP, async () => {
    return await pythonBridge.call('stop_agent');
  });
  
  registerHandle(channels.AGENT_STATUS, async () => {
    return await pythonBridge.call('agent_status');
  });

  registerHandle(channels.AGENT_SET_MODE, async (_, mode: string) => {
    return await pythonBridge.call('agent_set_mode', { mode });
  });

  registerHandle(channels.AGENT_CLOSE_POSITION, async (_, positionKey: string) => {
    return await pythonBridge.call('agent_close_position', { positionKey });
  });

  registerHandle(channels.AGENT_EMERGENCY_FLATTEN, async (_, scope: 'account') => {
    return await pythonBridge.call('agent_emergency_flatten', { scope });
  });
  
  registerHandle(channels.AGENT_EXECUTE_SIGNAL, async (_, signal: any) => {
    return await pythonBridge.call('execute_signal', { signal });
  });
  
  registerHandle(channels.AGENT_DISMISS_SIGNAL, async (_, signalId: string) => {
    return await pythonBridge.call('agent_dismiss_signal', { signalId });
  });
  
  registerHandle(channels.AGENT_SCAN_NOW, async () => {
    return await pythonBridge.call('agent_scan_now');
  });

  // ─── Activity Log ─────────────────────────────────────────────────
  
  registerHandle(channels.LOG_GET_ALL, async () => {
    return await pythonBridge.call('log_get_all');
  });
  
  registerHandle(channels.LOG_CLEAR, async () => {
    return await pythonBridge.call('log_clear');
  });

  // ─── Settings ─────────────────────────────────────────────────────
  
  registerHandle(channels.SETTINGS_GET, async () => {
    return await pythonBridge.call('get_settings');
  });
  
  registerHandle(channels.SETTINGS_SAVE, async (_, settings: AppSettings) => {
    // Kite credentials change only through account-verified login. Masked
    // fields from the public settings DTO cannot alter the recovery token.
    const kiteUpdates = credentialReplacements(settings.credentials || {});
    if (Object.keys(kiteUpdates).length) throw new Error('Update Kite credentials through login');
    // Bind an existing legacy native key before changing its saved profile.
    // A later save failure must not pair that key with a different provider.
    const nativeCredentials = secureStorage.loadCredentials();
    if (nativeCredentials.llmApiKey && !nativeCredentials.llmProvider) {
      const previous = await pythonBridge.call('get_settings');
      secureStorage.updateCredentials({ llmApiKey: nativeCredentials.llmApiKey, llmProvider: previous.llm.provider });
    }
    // Validate public settings before changing any stored secret.
    const publicSettings = { ...settings, credentials: {}, llm: settings.llm ? { ...settings.llm, apiKey: '' } : undefined };
    const result = await pythonBridge.call('save_settings', publicSettings as unknown as Record<string, unknown>);
    if (settings.llm && settings.llm.apiKey && settings.llm.apiKey !== '********') {
      secureStorage.updateCredentials({ llmApiKey: settings.llm.apiKey, llmProvider: settings.llm.provider });
      await pythonBridge.call('set_credentials', { credentials: secureStorage.loadCredentials() });
    }
    return result;
  });
  
  registerHandle(channels.SETTINGS_SAVE_LLM_KEY, async (_, llmApiKey: string) => {
    if (llmApiKey && llmApiKey !== '********') {
      const settings = await pythonBridge.call('get_settings');
      secureStorage.updateCredentials({ llmApiKey, llmProvider: settings.llm.provider });
      await pythonBridge.call('set_credentials', { credentials: secureStorage.loadCredentials() });
    }
    return { status: 'saved' }; // No need to call python since it's just saving to secure storage
  });

  registerHandle(channels.SETTINGS_DISCOVER_MODELS, async (_, params: any) => {
    return await pythonBridge.call('discover_models', params);
  });
  
  registerHandle(channels.SETTINGS_RESET, async () => {
    return await pythonBridge.call('settings_reset');
  });

  // ─── Watchlist ────────────────────────────────────────────────────
  
  registerHandle(channels.WATCHLIST_GET, async () => {
    return await pythonBridge.call('watchlist_get');
  });
  
  registerHandle(channels.WATCHLIST_ADD, async (_, symbol: string) => {
    return await pythonBridge.call('watchlist_add', { symbol });
  });
  
  registerHandle(channels.WATCHLIST_REMOVE, async (_, symbol: string) => {
    return await pythonBridge.call('watchlist_remove', { symbol });
  });

  // ─── Dashboard ────────────────────────────────────────────────────
  
  registerHandle(channels.DASHBOARD_SUMMARY, async () => {
    return await pythonBridge.call('dashboard_summary');
  });

  // ─── Journal & Analytics ──────────────────────────────────────────

  registerHandle(channels.JOURNAL_GET_TRADES, async () => {
    return await pythonBridge.call('journal_get_trades');
  });

  registerHandle(channels.JOURNAL_GET_EVENTS, async (_, trade_id: string) => {
    return await pythonBridge.call('journal_get_events', { trade_id });
  });

  registerHandle(channels.ANALYTICS_STRATEGY_EXPECTANCY, async () => {
    return await pythonBridge.call('analytics_strategy_expectancy');
  });

  registerHandle(channels.ANALYTICS_CONFLUENCE_VALIDATION, async () => {
    return await pythonBridge.call('analytics_confluence_validation');
  });

  registerHandle(channels.ANALYTICS_SIGNAL_SCORE_CALIBRATION, async () => {
    return await pythonBridge.call('analytics_signal_score_calibration');
  });

  registerHandle(channels.ANALYTICS_EXIT_REASON, async () => {
    return await pythonBridge.call('analytics_exit_reason_effectiveness');
  });

  registerHandle(channels.ANALYTICS_EXIT_QUALITY_REPORT, async () => {
    return await pythonBridge.call('analytics_exit_quality_report');
  });

  registerHandle(channels.ANALYTICS_EXIT_QUALITY_TRADE, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_exit_quality_trade', { trade_id });
  });

  registerHandle(channels.ANALYTICS_EXIT_MANAGEMENT_REPLAY, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_exit_management_replay', { trade_id });
  });

  registerHandle(channels.ANALYTICS_ACTIVE_POSITION_EXPLANATIONS, async () => {
    return await pythonBridge.call('analytics_active_position_explanations');
  });

  registerHandle(channels.ANALYTICS_TRADE_REPLAY, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_trade_replay', { trade_id });
  });

  registerHandle(channels.ANALYTICS_WHAT_IF, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_what_if', { trade_id });
  });

  registerHandle(channels.ANALYTICS_LLM_POST_MORTEM, async (_, trade_id: string) => {
    return await pythonBridge.call('analytics_llm_post_mortem', { trade_id });
  });

  // ─── Backtesting ──────────────────────────────────────────────────
  registerHandle(channels.BACKTEST_RUN, async (_, params: any) => {
    return await pythonBridge.call('run_backtest', params);
  });
}
