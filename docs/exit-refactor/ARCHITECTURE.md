# Exit-management architecture

## 1. Decision and invariants

Introduce a small, broker-independent exit domain around the existing engine.
Its central operation is a pure state transition:

```text
evaluate_exit(thesis, position_state, market_context, risk_snapshot, policy)
    -> next_state, decision, proposed_intent
```

Inputs contain explicit event times and versions. The function reads no network,
database, wall clock, mutable global configuration, or LLM. Hard-risk evaluation
uses the same explicit risk rules in every environment, and has a separate fast
live scheduler. Normal thesis evaluation happens once per eligible completed 5m
candle, with the latest available completed 15m context.

Non-negotiable invariants:

1. Protective stops, daily-loss flattening, emergency flattening and forced
   intraday deadlines preempt every normal HOLD or profit-management decision.
2. A timeout, missing price, empty result, incomplete candle or unknown broker
   state is not proof that a position is flat or its thesis has failed.
3. At most one reduction intent owns a broker position at a time. A fill reduces
   **actual residual quantity**; an order acknowledgement does not close a trade.
4. Confirmed protection is never relaxed to improve a thesis score. Initial R is
   immutable; trailing a stop does not redefine the denominator.
5. Normal exit decisions require thesis-specific price evidence and confirmation;
   fresh entry triggers are not required to keep an existing position open.
6. Decisions and state transitions can be reproduced from recorded inputs. Live,
   paper, scenario, backtest and replay call the same exit function.
7. Pausing entries, choosing confirmation mode, losing the renderer or stopping
   an LLM job cannot disable risk supervision of an existing managed position.

## 2. Current architecture, as implemented

### 2.1 Repository map

| Area | Current responsibility and important behavior |
|---|---|
| `src/main/` | Electron lifecycle, native secret storage, login, preload and IPC; spawns/restarts Python |
| `src/renderer/` | Zustand state, entry controls, order/position UI, journal and research screens |
| `backend/main.py` | Serial stdin JSON-RPC dispatcher; global subsystem instances; synchronous analytics/backtests |
| `backend/trading_engine.py` | Scanning, entry submission/fill polling, adoption, protection, all normal exits, square-off, journal reconciliation |
| `backend/scanner.py` | Cached five-day 5m history; 17 strategies; oscillator/breakout aggregation; regime/playbook entry gating; directional reevaluation |
| `backend/strategies/` | Mostly crossover/reversal events on the latest candle; fixed-percent or indicator-based initial stops |
| `backend/playbooks/` | Trend Pullback, Breakout, Mean Reversion entries; boolean invalidation methods have no production caller |
| `backend/regime_classifier.py` | Stateless ADX/ATR regime label plus EMA/VWAP features on one timeframe |
| `backend/indicators.py` | Session-reset candle-approximation VWAP, already used by RSI, VWAP bounce and regime classification |
| `backend/screener.py`, `nifty_universe.py` | Batched-quote cross-sectional ranking, daily constituent lookup/fallback, partial sector map |
| `backend/execution_gateway.py` | Central order facade, entry eligibility/count/cooldown checks, in-flight entry suppression, emergency entry-limit bypass |
| `backend/kite_client.py`, `request_policy.py` | Broker API adapter, mixed response casing, historical order cache, priority token bucket, retries/circuit breaker and timeout reconciliation |
| `backend/risk_manager.py` | Sizing, daily-loss latch and persistence, broker P&L reconciliation, exposure/sector/correlation checks |
| `backend/config.py`, `journal.py` | Global settings, atomic active-trade/risk JSON, SQLite WAL trade/event journal |
| `backend/trading_costs.py` | Reusable intraday equity fee calculation and signed slippage attribution |
| `backend/analytics.py`, `calibration.py` | Descriptive trade statistics, chart reconstruction, simplified what-if, historical score-bucket outcome frequency |
| `backend/backtesting/` | Separate raw-strategy runner and OHLC stop/target broker; metrics, rolling-window validator, regression helper |
| `backend/mock_kite_client.py`, `ticker.py` | UI-development synthetic book/ticks; production order updates are forwarded to the renderer, not consumed by engine recovery |
| `backend/agent_gateway.py`, `llm_client.py` | Latent proposal route with deterministic checks; active LLM use is journal post-mortem text |
| Tests/config/build | Offline pytest suite, small frontend settings test, Ruff/ESLint/TypeScript/Vite/CI and installer scripts |

