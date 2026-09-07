import datetime
import uuid
from typing import Any, Dict, List

import pandas as pd

from ..trading_costs import cost_calculator


class SimulatedBroker:
    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position details
        self.trades: List[Dict[str, Any]] = []          # completed trades

        # order book for pending limit/stop orders
        self.pending_orders: List[Dict[str, Any]] = []

    def current_equity(self, current_prices: Dict[str, float]) -> float:
        equity = self.cash
        for symbol, pos in self.positions.items():
            qty = pos["quantity"]
            entry_price = pos["entry_price"]
            direction = pos["direction"]
            current_price = current_prices.get(symbol, entry_price)

            if direction == "BUY":
                equity += (current_price - entry_price) * qty
            else:
                equity += (entry_price - current_price) * qty
        return equity

    def place_market_order(self, symbol: str, direction: str, quantity: int, price: float, timestamp: datetime.datetime, signal_info: Dict[str, Any] = None) -> str:
        # Simulate slippage: 0.05%
        slippage_pct = 0.0005
        executed_price = price * (1 + slippage_pct) if direction == "BUY" else price * (1 - slippage_pct)
        executed_price = round(executed_price, 2)

        # Calculate costs
        side = "BUY" if direction == "BUY" else "SELL"
        charges = cost_calculator.calculate_leg_charges(executed_price, quantity, side)

        # Deduct costs from cash
        self.cash -= charges["total"]

        order_id = str(uuid.uuid4())

        if symbol in self.positions:
            # Handle squaring off or adding to position
            pos = self.positions[symbol]
            if pos["direction"] != direction:
                # Assuming complete square off for simplicity
                if pos["quantity"] == quantity:
                    # Close position
                    exit_price = executed_price
                    entry_price = pos["entry_price"]

                    trade_record = {
                        "symbol": symbol,
                        "direction": pos["direction"],
                        "entry_time": pos["entry_time"],
                        "exit_time": timestamp,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "quantity": quantity,
                        "mfe": pos["mfe"],
                        "mae": pos["mae"],
                        "signal_info": pos.get("signal_info", {}),
                    }

                    trade_charges = cost_calculator.calculate_trade_charges(
                        pos["direction"], entry_price, exit_price, quantity
                    )

                    trade_record.update(trade_charges)
                    self.trades.append(trade_record)

                    # Update cash with gross PnL (net PnL already accounted for via leg charges)
                    gross_pnl = trade_charges["gross_pnl"]
                    self.cash += gross_pnl

                    del self.positions[symbol]
                else:
                    raise NotImplementedError("Partial fills/exits not implemented in mock broker yet.")
            else:
                raise NotImplementedError("Pyramiding not implemented in mock broker yet.")
        else:
            # Open new position
            self.positions[symbol] = {
                "direction": direction,
                "quantity": quantity,
                "entry_price": executed_price,
                "entry_time": timestamp,
                "signal_info": signal_info or {},
                "sl": signal_info.get("stopLoss") if signal_info else None,
                "target": signal_info.get("target") if signal_info else None,
                "mfe": executed_price, # Maximum Favorable Excursion
                "mae": executed_price, # Maximum Adverse Excursion
            }

        return order_id

    def process_candle(self, symbol: str, candle: pd.Series):
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]
        high = candle["high"]
        low = candle["low"]
        timestamp = candle["date"] if "date" in candle else datetime.datetime.now()

        sl = pos["sl"]
        target = pos["target"]
        direction = pos["direction"]

        # Update MFE/MAE
        if direction == "BUY":
            pos["mfe"] = max(pos["mfe"], high)
            pos["mae"] = min(pos["mae"], low)
        else:
            pos["mfe"] = min(pos["mfe"], low)
            pos["mae"] = max(pos["mae"], high)

        # Check SL and Target hit
        exit_price = None

        if direction == "BUY":
            if sl is not None and low <= sl:
                # Slippage on SL hit (worse price)
                exit_price = min(candle["open"], sl) * 0.9995
            elif target is not None and high >= target:
                exit_price = max(candle["open"], target)
        else:
            if sl is not None and high >= sl:
                exit_price = max(candle["open"], sl) * 1.0005
            elif target is not None and low <= target:
                exit_price = min(candle["open"], target)

        if exit_price is not None:
            # Place exit market order
            exit_dir = "SELL" if direction == "BUY" else "BUY"
            self.place_market_order(symbol, exit_dir, pos["quantity"], exit_price, timestamp)
