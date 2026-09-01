from typing import Any, Dict, List

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volume import VolumeWeightedAveragePrice

from .base import BaseStrategy


class VWAPBounceStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "VWAP Bounce"

    def get_description(self) -> str:
        return "Price bounces from VWAP with trend alignment"

    def calculate_signals(
        self, df: pd.DataFrame, tradingsymbol: str
    ) -> List[Dict[str, Any]]:
        if len(df) < 30:
            return []

        signals = []
        df = df.copy()

        vwap = VolumeWeightedAveragePrice(
            high=df["high"], low=df["low"], close=df["close"], volume=df["volume"]
        ).volume_weighted_average_price()

        rsi = RSIIndicator(close=df["close"], window=14).rsi()
        fast_ema = EMAIndicator(close=df["close"], window=9).ema_indicator()
        slow_ema = EMAIndicator(close=df["close"], window=21).ema_indicator()

        df["vwap"] = vwap
        df["rsi"] = rsi
        df["fast_ema"] = fast_ema
        df["slow_ema"] = slow_ema

        last = df.iloc[-1]

        dist_to_vwap = abs(last["close"] - last["vwap"]) / last["vwap"]

        if dist_to_vwap <= 0.002:  # Within 0.2% of VWAP
            # BUY Condition
            if (
                last["close"] > last["open"]
                and last["rsi"] > 40
                and last["fast_ema"] > last["slow_ema"]
                and last["close"] >= last["vwap"]
            ):
                entry = last["close"]
                sl = self.calculate_stop_loss(entry, "BUY")
                target = self.calculate_target(entry, sl)

                confidence = 80
                signals.append(
                    self.format_signal(
                        tradingsymbol,
                        "BUY",
                        confidence,
                        entry,
                        sl,
                        target,
                        round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                        if last["close"] != sl
                        else 0,
                        "Bullish bounce off VWAP, RSI > 40, trend aligned",
                        {"vwap": last["vwap"], "rsi": last["rsi"]},
                    )
                )

            # SELL Condition
            elif (
                last["close"] < last["open"]
                and last["rsi"] < 60
                and last["fast_ema"] < last["slow_ema"]
                and last["close"] <= last["vwap"]
            ):
                entry = last["close"]
                sl = self.calculate_stop_loss(entry, "SELL")
                target = self.calculate_target(entry, sl)

                confidence = 80
                signals.append(
                    self.format_signal(
                        tradingsymbol,
                        "SELL",
                        confidence,
                        entry,
                        sl,
                        target,
                        round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                        if last["close"] != sl
                        else 0,
                        "Bearish rejection from VWAP, RSI < 60, trend aligned",
                        {"vwap": last["vwap"], "rsi": last["rsi"]},
                    )
                )

        return signals
