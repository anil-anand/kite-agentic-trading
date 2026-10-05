"""Adversarial split/manifest cases found during the phase-10 review."""

from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.walk_forward import (
    SelectionResult,
    WalkForwardConfig,
    WalkForwardValidator,
)


def _frame(days=35):
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=days, freq="D", tz="UTC"),
            "close": [100.0] * days,
        }
    )


def _config(**kwargs):
    return replace(
        WalkForwardConfig(
            train_days=10,
            warmup_days=3,
            test_days=5,
            step_days=5,
            final_holdout_days=5,
        ),
        **kwargs,
    )


def _run(runner, *, frame=None, config=None, selector=None):
    return WalkForwardValidator().validate_study(
        _frame() if frame is None else frame,
        "TEST",
        config=_config() if config is None else config,
        policies={"candidate": {"version": "v1"}},
        runner=runner,
        selector=selector,
        source_commit="frozen-source",
    )


def _trade(fold, exit_time):
    return {
        "entry_time": fold.test_start,
        "exit_time": exit_time,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "direction": "BUY",
        "quantity": 1,
        "gross_pnl": 1.0,
        "net_pnl": 0.9,
        "total_fees": 0.1,
        "signal_info": {"stopLoss": 99.0},
    }


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(days=6)])
def test_completed_trade_cannot_borrow_next_fold_or_holdout_exit(offset):
    with pytest.raises(ValueError, match="out-of-window"):
        _run(lambda fold, *_args: {"trades": [_trade(fold, fold.test_end + offset)]})


@pytest.mark.parametrize("at_start", [True, False])
def test_warmup_or_future_equity_cannot_change_oos_risk(at_start):
    def runner(fold, *_args):
        timestamp = (
            fold.test_start - timedelta(seconds=1)
            if at_start
            else fold.test_end + timedelta(seconds=1)
        )
        return {"equity_curve": [{"timestamp": timestamp, "equity": 90_000.0}]}

    with pytest.raises(ValueError, match="out-of-window equity"):
        _run(runner)


def test_equity_snapshot_at_end_is_permitted_without_boundary_fill():
    result = _run(
        lambda fold, *_args: {
            "equity_curve": [
                {"timestamp": fold.test_start, "equity": 100_000.0},
                {"timestamp": fold.test_end, "equity": 100_000.0},
            ],
        }
    )
    assert result["windows"]


@pytest.mark.parametrize("value", [pd.NaT, "2026-01-01T00:00:00"])
def test_missing_or_naive_market_time_cannot_silently_shift_fold_ownership(value):
    frame = _frame()
    frame["date"] = frame["date"].astype(object)
    frame.loc[0, "date"] = value
    with pytest.raises(ValueError, match="aware timestamps"):
        _run(lambda *_args: {}, frame=frame)


def test_incomplete_terminal_slice_cannot_inflate_independent_fold_count():
    folds, holdout_start, _end = WalkForwardValidator().plan_folds(_frame(), _config())
    assert len(folds) == 3
    assert all(fold.test_end - fold.test_start == timedelta(days=5) for fold in folds)
    assert folds[-1].test_end < holdout_start


def test_nested_criterion_must_be_declared_before_selection_and_stay_fixed():
    with pytest.raises(ValueError, match="predeclared criterion"):
        _run(
            lambda *_args: {},
            config=_config(selection_mode="nested", inner_validation_days=3),
            selector=lambda _selection: SelectionResult("candidate", {}, "RISK"),
        )
    with pytest.raises(ValueError, match="changed the predeclared criterion"):
        _run(
            lambda *_args: {},
            config=_config(
                selection_mode="nested",
                inner_validation_days=3,
                selection_criterion_id="TAIL_RISK_AND_STABILITY",
            ),
            selector=lambda _selection: SelectionResult("candidate", {}, "RISK"),
        )


