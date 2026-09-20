"""Execution evidence must survive missing prices, lagged ledgers and restarts."""

from copy import deepcopy
from datetime import timedelta

import pytest

from backend.config import config_manager
from backend.financial_eligibility import verified_outcome
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import as_utc, now_utc
from backend.trading_engine import TradingEngine


@pytest.mark.parametrize("reported_average", [None, 104])
def test_complete_entry_fills_supply_the_execution_price(lifecycle, reported_average):
    e = lifecycle

    def fill():
        e.sdk.fill_entry()
        e.sdk.position_rows[0]["average_price"] = reported_average

    e.sdk.after_entry = fill
    assert e.engine.execute_signal(e.signal)
    assert e.journal.get_trades()[0]["entry_price"] == 101
    assert e.engine.active_trades["RELIANCE"]["entry_price"] == 101
    assert config_manager.load_active_trades()["RELIANCE"]["entry_price"] == 101


def test_missing_execution_price_retains_protected_owner_until_fills_recover(lifecycle):
    e = lifecycle

    def fill():
        e.sdk.fill_entry()
        e.sdk.position_rows[0]["average_price"] = None
        e.sdk.fail_fills = True

    e.sdk.after_entry = fill
    assert not e.engine.execute_signal(e.signal)
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["entry_price"] is None
    assert trade["signal_entry_price"] == 100
    assert trade["entry_state"] == "RECOVERY_REQUIRED"
    assert trade["stop_order_id"] == "O2"
    assert e.journal.get_trades() == []
    assert e.risk.pending_entry_reservation_count == 1
    assert len(e.sdk.calls) == 2
    for _ in range(2):
        e.engine.monitor_positions()
    assert len(e.sdk.calls) == 2
    e.sdk.fail_fills = False
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted.monitor_positions()
    assert restarted.active_trades["RELIANCE"]["entry_state"] == "OPEN"
    assert e.journal.get_trades()[0]["entry_price"] == 101
    assert e.risk.pending_entry_reservation_count == 0
    assert len(e.sdk.calls) == 2


def close_with_stop(e):
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "STOP-FILL",
            "order_id": "O2",
            "transaction_type": "SELL",
            "quantity": 10,
            "average_price": 95,
            "fill_timestamp": now_utc(),
        }
    )
    e.sdk.book[1].update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10)


@pytest.mark.parametrize("visible_entry_quantity", [4, 11])
def test_incomplete_entry_allocation_cannot_become_verified_outcome(
    lifecycle, visible_entry_quantity
):
    e = lifecycle
    e.sdk.after_entry = e.sdk.fill_entry
    assert e.engine.execute_signal(e.signal)
    close_with_stop(e)
    complete_entry = deepcopy(e.sdk.executions[0])
    e.sdk.executions[0]["quantity"] = visible_entry_quantity
    assert not e.engine._journal_external_close("RELIANCE")
    row = e.journal.get_trades()[0]
    assert row["status"] == "RECONCILIATION_PENDING"
    assert not verified_outcome(row)
    e.sdk.executions[0] = complete_entry
    TradingEngine()._reconcile_journal_trades()
    row = e.journal.get_trades()[0]
    assert verified_outcome(row)
    assert row["gross_pnl"] == -60
    assert row["net_pnl"] < -60


@pytest.mark.parametrize("already_closed", [False, True])
def test_late_entry_time_repairs_active_and_journal_before_verified_close(
    lifecycle, already_closed
):
    e = lifecycle
    actual_time = now_utc() - timedelta(minutes=5)

    def fill():
        e.sdk.fill_entry()
        e.sdk.executions[0]["fill_timestamp"] = None

    e.sdk.after_entry = fill
    assert e.engine.execute_signal(e.signal)
    assert e.journal.get_trades()[0]["entry_time"] is None
    close_with_stop(e)
    if already_closed:
        assert e.engine._journal_external_close("RELIANCE")
        assert e.journal.get_trades()[0]["entry_time"] is None
        assert verified_outcome(e.journal.get_trades()[0])
        e.engine.active_trades.clear()
    e.sdk.executions[0]["fill_timestamp"] = actual_time
    if already_closed:
        TradingEngine()._reconcile_journal_trades()
    else:
        assert e.engine._journal_external_close("RELIANCE")
    row = e.journal.get_trades()[0]
    assert as_utc(row["entry_time"]) == actual_time
    if not already_closed:
        assert e.engine.active_trades["RELIANCE"]["entry_time"] == actual_time
    assert verified_outcome(row)
    assert e.journal.get_verified_todays_outcomes()[0]["id"] == row["id"]


def test_unknown_execution_time_does_not_disable_existing_thesis_reevaluation(
    lifecycle, monkeypatch
):
    import backend.trading_engine as engine_module

    e = lifecycle

    def fill():
        e.sdk.fill_entry()
        e.sdk.executions[0]["fill_timestamp"] = None

    e.sdk.after_entry = fill
    assert e.engine.execute_signal(e.signal)
    trade = e.engine.active_trades["RELIANCE"]
    trade["entry_observed_at"] = now_utc() - timedelta(minutes=31)
    trade["last_reeval_time"] = None
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    evaluated, exits = [], []
    monkeypatch.setattr(
        engine_module.scanner,
        "evaluate_position",
        lambda *args: (
            evaluated.append(args)
            or {"assessment_available": True, "buy_signals": 0, "sell_signals": 2}
        ),
    )
    monkeypatch.setattr(
        restarted,
        "_exit_position",
        lambda position, symbol, reason: exits.append((symbol, reason)),
    )
    restarted._reevaluate_positions()
    assert len(evaluated) == 1
    assert exits[0][0] == "RELIANCE"
    assert "Thesis invalidated" in exits[0][1]
    assert restarted.active_trades["RELIANCE"]["entry_time"] is None
    assert e.journal.get_trades()[0]["entry_time"] is None
