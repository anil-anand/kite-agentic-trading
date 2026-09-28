# Phase 10 pre-commit review

The findings below have been corrected in source. The follow-up now includes
production portfolio admission, repaired legacy control, operational recorders,
measured statistical inference, and the previously missing security prerequisites.
Empirical study outcomes and operational sample coverage are recorded separately;
source correctness does not imply that a candidate has demonstrated improvement.
Live activation and legacy retirement remain conditional on accepted evidence.
No commit or real broker order was made.

## Findings and applied corrections

### P10-01 — HIGH: OOS outcomes could borrow the next fold or holdout

**Code:** `WalkForwardValidator._assert_scored_trades`, `plan_folds`,
`_assert_scored_equity`; `MetricsEvaluator.evaluate_walk_forward`.

Only entry time was checked: a trade entered during OOS could exit in the next
fold or reserved holdout and count that outcome. Warmup/future equity could
alter risk metrics. A short terminal fragment also counted as another fold.

An expert scores only outcomes available in the declared interval and reports
unfinished exposure as censored. The correction requires valid aware timestamps
and `test_start <= entry <= exit < test_end`, checks equity ownership/order,
retains unresolved execution metadata and emits complete windows only. A terminal
equity snapshot at `test_end` is allowed; it does not authorize a boundary fill.

### P10-02 — HIGH: date-only splits admitted unavailable data and labels

**Code:** `WalkForwardValidator._normalise_frame`, `_slice`, `validate_study`.

Historical rows received after selection, or labels completed after its cutoff,
could reach the selector. Naive times silently became UTC despite the legacy
helper's exchange-time convention. Future label columns, nested mutable object
cells and DataFrame metadata could leak into later callbacks.

An expert cannot use later corrections or unfinished outcomes. The correction
requires explicit aware timestamps, filters supplied availability/receipt and
label horizons at each cutoff, strips declared outcome columns from warmup/test,
rejects nonscalar cells and drops DataFrame metadata. Callers must declare all
supervised labels and process test events causally; arbitrary callbacks remain
unverified and cannot establish shared-engine parity.

### P10-03 — HIGH: selection and artifacts were insufficiently frozen

**Code:** `WalkForwardConfig`, `WalkForwardValidator._validate_selection`,
`validate_study`.

The selector named its criterion after selection; mutable artifacts could change
afterward. Candidate hashes lacked the complete reconstructable policy set.
Discarded prior trials and initial account state were not retained.

An expert preregisters the small candidate set and criterion and retains failed
experiments. The correction pins the criterion before selection, detaches
artifacts/results, retains policies and selections with hashes, and records
initial capital/reset state, declared prior trials, fold metrics and censoring.
This does not sandbox callbacks or independently prove disclosure of all trials.

### P10-04 — HIGH: promotion could pass on unsupported evidence

**Code:** `PromotionCriteria`, `evaluate_promotion_gate` in
`backend/backtesting/promotion.py`.

A minimal manifest, holdout status string and booleans could pass without
materiality/noninferiority, paired/portfolio, ablation or cost-stress evidence.
Reports were not bound to the study/dataset/policy. Booleans counted as sample
integers, negative failure rates passed, and security gates could be omitted.

An expert requires reproducible capture or premature-exit improvement without
unacceptable delayed invalidation or stressed losses. The correction binds
retained hashed reports and evaluated metrics to the study, dataset, policy,
source and criteria; requires research/operational/holdout/security reports;
checks counts and numeric domains; and compares uncertainty bounds with explicit
materiality/noninferiority margins. Raw-lab, synthetic and unverified callback
studies cannot pass. Declaration chronology uses actual research access times,
not the historical market dates being backtested.

Hashes establish integrity and matching identity, not an attestation's truth.
The gate is an offline review function with no live dispatch authority.

### P10-05 — MEDIUM: feature warmup still reached execution state

**Code:** `BacktestEngine.run`, `_aware_time`.

Early signals were suppressed, but warmup candles still reached the broker.
A supplied account with orders/exposure could fill or charge fees before scoring
while claiming feature-only warmup. Invalid/naive boundaries and availability
values were not rejected reliably.

