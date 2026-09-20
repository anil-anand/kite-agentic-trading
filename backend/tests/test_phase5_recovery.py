"""Phase-five provenance and state survive real entry/recovery boundaries."""

import pytest

from backend.config import config_manager
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.tests.test_review5_lifecycle import unknown_entry
from backend.trading_engine import TradingEngine


def _state(env, key):
    return env.journal.get_managed_position(key)["state"]["state"]


def _binding(env, key):
    return env.journal.get_position_thesis(key)["payload"]["fill_binding"]


def _terminal_cancel(env):
    def cancel(variety, order_id, **kwargs):
        order = next(row for row in env.sdk.book if row["order_id"] == order_id)
        order.update(status="CANCELLED", pending_quantity=0)
        return order_id

    env.sdk.cancel_order = cancel


@pytest.mark.parametrize("filled", [4, 10])
def test_acknowledged_entry_recovers_frozen_thesis_without_json(lifecycle, filled):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(
        filled, "OPEN" if filled == 4 else "COMPLETE"
    )

    def crash(*args, **kwargs):
        raise SystemExit("accepted entry before compatibility checkpoint")

    e.engine._wait_for_entry_fill = crash
    with pytest.raises(SystemExit):
        e.engine.execute_signal(e.signal)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    original = e.journal.get_position_thesis(key, revision=1)["payload"]
    config_manager.save_active_trades({})

    restarted = TradingEngine()
    restarted.reconcile_active_trades()

    assert restarted.active_trades["RELIANCE"]["exit_management_position_key"] == key
    assert e.journal.get_position_thesis(key, revision=1)["payload"] == original
    assert e.sdk.calls[-1]["quantity"] == filled
    if filled == 4:
        assert _binding(e, key) is None
        _terminal_cancel(e)
        e.sdk.fill_entry(10, "COMPLETE")
        restarted.monitor_positions()
    assert _binding(e, key)["entry_vwap"] == 101
    assert _binding(e, key)["filled_quantity"] == 10
    assert _binding(e, key)["initial_risk_budget"] == 60
    assert _state(e, key)["exposure"] == "OPEN"
    assert _state(e, key)["known_quantity"] == 10


def test_crash_before_attempt_aborts_managed_thesis_without_dispatch(lifecycle):
    e = lifecycle

    def crash(*args, **kwargs):
        raise SystemExit("thesis committed before attempt")

    e.engine._submit_lifecycle_order = crash
    with pytest.raises(SystemExit):
        e.engine.execute_signal(e.signal)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    config_manager.save_active_trades({})

    restarted = TradingEngine()
    restarted.reconcile_active_trades()

    assert restarted.active_trades == {}
    assert _state(e, key)["exposure"] == "ENTRY_ABORTED"
    assert _binding(e, key) is None
    assert e.sdk.calls == []


@pytest.mark.parametrize("filled,residual", [(0, 0), (10, 0), (10, 6)])
def test_recovered_entry_preserves_original_r_and_terminal_state(
    lifecycle, filled, residual
):
    e = lifecycle
    unknown_entry(e)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    e.sdk.fill_entry(filled, "COMPLETE" if filled else "CANCELLED", residual=residual)
    if filled > residual:
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "MANUAL-FILL",
                "order_id": "MANUAL",
                "transaction_type": "SELL",
                "quantity": filled - residual,
                "average_price": 99,
            }
        )
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted.monitor_positions()

    if filled:
        assert _binding(e, key)["filled_quantity"] == filled
        assert _binding(e, key)["initial_risk_budget"] == 60
        assert _state(e, key)["known_quantity"] == residual
        assert _state(e, key)["exposure"] == ("OPEN" if residual else "CLOSED")
    else:
        assert _binding(e, key) is None
        assert _state(e, key)["exposure"] == "ENTRY_ABORTED"
    if residual:
        assert e.sdk.calls[-1]["quantity"] == residual
    else:
        assert restarted.active_trades == {}


