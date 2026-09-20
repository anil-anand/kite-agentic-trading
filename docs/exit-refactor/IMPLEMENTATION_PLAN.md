# Exit-refactor implementation plan

## Execution contract for the next coding agent

This is a ten-phase plan, not permission to implement all findings indiscriminately.
Within each phase, take the listed slices as small reviewable changes. Keep the
application runnable and preserve old record readability at every boundary.
Do not replace the trading engine, broker gateway, SQLite journal, strategy set
or Electron architecture wholesale.

Read ARCHITECTURE, DECISION_MODEL, STATE_MACHINE and the referenced audit findings
before changing behavior. Record policy versions and intended behavior in each
implementation change. Use broker fakes/temporary storage only during tests.

For every Python slice run Ruff lint/format checks and the full pytest suite;
for frontend slices run lint, typecheck, build and relevant frontend tests. Add
meaningful contract/scenario tests when changing trading/risk logic. Do not
preserve a demonstrably unsafe expectation merely because an existing test
asserts it; replace it with the documented invariant and a regression explanation.

### Dependency graph

```text
1 broker/accounting -> 2 execution recovery -> 3 continuous risk/session control
                                             |
                         4 causal market context
                                             |
                         5 thesis/state/trace persistence
                                             |
                         6 deterministic decision model
                                             |
                         7 live shadow integration
                                             |
                         8 shared paper/backtest/replay
                                             |
                         9 quality metrics/operator explanations
                                             |
                        10 robustness, promotion, retirement
```

Independent security/storage fixes identified below are live-promotion dependencies,
not extra exit-policy phases. Phases 4 and 5 can be developed independently after
their contracts are agreed, but phase 6 integration requires both.

## Phase 1 — Canonical broker contracts and coherent risk/accounting inputs

**Responsibility:** establish that the engine operates on real prices, quantities,
identities and exposure, with explicit unknown state. Fix F01/F07/F08/F09/F10
foundations before changing normal trading decisions.

**Modify:**
`backend/kite_client.py`, `backend/main.py`, `backend/trading_engine.py`,
`backend/risk_manager.py`, `backend/trading_costs.py`, `backend/journal.py`,
`backend/mock_kite_client.py`, `src/shared/types.ts`,
`src/main/ipc-handlers.ts`, `src/renderer/components/OrderForm.tsx` as required for
the corrected boundary.

**Create:** `backend/broker_models.py`, `backend/accounting.py`,
`backend/tests/test_broker_contracts.py`,
`backend/tests/test_accounting_reconciliation.py`,
`backend/tests/test_rpc_contracts.py`.

**Small slices:**

1. Define canonical positions/orders/fills/snapshot quality and compound identity.
   Normalize SDK fields once. Keep renderer conversion explicit, with compatibility
   DTOs rather than mixed-key dictionaries.
2. Split current orders from display history. Consume correct actual fill/mark/
   turnover fields. Make unknown execution price/P&L nullable and exclude legacy
   placeholder records from research views pending repair.
3. Reuse TradingCostCalculator through one accounting service; pin rate/rounding
   version and order-level fee scope. Align fast risk/reconciliation definitions.
4. Use reconciled current broker quantity/marks and pending-entry reservations for
   admission, not active-record entry notional. Classify protective/reducing orders
   by server ownership. Unknown correlation/mark data follows an explicit conservative
   policy. Reserve open-position/count capacity atomically.
5. Validate enabled entry RPC geometry/finite values/quantity/freshness and cap
   advisory quantity with live margin/risk. Block new risk until reconciled.
   Keep the broader manual ticket product redesign separate.

**Dependencies:** none beyond existing code; agreement on accounting units and
supported NSE/MIS scope. No real credential/account fixtures.

**Tests:** synthetic probes listed in REPOSITORY_AUDIT become regression tests;
SDK-shaped long/short/partial order contracts; correct fill VWAP; unknown-price
exclusion; current/pending/reducing exposure; correlation unavailable; concurrent
admission and fee/P&L identity. Update existing hybrid-key fakes.

