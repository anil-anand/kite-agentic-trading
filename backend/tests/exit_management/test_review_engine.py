"""Adversarial phase-6 review regressions for policy/execution boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
    ExitIntentType,
    ExposureState,
    ManagementState,
    ProtectionState,
    ThesisHealth,
)
from backend.exit_management.profiles import ManagementProfile, ManagementProfileName
from backend.market_context import KnownLevel
from backend.session_clock import SessionClock, SessionPolicy

from .test_engine import _bar, _context, _risk, _state, _thesis

UTC = timezone.utc
START = datetime(2026, 9, 21, 4, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    "exposure",
    [ExposureState.ENTRY_PENDING, ExposureState.FLAT_PENDING_RECONCILIATION],
)
def test_account_flatten_with_zero_position_quantity_does_not_crash(exposure):
    context = _context(START)
    state = replace(_state(), exposure=exposure, known_quantity=0)

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, signed_quantity=0, daily_loss_latched=True),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.proposed_intent.intent_type is ExitIntentType.FLATTEN
    assert result.proposed_intent.quantity is None
    assert result.next_position_state.known_quantity == 0


@pytest.mark.parametrize(
    "exposure", [ExposureState.CLOSED, ExposureState.ENTRY_ABORTED]
)
def test_terminal_position_never_emits_a_normal_reduction(exposure):
    context = _context(START, close=110.0)
    state = replace(
        _state(),
        exposure=exposure,
        known_quantity=0,
        protection=ProtectionState.NONE_FLAT,
        had_fills=exposure is ExposureState.CLOSED,
    )

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, mark=110.0, signed_quantity=0),
        ExitPolicy(),
    )

    assert result.proposed_intent is None
    assert result.next_position_state == state


def test_operator_close_on_confirmed_closed_epoch_cannot_start_another_exit():
    context = _context(START)
    state = replace(
        _state(),
        exposure=ExposureState.CLOSED,
        known_quantity=0,
        protection=ProtectionState.NONE_FLAT,
    )

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, signed_quantity=0, operator_close_requested=True),
        ExitPolicy(),
    )

    assert result.proposed_intent is None
    assert result.next_position_state == state


def test_flat_pending_position_keeps_reconciliation_before_normal_target_exit():
    context = _context(START, close=110.0)
    state = replace(
        _state(), exposure=ExposureState.FLAT_PENDING_RECONCILIATION, known_quantity=0
    )

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, mark=110.0, signed_quantity=0),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.RECONCILE_REQUIRED
    assert result.proposed_intent.intent_type is ExitIntentType.RECONCILE


def test_stale_quote_cannot_execute_fixed_objective_or_update_observed_mfe():
    context = _context(START, close=100.0)
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=110.0, mark_time=START - timedelta(minutes=10)),
        ExitPolicy(),
    )

    assert result.decision.action is not ExitAction.REQUEST_EXIT
    assert result.next_management_state.observed_mfe_r in (None, 0.0)


@pytest.mark.parametrize("mismatch", ["thesis", "risk", "instrument", "quantity"])
def test_disagreeing_position_inputs_request_reconciliation(mismatch):
    context = _context(START, close=110.0)
    thesis = _thesis()
    risk = _risk(context, mark=110.0)
    if mismatch == "thesis":
        thesis = replace(thesis, position_key="another-position-epoch")
    elif mismatch == "risk":
        risk = replace(risk, position_key="another-position-epoch")
    elif mismatch == "instrument":
        context = replace(context, instrument_id="another-instrument")
    else:
        risk = replace(risk, signed_quantity=3)

    result = evaluate_exit(thesis, _state(), context, risk, ExitPolicy())

    assert result.decision.action is ExitAction.RECONCILE_REQUIRED
    assert result.proposed_intent.intent_type is ExitIntentType.RECONCILE


def test_another_positions_stop_breach_cannot_close_this_position():
    context = _context(START, close=100.0)

    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=94.0, position_key="another-position-epoch"),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.RECONCILE_REQUIRED
    assert result.proposed_intent.intent_type is ExitIntentType.RECONCILE
    assert result.next_position_state.latched_exit_intent_id is None


@pytest.mark.parametrize("older", [False, True])
def test_revised_or_out_of_order_bar_cannot_advance_policy_memory(older):
    first_context = _context(START + timedelta(minutes=5), close=100.0)
    first = evaluate_exit(
        _thesis(), _state(), first_context, _risk(first_context), ExitPolicy()
    )
    if older:
        repeated_context = _context(START, close=100.0)
    else:
        revised_bar = replace(
            first_context.primary_bar,
            bar_id="same-time-revised-bar",
            revision="later-revision",
        )
        repeated_context = replace(
            first_context,
            primary_bar=revised_bar,
            primary_bars=(revised_bar,),
            snapshot_id="later-revision-context",
        )

    result = evaluate_exit(
        _thesis(),
        first.next_position_state,
        repeated_context,
        _risk(repeated_context),
        ExitPolicy(),
        management_state=first.next_management_state,
    )

    assert result.next_management_state == first.next_management_state
    assert result.next_position_state == first.next_position_state
    assert result.proposed_intent is None


def test_reclaimed_entry_boundary_does_not_heal_current_structure_failure():
    context = _context(START, close=102.0)
    observation = EvidenceObservation(
        observation_id="lost-post-entry-base",
        family=EvidenceFamily.STRUCTURE,
        dependency_group="post_entry_structure",
        direction=EvidenceDirection.OPPOSING,
        severity=EvidenceSeverity.MATERIAL,
        predicate="FAVORABLE_STRUCTURE_FAILURE",
        source_bar_ids=(context.primary_bar.bar_id,),
        known_at=context.primary_bar.available_at.isoformat(),
        details={"level_id": "base-1"},
    )
    report = evidence_report(
        (observation,),
        predicates={"entry_boundary_failure": False, "entry_boundary_recovery": True},
    )
    state = replace(_state(), thesis_health=ThesisHealth.WEAKENING)
    management = ManagementState(
        recovery_count=1,
        last_recovery_bar_end=START.isoformat(),
    )

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, mark=102.0),
        ExitPolicy(),
        management_state=management,
        evidence=report,
    )

    assert result.next_position_state.thesis_health is ThesisHealth.WEAKENING


def _trail_context():
    primary_start = START + timedelta(minutes=45)
    bars = tuple(
        _bar(
            primary_start - timedelta(minutes=5 * offset),
            close=106.5 if offset == 3 else 108.0,
        )
        for offset in range(16, 0, -1)
    )
    return _context(
        primary_start,
        close=109.0,
        bars=bars,
        known_structure=(
            KnownLevel(
                level_id="confirmed-post-entry-swing",
                kind="SWING_LOW",
                price=106.0,
                formed_at=primary_start - timedelta(minutes=15),
                known_at=primary_start,
                source_bar_ids=tuple(bar.bar_id for bar in bars[-5:]),
            ),
        ),
    )


def test_already_crossed_structural_stop_is_not_submitted_from_old_close():
    context = _trail_context()
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=105.0),
        ExitPolicy(),
        management_state=ManagementState(confirmed_stop=95.0),
    )

    assert result.decision.action is not ExitAction.TIGHTEN_STOP
    assert result.proposed_intent is None
    assert result.next_management_state.confirmed_stop == 95.0
    assert result.evidence.predicates["favorable_structure_id"] == (
        "confirmed-post-entry-swing"
    )


def test_pending_stop_modification_cannot_launch_another_stop_command():
    context = _trail_context()
    state = replace(_state(), protection=ProtectionState.UPDATE_PENDING)
    management = ManagementState(confirmed_stop=95.0, requested_stop=104.0)

    result = evaluate_exit(
        _thesis(),
        state,
        context,
        _risk(context, mark=109.0),
        ExitPolicy(),
        management_state=management,
    )

    assert result.decision.action is not ExitAction.TIGHTEN_STOP
    assert result.proposed_intent is None
    assert result.next_management_state.requested_stop == 104.0
    assert result.evidence.predicates["favorable_structure_id"] == (
        "confirmed-post-entry-swing"
    )


def test_structural_stop_respects_separately_pinned_trailing_buffer_override():
    context = _trail_context()
    profile = ManagementProfile(
        name=ManagementProfileName.BREAKOUT_FOLLOW_THROUGH,
        structural_trail_buffer_atr_multiple=0.5,
    )
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=109.0),
        ExitPolicy(profile_overrides={profile.name.value: profile}),
        management_state=ManagementState(confirmed_stop=95.0),
    )

    # Every source bar has at least one point of true range, so a half-ATR
    # structural trail must leave at least half a point below the 106 swing.
    assert result.decision.action is ExitAction.TIGHTEN_STOP
    assert result.proposed_intent.stop_price <= 105.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"breakout_follow_through": object()},
        {
            "breakout_follow_through": ManagementProfile(
                name=ManagementProfileName.TREND_CONTINUATION
            )
        },
    ],
)
def test_invalid_profile_overrides_are_rejected_before_event_evaluation(overrides):
    with pytest.raises((TypeError, ValueError)):
        ExitPolicy(profile_overrides=overrides)


def test_newer_adverse_observed_mark_preempts_stale_target_quote():
    context = _context(START)
    event_time = context.decision_event_time
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=110.0, mark_time=event_time - timedelta(seconds=1)),
        ExitPolicy(),
        observed_mark_price=94.0,
        observed_mark_time=event_time,
    )

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"


def test_unknown_broker_state_retains_operator_close_obligation():
    context = _context(START)
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, broker_state_known=False, operator_close_requested=True),
        ExitPolicy(),
    )

    assert result.decision.action is ExitAction.RECONCILE_REQUIRED
    assert result.next_position_state.exposure is ExposureState.RECOVERY_REQUIRED
    assert result.next_position_state.latched_exit_intent_id is not None


def test_hard_risk_event_uses_authoritative_time_when_context_is_older():
    context = _context(START)
    now = context.decision_event_time + timedelta(minutes=1)
    risk = replace(
        _risk(context, operator_close_requested=True),
        session=SessionClock(SessionPolicy()).snapshot(now),
    )

    result = evaluate_exit(_thesis(), _state(), context, risk, ExitPolicy())

    assert result.decision.occurred_at == now.isoformat()


def test_supplied_predicates_cannot_override_unknown_required_entry_volatility():
    thesis = _thesis()
    thesis = replace(
        thesis, causal_anchors={**thesis.causal_anchors, "volatility": {"atr": None}}
    )
    state, management = _state(), ManagementState()
    results = []
    for index in range(2):
        context = _context(START + timedelta(minutes=5 * index), close=99.0)
        report = evidence_report(
            (
                EvidenceObservation(
                    observation_id=f"asserted-failure-{index}",
                    family=EvidenceFamily.STRUCTURE,
                    dependency_group="entry_boundary",
                    direction=EvidenceDirection.OPPOSING,
                    severity=EvidenceSeverity.MATERIAL,
                    predicate="ENTRY_BOUNDARY_FAILURE",
                    source_bar_ids=(context.primary_bar.bar_id,),
                    known_at=context.primary_bar.available_at.isoformat(),
                ),
            ),
            predicates={
                "entry_boundary_failure": True,
                "required_context_usable": True,
            },
        )
        result = evaluate_exit(
            thesis,
            state,
            context,
            _risk(context, mark=99.0),
            ExitPolicy(),
            management_state=management,
            evidence=report,
        )
        state, management = result.next_position_state, result.next_management_state
        results.append(result)

    assert all(result.decision.action is ExitAction.HOLD for result in results)
    assert management.failure_count == 0
