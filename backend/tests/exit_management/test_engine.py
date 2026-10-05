from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
    ManagementProfileSnapshot,
    ManagementState,
    PositionState,
    ProtectionState,
    ThesisHealth,
)
from backend.exit_management.thesis import bind_terminal_fill
from backend.market_context import (
    ContextQuality,
    MarketContext,
    OHLCVBar,
    SourceQuality,
)
from backend.risk_rules import HardRiskSnapshot
from backend.session_clock import SessionClock, SessionPolicy

from .test_thesis import _draft

UTC = timezone.utc


def _bar(start, close=100.0, suffix="current"):
    end = start + timedelta(minutes=5)
    return OHLCVBar(
        bar_id=f"5m:{end.isoformat()}:{suffix}",
        start=start,
        end=end,
        available_at=end,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1000.0,
        revision=suffix,
    )


def _context(start, close=100.0, *, bars=(), known_structure=()):
    primary = _bar(start, close)
    all_bars = tuple(bars) + (primary,)
    return MarketContext(
        snapshot_id=f"context:{primary.bar_id}",
        instrument_id="1",
        session_id="2026-09-21",
        decision_event_time=primary.end,
        received_at=primary.end,
        source_as_of=primary.end,
        feature_version="market-context-v1",
        primary_bar=primary,
        higher_bar=None,
        primary_bars=all_bars,
        higher_bars=(),
        primary_quality=SourceQuality(ContextQuality.VALID),
        higher_quality=SourceQuality(ContextQuality.INCOMPLETE),
        input_bar_ids=tuple(bar.bar_id for bar in all_bars),
        input_hash="fixture",
        session_vwap=99.0,
        atr=2.0,
        direction_dynamics={"ema_20_slope": 0.1},
        participation={"relative_volume_20": 1.0},
        observation_quality={},
        known_structure=tuple(known_structure),
        setup_range=None,
        raw_regime="TRENDING",
        raw_regime_features={},
        confirmed_regime="TRENDING",
        transition_candidate=None,
        transition_age=0,
    )


def _thesis():
    draft = _draft()
    draft = replace(
        draft,
        causal_anchors={
            **draft.causal_anchors,
            "volatility": {"atr": 2.0, "quality": "VALID"},
        },
        management_profile=replace(
            draft.management_profile, version="management-profiles-v1"
        ),
    )
    return bind_terminal_fill(
        draft,
        entry_vwap=100.0,
        filled_quantity=10,
        terminal_at="2026-09-21T04:20:00+00:00",
        source_fill_ids=("entry-fill",),
    )


def _state():
    return PositionState(
        position_key=_thesis().position_key,
        exposure="OPEN",
        thesis_health=ThesisHealth.VALID,
        protection=ProtectionState.ACTIVE,
        known_quantity=10,
    )


def _risk(context, mark=100.0, **changes):
    snapshot = HardRiskSnapshot(
        session=SessionClock(SessionPolicy()).snapshot(context.decision_event_time),
        position_key=_thesis().position_key,
        signed_quantity=10,
        direction="BUY",
        mark_price=mark,
        mark_time=context.decision_event_time,
        hard_stop_price=95.0,
    )
    return type(snapshot)(**{**snapshot.__dict__, **changes})


def _boundary_failure(context):
    primary = context.primary_bar
    return evidence_report(
        (
            EvidenceObservation(
                observation_id=f"boundary:{primary.bar_id}",
                family=EvidenceFamily.STRUCTURE,
                dependency_group="entry_boundary",
                direction=EvidenceDirection.OPPOSING,
                severity=EvidenceSeverity.MATERIAL,
                predicate="ENTRY_BOUNDARY_FAILURE",
                source_bar_ids=(primary.bar_id,),
            ),
        ),
        predicates={"entry_boundary_failure": True},
    )


def test_one_weak_indicator_does_not_close_an_intact_position():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC))
    evidence = evidence_report(
        (
            EvidenceObservation(
                observation_id="rsi-watch",
                family=EvidenceFamily.DYNAMICS,
                dependency_group="oscillator_cluster",
                direction=EvidenceDirection.OPPOSING,
                severity=EvidenceSeverity.WATCH,
                predicate="OSCILLATOR_WATCH",
            ),
        )
    )

    result = evaluate_exit(
        _thesis(), _state(), context, _risk(context), ExitPolicy(), evidence=evidence
    )

    assert result.decision.action is ExitAction.HOLD
    assert result.next_position_state.exposure.value == "OPEN"
    assert result.proposed_intent is None


def test_two_distinct_completed_boundary_failures_latch_breakout_exit():
    first_context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=98)
    first = evaluate_exit(
        _thesis(),
        _state(),
        first_context,
        _risk(first_context),
        ExitPolicy(),
        evidence=_boundary_failure(first_context),
    )
    second_context = _context(datetime(2026, 9, 21, 4, 25, tzinfo=UTC), close=97)
    second = evaluate_exit(
        _thesis(),
        first.next_position_state,
        second_context,
        _risk(second_context),
        ExitPolicy(),
        management_state=first.next_management_state,
        evidence=_boundary_failure(second_context),
    )

    assert first.decision.action is ExitAction.HOLD
    assert second.decision.action is ExitAction.REQUEST_EXIT
    assert second.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert second.next_position_state.thesis_health is ThesisHealth.INVALIDATED
    assert second.next_position_state.latched_exit_intent_id
    assert (
        second.proposed_intent.intent_id
        == second.next_position_state.latched_exit_intent_id
    )


