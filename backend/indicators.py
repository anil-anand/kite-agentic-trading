import numpy as np
import pandas as pd

SESSION_VWAP_DEFINITION = "session_typical_price_v1"


class SessionVWAP:
    """
    Session-reset candle approximation of Volume Weighted Average Price (VWAP).

    This calculates the VWAP by resetting the cumulative price*volume and
    cumulative volume at the start of each trading session.

    Session is determined by grouping the DataFrame by the date component of the 'date' column
    or the index if it's a datetime index. The timezone is assumed to be Asia/Kolkata
    if the datetimes are timezone-aware, or naive local time matching Kolkata.

    Note: Broker-provided day VWAP/average price is usually calculated tick-by-tick
    and is exact. This locally calculated VWAP is an approximation based on 5-minute
    (or other interval) candles' typical price.
    """

    def __init__(self, df: pd.DataFrame, timezone: str = "Asia/Kolkata"):
        self.df = df.copy()
        self.timezone = timezone

    def vwap(self) -> pd.Series:
        df = self.df.copy()

        if len(df) == 0:
            return pd.Series(dtype=float)

        required = {"high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            return pd.Series(float("nan"), index=df.index, dtype=float)

        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        volume = pd.to_numeric(df["volume"], errors="coerce")
        valid = (
            np.isfinite(high)
            & np.isfinite(low)
            & np.isfinite(close)
            & np.isfinite(volume)
            & (low > 0)
            & (high >= low)
            & (close >= low)
            & (close <= high)
            & (volume >= 0)
        )
        # An invalid candle leaves the cumulative session numerator/denominator
        # unknown. Skipping just its price (or silently skipping the candle)
        # would manufacture a plausible but incomplete VWAP on later bars.
        valid_volume = volume.where(valid)
        typical_price = (high + low + close) / 3

        # Determine date for grouping
        if "date" in df.columns:
            date_col = df["date"]
        elif isinstance(df.index, pd.DatetimeIndex):
            date_col = df.index
        else:
            # Fallback: if no date is present, just calculate as a single session
            cumulative_vol = valid_volume.cumsum()
            cumulative_pv = (typical_price * valid_volume).cumsum()
            result = cumulative_pv / cumulative_vol.where(cumulative_vol > 0)
            return result.where(valid.cummin()).astype(float)

        def session_date(value):
            # Mixed aware/naive broker frames cannot be parsed in one vectorized
            # call without either raising or interpreting naive values as UTC.
            try:
                timestamp = pd.Timestamp(value)
                if pd.isna(timestamp):
                    return pd.NaT
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize(self.timezone)
                return timestamp.tz_convert(self.timezone).date()
            except (TypeError, ValueError):
                return pd.NaT

        dates = [session_date(value) for value in date_col]

        df["_session_date"] = dates
        df["_pv"] = typical_price * valid_volume
        df["_valid_volume"] = valid_volume
        df["_valid"] = valid

        # Group by session date and calculate cumulative sums
        grouped = df.groupby("_session_date")

        cumulative_pv = grouped["_pv"].cumsum()
        cumulative_vol = grouped["_valid_volume"].cumsum()

        vwap_series = cumulative_pv / cumulative_vol.where(cumulative_vol > 0)

        # An unassignable timestamp also leaves subsequent session totals
        # uncertain; callers must normalize or repair the source frame first.
        known_dates = df["_session_date"].notna().cummin()
        valid_session = grouped["_valid"].cummin().eq(True)
        return vwap_series.where(valid_session & known_dates).astype(float)
