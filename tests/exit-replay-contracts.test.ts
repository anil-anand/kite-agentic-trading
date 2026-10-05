import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as IPC from '../src/shared/ipc-channels.ts';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import PositionCard from '../src/renderer/components/PositionCard.tsx';
import ExitReplayPanel from '../src/renderer/components/ExitReplayPanel.tsx';
import PnLDisplay from '../src/renderer/components/PnLDisplay.tsx';
import type { ActivePositionExplanation, Position } from '../src/shared/types.ts';
import { matchPositionExplanation } from '../src/renderer/utils/position-explanations.ts';

const source = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('exit-quality IPC stays on retained replay contracts', () => {
  assert.equal(IPC.ANALYTICS_EXIT_QUALITY_REPORT, 'analytics:exit-quality-report');
  assert.equal(IPC.ANALYTICS_EXIT_QUALITY_TRADE, 'analytics:exit-quality-trade');
  assert.equal(IPC.ANALYTICS_EXIT_MANAGEMENT_REPLAY, 'analytics:exit-management-replay');
  assert.equal(IPC.ANALYTICS_ACTIVE_POSITION_EXPLANATIONS, 'analytics:active-position-explanations');

  const handlers = source('../src/main/ipc-handlers.ts');
  const preload = source('../src/main/preload.ts');
  const journal = source('../src/renderer/pages/Journal.tsx');

  for (const name of [
    'ANALYTICS_EXIT_QUALITY_REPORT',
    'ANALYTICS_EXIT_QUALITY_TRADE',
    'ANALYTICS_EXIT_MANAGEMENT_REPLAY',
    'ANALYTICS_ACTIVE_POSITION_EXPLANATIONS',
  ]) {
    assert.match(handlers, new RegExp(name));
    assert.match(preload, new RegExp(name));
  }
  assert.match(journal, /getExitManagementReplay/);
  assert.match(journal, /getExitQualityForTrade/);
  assert.doesNotMatch(journal, /getWhatIfAnalysis\(/);
  assert.doesNotMatch(journal, /getTradeReplay\(/);
});

const position = { positionKey: 'LIVE:A:NSE:X:MIS', tradingsymbol: 'X', exchange: 'NSE', quantity: 4, averagePrice: 100, lastPrice: 103, pnl: -12 } as Position;

test('active explanation distinguishes pending protection and shadow decisions from execution', () => {
  const explanation: ActivePositionExplanation = {
    position_key: position.positionKey!, symbol: 'X', state_corrupt: false,
    policy_mode: 'SHADOW', thesis: { reasoning: 'Breakout and retest', original_boundary: 99, initial_stop: 98, expected_behavior: 'Hold the breakout level' },
    health: 'WEAKENING', development: 'PULLBACK', exposure: 'OPEN',
    protection: { quality: 'MODIFY_PENDING', confirmed_stop: 98, requested_stop: 101 },
    residual_quantity: 4, pending_intent: { intent_type: 'TIGHTEN', status: 'UNKNOWN', quantity: 4 },
    management: { mfe_r: 2, mae_r: 0.2, u_r: 1.5, last_bar_end: '2026-09-01T10:15:00+05:30' },
    latest_decision: { action: 'HOLD', primary_reason_code: 'HOLD_HEALTHY_PULLBACK', occurred_at: '2026-09-01T10:15:02+05:30', trace: { evidence: { observations: [
      { family: 'participation', direction: 'UNKNOWN', predicate: 'volume_baseline', quality: 'UNAVAILABLE', freshness: 'STALE' },
    ] } } },
  };
  const html = renderToStaticMarkup(React.createElement(PositionCard, { position, explanation, onExit() {} }));
  for (const expected of ['SHADOW', 'Confirmed stop: ₹98.00', 'Requested stop: ₹101.00', 'MODIFY_PENDING', 'TIGHTEN / UNKNOWN', 'participation', 'STALE', '2026-09-01T10:15:00+05:30', '-₹12.00']) assert.ok(html.includes(expected), expected);
  assert.ok(html.includes('execution is tracked above'));
  assert.ok(html.includes('Original boundary: ₹99.00'));
});

test('missing or corrupt management facts never fabricate HOLD or confirmed protection', () => {
  const explanation: ActivePositionExplanation = {
    position_key: position.positionKey!, symbol: 'X', state_corrupt: true,
    thesis: {}, health: null, development: null, exposure: null, protection: {}, management: {}, latest_decision: {},
  };
  const html = renderToStaticMarkup(React.createElement(PositionCard, { position, explanation, onExit() {} }));
  assert.match(html, /Saved management state is corrupt/);
  assert.match(html, /Confirmed stop: Unavailable/);
  assert.match(html, /Completed bar: Unavailable/);
  assert.doesNotMatch(html, />HOLD/);
  const missing = renderToStaticMarkup(React.createElement(PositionCard, { position, onExit() {} }));
  assert.match(missing, /protection explanation unavailable/);
});

test('replay mismatch and corrupt records remain inspectable without claiming reproduction', () => {
  const html = renderToStaticMarkup(React.createElement(ExitReplayPanel, { replay: {
    available: false, reason: 'STATE_CORRUPT', position: { state_corrupt: true },
    decisions: [{ decision_id: 'corrupt', payload: null, payload_corrupt: true }, { decision_id: 'valid', payload: { action: 'HOLD', primary_reason_code: 'HOLD_THESIS_VALID', policy_version: 'v1', trace: { next_state: { thesis_health: 'VALID' } } } }],
    verification: [{ decision_id: 'corrupt', status: 'CORRUPT_RETAINED_PAYLOAD' }, { decision_id: 'valid', status: 'REPLAY_MISMATCH' }],
    replayability: { reproduced: 0, total: 2, uses_current_market_data: false },
    intents: [{ intent_type: 'EXIT', status: 'UNKNOWN' }],
  } }));
  assert.match(html, /Reproduced 0 of 2/);
  assert.match(html, /REPLAY_MISMATCH/);
  assert.match(html, /retained decision payload corrupt/);
  assert.match(html, /HOLD_THESIS_VALID/);
  assert.match(html, /UNKNOWN/);
  assert.doesNotMatch(html, /Reproduced from immutable/);
});

test('unavailable P&L is neutral and a loss has exactly one minus sign', () => {
  const unavailable = renderToStaticMarkup(React.createElement(PnLDisplay, { amount: null, netAmount: null }));
  assert.match(unavailable, /Unavailable/);
  assert.doesNotMatch(unavailable, /text-loss-light|text-profit-light/);
  const loss = renderToStaticMarkup(React.createElement(PnLDisplay, { amount: -10, netAmount: -12 }));
  assert.match(loss, /-₹12.00/);
  assert.doesNotMatch(loss, /-₹-/);
});

test('broker positions match one lifecycle epoch by full account and instrument identity', () => {
  const brokerPosition = { ...position, positionKey: 'LIVE:A:NSE:1:X:MIS', namespace: 'LIVE', accountId: 'A', instrumentToken: 1, product: 'MIS' } as Position;
  const explanation = {
    position_key: `${brokerPosition.positionKey}:epoch-3`, broker_position_key: brokerPosition.positionKey,
    namespace: 'LIVE', account_id: 'A', exchange: 'NSE', instrument_id: '1', symbol: 'X', product: 'MIS',
  } as ActivePositionExplanation;
  assert.equal(matchPositionExplanation(brokerPosition, [explanation]), explanation);
  assert.equal(matchPositionExplanation(brokerPosition, [{ ...explanation, position_key: 'LIVE:B:NSE:1:X:MIS:epoch-3', broker_position_key: 'LIVE:B:NSE:1:X:MIS', account_id: 'B' }]), undefined);
  assert.equal(matchPositionExplanation(brokerPosition, [explanation, { ...explanation, position_key: `${brokerPosition.positionKey}:epoch-4` }]), undefined, 'two nonterminal epochs need reconciliation');
});

test('successful reads of old assessments cannot advertise current healthy state', () => {
  const explanation: ActivePositionExplanation = {
    position_key: position.positionKey!, symbol: 'X', state_corrupt: false,
    thesis: {}, health: 'VALID', development: 'FAVORABLE', exposure: 'OPEN',
    protection: { quality: 'ACTIVE', confirmed_stop: 98 }, management: {},
    latest_decision: { occurred_at: '2000-01-01T00:00:00+00:00', action: 'HOLD' },
  };
  const html = renderToStaticMarkup(React.createElement(PositionCard, { position, explanation, onExit() {} }));
  assert.match(html, /Current assessment unavailable/);
  assert.match(html, /over 10 minutes old/);
  assert.match(html, /last recorded broker confirmation/);
});
