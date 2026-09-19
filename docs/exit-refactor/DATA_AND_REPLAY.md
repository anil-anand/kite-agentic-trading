# Data contracts, persistence, replay and exit-quality measurement

## 1. Boundary contracts

Python domain objects use snake_case. SDK responses are normalized once in the
broker adapter; frontend DTOs are explicitly serialized at the RPC boundary.
Avoid recursive casing conversions of arbitrary dictionaries, especially payloads
whose keys are instrument names, IDs or feature names.

All money/prices must be finite; quantities are integral and respect lot size.
Use tick-aware price normalization and declared fee rounding. UTC-aware instants
are stored; exchange-local session IDs/labels use Asia/Kolkata. Broker-naive
timestamps are localized by the adapter's explicit broker convention, not the
host timezone. Preserve raw, secret-free broker payload hashes for diagnosis.

### 1.1 Broker snapshot

```text
BrokerSnapshot:
  namespace, account_id, snapshot_id, fetched_at, observed_at
  positions: [BrokerPosition]
  current_orders: [BrokerOrder]
  fills: [BrokerFill]
  positions_quality, orders_quality, fills_quality
  completeness, stale_since, errors

BrokerPosition:
  account_id, exchange, instrument_id, tradingsymbol, product
  signed_quantity, average_price, last_price, mark_time
  realised_gross, unrealised_gross, buy_value, sell_value

BrokerOrder:
  broker_order_id, client_intent_tag, instrument/product identity
  side, order_type, original_quantity, filled_quantity, remaining_quantity
  price, trigger_price, status, exchange_time, received_at
  role: ENTRY | PROTECTION | REDUCTION | UNKNOWN

BrokerFill:
  broker_fill_id, broker_order_id, identity, side, quantity, fill_price
  exchange_time, received_at, allocated_position_id
```

`role` is established by app intent/ownership metadata, not trusted from renderer
input. A failure is `UNAVAILABLE/UNKNOWN`, never an empty successful snapshot.
A missing position in a successful complete snapshot is evidence for flatness,
but still requires fill/order reconciliation before a terminal state. The
historical display archive must not masquerade as today's current order book.

### 1.2 Market context

```text
MarketContext:
  snapshot_id, instrument identity, session_id
  decision_event_time, received_at, feature_version
  primary_bar: {bar_id, start, end, available_at, ohlcv, revision}
  higher_bar:  {bar_id, start, end, available_at, ohlcv, revision} | null
  input_bar_ids_or_dataset_ref, input_hash
  quality_by_source, expected_bars, missing_bars, freshness_by_source
  session_vwap, atr, direction_dynamics, participation
  known_structure: [{level_id, kind, price, formed_at, known_at, source_bars}]
  raw_regime, confirmed_regime, transition_candidate, transition_age
  higher_timeframe_context, session_remaining, nearest_relevant_levels
```

OHLCV validation checks positive finite prices, high/low consistency, nonnegative
volume, ordered unique timestamps, exchange-session membership, expected
intervals and availability. Conflicting duplicates are quarantined/versioned,
not arbitrarily averaged. Detect stale data even if an HTTP request succeeded.
Do not forward-fill missing volume/candles to make a failed setup look complete.

Use immutable cached frames keyed by instrument, interval, session, last bar and
revision; do not share mutable strategy DataFrames between consumers. Keep
original and corrected data versions distinct. Production does not reevaluate an
already-traded bar as if a later correction had been available originally.

### 1.3 Position management state

```text
PositionState:
  position_id, epoch, version, namespace, account_id, identity
  exposure_state, recovery_from, health, phase
  thesis_id, thesis_version, exit_policy_id, config_hash
  initial_fill_ids, entry_vwap, initial_quantity, residual_quantity
  initial_stop, initial_risk_per_share, initial_risk_currency
  first_fill_at, entry_terminal_at, completed_bars_held
  last_evaluated_bar_id, last_event_sequence
  confirmation_episodes, recovery_counters
  mfe_price/r, mae_price/r, close_mfe_r, extrema_quality, extrema_times
  realised_gross/net, estimated_liquidation_net_r
  confirmed_protection, requested_protection, protection_quality
  active_intent_id, exit_obligation, data_quality, ownership
```

Do not overload existing ambiguously named `initial_r`, `mfe` or `mae` database
columns with a new unit. Add unit-explicit versioned fields; old columns remain
legacy projections until a migration can prove their semantics.

