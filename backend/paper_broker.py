"""In-process paper-trading broker.

Presents the same order/position surface the trading engine uses, but simulates
fills against *real* live prices instead of routing to Kite. Market-data reads
(instruments, LTP, quotes, historical) are delegated to the wrapped real client
— paper trading uses real market data; only the orders are simulated. This lets
the entire engine (scanning, sizing, protective stops, graduated exits) run
byte-for-byte identically against a virtual account with zero capital at risk.

Fill model — deliberately simple, deterministic, and documented:
  * MARKET orders fill immediately at the current LTP.
  * LIMIT orders fill immediately at their limit price. The engine only ever
    sends marketable limits (entries at ~LTP, exits at LTP +/- a buffer), so
    this faithfully approximates their behaviour without an order book.
  * SL (stop) orders rest as "TRIGGER PENDING" and fill when the LTP crosses the
    trigger — evaluated on every get_positions() poll (the engine polls ~5s).

Positions are netted per symbol with weighted-average entry pricing; realised
P&L is booked on the closing portion. Not modelled: partial fills, queue
priority, market impact, brokerage/taxes/STT. It's a decision simulator, not a
fills simulator.
"""

import threading

_OPEN_STATUSES = {"OPEN", "TRIGGER PENDING"}


class PaperBroker:
    def __init__(self, market_data_client):
        # Real client used only for market data (LTP, instruments, quotes, ...).
        self._md = market_data_client
        self._lock = threading.RLock()
        self._orders = {}  # order_id -> order dict
        self._positions = {}  # symbol -> position dict
        self._seq = 0
        self._ltp_override = {}  # symbol -> price, for tests / manual ticking

    # -- market-data delegation --------------------------------------------
    def __getattr__(self, name):
        # Only reached for attributes not defined on this class — delegate them
        # (get_instruments, get_ltp, get_quote, get_historical_data, ...) to the
        # real client. Guard against recursion during __init__.
        if name.startswith("_"):
            raise AttributeError(name)
        md = self.__dict__.get("_md")
        if md is None:
            raise AttributeError(name)
        return getattr(md, name)

    # -- test / manual ticking hook ----------------------------------------
    def set_price(self, symbol, price):
        """Override the LTP used for fills and marking (tests / manual ticks)."""
        with self._lock:
            self._ltp_override[symbol] = float(price)

    def _current_ltp(self, symbol, exchange="NSE"):
        if symbol in self._ltp_override:
            return self._ltp_override[symbol]
        key = f"{exchange}:{symbol}"
        try:
            data = self._md.get_ltp([key]) or {}
            entry = data.get(key) or {}
            price = entry.get("last_price")
            if price:
                return float(price)
        except Exception:
            pass
        pos = self._positions.get(symbol)
        if pos and pos.get("lastPrice"):
            return float(pos["lastPrice"])
        return 0.0

    def _next_order_id(self):
        self._seq += 1
        return f"PAPER{self._seq}"

    # -- order placement ---------------------------------------------------
    def place_order(
        self,
        variety,
        exchange,
        tradingsymbol,
        transaction_type,
        quantity,
        product,
        order_type,
        price=None,
        trigger_price=None,
        **kwargs,
    ):
        with self._lock:
            order_id = self._next_order_id()
            qty = int(quantity)
            order = {
                "orderId": order_id,
                "tradingsymbol": tradingsymbol,
                "exchange": exchange,
                "transaction_type": transaction_type,
                "quantity": qty,
                "filledQuantity": 0,
                "product": product,
                "order_type": order_type,
                "price": price,
                "trigger_price": trigger_price,
                "status": "OPEN",
            }
            self._orders[order_id] = order

            if qty <= 0:
                order["status"] = "REJECTED"
                return order_id

            if order_type == "SL":
                # Resting protective stop — fills later when LTP crosses trigger.
                if trigger_price is None:
                    order["status"] = "REJECTED"
                    return order_id
                order["status"] = "TRIGGER PENDING"
                return order_id

            # MARKET / LIMIT — marketable, fill now.
            if order_type == "MARKET":
                fill_price = self._current_ltp(tradingsymbol, exchange)
            else:  # LIMIT
                fill_price = (
                    price if price else self._current_ltp(tradingsymbol, exchange)
                )

            if not fill_price or fill_price <= 0:
                # No price to fill against — reject rather than book a bad fill.
                order["status"] = "REJECTED"
                return order_id

            self._fill(order, fill_price)
            return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        with self._lock:
            order = self._orders.get(str(order_id))
            if order and order["status"] in _OPEN_STATUSES:
                order["status"] = "CANCELLED"

    def modify_order(self, variety, order_id, trigger_price=None, price=None, **kwargs):
        with self._lock:
            order = self._orders.get(str(order_id))
            if not order or order["status"] not in _OPEN_STATUSES:
                return
            if trigger_price is not None:
                order["trigger_price"] = trigger_price
            if price is not None:
                order["price"] = price

    # -- fills & position maths --------------------------------------------
    def _fill(self, order, fill_price):
        order["status"] = "COMPLETE"
        order["filledQuantity"] = order["quantity"]
        order["averagePrice"] = fill_price
        self._apply_fill(
            order["tradingsymbol"],
            order["exchange"],
            order["product"],
            order["transaction_type"],
            order["quantity"],
            fill_price,
        )

    def _apply_fill(self, symbol, exchange, product, txn, qty, price):
        signed = qty if txn == "BUY" else -qty
        pos = self._positions.get(symbol)
        if pos is None:
            pos = {
                "tradingsymbol": symbol,
                "exchange": exchange,
                "product": product,
                "quantity": 0,
                "averagePrice": 0.0,
                "realised": 0.0,
                "lastPrice": price,
            }
            self._positions[symbol] = pos

        prev_qty = pos["quantity"]
        prev_avg = pos["averagePrice"]
        new_qty = prev_qty + signed

        opening_or_adding = prev_qty == 0 or (prev_qty > 0) == (signed > 0)
        if opening_or_adding:
            total_cost = prev_avg * abs(prev_qty) + price * qty
            pos["averagePrice"] = total_cost / abs(new_qty) if new_qty else 0.0
        else:
            # Reducing / closing / reversing.
            closing_qty = min(qty, abs(prev_qty))
            direction = 1 if prev_qty > 0 else -1
            pos["realised"] += (price - prev_avg) * closing_qty * direction
            if abs(signed) > abs(prev_qty):
                pos["averagePrice"] = price  # reversed — leftover opens new side
            elif new_qty == 0:
                pos["averagePrice"] = 0.0
            # else partial close keeps the existing average

        pos["quantity"] = new_qty
        pos["lastPrice"] = price

    def _check_resting_orders(self):
        for order in self._orders.values():
            if order["status"] != "TRIGGER PENDING":
                continue
            symbol = order["tradingsymbol"]
            ltp = self._current_ltp(symbol, order["exchange"])
            if not ltp:
                continue
            trigger = order["trigger_price"]
            txn = order["transaction_type"]
            # SELL stop protects a long -> fires on the way down (ltp <= trigger).
            # BUY stop protects a short -> fires on the way up (ltp >= trigger).
            triggered = (txn == "SELL" and ltp <= trigger) or (
                txn == "BUY" and ltp >= trigger
            )
            if triggered:
                self._fill(order, trigger)

    def get_positions(self):
        with self._lock:
            self._check_resting_orders()
            net = []
            for symbol, pos in self._positions.items():
                ltp = self._current_ltp(symbol, pos["exchange"]) or pos.get(
                    "lastPrice", 0.0
                )
                pos["lastPrice"] = ltp
                qty = pos["quantity"]
                unrealised = (ltp - pos["averagePrice"]) * qty if qty else 0.0
                net.append(
                    {
                        "tradingsymbol": symbol,
                        "exchange": pos["exchange"],
                        "product": pos["product"],
                        "quantity": qty,
                        "averagePrice": pos["averagePrice"],
                        "lastPrice": ltp,
                        "realised": round(pos["realised"], 2),
                        "unrealised": round(unrealised, 2),
                        "pnl": round(pos["realised"] + unrealised, 2),
                    }
                )
            return {"net": net, "day": list(net)}

    def get_orders(self):
        with self._lock:
            return [dict(o) for o in self._orders.values()]
