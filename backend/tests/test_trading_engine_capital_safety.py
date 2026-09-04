import datetime
import threading

from backend.trading_engine import TradingEngine


class FakeKiteClient:
    def __init__(self):
        self.positions = {"net": []}
        self.orders = []
        self.place_calls = []
        self.cancel_calls = []
        self.modify_calls = []
        self._next_id = 1
        self.margins = {"equity": {"available": {"live_balance": 10_000}}}

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

    def modify_order(self, **kwargs):
        self.modify_calls.append(kwargs)

    def get_positions(self):
        return self.positions

    def get_margins(self):
        return self.margins

    def get_orders(self):
        return self.orders

    def get_instruments(self, exchange=None):
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 111, "tick_size": 0.05},
            {"tradingsymbol": "INFY", "instrument_token": 222, "tick_size": 0.05},
        ]


class FakeScanner:
    """Stub scanner: replays preset signals to on_signal, and returns a fixed
    evaluation for _reevaluate_positions."""

    def __init__(self, signals=None, evaluation=None):
        self._signals = signals or []
        self._evaluation = evaluation or {"buy_signals": 0, "sell_signals": 0}
        self.scan_calls = []

    def scan_watchlist(self, watchlist, on_signal=None):
        self.scan_calls.append(list(watchlist))
        if on_signal:
            for sig in self._signals:
                on_signal(sig)
        return []

    def evaluate_position(self, symbol, token):
        return self._evaluation


class FakeRiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.open_positions = 0
        self.pnl_updates = []

    def can_trade(self):
        return True, "OK"

    def calculate_position_size(self, price, stop_loss, available_margin=None):
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
    assert fake_client.place_calls[1]["order_type"] == "SL"
    assert fake_client.place_calls[1]["trigger_price"] == 95.0
    assert fake_client.place_calls[1]["price"] == 94.05
    assert "RELIANCE" in engine.active_trades
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "OID2"


def test_execute_signal_does_not_submit_when_margin_cannot_fund_entry(monkeypatch):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.margins = {"equity": {"available": {"live_balance": 99}}}
    fake_risk = FakeRiskManager()
    fake_risk.calculate_position_size = lambda price, stop_loss, available_margin: 0
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()

    assert engine.execute_signal(_sample_signal()) is False
    assert fake_client.place_calls == []


def test_execute_signal_treats_explicit_zero_live_balance_as_unavailable_margin(
    monkeypatch,
):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.margins = {"equity": {"available": {"live_balance": 0}, "net": 10_000}}
    fake_risk = FakeRiskManager()
    fake_risk.calculate_position_size = lambda price, stop_loss, available_margin: 0
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()

    assert engine.execute_signal(_sample_signal()) is False
    assert fake_client.place_calls == []


def test_execute_signal_rejects_low_expected_profit_after_costs(monkeypatch):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)
    monkeypatch.setattr(
        te.config_manager,
        "get_risk_config",
        lambda: {
            "transactionCostFilterEnabled": True,
            "brokeragePercentPerOrder": 0.03,
            "brokerageCapPerOrder": 20,
            "statutoryChargesPercentRoundTrip": 0.015,
        },
    )

    engine = TradingEngine()
    signal = {**_sample_signal(), "target": 100.01}

    assert engine.execute_signal(signal) is False
    assert fake_client.place_calls == []


def test_execute_signal_allows_low_expected_profit_when_cost_filter_disabled(
    monkeypatch,
):
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
    monkeypatch.setattr(
        te.config_manager,
        "get_risk_config",
        lambda: {"transactionCostFilterEnabled": False},
    )

    engine = TradingEngine()
    engine._entry_fill_timeout_seconds = 1
    engine._entry_fill_poll_seconds = 0
    signal = {**_sample_signal(), "target": 100.01}

    assert engine.execute_signal(signal) is True
    assert len(fake_client.place_calls) == 2


def test_concurrent_entries_do_not_reuse_reserved_margin(monkeypatch):
    import backend.trading_engine as te

    class BlockingKiteClient(FakeKiteClient):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def place_order(self, **kwargs):
            if not self.place_calls:
                self.entered.set()
                self.release.wait(timeout=1)
            return super().place_order(**kwargs)

    fake_client = BlockingKiteClient()
    fake_client.margins = {"equity": {"available": {"live_balance": 1_000}}}
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
    fake_risk.calculate_position_size = lambda price, stop_loss, available_margin: (
        10 if available_margin >= 1_000 else 0
    )
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    first = threading.Thread(target=engine.execute_signal, args=(_sample_signal(),))
    second_signal = {**_sample_signal(), "tradingsymbol": "INFY"}

    first.start()
    assert fake_client.entered.wait(timeout=1)
    second_result = []
    second = threading.Thread(
        target=lambda: second_result.append(engine.execute_signal(second_signal))
    )
    second.start()
    fake_client.release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert second_result == [False]
    assert len(fake_client.place_calls) == 2
    assert fake_client.place_calls[0]["tradingsymbol"] == "RELIANCE"
    assert fake_client.place_calls[1]["order_type"] == "SL"


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


