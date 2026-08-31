import React from 'react';
import { useTradingStore } from '../stores/trading-store';

const Settings: React.FC = () => {
  return (
    <div className="p-6 h-full overflow-auto max-w-4xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-white mb-6">Settings</h1>
        
        <div className="space-y-6">
          <section className="bg-surface-800 p-6 rounded-xl border border-surface-700">
            <h2 className="text-lg font-semibold text-white mb-4">API Credentials</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-surface-400 text-sm mb-1">Kite API Key</label>
                <input type="password" value="••••••••••••••••" readOnly className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white" />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Kite API Secret</label>
                <input type="password" value="••••••••••••••••" readOnly className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white" />
              </div>
              <button className="bg-surface-700 hover:bg-surface-600 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors">Edit Credentials</button>
            </div>
          </section>

          <section className="bg-surface-800 p-6 rounded-xl border border-surface-700">
            <h2 className="text-lg font-semibold text-white mb-4">Risk Management</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-surface-400 text-sm mb-1">Max Capital Per Trade (₹)</label>
                <input type="number" defaultValue={10000} className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Max Daily Loss (₹)</label>
                <input type="number" defaultValue={2000} className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Default Stop Loss (%)</label>
                <input type="number" defaultValue={1.5} step="0.1" className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Default Target (%)</label>
                <input type="number" defaultValue={3.0} step="0.1" className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" />
              </div>
            </div>
          </section>

          <section className="bg-surface-800 p-6 rounded-xl border border-surface-700">
            <h2 className="text-lg font-semibold text-white mb-4">Notifications</h2>
            <div className="space-y-3">
              {['Sound Enabled', 'Desktop Notifications', 'Notify on Signal', 'Notify on Order Execution'].map((opt, i) => (
                <label key={i} className="flex items-center gap-3 cursor-pointer">
                  <input type="checkbox" defaultChecked className="w-4 h-4 rounded border-surface-600 text-accent-light focus:ring-accent-light bg-surface-900" />
                  <span className="text-surface-200 text-sm">{opt}</span>
                </label>
              ))}
            </div>
          </section>
        </div>
      </div>
      
      <div className="flex justify-end gap-4 pb-8">
        <button className="px-6 py-2 rounded-lg text-surface-300 hover:text-white transition-colors">Reset to Defaults</button>
        <button className="px-6 py-2 bg-accent-dark hover:bg-accent rounded-lg text-white font-medium transition-colors">Save Changes</button>
      </div>
    </div>
  );
};

export default Settings;
