"""Adversarial execution cases for the shared phase-8 broker adapter."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.broker_models import OrderRole
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator


def at(minutes=0):
    return datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc) + timedelta(
        minutes=minutes
    )


def candle(opening=100, high=101, low=99, close=100, minutes=5, **extra):
    return pd.Series(
        dict(open=opening, high=high, low=low, close=close, date=at(minutes), **extra)
    )


def submit(broker, *, side="SELL", quantity=10, minutes=0, **extra):
    return broker.submit_coordinator_order(
        "fixture",
        dict(
            tradingsymbol="TEST",
            transaction_type=side,
            quantity=quantity,
            timestamp=at(minutes),
            **extra,
        ),
    )


@pytest.mark.parametrize(
    "side,stop,limit,gap,recovery",
    [
        ("BUY", 95, 94, (90, 92, 85, 88), (100, 102, 98, 101)),
        ("SELL", 105, 106, (110, 115, 108, 112), (100, 102, 98, 99)),
    ],
)
def test_triggered_stop_limit_fills_after_recovery_without_second_trigger(
    side, stop, limit, gap, recovery
):
    broker = SimulatedBroker()
    broker.place_market_order("TEST", side, 10, 100, at())
    stop_id = broker.set_protective_stop(
        "TEST", stop, at(), stop_limit=True, limit_price=limit
    )
    broker.process_candle("TEST", candle(*gap))
    assert broker.orders[stop_id]["status"] == "TRIGGERED"
    broker.process_candle("TEST", candle(*recovery, minutes=10))
    assert "TEST" not in broker.positions
    assert broker.trades[0]["exit_reason"] == "stop_loss"
    assert broker.orders[stop_id]["status"] == "COMPLETE"


def test_partial_stop_is_a_working_market_reduction_even_after_rebound():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    broker.place_market_order("TEST", "BUY", 10, 100, at(), {"stopLoss": 95})
    broker.process_candle("TEST", candle(94, 95, 90, 92))
    assert broker.positions["TEST"]["quantity"] == 5
    broker.process_candle("TEST", candle(101, 103, 100, 102, minutes=10))
    assert broker.positions["TEST"]["quantity"] == 3


def test_partial_stop_then_separate_reduction_retains_full_residual_coverage():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    broker.place_market_order("TEST", "BUY", 10, 100, at(), {"stopLoss": 95})
    broker.process_candle("TEST", candle(94, 95, 90, 92))
    broker.place_market_order("TEST", "SELL", 2, 93, at(6))
    position = broker.positions["TEST"]
    stop = broker.orders[position["stop_order_id"]]
    assert position["quantity"] == 3
    assert stop["quantity"] == 8
    assert stop["filled_quantity"] == 5
    assert stop["remaining_quantity"] == 3


@pytest.mark.parametrize(
    "side,opening,high,low,limit",
    [("BUY", 102, 104, 99, 100), ("SELL", 98, 101, 96, 100)],
)
def test_limit_touch_fills_intrabar_without_violating_limit(
    side, opening, high, low, limit
):
    broker = SimulatedBroker()
    order_id = submit(broker, side=side, role="ENTRY", order_type="LIMIT", price=limit)
    broker.process_candle("TEST", candle(opening, high, low, 100))
    fill = broker.fills[0]
    assert fill["order_id"] == order_id
    assert fill["price"] <= limit if side == "BUY" else fill["price"] >= limit
    assert broker.positions["TEST"]["excursion_quality"] == "PARTIAL_BAR_BOUNDS"


def test_intrabar_entry_does_not_claim_target_or_mfe_before_entry():
    broker = SimulatedBroker()
    submit(
        broker,
        side="BUY",
        role="ENTRY",
        order_type="LIMIT",
        price=100,
        signal_info={"stopLoss": 90, "target": 110},
    )
    broker.process_candle("TEST", candle(115, 120, 99, 100))
    assert broker.positions["TEST"]["quantity"] == 10
    assert broker.positions["TEST"]["mfe"] < 120
    assert broker.trades == []


def test_zero_volume_cannot_fill_pending_entry_or_protective_order():
    broker = SimulatedBroker()
    submit(broker, side="BUY", role="ENTRY", signal_info={"stopLoss": 95})
    broker.process_candle("TEST", candle(94, 95, 90, 92, volume=0))
    assert broker.positions == {}
    assert broker.fills == []
    assert broker.events[-1]["type"] == "NO_LIQUIDITY"


def test_volume_participation_budget_is_shared_by_entry_and_stop():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_volume_participation=0.1)
    )
    submit(broker, side="BUY", role="ENTRY", signal_info={"stopLoss": 95})
    broker.process_candle("TEST", candle(100, 101, 90, 92, volume=20))
    assert sum(fill["quantity"] for fill in broker.fills) == 2
    assert broker.positions["TEST"]["quantity"] == 2
    stop = broker.orders[broker.positions["TEST"]["stop_order_id"]]
    assert stop["triggered"] is True


def test_missing_volume_cannot_bypass_declared_participation_cap():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_volume_participation=0.1)
    )
    submit(broker, side="BUY", role="ENTRY")
    broker.process_candle("TEST", candle())
    assert broker.fills == []
    assert broker.execution_manifest["missing_volume_policy"] == "NO_FILLS"


def test_cancelled_stop_does_not_suppress_target_and_reprotection_creates_working_order():
    broker = SimulatedBroker()
    broker.place_market_order(
        "TEST", "BUY", 10, 100, at(), {"stopLoss": 95, "target": 110}
    )
    old_stop = broker.positions["TEST"]["stop_order_id"]
    broker.cancel_order(old_stop)
    new_stop = broker.set_protective_stop("TEST", 96, at(1))
    assert new_stop != old_stop
    assert broker.orders[new_stop]["status"] == "TRIGGER PENDING"
    broker.cancel_order(new_stop)
    broker.process_candle("TEST", candle(100, 112, 94, 105))
    assert broker.trades[0]["exit_reason"] == "target"


def test_pending_reduction_cannot_reopen_position_after_other_exit():
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100, at())
    pending = submit(broker)
    broker.close_position("TEST", 102, at(1))
    broker.process_candle("TEST", candle())
    assert broker.positions == {}
    assert broker.orders[pending]["status"] == "CANCELLED"
    assert len(broker.fills) == 2


def test_opening_target_gap_has_known_precedence_over_later_stop_touch():
    broker = SimulatedBroker()
    broker.place_market_order(
        "TEST", "BUY", 10, 100, at(), {"stopLoss": 95, "target": 110}
    )
    broker.process_candle("TEST", candle(115, 117, 94, 100))
    assert broker.trades[0]["exit_reason"] == "target"
    assert broker.trades[0]["exit_price"] >= 110
    assert not broker.ambiguous_events


def test_target_fills_obey_partial_fraction_and_aggregate_fees_once():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    broker.place_market_order("TEST", "BUY", 10, 100, at(), {"target": 110})
    broker.process_candle("TEST", candle(111, 112, 110, 111))
    assert broker.positions["TEST"]["quantity"] == 5
    for minute in (10, 15, 20, 25):
        broker.process_candle("TEST", candle(111, 112, 110, 111, minutes=minute))
    assert broker.positions == {}
    assert (
        len([order for order in broker.orders.values() if order["reason"] == "target"])
        == 1
    )
    assert broker.cash == round(broker.initial_capital + broker.trades[0]["net_pnl"], 2)


def test_bar_close_order_and_new_stop_cannot_trade_earlier_bar_extrema():
    broker = SimulatedBroker()
    order = submit(broker, side="BUY", role="ENTRY", minutes=5)
    broker.process_candle("TEST", candle(100, 110, 90, 105, minutes=0))
    assert broker.orders[order]["filled_quantity"] == 0
    broker.process_candle("TEST", candle(100, 101, 99, 100, minutes=5))
    broker.set_protective_stop("TEST", 99.5, at(15))
    broker.process_candle("TEST", candle(100, 105, 95, 103, minutes=10))
    assert broker.positions["TEST"]["quantity"] == 10


def test_latency_and_entry_metadata_survive_common_submission():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(
            order_latency_bars=1, max_fill_fraction=0.5
        )
    )
    order = submit(broker, side="BUY", role="ENTRY", signal_info={"stopLoss": 90})
    broker.process_candle("TEST", candle())
    assert broker.fills == []
    broker.process_candle("TEST", candle(minutes=10))
    assert broker.orders[order]["filled_quantity"] == 5
    assert broker.positions["TEST"]["sl"] == 90


def test_duplicate_candle_cannot_advance_partial_fills_twice():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    submit(broker, side="BUY", role="ENTRY")
    event = candle()
    broker.process_candle("TEST", event)
    with pytest.raises(ValueError, match="unique and chronological"):
        broker.process_candle("TEST", event)
    assert len(broker.fills) == 1


def test_invalid_candle_is_rejected_without_order_or_cash_mutation():
    broker = SimulatedBroker()
    submit(broker, side="BUY", role="ENTRY")
    with pytest.raises(ValueError, match="OHLC"):
        broker.process_candle("TEST", candle(100, 105, 99, 110))
    assert broker.fills == []
    assert broker.cash == broker.initial_capital


def test_invalid_entry_protection_is_rejected_before_creating_fill_or_order():
    broker = SimulatedBroker()
    with pytest.raises(ValueError, match="stopLoss"):
        broker.place_market_order(
            "TEST", "BUY", 10, 100, at(), {"stopLoss": float("nan")}
        )
    assert broker.fills == []
    assert broker.orders == {}
    assert broker.positions == {}
    assert broker.cash == broker.initial_capital


def test_report_only_ambiguity_censors_instead_of_creating_later_profitable_exit():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(ambiguity_policy="REPORT_ONLY")
    )
    broker.place_market_order(
        "TEST", "BUY", 10, 100, at(), {"stopLoss": 95, "target": 110}
    )
    broker.process_candle("TEST", candle(100, 112, 94, 105))
    broker.process_candle("TEST", candle(120, 125, 118, 124, minutes=10))
    assert broker.trades == []
    assert broker.censored_positions[0]["reason"] == "STOP_TARGET_AMBIGUITY"


def test_snapshot_does_not_refresh_old_mark_and_partial_exit_keeps_close_mark():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    broker.place_market_order("TEST", "BUY", 10, 100, at(), {"stopLoss": 95})
    broker.process_candle("TEST", candle(94, 95, 90, 92))
    snapshot = broker.position_snapshot(at(30)).net[0]
    assert snapshot.last_price == 92
    # The 04:25 candle's close is observed at 04:30, and a later snapshot
    # preserves that clock rather than refreshing it to the snapshot time.
    assert snapshot.mark_time == at(10)
    with pytest.raises(ValueError, match="chronological"):
        broker.mark_price("TEST", 100, at(1))


def test_interleaved_partial_entry_and_stop_preserves_cash_and_weighted_entry():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    submit(broker, side="BUY", role="ENTRY", signal_info={"stopLoss": 95})
    broker.process_candle("TEST", candle(100, 101, 94, 96))
    broker.process_candle("TEST", candle(98, 101, 97, 99, minutes=10))
    broker.close_position("TEST", 101, at(11))
    trade = broker.trades[0]
    entries = [fill for fill in broker.fills if fill["side"] == "BUY"]
    entry_quantity = sum(fill["quantity"] for fill in entries)
    entry_notional = sum(fill["price"] * fill["quantity"] for fill in entries)
    assert trade["entry_price"] == pytest.approx(entry_notional / entry_quantity)
    assert broker.cash == round(broker.initial_capital + trade["net_pnl"], 2)


def test_coordinator_adapter_propagates_canonical_reason_to_execution(tmp_path):
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100, at(), {"stopLoss": 95})
    key = broker.position_snapshot(at()).net[0].key.as_string()
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "paper.db")))
    coordinator.handoff_with_broker_adapter(
        broker=broker,
        position_key=key,
        role=OrderRole.REDUCTION,
        side="SELL",
        requested_quantity=10,
        payload={"tradingsymbol": "TEST", "timestamp": at()},
        stop_order_id=broker.positions["TEST"]["stop_order_id"],
        reason="THESIS_BREAKOUT_FAILED",
    )
    broker.process_candle("TEST", candle())
    assert broker.trades[0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"


def test_simulated_namespace_cannot_impersonate_live_account():
    with pytest.raises(ValueError, match="LIVE namespace"):
        SimulatedBroker(namespace="LIVE")


@pytest.mark.parametrize("order_type", ["SL", "SL-LIMIT"])
def test_canonical_sl_payload_is_stop_limit_and_survives_gap(order_type):
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100, at())
    stop = submit(
        broker, role="PROTECTION", order_type=order_type, trigger_price=95, price=94
    )
    broker.process_candle("TEST", candle(90, 92, 85, 88))
    assert broker.orders[stop]["status"] == "TRIGGERED"
    assert broker.order_snapshot(at(5)).orders[-1].order_type == "SL"
    assert broker.positions["TEST"]["quantity"] == 10


def test_canonical_sl_m_payload_is_a_market_stop():
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 10, 100, at())
    stop = submit(broker, role="PROTECTION", order_type="SL-M", trigger_price=95)
    broker.process_candle("TEST", candle(90, 92, 85, 88))
    assert broker.orders[stop]["status"] == "COMPLETE"
    assert broker.order_snapshot(at(5)).orders[-1].order_type == "SL-M"
    assert broker.trades[0]["exit_price"] == 89.95
