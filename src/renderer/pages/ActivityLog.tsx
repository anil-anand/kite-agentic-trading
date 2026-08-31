import React, { useState } from 'react';
import { useTradingStore } from '../stores/trading-store';
import { Info, AlertTriangle, AlertCircle, CheckCircle, Zap } from 'lucide-react';

const ActivityLog: React.FC = () => {
  const { activityLog } = useTradingStore();
  const [filter, setFilter] = useState<string>('all');
  
  // Format local date YYYY-MM-DD reliably
  const getLocalDateStr = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  
  const [selectedDate, setSelectedDate] = useState<string>(getLocalDateStr(new Date()));

  const getIcon = (level: string) => {
    switch (level) {
      case 'info': return <Info size={16} className="text-blue-400" />;
      case 'warning': return <AlertTriangle size={16} className="text-warning-light" />;
      case 'error': return <AlertCircle size={16} className="text-loss-light" />;
      case 'success': return <CheckCircle size={16} className="text-profit-light" />;
      case 'signal': return <Zap size={16} className="text-accent-light" />;
      case 'order': return <CheckCircle size={16} className="text-profit-light" />;
      default: return <Info size={16} className="text-surface-400" />;
    }
  };

  const availableDates = Array.from(new Set(activityLog.map(l => getLocalDateStr(new Date(l.timestamp || new Date()))))).sort((a, b) => b.localeCompare(a));
  
  // Ensure the current date is in the list even if there are no logs yet today
  const todayStr = getLocalDateStr(new Date());
  if (!availableDates.includes(todayStr)) {
    availableDates.unshift(todayStr);
  }

  const dateFilteredLogs = selectedDate === 'all' 
    ? activityLog 
    : activityLog.filter(l => getLocalDateStr(new Date(l.timestamp || new Date())) === selectedDate);

  const filteredLogs = filter === 'all' ? dateFilteredLogs : dateFilteredLogs.filter(l => l.level === filter);

  return (
    <div className="p-6 h-full flex flex-col space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white flex items-center gap-4">
          Activity Log
          <select 
            value={selectedDate} 
            onChange={(e) => setSelectedDate(e.target.value)}
            className="text-sm bg-surface-800 border border-surface-700 text-surface-200 rounded px-2 py-1 outline-none focus:border-accent-light"
          >
            <option value="all">All Time</option>
            {availableDates.map(d => (
              <option key={d} value={d}>
                {d === todayStr ? `Today (${d})` : d}
              </option>
            ))}
          </select>
        </h1>
        <div className="flex gap-2">
          {['all', 'info', 'signal', 'order', 'error'].map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1 rounded text-sm capitalize ${filter === f ? 'bg-surface-700 text-white' : 'bg-surface-800 text-surface-400 hover:text-white'}`}
            >
              {f}
            </button>
          ))}
          <button 
            onClick={() => useTradingStore.getState().setActivityLog([])}
            className="px-3 py-1 rounded text-sm bg-surface-800 text-surface-400 hover:text-white ml-4 border border-surface-700 hover:border-loss-light transition-colors"
          >
            Clear
          </button>
        </div>
      </div>

      <div className="flex-1 bg-surface-800 rounded-xl border border-surface-700 overflow-auto p-4">
        <div className="space-y-1">
          {filteredLogs.length === 0 ? (
            <div className="text-center py-8 text-surface-400">No logs to display.</div>
          ) : (
            filteredLogs.map((log, index) => {
              const level = log.level || 'info';
              const timestamp = log.timestamp || new Date().toISOString();
              const message = log.message || '';
              return (
                <div key={log.id || index} className="flex gap-4 p-2 hover:bg-surface-700/50 rounded transition-colors text-sm items-start border-b border-surface-700/50 last:border-0">
                  <div className="text-surface-500 font-mono whitespace-nowrap flex flex-col text-xs mt-0.5 min-w-[90px]">
                    <span className="text-surface-400">{new Date(timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>
                    <span>{new Date(timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                  </div>
                  <div className="mt-1">{getIcon(level)}</div>
                  <div className="text-surface-200 flex-1 mt-0.5">
                    <span className="font-semibold text-white mr-2">[{level.toUpperCase()}]</span>
                    {message}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default ActivityLog;
