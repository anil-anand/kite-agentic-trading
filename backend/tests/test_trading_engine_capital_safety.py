import datetime

from backend.trading_engine import TradingEngine


class FakeKiteClient:
    def __init__(self):
        self.positions = {"net": []}
        self.orders = []
        self.place_calls = []
        self.cancel_calls = []
        self.modify_calls = []
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

    def modify_order(self, **kwargs):
        self.modify_calls.append(kwargs)

    def get_positions(self):
        return self.positions

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
    assert fake_client.place_calls[1]["order_type"] == "SL"
    assert fake_client.place_calls[1]["trigger_price"] == 95.0
    assert fake_client.place_calls[1]["price"] == 94.05
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


# ---------------------------------------------------------------------------
# _reevaluate_positions — the four graduated exit rules
# ---------------------------------------------------------------------------


def _open_position(ltp, avg=100.0, qty=10):
    return {
        "tradingsymbol": "RELIANCE",
        "quantity": qty,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": ltp,
        "averagePrice": avg,
        "realised": 0.0,
        "unrealised": 0.0,
    }


def _tracked_trade(sl=95.0, entry_price=100.0, minutes_ago=1, stop="STOP1"):
    return {
        "sl": sl,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": entry_price,
        "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago),
        "original_strategy": "test",
        "stop_order_id": stop,
        "exit_pending": False,
        "exit_order_id": None,
        "exchange": "NSE",
    }


def _setup_reeval(monkeypatch, ltp, evaluation, mode="auto", trade=None):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_client.positions = {"net": [_open_position(ltp)]}
    fake_scanner = FakeScanner(evaluation=evaluation)
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "scanner", fake_scanner)

    engine = TradingEngine()
    engine.mode = mode
    engine.active_trades["RELIANCE"] = trade or _tracked_trade()
    return engine, fake_client


def test_reevaluate_rule1_exits_on_thesis_invalidation_auto(monkeypatch):
    # 3 opposing, 0 supporting -> thesis invalidated -> exit in auto mode.
    engine, fake_client = _setup_reeval(
        monkeypatch, ltp=98.0, evaluation={"buy_signals": 0, "sell_signals": 3}
    )
    engine._reevaluate_positions()

    sells = [c for c in fake_client.place_calls if c["transaction_type"] == "SELL"]
    assert len(sells) == 1
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"
    assert engine.active_trades["RELIANCE"]["exit_pending"] is True


def test_reevaluate_rule1_no_exit_in_confirm_mode(monkeypatch):
    engine, fake_client = _setup_reeval(
        monkeypatch,
        ltp=98.0,
        evaluation={"buy_signals": 0, "sell_signals": 3},
        mode="confirm",
    )
    engine._reevaluate_positions()

    assert fake_client.place_calls == []
    assert "RELIANCE" in engine.active_trades
    assert engine.active_trades["RELIANCE"]["exit_pending"] is False


def test_reevaluate_rule2_weak_conviction_exit(monkeypatch):
    # 0 supporting, in loss, held >= weak_exit_mins (15) -> exit. Only 1 opposing
    # so Rule 1 (needs >= 2 opposing) does not fire.
    engine, fake_client = _setup_reeval(
        monkeypatch,
        ltp=95.0,  # below entry 100 -> in loss
        evaluation={"buy_signals": 0, "sell_signals": 1},
        trade=_tracked_trade(minutes_ago=20),
    )
    engine._reevaluate_positions()

    sells = [c for c in fake_client.place_calls if c["transaction_type"] == "SELL"]
    assert len(sells) == 1


def test_reevaluate_rule2_holds_when_not_yet_timed_out(monkeypatch):
    # Same weak setup but held only 5 mins (< 15) -> no exit yet.
    engine, fake_client = _setup_reeval(
        monkeypatch,
        ltp=95.0,
        evaluation={"buy_signals": 0, "sell_signals": 1},
        trade=_tracked_trade(minutes_ago=5),
    )
    engine._reevaluate_positions()

    assert fake_client.place_calls == []
    assert "RELIANCE" in engine.active_trades


