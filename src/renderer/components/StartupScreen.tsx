import React from 'react';
import { Loader2 } from 'lucide-react';
import { useTradingStore } from '../stores/trading-store';

const StartupScreen: React.FC = () => {
  const startup = useTradingStore(state => state.startup);
  const failed = startup.status === 'error';

  return (
    <div className="flex h-screen items-center justify-center bg-surface-950 p-6">
      <div className="flex max-w-sm flex-col items-center text-center" role={failed ? 'alert' : 'status'}>
        <div className="mb-6 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent/10 text-accent-light">
          {failed ? <span className="text-2xl font-bold">!</span> : <Loader2 size={28} className="motion-safe:animate-spin" aria-hidden="true" />}
        </div>
        <h1 className="text-2xl font-bold text-white">Kite Agent</h1>
        <p className="mt-3 text-sm text-surface-300">
          {failed ? startup.error : startup.status === 'restoring-session' ? 'Restoring your session…' : 'Starting up…'}
        </p>
        {failed && (
          <button onClick={() => window.location.reload()} className="mt-6 rounded-lg bg-accent-dark px-5 py-2.5 font-medium text-white transition-colors hover:bg-accent">
            Retry
          </button>
        )}
      </div>
    </div>
  );
};

export default StartupScreen;