## 2. Durable records with existing infrastructure

Reuse `journal.db`, SQLite WAL and `trade_events`. Add indexed records for the
information that must be uniquely addressed/queried, not a new storage service:

| Record | Contents / uniqueness |
|---|---|
| `position_theses` | Immutable JSON + schema/hash; unique `(position_id, thesis_version)` |
| `position_state` | Versioned checkpoint, last applied event sequence, identity and active intent |
| `decision_evaluations` | Typed trace; unique decision key `(position_id, policy_version, event_id, state_version)` |
| `order_intents` | Durable desired action, role, status, quantity, stable active-attempt tag/order ID, expected state version |
| `execution_fills` | Idempotent fill ledger/allocation; unique `(namespace, account_id, broker_fill_id)` |
| `policy_artifacts` | Content-addressed effective config/profile/feature/cost versions and source commit |
| `market_snapshots` | Content-addressed decision inputs or references to retained immutable candle chunks |
| `trade_events` (extend envelope) | Ordered state, intent-attempt, protection, broker-fact, gap, fault and reconciliation events |

Order attempts can be journal events plus an indexed current projection in
`order_intents`; a separate attempts table is unnecessary initially. Research
run manifests/counterfactual outputs can be versioned local artifacts referenced
by journal IDs, isolated from live tables.

### 2.1 Transactions and crash ordering

1. Validate input and reduce against state version V.
2. In an explicit SQLite transaction, write trace, next state V+1, event and any
   intent. Enforce at most one active reduction owner per position.
3. Dispatch the committed intent through the coordinator/gateway. Record attempt
   tag before submission. A broker tag is correlation metadata, **not** a broker
   exactly-once guarantee.
4. Persist acknowledgement or UNKNOWN outcome; allocate incoming fills by stable
   fill/order identity. Derive quantity and accounting from fills and reconcile
   to the broker.
5. Write the next checkpoint only after its event sequence is committed. On
   restart replay later events and reconcile outstanding attempts before acting.

SQLite's current `isolation_level=None` plus `with conn:` does not by itself make
the journal's multi-statement writes transactional; use explicit transactions.
Choose bounded connection busy handling, foreign keys, schema migrations and
indexes on `(trade_id, sequence)`, open intents, session and policy. Keep IO out
of position locks. Use a single lifecycle writer or compare-and-swap versions;
atomic file replacement prevents torn JSON, not stale concurrent overwrite.

Retain `ConfigManager._atomic_write_json` for migration/backups/checkpoints, with
schema version and event high-water mark. SQLite is the authoritative lifecycle
record once migrated; do not maintain two independently authoritative stores.
Checkpoint failures never silently discard an outstanding broker obligation.

### 2.2 Legacy migration

- Back up and import existing snapshots/journal IDs. Preserve original timestamps,
  stops, order IDs and reason text; mark unknown quantity/thesis fields explicitly.
- Normalize known casing/units; recover fills/quantity through successful broker
  reconciliation, not defaults. No synthetic fill price of zero is permitted.
- Existing open trades start supervised under a legacy/partial-thesis policy,
  keeping their confirmed protection and target semantics. Policy changes require
  a recorded amendment; no automatic stop loosening.
- Legacy CLOSED/UNRECONCILED records with zero-price placeholder P&L are excluded
  from performance/calibration until repaired from verified fills. Migration
  records the original value and repair provenance.
- Current `signal_score` is stored in `trades.confidence`. Provide a deliberate
  compatibility alias and schema migration; do not assume calibration reads a
  nonexistent field. Separate score semantics from probability semantics.
- Namespace all state, settings affecting research, caches, journals and order
  IDs by `LIVE / PAPER / REPLAY / DEV` and account identity. DEV data cannot
  calibrate live decisions or become live restart position state.

## 3. Decision trace

Record every scheduled exit evaluation, including HOLD and input-unavailable
outcomes. Repeated polling of the same bar may be a compact diagnostic referencing
the original trace; it cannot increment confirmation. Hard-risk evaluations that
create/change an obligation or encounter degraded state are recorded immediately;
unchanged high-frequency heartbeat checks may be summarized separately.

Required trace sections:

1. Identity: position/thesis/decision IDs, schema, sequence and state version.
2. Timing: bar start/end, availability, decision time, broker snapshot time,
   quote/feed ages, latest completed HTF bar and session.