def test_reevaluate_rule3_tightens_to_breakeven(monkeypatch):
    # Held >= breakeven_mins (45), still supported -> tighten SL to entry, no exit.
    engine, fake_client = _setup_reeval(
        monkeypatch,
        ltp=101.0,
        evaluation={"buy_signals": 1, "sell_signals": 0},
        trade=_tracked_trade(sl=95.0, minutes_ago=50),
    )
    engine._reevaluate_positions()

    assert fake_client.place_calls == []  # no exit
    assert len(fake_client.modify_calls) == 1
    assert fake_client.modify_calls[0]["order_id"] == "STOP1"
    assert fake_client.modify_calls[0]["trigger_price"] == 100.0
    assert engine.active_trades["RELIANCE"]["sl"] == 100.0


def test_reevaluate_rule4_holds_when_thesis_valid(monkeypatch):
    # Supported, recent, not in loss -> hold: no exit, no SL change.
    engine, fake_client = _setup_reeval(
        monkeypatch,
        ltp=105.0,
        evaluation={"buy_signals": 2, "sell_signals": 0},
        trade=_tracked_trade(sl=95.0, minutes_ago=5),
    )
    engine._reevaluate_positions()

    assert fake_client.place_calls == []
    assert fake_client.modify_calls == []
    assert engine.active_trades["RELIANCE"]["sl"] == 95.0


# ---------------------------------------------------------------------------
# scan_and_trade — confidence gating and auto-execution
# ---------------------------------------------------------------------------


def _signal(symbol, confidence):
    return {
        "id": f"sig-{symbol}-{confidence}",
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "direction": "BUY",
        "confidence": confidence,
        "entryPrice": 100.0,
        "stopLoss": 95.0,
        "target": 110.0,
        "strategy": "test",
    }


def _setup_scan(monkeypatch, signals, mode="auto", can_trade=True):
    import backend.trading_engine as te

    fake_client = FakeKiteClient()
    fake_scanner = FakeScanner(signals=signals)
    fake_risk = FakeRiskManager()
    fake_risk.can_trade = lambda: (can_trade, "OK" if can_trade else "blocked")
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "scanner", fake_scanner)
    monkeypatch.setattr(te, "risk_manager", fake_risk)

    engine = TradingEngine()
    engine.mode = mode
    engine.dynamic_watchlist = ["RELIANCE", "INFY"]  # skip the screener path
    engine._reevaluate_positions = lambda: None  # isolate from re-eval

    pushed, executed = [], []
    engine._push_signal = lambda sig: pushed.append(sig)
    engine.execute_signal = lambda sig: (executed.append(sig), True)[1]
    return engine, pushed, executed


def test_scan_pushes_at_70_not_below(monkeypatch):
    engine, pushed, executed = _setup_scan(
        monkeypatch, [_signal("RELIANCE", 65), _signal("INFY", 75)]
    )
    engine.scan_and_trade()

    pushed_syms = {s["tradingsymbol"] for s in pushed}
    assert pushed_syms == {"INFY"}  # 65 is below the 70 display threshold


def test_scan_no_autotrade_below_80(monkeypatch):
    engine, pushed, executed = _setup_scan(monkeypatch, [_signal("INFY", 75)])
    engine.scan_and_trade()

    assert len(pushed) == 1
    assert executed == []  # 75 < 80 auto-trade threshold


def test_scan_autotrades_at_80_in_auto_mode(monkeypatch):
    engine, pushed, executed = _setup_scan(monkeypatch, [_signal("RELIANCE", 85)])
    engine.scan_and_trade()

    assert [s["tradingsymbol"] for s in executed] == ["RELIANCE"]


def test_scan_no_autotrade_in_confirm_mode(monkeypatch):
    engine, pushed, executed = _setup_scan(
        monkeypatch, [_signal("RELIANCE", 85)], mode="confirm"
    )
    engine.scan_and_trade()

    assert len(pushed) == 1
    assert executed == []


def test_scan_no_autotrade_when_cannot_trade(monkeypatch):
    engine, pushed, executed = _setup_scan(
        monkeypatch, [_signal("RELIANCE", 85)], can_trade=False
    )
    engine.scan_and_trade()

    assert len(pushed) == 1  # still shown to the user
    assert executed == []  # but not auto-executed


def test_scan_skips_already_active_symbol(monkeypatch):
    engine, pushed, executed = _setup_scan(monkeypatch, [_signal("RELIANCE", 85)])
    engine.active_trades["RELIANCE"] = _tracked_trade()
    engine.scan_and_trade()

    assert len(pushed) == 1
    assert executed == []  # already active -> not re-entered
