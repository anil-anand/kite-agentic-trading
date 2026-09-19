# Deterministic exit decision model

## 1. Meaning of a decision

The engine answers: **does the reason for owning this particular position still
hold, and what risk-reducing action is justified now?** It does not ask the entry
scanner to keep producing a buy/sell event.

A decision contains an action, primary reason code, supporting and contradicting
observations, failed/passed rule predicates, next lifecycle state, and any proposed
stop/exit intent. `HOLD` is a positive, explainable decision. `HOLD_DATA_DEGRADED`
means normal evidence is unavailable; it is not a declaration that the thesis is
healthy. Hard-risk rules remain active in either case.

Version 1 uses explicit predicates and ordinal evidence severity. It needs no
weighted “exit confidence” total. If a later UI wants an evidence summary, it must
be labeled **evidence strength**, show its contributors and never display a
heuristic score as a probability.

## 2. Entry thesis contract

Create a draft before entry submission from the **exact** selected signal,
playbook and as-of market snapshot. Bind it to actual fills after execution; do
not refetch a later scanner snapshot and call that the entry rationale.

| Field | Meaning |
|---|---|
| Identity/provenance | Thesis ID/version, position epoch, account/mode namespace, symbol/instrument/exchange/product, `SYSTEM / OPERATOR / LEGACY_PARTIAL / UNKNOWN` |
| Setup | Stable playbook ID and version; setup variant; selected strategy evidence and reason; distinguish actual pullback from a trend-only trigger |
| Direction | Signed long/short exposure; never inferred from a score |
| Entry context | Signal bar/open/close times, decision time, 5m/15m context snapshots and hashes; initial regime and its raw features |
| Supporting evidence | Family observations, origin indicators, dependencies and missing inputs; no correlated-vote multiplication |
| Structure | Frozen setup boundary/range, last causally confirmed swing, trigger-bar extreme, nearby relevant level IDs and how/when each became known |
| Value context | Session VWAP, its slope/distance in ATR units and the setup's expected relationship to it |
| Dynamics | Directional progress, momentum state, trend strength; the source calculations, not just names |
| Participation | Volume relative to an available causal baseline, baseline type and sample sufficiency |
| Volatility | ATR, normalized range/expansion, baseline quality and stop buffer provenance |
| Higher timeframe | Last completed 15m bar ID, structure/trend/VWAP relationship, agreement or contradiction; unknown remains explicit |
| Invalidation specification | Named price boundary and evaluation type; hard tick stop versus completed-close acceptance; required confirmation and predicates |
| Expected behavior | Follow-through/pullback/convergence expectation, review horizon in completed bars, permitted consolidation and objective |
| Risk/fills | Planned entry, initial stop, actual entry fills/VWAP, original filled quantity, immutable per-share R and initial monetary risk |
| Management profile | Trend continuation, breakout follow-through, range convergence or unknown-thesis; objective mode, stop policy, time/session policy versions |
| Reproducibility | Code/build/config/feature/cost/entry/exit policy versions; effective configuration values and content hashes |

The thesis is immutable. New context and confirmed swing levels are added to
management state; they never rewrite what justified the entry. A deliberate
operator/policy migration creates a versioned amendment with the previous thesis
retained. A trend cannot silently become a “mean reversion trade” to excuse a
loss.

### 2.1 Binding actual risk

Let `d = +1` for long and `-1` for short, `E0` be the actual initial entry VWAP,
`S0` the initial protective-stop trigger, and `Q0` initial filled quantity:

```text
R0_price = abs(E0 - S0)             # price units/share, strictly positive
B0       = R0_price * Q0           # initial price-risk budget in currency
uR       = d * (mark - E0) / R0_price
```

Require `d * (E0 - S0) > 0`; validate tick/lot sizes and finite positive prices.
If an adverse fill makes initial risk exceed the accepted risk budget, cancel
remaining entry quantity and apply the deterministic entry-risk recovery policy.
Do not widen a stop to accommodate it. All partial fills are protected as they
arrive; final `E0/Q0/B0` are fixed when entry filling terminates. During partial
entry, use explicitly provisional fill-based risk. Adding/pyramiding is outside
version 1; an external addition creates a reconciliation event, not a new R
denominator hidden inside the same trade.

Record cost-adjusted liquidation estimates separately. Entry-price breakeven is
not net breakeven after fees, spread and slippage.

### 2.2 Causal anchors

- A breakout boundary is drawn from completed **pre-trigger** bars, not from a
  range containing the breakout itself. Keep its bounds/time window fixed.