def test_thesis_write_failure_retains_protected_entry_for_retry(lifecycle, monkeypatch):
    e = lifecycle
    unknown_entry(e)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    e.sdk.fill_entry()
    bind = e.engine._bind_phase5_terminal_entry

    def fail(*args, **kwargs):
        raise OSError("injected checkpoint write failure")

    monkeypatch.setattr(e.engine, "_bind_phase5_terminal_entry", fail)
    e.engine.monitor_positions()
    assert e.engine.active_trades["RELIANCE"]["stop_order_id"] == "O2"
    assert e.engine.active_trades["RELIANCE"]["entry_state"] == "RECOVERY_REQUIRED"
    assert e.risk.pending_entry_reservation_count == 1
    assert len(e.sdk.calls) == 2

    monkeypatch.setattr(e.engine, "_bind_phase5_terminal_entry", bind)
    e.engine.monitor_positions()
    assert _state(e, key)["exposure"] == "OPEN"
    assert e.risk.pending_entry_reservation_count == 0
    assert len(e.sdk.calls) == 2


def test_entry_binding_during_pending_exit_cannot_replace_cancelled_protection(
    lifecycle,
):
    e = lifecycle
    unknown_entry(e)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    e.sdk.fill_entry()
    e.sdk.fail_fills = True
    e.engine.monitor_positions()
    _terminal_cancel(e)
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert e.sdk.calls[-1]["transaction_type"] == "SELL"
    e.sdk.fail_fills = False
    e.engine._recover_pending_entry(
        "RELIANCE",
        e.engine._positions()[0],
        e.engine.active_trades["RELIANCE"],
    )
    assert _binding(e, key)["filled_quantity"] == 10
    assert _state(e, key)["exposure"] == "EXIT_PENDING"
    assert len(e.sdk.calls) == 3


@pytest.mark.parametrize("cancel", [True, False])
def test_entry_control_arriving_during_thesis_commit_prevents_dispatch(
    lifecycle, monkeypatch, cancel
):
    e = lifecycle
    register = e.engine._register_entry_thesis
    captured = {}

    def pause_after_commit(**kwargs):
        result = register(**kwargs)
        captured["key"] = kwargs["position_key"]
        if cancel:
            e.engine.active_trades["RELIANCE"]["entry_cancel_requested"] = True
        else:
            monkeypatch.setattr(
                e.engine, "_entry_admission_allowed", lambda: (False, "ENTRY_PAUSED")
            )
        return result

    monkeypatch.setattr(e.engine, "_register_entry_thesis", pause_after_commit)
    assert e.engine.execute_signal(e.signal) is False
    assert e.sdk.calls == []
    assert _state(e, captured["key"])["exposure"] == "ENTRY_ABORTED"
    assert e.journal.list_unresolved_order_intents() == []
    assert e.risk.pending_entry_reservation_count == 0


def test_terminal_entry_binding_uses_all_fills_when_residual_already_reduced(
    lifecycle, monkeypatch
):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(10, "COMPLETE", residual=6)

    def observed_terminal_fill(signal, order_id, baseline_quantity):
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "EARLY-REDUCTION",
                "order_id": "MANUAL",
                "transaction_type": "SELL",
                "quantity": 4,
                "average_price": 99,
            }
        )
        # The completed entry observation and current net position describe
        # distinct quantities once an external reduction has occurred.
        return e.engine._positions()[0]

    monkeypatch.setattr(e.engine, "_wait_for_entry_fill", observed_terminal_fill)
    assert e.engine.execute_signal(e.signal)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    assert _binding(e, key)["filled_quantity"] == 10
    assert _binding(e, key)["initial_risk_budget"] == 60
    assert _state(e, key)["known_quantity"] == 6
    assert e.sdk.calls[-1]["quantity"] == 6
    assert e.journal.get_trades()[0]["quantity"] == 10


