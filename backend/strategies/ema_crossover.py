from typing import Any, Dict, List

import pandas as pd
from ta.trend import EMAIndicator

from .base import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "EMA Crossover"

    def get_description(self) -> str:
        return "Fast EMA(9) crosses Slow EMA(21) with volume confirmation"

    def calculate_signals(
        self, df: pd.DataFrame, tradingsymbol: str
    ) -> List[Dict[str, Any]]:
        if len(df) < 30:
            return []

        signals = []
        df = df.copy()

        # Calculate indicators
        fast_ema = EMAIndicator(close=df["close"], window=9).ema_indicator()
        slow_ema = EMAIndicator(close=df["close"], window=21).ema_indicator()
        vol_sma = df["volume"].rolling(window=20).mean()

        df["fast_ema"] = fast_ema
        df["slow_ema"] = slow_ema
        df["vol_sma"] = vol_sma

        last = df.iloc[-1]
        prev = df.iloc[-2]

        vol_confirmed = last["volume"] > last["vol_sma"]

        # BUY Condition
        if (
            prev["fast_ema"] <= prev["slow_ema"]
            and last["fast_ema"] > last["slow_ema"]
            and vol_confirmed
        ):
            entry = last["close"]
            sl = self.calculate_stop_loss(entry, "BUY")
            target = self.calculate_target(entry, sl)

            vol_ratio = last["volume"] / last["vol_sma"] if last["vol_sma"] > 0 else 1
            confidence = min(100, int(60 + (vol_ratio * 10)))

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
                    f"EMA 9 crossed above EMA 21 with volume ratio {vol_ratio:.2f}x",
                    {
                        "fast_ema": last["fast_ema"],
                        "slow_ema": last["slow_ema"],
                        "vol_sma": last["vol_sma"],
                    },
                )
            )

        # SELL Condition
        elif (
            prev["fast_ema"] >= prev["slow_ema"]
            and last["fast_ema"] < last["slow_ema"]
            and vol_confirmed
        ):
            entry = last["close"]
            sl = self.calculate_stop_loss(entry, "SELL")
            target = self.calculate_target(entry, sl)

            vol_ratio = last["volume"] / last["vol_sma"] if last["vol_sma"] > 0 else 1
            confidence = min(100, int(60 + (vol_ratio * 10)))

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
                    f"EMA 9 crossed below EMA 21 with volume ratio {vol_ratio:.2f}x",
                    {
                        "fast_ema": last["fast_ema"],
                        "slow_ema": last["slow_ema"],
                        "vol_sma": last["vol_sma"],
                    },
                )
            )

        return signals
