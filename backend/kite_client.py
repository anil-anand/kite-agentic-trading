from kiteconnect import KiteConnect
from typing import Dict, List, Any, Optional

def to_camel(s):
    parts = s.split('_')
    return parts[0] + ''.join(word.capitalize() for word in parts[1:])

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
        res = self.kite.orders() if self.kite else []
        return convert_keys(res)
        
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, price=None, validity=None, validity_ttl=None, disclosed_quantity=None, trigger_price=None, squareoff=None, stoploss=None, trailing_stoploss=None, tag=None) -> str:
        return self.kite.place_order(variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, price, validity, validity_ttl, disclosed_quantity, trigger_price, squareoff, stoploss, trailing_stoploss, tag)
        
    def cancel_order(self, variety, order_id, parent_order_id=None):
        return self.kite.cancel_order(variety, order_id, parent_order_id)
        
    def modify_order(self, variety, order_id, parent_order_id=None, exchange=None, tradingsymbol=None, transaction_type=None, quantity=None, price=None, order_type=None, product=None, trigger_price=None, validity=None, disclosed_quantity=None):
        return self.kite.modify_order(variety, order_id, parent_order_id, exchange, tradingsymbol, transaction_type, quantity, price, order_type, product, trigger_price, validity, disclosed_quantity)
        
    def get_margins(self) -> Dict[str, Any]:
        return self.kite.margins() if self.kite else {}
        
    def get_holdings(self) -> List[Dict[str, Any]]:
        return self.kite.holdings() if self.kite else []
        
    def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        return self.kite.quote(instruments) if self.kite else {}
        
    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        return self.kite.ltp(instruments) if self.kite else {}
        
    def get_historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        return self.kite.historical_data(instrument_token, from_date, to_date, interval, continuous, oi) if self.kite else []
        
    def get_instruments(self, exchange=None):
        if not self.instruments_cache:
            self.instruments_cache = self.kite.instruments() if self.kite else []
            
        if exchange:
            return [i for i in self.instruments_cache if i['exchange'] == exchange]
        return self.instruments_cache
        
    def search_instruments(self, query: str) -> List[Dict[str, Any]]:
        query = query.upper()
        instruments = self.get_instruments("NSE")
        results = [i for i in instruments if query in i['tradingsymbol']][:50]
        return results

kite_client = KiteClient()
