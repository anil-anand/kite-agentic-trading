import assert from 'node:assert/strict';
import test from 'node:test';
import { STRATEGY_IDS, buildStrategySettings } from '../src/renderer/utils/strategy-settings.ts';

test('builds settings for every configured strategy', () => {
  const enabledStrategies = ['ema_crossover', 'stoc_rsi'];

  const settings = buildStrategySettings(enabledStrategies);

  assert.deepEqual(Object.keys(settings), STRATEGY_IDS);
  assert.equal(settings.ema_crossover.enabled, true);
  assert.equal(settings.stoc_rsi.enabled, true);
  assert.equal(settings.adx_momentum.enabled, false);
});
