from ta.momentum import WilliamsRIndicator

from .base import BaseStrategy


class WilliamsRStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Williams %R"

    def get_description(self) -> str:
        return "Momentum strategy trading reversals from -80 and -20 levels."

    def calculate_signals(self, df, symbol: str):
        if len(df) < 20:
            return []
        wr = WilliamsRIndicator(
            high=df["high"], low=df["low"], close=df["close"], lbp=14
        )
        df["wr"] = wr.williams_r()

        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if prev["wr"] < -80 and last["wr"] >= -80:
            sl = self.calculate_stop_loss(last["close"], "BUY", 0.6)
            target = self.calculate_target(last["close"], sl, 1.2)
            signals.append(
                self.format_signal(
                    symbol,
                    "BUY",
                    70,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Williams %R crossed above -80",
                    {},
                )
            )
        elif prev["wr"] > -20 and last["wr"] <= -20:
            sl = self.calculate_stop_loss(last["close"], "SELL", 0.6)
            target = self.calculate_target(last["close"], sl, 1.2)
            signals.append(
                self.format_signal(
                    symbol,
                    "SELL",
                    70,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "Williams %R crossed below -20",
                    {},
                )
            )
        return signals
