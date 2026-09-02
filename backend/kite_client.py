from typing import Any, Dict, List

from kiteconnect import KiteConnect


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
        res = self.kite.positions() if self.kite else {"net": [], "day": []}
        return convert_keys(res)

    def get_orders(self) -> List[Dict[str, Any]]:
        from .config import config_manager

        res = self.kite.orders() if self.kite else []
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

        order_id = self.kite.place_order(
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            **kwargs,
        )
        config_manager.add_app_order_id(order_id)
        return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return self.kite.cancel_order(variety, order_id, parent_order_id)

    def modify_order(self, variety, order_id, **kwargs):
        return self.kite.modify_order(variety=variety, order_id=order_id, **kwargs)

    def get_margins(self) -> Dict[str, Any]:
        return self.kite.margins() if self.kite else {}

    def get_holdings(self) -> List[Dict[str, Any]]:
        return self.kite.holdings() if self.kite else []

    def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        return self.kite.quote(instruments) if self.kite else {}

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        return self.kite.ltp(instruments) if self.kite else {}

    def get_historical_data(
        self, instrument_token, from_date, to_date, interval, continuous=False, oi=False
    ):
        return (
            self.kite.historical_data(
                instrument_token, from_date, to_date, interval, continuous, oi
            )
            if self.kite
            else []
        )

    def get_instruments(self, exchange=None):
        if not self.instruments_cache:
            self.instruments_cache = {}

        if exchange:
            if exchange not in self.instruments_cache:
                self.instruments_cache[exchange] = (
                    self.kite.instruments(exchange) if self.kite else []
                )
            return self.instruments_cache[exchange]

        # If no exchange is specified, fetch all (not recommended due to size)
        if "all" not in self.instruments_cache:
            self.instruments_cache["all"] = self.kite.instruments() if self.kite else []
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
