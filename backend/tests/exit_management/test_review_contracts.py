"""Adversarial contracts for persistent phase-6 management memory."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import (
    ExitAction,
    ExitDecision,
    ManagementState,
    ProposedIntent,
)

from .test_engine import _context, _risk, _state, _thesis


@pytest.mark.parametrize(
    "field",
    ("observed_mfe_r", "observed_mae_r", "completed_mfe_r", "completed_mae_r"),
)
def test_excursions_use_nonnegative_magnitudes(field):
    with pytest.raises(ValueError, match="nonnegative"):
        ManagementState(**{field: -0.25})
    assert getattr(ManagementState(**{field: 0.25}), field) == 0.25


@pytest.mark.parametrize("field", ("confirmed_stop", "requested_stop"))
@pytest.mark.parametrize("value", (0, -1, float("nan"), float("inf"), True))
def test_checkpoint_cannot_restore_unusable_protection_prices(field, value):
    with pytest.raises(ValueError):
        ManagementState.from_dict({field: value})


@pytest.mark.parametrize(
    "field",
    (
        "last_processed_primary_bar_end",
        "last_failure_bar_end",
        "last_local_failure_bar_end",
        "last_stagnation_bar_end",
        "favorable_structure_known_at",
        "observed_mfe_at",
    ),
)
def test_new_policy_memory_rejects_ambiguous_or_invalid_timestamps(field):
    with pytest.raises(ValueError, match="aware ISO timestamp"):
        ManagementState(**{field: "2026-09-21T10:00:00"})
    with pytest.raises(ValueError, match="aware ISO timestamp"):
        ManagementState(**{field: "invalid"})


def test_independent_failure_memory_and_structure_survive_json_checkpoint():
    state = ManagementState(
        policy_fingerprint="sha256:policy",
        failure_episode="entry-range",
        failure_count=1,
        last_failure_bar_end="2026-09-21T10:00:00+05:30",
        local_failure_episode="swing-3",
        local_failure_count=1,
        last_local_failure_bar_end="2026-09-21T10:00:00+05:30",
        stagnation_count=2,
        last_stagnation_bar_end="2026-09-21T10:00:00+05:30",
        progress_close_r=2.4,
        favorable_structure_id="swing-3",
        favorable_structure_price=108.0,
        favorable_structure_buffer=0.2,
        favorable_structure_known_at="2026-09-21T09:55:00+05:30",
        observed_mfe_r=3.0,
        observed_mae_r=0.25,
        observed_mfe_at="2026-09-21T09:57:00+05:30",
        observed_mae_at="2026-09-21T09:40:00+05:30",
        observed_extrema_source="risk_mark",
        completed_mfe_r=2.4,
        completed_mae_r=0.15,
        completed_mfe_at="2026-09-21T09:55:00+05:30",
        completed_mae_at="2026-09-21T09:45:00+05:30",
        confirmed_stop=101.0,
    )

    restored = ManagementState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored == state
    assert restored.last_local_failure_bar_end == "2026-09-21T04:30:00+00:00"
    assert restored.failure_episode != restored.local_failure_episode
    assert restored.confirmed_stop == 101.0


def test_decision_evidence_cannot_change_after_recording():
    evidence = {"predicate": "intact", "sources": ["bar-1"]}
    decision = ExitDecision(
        decision_id="decision-1",
        action=ExitAction.HOLD,
        primary_reason_code="HOLD_THESIS_VALID",
        policy_version="policy-1",
        occurred_at="2026-09-21T04:30:00+00:00",
        supporting_evidence=(evidence,),
    )
    serialized = decision.to_dict()

    evidence["sources"].append("future-bar")
    evidence["predicate"] = "failed"

    assert decision.to_dict() == serialized
    with pytest.raises(TypeError):
        decision.supporting_evidence[0]["predicate"] = "failed"


def test_proposed_intent_rejects_nontext_position_identity():
    with pytest.raises(ValueError, match="position key"):
        ProposedIntent(
            intent_type="EXIT",
            position_key=10,
            reason_code="THESIS_BREAKOUT_FAILED",
        )


def test_future_quote_cannot_touch_objective_or_record_excursion():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    risk = _risk(
        context,
        mark=115.0,
        mark_time=context.decision_event_time + timedelta(minutes=5),
    )

    result = evaluate_exit(_thesis(), _state(), context, risk, ExitPolicy())

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    assert result.next_management_state.observed_mfe_r is None


def test_untimed_mark_override_cannot_bypass_stale_quote_rejection():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    risk = _risk(
        context,
        mark=115.0,
        mark_time=context.decision_event_time - timedelta(minutes=10),
    )

    result = evaluate_exit(
        _thesis(),
        _state(),
        context,
        risk,
        ExitPolicy(),
        observed_mark_price=115.0,
    )

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    assert result.next_management_state.observed_mfe_r is None


def test_decision_identity_separates_policy_artifacts():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    inputs = (_thesis(), _state(), context, _risk(context))
    first = evaluate_exit(*inputs, ExitPolicy(policy_version="candidate-v1"))
    other = evaluate_exit(*inputs, ExitPolicy(policy_version="candidate-v2"))
    repeated = evaluate_exit(*inputs, ExitPolicy(policy_version="candidate-v1"))

    assert first.decision.decision_id != other.decision.decision_id
    assert (
        first.next_management_state.policy_fingerprint
        != other.next_management_state.policy_fingerprint
    )
    assert json.dumps(first.decision.to_dict(), sort_keys=True) == json.dumps(
        repeated.decision.to_dict(), sort_keys=True
    )


def test_management_policy_cannot_change_silently_mid_position():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    first = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())
    changed = ExitPolicy(tick_size=0.1)
    next_context = _context(context.primary_bar.end, close=98.0)

    result = evaluate_exit(
        _thesis(),
        first.next_position_state,
        next_context,
        _risk(next_context),
        changed,
        management_state=first.next_management_state,
    )

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    assert result.next_management_state.failure_count == 0
    assert result.next_management_state.eligible_completed_bars == 1
    assert (
        result.next_management_state.policy_fingerprint
        == first.next_management_state.policy_fingerprint
    )


def test_policy_mismatch_never_vetoes_account_flatten():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    first = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())

    result = evaluate_exit(
        _thesis(),
        first.next_position_state,
        context,
        replace(_risk(context), daily_loss_latched=True),
        ExitPolicy(tick_size=0.1),
        management_state=first.next_management_state,
    )

    assert result.decision.action is ExitAction.REQUEST_EXIT
    assert result.decision.primary_reason_code == "RISK_DAILY_LOSS"
    assert result.proposed_intent is not None


def test_quote_before_entry_cannot_touch_objective_or_set_in_trade_extrema():
    context = _context(datetime(2026, 9, 21, 4, 15, tzinfo=timezone.utc))
    risk = _risk(
        context,
        mark=115.0,
        mark_time=context.decision_event_time - timedelta(seconds=30),
    )

    result = evaluate_exit(_thesis(), _state(), context, risk, ExitPolicy())

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    assert result.next_management_state.observed_mfe_r is None


def test_hard_stop_does_not_consume_future_quote_within_live_clock_skew_tolerance():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    risk = _risk(
        context,
        mark=94.0,
        mark_time=context.decision_event_time + timedelta(seconds=3),
    )

    result = evaluate_exit(_thesis(), _state(), context, risk, ExitPolicy())

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    assert result.next_management_state.observed_mae_r is None


def test_trace_retains_effective_inputs_state_and_intent_for_replay():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    thesis, state, risk, policy = _thesis(), _state(), _risk(context), ExitPolicy()

    result = evaluate_exit(thesis, state, context, risk, policy)
    trace = result.decision.to_dict()["trace"]

    assert trace["input_hash"]
    assert trace["thesis_id"] == thesis.thesis_id
    assert trace["thesis_hash"]
    assert trace["context_content_hash"]
    assert trace["context"]["primary_bar"]["bar_id"] == context.primary_bar.bar_id
    assert trace["risk"]["mark_price"] == risk.mark_price
    assert trace["policy_artifact"]["policy"]["policy_version"] == policy.policy_version
    assert trace["state_before"] == state.to_dict()
    assert trace["state_after"] == result.next_position_state.to_dict()
    assert trace["management_after"] == result.next_management_state.to_dict()
    assert trace["intent"] is None
    assert "candidates" in trace
