import threading
import time
import sys
import json
import uuid
import datetime
from .scanner import scanner
from .risk_manager import risk_manager
from .kite_client import kite_client
from .config import config_manager
from .utils import DateTimeEncoder

class TradingEngine:
    def __init__(self):
        self.running = False
        self.thread = None
        self.mode = "confirm" # auto or confirm
        self.interval = 60 # seconds
        self.active_trades = {} # tradingsymbol -> { sl, target, direction }
        
    def start(self, mode: str = "confirm"):
        if self.running:
            return
            
        self.mode = mode
        self.running = True
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.daemon = True
        self.thread.start()
        self._push_state_update()
        self._push_log(f"Trading engine started in {mode} mode")
        
    def stop(self):
        self.running = False
        self._push_state_update()
        self._push_log("Trading engine stopped")
        
    def status(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode
        }
        
    def _push_state_update(self):
        event = {
            "event": "agent:state-update",
            "data": {
                "running": self.running,
                "mode": self.mode,
                "status": "scanning" if self.running else "idle"
            }
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()

    def _push_log(self, message: str, level: str = "info"):
        event = {
            "event": "log:entry",
            "data": {
                "id": str(uuid.uuid4()),
                "level": level,
                "message": message,
                "timestamp": datetime.datetime.now().isoformat()
            }
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()
        
    def _push_signal(self, signal: dict):
        event = {
            "event": "agent:signal",
            "data": signal
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()
        
    def _run_loop(self):
        last_scan_time = 0
        scan_interval = 60 # Check for new signals every 60 seconds
        monitor_interval = 5 # Check open positions every 5 seconds for rapid exits
        
        while self.running:
            try:
                # 1. Fast polling: Monitor live positions for Stop-Loss / Target
                self.monitor_positions()
                
                # 2. Check End of Day square off
                if risk_manager.should_square_off():
                    self.square_off_all()
                    self.stop()
                    break
                    
                # 3. Slow polling: Scan for new entry signals
                current_time = time.time()
                if current_time - last_scan_time >= scan_interval:
                    self.scan_and_trade()
                    last_scan_time = current_time
                    
            except Exception as e:
                self._push_log(f"Error in trading loop: {e}")
                
            # Sleep for the shorter interval (5 seconds)
            for _ in range(monitor_interval):
                if not self.running:
                    break
                time.sleep(1)
                
    def scan_and_trade(self):
        can_trade, reason = risk_manager.can_trade()
        if not can_trade:
            if not getattr(self, '_notified_cannot_trade', False):
                self._push_log(f"Agent is running in offline mode ({reason}). It will scan for opportunities but will NOT execute trades.", level="warning")
                self._notified_cannot_trade = True
        else:
            self._notified_cannot_trade = False
            
        # Use our AI/Algorithmic screener to dynamically find "In Play" stocks from NIFTY 50 + Custom Watchlist
        if not hasattr(self, 'dynamic_watchlist') or not self.dynamic_watchlist:
            from .screener import screener_engine
            from .nifty_universe import NIFTY_50
            
            custom_watchlist = config_manager.get_watchlist()
            full_universe = list(set(NIFTY_50 + custom_watchlist))
            
            self._push_log(f"Running algorithmic screener on NIFTY 50 + {len(custom_watchlist)} custom stocks...")
            self.dynamic_watchlist = screener_engine.generate_daily_watchlist(universe=full_universe, limit=12)
            self._push_log(f"Dynamic Watchlist selected: {', '.join(self.dynamic_watchlist)}")
            
        def handle_new_signal(signal):
            if signal["confidence"] >= 70:
                self._push_signal(signal)
                if self.mode == "auto" and signal["confidence"] >= 80 and can_trade:
                    # Prevent buying the same stock if we already have an active trade for it!
                    if signal["tradingsymbol"] not in self.active_trades:
                        self.execute_signal(signal)
                    else:
                        self._push_log(f"Skipping auto-trade for {signal['tradingsymbol']} as it is already an active position.")
                    
        # Scan stocks in parallel and stream signals to the UI instantly via handle_new_signal callback
        signals = scanner.scan_watchlist(self.dynamic_watchlist, on_signal=handle_new_signal)
                    
    def execute_signal(self, signal: dict):
        can_trade, reason = risk_manager.can_trade()
        if not can_trade:
            self._push_log(f"Cannot execute signal {signal['id']}: {reason}")
            return False
            
        qty = risk_manager.calculate_position_size(signal["entryPrice"], signal["stopLoss"])
        
        transaction_type = "BUY" if signal["direction"] == "BUY" else "SELL"
        
        try:
            order_id = kite_client.place_order(
                variety="regular",
                exchange=signal["exchange"],
                tradingsymbol=signal["tradingsymbol"],
                transaction_type=transaction_type,
                quantity=qty,
                product="MIS",
                order_type="LIMIT",
                price=signal["entryPrice"]
            )
            self._push_log(f"Executed {transaction_type} for {signal['tradingsymbol']}, qty {qty}, order_id {order_id}")
            self.active_trades[signal["tradingsymbol"]] = {
                "sl": signal["stopLoss"],
                "target": signal["target"],
                "direction": signal["direction"]
            }
            return True
        except Exception as e:
            self._push_log(f"Failed to execute signal: {e}")
            return False
            
    def _get_exit_limit_price(self, ltp: float, tx_type: str) -> float:
        # A pseudo-market limit order to ensure immediate fill without Kite MARKET restrictions
        buffer = 0.01 # 1% buffer
        if tx_type == "BUY":
            return round(ltp * (1 + buffer), 2)
        else:
            return round(ltp * (1 - buffer), 2)

    def monitor_positions(self):
        try:
            positions = kite_client.get_positions().get("net", [])
            open_count = sum(1 for p in positions if p["quantity"] != 0)
            risk_manager.set_open_positions(open_count)
            
            # Get symbols of currently open positions to track manual closures
            open_symbols = {p["tradingsymbol"] for p in positions if p["quantity"] != 0}
            
            # Clean up active_trades if position was closed manually via Kite App
            symbols_to_remove = []
            for symbol in self.active_trades.keys():
                if symbol not in open_symbols:
                    symbols_to_remove.append(symbol)
            for symbol in symbols_to_remove:
                self._push_log(f"Detected manual closure for {symbol}. Removing from tracking.")
                del self.active_trades[symbol]
                
            # Evaluate SL and Targets
            for p in positions:
                if p["quantity"] != 0:
                    symbol = p["tradingsymbol"]
                    if symbol in self.active_trades:
                        trade = self.active_trades[symbol]
                        ltp = p.get("lastPrice", 0)
                        if ltp == 0:
                            continue
                            
                        hit_sl = False
                        hit_target = False
                        
                        if trade["direction"] == "BUY":
                            if ltp <= trade["sl"]: hit_sl = True
                            if ltp >= trade["target"]: hit_target = True
                        else:
                            if ltp >= trade["sl"]: hit_sl = True
                            if ltp <= trade["target"]: hit_target = True
                            
                        if hit_sl or hit_target:
                            reason = "Stop Loss" if hit_sl else "Target"
                            self._push_log(f"{reason} hit for {symbol} at {ltp}. Exiting position.")
                            
                            tx_type = "SELL" if p["quantity"] > 0 else "BUY"
                            kite_client.place_order(
                                variety="regular",
                                exchange=p["exchange"],
                                tradingsymbol=symbol,
                                transaction_type=tx_type,
                                quantity=abs(p["quantity"]),
                                product=p["product"],
                                order_type="LIMIT",
                                price=self._get_exit_limit_price(ltp, tx_type)
                            )
                            # Let the manual closure cleanup handle removing it on the next loop
        except Exception as e:
            self._push_log(f"Error monitoring positions: {e}")
            
    def square_off_all(self):
        self._push_log("Squaring off all open positions")
        try:
            positions = kite_client.get_positions().get("net", [])
            for p in positions:
                if p["quantity"] != 0:
                    tx_type = "SELL" if p["quantity"] > 0 else "BUY"
                    ltp = p.get("lastPrice", 0)
                    
                    kite_client.place_order(
                        variety="regular",
                        exchange=p["exchange"],
                        tradingsymbol=p["tradingsymbol"],
                        transaction_type=tx_type,
                        quantity=abs(p["quantity"]),
                        product=p["product"],
                        order_type="LIMIT",
                        price=self._get_exit_limit_price(ltp, tx_type) if ltp > 0 else 0
                    )
                    if p["tradingsymbol"] in self.active_trades:
                        del self.active_trades[p["tradingsymbol"]]
        except Exception as e:
            self._push_log(f"Error in square off: {e}")

trading_engine = TradingEngine()