### 2.2 Current entry flow

```text
NIFTY universe + custom watchlist
  -> quote screener (top 12, open/pending symbols preserved)
  -> 5m scanner -> raw strategy events
  -> oscillator + breakout aggregation -> regime-compatible playbook
  -> score + historical outcome-frequency lookup
  -> signal UI; auto entry if score >= 70 and probability absent or >= .60
  -> execute_signal -> sizing/exposure -> gateway -> LIMIT order
  -> fill/position polling -> protective SL -> confirmation -> journal/snapshot
```

Evidence: `trading_engine.py:396–707`, `scanner.py:118–242`.
`scan_watchlist` calculates symbols concurrently, but its `on_signal` callbacks
are invoked in the consuming `as_completed` loop; fill polling can block that
consumer and therefore the same thread that normally monitors risk.

Other entry/adoption routes:

- **Take Trade:** renderer sends an entire signal dictionary to `execute_signal`.
  It has no server-owned signal lookup/expiry validation; a supplied quantity
  bypasses the margin-aware sizing function.
- **Manual order form:** `main.place_order` calls the gateway directly and labels
  all orders entries. It bypasses engine thesis/protection creation, and its
  frontend camelCase request does not match broker keyword parameters.
- **Untracked broker position:** auto mode tries to adopt every nonzero net
  position using default percentage stop/target, keyed by symbol. Confirmation
  mode does not adopt. The current response-casing defect prevents ordinary
  camelCase positions from passing its average-price check.
- **AgentGateway:** validates/clamps a proposal and calls `execute_signal` with
  `signal_score=100`. Repository call-site search found no active application
  caller; this is a latent integration, not evidence of current LLM-driven trades.
- **Backtest:** a raw strategy signal queues a market fill at the next bar's open
  with 10% cash sizing. This is not the production entry stack.

### 2.3 Inventory of current exit and protection paths

| Path | Trigger and current behavior | Relevant source |
|---|---|---|
| Broker protective order | SL by default, with a 1% limit-price buffer; independent of app while working | `trading_engine._place_protective_stop`, lines 1338–1436 |
| App stop | Polled `lastPrice` crosses stored `sl`; live reread then exit | `monitor_positions`, 728–885 |
| App target | Polled `lastPrice` crosses stored target; exits entire position | same |
| Resistance/support | One candle tests a prior 20-bar extreme and closes back inside; level only needs to be between entry and target | `_check_resistance_exit`, 898–967 |
| Opposing triggers | At reevaluation, at least two opposing aggregated events and no supporting event | `_reevaluate_positions`, 1134–1140 |
| Weak conviction | No supporting event, current loss, elapsed-time threshold | same, 1142–1148 |
| Time breakeven | Elapsed time alone assigns stop to entry; broker modification follows local mutation | same, 1150–1158; `_tighten_to_breakeven` |
| Daily loss/session | `should_square_off` -> `square_off_all`; loop stops immediately after submitting exits | `_run_loop`, 344–348; `risk_manager.py:186–247` |
| Stop failure | Entry/restart stop cannot be confirmed -> emergency MARKET request, optionally stop agent; failure can drop tracking without flat confirmation | `trading_engine.py:192–223,608–638` |
| External/manual close | Missing position plus grace period -> execution lookup -> journal close -> cancel remaining stop/remove tracking | `monitor_positions`, `_journal_external_close` |
| Pending exit completion | COMPLETE order deletes tracking; rejection/cancellation resets flags; partial residuals are not modeled | `_sync_exit_pending_status`, 1520–1567 |
| Restart | Reloads snapshots, checks position side, replaces missing stops, keeps selected working exits | `reconcile_active_trades`, 87–236 |
| Operator UI exit | Dashboard handler only logs; no effective exit request | `Dashboard.tsx:46–48` |
| Simulation | OHLC stop-first/target-second checks plus final-test liquidation; no live thesis/time/session exit policy | `simulated_broker.py:176–217`, `backtest_engine.py:116–128` |