def test_delayed_data_and_crossing_labels_never_reach_selector_or_warmup():
    frame = _frame()
    frame["available_at"] = frame["date"] + timedelta(hours=1)
    frame["received_at"] = frame["date"] + timedelta(hours=2)
    frame["label_end_at"] = frame["date"] + timedelta(days=1)
    frame["future_return"] = 99.0
    # A corrected historical row first received in the holdout is not historical
    # knowledge available to any training/validation/warmup callback.
    frame.loc[0, "received_at"] = frame["date"].iloc[-1]
    observed = []

    def selector(selection):
        assert (
            selection.train_data["received_at"] < selection.fold.inner_training_end
        ).all()
        assert (
            selection.train_data["label_end_at"] < selection.fold.inner_training_end
        ).all()
        assert (
            selection.inner_validation_data["label_end_at"] < selection.fold.train_end
        ).all()
        observed.append(selection)
        return SelectionResult("candidate", {"criterion": "frozen"}, "TAIL_RISK")

    def runner(fold, warmup, test, _policy):
        assert "label_end_at" not in warmup
        assert "future_return" not in warmup
        assert "label_end_at" not in test
        assert "future_return" not in test
        assert (warmup["received_at"] < fold.test_start).all()
        assert (test["received_at"] < fold.test_end).all()
        return {}

    _run(
        runner,
        frame=frame,
        config=_config(
            selection_mode="nested",
            inner_validation_days=3,
            selection_criterion_id="TAIL_RISK",
            label_columns=("future_return",),
        ),
        selector=selector,
    )
    assert observed


def test_supervised_labels_require_explicit_known_at_horizons():
    frame = _frame()
    frame["future_return"] = 1.0
    with pytest.raises(ValueError, match="label_end_at"):
        _run(
            lambda *_args: {},
            frame=frame,
            config=_config(label_columns=("future_return",)),
        )


def test_manifest_pins_policies_capital_trials_results_and_unresolved_exposure():
    result = _run(
        lambda fold, *_args: {
            "trades": [_trade(fold, fold.test_start + timedelta(hours=1))],
            "censored_positions": [{"symbol": "TEST", "quantity": 10}],
            "pending_orders": [{"role": "EXIT", "remaining_quantity": 10}],
            "artifact": {"execution_policy": {"latency_ms": 100}},
        },
        config=_config(prior_trial_count=7),
    )
    manifest = result["manifest"]
    assert manifest["trial_count"] == len(result["windows"]) + 7
    assert manifest["candidate_policy_artifacts"] == {"candidate": {"version": "v1"}}
    assert manifest["initial_account_state"]["initial_capital"] == 100_000.0
    assert manifest["runner_contract"] == "EXTERNAL_CALLBACK_UNVERIFIED"
    for report in manifest["fold_results"]:
        assert report["metrics"]["trade_count"] == 1
        assert report["execution_state"]["censored_positions"][0]["quantity"] == 10
        assert (
            report["execution_state"]["pending_orders"][0]["remaining_quantity"] == 10
        )
        assert len(report["selection_artifact_hash"]) == 64


def test_retained_trade_artifacts_are_detached_from_later_callback_mutation():
    previous_trades = []

    def runner(fold, *_args):
        for trade in previous_trades:
            trade["signal_info"]["stopLoss"] = 1.0
        trade = _trade(fold, fold.test_start + timedelta(hours=1))
        previous_trades.append(trade)
        return {"trades": [trade]}

    result = _run(runner)
    assert all(
        trade["signal_info"]["stopLoss"] == 99.0 for trade in result["all_oos_trades"]
    )


def test_nested_object_cells_cannot_alias_later_fold_inputs():
    frame = _frame()
    frame["feature"] = [{"cached_return": 1.0} for _ in range(len(frame))]
    with pytest.raises(ValueError, match="scalar values"):
        _run(lambda *_args: {}, frame=frame)


def test_frame_attrs_cannot_bypass_callback_time_isolation():
    frame = _frame()
    frame.attrs["full_future_dataset"] = _frame()

    def runner(_fold, warmup, test, _policy):
        assert warmup.attrs == {}
        assert test.attrs == {}
        return {}

    _run(runner, frame=frame)
    assert "full_future_dataset" in frame.attrs


@pytest.mark.parametrize("capital", [0, -1, True, float("nan"), float("inf")])
def test_invalid_initial_capital_cannot_create_usable_research_results(capital):
    with pytest.raises(ValueError, match="finite and positive"):
        WalkForwardValidator(initial_capital=capital)
