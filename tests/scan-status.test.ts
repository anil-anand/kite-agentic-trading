import assert from 'node:assert/strict';
import test from 'node:test';
import type { AgentState, ScanProgress } from '../src/shared/types.ts';
import { scanSummary } from '../src/renderer/utils/scan-status.ts';

const state: AgentState = {
  running: true, mode: 'auto', enabledStrategies: [], tradesToday: 0,
  signalsGenerated: 0, currentPnl: 0, maxDrawdownToday: 0,
  lastScanTime: null, status: 'monitoring', statusMessage: '', scanOnly: true,
  marketSession: { isOpen: false, isTradingDay: false, isWeekend: true },
};
const progress: ScanProgress = {
  phase: 'completed', analysisOnly: true, startedAt: '2026-09-26T10:30:00Z',
  completedAt: '2026-09-26T10:30:30Z', nextScanAt: null, universeSize: 100,
  totalSymbols: 12, completedSymbols: 12, evaluatedSymbols: 12,
  skippedSymbols: 0, failedSymbols: 0, signalsFound: 0, signalsPublished: 0,
  enabledStrategies: [], queuedSymbols: [], workers: [], results: [], message: null,
};

test('a completed weekend scan explains closed-market analysis and the signal threshold', () => {
  const summary = scanSummary({ ...state, scanProgress: progress });
  assert.equal(summary.title, 'Closed-market scan complete');
  assert.match(summary.detail, /weekend/);
  assert.match(summary.detail, /12 stocks evaluated, 0 qualifying signals/);
  assert.match(summary.detail, /analysis only/);
  assert.match(summary.detail, /score ≥ 70/);
});

test('unchanged candles are not reported as a fresh evaluation with no matches', () => {
  const summary = scanSummary({ ...state, scanProgress: {
    ...progress, evaluatedSymbols: 0, skippedSymbols: 12,
    results: [{ symbol: 'TEST', outcome: 'unchanged', detail: '', signals: 0, candleTime: '2026-09-25T10:00:00Z' }],
  } });
  assert.equal(summary.title, 'No new completed candles');
  assert.match(summary.detail, /already analyzed/);
  assert.match(summary.detail, /weekend/);
});

test('empty watchlists, data failures and missing candles explain distinct causes', () => {
  assert.equal(scanSummary({ ...state, scanProgress: { ...progress, totalSymbols: 0 } }).title, 'No stocks selected');
  assert.equal(scanSummary({ ...state, scanProgress: { ...progress, failedSymbols: 12, evaluatedSymbols: 0 } }).title, 'All stock scans failed');
  assert.equal(scanSummary({ ...state, scanProgress: { ...progress, failedSymbols: 1 } }).title, 'Scan complete with data errors');
  assert.equal(scanSummary({ ...state, scanProgress: { ...progress, skippedSymbols: 12, evaluatedSymbols: 0 } }).title, 'No stocks could be evaluated');
  assert.equal(scanSummary({ ...state, scanProgress: { ...progress, phase: 'error' } }).title, 'Scan could not complete');
});

test('a scan-only run during market hours does not claim that the market is closed', () => {
  const summary = scanSummary({ ...state, marketSession: { isOpen: true, isTradingDay: true, isWeekend: false }, scanProgress: progress });
  assert.equal(summary.title, 'Scan complete');
  assert.doesNotMatch(summary.detail, /closed|weekend/);
  assert.match(summary.detail, /analysis only/);
});

test('stopping during a scan distinguishes finishing work from an active entry run', () => {
  assert.equal(scanSummary({ ...state, running: false, scanProgress: { ...progress, phase: 'scanning' } }).title, 'Finishing current scan');
  assert.equal(scanSummary({ ...state, running: false, scanProgress: progress }).title, 'Scanner stopped');
});
