import React from 'react';
import { Position } from '@shared/types';
import { XCircle } from 'lucide-react';

interface Props {
  position: Position;
  onExit: (position: Position) => void;
  exitPending?: boolean;
}

const PositionCard: React.FC<Props> = ({ position, onExit, exitPending = false }) => {
  const isProfit = position.pnl != null && position.pnl >= 0;
  
  return (
    <div className="bg-surface-800 rounded-lg p-4 border border-surface-700 flex flex-col gap-3 transition-transform hover:-translate-y-1">
      <div className="flex justify-between items-start">
        <div>
          <h3 className="font-bold text-white text-lg">{position.tradingsymbol}</h3>
          <span className="text-xs bg-surface-700 px-2 py-1 rounded text-surface-300">{position.exchange}</span>
        </div>
        <button
          onClick={() => onExit(position)}
          disabled={exitPending || !position.positionKey}
          className="text-loss-light hover:text-loss-dark transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          title={position.positionKey ? 'Exit Position' : 'Position identity unavailable'}
        >
          <XCircle size={20} />
        </button>
      </div>
      {exitPending && <p className="text-xs text-amber-200">Close pending broker reconciliation…</p>}
      <div className="grid grid-cols-3 gap-2 text-sm">
        <div>
          <div className="text-surface-400">Qty</div>
          <div className="font-mono text-white">{position.quantity}</div>
        </div>
        <div>
          <div className="text-surface-400">Avg</div>
          <div className="font-mono text-white">{position.averagePrice != null ? `₹${position.averagePrice.toFixed(2)}` : 'Unavailable'}</div>
        </div>
        <div>
          <div className="text-surface-400">LTP</div>
          <div className="font-mono text-white">{position.lastPrice != null ? `₹${position.lastPrice.toFixed(2)}` : 'Unavailable'}</div>
        </div>
      </div>
      <div className="mt-2 pt-2 border-t border-surface-700 flex justify-between items-center">
        <span className="text-surface-400 text-sm">P&L</span>
        <div className={`font-mono font-bold ${position.pnl == null ? 'text-surface-300' : isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
          {position.pnl == null ? 'Unavailable' : `${isProfit ? '+' : ''}₹${position.pnl.toFixed(2)}`}
        </div>
      </div>
    </div>
  );
};

export default PositionCard;
