try {
  const electron = require('electron');
  const channels = require('../shared/ipc-channels');

  const devModeFlag = (process.env.KITE_DEV_MODE || '').toLowerCase();
  const isDevMode = ['1', 'true', 'yes', 'on'].includes(devModeFlag);

  electron.contextBridge.exposeInMainWorld('electronAPI', {
    isDevMode,
    invoke: (channel: string, ...args: any[]) => electron.ipcRenderer.invoke(channel, ...args),
    on: (channel: string, listener: (...args: any[]) => void) => electron.ipcRenderer.on(channel, listener),
    removeListener: (channel: string, listener: (...args: any[]) => void) => electron.ipcRenderer.removeListener(channel, listener),
    removeAllListeners: (channel: string) => electron.ipcRenderer.removeAllListeners(channel),
    
    auth: {
      login: (apiKey: string, apiSecret: string) => electron.ipcRenderer.invoke(channels.AUTH_LOGIN, apiKey, apiSecret),
      logout: () => electron.ipcRenderer.invoke(channels.AUTH_LOGOUT),
      status: () => electron.ipcRenderer.invoke(channels.AUTH_STATUS),
    },
    orders: {
      place: (orderParams: any) => electron.ipcRenderer.invoke(channels.ORDERS_PLACE, orderParams),
      modify: (orderParams: any) => electron.ipcRenderer.invoke(channels.ORDERS_MODIFY, orderParams),
      cancel: (orderId: string, variety: string) => electron.ipcRenderer.invoke(channels.ORDERS_CANCEL, orderId, variety),
      getAll: () => electron.ipcRenderer.invoke(channels.ORDERS_GET_ALL),
      getTrades: () => electron.ipcRenderer.invoke(channels.ORDERS_GET_TRADES),
    },
    portfolio: {
      positions: () => electron.ipcRenderer.invoke(channels.PORTFOLIO_POSITIONS),
      holdings: () => electron.ipcRenderer.invoke(channels.PORTFOLIO_HOLDINGS),
      margins: () => electron.ipcRenderer.invoke(channels.PORTFOLIO_MARGINS),
    },
    market: {
      quote: (instruments: string[]) => electron.ipcRenderer.invoke(channels.MARKET_QUOTE, instruments),
      ltp: (instruments: string[]) => electron.ipcRenderer.invoke(channels.MARKET_LTP, instruments),
      ohlc: (instruments: string[]) => electron.ipcRenderer.invoke(channels.MARKET_OHLC, instruments),
      historical: (params: any) => electron.ipcRenderer.invoke(channels.MARKET_HISTORICAL, params),
      instruments: (exchange: string) => electron.ipcRenderer.invoke(channels.MARKET_INSTRUMENTS, exchange),
      search: (query: string) => electron.ipcRenderer.invoke(channels.MARKET_SEARCH, query),
    },
    ticker: {
      subscribe: (tokens: number[]) => electron.ipcRenderer.invoke(channels.TICKER_SUBSCRIBE, tokens),
      unsubscribe: (tokens: number[]) => electron.ipcRenderer.invoke(channels.TICKER_UNSUBSCRIBE, tokens),
      status: () => electron.ipcRenderer.invoke(channels.TICKER_STATUS),
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
      start: () => electron.ipcRenderer.invoke(channels.AGENT_START),
      stop: () => electron.ipcRenderer.invoke(channels.AGENT_STOP),
      status: () => electron.ipcRenderer.invoke(channels.AGENT_STATUS),
      executeSignal: (signalId: string) => electron.ipcRenderer.invoke(channels.AGENT_EXECUTE_SIGNAL, signalId),
      dismissSignal: (signalId: string) => electron.ipcRenderer.invoke(channels.AGENT_DISMISS_SIGNAL, signalId),
      scanNow: () => electron.ipcRenderer.invoke(channels.AGENT_SCAN_NOW),
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
      getAll: () => electron.ipcRenderer.invoke(channels.LOG_GET_ALL),
      clear: () => electron.ipcRenderer.invoke(channels.LOG_CLEAR),
      onEntry: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.LOG_ENTRY, listener);
        return () => electron.ipcRenderer.removeListener(channels.LOG_ENTRY, listener);
      }
    },
    settings: {
      get: () => electron.ipcRenderer.invoke(channels.SETTINGS_GET),
      save: (settings: any) => electron.ipcRenderer.invoke(channels.SETTINGS_SAVE, settings),
      reset: () => electron.ipcRenderer.invoke(channels.SETTINGS_RESET),
    },
    watchlist: {
      get: () => electron.ipcRenderer.invoke(channels.WATCHLIST_GET),
      add: (symbol: string) => electron.ipcRenderer.invoke(channels.WATCHLIST_ADD, symbol),
      remove: (symbol: string) => electron.ipcRenderer.invoke(channels.WATCHLIST_REMOVE, symbol),
      onUpdate: (callback: (data: any) => void) => {
        const listener = (_: any, data: any) => callback(data);
        electron.ipcRenderer.on(channels.WATCHLIST_UPDATE, listener);
        return () => electron.ipcRenderer.removeListener(channels.WATCHLIST_UPDATE, listener);
      }
    },
    dashboard: {
      summary: () => electron.ipcRenderer.invoke(channels.DASHBOARD_SUMMARY),
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
  electron.contextBridge.exposeInMainWorld('preloadError', e.message + '\n' + e.stack);
  console.error("PRELOAD ERROR", e);
}