An expert starts an independent fold with its declared untouched account. The
correction rejects carried state for this mode, keeps warmup candles out of
broker processing, preserves feature history, and validates timestamps before
execution. Continuous-state studies require a separately declared design.

### P10-06 — MEDIUM: reset folds produced a fictitious account risk path

**Code:** `MetricsEvaluator.evaluate_walk_forward`.

Although continuous MTM was marked unavailable, nested trade metrics still
reported drawdown and Sharpe from concatenating independent reset accounts.
That curve represents no actual account. An expert keeps each account's risk
path separate. The correction preserves additive financial/R summaries and
makes cross-fold drawdown and trade-day Sharpe explicitly unavailable. Individual
fold risk metrics retain their original basis.

### P10-07 — MEDIUM: fixture labels could misrepresent the executed policy

**Code:** `RegressionSuite.load_fixture_manifest`, `_raw_fixture`,
`_verify_execution_manifest`, `run_regression_test`.

A candidate `REPLAY` manifest could be attached to a raw-strategy run that never
executed the candidate. Unknown modes/model mismatches and an unverified second
file read were accepted. NaN expectations could evade the tolerance comparison.

An expert requires the reported controller and assumptions to match execution.
The correction validates modes, rejects candidate fixtures on the raw runner,
checks the actual model version, parses verified bytes and rejects nonfinite
metric comparisons.

### P10-08 — MEDIUM: cohort counts hid concentration and missing coverage

**Code:** `MetricsEvaluator.evaluate`.

Symbol/playbook/time counts alone could not show that apparent improvement came
from one group; missing metadata/R denominators were easy to overlook. An expert
examines outcomes and sample coverage before claiming robustness. The correction
adds cohort financial and gross/net-R outcomes, sample sizes and coverage,
including available regime/side/setup/sector/liquidity metadata. Unknowns remain
unavailable; these are diagnostics, not per-symbol tuning rules.

## Adversarial validation

The four `test_phase10_*_review.py` modules for walk-forward, metrics, promotion
and warmup cover the research defects, malformed evidence, mutation and boundary
cases above.

`test_phase10_robustness_review.py` runs seven fixed policy variants: baseline,
one/three-close confirmation, adjacent structural buffers and adjacent review
horizons. It checks intact retests with adverse dynamics/volume spikes,
structure failure without oscillator confirmation, and news-like hard-stop
preemption. One-close confirmation is an ablation, not a recommended policy.
Five fixed execution scenarios stress slippage, latency, participation and
partial fills. A rebound without liquidity cannot fill or revoke an exit;
eventual reductions reconcile exactly the original quantity and net cash.
All 26 scenarios passed without selecting a variant by P&L.

Initial review validation: **1,582 Python tests passed**. `uv run ruff check backend/
run_backend.py`, `uv run ruff format --check backend/ run_backend.py` and
`git diff --check` passed. The changed Python files were formatted with Ruff.
At that initial review, no frontend files had changed, so frontend build checks
were not required. The follow-up frontend changes and checks are recorded below.

Existing pure-policy, shadow, replay and execution suites also exercise trend
pullbacks, breakout retests/failures, VWAP reclaim/rejection, oscillator-only
deterioration, regime transitions, valid profitable retracements, confirmed
exhaustion, stale/missing data, gaps, unknown submissions, failed modifications,
stop/cancel races and partial residuals. Shared-policy synthetic tests establish
their asserted invariants, not empirical superiority or actual broker latency.

## Acceptance and implementation handoff

The follow-up [executable acceptance workflow](PHASE10_ACCEPTANCE.md) replaces
the manual paired-study handoff with frozen input registration, actual shared
candidate/control execution, retained parity/fill/censoring artifacts, single-use
holdout execution, operational-export measurement, and bound promotion-package
review. `research_study.py`, `operational_evidence.py` and `acceptance.py` contain
the implementations. A three-session synthetic example is included for exercising
the CLI. It is not evidence of historical effectiveness or live operation.

The workflow review also corrected cross-session simulation inputs, holdout
feature/receipt contamination during CSV parsing, unverified OOS results unlocking
holdout, duplicated retained entries inflating sample counts, invalid session
bars reaching execution, and copied/tampered reports appearing accepted. Only the
preregistered candidate reaches holdout; stress/variant repetitions never inflate
baseline sample counts. Failed accesses remain visible and consumed.

