"""A mock Kite client for local development (KITE_DEV_MODE=1).

Implements the same surface the app calls on the real KiteClient, returning
synthetic-but-plausible market data so the entire UI and engine can run with no
Zerodha account, no login, and no network. Historical candles are a deterministic
seeded random walk per instrument, so strategies actually produce signals.

Not for any real trading. Orders are not sent anywhere, but the mock keeps a
full in-memory simulated book: orders fill (market at the live price, limit at
the limit, stop-losses rest and trigger when the drifting price crosses them),
positions mark to a moving price with realised/unrealised P&L, and the
Orders/Positions views populate through the same explicit DTO serializers as
the real client.
"""

import datetime
import random
import zlib

from .broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    OrderRole,
    fill_snapshot_to_renderer_dto,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
    order_snapshot_to_renderer_dto,
    position_snapshot_to_renderer_dto,
)
from .nifty_universe import NIFTY_100
from .time_utils import now_utc


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
        self._fill_seq = 0
        # Simulated trading book: the agent's orders actually fill against a
        # live-moving synthetic price, so dev mode shows real positions and P&L.
        self._orders = {}  # order_id -> order dict
        self._fills = []
        self._order_roles = {}
        self._positions = {}  # symbol -> position dict
        self._live_prices = {}  # symbol -> current (drifting) price
        self.namespace = ExecutionNamespace.DEV
        self.account_id = "DEV0001"

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
        now = now_utc()
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
        order_role=OrderRole.UNKNOWN,
        **kwargs,
    ):
        self._order_seq += 1
        order_id = f"DEV{self._order_seq}"
        qty = int(quantity)
        now = now_utc().isoformat()
        # camelCase keys match the real KiteClient.convert_keys() output the
        # renderer reads (e.g. the Orders page uses o.transactionType).
        order = {
            "orderId": order_id,
            "tradingsymbol": tradingsymbol,
            "instrumentToken": _token_for(tradingsymbol),
            "exchange": exchange,
            "transactionType": transaction_type,
            "quantity": qty,
            "filledQuantity": 0,
            "pendingQuantity": qty,
            "product": product,
            "orderType": order_type,
            "price": price,
            "triggerPrice": trigger_price,
            "status": "OPEN",
            "isAppOrder": True,
            # The Orders UI renders new Date(orderTimestamp); without it dev-mode
            # orders show "Invalid Date".
            "orderTimestamp": now,
            "exchangeTimestamp": now,
        }
        self._orders[order_id] = order
        role = (
            order_role
            if isinstance(order_role, OrderRole)
            else OrderRole(str(order_role).upper())
        )
        self._order_roles[order_id] = role.value

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
            order["pendingQuantity"] = 0
        return {"order_id": order_id}

    def modify_order(self, variety, order_id, trigger_price=None, price=None, **kwargs):
        order = self._orders.get(str(order_id))
        if order and order["status"] in _OPEN_STATUSES:
            if trigger_price is not None:
                order["triggerPrice"] = trigger_price
            if price is not None:
                order["price"] = price
        return {"order_id": order_id}

    def _fill(self, order, price):
        fill_time = now_utc().isoformat()
        order["status"] = "COMPLETE"
        order["filledQuantity"] = order["quantity"]
        order["pendingQuantity"] = 0
        order["averagePrice"] = price
        order["exchangeTimestamp"] = fill_time
        self._fill_seq += 1
        self._fills.append(
            {
                "tradeId": f"DEVF{self._fill_seq}",
                "orderId": order["orderId"],
                "tradingsymbol": order["tradingsymbol"],
                "instrumentToken": _token_for(order["tradingsymbol"]),
                "exchange": order["exchange"],
                "product": order["product"],
                "transactionType": order["transactionType"],
                "quantity": order["quantity"],
                "averagePrice": price,
                "fillTimestamp": fill_time,
            }
        )
        self._apply_fill(
            order["tradingsymbol"],
            order["exchange"],
            order["product"],
            order["transactionType"],
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
                "buyQuantity": 0,
                "sellQuantity": 0,
                "buyValue": 0.0,
                "sellValue": 0.0,
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
        if txn == "BUY":
            pos["buyQuantity"] += qty
            pos["buyValue"] += price * qty
        else:
            pos["sellQuantity"] += qty
            pos["sellValue"] += price * qty

    def _check_resting_orders(self):
        for order in self._orders.values():
            if order["status"] != "TRIGGER PENDING":
                continue
            ltp = self._live_price(order["tradingsymbol"])
            trig = order["triggerPrice"]
            txn = order["transactionType"]
            # SELL stop protects a long (fires on the way down); BUY stop
            # protects a short (fires on the way up).
            if (txn == "SELL" and ltp <= trig) or (txn == "BUY" and ltp >= trig):
                self._fill(order, trig)

    def _raw_positions(self):
        self._check_resting_orders()
        net = []
        for symbol, pos in self._positions.items():
            qty = pos["quantity"]
            ltp = self._live_price(symbol)
            unrealised = (ltp - pos["averagePrice"]) * qty if qty else 0.0
            net.append(
                {
                    "tradingsymbol": symbol,
                    "instrumentToken": _token_for(symbol),
                    "exchange": pos["exchange"],
                    "product": pos["product"],
                    "quantity": qty,
                    "averagePrice": round(pos["averagePrice"], 2),
                    "lastPrice": ltp,
                    # This price was generated by the simulator on this read.
                    "timestamp": now_utc().isoformat(),
                    "realised": round(pos["realised"], 2),
                    "unrealised": round(unrealised, 2),
                    "pnl": round(pos["realised"] + unrealised, 2),
                    "buyQuantity": pos["buyQuantity"],
                    "sellQuantity": pos["sellQuantity"],
                    "dayBuyQuantity": pos["buyQuantity"],
                    "daySellQuantity": pos["sellQuantity"],
                    "buyValue": round(pos["buyValue"], 2),
                    "sellValue": round(pos["sellValue"], 2),
                }
            )
        return {"net": net, "day": list(net)}

    def get_positions(self):
        return position_snapshot_to_renderer_dto(self.get_positions_snapshot())

    def _raw_orders(self):
        return [dict(o) for o in self._orders.values()]

    def get_orders(self):
        # Preserve the historical direct-mock list API.  The RPC boundary uses
        # get_order_history_snapshot() and carries quality beside the rows.
        return order_snapshot_to_renderer_dto(self.get_current_orders_snapshot())[
            "orders"
        ]

    def _raw_fills(self):
        return [dict(fill) for fill in self._fills]

    def get_trades(self):
        return fill_snapshot_to_renderer_dto(self.get_fills_snapshot())

    def get_positions_snapshot(self):
        return normalize_positions_response(
            self._raw_positions(),
            namespace=self.namespace,
            account_id=self.account_id,
        )

    def get_current_orders_snapshot(self):
        return normalize_orders_response(
            self._raw_orders(),
            namespace=self.namespace,
            account_id=self.account_id,
            roles_by_order_id=self._order_roles,
        )

    def get_order_history_snapshot(self, current_snapshot=None):
        return current_snapshot or self.get_current_orders_snapshot()

    def get_fills_snapshot(self):
        return normalize_fills_response(
            self._raw_fills(), namespace=self.namespace, account_id=self.account_id
        )

    def get_broker_snapshot(self):
        positions = self.get_positions_snapshot()
        orders = self.get_current_orders_snapshot()
        fills = self.get_fills_snapshot()
        return BrokerSnapshot(
            namespace=self.namespace,
            account_id=self.account_id,
            positions=positions.net,
            day_positions=positions.day,
            current_orders=orders.orders,
            fills=fills.fills,
            positions_quality=positions.quality,
            orders_quality=orders.quality,
            fills_quality=fills.quality,
            fetched_at=max(positions.fetched_at, orders.fetched_at, fills.fetched_at),
            positions_fetched_at=positions.fetched_at,
            orders_fetched_at=orders.fetched_at,
            fills_fetched_at=fills.fetched_at,
        )
