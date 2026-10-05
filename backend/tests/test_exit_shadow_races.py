"""Races between historical-data reads and authoritative broker checkpoints."""

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.models import (
    LifecycleEvent,
    ProtectionState,
    reduce_lifecycle,
)
from backend.exit_management.thesis import ThesisBindingStatus
from backend.journal import journal
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_exit_live_integration import START, _managed_engine, _position


@pytest.fixture(autouse=True)
def _isolated_risk_inputs(monkeypatch):
    import backend.trading_engine as engine_module

    monkeypatch.setattr(engine_module.risk_manager, "kill_switch_active", False)
    monkeypatch.setattr(
        engine_module.TradingEngine,
        "_session_clock",
        lambda self: SessionClock(SessionPolicy()),
    )


def _checkpoint_transition(engine, key, event, event_id, *, protection=None):
    state = engine._phase5_state_for(key)
    next_state = reduce_lifecycle(
        state,
        event,
        event_id=event_id,
        occurred_at=START + timedelta(minutes=5),
        protection=protection,
    )
    engine._commit_phase5_checkpoint(
        position_key=key,
        state=next_state,
        event_id=event_id,
        event_type="SCRIPTED_BROKER_OBSERVATION",
        details={},
    )


def _set_context(monkeypatch, context):
    import backend.trading_engine as engine_module

    monkeypatch.setattr(engine_module, "now_utc", lambda: context.decision_event_time)
    monkeypatch.setattr(
        engine_module.scanner, "get_market_context", lambda *args, **kwargs: context
    )


def test_reconciliation_does_not_consume_unprocessed_completed_bar(monkeypatch):
    engine, thesis = _managed_engine()
    context = _context(START, close=98.0)
    _set_context(monkeypatch, context)
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    _checkpoint_transition(
        engine,
        thesis.position_key,
        LifecycleEvent.RECONCILIATION_REQUIRED,
        "broker-recovery-start",
    )

    position = _position(100.0, context.decision_event_time)
    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["counters"].get("exit_shadow_last_bar_end") is None
    assert checkpoint["counters"]["exit_policy"]["eligible_completed_bars"] == 0
    assert (
        journal.get_exit_decisions(thesis.position_key)[-1]["payload"]["action"]
        == "RECONCILE_REQUIRED"
    )

    _checkpoint_transition(
        engine,
        thesis.position_key,
        LifecycleEvent.RECONCILED_OPEN,
        "broker-recovery-complete",
    )
    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])

    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["counters"]["exit_policy"]["eligible_completed_bars"] == 1
    assert checkpoint["counters"]["exit_policy"]["failure_count"] == 1
    assert checkpoint["counters"]["exit_shadow_last_bar_end"] == (
        context.primary_bar.end.isoformat()
    )
    assert len(journal.get_exit_decisions(thesis.position_key)) == 2


@pytest.mark.parametrize("recovers", [True, False])
def test_protection_change_during_preparation_uses_latest_broker_fact(
    monkeypatch, recovers
):
    engine, thesis = _managed_engine()
    context = _context(START, close=100.0)
    _set_context(monkeypatch, context)
    trade = engine.active_trades["RELIANCE"]
    if recovers:
        _checkpoint_transition(
            engine,
            thesis.position_key,
            LifecycleEvent.RECONCILIATION_REQUIRED,
            "initial-protection-failure",
            protection=ProtectionState.FAILED_OR_UNKNOWN,
        )
        trade["recovery_state"] = "EMERGENCY_REDUCTION_REQUIRED"

    def tick_size(*args):
        if recovers:
            _checkpoint_transition(
                engine,
                thesis.position_key,
                LifecycleEvent.RECONCILED_OPEN,
                "protection-restored-during-preparation",
                protection=ProtectionState.ACTIVE,
            )
            trade.pop("recovery_state")
        else:
            _checkpoint_transition(
                engine,
                thesis.position_key,
                LifecycleEvent.RECONCILIATION_REQUIRED,
                "protection-failed-during-preparation",
                protection=ProtectionState.FAILED_OR_UNKNOWN,
            )
            trade["recovery_state"] = "EMERGENCY_REDUCTION_REQUIRED"
        return 0.05

    monkeypatch.setattr(engine, "_get_tick_size", tick_size)
    engine._evaluate_shadow_position(
        _position(100.0, context.decision_event_time), dict(trade)
    )

    decision = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    candidate = journal.get_managed_position(thesis.position_key)["state"]["counters"][
        "shadow_candidate_position_state"
    ]
    assert decision["trace"]["risk"]["protection_failed"] is not recovers
    if recovers:
        assert decision["action"] == "HOLD"
        assert candidate["latched_exit_intent_id"] is None
    else:
        assert decision["primary_reason_code"] == "RISK_PROTECTION_FAILURE"
        assert candidate["latched_exit_intent_id"] is not None


