import React from 'react';
import { useTradingStore } from '../stores/trading-store';
import SignalCard from '../components/SignalCard';
import { useKiteAPI } from '../hooks/useKiteAPI';
import { Check, X, RefreshCw } from 'lucide-react';
import { buildStrategySettings, STRATEGY_IDS } from '../utils/strategy-settings';
import * as IPC from '@shared/ipc-channels';

const REGIME_LABELS: Record<string, string> = {
  TRENDING: 'Trending',
  RANGE_BOUND: 'Range-bound',
  HIGH_VOLATILITY: 'High volatility',
  UNKNOWN: 'Undetermined',
  MANUAL: 'Manual',
};

const REGIME_STYLES: Record<string, string> = {
  TRENDING: 'bg-profit-fade text-profit-light',
  RANGE_BOUND: 'bg-accent-dark text-white',
  HIGH_VOLATILITY: 'bg-loss-fade text-loss-light',
  UNKNOWN: 'bg-surface-700 text-surface-300',
  MANUAL: 'bg-surface-700 text-surface-300',
};

const SESSION_LABELS: Record<string, string> = {
  warmup: 'Warming up',
  observing: 'Observing (no trades)',
  active: 'Active',
  halted: 'Halted',
};

// Session strategy-selection audit panel (issue #62). Read-only view of the
// deterministic regime decision plus a manual re-evaluate control.
const StrategySelectionPanel: React.FC<{ sessionState?: string }> = ({ sessionState }) => {
  const [record, setRecord] = React.useState<any>(null);
  const [busy, setBusy] = React.useState(false);
  const [advisorOn, setAdvisorOn] = React.useState(false);
  const [newsOn, setNewsOn] = React.useState(false);

  React.useEffect(() => {
    let mounted = true;
    window.electronAPI?.invoke(IPC.AGENT_STRATEGY_SELECTION_GET).then((r: any) => {
      if (mounted && r && Object.keys(r).length) setRecord(r);
    }).catch(() => {});
    window.electronAPI?.invoke(IPC.SETTINGS_GET).then((s: any) => {
      if (mounted && s?.aiStrategyAdvisor) {
        setAdvisorOn(!!s.aiStrategyAdvisor.enabled);
        setNewsOn(!!s.aiStrategyAdvisor.useNews);
      }
    }).catch(() => {});
    const onSelection = (_e: any, data: any) => setRecord(data);
    window.electronAPI?.on(IPC.AGENT_STRATEGY_SELECTION, onSelection);
    return () => {
      mounted = false;
      window.electronAPI?.removeAllListeners(IPC.AGENT_STRATEGY_SELECTION);
    };
  }, []);

  const toggleAdvisor = () => {
    const next = !advisorOn;
    setAdvisorOn(next);
    window.electronAPI?.invoke(IPC.SETTINGS_SAVE, { aiStrategyAdvisor: { enabled: next, useNews: newsOn } });
  };

  const toggleNews = () => {
    const next = !newsOn;
    setNewsOn(next);
    window.electronAPI?.invoke(IPC.SETTINGS_SAVE, { aiStrategyAdvisor: { enabled: advisorOn, useNews: next } });
  };

  const reevaluate = async () => {
    setBusy(true);
    try {
      const r = await window.electronAPI?.invoke(IPC.AGENT_STRATEGY_REEVALUATE);
      if (r) setRecord(r);
    } catch (e) {
      console.error('Re-evaluate failed', e);
    } finally {
      setBusy(false);
    }
  };

  const regime = record?.regime || 'UNKNOWN';
  const decidedAt = record?.decided_at ? new Date(record.decided_at).toLocaleTimeString() : null;
  const missing: string[] = record?.inputs_missing || [];

  return (
    <div className="bg-surface-800 border border-surface-700 rounded-xl p-6">
      <div className="flex justify-between items-center mb-3">
        <h2 className="text-lg font-semibold text-white">Session Strategy Selection</h2>
        <button
          onClick={reevaluate}
          disabled={busy}
          className="flex items-center gap-2 text-xs bg-surface-700 hover:bg-surface-600 disabled:opacity-50 text-white px-3 py-1.5 rounded transition-colors"
        >
          <RefreshCw size={14} className={busy ? 'animate-spin' : ''} /> Re-evaluate
        </button>
      </div>

      <div className="flex items-center justify-between mb-3 p-2 bg-surface-900 rounded-lg border border-surface-700">
        <div>
          <div className="text-sm text-white">AI advisor (LLM)</div>
          <div className="text-[11px] text-surface-400">Lets your configured LLM refine the regime pick. Always falls back to the deterministic decision.</div>
        </div>
        <button
          onClick={toggleAdvisor}
          className={`w-12 h-6 rounded-full relative transition-colors shrink-0 ${advisorOn ? 'bg-accent-light' : 'bg-surface-600'}`}
        >
          <div className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-transform ${advisorOn ? 'left-7' : 'left-1'}`}></div>
        </button>
      </div>

      {advisorOn && (
        <div className="flex items-center justify-between mb-3 p-2 pl-4 bg-surface-900 rounded-lg border border-surface-700">
          <div>
            <div className="text-sm text-white">Include market news</div>
            <div className="text-[11px] text-surface-400">Best-effort market-news headlines added to the advisor prompt. Optional — never blocks the decision.</div>
          </div>
          <button
            onClick={toggleNews}
            className={`w-12 h-6 rounded-full relative transition-colors shrink-0 ${newsOn ? 'bg-accent-light' : 'bg-surface-600'}`}
          >
            <div className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-transform ${newsOn ? 'left-7' : 'left-1'}`}></div>
          </button>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap mb-3">
        <span className={`px-2 py-1 text-xs font-bold rounded ${REGIME_STYLES[regime] || REGIME_STYLES.UNKNOWN}`}>
          {REGIME_LABELS[regime] || regime}
        </span>
        {record?.source === 'llm_advisory' && (
          <span className="px-2 py-1 text-xs rounded bg-accent-dark text-white">LLM advised</span>
        )}
        {sessionState && (
          <span className="px-2 py-1 text-xs rounded bg-surface-700 text-surface-300">
            {SESSION_LABELS[sessionState] || sessionState}
          </span>
        )}
        {decidedAt && <span className="text-xs text-surface-400">decided {decidedAt}</span>}
      </div>

      {record ? (
        <>
          <p className="text-sm text-surface-300 mb-3">{record.rationale}</p>
          {record.applied ? (
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="text-profit-light font-medium">Enabled ({record.enabled?.length || 0})</span>
                <div className="mt-1 flex flex-wrap gap-1">
                  {(record.enabled || []).map((s: string) => (
                    <span key={s} className="text-[10px] bg-surface-900 text-surface-300 px-2 py-0.5 rounded capitalize">
                      {s.replace(/_/g, ' ')}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <span className="text-loss-light font-medium">Disabled ({record.disabled?.length || 0})</span>
                <div className="mt-1 flex flex-wrap gap-1">
                  {(record.disabled || []).map((s: string) => (
                    <span key={s} className="text-[10px] bg-surface-900 text-surface-500 px-2 py-0.5 rounded capitalize">
                      {s.replace(/_/g, ' ')}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <p className="text-xs text-surface-400">No overlay applied — the baseline strategy set is used unchanged.</p>
          )}
          {missing.length > 0 && (
            <p className="text-[11px] text-surface-500 mt-3">Inputs unavailable: {missing.join(', ')}</p>
          )}
        </>
      ) : (
        <p className="text-sm text-surface-400">No decision yet for this session.</p>
      )}
    </div>
  );
};

const AgentControl: React.FC = () => {
  const { agentState, signals, setAgentState } = useTradingStore();
  const { startAgent, stopAgent } = useKiteAPI();

  const handleToggle = async () => {
    try {
      if (agentState.running) {
        await stopAgent();
        setAgentState({ running: false });
        useTradingStore.getState().setSignals([]); // Clear live signals on stop
      } else {
        await startAgent(agentState.mode || 'auto');
        setAgentState({ running: true });
      }
    } catch (e) {
      console.error('Failed to toggle agent', e);
    }
  };

  const handleModeChange = (mode: 'auto' | 'confirm') => {
    setAgentState({ mode });
    window.electronAPI?.invoke('settings:save', { mode });
  };

  const handleStrategyToggle = (strat: any) => {
    const isEnabled = agentState.enabledStrategies.includes(strat);
    const newStrategies = isEnabled 
      ? agentState.enabledStrategies.filter(s => s !== strat)
      : [...agentState.enabledStrategies, strat];
      
    setAgentState({ enabledStrategies: newStrategies });
    
    window.electronAPI?.invoke('settings:save', {
      strategies: buildStrategySettings(newStrategies),
    });
  };

  // Group signals by tradingsymbol + direction to calculate confluence
  const groupedSignals = React.useMemo(() => {
    const groups: Record<string, {
      tradingsymbol: string;
      direction: 'BUY' | 'SELL';
      signals: typeof signals;
      avgConfidence: number;
      confluenceScore: number;
    }> = {};

    signals.forEach(sig => {
      const key = `${sig.tradingsymbol}_${sig.direction}`;
      if (!groups[key]) {
        groups[key] = {
          tradingsymbol: sig.tradingsymbol,
          direction: sig.direction,
          signals: [],
          avgConfidence: 0,
          confluenceScore: 0
        };
      }
      groups[key].signals.push(sig);
    });

    // Calculate aggregates and sort
    return Object.values(groups)
      .map(group => {
        group.confluenceScore = group.signals.length;
        group.avgConfidence = Math.round(group.signals.reduce((acc, s) => acc + s.confidence, 0) / group.confluenceScore);
        return group;
      })
      .sort((a, b) => {
        // Sort by confluence score first (N strategies)
        if (b.confluenceScore !== a.confluenceScore) {
          return b.confluenceScore - a.confluenceScore;
        }
        // Then by average confidence
        return b.avgConfidence - a.avgConfidence;
      });
  }, [signals]);

  return (
    <div className="p-6 flex flex-col space-y-6 min-h-full relative">
      {/* Sticky Header - Use negative margins to span full width and block scroll-behind */}
      <div className="flex justify-between items-center sticky top-0 bg-surface-900 z-30 py-4 px-6 -mx-6 -mt-6 mb-2 border-b border-surface-800">
        <div>
          <h1 className="text-2xl font-bold text-white">Agent Control</h1>
          <p className="text-sm text-surface-400 mt-1">
            Scanning NIFTY 100 universe + your Custom Watchlist using all active strategies below.
          </p>
        </div>
        <button 
          onClick={handleToggle}
          className={`px-8 py-3 rounded-lg font-bold shadow-lg transition-all ${agentState.running ? 'bg-loss-dark hover:bg-loss text-white' : 'bg-profit-dark hover:bg-profit text-white animate-pulse-slow'}`}
        >
          {agentState.running ? 'STOP AGENT' : 'START AGENT'}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 relative">
        {/* Sticky Left Column (Controls) */}
        <div className="lg:col-span-2 space-y-6 sticky top-[100px] h-fit pb-6 z-10">
          <div className="bg-surface-800 border border-surface-700 rounded-xl p-6">
            <h2 className="text-lg font-semibold text-white mb-4">Trading Mode</h2>
            <div className="flex gap-4">
              <label className="flex-1 cursor-pointer" onClick={() => handleModeChange('auto')}>
                <input type="radio" name="mode" value="auto" checked={agentState.mode === 'auto'} readOnly className="hidden" />
                <div className={`p-4 rounded-lg border-2 transition-colors ${agentState.mode === 'auto' ? 'border-accent-light bg-surface-700' : 'border-surface-600 bg-surface-900'}`}>
                  <div className="font-bold text-white mb-1">Full Auto</div>
                  <div className="text-xs text-surface-400">Agent executes trades automatically based on signals.</div>
                </div>
              </label>
              <label className="flex-1 cursor-pointer" onClick={() => handleModeChange('confirm')}>
                <input type="radio" name="mode" value="confirm" checked={agentState.mode === 'confirm'} readOnly className="hidden" />
                <div className={`p-4 rounded-lg border-2 transition-colors ${agentState.mode === 'confirm' ? 'border-accent-light bg-surface-700' : 'border-surface-600 bg-surface-900'}`}>
                  <div className="font-bold text-white mb-1">Signal + Confirm</div>
                  <div className="text-xs text-surface-400">Agent generates signals but waits for manual execution.</div>
                </div>
              </label>
            </div>
          </div>

          <StrategySelectionPanel sessionState={(agentState as any).session_state} />

          <div className="bg-surface-800 border border-surface-700 rounded-xl p-6">
            <h2 className="text-lg font-semibold text-white mb-2">Active Strategies</h2>
            <p className="text-sm text-surface-400 mb-4">All enabled strategies will be evaluated together for every stock in the scan. When a session strategy selection is applied above, only the strategies it enables are evaluated.</p>
            <div className="grid grid-cols-2 gap-4">
              {STRATEGY_IDS.map((strat) => {
                const isEnabled = agentState.enabledStrategies.includes(strat as any);
                return (
                  <div key={strat} className="flex items-center justify-between p-3 bg-surface-900 rounded-lg border border-surface-700">
                    <span className="text-white capitalize">{strat.replace(/_/g, ' ')}</span>
                    <button 
                      onClick={() => handleStrategyToggle(strat)}
                      className={`w-12 h-6 rounded-full relative transition-colors ${isEnabled ? 'bg-accent-light' : 'bg-surface-600'}`}
                    >
                      <div className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-transform ${isEnabled ? 'left-7' : 'left-1'}`}></div>
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* Naturally scrolling Right Column (Live Signals) */}
        <div className="bg-surface-800 border border-surface-700 rounded-xl flex flex-col h-full min-h-[500px]">
          {/* Pad the header itself instead of the parent so it blocks scrolling cards behind it */}
          <div className="sticky top-[89px] bg-surface-800 z-20 p-6 pb-4 border-b border-surface-700 rounded-t-xl">
            <div className="flex justify-between items-center">
              <h2 className="text-lg font-semibold text-white">Live Signals</h2>
              <span className="text-xs text-surface-400">Grouped by Confluence</span>
            </div>
          </div>
          <div className="space-y-4 p-6 pt-4">
            {groupedSignals.length === 0 ? (
              <div className="text-center text-surface-400 mt-10">No active signals</div>
            ) : (
              groupedSignals.map(group => {
                const bestSignal = group.signals.reduce((prev, current) => (prev.confidence > current.confidence) ? prev : current);
                const isBuy = group.direction === 'BUY';

                return (
                  <div key={`${group.tradingsymbol}_${group.direction}`} className="bg-surface-800 border border-surface-700 rounded-xl p-4 flex flex-col gap-3 shadow-lg">
                    <div className="flex justify-between items-start">
                      <div className="flex items-center gap-2">
                        <span className={`px-2 py-1 text-xs font-bold rounded ${isBuy ? 'bg-profit-fade text-profit-light' : 'bg-loss-fade text-loss-light'}`}>
                          {group.direction}
                        </span>
                        <span className="font-bold text-white text-lg">{group.tradingsymbol}</span>
                      </div>
                      <div className="bg-accent-dark text-white px-2 py-1 rounded text-xs font-bold">
                        {group.confluenceScore} {group.confluenceScore === 1 ? 'Strategy' : 'Strategies'}
                      </div>
                    </div>

                    <div className="flex flex-wrap gap-1 mt-1">
                      {group.signals.map(s => (
                        <span key={s.id} className="text-[10px] bg-surface-700 text-surface-300 px-2 py-1 rounded" title={s.reasoning}>
                          {s.strategy} ({s.confidence}%)
                        </span>
                      ))}
                    </div>

                    <div className="grid grid-cols-3 gap-2 text-sm mt-1">
                      <div>
                        <span className="text-surface-400 block text-xs">Entry</span>
                        <span className="font-mono">₹{bestSignal.entryPrice}</span>
                      </div>
                      <div>
                        <span className="text-surface-400 block text-xs">Target</span>
                        <span className="font-mono text-profit-light">₹{bestSignal.target}</span>
                      </div>
                      <div>
                        <span className="text-surface-400 block text-xs">SL</span>
                        <span className="font-mono text-loss-light">₹{bestSignal.stopLoss}</span>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 mt-1">
                      <div className="flex-1 bg-surface-700 h-2 rounded-full overflow-hidden">
                        <div className="h-full bg-accent-light" style={{ width: `${group.avgConfidence}%` }}></div>
                      </div>
                      <span className="text-xs font-mono text-surface-400">Avg {group.avgConfidence}%</span>
                    </div>

                    <div className="flex gap-2 mt-2 pt-3 border-t border-surface-700">
                      <button 
                        onClick={() => {
                          window.electronAPI?.invoke('agent:execute-signal', bestSignal);
                          group.signals.forEach(s => useTradingStore.getState().removeSignal(s.id));
                        }} 
                        className="flex-1 bg-profit-dark hover:bg-profit flex items-center justify-center gap-2 py-2 rounded transition-colors text-white text-sm font-medium"
                      >
                        <Check size={16} /> Take Trade
                      </button>
                      <button 
                        onClick={() => {
                          group.signals.forEach(s => useTradingStore.getState().removeSignal(s.id));
                        }} 
                        className="flex-1 bg-surface-700 hover:bg-surface-600 flex items-center justify-center gap-2 py-2 rounded transition-colors text-white text-sm font-medium"
                      >
                        <X size={16} /> Dismiss {group.confluenceScore > 1 ? 'All' : ''}
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default AgentControl;
