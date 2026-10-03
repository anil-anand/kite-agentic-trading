"""Phase-7 policy pins survive settings edits, restart and malformed input."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.config import ConfigManager
from backend.exit_management.models import PositionCheckpoint
from backend.exit_management.thesis import capture_entry_thesis
from backend.journal import journal
from backend.tests.exit_management.test_engine import _context, _state, _thesis
from backend.tests.exit_management.test_thesis import _signal
from backend.tests.test_exit_live_integration import START, _position
from backend.trading_engine import TradingEngine


@pytest.mark.parametrize("value", [None, [], {}, ["shadow"], 1, True, "unknown"])
def test_malformed_mode_is_a_safe_legacy_pin(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = ConfigManager()
    manager.config["exitManagement"]["livePolicyMode"] = value

    assert manager.get_exit_live_policy_mode() == "legacy_control"
    assert (
        TradingEngine._exit_policy_mode_from_settings({"livePolicyMode": value})
        == "legacy_control"
    )


@pytest.mark.parametrize("value", [None, [], "shadow", False])
def test_malformed_settings_container_retains_legacy_control(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = ConfigManager()
    manager.config["exitManagement"] = value

    assert manager.get_exit_live_policy_mode() == "legacy_control"
    assert (
        manager.get_effective_exit_management_config()["exitManagement"][
            "livePolicyMode"
        ]
        == "legacy_control"
    )


def test_new_settings_default_to_shadow_with_activation_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = ConfigManager()

    assert manager.get_exit_live_policy_mode() == "shadow"
    assert manager.get_exit_management_config()["candidateActivationEnabled"] is False


@pytest.mark.parametrize("mode", ["legacy_control", "shadow", "candidate"])
def test_entry_policy_mode_is_durable_across_settings_change_and_restart(
    monkeypatch, mode
):
    import backend.trading_engine as engine_module

    settings = {"exitManagement": {"livePolicyMode": mode}}
    monkeypatch.setattr(
        engine_module.config_manager,
        "get_effective_exit_management_config",
        lambda: deepcopy(settings),
    )
    monkeypatch.setattr(
        engine_module, "kite_client", SimpleNamespace(namespace=None, account_id=None)
    )
    engine = TradingEngine()
    key = "LIVE:acct-1:NSE:1:RELIANCE:MIS:pinned-mode"
    thesis, intent_id = engine._register_entry_thesis(
        signal=_signal(),
        position_key=key,
        trade_id="pinned-mode-trade",
        position_epoch="pinned-mode",
        instrument_id="1",
        quantity=10,
        transaction_type="BUY",
        order_kwargs={"tradingsymbol": "RELIANCE", "quantity": 10},
        recovery_trade={"tradingsymbol": "RELIANCE", "direction": "BUY"},
    )
    settings["exitManagement"]["livePolicyMode"] = (
        "shadow" if mode == "legacy_control" else "legacy_control"
    )

    record = journal.get_managed_position(key)
    assert record["state"]["counters"]["exit_policy_mode"] == mode
    assert journal.get_order_intent(intent_id)["payload"]["exit_policy_mode"] == mode
    assert engine._exit_policy_mode_for_thesis(thesis) == mode

    restarted = TradingEngine()
    restarted.active_trades["RELIANCE"] = {
        "exit_management_position_key": key,
        "exit_policy_mode": settings["exitManagement"]["livePolicyMode"],
    }
    restarted._restore_phase5_checkpoints()
    assert restarted.active_trades["RELIANCE"]["exit_policy_mode"] == mode


@pytest.mark.parametrize("mode", ["shadow", "candidate"])
def test_candidate_activation_setting_never_grants_broker_authority(monkeypatch, mode):
    import backend.trading_engine as engine_module

    thesis = _thesis()
    settings = {
        "livePolicyMode": mode,
        "candidatePolicyVersion": "deterministic-exit-v1",
        "candidateActivationEnabled": True,
    }
    thesis = replace(
        thesis,
        policy_snapshot=capture_entry_thesis(
            _signal(),
            position_key=thesis.position_key,
            trade_id=thesis.trade_id,
            position_epoch=thesis.position_epoch,
            instrument_id=thesis.instrument_id,
            effective_config={"exitManagement": settings},
            created_at=thesis.created_at,
        ).policy_snapshot,
    )
    state = _state()
    journal.create_managed_position(
        thesis,
        PositionCheckpoint(
            position_key=thesis.position_key,
            state_version=state.version,
            sequence=0,
            state=state,
            counters={"exit_policy_mode": mode, "exit_policy": {}},
            protection={"confirmed_stop": 95.0},
        ),
    )
    engine = TradingEngine()
    trade = {
        "tradingsymbol": "RELIANCE",
        "exit_management_position_key": thesis.position_key,
        "exit_policy_mode": mode,
        "direction": "BUY",
        "quantity": 10,
        "sl": 95.0,
        "target": 110.0,
        "exchange": "NSE",
        "product": "MIS",
        "instrument_id": "1",
        "namespace": "LIVE",
        "account_id": "acct-1",
    }
    engine.active_trades["RELIANCE"] = trade
    decision_at = START + timedelta(minutes=5)
    monkeypatch.setattr(engine_module, "now_utc", lambda: decision_at)
    monkeypatch.setattr(
        engine_module.config_manager, "get_exit_management_config", lambda: settings
    )
    monkeypatch.setattr(
        engine_module.scanner,
        "get_market_context",
        lambda *args, **kwargs: _context(START, close=94.0),
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)

    def mutation_forbidden(*args, **kwargs):
        pytest.fail("Phase-7 candidate acquired broker mutation authority")

    monkeypatch.setattr(engine._order_lifecycle, "submit", mutation_forbidden)
    for gateway in (engine_module.execution_gateway, engine_module.kite_client):
        for method in ("place_order", "modify_order", "cancel_order"):
            monkeypatch.setattr(gateway, method, mutation_forbidden)

    position = {
        **_position(94.0, decision_at),
        "namespace": "LIVE",
        "account_id": "acct-1",
    }
    engine._evaluate_shadow_position(position, trade)

    decision = journal.get_exit_decisions(thesis.position_key)[0]["payload"]
    assert decision["action"] == "REQUEST_EXIT"
    assert decision["trace"]["orchestration"]["dispatch"] == "SUPPRESSED_PHASE7"
    assert decision["trace"]["orchestration"]["candidate_activation_enabled"] is False
    assert (
        journal.get_managed_position(thesis.position_key)["state"]["state"]["exposure"]
        == "OPEN"
    )
