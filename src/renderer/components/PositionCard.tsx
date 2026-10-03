import React from 'react';
import { ActivePositionExplanation, Position } from '@shared/types';
import { XCircle } from 'lucide-react';
import ExitDecisionDetails, { displayValue } from './ExitDecisionDetails';

const price = (value?: number | null) => value == null || !Number.isFinite(value) ? 'Unavailable' : `₹${value.toFixed(2)}`;
const risk = (value?: number | null) => value == null || !Number.isFinite(value) ? 'Unavailable' : `${value.toFixed(2)}R`;

interface Props {
  position: Position;
  explanation?: ActivePositionExplanation;
  onExit: (position: Position) => void;
  exitPending?: boolean;
}

const PositionCard: React.FC<Props> = ({ position, explanation, onExit, exitPending = false }) => {
  const isProfit = position.pnl != null && position.pnl >= 0;
  const assessedAt = explanation?.latest_decision?.occurred_at;
  const assessmentTime = typeof assessedAt === 'string' ? Date.parse(assessedAt) : NaN;
  // This is a display-age warning, never an input to the trading policy.
  const assessmentStale = Number.isFinite(assessmentTime) && Date.now() - assessmentTime > 600_000;
  
  return (
    <div className="bg-surface-800 rounded-lg p-4 border border-surface-700 flex flex-col gap-3 transition-transform hover:-translate-y-1">
      <div className="flex justify-between items-start">
        <div>
          <h3 className="font-bold text-white text-lg">{position.tradingsymbol}</h3>
          <span className="text-xs bg-surface-700 px-2 py-1 rounded text-surface-300">{position.exchange}</span>
        </div>
        <button
          onClick={() => onExit(position)}
          disabled={exitPending || !position.positionKey}
          className="text-loss-light hover:text-loss-dark transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          title={position.positionKey ? 'Exit Position' : 'Position identity unavailable'}
        >
          <XCircle size={20} />
        </button>
      </div>
      {exitPending && <p className="text-xs text-amber-200">Close pending broker reconciliation…</p>}
      {explanation ? (
        <div className="rounded border border-surface-700 bg-surface-900/70 p-3 text-xs">
          {explanation.state_corrupt && <p role="alert" className="mb-2 text-amber-200">Saved management state is corrupt; protection and policy state require reconciliation.</p>}
          {explanation.quality?.decision_corrupt === true && <p role="alert" className="mb-2 text-amber-200">Retained policy assessment is corrupt. Its current health and evidence are unavailable.</p>}
          {assessmentStale && <p role="alert" className="mb-2 text-amber-200">Latest policy assessment is over 10 minutes old. Current thesis health is unavailable; displayed protection is the last recorded broker confirmation.</p>}
          <p className="mb-2 text-surface-400">Policy mode: {explanation.policy_mode || 'Unavailable'} · Exposure: {explanation.exposure || 'Unknown'}</p>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-surface-300">
            <span>Thesis: <strong className="text-white">{explanation.thesis.playbook || explanation.thesis.strategy || 'Unknown'}</strong></span>
            <span>Health: <strong className="text-white">{assessmentStale || explanation.quality?.decision_corrupt === true ? 'Current assessment unavailable' : explanation.health || 'Unknown'}</strong></span>
            <span>Phase: <strong className="text-white">{explanation.development || 'Unknown'}</strong></span>
          </div>
          <p className="mt-2 text-surface-300">Entry reason: {explanation.thesis.reasoning || 'Unavailable'}</p>
          <p className="mt-1 text-surface-400">Expected behavior: {explanation.thesis.expected_behavior || 'Unavailable'} · Original boundary: {price(explanation.thesis.original_boundary)} · Initial stop: {price(explanation.thesis.initial_stop)}</p>
          <div className="mt-2 text-surface-300">Protection quality: <strong>{explanation.protection.quality || 'Unknown'}</strong> · Confirmed stop: {price(explanation.protection.confirmed_stop)} · Requested stop: {price(explanation.protection.requested_stop)}</div>
          <p className="mt-1 text-surface-400">Protected quantity: {displayValue(explanation.protection.protected_quantity)} · Broker stop order: {displayValue(explanation.protection.confirmed_stop_order_id)}</p>
          <p className="mt-1 text-surface-400">A requested or shadow-proposed stop does not confirm broker protection.</p>
          <p className="mt-2 text-surface-300">Managed residual: {displayValue(explanation.residual_quantity)} · Pending intent: {explanation.pending_intent ? `${displayValue(explanation.pending_intent.intent_type)} / ${displayValue(explanation.pending_intent.status)} (${displayValue(explanation.pending_intent.quantity)})` : 'None recorded'}</p>
          <p className="mt-2 text-surface-300">uR: {risk(explanation.management.u_r)} · MFE: {risk(explanation.management.mfe_r)} · MAE: {risk(explanation.management.mae_r)} · Price-path giveback: {risk(explanation.management.giveback_r)}</p>
          <p className="mt-1 text-surface-400">Completed bar: {explanation.management.last_bar_end || 'Unavailable'} · Session remaining at evaluation: {displayValue(explanation.management.session_remaining_minutes)} min</p>
          <p className="mt-1 text-surface-400">Data quality at evaluation: primary {displayValue(explanation.quality?.primary)} / higher {displayValue(explanation.quality?.higher)} · State updated: {displayValue(explanation.quality?.checkpoint_at)}</p>
          <details className="mt-2 text-surface-400"><summary className="cursor-pointer">Context and data quality</summary><pre className="mt-2 overflow-x-auto whitespace-pre-wrap">{JSON.stringify({ context: explanation.context ?? 'Unavailable', quality: explanation.quality ?? 'Unavailable' }, null, 2)}</pre></details>
          {explanation.latest_decision ? <><p className="mt-2 text-surface-400">Latest policy evaluation (execution is tracked above):</p><ExitDecisionDetails decision={explanation.latest_decision} /></> : <p className="mt-2 text-amber-200">Latest policy evaluation unavailable.</p>}
        </div>
      ) : (
        <p className="text-xs text-amber-200">Current thesis, health and protection explanation unavailable. Check supervision and broker reconciliation status.</p>
      )}
      <div className="grid grid-cols-3 gap-2 text-sm">
        <div>
          <div className="text-surface-400">Qty</div>
          <div className="font-mono text-white">{position.quantity}</div>
        </div>
        <div>
          <div className="text-surface-400">Avg</div>
          <div className="font-mono text-white">{position.averagePrice != null ? `₹${position.averagePrice.toFixed(2)}` : 'Unavailable'}</div>
        </div>
        <div>
          <div className="text-surface-400">LTP</div>
          <div className="font-mono text-white">{position.lastPrice != null ? `₹${position.lastPrice.toFixed(2)}` : 'Unavailable'}</div>
        </div>
      </div>
      <div className="mt-2 pt-2 border-t border-surface-700 flex justify-between items-center">
        <span className="text-surface-400 text-sm">P&L</span>
        <div className={`font-mono font-bold ${position.pnl == null ? 'text-surface-300' : isProfit ? 'text-profit-light' : 'text-loss-light'}`}>
          {position.pnl == null ? 'Unavailable' : `${isProfit ? '+' : '-'}₹${Math.abs(position.pnl).toFixed(2)}`}
        </div>
      </div>
    </div>
  );
};

export default PositionCard;
