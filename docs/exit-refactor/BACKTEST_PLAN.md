# Research and backtest validation plan

## 1. Question and comparison design

The question is whether exits become **less prematurely reactive while preserving
capital protection and timely genuine invalidation**, after realistic execution
costs. Historical P&L alone cannot answer it.

Maintain three clearly labeled baselines:

1. **Forensic current behavior:** captured/reconstructed legacy decisions with
   their actual limitations, used to explain incidents. Do not use known corrupt
   zero-price accounting as a profitability baseline.
2. **Correctness-repaired control:** existing entry/normal-exit policy semantics
   with canonical broker inputs, coherent costs and common simulation assumptions.
   This attributes exit-policy changes separately from accounting/execution fixes.
3. **Candidate thesis policy:** the shared new deterministic engine, pinned
   profile/config version, under the same initial entries and execution assumptions.

Initially hold entry selection, initial stops/targets, sizing and datasets fixed.
Compare exits on isolated matched entry opportunities, then run a full portfolio
policy experiment. Exit changes alter slot availability, cooldowns, daily risk
and subsequent entries; a fixed-entry paired study cannot claim those portfolio
effects. Record rejected overlapping opportunities rather than allowing an
impossible second position to make pairing convenient.

A preserved legacy comparator may be a pinned fixture/event replay or a clearly
labeled frozen research control. The **candidate exit implementation itself is
one shared engine** in live, paper, scenario, backtest and replay; no backtest-only
candidate exit rules are permitted.

## 2. Current live/research divergence inventory

| Dimension | Live today | Research/dev today | Required resolution |
|---|---|---|---|
| Entry logic | Regime -> aggregated families -> playbook -> score/calibrator | Backtest calls one raw strategy | Extract pure production entry evaluation; label raw-strategy lab separately |
| Selection | Dynamic 12-stock screener, current universe, callback order | Requested symbol(s), no production screener | Fixed-entry attribution first; point-in-time selection in full portfolio study |
| Sizing/risk | Margin/leverage/stop sizing and portfolio gates, with audited defects | 10% cash sizing; no equivalent limits | Same deterministic admission/risk inputs through adapters |
| Exits | Stop/target polling, single rejection, opposing/no-support/time rules | OHLC stop/target and end-of-test only | Shared thesis engine, risk rules and lifecycle reducer |
| Time | Live wall clock, mixed timezones, approximate polling | Bar timestamps; no daily intraday square-off | Shared exchange/session clock and availability events |
| Candles | Completion filter normally on; resistance separately uses penultimate raw row | Raw historical slices through current bar | Shared completion/as-of context; incomplete last data excluded |
| Orders | LIMIT entry, stop-limit protection, buffered LIMIT exit plus fallback | Next-open MARKET entry; instantaneous synthetic stop/target execution | Explicit order-type/queue/latency model and contract parity |
| Intrabar ordering | Real broker sequence, app can race protective stop | Stop first when both touched | Retain conservative fallback, use finer data where available, disclose bounds |
| Slippage | Actual execution, partially recorded | Hardcoded 5bp; stop path applies it twice | Versioned single-application fill model, measured/stressed spreads/impact |
| Costs | Fee calculator plus incompatible risk turnover estimate | Shared fee calculator with simplifying full fills | One versioned charge contract, per-order fill aggregation |
| Partial fills | Entry polling and boolean exit state, incomplete residual support | Unsupported | Shared fill events and residual state; stress liquidity/latency |
| MFE/MAE | Schema fields, generally not populated | Full candle extrema including potentially post-exit prices | Resolution-aware actual exposure window / ambiguity bounds |
| Restart | JSON + partial broker reconciliation | No comparable restart events | Scripted crash/recovery replay through shared coordinator |
| Development mode | Separate random quote/tick/candle processes and immediate limit fills | Not a research paper simulator | Keep UI mock; use isolated real-data paper adapter for validation |

## 3. Event ordering and causality

The event driver advances an injected clock, not `datetime.now()`. Each primary
bar interval has explicit start/end/availability. Use this ordering:

1. Process previously submitted orders/fills and broker facts at their modeled
   times, including gaps, partial fills and hard stops.
2. Update marked portfolio risk and apply hard-risk/session events at their
   timestamps. Forced deadlines are not delayed until a convenient candle.
3. At a bar's availability event, expose only bars/context known by that event.
   Confirm new 15m context only after all constituent 5m bars are complete.
4. Evaluate open positions with the common exit engine; persist a trace/intent.
5. Evaluate new entries through the common production entry function if enabled.
   Use deterministic time/symbol priority and atomic risk reservations.
6. Execute new ordinary decisions at the next **available executable event** with
   latency, not retrospectively at the close that generated them.

With only 5m OHLC data, use next-bar open for ordinary decision fills and clearly
state the approximation. A stop/target active before a bar may be processed within
it. A new trailing stop computed at that bar's close cannot protect the same
bar's earlier low/high. A right-confirmed pivot cannot be used before known-at.

