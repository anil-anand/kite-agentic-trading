# Repository audit and engineering roadmap

**Baseline:** `021fe80a3c856e35194638a170212c460fd9ae00`.
**Inspection date:** 2026-09-19. **Disposition:** findings and design only.

## Executive summary

The premature-exit complaint has direct implementation support. Normal management
uses absence/opposition of fresh entry events, a one-candle rejection rule and
elapsed-time breakeven, without a durable entry thesis or live MFE/MAE state.
Changing a few indicator thresholds will not fix that conceptual mismatch.

The most urgent findings, however, are foundations beneath the policy:

1. **Broker response casing is inconsistent with backend consumers.** Verified
   consequences include skipped position adoption, zero-price journal exits and
   missing turnover charges.
2. **Order uncertainty can become duplicate exposure-changing requests.** Protective
   cancellation is unconfirmed; placement timeouts are retried; LIMIT exceptions
   also cause a MARKET fallback. Residual fill state is incomplete.
3. **Hard risk is not continuously supervised.** Scanning/fill waits share the
   monitoring loop, all new broker orders get noncritical priority, and square-off
   stops the loop after submission rather than confirmed flatness.
4. **Portfolio exposure is understated/misclassified.** Active trade records omit
   quantity while risk sums it; marks/ownership/order roles are not modeled;
   correlation failure returns zero.
5. **Research cannot establish live exit quality.** Backtests bypass production
   playbooks/risk/exits; walk-forward does not select/train; simulation and what-if
   assumptions materially differ. Existing calibration is descriptive frequency,
   not verified OOS calibration.
6. **Operational boundaries need urgent separate repair.** Secrets can reach
   persisted renderer auth state; DEV and LIVE use shared storage; important
   operator close/cancel controls do not execute their advertised actions.

The right refactor is a narrow deterministic thesis/lifecycle engine plus the
minimum broker, risk, temporal-data and persistence correctness it needs. Preserve
the existing gateway/risk/journal/strategy infrastructure and fix specific defects.

## Audit method, coverage and validation

Reviewed the complete production backend, all 17 strategy implementations,
aggregation helpers and three playbooks; live entry/exit/protection/adoption and
reconciliation paths; risk/config/cost/journal/analytics/calibration; all research
modules, development broker, ticker and AI integrations; every renderer page,
shared contract and Electron bridge/auth/storage path. Surveyed the full test
inventory and inspected the critical fixtures/assertions for trading, broker,
research, persistence, analytics, strategies and operator contracts. Reviewed
build/CI/installer/configuration and existing documentation. Generated bundles,
dependencies, private runtime account databases/logs and credential stores are
not evidence of source correctness and were not used for account analysis.

Evidence references below are baseline **file:line** ranges plus symbols, not
claims about actual historical incidents. “Confirmed” means code path and, where
specified, a synthetic offline reproduction. Frequency and monetary impact in
the operator's history remain unmeasured.

### Baseline checks performed

| Check | Result |
|---|---|
| `uv run --locked pytest` (temporary HOME from conftest; DEV off) | 340 passed in 4.41s; two third-party deprecation warnings |
| `uv run --locked ruff check backend/ run_backend.py` | Passed |
| `uv run --locked ruff format --check backend/ run_backend.py` | Passed; 87 files formatted |
| `npm run lint` | Passed, four existing unused-symbol warnings |
| `npm run typecheck` | Passed |
| `npm run build` | Passed, including main/renderer compilation |
| `npm run test:strategy-settings` | Could not start: local `tsx` executable missing; declared dependency exists in package.json |
| `node --experimental-strip-types --test tests/strategy-settings.test.ts` | Same frontend settings test passed (1 test), using Node's TypeScript support |

No application code/tests were changed to obtain these results. Native broker
latency, live order-type acceptance and actual fills were not exercised.

### Synthetic offline probes performed

The probes ran the existing functions with fake broker responses and a temporary
HOME under the approved temporary directory; they made no authenticated/network
requests. Reproduction inputs/results are included so the next agent can promote
them into contract tests:

| Probe | Input/path | Observed result |
|---|---|---|
| Real-shaped adoption | `convert_keys` of a 10-share position with `average_price=100`; `_adopt_position` | No tracked position, zero protective-stop calls |
| Completed exit price | CamelCase COMPLETE order with `averagePrice=105`; `_sync_exit_pending_status` | `journal.close_trade` received `0.0` |
| Failed position lookup | `get_positions` raises; `_find_live_position_by_symbol` | `{}`, identical to flat lookup |
| Turnover charge update | Position with `buyValue=1000`, `unrealised=20`; `update_from_positions` | Daily P&L `20.0`; incurred entry fees absent |
| Exposure admission | Active record shaped like engine output, lacking quantity; gross cap 1000; proposed notional 500 | Accepted; existing exposure contributes zero |
| Candle failure | Empty `_fetch_candles`; 90-minute long temporarily losing; `_reevaluate_positions` | Weak-conviction exit requested |
| Unknown close accounting | Long entry 100 x 10; `close_trade(..., 0, 'UNRECONCILED')` | Gross P&L `-1000.0` |
| Calibration alias/invalid denominator | Ten +1R trades at `signal_score=85` plus the unreconciled record | Score stored as `confidence=85`; calibrator returns `10/11`, n=11 |
| Ambiguous placement | Timeout and reconciler returns no match, one retry configured | Two submission attempts |
| Coarse-bar stop/target | Long near 100, SL95, target110; bar O100/H112/L94/C104 | Stop-first exit `94.91`, MFE112, missing exit reason |

## Severity and scope notation

- **Critical / P0:** must fix before the relevant live/risk/operational reliance.
- **High / P0-research:** must fix before treating research as evidence for promotion.
- **High / P1:** material trading/intelligence/architecture improvement.
- **Medium / P2:** useful but not a prerequisite for the minimal coherent exit policy.
- **Low / P3:** future experimentation.

Effort is relative: S = localized contract/rule, M = several components/tests,
L = multi-phase lifecycle/data work. Delivery states distinguish **exit refactor**,
**urgent separate fix**, and **follow-up**. The “live gate” on every finding states
whether it blocks live reliance, research reliance, or only activation of that feature.

## Critical correctness / safety findings

### F01 — Broker/domain field mismatch corrupts lifecycle and accounting

- **Severity/priority:** Critical / P0. **Components:** Kite adapter, engine, risk,
  journal and dashboard.
- **Evidence:** `kite_client.py:10–21,55–94` converts positions/orders/trades to
  camelCase. Engine reads `average_price` at `660,694,977,1550,1658`, and fill
  `transaction_type` at `1626`; risk reads `average_price/transaction_type` at
  `83–89`, `buy_value/sell_value` at `230–236`, and snake_case order fields at
  `369–378`. Dashboard uses snake_case averages at `main.py:248–254`.
- **Problem/impact:** verified adoption is skipped; actual fill prices can fall
  back to signal price or zero; short/pending exposure and fees are wrong. A
  plausible-looking UI can coexist with incorrect risk/journal state.
