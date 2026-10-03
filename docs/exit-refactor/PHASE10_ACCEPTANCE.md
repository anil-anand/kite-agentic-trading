# Phase 10 evidence workflow

The evidence blocker cannot be cleared by passing synthetic tests or by changing
the promotion flag. The repository now has an executable, offline workflow for
registered paired and portfolio studies and authentic operational capture. It does not fetch
market data, connect to a broker, activate trading, or retire legacy controls.

## Run the supplied example

From the repository root, after source changes and tests are complete:

```bash
uv run python -m backend.backtesting.acceptance register \
  --plan research_data/exit_management/phase10/acceptance_example/plan.json \
  --directory .research_runs/phase10-example
uv run python -m backend.backtesting.acceptance run \
  --directory .research_runs/phase10-example --stage oos
uv run python -m backend.backtesting.acceptance run \
  --directory .research_runs/phase10-example --stage holdout
uv run python -m backend.backtesting.acceptance status \
  --directory .research_runs/phase10-example
```

Use a new directory for each registration. The example is deliberately synthetic:
two OOS sessions, a third holdout session, two declared policies during OOS, and
two execution scenarios. Only the nominated candidate reaches the holdout. Its
control is another shared exit policy, **not the repaired legacy control**.
Example sample counts and margins demonstrate the schema; they are not research
recommendations. These results cannot authorize promotion.

## Historical inputs and registration

Copy the example plan and replace its synthetic inputs with approved exports.
The plan declares:

- Study/data-source identity and classification; symbol-to-CSV paths and retained
  entry-case paths, relative to the plan file.
- Chronological, disjoint, half-open OOS windows and a later holdout. Each has
  `fold_id`, `warmup_start`, `test_start`, and `test_end`, with explicit UTC offsets.
- Complete candidate/control policies, a small optional variant set, and named
  execution/cost scenarios including `base`. No automatic P&L optimization runs.
- Positive improvement and explicit noninferiority margins, minimum samples,
  execution limits, operational limits, and disclosed prior trials.

CSV fields are `date`, `open`, `high`, `low`, `close`, `volume`, and optional
`available_at`/`received_at`. `date` is the aware **start** of a five-minute bar.
Bars must fit the declared exchange session. Unavailable observations are not
borrowed from later windows. Labels and undeclared feature columns are rejected.
Routing timestamps are inspected when partitioning data; holdout OHLCV and
receipt values do not participate in OOS type inference or feature evaluation.

Each case retains `case_id`, aware `checkpoint_at`, `initial_capital`, and
`positions` containing the original `EntryThesis.to_dict()` and terminal-entry
`PositionState.to_dict()` records. Optional account/daily-loss settings and initial
management state use the `run_paired_case` contract. Entry context and causal
anchors must already have existed at thesis creation. Do not reconstruct a
successful trade's thesis after seeing its future path. The workflow checks
declared chronology; it cannot independently authenticate input provenance.

Registration snapshots input bytes, the full policies and limits, Git HEAD, and
the working source/dependency hashes. Outputs retain exact inputs, decisions,
replayed parity checks, fills, costs, censoring, and both branch outcomes.
Independent case accounts do not form one continuous portfolio equity curve.
Each complete report is streamed into `reports/*.json.gz`; the stage index retains
measured summaries and compressed/uncompressed/summary hashes. Status and review
verify those files. Full decision traces remain available without retaining every
monthly report in RAM.

Each stage creates an exclusive access record before executing. Failed or
interrupted attempts remain consumed. Holdout requires intact, nonempty OOS
results from the same registration. Source/data changes require a new study and
disclosure of previous attempts. Local files and hashes are integrity controls,
not a tamper-proof external experiment registry. Keep original study directories
and review the complete experiment history.

`run --workers 2` executes independent treatments in spawned processes (allowed
range 1–4; default 1). Each child retains its full report locally and returns a
compact index. Results retain deterministic treatment ordering and independent
accounts. Worker initialization errors, exceptions and abrupt process loss fail
the consumed stage; they cannot produce a completed result. Start conservatively:
parallel workers each need memory for their current treatment. Execution UUIDs
remain the original coordinator-generated identities, so equivalent repeated
runs need not have byte-identical raw trace hashes.

`.research_runs/` is ignored by Git. Keep licensed data and private account
exports there or in another approved local location; commit only non-sensitive
fixtures and reviewed summaries. The CLI rejects inputs from live app storage.

## Shadow and isolated paper observations

```bash
uv run python -m backend.backtesting.acceptance record-operational \
  --directory .research_runs/approved-study --report /approved/export.json
```

The export must follow `operational-run-v1`, documented in
[`operational_evidence.py`](../../backend/backtesting/operational_evidence.py).
It must contain the registered study/full-policy identity, actual real-time
capture provenance, every decision including HOLD, independent receipt and
persistence timestamps, exact position epochs, intents, orders, fills, source
counts, reconciliation, and censored residuals. Shadow candidate actions must
remain suppressed; paper executions must link to their candidate decisions.
The assessor replays decisions and measures completeness, timing, sample sizes,
quantity conservation and unresolved exposure. Status revalidates original
exports; it never trusts a saved `passed` boolean.

For daily captures and restarts, preregister `operational_cohorts` with fixed
`required_slots` and their actual `session_dates`. Run `claim-operational` before
each attempt and preserve its original attempt/slot identity in the capture plan.
The cohort combines distinct observed dates and all immutable reports. Missing,
failed or unresolved attempts remain failures; a retry cannot replace a failed
capture. The complete procedure is in [OPERATIONAL_CAPTURE.md](OPERATIONAL_CAPTURE.md).

