import { useEffect } from 'react';
import { useTradingStore } from '../stores/trading-store';
import * as IPC from '@shared/ipc-channels';
import { OrderRequest, KiteCredentials, StrategyName } from '@shared/types';

export interface ElectronAPI {
  isDevMode?: boolean;
  invoke(channel: string, ...args: any[]): Promise<any>;
  on(channel: string, listener: (...args: any[]) => void): void;
  removeListener(channel: string, listener: (...args: any[]) => void): void;
  removeAllListeners(channel: string): void;
  dashboard: {
    summary(): Promise<any>;
  };
  portfolio: {
    positions(): Promise<any>;
    holdings(): Promise<any>;
    margins(): Promise<any>;
  };
  orders: {
    place(orderParams: any): Promise<any>;
    modify(orderParams: any): Promise<any>;
    cancel(orderId: string, variety: string): Promise<any>;
    getAll(): Promise<any>;
    getTrades(): Promise<any>;
  };
  journal: {
    getTrades(): Promise<any>;
    getEvents(tradeId: string): Promise<any>;
  };
  settings: {
    get(): Promise<any>;
    save(settings: any): Promise<any>;
    saveLlmKey(key: string): Promise<any>;
    reset(): Promise<any>;
  };
  analytics: {
    getStrategyExpectancy(): Promise<any>;
    getConfluenceValidation(): Promise<any>;
    getConfidenceCalibration(): Promise<any>;
    getExitReasonEffectiveness(): Promise<any>;
    getTradeReplay(tradeId: string): Promise<any>;
    getWhatIfAnalysis(tradeId: string): Promise<any>;
    getLlmPostMortem(tradeId: string): Promise<any>;
  };
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export const useKiteAPI = () => {
  const store = useTradingStore();

  useEffect(() => {
    if (!window.electronAPI) return;

    const unsubTick = window.electronAPI.on(IPC.TICKER_TICK, (event: any, data: any) => {
       if (data && data.tradingsymbol) {
         store.updateTick(data.tradingsymbol, data);
       }
    });
    const unsubSignal = window.electronAPI.on(IPC.AGENT_SIGNAL, (event: any, data: any) => {
       store.addSignal(data);
    });
    const unsubLog = window.electronAPI.on(IPC.LOG_ENTRY, (event: any, data: any) => {
       store.addLogEntry(data);
    });
    const unsubAgentState = window.electronAPI.on(IPC.AGENT_STATE_UPDATE, (event: any, data: any) => {
       store.setAgentState(data);
    });

    const init = async () => {
      try {
        const authStat = await window.electronAPI?.invoke(IPC.AUTH_STATUS);
        if (authStat !== undefined) {
          store.setAuth({ isLoggedIn: authStat === true });
          store.setConnectionStatus(authStat === true ? 'connected' : 'disconnected');
        }
        const agentStat = await window.electronAPI?.invoke(IPC.AGENT_STATUS);
        if (agentStat) {
          store.setAgentState({ running: agentStat.running, mode: agentStat.mode || 'auto' });
        }
        const settings = await window.electronAPI?.invoke(IPC.SETTINGS_GET);
        if (settings) {
           if (settings.strategies) {
             const enabledStrats = Object.keys(settings.strategies).filter(
               s => settings.strategies[s].enabled
             ) as StrategyName[];
             store.setAgentState({ enabledStrategies: enabledStrats });
           }
           if (settings.mode) {
             store.setAgentState({ mode: settings.mode });
           } else {
             store.setAgentState({ mode: 'auto' });
           }
           store.setSettings(settings);

           // Populate the watchlist from the configured symbols and subscribe
           // to their live ticks so prices update.
           if (Array.isArray(settings.watchlist) && settings.watchlist.length > 0) {
             await loadWatchlist(settings.watchlist);
           }
        }
      } catch (e) {
        console.error("Init Error", e);
      }
    };

    const loadWatchlist = async (symbols: string[]) => {
      try {
        const instruments = await window.electronAPI?.invoke(IPC.MARKET_INSTRUMENTS, 'NSE') || [];
        const tokenBySymbol: Record<string, number> = {};
        for (const i of instruments) {
          tokenBySymbol[i.tradingsymbol] = i.instrument_token;
        }
        const keys = symbols.map(s => `NSE:${s}`);
        const ltpMap = await window.electronAPI?.invoke(IPC.MARKET_LTP, keys) || {};

        const items = symbols
          .filter(s => tokenBySymbol[s])
          .map(s => ({
            tradingsymbol: s,
            exchange: 'NSE',
            instrumentToken: tokenBySymbol[s],
            lastPrice: ltpMap[`NSE:${s}`]?.last_price || 0,
            change: 0,
            changePercent: 0,
            open: 0, high: 0, low: 0, close: 0, volume: 0,
            activeSignals: [],
          }));

        store.setWatchlist(items as any);
        const tokens = items.map(i => i.instrumentToken);
        if (tokens.length > 0) {
          await window.electronAPI?.invoke(IPC.TICKER_SUBSCRIBE, tokens);
        }
      } catch (e) {
        console.error("Failed to load watchlist", e);
      }
    };

    init();

    return () => {
      window.electronAPI?.removeAllListeners(IPC.TICKER_TICK);
      window.electronAPI?.removeAllListeners(IPC.AGENT_SIGNAL);
      window.electronAPI?.removeAllListeners(IPC.LOG_ENTRY);
      window.electronAPI?.removeAllListeners(IPC.AGENT_STATE_UPDATE);
    };
  }, []);

  const login = async (creds: KiteCredentials) => {
    try {
      const res = await window.electronAPI?.invoke(IPC.AUTH_LOGIN, creds);
      return res;
    } catch (e: any) {
      throw new Error(e.message);
    }
  };

  const logout = async () => {
    await window.electronAPI?.invoke(IPC.AUTH_LOGOUT);
    store.setAuth({ isLoggedIn: false });
    store.setConnectionStatus('disconnected');
  };

  const placeOrder = async (order: OrderRequest) => {
    return await window.electronAPI?.invoke(IPC.ORDERS_PLACE, order);
  };

  const cancelOrder = async (orderId: string) => {
    return await window.electronAPI?.invoke(IPC.ORDERS_CANCEL, orderId);
  };

  const startAgent = async (mode: string) => {
    return await window.electronAPI?.invoke(IPC.AGENT_START, { mode });
  };

  const stopAgent = async () => {
    return await window.electronAPI?.invoke(IPC.AGENT_STOP);
  };

  return { login, logout, placeOrder, cancelOrder, startAgent, stopAgent };
};
