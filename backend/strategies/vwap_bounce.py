from typing import Any, Dict, List

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

from backend.indicators import SessionVWAP

from .base import BaseStrategy


class VWAPBounceStrategy(BaseStrategy):
    def __init__(self, *args, vwap_tolerance: float = 0.002, **kwargs):
        super().__init__(*args, **kwargs)
        self.vwap_tolerance = vwap_tolerance

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

        vwap = SessionVWAP(df).vwap()

        rsi = RSIIndicator(close=df["close"], window=14).rsi()
        fast_ema = EMAIndicator(close=df["close"], window=9).ema_indicator()
        slow_ema = EMAIndicator(close=df["close"], window=21).ema_indicator()

        df["vwap"] = vwap
        df["rsi"] = rsi
        df["fast_ema"] = fast_ema
        df["slow_ema"] = slow_ema

        if len(df) < 2:
            return signals

        current = df.iloc[-1]
        prior = df.iloc[-2]

        # Long Bounce Logic
        long_touch = prior["low"] <= prior["vwap"] * (
            1 + self.vwap_tolerance
        ) and prior["high"] >= prior["vwap"] * (1 - self.vwap_tolerance)
        long_rejection = prior["close"] > prior["low"] or current["low"] > prior["low"]
        long_reclaim = (
            current["close"] > current["open"] and current["close"] > current["vwap"]
        )
        long_confirmation = (
            current["rsi"] > 40 and current["fast_ema"] > current["slow_ema"]
        )

        if long_touch and long_reclaim:
            evidence = {
                "vwap_touch": bool(long_touch),
                "rejection": bool(long_rejection),
                "reclaim": bool(long_reclaim),
                "confirmation": bool(long_confirmation),
                "vwap": float(current["vwap"]),
                "rsi": float(current["rsi"]),
                "prior_low": float(prior["low"]),
                "current_close": float(current["close"]),
            }
            if long_confirmation:
                entry = current["close"]
                sl = self.calculate_stop_loss(entry, "BUY")
                target = self.calculate_target(entry, sl)

                signal_score = 80
                signals.append(
                    self.format_signal(
                        tradingsymbol,
                        "BUY",
                        signal_score,
                        entry,
                        sl,
                        target,
                        round(
                            abs(target - current["close"]) / abs(current["close"] - sl),
                            2,
                        )
                        if current["close"] != sl
                        else 0,
                        "Bullish bounce off VWAP",
                        evidence,
                    )
                )

        # Short Bounce Logic
        short_touch = prior["high"] >= prior["vwap"] * (
            1 - self.vwap_tolerance
        ) and prior["low"] <= prior["vwap"] * (1 + self.vwap_tolerance)
        short_rejection = (
            prior["close"] < prior["high"] or current["high"] < prior["high"]
        )
        short_reclaim = (
            current["close"] < current["open"] and current["close"] < current["vwap"]
        )
        short_confirmation = (
            current["rsi"] < 60 and current["fast_ema"] < current["slow_ema"]
        )

        if short_touch and short_reclaim:
            evidence = {
                "vwap_touch": bool(short_touch),
                "rejection": bool(short_rejection),
                "reclaim": bool(short_reclaim),
                "confirmation": bool(short_confirmation),
                "vwap": float(current["vwap"]),
                "rsi": float(current["rsi"]),
                "prior_high": float(prior["high"]),
                "current_close": float(current["close"]),
            }
            if short_confirmation:
                entry = current["close"]
                sl = self.calculate_stop_loss(entry, "SELL")
                target = self.calculate_target(entry, sl)

                signal_score = 80
                signals.append(
                    self.format_signal(
                        tradingsymbol,
                        "SELL",
                        signal_score,
                        entry,
                        sl,
                        target,
                        round(
                            abs(target - current["close"]) / abs(current["close"] - sl),
                            2,
                        )
                        if current["close"] != sl
                        else 0,
                        "Bearish rejection from VWAP",
                        evidence,
                    )
                )

        return signals