3. Reproduction: code, entry/exit profile, config, feature, cost, data and optional
   calibration artifact hashes. Full referenced inputs must be retained.
4. Position/risk: entry/residual, initial R, uR, MFE/MAE, giveback, elapsed bars,
   account kill latch, pending orders and confirmed/requested protection.
5. Context: setup, raw/stable regime, structure/VWAP/HTF and uncertainty.
6. Evidence: supporting/contradicting/neutral/unknown by family, dependency groups,
   raw values, thresholds, source bars and predicate outcomes.
7. State: old/new axes, counters, candidate failure episode and recovery status.
8. Decision: candidates, suppression reasons, primary/contributing codes, action,
   stop proposal/quantity, urgency and intent linkage.
9. Result (separate subsequent events): acknowledgement, fills, errors, stop
   confirmation, residual reconciliation and accounting quality.

Illustrative structured trace fragment (values are synthetic):

```json
{
  "schema_version": 1,
  "decision_id": "position-7:policy-v1:bar-1010:state-12",
  "position_id": "position-7",
  "thesis_id": "thesis-7-v1",
  "namespace": "REPLAY",
  "decision_at": "2026-09-01T10:15:02+05:30",
  "primary_bar": {"start": "2026-09-01T10:10:00+05:30", "end": "2026-09-01T10:15:00+05:30"},
  "higher_bar_end": "2026-09-01T10:15:00+05:30",
  "inputs_ref": "sha256:example-fixture-inputs",
  "policy_ref": "sha256:example-fixture-policy",
  "position": {"entry": 100, "initial_stop": 98, "initial_quantity": 10, "residual_quantity": 10, "initial_risk_currency": 20, "u_r": 1.2, "mfe_r": 1.6, "mae_r": 0.2, "giveback_r": 0.4},
  "regime": {"raw": "TRENDING", "stable": "TRENDING"},
  "evidence": [
    {"family": "structure_price", "dependency_group": "defended-swing-3", "stance": "SUPPORT", "predicate": "close_above_defended_swing", "value": 102.4, "level": 101.8},
    {"family": "dynamics", "dependency_group": "oscillator-momentum", "stance": "CONTRADICT", "severity": "WATCH", "sources": ["rsi", "stochastic"]},
    {"family": "participation", "dependency_group": "volume", "stance": "SUPPORT", "predicate": "pullback_volume_contracting", "baseline_quality": "VALID"}
  ],
  "transition": {"health_from": "VALID", "health_to": "VALID", "phase_to": "PULLBACK", "failure_count": 0},
  "decision": {"action": "HOLD", "primary_reason_code": "HOLD_HEALTHY_PULLBACK", "suppressed_candidates": [{"rule": "multi_family_failure", "why": "no_material_price_failure"}]},
  "intent_id": null
}
```

The actual record also contains risk/protection quality and explicit referenced
bar availability. This compact example is explanatory, not the full schema.

## 4. One accounting vocabulary

| Measure | Definition / source |
|---|---|
| Realized gross | Signed P&L from allocated entry/exit fills for closed quantity |
| Unrealized gross | Signed residual quantity marked to a fresh declared price; stale marks labeled |
| Realized net | Realized gross minus allocated actual/estimated incurred fees; charge quality explicit |
| Liquidation net estimate | Realized net + open marked P&L minus expected remaining close fees/spread/slippage |
| Session net-risk P&L | Session gross realized + current unrealized - incurred fees, with a separately visible prospective liquidation-cost reserve |
| Research trade net R | Actual/simulated complete-trade net P&L divided by immutable initial monetary risk B0 |
| Slippage attribution | Signed difference between declared arrival/decision benchmark and fill; diagnostic, already reflected in actual-fill P&L |

The daily-loss gate uses one documented session net-risk definition everywhere;
whether the prospective liquidation reserve is part of the configured threshold
is pinned/versioned, not alternated by polling path. Opening fees on residual
positions are incurred session costs, not silently ignored. Broker day positions
are reconciliation inputs; do not confuse account net positions with today's
trade count or gross/net journal P&L.

For the correctness-repaired v1 control, use `realized_gross + unrealized_gross -
incurred_fee_estimate` for the daily-loss gate, matching the current intended net
definition. Show prospective liquidation reserve separately. Including it in the
gate later requires an explicit risk-policy version/migration and a separate
comparison; a polling implementation must not change the definition implicitly.

