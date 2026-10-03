"""Exit obligations survive contention and independently confirmed flatness."""

import threading

import pytest

from backend.broker_models import OrderRole
from backend.order_lifecycle import IntentType
from backend.risk_rules import HardRiskReason
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.trading_engine import TradingEngine

SYMBOL = "RELIANCE"
HARD_REASON = HardRiskReason.RISK_CATASTROPHIC_STOP.value


def _open_position(env):
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)
    return env.engine._positions()[0]


def _confirm_cancellations(env):
    def cancel(variety, order_id, **kwargs):
        for order in env.sdk.book:
            if order["order_id"] == order_id:
                order.update(status="CANCELLED", pending_quantity=0)
        return order_id

    env.sdk.cancel_order = cancel


def _fill_stop(env):
    stop = env.sdk.book[1]
    stop.update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    env.sdk.position_rows = []
    env.sdk.executions.append(
        {
            **env.sdk.executions[0],
            "trade_id": "STOP-FILL",
            "order_id": stop["order_id"],
            "transaction_type": "SELL",
            "average_price": 95,
        }
    )


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("reason", [HARD_REASON, "Stop Loss"])
def test_contended_hard_exit_is_durable_and_retries_after_price_recovers(
    lifecycle, restart, reason
):
    env = lifecycle
    engine = env.engine
    position = _open_position(env)
    _confirm_cancellations(env)
    key = engine._trade_position_key(SYMBOL, engine.active_trades[SYMBOL])
    finished = threading.Event()
    errors = []

    def request_exit():
        try:
            engine._place_exit_order({**position, "last_price": 94}, SYMBOL, reason)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    # A stop modification can own both locks while waiting for the broker.
    # Latching risk must not depend on either broker-operation lock releasing.
    worker = threading.Thread(target=request_exit)
    try:
        with engine._management_lock(SYMBOL):
            with engine._order_lifecycle._position_lock(key):
                worker.start()
                assert finished.wait(2)
                assert not errors
                trade = engine.active_trades[SYMBOL]
                assert trade["exit_pending"]
                intent_id = trade["exit_intent_id"]
                intent = env.journal.get_order_intent_projection(intent_id)
                assert intent["intent_type"] == "FLATTEN"
                assert intent["reason"] == reason
                assert intent["latched"]
                assert not intent["latest_attempt"]
                assert len(env.sdk.calls) == 2
    finally:
        # Join outside the locks even when a regression blocks latching.
        worker.join(2)
    assert not worker.is_alive()

    if restart:
        engine = TradingEngine()
        engine._restore_lifecycle_owners()
        assert engine.active_trades[SYMBOL]["exit_intent_id"] == intent_id
    # The fresh broker price is healthy again. The durable obligation must
    # still execute, with one reduction and the same parent intent.
    engine.monitor_positions()
    engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    assert env.sdk.calls[-1]["order_type"] == "MARKET"
    assert env.sdk.calls[-1]["quantity"] == 10
    assert engine.active_trades[SYMBOL]["exit_intent_id"] == intent_id
    assert engine.active_trades[SYMBOL]["exit_reason"] == reason


def test_hard_exit_escalates_a_normal_handoff_already_in_flight(lifecycle):
    env = lifecycle
    position = _open_position(env)
    cancelling = threading.Event()
    release = threading.Event()
    errors = []

    def cancel(variety, order_id, **kwargs):
        cancelling.set()
        assert release.wait(3)
        env.sdk.book[1].update(status="CANCELLED", pending_quantity=0)
        return order_id

    env.sdk.cancel_order = cancel

    def normal_exit():
        try:
            env.engine._place_exit_order(position, SYMBOL, "Target")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=normal_exit)
    worker.start()
    try:
        assert cancelling.wait(2)
        intent_id = env.engine.active_trades[SYMBOL]["exit_intent_id"]
        env.engine._place_exit_order(position, SYMBOL, HARD_REASON)
        intent = env.journal.get_order_intent_projection(intent_id)
        assert intent["intent_type"] == "FLATTEN"
        assert intent["reason"] == HARD_REASON
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert env.engine.active_trades[SYMBOL]["exit_reason"] == HARD_REASON
    assert len(env.sdk.calls) == 3
    assert env.sdk.calls[-1]["order_type"] == "MARKET"


@pytest.mark.parametrize("restart", [False, True])
def test_contended_escalation_replaces_an_in_flight_limit_after_acknowledgement(
    lifecycle, restart
):
    env = lifecycle
    position = _open_position(env)
    _confirm_cancellations(env)
    submitted = threading.Event()
    release = threading.Event()
    errors = []
    original_place = env.sdk.place_order

    def place(**kwargs):
        order_id = original_place(**kwargs)
        if order_id == "O3":
            submitted.set()
            assert release.wait(3)
        return order_id

    env.sdk.place_order = place

    def normal_exit():
        try:
            env.engine._place_exit_order(position, SYMBOL, "Target")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=normal_exit)
    worker.start()
    try:
        assert submitted.wait(2)
        old_checkpoint = dict(env.engine.active_trades[SYMBOL])
        intent_id = old_checkpoint["exit_intent_id"]
        env.engine._place_exit_order(position, SYMBOL, HARD_REASON)
        assert env.journal.get_order_intent(intent_id)["reason"] == HARD_REASON
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert env.sdk.calls[-1]["order_type"] == "LIMIT"

    engine = env.engine
    if restart:
        engine = TradingEngine()
        # JSON may predate both escalation and the acknowledgement. SQLite
        # must restore urgency as well as the pending broker order's identity.
        engine.active_trades[SYMBOL] = old_checkpoint
        engine._restore_lifecycle_owners()
    assert engine.active_trades[SYMBOL]["exit_market_required"]
    engine.monitor_positions()
    engine.monitor_positions()
    assert env.sdk.book[2]["status"] == "CANCELLED"
    assert len(env.sdk.calls) == 4
    assert env.sdk.calls[-1]["order_type"] == "MARKET"
    assert env.sdk.calls[-1]["quantity"] == 10
    assert engine.active_trades[SYMBOL]["exit_intent_id"] == intent_id
    assert engine.active_trades[SYMBOL]["exit_reason"] == HARD_REASON


