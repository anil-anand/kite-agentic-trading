"""Phase-10 causal walk-forward and promotion-gate contracts."""

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from backend.backtesting.backtest_engine import BacktestEngine
from backend.backtesting.promotion import (
    PromotionCriteria,
    evaluate_promotion_gate,
)
from backend.backtesting.regression_suite import RegressionSuite
from backend.backtesting.walk_forward import (
    SelectionResult,
    WalkForwardConfig,
    WalkForwardValidator,
)
from backend.strategies.base import BaseStrategy


def _frame(days=70):
    dates = pd.date_range("2026-01-01", periods=days, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0] * days,
            "high": [101.0] * days,
            "low": [99.0] * days,
            "close": [100.5] * days,
            "volume": [1_000] * days,
        }
    )


def _trade(fold, *, entry_time=None):
    entry_at = entry_time or fold.test_start + timedelta(hours=1)
    return {
        "symbol": "FIXTURE",
        "entry_time": entry_at.isoformat(),
        "exit_time": (entry_at + timedelta(hours=1)).isoformat(),
        "entry_price": 100.0,
        "exit_price": 101.0,
        "quantity": 1,
        "direction": "BUY",
        "gross_pnl": 1.0,
        "net_pnl": 0.9,
        "total_fees": 0.1,
        "signal_info": {"stopLoss": 99.0},
        "exit_reason": "HOLDOUT_FIXTURE_EXIT",
    }


def _config(**overrides):
    values = {
        "train_days": 10,
        "warmup_days": 3,
        "test_days": 5,
        "step_days": 5,
        "purge_days": 2,
        "embargo_days": 1,
        "final_holdout_days": 10,
        "max_label_horizon_days": 2,
        "policy_version": "deterministic-exit-v1",
        "study_id": "phase10-fixture",
    }
    values.update(overrides)
    return WalkForwardConfig(**values)


class _WarmupSignalStrategy(BaseStrategy):
    def get_name(self):
        return "warmup-signal-fixture"

    def get_description(self):
        return "Emits a deterministic signal on every available feature bar."

    def calculate_signals(self, _df, _tradingsymbol):
        return [
            {
                "direction": "BUY",
                "entryPrice": 100.0,
                "stopLoss": 90.0,
                "target": 150.0,
            }
        ]


def test_fixed_walk_forward_uses_half_open_disjoint_oos_and_feature_only_warmup():
    seen = []

    def runner(fold, warmup, test, policy):
        assert policy == {"version": "deterministic-exit-v1"}
        assert (warmup["date"] < fold.test_start).all()
        assert (test["date"] >= fold.test_start).all()
        assert (test["date"] < fold.test_end).all()
        seen.append((fold, warmup.copy(), test.copy()))
        return {
            "trades": [_trade(fold)],
            "equity_curve": [
                {"timestamp": fold.test_start.isoformat(), "equity": 100_000.0},
                {"timestamp": fold.test_end.isoformat(), "equity": 100_000.9},
            ],
            "artifact": {"runner": "synthetic-common-policy"},
        }

    result = WalkForwardValidator().validate_study(
        _frame(),
        "FIXTURE",
        config=_config(),
        policies={"candidate": {"version": "deterministic-exit-v1"}},
        runner=runner,
        source_commit="fixture-commit",
    )

    windows = result["windows"]
    assert seen
    assert all(
        windows[index]["fold"]["test_end"] <= windows[index + 1]["fold"]["test_start"]
        for index in range(len(windows) - 1)
    )
    holdout = result["manifest"]["untouched_holdout"]
    assert holdout["status"] == "RESERVED_UNOBSERVED"
    assert all(window["fold"]["test_end"] <= holdout["start"] for window in windows)
    assert result["manifest"]["trial_count"] == len(windows)
    assert result["overall_oos_metrics"]["metrics_basis"] == (
        "DISJOINT_OOS_FOLD_AGGREGATE_NO_CONTINUOUS_MTM"
    )
    assert result["overall_oos_metrics"]["continuous_mtm"]["available"] is False
    assert result["overall_oos_metrics"]["aggregate_trade_metrics"]["total_net_r"]


def test_nested_selection_cannot_see_outer_test_and_freezes_selection_artifact():
    selections = []

    def selector(selection):
        assert selection.train_data["date"].max() < selection.fold.inner_training_end
        assert selection.fold.inner_training_end < selection.fold.inner_validation_start
        assert selection.inner_validation_data["date"].min() >= (
            selection.fold.inner_validation_start
        )
        assert selection.inner_validation_data["date"].max() < (
            selection.fold.inner_validation_end
        )
        selections.append(selection)
        return SelectionResult(
            policy_id="conservative",
            artifact={"chosen_before_test": selection.fold.fold_id},
            criterion_id="TAIL_RISK_AND_STABILITY",
        )

    def runner(fold, _warmup, test, policy):
        assert test["date"].min() >= fold.test_start
        assert policy == {"failure_confirmation_bars": 3}
        return {"trades": [_trade(fold)]}

    result = WalkForwardValidator().validate_study(
        _frame(),
        "FIXTURE",
        config=_config(
            selection_mode="nested",
            inner_validation_days=3,
            selection_criterion_id="TAIL_RISK_AND_STABILITY",
        ),
        policies={
            "baseline": {"failure_confirmation_bars": 2},
            "conservative": {"failure_confirmation_bars": 3},
        },
        selector=selector,
        runner=runner,
    )

    assert selections
    assert result["manifest"]["trial_count"] == len(selections) * 2
    assert all(
        window["selected_policy_id"] == "conservative" for window in result["windows"]
    )
    assert all(
        window["selection_artifact"]["chosen_before_test"] == window["fold"]["fold_id"]
        for window in result["windows"]
    )


