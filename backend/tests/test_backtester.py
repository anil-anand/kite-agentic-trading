import datetime

import pandas as pd

from backend.backtesting.backtest_engine import BacktestEngine
from backend.backtesting.metrics_evaluator import MetricsEvaluator
from backend.backtesting.simulated_broker import SimulatedBroker
from backend.strategies.base import BaseStrategy


class MockStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Mock"

    def get_description(self) -> str:
        return "Mock"

    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str):
        if len(df) == 0:
            return []

        last = df.iloc[-1]

        # Simple rule: buy if close > 110, sell if close < 90
        if last["close"] > 110:
            return [
                {
                    "direction": "BUY",
                    "entryPrice": last["close"],
                    "stopLoss": last["close"] - 10,
                    "target": last["close"] + 20,
                    "signal_score": 100,
                    "strategy": "Mock",
                    "indicators": {},
                }
            ]
        elif last["close"] < 90:
            return [
                {
                    "direction": "SELL",
                    "entryPrice": last["close"],
                    "stopLoss": last["close"] + 10,
                    "target": last["close"] - 20,
                    "signal_score": 100,
                    "strategy": "Mock",
                    "indicators": {},
                }
            ]
        return []


def test_simulated_broker():
    broker = SimulatedBroker(initial_capital=100000)
    now = datetime.datetime.now()

    # Place a buy order
    broker.place_market_order(
        "TEST", "BUY", 10, 100.0, now, signal_info={"stopLoss": 90, "target": 120}
    )

    assert "TEST" in broker.positions
    assert broker.positions["TEST"]["quantity"] == 10

    # Process candle that hits target
    candle = pd.Series(
        {
            "open": 105.0,
            "high": 125.0,
            "low": 100.0,
            "close": 120.0,
            "date": now + datetime.timedelta(minutes=5),
        }
    )

    broker.process_candle("TEST", candle)

    # Should be closed
    assert "TEST" not in broker.positions
    assert len(broker.trades) == 1

    trade = broker.trades[0]
    assert trade["direction"] == "BUY"
    assert trade["net_pnl"] > 0


def test_backtest_engine():
    dates = pd.date_range(start="2023-01-01", periods=10, freq="5min")
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [100, 105, 115, 120, 110, 100, 90, 80, 85, 95],
            "high": [105, 110, 120, 125, 115, 105, 95, 85, 90, 100],
            "low": [95, 100, 110, 115, 105, 95, 85, 75, 80, 90],
            "close": [105, 115, 120, 110, 100, 90, 85, 85, 95, 100],
            "volume": [1000] * 10,
        }
    )

    strategy = MockStrategy()
    engine = BacktestEngine(strategy)
    engine.load_data("TEST", df)

    engine.run()

    assert len(engine.broker.trades) > 0


def test_metrics_evaluator():
    trades = [
        {
            "entry_time": "2023-01-01 10:00:00",
            "exit_time": "2023-01-01 10:30:00",
            "gross_pnl": 500,
            "net_pnl": 450,
            "total_fees": 50,
            "quantity": 10,
            "entry_price": 100,
        },
        {
            "entry_time": "2023-01-02 10:00:00",
            "exit_time": "2023-01-02 10:30:00",
            "gross_pnl": -200,
            "net_pnl": -250,
            "total_fees": 50,
            "quantity": 10,
            "entry_price": 100,
        },
    ]

    metrics = MetricsEvaluator.evaluate(trades, 100000)

    assert metrics["trade_count"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["net_profit"] == 200
    assert metrics["total_fees_paid"] == 100
