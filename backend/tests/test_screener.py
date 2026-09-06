from unittest.mock import patch

import pytest

from backend.screener import DynamicScreener


@pytest.fixture
def mock_kite_client():
    with patch("backend.screener.kite_client") as mock:
        yield mock


@pytest.fixture
def screener():
    return DynamicScreener()


@pytest.fixture
def mock_config():
    with patch("backend.screener.config_manager") as mock:
        # Default config that weights volume and liquidity heavily
        mock.get_screener_config.return_value = {
            "weights": {
                "baseline": 1.0,
                "gap": 1.0,
                "volatility": 1.0,
                "trend": 1.0,
                "volume": 5.0,  # Emphasize volume heavily for the test
                "liquidity": 5.0,  # Emphasize liquidity heavily
            },
            "filters": {
                "min_volume": 10000,
                "min_value_traded": 1000000,
            },
        }
        yield mock


def test_screener_prioritizes_tradability(mock_kite_client, mock_config, screener):
    mock_kite_client.get_quote.return_value = {
        "NSE:LOW_VOL_MOVER": {
            "last_price": 110,
            "ohlc": {"open": 100, "high": 112, "low": 98, "close": 100},
            "volume": 15000,
            "average_price": 105,
            "buy_quantity": 1000,
            "sell_quantity": 1000,
        },
        "NSE:HIGH_VOL_STEADY": {
            "last_price": 103,
            "ohlc": {"open": 100, "high": 104, "low": 99, "close": 100},
            "volume": 500000,
            "average_price": 102,
            "buy_quantity": 50000,
            "sell_quantity": 50000,
        },
    }

    universe = ["LOW_VOL_MOVER", "HIGH_VOL_STEADY"]

    watchlist = screener.generate_daily_watchlist(universe=universe, limit=2)

    # Despite LOW_VOL_MOVER having a 10% move vs HIGH_VOL_STEADY's 3% move,
    # HIGH_VOL_STEADY should be ranked higher due to much better tradability (volume/liquidity).
    assert watchlist == ["HIGH_VOL_STEADY", "LOW_VOL_MOVER"]


def test_screener_filters_illiquid(mock_kite_client, mock_config, screener):
    mock_kite_client.get_quote.return_value = {
        "NSE:ILLIQUID": {
            "last_price": 110,
            "ohlc": {"open": 100, "high": 112, "low": 98, "close": 100},
            "volume": 5000,  # Below min_volume 10000
            "average_price": 105,
            "buy_quantity": 100,
            "sell_quantity": 100,
        },
        "NSE:GOOD": {
            "last_price": 103,
            "ohlc": {"open": 100, "high": 104, "low": 99, "close": 100},
            "volume": 500000,
            "average_price": 102,
            "buy_quantity": 50000,
            "sell_quantity": 50000,
        },
    }

    universe = ["ILLIQUID", "GOOD"]

    watchlist = screener.generate_daily_watchlist(universe=universe, limit=2)

    assert watchlist == ["GOOD"]