**Acceptance:** zero-price placeholders cannot enter financial metrics; actual
positions contribute quantity and current marks; all enabled entry routes receive
the same essential admission checks; contract tests cover Python-to-renderer DTOs.

**Expected behavior:** some previously accepted trades may be correctly rejected
by actual risk limits. Entry indicator formulas and normal exit policy remain the
comparison control while foundation defects are corrected.

**Implementation evidence:** the canonical broker, accounting, journal, RPC,
admission and lifecycle suites in `backend/tests/` exercise the production
adapters through synthetic SDK records and temporary storage. They cover missing
prices and timestamps, stale marks, partial/unknown executions, reducer exposure,
durable ownership/linkage, transaction rollback, cooldowns and restart recovery.
`test_execution_accounting_integrity.py` additionally checks that incomplete entry
ledgers cannot become verified outcomes, actual fill prices replace signal-price
fallbacks, late execution times repair the journal, and unknown execution time
does not disable existing thesis timers. Renderer polling scenarios in `tests/`
exercise complete, partial and unavailable snapshots through the real components.
`test_session_accounting_admission.py` checks execution/turnover agreement,
per-order fee grouping and daily-loss enforcement from the admission snapshot;
`test_entry_boundary_contracts.py` covers malformed proposal prices. The DEV
adapter tests use the same canonical identity and mark contracts.
Normal entry indicators and exit thresholds remain the comparison control.

**Final phase-1 validation:** 620 Python tests and both renderer polling tests
passed. Ruff lint/format, frontend lint/typecheck, an isolated full frontend build,
and `git diff --check` passed. Frontend lint retains four existing unused-symbol
warnings. No real broker calls or orders were used.

