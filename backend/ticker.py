import json
import random
import sys
import threading
import time

from .dev_mode import is_dev_mode
from .kite_client import kite_client
from .utils import DateTimeEncoder

# Channels the renderer listens on (src/shared/ipc-channels.ts). The engine's
# stdout is the transport, so the "event" name IS the renderer channel.
_TICK_CHANNEL = "ticker:tick"
_ORDER_UPDATE_CHANNEL = "ticker:order-update"


class TickerManager:
    def __init__(self):
        self.ticker = None
        self.thread = None
        self.tokens = set()
        self.running = False
        self._dev = False
        self._dev_thread = None
        self._symbol_map = {}  # instrument_token -> tradingsymbol
        self._dev_prices = {}  # token -> last synthetic price

    def start(self, api_key: str, access_token: str):
        if self.running:
            return

        self._dev = is_dev_mode()
        if self._dev:
            # No real websocket in dev mode — emit synthetic ticks for whatever
            # tokens get subscribed, using the mock client for base prices.
            self.running = True
            self._dev_thread = threading.Thread(target=self._dev_emit_loop)
            self._dev_thread.daemon = True
            self._dev_thread.start()
            return

        from kiteconnect import KiteTicker

        self.ticker = KiteTicker(api_key, access_token)
        self.ticker.on_ticks = self.on_ticks
        self.ticker.on_connect = self.on_connect
        self.ticker.on_close = self.on_close
        self.ticker.on_error = self.on_error
        self.ticker.on_reconnect = self.on_reconnect
        self.ticker.on_noreconnect = self.on_noreconnect
        self.ticker.on_order_update = self.on_order_update

        self.running = True
        self.thread = threading.Thread(
            target=self.ticker.connect, kwargs={"threaded": True}
        )
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass

    def status(self) -> dict:
        return {"running": self.running, "dev": self._dev, "tokens": len(self.tokens)}

    def subscribe(self, tokens: list):
        for token in tokens:
            self.tokens.add(int(token))
        if not self._dev and self.ticker and self.running:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))

    def unsubscribe(self, tokens: list):
        for token in tokens:
            self.tokens.discard(int(token))
        if not self._dev and self.ticker and self.running:
            self.ticker.unsubscribe(tokens)

    # -- token -> symbol -------------------------------------------------
    def _symbol_for(self, token: int) -> str:
        if not self._symbol_map:
            try:
                for i in kite_client.get_instruments("NSE"):
                    self._symbol_map[i["instrument_token"]] = i["tradingsymbol"]
            except Exception:
                pass
        return self._symbol_map.get(int(token), "")

    def _emit_tick(self, token, last_price, change_percent=0.0, volume=0):
        # Emit in the shape the renderer's Tick expects (camelCase, keyed by
        # tradingsymbol) on the renderer's ticker:tick channel.
        symbol = self._symbol_for(token)
        if not symbol:
            return
        event = {
            "event": _TICK_CHANNEL,
            "data": {
                "instrumentToken": token,
                "tradingsymbol": symbol,
                "lastPrice": round(last_price, 2),
                "changePercent": round(change_percent, 2),
                "volume": volume,
                "timestamp": None,
            },
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()

    # -- live callbacks --------------------------------------------------
    def on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick.get("instrument_token")
            ohlc = tick.get("ohlc") or {}
            prev_close = ohlc.get("close") or 0
            ltp = tick.get("last_price") or 0
            change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0
            self._emit_tick(token, ltp, change_pct, tick.get("volume", 0))

    def on_order_update(self, ws, data):
        event = {"event": _ORDER_UPDATE_CHANNEL, "data": data}
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()

    def on_connect(self, ws, response):
        if self.tokens:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))

    def on_close(self, ws, code, reason):
        pass

    def on_error(self, ws, code, reason):
        print(f"Ticker Error: {code} - {reason}", file=sys.stderr)

    def on_reconnect(self, ws, attempts_count):
        pass

    def on_noreconnect(self, ws):
        pass

    # -- dev synthetic emitter -------------------------------------------
    def _dev_emit_loop(self):
        """Emit a synthetic tick for each subscribed token on an interval, so the
        Watchlist and any tick-driven UI update in dev mode with no websocket."""
        while self.running:
            for token in list(self.tokens):
                prev = self._dev_prices.get(token)
                if prev is None:
                    prev = self._dev_seed_price(token)
                drift = random.uniform(-0.004, 0.004)
                price = round(prev * (1 + drift), 2)
                self._dev_prices[token] = price
                change_pct = drift * 100
                self._emit_tick(token, price, change_pct, random.randint(1000, 50000))
            time.sleep(2)

    def _dev_seed_price(self, token):
        try:
            symbol = self._symbol_for(token)
            data = kite_client.get_ltp([f"NSE:{symbol}"]) if symbol else {}
            price = (data.get(f"NSE:{symbol}") or {}).get("last_price")
            if price:
                return float(price)
        except Exception:
            pass
        return 100.0 + (int(token) % 900)


ticker_manager = TickerManager()
