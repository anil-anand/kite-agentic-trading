import math
from typing import Any, Dict, Optional


class TradingCostCalculator:
    """
    Calculates charges for Indian Equity Intraday (MIS) trades based on standard brokerage rates.
    Configurable parameters can be passed in if rates change.
    """

    def __init__(
        self,
        brokerage_pct: float = 0.0003,
        max_brokerage: float = 20.0,
        stt_sell_pct: float = 0.00025,
        exchange_txn_pct: float = 0.0000322,
        sebi_pct: float = 0.000001,
        stamp_buy_pct: float = 0.00003,
        gst_pct: float = 0.18,
        rate_version: str = "equity-intraday-rates-v1",
        rounding_version: str = "python-round-paise-v1",
    ):
        self.brokerage_pct = brokerage_pct
        self.max_brokerage = max_brokerage
        self.stt_sell_pct = stt_sell_pct
        self.exchange_txn_pct = exchange_txn_pct
        self.sebi_pct = sebi_pct
        self.stamp_buy_pct = stamp_buy_pct
        self.gst_pct = gst_pct
        self.rate_version = rate_version
        self.rounding_version = rounding_version

    def calculate_turnover_charges(
        self, turnover: float, side: str
    ) -> Dict[str, float]:
        """Calculate charges for one order's aggregate executed turnover.

        Brokerage is capped once for the order.  Callers with partial fills must
        aggregate those fills before using this method.
        """

        if not math.isfinite(turnover) or turnover < 0:
            raise ValueError("turnover must be finite and non-negative")
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        # Brokerage
        brokerage = min(turnover * self.brokerage_pct, self.max_brokerage)

        # STT is only on the sell side for intraday
        stt = 0.0
        if side == "SELL":
            stt = round(turnover * self.stt_sell_pct)

        # Exchange txn charges
        exchange_txn = turnover * self.exchange_txn_pct

        # SEBI charges
        sebi = turnover * self.sebi_pct

        # GST is 18% on (brokerage + SEBI charges + transaction charges)
        gst = (brokerage + sebi + exchange_txn) * self.gst_pct

        # Stamp duty is only on the buy side
        stamp = 0.0
        if side == "BUY":
            stamp = round(turnover * self.stamp_buy_pct)

        brokerage_rnd = round(brokerage, 2)
        exchange_txn_rnd = round(exchange_txn, 2)
        sebi_rnd = round(sebi, 2)
        gst_rnd = round(gst, 2)

        return {
            "brokerage": brokerage_rnd,
            "stt": stt,
            "exchange_txn": exchange_txn_rnd,
            "sebi": sebi_rnd,
            "gst": gst_rnd,
            "stamp": stamp,
            "total": round(
                brokerage_rnd + stt + exchange_txn_rnd + sebi_rnd + gst_rnd + stamp, 2
            ),
        }

    def calculate_leg_charges(
        self, price: float, quantity: int, side: str
    ) -> Dict[str, float]:
        """Compatibility helper for a single-fill/single-order leg."""

        if (
            isinstance(price, bool)
            or not math.isfinite(price)
            or price <= 0
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError("price and quantity must be finite and non-negative")
        return self.calculate_turnover_charges(price * quantity, side)

    def calculate_trade_charges(
        self,
        direction: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        signal_entry_price: Optional[float] = None,
        signal_exit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Calculates total charges, gross P&L, slippage, and net P&L for a complete trade round-trip.
        """
        direction = str(direction).upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL")
        if (
            not math.isfinite(entry_price)
            or not math.isfinite(exit_price)
            or entry_price <= 0
            or exit_price <= 0
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("trade prices and quantity must be finite and positive")

        entry_side = "BUY" if direction == "BUY" else "SELL"
        exit_side = "SELL" if direction == "BUY" else "BUY"

        entry_charges = self.calculate_leg_charges(entry_price, quantity, entry_side)
        exit_charges = self.calculate_leg_charges(exit_price, quantity, exit_side)

        brokerage = entry_charges["brokerage"] + exit_charges["brokerage"]
        stt = entry_charges["stt"] + exit_charges["stt"]
        exchange_txn = entry_charges["exchange_txn"] + exit_charges["exchange_txn"]
        sebi = entry_charges["sebi"] + exit_charges["sebi"]
        gst = entry_charges["gst"] + exit_charges["gst"]
        stamp = entry_charges["stamp"] + exit_charges["stamp"]

        taxes = round(stt + gst + stamp, 2)
        exchange_charges_total = round(exchange_txn + sebi, 2)
        total_fees = round(brokerage + taxes + exchange_charges_total, 2)

        # P&L
        if direction == "BUY":
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        net_pnl = round(gross_pnl - total_fees, 2)

        # Slippage calculation based on limit prices vs actual execution VWAP
        slippage = 0.0
        if signal_entry_price is not None:
            if direction == "BUY":
                slippage -= (entry_price - signal_entry_price) * quantity
            else:
                slippage -= (signal_entry_price - entry_price) * quantity

        if signal_exit_price is not None:
            if direction == "BUY":
                slippage -= (signal_exit_price - exit_price) * quantity
            else:
                slippage -= (exit_price - signal_exit_price) * quantity

        return {
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": net_pnl,
            "brokerage": round(brokerage, 2),
            "taxes": taxes,
            "exchange_charges": exchange_charges_total,
            "other_fees": 0.0,
            "total_fees": total_fees,
            "slippage": round(slippage, 2),
        }


cost_calculator = TradingCostCalculator()