## Completion of the missing implementation

### P10-09 — HIGH: research bypassed production admission and account state

**Code:** `RiskManager.for_research`, `BrokerSnapshot.entry_ready_at`,
`run_portfolio_study`, `CandidateRunner`, `SimulatedBroker`, `bucket_success_probability`.

Fixed-entry replay could not show how exits free capital, change cooldowns and
calibration, or affect later accepted/rejected opportunities. Live globals and
wall clocks could leak into a historical adapter. The correction injects config,
clock, verified accounting/count/correlation/sector providers and canonical broker
snapshots into the existing risk algorithm. Two independent accounts use the shared
entry, sizing, reservation and calibration logic. Actual fills determine exposure;
partial entries remain protected; invalid fill-bound risk and unfinished orders
remain explicit. Paired cases retain first-fill age and terminal management start.
An experienced trader manages the real remaining allocation and available capital,
not a collection of independently funded hindsight trades.

### P10-10 — HIGH: live/research entry order and feature histories differed

**Code:** `entry_ordering.py`, `Scanner._fetch_candles`, live scan-batch admission,
`run_portfolio_study`, `run_paired_case`.

Concurrent callback completion order could allocate scarce risk capacity differently
from historical ranking. Research's growing history also changed indicators relative
to the live scanner's five-calendar-day source window. Both paths now share stable
batch ordering and the same candle-history horizon and tick normalization. Account
calibration remains branch-specific. Bounded exact-content hashing and event-local
immutable context sharing reduce repeated computation without changing decisions.

### P10-11 — HIGH: operational evidence had no authentic acquisition path

**Code:** `OperationalRecorder`, journal commit observer, `RealtimePaperSession`,
`assess_operational_run`.

Retrospective decision timestamps cannot establish actual receipt/persistence lag,
complete capture, or recovery history. The live observer now records after durable
commit without granting dispatch authority. The isolated paper process uses real
receipt clocks, fresh quotes/completed bars, independent supervision and canonical
execution. Positive quote latency, mid-entry-bar extrema, queue starvation and
reconciliation histories are handled explicitly. Unknown entry obligations and
incomplete captures remain failures. Fixture clocks and DEV are synthetic. Runtime
source hashes are verified at startup and finish; one run cannot claim another
revision. This preserves the distinction between actual execution facts and intent.

### P10-12 — HIGH: reports could overstate statistical certainty

**Code:** `summarize_paired_stage`, `session_block_interval`,
`derive_entry_boundary_references`, acceptance `_review_package`.

Repeated treatments are not independent trades. Missing invalidation observations
are not negative examples; delayed bar receipts do not extend observed exposure.
The correction groups symbols by session, resamples session blocks, retains empty
sessions/censoring, validates entry epochs and scoring windows, and compares measured
bounds against frozen margins. Degenerate or incomplete samples remain inconclusive.
Independent reference events are causal; the optional mechanical boundary proxy is
labelled explicitly. Both OOS and holdout must establish the required evidence.
Hand-entered metrics, missing treatments, unpaired entries or a shared-policy control
cannot replace the executed portfolio/repaired-legacy study.

### P10-13 — CRITICAL/HIGH: F26/F29/F30 source prerequisites were unresolved

**Code:** backend runtime/config/journal/analytics/calibration paths; Electron
runtime/credential/IPC/navigation boundaries; `llm_client.py` provider binding.

DEV shared persistence with LIVE, credentials crossed renderer boundaries, blank
saves could erase secrets, and privileged routes/provider configuration lacked
sufficient isolation. DEV now has a separate namespace across all consumers;
auth returns identity only; verified credential replacement is atomic; blank saves
preserve native secrets. IPC validates channel, sender frame and URL; windows are
sandboxed; callback URLs are pinned; provider presets and key binding prevent keys
from following arbitrary hosts. Tests exercise storage separation, failed writes,
credential scrubbing, untrusted frames, redirects and provider changes. No real
credentials were accessed during this review.

