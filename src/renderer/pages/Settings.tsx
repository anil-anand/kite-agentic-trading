import React, { useState, useEffect } from 'react';
import { useTradingStore } from '../stores/trading-store';
import { SETTINGS_SAVE } from '@shared/ipc-channels';

const Settings: React.FC = () => {
  const { settings, setSettings } = useTradingStore();
  const [localSettings, setLocalSettings] = useState<any>(settings);
  const [saveStatus, setSaveStatus] = useState<string>('');

  // Sync local state when global settings load
  useEffect(() => {
    if (settings) {
      setLocalSettings(settings);
    }
  }, [settings]);

  if (!localSettings) {
    return <div className="p-6 text-white">Loading settings...</div>;
  }

  const handleRiskChange = (key: string, value: string | boolean) => {
    setLocalSettings((prev: any) => {
      let finalValue = value;
      if (typeof value === 'string' && !value.includes(':')) {
        finalValue = parseFloat(value) || 0;
      }
      return {
        ...prev,
        risk: {
          ...prev.risk,
          [key]: finalValue
        }
      };
    });
  };

  const saveChanges = async () => {
    try {
      setSaveStatus('Saving...');
      await window.electronAPI?.invoke(SETTINGS_SAVE, localSettings);
      setSettings(localSettings);
      setSaveStatus('Saved successfully!');
      setTimeout(() => setSaveStatus(''), 3000);
    } catch (error) {
      setSaveStatus('Error saving settings');
    }
  };

  const resetToDefaults = () => {
    setLocalSettings((prev: any) => ({
      ...prev,
      risk: {
        ...prev.risk,
        maxCapitalPerTrade: 10000,
        maxDailyLoss: 2000,
        maxSimultaneousPositions: 5,
        autoSquareOff: true,
        squareOffTime: "15:15",
        defaultStopLossPercent: 1.5,
        defaultTargetPercent: 3
      }
    }));
  };

  return (
    <div className="p-6 h-full overflow-auto max-w-4xl mx-auto space-y-8">
      <div>
        <div className="flex justify-between items-center mb-6">
          <h1 className="text-2xl font-bold text-white">Settings</h1>
          {saveStatus && (
            <span className={`text-sm ${saveStatus.includes('Error') ? 'text-loss-light' : 'text-profit-light'}`}>
              {saveStatus}
            </span>
          )}
        </div>
        
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
              <p className="text-xs text-surface-500">To update credentials, log out and enter them on the login screen.</p>
            </div>
          </section>

          <section className="bg-surface-800 p-6 rounded-xl border border-surface-700">
            <h2 className="text-lg font-semibold text-white mb-4">Risk Management</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-surface-400 text-sm mb-1">Max Capital Per Trade (₹)</label>
                <input 
                  type="number" 
                  value={localSettings.risk?.maxCapitalPerTrade || ''} 
                  onChange={(e) => handleRiskChange('maxCapitalPerTrade', e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Max Daily Loss (₹)</label>
                <input 
                  type="number" 
                  value={localSettings.risk?.maxDailyLoss || ''} 
                  onChange={(e) => handleRiskChange('maxDailyLoss', e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Auto Square Off</label>
                <div className="flex items-center h-10">
                  <label className="relative inline-flex items-center cursor-pointer">
                    <input 
                      type="checkbox" 
                      className="sr-only peer" 
                      checked={localSettings.risk?.autoSquareOff ?? true}
                      onChange={(e) => handleRiskChange('autoSquareOff', e.target.checked)}
                    />
                    <div className="w-11 h-6 bg-surface-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-accent-dark"></div>
                  </label>
                </div>
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Square Off Time</label>
                <input 
                  type="time" 
                  value={localSettings.risk?.squareOffTime || '15:15'} 
                  onChange={(e) => handleRiskChange('squareOffTime', e.target.value)}
                  disabled={!(localSettings.risk?.autoSquareOff ?? true)}
                  className={`w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none ${!(localSettings.risk?.autoSquareOff ?? true) ? 'opacity-50 cursor-not-allowed' : ''}`} 
                />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Max Simultaneous Positions</label>
                <input 
                  type="number" 
                  value={localSettings.risk?.maxSimultaneousPositions || ''} 
                  onChange={(e) => handleRiskChange('maxSimultaneousPositions', e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Default Stop Loss (%)</label>
                <input 
                  type="number" 
                  step="0.1"
                  value={localSettings.risk?.defaultStopLossPercent || ''} 
                  onChange={(e) => handleRiskChange('defaultStopLossPercent', e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Default Target (%)</label>
                <input 
                  type="number" 
                  step="0.1"
                  value={localSettings.risk?.defaultTargetPercent || ''} 
                  onChange={(e) => handleRiskChange('defaultTargetPercent', e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
            </div>
          </section>
        </div>
      </div>
      
      <div className="flex justify-end gap-4 pb-8">
        <button 
          onClick={resetToDefaults}
          className="px-6 py-2 bg-surface-700 hover:bg-surface-600 rounded-lg text-white font-medium transition-colors"
        >
          Reset to Defaults
        </button>
        <button 
          onClick={saveChanges}
          className="px-6 py-2 bg-accent-dark hover:bg-accent rounded-lg text-white font-medium transition-colors"
        >
          Save Changes
        </button>
      </div>
    </div>
  );
};

export default Settings;
