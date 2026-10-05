"""Durable operator reductions independent of the strategy's symbol index."""

import uuid

from .broker_models import OrderRole
from .config import config_manager
from .order_lifecycle import IntentType
from .reduction_policy import ReductionOrderPolicy
from .time_utils import now_utc


class ScopedReductions:
    def __init__(self, engine, *, submit_order, current_scope):
        self.engine = engine
        self.submit_order = submit_order
        self.current_scope = current_scope
        self.owners = {}

    def restore(self, intent):
        owner = dict(intent["payload"]["recovery_trade"])
        owner["exit_intent_id"] = intent["intent_id"]
        key = intent["position_key"].rsplit(":", 1)[0]
        with self.engine._trade_lock:
            current = self.owners.get(key)
            if current and current["position_epoch"] != owner["position_epoch"]:
                raise ValueError("conflicting reduction epochs for one broker position")
            self.owners[key] = owner

    def dispatch(self, position, reason):
        engine = self.engine
        if (
            position.get("namespace"),
            position.get("account_id"),
        ) != self.current_scope():
            return
        key = position["position_key"]
        lock = engine._management_lock(key)
        if not lock.acquire(blocking=False):
            return
        try:
            self._dispatch_owned(position, reason)
        except Exception as exc:
            engine._lifecycle_recovery_pending = True
            engine._push_log(
                f"Scoped reduction for {key} awaits reconciliation: {exc}",
                level="warning",
            )
        finally:
            lock.release()

    def _dispatch_owned(self, position, reason):
        engine = self.engine
        coordinator = engine._order_lifecycle
        key = position["position_key"]
        symbol = position["tradingsymbol"]
        owner = self.owners.get(key)
        if owner is None:
            # A critical worker may run before startup's general restoration.
            # Recover the durable epoch before creating any new intent.
            existing = [
                intent
                for intent in coordinator.journal.list_unresolved_order_intents()
                if intent["position_key"].rsplit(":", 1)[0] == key
            ]
            if existing:
                if len(existing) != 1 or not existing[0]["payload"].get(
                    "scoped_reduction"
                ):
                    raise ValueError("another durable owner must be reconciled first")
                self.restore(existing[0])
                owner = self.owners[key]
        if owner is None:
            owner = {
                **{
                    field: position[field]
                    for field in (
                        "namespace",
                        "account_id",
                        "exchange",
                        "product",
                        "tradingsymbol",
                    )
                },
                "instrument_id": str(position["instrument_token"]),
                "position_epoch": f"reduction-{uuid.uuid4()}",
                "direction": "BUY" if position["quantity"] > 0 else "SELL",
                "quantity": abs(position["quantity"]),
                "entry_state": "OPEN",
                "exit_reason": reason,
            }
            epoch_key = engine._trade_position_key(symbol, owner)
            intent = coordinator.latch_reduction_intent(
                position_key=epoch_key,
                intent_type=IntentType.FLATTEN,
                role=OrderRole.REDUCTION,
                side="SELL" if position["quantity"] > 0 else "BUY",
                quantity=abs(position["quantity"]),
                payload={"scoped_reduction": True, "recovery_trade": owner},
                trade_id=None,
                reason=reason,
            )
            owner["exit_intent_id"] = intent["intent_id"]
            with engine._trade_lock:
                self.owners[key] = owner
        if not engine._trade_matches_position(owner, position):
            return
        epoch_key = engine._trade_position_key(symbol, owner)
        intent_id = owner["exit_intent_id"]
        orders = engine._orders()
        engine._reconcile_durable_attempts(orders)
        owned_ids = engine._owned_lifecycle_order_ids(epoch_key, owner)
        for order in orders:
            if order.get("position_key") != key:
                continue
            order_id = str(order["order_id"])
            if order_id in owned_ids:
                continue
            if order["status"] in engine._TERMINAL_ORDER_STATUSES:
                continue
            owner.setdefault("external_handoff_baselines", {}).setdefault(
                order_id, int(order.get("filled_quantity", 0))
            )
            owner.setdefault("external_handoff_orders", {})[order_id] = order
            coordinator.journal.record_external_handoff(intent_id, owner)
            terminal = engine._cancel_order_terminal(order_id)
            if not terminal:
                return
            owner["external_handoff_orders"][order_id] = terminal
            coordinator.journal.record_external_handoff(intent_id, owner)

        projection = engine._lifecycle_projection(intent_id)
        latest = (projection or {}).get("latest_attempt") or {}
        order_id = latest.get("broker_order_id")
        if order_id:
            order = engine._find_order(order_id)
            if not order:
                return
            coordinator.observe_order(intent_id, order)
            # A flat position still requires cancellation of a working reducer.
            if order["status"] not in engine._TERMINAL_ORDER_STATUSES and (
                not position["quantity"]
                or ReductionOrderPolicy(
                    working_timeout_seconds=float(
                        config_manager.get_order_lifecycle_config().get(
                            "workingAttemptTimeoutSeconds", 15
                        )
                    )
                ).cancellation_due(
                    order_type=order["order_type"],
                    submitted_at=latest.get("created_at"),
                    now=now_utc(),
                    market_required=True,
                )
            ):
                terminal = engine._cancel_order_terminal(order_id)
                if not terminal:
                    return
                coordinator.observe_order(intent_id, terminal)

        result = coordinator.handoff_protection_and_submit_reduction(
            position_key=epoch_key,
            role=OrderRole.REDUCTION,
            side="SELL" if owner["direction"] == "BUY" else "BUY",
            requested_quantity=owner["quantity"],
            payload={"scoped_reduction": True, "recovery_trade": owner},
            stop_order_id=None,
            cancel_stop=engine._cancel_order_terminal,
            read_order=engine._find_order,
            read_residual=lambda observed: engine._find_reconciled_residual(
                symbol, owner, observed, critical=True
            ),
            submit_order=lambda tag, fresh: self._submit(tag, fresh, owner),
            hard=True,
            reason=owner["exit_reason"],
            existing_intent_id=intent_id,
        )
        if result.state == "FLAT_PENDING_RECONCILIATION":
            coordinator.journal.complete_order_intent(
                intent_id, state="FLAT_ORDER_CLEAN"
            )
            with engine._trade_lock:
                self.owners.pop(key, None)

    def _submit(self, tag, fresh, owner):
        return self.submit_order(
            variety="regular",
            exchange=owner["exchange"],
            product=owner["product"],
            tradingsymbol=owner["tradingsymbol"],
            transaction_type=fresh["transaction_type"],
            quantity=fresh["quantity"],
            order_type="MARKET",
            order_role=OrderRole.REDUCTION,
            critical=True,
            attempt_tag=tag,
        )

    def sync(self, positions):
        by_key = {p["position_key"]: p for p in positions}
        with self.engine._trade_lock:
            owners = list(self.owners.items())
        for key, owner in owners:
            position = by_key.get(key) or {
                **owner,
                "instrument_token": owner["instrument_id"],
                "position_key": key,
                "quantity": 0,
            }
            self.dispatch(position, owner["exit_reason"])
