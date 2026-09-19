# Deterministic validation plan

## 1. Baseline and purpose

At the audited commit, `uv run --locked pytest` passed **340 tests**. Ruff lint
and format checks passed; frontend lint (four warnings), typecheck and build
passed. The frontend settings test command was blocked by a missing local `tsx`
executable; the same test passed with
`node --experimental-strip-types --test tests/strategy-settings.test.ts`.
None of those results proves live order safety or research parity.

The present suite is strongest in synthetic strategy triggers, aggregation,
basic stop placement, duplicate in-memory calls, JSON persistence and selected
reconciliation cases. Important test doubles use `average_price` even though
the real wrapper returns `averagePrice`; some tests intentionally encode unsafe
behavior such as closing UNRECONCILED trades at zero. Preserve the regression
intent while correcting those expected contracts.

Use pure fixtures and scripted brokers. Tests must be offline, use temporary
HOME/data namespaces, never instantiate an authenticated broker, and never read
operator credentials. All new clock-based tests use injected time rather than
sleeps or the actual session date. Give random scenarios fixed recorded seeds.

## 2. Test layers and suggested files

| Layer | Suggested files | Responsibility |
|---|---|---|
| Broker/RPC contracts | `backend/tests/test_broker_contracts.py`, `test_rpc_contracts.py`, existing main/gateway tests | SDK-shaped payload -> canonical domain -> frontend DTO; typed commands |
| Risk/accounting | Extend `test_risk_manager.py`, `test_trading_costs.py`, `test_journal*.py`; add `test_accounting_reconciliation.py` | Actual quantity/marks/pending risk, fee allocation and one P&L definition |
| Execution/recovery | `test_order_lifecycle.py`, existing capital-safety/persistence/request-policy tests | Unknown outcomes, residual fills, protection handoff, restart and critical priority |
| Scheduling/session | `test_risk_supervisor.py`, `test_session_clock.py` | Entry pause, blocking scan, deadlines, rollover and recovery |
| Market context | `test_market_context.py`, extend `test_indicators.py`, `test_regime_classifier.py` | Causality, 5m/15m alignment, data-quality semantics and structure known-at |
| Pure exit domain | `tests/exit_management/test_thesis.py`, `test_evidence.py`, `test_engine.py`, `test_state_machine.py` under `backend/` | Explicit thesis, family dependence, counters, state/action precedence |
| Stateful scenarios | `backend/tests/exit_management/test_scenarios.py` plus immutable OHLCV/event fixtures | Trader-like lifecycle behavior under known synthetic paths |
| Persistence/replay | `backend/tests/test_exit_replay.py`, `test_exit_persistence.py` | Crash boundaries, trace identity, policy pinning and legacy migration |
| Execution simulation/parity | Extend `test_backtester.py`; add `test_exit_parity.py`, `test_simulation_execution.py`, `test_walk_forward.py` | Shared decisions, explicit fills, no leakage and correct fold accounting |
| Quality metrics | `backend/tests/test_exit_quality.py` | R/MFE/MAE/giveback, counterfactual constraints and censoring |
| Frontend lifecycle | `tests/lifecycle-contracts.test.ts`, component/integration tests where supported | Working close/cancel, mode acknowledgement, evidence and pending-stop presentation |

No broad test-framework rewrite is required. Start with pytest and existing
TypeScript test infrastructure; add UI integration tooling only for behavior
that cannot be verified meaningfully through contract tests.

## 3. P0 safety invariants

### 3.1 Canonical broker contracts

Fixtures include raw Kite-shaped positions, orders and fills, plus the actual
adapter output. Test successful, empty-successful, malformed, timed-out and stale
snapshots separately. Assert:

- Average fill price, signed side, filled/remaining quantity and turnover survive
  normalization. No missing price defaults to zero for financial accounting.
- A real-shaped manually open position receives correctly sized protection when
  explicitly adopted; the response contract cannot suppress adoption silently.
- Completed exit fills produce the verified fill VWAP and correct net P&L.
- Order history from a prior session cannot count as a currently working stop.
- Product/exchange/account identity prevents same-symbol collisions.
- Incoming manual order fields are mapped and validated; the caller cannot
  choose its own risk-bypass role. Manual close is a reduction, not an entry.

### 3.2 Execution and broker failure matrix

