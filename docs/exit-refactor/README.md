# Exit-management architecture package

**Status:** architecture proposal; no trading implementation is included.
**Audit baseline:** `021fe80a3c856e35194638a170212c460fd9ae00`, inspected 2026-09-19.

## Architectural conclusion

Manage a position against a **persisted, entry-specific thesis**, using causal
market context and completed candles. Keep a separately scheduled **hard-risk
supervisor** authoritative over stops, the daily loss latch, forced session
closure, and emergency recovery. Both produce intents for the existing execution
gateway; neither considers order submission equivalent to a confirmed fill.

The current premature-exit problem is real in the implementation: absence of a
new entry trigger is treated as loss of conviction, a single rejected level can
close a trade, and elapsed time can move a stop to entry without checking whether
that is a sensible or even tighter stop. Execution/accounting defects also need
attention before exit-quality comparisons are trustworthy.

## Reading order

| Document | Purpose |
|---|---|
| [REPOSITORY_AUDIT.md](REPOSITORY_AUDIT.md) | Evidence, severity, all 14 requested high-risk checks, and scoped engineering roadmap |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Current system, all order/exit paths, proposed components and authority boundaries |
| [DECISION_MODEL.md](DECISION_MODEL.md) | Thesis, evidence families, invalidation, hysteresis, profit and time management |
| [STATE_MACHINE.md](STATE_MACHINE.md) | Exposure lifecycle, thesis health, development phase, protection and recovery transitions |
| [DATA_AND_REPLAY.md](DATA_AND_REPLAY.md) | Contracts, persistence, decision traces, accounting, exit-quality metrics and counterfactuals |
| [EXIT_REASON_CODES.md](EXIT_REASON_CODES.md) | Stable machine-readable reasons, action precedence and legacy mapping |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Ten dependency-ordered implementation phases with files, tests and acceptance gates |
| [TEST_PLAN.md](TEST_PLAN.md) | Deterministic scenarios, safety invariants, contracts, failure and parity tests |
| [BACKTEST_PLAN.md](BACKTEST_PLAN.md) | Causal simulation, paired comparisons, walk-forward evaluation and promotion criteria |

## Scope and sequencing

The exit refactor includes the minimum correctness foundations it depends on:
broker contracts, residual-quantity execution, continuous risk supervision,
causal candle/context handling, versioned thesis state, shared exit policy,
trace/replay, and defensible exit-quality evaluation. It retains the scanner,
strategies, playbook concept, gateway, risk manager, SQLite journal, atomic
snapshot mechanism, and Electron application.

Credential leakage, development/live storage isolation, and broader manual-order
security are urgent **separate fixes** that also block live promotion. A new
screener model, entry-alpha optimization, statistical calibration model,
portfolio optimizer, and LLM trading agent are separate projects. The audit gives
each finding an explicit destination rather than turning this into an app rewrite.

## Ten phases for the next coding agent

1. Normalize broker contracts and reconcile risk/accounting inputs.
2. Make order intents, protective stops, and residual execution recoverable.
3. Keep risk supervision running independently of entry scanning and UI mode.
4. Build validated, completed-candle 5m/15m market context.
5. Persist immutable theses, lifecycle state, and structured decisions.
6. Implement the pure deterministic exit policy and management profiles.
7. Integrate it with live orchestration in shadow mode.
8. Run the same policy/coordinator in paper, backtest, and replay adapters.
9. Expose exit-quality metrics and operator explanations.
10. Complete robustness experiments, promotion gates, and legacy-path retirement.

Each phase is broken into small reviewable slices in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Proposed module paths throughout
this package are future work, not files that already exist.

## Evidence and uncertainty

- Backend baseline: **340 tests passed**, Ruff lint passed, Ruff format check passed.
- Frontend baseline: lint passed with four existing warnings; typecheck and build passed.
- `npm run test:strategy-settings` could not start: the local installed dependencies
  lack the `tsx` executable declared in `package.json`. This is an environment
  limitation. The same test passed using
  `node --experimental-strip-types --test tests/strategy-settings.test.ts`.
- Small offline probes used synthetic broker payloads and a temporary HOME. They
  reproduced several audit findings; no broker session or account data was used.
- Code establishes failure paths, not their frequency in the user's trading
  history. Proposed trading thresholds are research hypotheses, not calibrated
  probabilities or claims of increased profitability.

The audit's source references use baseline line numbers and function names.
Validation status, simulation assumptions, and unresolved broker-specific
questions are explicit in the linked documents.
