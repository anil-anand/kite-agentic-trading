import React, { useState, useEffect } from 'react';
import {
  JournalTrade,
  TradeEvent,
  StrategyExpectancy,
  ConfluenceValidation,
  SignalScoreCalibration,
  ExitReasonEffectiveness,
  ExitDecisionReplay,
  ExitQualityRecord,
  ExitQualityReport
} from '../../shared/types';
import { ChevronDown, ChevronRight, Activity, PieChart, BarChart3, Clock, AlertTriangle, LineChart, Lightbulb } from 'lucide-react';
import ExitReplayPanel from '../components/ExitReplayPanel';

const formatR = (value: number | null) => value == null || !Number.isFinite(value) ? 'Unavailable' : `${value.toFixed(2)}R`;
const retainedJSON = (value: string) => { try { return JSON.parse(value); } catch { return { quality: 'CORRUPT_RETAINED_JSON', raw: value }; } };

const Journal: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'trades' | 'analytics'>('trades');
  const [trades, setTrades] = useState<JournalTrade[]>([]);
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null);
  const [activeTradeTab, setActiveTradeTab] = useState<'overview' | 'replay' | 'quality'>('overview');

  // Per-trade data
  const [tradeEvents, setTradeEvents] = useState<Record<string, TradeEvent[]>>({});
  const [exitReplays, setExitReplays] = useState<Record<string, ExitDecisionReplay>>({});
  const [exitQuality, setExitQuality] = useState<Record<string, ExitQualityRecord>>({});
  const [detailErrors, setDetailErrors] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState<string | null>(null);


  // Analytics State
  const [expectancy, setExpectancy] = useState<StrategyExpectancy[]>([]);
  const [confluence, setConfluence] = useState<ConfluenceValidation[]>([]);
  const [calibration, setCalibration] = useState<SignalScoreCalibration[]>([]);
  const [exitReasons, setExitReasons] = useState<ExitReasonEffectiveness[]>([]);
  const [qualityReport, setQualityReport] = useState<ExitQualityReport | null>(null);

  const [loading, setLoading] = useState(false);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    if (!window.electronAPI) return;
    setLoading(true);
    setLoadError(null);
    try {
      const fetchedTrades = await window.electronAPI.journal.getTrades();
      setTrades(fetchedTrades || []);

      const exp = await window.electronAPI.analytics.getStrategyExpectancy();
      const conf = await window.electronAPI.analytics.getConfluenceValidation();
      const calib = await window.electronAPI.analytics.getSignalScoreCalibration();
      const exitR = await window.electronAPI.analytics.getExitReasonEffectiveness();
      const quality = await window.electronAPI.analytics.getExitQualityReport();

      setExpectancy(exp || []);
      setConfluence(conf || []);
      setCalibration(calib || []);
      setExitReasons(exitR || []);
      setQualityReport(quality || null);
      // Cached detail rows can change as fills and reconciliation arrive.
      setTradeEvents({});
      setExitReplays({});
      setExitQuality({});
      setDetailErrors({});
      setExpandedTradeId(null);
    } catch (e) {
      setLoadError('Journal refresh failed. Previously displayed values may be stale.');
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

    const api = window.electronAPI;
    if (api) {
      const jobs: Array<[string, () => Promise<void>]> = [
        ['events', async () => { if (!tradeEvents[tradeId]) {
          const events = await api.journal.getEvents(tradeId);
          setTradeEvents(prev => ({ ...prev, [tradeId]: events }));
        } }],
        ['replay', async () => { if (!exitReplays[tradeId]) {
          const replay = await api.analytics.getExitManagementReplay(tradeId);
          if (replay && !replay.error) {
            setExitReplays(prev => ({ ...prev, [tradeId]: replay }));
          } else throw new Error('Replay unavailable');
        } }],
        ['quality', async () => { if (!exitQuality[tradeId]) {
          const quality = await api.analytics.getExitQualityForTrade(tradeId);
          if (quality && !quality.error) {
            setExitQuality(prev => ({ ...prev, [tradeId]: quality }));
          } else throw new Error('Quality metrics unavailable');
        } }],
      ];
      await Promise.allSettled(jobs.map(async ([kind, job]) => {
        const key = `${tradeId}:${kind}`;
        setDetailErrors(prev => ({ ...prev, [key]: '' }));
        try { await job(); } catch {
          setDetailErrors(prev => ({ ...prev, [key]: `${kind} unavailable. Reopen the trade to retry.` }));
        }
      }));
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
              <th className="p-4 font-medium text-right">Gross P&L</th>
              <th className="p-4 font-medium text-right">Net P&L</th>
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
                  <td className="p-4 text-surface-200">
                    {t.strategy}
                    {t.status !== 'CLOSED' && (
                      <span className="ml-2 rounded bg-amber-900/40 px-2 py-1 text-[10px] text-amber-200">
                        {t.status.replace('_', ' ')}
                      </span>
                    )}
                    {t.financial_quality && t.financial_quality !== 'RECONCILED' && (
                      <span className="ml-2 rounded bg-surface-700 px-2 py-1 text-[10px] text-surface-300">
                        {t.financial_quality}
                      </span>
                    )}
                  </td>
                  <td className="p-4 text-surface-300">{new Date(t.entry_time).toLocaleString()}</td>
                  <td className="p-4 text-surface-200">₹{t.entry_price?.toFixed(2)}</td>
                  <td className="p-4 text-surface-200">{t.exit_price ? `₹${t.exit_price.toFixed(2)}` : '-'}</td>
                  <td className={`p-4 text-right font-medium ${t.gross_pnl && t.gross_pnl > 0 ? 'text-profit-light' : t.gross_pnl && t.gross_pnl < 0 ? 'text-loss-light' : 'text-surface-300'}`}>
                    {t.gross_pnl != null ? `${t.gross_pnl > 0 ? '+' : ''}₹${t.gross_pnl.toFixed(2)}` : '-'}
                  </td>
                  <td className={`p-4 text-right font-medium ${t.net_pnl && t.net_pnl > 0 ? 'text-profit-light' : t.net_pnl && t.net_pnl < 0 ? 'text-loss-light' : 'text-surface-300'}`}>
                    {t.net_pnl != null ? `${t.net_pnl > 0 ? '+' : ''}₹${t.net_pnl.toFixed(2)}` : '-'}
                  </td>
                </tr>

                {/* Expanded Details */}
                {expandedTradeId === t.id && (
                  <tr className="bg-surface-900 border-b border-surface-700 shadow-inner">
                    <td colSpan={8} className="p-0">

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
                          <div className="flex items-center"><LineChart size={14} className="mr-1" /> Replay</div>
                          {activeTradeTab === 'replay' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
                        </button>
                        <button
                          onClick={() => setActiveTradeTab('quality')}
                          className={`pb-3 mr-6 text-sm font-semibold transition-all relative ${activeTradeTab === 'quality' ? 'text-accent-light' : 'text-surface-400 hover:text-white'}`}
                        >
                          <div className="flex items-center"><Lightbulb size={14} className="mr-1" /> Exit Quality</div>
                          {activeTradeTab === 'quality' && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-accent-light" />}
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
                                  const details = retainedJSON(e.details || '{}');
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
                                  <div className="text-sm text-surface-400 pl-4">{detailErrors[`${t.id}:events`] || (tradeEvents[t.id] ? 'No retained trade events.' : 'Loading events...')}</div>
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
                                        <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Entry Signal Score</span> {(t.signal_score ?? (t as any).signalScore) != null ? `${t.signal_score ?? (t as any).signalScore} / 100` : 'N/A'}</p>
                                      {(t.estimated_probability != null || (t as any).estimatedProbability != null) && (
                                        <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Historical Score-Bucket Estimate</span> {((t.estimated_probability ?? (t as any).estimatedProbability) * 100).toFixed(1)}% <span className="text-[10px] text-surface-400 opacity-80">(not an exit probability; n={t.calibration_sample_size ?? (t as any).calibrationSampleSize})</span></p>
                                      )}
                                    </div>
                                    {(t.market_regime || t.strategy_family || t.production_playbook || t.screener_score != null) && (
                                      <div className="grid grid-cols-2 gap-4 pt-2 border-t border-surface-700/50">
                                        {t.market_regime && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Market Regime</span> {t.market_regime}</p>}
                                        {t.strategy_family && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Strategy Family</span> {t.strategy_family}</p>}
                                        {t.production_playbook && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Playbook</span> {t.production_playbook}</p>}
                                        {t.screener_score != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Screener Score</span> {t.screener_score.toFixed(2)}</p>}
                                      </div>
                                    )}
                                    {(t.target_distance != null || t.stop_distance != null || t.initial_r != null) && (
                                      <div className="grid grid-cols-3 gap-4 pt-2 border-t border-surface-700/50">
                                        {t.target_distance != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Target Dist</span> {t.target_distance.toFixed(2)}</p>}
                                        {t.stop_distance != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Stop Dist</span> {t.stop_distance.toFixed(2)}</p>}
                                        {t.initial_r != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Legacy initial R (unit unverified)</span> {t.initial_r.toFixed(2)}</p>}
                                      </div>
                                    )}
                                    {(t.realized_r != null || t.mae != null || t.mfe != null || t.holding_time_seconds != null) && (
                                      <div className="grid grid-cols-4 gap-2 pt-2 border-t border-surface-700/50">
                                        {t.realized_r != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Realized R</span> <span className={t.realized_r >= 0 ? 'text-profit-light' : 'text-loss-light'}>{t.realized_r.toFixed(2)}</span></p>}
                                        {t.mae != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Legacy MAE (unit unverified)</span> {t.mae.toFixed(2)}</p>}
                                        {t.mfe != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Legacy MFE (unit unverified)</span> {t.mfe.toFixed(2)}</p>}
                                        {t.holding_time_seconds != null && <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Hold Time</span> {(t.holding_time_seconds / 60).toFixed(1)}m</p>}
                                      </div>
                                    )}
                                  {t.exit_reason && (
                                    <div className="pt-2 border-t border-surface-700/50">
                                      <p><span className="text-surface-400 block text-xs mb-1 uppercase tracking-wider">Exit Reason</span> {t.exit_reason}</p>
                                    </div>
                                  )}
                                  {t.gross_pnl != null ? (
                                    <div className="pt-2 border-t border-surface-700/50">
                                      <h5 className="text-surface-400 block text-xs mb-2 uppercase tracking-wider">Execution & Costs</h5>
                                      <div className="grid grid-cols-2 gap-2 text-xs">
                                        <p className="flex justify-between"><span>Slippage:</span> <span className={t.slippage && t.slippage < 0 ? 'text-loss-light' : 'text-profit-light'}>₹{t.slippage?.toFixed(2) || '0.00'}</span></p>
                                        <p className="flex justify-between"><span>Brokerage:</span> <span>₹{t.brokerage?.toFixed(2) || '0.00'}</span></p>
                                        <p className="flex justify-between"><span>Taxes (STT/GST):</span> <span>₹{t.taxes?.toFixed(2) || '0.00'}</span></p>
                                        <p className="flex justify-between"><span>Exchange Txn:</span> <span>₹{t.exchange_charges?.toFixed(2) || '0.00'}</span></p>
                                        <p className="flex justify-between"><span>Other Fees:</span> <span>₹{t.other_fees?.toFixed(2) || '0.00'}</span></p>
                                        <p className="flex justify-between font-semibold"><span>Total Fees:</span> <span>₹{((t.brokerage || 0) + (t.taxes || 0) + (t.exchange_charges || 0) + (t.other_fees || 0)).toFixed(2)}</span></p>
                                      </div>
                                    </div>
                                  ) : (
                                    <div className="pt-2 border-t border-surface-700/50">
                                      <h5 className="text-surface-400 block text-xs mb-2 uppercase tracking-wider">Execution & Costs</h5>
                                      <div className="text-xs text-surface-500">N/A (Legacy Trade)</div>
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
                                    {JSON.stringify(retainedJSON(t.confluence_snapshot), null, 2)}
                                  </pre>
                                </div>
                              )}
                            </div>
                          </div>
                        )}

                        {activeTradeTab === 'replay' && (
                          <div className="animate-in fade-in duration-300">
                            {exitReplays[t.id] ? (
                              <ExitReplayPanel replay={exitReplays[t.id]} />
                            ) : (
                              <div className="text-surface-400 text-sm py-12 text-center">{detailErrors[`${t.id}:replay`] || 'Loading retained decision trace...'}</div>
                            )}
                          </div>
                        )}

                        {activeTradeTab === 'quality' && (
                          <div className="animate-in fade-in duration-300">
                            {exitQuality[t.id] ? (
                              <div className="space-y-4">
                                {!exitQuality[t.id].eligible && <p className="rounded border border-amber-700/50 bg-amber-950/20 p-3 text-sm text-amber-200">Excluded from aggregate research: {exitQuality[t.id].exclusion_reason || 'unavailable execution facts'}.</p>}
                                <p className="text-xs text-surface-400">Accounting: {exitQuality[t.id].quality} · Replay: {exitQuality[t.id].replay_status} · Metric coverage: {exitQuality[t.id].coverage.available_count}/{exitQuality[t.id].coverage.total_count}</p>
                                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">Captured R</h4>
                                  <div className="text-2xl font-bold text-white">{formatR(exitQuality[t.id]!.metrics.captured_net_r)} net</div>
                                  <p className="text-xs text-surface-400 mt-2">Gross: {formatR(exitQuality[t.id]!.metrics.captured_gross_r)}</p>
                                </div>
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">MFE / MAE</h4>
                                  <div className="text-xl font-bold text-white">{formatR(exitQuality[t.id]!.metrics.mfe_r)} / {formatR(exitQuality[t.id]!.metrics.mae_r)}</div>
                                  <p className="text-xs text-surface-400 mt-2">{exitQuality[t.id].coverage.extrema_quality}</p>
                                </div>
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">Price-path giveback</h4>
                                  <div className="text-xl font-bold text-white">{formatR(exitQuality[t.id]!.metrics.r_given_back)}</div>
                                  <p className="text-xs text-surface-400 mt-2">MFE capture: {exitQuality[t.id]!.metrics.mfe_capture_pct == null ? 'Unavailable' : `${exitQuality[t.id]!.metrics.mfe_capture_pct?.toFixed(1)}%`}</p>
                                  <p className="text-xs text-surface-400 mt-2">Initial-quantity opportunity proxy.</p>
                                </div>
                                <div className="bg-surface-800 p-5 rounded-lg border border-surface-700 shadow-md">
                                  <h4 className="text-sm font-semibold text-surface-300 mb-2">Exposure-aware giveback</h4>
                                  <div className="text-xl font-bold text-white">{formatR(exitQuality[t.id].metrics.exposure_aware_r_given_back)}</div>
                                  <p className="text-xs text-surface-400 mt-2">Peak trade P&amp;L: {formatR(exitQuality[t.id].metrics.exposure_peak_r)}</p>
                                </div>
                              </div>
                              <div className="rounded border border-surface-700 bg-surface-800 p-4 text-sm text-surface-300">
                                <strong className="text-white">Risk-constrained hold-N:</strong> {exitQuality[t.id].hold_n?.message || exitQuality[t.id].hold_n?.censor_reason || 'Unavailable'}
                                <p className="mt-2 text-xs">Status: {exitQuality[t.id].hold_n?.status || 'Unavailable'}. Forward research diagnostics are separate from the original decision.</p>
                              </div>
                              <details className="rounded border border-surface-700 p-4 text-xs text-surface-300"><summary className="cursor-pointer">Metrics, timing, units and research assumptions</summary><pre className="mt-3 overflow-x-auto whitespace-pre-wrap">{JSON.stringify(exitQuality[t.id], null, 2)}</pre></details>
                              </div>
                            ) : (
                              <div className="text-surface-400 text-sm py-12 text-center">{detailErrors[`${t.id}:quality`] || 'Loading quality metrics...'}</div>
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

        {/* Entry score outcome frequency; this is not a probability forecast. */}
        <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg transition-transform hover:-translate-y-1 duration-300">
          <h3 className="text-lg font-semibold text-white mb-6 flex items-center">
            <Activity className="mr-2 text-accent-light" size={20} /> Entry Score Outcome Frequency
          </h3>
          <div className="space-y-5">
            {calibration.map((c, idx) => (
              <div key={c.signal_score_bucket} className="flex flex-col space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-surface-300 font-medium">Entry score: {c.signal_score_bucket}</span>
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

      {qualityReport && (
        <div className="bg-surface-800 rounded-xl p-6 border border-surface-700 shadow-lg">
          <h3 className="text-lg font-semibold text-white mb-2">Exit Quality Coverage</h3>
          <p className="text-xs text-surface-400 mb-5">{qualityReport.research_label}</p>
          <div className="grid grid-cols-3 gap-4 text-sm">
            <div><span className="block text-surface-400">Eligible</span><span className="text-white text-xl">{qualityReport.records_eligible}</span></div>
            <div><span className="block text-surface-400">Excluded</span><span className="text-white text-xl">{qualityReport.records_excluded}</span></div>
            <div><span className="block text-surface-400">Avg net R</span><span className="text-white text-xl">{qualityReport.averages.captured_net_r == null ? 'Unavailable' : `${qualityReport.averages.captured_net_r.toFixed(2)}R`}</span></div>
          </div>
          {qualityReport.reason_distribution.length > 0 && <div className="mt-5 text-xs text-surface-300">Reason distribution: {qualityReport.reason_distribution.map(item => `${item.initiating_reason_code} → ${item.execution_outcome_code} (${item.count})`).join(', ')}</div>}
          <details className="mt-5 text-sm text-surface-300">
            <summary className="cursor-pointer">Metric denominators and cohort comparisons</summary>
            <table className="mt-3 w-full text-left text-xs"><thead><tr><th>Metric</th><th>Available / eligible</th><th>Mean</th></tr></thead><tbody>
              {Object.entries(qualityReport.coverage).map(([metric, coverage]) => <tr key={metric}><td className="py-1">{metric}</td><td>{coverage.available} / {coverage.eligible}</td><td>{qualityReport.averages[metric] == null ? 'Unavailable' : qualityReport.averages[metric]?.toFixed(3)}</td></tr>)}
            </tbody></table>
            <table className="mt-4 w-full text-left text-xs"><thead><tr><th>Dimension</th><th>Cohort</th><th>Trades</th><th>Average net R</th></tr></thead><tbody>
              {qualityReport.cohorts.map(cohort => <tr key={`${cohort.dimension}:${cohort.value}`}><td className="py-1">{cohort.dimension}</td><td>{cohort.value}</td><td>{cohort.count}</td><td>{formatR(cohort.average_net_r)}</td></tr>)}
            </tbody></table>
          </details>
          <details className="mt-4 text-xs text-surface-300">
            <summary className="cursor-pointer">Action distributions, censoring and quality diagnostics</summary>
            <pre className="mt-3 overflow-x-auto whitespace-pre-wrap">{JSON.stringify({ ...qualityReport, records: undefined }, null, 2)}</pre>
          </details>
        </div>
      )}

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

      {loadError && <p role="alert" className="mb-4 text-sm text-amber-200">{loadError}</p>}
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
