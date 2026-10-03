"""Phase-6 policy must derive authority from causal market and broker facts."""

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
from backend.exit_management.models import ExitAction, ManagementState, ThesisHealth

from .test_engine import _context, _risk, _state, _thesis
from .test_review_engine import START


@pytest.mark.parametrize("source", ["missing", "unrelated", "future"])
def test_supplied_evidence_cannot_invent_a_boundary_failure(source):
    state, management = _state(), ManagementState()
    for index in range(2):
        context = _context(START + timedelta(minutes=5 * index), close=101.0)
        known_at = None
        if source == "future":
            known_at = (context.decision_event_time + timedelta(minutes=5)).isoformat()
        supplied = evidence_report(
            (
                EvidenceObservation(
                    observation_id=f"untrusted-failure-{index}",
                    family=EvidenceFamily.STRUCTURE,
                    dependency_group="entry_boundary",
                    direction=EvidenceDirection.UNKNOWN,
                    severity=EvidenceSeverity.UNKNOWN,
                    predicate="ENTRY_BOUNDARY_UNAVAILABLE",
                    source_bar_ids=("different-instrument-and-bar",)
                    if source == "unrelated"
                    else (),
                    known_at=known_at,
                    quality="UNAVAILABLE",
                ),
            ),
            predicates={"entry_boundary_failure": True},
        )
        result = evaluate_exit(
            _thesis(),
            state,
            context,
            _risk(context, mark=101.0),
            ExitPolicy(),
            management_state=management,
            evidence=supplied,
        )
        state, management = result.next_position_state, result.next_management_state
        assert result.decision.action is ExitAction.HOLD
        assert management.failure_count == 0
        assert state.thesis_health is ThesisHealth.VALID


@pytest.mark.parametrize("future_diagnostic", [False, True])
def test_supplied_evidence_cannot_suppress_a_real_confirmed_failure(future_diagnostic):
    state, management = _state(), ManagementState()
    for index in range(2):
        context = _context(START + timedelta(minutes=5 * index), close=99.0)
        observation = EvidenceObservation(
            observation_id=f"asserted-reclaim-{index}",
            family=EvidenceFamily.STRUCTURE,
            dependency_group="entry_boundary",
            direction=EvidenceDirection.SUPPORTING,
            severity=EvidenceSeverity.MATERIAL,
            predicate="ENTRY_BOUNDARY_RECOVERY",
            source_bar_ids=(context.primary_bar.bar_id,),
            known_at=(
                context.decision_event_time
                + timedelta(minutes=5 if future_diagnostic else 0)
            ).isoformat(),
        )
        supplied = evidence_report(
            (observation,),
            predicates={
                "entry_boundary_failure": False,
                "entry_boundary_recovery": True,
                "required_context_usable": False,
            },
        )
        result = evaluate_exit(
            _thesis(),
            state,
            context,
            _risk(context, mark=99.0),
            ExitPolicy(),
            management_state=management,
            evidence=supplied,
        )
        state, management = result.next_position_state, result.next_management_state

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert state.thesis_health is ThesisHealth.INVALIDATED


