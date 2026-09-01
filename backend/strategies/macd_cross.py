from typing import Any, Dict, List

import pandas as pd
from ta.trend import MACD

from .base import BaseStrategy


class MACDCrossStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "MACD Cross"

    def get_description(self) -> str:
        return "MACD Line crosses Signal Line"

    def calculate_signals(
        self, df: pd.DataFrame, tradingsymbol: str
    ) -> List[Dict[str, Any]]:
        if len(df) < 35:
            return []

        signals = []
        df = df.copy()

        macd = MACD(close=df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_diff"] = macd.macd_diff()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # BUY Condition
        if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
            entry = last["close"]
            sl = self.calculate_stop_loss(entry, "BUY")
            target = self.calculate_target(entry, sl)
            confidence = (
                75 if last["macd"] < 0 else 65
            )  # Stronger if crossed below zero line

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
                    "MACD crossed above signal line",
                    {"macd": last["macd"], "signal": last["macd_signal"]},
                )
            )

        # SELL Condition
        elif prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
            entry = last["close"]
            sl = self.calculate_stop_loss(entry, "SELL")
            target = self.calculate_target(entry, sl)
            confidence = 75 if last["macd"] > 0 else 65

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
                    "MACD crossed below signal line",
                    {"macd": last["macd"], "signal": last["macd_signal"]},
                )
            )

        return signals
