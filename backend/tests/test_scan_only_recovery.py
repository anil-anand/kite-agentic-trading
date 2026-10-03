"""Recovery can block orders without blocking explicitly read-only analysis."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import backend.trading_engine as te
from backend.risk_rules import HardRiskReason
from backend.tests.test_persistence_reconcile import _setup_reconcile, _trade
from backend.trading_engine import TradingEngine


@pytest.fixture
def engine(monkeypatch):
    engine = TradingEngine()
    monkeypatch.setattr(
        te, "now_utc", lambda: datetime(2026, 9, 21, 5, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(
        te,
        "risk_manager",
        SimpleNamespace(
            kill_switch_active=False,
            reconciliation_status="RECONCILED",
            reconcile_state=lambda: None,
            can_trade=lambda: (True, "OK"),
        ),
    )
    monkeypatch.setattr(engine, "_load_control_state", lambda: None)
    monkeypatch.setattr(engine, "_save_control_state", lambda: None)
    monkeypatch.setattr(engine, "reconcile_active_trades", lambda: None)
    monkeypatch.setattr(
        engine,
        "_activate_supervision",
        lambda: setattr(engine, "_supervision_active", True),
    )
    monkeypatch.setattr(engine, "_run_loop", lambda: None)
    yield engine
    engine.stop()
    if engine.thread:
        engine.thread.join(1)


@pytest.mark.parametrize("supervised", [False, True])
@pytest.mark.parametrize(
    "blocker",
    [
        "_reconciliation_pending",
        "_lifecycle_recovery_pending",
        "_control_state_invalid",
        "_protection_failure_halt",
    ],
)
def test_start_runs_scanner_with_entries_blocked(
    engine, monkeypatch, supervised, blocker
):
    engine._supervision_active = supervised
    setattr(engine, blocker, True)
    scans = []
    monkeypatch.setattr(engine, "_run_loop", lambda: scans.append(True))

    state = engine.start("auto")
    engine.thread.join(1)

    assert scans == [True]
    assert state["running"] and state["scanOnly"]
    assert state["entryPaused"] and state["supervisionActive"]
    assert state["effectiveMode"] == "scan_only"
    assert state["entryBlockReasons"]
    assert engine._entry_admission_allowed()[0] is False

    # A recovering account must never silently upgrade a read-only run.
    setattr(engine, blocker, False)
    assert engine.status()["entryPaused"]
    assert engine._entry_admission_allowed()[0] is False


def test_healthy_start_still_allows_live_entries(engine):
    state = engine.start("auto")
    assert state["running"] and not state["scanOnly"]
    assert not state["entryPaused"]
    assert engine._entry_admission_allowed() == (True, "OK")


def test_resume_keeps_live_candle_deduplication(engine, monkeypatch):
    engine._supervision_active = True
    candle = datetime(2026, 9, 21, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(te.scanner, "last_scanned_candle", {"TEST": candle})
    monkeypatch.setattr(te.scanner, "last_analysis_candle", {"TEST": candle})
    engine.start("auto")
    assert te.scanner.last_scanned_candle == {"TEST": candle}
    assert te.scanner.last_analysis_candle == {}


def test_stop_during_final_start_checks_wins(engine, monkeypatch):
    original = engine._entry_block_reasons
    stopped = False

    def stop_while_checking():
        nonlocal stopped
        if not stopped:
            stopped = True
            engine.stop()
        return original()

    monkeypatch.setattr(engine, "_entry_block_reasons", stop_while_checking)
    state = engine.start("auto")
    assert not state["running"] and state["entryPaused"]
    assert engine.thread is None
    assert engine._entry_stop.is_set()


def test_queued_start_cannot_undo_a_later_pause(engine):
    admitted_version = engine._entry_control_version
    engine.stop()
    with pytest.raises(ValueError, match="superseded"):
        engine.start("auto", expected_control_version=admitted_version)
    assert not engine.running and engine.thread is None


def test_daily_loss_supervision_keeps_read_only_scan_running(engine):
    te.risk_manager.kill_switch_active = True
    state = engine.start("auto")
    assert state["scanOnly"]
    engine._latch_account_flatten(HardRiskReason.RISK_DAILY_LOSS.value)
    assert engine.running and not engine._entry_stop.is_set()
    assert not engine._entry_admission_allowed()[0]
    assert engine._hard_flatten_pending


@pytest.mark.parametrize("day", [21, 26])  # Weekday after close and Saturday.
def test_closed_market_scan_survives_session_supervision(engine, monkeypatch, day):
    monkeypatch.setattr(
        te, "now_utc", lambda: datetime(2026, 9, day, 12, tzinfo=timezone.utc)
    )
    state = engine.start("auto")
    assert state["running"] and state["scanOnly"]
    assert "The exchange entry window is closed" in state["entryBlockReasons"]
    engine._latch_account_flatten(HardRiskReason.SESSION_FORCED_FLAT.value)
    engine._latch_account_flatten(HardRiskReason.SESSION_FORCED_FLAT.value)
    assert engine.running and not engine._entry_stop.is_set()
    assert engine.status()["entryPaused"]


def test_read_only_scan_emits_signals_but_never_submits(engine, monkeypatch):
    engine._reconciliation_pending = True
    engine.start("auto")
    engine.dynamic_watchlist = ["TEST"]
    published, submitted = [], []

    def scan(symbols, on_signal, *, analysis_only=False):
        assert symbols == ["TEST"] and analysis_only
        on_signal(
            {
                "tradingsymbol": "TEST",
                "direction": "BUY",
                "signal_score": 90,
                "entryPrice": 100,
                "stopLoss": 95,
                "target": 110,
                "analysisOnly": True,
            }
        )

    monkeypatch.setattr(te.scanner, "scan_watchlist", scan)
    monkeypatch.setattr(engine, "_push_signal", published.append)
    monkeypatch.setattr(engine, "execute_signal", submitted.append)
    engine.scan_and_trade()
    assert len(published) == 1
    assert submitted == []


@pytest.mark.parametrize(
    "marker",
    [
        {"analysisOnly": True},
        {"market_context": {"analysis_only": True}},
    ],
)
def test_analysis_signals_remain_non_executable_after_live_restart(engine, marker):
    engine.start("auto")
    assert engine._entry_admission_allowed()[0]
    assert engine.execute_signal({"tradingsymbol": "TEST", **marker}) is False
    assert not engine.active_trades and not engine._pending_entries


def test_quarantined_legacy_record_is_reported_once_as_pending(monkeypatch):
    trade = _trade(
        account_id=None,
        instrument_id=None,
        broker_reconciliation_pending=True,
        ownership_quarantined=True,
        recovery_state="LEGACY_IDENTITY_UNRESOLVED",
    )
    engine, client, _ = _setup_reconcile(
        monkeypatch,
        {"RELIANCE": trade},
        positions_net=[],
        orders=[{"order_id": "STOP1", "status": "TRIGGER PENDING"}],
    )
    logs = []
    monkeypatch.setattr(engine, "_push_log", lambda message, **kw: logs.append(message))
    for _ in range(3):
        engine.reconcile_active_trades()

    reports = [message for message in logs if message.startswith("Reconcile")]
    assert len(reports) == 1
    assert "Reconcile pending: 0 verified open position(s)" in reports[0]
    assert (
        "RELIANCE: saved trade is missing verified account/instrument identity"
        in reports[0]
    )
    assert not any("resumed" in message for message in logs)
    assert engine._reconciliation_pending
    assert "RELIANCE" in engine.active_trades
    assert client.place_calls == client.cancel_calls == []
    assert any("RELIANCE:" in reason for reason in engine.status()["entryBlockReasons"])


def test_reconciliation_logs_recovery_transition_without_poll_spam(monkeypatch):
    engine, client, _ = _setup_reconcile(
        monkeypatch,
        {"RELIANCE": _trade()},
        positions_net=[],
        orders=[{"order_id": "STOP1", "status": "CANCELLED"}],
    )
    monkeypatch.setattr(engine, "_journal_external_close", lambda symbol: False)
    logs = []
    monkeypatch.setattr(engine, "_push_log", lambda message, **kw: logs.append(message))
    engine.reconcile_active_trades()
    engine.reconcile_active_trades()
    monkeypatch.setattr(engine, "_journal_external_close", lambda symbol: True)
    engine.reconcile_active_trades()
    reports = [
        message
        for message in logs
        if message.startswith(("Reconcile pending", "Reconcile complete"))
    ]
    assert len(reports) == 2
    assert "ACCOUNTING" not in reports[0]  # User-facing reason is readable.
    assert "accounting reconciliation pending" in reports[0]
    assert reports[1] == "Reconcile complete: 0 verified open position(s)."
    assert not engine.active_trades and not engine._reconciliation_pending
    assert client.place_calls == []
