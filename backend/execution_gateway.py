import threading

from .broker_models import OrderRole, OrderSubmissionUnknown
from .config import config_manager
from .journal import journal
from .kite_client import kite_client
from .risk_manager import risk_manager
from .time_utils import as_utc, now_utc
from .utils import push_log


class ExecutionGateway:
    def __init__(self):
        # We move the pending_entries lock here to serialize order placements and prevent duplicates
        self._gateway_lock = threading.RLock()
        self._pending_entries: set = set()

    def place_order(self, **kwargs):
        """
        The single path for placing orders.
        Applies risk checks and handles duplicate entry prevention.
        """
        symbol = kwargs.get("tradingsymbol")
        is_entry = kwargs.pop("is_entry", False)
        reservation_id = kwargs.pop("entry_reservation_id", None)
        requested_role = kwargs.pop(
            "order_role", OrderRole.ENTRY if is_entry else OrderRole.UNKNOWN
        )

        if not is_entry:
            return self._submit_order(symbol, requested_role, kwargs)

        broker_call_started = False
        with self._gateway_lock:
            try:
                if symbol in self._pending_entries:
                    raise Exception(
                        f"Duplicate entry prevention: {symbol} is already pending."
                    )
                self._pending_entries.add(symbol)

                if not reservation_id or not risk_manager.validate_entry_reservation(
                    reservation_id=reservation_id,
                    symbol=symbol,
                    direction=kwargs.get("transaction_type"),
                    quantity=kwargs.get("quantity"),
                    price=kwargs.get("price"),
                ):
                    raise Exception(
                        "Entry requires a valid server-side risk reservation"
                    )

                can_trade, reason = risk_manager.can_trade()
                if not can_trade:
                    raise Exception(f"Risk check failed: {reason}")

                risk_config = config_manager.get_risk_config()
                max_daily_trades = risk_config.get("maxDailyTrades", 10)
                max_symbol_trades = risk_config.get("maxTradesPerSymbolPerDay", 2)
                cooldown_mins = risk_config.get("tradeCooldownMins", 15)
                todays_counts = journal.get_todays_trade_counts()
                if todays_counts["total"] >= max_daily_trades:
                    raise Exception(f"Max daily trades ({max_daily_trades}) reached.")
                if todays_counts["by_symbol"].get(symbol, 0) >= max_symbol_trades:
                    raise Exception(
                        f"Max trades per symbol ({max_symbol_trades}) reached "
                        f"today for {symbol}."
                    )

                last_exit = journal.get_last_exit_time(symbol)
                if last_exit:
                    normalized_last_exit = as_utc(last_exit)
                    mins_since_exit = (
                        now_utc() - normalized_last_exit
                    ).total_seconds() / 60
                    if mins_since_exit < cooldown_mins:
                        raise Exception(
                            f"Cooldown period active "
                            f"({mins_since_exit:.1f}/{cooldown_mins} mins)."
                        )

                broker_call_started = True
                order_id = self._submit_order(symbol, OrderRole.ENTRY, kwargs)
                risk_manager.bind_entry_order(reservation_id, order_id)
                return order_id
            except Exception as exc:
                if not broker_call_started:
                    risk_manager.release_entry_reservation(reservation_id)
                push_log(
                    f"ExecutionGateway rejected/failed entry for {symbol}: {exc}",
                    level="warning" if not broker_call_started else "error",
                )
                if broker_call_started:
                    raise OrderSubmissionUnknown(
                        f"Entry submission outcome is unknown for {symbol}: {exc}"
                    ) from exc
                raise
            finally:
                self._pending_entries.discard(symbol)

    def _submit_order(self, symbol, order_role, kwargs):
        try:
            order_id = kite_client.place_order(order_role=order_role, **kwargs)
            push_log(f"ExecutionGateway: Order {order_id} placed for {symbol}")
            return order_id
        except Exception as exc:
            push_log(
                f"ExecutionGateway: Order failed for {symbol}: {exc}", level="error"
            )
            raise

    def modify_order(self, **kwargs):
        """
        The single path for modifying orders.
        """
        order_id = kwargs.get("order_id")
        try:
            res = kite_client.modify_order(**kwargs)
            push_log(f"ExecutionGateway: Order {order_id} modified")
            return res
        except Exception as e:
            push_log(
                f"ExecutionGateway: Order modify failed for {order_id}: {e}",
                level="error",
            )
            raise e

    def cancel_order(self, **kwargs):
        """
        The single path for cancelling orders.
        """
        order_id = kwargs.get("order_id")
        try:
            res = kite_client.cancel_order(**kwargs)
            push_log(f"ExecutionGateway: Order {order_id} cancelled")
            return res
        except Exception as e:
            push_log(
                f"ExecutionGateway: Order cancel failed for {order_id}: {e}",
                level="error",
            )
            raise e

    def emergency_flatten_position(self, **kwargs):
        """
        Bypasses normal pre-trade limits (max trades, daily loss limits)
        to ensure we can exit a position in an emergency.
        Still logs the action.
        """
        symbol = kwargs.get("tradingsymbol")
        push_log(
            f"ExecutionGateway: Emergency flattening position for {symbol}",
            level="warning",
        )

        try:
            order_id = kite_client.place_order(order_role=OrderRole.REDUCTION, **kwargs)
            push_log(
                f"ExecutionGateway: Emergency order {order_id} placed for {symbol}"
            )
            return order_id
        except Exception as e:
            push_log(
                f"ExecutionGateway: Emergency order failed for {symbol}: {e}",
                level="error",
            )
            raise e


execution_gateway = ExecutionGateway()
