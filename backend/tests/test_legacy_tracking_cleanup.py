"""Retire obsolete legacy tracking only after complete, current broker proof."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

import backend.trading_engine as te
from backend.broker_models import SnapshotQuality
from backend.config import config_manager
from backend.tests.test_persistence_reconcile import (
    _open_position,
    _setup_reconcile,
    _trade,
)
from backend.time_utils import now_utc
from backend.trading_engine import TradingEngine


@pytest.fixture
def legacy(monkeypatch, tmp_path):
    trade = _trade(
        entry_time=now_utc() - timedelta(days=2),
        broker_reconciliation_pending=True,
        ownership_quarantined=True,
        recovery_state="LEGACY_IDENTITY_UNRESOLVED",
    )
    trade.pop("account_id")
    trade.pop("instrument_id")
    engine, client, saved = _setup_reconcile(
        monkeypatch, {"RELIANCE": trade}, positions_net=[], orders=[]
    )
    client.orders = []  # Old IDs have aged out of the broker's daily book.
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    original = {"RELIANCE": trade}
    monkeypatch.setattr(
        config_manager, "load_active_trades", lambda: saved.get("trades", original)
    )
    logs = []
    monkeypatch.setattr(engine, "_push_log", lambda message, **kw: logs.append(message))
    return engine, client, trade, saved, logs, tmp_path


def test_flat_old_legacy_record_is_archived_removed_and_stays_removed(legacy):
    engine, client, trade, saved, logs, path = legacy
    engine.reconcile_active_trades()
    assert not engine.active_trades and not engine._reconciliation_pending
    assert saved["trades"] == {}
    archive = json.loads((path / "retired_active_trades.json").read_text())
    assert len(archive) == 1
    record = next(iter(archive.values()))
    assert record["trade"]["entry_order_id"] == trade["entry_order_id"]
    assert "exit_price" not in record["trade"]
    assert record["verification"]["positions_snapshot_id"]
    assert record["verification"]["orders_snapshot_id"]
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert not restarted.active_trades and not restarted._reconciliation_pending
    assert (
        len([line for line in logs if "Removed inactive saved tracking" in line]) == 1
    )
    assert client.place_calls == client.cancel_calls == []


@pytest.mark.parametrize("exposure", ["position", "working_order", "other_product"])
def test_live_exposure_or_any_working_symbol_order_keeps_tracking(legacy, exposure):
    engine, client, _, _, _, path = legacy
    if exposure == "position":
        client.positions["net"] = [_open_position()]
    else:
        client.orders = [
            {
                "order_id": "LIVE-ORDER",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "CNC" if exposure == "other_product" else "MIS",
                "instrument_token": 111,
                "transaction_type": "BUY",
                "order_type": "LIMIT",
                "quantity": 10,
                "filled_quantity": 0,
                "pending_quantity": 10,
                "status": "OPEN",
            }
        ]
    engine.reconcile_active_trades()
    assert "RELIANCE" in engine.active_trades
    assert not (path / "retired_active_trades.json").exists()
    assert client.place_calls == client.cancel_calls == []


@pytest.mark.parametrize(
    "quality",
    [SnapshotQuality.UNAVAILABLE, SnapshotQuality.PARTIAL, SnapshotQuality.STALE],
)
@pytest.mark.parametrize("source", ["_order_snapshot", "_position_snapshot"])
def test_incomplete_broker_data_never_retires_tracking(
    legacy, monkeypatch, source, quality
):
    engine, _, _, _, _, path = legacy
    original = getattr(engine, source)
    monkeypatch.setattr(
        engine, source, lambda **kwargs: replace(original(**kwargs), quality=quality)
    )
    engine.reconcile_active_trades()
    assert "RELIANCE" in engine.active_trades and engine._reconciliation_pending
    assert not (path / "retired_active_trades.json").exists()


def test_cached_complete_data_does_not_prove_flatness(legacy, monkeypatch):
    engine, _, _, _, _, path = legacy
    original = engine._position_snapshot
    monkeypatch.setattr(
        engine,
        "_position_snapshot",
        lambda **kwargs: replace(
            original(**kwargs), fetched_at=now_utc() - timedelta(minutes=2)
        ),
    )
    engine.reconcile_active_trades()
    assert "RELIANCE" in engine.active_trades
    assert not (path / "retired_active_trades.json").exists()


def test_late_fill_after_initial_flat_read_keeps_tracking(legacy, monkeypatch):
    engine, client, _, _, _, path = legacy
    original = engine._order_snapshot
    reads = 0

    def orders(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            client.positions["net"] = [_open_position()]
        return original(**kwargs)

    monkeypatch.setattr(engine, "_order_snapshot", orders)
    engine.reconcile_active_trades()
    assert reads == 2
    assert "RELIANCE" in engine.active_trades
    assert not (path / "retired_active_trades.json").exists()


@pytest.mark.parametrize(
    "blocker",
    ["unknown_entry", "same_day", "wrong_account", "unknown_account", "durable_intent"],
)
def test_uncertain_or_mismatched_ownership_is_not_auto_retired(
    legacy, monkeypatch, blocker
):
    engine, client, trade, _, _, path = legacy
    if blocker == "unknown_entry":
        trade["entry_state"] = "RECOVERY_REQUIRED"
    elif blocker == "same_day":
        trade["entry_time"] = now_utc()
    elif blocker == "wrong_account":
        trade["account_id"] = "another-account"
    elif blocker == "unknown_account":
        client.account_id = "UNKNOWN"
    else:
        monkeypatch.setattr(
            engine._order_lifecycle.journal,
            "list_unresolved_order_intents",
            lambda: [
                {
                    "position_key": "LIVE:dummy:NSE:111:RELIANCE:MIS:unknown-entry",
                }
            ],
        )
    assert not engine._retire_inactive_legacy_trade("RELIANCE", trade, [], [])
    assert not (path / "retired_active_trades.json").exists()


def test_archive_failure_retains_tracking_and_later_retries(legacy, monkeypatch):
    engine, _, _, _, _, path = legacy
    original = config_manager.archive_inactive_trade

    def unavailable(*args):
        raise OSError("archive disk unavailable")

    monkeypatch.setattr(config_manager, "archive_inactive_trade", unavailable)
    engine.reconcile_active_trades()
    assert "RELIANCE" in engine.active_trades and engine._reconciliation_pending
    monkeypatch.setattr(config_manager, "archive_inactive_trade", original)
    engine.reconcile_active_trades()
    assert not engine.active_trades and not engine._reconciliation_pending
    assert (path / "retired_active_trades.json").exists()


def test_pause_log_explains_closed_market_and_deduplicates(legacy, monkeypatch):
    engine, _, _, _, logs, _ = legacy
    engine._supervision_active = True
    monkeypatch.setattr(te, "now_utc", lambda: now_utc().replace(hour=20))
    first = engine.stop(reason="broker session restored; waiting for Start Agent")
    engine.stop(reason="broker session restored; waiting for Start Agent")
    pauses = [line for line in logs if line.startswith("New entries paused:")]
    assert len(pauses) == 1
    assert "waiting for Start Agent" in pauses[0]
    assert "exchange entry window is closed" in pauses[0]
    assert first["statusMessage"] == pauses[0]
