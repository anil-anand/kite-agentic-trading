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
                "confidence": 85,
                "entryPrice": 120.0,
                "stopLoss": 110.0,
                "target": 140.0,
                "reasoning": "mock",
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
    assert any(sig["strategy"] == "family_trend" for sig in signals)

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
                "confidence": 90,
                "entryPrice": 145.0,
                "stopLoss": 135.0,
                "target": 160.0,
                "reasoning": "mock",
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
    assert any(
        sig["strategy"] in ("family_breakout", "family_trend") for sig in signals
    )
