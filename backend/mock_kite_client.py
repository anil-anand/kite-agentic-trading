"""A mock Kite client for local development (KITE_DEV_MODE=1).

Implements the same surface the app calls on the real KiteClient, returning
synthetic-but-plausible market data so the entire UI and engine can run with no
Zerodha account, no login, and no network. Historical candles are a deterministic
seeded random walk per instrument, so strategies actually produce signals.

Not for any real trading — orders are accepted and given ids but nothing is sent
anywhere, and positions/orders start empty (use paper mode for a simulated book).
"""

import datetime
import random
import zlib

from .nifty_universe import NIFTY_100


def _token_for(symbol: str) -> int:
    """Deterministic, stable instrument token for any symbol.

    Uses a hash so the mock covers the entire scan universe (NIFTY 100 plus any
    custom watchlist symbol) without a hand-written table — every symbol resolves
    to a token, so instruments, quotes, LTP, and candles all work.
    """
    return 100000 + (zlib.crc32(symbol.encode()) % 900000)


# Full dev universe: the same NIFTY 100 the app actually scans.
_UNIVERSE = {symbol: _token_for(symbol) for symbol in NIFTY_100}


def _base_price(token: int) -> float:
    return 100.0 + (token % 900)


_OPEN_STATUSES = {"OPEN", "TRIGGER PENDING"}


