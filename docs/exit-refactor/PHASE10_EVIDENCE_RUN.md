# Phase 10 execution record — 2026-09-28

This record distinguishes implemented source, actual observations, and promotion
acceptance. No commit, real broker order, or live candidate activation occurred.

## Frozen source and checks

Final source tree: `81ad985f45f3681f0e1258b395f7b636ce103174bcdf310b8141ab5b5b62b126`.
The exact 251 source/dependency files are archived in
`.research_runs/public-nse-phase10-20260928-v4/frozen-source.zip`, with hashes,
and copied into its isolated `source/` execution directory.
The final source passes **1,897 Python tests**, including cohort and coverage
regressions. The unchanged frontend retains 27 passing TypeScript tests, 2 passing
polling tests, and lint/typecheck/build verification. Final Ruff lint/format
(206 files) and diff checks pass.
One existing unused `SignalCard` lint warning remains.

## Historical study

The first source's final registered run was `.research_runs/public-nse-phase10-20260928-v3/`, frozen
at 2026-09-28 10:17:01 UTC.
at the following full portfolio and paired terminal-entry comparison scope:

- OOS: January, February and March 2024, separate chronological folds.
- Untouched holdout: April 2024, only the frozen nominated candidate.
- Universe: HDFCBANK, INFY and RELIANCE, selected before outcome access. This is a
  static research universe, not reconstructed historical NIFTY membership.
- Control: correctness-repaired legacy normal management, with shared production
  entry/risk/calibration and independent evolving account state.
- Treatments: candidate plus a predeclared confirmation neighbor during OOS;
  base and adverse cost/latency/participation assumptions.
- Inference: session blocks, explicit censoring and missing coverage, frozen
  materiality/noninferiority limits, and a labelled mechanical boundary reference.

