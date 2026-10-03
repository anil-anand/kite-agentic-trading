import React from 'react';
import { useTradingStore } from '../stores/trading-store';

const AgentEntryNotice: React.FC = () => {
  const state = useTradingStore(store => store.agentState);
  const reasons = state.entryBlockReasons ?? [];
  const recovery = state.reconciliationPending || state.lifecycleRecoveryPending || state.controlStateInvalid || state.protectionFailureHalt;
  if (!reasons.length && !recovery) return null;

  return (
    <div role="status" className="rounded border border-amber-700/60 bg-amber-900/20 p-3 text-sm text-amber-200">
      <p>New trade entries are blocked. {state.scanOnly ? 'Scan-only analysis is running.' : 'Start Agent can still run scan-only analysis.'}</p>
      {reasons.length > 0 ? (
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {reasons.map(reason => <li key={reason}>{reason}</li>)}
        </ul>
      ) : <p className="mt-2">Broker or saved trade recovery is pending.</p>}
    </div>
  );
};

export default AgentEntryNotice;
