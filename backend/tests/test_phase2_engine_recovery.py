"""Adversarial broker races for phase-two engine integration."""

from datetime import timedelta

from backend.broker_models import OrderSubmissionRejected, OrderSubmissionUnknown
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import now_utc


def open_trade(env):
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    return env.engine.active_trades["RELIANCE"]


def cancel_terminal(env, cancelled):
    def cancel(variety, order_id, **kwargs):
        cancelled.append(order_id)
        order = next(row for row in env.sdk.book if row["order_id"] == order_id)
        if order["status"] in {"COMPLETE", "CANCELLED"}:
            raise ValueError("cannot cancel a terminal order")
        order.update(status="CANCELLED", pending_quantity=0)
        return order_id

    return cancel


def test_unknown_protective_submit_never_competes_with_emergency_exit(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    place = e.sdk.place_order

    def accepted_then_timeout(**kwargs):
        result = place(**kwargs)
        if kwargs["order_type"] == "SL":
            raise OrderSubmissionUnknown("accepted stop, response lost")
        return result

    e.sdk.place_order = accepted_then_timeout
    assert e.engine.execute_signal(e.signal) is False
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["protection_intent_id"]
    assert trade["exit_intent_id"]
    assert len(e.sdk.calls) == 2
    assert trade["exit_order_id"] is None

    e.sdk.cancel_order = cancel_terminal(e, [])
    e.engine.monitor_positions()
    assert len(e.sdk.calls) == 3
    assert e.sdk.calls[-1]["quantity"] == 10
    assert e.sdk.book[1]["status"] == "CANCELLED"


def test_stop_resize_reconciles_partial_fill_during_cancel(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.book[1].update(quantity=4, pending_quantity=4)

    def cancel(variety, order_id, **kwargs):
        assert order_id == "O2"
        e.sdk.book[1].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
        e.sdk.position_rows[0]["quantity"] = 8
        return order_id

    e.sdk.cancel_order = cancel
    position = e.engine._positions()[0]
    assert e.engine._ensure_recovery_protection("RELIANCE", position, trade)
    assert e.sdk.calls[-1]["quantity"] == 8
    assert trade["protection_quantity"] == 8


def test_flatten_cancels_entry_remainder_and_includes_late_fill(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(4, "OPEN")
    assert e.engine.execute_signal(e.signal) is False
    assert e.sdk.book[0]["status"] == "OPEN"
    cancelled = []

    def cancel(variety, order_id, **kwargs):
        cancelled.append(order_id)
        order = next(row for row in e.sdk.book if row["order_id"] == order_id)
        if order_id == "O1":
            order["filled_quantity"] = 6
            e.sdk.position_rows[0]["quantity"] = 6
        order.update(status="CANCELLED", pending_quantity=0)
        return order_id

    e.sdk.cancel_order = cancel
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert cancelled == ["O1", "O2"]
    assert e.sdk.calls[-1]["quantity"] == 6
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
    e.engine.monitor_positions()
    assert len(e.sdk.calls) == 3


def test_triggered_unfilled_stop_enters_reduction_recovery(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.book[1]["status"] = "OPEN"
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.engine.monitor_positions()
    assert trade["exit_pending"]
    assert trade["exit_order_id"] == "O3"
    assert e.sdk.book[1]["status"] == "CANCELLED"


def test_weaker_native_stop_is_replaced_after_terminal_cancellation(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.book[1].update(trigger_price=90, price=89)
    e.sdk.cancel_order = cancel_terminal(e, [])
    assert e.engine._ensure_recovery_protection(
        "RELIANCE", e.engine._positions()[0], trade
    )
    assert e.sdk.calls[-1]["trigger_price"] == 95
    assert e.sdk.book[1]["status"] == "CANCELLED"


def test_adoption_reserves_owner_before_protection_and_confirms_coverage(lifecycle):
    e = lifecycle
    e.sdk.position_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "quantity": 10,
            "average_price": 100,
            "last_price": 100,
            "buy_quantity": 10,
            "sell_quantity": 0,
            "buy_value": 1000,
            "sell_value": 0,
        }
    ]
    place = e.sdk.place_order

    def owned_before_send(**kwargs):
        assert e.engine.active_trades["RELIANCE"]["position_epoch"].startswith(
            "adopted-"
        )
        return place(**kwargs)

    e.sdk.place_order = owned_before_send
    e.engine._adopt_position(e.engine._positions()[0])
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["protection_quantity"] == 10
    assert trade["broker_reconciliation_pending"] is False
    assert trade["protection_intent_id"]


def test_external_flat_cancels_working_app_exit_before_it_can_reverse(lifecycle):
    e = lifecycle
    open_trade(e)
    cancelled = []
    e.sdk.cancel_order = cancel_terminal(e, cancelled)
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    assert e.sdk.book[2]["status"] == "OPEN"
    e.sdk.position_rows[0]["quantity"] = 0
    e.engine._sync_exit_pending_status("RELIANCE", e.engine._orders())
    assert cancelled == ["O2", "O3"]
    assert e.sdk.book[2]["status"] == "CANCELLED"
    assert len(e.sdk.calls) == 3


def test_stale_limit_exit_waits_for_cancel_then_markets_fresh_residual(
    lifecycle, monkeypatch
):
    import backend.trading_engine as engine_module

    e = lifecycle
    trade = open_trade(e)
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    assert e.sdk.calls[-1]["order_type"] == "LIMIT"
    future = now_utc() + timedelta(seconds=60)
    monkeypatch.setattr(engine_module, "now_utc", lambda: future)

    def unknown_cancel(**kwargs):
        raise TimeoutError("cancellation outcome unknown")

    e.sdk.cancel_order = unknown_cancel
    e.engine._sync_exit_pending_status("RELIANCE", e.engine._orders())
    assert trade["exit_order_id"] == "O3"
    assert trade["exit_market_required"]
    assert len(e.sdk.calls) == 3

    def partial_cancel(variety, order_id, **kwargs):
        assert order_id == "O3"
        e.sdk.book[2].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
        e.sdk.position_rows[0]["quantity"] = 8
        return order_id

    e.sdk.cancel_order = partial_cancel
    e.engine._sync_exit_pending_status("RELIANCE", e.engine._orders())
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
    assert e.sdk.calls[-1]["quantity"] == 8
    assert (
        e.engine.active_trades["RELIANCE"]["exit_intent_id"] == trade["exit_intent_id"]
    )


def test_hard_exit_escalates_normal_limit_using_same_intent(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    original_intent = trade["exit_intent_id"]
    assert e.sdk.calls[-1]["order_type"] == "LIMIT"
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert e.sdk.book[2]["status"] == "CANCELLED"
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
    assert trade["exit_intent_id"] == original_intent
    assert trade["exit_reason"] == "Stop Loss"
    intent = e.journal.get_order_intent(original_intent)
    assert intent["intent_type"] == "FLATTEN"
    assert intent["reason"] == "Stop Loss"
    assert len(e.sdk.calls) == 4


def test_rejected_replacement_stop_latches_exit_for_open_unbreached_trade(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.book[1].update(status="CANCELLED", pending_quantity=0)
    place = e.sdk.place_order

    def reject_stop(**kwargs):
        if kwargs["order_type"] == "SL":
            raise OrderSubmissionRejected("invalid protective request")
        return place(**kwargs)

    e.sdk.place_order = reject_stop
    e.engine.monitor_positions()
    assert trade["exit_pending"]
    assert trade["exit_order_id"] == "O3"
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