def test_duplicate_bar_does_not_advance_failure_confirmation():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=98)
    first = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context),
        ExitPolicy(),
        evidence=_boundary_failure(context),
    )
    duplicate = evaluate_exit(
        _thesis(),
        first.next_position_state,
        context,
        _risk(context),
        ExitPolicy(),
        management_state=first.next_management_state,
        evidence=_boundary_failure(context),
    )

    assert duplicate.decision.primary_reason_code == "DATA_DUPLICATE_BAR"
    assert duplicate.next_management_state.failure_count == 1
    assert duplicate.next_position_state.exposure.value == "OPEN"


def test_hard_stop_preempts_normal_hold_without_bar_confirmation():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC))

    result = evaluate_exit(
        _thesis(), _state(), context, _risk(context, mark=94.0), ExitPolicy()
    )

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"
    assert result.proposed_intent is not None
    assert result.next_position_state.exposure.value == "EXIT_PENDING"


def test_favorable_causal_swing_can_only_propose_a_monotone_stop_ratchet():
    from .test_review_engine import _trail_context

    context = _trail_context()

    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=109.0),
        ExitPolicy(),
        management_state=ManagementState(confirmed_stop=95.0),
    )

    assert result.decision.action is ExitAction.TIGHTEN_STOP
    assert result.proposed_intent is not None
    assert result.proposed_intent.stop_price < 106.0
    assert result.next_management_state.confirmed_stop == 95.0
    assert result.next_management_state.requested_stop < 106.0


def test_identical_serialized_inputs_produce_identical_decision_and_state():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=100.5)
    thesis = _thesis()
    first = evaluate_exit(thesis, _state(), context, _risk(context), ExitPolicy())
    second = evaluate_exit(thesis, _state(), context, _risk(context), ExitPolicy())

    assert first.decision.to_dict() == second.decision.to_dict()
    assert first.next_position_state.to_dict() == second.next_position_state.to_dict()
    assert (
        first.next_management_state.to_dict() == second.next_management_state.to_dict()
    )


def test_fixed_objective_is_precommitted_but_runner_objective_is_only_a_review():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=110.0)
    fixed = evaluate_exit(
        _thesis(), _state(), context, _risk(context, mark=110.0), ExitPolicy()
    )
    profile_values = dict(_thesis().management_profile.values)
    profile_values["objective_mode"] = "structure_runner"
    runner_thesis = replace(
        _thesis(),
        management_profile=ManagementProfileSnapshot(
            name="breakout_follow_through",
            version="management-profiles-v1",
            structure_status="VALID",
            values=profile_values,
        ),
    )
    runner = evaluate_exit(
        runner_thesis,
        _state(),
        context,
        _risk(context, mark=110.0),
        ExitPolicy(),
    )

    assert fixed.decision.primary_reason_code == "PROFIT_FIXED_OBJECTIVE_REACHED"
    assert fixed.decision.action is ExitAction.REQUEST_EXIT
    assert runner.decision.primary_reason_code == "HOLD_OBJECTIVE_REVIEW_ZONE"
    assert runner.decision.action is ExitAction.HOLD


def test_short_boundary_failure_has_the_same_two_distinct_bar_confirmation():
    short_thesis = replace(
        _thesis(), direction="SELL", initial_stop=105.0, objective=90.0
    )
    first_context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=97.0)
    first = evaluate_exit(
        short_thesis,
        _state(),
        first_context,
        _risk(
            first_context,
            direction="SELL",
            signed_quantity=-10,
            hard_stop_price=105.0,
        ),
        ExitPolicy(),
    )
    second_context = _context(datetime(2026, 9, 21, 4, 25, tzinfo=UTC), close=97.0)
    second = evaluate_exit(
        short_thesis,
        first.next_position_state,
        second_context,
        _risk(
            second_context,
            direction="SELL",
            signed_quantity=-10,
            hard_stop_price=105.0,
        ),
        ExitPolicy(),
        management_state=first.next_management_state,
    )

    assert first.decision.action is ExitAction.HOLD
    assert second.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert second.next_position_state.thesis_health is ThesisHealth.INVALIDATED


def test_time_review_cannot_exit_on_dynamics_without_price_context_failure():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=99.0)
    evidence = evidence_report(
        (
            EvidenceObservation(
                observation_id="adverse-dynamics",
                family=EvidenceFamily.DYNAMICS,
                dependency_group="directional_dynamics",
                direction=EvidenceDirection.OPPOSING,
                severity=EvidenceSeverity.MATERIAL,
                predicate="MATERIAL_ADVERSE_DYNAMICS",
            ),
        )
    )
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context),
        ExitPolicy(),
        management_state=ManagementState(
            eligible_completed_bars=5,
            weakening_count=1,
            last_weakening_bar_end=context.primary_bar.start.isoformat(),
        ),
        evidence=evidence,
    )

    assert result.decision.action is ExitAction.HOLD
    assert result.decision.primary_reason_code == "HOLD_THESIS_WEAKENING"
