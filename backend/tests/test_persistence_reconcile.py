"""Tests for active-trades persistence and startup reconciliation.

Covers:
- ConfigManager.save_active_trades / load_active_trades (datetime handling,
  missing file, corrupt file).
- TradingEngine.reconcile_active_trades against live broker state (drop closed,
  re-place missing stops, keep live stops, exit-pending handling).
- start() wiring and persistence-on-mutation.
"""

import datetime

import pytest

import backend.trading_engine as te
from backend.config import config_manager
from backend.trading_engine import TradingEngine

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeKiteClient:
    def __init__(self):
        self.positions = {"net": []}
        self.orders = []
        self.place_calls = []
        self.cancel_calls = []
        self._next_id = 1

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        oid = f"OID{self._next_id}"
        self._next_id += 1
        return oid

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.cancel_calls.append({"variety": variety, "order_id": order_id})

    def get_positions(self):
        return self.positions

    def get_orders(self):
        return self.orders

    def get_instruments(self, exchange=None):
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 111, "tick_size": 0.05}
        ]


class FakeRiskManager:
    def can_trade(self):
        return True, "OK"

    def set_open_positions(self, count):
        pass

    def update_pnl(self, pnl):
        pass


def _trade(**over):
    base = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime(2026, 9, 2, 10, 0, 0),
        "original_strategy": "test",
        "entry_order_id": "ENTRY1",
        "stop_order_id": "STOP1",
        "exit_pending": False,
        "exit_order_id": None,
        "exchange": "NSE",
    }
    base.update(over)
    return base


def _open_position(qty=10):
    return {
        "tradingsymbol": "RELIANCE",
        "quantity": qty,
        "exchange": "NSE",
        "product": "MIS",
        "lastPrice": 100.0,
        "averagePrice": 100.0,
    }


# ---------------------------------------------------------------------------
# ConfigManager persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    return tmp_path


class TestConfigPersistence:
    def test_round_trip_preserves_fields_and_datetime(self, isolated_config_dir):
        trades = {"RELIANCE": _trade()}
        config_manager.save_active_trades(trades)
        loaded = config_manager.load_active_trades()

        assert set(loaded.keys()) == {"RELIANCE"}
        r = loaded["RELIANCE"]
        assert r["sl"] == 95.0
        assert r["direction"] == "BUY"
        assert r["stop_order_id"] == "STOP1"
        # entry_time restored as a datetime, equal to what we saved.
        assert isinstance(r["entry_time"], datetime.datetime)
        assert r["entry_time"] == datetime.datetime(2026, 9, 2, 10, 0, 0)

    def test_load_missing_file_returns_empty(self, isolated_config_dir):
        assert config_manager.load_active_trades() == {}

    def test_load_corrupt_file_returns_empty(self, isolated_config_dir):
        (isolated_config_dir / "active_trades.json").write_text("{not valid json")
        assert config_manager.load_active_trades() == {}

    def test_save_handles_missing_entry_time(self, isolated_config_dir):
        trades = {"RELIANCE": _trade(entry_time=None)}
        config_manager.save_active_trades(trades)  # must not raise
        loaded = config_manager.load_active_trades()
        assert loaded["RELIANCE"]["entry_time"] is None

    def test_bad_entry_time_string_falls_back_to_now(self, isolated_config_dir):
        (isolated_config_dir / "active_trades.json").write_text(
            '{"RELIANCE": {"entry_time": "not-a-date", "direction": "BUY"}}'
        )
        loaded = config_manager.load_active_trades()
        assert isinstance(loaded["RELIANCE"]["entry_time"], datetime.datetime)


# ---------------------------------------------------------------------------
# reconcile_active_trades
# ---------------------------------------------------------------------------


def _setup_reconcile(monkeypatch, persisted, positions_net, orders):
    fake_client = FakeKiteClient()
    fake_client.positions = {"net": positions_net}
    fake_client.orders = orders
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    saved = {}
    monkeypatch.setattr(te.config_manager, "load_active_trades", lambda: persisted)
    monkeypatch.setattr(
        te.config_manager, "save_active_trades", lambda t: saved.update({"trades": t})
    )

    engine = TradingEngine()
    return engine, fake_client, saved


def test_reconcile_drops_closed_trade_and_cancels_live_stop(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[],  # position is closed
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],  # stop still live
    )
    engine.reconcile_active_trades()

    assert engine.active_trades == {}
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"


def test_reconcile_drops_closed_trade_no_cancel_when_stop_gone(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[],
        orders=[{"orderId": "STOP1", "status": "COMPLETE"}],  # stop already gone
    )
    engine.reconcile_active_trades()

    assert engine.active_trades == {}
    assert fake_client.cancel_calls == []