- **Solution:** normalize once into typed Python contracts; explicit frontend DTO
  mapping; require verified values for accounting; SDK-shaped contract fixtures.
- **Effort/dependencies:** M; first prerequisite for F02–F11 and all exit metrics.
  **Live gate:** yes. **Delivery:** exit refactor phase 1.

### F02 — Ambiguous submissions/cancellation can duplicate or reverse exposure

- **Severity/priority:** Critical / P0. **Components:** request policy, broker
  adapter, execution gateway, exit helper.
- **Evidence:** `request_policy.py:211–233` converts an unreconciled ambiguous
  order outcome into RETRYABLE. `kite_client.py:112–152` creates a new local tag
  per facade call and permits broad/time-parse-failed fallback matching.
  `trading_engine.py:1438–1450` clears the stop ID before unconfirmed cancellation;
  `1452–1511` cancels then submits, and retries LIMIT errors as MARKET.
- **Problem/impact:** a broker-accepted order can be resent while invisible; a
  filled stop and stale app exit can both sell. The monitor reread at `870` is
  useful but occurs before the cancel/submit race and is not used on every exit path.
- **Solution:** durable intent/tag and attempt outcomes; UNKNOWN blocks blind
  resubmission; confirm cancellation or use a validated broker modification
  handoff, reconcile fills and submit only known residual quantity. A tag alone
  is not exchange idempotency. Retain emergency urgency throughout recovery.
- **Effort/dependencies:** L; F01, durable intent storage. **Live gate:** yes.
  **Delivery:** exit refactor phase 2.

### F03 — Hard-risk scheduling and broker priority are not authoritative end-to-end

- **Severity/priority:** Critical / P0. **Components:** engine loop, gateway,
  broker request priority, start/stop controls.
- **Evidence:** `_run_loop` (`trading_engine.py:332–370`) runs monitor, square-off,
  scan and journal work serially; scanner callbacks can wait for entry fills
  (`scanner.py:226–235`, engine `582–607`). Square-off calls `stop()` immediately
  (`344–348`). `stop()` disables the whole loop (`288–291`). Every new broker
  order, including protective/emergency, uses `Priority.ORDER`
  (`kite_client.py:154–158`), while open circuit rejects it
  (`request_policy.py:90–113`).
- **Problem/impact:** nominal five-second risk polling is not a deadline; slow
  analytics/scans delay loss/session handling. Square-off may leave working exits
  and residual positions unsupervised. A critical cancel can succeed while the
  replacement emergency order is blocked by the same circuit.
- **Solution:** separate bounded risk/position supervision from entry/research
  workers; propagate server-owned critical order role; maintain hard intents
  until flat/order-clean; make Stop Agent mean entry pause with explicit status.
- **Effort/dependencies:** L; F01–F02, session clock. **Live gate:** yes.
  **Delivery:** exit refactor phases 2–3.

### F04 — Unknown broker state and historical caches are treated as current truth

- **Severity/priority:** Critical / P0. **Components:** recovery, broker adapter,
  snapshots.
- **Evidence:** `_find_live_position_by_symbol` returns `{}` on exception
  (`trading_engine.py:887–896`); monitor then journals/removes tracking as flat
  (`870–880`). `_open_order_ids` returns empty on error (`75–85`), authorizing
  stop replacement. `kite_client.get_orders` merges all saved history into its
  return (`67–88`); absent old orders retain old OPEN statuses. Startup skips
  individual malformed records (`120–130`).
- **Problem/impact:** a network failure can remove a live position or duplicate
  protection; an old cached order can falsely establish current coverage.
- **Solution:** typed successful-empty versus unknown/stale snapshots, current
  order book separate from history, recovery state and broker reconciliation
  before mutation. Corrupt records become supervised unresolved exposure.
- **Effort/dependencies:** M; F01–F02. **Live gate:** yes.
  **Delivery:** exit refactor phases 1–2.

### F05 — Protective-stop failure paths can abandon exposure or over-flatten

- **Severity/priority:** Critical / P0. **Components:** entry, adoption, restart,
  stop confirmation.
- **Evidence:** `_confirm_protective_stop` recognizes OPEN/TRIGGER PENDING but
  not COMPLETE as a possible reducing fill (`1413–1436`). Entry emergency path
  returns without tracking/flat confirmation (`608–638`); restart path drops the
  trade after the request or its failure (`192–223`). Adoption places a stop
  without confirming it (`1003–1041`). `start()` sets running true after
  reconciliation may have called `stop()` (`257–284`).
  Ordinary monitoring synchronizes pending exit orders, not continuing protective
  coverage (`755–761`); ticker order updates only go to the renderer
  (`ticker.py:131–135`). A manually cancelled/rejected stop can remain recorded
  as protective without an active coverage check.
- **Problem/impact:** an immediately filled stop may lead to another stale full
  quantity exit; failed emergency requests can leave untracked exposure; startup
  can override the attempted protection-failure halt. Confirmation exceptions
  also escape to generic entry failure rather than establish a durable obligation.
- **Solution:** stop/fill facts feed the shared coordinator; retain every known
  fill and protection obligation; emergency exits close only reconciled residual;
  distinguish entry suspension from supervision. Confirm adopted protection.
  Consume protective-order events and verify coverage periodically with freshness
  limits, including quantity/type/side and triggered-but-unfilled status.
- **Effort/dependencies:** M–L; F01–F04. **Live gate:** yes.
  **Delivery:** exit refactor phases 2–3.

### F06 — Quantity, ownership and partial-fill lifecycle are incomplete

- **Severity/priority:** Critical / P0. **Components:** active trades, entry wait,
  exits, adoption, journal allocation.
- **Evidence:** active records at `trading_engine.py:688–705,1029–1041` omit
  quantity/product; maps are symbol-keyed. `_wait_for_entry_fill` waits for terminal
  status or timeout and ignores cancellation failure (`1268–1304`). Entry/stop
  quantity uses total same-side position, not allocated fill delta. COMPLETE exit
  deletes tracking (`1545–1566`); no residual ledger. Execution reconciliation can
  take all matched opposite fills when stored quantity is absent (`1639–1643`),
  and journal rows do not preserve the active record's order linkage.
  `square_off_all` and adoption
  inspect all nonzero net positions (`1256–1263,817–823`).
- **Problem/impact:** partially filled entries can be unprotected during polling,
  late fills can exceed protection, partial exits/external additions cannot be
  allocated reliably, and same-symbol products/other positions can be mixed.
- **Solution:** fill ledger, compound broker-position identity and explicit
  management ownership/epoch; protect actual partial fills; quantity-aware
  completion; keep no-pyramiding policy initially; explicit account emergency scope.
- **Effort/dependencies:** L; F01–F02 and journal IDs. **Live gate:** yes.
  **Delivery:** exit refactor phases 1–2/5.

### F07 — Portfolio exposure is not actual current portfolio risk

- **Severity/priority:** Critical / P0. **Components:** `risk_manager`, engine
  active records/pending orders.
