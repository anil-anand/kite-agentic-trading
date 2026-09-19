# Exit and management reason codes

## 1. Contract

Reason codes are stable enums. Store their schema version and display text
separately. Details contain levels, observations, clocks and order IDs; symbols
and variable counts do not belong inside the enum. Do not use a prose string as
an analytics grouping key.

Every evaluation records:

```text
primary_reason_code, contributing_reason_codes, suppressed_candidates,
action, urgency, policy_version, rule_id, decision_id, state_before/after
```

Every closure also records:

```text
initiating_decision_id, initiating_reason_code, execution_outcome_code,
fill_order_ids, protection_role, secondary_risk_causes, reconciliation_quality
```

An order filled by a protective stop after a thesis exit request preserves both
facts. Matching `exit_order_id` must not relabel every exit as “target.” Unknown
attribution stays unknown.

## 2. Authority and precedence

Authority order is: broker facts/actual flatness; emergency/account hard risk and
forced deadline; protection/execution recovery; confirmed thesis failure;
precommitted objective/profit policy; time-based management; ordinary HOLD.
Unknown execution outcome constrains **safe submission**, not the authority of
an existing hard-risk obligation.

For simultaneous hard causes, primary attribution is deterministic:
`OPERATOR_EMERGENCY_FLATTEN`, `RISK_DAILY_LOSS`, `RISK_CATASTROPHIC_STOP`,
`SESSION_FORCED_FLAT`, `RISK_PROTECTION_FAILURE`, then operational hard recovery.
Preserve all other causes as contributors. Broker fill outcome is a different
field, so reason precedence cannot overwrite execution truth.

## 3. Hard-risk and operational obligations

| Code | Condition | Action / urgency |
|---|---|---|
| `RISK_CATASTROPHIC_STOP` | Fresh price reaches initial/hard risk boundary | EXIT immediately; no bar confirmation |
| `RISK_PROTECTIVE_STOP_TRIGGERED` | Protective stop triggered/filling, residual may remain | Reconcile/finish residual reduction immediately |
| `RISK_DAILY_LOSS` | Session net-risk measure breaches configured daily limit | Latch account flatten, cancel entry remainder, block new risk |
| `RISK_PROTECTION_FAILURE` | Known exposure has invalid/unconfirmed protection beyond operational deadline | Restore protection or flatten known residual; suspend entries |
| `RISK_ENTRY_BUDGET_EXCEEDED` | Actual fills exceed accepted initial risk/quantity constraints | Cancel entry remainder; deterministic reduce/flatten recovery |
| `RISK_DATA_BLINDNESS` | Explicit operational feed/broker-blindness deadline breached | Latched recovery obligation; preserve valid protection, flatten only with safe quantity/outcome knowledge |
| `SESSION_FORCED_FLAT` | Exchange/product forced intraday deadline | Cancel entries; continue flatten/reconcile until terminal |
| `OPERATOR_EMERGENCY_FLATTEN` | Explicit emergency command with account/position scope | Immediate authoritative reduction obligation |
| `EXEC_UNRECOVERABLE_FAILURE` | Execution cannot complete its mandated transition within policy deadline | Escalate hard recovery, suspend entries, keep obligation visible |

“Immediate” means no thesis/LLM/closed-candle wait; it does not assert zero broker
latency or promise a fill when exchange/network execution is unavailable.

## 4. Normal thesis exits

| Code | Required evidence | Action |
|---|---|---|
| `THESIS_STRUCTURE_ACCEPTANCE_FAILED` | Confirmed completed-close acceptance beyond frozen structural premise | EXIT |
| `THESIS_BREAKOUT_FAILED` | Confirmed acceptance back inside entry-defined prior range | EXIT |
| `THESIS_VWAP_ACCEPTANCE_FAILED` | VWAP was an explicit thesis premise; buffered failure plus required confirmation | EXIT |
| `THESIS_MULTI_FAMILY_FAILURE` | Price-led failure + nonduplicated corroboration across confirmation bars | EXIT |
| `THESIS_REGIME_INCOMPATIBLE` | Setup-destroying confirmed transition with price consequence, not label alone | EXIT |

These codes require the exact predicate outcomes specified in the pinned profile.
No `NO_BUY_SIGNALS` or `RSI_NEGATIVE` exit code exists.

## 5. Profit, time and operator management

| Code | Condition | Action |
|---|---|---|
| `PROFIT_FIXED_OBJECTIVE_REACHED` | Entry-pinned fixed objective barrier reached | EXIT using declared execution semantics |
| `PROFIT_CONVERGENCE_OBJECTIVE` | Range/reversion objective achieved | EXIT; preserve setup-specific attribution |
| `PROFIT_REVERSAL_CONFIRMED` | Favorable progress lost with required independent corroboration/confirmation | EXIT |
| `PROFIT_STRUCTURE_TRAIL` | New favorable structure allows a valid tighter stop | TIGHTEN_STOP; confirmed only after broker acknowledgement |
| `PROFIT_COST_AWARE_PROTECTION` | Valid structural stop also locks a positive estimated net floor | TIGHTEN_STOP / annotate protection; never a profit-only trigger |
| `TIME_NO_PROGRESS_CONFIRMED` | Profile review horizon plus confirmed failure of expected progress | EXIT |
| `SESSION_LATE_MANAGEMENT` | Late-session normal-management predicate is satisfied before forced deadline | EXIT or TIGHTEN_STOP as the frozen rule specifies |
| `OPERATOR_POSITION_CLOSE` | Explicit close of a managed position | EXIT without entry gates; no LLM veto |

