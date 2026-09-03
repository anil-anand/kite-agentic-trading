import React, { useState, useEffect } from 'react';
import { 
  JournalTrade, 
  TradeEvent, 
  StrategyExpectancy, 
  ConfluenceValidation, 
  ConfidenceCalibration, 
  ExitReasonEffectiveness,
  TradeReplayData,
  WhatIfAnalysis,
  LLMPostMortem
} from '../../shared/types';
import { ChevronDown, ChevronRight, Activity, PieChart, BarChart3, Clock, AlertTriangle, LineChart, Cpu, Lightbulb } from 'lucide-react';
import TradeReplayChart from '../components/TradeReplayChart';

export const renderAnalysisLine = (line: string) => line.split(/(\*\*.*?\*\*)/g).map((part, index) =>
  part.startsWith('**') && part.endsWith('**')
    ? <strong key={index}>{part.slice(2, -2)}</strong>
    : part
);

const Journal: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'trades' | 'analytics'>('trades');
  const [trades, setTrades] = useState<JournalTrade[]>([]);
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null);
  const [activeTradeTab, setActiveTradeTab] = useState<'overview' | 'replay' | 'whatif' | 'ai'>('overview');
  
  // Per-trade data
  const [tradeEvents, setTradeEvents] = useState<Record<string, TradeEvent[]>>({});
  const [tradeReplays, setTradeReplays] = useState<Record<string, TradeReplayData>>({});
  const [whatIfs, setWhatIfs] = useState<Record<string, WhatIfAnalysis>>({});
  const [llmPostMortems, setLlmPostMortems] = useState<Record<string, LLMPostMortem>>({});
  
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
    setActiveTradeTab('overview');
    
    if (window.electronAPI) {
      try {
        if (!tradeEvents[tradeId]) {
          const events = await window.electronAPI.journal.getEvents(tradeId);
          setTradeEvents(prev => ({ ...prev, [tradeId]: events }));
        }
        if (!tradeReplays[tradeId]) {
          const replay = await window.electronAPI.analytics.getTradeReplay(tradeId);
          if (replay && !replay.error) {
            setTradeReplays(prev => ({ ...prev, [tradeId]: replay }));
          }
        }
        if (!whatIfs[tradeId]) {
          const whatif = await window.electronAPI.analytics.getWhatIfAnalysis(tradeId);
          if (whatif && !whatif.error) {
            setWhatIfs(prev => ({ ...prev, [tradeId]: whatif }));
          }
        }
        if (!llmPostMortems[tradeId]) {
          const llm = await window.electronAPI.analytics.getLlmPostMortem(tradeId);
          if (llm) {
            setLlmPostMortems(prev => ({ ...prev, [tradeId]: llm }));
          }
        }
      } catch (e) {
        console.error('Error fetching trade details', e);
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
                    <td colSpan={7} className="p-0">
                      
                      {/* Sub-tabs */}
                      <div className="flex border-b border-surface-700 bg-surface-800/50 px-6 pt-4">
                        <button 
                          onClick={() => setActiveTradeTab('overview')}
                          className={`pb-3 mr-6 text-sm font-semibold transition-all relative ${activeTradeTab === 'overview' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
                        >
                          Overview
                          {activeTradeTab === 'overview' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
                        </button>
                        <button 
                          onClick={() => setActiveTradeTab('replay')}
                          className={`pb-3 mr-6 text-sm font-semibold transition-all relative ${activeTradeTab === 'replay' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
                        >
                          <div className="flex items-center"><LineChart size={14} className="mr-1"/> Replay</div>
                          {activeTradeTab === 'replay' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
                        </button>
                        <button 
                          onClick={() => setActiveTradeTab('whatif')}
                          className={`pb-3 mr-6 text-sm font-semibold transition-all relative ${activeTradeTab === 'whatif' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
                        >
                          <div className="flex items-center"><Lightbulb size={14} className="mr-1"/> What-If</div>
                          {activeTradeTab === 'whatif' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
                        </button>
                        <button 
                          onClick={() => setActiveTradeTab('ai')}
                          className={`pb-3 mr-6 text-sm font-semibold transition-all relative ${activeTradeTab === 'ai' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
                        >
                          <div className="flex items-center"><Cpu size={14} className="mr-1"/> AI Post-Mortem</div>
                          {activeTradeTab === 'ai' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
                        </button>
                      </div>

                      <div className="p-6">
                        {activeTradeTab === 'overview' && (
                          <div className="grid grid-cols-2 gap-8 animate-in fade-in duration-300">
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
                        )}

                        {activeTradeTab === 'replay' && (
                          <div className="animate-in fade-in duration-300">
                            {tradeReplays[t.id] ? (
                              <TradeReplayChart trade={t} candles={tradeReplays[t.id].candles} />
                            ) : (
                              <div className="text-surface-400 text-sm py-12 text-center">Loading chart data...</div>
                            )}
                          </div>
                        )}

                        {activeTradeTab === 'whatif' && (
                          <div className="animate-in fade-in duration-300">
                            {whatIfs[t.id] ? (
                              <div className="grid grid-cols-3 gap-6">
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">Held to End of Day</h4>
                                  <div className={`text-2xl font-bold ${whatIfs[t.id].eod_pnl > 0 ? 'text-profit-light' : 'text-loss-light'}`}>
                                    {whatIfs[t.id].eod_pnl > 0 ? '+' : ''}₹{whatIfs[t.id].eod_pnl.toFixed(2)}
                                  </div>
                                  <p className="text-xs text-surface-400 mt-2">Vs Actual: ₹{t.pnl?.toFixed(2) || 0}</p>
                                </div>
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">If Held to Target</h4>
                                  {whatIfs[t.id].target_hit ? (
                                    <>
                                      <div className="text-2xl font-bold text-profit-light">Hit Target</div>
                                      <p className="text-xs text-surface-400 mt-2">At {new Date(whatIfs[t.id].target_hit_time || '').toLocaleTimeString()}</p>
                                    </>
                                  ) : (
                                    <div className="text-2xl font-bold text-surface-400">Target Not Hit</div>
                                  )}
                                </div>
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">1.5x Wider Stop Loss</h4>
                                  <div className="text-sm text-surface-200 mb-1">Stop: ₹{whatIfs[t.id].wider_stop_price.toFixed(2)}</div>
                                  <div className={`text-xl font-bold ${whatIfs[t.id].wider_stop_pnl > 0 ? 'text-profit-light' : 'text-loss-light'}`}>
                                    {whatIfs[t.id].wider_stop_pnl > 0 ? '+' : ''}₹{whatIfs[t.id].wider_stop_pnl.toFixed(2)}
                                  </div>
                                </div>
                              </div>
                            ) : (
                              <div className="text-surface-400 text-sm py-12 text-center">Loading scenarios...</div>
                            )}
                          </div>
                        )}

                        {activeTradeTab === 'ai' && (
                          <div className="animate-in fade-in duration-300">
                            {llmPostMortems[t.id] ? (
                              llmPostMortems[t.id].error ? (
                                <div className="bg-red-500/10 border border-red-500/20 text-red-400 p-4 rounded-lg flex items-center">
                                  <AlertTriangle className="mr-3" size={20} />
                                  {llmPostMortems[t.id].error}
                                </div>
                              ) : (
                                <div className="bg-surface-800 p-6 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="flex items-center text-sm font-semibold text-accent-light mb-4 uppercase tracking-widest">
                                    <Cpu size={16} className="mr-2" /> AI Analysis
                                  </h4>
                                  <div className="prose prose-invert prose-sm max-w-none prose-p:leading-relaxed prose-headings:text-white prose-a:text-accent-light">
                                    {/* Using a simple replace for markdown since we don't have react-markdown */}
                                    {llmPostMortems[t.id].analysis?.split('\n').map((line, i) => {
                                      if (line.startsWith('## ')) return <h3 key={i} className="text-lg mt-4 mb-2">{line.replace('## ', '')}</h3>;
                                      if (line.startsWith('# ')) return <h2 key={i} className="text-xl mt-4 mb-2">{line.replace('# ', '')}</h2>;
                                      if (line.startsWith('* ') || line.startsWith('- ')) return <li key={i} className="ml-4">{line.substring(2)}</li>;
                                      if (line.trim() === '') return <br key={i} />;
                                      return <p key={i}>{renderAnalysisLine(line)}</p>;
                                    })}
                                  </div>
                                </div>
                              )
                            ) : (
                              <div className="text-surface-400 text-sm py-12 flex flex-col items-center justify-center">
                                <div className="animate-spin h-6 w-6 border-2 border-surface-600 border-t-accent-light rounded-full mb-4" />
                                Analyzing trade with AI...
                              </div>
                            )}
                          </div>
                        )}
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
      {expectancy.length === 0 && confluence.length === 0 && calibration.length === 0 && exitReasons.length === 0 && (
        <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 text-center">
          <p className="text-surface-200 font-medium">No completed trades to analyze yet.</p>
          <p className="text-surface-400 text-sm mt-2">Analytics will appear after trades are closed.</p>
        </div>
      )}

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