- **Evidence:** `can_accept_position` values active trades as
  `quantity * entry_price` (`risk_manager.py:363–367`), but engine records omit
  quantity. Orders are summed without entry/reduction/protection roles
  (`369–378`), using mismatched fill/side fields. Actual broker positions/marks
  are not inputs. Zero-price orders are ignored. Open-position count refreshes
  only in monitor (`trading_engine.py:737–738`).
- **Problem/impact:** current gross/net/sector/correlation risk can be absent,
  stale or counted on the wrong side. Multiple entries in one scan can rely on
  an old position count. A fixed entry notional is not current exposure or
  aggregate stop-risk.
- **Solution:** authoritative marked positions plus conservative entry reservations;
  explicit order roles/residuals; separate gross/net notional and loss-to-stop
  risk; atomic admission capacity including pending positions. Validate mark quality.
- **Effort/dependencies:** M; F01/F06, coherent prices. **Live gate:** yes.
  **Delivery:** exit refactor phase 1; advanced portfolio optimization separate.

### F08 — Correlation-data failure is interpreted as diversification

- **Severity/priority:** High / P0 for portfolio-limit reliance. **Components:**
  correlation/sector concentration.
- **Evidence:** `_get_correlation` returns `0.0` for missing tokens/history,
  fewer than five joined rows, NaN and exceptions (`risk_manager.py:293–326`).
  Limit checks only aggregate `corr >= threshold` (`405–417`). Lookback data are
  not explicitly truncated to the named sample count. Sector map covers only
  part of the universe (`nifty_universe.py:110–165`).
- **Problem/impact:** missing information can enable additional concentrated
  positions. Negative raw correlation with opposite signed exposures is not
  necessarily diversifying. Unknown sectors are pooled, which is conservative
  in one sense but is not actual sector identification.
- **Solution:** correlation estimate + sample/freshness/UNKNOWN status; conservative
  fallback concentration/admission policy and signed exposure treatment; explicit
  unknown-sector policy. No exit requires a correlation API call.
- **Effort/dependencies:** M; F07/data service. **Live gate:** yes before claiming
  these limits protect the portfolio. **Delivery:** conservative fallback in
  phase 1; richer correlation/sector model immediately afterward.

### F09 — Entry admission and manual routes have inconsistent guarantees

- **Severity/priority:** High / P0. **Components:** RPC, gateway, sizing, confirm
  signals.
- **Evidence:** `main.py:122–125` routes manual orders directly to gateway and
  marks all as entries. Portfolio sizing/protection occur only in engine.
  `trading_engine.py:534–540` accepts a positive supplied quantity instead of
  margin-aware sizing; `main.py:280–282` accepts a renderer-supplied signal dict.
  `RiskManager.can_trade` blocks FAILED, not PENDING/STALE reconciliation
  (`113–152`). Gateway checks count/cooldown before pending lock (`32–81`).
- **Problem/impact:** manual/proposal/confirmation paths do not all enforce the
  same risk budget, age, price geometry, ownership and protection contract.
  Pending admissions/counts can race or remain stale; old UI signals can execute.
- **Solution:** server-owned entry intents/signal ID + expiry, finite price/side/
  quantity validation and current quote/risk checks; advisory quantity always
  capped by margin/risk; central admission reservation; separate reduction command.
- **Effort/dependencies:** M; F01/F07, typed RPC. **Live gate:** yes for enabled
  routes. **Delivery:** required admission foundation in phase 1; full manual
  ticket workflow a separate immediate follow-up.

### F10 — P&L and fees have incompatible definitions and placeholder losses

- **Severity/priority:** Critical / P0. **Components:** risk, journal, fills,
  costs, analytics/dashboard.
- **Evidence:** risk reconcile uses per-fill cost calculator (`54–111`) while
  fast updates use an independent turnover approximation (`219–247`), both hit
  F01. `journal.close_trade` charges actual/zero supplied price (`287–385`), while
  `update_trade_exit` without costs uses gross as net (`429–435`). Unreconciled
  closes pass zero (`trading_engine.py:1731–1740,1786`). Periodic repair checks
  only OPEN or explicitly UNRECONCILED, not ordinary mispriced CLOSED trades
  (`1765–1803`). Dashboard counts net-position rows as trades (`main.py:261–275`).
- **Problem/impact:** daily loss gates, UI, statistics and calibration disagree.
  Unknown fills create fabricated large losses/profits. Per-fill brokerage caps
  differ from per-order charges; slippage benchmarks are incompletely recorded.
- **Solution:** fill-derived accounting and one versioned fee service, aggregate
  fees at correct order scope; unknown fill price/P&L remains null/pending;
  explicit gross/net/liquidation estimates; idempotent correction provenance.
- **Effort/dependencies:** M–L; F01/F06. **Live gate:** yes; also research gate.
  **Delivery:** phase 1 foundations, phases 2/5/9 final allocation/reporting.

### F11 — Session boundaries and daily state use inconsistent clocks

- **Severity/priority:** High / P0. **Components:** risk, engine, journal/config,
  UI session status.
- **Evidence:** `can_trade` uses IST (`risk_manager.py:120–139`); square-off uses
  naive local now and only acts up to 15:30 (`196–217`). Daily rollover resets
  only in `reconcile_state` (`58–64`), called at engine start or dashboard pending
  state, not in each monitor update. Journal count/cooldown and screener schedules
  use local time. Backend admission has no holiday/weekend calendar.
- **Problem/impact:** host timezone changes decisions, a long-lived session can
  retain yesterday's kill/P&L state, and a late recovery can miss forced close.
- **Solution:** injected exchange clock/calendar, explicit session transitions,
  forced-flat obligations persisting after deadline, and broker-validated rollover;
  store aware timestamps and separate display timezone.
- **Effort/dependencies:** M; F03 and coherent risk snapshot. **Live gate:** yes.
  **Delivery:** exit refactor phases 3–4.

## High-value trading intelligence improvements

### F12 — Context-free normal exits confuse missing triggers with failed thesis

- **Severity/priority:** High / P1, primary refactor. **Components:** engine,
  scanner, unused playbook invalidation.
- **Evidence:** `trading_engine.py:1134–1148` exits on opposing count/no support;
  scanner returns zero counts on empty candles (`244–281`). One-candle rejection
  uses `df.iloc[-2]` (`898–967`), with no entry-time restriction and unused LTP.
  Early continue branches skip last-reevaluation/event updates (`1134–1185`).
  `playbooks/*.evaluate_invalidation` has no production caller.
- **Problem/impact:** a good crossover trade naturally lacks a new crossover;
  normal retracements, pre-entry candles or data failure can close it. Exit
  frequency can change when strategies are toggled after entry.
- **Solution:** immutable thesis + state evidence, completed-bar identity,
  weakening/invalidation split and confirmation/hysteresis in the shared policy.
- **Effort/dependencies:** L; F01–F11, F20/F22. **Live gate:** validate before
  deploying candidate exits. **Delivery:** core phases 4–7.

### F13 — Profit/time management lacks R, excursions and a stop ratchet

- **Severity/priority:** High / P1. **Components:** management state, stop update.
- **Evidence:** time alone calls breakeven (`trading_engine.py:1150–1158`);
  `_tighten_to_breakeven` assigns local stop first, without side-aware improvement
  or current-price checks (`1189–1243`). `trailing_sl` is only stored/read to
  suppress resistance (`704,850–854`). Journal excursion columns exist but live
  open/close calls do not populate them.
