"""Retained execution facts repair accounting without faking live snapshots."""

import pytest

import backend.trading_engine as te
from backend.broker_models import (
    BrokerDataUnavailable,
    ExecutionNamespace,
    normalize_fills_response,
    unavailable_fill_snapshot,
)
from backend.journal import TradeJournal
from backend.trading_engine import TradingEngine

POSITION_KEY = "LIVE:account-a:NSE:123:RELIANCE:MIS:epoch-1"


def trade_record():
    return {
        "tradingsymbol": "RELIANCE",
        "namespace": "LIVE",
        "account_id": "account-a",
        "exchange": "NSE",
        "product": "MIS",
        "instrument_id": "123",
        "position_epoch": "epoch-1",
        "entry_order_id": "entry-1",
        "exit_order_id": "exit-1",
        "direction": "BUY",
        "quantity": 10,
        "entry_price": 100.0,
        "entry_time": "2026-09-18T04:00:00+00:00",
        "exit_reason": "Target",
    }


def fill_row(order_id, side, price, timestamp, fill_id=None):
    return {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "MIS",
        "instrument_token": 123,
        "trade_id": fill_id or f"fill-{order_id}",
        "order_id": order_id,
        "transaction_type": side,
        "quantity": 10,
        "average_price": price,
        "fill_timestamp": timestamp,
    }


def snapshot(rows):
    return normalize_fills_response(
        rows, namespace=ExecutionNamespace.LIVE, account_id="account-a"
    )


def setup_engine(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    monkeypatch.setattr(te, "journal", journal)
    engine = TradingEngine()
    monkeypatch.setattr(engine, "_push_log", lambda *args, **kwargs: None)
    return engine, journal


@pytest.mark.parametrize("outage", [False, True])
def test_prior_session_fills_repair_close_after_broker_day_rollover(
    tmp_path, monkeypatch, outage
):
    engine, journal = setup_engine(tmp_path, monkeypatch)
    trade = trade_record()
    rows = [
        fill_row("entry-1", "BUY", 100, "2026-09-18T04:00:00+00:00"),
        fill_row("exit-1", "SELL", 103, "2026-09-18T05:00:00+00:00"),
    ]
    monkeypatch.setattr(engine, "_fill_snapshot", lambda: snapshot(rows))
    engine._record_lifecycle_fills("RELIANCE", trade)
    assert (
        len(
            journal.get_position_fills(
                POSITION_KEY, broker_order_ids={"entry-1", "exit-1"}
            )
        )
        == 2
    )
    current = unavailable_fill_snapshot("offline") if outage else snapshot([])
    monkeypatch.setattr(engine, "_fill_snapshot", lambda: current)
    price, reason, executed_at, costs = engine._reconcile_execution("RELIANCE", trade)
    assert price == 103
    assert reason == "Target"
    assert costs["gross_pnl"] == 30
    assert executed_at == "2026-09-18T05:00:00+00:00"
    # Retained historical facts have no authority to improve live risk quality.
    assert engine._fill_snapshot() is current
    if outage:
        with pytest.raises(BrokerDataUnavailable):
            engine._fill_snapshot().require_complete()


def test_fill_capture_excludes_unowned_day_history_and_other_epochs(
    tmp_path, monkeypatch
):
    engine, journal = setup_engine(tmp_path, monkeypatch)
    trade = trade_record()
    rows = [
        fill_row("entry-1", "BUY", 100, "2026-09-18T04:00:00+00:00"),
        fill_row("old-entry", "BUY", 80, "2026-09-18T03:55:00+00:00"),
        fill_row("unrelated-exit", "SELL", 101, "2026-09-18T04:01:00+00:00"),
    ]
    monkeypatch.setattr(engine, "_fill_snapshot", lambda: snapshot(rows))
    engine._record_lifecycle_fills("RELIANCE", trade)
    captured = journal.get_position_fills(
        POSITION_KEY, broker_order_ids={row["order_id"] for row in rows}
    )
    assert [row["broker_order_id"] for row in captured] == ["entry-1"]
    next_trade = {**trade, "position_epoch": "epoch-2", "entry_order_id": "entry-2"}
    engine._record_lifecycle_fills("RELIANCE", next_trade)
    assert (
        journal.get_position_fills(
            POSITION_KEY.replace("epoch-1", "epoch-2"),
            broker_order_ids={"entry-1", "entry-2"},
        )
        == []
    )


def test_live_and_retained_fills_are_deduplicated_and_conflicts_fail_closed(
    tmp_path, monkeypatch
):
    engine, _ = setup_engine(tmp_path, monkeypatch)
    trade = trade_record()
    row = fill_row("entry-1", "BUY", 100, "2026-09-18T04:00:00+00:00")
    monkeypatch.setattr(engine, "_fill_snapshot", lambda: snapshot([row]))
    engine._record_lifecycle_fills("RELIANCE", trade)
    assert len(engine._accounting_fills("RELIANCE", trade)) == 1
    row["quantity"] = 20
    with pytest.raises(ValueError, match="retained/live fill conflict"):
        engine._accounting_fills("RELIANCE", trade)


def test_accounting_excludes_a_live_fill_owned_by_another_epoch(tmp_path, monkeypatch):
    engine, journal = setup_engine(tmp_path, monkeypatch)
    old_key = POSITION_KEY.replace("epoch-1", "epoch-0")
    journal.create_order_intent(
        intent_id="old-exit-intent",
        position_key=old_key,
        intent_type="EXIT",
        role="REDUCTION",
        side="SELL",
        quantity=10,
    )
    journal.prepare_order_attempt(
        intent_id="old-exit-intent", attempt_id="old-attempt", attempt_tag="old-tag"
    )
    journal.record_order_attempt_state(
        "old-attempt", "FILLED", broker_order_id="old-exit"
    )
    rows = [
        fill_row("entry-1", "BUY", 100, "2026-09-18T04:00:00+00:00"),
        fill_row("old-exit", "SELL", 103, "2026-09-18T04:01:00+00:00"),
    ]
    monkeypatch.setattr(engine, "_fill_snapshot", lambda: snapshot(rows))
    fills = engine._accounting_fills("RELIANCE", trade_record())
    assert [fill.broker_order_id for fill in fills] == ["entry-1"]
    assert engine._reconcile_execution("RELIANCE", trade_record())[1] == "UNRECONCILED"


def test_empty_ledger_does_not_turn_fill_outage_into_zero_fill_proof(
    tmp_path, monkeypatch
):
    engine, _ = setup_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(
        engine, "_fill_snapshot", lambda: unavailable_fill_snapshot("offline")
    )
    with pytest.raises(BrokerDataUnavailable):
        engine._accounting_fills("RELIANCE", trade_record())
