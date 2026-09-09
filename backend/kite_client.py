import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from kiteconnect import KiteConnect

from .request_policy import Priority, broker_gateway


def to_camel(s):
    parts = s.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


def convert_keys(obj):
    if isinstance(obj, list):
        return [convert_keys(i) for i in obj]
    elif isinstance(obj, dict):
        return {to_camel(k): convert_keys(v) for k, v in obj.items()}
    else:
        return obj


class KiteClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KiteClient, cls).__new__(cls)
            cls._instance.kite = None
            cls._instance.instruments_cache = None
            cls._instance.access_token = None
        return cls._instance

    def init(self, api_key: str):
        self.kite = KiteConnect(api_key=api_key)

    def set_access_token(self, access_token: str):
        if self.kite:
            self.kite.set_access_token(access_token)
            self.access_token = access_token

    def login_url(self) -> str:
        if self.kite:
            return self.kite.login_url()
        return ""

    def generate_session(self, request_token: str, api_secret: str) -> Dict[str, Any]:
        if not self.kite:
            raise Exception("Kite client not initialized")
        session = self.kite.generate_session(request_token, api_secret)
        self.set_access_token(session["access_token"])
        return session

    def get_positions(self) -> Dict[str, Any]:
        if not self.kite:
            return {"net": [], "day": []}
        res = broker_gateway.execute(self.kite.positions, priority=Priority.RECONCILE)
        return convert_keys(res)

    def get_orders(self) -> List[Dict[str, Any]]:
        from .config import config_manager

        if not self.kite:
            return []

        res = broker_gateway.execute(self.kite.orders, priority=Priority.RECONCILE)
        app_orders = config_manager.get_app_order_ids()

        historical = config_manager.get_historical_orders()
        for o in res:
            o_id = str(o.get("order_id"))
            historical[o_id] = o

        config_manager.save_historical_orders(historical)
        all_orders = list(historical.values())

        def get_ts(order):
            return str(
                order.get("order_timestamp") or order.get("exchange_timestamp") or ""
            )

        all_orders.sort(key=get_ts, reverse=True)

        for o in all_orders:
            o["is_app_order"] = str(o.get("order_id")) in app_orders

        return convert_keys(all_orders)

    def get_trades(self) -> List[Dict[str, Any]]:
        if not self.kite:
            return []
        res = broker_gateway.execute(self.kite.trades, priority=Priority.RECONCILE)
        return convert_keys(res)

    def place_order(
        self,
        variety,
        exchange,
        tradingsymbol,
        transaction_type,
        quantity,
        product,
        order_type,
        **kwargs,
    ) -> str:
        from .config import config_manager

        # A short client-side idempotency tag lets the reconciler distinguish
        # this order from previous orders with the same symbol/side/qty.
        # Kite's `tag` field is free-form; we use the last 8 chars of a UUID.
        idempotency_tag = f"ag{uuid.uuid4().hex[-6:]}"
        placed_at = datetime.now(timezone.utc)

        def reconciler():
            # Check if order was placed despite timeout.
            # Narrow by tag first; fall back to attribute match + time window.
            orders = broker_gateway.execute(
                self.kite.orders, priority=Priority.RECONCILE
            )
            for o in orders:
                if str(o.get("tag", "")) == idempotency_tag:
                    return str(o.get("order_id"))

            # Tag may not be echoed by all Kite environments; fall back to
            # attribute match restricted to orders placed in the last 60 s.
            cutoff = placed_at
            for o in orders:
                ts_str = str(
                    o.get("order_timestamp") or o.get("exchange_timestamp") or ""
                )
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        # Kite returns IST-naive strings; treat as IST (+05:30)
                        from datetime import timedelta

                        ts = ts.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    if (ts - cutoff).total_seconds() < -60:
                        continue  # order is too old to be ours
                except (ValueError, TypeError):
                    pass  # can't parse timestamp — skip time-window guard

                if (
                    o.get("tradingsymbol") == tradingsymbol
                    and o.get("transaction_type") == transaction_type
                    and o.get("quantity") == quantity
                    and o.get("product") == product
                    and o.get("order_type") == order_type
                ):
                    return str(o.get("order_id"))
            return None

        order_id = broker_gateway.execute(
            self.kite.place_order,
            priority=Priority.ORDER,
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
        config_manager.add_app_order_id(order_id)
        return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return broker_gateway.execute(
            self.kite.cancel_order,
            priority=Priority.CRITICAL,
            variety=variety,
            order_id=order_id,
            parent_order_id=parent_order_id,
        )

    def modify_order(self, variety, order_id, **kwargs):
        return broker_gateway.execute(
            self.kite.modify_order,
            priority=Priority.CRITICAL,
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

    def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        if not self.kite:
            return {}
        return broker_gateway.execute(
            self.kite.quote, Priority.ANALYTICS, False, None, instruments
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
