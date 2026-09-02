import React, { useState, useEffect } from 'react';
import { 
  JournalTrade, 
  TradeEvent, 
  StrategyExpectancy, 
  ConfluenceValidation, 
  ConfidenceCalibration, 
  ExitReasonEffectiveness 
} from '../../shared/types';
import { ChevronDown, ChevronRight, Activity, PieChart, BarChart3, Clock, AlertTriangle } from 'lucide-react';

const Journal: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'trades' | 'analytics'>('trades');
  const [trades, setTrades] = useState<JournalTrade[]>([]);
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null);
  const [tradeEvents, setTradeEvents] = useState<Record<string, TradeEvent[]>>({});
  
  // Analytics State
  const [expectancy, setExpectancy] = useState<StrategyExpectancy[]>([]);
  const [confluence, setConfluence] = useState<ConfluenceValidation[]>([]);
  const [calibration, setCalibration] = useState<ConfidenceCalibration[]>([]);
  const [exitReasons, setExitReasons] = useState<ExitReasonEffectiveness[]>([]);

  const [loading, setLoading] = useState(false);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    if (!window.electronAPI) return;
    setLoading(true);
    try {
      const fetchedTrades = await window.electronAPI.journal.getTrades();
      setTrades(fetchedTrades || []);

      const exp = await window.electronAPI.analytics.getStrategyExpectancy();
      const conf = await window.electronAPI.analytics.getConfluenceValidation();
      const calib = await window.electronAPI.analytics.getConfidenceCalibration();
      const exitR = await window.electronAPI.analytics.getExitReasonEffectiveness();

      setExpectancy(exp || []);
      setConfluence(conf || []);
      setCalibration(calib || []);
      setExitReasons(exitR || []);
    } catch (e) {
      console.error('Error loading journal data', e);
    } finally {
      setLoading(false);
    }
  };

  const toggleTrade = async (tradeId: string) => {
    if (expandedTradeId === tradeId) {
      setExpandedTradeId(null);
      return;
    }
    setExpandedTradeId(tradeId);
    
    if (!tradeEvents[tradeId] && window.electronAPI) {
      try {
        const events = await window.electronAPI.journal.getEvents(tradeId);
        setTradeEvents(prev => ({ ...prev, [tradeId]: events }));
      } catch (e) {
        console.error('Error fetching trade events', e);
      }
    }
  };

  const renderTrades = () => (
    <div className="flex flex-col space-y-4">
      <div className="bg-surface-800 rounded-lg overflow-hidden border border-surface-700 shadow-lg">
        <table className="w-full text-left text-sm">
          <thead className="bg-surface-900 text-surface-400">
            <tr>
              <th className="p-4 font-medium">Symbol</th>
              <th className="p-4 font-medium">Dir</th>
              <th className="p-4 font-medium">Strategy</th>
              <th className="p-4 font-medium">Entry Time</th>
              <th className="p-4 font-medium">Entry Price</th>
              <th className="p-4 font-medium">Exit Price</th>
              <th className="p-4 font-medium text-right">P&L</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-700/50">
            {trades.map(t => (
              <React.Fragment key={t.id}>
                <tr 
                  className="hover:bg-surface-750 cursor-pointer transition-colors"
                  onClick={() => toggleTrade(t.id)}
                >
                  <td className="p-4 flex items-center">
                    {expandedTradeId === t.id ? <ChevronDown size={16} className="mr-2 text-surface-400" /> : <ChevronRight size={16} className="mr-2 text-surface-400" />}
                    <span className="font-semibold text-white">{t.tradingsymbol}</span>
                  </td>
                  <td className="p-4">
                    <span className={`px-2 py-1 rounded text-xs font-medium ${t.direction === 'BUY' ? 'bg-profit-dark text-profit-light' : 'bg-loss-dark text-loss-light'}`}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="p-4 text-surface-200">{t.strategy}</td>
                  <td className="p-4 text-surface-300">{new Date(t.entry_time).toLocaleString()}</td>
                  <td className="p-4 text-surface-200">₹{t.entry_price?.toFixed(2)}</td>
                  <td className="p-4 text-surface-200">{t.exit_price ? `₹${t.exit_price.toFixed(2)}` : '-'}</td>
                  <td className={`p-4 text-right font-medium ${t.pnl && t.pnl > 0 ? 'text-profit-light' : t.pnl && t.pnl < 0 ? 'text-loss-light' : 'text-surface-300'}`}>
                    {t.pnl ? `${t.pnl > 0 ? '+' : ''}₹${t.pnl.toFixed(2)}` : '-'}
                  </td>
                </tr>
                
                {/* Expanded Details */}
                {expandedTradeId === t.id && (
                  <tr className="bg-surface-900 border-b border-surface-700 shadow-inner">
                    <td colSpan={7} className="p-6">
                      <div className="grid grid-cols-2 gap-8">
                        <div>
                          <h4 className="text-sm font-semibold text-surface-200 mb-4 flex items-center">
                            <Clock size={16} className="mr-2 text-accent-light" /> Timeline
                          </h4>
                          <div className="space-y-4 pl-2 border-l-2 border-surface-700/50">
                            {(tradeEvents[t.id] || []).map(e => {
                              const details = JSON.parse(e.details || '{}');
                              return (
                                <div key={e.id} className="relative pl-6">
                                  <div className="absolute w-3 h-3 bg-accent-light rounded-full -left-[23px] top-1.5 shadow-[0_0_8px_rgba(var(--color-accent-light),0.5)]" />
                                  <div className="text-xs text-surface-400 mb-1">{new Date(e.timestamp).toLocaleTimeString()}</div>
                                  <div className="text-sm font-medium text-white">{e.event_type.replace('_', ' ').toUpperCase()}</div>
                                  <pre className="text-xs text-surface-300 mt-2 bg-surface-800 p-3 rounded-lg max-w-full overflow-x-auto whitespace-pre-wrap border border-surface-700/50">
                                    {JSON.stringify(details, null, 2)}
                                  </pre>
                                </div>
                              );
                            })}
                            {(!tradeEvents[t.id] || tradeEvents[t.id].length === 0) && (
                              <div className="text-sm text-surface-400 pl-4">Loading events...</div>
                            )}
                          </div>
                        </div>
                        <div className="space-y-6">
                          <div>
                            <h4 className="text-sm font-semibold text-surface-200 mb-3 flex items-center">
                              <Activity size={16} className="mr-2 text-accent-light" /> Context & Rationale
                            </h4>
                            <div className="bg-surface-800 p-4 rounded-lg border border-surface-700 text-sm text-surface-200 space-y-3">
                              <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Reasoning</span> {t.reasoning || 'N/A'}</p>
                              <div className="grid grid-cols-2 gap-4 pt-2 border-t border-surface-700/50">
                                <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Confidence</span> {t.confidence ? `${t.confidence}%` : 'N/A'}</p>
                                <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Status</span> {t.status}</p>
                              </div>
                              {t.exit_reason && (
                                <div className="pt-2 border-t border-surface-700/50">
                                  <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Exit Reason</span> {t.exit_reason}</p>
                                </div>
                              )}
                            </div>
                          </div>
                          {t.confluence_snapshot && (
                             <div>
                               <h4 className="text-sm font-semibold text-surface-200 mb-3 flex items-center">
                                 <AlertTriangle size={16} className="mr-2 text-warning-light" /> Confluence Snapshot
                               </h4>
                               <pre className="bg-surface-800 p-4 rounded-lg border border-surface-700 text-xs text-surface-300 overflow-x-auto">
                                 {JSON.stringify(JSON.parse(t.confluence_snapshot), null, 2)}
                               </pre>
                             </div>
                          )}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
            {trades.length === 0 && !loading && (
              <tr>
                <td colSpan={7} className="p-12 text-center text-surface-400">
                  <div className="flex flex-col items-center">
                    <Clock size={32} className="mb-4 opacity-50" />
                    <p>No trades found in the journal.</p>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );

  const renderAnalytics = () => (
    <div className="space-y-8 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* Expectancy */}
      <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg">
        <h3 className="text-lg font-semibold text-white mb-6 flex items-center">
          <BarChart3 className="mr-2 text-accent-light" size={20} /> Strategy Expectancy
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-surface-400 border-b border-surface-700">
              <tr>
                <th className="pb-4 font-medium uppercase tracking-wider text-xs">Strategy</th>
                <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Trades</th>
                <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Win Rate</th>
                <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Profit Factor</th>
                <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Avg R</th>
                <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Avg Hold (m)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-700/50">
              {expectancy.map((e, idx) => (
                <tr key={e.strategy} className="hover:bg-surface-750 transition-colors" style={{ animationDelay: `${idx * 50}ms` }}>
                  <td className="py-4 font-medium text-white">{e.strategy}</td>
                  <td className="py-4 text-right text-surface-200">{e.total_trades}</td>
                  <td className="py-4 text-right">
                    <div className="flex items-center justify-end">
                      <span className="w-12 text-surface-200 font-medium">{e.win_rate_pct}%</span>
                      <div className="w-24 h-2.5 bg-surface-900 rounded-full ml-3 overflow-hidden shadow-inner">
                        <div className={`h-full rounded-full transition-all duration-1000 ${e.win_rate_pct > 50 ? 'bg-profit-light shadow-[0_0_8px_rgba(0,255,128,0.3)]' : 'bg-warning-light'}`} style={{ width: `${e.win_rate_pct}%` }} />
                      </div>
                    </div>
                  </td>
                  <td className="py-4 text-right text-surface-200">{e.profit_factor ? e.profit_factor.toFixed(2) : '∞'}</td>
                  <td className={`py-4 text-right font-semibold ${e.avg_r_multiple > 0 ? 'text-profit-light' : 'text-loss-light'}`}>{e.avg_r_multiple.toFixed(2)}</td>
                  <td className="py-4 text-right text-surface-200">{e.avg_hold_time_mins}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-8">
        {/* Confluence */}
        <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg transition-transform hover:-translate-y-1 duration-300">
          <h3 className="text-lg font-semibold text-white mb-6 flex items-center">
            <PieChart className="mr-2 text-accent-light" size={20} /> Confluence Edge
          </h3>
          <div className="space-y-5">
            {confluence.map((c, idx) => (
              <div key={c.confluence_count} className="flex flex-col space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-surface-300 font-medium">{c.confluence_count} Strategies Firing</span>
                  <span className="font-semibold text-white">{c.win_rate_pct}% win <span className="text-surface-400 font-normal">({c.total_trades} trades)</span></span>
                </div>
                <div className="h-3 w-full bg-surface-900 rounded-full overflow-hidden shadow-inner">
                  <div className="h-full bg-gradient-to-r from-accent-dark to-accent-light rounded-full transition-all duration-1000" style={{ width: `${c.win_rate_pct}%`, animationDelay: `${idx * 100}ms` }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Confidence Calibration */}
        <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg transition-transform hover:-translate-y-1 duration-300">
          <h3 className="text-lg font-semibold text-white mb-6 flex items-center">
            <Activity className="mr-2 text-accent-light" size={20} /> Confidence Calibration
          </h3>
          <div className="space-y-5">
            {calibration.map((c, idx) => (
              <div key={c.confidence_bucket} className="flex flex-col space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-surface-300 font-medium">Predicted: {c.confidence_bucket}%</span>
                  <span className="font-semibold text-white">Actual: {c.actual_win_rate_pct}% <span className="text-surface-400 font-normal">({c.total_trades} trades)</span></span>
                </div>
                <div className="h-3 w-full bg-surface-900 rounded-full overflow-hidden shadow-inner">
                  <div className="h-full bg-profit-light rounded-full transition-all duration-1000 shadow-[0_0_8px_rgba(0,255,128,0.3)]" style={{ width: `${c.actual_win_rate_pct}%`, animationDelay: `${idx * 100}ms` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
      
      {/* Exit Reasons */}
      <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg">
        <h3 className="text-lg font-semibold text-white mb-6">Exit Reason Effectiveness</h3>
        <table className="w-full text-left text-sm">
          <thead className="text-surface-400 border-b border-surface-700">
            <tr>
              <th className="pb-4 font-medium uppercase tracking-wider text-xs">Exit Reason</th>
              <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Trades</th>
              <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Win Rate</th>
              <th className="pb-4 font-medium text-right uppercase tracking-wider text-xs">Total P&L</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-700/50">
            {exitReasons.map(e => (
              <tr key={e.exit_reason} className="hover:bg-surface-750 transition-colors">
                <td className="py-4 font-medium text-white">{e.exit_reason}</td>
                <td className="py-4 text-right text-surface-200">{e.total_trades}</td>
                <td className="py-4 text-right font-medium text-surface-200">{e.win_rate_pct}%</td>
                <td className={`py-4 text-right font-semibold ${e.total_pnl > 0 ? 'text-profit-light' : 'text-loss-light'}`}>
                  ₹{e.total_pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );

  return (
    <div className="p-8 max-w-7xl mx-auto h-full overflow-y-auto custom-scrollbar">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white tracking-tight flex items-center">
            <PieChart className="mr-3 text-accent-light" size={32} />
            Journal & Analytics
          </h1>
          <p className="text-surface-400 mt-2 text-sm">Review past trades, audit performance, and validate strategy edge.</p>
        </div>
        <button 
          onClick={loadData} 
          disabled={loading}
          className="px-5 py-2.5 bg-accent-dark hover:bg-accent-light text-white rounded-lg transition-all duration-300 shadow-lg hover:shadow-accent-dark/50 text-sm font-medium border border-accent-light/20 flex items-center"
        >
          {loading ? (
            <><div className="animate-spin h-4 w-4 border-2 border-white/20 border-t-white rounded-full mr-2" /> Loading...</>
          ) : 'Refresh Data'}
        </button>
      </div>

      <div className="flex border-b border-surface-800 mb-8 space-x-8">
        <button 
          onClick={() => setActiveTab('trades')}
          className={`pb-4 text-sm font-semibold transition-all relative outline-none ${activeTab === 'trades' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
        >
          <div className="flex items-center">
            <Clock size={16} className="mr-2" /> Trades Log
          </div>
          {activeTab === 'trades' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light rounded-t-full shadow-[0_0_8px_rgba(var(--color-accent-light),0.8)]" />}
        </button>
        <button 
          onClick={() => setActiveTab('analytics')}
          className={`pb-4 text-sm font-semibold transition-all relative outline-none ${activeTab === 'analytics' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
        >
          <div className="flex items-center">
            <BarChart3 size={16} className="mr-2" /> Analytics Overview
          </div>
          {activeTab === 'analytics' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light rounded-t-full shadow-[0_0_8px_rgba(var(--color-accent-light),0.8)]" />}
        </button>
      </div>

      <div className="relative">
        {loading && trades.length === 0 ? (
          <div className="absolute inset-0 z-10 flex items-center justify-center py-20 bg-surface-900/50 backdrop-blur-sm rounded-lg">
            <div className="animate-spin rounded-full h-10 w-10 border-4 border-surface-700 border-t-accent-light shadow-lg"></div>
          </div>
        ) : null}
        
        {activeTab === 'trades' ? renderTrades() : renderAnalytics()}
      </div>
    </div>
  );
};

export default Journal;
