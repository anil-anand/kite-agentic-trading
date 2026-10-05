"""Fixed objective price barriers survive adapter changes and OHLC uncertainty."""

from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.candidate_runner import CandidateRunner
from backend.backtesting.legacy_control import LegacyControlRunner
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator
from backend.tests.exit_management.test_engine import _state, _thesis
from backend.tests.test_candidate_runner_boundaries import ENTRY_AT


def _runner(
    tmp_path,
    side,
    runner_type=CandidateRunner,
    *,
    partial=False,
    ambiguity="STOP_FIRST",
    latency=0,
):
    sign = 1 if side == "BUY" else -1
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(
            slippage_bps=0, ambiguity_policy=ambiguity, order_latency_bars=latency
        )
    )
    entry_at = ENTRY_AT + timedelta(minutes=2) if partial else ENTRY_AT
    broker.place_market_order(
        "RELIANCE",
        side,
        10,
        100,
        entry_at,
        {"stopLoss": 100 - sign * 5, "target": 100 + sign * 10},
    )
    key = broker._key_for("RELIANCE").as_string()
    thesis = replace(
        _thesis(),
        position_key=key,
        instrument_id="SIM-RELIANCE",
        direction=side,
        initial_stop=100 - sign * 5,
        objective=100 + sign * 10,
    )
    runner = runner_type(
        broker=broker,
        coordinator=OrderLifecycleCoordinator(
            TradeJournal(str(tmp_path / "barriers.db"))
        ),
    )
    runner.register_position(thesis=thesis, state=replace(_state(), position_key=key))
    return runner


def _bar(side, *, gap=False, both=False, close_touch=False):
    values = {
        "open": 112 if gap else 100,
        "high": 113 if gap else 111,
        "low": 94 if both else 99,
        "close": 111 if close_touch else 101,
    }
    if side == "SELL":
        values = {
            "open": 200 - values["open"],
            "high": 200 - values["low"],
            "low": 200 - values["high"],
            "close": 200 - values["close"],
        }
    return {"date": ENTRY_AT, "volume": 1000, **values}


@pytest.mark.parametrize("runner_type", [CandidateRunner, LegacyControlRunner])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("gap", [False, True])
def test_target_touch_retrace_uses_one_coordinated_reduction(
    tmp_path, runner_type, side, gap
):
    runner = _runner(tmp_path, side, runner_type)
    runner.on_event(
        ENTRY_AT + timedelta(minutes=5), candles={"RELIANCE": _bar(side, gap=gap)}
    )
    broker = runner.broker
    assert not broker.positions
    trade = broker.trades[0]
    assert trade["exit_reason"] == "PROFIT_FIXED_OBJECTIVE_REACHED"
    expected_price = 112 if gap else 110
    assert trade["exit_price"] == (
        expected_price if side == "BUY" else 200 - expected_price
    )
    reductions = [o for o in broker.orders.values() if o["role"] == "REDUCTION"]
    assert len(reductions) == 1
    assert reductions[0]["tag"].startswith("ol")
    assert not broker.pending_orders
    assert (
        broker.execution_manifest["target_order_type"]
        == "PRECOMMITTED_LIMIT_WITH_COORDINATOR_HANDOFF"
    )
    touch = next(e for e in broker.events if e["type"] == "TARGET_BARRIER_TOUCH")
    assert touch["timing_quality"] == ("OBSERVED_PRINT" if gap else "INTRABAR_BOUNDS")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("gap", [False, True])
