"""A flat admission stress cannot be reported as management of held exposure."""

from copy import deepcopy

import pytest

from backend.backtesting.acceptance import (
    _executed_research_failures,
    _execution_coverage_summary,
)
from backend.backtesting.promotion import promotion_artifact_hash
from backend.backtesting.retained_report import compact_report


def _measured_fixture(scope="ENTRY_ADMISSION_ONLY"):
    # A deliberately minimal gate fixture: economic correctness is tested by
    # actual paired/portfolio executions elsewhere. This isolates claim binding.
    coverage = {
        "scope": scope,
        "entry_orders_submitted": 4 if scope == "ENTRY_ADMISSION_ONLY" else 0,
        "entry_orders_with_fills": 0,
        "entry_quantity_filled": 0,
        "completed_trades": 0,
    }
    account = {
        "execution_coverage": coverage,
        "status": "COMPLETE",
        "parity": {"mismatches": 0},
        "excluded_entry_checkpoints": [],
    }
    identity = {"fold_id": "fold", "policy_id": "candidate", "scenario_id": "adverse"}
    paired = {
        "control_mode": "LEGACY_REPAIRED",
        "artifacts": {
            "inputs": {
                "payload": {
                    "case": {
                        "positions": [
                            {"thesis": {"fill_binding": {"filled_quantity": 1}}}
                        ]
                    }
                }
            }
        },
    }
    result = {
        "statistics": {
            "metrics": {},
            "intervals": {
                name: {"status": "ESTIMATED"}
                for name in ("capture", "delay", "stressed_loss")
            },
        },
        "results": [{**identity, "report": paired}],
        "portfolio_results": [
            {
                **identity,
                "report": {
                    "production_admission_parity": True,
                    "candidate": deepcopy(account),
                    "control": deepcopy(account),
                },
            }
        ],
    }
    manifest = {
        "acceptance_statistics_hashes": {
            "oos": promotion_artifact_hash(result["statistics"])
        }
    }
    evidence = {
        "reports": {
            "execution_and_data_stress_passed": {
                "artifact": {
                    "observations": {
                        "execution_coverage": {
                            "oos": deepcopy(_execution_coverage_summary(result))
                        }
                    }
                }
            }
        }
    }
    return result, manifest, evidence


@pytest.mark.parametrize("scope", ["ENTRY_ADMISSION_ONLY", "NO_ENTRY_ORDERS"])
def test_truthful_zero_exposure_portfolio_scope_can_accompany_paired_exit_stress(scope):
    result, manifest, evidence = _measured_fixture(scope)
    summary = _execution_coverage_summary(result)
    assert summary["paired_stress_treatments_with_entry_exposure"] == 1
    assert summary["portfolio_treatments"][0]["candidate"]["scope"] == scope
    assert _executed_research_failures("oos", result, manifest, evidence, {}) == []
    compact = compact_report(result["portfolio_results"][0]["report"])
    assert compact["candidate"]["execution_coverage"]["scope"] == scope


def test_cancellation_only_portfolio_cannot_be_claimed_as_held_exposure_stress():
    result, manifest, evidence = _measured_fixture()
    declared = evidence["reports"]["execution_and_data_stress_passed"]["artifact"][
        "observations"
    ]["execution_coverage"]["oos"]
    declared["portfolio_treatments"][0]["candidate"].update(
        scope="HELD_EXPOSURE", entry_orders_with_fills=4, entry_quantity_filled=40
    )
    assert _executed_research_failures("oos", result, manifest, evidence, {}) == [
        "OOS_EXECUTION_STRESS_COVERAGE_NOT_BOUND_TO_RUNS"
    ]


@pytest.mark.parametrize(
    "reports", [None, {}, {"execution_and_data_stress_passed": None}]
)
def test_unscoped_stress_attestation_cannot_substitute_for_measured_coverage(reports):
    result, manifest, evidence = _measured_fixture()
    evidence["reports"] = reports
    assert _executed_research_failures("oos", result, manifest, evidence, {}) == [
        "OOS_EXECUTION_STRESS_COVERAGE_NOT_BOUND_TO_RUNS"
    ]
