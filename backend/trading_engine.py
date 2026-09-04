import datetime
import json
import sys
import threading
import time
import uuid

from .config import config_manager
from .journal import journal
from .kite_client import kite_client
from .risk_manager import risk_manager
from .scanner import scanner
from .strategy_selector import assemble_context, select_strategies
from .utils import DateTimeEncoder

# Module-level lock for stdout to prevent interleaved JSON output between
# the main JSON-RPC thread, the daemon trading loop, and scanner pool threads.
_stdout_lock = threading.Lock()


class TradingEngine:
    def __init__(self):
        self.running = False
        self.thread = None
        self.mode = "auto"  # auto or confirm
        self.interval = 60  # seconds
        self.active_trades = {}  # tradingsymbol -> { sl, target, direction, entry_price, entry_time, original_strategy, stop_order_id, exit_pending, exit_order_id }
        self._instrument_map = {}  # cached symbol -> instrument_token map
        self._entry_fill_timeout_seconds = 15
        self._entry_fill_poll_seconds = 1

        # Reentrant lock protecting self.active_trades and self._pending_entries.
        # RLock is used because several public methods (e.g. monitor_positions)
        # call private helpers (_place_exit_order, _cancel_protective_stop) that
        # also need to hold the lock — reentrant acquisition avoids deadlocks.
        self._trade_lock = threading.RLock()

        # Symbols whose entry orders are in flight but not yet added to
        # active_trades. Prevents monitor_positions from adopting a position
        # that execute_signal is still setting up.
        self._pending_entries: set = set()
        self._tick_size_map = {}
        self._reserved_entry_margin = 0.0

        # Session state machine for AI/regime strategy selection (issue #62).
        #   warmup    -> engine just started, nothing evaluated yet
        #   observing -> inside the first-15-min observation window; signals may
        #                be generated but NO trades are placed
        #   active    -> observation done and the strategy decision applied;
        #                normal auto-execution allowed
        #   halted    -> square-off / stopped for the day
        self.session_state = "warmup"
        self.selection_record = None

    # First-15-minute observation window, anchored to the official market open
    # (naive local time, consistent with risk_manager's configured times).
    MARKET_OPEN = datetime.time(9, 15)
    OBSERVATION_MINUTES = 15

    def _get_tick_size(self, symbol: str, exchange: str = "NSE") -> float:
        if symbol in self._tick_size_map:
            return self._tick_size_map[symbol]
        try:
            instruments = kite_client.get_instruments(exchange)
            for i in instruments:
                self._tick_size_map[i["tradingsymbol"]] = float(
                    i.get("tick_size", 0.05)
                )
            return self._tick_size_map.get(symbol, 0.05)
        except Exception as e:
            self._push_log(
                f"Error fetching tick size for {symbol}: {e}", level="warning"
            )
            return 0.05

    def _round_to_tick(self, price: float, tick_size: float) -> float:
        return round(round(price / tick_size) * tick_size, 2)

    def start(self, mode: str = "auto"):
        if self.running:
            return

        self.mode = mode
        self.running = True
        self.session_state = "warmup"
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
            "mode": self.mode,
            "session_state": self.session_state,
        }

    # --- Session state machine + strategy selection (issue #62) --------------
    def _now(self) -> datetime.datetime:
        # Seam for tests.
        return datetime.datetime.now()

    def _observation_deadline(self, now: datetime.datetime) -> datetime.datetime:
        open_dt = datetime.datetime.combine(now.date(), self.MARKET_OPEN)
        return open_dt + datetime.timedelta(minutes=self.OBSERVATION_MINUTES)

    def _selection_done_today(self, now: datetime.datetime) -> bool:
        """True if a strategy decision has already been recorded for today —
        used so a mid-session restart re-hydrates rather than re-deciding."""
        record = config_manager.get_strategy_selection()
        return bool(record) and record.get("session_date") == now.date().isoformat()

    def _auto_execute_allowed(self) -> bool:
        """Trades may only be placed once the session is ACTIVE. During the
        observation window (or warmup/halted) no order is placed."""
        return self.session_state == "active"

    def update_session_state(self, now: datetime.datetime = None):
        """Advance the session state machine. Before the observation deadline we
        stay in OBSERVING (signals only, no trades). At/after the deadline we
        run the one-shot strategy selection (unless already done today) and move
        to ACTIVE. A late start finds the deadline already passed and transitions
        straight through, using whatever session data is available now."""
        if self.session_state == "halted":
            return
        now = now or self._now()

        if now < self._observation_deadline(now):
            if self.session_state != "observing":
                self.session_state = "observing"
                self._push_log(
                    "Session in observation window — collecting market context; "
                    "no trades will be placed until it closes.",
                    level="info",
                )
                self._push_state_update()
            return

        # Deadline passed.
        if self._selection_done_today(now):
            # Restart mid-session: reuse the existing decision, don't re-decide.
            if self.selection_record is None:
                self.selection_record = config_manager.get_strategy_selection()
            if self.session_state != "active":
                self.session_state = "active"
                self._push_state_update()
            return

        self.run_strategy_selection(now=now)
        self.session_state = "active"
        self._push_state_update()

    def _selection_universe(self) -> list:
        from .nifty_universe import get_nifty100_universe

        custom = config_manager.get_watchlist()
        return list(set(get_nifty100_universe() + custom))

    def run_strategy_selection(self, now: datetime.datetime = None):
        """Assemble the session context, classify the regime, and persist the
        decision as a per-session overlay. Conservative fallback: on UNKNOWN (or
        any failure) nothing is applied and the user's baseline set is left
        untouched."""
        now = now or self._now()
        baseline = config_manager.get_strategy_config()
        available_ids = [
            sid for sid, cfg in baseline.items() if cfg.get("enabled", False)
        ]
        try:
            context = assemble_context(
                kite_client.get_quote, self._selection_universe()
            )
            decision = select_strategies(context, available_ids)
        except Exception as e:
            self._push_log(
                f"Strategy selection failed ({e}); leaving baseline strategies "
                "unchanged.",
                level="warning",
            )
            decision = {
                "regime": "UNKNOWN",
                "applied": False,
                "enabled": [],
                "disabled": [],
                "rationale": f"Selection error: {e}. Baseline left unchanged.",
                "inputs_used": {},
                "inputs_missing": ["market_quotes"],
                "decided_at": now.isoformat(),
            }

        decision.setdefault("source", "deterministic")
        # Optional LLM advisory pass (Phase 2). Off unless opted in; always
        # falls back to the deterministic decision on any problem.
        decision = self._maybe_llm_advise(decision, available_ids)

        decision["session_date"] = now.date().isoformat()
        config_manager.save_strategy_selection(decision)
        self.selection_record = decision
        self._push_log(decision["rationale"], level="info")
        self._push_strategy_selection(decision)
        return decision

    def _maybe_llm_advise(self, decision: dict, available_ids: list) -> dict:
        """Run the opt-in LLM advisory layer over a deterministic decision. Any
        failure (disabled, no key, API/parse error) returns the input decision
        unchanged — the advisory layer can never make the selection worse."""
        advisor_cfg = config_manager.config.get("aiStrategyAdvisor", {})
        if not advisor_cfg.get("enabled", False):
            return decision
        if not decision.get("applied"):
            return decision

        try:
            from .analytics import analytics
            from .llm_client import OpenAICompatibleClient
            from .strategy_advisor import advise as llm_advise

            creds = config_manager.get_credentials()
            llm = config_manager.get_llm_settings()
            api_key = creds.get("llmApiKey")
            provider = llm.get("provider", "Gemini")
            if not api_key and provider != "Ollama":
                self._push_log(
                    "AI strategy advisor is on but no LLM API key is configured; "
                    "using the deterministic decision.",
                    level="warning",
                )
                return decision

            try:
                expectancy = analytics.get_strategy_expectancy()
            except Exception:
                expectancy = []

            def generate_fn(prompt: str) -> str:
                return OpenAICompatibleClient().generate(
                    base_url=llm.get("baseUrl", ""),
                    api_key=api_key,
                    model=llm.get("model", ""),
                    prompt=prompt,
                    provider=provider,
                    plan=llm.get("openCodePlan", "zen"),
                )

            result = llm_advise(
                context=decision.get("inputs_used", {}),
                deterministic=decision,
                available_ids=available_ids,
                generate_fn=generate_fn,
                expectancy=expectancy,
            )
            if result.get("llm_error"):
                self._push_log(
                    f"AI strategy advisor failed ({result['llm_error']}); using "
                    "the deterministic decision.",
                    level="warning",
                )
            return result
        except Exception as e:
            self._push_log(
                f"AI strategy advisor error ({e}); using the deterministic decision.",
                level="warning",
            )
            return decision

    def reevaluate_strategies(self):
        """Force a fresh strategy decision on demand (manual re-evaluate)."""
        record = self.run_strategy_selection()
        if self.session_state == "observing":
            self.session_state = "active"
            self._push_state_update()
        return record

    def set_strategy_override(self, enabled_ids: list):
        """Apply a manual per-session override of the enabled strategy set. Kept
        as an overlay over the user's baseline — it never mutates the baseline
        config. Only ids the user already permits take effect."""
        baseline = config_manager.get_strategy_config()
        permitted = {sid for sid, cfg in baseline.items() if cfg.get("enabled", False)}
        enabled = [sid for sid in enabled_ids if sid in permitted]
        now = self._now()
        record = {
            "regime": (self.selection_record or {}).get("regime", "MANUAL"),
            "applied": True,
            "enabled": enabled,
            "disabled": [sid for sid in permitted if sid not in set(enabled)],
            "rationale": "Manual override of the session strategy set by the user.",
            "inputs_used": {},
            "inputs_missing": [],
            "decided_at": now.isoformat(),
            "session_date": now.date().isoformat(),
            "source": "manual",
        }
        config_manager.save_strategy_selection(record)
        self.selection_record = record
        self._push_strategy_selection(record)
        return record

    def _push_strategy_selection(self, record: dict):
        event = {"event": "agent:strategy-selection", "data": record}
        with _stdout_lock:
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()

    def _push_state_update(self):
        event = {
            "event": "agent:state-update",
            "data": {
                "running": self.running,
                "mode": self.mode,
                "status": "scanning" if self.running else "idle",
                "session_state": self.session_state,
            },
        }
        with _stdout_lock:
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
        with _stdout_lock:
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()

    def _push_signal(self, signal: dict):
        event = {"event": "agent:signal", "data": signal}
        with _stdout_lock:
            print(json.dumps(event, cls=DateTimeEncoder))
            sys.stdout.flush()

    def _run_loop(self):
        last_scan_time = 0
        scan_interval = 30  # Check for new signals every 30 seconds
        monitor_interval = 5  # Check open positions every 5 seconds for rapid exits

        while self.running:
            try:
                # 0. Advance the session state machine (observation -> active +
                #    one-shot strategy selection). Failure here must not stop the
                #    loop, so it's inside the same try.
                self.update_session_state()

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

        # Use our AI/Algorithmic screener to dynamically find "In Play" stocks from NIFTY 100 + Custom Watchlist
        if not hasattr(self, "dynamic_watchlist") or not self.dynamic_watchlist:
            from .nifty_universe import get_nifty100_universe
            from .screener import screener_engine

            custom_watchlist = config_manager.get_watchlist()
            full_universe = list(set(get_nifty100_universe() + custom_watchlist))

            self._push_log(
                f"Running algorithmic screener on NIFTY 100 + {len(custom_watchlist)} custom stocks..."
            )
            self.dynamic_watchlist = screener_engine.generate_daily_watchlist(
                universe=full_universe, limit=12
            )
            self._push_log(
                f"Dynamic Watchlist selected: {', '.join(self.dynamic_watchlist)}"
            )

        def handle_new_signal(signal):
            # NOTE: This callback is invoked from scanner ThreadPoolExecutor
            # threads, so active_trades access must be guarded by the lock.
            if signal["confidence"] >= 70:
                self._push_signal(signal)
                if (
                    self.mode == "auto"
                    and signal["confidence"] >= 80
                    and can_trade
                    and self._auto_execute_allowed()
                ):
                    symbol = signal["tradingsymbol"]
                    with self._trade_lock:
                        already_active = (
                            symbol in self.active_trades
                            or symbol in self._pending_entries
                        )
                    if already_active:
                        self._push_log(
                            f"Skipping auto-trade for {symbol} as it is already an active or pending position."
                        )
                    else:
                        self.execute_signal(signal)

        # Scan stocks in parallel and stream signals to the UI instantly via handle_new_signal callback
        scanner.scan_watchlist(self.dynamic_watchlist, on_signal=handle_new_signal)

        # Re-evaluate open positions for thesis invalidation
        with self._trade_lock:
            has_trades = bool(self.active_trades)
        if has_trades:
            self._reevaluate_positions()

    def execute_signal(self, signal: dict):
        symbol = signal["tradingsymbol"]

        # Atomically guard against duplicate entry orders for the same symbol.
        with self._trade_lock:
            if symbol in self.active_trades or symbol in self._pending_entries:
                self._push_log(
                    f"Skipping execution for {symbol}: already active or pending."
                )
                return False
            self._pending_entries.add(symbol)

        try:
            return self._execute_signal_inner(signal)
        finally:
            with self._trade_lock:
                self._pending_entries.discard(symbol)

    def _execute_signal_inner(self, signal: dict):
        """Core execution logic. Called with the symbol reserved in _pending_entries."""
        symbol = signal["tradingsymbol"]

        can_trade, reason = risk_manager.can_trade()
        if not can_trade:
            self._push_log(f"Cannot execute signal {signal['id']}: {reason}")
            return False

        transaction_type = "BUY" if signal["direction"] == "BUY" else "SELL"
        exchange = signal.get("exchange", "NSE")
        tick_size = self._get_tick_size(symbol, exchange)
        entry_price = self._round_to_tick(signal["entryPrice"], tick_size)

        try:
            baseline_position = self._find_live_position(symbol, signal["direction"])
            baseline_quantity = abs(baseline_position.get("quantity", 0))
            # Serialize the margin snapshot, sizing, and submission so concurrent
            # scanner callbacks cannot reserve the same available margin.
            with self._trade_lock:
                margins = kite_client.get_margins()
                equity_margin = margins.get("equity", {})
                available = equity_margin.get("available", {})
                if "live_balance" in available:
                    available_margin = available["live_balance"]
                else:
                    available_margin = equity_margin.get("net", 0)
                available_margin = max(
                    0, available_margin - self._reserved_entry_margin
                )

                qty = risk_manager.calculate_position_size(
                    entry_price, signal["stopLoss"], available_margin
                )
                if qty <= 0:
                    self._push_log(
                        f"Cannot execute signal {signal['id']}: insufficient available margin",
                        level="warning",
                    )
                    return False

                reserved_margin = qty * entry_price
                self._reserved_entry_margin += reserved_margin
                order_id = kite_client.place_order(
                    variety="regular",
                    exchange=exchange,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=qty,
                    product="MIS",
                    order_type="LIMIT",
                    price=entry_price,
                )
            self._push_log(
                f"Executed {transaction_type} for {symbol}, qty {qty}, order_id {order_id}"
            )

            # Wait for fill — NO LOCK held; this blocks up to 15 seconds.
            position = self._wait_for_entry_fill(signal, order_id, baseline_quantity)
            if not position:
                with self._trade_lock:
                    self._reserved_entry_margin -= reserved_margin
                    reserved_margin = 0
                self._push_log(
                    f"Entry order {order_id} for {symbol} not filled. Not tracking as active trade.",
                    level="warning",
                )
                try:
                    kite_client.cancel_order("regular", order_id)
                except Exception:
                    pass
                return False

            with self._trade_lock:
                self._reserved_entry_margin -= reserved_margin
                reserved_margin = 0

            stop_order_id = self._place_protective_stop(
                signal,
                abs(position.get("quantity", qty)) or qty,
                position.get("exchange", signal["exchange"]),
                position.get("product", "MIS"),
            )
            if not stop_order_id:
                self._push_log(
                    f"Failed to place protective stop for {symbol}. Exiting position immediately.",
                    level="error",
                )
                self._exit_position(
                    position,
                    symbol,
                    "Protective stop placement failed",
                )
                return False

            # Get confluence snapshot for journal
            try:
                token = self._ensure_instrument_map().get(symbol)
                evaluation = scanner.evaluate_position(symbol, token) if token else {}
            except Exception as e:
                self._push_log(
                    f"Error fetching confluence snapshot for {symbol}: {e}",
                    level="warning",
                )
                evaluation = {}

            trade_id = str(uuid.uuid4())
            try:
                journal.open_trade(
                    trade_id=trade_id,
                    tradingsymbol=symbol,
                    exchange=position.get("exchange", exchange),
                    direction=signal["direction"],
                    product=position.get("product", "MIS"),
                    strategy=signal.get("strategy", "unknown"),
                    entry_price=position.get("averagePrice", signal["entryPrice"]),
                    quantity=abs(position.get("quantity", qty)) or qty,
                    stop_loss=signal["stopLoss"],
                    target=signal["target"],
                    signal_id=signal.get("id"),
                    reasoning=signal.get("reasoning"),
                    confidence=signal.get("confidence"),
                    confluence_snapshot=evaluation,
                    indicator_snapshot=signal.get("indicators"),
                )
            except Exception as e:
                self._push_log(
                    f"Failed to log trade open for {symbol}: {e}", level="error"
                )

            with self._trade_lock:
                self.active_trades[symbol] = {
                    "trade_id": trade_id,
                    "sl": signal["stopLoss"],
                    "target": signal["target"],
                    "direction": signal["direction"],
                    "entry_price": position.get("averagePrice", signal["entryPrice"]),
                    "entry_time": datetime.datetime.now(),
                    "original_strategy": signal.get("strategy", "unknown"),
                    "entry_order_id": order_id,
                    "stop_order_id": stop_order_id,
                    "exit_pending": False,
                    "exit_order_id": None,
                    "exchange": position.get("exchange", exchange),
                }
            return True
        except Exception as e:
            with self._trade_lock:
                self._reserved_entry_margin = max(
                    0, self._reserved_entry_margin - locals().get("reserved_margin", 0)
                )
            self._push_log(f"Failed to execute signal: {e}")
            return False

    def _get_exit_limit_price(
        self, symbol: str, exchange: str, ltp: float, tx_type: str
    ) -> float:
        # A pseudo-market limit order to ensure immediate fill without Kite MARKET restrictions
        buffer = 0.01  # 1% buffer
        tick_size = self._get_tick_size(symbol, exchange)
        if tx_type == "BUY":
            price = ltp * (1 + buffer)
        else:
            price = ltp * (1 - buffer)
        return self._round_to_tick(price, tick_size)

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

            # Snapshot in-memory state under a short lock. All Kite calls below
            # run WITHOUT the lock held, so a slow broker API can't freeze the
            # JSON-RPC thread or the scanner callback (both need this lock).
            with self._trade_lock:
                symbols_to_remove = [
                    s for s in self.active_trades if s not in open_symbols
                ]
                tracked = set(self.active_trades.keys())
                pending = set(self._pending_entries)

            # Manual closures: the position is flat but we still track it — it was
            # closed outside the app-side exit path, most often by the broker-side
            # protective stop filling (also: a manual close in the Kite app). Book
            # the close in the journal so the trade doesn't stay OPEN forever and
            # is included in analytics, then cancel any leftover stop and drop it.
            for symbol in symbols_to_remove:
                self._push_log(
                    f"Detected external closure for {symbol}. Removing from tracking."
                )
                self._journal_external_close(symbol)
                self._cancel_protective_stop(symbol)
                with self._trade_lock:
                    self.active_trades.pop(symbol, None)

            # Evaluate each open position. Trade state is re-read under a short
            # lock immediately before each decision.
            for p in positions:
                if p["quantity"] == 0:
                    continue
                symbol = p["tradingsymbol"]

                if (
                    symbol not in tracked
                    and symbol not in pending
                    and self.mode == "auto"
                ):
                    self._adopt_position(p)
                    continue

                with self._trade_lock:
                    trade = self.active_trades.get(symbol)
                    if not trade:
                        continue
                    exit_pending = trade.get("exit_pending")
                    direction = trade["direction"]
                    sl = trade["sl"]
                    target = trade["target"]

                if exit_pending:
                    self._sync_exit_pending_status(symbol)
                    continue

                ltp = p.get("lastPrice", 0)
                if ltp == 0:
                    continue

                if direction == "BUY":
                    hit_sl = ltp <= sl
                    hit_target = ltp >= target
                else:
                    hit_sl = ltp >= sl
                    hit_target = ltp <= target

                if hit_sl or hit_target:
                    reason = "Stop Loss" if hit_sl else "Target"
                    self._push_log(
                        f"{reason} hit for {symbol} at {ltp}. Exiting position."
                    )
                    # Re-read the live position right before exiting. If the
                    # broker's protective stop already closed it, our snapshot is
                    # stale — placing an exit now would sell a flat position into
                    # a new (opposite) position. Skip and clean up in that case;
                    # otherwise exit against the actual live position.
                    live = self._find_live_position_by_symbol(symbol)
                    if not live:
                        self._push_log(
                            f"{symbol} already flat before exit (broker stop likely filled). Cleaning up.",
                            level="warning",
                        )
                        self._journal_external_close(symbol)
                        self._cancel_protective_stop(symbol)
                        with self._trade_lock:
                            self.active_trades.pop(symbol, None)
                        continue
                    self._place_exit_order(live, symbol, reason)
        except Exception as e:
            self._push_log(f"Error monitoring positions: {e}")

    def _find_live_position_by_symbol(self, symbol: str) -> dict:
        """Fetch the current open position for a symbol, or {} if flat/closed."""
        try:
            positions = kite_client.get_positions().get("net", [])
        except Exception:
            return {}
        for p in positions:
            if p.get("tradingsymbol") == symbol and p.get("quantity", 0) != 0:
                return p
        return {}

    def _adopt_position(self, p: dict):
        """Adopt an untracked open position (auto mode) with a protective stop.

        Kite calls run without the trade lock held; the trade is registered under
        a short lock, and a lost race (another path started tracking the symbol)
        cancels the just-placed stop to avoid an orphaned broker order.
        """
        symbol = p["tradingsymbol"]
        avg_price = p.get("averagePrice", 0)
        if avg_price <= 0:
            return

        direction = "BUY" if p["quantity"] > 0 else "SELL"
        risk_config = config_manager.get_risk_config()
        sl_pct = risk_config.get("defaultStopLossPercent", 1.5)
        tgt_pct = risk_config.get("defaultTargetPercent", 3.0)
        exchange = p.get("exchange", "NSE")
        tick_size = self._get_tick_size(symbol, exchange)

        if direction == "BUY":
            sl = avg_price * (1 - sl_pct / 100)
            target = avg_price * (1 + tgt_pct / 100)
        else:
            sl = avg_price * (1 + sl_pct / 100)
            target = avg_price * (1 - tgt_pct / 100)

        sl = self._round_to_tick(sl, tick_size)
        target = self._round_to_tick(target, tick_size)

        adopted_signal = {
            "tradingsymbol": symbol,
            "direction": direction,
            "stopLoss": sl,
        }
        stop_order_id = self._place_protective_stop(
            adopted_signal, abs(p["quantity"]), exchange, p.get("product", "MIS")
        )

        trade_id = str(uuid.uuid4())
        try:
            journal.open_trade(
                trade_id=trade_id,
                tradingsymbol=symbol,
                exchange=exchange,
                direction=direction,
                product=p.get("product", "MIS"),
                strategy="manual",
                entry_price=avg_price,
                quantity=abs(p["quantity"]),
                stop_loss=sl,
                target=target,
            )
        except Exception as e:
            self._push_log(
                f"Failed to log adopted trade for {symbol}: {e}", level="error"
            )

        with self._trade_lock:
            lost_race = symbol in self.active_trades or symbol in self._pending_entries
            if not lost_race:
                self.active_trades[symbol] = {
                    "trade_id": trade_id,
                    "sl": sl,
                    "target": target,
                    "direction": direction,
                    "entry_price": avg_price,
                    "entry_time": datetime.datetime.now(),
                    "original_strategy": "manual",
                    "stop_order_id": stop_order_id,
                    "exit_pending": False,
                    "exit_order_id": None,
                    "exchange": exchange,
                }

        if lost_race:
            if stop_order_id:
                try:
                    kite_client.cancel_order("regular", stop_order_id)
                except Exception:
                    pass
            return

        self._push_log(
            f"Adopted open position {symbol} ({direction}) at ₹{avg_price}. Auto-calculated SL: ₹{sl}, Target: ₹{target}"
        )

    def _reevaluate_positions(self):
        """Re-evaluate open positions against current strategy signals (thesis invalidation)."""
        with self._trade_lock:
            if not self.active_trades:
                return
            symbols_to_evaluate = list(self.active_trades.keys())

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

        for symbol in symbols_to_evaluate:
            # Read trade data under lock (snapshot into locals)
            with self._trade_lock:
                if symbol not in self.active_trades:
                    continue  # May have been removed by a prior iteration
                trade = self.active_trades[symbol]
                direction = trade["direction"]
                entry_time = trade.get("entry_time", now)
                entry_price = trade.get("entry_price", 0)
                current_sl = trade["sl"]

            token = instrument_map.get(symbol)
            if not token:
                continue

            # Get current position data
            pos = position_map.get(symbol)
            if not pos:
                continue  # Position already closed

            if entry_price == 0:
                entry_price = pos.get("averagePrice", 0)

            # Evaluate current strategy signals for this symbol.
            # This is network I/O — intentionally NOT under the lock.
            try:
                evaluation = scanner.evaluate_position(symbol, token)
            except Exception as e:
                self._push_log(f"Error evaluating {symbol}: {e}")
                continue

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
                if entry_price > 0 and current_sl != entry_price:
                    old_sl = current_sl
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

            with self._trade_lock:
                trade = self.active_trades.get(symbol)
                trade_id = trade.get("trade_id") if trade else None

            if trade_id:
                try:
                    journal.log_event(
                        trade_id,
                        "thesis_reevaluation",
                        {
                            "supporting": supporting,
                            "opposing": opposing,
                            "ltp": ltp,
                            "mins_held": mins_held,
                        },
                    )
                except Exception:
                    pass

    def _tighten_to_breakeven(self, symbol: str):
        """Move the stop-loss to the entry price (breakeven), both in-memory and broker-side."""
        with self._trade_lock:
            if symbol not in self.active_trades:
                return
            trade = self.active_trades[symbol]
            entry_price = trade.get("entry_price", 0)
            if entry_price <= 0:
                return
            trade["sl"] = entry_price
            stop_order_id = trade.get("stop_order_id")
            exchange = trade.get("exchange", "NSE")
            direction = trade.get("direction")
        if stop_order_id:
            tick_size = self._get_tick_size(symbol, exchange)
            trigger_price = self._round_to_tick(entry_price, tick_size)
            stop_tx = "SELL" if direction == "BUY" else "BUY"
            buffer_pct = 0.01

            if stop_tx == "SELL":
                limit_price = trigger_price * (1 - buffer_pct)
            else:
                limit_price = trigger_price * (1 + buffer_pct)

            limit_price = self._round_to_tick(limit_price, tick_size)

            try:
                kite_client.modify_order(
                    variety="regular",
                    order_id=stop_order_id,
                    trigger_price=trigger_price,
                    price=limit_price,
                )
                with self._trade_lock:
                    trade = self.active_trades.get(symbol, {})
                    trade_id = trade.get("trade_id")

                if trade_id:
                    try:
                        journal.log_event(
                            trade_id,
                            "stop_modified",
                            {
                                "trigger_price": trigger_price,
                                "limit_price": limit_price,
                                "reason": "breakeven",
                            },
                        )
                    except Exception:
                        pass
            except Exception as e:
                self._push_log(
                    f"Failed to modify broker-side stop for {symbol} to breakeven ₹{entry_price}: {e}",
                    level="error",
                )

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
        self.session_state = "halted"
        try:
            positions = kite_client.get_positions().get("net", [])
            for p in positions:
                if p["quantity"] != 0:
                    self._place_exit_order(p, p["tradingsymbol"], "Square off")
        except Exception as e:
            self._push_log(f"Error in square off: {e}")

    def _wait_for_entry_fill(
        self, signal: dict, order_id: str, baseline_quantity: int = 0
    ) -> dict:
        deadline = time.time() + self._entry_fill_timeout_seconds
        symbol = signal["tradingsymbol"]
        direction = signal["direction"]
        while time.time() <= deadline:
            position = self._find_live_position(symbol, direction)
            filled_quantity = abs(position.get("quantity", 0)) if position else 0
            order = self._find_order(order_id)
            if position and (
                not order
                or filled_quantity > baseline_quantity
                and str(order.get("status", "")).upper()
                in {
                    "COMPLETE",
                    "CANCELLED",
                }
            ):
                return position

            if order and str(order.get("status", "")).upper() in {
                "REJECTED",
                "CANCELLED",
            }:
                return {}

            time.sleep(self._entry_fill_poll_seconds)

        try:
            kite_client.cancel_order("regular", order_id)
        except Exception:
            pass
        position = self._find_live_position(symbol, direction)
        if abs(position.get("quantity", 0)) > baseline_quantity:
            return position
        return {}

    def _find_order(self, order_id: str) -> dict:
        for order in kite_client.get_orders():
            if str(order.get("orderId")) == str(order_id):
                return order
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
            symbol = signal["tradingsymbol"]
            direction = signal["direction"]
            stop_tx = "SELL" if direction == "BUY" else "BUY"
            tick_size = self._get_tick_size(symbol, exchange)
            trigger_price = self._round_to_tick(signal["stopLoss"], tick_size)

            buffer_pct = 0.01
            if stop_tx == "SELL":
                limit_price = trigger_price * (1 - buffer_pct)
            else:
                limit_price = trigger_price * (1 + buffer_pct)
            limit_price = self._round_to_tick(limit_price, tick_size)

            order_id = kite_client.place_order(
                variety="regular",
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=stop_tx,
                quantity=quantity,
                product=product,
                order_type="SL",
                price=limit_price,
                trigger_price=trigger_price,
            )
            self._push_log(
                f"Placed protective stop for {symbol} at trigger ₹{trigger_price}, limit ₹{limit_price}, order_id {order_id}"
            )
            with self._trade_lock:
                trade = self.active_trades.get(symbol, {})
                trade_id = trade.get("trade_id")

            if trade_id:
                try:
                    journal.log_event(
                        trade_id,
                        "stop_placed",
                        {
                            "trigger_price": trigger_price,
                            "limit_price": limit_price,
                            "order_id": order_id,
                        },
                    )
                except Exception:
                    pass
            return order_id
        except Exception as e:
            self._push_log(
                f"Failed to place protective stop for {signal.get('tradingsymbol', 'unknown')}: {e}",
                level="error",
            )
            return ""

    def _cancel_protective_stop(self, symbol: str):
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if not trade:
                return
            stop_order_id = trade.get("stop_order_id")
            if not stop_order_id:
                return
            trade["stop_order_id"] = None
        try:
            kite_client.cancel_order("regular", stop_order_id)
        except Exception:
            pass

    def _place_exit_order(self, position: dict, symbol: str, reason: str = ""):
        # Atomically check and set exit_pending to prevent duplicate exit orders.
        with self._trade_lock:
            if symbol in self.active_trades and self.active_trades[symbol].get(
                "exit_pending"
            ):
                return
            # Set exit_pending BEFORE placing the order so concurrent callers
            # see it and bail out, even if the place_order call hasn't returned.
            if symbol in self.active_trades:
                self.active_trades[symbol]["exit_pending"] = True

        self._cancel_protective_stop(symbol)

        ltp = position.get("lastPrice", 0)
        tx_type = "SELL" if position["quantity"] > 0 else "BUY"

        if ltp > 0:
            order_type = "LIMIT"
            exchange = position.get("exchange", "NSE")
            price = self._get_exit_limit_price(symbol, exchange, ltp, tx_type)
        else:
            order_type = "MARKET"
            price = None

        order_kwargs = {
            "variety": "regular",
            "exchange": position["exchange"],
            "tradingsymbol": symbol,
            "transaction_type": tx_type,
            "quantity": abs(position["quantity"]),
            "product": position["product"],
            "order_type": order_type,
        }
        if price is not None:
            order_kwargs["price"] = price

        try:
            order_id = kite_client.place_order(**order_kwargs)
        except Exception as e:
            if order_type == "LIMIT":
                self._push_log(
                    f"Failed to place LIMIT exit order for {symbol}: {e}. Retrying with MARKET order.",
                    level="warning",
                )
                order_kwargs["order_type"] = "MARKET"
                if "price" in order_kwargs:
                    del order_kwargs["price"]
                try:
                    order_id = kite_client.place_order(**order_kwargs)
                except Exception as e2:
                    with self._trade_lock:
                        if symbol in self.active_trades:
                            self.active_trades[symbol]["exit_pending"] = False
                    raise e2
            else:
                with self._trade_lock:
                    if symbol in self.active_trades:
                        self.active_trades[symbol]["exit_pending"] = False
                raise

        with self._trade_lock:
            if symbol in self.active_trades:
                self.active_trades[symbol]["exit_order_id"] = order_id
                self.active_trades[symbol]["exit_reason"] = reason
        reason_prefix = f"{reason}: " if reason else ""
        self._push_log(f"{reason_prefix}exit order placed for {symbol} ({tx_type})")

    def _sync_exit_pending_status(self, symbol: str):
        with self._trade_lock:
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
            with self._trade_lock:
                if symbol not in self.active_trades:
                    break
                if status in {"REJECTED", "CANCELLED"}:
                    self.active_trades[symbol]["exit_pending"] = False
                    self.active_trades[symbol]["exit_order_id"] = None
                    self._push_log(
                        f"Exit order {order_id} for {symbol} {status.lower()}. Re-attempting on next cycle.",
                        level="warning",
                    )
                elif status == "COMPLETE":
                    trade_id = self.active_trades[symbol].get("trade_id")
                    exit_reason = self.active_trades[symbol].get(
                        "exit_reason", "unknown"
                    )
                    avg_price = float(order.get("averagePrice") or 0)

                    if trade_id:
                        try:
                            journal.close_trade(trade_id, avg_price, exit_reason)
                        except Exception as e:
                            self._push_log(
                                f"Error closing trade in journal for {symbol}: {e}",
                                level="error",
                            )

                    self._push_log(
                        f"Exit order {order_id} for {symbol} filled. Removing from tracking."
                    )
                    del self.active_trades[symbol]
            break

    def _journal_external_close(self, symbol: str):
        """Book a journal close for a position closed outside the app-side exit
        path (broker-stop fill or a manual close in Kite). Uses the current LTP
        as the exit price and infers the reason from the protective stop's
        status."""
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if not trade:
                return
            trade_id = trade.get("trade_id")
            stop_order_id = trade.get("stop_order_id")
            exchange = trade.get("exchange", "NSE")
        if not trade_id:
            return

        # Exit price: best available post-hoc estimate is the current LTP.
        exit_price = 0.0
        try:
            data = kite_client.get_ltp([f"{exchange}:{symbol}"])
            exit_price = (data.get(f"{exchange}:{symbol}") or {}).get("last_price", 0.0)
        except Exception:
            pass

        # If the protective stop shows COMPLETE, the stop closed it.
        reason = "closed_externally"
        if stop_order_id:
            try:
                for o in kite_client.get_orders():
                    if str(o.get("orderId")) == str(stop_order_id):
                        if str(o.get("status", "")).upper() == "COMPLETE":
                            reason = "stop_loss"
                        break
            except Exception:
                pass

        try:
            journal.close_trade(trade_id, exit_price, reason)
        except Exception as e:
            self._push_log(
                f"Error closing trade in journal for {symbol}: {e}", level="error"
            )


trading_engine = TradingEngine()
