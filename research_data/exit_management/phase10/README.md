# Phase-10 research fixtures

These fixtures are synthetic, non-sensitive regression inputs. They are not
broker exports, account data, or evidence for candidate-policy promotion.

Each CSV has a sidecar manifest that pins its SHA-256 digest, policy label and
simulation model. Any modification to a CSV requires an intentional manifest
update and a reviewed regression expectation.

The breakout CSV tests fixture provenance. Its `REPLAY` manifest must not be
passed to the raw-only `RegressionSuite` executor; that mismatch is rejected.
It does not contain a complete entry thesis, retained decision trace or expected
candidate execution path and is not a completed replay study.

Predeclared synthetic policy and execution perturbations live in
`backend/tests/test_phase10_robustness_review.py` and run in the offline pytest
suite. These invariant checks are separate from historical robustness and
promotion evidence. See `docs/exit-refactor/PHASE10_REVIEW.md` for remaining
study and operational acceptance requirements.

`acceptance_example/plan.json` supplies a complete synthetic workflow example:
retained terminal-entry cases and candles for two OOS sessions and one holdout,
declared policy variants and execution stress. Run it using
`docs/exit-refactor/PHASE10_ACCEPTANCE.md`. Registration snapshots and hashes the
exact inputs. Its control is another shared exit policy, not a repaired legacy
control; results are never historical, portfolio or operational promotion evidence.