| Scenario | Required invariant |
|---|---|
| Placement times out, order accepted but invisible briefly | One intent/tag; UNKNOWN and reconciliation; no blind resubmission |
| Placement fails definitively | Retry/replacement only after no-live-order outcome is proven |
| Cancel times out while stop fills | Consume stop fill, recompute residual; no opposite position |
| Stop reports COMPLETE during initial confirmation | Reconcile actual exposure before any emergency reduction |
| Partial entry followed by cancellation | Protect partial immediately; quantity/R based on allocated fills; no adoption duplicate |
| Entry cancellation fails, remainder fills later | Retain pending-entry obligation; extend protection or close new residual |
| Partial exit then CANCELLED/REJECTED | Preserve original exit intent; close only remaining quantity |
| Exit COMPLETE but broker position disagrees | Recovery, not delete tracking or invent flatness |
| Stop quantity mismatches changed residual | Restore correct coverage using known order state; no overclosing |
| Stop modification rejected | Confirmed stop remains unchanged; requested value displayed as failed/pending |
| Both normal and hard exit requested concurrently | One reduction owner; hard urgency escalates existing intent |
| Circuit open from analytics failures | Genuine hard protection/flatten and required reconciliation get critical service |
| Brokerage/API persistence fails after acceptance | Still reconcile accepted order; never interpret local IO failure as safe-to-resubmit |
| No LTP available for mandated exit | Explicit execution policy and recovery; no ₹0 price or false completion |
| Market gap through stop-limit | Triggered-but-unfilled status modeled; recovery manages known residual |
| Process dies at each submission/ack/fill/checkpoint boundary | Restart resolves intent using persisted identity before new order |
| Corrupt/missing active snapshot with live exposure | Restore from journal/broker or recovery; never silently abandon it |

Where a broker does not offer atomic reduce-only/stop handoff, test the actual
supported cancel-confirm-reread or modification sequence. Do not write a fake
that grants guarantees the real API does not have.

### 3.3 Risk and session

- Current marked gross/net/symbol/sector exposure includes actual reconciled
  quantities and conservative pending-entry reservations. Protection/reduction
  orders do not become new entry exposure; unknown orders remain conservative.
- Unknown correlation is not zero. Insufficient aligned samples, missing symbols,
  constant prices and network failure follow the documented fallback/admission
  policy. Adverse signed exposure is considered; negatively correlated opposing
  positions are not automatically assumed diversifying.
- Max simultaneous/daily/symbol trade limits include pending admissions across
  concurrent symbols and are reserved/released exactly once.
- Daily loss uses the same net definition in fast updates, reconciliation and UI;
  fees cannot disappear on the next poll. Kill state latches through a rebound
  and same-session restart.
- RECONCILIATION_PENDING/FAILED/STALE blocks new risk. Reductions continue.
- Entry pause/confirmation mode does not stop management; scan/LLM/backtest stalls
  do not delay hard-risk scheduling. A start call cannot override a recovery halt.
- Square-off keeps supervising until residuals and orders are terminal, even
  after the cutoff/15:30. Pending entry remainders cannot reopen the account.
- UTC-host, IST-host, holiday, weekend, partial session and date rollover fixtures
  produce identical exchange-clock decisions. Overnight unresolved obligations
  survive a new session.

## 4. Evidence, causality and state unit tests

1. **Trigger versus state:** no repeated EMA entry cross in an intact trend ->
   HOLD, not no-conviction exit.
2. **Correlation cap:** RSI + Stochastic + StochRSI contradiction remains one
   dynamics group. Adding aliases/duplicated indicators cannot change the action.
3. **Dependency cap:** VWAP structure failure and VWAP context failure from the
   same crossing cannot satisfy two-family confirmation.
4. **Unknown values:** missing/NaN volume, invalid ATR, failed fetch or stale HTF
   cannot become a negative vote or heal a prior warning.
5. **Price primacy:** weak momentum/volume without material price failure does
   not produce Route B invalidation; two buffered structural failure closes do
   produce Route A even if another oscillator supports the position.
6. **Setup semantics:** below-VWAP state can support early range convergence and
   contradict a VWAP-reclaim setup; same raw snapshot, different pinned thesis.
7. **Confirmation:** distinct consecutive bars count; duplicates/polls/revisions
   do not. A healthy reclaim resets the episode; missing expected bar breaks it.