Reuse `TradingCostCalculator` for estimates. Add effective date, exchange/product,
rate version, rounding and **per-order** brokerage aggregation. Repeated fills
of one order must not each receive a fresh brokerage cap. Verify rates with broker
contract-note examples before reliance. Actual fill prices already incorporate
slippage; subtracting it again from net P&L would double count it.

### 4.1 Conservative portfolio-admission baseline

Use actual broker residuals and fresh declared marks, across all account exposure
covered by the risk mandate, including positions not discretionarily managed by
the app. For each instrument, `notional_i = abs(quantity_i) * mark_i * multiplier_i`.
Version 1 supports the validated equity/MIS multiplier; unsupported instruments
do not silently inherit it.

- **Gross exposure:** sum actual notionals plus remaining risk-increasing order
  reservations and proposed entry notional. Do not count verified protective or
  reducing orders as new gross exposure; UNKNOWN order intent stays conservative.
- **Net exposure:** test possible pending-fill sides, not just their cancelling
  signed sum. At minimum evaluate current signed notional plus all pending buys,
  and current signed notional minus all pending sells, including the proposal in
  the applicable scenario. Both must satisfy the net cap.
- **Loss-to-confirmed-stop from the current mark:** sum positive adverse distance
  `abs(q_i) * max(0, d_i * (mark_i - confirmed_stop_i))`, with fees/gap exposure
  reported separately. This is distinct from historical initial R. Missing or
  unverified protection is UNKNOWN risk, not zero risk; suspend new admission
  until its operational policy resolves it.
- **Symbol/sector:** aggregate actual/reserved gross notionals under stable
  identities. Pool unknown sectors conservatively while surfacing missing metadata.
- **Correlation unknown:** for admission, include unresolved pairs in the proposed
  correlated-exposure group at full gross weight until data are adequate; retain
  `estimate=UNKNOWN`, not a fabricated measured coefficient. For known pairs,
  evaluate sign-adjusted correlation `d_i * d_j * rho_ij` as well as absolute
  exposure so an opposite-side negative correlation is not assumed a hedge.

This is a bounded correctness baseline, not a new portfolio optimizer. The same
snapshot/admission function must run in production and portfolio backtests.

## 5. Exit-quality metrics

Let `d`, `E0`, `R0_price`, `Q0`, `B0` have the definitions in DECISION_MODEL.
For an interval actually observed while the position exists:

```text
MFE_R = max(0, max(d * (observed_price - E0))) / R0_price
MAE_R = max(0, max(-d * (observed_price - E0))) / R0_price
captured_gross_R = realized_gross / B0
captured_net_R   = realized_net / B0
MFE_capture_pct = 100 * captured_gross_R / MFE_R   # null if MFE_R <= 0
R_given_back = MFE_R - captured_gross_R
```

MAE is a nonnegative adverse magnitude. MFE/MAE also retain price/currency units
and source resolution. Do not average raw price extremes across differently
priced symbols. Do not silently clip negative capture or capture above 100%;
the latter can reveal fill/observation timing or coarse-data uncertainty.

For partial exits, retain the comparable **price-path** metrics above, labeled
as initial-quantity opportunity proxies. Also compute exposure-aware peak marked
trade P&L: realized P&L to time t plus marked residual P&L, normalized by B0.
Use that curve for actual giveback of capital-at-risk, rather than assuming
already-exited shares remained invested. No automatic scale-out optimization is
needed to support correct partial-fill accounting.

### 5.1 Required measurement table

