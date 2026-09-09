import numpy as np
import pytest

from backend.config import config_manager
from backend.kite_client import kite_client
from backend.scanner import scanner
from backend.tests.conftest import build_candles


@pytest.fixture
def mock_scanner_config(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "get_families_config",
        lambda: {
            "trend": {"enabled": True, "weight": 1.0},
            "mean_reversion": {"enabled": True, "weight": 1.0},
            "breakout": {"enabled": True, "weight": 1.0},
        },
    )
    # Make sure strategies are enabled
    strat_cfg = {k: {"enabled": True} for k in scanner.strategies.keys()}
    monkeypatch.setattr(config_manager, "get_strategy_config", lambda: strat_cfg)
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 12345}],
    )


def test_scanner_aggregation_trending(mock_scanner_config, monkeypatch, uptrend):
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (uptrend, False))

    # Force EMA Crossover (Trend family) to return a BUY signal
    def mock_calc_signals(self, df, symbol):
        return [
            {
                "strategy": "EMA Crossover",
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 120.0,
                "stopLoss": 110.0,
                "target": 140.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["ema_crossover"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["ema_crossover"]),
    )

    signals = scanner.scan_watchlist(["TEST"])
    # Should yield family_trend signals
    assert len(signals) > 0
    assert any(sig["strategy"] == "Trend Pullback" for sig in signals)

    # Check that regime was correctly identified
    assert any(sig["regime"] == "TRENDING" for sig in signals)


def test_scanner_aggregation_uncertain(mock_scanner_config, monkeypatch):
    # Small number of candles
    closes = np.linspace(100, 110, 30)
    df = build_candles(closes)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))

    signals = scanner.scan_watchlist(["TEST"])
    # Regime classifier needs 50 candles, so it returns UNCERTAIN
    # Aggregation skips if regime is UNCERTAIN
    assert len(signals) == 0


def test_scanner_aggregation_breakout(mock_scanner_config, monkeypatch):
    # Normal volatility then sudden expansion
    closes = list(np.linspace(100, 102, 45))  # tight range
    closes += [105, 110, 118, 130, 150]  # sudden breakout
    df = build_candles(closes)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))

    # Force Bollinger Breakout (Breakout family) to return a BUY signal
    def mock_calc_signals(self, df, symbol):
        return [
            {
                "strategy": "Bollinger Breakout",
                "direction": "BUY",
                "signal_score": 90,
                "entryPrice": 145.0,
                "stopLoss": 135.0,
                "target": 160.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["bollinger_breakout"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["bollinger_breakout"]),
    )

    signals = scanner.scan_watchlist(["TEST"])
    assert any(sig["regime"] == "BREAKOUT" for sig in signals)
    # Breakout regime allows breakout and trend families
    assert any(sig["strategy"] in ("Breakout", "Trend Pullback") for sig in signals)


def test_scanner_incomplete_candle_skipping(mock_scanner_config, monkeypatch):
    import pandas as pd

    from backend.tests.conftest import build_candles

    now = pd.Timestamp.now(tz="Asia/Kolkata")

    # Create 55 candles ending at `now` (the last one is incomplete)
    dates = pd.date_range(end=now, periods=55, freq="5min")
    df = build_candles(np.linspace(100, 140, 55), dates=dates)

    # Force _fetch_candles to return this df
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))

    called_df_length = []

    def mock_calc_signals(self, df_in, symbol):
        called_df_length.append(len(df_in))
        return [
            {
                "strategy": "EMA Crossover",
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 120.0,
                "stopLoss": 110.0,
                "target": 140.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["ema_crossover"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["ema_crossover"]),
    )

    # First call
    signals = scanner.scan_watchlist(["TEST"])

    # Should slice out the incomplete candle (so length is 54)
    assert len(called_df_length) > 0
    assert called_df_length[0] == 54
    assert len(signals) > 0

    called_df_length.clear()

    # Second call right away
    signals2 = scanner.scan_watchlist(["TEST"])

    # Should be empty because the completed candle timestamp hasn't changed
    assert len(signals2) == 0
    assert len(called_df_length) == 0
