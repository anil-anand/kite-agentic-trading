import React from 'react';
import { useTradingStore } from '../stores/trading-store';
import { useKiteAPI } from '../hooks/useKiteAPI';
import PnLDisplay from '../components/PnLDisplay';
import PositionCard from '../components/PositionCard';
import { Activity } from 'lucide-react';
import type { Position } from '@shared/types';

const Dashboard: React.FC = () => {
  const { dashboard, positions, agentState, activityLog, setDashboard, setPositions } = useTradingStore();
  const { startAgent, stopAgent } = useKiteAPI();
  const [positionQuality, setPositionQuality] = React.useState<string | null>(null);
  const [summaryQuality, setSummaryQuality] = React.useState<string | null>(null);

  const handleToggleAgent = async () => {
    try {
      if (agentState.running) {
        await stopAgent();
        useTradingStore.getState().setAgentState({ running: false });
      } else {
        await startAgent(agentState.mode || 'confirm');
        useTradingStore.getState().setAgentState({ running: true });
      }
    } catch (e) {
      console.error('Failed to toggle agent from dashboard', e);
    }
  };

  React.useEffect(() => {
    const fetchData = async () => {
      if (!useTradingStore.getState().auth.isLoggedIn) return;
      const [summaryResult, positionsResult] = await Promise.allSettled([
        window.electronAPI?.dashboard.summary() ?? Promise.reject(new Error('Dashboard summary API unavailable')),
        window.electronAPI?.portfolio.positions() ?? Promise.reject(new Error('Positions API unavailable')),
      ]);

      if (summaryResult.status === 'fulfilled' && summaryResult.value) {
        setDashboard(summaryResult.value);
        setSummaryQuality(null);
      } else {
        setSummaryQuality('UNAVAILABLE');
        if (summaryResult.status === 'rejected') {
          console.error('Failed to fetch dashboard summary:', summaryResult.reason);
        }
      }

      if (positionsResult.status !== 'fulfilled' || !positionsResult.value) {
        setPositionQuality('UNAVAILABLE');
        if (positionsResult.status === 'rejected') {
          console.error('Failed to fetch positions:', positionsResult.reason);
        }
        return;
      }

      const posResponse: any = positionsResult.value;
      const quality = posResponse.snapshotQuality ?? 'COMPLETE';
      setPositionQuality(quality);
      if (!Array.isArray(posResponse.net)) return;
      if (quality === 'COMPLETE') {
        // Only a complete empty snapshot proves the account is flat.
        setPositions(posResponse.net);
      } else if (posResponse.net.length > 0) {
        // A partial response may omit rows.  Overlay the rows it did provide
        // while retaining last-known rows instead of showing a false flat book.
        const incoming = new Map<string, Position>(posResponse.net.map((p: any): [string, Position] => [
          p.positionKey ?? `${p.namespace ?? ''}:${p.accountId ?? ''}:${p.exchange}:${p.tradingsymbol}:${p.product}`,
          p as Position,
        ]));
        const retained: Position[] = useTradingStore.getState().positions.map((p) => {
          const key = p.positionKey ?? `${p.namespace ?? ''}:${p.accountId ?? ''}:${p.exchange}:${p.tradingsymbol}:${p.product}`;
          return incoming.get(key) ?? p;
        });
        const retainedKeys = new Set(retained.map((p) => p.positionKey ?? `${p.namespace ?? ''}:${p.accountId ?? ''}:${p.exchange}:${p.tradingsymbol}:${p.product}`));
        setPositions([...retained, ...posResponse.net.filter((p: any) => !retainedKeys.has(
          p.positionKey ?? `${p.namespace ?? ''}:${p.accountId ?? ''}:${p.exchange}:${p.tradingsymbol}:${p.product}`,
        ))]);
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, [setDashboard, setPositions]);

  const handleExit = (symbol: string) => {
    console.log("Exit", symbol);
  };

  return (
    <div className="p-6 space-y-6 h-full overflow-auto">
      <h1 className="text-2xl font-bold text-white">Dashboard</h1>
      {((positionQuality && positionQuality !== 'COMPLETE') || summaryQuality ||
        (dashboard?.reconciliationStatus && dashboard.reconciliationStatus !== 'RECONCILED')) && (
        <div className="rounded border border-amber-700/60 bg-amber-900/20 p-3 text-sm text-amber-200">
          Broker data is degraded or pending reconciliation. Risk and P&amp;L values may be unavailable.
        </div>
      )}
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <PnLDisplay 
          amount={dashboard?.totalPnl ?? null}
          netAmount={dashboard?.netPnl}
          percentage={
            ((dashboard?.availableMargin ?? 0) + (dashboard?.usedMargin ?? 0)) > 0 && dashboard?.netPnl != null
              ? (dashboard.netPnl / ((dashboard.availableMargin ?? 0) + (dashboard.usedMargin ?? 0))) * 100
              : undefined
          } 
        />
        
        <div className="bg-surface-800 p-4 rounded-xl border border-surface-700 flex flex-col justify-center">
          <span className="text-surface-400 text-sm mb-1">Trades Taken</span>
          <span className="text-3xl font-mono text-white font-bold">{dashboard?.tradesToday || 0}</span>
        </div>
        
        <div className="bg-surface-800 p-4 rounded-xl border border-surface-700 flex flex-col justify-center">
          <span className="text-surface-400 text-sm mb-1">Win Rate</span>
          <span className="text-3xl font-mono text-white font-bold">{dashboard?.winRate || 0}%</span>
        </div>
        
        <div className="bg-surface-800 p-4 rounded-xl border border-surface-700 flex flex-col justify-center">
          <span className="text-surface-400 text-sm mb-1">Available Margin</span>
          <span className="text-3xl font-mono text-white font-bold">{dashboard?.availableMargin != null ? `₹${dashboard.availableMargin.toFixed(2)}` : 'Unavailable'}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <h2 className="text-xl font-semibold text-white">Open Positions</h2>
          {positions.filter(p => p.quantity !== 0).length === 0 && positionQuality !== 'COMPLETE' ? (
            <div className="bg-surface-800 border border-amber-700/60 rounded-xl p-8 flex flex-col items-center justify-center text-amber-200 h-48">
              <Activity size={48} className="mb-4 opacity-40" />
              <p>Open positions unavailable</p>
            </div>
          ) : positions.filter(p => p.quantity !== 0).length === 0 ? (
            <div className="bg-surface-800 border border-surface-700 rounded-xl p-8 flex flex-col items-center justify-center text-surface-400 h-48">
              <Activity size={48} className="mb-4 opacity-20" />
              <p>No open positions</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {positions.filter(p => p.quantity !== 0).map(p => <PositionCard key={p.tradingsymbol} position={p} onExit={handleExit} />)}
            </div>
          )}
        </div>

        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-white">Agent Status</h2>
          <div className="bg-surface-800 border border-surface-700 rounded-xl p-4">
            <div className="flex items-center justify-between mb-4">
              <span className="text-surface-300">Status</span>
              <span className={`px-2 py-1 rounded text-xs font-bold ${agentState.running ? 'bg-profit-fade text-profit-light' : 'bg-surface-700 text-surface-400'}`}>
                {agentState.running ? 'RUNNING' : 'STOPPED'}
              </span>
            </div>
            <div className="flex items-center justify-between mb-4">
              <span className="text-surface-300">Mode</span>
              <span className="text-white capitalize">{agentState.mode}</span>
            </div>
            <button 
              onClick={handleToggleAgent}
              className={`w-full py-2 rounded font-bold transition-colors ${agentState.running ? 'bg-loss-dark hover:bg-loss text-white' : 'bg-profit-dark hover:bg-profit text-white'}`}
            >
              {agentState.running ? 'Stop Agent' : 'Start Agent'}
            </button>
          </div>
          
          <h2 className="text-xl font-semibold text-white pt-4">Recent Activity</h2>
          <div className="bg-surface-800 border border-surface-700 rounded-xl p-4 space-y-3">
            {activityLog.slice(0, 5).map(log => (
              <div key={log.id} className="text-sm border-b border-surface-700 pb-2 last:border-0 last:pb-0">
                <div className="text-xs text-surface-500 mb-1">{new Date(log.timestamp).toLocaleTimeString()}</div>
                <div className="text-surface-200">{log.message}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