### P10-14 — MEDIUM: full studies could exhaust memory retaining traces

**Code:** acceptance retained-report persistence and study artifact verification.

Monthly portfolio and paired runs produce substantial decision traces. The study
must retain those facts without accumulating every full trace in one process and
serializing another complete in-memory copy. Full reports are persisted separately;
measured summaries retain content-bound references. Altered or missing retained
reports cannot establish acceptance. This is storage organization, not sampling or
trace deletion.

### P10-15 — HIGH: simultaneous history acquisition selected obsolete structure

**Code:** `_management_profile` in `exit_management/thesis.py` and structural
candidate selection in `exit_management/evidence.py`.

The first authentic paper experiment exposed equal `known_at` values for swing
levels received together during startup. Selecting only by `known_at` could choose
a four-day-old swing even when more recently formed valid structure was available.
That gave a trend premise an unnecessarily distant boundary. An experienced trader
uses the most recently formed valid level among simultaneously known alternatives.
The correction orders by knowledge time, formation time, then stable level identity;
regressions cover equal-receipt levels and input ordering. The source change invalidated
the active research freeze. The interrupted historical attempt and the first paper
attempt remain retained and disclosed, rather than relabelled as corrected evidence.

Candidate live dispatch remains disabled. Conditional rollout and legacy retirement
must follow accepted empirical evidence; deleting the legacy normal controls before
that condition would violate the implementation specification. Existing CI runs the
Python regressions; no duplicate test job was added.

### P10-16 — HIGH: evidence capture could exhaust the supervised process

**Code:** `OperationalRecorder`, `LiveOperationalObserver.close`, and
`RealtimePaperSession` reporting histories and intent joins.

The authentic 28-minute, one-position pilot exposed full event retention in RAM,
linear duplicate scans per decision, and repeated scans/copies of growing paper
histories. Extended observation could exhaust memory or delay supervision. An
experienced operator's diagnostic recorder must preserve evidence without consuming
the trading process's resources as though every old decision were still active.
Capture now uses a verified disk-backed log, a compact identity index, incremental
paper execution joins, and bounded in-memory diagnostic history. Live shutdown
seals the log without materializing its entire export. Offline exports retain all
decisions and exact joins; source validation, censoring and failed captures remain
unchanged. Tests cover retained memory, duplicate/tampered logs, repeated pending
exits, complete replay and reopen/export compatibility.

Independent historical treatments can also run in bounded spawned workers. Full
reports stay in their worker until persisted; the parent receives compact indexes.
Tests compare sequential and parallel trading outcomes and trace relationships,
and exercise initialization errors, normal failures and abrupt worker death.
This changes computation scheduling only. Original execution IDs are retained.

### P10-17 — MEDIUM: lifecycle tests depended on the host's market session

**Code:** the shared `lifecycle` fixture and the capital-safety/external-close
fixtures under `backend/tests/`.

After the real 15:15 deadline, 36 previously passing brokerage tests failed because
they implicitly expected an open market. Production correctly requested forced
flattening. These unit fixtures now explicitly provide their intended open-session
classification while preserving actual receipt timestamps and accounting dates.
The fixture is opt-in; dedicated deadline tests retain their explicit clocks.
All 213 affected lifecycle/accounting/recovery and real session-boundary tests,
plus 26 capital-safety/external-close tests, pass after the deadline. Production
session rules were not changed to accommodate the tests.

### P10-18 — HIGH: adjusted historical prices changed executable units

**Inputs:** the public revision pinned by
`.research_runs/public-nse-inputs/study-plan-v3.json`; provenance and primary-source
comparisons are retained in [PHASE10_EVIDENCE_RUN.md](PHASE10_EVIDENCE_RUN.md).

The first source provided 2024 HDFCBANK and RELIANCE prices consistent with later
1:1 bonuses. Those bars can look plausible while changing historical share sizing,
tick rounding, participation and rupee-based costs. An execution study must use
qualified historical units. Doubling adjusted prices cannot recover already rounded
ticks or establish the original volume convention. The correction is to disqualify
that study for historical execution acceptance, preserve its consumed attempt, and
acquire a versioned archive predating those bonuses. Qualification checks against
NSE daily records precede a new registration; policies and acceptance margins are
unchanged. Publisher indicators are discarded. The replacement remains subject to
ordinary vendor completeness limitations and to the actual acceptance results.

