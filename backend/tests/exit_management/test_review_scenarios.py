"""Mirrored, causal trader-behaviour scenarios added during phase-6 review."""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.evidence import (
    EvidenceDirection,
    EvidenceFamily,
    EvidenceObservation,
    EvidenceSeverity,
    evidence_report,
)
from backend.exit_management.models import (
    ExitAction,
    ExposureState,
    ManagementProfileSnapshot,
    ManagementState,
    ThesisHealth,
)
from backend.market_context import ContextQuality, SourceQuality

from .test_engine import _bar, _context, _risk, _state, _thesis
from .test_review_engine import START, _trail_context


def _scenario_thesis(side, profile="breakout_follow_through", *, requires_vwap=False):
    thesis = _thesis()
    values = dict(thesis.management_profile.values)
    values["objective_mode"] = (
        "structure_runner" if profile == "trend_continuation" else "fixed_objective"
    )
    values["requires_vwap_acceptance"] = requires_vwap
    if profile == "trend_continuation":
        values["entry_boundary"] = {
            "price": 95.0 if side == "BUY" else 105.0,
            "level_id": "defended-entry-swing",
        }
    elif side == "SELL":
        values["entry_boundary"] = {
            "high": 104.0,
            "low": 100.5,
            "level_id": "pre-trigger-range",
        }
    return replace(
        thesis,
        direction=side,
        initial_stop=95.0 if side == "BUY" else 105.0,
        objective=110.0 if side == "BUY" else 90.0,
        management_profile=ManagementProfileSnapshot(
            name=profile,
            version="management-profiles-v1",
            structure_status="VALID",
            values=values,
        ),
    )


