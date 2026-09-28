"""Operational acceptance retains every preclaimed attempt across restarts."""

import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting import acceptance
from backend.backtesting.promotion import promotion_artifact_hash
from backend.tests.test_acceptance_workflow import _plan, _read, _write


@pytest.fixture(autouse=True)
def source(monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "_source_identity",
        lambda: {
            "base_commit": "synthetic-test",
            "source_tree_sha256": "f" * 64,
            "source_files": {},
        },
    )


def registered(tmp_path, *, edit=None, study_id="cohort-fixture"):
    path, plan = _plan(tmp_path, study_id=study_id)
    day = datetime.now(timezone.utc).date().isoformat()
    plan["operational_cohorts"] = {
        "ISOLATED_PAPER": {
            "required_slots": [{"slot_id": "session-1", "session_dates": [day]}]
        }
    }
    if edit:
        edit(plan)
    _write(path, plan)
    directory = tmp_path / "study"
    return directory, acceptance.register_study(path, directory)


def failed_capture(registration, claim):
    started = datetime.fromisoformat(claim["claimed_at"]) + timedelta(seconds=1)
    return {
        "schema_version": "operational-run-v1",
        "study_id": registration["plan"]["study_id"],
        "policy_artifact_hash": promotion_artifact_hash(
            registration["plan"]["candidate_policy"]
        ),
        "mode": "ISOLATED_PAPER",
        "provenance": {
            "source_revision": registration["source"]["source_tree_sha256"],
            "capture_attempt_id": claim["attempt_id"],
            "capture_slot": claim["slot_id"],
            "capture_artifact_ref": claim["attempt_id"],
            "captured_started_at": started.isoformat(),
            "captured_ended_at": (started + timedelta(seconds=1)).isoformat(),
            "capture_complete": False,
        },
    }


@pytest.mark.parametrize(
    "slots",
    [
        [],
        [{"slot_id": "../escape", "session_dates": ["2026-09-29"]}],
        [{"slot_id": "slot", "session_dates": ["2026-02-30"]}],
        [{"slot_id": "slot", "session_dates": []}],
        [{"slot_id": "slot", "session_dates": ["2026-09-29"]}] * 2,
    ],
)
def test_registration_rejects_unusable_or_ambiguous_capture_slots(tmp_path, slots):
    def edit(plan):
        plan["operational_cohorts"]["ISOLATED_PAPER"]["required_slots"] = slots

    with pytest.raises(ValueError):
        registered(tmp_path, edit=edit)


def test_registration_rejects_cohort_shorter_than_frozen_session_minimum(tmp_path):
    def edit(plan):
        plan["operational_limits"]["minimum_sessions"] = 2

    with pytest.raises(ValueError, match="session minimum"):
        registered(tmp_path, edit=edit)


def test_pending_and_failed_attempts_remain_in_cohort_after_retry(tmp_path):
    directory, registration = registered(tmp_path)
    first = acceptance.claim_operational_capture(
        directory, "ISOLATED_PAPER", "session-1"
    )
    pending = acceptance.assess_study(directory)["operational"]["isolated_paper"]
    assert pending["passed"] is False
    report_path = tmp_path / "failed.json"
    _write(report_path, failed_capture(registration, first))
    failed = acceptance.record_operational_evidence(directory, report_path)
    assert failed["passed"] is False
    with pytest.raises(FileExistsError):
        acceptance.record_operational_evidence(directory, report_path)
    second = acceptance.claim_operational_capture(
        directory, "ISOLATED_PAPER", "session-1"
    )
    assert second["attempt_id"] != first["attempt_id"]
    after_retry = acceptance.assess_study(directory)["operational"]["isolated_paper"]
    assert after_retry["passed"] is False
    assert after_retry["report_sha256"] != failed["report_sha256"]
    assert (
        len(list((directory / "operational/isolated_paper/claims").glob("*.hash.json")))
        == 2
    )
    assert (
        len(
            list((directory / "operational/isolated_paper/reports").glob("*.hash.json"))
        )
        == 1
    )


def test_import_cannot_create_or_relabel_a_claim_after_capture(tmp_path):
    directory, registration = registered(tmp_path)
    claim = acceptance.claim_operational_capture(
        directory, "ISOLATED_PAPER", "session-1"
    )
    report = failed_capture(registration, claim)
    report["provenance"]["captured_started_at"] = (
        datetime.fromisoformat(claim["claimed_at"]) - timedelta(seconds=1)
    ).isoformat()
    path = tmp_path / "report.json"
    _write(path, report)
    with pytest.raises(ValueError, match="pre-capture claim"):
        acceptance.record_operational_evidence(directory, path)
    report["provenance"].pop("capture_attempt_id")
    _write(path, report)
    with pytest.raises(ValueError, match="pre-capture attempt"):
        acceptance.record_operational_evidence(directory, path)


def test_cohort_claims_and_reports_are_bound_to_registered_study(tmp_path):
    directory, registration = registered(tmp_path / "original")
    destination, _ = registered(tmp_path / "different", study_id="another-study")
    claim = acceptance.claim_operational_capture(
        directory, "ISOLATED_PAPER", "session-1"
    )
    path = tmp_path / "failed.json"
    _write(path, failed_capture(registration, claim))
    acceptance.record_operational_evidence(directory, path)
    shutil.copytree(directory / "operational", destination / "operational")
    with pytest.raises(ValueError, match="another study"):
        acceptance.assess_study(destination)
    stored = (
        directory / "operational/isolated_paper/reports" / f"{claim['attempt_id']}.json"
    )
    payload = _read(stored)
    payload["report"]["provenance"]["capture_complete"] = True
    _write(stored, payload)
    with pytest.raises(ValueError, match="artifact changed"):
        acceptance.assess_study(directory)


def test_claim_command_returns_only_identity_and_pinned_policy(tmp_path, capsys):
    directory, _ = registered(tmp_path)
    assert (
        acceptance.main(
            [
                "claim-operational",
                "--directory",
                str(directory),
                "--mode",
                "ISOLATED_PAPER",
                "--slot",
                "session-1",
            ]
        )
        == 0
    )
    claim = json.loads(capsys.readouterr().out)
    assert claim["capture_attempt_id"] == claim["attempt_id"]
    assert claim["capture_slot"] == "session-1"
    assert claim["live_activation"] == "DISABLED"
    with pytest.raises(ValueError, match="preregistered"):
        acceptance.claim_operational_capture(directory, "ISOLATED_PAPER", "undeclared")
