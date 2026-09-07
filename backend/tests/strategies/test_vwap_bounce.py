from unittest.mock import patch

import pandas as pd

from backend.strategies.vwap_bounce import VWAPBounceStrategy


def test_vwap_bounce_long():
    strategy = VWAPBounceStrategy(vwap_tolerance=0.002)

    data = []
    for i in range(30):
        data.append(
            {
                "timestamp": pd.Timestamp("2024-01-01 09:15:00")
                + pd.Timedelta(minutes=i * 5),
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 100,
                "volume": 1000,
            }
        )

    # Prior candle
    data[-2] = {
        "timestamp": pd.Timestamp("2024-01-01 11:35:00"),
        "open": 101,
        "high": 101.5,
        "low": 99.9,  # Below VWAP of 100 (99.9 <= 100 * 1.002 = 100.2)
        "close": 100.5,
        "volume": 1500,
    }

    # Current candle
    data[-1] = {
        "timestamp": pd.Timestamp("2024-01-01 11:40:00"),
        "open": 100.6,
        "high": 102,
        "low": 100.5,
        "close": 101.5,  # closes above open and above VWAP
        "volume": 2000,
    }

    df = pd.DataFrame(data)

    with (
        patch("backend.strategies.vwap_bounce.SessionVWAP") as MockVWAP,
        patch("backend.strategies.vwap_bounce.RSIIndicator") as MockRSI,
        patch("backend.strategies.vwap_bounce.EMAIndicator") as MockEMA,
    ):
        MockVWAP.return_value.vwap.return_value = pd.Series([100.0] * 30)

        mock_rsi = pd.Series([50.0] * 30)
        mock_rsi.iloc[-1] = 60.0
        MockRSI.return_value.rsi.return_value = mock_rsi

        def ema_side_effect(close, window):
            mock = type("MockEMA", (), {})()
            if window == 9:
                mock.ema_indicator = lambda: pd.Series([101.0] * 30)
            else:
                mock.ema_indicator = lambda: pd.Series([99.0] * 30)
            return mock

        MockEMA.side_effect = ema_side_effect

        signals = strategy.calculate_signals(df, "TEST_SYMBOL")

        assert len(signals) == 1
        assert signals[0]["direction"] == "BUY"
        assert signals[0]["indicators"]["vwap_touch"] is True
        assert signals[0]["indicators"]["reclaim"] is True
        assert signals[0]["indicators"]["confirmation"] is True


def test_vwap_bounce_short():
    strategy = VWAPBounceStrategy(vwap_tolerance=0.002)

    data = []
    for i in range(30):
        data.append(
            {
                "timestamp": pd.Timestamp("2024-01-01 09:15:00")
                + pd.Timedelta(minutes=i * 5),
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 100,
                "volume": 1000,
            }
        )

    # Prior candle
    data[-2] = {
        "timestamp": pd.Timestamp("2024-01-01 11:35:00"),
        "open": 99,
        "high": 100.1,  # Above VWAP of 100 (100.1 >= 100 * 0.998 = 99.8)
        "low": 98.5,
        "close": 99.5,
        "volume": 1500,
    }

    # Current candle
    data[-1] = {
        "timestamp": pd.Timestamp("2024-01-01 11:40:00"),
        "open": 99.4,
        "high": 99.5,
        "low": 98.0,
        "close": 98.5,  # closes below open and below VWAP
        "volume": 2000,
    }

    df = pd.DataFrame(data)

    with (
        patch("backend.strategies.vwap_bounce.SessionVWAP") as MockVWAP,
        patch("backend.strategies.vwap_bounce.RSIIndicator") as MockRSI,
        patch("backend.strategies.vwap_bounce.EMAIndicator") as MockEMA,
    ):
        MockVWAP.return_value.vwap.return_value = pd.Series([100.0] * 30)

        mock_rsi = pd.Series([50.0] * 30)
        mock_rsi.iloc[-1] = 40.0  # RSI < 60
        MockRSI.return_value.rsi.return_value = mock_rsi

        def ema_side_effect(close, window):
            mock = type("MockEMA", (), {})()
            if window == 9:
                mock.ema_indicator = lambda: pd.Series([99.0] * 30)
            else:
                mock.ema_indicator = lambda: pd.Series([101.0] * 30)
            return mock

        MockEMA.side_effect = ema_side_effect

        signals = strategy.calculate_signals(df, "TEST_SYMBOL")

        assert len(signals) == 1
        assert signals[0]["direction"] == "SELL"
        assert signals[0]["indicators"]["vwap_touch"] is True
        assert signals[0]["indicators"]["reclaim"] is True
        assert signals[0]["indicators"]["confirmation"] is True
