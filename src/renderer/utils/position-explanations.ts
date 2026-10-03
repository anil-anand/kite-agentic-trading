import type { ActivePositionExplanation, Position } from '@shared/types';

/** Lifecycle keys include a trade epoch; broker position keys do not. */
export function matchPositionExplanation(position: Position, explanations: ActivePositionExplanation[]): ActivePositionExplanation | undefined {
  const matches = explanations.filter(item => {
    if (position.positionKey && (item.broker_position_key === position.positionKey || item.position_key === position.positionKey)) return true;
    return Boolean(position.namespace && position.accountId && position.exchange && position.product && position.instrumentToken != null) &&
      item.namespace === position.namespace && item.account_id === position.accountId &&
      item.exchange === position.exchange && item.product === position.product &&
      item.instrument_id != null && String(item.instrument_id) === String(position.instrumentToken) &&
      item.symbol === position.tradingsymbol;
  });
  // Competing lifecycle epochs require reconciliation, not arbitrary selection.
  return matches.length === 1 ? matches[0] : undefined;
}
