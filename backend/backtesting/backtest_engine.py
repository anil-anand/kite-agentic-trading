from typing import Dict

import pandas as pd

from ..strategies.base import BaseStrategy
from .simulated_broker import SimulatedBroker


class BacktestEngine:
    def __init__(self, strategy: BaseStrategy, initial_capital: float = 100000.0):
        self.strategy = strategy
        self.broker = SimulatedBroker(initial_capital=initial_capital)
        self.market_data: Dict[str, pd.DataFrame] = {} # symbol -> OHLCV DataFrame

    def load_data(self, symbol: str, df: pd.DataFrame):
        """
        Loads OHLCV data for a symbol.
        DataFrame must contain: open, high, low, close, volume, date
        """
        df = df.copy()
        if 'date' in df.columns and not pd.api.types.is_datetime64_any_dtype(df['date']):
            df['date'] = pd.to_datetime(df['date'])

        df = df.sort_values('date').reset_index(drop=True)
        self.market_data[symbol] = df

    def run(self):
        """
        Runs the backtest by iterating through the timeline chronologically across all symbols.
        """
        # Find the global timeline
        all_dates = []
        for df in self.market_data.values():
            all_dates.extend(df['date'].tolist())

        if not all_dates:
            return

        unique_dates = sorted(list(set(all_dates)))

        # We need at least enough history to generate first signals
        # Let's start from index 50 to give strategies a warm-up period
        # Alternatively, we just iterate from start and pass available slices.
        # Strategies typically have an early return if len(df) < required_window

        # Pre-align dataframes
        aligned_data = {}
        for symbol, df in self.market_data.items():
            aligned_data[symbol] = df.set_index('date')

        for current_time in unique_dates:
            # 1. Process open positions (check stops/targets hit in this candle)
            for symbol in list(self.broker.positions.keys()):
                df_sym = aligned_data.get(symbol)
                if df_sym is not None and current_time in df_sym.index:
                    candle = df_sym.loc[current_time]
                    candle['date'] = current_time
                    self.broker.process_candle(symbol, candle)

            # 2. Evaluate strategies on the slice up to current_time
            for symbol, df_sym in aligned_data.items():
                if current_time in df_sym.index:
                    slice_df = df_sym.loc[:current_time].reset_index() # includes current_time

                    # Strategies expect standard columns
                    if len(slice_df) > 0:
                        signals = self.strategy.calculate_signals(slice_df, symbol)

                        # Process signals
                        for signal in signals:
                            # Simple execution: place market order for entry
                            direction = signal['direction']
                            entry_price = signal['entryPrice'] # This is the price the strategy assumed

                            # Simple position sizing: e.g. 1% risk
                            # But for backtesting, let's keep it simple: use 10% of equity per trade
                            qty = max(1, int((self.broker.cash * 0.1) / entry_price))

                            if symbol not in self.broker.positions:
                                self.broker.place_market_order(
                                    symbol=symbol,
                                    direction=direction,
                                    quantity=qty,
                                    price=entry_price, # Use close of this candle
                                    timestamp=current_time,
                                    signal_info=signal
                                )
