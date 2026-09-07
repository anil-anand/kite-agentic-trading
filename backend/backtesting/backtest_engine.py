from typing import Dict

import pandas as pd

from ..strategies.base import BaseStrategy
from .simulated_broker import SimulatedBroker


class BacktestEngine:
    def __init__(self, strategy: BaseStrategy, initial_capital: float = 100000.0):
        self.strategy = strategy
        self.broker = SimulatedBroker(initial_capital=initial_capital)
        self.market_data: Dict[str, pd.DataFrame] = {}  # symbol -> OHLCV DataFrame

    def load_data(self, symbol: str, df: pd.DataFrame):
        """
        Loads OHLCV data for a symbol.
        DataFrame must contain: open, high, low, close, volume, date
        """
        df = df.copy()
        if "date" in df.columns and not pd.api.types.is_datetime64_any_dtype(
            df["date"]
        ):
            df["date"] = pd.to_datetime(df["date"])

        df = df.sort_values("date").reset_index(drop=True)
        self.market_data[symbol] = df

    def run(self):
        """
        Runs the backtest by iterating through the timeline chronologically across all symbols.

        Execution model (avoids look-ahead bias):
          - Signals generated from the slice ending at bar T are queued.
          - They are filled at the OPEN of bar T+1, not at T's close which is
            unknowable until the bar completes.
        End-of-test policy:
          - Any position still open after the last bar is liquidated at that
            bar's close and the trade is recorded with reason 'end_of_test'.
        """
        # Find the global timeline
        all_dates = []
        for df in self.market_data.values():
            all_dates.extend(df["date"].tolist())

        if not all_dates:
            return

        unique_dates = sorted(list(set(all_dates)))

        # Pre-align dataframes
        aligned_data = {}
        for symbol, df in self.market_data.items():
            aligned_data[symbol] = df.set_index("date")

        # pending_orders: signals queued at bar T, to be filled at bar T+1 open.
        # { symbol -> [{"direction", "qty", "signal_info", "queued_at"}] }
        pending_orders: Dict[str, list] = {}

        for current_time in unique_dates:
            # 1. Fill any orders queued at the previous bar — at this bar's open.
            for symbol, orders in list(pending_orders.items()):
                df_sym = aligned_data.get(symbol)
                if df_sym is None or current_time not in df_sym.index:
                    continue
                next_open = float(df_sym.loc[current_time, "open"])
                for order in orders:
                    if symbol not in self.broker.positions:
                        self.broker.place_market_order(
                            symbol=symbol,
                            direction=order["direction"],
                            quantity=order["qty"],
                            price=next_open,  # filled at next bar's open
                            timestamp=current_time,
                            signal_info=order["signal_info"],
                        )
                del pending_orders[symbol]

            # 2. Process open positions (check stops/targets hit in this candle)
            for symbol in list(self.broker.positions.keys()):
                df_sym = aligned_data.get(symbol)
                if df_sym is not None and current_time in df_sym.index:
                    candle = df_sym.loc[current_time].copy()
                    candle["date"] = current_time
                    self.broker.process_candle(symbol, candle)

            # 3. Evaluate strategies on the completed slice up to and including current_time
            for symbol, df_sym in aligned_data.items():
                if current_time in df_sym.index:
                    slice_df = df_sym.loc[
                        :current_time
                    ].reset_index()  # includes current_time

                    # Strategies expect standard columns
                    if len(slice_df) > 0:
                        signals = self.strategy.calculate_signals(slice_df, symbol)

                        # Queue signals for next-bar execution (avoids look-ahead bias)
                        for signal in signals:
                            direction = signal["direction"]
                            entry_price = signal["entryPrice"]

                            # Position sizing: 10% of current equity per trade
                            qty = max(1, int((self.broker.cash * 0.1) / entry_price))

                            if symbol not in self.broker.positions:
                                pending_orders.setdefault(symbol, []).append(
                                    {
                                        "direction": direction,
                                        "qty": qty,
                                        "signal_info": signal,
                                        "queued_at": current_time,
                                    }
                                )

        # 4. End-of-test: liquidate any positions still open at the final bar's close.
        if unique_dates:
            last_time = unique_dates[-1]
            for symbol in list(self.broker.positions.keys()):
                df_sym = aligned_data.get(symbol)
                if df_sym is not None and last_time in df_sym.index:
                    exit_price = float(df_sym.loc[last_time, "close"])
                    self.broker.close_position(
                        symbol=symbol,
                        price=exit_price,
                        timestamp=last_time,
                        reason="end_of_test",
                    )