def test_tighten_to_breakeven_modifies_broker_side_stop(monkeypatch):
    """_tighten_to_breakeven must call modify_order to move the SL-M trigger."""
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    monkeypatch.setattr(te, "kite_client", fake_client)

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

    engine._tighten_to_breakeven("RELIANCE")

    # In-memory SL updated
    assert engine.active_trades["RELIANCE"]["sl"] == 100.0
    # Broker-side SL-M order modified
    assert len(fake_client.modify_calls) == 1
    assert fake_client.modify_calls[0]["order_id"] == "STOP1"
    assert fake_client.modify_calls[0]["trigger_price"] == 100.0


def test_adopted_position_gets_protective_stop(monkeypatch):
    """Positions adopted in auto mode must get a broker-side SL-M placed."""
    import backend.trading_engine as te
    from backend.config import config_manager

    fake_client = FakeKiteClient()
    fake_client.positions = {
        "net": [
            {
                "tradingsymbol": "INFY",
                "quantity": 5,
                "exchange": "NSE",
                "product": "MIS",
                "lastPrice": 200.0,
                "averagePrice": 200.0,
                "realised": 0.0,
                "unrealised": 0.0,
            }
        ]
    }
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine.mode = "auto"
    engine.monitor_positions()

    # Should have adopted INFY
    assert "INFY" in engine.active_trades
    trade = engine.active_trades["INFY"]
    # Protective stop order should have been placed
    assert trade["stop_order_id"] is not None
    assert len(fake_client.place_calls) == 1
    assert fake_client.place_calls[0]["order_type"] == "SL"
    assert fake_client.place_calls[0]["trigger_price"] == 197.0
    assert fake_client.place_calls[0]["price"] == 195.05
    # Stop trigger should match the calculated SL
    risk_config = config_manager.get_risk_config()
    sl_pct = risk_config.get("defaultStopLossPercent", 1.5)
    expected_sl = round(200.0 * (1 - sl_pct / 100), 2)
    assert fake_client.place_calls[0]["trigger_price"] == expected_sl
    assert trade["exit_pending"] is False
    assert trade["exit_order_id"] is None


def test_exit_order_uses_market_when_ltp_zero(monkeypatch):
    """When LTP is 0, _place_exit_order should use MARKET order type instead of LIMIT at price 0."""
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    monkeypatch.setattr(te, "kite_client", fake_client)

    engine = TradingEngine()

    position = {
        "tradingsymbol": "RELIANCE",
        "quantity": 10,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": 0,
    }

    engine._place_exit_order(position, "RELIANCE", "Square off")

    assert len(fake_client.place_calls) == 1
    call = fake_client.place_calls[0]
    assert call["order_type"] == "MARKET"
    assert "price" not in call


def test_exit_order_uses_limit_when_ltp_available(monkeypatch):
    """When LTP is available, _place_exit_order should use LIMIT order type."""
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    monkeypatch.setattr(te, "kite_client", fake_client)

    engine = TradingEngine()

    position = {
        "tradingsymbol": "RELIANCE",
        "quantity": 10,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": 100.0,
    }

    engine._place_exit_order(position, "RELIANCE", "Target")

    assert len(fake_client.place_calls) == 1
    call = fake_client.place_calls[0]
    assert call["order_type"] == "LIMIT"
    assert call["price"] > 0


def test_sync_exit_pending_removes_trade_on_complete(monkeypatch):
    """When an exit order is COMPLETE, _sync_exit_pending_status should remove the trade."""
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.orders = [
        {"orderId": "EXIT1", "status": "COMPLETE", "filledQuantity": 10}
    ]
    monkeypatch.setattr(te, "kite_client", fake_client)

    engine = TradingEngine()
    engine.active_trades["RELIANCE"] = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "stop_order_id": None,
        "exit_pending": True,
        "exit_order_id": "EXIT1",
    }

    engine._sync_exit_pending_status("RELIANCE")

    # Trade should be removed entirely
    assert "RELIANCE" not in engine.active_trades


def test_duplicate_execute_signal_is_blocked(monkeypatch):
    """Concurrent execute_signal calls for the same symbol must not both proceed."""
    import threading

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

    results = []

    def call_execute():
        res = engine.execute_signal(_sample_signal())
        results.append(res)

    t1 = threading.Thread(target=call_execute)
    t2 = threading.Thread(target=call_execute)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Exactly one should succeed, the other should be skipped
    assert results.count(True) == 1
    assert results.count(False) == 1

    # Only one entry order should have been placed
    entry_orders = [c for c in fake_client.place_calls if c["order_type"] == "LIMIT"]
    assert len(entry_orders) == 1


