import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Sidebar from './components/Sidebar';
import StatusBar from './components/StatusBar';
import Dashboard from './pages/Dashboard';
import AgentControl from './pages/AgentControl';
import Chart from './pages/Chart';
import Orders from './pages/Orders';
import Watchlist from './pages/Watchlist';
import ActivityLog from './pages/ActivityLog';
import Settings from './pages/Settings';
import LoginModal from './components/LoginModal';
import { useTradingStore } from './stores/trading-store';
import { useKiteAPI } from './hooks/useKiteAPI';

const App: React.FC = () => {
  useKiteAPI();
  const auth = useTradingStore(state => state.auth);

  return (
    <div className="flex h-screen overflow-hidden bg-surface-950">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <main className="flex-1 overflow-auto bg-surface-900 relative">
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/agent" element={<AgentControl />} />
            <Route path="/chart" element={<Chart />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/activity" element={<ActivityLog />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
          {!auth.isLoggedIn && <LoginModal />}
        </main>
        <StatusBar />
      </div>
    </div>
  );
};

export default App;
