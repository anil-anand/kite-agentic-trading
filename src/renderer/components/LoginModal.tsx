import React, { useState } from 'react';
import { useKiteAPI } from '../hooks/useKiteAPI';
import { useTradingStore } from '../stores/trading-store';

const LoginModal: React.FC = () => {
  const { login } = useKiteAPI();
  const setAuth = useTradingStore(state => state.setAuth);
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const res = await login({ apiKey, apiSecret });
      if (!res) {
        setError('Fatal Error: `res` is undefined. IPC bridge (window.electronAPI) might be broken.');
      } else if (res.error) {
        setError(`Login failed: ${res.error}`);
      } else if (res.isLoggedIn) {
        setAuth(res);
      } else {
        setError(`Unknown response: ${JSON.stringify(res)}`);
      }
    } catch (err: any) {
      setError(`Caught Exception: ${err.message || 'Unknown error'}`);
    }
    setLoading(false);
  };

  return (
    <div className="absolute inset-0 bg-surface-950/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-surface-900 border border-surface-700 rounded-xl p-8 max-w-md w-full shadow-2xl animate-slide-up">
        <h2 className="text-2xl font-bold text-white mb-2 text-center">Connect to Kite</h2>
        <p className="text-surface-400 text-sm text-center mb-6">Enter your API credentials to start trading.</p>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-surface-300 text-sm font-medium mb-1">API Key</label>
            <input required type="text" value={apiKey} onChange={(e) => setApiKey(e.target.value)} className="w-full bg-surface-800 border border-surface-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-accent-light transition-colors" placeholder="kite_api_key" />
          </div>
          <div>
            <label className="block text-surface-300 text-sm font-medium mb-1">API Secret</label>
            <input required type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} className="w-full bg-surface-800 border border-surface-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-accent-light transition-colors" placeholder="••••••••••••••••" />
          </div>
          
          {error && <div className="text-loss-light text-sm bg-loss-fade p-3 rounded">{error}</div>}
          
          <button type="submit" disabled={loading} className="w-full bg-accent-dark hover:bg-accent py-3 rounded-lg text-white font-bold transition-colors mt-2">
            {loading ? 'Connecting...' : 'Connect & Login'}
          </button>
        </form>
        
        <div className="mt-6 text-center text-xs text-surface-500">
          <a href="https://developers.kite.trade/" target="_blank" rel="noreferrer" className="hover:text-accent-light underline transition-colors">Get your API credentials from Kite Connect</a>
        </div>
      </div>
    </div>
  );
};

export default LoginModal;