- **Problem/impact:** a profitable stop could be loosened back to entry, a stop
  could move into/through current price, failed broker modification creates local
  divergence, and winners cannot be managed by structure/progress/giveback.
- **Solution:** immutable initial R, causal extrema and management phase, monotone
  confirmed structural protection; time reviews with failed-progress evidence.
- **Effort/dependencies:** M–L; F02/F06/F12. **Live gate:** yes for new profit
  management; fix stop loosening before existing use. **Delivery:** phases 2/5–7.

### F14 — Entry evidence/playbooks/regime are useful but semantically incomplete

- **Severity/priority:** High / P1. **Components:** strategies, aggregates,
  playbooks, regime classifier/config.
- **Evidence:** regime is stateless ADX/ATR with breakout priority (`regime_classifier.py:60–76`);
  no HTF path. Trend playbook sums trend event scores and permits a pure trigger
  without pullback (`trend_pullback.py:23–61`); directional features are not gated.
  Mean reversion requires two aggregated MR signals (`mean_reversion.py:44–50`),
  effectively oscillator evidence plus VWAP in the present mapping. Oscillator
  aggregation is capped, but trend signals remain separate. Family config getter
  has no production consumer. Aggregate/playbook dictionaries lose `riskReward`
  or timestamp metadata (`oscillator_evidence.py:67–81`, playbook returns).
- **Problem/impact:** names/counts can overstate independent confirmation and
  setup quality; configurable family weights are inert; a changing ADX label is
  not structural regime change. Breakout evidence also uses rolling VWAP while
  other components use session VWAP (`breakout_evidence.py:93–101`).
- **Solution:** preserve aggregation provenance, add explicit setup variants and
  dependency groups, common session/context semantics; snapshot continuous
  directional features. Evaluate broader entry/playbook scoring changes separately.
- **Effort/dependencies:** M; F20/F31. **Live gate:** new exits must not rely on
  misleading names/counts. **Delivery:** context/thesis subset phases 4–6;
  entry redesign and family-setting behavior immediate follow-up.

## Research validity issues

### F15 — “Calibrated probability” is an unversioned historical frequency

- **Severity/priority:** High / P0-research. **Components:** calibration,
  entry gating, analytics labels.
- **Evidence:** `calibration.py:18–66` queries all CLOSED strategy/score buckets,
  minimum 10, gross +0.9R successes; no as-of cutoff, temporal split, model version,
  uncertainty or invalid-record denominator exclusion. `journal.py:205–226`
  explicitly writes `signal_score` into `confidence`: **current representation
  is read consistently despite the legacy name**. Engine allows absent frequency
  or >=60% (`463–466`). UI calls buckets predicted percentages.
- **Problem/impact:** OOS claim in docstring is unsupported; frequencies depend on
  historical exit policy, invalid/legacy records and selection. They cannot be
  interpreted as exit-failure probabilities. Live past outcomes are not inherently
  look-ahead, but no mechanism makes historical reuse time-safe.
- **Solution:** label descriptive entry-outcome statistics honestly; clean data
  and freeze entry context for exit comparisons. A future calibrated model needs
  time-separated versioned artifacts/labels and uncertainty; do not use this for exits.
- **Effort/dependencies:** S to remove misuse/label; L for valid model, F10/F22.
  **Live gate:** before relying on probability-based decisions. **Delivery:**
  exit engine isolation/labels now; statistical calibration separate project.

### F16 — Backtests run a different trading system from production

- **Severity/priority:** High / P0-research. **Components:** backtest runner,
  scanner/playbooks, risk/exits.
- **Evidence:** `backtest_engine.py:9–12,87–114` calls one BaseStrategy and sizes
  10% cash; no scanner aggregation, regime/playbook gating, calibration, portfolio
  admission, thesis, live rejections/time rules. `main.py:311–362` exposes that
  raw-strategy runner as app backtesting.
- **Problem/impact:** even a correct historical P&L result does not validate the
  deployed entry/exit stack; all requested live exit behaviors are absent.
- **Solution:** same exit reducer/policy/coordinator behind broker/data/clock
  adapters; modest extraction of pure production entry evaluation. Keep the
  raw-strategy lab clearly labeled and separate from deployment evidence.
- **Effort/dependencies:** L; F01–F13/F20. **Live gate:** before promoting new
  policy using backtests. **Delivery:** exit refactor phase 8.

### F17 — Simulator assumptions distort fills, excursion and intraday exposure

- **Severity/priority:** High / P0-research. **Components:** simulated broker,
  backtest engine.
- **Evidence:** stop-first is explicit (`simulated_broker.py:200–210`), which is
  a useful conservative baseline. Stop price already applies 5bp then calls a
  method adding another 5bp (`203,208,215–217`, `43–50`). Stops/targets do not save
  exit reason; full candle extrema update before exit (`189–195`). Partial fills
  raise NotImplementedError (`97–102`); cash does not reserve buying power. No
  daily session flatten exists; final liquidation only works for symbols present
  at the global last timestamp (`backtest_engine.py:116–128`).
- **Problem/impact:** unmodeled overnight holdings, liquidity/capital constraints,
  ambiguous post-exit extrema and inconsistent slippage prevent trustworthy
  capture/drawdown/exit-reason studies. Errors are not all optimistic; double
  slippage is pessimistic but still invalidates comparisons.
- **Solution:** explicit versioned execution model, exactly-once slippage/fees,
  gap/stop-limit/latency/partial-fill events, common session/risk policy, ambiguity
  bounds and resolution-aware excursions.
- **Effort/dependencies:** L; F06/F10/F16. **Live gate:** research gate.
  **Delivery:** exit refactor phase 8.

### F18 — Walk-forward slicing is not train/select/test validation

- **Severity/priority:** High / P0-research. **Components:** walk-forward validator.
- **Evidence:** `walk_forward.py:40–88` instantiates the same default class each
  window; no training/selection/calibration step. It trades the warmup and filters
  results after execution (`59–75`); cash/open positions already changed. Inclusive
  windows and user-supplied step permit repeated periods/boundary trades.
- **Problem/impact:** results labeled OOS do not demonstrate parameter selection
  validity and can be contaminated by unscored positions/equity or duplicated data.
- **Solution:** honest fixed-policy stability mode or nested temporal selection,
  feature-only warmup, half-open disjoint test windows, purge/embargo for overlapping
  labels, frozen artifacts and untouched final holdout.
- **Effort/dependencies:** M; common runner and dataset metadata. **Live gate:**
  research gate. **Delivery:** exit refactor phase 10.

### F19 — What-if and performance metrics cannot judge exit quality reliably

- **Severity/priority:** High / P0-research. **Components:** analytics, metrics,
  journal replay UI.
