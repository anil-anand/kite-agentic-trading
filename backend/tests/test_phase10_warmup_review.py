"""Warmup is historical features, never an execution/account event."""

from datetime import timedelta

import pandas as pd
import pytest

from backend.backtesting.backtest_engine import BacktestEngine
from backend.tests.test_walk_forward import _WarmupSignalStrategy


def _engine():
    start = pd.Timestamp("2026-09-01T04:00:00Z")
    frame = pd.DataFrame(
        {
            "date": pd.date_range(start, periods=5, freq="5min"),
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.0] * 5,
            "volume": [1_000] * 5,
        }
    )
    engine = BacktestEngine(_WarmupSignalStrategy())
    engine.load_data("FIXTURE", frame)
    return engine, start + timedelta(minutes=10)


def test_warmup_never_reaches_execution_and_still_supplies_feature_history(monkeypatch):
    engine, boundary = _engine()
    executed_bars, histories = [], []
    process = engine.broker.process_candle
    calculate = engine.strategy.calculate_signals_with_context

    def capture_execution(symbol, candle):
        executed_bars.append(candle["date"])
        return process(symbol, candle)

    def capture_features(frame, *args, **kwargs):
        histories.append(frame.copy())
        return calculate(frame, *args, **kwargs)

    monkeypatch.setattr(engine.broker, "process_candle", capture_execution)
    monkeypatch.setattr(
        engine.strategy, "calculate_signals_with_context", capture_features
    )
    engine.run(trading_start_at=boundary)

    assert executed_bars and min(executed_bars) == boundary
    assert len(histories[0]) == 2
    assert histories[0]["date"].max() < boundary
    assert engine.broker.fills[0]["exchange_time"] >= boundary


def test_seeded_account_cannot_be_labelled_feature_only_warmup():
    engine, boundary = _engine()
    engine.broker.place_market_order(
        "FIXTURE",
        "BUY",
        1,
        100,
        boundary - timedelta(minutes=10),
        {"stopLoss": 90, "target": 150},
    )
    original_fills = list(engine.broker.fills)
    with pytest.raises(ValueError, match="fresh account"):
        engine.run(trading_start_at=boundary)
    assert engine.broker.fills == original_fills
    assert engine.broker.positions["FIXTURE"]["quantity"] == 1


@pytest.mark.parametrize("boundary", [pd.NaT, pd.Timestamp("2026-09-01T04:10:00")])
def test_warmup_boundary_requires_explicit_finite_instant(boundary):
    engine, _ = _engine()
    with pytest.raises(ValueError, match="finite aware timestamp"):
        engine.run(trading_start_at=boundary)
    assert engine.broker.fills == []


@pytest.mark.parametrize("value", [pd.NaT, "bad-time", "2026-09-01T04:05:00"])
def test_invalid_availability_fails_before_simulated_mutation(value):
    engine, boundary = _engine()
    engine.market_data["FIXTURE"]["available_at"] = value
    with pytest.raises(ValueError, match="availability requires"):
        engine.run(trading_start_at=boundary)
    assert engine.broker.orders == {}