The shared app exit helper cancels the protective order before submitting a
marketable LIMIT; on an exception it tries MARKET. Cancellation is not confirmed,
and unknown placement outcomes are retried at two layers. This must be repaired
before placing a more sophisticated decision policy above it.

### 2.4 Why current management is incoherent

- Entry triggers represent **events**, not the enduring state of a trend. A
  successful EMA crossover should normally disappear as an entry signal on the
  next bar. Its disappearance says nothing by itself about thesis validity.
- The original playbook, key level and expected behavior are not inputs to normal
  exits. `original_strategy` is stored but does not control these rules.
- Oscillator/breakout deduplication is useful, but trend events still count as
  separate opposing signals; regime and price structure do not arbitrate them.
- An empty market-data fetch becomes zero supporting signals and can cause an
  exit. Uncertainty is being converted into adverse evidence.
- Resistance uses raw `df.iloc[-2]` rather than the scanner's explicit candle
  completion filter. It can act on a pre-entry candle and repeats on the monitor
  cadence. The `ltp` argument does not affect its decision.
- Early `continue` branches skip reevaluation timestamp/event updates, producing
  repeated decisions. Confirmation mode suppresses some thesis exits but still
  changes stops and takes other exit paths.
- No live MFE/MAE or management-phase state exists. `trailing_sl` merely suppresses
  resistance exits; it does not implement a trailing stop.
- Accounting, broker contracts, lifecycle recovery and simulation assumptions
  contain defects independently of the trading heuristics. See audit F01–F12.

## 3. Proposed boundaries and component changes

Use ordinary Python data classes/enums and existing pandas/SQLite infrastructure.
No distributed service, event bus product or new database is required.

| Component | Add/change | Responsibility |
|---|---|---|
| `backend/broker_models.py` | Add | Typed position, order, fill and snapshot contracts; explicit unavailable/unknown state; compound identities |
| `backend/accounting.py` | Add | One fill/fee-derived P&L projection shared by risk, journal and research |
| `kite_client.py` | Change boundary | Normalize SDK responses to Python contracts; convert to frontend DTOs only at RPC; separate current order snapshot from archive |
| `backend/order_lifecycle.py` | Add, extract narrow execution logic | Durable intent/tag tracking; protection handoff; residual quantities; bounded cancel/replace/reconcile state machine |
| `execution_gateway.py`, `request_policy.py` | Preserve and strengthen | Single broker mutation facade; server-owned order roles, critical priority, outcome-aware retry policy |
| `backend/session_clock.py` | Add | Explicit exchange calendar/time, deadlines and injected event clock |
| `backend/risk_rules.py` | Add | Pure hard-risk predicates shared by the fast supervisor and exit engine; RiskManager still owns account risk state |
| `backend/market_context.py` | Add | Candle validation/completion, 15m as-of context, causal structure/features and data-quality state |
| `backend/exit_management/models.py` | Add | Immutable thesis and policies, lifecycle state, evidence, decisions, versioned serialization |
| `backend/exit_management/thesis.py` | Add | Build thesis from the exact accepted entry decision and causal context; bind actual fills |
| `backend/exit_management/evidence.py` | Add | State observations by family, dependency tags, supporting/contradicting/unknown evidence |
| `backend/exit_management/engine.py` | Add | Pure hard-risk/thesis decision functions and state reducer; precedence and hysteresis |
| `backend/exit_management/profiles.py` | Add | Three small setup-management profiles; pinned versions and a conservative unknown-thesis profile |
| `trading_engine.py` | Change orchestration | Separate entry work from risk/position supervision; build snapshots, call policy, submit intents, consume broker facts |
| `risk_manager.py` | Preserve and correct | Daily kill latch, sizing and exposure based on normalized broker quantities/marks/fills, pending entry reservations |
| `scanner.py`, `playbooks/` | Preserve, narrow extraction | Expose pure production entry evaluation and thesis metadata; keep strategy calculations and selection behavior stable initially |
| `backend/entry_decisions.py` | Add in parity phase | Extract the production entry calculation with injected context/config/artifacts, retaining existing strategy/playbook formulas |
| `journal.py`, `config.py` | Extend | Transactional lifecycle/intent/trace storage in existing DB; retain atomic JSON as migration/checkpoint mechanism |
| `backend/replay.py`, `backend/exit_quality.py` | Add | Reexecute decisions and bounded counterfactuals; shared exit-quality definitions |
| `backtesting/` | Adapt | Event driver and explicit execution model call shared policy/reducer; raw-strategy lab remains labeled separately |
| `backend/backtesting/paper_broker.py` | Add adapter | Live-market-data paper execution through the same lifecycle contracts; isolated namespace |
| Frontend/shared RPC | Extend | Authoritative entry/risk status, protection and exit-pending state, thesis/evidence/replay, working operator reductions |

