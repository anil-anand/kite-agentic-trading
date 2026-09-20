"""Review-five producer/consumer regressions using a stubbed Kite SDK transport."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.broker_models import ExecutionNamespace
from backend.config import config_manager
from backend.execution_gateway import ExecutionGateway
from backend.journal import TradeJournal
from backend.kite_client import KiteClient
from backend.risk_manager import RiskManager
from backend.time_utils import now_utc
from backend.trading_engine import TradingEngine


class ScriptedSDK:
    def __init__(self):
        self.book = []
        self.executions = []
        self.position_rows = []
        self.calls = []
        self.instrument_calls = []
        self.fail_positions = False
        self.fail_orders = False
        self.fail_fills = False
        self.hide_entry = False
        self.hide_stops = False
        self.after_entry = lambda: None

    def positions(self):
        if self.fail_positions:
            raise TimeoutError("positions unavailable")
        return {
            "net": deepcopy(self.position_rows),
            "day": deepcopy(self.position_rows),
        }

    def orders(self):
        if self.fail_orders:
            raise TimeoutError("orders unavailable")
        return deepcopy(
            [
                o
                for o in self.book
                if not (
                    (self.hide_entry and o["order_id"] == "O1")
                    or (self.hide_stops and o["order_type"] == "SL")
                )
            ]
        )

    def trades(self):
        if self.fail_fills:
            raise TimeoutError("fills unavailable")
        return deepcopy(self.executions)

    def quote(self, instruments):
        return {
            name: {"last_price": 100, "timestamp": now_utc()} for name in instruments
        }

    def margins(self, *args, **kwargs):
        return {"equity": {"available": {"live_balance": 10000}}}

    def instruments(self, exchange=None):
        self.instrument_calls.append(exchange)
        return [
            {
                "tradingsymbol": "RELIANCE",
                "exchange": exchange,
                "instrument_token": 111 if exchange == "NSE" else 222,
                "tick_size": 0.05,
            }
        ]

    def place_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        order_id = f"O{len(self.book) + 1}"
        self.book.append(
            {
                **kwargs,
                "order_id": order_id,
                "instrument_token": 111,
                "filled_quantity": 0,
                "pending_quantity": kwargs["quantity"],
                "status": "TRIGGER PENDING" if kwargs["order_type"] == "SL" else "OPEN",
            }
        )
        if order_id == "O1":
            self.after_entry()
        return order_id

    def cancel_order(self, variety, order_id, **kwargs):
        # Default cancellation remains unresolved. Tests explicitly deliver its
        # outcome, as an acknowledgement does not prove terminal cancellation.
        return order_id

    def fill_entry(self, quantity=10, status="COMPLETE", residual=None):
        self.book[0].update(
            filled_quantity=quantity,
            status=status,
            pending_quantity=0
            if status in {"COMPLETE", "CANCELLED"}
            else 10 - quantity,
        )
        residual = quantity if residual is None else residual
        self.position_rows = [
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "quantity": residual,
                "average_price": 101,
                "last_price": 100,
                "day_buy_quantity": quantity,
                "day_sell_quantity": quantity - residual,
                "buy_quantity": quantity,
                "sell_quantity": quantity - residual,
                "buy_value": quantity * 101,
                "sell_value": (quantity - residual) * 99,
                "realised": 0,
                "unrealised": 0,
            }
        ]
        self.executions = (
            [
                {
                    "trade_id": "F1",
                    "order_id": "O1",
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "instrument_token": 111,
                    "transaction_type": "BUY",
                    "quantity": quantity,
                    "average_price": 101,
                    "fill_timestamp": now_utc().replace(microsecond=0),
                }
            ]
            if quantity
            else []
        )


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    import backend.execution_gateway as gateway_module
    import backend.journal as journal_module
    import backend.kite_client as client_module
    import backend.trading_engine as engine_module

    sdk = ScriptedSDK()
    client = KiteClient()
    monkeypatch.setattr(client, "kite", sdk)
    monkeypatch.setattr(client, "account_id", "acct-A")
    monkeypatch.setattr(client, "namespace", ExecutionNamespace.LIVE)
    monkeypatch.setattr(client, "instruments_cache", None)

    def transport(
        action, priority, is_order=False, order_reconciler=None, *args, **kwargs
    ):
        return action(*args, **kwargs)

    monkeypatch.setattr(client_module.broker_gateway, "execute", transport)
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    config = dict(config_manager.get_risk_config())
    config.update(
        maxDailyTrades=100,
        maxTradesPerSymbolPerDay=100,
        maxSimultaneousPositions=10,
        maxGrossExposure=500000,
        maxNetExposure=100000,
        maxSectorExposure=500000,
        maxCorrelatedExposure=500000,
        maxSingleSymbolExposure=500000,
    )
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    journal = TradeJournal(str(tmp_path / "journal.db"))
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    monkeypatch.setattr(risk, "can_trade", lambda: (True, "OK"))
    gateway = ExecutionGateway()
    for module in (engine_module, gateway_module):
        monkeypatch.setattr(module, "kite_client", client)
        monkeypatch.setattr(module, "risk_manager", risk)
        monkeypatch.setattr(module, "journal", journal)
    monkeypatch.setattr(journal_module, "journal", journal)
    monkeypatch.setattr(engine_module, "execution_gateway", gateway)
    monkeypatch.setattr(engine_module.scanner, "evaluate_position", lambda *args: {})
    engine = TradingEngine()
    engine.mode = "confirm"
    engine._entry_fill_timeout_seconds = -1
    signal = dict(
        id="S1",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        direction="BUY",
        entryPrice=100,
        stopLoss=95,
        target=110,
        quantity=10,
        timestamp=now_utc().isoformat(),
    )
    return SimpleNamespace(
        engine=engine, sdk=sdk, risk=risk, journal=journal, signal=signal, client=client
    )


def unknown_entry(env):
    env.sdk.after_entry = lambda: setattr(env.sdk, "fail_positions", True)
    assert env.engine.execute_signal(env.signal) is False
    assert env.engine.active_trades["RELIANCE"]["entry_state"] == "RECOVERY_REQUIRED"
    assert env.engine._reserved_entry_margin == 1000
    env.sdk.fail_positions = False


@pytest.mark.parametrize("quantity", [4, 10])
def test_timeout_protects_position_during_repeated_fill_outages_and_later_recovers(
    lifecycle, quantity
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry(quantity, "CANCELLED" if quantity == 4 else "COMPLETE")
    e.sdk.fail_fills = True
    for _ in range(3):
        e.engine.monitor_positions()
        assert len(e.sdk.calls) == 2
        assert e.sdk.calls[-1]["quantity"] == quantity
        assert e.engine.active_trades["RELIANCE"]["entry_state"] == "RECOVERY_REQUIRED"
        assert e.journal.get_trades() == []
        assert e.risk.pending_entry_reservation_count == 1
        assert e.engine._reserved_entry_margin == 0
    e.sdk.fail_fills = False
    e.engine.monitor_positions()
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["entry_state"] == "OPEN"
    assert trade["quantity"] == quantity
    assert e.journal.get_trades()[0]["entry_price"] == 101
    assert e.journal.get_trades()[0]["stop_order_id"] == "O2"
    assert e.risk.pending_entry_reservation_count == 0
    assert trade["reserved_margin"] == 0
    e.engine.monitor_positions()
    assert len(e.sdk.calls) == 2


def test_never_submitted_stop_is_placed_but_acknowledged_invisible_stop_is_not_duplicated(
    lifecycle,
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.sdk.hide_stops = True
    for _ in range(3):
        e.engine.monitor_positions()
    assert len(e.sdk.calls) == 2
    assert e.engine.active_trades["RELIANCE"]["protection_attempt_order_id"] == "O2"
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert len(e.sdk.calls) == 2
    e.sdk.hide_stops = False
    restarted.monitor_positions()
    assert restarted.active_trades["RELIANCE"]["entry_state"] == "OPEN"
    assert len(e.sdk.calls) == 2


@pytest.mark.parametrize("book_failure", ["unavailable", "absent"])
@pytest.mark.parametrize("polling", [True, False])
@pytest.mark.parametrize("terminal", ["COMPLETE", "CANCELLED"])
def test_partial_entry_with_unknown_order_retains_ownership_until_terminal(
    lifecycle, book_failure, polling, terminal
):
    e = lifecycle

    def partial():
        e.sdk.fill_entry(4, "OPEN")
        if book_failure == "unavailable":
            e.sdk.fail_orders = True
        else:
            e.sdk.hide_entry = True

    e.sdk.after_entry = partial
    e.engine._entry_fill_timeout_seconds = 1 if polling else -1
    e.engine._entry_fill_poll_seconds = 0
    assert e.engine.execute_signal(e.signal) is False
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["entry_remainder_pending"] is True
    assert trade["entry_state"] == "RECOVERY_REQUIRED"
    assert e.journal.get_trades() == []
    assert e.risk.pending_entry_reservation_count == 1
    assert e.engine._reserved_entry_margin == 600
    assert len(e.sdk.calls) == 2
    e.engine.monitor_positions()
    assert len(e.sdk.calls) == 2
    e.sdk.fail_orders = e.sdk.hide_entry = False
    e.sdk.fill_entry(10 if terminal == "COMPLETE" else 4, terminal)
    # Stop resizing cancellation must be terminally observed before replacement.
    e.sdk.cancel_order = lambda variety, order_id, **kw: next(
        o.update(status="CANCELLED", pending_quantity=0)
        for o in e.sdk.book
        if o["order_id"] == order_id
    )
    e.engine.monitor_positions()
    row = e.journal.get_trades()[0]
    assert row["quantity"] == (10 if terminal == "COMPLETE" else 4)
    assert row["entry_price"] == 101
    assert e.risk.pending_entry_reservation_count == 0
    assert e.engine._reserved_entry_margin == 0
    before = len(e.sdk.calls)
    e.engine.monitor_positions()
    assert len(e.sdk.calls) == before


def test_rejected_exchange_cannot_poison_subsequent_canonical_entry(lifecycle):
    e = lifecycle
    assert e.engine.execute_signal({**e.signal, "exchange": "BSE"}) is False
    assert e.engine.execute_signal({**e.signal, "product": "CNC"}) is False
    assert e.sdk.instrument_calls == []
    assert e.engine.execute_signal({**e.signal, "instrumentToken": 222}) is False
    unknown_entry(e)
    assert e.engine.active_trades["RELIANCE"]["instrument_id"] == "111"
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    assert e.journal.get_trades()[0]["instrument_id"] == "111"
    assert len(e.sdk.calls) == 2


@pytest.mark.parametrize("flat_fill", [False, True])
def test_recovery_releases_cash_after_verified_flat_and_restart(lifecycle, flat_fill):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry(
        10 if flat_fill else 0, "COMPLETE" if flat_fill else "CANCELLED", residual=0
    )
    if flat_fill:
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "F2",
                "order_id": "MANUAL",
                "transaction_type": "SELL",
                "average_price": 99,
            }
        )
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted.monitor_positions()
    restarted.monitor_positions()
    assert restarted.active_trades == {}
    assert restarted._reserved_entry_margin == 0
    assert e.risk.pending_entry_reservation_count == 0
    if flat_fill:
        assert e.journal.get_trades()[0]["gross_pnl"] == -20


@pytest.mark.parametrize("change", ["account", "product", "namespace"])
@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_persisted_trade_cannot_manage_same_symbol_after_identity_switch(
    lifecycle, change, legacy
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    trade = e.engine.active_trades["RELIANCE"]
    if legacy:
        for key in ("account_id", "namespace", "instrument_id", "identity_verified"):
            trade.pop(key, None)
    if change == "account":
        e.client.account_id = "acct-B"
    elif change == "product":
        e.sdk.position_rows[0]["product"] = "CNC"
    else:
        e.client.namespace = ExecutionNamespace.DEV
    trade["sl"] = 105
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted.monitor_positions()
    assert restarted.active_trades["RELIANCE"]["ownership_quarantined"] is True
    assert len(e.sdk.calls) == 1


def test_legacy_identity_migrates_only_from_canonical_persisted_entry(lifecycle):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    trade = e.engine.active_trades["RELIANCE"]
    for key in ("account_id", "namespace", "instrument_id", "identity_verified"):
        trade.pop(key)
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert restarted.active_trades["RELIANCE"]["account_id"] == "acct-A"
    assert restarted.active_trades["RELIANCE"]["instrument_id"] == "111"
    assert len(e.sdk.calls) == 2


@pytest.mark.parametrize("partial", [False, True])
def test_stop_replacement_preserves_journal_only_fill_attribution(lifecycle, partial):
    import json

    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    predecessor_quantity = 4 if partial else 0
    e.sdk.book[1].update(
        status="CANCELLED", filled_quantity=predecessor_quantity, pending_quantity=0
    )
    if partial:
        e.sdk.position_rows[0].update(quantity=6, day_sell_quantity=4)
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "STOP-PART",
                "order_id": "O2",
                "transaction_type": "SELL",
                "quantity": 4,
                "average_price": 94,
            }
        )
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    trade = restarted.active_trades["RELIANCE"]
    row = e.journal.get_trades()[0]
    assert row["stop_order_id"] == "O3"
    history = json.loads(row["execution_linkage_history"])
    assert history[0]["order_id"] == "O2"
    assert history[0]["predecessor"]["filled_quantity"] == predecessor_quantity
    assert restarted._persist_execution_linkage(trade) is True
    assert (
        len(json.loads(e.journal.get_trade(row["id"])["execution_linkage_history"]))
        == 1
    )
    e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10)
    e.sdk.book[2].update(
        status="COMPLETE", filled_quantity=10 - predecessor_quantity, pending_quantity=0
    )
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "STOP-FINAL",
            "order_id": "O3",
            "transaction_type": "SELL",
            "quantity": 10 - predecessor_quantity,
            "average_price": 93,
        }
    )
    journal_only = TradingEngine()
    journal_only._reconcile_journal_trades()
    closed = e.journal.get_trade(row["id"])
    assert closed["status"] == "CLOSED"
    assert closed["exit_reason"] == "stop_loss"
    assert closed["exit_price"] == (93.4 if partial else 93)
    assert closed["gross_pnl"] == (-76 if partial else -80)


def test_rejected_exit_replacement_and_persistence_failure_are_recoverable(
    lifecycle, monkeypatch
):
    import sqlite3

    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    e.sdk.cancel_order = lambda variety, order_id, **kw: next(
        o.update(status="CANCELLED", pending_quantity=0)
        for o in e.sdk.book
        if o["order_id"] == order_id
    )
    position = e.engine._positions()[0]
    e.engine._place_exit_order(position, "RELIANCE", "Target")
    row = e.journal.get_trades()[0]
    assert row["exit_order_id"] == "O3"
    e.sdk.book[2].update(status="REJECTED", pending_quantity=0)
    e.engine._sync_exit_pending_status("RELIANCE")
    real_write = e.journal._log_event_inner

    def fail_linkage(conn, trade_id, timestamp, event, details):
        if event == "execution_linkage_updated":
            raise sqlite3.OperationalError("injected linkage failure")
        return real_write(conn, trade_id, timestamp, event, details)

    monkeypatch.setattr(e.journal, "_log_event_inner", fail_linkage)
    e.engine._place_exit_order(position, "RELIANCE", "Target")
    assert e.engine.active_trades["RELIANCE"]["exit_order_id"] == "O4"
    assert e.engine.active_trades["RELIANCE"]["execution_linkage_pending"] is True
    assert e.journal.get_trade(row["id"])["exit_order_id"] == "O3"
    assert e.engine.execute_signal({**e.signal, "tradingsymbol": "INFY"}) is False
    monkeypatch.setattr(e.journal, "_log_event_inner", real_write)
    e.engine.monitor_positions()
    assert not e.engine.active_trades["RELIANCE"].get("execution_linkage_pending")
    assert e.journal.get_trade(row["id"])["exit_order_id"] == "O4"
    assert len(e.sdk.calls) == 4


def test_successful_recovery_cash_release_restores_next_entry_sizing(
    lifecycle, monkeypatch
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    # The sizing consumer must see the broker's available margin without the
    # old pending cash hold. Reject after capture to avoid submitting another entry.
    seen = []
    e.engine._instrument_map["INFY"] = "333"
    e.engine._tick_size_map["INFY"] = 0.05

    def sizing(qty, price, available, stop):
        seen.append(available)
        return 0

    monkeypatch.setattr(e.risk, "cap_advisory_quantity", sizing)
    assert e.engine.execute_signal({**e.signal, "tradingsymbol": "INFY"}) is False
    assert seen == [10000]


def test_fill_outage_retains_financial_owner_after_emergency_reduction(
    lifecycle, monkeypatch
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.sdk.fail_fills = True
    submit = e.sdk.place_order

    def fail_stop(**kwargs):
        if kwargs["order_type"] == "SL":
            raise ValueError("definitively rejected stop")
        return submit(**kwargs)

    monkeypatch.setattr(e.sdk, "place_order", fail_stop)
    e.engine.monitor_positions()
    assert (
        e.engine.active_trades["RELIANCE"]["recovery_state"]
        == "EMERGENCY_REDUCTION_REQUIRED"
    )
    e.engine.monitor_positions()
    assert e.engine.active_trades["RELIANCE"]["exit_order_id"] == "O2"
    e.sdk.book[1].update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
    e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "REDUCTION",
            "order_id": "O2",
            "transaction_type": "SELL",
            "average_price": 99,
        }
    )
    for _ in range(2):
        e.engine.monitor_positions()
        assert "RELIANCE" in e.engine.active_trades
        assert e.risk.pending_entry_reservation_count == 1
        assert e.journal.get_trades() == []
    e.sdk.fail_fills = False
    e.engine.monitor_positions()
    assert e.engine.active_trades == {}
    assert e.journal.get_trades()[0]["gross_pnl"] == -20
    assert e.risk.pending_entry_reservation_count == 0
    assert len(e.sdk.calls) == 2


def test_partial_exit_predecessor_and_replacement_survive_journal_only_restart(
    lifecycle,
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.engine.monitor_positions()
    e.sdk.cancel_order = lambda variety, order_id, **kw: next(
        o.update(status="CANCELLED", pending_quantity=0)
        for o in e.sdk.book
        if o["order_id"] == order_id
    )
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    e.sdk.book[2].update(status="CANCELLED", filled_quantity=4, pending_quantity=0)
    e.sdk.position_rows[0].update(quantity=6, day_sell_quantity=4)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "EXIT-PART",
            "order_id": "O3",
            "transaction_type": "SELL",
            "quantity": 4,
            "average_price": 110,
        }
    )
    e.engine._sync_exit_pending_status("RELIANCE")
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    assert e.sdk.calls[-1]["quantity"] == 6
    e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10)
    e.sdk.book[3].update(status="COMPLETE", filled_quantity=6, pending_quantity=0)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "EXIT-FINAL",
            "order_id": "O4",
            "transaction_type": "SELL",
            "quantity": 6,
            "average_price": 109,
        }
    )
    TradingEngine()._reconcile_journal_trades()
    row = e.journal.get_trades()[0]
    assert row["exit_order_id"] == "O4"
    assert row["exit_reason"] == "app_exit"
    assert row["exit_price"] == 109.4
    assert row["gross_pnl"] == 84


def test_unobserved_replacement_retains_attempt_over_terminal_predecessor(
    lifecycle, monkeypatch
):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry(4, "OPEN")
    e.engine.monitor_positions()
    assert e.engine.active_trades["RELIANCE"]["stop_order_id"] == "O2"
    e.sdk.fill_entry()
    e.sdk.cancel_order = lambda variety, order_id, **kw: next(
        o.update(status="CANCELLED", pending_quantity=0)
        for o in e.sdk.book
        if o["order_id"] == order_id
    )
    original = e.sdk.orders
    monkeypatch.setattr(
        e.sdk, "orders", lambda: [o for o in original() if o["order_id"] != "O3"]
    )
    for _ in range(3):
        e.engine.monitor_positions()
    assert len(e.sdk.calls) == 3
    assert e.engine.active_trades["RELIANCE"]["protection_attempt_order_id"] == "O3"
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert len(e.sdk.calls) == 3
    monkeypatch.setattr(e.sdk, "orders", original)
    restarted.monitor_positions()
    assert restarted.active_trades["RELIANCE"]["entry_state"] == "OPEN"
    assert len(e.sdk.calls) == 3


def test_full_restart_retains_count_slot_for_absent_entry_order(lifecycle, monkeypatch):
    import backend.execution_gateway as gateway_module
    import backend.trading_engine as engine_module

    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry(4, "OPEN")
    e.sdk.hide_entry = True
    e.engine.monitor_positions()
    config = dict(config_manager.get_risk_config(), maxDailyTrades=1)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    fresh_risk = RiskManager()
    fresh_risk.reconciliation_status = "RECONCILED"
    fresh_risk._get_correlation = lambda *_: 0
    monkeypatch.setattr(engine_module, "risk_manager", fresh_risk)
    monkeypatch.setattr(gateway_module, "risk_manager", fresh_risk)
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted.reconcile_active_trades()
    assert fresh_risk.pending_entry_reservation_count == 1
    assert restarted._reserved_entry_margin == 600
    accepted, reason = fresh_risk.can_accept_position(
        "INFY", "BUY", 1, 100, e.client.get_broker_snapshot()
    )
    assert accepted is False
    assert "MAX_DAILY_TRADES" in reason
    e.sdk.hide_entry = False
    e.sdk.fill_entry(4, "CANCELLED")
    restarted.monitor_positions()
    assert fresh_risk.pending_entry_reservation_count == 0
    assert restarted._reserved_entry_margin == 0
    assert e.journal.get_trades()[0]["quantity"] == 4
