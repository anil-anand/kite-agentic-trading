"""Policy compatibility and immutable checkpoint regressions from phase-6 review."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import (
    DecisionRecord,
    ExitAction,
    ManagementProfileSnapshot,
    ManagementState,
    PositionCheckpoint,
)
from backend.exit_management.profiles import (
    ManagementProfile,
    ManagementProfileName,
    ObjectiveMode,
    resolve_profile,
)
from backend.exit_management.thesis import (
    EntryThesis,
    bind_terminal_fill,
    capture_entry_thesis,
)
from backend.journal import TradeJournal

from .test_engine import _context, _risk, _state, _thesis
from .test_thesis import _config, _signal


@pytest.mark.parametrize("mode", ["runner-v2", "FIXED_OBEJCTIVE", None, {}, []])
def test_unsupported_recorded_objective_mode_cannot_invent_target_exit(mode):
    thesis = _thesis()
    thesis = replace(
        thesis,
        management_profile=replace(
            thesis.management_profile,
            values={**thesis.management_profile.values, "objective_mode": mode},
        ),
    )
    profile = resolve_profile(thesis.management_profile)
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=111)

    result = evaluate_exit(
        thesis, _state(), context, _risk(context, mark=111), ExitPolicy()
    )

    assert profile.objective_mode is ObjectiveMode.NONE
    assert profile.normal_thesis_management
    assert result.decision.action is ExitAction.HOLD
    assert result.proposed_intent is None
    # The unsupported source value remains visible in the replay record.
    recorded = result.decision.to_dict()["trace"]["policy_artifact"]["profile"]
    assert recorded["values"]["objective_mode"] == mode


def test_unsupported_objective_does_not_disable_valid_thesis_failure():
    thesis = _thesis()
    thesis = replace(
        thesis,
        management_profile=replace(
            thesis.management_profile,
            values={
                **thesis.management_profile.values,
                "objective_mode": "runner-v2",
            },
        ),
    )
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=98)
    first = evaluate_exit(
        thesis, _state(), context, _risk(context, mark=98), ExitPolicy()
    )
    next_context = _context(context.primary_bar.end, close=98)
    second = evaluate_exit(
        thesis,
        first.next_position_state,
        next_context,
        _risk(next_context, mark=98),
        ExitPolicy(),
        management_state=first.next_management_state,
    )

    assert first.decision.action is ExitAction.HOLD
    assert second.decision.action is ExitAction.REQUEST_EXIT
    assert second.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (ManagementProfileName.TREND_CONTINUATION, ObjectiveMode.STRUCTURE_RUNNER),
        (ManagementProfileName.BREAKOUT_FOLLOW_THROUGH, ObjectiveMode.FIXED_OBJECTIVE),
        (ManagementProfileName.RANGE_CONVERGENCE, ObjectiveMode.FIXED_OBJECTIVE),
        (ManagementProfileName.UNKNOWN_LEGACY_BOUNDED, ObjectiveMode.NONE),
    ],
)
def test_omitted_objective_mode_preserves_declared_profile_default(name, expected):
    snapshot = replace(_thesis().management_profile, name=name, values={})

    assert resolve_profile(snapshot).objective_mode is expected


@pytest.mark.parametrize(
    "field",
    [
        "last_processed_primary_bar_id",
        "policy_fingerprint",
        "latched_exit_reason_code",
        "failure_episode",
        "local_failure_episode",
        "last_favorable_progress_bar_id",
        "observed_extrema_source",
        "favorable_structure_id",
    ],
)
@pytest.mark.parametrize("value", [[], {}, True, 1, "", " "])
def test_management_restore_rejects_mutable_or_invalid_event_identity(field, value):
    payload = json.loads(json.dumps({field: value}))

    with pytest.raises(ValueError, match="nonempty text"):
        ManagementState.from_dict(payload)


def test_initiating_reason_round_trips_without_rewriting_legacy_checkpoints():
    state = ManagementState(
        latched_exit_reason_code="THESIS_BREAKOUT_FAILED",
        latched_exit_urgency="CRITICAL",
    )

    assert ManagementState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
    assert ManagementState.from_dict({}).latched_exit_reason_code is None
    assert ManagementState.from_dict({}).latched_exit_urgency is None


@pytest.mark.parametrize("urgency", [[], {}, True, 1, "", "critical", "HIGH"])
def test_latched_urgency_restore_rejects_unknown_or_mutable_values(urgency):
    with pytest.raises(ValueError, match="urgency must be NORMAL or CRITICAL"):
        ManagementState.from_dict({"latched_exit_urgency": urgency})


@pytest.mark.parametrize("urgency", ["NORMAL", "CRITICAL"])
def test_decision_records_explicit_urgency(urgency):
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    result = evaluate_exit(_thesis(), _state(), context, _risk(context), ExitPolicy())

    assert replace(result.decision, urgency=urgency).to_dict()["urgency"] == urgency


@pytest.mark.parametrize("values", [None, [], "invalid", True, 10])
def test_nonmapping_profile_is_rejected_before_policy_or_thesis_restore(values):
    with pytest.raises(ValueError, match="values must be a mapping"):
        ManagementProfile(ManagementProfileName.BREAKOUT_FOLLOW_THROUGH, values=values)
    with pytest.raises(ValueError, match="values must be a mapping"):
        ManagementProfileSnapshot(values=values)
    payload = _thesis().to_dict()
    payload["management_profile"]["values"] = values
    with pytest.raises(ValueError, match="values must be a mapping"):
        EntryThesis.from_dict(payload)


def test_production_thesis_snapshot_resolves_without_rewriting_phase5_contract():
    signal = _signal()
    signal["market_context"].update(atr=2.0, observation_quality={"atr": "VALID"})
    thesis = capture_entry_thesis(
        signal,
        position_key=_state().position_key,
        trade_id="production-thesis",
        position_epoch="epoch-1",
        instrument_id="1",
        effective_config=_config(),
        created_at="2026-09-20T04:25:01+00:00",
    )
    thesis = bind_terminal_fill(
        thesis,
        entry_vwap=100,
        filled_quantity=10,
        terminal_at="2026-09-21T04:20:00+00:00",
        source_fill_ids=("entry-fill",),
    )
    original = json.loads(json.dumps(thesis.to_dict()))
    thesis = EntryThesis.from_dict(original)
    profile = resolve_profile(thesis.management_profile)
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=98)

    result = evaluate_exit(thesis, _state(), context, _risk(context), ExitPolicy())

    assert profile.version == "management-profiles-v1"
    assert profile.objective_mode is ObjectiveMode.FIXED_OBJECTIVE
    assert result.next_management_state.failure_count == 1
    assert thesis.to_dict() == original


def test_phase6_evidence_and_memory_round_trip_through_phase5_serializers():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=101)
    initial = _state()
    result = evaluate_exit(_thesis(), initial, context, _risk(context), ExitPolicy())
    decision = result.decision
    record = DecisionRecord(
        decision_id=decision.decision_id,
        position_key=initial.position_key,
        occurred_at=decision.occurred_at,
        action=decision.action.value,
        primary_reason_code=decision.primary_reason_code,
        policy_version=decision.policy_version,
        state_before=initial,
        state_after=result.next_position_state,
        supporting_evidence=decision.supporting_evidence,
        opposing_evidence=decision.opposing_evidence,
        trace=decision.trace,
    )
    state = result.next_position_state
    checkpoint = PositionCheckpoint(
        position_key=state.position_key,
        state_version=state.version,
        sequence=1,
        state=state,
        counters={"exit_policy": result.next_management_state.to_dict()},
    )

    stored_record = json.loads(json.dumps(record.to_dict(), allow_nan=False))
    restored = PositionCheckpoint.from_dict(
        json.loads(json.dumps(checkpoint.to_dict()))
    )

    assert stored_record["supporting_evidence"]
    assert (
        stored_record["supporting_evidence"]
        == decision.to_dict()["supporting_evidence"]
    )
    assert (
        ManagementState.from_dict(restored.counters["exit_policy"])
        == result.next_management_state
    )


def test_quote_only_extrema_commit_and_restore_with_phase5_journal(tmp_path):
    journal = TradeJournal(str(tmp_path / "phase6-checkpoints.db"))
    initial = _state()
    thesis = _thesis()
    checkpoint = PositionCheckpoint(
        position_key=initial.position_key,
        state_version=initial.version,
        sequence=0,
        state=initial,
    )
    journal.create_managed_position(thesis, checkpoint)
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))

    result = evaluate_exit(
        thesis, initial, None, _risk(context, mark=103), ExitPolicy()
    )
    state, memory, decision = (
        result.next_position_state,
        result.next_management_state,
        result.decision,
    )
    assert state.version == initial.version + 1
    assert memory.eligible_completed_bars == 0
    assert memory.observed_mfe_r == pytest.approx(0.6)
    record = DecisionRecord(
        decision_id=decision.decision_id,
        position_key=initial.position_key,
        occurred_at=decision.occurred_at,
        action=decision.action.value,
        primary_reason_code=decision.primary_reason_code,
        policy_version=decision.policy_version,
        state_before=initial,
        state_after=state,
        trace=decision.trace,
    )
    journal.commit_position_checkpoint(
        PositionCheckpoint(
            position_key=state.position_key,
            state_version=state.version,
            sequence=1,
            state=state,
            counters={"exit_policy": memory.to_dict()},
        ),
        event_id=state.last_event_id,
        event_type="STATE_OBSERVED",
        expected_state_version=initial.version,
        decision=record,
    )

    restarted = TradeJournal(str(tmp_path / "phase6-checkpoints.db"))
    restored = restarted.get_managed_position(state.position_key)
    restored_checkpoint = PositionCheckpoint.from_dict(restored["state"])
    assert restored_checkpoint.state == state
    assert (
        ManagementState.from_dict(restored_checkpoint.counters["exit_policy"]) == memory
    )
    saved_decision = restarted.get_exit_decisions(state.position_key)[0]["payload"]
    assert saved_decision["trace"]["management_after"] == memory.to_dict()
