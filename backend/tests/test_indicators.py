import numpy as np
import pandas as pd
import pytest

from backend.indicators import SessionVWAP


def test_session_vwap_day_boundary():
    # 2 days of 5 min candles (2 candles per day to keep it simple)
    # Day 1: 09:15, 09:20
    # Day 2: 09:15, 09:20
    dates = pd.to_datetime(
        [
            "2026-09-01 09:15:00",
            "2026-09-01 09:20:00",
            "2026-09-02 09:15:00",
            "2026-09-02 09:20:00",
        ]
    ).tz_localize("Asia/Kolkata")

    df = pd.DataFrame(
        {
            "date": dates,
            "open": [100, 105, 200, 205],
            "high": [105, 110, 205, 210],
            "low": [95, 100, 195, 200],
            "close": [100, 105, 200, 205],
            "volume": [100, 200, 100, 200],
        }
    )

    # Typical prices:
    # D1, C1: (105 + 95 + 100) / 3 = 100
    # D1, C2: (110 + 100 + 105) / 3 = 105
    # D2, C1: (205 + 195 + 200) / 3 = 200
    # D2, C2: (210 + 200 + 205) / 3 = 205

    vwap_ind = SessionVWAP(df)
    vwap_series = vwap_ind.vwap()

    assert len(vwap_series) == 4

    # Check day 1
    # D1, C1 VWAP = 100
    # D1, C2 VWAP = (100 * 100 + 105 * 200) / 300 = 31000 / 300 = 103.333
    assert np.isclose(vwap_series.iloc[0], 100.0)
    assert np.isclose(vwap_series.iloc[1], 103.33333333333333)

    # Check day 2 - It MUST reset and not use day 1's volume/value
    # D2, C1 VWAP = 200 (if it didn't reset, it would be (31000 + 20000)/400 = 127.5)
    # D2, C2 VWAP = (200 * 100 + 205 * 200) / 300 = 61000 / 300 = 203.333
    assert np.isclose(vwap_series.iloc[2], 200.0)
    assert np.isclose(vwap_series.iloc[3], 203.33333333333333)


def test_session_vwap_no_date_column():
    # Test fallback to single session calculation
    df = pd.DataFrame(
        {
            "open": [100, 105],
            "high": [105, 110],
            "low": [95, 100],
            "close": [100, 105],
            "volume": [100, 200],
        }
    )

    vwap_ind = SessionVWAP(df)
    vwap_series = vwap_ind.vwap()

    assert len(vwap_series) == 2
    assert np.isclose(vwap_series.iloc[0], 100.0)
    assert np.isclose(vwap_series.iloc[1], 103.33333333333333)


def test_session_vwap_zero_cumulative_volume_is_unknown():
    df = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-09-01 09:15:00", periods=2, freq="5min", tz="Asia/Kolkata"
            ),
            "open": [100, 101],
            "high": [101, 102],
            "low": [99, 100],
            "close": [100, 101],
            "volume": [0, 0],
        }
    )

    vwap_series = SessionVWAP(df).vwap()

    assert vwap_series.isna().all()


@pytest.mark.parametrize(
    ("column", "invalid"),
    [
        ("close", np.nan),
        ("close", np.inf),
        ("close", 150),
        ("volume", -1),
        ("volume", np.inf),
    ],
)
def test_invalid_candle_leaves_session_vwap_unknown_until_next_session(column, invalid):
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-09-01 09:15",
                    "2026-09-01 09:20",
                    "2026-09-01 09:25",
                    "2026-09-02 09:15",
                ]
            ),
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [100.0] * 4,
        }
    )
    df.loc[1, column] = invalid

    result = SessionVWAP(df).vwap()

    assert result.iloc[0] == 100
    assert result.iloc[1:3].isna().all()
    assert result.iloc[3] == 100


def test_zero_volume_after_observed_trading_preserves_existing_session_vwap():
    df = pd.DataFrame(
        {"high": [101, 151], "low": [99, 149], "close": [100, 150], "volume": [100, 0]}
    )

    assert SessionVWAP(df).vwap().tolist() == [100, 100]


def test_mixed_aware_and_naive_dates_share_the_explicit_exchange_session():
    df = pd.DataFrame(
        {
            "date": [
                "2026-09-01T09:15:00",
                "2026-09-01T03:50:00+00:00",
                "2026-09-02T09:15:00+05:30",
            ],
            "high": [101, 111, 201],
            "low": [99, 109, 199],
            "close": [100, 110, 200],
            "volume": [100, 100, 100],
        }
    )

    assert SessionVWAP(df).vwap().tolist() == [100, 105, 200]


def test_invalid_timestamp_does_not_silently_drop_volume_from_later_vwap():
    df = pd.DataFrame(
        {
            "date": ["2026-09-01T09:15:00", "bad", "2026-09-01T09:25:00"],
            "high": [101, 111, 121],
            "low": [99, 109, 119],
            "close": [100, 110, 120],
            "volume": [100, 100, 100],
        }
    )

    result = SessionVWAP(df).vwap()
    assert result.iloc[0] == 100
    assert result.iloc[1:].isna().all()
