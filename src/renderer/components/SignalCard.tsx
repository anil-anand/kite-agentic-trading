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
      
      <div className="flex flex-col gap-1 mt-2">
        <div className="flex items-center justify-between">
          <span className="text-xs text-surface-400">Signal Score</span>
          <span className="text-xs font-mono text-surface-400">{signal.signal_score ?? (signal as any).signalScore ?? 0}/100</span>
        </div>
        <div className="h-1 bg-surface-700 rounded-full overflow-hidden mt-1">
          <div className="h-full bg-surface-500" style={{ width: `${signal.signal_score ?? (signal as any).signalScore ?? 0}%` }}></div>
        </div>
        
        {(signal.estimated_probability != null || (signal as any).estimatedProbability != null) && (
          <div className="flex items-center justify-between mt-1 p-1.5 bg-accent-dark/20 rounded border border-accent-dark/30">
            <span className="text-xs text-accent-light font-medium">Calibrated Prob</span>
            <span className="text-xs font-mono text-accent-light">{((signal.estimated_probability ?? (signal as any).estimatedProbability) * 100).toFixed(1)}% <span className="text-[10px] text-surface-400 opacity-80">(n={signal.calibration_sample_size ?? (signal as any).calibrationSampleSize})</span></span>
          </div>
        )}
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