- **Evidence:** `analytics.py:383–468` calculates gross held-to-EOD/wider-stop
  outcomes and compares actual net P&L; target search ignores prior stop sequence;
  it has no original decision/protection state. `metrics_evaluator.py:47–65`
  uses closed-trade equity and only days with exits for Sharpe. Analytics average R
  is gross (`analytics.py:58–71`) while backtest R is net (`metrics_evaluator.py:67–78`).
- **Problem/impact:** charts can label sensible protection as a poor exit by
  showing unreachable later targets, and hide open-equity risk. Neither premature
  nor delayed exit has a defensible current definition.
- **Solution:** risk-constrained hold-N replay with pinned protection/costs,
  price-only diagnostics labeled separately; full MTM risk series; unit-explicit
  gross/net R, MFE capture, giveback, continuation, avoided reversal and censoring.
- **Effort/dependencies:** M–L; F10/F16–F18/F22. **Live gate:** before relying on
  exit-quality claims. **Delivery:** exit refactor phases 8–10.

## Data quality, reproducibility and architecture

### F20 — Candle completion is partially implemented; freshness/integrity are not

- **Severity/priority:** High / P0 for causal-data reliance. **Components:**
  scanner, indicators, ticker, broker data.
- **Evidence:** normal scanner completion filters exist (`scanner.py:140–161,
  262–281`) and should be retained. `_fetch_candles` only casts prices, caches by
  token and uses timedelta `.seconds` (`80–116`); no sorted/duplicate/OHLCV/session/
  latest-age validation. No 15m as-of path exists. Strategies such as ADX/PSAR
  mutate input frames; raw cache can be reused when incomplete evaluation enabled.
  Ticker emits local receipt time and ignores disconnect state (`ticker.py:100–152`).
- **Problem/impact:** a successful stale response can be traded, corrections or
  duplicate bars can change signals, and raw penultimate-row exit logic disagrees
  with entry completion. No reproducible HTF context is available.
- **Solution:** shared immutable candle/context service with event availability,
  session calendar, integrity/freshness statuses and causal 15m resampling; unknown
  inputs remain unknown; preserve SessionVWAP with invalid/zero-volume handling.
- **Effort/dependencies:** M–L; clock and input contracts. **Live gate:** yes for
  candidate policy. **Delivery:** exit refactor phase 4.

### F21 — Screener ranks are outlier-sensitive and failure fallback bypasses filters

- **Severity/priority:** Medium / P2, with high-value entry reliability follow-up.
  **Components:** screener/universe/watchlist ordering.
- **Evidence:** `screener.py:106–126` uses unrestricted min-max scaling; volume is
  cumulative absolute volume, not time-of-day relative volume; liquidity is total
  buy+sell quantity, not spread/executable depth (`46–80`). Empty/error quote paths
  return unfiltered `universe[:limit]` (`26–27,155–157`), while all-filtered returns
  empty correctly (`101–104`). Universe set conversions lose stable order
  (`trading_engine.py:425,442`).
- **Problem/impact:** one extreme compresses other ranks; morning/afternoon and
  high-share-volume names are not comparable; a data outage can bypass tradability
  gates, and fallback/ties can vary by iteration order.
- **Solution:** explicit failed/stale screening result retaining last-known-valid
  universe only under freshness rules; stable tie order; percentile/winsorized
  causal normalization, value/spread/participation filters and prior-session
  time-of-day relative volume, each evaluated separately.
- **Effort/dependencies:** M; F20/data provenance. **Live gate:** repair bypass
  fallback before relying on liquidity filtering. **Delivery:** separate immediate
  follow-up; no screener alpha optimization inside exit refactor.

### F22 — Existing journal schema is richer than recorded decision evidence

- **Severity/priority:** High / P1. **Components:** journal, engine, analytics.
- **Evidence:** journal has regime/playbook/raw/feature/time/R/MAE/MFE/version
  columns (`journal.py:71–88,182–194`), but engine entry call only supplies older
  subset (`trading_engine.py:653–682`); normal exit/time branches skip reevaluation
  event logging. Base signals use wall-clock timestamps (`strategies/base.py:81`),
  playbook output omits them. Replay fetches today's historical data for a trade
  date (`analytics.py:307–381`), not retained as-seen inputs.
- **Problem/impact:** “why HOLD/exit here?” cannot be reproduced, and changed
  settings/data/history make old decisions look different. Rich UI fields remain null.
- **Solution:** exact entry snapshot + immutable thesis, versioned policy/data
  references, every eligible decision trace and intent/fill linkage; extend existing
  SQLite/events rather than replace them.
- **Effort/dependencies:** M–L; F20/F31. **Live gate:** before trusting candidate
  explainability/research. **Delivery:** phases 5/7/9.

### F23 — Latent AI proposal route manufactures quantitative strength

- **Severity/priority:** High / P1, latent feature. **Components:** AgentGateway,
  LLM analytics.
- **Evidence:** `agent_gateway.py:123–136` assigns `signal_score=100`, calls engine
  without quantitative entry evidence and omits exchange; proposal MARKET/LIMIT
  choice does not control engine's LIMIT entry. It does clamp suggested quantity
  through risk sizing (`113–121`), a useful boundary, but not available-margin-aware.
  Input coercion is incomplete for nonfinite/types (`73–111`). Call-site search
  found only tests invoking it; active LLM calls are journal post-mortems.
- **Problem/impact:** enabling this route could make unvalidated advice look like
  a strongest-possible quantitative signal; tests stub execution and miss contract
  failures. There is no evidence that this currently places live LLM trades.
- **Solution:** retain LLM as post-trade/research interpreter; if proposals are
  later enabled, require server-owned strategy/thesis validation, strict schema,
  fresh prices and ordinary admission; advisory text has no synthetic score.
- **Effort/dependencies:** M; F01/F09/F22. **Live gate:** before enabling proposals.
  **Delivery:** separate follow-up; explicitly exclude from critical exit path.

### F24 — Bridge restart and synchronous RPC work do not restore operational readiness

- **Severity/priority:** High / P0 for recovery; P1 for job UX. **Components:**
  Python bridge/main, auth/bootstrap, journal UI.
- **Evidence:** bridge restarts process but leaves pending requests unresolved on
  unexpected exit (`python-bridge.ts:66–88,112–125`), has no deadlines or readiness
  handshake and does not rehydrate credentials/supervision. Python dispatch is
  serial (`main.py:371–395`), with synchronous backtest/LLM methods. Expanding a
  trade fetches replay, what-if and LLM automatically (`Journal.tsx:78–101`).
- **Problem/impact:** a restarted child can be advertised running while positions
  are not resumed; UI commands hang/queue behind long analysis. It does not imply
  every LLM call blocks the separate engine thread, but operator control is delayed.
- **Solution:** process-generation/request failure handling, deadlines, authenticated
  readiness/reconciliation handshake; bounded asynchronous research jobs with
  separate control priority and explicit post-mortem request/cache.
- **Effort/dependencies:** M; F03/F04/F22. **Live gate:** recovery subset yes.
  **Delivery:** phase 3 recovery and nonblocking control dispatch; richer research
  job management/UI immediate follow-up.

### F25 — Persistence is atomic in places but not transactionally coherent

- **Severity/priority:** High / P0 for lifecycle intents. **Components:** config,
  journal/order cache, engine snapshots.