- A swing detector may use two right-hand confirmation bars, but its
  `known_at` is the close of the second confirming bar. It may never be used at
  the earlier pivot timestamp. Use one small versioned detector, not a catalogue
  of retrospectively fitted patterns.
- For trend pullbacks, use the last valid pre-entry swing/retest level. Record
  whether the entry actually provided a pullback. Current playbook naming alone
  is insufficient evidence.
- For mean reversion, record the defended range edge and convergence objective.
  “Price below VWAP” is often expected early in a long reversion and is not
  automatically a contradiction.
- If no defensible structure exists, mark it missing. Initial integration uses
  the explicit bounded/unknown-thesis profile rather than inventing a structural
  reason or silently changing entry selection. Enabling a structure-required
  entry gate later is a separate measured entry change.

## 3. Market context and evidence families

Compute continuous observations from completed candles, not just trigger events.
Each observation has `family`, `dependency_group`, `direction_relative_to_thesis`,
`severity`, `value`, `threshold`, `source_bar_ids`, `known_at`, `freshness` and
`quality`. Preserve contradictory as well as supporting evidence.

| Evidence family | Useful observations | Interpretation / dependency rule |
|---|---|---|
| Structure and price action | Defended swing/range, acceptance beyond boundary, follow-through, close location, rejection/reclaim | Primary price-led evidence. A rejection candle and its close below the same level are one price event |
| Value/context | Session VWAP position/slope, distance to prior-session and setup levels, reclaim/acceptance | Setup-dependent. A VWAP-defined invalidation and “VWAP failure” are not two confirmations |
| Directional dynamics | Progress/overlap, one momentum representation, directional EMA/ADX trend state | RSI, Stochastic, StochRSI, MACD and EMA derivatives cannot become separate votes. ADX describes strength, not direction |
| Participation | Relative volume on adverse/favorable displacement, contraction during pullback | Volume decline alone is neutral/weakening; high volume needs price response. MFI is not independent of oscillator evidence |
| Volatility | ATR-relative move and pullback, expansion/contraction, gap/noise allowance | Mostly a tolerance/severity modifier; expansion by itself is not bearish or thesis invalidation |
| Higher-timeframe context | Completed 15m swing/trend alignment, range boundaries and adverse acceptance | Contextual corroboration. Reusing the same price move/EMA regime at 5m and 15m is not asserted to be independent statistical evidence |
| Regime context | Raw/stable regime, transition age, directional features, uncertainty | Selects expected behavior and tolerances. Do not count a regime label and its ADX/ATR inputs as independent reasons |
| Trade/time/profit context | Age, valid decision-bar count, uR, MFE/MAE, giveback, session remaining | Chooses management policy; being old or profitable is not directional evidence |

“Independent” here means **nonduplicated economic evidence families**, not a
claim of statistical independence. Collapse shared dependency groups before
evaluating a multi-family rule. Profile mapping is fixed and testable. Version 1
does not count regime, volatility or time/profit as extra adverse votes.

Raw severity is ordinal:

- `NONE`: no material contradiction.
- `WATCH`: a weak or isolated observation.
- `MATERIAL`: a meaningful adverse condition on this completed bar.
- `DECISIVE`: a confirmed, explicitly specified thesis failure.

Missing/invalid values produce `UNKNOWN`, not zero, false support, or adverse
votes. A family's evidence cannot grow merely by enabling more strategies.

### 3.1 Reference predicate contract for the first implementation

The following is a small **candidate v1 specification for fixtures and research**,
not evidence of optimal parameters. Pin it in the profile artifact before OOS
evaluation; do not leave “material weakness” as an implementer's free-form judgment.
Let `C_t` be completed close, `L` a known relevant level, and
`b = max(2 * tick_size, 0.1 * entry_ATR)` for an entry-frozen boundary. A later
management swing pins its own buffer from ATR available at its confirmation.

| Predicate | Candidate deterministic definition |
|---|---|
| Boundary failure / recovery | Failure: `d * (C_t - L) < -b`; recovery: `d * (C_t - L) > b`; equality/inside buffer is neutral for episode advancement |
| Failed favorable structure | The same buffered failure applied to the most recent causally confirmed post-entry favorable swing/base; reference its level ID so consecutive bars describe the same episode |
| VWAP contradiction | For a profile explicitly requiring favorable VWAP acceptance: `d * (C_t - VWAP_t) < -b`; for a range-convergence trade, the starting wrong-side VWAP location is not this predicate |
| Material adverse dynamics | Two successive close-to-close changes are adverse and the directional EMA20 slope is adverse; all associated oscillators/EMA derivatives remain one dependency group |
| Volume-backed adverse move | Adverse close displacement exceeds the declared noise buffer and volume is at least 1.5 times the preceding 20 valid completed bars' mean, excluding the evaluated bar; missing/insufficient baseline is UNKNOWN |
| Contracting pullback participation | Adverse retest stays inside the defended premise and current volume is below that valid prior-bar baseline; supports pullback interpretation, never proves continuation alone |
| Stable regime candidate | Same raw new label on two distinct primary bars establishes a context transition; a label alone still cannot invalidate a trade |
| Favorable progress | New directional completed-close extreme beyond the noise band plus a known favorable swing/base; tick-only spikes are recorded but do not establish this condition |
| Consolidation | Last three completed bars have a common price intersection (`max(low) <= min(high)`), with no buffered premise failure and no new favorable progress; record the exact interval/level IDs |

