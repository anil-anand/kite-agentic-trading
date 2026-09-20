"""Operator obligations exercised through canonical SDK and lifecycle boundaries."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.config import config_manager
from backend.risk_rules import HardRiskReason
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.tests.test_review5_lifecycle import unknown_entry
from backend.time_utils import now_utc
from backend.trading_engine import TradingEngine


@pytest.fixture
def controls(lifecycle, monkeypatch):
    observed = SessionClock(SessionPolicy()).snapshot(
        datetime(2026, 9, 21, 5, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(
        TradingEngine,
        "_session_clock",
        lambda self: SimpleNamespace(snapshot=lambda _: observed),
    )
    monkeypatch.setattr(
        TradingEngine,
        "_activate_supervision",
        lambda self: setattr(self, "_supervision_active", True),
    )
    lifecycle.cancelled = []
    lifecycle.risk.kill_switch_active = False
    return lifecycle


def terminal_cancellation(env):
    def cancel(variety, order_id, **kwargs):
        env.cancelled.append(order_id)
        record = next(order for order in env.sdk.book if order["order_id"] == order_id)
        record.update(status="CANCELLED", pending_quantity=0)
        return order_id

    env.sdk.cancel_order = cancel
    return cancel


def open_managed_position(env):
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    assert env.engine.active_trades["RELIANCE"]["entry_state"] == "OPEN"


def fill_reduction(env, order_id, quantity=None, status="COMPLETE"):
    order = next(order for order in env.sdk.book if order["order_id"] == order_id)
    row = next(
        row
        for row in env.sdk.position_rows
        if row["tradingsymbol"] == order["tradingsymbol"]
        and row["product"] == order["product"]
    )
    quantity = order["quantity"] if quantity is None else quantity
    assert quantity <= abs(row["quantity"]), "reduction would reverse broker exposure"
    assert (order["transaction_type"] == "SELL") == (row["quantity"] > 0)
    order.update(
        filled_quantity=quantity,
        pending_quantity=0
        if status in {"COMPLETE", "CANCELLED", "REJECTED"}
        else order["quantity"] - quantity,
        status=status,
    )
    is_sell = order["transaction_type"] == "SELL"
    row["quantity"] += -quantity if is_sell else quantity
    side = "sell" if is_sell else "buy"
    row[f"day_{side}_quantity"] = row.get(f"day_{side}_quantity", 0) + quantity
    row[f"{side}_quantity"] = row.get(f"{side}_quantity", 0) + quantity
    row[f"{side}_value"] = row.get(f"{side}_value", 0) + quantity * 100
    env.sdk.executions.append(
        {
            "trade_id": f"REDUCE-{len(env.sdk.executions)}",
            "order_id": order_id,
            "tradingsymbol": order["tradingsymbol"],
            "exchange": order["exchange"],
            "product": order["product"],
            "instrument_token": 111,
            "transaction_type": order["transaction_type"],
            "quantity": quantity,
            "average_price": 100,
            "fill_timestamp": now_utc(),
        }
    )


def test_emergency_latch_survives_new_engine_and_paused_resume(controls):
    env = controls
    response = env.engine.request_emergency_flatten("account")
    assert response["accepted"] and response["pending"]
    assert env.sdk.calls == []
    restarted = TradingEngine()
    restarted.resume_supervision()
    assert (
        restarted._hard_flatten_reason
        == HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value
    )
    assert restarted._hard_flatten_pending
    assert not restarted.running
    assert restarted._supervision_active


def test_emergency_cancels_zero_fill_entry_and_settles_terminal_cleanup(controls):
    env = controls
    unknown_entry(env)
    terminal_cancellation(env)
    env.engine.request_emergency_flatten("account")
    env.engine.monitor_positions()
    env.engine.monitor_positions()

    assert env.cancelled == ["O1"]
    assert env.sdk.book[0]["status"] == "CANCELLED"
    assert len(env.sdk.calls) == 1
    assert env.engine.active_trades == {}
    assert env.risk.pending_entry_reservation_count == 0
    assert env.engine._reserved_entry_margin == 0
    assert not env.engine._hard_flatten_pending
    assert not env.engine._pending_lifecycle_obligations()
    saved = config_manager.load_operator_state("LIVE", "acct-A")
    assert saved["hardFlattenPending"] is False


def test_late_partial_entry_fill_is_flattened_from_confirmed_residual(controls):
    env = controls
    env.sdk.after_entry = lambda: env.sdk.fill_entry(4, "OPEN")
    assert env.engine.execute_signal(env.signal) is False
    assert env.sdk.book[1]["quantity"] == 4
    cancel = terminal_cancellation(env)

    def late_fill_before_cancel_ack(variety, order_id, **kwargs):
        if order_id == "O1":
            env.sdk.fill_entry(10, "CANCELLED")
            env.cancelled.append(order_id)
            return order_id
        return cancel(variety, order_id, **kwargs)

    env.sdk.cancel_order = late_fill_before_cancel_ack
    env.engine.request_emergency_flatten("account")
    env.engine.monitor_positions()
    reduction = env.sdk.book[-1]
    assert reduction["order_type"] == "MARKET"
    assert reduction["transaction_type"] == "SELL"
    assert reduction["quantity"] == 10
    assert env.sdk.book[0]["status"] == "CANCELLED"
    assert env.sdk.book[1]["status"] == "CANCELLED"
    fill_reduction(env, reduction["order_id"])
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.sdk.position_rows[0]["quantity"] == 0
    assert len(env.sdk.calls) == 3
    assert env.engine.active_trades == {}
    assert not env.engine._hard_flatten_pending
    assert env.risk.pending_entry_reservation_count == 0


def test_daily_loss_flatten_remains_pending_until_fill_and_order_clean(controls):
    env = controls
    open_managed_position(env)
    terminal_cancellation(env)
    env.risk.kill_switch_active = True
    env.engine.monitor_positions()
    assert env.engine._hard_flatten_reason == HardRiskReason.RISK_DAILY_LOSS.value
    assert env.engine._hard_flatten_pending
    assert env.sdk.calls[-1]["quantity"] == 10
    assert env.sdk.calls[-1]["order_type"] == "MARKET"
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    fill_reduction(env, "O3")
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert not env.engine._hard_flatten_pending
    assert not env.engine._pending_lifecycle_obligations()
    assert env.journal.get_trades()[0]["status"] == "CLOSED"
    assert env.risk.kill_switch_active
    assert len(env.sdk.calls) == 3


@pytest.mark.parametrize("partial", [False, True])
def test_operator_close_manages_untracked_position_in_confirm_mode(controls, partial):
    env = controls
    env.sdk.position_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "quantity": 7,
            "average_price": 100,
            "last_price": 100,
            "day_buy_quantity": 0,
            "day_sell_quantity": 0,
            "buy_quantity": 0,
            "sell_quantity": 0,
            "buy_value": 0,
            "sell_value": 0,
            "realised": 0,
            "unrealised": 0,
        }
    ]
    position_key = env.client.get_positions_snapshot().net[0].key.as_string()
    response = env.engine.request_operator_close(position_key)
    assert response["pending"]
    assert env.sdk.calls == []
    env.engine.monitor_positions()
    assert env.sdk.calls[-1]["quantity"] == 7
    assert env.sdk.calls[-1]["transaction_type"] == "SELL"
    assert env.sdk.calls[-1]["order_type"] == "MARKET"
    if partial:
        fill_reduction(env, "O1", quantity=3, status="CANCELLED")
        env.engine.monitor_positions()
        assert len(env.sdk.calls) == 2
        assert env.sdk.calls[-1]["quantity"] == 4
        fill_reduction(env, "O2")
    else:
        fill_reduction(env, "O1")
    for _ in range(2):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert position_key not in env.engine._operator_close_keys
    assert len(env.sdk.calls) == (2 if partial else 1)


def test_account_flatten_manages_same_symbol_products_sequentially(controls):
    env = controls
    open_managed_position(env)
    cnc = deepcopy(env.sdk.position_rows[0])
    cnc.update(
        product="CNC",
        quantity=3,
        day_buy_quantity=0,
        buy_quantity=0,
        buy_value=0,
        average_price=100,
    )
    env.sdk.position_rows.insert(0, cnc)
    terminal_cancellation(env)
    env.engine.request_emergency_flatten("account")
    env.engine.monitor_positions()
    assert env.sdk.calls[-1]["product"] == "MIS"
    assert env.sdk.calls[-1]["quantity"] == 10
    assert not env.engine.active_trades["RELIANCE"].get("ownership_quarantined")
    fill_reduction(env, "O3")
    env.engine.monitor_positions()
    env.engine.monitor_positions()
    assert env.sdk.calls[-1]["product"] == "CNC"
    assert env.sdk.calls[-1]["quantity"] == 3
    assert len(env.sdk.calls) == 4
    fill_reduction(env, "O4")
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert not env.engine._hard_flatten_pending
    assert all(row["quantity"] == 0 for row in env.sdk.position_rows)
    assert len(env.sdk.calls) == 4


@pytest.mark.parametrize("control", ["close", "emergency"])
@pytest.mark.parametrize("known_role", [False, True])
def test_untracked_position_close_waits_for_existing_native_reducer_handoff(
    controls, control, known_role
):
    env = controls
    env.sdk.position_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "quantity": 7,
            "average_price": 100,
            "last_price": 100,
        }
    ]
    env.sdk.book = [
        {
            "order_id": "MANUAL-STOP",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "transaction_type": "SELL",
            "quantity": 7,
            "filled_quantity": 0,
            "pending_quantity": 7,
            "status": "TRIGGER PENDING",
            "order_type": "SL",
            "trigger_price": 95,
            "price": 94,
        }
    ]
    if known_role:
        config_manager.add_app_order_id("MANUAL-STOP", "PROTECTION")
    if control == "close":
        position_key = env.client.get_positions_snapshot().net[0].key.as_string()
        env.engine.request_operator_close(position_key)
    else:
        env.engine.request_emergency_flatten("account")
    # The SDK's default cancel acknowledges the request without terminating
    # the stop. Both orders could still execute if a new sell were submitted.
    env.engine.monitor_positions()
    assert env.sdk.calls == []
    assert env.sdk.book[0]["status"] == "TRIGGER PENDING"

    terminal_cancellation(env)
    env.engine.monitor_positions()
    assert env.sdk.book[0]["status"] == "CANCELLED"
    assert len(env.sdk.calls) == 1
    assert env.sdk.calls[0]["quantity"] == 7
    assert env.sdk.calls[0]["transaction_type"] == "SELL"
    fill_reduction(env, "O2")
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert not env.engine._hard_flatten_pending
    assert all(row["quantity"] == 0 for row in env.sdk.position_rows)
    assert len(env.sdk.calls) == 1


@pytest.mark.parametrize("control", ["close", "emergency"])
@pytest.mark.parametrize("filled", [3, 7])
def test_external_stop_fill_during_cancel_blocks_lagging_position_reduction(
    controls, control, filled
):
    env = controls
    env.sdk.position_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "quantity": 7,
            "average_price": 100,
            "last_price": 100,
        }
    ]
    env.sdk.book = [
        {
            "order_id": "MANUAL-STOP",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "transaction_type": "SELL",
            "quantity": 7,
            "filled_quantity": 0,
            "pending_quantity": 7,
            "status": "TRIGGER PENDING",
            "order_type": "SL",
            "trigger_price": 95,
            "price": 94,
        }
    ]

    def fill_during_cancel(variety, order_id, **kwargs):
        order = env.sdk.book[0]
        order.update(status="CANCELLED", filled_quantity=filled, pending_quantity=0)
        # Both position and executions responses lag the definitive terminal
        # order's fill count; they cannot authorize a full-size second sell.
        return order_id

    env.sdk.cancel_order = fill_during_cancel
    if control == "close":
        position_key = env.client.get_positions_snapshot().net[0].key.as_string()
        env.engine.request_operator_close(position_key)
    else:
        env.engine.request_emergency_flatten("account")
    env.engine.monitor_positions()
    assert env.sdk.calls == []

    env.sdk.position_rows[0]["quantity"] = 7 - filled
    env.engine.monitor_positions()
    if filled == 7:
        assert env.sdk.calls == []
    else:
        assert len(env.sdk.calls) == 1
        assert env.sdk.calls[0]["quantity"] == 4
