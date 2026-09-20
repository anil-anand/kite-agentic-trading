import threading
from datetime import datetime, timedelta, timezone

import backend.trading_engine as te
from backend.risk_rules import (
    HardRiskAction,
    HardRiskPolicy,
    HardRiskReason,
    HardRiskSnapshot,
    evaluate_hard_risk,
)
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.trading_engine import TradingEngine


class _RiskState:
    kill_switch_active = False

    def rotate_session_if_verified(self, *args, **kwargs):
        return False


def _session():
    return SessionClock(SessionPolicy()).snapshot(
        datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
    )


def test_pure_hard_rule_precedence_and_stop_boundary():
    policy = HardRiskPolicy()
    decision = evaluate_hard_risk(
        HardRiskSnapshot(
            session=_session(),
            signed_quantity=10,
            direction="BUY",
            mark_price=94.0,
            mark_time=_session().observed_at,
            hard_stop_price=95.0,
            daily_loss_latched=True,
        ),
        policy,
    )

    assert decision.action is HardRiskAction.FLATTEN_ACCOUNT
    assert decision.primary_reason_code is HardRiskReason.RISK_DAILY_LOSS

    stop_only = evaluate_hard_risk(
        HardRiskSnapshot(
            session=_session(),
            signed_quantity=10,
            direction="BUY",
            mark_price=95.0,
            mark_time=_session().observed_at,
            hard_stop_price=95.0,
        ),
        policy,
    )
    assert stop_only.action is HardRiskAction.EXIT_POSITION
    assert stop_only.primary_reason_code is HardRiskReason.RISK_CATASTROPHIC_STOP


def test_blocking_scan_does_not_delay_supervisor(monkeypatch):
    engine = TradingEngine()
    scan_started = threading.Event()
    monitor_called = threading.Event()
    release_scan = threading.Event()

    def blocking_scan():
        scan_started.set()
        release_scan.wait(1)

    monkeypatch.setattr(engine, "scan_and_trade", blocking_scan)
    monkeypatch.setattr(engine, "monitor_positions", monitor_called.set)
    monkeypatch.setattr(engine, "_session_clock", lambda: SessionClock(SessionPolicy()))
    monkeypatch.setattr(te, "risk_manager", _RiskState())
    monkeypatch.setattr(
        te.config_manager,
        "get_risk_config",
        lambda: {"supervisorIntervalSeconds": 0.05},
    )
    monkeypatch.setattr(
        te,
        "now_utc",
        lambda: datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc),
    )

    engine.running = True
    engine._supervision_active = True
    entry = threading.Thread(target=engine._run_loop)
    supervisor = threading.Thread(target=engine._supervisor_loop)
    entry.start()
    supervisor.start()
    try:
        assert scan_started.wait(0.5)
        assert monitor_called.wait(0.5)
    finally:
        engine.running = False
        engine._entry_stop.set()
        engine._supervision_active = False
        engine._supervisor_stop.set()
        engine._supervisor_wakeup.set()
        release_scan.set()
        entry.join(1)
        supervisor.join(1)


def test_forced_deadline_latches_flatten_without_new_candle(monkeypatch):
    engine = TradingEngine()
    flattened = []

    monkeypatch.setattr(engine, "monitor_positions", lambda: None)
    monkeypatch.setattr(
        engine,
        "_session_clock",
        lambda: SessionClock(SessionPolicy()),
    )
    monkeypatch.setattr(
        engine, "square_off_all", lambda reason: flattened.append(reason)
    )
    monkeypatch.setattr(te, "risk_manager", _RiskState())
    monkeypatch.setattr(
        te,
        "now_utc",
        lambda: datetime(2026, 9, 21, 9, 46, tzinfo=timezone.utc),
    )

    engine._supervise_hard_risk_once()

    assert flattened == []  # Latching never blocks the scheduler on broker IO.
    assert (
        engine.status()["hardFlattenReason"] == HardRiskReason.SESSION_FORCED_FLAT.value
    )
    assert engine.status()["hardFlattenPending"] is True
    assert engine.running is False
    assert engine.status()["supervisionActive"] is False


def test_paused_supervision_rejects_new_entry_before_broker_work():
    engine = TradingEngine()
    engine._supervision_active = True
    engine.running = False

    accepted = engine.execute_signal({"tradingsymbol": "RELIANCE"})

    assert accepted is False
    assert engine.status()["entryPaused"] is True
    assert engine.status()["supervisionActive"] is True


