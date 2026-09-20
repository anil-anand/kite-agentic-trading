from dataclasses import replace
from types import SimpleNamespace

from backend.broker_models import ExecutionNamespace
from backend.journal import TradeJournal
from backend.tests.test_exit_persistence import (
    _draft,
    _entry_intent,
    _pending_checkpoint,
    _terminal_transition,
)


def _engine(monkeypatch, journal):
    import backend.trading_engine as engine_module

    monkeypatch.setattr(engine_module, "journal", journal)
    monkeypatch.setattr(
        engine_module,
        "kite_client",
        SimpleNamespace(namespace=ExecutionNamespace.LIVE, account_id="acct-1"),
    )
    return engine_module.TradingEngine()


def test_missing_checkpoint_cannot_retire_operational_owner(monkeypatch, tmp_path):
    engine = _engine(monkeypatch, TradeJournal(str(tmp_path / "journal.db")))
    owner = {
        "exit_management_position_key": "LIVE:acct-1:NSE:1:RELIANCE:MIS:missing",
        "quantity": 10,
    }
    engine.active_trades["RELIANCE"] = owner
    assert engine._retire_phase5_position(dict(owner)) is False
    assert engine.active_trades["RELIANCE"] is owner
    assert owner["cleanup_pending"]
    assert owner["exit_state_recovery_required"]
    assert owner["recovery_state"] == "EXIT_STATE_PERSISTENCE_FAILED"


def test_database_restart_restores_residual_and_first_fill_time(monkeypatch, tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    bound, opened = _terminal_transition(thesis, state)
    opened = replace(
        opened,
        state=replace(opened.state, known_quantity=6),
        input_references={"first_fill_at": "2026-09-20T04:25:10+00:00"},
    )
    journal.commit_position_checkpoint(
        opened,
        event_id="terminal-fill",
        event_type="ENTRY_TERMINAL_PROTECTED",
        bound_thesis=bound,
    )
    engine = _engine(monkeypatch, journal)
    engine._restore_phase5_checkpoints()
    restored = engine.active_trades["RELIANCE"]
    assert restored["quantity"] == 10
    assert restored["executed_entry_quantity"] == 10
    assert restored["residual_quantity"] == 6
    assert restored["entry_price"] == 101.0
    assert restored["entry_time"].isoformat() == "2026-09-20T04:25:10+00:00"
    assert restored["exit_management_checkpoint"]["counters"]["episode"]["count"] == 2


def test_checkpoint_from_another_scope_is_quarantined(monkeypatch, tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    thesis = replace(
        thesis, position_key=thesis.position_key.replace("LIVE:", "PAPER:", 1)
    )
    _, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    engine = _engine(monkeypatch, journal)
    engine.active_trades["RELIANCE"] = {
        "exit_management_position_key": thesis.position_key,
        "direction": "SELL",
    }
    engine._restore_phase5_checkpoints()
    restored = engine.active_trades["RELIANCE"]
    assert restored["ownership_quarantined"]
    assert restored["exit_state_recovery_required"]
    assert restored["direction"] == "SELL"
    assert thesis.position_key not in engine._managed_position_states
