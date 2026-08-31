import React, { useState } from 'react';
import { useTradingStore } from '../stores/trading-store';
import { Info, AlertTriangle, AlertCircle, CheckCircle, Zap } from 'lucide-react';

const ActivityLog: React.FC = () => {
  const { activityLog } = useTradingStore();
  const [filter, setFilter] = useState<string>('all');

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

  const filteredLogs = filter === 'all' ? activityLog : activityLog.filter(l => l.level === filter);

  return (
    <div className="p-6 h-full flex flex-col space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Activity Log</h1>
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
          <button className="px-3 py-1 rounded text-sm bg-surface-800 text-surface-400 hover:text-white ml-4 border border-surface-700">Clear</button>
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
                <div key={log.id || index} className="flex gap-4 p-2 hover:bg-surface-700/50 rounded transition-colors text-sm items-start">
                  <div className="text-surface-500 font-mono whitespace-nowrap mt-0.5">
                    {new Date(timestamp).toLocaleTimeString()}
                  </div>
                  <div className="mt-0.5">{getIcon(level)}</div>
                  <div className="text-surface-200 flex-1">
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