Price-derived family conditions are still correlated. Apply dependency collapsing
and the price-led requirement in section 5; this table is not a list of independent
votes. The simple volume baseline is labeled `ROLLING_20_TRADING_BARS`, not
time-of-day relative volume; it can span a session boundary and that fact is
recorded. A better seasonal baseline is a separately versioned follow-up.
Use only the fields required by enabled rules, rather than adding every possible
indicator. Profile tests must spell out any setup-specific override.

## 4. Decision precedence

Evaluate in the following order, recording every candidate and the winning rule:

1. **Known flat/fills and pending execution:** consume broker facts first. Finish
   protection cleanup and reconciliation, rather than submit another reduction.
2. **Hard risk / forced deadline / explicit operator close:** produce or escalate
   a latched exit obligation without waiting for a bar or minimum hold time.
3. **Unknown execution/protection state:** request reconciliation; an existing
   exit obligation remains active. Normal stop/exit commands wait for safe
   residual-quantity determination, not for improved market evidence.
4. **Decision eligibility:** require a new eligible completed 5m bar. Record
   stale/duplicate/incomplete/missing-context outcomes. Unknown required evidence
   cannot establish a new discretionary invalidation.
5. **Confirmed thesis invalidation:** exit regardless of whether the trade is
   winning or losing. MFE and higher-timeframe support cannot override a
   previously specified hard invalidation boundary.
6. **Explicit objective / confirmed profit reversal:** apply the frozen management
   profile; otherwise propose a valid ratcheting structural stop.
7. **Confirmed time-based stagnation:** exit only when expected behavior has
   failed and relevant context agrees.
8. **Weakening, pullback, continuation or consolidation:** HOLD with state and
   reason; propose protection only if justified independently.

Requested and confirmed protection are separate. A proposed stop is not displayed
as effective until the coordinator obtains broker confirmation.

## 5. Weakening versus invalidation

### 5.1 Weakening

Examples: one adverse candle, momentum flattening, an isolated VWAP approach,
contracting volume, lack of a fresh crossover, a modest retracement, a provisional
regime change. Record these and monitor. Price structure still defended plus
contracting pullback volume is often **healthy pullback**, even with several
negative oscillator readings.

Version 1 enters `WEAKENING` after material adverse evidence persists for two
eligible consecutive decision bars without an invalidation rule firing. One
material observation is a warning in the trace. Supporting/contradicting evidence
must be interpreted through the setup; generic evidence cannot outvote a defined
boundary.

### 5.2 Invalidation routes

**Route A — explicit setup boundary failure:** two consecutive eligible 5m closes
beyond the entry-defined structural boundary and buffer. This can be decisive
without an oscillator/volume vote because that boundary was the thesis premise.
The first close is a failure candidate, not a completed exit decision. The
separate hard stop acts immediately if reached at any time.

**Route B — confirmed multi-family deterioration:** a material price-led failure
(for example, lost progress structure or failed reclaim) plus at least one
material corroborator from a different, nonduplicated value/dynamics/participation
family, on two consecutive eligible 5m bars. Both bars must describe the same
failure episode. A collection of indicators describing the same price crossing
does not satisfy this route. Intact relevant 15m structure is recorded as
contradiction and can keep a merely local setback in WEAKENING; it cannot rescue
Route A.

**Route C — setup-destroying regime transition:** a confirmed directionally adverse
transition **and** its setup-specific price consequence. For example, a range
fade fails when price accepts outside the defended range in a developing trend,
not whenever ADX rises. This is implemented using Route A/B predicates, not a
second regime-label exit function.

**Not invalidation:** `supporting_signals == 0`, a single opposing signal, a lone
ADX/RSI threshold crossing, a transient tick below VWAP, or missing candles.

### 5.3 Confirmation and hysteresis