- **Evidence:** `_atomic_write_json` fsync+replace is good (`config.py:345–368`),
  but `config.save`, app-order read/modify/write and historical cache writes are
  ordinary writes (`187–189,301–323`). Snapshot is copied then written outside
  lock (`trading_engine.py:66–73`), allowing an older complete snapshot to win.
  Journal uses autocommit (`journal.py:25–30`) with multi-statement `with conn`
  sections; order intents are not durable before submission.
- **Problem/impact:** complete JSON is not ordered state; concurrent writers can
  lose IDs/settings or persist stale exposure. Crash can separate entry/exit
  records from events and broker obligations.
- **Solution:** explicit SQLite transaction for state/trace/intent, unique fill/intent
  identity, versioned checkpoint/event sequence; retain atomic JSON for compatible
  checkpoints/migration; validate and atomically save settings.
- **Effort/dependencies:** M; lifecycle contracts. **Live gate:** yes for reliable
  restart. **Delivery:** phases 2/5; unrelated order-archive retention follow-up.

## Security / credential / operational issues

### F26 — DEV trading is not isolated from LIVE persistent state

- **Severity/priority:** Critical / P0 when switching modes. **Components:** dev
  flag, mock broker, config/journal/calibrator, Electron storage.
- **Evidence:** DEV swaps only client (`kite_client.py:263–272`); config/journal/
  calibration retain `~/.kite-agentic-trading` paths. `dev_mode.py` has no packaged
  app distinction despite documentation. Mock has no `get_trades`, ignores history
  range/interval (`139–142`), fills limit immediately and only rests SL, not SL-M
  (`229–242`); live price, quotes and ticker have different synthetic sources.
- **Problem/impact:** synthetic trades/risk/order IDs can contaminate real restart
  state and calibration. UI mock cannot validate exit quality or real execution;
  dev risk reconciliation can fail on missing methods.
- **Solution:** namespace/isolate mode/account persistence and broker capabilities;
  visible effective mode, explicit supported mock surface; keep mock for UI and
  build a separate realistic paper adapter for research.
- **Effort/dependencies:** M; F01/storage paths. **Live gate:** yes before combined
  dev/live use. **Delivery:** urgent separate isolation fix; phase 8 paper adapter.

### F27 — Operator controls can promise an action they do not perform

- **Severity/priority:** Critical / P0 for operator reliance. **Components:**
  dashboard, orders, agent mode, hooks and IPC.
- **Evidence:** position Exit only console.logs (`Dashboard.tsx:46–48`); Cancel
  has no handler (`Orders.tsx:86`), IPC cancel sends `orderId` instead of
  `order_id` (`ipc-handlers.ts:46–48`). Mode radio saves config/UI but active
  engine mode is only set by start (`AgentControl.tsx:33–36`, engine `261`).
  `useKiteAPI` is mounted at root and several children; each registers handlers
  and cleanup removes all listeners (`useKiteAPI.ts:58–74,157–162`).
- **Problem/impact:** operator cannot reliably close/cancel through visible
  controls, UI can display confirm while engine remains auto, and navigation can
  duplicate/remove subscriptions. “Will Auto-Enter” omits risk/market/recovery gates.
- **Solution:** backend-acknowledged typed pause/close/cancel/mode commands using
  shared lifecycle coordinator; singleton root subscriptions with exact cleanup;
  show effective mode/risk reasons and command outcomes before dismissing a signal.
- **Effort/dependencies:** M; F02/F03/F09. **Live gate:** yes.
  **Delivery:** core risk/close controls phase 3, explanation/UI phase 9.

### F28 — UX/contracts obscure actual state and some screens remain placeholders

- **Severity/priority:** Medium / P2, P1 for lifecycle explanations. **Components:**
  shared types, journal, signal/dashboard/chart/watchlist UI.
- **Evidence:** shared RiskConfig/StrategyConfig/AppSettings diverge from backend
  (`types.ts:263–355`); `JournalTrade.signal_score` does not alias DB `confidence`.
  `PnLDisplay.tsx:16,21` shows absolute negative values without a minus sign.
  Signal scores are percentages and calibration buckets “Predicted”
  (`AgentControl.tsx:202,226`, `Journal.tsx:470`). Chart uses static 2023 candles
  (`Chart.tsx:56–83`); watchlist actions lack handlers; backtest UI lists seven
  strategies. Several IPC methods have no backend branch (`ipc-handlers.ts`, `main.py`).
- **Problem/impact:** operators may mistake heuristic strength, placeholder data,
  stale state or gross metrics for decision truth; TypeScript passes because
  boundary payloads use any. No active thesis/confirmed-stop/freshness view exists.
- **Solution:** generated or shared validated DTO contracts; correct signed P&L
  and score labels; lifecycle panels and backend rejection reasons. Clearly label
  or complete placeholder functionality separately.
- **Effort/dependencies:** M; F01/F22/F27. **Live gate:** lifecycle/P&L presentation
  before candidate reliance. **Delivery:** relevant subset phase 9; chart/watchlist
  feature completion separate P2 work.

### F29 — Native secret storage is undermined by renderer persistence and blank saves

- **Severity/priority:** Critical / P0. **Components:** auth manager, Zustand,
  settings IPC/config.
- **Evidence:** login success returns apiKey/apiSecret/accessToken to renderer
  (`auth-manager.ts:91–99`); LoginModal stores the response (`23–24`); Zustand
  persists full auth (`trading-store.ts:103–108`). Settings getter returns masked
  empty credential fields (`config.py:218–226`), and a full settings save passes
  them to `secureStorage.updateCredentials` without empty-value filtering
  (`Settings.tsx:123–129`, `ipc-handlers.ts:154–158`). Defaults can recreate the
  credentials keys on load (`config.py:112,162–167`).
- **Problem/impact:** successful auth can persist real secrets in renderer
  localStorage despite encrypted native storage; saving ordinary settings can
  overwrite saved credentials with blanks on that path. Logout/token lifecycle
  and recovery are consequently unreliable.
- **Solution:** renderer receives identity/status only; never persist secrets in
  auth/settings. Separate credential commands from masked settings, with explicit
  replacement/clear semantics and migration that does not expose old values.
  Preserve native safeStorage and refusal to write without encryption.
- **Effort/dependencies:** M; dedicated security tests, no actual secrets needed.
  **Live gate:** yes. **Delivery:** urgent separate fix, prerequisite to promotion.

### F30 — IPC/provider capability boundaries are broader than documentation claims

- **Severity/priority:** High / P1 security. **Components:** preload/main window,
  IPC/auth callback, LLM settings/client.
- **Evidence:** preload exposes generic invoke/on and raw event listener callbacks
  (`preload.ts:8–13`); main disables sandbox (`index.ts:26–30`); handlers do not
  validate sender origin. Auth accepts any redirect carrying request_token
  (`auth-manager.ts:69–74`). LLM base URLs except selected special cases are
  accepted from settings/RPC and used with credentials (`main.py:187–206`,
  `llm_client.py:101–154`), despite README saying arbitrary endpoints are not accepted.
