"""Behavioral (trigger) tests for strategies whose entry condition can be
constructed deterministically. Each series is shaped so the signal fires on the
final bar, mirroring how the live scanner evaluates the latest candle.

The series here were validated against the actual indicator math; if a strategy's
logic changes, these are the tests that should catch it.
"""

import numpy as np

from backend.strategies.bollinger_breakout import BollingerBreakoutStrategy
from backend.strategies.cci_reversal import CCIReversalStrategy
from backend.strategies.donchian_breakout import DonchianBreakoutStrategy
from backend.strategies.ema_crossover import EMACrossoverStrategy
from backend.strategies.williams_r import WilliamsRStrategy
from backend.tests.conftest import assert_valid_signal, build_candles


def _one(signals):
    assert len(signals) == 1, f"expected exactly one signal, got {len(signals)}"
    assert_valid_signal(signals[0])
    return signals[0]


class TestDonchianBreakout:
    def test_buy_on_new_high(self):
        df = build_candles([100] * 24 + [106])
        sig = _one(DonchianBreakoutStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "BUY"

    def test_sell_on_new_low(self):
        df = build_candles([100] * 24 + [94])
        sig = _one(DonchianBreakoutStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "SELL"

    def test_no_signal_inside_range(self):
        df = build_candles([100 + (0.1 if i % 2 else -0.1) for i in range(25)])
        assert DonchianBreakoutStrategy().calculate_signals(df, "T") == []


class TestEMACrossover:
    def test_buy_on_golden_cross_with_volume(self):
        closes = list(np.linspace(120, 100, 26)) + list(np.linspace(100, 112, 8))
        volumes = [100_000] * (26 + 8 - 1) + [500_000]
        df = build_candles(closes, volumes=volumes)
        sig = _one(EMACrossoverStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "BUY"
        # Confidence scales with the volume ratio and is capped at 100.
        assert sig["confidence"] == 100

    def test_no_signal_without_volume_confirmation(self):
        # Same golden cross, but the breakout bar has below-average volume.
        closes = list(np.linspace(120, 100, 26)) + list(np.linspace(100, 112, 8))
        volumes = [100_000] * (26 + 8 - 1) + [10]
        df = build_candles(closes, volumes=volumes)
        assert EMACrossoverStrategy().calculate_signals(df, "T") == []


class TestBollingerBreakout:
    def test_buy_on_upper_band_break_with_volume(self):
        closes = [100 + (0.2 if i % 2 else -0.2) for i in range(24)] + [106]
        volumes = [100_000] * 24 + [400_000]
        df = build_candles(closes, volumes=volumes)
        sig = _one(BollingerBreakoutStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "BUY"

    def test_no_signal_without_volume_spike(self):
        closes = [100 + (0.2 if i % 2 else -0.2) for i in range(24)] + [106]
        volumes = [100_000] * 25  # no spike on the breakout bar
        df = build_candles(closes, volumes=volumes)
        assert BollingerBreakoutStrategy().calculate_signals(df, "T") == []


class TestWilliamsR:
    def test_buy_on_bounce_from_oversold(self):
        df = build_candles(list(np.linspace(120, 100, 20)) + [103])
        sig = _one(WilliamsRStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "BUY"

    def test_sell_on_rejection_from_overbought(self):
        df = build_candles(list(np.linspace(100, 120, 20)) + [117])
        sig = _one(WilliamsRStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "SELL"


class TestCCIReversal:
    def test_buy_on_cross_up_from_oversold(self):
        df = build_candles(list(np.linspace(120, 100, 20)) + [104])
        sig = _one(CCIReversalStrategy().calculate_signals(df, "T"))
        assert sig["direction"] == "BUY"
