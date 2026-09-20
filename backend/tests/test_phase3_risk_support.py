"""Adversarial hard-risk inputs, rollover persistence and critical broker IO."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.kite_client as kc
import backend.risk_manager as rm
from backend.broker_models import OrderRole, SnapshotQuality
from backend.config import ConfigManager, config_manager
from backend.request_policy import BrokerGateway, Priority
from backend.risk_manager import RiskManager
from backend.risk_rules import (
    HardRiskAction,
    HardRiskPolicy,
    HardRiskReason,
    HardRiskSnapshot,
    evaluate_hard_risk,
)
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_phase1_review_corrections import _snapshot


def session(day=21, hour=5):
    return SessionClock(SessionPolicy()).snapshot(
        datetime(2026, 9, day, hour, tzinfo=timezone.utc)
    )


def stop_snapshot(**changes):
    observed = session()
    return replace(
        HardRiskSnapshot(
            session=observed,
            signed_quantity=10,
            direction="BUY",
            mark_price=94,
            mark_time=observed.observed_at,
            hard_stop_price=95,
        ),
        **changes,
    )


@pytest.mark.parametrize("mark", [True, float("inf"), float("nan"), 0, -1])
def test_invalid_prices_cannot_create_stop_breaches(mark):
    decision = evaluate_hard_risk(stop_snapshot(mark_price=mark), HardRiskPolicy())
    assert decision.action is HardRiskAction.HOLD


@pytest.mark.parametrize("age", [None, -6, 121])
def test_missing_stale_future_marks_cannot_create_stop_breaches(age):
    mark_time = None if age is None else session().observed_at - timedelta(seconds=age)
    decision = evaluate_hard_risk(stop_snapshot(mark_time=mark_time), HardRiskPolicy())
    assert decision.action is HardRiskAction.HOLD


@pytest.mark.parametrize(
    "direction,quantity,mark", [("BUY", 10, 95), ("SELL", -10, 95)]
)
def test_fresh_hard_boundary_needs_no_candle_confirmation(direction, quantity, mark):
    decision = evaluate_hard_risk(
        stop_snapshot(direction=direction, signed_quantity=quantity, mark_price=mark),
        HardRiskPolicy(),
    )
    assert decision.action is HardRiskAction.EXIT_POSITION
    assert decision.primary_reason_code is HardRiskReason.RISK_CATASTROPHIC_STOP


def test_simultaneous_hard_causes_keep_specified_precedence_and_attribution():
    deadline = session(hour=10)
    decision = evaluate_hard_risk(
        stop_snapshot(
            session=deadline,
            mark_time=deadline.observed_at,
            daily_loss_latched=True,
            protection_failed=True,
        ),
        HardRiskPolicy(),
    )
    assert decision.action is HardRiskAction.FLATTEN_ACCOUNT
    assert decision.primary_reason_code is HardRiskReason.RISK_DAILY_LOSS
    assert decision.contributing_reason_codes == (
        HardRiskReason.RISK_CATASTROPHIC_STOP,
        HardRiskReason.SESSION_FORCED_FLAT,
        HardRiskReason.RISK_PROTECTION_FAILURE,
    )


def test_unknown_broker_flat_snapshot_is_not_proof_of_no_risk():
    decision = evaluate_hard_risk(
        HardRiskSnapshot(session=session(), broker_state_known=False),
        HardRiskPolicy(),
    )
    assert decision.action is HardRiskAction.RECONCILE_REQUIRED


@pytest.fixture
def persisted_risk(monkeypatch):
    state = {
        "date": "2026-09-18",
        "daily_pnl": -2500,
        "incurred_fees": 75,
        "kill_switch_active": True,
        "reconciliation_status": "RECONCILED",
    }
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: dict(state))
    monkeypatch.setattr(config_manager, "save_daily_risk_state", state.update)
    monkeypatch.setattr(rm, "get_ist_now", lambda: session().exchange_time)
    return RiskManager(), state


def test_next_day_restart_preserves_previous_loss_latch(persisted_risk):
    manager, persisted = persisted_risk
    assert manager.date_str == "2026-09-18"
    assert manager.kill_switch_active
    assert manager.daily_pnl == -2500
    assert manager.reconciliation_status == "RECONCILIATION_PENDING"
    assert persisted["date"] == "2026-09-18"


def test_reconciliation_does_not_reset_session_before_residual_proof(
    persisted_risk, monkeypatch
):
    manager, persisted = persisted_risk
    snapshot = replace(_snapshot(), positions_quality=SnapshotQuality.UNAVAILABLE)
    monkeypatch.setattr(
        kc, "kite_client", SimpleNamespace(get_broker_snapshot=lambda: snapshot)
    )
    manager.reconcile_state()
    assert manager.date_str == "2026-09-18"
    assert manager.kill_switch_active
    assert persisted["kill_switch_active"]


@pytest.mark.parametrize(
    "block",
    ["unverified", "residual", "reservation", "weekend", "backwards", "preopen"],
)
def test_rollover_requires_forward_trading_session_and_no_obligation(
    persisted_risk, block
):
    manager, _ = persisted_risk
    observed = session()
    if block == "reservation":
        manager._entry_reservations["pending"] = object()
    elif block == "weekend":
        observed = session(day=20)
    elif block == "backwards":
        manager.date_str = "2026-09-22"
    elif block == "preopen":
        observed = session(hour=1)
    assert not manager.rotate_session_if_verified(
        observed,
        reconciliation_verified=block != "unverified",
        has_residual_obligations=block == "residual",
    )
    assert manager.kill_switch_active


def test_unrotated_session_cannot_admit_new_risk_even_after_accounting_recovers(
    persisted_risk,
):
    manager, _ = persisted_risk
    manager.kill_switch_active = False
    manager.reconciliation_status = "RECONCILED"
    assert manager.can_trade()[0] is False
    accepted, reason = manager.can_accept_position("INFY", "BUY", 1, 100, _snapshot())
    assert not accepted
    assert reason == "RECONCILIATION_REQUIRED: SESSION_RESET_PENDING"


def test_verified_rollover_clears_old_loss_once(persisted_risk):
    manager, state = persisted_risk
    assert manager.rotate_session_if_verified(
        session(), reconciliation_verified=True, has_residual_obligations=False
    )
    assert manager.date_str == "2026-09-21"
    assert manager.daily_pnl == 0
    assert not manager.kill_switch_active
    assert state["date"] == "2026-09-21"
    manager.kill_switch_active = True
    assert not manager.rotate_session_if_verified(
        session(), reconciliation_verified=True, has_residual_obligations=False
    )
    assert manager.kill_switch_active


def test_critical_snapshot_keeps_mark_quote_on_critical_priority(monkeypatch):
    row = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "MIS",
        "quantity": 10,
        "average_price": 100,
        "last_price": 94,
    }
    calls = []
    client = kc.KiteClient()
    sdk = SimpleNamespace(
        positions=lambda: {"net": [row], "day": [row]},
        quote=lambda instruments: {
            "NSE:RELIANCE": {"last_price": 101, "timestamp": session().observed_at}
        },
    )
    monkeypatch.setattr(client, "kite", sdk)

    def execute(
        action, priority, is_order=False, order_reconciler=None, *args, **kwargs
    ):
        calls.append(priority)
        return action(*args, **kwargs)

    monkeypatch.setattr(kc.broker_gateway, "execute", execute)
    snapshot = client.get_positions_snapshot(critical=True)
    assert calls == [Priority.CRITICAL, Priority.CRITICAL]
    assert snapshot.net[0].last_price == 101


def test_ambiguous_protective_submission_reconciles_through_open_circuit(monkeypatch):
    gateway = BrokerGateway(rate_limit=100, circuit_breaker_threshold=1, max_retries=0)
    client = kc.KiteClient()
    tag = "phase3-protection"

    def accepted_then_timeout(**kwargs):
        raise TimeoutError("response lost after accepted order")

    monkeypatch.setattr(kc, "broker_gateway", gateway)
    monkeypatch.setattr(
        client,
        "kite",
        SimpleNamespace(
            place_order=accepted_then_timeout,
            orders=lambda: [{"tag": tag, "order_id": "STOP-1"}],
        ),
    )
    monkeypatch.setattr(config_manager, "add_app_order_id", lambda *args: None)
    assert (
        client.place_order(
            "regular",
            "NSE",
            "RELIANCE",
            "SELL",
            10,
            "MIS",
            "SL",
            order_role=OrderRole.PROTECTION,
            attempt_tag=tag,
        )
        == "STOP-1"
    )


def test_operator_state_is_scoped_to_execution_namespace_and_account(tmp_path):
    manager = ConfigManager.__new__(ConfigManager)
    manager.config_dir = tmp_path
    import threading

    manager._state_file_lock = threading.RLock()
    manager.save_operator_state("LIVE", "account-a", {"flatten": True})
    manager.save_operator_state("DEV", "account-a", {"flatten": False})
    assert manager.load_operator_state("LIVE", "account-a") == {"flatten": True}
    assert manager.load_operator_state("DEV", "account-a") == {"flatten": False}
    assert manager.load_operator_state("LIVE", "account-b") == {}
    (tmp_path / "operator_state.json").write_text("[]")
    with pytest.raises(ValueError, match="malformed"):
        manager.load_operator_state("LIVE", "account-a")
