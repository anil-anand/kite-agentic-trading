import math
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from kiteconnect import KiteConnect

from .broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    FillSnapshot,
    OrderRole,
    OrderSnapshot,
    OrderSubmissionRejected,
    OrderSubmissionUnknown,
    PositionSnapshot,
    SnapshotQuality,
    fill_snapshot_to_renderer_dto,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
    order_snapshot_to_renderer_dto,
    order_to_backend_dict,
    parse_broker_timestamp,
    position_snapshot_to_renderer_dto,
    unavailable_fill_snapshot,
    unavailable_order_snapshot,
    unavailable_position_snapshot,
)
from .request_policy import Priority, broker_gateway
from .time_utils import EXCHANGE_TIMEZONE


class KiteClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KiteClient, cls).__new__(cls)
            cls._instance.kite = None
            cls._instance.instruments_cache = None
            cls._instance.access_token = None
            cls._instance.account_id = "UNKNOWN"
            cls._instance.namespace = ExecutionNamespace.LIVE
        return cls._instance

    def init(self, api_key: str):
        self.account_id = "UNKNOWN"
        self.access_token = None
        self.kite = KiteConnect(api_key=api_key)

    def set_access_token(self, access_token: str):
        if access_token != self.access_token:
            self.account_id = "UNKNOWN"
        if self.kite:
            self.kite.set_access_token(access_token)
            self.access_token = access_token
        else:
            self.access_token = access_token

    def refresh_account_id(self) -> str:
        """Load the broker account identity after restoring an access token."""

        if not self.kite or not self.access_token:
            self.account_id = "UNKNOWN"
            return self.account_id
        try:
            profile = broker_gateway.execute(
                self.kite.profile, priority=Priority.RECONCILE
            )
            self.account_id = str(profile.get("user_id") or "UNKNOWN")
        except Exception:
            self.account_id = "UNKNOWN"
        return self.account_id

    def login_url(self) -> str:
        if self.kite:
            return self.kite.login_url()
        return ""

    def generate_session(self, request_token: str, api_secret: str) -> Dict[str, Any]:
        if not self.kite:
            raise Exception("Kite client not initialized")
        session = self.kite.generate_session(request_token, api_secret)
        self.set_access_token(session["access_token"])
        self.account_id = "UNKNOWN"
        self.refresh_account_id()
        return session

    def get_positions_snapshot(self, *, critical: bool = False) -> PositionSnapshot:
        if not self.kite:
            return unavailable_position_snapshot("Kite client is not initialized")
        try:
            priority = Priority.CRITICAL if critical else Priority.RECONCILE
            res = broker_gateway.execute(self.kite.positions, priority=priority)
            snapshot = normalize_positions_response(
                res,
                namespace=self.namespace,
                account_id=self.account_id,
            )
            return self._attach_timestamped_marks(snapshot, critical=critical)
        except Exception as exc:
            return unavailable_position_snapshot(exc)

    def _attach_timestamped_marks(
        self, snapshot: PositionSnapshot, *, critical: bool = False
    ) -> PositionSnapshot:
        """Enrich positions with exchange/last-trade time from full quotes.

        Kite position rows normally expose ``last_price`` but no mark time.
        The position HTTP receipt time is not a market timestamp, so an
        unavailable quote intentionally leaves the mark unknown and blocks new
        risk while reductions remain possible.
        """

        positions = list(snapshot.net)
        if not positions:
            return snapshot
        instruments = [
            f"{position.key.exchange}:{position.key.tradingsymbol}"
            for position in positions
        ]
        try:
            quotes = (
                self.get_quote(instruments, critical=True)
                if critical
                else self.get_quote(instruments)
            )
        except Exception:
            return snapshot
        if not isinstance(quotes, dict):
            return snapshot
        enriched = []
        for position in positions:
            quote = quotes.get(
                f"{position.key.exchange}:{position.key.tradingsymbol}", {}
            )
            if not isinstance(quote, dict):
                enriched.append(position)
                continue
            try:
                mark_time = parse_broker_timestamp(
                    quote.get("last_trade_time")
                    or quote.get("lastTradeTime")
                    or quote.get("timestamp")
                )
            except (TypeError, ValueError):
                mark_time = None
            mark = quote.get("last_price", quote.get("lastPrice"))
            try:
                mark = float(mark)
            except (TypeError, ValueError):
                mark = None
            # Price and timestamp are one observation.  Do not pair a fresh
            # quote timestamp with a fallback position price: that would make
            # an old/unknown mark look admission-fresh.  ``bool`` is numeric
            # in Python but never a valid broker price.
            if (
                isinstance(quote.get("last_price", quote.get("lastPrice")), bool)
                or mark is None
                or not math.isfinite(mark)
                or mark <= 0
                or mark_time is None
            ):
                enriched.append(position)
                continue
            enriched.append(replace(position, last_price=mark, mark_time=mark_time))
        return replace(snapshot, net=tuple(enriched))

    def get_positions(self) -> Dict[str, Any]:
        """Compatibility renderer DTO; domain code uses ``get_positions_snapshot``."""

        return position_snapshot_to_renderer_dto(self.get_positions_snapshot())

    def get_current_orders_snapshot(self, *, critical: bool = False) -> OrderSnapshot:
        from .config import config_manager

        if not self.kite:
            return unavailable_order_snapshot("Kite client is not initialized")
        try:
            priority = Priority.CRITICAL if critical else Priority.RECONCILE
            res = broker_gateway.execute(self.kite.orders, priority=priority)
            return normalize_orders_response(
                res,
                namespace=self.namespace,
                account_id=self.account_id,
                roles_by_order_id=config_manager.get_app_order_roles(),
            )
        except Exception as exc:
            return unavailable_order_snapshot(exc)

    def get_orders(self) -> List[Dict[str, Any]]:
        """Display history DTO; risk code uses only the current order snapshot."""

        return order_snapshot_to_renderer_dto(self.get_order_history_snapshot())

    def get_order_history_snapshot(
        self, current_snapshot: Optional[OrderSnapshot] = None
    ) -> OrderSnapshot:
        """Return display history without allowing it to become current truth."""

        from .config import config_manager

        current = current_snapshot or self.get_current_orders_snapshot()
        roles = config_manager.get_app_order_roles()
        historical = config_manager.get_historical_orders()

        if current.quality is SnapshotQuality.COMPLETE:
            for order in current.orders:
                historical[order.broker_order_id] = order_to_backend_dict(order)
            config_manager.save_historical_orders(historical)

        archive = normalize_orders_response(
            historical.values(),
            namespace=self.namespace,
            account_id=self.account_id,
            roles_by_order_id=roles,
        )
        current_ids = {order.broker_order_id for order in current.orders}
        current_orders = tuple(
            replace(order, is_archived=False) for order in current.orders
        )
        archived_orders = tuple(
            replace(order, is_archived=True)
            for order in archive.orders
            if order.broker_order_id not in current_ids
        )
        orders = sorted(
            current_orders + archived_orders,
            key=lambda order: (
                order.order_time or order.exchange_time or current.fetched_at
            ),
            reverse=True,
        )
        errors = tuple(current.errors) + tuple(archive.errors)
        if current.quality is SnapshotQuality.UNAVAILABLE:
            quality = SnapshotQuality.STALE if orders else SnapshotQuality.UNAVAILABLE
        elif current.quality is not SnapshotQuality.COMPLETE:
            quality = current.quality
        elif archive.quality is SnapshotQuality.PARTIAL:
            quality = SnapshotQuality.PARTIAL
        else:
            quality = SnapshotQuality.COMPLETE
        return OrderSnapshot(
            orders=tuple(orders),
            quality=quality,
            fetched_at=current.fetched_at,
            errors=errors,
            snapshot_id=current.snapshot_id,
        )

    def get_fills_snapshot(self, *, critical: bool = False) -> FillSnapshot:
        if not self.kite:
            return unavailable_fill_snapshot("Kite client is not initialized")
        try:
            priority = Priority.CRITICAL if critical else Priority.RECONCILE
            res = broker_gateway.execute(self.kite.trades, priority=priority)
            return normalize_fills_response(
                res,
                namespace=self.namespace,
                account_id=self.account_id,
            )
        except Exception as exc:
            return unavailable_fill_snapshot(exc)

    def get_trades(self) -> List[Dict[str, Any]]:
        return fill_snapshot_to_renderer_dto(self.get_fills_snapshot())

    def get_broker_snapshot(self, *, critical: bool = False) -> BrokerSnapshot:
        positions = self.get_positions_snapshot(critical=critical)
        orders = self.get_current_orders_snapshot(critical=critical)
        fills = self.get_fills_snapshot(critical=critical)
        return BrokerSnapshot(
            namespace=self.namespace,
            account_id=self.account_id,
            positions=positions.net,
            day_positions=positions.day,
            current_orders=orders.orders,
            fills=fills.fills,
            positions_quality=positions.quality,
            orders_quality=orders.quality,
            fills_quality=fills.quality,
            fetched_at=max(positions.fetched_at, orders.fetched_at, fills.fetched_at),
            errors=positions.errors + orders.errors + fills.errors,
            positions_fetched_at=positions.fetched_at,
            orders_fetched_at=orders.fetched_at,
            fills_fetched_at=fills.fetched_at,
        )

    def place_order(
        self,
        variety,
        exchange,
        tradingsymbol,
        transaction_type,
        quantity,
        product,
        order_type,
        order_role=OrderRole.UNKNOWN,
        **kwargs,
    ) -> str:
        from .config import config_manager

        # The lifecycle coordinator supplies one durable tag per attempt.  A
        # direct legacy caller still receives a fresh tag, but it must not be
        # used to retry a prior unknown submission.
        idempotency_tag = kwargs.pop("attempt_tag", None) or kwargs.pop("tag", None)
        if not isinstance(idempotency_tag, str) or not idempotency_tag.strip():
            idempotency_tag = f"ol{uuid.uuid4().hex[:18]}"
        idempotency_tag = idempotency_tag.strip()
        if len(idempotency_tag) > 20:
            raise OrderSubmissionRejected(
                "broker attempt tag must be at most 20 characters"
            )

        def reconciler():
            # Attribute/time matching can select another strategy or a prior
            # manual order.  An exact durable tag is the only safe automatic
            # reconciliation key; a missing tag remains UNKNOWN for recovery.
            orders = broker_gateway.execute(
                self.kite.orders,
                priority=Priority.CRITICAL if critical else Priority.RECONCILE,
            )
            for o in orders:
                if str(o.get("tag", "")) == idempotency_tag:
                    order_id = o.get("order_id") or o.get("orderId")
                    if order_id:
                        return str(order_id)
            return None

        try:
            role = (
                order_role
                if isinstance(order_role, OrderRole)
                else OrderRole(str(order_role).upper())
            )
        except ValueError:
            role = OrderRole.UNKNOWN
        critical = bool(kwargs.pop("critical", False)) or role in {
            OrderRole.PROTECTION,
        }
        # A hard supervisor passes ``critical`` for a flatten/recovery.  A
        # routine reduction retains ORDER priority so it cannot starve native
        # protection while the broker gateway is rate-limited.
        priority = Priority.CRITICAL if critical else Priority.ORDER
        order_id = broker_gateway.execute(
            self.kite.place_order,
            priority=priority,
            is_order=True,
            order_reconciler=reconciler,
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            tag=idempotency_tag,
            **kwargs,
        )
        if isinstance(order_id, bool) or order_id is None or not str(order_id).strip():
            raise OrderSubmissionUnknown("broker acknowledgement has no order ID")
        order_id = str(order_id)
        config_manager.add_app_order_id(order_id, role.value)
        return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return broker_gateway.execute(
            self.kite.cancel_order,
            priority=Priority.CRITICAL,
            is_order=True,
            variety=variety,
            order_id=order_id,
            parent_order_id=parent_order_id,
        )

    def modify_order(self, variety, order_id, **kwargs):
        return broker_gateway.execute(
            self.kite.modify_order,
            priority=Priority.CRITICAL,
            is_order=True,
            variety=variety,
            order_id=order_id,
            **kwargs,
        )

    def get_margins(self) -> Dict[str, Any]:
        if not self.kite:
            return {}
        return broker_gateway.execute(self.kite.margins, priority=Priority.RECONCILE)

    def get_holdings(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        return broker_gateway.execute(self.kite.holdings, priority=Priority.RECONCILE)

    def get_quote(
        self, instruments: List[str], *, critical: bool = False
    ) -> Dict[str, Any]:
        if not self.kite:
            return {}
        return broker_gateway.execute(
            self.kite.quote,
            Priority.CRITICAL if critical else Priority.ANALYTICS,
            False,
            None,
            instruments,
        )

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        if not self.kite:
            return {}
        return broker_gateway.execute(
            self.kite.ltp, Priority.ANALYTICS, False, None, instruments
        )

    def get_historical_data(
        self, instrument_token, from_date, to_date, interval, continuous=False, oi=False
    ):
        if not self.kite:
            return []

        # KiteConnect formats datetime objects with ``strftime`` and therefore
        # discards their timezone offset.  Convert UTC/internal timestamps to
        # exchange wall time at this shared broker boundary so every caller
        # serializes the intended IST trading interval.
        from_date = self._historical_boundary_in_exchange_time(from_date)
        to_date = self._historical_boundary_in_exchange_time(to_date)
        return broker_gateway.execute(
            self.kite.historical_data,
            priority=Priority.ANALYTICS,
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            continuous=continuous,
            oi=oi,
        )

    @staticmethod
    def _historical_boundary_in_exchange_time(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=EXCHANGE_TIMEZONE)
            return value.astimezone(EXCHANGE_TIMEZONE)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=EXCHANGE_TIMEZONE)
            return parsed.astimezone(EXCHANGE_TIMEZONE)
        return value

    def get_instruments(self, exchange=None):
        if not self.instruments_cache:
            self.instruments_cache = {}

        if exchange:
            if exchange not in self.instruments_cache:
                if self.kite:
                    self.instruments_cache[exchange] = broker_gateway.execute(
                        self.kite.instruments,
                        priority=Priority.ANALYTICS,
                        exchange=exchange,
                    )
                else:
                    self.instruments_cache[exchange] = []
            return self.instruments_cache[exchange]

        if "all" not in self.instruments_cache:
            if self.kite:
                self.instruments_cache["all"] = broker_gateway.execute(
                    self.kite.instruments, priority=Priority.ANALYTICS
                )
            else:
                self.instruments_cache["all"] = []
        return self.instruments_cache["all"]

    def search_instruments(self, query: str) -> List[Dict[str, Any]]:
        query = query.upper()
        instruments = self.get_instruments("NSE")
        results = [i for i in instruments if query in i["tradingsymbol"]][:50]
        return results


def _make_client():
    # In development (KITE_DEV_MODE=1) swap in a mock that serves synthetic data
    # so the app runs with no Zerodha login, credentials, or network.
    from .dev_mode import is_dev_mode

    if is_dev_mode():
        from .mock_kite_client import MockKiteClient

        return MockKiteClient()
    return KiteClient()


kite_client = _make_client()
