"""Executed candidate decisions retain sufficient immutable inputs for replay."""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from backend.exit_management.engine import ExitPolicy
from backend.exit_management.models import ProtectionState
from backend.exit_management.profiles import ManagementProfile
from backend.replay import replay_recorded_exit_decision
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_candidate_execution import START, SYMBOL, _event, _runner
from backend.tests.test_candidate_runner_boundaries import _candidate, _trail_event


def test_epoch_position_records_replay_through_exit_and_partial_reconciliation(
    tmp_path,
):
    runner, thesis, state = _candidate(tmp_path, register=False)
    broker_key = runner.broker._key_for(thesis.symbol).as_string()
    position_key = f"{broker_key}:{thesis.position_epoch}"
    thesis = replace(thesis, position_key=position_key)
    runner.register_position(
        thesis=thesis, state=replace(state, position_key=position_key)
    )
    runner.broker.execution_policy = replace(
        runner.broker.execution_policy, max_fill_fraction=0.5
    )
    for index, close in enumerate((98.0, 98.0, 99.0, 100.0)):
        _event(runner, index, close)
    result = runner.finish()
    retained = json.loads(json.dumps(result["recorded_decisions"]))
    assert len(retained) == len(runner.evaluations) == 4
    assert {item["action"] for item in retained} == {
        "HOLD",
        "REQUEST_EXIT",
        "MANAGE_PENDING_INTENT",
    }
    for record, expected in zip(retained, runner.evaluations):
        replayed = replay_recorded_exit_decision(record)
        assert replayed == expected
        assert (
            record["trace"]["input_snapshot"]["state"]["position_key"] == position_key
        )
    assert retained[-1]["trace"]["input_snapshot"]["state"]["known_quantity"] == 3
    managed = runner.positions[SYMBOL]
    projection = runner.coordinator.journal.get_order_intent_projection(
        managed.coordinator_intent_id
    )
    assert projection["position_key"] == broker_key
    artifacts = result["manifest"]["position_artifacts"][position_key]
    assert artifacts["broker_position_key"] == broker_key
    assert artifacts["initial_position_state"]["known_quantity"] == 10
    # Exported research data must not alias the runner's original audit records.
    retained[0]["trace"]["state_after"]["known_quantity"] = 1
    assert runner.recorded_decisions[0]["trace"]["state_after"]["known_quantity"] == 10


def test_ratchet_record_keeps_pure_output_before_execution_acknowledgement(tmp_path):
    runner, thesis, _ = _candidate(tmp_path, stop_limit=True)
    _trail_event(runner, thesis)
    managed = runner.positions[thesis.symbol]
    assert managed.state.protection is ProtectionState.ACTIVE
    assert managed.management.confirmed_stop > 95.0
    record = json.loads(json.dumps(runner.finish()["recorded_decisions"][0]))
    replayed = replay_recorded_exit_decision(record)
    assert record["action"] == "TIGHTEN_STOP"
    assert replayed.next_position_state.protection is ProtectionState.UPDATE_PENDING
    assert replayed.next_management_state.confirmed_stop == 95.0
    assert (
        replayed.next_management_state.requested_stop
        == managed.management.confirmed_stop
    )


def test_manifest_pins_complete_policy_profiles_and_exchange_calendar(tmp_path):
    runner, thesis, state = _candidate(tmp_path, register=False)
    profile = ManagementProfile(
        name=thesis.management_profile.name,
        failure_confirmation_bars=3,
        minimum_buffer_ticks=4,
    )
    policy = ExitPolicy(profile_overrides={profile.name.value: profile})
    runner.register_position(thesis=thesis, state=state, policy=policy)
    runner.clock = SessionClock(SessionPolicy(holidays=frozenset({date(2026, 10, 2)})))
    _event(runner, 0, 100.0)
    manifest = json.loads(json.dumps(runner.finish()["manifest"]))
    session = manifest["session_policy"]
    assert session["exchange_timezone"] == "Asia/Kolkata"
    assert session["holidays"] == ["2026-10-02"]
    assert session["forced_flatten_time"] == "15:15:00"
    artifact = manifest["position_artifacts"][thesis.position_key]
    assert artifact["exit_policy"]["hard_risk_policy"]["mark_max_age_seconds"] == 120
    assert artifact["resolved_profile"]["failure_confirmation_bars"] == 3
    assert artifact["resolved_profile"]["minimum_buffer_ticks"] == 4
    assert artifact["thesis"] == thesis.to_dict()
    replay_recorded_exit_decision(runner.recorded_decisions[0])


def test_pinned_policy_mutation_is_rejected_before_broker_execution(tmp_path):
    runner = _runner(tmp_path)
    _event(runner, 0, 100.0)
    managed = runner.positions[SYMBOL]
    managed.policy = replace(managed.policy, tick_size=0.1)
    before = list(runner.broker.events)
    with pytest.raises(ValueError, match="exit policy cannot change"):
        _event(runner, 1, 100.0)
    assert runner.broker.events == before
    assert len(runner.recorded_decisions) == 1


def test_pinned_session_mutation_is_rejected_before_broker_execution(tmp_path):
    runner = _runner(tmp_path)
    _event(runner, 0, 100.0)
    runner.clock = SessionClock(SessionPolicy(holidays=frozenset({START.date()})))
    before = list(runner.broker.events)
    with pytest.raises(ValueError, match="session policy cannot change"):
        runner.on_event(START + timedelta(minutes=6))
    assert runner.broker.events == before
    assert len(runner.recorded_decisions) == 1


@pytest.mark.parametrize("setting", ["execution", "daily_loss", "cost"])
def test_execution_and_risk_artifacts_cannot_drift_during_run(tmp_path, setting):
    runner = _runner(tmp_path)
    _event(runner, 0, 100.0)
    if setting == "execution":
        runner.broker.execution_policy = replace(
            runner.broker.execution_policy, slippage_bps=20
        )
    elif setting == "daily_loss":
        runner.daily_loss_limit = 500
    else:
        runner.broker.cost_service.brokerage_pct = 0.01
    before = list(runner.broker.events)
    with pytest.raises(ValueError, match="execution/risk settings cannot change"):
        _event(runner, 1, 100.0)
    assert runner.broker.events == before
    manifest = runner.finish()["manifest"]
    assert manifest["execution"]["execution_policy"]["slippage_bps"] == 0
    assert manifest["daily_loss_limit"] is None