The capture/export adapter is implemented. See
[OPERATIONAL_CAPTURE.md](OPERATIONAL_CAPTURE.md) for the opt-in live observer,
isolated paper CLI, input schema, and retained provenance. Receipt and persistence
are measured independently as observations occur. Queue overflow, missing epoch
joins, unexpected shutdown, or incomplete reconciliation keep an export ineligible.
Injected clocks and DEV streams are explicitly synthetic. A real paper session
requires a contemporaneous external feed; historical replay cannot satisfy it.

## Production portfolio and repaired control

Set `control_kind: "LEGACY_REPAIRED"` and supply `portfolio` with
`universe_events`, `instrument_metadata`, `strategy_config`, `risk_config`,
`initial_capital`, and optional `calibration_history`. Universe events retain their
selection time, availability time, and ordered symbol membership. This supports a
preselected universe; it does not reconstruct unavailable historical screener ranks.
The two accounts independently execute shared production admission, calibration,
risk reservations, sizing, cooldowns, fill/rejection handling and capital release.
No live singleton, credentials, journal, or wall clock enters research risk checks.

`entry_case_source: "GENERATED_CONTROL_ENTRIES"` and `cases: []` produce paired
checkpoints from every valid terminal entry in the baseline control account.
Gap-invalid and otherwise unpairable entries are retained as exclusions and block
acceptance; they are not silently removed from the population. Each checkpoint
retains the actual first-fill time for legacy holding clocks. Full-portfolio
results include changing admissions, rejected opportunities, pending obligations,
zero-trade sessions, and account equity. Paired and portfolio results remain
separate estimands.

Declare `inference_policy` before access. The implementation uses a seeded moving
session-block bootstrap, grouping contemporaneous symbols and retaining zero-trade
sessions. Stress repetitions do not multiply sample size. Missing coverage and
insufficient independent observations leave bounds unavailable. Retain independently
annotated `reference_diagnostics`, or declare `reference_policy` for the mechanical
entry-boundary diagnostic. The latter is an explicit proxy, not expert-labelled
thesis invalidation. Missing causal anchors and censored horizons are not negatives.
Reference version 2 supports the directional swing price frozen at entry as well
as range edges, using `FROZEN_ENTRY_ATR_MULTIPLE` buffer units. Version 1 retains
its range-only interpretation. Later structure never replaces the entry anchor.

## Promotion review

```bash
uv run python -m backend.backtesting.acceptance review-promotion \
  --directory .research_runs/approved-study --package /approved/review-package.json
```

The package has `manifest` and `evidence` matching `evaluate_promotion_gate` in
[`promotion.py`](../../backend/backtesting/promotion.py). In addition to that
contract, the manifest must match the registered study, source commit/tree,
dataset-map hash, full candidate/control artifacts, criteria/policy registration
time, prior trials, and exact fold/holdout windows. It includes
`registration_sha256`, `acceptance_stage_hashes`, and
`acceptance_statistics_hashes` for both executed results. Numerical claims must
match computed OOS statistics, and the untouched holdout must independently meet
the frozen materiality and noninferiority margins. Executed repaired-control and
production-portfolio treatment coverage are checked directly.
The execution/data-stress report must also retain
`observations.execution_coverage[stage]` exactly matching each stage's measured
coverage summary. A portfolio with cancelled unfilled entries is admission evidence;
it cannot be described as held-exposure exit stress. Paired retained-entry stress
is identified separately. Expiry and latency assumptions are not relaxed to create
an exposure sample.
Use the recorded OOS access `started_at` as `research_started_at` and holdout
`completed_at` as its `evaluated_at`. Operational report observations must include
`operational_export_sha256`, matching each independently assessed original export
or the complete cohort hash binding its plan, claims and original report hashes.

This review requires genuine retained results for repaired-legacy comparisons,
production portfolio admission/selection, cohort/ablation/stress coverage,
materiality and noninferiority with uncertainty bounds, holdout, operational
parity, security and broker prerequisites. Hashes bind these reports to the study;
they do not authenticate reviewers' attestations. Exact registered-treatment
coverage does not by itself prove that the experiment set covers all required
ablations, parameter families, null controls, cohorts and operation stresses in
BACKTEST_PLAN. Those substantive requirements must be assessed from retained
experiments by the reviewer; a limited three-symbol perturbation study must not be
reported as that complete package. A passing research review still
performs no live activation.

## Implementation and empirical acceptance

| Requirement | Implemented mechanism | Acceptance observation |
| --- | --- | --- |
| Historical OOS and holdout | Frozen once-only runs, causal inputs and retained traces | Actual results must meet predeclared margins and coverage |
| Repaired-legacy comparison | Shared canonical execution with frozen legacy normal policy | Retained paired and independent account comparisons |
| Full portfolio | Shared entry/risk/admission, deterministic batch priority, dynamic capacity and calibration | Complete treatment coverage and no hidden excluded population |
| Shadow and isolated paper | Durable live observer, real-time paper process and validated exports | Sufficient genuinely observed sessions, reconciliation and timing |
| Materiality/noninferiority | Session-block uncertainty, independent references and exact report binding | Adequate independent outcomes; inconclusive bounds cannot pass |
| Security prerequisites | F26 storage isolation, F29 credential boundaries, F30 IPC/provider fixes | Focused tests plus external broker/recovery observations where applicable |

Source implementation and empirical acceptance are separate. A complete workflow
can produce a failed or inconclusive study. It must not turn missing real-time
observations, limited cohorts, source uncertainty, or adverse measured results into
a passing promotion claim. The workflow never activates live trading or retires
legacy controls; those changes remain conditional on accepted evidence and the
specified controlled rollout.
