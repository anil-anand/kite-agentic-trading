import datetime
import json
import sys
import threading
import time
import uuid

from .config import config_manager
from .kite_client import kite_client
from .risk_manager import risk_manager
from .scanner import scanner
from .utils import DateTimeEncoder


class TradingEngine:
    def __init__(self):
        self.running = False
        self.thread = None
        self.mode = "confirm"  # auto or confirm
        self.interval = 60  # seconds
        self.active_trades = {}  # tradingsymbol -> { sl, target, direction, entry_price, entry_time, original_strategy, stop_order_id, exit_pending, exit_order_id }
        self._instrument_map = {}  # cached symbol -> instrument_token map
        self._entry_fill_timeout_seconds = 15
        self._entry_fill_poll_seconds = 1

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
        return {"running": self.running, "mode": self.mode}

    def _push_state_update(self):
        event = {
            "event": "agent:state-update",
            "data": {
                "running": self.running,
                "mode": self.mode,
                "status": "scanning" if self.running else "idle",
            },
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
                "timestamp": datetime.datetime.now().isoformat(),
            },
        }
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()

    def _push_signal(self, signal: dict):
        event = {"event": "agent:signal", "data": signal}
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()

    def _run_loop(self):
        last_scan_time = 0
        scan_interval = 60  # Check for new signals every 60 seconds
        monitor_interval = 5  # Check open positions every 5 seconds for rapid exits

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

    def _ensure_instrument_map(self):
        """Cache the NSE instrument map for reuse across scan and re-evaluation."""
        if not self._instrument_map:
            instruments = kite_client.get_instruments("NSE")
            self._instrument_map = {
                i["tradingsymbol"]: i["instrument_token"] for i in instruments
            }
        return self._instrument_map

    def scan_and_trade(self):
        can_trade, reason = risk_manager.can_trade()
        if not can_trade:
            if not getattr(self, "_notified_cannot_trade", False):
                self._push_log(
                    f"Agent is running in offline mode ({reason}). It will scan for opportunities but will NOT execute trades.",
                    level="warning",
                )
                self._notified_cannot_trade = True
        else:
            self._notified_cannot_trade = False

        # Use our AI/Algorithmic screener to dynamically find "In Play" stocks from NIFTY 50 + Custom Watchlist
        if not hasattr(self, "dynamic_watchlist") or not self.dynamic_watchlist:
            from .nifty_universe import get_nifty50_universe
            from .screener import screener_engine

            custom_watchlist = config_manager.get_watchlist()
            full_universe = list(set(get_nifty50_universe() + custom_watchlist))

            self._push_log(
                f"Running algorithmic screener on NIFTY 50 + {len(custom_watchlist)} custom stocks..."
            )
            self.dynamic_watchlist = screener_engine.generate_daily_watchlist(
                universe=full_universe, limit=12
            )
            self._push_log(
                f"Dynamic Watchlist selected: {', '.join(self.dynamic_watchlist)}"
            )

        def handle_new_signal(signal):
            if signal["confidence"] >= 70:
                self._push_signal(signal)
                if self.mode == "auto" and signal["confidence"] >= 80 and can_trade:
                    # Prevent buying the same stock if we already have an active trade for it!
                    if signal["tradingsymbol"] not in self.active_trades:
                        self.execute_signal(signal)
                    else:
                        self._push_log(
                            f"Skipping auto-trade for {signal['tradingsymbol']} as it is already an active position."
                        )

        # Scan stocks in parallel and stream signals to the UI instantly via handle_new_signal callback
        scanner.scan_watchlist(self.dynamic_watchlist, on_signal=handle_new_signal)

        # Re-evaluate open positions for thesis invalidation
        if self.active_trades:
            self._reevaluate_positions()

    def execute_signal(self, signal: dict):
        can_trade, reason = risk_manager.can_trade()
        if not can_trade:
            self._push_log(f"Cannot execute signal {signal['id']}: {reason}")
            return False

        qty = risk_manager.calculate_position_size(
            signal["entryPrice"], signal["stopLoss"]
        )

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
                price=signal["entryPrice"],
            )
            self._push_log(
                f"Executed {transaction_type} for {signal['tradingsymbol']}, qty {qty}, order_id {order_id}"
            )
            position = self._wait_for_entry_fill(signal, order_id)
            if not position:
                self._push_log(
                    f"Entry order {order_id} for {signal['tradingsymbol']} not filled. Not tracking as active trade.",
                    level="warning",
                )
                try:
                    kite_client.cancel_order("regular", order_id)
                except Exception:
                    pass
                return False

            stop_order_id = self._place_protective_stop(
                signal,
                abs(position.get("quantity", qty)) or qty,
                position.get("exchange", signal["exchange"]),
                position.get("product", "MIS"),
            )
            if not stop_order_id:
                self._push_log(
                    f"Failed to place protective stop for {signal['tradingsymbol']}. Exiting position immediately.",
                    level="error",
                )
                self._exit_position(
                    position,
                    signal["tradingsymbol"],
                    "Protective stop placement failed",
                )
                return False

            self.active_trades[signal["tradingsymbol"]] = {
                "sl": signal["stopLoss"],
                "target": signal["target"],
                "direction": signal["direction"],
                "entry_price": signal["entryPrice"],
                "entry_time": datetime.datetime.now(),
                "original_strategy": signal.get("strategy", "unknown"),
                "entry_order_id": order_id,
                "stop_order_id": stop_order_id,
                "exit_pending": False,
                "exit_order_id": None,
            }
            return True
        except Exception as e:
            self._push_log(f"Failed to execute signal: {e}")
            return False

    def _get_exit_limit_price(self, ltp: float, tx_type: str) -> float:
        # A pseudo-market limit order to ensure immediate fill without Kite MARKET restrictions
        buffer = 0.01  # 1% buffer
        if tx_type == "BUY":
            return round(ltp * (1 + buffer), 2)
        else:
            return round(ltp * (1 - buffer), 2)

    def monitor_positions(self):
        try:
            positions = kite_client.get_positions().get("net", [])
            total_pnl = sum(
                p.get("realised", 0.0) + p.get("unrealised", 0.0) for p in positions
            )
            pnl_delta = total_pnl - risk_manager.daily_pnl
            if pnl_delta != 0:
                risk_manager.update_pnl(pnl_delta)
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
                self._push_log(
                    f"Detected manual closure for {symbol}. Removing from tracking."
                )
                self._cancel_protective_stop(symbol)
                del self.active_trades[symbol]

            # Evaluate SL and Targets
            for p in positions:
                if p["quantity"] != 0:
                    symbol = p["tradingsymbol"]

                    # Adopt untracked positions in auto mode
                    if symbol not in self.active_trades and self.mode == "auto":
                        avg_price = p.get("averagePrice", 0)
                        if avg_price > 0:
                            direction = "BUY" if p["quantity"] > 0 else "SELL"
                            risk_config = config_manager.get_risk_config()
                            sl_pct = risk_config.get("defaultStopLossPercent", 1.5)
                            tgt_pct = risk_config.get("defaultTargetPercent", 3.0)

                            if direction == "BUY":
                                sl = round(avg_price * (1 - sl_pct / 100), 2)
                                target = round(avg_price * (1 + tgt_pct / 100), 2)
                            else:
                                sl = round(avg_price * (1 + sl_pct / 100), 2)
                                target = round(avg_price * (1 - tgt_pct / 100), 2)

                            self.active_trades[symbol] = {
                                "sl": sl,
                                "target": target,
                                "direction": direction,
                                "entry_price": avg_price,
                                "entry_time": datetime.datetime.now(),
                                "original_strategy": "adopted",
                            }
                            self._push_log(
                                f"Adopted open position {symbol} ({direction}) at ₹{avg_price}. Auto-calculated SL: ₹{sl}, Target: ₹{target}"
                            )

                    if symbol in self.active_trades:
                        trade = self.active_trades[symbol]
                        if trade.get("exit_pending"):
                            self._sync_exit_pending_status(symbol)
                            continue
                        ltp = p.get("lastPrice", 0)
                        if ltp == 0:
                            continue

                        hit_sl = False
                        hit_target = False

                        if trade["direction"] == "BUY":
                            if ltp <= trade["sl"]:
                                hit_sl = True
                            if ltp >= trade["target"]:
                                hit_target = True
                        else:
                            if ltp >= trade["sl"]:
                                hit_sl = True
                            if ltp <= trade["target"]:
                                hit_target = True

                        if hit_sl or hit_target:
                            reason = "Stop Loss" if hit_sl else "Target"
                            self._push_log(
                                f"{reason} hit for {symbol} at {ltp}. Exiting position."
                            )
                            self._place_exit_order(p, symbol)
        except Exception as e:
            self._push_log(f"Error monitoring positions: {e}")

    def _reevaluate_positions(self):
        """Re-evaluate open positions against current strategy signals (thesis invalidation)."""
        if not self.active_trades:
            return

        risk_config = config_manager.get_risk_config()
        weak_exit_mins = risk_config.get("positionRevalWeakExitMins", 15)
        breakeven_mins = risk_config.get("positionRevalBreakevenMins", 45)
        instrument_map = self._ensure_instrument_map()
        now = datetime.datetime.now()

        # Get current positions for P&L and LTP data
        try:
            positions = kite_client.get_positions().get("net", [])
            position_map = {
                p["tradingsymbol"]: p for p in positions if p["quantity"] != 0
            }
        except Exception as e:
            self._push_log(f"Error fetching positions for re-evaluation: {e}")
            return

        # Snapshot keys to avoid modifying dict during iteration
        symbols_to_evaluate = list(self.active_trades.keys())

        for symbol in symbols_to_evaluate:
            if symbol not in self.active_trades:
                continue  # May have been removed by a prior iteration

            trade = self.active_trades[symbol]
            token = instrument_map.get(symbol)
            if not token:
                continue

            # Get current position data
            pos = position_map.get(symbol)
            if not pos:
                continue  # Position already closed

            # Evaluate current strategy signals for this symbol
            try:
                evaluation = scanner.evaluate_position(symbol, token)
            except Exception as e:
                self._push_log(f"Error evaluating {symbol}: {e}")
                continue

            direction = trade["direction"]
            entry_time = trade.get("entry_time", now)
            entry_price = trade.get("entry_price", pos.get("averagePrice", 0))
            mins_held = (now - entry_time).total_seconds() / 60
            ltp = pos.get("lastPrice", 0)

            # Determine P&L direction
            if direction == "BUY":
                in_loss = ltp < entry_price
                supporting = evaluation["buy_signals"]
                opposing = evaluation["sell_signals"]
            else:
                in_loss = ltp > entry_price
                supporting = evaluation["sell_signals"]
                opposing = evaluation["buy_signals"]

            # === Graduated Exit Rules ===

            # Rule 1: Strong opposing signal — thesis fully invalidated
            if opposing >= 2 and supporting == 0:
                reason = f"Thesis invalidated for {symbol}: {opposing} opposing signals, 0 supporting. Exiting."
                self._push_log(reason, level="warning")
                if self.mode == "auto":
                    self._exit_position(pos, symbol, reason)
                continue

            # Rule 2: Weak conviction — no support + in loss + time elapsed
            if supporting == 0 and in_loss and mins_held >= weak_exit_mins:
                reason = f"Weak conviction for {symbol}: 0 supporting signals, in loss, held {mins_held:.0f} mins. Exiting."
                self._push_log(reason, level="warning")
                if self.mode == "auto":
                    self._exit_position(pos, symbol, reason)
                continue

            # Rule 3: Time decay — tighten to breakeven
            if mins_held >= breakeven_mins:
                if entry_price > 0 and trade["sl"] != entry_price:
                    old_sl = trade["sl"]
                    self._tighten_to_breakeven(symbol)
                    self._push_log(
                        f"Time decay for {symbol}: held {mins_held:.0f} mins. SL tightened from ₹{old_sl} to breakeven ₹{entry_price}."
                    )
                continue

            # Rule 4: Thesis still valid — hold
            if supporting > 0:
                self._push_log(
                    f"Thesis valid for {symbol}: {supporting} supporting, {opposing} opposing. Holding."
                )

    def _tighten_to_breakeven(self, symbol: str):
        """Move the stop-loss to the entry price (breakeven)."""
        if symbol in self.active_trades:
            entry_price = self.active_trades[symbol].get("entry_price", 0)
            if entry_price > 0:
                self.active_trades[symbol]["sl"] = entry_price

    def _exit_position(self, position: dict, symbol: str, reason: str):
        """Exit a position due to thesis invalidation."""
        try:
            ltp = position.get("lastPrice", 0)
            if ltp == 0:
                self._push_log(f"Cannot exit {symbol}: no LTP available")
                return
            self._place_exit_order(position, symbol, reason)
        except Exception as e:
            self._push_log(f"Failed to exit {symbol}: {e}")

    def square_off_all(self):
        self._push_log("Squaring off all open positions")
        try:
            positions = kite_client.get_positions().get("net", [])
            for p in positions:
                if p["quantity"] != 0:
                    self._place_exit_order(p, p["tradingsymbol"], "Square off")
        except Exception as e:
            self._push_log(f"Error in square off: {e}")

    def _wait_for_entry_fill(self, signal: dict, order_id: str) -> dict:
        deadline = time.time() + self._entry_fill_timeout_seconds
        symbol = signal["tradingsymbol"]
        direction = signal["direction"]
        while time.time() <= deadline:
            position = self._find_live_position(symbol, direction)
            if position:
                return position

            if self._is_order_closed_without_fill(order_id):
                return {}

            time.sleep(self._entry_fill_poll_seconds)
        return {}

    def _find_live_position(self, symbol: str, direction: str) -> dict:
        positions = kite_client.get_positions().get("net", [])
        for p in positions:
            qty = p.get("quantity", 0)
            if p.get("tradingsymbol") != symbol or qty == 0:
                continue
            if direction == "BUY" and qty > 0:
                return p
            if direction == "SELL" and qty < 0:
                return p
        return {}

    def _is_order_closed_without_fill(self, order_id: str) -> bool:
        orders = kite_client.get_orders()
        for order in orders:
            if str(order.get("orderId")) != str(order_id):
                continue
            status = str(order.get("status", "")).upper()
            filled_qty = order.get("filledQuantity", 0) or 0
            if status in {"REJECTED", "CANCELLED"}:
                return True
            if status == "COMPLETE" and filled_qty <= 0:
                return True
            return False
        return False

    def _place_protective_stop(
        self, signal: dict, quantity: int, exchange: str, product: str
    ) -> str:
        try:
            direction = signal["direction"]
            stop_tx = "SELL" if direction == "BUY" else "BUY"
            trigger_price = signal["stopLoss"]
            order_id = kite_client.place_order(
                variety="regular",
                exchange=exchange,
                tradingsymbol=signal["tradingsymbol"],
                transaction_type=stop_tx,
                quantity=quantity,
                product=product,
                order_type="SL-M",
                trigger_price=trigger_price,
            )
            self._push_log(
                f"Placed protective stop for {signal['tradingsymbol']} at trigger ₹{trigger_price}, order_id {order_id}"
            )
            return order_id
        except Exception as e:
            self._push_log(
                f"Failed to place protective stop for {signal['tradingsymbol']}: {e}",
                level="error",
            )
            return ""

    def _cancel_protective_stop(self, symbol: str):
        trade = self.active_trades.get(symbol)
        if not trade:
            return
        stop_order_id = trade.get("stop_order_id")
        if not stop_order_id:
            return
        try:
            kite_client.cancel_order("regular", stop_order_id)
        except Exception:
            pass
        trade["stop_order_id"] = None

    def _place_exit_order(self, position: dict, symbol: str, reason: str = ""):
        if symbol in self.active_trades and self.active_trades[symbol].get(
            "exit_pending"
        ):
            return

        ltp = position.get("lastPrice", 0)
        tx_type = "SELL" if position["quantity"] > 0 else "BUY"
        self._cancel_protective_stop(symbol)
        order_id = kite_client.place_order(
            variety="regular",
            exchange=position["exchange"],
            tradingsymbol=symbol,
            transaction_type=tx_type,
            quantity=abs(position["quantity"]),
            product=position["product"],
            order_type="LIMIT",
            price=self._get_exit_limit_price(ltp, tx_type) if ltp > 0 else 0,
        )
        if symbol in self.active_trades:
            self.active_trades[symbol]["exit_pending"] = True
            self.active_trades[symbol]["exit_order_id"] = order_id
        reason_prefix = f"{reason}: " if reason else ""
        self._push_log(f"{reason_prefix}exit order placed for {symbol} ({tx_type})")

    def _sync_exit_pending_status(self, symbol: str):
        trade = self.active_trades.get(symbol)
        if not trade:
            return
        order_id = trade.get("exit_order_id")
        if not order_id:
            trade["exit_pending"] = False
            return
        orders = kite_client.get_orders()
        for order in orders:
            if str(order.get("orderId")) != str(order_id):
                continue
            status = str(order.get("status", "")).upper()
            if status in {"REJECTED", "CANCELLED"}:
                trade["exit_pending"] = False
                trade["exit_order_id"] = None
                self._push_log(
                    f"Exit order {order_id} for {symbol} {status.lower()}. Re-attempting on next cycle.",
                    level="warning",
                )
            break


trading_engine = TradingEngine()
