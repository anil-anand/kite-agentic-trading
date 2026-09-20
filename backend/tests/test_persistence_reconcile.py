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
from backend.time_utils import as_utc
from backend.trading_engine import TradingEngine

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeKiteClient:
    def __init__(self):
        self.account_id = "dummy"
        from backend.broker_models import ExecutionNamespace

        self.namespace = ExecutionNamespace.LIVE
        self.positions = {"net": []}
        self.orders = []
        self.place_calls = []
        self.cancel_calls = []
        self.flatten_calls = []
        self._next_id = 1

    def get_broker_snapshot(self):
        import datetime

        from backend.broker_models import (
            BrokerSnapshot,
            ExecutionNamespace,
            SnapshotQuality,
            normalize_orders_response,
            normalize_positions_response,
        )

        positions = normalize_positions_response(
            self.positions, namespace=ExecutionNamespace.LIVE, account_id="dummy"
        )
        orders = normalize_orders_response(
            self.orders, namespace=ExecutionNamespace.LIVE, account_id="dummy"
        )
        return BrokerSnapshot(
            namespace=ExecutionNamespace.LIVE,
            account_id="dummy",
            positions=positions.net,
            day_positions=positions.day,
            current_orders=orders.orders,
            fills=tuple(),
            positions_quality=SnapshotQuality.COMPLETE,
            orders_quality=SnapshotQuality.COMPLETE,
            fills_quality=SnapshotQuality.COMPLETE,
            fetched_at=datetime.datetime.now(datetime.timezone.utc),
            errors=[],
        )

    def get_positions_snapshot(self):
        from backend.broker_models import (
            normalize_positions_response,
        )

        payload = self.get_positions()
        payload = {
            "net": [
                dict(
                    row,
                    instrument_token=111 if row["tradingsymbol"] == "RELIANCE" else 222,
                )
                for row in payload["net"]
            ]
        }
        payload["day"] = list(payload["net"])
        return normalize_positions_response(
            payload, namespace=self.namespace, account_id=self.account_id
        )

    def get_current_orders_snapshot(self):
        from backend.broker_models import ExecutionNamespace, normalize_orders_response

        return normalize_orders_response(
            self.orders, namespace=ExecutionNamespace.LIVE, account_id="dummy"
        )

    def get_fills_snapshot(self):
        from backend.broker_models import ExecutionNamespace, normalize_fills_response

        return normalize_fills_response(
            [], namespace=ExecutionNamespace.LIVE, account_id="dummy"
        )

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        oid = f"OID{self._next_id}"
        self._next_id += 1
        status = (
            "COMPLETE"
            if kwargs.get("order_type") in ["LIMIT", "MARKET"]
            else "TRIGGER PENDING"
        )
        qty = kwargs.get("quantity", 0) if status == "COMPLETE" else 0
        self.orders.append(
            {
                "orderId": oid,
                "order_id": oid,
                "status": status,
                "quantity": kwargs.get("quantity", 0),
                "filledQuantity": qty,
                "tradingsymbol": kwargs.get("tradingsymbol", "RELIANCE"),
                "exchange": kwargs.get("exchange", "NSE"),
                "product": kwargs.get("product", "MIS"),
                "transaction_type": kwargs.get("transaction_type", "SELL"),
                "order_type": kwargs.get("order_type", "SL"),
                "instrument_token": 111,
                "trigger_price": kwargs.get("trigger_price", 0),
                "price": kwargs.get("price", 0),
            }
        )
        return oid

    def emergency_flatten_position(self, **kwargs):
        self.place_calls.append(kwargs)
        if "tradingsymbol" in kwargs:
            self.flatten_calls.append(kwargs["tradingsymbol"])
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
    def reconcile_state(self):
        pass

    def can_trade(self):
        return True, "OK"

    def can_accept_position(
        self, symbol, direction, qty, price, active_trades, open_orders
    ):
        return True, "OK"

    def set_open_positions(self, count):
        pass

    def update_pnl(self, pnl):
        pass

    def update_from_positions(self, positions):
        pass

    def update_from_position_snapshot(self, snapshot):
        pass