This phase does not authorize live promotion: the CRITICAL phase-2 stop-fill/cancel
handoff and separate [F26 DEV/account storage isolation](REPOSITORY_AUDIT.md#f26--dev-trading-is-not-isolated-from-live-persistent-state)
gates remain open.

## Phase 2 — Recoverable order intents, residual fills and protective-stop handoff

**Responsibility:** an exit/protection action must not duplicate, reverse or abandon
exposure. Address F02/F04/F05/F06/F25; preserve the gateway and reconciliation role.

**Modify:** `backend/execution_gateway.py`, `backend/kite_client.py`,
`backend/request_policy.py`, `backend/trading_engine.py`, `backend/journal.py`,
`backend/config.py`; existing execution, persistence, external-close and
capital-safety tests.

**Create:** `backend/order_lifecycle.py`,
`backend/tests/test_order_lifecycle.py`; additive intent/fill schema migrations
inside the existing journal migration mechanism, made explicit/versioned.

**Small slices:**

1. Persist intent, unique attempt tag, role, quantity and state version before
   submission. Add idempotent fill ledger and current intent projection.
2. Distinguish definite rejection from UNKNOWN request outcome. Reconcile by
   stable identity; eliminate blind timeout retry and LIMIT->MARKET fallback
   unless the earlier attempt is proven not live.
3. Serialize reductions per broker position. Implement broker-supported protective
   order handoff with cancel confirmation or validated modification; reread fills/
   residual after the handoff, not just before it.
4. Protect partial entry fills immediately; track cancellation remainder/late
   fills. On partial exits, retain intent and correctly size residual protection
   or reduction. Treat stop COMPLETE as a possible fill, not automatic failure.
5. Never drop tracking after unconfirmed emergency submission/failure. Startup
   resumes intents and keeps unknown broker/ownership state in recovery. Confirm
   adopted protection and explicit ownership; prevent same-symbol product collisions.
   Consume protective-order updates and periodically verify fresh side/quantity/
   type/coverage, including manually cancelled or triggered-but-unfilled stops.

**Dependencies:** phase 1. Broker capability semantics must be validated using
scripted contract fixtures; do not assume reduce-only/OCO/atomic modification.

**Tests:** timeout accepted/late-visible; cancel/stop-fill race; stop immediately
COMPLETE; partial entry/exit then reject/cancel; late entry fills; broker mismatch;
missing/stale order snapshot; crash injection at every persist/send/ack boundary;
concurrent normal/hard exits; fill deduplication; terminal-flat/order-clean checks.

**Acceptance:** a known filled share is always represented by a lifecycle and
protection/recovery obligation; submission does not imply closure; UNKNOWN never
authorizes a fresh untracked duplicate; no scenario opens an unintended opposite
position; entry/protection/exit identity survives restart.

**Expected behavior:** uncertain execution remains visibly pending/recovering.
Hard exits retain urgency and continue recovery instead of claiming success or
removing state. This is a targeted coordinator extraction, not a broker rewrite.

## Phase 3 — Continuous hard-risk supervision and authoritative operator control

**Responsibility:** hard risk/session obligations run independently of entry work,
mode changes, long RPC research jobs and temporary renderer loss. Address F03/F11,
recovery part of F24, and critical operator controls in F27.

**Modify:** `backend/trading_engine.py`, `backend/risk_manager.py`,
`backend/execution_gateway.py`, `backend/request_policy.py`, `backend/ticker.py`,
`backend/main.py`, `src/main/python-bridge.ts`, `src/main/ipc-handlers.ts`,
`src/main/preload.ts`, `src/shared/ipc-channels.ts`, `src/shared/types.ts`,
`src/renderer/hooks/useKiteAPI.ts`, `src/renderer/pages/AgentControl.tsx`,
`src/renderer/pages/Dashboard.tsx`, `src/renderer/pages/Orders.tsx`,
`src/renderer/stores/trading-store.ts`.

**Create:** `backend/session_clock.py`, `backend/risk_rules.py`,
`backend/tests/test_risk_supervisor.py`, `backend/tests/test_session_clock.py`,
`tests/lifecycle-contracts.test.ts`.

**Small slices:**

1. Extract hard-risk predicates into pure `risk_rules` using explicit snapshots/
   policy/clock. Both fast live supervisor and later shared exit engine call these
   same rules. RiskManager remains the account-state/admission service.
2. Split entry worker from bounded supervisory scheduling; queue broker events
   for per-position reduction and retain polling fallback. Avoid broker IO under
   a global trade lock. Propagate CRITICAL role to protection/flatten and their
   required reconciliation through the broker facade.
   Dispatch long research RPC work to a bounded worker so stdin/control dispatch
   remains responsive; preserve response IDs. Operator close/emergency admission
   cannot queue behind LLM generation. A richer research-job UI can follow later.
3. Implement session calendar/deadline and persisted daily-loss latches. Cancel
   pending entries on flatten and keep managing until flat/order-clean, even past
   cutoff. New session requires verified reset; residual obligations carry over.
4. Expose backend-acknowledged entry pause/effective mode/manual close/cancel and
   scoped emergency commands. Keep management active in confirmation/paused-entry
   modes. Fix root event subscription ownership/cleanup and pending UI outcomes.
5. Add backend readiness/generation handshake and pending-request rejection on
   unexpected child death. Restore authentication through the trusted main process,
   reconcile/resume supervision, then advertise readiness. Startup cannot override
   a protective-failure halt. Defer broad async research UI to its separate task.

**Dependencies:** phases 1–2; security work supplies secret-free renderer identity
and trusted rehydration before live promotion.

**Tests:** blocking scanner/fill/LLM job while risk timer fires; open circuit;
paused/confirm mode and disconnected renderer; actual close/cancel acknowledgements;
multiple start/stop calls; host timezone invariance; holidays/rollover; forced
cutoff without candles; restart/late fill; invalid settings/mode changes.

**Acceptance:** measurable supervisory deadline budget independent of scanning;
one pure hard-risk rule set; hard reduction never blocked by entry limits or
normal HOLD; square-off terminal proof; UI effective state matches backend;
restart cannot advertise recovered trading before reconciliation.

**Expected behavior:** Stop Agent pauses new entries and reports ongoing position
supervision. Explicit close/emergency actions are executable and remain pending
until reconciled. No new discretionary exit behavior is necessary yet.

## Phase 4 — Causal completed-candle market context

**Responsibility:** every normal decision receives validated 5m/15m inputs with
known availability and structure; address F20 and context subset of F14.

**Modify:** `backend/scanner.py`, `backend/regime_classifier.py`,
`backend/indicators.py`, `backend/config.py`, `backend/ticker.py`;
`backend/strategies/breakout_evidence.py` only for explicitly versioned common
VWAP/context semantics, with its entry-output impact recorded.

**Create:** `backend/market_context.py`, `backend/tests/test_market_context.py`.

**Small slices:**

1. Normalize/sort/deduplicate/validate OHLCV/session timestamps and expose quality/
   freshness/availability instead of empty-data directional counts.
2. Centralize 5m completion; derive 15m from complete contiguous session-aligned
   bars, with as-of join and staleness limits. Keep immutable revision-aware cache.
3. Build causal setup range and known-at swing levels plus continuous VWAP,
   momentum/trend, volatility and participation observations from existing inputs.
4. Preserve raw stateless regime output; add transition candidate/persistence in
   context state, with directional/quality features. Do not turn regime alone into
   a new entry/exit gate. Use one documented session VWAP definition.

**Dependencies:** phase 3 clock; feature contract from DATA_AND_REPLAY.

**Tests:** incomplete final bars, duplicate/revised/out-of-order bars, session gap,
zero volume, invalid prices, stale successful response, 09:15 anchoring, partial
15m, delayed data, known-at pivot, future-prefix invariance and immutable cache.

**Acceptance:** every input has as-of/source identity and quality; future HTF or
right-confirmed structure never leaks; no missing data becomes adverse evidence;
existing entry calculation behavior is characterized for any intended semantic change.

**Expected behavior:** normal management waits for usable completed context.
Hard supervision continues regardless. No new indicators are needed.

## Phase 5 — Immutable thesis, lifecycle state and replayable journal

**Responsibility:** make the position remember its premise and history; implement
the contract/state/persistence portions of F12/F13/F22/F25/F31.

**Modify:** `backend/trading_engine.py`, `backend/journal.py`, `backend/config.py`,
`backend/playbooks/base.py`, `backend/playbooks/trend_pullback.py`,
`backend/playbooks/breakout.py`, `backend/playbooks/mean_reversion.py`,
`backend/scanner.py`, `backend/order_lifecycle.py`.

**Create:** `backend/exit_management/__init__.py`,
`backend/exit_management/models.py`, `backend/exit_management/thesis.py`,
`backend/tests/exit_management/test_thesis.py`,
`backend/tests/exit_management/test_state_machine.py`,
`backend/tests/test_exit_persistence.py`.

**Small slices:**

1. Add explicit immutable thesis/profile/position/evidence/decision types and the
   orthogonal lifecycle reducer. Reuse phase-2 order/fill contracts.
2. Capture the exact selected entry evidence/causal anchors and truthful setup
   variant before submitting; bind fill VWAP/initial risk after terminal entry.
   Track provisional risk for partial fills. Do not refetch rationale after fill.
3. Persist thesis, state/checkpoint, input/policy references and decision records
   in explicit transactions with order intents. Add indexes/version migrations.
4. Back up/import legacy snapshots and aliases. Preserve existing protection/
   objective; unknown thesis/quantity remains unknown until recoverable. No
   retrospective invention of structure/MFE or automatic stop relaxation.

**Dependencies:** phase 2 ledger/coordinator; phase 4 context (models can precede it).

**Tests:** state transition table, fill/state idempotency, immutable R/config,
transaction crash boundaries, concurrent checkpoint version ordering, migration
from missing/legacy fields, corrupted record recovery and namespace isolation.

**Acceptance:** every new managed position has a thesis/provenance/policy version
and protected filled quantity; all state transitions are attributable; restart
restores counters/extrema/intents; legacy data remain readable without fake facts.

**Expected behavior:** bookkeeping becomes explicit while approved legacy exit
semantics can still run as control. Unknown legacy trades use bounded management.

## Phase 6 — Pure deterministic thesis and profit-management engine

**Responsibility:** implement DECISION_MODEL, STATE_MACHINE and EXIT_REASON_CODES,
using shared hard-risk rules and a small profile set. No broker/database calls.

**Modify:** `backend/exit_management/models.py`,
`backend/exit_management/thesis.py`; playbook contract only as needed to refer to
management profile/structural premise. Existing entry formulas remain stable.

**Create:** `backend/exit_management/evidence.py`,
`backend/exit_management/engine.py`, `backend/exit_management/profiles.py`,
`backend/tests/exit_management/test_evidence.py`,
`backend/tests/exit_management/test_engine.py`,
`backend/tests/exit_management/test_scenarios.py` and small immutable scenario data.

**Small slices:**

1. Evidence families/dependency grouping/UNKNOWN semantics and full predicate trace.
2. Entry-specific structural invalidation, weakening, confirmation and recovery
   hysteresis; distinct-bar processing and latched invalidation.
3. Development/pullback/consolidation interpretation, immutable R and observed/
   completed-close excursion updates.
4. Monotone structural profit protection, fixed objective versus runner review
   semantics, expected-progress time reviews and late-session management; call
   phase-3 hard rules for forced events.

**Dependencies:** phases 3–5. Profiles use the candidate research defaults and
small parameter budget; do not invent per-symbol optimized thresholds.

**Tests:** all applicable S01–S30 paths, mirrored long/short, duplicate correlated
indicators, missing data, same-bar repetition, pre-entry rejection, stop ratchet,
already-profitable stop, hard-risk preemption and deterministic serialization.

**Acceptance:** single weak indicator/rejection/absent entry event never causes
a normal exit; decisive defined failure does; no HOLD cancels a hard/pending exit;
no profit/time rule loosens confirmed protection; identical inputs give identical
decisions including HOLD reasoning.

**Expected behavior:** patient intact-thesis holds, measurable weakening, confirmed
failure exits and justified profit protection. Trading benefit remains unproven
until later validation; this phase does not activate the policy live.

## Phase 7 — Live orchestration integration in shadow mode

**Responsibility:** connect the pure engine to existing live data/state/supervision,
record comparable decisions without order authority, then validate the integration.

**Modify:** `backend/trading_engine.py`, `backend/scanner.py`,
`backend/order_lifecycle.py`, `backend/journal.py`, `backend/config.py`,
`backend/main.py`, `backend/ticker.py`.

**Create:** `backend/tests/test_exit_live_integration.py` and fixed live-like event
fixtures. Reuse previous modules instead of adding a second live exit engine.

**Small slices:**

1. Schedule a thesis evaluation exactly once per eligible completed bar for every
   managed position, even if it is no longer a new-entry watchlist candidate.
2. Persist all state/trace results and suppressed actions. Broker/quote events
   update fills/extrema/protection independently of normal-bar evaluation.
3. Add a per-position pinned `legacy_control / shadow / candidate` policy mode.
   Shadow has no callable broker mutation capability; record old and candidate
   reasons side by side with input-quality provenance.
4. Test candidate intents through the common coordinator in scripted integration;
   keep live activation disabled until phases 8–10 gates pass.

**Dependencies:** phases 1–6. Broader security/isolation fixes remain promotion gates.

**Tests:** no shadow order/modify/cancel calls; hard supervisor remains active;
concurrent fills between evaluation/dispatch; mode/config pinning; catch-up after
outage without retroactive orders; every decision joins to state/input references.

**Acceptance:** complete trace coverage and bounded decision latency; no missing
position management because scanning skipped a symbol; confirmed/requested stop
states remain distinct; old and candidate paths cannot both submit exits.

**Expected behavior:** observable candidate HOLD/weakening/exit recommendations in
shadow, with existing approved control behavior executing through corrected safety.

## Phase 8 — Shared paper, backtest and decision replay adapters

**Responsibility:** candidate policy is executable identically in every mode;
resolve F16/F17 and counterfactual foundations without parallel policy logic.

**Modify:** `backend/backtesting/backtest_engine.py`,
`backend/backtesting/simulated_broker.py`, `backend/backtesting/regression_suite.py`,
`backend/scanner.py`, `backend/strategies/base.py` for injected metadata/config,
`backend/main.py`, `backend/trading_costs.py`, `backend/order_lifecycle.py`.

**Create:** `backend/replay.py`, `backend/entry_decisions.py`,
`backend/backtesting/paper_broker.py`,
`backend/tests/test_exit_parity.py`,
`backend/tests/test_simulation_execution.py`,
`backend/tests/test_exit_replay.py`.

**Small slices:**

1. Replay fixed entry/fill opportunities through common context/policy/reducer;
   add identical-input parity fixtures shared with phase 7.
2. Adapt simulated broker to canonical order/fill events and coordinator interface;
   implement declared gap/stop-limit/partial-fill/latency/ambiguity model with
   single-application slippage, fees and residual protection.
3. Add session/deadline/hard-risk events, synchronized symbols, pending reservations
   and MTM equity; treat missing final data as explicit censoring/recovery.
4. Extract production entry evaluation into `entry_decisions` with injected
   configuration/clock/calibration artifact inputs, preserving raw strategy and
   playbook formulas. Pass isolated frames; remove wall-clock metadata from
   reproducible entry outputs. Keep raw-strategy lab labeled separately.
5. Add isolated real-data paper adapter and exact/alternative-policy replay.
   Retain UI mock as UI mock; namespaces prevent live broker reachability.

**Dependencies:** phases 1–7; mode isolation before paper/live coexistence.

**Tests:** same candidate decisions/state hashes across five modes for identical
inputs; no look-ahead/retroactive stops; both-touched ambiguity; adverse/favorable
gaps; partial fills/latency; session/end-of-data; cash/net identity; deterministic
multi-symbol order; no access to live journal/calibrator from replay.

**Acceptance:** one candidate exit engine and common lifecycle coordinator;
differences in outcomes attributable to declared input/fill assumptions, not
separate decision rules. Dataset/execution/cost versions recorded per run.

**Expected behavior:** realistic, reproducible exit-only and full-stack comparisons,
with conservative ambiguity and honest uncertainty rather than ideal fills.

## Phase 9 — Exit-quality analytics and operator explanation

**Responsibility:** answer why HOLD/weakening/exit and distinguish missed continuation
from useful profit protection. Address F19/F22/F28 lifecycle subset.

**Modify:** `backend/analytics.py`, `backend/journal.py`,
`backend/backtesting/metrics_evaluator.py`, `backend/main.py`,
`src/shared/types.ts`, `src/shared/ipc-channels.ts`,
`src/main/ipc-handlers.ts`, `src/main/preload.ts`,
`src/renderer/pages/Journal.tsx`, `src/renderer/pages/Backtesting.tsx`,
`src/renderer/pages/Dashboard.tsx`, `src/renderer/pages/AgentControl.tsx`,
`src/renderer/components/PositionCard.tsx`,
`src/renderer/components/TradeReplayChart.tsx`,
`src/renderer/components/PnLDisplay.tsx`, `src/renderer/components/StatusBar.tsx`,
`src/renderer/stores/trading-store.ts`.

**Create:** `backend/exit_quality.py`, `backend/tests/test_exit_quality.py`,
`tests/exit-replay-contracts.test.ts`; small focused UI view components if needed.

**Small slices:**

1. Implement unit-explicit price/exposure MFE/MAE, captured net/gross R, giveback,
   time/latency and reason distributions with quality coverage.
2. Risk-constrained hold-N and alternative-policy simulations with retained stops,
   forced deadlines, costs, ambiguity/censoring and account-scenario labeling.
3. Cohort reports and full MTM drawdown/session-return definitions. Exclude
   unresolved/legacy fake-price records without hiding their counts.
4. Active-position thesis/health/context/evidence/protection panels; replay each
   HOLD/transition/intent and outcome. Fix misleading percent/probability/negative
   P&L labels. Display effective entry/supervision state and stale/unknown inputs.

**Dependencies:** phases 5/7/8; typed RPC and operator controls from phase 3.

**Tests:** hand-calculated long/short/partial/no-MFE/censored cases; +0.5R->+3R
with and without prior stop; +2R before reversal; gross/net comparison identity;
UI unavailable values/pending stops and backend reason mapping.

**Acceptance:** every candidate action can be replayed with its inputs/versions;
HOLD reasoning and weakening are inspectable; forward outcomes cannot enter the
original decision; exit-quality metrics distinguish opportunity cost and avoided
loss. User sees actual confirmed risk/execution state rather than only P&L.

**Expected behavior:** actionable explanations and honest research diagnostics.
LLM commentary remains optional, trace-linked and outside order authority.

## Phase 10 — Robustness validation, controlled activation and legacy retirement

**Responsibility:** establish evidence for promotion and retire conflicting normal
paths only after safety, parity and research gates pass.

**Modify:** `backend/backtesting/walk_forward.py`,
`backend/backtesting/regression_suite.py`, `backend/backtesting/metrics_evaluator.py`,
`backend/trading_engine.py`, `backend/scanner.py`, `backend/playbooks/base.py` and
the three playbooks' unused invalidation methods, `backend/config.py`,
`.github/workflows/ci.yml`, relevant Settings/operator labels and documentation.

**Create:** `backend/tests/test_walk_forward.py`, versioned research run manifests
and curated non-sensitive replay/regression fixtures in an agreed research-data
directory; avoid committing licensed/raw account datasets.

**Small slices:**

1. Feature-only warmup, disjoint OOS windows, optional small nested selection,
   purging/embargo and artifact freezing. Record trial count and untouched holdout.
2. Execute BACKTEST_PLAN's paired/full-portfolio studies, ablations, parameter
   perturbations, multi-symbol/regime/time periods and execution/cost/data stress.
3. Meet predeclared sample/materiality/tail-risk/operational gates; run shadow and
   isolated paper. Inconclusive evidence keeps candidate unpromoted.
4. Enable limited pinned policy cohort with rollback. Retire count-based
   `_reevaluate_positions`, standalone `_check_resistance_exit`, unconditional
   `_tighten_to_breakeven` policy and unused boolean playbook invalidation code.
   Reuse safe coordinator/stop helpers; do not delete recovery infrastructure.
5. Remove/deprecate obsolete configuration only with migration/clear operator
   semantics. Keep legacy reasons/data readable and frozen controls reproducible.

**Dependencies:** phases 1–9; all live-gating separate security/mode/manual-route
issues resolved; real broker assumptions verified to the extent supported.

**Tests:** train/test isolation, no warmup trades, nonoverlap/label horizons,
fixed-policy reproducibility, perturbation regression suite, policy pin/rollback,
new-session recovery and no duplicate surviving exit path.

**Acceptance:** documented OOS/paper results meet predeclared criteria including
tail risk and invalidation latency; candidate shares live/research code; replay
coverage complete; operational failures never erase hard obligations. Rollback
cannot loosen stops or revoke exits. No improvement claim based only on in-sample P&L.

**Expected behavior:** coherent, explained, validated management with strict
hard-risk authority. More holding time or fewer trades alone is not success.

## Separate work and explicit non-rewrites

Before live promotion, deliver focused fixes for F29 secrets/blank saves, F26
DEV/LIVE storage isolation and F30 privileged IPC/provider boundaries. Complete
any enabled manual entry protection/validation gaps from F09. These are not
opportunities to add LLM trade authority or replace native credential storage.

After the exit refactor: conservative correlation fallback can evolve into tested
signed concentration models; screener fallback/normalization, entry family/profile
semantics, async analytics jobs and bounded retention can be improved independently.
New probability models, market-breadth features, automated partial-profit policies,
pyramiding and portfolio optimization require separate research packages.

Keep the existing gateway, risk-manager authority, broker reconciliation purpose,
kill latch, emergency/protective mechanisms, SessionVWAP, aggregation raw provenance,
SQLite WAL/atomic JSON utilities, strategy tests and build stack. The roadmap
strengthens their contracts and isolates the exit decision responsibility.
