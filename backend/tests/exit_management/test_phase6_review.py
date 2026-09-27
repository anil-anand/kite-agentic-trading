"""Policy pinning, hard-risk recovery and trace regressions from pre-commit review."""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import ExitAction, ExposureState, ManagementState
from backend.exit_management.profiles import ManagementProfile, ObjectiveMode
from backend.risk_rules import HardRiskPolicy

from .test_engine import _context, _risk, _state, _thesis
from .test_review_engine import START


@pytest.mark.parametrize("age", [float("nan"), float("inf"), -1, True, "120"])
def test_invalid_mark_freshness_policy_cannot_silence_hard_stop(age):
    with pytest.raises(ValueError, match="hard-risk policy"):
        ExitPolicy(hard_risk_policy=HardRiskPolicy(mark_max_age_seconds=age))


def test_changed_profile_default_cannot_turn_pinned_runner_into_target_exit():
    thesis = _thesis()
    thesis = replace(
        thesis,
        management_profile=replace(
            thesis.management_profile,
            name="trend_continuation",
            values={
                key: value
                for key, value in thesis.management_profile.values.items()
                if key != "objective_mode"
            },
        ),
    )
    context = _context(START, close=101.0)
    first = evaluate_exit(thesis, _state(), context, _risk(context), ExitPolicy())
    target = _context(START + timedelta(minutes=5), close=110.0)
    changed = ExitPolicy(
        profile_overrides={
            "trend_continuation": ManagementProfile(
                name="trend_continuation", objective_mode=ObjectiveMode.FIXED_OBJECTIVE
            )
        }
    )
    result = evaluate_exit(
        thesis,
        first.next_state,
        target,
        _risk(target, mark=110.0),
        changed,
        management_state=first.next_management_state,
    )
    assert result.decision.action is ExitAction.HOLD
    assert result.decision.trace["policy_artifact_mismatch"] is True


def test_explicit_frozen_fixed_objective_survives_other_policy_mismatch():
    context = _context(START, close=101.0)
    first = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())
    target = _context(START + timedelta(minutes=5), close=110.0)
    result = evaluate_exit(
        _thesis(),
        first.next_state,
        target,
        _risk(target, mark=110.0),
        ExitPolicy(tick_size=0.1),
        management_state=first.next_management_state,
    )
    assert result.decision.primary_reason_code == "PROFIT_FIXED_OBJECTIVE_REACHED"


def test_hard_exit_with_disagreeing_residual_latches_and_requests_reconciliation():
    context = _context(START)
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, signed_quantity=3, mark=94.0),
        ExitPolicy(),
    )
    assert result.decision.action is ExitAction.RECONCILE_REQUIRED
    assert result.next_state.exposure is ExposureState.RECOVERY_REQUIRED
    assert result.next_state.latched_exit_intent_id
    assert result.proposed_intent.quantity is None
    assert result.proposed_intent.details["requires_reconciliation"]


def test_pending_and_escalated_exit_keep_original_reason_and_intent():
    state, management = _state(), ManagementState()
    for index in range(2):
        context = _context(START + timedelta(minutes=5 * index), close=99.0)
        result = evaluate_exit(
            _thesis(),
            state,
            context,
            _risk(context, mark=99.0),
            ExitPolicy(),
            management_state=management,
        )
        state, management = result.next_state, result.next_management_state
    original_intent = state.latched_exit_intent_id
    assert management.latched_exit_reason_code == "THESIS_BREAKOUT_FAILED"
    recovered = _context(START + timedelta(minutes=10), close=102.0)
    pending = evaluate_exit(
        _thesis(),
        state,
        recovered,
        _risk(recovered, mark=102.0),
        ExitPolicy(),
        management_state=management,
    )
    assert pending.decision.action is ExitAction.MANAGE_PENDING_INTENT
    assert pending.decision.trace["initiating_reason_code"] == "THESIS_BREAKOUT_FAILED"
    escalation = evaluate_exit(
        _thesis(),
        pending.next_state,
        recovered,
        _risk(recovered, mark=102.0, daily_loss_latched=True),
        ExitPolicy(),
        management_state=pending.next_management_state,
    )
    assert escalation.proposed_intent.intent_id == original_intent
    assert escalation.proposed_intent.urgency == "CRITICAL"
    assert escalation.decision.primary_reason_code == "RISK_DAILY_LOSS"
    assert escalation.decision.urgency == "CRITICAL"
    assert (
        escalation.decision.trace["initiating_reason_code"] == "THESIS_BREAKOUT_FAILED"
    )


def test_legacy_pending_escalation_does_not_invent_initiating_reason():
    context = _context(START)
    state = replace(
        _state(), exposure=ExposureState.EXIT_PENDING, latched_exit_intent_id="existing"
    )
    result = evaluate_exit(
        _thesis(), state, context, _risk(context, daily_loss_latched=True), ExitPolicy()
    )
    assert result.next_management_state.latched_exit_reason_code is None
    assert result.decision.trace["initiating_reason_code"] is None
    assert result.decision.urgency == "CRITICAL"
    pending = evaluate_exit(
        _thesis(),
        result.next_state,
        context,
        _risk(context),
        ExitPolicy(),
        management_state=result.next_management_state,
    )
    assert pending.decision.urgency == "CRITICAL"


def test_profit_trace_has_explicit_r_price_units_and_nullable_giveback_fraction():
    context = _context(START)
    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        _risk(context, mark=99.0),
        ExitPolicy(),
        management_state=ManagementState(confirmed_stop=95.0),
    )
    profit = result.decision.to_dict()["trace"]["profit_context"]
    assert profit["initial_r_per_share"] == 5.0
    assert profit["unrealized_r"] == -0.2
    assert profit["observed_mae_price_distance"] == 1.0
    assert profit["giveback_fraction"] is None
    assert profit["confirmed_stop_gross_r"] == -1.0