def test_deadline_scheduler_remains_responsive_during_blocked_management(monkeypatch):
    engine = TradingEngine()
    entered = threading.Event()
    release = threading.Event()
    clock = [datetime(2026, 9, 21, 9, 44, tzinfo=timezone.utc)]
    monkeypatch.setattr(te, "now_utc", lambda: clock[0])
    monkeypatch.setattr(te, "risk_manager", _RiskState())
    monkeypatch.setattr(
        te.config_manager,
        "get_risk_config",
        lambda: {"supervisorIntervalSeconds": 0.25},
    )
    monkeypatch.setattr(engine, "_save_control_state", lambda: None)

    def blocked():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(engine, "_supervise_hard_risk_once", blocked)
    engine._supervision_active = True
    thread = threading.Thread(target=engine._supervisor_loop)
    thread.start()
    try:
        assert entered.wait(0.5)
        clock[0] += timedelta(minutes=2)
        engine._supervisor_wakeup.set()
        deadline = threading.Event()
        for _ in range(30):
            if engine._hard_flatten_reason:
                break
            deadline.wait(0.01)
        assert engine._hard_flatten_reason == HardRiskReason.SESSION_FORCED_FLAT.value
        assert not release.is_set()
    finally:
        engine._supervision_active = False
        engine._supervisor_stop.set()
        engine._supervisor_wakeup.set()
        release.set()
        thread.join(1)
        engine._management_thread.join(1)


def test_stop_during_start_reconciliation_cannot_reenable_entries(monkeypatch):
    engine = TradingEngine()

    class Risk(_RiskState):
        def reconcile_state(self):
            engine.stop()

    monkeypatch.setattr(te, "risk_manager", Risk())
    monkeypatch.setattr(engine, "_load_control_state", lambda: None)
    monkeypatch.setattr(engine, "reconcile_active_trades", lambda: None)
    monkeypatch.setattr(engine, "_activate_supervision", lambda: None)
    assert engine.start("auto")["entryPaused"] is True


def test_continuous_entry_has_single_recovery_owner_and_keeps_timeout(
    lifecycle, monkeypatch
):
    env = lifecycle
    engine = env.engine
    engine._supervision_active = True
    engine.running = True
    engine._entry_fill_timeout_seconds = 15
    monkeypatch.setattr(engine, "_entry_admission_allowed", lambda: (True, "OK"))
    monkeypatch.setattr(
        engine,
        "_wait_for_entry_fill",
        lambda *args: (_ for _ in ()).throw(AssertionError("second entry owner")),
    )
    assert engine.execute_signal(env.signal)
    assert len(env.sdk.calls) == 1
    trade = engine.active_trades["RELIANCE"]
    trade["entry_observed_at"] = te.now_utc() - timedelta(seconds=16)

    def cancel(variety, order_id, **kwargs):
        env.sdk.book[0].update(status="CANCELLED", pending_quantity=0)
        return order_id

    env.sdk.cancel_order = cancel
    engine.monitor_positions()
    assert env.sdk.book[0]["status"] == "CANCELLED"
    assert not engine.active_trades
    assert engine._reserved_entry_margin == 0


def test_critical_engine_reconciliation_reads_keep_priority(lifecycle, monkeypatch):
    env = lifecycle
    observed = []
    for name in (
        "get_positions_snapshot",
        "get_current_orders_snapshot",
        "get_fills_snapshot",
        "get_broker_snapshot",
    ):
        original = getattr(env.client, name)

        def record(*args, _original=original, _name=name, **kwargs):
            observed.append((_name, kwargs.get("critical")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(env.client, name, record)
    env.engine._positions()
    env.engine._orders()
    env.engine._fills()
    env.engine._broker_snapshot()
    assert observed and all(critical for _, critical in observed)


def test_reconciliation_preserves_owner_added_during_broker_read(
    lifecycle, monkeypatch
):
    env = lifecycle
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)
    original_read = env.engine._positions
    concurrent_owner = {
        "tradingsymbol": "CONCURRENT",
        "entry_state": "RECOVERY_REQUIRED",
    }

    def read_with_concurrent_entry(**kwargs):
        positions = original_read(**kwargs)
        env.engine.active_trades["CONCURRENT"] = concurrent_owner
        return positions

    monkeypatch.setattr(env.engine, "_positions", read_with_concurrent_entry)
    env.engine.reconcile_active_trades()
    assert env.engine.active_trades["CONCURRENT"] is concurrent_owner
    assert "RELIANCE" in env.engine.active_trades


def test_hard_latch_survives_control_storage_failure(monkeypatch):
    engine = TradingEngine()
    engine.running = True

    def unavailable():
        raise OSError("storage unavailable")

    monkeypatch.setattr(engine, "_save_control_state", unavailable)
    engine._latch_account_flatten(HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value)
    assert engine.status()["hardFlattenPending"]
    assert engine.status()["controlStateInvalid"]
    assert engine.status()["entryPaused"]
