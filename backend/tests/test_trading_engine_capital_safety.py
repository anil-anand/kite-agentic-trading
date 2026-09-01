import datetime

from backend.trading_engine import TradingEngine


class FakeKiteClient:
    def __init__(self):
        self.positions = {"net": []}
        self.orders = []
        self.place_calls = []
        self.cancel_calls = []
        self._next_id = 1

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        order_id = f"OID{self._next_id}"
        self._next_id += 1
        return order_id

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.cancel_calls.append(
            {
                "variety": variety,
                "order_id": order_id,
                "parent_order_id": parent_order_id,
            }
        )

    def get_positions(self):
        return self.positions

    def get_orders(self):
        return self.orders


class FakeRiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.open_positions = 0
        self.pnl_updates = []

    def can_trade(self):
        return True, "OK"

    def calculate_position_size(self, price, stop_loss):
        return 10

    def set_open_positions(self, count):
        self.open_positions = count

    def update_pnl(self, pnl):
        self.daily_pnl += pnl
        self.pnl_updates.append(pnl)


def _sample_signal():
    return {
        "id": "sig-1",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "direction": "BUY",
        "entryPrice": 100.0,
        "stopLoss": 95.0,
        "target": 110.0,
        "strategy": "test",
    }


def test_execute_signal_places_protective_stop_and_tracks_trade(monkeypatch):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.positions = {
        "net": [
            {
                "tradingsymbol": "RELIANCE",
                "quantity": 10,
                "exchange": "NSE",
                "product": "MIS",
                "lastPrice": 100.0,
            }
        ]
    }
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine._entry_fill_timeout_seconds = 1
    engine._entry_fill_poll_seconds = 0

    assert engine.execute_signal(_sample_signal()) is True
    assert len(fake_client.place_calls) == 2
    assert fake_client.place_calls[0]["order_type"] == "LIMIT"
    assert fake_client.place_calls[1]["order_type"] == "SL-M"
    assert fake_client.place_calls[1]["trigger_price"] == 95.0
    assert "RELIANCE" in engine.active_trades
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "OID2"


def test_execute_signal_does_not_track_unfilled_order(monkeypatch):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.orders = [
        {"orderId": "OID1", "status": "REJECTED", "filledQuantity": 0},
    ]
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine._entry_fill_timeout_seconds = 1
    engine._entry_fill_poll_seconds = 0

    assert engine.execute_signal(_sample_signal()) is False
    assert len(fake_client.place_calls) == 1
    assert "RELIANCE" not in engine.active_trades
    assert fake_client.cancel_calls[0]["order_id"] == "OID1"


def test_monitor_positions_updates_pnl_and_prevents_double_exit(monkeypatch):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.positions = {
        "net": [
            {
                "tradingsymbol": "RELIANCE",
                "quantity": 10,
                "exchange": "NSE",
                "product": "MIS",
                "lastPrice": 94.0,
                "realised": -50.0,
                "unrealised": -25.0,
                "averagePrice": 100.0,
            }
        ]
    }
    fake_client.orders = [{"orderId": "OID1", "status": "OPEN", "filledQuantity": 0}]
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine.active_trades["RELIANCE"] = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "stop_order_id": "STOP1",
        "exit_pending": False,
        "exit_order_id": None,
    }

    engine.monitor_positions()
    assert fake_risk.daily_pnl == -75.0
    assert fake_risk.pnl_updates == [-75.0]
    assert len(fake_client.place_calls) == 1
    assert fake_client.place_calls[0]["transaction_type"] == "SELL"
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"
    assert engine.active_trades["RELIANCE"]["exit_pending"] is True

    engine.monitor_positions()
    assert len(fake_client.place_calls) == 1