- **Problem/impact:** a compromised renderer/invalid configuration has excessive
  trading and credential-endpoint capability. This is a source boundary finding,
  not a demonstrated remote exploit or evidence that secrets were logged.
- **Solution:** typed allowlisted commands/events and sender validation, strip IPC
  event objects, explicit trusted navigation/redirect policy, sandbox/CSP review,
  enforce provider URL/scheme presets on backend before attaching credentials.
- **Effort/dependencies:** M; F29/RPC contracts. **Live gate:** before trusting
  these privileged/untrusted-input boundaries. **Delivery:** separate security fix;
  exit commands use the corrected boundary, no LLM authority added.

## Reliability, performance and maintainability

### F31 — Trading settings are mutable, weakly validated and unversioned

- **Severity/priority:** High / P1 (validation is a live gate). **Components:**
  config, strategies, risk and settings UI.
- **Evidence:** `config.save_settings` merges arbitrary values (`228–262`);
  getters expose mutable dictionaries. UI accepts numeric/time inputs with little
  validation (`Settings.tsx:74–86,263–339`). Strategy stops/targets read globals
  (`strategies/base.py:24–52`) and many strategies override percentages internally.
  `evaluateOnIncompleteCandle` affects both entry/position evaluation. No config
  hash/version is pinned to an active trade.
- **Problem/impact:** invalid values and mid-trade strategy toggles change behavior
  without reproducible intent; defaults/hidden overrides complicate research.
- **Solution:** validated effective config, immutable per-position policy snapshot,
  explicit allowed runtime changes and migration events; distinguish operational
  risk controls from research parameters. Keep small parameter budget.
- **Effort/dependencies:** M; contracts/provenance. **Live gate:** validate active
  risk settings and candidate policy before reliance. **Delivery:** phases 3–6;
  broad entry-setting rationalization separate.

### F32 — Repeated work and unbounded history will degrade a long-running session

- **Severity/priority:** Medium / P2. **Components:** broker cache, analytics,
  scanner, localStorage, RPC.
- **Evidence:** each `get_orders` loads/rewrites/sorts complete JSON history
  (`kite_client.py:67–88`); monitor calls `_persist_trades` even without changes
  (`trading_engine.py:884–885`); strategies recalculate overlapping indicators;
  journal/analytics read all rows; analytics connections lack explicit close
  management; `trading-store.ts:95,104–108` persists an unbounded activity log on
  state updates; Journal expansion fetches the same day data again for what-if.
- **Problem/impact:** growing filesystem/serialization/CPU load and duplicate
  requests can increase latency and obscure operational health. No benchmark yet
  quantifies the scale threshold; the blocking risk-loop issue is separately F03.
- **Solution:** instrument latency first; cache immutable per-bar features, paginate/
  index journal queries, deduplicate market snapshots, bound display logs, batch
  versioned checkpoints and separate current orders from retained history.
- **Effort/dependencies:** M; F20/F22/F25. **Live gate:** critical scheduling fix
  yes under F03; broader performance work no. **Delivery:** focused caches in
  refactor, measured reliability follow-up for the rest.

### F33 — Passing tests leave cross-boundary safety and research behavior untested

- **Severity/priority:** High / P1. **Components:** tests/CI/contracts.
- **Evidence:** capital-safety fixtures mix camel and snake keys
  (`test_trading_engine_capital_safety.py:24–31,327,407`); external-close fixtures
  use `average_price` (`test_journal_external_close.py:108–157`). Tests assert
  zero-price UNRECONCILED close and startup proceeds despite reconcile failure
  (`test_journal_external_close.py:98–106`, `test_persistence_reconcile.py:446–460`).
  Backtest suite has three broad tests, risk tests only cover sizing. No dedicated
  calibration/walk-forward/parity/MTF test module exists. CI has no Electron
  lifecycle/control integration coverage.
- **Problem/impact:** mocked components can each pass while the real adapter
  contract fails; unsafe expectations become regressions to preserve accidentally.
- **Solution:** SDK-shaped end-to-end fixtures, failure/partial-fill/restart state
  tests, causal prefix tests, long/short scenario and mode parity tests. Keep
  synthetic strategy tests. Verify supported Python/OS/package runtimes explicitly;
  baseline here was Python 3.14/macOS, not every declared supported version.
- **Effort/dependencies:** M–L, incremental alongside each phase. **Live gate:**
  P0 behavior must be tested before reliance. **Delivery:** all phases, CI extension;
  dependency install issue is local environment, not a code defect claim.

## Future opportunities

### F34 — Missing market/portfolio context is an opportunity, not a reason for more indicators

- **Severity/priority:** Medium / P2; exploratory variants P3. **Components:**
  context, selection, research, optional AI interpretation.
- **Evidence:** present regime uses only the instrument's 5m ADX/EMA/ATR/VWAP
  (`regime_classifier.py:24–89`); screener uses quote movement/volume/liquidity
  proxies; no benchmark/sector relative strength, breadth, event calendar,
  historical time-of-day volume profile or retained uncertainty model is consumed.
  Hardcoded strategy thresholds have no attached research provenance artifacts.
- **Problem/impact:** the system may miss broad market alignment, event-driven
  discontinuities or unusual liquidity, but code alone cannot establish the P&L
  benefit of adding those inputs or prove existing thresholds were overfit.
- **Solution:** prioritize causal structure/HTF/trade-state first; afterward test
  a small benchmark/sector context and time-of-day participation hypothesis using
  incremental ablations, point-in-time data and OOS evaluation. LLMs can summarize
  anomalies/replays or propose experiments, with deterministic deployment bounds.
- **Effort/dependencies:** M–L; F16–F22, suitable data licenses/provenance.
  **Live gate:** no for optional signals; absence/unknown must be honest.
  **Delivery:** separate P2/P3 research, not current exit scope.

## Explicit verification of the 14 requested high-risk categories

| # | Category | Verified conclusion / finding |
|---|---|---|
| 1 | OOS/time-separated probability calibration | No split/as-of/artifact mechanism; descriptive same-journal frequency. F15 |
| 2 | Current versus legacy signal representation | Current signal_score intentionally writes confidence and calibrator reads it. Storage alias works; frontend alias and mixed version semantics do not. F15/F28 |
| 3 | Live/backtest same logic | No: raw strategy/10% sizing/simple stops versus production stack. F16 |
| 4 | Meaningful walk-forward train/select/test | No selection/training; warmup trades affect test state; overlaps possible. F18 |
| 5 | Intrabar stop/target ambiguity | Simulator explicitly stop-first; live follows broker timing; what-if ignores path ordering. Conservative fallback exists, common contract absent. F17/F19 |
| 6 | Realistic/versioned slippage/costs | Shared configurable fee calculator is useful; effective-date version absent, fixed simulation slippage/double stop application and differing risk estimate. F10/F17 |
| 7 | Coherent P&L definitions | No: mismatched fields, placeholder fills, gross/net R and daily estimates differ. F01/F10/F19 |
| 8 | Conservative missing correlation | No: failure/insufficient data returns zero. F08 |
| 9 | Current market/risk exposure | No: missing stored quantity, entry notional, unclassified orders and stale counts. F06/F07 |
| 10 | Robust screener normalization | Min-max is outlier-dependent; no distribution/time-of-day normalization or outlier tests. F21 |
| 11 | AI proposals cannot fabricate quantitative strength | Latent gateway creates score100 despite no quantitative evidence; risk clamp exists; no active caller found. F23 |
| 12 | Restart/partial/failed broker consistency | Recovery infrastructure exists, but unknown->flat, unconfirmed stop handoff, missing residuals and child readiness gaps remain. F02–F06/F24/F25 |
| 13 | Overfit strategies/playbooks/thresholds | Many hand-set thresholds and bonuses, but no inspected provenance/data establishes actual historical overfit. Validation capability currently inadequate; record uncertainty and test perturbations. F14–F18/F31/F34 |
| 14 | Ignored market context | No HTF decision path/explicit structural thesis or broad benchmark/sector/breadth/time-of-day context. Prioritize first two in exits; validate others separately. F12/F14/F20/F34 |

