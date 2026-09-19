# Position and execution state machines

## 1. Model and ownership

Separate **exposure lifecycle**, **thesis health**, **trade development** and
**protection**. `PROFITABLE`, `PULLBACK` and `EXIT_PENDING` are not competing
mutually exclusive states: a winning protected trade may pull back while an
already-issued exit is still working.

One reducer owns each managed broker-position key:

```text
(execution_namespace, account_id, exchange, instrument_id, product, position_epoch)
```

The epoch distinguishes closing/reopening the same instrument. Version 1 allows
one managed logical trade per broker net-position key; it does not net several
independent playbook trades together. External size/side/product changes require
reconciliation. Financial risk can include all account positions without giving
the app discretionary ownership of them.

Events include unique IDs, market time, received time and a monotonically
increasing local sequence. Duplicate fills/updates/candles are idempotent. The
reducer returns a new version; committing it and its trace/intent is atomic.
Unexpected transitions cause `RECOVERY_REQUIRED` and a structured fault, not a
dictionary reset.

## 2. Exposure lifecycle states

| State | Definition |
|---|---|
| `NEW` | Validated thesis draft; any local risk reservation is explicit, with no submitted broker order or filled exposure yet |
| `ENTRY_PENDING` | Entry intent is prepared, submitting, working or partially filled; any filled quantity already requires protection; an unresolved outcome transitions to recovery |
| `OPEN` | Initial entry is terminal, positive managed residual is reconciled, and protection/operational state is explicit |
| `EXIT_PENDING` | Latched reduction/flatten obligation; may be cancelling protection, submitting, working, partially filled or recovering an execution attempt |
| `FLAT_PENDING_RECONCILIATION` | Evidence indicates zero residual, but fills/accounting and potentially live entry/exit/stop orders still need resolution |
| `RECOVERY_REQUIRED` | Broker/order/ownership state cannot safely support normal mutation; retains prior lifecycle, known quantity bounds, thesis and any latched exit obligation |
| `CLOSED` | Residual confirmed zero, entry intent terminal, all app orders terminal/cancelled, fill allocation and close record consistent |
| `ENTRY_ABORTED` | Confirmed no fills and no working/unknown entry orders; reservation released |

An unavailable snapshot cannot create `CLOSED`, `ENTRY_ABORTED` or “zero quantity.”
Financial values may remain unknown while flatness is being established. If older
fills cannot yet be obtained, retain `FLAT_PENDING_RECONCILIATION` with a visible
accounting issue; do not fabricate a ₹0 fill.

## 3. Complete exposure transition table

Only the transitions below are permitted, plus idempotent self-transitions that
consume known duplicate/non-state-changing events. Terminal states reject
exposure-changing events for that epoch and investigate them as new exposure.

