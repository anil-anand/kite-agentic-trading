import React, { useState, useEffect } from 'react';
import { useTradingStore } from '../stores/trading-store';
import { SETTINGS_SAVE } from '@shared/ipc-channels';
import { LLMSettings, OpenCodePlan } from '@shared/types';

const LLM_PROVIDERS = ['OpenAI', 'Anthropic', 'Gemini', 'OpenRouter', 'Ollama', 'OpenCode'] as const;
const LLM_PRESETS: Record<string, Partial<LLMSettings>> = {
  OpenAI: { baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  Anthropic: { baseUrl: 'https://api.anthropic.com/v1', model: 'claude-3-5-haiku-latest' },
  Gemini: { baseUrl: 'https://generativelanguage.googleapis.com/v1beta', model: 'gemini-2.5-flash' },
  OpenRouter: { baseUrl: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o-mini' },
  Ollama: { baseUrl: 'http://localhost:11434', model: 'llama3.2' },
  OpenCode: { baseUrl: 'https://opencode.ai/zen/v1', model: 'big-pickle' },
};
const OPENCODE_PLAN_PRESETS: Record<OpenCodePlan, Partial<LLMSettings>> = {
  zen: { baseUrl: 'https://opencode.ai/zen/v1', model: 'big-pickle', openCodePlan: 'zen' },
  go: { baseUrl: 'https://opencode.ai/zen/go/v1', model: 'kimi-k3', openCodePlan: 'go' },
};

const Settings: React.FC = () => {
  const { settings, setSettings } = useTradingStore();
  const [localSettings, setLocalSettings] = useState<any>(settings);
  const [saveStatus, setSaveStatus] = useState<string>('');
  const [llmKey, setLlmKey] = useState<string>('');
  const [models, setModels] = useState<string[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [discoveryError, setDiscoveryError] = useState('');

  // Sync local state when global settings load
  useEffect(() => {
    if (settings) {
      setLocalSettings(settings);
      setLlmKey('');
    }
  }, [settings]);

  if (!localSettings) {
    return <div className="p-6 text-white">Loading settings...</div>;
  }

  const handleRiskChange = (key: string, value: string | boolean) => {
    setLocalSettings((prev: any) => {
      let finalValue: string | number | boolean = value;
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

  const updateLlm = (changes: Partial<LLMSettings>) => {
    setLocalSettings((prev: any) => prev ? ({ ...prev, llm: { ...prev.llm, ...changes } }) : prev);
  };

  const handleProviderChange = (provider: string) => {
    updateLlm({ provider: provider as LLMSettings['provider'], ...(provider === 'OpenCode' ? OPENCODE_PLAN_PRESETS.zen : LLM_PRESETS[provider] || {}) });
    setModels([]);
    setDiscoveryError('');
  };

  const handleOpenCodePlanChange = (plan: OpenCodePlan) => {
    updateLlm(OPENCODE_PLAN_PRESETS[plan]);
    setModels([]);
    setDiscoveryError('');
  };

  const discoverModels = async () => {
    try {
      setDiscovering(true);
      const result = await window.electronAPI?.settings.discoverModels({
          provider: localSettings.llm.provider,
          baseUrl: localSettings.llm.baseUrl,
          openCodePlan: localSettings.llm.openCodePlan || 'zen',
          apiKey: llmKey,
       });
       if (Array.isArray(result)) setModels(result);
     } catch (error) {
       setDiscoveryError(error instanceof Error ? error.message : 'Model discovery failed');
     } finally {
      setDiscovering(false);
    }
  };

  const saveChanges = async () => {
    try {
      setSaveStatus('Saving...');
      await window.electronAPI?.invoke(SETTINGS_SAVE, {
        ...localSettings,
        llm: { ...localSettings.llm, apiKey: llmKey },
      });
      
      setSettings({ ...localSettings, llm: { ...localSettings.llm, apiKey: '' } });
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
              {localSettings.llm?.provider === 'OpenCode' && (
                <div>
                  <label className="block text-surface-400 text-sm mb-1">OpenCode plan</label>
                  <select
                    value={localSettings.llm.openCodePlan || 'zen'}
                    onChange={(e) => handleOpenCodePlanChange(e.target.value as OpenCodePlan)}
                    className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white"
                  >
                    <option value="zen">Zen</option>
                    <option value="go">Go</option>
                  </select>
                </div>
              )}
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
            <h2 className="text-lg font-semibold text-white mb-4">BYOK LLM Post-Mortems</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-surface-400 text-sm mb-1">Provider</label>
                <select
                  value={localSettings.llm?.provider || 'Gemini'}
                  onChange={(e) => handleProviderChange(e.target.value)}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white"
                >
                   {LLM_PROVIDERS.map((provider) => <option key={provider}>{provider}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">Model</label>
                <div className="flex gap-2">
                  <select value={localSettings.llm?.model || ''} onChange={(e) => updateLlm({ model: e.target.value })}
                    className="flex-1 bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none">
                    <option value={localSettings.llm?.model || ''}>{localSettings.llm?.model || 'Select a model'}</option>
                    {models.filter((model) => model !== localSettings.llm?.model).map((model) => <option key={model}>{model}</option>)}
                  </select>
                  <button type="button" onClick={discoverModels} disabled={discovering}
                    className="px-3 py-2 bg-surface-700 hover:bg-surface-600 rounded-lg text-white disabled:opacity-50">
                    {discovering ? 'Loading...' : 'Refresh'}
                  </button>
                </div>
              </div>
              <div>
                <label className="block text-surface-400 text-sm mb-1">API Key</label>
                <input 
                   type="password"
                   value={llmKey}
                   onChange={(e) => setLlmKey(e.target.value)}
                   placeholder={localSettings.llm?.apiKeyConfigured ? 'Key saved (enter to replace)' : localSettings.llm?.provider === 'Ollama' ? 'Optional Ollama Cloud API key' : 'Enter provider API key'}
                  className="w-full bg-surface-900 border border-surface-700 rounded-lg px-4 py-2 text-white focus:border-accent-light outline-none" 
                />
              </div>
               {discoveryError && (
                 <p role="alert" className="text-sm text-loss-light">Model discovery failed: {discoveryError}</p>
               )}
                <p className="text-xs text-surface-500">Used for trade post-mortems in the Journal. Keys are encrypted locally. Ollama runs locally without a key, or use an Ollama Cloud key.</p>
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
