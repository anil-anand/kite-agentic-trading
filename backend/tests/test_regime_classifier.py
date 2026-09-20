import numpy as np

from backend.regime_classifier import regime_classifier
from backend.tests.conftest import build_candles


def test_regime_trending():
    # Strong uptrend: ADX should be high (>25)
    closes = np.linspace(100, 200, 60)
    df = build_candles(closes)
    result = regime_classifier.classify(df)
    assert result["regime"] == "TRENDING"
    assert result["features"]["adx"] > 25
    assert result["features"]["ema_trending_up"] is True


def test_regime_ranging(choppy):
    # Choppy data: ADX should be low (<20)
    result = regime_classifier.classify(choppy)
    assert result["regime"] == "RANGING"


def test_regime_breakout():
    # Normal volatility then sudden expansion
    closes = list(np.linspace(100, 102, 45))  # tight range
    closes += [105, 110, 118, 130, 150]  # sudden breakout
    df = build_candles(closes)
    result = regime_classifier.classify(df)
    assert result["regime"] == "BREAKOUT"
    assert result["features"]["volatility_expanding"] is True


def test_regime_uncertain():
    # Not enough data
    closes = list(np.linspace(100, 110, 30))
    df = build_candles(closes)
    result = regime_classifier.classify(df)
    assert result["regime"] == "UNCERTAIN"


def test_zero_volume_preserves_raw_regime_but_value_context_is_unknown():
    frame = build_candles(np.linspace(100, 200, 60), volumes=[0] * 60)
    result = regime_classifier.classify(frame)
    assert result["regime"] == "TRENDING"
    assert result["features"]["vwap"] is None
    assert result["features"]["price_above_vwap"] is None
    assert result["features"]["price_below_vwap"] is None
