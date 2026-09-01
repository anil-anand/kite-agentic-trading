from ta.volatility import KeltnerChannel

from .base import BaseStrategy


class KeltnerBreakoutStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Keltner Channel Breakout"

    def get_description(self) -> str:
        return "Momentum breakout beyond the upper or lower Keltner bands."

    def calculate_signals(self, df, symbol: str):
        if len(df) < 25:
            return []
        kc = KeltnerChannel(
            high=df["high"], low=df["low"], close=df["close"], window=20, window_atr=10
        )
        df["kc_h"] = kc.keltner_channel_hband()
        df["kc_l"] = kc.keltner_channel_lband()

        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if prev["close"] <= prev["kc_h"] and last["close"] > last["kc_h"]:
            sl = self.calculate_stop_loss(last["close"], "BUY", 0.5)
            target = self.calculate_target(last["close"], sl, 2.0)
            signals.append(
                self.format_signal(
                    symbol,
                    "BUY",
                    82,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Broke above Upper Keltner Channel",
                    {},
                )
            )
        elif prev["close"] >= prev["kc_l"] and last["close"] < last["kc_l"]:
            sl = self.calculate_stop_loss(last["close"], "SELL", 0.5)
            target = self.calculate_target(last["close"], sl, 2.0)
            signals.append(
                self.format_signal(
                    symbol,
                    "SELL",
                    82,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Broke below Lower Keltner Channel",
                    {},
                )
            )
        return signals