def _trade(**over):
    base = {
        "tradingsymbol": "RELIANCE",
        "namespace": "LIVE",
        "account_id": "dummy",
        "instrument_id": "111",
        "product": "MIS",
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
        "average_price": 100.0,
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
        assert r["entry_time"] == as_utc(datetime.datetime(2026, 9, 2, 10, 0, 0))

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

    def test_bad_entry_time_stays_unknown(self, isolated_config_dir):
        (isolated_config_dir / "active_trades.json").write_text(
            '{"RELIANCE": {"entry_time": "not-a-date", "direction": "BUY"}}'
        )
        loaded = config_manager.load_active_trades()
        assert loaded["RELIANCE"]["entry_time"] is None

    def test_observation_time_survives_restart_without_inventing_execution_time(
        self, isolated_config_dir
    ):
        observed = as_utc(datetime.datetime(2026, 9, 2, 10, 0))
        config_manager.save_active_trades(
            {"RELIANCE": _trade(entry_time=None, entry_observed_at=observed)}
        )
        loaded = config_manager.load_active_trades()["RELIANCE"]
        assert loaded["entry_time"] is None
        assert loaded["entry_observed_at"] == observed
        assert (
            observed.isoformat()
            in (isolated_config_dir / "active_trades.json").read_text()
        )

    @pytest.mark.parametrize("bad_timestamp", ["not-a-date", True, 123, []])
    def test_invalid_persisted_times_remain_unknown(
        self, isolated_config_dir, bad_timestamp
    ):
        config_manager.save_active_trades(
            {
                "RELIANCE": _trade(
                    entry_time=bad_timestamp,
                    entry_observed_at=bad_timestamp,
                    last_reeval_time=bad_timestamp,
                )
            }
        )
        loaded = config_manager.load_active_trades()["RELIANCE"]
        for field in ("entry_time", "entry_observed_at", "last_reeval_time"):
            assert loaded[field] is None

    def test_write_is_atomic_no_temp_file_left(self, isolated_config_dir):
        config_manager.save_active_trades({"RELIANCE": _trade()})
        leftovers = list(isolated_config_dir.glob(".active_trades.json.*"))
        assert leftovers == []
        # The real file is present and valid JSON.
        assert (isolated_config_dir / "active_trades.json").exists()
        assert "RELIANCE" in config_manager.load_active_trades()

    def test_concurrent_writes_never_corrupt(self, isolated_config_dir):
        import threading

        def writer(n):
            config_manager.save_active_trades({f"SYM{n}": _trade(sl=float(n))})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # File is always complete/parseable (one writer's snapshot won), and no
        # temp files leaked.
        loaded = config_manager.load_active_trades()
        assert len(loaded) == 1
        assert list(isolated_config_dir.glob(".active_trades.json.*")) == []

    def test_overwrites_previous_content(self, isolated_config_dir):
        config_manager.save_active_trades({"RELIANCE": _trade()})
        config_manager.save_active_trades({"INFY": _trade()})
        loaded = config_manager.load_active_trades()
        assert set(loaded.keys()) == {"INFY"}


# ---------------------------------------------------------------------------
# reconcile_active_trades
# ---------------------------------------------------------------------------


def _setup_reconcile(monkeypatch, persisted, positions_net, orders):
    fake_client = FakeKiteClient()
    for o in orders:
        if "tradingsymbol" not in o:
            o["tradingsymbol"] = "RELIANCE"
        if "order_id" not in o:
            o["order_id"] = o.get("orderId", "ORD1")
        if "exchange" not in o:
            o["exchange"] = "NSE"
        if "product" not in o:
            o["product"] = "MIS"
        if "transaction_type" not in o:
            o["transaction_type"] = "SELL"
        if "order_type" not in o:
            o["order_type"] = "SL"
        if "quantity" not in o:
            o["quantity"] = 10
        o.setdefault("instrument_token", 111)
        o.setdefault("trigger_price", 95)
        o.setdefault("price", 94.05)
        o.setdefault("filled_quantity", 10 if o["status"] == "COMPLETE" else 0)
    for symbol, trade in persisted.items():
        entry_id = trade.get("entry_order_id")
        if entry_id and not any(o.get("order_id") == entry_id for o in orders):
            orders.append(
                {
                    "order_id": entry_id,
                    "status": "COMPLETE",
                    "tradingsymbol": symbol,
                    "exchange": "NSE",
                    "product": "MIS",
                    "instrument_token": trade.get("instrument_id", 111),
                    "transaction_type": trade.get("direction", "BUY"),
                    "order_type": "LIMIT",
                    "quantity": trade.get("quantity", 10),
                    "filled_quantity": trade.get("quantity", 10),
                    "pending_quantity": 0,
                }
            )
    fake_client.positions = {"net": positions_net}
    fake_client.orders = orders
    monkeypatch.setattr(te, "execution_gateway", fake_client)
    monkeypatch.setattr(te, "kite_client", fake_client)
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    saved = {}
    monkeypatch.setattr(te.config_manager, "load_active_trades", lambda: persisted)
    monkeypatch.setattr(
        te.config_manager, "save_active_trades", lambda t: saved.update({"trades": t})
    )

    engine = TradingEngine()
    return engine, fake_client, saved


def test_reconcile_retains_closed_trade_until_stop_cancellation_confirmed(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[],  # position is closed
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],  # stop still live
    )
    engine.reconcile_active_trades()

    assert engine.active_trades["RELIANCE"]["cleanup_pending"] is True
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"
    fake_client.orders[0]["status"] = "CANCELLED"
    engine.reconcile_active_trades()
    # Phase 5 retains a migrated owner until accounting is also reconciled;
    # terminal order cleanup alone cannot prove a complete lifecycle closure.
    assert (
        engine.active_trades["RELIANCE"]["recovery_state"]
        == "ACCOUNTING_RECONCILIATION_PENDING"
    )
    monkeypatch.setattr(engine, "_journal_external_close", lambda symbol: True)
    engine.reconcile_active_trades()
    assert engine.active_trades == {}


