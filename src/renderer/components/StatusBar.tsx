import React from 'react';
import { useTradingStore } from '../stores/trading-store';

const StatusBar: React.FC = () => {
  const { auth, connectionStatus, agentState, dashboard } = useTradingStore();
  const pnl = dashboard?.totalPnl || 0;

  // Simple IST check
  const isMarketOpen = () => {
    const now = new Date();
    const istOffset = 5.5 * 60 * 60 * 1000;
    const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
    const istTime = new Date(utc + istOffset);
    const day = istTime.getDay();
    const hours = istTime.getHours();
    const minutes = istTime.getMinutes();
    const timeInMinutes = hours * 60 + minutes;
    if (day === 0 || day === 6) return false;
    return timeInMinutes >= 555 && timeInMinutes <= 930; // 9:15 to 15:30
  };

  const marketOpen = isMarketOpen();
  
  // If we are logged in, assume connected to Kite unless explicitly disconnected
  const isConnected = auth.isLoggedIn && connectionStatus !== 'disconnected';

  return (
    <div className="h-8 bg-surface-950 border-t border-surface-800 flex items-center justify-between px-4 text-xs font-mono">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-profit-light' : 'bg-loss-light'}`} />
          <span className="text-surface-300 capitalize">{isConnected ? 'connected' : 'disconnected'}</span>
        </div>
        <div className={`text-surface-400 ${marketOpen ? 'text-profit-light' : ''}`}>
          Market: {marketOpen ? 'OPEN' : 'CLOSED'}
        </div>
      </div>
      <div className="flex items-center gap-2 text-surface-400">
        Agent Status: <span className="text-white">
          {!agentState.running ? 'STOPPED' : agentState.status === 'idle' ? 'SCANNING' : agentState.status.toUpperCase()}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-surface-400">Today's P&L:</span>
        <span className={`font-bold ${pnl >= 0 ? 'text-profit-light' : 'text-loss-light'}`}>
          ₹{pnl.toFixed(2)}
        </span>
      </div>
    </div>
  );
};

export default StatusBar;
