"""Alternative-policy outcomes require executions, costs and censored coverage."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.alternative_policy import simulate_alternative_exit_execution
from backend.exit_management.engine import ExitPolicy
from backend.exit_management.profiles import resolve_profile
from backend.replay import replay_recorded_exit_decision
from backend.tests.test_candidate_execution import START, SYMBOL, _event, _runner


def market_data(*, volume=1000, close=94):
    return {
        SYMBOL: pd.DataFrame(
            [
                {
                    "date": START + timedelta(minutes=5 * index),
                    "open": close,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": volume,
                }
                for index in range(3)
            ]
        )
    }


def test_paired_execution_retains_stop_fills_costs_and_original_checkpoint(tmp_path):
    runner = _runner(tmp_path)
    before = deepcopy(runner.broker.positions)
    fills = deepcopy(runner.broker.fills)
    report = simulate_alternative_exit_execution(
        checkpoint=runner,
        policies={SYMBOL: ExitPolicy(policy_version="alternative-fixture")},
        market_data=market_data(),
    )
    assert report["status"] == "COMPLETE"
    assert report["comparisons"][0]["net_r_delta"] == 0
    assert report["original"]["trades"] == report["alternative"]["trades"]
    trade = report["alternative"]["trades"][0]
    assert trade["exit_price"] < 95  # Gap uses executable open, never ideal stop.
    assert isinstance(trade["exit_time"], str)
    assert trade["exit_time"].endswith("+00:00")
    assert trade["net_pnl"] < trade["gross_pnl"]
    assert len(report["alternative"]["fills"]) == 2
    assert report["alternative"]["manifest"]["execution"]["namespace"] == "REPLAY"
    assert report["alternative"]["manifest"]["datasets"][SYMBOL]
    assert report["alternative"]["outcomes"][SYMBOL]["initial_risk_currency"] == 50
    assert runner.broker.positions == before
    assert runner.broker.fills == fills
    assert runner.evaluations == []
    assert runner._last_event is None
    json.dumps(report, allow_nan=False)


def test_missing_liquidity_censors_account_and_paired_financial_claims(tmp_path):
    runner = _runner(tmp_path)
    report = simulate_alternative_exit_execution(
        checkpoint=runner,
        policies={SYMBOL: ExitPolicy()},
        market_data=market_data(volume=0),
    )
    assert report["status"] == "CENSORED"
    assert report["comparisons"][0]["net_r_delta"] is None
    assert report["alternative"]["outcomes"][SYMBOL]["captured_net_r"] is None
    assert report["alternative"]["censored_positions"]
    assert len(report["alternative"]["fills"]) == 1


def test_alternative_cannot_change_hard_risk_or_transplant_confirmation(tmp_path):
    runner = _runner(tmp_path)
    with pytest.raises(ValueError, match="preserve hard risk"):
        simulate_alternative_exit_execution(
            checkpoint=runner,
            policies={SYMBOL: ExitPolicy(tick_size=0.1)},
            market_data=market_data(),
        )
    _event(runner, 0, 100)
    with pytest.raises(ValueError, match="untouched entry checkpoint"):
        simulate_alternative_exit_execution(
            checkpoint=runner,
            policies={SYMBOL: ExitPolicy()},
            market_data=market_data(),
        )


def test_account_loss_latch_and_daily_risk_survive_policy_branch(tmp_path):
    runner = _runner(tmp_path, loss_limit=10)
    runner.daily_loss_latched = True
    report = simulate_alternative_exit_execution(
        checkpoint=runner,
        policies={SYMBOL: ExitPolicy()},
        market_data=market_data(close=102),
    )
    branch = report["alternative"]
    assert branch["manifest"]["daily_loss_limit"] == 10
    assert branch["recorded_decisions"][0]["primary_reason_code"] == "RISK_DAILY_LOSS"
    assert branch["status"] == "COMPLETE"
    assert runner.daily_loss_latched


def test_alternative_does_not_reset_a_latched_stop_request(tmp_path):
    runner = _runner(tmp_path)
    runner.positions[SYMBOL].management = replace(
        runner.positions[SYMBOL].management, eligible_completed_bars=1
    )
    with pytest.raises(ValueError, match="prior normal management"):
        simulate_alternative_exit_execution(
            checkpoint=runner,
            policies={SYMBOL: ExitPolicy()},
            market_data=market_data(),
        )


def test_changed_normal_confirmation_can_hold_recovery_without_inventing_pnl(tmp_path):
    runner = _runner(tmp_path)
    profile = resolve_profile(runner.positions[SYMBOL].thesis.management_profile)
    policy = ExitPolicy(
        profile_overrides={
            profile.name.value: replace(profile, failure_confirmation_bars=3)
        }
    )
    rows = []
    for index in range(-7, 7):
        close = 98 if index in (0, 1) else 101
        rows.append(
            {
                "date": START + timedelta(minutes=5 * index),
                "open": close,
                "high": close + 1,
                "low": close - 0.5,
                "close": close,
                "volume": 1000,
            }
        )
    report = simulate_alternative_exit_execution(
        checkpoint=runner,
        policies={SYMBOL: policy},
        market_data={SYMBOL: pd.DataFrame(rows)},
    )
    assert report["original"]["trades"][0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert report["alternative"]["trades"] == []
    assert report["comparisons"][0]["net_r_delta"] is None
    reasons = [
        record["primary_reason_code"]
        for record in report["alternative"]["recorded_decisions"]
    ]
    assert "HOLD_THESIS_VALID" in reasons
    assert reasons[-1] == "SESSION_FORCED_FLAT"
    for record in report["alternative"]["recorded_decisions"]:
        replay_recorded_exit_decision(record)
