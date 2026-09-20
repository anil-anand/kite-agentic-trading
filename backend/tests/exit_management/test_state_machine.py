import pytest

from backend.exit_management.models import (
    DevelopmentPhase,
    ExposureState,
    LifecycleEvent,
    LifecycleTransitionError,
    PositionState,
    ProtectionState,
    ThesisHealth,
    reduce_lifecycle,
)


def _initial():
    return PositionState(
        position_key="LIVE:acct-1:NSE:1:RELIANCE:MIS:epoch-1",
        thesis_health=ThesisHealth.VALID,
    )


def _step(state, event, event_id, **kwargs):
    return reduce_lifecycle(
        state,
        event,
        event_id=event_id,
        occurred_at="2026-09-20T04:25:00+00:00",
        **kwargs,
    )


def test_exposure_transition_table_requires_protection_before_open():
    new = _initial()
    pending = _step(new, LifecycleEvent.ENTRY_INTENT_COMMITTED, "intent-1")
    with pytest.raises(LifecycleTransitionError, match="confirmed active protection"):
        _step(pending, LifecycleEvent.ENTRY_TERMINAL_PROTECTED, "open-1")

    opened = _step(
        pending,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        "open-1",
        protection=ProtectionState.ACTIVE,
        known_quantity=10,
    )
    assert opened.exposure.value == "OPEN"
    assert opened.protection is ProtectionState.ACTIVE
    assert opened.known_quantity == 10


def test_exit_is_latched_and_cannot_return_to_open_after_a_price_recovery():
    pending = _step(_initial(), LifecycleEvent.ENTRY_INTENT_COMMITTED, "intent-1")
    opened = _step(
        pending,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        "open-1",
        protection=ProtectionState.ACTIVE,
        known_quantity=10,
    )
    exiting = _step(
        opened,
        LifecycleEvent.EXIT_REQUESTED,
        "exit-1",
        exit_intent_id="exit-intent-1",
    )
    assert exiting.exposure.value == "EXIT_PENDING"
    with pytest.raises(LifecycleTransitionError):
        _step(
            exiting,
            LifecycleEvent.RECONCILED_OPEN,
            "bounce-is-not-a-recovery",
            protection=ProtectionState.ACTIVE,
        )


def test_duplicate_event_is_idempotent_and_invalidation_stays_latched():
    pending = _step(_initial(), LifecycleEvent.ENTRY_INTENT_COMMITTED, "intent-1")
    updated = _step(pending, LifecycleEvent.ENTRY_UPDATE, "bar-1")
    assert _step(updated, LifecycleEvent.ENTRY_UPDATE, "bar-1") == updated

    invalidated = _step(
        pending,
        LifecycleEvent.EXIT_REQUESTED,
        "exit-1",
        exit_intent_id="exit-intent-1",
        thesis_health=ThesisHealth.INVALIDATED,
        known_quantity=10,
    )
    next_state = _step(
        invalidated,
        LifecycleEvent.EXIT_UPDATE,
        "exit-progress-1",
        thesis_health=ThesisHealth.VALID,
    )
    assert next_state.thesis_health is ThesisHealth.INVALIDATED


def _opened():
    pending = _step(_initial(), LifecycleEvent.ENTRY_INTENT_COMMITTED, "entry")
    return _step(
        pending,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        "opened",
        known_quantity=10,
        protection=ProtectionState.ACTIVE,
    )


@pytest.mark.parametrize(
    "event", [LifecycleEvent.RECONCILED_OPEN, LifecycleEvent.RECONCILED_ENTRY_PENDING]
)
def test_recovery_cannot_resume_entry_or_hold_after_a_latched_exit(event):
    exiting = _step(
        _opened(), LifecycleEvent.EXIT_REQUESTED, "exit", exit_intent_id="exit-1"
    )
    recovery = _step(exiting, LifecycleEvent.RECONCILIATION_REQUIRED, "unknown-cancel")
    assert recovery.recovery_from is ExposureState.EXIT_PENDING
    restored = PositionState.from_dict(recovery.to_dict())
    with pytest.raises(LifecycleTransitionError, match="latched exit"):
        _step(restored, event, "price-recovered")
    continuing = _step(restored, LifecycleEvent.RECONCILED_EXIT_PENDING, "reconciled")
    assert continuing.latched_exit_intent_id == "exit-1"


def test_hard_risk_escalation_cannot_replace_the_existing_exit_owner():
    exiting = _step(
        _opened(), LifecycleEvent.EXIT_REQUESTED, "exit", exit_intent_id="exit-1"
    )
    with pytest.raises(LifecycleTransitionError, match="cannot be replaced"):
        _step(
            exiting, LifecycleEvent.EXIT_REQUESTED, "hard-risk", exit_intent_id="exit-2"
        )


