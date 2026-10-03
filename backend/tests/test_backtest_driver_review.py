"""Independent causal data and end-of-stream regressions for phase 8."""

from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.backtest_engine import BacktestEngine
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.market_context import MarketContextService
from backend.strategies.base import BaseStrategy
from backend.tests.test_candidate_execution import START, SYMBOL, _runner


class OneEntry(BaseStrategy):
    def get_name(self):
        return "single-fixed-opportunity"

    def get_description(self):
        return "One synthetic entry for event-order tests"

    def calculate_signals(self, df, tradingsymbol):
        if len(df) != 1:
            return []
        return [{"direction": "BUY", "entryPrice": 100, "stopLoss": 50, "target": 200}]


def rows(*indices):
    return [
        {
            "date": START + timedelta(minutes=5 * index),
            "open": 100,
            "high": 101,
            "low": 99.5,
            "close": 100.5,
            "volume": 1000,
        }
        for index in indices
    ]


@pytest.mark.parametrize("field", ["available_at", "received_at"])
def test_raw_driver_rejects_declared_delay_before_creating_early_fill(field):
    data = rows(0, 1, 2)
    for row in data:
        row[field] = row["date"] + timedelta(minutes=5)
    data[0][field] = START + timedelta(minutes=12)
    engine = BacktestEngine(OneEntry())
    engine.load_data("TEST", pd.DataFrame(data))
    with pytest.raises(ValueError, match="delayed data"):
        engine.run()
    assert engine.broker.fills == []
    assert engine.broker.orders == {}


def test_raw_final_zero_volume_cannot_force_ideal_liquidation():
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(max_fill_fraction=0.5)
    )
    engine = BacktestEngine(OneEntry(), broker=broker)
    data = rows(0, 1, 2)
    data[-1]["volume"] = 0
    engine.load_data("TEST", pd.DataFrame(data))
    result = engine.run()
    assert broker.positions["TEST"]["quantity"] > 0
    assert broker.trades == []
    assert result["censored_positions"][0]["reason"] == "RESEARCH_END_OF_DATA"
    assert result["equity_curve"][-1]["equity"] == broker.current_equity({})


def test_candidate_warmup_never_executes_against_future_seed_and_retains_first_receipt(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    engine = BacktestEngine(
        None, broker=runner.broker, mode=BacktestEngine.CANDIDATE_EXIT_REPLAY
    )
    contexts = []
    original = MarketContextService.build

    def capture(self, *args, **kwargs):
        context = original(self, *args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(MarketContextService, "build", capture)
    engine.load_data(SYMBOL, pd.DataFrame(rows(-2, -1, 0, 1, 2)))
    result = engine.run_candidate_execution(runner)
    assert result["manifest"]["warmup"] == "FEATURES_ONLY_BEFORE_CHECKPOINT"
    assert all(item["timestamp"] > START for item in result["equity_curve"])
    assert len(runner.broker.fills) == 1  # Seed; deadline has no executable data.
    first_start = START - timedelta(minutes=10)
    first_available = first_start + timedelta(minutes=5)
    retained = [
        bar
        for context in contexts
        if context.decision_event_time >= START
        for bar in context.primary_bars
        if bar.start == first_start
    ]
    assert retained
    assert all(bar.available_at == first_available for bar in retained)


def test_candidate_deadline_precedes_a_first_bar_delivered_next_session(tmp_path):
    runner = _runner(tmp_path)
    engine = BacktestEngine(
        None, broker=runner.broker, mode=BacktestEngine.CANDIDATE_EXIT_REPLAY
    )
    data = rows(0)
    data[0]["received_at"] = START + timedelta(days=1, minutes=5)
    engine.load_data(SYMBOL, pd.DataFrame(data))
    result = engine.run_candidate_execution(runner)
    deadline = START.replace(hour=9, minute=45)
    assert any(point["timestamp"] == deadline for point in result["equity_curve"])
    assert runner.execution_results
    reductions = [
        order for order in runner.broker.orders.values() if order["role"] == "REDUCTION"
    ]
    assert reductions[0]["submitted_at"] == deadline


def test_raw_symbol_insertion_order_cannot_change_reservations_or_fill_priority():
    def run(symbols):
        engine = BacktestEngine(OneEntry())
        for symbol in symbols:
            engine.load_data(symbol, pd.DataFrame(rows(0, 1)))
        result = engine.run()
        return (
            [
                (fill["symbol"], fill["quantity"], fill["price"])
                for fill in engine.broker.fills
            ],
            result["equity_curve"],
        )

    assert run(["BBB", "AAA"]) == run(["AAA", "BBB"])


def test_backtest_rpc_pins_settings_and_excludes_current_incomplete_candle(monkeypatch):
    import backend.main as main

    monkeypatch.setattr(main, "now_utc", lambda: START + timedelta(minutes=11))
    monkeypatch.setattr(main.scanner, "strategies", {"fixture": OneEntry()})
    monkeypatch.setattr(
        main.kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 1}],
    )
    monkeypatch.setattr(
        main.kite_client, "get_historical_data", lambda **kwargs: rows(0, 1, 2)
    )
    monkeypatch.setattr(
        main.config_manager, "get_risk_config", lambda: {"defaultStopLossPercent": 2}
    )
    response = main.handle_request(
        {
            "id": "phase8-raw",
            "method": "run_backtest",
            "params": {"strategy_id": "fixture", "symbol": "TEST"},
        }
    )
    assert "error" not in response
    result = response["result"]
    assert result["manifest"]["risk_config"]["defaultStopLossPercent"] == 2
    assert len(result["equity_curve"]) == 2
    assert result["metrics_basis"] == "FULL_MARK_TO_MARKET_EQUITY"