def test_preparation_delay_cannot_rejuvenate_old_stop_crossing_mark(monkeypatch):
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    context = _context(START, close=100.0)
    _set_context(monkeypatch, context)
    clock = [context.decision_event_time]
    monkeypatch.setattr(engine_module, "now_utc", lambda: clock[0])

    def tick_size(*args):
        clock[0] += timedelta(minutes=3)
        return 0.05

    monkeypatch.setattr(engine, "_get_tick_size", tick_size)
    engine._evaluate_shadow_position(
        _position(94.0, context.decision_event_time), engine.active_trades["RELIANCE"]
    )

    decision = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    assert decision["action"] == "HOLD"
    assert decision["trace"]["mark_price"] is None
    assert decision["trace"]["risk"]["session"]["observed_at"] == clock[0].isoformat()


def test_terminal_thesis_revision_during_fetch_requires_fresh_evaluation(monkeypatch):
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    context = _context(START, close=98.0)
    _set_context(monkeypatch, context)
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    stale_thesis = replace(
        thesis,
        revision=thesis.revision - 1,
        binding_status=ThesisBindingStatus.DRAFT,
        fill_binding=None,
        provisional_risk=None,
    )
    original_thesis_reader = engine._phase5_thesis_for
    fetched = [False]

    def read_thesis(key):
        # Simulate reading the draft immediately before a terminal fill binds
        # the same known quantity. The durable record already holds that bind.
        return original_thesis_reader(key) if fetched[0] else stale_thesis

    def fetch(*args, **kwargs):
        fetched[0] = True
        return context

    monkeypatch.setattr(engine, "_phase5_thesis_for", read_thesis)
    monkeypatch.setattr(engine_module.scanner, "get_market_context", fetch)
    position = _position(100.0, context.decision_event_time)
    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])

    assert journal.get_exit_decisions(thesis.position_key) == []
    assert journal.get_managed_position(thesis.position_key)["checkpoint_sequence"] == 0

    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["counters"]["exit_policy"]["failure_count"] == 1
    assert len(journal.get_exit_decisions(thesis.position_key)) == 1


def test_blocked_position_worker_does_not_delay_another_owned_position(monkeypatch):
    engine, _ = _managed_engine()
    first = _position(100.0, START + timedelta(minutes=5))
    second = {
        **first,
        "tradingsymbol": "INFY",
        "instrument_token": "2",
        "position_key": "LIVE:acct-1:NSE:2:INFY:MIS",
    }
    engine.active_trades["INFY"] = {
        **engine.active_trades["RELIANCE"],
        "tradingsymbol": "INFY",
        "instrument_id": "2",
        "exit_management_position_key": "LIVE:acct-1:NSE:2:INFY:MIS:epoch-2",
        "position_epoch": "epoch-2",
    }
    engine._last_positions = [first, second]
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def review(position, trade):
        if position["tradingsymbol"] == "RELIANCE":
            first_entered.set()
            release_first.wait(timeout=5)
        else:
            second_finished.set()

    monkeypatch.setattr(engine, "_evaluate_shadow_position", review)
    try:
        engine._schedule_shadow_evaluations()
        assert first_entered.wait(timeout=2)
        assert second_finished.wait(timeout=2)
        assert not release_first.is_set()
    finally:
        release_first.set()
        for worker in engine._shadow_position_workers.values():
            worker.join(timeout=2)
            assert not worker.is_alive()
