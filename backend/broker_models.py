"""Canonical broker contracts and explicit renderer DTO serialization.

The Kite SDK uses snake_case while the renderer uses camelCase.  Backend domain
code must never depend on the renderer representation, so SDK payloads are
normalized here exactly once and converted back only at the RPC boundary.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

BROKER_CONTRACT_VERSION = "broker-contract-v1"
_BROKER_TIMEZONE = ZoneInfo("Asia/Kolkata")


class SnapshotQuality(str, Enum):
    """Whether a broker read can be used as current truth."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ExecutionNamespace(str, Enum):
    LIVE = "LIVE"
    DEV = "DEV"
    PAPER = "PAPER"
    REPLAY = "REPLAY"


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    PROTECTION = "PROTECTION"
    REDUCTION = "REDUCTION"
    UNKNOWN = "UNKNOWN"


class BrokerContractError(ValueError):
    """Raised when an individual broker record cannot be normalized."""


class BrokerDataUnavailable(RuntimeError):
    """Raised when a caller needs a complete snapshot but one is unavailable."""


class OrderSubmissionUnknown(RuntimeError):
    """The broker call failed after submission may have reached the broker."""


class OrderSubmissionRejected(RuntimeError):
    """The broker definitively rejected a mutation before accepting an order."""


@dataclass(frozen=True)
class BrokerPositionKey:
    namespace: ExecutionNamespace
    account_id: str
    exchange: str
    instrument_id: str
    tradingsymbol: str
    product: str

    def as_string(self) -> str:
        return ":".join(
            (
                self.namespace.value,
                self.account_id,
                self.exchange,
                self.instrument_id,
                self.tradingsymbol,
                self.product,
            )
        )


@dataclass(frozen=True)
class BrokerPosition:
    key: BrokerPositionKey
    signed_quantity: int
    average_price: Optional[float]
    last_price: Optional[float]
    realised_gross: Optional[float]
    unrealised_gross: Optional[float]
    pnl: Optional[float]
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_price: Optional[float] = None
    sell_price: Optional[float] = None
    buy_value: Optional[float] = None
    sell_value: Optional[float] = None
    overnight_quantity: int = 0
    day_buy_quantity: int = 0
    day_sell_quantity: int = 0
    close_price: Optional[float] = None
    multiplier: float = 1.0
    mark_time: Optional[datetime] = None

    @property
    def direction(self) -> Optional[str]:
        if self.signed_quantity > 0:
            return "BUY"
        if self.signed_quantity < 0:
            return "SELL"
        return None

    @property
    def marked_notional(self) -> Optional[float]:
        if self.signed_quantity == 0:
            return 0.0
        if self.last_price is None:
            return None
        return abs(self.signed_quantity) * self.last_price * self.multiplier


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    key: BrokerPositionKey
    side: str
    order_type: str
    original_quantity: int
    filled_quantity: int
    remaining_quantity: int
    price: Optional[float]
    average_fill_price: Optional[float]
    trigger_price: Optional[float]
    status: str
    variety: str
    validity: Optional[str]
    status_message: Optional[str]
    client_intent_tag: Optional[str]
    role: OrderRole
    exchange_time: Optional[datetime]
    order_time: Optional[datetime]
    received_at: datetime
    is_archived: bool = False

    @property
    def is_app_order(self) -> bool:
        return self.role is not OrderRole.UNKNOWN

    @property
    def is_working(self) -> bool:
        # Kite has added request/validation states over time.  Only the
        # documented terminal states are safe to classify as not working;
        # an unfamiliar status must remain conservative until reconciled.
        return self.status not in {
            "COMPLETE",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "REJECTED AMO",
        }

    @property
    def risk_reference_price(self) -> Optional[float]:
        """Best known price for unfilled exposure, without inventing a zero."""

        return self.price or self.trigger_price or self.average_fill_price


@dataclass(frozen=True)
class BrokerFill:
    broker_fill_id: str
    broker_order_id: str
    key: BrokerPositionKey
    side: str
    quantity: int
    fill_price: Optional[float]
    exchange_time: Optional[datetime]
    received_at: datetime

    @property
    def turnover(self) -> Optional[float]:
        if self.fill_price is None:
            return None
        return self.fill_price * self.quantity


