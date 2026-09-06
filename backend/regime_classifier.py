import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from backend.indicators import SessionVWAP


class RegimeClassifier:
    def __init__(self):
        # Thresholds can be made configurable later
        self.adx_trend_threshold = 25
        self.adx_range_threshold = 20

    def classify(self, df: pd.DataFrame) -> dict:
        """
        Classifies the market regime for the given dataframe.
        Returns a dict with the regime state and feature metrics.
        """
        if len(df) < 50:
            return {"regime": "UNCERTAIN", "features": {}}

        df = df.copy()

        # Calculate Features
        adx_ind = ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"], window=14
        )
        df["adx"] = adx_ind.adx()

        ema_fast = EMAIndicator(close=df["close"], window=20).ema_indicator()
        ema_slow = EMAIndicator(close=df["close"], window=50).ema_indicator()
        df["ema_20"] = ema_fast
        df["ema_50"] = ema_slow

        atr_ind = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14
        )
        df["atr"] = atr_ind.average_true_range()

        vwap_ind = SessionVWAP(df)
        df["vwap"] = vwap_ind.vwap()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        adx = last["adx"]

        # Determine EMA slope (approximated by fast > slow and current fast > previous fast)
        ema_trending_up = (
            last["ema_20"] > last["ema_50"] and last["ema_20"] > prev["ema_20"]
        )
        ema_trending_down = (
            last["ema_20"] < last["ema_50"] and last["ema_20"] < prev["ema_20"]
        )

        # Price relative to VWAP
        price_above_vwap = last["close"] > last["vwap"]
        price_below_vwap = last["close"] < last["vwap"]

        # Volatility expansion (Breakout condition)
        # E.g., current ATR > 1.2 * SMA of ATR
        df["atr_sma"] = df["atr"].rolling(window=14).mean()
        last_atr_sma = df["atr_sma"].iloc[-1]
        volatility_expanding = (
            last["atr"] > 1.2 * last_atr_sma if last_atr_sma > 0 else False
        )

        # Regime Logic
        regime = "UNCERTAIN"

        if volatility_expanding:
            regime = "BREAKOUT"
        elif adx > self.adx_trend_threshold:
            regime = "TRENDING"
        elif adx < self.adx_range_threshold:
            regime = "RANGING"

        features = {
            "adx": round(adx, 2) if not pd.isna(adx) else 0.0,
            "ema_20": round(last["ema_20"], 2) if not pd.isna(last["ema_20"]) else 0.0,
            "ema_50": round(last["ema_50"], 2) if not pd.isna(last["ema_50"]) else 0.0,
            "atr": round(last["atr"], 2) if not pd.isna(last["atr"]) else 0.0,
            "vwap": round(last["vwap"], 2) if not pd.isna(last["vwap"]) else 0.0,
            "volatility_expanding": bool(volatility_expanding),
            "ema_trending_up": bool(ema_trending_up),
            "ema_trending_down": bool(ema_trending_down),
            "price_above_vwap": bool(price_above_vwap),
            "price_below_vwap": bool(price_below_vwap),
        }

        return {"regime": regime, "features": features}


regime_classifier = RegimeClassifier()