| From | To | Guard / event | Required effect |
|---|---|---|---|
| NEW | ENTRY_PENDING | Valid entry intent committed and reservation acquired | Persist intent/tag before broker submission |
| NEW | ENTRY_ABORTED | Local rejection/cancellation; no broker submission or unknown effect | Release reservation; preserve rejected thesis/reason |
| NEW | RECOVERY_REQUIRED | Submission/ownership cannot be determined | Freeze entries for key; retain reservation |
| ENTRY_PENDING | ENTRY_PENDING | Acknowledgement/partial fill/stop confirmation | Deduplicate fill; update provisional risk and protect actual filled quantity |
| ENTRY_PENDING | OPEN | Entry terminal, residual > 0, protection established | Freeze initial fills/R; start completed-post-fill management eligibility |
| ENTRY_PENDING | EXIT_PENDING | Hard risk, operator close, stop failure or risk budget exceeded with known residual | Cancel remaining entry; latch flatten; track every late fill |
| ENTRY_PENDING | ENTRY_ABORTED | Entry terminal and successful fill reconciliation proves zero fills | Release reservation once |
| ENTRY_PENDING | FLAT_PENDING_RECONCILIATION | Filled entry already offset by stop/exit | Resolve pending entry and protection so late fills cannot reopen |
| ENTRY_PENDING | RECOVERY_REQUIRED | Unknown submission/cancel, data disagreement, missing protection verification | Retain possible exposure and outstanding orders |
| OPEN | OPEN | HOLD, development/health change or valid stop modification request/ack | Commit trace/counters; confirmed stop only changes on acknowledgement |
| OPEN | EXIT_PENDING | Authoritative risk, confirmed normal exit, fixed objective or operator close | Persist one latched intent; no new entries for key |
| OPEN | FLAT_PENDING_RECONCILIATION | Broker stop/external fills establish flatness | Attribute fills and cancel orphaned protection |
| OPEN | RECOVERY_REQUIRED | Unknown broker state, unexplained side/quantity/product change or unverified stop loss of coverage | Maintain supervision and known protection; resolve ownership/quantity |
| EXIT_PENDING | EXIT_PENDING | Working/partial fill; rejected attempt with known residual; hard-risk escalation | Retain obligation; retry only after prior attempt is known terminal or uniquely reconciled |
| EXIT_PENDING | FLAT_PENDING_RECONCILIATION | Zero residual indicated by reconciled fills/position snapshot | Confirm all orders incapable of adding/reversing exposure |
| EXIT_PENDING | RECOVERY_REQUIRED | Ambiguous request outcome, conflicting snapshots or unexpected reversal | Keep exit obligation and all associated IDs; no blind second sell/buy |
| FLAT_PENDING_RECONCILIATION | CLOSED | Flatness, fill allocation and order cleanup validated | Final P&L/R, reason attribution, cooldown; immutable closure event |
| FLAT_PENDING_RECONCILIATION | EXIT_PENDING | Late entry fill creates known residual under existing close obligation | Cancel remaining entry; close only actual residual |
| FLAT_PENDING_RECONCILIATION | RECOVERY_REQUIRED | A live/unknown order or unexplained exposure remains | Continue supervision, no terminal claim |
| RECOVERY_REQUIRED | ENTRY_PENDING | Restored entry intent known working/partial, no overriding exit obligation | Resume residual protection and entry tracking |
| RECOVERY_REQUIRED | OPEN | Reconciled positive residual, no latched exit obligation, valid protection | Resume pinned policy with gaps explicitly recorded |
| RECOVERY_REQUIRED | EXIT_PENDING | Reconciled residual with latched exit/hard-risk obligation | Continue original intent/priority, not a fresh discretionary vote |
| RECOVERY_REQUIRED | FLAT_PENDING_RECONCILIATION | Reconciled zero residual but cleanup/accounting remains | Resolve remaining obligations |
| RECOVERY_REQUIRED | ENTRY_ABORTED | Proven zero fills and terminal entry | Release reservation once |

There is **no** `EXIT_PENDING -> OPEN` transition because the price recovered.
Cancelling a normal exit order does not cancel the reason to exit. If a future
product needs operator rescission, it must be a separate audited capability with
confirmed order cancellation and risk approval; version 1 does not implement it.

## 4. Thesis health transitions

Health describes evidence, not whether an order has filled.

| From | To | Guard |
|---|---|---|
| UNKNOWN | VALID | A versioned, validated thesis becomes available from original recorded inputs or an explicit operator amendment |
| UNKNOWN | UNKNOWN | Missing/legacy thesis; hard risk/objective/session still managed |
| VALID | WEAKENING | Two consecutive eligible bars meet material weakening predicate, but no invalidation route is confirmed |
| VALID | INVALIDATED | Entry-defined failure or multi-family route reaches confirmation |
| WEAKENING | WEAKENING | Contradiction persists, recovery incomplete, or current evidence unavailable |
| WEAKENING | VALID | Two consecutive eligible recovery bars reclaim the relevant buffered premise and restore supporting structure |
| WEAKENING | INVALIDATED | A confirmed invalidation route fires |
| INVALIDATED | INVALIDATED | Latched until this position epoch closes |

