from ta.momentum import StochRSIIndicator

from .base import BaseStrategy


class StochRSIStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Stochastic RSI"

    def get_description(self) -> str:
        return "Extremely sensitive momentum strategy trading 20/80 boundaries."

    def calculate_signals(self, df, symbol: str):
        if len(df) < 20:
            return []
        stoch_rsi = StochRSIIndicator(
            close=df["close"], window=14, smooth1=3, smooth2=3
        )
        df["srsi_k"] = stoch_rsi.stochrsi_k()
        df["srsi_d"] = stoch_rsi.stochrsi_d()

        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if (
            prev["srsi_k"] < 0.20
            and last["srsi_k"] >= 0.20
            and last["srsi_k"] > last["srsi_d"]
        ):
            sl = self.calculate_stop_loss(last["close"], "BUY", 0.5)
            target = self.calculate_target(last["close"], sl, 1.2)
            signals.append(
                self.format_signal(
                    symbol,
                    "BUY",
                    77,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "StochRSI K crossed above 0.20",
                    {},
                )
            )
        elif (
            prev["srsi_k"] > 0.80
            and last["srsi_k"] <= 0.80
            and last["srsi_k"] < last["srsi_d"]
        ):
            sl = self.calculate_stop_loss(last["close"], "SELL", 0.5)
            target = self.calculate_target(last["close"], sl, 1.2)
            signals.append(
                self.format_signal(
                    symbol,
                    "SELL",
                    77,
                    last["close"],
                    sl,
                    target,
                    round(abs(target - last["close"]) / abs(last["close"] - sl), 2)
                    if last["close"] != sl
                    else 0,
                    "StochRSI K crossed below 0.80",
                    {},
                )
            )
        return signals
