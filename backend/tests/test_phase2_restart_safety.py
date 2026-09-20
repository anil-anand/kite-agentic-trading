"""Crash-boundary recovery and broker-confirmed stop modification regressions."""

from copy import deepcopy

import pytest

from backend.broker_models import OrderRole, OrderSubmissionRejected
from backend.config import config_manager
from backend.order_lifecycle import IntentType
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.trading_engine import TradingEngine


def test_restart_recovers_entry_owner_when_checkpoint_was_never_written(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(4, "OPEN")

    # Crash after acknowledgement but before the caller writes its checkpoint.
    def crash(*args, **kwargs):
        raise SystemExit("crash after entry acknowledgement")

    e.engine._wait_for_entry_fill = crash
    with pytest.raises(SystemExit):
        e.engine.execute_signal(e.signal)
    config_manager.save_active_trades({})
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    owner = restarted.active_trades["RELIANCE"]
    assert owner["entry_order_id"] == "O1"
    assert owner["sl"] == 95
    assert owner["target"] == 110
    assert owner["entry_remainder_pending"] is True
    assert owner["stop_order_id"] == "O2"
    assert e.sdk.calls[-1]["quantity"] == 4
    assert len(e.sdk.calls) == 2


def test_restart_does_not_resubmit_entry_that_crashed_before_attempt(lifecycle):
    e = lifecycle
    e.journal.create_order_intent(
        intent_id="before-attempt",
        position_key="LIVE:acct-A:NSE:111:RELIANCE:MIS:e1",
        intent_type="ENTER",
        role="ENTRY",
        side="BUY",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
    )
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert restarted.active_trades == {}
    assert e.journal.list_unresolved_order_intents() == []
    assert e.sdk.calls == []
    assert restarted._lifecycle_recovery_pending is False


def test_restart_finds_accepted_exit_missing_from_checkpoint(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    assert e.engine.execute_signal(e.signal)
    checkpoint = deepcopy(e.engine.active_trades)
    owner = checkpoint["RELIANCE"]
    # The stop was confirmed cancelled and the reduction accepted, but the
    # process died before attaching its intent/order IDs to the old JSON.
    e.sdk.book[1]["status"] = "CANCELLED"
    e.sdk.book[1]["pending_quantity"] = 0
    result = e.engine._submit_lifecycle_order(
        position_key=e.engine._trade_position_key("RELIANCE", owner),
        intent_type=IntentType.EXIT,
        role=OrderRole.REDUCTION,
        side="SELL",
        quantity=10,
        order_kwargs={
            "variety": "regular",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "transaction_type": "SELL",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 99,
            "order_role": OrderRole.REDUCTION,
        },
        trade_id=owner["trade_id"],
        reason="Stop Loss",
        latched=True,
    )
    config_manager.save_active_trades(checkpoint)
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    recovered = restarted.active_trades["RELIANCE"]
    assert recovered["exit_intent_id"] == result.intent_id
    assert recovered["exit_order_id"] == "O3"
    assert recovered["exit_pending"] is True
    assert len(e.sdk.calls) == 3  # no competing replacement stop


@pytest.mark.parametrize("direction,stop", [("BUY", 105), ("SELL", 95)])
def test_breakeven_never_loosens_existing_profit_protection(lifecycle, direction, stop):
    e = lifecycle
    e.engine.active_trades["RELIANCE"] = {
        "tradingsymbol": "RELIANCE",
        "namespace": "LIVE",
        "account_id": "acct-A",
        "exchange": "NSE",
        "product": "MIS",
        "instrument_id": "111",
        "position_epoch": "tighten-test",
        "direction": direction,
        "entry_price": 100,
        "sl": stop,
        "stop_order_id": "STOP",
    }
    assert e.engine._tighten_to_breakeven("RELIANCE") is False
    assert e.engine.active_trades["RELIANCE"]["sl"] == stop
    assert e.sdk.calls == []


def test_modify_acknowledgement_does_not_confirm_new_trigger(lifecycle, monkeypatch):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    assert e.engine.execute_signal(e.signal)
    modifications = []
    monkeypatch.setattr(
        e.sdk,
        "modify_order",
        lambda **kwargs: modifications.append(kwargs) or "O2",
        raising=False,
    )
    assert e.engine._tighten_to_breakeven("RELIANCE") is False
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["sl"] == 95
    assert trade["requested_stop_trigger"] == 101
    assert len(modifications) == 1
    intent_id = trade["protection_intent_id"]
    durable = e.journal.get_order_intent(intent_id)["payload"]["recovery_trade"]
    assert durable["sl"] == 95
    assert durable["requested_stop_trigger"] == 101
    assert e.engine._tighten_to_breakeven("RELIANCE") is False
    assert len(modifications) == 1
    e.sdk.book[1]["trigger_price"] = 101
    e.sdk.book[1]["price"] = 99.99
    e.engine._tighten_to_breakeven("RELIANCE")
    assert trade["sl"] == 101
    assert "requested_stop_trigger" not in trade
    durable = e.journal.get_order_intent(intent_id)["payload"]["recovery_trade"]
    assert durable["sl"] == 101
    assert "requested_stop_trigger" not in durable
    config_manager.save_active_trades({})
    restarted = TradingEngine()
    restarted._restore_lifecycle_owners()
    assert restarted.active_trades["RELIANCE"]["sl"] == 101


def test_protection_failure_halts_entries_but_keeps_recovery_running(lifecycle):
    e = lifecycle
    original_place = e.sdk.place_order

    def reject_protection(**kwargs):
        if kwargs["order_type"] == "SL":
            raise OrderSubmissionRejected("protective stop rejected")
        return original_place(**kwargs)

    e.sdk.place_order = reject_protection
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    e.engine.running = True
    assert e.engine.execute_signal(e.signal) is False
    assert e.engine.running is True
    assert e.engine._protection_failure_halt is True
    assert e.engine.active_trades["RELIANCE"]["exit_pending"] is True