@pytest.mark.parametrize("empty_order_book", [False, True])
def test_flat_pending_handoff_retires_without_an_exit_order(
    lifecycle, empty_order_book
):
    env = lifecycle
    position = _open_position(env)
    # Cancellation is unresolved, so the reduction has an intent but no attempt.
    env.engine._place_exit_order(position, SYMBOL, HARD_REASON)
    trade = dict(env.engine.active_trades[SYMBOL])
    assert trade["exit_pending"]
    assert not trade["exit_order_id"]
    _fill_stop(env)
    if empty_order_book:
        env.engine._reconcile_durable_attempts(env.engine._orders())
        env.sdk.book = []

    env.engine.monitor_positions()

    assert SYMBOL not in env.engine.active_trades
    assert len(env.sdk.calls) == 2
    assert not env.journal.get_order_intent(trade["exit_intent_id"])["active"]
    assert env.journal.get_trade(trade["trade_id"])["status"] == "CLOSED"
    checkpoint = env.journal.get_managed_position(trade["exit_management_position_key"])
    assert checkpoint["state"]["state"]["exposure"] == "CLOSED"


@pytest.mark.parametrize("terminal_observed", [False, True])
def test_missing_exit_order_requires_terminal_evidence(lifecycle, terminal_observed):
    env = lifecycle
    position = _open_position(env)
    _confirm_cancellations(env)
    env.engine._place_exit_order(position, SYMBOL, HARD_REASON)
    trade = dict(env.engine.active_trades[SYMBOL])
    if terminal_observed:
        env.sdk.book[2].update(status="CANCELLED", pending_quantity=0)
        env.engine._reconcile_durable_attempts(env.engine._orders())
    env.sdk.book.pop()  # The exit disappears from the current broker order book.
    _fill_stop(env)

    env.engine.monitor_positions()

    assert (SYMBOL not in env.engine.active_trades) is terminal_observed
    assert env.journal.get_order_intent(trade["exit_intent_id"])["active"] is (
        not terminal_observed
    )
    assert len(env.sdk.calls) == 3


@pytest.mark.parametrize(
    "uncertainty", ["orders", "fills", "position_lag", "working", "unknown_exit"]
)
def test_flat_pending_handoff_retains_unresolved_obligations(lifecycle, uncertainty):
    env = lifecycle
    position = _open_position(env)
    env.engine._place_exit_order(position, SYMBOL, HARD_REASON)
    trade = dict(env.engine.active_trades[SYMBOL])
    _fill_stop(env)
    if uncertainty == "orders":
        env.sdk.fail_orders = True
    elif uncertainty == "fills":
        env.sdk.fail_fills = True
    elif uncertainty == "position_lag":
        env.sdk.executions.pop()
        env.sdk.book[1].update(status="CANCELLED", filled_quantity=0)
    elif uncertainty == "working":
        env.sdk.book.append(
            {
                **env.sdk.book[1],
                "order_id": "EXTERNAL-WORKING",
                "status": "OPEN",
                "filled_quantity": 0,
                "pending_quantity": 10,
                "tag": "manual",
            }
        )
    else:

        def unknown_submit(tag):
            raise TimeoutError("exit acknowledgement unavailable")

        result = env.engine._order_lifecycle.submit(
            position_key=env.engine._trade_position_key(SYMBOL, trade),
            intent_type=IntentType.FLATTEN,
            role=OrderRole.REDUCTION,
            side="SELL",
            quantity=10,
            payload={},
            existing_intent_id=trade["exit_intent_id"],
            submit_order=unknown_submit,
        )
        assert result.state == "UNKNOWN"
        assert result.broker_order_id is None

    env.engine.monitor_positions()

    assert SYMBOL in env.engine.active_trades
    assert env.journal.get_order_intent(trade["exit_intent_id"])["active"]
    assert len(env.sdk.calls) == 2


def test_orphaned_exit_flag_still_uses_confirmed_external_close(lifecycle):
    env = lifecycle
    _open_position(env)
    trade = env.engine.active_trades[SYMBOL]
    trade["exit_pending"] = True
    assert not trade.get("exit_intent_id")
    assert not trade.get("exit_order_id")
    _fill_stop(env)
    env.engine._external_close_grace_seconds = 0

    env.engine.monitor_positions()
    env.engine.monitor_positions()

    assert SYMBOL not in env.engine.active_trades
    assert env.journal.get_trade(trade["trade_id"])["status"] == "CLOSED"
    assert len(env.sdk.calls) == 2
