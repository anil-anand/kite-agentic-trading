import React, { useState } from 'react';
import { Play, TrendingUp, DollarSign, Activity, ListOrdered } from 'lucide-react';

const metricValue = (value: unknown, unit = '') => typeof value !== 'number' || !Number.isFinite(value) ? 'Unavailable' : `${value.toFixed(2)}${unit}`;
const currencyValue = (value: unknown) => typeof value !== 'number' || !Number.isFinite(value) ? 'Unavailable' : `${value < 0 ? '-' : ''}₹${Math.abs(value).toFixed(2)}`;

// Hardcoded strategies list for now (matching backend scanner.py mapping)
const STRATEGIES = [
  { id: 'ema_crossover', name: 'EMA Crossover' },
  { id: 'rsi_reversal', name: 'RSI Reversal' },
  { id: 'vwap_bounce', name: 'VWAP Bounce' },
  { id: 'supertrend', name: 'Supertrend' },
  { id: 'macd_cross', name: 'MACD Cross' },
  { id: 'bollinger_breakout', name: 'Bollinger Breakout' },
  { id: 'stochastic_reversal', name: 'Stochastic Reversal' },
];

const Backtesting: React.FC = () => {
  const [symbol, setSymbol] = useState('RELIANCE');
  const [strategyId, setStrategyId] = useState('ema_crossover');
  const [days, setDays] = useState(30);
  const [initialCapital, setInitialCapital] = useState(100000);
  
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const runBacktest = async () => {
    setLoading(true);
    setError(null);
    setResults(null);
    
    try {
      // Use the newly exposed electronAPI method
      // @ts-expect-error electronAPI type not fully defined
      const res = await window.electronAPI.backtest.run({
        symbol,
        strategy_id: strategyId,
        days: Number(days),
        initial_capital: Number(initialCapital)
      });
      // electronAPI invokes throw an exception on error, so we don't check res.error here
      // The return value IS the payload
      setResults(res);
    } catch (err: any) {
      setError(err.message || 'Failed to run backtest');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-full overflow-hidden p-6 gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-surface-50 flex items-center gap-2">
            <Activity className="w-6 h-6 text-primary-500" />
            Backtesting Lab
          </h1>
          <p className="text-sm text-surface-400 mt-1">
            Raw-strategy research lab with declared simulation assumptions; it is not live-policy parity.
          </p>
        </div>
      </div>

      <div className="bg-surface-800 border border-surface-700 rounded-lg p-5 flex flex-wrap gap-4 items-end">
        <div className="flex-1 min-w-[200px]">
          <label className="block text-xs font-medium text-surface-400 mb-1">Strategy</label>
          <select 
            className="w-full bg-surface-900 border border-surface-700 rounded-md px-3 py-2 text-surface-50 focus:outline-none focus:ring-1 focus:ring-primary-500"
            value={strategyId}
            onChange={e => setStrategyId(e.target.value)}
          >
            {STRATEGIES.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>
        
        <div className="flex-1 min-w-[150px]">
          <label className="block text-xs font-medium text-surface-400 mb-1">Symbol</label>
          <input 
            type="text" 
            className="w-full bg-surface-900 border border-surface-700 rounded-md px-3 py-2 text-surface-50 focus:outline-none focus:ring-1 focus:ring-primary-500"
            value={symbol}
            onChange={e => setSymbol(e.target.value.toUpperCase())}
            placeholder="e.g. RELIANCE"
          />
        </div>

        <div className="w-32">
          <label className="block text-xs font-medium text-surface-400 mb-1">Days History</label>
          <input 
            type="number" 
            className="w-full bg-surface-900 border border-surface-700 rounded-md px-3 py-2 text-surface-50 focus:outline-none focus:ring-1 focus:ring-primary-500"
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            min={1}
            max={100}
          />
        </div>

        <div className="w-40">
          <label className="block text-xs font-medium text-surface-400 mb-1">Capital (₹)</label>
          <input 
            type="number" 
            className="w-full bg-surface-900 border border-surface-700 rounded-md px-3 py-2 text-surface-50 focus:outline-none focus:ring-1 focus:ring-primary-500"
            value={initialCapital}
            onChange={e => setInitialCapital(Number(e.target.value))}
            min={1000}
          />
        </div>

        <button 
          onClick={runBacktest}
          disabled={loading}
          className={`flex items-center gap-2 px-6 py-2 rounded-md font-medium transition-colors ${
            loading 
              ? 'bg-surface-700 text-surface-400 cursor-not-allowed' 
              : 'bg-primary-600 hover:bg-primary-500 text-white'
          }`}
        >
          {loading ? (
            <div className="w-5 h-5 border-2 border-white/20 border-t-white rounded-full animate-spin" />
          ) : (
            <Play className="w-4 h-4" />
          )}
          {loading ? 'Running...' : 'Run Backtest'}
        </button>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 rounded-lg p-4">
          {error}
        </div>
      )}

      {results && results.metrics && (
        <div className="flex-1 overflow-auto flex flex-col gap-6">
          <p className="rounded border border-amber-700/50 bg-amber-950/20 p-3 text-xs text-amber-200">Metrics basis: {results.metrics_basis || results.metrics.metrics_basis || 'Unavailable'}. Candidate exit studies require retained replay artifacts and are reported separately.</p>
          <div className="grid grid-cols-4 gap-4">
            <MetricCard title="Net PnL" value={currencyValue(results.metrics.net_profit)} icon={<DollarSign className="w-5 h-5" />} trend={results.metrics.net_profit == null ? undefined : results.metrics.net_profit >= 0 ? 'up' : 'down'} />
            <MetricCard title="Win Rate" value={metricValue(results.metrics.win_rate == null ? null : results.metrics.win_rate * 100, '%')} icon={<TrendingUp className="w-5 h-5" />} />
            <MetricCard title="Total Trades" value={results.metrics.trade_count} icon={<ListOrdered className="w-5 h-5" />} />
            <MetricCard title="Expectancy" value={currencyValue(results.metrics.expectancy)} icon={<Activity className="w-5 h-5" />} />
            <MetricCard title="Profit Factor" value={metricValue(results.metrics.profit_factor)} icon={<Activity className="w-5 h-5" />} />
            <MetricCard title="Portfolio MTM Drawdown" value={currencyValue(results.metrics.max_drawdown)} icon={<TrendingUp className="w-5 h-5 text-red-400" />} />
            <MetricCard title="Avg R-Multiple" value={metricValue(results.metrics.avg_r, ' R')} icon={<Activity className="w-5 h-5" />} />
            <MetricCard title="Fees & Taxes" value={currencyValue(results.metrics.total_fees_paid)} icon={<DollarSign className="w-5 h-5" />} />
            <MetricCard title="Completed-trade Drawdown" value={currencyValue(results.metrics.completed_trade_drawdown)} icon={<TrendingUp className="w-5 h-5" />} />
          </div>

          <div className="bg-surface-800 border border-surface-700 rounded-lg flex-1 flex flex-col overflow-hidden">
            <div className="p-4 border-b border-surface-700">
              <h2 className="text-lg font-medium text-surface-50">Trade Log</h2>
            </div>
            <div className="flex-1 overflow-auto">
              <table className="w-full text-left text-sm text-surface-300">
                <thead className="text-xs text-surface-400 uppercase bg-surface-900 sticky top-0">
                  <tr>
                    <th className="px-4 py-3">Entry Time</th>
                    <th className="px-4 py-3">Dir</th>
                    <th className="px-4 py-3 text-right">Entry</th>
                    <th className="px-4 py-3 text-right">Exit</th>
                    <th className="px-4 py-3 text-right">Qty</th>
                    <th className="px-4 py-3 text-right">PnL</th>
                    <th className="px-4 py-3 text-right">Fees</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-700">
                  {results.trades.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="px-4 py-8 text-center text-surface-500">
                        No trades generated in this period.
                      </td>
                    </tr>
                  ) : (
                    results.trades.map((t: any, i: number) => (
                      <tr key={i} className="hover:bg-surface-700/50">
                        <td className="px-4 py-3">{new Date(t.entry_time).toLocaleString()}</td>
                        <td className="px-4 py-3">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${t.direction === 'BUY' ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                            {t.direction}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right text-surface-100 font-mono">₹{t.entry_price.toFixed(2)}</td>
                        <td className="px-4 py-3 text-right text-surface-100 font-mono">₹{t.exit_price.toFixed(2)}</td>
                        <td className="px-4 py-3 text-right font-mono">{t.quantity}</td>
                        <td className={`px-4 py-3 text-right font-mono font-medium ${t.net_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {t.net_pnl >= 0 ? '+' : ''}{t.net_pnl.toFixed(2)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-surface-400">₹{t.total_fees.toFixed(2)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const MetricCard = ({ title, value, icon, trend }: { title: string, value: string | number, icon?: React.ReactNode, trend?: 'up' | 'down' }) => (
  <div className="bg-surface-800 border border-surface-700 rounded-lg p-4 flex flex-col">
    <div className="flex items-center text-surface-400 mb-2 gap-2">
      {icon}
      <span className="text-xs font-medium uppercase tracking-wider">{title}</span>
    </div>
    <div className={`text-2xl font-semibold ${trend === 'up' ? 'text-green-400' : trend === 'down' ? 'text-red-400' : 'text-surface-50'}`}>
      {value}
    </div>
  </div>
);

export default Backtesting;
