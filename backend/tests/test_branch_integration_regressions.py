"""Offline regressions for the branch's integrated broker/exit boundaries."""

from copy import deepcopy
from datetime import timedelta

import pytest

from backend.config import config_manager
from backend.financial_eligibility import verified_outcome
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import now_utc
from backend.trading_engine import TradingEngine


@pytest.mark.parametrize("restart", [False, True])
def test_terminal_cumulative_fills_cannot_regress(lifecycle, restart):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    stop = deepcopy(env.engine._orders()[1])
    stop.update(status="CANCELLED", filled_quantity=4, pending_quantity=0)
    env.engine._order_lifecycle.observe_order(trade["protection_intent_id"], stop)
    stop["filled_quantity"] = 2
    env.engine._order_lifecycle.observe_order(trade["protection_intent_id"], stop)
    if restart:
        env.engine._order_lifecycle = OrderLifecycleCoordinator(
            TradeJournal(env.journal.db_path)
        )
    env.sdk.book[1].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
    env.sdk.fail_fills = True
    env.sdk.position_rows[0]["quantity"] = 8
    assert env.engine._find_order("O2")["filled_quantity"] == 4
    assert (
        env.engine._find_reconciled_residual("RELIANCE", trade, critical=True) is None
    )
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 2
    env.sdk.position_rows[0]["quantity"] = 6
    assert (
        env.engine._find_reconciled_residual("RELIANCE", trade, critical=True)[
            "quantity"
        ]
        == 6
    )
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert env.sdk.calls[-1]["quantity"] == 6
    assert len(env.sdk.calls) == 3


def test_delayed_working_status_cannot_erase_additional_execution(lifecycle):
    env = lifecycle
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    order = deepcopy(env.engine._orders()[1])
    order.update(status="CANCELLED", filled_quantity=4, pending_quantity=0)
    env.engine._order_lifecycle.observe_order(trade["protection_intent_id"], order)
    order.update(status="OPEN", filled_quantity=6, pending_quantity=4)
    result = env.engine._order_lifecycle.observe_order(
        trade["protection_intent_id"], order
    )
    assert result.state == "CANCELLED"
    restored = TradeJournal(env.journal.db_path)
    retained = restored.get_terminal_order_fact(
        "O2", namespace="LIVE", account_id="acct-A"
    )
    assert retained["status"] == "CANCELLED"
    assert retained["filled_quantity"] == 6


@pytest.mark.parametrize("quantity,status", [(10, "COMPLETE"), (4, "CANCELLED")])
def test_entry_binding_uses_exchange_update_time(lifecycle, quantity, status):
    env = lifecycle
    first_fill = now_utc().replace(microsecond=0)
    registered = first_fill - timedelta(seconds=5)
    terminal = first_fill + timedelta(seconds=1)

    def fill():
        env.sdk.fill_entry(quantity, status)
        env.sdk.executions[0]["fill_timestamp"] = first_fill
        env.sdk.book[0].update(
            exchange_timestamp=registered, exchange_update_timestamp=terminal
        )

    env.sdk.after_entry = fill
    assert env.engine.execute_signal(env.signal)
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    trade = restarted.active_trades["RELIANCE"]
    key = trade["exit_management_position_key"]
    binding = env.journal.get_position_thesis(key)["payload"]["fill_binding"]
    assert binding["entry_first_fill_at"] == first_fill.isoformat()
    assert binding["entry_terminal_at"] == terminal.isoformat()
    assert trade["entry_state"] == "OPEN"


