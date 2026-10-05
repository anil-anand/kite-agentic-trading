"""Small phase-6 trader-behaviour scenarios; orchestration parity is phase 8."""

from datetime import datetime, timezone

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import ExitAction, ManagementState

from .test_engine import _context, _risk, _state, _thesis

UTC = timezone.utc


def test_s04_breakout_reclaim_is_a_hold_not_a_failed_acceptance():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=100.5)

    result = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())

    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None


def test_s21_missing_normal_data_is_explicit_hold_and_preserves_risk_state():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC))
    first = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())
    degraded = evaluate_exit(
        _thesis(),
        first.next_position_state,
        None,
        _risk(context),
        ExitPolicy(),
        management_state=first.next_management_state,
    )

    assert degraded.decision.action is ExitAction.HOLD
    assert degraded.decision.primary_reason_code == "HOLD_DATA_DEGRADED"
    assert degraded.next_position_state.exposure.value == "OPEN"


def test_s24_unknown_thesis_retains_bounded_management_without_fabricated_exit():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC))

    result = evaluate_exit(
        None,
        _state(),
        context,
        _risk(context),
        ExitPolicy(),
        management_state=ManagementState(confirmed_stop=95.0),
    )

    assert result.decision.action is ExitAction.HOLD
    assert result.decision.primary_reason_code == "HOLD_UNKNOWN_THESIS"
