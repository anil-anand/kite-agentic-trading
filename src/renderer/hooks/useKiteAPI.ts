import { useEffect } from 'react';
import { useTradingStore } from '../stores/trading-store';
import * as IPC from '@shared/ipc-channels';
import { OrderRequest, KiteCredentials } from '@shared/types';

export interface ElectronAPI {
  invoke(channel: string, ...args: any[]): Promise<any>;
  on(channel: string, listener: (...args: any[]) => void): void;
  removeListener(channel: string, listener: (...args: any[]) => void): void;
  removeAllListeners(channel: string): void;
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
        }
        const agentStat = await window.electronAPI?.invoke(IPC.AGENT_STATUS);
        if (agentStat) {
          store.setAgentState({ running: agentStat.running, mode: agentStat.mode || 'confirm' });
        }
        const settings = await window.electronAPI?.invoke(IPC.SETTINGS_GET);
        if (settings && settings.strategies) {
           const enabledStrats = Object.keys(settings.strategies).filter(s => settings.strategies[s].enabled);
           store.setAgentState({ enabledStrategies: enabledStrats });
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
