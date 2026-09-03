"""Tests for the paper-trading broker and the engine's paper mode.

The PaperBroker is the risky new surface (fill model + position maths), so it
gets thorough unit coverage; a few engine-level tests confirm the seam wires up
and an end-to-end paper trade behaves.
"""

import backend.trading_engine as te
from backend.paper_broker import PaperBroker
from backend.trading_engine import TradingEngine


class MDStub:
    """Minimal market-data client: instruments + LTP, records get_ltp calls."""

    def __init__(self, ltp=None):
        self._ltp = dict(ltp or {})
        self.ltp_calls = []

    def get_instruments(self, exchange=None):
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 111, "tick_size": 0.05},
            {"tradingsymbol": "INFY", "instrument_token": 222, "tick_size": 0.05},
        ]

    def get_ltp(self, instruments):
        self.ltp_calls.append(list(instruments))
        out = {}
        for key in instruments:
            sym = key.split(":")[-1]
            if sym in self._ltp:
                out[key] = {"last_price": self._ltp[sym]}
        return out


def _buy(broker, symbol="RELIANCE", qty=10, price=100.0, order_type="LIMIT"):
    return broker.place_order(
        variety="regular",
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type="BUY",
        quantity=qty,
        product="MIS",
        order_type=order_type,
        price=price,
    )


def _sell(broker, symbol="RELIANCE", qty=10, price=100.0, order_type="LIMIT"):
    return broker.place_order(
        variety="regular",
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type="SELL",
        quantity=qty,
        product="MIS",
        order_type=order_type,
        price=price,
    )


def _pos(broker, symbol="RELIANCE"):
    for p in broker.get_positions()["net"]:
        if p["tradingsymbol"] == symbol:
            return p
    return None


# ---------------------------------------------------------------------------
# Order fills
# ---------------------------------------------------------------------------


