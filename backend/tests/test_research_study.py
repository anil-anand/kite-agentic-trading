"""Paired studies execute retained checkpoints without borrowing later data."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.research_study import run_paired_case
from backend.backtesting.simulated_broker import SimulationExecutionPolicy
from backend.exit_management.engine import ExitPolicy
from backend.exit_management.models import ManagementState
from backend.exit_management.profiles import resolve_profile
from backend.replay import serialize_replay_artifact
from backend.tests.exit_management.test_engine import _state, _thesis
from backend.tests.test_candidate_execution import START, SYMBOL

DAY = START.replace(hour=0, minute=0)
END = DAY + timedelta(days=1)


def case():
    thesis = _thesis()
    return {
        "case_id": "synthetic-breakout-retest",
        "checkpoint_at": START.isoformat(),
        "initial_capital": 100000,
        "positions": [{"thesis": thesis.to_dict(), "state": _state().to_dict()}],
    }


def data(*, volume=1000):
    rows = []
    for index in range(-7, 4):
        price = 98 if index in (0, 1) else 101
        rows.append(
            {
                "date": START + timedelta(minutes=5 * index),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": volume,
            }
        )
    rows.append(
        {
            "date": START.replace(hour=9, minute=45),
            "open": 102,
            "high": 102.2,
            "low": 101.8,
            "close": 102,
            "volume": volume,
        }
    )
    return {SYMBOL: pd.DataFrame(rows)}


def run(**changes):
    values = {
        "case": case(),
        "market_data": data(),
        "candidate_policy": ExitPolicy(),
        "control_policy": ExitPolicy(policy_version="declared-control"),
        "test_start": DAY,
        "test_end": END,
        "source_revision": "synthetic-test-source",
    }
    values.update(changes)
    return run_paired_case(**values)


def test_shared_execution_and_decision_parity_are_real_and_artifacts_detached():
    seed, frames = case(), data()
    before = deepcopy(seed)
    original_frames = deepcopy(frames)
    result = run(case=seed, market_data=frames)
    assert result["paired_counts"] == {"total": 1, "complete": 1, "censored": 0}
    assert result["comparisons"][0]["net_r_delta"] == 0
    assert result["candidate"]["trades"][0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert (
        result["candidate"]["trades"][0]["net_pnl"]
        < result["candidate"]["trades"][0]["gross_pnl"]
    )
    assert result["parity"]["candidate"]["replayed_decisions"] >= 2
    assert result["parity"]["control"]["mismatches"] == 0
    assert result["candidate"]["manifest"]["execution"]["namespace"] == "REPLAY"
    assert result["production_admission_parity"] is False
    assert result["operational_evidence"] is False
    assert result["identities"][0]["source_position_key"].startswith("LIVE:")
    assert result["identities"][0]["replay_position_key"].startswith("REPLAY:")
    assert seed == before
    pd.testing.assert_frame_equal(frames[SYMBOL], original_frames[SYMBOL])
    seed["positions"].clear()
    frames[SYMBOL].loc[0, "close"] = 999
    assert result["artifacts"]["inputs"]["payload"]["case"] == before
    for artifact in result["artifacts"].values():
        encoded = json.dumps(
            artifact["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert artifact["sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
    json.dumps(result, allow_nan=False)


def test_parameter_control_really_changes_fills_and_delta_direction():
    profile = resolve_profile(_thesis().management_profile)
    control = ExitPolicy(
        profile_overrides={
            profile.name.value: replace(profile, failure_confirmation_bars=3)
        }
    )
    result = run(control_policy=serialize_replay_artifact(control))
    candidate_trade = result["candidate"]["trades"][0]
    control_trade = result["control"]["trades"][0]
    assert candidate_trade["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert control_trade["exit_reason"] == "SESSION_FORCED_FLAT"
    assert result["comparisons"][0]["net_r_delta"] == pytest.approx(
        (candidate_trade["net_pnl"] - control_trade["net_pnl"]) / 50
    )
    assert result["comparisons"][0]["net_r_delta"] < 0


def test_missing_liquidity_censors_both_policies_without_financial_claim():
    result = run(market_data=data(volume=0))
    assert result["paired_counts"] == {"total": 1, "complete": 0, "censored": 1}
    assert result["comparisons"][0]["net_r_delta"] is None
    assert result["candidate"]["censored_positions"][0]["quantity"] == 10
    assert len(result["candidate"]["fills"]) == 1
    assert result["parity"]["candidate"]["replayed_decisions"] > 2


def test_missing_symbol_data_is_reported_and_still_runs_independent_clock():
    result = run(market_data={})
    assert result["missing_market_symbols"] == [SYMBOL]
    assert (
        result["candidate"]["recorded_decisions"][0]["primary_reason_code"]
        == "SESSION_FORCED_FLAT"
    )
    assert result["paired_counts"]["censored"] == 1


def test_execution_cost_stress_changes_exits_but_preserves_fixed_entry_price():
    baseline = run(execution_policy=SimulationExecutionPolicy(slippage_bps=0))
    stressed = run(
        execution_policy={"slippage_bps": 25}, cost_policy={"brokerage_pct": 0.001}
    )
    assert stressed["candidate"]["fills"][0]["price"] == 100
    assert (
        stressed["candidate"]["trades"][0]["net_pnl"]
        < baseline["candidate"]["trades"][0]["net_pnl"]
    )
    assert (
        stressed["artifacts"]["policies"]["sha256"]
        != baseline["artifacts"]["policies"]["sha256"]
    )


@pytest.mark.parametrize(
    "change", ["future_row", "future_receipt", "naive_date", "label"]
)
def test_future_availability_labels_and_ambiguous_timestamps_are_rejected(change):
    frames = data()
    if change == "future_row":
        frames[SYMBOL].loc[len(frames[SYMBOL]) - 1, "date"] = END
    elif change == "future_receipt":
        frames[SYMBOL]["received_at"] = END
    elif change == "naive_date":
        frames[SYMBOL]["date"] = frames[SYMBOL]["date"].dt.tz_localize(None)
    else:
        frames[SYMBOL]["future_pnl"] = 1000
    with pytest.raises(ValueError):
        run(market_data=frames)


def test_forced_deadline_and_entry_cannot_escape_scoring_bounds():
    with pytest.raises(ValueError, match="forced deadline"):
        run(test_end=START + timedelta(hours=1))
    with pytest.raises(ValueError, match="checkpoint must belong"):
        run(test_start=START + timedelta(minutes=5))


@pytest.mark.parametrize("hour, minute", [(10, 0), (3, 40), (9, 58), (4, 21)])
def test_outside_session_bars_cannot_fill_orders_or_supply_feature_warmup(hour, minute):
    frames = data()
    row = frames[SYMBOL].iloc[-1].copy()
    # Reject post-close/premarket, close-straddling and off-grid bars before
    # they can provide simulated liquidity or enter feature warmup.
    row["date"] = START.replace(hour=hour, minute=minute)
    frames[SYMBOL] = pd.concat([frames[SYMBOL], row.to_frame().T], ignore_index=True)
    with pytest.raises(ValueError, match="declared trading session"):
        run(market_data=frames)


def test_warmup_before_checkpoint_never_fills_an_entry_stop():
    frames = data()
    frames[SYMBOL].loc[0, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    result = run(market_data=frames, test_start=START, warmup_start=DAY)
    assert result["candidate"]["trades"][0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert result["candidate"]["fills"][1]["exchange_time"] > START.isoformat()


def test_nonfresh_management_and_future_entry_premise_are_rejected():
    seed = case()
    seed["positions"][0]["management"] = ManagementState(failure_count=1).to_dict()
    with pytest.raises(ValueError, match="fresh normal-management"):
        run(case=seed)
    seed = case()
    seed["positions"][0]["thesis"]["created_at"] = (
        START + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="future entry premise"):
        run(case=seed)


def test_entry_snapshot_cannot_borrow_data_after_the_thesis_was_frozen():
    seed = case()
    seed["positions"][0]["thesis"]["input_reference"]["source_as_of"] = (
        START.isoformat()
    )
    with pytest.raises(ValueError, match="unavailable when the thesis was frozen"):
        run(case=seed)
    seed = case()
    seed["positions"][0]["thesis"]["causal_anchors"]["setup_range"]["known_at"] = (
        START.isoformat()
    )
    with pytest.raises(ValueError, match="future entry snapshot"):
        run(case=seed)


def test_partial_fill_stress_keeps_residuals_censored_instead_of_imputing_close():
    result = run(execution_policy={"max_fill_fraction": 0.5})
    assert result["paired_counts"]["censored"] == 1
    assert result["candidate"]["trades"] == []
    remaining = result["candidate"]["censored_positions"][0]["quantity"]
    assert 0 < remaining < 10
    assert result["comparisons"][0]["net_r_delta"] is None


def test_shared_account_daily_loss_closes_multiple_symbols_with_fixed_risk():
    seed = case()
    second_thesis = replace(
        _thesis(),
        symbol="SECOND",
        position_key="LIVE:other",
        thesis_id="second",
        trade_id="second",
    )
    second_state = replace(_state(), position_key=second_thesis.position_key)
    seed["positions"].append(
        {"thesis": second_thesis.to_dict(), "state": second_state.to_dict()}
    )
    seed["daily_loss_limit"] = 20
    frames = data()
    frames["SECOND"] = frames[SYMBOL].copy()
    result = run(case=seed, market_data=frames)
    assert result["paired_counts"]["total"] == 2
    assert result["paired_counts"]["complete"] == 2
    reasons = [
        record["primary_reason_code"]
        for record in result["candidate"]["recorded_decisions"]
    ]
    assert reasons.count("RISK_DAILY_LOSS") >= 2
    assert all(
        trade["exit_reason"] == "RISK_DAILY_LOSS"
        for trade in result["candidate"]["trades"]
    )
