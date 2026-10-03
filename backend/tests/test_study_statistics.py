"""Session dependence, censoring and diagnostic provenance in study inference."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from backend.backtesting.study_statistics import (
    SessionInferencePolicy,
    session_block_interval,
    summarize_paired_stage,
)

POLICY = SessionInferencePolicy(
    block_length_sessions=1, minimum_blocks=2, bootstrap_repetitions=200
)


def sample(days=4):
    rows, references, sessions = [], [], []
    for day in range(days):
        start = datetime(2026, 9, 21 + day, 4, 20, tzinfo=timezone.utc)
        sessions.append(start.date().isoformat())
        thesis = {
            "symbol": "ABC",
            "direction": "BUY",
            "playbook": "Breakout",
            "causal_anchors": {"regime": "TRENDING"},
            "fill_binding": {"initial_risk_budget": 50, "initial_r_per_share": 5},
        }
        for scenario, candidate_net, control_net in (
            ("base", 20, 10),
            ("adverse", -15, -10),
        ):
            trades = {
                name: [
                    {
                        "symbol": "ABC",
                        "entry_time": start.isoformat(),
                        "exit_time": (start + timedelta(minutes=15)).isoformat(),
                        "entry_price": 100,
                        "mae": 98,
                        "net_pnl": net,
                        "excursion_quality": "COMPLETE_BARS",
                        "ambiguity": False,
                    }
                ]
                for name, net in (
                    ("candidate", candidate_net),
                    ("control", control_net),
                )
            }
            report = {
                "artifacts": {
                    "inputs": {
                        "payload": {
                            "case": {
                                "checkpoint_at": start.isoformat(),
                                "positions": [{"thesis": thesis}],
                            },
                            "test_end": (start + timedelta(hours=12)).isoformat(),
                        }
                    }
                },
                **{name: {"trades": items} for name, items in trades.items()},
            }
            rows.append(
                {
                    "fold_id": f"fold-{day}",
                    "case_id": f"case-{day}",
                    "policy_id": "candidate",
                    "scenario_id": scenario,
                    "report": report,
                }
            )
        references.append(
            {
                "case_id": f"case-{day}",
                "symbol": "ABC",
                "reference_policy_version": "frozen-structure-v1",
                "reference_event_id": f"observation-{day}",
                "invalidated_at": None,
                "assessed_no_invalidation": True,
                "observed_through": (start + timedelta(hours=1)).isoformat(),
            }
        )
    return rows, references, sessions


def test_intervals_resample_sessions_together_and_keep_zero_trade_sessions():
    days = [f"2026-09-{day:02}" for day in range(21, 27)]
    values = {days[0]: [0.4] * 50, days[2]: [-0.4] * 50, days[4]: [0.2] * 50}
    result = session_block_interval(values, sessions=days, policy=POLICY)
    assert result["session_count"] == 6
    assert result["nonempty_session_count"] == 3
    assert result["observation_count"] == 150
    # Fifty correlated names on one day are not fifty independent observations.
    assert result["lower_bound"] < 0 < result["upper_bound"]
    assert result == session_block_interval(values, sessions=days, policy=POLICY)


def test_one_busy_session_cannot_meet_minimum_independent_evidence():
    result = session_block_interval(
        {"2026-09-21": [1] * 1000}, sessions=["2026-09-21", "2026-09-22"], policy=POLICY
    )
    assert result["status"] == "INCONCLUSIVE"
    assert result["lower_bound"] is None


def test_actual_net_r_and_worst_stress_are_measured_once_per_entry():
    rows, refs, sessions = sample()
    variant = deepcopy(rows[0])
    variant["policy_id"] = "perturbation"
    rows.append(variant)
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    metrics = result["metrics"]
    assert metrics["completed_trade_count"] == 4
    assert result["coverage"]["total_pairs"] == 4
    assert result["intervals"]["capture"]["mean"] == pytest.approx(0.2)
    assert result["intervals"]["stressed_loss"]["mean"] == pytest.approx(0.1)
    assert result["intervals"]["delay"]["mean"] == 0
    assert metrics["capture_improvement_r_lower_bound"] is None
    assert metrics["cost_stressed_loss_increase_r_upper_bound"] is None
    assert metrics["delayed_invalidation_increase_upper_bound"] is None
    assert metrics["tail_adverse_r"] == pytest.approx(0.4)
    assert metrics["premature_exit_reduction_lower_bound"] is None
    assert result["cohorts"]["symbol"]["ABC"]["count"] == 4


def test_missing_invalidation_reference_never_means_no_delay():
    rows, _, sessions = sample()
    result = summarize_paired_stage(rows, sessions=sessions, policy=POLICY)
    assert result["metrics"]["delayed_invalidation_increase_upper_bound"] is None
    assert result["coverage"]["missing"]["MISSING_INDEPENDENT_REFERENCE"] == 4


def test_delay_measures_exposure_after_reference_including_execution_delay():
    rows, refs, sessions = sample()
    for ref in refs:
        ref["invalidated_at"] = ref["observed_through"].replace("05:20", "04:25")
        ref.pop("assessed_no_invalidation")
    for row in rows:
        row["report"]["control"]["trades"][0]["exit_time"] = row["report"]["control"][
            "trades"
        ][0]["exit_time"].replace("04:35", "04:25")
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    assert result["intervals"]["delay"]["mean"] == 1
    assert result["metrics"]["delayed_invalidation_increase_upper_bound"] is None


@pytest.mark.parametrize("branch", ["candidate", "control"])
def test_censored_worst_outcomes_cannot_disappear_from_improvement_bounds(branch):
    rows, refs, sessions = sample()
    rows[0]["report"][branch]["trades"] = []
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    assert result["coverage"]["complete_pairs"] == 3
    assert result["metrics"]["unresolved_execution_rate"] == 0.25
    assert result["metrics"]["capture_improvement_r_lower_bound"] is None
    assert result["metrics"]["delayed_invalidation_increase_upper_bound"] is None


def test_ambiguous_extrema_do_not_become_precise_tail_estimates():
    rows, refs, sessions = sample()
    rows[0]["report"]["candidate"]["trades"][0].update(
        ambiguity=True, excursion_quality="PARTIAL_FILL_BAR_BOUNDED"
    )
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    assert result["metrics"]["tail_adverse_r"] is None
    assert result["metrics"]["ambiguous_execution_rate"] == 0.25


def test_duplicate_treatments_and_future_labels_are_rejected():
    rows, refs, sessions = sample()
    with pytest.raises(ValueError, match="duplicate treatment"):
        summarize_paired_stage(rows + [rows[0]], sessions=sessions, policy=POLICY)
    refs[0]["observed_through"] = "2026-09-22T04:20:00+00:00"
    with pytest.raises(ValueError, match="cutoff"):
        summarize_paired_stage(
            rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence_level", True),
        ("minimum_blocks", 1),
        ("bootstrap_repetitions", False),
        ("block_length_sessions", 0),
        ("invalidation_grace_seconds", float("nan")),
    ],
)
def test_invalid_inference_policy_is_rejected_before_research(field, value):
    with pytest.raises(ValueError):
        SessionInferencePolicy(**{field: value})


def test_zero_width_resamples_do_not_certify_rare_event_noninferiority():
    days = [f"2026-09-{day:02}" for day in range(21, 27)]
    result = session_block_interval(
        {day: [0.0] * 100 for day in days}, sessions=days, policy=POLICY
    )
    assert result["mean"] == 0
    assert result["status"] == "INCONCLUSIVE_DEGENERATE_RESAMPLES"
    assert result["upper_bound"] is None


@pytest.mark.parametrize("branch", ["candidate", "control"])
@pytest.mark.parametrize("field", ["entry_time", "exit_time"])
def test_execution_timestamps_outside_scoring_cannot_enter_statistics(branch, field):
    rows, refs, sessions = sample()
    rows[0]["report"][branch]["trades"][0][field] = "2026-09-22T00:00:00+00:00"
    with pytest.raises(ValueError, match="scoring window"):
        summarize_paired_stage(
            rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
        )


def test_renaming_fold_or_case_cannot_recount_the_same_entry_epoch():
    rows, refs, sessions = sample()
    duplicate = deepcopy(rows[0])
    duplicate.update(fold_id="new-fold", case_id="renamed-case")
    with pytest.raises(ValueError, match="duplicate entry epoch"):
        summarize_paired_stage(rows + [duplicate], sessions=sessions, policy=POLICY)


def test_stress_must_keep_the_same_entry_premise_and_scoring_window():
    rows, refs, sessions = sample()
    rows[1]["report"]["artifacts"]["inputs"]["payload"]["test_end"] = (
        "2026-09-23T00:00:00+00:00"
    )
    with pytest.raises(ValueError, match="same frozen case"):
        summarize_paired_stage(
            rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
        )


@pytest.mark.parametrize(
    "mutation",
    [{"ambiguity": True}, {"ambiguity": None}, {"mae": 101}, {"entry_price": -100}],
)
def test_contradictory_or_unknown_excursion_evidence_cannot_establish_tail_safety(
    mutation,
):
    rows, refs, sessions = sample()
    rows[0]["report"]["candidate"]["trades"][0].update(mutation)
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    assert result["metrics"]["tail_adverse_r"] is None


def test_unknown_control_execution_ambiguity_is_not_an_unambiguous_pair():
    rows, refs, sessions = sample()
    rows[0]["report"]["control"]["trades"][0].pop("ambiguity")
    result = summarize_paired_stage(
        rows, sessions=sessions, policy=POLICY, reference_diagnostics=refs
    )
    assert result["metrics"]["ambiguous_execution_rate"] == 0.25
