"""Legacy normal controls must obey the phase-4 causal data boundary."""

from datetime import timedelta

import pandas as pd
import pytest

import backend.trading_engine as engine_module
from backend.market_context import build_market_context
from backend.tests.conftest import build_candles
from backend.trading_engine import TradingEngine

START = pd.Timestamp("2026-09-21 09:15", tz="Asia/Kolkata")


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_rejection_uses_latest_completed_bar_once_and_requires_post_entry_data(
    monkeypatch, direction
):
    frame = build_candles(
        [100.0] * 21,
        dates=pd.date_range(START, periods=21, freq="5min"),
    )
    if direction == "BUY":
        frame.loc[20, ["high", "close"]] = [101, 100.1]
        entry, target = 98, 110
    else:
        frame.loc[20, ["low", "close"]] = [99, 99.9]
        entry, target = 102, 90
    at = (START + timedelta(minutes=105)).to_pydatetime()
    context = build_market_context("111", frame, at)
    engine = TradingEngine()
    engine._instrument_map = {"TEST": 111}
    trade = {
        "direction": direction,
        "entry_time": START.to_pydatetime(),
        "entry_price": entry,
        "target": target,
    }
    engine.active_trades["TEST"] = trade
    monkeypatch.setattr(engine_module.scanner, "get_market_context", lambda *a: context)

    # The latest row is already complete. The old df.iloc[-2] missed this
    # actual rejection and could instead act on an unrelated earlier candle.
    assert engine._check_resistance_exit("TEST", 100, direction)
    assert not engine._check_resistance_exit("TEST", 100, direction)
    trade.pop("last_resistance_bar_start")

    # A rejection before or during the entry candle is not post-entry evidence.
    trade["entry_time"] = context.primary_bar.start + timedelta(seconds=1)
    assert not engine._check_resistance_exit("TEST", 100, direction)
    trade["entry_time"] = START.to_pydatetime()

    context = build_market_context("111", frame, at + timedelta(minutes=30))
    assert not engine._check_resistance_exit("TEST", 100, direction)
    assert "last_resistance_bar_start" not in trade

    frame.loc[10, "low"] = 1_000
    context = build_market_context("111", frame, at)
    assert not engine._check_resistance_exit("TEST", 100, direction)


@pytest.mark.parametrize("assessment", [False, None])
def test_unavailable_review_cannot_exit_or_postpone_recovered_evidence(
    monkeypatch, assessment
):
    engine = TradingEngine()
    now = (START + timedelta(hours=2)).to_pydatetime()
    previous_review = now - timedelta(minutes=35)
    trade = {
        "direction": "BUY",
        "entry_time": now - timedelta(hours=1),
        "entry_price": 100,
        "sl": 95,
        "last_reeval_time": previous_review,
    }
    engine.active_trades["TEST"] = trade
    engine._instrument_map = {"TEST": 111}
    position = {"tradingsymbol": "TEST", "quantity": 10, "last_price": 99}
    evaluation = {"buy_signals": 0, "sell_signals": 2}
    if assessment is not None:
        evaluation["assessment_available"] = assessment
    monkeypatch.setattr(engine_module, "now_utc", lambda: now)
    monkeypatch.setattr(engine, "_positions", lambda: [position])
    monkeypatch.setattr(engine, "_has_fresh_position_mark", lambda p: True)
    monkeypatch.setattr(engine, "_trade_matches_position", lambda t, p: True)
    monkeypatch.setattr(engine, "_persist_trades", lambda: None)
    monkeypatch.setattr(
        engine_module.scanner, "evaluate_position", lambda *a: evaluation
    )
    exits, tightenings = [], []
    monkeypatch.setattr(engine, "_exit_position", lambda *a: exits.append(a))
    monkeypatch.setattr(
        engine, "_tighten_to_breakeven", lambda *a: tightenings.append(a)
    )

    engine._reevaluate_positions()

    assert not exits
    assert not tightenings
    assert trade["last_reeval_time"] == previous_review

    evaluation["assessment_available"] = True
    engine._reevaluate_positions()
    assert len(exits) == 1
    assert "Thesis invalidated" in exits[0][2]