- State includes last processed 5m bar ID, per-rule failure episode, candidate
  count, last qualifying bar and recovery count. Polling a bar 20 times counts
  once. The entry signal candle cannot count as post-entry adverse confirmation.
  The first fully post-entry decision bar starts at or after `entry_terminal_at`;
  while entry fills are still pending, protection and hard risk manage the
  provisional exposure. A reused 15m bar is context, not a new HTF confirmation;
  HTF-specific counters advance only when its distinct bar ID advances.
- Start with two consecutive completed bars for ordinary failure confirmation
  and two supportive completed bars for recovery from WEAKENING. These are
  **research defaults**, not optimized or mandatory universal market constants.
- A healthy reclaim resets the failure episode. A missing expected bar breaks
  consecutiveness; unknown bars do not confirm or heal. The next valid bar starts
  a new consecutive sequence. A market holiday/closed session is not a missing bar.
- Structure boundaries use a predeclared noise buffer; a recovery must reclaim
  the opposite side of that buffer. Within the band, retain health state and do
  not invent a new failure.
- `INVALIDATED` and `EXIT_PENDING` are latched for the position epoch. A rejected
  order leads to execution recovery, not `VALID` because the next candle bounced.
- After closure, retain existing entry cooldown/maximum-trade controls. A new
  trade requires a new thesis and epoch; no automatic flip or revenge re-entry.
- No minimum hold period suppresses hard risk or a confirmed structural failure.
  “Early grace” only suppresses weak, nondecisive management reactions.

## 6. Small management-profile set

| Profile | Expected behavior / healthy setback | Invalidation | Objective and protection |
|---|---|---|---|
| Trend continuation | Directional progress, higher lows/lower highs; retest/consolidation can contract volume while 15m structure holds | Loss/acceptance beyond the defended swing; failed reclaim plus confirmed independent deterioration | Let intact structure develop; trail behind confirmed swing with volatility room; a nearby level is a review location, not an automatic exit |
| Breakout follow-through | Acceptance beyond a frozen pre-trigger range; retest of boundary may be normal | Two closes accepted back inside the old range beyond buffer, or hard stop; failed retest episode | Initially protect original boundary; progress to structural trailing after follow-through, not after an arbitrary small profit |
| Range convergence | Move from defended range edge toward predeclared VWAP/midrange objective; diminishing momentum near objective is expected | Acceptance outside defended range; confirmed adverse trend/structure expansion | Take the declared convergence objective; do not silently turn a completed range trade into a trend runner |
| Unknown/legacy bounded | Original reason not recoverable | Hard risk and explicit existing objective/session obligations only; no fabricated thesis invalidation | Retain confirmed stop/legacy fixed target, surface missing thesis; validated amendments are explicit |

These profiles do not introduce new entry indicators. They organize already useful
structure, VWAP, momentum, volume and volatility around an explicit premise.

## 7. Profit management

### 7.1 State to track

Track signed unrealized R, observed MFE/MAE in price and R units, mark source,
last favorable extreme time, retracement from MFE, favorable progress structure,
stop-confirmed risk floor, time at/near objective and time since progress. Record
both intrabar-observed and completed-close extrema where available. Use
completed-close/structure evidence for ordinary policy thresholds so a tick spike
does not abruptly activate an aggressive trail.

```text
giveback_R = max(0, MFE_R - uR)
giveback_fraction = giveback_R / MFE_R     # null if MFE_R <= 0
```

Giveback is context, not an exit by itself. “Profit > X, therefore trail tightly”
is not the policy. Winning does not make the original risk premise irrelevant.

### 7.2 Structural protection

1. Require genuine favorable progress and a newly confirmed favorable swing or
   accepted post-breakout base. The structure must become known after entry.
2. For a long, propose `swing_low - buffer`; for a short, `swing_high + buffer`.
   Buffer is tick/volatility aware, pinned when that structure is accepted.
3. Compare against the **confirmed** stop. Long stops only increase, short stops
   only decrease. Tick rounding must preserve side and minimum broker distance.
4. Require room between current executable price and proposed trigger. If price
   already crossed it, do not submit an invalid stop; reevaluate whether the
   price failure qualifies for exit or retain current protection.
5. An unchanged rounded stop is a no-op. A modification remains pending until
   confirmed, then protection status can become `PROFIT_PROTECTED` if its estimated
   net liquidation floor is positive. Gaps mean this is not a guaranteed profit.

Strong directional structure favors a wider structure-based trail. Contracting
consolidation alone does not tighten the stop into the noise. Exhaustion requires
price evidence such as repeated failed progress/lost favorable structure plus a
nonduplicated corroborator, using ordinary confirmation. A climactic volume bar
without adverse acceptance is insufficient.

### 7.3 Targets and partial reductions

