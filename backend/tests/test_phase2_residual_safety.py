"""Order and fill facts must agree before a residual can size another order."""

from copy import deepcopy

from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import now_utc


def _stop_fill(env, quantity, status="COMPLETE"):
    env.sdk.book[1].update(status=status, filled_quantity=quantity, pending_quantity=0)
    env.sdk.executions.append(
        {
            "trade_id": "STOP-FILL",
            "order_id": "O2",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "transaction_type": "SELL",
            "quantity": quantity,
            "average_price": 94,
            "fill_timestamp": now_utc(),
        }
    )


def test_stop_fill_prevents_reversal_when_positions_endpoint_lags(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    stale_position = env.engine._positions()[0]
    _stop_fill(env, 10)

    env.engine._place_exit_order(stale_position, "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 2
    assert env.engine.active_trades["RELIANCE"]["exit_pending"] is True

    env.sdk.position_rows[0]["quantity"] = 0
    env.engine._place_exit_order(stale_position, "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 2
    assert env.engine.active_trades["RELIANCE"]["cleanup_pending"] is True


def test_partial_stop_already_reflected_in_position_is_not_subtracted_twice(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    _stop_fill(env, 4, "CANCELLED")
    env.sdk.position_rows[0]["quantity"] = 6
    residual = env.engine._positions()[0]

    env.engine._place_exit_order(residual, "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 3
    assert env.sdk.calls[-1]["quantity"] == 6
    assert env.sdk.calls[-1]["transaction_type"] == "SELL"


def test_completed_stop_order_count_blocks_lagging_positions_and_fills(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    env.sdk.book[1].update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    # Both executions and positions endpoints lag, but the terminal order
    # already proves all ten shares were sold.
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 2


def test_terminal_handoff_fact_survives_an_older_subsequent_order_snapshot(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    terminal_stop = deepcopy(env.engine._orders()[1])
    terminal_stop.update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    # Subsequent orders, executions, and positions reads still show the
    # pre-fill state. The handoff's existing terminal fact must prevail.
    trade = env.engine.active_trades["RELIANCE"]
    assert (
        env.engine._find_reconciled_residual("RELIANCE", trade, terminal_stop) is None
    )
    assert len(env.sdk.calls) == 2


def test_retained_terminal_stop_survives_stale_book_and_daily_rollover(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    terminal_stop = deepcopy(env.engine._orders()[1])
    terminal_stop.update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    env.engine._order_lifecycle.observe_order(
        trade["protection_intent_id"], terminal_stop
    )

    assert env.engine._find_order("O2")["status"] == "COMPLETE"
    assert env.engine._find_reconciled_residual("RELIANCE", trade) is None
    env.sdk.book = []
    assert env.engine._find_order("O2")["filled_quantity"] == 10
    assert env.engine._find_reconciled_residual("RELIANCE", trade) is None


def test_terminal_cache_does_not_hide_conflicting_current_order_identity(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    terminal_stop = deepcopy(env.engine._orders()[1])
    terminal_stop.update(status="CANCELLED", pending_quantity=0)
    env.engine._order_lifecycle.observe_order(
        trade["protection_intent_id"], terminal_stop
    )
    env.sdk.book[1]["product"] = "CNC"

    assert env.engine._find_order("O2") is None


def test_residual_reader_selects_exact_product_independent_of_row_order(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    holding = deepcopy(env.sdk.position_rows[0])
    holding.update(product="CNC", quantity=100)
    env.sdk.position_rows.insert(0, holding)
    trade = env.engine.active_trades["RELIANCE"]

    residual = env.engine._find_live_position_by_symbol("RELIANCE", trade)
    assert residual["product"] == "MIS"
    assert residual["quantity"] == 10


def test_reconciled_residual_refuses_an_unexplained_side_flip(lifecycle):
    env = lifecycle
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    env.sdk.position_rows[0]["quantity"] = -2
    trade = env.engine.active_trades["RELIANCE"]
    assert env.engine._find_reconciled_residual("RELIANCE", trade) is None
    assert trade["ownership_quarantined"] is True
