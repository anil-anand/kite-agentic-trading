import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { publicAuthState } from '@shared/credential-boundary';
import {
  AuthState, Position, Order, Holding, Margins, AgentState,
  Signal, ActivityLogEntry, WatchlistItem, Tick, AppSettings, DashboardSummary
} from '@shared/types';

interface TradingState {
  auth: AuthState;
  positions: Position[];
  orders: Order[];
  holdings: Holding[];
  margins: Margins | null;
  agentState: AgentState;
  signals: Signal[];
  activityLog: ActivityLogEntry[];
  watchlist: WatchlistItem[];
  ticks: Record<string, Tick>;
  settings: AppSettings | null;
  dashboard: DashboardSummary | null;
  connectionStatus: 'connected' | 'disconnected' | 'connecting';

  setAuth: (auth: Partial<AuthState>) => void;
  setPositions: (positions: Position[]) => void;
  setOrders: (orders: Order[]) => void;
  setHoldings: (holdings: Holding[]) => void;
  setMargins: (margins: Margins | null) => void;
  setAgentState: (state: Partial<AgentState>) => void;
  setSignals: (signals: Signal[]) => void;
  addSignal: (signal: Signal) => void;
  removeSignal: (id: string) => void;
  setActivityLog: (logs: ActivityLogEntry[]) => void;
  addLogEntry: (entry: ActivityLogEntry) => void;
  setWatchlist: (watchlist: WatchlistItem[]) => void;
  updateTick: (symbol: string, tick: Tick) => void;
  setSettings: (settings: AppSettings) => void;
  setDashboard: (dashboard: DashboardSummary) => void;
  setConnectionStatus: (status: 'connected' | 'disconnected' | 'connecting') => void;
}

// Earlier versions persisted native credentials here. Delete that payload
// without reading its secrets or hydrating untrusted session state.
try { localStorage.removeItem('kite-trading-storage'); } catch { /* Storage may be disabled. */ }

export const useTradingStore = create<TradingState>()(
  persist(
    (set) => ({
      auth: { isLoggedIn: false, credentials: null, loginUrl: null, error: null },
      positions: [],
      orders: [],
      holdings: [],
      margins: null,
      agentState: {
        running: false,
        mode: 'auto',
        enabledStrategies: [],
        tradesToday: 0,
        signalsGenerated: 0,
        currentPnl: 0,
        maxDrawdownToday: 0,
        lastScanTime: null,
        status: 'idle',
        statusMessage: ''
      },
      signals: [],
      activityLog: [],
      watchlist: [],
      ticks: {},
      settings: null,
      dashboard: null,
      connectionStatus: 'disconnected',

      setAuth: (auth) => set((state) => ({ auth: { ...state.auth, ...publicAuthState(auth) } })),
      setPositions: (positions) => set({ positions }),
      setOrders: (orders) => set({ orders }),
      setHoldings: (holdings) => set({ holdings }),
      setMargins: (margins) => set({ margins }),
      setAgentState: (agentState) => set((state) => ({ agentState: { ...state.agentState, ...agentState } })),
      setSignals: (signals) => set({ signals }),
      addSignal: (signal) => set((state) => {
        const existingIdx = state.signals.findIndex(
          s => s.tradingsymbol === signal.tradingsymbol && s.strategy === signal.strategy
        );

        let newSignals = [...state.signals];
        if (existingIdx >= 0) {
          newSignals[existingIdx] = signal; // Update existing
        } else {
          newSignals = [signal, ...state.signals]; // Prepend new
        }

        // Keep max 50 signals in memory
        return { signals: newSignals.slice(0, 50) };
      }),
      removeSignal: (id) => set((state) => ({
        signals: state.signals.filter(s => s.id !== id)
      })),
      setActivityLog: (activityLog) => set({ activityLog }),
      addLogEntry: (entry) => set((state) => ({ activityLog: [entry, ...state.activityLog] })),
      setWatchlist: (watchlist) => set({ watchlist }),
      updateTick: (symbol, tick) => set((state) => ({ ticks: { ...state.ticks, [symbol]: tick } })),
      setSettings: (settings) => set({ settings }),
      setDashboard: (dashboard) => set({ dashboard }),
      setConnectionStatus: (status) => set({ connectionStatus: status })
    }),
    {
      name: `kite-trading-preferences-v2-${window.electronAPI?.isDevMode ? 'dev' : 'live'}`,
      partialize: (state) => ({ watchlist: state.watchlist }),
      merge: (persisted, current) => {
        const preferences = persisted as Partial<TradingState> | null;
        return { ...current, watchlist: Array.isArray(preferences?.watchlist) ? preferences.watchlist : [] };
      },
    }
  )
);