Inputs came from the public
[publisher dataset](https://huggingface.co/datasets/xxparthparekhxx/indian-stock-market-minute-data)
at revision `91fc0616910dc9d92c5e34f8470dd58817529cc8`. Raw acquisition hashes,
normalization scope, and the plan are retained in `.research_runs/public-nse-inputs/`.
Only complete five-minute buckets and declared ordinary weekday sessions are
used; missing bars are not filled. The publisher's accuracy and corporate-action
handling were not independently certified. Historical receipt times are unavailable,
so completed-bar availability is a declared simulation assumption.
The frozen common `equity-intraday-rates-v1` charge model is used in both branches;
its rates/rounding are not claimed to reproduce certified 2024 contract notes.
Fee accuracy remains part of the separate broker/cost evidence requirement.

**Input qualification failed for historical executable units.** A primary-source
check against the [NSE 1 January 2024 daily archive](https://nsearchives.nseindia.com/content/historical/EQUITIES/2024/JAN/cm01JAN2024bhav.csv.zip)
found HDFCBANK open 853.00 versus official 1,706.00, RELIANCE 1,290.30 versus
2,580.55, and INFY 1,539.00 matching 1,539.00. The first two series are consistent
with later 1:1 bonuses ([HDFC August 2025 disclosure](https://www.hdfc.bank.in/content/dam/hdfcbankpws/in/en/personal-banking/about-us/stakeholders-information/disclosures/other-stock-exchange-disclosure/18/28aug2025-se-disclosure-allotment.pdf),
[Reliance October 2024 disclosure](https://www.ril.com/sites/default/files/2024-10/SE_16102024.pdf)).
Volumes also differ in the direction expected from adjustment. The publisher
does not provide a verifiable adjustment-factor/reconstruction contract.

Consequently this frozen run is a **comparison on publisher-adjusted price bars**.
It cannot establish historical executable price/share units, tick/lot sizing,
liquidity capacity or fee accuracy. Multiplying by two does not exactly recover
lost ticks: HDFCBANK's adjusted high 854.60 doubles to 1,709.20 while the official
high is 1,709.15. The same adjusted prices were returned by unauthenticated public
Upstox historical endpoints. No dataset or parameter is being silently rewritten.
Historical execution acceptance needs qualified inputs and a separate preregistered
run; completion of this limited run cannot clear that requirement.

Six previous development/interrupted attempts are disclosed. Four used December
only for implementation/profiling. The first OOS attempt was interrupted when an
authentic paper observation exposed a structural tie-break bug. No historical
results were used to choose the correction or change the frozen policies/margins.
The first registration, access claim and interruption reason remain retained.

The second OOS attempt's first January adverse portfolio treatment
completed with 322 accepted limit orders, all cancelled before a fill under the
predeclared latency/entry-expiry assumptions. Both accounts reconciled flat with
zero completed trades; these are not counted as trade samples. The source freeze
was explicitly interrupted after the real paper pilot exposed unbounded recorder
memory and quadratic duplicate scans. No base portfolio, paired or holdout
results were inspected. The interruption record is retained. The new registration
discloses all six previous attempts and retains the policies, windows, datasets
and margins unchanged.

**Historical execution status: interrupted and disqualified for executable units.**
The v3 attempt was stopped at 10:36:03 UTC after the primary-source qualification
failure. Three portfolio report files had been persisted; none of this attempt's
portfolio, paired or holdout outcome summaries were inspected. Its interruption
record and all reports remain retained. A replacement CC0 archive, published before
the later bonuses, is being acquired for January–April 2022. The period change is
due solely to qualified source coverage, not outcome selection. A separate
registration will disclose this seventh previous development/interrupted attempt.

Full reports live in `reports/*.json.gz`; stage indexes bind compressed,
uncompressed and summary hashes. The access record is consumed even if interrupted.
Do not restart the same registration; a new attempt requires an explicit new plan
and prior-trial disclosure.

## Qualified replacement run

`.research_runs/public-nse-phase10-20260928-v4/` was registered at
12:32:51 UTC and started from its isolated final source at 12:33:11 UTC, using four
workers. Its OOS periods are January, February and March 2022; its untouched holdout
is April 2022. Candidate, repaired control, confirmation neighbor, execution costs,
sample minima and materiality/noninferiority margins are unchanged. The diagnostic
is explicitly versioned to support frozen entry swings as well as ranges, and
stress reports distinguish admission-only accounts from filled exposure. No
historical outcome was used to select these corrections.

The [CC0 publisher archive](https://www.kaggle.com/datasets/debashis74017/stock-market-data-nifty-50-stocks-1-min-data)
is pinned to version 9, published June 2023, predating the later bonuses.
Its actual coverage ends in October 2022. The superseded 2023 acquisition proposal
and its pre-acquisition correction to 2022 remain retained. Comparison with
[NSE's 3 January 2022 archive](https://nsearchives.nseindia.com/content/historical/EQUITIES/2022/JAN/cm03JAN2022bhav.csv.zip)
confirms the original price scale for all three selected symbols. HDFC and INFY
open/high/low/last match; RELIANCE opens match, extrema differ by ₹0.15 and last
price by ₹0.95. Vendor volumes differ from NSE totals by approximately 0.27%,
0.32% and 2.24%, respectively. This qualifies price units, not exhaustive tick data.

All 54 publisher indicators were discarded. An independent Decimal calculation
reproduced all 21,357 retained five-minute bars from consecutive source minutes.
Version/license records, source ranges, normalized hashes, official calendars,
session coverage and independent validation are retained under
`.research_runs/public-nse-raw-inputs/`.

Of 103 expected December–April sessions, eight are wholly absent: December 6–9,
February 9–11 and April 18. December 20 and March 7 are partial. Missing/incomplete
bars are not filled; these limitations remain part of the study's interpretation.
Exact 2022 fees and broker execution are not certified by this dataset.

The replacement also preregisters five separate capture slots per operational
mode for September 29–30, October 1 and October 5–6, using the
[NSE 2026 trading calendar](https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf).
These are planned observations, not completed sessions. Every capture must be
claimed before it starts and imported with its original identity. Failed and
pending attempts cannot be replaced by successful retries. No live shadow capture
or broker order has been started by this registration.

**Replacement historical status: running.** Completion and passing acceptance are
separate results; neither is claimed in advance.

## Authentic isolated paper observation

Both attempts use public contemporaneous quotes, actual receipt/persistence clocks,
a declared one-share manual operational premise, shared candidate/coordinator code,
and simulated execution. They do not establish production entry selection or real
broker fill behavior. The standalone pilot is not a substituted LIVE_SHADOW run or
a complete five-session registered acceptance sample.

The first attempt, `attempt-20260928T092007`, retained 103 replay-verified HOLD
observations and one paper entry fill. It was explicitly failed/stopped when the
simultaneous-history swing selection defect was found. Residual quantity one is
censored; no closing fill was invented.

The corrected attempt, `attempt-20260928T092747`, uses source
`94e7169b3a876351a89f12b6c953dd8e20ce5070667b75da0ca92af32594155f`, preceding
the recorder storage and test-fixture corrections. It is not relabelled as an
observation of the later final source.
At the actual 15:15 IST deadline it requested `SESSION_FORCED_FLAT` at
15:15:00.254619, acquired at 15:15:00.253510 and persisted at 15:15:00.385018.
One flatten intent and one order were created; repeated critical evaluations reused
that obligation. This measures decision/recording latency, not broker execution
latency. The five-minute model requires a later eligible completed bar for the
simulated fill.

**Paper execution status: completed capture; unresolved execution.** The source
hash was verified unchanged at END. All 1,567 decisions passed replay and timing
verification: 984 HOLD and 583 REQUEST_EXIT, with maximum receipt-to-persistence
lag 2.360001 seconds. One session, one entry fill and three intents were retained.
One share remains open with a working exit order and explicit censoring. No
duplicate exit order or invented closing fill was recorded.

The feed stopped advancing at 15:15 IST. It never supplied the later eligible
15:20-start candle, so the declared five-minute execution model could not fill
the order created just after 15:15. The pilot assessment failed closed-position
and unresolved-position requirements; the promotion assessment also failed its
preregistered session minimum. This is observed deadline/stale-data behavior,
not evidence of timely real broker execution or a successful paper closure.

At 15:28:50 IST a separate original-URL versus unique-nonce check still received
the same stale quote/candle with HTTP cache ages of two seconds and zero seconds.
Fresh HTTP delivery did not imply fresh market data. The original pilot did not
retain HTTP headers, so its historical cache state cannot be reconstructed.

Artifacts and both assessments are under
`.research_runs/public-paper-pilot/attempt-20260928T092747/`; the independent
feed diagnosis is under `.research_runs/public-feed-diagnosis/20260928T095850/`.
The final disk-backed reader re-exported this actual 4,870-event capture with
exactly the original report hash; `new-reader-compatibility.json` retains that
verification. Its observations and acceptance failures were not changed.

## Acceptance meaning

Working source, passing tests, a limited historical study and one paper session
cannot establish the complete ablation/cohort, live-shadow, multi-session paper,
broker/recovery and substantive research review required by BACKTEST_PLAN.
Actual metrics must meet the preregistered limits. Missing, inconclusive or adverse
observations keep promotion closed. Legacy controls remain available until that
conditional acceptance and controlled rollout are satisfied.