| Metric | Exact use / caveat |
|---|---|
| MFE/MAE and timestamps | In-trade excursion in price, R and exposure-aware currency; bound ambiguous entry/exit bars |
| MFE capture % | Captured gross R / in-trade MFE_R; also show net captured R and denominator coverage |
| Average/median/tail R captured | Both gross and net; bootstrap uncertainty by session, not independent trade assumption |
| R given back | In-trade favorable peak to final capture; distinguish execution delay from policy giveback |
| Continuation after exit | Maximum additional favorable R and N-bar close-to-exit signed R after exit, over a fixed horizon |
| Profit forgone after exit | Positive net advantage of a declared risk-constrained hold-N replay over actual exit; not the unconstrained daily maximum |
| Reversal loss avoided | Actual exit net R minus hold-N net R when positive; quantifies useful protection |
| Premature exit diagnostic rate | Share of eligible normal exits whose safe hold-N improves net R by a predeclared materiality threshold, without prior retained-stop/forced-risk termination |
| Delayed exit diagnostic rate | Share remaining exposed beyond a fixed grace from an independent predeclared invalidation event; report additional adverse R and execution latency separately |
| Reason/action distributions | Stable enums by initiating decision and execution outcome; include HOLD/TIGHTEN/recovery and unknown attribution |
| Duration | Exchange bars and elapsed minutes; invalidation-to-intent and intent-to-fill separately |
| Cohorts | Entry regime, exit regime, regime transition, playbook, setup variant, long/short, symbol, liquidity, entry time and exit time-of-day |
| Safety/quality | Stop-coverage time, stale-data time, unresolved intents, duplicate/reversal incidents, missing traces, censored counterfactuals |

“Premature” and “delayed” are **research labels under declared assumptions**, not
market truth. Use multiple fixed horizons such as 3/6/12 primary candles,
preselected before inspecting results. Materiality can initially be 0.5R net as
a diagnostic, with sensitivity reporting rather than optimization.

A +0.5R exit followed by +3R **before the retained stop or session deadline** is
visible as continuation/possible premature exit. If the path first stopped out,
the later +3R is not available to the risk-constrained holder. A +2R exit before
a reversal can have high avoided-loss benefit even if some further favorable
tick occurred. Report both opportunity cost and avoided loss.

## 6. Replay modes

### 6.1 Exact decision replay

Load the pinned thesis, state checkpoint, immutable input context, risk snapshot
and policy artifact. Run the same reducer/evaluator; compare decision payload,
state hash and proposed intent. Exclude nondeterministic display IDs/timestamps
by deriving them from event identity. A mismatch is a reproducibility defect.

Never reconstruct “what the live engine knew” from newly downloaded historical
candles alone. Those are a separate corrected-data replay because availability,
revisions and broker state may differ.

### 6.2 Scenario / alternative-policy replay

Start from the same checkpoint and replace only the declared policy/artifact.
Feed the same as-of market events; use a fixed execution model/seed. Counterfactual
orders are simulated in a REPLAY namespace. No replay component can reach the
live execution adapter.

### 6.3 Hold another N candles

Suppress the selected **normal** exit; retain the stop already confirmed at the
decision, the original catastrophic/daily risk obligations and forced session
deadline. Stop at the earliest risk termination, declared N-bar exit or missing
data boundary. Do not widen a stop. If protective changes continue under a
reference management policy, label that as a different counterfactual from
“hold with current protection.”

Account-level daily-loss counterfactuals require recomputing the alternative
position's marked P&L alongside the declared other-position scenario. A fixed
recorded-other-trades experiment is conditional, not a portfolio-policy result.
If those inputs are unavailable, label output `PRICE_PATH_ONLY`, not an executable
counterfactual. Run full portfolio policy replays separately for interactions.

All counterfactuals record horizon, starting state, retained controls, fill/cost
model, seed, censoring reason, net outcome, extrema bounds and dataset versions.
Forward outcomes never feed back into the original decision. Comparing several
horizons is a diagnostic, not permission to pick each trade's best horizon.

## 7. Retention and operator presentation

Retain enough immutable inputs and artifacts to replay every deployed decision
for the configured research/audit horizon. Store chunked candle data once and
reference by hash; avoid repeating multi-day arrays in every trace. The journal
needs paginated queries and bounded in-memory caches; activity-log localStorage
is not the decision record.

Operator views should show:

- Thesis/setup, entry reason, original boundary and expected behavior.
- Latest HOLD/weakening/invalidation reason and its exact completed-bar time.
- Supporting, contradicting and unavailable evidence with family grouping.
- uR/MFE/MAE/giveback and remaining session time.
- Requested versus broker-confirmed protection, residual quantity and pending
  intent stage; unknown values are not green status indicators.
- Entry admission state separately from risk supervision, broker connectivity,
  data freshness, daily-loss latch and reconciliation status.
- Replay of the state/evidence at each evaluation, plus separately labeled
  forward what-if results and uncertainty/censoring.

Post-trade LLM interpretation is an optional asynchronous job over a trace ID.
Persist model/prompt version and output; request it explicitly and cache it.
It may cite evidence and propose experiments, but cannot alter the deterministic
record or the active policy.
