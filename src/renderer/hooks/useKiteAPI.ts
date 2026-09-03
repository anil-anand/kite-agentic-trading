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
    discoverModels(params: any): Promise<any>;
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
        }
      } catch (e) {
        console.error("Init Error", e);
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
