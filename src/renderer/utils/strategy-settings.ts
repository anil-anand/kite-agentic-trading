export const STRATEGY_IDS = [
  'ema_crossover',
  'rsi_reversal',
  'vwap_bounce',
  'supertrend',
  'macd_cross',
  'bollinger_breakout',
  'stochastic_reversal',
  'adx_momentum',
  'psar_trend',
  'donchian_breakout',
  'cci_reversal',
  'williams_r',
  'mfi_exhaustion',
  'keltner_breakout',
  'awesome_oscillator',
  'tsi_cross',
  'stoc_rsi',
] as const;

export const buildStrategySettings = (enabledStrategies: readonly string[]) =>
  Object.fromEntries(
    STRATEGY_IDS.map((strategyId) => [
      strategyId,
      { enabled: enabledStrategies.includes(strategyId) },
    ]),
  );
