"""Predeclared synthetic perturbations; these are not OOS promotion evidence.

Change one policy dimension at a time. Assert management/execution invariants,
never select a variant by its historical or synthetic P&L.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import ExitAction, ExposureState
from backend.exit_management.profiles import ManagementProfile
from backend.tests.exit_management.test_engine import _context, _risk, _state, _thesis
from backend.tests.test_candidate_execution import START, SYMBOL, _event, _runner

VARIANTS = (
    {},
    {"failure_confirmation_bars": 1},  # Deliberate single-close ablation.
    {"failure_confirmation_bars": 3},
    {"boundary_buffer_atr_multiple": 0.05},
    {"boundary_buffer_atr_multiple": 0.15},
    {"review_horizon_bars": 5},
    {"review_horizon_bars": 7},
)


def _profile(variant):
    return ManagementProfile(
        name="breakout_follow_through", **{"review_horizon_bars": 6, **variant}
    )


def _path(closes, variant, *, volume_spike=False):
    profile = _profile(variant)
    policy = ExitPolicy(profile_overrides={profile.name.value: profile})
    state, memory = _state(), None
    results = []
    for index, close in enumerate(closes):
        context = _context(START + timedelta(minutes=5 * index), close)
        if volume_spike:
            context = replace(
                context,
                participation={"relative_volume_20": 10.0},
                direction_dynamics={"ema_20_slope": -1.0},
            )
        result = evaluate_exit(
            _thesis(),
            state,
            context,
            _risk(context, mark=close),
            policy,
            management_state=memory,
        )
        state, memory = result.next_position_state, result.next_management_state
        results.append(result)
    return results


@pytest.mark.parametrize("variant", VARIANTS)
def test_retest_volume_spike_and_age_cannot_override_intact_boundary(variant):
    results = _path(
        [101, 99.45, 100.5, 101, 100, 100.5, 100, 101], variant, volume_spike=True
    )
    assert all(result.decision.action is ExitAction.HOLD for result in results)


@pytest.mark.parametrize("variant", VARIANTS)
def test_structure_failure_exits_without_oscillator_vote_and_stays_latched(variant):
    count = _profile(variant).failure_confirmation_bars
    results = _path([98.8] * count + [102], variant)
    assert all(
        result.decision.action is ExitAction.HOLD for result in results[: count - 1]
    )
    assert results[count - 1].decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert results[count - 1].decision.action is ExitAction.REQUEST_EXIT
    assert results[-1].decision.action is ExitAction.MANAGE_PENDING_INTENT
    assert results[-1].next_position_state.latched_exit_intent_id == (
        results[count - 1].next_position_state.latched_exit_intent_id
    )


@pytest.mark.parametrize("variant", VARIANTS)
def test_news_like_hard_stop_has_no_confirmation_or_age_delay(variant):
    results = _path([94, 102], variant)
    assert results[0].decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"
    assert results[0].decision.action is ExitAction.REQUEST_EXIT
    assert results[-1].decision.action is ExitAction.MANAGE_PENDING_INTENT


@pytest.mark.parametrize(
    ("slippage_bps", "latency", "fraction"),
    [(0, 0, 1.0), (10, 0, 1.0), (0, 1, 1.0), (0, 0, 0.5), (10, 1, 0.5)],
)
def test_failed_breakout_reconciles_under_predeclared_execution_stress(
    tmp_path, slippage_bps, latency, fraction
):
    runner = _runner(tmp_path)
    runner.broker.execution_policy = replace(
        runner.broker.execution_policy,
        slippage_bps=slippage_bps,
        order_latency_bars=latency,
        max_fill_fraction=fraction,
        max_volume_participation=0.1,
    )
    _event(runner, 0, 98)
    _event(runner, 1, 98)
    _event(runner, 2, 102, volume=0)  # A rebound without liquidity cannot fill.
    assert runner.positions[SYMBOL].state.exposure is ExposureState.EXIT_PENDING
    assert runner.broker.positions[SYMBOL]["quantity"] == 10
    for index in range(3, 13):
        _event(runner, index, 102, volume=100)
    reductions = [o for o in runner.broker.orders.values() if o["role"] == "REDUCTION"]
    assert reductions[0]["order_type"] == "LIMIT"
    assert all(o["order_type"] == "MARKET" for o in reductions[1:])
    if latency:
        # A full-bar execution delay exceeds the live 15-second deadline.
        # Repeated cancellation retains exposure; the study must censor it.
        assert runner.positions[SYMBOL].state.exposure is ExposureState.EXIT_PENDING
        assert runner.broker.positions[SYMBOL]["quantity"] == 10
        assert all(o["status"] == "CANCELLED" for o in reductions[:-1])
        assert not runner.broker.trades
        return
    assert runner.positions[SYMBOL].state.exposure is ExposureState.CLOSED
    assert len(reductions) >= 2
    assert sum(f["quantity"] for f in runner.broker.fills if f["side"] == "SELL") == 10
    assert runner.broker.trades[0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert runner.broker.cash == pytest.approx(
        runner.broker.initial_capital + runner.broker.trades[0]["net_pnl"]
    )
