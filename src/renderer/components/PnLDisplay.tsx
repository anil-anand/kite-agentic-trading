import React from 'react';

interface Props {
  amount: number;
  percentage?: number;
}

const PnLDisplay: React.FC<Props> = ({ amount, percentage }) => {
  const isProfit = amount >= 0;
  
  return (
    <div className={`flex flex-col items-center justify-center p-6 rounded-xl border border-surface-700 bg-surface-800 ${isProfit ? 'shadow-[0_0_15px_rgba(16,185,129,0.1)]' : 'shadow-[0_0_15px_rgba(244,63,94,0.1)]'}`}>
      <span className="text-surface-400 text-sm font-medium mb-1">Total P&L</span>
      <div className={`text-4xl font-mono font-bold animate-count-up ${isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
        {isProfit ? '+' : ''}₹{Math.abs(amount).toFixed(2)}
      </div>
      {percentage !== undefined && (
        <div className={`text-sm mt-2 font-mono ${isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
          {isProfit ? '+' : ''}{percentage.toFixed(2)}%
        </div>
      )}
    </div>
  );
};

export default PnLDisplay;