Fixed objectives are precommitted execution barriers. Their touch handling may
be fast/intrabar; they are not continuously recomputed tick-level thesis votes.
The shared simulator must use the same objective mode and ordering assumptions.

## 6. HOLD / non-exit decisions

| Code | Meaning |
|---|---|
| `HOLD_THESIS_VALID` | Premise intact and no higher-priority action justified |
| `HOLD_EARLY_DEVELOPMENT` | Normal early formation; weak signals insufficient |
| `HOLD_HEALTHY_PULLBACK` | Retracement within intact premise/context |
| `HOLD_CONSOLIDATION` | Controlled pause/overlap without confirmed deterioration |
| `HOLD_TREND_CONTINUATION` | Directional progress and supporting structure persist |
| `HOLD_THESIS_WEAKENING` | Material warning, but invalidation criteria not met |
| `HOLD_CONFIRMATION_PENDING` | Failure/recovery episode has insufficient distinct eligible bars |
| `HOLD_HIGHER_TIMEFRAME_SUPPORT` | Local setback not decisive against intact relevant HTF structure |
| `HOLD_OBJECTIVE_REVIEW_ZONE` | Runner reaches level that is a review zone, not fixed target |
| `HOLD_NO_VALID_STOP_IMPROVEMENT` | Proposed trail would loosen, violate broker distance or trail into current noise |
| `HOLD_UNKNOWN_THESIS` | Missing original premise; bounded risk/objective/session policy applies |
| `HOLD_DATA_DEGRADED` | Ordinary evaluation unavailable; hard-risk supervision continues |

HOLD never cancels a latched EXIT/FLATTEN intent. While an exit is pending the
decision action is `MANAGE_PENDING_INTENT`, with its initiating reason retained.

## 7. Data/execution diagnostics and terminal outcomes

| Code | Use |
|---|---|
| `DATA_INCOMPLETE_CANDLE` | Skipped normal evaluation |
| `DATA_DUPLICATE_BAR` | Idempotent no-op; counter unchanged |
| `DATA_STALE_CONTEXT` | Required source exceeded freshness policy |
| `DATA_GAP_OR_INVALID_OHLCV` | Missing/out-of-order/invalid market records |
| `DATA_HIGHER_TIMEFRAME_UNAVAILABLE` | HTF required input missing; no fabricated neutral context |
| `EXEC_INTENT_ALREADY_PENDING` | Existing action owns reduction/protection mutation |
| `EXEC_SUBMISSION_UNKNOWN` | Request outcome unresolved; reconcile by stable intent/tag |
| `EXEC_CANCEL_UNKNOWN` | Protective/exit cancellation not confirmed |
| `EXEC_PARTIAL_FILL` | Allocate fill and recompute residual, no full closure |
| `EXEC_ORDER_REJECTED` | Terminal attempt rejected; parent obligation may remain |
| `EXEC_PROTECTION_UPDATE_PENDING` | Requested stop differs from confirmed protection |
| `EXEC_BROKER_STATE_UNKNOWN` | Failed/malformed/stale snapshot, not a flat book |
| `EXEC_EXTERNAL_POSITION_CHANGE` | Unexpected quantity/side/product/ownership change |
| `EXEC_FILL_ATTRIBUTION_PENDING` | Flatness/fill accounting not yet fully reconciled |
| `BROKER_STOP_FILLED` | Actual protective order executed; add initial/trailing/profit role |
| `BROKER_APP_EXIT_FILLED` | Actual fill of app reduction intent |
| `BROKER_EXTERNAL_CLOSE` | Verified manual/broker close outside app intent |
| `RESEARCH_END_OF_DATA` | Research liquidation/censoring boundary, never mixed with ordinary live exit efficacy |
| `LEGACY_REASON_UNRESOLVED` | Historical text insufficient for reliable attribution |

## 8. Legacy migration

Keep original text verbatim in `legacy_reason_text`. Map only what is provable:

| Existing reason | Migration |
|---|---|
| `Stop Loss`, `stop_loss`, `stop_hit` | Historical stop-category reason; distinguish actual broker fill only with order/fill evidence |
| `Target`, `target`, `target_hit` | Historical target category, `attribution_quality=legacy`; current reconciler could have mislabeled it |
| `Resistance/Support` | Legacy single-rejection policy, not new confirmed structural invalidation |
| `Thesis invalidated for ...` | Legacy opposing-trigger policy; do not assert new multi-family confirmation |
| `Weak conviction for ...` | Legacy no-support/time/loss rule |
| `Square off` | Ambiguous daily-loss/session/operator cause unless trace proves it |
| `UNRECONCILED` | Accounting/fill attribution pending; exclude fabricated zero-price results from research |
| `manual_broker_exit` | Verify fill evidence; preserve external-close category |
| `end_of_test` | `RESEARCH_END_OF_DATA` |

Historical categories stay distinct in comparative studies. Never backfill
counterfactual certainty or a new thesis into an old untraceable trade.