class MockKiteClient:
    def __init__(self):
        self.access_token = "dev-token"
        self._order_seq = 0
        # Simulated trading book: the agent's orders actually fill against a
        # live-moving synthetic price, so dev mode shows real positions and P&L.
        self._orders = {}  # order_id -> order dict
        self._positions = {}  # symbol -> position dict
        self._live_prices = {}  # symbol -> current (drifting) price

    # -- auth (no-ops in dev) ----------------------------------------------
    def init(self, api_key):
        pass

    def set_access_token(self, access_token):
        self.access_token = access_token

    def login_url(self):
        return ""

    def generate_session(self, request_token, api_secret):
        return {
            "access_token": "dev-token",
            "user_id": "DEV0001",
            "user_name": "Dev User",
        }

    # -- instruments & market data -----------------------------------------
    def get_instruments(self, exchange=None):
        return [
            {
                "instrument_token": token,
                "exchange_token": str(token),
                "tradingsymbol": symbol,
                "name": symbol,
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
                "tick_size": 0.05,
                "lot_size": 1,
                "last_price": _base_price(token),
            }
            for symbol, token in _UNIVERSE.items()
        ]

    def search_instruments(self, query):
        query = (query or "").upper()
        return [i for i in self.get_instruments("NSE") if query in i["tradingsymbol"]][
            :50
        ]

    def _live_price(self, symbol):
        """Current price that drifts a little on each read, so P&L moves and
        resting stops can trigger over the agent's monitor loop. Seeded from the
        deterministic candle close the first time a symbol is touched."""
        price = self._live_prices.get(symbol)
        if price is None:
            token = _token_for(symbol)
            candles = self._synthetic_candles(token, n=1)
            price = candles[-1]["close"] if candles else _base_price(token)
        price = round(price * (1 + random.uniform(-0.003, 0.003)), 2)
        self._live_prices[symbol] = price
        return price

    def get_ltp(self, instruments):
        out = {}
        for key in instruments:
            symbol = key.split(":")[-1]
            out[key] = {
                "instrument_token": _token_for(symbol),
                "last_price": self._live_price(symbol),
            }
        return out

    def get_quote(self, instruments):
        out = {}
        for key in instruments:
            symbol = key.split(":")[-1]
            token = _token_for(symbol)
            candles = self._synthetic_candles(token, n=2)
            last = candles[-1]
            prev = candles[0]
            out[key] = {
                "instrument_token": token,
                "last_price": last["close"],
                "volume": last["volume"],
                "ohlc": {
                    "open": last["open"],
                    "high": last["high"],
                    "low": last["low"],
                    "close": prev["close"],  # previous close
                },
            }
        return out

    def get_historical_data(
        self, instrument_token, from_date, to_date, interval, continuous=False, oi=False
    ):
        return self._synthetic_candles(int(instrument_token), n=150)

    def _synthetic_candles(self, token, n=150):
        """Deterministic seeded random walk so strategies have real data to chew on."""
        if not token:
            return []
        rng = random.Random(token)
        price = _base_price(token)
        now = datetime.datetime.now()
        candles = []
        for i in range(n):
            open_ = price
            drift = rng.uniform(-0.008, 0.008)
            close = round(open_ * (1 + drift), 2)
            high = round(max(open_, close) * (1 + abs(rng.uniform(0, 0.004))), 2)
            low = round(min(open_, close) * (1 - abs(rng.uniform(0, 0.004))), 2)
            volume = rng.randint(50_000, 500_000)
            candles.append(
                {
                    "date": now - datetime.timedelta(minutes=5 * (n - i)),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
            price = close
        return candles

    def get_holdings(self):
        return []

    def get_margins(self):
        return {
            "equity": {
                "enabled": True,
                "net": 100000.0,
                "available": {"live_balance": 100000.0, "cash": 100000.0},
            },
            "commodity": {},
        }

    # -- simulated order book ---------------------------------------------
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
        self._order_seq += 1
        order_id = f"DEV{self._order_seq}"
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
            # Protective stop rests until the live price crosses the trigger.
            if trigger_price is None:
                order["status"] = "REJECTED"
            else:
                order["status"] = "TRIGGER PENDING"
            return order_id

        # MARKET/LIMIT are marketable in the sim — fill immediately.
        fill = self._live_price(tradingsymbol) if order_type == "MARKET" else price
        if not fill or fill <= 0:
            order["status"] = "REJECTED"
            return order_id
        self._fill(order, fill)
        return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        order = self._orders.get(str(order_id))
        if order and order["status"] in _OPEN_STATUSES:
            order["status"] = "CANCELLED"
        return {"order_id": order_id}

    def modify_order(self, variety, order_id, trigger_price=None, price=None, **kwargs):
        order = self._orders.get(str(order_id))
        if order and order["status"] in _OPEN_STATUSES:
            if trigger_price is not None:
                order["trigger_price"] = trigger_price
            if price is not None:
                order["price"] = price
        return {"order_id": order_id}

    def _fill(self, order, price):
        order["status"] = "COMPLETE"
        order["filledQuantity"] = order["quantity"]
        order["averagePrice"] = price
        self._apply_fill(
            order["tradingsymbol"],
            order["exchange"],
            order["product"],
            order["transaction_type"],
            order["quantity"],
            price,
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
            }
            self._positions[symbol] = pos

        prev_qty = pos["quantity"]
        prev_avg = pos["averagePrice"]
        new_qty = prev_qty + signed

        if prev_qty == 0 or (prev_qty > 0) == (signed > 0):
            # Opening or adding — weighted average.
            total = prev_avg * abs(prev_qty) + price * qty
            pos["averagePrice"] = total / abs(new_qty) if new_qty else 0.0
        else:
            # Reducing / closing / reversing — book realised on the closed part.
            closing = min(qty, abs(prev_qty))
            direction = 1 if prev_qty > 0 else -1
            pos["realised"] += (price - prev_avg) * closing * direction
            if abs(signed) > abs(prev_qty):
                pos["averagePrice"] = price  # reversed
            elif new_qty == 0:
                pos["averagePrice"] = 0.0
        pos["quantity"] = new_qty

    def _check_resting_orders(self):
        for order in self._orders.values():
            if order["status"] != "TRIGGER PENDING":
                continue
            ltp = self._live_price(order["tradingsymbol"])
            trig = order["trigger_price"]
            txn = order["transaction_type"]
            # SELL stop protects a long (fires on the way down); BUY stop
            # protects a short (fires on the way up).
            if (txn == "SELL" and ltp <= trig) or (txn == "BUY" and ltp >= trig):
                self._fill(order, trig)

    def get_positions(self):
        self._check_resting_orders()
        net = []
        for symbol, pos in self._positions.items():
            qty = pos["quantity"]
            ltp = self._live_price(symbol)
            unrealised = (ltp - pos["averagePrice"]) * qty if qty else 0.0
            net.append(
                {
                    "tradingsymbol": symbol,
                    "exchange": pos["exchange"],
                    "product": pos["product"],
                    "quantity": qty,
                    "averagePrice": round(pos["averagePrice"], 2),
                    "lastPrice": ltp,
                    "realised": round(pos["realised"], 2),
                    "unrealised": round(unrealised, 2),
                    "pnl": round(pos["realised"] + unrealised, 2),
                }
            )
        return {"net": net, "day": list(net)}

    def get_orders(self):
        return [dict(o) for o in self._orders.values()]