def test_thesis_failure_episode_resets_inside_buffer_before_another_failure():
    state, management = _state(), ManagementState()
    # The frozen breakout high is 99.5 with a 0.2 noise buffer. A close within
    # that band is not a second failure, and the next failure starts at one.
    for index, close in enumerate([99.0, 99.5, 99.0]):
        context = _context(START + timedelta(minutes=5 * index), close=close)
        result = evaluate_exit(
            _thesis(),
            state,
            context,
            _risk(context, mark=close),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_position_state, result.next_management_state
        assert result.decision.action is ExitAction.HOLD
    assert management.failure_count == 1


def test_unknown_data_between_failure_bars_does_not_count_as_confirmation():
    state = _state()
    first_context = _context(START, close=99.0)
    first = evaluate_exit(
        _thesis(), state, first_context, _risk(first_context), ExitPolicy()
    )
    missing_context = _context(START + timedelta(minutes=5), close=99.0)
    missing_context = replace(missing_context, primary_bar=None, primary_bars=())
    missing = evaluate_exit(
        _thesis(),
        first.next_position_state,
        missing_context,
        _risk(missing_context),
        ExitPolicy(),
        management_state=first.next_management_state,
    )
    final_context = _context(START + timedelta(minutes=10), close=99.0)
    final = evaluate_exit(
        _thesis(),
        missing.next_position_state,
        final_context,
        _risk(final_context),
        ExitPolicy(),
        management_state=missing.next_management_state,
    )
    assert final.decision.action is ExitAction.HOLD
    assert final.next_management_state.failure_count == 1


def _mirrored_trail_context(side):
    from .test_review_engine import _trail_context

    context = _trail_context()
    if side == "BUY":
        return context
    bars = tuple(
        replace(
            bar,
            open=200.0 - bar.open,
            high=200.0 - bar.low,
            low=200.0 - bar.high,
            close=200.0 - bar.close,
        )
        for bar in context.primary_bars
    )
    return replace(
        context,
        primary_bar=bars[-1],
        primary_bars=bars,
        known_structure=tuple(
            replace(level, kind="SWING_HIGH", price=200.0 - level.price)
            for level in context.known_structure
        ),
        session_vwap=101.0,
        direction_dynamics={"ema_20_slope": -0.1},
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_structural_trail_cannot_loosen_stronger_risk_snapshot_stop(side):
    from .test_review_scenarios import _scenario_thesis

    context = _mirrored_trail_context(side)
    result = evaluate_exit(
        _scenario_thesis(side, "trend_continuation"),
        _state(),
        context,
        _risk(
            context,
            mark=109.0 if side == "BUY" else 91.0,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=107.0 if side == "BUY" else 93.0,
        ),
        ExitPolicy(),
        management_state=ManagementState(
            confirmed_stop=95.0 if side == "BUY" else 105.0
        ),
    )
    assert result.decision.action is not ExitAction.TIGHTEN_STOP


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_crossed_confirmed_stop_preempts_weaker_risk_snapshot_floor(side):
    from .test_review_scenarios import _scenario_thesis

    context = _mirrored_trail_context(side)
    result = evaluate_exit(
        _scenario_thesis(side, "trend_continuation"),
        _state(),
        context,
        _risk(
            context,
            mark=106.0 if side == "BUY" else 94.0,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=95.0 if side == "BUY" else 105.0,
        ),
        ExitPolicy(),
        management_state=ManagementState(
            confirmed_stop=107.0 if side == "BUY" else 93.0
        ),
    )
    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_unconfirmed_requested_stop_does_not_become_hard_price_floor(side):
    from backend.exit_management.models import ProtectionState

    from .test_review_scenarios import _scenario_thesis

    context = _mirrored_trail_context(side)
    result = evaluate_exit(
        _scenario_thesis(side, "trend_continuation"),
        replace(_state(), protection=ProtectionState.UPDATE_PENDING),
        context,
        _risk(
            context,
            mark=106.0 if side == "BUY" else 94.0,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=95.0 if side == "BUY" else 105.0,
        ),
        ExitPolicy(),
        management_state=ManagementState(
            confirmed_stop=95.0 if side == "BUY" else 105.0,
            requested_stop=107.0 if side == "BUY" else 93.0,
        ),
    )
    assert result.decision.action is ExitAction.HOLD
    assert result.next_management_state.requested_stop is not None


def test_newly_known_base_during_retracement_is_not_new_favorable_progress():
    from backend.exit_management.models import DevelopmentPhase

    from .test_review_engine import _trail_context
    from .test_review_scenarios import _scenario_thesis

    thesis = _scenario_thesis("BUY", "trend_continuation")
    first_context = _context(START, close=115.0)
    first = evaluate_exit(
        thesis,
        _state(),
        first_context,
        _risk(first_context, mark=115.0),
        ExitPolicy(),
    )
    # A later 106 swing becomes known while the completed close has fallen
    # from 115 (3R) to 109 (1.8R). The base may justify protection, but does not
    # make the falling close a new directional high or reset the progress age.
    later_context = _trail_context()
    later = evaluate_exit(
        thesis,
        first.next_position_state,
        later_context,
        _risk(later_context, mark=109.0),
        ExitPolicy(),
        management_state=first.next_management_state,
    )

    assert later.next_management_state.completed_mfe_r == 3.0
    assert (
        later.next_management_state.completed_mfe_at
        == first_context.primary_bar.end.isoformat()
    )
    assert later.next_management_state.last_close_progress_bar_count == 1
    assert later.next_position_state.development is not DevelopmentPhase.FAVORABLE
    assert (
        later.next_management_state.last_favorable_progress_bar_id
        != later_context.primary_bar.bar_id
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_hard_stop_observation_records_adverse_excursion_before_exit(side):
    from .test_review_scenarios import _scenario_thesis

    context = _context(START)
    result = evaluate_exit(
        _scenario_thesis(side, "trend_continuation"),
        _state(),
        None,
        _risk(
            context,
            mark=94.0 if side == "BUY" else 106.0,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=95.0 if side == "BUY" else 105.0,
        ),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.next_management_state.observed_mae_r == pytest.approx(1.2)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_pending_exit_still_observes_excursions_of_unfilled_residual(side):
    from backend.exit_management.models import ExposureState

    from .test_review_scenarios import _scenario_thesis

    context = _context(START)
    state = replace(
        _state(),
        exposure=ExposureState.EXIT_PENDING,
        thesis_health=ThesisHealth.INVALIDATED,
        latched_exit_intent_id="latched-exit-awaiting-fill",
    )
    result = evaluate_exit(
        _scenario_thesis(side, "trend_continuation"),
        state,
        None,
        _risk(
            context,
            mark=108.0 if side == "BUY" else 92.0,
            direction=side,
            signed_quantity=10 if side == "BUY" else -10,
            hard_stop_price=95.0 if side == "BUY" else 105.0,
        ),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.MANAGE_PENDING_INTENT
    assert (
        result.next_position_state.latched_exit_intent_id
        == state.latched_exit_intent_id
    )
    assert result.next_management_state.observed_mfe_r == pytest.approx(1.6)
    assert result.next_management_state.eligible_completed_bars == 0


def test_cached_higher_support_expires_at_authoritative_risk_event_time():
    from backend.market_context import ContextPolicy, ContextQuality, SourceQuality
    from backend.session_clock import SessionClock, SessionPolicy

    from .test_evidence import _frozen_thesis, _structure_context

    thesis = _frozen_thesis()
    context = _structure_context()
    higher = []
    for offset in range(0, 24, 3):
        group = context.primary_bars[offset : offset + 3]
        higher.append(
            replace(
                group[-1],
                bar_id=f"15m:{group[-1].end.isoformat()}",
                start=group[0].start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                volume=sum(bar.volume for bar in group),
            )
        )
    context = replace(
        context,
        higher_bar=higher[-1],
        higher_bars=tuple(higher),
        higher_quality=SourceQuality(ContextQuality.VALID),
        context_policy=ContextPolicy(max_higher_age_seconds=300),
    )
    fresh = evaluate_exit(thesis, _state(), context, _risk(context), ExitPolicy())
    assert fresh.evidence.predicates["higher_timeframe_support"] is True

    now = context.decision_event_time + timedelta(seconds=1)
    delayed_risk = replace(
        _risk(context), session=SessionClock(SessionPolicy()).snapshot(now)
    )
    delayed = evaluate_exit(thesis, _state(), context, delayed_risk, ExitPolicy())

    assert delayed.evidence.predicates["higher_timeframe_support"] is None
    assert delayed.evidence.predicates["entry_boundary_failure"] is False
