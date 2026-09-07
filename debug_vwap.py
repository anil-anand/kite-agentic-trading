import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

from backend.indicators import SessionVWAP
from backend.tests.conftest import build_candles


def main():
    print("--- VWAP BOUNCE ---")
    base = list(np.linspace(100, 112, 34))
    closes = base + [106.12]
    opens = list(closes)
    opens[-1] = 106.12 * 0.999
    df = build_candles(closes, opens=opens)

    vwap = SessionVWAP(df).vwap()
    rsi = RSIIndicator(close=df["close"], window=14).rsi()
    fast_ema = EMAIndicator(close=df["close"], window=9).ema_indicator()
    slow_ema = EMAIndicator(close=df["close"], window=21).ema_indicator()

    print("VWAP:", vwap.iloc[-1])
    print("Close:", df["close"].iloc[-1])
    print("Open:", df["open"].iloc[-1])
    print("RSI:", rsi.iloc[-1])
    print("fast EMA:", fast_ema.iloc[-1])
    print("slow EMA:", slow_ema.iloc[-1])


if __name__ == "__main__":
    main()