def test_concurrent_place_exit_order_only_fires_once(monkeypatch):
    """Two threads calling _place_exit_order for the same symbol must produce only one exit order."""
    import threading

    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    monkeypatch.setattr(te, "kite_client", fake_client)

    engine = TradingEngine()
    engine.active_trades["RELIANCE"] = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "stop_order_id": None,
        "exit_pending": False,
        "exit_order_id": None,
    }

    position = {
        "tradingsymbol": "RELIANCE",
        "quantity": 10,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": 94.0,
    }

    barrier = threading.Barrier(2)

    def call_exit():
        barrier.wait()
        engine._place_exit_order(position, "RELIANCE", "SL")

    t1 = threading.Thread(target=call_exit)
    t2 = threading.Thread(target=call_exit)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Only one exit order should be placed
    exit_orders = [
        c for c in fake_client.place_calls if c["transaction_type"] == "SELL"
    ]
    assert len(exit_orders) == 1


def test_pending_entries_prevents_adoption(monkeypatch):
    """monitor_positions must not adopt a position whose symbol is in _pending_entries."""
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
                "averagePrice": 100.0,
                "realised": 0.0,
                "unrealised": 0.0,
            }
        ]
    }
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine.mode = "auto"
    # Simulate an in-flight entry for RELIANCE
    engine._pending_entries.add("RELIANCE")

    engine.monitor_positions()

    # Should NOT have adopted RELIANCE
    assert "RELIANCE" not in engine.active_trades
    # No protective stop should have been placed
    assert len(fake_client.place_calls) == 0


def test_exit_pending_resets_on_place_order_failure(monkeypatch):
    """If place_order raises during exit, exit_pending must be reset so the next cycle can retry."""
    import backend.trading_engine as te

    call_count = 0

    def failing_place_order(**kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("Network error")

    fake_client = FakeKiteClient()
    fake_client.place_order = failing_place_order
    monkeypatch.setattr(te, "kite_client", fake_client)

    engine = TradingEngine()
    engine.active_trades["RELIANCE"] = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "stop_order_id": None,
        "exit_pending": False,
        "exit_order_id": None,
    }

    position = {
        "tradingsymbol": "RELIANCE",
        "quantity": 10,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": 94.0,
    }

    try:
        engine._place_exit_order(position, "RELIANCE", "SL")
    except Exception:
        pass

    # exit_pending must be reset so the next cycle can retry
    assert engine.active_trades["RELIANCE"]["exit_pending"] is False
    assert call_count == 2


class SequencedKiteClient(FakeKiteClient):
    """Returns a different positions snapshot on each get_positions call, so a
    test can simulate the book changing mid-tick (e.g. a broker stop filling
    between the monitor snapshot and the pre-exit re-read)."""

    def __init__(self, position_sequence):
        super().__init__()
        self._sequence = position_sequence
        self._calls = 0

    def get_positions(self):
        idx = min(self._calls, len(self._sequence) - 1)
        self._calls += 1
        return {"net": self._sequence[idx]}


def test_monitor_skips_exit_when_broker_stop_already_closed(monkeypatch):
    """#1 — phantom-short race: the snapshot shows an open position at its stop,
    but by the time we re-read before exiting the broker stop has already closed
    it. The app must NOT place an exit (which would open an opposite position);
    it should clean up instead."""
    import backend.trading_engine as te

    open_pos = [
        {
            "tradingsymbol": "RELIANCE",
            "quantity": 10,
            "exchange": "NSE",
            "product": "MIS",
            "lastPrice": 94.0,  # <= sl of 95 -> stop hit
            "realised": 0.0,
            "unrealised": -60.0,
            "averagePrice": 100.0,
        }
    ]
    # 1st get_positions (snapshot) = open; 2nd (pre-exit re-read) = flat.
    fake_client = SequencedKiteClient([open_pos, []])
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

    # No exit order placed — the position was already flat on re-read.
    assert fake_client.place_calls == []
    # Trade cleaned up, lingering broker stop cancelled.
    assert "RELIANCE" not in engine.active_trades
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"


def test_trade_lock_not_held_during_order_io(monkeypatch):
    """#2 — monitor_positions must not hold _trade_lock while making Kite calls.
    While place_order runs (invoked from the exit path), another thread must be
    able to acquire the lock. Before the fix, the whole loop ran under the lock
    and this would block/time out."""
    import threading

    import backend.trading_engine as te

    engine = TradingEngine()
    other_acquired = {"ok": None}

    def place_order_probe(**kwargs):
        def grab():
            got = engine._trade_lock.acquire(timeout=1.0)
            other_acquired["ok"] = got
            if got:
                engine._trade_lock.release()

        t = threading.Thread(target=grab)
        t.start()
        t.join()
        return "EXIT1"

    fake_client = FakeKiteClient()
    fake_client.place_order = place_order_probe
    fake_client.positions = {
        "net": [
            {
                "tradingsymbol": "RELIANCE",
                "quantity": 10,
                "exchange": "NSE",
                "product": "MIS",
                "lastPrice": 111.0,  # >= target of 110 -> target hit
                "realised": 0.0,
                "unrealised": 0.0,
                "averagePrice": 100.0,
            }
        ]
    }
    fake_risk = FakeRiskManager()
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine.active_trades["RELIANCE"] = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "stop_order_id": None,
        "exit_pending": False,
        "exit_order_id": None,
    }

    engine.monitor_positions()

    # The probe thread acquired the lock during the Kite call -> lock not held.
    assert other_acquired["ok"] is True
