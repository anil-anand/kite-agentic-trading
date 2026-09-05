"""Shared fixtures for backend tests.

Strategy signal calculations are pure functions of an OHLCV DataFrame, so
these fixtures let us construct deterministic candle data and assert on the
resulting signals without ever touching the Kite API.
"""

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

# Redirect HOME to a throwaway dir BEFORE any test module imports backend code.
# pytest loads conftest.py before collecting/importing test modules, and
# ConfigManager reads Path.home() at import time, so this must happen here at
# module scope rather than in a fixture to keep the real
# ~/.kite-agentic-trading untouched.
_TMP_HOME = tempfile.mkdtemp(prefix="kite-test-home-")
os.environ["HOME"] = _TMP_HOME


def build_candles(closes, *, opens=None, highs=None, lows=None, volumes=None):
    """Build an OHLCV DataFrame from a close-price series.

    Missing columns are derived sensibly: open defaults to the previous close,
    high/low wrap the open/close range, and volume defaults to a flat baseline.
    Pass explicit arrays to control any column precisely.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)

    if opens is None:
        opens = np.empty(n)
        if n:
            opens[0] = closes[0]
            opens[1:] = closes[:-1]
    else:
        opens = np.asarray(opens, dtype=float)

    if highs is None:
        highs = np.maximum(opens, closes) * 1.002
    else:
        highs = np.asarray(highs, dtype=float)

    if lows is None:
        lows = np.minimum(opens, closes) * 0.998
    else:
        lows = np.asarray(lows, dtype=float)

    if volumes is None:
        volumes = np.full(n, 100_000.0)
    else:
        volumes = np.asarray(volumes, dtype=float)

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


@pytest.fixture
def make_candles():
    """Factory fixture wrapping build_candles."""
    return build_candles


@pytest.fixture
def uptrend():
    """60 bars of a smooth, strong uptrend."""
    return build_candles(np.linspace(100, 140, 60))


@pytest.fixture
def downtrend():
    """60 bars of a smooth, strong downtrend."""
    return build_candles(np.linspace(140, 100, 60))


@pytest.fixture
def choppy():
    """60 bars oscillating in a tight range (deterministic, seeded)."""
    rng = np.random.default_rng(42)
    base = 100 + np.cumsum(rng.normal(0, 0.3, 60))
    return build_candles(base)


def assert_valid_signal(sig, entry_tolerance=1e-6):
    """Assert a strategy signal dict has the expected shape and self-consistent
    stop-loss / target placement relative to its direction."""
    required = {
        "id",
        "tradingsymbol",
        "exchange",
        "strategy",
        "direction",
        "confidence",
        "entryPrice",
        "stopLoss",
        "target",
        "riskReward",
        "reasoning",
        "timestamp",
        "indicators",
    }
    assert required.issubset(sig.keys()), f"missing keys: {required - sig.keys()}"

    assert sig["direction"] in ("BUY", "SELL")
    assert 0 <= sig["confidence"] <= 100
    assert sig["entryPrice"] > 0

    entry, sl, target = sig["entryPrice"], sig["stopLoss"], sig["target"]
    if sig["direction"] == "BUY":
        assert sl < entry + entry_tolerance, "BUY stop-loss should be below entry"
        assert target > entry - entry_tolerance, "BUY target should be above entry"
    else:
        assert sl > entry - entry_tolerance, "SELL stop-loss should be above entry"
        assert target < entry + entry_tolerance, "SELL target should be below entry"


@pytest.fixture(autouse=True)
def mock_journal_overtrading(monkeypatch):
    """Bypass overtrading protections for all tests so they don't fail when
    multiple tests execute signals for the same symbol (e.g. RELIANCE) and hit
    the daily limits across the shared test session database."""
    from backend.journal import journal

    monkeypatch.setattr(
        journal, "get_todays_trade_counts", lambda: {"total": 0, "by_symbol": {}}
    )
    monkeypatch.setattr(journal, "get_last_exit_time", lambda s: None)
