import React, { useEffect, useRef, useState } from 'react';
import { createChart } from 'lightweight-charts';
import { Search } from 'lucide-react';

const Chart: React.FC = () => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [showResults, setShowResults] = useState(false);

  useEffect(() => {
    const delayDebounceFn = setTimeout(async () => {
      if (searchQuery.length >= 2) {
        try {
          const results = await window.electronAPI?.invoke('market:search', searchQuery);
          if (results) {
            setSearchResults(results);
            setShowResults(true);
          }
        } catch (e) {
          console.error('Search failed', e);
        }
      } else {
        setSearchResults([]);
        setShowResults(false);
      }
    }, 300);

    return () => clearTimeout(delayDebounceFn);
  }, [searchQuery]);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: '#0f1729' },
        textColor: '#94a3c0',
      },
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      width: chartContainerRef.current.clientWidth,
      height: chartContainerRef.current.clientHeight,
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#f43f5e',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#f43f5e',
    });

    const data = [
      { time: '2023-01-01', open: 100, high: 105, low: 90, close: 95 },
      { time: '2023-01-02', open: 95, high: 110, low: 90, close: 105 },
      { time: '2023-01-03', open: 105, high: 115, low: 100, close: 110 },
      { time: '2023-01-04', open: 110, high: 120, low: 105, close: 115 },
      { time: '2023-01-05', open: 115, high: 125, low: 110, close: 120 },
    ];
    candleSeries.setData(data as any);

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth, height: chartContainerRef.current.clientHeight });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
    };
  }, []);

  const handleSelectInstrument = (instrument: any) => {
    console.log("Selected instrument", instrument);
    setSearchQuery(instrument.tradingsymbol);
    setShowResults(false);
    // Ideally fetch historical data here and update chart data
  };

  return (
    <div className="h-full flex flex-col relative">
      <div className="p-4 border-b border-surface-800 bg-surface-900 flex items-center justify-between">
        <div className="relative w-64 z-10">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-surface-400" size={16} />
          <input 
            type="text" 
            placeholder="Search instrument..." 
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onFocus={() => { if (searchResults.length > 0) setShowResults(true); }}
            className="w-full bg-surface-800 border border-surface-700 rounded pl-10 pr-4 py-2 text-white focus:outline-none focus:border-accent-light text-sm" 
          />
          {showResults && searchResults.length > 0 && (
            <div className="absolute top-full mt-1 w-full max-h-64 overflow-y-auto bg-surface-800 border border-surface-700 rounded shadow-lg">
              {searchResults.map((res: any) => (
                <div 
                  key={res.instrument_token} 
                  className="px-4 py-2 hover:bg-surface-700 cursor-pointer border-b border-surface-700/50 last:border-0"
                  onClick={() => handleSelectInstrument(res)}
                >
                  <div className="text-white font-semibold text-sm">{res.tradingsymbol}</div>
                  <div className="text-surface-400 text-xs">{res.name || res.exchange}</div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="flex bg-surface-800 rounded p-1">
          {['1m', '5m', '15m', '1h', '1D'].map((tf, i) => (
            <button key={tf} className={`px-3 py-1 text-sm rounded ${i === 1 ? 'bg-surface-600 text-white' : 'text-surface-400 hover:text-white'}`}>
              {tf}
            </button>
          ))}
        </div>
      </div>
      <div ref={chartContainerRef} className="flex-1 bg-surface-900" />
    </div>
  );
};

export default Chart;