8. **Recovery:** WEAKENING requires the configured recovery sequence; it does not
   flap with each inside-buffer close. INVALIDATED never recovers in this epoch.
9. **Stop ratchet:** long stop cannot decrease; short cannot increase. Existing
   profitable protection cannot move back to entry. No-op tick rounding is stable.
10. **Pure deterministic API:** no wall-clock/global config/random/network access;
    identical serialized inputs reproduce the same output/state hash.
11. **Causal HTF:** a 10:00–10:15 bar is unavailable at 10:10; exactly aligned
    09:15-based resampling and availability delay determine eligibility.
12. **Causal structure:** right-confirmed swing is inaccessible before known-at;
    appending future bars never changes decisions in the already-known prefix.
13. **Session VWAP:** reset per session/zone, zero cumulative volume yields unknown,
    no carry across date boundary; rolling VWAP cannot masquerade as session VWAP.
14. **R freeze:** trailing/partial exits/config changes do not alter initial R;
    partial entry risk is provisional until its terminal fill event.
15. **All state edges:** table-driven legal transitions and rejection of illegal
    ones, especially UNKNOWN->CLOSED, EXIT_PENDING->OPEN and reused trade epochs.

## 5. Trader-behavior scenario suite

Build small synthetic candle/event paths with declared expected transitions and
actions. Mirror long/short cases using direction-normalized fixtures where
economically appropriate; liquidity/fee asymmetry remains explicit.

| ID | Path | Expected management |
|---|---|---|
| S01 | Early trend, one adverse candle, defended swing intact | HOLD_EARLY_DEVELOPMENT; hard stop unchanged |
| S02 | Rising trend, RSI/Stochastic turn down, low-volume pullback above structure | HOLD_HEALTHY_PULLBACK; one dynamics warning |
| S03 | +1.5R trade consolidates with intact 15m structure | HOLD_CONSOLIDATION; no clock-only breakeven |
| S04 | Breakout retests boundary intrabar, closes/reclaims above it | HOLD; no failed-breakout acceptance |
| S05 | Breakout accepts inside frozen range on two closes | THESIS_BREAKOUT_FAILED, exit latched |
| S06 | One bearish rejection at resistance between entry and target | Review/HOLD; not a full exit by itself |
| S07 | Adverse swing failure persists with volume-backed displacement | Confirmed exit; no minimum-hold veto |
| S08 | 5m weakness while relevant 15m structure remains intact | Weakening/HOLD unless explicit structural premise is invalidated |
| S09 | Regime label TRENDING->BREAKOUT from volatility expansion, direction intact | No regime-label liquidation |
| S10 | Range fade accepts outside defended range in adverse trend | Invalidation, not indefinite mean-reversion hope |
| S11 | Range convergence hits declared objective | Full objective exit; no silent runner conversion |
| S12 | Trend runner touches review zone and continues | HOLD/review; fixed-target comparator remains a separate profile |
| S13 | New favorable swing confirmed after progress | Propose monotone structural trail; acknowledge before effective |
| S14 | Temporary oscillator deterioration at +2R, structure intact | No profit-only aggressive trail/exit |
| S15 | +2R after strong MFE, confirmed loss of progress and independent deterioration | PROFIT_REVERSAL_CONFIRMED |
| S16 | Old trade with no progress and confirmed adverse structure/context | TIME_NO_PROGRESS_CONFIRMED |
| S17 | Old trade building a healthy base | HOLD, age alone insufficient |
| S18 | Intrabar catastrophic stop in EARLY phase | Immediate hard reduction; no candle confirmation |
| S19 | Daily loss trips while another normal exit is pending | Escalate intent, cancel entries, manage residuals |
| S20 | Forced deadline during data outage / stopped entry scanner | Hard close obligation; persistent recovery until resolved |
| S21 | Empty candle response while trade is temporarily losing | HOLD_DATA_DEGRADED, never no-conviction exit |
| S22 | Restart during WEAKENING after first failure close | Restore counters/time, evaluate only eligible new data |
| S23 | Restart during unknown exit submission | Reconcile same intent; no duplicate exit |
| S24 | Manual/adopted position with unknown thesis | Retain verified protection/objective/session management; no fabricated playbook |
| S25 | External partial close / reverse / CNC and MIS same symbol | Separate identity, reconcile allocation and correct residual coverage |
| S26 | Incomplete 15m/duplicate 5m/out-of-order revision | Explicit unavailable/no-op state; no false confirmation |
| S27 | +0.5R exit; +3R reachable before retained stop/deadline | Premature-exit diagnostic visible in replay |
| S28 | Same path, retained stop hit before later +3R | Do not label +3R executable missed profit |
| S29 | +2R exit before reversal | Report avoided giveback and continuation separately |
| S30 | Stop and target touched in one coarse bar | Shared conservative ambiguity handling; bounded excursion and reason |