class TestFills:
    def test_limit_buy_fills_at_limit_price(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 101.0)  # LTP differs; LIMIT fills at its price
        _buy(b, price=100.0)
        p = _pos(b)
        assert p["quantity"] == 10
        assert p["averagePrice"] == 100.0

    def test_market_buy_fills_at_ltp(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 102.5)
        _buy(b, price=None, order_type="MARKET")
        assert _pos(b)["averagePrice"] == 102.5

    def test_market_fill_uses_delegated_get_ltp(self):
        md = MDStub(ltp={"RELIANCE": 103.0})
        b = PaperBroker(md)
        _buy(b, price=None, order_type="MARKET")
        assert _pos(b)["averagePrice"] == 103.0
        assert md.ltp_calls  # LTP was fetched from the wrapped client

    def test_zero_quantity_is_rejected(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        oid = _buy(b, qty=0)
        orders = {o["orderId"]: o for o in b.get_orders()}
        assert orders[oid]["status"] == "REJECTED"
        assert _pos(b) is None

    def test_market_with_no_price_is_rejected(self):
        b = PaperBroker(MDStub())  # no LTP anywhere
        oid = _buy(b, price=None, order_type="MARKET")
        orders = {o["orderId"]: o for o in b.get_orders()}
        assert orders[oid]["status"] == "REJECTED"
        assert _pos(b) is None


# ---------------------------------------------------------------------------
# Resting stop (SL) orders
# ---------------------------------------------------------------------------


def _place_sl(broker, txn, trigger, qty=10, symbol="RELIANCE"):
    return broker.place_order(
        variety="regular",
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type=txn,
        quantity=qty,
        product="MIS",
        order_type="SL",
        price=trigger * (0.99 if txn == "SELL" else 1.01),
        trigger_price=trigger,
    )


class TestStopOrders:
    def test_sl_rests_and_does_not_fill_immediately(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _buy(b, price=100.0)
        oid = _place_sl(b, "SELL", trigger=95.0)
        orders = {o["orderId"]: o for o in b.get_orders()}
        assert orders[oid]["status"] == "TRIGGER PENDING"
        assert _pos(b)["quantity"] == 10  # position unchanged

    def test_sell_stop_triggers_on_the_way_down(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _buy(b, price=100.0)
        _place_sl(b, "SELL", trigger=95.0)
        b.set_price("RELIANCE", 94.0)  # below trigger
        p = _pos(b)  # get_positions() evaluates resting stops
        assert p["quantity"] == 0
        assert p["realised"] == -50.0  # (95 - 100) * 10

    def test_sell_stop_does_not_trigger_above(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _buy(b, price=100.0)
        _place_sl(b, "SELL", trigger=95.0)
        b.set_price("RELIANCE", 97.0)  # still above trigger
        assert _pos(b)["quantity"] == 10

    def test_buy_stop_protecting_short_triggers_on_the_way_up(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _sell(b, price=100.0)  # open short
        _place_sl(b, "BUY", trigger=105.0)
        b.set_price("RELIANCE", 106.0)  # above trigger
        p = _pos(b)
        assert p["quantity"] == 0
        assert p["realised"] == -50.0  # short covered 5 higher: (105-100)*10*-1

    def test_cancel_removes_resting_stop(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _buy(b, price=100.0)
        oid = _place_sl(b, "SELL", trigger=95.0)
        b.cancel_order("regular", oid)
        b.set_price("RELIANCE", 90.0)  # would have triggered
        assert _pos(b)["quantity"] == 10  # not closed — stop was cancelled

    def test_modify_moves_the_trigger(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        _buy(b, price=100.0)
        oid = _place_sl(b, "SELL", trigger=95.0)
        b.modify_order("regular", oid, trigger_price=99.0, price=98.0)
        b.set_price("RELIANCE", 98.5)  # below the new trigger, above the old
        assert _pos(b)["quantity"] == 0  # closed at the moved-up stop


# ---------------------------------------------------------------------------
# Position maths
# ---------------------------------------------------------------------------


class TestPositionMaths:
    def test_weighted_average_on_adding(self):
        b = PaperBroker(MDStub())
        _buy(b, qty=10, price=100.0)
        _buy(b, qty=10, price=110.0)
        p = _pos(b)
        assert p["quantity"] == 20
        assert p["averagePrice"] == 105.0

    def test_partial_close_books_realised_keeps_average(self):
        b = PaperBroker(MDStub())
        _buy(b, qty=10, price=100.0)
        _sell(b, qty=4, price=108.0)  # close 4 of 10
        p = _pos(b)
        assert p["quantity"] == 6
        assert p["averagePrice"] == 100.0
        assert p["realised"] == 32.0  # (108 - 100) * 4

    def test_full_close_zeroes_position(self):
        b = PaperBroker(MDStub())
        _buy(b, qty=10, price=100.0)
        _sell(b, qty=10, price=105.0)
        p = _pos(b)
        assert p["quantity"] == 0
        assert p["averagePrice"] == 0.0
        assert p["realised"] == 50.0

    def test_reversal_books_realised_and_opens_other_side(self):
        b = PaperBroker(MDStub())
        _buy(b, qty=10, price=100.0)
        _sell(b, qty=15, price=105.0)  # close 10, open short 5
        p = _pos(b)
        assert p["quantity"] == -5
        assert p["averagePrice"] == 105.0
        assert p["realised"] == 50.0  # only the closed 10 count

    def test_short_round_trip_realised(self):
        b = PaperBroker(MDStub())
        _sell(b, qty=10, price=100.0)  # open short
        _buy(b, qty=10, price=96.0)  # cover lower -> profit
        p = _pos(b)
        assert p["quantity"] == 0
        assert p["realised"] == 40.0  # (100 - 96) * 10

    def test_unrealised_and_pnl_marked_to_ltp(self):
        b = PaperBroker(MDStub())
        _buy(b, qty=10, price=100.0)
        b.set_price("RELIANCE", 107.0)
        p = _pos(b)
        assert p["lastPrice"] == 107.0
        assert p["unrealised"] == 70.0
        assert p["pnl"] == 70.0


# ---------------------------------------------------------------------------
# Delegation & orders
# ---------------------------------------------------------------------------


class TestDelegationAndOrders:
    def test_delegates_unknown_methods_to_market_data_client(self):
        md = MDStub()
        b = PaperBroker(md)
        assert b.get_instruments("NSE")[0]["tradingsymbol"] == "RELIANCE"

    def test_get_orders_reflects_status_transitions(self):
        b = PaperBroker(MDStub())
        b.set_price("RELIANCE", 100.0)
        oid = _buy(b, price=100.0)
        orders = {o["orderId"]: o for o in b.get_orders()}
        assert orders[oid]["status"] == "COMPLETE"
        assert orders[oid]["filledQuantity"] == 10


# ---------------------------------------------------------------------------
# Engine paper mode
# ---------------------------------------------------------------------------


class FakeRisk:
    def __init__(self):
        self.daily_pnl = 0.0

    def can_trade(self):
        return True, "OK"

    def calculate_position_size(self, price, stop_loss):
        return 10

    def set_open_positions(self, count):
        pass

    def update_pnl(self, pnl):
        pass


def _signal():
    return {
        "id": "sig-1",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "direction": "BUY",
        "confidence": 90,
        "entryPrice": 100.0,
        "stopLoss": 95.0,
        "target": 110.0,
        "strategy": "test",
    }


def _paper_engine(monkeypatch, ltp=100.0):
    monkeypatch.setattr(te, "risk_manager", FakeRisk())
    engine = TradingEngine()
    engine.mode = "paper"
    broker = PaperBroker(MDStub())
    broker.set_price("RELIANCE", ltp)
    engine._broker_override = broker
    engine._entry_fill_timeout_seconds = 1
    engine._entry_fill_poll_seconds = 0
    return engine, broker


def test_paper_mode_is_auto_executing():
    engine = TradingEngine()
    for mode, expected in [("auto", True), ("paper", True), ("confirm", False)]:
        engine.mode = mode
        assert engine._auto_execute_enabled() is expected


def test_start_paper_sets_simulator_and_resets(monkeypatch):
    engine = TradingEngine()
    engine.active_trades["OLD"] = {"stale": True}
    monkeypatch.setattr(engine, "_run_loop", lambda: None)
    try:
        engine.start("paper")
        assert isinstance(engine.broker, PaperBroker)
        assert engine.active_trades == {}  # fresh virtual account
        assert engine.mode == "paper"
    finally:
        engine.stop()


def test_live_mode_uses_real_client_by_default():
    engine = TradingEngine()
    # No override -> resolves to the module-level live client.
    assert engine.broker is te.kite_client


def test_paper_execute_signal_opens_simulated_position(monkeypatch):
    engine, broker = _paper_engine(monkeypatch, ltp=100.0)

    assert engine.execute_signal(_signal()) is True
    # Simulated position opened, tracked, with a resting protective stop.
    assert broker.get_positions()["net"][0]["quantity"] == 10
    assert "RELIANCE" in engine.active_trades
    stop_id = engine.active_trades["RELIANCE"]["stop_order_id"]
    orders = {o["orderId"]: o for o in broker.get_orders()}
    assert orders[stop_id]["status"] == "TRIGGER PENDING"


def test_paper_end_to_end_stop_out(monkeypatch):
    engine, broker = _paper_engine(monkeypatch, ltp=100.0)
    engine.execute_signal(_signal())

    # Price falls through the protective stop.
    broker.set_price("RELIANCE", 94.0)
    engine.monitor_positions()

    pos = broker.get_positions()["net"][0]
    assert pos["quantity"] == 0  # simulated stop closed the position
    assert pos["realised"] == -50.0  # (95 stop - 100 entry) * 10
    assert "RELIANCE" not in engine.active_trades  # tracking cleaned up
