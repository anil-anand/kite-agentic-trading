"""Synthetic cohort contracts do not substitute for real operational sessions."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from backend.backtesting.operational_capture import OperationalRecorder
from backend.backtesting.operational_evidence import assess_operational_cohort
from backend.backtesting.paper_session import RealtimePaperSession
from backend.exit_management.engine import ExitPolicy
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_operational_capture import Clock, plan
from backend.tests.test_operational_evidence import LIMITS, START, _contract_fixture


def cohort():
    reports, claims, slots = {}, [], []
    for day in (0, 1):
        at = START + timedelta(days=day)

        def context(start, close):
            return replace(_context(start, close), session_id=start.date().isoformat())

        with (
            patch("backend.tests.test_operational_evidence.START", at),
            patch("backend.tests.test_operational_evidence._context", context),
        ):
            report = _contract_fixture()
        attempt, slot = f"attempt-{day}", f"slot-{day}"
        report["provenance"].update(
            source_revision="frozen-source",
            capture_artifact_ref=f"capture-{day}",
            capture_slot=slot,
            capture_attempt_id=attempt,
        )
        reports[attempt] = report
        claims.append(
            {
                "attempt_id": attempt,
                "slot_id": slot,
                "claimed_at": (at - timedelta(seconds=1)).isoformat(),
            }
        )
        slots.append({"slot_id": slot, "session_dates": [at.date().isoformat()]})
    declaration = {
        "study_id": report["study_id"],
        "source_revision": "frozen-source",
        "mode": "ISOLATED_PAPER",
        "policy_artifact_hash": report["policy_artifact_hash"],
        "slots": slots,
    }
    limits = replace(
        LIMITS,
        minimum_sessions=2,
        minimum_decisions=4,
        minimum_hold_decisions=2,
        minimum_closed_positions=2,
    )
    return reports, limits, declaration, claims


def assess(values):
    return assess_operational_cohort(*values, expected_policy=ExitPolicy())


def test_distinct_sessions_meet_aggregate_minima_without_rewriting_replay_ids():
    values = cohort()
    original = deepcopy(values[0])
    result = assess(values)
    assert result["passed"], result["failures"]
    assert result["observations"]["session_count"] == 2
    assert result["observations"]["closed_position_count"] == 2
    assert result["observations"]["replay_verified_count"] == 4
    assert all(not item["passed"] for item in result["capture_assessments"].values())
    assert values[0] == original


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda r: r["attempt-0"]["provenance"].update(capture_complete=False),
            "CAPTURE_INCOMPLETE_OR_FAILED",
        ),
        (
            lambda r: r["attempt-0"]["provenance"].update(
                capture_artifact_ref="capture-1"
            ),
            "DUPLICATE_OPERATIONAL_CAPTURE_IDENTITY",
        ),
        (
            lambda r: r["attempt-0"]["provenance"].update(
                source_revision="another-source"
            ),
            "CAPTURE_IDENTITY_DIFFERS_FROM_COHORT",
        ),
        (
            lambda r: r["attempt-0"]["provenance"].update(
                capture_attempt_id="attempt-1"
            ),
            "CAPTURE_DOES_NOT_MATCH_CLAIM",
        ),
        (lambda r: r.pop("attempt-0"), "MISSING_CLAIMED_OPERATIONAL_CAPTURE"),
    ],
)
def test_failed_missing_duplicated_or_relabelled_capture_cannot_be_hidden(
    mutation, reason
):
    values = cohort()
    mutation(values[0])
    result = assess(values)
    assert not result["passed"]
    assert any(reason in item for item in result["failures"])


def test_repeated_same_session_does_not_meet_two_session_minimum():
    values = cohort()
    copied = deepcopy(values[0]["attempt-0"])
    copied["provenance"].update(
        capture_attempt_id="attempt-1",
        capture_slot="slot-1",
        capture_artifact_ref="capture-1",
    )
    values[0]["attempt-1"] = copied
    values[3][1]["claimed_at"] = values[3][0]["claimed_at"]
    result = assess(values)
    assert "INSUFFICIENT_SESSIONS" in result["failures"]
    assert "OVERLAPPING_OPERATIONAL_CAPTURES" in result["failures"]
    assert any("SESSION_COVERAGE_INCOMPLETE" in item for item in result["failures"])


@pytest.mark.parametrize(
    "field",
    ["positions", "intents", "fills", "recorded_decisions", "censored_positions"],
)
def test_malformed_capture_is_retained_as_failure_without_crashing_cohort(field):
    values = cohort()
    values[0]["attempt-0"][field] = None
    result = assess(values)
    assert not result["passed"]
    assert any("MISSING_OR_INVALID" in item for item in result["failures"])


def test_malformed_order_lists_do_not_crash_aggregation():
    values = cohort()
    values[0]["attempt-0"]["intents"][0]["order_ids"] = None
    assert not assess(values)["passed"]


def test_lazy_report_mapping_is_loaded_once_per_capture():
    reports, *rest = cohort()

    class Lazy(Mapping):
        def __init__(self):
            self.loads = []

        def __iter__(self):
            return iter(reports)

        def __len__(self):
            return len(reports)

        def __getitem__(self, key):
            self.loads.append(key)
            return deepcopy(reports[key])

    lazy = Lazy()
    assert assess((lazy, *rest))["passed"]
    assert lazy.loads == ["attempt-0", "attempt-1"]


@pytest.mark.parametrize("paper_adapter", [False, True])
def test_recorder_preserves_claim_identity_on_export_and_reopen(
    tmp_path, paper_adapter
):
    declared = {**plan(), "capture_slot": "slot-0", "capture_attempt_id": "attempt-0"}
    clock = Clock()
    if paper_adapter:
        recorder = RealtimePaperSession(
            tmp_path / "capture", plan=declared, fixture_clock=clock
        ).recorder
    else:
        recorder = OperationalRecorder(
            tmp_path / "capture", plan=declared, fixture_clock=clock
        )
    clock.at += timedelta(seconds=1)
    report = recorder.finish()
    assert report == OperationalRecorder(tmp_path / "capture").export()
    assert report["provenance"]["capture_slot"] == "slot-0"
    assert report["provenance"]["capture_attempt_id"] == "attempt-0"
    assert report["provenance"]["source_classification"] == "SYNTHETIC_FIXTURE"


def test_recorder_rejects_partial_claim_identity(tmp_path):
    with pytest.raises(ValueError, match="immutable identity"):
        OperationalRecorder(
            tmp_path / "capture",
            plan={**plan(), "capture_slot": "slot"},
            fixture_clock=Clock(),
        )
