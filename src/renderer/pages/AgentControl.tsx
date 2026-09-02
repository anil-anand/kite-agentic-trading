import React from 'react';
import { useTradingStore } from '../stores/trading-store';
import SignalCard from '../components/SignalCard';
import { useKiteAPI } from '../hooks/useKiteAPI';
import { Check, X } from 'lucide-react';

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
        await startAgent(agentState.mode || 'confirm');
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
    
    // Convert to dictionary for backend { "ema_crossover": { "enabled": true }, ... }
    const strategySettings: any = {};
    ['ema_crossover', 'rsi_reversal', 'vwap_bounce', 'supertrend', 'macd_cross', 'bollinger_breakout', 'stochastic_reversal'].forEach(s => {
      strategySettings[s] = { enabled: newStrategies.includes(s) };
    });
    
    window.electronAPI?.invoke('settings:save', { strategies: strategySettings });
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

          <div className="bg-surface-800 border border-surface-700 rounded-xl p-6">
            <h2 className="text-lg font-semibold text-white mb-2">Active Strategies</h2>
            <p className="text-sm text-surface-400 mb-4">All enabled strategies will be evaluated together for every stock in the scan.</p>
            <div className="grid grid-cols-2 gap-4">
              {[
                'ema_crossover', 'rsi_reversal', 'vwap_bounce', 'supertrend', 
                'macd_cross', 'bollinger_breakout', 'stochastic_reversal',
                'adx_momentum', 'psar_trend', 'donchian_breakout', 'cci_reversal',
                'williams_r', 'mfi_exhaustion', 'keltner_breakout', 
                'awesome_oscillator', 'tsi_cross', 'stoc_rsi'
              ].map((strat) => {
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
