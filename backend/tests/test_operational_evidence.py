"""Synthetic validator fixtures: these tests produce no operational evidence files.

A positive contract test checks consistency of supplied attestations only. It
cannot establish authentic data provenance or replace actual shadow/paper runs.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from backend.backtesting.operational_evidence import (
    OperationalLimits,
    assess_operational_run,
)
from backend.backtesting.promotion import promotion_artifact_hash
from backend.exit_management.engine import ExitPolicy
from backend.replay import (
    ExitReplayEvent,
    replay_exit_decisions,
    serialize_replay_artifact,
)
from backend.tests.exit_management.test_engine import _context, _risk, _state, _thesis
from backend.tests.test_candidate_execution import START

LIMITS = OperationalLimits(
    minimum_sessions=1,
    minimum_decisions=2,
    minimum_hold_decisions=1,
    minimum_closed_positions=1,
    maximum_observation_lag_seconds=10,
    maximum_unresolved_position_rate=0,
)


def _contract_fixture():
    key = "PAPER:test:NSE:123:RELIANCE:MIS:epoch"
    contexts = [
        _context(START + timedelta(minutes=5 * index), 98) for index in range(2)
    ]
    result = replay_exit_decisions(
        thesis=replace(_thesis(), position_key=key),
        position_state=replace(_state(), position_key=key),
        management_state=None,
        policy=ExitPolicy(),
        events=tuple(
            ExitReplayEvent(item, replace(_risk(item, mark=98), position_key=key))
            for item in contexts
        ),
    )
    records = [item.decision.to_dict() for item in result.evaluations]
    opened = START.isoformat()
    closed = (START + timedelta(minutes=11)).isoformat()
    return {
        "schema_version": "operational-run-v1",
        "study_id": "synthetic-contract-test-only",
        "policy_artifact_hash": promotion_artifact_hash(
            serialize_replay_artifact(ExitPolicy())
        ),
        "mode": "ISOLATED_PAPER",
        "provenance": {
            "acquisition_mode": "REAL_TIME",
            "capture_complete": True,
            "source_classification": "REAL_MARKET_DATA",
            "data_source_id": "unit-test-attestation-not-an-external-feed",
            "capture_artifact_ref": "unit-test-only",
            "captured_started_at": opened,
            "captured_ended_at": (START + timedelta(minutes=12)).isoformat(),
            "source_decision_count": 2,
            "source_position_count": 1,
            "source_fill_count": 2,
            "source_intent_count": 2,
        },
        "recorded_decisions": records,
        "capture_receipts": [
            {
                "decision_id": item["decision_id"],
                "received_at": item["occurred_at"],
                "persisted_at": item["occurred_at"],
            }
            for item in records
        ],
        "positions": [
            {
                "position_key": key,
                "namespace": "PAPER",
                "direction": "BUY",
                "initial_quantity": 10,
                "final_quantity": 0,
                "opened_at": opened,
                "reconciled_at": closed,
                "working_order_ids": [],
            }
        ],
        "intents": [
            {
                "intent_id": "entry-intent",
                "position_key": key,
                "origin": "ENTRY",
                "decision_ids": [],
                "order_ids": ["entry-order"],
                "status": "COMPLETE",
            },
            {
                "intent_id": "reduction-intent",
                "position_key": key,
                "origin": "CANDIDATE",
                "decision_ids": [records[1]["decision_id"]],
                "order_ids": ["reduction-order"],
                "status": "COMPLETE",
            },
        ],
        "fills": [
            {
                "fill_id": "entry-fill",
                "order_id": "entry-order",
                "position_key": key,
                "side": "BUY",
                "quantity": 10,
                "price": 100,
                "exchange_time": opened,
                "received_at": opened,
            },
            {
                "fill_id": "exit-fill",
                "order_id": "reduction-order",
                "position_key": key,
                "side": "SELL",
                "quantity": 10,
                "price": 98,
                "exchange_time": closed,
                "received_at": closed,
            },
        ],
        "censored_positions": [],
    }


def test_consistent_retained_contract_measures_decisions_and_execution():
    report = _contract_fixture()
    result = assess_operational_run(report, LIMITS, expected_policy=ExitPolicy())
    assert result["passed"], result["failures"]
    assert result["observations"]["hold_decision_count"] == 1
    assert result["observations"]["replay_verified_count"] == 2
    assert result["observations"]["timing_verified_count"] == 2
    assert result["observations"]["linked_fill_count"] == 2
    assert result["observations"]["closed_position_count"] == 1
    assert (
        result["verification_scope"]
        == "RETAINED_FACTS_WITH_EXTERNALLY_ATTESTED_PROVENANCE"
    )
    assert result["candidate_live_activation"] == "NOT_PERFORMED"


@pytest.mark.parametrize("classification", ["SYNTHETIC", "HISTORICAL_REPLAY", None])
def test_synthetic_or_replay_data_cannot_satisfy_operational_gate(classification):
    report = _contract_fixture()
    report["provenance"]["source_classification"] = classification
    result = assess_operational_run(report, LIMITS)
    assert not result["passed"]
    assert "MISSING_REAL_TIME_MARKET_PROVENANCE" in result["failures"]
    assert result["observations"]["replay_verified_count"] == 2


def test_relabeling_replay_namespace_as_paper_fails():
    report = _contract_fixture()
    report["manifest"] = {"execution": {"namespace": "REPLAY"}}
    result = assess_operational_run(report, LIMITS)
    assert "EXECUTION_MANIFEST_NAMESPACE_MISMATCH" in result["failures"]


def test_old_market_decisions_cannot_receive_current_capture_timestamps():
    report = _contract_fixture()
    for receipt in report["capture_receipts"]:
        receipt["received_at"] = "2026-10-01T04:30:00+00:00"
        receipt["persisted_at"] = "2026-10-01T04:30:01+00:00"
    result = assess_operational_run(report, LIMITS)
    assert "DECISION_OR_CONTEXT_CAPTURE_TIMING_INVALID" in result["failures"]
    assert result["observations"]["timing_verified_count"] == 0


def test_missing_hold_trace_cannot_be_hidden_by_exit_only_export():
    report = _contract_fixture()
    report["recorded_decisions"].pop(0)
    result = assess_operational_run(report, LIMITS)
    assert "EXPORT_COUNT_MISMATCH_SOURCE_DECISION_COUNT" in result["failures"]
    assert "CAPTURE_RECEIPTS_DO_NOT_COVER_ALL_DECISIONS" in result["failures"]
    assert "INSUFFICIENT_HOLD_DECISIONS" in result["failures"]


def test_corrupted_trace_fails_parity_even_when_identity_labels_match():
    report = _contract_fixture()
    report["recorded_decisions"][0]["trace"]["state_after"]["known_quantity"] = 3
    result = assess_operational_run(report, LIMITS)
    assert "RECORDED_DECISION_REPLAY_MISMATCH" in result["failures"]
    assert result["observations"]["replay_verified_count"] == 1


def test_wrong_policy_is_rejected_against_frozen_expected_artifact():
    report = _contract_fixture()
    result = assess_operational_run(
        report, LIMITS, expected_policy=replace(ExitPolicy(), tick_size=0.1)
    )
    assert "DECISION_POLICY_DIFFERS_FROM_PREDECLARED_POLICY" in result["failures"]
    assert "POLICY_IDENTITY_DIFFERS_FROM_PREDECLARED_POLICY" in result["failures"]


def test_missing_fill_never_becomes_a_closed_trade_from_acknowledgement():
    report = _contract_fixture()
    report["fills"].pop()
    result = assess_operational_run(report, LIMITS)
    assert "EXPORT_COUNT_MISMATCH_SOURCE_FILL_COUNT" in result["failures"]
    assert "FILLS_DO_NOT_RECONCILE_TO_POSITION" in result["failures"]


def test_missing_intent_link_blocks_paper_execution_evidence():
    report = _contract_fixture()
    report["intents"][1]["decision_ids"] = []
    result = assess_operational_run(report, LIMITS)
    assert "CANDIDATE_INTENT_HAS_NO_DECISION" in result["failures"]
    assert "PAPER_ACTION_MISSING_COORDINATOR_INTENT" in result["failures"]


def test_uncertain_residual_is_censored_and_counts_against_declared_limit():
    report = _contract_fixture()
    report["positions"][0]["final_quantity"] = 5
    report["fills"][1]["quantity"] = 5
    position_key = report["positions"][0]["position_key"]
    report["censored_positions"] = [
        {"position_key": position_key, "quantity": 5, "reason": "SUBMISSION_UNKNOWN"}
    ]
    result = assess_operational_run(report, LIMITS)
    assert "UNRESOLVED_POSITION_RATE_OUTSIDE_DECLARED_LIMIT" in result["failures"]
    assert "INSUFFICIENT_CLOSED_POSITIONS" in result["failures"]
    assert result["observations"]["unresolved_position_rate"] == 1
    assert result["observations"]["censored_position_count"] == 1


def test_entry_fill_received_after_decision_cannot_support_known_exposure():
    report = _contract_fixture()
    report["fills"][0]["received_at"] = (START + timedelta(minutes=6)).isoformat()
    result = assess_operational_run(report, LIMITS)
    assert "DECISION_QUANTITY_NOT_SUPPORTED_BY_OBSERVED_FILLS" in result["failures"]


def test_shadow_mode_requires_actual_suppressed_orchestration_trace():
    report = _contract_fixture()
    report["mode"] = "LIVE_SHADOW"
    result = assess_operational_run(report, LIMITS)
    assert "SHADOW_DISPATCH_SUPPRESSION_NOT_RETAINED" in result["failures"]
    assert "SHADOW_CANDIDATE_WAS_DISPATCHED" in result["failures"]


def test_duplicate_identity_is_not_extra_sample_size():
    report = _contract_fixture()
    report["recorded_decisions"].append(deepcopy(report["recorded_decisions"][0]))
    report["provenance"]["source_decision_count"] += 1
    result = assess_operational_run(report, LIMITS)
    assert "INVALID_OR_DUPLICATE_DECISION_ID" in result["failures"]
    assert result["observations"]["decision_count"] == 2


@pytest.mark.parametrize(
    "field", ["positions", "intents", "fills", "capture_receipts", "recorded_decisions"]
)
def test_missing_execution_or_observation_collection_fails_closed(field):
    report = _contract_fixture()
    del report[field]
    result = assess_operational_run(report, LIMITS)
    assert f"MISSING_OR_INVALID_{field.upper()}" in result["failures"]


@pytest.mark.parametrize("value", [False, 0, -1, float("nan")])
def test_invalid_limits_cannot_waive_required_sample(value):
    with pytest.raises(ValueError):
        replace(LIMITS, minimum_decisions=value)


def test_flat_quantity_with_unknown_submission_still_requires_censoring():
    report = _contract_fixture()
    report["intents"][1]["status"] = "UNKNOWN"
    result = assess_operational_run(report, LIMITS)
    assert "UNRESOLVED_POSITION_NOT_CENSORED" in result["failures"]
    assert result["observations"]["closed_position_count"] == 0
    assert result["observations"]["unresolved_intent_position_count"] == 1


def test_missing_fills_do_not_inflate_measured_closed_position_count():
    report = _contract_fixture()
    report["fills"].pop()
    result = assess_operational_run(report, LIMITS)
    assert result["observations"]["closed_position_count"] == 0


@pytest.mark.parametrize("bad_value", [[], {}, None, float("nan")])
def test_malformed_action_fails_without_crashing_report_import(bad_value):
    report = _contract_fixture()
    report["recorded_decisions"][0]["action"] = bad_value
    result = assess_operational_run(report, LIMITS)
    assert not result["passed"]
    assert "RECORDED_DECISION_REPLAY_MISMATCH" in result["failures"]


def test_actual_shadow_persistence_trace_schema_is_supported(monkeypatch):
    from backend.journal import journal
    from backend.tests.test_exit_shadow_state import _evaluate, _live

    engine, thesis, current = _live(monkeypatch)
    _evaluate(engine, current)
    current["context"] = _context(START + timedelta(minutes=5), 98)
    _evaluate(engine, current)
    records = [
        item["payload"] for item in journal.get_exit_decisions(thesis.position_key)
    ]
    report = _contract_fixture()
    report["mode"] = "LIVE_SHADOW"
    report["recorded_decisions"] = records
    report["capture_receipts"] = [
        {
            "decision_id": item["decision_id"],
            "received_at": item["occurred_at"],
            "persisted_at": item["occurred_at"],
        }
        for item in records
    ]
    for field in ("positions", "intents", "fills"):
        for item in report[field]:
            item["position_key"] = thesis.position_key
    report["positions"][0]["namespace"] = "LIVE"
    report["intents"][1]["origin"] = "LEGACY_CONTROL"
    report["intents"][1]["decision_ids"] = []
    result = assess_operational_run(report, LIMITS, expected_policy=ExitPolicy())
    assert result["passed"], result["failures"]
    assert result["observations"]["shadow_suppressed_count"] == 2
    assert result["observations"]["replay_verified_count"] == 2


def test_duplicate_candidate_dispatch_from_one_decision_is_rejected():
    report = _contract_fixture()
    duplicate = deepcopy(report["intents"][1])
    duplicate.update(intent_id="duplicate-reduction", order_ids=["duplicate-order"])
    report["intents"].append(duplicate)
    report["provenance"]["source_intent_count"] += 1
    result = assess_operational_run(report, LIMITS)
    assert "DECISION_BOUND_TO_MULTIPLE_CANDIDATE_INTENTS" in result["failures"]


def test_hold_cannot_be_used_to_justify_candidate_order():
    report = _contract_fixture()
    report["intents"][1]["decision_ids"] = [
        report["recorded_decisions"][0]["decision_id"]
    ]
    result = assess_operational_run(report, LIMITS)
    assert "CANDIDATE_DISPATCH_HAS_NO_MUTATION_DECISION" in result["failures"]