Retire the count-based reevaluation, standalone rejection exit and unconditional
breakeven paths **only after shadow/parity acceptance**. Remove or adapt the
currently unused `evaluate_invalidation` methods so a second, conflicting exit
implementation cannot become active later. Keep fixed target behavior as an
explicit frozen profile setting during initial comparison; profile changes are
separate experimental treatments.

## 4. Event and authority flow

```text
Broker facts / fresh quotes / session deadline / operator emergency
                 |
                 v
       Hard-risk supervisor (independent scheduler)
                 |         strongest action wins
                 +--------------------------+
                                            v
Completed 5m bars -> market context -> exit policy -> decision + next state
Completed 15m context ----^                      |
Frozen entry thesis ----------------------------+
                                                v
                       journal transaction: state + trace + intent
                                                |
                                                v
                              order lifecycle coordinator
                                                |
                                                v
                         existing execution gateway / broker gateway
                                                |
                                                v
                        fills/order updates + position reconciliation
                                                |
                                                v
                              shared position-state reducer
```

The deterministic policy proposes `HOLD`, `REQUEST_EXIT` or `TIGHTEN_STOP`;
`RECONCILE_REQUIRED` accompanies unknown execution state. It never calls the
broker. The coordinator validates current state/version and exposure immediately
before a mutation. A hard intent latches until flatness and order cleanup are
confirmed; it cannot be cancelled by a later healthy candle.

Use one serial owner per broker position, with short lock/compare-and-swap state
updates, not network calls inside a global trade lock. Fill/order callbacks enqueue
facts; they do not concurrently edit dictionaries. Polling remains a recovery
fallback because streams can disconnect or arrive out of order.

Keep the stdin/control dispatcher responsive: long backtest/replay/LLM requests
run in a bounded research worker and return through their original response IDs.
An operator emergency command must be admitted without waiting for those jobs.
The risk supervisor remains independent of both RPC research and entry scanning.

Normal decisions commit state, trace and intent atomically. A database failure
suspends new entries and normal modifications; a hard-risk action still proceeds
through a best-effort emergency record/tag and subsequent reconciliation. This
exception is itself observable. Storage failure must never delay a protective
action waiting for analytics or an LLM.

## 5. Hard risk versus thesis management

| Category | Authority / cadence | Result |
|---|---|---|
| Hard/catastrophic stop or broker stop | Fresh executable data/broker events; no candle confirmation | Reduce/flatten and track actual fills |
| Daily loss latch / emergency flatten | Account-level supervisor; persists across same-session restart | Cancel pending entries; reduce designated account exposure until terminal |
| Forced session deadline | Exchange clock event, even if there is no new candle | Latched close intent, continue recovery after deadline |
| Protective/execution failure | Coordinator + risk supervisor | Reconcile immediately; restore valid protection or flatten known residual; suspend entries |
| Thesis invalidation | Completed 5m, entry-specific acceptance rules | Normal exit intent after confirmation |
| Thesis weakening | Completed 5m | HOLD with evidence/counters; sometimes a validated tighter stop |
| Profit protection | Completed 5m structure + MFE/R + context | Ratcheting stop or confirmed exhaustion exit |
| Normal pullback/consolidation | Completed 5m and 15m context | Explicit HOLD, including why adverse evidence was insufficient |
| Time management | Valid completed-bar count and session context | Stagnation review, not clock-only breakeven |

