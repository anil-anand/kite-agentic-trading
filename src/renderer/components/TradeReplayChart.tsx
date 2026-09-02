import React, { useEffect, useRef } from 'react';
import { createChart, ColorType, IChartApi, ISeriesApi, Time } from 'lightweight-charts';
import { Candle, JournalTrade } from '../../shared/types';

interface TradeReplayChartProps {
  candles: Candle[];
  trade: JournalTrade;
}

const TradeReplayChart: React.FC<TradeReplayChartProps> = ({ candles, trade }) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const handleResize = () => {
      chartRef.current?.applyOptions({
        width: chartContainerRef.current?.clientWidth,
      });
    };

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#9CA3AF',
      },
      grid: {
        vertLines: { color: 'rgba(55, 65, 81, 0.5)' },
        horzLines: { color: 'rgba(55, 65, 81, 0.5)' },
      },
      width: chartContainerRef.current.clientWidth,
      height: 400,
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        mode: 1, // Normal mode
      },
    });

    chartRef.current = chart;

    const candlestickSeries = chart.addCandlestickSeries({
      upColor: '#10B981',
      downColor: '#EF4444',
      borderVisible: false,
      wickUpColor: '#10B981',
      wickDownColor: '#EF4444',
    });
    
    seriesRef.current = candlestickSeries;

    // Convert API candles to lightweight-charts format
    const formattedData = candles.map(c => ({
      time: c.time as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    })).sort((a, b) => (a.time as number) - (b.time as number));

    candlestickSeries.setData(formattedData);

    // Add markers for entry and exit
    const markers: any[] = [];
    
    if (trade.entry_time) {
      const entryTime = Math.floor(new Date(trade.entry_time).getTime() / 1000);
      markers.push({
        time: entryTime as Time,
        position: trade.direction === 'BUY' ? 'belowBar' : 'aboveBar',
        color: trade.direction === 'BUY' ? '#10B981' : '#EF4444',
        shape: trade.direction === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: `Entry: ${trade.direction} @ ₹${trade.entry_price}`,
      });
    }

    if (trade.exit_time && trade.exit_price) {
      const exitTime = Math.floor(new Date(trade.exit_time).getTime() / 1000);
      markers.push({
        time: exitTime as Time,
        position: trade.direction === 'BUY' ? 'aboveBar' : 'belowBar',
        color: '#F59E0B',
        shape: trade.direction === 'BUY' ? 'arrowDown' : 'arrowUp',
        text: `Exit @ ₹${trade.exit_price}`,
      });
    }

    // Sort markers by time
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    candlestickSeries.setMarkers(markers);

    chart.timeScale().fitContent();

    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
    };
  }, [candles, trade]);

  return (
    <div className="w-full bg-surface-900 rounded-lg overflow-hidden border border-surface-700 p-2">
      <div ref={chartContainerRef} className="w-full" />
    </div>
  );
};

export default TradeReplayChart;