Fresh required inputs are tracked on a separate quality axis. A data outage does
not turn VALID into UNKNOWN-thesis; it marks **assessment unavailable** and breaks
consecutive confirmation. Conversely it cannot clear WEAKENING/INVALIDATED.
UNKNOWN can still have a known precommitted objective or hard stop.

On an invalidation event, health becomes INVALIDATED and an exit intent is
committed together. If the broker state is unknown, exposure becomes/stays
RECOVERY_REQUIRED with that same exit obligation. There is no interval in which
an invalidated position is represented as an ordinary HOLD.

## 5. Development phase transitions

These phases guide interpretation, not hard-risk permissions. The phase resolver
uses only available post-entry completed bars and known structure. Numerical
definitions/horizons are pinned in the management profile.

| From | To | Guard |
|---|---|---|
| EARLY | DEVELOPING | First eligible post-entry review establishes behavior without favorable structural progress |
| EARLY | FAVORABLE | Confirmed favorable follow-through/base establishes progress |
| EARLY | PULLBACK | Controlled adverse retest while the entry premise remains intact |
| EARLY | CONSOLIDATING | Profile-defined overlap/no-progress pattern while structure is intact |
| DEVELOPING | FAVORABLE | New favorable completed-close/structural progress |
| DEVELOPING | PULLBACK | Controlled retracement/retest with no confirmed invalidation |
| DEVELOPING | CONSOLIDATING | Overlapping/no-progress bars inside defended structure |
| FAVORABLE | PULLBACK | Retracement from favorable progress within tolerated structure |
| FAVORABLE | CONSOLIDATING | Pause near favorable extreme, intact structure |
| PULLBACK | FAVORABLE | Reclaim and renewed favorable progress |
| PULLBACK | DEVELOPING | Retest resolved but favorable progress not established |
| PULLBACK | CONSOLIDATING | Controlled retest settles into overlap |
| CONSOLIDATING | FAVORABLE | Confirmed continuation out of the base |
| CONSOLIDATING | PULLBACK | Adverse retest within intact premise |
| CONSOLIDATING | DEVELOPING | Overlap ends without favorable confirmation |

All phases allow self-transition. No later phase returns to EARLY. Invalidated or
closed exposure freezes the last development phase for diagnosis; new phases do
not resurrect the position. Profit is a numeric property, not another phase.

## 6. Protection state

Track confirmed stop ID, trigger, limit/type, protected quantity and last verified
broker status, alongside requested values and command ID. Derived coverage is:

- `UNCONFIRMED`: position/fill known, protective acknowledgement not established.
- `ACTIVE`: verified protective order covers the correct residual side/quantity.
- `UPDATE_PENDING`: desired tighter stop/quantity change sent; previous confirmed
  protection remains the displayed effective state until resolved.
- `HANDOFF_PENDING`: a controlled reduction is replacing/cancelling protection.
- `FAILED_OR_UNKNOWN`: rejected, missing, wrongly sized, triggered-but-unfilled,
  or unavailable verification; needs authoritative recovery.
- `NONE_FLAT`: no position and all protective orders terminal.

Allowed edges: UNCONFIRMED -> ACTIVE/FAILED_OR_UNKNOWN/NONE_FLAT;
ACTIVE -> UPDATE_PENDING/HANDOFF_PENDING/FAILED_OR_UNKNOWN/NONE_FLAT;
UPDATE_PENDING -> ACTIVE/FAILED_OR_UNKNOWN/HANDOFF_PENDING/NONE_FLAT;
HANDOFF_PENDING -> NONE_FLAT/ACTIVE/FAILED_OR_UNKNOWN;
FAILED_OR_UNKNOWN -> ACTIVE/HANDOFF_PENDING/NONE_FLAT after reconciliation.
Repeated observations are self-transitions. NONE_FLAT -> UNCONFIRMED belongs to
a late-fill recovery or a new position epoch, never an unexplained reset.