“Unrecoverable execution failure” creates an authoritative recovery/flatten
obligation, not a claim that an unavailable broker has executed a trade. With an
unknown outstanding order, resolve its outcome before duplicating exposure-changing
requests. Preserve broker protection while the outcome is unknown where possible.

## 6. Position, stop and recovery lifecycle

Use orthogonal axes rather than a long mutually exclusive list containing both
profit and execution states. A trade can be profitable, pulling back and protected
at once. The full transition specification is [STATE_MACHINE.md](STATE_MACHINE.md).

- Exposure: `ENTRY_PENDING -> OPEN -> EXIT_PENDING -> FLAT_PENDING_RECONCILIATION
  -> CLOSED`, with `RECOVERY_REQUIRED` and `ENTRY_ABORTED` branches.
- Thesis health: `UNKNOWN / VALID / WEAKENING / INVALIDATED`.
- Development: `EARLY / DEVELOPING / FAVORABLE / PULLBACK / CONSOLIDATING`.
- Protection: actual confirmed broker stop, requested stop, pending modification,
  residual protected quantity, and protection quality tracked separately.

Never infer the original discretionary thesis of an adopted/manual position.
Mark its provenance `UNKNOWN` or `LEGACY_PARTIAL`; retain existing valid protection
and forced-risk/session management. New normal thesis rules only apply when their
required anchors are available. Ownership is explicit: net broker risk includes
all exposure, but unrelated CNC/NRML positions are not silently assigned the app's
intraday playbook. Emergency account-wide scope is an explicit command/configured
mandate, not a side effect of symbol matching.

At startup: restore namespace/account state; obtain successful positions, current
orders and fills; reconcile intents and residual exposure; resolve conflicting
protective/exit orders; establish supervision; then permit entries. Corrupt
snapshots and unknown outcomes remain recovery states. Existing positions do not
need a new entry signal to resume management.

## 7. Live/research equivalence and observability

The shared engine consumes canonical events. Adapters differ only in market-data
availability and execution simulation, not in exit reasoning. Record both
`market_time` and `received_at`, policy/config/code versions, normalized input
hashes, bar IDs, protection/fill state and all candidate/suppressed actions.

Two forms of parity are distinct:

1. **Decision parity:** identical inputs/state produce identical decisions and
   transitions in every mode.
2. **Execution realism:** different fills/latency may legitimately change later
   state; the simulator must disclose those assumptions rather than claim live
   fills were identical.

Live tick extrema and coarse OHLC extrema are labeled by source/resolution. An
exit bar does not automatically contribute its entire later high/low to observed
MFE/MAE. Replay answers “why HOLD?” from recorded facts and “what if?” from a
separate forward simulation with frozen risk constraints.

The LLM may interpret traces or suggest experiments offline. It receives no
capability to change active stops, bypass risk gates or issue exit orders.

## 8. Preservation and rollout

Preserve the useful foundations: gateway boundary, entry-limit bypass for genuine
reductions, token-bucket priority design, circuit states, timeout-reconciliation
concept, atomic JSON replacement, WAL journal, session VWAP, aggregation raw
provenance, conservative stop-first baseline, next-bar entry timing and offline
synthetic strategy tests. Their specific integration defects need targeted fixes;
their existence is a reason to avoid wholesale replacement.

Roll out per-policy/per-position versions: fixtures -> paired replay -> live
shadow -> isolated paper -> limited controlled live deployment. Shadow produces
records only; it never submits or modifies orders. A switch affects new positions;
existing positions retain their pinned policy or a recorded migration at a safe
state boundary. Rollback cannot restore a looser stop or erase an exit obligation.

Trading behavior remains a hypothesis until the validation gates pass. In
particular, patience may increase adverse excursion or giveback. Improvement must
include tail risk, confirmed invalidation latency and execution reliability, not
only fewer exits or higher historical P&L.
