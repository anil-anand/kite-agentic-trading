"""Fail-closed research review; this module never grants live dispatch authority.

Hashes bind retained reports to a particular study and predeclared criteria. They
check integrity, not the truth of an operator's research/security attestations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping

from ..time_utils import as_utc

_SECURITY_GATES = ("F26", "F29", "F30")
_REQUIRED_REPORTS = (
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
)
_METRIC_FIELDS = (
    "oos_fold_count",
    "completed_trade_count",
    "unresolved_execution_rate",
    "ambiguous_execution_rate",
    "tail_adverse_r",
    "capture_improvement_r_lower_bound",
    "premature_exit_reduction_lower_bound",
    "delayed_invalidation_increase_upper_bound",
    "cost_stressed_loss_increase_r_upper_bound",
)


def promotion_artifact_hash(artifact: Any) -> str:
    """Hash retained JSON evidence; NaN and non-JSON values are invalid."""

    return hashlib.sha256(
        json.dumps(
            artifact, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def promotion_context_hash(manifest: Mapping[str, Any]) -> str:
    """Bind reports to the full run without a circular holdout-report hash."""

    context = dict(manifest)
    holdout = context.get("untouched_holdout")
    if isinstance(holdout, Mapping):
        context["untouched_holdout"] = {
            key: value
            for key, value in holdout.items()
            if key != "result_artifact_sha256"
        }
    return promotion_artifact_hash(context)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if isfinite(value) else None


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _timestamp(value: Any):
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            return None
        return as_utc(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class PromotionCriteria:
    """Margins must be declared before OOS/holdout access, never fitted to P&L.

    An improvement in capture OR premature exits is required; adverse changes
    in invalidation incidence and stressed losses must remain within margins.
    Uncertainty bounds, rather than point estimates, are compared below.
    """

    minimum_oos_folds: int
    minimum_completed_trades: int
    maximum_unresolved_execution_rate: float
    maximum_ambiguous_execution_rate: float
    maximum_tail_adverse_r: float
    required_security_gates: tuple[str, ...] = _SECURITY_GATES
    require_untouched_holdout: bool = True
    minimum_capture_improvement_r: float | None = None
    minimum_premature_exit_reduction: float | None = None
    maximum_delayed_invalidation_increase: float | None = None
    maximum_cost_stressed_loss_increase_r: float | None = None

    def __post_init__(self) -> None:
        for name in ("minimum_oos_folds", "minimum_completed_trades"):
            if not _positive_integer(getattr(self, name)):
                raise ValueError(f"{name} must be a positive integer")
        if self.require_untouched_holdout is not True:
            raise ValueError("promotion cannot waive the untouched holdout")
        if not isinstance(self.required_security_gates, tuple) or any(
            not isinstance(gate, str) or not gate.strip()
            for gate in self.required_security_gates
        ):
            raise ValueError("security gate identifiers must be nonempty strings")
        if not set(_SECURITY_GATES).issubset(self.required_security_gates):
            raise ValueError("promotion cannot waive F26/F29/F30 security gates")
        rates = {
            "maximum_unresolved_execution_rate",
            "maximum_ambiguous_execution_rate",
            "minimum_premature_exit_reduction",
            "maximum_delayed_invalidation_increase",
        }
        for name in (
            *rates,
            "maximum_tail_adverse_r",
            "minimum_capture_improvement_r",
            "maximum_cost_stressed_loss_increase_r",
        ):
            value = getattr(self, name)
            if value is None and name.startswith(
                ("minimum_", "maximum_delayed", "maximum_cost")
            ):
                continue
            number = _finite_number(value)
            if number is None or number < 0 or (name in rates and number > 1):
                raise ValueError(
                    f"{name} must be finite and within its nonnegative domain"
                )


@dataclass(frozen=True)
class PromotionGateResult:
    passed: bool
    failures: tuple[str, ...]
    evidence_summary: Mapping[str, Any]


def evaluate_promotion_gate(
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    criteria: PromotionCriteria,
) -> PromotionGateResult:
    """Review retained, content-bound attestations without enabling dispatch.

    Evidence carries ``manifest_sha256``, ``criteria_sha256`` and ``reports``.
    Each required report has ``passed``, ``sha256`` and a retained JSON ``artifact``
    containing study/policy/criteria identity, a report reference and observations.
    Bare booleans, synthetic fixtures and arbitrary callback runs are insufficient.
    """

    if not isinstance(manifest, Mapping) or not isinstance(evidence, Mapping):
        raise TypeError("promotion gate requires manifest and evidence mappings")
    failures: list[str] = []
    if manifest.get("schema_version") != "walk-forward-manifest-v1":
        failures.append("INVALID_OR_UNVERSIONED_RESEARCH_MANIFEST")
    if manifest.get("promotion_status") != "RESEARCH_EVIDENCE_ONLY":
        failures.append("RESEARCH_MANIFEST_NOT_FROZEN_FOR_PROMOTION_REVIEW")
    if manifest.get("runner_contract") != "SHARED_CANDIDATE_STACK_VERIFIED":
        failures.append("UNVERIFIED_CANDIDATE_RUNNER")
    if manifest.get("data_classification") not in {
        "CURATED_NON_SENSITIVE",
        "LICENSED_RESEARCH",
    }:
        failures.append("MISSING_OR_INELIGIBLE_RESEARCH_DATA_CLASSIFICATION")
    config = manifest.get("config")
    policy_version = (
        config.get("policy_version") if isinstance(config, Mapping) else None
    )
    if not isinstance(policy_version, str) or not policy_version.strip():
        failures.append("MISSING_POLICY_VERSION")
    for field in ("study_id", "source_commit"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            failures.append(f"MISSING_{field.upper()}")
    for field in ("dataset_hash", "policy_artifact_hash"):
        if not _digest(manifest.get(field)):
            failures.append(f"INVALID_{field.upper()}")
    policies = manifest.get("candidate_policy_artifacts")
    valid_policies = (
        isinstance(policies, Mapping)
        and bool(policies)
        and all(
            isinstance(policy_id, str) and bool(policy_id.strip())
            for policy_id in policies
        )
    )
    if valid_policies:
        try:
            valid_policies = promotion_artifact_hash(dict(policies)) == manifest.get(
                "policy_artifact_hash"
            )
        except (TypeError, ValueError, OverflowError):
            valid_policies = False
    if not valid_policies:
        failures.append("POLICY_ARTIFACTS_MISSING_OR_HASH_MISMATCH")
    criteria_hash = promotion_artifact_hash(asdict(criteria))
    try:
        manifest_hash = promotion_artifact_hash(dict(manifest))
        context_hash = promotion_context_hash(manifest)
    except (TypeError, ValueError, OverflowError):
        manifest_hash = None
        context_hash = None
    if manifest_hash is None or evidence.get("manifest_sha256") != manifest_hash:
        failures.append("EVIDENCE_NOT_BOUND_TO_RESEARCH_MANIFEST")
    if evidence.get("criteria_sha256") != criteria_hash:
        failures.append("EVIDENCE_NOT_BOUND_TO_PREDECLARED_CRITERIA")
    if manifest.get("promotion_criteria_sha256") != criteria_hash:
        failures.append("CRITERIA_NOT_PINNED_IN_RESEARCH_MANIFEST")

    folds = manifest.get("folds")
    valid_folds = isinstance(folds, list) and bool(folds)
    previous_end = None
    for fold in folds if valid_folds else ():
        start = (
            _timestamp(fold.get("test_start")) if isinstance(fold, Mapping) else None
        )
        end = _timestamp(fold.get("test_end")) if isinstance(fold, Mapping) else None
        if (
            start is None
            or end is None
            or end <= start
            or (previous_end is not None and start < previous_end)
        ):
            valid_folds = False
            break
        previous_end = end
    if not valid_folds:
        failures.append("INVALID_OR_OVERLAPPING_OOS_FOLDS")
    declared_at = _timestamp(manifest.get("promotion_criteria_declared_at"))
    research_started_at = _timestamp(manifest.get("research_started_at"))
    policy_frozen_at = _timestamp(manifest.get("policy_frozen_at"))
    if (
        declared_at is None
        or research_started_at is None
        or policy_frozen_at is None
        or declared_at > research_started_at
        or policy_frozen_at > research_started_at
    ):
        failures.append("CRITERIA_NOT_DECLARED_BEFORE_OOS")
    holdout = manifest.get("untouched_holdout")
    holdout_start = (
        _timestamp(holdout.get("start")) if isinstance(holdout, Mapping) else None
    )
    holdout_end = (
        _timestamp(holdout.get("end_exclusive"))
        if isinstance(holdout, Mapping)
        else None
    )
    holdout_evaluated_at = (
        _timestamp(holdout.get("evaluated_at"))
        if isinstance(holdout, Mapping)
        else None
    )
    if (
        not isinstance(holdout, Mapping)
        or holdout.get("status") != "EVALUATED_AND_FROZEN"
        or holdout_start is None
        or holdout_end is None
        or holdout_end <= holdout_start
        or previous_end is None
        or holdout_start < previous_end
        or holdout_evaluated_at is None
        or research_started_at is None
        or holdout_evaluated_at < research_started_at
    ):
        failures.append("UNTOUCHED_HOLDOUT_NOT_EVALUATED_AND_FROZEN")

    reports = evidence.get("reports")
    for gate in (*_REQUIRED_REPORTS, *criteria.required_security_gates):
        report = reports.get(gate) if isinstance(reports, Mapping) else None
        artifact = report.get("artifact") if isinstance(report, Mapping) else None
        valid = (
            isinstance(report, Mapping)
            and report.get("passed") is True
            and isinstance(artifact, Mapping)
        )
        if valid:
            try:
                valid = report.get("sha256") == promotion_artifact_hash(dict(artifact))
            except (TypeError, ValueError, OverflowError):
                valid = False
            valid = valid and all(
                artifact.get(key) == expected
                for key, expected in (
                    ("study_id", manifest.get("study_id")),
                    ("policy_artifact_hash", manifest.get("policy_artifact_hash")),
                    ("source_commit", manifest.get("source_commit")),
                    ("dataset_hash", manifest.get("dataset_hash")),
                    ("research_context_sha256", context_hash),
                    ("criteria_sha256", criteria_hash),
                    ("gate", gate),
                )
            )
            valid = (
                valid
                and context_hash is not None
                and isinstance(artifact.get("report_ref"), str)
                and bool(artifact["report_ref"].strip())
                and isinstance(artifact.get("observations"), Mapping)
                and bool(artifact["observations"])
            )
        if not valid:
            failures.append(
                gate.upper()
                if gate not in criteria.required_security_gates
                else f"SECURITY_GATE_{gate}_NOT_RESOLVED"
            )

    materiality = (
        reports.get("oos_materiality_and_noninferiority_passed")
        if isinstance(reports, Mapping)
        else None
    )
    materiality_artifact = (
        materiality.get("artifact") if isinstance(materiality, Mapping) else None
    )
    observations = (
        materiality_artifact.get("observations")
        if isinstance(materiality_artifact, Mapping)
        else None
    )
    if not isinstance(observations, Mapping) or observations.get("metrics") != {
        name: evidence.get(name) for name in _METRIC_FIELDS
    }:
        failures.append("OOS_METRICS_NOT_BOUND_TO_RETAINED_REPORT")
    holdout_report = (
        reports.get("untouched_holdout_passed")
        if isinstance(reports, Mapping)
        else None
    )
    if (
        not isinstance(holdout, Mapping)
        or not isinstance(holdout_report, Mapping)
        or holdout.get("result_artifact_sha256") != holdout_report.get("sha256")
        or not _digest(holdout.get("result_artifact_sha256"))
    ):
        failures.append("HOLDOUT_RESULT_NOT_BOUND_TO_MANIFEST")

    fold_count = evidence.get("oos_fold_count")
    if (
        not _positive_integer(fold_count)
        or fold_count < criteria.minimum_oos_folds
        or not valid_folds
        or fold_count != len(folds)
    ):
        failures.append("INSUFFICIENT_OR_INCONSISTENT_OOS_FOLDS")
    trade_count = evidence.get("completed_trade_count")
    if (
        not _positive_integer(trade_count)
        or trade_count < criteria.minimum_completed_trades
    ):
        failures.append("INSUFFICIENT_COMPLETED_OOS_TRADES")
    fold_results = manifest.get("fold_results")
    completed_counts = []
    valid_fold_policies = (
        valid_policies and isinstance(fold_results, list) and bool(fold_results)
    )
    if isinstance(fold_results, list):
        for index, result in enumerate(fold_results):
            selected_id = (
                result.get("selected_policy_id")
                if isinstance(result, Mapping)
                else None
            )
            if (
                not valid_policies
                or not isinstance(selected_id, str)
                or selected_id not in policies
            ):
                valid_fold_policies = False
            else:
                try:
                    selected_hash = promotion_artifact_hash(policies[selected_id])
                except (TypeError, ValueError, OverflowError):
                    selected_hash = None
                if (
                    selected_hash is None
                    or result.get("selected_policy_artifact_hash") != selected_hash
                ):
                    valid_fold_policies = False
            metrics = result.get("metrics") if isinstance(result, Mapping) else None
            count = metrics.get("trade_count") if isinstance(metrics, Mapping) else None
            if (
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
                and isinstance(folds, list)
                and index < len(folds)
                and result.get("fold") == folds[index]
            ):
                completed_counts.append(count)
    if not valid_fold_policies:
        failures.append("FOLD_POLICY_NOT_BOUND_TO_RETAINED_ARTIFACTS")
    if (
        not valid_folds
        or not isinstance(fold_results, list)
        or len(fold_results) != len(folds)
        or len(completed_counts) != len(folds)
        or sum(completed_counts) != trade_count
    ):
        failures.append("COMPLETED_TRADE_COUNT_NOT_BOUND_TO_FOLD_RESULTS")
    for name, limit in (
        ("unresolved_execution_rate", criteria.maximum_unresolved_execution_rate),
        ("ambiguous_execution_rate", criteria.maximum_ambiguous_execution_rate),
        ("tail_adverse_r", criteria.maximum_tail_adverse_r),
    ):
        value = _finite_number(evidence.get(name))
        if (
            value is None
            or value < 0
            or value > limit
            or (name.endswith("rate") and value > 1)
        ):
            failures.append(f"{name.upper()}_OUTSIDE_PREDECLARED_LIMIT")

    improvement = False
    for name, minimum in (
        ("capture_improvement_r_lower_bound", criteria.minimum_capture_improvement_r),
        (
            "premature_exit_reduction_lower_bound",
            criteria.minimum_premature_exit_reduction,
        ),
    ):
        value = _finite_number(evidence.get(name))
        if evidence.get(name) is not None and value is None:
            failures.append(f"{name.upper()}_NOT_FINITE")
        if (
            name == "premature_exit_reduction_lower_bound"
            and evidence.get(name) is not None
            and (value is None or not -1 <= value <= 1)
        ):
            failures.append("PREMATURE_EXIT_REDUCTION_OUTSIDE_RATE_DIFFERENCE_DOMAIN")
        if (
            minimum is not None
            and minimum > 0
            and value is not None
            and value >= minimum
            and (name != "premature_exit_reduction_lower_bound" or value <= 1)
        ):
            improvement = True
    if not improvement:
        failures.append("OOS_IMPROVEMENT_NOT_ESTABLISHED_AGAINST_PREDECLARED_MARGIN")
    for name, limit in (
        (
            "delayed_invalidation_increase_upper_bound",
            criteria.maximum_delayed_invalidation_increase,
        ),
        (
            "cost_stressed_loss_increase_r_upper_bound",
            criteria.maximum_cost_stressed_loss_increase_r,
        ),
    ):
        value = _finite_number(evidence.get(name))
        if (
            limit is None
            or value is None
            or value > limit
            or (
                name == "delayed_invalidation_increase_upper_bound"
                and not -1 <= value <= 1
            )
        ):
            failures.append(f"{name.upper()}_OUTSIDE_PREDECLARED_LIMIT")

    return PromotionGateResult(
        passed=not failures,
        failures=tuple(failures),
        evidence_summary={
            "policy_version": policy_version,
            "oos_fold_count": fold_count,
            "completed_trade_count": trade_count,
            "candidate_live_activation": "NOT_PERFORMED_BY_RESEARCH_GATE",
            "verification_scope": "BOUND_REPORT_ATTESTATIONS_NOT_INDEPENDENT_VERIFICATION",
        },
    )
