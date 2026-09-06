import pandas as pd


class SessionVWAP:
    """
    True Session Volume Weighted Average Price (VWAP).

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
        df = self.df

        if len(df) == 0:
            return pd.Series(dtype=float)

        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        volume = df["volume"]

        # Determine date for grouping
        if "date" in df.columns:
            date_col = pd.to_datetime(df["date"])
        elif isinstance(df.index, pd.DatetimeIndex):
            date_col = df.index
        else:
            # Fallback: if no date is present, just calculate as a single session
            cumulative_vol = volume.cumsum()
            cumulative_pv = (typical_price * volume).cumsum()
            return cumulative_pv / cumulative_vol

        # Convert to specified timezone if it is tz-aware
        if hasattr(date_col, "dt"):
            if date_col.dt.tz is not None:
                date_col = date_col.dt.tz_convert(self.timezone)
            dates = date_col.dt.date
        else:
            if date_col.tz is not None:
                date_col = date_col.tz_convert(self.timezone)
            dates = date_col.date

        df["_session_date"] = dates
        df["_pv"] = typical_price * volume

        # Group by session date and calculate cumulative sums
        grouped = df.groupby("_session_date")

        cumulative_pv = grouped["_pv"].cumsum()
        cumulative_vol = grouped["volume"].cumsum()

        vwap_series = cumulative_pv / cumulative_vol

        return vwap_series