## 6. Persistence and replay tests

- Round-trip thesis, nested state, UTC/exchange times, confirmation episodes and
  policy versions. Missing fields migrate to explicit unknown, not current time.
- Crash injection before/after each statement of trace/state/intent transaction
  proves all-or-nothing persistence. A completed broker mutation with failed
  local commit enters recovery, not a retry with a fresh tag.
- Repeated fill delivery has no duplicate quantity, fees or journal closure.
  Partial fills and order-level brokerage caps reconcile across restarts.
- Concurrent checkpoint writers cannot roll back event/state sequence.
- Legacy score alias reads correctly; unknown-price/legacy-unreconciled trades
  are excluded from calibration/performance with explicit counts.
- Replay uses retained inputs/artifacts and reproduces every HOLD/EXIT/TIGHTEN
  decision. Changing a future candle cannot alter past decisions.
- Counterfactuals cannot mutate live journal/position state, access live gateway,
  widen stops or consume forward data in the original decision.
- Policy rollback preserves confirmed stops and latched exits. A shadow decision
  cannot call any order mutation method.

## 7. Research/simulator tests

- Entry signal at completed T cannot fill before T+1 availability. Normal close
  decisions cannot fill at an already-observed historical close.
- Gap-through-stop uses executable price/model, never fictitious trigger fill;
  stop-limit nonfill, spread, latency and partial liquidity scenarios are explicit.
- Slippage applied once per simulated fill; fees once with correct per-order cap.
  Cash change over a flat round trip equals trade net P&L within declared rounding.
- Stop/target order ambiguity identical across backtest and counterfactual driver.
  Exit-bar extrema exclude unknown post-exit movement or return honest bounds.
- Every session closes managed intraday exposure; missing last-symbol bar is
  censored/escalated explicitly rather than silently retaining an open position.
- Synchronized multi-symbol admission/order sequencing is deterministic and uses
  actual exposure, pending reservations, risk constraints and full MTM equity.
- Training/warmup cannot trade into a scored test window. Test folds are disjoint;
  parameter/calibration selection cannot inspect the next test period.
- Every quality metric has hand-calculated long/short, zero-MFE, partial-fill,
  missing-data and cost-only-loss examples.

## 8. Operator and security integration tests

Verify entry pause, manual close, cancel, emergency scope and effective mode
against backend acknowledgements. Position cards show confirmed versus requested
stops and residuals; unavailable risk/data state stays visible. Navigating pages
cannot duplicate subscriptions or remove the root subscription. Crash/restart
handshake restores supervision before the UI advertises readiness.

Separate urgent security work needs tests proving no API secret/access token is
returned to or persisted by renderer auth state; blank masked settings cannot
overwrite saved credentials; DEV/PAPER state cannot populate LIVE storage; and
untrusted IPC cannot assign a risk-bypass role or arbitrary credential endpoint.
Use fake values only.

## 9. Required phase checks and release gates

For Python changes run `uv run ruff check backend/ run_backend.py`,
`uv run ruff format --check backend/ run_backend.py`, and `uv run pytest`.
For frontend changes run `npm run lint`, `npm run typecheck`, `npm run build`
and the relevant test scripts. CI should run the new deterministic contracts,
recovery, parity and fixed-fixture replay tests; expensive research experiments
are reproducible offline jobs with saved manifests.

Release gates: all P0 invariants pass; every managed decision has replayable
inputs; identical-input decision parity is complete; fault injection produces no
unexplained duplicate/reversal/abandoned exposure; paper/shadow evidence and
out-of-sample robustness meet BACKTEST_PLAN. Unit-test success alone does not
authorize a claim of superior exits.
