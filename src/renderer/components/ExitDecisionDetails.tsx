import React from 'react';

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

export const displayValue = (value: unknown): string => value == null ? 'Unavailable' : typeof value === 'object' ? JSON.stringify(value) : String(value);

/** Retained observations only: never infer a healthy HOLD from an absent record. */
const ExitDecisionDetails: React.FC<{ decision: Record<string, unknown> }> = ({ decision }) => {
  const trace = object(decision.trace);
  const evidence = object(trace.evidence);
  const observations = Array.isArray(evidence.observations) ? evidence.observations : [
    ...(Array.isArray(decision.supporting_evidence) ? decision.supporting_evidence : []),
    ...(Array.isArray(decision.opposing_evidence) ? decision.opposing_evidence : []),
  ];
  const groups = new Map<string, Record<string, unknown>[]>();
  for (const raw of observations) {
    const item = object(raw);
    const family = displayValue(item.family);
    groups.set(family, [...(groups.get(family) || []), item]);
  }
  return (
    <details className="mt-2 rounded border border-surface-700 p-3 text-xs">
      <summary className="cursor-pointer text-white">{displayValue(decision.action)} · {displayValue(decision.primary_reason_code)}</summary>
      <p className="mt-2 text-surface-400">Evaluation time: {displayValue(decision.occurred_at)} · Policy: {displayValue(decision.policy_version)}</p>
      {groups.size ? [...groups].map(([family, items]) => (
        <div key={family} className="mt-3 text-surface-300">
          <strong>{family}</strong>
          {items.map((item, index) => <p key={index}>{displayValue(item.direction)} · {displayValue(item.predicate)} · Quality: {displayValue(item.quality)} · Freshness: {displayValue(item.freshness)}</p>)}
        </div>
      )) : <p className="mt-2 text-amber-200">Evidence unavailable for this evaluation.</p>}
      <details className="mt-3 text-surface-400">
        <summary className="cursor-pointer">Full retained inputs, state, evidence and suppressed candidates</summary>
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap">{JSON.stringify(decision, null, 2)}</pre>
      </details>
    </details>
  );
};

export default ExitDecisionDetails;