def test_both_barriers_report_ambiguity_unless_open_proves_target_first(
    tmp_path, side, gap
):
    runner = _runner(tmp_path, side)
    runner.on_event(
        ENTRY_AT + timedelta(minutes=5),
        candles={"RELIANCE": _bar(side, gap=gap, both=True)},
    )
    assert len(runner.broker.ambiguous_events) == (0 if gap else 1)
    assert runner.broker.trades[0]["exit_reason"] == (
        "PROFIT_FIXED_OBJECTIVE_REACHED" if gap else "stop_loss"
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("close_touch", [False, True])
def test_partial_entry_bar_requires_post_entry_target_proof(
    tmp_path, side, close_touch
):
    runner = _runner(tmp_path, side, partial=True)
    runner.on_event(
        ENTRY_AT + timedelta(minutes=5),
        candles={"RELIANCE": _bar(side, close_touch=close_touch)},
    )
    assert bool(runner.broker.trades) == close_touch
    assert len(runner.broker.ambiguous_events) == (0 if close_touch else 1)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_quote_and_candle_adapters_preserve_the_same_fixed_objective_reason(
    tmp_path, side
):
    runner = _runner(tmp_path, side)
    at = ENTRY_AT + timedelta(seconds=1)
    runner.broker.mark_price("RELIANCE", 111 if side == "BUY" else 89, at)
    result = runner.on_event(at)
    assert result[0].decision.primary_reason_code == "PROFIT_FIXED_OBJECTIVE_REACHED"
    assert len(runner.execution_results) == 1


def test_report_only_ambiguity_cannot_create_a_verified_target_fill(tmp_path):
    runner = _runner(tmp_path, "BUY", ambiguity="REPORT_ONLY")
    runner.on_event(
        ENTRY_AT + timedelta(minutes=5), candles={"RELIANCE": _bar("BUY", both=True)}
    )
    assert runner.broker.trades == []
    assert runner.broker.censored_positions


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_partial_entry_bar_keeps_stop_target_ordering_ambiguous(tmp_path, side):
    runner = _runner(tmp_path, side, partial=True)
    runner.on_event(
        ENTRY_AT + timedelta(minutes=5),
        candles={"RELIANCE": _bar(side, both=True, close_touch=True)},
    )
    assert runner.broker.ambiguous_events[0]["type"] == "STOP_TARGET_AMBIGUITY"
    assert runner.broker.trades[0]["exit_reason"] == "stop_loss"
    assert runner.finish()["objective_events"]


def test_target_handoff_respects_declared_execution_latency(tmp_path):
    runner = _runner(tmp_path, "BUY", latency=1)
    runner.on_event(ENTRY_AT + timedelta(minutes=5), candles={"RELIANCE": _bar("BUY")})
    assert runner.broker.trades == []
    assert len(runner.execution_results) >= 1


def test_managed_position_rejects_direct_simulator_execution(tmp_path):
    runner = _runner(tmp_path, "BUY")
    with pytest.raises(ValueError, match="target coordinator"):
        runner.broker.process_candle("RELIANCE", pd.Series(_bar("BUY")))
    assert runner.broker.trades == []
    assert not [o for o in runner.broker.orders.values() if o["role"] == "REDUCTION"]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_stop_limit_trigger_at_open_does_not_backdate_intrabar_fill(side):
    sign = 1 if side == "BUY" else -1
    broker = SimulatedBroker(execution_policy=SimulationExecutionPolicy(slippage_bps=0))
    broker.place_market_order("TEST", side, 10, 100, ENTRY_AT)
    broker.set_protective_stop(
        "TEST", 100 - sign * 5, ENTRY_AT, stop_limit=True, limit_price=100 - sign * 6
    )
    candle = {
        "date": ENTRY_AT + timedelta(minutes=5),
        "open": 90,
        "high": 96,
        "low": 89,
        "close": 95,
    }
    if side == "SELL":
        candle.update(open=110, high=111, low=104, close=105)
    broker.process_candle("TEST", pd.Series(candle))
    timing = broker.trades[0]["exit_timing"]
    assert timing["timing_quality"] == "INTRABAR_BOUNDS"
    assert timing["earliest_at"] == candle["date"].isoformat()
    assert broker.trades[0]["exit_time"] == ENTRY_AT + timedelta(minutes=10)


@pytest.mark.parametrize("missing", ["event", "fill_link", "intent_link"])
def test_target_coverage_requires_all_retained_execution_joins(tmp_path, missing):
    runner = _runner(tmp_path, "BUY")
    runner.on_event(ENTRY_AT + timedelta(minutes=5), candles={"RELIANCE": _bar("BUY")})
    assert runner.finish()["execution_coverage"]["target_execution_complete"]
    if missing == "event":
        runner.broker.events = [
            event
            for event in runner.broker.events
            if event["type"] != "TARGET_BARRIER_TOUCH"
        ]
    elif missing == "fill_link":
        runner.broker.fills[-1].pop("objective_event_id")
    else:
        runner.broker.orders[runner.broker.fills[-1]["order_id"]]["tag"] = None
    assert not runner.finish()["execution_coverage"]["target_execution_complete"]
