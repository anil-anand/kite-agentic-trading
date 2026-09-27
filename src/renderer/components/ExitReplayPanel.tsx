import React from 'react';
import type { ExitDecisionReplay } from '@shared/types';
import ExitDecisionDetails from './ExitDecisionDetails';

const ExitReplayPanel: React.FC<{ replay: ExitDecisionReplay }> = ({ replay }) => (
  <div className="space-y-4">
    <p className="text-xs text-surface-400">Retained decision inputs. Verification results below determine whether each evaluation reproduced.</p>
    {!replay.available && <p role="alert" className="text-sm text-amber-200">Replay unavailable: {replay.reason || (replay.position?.state_corrupt ? 'Saved state is corrupt' : !replay.decisions?.length ? 'No retained policy evaluations' : 'Retained inputs unavailable')}. Available records remain inspectable below.</p>}
    {replay.replayability && <p className="text-sm text-surface-300">Reproduced {replay.replayability.reproduced} of {replay.replayability.total} evaluations.</p>}
    {(replay.verification || []).map(result => (
      <div key={result.decision_id} className="rounded border border-surface-700 bg-surface-800 p-4 text-sm">
        <div className="flex flex-wrap justify-between gap-4"><span className="font-mono break-all text-surface-400">{result.decision_id}</span><span className={result.status === 'REPRODUCED' ? 'text-profit-light' : 'text-amber-200'}>{result.status}</span></div>
        {result.message && <p className="mt-2 text-amber-200">{result.message}</p>}
      </div>
    ))}
    {(replay.decisions || []).map(decision => decision.payload ? (
      <ExitDecisionDetails key={decision.decision_id} decision={decision.payload} />
    ) : <p key={decision.decision_id} className="text-amber-200">{decision.decision_id}: retained decision payload corrupt or unavailable.</p>)}
    {!replay.decisions?.length && <p className="text-sm text-amber-200">No retained policy evaluations are available.</p>}
    <details className="rounded border border-surface-700 p-4 text-xs text-surface-300">
      <summary className="cursor-pointer">Pinned thesis, latest state and execution intents</summary>
      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap">{JSON.stringify({ thesis: replay.thesis, position: replay.position, intents: replay.intents }, null, 2)}</pre>
      <p className="mt-2 text-surface-400">Decision-time state and versioned inputs are in each evaluation. Broker outcomes and fills are recorded separately in the trade timeline.</p>
    </details>
    <details className="rounded border border-surface-700 p-4 text-xs text-surface-300">
      <summary className="cursor-pointer">Execution outcomes, order attempts and allocated fills</summary>
      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap">{JSON.stringify({ attempts: replay.attempts ?? 'Unavailable', execution_events: replay.execution_events ?? 'Unavailable', fills: replay.fills ?? 'Unavailable', checkpoints: replay.checkpoints ?? 'Unavailable' }, null, 2)}</pre>
    </details>
  </div>
);

export default ExitReplayPanel;