def test_recovered_binding_rereads_residual_after_stop_resize_fill(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(4, "OPEN")
    assert e.engine.execute_signal(e.signal) is False
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    e.sdk.fill_entry(10, "COMPLETE")

    def cancel_with_fill(variety, order_id, **kwargs):
        assert order_id == "O2"
        e.sdk.book[1].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
        e.sdk.position_rows[0].update(quantity=8, day_sell_quantity=2, sell_quantity=2)
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "STOP-DURING-RESIZE",
                "order_id": "O2",
                "transaction_type": "SELL",
                "quantity": 2,
                "average_price": 94,
            }
        )
        return order_id

    e.sdk.cancel_order = cancel_with_fill
    e.engine.monitor_positions()
    assert _binding(e, key)["filled_quantity"] == 10
    assert _binding(e, key)["initial_risk_budget"] == 60
    assert _state(e, key)["known_quantity"] == 8
    checkpoint = e.journal.get_managed_position(key)["state"]
    assert checkpoint["protection"]["protected_quantity"] == 8
    assert e.sdk.calls[-1]["quantity"] == 8


@pytest.mark.parametrize("native_stop", [True, False])
@pytest.mark.parametrize("persist_failure", [True, False])
def test_flat_closure_is_durable_before_operational_owner_is_removed(
    lifecycle, monkeypatch, native_stop, persist_failure
):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    assert e.engine.execute_signal(e.signal)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    e.engine._external_close_grace_seconds = 0
    e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10, sell_quantity=10)
    if native_stop:
        e.sdk.book[1].update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    else:
        _terminal_cancel(e)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "FINAL-CLOSE",
            "order_id": "O2" if native_stop else "MANUAL",
            "transaction_type": "SELL",
            "quantity": 10,
            "average_price": 94,
        }
    )
    close = e.engine._record_phase5_closed
    if persist_failure:

        def fail(*args, **kwargs):
            raise OSError("injected closure checkpoint failure")

        monkeypatch.setattr(e.engine, "_record_phase5_closed", fail)
    e.engine.monitor_positions()
    e.engine.monitor_positions()
    if persist_failure:
        assert "RELIANCE" in e.engine.active_trades
        assert _state(e, key)["exposure"] == "FLAT_PENDING_RECONCILIATION"
        monkeypatch.setattr(e.engine, "_record_phase5_closed", close)
        e.engine.monitor_positions()
    assert e.engine.active_trades == {}
    assert _state(e, key)["exposure"] == "CLOSED"
    assert e.journal.get_trades()[0]["quantity"] == 10
    assert len(e.sdk.calls) == 2


@pytest.mark.parametrize("exit_pending", [True, False])
def test_missing_open_snapshot_restores_thesis_and_confirmed_execution_obligations(
    lifecycle, monkeypatch, exit_pending
):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    assert e.engine.execute_signal(e.signal)
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    thesis = e.journal.get_position_thesis(key)["payload"]
    if exit_pending:
        _terminal_cancel(e)
        e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
        intent_id = e.engine.active_trades["RELIANCE"]["exit_intent_id"]
    else:

        def modify(**kwargs):
            e.sdk.book[1].update(kwargs)
            return "O2"

        monkeypatch.setattr(e.sdk, "modify_order", modify, raising=False)
        e.engine._tighten_to_breakeven("RELIANCE")
        assert e.engine.active_trades["RELIANCE"]["sl"] == 101
    before = len(e.sdk.calls)
    config_manager.save_active_trades({})
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restored = restarted.active_trades["RELIANCE"]
    assert restored["exit_management_position_key"] == key
    assert e.journal.get_position_thesis(key)["payload"] == thesis
    assert restarted._entry_theses[key].fill_binding.filled_quantity == 10
    if exit_pending:
        assert restored["exit_intent_id"] == intent_id
        assert restored["exit_pending"] is True
        assert _state(e, key)["exposure"] == "EXIT_PENDING"
    else:
        assert restored["sl"] == 101
        assert _state(e, key)["exposure"] == "OPEN"
    assert len(e.sdk.calls) == before
