import logging
import threading
import json
from kiteconnect import KiteTicker
from .utils import DateTimeEncoder

class TickerManager:
    def __init__(self):
        self.ticker = None
        self.thread = None
        self.tokens = set()
        self.running = False
        
    def start(self, api_key: str, access_token: str):
        if self.running:
            return
            
        self.ticker = KiteTicker(api_key, access_token)
        self.ticker.on_ticks = self.on_ticks
        self.ticker.on_connect = self.on_connect
        self.ticker.on_close = self.on_close
        self.ticker.on_error = self.on_error
        self.ticker.on_reconnect = self.on_reconnect
        self.ticker.on_noreconnect = self.on_noreconnect
        self.ticker.on_order_update = self.on_order_update
        
        self.running = True
        self.thread = threading.Thread(target=self.ticker.connect, kwargs={"threaded": True})
        self.thread.daemon = True
        self.thread.start()
        
    def stop(self):
        if self.ticker and self.running:
            self.running = False
            self.ticker.close()
            
    def subscribe(self, tokens: list):
        for token in tokens:
            self.tokens.add(token)
        if self.ticker and self.running:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))
            
    def unsubscribe(self, tokens: list):
        for token in tokens:
            if token in self.tokens:
                self.tokens.remove(token)
        if self.ticker and self.running:
            self.ticker.unsubscribe(tokens)
            
    def on_ticks(self, ws, ticks):
        import json
        import sys
        
        for tick in ticks:
            # Emit tick event via stdout
            event = {
                "event": "tick",
                "data": {
                    "instrument_token": tick.get("instrument_token"),
                    "last_price": tick.get("last_price"),
                    "volume": tick.get("volume"),
                    "buy_quantity": tick.get("buy_quantity"),
                    "sell_quantity": tick.get("sell_quantity"),
                    "timestamp": str(tick.get("timestamp")) if tick.get("timestamp") else None
                }
            }
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()
            
    def on_order_update(self, ws, data):
        import json
        import sys
        from .utils import DateTimeEncoder
        
        event = {
            "event": "order_update",
            "data": data
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()
            
    def on_connect(self, ws, response):
        if self.tokens:
            self.ticker.subscribe(list(self.tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.tokens))
            
    def on_close(self, ws, code, reason):
        pass
        
    def on_error(self, ws, code, reason):
        import sys
        print(f"Ticker Error: {code} - {reason}", file=sys.stderr)
        
    def on_reconnect(self, ws, attempts_count):
        pass
        
    def on_noreconnect(self, ws):
        pass

ticker_manager = TickerManager()
