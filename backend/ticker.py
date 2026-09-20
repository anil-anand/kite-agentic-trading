import json
import math
import random
import sys
import threading
import time
from datetime import datetime, timezone

from .dev_mode import is_dev_mode
from .kite_client import kite_client
from .time_utils import as_utc, now_utc
from .utils import DateTimeEncoder
from .utils import stdout_lock as _stdout_lock

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
        self._order_update_listeners = []
        self._connection_state = "DISCONNECTED"
        self._last_tick_at = None
        self._last_tick_received_at = None
        self._observed_by_token = {}

    def add_order_update_listener(self, listener):
        """Register a backend consumer without giving ticker callbacks state ownership."""

        if listener not in self._order_update_listeners:
            self._order_update_listeners.append(listener)

    def remove_order_update_listener(self, listener):
        if listener in self._order_update_listeners:
            self._order_update_listeners.remove(listener)

    def start(self, api_key: str, access_token: str):
        if self.running:
            return

        # Tests and the dev session use explicit sentinel credentials.  Treat
        # those as synthetic mode even when the module-level environment flag
        # was imported before the test changed it; never open a websocket for
        # the sentinel pair.
        self._dev = is_dev_mode() or (api_key == "dev" and access_token == "dev")
        if self._dev:
            # No real websocket in dev mode — emit synthetic ticks for whatever
            # tokens get subscribed, using the mock client for base prices.
            self.running = True
            self._connection_state = "CONNECTED"
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
        self._connection_state = "CONNECTING"
        self.thread = threading.Thread(
            target=self.ticker.connect, kwargs={"threaded": True}
        )
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        self._connection_state = "DISCONNECTED"
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "running": self.running,
            "dev": self._dev,
            "tokens": len(self.tokens),
            "connectionState": self._connection_state,
            "streamAvailable": self._connection_state == "CONNECTED",
            "lastTickAt": self._last_tick_at.isoformat()
            if self._last_tick_at
            else None,
            "lastTickReceivedAt": self._last_tick_received_at.isoformat()
            if self._last_tick_received_at
            else None,
        }

    def subscribe(self, tokens: list):
        for token in tokens:
            self.tokens.add(int(token))
        if not self._dev and self.ticker and self.running:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))

    def unsubscribe(self, tokens: list):
        # Normalize to int so the broker unsubscribe matches the int tokens we
        # subscribed with — a stringified token from the renderer would
        # otherwise leave a stale subscription active.
        normalized = [int(token) for token in tokens]
        for token in normalized:
            self.tokens.discard(token)
        if not self._dev and self.ticker and self.running:
            self.ticker.unsubscribe(normalized)

    # -- token -> symbol -------------------------------------------------
    def _symbol_for(self, token: int) -> str:
        if not self._symbol_map:
            try:
                for i in kite_client.get_instruments("NSE"):
                    self._symbol_map[i["instrument_token"]] = i["tradingsymbol"]
            except Exception:
                pass
        return self._symbol_map.get(int(token), "")

    def _emit_tick(
        self,
        token,
        last_price,
        change_percent=0.0,
        volume=0,
        observed_at=None,
    ):
        # Emit in the shape the renderer's Tick expects (camelCase, keyed by
        # tradingsymbol) on the renderer's ticker:tick channel.
        try:
            token = int(token)
        except (TypeError, ValueError):
            return
        symbol = self._symbol_for(token)
        if not symbol:
            return
        received_at = now_utc()
        try:
            source_time = as_utc(observed_at)
        except (TypeError, ValueError):
            source_time = None
        self._last_tick_received_at = received_at
        quality = "EXCHANGE" if source_time is not None else "RECEIPT_ONLY"
        if self._dev:
            source_time = received_at
            quality = "SYNTHETIC"
        if source_time is not None:
            # A future timestamp is not an observation available at receipt.
            # Reject it rather than poisoning the monotonic high-water mark and
            # consequently dropping every subsequent correctly timed quote.
            if source_time > received_at:
                return
            # Reconnection/batched delivery must not move an instrument's mark
            # backwards in source time. The global freshness high-water mark
            # likewise cannot regress when a less active symbol next updates.
            previous = self._observed_by_token.get(token)
            if previous is not None and source_time < previous:
                return
            self._observed_by_token[token] = source_time
            self._last_tick_at = max(self._last_tick_at or source_time, source_time)
        # Keep the existing renderer timestamp parseable, but never promote a
        # receipt-only observation to the feed's known source freshness.
        timestamp = source_time or received_at
        event = {
            "event": _TICK_CHANNEL,
            "data": {
                "instrumentToken": token,
                "tradingsymbol": symbol,
                "lastPrice": round(last_price, 2),
                "changePercent": round(change_percent, 2),
                "volume": volume,
                "timestamp": timestamp.isoformat(),
                "observedAt": source_time.isoformat() if source_time else None,
                "receivedAt": received_at.isoformat(),
                "timestampQuality": quality,
            },
        }
        with _stdout_lock:
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()

    # -- live callbacks --------------------------------------------------
    def on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick.get("instrument_token")
            ohlc = tick.get("ohlc") or {}
            try:
                ltp = float(tick.get("last_price"))
                prev_close = float(ohlc.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ltp) or ltp <= 0:
                continue
            if not math.isfinite(prev_close) or prev_close <= 0:
                prev_close = 0
            change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0
            observed_at = tick.get("exchange_timestamp") or tick.get("timestamp")
            if isinstance(observed_at, datetime) and observed_at.tzinfo is None:
                # KiteTicker decodes wire epoch seconds with
                # datetime.fromtimestamp(), returning host-local naive values.
                # Undo that SDK conversion here, before the domain convention
                # (naive broker ISO strings are exchange-local) can be applied.
                observed_at = observed_at.astimezone(timezone.utc)
            self._emit_tick(
                token,
                ltp,
                change_pct,
                tick.get("volume_traded", tick.get("volume", 0)),
                observed_at=observed_at,
            )

    def on_order_update(self, ws, data):
        for listener in tuple(self._order_update_listeners):
            try:
                listener(dict(data))
            except Exception as exc:
                print(f"Ticker order listener failed: {exc}", file=sys.stderr)
        event = {"event": _ORDER_UPDATE_CHANNEL, "data": data}
        with _stdout_lock:
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()

    def on_connect(self, ws, response):
        self._connection_state = "CONNECTED"
        if self.tokens:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))

    def on_close(self, ws, code, reason):
        self._connection_state = "DISCONNECTED"

    def on_error(self, ws, code, reason):
        print(f"Ticker Error: {code} - {reason}", file=sys.stderr)

    def on_reconnect(self, ws, attempts_count):
        self._connection_state = "RECONNECTING"

    def on_noreconnect(self, ws):
        self._connection_state = "DISCONNECTED"

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
