"""Adversarial review regressions for promotion and fixture provenance."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from backend.backtesting.promotion import (
    PromotionCriteria,
    evaluate_promotion_gate,
    promotion_artifact_hash,
    promotion_context_hash,
)
from backend.backtesting.regression_suite import RegressionSuite
from backend.strategies.base import BaseStrategy


def _review_package():
    """Synthetic test attestations only; no operational evidence is generated."""
    criteria = PromotionCriteria(
        minimum_oos_folds=2,
        minimum_completed_trades=10,
        maximum_unresolved_execution_rate=0.01,
        maximum_ambiguous_execution_rate=0.05,
        maximum_tail_adverse_r=2.0,
        minimum_capture_improvement_r=0.1,
        maximum_delayed_invalidation_increase=0.01,
        maximum_cost_stressed_loss_increase_r=0.1,
    )
    criteria_hash = promotion_artifact_hash(asdict(criteria))
    policies = {"candidate": {"policy_version": "test-policy", "confirmation_bars": 2}}
    manifest = {
        "schema_version": "walk-forward-manifest-v1",
        "promotion_status": "RESEARCH_EVIDENCE_ONLY",
        "runner_contract": "SHARED_CANDIDATE_STACK_VERIFIED",
        "data_classification": "CURATED_NON_SENSITIVE",
        "study_id": "review-test-only",
        "source_commit": "test-source",
        "dataset_hash": "d" * 64,
        "candidate_policy_artifacts": policies,
        "policy_artifact_hash": promotion_artifact_hash(policies),
        "promotion_criteria_sha256": criteria_hash,
        "promotion_criteria_declared_at": "2026-06-01T00:00:00+00:00",
        "research_started_at": "2026-06-02T00:00:00+00:00",
        "policy_frozen_at": "2026-06-01T00:00:00+00:00",
        "config": {"policy_version": "test-policy"},
        "folds": [
            {
                "test_start": "2026-02-01T00:00:00+00:00",
                "test_end": "2026-02-02T00:00:00+00:00",
            },
            {
                "test_start": "2026-02-02T00:00:00+00:00",
                "test_end": "2026-02-03T00:00:00+00:00",
            },
        ],
        "fold_results": [
            {"metrics": {"trade_count": 5}},
            {"metrics": {"trade_count": 5}},
        ],
        "untouched_holdout": {
            "status": "EVALUATED_AND_FROZEN",
            "start": "2026-03-01T00:00:00+00:00",
            "end_exclusive": "2026-03-02T00:00:00+00:00",
            "evaluated_at": "2026-06-03T00:00:00+00:00",
        },
    }
    for index, result in enumerate(manifest["fold_results"]):
        result["fold"] = manifest["folds"][index].copy()
        result["selected_policy_id"] = "candidate"
        result["selected_policy_artifact_hash"] = promotion_artifact_hash(
            policies["candidate"]
        )
    metrics = {
        "oos_fold_count": 2,
        "completed_trade_count": 10,
        "unresolved_execution_rate": 0.0,
        "ambiguous_execution_rate": 0.01,
        "tail_adverse_r": 1.0,
        "capture_improvement_r_lower_bound": 0.12,
        "premature_exit_reduction_lower_bound": None,
        "delayed_invalidation_increase_upper_bound": 0.005,
        "cost_stressed_loss_increase_r_upper_bound": 0.05,
    }
    evidence = {"criteria_sha256": criteria_hash, **metrics, "reports": {}}
    gates = (
        "p0_safety_tests_passed",
        "decision_parity_passed",
        "replay_coverage_complete",
        "shadow_operational_passed",
        "isolated_paper_operational_passed",
        "paired_and_portfolio_studies_passed",
        "ablation_and_perturbation_passed",
        "multi_cohort_robustness_passed",
        "execution_and_data_stress_passed",
        "oos_materiality_and_noninferiority_passed",
        "untouched_holdout_passed",
        "F26",
        "F29",
        "F30",
    )
    for gate in gates:
        artifact = {
            "study_id": manifest["study_id"],
            "source_commit": manifest["source_commit"],
            "dataset_hash": manifest["dataset_hash"],
            "research_context_sha256": promotion_context_hash(manifest),
            "policy_artifact_hash": manifest["policy_artifact_hash"],
            "criteria_sha256": criteria_hash,
            "gate": gate,
            "report_ref": f"test-only/{gate}.json",
            "observations": {"test_fixture_only": True},
        }
        if gate == "oos_materiality_and_noninferiority_passed":
            artifact["observations"] = {"metrics": metrics.copy()}
        evidence["reports"][gate] = {
            "passed": True,
            "artifact": artifact,
            "sha256": promotion_artifact_hash(artifact),
        }
    manifest["untouched_holdout"]["result_artifact_sha256"] = evidence["reports"][
        "untouched_holdout_passed"
    ]["sha256"]
    evidence["manifest_sha256"] = promotion_artifact_hash(manifest)
    return manifest, evidence, criteria


def test_complete_bound_attestations_are_reviewable_but_never_activate_live():
    result = evaluate_promotion_gate(*_review_package())
    assert result.failures == ()
    assert result.passed is True
    assert (
        result.evidence_summary["candidate_live_activation"]
        == "NOT_PERFORMED_BY_RESEARCH_GATE"
    )
    assert (
        "NOT_INDEPENDENT_VERIFICATION" in result.evidence_summary["verification_scope"]
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("oos_fold_count", True),
        ("completed_trade_count", True),
        ("unresolved_execution_rate", -0.1),
        ("ambiguous_execution_rate", -0.1),
        ("tail_adverse_r", -1),
        ("tail_adverse_r", float("nan")),
        ("delayed_invalidation_increase_upper_bound", 0.1),
        ("delayed_invalidation_increase_upper_bound", -2),
        ("cost_stressed_loss_increase_r_upper_bound", 0.2),
        ("capture_improvement_r_lower_bound", 0.09),
    ],
)
def test_invalid_or_inferior_research_cannot_pass(name, value):
    manifest, evidence, criteria = _review_package()
    evidence[name] = value
    if value == value:
        report = evidence["reports"]["oos_materiality_and_noninferiority_passed"]
        report["artifact"]["observations"]["metrics"][name] = value
        report["sha256"] = promotion_artifact_hash(report["artifact"])
    assert evaluate_promotion_gate(manifest, evidence, criteria).passed is False


@pytest.mark.parametrize("value", [-2, 20])
def test_premature_exit_rate_difference_rejects_impossible_bounds(value):
    manifest, evidence, criteria = _review_package()
    evidence["premature_exit_reduction_lower_bound"] = value
    report = evidence["reports"]["oos_materiality_and_noninferiority_passed"]
    report["artifact"]["observations"]["metrics"][
        "premature_exit_reduction_lower_bound"
    ] = value
    report["sha256"] = promotion_artifact_hash(report["artifact"])
    result = evaluate_promotion_gate(manifest, evidence, criteria)
    assert "PREMATURE_EXIT_REDUCTION_OUTSIDE_RATE_DIFFERENCE_DOMAIN" in result.failures


def test_reports_cannot_be_reused_for_a_different_dataset_or_run_context():
    manifest, evidence, criteria = _review_package()
    manifest["dataset_hash"] = "c" * 64
    evidence["manifest_sha256"] = promotion_artifact_hash(manifest)
    result = evaluate_promotion_gate(manifest, evidence, criteria)
    assert "P0_SAFETY_TESTS_PASSED" in result.failures
    manifest, evidence, criteria = _review_package()
    manifest["config"]["confirmation_bars"] = 999
    evidence["manifest_sha256"] = promotion_artifact_hash(manifest)
    assert not evaluate_promotion_gate(manifest, evidence, criteria).passed


@pytest.mark.parametrize(
    "policies", [None, {}, {"candidate": {"policy_version": "modified"}}]
)
def test_promotion_requires_retained_policy_bytes_matching_the_declared_hash(policies):
    manifest, evidence, criteria = _review_package()
    if policies is None:
        del manifest["candidate_policy_artifacts"]
    else:
        manifest["candidate_policy_artifacts"] = policies
    result = evaluate_promotion_gate(manifest, evidence, criteria)
    assert "POLICY_ARTIFACTS_MISSING_OR_HASH_MISMATCH" in result.failures


@pytest.mark.parametrize(
    "mutation",
    [
        {"selected_policy_id": "unregistered"},
        {"selected_policy_artifact_hash": None},
        {"selected_policy_artifact_hash": "b" * 64},
    ],
)
def test_each_fold_must_identify_its_retained_selected_policy(mutation):
    manifest, evidence, criteria = _review_package()
    manifest["fold_results"][0].update(mutation)
    result = evaluate_promotion_gate(manifest, evidence, criteria)
    assert "FOLD_POLICY_NOT_BOUND_TO_RETAINED_ARTIFACTS" in result.failures


@pytest.mark.parametrize(
    "change",
    [
        {"require_untouched_holdout": False},
        {"required_security_gates": ()},
        {"maximum_unresolved_execution_rate": 1.1},
        {"maximum_ambiguous_execution_rate": -0.1},
    ],
)
def test_promotion_criteria_cannot_waive_mandatory_gates(change):
    _, _, criteria = _review_package()
    with pytest.raises(ValueError):
        replace(criteria, **change)


def test_manifest_report_and_predeclared_margin_tampering_fail_closed():
    manifest, evidence, criteria = _review_package()
    manifest["policy_artifact_hash"] = "b" * 64
    assert not evaluate_promotion_gate(manifest, evidence, criteria).passed
    manifest, evidence, criteria = _review_package()
    evidence["reports"]["F26"]["artifact"]["source_commit"] = "other-source"
    assert (
        "SECURITY_GATE_F26_NOT_RESOLVED"
        in evaluate_promotion_gate(manifest, evidence, criteria).failures
    )
    manifest, evidence, criteria = _review_package()
    relaxed = replace(criteria, maximum_delayed_invalidation_increase=0.5)
    assert (
        "CRITERIA_NOT_PINNED_IN_RESEARCH_MANIFEST"
        in evaluate_promotion_gate(manifest, evidence, relaxed).failures
    )


@pytest.mark.parametrize(
    "gate", ["F26", "shadow_operational_passed", "multi_cohort_robustness_passed"]
)
def test_bare_pass_boolean_or_reused_report_cannot_replace_retained_evidence(gate):
    manifest, evidence, criteria = _review_package()
    evidence["reports"][gate] = True
    assert not evaluate_promotion_gate(manifest, evidence, criteria).passed
    evidence["reports"][gate] = deepcopy(evidence["reports"]["p0_safety_tests_passed"])
    assert not evaluate_promotion_gate(manifest, evidence, criteria).passed


@pytest.mark.parametrize(
    "mutation",
    [
        {"runner_contract": "EXTERNAL_CALLBACK_UNVERIFIED"},
        {"data_classification": "SYNTHETIC_NON_SENSITIVE"},
        {"promotion_criteria_declared_at": "2026-06-03T00:00:00+00:00"},
        {"folds": [{"test_start": "invalid", "test_end": "invalid"}]},
        {"fold_results": [{"metrics": {"trade_count": 99}}]},
    ],
)
def test_unverified_synthetic_retrospective_or_inconsistent_runs_fail(mutation):
    manifest, evidence, criteria = _review_package()
    manifest.update(mutation)
    evidence["manifest_sha256"] = promotion_artifact_hash(manifest)
    assert not evaluate_promotion_gate(manifest, evidence, criteria).passed


class _NoSignals(BaseStrategy):
    def get_name(self):
        return "review-fixture"

    def get_description(self):
        return "No signals"

    def calculate_signals(self, _df, _symbol):
        return []


def _fixture(tmp_path, **overrides):
    dataset = b"date,open,high,low,close,volume\n2026-01-01T04:00:00+00:00,100,101,99,100,1000\n2026-01-01T04:05:00+00:00,100,101,99,100,1000\n"
    manifest = {
        "schema_version": "research-fixture-v1",
        "fixture_name": "fixture",
        "data_classification": "SYNTHETIC_NON_SENSITIVE",
        "mode": "RAW_STRATEGY_LAB",
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "policy_version": "raw-strategy-lab-v1",
        "execution_model_version": "simulation-execution-v1",
        "timestamp_convention": "BAR_START_UTC",
        **overrides,
    }
    (tmp_path / "fixture.csv").write_bytes(dataset)
    (tmp_path / "fixture.manifest.json").write_text(json.dumps(manifest))
    return RegressionSuite(str(tmp_path))


def test_replay_fixture_cannot_be_presented_as_a_raw_strategy_policy_run():
    root = Path(__file__).resolve().parents[2]
    suite = RegressionSuite(str(root / "research_data/exit_management/phase10"))
    with pytest.raises(ValueError, match="cannot execute a candidate/replay fixture"):
        suite.generate_regression_baseline("phase10_synthetic_breakout", _NoSignals())
    assert suite.last_manifest is None


def test_actual_execution_version_must_match_the_pinned_fixture(tmp_path):
    suite = _fixture(tmp_path, execution_model_version="another-model")
    with pytest.raises(ValueError, match="execution model"):
        suite.generate_regression_baseline("fixture", _NoSignals())


def test_regression_compares_finite_metrics_and_pins_executed_raw_mode(tmp_path):
    suite = _fixture(tmp_path)
    assert suite.run_regression_test("fixture", _NoSignals(), {"trade_count": 0})
    assert suite.last_manifest["entry_policy"] == "RAW_STRATEGY_LAB_ONLY"
    with pytest.raises(AssertionError, match="finite"):
        suite.run_regression_test(
            "fixture", _NoSignals(), {"trade_count": float("nan")}
        )


@pytest.mark.parametrize("mode", [None, "LIVE", "replay", "anything"])
def test_fixture_requires_known_explicit_execution_mode(tmp_path, mode):
    suite = _fixture(tmp_path, mode=mode)
    with pytest.raises(ValueError, match="explicit supported mode"):
        suite.load_fixture_manifest("fixture")