Version 1 supports an explicit `FIXED_OBJECTIVE` or `STRUCTURE_RUNNER` target mode,
frozen at entry. The initial shadow baseline retains existing fixed targets;
changing trend targets into review zones is tested as a distinct experiment.
A fixed target is a precommitted price barrier and may be executed on a fast
price event, as today; this is execution of the stored objective, not a new
tick-level thesis evaluation. Hard stops take precedence in an ambiguous bar.
A range objective can remain a full exit at its predeclared level. A runner
review zone triggers an evaluation, not a sell simply because the level was
touched. No automatic mid-trade target extension is allowed.

Automatic discretionary scale-outs and pyramiding are deferred to reduce rule
and accounting complexity. **Partial broker fills are mandatory support now**;
they are not the same feature as choosing to take partial profits.

## 8. Time and session management

Time is measured in completed exchange-session bars since first fill; partial
entry bars do not count as full decision bars. Wall-clock age is retained for
latency/recovery. Data outages do not masquerade as many completed no-progress
bars.

At the profile's first review horizon, ask whether price progressed as expected.
A candidate v1 stagnation predicate is: review horizon reached, no new favorable
completed-close extreme over the review interval, no confirmed favorable
structure, and material adverse price/context evidence on two eligible bars.
With those predicates, issue `TIME_NO_PROGRESS_CONFIRMED`. If price is forming a
healthy base and higher-timeframe structure remains intact, HOLD with
`HOLD_CONSOLIDATION`; time alone never assigns entry-price breakeven.

Session management has two separately versioned times:

- **Management/review time:** stop new risk and review whether the remaining
  session can plausibly support the expected behavior. May choose a justified
  normal exit; must not reinterpret every late candle as deterioration.
- **Forced intraday flat deadline:** authoritative clock event. Cancel remaining
  entries and continue close/reconciliation until flat/order-clean. This applies
  even with absent candles, stale scan threads, confirmation mode or later clock
  time beyond the exchange close. Report inability to execute rather than claim
  completion. Broker-specific cutoffs/holidays need validation.

## 9. Timeframe and data-quality rules

- Primary policy: completed 5m candles. Higher context: completed 15m candles,
  aligned from 09:15 Asia/Kolkata, not arbitrary host-local resampling buckets.
- A bar is eligible only when `bar_end + configured_availability_delay <=
  decision_event_time` and the data were actually received/available. A future
  completed 15m bar must never be joined backward to earlier 5m decisions.
- Deriving 15m from three validated contiguous 5m bars is preferable initially;
  a missing constituent means incomplete HTF context. Reuse the last completed
  valid 15m bar only within its explicit expected-update/staleness policy.
- Ticks/fast quotes and broker updates serve protective stops, execution, MFE
  observation and hard risk. They do not advance normal confirmation counters.
- The global entry setting `evaluateOnIncompleteCandle` cannot turn on intrabar
  thesis exits. Research has a separate explicit experimental switch if needed.
- Missing required 5m/context data pauses ordinary decisions. Missing optional
  volume weakens evidence availability, not the trade. A confirmed hard stop,
  existing confirmed invalidation or session deadline still acts immediately.
- Long operational blindness has an explicit risk policy with observable data
  ages and protection status. It may create a hard recovery/flatten obligation;
  it must not be disguised as a thesis score.

## 10. Research parameter budget

Keep a small, pre-registered initial parameter set, with shared values where
possible. Candidate starting values for fixtures are two failure closes, two
recovery closes, a two-tick minimum structural buffer and `0.1 * entry_ATR` noise
allowance. A structure-trail buffer and one review horizon per profile are the
remaining primary research knobs. Section 3.1's existing-style feature windows
and coarse volume/overlap definitions are fixed baseline assumptions, not a
simultaneous optimization grid. For initial time-review fixtures, use 6 completed
bars for breakout/range convergence and 12 for trend development; unknown-thesis
management has no discretionary time-decay exit. These are review times, not
automatic exit timers. Their final values require walk-forward sensitivity tests;
do not add per-symbol, per-hour or per-indicator micro-thresholds.

Operational freshness/latency deadlines are calibrated against observed feed and
broker behavior, not optimized for trade profitability. Cost schedules are
effective-dated inputs. Regime classifications are context estimates, not truth
labels that can unilaterally liquidate positions.

The existing `ProbabilityCalibrator` is unsuitable for exit decisions: it is an
unversioned frequency of historical **entry-score buckets** achieving gross
`+0.9R` under the prior exit policy, with no time-separated model artifact. Its
legacy `confidence` column does receive today's `signal_score`; the field name
alone is not a demonstrated storage defect. See audit F15 for the actual defects.