Partial entry/exit bars cannot supply precise pre/post-fill extremes without
finer data. Use bounds or mark those excursion samples unavailable; do not grant
the strategy a perfect fill path from OHLC alone.

## 4. Execution assumptions that must be explicit

Each run manifest pins all of the following:

- Data resolution, timezone/session calendar, timestamp convention and availability
  delay; quote/price source and treatment of missing/corrected records.
- Entry/exit/protective order type, tick/lot size, trigger/limit semantics,
  supported broker modifications and cancellation/acknowledgement latency.
- Spread, slippage/impact model, cost schedule/effective date, participation cap,
  partial-fill model, gap behavior and random seed if used.
- Stop/target ambiguity policy, opening gap handling, no-data/session/end-of-file
  policies, and behavior when circuits/price bands prevent execution.
- Account capital/margin, sizing, all risk limits, open/pending exposure and
  effective policy/config versions.

### 4.1 OHLC ambiguity and gaps

If both active stop and fixed target are touched within one coarse bar and order
cannot be resolved, report it as ambiguous. Conservative stop-first is the main
baseline; also report favorable-order bounds and the ambiguous-trade proportion.
Where licensed/fresh 1m or tick data exist, replay the finer event sequence. A 1m
bar can still be ambiguous; finer does not mean omniscient.

For a gap beyond a stop trigger, fill at the first model-supported executable
price, including spread/impact. A stop-limit may trigger without filling; retain
its working order and recovery obligation. A favorable target gap must respect
the actual order type/limit, availability and latency, not assume a perfect print.

Preserve next-bar entry timing already present in `BacktestEngine`. Correct
slippage double-application and stop/target reason recording in `SimulatedBroker`.
Do not replace these with same-close fills to make results look better.

### 4.2 Costs and liquidity

Use the common cost service, versioned by effective date/exchange/product. Verify
rounding and brokerage caps against representative broker contract-note fixtures.
Do not assert today's default rates apply to every historical year/product.
Realized net uses fill prices minus fees; slippage already in fill prices is not
subtracted again.

Stress a predeclared range of spread/impact and latency. Version 1 can use a simple
documented volume-participation cap rather than an invented full order-book model.
Calibrate operational distributions from future paper/live observations separated
from held-out policy evaluation. Report unfillable orders/censored exits, not
automatic ideal fills. Pending orders consume capacity/margin until resolved.

## 5. Data and cohort plan

Build immutable datasets spanning multiple symbols, sectors, liquidity groups,
long/short sides, quiet ranges, directional trends, failed breakouts, volatility
shocks, gaps and different times of day. Use several nonoverlapping time periods,
not one favorable month or only one liquid symbol.

Store dataset version/hash, data vendor, timezone, session calendar, instrument
mapping validity, corporate-action treatment, universe membership and all quality
exclusions. For a full-universe study, historical membership must be point-in-time;
today's NIFTY 100 introduces survivorship/selection bias. Corporate-action gaps
cannot be mistaken for intraday alpha or stopped-out price shocks. Adjusted
historical price/volume and executable unadjusted fills need a consistent mapping.

Prior-session levels use only the prior completed session. Time-of-day relative
volume uses previous sessions' comparable completed buckets, not full-day volume
that was unknowable at 10:00. Normalization parameters may use the contemporaneous
cross-section or training data as specified, never the later test distribution.

Do not fabricate historical depth/latency that the data do not contain. A study
missing selection or microstructure inputs is explicitly an exit-only/price-bar
study, with liquidity stress and limited claims.

## 6. Walk-forward methodology

The current `WalkForwardValidator` executes rolling slices with a default strategy;
it does not train/select anything. Warmup is traded, and filtering its trades out
afterward does not remove its impact on cash and open positions. It is not a
rigorous parameter-selection validation pipeline.

The replacement driver supports two honest designs:

### Fixed policy

Predeclare the small v1 rule/profile set before evaluating held-out periods.
There is no fictitious fitting step. Use time-separated rolling OOS windows for
stability, with earlier bars for indicator warmup only. Entries/risk accounting
begin at the scoring boundary with a declared initial state.

### Limited parameter selection

For each outer chronological fold:

1. Training/history: construct causal features and any calibration artifact using
   only outcomes fully known by the training cutoff.
2. Inner validation: choose among a small preregistered parameter set using a
   multi-objective criterion; do not search for the best historical P&L.
3. Purge labels whose trade/counterfactual horizon crosses a split. Embargo at
   least the maximum overlapping label/holding horizon when applicable; record
   the actual rule in the manifest.
4. Freeze policy/config/cost/calibration artifacts before the next test window.
5. Test once on the next unseen interval. Test windows are half-open and
   nonoverlapping. Require valid step sizes; no repeated boundary trades.
6. Aggregate disjoint OOS session results. Never refit a failed fold and keep
   calling its revised result out-of-sample.