### P10-19 — HIGH: operational import did not support a restart-safe cohort

**Code:** `acceptance.record_operational_evidence`, operational assessment, and
the recorder's capture identity.

The importer retained exactly one immutable report per mode. A multi-session
study therefore depended on one uninterrupted process; a daily report could not
be followed by another report in the same study. Removing failed reports to make
room would erase evidence. The correction preregisters session slots, claims each
attempt before capture, and retains each report separately. Cohort assessment
combines distinct sessions and validates every attempt. Missing, failed and
unresolved attempts remain visible; retries cannot replace them. Recorded decisions
are replayed without rewriting IDs. Paper identities are scoped to their capture,
while duplicate live position/order/fill histories cannot inflate sample counts.
The importer validates one full export at a time rather than loading the whole
cohort into memory. Actual future observations remain necessary.

### P10-20 — MEDIUM: an empty stress portfolio could be described as exit exposure

**Code:** `portfolio_study.py`, retained report summaries and acceptance's executed
coverage checks.

With a one-observed-bar entry expiry and one-bar execution latency, an entry can
expire before it becomes fill-eligible. A flat, complete account in that scenario
is admission/cancellation evidence; it does not demonstrate adverse management of
held positions. The correction records measured entry and filled-exposure coverage
and binds the review's coverage claims to it. Retained-entry paired stress remains
separately identified. Neither expiry nor latency is changed to manufacture fills.

### P10-21 — MEDIUM: the mechanical reference omitted valid entry swing boundaries

**Code:** `reference_diagnostics.derive_entry_boundary_references`.

Trend premises retain a swing with a `price`, while range premises retain `low`
and `high`. A range-only diagnostic marked valid trend entries unobservable and
could never establish complete delayed-invalidation coverage for a mixed study.
The correction versions the diagnostic to include the appropriate frozen entry
swing price. It retains the same confirmation/buffer rules and causal availability
checks. It neither selects later structure nor substitutes an unrelated range for
an invalid swing. This remains a labelled mechanical diagnostic, not independent
expert adjudication of a trade's thesis.

## Severity classification

| Severity | Disposition |
|---|---|
| CRITICAL | F29 credential exposure/persistence and destructive blank-save behavior corrected in P10-13 |
| HIGH | P10-01–04, P10-09–13, P10-15–16 and P10-19 source findings corrected; P10-18 replaces disqualified adjusted inputs with a qualified pre-bonus archive; empirical acceptance is evaluated from retained observations |
| MEDIUM | P10-05–08, P10-14, P10-17 and P10-20–21 corrected |
| LOW | No separate finding |

## Final source validation

After all corrections: **1,897 Python tests**, **27 TypeScript tests**,
and **2 polling tests** passed. Ruff lint/format, frontend lint/typecheck/build,
and diff checks passed. Frontend lint retains one existing unused `SignalCard`
warning. Logs and the source-bound validation record are retained under
`.research_runs/public-nse-phase10-20260928-v4/` and `.research_runs/phase10-*.log`.

The first public historical study preregistered January–March 2024 OOS and April
2024 holdout. Its third attempt was stopped for P10-18 without inspecting that
attempt's portfolio, paired or holdout outcomes. Exact source bytes, consumed
attempts and failed/censored paper pilots are preserved. A replacement pre-bonus
archive was acquired for January–March 2022 OOS and April 2022 holdout, retaining
HDFCBANK/INFY/RELIANCE, repaired legacy control, one confirmation perturbation,
base/adverse execution, independent account admissions and the fixed uncertainty
and margin criteria. The final source is frozen at
`81ad985f45f3681f0e1258b395f7b636ce103174bcdf310b8141ab5b5b62b126`; the replacement
study is running from its isolated source copy. These limited cohorts/experiments are not a claim of complete
ablation or broker operational coverage.

Actual run provenance and current outcomes are recorded in
[PHASE10_EVIDENCE_RUN.md](PHASE10_EVIDENCE_RUN.md).
