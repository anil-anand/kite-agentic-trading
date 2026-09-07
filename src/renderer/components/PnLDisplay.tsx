import React from 'react';

interface Props {
  amount: number;
  netAmount?: number;
  percentage?: number;
}

const PnLDisplay: React.FC<Props> = ({ amount, netAmount, percentage }) => {
  const isProfit = (netAmount !== undefined ? netAmount : amount) >= 0;
  
  return (
    <div className={`flex flex-col items-center justify-center p-6 rounded-xl border border-surface-700 bg-surface-800 ${isProfit ? 'shadow-[0_0_15px_rgba(16,185,129,0.1)]' : 'shadow-[0_0_15px_rgba(244,63,94,0.1)]'}`}>
      <span className="text-surface-400 text-sm font-medium mb-1">Net P&L</span>
      <div className={`text-4xl font-mono font-bold animate-count-up ${isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
        {isProfit ? '+' : ''}₹{Math.abs(netAmount !== undefined ? netAmount : amount).toFixed(2)}
      </div>
      <div className="flex flex-row space-x-4 mt-2">
        {netAmount !== undefined && (
          <div className="text-xs font-mono text-surface-400">
            Gross: <span className={amount >= 0 ? 'text-profit-light' : 'text-loss-light'}>{amount >= 0 ? '+' : ''}₹{Math.abs(amount).toFixed(2)}</span>
          </div>
        )}
        {percentage !== undefined && (
          <div className={`text-xs font-mono ${isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
            {isProfit ? '+' : ''}{percentage.toFixed(2)}%
          </div>
        )}
      </div>
    </div>
  );
};

export default PnLDisplay;