def _run_prices(
    side,
    prices,
    *,
    profile="breakout_follow_through",
    changes=None,
    requires_vwap=False,
):
    thesis = _scenario_thesis(side, profile, requires_vwap=requires_vwap)
    state = _state()
    management = ManagementState()
    # The documented rolling baseline may span a session boundary. Seed twenty
    # actual prior-session bars so participation scenarios use known evidence.
    prior_start = START.replace(day=18, hour=8, minute=20)
    bars = [_bar(prior_start + timedelta(minutes=5 * i)) for i in range(20)]
    results = []
    for index, long_price in enumerate(prices):
        price = long_price if side == "BUY" else 200.0 - long_price
        context = _context(
            START + timedelta(minutes=5 * index), close=price, bars=tuple(bars)
        )
        context = replace(
            context,
            session_vwap=99.0 if side == "BUY" else 101.0,
            direction_dynamics={"ema_20_slope": 0.1 if side == "BUY" else -0.1},
        )
        if changes is not None:
            context = replace(context, **changes(index, context))
        risk = _risk(
            context,
            mark=price,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=95.0 if side == "BUY" else 105.0,
        )
        result = evaluate_exit(
            thesis, state, context, risk, ExitPolicy(), management_state=management
        )
        results.append(result)
        state = result.next_position_state
        management = result.next_management_state
        bars.append(context.primary_bar)
    return results


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_breakout_retest_and_reclaim_remain_patient(side):
    results = _run_prices(side, [101.0, 100.2, 99.55, 100.4])

    assert all(result.decision.action is ExitAction.HOLD for result in results)
    assert results[-1].next_position_state.thesis_health is ThesisHealth.VALID


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_two_buffered_breakout_failures_exit_without_oscillator_confirmation(side):
    results = _run_prices(side, [99.0, 98.8])

    assert results[0].decision.action is ExitAction.HOLD
    assert results[1].decision.action is ExitAction.REQUEST_EXIT
    assert results[1].decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert results[1].next_position_state.thesis_health is ThesisHealth.INVALIDATED


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_strong_trend_and_low_volume_temporary_pullback_do_not_exit(side):
    results = _run_prices(
        side,
        [102.0, 104.0, 103.5, 103.0],
        profile="trend_continuation",
        changes=lambda index, context: {
            "participation": {"relative_volume_20": 0.6 if index >= 2 else 1.0},
            "direction_dynamics": {
                "ema_20_slope": (-0.1 if side == "BUY" else 0.1)
                if index >= 2
                else (0.1 if side == "BUY" else -0.1)
            },
        },
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_large_giveback_with_defended_entry_structure_is_not_automatic_exit(side):
    results = _run_prices(
        side, [108.0, 115.0, 110.0, 106.0], profile="trend_continuation"
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results)
    assert results[-1].next_management_state.completed_mfe_r == 3.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_volume_climax_with_favorable_price_response_is_not_exhaustion(side):
    results = _run_prices(
        side,
        [100.5, 101.0, 103.0],
        changes=lambda index, context: {"participation": {"relative_volume_20": 8.0}},
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("regime", ["RANGING", "TRENDING", "VOLATILE"])
def test_regime_transition_alone_cannot_invalidate_defended_trade(side, regime):
    results = _run_prices(
        side,
        [100.4, 100.3],
        changes=lambda index, context: {
            "raw_regime": regime,
            "confirmed_regime": regime,
            "transition_candidate": regime,
            "transition_age": 2,
        },
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_news_like_adverse_price_bypasses_normal_bar_confirmation(side):
    results = _run_prices(side, [94.0])

    assert results[0].decision.action is ExitAction.REQUEST_EXIT
    assert results[0].decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_fixed_objective_executes_frozen_full_exit(side):
    results = _run_prices(side, [110.0])

    assert results[0].decision.action is ExitAction.REQUEST_EXIT
    assert results[0].decision.primary_reason_code == "PROFIT_FIXED_OBJECTIVE_REACHED"
    assert results[0].proposed_intent.quantity == 10


def test_missing_bar_breaks_failure_confirmation_without_healing_thesis():
    thesis = _scenario_thesis("BUY")
    state = _state()
    management = ManagementState()
    results = []
    for index in range(3):
        context = _context(START + timedelta(minutes=5 * index), close=99.0)
        if index == 1:
            context = replace(
                context, primary_quality=SourceQuality(ContextQuality.GAP)
            )
        result = evaluate_exit(
            thesis,
            state,
            context,
            _risk(context, mark=99.0),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_position_state, result.next_management_state
        results.append(result)

    assert all(result.decision.action is ExitAction.HOLD for result in results)
    assert results[-1].next_management_state.failure_count == 1


def test_rejected_exit_does_not_resume_management_when_price_bounces():
    context = _context(START, close=103.0)
    state = replace(
        _state(),
        exposure=ExposureState.RECOVERY_REQUIRED,
        recovery_from=ExposureState.EXIT_PENDING,
        thesis_health=ThesisHealth.INVALIDATED,
        latched_exit_intent_id="original-exit-obligation",
    )

    result = evaluate_exit(
        _scenario_thesis("BUY"),
        state,
        context,
        _risk(context, mark=103.0),
        ExitPolicy(),
    )

    assert result.decision.action in {
        ExitAction.RECONCILE_REQUIRED,
        ExitAction.MANAGE_PENDING_INTENT,
    }
    assert (
        result.next_position_state.latched_exit_intent_id == "original-exit-obligation"
    )
    assert result.next_position_state.thesis_health is ThesisHealth.INVALIDATED


def test_old_healthy_base_with_only_oscillator_weakness_cannot_exit_on_time():
    thesis = _scenario_thesis("BUY", "trend_continuation")
    context = _context(START, close=100.5)
    report = evidence_report(
        (
            EvidenceObservation(
                observation_id="oscillator-turn",
                family=EvidenceFamily.DYNAMICS,
                dependency_group="oscillator_cluster",
                direction=EvidenceDirection.OPPOSING,
                severity=EvidenceSeverity.MATERIAL,
                predicate="OSCILLATOR_TURN",
                source_bar_ids=(context.primary_bar.bar_id,),
                known_at=context.primary_bar.available_at.isoformat(),
            ),
        ),
        predicates={"entry_boundary_failure": False, "entry_boundary_recovery": True},
    )

    result = evaluate_exit(
        thesis,
        _state(),
        context,
        _risk(context, mark=100.5),
        ExitPolicy(),
        management_state=ManagementState(
            eligible_completed_bars=11,
            weakening_count=1,
            last_weakening_bar_end=START.isoformat(),
            completed_mfe_r=0.2,
        ),
        evidence=report,
    )

    assert result.decision.action is ExitAction.HOLD


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_vwap_rejection_with_no_progress_reaches_confirmed_time_exit(side):
    vwap = 101.0 if side == "BUY" else 99.0
    results = _run_prices(
        side,
        [99.5] * 12,
        profile="trend_continuation",
        requires_vwap=True,
        changes=lambda index, context: {"session_vwap": vwap},
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results[:-1])
    assert results[-1].decision.action is ExitAction.REQUEST_EXIT
    assert results[-1].decision.primary_reason_code == "TIME_NO_PROGRESS_CONFIRMED"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_new_completed_close_progress_prevents_stagnation_exit_without_a_swing(side):
    vwap = 103.0 if side == "BUY" else 97.0
    results = _run_prices(
        side,
        [100.0 + 0.25 * index for index in range(12)],
        profile="trend_continuation",
        requires_vwap=True,
        changes=lambda index, context: {"session_vwap": vwap},
    )

    assert all(result.decision.action is ExitAction.HOLD for result in results)


def test_profitable_runner_exits_after_confirmed_lost_structure_and_vwap_rejection():
    thesis = _scenario_thesis("BUY", "trend_continuation", requires_vwap=True)
    template = _trail_context()
    bars = list(template.primary_bars[:-1])
    state, management = _state(), ManagementState()
    results = []
    for index, close in enumerate([115.0, 105.0, 104.5]):
        context = _context(
            template.primary_bar.start + timedelta(minutes=5 * index),
            close=close,
            bars=tuple(bars),
            known_structure=template.known_structure,
        )
        context = replace(context, session_vwap=107.0)
        result = evaluate_exit(
            thesis,
            state,
            context,
            _risk(context, mark=close),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_position_state, result.next_management_state
        results.append(result)
        bars.append(context.primary_bar)

    assert results[0].next_management_state.last_favorable_progress_bar_id
    assert results[0].next_management_state.completed_mfe_r == 3.0
    assert results[1].decision.action is ExitAction.HOLD
    assert results[2].decision.action is ExitAction.REQUEST_EXIT
    assert results[2].decision.primary_reason_code == "PROFIT_REVERSAL_CONFIRMED"


def test_unknown_participation_cannot_heal_an_existing_weakening_state():
    thesis = _scenario_thesis("BUY")
    state = replace(_state(), thesis_health=ThesisHealth.WEAKENING)
    management = ManagementState()
    for index in range(2):
        context = _context(START + timedelta(minutes=5 * index), close=100.5)
        context = replace(context, participation={"relative_volume_20": None})
        result = evaluate_exit(
            thesis,
            state,
            context,
            _risk(context, mark=100.5),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_position_state, result.next_management_state
        assert result.decision.action is ExitAction.HOLD

    assert state.thesis_health is ThesisHealth.WEAKENING


def test_two_fully_known_supportive_bars_restore_weakening_to_valid():
    thesis = _scenario_thesis("BUY")
    state = replace(_state(), thesis_health=ThesisHealth.WEAKENING)
    management = ManagementState()
    first_start = START.replace(hour=3, minute=45)
    bars = [
        _bar(first_start + timedelta(minutes=5 * i), close=100.5) for i in range(20)
    ]
    for index in range(2):
        context = _context(
            first_start + timedelta(minutes=5 * (20 + index)),
            close=100.5,
            bars=tuple(bars),
        )
        result = evaluate_exit(
            thesis,
            state,
            context,
            _risk(context, mark=100.5),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_position_state, result.next_management_state
        bars.append(context.primary_bar)
        assert result.decision.action is ExitAction.HOLD
        if index == 0:
            assert state.thesis_health is ThesisHealth.WEAKENING

    assert state.thesis_health is ThesisHealth.VALID
