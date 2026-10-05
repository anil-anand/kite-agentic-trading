"""New-day recovery with delayed broker reads and retained operator controls."""

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

import backend.broker_models as models
import backend.trading_engine as te
from backend.broker_models import (
    SnapshotQuality,
    normalize_fills_response,
    unavailable_fill_snapshot,
)
from backend.config import config_manager
from backend.risk_rules import HardRiskReason
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.trading_engine import TradingEngine

NOW = datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc)  # 11 AM IST, Monday
PREVIOUS = "2026-09-28"


@pytest.fixture
def recovery(lifecycle, monkeypatch):
    env = lifecycle
    monkeypatch.setattr(te, "now_utc", lambda: NOW)
    monkeypatch.setattr(models, "utc_now", lambda: NOW)
    monkeypatch.setattr(env.risk, "_now", lambda: NOW)
    monkeypatch.setattr(
        env.risk, "_exchange_now", lambda: NOW.astimezone(te.EXCHANGE_TIMEZONE)
    )
    monkeypatch.setattr(
        TradingEngine, "_session_clock", lambda _: SessionClock(SessionPolicy())
    )
    monkeypatch.setattr(env.client, "_accounting_fill_read", None)
    env.risk.bind_account(env.client.namespace, env.client.account_id)
    env.risk.date_str = "2026-10-05"
    env.risk.kill_switch_active = False
    return env


def saved_halt(env, *, legacy=False, session=PREVIOUS, reason=None):
    state = {
        "hardFlattenReason": reason or HardRiskReason.SESSION_FORCED_FLAT.value,
        "hardFlattenPending": False,
        "pendingClosePositionKeys": [],
    }
    if not legacy:
        state["hardFlattenSession"] = session
    config_manager.save_operator_state("LIVE", "acct-A", state)
    if legacy:
        stamp = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).timestamp()
        os.utime(config_manager.config_dir / "operator_state.json", (stamp, stamp))
    env.engine._load_control_state()


@pytest.mark.parametrize("legacy", [False, True])
def test_settled_old_square_off_clears_when_risk_session_is_already_current(
    recovery, legacy
):
    env = recovery
    saved_halt(env, legacy=legacy)
    assert env.engine._hard_flatten_session == PREVIOUS

    env.engine._supervise_hard_risk_once()

    assert env.risk.date_str == "2026-10-05"
    assert env.risk.reconciliation_status == "RECONCILED"
    assert env.engine._hard_flatten_reason is None
    assert env.engine._entry_block_reasons() == []
    assert env.engine.running is False
    assert env.sdk.calls == []
    restarted = TradingEngine()
    restarted._load_control_state()
    assert restarted._hard_flatten_reason is None


@pytest.mark.parametrize(
    "block",
    [
        "fills",
        "orders",
        "positions",
        "residual",
        "same_day",
        "emergency",
        "daily_loss",
        "invalid_controls",
    ],
)
def test_old_halt_recovery_keeps_other_safety_gates(recovery, monkeypatch, block):
    env = recovery
    saved_halt(
        env,
        session="2026-10-05" if block == "same_day" else PREVIOUS,
        reason=HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value
        if block == "emergency"
        else None,
    )
    if block in {"fills", "orders", "positions"}:
        setattr(env.sdk, f"fail_{block}", True)
    elif block == "residual":
        monkeypatch.setattr(env.engine, "_has_residual_obligations", lambda: True)
    elif block == "daily_loss":
        env.risk.kill_switch_active = True
    elif block == "invalid_controls":
        env.engine._control_state_invalid = True

    env.engine._supervise_hard_risk_once()

    assert env.engine._hard_flatten_reason is not None
    assert env.engine.running is False
    assert env.sdk.calls == []


def test_new_emergency_during_new_day_reconciliation_is_not_cleared(
    recovery, monkeypatch
):
    env = recovery
    saved_halt(env)
    env.risk.date_str = PREVIOUS
    monitor = env.engine.monitor_positions

    def emergency():
        monitor()
        env.engine._latch_account_flatten(
            HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value
        )
        env.engine._settle_control_obligations()

    monkeypatch.setattr(env.engine, "monitor_positions", emergency)
    env.engine._supervise_hard_risk_once()
    assert (
        env.engine._hard_flatten_reason
        == HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value
    )
    assert env.engine._hard_flatten_session == "2026-10-05"


@pytest.mark.parametrize("consumer", ["client", "engine"])
@pytest.mark.parametrize("poll_delay", [6, 121])
def test_delayed_fills_keep_their_original_snapshot_times(
    recovery, monkeypatch, consumer, poll_delay
):
    env = recovery
    clock = [NOW]
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(models, "utc_now", lambda: clock[0])

    def fills(**kwargs):
        entered.set()
        assert release.wait(3)
        return normalize_fills_response(
            [],
            namespace=env.client.namespace,
            account_id=env.client.account_id,
            fetched_at=NOW + timedelta(milliseconds=250),
        )

    monkeypatch.setattr(env.client, "get_fills_snapshot", fills)
    read = (
        env.client.get_broker_snapshot
        if consumer == "client"
        else env.engine._broker_snapshot
    )
    try:
        assert read().fills_quality is SnapshotQuality.UNAVAILABLE
        assert entered.is_set()
        pending = env.client._accounting_fill_read
        release.set()
        assert pending["done"].wait(1)
        clock[0] = NOW + timedelta(seconds=poll_delay)
        result = read()
        assert result.positions_fetched_at == NOW
        assert result.orders_fetched_at == NOW
        assert result.entry_ready_at(clock[0]) is (poll_delay == 6)
    finally:
        release.set()