def test_walk_forward_rejects_warmup_trades_and_direct_pnl_selection():
    def leaking_runner(fold, _warmup, _test, _policy):
        return {"trades": [_trade(fold, entry_time=fold.test_start - timedelta(1))]}

    with pytest.raises(ValueError, match="warmup"):
        WalkForwardValidator().validate_study(
            _frame(),
            "FIXTURE",
            config=_config(),
            policies={"candidate": {}},
            runner=leaking_runner,
        )

    def pnl_selector(_selection):
        return SelectionResult("candidate", {}, "NET_PNL")

    with pytest.raises(ValueError, match="P&L"):
        WalkForwardValidator().validate_study(
            _frame(),
            "FIXTURE",
            config=_config(
                selection_mode="nested",
                inner_validation_days=3,
                selection_criterion_id="TAIL_RISK_AND_STABILITY",
            ),
            policies={"candidate": {}},
            runner=lambda fold, *_args: {"trades": [_trade(fold)]},
            selector=pnl_selector,
        )


def test_walk_forward_rejects_overlapping_windows_and_insufficient_purge():
    validator = WalkForwardValidator()
    with pytest.raises(ValueError, match="step_days"):
        validator.plan_folds(_frame(), _config(step_days=4))
    with pytest.raises(ValueError, match="purge_days"):
        validator.plan_folds(_frame(), _config(purge_days=1))


def test_raw_strategy_feature_warmup_cannot_submit_or_fill_before_scoring_boundary():
    start = pd.Timestamp("2026-09-01T04:00:00+00:00")
    frame = pd.DataFrame(
        {
            "date": pd.date_range(start, periods=5, freq="5min"),
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.0] * 5,
            "volume": [1_000] * 5,
        }
    )
    engine = BacktestEngine(_WarmupSignalStrategy())
    engine.load_data("FIXTURE", frame)
    boundary = (start + timedelta(minutes=10)).to_pydatetime()
    engine.run(trading_start_at=boundary)

    entry_fills = [
        fill
        for fill in engine.broker.fills
        if engine.broker.orders[fill["order_id"]]["role"] == "ENTRY"
    ]
    assert entry_fills
    assert all(fill["exchange_time"] >= boundary for fill in entry_fills)
    assert engine.run_manifest["warmup"] == "FEATURES_ONLY_BEFORE_TRADING_START"
    assert all(point["timestamp"] >= boundary for point in engine.equity_curve)


def test_curated_regression_fixture_is_versioned_and_hash_pinned():
    root = Path(__file__).resolve().parents[2]
    suite = RegressionSuite(str(root / "research_data" / "exit_management" / "phase10"))
    manifest = suite.load_fixture_manifest("phase10_synthetic_breakout")

    assert manifest["data_classification"] == "SYNTHETIC_NON_SENSITIVE"
    assert manifest["mode"] == "REPLAY"
    assert manifest["policy_version"] == "deterministic-exit-v1"


def test_promotion_gate_fails_closed_without_holdout_security_and_operational_evidence():
    criteria = PromotionCriteria(
        minimum_oos_folds=2,
        minimum_completed_trades=10,
        maximum_unresolved_execution_rate=0.01,
        maximum_ambiguous_execution_rate=0.05,
        maximum_tail_adverse_r=2.0,
    )
    manifest = {
        "schema_version": "walk-forward-manifest-v1",
        "promotion_status": "RESEARCH_EVIDENCE_ONLY",
        "config": {"policy_version": "deterministic-exit-v1"},
        "untouched_holdout": {"status": "RESERVED_UNOBSERVED"},
    }
    failed = evaluate_promotion_gate(manifest, {}, criteria)
    assert failed.passed is False
    assert "UNTOUCHED_HOLDOUT_NOT_EVALUATED_AND_FROZEN" in failed.failures
    assert "P0_SAFETY_TESTS_PASSED" in failed.failures

    passed = evaluate_promotion_gate(
        {
            **manifest,
            "untouched_holdout": {"status": "EVALUATED_AND_FROZEN"},
        },
        {
            "p0_safety_tests_passed": True,
            "decision_parity_passed": True,
            "replay_coverage_complete": True,
            "shadow_operational_passed": True,
            "isolated_paper_operational_passed": True,
            "security_gates": {"F26": True, "F29": True, "F30": True},
            "oos_fold_count": 2,
            "completed_trade_count": 10,
            "unresolved_execution_rate": 0.0,
            "ambiguous_execution_rate": 0.0,
            "tail_adverse_r": 1.0,
        },
        criteria,
    )
    # Bare boolean attestations and mutable labels are not retained, bound
    # research evidence and cannot clear the promotion gate.
    assert passed.passed is False
    assert "EVIDENCE_NOT_BOUND_TO_RESEARCH_MANIFEST" in passed.failures
    assert passed.evidence_summary["candidate_live_activation"] == (
        "NOT_PERFORMED_BY_RESEARCH_GATE"
    )