@dataclass(frozen=True)
class PositionSnapshot:
    net: tuple[BrokerPosition, ...]
    day: tuple[BrokerPosition, ...]
    quality: SnapshotQuality
    fetched_at: datetime
    errors: tuple[str, ...] = ()
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def require_complete(self) -> PositionSnapshot:
        if self.quality is not SnapshotQuality.COMPLETE:
            raise BrokerDataUnavailable(
                f"Position snapshot is {self.quality.value}: {', '.join(self.errors)}"
            )
        return self


@dataclass(frozen=True)
class OrderSnapshot:
    orders: tuple[BrokerOrder, ...]
    quality: SnapshotQuality
    fetched_at: datetime
    errors: tuple[str, ...] = ()
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def require_complete(self) -> OrderSnapshot:
        if self.quality is not SnapshotQuality.COMPLETE:
            raise BrokerDataUnavailable(
                f"Order snapshot is {self.quality.value}: {', '.join(self.errors)}"
            )
        return self


@dataclass(frozen=True)
class FillSnapshot:
    fills: tuple[BrokerFill, ...]
    quality: SnapshotQuality
    fetched_at: datetime
    errors: tuple[str, ...] = ()
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def require_complete(self) -> FillSnapshot:
        if self.quality is not SnapshotQuality.COMPLETE:
            raise BrokerDataUnavailable(
                f"Fill snapshot is {self.quality.value}: {', '.join(self.errors)}"
            )
        return self


