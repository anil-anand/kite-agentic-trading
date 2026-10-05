try {
  const electron = require('electron');
  const channels = require('../shared/ipc-channels');

  const devModeFlag = (process.env.KITE_DEV_MODE || '').trim().toLowerCase();
  const isDevMode = ['1', 'true', 'yes', 'on'].includes(devModeFlag);

  const invoke = (channel: string, ...args: any[]) => {
    if (!channels.INVOKE_CHANNELS.includes(channel)) throw new Error('Unsupported IPC command');
    return electron.ipcRenderer.invoke(channel, ...args);
  };
  const checkEvent = (channel: string) => {
    if (!channels.EVENT_CHANNELS.includes(channel)) throw new Error('Unsupported IPC event');
  };

  electron.contextBridge.exposeInMainWorld('electronAPI', {
    isDevMode,
    invoke,
    on: (channel: string, listener: (...args: any[]) => void) => {
      checkEvent(channel);
      const wrapped = (_event: any, ...args: any[]) => listener(undefined, ...args);
      electron.ipcRenderer.on(channel, wrapped);
      // Keep the exact native listener in this context, including on renderer
      // remount/reload. Do not return the privileged ipcRenderer emitter.
      return () => electron.ipcRenderer.removeListener(channel, wrapped);
    },
    removeListener: (channel: string, listener: (...args: any[]) => void) => { checkEvent(channel); electron.ipcRenderer.removeListener(channel, listener); },
    removeAllListeners: (channel: string) => { checkEvent(channel); electron.ipcRenderer.removeAllListeners(channel); },
    
    auth: {
      login: (apiKey: string, apiSecret: string, redirectUrl: string) => invoke(channels.AUTH_LOGIN, { apiKey, apiSecret, redirectUrl }),
      logout: () => invoke(channels.AUTH_LOGOUT),
      status: () => invoke(channels.AUTH_STATUS),
    },
    orders: {
      place: (orderParams: any) => invoke(channels.ORDERS_PLACE, orderParams),
      modify: (orderParams: any) => invoke(channels.ORDERS_MODIFY, orderParams),
      cancel: (orderId: string, variety: string) => invoke(channels.ORDERS_CANCEL, orderId, variety),
      getAll: () => invoke(channels.ORDERS_GET_ALL),
      getTrades: () => invoke(channels.ORDERS_GET_TRADES),
    },
    portfolio: {
      positions: () => invoke(channels.PORTFOLIO_POSITIONS),
      holdings: () => invoke(channels.PORTFOLIO_HOLDINGS),
      margins: () => invoke(channels.PORTFOLIO_MARGINS),
    },
    market: {
      quote: (instruments: string[]) => invoke(channels.MARKET_QUOTE, instruments),
      ltp: (instruments: string[]) => invoke(channels.MARKET_LTP, instruments),
      ohlc: (instruments: string[]) => invoke(channels.MARKET_OHLC, instruments),
      historical: (params: any) => invoke(channels.MARKET_HISTORICAL, params),
      instruments: (exchange: string) => invoke(channels.MARKET_INSTRUMENTS, exchange),
      search: (query: string) => invoke(channels.MARKET_SEARCH, query),
    },
    ticker: {
      subscribe: (tokens: number[]) => invoke(channels.TICKER_SUBSCRIBE, tokens),
      unsubscribe: (tokens: number[]) => invoke(channels.TICKER_UNSUBSCRIBE, tokens),
      status: () => invoke(channels.TICKER_STATUS),
      onTick: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.TICKER_TICK, listener);
        return () => electron.ipcRenderer.removeListener(channels.TICKER_TICK, listener);
      },
      onOrderUpdate: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.TICKER_ORDER_UPDATE, listener);
        return () => electron.ipcRenderer.removeListener(channels.TICKER_ORDER_UPDATE, listener);
      }
    },
    agent: {
      start: (params: { mode: string }) => invoke(channels.AGENT_START, params),
      stop: () => invoke(channels.AGENT_STOP),
      status: () => invoke(channels.AGENT_STATUS),
      setMode: (mode: string) => invoke(channels.AGENT_SET_MODE, mode),
      closePosition: (positionKey: string) => invoke(channels.AGENT_CLOSE_POSITION, positionKey),
      emergencyFlatten: () => invoke(channels.AGENT_EMERGENCY_FLATTEN, 'account'),
      executeSignal: (signalId: string) => invoke(channels.AGENT_EXECUTE_SIGNAL, signalId),
      dismissSignal: (signalId: string) => invoke(channels.AGENT_DISMISS_SIGNAL, signalId),
      scanNow: () => invoke(channels.AGENT_SCAN_NOW),
      onStateUpdate: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.AGENT_STATE_UPDATE, listener);
        return () => electron.ipcRenderer.removeListener(channels.AGENT_STATE_UPDATE, listener);
      },
      onSignal: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.AGENT_SIGNAL, listener);
        return () => electron.ipcRenderer.removeListener(channels.AGENT_SIGNAL, listener);
      }
    },
    log: {
      getAll: () => invoke(channels.LOG_GET_ALL),
      clear: () => invoke(channels.LOG_CLEAR),
      onEntry: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.LOG_ENTRY, listener);
        return () => electron.ipcRenderer.removeListener(channels.LOG_ENTRY, listener);
      }
    },
    settings: {
      get: () => invoke(channels.SETTINGS_GET),
      save: (settings: any) => invoke(channels.SETTINGS_SAVE, settings),
      saveLlmKey: (key: string) => invoke(channels.SETTINGS_SAVE_LLM_KEY, key),
      discoverModels: (params: any) => invoke(channels.SETTINGS_DISCOVER_MODELS, params),
      reset: () => invoke(channels.SETTINGS_RESET),
    },
    watchlist: {
      get: () => invoke(channels.WATCHLIST_GET),
      add: (symbol: string) => invoke(channels.WATCHLIST_ADD, symbol),
      remove: (symbol: string) => invoke(channels.WATCHLIST_REMOVE, symbol),
      onUpdate: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.WATCHLIST_UPDATE, listener);
        return () => electron.ipcRenderer.removeListener(channels.WATCHLIST_UPDATE, listener);
      }
    },
    dashboard: {
      summary: () => invoke(channels.DASHBOARD_SUMMARY),
    },
    journal: {
      getTrades: () => invoke(channels.JOURNAL_GET_TRADES),
      getEvents: (tradeId: string) => invoke(channels.JOURNAL_GET_EVENTS, tradeId),
    },
    analytics: {
      getStrategyExpectancy: () => invoke(channels.ANALYTICS_STRATEGY_EXPECTANCY),
      getConfluenceValidation: () => invoke(channels.ANALYTICS_CONFLUENCE_VALIDATION),
      getSignalScoreCalibration: () => invoke(channels.ANALYTICS_SIGNAL_SCORE_CALIBRATION),
      getExitReasonEffectiveness: () => invoke(channels.ANALYTICS_EXIT_REASON),
      getExitQualityReport: () => invoke(channels.ANALYTICS_EXIT_QUALITY_REPORT),
      getExitQualityForTrade: (tradeId: string) => invoke(channels.ANALYTICS_EXIT_QUALITY_TRADE, tradeId),
      getExitManagementReplay: (tradeId: string) => invoke(channels.ANALYTICS_EXIT_MANAGEMENT_REPLAY, tradeId),
      getActivePositionExplanations: () => invoke(channels.ANALYTICS_ACTIVE_POSITION_EXPLANATIONS),
      getTradeReplay: (tradeId: string) => invoke(channels.ANALYTICS_TRADE_REPLAY, tradeId),
      getWhatIfAnalysis: (tradeId: string) => invoke(channels.ANALYTICS_WHAT_IF, tradeId),
      getLlmPostMortem: (tradeId: string) => invoke(channels.ANALYTICS_LLM_POST_MORTEM, tradeId),
    },
    backtest: {
      run: (params: any) => invoke(channels.BACKTEST_RUN, params),
    },
    app: {
      onPythonStatus: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.APP_PYTHON_STATUS, listener);
        return () => electron.ipcRenderer.removeListener(channels.APP_PYTHON_STATUS, listener);
      },
      onError: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.APP_ERROR, listener);
        return () => electron.ipcRenderer.removeListener(channels.APP_ERROR, listener);
      }
    }
  });
} catch (e: any) {
  const electron = require('electron');
  electron.contextBridge.exposeInMainWorld('electronAPI', null);
  electron.contextBridge.exposeInMainWorld('preloadError', 'Application bridge failed to initialize.');
  console.error('PRELOAD ERROR', e);
}
