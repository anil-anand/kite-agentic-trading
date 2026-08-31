import React from 'react';
import { useTradingStore } from '../stores/trading-store';
import { Search, X } from 'lucide-react';

const Watchlist: React.FC = () => {
  const { watchlist, ticks } = useTradingStore();

  return (
    <div className="p-6 h-full flex flex-col space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Watchlist</h1>
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-surface-400" size={16} />
          <input type="text" placeholder="Add instrument..." className="w-full bg-surface-800 border border-surface-700 rounded-lg pl-10 pr-4 py-2 text-white focus:outline-none focus:border-accent-light" />
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 overflow-auto">
        {watchlist.length === 0 ? (
          <div className="col-span-full py-12 text-center text-surface-400 border border-dashed border-surface-700 rounded-xl">
            Watchlist is empty. Search to add instruments.
          </div>
        ) : (
          watchlist.map(item => {
            const tick = ticks[item.tradingsymbol];
            const price = tick?.lastPrice || item.lastPrice;
            const change = tick?.changePercent || item.changePercent;
            const isPos = change >= 0;

            return (
              <div key={item.tradingsymbol} className="bg-surface-800 rounded-xl p-4 border border-surface-700 hover:border-surface-600 transition-colors group">
                <div className="flex justify-between items-start mb-2">
                  <h3 className="font-bold text-lg text-white">{item.tradingsymbol}</h3>
                  <button className="text-surface-500 hover:text-loss-light opacity-0 group-hover:opacity-100 transition-opacity">
                    <X size={16} />
                  </button>
                </div>
                <div className="flex items-end justify-between mt-4">
                  <div className="font-mono text-2xl font-bold text-white">₹{price.toFixed(2)}</div>
                  <div className={`font-mono text-sm ${isPos ? 'text-profit-light' : 'text-loss-light'}`}>
                    {isPos ? '+' : ''}{change.toFixed(2)}%
                  </div>
                </div>
                <div className="flex gap-2 mt-4 pt-4 border-t border-surface-700">
                  <button className="flex-1 bg-profit-dark hover:bg-profit py-1.5 rounded text-white text-xs font-bold transition-colors">B</button>
                  <button className="flex-1 bg-loss-dark hover:bg-loss py-1.5 rounded text-white text-xs font-bold transition-colors">S</button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default Watchlist;