def test_reconcile_replaces_missing_stop_on_open_position(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id=None)},
        positions_net=[_open_position(10)],
        orders=[],
    )
    engine.reconcile_active_trades()

    assert "RELIANCE" in engine.active_trades
    sl_orders = [c for c in fake_client.place_calls if c["order_type"] == "SL"]
    assert len(sl_orders) == 1
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "OID1"


def test_reconcile_replaces_stop_that_is_no_longer_live(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="OLD")},
        positions_net=[_open_position(10)],
        orders=[{"orderId": "OLD", "status": "CANCELLED"}],
    )
    engine.reconcile_active_trades()

    assert len(fake_client.place_calls) == 1
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "OID1"


def test_reconcile_keeps_live_stop_without_replacing(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[_open_position(10)],
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],
    )
    engine.reconcile_active_trades()

    assert fake_client.place_calls == []  # no re-place
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "STOP1"


def test_reconcile_keeps_working_exit_order(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={
            "RELIANCE": _trade(
                stop_order_id="STOP1", exit_pending=True, exit_order_id="EXIT1"
            )
        },
        positions_net=[_open_position(10)],
        orders=[
            {"orderId": "STOP1", "status": "TRIGGER PENDING"},
            {"orderId": "EXIT1", "status": "OPEN"},
        ],
    )
    engine.reconcile_active_trades()

    trade = engine.active_trades["RELIANCE"]
    assert trade["exit_pending"] is True
    assert trade["exit_order_id"] == "EXIT1"


def test_reconcile_clears_stale_exit_pending(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={
            "RELIANCE": _trade(
                stop_order_id="STOP1", exit_pending=True, exit_order_id="EXIT1"
            )
        },
        positions_net=[_open_position(10)],
        orders=[
            {"orderId": "STOP1", "status": "TRIGGER PENDING"},
            {"orderId": "EXIT1", "status": "COMPLETE"},  # exit already done/gone
        ],
    )
    engine.reconcile_active_trades()

    trade = engine.active_trades["RELIANCE"]
    assert trade["exit_pending"] is False
    assert trade["exit_order_id"] is None


def test_reconcile_empty_persisted_is_noop(monkeypatch):
    engine, fake_client, saved = _setup_reconcile(
        monkeypatch, persisted={}, positions_net=[_open_position()], orders=[]
    )
    engine.reconcile_active_trades()

    assert engine.active_trades == {}
    assert fake_client.place_calls == []
    assert saved == {}  # nothing persisted either


def test_reconcile_persists_result(monkeypatch):
    engine, fake_client, saved = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[_open_position(10)],
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],
    )
    engine.reconcile_active_trades()

    assert "trades" in saved
    assert "RELIANCE" in saved["trades"]


def test_reconcile_preserves_entry_time(monkeypatch):
    et = datetime.datetime(2026, 9, 2, 9, 30, 0)
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1", entry_time=et)},
        positions_net=[_open_position(10)],
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],
    )
    engine.reconcile_active_trades()

    assert engine.active_trades["RELIANCE"]["entry_time"] == et


# ---------------------------------------------------------------------------
# start() wiring and persistence-on-mutation
# ---------------------------------------------------------------------------


def test_start_invokes_reconcile(monkeypatch):
    monkeypatch.setattr(te, "kite_client", FakeKiteClient())
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    engine = TradingEngine()
    called = {"reconcile": False}
    monkeypatch.setattr(
        engine, "reconcile_active_trades", lambda: called.__setitem__("reconcile", True)
    )
    monkeypatch.setattr(engine, "_run_loop", lambda: None)  # don't spin the loop

    engine.start("confirm")
    try:
        assert called["reconcile"] is True
        assert engine.running is True
    finally:
        engine.stop()


def test_start_reconcile_failure_does_not_block(monkeypatch):
    monkeypatch.setattr(te, "kite_client", FakeKiteClient())
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    engine = TradingEngine()

    def boom():
        raise RuntimeError("kite down")

    monkeypatch.setattr(engine, "reconcile_active_trades", boom)
    monkeypatch.setattr(engine, "_run_loop", lambda: None)

    engine.start("confirm")
    try:
        assert engine.running is True  # startup proceeded despite reconcile failure
    finally:
        engine.stop()


def test_monitor_positions_persists(monkeypatch):
    fake_client = FakeKiteClient()
    fake_client.positions = {"net": [_open_position(10)]}
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    saved = {}
    monkeypatch.setattr(
        te.config_manager, "save_active_trades", lambda t: saved.update({"trades": t})
    )

    engine = TradingEngine()
    engine.active_trades["RELIANCE"] = _trade(stop_order_id="STOP1")
    engine.monitor_positions()

    assert "trades" in saved  # persisted in the finally block