## Components explicitly worth preserving

| Component/pattern | Why preserve | Targeted correction boundary |
|---|---|---|
| `ExecutionGateway` facade | A single broker mutation boundary and risk-reducing bypass already exist | Server-owned roles, durable outcomes and critical priority; not a replacement gateway |
| `BrokerGateway` token bucket/circuit state | Central throttling, priority waiting and half-open handling are useful | Correct critical callers and ambiguous order retry semantics |
| RiskManager's daily-loss latch and sizing | Authoritative loss concept, persisted daily state, stop-distance/margin sizing | Canonical accounting, actual exposure, session lifecycle and admission reservations |
| Atomic JSON fsync/replace | Protects against torn snapshots, covered by persistence tests | Add version ordering/transactional lifecycle source; retain migration/checkpoint utility |
| SQLite WAL trade/event journal | Appropriate local persistence and event history foundation | Explicit transactions, required fields, intent/fill identity, indexed replay inputs |
| SessionVWAP | Correct per-session reset and candle approximation documented/tested | Common use and quality checks; do not replace with rolling VWAP |
| Oscillator/breakout aggregation | Already limits redundant contributions and retains raw evidence | Extend dependency semantics; do not restore raw indicator voting |
| Playbook abstraction and 17 signal functions | Useful setup/entry infrastructure with offline fixtures | Add truthful setup/thesis metadata; avoid rewriting entry alpha during exit attribution |
| Next-bar entry fills and stop-first baseline | Valuable existing anti-look-ahead/conservative assumptions | Fix broader execution gaps and expose ambiguity; do not regress to ideal same-close fills |
| Native safeStorage refusal to write unencrypted | Good secret-at-rest boundary | Remove renderer persistence/blank overwrite, validate migration format; keep native encryption |
| Shared stdout lock, single-instance app lock | Appropriate local coordination foundations | Typed event payloads, lifecycle serialization and readiness, not a transport rewrite |
| Existing tests/CI | Useful repeatable offline and compile checks | Add real contract/failure/parity coverage; do not discard working strategy tests |

## Recommended engineering roadmap

### P0 — Must fix before relying on the system

**Live/risk foundation:** F01–F11, recovery portion of F24, lifecycle portion of
F25, data integrity/freshness in F20, effective operator reductions/mode in F27,
and validated risk configuration in F31. These fit the early exit-refactor phases
because otherwise a correct decision can still become an unsafe order.

**Urgent separate operational fixes:** F29 credential containment, F26 DEV/LIVE
storage isolation, and F30 privileged endpoint/IPC boundaries. Complete before
live promotion; keep their implementation as focused security/operations work.
Repair F09 manual entry contracts/protection before making that route available.

**Research reliance:** F15–F19 and fill/accounting quality in F10. The shared
runner/metrics/walk-forward portions belong in this refactor; a new probability
model does not. Until then, historical results cannot justify deployment.

### P1 — High-value improvements

- Core thesis/weakening/confirmation/profit lifecycle: F12–F14, F22, F31.
- Contract/scenario/replay tests and real operator explanations: F27–F28/F33.
- Immediately afterward: strengthen correlation/sector metadata beyond fallback
  (F08), complete consistent manual admission workflow (F09), remove screener
  failure bypass (F21), and asynchronous research/LLM jobs (F24).
- Correct entry metadata/family configuration and conflicting VWAP semantics
  (F14) in separate measured entry changes; prevent score100 misuse before ever
  enabling latent AI proposal routing (F23).

### P2 — Valuable but non-critical

- Robust screener normalization/participation study (F21), broader typed UI and
  placeholder chart/watchlist completion (F28).
- Measured caching, archive/query pagination and log retention (F32).
- Point-in-time market/sector/relative-volume context, reliable data lifecycle
  and packaged-runtime coverage beyond the minimal refactor (F20/F33/F34).

### P3 — Future research / experimentation

- Valid exit-conditioned statistical models only after trace/label/OOS foundations.
- Portfolio-aware adaptive management or partial-profit policies, with independent
  attribution and robust fill accounting already established.
- LLM-assisted replay interpretation/anomaly clustering/research suggestions;
  no LLM order authority and no online parameter self-modification.

### Scope decision

1. **Current exit refactor:** correctness prerequisites; causal 5m/15m context;
   immutable thesis/state; one deterministic policy; execution-safe integration;
   shared simulation/replay; traces, exit metrics, operator lifecycle visibility
   and robustness gates. See ten phases in IMPLEMENTATION_PLAN.
2. **Immediately afterward (or urgent separate release prerequisite):** credential/
   mode isolation fixes, full manual-order validation, richer concentration data,
   screener fallback repair and async research jobs.
3. **Remain separate:** entry-alpha changes, screener ranking optimization, new
   probability models, extra context signals, portfolio optimization, automated
   scale-outs/pyramiding, broad chart/watchlist features and LLM trading proposals.
4. **Explicitly retain:** components in the preservation table, their existing
   useful tests and the Electron/Python/local-SQLite architecture.

## Assumptions and validation questions

- The intended first domain is liquid NSE equity intraday MIS. Other products,
  exchanges, lot/multiplier semantics and operator-owned holdings need explicit
  ownership/risk rules rather than implicit adoption.
- Kite's supported stop types, stop modifications, tag echo, transient statuses,
  order-history freshness and circuit/price-band behavior need sandbox/paper or
  carefully controlled execution-contract verification. No atomic reduce-only
  guarantee is assumed.
- Observed availability/latency/slippage/fee rounding must be measured; current
  hardcoded buffers/rates are not empirical proof of realism.
- Existing trade history may lack reliable fills, config versions or entry
  structure. It can support forensic charts with caveats, not exact old decision
  reconstruction or unbiased exit-conditioned labels by default.
- Two-bar confirmation, structure buffers and review horizons are candidate
  research choices. Test whether reduced panic exits worsen failure latency,
  tail losses or giveback across symbols/regimes and OOS periods.
- Source review does not prove any threshold was historically fitted or any
  optional context signal will improve returns. Those remain experiments, not
  justifications for expanding this refactor.