def test_journal_pass_shares_one_fill_read_and_one_pending_warning(
    recovery, monkeypatch
):
    env = recovery
    records = [
        {
            "id": str(i),
            "tradingsymbol": "RELIANCE",
            "status": "CLOSED",
            "exit_reason": "UNRECONCILED",
        }
        for i in range(50)
    ]
    monkeypatch.setattr(te.journal, "get_trades", lambda: records)
    calls, messages = [], []

    def pending():
        calls.append(True)
        return unavailable_fill_snapshot("fill history read is pending")

    monkeypatch.setattr(env.engine, "_fill_snapshot", pending)
    monkeypatch.setattr(
        env.engine, "_push_log", lambda message, **kwargs: messages.append(message)
    )
    env.engine._reconcile_journal_trades()
    assert len(calls) == 1
    assert len(messages) == 1
    assert "waiting for fill history" in messages[0]
    assert env.journal.get_trades() == records


def test_legacy_session_date_survives_another_accounts_state_write(recovery):
    env = recovery
    saved_halt(env, legacy=True)
    config_manager.save_operator_state("LIVE", "acct-B", {"hardFlattenReason": None})
    state = config_manager.load_operator_state("LIVE", "acct-A")
    assert state["hardFlattenSession"] == PREVIOUS


def test_fill_read_starts_before_essential_reads_to_avoid_permanent_pending(
    recovery, monkeypatch
):
    env = recovery
    entered, release = threading.Event(), threading.Event()
    orders = env.client.get_current_orders_snapshot

    def fills(**kwargs):
        entered.set()
        assert release.wait(2)
        return normalize_fills_response(
            [], namespace=env.client.namespace, account_id=env.client.account_id
        )

    def current_orders(**kwargs):
        assert entered.wait(1), "fills must be started before waiting for orders"
        release.set()
        return orders(**kwargs)

    monkeypatch.setattr(env.client, "get_fills_snapshot", fills)
    monkeypatch.setattr(env.client, "get_current_orders_snapshot", current_orders)
    try:
        result = env.engine._broker_snapshot()
        assert result.entry_ready_at(NOW)
    finally:
        release.set()


@pytest.mark.parametrize(
    "change",
    ["positions_unavailable", "orders_unavailable", "new_order", "new_position"],
)
def test_completed_fill_read_cannot_hide_newer_broker_risk(
    recovery, monkeypatch, change
):
    env = recovery
    release = threading.Event()

    def fills(**kwargs):
        assert release.wait(2)
        return normalize_fills_response(
            [], namespace=env.client.namespace, account_id=env.client.account_id
        )

    monkeypatch.setattr(env.client, "get_fills_snapshot", fills)
    try:
        assert not env.client.get_broker_snapshot().entry_ready_at(NOW)
        pending = env.client._accounting_fill_read
        release.set()
        assert pending["done"].wait(1)
        if change == "positions_unavailable":
            env.sdk.fail_positions = True
        elif change == "orders_unavailable":
            env.sdk.fail_orders = True
        else:
            env.sdk.place_order(
                exchange="NSE",
                tradingsymbol="RELIANCE",
                product="MIS",
                transaction_type="BUY",
                quantity=1,
                order_type="LIMIT",
                price=100,
                variety="regular",
            )
            if change == "new_position":
                env.sdk.fill_entry(quantity=1)
        result = env.client.get_broker_snapshot()
        assert not result.entry_ready_at(NOW)
        assert result.fills_quality is SnapshotQuality.STALE
        if change == "new_position":
            assert result.positions[0].signed_quantity == 1
        elif change == "new_order":
            assert result.current_orders[0].remaining_quantity == 1
    finally:
        release.set()


def test_failed_halt_clear_persistence_preserves_entry_block(recovery, monkeypatch):
    env = recovery
    saved_halt(env)

    def fail():
        raise OSError("state storage unavailable")

    monkeypatch.setattr(env.engine, "_save_control_state", fail)
    with pytest.raises(OSError, match="storage unavailable"):
        env.engine._supervise_hard_risk_once()
    assert env.engine._hard_flatten_reason == HardRiskReason.SESSION_FORCED_FLAT.value
    assert env.engine._control_state_invalid
    assert env.engine._hard_flatten_session == PREVIOUS


def test_pending_accounting_read_is_not_reused_after_account_switch(
    recovery, monkeypatch
):
    env = recovery
    release = threading.Event()
    calls = []

    def fills(**kwargs):
        account = env.client.account_id
        calls.append(account)
        if account == "acct-A":
            assert release.wait(2)
        return normalize_fills_response(
            [], namespace=env.client.namespace, account_id=account
        )

    monkeypatch.setattr(env.client, "get_fills_snapshot", fills)
    try:
        assert not env.client.get_broker_snapshot().entry_ready_at(NOW)
        previous = env.client._accounting_fill_read
        monkeypatch.setattr(env.client, "account_id", "acct-B")
        result = env.client.get_broker_snapshot()
        assert result.account_id == "acct-B"
        assert result.entry_ready_at(NOW)
        assert calls == ["acct-A", "acct-B"]
        release.set()
        assert previous["done"].wait(1)
        assert env.client._accounting_fill_read is None
    finally:
        release.set()