`PROFIT_PROTECTED` is an additional derived label only after a confirmed stop
implies a positive estimated net floor under ordinary fill assumptions. It is
not a promise across a gap or a stop-limit nonfill. A stop already better than
entry may never be moved back to entry.

## 7. Order attempt and intent state

An **intent** expresses an obligation (`ENTER`, `PROTECT`, `TIGHTEN`, `EXIT` or
`FLATTEN`); an **attempt** is one broker mutation carrying a stable unique tag.
Several proven-terminal attempts can serve one still-active intent.

```text
PREPARED -> SUBMITTING -> ACKNOWLEDGED -> WORKING -> FILLED
                 |              |          |
                 v              +----------+-> PARTIALLY_FILLED -> FILLED
              UNKNOWN                       |          |
                 |                          +----------+-> CANCEL_PENDING
                 +-> reconcile known status                 |
                                                   CANCELLED / FILLED
```

Rejection may follow SUBMITTING/ACKNOWLEDGED/WORKING; record any known filled
quantity even for a terminal CANCELLED/REJECTED response. UNKNOWN may resolve
to any proven broker state, or stay UNKNOWN. It does not authorize a new UUID
and another submission. A missing item in a failed/stale order-book read is not
a negative acknowledgement. Validate transient broker statuses instead of
recognizing only OPEN and TRIGGER PENDING.

Only a terminal attempt with reconciled residual can authorize a replacement.
The parent EXIT/FLATTEN intent remains latched through attempt failure and
restart. Stop COMPLETE is first processed as a potential exposure-reducing fill;
it is not “stop confirmation failed, therefore sell the old quantity again.”

## 8. Concurrency, session and restart rules

- Per-position serialization covers decision commitment, reduction ownership and
  fill allocation. Network I/O runs outside state locks with expected-version
  checks on return. Reserve account-level entry capacity atomically across symbols.
- Hard risk can escalate an existing normal exit's urgency; it does not create
  a competing full-quantity exit. Pending entries are cancelled and late fills
  remain managed during account flattening.
- Entry pause/confirm mode affects entry admission only. Supervision continues.
  Process shutdown exposes unresolved obligations; native broker stops remain
  authoritative during downtime. Startup reconciliation precedes new entries.
- Session change rotates daily counters only after an exchange-session event and
  successful broker reconciliation. An unresolved prior-session position or
  flatten obligation is carried forward; midnight does not forgive it.
- Checkpoints store counters, last bar ID, extrema, pinned versions, desired and
  confirmed protection, intent/attempt IDs, sequence and fill allocation. Restart
  replays later events, reconciles broker facts, then resumes eligible decisions.
- Historical backfill may reconstruct context but cannot issue multiple retroactive
  orders as though they were sent during downtime. The catch-up decision uses
  current availability and records the missing-supervision interval.

## 9. Illustrative sequences

**Healthy trend:** ENTRY_PENDING(partial fill/protect) -> OPEN/EARLY/VALID ->
FAVORABLE -> PULLBACK/VALID/HOLD -> FAVORABLE; stop advances only after a causal
new swing is confirmed. RSI/Stochastic weakening remains one dynamics observation.

**Failed breakout:** OPEN/VALID -> first close inside original range (candidate)
-> second qualifying close (INVALIDATED + EXIT_PENDING) -> partial exit ->
terminal residual exit -> FLAT_PENDING_RECONCILIATION -> CLOSED. A bounce after
the second close does not revoke the exit.

**Stop fills during app exit:** exit intent reserved -> stop cancel outcome
unknown -> RECOVERY_REQUIRED -> stop fill reconciled -> zero residual -> cleanup
-> CLOSED. No app order sells the original quantity again.

**Hard risk during early grace:** ENTRY_PENDING or OPEN/EARLY -> EXIT_PENDING
immediately; cancel entry remainder and flatten known filled exposure. Grace
period, profit, evidence and confirmation mode have no veto.
