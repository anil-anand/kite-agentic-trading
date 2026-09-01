from ta.volatility import DonchianChannel

from .base import BaseStrategy


class DonchianBreakoutStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Donchian Breakout"

    def get_description(self) -> str:
        return "Trades when price breaks the 20-period high or low."

    def calculate_signals(self, df, symbol: str):
        if len(df) < 25:
            return []
        dc = DonchianChannel(
            high=df["high"], low=df["low"], close=df["close"], window=20
        )
        df["dc_high"] = dc.donchian_channel_hband()
        df["dc_low"] = dc.donchian_channel_lband()

        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last["close"] > prev["dc_high"]:
            sl = self.calculate_stop_loss(last["close"], "BUY", 0.5)
            target = self.calculate_target(last["close"], sl, 2.0)
            signals.append(
                self.format_signal(
                    symbol,
                    "BUY",
                    75,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Price broke 20-period Donchian high",
                    {},
                )
            )
        elif last["close"] < prev["dc_low"]:
            sl = self.calculate_stop_loss(last["close"], "SELL", 0.5)
            target = self.calculate_target(last["close"], sl, 2.0)
            signals.append(
                self.format_signal(
                    symbol,
                    "SELL",
                    75,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Price broke 20-period Donchian low",
                    {},
                )
            )
        return signals
