"""Preregistered session-block inference over retained paired executions.

Outcomes and independent invalidation annotations are research-only inputs. They
are never passed to entry/exit decisions. Missing observations remain unavailable;
neither censored trades nor repeated stress treatments inflate the sample.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any, Mapping, Sequence

import numpy as np

from ..time_utils import EXCHANGE_TIMEZONE
from .promotion import promotion_artifact_hash


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) else None


def _at(value: Any) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("statistics require aware event timestamps")
    return parsed


@dataclass(frozen=True)
class SessionInferencePolicy:
    confidence_level: float = 0.95
    bootstrap_repetitions: int = 2000
    block_length_sessions: int = 5
    minimum_blocks: int = 5
    random_seed: int = 0
    invalidation_grace_seconds: float = 300.0

    def __post_init__(self):
        for name, minimum in (
            ("bootstrap_repetitions", 100),
            ("block_length_sessions", 1),
            ("minimum_blocks", 2),
            ("random_seed", 0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        confidence, grace = (
            _number(self.confidence_level),
            _number(self.invalidation_grace_seconds),
        )
        if confidence is None or not 0.5 < confidence < 1:
            raise ValueError("confidence_level must lie strictly between 0.5 and 1")
        if grace is None or grace < 0:
            raise ValueError(
                "invalidation_grace_seconds must be finite and nonnegative"
            )


def session_block_interval(
    values: Mapping[str, Sequence[float]],
    *,
    sessions: Sequence[str],
    policy: SessionInferencePolicy,
) -> dict:
    """Resample contiguous session blocks, keeping all symbols together.

    The statistic is the paired trade mean, not an equal-weight mean of session
    means. Zero-trade sessions are included in block construction. Bootstrap
    draws containing no observations are reported and excluded; insufficient
    independent blocks or successful draws yield an inconclusive interval.
    """
    days = [date.fromisoformat(day) for day in sessions]
    if days != sorted(set(days)) or set(values) - set(sessions):
        raise ValueError(
            "sessions must be unique chronological dates covering all values"
        )
    observations = []
    for session in sessions:
        row = []
        for value in values.get(session, ()):
            number = _number(value)
            if number is None:
                raise ValueError("bootstrap samples must be finite numbers")
            row.append(number)
        observations.append(row)
    sample = [value for row in observations for value in row]
    block_count = len(sessions) // policy.block_length_sessions
    result = {
        "mean": float(np.mean(sample)) if sample else None,
        "lower_bound": None,
        "upper_bound": None,
        "status": "INCONCLUSIVE",
        "observation_count": len(sample),
        "session_count": len(sessions),
        "nonempty_session_count": sum(bool(row) for row in observations),
        "independent_block_count": block_count,
        "successful_resamples": 0,
        "empty_resamples": 0,
        "method": "CIRCULAR_MOVING_SESSION_BLOCK_PERCENTILE",
        "policy": asdict(policy),
    }
    # Empty calendar periods cannot masquerade as independent outcome evidence.
    active_blocks = sum(
        any(observations[start : start + policy.block_length_sessions])
        for start in range(0, len(observations), policy.block_length_sessions)
    )
    result["nonempty_block_count"] = active_blocks
    if not sample or min(block_count, active_blocks) < policy.minimum_blocks:
        return result
    rng = np.random.default_rng(policy.random_seed)
    means = []
    size = len(observations)
    for _ in range(policy.bootstrap_repetitions):
        drawn = []
        while len(drawn) < size:
            start = int(rng.integers(size))
            drawn.extend(
                (start + offset) % size
                for offset in range(policy.block_length_sessions)
            )
        resample = [value for index in drawn[:size] for value in observations[index]]
        if resample:
            means.append(float(np.mean(resample)))
    result["successful_resamples"] = len(means)
    result["empty_resamples"] = policy.bootstrap_repetitions - len(means)
    if len(means) < 0.95 * policy.bootstrap_repetitions:
        return result
    if np.isclose(min(means), max(means), rtol=1e-12, atol=1e-15):
        # A percentile bootstrap cannot estimate sampling uncertainty when
        # every observed cluster outcome is identical (notably zero rare
        # events). A zero-width interval must not certify noninferiority.
        result["status"] = "INCONCLUSIVE_DEGENERATE_RESAMPLES"
        return result
    alpha = (1 - policy.confidence_level) / 2
    result.update(
        status="ESTIMATED",
        lower_bound=float(np.quantile(means, alpha)),
        upper_bound=float(np.quantile(means, 1 - alpha)),
    )
    return result


def _branch_trade(report: dict, branch: str, symbol: str) -> dict | None:
    trades = [
        trade for trade in report[branch]["trades"] if trade.get("symbol") == symbol
    ]
    if len(trades) > 1:
        raise ValueError(
            "paired cases must identify exactly one entry epoch per symbol"
        )
    return trades[0] if trades else None


def _net_r(trade: dict | None, budget: float) -> float | None:
    net = _number(trade.get("net_pnl")) if trade else None
    return net / budget if net is not None else None


def _invalidation(
    reference: dict | None,
    candidate: dict | None,
    control: dict | None,
    checkpoint: datetime,
    cutoff: datetime,
    grace: float,
) -> tuple[float | None, str]:
    if reference is None:
        return None, "MISSING_INDEPENDENT_REFERENCE"
    if not reference.get("reference_policy_version") or not reference.get(
        "reference_event_id"
    ):
        raise ValueError(
            "invalidation references require independent frozen policy/event identity"
        )
    through = _at(reference["observed_through"])
    if not checkpoint <= through < cutoff:
        raise ValueError("invalidation annotation horizon crosses the scoring cutoff")
    exits = [
        _at(trade["exit_time"]) if trade else None for trade in (candidate, control)
    ]
    if any(at is None or at > through for at in exits):
        return None, "CENSORED_INVALIDATION_OBSERVATION"
    invalidated = reference.get("invalidated_at")
    if invalidated is None:
        if reference.get("assessed_no_invalidation") is not True:
            return None, "NO_EXPLICIT_INVALIDATION_ASSESSMENT"
        return 0.0, "DECLARED_NO_INVALIDATION_DURING_OBSERVED_EXPOSURE"
    invalidated = _at(invalidated)
    if not checkpoint <= invalidated <= through:
        raise ValueError("reference invalidation lies outside observed entry exposure")
    delayed = [float((at - invalidated).total_seconds() > grace) for at in exits]
    return delayed[0] - delayed[1], "DECLARED_REFERENCE_INVALIDATION"


def summarize_paired_stage(
    rows: Sequence[Mapping[str, Any]],
    *,
    sessions: Sequence[str],
    policy: SessionInferencePolicy | Mapping,
    reference_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict:
    """Compute promotion measurements from real fills, without choosing a winner."""
    if isinstance(policy, Mapping):
        policy = SessionInferencePolicy(**dict(policy))
    if not isinstance(policy, SessionInferencePolicy):
        raise TypeError("a frozen session inference policy is required")
    treatments = {}
    for source in rows:
        row = dict(source)
        identity = (
            row["fold_id"],
            row["case_id"],
            row["policy_id"],
            row["scenario_id"],
        )
        if identity in treatments:
            raise ValueError("duplicate treatment cannot inflate evidence")
        treatments[identity] = row
    references = {}
    for item in reference_diagnostics:
        key = (item["case_id"], item["symbol"])
        if key in references:
            raise ValueError("duplicate invalidation reference")
        references[key] = dict(item)
    values = {name: defaultdict(list) for name in ("capture", "delay", "stressed_loss")}
    observations = []
    total = complete = ambiguous = tail_covered = 0
    tails = []
    missing = defaultdict(int)
    expected_stress = sorted(
        {key[3] for key in treatments if key[2] == "candidate" and key[3] != "base"}
    )
    fold_counts = defaultdict(int)
    scored_entries = set()
    cohorts = {
        name: defaultdict(list)
        for name in ("symbol", "regime", "playbook", "direction", "fold")
    }
    for (fold, case_id, policy_id, scenario_id), row in treatments.items():
        if policy_id != "candidate" or scenario_id != "base":
            continue
        report = row["report"]
        case = report["artifacts"]["inputs"]["payload"]["case"]
        checkpoint = _at(case["checkpoint_at"])
        inputs = report["artifacts"]["inputs"]["payload"]
        cutoff = _at(inputs["test_end"])
        scoring_start = _at(inputs.get("test_start", checkpoint))
        if not scoring_start <= checkpoint < cutoff:
            raise ValueError("paired checkpoint must lie inside its scoring window")
        if case.get("case_id", case_id) != case_id:
            raise ValueError("paired case identity differs from its retained artifact")
        session = checkpoint.astimezone(EXCHANGE_TIMEZONE).date().isoformat()
        if session not in sessions:
            raise ValueError(
                "scored case session is not in the declared session calendar"
            )
        for position in case["positions"]:
            thesis = position["thesis"]
            symbol = thesis["symbol"]
            entry_identity = (symbol, checkpoint)
            if entry_identity in scored_entries:
                raise ValueError("duplicate entry epoch cannot inflate paired evidence")
            scored_entries.add(entry_identity)
            if thesis["direction"] not in {"BUY", "SELL"}:
                raise ValueError("paired direction must be BUY or SELL")
            budget = _number(thesis["fill_binding"]["initial_risk_budget"])
            risk = _number(thesis["fill_binding"]["initial_r_per_share"])
            if budget is None or budget <= 0 or risk is None or risk <= 0:
                raise ValueError(
                    "paired results require frozen positive fill-bound risk"
                )
            candidate, control = (
                _branch_trade(report, branch, symbol)
                for branch in ("candidate", "control")
            )
            for trade in (candidate, control):
                if trade is not None and not (
                    scoring_start
                    <= _at(trade["entry_time"])
                    <= checkpoint
                    <= _at(trade["exit_time"])
                    < cutoff
                ):
                    raise ValueError("paired execution lies outside its scoring window")
            candidate_r, control_r = _net_r(candidate, budget), _net_r(control, budget)
            total += 1
            ambiguous += int(
                any(
                    trade and trade.get("ambiguity") is not False
                    for trade in (candidate, control)
                )
            )
            if candidate is not None:
                fold_counts[fold] += 1
            delta = None
            if candidate_r is not None and control_r is not None:
                complete += 1
                delta = candidate_r - control_r
                values["capture"][session].append(delta)
                labels = {
                    "symbol": symbol,
                    "regime": thesis.get("causal_anchors", {}).get("regime"),
                    "playbook": thesis.get("playbook"),
                    "direction": thesis.get("direction"),
                    "fold": fold,
                }
                for name, label in labels.items():
                    cohorts[name][str(label or "UNAVAILABLE")].append(delta)
            if candidate:
                mae = _number(candidate.get("mae"))
                entry = _number(candidate.get("entry_price"))
                if (
                    mae is not None
                    and entry is not None
                    and entry > 0
                    and mae > 0
                    and (mae <= entry if thesis["direction"] == "BUY" else mae >= entry)
                    and candidate.get("excursion_quality") == "COMPLETE_BARS"
                    and candidate.get("ambiguity") is False
                ):
                    sign = 1 if thesis["direction"] == "BUY" else -1
                    tails.append(max(0.0, sign * (entry - mae) / risk))
                    tail_covered += 1
            delayed, coverage = _invalidation(
                references.get((case_id, symbol)),
                candidate,
                control,
                checkpoint,
                cutoff,
                policy.invalidation_grace_seconds,
            )
            if delayed is None:
                missing[coverage] += 1
            else:
                values["delay"][session].append(delayed)
            stress_deltas = []
            for stress in expected_stress:
                stressed = treatments.get((fold, case_id, "candidate", stress))
                if stressed:
                    stress_inputs = stressed["report"]["artifacts"]["inputs"]["payload"]
                    if stress_inputs != inputs:
                        raise ValueError(
                            "execution stress must use the same frozen case and window"
                        )
                    for branch in ("candidate", "control"):
                        trade = _branch_trade(stressed["report"], branch, symbol)
                        if trade is not None and not (
                            scoring_start
                            <= _at(trade["entry_time"])
                            <= checkpoint
                            <= _at(trade["exit_time"])
                            < cutoff
                        ):
                            raise ValueError(
                                "stressed execution lies outside its scoring window"
                            )
                    stressed_r = [
                        _net_r(
                            _branch_trade(stressed["report"], branch, symbol), budget
                        )
                        for branch in ("candidate", "control")
                    ]
                    if all(value is not None for value in stressed_r):
                        stress_deltas.append(
                            max(0.0, -stressed_r[0]) - max(0.0, -stressed_r[1])
                        )
            if expected_stress and len(stress_deltas) == len(expected_stress):
                # Worst declared stress per entry, never one sample per treatment.
                values["stressed_loss"][session].append(max(stress_deltas))
            else:
                missing["MISSING_OR_CENSORED_EXECUTION_STRESS"] += 1
            observations.append(
                {
                    "fold_id": fold,
                    "case_id": case_id,
                    "symbol": symbol,
                    "session": session,
                    "captured_net_r_delta": delta,
                    "delayed_invalidation_increase": delayed,
                    "invalidation_coverage": coverage,
                }
            )
    intervals = {
        name: session_block_interval(items, sessions=sessions, policy=policy)
        for name, items in values.items()
    }
    # Partial observation cannot establish a whole-study noninferiority claim.
    for name in ("capture", "delay", "stressed_loss"):
        if intervals[name]["observation_count"] != total:
            intervals[name].update(
                status="INCONCLUSIVE_INCOMPLETE_COVERAGE",
                lower_bound=None,
                upper_bound=None,
            )
    metrics = {
        "oos_fold_count": len(
            {key[0] for key in treatments if key[2:] == ("candidate", "base")}
        ),
        "completed_trade_count": sum(fold_counts.values()),
        "unresolved_execution_rate": (total - complete) / total if total else None,
        "ambiguous_execution_rate": ambiguous / total if total else None,
        "tail_adverse_r": max(tails) if tail_covered == total and tails else None,
        "capture_improvement_r_lower_bound": intervals["capture"]["lower_bound"],
        "premature_exit_reduction_lower_bound": None,
        "delayed_invalidation_increase_upper_bound": intervals["delay"]["upper_bound"],
        "cost_stressed_loss_increase_r_upper_bound": intervals["stressed_loss"][
            "upper_bound"
        ],
    }
    result = {
        "schema_version": "paired-session-inference-v1",
        "metrics": metrics,
        "intervals": intervals,
        "paired_observations": observations,
        "coverage": {
            "total_pairs": total,
            "complete_pairs": complete,
            "tail_observed": tail_covered,
            "missing": dict(missing),
        },
        "fold_trade_counts": dict(fold_counts),
        "cohorts": {
            name: {
                label: {
                    "count": len(samples),
                    "mean_net_r_delta": float(np.mean(samples)),
                }
                for label, samples in buckets.items()
            }
            for name, buckets in cohorts.items()
        },
        "policy": asdict(policy),
        "sessions": list(sessions),
        "reference_diagnostics_sha256": promotion_artifact_hash(
            list(reference_diagnostics)
        ),
        "source_results_sha256": promotion_artifact_hash(list(rows)),
        "premature_exit_diagnostic": "UNAVAILABLE_WITHOUT_RISK_CONSTRAINED_CONTINUATION_RUNS",
        "tail_basis": "COMPLETE_OBSERVED_BAR_EXPOSURE_ONLY",
    }
    return result