@pytest.mark.parametrize("quantity", [None, 0])
def test_terminal_entry_requires_positive_reconciled_residual(quantity):
    pending = _step(_initial(), LifecycleEvent.ENTRY_INTENT_COMMITTED, "entry")
    with pytest.raises(LifecycleTransitionError, match="positive quantity"):
        _step(
            pending,
            LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
            "opened",
            protection=ProtectionState.ACTIVE,
            known_quantity=quantity,
        )


def test_a_failed_snapshot_cannot_manufacture_flatness_or_abort_a_filled_entry():
    opened = _opened()
    with pytest.raises(LifecycleTransitionError, match="known zero"):
        _step(opened, LifecycleEvent.FLAT_OBSERVED, "failed-read")
    recovery = _step(opened, LifecycleEvent.RECONCILIATION_REQUIRED, "uncertain")
    with pytest.raises(LifecycleTransitionError, match="filled entry"):
        _step(
            recovery,
            LifecycleEvent.ENTRY_ABORTED,
            "terminal-zero",
            known_quantity=0,
            protection=ProtectionState.NONE_FLAT,
        )


def test_flatness_does_not_imply_orders_are_incapable_of_reopening_exposure():
    flat = _step(_opened(), LifecycleEvent.FLAT_OBSERVED, "flat", known_quantity=0)
    with pytest.raises(LifecycleTransitionError, match="order cleanup"):
        _step(flat, LifecycleEvent.FLAT_CONFIRMED, "closed")
    closed = _step(
        flat,
        LifecycleEvent.FLAT_CONFIRMED,
        "closed",
        protection=ProtectionState.NONE_FLAT,
    )
    assert closed.exposure is ExposureState.CLOSED


def test_confirmed_stop_stays_effective_while_tightening_is_pending():
    pending = _step(
        _opened(),
        LifecycleEvent.STATE_OBSERVED,
        "stop-modify",
        protection=ProtectionState.UPDATE_PENDING,
    )
    assert pending.exposure is ExposureState.OPEN
    acknowledged = _step(
        pending,
        LifecycleEvent.STATE_OBSERVED,
        "stop-ack",
        protection=ProtectionState.ACTIVE,
    )
    with pytest.raises(LifecycleTransitionError, match="confirmed active protection"):
        _step(
            acknowledged,
            LifecycleEvent.STATE_OBSERVED,
            "stop-lost",
            protection=ProtectionState.UNCONFIRMED,
        )


def test_data_outage_cannot_erase_known_thesis_or_restart_development_grace():
    opened = _step(
        _opened(),
        LifecycleEvent.STATE_OBSERVED,
        "favorable",
        development=DevelopmentPhase.FAVORABLE,
    )
    with pytest.raises(LifecycleTransitionError, match="cannot erase"):
        _step(
            opened,
            LifecycleEvent.STATE_OBSERVED,
            "outage",
            thesis_health=ThesisHealth.UNKNOWN,
        )
    with pytest.raises(LifecycleTransitionError, match="return to EARLY"):
        _step(
            opened,
            LifecycleEvent.STATE_OBSERVED,
            "reset",
            development=DevelopmentPhase.EARLY,
        )


def test_invalidation_requires_simultaneous_intent_even_while_broker_is_unknown():
    with pytest.raises(LifecycleTransitionError, match="simultaneous latched"):
        _step(
            _opened(),
            LifecycleEvent.RECONCILIATION_REQUIRED,
            "broken-thesis",
            thesis_health=ThesisHealth.INVALIDATED,
        )
    recovery = _step(
        _opened(),
        LifecycleEvent.RECONCILIATION_REQUIRED,
        "broken-thesis",
        thesis_health=ThesisHealth.INVALIDATED,
        exit_intent_id="exit-1",
        development=DevelopmentPhase.PULLBACK,
    )
    assert recovery.thesis_health is ThesisHealth.INVALIDATED
    assert recovery.development is DevelopmentPhase.EARLY


@pytest.mark.parametrize("quantity", [-1, 1.5, True])
def test_position_state_rejects_non_integer_or_negative_quantity(quantity):
    with pytest.raises(ValueError, match="nonnegative integer"):
        PositionState(position_key="position", known_quantity=quantity)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "1"),
        ("version", True),
        ("known_quantity", "10"),
        ("had_fills", "false"),
        ("protection", "UNCONFIRMED"),
        ("known_quantity", None),
    ],
)
def test_corrupt_checkpoint_cannot_be_coerced_into_a_valid_open_state(field, value):
    stored = _opened().to_dict()
    stored[field] = value
    with pytest.raises(ValueError):
        PositionState.from_dict(stored)


@pytest.mark.parametrize(
    "field",
    [
        "exposure",
        "protection",
        "version",
        "known_quantity",
        "latched_exit_intent_id",
        "last_event_id",
    ],
)
def test_missing_core_checkpoint_fields_do_not_silently_reset_the_position(field):
    stored = _opened().to_dict()
    stored.pop(field)
    with pytest.raises(KeyError):
        PositionState.from_dict(stored)
