import { useEffect } from 'react';
import { useTradingStore } from '../stores/trading-store';
import * as IPC from '@shared/ipc-channels';
import { OrderRequest, KiteCredentials, StrategyName, OrderVariety } from '@shared/types';

export interface ElectronAPI {
  isDevMode?: boolean;
  invoke(channel: string, ...args: any[]): Promise<any>;
  on(channel: string, listener: (...args: any[]) => void): () => void;
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
  agent: {
    start(params: { mode: string }): Promise<any>;
    stop(): Promise<any>;
    status(): Promise<any>;
    setMode(mode: string): Promise<any>;
    closePosition(positionKey: string): Promise<any>;
    emergencyFlatten(): Promise<any>;
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
    getSignalScoreCalibration(): Promise<any>;
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

export const useKiteAPI = ({ subscribe = false }: { subscribe?: boolean } = {}) => {
  const store = useTradingStore();

  useEffect(() => {
    if (!subscribe || !window.electronAPI) return;
    let active = true;
    let initialization = 0;
    let stateRevision = 0;

    const tickListener = (_event: any, data: any) => {
      if (data && data.tradingsymbol) {
        store.updateTick(data.tradingsymbol, data);
      }
    };
    const signalListener = (_event: any, data: any) => {
      store.addSignal(data);
    };
    const logListener = (_event: any, data: any) => {
      store.addLogEntry(data);
    };
    const stateListener = (_event: any, data: any) => {
      stateRevision++;
      store.setAgentState({ ...data, statusMessage: data?.statusMessage ?? '' });
    };

    const init = async () => {
      const request = ++initialization;
      const isCurrent = () => active && request === initialization;
      try {
        const authStat = await window.electronAPI?.invoke(IPC.AUTH_STATUS);
        if (!isCurrent()) return;
        if (authStat !== undefined) {
          store.setAuth({ isLoggedIn: authStat === true });
          store.setConnectionStatus(authStat === true ? 'connected' : 'disconnected');
        }
        const revision = stateRevision;
        const agentStat = await window.electronAPI?.invoke(IPC.AGENT_STATUS);
        if (!isCurrent()) return;
        if (agentStat && revision === stateRevision) {
          store.setAgentState({ ...agentStat, statusMessage: agentStat.statusMessage ?? '' });
        }

        const settings = await window.electronAPI?.invoke(IPC.SETTINGS_GET);
        if (!isCurrent()) return;
        if (settings) {
          if (settings.strategies) {
            const enabledStrats = Object.keys(settings.strategies).filter(
              s => settings.strategies[s].enabled
            ) as StrategyName[];
            store.setAgentState({ enabledStrategies: enabledStrats });
          }
          store.setSettings(settings);

          if (Array.isArray(settings.watchlist) && settings.watchlist.length > 0) {
            await loadWatchlist(settings.watchlist, isCurrent);
          }
        }
      } catch (e) {
        console.error("Init Error", e);
      }
    };

    const backendStatusListener = (_event: any, data: any) => {
      if (data?.ready) {
        store.setConnectionStatus('connected');
        if (data.supervision) {
          store.setAgentState({ ...data.supervision, statusMessage: data.supervision.statusMessage ?? '' });
        }
        void init();
      } else {
        initialization++;
        stateRevision++;
        store.setConnectionStatus(data?.error ? 'disconnected' : 'connecting');
        store.setAgentState({
          running: false,
          supervisionActive: false,
          entryPaused: true,
          effectiveMode: 'paused',
          reconciliationPending: true,
          status: data?.error ? 'error' : 'stopped',
          statusMessage: data?.error || 'Backend recovering; supervision is not yet verified.',
        });
      }
    };

    const unsubscribe = [
      window.electronAPI.on(IPC.TICKER_TICK, tickListener),
      window.electronAPI.on(IPC.AGENT_SIGNAL, signalListener),
      window.electronAPI.on(IPC.LOG_ENTRY, logListener),
      window.electronAPI.on(IPC.AGENT_STATE_UPDATE, stateListener),
      window.electronAPI.on(IPC.APP_PYTHON_STATUS, backendStatusListener),
    ];

    const loadWatchlist = async (symbols: string[], isCurrent: () => boolean) => {
      try {
        const instruments = await window.electronAPI?.invoke(IPC.MARKET_INSTRUMENTS, 'NSE') || [];
        const tokenBySymbol: Record<string, number> = {};
        for (const i of instruments) {
          tokenBySymbol[i.tradingsymbol] = i.instrument_token;
        }
        const keys = symbols.map(s => `NSE:${s}`);
        const ltpMap = await window.electronAPI?.invoke(IPC.MARKET_LTP, keys) || {};
        if (!isCurrent()) return;

        const items = symbols
          .map(s => ({ symbol: s, token: tokenBySymbol[s] }))
          .filter((x): x is { symbol: string; token: number } => typeof x.token === 'number')
          .map(({ symbol, token }) => ({
            tradingsymbol: symbol,
            exchange: 'NSE',
            instrumentToken: token,
            lastPrice: ltpMap[`NSE:${symbol}`]?.last_price ?? 0,
            change: 0,
            changePercent: 0,
            open: 0, high: 0, low: 0, close: 0, volume: 0,
            activeSignals: [],
          }));

        store.setWatchlist(items);
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
      active = false;
      unsubscribe.forEach(remove => remove());
    };
  }, [subscribe]);

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

  const cancelOrder = async (orderId: string, variety: OrderVariety | string = 'regular') => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.invoke(IPC.ORDERS_CANCEL, orderId, variety);
  };

  const startAgent = async (mode: string) => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.agent.start({ mode });
  };

  const stopAgent = async () => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.agent.stop();
  };

  const setAgentMode = async (mode: string) => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.agent.setMode(mode);
  };

  const closePosition = async (positionKey: string) => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.agent.closePosition(positionKey);
  };

  const emergencyFlatten = async () => {
    if (!window.electronAPI) throw new Error('Backend connection unavailable');
    return await window.electronAPI.agent.emergencyFlatten();
  };

  return {
    login,
    logout,
    placeOrder,
    cancelOrder,
    startAgent,
    stopAgent,
    setAgentMode,
    closePosition,
    emergencyFlatten,
  };
};
