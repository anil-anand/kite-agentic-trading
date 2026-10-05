"""Phase-8 broker execution-model contracts."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.backtesting.paper_broker import PaperBroker
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.broker_models import OrderRole
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator

UTC = timezone.utc


def _at(minutes=0):
    return datetime(2026, 9, 21, 4, 20, tzinfo=UTC) + timedelta(minutes=minutes)


def _candle(*, opening, high, low, close, minutes=5):
    return pd.Series(
        {
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "date": _at(minutes),
        }
    )


def test_gap_stop_applies_slippage_once_and_records_canonical_facts():
    broker = SimulatedBroker()
    broker.place_market_order(
        "TEST", "BUY", 10, 100.0, _at(), {"stopLoss": 95.0, "target": 120.0}
    )
    broker.process_candle("TEST", _candle(opening=90, high=92, low=85, close=88))

    trade = broker.trades[0]
    # The gap executable price is 90; one 5bp sell-side application is 89.955
    # rounded once.  The old path multiplied stop slippage twice.
    assert trade["exit_price"] == 89.95
    assert trade["exit_reason"] == "stop_loss"
    assert broker.order_snapshot(_at(5)).orders[-1].filled_quantity == 10
    assert broker.fill_snapshot(_at(5)).fills[-1].fill_price == 89.95


def test_partial_stop_keeps_residual_position_and_correctly_sized_protection():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    broker.place_market_order("TEST", "BUY", 10, 100.0, _at(), {"stopLoss": 95.0})
    broker.process_candle("TEST", _candle(opening=94, high=95, low=90, close=92))

    position = broker.positions["TEST"]
    stop = broker.orders[position["stop_order_id"]]
    assert position["quantity"] == 5
    assert stop["filled_quantity"] == 5
    assert stop["remaining_quantity"] == 5
    # The broker retains original order quantity; remaining coverage is what
    # protects the five-share residual.
    assert stop["quantity"] == 10


def test_stop_target_ambiguity_is_conservative_and_post_exit_extrema_are_not_granted():
    broker = SimulatedBroker()
    broker.place_market_order(
        "TEST", "BUY", 10, 100.0, _at(), {"stopLoss": 95.0, "target": 110.0}
    )
    broker.process_candle("TEST", _candle(opening=100, high=112, low=94, close=105))

    assert broker.trades[0]["exit_reason"] == "stop_loss"
    assert broker.trades[0]["ambiguity"] is True
    # Stop-first cannot truthfully award the later high as MFE.
    assert broker.trades[0]["mfe"] < 112
    assert broker.ambiguous_events[0]["resolution"] == "STOP_FIRST"


def test_gap_through_stop_limit_remains_an_explicit_unfilled_obligation():
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100.0, _at(), {"stopLoss": 90.0})
    stop_id = broker.set_protective_stop(
        "TEST", 95.0, _at(), stop_limit=True, limit_price=94.0
    )
    broker.process_candle("TEST", _candle(opening=90, high=92, low=85, close=88))

    assert "TEST" in broker.positions
    assert broker.orders[stop_id]["status"] == "TRIGGERED"
    assert any(event["type"] == "STOP_LIMIT_UNFILLED" for event in broker.events)


def test_flat_cash_equals_initial_capital_plus_recorded_net_pnl():
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100.0, _at())
    broker.close_position("TEST", 110.0, _at(5), reason="fixture_close")

    assert broker.cash == round(broker.initial_capital + broker.trades[0]["net_pnl"], 2)


def test_common_coordinator_adapter_submits_only_reconciled_residual(tmp_path):
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100.0, _at(), {"stopLoss": 95.0})
    key = broker.position_snapshot(_at()).net[0].key.as_string()
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "paper.db")))
    result = coordinator.handoff_with_broker_adapter(
        broker=broker,
        position_key=key,
        role=OrderRole.REDUCTION,
        side="SELL",
        requested_quantity=10,
        payload={"tradingsymbol": "TEST", "timestamp": _at().isoformat()},
        stop_order_id=broker.positions["TEST"]["stop_order_id"],
        reason="THESIS_BREAKOUT_FAILED",
    )

    assert result.broker_order_id
    order = broker.get_order(result.broker_order_id)
    assert order["quantity"] == 10
    broker.process_candle("TEST", _candle(opening=99, high=100, low=98, close=99))
    assert "TEST" not in broker.positions
    assert broker.reconcile_fills_with(coordinator, key) == 2
    # Replayed delivery is deduplicated by the shared fill ledger.
    assert broker.reconcile_fills_with(coordinator, key) == 0


def test_paper_adapter_is_explicitly_isolated_from_live_broker_state():
    paper = PaperBroker(data_source_id="fixture-feed")
    paper.mark("TEST", 100.0, _at())
    assert paper.execution_manifest["namespace"] == "PAPER"
    assert paper.execution_manifest["live_broker_reachable"] is False
