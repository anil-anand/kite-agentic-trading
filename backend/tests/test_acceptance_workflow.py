"""Run frozen study stages offline and challenge their evidence boundaries."""

import gzip
import json
import shutil
from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import pytest

from backend.backtesting import acceptance
from backend.backtesting.promotion import promotion_artifact_hash
from backend.exit_management.engine import ExitPolicy
from backend.replay import serialize_replay_artifact
from backend.tests.test_research_study import DAY, SYMBOL, case, data


def _write(path, payload):
    path.write_text(json.dumps(payload, allow_nan=False))


def _read(path):
    return json.loads(path.read_text())


def _shift(value, days):
    if isinstance(value, dict):
        return {name: _shift(item, days) for name, item in value.items()}
    if isinstance(value, list):
        return [_shift(item, days) for item in value]
    if isinstance(value, str) and "T" in value:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return value
        if timestamp.tzinfo is not None:
            return (timestamp + timedelta(days=days)).isoformat()
    return value


@pytest.fixture(autouse=True)
def fixed_source_identity(monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "_source_identity",
        lambda: {
            "base_commit": "synthetic-test",
            "source_tree_sha256": "f" * 64,
            "source_files": {},
        },
    )


def _plan(
    tmp_path,
    *,
    multi_session=False,
    variants=False,
    poison=None,
    oos_cases=True,
    study_id="offline-fixture",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    days = [0, 1, 2] if multi_session else [0, 2]
    frames, paths = [], []
    for day in days:
        seed = _shift(case(), day)
        seed["case_id"] = f"case-{day}"
        case_path = tmp_path / f"case-{day}.json"
        _write(case_path, seed)
        if oos_cases or day == 2:
            paths.append(case_path.name)
        frame = data()[SYMBOL].copy()
        frame["date"] += timedelta(days=day)
        frame["received_at"] = frame["date"] + timedelta(minutes=5)
        if poison == "holdout" and day == 2:
            frame["open"] = "withheld-invalid-price"
            frame["received_at"] = "withheld-invalid-receipt"
        if poison == "oos" and day == 0:
            frame["open"] = "bad-price"
        frames.append(frame)
    pd.concat(frames, ignore_index=True).to_csv(tmp_path / "history.csv", index=False)
    fold_end = DAY + timedelta(days=2 if multi_session else 1)
    plan = {
        "study_id": study_id,
        "data_source": "GENERATED_TEST_CANDLES",
        "data_classification": "SYNTHETIC_NON_SENSITIVE",
        "datasets": {SYMBOL: "history.csv"},
        "cases": paths,
        "oos_folds": [
            {
                "fold_id": "oos-1",
                "warmup_start": DAY.isoformat(),
                "test_start": DAY.isoformat(),
                "test_end": fold_end.isoformat(),
            }
        ],
        "holdout": {
            "fold_id": "holdout",
            "warmup_start": (DAY + timedelta(days=2)).isoformat(),
            "test_start": (DAY + timedelta(days=2)).isoformat(),
            "test_end": (DAY + timedelta(days=3)).isoformat(),
        },
        "candidate_policy": serialize_replay_artifact(ExitPolicy()),
        "control_policy": serialize_replay_artifact(
            ExitPolicy(policy_version="declared-control")
        ),
        "control_description": "Frozen same-engine comparison; no legacy equivalence claim",
        "variants": {
            "neighbor": serialize_replay_artifact(
                ExitPolicy(policy_version="predeclared-neighbor")
            )
        }
        if variants
        else {},
        "execution_scenarios": {"base": {"execution_policy": {"slippage_bps": 0}}},
        "criteria": {
            "minimum_oos_folds": 1,
            "minimum_completed_trades": 1,
            "maximum_unresolved_execution_rate": 0.1,
            "maximum_ambiguous_execution_rate": 0.1,
            "maximum_tail_adverse_r": 2,
            "minimum_capture_improvement_r": 0.1,
            "minimum_premature_exit_reduction": 0.1,
            "maximum_delayed_invalidation_increase": 0.1,
            "maximum_cost_stressed_loss_increase_r": 0.1,
        },
        "prior_trial_count": 0,
        "operational_limits": {
            "minimum_sessions": 1,
            "minimum_decisions": 1,
            "minimum_hold_decisions": 1,
            "minimum_closed_positions": 1,
            "maximum_observation_lag_seconds": 5,
            "maximum_unresolved_position_rate": 0.1,
        },
    }
    path = tmp_path / "plan.json"
    _write(path, plan)
    return path, plan


def _registered(tmp_path, **options):
    path, plan = _plan(tmp_path, **options)
    directory = tmp_path / "registered"
    registration = acceptance.register_study(path, directory)
    return directory, registration, path, plan


def test_real_oos_execution_then_once_only_holdout_uses_only_frozen_candidate(tmp_path):
    directory, registration, path, plan = _registered(tmp_path, variants=True)
    oos = acceptance.run_study_stage(directory, "oos")
    assert {row["policy_id"] for row in oos["results"]} == {"candidate", "neighbor"}
    assert all(
        row["report"]["paired_counts"]["complete"] == 1 for row in oos["results"]
    )
    assert all(
        row["report"]["parity"]["candidate"]["replayed_decisions"] >= 2
        for row in oos["results"]
    )
    assert (
        oos["results"][0]["report"]["candidate"]["trades"][0]["exit_reason"]
        == "THESIS_BREAKOUT_FAILED"
    )
    holdout = acceptance.run_study_stage(directory, "holdout")
    assert len(holdout["results"]) == 1
    assert {row["policy_id"] for row in holdout["results"]} == {"candidate"}
    assert holdout["registration_sha256"] == promotion_artifact_hash(registration)
    with pytest.raises(FileExistsError):
        acceptance.run_study_stage(directory, "holdout")
    status = acceptance.assess_study(directory)
    assert status["stages"]["oos"]["complete_pairs"] == 1
    assert status["stages"]["holdout"]["complete_pairs"] == 1
    assert status["promotion_ready"] is False
    assert status["live_activation"] == "DISABLED"
    assert status["data_classification"] == "SYNTHETIC_NON_SENSITIVE"


def test_multi_session_oos_executes_every_case_with_its_own_session_horizon(tmp_path):
    directory, _, _, _ = _registered(tmp_path, multi_session=True)
    result = acceptance.run_study_stage(directory, "oos")
    assert {row["case_id"] for row in result["results"]} == {"case-0", "case-1"}
    assert result["empty_folds"] == []
    for row in result["results"]:
        with gzip.open(directory / row["retained_report"]["file"], "rt") as retained:
            report = json.load(retained)
        assert report["paired_counts"]["complete"] == 1
        checkpoint = pd.Timestamp(
            report["artifacts"]["inputs"]["payload"]["case"]["checkpoint_at"]
        )
        dates = [
            pd.Timestamp(candle["date"])
            for candle in report["artifacts"]["data"]["payload"][SYMBOL]
        ]
        assert max(dates).date() == checkpoint.date()


def test_holdout_feature_and_receipt_poison_does_not_change_oos(tmp_path):
    clean, _, _, _ = _registered(tmp_path / "clean")
    poisoned, _, _, _ = _registered(tmp_path / "poisoned", poison="holdout")
    baseline = acceptance.run_study_stage(clean, "oos")
    actual = acceptance.run_study_stage(poisoned, "oos")
    clean_report = baseline["results"][0]["report"]
    poison_report = actual["results"][0]["report"]
    assert poison_report["candidate"]["trades"] == clean_report["candidate"]["trades"]
    assert poison_report["artifacts"]["data"] == clean_report["artifacts"]["data"]
    assert not (poisoned / "holdout.access.json").exists()
    with pytest.raises(ValueError):
        acceptance.run_study_stage(poisoned, "holdout")
    assert (poisoned / "holdout.failure.json").exists()


def test_frozen_original_input_files_can_change_without_changing_registered_run(
    tmp_path,
):
    directory, registration, path, plan = _registered(tmp_path)
    original_criteria = deepcopy(registration["plan"]["criteria"])
    plan["criteria"]["minimum_completed_trades"] = 999
    _write(path, plan)
    (tmp_path / "history.csv").write_text("untrusted replacement")
    result = acceptance.run_study_stage(directory, "oos")
    assert result["results"][0]["report"]["paired_counts"]["complete"] == 1
    assert (
        _read(directory / "registration.json")["plan"]["criteria"] == original_criteria
    )


@pytest.mark.parametrize("mutation", ["criteria", "data", "source"])
def test_changed_registered_criteria_data_or_code_fail_before_access(
    tmp_path, monkeypatch, mutation
):
    directory, registration, _, _ = _registered(tmp_path)
    if mutation == "criteria":
        registration["plan"]["criteria"]["minimum_completed_trades"] += 1
        _write(directory / "registration.json", registration)
    elif mutation == "data":
        path = directory / "inputs" / registration["plan"]["datasets"][SYMBOL]["file"]
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        monkeypatch.setattr(
            acceptance, "_source_identity", lambda: {"source_tree_sha256": "changed"}
        )
    with pytest.raises(ValueError):
        acceptance.run_study_stage(directory, "oos")
    assert not (directory / "oos.access.json").exists()


def test_cases_outside_all_registered_windows_are_rejected(tmp_path):
    path, plan = _plan(tmp_path)
    seed_path = tmp_path / plan["cases"][0]
    seed = _read(seed_path)
    seed["checkpoint_at"] = (DAY - timedelta(days=1)).isoformat()
    _write(seed_path, seed)
    with pytest.raises(ValueError, match="must belong"):
        acceptance.register_study(path, tmp_path / "registered")


def test_renamed_case_cannot_double_count_the_same_retained_position(tmp_path):
    path, plan = _plan(tmp_path)
    duplicate = _read(tmp_path / plan["cases"][0])
    duplicate["case_id"] = "same-position-renamed-case"
    _write(tmp_path / "duplicate.json", duplicate)
    plan["cases"].append("duplicate.json")
    _write(path, plan)
    with pytest.raises(ValueError, match="retained entry"):
        acceptance.register_study(path, tmp_path / "registered")


def test_empty_oos_coverage_cannot_consume_holdout(tmp_path):
    directory, _, _, _ = _registered(tmp_path, oos_cases=False)
    result = acceptance.run_study_stage(directory, "oos")
    assert result["empty_folds"] == ["oos-1"]
    with pytest.raises(ValueError, match="OOS study"):
        acceptance.run_study_stage(directory, "holdout")
    assert not (directory / "holdout.access.json").exists()


def test_failed_attempt_is_retained_and_cannot_be_retried(tmp_path):
    directory, _, _, _ = _registered(tmp_path, poison="oos")
    with pytest.raises(ValueError):
        acceptance.run_study_stage(directory, "oos")
    failure = _read(directory / "oos.failure.json")
    assert failure["status"] == "FAILED"
    assert "bad-price" not in json.dumps(failure)
    with pytest.raises(FileExistsError):
        acceptance.run_study_stage(directory, "oos")
    assert (
        acceptance.assess_study(directory)["stages"]["oos"]["status"]
        == "ATTEMPT_FAILED_OR_INTERRUPTED"
    )


def test_tampered_oos_result_cannot_unlock_holdout_or_report_progress(tmp_path):
    directory, _, _, _ = _registered(tmp_path)
    acceptance.run_study_stage(directory, "oos")
    path = directory / "oos.result.json"
    result = _read(path)
    result["trial_count"] += 1
    _write(path, result)
    with pytest.raises(ValueError, match="stage results"):
        acceptance.run_study_stage(directory, "holdout")
    with pytest.raises(ValueError, match="stage results"):
        acceptance.assess_study(directory)
    assert not (directory / "holdout.access.json").exists()


def test_copied_stage_and_hash_are_bound_to_original_registration(tmp_path):
    source, _, _, _ = _registered(tmp_path / "source", study_id="original")
    destination, _, _, _ = _registered(tmp_path / "destination", study_id="different")
    acceptance.run_study_stage(source, "oos")
    for suffix in ("result.json", "result.hash.json", "access.json"):
        shutil.copyfile(source / f"oos.{suffix}", destination / f"oos.{suffix}")
    with pytest.raises(ValueError, match="another study"):
        acceptance.run_study_stage(destination, "holdout")
    with pytest.raises(ValueError, match="another study"):
        acceptance.assess_study(destination)


def _record_insufficient_operations(directory, registration, report_path):
    report = {
        "schema_version": "operational-run-v1",
        "study_id": registration["plan"]["study_id"],
        "policy_artifact_hash": promotion_artifact_hash(
            registration["plan"]["candidate_policy"]
        ),
        "mode": "LIVE_SHADOW",
        "provenance": {"source_revision": registration["source"]["source_tree_sha256"]},
    }
    _write(report_path, report)
    result = acceptance.record_operational_evidence(directory, report_path)
    assert result["passed"] is False


def test_operational_status_recomputes_assessment_instead_of_trusting_pass_flag(
    tmp_path,
):
    directory, registration, _, _ = _registered(tmp_path)
    _record_insufficient_operations(directory, registration, tmp_path / "capture.json")
    path = directory / "live_shadow.json"
    payload = _read(path)
    payload["assessment"] = {"passed": True, "failures": []}
    _write(path, payload)
    with pytest.raises(ValueError, match="operational export"):
        acceptance.assess_study(directory)
    # Even an internally consistent copied hash must not make the forged cached
    # assessment replace validation of the retained operational facts.
    _write(
        directory / "live_shadow.hash.json",
        {"sha256": promotion_artifact_hash(payload)},
    )
    status = acceptance.assess_study(directory)
    assert status["operational"]["live_shadow"]["passed"] is False
    assert status["promotion_ready"] is False


def test_copied_operational_artifact_cannot_count_for_another_study(tmp_path):
    source, registration, _, _ = _registered(tmp_path / "source", study_id="original")
    destination, _, _, _ = _registered(tmp_path / "destination", study_id="different")
    _record_insufficient_operations(source, registration, tmp_path / "capture.json")
    for name in ("live_shadow.json", "live_shadow.hash.json"):
        shutil.copyfile(source / name, destination / name)
    with pytest.raises(ValueError, match="another study"):
        acceptance.assess_study(destination)


@pytest.mark.parametrize("revision", [None, "wrong-source"])
def test_operational_import_requires_executed_source_identity(tmp_path, revision):
    directory, registration, _, _ = _registered(tmp_path)
    report = {
        "study_id": registration["plan"]["study_id"],
        "policy_artifact_hash": promotion_artifact_hash(
            registration["plan"]["candidate_policy"]
        ),
        "provenance": {"source_revision": revision},
    }
    path = tmp_path / "capture.json"
    _write(path, report)
    with pytest.raises(ValueError, match="different source"):
        acceptance.record_operational_evidence(directory, path)


def test_hand_entered_confidence_claim_cannot_replace_measured_inconclusive_run(
    tmp_path,
):
    directory, registration, _, _ = _registered(tmp_path)
    result = acceptance.run_study_stage(directory, "oos")
    statistics = result["statistics"]
    evidence = {**statistics["metrics"], "capture_improvement_r_lower_bound": 1.0}
    failures = acceptance._executed_research_failures(
        "oos",
        result,
        {"acceptance_statistics_hashes": {"oos": promotion_artifact_hash(statistics)}},
        evidence,
        registration["plan"]["criteria"],
    )
    assert "EXECUTED_CAPTURE_IMPROVEMENT_R_LOWER_BOUND_MISMATCH" in failures
    assert "OOS_CAPTURE_INFERENCE_INCONCLUSIVE" in failures
    assert "OOS_STATISTICS_NOT_BOUND_TO_EXECUTED_RESULTS" not in failures


def test_holdout_must_meet_frozen_materiality_and_noninferiority_margins(tmp_path):
    directory, registration, _, _ = _registered(tmp_path)
    acceptance.run_study_stage(directory, "oos")
    result = acceptance.run_study_stage(directory, "holdout")
    failures = acceptance._executed_research_failures(
        "holdout", result, {}, {}, registration["plan"]["criteria"]
    )
    assert "HOLDOUT_MATERIALITY_NOT_ESTABLISHED" in failures
    assert (
        "HOLDOUT_DELAYED_INVALIDATION_INCREASE_UPPER_BOUND_NOT_ESTABLISHED" in failures
    )


def test_source_change_during_execution_consumes_attempt_without_publishing_result(
    tmp_path, monkeypatch
):
    directory, registration, _, _ = _registered(tmp_path)
    calls = []

    def changing_source():
        calls.append(True)
        source = deepcopy(registration["source"])
        if len(calls) > 1:
            source["source_tree_sha256"] = "e" * 64
        return source

    monkeypatch.setattr(acceptance, "_source_identity", changing_source)
    with pytest.raises(ValueError, match="source changed"):
        acceptance.run_study_stage(directory, "oos")
    assert (directory / "oos.access.json").exists()
    assert (directory / "oos.failure.json").exists()
    assert not (directory / "oos.result.json").exists()


def test_status_rejects_corrupted_retained_trace_even_if_stage_index_is_unchanged(
    tmp_path,
):
    directory, _, _, _ = _registered(tmp_path)
    result = acceptance.run_study_stage(directory, "oos")
    path = directory / result["results"][0]["retained_report"]["file"]
    path.write_bytes(path.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="compressed bytes changed"):
        acceptance.assess_study(directory)


def test_synthetic_classification_cannot_pass_a_submitted_promotion_review(tmp_path):
    from backend.tests.test_phase10_promotion_review import _review_package

    directory, _, _, _ = _registered(tmp_path)
    manifest, evidence, _ = _review_package()
    manifest["data_classification"] = "SYNTHETIC_NON_SENSITIVE"
    package_path = tmp_path / "review-fixture.json"
    _write(package_path, {"manifest": manifest, "evidence": evidence})
    result = acceptance.review_promotion_package(directory, package_path)
    assert result["passed"] is False
    assert "MISSING_OR_INELIGIBLE_RESEARCH_DATA_CLASSIFICATION" in result["failures"]
    status = acceptance.assess_study(directory)
    assert status["promotion_ready"] is False
    assert status["live_activation"] == "DISABLED"
    path = directory / "promotion.review.json"
    payload = _read(path)
    payload["assessment"] = {"passed": True, "failures": []}
    _write(path, payload)
    with pytest.raises(ValueError, match="promotion review"):
        acceptance.assess_study(directory)
    _write(
        directory / "promotion.review.hash.json",
        {"sha256": promotion_artifact_hash(payload)},
    )
    assert acceptance.assess_study(directory)["promotion_ready"] is False
    destination, _, _, _ = _registered(tmp_path / "different", study_id="different")
    for name in ("promotion.review.json", "promotion.review.hash.json"):
        shutil.copyfile(directory / name, destination / name)
    with pytest.raises(ValueError, match="another study"):
        acceptance.assess_study(destination)


def test_review_wrapper_rejects_unmeasured_claims_and_rechecks_saved_package(
    tmp_path, monkeypatch
):
    """Isolate review linkage; stubs cannot establish actual empirical acceptance."""
    from backend.backtesting.promotion import PromotionGateResult

    directory, registration, _, _ = _registered(tmp_path)
    oos = acceptance.run_study_stage(directory, "oos")
    holdout = acceptance.run_study_stage(directory, "holdout")
    plan = registration["plan"]
    op_hash = "a" * 64
    monkeypatch.setattr(
        acceptance,
        "evaluate_promotion_gate",
        lambda *_: PromotionGateResult(True, (), {"unit_test_contract_stub": True}),
    )
    monkeypatch.setattr(
        acceptance,
        "_operational_result",
        lambda *_: {"passed": True, "report_sha256": op_hash},
    )
    manifest = {
        "study_id": plan["study_id"],
        "registration_sha256": promotion_artifact_hash(registration),
        "source_commit": registration["source"]["base_commit"],
        "source_tree_sha256": registration["source"]["source_tree_sha256"],
        "dataset_hash": promotion_artifact_hash(plan["datasets"]),
        "data_classification": plan["data_classification"],
        "candidate_policy_artifacts": {
            "candidate": plan["candidate_policy"],
            **plan["variants"],
        },
        "control_policy": plan["control_policy"],
        "config": {"policy_version": plan["candidate_policy"]["policy_version"]},
        "prior_trial_count": plan["prior_trial_count"],
        "promotion_criteria_declared_at": registration["registered_at"],
        "policy_frozen_at": registration["registered_at"],
        "research_started_at": oos["started_at"],
        "untouched_holdout": {
            "start": plan["holdout"]["test_start"],
            "end_exclusive": plan["holdout"]["test_end"],
            "evaluated_at": holdout["completed_at"],
        },
        "acceptance_stage_hashes": {
            "oos": promotion_artifact_hash(oos),
            "holdout": promotion_artifact_hash(holdout),
        },
        "folds": plan["oos_folds"],
        "fold_results": [
            {
                "selected_policy_id": "candidate",
                "metrics": {
                    "trade_count": sum(
                        len(row["report"]["candidate"]["trades"])
                        for row in oos["results"]
                        if row["fold_id"] == fold["fold_id"]
                        and row["policy_id"] == "candidate"
                        and row["scenario_id"] == "base"
                    )
                },
            }
            for fold in plan["oos_folds"]
        ],
    }
    evidence = {
        "completed_trade_count": sum(
            item["metrics"]["trade_count"] for item in manifest["fold_results"]
        ),
        "reports": {
            gate: {"artifact": {"observations": {"operational_export_sha256": op_hash}}}
            for gate in (
                "shadow_operational_passed",
                "isolated_paper_operational_passed",
            )
        },
    }
    path = tmp_path / "wrapper-contract.json"
    _write(path, {"manifest": manifest, "evidence": evidence})
    result = acceptance.review_promotion_package(directory, path)
    assert result["passed"] is False
    assert "REGISTERED_CONTROL_IS_NOT_REPAIRED_LEGACY" in result["failures"]
    assert "OOS_PORTFOLIO_TREATMENT_COVERAGE_INCOMPLETE" in result["failures"]
    assert "OOS_CAPTURE_INFERENCE_INCONCLUSIVE" in result["failures"]
    assert result["live_activation"] == "DISABLED"
    assert acceptance.assess_study(directory)["promotion_ready"] is False
    # A matching outer hash does not replace actual binding checks on reread.
    retained_path = directory / "promotion.review.json"
    retained = _read(retained_path)
    retained["package"]["manifest"]["acceptance_stage_hashes"]["oos"] = "b" * 64
    _write(retained_path, retained)
    _write(
        directory / "promotion.review.hash.json",
        {"sha256": promotion_artifact_hash(retained)},
    )
    status = acceptance.assess_study(directory)
    assert status["promotion_ready"] is False
    assert "REPORTS_NOT_BOUND_TO_EXECUTED_STAGES" in status["remaining_acceptance"]
