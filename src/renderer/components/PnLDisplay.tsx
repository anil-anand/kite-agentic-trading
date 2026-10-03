import React from 'react';
import Skeleton from './Skeleton';

interface Props {
  loading?: boolean;
  amount: number | null;
  netAmount?: number | null;
  percentage?: number;
}

const PnLDisplay: React.FC<Props> = ({ amount, netAmount, percentage, loading = false }) => {
  const value = netAmount != null && Number.isFinite(netAmount) ? netAmount : null;
  const isProfit = value != null && value >= 0;
  
  return (
    <div aria-busy={loading} className={`flex flex-col items-center justify-center p-6 rounded-xl border border-surface-700 bg-surface-800 ${value == null ? '' : isProfit ? 'shadow-[0_0_15px_rgba(16,185,129,0.1)]' : 'shadow-[0_0_15px_rgba(244,63,94,0.1)]'}`}>
      <span className="text-surface-400 text-sm font-medium mb-1">Net P&L</span>
      {loading ? (
        <>
          <Skeleton className="h-10 w-40 max-w-full" />
          <Skeleton className="mt-2 h-4 w-28 max-w-full" />
          <span className="sr-only">Loading net P&amp;L…</span>
        </>
      ) : (
        <>
          <div className={`text-4xl font-mono font-bold animate-count-up ${value == null ? 'text-surface-300' : isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
            {value == null ? 'Unavailable' : `${isProfit ? '+' : '-'}₹${Math.abs(value).toFixed(2)}`}
          </div>
          <div className="flex flex-row space-x-4 mt-2">
            {netAmount == null && (
              <div className="text-xs font-mono text-surface-400">Net P&amp;L: Unavailable</div>
            )}
            {amount != null && (
              <div className="text-xs font-mono text-surface-400">
                Gross P&amp;L: <span className={amount >= 0 ? 'text-profit-light' : 'text-loss-light'}>{amount >= 0 ? '+' : '-'}₹{Math.abs(amount).toFixed(2)}</span>
              </div>
            )}
            {percentage !== undefined && netAmount != null && (
              <div className={`text-xs font-mono ${isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
                {isProfit ? '+' : ''}{percentage.toFixed(2)}%
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default PnLDisplay;
