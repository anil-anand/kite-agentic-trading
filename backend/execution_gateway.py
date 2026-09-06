import datetime
import threading

from .config import config_manager
from .journal import journal
from .kite_client import kite_client
from .risk_manager import risk_manager
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

        # We need to distinguish between entry orders and protective/exit orders.
        is_entry = kwargs.pop("is_entry", False)

        # 1. Evaluate general trading eligibility
        # Wait, if it is an exit or a stop loss being placed, we shouldn't block it
        # if max daily loss is reached. We actually *want* exits to happen.
        # But wait, `can_trade` returns False if max daily loss is hit.
        # So if we are placing a protective stop or an exit limit, we must NOT block it.
        if is_entry:
            can_trade, reason = risk_manager.can_trade()
            if not can_trade:
                push_log(
                    f"ExecutionGateway rejected entry order for {symbol}: {reason}",
                    level="warning",
                )
                raise Exception(f"Risk check failed: {reason}")

            risk_config = config_manager.get_risk_config()
            max_daily_trades = risk_config.get("maxDailyTrades", 10)
            max_symbol_trades = risk_config.get("maxTradesPerSymbolPerDay", 2)
            cooldown_mins = risk_config.get("tradeCooldownMins", 15)

            todays_counts = journal.get_todays_trade_counts()
            if todays_counts["total"] >= max_daily_trades:
                msg = f"Max daily trades ({max_daily_trades}) reached."
                push_log(
                    f"ExecutionGateway rejected entry for {symbol}: {msg}",
                    level="warning",
                )
                raise Exception(msg)

            if todays_counts["by_symbol"].get(symbol, 0) >= max_symbol_trades:
                msg = f"Max trades per symbol ({max_symbol_trades}) reached today for {symbol}."
                push_log(
                    f"ExecutionGateway rejected entry for {symbol}: {msg}",
                    level="warning",
                )
                raise Exception(msg)

            last_exit = journal.get_last_exit_time(symbol)
            if last_exit:
                mins_since_exit = (
                    datetime.datetime.now() - last_exit
                ).total_seconds() / 60
                if mins_since_exit < cooldown_mins:
                    msg = f"Cooldown period active ({mins_since_exit:.1f}/{cooldown_mins} mins)."
                    push_log(
                        f"ExecutionGateway rejected entry for {symbol}: {msg}",
                        level="warning",
                    )
                    raise Exception(msg)

            with self._gateway_lock:
                if symbol in self._pending_entries:
                    msg = f"Duplicate entry prevention: {symbol} is already pending."
                    push_log(msg, level="warning")
                    raise Exception(msg)
                self._pending_entries.add(symbol)

        try:
            order_id = kite_client.place_order(**kwargs)
            push_log(f"ExecutionGateway: Order {order_id} placed for {symbol}")
            return order_id
        except Exception as e:
            push_log(f"ExecutionGateway: Order failed for {symbol}: {e}", level="error")
            raise e
        finally:
            if is_entry:
                with self._gateway_lock:
                    self._pending_entries.discard(symbol)

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
            order_id = kite_client.place_order(**kwargs)
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