@pytest.mark.parametrize(
    "handoff_fill,restart", [(0, False), (4, False), (4, True), (10, True)]
)
def test_adoption_settles_manual_stop_before_replacement(
    lifecycle, handoff_fill, restart
):
    env = lifecycle
    env.sdk.book = [
        {
            "order_id": "MANUAL-ENTRY",
            "quantity": 10,
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "order_type": "MARKET",
        }
    ]
    env.sdk.fill_entry()
    env.sdk.quote_price = 102
    env.sdk.position_rows[0]["last_price"] = 102
    env.sdk.book.append(
        {
            **env.sdk.book[0],
            "order_id": "MANUAL-STOP",
            "transaction_type": "SELL",
            "order_type": "SL",
            "trigger_price": 100,
            "price": 99,
            "status": "TRIGGER PENDING",
            "filled_quantity": 0,
            "pending_quantity": 10,
        }
    )

    def cancel(variety, order_id, **kwargs):
        stop = next(row for row in env.sdk.book if row["order_id"] == order_id)
        stop.update(
            status="CANCELLED", filled_quantity=handoff_fill, pending_quantity=0
        )
        env.sdk.position_rows[0]["quantity"] = 10 - handoff_fill
        return order_id

    if not restart:
        env.sdk.cancel_order = cancel
    env.engine._adopt_position(env.engine._positions()[0])
    if restart:
        assert env.sdk.calls == []  # Cancellation acknowledgement is insufficient.
        config_manager.save_active_trades({})
        env.sdk.cancel_order = cancel
        env.engine = TradingEngine()
        env.engine.reconcile_active_trades()
        env.engine.monitor_positions()
    working = [o for o in env.sdk.book if o["status"] == "TRIGGER PENDING"]
    if handoff_fill == 10:
        assert working == []
        assert env.sdk.calls == []
        return
    assert len(working) == 1
    assert working[0]["order_id"] != "MANUAL-STOP"
    assert working[0]["quantity"] == 10 - handoff_fill
    assert working[0]["trigger_price"] == 100


@pytest.mark.parametrize("restart", [False, True])
def test_hard_exit_keeps_manual_partial_reductions(lifecycle, restart):
    env = lifecycle
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)

    def cancel(variety, order_id, **kwargs):
        next(o for o in env.sdk.book if o["order_id"] == order_id).update(
            status="CANCELLED", pending_quantity=0
        )
        return order_id

    env.sdk.cancel_order = cancel
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Target")
    env.sdk.book[2].update(status="CANCELLED", filled_quantity=3, pending_quantity=0)
    for order_id, quantity in [("O3", 3), ("MANUAL", 2)]:
        env.sdk.executions.append(
            {
                **env.sdk.executions[0],
                "trade_id": "F-" + order_id,
                "order_id": order_id,
                "transaction_type": "SELL",
                "quantity": quantity,
                "fill_timestamp": now_utc(),
                "average_price": 102,
            }
        )
    env.sdk.position_rows[0]["quantity"] = 5
    trade = env.engine.active_trades["RELIANCE"]
    env.engine._record_lifecycle_fills("RELIANCE", trade)
    env.sdk.position_rows[0]["quantity"] = 7
    assert (
        env.engine._find_reconciled_residual("RELIANCE", trade, critical=True) is None
    )
    env.sdk.position_rows[0]["quantity"] = 5
    assert (
        env.engine._find_reconciled_residual("RELIANCE", trade, critical=True)[
            "quantity"
        ]
        == 5
    )
    if restart:
        env.sdk.fail_fills = True
        env.engine._persist_trades()
        env.engine = TradingEngine()
        env.engine.reconcile_active_trades()
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Stop Loss")
    reductions = [
        o
        for o in env.sdk.calls
        if o["order_type"] == "MARKET" and o["transaction_type"] == "SELL"
    ]
    assert len(reductions) == 1
    assert reductions[0]["quantity"] == 5


@pytest.mark.parametrize("later_session", [False, True])
def test_missing_external_close_cannot_claim_a_later_manual_round_trip(
    lifecycle, later_session
):
    env = lifecycle
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    first = env.sdk.executions[0]["fill_timestamp"]
    later = first + (timedelta(days=1) if later_session else timedelta(minutes=1))
    for side, price, at in [
        ("BUY", 120, later),
        ("SELL", 121, later + timedelta(seconds=5)),
    ]:
        env.sdk.executions.append(
            {
                **env.sdk.executions[0],
                "trade_id": "LATER-" + side,
                "order_id": "MANUAL-" + side,
                "transaction_type": side,
                "fill_timestamp": at,
                "average_price": price,
            }
        )
    env.sdk.position_rows[0]["quantity"] = 0
    assert env.engine._reconcile_execution("RELIANCE", trade) == (
        None,
        "UNRECONCILED",
        None,
        None,
    )
    assert not env.engine._journal_external_close("RELIANCE")
    assert not verified_outcome(env.journal.get_trades()[0])