@dataclass(frozen=True)
class BrokerSnapshot:
    namespace: ExecutionNamespace
    account_id: str
    positions: tuple[BrokerPosition, ...]
    day_positions: tuple[BrokerPosition, ...]
    current_orders: tuple[BrokerOrder, ...]
    fills: tuple[BrokerFill, ...]
    positions_quality: SnapshotQuality
    orders_quality: SnapshotQuality
    fills_quality: SnapshotQuality
    fetched_at: datetime
    errors: tuple[str, ...] = ()
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    positions_fetched_at: Optional[datetime] = None
    orders_fetched_at: Optional[datetime] = None
    fills_fetched_at: Optional[datetime] = None
    max_age_seconds: int = 120
    max_read_skew_seconds: int = 5

    @property
    def entry_ready(self) -> bool:
        """Whether this snapshot is safe for a new risk-increasing order.

        ``COMPLETE`` describes parsing, not freshness or read consistency.  A
        snapshot that is old, belongs to an unverified account, or was read
        with excessive skew is deliberately not admission-authoritative.
        """

        if self.account_id in {"", "UNKNOWN", None}:
            return False
        if any(
            quality is not SnapshotQuality.COMPLETE
            for quality in (
                self.positions_quality,
                self.orders_quality,
                self.fills_quality,
            )
        ):
            return False
        now = utc_now()
        source_times = [
            _as_utc(item)
            for item in (
                self.positions_fetched_at,
                self.orders_fetched_at,
                self.fills_fetched_at,
            )
            if item is not None
        ]
        if not source_times:
            source_times = [self.fetched_at]
        ages = [(now - _as_utc(item)).total_seconds() for item in source_times]
        if any(age < -5 or age > self.max_age_seconds for age in ages):
            return False
        if max(source_times) - min(source_times) > timedelta(
            seconds=self.max_read_skew_seconds
        ):
            return False
        # A non-zero position needs a timestamped market mark.  ``fetched_at``
        # describes when the HTTP response arrived, not when the price was
        # observed at the exchange; using it as a mark timestamp made an
        # unknown/illiquid mark appear fresh and could admit new risk.
        for position in self.positions:
            if position.signed_quantity:
                if position.last_price is None or position.mark_time is None:
                    return False
                mark_time = position.mark_time
                mark_age = (now - _as_utc(mark_time)).total_seconds()
                if mark_age < -5 or mark_age > self.max_age_seconds:
                    return False
        return True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_BROKER_TIMEZONE).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _value(payload: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return default


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise BrokerContractError(f"{field_name} is required")
        return ""
    result = str(value).strip()
    if required and not result:
        raise BrokerContractError(f"{field_name} is required")
    return result


def _quantity(
    value: Any, field_name: str, *, default: int = 0, required: bool = False
) -> int:
    if value is None:
        if required:
            raise BrokerContractError(f"{field_name} is required")
        return default
    if isinstance(value, bool):
        raise BrokerContractError(f"{field_name} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrokerContractError(f"{field_name} must be an integer") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise BrokerContractError(f"{field_name} must be an integer")
    return int(number)


def _number(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
    zero_is_unknown: bool = False,
) -> Optional[float]:
    if isinstance(value, bool):
        raise BrokerContractError(f"{field_name} must be numeric, not boolean")
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrokerContractError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise BrokerContractError(f"{field_name} must be finite")
    if zero_is_unknown and number == 0:
        return None
    if positive and number <= 0:
        raise BrokerContractError(f"{field_name} must be positive")
    if non_negative and number < 0:
        raise BrokerContractError(f"{field_name} must be non-negative")
    return number


def parse_broker_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        for candidate in (text, text.replace("Z", "+00:00")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if parsed is None:
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise BrokerContractError("invalid broker timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BROKER_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _position_key(
    payload: Mapping[str, Any],
    namespace: ExecutionNamespace,
    account_id: str,
) -> BrokerPositionKey:
    symbol = _text(
        _value(payload, "tradingsymbol", "tradingSymbol"), "tradingsymbol"
    ).upper()
    exchange = _text(_value(payload, "exchange"), "exchange").upper()
    product = _text(_value(payload, "product"), "product").upper()
    instrument = _value(
        payload,
        "instrument_token",
        "instrumentToken",
        "exchange_token",
        "exchangeToken",
    )
    instrument_id = str(instrument) if instrument not in (None, "") else symbol
    return BrokerPositionKey(
        namespace=namespace,
        account_id=account_id or "UNKNOWN",
        exchange=exchange,
        instrument_id=instrument_id,
        tradingsymbol=symbol,
        product=product,
    )


def normalize_position(
    payload: Mapping[str, Any],
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
) -> BrokerPosition:
    key = _position_key(payload, namespace, account_id)
    quantity = _quantity(_value(payload, "quantity"), "quantity", required=True)
    return BrokerPosition(
        key=key,
        signed_quantity=quantity,
        average_price=_number(
            _value(payload, "average_price", "averagePrice"),
            "average_price",
            positive=True,
            zero_is_unknown=True,
        ),
        last_price=_number(
            _value(payload, "last_price", "lastPrice"),
            "last_price",
            positive=True,
            zero_is_unknown=True,
        ),
        realised_gross=_number(_value(payload, "realised", "realized"), "realised"),
        unrealised_gross=_number(
            _value(payload, "unrealised", "unrealized"), "unrealised"
        ),
        pnl=_number(_value(payload, "pnl", "m2m"), "pnl"),
        buy_quantity=_quantity(
            _value(payload, "buy_quantity", "buyQuantity"), "buy_quantity"
        ),
        sell_quantity=_quantity(
            _value(payload, "sell_quantity", "sellQuantity"), "sell_quantity"
        ),
        buy_price=_number(
            _value(payload, "buy_price", "buyPrice"),
            "buy_price",
            positive=True,
            zero_is_unknown=True,
        ),
        sell_price=_number(
            _value(payload, "sell_price", "sellPrice"),
            "sell_price",
            positive=True,
            zero_is_unknown=True,
        ),
        buy_value=_number(
            _value(payload, "buy_value", "buyValue"),
            "buy_value",
            non_negative=True,
        ),
        sell_value=_number(
            _value(payload, "sell_value", "sellValue"),
            "sell_value",
            non_negative=True,
        ),
        overnight_quantity=_quantity(
            _value(payload, "overnight_quantity", "overnightQuantity"),
            "overnight_quantity",
        ),
        day_buy_quantity=_quantity(
            _value(payload, "day_buy_quantity", "dayBuyQuantity"),
            "day_buy_quantity",
        ),
        day_sell_quantity=_quantity(
            _value(payload, "day_sell_quantity", "daySellQuantity"),
            "day_sell_quantity",
        ),
        close_price=_number(
            _value(payload, "close_price", "closePrice"),
            "close_price",
            positive=True,
            zero_is_unknown=True,
        ),
        multiplier=_number(
            _value(payload, "multiplier", default=1), "multiplier", positive=True
        )
        or 1.0,
        mark_time=parse_broker_timestamp(
            _value(payload, "timestamp", "last_price_time", "lastPriceTime")
        ),
    )


def normalize_positions_response(
    payload: Any,
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
    fetched_at: Optional[datetime] = None,
) -> PositionSnapshot:
    fetched = fetched_at or utc_now()
    if not isinstance(payload, Mapping):
        return PositionSnapshot(
            net=(),
            day=(),
            quality=SnapshotQuality.UNAVAILABLE,
            fetched_at=fetched,
            errors=("positions response is not an object",),
        )
    errors: list[str] = []

    def normalize_group(name: str) -> tuple[BrokerPosition, ...]:
        if name not in payload:
            errors.append(f"missing {name} positions group")
            return ()
        raw_group = payload[name]
        if not isinstance(raw_group, list):
            errors.append(f"{name} positions is not a list")
            return ()
        records = []
        for index, raw in enumerate(raw_group):
            if not isinstance(raw, Mapping):
                errors.append(f"{name}[{index}] is not an object")
                continue
            try:
                records.append(
                    normalize_position(raw, namespace=namespace, account_id=account_id)
                )
            except BrokerContractError as exc:
                errors.append(f"{name}[{index}]: {exc}")
        return tuple(records)

    net = normalize_group("net")
    day = normalize_group("day")
    quality = SnapshotQuality.PARTIAL if errors else SnapshotQuality.COMPLETE
    return PositionSnapshot(
        net=net, day=day, quality=quality, fetched_at=fetched, errors=tuple(errors)
    )


def unavailable_position_snapshot(error: Exception | str) -> PositionSnapshot:
    return PositionSnapshot(
        net=(),
        day=(),
        quality=SnapshotQuality.UNAVAILABLE,
        fetched_at=utc_now(),
        errors=(str(error),),
    )


def _coerce_role(value: Any) -> OrderRole:
    if isinstance(value, OrderRole):
        return value
    try:
        return OrderRole(str(value).upper())
    except (TypeError, ValueError):
        return OrderRole.UNKNOWN


def normalize_order(
    payload: Mapping[str, Any],
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
    role: OrderRole = OrderRole.UNKNOWN,
    received_at: Optional[datetime] = None,
) -> BrokerOrder:
    key = _position_key(payload, namespace, account_id)
    order_id = _text(_value(payload, "order_id", "orderId"), "broker_order_id")
    original = _quantity(_value(payload, "quantity"), "quantity", required=True)
    filled = _quantity(
        _value(payload, "filled_quantity", "filledQuantity"), "filled_quantity"
    )
    pending_raw = _value(payload, "pending_quantity", "pendingQuantity")
    remaining = (
        _quantity(pending_raw, "pending_quantity")
        if pending_raw is not None
        else max(0, original - filled)
    )
    if (
        min(original, filled, remaining) < 0
        or filled > original
        or filled + remaining > original
    ):
        raise BrokerContractError("order quantities are inconsistent")
    side = _text(
        _value(payload, "transaction_type", "transactionType"), "transaction_type"
    ).upper()
    if side not in {"BUY", "SELL"}:
        raise BrokerContractError("transaction_type must be BUY or SELL")
    return BrokerOrder(
        broker_order_id=order_id,
        key=key,
        side=side,
        order_type=_text(
            _value(payload, "order_type", "orderType"), "order_type"
        ).upper(),
        original_quantity=original,
        filled_quantity=filled,
        remaining_quantity=remaining,
        price=_number(
            _value(payload, "price"),
            "price",
            positive=True,
            zero_is_unknown=True,
        ),
        average_fill_price=_number(
            _value(payload, "average_price", "averagePrice"),
            "average_price",
            positive=True,
            zero_is_unknown=True,
        ),
        trigger_price=_number(
            _value(payload, "trigger_price", "triggerPrice"),
            "trigger_price",
            positive=True,
            zero_is_unknown=True,
        ),
        status=_text(_value(payload, "status"), "status").upper(),
        variety=_text(_value(payload, "variety", default="regular"), "variety"),
        validity=_text(_value(payload, "validity"), "validity", required=False) or None,
        status_message=_text(
            _value(payload, "status_message", "statusMessage"),
            "status_message",
            required=False,
        )
        or None,
        client_intent_tag=_text(_value(payload, "tag"), "tag", required=False) or None,
        role=_coerce_role(role),
        exchange_time=parse_broker_timestamp(
            _value(payload, "exchange_timestamp", "exchangeTimestamp")
        ),
        order_time=parse_broker_timestamp(
            _value(payload, "order_timestamp", "orderTimestamp")
        ),
        received_at=received_at or utc_now(),
    )


def normalize_orders_response(
    payload: Any,
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
    roles_by_order_id: Optional[Mapping[str, Any]] = None,
    fetched_at: Optional[datetime] = None,
) -> OrderSnapshot:
    fetched = fetched_at or utc_now()
    roles = roles_by_order_id or {}
    errors: list[str] = []
    records = []
    if isinstance(payload, (str, bytes, Mapping)) or not isinstance(payload, Iterable):
        return OrderSnapshot(
            orders=(),
            quality=SnapshotQuality.UNAVAILABLE,
            fetched_at=fetched,
            errors=("orders response is not a list",),
        )
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            errors.append(f"orders[{index}] is not an object")
            continue
        try:
            order_id = str(_value(raw, "order_id", "orderId", default=""))
            records.append(
                normalize_order(
                    raw,
                    namespace=namespace,
                    account_id=account_id,
                    # Broker payloads normally have no role; persisted app
                    # ownership wins.  Keeping an explicitly supplied role is
                    # useful for deterministic adapters and does not let the
                    # renderer choose a mutation role.
                    role=_coerce_role(
                        roles.get(
                            order_id, _value(raw, "role", default=OrderRole.UNKNOWN)
                        )
                    ),
                    received_at=fetched,
                )
            )
        except (BrokerContractError, TypeError) as exc:
            errors.append(f"orders[{index}]: {exc}")
    quality = SnapshotQuality.PARTIAL if errors else SnapshotQuality.COMPLETE
    return OrderSnapshot(
        orders=tuple(records),
        quality=quality,
        fetched_at=fetched,
        errors=tuple(errors),
    )


def unavailable_order_snapshot(error: Exception | str) -> OrderSnapshot:
    return OrderSnapshot(
        orders=(),
        quality=SnapshotQuality.UNAVAILABLE,
        fetched_at=utc_now(),
        errors=(str(error),),
    )


def normalize_fill(
    payload: Mapping[str, Any],
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
    received_at: Optional[datetime] = None,
) -> BrokerFill:
    key = _position_key(payload, namespace, account_id)
    order_id = _text(_value(payload, "order_id", "orderId"), "broker_order_id")
    fill_id_raw = _value(payload, "trade_id", "tradeId", "fill_id", "fillId")
    fill_id = (
        str(fill_id_raw)
        if fill_id_raw not in (None, "")
        else f"{order_id}:{_value(payload, 'fill_timestamp', 'fillTimestamp', default='unknown')}"
    )
    side = _text(
        _value(payload, "transaction_type", "transactionType"), "transaction_type"
    ).upper()
    if side not in {"BUY", "SELL"}:
        raise BrokerContractError("transaction_type must be BUY or SELL")
    quantity = _quantity(_value(payload, "quantity"), "quantity")
    if quantity <= 0:
        raise BrokerContractError("fill quantity must be positive")
    return BrokerFill(
        broker_fill_id=fill_id,
        broker_order_id=order_id,
        key=key,
        side=side,
        quantity=quantity,
        fill_price=_number(
            _value(payload, "average_price", "averagePrice", "price"),
            "fill_price",
            positive=True,
            zero_is_unknown=True,
        ),
        exchange_time=parse_broker_timestamp(
            _value(
                payload,
                "fill_timestamp",
                "fillTimestamp",
                "exchange_timestamp",
                "exchangeTimestamp",
            )
        ),
        received_at=received_at or utc_now(),
    )


def normalize_fills_response(
    payload: Any,
    *,
    namespace: ExecutionNamespace = ExecutionNamespace.LIVE,
    account_id: str = "UNKNOWN",
    fetched_at: Optional[datetime] = None,
) -> FillSnapshot:
    fetched = fetched_at or utc_now()
    errors: list[str] = []
    records = []
    if isinstance(payload, (str, bytes, Mapping)) or not isinstance(payload, Iterable):
        return FillSnapshot(
            fills=(),
            quality=SnapshotQuality.UNAVAILABLE,
            fetched_at=fetched,
            errors=("fills response is not a list",),
        )
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            errors.append(f"fills[{index}] is not an object")
            continue
        try:
            records.append(
                normalize_fill(
                    raw,
                    namespace=namespace,
                    account_id=account_id,
                    received_at=fetched,
                )
            )
        except (BrokerContractError, TypeError) as exc:
            errors.append(f"fills[{index}]: {exc}")
    quality = SnapshotQuality.PARTIAL if errors else SnapshotQuality.COMPLETE
    return FillSnapshot(
        fills=tuple(records),
        quality=quality,
        fetched_at=fetched,
        errors=tuple(errors),
    )


def unavailable_fill_snapshot(error: Exception | str) -> FillSnapshot:
    return FillSnapshot(
        fills=(),
        quality=SnapshotQuality.UNAVAILABLE,
        fetched_at=utc_now(),
        errors=(str(error),),
    )


def position_to_backend_dict(position: BrokerPosition) -> dict[str, Any]:
    """Compatibility projection for the legacy orchestrator, always snake_case."""

    return {
        "position_key": position.key.as_string(),
        "namespace": position.key.namespace.value,
        "account_id": position.key.account_id,
        "tradingsymbol": position.key.tradingsymbol,
        "exchange": position.key.exchange,
        "instrument_token": position.key.instrument_id,
        "product": position.key.product,
        "quantity": position.signed_quantity,
        "average_price": position.average_price,
        "last_price": position.last_price,
        "realised": position.realised_gross,
        "unrealised": position.unrealised_gross,
        "pnl": position.pnl,
        "buy_quantity": position.buy_quantity,
        "sell_quantity": position.sell_quantity,
        "buy_price": position.buy_price,
        "sell_price": position.sell_price,
        "buy_value": position.buy_value,
        "sell_value": position.sell_value,
        "overnight_quantity": position.overnight_quantity,
        "day_buy_quantity": position.day_buy_quantity,
        "day_sell_quantity": position.day_sell_quantity,
        "close_price": position.close_price,
        "multiplier": position.multiplier,
        "mark_time": position.mark_time.isoformat() if position.mark_time else None,
    }


def order_to_backend_dict(order: BrokerOrder) -> dict[str, Any]:
    return {
        "order_id": order.broker_order_id,
        "position_key": order.key.as_string(),
        "namespace": order.key.namespace.value,
        "account_id": order.key.account_id,
        "tradingsymbol": order.key.tradingsymbol,
        "exchange": order.key.exchange,
        "instrument_token": order.key.instrument_id,
        "product": order.key.product,
        "transaction_type": order.side,
        "quantity": order.original_quantity,
        "filled_quantity": order.filled_quantity,
        "pending_quantity": order.remaining_quantity,
        "price": order.price,
        "average_price": order.average_fill_price,
        "trigger_price": order.trigger_price,
        "order_type": order.order_type,
        "variety": order.variety,
        "validity": order.validity,
        "status": order.status,
        "status_message": order.status_message,
        "tag": order.client_intent_tag,
        "is_archived": order.is_archived,
        "role": order.role.value,
        "is_app_order": order.is_app_order,
        "order_timestamp": order.order_time.isoformat() if order.order_time else None,
        "exchange_timestamp": order.exchange_time.isoformat()
        if order.exchange_time
        else None,
    }


def fill_to_backend_dict(fill: BrokerFill) -> dict[str, Any]:
    return {
        "trade_id": fill.broker_fill_id,
        "order_id": fill.broker_order_id,
        "position_key": fill.key.as_string(),
        "namespace": fill.key.namespace.value,
        "account_id": fill.key.account_id,
        "tradingsymbol": fill.key.tradingsymbol,
        "exchange": fill.key.exchange,
        "instrument_token": fill.key.instrument_id,
        "product": fill.key.product,
        "transaction_type": fill.side,
        "quantity": fill.quantity,
        "average_price": fill.fill_price,
        "fill_timestamp": fill.exchange_time.isoformat()
        if fill.exchange_time
        else None,
    }


def position_to_renderer_dto(position: BrokerPosition) -> dict[str, Any]:
    return {
        "positionKey": position.key.as_string(),
        "namespace": position.key.namespace.value,
        "accountId": position.key.account_id,
        "tradingsymbol": position.key.tradingsymbol,
        "exchange": position.key.exchange,
        "instrumentToken": position.key.instrument_id,
        "product": position.key.product,
        "quantity": position.signed_quantity,
        "overnightQuantity": position.overnight_quantity,
        "averagePrice": position.average_price,
        "lastPrice": position.last_price,
        "closePrice": position.close_price,
        "pnl": position.pnl,
        "unrealised": position.unrealised_gross,
        "realised": position.realised_gross,
        "buyQuantity": position.buy_quantity,
        "sellQuantity": position.sell_quantity,
        "buyPrice": position.buy_price,
        "sellPrice": position.sell_price,
        "buyValue": position.buy_value,
        "sellValue": position.sell_value,
        "multiplier": position.multiplier,
        "dayBuyQuantity": position.day_buy_quantity,
        "daySellQuantity": position.day_sell_quantity,
        "markTime": position.mark_time.isoformat() if position.mark_time else None,
    }


def order_to_renderer_dto(order: BrokerOrder) -> dict[str, Any]:
    return {
        "orderId": order.broker_order_id,
        "positionKey": order.key.as_string(),
        "namespace": order.key.namespace.value,
        "accountId": order.key.account_id,
        "isAppOrder": order.is_app_order,
        "role": order.role.value,
        "tradingsymbol": order.key.tradingsymbol,
        "exchange": order.key.exchange,
        "transactionType": order.side,
        "quantity": order.original_quantity,
        "filledQuantity": order.filled_quantity,
        "pendingQuantity": order.remaining_quantity,
        "price": order.price,
        "averagePrice": order.average_fill_price,
        "triggerPrice": order.trigger_price,
        "product": order.key.product,
        "orderType": order.order_type,
        "variety": order.variety,
        "validity": order.validity,
        "status": order.status,
        "statusMessage": order.status_message,
        "isWorking": order.is_working,
        "tag": order.client_intent_tag,
        "isArchived": order.is_archived,
        "orderTimestamp": order.order_time.isoformat() if order.order_time else None,
        "exchangeTimestamp": order.exchange_time.isoformat()
        if order.exchange_time
        else None,
    }


def fill_to_renderer_dto(fill: BrokerFill) -> dict[str, Any]:
    return {
        "tradeId": fill.broker_fill_id,
        "orderId": fill.broker_order_id,
        "positionKey": fill.key.as_string(),
        "namespace": fill.key.namespace.value,
        "accountId": fill.key.account_id,
        "tradingsymbol": fill.key.tradingsymbol,
        "exchange": fill.key.exchange,
        "product": fill.key.product,
        "transactionType": fill.side,
        "quantity": fill.quantity,
        "averagePrice": fill.fill_price,
        "fillTimestamp": fill.exchange_time.isoformat() if fill.exchange_time else None,
    }


def position_snapshot_to_renderer_dto(
    snapshot: PositionSnapshot,
) -> dict[str, Any]:
    """Compatibility DTO for the existing ``{net, day}`` renderer shape."""

    return {
        "net": [position_to_renderer_dto(item) for item in snapshot.net],
        "day": [position_to_renderer_dto(item) for item in snapshot.day],
        "snapshotQuality": snapshot.quality.value,
        "snapshotId": snapshot.snapshot_id,
        "fetchedAt": snapshot.fetched_at.isoformat(),
    }


def order_snapshot_to_renderer_dto(snapshot: OrderSnapshot) -> dict[str, Any]:
    """Serialize rows and snapshot quality independently.

    A quality flag attached only to rows vanishes for ``[]`` and makes an
    unavailable order book indistinguishable from a verified empty one.
    """

    return {
        "orders": [order_to_renderer_dto(item) for item in snapshot.orders],
        "snapshotQuality": snapshot.quality.value,
        "snapshotId": snapshot.snapshot_id,
        "fetchedAt": snapshot.fetched_at.isoformat(),
        "errors": list(snapshot.errors),
    }


def fill_snapshot_to_renderer_dto(snapshot: FillSnapshot) -> list[dict[str, Any]]:
    snapshot.require_complete()
    return [fill_to_renderer_dto(item) for item in snapshot.fills]
