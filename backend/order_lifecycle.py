"""Durable, broker-independent coordinator for order obligations.

This module deliberately does not contain trading policy.  It is the narrow
phase-2 safety boundary between a decision that needs an order and the existing
broker gateway.  Its two important guarantees are:

* an intent and unique broker tag are durable before a broker submission; and
* an ambiguous submission/cancel outcome blocks a replacement until broker
  facts identify the previous attempt and the current residual position.

The coordinator is usable from live, paper and replay adapters because callers
inject the broker mutations and reads.  It never treats an acknowledgement as a
fill or a terminal flat position.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from .broker_models import OrderRole, OrderSubmissionRejected, OrderSubmissionUnknown


class IntentType(str, Enum):
    ENTER = "ENTER"
    PROTECT = "PROTECT"
    TIGHTEN = "TIGHTEN"
    EXIT = "EXIT"
    FLATTEN = "FLATTEN"


class AttemptState(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


_ACTIVE_ATTEMPT_STATES = {
    AttemptState.PREPARED.value,
    AttemptState.SUBMITTING.value,
    AttemptState.ACKNOWLEDGED.value,
    AttemptState.WORKING.value,
    AttemptState.PARTIALLY_FILLED.value,
    AttemptState.CANCEL_PENDING.value,
    AttemptState.UNKNOWN.value,
}
_TERMINAL_ORDER_STATUSES = {
    "COMPLETE",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
    "REJECTED AMO",
}
_ANY_PREVIOUS_ATTEMPT = object()


@dataclass(frozen=True)
class SubmissionResult:
    intent_id: str
    attempt_id: Optional[str]
    attempt_tag: Optional[str]
    state: str
    broker_order_id: Optional[str] = None
    detail: Optional[str] = None

    @property
    def needs_reconciliation(self) -> bool:
        return self.state in {
            AttemptState.UNKNOWN.value,
            "HANDOFF_PENDING",
            "RECONCILE_REQUIRED",
        }


class OrderLifecycleCoordinator:
    """Serializes one reduction owner per broker-position key.

    The in-process lock only makes local callers pleasant to use.  The
    journal's partial unique index is the cross-restart/cross-thread authority,
    so no broker network operation needs to run under a global engine lock.
    """

    def __init__(self, trade_journal):
        self.journal = trade_journal
        self._locks_guard = threading.Lock()
        self._position_locks: dict[str, threading.RLock] = {}

    def _position_lock(self, position_key: str) -> threading.RLock:
        with self._locks_guard:
            return self._position_locks.setdefault(position_key, threading.RLock())

    @staticmethod
    def _new_attempt_tag() -> str:
        """Return a stable, Kite-compatible tag for one persisted attempt."""

        # Kite accepts a short free-form tag.  The UUID remains the durable
        # primary identity; this compact tag is for broker reconciliation.
        return f"ol{uuid.uuid4().hex[:18]}"

    @staticmethod
    def _as_role(role: OrderRole | str) -> str:
        if isinstance(role, OrderRole):
            return role.value
        return OrderRole(str(role).upper()).value

    def _ensure_intent(
        self,
        *,
        position_key: str,
        intent_type: IntentType | str,
        role: OrderRole | str,
        side: str,
        quantity: int,
        trade_id: Optional[str],
        reason: Optional[str],
        payload: dict[str, Any],
        latched: bool,
        existing_intent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        intent_type_value = (
            intent_type.value
            if isinstance(intent_type, IntentType)
            else IntentType(str(intent_type).upper()).value
        )
        if existing_intent_id:
            intent = self.journal.get_order_intent(existing_intent_id)
            if intent is None:
                raise ValueError(
                    f"unknown existing lifecycle intent {existing_intent_id}"
                )
            if intent["position_key"] != position_key:
                raise ValueError(
                    "existing lifecycle intent belongs to another position"
                )
            return intent

        # A repeated action is not a new obligation.  In particular, an
        # UNKNOWN attempt must retain its original tag and block a duplicate.
        active = self.journal.find_active_order_intent(
            position_key, {intent_type_value}
        )
        if active is not None:
            return active
        return self.journal.create_order_intent(
            intent_id=str(uuid.uuid4()),
            position_key=position_key,
            trade_id=trade_id,
            intent_type=intent_type_value,
            role=self._as_role(role),
            side=side,
            quantity=quantity,
            reason=reason,
            payload=payload,
            latched=latched,
        )

    def prepare_intent(self, **kwargs) -> dict[str, Any]:
        """Persist an obligation before entry/protection cancellation begins."""

        with self._position_lock(kwargs["position_key"]):
            return self._ensure_intent(**kwargs)

    def submit(
        self,
        *,
        position_key: str,
        intent_type: IntentType | str,
        role: OrderRole | str,
        side: str,
        quantity: int,
        payload: dict[str, Any],
        submit_order: Callable[[str], str],
        trade_id: Optional[str] = None,
        reason: Optional[str] = None,
        latched: bool = False,
        existing_intent_id: Optional[str] = None,
        expected_previous_attempt_id: Any = _ANY_PREVIOUS_ATTEMPT,
    ) -> SubmissionResult:
        """Persist one attempt, then submit exactly that attempt once.

        ``submit_order`` receives the durable attempt tag.  A timeout or other
        uncertain mutation does not cause a retry here; the result is UNKNOWN
        and later reconciliation must resolve it by that tag/order identity.
        """

        lock = self._position_lock(position_key)
        with lock:
            intent_type_value = (
                intent_type.value
                if isinstance(intent_type, IntentType)
                else str(intent_type).upper()
            )
            if intent_type_value in {"ENTER", "PROTECT", "TIGHTEN"}:
                reduction = self.journal.find_active_order_intent(
                    position_key, {IntentType.EXIT.value, IntentType.FLATTEN.value}
                )
                if reduction:
                    return SubmissionResult(
                        reduction["intent_id"],
                        None,
                        None,
                        "RECONCILE_REQUIRED",
                        detail="a latched reduction owns this position",
                    )
            intent = self._ensure_intent(
                position_key=position_key,
                intent_type=intent_type,
                role=role,
                side=side,
                quantity=quantity,
                trade_id=trade_id,
                reason=reason,
                payload=payload,
                latched=latched,
                existing_intent_id=existing_intent_id,
            )
            projection = self.journal.get_order_intent_projection(intent["intent_id"])
            latest = projection.get("latest_attempt") if projection else None
            if expected_previous_attempt_id is not _ANY_PREVIOUS_ATTEMPT and (
                (latest or {}).get("attempt_id") != expected_previous_attempt_id
            ):
                return SubmissionResult(
                    intent["intent_id"],
                    (latest or {}).get("attempt_id"),
                    (latest or {}).get("attempt_tag"),
                    "RECONCILE_REQUIRED",
                    (latest or {}).get("broker_order_id"),
                    "another reduction attempt superseded the handoff snapshot",
                )
            if latest and latest["state"] in _ACTIVE_ATTEMPT_STATES:
                return SubmissionResult(
                    intent_id=intent["intent_id"],
                    attempt_id=latest["attempt_id"],
                    attempt_tag=latest["attempt_tag"],
                    state="RECONCILE_REQUIRED",
                    broker_order_id=latest.get("broker_order_id"),
                    detail="a prior attempt is still active or unknown",
                )

            attempt_id = str(uuid.uuid4())
            attempt_tag = self._new_attempt_tag()
            attempt = self.journal.prepare_order_attempt(
                intent_id=intent["intent_id"],
                attempt_id=attempt_id,
                attempt_tag=attempt_tag,
                payload=payload,
                enforce_previous_attempt=(
                    expected_previous_attempt_id is not _ANY_PREVIOUS_ATTEMPT
                ),
                expected_previous_attempt_id=(
                    None
                    if expected_previous_attempt_id is _ANY_PREVIOUS_ATTEMPT
                    else expected_previous_attempt_id
                ),
            )
            # A separate process could have prepared an attempt while the
            # in-process lock was unavailable.  Never submit a fresh tag then.
            if attempt["attempt_id"] != attempt_id:
                return SubmissionResult(
                    intent_id=intent["intent_id"],
                    attempt_id=attempt["attempt_id"],
                    attempt_tag=attempt["attempt_tag"],
                    state="RECONCILE_REQUIRED",
                    broker_order_id=attempt.get("broker_order_id"),
                    detail="a persisted attempt already owns this intent",
                )

        # Broker I/O deliberately happens after the durable state transition
        # and outside the lock.  The persisted SUBMITTING attempt continues to
        # block a concurrent caller while this request is in flight.
        try:
            response = submit_order(attempt_tag)
            if (
                isinstance(response, bool)
                or response is None
                or not str(response).strip()
            ):
                raise OrderSubmissionUnknown("broker acknowledgement has no order ID")
            broker_order_id = str(response)
        except OrderSubmissionRejected as exc:
            self.journal.record_order_attempt_state(
                attempt_id,
                AttemptState.REJECTED.value,
                error=str(exc),
            )
            return SubmissionResult(
                intent_id=intent["intent_id"],
                attempt_id=attempt_id,
                attempt_tag=attempt_tag,
                state=AttemptState.REJECTED.value,
                detail=str(exc),
            )
        except Exception as exc:
            # A legacy adapter can throw an untyped transport exception.  It
            # is conservative to classify it as UNKNOWN; only the gateway may
            # assert a definite broker rejection.
            self.journal.record_order_attempt_state(
                attempt_id,
                AttemptState.UNKNOWN.value,
                error=str(exc),
            )
            return SubmissionResult(
                intent_id=intent["intent_id"],
                attempt_id=attempt_id,
                attempt_tag=attempt_tag,
                state=AttemptState.UNKNOWN.value,
                detail=str(exc),
            )

        self.journal.record_order_attempt_state(
            attempt_id,
            AttemptState.ACKNOWLEDGED.value,
            broker_order_id=broker_order_id,
        )
        return SubmissionResult(
            intent_id=intent["intent_id"],
            attempt_id=attempt_id,
            attempt_tag=attempt_tag,
            state=AttemptState.ACKNOWLEDGED.value,
            broker_order_id=broker_order_id,
        )

    def observe_order(
        self, intent_id: str, order: Optional[dict[str, Any]]
    ) -> SubmissionResult:
        """Consume one current order-book fact without guessing from absence."""

        projection = self.journal.get_order_intent_projection(intent_id)
        if projection is None:
            raise ValueError(f"unknown lifecycle intent {intent_id}")
        attempt = projection.get("latest_attempt")
        if attempt is None:
            return SubmissionResult(intent_id, None, None, "RECONCILE_REQUIRED")
        if order is None:
            self.journal.record_order_intent_event(
                intent_id,
                "order_snapshot_unavailable",
                {"attempt_tag": attempt["attempt_tag"]},
                attempt["attempt_id"],
            )
            return SubmissionResult(
                intent_id,
                attempt["attempt_id"],
                attempt["attempt_tag"],
                "RECONCILE_REQUIRED",
                attempt.get("broker_order_id"),
            )
        if not order:
            # Absence never disproves an acknowledged or in-flight mutation.
            return SubmissionResult(
                intent_id,
                attempt["attempt_id"],
                attempt["attempt_tag"],
                "RECONCILE_REQUIRED",
                attempt.get("broker_order_id"),
            )

        broker_order_id = order.get("order_id") or order.get("orderId")
        observed_tag = order.get("tag")
        identity_parts = str(projection["position_key"]).split(":", 6)
        identity_fields = (
            "namespace",
            "account_id",
            "exchange",
            "instrument_token",
            "tradingsymbol",
            "product",
        )
        identity_mismatch = len(identity_parts) < 6 or any(
            order.get(field) not in (None, "")
            and str(getattr(order[field], "value", order[field])) != expected
            for field, expected in zip(identity_fields, identity_parts[:6])
        )
        if order.get("transaction_type") not in (None, "", projection["side"]):
            identity_mismatch = True
        if (
            identity_mismatch
            or not broker_order_id
            or (
                attempt.get("broker_order_id")
                and str(broker_order_id) != str(attempt["broker_order_id"])
            )
            or (observed_tag and observed_tag != attempt["attempt_tag"])
            or (
                not attempt.get("broker_order_id")
                and observed_tag != attempt["attempt_tag"]
            )
        ):
            return SubmissionResult(
                intent_id,
                attempt["attempt_id"],
                attempt["attempt_tag"],
                "RECONCILE_REQUIRED",
                attempt.get("broker_order_id"),
                "order identity does not match the current attempt",
            )

        status = str(order.get("status", "")).upper()
        filled = int(order.get("filled_quantity", 0) or 0)
        if status == "COMPLETE":
            state = AttemptState.FILLED.value if filled else AttemptState.REJECTED.value
        elif status in {"CANCELLED", "EXPIRED", "REJECTED AMO"}:
            state = AttemptState.CANCELLED.value
        elif status == "REJECTED":
            state = AttemptState.REJECTED.value
        elif filled > 0:
            state = AttemptState.PARTIALLY_FILLED.value
        else:
            state = AttemptState.WORKING.value
        terminal_states = {
            AttemptState.FILLED.value,
            AttemptState.CANCELLED.value,
            AttemptState.REJECTED.value,
        }
        if attempt["state"] in terminal_states and state != attempt["state"]:
            # Delayed working/order callbacks cannot revive a terminal order.
            return SubmissionResult(
                intent_id,
                attempt["attempt_id"],
                attempt["attempt_tag"],
                attempt["state"],
                attempt.get("broker_order_id"),
                "ignored observation after a terminal broker fact",
            )
        self.journal.record_order_attempt_state(
            attempt["attempt_id"],
            state,
            broker_order_id=broker_order_id,
            details={"order": dict(order)},
        )
        return SubmissionResult(
            intent_id,
            attempt["attempt_id"],
            attempt["attempt_tag"],
            state,
            str(broker_order_id) if broker_order_id else attempt.get("broker_order_id"),
        )

    def record_fill(
        self,
        *,
        position_key: str,
        broker_fill_id: str,
        broker_order_id: str,
        side: str,
        quantity: int,
        fill_price: Optional[float],
        exchange_time: Any = None,
        raw_fill: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Write a fill once and associate it with its durable attempt when known."""

        attempt = self.journal.get_order_attempt_by_broker_order(
            broker_order_id, position_key=position_key
        )
        return self.journal.record_order_fill(
            broker_fill_id=broker_fill_id,
            broker_order_id=broker_order_id,
            position_key=position_key,
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            exchange_time=exchange_time,
            intent_id=attempt.get("intent_id") if attempt else None,
            attempt_id=attempt.get("attempt_id") if attempt else None,
            raw_fill=raw_fill,
        )

    def handoff_protection_and_submit_reduction(
        self,
        *,
        position_key: str,
        role: OrderRole | str,
        side: str,
        requested_quantity: int,
        payload: dict[str, Any],
        stop_order_id: Optional[str],
        cancel_stop: Callable[[str], Any],
        read_order: Callable[[str], Optional[dict[str, Any]]],
        read_residual: Callable[[Optional[dict[str, Any]]], Optional[dict[str, Any]]],
        submit_order: Callable[[str, dict[str, Any]], str],
        trade_id: Optional[str] = None,
        reason: Optional[str] = None,
        hard: bool = False,
        existing_intent_id: Optional[str] = None,
    ) -> SubmissionResult:
        """Cancel-confirm-reread handoff for a non-atomic broker.

        A stop that is COMPLETE is processed as a possible reducing fill.  The
        residual reader receives the terminal stop observation and must compare
        its fills with the current position, whose endpoint may still lag.
        The newly submitted order uses only that reconciled residual quantity.
        """

        intent_type = IntentType.FLATTEN if hard else IntentType.EXIT
        intent = self.prepare_intent(
            position_key=position_key,
            intent_type=intent_type,
            role=role,
            side=side,
            quantity=requested_quantity,
            trade_id=trade_id,
            reason=reason,
            payload=payload,
            latched=True,
            existing_intent_id=existing_intent_id,
        )
        intent_id = intent["intent_id"]
        existing = self.journal.get_order_intent_projection(intent_id)
        latest = existing.get("latest_attempt") if existing else None
        if latest and latest["state"] in _ACTIVE_ATTEMPT_STATES:
            return SubmissionResult(
                intent_id,
                latest["attempt_id"],
                latest["attempt_tag"],
                "RECONCILE_REQUIRED",
                latest.get("broker_order_id"),
                "existing reduction attempt remains active",
            )

        protection = self.journal.find_active_order_intent(
            position_key, {IntentType.PROTECT.value, IntentType.TIGHTEN.value}
        )
        if protection:
            projection = self.journal.get_order_intent_projection(
                protection["intent_id"]
            )
            attempt = (projection or {}).get("latest_attempt") or {}
            if attempt.get("state") in _ACTIVE_ATTEMPT_STATES:
                protection_order_id = attempt.get("broker_order_id")
                if not protection_order_id or (
                    stop_order_id and str(protection_order_id) != str(stop_order_id)
                ):
                    return SubmissionResult(
                        intent_id,
                        None,
                        None,
                        "HANDOFF_PENDING",
                        detail="protective submission must be reconciled before reduction",
                    )
                stop_order_id = str(protection_order_id)

        stop = None
        if stop_order_id:
            self.journal.record_order_intent_event(
                intent_id,
                "protection_handoff_started",
                {"stop_order_id": stop_order_id},
            )
            stop = read_order(stop_order_id)
            if not stop or str(stop.get("status", "")).upper() not in (
                _TERMINAL_ORDER_STATUSES
            ):
                try:
                    cancel_stop(stop_order_id)
                except Exception as exc:
                    self.journal.record_order_intent_event(
                        intent_id,
                        "protection_cancel_unknown",
                        {"stop_order_id": stop_order_id, "error": str(exc)},
                    )
                # A rejected cancellation can mean the stop already filled or
                # a prior cancellation succeeded. Resolve its broker state
                # even when the mutation raised; do not recancel forever.
                stop = read_order(stop_order_id)
            if stop is None:
                self.journal.record_order_intent_event(
                    intent_id,
                    "protection_cancel_unverified",
                    {"stop_order_id": stop_order_id},
                )
                return SubmissionResult(
                    intent_id,
                    None,
                    None,
                    "HANDOFF_PENDING",
                    detail="current order snapshot is unavailable",
                )
            if (
                not stop
                or str(stop.get("status", "")).upper() not in _TERMINAL_ORDER_STATUSES
            ):
                self.journal.record_order_intent_event(
                    intent_id,
                    "protection_still_live",
                    {
                        "stop_order_id": stop_order_id,
                        "status": (stop or {}).get("status"),
                    },
                )
                return SubmissionResult(
                    intent_id,
                    None,
                    None,
                    "HANDOFF_PENDING",
                    detail="protective stop is still live or absent from a partial read",
                )
            self.journal.record_order_intent_event(
                intent_id,
                "protection_handoff_terminal",
                {
                    "stop_order_id": stop_order_id,
                    "status": stop.get("status"),
                    "filled_quantity": stop.get("filled_quantity", 0),
                },
            )

        residual = read_residual(stop)
        if residual is None:
            self.journal.record_order_intent_event(
                intent_id, "residual_snapshot_unavailable", {}
            )
            return SubmissionResult(
                intent_id, None, None, "RECONCILE_REQUIRED", detail="residual unknown"
            )
        if not residual or not int(residual.get("quantity", 0) or 0):
            self.journal.record_order_intent_event(intent_id, "residual_flat", {})
            return SubmissionResult(
                intent_id, None, None, "FLAT_PENDING_RECONCILIATION"
            )

        if (int(residual["quantity"]) > 0) != (intent["side"] == "SELL"):
            self.journal.record_order_intent_event(
                intent_id,
                "residual_side_disagreement",
                {"quantity": int(residual["quantity"]), "intent_side": intent["side"]},
            )
            return SubmissionResult(
                intent_id,
                None,
                None,
                "RECONCILE_REQUIRED",
                detail="position side changed",
            )

        residual_quantity = abs(int(residual["quantity"]))
        reduction_payload = dict(payload)
        # Preserve the fresh broker facts used to size the residual order so
        # the submitter need not fall back to its pre-handoff snapshot.
        reduction_payload.update(
            {
                field: residual[field]
                for field in ("exchange", "product", "last_price")
                if residual.get(field) not in (None, "")
            }
        )
        reduction_payload["quantity"] = residual_quantity
        reduction_payload["transaction_type"] = (
            "SELL" if int(residual["quantity"]) > 0 else "BUY"
        )
        return self.submit(
            position_key=position_key,
            intent_type=intent_type,
            role=role,
            side=reduction_payload["transaction_type"],
            quantity=requested_quantity,
            payload=reduction_payload,
            submit_order=lambda tag: submit_order(tag, reduction_payload),
            trade_id=trade_id,
            reason=reason,
            latched=True,
            existing_intent_id=intent_id,
            expected_previous_attempt_id=(latest or {}).get("attempt_id"),
        )