def test_reconcile_retains_flat_accounting_until_repaired_when_stop_gone(monkeypatch):
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id="STOP1")},
        positions_net=[],
        orders=[{"orderId": "STOP1", "status": "COMPLETE"}],  # stop already gone
    )
    engine.reconcile_active_trades()

    assert (
        engine.active_trades["RELIANCE"]["recovery_state"]
        == "ACCOUNTING_RECONCILIATION_PENDING"
    )
    assert fake_client.cancel_calls == []
    monkeypatch.setattr(engine, "_journal_external_close", lambda symbol: True)
    engine.reconcile_active_trades()
    assert engine.active_trades == {}


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


def test_reconcile_quarantines_direction_flip_until_orders_are_resolved(monkeypatch):
    # A reversed position cannot erase ownership of the still-live old stop.
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(direction="BUY", stop_order_id="STOP1")},
        positions_net=[_open_position(-5)],  # now short
        orders=[{"orderId": "STOP1", "status": "TRIGGER PENDING"}],
    )
    engine.reconcile_active_trades()

    assert engine.active_trades["RELIANCE"]["ownership_quarantined"] is True
    assert fake_client.cancel_calls[0]["order_id"] == "STOP1"
    assert fake_client.place_calls == []  # no wrong-side stop placed


def test_reconcile_isolates_a_malformed_record(monkeypatch):
    # BAD is missing 'sl', which raises when re-placing its stop. It must be
    # retained as an unresolved ownership obligation without aborting GOOD.
    good = _trade(stop_order_id="GOODSTOP")
    bad = _trade(stop_order_id=None)
    del bad["sl"]
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"GOOD": good, "BAD": bad},
        positions_net=[
            {**_open_position(10), "tradingsymbol": "GOOD"},
            {**_open_position(10), "tradingsymbol": "BAD"},
        ],
        orders=[{"orderId": "GOODSTOP", "status": "TRIGGER PENDING"}],
    )
    engine.reconcile_active_trades()
    assert "GOOD" in engine.active_trades
    assert engine.active_trades["BAD"]["broker_reconciliation_pending"] is True
    assert engine._reconciliation_pending is True


def test_reconcile_flattens_trade_when_stop_replace_fails(monkeypatch):
    # If re-placing a protective stop fails before an order id is returned,
    # it latches a recoverable emergency intent.  An ambiguous/failed attempt
    # never claims flatness or issues an untracked second market order.
    engine, fake_client, _ = _setup_reconcile(
        monkeypatch,
        persisted={"RELIANCE": _trade(stop_order_id=None)},
        positions_net=[_open_position(10)],
        orders=[],
    )

    def boom(**kwargs):
        raise RuntimeError("order rejected")

    fake_client.place_order = boom
    engine.reconcile_active_trades()

    assert engine.active_trades["RELIANCE"]["broker_reconciliation_pending"] is True
    assert engine._reconciliation_pending is True
    assert fake_client.flatten_calls == []
    assert engine.active_trades["RELIANCE"]["exit_pending"] is True
    assert engine.active_trades["RELIANCE"]["exit_intent_id"]


# ---------------------------------------------------------------------------
# start() wiring and persistence-on-mutation
# ---------------------------------------------------------------------------


def test_start_invokes_reconcile(monkeypatch):
    monkeypatch.setattr(te, "execution_gateway", FakeKiteClient())
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    engine = TradingEngine()
    called = {"reconcile": False}
    monkeypatch.setattr(
        engine, "reconcile_active_trades", lambda: called.__setitem__("reconcile", True)
    )
    monkeypatch.setattr(engine, "_run_loop", lambda: None)  # don't spin the loop
    monkeypatch.setattr(engine, "_supervisor_loop", lambda: None)
    monkeypatch.setattr(engine, "_normal_management_loop", lambda: None)

    engine.start("confirm")
    try:
        assert called["reconcile"] is True
        assert engine.running is True
    finally:
        engine.stop()


def test_start_reconcile_failure_pauses_entries_but_keeps_supervision(monkeypatch):
    monkeypatch.setattr(te, "execution_gateway", FakeKiteClient())
    monkeypatch.setattr(te, "risk_manager", FakeRiskManager())

    engine = TradingEngine()
    monkeypatch.setattr(engine, "_supervisor_loop", lambda: None)
    monkeypatch.setattr(engine, "_normal_management_loop", lambda: None)

    def boom():
        raise RuntimeError("kite down")

    monkeypatch.setattr(engine, "reconcile_active_trades", boom)
    monkeypatch.setattr(engine, "_run_loop", lambda: None)

    engine.start("confirm")
    try:
        assert engine.running is False
        assert engine.status()["entryPaused"] is True
        assert engine.status()["supervisionActive"] is True
    finally:
        engine.stop()


def test_monitor_positions_persists(monkeypatch):
    fake_client = FakeKiteClient()
    fake_client.positions = {"net": [_open_position(10)]}
    monkeypatch.setattr(te, "execution_gateway", fake_client)
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
