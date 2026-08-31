import React from 'react';
import { Signal } from '@shared/types';
import { Check, X } from 'lucide-react';

interface Props {
  signal: Signal;
  onTakeTrade: (signal: Signal) => void;
  onDismiss: (id: string) => void;
  compact?: boolean;
}

const SignalCard: React.FC<Props> = ({ signal, onTakeTrade, onDismiss, compact = false }) => {
  const isBuy = signal.direction === 'BUY';

  return (
    <div className="bg-surface-800 p-4 rounded-lg border border-surface-700 flex flex-col gap-3">
      {compact ? (
        <div className="flex justify-between items-start">
          <span className="font-bold text-white text-sm">{signal.strategy}</span>
        </div>
      ) : (
        <div className="flex justify-between items-start">
          <div className="flex gap-2 items-center">
            <span className={`px-2 py-1 text-xs font-bold rounded ${isBuy ? 'bg-profit-fade text-profit-light' : 'bg-loss-fade text-loss-light'}`}>
              {signal.direction}
            </span>
            <span className="font-bold text-white">{signal.tradingsymbol}</span>
          </div>
          <div className="text-xs text-surface-400 bg-surface-700 px-2 py-1 rounded">
            {signal.strategy}
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 text-sm">
        <div>
          <span className="text-surface-400 block text-xs">Entry</span>
          <span className="font-mono">₹{signal.entryPrice}</span>
        </div>
        <div>
          <span className="text-surface-400 block text-xs">Target</span>
          <span className="font-mono text-profit-light">₹{signal.target}</span>
        </div>
        <div>
          <span className="text-surface-400 block text-xs">SL</span>
          <span className="font-mono text-loss-light">₹{signal.stopLoss}</span>
        </div>
      </div>

      <div className="text-xs text-surface-300 line-clamp-2" title={signal.reasoning}>
        {signal.reasoning}
      </div>
      
      <div className="flex items-center gap-2 mt-2">
        <div className="flex-1 bg-surface-700 h-2 rounded-full overflow-hidden">
          <div className="h-full bg-accent-light" style={{ width: `${signal.confidence}%` }}></div>
        </div>
        <span className="text-xs font-mono text-surface-400">{signal.confidence}%</span>
      </div>

      <div className="flex gap-2 mt-2 pt-3 border-t border-surface-700">
        <button onClick={() => onTakeTrade(signal)} className="flex-1 bg-profit-dark hover:bg-profit flex items-center justify-center gap-2 py-2 rounded transition-colors text-white text-sm font-medium">
          <Check size={16} /> Take Trade
        </button>
        <button onClick={() => onDismiss(signal.id)} className="flex-1 bg-surface-700 hover:bg-surface-600 flex items-center justify-center gap-2 py-2 rounded transition-colors text-white text-sm font-medium">
          <X size={16} /> Dismiss
        </button>
      </div>
    </div>
  );
};

export default SignalCard;
