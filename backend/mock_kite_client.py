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

# A small, fixed dev universe. tradingsymbol -> instrument_token.
_UNIVERSE = {
    "RELIANCE": 738561,
    "TCS": 2953217,
    "INFY": 408065,
    "HDFCBANK": 341249,
    "ICICIBANK": 1270529,
    "SBIN": 779521,
    "ITC": 424961,
    "AXISBANK": 1510401,
}
_TOKEN_TO_SYMBOL = {t: s for s, t in _UNIVERSE.items()}


def _base_price(token: int) -> float:
    return 100.0 + (token % 900)


class MockKiteClient:
    def __init__(self):
        self.access_token = "dev-token"
        self._order_seq = 0

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

    def _ltp_for(self, symbol):
        token = _UNIVERSE.get(symbol, 0)
        candles = self._synthetic_candles(token, n=1)
        return candles[-1]["close"] if candles else _base_price(token)

    def get_ltp(self, instruments):
        out = {}
        for key in instruments:
            symbol = key.split(":")[-1]
            out[key] = {
                "instrument_token": _UNIVERSE.get(symbol, 0),
                "last_price": self._ltp_for(symbol),
            }
        return out

    def get_quote(self, instruments):
        out = {}
        for key in instruments:
            symbol = key.split(":")[-1]
            token = _UNIVERSE.get(symbol, 0)
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

    # -- account (empty book in dev) ---------------------------------------
    def get_positions(self):
        return {"net": [], "day": []}

    def get_orders(self):
        return []

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

    # -- orders (accepted, but nothing is sent anywhere) -------------------
    def place_order(self, **kwargs):
        self._order_seq += 1
        return f"DEV{self._order_seq}"

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return {"order_id": order_id}

    def modify_order(self, **kwargs):
        return {"order_id": kwargs.get("order_id")}
