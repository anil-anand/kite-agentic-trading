import datetime
import json
import math
import sys
import threading
import time
import uuid
from dataclasses import replace
from typing import Optional

from .accounting import accounting_service
from .broker_models import (
    OrderRole,
    OrderSubmissionUnknown,
    fill_to_backend_dict,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
    order_to_backend_dict,
    position_to_backend_dict,
)
from .config import config_manager
from .execution_gateway import execution_gateway
from .journal import journal
from .kite_client import kite_client
from .risk_manager import risk_manager
from .scanner import scanner
from .time_utils import EXCHANGE_TIMEZONE, as_utc, now_utc
from .utils import DateTimeEncoder
from .utils import stdout_lock as _stdout_lock

# _stdout_lock is the shared lock from utils so ALL modules (execution_gateway,
# request_policy, trading_engine) serialize their stdout writes through the same
# object, preventing interleaved JSON output.


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
        self._entry_recovery: dict[str, str] = {}

        # Reentrant lock protecting self.active_trades and self._pending_entries.
        # RLock is used because several public methods (e.g. monitor_positions)
        # call private helpers (_place_exit_order, _cancel_protective_stop) that
        # also need to hold the lock — reentrant acquisition avoids deadlocks.
        self._trade_lock = threading.RLock()

        # Symbols whose entry orders are in flight but not yet added to
        # active_trades. Prevents monitor_positions from adopting a position
        # that execute_signal is still setting up.
        # This is kept in TradingEngine because monitor_positions needs it to avoid
        # premature adoption, while execution_gateway handles duplicate *orders*.
        self._pending_entries: set = set()
        self._tick_size_map = {}
        self._reserved_entry_margin = 0.0
        self._reconciliation_pending = False

        # Dynamic Watchlist State
        self.dynamic_watchlist = []
        self.universe_version = 0
        self.last_universe_refresh_time = None
        self.watchlist_rankings = {}

        # Issue 3 — grace-period tracking for external closure detection.
        # Maps tradingsymbol -> first time it was found missing from open_symbols.
        # A position is only treated as externally closed if it is still absent
        # after _external_close_grace_seconds, preventing false drops due to
        # transient Kite API position-data lag.
        self._external_close_candidates: dict = {}
        self._external_close_grace_seconds: float = 10.0

    # Kite order statuses that mean an order is still live (protecting / working).
    _TERMINAL_ORDER_STATUSES = {
        "COMPLETE",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "REJECTED AMO",
    }

    def _position_snapshot(self):
        if hasattr(kite_client, "get_positions_snapshot"):
            return kite_client.get_positions_snapshot()

        # Test/legacy adapters are normalized at this one compatibility edge.
        payload = kite_client.get_positions()
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("day", payload.get("net", []))
            for group in ("net", "day"):
                normalized = []
                for raw in payload.get(group, []):
                    record = dict(raw)
                    record.setdefault("exchange", "NSE")
                    record.setdefault("product", "MIS")
                    normalized.append(record)
                payload[group] = normalized
        return normalize_positions_response(payload)

    def _order_snapshot(self):
        if hasattr(kite_client, "get_current_orders_snapshot"):
            return kite_client.get_current_orders_snapshot()
        return normalize_orders_response(kite_client.get_orders())

    def _fill_snapshot(self):
        if hasattr(kite_client, "get_fills_snapshot"):
            return kite_client.get_fills_snapshot()
        return normalize_fills_response(kite_client.get_trades())

    def _broker_snapshot(self):
        return kite_client.get_broker_snapshot()

    def _positions(self) -> list:
        snapshot = self._position_snapshot().require_complete()
        return [position_to_backend_dict(position) for position in snapshot.net]

    def _day_positions(self) -> list:
        snapshot = self._position_snapshot().require_complete()
        return [position_to_backend_dict(position) for position in snapshot.day]

    def _orders(self) -> list:
        snapshot = self._order_snapshot().require_complete()
        return [order_to_backend_dict(order) for order in snapshot.orders]

    def _fills(self) -> list:
        snapshot = self._fill_snapshot().require_complete()
        return [fill_to_backend_dict(fill) for fill in snapshot.fills]

    def _persist_trades(self):
        """Snapshot active_trades to disk so it survives a crash/restart."""
        try:
            with self._trade_lock:
                snapshot = {s: dict(t) for s, t in self.active_trades.items()}
            config_manager.save_active_trades(snapshot)
        except Exception as e:
            self._push_log(f"Failed to persist active trades: {e}", level="warning")

    def _persist_execution_linkage(self, trade: dict) -> bool:
        """Persist current and predecessor IDs, retaining a retry obligation."""
        trade_id = trade.get("trade_id")
        if not trade_id or trade.get("journal_entry_recorded") is False:
            return True
        try:
            existing = journal.get_trade(trade_id)
            proofs = {}
            if existing:
                for field in ("stop_order_id", "exit_order_id"):
                    old, new = existing.get(field), trade.get(field)
                    if old and new and str(old) != str(new):
                        proof = self._find_order(str(old))
                        if proof:
                            proofs[field] = proof
            journal.update_execution_linkage(
                trade_id,
                entry_order_id=trade.get("entry_order_id"),
                stop_order_id=trade.get("stop_order_id"),
                exit_order_id=trade.get("exit_order_id"),
                verified_predecessors=proofs,
            )
            row = journal.get_trade(trade_id)
            with self._trade_lock:
                for current in (trade, *self.active_trades.values()):
                    if current.get("trade_id") == trade_id:
                        current["execution_linkage_history"] = row.get(
                            "execution_linkage_history"
                        )
                        current.pop("execution_linkage_pending", None)
            return True
        except Exception as exc:
            with self._trade_lock:
                for current in (trade, *self.active_trades.values()):
                    if current.get("trade_id") == trade_id:
                        current["execution_linkage_pending"] = True
                        current["broker_reconciliation_pending"] = True
            self._push_log(
                f"Failed to persist execution linkage for {trade_id}: {exc}",
                level="warning",
            )
            return False

    def _open_order_ids(self) -> Optional[set]:
        """Set of order ids that are currently live at the broker, or None if unknown."""
        try:
            orders = self._orders()
        except Exception:
            return None
        return {
            str(o.get("order_id"))
            for o in orders
            if str(o.get("status", "")).upper() not in self._TERMINAL_ORDER_STATUSES
        }

    def reconcile_active_trades(self):
        """Reconcile persisted active_trades against live broker state on startup.

        active_trades lives only in memory while the engine runs, so a crash or
        restart loses all stop/target tracking and can orphan broker-side stop
        orders. On start we reload the persisted trades and:

        - drop any trade whose position is no longer open (it closed while we
          were down), cancelling a lingering protective stop if one is still live;
        - for a position that's still open, verify its protective stop order is
          still live and re-place it if it's gone (cancelled, filled, or never
          recorded) — so no resumed position is left unprotected;
        - clear a stale exit_pending unless the recorded exit order is still
          working, in which case we keep waiting on it.

        Untracked live positions are intentionally left to the normal
        auto-mode adoption path in monitor_positions.
        """
        persisted = config_manager.load_active_trades()
        if not persisted:
            return

        # Restore persisted ownership before attempting broker reads.  An
        # unavailable startup snapshot is uncertainty, not proof that the
        # position is flat or unowned.
        with self._trade_lock:
            self.active_trades = {
                symbol: dict(trade) for symbol, trade in persisted.items()
            }

        self._reserved_entry_margin = sum(
            float(trade.get("reserved_margin", 0) or 0)
            for trade in self.active_trades.values()
        )

        for symbol, trade in self.active_trades.items():
            if trade.get("reservation_id") and self._has_canonical_identity(trade):
                risk_manager.restore_entry_reservation(symbol, trade)

        try:
            positions = self._positions()
        except Exception as e:
            self._reconciliation_pending = True
            with self._trade_lock:
                for trade in self.active_trades.values():
                    trade["broker_reconciliation_pending"] = True
                    trade["recovery_state"] = "BROKER_STATE_UNAVAILABLE"
            self._persist_trades()
            self._push_log(f"Reconcile: failed to fetch positions: {e}", level="error")
            return

        open_map = {
            p["tradingsymbol"]: p for p in positions if p.get("quantity", 0) != 0
        }
        open_orders = self._open_order_ids()
        if open_orders is None:
            self._reconciliation_pending = True
            with self._trade_lock:
                for trade in self.active_trades.values():
                    trade["broker_reconciliation_pending"] = True
                    trade["recovery_state"] = "ORDER_STATE_UNAVAILABLE"
            self._persist_trades()
            self._push_log("Reconcile: failed to fetch open orders", level="error")
            return

        reconciled = {}
        for symbol, trade in persisted.items():
            keep = False
            try:
                keep = self._reconcile_one(symbol, trade, open_map, open_orders)
            except Exception as e:
                # A single malformed record must not abort reconciliation of the
                # rest — skip it and keep going.
                self._push_log(
                    f"Reconcile: skipping {symbol} due to error: {e}", level="warning"
                )
                trade["broker_reconciliation_pending"] = True
                trade["recovery_state"] = f"RECONCILIATION_ERROR: {e}"
                self._reconciliation_pending = True
                reconciled[symbol] = trade
            if keep:
                reconciled[symbol] = trade

        with self._trade_lock:
            self.active_trades = reconciled
        self._reconciliation_pending = any(
            trade.get("broker_reconciliation_pending") for trade in reconciled.values()
        )
        self._persist_trades()
        self._push_log(
            f"Reconcile complete: {len(reconciled)} active trade(s) resumed."
        )

    def _cancel_stop_if_live(self, trade: dict, open_orders: set):
        stop_id = trade.get("stop_order_id")
        if stop_id and str(stop_id) in open_orders:
            try:
                execution_gateway.cancel_order(variety="regular", order_id=stop_id)
            except Exception:
                pass

    @staticmethod
    def _has_canonical_identity(trade: dict) -> bool:
        return all(
            trade.get(field) not in (None, "", "UNKNOWN")
            for field in (
                "namespace",
                "account_id",
                "exchange",
                "product",
                "instrument_id",
            )
        ) and str(trade.get("instrument_id")) != str(trade.get("tradingsymbol", ""))

    def _trade_matches_position(self, trade: dict, position: dict) -> bool:
        """Missing persisted identity never grants management authority."""

        if (
            trade.get("ownership_quarantined")
            or trade.get("recovery_state") == "IDENTITY_MISMATCH"
        ):
            return False
        if not self._has_canonical_identity(trade):
            return False
        identity = {
            "namespace": position.get("namespace"),
            "account_id": position.get("account_id"),
            "exchange": position.get("exchange"),
            "product": position.get("product"),
            "instrument_id": position.get("instrument_token"),
        }
        if any(value in (None, "", "UNKNOWN") for value in identity.values()):
            return False
        return str(trade.get("tradingsymbol")) == str(
            position.get("tradingsymbol")
        ) and all(
            str(trade.get(field)) == str(value) for field, value in identity.items()
        )

    def _resolve_legacy_ownership(self, symbol: str, trade: dict) -> bool:
        """Migrate only through canonical, durable journal entry ownership.

        A current same-symbol position/order cannot identify the account of an
        old snapshot. The journal must already bind the same trade and entry ID
        to its account/namespace/token before management can resume.
        """
        if self._has_canonical_identity(trade):
            return True
        trade_id = trade.get("trade_id")
        if not trade_id:
            return False
        try:
            row = journal.get_trade(trade_id)
        except Exception:
            return False
        if not row or not self._has_canonical_identity(row):
            return False
        if (
            row.get("tradingsymbol") != symbol
            or row.get("direction") != trade.get("direction")
            or not row.get("entry_order_id")
            or str(row["entry_order_id"]) != str(trade.get("entry_order_id"))
        ):
            return False
        fields = ("namespace", "account_id", "exchange", "product", "instrument_id")
        if any(
            trade.get(field) not in (None, "", "UNKNOWN", symbol)
            and str(trade[field]) != str(row[field])
            for field in fields
        ):
            return False
        trade.update({field: row[field] for field in fields})
        trade.update(tradingsymbol=symbol, identity_verified=True)
        if trade.get("recovery_state") == "LEGACY_IDENTITY_UNRESOLVED":
            trade.pop("ownership_quarantined", None)
            trade.pop("recovery_state", None)
        return True

    def _settle_entry_resources(
        self,
        symbol: str,
        *,
        pending_quantity: Optional[int] = None,
        outcome: Optional[str] = None,
    ) -> None:
        """Release cash as broker exposure takes over; counts wait for accounting.

        Mutate the authoritative record, so repeated recovery and restart cannot
        subtract a stale copied reservation twice. A working/unknown remainder
        retains its proportional cash hold and all entry/count ownership.
        """
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if trade is None:
                return
            held = float(trade.get("reserved_margin", 0) or 0)
            remaining = held
            if outcome is not None:
                remaining = 0.0
            elif pending_quantity is not None:
                unit_price = trade.get("reservation_price", trade.get("entry_price"))
                if unit_price is not None:
                    remaining = min(held, max(0, pending_quantity) * unit_price)
            self._reserved_entry_margin = max(
                0.0, self._reserved_entry_margin - (held - remaining)
            )
            trade["reserved_margin"] = remaining
            if outcome is not None:
                reservation_id = trade.get("reservation_id")
                if outcome == "filled":
                    risk_manager.complete_entry_reservation(reservation_id)
                else:
                    risk_manager.release_entry_reservation(reservation_id)
                trade.pop("reservation_id", None)

    def _quarantine_identity_mismatch(self, symbol: str, trade: dict) -> None:
        with self._trade_lock:
            current = self.active_trades.get(symbol)
            if current is not None:
                current["ownership_quarantined"] = True
                current["broker_reconciliation_pending"] = True
                current["recovery_state"] = "IDENTITY_MISMATCH"

    def _entry_fill_quantity(self, trade: dict) -> Optional[int]:
        """Return verified entry fill quantity, or ``None`` when unknown."""

        order_id = trade.get("entry_order_id")
        if not order_id:
            return None
        order = self._find_order(str(order_id))
        if order is None or not order:
            return None
        status = str(order.get("status", "")).upper()
        if status not in self._TERMINAL_ORDER_STATUSES:
            return None
        reported = int(order.get("filled_quantity", 0) or 0)
        if not self._has_canonical_identity(trade) and reported:
            return None
        try:
            fills = self._fill_snapshot().require_complete().fills
        except Exception:
            return None
        quantity = sum(
            fill.quantity
            for fill in fills
            if fill.broker_order_id == str(order_id)
            and fill.side == trade.get("direction")
            and fill.key.tradingsymbol == trade.get("tradingsymbol")
            and fill.key.namespace.value == str(trade.get("namespace"))
            and fill.key.account_id == str(trade.get("account_id"))
            and fill.key.exchange == str(trade.get("exchange"))
            and fill.key.product == str(trade.get("product"))
            and fill.key.instrument_id == str(trade.get("instrument_id"))
        )
        if quantity != reported:
            return None
        return quantity

    def _entry_zero_fill_verified(self, trade: dict) -> bool:
        return self._entry_fill_quantity(trade) == 0

    def _entry_obligation_unresolved(self, trade: dict) -> bool:
        return trade.get(
            "entry_state"
        ) == "RECOVERY_REQUIRED" and not self._entry_zero_fill_verified(trade)

    def _reconcile_one(
        self, symbol: str, trade: dict, open_map: dict, open_orders: set
    ) -> bool:
        """Reconcile a single persisted trade. Returns True to keep tracking it."""
        trade.setdefault("tradingsymbol", symbol)
        if not self._resolve_legacy_ownership(symbol, trade):
            trade["broker_reconciliation_pending"] = True
            trade["recovery_state"] = "LEGACY_IDENTITY_UNRESOLVED"
            trade["ownership_quarantined"] = True
            return True
        current_account = getattr(kite_client, "account_id", None)
        current_namespace = getattr(
            getattr(kite_client, "namespace", None), "value", None
        )
        if (
            self._has_canonical_identity(trade)
            and current_account not in (None, "", "UNKNOWN")
            and str(trade.get("account_id")) != str(current_account)
        ) or (
            self._has_canonical_identity(trade)
            and current_namespace not in (None, "", "UNKNOWN")
            and str(trade.get("namespace")) != str(current_namespace)
        ):
            trade["broker_reconciliation_pending"] = True
            trade["recovery_state"] = "IDENTITY_MISMATCH"
            trade["ownership_quarantined"] = True
            return True
        pos = open_map.get(symbol)
        if not pos and trade.get("entry_state") == "RECOVERY_REQUIRED":
            self._recover_pending_entry(symbol, {}, trade)
            trade.update(self.active_trades.get(symbol, trade))
            return symbol in self.active_trades
        if not pos:
            if self._entry_obligation_unresolved(trade):
                trade["broker_reconciliation_pending"] = True
                trade["recovery_state"] = "ENTRY_OUTCOME_UNRESOLVED"
                return True
            self._cancel_stop_if_live(trade, open_orders)
            self._push_log(
                f"Reconcile: {symbol} no longer open; dropping stale tracking."
            )
            return False

        if (
            not self._has_canonical_identity(trade)
            and trade.get("entry_state") == "RECOVERY_REQUIRED"
        ):
            trade["broker_reconciliation_pending"] = True
            trade["recovery_state"] = "LEGACY_IDENTITY_UNRESOLVED"
            return True
        position_identity = {
            "namespace": pos.get("namespace"),
            "account_id": pos.get("account_id"),
            "exchange": pos.get("exchange"),
            "product": pos.get("product"),
            "instrument_id": pos.get("instrument_token"),
        }
        if any(
            position_identity[field] not in (None, "")
            and trade.get(field) not in (None, "")
            and str(position_identity[field]) != str(trade.get(field))
            for field in position_identity
        ):
            trade["broker_reconciliation_pending"] = True
            trade["recovery_state"] = "IDENTITY_MISMATCH"
            trade["ownership_quarantined"] = True
            return True

        # If the live position's side no longer matches the recorded trade, the
        # record is stale (position was closed and reopened the other way). Drop
        # it rather than protect it with a wrong-side stop.
        live_direction = "BUY" if pos["quantity"] > 0 else "SELL"
        if trade.get("direction") != live_direction:
            self._cancel_stop_if_live(trade, open_orders)
            self._push_log(
                f"Reconcile: {symbol} direction changed "
                f"(was {trade.get('direction')}, now {live_direction}); dropping.",
                level="warning",
            )
            return False

        if trade.get(
            "entry_state"
        ) == "RECOVERY_REQUIRED" and not self._has_canonical_identity(trade):
            trade["broker_reconciliation_pending"] = True
            trade["recovery_state"] = "LEGACY_IDENTITY_UNRESOLVED"
            return True

        if trade.get("entry_state") == "RECOVERY_REQUIRED":
            self._recover_pending_entry(symbol, pos, trade)
            trade.update(self.active_trades.get(symbol, trade))
            return symbol in self.active_trades

        # Position still open — ensure a live protective stop.
        stop_id = trade.get("stop_order_id")
        recovery_state = trade.get("recovery_state")
        attempted_stop_id = trade.get("protection_attempt_order_id")
        if recovery_state == "STOP_CANCEL_UNCONFIRMED":
            # The old stop's cancellation outcome is unknown. Do not create a
            # second protective order until an operator or a later verified
            # broker read resolves that linkage.
            trade["broker_reconciliation_pending"] = True
            return True
        if recovery_state == "PROTECTION_REPLACEMENT_UNCONFIRMED":
            if attempted_stop_id and str(attempted_stop_id) in open_orders:
                # The previous submission is now independently visible as
                # working. Adopt that linkage; do not submit another stop.
                trade["stop_order_id"] = attempted_stop_id
                trade.pop("protection_attempt_order_id", None)
                trade["broker_reconciliation_pending"] = False
                trade.pop("recovery_state", None)
                stop_id = attempted_stop_id
            else:
                # A missing or unobservable order id is an unknown submission
                # outcome. Retrying could create a duplicate protective order.
                trade["broker_reconciliation_pending"] = True
                return True
        if stop_id and str(stop_id) not in open_orders:
            protection_status = self._protection_status(stop_id)
            if protection_status == "UNKNOWN":
                trade["broker_reconciliation_pending"] = True
                trade["recovery_state"] = "PROTECTION_STATE_UNKNOWN"
                return True
            if protection_status == "COMPLETE":
                residual = self._find_live_position_by_symbol(symbol)
                if residual is None:
                    trade["broker_reconciliation_pending"] = True
                    trade["recovery_state"] = "STOP_COMPLETE_RECONCILIATION"
                    return True
                if not residual:
                    trade["cleanup_pending"] = True
                    trade["broker_reconciliation_pending"] = True
                    trade["recovery_state"] = "STOP_COMPLETE_FLAT_PENDING"
                    return True
                pos = residual
        if not stop_id or str(stop_id) not in open_orders:
            self._push_log(
                f"Reconcile: {symbol} is open with no live protective stop; re-placing.",
                level="warning",
            )
            new_stop_id = self._place_protective_stop(
                {
                    "tradingsymbol": symbol,
                    "direction": trade["direction"],
                    "stopLoss": trade["sl"],
                },
                abs(pos["quantity"]),
                pos.get("exchange", trade.get("exchange", "NSE")),
                pos.get("product", "MIS"),
            )

            if not new_stop_id or not self._confirm_protective_stop(new_stop_id):
                self._push_log(
                    f"CRITICAL: Failed to confirm replaced protective stop for {symbol} during reconcile. Emergency flattening.",
                    level="error",
                )
                # If an id was returned but confirmation was lost, do not
                # flatten or submit another order: that stop may already be
                # live. With no id at all there is no known protective order,
                # so emergency flatten remains the safer fallback.
                if not new_stop_id:
                    try:
                        execution_gateway.emergency_flatten_position(
                            variety="regular",
                            exchange=pos.get("exchange", trade.get("exchange", "NSE")),
                            tradingsymbol=symbol,
                            transaction_type="SELL"
                            if trade["direction"] == "BUY"
                            else "BUY",
                            quantity=abs(pos["quantity"]),
                            product=pos.get("product", "MIS"),
                            order_type="MARKET",
                        )
                    except Exception as e:
                        self._push_log(
                            f"Emergency flatten failed for {symbol}: {e}",
                            level="error",
                        )

                risk_config = config_manager.get_risk_config()
                if risk_config.get("haltAutoTradesOnStopFailure", True):
                    self._push_log(
                        "Halting auto trades due to protective stop failure in reconcile.",
                        level="error",
                    )
                    self.stop()

                # Keep the persisted ownership and the prior linkage.  The
                # replacement attempt itself may have an unknown outcome; a
                # failed confirmation is not permission to forget exposure.
                trade["broker_reconciliation_pending"] = True
                trade["recovery_state"] = "PROTECTION_REPLACEMENT_UNCONFIRMED"
                if new_stop_id:
                    trade["protection_attempt_order_id"] = new_stop_id
                return True

            trade["stop_order_id"] = new_stop_id or None
            self._persist_execution_linkage(trade)

        # Keep waiting on an exit that's still working; otherwise clear it.
        exit_id = trade.get("exit_order_id")
        if exit_id and str(exit_id) in open_orders:
            trade["exit_pending"] = True
        else:
            trade["exit_pending"] = False
            trade["exit_order_id"] = None

        trade.setdefault("exchange", pos.get("exchange", "NSE"))
        trade["broker_reconciliation_pending"] = False
        trade.pop("recovery_state", None)
        return True

    def _get_tick_size(self, symbol: str, exchange: str = "NSE") -> float:
        if exchange != "NSE":
            raise ValueError("Only NSE instrument lookup is supported in phase 1")
        if symbol in self._tick_size_map:
            return self._tick_size_map[symbol]
        try:
            instruments = kite_client.get_instruments(exchange)
            for i in instruments:
                instrument_symbol = str(i["tradingsymbol"]).upper()
                self._tick_size_map[instrument_symbol] = float(i.get("tick_size", 0.05))
                token = i.get("instrument_token", i.get("instrumentToken"))
                if token not in (None, ""):
                    self._instrument_map[instrument_symbol] = str(token)
            return self._tick_size_map.get(symbol.upper(), 0.05)
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
        # Reconcile risk manager state from broker
        try:
            risk_manager.reconcile_state()
        except Exception as e:
            self._push_log(
                f"Risk manager reconcile on start failed: {e}", level="error"
            )

        # Resume managing any positions that were open when we last ran, before
        # the monitor loop starts. Failures here must not block startup.
        try:
            self.reconcile_active_trades()
        except Exception as e:
            self._push_log(
                f"Reconcile active trades on start failed: {e}", level="error"
            )

        scanner.last_scanned_candle.clear()

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

    def _push_state_update(self, status: str = None):
        if not status:
            status = "scanning" if self.running else "idle"

        event = {
            "event": "agent:state-update",
            "data": {
                "running": self.running,
                "mode": self.mode,
                "status": status,
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
                "timestamp": now_utc().isoformat(),
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
        last_reconcile_time = 0
        scan_interval = 30  # Check for new signals every 30 seconds
        reconcile_interval = 300  # Reconcile executions every 5 minutes
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
                    self._push_state_update(status="scanning")
                    self.scan_and_trade()
                    self._push_state_update(status="monitoring")
                    last_scan_time = current_time

                # 4. Reconcile journal trades periodically
                if current_time - last_reconcile_time >= reconcile_interval:
                    self._reconcile_journal_trades()
                    last_reconcile_time = current_time

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
                str(i["tradingsymbol"]).upper(): str(
                    i.get("instrument_token", i.get("instrumentToken"))
                )
                for i in instruments
                if i.get("instrument_token", i.get("instrumentToken")) not in (None, "")
            }
        return self._instrument_map

    def _resolve_instrument_identity(
        self, symbol: str, exchange: str, requested: object = None
    ) -> Optional[str]:
        """Return the broker's canonical token, never a symbol placeholder."""

        try:
            # Populate the identity and tick caches from the same authoritative
            # instrument record.  A quote/tick lookup alone is not identity
            # resolution and must not turn a symbol into a verified token.
            self._get_tick_size(symbol, exchange)
            instrument_id = self._instrument_map.get(symbol.upper())
        except Exception:
            instrument_id = None
        if instrument_id in (None, "", symbol):
            return None
        if requested not in (None, "") and str(requested) != str(instrument_id):
            return None
        return str(instrument_id)

    def _get_current_refresh_interval(self) -> int:
        screener_config = config_manager.get_screener_config()
        refresh_schedule = screener_config.get("refreshSchedule", {})
        opening_mins = refresh_schedule.get("openingPeriodMins", 15)
        normal_mins = refresh_schedule.get("normalSessionMins", 60)
        late_mins = refresh_schedule.get("lateSessionMins", 30)

        now = now_utc().astimezone(EXCHANGE_TIMEZONE).time()
        if now < datetime.time(10, 0):
            return opening_mins
        elif now < datetime.time(14, 0):
            return normal_mins
        else:
            return late_mins

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
        now = now_utc()
        needs_refresh = False
        if not self.dynamic_watchlist:
            needs_refresh = True
        elif self.last_universe_refresh_time:
            interval_mins = self._get_current_refresh_interval()
            if (
                now - self.last_universe_refresh_time
            ).total_seconds() / 60.0 >= interval_mins:
                needs_refresh = True

        if needs_refresh:
            from .nifty_universe import get_nifty100_universe
            from .screener import screener_engine

            custom_watchlist = config_manager.get_watchlist()
            full_universe = list(set(get_nifty100_universe() + custom_watchlist))

            self._push_log(
                f"Running algorithmic screener on NIFTY 100 + {len(custom_watchlist)} custom stocks..."
            )
            try:
                new_watchlist = screener_engine.generate_daily_watchlist(
                    universe=full_universe, limit=12
                )

                self.watchlist_rankings = {
                    symbol: i + 1 for i, symbol in enumerate(new_watchlist)
                }

                with self._trade_lock:
                    preserved = set(self.active_trades.keys()) | self._pending_entries

                self.dynamic_watchlist = list(set(new_watchlist) | preserved)
                self.universe_version += 1
                self.last_universe_refresh_time = now

                self._push_log(
                    f"Dynamic Watchlist refreshed (Version: {self.universe_version}): {', '.join(self.dynamic_watchlist)}"
                )
            except Exception as e:
                self._push_log(
                    f"Dynamic Watchlist refresh failed: {e}. Retaining last known good universe.",
                    level="error",
                )

        def handle_new_signal(signal):
            # NOTE: This callback is invoked from scanner ThreadPoolExecutor
            # threads, so active_trades access must be guarded by the lock.
            signal["universe_version"] = str(self.universe_version)
            signal["screener_ranking"] = self.watchlist_rankings.get(
                signal["tradingsymbol"]
            )

            if signal["signal_score"] >= 70:
                self._push_signal(signal)
                prob = signal.get("estimated_probability")
                if self.mode == "auto" and (prob is None or prob >= 0.60) and can_trade:
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

        # Atomically guard against duplicate entry orders for the same symbol in trading_engine
        # (execution_gateway also has its own lock to prevent broker-level duplicate orders).
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
        if not all(
            self._is_valid_management_price(signal.get(field))
            for field in ("entryPrice", "stopLoss", "target")
        ):
            self._push_log(
                "Entry rejected: prices must be finite positive JSON numbers",
                level="warning",
            )
            return False
        signal = dict(signal)
        for field in ("entryPrice", "stopLoss", "target"):
            signal[field] = float(signal[field])

        with self._trade_lock:
            linkage_pending = any(
                trade.get("execution_linkage_pending")
                for trade in self.active_trades.values()
            )
        if linkage_pending:
            self._push_log(
                "Entry rejected: execution linkage persistence is pending",
                level="warning",
            )
            return False

        transaction_type = "BUY" if signal["direction"] == "BUY" else "SELL"
        exchange = str(signal.get("exchange", "NSE")).upper()
        product = str(signal.get("product", "MIS")).upper()
        if exchange != "NSE" or product != "MIS":
            self._push_log(
                "Entry rejected: only NSE/MIS is supported in phase 1", level="warning"
            )
            return False
        requested_instrument = signal.get("instrumentToken") or signal.get(
            "instrument_token"
        )
        instrument_id = self._resolve_instrument_identity(
            symbol, exchange, requested_instrument
        )
        if not instrument_id:
            self._push_log(
                f"Cannot execute signal {signal.get('id')}: canonical instrument identity is unavailable",
                level="warning",
            )
            return False
        raw_account_id = getattr(kite_client, "account_id", None)
        account_id = str(raw_account_id or "UNKNOWN")
        if account_id in {"", "UNKNOWN"}:
            self._push_log(
                f"Cannot execute signal {signal.get('id')}: broker account identity is unavailable",
                level="warning",
            )
            return False
        namespace = getattr(
            getattr(kite_client, "namespace", None), "value", None
        ) or str(getattr(kite_client, "namespace", "LIVE"))
        tick_size = self._get_tick_size(symbol, exchange)
        try:
            entry_price = self._round_to_tick(float(signal["entryPrice"]), tick_size)
            signal["entryPrice"] = entry_price
        except (KeyError, TypeError, ValueError):
            self._push_log(
                f"Cannot execute signal {signal.get('id')}: entry price must be numeric",
                level="warning",
            )
            return False

        try:
            baseline_position = self._find_live_position(symbol, signal["direction"])
            if baseline_position is None:
                raise RuntimeError(f"Snapshot unavailable for {symbol}")
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
                if (
                    isinstance(available_margin, bool)
                    or not isinstance(available_margin, (int, float))
                    or not math.isfinite(available_margin)
                ):
                    available_margin = float("nan")
                else:
                    available_margin = max(
                        0, available_margin - self._reserved_entry_margin
                    )

                validation_error = self._validate_entry_signal(
                    signal, entry_price, available_margin
                )
                if validation_error:
                    self._push_log(validation_error, level="warning")
                    return False

                llm_qty = signal.get("quantity")
                if llm_qty is not None:
                    if isinstance(llm_qty, bool) or not isinstance(llm_qty, int):
                        self._push_log(
                            f"Cannot execute signal {signal.get('id')}: quantity must be an integer",
                            level="warning",
                        )
                        return False
                    cap_advisory = getattr(risk_manager, "cap_advisory_quantity", None)
                    qty = (
                        cap_advisory(
                            llm_qty,
                            entry_price,
                            available_margin,
                            signal["stopLoss"],
                        )
                        if cap_advisory
                        else llm_qty
                    )
                else:
                    qty = risk_manager.calculate_position_size(
                        entry_price, signal["stopLoss"], available_margin
                    )
                if qty <= 0:
                    self._push_log(
                        f"Cannot execute signal {signal['id']}: insufficient available margin",
                        level="warning",
                    )
                    return False

                broker_snapshot = self._broker_snapshot()
                reservation_id, reject_reason = risk_manager.reserve_entry(
                    symbol=symbol,
                    direction=signal["direction"],
                    qty=qty,
                    price=entry_price,
                    exchange=exchange,
                    product=product,
                    instrument_id=instrument_id,
                    broker_snapshot=broker_snapshot,
                )
                if not reservation_id:
                    self._push_log(
                        f"Portfolio risk limit rejected {symbol}: {reject_reason}",
                        level="warning",
                    )
                    return False

                reserved_margin = qty * entry_price
                self._reserved_entry_margin += reserved_margin
                try:
                    order_id = execution_gateway.place_order(
                        is_entry=True,
                        variety="regular",
                        exchange=exchange,
                        tradingsymbol=symbol,
                        transaction_type=transaction_type,
                        quantity=qty,
                        product=product,
                        order_type="LIMIT",
                        price=entry_price,
                        entry_reservation_id=reservation_id,
                        order_role=OrderRole.ENTRY,
                    )
                except OrderSubmissionUnknown as exc:
                    self.active_trades[symbol] = {
                        "trade_id": None,
                        "sl": signal["stopLoss"],
                        "target": signal["target"],
                        "direction": signal["direction"],
                        "entry_price": None,
                        "signal_entry_price": entry_price,
                        "entry_time": None,
                        "entry_observed_at": now_utc(),
                        "original_strategy": signal.get("strategy", "unknown"),
                        "entry_order_id": None,
                        "entry_submission_state": "UNKNOWN",
                        "stop_order_id": None,
                        "quantity": qty,
                        "product": product,
                        "exchange": exchange,
                        "instrument_id": instrument_id,
                        "account_id": account_id,
                        "namespace": namespace,
                        "tradingsymbol": symbol,
                        "identity_verified": True,
                        "entry_state": "RECOVERY_REQUIRED",
                        "broker_reconciliation_pending": True,
                        "reservation_id": reservation_id,
                        "reserved_margin": reserved_margin,
                        "reservation_price": entry_price,
                        "requested_quantity": qty,
                        "exit_pending": False,
                        "exit_order_id": None,
                    }
                    self._persist_trades()
                    reserved_margin = 0
                    self._push_log(str(exc), level="error")
                    return False

            self._push_log(
                f"Executed {transaction_type} for {symbol}, qty {qty}, order_id {order_id}"
            )

            # Wait for fill — NO LOCK held; this blocks up to 15 seconds.
            position = self._wait_for_entry_fill(signal, order_id, baseline_quantity)
            resolution = self._entry_recovery.pop(order_id, None)
            if position is None and resolution == "UNKNOWN":
                with self._trade_lock:
                    self.active_trades[symbol] = {
                        "trade_id": None,
                        "sl": signal["stopLoss"],
                        "target": signal["target"],
                        "direction": signal["direction"],
                        "entry_price": None,
                        "signal_entry_price": entry_price,
                        "entry_time": None,
                        "entry_observed_at": now_utc(),
                        "original_strategy": signal.get("strategy", "unknown"),
                        "entry_order_id": order_id,
                        "stop_order_id": None,
                        "quantity": qty,
                        "product": product,
                        "exchange": exchange,
                        "instrument_id": instrument_id,
                        "account_id": account_id,
                        "namespace": namespace,
                        "tradingsymbol": symbol,
                        "identity_verified": True,
                        "entry_state": "RECOVERY_REQUIRED",
                        "broker_reconciliation_pending": True,
                        "reservation_id": reservation_id,
                        "reserved_margin": reserved_margin,
                        "reservation_price": entry_price,
                        "requested_quantity": qty,
                        "exit_pending": False,
                        "exit_order_id": None,
                    }
                self._persist_trades()
                self._push_log(
                    f"Entry {order_id} for {symbol} has unknown fill state; retaining ownership for recovery.",
                    level="error",
                )
                reserved_margin = 0
                return False
            if position and resolution == "PARTIAL_WORKING":
                # The order remainder is still live, but the broker has
                # already confirmed a residual position.  Register ownership
                # before placing protection; do not journal/finalize the entry
                # until its total execution is terminal and allocatable.
                with self._trade_lock:
                    self.active_trades[symbol] = {
                        "trade_id": None,
                        "sl": signal["stopLoss"],
                        "target": signal["target"],
                        "direction": signal["direction"],
                        "entry_price": None,
                        "signal_entry_price": entry_price,
                        "entry_time": None,
                        "entry_observed_at": now_utc(),
                        "original_strategy": signal.get("strategy", "unknown"),
                        "entry_order_id": order_id,
                        "stop_order_id": None,
                        "quantity": qty,
                        "executed_entry_quantity": None,
                        "residual_quantity": abs(position.get("quantity", 0)),
                        "product": position.get("product", product),
                        "exchange": position.get("exchange", exchange),
                        "instrument_id": instrument_id,
                        "account_id": account_id,
                        "namespace": namespace,
                        "tradingsymbol": symbol,
                        "identity_verified": True,
                        "entry_state": "RECOVERY_REQUIRED",
                        "entry_remainder_pending": True,
                        "broker_reconciliation_pending": True,
                        "reservation_id": reservation_id,
                        "reserved_margin": reserved_margin,
                        "reservation_price": entry_price,
                        "requested_quantity": qty,
                        "exit_pending": False,
                        "exit_order_id": None,
                    }
                reserved_margin = 0
                self._recover_pending_entry(
                    symbol, position, dict(self.active_trades[symbol])
                )
                self._persist_trades()
                return False
            if not position:
                with self._trade_lock:
                    self._reserved_entry_margin -= reserved_margin
                    reserved_margin = 0
                self._push_log(
                    f"Entry order {order_id} for {symbol} not filled. Not tracking as active trade.",
                    level="warning",
                )
                if resolution == "CONFIRMED_UNFILLED":
                    release_reservation = getattr(
                        risk_manager, "release_entry_reservation", None
                    )
                    if release_reservation:
                        release_reservation(reservation_id)
                try:
                    execution_gateway.cancel_order(variety="regular", order_id=order_id)
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
                position.get("product", product),
            )
            if not stop_order_id or not self._confirm_protective_stop(stop_order_id):
                self._push_log(
                    f"CRITICAL: Failed to confirm protective stop for {symbol}. Emergency flattening.",
                    level="error",
                )
                try:
                    execution_gateway.emergency_flatten_position(
                        variety="regular",
                        exchange=position.get("exchange", signal["exchange"]),
                        tradingsymbol=symbol,
                        transaction_type="SELL"
                        if signal["direction"] == "BUY"
                        else "BUY",
                        quantity=abs(position.get("quantity", qty)) or qty,
                        product=position.get("product", product),
                        order_type="MARKET",
                    )
                except Exception as e:
                    self._push_log(
                        f"Emergency flatten failed for {symbol}: {e}", level="error"
                    )

                risk_config = config_manager.get_risk_config()
                if risk_config.get("haltAutoTradesOnStopFailure", True):
                    self._push_log(
                        "Halting auto trades due to protective stop failure.",
                        level="error",
                    )
                    self.stop()

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

            actual_entry_price = position.get("average_price")
            if not self._is_valid_management_price(actual_entry_price):
                actual_entry_price = None
            actual_quantity = abs(position.get("quantity", qty)) or qty
            entry_time = None
            try:
                entry_fills = [
                    fill
                    for fill in self._fill_snapshot().require_complete().fills
                    if fill.broker_order_id == str(order_id)
                    and fill.side == transaction_type
                    and fill.key.tradingsymbol == symbol
                    and fill.key.exchange == exchange
                    and fill.key.product == product
                    and fill.key.account_id == account_id
                    and fill.key.namespace.value == namespace
                    and fill.key.instrument_id == str(instrument_id)
                ]
                if sum(fill.quantity for fill in entry_fills) == actual_quantity:
                    actual_entry_price = accounting_service.project_fills(
                        entry_fills
                    ).vwap
                    entry_time = self._entry_execution_time(entry_fills)
            except Exception as exc:
                self._push_log(
                    f"Entry execution time unavailable for {symbol}: {exc}",
                    level="warning",
                )
            if actual_entry_price is None:
                # A verified position and stop establish exposure/protection,
                # but the submitted limit is not evidence of execution price.
                # Retain the existing stop and count owner until fills recover.
                with self._trade_lock:
                    self.active_trades[symbol] = {
                        "trade_id": None,
                        "sl": signal["stopLoss"],
                        "target": signal["target"],
                        "direction": signal["direction"],
                        "entry_price": None,
                        "signal_entry_price": entry_price,
                        "entry_time": entry_time,
                        "entry_observed_at": now_utc(),
                        "original_strategy": signal.get("strategy", "unknown"),
                        "entry_order_id": order_id,
                        "stop_order_id": stop_order_id,
                        "quantity": actual_quantity,
                        "product": product,
                        "exchange": exchange,
                        "instrument_id": instrument_id,
                        "account_id": account_id,
                        "namespace": namespace,
                        "tradingsymbol": symbol,
                        "identity_verified": True,
                        "entry_state": "RECOVERY_REQUIRED",
                        "recovery_state": "ENTRY_FILL_RECONCILIATION_PENDING",
                        "broker_reconciliation_pending": True,
                        "reservation_id": reservation_id,
                        "reserved_margin": 0,
                        "reservation_price": entry_price,
                        "requested_quantity": qty,
                        "exit_pending": False,
                        "exit_order_id": None,
                        "trailing_sl": signal.get("trailing_sl", False),
                    }
                self._persist_trades()
                self._push_log(
                    f"Entry execution price unavailable for {symbol}; retaining protected recovery ownership.",
                    level="warning",
                )
                return False
            trade_id = str(uuid.uuid4())
            journal_entry_recorded = False
            try:
                journal.open_trade(
                    trade_id=trade_id,
                    tradingsymbol=symbol,
                    exchange=position.get("exchange", exchange),
                    direction=signal["direction"],
                    product=position.get("product", product),
                    strategy=signal.get("strategy", "unknown"),
                    entry_price=actual_entry_price,
                    quantity=actual_quantity,
                    stop_loss=signal["stopLoss"],
                    target=signal["target"],
                    signal_id=signal.get("id"),
                    reasoning=signal.get("reasoning"),
                    signal_score=signal.get("signal_score"),
                    estimated_probability=signal.get("estimated_probability"),
                    calibration_sample_size=signal.get("calibration_sample_size"),
                    confluence_snapshot=evaluation,
                    indicator_snapshot={
                        "features": signal.get("indicators"),
                        "raw_signals": signal.get("raw_signals"),
                        "regime": signal.get("regime"),
                        "portfolio_state": {
                            "open_positions": risk_manager.open_positions,
                            "daily_pnl": risk_manager.daily_pnl,
                        },
                    },
                    universe_version=signal.get("universe_version"),
                    screener_ranking=signal.get("screener_ranking"),
                    signal_entry_price=signal["entryPrice"],
                    namespace=namespace,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    entry_order_id=order_id,
                    stop_order_id=stop_order_id,
                    entry_time=entry_time.isoformat() if entry_time else None,
                )
                journal_entry_recorded = True
                complete_reservation = getattr(
                    risk_manager, "complete_entry_reservation", None
                )
                if complete_reservation:
                    complete_reservation(reservation_id)
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
                    "entry_price": actual_entry_price,
                    "signal_entry_price": entry_price,
                    "entry_time": entry_time,
                    "entry_observed_at": now_utc(),
                    "original_strategy": signal.get("strategy", "unknown"),
                    "entry_order_id": order_id,
                    "stop_order_id": stop_order_id,
                    "quantity": actual_quantity,
                    "product": position.get("product", "MIS"),
                    "exit_pending": False,
                    "exit_order_id": None,
                    "exchange": position.get("exchange", exchange),
                    "instrument_id": position.get("instrument_token", instrument_id),
                    "account_id": position.get("account_id", account_id),
                    "namespace": position.get("namespace", namespace),
                    "tradingsymbol": symbol,
                    "identity_verified": account_id not in {"UNKNOWN", "TEST_COMPAT"},
                    "journal_entry_recorded": journal_entry_recorded,
                    "reservation_id": None
                    if journal_entry_recorded
                    else reservation_id,
                    "entry_state": "OPEN"
                    if journal_entry_recorded
                    else "RECOVERY_REQUIRED",
                    # trailing_sl=True disables the resistance/support smart exit
                    # (Issue 6) since the dynamic stop already handles exit management.
                    "trailing_sl": signal.get("trailing_sl", False),
                }
            self._persist_trades()
            return True
        except Exception as e:
            with self._trade_lock:
                self._reserved_entry_margin = max(
                    0, self._reserved_entry_margin - locals().get("reserved_margin", 0)
                )
            self._push_log(f"Failed to execute signal: {e}")
            return False

    @staticmethod
    def _is_valid_management_price(value) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value) and value > 0
        except OverflowError:
            return False

    def _validate_entry_signal(
        self, signal: dict, entry_price: float, available_margin: float
    ) -> Optional[str]:
        if str(signal.get("exchange", "NSE")).upper() != "NSE":
            return "Entry rejected: only NSE execution is supported in phase 1"
        if str(signal.get("product", "MIS")).upper() != "MIS":
            return "Entry rejected: only MIS execution is supported in phase 1"
        direction = signal.get("direction")
        if direction not in {"BUY", "SELL"}:
            return "Entry rejected: direction must be BUY or SELL"
        if not all(
            self._is_valid_management_price(value)
            for value in (entry_price, signal.get("stopLoss"), signal.get("target"))
        ):
            return "Entry rejected: prices must be finite and positive"
        stop_loss = signal["stopLoss"]
        target = signal["target"]
        if direction == "BUY" and not (stop_loss < entry_price < target):
            return "Entry rejected: BUY requires stop < entry < target"
        if direction == "SELL" and not (target < entry_price < stop_loss):
            return "Entry rejected: SELL requires target < entry < stop"
        if isinstance(available_margin, bool) or not math.isfinite(available_margin):
            return "Entry rejected: margin freshness is unavailable"
        timestamp = signal.get("timestamp")
        if not timestamp:
            return "Entry rejected: signal freshness is unknown"
        try:
            age = (now_utc() - as_utc(timestamp)).total_seconds()
        except (TypeError, ValueError):
            return "Entry rejected: signal timestamp is invalid"
        if age < -5 or age > 300:
            return "Entry rejected: signal is stale"
        return None

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
            if self._reconciliation_pending:
                # Retry startup ownership reconciliation before adoption or
                # replacement decisions.  Persisted ownership remains active
                # while this retry is unresolved.
                self.reconcile_active_trades()
            positions_snapshot = self._position_snapshot().require_complete()
            positions_net = [
                position_to_backend_dict(position)
                for position in positions_snapshot.net
            ]

            # Use the cumulative order-grouped projection whenever the broker
            # exposes one.  Position-only fee estimates are explicitly degraded
            # and are only a compatibility fallback for test/dev adapters.
            broker_snapshot = None
            if hasattr(kite_client, "get_broker_snapshot"):
                broker_snapshot = self._broker_snapshot()
                update_snapshot = getattr(
                    risk_manager, "update_from_broker_snapshot", None
                )
                if callable(update_snapshot):
                    update_snapshot(broker_snapshot)
                else:
                    # Compatibility edge for old test/dev risk adapters.  The
                    # real RiskManager always uses the order-grouped snapshot.
                    risk_manager.update_from_position_snapshot(positions_snapshot)
                reconcile_reservations = getattr(
                    risk_manager, "reconcile_entry_reservations", None
                )
                if callable(reconcile_reservations):
                    reconcile_reservations(broker_snapshot)
            else:
                risk_manager.update_from_position_snapshot(positions_snapshot)

            open_count = sum(1 for p in positions_net if p["quantity"] != 0)
            risk_manager.set_open_positions(open_count)

            # Get symbols of currently open positions to track manual closures
            open_symbols = {
                p["tradingsymbol"] for p in positions_net if p["quantity"] != 0
            }

            with self._trade_lock:
                ownership_records = list(self.active_trades.items())
            for symbol, trade in ownership_records:
                if not self._resolve_legacy_ownership(symbol, trade):
                    trade["ownership_quarantined"] = True
                    trade["broker_reconciliation_pending"] = True
                    trade["recovery_state"] = "LEGACY_IDENTITY_UNRESOLVED"
                elif trade.get("execution_linkage_pending"):
                    self._persist_execution_linkage(trade)

            # Entry outcome/accounting obligations are independent of whether a
            # position row happens to be non-zero this polling cycle.  In
            # particular, a CANCELLED zero-fill entry must release cleanly and
            # a stop-filled entry must retain a durable reconciliation owner.
            with self._trade_lock:
                flat_recovery = [
                    (symbol, dict(trade))
                    for symbol, trade in self.active_trades.items()
                    if trade.get("entry_state") == "RECOVERY_REQUIRED"
                    and not trade.get("ownership_quarantined")
                    and symbol not in open_symbols
                ]
            for symbol, recovery_trade in flat_recovery:
                self._recover_pending_entry(symbol, {}, recovery_trade)

            with self._trade_lock:
                cleanup_symbols = [
                    symbol
                    for symbol, trade in self.active_trades.items()
                    if trade.get("cleanup_pending")
                    and symbol not in open_symbols
                    and not trade.get("ownership_quarantined")
                ]
            for symbol in cleanup_symbols:
                with self._trade_lock:
                    cleanup_trade = dict(self.active_trades.get(symbol, {}))
                if self._entry_obligation_unresolved(cleanup_trade):
                    # A flat snapshot does not settle an entry whose terminal
                    # outcome/fills are still unknown.  Keep ownership for a
                    # later fill instead of ordinary external-close cleanup.
                    continue
                if cleanup_trade.get("recovery_state") == "STOP_COMPLETE_FLAT_PENDING":
                    if not self._journal_external_close(symbol):
                        # A completed stop is a broker event, not proof that the
                        # fill ledger can yet be allocated to this trade.
                        continue
                if self._cancel_protective_stop(symbol):
                    with self._trade_lock:
                        self.active_trades.pop(symbol, None)

            # 1. Sync pending exits FIRST. If an exit order was filled, the position
            # drops from 'open_symbols'. We must process the pending exit before
            # checking for external closures, otherwise the agent thinks the broker
            # closed it unexpectedly!
            pending_exits = []
            with self._trade_lock:
                for symbol, trade in self.active_trades.items():
                    if trade.get("exit_pending"):
                        pending_exits.append(symbol)

            if pending_exits:
                try:
                    all_orders = self._orders()
                    for symbol in pending_exits:
                        self._sync_exit_pending_status(symbol, all_orders)
                except Exception as e:
                    self._push_log(f"Error syncing pending exits: {e}")

            # Snapshot in-memory state under a short lock. All Kite calls below
            # run WITHOUT the lock held, so a slow broker API can't freeze the
            # JSON-RPC thread or the scanner callback (both need this lock).
            with self._trade_lock:
                missing_symbols = [
                    s
                    for s in self.active_trades
                    if s not in open_symbols
                    and not self.active_trades[s].get("exit_pending")
                    and not self.active_trades[s].get("cleanup_pending")
                    and not self.active_trades[s].get("ownership_quarantined")
                    and self.active_trades[s].get("entry_state") != "RECOVERY_REQUIRED"
                ]
                tracked = set(self.active_trades.keys())
                pending = set(self._pending_entries)

            now_ts = time.time()
            confirmed_removals = []
            for symbol in missing_symbols:
                if symbol not in self._external_close_candidates:
                    # First time we see it missing — record the timestamp and skip
                    self._external_close_candidates[symbol] = now_ts
                    self._push_log(
                        f"{symbol} not found in open positions. Will confirm closure in "
                        f"{self._external_close_grace_seconds:.0f}s.",
                        level="info",
                    )
                elif (
                    now_ts - self._external_close_candidates[symbol]
                    >= self._external_close_grace_seconds
                ):
                    confirmed_removals.append(symbol)

            # Clear candidates that came back to life (position reappeared)
            for symbol in list(self._external_close_candidates.keys()):
                if symbol in open_symbols or symbol not in missing_symbols:
                    if symbol in self._external_close_candidates:
                        del self._external_close_candidates[symbol]

            # Manual closures — confirmed after grace period:
            for symbol in confirmed_removals:
                self._external_close_candidates.pop(symbol, None)
                self._push_log(
                    f"Detected external closure for {symbol}. Removing from tracking."
                )
                journaled = self._journal_external_close(symbol)
                if not journaled and self.active_trades.get(symbol, {}).get("trade_id"):
                    with self._trade_lock:
                        current = self.active_trades.get(symbol)
                        if current:
                            current["cleanup_pending"] = True
                            current["broker_reconciliation_pending"] = True
                            current["recovery_state"] = (
                                "ACCOUNTING_RECONCILIATION_PENDING"
                            )
                    continue
                stop_cancelled = self._cancel_protective_stop(symbol)
                with self._trade_lock:
                    current = self.active_trades.get(symbol)
                    if current and stop_cancelled:
                        self.active_trades.pop(symbol, None)
                    elif current:
                        current["cleanup_pending"] = True
                        current["broker_reconciliation_pending"] = True
                        current["recovery_state"] = "STOP_CANCEL_UNCONFIRMED"

            # Evaluate each open position. Trade state is re-read under a short
            # lock immediately before each decision.
            for p in positions_net:
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
                    if trade.get("entry_state") == "RECOVERY_REQUIRED":
                        recovery_trade = dict(trade)
                    else:
                        recovery_trade = None
                    exit_pending = trade.get("exit_pending")
                    direction = trade["direction"]
                    sl = trade.get("sl")
                    target = trade.get("target")

                if not self._trade_matches_position(trade, p):
                    self._quarantine_identity_mismatch(symbol, trade)
                    self._push_log(
                        f"Ownership mismatch for {symbol}; skipping all trade-specific management.",
                        level="warning",
                    )
                    continue

                if recovery_trade is not None:
                    if (
                        recovery_trade.get("recovery_state")
                        == "EMERGENCY_REDUCTION_REQUIRED"
                    ):
                        self._place_exit_order(
                            p, symbol, "Emergency protection recovery"
                        )
                        continue
                    self._recover_pending_entry(symbol, p, recovery_trade)
                    continue

                if exit_pending:
                    continue

                ltp = p.get("last_price") or 0
                if ltp == 0:
                    continue

                # Legacy/corrupt management fields must not interrupt other
                # positions, or disable a valid hard stop on this position.
                valid_sl = self._is_valid_management_price(sl)
                valid_target = self._is_valid_management_price(target)
                if not valid_sl or not valid_target:
                    self._push_log(
                        f"Invalid management price for {symbol}", level="error"
                    )
                hit_sl = valid_sl and (ltp <= sl if direction == "BUY" else ltp >= sl)
                hit_target = valid_target and (
                    ltp >= target if direction == "BUY" else ltp <= target
                )

                with self._trade_lock:
                    trade_snapshot = self.active_trades.get(symbol, {})
                    is_trailing = trade_snapshot.get("trailing_sl", False)

                hit_resistance = False
                if not hit_sl and not hit_target and not is_trailing:
                    hit_resistance = self._check_resistance_exit(symbol, ltp, direction)

                if hit_sl or hit_target or hit_resistance:
                    reason = (
                        "Stop Loss"
                        if hit_sl
                        else ("Target" if hit_target else "Resistance/Support")
                    )
                    self._push_log(
                        f"{reason} hit for {symbol} at {ltp}. Exiting position."
                    )
                    # Re-read the live position right before exiting. If the
                    # broker's protective stop already closed it, our snapshot is
                    # stale — placing an exit now would sell a flat position into
                    # a new (opposite) position. Skip and clean up in that case;
                    # otherwise exit against the actual live position.
                    live = self._find_live_position_by_symbol(symbol)
                    if live is None:
                        self._push_log(
                            f"Position snapshot failed for {symbol}; skipping exit evaluation.",
                            level="warning",
                        )
                        continue
                    if not live:
                        self._push_log(
                            f"{symbol} already flat before exit (broker stop likely filled). Cleaning up.",
                            level="warning",
                        )
                        journaled = self._journal_external_close(symbol)
                        if not journaled and self.active_trades.get(symbol, {}).get(
                            "trade_id"
                        ):
                            with self._trade_lock:
                                current = self.active_trades.get(symbol)
                                if current:
                                    current["cleanup_pending"] = True
                                    current["broker_reconciliation_pending"] = True
                                    current["recovery_state"] = (
                                        "ACCOUNTING_RECONCILIATION_PENDING"
                                    )
                            continue
                        stop_cancelled = self._cancel_protective_stop(symbol)
                        with self._trade_lock:
                            current = self.active_trades.get(symbol)
                            if current and stop_cancelled:
                                self.active_trades.pop(symbol, None)
                            elif current:
                                current["cleanup_pending"] = True
                                current["broker_reconciliation_pending"] = True
                                current["recovery_state"] = "STOP_CANCEL_UNCONFIRMED"
                        continue
                    self._place_exit_order(live, symbol, reason)
        except Exception as e:
            self._push_log(f"Error monitoring positions: {e}")
        finally:
            self._persist_trades()

    def _find_live_position_by_symbol(
        self, symbol: str, trade: Optional[dict] = None
    ) -> Optional[dict]:
        """Fetch the current open position for a symbol, or {} if flat/closed, or None if unknown."""
        try:
            positions = self._positions()
        except Exception:
            return None
        for p in positions:
            if p.get("tradingsymbol") == symbol and p.get("quantity", 0) != 0:
                if trade is not None and not self._trade_matches_position(trade, p):
                    return None
                return p
        return {}

    def _protection_status(self, order_id: Optional[str]) -> str:
        if not order_id:
            return "ABSENT"
        order = self._find_order(order_id)
        if order is None:
            return "UNKNOWN"
        if not order:
            return "ABSENT"
        return str(order.get("status", "UNKNOWN")).upper()

    def _journal_recovered_entry(
        self,
        symbol: str,
        trade: dict,
        position: dict,
        quantity: int,
        entry_price: float,
        fill,
        entry_time,
    ) -> Optional[str]:
        """Idempotently insert a recovered fill before consuming capacity."""

        trade_id = trade.get("trade_id") or (
            f"entry-{trade.get('entry_order_id') or fill.broker_order_id}"
        )
        if trade.get("journal_entry_recorded"):
            return trade_id
        try:
            journal.open_trade(
                trade_id=trade_id,
                tradingsymbol=symbol,
                exchange=fill.key.exchange,
                direction=trade["direction"],
                product=fill.key.product,
                strategy=trade.get("original_strategy", "unknown"),
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=trade["sl"],
                target=trade["target"],
                signal_entry_price=trade.get("signal_entry_price"),
                namespace=fill.key.namespace.value,
                account_id=fill.key.account_id,
                instrument_id=fill.key.instrument_id,
                entry_order_id=trade.get("entry_order_id") or fill.broker_order_id,
                stop_order_id=trade.get("stop_order_id"),
                exit_order_id=trade.get("exit_order_id"),
                entry_time=entry_time.isoformat() if entry_time else None,
            )
        except Exception as exc:
            self._push_log(
                f"Failed to journal recovered entry for {symbol}: {exc}",
                level="error",
            )
            return None
        return trade_id

    @staticmethod
    def _entry_execution_time(fills):
        """Earliest execution, only when the complete allocation is timestamped."""
        times = [fill.exchange_time for fill in fills]
        return min(times) if times and all(times) else None

    def _ensure_recovery_protection(
        self, symbol: str, position: dict, trade: dict
    ) -> bool:
        """Protect a verified residual independently of entry price allocation."""
        residual_quantity = abs(position.get("quantity", 0))
        if not residual_quantity or not self._trade_matches_position(trade, position):
            return False
        if (position["quantity"] > 0) != (trade["direction"] == "BUY"):
            self._quarantine_identity_mismatch(symbol, trade)
            return False
        stop_id = trade.get("protection_attempt_order_id") or trade.get("stop_order_id")
        status = self._protection_status(stop_id)
        if stop_id and status == "ABSENT":
            # A returned id absent from the current book has an unknown
            # submission outcome.  Do not create a second protective stop.
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["recovery_state"] = "ENTRY_PROTECTION_UNCONFIRMED"
                    current["broker_reconciliation_pending"] = True
            return False
        stop_order = (
            self._find_order(stop_id)
            if stop_id and status not in {"UNKNOWN", "ABSENT"}
            else None
        )
        expected_side = "SELL" if trade["direction"] == "BUY" else "BUY"
        covered_quantity = 0
        if status in {"OPEN", "TRIGGER PENDING"} and stop_order:
            if (
                self._trade_matches_position(trade, stop_order)
                and str(stop_order.get("transaction_type", "")) == expected_side
            ):
                covered_quantity = max(
                    0, int(stop_order.get("pending_quantity", 0) or 0)
                )
        if status == "UNKNOWN":
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["recovery_state"] = "ENTRY_PROTECTION_UNCONFIRMED"
                    current["broker_reconciliation_pending"] = True
            return False

        if covered_quantity != residual_quantity and status in {
            "OPEN",
            "TRIGGER PENDING",
        }:
            # Preserve known partial protection while the entry remainder is
            # working.  A cancellation failure/unknown result cannot justify a
            # second stop; record the exact uncovered hard-risk obligation.
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["unprotected_quantity"] = max(
                        0, residual_quantity - covered_quantity
                    )
                    current["recovery_state"] = "PARTIAL_ENTRY_PROTECTION_INSUFFICIENT"
                    current["broker_reconciliation_pending"] = True
            ltp = position.get("last_price") or 0
            breached = ltp > 0 and (
                (trade["direction"] == "BUY" and ltp <= trade["sl"])
                or (trade["direction"] == "SELL" and ltp >= trade["sl"])
            )
            if breached:
                with self._trade_lock:
                    current = self.active_trades.get(symbol)
                    if current:
                        current["recovery_state"] = "EMERGENCY_REDUCTION_REQUIRED"
                return False
            try:
                execution_gateway.cancel_order(variety="regular", order_id=stop_id)
            except Exception:
                return False
            cancelled = self._find_order(stop_id)
            if cancelled is None or not cancelled:
                return False
            cancelled_status = str(cancelled.get("status", "")).upper()
            if cancelled_status not in self._TERMINAL_ORDER_STATUSES:
                return False
            # The next branch places exactly one replacement only after the old
            # partial stop is terminally observed.
            status = cancelled_status

        if status in {
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "REJECTED AMO",
            "ABSENT",
            "COMPLETE",
        }:
            if status == "COMPLETE":
                live = self._find_live_position_by_symbol(symbol, trade)
                if live is None:
                    return False
                if not live:
                    # The next flat recovery cycle performs fill allocation.
                    return False
                position = live
                residual_quantity = abs(live.get("quantity", 0))
            stop_id = self._place_protective_stop(
                {
                    "tradingsymbol": symbol,
                    "direction": trade["direction"],
                    "stopLoss": trade["sl"],
                },
                residual_quantity,
                position["exchange"],
                position["product"],
            )
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    if stop_id:
                        current["protection_attempt_order_id"] = stop_id
                    else:
                        current["recovery_state"] = "EMERGENCY_REDUCTION_REQUIRED"
                        current["unprotected_quantity"] = residual_quantity
                        current["broker_reconciliation_pending"] = True
            status = self._protection_status(stop_id)
        if status not in {"OPEN", "TRIGGER PENDING"}:
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["recovery_state"] = (
                        "ENTRY_PROTECTION_UNCONFIRMED"
                        if stop_id
                        else "EMERGENCY_REDUCTION_REQUIRED"
                    )
                    current["broker_reconciliation_pending"] = True
                    if stop_id:
                        current["protection_attempt_order_id"] = stop_id
            return False

        with self._trade_lock:
            current = self.active_trades.get(symbol)
            if current:
                current["stop_order_id"] = stop_id
                current["protection_quantity"] = residual_quantity
                current["residual_quantity"] = residual_quantity
                linkage_trade = dict(current)
            else:
                linkage_trade = None
        if linkage_trade:
            self._persist_execution_linkage(linkage_trade)

        return True

    def _recover_pending_entry(self, symbol: str, position: dict, trade: dict) -> None:
        """Recover a timed-out entry from verified fills and residual exposure.

        ``quantity`` is the original allocated entry quantity.  It must never
        be overwritten with the current position residual: manual/stop partial
        exits are part of the same trade lifecycle and need that full quantity
        for later exit allocation and P&L repair.
        """

        if trade.get("ownership_quarantined") or not self._has_canonical_identity(
            trade
        ):
            return
        if position and not self._trade_matches_position(trade, position):
            self._quarantine_identity_mismatch(symbol, trade)
            return
        entry_order_id = trade.get("entry_order_id")
        entry_order = self._find_order(entry_order_id) if entry_order_id else None
        if entry_order and (
            not self._trade_matches_position(trade, entry_order)
            or entry_order.get("transaction_type") != trade.get("direction")
        ):
            self._quarantine_identity_mismatch(symbol, trade)
            return
        entry_status = str((entry_order or {}).get("status", "")).upper()
        entry_terminal = (
            bool(entry_order) and entry_status in self._TERMINAL_ORDER_STATUSES
        )
        reported_quantity = int((entry_order or {}).get("filled_quantity", 0) or 0)
        residual_quantity = abs(position.get("quantity", 0))
        requested_quantity = trade.get("requested_quantity", trade.get("quantity", 0))
        pending_quantity = (
            int(entry_order.get("pending_quantity", 0) or 0)
            if entry_order and not entry_terminal
            else max(0, requested_quantity - residual_quantity)
        )
        if entry_terminal and (residual_quantity or reported_quantity == 0):
            pending_quantity = 0
        self._settle_entry_resources(symbol, pending_quantity=pending_quantity)
        with self._trade_lock:
            current = self.active_trades.get(symbol)
            if not current:
                return
            current["residual_quantity"] = residual_quantity
            current["entry_remainder_pending"] = not entry_terminal
            current["broker_reconciliation_pending"] = True
            current["recovery_state"] = (
                "ENTRY_FILL_RECONCILIATION_PENDING"
                if entry_terminal
                else "ENTRY_REMAINDER_UNRESOLVED"
            )
        protected = (
            self._ensure_recovery_protection(symbol, position, trade)
            if position
            else False
        )
        if not entry_order:
            return

        try:
            fills = self._fill_snapshot().require_complete().fills
        except Exception:
            return

        candidates = [
            fill
            for fill in fills
            if fill.broker_order_id == str(entry_order_id)
            and fill.side == trade.get("direction")
            and fill.key.tradingsymbol == symbol
            and (
                not trade.get("exchange") or fill.key.exchange == str(trade["exchange"])
            )
            and (not trade.get("product") or fill.key.product == str(trade["product"]))
        ]
        if not candidates:
            if entry_terminal and reported_quantity == 0 and not residual_quantity:
                self._settle_entry_resources(symbol, outcome="unfilled")
                with self._trade_lock:
                    self.active_trades.pop(symbol, None)
            return

        # Accounting must agree with the canonical owner already established
        # for protection. Fill prices cannot confer ownership on a legacy row.
        identities = {
            (
                fill.key.namespace.value,
                fill.key.account_id,
                fill.key.exchange,
                fill.key.product,
                fill.key.instrument_id,
            )
            for fill in candidates
        }
        if len(identities) != 1:
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["broker_reconciliation_pending"] = True
                    current["recovery_state"] = "LEGACY_IDENTITY_UNRESOLVED"
            return
        namespace, account_id, exchange, product, instrument_id = next(iter(identities))
        if self._has_canonical_identity(trade) and (
            str(trade["namespace"]) != namespace
            or str(trade["account_id"]) != account_id
            or str(trade["exchange"]) != exchange
            or str(trade["product"]) != product
            or str(trade["instrument_id"]) != instrument_id
        ):
            self._quarantine_identity_mismatch(symbol, trade)
            return
        entry_fills = [
            fill
            for fill in candidates
            if (
                fill.key.namespace.value,
                fill.key.account_id,
                fill.key.exchange,
                fill.key.product,
                fill.key.instrument_id,
            )
            == (namespace, account_id, exchange, product, instrument_id)
        ]
        projection = accounting_service.project_fills(entry_fills)
        if projection.vwap is None or projection.quantity <= 0:
            return
        if projection.quantity != reported_quantity:
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["entry_remainder_pending"] = True
                    current["broker_reconciliation_pending"] = True
                    current["recovery_state"] = "ENTRY_FILL_RECONCILIATION_PENDING"
            return

        original_quantity = projection.quantity
        residual_quantity = abs(position.get("quantity", 0))
        fill = entry_fills[0]
        entry_time = self._entry_execution_time(entry_fills)

        with self._trade_lock:
            current = self.active_trades.get(symbol)
            if not current:
                return
            current.update(
                {
                    "quantity": original_quantity,
                    "executed_entry_quantity": original_quantity,
                    "residual_quantity": residual_quantity,
                    "entry_price": projection.vwap,
                    "entry_time": entry_time,
                    "namespace": namespace,
                    "account_id": account_id,
                    "exchange": exchange,
                    "product": product,
                    "instrument_id": instrument_id,
                    "identity_verified": True,
                    "entry_remainder_pending": not entry_terminal,
                }
            )

        if residual_quantity <= 0:
            if not entry_terminal:
                with self._trade_lock:
                    current = self.active_trades.get(symbol)
                    if current:
                        current["broker_reconciliation_pending"] = True
                        current["recovery_state"] = "ENTRY_REMAINDER_WORKING"
                return
            trade_id = self._journal_recovered_entry(
                symbol,
                trade,
                position,
                original_quantity,
                projection.vwap,
                fill,
                entry_time,
            )
            if not trade_id:
                return
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["trade_id"] = trade_id
                    current["journal_entry_recorded"] = True
                    current["quantity"] = original_quantity
                    current["executed_entry_quantity"] = original_quantity
            # Never manufacture a breakeven stop price.  The normal allocation
            # repair either finds all exits or stores nullable pending facts.
            resolved = self._journal_external_close(symbol)
            if not resolved:
                with self._trade_lock:
                    current = self.active_trades.get(symbol)
                    if current:
                        current["broker_reconciliation_pending"] = True
                        current["recovery_state"] = "ACCOUNTING_RECONCILIATION_PENDING"
                        current["entry_state"] = "RECOVERY_REQUIRED"
                return
            self._settle_entry_resources(symbol, outcome="filled")
            with self._trade_lock:
                self.active_trades.pop(symbol, None)
            return

        if not entry_terminal or not protected:
            return
        trade = dict(self.active_trades[symbol])
        stop_id = trade.get("stop_order_id")
        trade_id = self._journal_recovered_entry(
            symbol,
            trade,
            position,
            original_quantity,
            projection.vwap,
            fill,
            entry_time,
        )
        if not trade_id:
            with self._trade_lock:
                current = self.active_trades.get(symbol)
                if current:
                    current["recovery_state"] = "ENTRY_JOURNAL_PENDING"
                    current["broker_reconciliation_pending"] = True
            return

        with self._trade_lock:
            current = self.active_trades.get(symbol)
            if not current:
                return
            current.update(
                {
                    "trade_id": trade_id,
                    "entry_state": "OPEN",
                    "broker_reconciliation_pending": False,
                    "journal_entry_recorded": True,
                    "quantity": original_quantity,
                    "executed_entry_quantity": original_quantity,
                }
            )
            current.pop("recovery_state", None)
            current.pop("protection_attempt_order_id", None)
            current.pop("unprotected_quantity", None)
            linkage_trade = dict(current)
        self._persist_execution_linkage(linkage_trade)
        self._settle_entry_resources(symbol, outcome="filled")
        self._push_log(
            f"Recovered entry for {symbol}; protective stop {stop_id} confirmed.",
            level="warning",
        )

    def _check_resistance_exit(
        self, symbol: str, ltp: float, direction: str, lookback: int = 20
    ) -> bool:
        try:
            token = self._ensure_instrument_map().get(symbol)
            if not token:
                return False

            # Fetch recent candles (use cache where available)
            df, _ = scanner._fetch_candles(token, symbol)
            if df is None or df.empty or len(df) < lookback + 2:
                return False

            # We calculate resistance/support from the lookback window *prior* to the last completed candle.
            # Then we check if the last completed candle tested that level but failed to close beyond it.
            past_candles = df.iloc[-(lookback + 2) : -2]
            last_candle = df.iloc[-2]

            if len(past_candles) < 5:
                return False

            if direction == "BUY":
                # Resistance = highest high over lookback
                resistance = past_candles["high"].max()
                # Exit only if the last completed candle TESTED the resistance (high >= resistance)
                # but CLOSED BELOW it (resistance rejected the price — it won)
                last_high = last_candle["high"]
                last_close = last_candle["close"]

                if last_high >= resistance and last_close < resistance:
                    with self._trade_lock:
                        trade = self.active_trades.get(symbol, {})
                        target = trade.get("target", 0)
                    # Only trigger if resistance is between entry and target
                    # (don't exit prematurely if resistance is below entry)
                    entry_price = trade.get("entry_price", 0)
                    if (
                        entry_price > 0
                        and resistance > entry_price
                        and resistance < target
                    ):
                        self._push_log(
                            f"Resistance exit check for {symbol}: resistance ₹{resistance:.2f}, "
                            f"last candle tested high ₹{last_high:.2f} but closed ₹{last_close:.2f} — resistance wins. Exiting.",
                            level="info",
                        )
                        return True
            else:  # SELL
                # Support = lowest low over lookback
                support = past_candles["low"].min()
                last_low = last_candle["low"]
                last_close = last_candle["close"]

                if last_low <= support and last_close > support:
                    with self._trade_lock:
                        trade = self.active_trades.get(symbol, {})
                        target = trade.get("target", 0)
                    entry_price = trade.get("entry_price", 0)
                    if entry_price > 0 and support < entry_price and support > target:
                        self._push_log(
                            f"Support exit check for {symbol}: support ₹{support:.2f}, "
                            f"last candle tested low ₹{last_low:.2f} but closed ₹{last_close:.2f} — support wins. Exiting.",
                            level="info",
                        )
                        return True
        except Exception as e:
            self._push_log(
                f"Resistance exit check failed for {symbol}: {e}", level="warning"
            )
        return False

    def _adopt_position(self, p: dict):
        """Adopt an untracked open position (auto mode) with a protective stop.

        Kite calls run without the trade lock held; the trade is registered under
        a short lock, and a lost race (another path started tracking the symbol)
        cancels the just-placed stop to avoid an orphaned broker order.
        """
        symbol = p["tradingsymbol"]
        avg_price = p.get("average_price", 0)
        if not self._is_valid_management_price(avg_price):
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
        journal_entry_recorded = False
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
                namespace=p.get(
                    "namespace",
                    getattr(getattr(kite_client, "namespace", None), "value", "LIVE"),
                ),
                account_id=p.get(
                    "account_id", getattr(kite_client, "account_id", "UNKNOWN")
                ),
                instrument_id=p.get("instrument_token", symbol),
                stop_order_id=stop_order_id,
            )
            journal_entry_recorded = True
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
                    "entry_time": None,
                    "entry_observed_at": now_utc(),
                    "original_strategy": "manual",
                    "stop_order_id": stop_order_id,
                    "quantity": abs(p["quantity"]),
                    "product": p.get("product", "MIS"),
                    "exit_pending": False,
                    "exit_order_id": None,
                    "exchange": exchange,
                    "instrument_id": p.get("instrument_token", symbol),
                    "account_id": p.get(
                        "account_id", getattr(kite_client, "account_id", "UNKNOWN")
                    ),
                    "namespace": p.get(
                        "namespace",
                        getattr(
                            getattr(kite_client, "namespace", None), "value", "LIVE"
                        ),
                    ),
                    "tradingsymbol": symbol,
                    "identity_verified": p.get(
                        "account_id", getattr(kite_client, "account_id", "UNKNOWN")
                    )
                    not in (None, "", "UNKNOWN"),
                    "journal_entry_recorded": journal_entry_recorded,
                    "entry_state": "OPEN"
                    if journal_entry_recorded
                    else "RECOVERY_REQUIRED",
                    "broker_reconciliation_pending": not journal_entry_recorded,
                }

        if lost_race:
            if stop_order_id:
                try:
                    execution_gateway.cancel_order(
                        variety="regular", order_id=stop_order_id
                    )
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
        now = now_utc()

        # Get current positions for P&L and LTP data
        try:
            positions = self._positions()
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
                if (
                    trade.get("entry_state") == "RECOVERY_REQUIRED"
                    or trade.get("execution_linkage_pending")
                    or trade.get("ownership_quarantined")
                ):
                    continue
                direction = trade["direction"]
                # Unknown broker execution time stays unknown financially.
                # Policy timers can still use the persisted first observation;
                # substituting this poll's clock would postpone them forever.
                entry_time = trade.get("entry_time") or trade.get("entry_observed_at")
                if entry_time is None:
                    entry_time = now
                    trade["entry_observed_at"] = now
                entry_price = trade.get("entry_price", 0)
                current_sl = trade["sl"]
                last_reeval = trade.get("last_reeval_time") or entry_time

            last_reeval = as_utc(last_reeval) or now
            entry_time = as_utc(entry_time) or now
            mins_since_reeval = (now - last_reeval).total_seconds() / 60
            reeval_interval = risk_config.get("positionRevalIntervalMins", 30)

            # Skip network I/O and evaluation if not enough time has passed
            if mins_since_reeval < reeval_interval:
                continue

            token = instrument_map.get(symbol)
            if not token:
                continue

            # Get current position data
            pos = position_map.get(symbol)
            if not pos:
                continue  # Position already closed
            if not self._trade_matches_position(trade, pos):
                self._quarantine_identity_mismatch(symbol, trade)
                continue

            if entry_price == 0:
                entry_price = pos.get("average_price", 0)

            # Evaluate current strategy signals for this symbol.
            # This is network I/O — intentionally NOT under the lock.
            try:
                evaluation = scanner.evaluate_position(symbol, token)
            except Exception as e:
                self._push_log(f"Error evaluating {symbol}: {e}")
                continue

            mins_held = (now - entry_time).total_seconds() / 60
            ltp = pos.get("last_price") or 0

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
                if trade:
                    trade["last_reeval_time"] = now
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

        self._persist_trades()

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
                execution_gateway.modify_order(
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
            ltp = position.get("last_price") or 0
            if ltp == 0:
                self._push_log(f"Cannot exit {symbol}: no LTP available")
                return
            self._place_exit_order(position, symbol, reason)
        except Exception as e:
            self._push_log(f"Failed to exit {symbol}: {e}")

    def square_off_all(self):
        self._push_log("Squaring off all open positions")
        try:
            positions = self._positions()
            for p in positions:
                if p["quantity"] != 0:
                    self._place_exit_order(p, p["tradingsymbol"], "Square off")
        except Exception as e:
            self._push_log(f"Error in square off: {e}")
        finally:
            self._persist_trades()

    def _wait_for_entry_fill(
        self, signal: dict, order_id: str, baseline_quantity: int = 0
    ) -> Optional[dict]:
        deadline = time.time() + self._entry_fill_timeout_seconds
        symbol = signal["tradingsymbol"]
        direction = signal["direction"]
        while time.time() <= deadline:
            position = self._find_live_position(symbol, direction)
            if position is None:
                time.sleep(self._entry_fill_poll_seconds)
                continue
            filled_quantity = abs(position.get("quantity", 0)) if position else 0
            order = self._find_order(order_id)
            if position and filled_quantity > baseline_quantity:
                if (
                    not order
                    or str(order.get("status", "")).upper()
                    not in self._TERMINAL_ORDER_STATUSES
                ):
                    self._entry_recovery[order_id] = "PARTIAL_WORKING"
                elif (
                    int(order.get("filled_quantity", 0) or 0)
                    != filled_quantity - baseline_quantity
                ):
                    self._entry_recovery[order_id] = "PARTIAL_WORKING"
                return position

            if (
                order
                and str(order.get("status", "")).upper()
                in {
                    "REJECTED",
                    "CANCELLED",
                }
                and (order.get("filled_quantity", 0) or 0) == 0
            ):
                self._entry_recovery[order_id] = "CONFIRMED_UNFILLED"
                return {}

            time.sleep(self._entry_fill_poll_seconds)

        cancel_succeeded = True
        try:
            execution_gateway.cancel_order(variety="regular", order_id=order_id)
        except Exception:
            cancel_succeeded = False
        position = self._find_live_position(symbol, direction)
        if position is None:
            self._entry_recovery[order_id] = "UNKNOWN"
            return None
        if abs(position.get("quantity", 0)) > baseline_quantity:
            order = self._find_order(order_id)
            if (
                not order
                or str(order.get("status", "")).upper()
                not in self._TERMINAL_ORDER_STATUSES
                or int(order.get("filled_quantity", 0) or 0)
                != abs(position["quantity"]) - baseline_quantity
            ):
                self._entry_recovery[order_id] = "PARTIAL_WORKING"
            return position
        order = self._find_order(order_id)
        if (
            cancel_succeeded
            and order
            and str(order.get("status", "")).upper() in {"CANCELLED", "REJECTED"}
            and (order.get("filled_quantity", 0) or 0) == 0
        ):
            self._entry_recovery[order_id] = "CONFIRMED_UNFILLED"
            return {}
        self._entry_recovery[order_id] = "UNKNOWN"
        return None

    def _find_order(self, order_id: str) -> Optional[dict]:
        try:
            orders = self._orders()
        except Exception:
            return None
        for order in orders:
            if str(order.get("order_id")) == str(order_id):
                return order
        return {}

    def _find_live_position(self, symbol: str, direction: str) -> Optional[dict]:
        try:
            positions = self._positions()
        except Exception:
            return None
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
        orders = self._orders()
        for order in orders:
            if str(order.get("order_id")) != str(order_id):
                continue
            status = str(order.get("status", "")).upper()
            filled_qty = order.get("filled_quantity", 0) or 0
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

            if quantity <= 0:
                raise ValueError("Protective stop quantity must be > 0")
            if signal.get("stopLoss", 0) <= 0:
                raise ValueError("Protective stop trigger price must be > 0")
            if exchange not in ["NSE", "NFO", "BSE", "MCX", "CDS"]:
                raise ValueError(f"Invalid exchange {exchange}")
            if product not in ["MIS", "NRML", "CNC"]:
                raise ValueError(f"Invalid product {product}")

            direction = signal["direction"]
            stop_tx = "SELL" if direction == "BUY" else "BUY"
            tick_size = self._get_tick_size(symbol, exchange)
            trigger_price = self._round_to_tick(signal["stopLoss"], tick_size)

            risk_config = config_manager.get_risk_config()
            stop_order_type = risk_config.get("stopOrderType", "SL")

            order_args = {
                "is_entry": False,
                "variety": "regular",
                "exchange": exchange,
                "tradingsymbol": symbol,
                "transaction_type": stop_tx,
                "quantity": quantity,
                "product": product,
                "order_type": stop_order_type,
                "trigger_price": trigger_price,
                "order_role": OrderRole.PROTECTION,
            }

            limit_price = 0
            if stop_order_type == "SL":
                buffer_pct = 0.01
                if stop_tx == "SELL":
                    limit_price = trigger_price * (1 - buffer_pct)
                else:
                    limit_price = trigger_price * (1 + buffer_pct)
                limit_price = self._round_to_tick(limit_price, tick_size)
                order_args["price"] = limit_price

            order_id = execution_gateway.place_order(**order_args)
            self._push_log(
                f"Placed protective stop ({stop_order_type}) for {symbol} at trigger ₹{trigger_price}, limit ₹{limit_price}, order_id {order_id}"
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

    def _confirm_protective_stop(self, order_id: str, timeout_seconds: int = 3) -> bool:
        """Polls the broker to confirm the protective stop is OPEN or TRIGGER PENDING."""
        if not order_id:
            return False

        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            order = self._find_order(order_id)
            if order:
                status = str(order.get("status", "")).upper()
                if status in {"OPEN", "TRIGGER PENDING"}:
                    return True
                elif status in {"REJECTED", "CANCELLED"}:
                    self._push_log(
                        f"Protective stop {order_id} failed with status {status}",
                        level="error",
                    )
                    return False
            time.sleep(0.5)

        self._push_log(
            f"Timed out confirming protective stop {order_id}", level="error"
        )
        return False

    def _cancel_protective_stop(self, symbol: str) -> bool:
        # PHASE-2 LIVE GATE: terminal cancellation is not a handoff barrier.
        # A COMPLETE stop may have filled between the pre-cancel position read
        # and this observation.  Phase 2 must reconcile fills and reread the
        # residual after handoff before _place_exit_order submits quantity.
        # Keep this explicit rather than representing phase 1 as live-safe.
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if not trade:
                return True
            stop_order_id = trade.get("stop_order_id")
            if not stop_order_id:
                return True
            trade["stop_cancel_requested"] = True
        try:
            execution_gateway.cancel_order(variety="regular", order_id=stop_order_id)
        except Exception:
            with self._trade_lock:
                if symbol in self.active_trades:
                    self.active_trades[symbol]["stop_cancel_pending"] = True
            return False

        # A successful cancel request is not a terminal broker fact. Keep the
        # linkage and refuse the opposing exit while the order remains working
        # or its state cannot be read.
        order = self._find_order(stop_order_id)
        if order is None:
            with self._trade_lock:
                if symbol in self.active_trades:
                    self.active_trades[symbol]["stop_cancel_pending"] = True
            return False
        if order:
            status = str(order.get("status", "")).upper()
            if status not in self._TERMINAL_ORDER_STATUSES:
                with self._trade_lock:
                    if symbol in self.active_trades:
                        self.active_trades[symbol]["stop_cancel_pending"] = True
                return False
        with self._trade_lock:
            if symbol in self.active_trades:
                self.active_trades[symbol].pop("stop_cancel_pending", None)
                self.active_trades[symbol].pop("stop_cancel_requested", None)
        return True

    def _place_exit_order(self, position: dict, symbol: str, reason: str = ""):
        # PHASE-2 LIVE GATE: ``position`` is intentionally only a pre-handoff
        # quantity in phase 1.  The stop-cancel/exit transition still requires
        # post-handoff residual reconciliation before live reliance.
        # Atomically check and set exit_pending to prevent duplicate exit orders.
        with self._trade_lock:
            existing = self.active_trades.get(symbol)
            if existing and not self._trade_matches_position(existing, position):
                self._quarantine_identity_mismatch(symbol, existing)
                self._push_log(
                    f"Refusing exit for {symbol}: position ownership is not the tracked trade.",
                    level="warning",
                )
                return
            if symbol in self.active_trades and self.active_trades[symbol].get(
                "exit_pending"
            ):
                return
            # Set exit_pending BEFORE placing the order so concurrent callers
            # see it and bail out, even if the place_order call hasn't returned.
            if symbol in self.active_trades:
                self.active_trades[symbol]["exit_pending"] = True

        if not self._cancel_protective_stop(symbol):
            with self._trade_lock:
                if symbol in self.active_trades:
                    self.active_trades[symbol]["exit_pending"] = False
            self._push_log(
                f"Protective stop cancellation for {symbol} is unconfirmed; exit not submitted.",
                level="warning",
            )
            return

        ltp = position.get("last_price") or 0
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
            "order_role": OrderRole.REDUCTION,
        }
        if price is not None:
            order_kwargs["price"] = price

        try:
            order_id = execution_gateway.place_order(**order_kwargs)
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
                    order_id = execution_gateway.place_order(**order_kwargs)
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
                linkage_trade = dict(self.active_trades[symbol])
            else:
                linkage_trade = None
        if linkage_trade:
            self._persist_execution_linkage(linkage_trade)
        reason_prefix = f"{reason}: " if reason else ""
        self._push_log(f"{reason_prefix}exit order placed for {symbol} ({tx_type})")

    def _sync_exit_pending_status(self, symbol: str, orders: list = None):
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if not trade:
                return
            order_id = trade.get("exit_order_id")
            if not order_id:
                trade["exit_pending"] = False
                return
        if orders is None:
            orders = self._orders()
        for order in orders:
            if str(order.get("order_id")) != str(order_id):
                continue
            status = str(order.get("status", "")).upper()
            if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                with self._trade_lock:
                    current = self.active_trades.get(symbol)
                    if current and current.get("exit_order_id") == order_id:
                        current["exit_pending"] = False
                        current["exit_order_id"] = None
                self._push_log(
                    f"Exit order {order_id} for {symbol} {status.lower()}. Re-attempting on next cycle.",
                    level="warning",
                )
            elif status == "COMPLETE":
                # Order completion alone is not proof that the entire original
                # position was allocated to this exit. Re-read the position
                # before clearing ownership or removing its protection.
                live = self._find_live_position_by_symbol(symbol, trade)
                if live is None:
                    self._push_log(
                        f"Exit {order_id} for {symbol} completed but position state is unavailable; retaining exit obligation.",
                        level="warning",
                    )
                    return
                if live:
                    with self._trade_lock:
                        current = self.active_trades.get(symbol)
                        if not current or current.get("exit_order_id") != order_id:
                            return
                        current["exit_pending"] = False
                        current["exit_order_id"] = None
                        protection_signal = {
                            "tradingsymbol": symbol,
                            "direction": current["direction"],
                            "stopLoss": current["sl"],
                        }
                    replacement = self._place_protective_stop(
                        protection_signal,
                        abs(live["quantity"]),
                        live.get("exchange", "NSE"),
                        live.get("product", "MIS"),
                    )
                    if replacement and self._confirm_protective_stop(replacement):
                        with self._trade_lock:
                            if symbol in self.active_trades:
                                self.active_trades[symbol]["stop_order_id"] = (
                                    replacement
                                )
                                linkage_trade = dict(self.active_trades[symbol])
                            else:
                                linkage_trade = None
                        if linkage_trade:
                            self._persist_execution_linkage(linkage_trade)
                    else:
                        with self._trade_lock:
                            if symbol in self.active_trades:
                                self.active_trades[symbol][
                                    "broker_reconciliation_pending"
                                ] = True
                                self.active_trades[symbol]["recovery_state"] = (
                                    "PROTECTION_REPLACEMENT_UNCONFIRMED"
                                )
                                if replacement:
                                    self.active_trades[symbol][
                                        "protection_attempt_order_id"
                                    ] = replacement
                    self._push_log(
                        f"Exit {order_id} for {symbol} was partial; retained residual position."
                    )
                else:
                    if trade.get("entry_state") == "RECOVERY_REQUIRED":
                        # An emergency reduction can finish while entry fills
                        # are unavailable. Keep its financial/count owner until
                        # the entry and exit allocation can both be reconciled.
                        self._recover_pending_entry(symbol, {}, dict(trade))
                        return
                    if not self._journal_external_close(symbol) and trade.get(
                        "trade_id"
                    ):
                        with self._trade_lock:
                            current = self.active_trades.get(symbol)
                            if current:
                                current["broker_reconciliation_pending"] = True
                                current["recovery_state"] = (
                                    "ACCOUNTING_RECONCILIATION_PENDING"
                                )
                        return
                    stop_cancelled = self._cancel_protective_stop(symbol)
                    with self._trade_lock:
                        current = self.active_trades.get(symbol)
                        if (
                            current
                            and current.get("exit_order_id") == order_id
                            and stop_cancelled
                        ):
                            del self.active_trades[symbol]
                        elif current and current.get("exit_order_id") == order_id:
                            current["exit_pending"] = False
                            current["exit_order_id"] = None
                            current["cleanup_pending"] = True
                            current["recovery_state"] = "STOP_CANCEL_UNCONFIRMED"
                    self._push_log(
                        f"Exit order {order_id} for {symbol} filled and position is flat; removed from tracking."
                    )
            break

    def _reconcile_execution(self, symbol: str, trade_record: dict) -> tuple:
        """Find actual exit fills; unknown prices remain nullable and pending."""

        try:
            fills = self._fill_snapshot().require_complete().fills
        except Exception as e:
            self._push_log(
                f"Failed to fetch trades for reconciliation: {e}", level="warning"
            )
            return None, "UNRECONCILED", None, None

        stop_order_id = str(trade_record.get("stop_order_id", ""))
        exit_order_id = str(trade_record.get("exit_order_id", ""))
        history = trade_record.get("execution_linkage_history") or []
        if isinstance(history, str):
            history = json.loads(history)
        stop_order_ids = {stop_order_id} | {
            item["order_id"] for item in history if item["field"] == "stop_order_id"
        }
        exit_order_ids = {exit_order_id} | {
            item["order_id"] for item in history if item["field"] == "exit_order_id"
        }
        entry_time_val = trade_record.get("entry_time")
        try:
            entry_time = as_utc(entry_time_val)
        except (TypeError, ValueError):
            entry_time = None

        entry_direction = trade_record.get("direction", "BUY")
        exit_transaction_type = "SELL" if entry_direction == "BUY" else "BUY"

        req_exchange = trade_record.get("exchange", "")
        req_product = trade_record.get("product", "")
        req_instrument = trade_record.get("instrument_token")
        req_instrument = trade_record.get("instrument_id", req_instrument)
        req_account = trade_record.get("account_id")
        req_namespace = trade_record.get("namespace")

        def identity_missing(value, *, instrument=False):
            return value in (None, "", "UNKNOWN") or (
                instrument and str(value) == symbol
            )

        # Legacy records may omit identity.  Resolve them only when the fill
        # ledger has one unambiguous canonical key; never assign the current
        # account merely because the symbol matches.
        linked_order_ids = {
            str(trade_record.get(field))
            for field in ("entry_order_id", "exit_order_id", "stop_order_id")
            if trade_record.get(field)
        }
        anchor_order_ids = {
            str(trade_record.get(field))
            for field in ("entry_order_id", "stop_order_id")
            if trade_record.get(field)
        }
        candidate_fills = fills
        if anchor_order_ids and any(
            fill.broker_order_id in anchor_order_ids for fill in fills
        ):
            candidate_fills = [
                fill for fill in fills if fill.broker_order_id in anchor_order_ids
            ]
        identity_candidates = {
            (
                fill.key.namespace.value,
                fill.key.account_id,
                fill.key.exchange,
                fill.key.instrument_id,
                fill.key.tradingsymbol,
                fill.key.product,
            )
            for fill in candidate_fills
            if fill.key.tradingsymbol == symbol
            and (not req_exchange or fill.key.exchange == req_exchange)
            and (not req_product or fill.key.product == req_product)
            and (not linked_order_ids or fill.broker_order_id in linked_order_ids)
        }
        if (
            identity_missing(req_account)
            or identity_missing(req_namespace)
            or identity_missing(req_instrument, instrument=True)
        ):
            if len(identity_candidates) != 1:
                return None, "UNRECONCILED", None, None
            candidate = next(iter(identity_candidates))
            req_namespace = (
                candidate[0] if identity_missing(req_namespace) else req_namespace
            )
            req_account = candidate[1] if identity_missing(req_account) else req_account
            req_exchange = req_exchange or candidate[2]
            req_instrument = (
                candidate[3]
                if identity_missing(req_instrument, instrument=True)
                else req_instrument
            )

        entry_order_id = str(trade_record.get("entry_order_id", ""))
        entry_fills = [
            fill
            for fill in fills
            if entry_order_id
            and fill.broker_order_id == entry_order_id
            and fill.side == entry_direction
            and fill.key.tradingsymbol == symbol
            and (not req_exchange or fill.key.exchange == req_exchange)
            and (not req_product or fill.key.product == req_product)
            and (identity_missing(req_account) or fill.key.account_id == req_account)
            and (
                identity_missing(req_namespace)
                or fill.key.namespace.value == req_namespace
            )
            and (
                identity_missing(req_instrument, instrument=True)
                or fill.key.instrument_id == str(req_instrument)
            )
        ]
        entry_quantity = sum(fill.quantity for fill in entry_fills)
        if entry_fills and entry_quantity != abs(trade_record.get("quantity", 0)):
            # A successful ledger read can still lag the executed position.
            # Partial entry turnover must never receive complete-trade fees
            # or become an eligible outcome for risk/calibration.
            return None, "UNRECONCILED", None, None
        if entry_fills:
            actual_entry_time = self._entry_execution_time(entry_fills)
            if actual_entry_time is not None:
                entry_time = actual_entry_time
                trade_id = trade_record.get("trade_id") or trade_record.get("id")
                if trade_id:
                    try:
                        journal.reconcile_entry_time(
                            trade_id,
                            entry_order_id=entry_order_id,
                            entry_time=actual_entry_time,
                        )
                    except Exception as exc:
                        self._push_log(
                            f"Entry execution time repair failed for {symbol}: {exc}",
                            level="warning",
                        )
                        return None, "UNRECONCILED", None, None
                    with self._trade_lock:
                        active = self.active_trades.get(symbol)
                        if active and active.get("trade_id") == trade_id:
                            active["entry_time"] = actual_entry_time

        matched_fills = []
        for fill in fills:
            if fill.key.tradingsymbol != symbol:
                continue
            if req_exchange and fill.key.exchange != req_exchange:
                continue
            if req_product and fill.key.product != req_product:
                continue
            if not identity_missing(req_account) and fill.key.account_id != req_account:
                continue
            if not identity_missing(
                req_instrument, instrument=True
            ) and fill.key.instrument_id != str(req_instrument):
                continue
            if not identity_missing(req_namespace) and fill.key.namespace.value != str(
                req_namespace
            ):
                continue
            if (
                stop_order_id
                and fill.broker_order_id in stop_order_ids
                and fill.side == exit_transaction_type
            ):
                matched_fills.append((fill, "stop_loss", 0))
            elif (
                exit_order_id
                and fill.broker_order_id in exit_order_ids
                and fill.side == exit_transaction_type
            ):
                matched_fills.append(
                    (
                        fill,
                        trade_record.get("exit_reason")
                        if trade_record.get("exit_reason")
                        not in (None, "", "UNRECONCILED")
                        else "app_exit",
                        0,
                    )
                )
            elif (
                fill.side == exit_transaction_type
                and entry_time is not None
                and fill.exchange_time is not None
                and fill.exchange_time >= entry_time
            ):
                # Only unlinked allocation uses the temporal window. Exact
                # canonical linkage survives delayed/legacy bookkeeping time.
                matched_fills.append((fill, "manual_broker_exit", 1))

        if not matched_fills:
            return None, "UNRECONCILED", None, None

        # Exact order linkage is preferred, but it is not evidence that the
        # whole position was closed.  Additional same-identity reductions are
        # allocated only when their quantity and direction are verified.
        exact_matches = [f for f in matched_fills if f[2] == 0]
        fallback_matches = [f for f in matched_fills if f[2] != 0]
        matched_fills = sorted(
            exact_matches + fallback_matches,
            key=lambda item: (
                item[2],
                item[0].exchange_time
                or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            ),
        )

        target_qty = abs(trade_record.get("quantity", 0))
        if target_qty == 0:
            # Never manufacture the original quantity from whatever opposite
            # fills happen to be visible.  It is an allocation contract field.
            return None, "UNRECONCILED", None, None

        selected_fills = []
        total_qty = 0
        reasons = set()
        for fill, reason, _ in matched_fills:
            remaining = target_qty - total_qty
            if remaining <= 0:
                break
            qty = min(fill.quantity, remaining)
            selected_fills.append(
                fill if qty == fill.quantity else replace(fill, quantity=qty)
            )
            total_qty += qty
            reasons.add(reason)

        if total_qty == 0 or total_qty < target_qty:
            return None, "UNRECONCILED", None, None

        # Attribution priority is not chronology: an earlier manual partial
        # may be allocated after a linked final exit. Missing timestamps leave
        # closure/cooldown unresolved rather than inventing an execution time.
        exit_times = [fill.exchange_time for fill in selected_fills]
        if not all(exit_times):
            return None, "UNRECONCILED", None, None

        projection = accounting_service.project_fills(selected_fills)
        if projection.vwap is None:
            return None, "UNRECONCILED", None, None
        vwap = projection.vwap

        # Determine main reason
        if "stop_loss" in reasons:
            final_reason = "stop_loss"
        elif trade_record.get("exit_reason") and any(
            reason == trade_record.get("exit_reason") for reason in reasons
        ):
            final_reason = trade_record["exit_reason"]
        elif "app_exit" in reasons:
            final_reason = "app_exit"
        else:
            final_reason = "manual_broker_exit"

        exit_time = max(exit_times).isoformat()

        signal_exit_price = (
            trade_record.get("sl") if final_reason == "stop_loss" else None
        )
        cost_details = accounting_service.calculate_trade(
            direction=trade_record.get("direction", "BUY"),
            entry_price=trade_record.get("entry_price"),
            exit_price=vwap,
            quantity=total_qty,
            signal_entry_price=trade_record.get("signal_entry_price"),
            signal_exit_price=signal_exit_price,
            exchange=req_exchange or "NSE",
            product=req_product or "MIS",
        )
        if cost_details is None:
            return None, "UNRECONCILED", None, None

        # Apply verified entry/exit order-group fees if available
        if entry_fills and cost_details is not None:
            trade_fees = accounting_service.fees_for_fills(entry_fills + selected_fills)
            if trade_fees is not None:
                cost_details["brokerage"] = trade_fees.brokerage
                cost_details["taxes"] = (
                    trade_fees.stt + trade_fees.gst + trade_fees.stamp
                )
                cost_details["exchange_charges"] = (
                    trade_fees.exchange_txn + trade_fees.sebi
                )
                cost_details["other_fees"] = 0.0
                cost_details["net_pnl"] = (
                    cost_details.get("gross_pnl", 0.0) - trade_fees.total
                )
                cost_details["cost_model_version"] = trade_fees.cost_model_version
                cost_details["rounding_version"] = trade_fees.rounding_version
                cost_details["financial_quality"] = "RECONCILED"
                cost_details["financial_provenance"] = "order_grouped_broker_fills"
        elif cost_details is not None and selected_fills:
            # Entry order linkage may be absent after a broker retention window.
            # Project one explicitly labelled entry order so fees cannot vanish
            # between fast risk and journal repair.
            estimated_entry = replace(
                selected_fills[0],
                broker_order_id="ESTIMATED_ENTRY_ORDER",
                side=entry_direction,
                quantity=total_qty,
                fill_price=trade_record.get("entry_price"),
            )
            trade_fees = accounting_service.fees_for_fills(
                [estimated_entry] + selected_fills
            )
            if trade_fees is not None:
                cost_details["brokerage"] = trade_fees.brokerage
                cost_details["taxes"] = (
                    trade_fees.stt + trade_fees.gst + trade_fees.stamp
                )
                cost_details["exchange_charges"] = (
                    trade_fees.exchange_txn + trade_fees.sebi
                )
                cost_details["net_pnl"] = (
                    cost_details.get("gross_pnl", 0.0) - trade_fees.total
                )
                cost_details["cost_model_version"] = trade_fees.cost_model_version
                cost_details["rounding_version"] = trade_fees.rounding_version
                cost_details["financial_quality"] = "ESTIMATED"
                cost_details["financial_provenance"] = (
                    "order_grouped_projection_entry_linkage_unavailable"
                )

        return vwap, final_reason, exit_time, cost_details

    def _journal_external_close(self, symbol: str):
        """Book a journal close for a position closed outside the app-side exit
        path (broker-stop fill or a manual close in Kite)."""
        with self._trade_lock:
            trade = self.active_trades.get(symbol)
            if not trade:
                return False
            trade_id = trade.get("trade_id")
            trade_record = dict(trade)

        if not trade_id or trade.get("journal_entry_recorded") is False:
            return False

        if not self._persist_execution_linkage(trade):
            return False
        trade_record = dict(self.active_trades.get(symbol, trade))

        exit_price, reason, exit_time, cost_details = self._reconcile_execution(
            symbol, trade_record
        )

        if reason == "UNRECONCILED":
            self._push_log(
                f"Execution exact details not found for {symbol}. Marking as UNRECONCILED.",
                level="warning",
            )

        try:
            journal.close_trade(
                trade_id, exit_price, reason, exit_time, cost_details=cost_details
            )
            return reason != "UNRECONCILED"
        except Exception as e:
            self._push_log(
                f"Error closing trade in journal for {symbol}: {e}", level="error"
            )
            return False

    def _reconcile_journal_trades(self):
        """Periodically check journal OPEN or UNRECONCILED trades and fix them using broker executions."""
        try:
            open_positions = {
                p["tradingsymbol"]: p
                for p in self._positions()
                if p.get("quantity", 0) != 0
            }
        except Exception as e:
            self._push_log(
                f"Reconcile job: failed to fetch positions: {e}", level="warning"
            )
            return

        try:
            journal_trades = journal.get_trades()
        except Exception:
            return

        for t in journal_trades:
            if t["status"] == "OPEN" and t["tradingsymbol"] not in open_positions:
                # Ghost open position in journal
                self._push_log(
                    f"Reconcile job: Found ghost OPEN trade for {t['tradingsymbol']}. Attempting to reconcile."
                )
                exit_price, reason, exit_time, cost_details = self._reconcile_execution(
                    t["tradingsymbol"], dict(t)
                )
                if reason != "UNRECONCILED":
                    journal.close_trade(
                        t["id"],
                        exit_price,
                        reason,
                        exit_time,
                        cost_details=cost_details,
                    )
                    self._push_log(
                        f"Reconcile job: Closed {t['tradingsymbol']} at {exit_price} ({reason})"
                    )
                else:
                    journal.close_trade(t["id"], None, "UNRECONCILED", None)

            elif t["status"] == "RECONCILIATION_PENDING" or (
                t["status"] == "CLOSED"
                and (
                    t["exit_reason"] == "UNRECONCILED"
                    or t.get("financial_quality") != "RECONCILED"
                    or (t.get("entry_order_id") and not t.get("entry_time"))
                )
            ):
                # Try to find fills now
                exit_price, reason, exit_time, cost_details = self._reconcile_execution(
                    t["tradingsymbol"], dict(t)
                )
                if reason != "UNRECONCILED":
                    journal.update_trade_exit(
                        t["id"],
                        exit_price,
                        reason,
                        exit_time,
                        cost_details=cost_details,
                    )
                    self._push_log(
                        f"Reconcile job: Reconciled {t['tradingsymbol']} at {exit_price} ({reason})"
                    )


trading_engine = TradingEngine()