Warmup calculates features only: no entry orders, P&L, fees, position carry or
daily counters. If a deployment study deliberately carries positions across a
split, record that as a separate continuous-state design with unambiguous scoring
ownership and purged overlapping labels. Do not mix both designs.

Keep a final untouched chronological holdout, accessed after policy/ranges are
frozen. Cross-symbol holdouts supplement temporal holdouts. Record the total
number of candidates/experiments tried, including discarded failures.

## 7. Calibration policy

The exit engine requires no probability model. Current score buckets measure
gross +0.9R frequency under historical entries/exits; they are neither a
time-separated calibrated model nor an exit-failure probability. Current
`confidence` storage is an alias for `signal_score`, so preserve/read that
compatibility deliberately while fixing semantics.

For exit-only attribution, freeze recorded entry decisions or a declared
deterministic entry policy. Never query the current live journal from a historical
test. Any later calibration project needs outcome/horizon definitions, clean
fill data, sufficient samples/uncertainty, temporal splits and a versioned artifact
trained strictly before each decision. Exit-policy changes themselves change the
entry-success label; old frequencies cannot be reused without validation.

## 8. Evaluation metrics and diagnostics

Use the definitions in [DATA_AND_REPLAY.md](DATA_AND_REPLAY.md). Primary outcomes:

- Paired change in net R captured, in-trade MFE capture and R given back.
- Risk-constrained continuation/profit-forgone and reversal-loss-avoided at fixed
  post-exit horizons.
- Premature-exit and delayed-invalidation diagnostics, with uncertainty/censoring.
- Tail adverse excursion, worst-session loss, MTM drawdown, recovery latency,
  confirmed-stop coverage and unresolved execution incidence.

Report net expectancy, costs and turnover as supporting outcomes. Include zero-
trade sessions in equity/return risk statistics; use full marked equity rather
than only the curve of completed trades. Do not call a trade-close-only drawdown
maximum portfolio drawdown, or a trade-day-only Sharpe a full daily Sharpe.

Break down by entry/exit regime, regime transition, playbook/setup, symbol/sector,
liquidity, side, time of entry/exit and hold duration. Keep small groups visibly
small rather than making per-bucket tuning rules. Confidence intervals use block
resampling of sessions/weeks to respect serial/cross-symbol dependence.

## 9. Anti-overfitting and stress experiments

Pre-register experiments and keep the candidate count small:

1. **Ablation:** structural premise only; add confirmation; add HTF context; add
   structural profit protection; then time management. Verify each adds value
   beyond merely increasing hold time.
2. **Parameter perturbation:** neighbors of confirmation count, buffer multiplier
   and review horizon. Look for broad stable regions, not an isolated optimum.
3. **Cost/slippage stress:** conservative/base/adverse fee-spread-impact scenarios;
   plausible latency and participation limits, delayed stop fills and gaps.
4. **Regime/time stress:** quiet sessions, trend days, high-volatility reversals,
   opening/late-session conditions and held-out periods/symbols.
5. **Data/operation stress:** missing candles, stale quotes, reordered updates,
   duplicate fills, process crashes, failed stop modifications and broker outages.
6. **Boundary stress:** one tick around each stop/acceptance threshold; one missing
   HTF bar; one execution event before/after a forced deadline.
7. **Null comparison:** unchanged protective-stop/fixed-objective control and a
   simple predeclared time/structure control. Complexity must earn its place.

Do not optimize indicators, entries, targets, risk sizing and exits together in
one large search. Additional indicators and per-symbol thresholds require a new
research rationale and independent validation.

## 10. Promotion and rollback

Before looking at holdout results, declare materiality/noninferiority margins,
minimum sample/coverage requirements, tail-risk budgets and acceptable ambiguity
rates. These are governance/research inputs, not numbers chosen after seeing a
good chart. Small samples yield “inconclusive,” not a calibrated success claim.

Promotion requires:

- P0 contract/execution/safety tests and identical-input decision parity pass.
- Complete decision/intent/fill trace linkage; unknown/censored results visible.
- OOS reduction in premature-exit diagnostics or better capture with no material
  increase in delayed structural failure, adverse tail risk or cost-stressed
  losses beyond predeclared tolerances.
- Improvements not concentrated in one symbol/regime/window or a narrow threshold.
- Shadow and isolated real-data paper runs demonstrate timely hard supervision,
  stop handoff, residual reconciliation and operator visibility.
- Separate P0 credential/storage/operator defects in the audit are resolved
  before live promotion.

Promote a limited policy/version cohort with observable rollback. Rollback stops
new entries and returns future decisions to the last approved policy only at
recorded safe boundaries; it never loosens a confirmed stop or cancels a pending
hard exit. Maintain the same risk supervisor throughout.

Unresolved broker support, fee accuracy, signal provenance and insufficient
historical availability remain explicit validation questions. No backtest result
in this phase establishes that the candidate is profitable or safer in practice.
