"""Behavioral (trigger) tests for the momentum / oscillator / trend strategies.

Oscillator crossovers (MACD, TSI, Stochastic, StochRSI, RSI, Awesome, ADX,
Supertrend, PSAR, Keltner, MFI) don't fire on simple linear ramps because the
smoothed lines move in lockstep. A sine wave produces genuine crossovers, so
these series are a sine slice cut so the signal lands on the final bar. Each
`_seed` was found against the real indicator math and is asserted here so a
regression in a strategy's condition breaks the test.
"""
import numpy as np

from backend.tests.conftest import build_candles, assert_valid_signal
from backend.scanner import scanner

STRAT = scanner.strategies


def _t(n):
    return np.arange(n)


def _sine(period, n, base=100.0, amp=8.0):
    """A sine-wave close series of length n."""
    return list(base + amp * np.sin(2 * np.pi * _t(n) / period))


def _one(strategy_id, df, direction):
    sigs = STRAT[strategy_id].calculate_signals(df.copy(), "T")
    assert len(sigs) == 1, f"{strategy_id}: expected 1 signal, got {sigs}"
    sig = sigs[0]
    assert_valid_signal(sig)
    assert sig["direction"] == direction
    return sig


class TestMACDCross:
    def test_buy_on_bullish_cross(self):
        _one("macd_cross", build_candles(_sine(20, 42)), "BUY")

    def test_sell_on_bearish_cross(self):
        _one("macd_cross", build_candles(_sine(30, 45)), "SELL")


class TestTSICross:
    def test_buy_on_zero_line_cross_up(self):
        _one("tsi_cross", build_candles(_sine(20, 41)), "BUY")

    def test_sell_on_zero_line_cross_down(self):
        _one("tsi_cross", build_candles(_sine(30, 50)), "SELL")


class TestStochasticReversal:
    def test_buy_on_oversold_cross(self):
        _one("stochastic_reversal", build_candles(_sine(30, 24)), "BUY")

    def test_sell_on_overbought_cross(self):
        _one("stochastic_reversal", build_candles(_sine(20, 27)), "SELL")


class TestStochRSI:
    def test_buy_on_cross_above_lower_band(self):
        _one("stoc_rsi", build_candles(_sine(20, 40)), "BUY")

    def test_sell_on_cross_below_upper_band(self):
        _one("stoc_rsi", build_candles(_sine(30, 42)), "SELL")


class TestRSIReversal:
    def test_buy_on_recovery_above_30_with_price_over_vwap(self):
        _one("rsi_reversal", build_candles(_sine(40, 36)), "BUY")


class TestAwesomeOscillator:
    def test_buy_on_zero_cross_up(self):
        # Deep decline then a long recovery pushes AO from below to above zero.
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 110, 10))
        _one("awesome_oscillator", build_candles(closes), "BUY")


class TestADXMomentum:
    def test_buy_on_di_cross_in_strong_trend(self):
        # V-bottom: sharp reversal with a strong (ADX > 25) directional move.
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 110, 2))
        _one("adx_momentum", build_candles(closes), "BUY")


class TestSupertrend:
    def test_buy_on_bullish_flip_with_adx(self):
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 110, 2))
        _one("supertrend", build_candles(closes), "BUY")


class TestParabolicSAR:
    def test_buy_on_flip_below_price(self):
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 110, 2))
        _one("psar_trend", build_candles(closes), "BUY")


class TestKeltnerBreakout:
    def test_buy_on_break_above_upper_band(self):
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 110, 2))
        _one("keltner_breakout", build_candles(closes), "BUY")


class TestMFIExhaustion:
    def test_buy_on_bounce_from_oversold(self):
        closes = list(np.linspace(120, 100, 30)) + list(np.linspace(100, 102, 4))
        _one("mfi_exhaustion", build_candles(closes), "BUY")


class TestVWAPBounce:
    def test_buy_on_bounce_off_vwap_in_uptrend(self):
        # Strong uptrend (fast EMA > slow EMA, RSI > 40) with a final green bar
        # that pulls back to close just above VWAP within the 0.2% band.
        base = list(np.linspace(100, 112, 34))
        closes = base + [109.92]
        opens = list(closes)
        opens[-1] = 109.92 * 0.999  # green candle
        _one("vwap_bounce", build_candles(closes, opens=opens), "BUY")
