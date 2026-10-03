"""Reproducible, single-access phase-10 research workflow.

This command runs retained entry cases through the shared execution stack. It
never opens the live journal, reads credentials, fetches data or submits orders.
Registration freezes inputs before research. Failed attempts remain recorded;
revising a study requires a new registration and disclosure of prior trials.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import multiprocessing
import re
import subprocess
import sys
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..market_context import ContextPolicy
from ..replay import serialize_replay_artifact
from .operational_evidence import (
    OperationalLimits,
    assess_operational_cohort,
    assess_operational_run,
)
from .promotion import (
    PromotionCriteria,
    evaluate_promotion_gate,
    promotion_artifact_hash,
)
from .reference_diagnostics import (
    EntryBoundaryReferencePolicy,
    derive_entry_boundary_references,
)
from .research_study import restore_exit_policy, restore_session_policy
from .retained_report import retain_report, validate_retained_report
from .study_statistics import SessionInferencePolicy, summarize_paired_stage

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "exit-acceptance-registration-v1"
_OPERATIONAL_MODES = {"LIVE_SHADOW", "ISOLATED_PAPER"}


def _operational_cohorts(value: Any, limits: dict) -> dict:
    """Freeze capture slots before any observation can be selected for import."""
    if not isinstance(value, dict) or set(value) - _OPERATIONAL_MODES:
        raise ValueError("operational_cohorts requires declared operational modes")
    for cohort in value.values():
        if not isinstance(cohort, dict) or set(cohort) != {"required_slots"}:
            raise ValueError("operational cohort requires only required_slots")
        slots = cohort["required_slots"]
        if not isinstance(slots, list) or not slots:
            raise ValueError("operational cohort requires nonempty capture slots")
        identifiers, sessions = set(), set()
        for slot in slots:
            if not isinstance(slot, dict) or set(slot) != {"slot_id", "session_dates"}:
                raise ValueError("capture slot requires slot_id and session_dates")
            identifier = slot["slot_id"]
            if (
                not isinstance(identifier, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identifier) is None
                or identifier in identifiers
            ):
                raise ValueError("capture slots require unique safe identifiers")
            identifiers.add(identifier)
            dates = slot["session_dates"]
            if not isinstance(dates, list) or not dates:
                raise ValueError("capture slot requires explicit session dates")
            for item in dates:
                if (
                    not isinstance(item, str)
                    or date.fromisoformat(item).isoformat() != item
                ):
                    raise ValueError("capture slot requires ISO session dates")
            if len(set(dates)) != len(dates):
                raise ValueError("capture slot cannot repeat session dates")
            sessions.update(dates)
        if len(sessions) < limits["minimum_sessions"]:
            raise ValueError(
                "operational cohort cannot cover the declared session minimum"
            )
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            serialize_replay_artifact(value), sort_keys=True, indent=2, allow_nan=False
        ).encode()
        + b"\n"
    )


def _read(path: Path) -> Any:
    def invalid_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")

    return json.loads(path.read_bytes(), parse_constant=invalid_constant)


def _write_once(path: Path, value: Any) -> None:
    payload = _json_bytes(value)
    with path.open("xb") as handle:
        handle.write(payload)


def _stamp(value: Any) -> pd.Timestamp:
    if not isinstance(value, (str, datetime)):
        raise ValueError("research timestamps require an explicit offset")
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("research timestamps must be finite and timezone-aware")
    return timestamp.tz_convert("UTC")


def _source_identity() -> dict:
    """Include uncommitted source changes without reading runtime/account files."""
    paths = sorted(
        [
            *ROOT.glob("backend/**/*.py"),
            *ROOT.glob("src/**/*.ts"),
            *ROOT.glob("src/**/*.tsx"),
            ROOT / "run_backend.py",
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "package.json",
            ROOT / "package-lock.json",
            ROOT / "vite.config.ts",
            ROOT / "tsconfig.main.json",
            *ROOT.glob("scripts/*.mjs"),
            *ROOT.glob("src/**/*.html"),
        ]
    )
    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {
        "base_commit": commit,
        "source_tree_sha256": promotion_artifact_hash(hashes),
        "source_files": hashes,
    }


def _input_path(base: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("input file path is required")
    path = (base / value).resolve()
    if path.is_relative_to((Path.home() / ".kite-agentic-trading").resolve()):
        raise ValueError("use an explicit non-sensitive export outside live storage")
    if not path.is_file():
        raise ValueError(f"research input file is missing: {path.name}")
    return path


def _policy(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("policy must be a retained ExitPolicy object")
    return serialize_replay_artifact(restore_exit_policy(value))


def _criteria(value: Mapping) -> PromotionCriteria:
    values = dict(value)
    if "required_security_gates" in values:
        values["required_security_gates"] = tuple(values["required_security_gates"])
    return PromotionCriteria(**values)


def _windows(plan: dict) -> None:
    folds = plan.get("oos_folds")
    holdout = plan.get("holdout")
    if not isinstance(folds, list) or not folds or not isinstance(holdout, dict):
        raise ValueError("explicit OOS folds and untouched holdout are required")
    previous_end = None
    identifiers = set()
    for fold in [*folds, holdout]:
        if not isinstance(fold, dict) or set(fold) != {
            "fold_id",
            "warmup_start",
            "test_start",
            "test_end",
        }:
            raise ValueError("fold requires id, warmup_start, test_start and test_end")
        identifier = fold["fold_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
        ):
            raise ValueError("fold identifiers must be nonempty and unique")
        identifiers.add(identifier)
        warmup, start, end = (
            _stamp(fold[key]) for key in ("warmup_start", "test_start", "test_end")
        )
        if (
            warmup > start
            or start >= end
            or (previous_end is not None and start < previous_end)
        ):
            raise ValueError(
                "folds/holdout must be chronological disjoint half-open windows"
            )
        for key, at in (
            ("warmup_start", warmup),
            ("test_start", start),
            ("test_end", end),
        ):
            fold[key] = at.isoformat()
        previous_end = end


def register_study(plan_path: str | Path, directory: str | Path) -> dict:
    """Freeze approved input bytes, policies and limits before reading outcomes."""
    plan_path, directory = Path(plan_path).resolve(), Path(directory).resolve()
    plan = _read(plan_path)
    allowed = {
        "study_id",
        "data_source",
        "data_classification",
        "datasets",
        "cases",
        "oos_folds",
        "holdout",
        "candidate_policy",
        "control_policy",
        "control_description",
        "variants",
        "execution_scenarios",
        "criteria",
        "prior_trial_count",
        "operational_limits",
        "operational_cohorts",
        "context_policy",
        "session_policy",
        "inference_policy",
        "reference_diagnostics",
        "portfolio",
        "control_kind",
        "legacy_control_policy",
        "entry_case_source",
        "reference_policy",
    }
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ValueError("unknown study plan fields")
    for field in ("study_id", "data_source", "control_description"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"study plan requires {field}")
    if plan.get("data_classification") not in {
        "SYNTHETIC_NON_SENSITIVE",
        "CURATED_NON_SENSITIVE",
        "LICENSED_RESEARCH",
    }:
        raise ValueError("explicit dataset classification is required")
    _windows(plan)
    plan["candidate_policy"] = _policy(plan["candidate_policy"])
    plan["control_policy"] = _policy(plan["control_policy"])
    plan["control_kind"] = plan.get("control_kind", "SHARED_POLICY")
    if plan["control_kind"] not in {"SHARED_POLICY", "LEGACY_REPAIRED"}:
        raise ValueError(
            "control_kind must identify shared-policy or repaired-legacy control"
        )
    plan["inference_policy"] = asdict(
        SessionInferencePolicy(**plan.get("inference_policy", {}))
    )
    diagnostics = plan.get("reference_diagnostics", [])
    if isinstance(diagnostics, str):
        diagnostics = _read(_input_path(plan_path.parent, diagnostics))
    if not isinstance(diagnostics, list) or any(
        not isinstance(item, dict) for item in diagnostics
    ):
        raise ValueError(
            "reference_diagnostics must retain independent annotated events"
        )
    plan["reference_diagnostics"] = diagnostics
    if plan.get("reference_policy") is not None:
        if diagnostics:
            raise ValueError(
                "choose a frozen mechanical reference or retained annotations, not both"
            )
        plan["reference_policy"] = asdict(
            EntryBoundaryReferencePolicy(**plan["reference_policy"])
        )
    portfolio = plan.get("portfolio")
    if portfolio is not None:
        if not isinstance(portfolio, dict) or set(portfolio) - {
            "universe_events",
            "instrument_metadata",
            "strategy_config",
            "risk_config",
            "initial_capital",
            "calibration_history",
        }:
            raise ValueError(
                "portfolio requires frozen point-in-time universe and production configurations"
            )
        universe = portfolio.get("universe_events")
        if isinstance(universe, str):
            universe = _read(_input_path(plan_path.parent, universe))
        if not isinstance(universe, list) or not universe:
            raise ValueError("portfolio requires point-in-time universe events")
        for event in universe:
            _stamp(event["at"])
            if "available_at" in event:
                _stamp(event["available_at"])
            if not isinstance(event.get("symbols"), list) or any(
                not isinstance(symbol, str) or not symbol for symbol in event["symbols"]
            ):
                raise ValueError("universe events require explicit symbol membership")
        for key in ("instrument_metadata", "strategy_config", "risk_config"):
            if not isinstance(portfolio.get(key), dict):
                raise ValueError(f"portfolio requires frozen {key}")
        portfolio["universe_events"] = universe
        calibration = portfolio.get("calibration_history", [])
        if isinstance(calibration, str):
            calibration = _read(_input_path(plan_path.parent, calibration))
        if not isinstance(calibration, list):
            raise ValueError(
                "calibration history must be an explicit retained outcome list"
            )
        portfolio["calibration_history"] = calibration
    plan["entry_case_source"] = plan.get("entry_case_source", "RETAINED_CHECKPOINTS")
    if plan["entry_case_source"] not in {
        "RETAINED_CHECKPOINTS",
        "GENERATED_CONTROL_ENTRIES",
    }:
        raise ValueError("unknown entry-case provenance")
    if portfolio is None and (
        plan["control_kind"] == "LEGACY_REPAIRED"
        or plan["entry_case_source"] == "GENERATED_CONTROL_ENTRIES"
    ):
        raise ValueError(
            "legacy/generated cases require frozen production portfolio configurations"
        )
    criteria = _criteria(plan["criteria"])
    if not any(
        value is not None and value > 0
        for value in (
            criteria.minimum_capture_improvement_r,
            criteria.minimum_premature_exit_reduction,
        )
    ) or any(
        value is None
        for value in (
            criteria.maximum_delayed_invalidation_increase,
            criteria.maximum_cost_stressed_loss_increase_r,
        )
    ):
        raise ValueError(
            "declare positive improvement and explicit noninferiority margins before research"
        )
    plan["criteria"] = serialize_replay_artifact(asdict(criteria))
    if len(plan["oos_folds"]) < plan["criteria"]["minimum_oos_folds"]:
        raise ValueError("registered OOS folds cannot satisfy the declared minimum")
    plan["operational_limits"] = asdict(OperationalLimits(**plan["operational_limits"]))
    if "operational_cohorts" in plan:
        plan["operational_cohorts"] = _operational_cohorts(
            plan["operational_cohorts"], plan["operational_limits"]
        )
    prior = plan.get("prior_trial_count", 0)
    if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
        raise ValueError("prior_trial_count must be a nonnegative integer")
    plan["prior_trial_count"] = prior
    variants = plan.get("variants", {})
    if not isinstance(variants, dict) or len(variants) > 7 or "candidate" in variants:
        raise ValueError("at most seven additional preregistered variants are allowed")
    if not all(isinstance(name, str) and name.strip() for name in variants):
        raise ValueError("variant ids must be nonempty strings")
    plan["variants"] = {name: _policy(policy) for name, policy in variants.items()}
    scenarios = plan.get("execution_scenarios")
    if not isinstance(scenarios, dict) or "base" not in scenarios or len(scenarios) > 8:
        raise ValueError("execution_scenarios requires base and at most eight cases")
    if (len(variants) + 1) * len(scenarios) > 24:
        raise ValueError(
            "preregistered policy/execution budget cannot exceed 24 treatments"
        )
    for name, scenario in scenarios.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(scenario, dict)
            or set(scenario) - {"execution_policy", "cost_policy"}
        ):
            raise ValueError(
                "scenario must declare only execution_policy and cost_policy"
            )
        from .simulated_broker import SimulationExecutionPolicy

        scenario["execution_policy"] = asdict(
            SimulationExecutionPolicy(**scenario.get("execution_policy", {}))
        )
    plan["context_policy"] = asdict(ContextPolicy(**plan.get("context_policy", {})))
    plan["session_policy"] = serialize_replay_artifact(
        restore_session_policy(plan.get("session_policy"))
    )
    datasets = plan.get("datasets")
    if (
        not isinstance(datasets, dict)
        or not datasets
        or not all(isinstance(name, str) and name for name in datasets)
    ):
        raise ValueError("datasets must map symbols to approved CSV paths")
    input_bytes = {}
    files = {}
    for index, (symbol, relative_path) in enumerate(sorted(datasets.items())):
        data = _input_path(plan_path.parent, relative_path).read_bytes()
        name = f"dataset-{index:04}.csv"
        input_bytes[name] = data
        files[symbol] = {"file": name, "sha256": hashlib.sha256(data).hexdigest()}
    cases = plan.get("cases")
    if not isinstance(cases, list) or (
        not cases and plan["entry_case_source"] == "RETAINED_CHECKPOINTS"
    ):
        raise ValueError("retained entry case files are required")
    if cases and plan["entry_case_source"] == "GENERATED_CONTROL_ENTRIES":
        raise ValueError(
            "generated-control studies cannot mix manually selected entry cases"
        )
    retained_cases, case_ids, retained_positions = [], set(), set()
    for path in cases:
        case = _read(_input_path(plan_path.parent, path))
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("case_id"), str)
            or not case["case_id"]
            or case["case_id"] in case_ids
        ):
            raise ValueError("retained cases require unique case_id values")
        checkpoint = _stamp(case.get("checkpoint_at"))
        if not any(
            _stamp(fold["test_start"]) <= checkpoint < _stamp(fold["test_end"])
            for fold in [*plan["oos_folds"], plan["holdout"]]
        ):
            raise ValueError(
                "every retained case must belong to an OOS or holdout window"
            )
        if not isinstance(case.get("positions"), list) or not case["positions"]:
            raise ValueError("retained cases require terminal entry positions")
        for position in case["positions"]:
            thesis = position["thesis"]
            identity = (
                thesis["position_key"],
                _stamp(thesis["fill_binding"]["entry_terminal_at"]).isoformat(),
            )
            if identity in retained_positions:
                raise ValueError(
                    "the same retained entry cannot count as multiple cases"
                )
            retained_positions.add(identity)
        case_ids.add(case["case_id"])
        retained_cases.append(case)
    plan["datasets"], plan["cases"] = files, retained_cases
    registration = {
        "schema_version": SCHEMA,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "source": _source_identity(),
        "plan": plan,
        "scope": "PAIRED_AND_POINT_IN_TIME_PORTFOLIO"
        if portfolio is not None
        else "INDEPENDENT_FIXED_ENTRY_PAIRED_ACCOUNTS",
        "live_activation": "DISABLED",
    }
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "inputs").mkdir()
    for name, data in input_bytes.items():
        (directory / "inputs" / name).write_bytes(data)
    _write_once(directory / "registration.json", registration)
    _write_once(
        directory / "registration.hash.json",
        {"sha256": promotion_artifact_hash(registration)},
    )
    return registration


def _stage_result(directory: Path, registration: dict, stage: str) -> dict:
    result = _read(directory / f"{stage}.result.json")
    claim = _read(directory / f"{stage}.access.json")
    if (
        promotion_artifact_hash(result)
        != _read(directory / f"{stage}.result.hash.json")["sha256"]
        or result.get("registration_sha256") != promotion_artifact_hash(registration)
        or result.get("stage") != stage
        or any(result.get(key) != value for key, value in claim.items())
        or set(claim) != {"registration_sha256", "stage", "started_at"}
        or result.get("status") != "COMPLETED"
    ):
        raise ValueError("retained stage results changed or belong to another study")
    for row in [*result["results"], *result["portfolio_results"]]:
        validate_retained_report(directory, row["retained_report"], row["report"])
    return result


def _stage_frames(directory: Path, plan: dict, folds: list[dict]) -> dict:
    """Select by event time before interpreting any held-out feature values.

    Date is routing metadata. OHLCV/receipt values outside this stage are never
    parsed or used to infer types, so poisoned holdout features cannot alter OOS.
    Raw bytes remain frozen and hashed independently of research execution.
    """
    frames = {}
    windows = [
        (_stamp(fold["warmup_start"]), _stamp(fold["test_end"])) for fold in folds
    ]
    for symbol, dataset in plan["datasets"].items():
        with (directory / "inputs" / dataset["file"]).open(newline="") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames
            if (
                not columns
                or "date" not in columns
                or len(columns) != len(set(columns))
            ):
                raise ValueError("historical CSV requires unique columns and date")
            rows = []
            for row in reader:
                event_at = _stamp(row["date"])
                if any(start <= event_at < end for start, end in windows):
                    row["date"] = event_at
                    rows.append(row)
        frame = pd.DataFrame(rows, columns=columns)
        if frame.empty:
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
        for column in ("available_at", "received_at"):
            if column in frame:
                frame[column] = frame[column].map(_stamp)
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="raise")
        frames[symbol] = frame
    return frames


def _registration(directory: Path) -> dict:
    registration = _read(directory / "registration.json")
    expected = _read(directory / "registration.hash.json")["sha256"]
    if (
        registration.get("schema_version") != SCHEMA
        or promotion_artifact_hash(registration) != expected
    ):
        raise ValueError("registered research plan was modified")
    if registration["source"] != _source_identity():
        raise ValueError(
            "source changed after registration; disclose prior trials in a new study"
        )
    for dataset in registration["plan"]["datasets"].values():
        data = (directory / "inputs" / dataset["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != dataset["sha256"]:
            raise ValueError("registered dataset bytes changed")
    return registration


def _execute_treatment(context: dict, job: dict) -> dict:
    """Retain the complete report in its executing process, returning only its index."""
    from .portfolio_study import run_portfolio_study
    from .research_study import run_paired_case

    directory, registration, frames = (
        context[key] for key in ("directory", "registration", "frames")
    )
    plan, fold = registration["plan"], job["fold"]
    start, end, warmup = (
        _stamp(fold[key]) for key in ("test_start", "test_end", "warmup_start")
    )
    scenario = plan["execution_scenarios"][job["scenario_id"]]
    policy = (
        plan["candidate_policy"]
        if job["policy_id"] == "candidate"
        else plan["variants"][job["policy_id"]]
    )
    common = {
        "candidate_policy": policy,
        "test_start": start,
        "test_end": end,
        "warmup_start": warmup,
        "execution_policy": scenario["execution_policy"],
        "cost_policy": scenario.get("cost_policy"),
        "context_policy": plan["context_policy"],
        "session_policy": plan["session_policy"],
        "source_revision": registration["source"]["source_tree_sha256"],
    }
    row = {key: job[key] for key in ("policy_id", "scenario_id")}
    row["fold_id"] = fold["fold_id"]
    available = {}
    if job["kind"] == "portfolio":
        symbols, cutoff = sorted(frames), end
    else:
        case = job["case"]
        session = restore_session_policy(plan["session_policy"])
        checkpoint = _stamp(case["checkpoint_at"])
        session_end = checkpoint.tz_convert(
            session.exchange_timezone
        ).normalize() + timedelta(days=1)
        cutoff = min(end, session_end.tz_convert("UTC"))
        symbols = sorted({item["thesis"]["symbol"] for item in case["positions"]})
        if set(symbols) - frames.keys():
            raise ValueError("case symbol has no registered historical dataset")
        row["case_id"] = case["case_id"]
    for symbol in symbols:
        frame = frames[symbol]
        mask = (frame["date"] >= warmup) & (frame["date"] < cutoff)
        for column in ("available_at", "received_at"):
            if column in frame:
                mask &= frame[column] < cutoff
        available[symbol] = frame.loc[mask].copy(deep=True)
    if job["kind"] == "portfolio":
        config = plan["portfolio"]
        universe = [
            event
            for event in config["universe_events"]
            if max(
                _stamp(event["at"]),
                _stamp(event.get("available_at", event["at"])),
            )
            < end
        ]
        report = run_portfolio_study(
            market_data=available,
            universe_events=universe,
            instrument_metadata=config["instrument_metadata"],
            strategy_config=config["strategy_config"],
            risk_config=config["risk_config"],
            initial_capital=config.get("initial_capital", 100000),
            calibration_history=config["calibration_history"],
            legacy_policy=plan.get("legacy_control_policy"),
            **common,
        )
    else:
        comparator = {}
        if plan["control_kind"] == "LEGACY_REPAIRED":
            comparator = {
                "control_mode": "LEGACY_REPAIRED",
                "legacy_policy": plan.get("legacy_control_policy"),
                "strategy_config": plan["portfolio"]["strategy_config"],
                "risk_config": plan["portfolio"]["risk_config"],
            }
        report = run_paired_case(
            case=case,
            market_data=available,
            control_policy=plan["control_policy"],
            **common,
            **comparator,
        )
    references = []
    if (
        job["kind"] == "paired"
        and plan.get("reference_policy") is not None
        and job["policy_id"] == "candidate"
        and job["scenario_id"] == "base"
    ):
        references = derive_entry_boundary_references(report, plan["reference_policy"])
    row.update(retain_report(directory, job["retained_name"], report))
    return {"row": row, "references": references}


_WORKER_CONTEXT = None
_WORKER_INITIALIZATION_FAILED = False


def _initialize_study_worker(directory: str, stage: str, registration_hash: str):
    global _WORKER_CONTEXT, _WORKER_INITIALIZATION_FAILED
    _WORKER_CONTEXT, _WORKER_INITIALIZATION_FAILED = None, False
    try:
        directory = Path(directory)
        registration = _registration(directory)
        if promotion_artifact_hash(registration) != registration_hash:
            raise ValueError("worker registration differs from the claimed stage")
        plan = registration["plan"]
        folds = plan["oos_folds"] if stage == "oos" else [plan["holdout"]]
        _WORKER_CONTEXT = {
            "directory": directory,
            "registration": registration,
            "frames": _stage_frames(directory, plan, folds),
        }
    except Exception:
        # Surface verification failure as a task error, so the parent records
        # the consumed attempt without publishing partial stage results.
        _WORKER_INITIALIZATION_FAILED = True


def _execute_study_worker(job: dict) -> dict:
    if _WORKER_INITIALIZATION_FAILED or _WORKER_CONTEXT is None:
        raise ValueError("research worker could not verify or load frozen inputs")
    return _execute_treatment(_WORKER_CONTEXT, job)


def _terminate_study_executor(executor):
    """Bound cleanup after failure without masking the original research error."""
    # Before Python 3.14, the executor has no public worker-termination method.
    # This guarded CPython fallback only touches this executor's child processes;
    # it cannot terminate unrelated multiprocessing children in the host app.
    processes = tuple((getattr(executor, "_processes", None) or {}).values())
    terminate = getattr(executor, "terminate_workers", None)
    try:
        if callable(terminate):
            terminate()
        else:
            for process in processes:
                if process.is_alive():
                    process.terminate()
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    for process in processes:
        try:
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        except Exception:
            pass


@contextmanager
def _study_job_runner(context: dict, stage: str, workers: int):
    if workers == 1:
        yield lambda jobs: (_execute_treatment(context, job) for job in jobs)
        return
    # Spawn never inherits a live journal, a broker client or account globals.
    # The executor also detects an abruptly lost worker: Pool.imap can otherwise
    # wait forever for a task whose worker was killed without returning a result.
    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_study_worker,
        initargs=(
            str(context["directory"]),
            stage,
            promotion_artifact_hash(context["registration"]),
        ),
    )
    try:
        yield lambda jobs: executor.map(_execute_study_worker, jobs, chunksize=1)
    except BaseException:
        _terminate_study_executor(executor)
        raise
    else:
        executor.shutdown(wait=True)


def run_study_stage(directory: str | Path, stage: str, *, workers: int = 1) -> dict:
    """Run frozen treatments; interrupted attempts stay consumed at any worker count."""
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 4
    ):
        raise ValueError("workers must be an integer between 1 and 4")
    directory = Path(directory).resolve()
    registration = _registration(directory)
    plan = registration["plan"]
    if stage not in {"oos", "holdout"}:
        raise ValueError("stage must be oos or holdout")
    if stage == "holdout":
        oos = _stage_result(directory, registration, "oos")
        if oos["empty_folds"] or not oos["results"]:
            raise ValueError("complete the frozen OOS study before accessing holdout")
    claim = {
        "registration_sha256": promotion_artifact_hash(registration),
        "stage": stage,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_once(directory / f"{stage}.access.json", claim)
    try:
        folds = plan["oos_folds"] if stage == "oos" else [plan["holdout"]]
        frames = _stage_frames(directory, plan, folds)
        results, portfolio_results, derived_references = [], [], []
        # The nominated candidate is frozen before OOS. Do not expose alternative
        # policy holdout results that could be used for post-hoc selection.
        policies = {"candidate": plan["candidate_policy"]}
        if stage == "oos":
            policies.update(plan["variants"])
        session = restore_session_policy(plan["session_policy"])
        context = {
            "directory": directory,
            "registration": registration,
            "frames": frames,
        }
        portfolio_jobs = []
        if plan.get("portfolio") is not None:
            for fold in folds:
                for policy_id in policies:
                    for scenario_id in plan["execution_scenarios"]:
                        portfolio_jobs.append(
                            {
                                "kind": "portfolio",
                                "fold": fold,
                                "policy_id": policy_id,
                                "scenario_id": scenario_id,
                                "retained_name": (
                                    f"{stage}-portfolio-{len(portfolio_jobs):06d}"
                                ),
                            }
                        )
        with _study_job_runner(context, stage, workers) as run_jobs:
            # Submission and collection follow the registered order. Completion
            # order cannot change report names, checkpoint order or statistics.
            for outcome in run_jobs(portfolio_jobs):
                portfolio_results.append(outcome["row"])
            paired_jobs = []
            for fold in folds:
                start, end = _stamp(fold["test_start"]), _stamp(fold["test_end"])
                cases = [
                    case
                    for case in plan["cases"]
                    if start <= _stamp(case["checkpoint_at"]) < end
                ]
                if plan["entry_case_source"] == "GENERATED_CONTROL_ENTRIES":
                    baseline = next(
                        row["report"]
                        for row in portfolio_results
                        if row["fold_id"] == fold["fold_id"]
                        and row["policy_id"] == "candidate"
                        and row["scenario_id"] == "base"
                    )
                    cases = deepcopy(baseline["control"]["entry_checkpoints"])
                    for case in cases:
                        case["case_id"] = f"{fold['fold_id']}:{case['case_id']}"
                for case in cases:
                    for policy_id in policies:
                        for scenario_id in plan["execution_scenarios"]:
                            paired_jobs.append(
                                {
                                    "kind": "paired",
                                    "fold": fold,
                                    "case": case,
                                    "policy_id": policy_id,
                                    "scenario_id": scenario_id,
                                    "retained_name": (
                                        f"{stage}-paired-{len(paired_jobs):06d}"
                                    ),
                                }
                            )
            for outcome in run_jobs(paired_jobs):
                results.append(outcome["row"])
                derived_references.extend(outcome["references"])
        sessions = []
        for fold in folds:
            start, end = _stamp(fold["test_start"]), _stamp(fold["test_end"])
            day = start.tz_convert(session.exchange_timezone).date()
            while day <= end.tz_convert(session.exchange_timezone).date():
                opened = pd.Timestamp(
                    datetime.combine(
                        day, session.open_time, tzinfo=session.exchange_timezone
                    )
                )
                closed = pd.Timestamp(
                    datetime.combine(
                        day, session.close_time, tzinfo=session.exchange_timezone
                    )
                )
                if (
                    day.weekday() < 5
                    and day not in session.holidays
                    and opened < end
                    and closed > start
                ):
                    sessions.append(day.isoformat())
                day += timedelta(days=1)
        references = [
            item
            for item in plan["reference_diagnostics"]
            if any(item.get("case_id") == row["case_id"] for row in results)
        ]
        if plan.get("reference_policy") is not None:
            references = derived_references
        statistics = summarize_paired_stage(
            results,
            sessions=sorted(set(sessions)),
            policy=plan["inference_policy"],
            reference_diagnostics=references,
        )
        # A long run must not publish the initial source identity after the
        # implementation or input bytes changed while it was executing.
        _registration(directory)
        result = {
            **claim,
            "status": "COMPLETED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "scope": registration["scope"],
            "data_classification": plan["data_classification"],
            "trial_count": len(results),
            "results": results,
            "portfolio_results": portfolio_results,
            "portfolio_trial_count": len(portfolio_results),
            "statistics": statistics,
            "reference_diagnostics": references,
            "empty_folds": [
                fold["fold_id"]
                for fold in folds
                if not any(row["fold_id"] == fold["fold_id"] for row in results)
            ],
        }
        result["execution_coverage"] = _execution_coverage_summary(result)
        _write_once(directory / f"{stage}.result.json", result)
        _write_once(
            directory / f"{stage}.result.hash.json",
            {"sha256": promotion_artifact_hash(result)},
        )
        return result
    except Exception as exc:
        _write_once(
            directory / f"{stage}.failure.json",
            {**claim, "status": "FAILED", "error_type": type(exc).__name__},
        )
        raise


def _cohort_plan(registration: dict, mode: str) -> dict | None:
    plan = registration["plan"]
    cohort = plan.get("operational_cohorts", {}).get(mode)
    if cohort is None:
        return None
    return {
        "study_id": plan["study_id"],
        "source_revision": registration["source"]["source_tree_sha256"],
        "mode": mode,
        "policy_artifact_hash": promotion_artifact_hash(plan["candidate_policy"]),
        "slots": cohort["required_slots"],
    }


def _bound_operational_payload(path: Path, registration: dict) -> dict:
    payload = _read(path)
    if promotion_artifact_hash(payload) != _read(path.with_suffix(".hash.json"))[
        "sha256"
    ] or payload.get("registration_sha256") != promotion_artifact_hash(registration):
        raise ValueError(
            "operational capture artifact changed or belongs to another study"
        )
    return payload


def _write_bound_operational(path: Path, registration: dict, **values) -> None:
    payload = {"registration_sha256": promotion_artifact_hash(registration), **values}
    _write_once(path, payload)
    _write_once(
        path.with_suffix(".hash.json"), {"sha256": promotion_artifact_hash(payload)}
    )


def claim_operational_capture(directory: str | Path, mode: str, slot_id: str) -> dict:
    """Consume an attempt before starting capture; missing/failed attempts remain due."""
    directory = Path(directory).resolve()
    registration = _registration(directory)
    if mode not in _OPERATIONAL_MODES:
        raise ValueError("capture claim requires a shadow or paper mode")
    cohort = _cohort_plan(registration, mode)
    if cohort is None or slot_id not in {slot["slot_id"] for slot in cohort["slots"]}:
        raise ValueError("capture claim requires a preregistered cohort slot")
    claim = {
        "attempt_id": uuid.uuid4().hex,
        "slot_id": slot_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    root = directory / "operational" / mode.lower()
    (root / "claims").mkdir(parents=True, exist_ok=True)
    _write_bound_operational(
        root / "claims" / f"{claim['attempt_id']}.json",
        registration,
        mode=mode,
        claim=claim,
    )
    return {
        **claim,
        "mode": mode,
        "study_id": cohort["study_id"],
        "source_revision": cohort["source_revision"],
        "policy": registration["plan"]["candidate_policy"],
        "capture_slot": slot_id,
        "capture_attempt_id": claim["attempt_id"],
        "live_activation": "DISABLED",
    }


class _OperationalReportFiles(Mapping):
    """Validate and load one complete offline export at a time."""

    def __init__(self, paths: dict, registration: dict, mode: str):
        self.paths, self.registration, self.mode = paths, registration, mode

    def __iter__(self):
        return iter(self.paths)

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, attempt):
        payload = _bound_operational_payload(self.paths[attempt], self.registration)
        if payload.get("mode") != self.mode:
            raise ValueError("operational cohort mode changed")
        return payload["report"]


def _cohort_result(directory: Path, registration: dict, mode: str) -> dict:
    root = directory / "operational" / mode.lower()
    claims, reports = [], {}
    for kind in ("claims", "reports"):
        folder = root / kind
        if not folder.exists():
            continue
        paths = sorted(
            p for p in folder.glob("*.json") if not p.name.endswith(".hash.json")
        )
        expected_names = {
            name for p in paths for name in (p.name, p.with_suffix(".hash.json").name)
        }
        if {p.name for p in folder.iterdir()} != expected_names:
            raise ValueError(
                "operational cohort contains incomplete or unexpected artifacts"
            )
        for path in paths:
            if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                raise ValueError("operational attempt requires its original identity")
            if kind == "claims":
                payload = _bound_operational_payload(path, registration)
                if payload.get("mode") != mode:
                    raise ValueError("operational cohort mode changed")
                claim = payload["claim"]
                if claim.get("attempt_id") != path.stem:
                    raise ValueError("operational claim identity changed")
                claims.append(claim)
            else:
                reports[path.stem] = path
    return assess_operational_cohort(
        _OperationalReportFiles(reports, registration, mode),
        registration["plan"]["operational_limits"],
        _cohort_plan(registration, mode),
        claims,
        expected_policy=registration["plan"]["candidate_policy"],
    )


def record_operational_evidence(directory: str | Path, report_path: str | Path) -> dict:
    """Validate a real captured export against the registered policy and limits."""
    directory = Path(directory).resolve()
    registration = _registration(directory)
    report = _read(_input_path(Path.cwd(), str(report_path)))
    plan = registration["plan"]
    if report.get("study_id") != plan["study_id"] or report.get(
        "policy_artifact_hash"
    ) != promotion_artifact_hash(plan["candidate_policy"]):
        raise ValueError("operational export belongs to another study/policy")
    provenance = report.get("provenance", {})
    if (
        not isinstance(provenance, dict)
        or provenance.get("source_revision")
        != (registration["source"]["source_tree_sha256"])
    ):
        raise ValueError(
            "operational capture executed different source from registered study"
        )
    mode = report.get("mode")
    if not isinstance(mode, str) or mode not in _OPERATIONAL_MODES:
        raise ValueError("operational evidence requires shadow or isolated paper")
    if _cohort_plan(registration, mode) is not None:
        attempt = provenance.get("capture_attempt_id")
        if (
            not isinstance(attempt, str)
            or re.fullmatch(r"[0-9a-f]{32}", attempt) is None
        ):
            raise ValueError(
                "operational cohort export requires its pre-capture attempt"
            )
        root = directory / "operational" / mode.lower()
        claim_path = root / "claims" / f"{attempt}.json"
        if not claim_path.exists():
            raise ValueError(
                "operational capture attempt was not claimed before capture"
            )
        claim_payload = _bound_operational_payload(claim_path, registration)
        claim = claim_payload["claim"]
        if (
            claim_payload.get("mode") != mode
            or claim.get("attempt_id") != attempt
            or claim.get("slot_id") != provenance.get("capture_slot")
            or _stamp(claim["claimed_at"])
            > _stamp(provenance.get("captured_started_at"))
        ):
            raise ValueError("operational export differs from its pre-capture claim")
        (root / "reports").mkdir(parents=True, exist_ok=True)
        _write_bound_operational(
            root / "reports" / f"{attempt}.json", registration, mode=mode, report=report
        )
        del report
        return _cohort_result(directory, registration, mode)
    result = assess_operational_run(
        report, plan["operational_limits"], expected_policy=plan["candidate_policy"]
    )
    payload = {
        "registration_sha256": promotion_artifact_hash(registration),
        "assessment": result,
        "report": report,
    }
    _write_once(directory / f"{mode.lower()}.json", payload)
    _write_once(
        directory / f"{mode.lower()}.hash.json",
        {"sha256": promotion_artifact_hash(payload)},
    )
    return result


def _operational_result(directory: Path, registration: dict, mode: str) -> dict:
    if _cohort_plan(registration, mode.upper()) is not None:
        return _cohort_result(directory, registration, mode.upper())
    path = directory / f"{mode}.json"
    if not path.exists():
        return {"passed": False, "failures": ["NO_CAPTURED_OPERATIONAL_EXPORT"]}
    payload = _read(path)
    if (
        promotion_artifact_hash(payload)
        != _read(directory / f"{mode}.hash.json")["sha256"]
        or payload.get("registration_sha256") != promotion_artifact_hash(registration)
        or payload["report"].get("study_id") != registration["plan"]["study_id"]
        or payload["report"].get("mode") != mode.upper()
        or payload["report"].get("provenance", {}).get("source_revision")
        != registration["source"]["source_tree_sha256"]
    ):
        raise ValueError("operational export changed or belongs to another study")
    return assess_operational_run(
        payload["report"],
        registration["plan"]["operational_limits"],
        expected_policy=registration["plan"]["candidate_policy"],
    )


def _review_package(directory: Path, registration: dict, package: dict) -> dict:
    """Bind a completed research/security review to this exact frozen study.

    Numerical claims must match executed studies. Security reviews are separately
    retained evidence. Their provenance still requires independent human review; neither
    this function nor a report hash can create or authenticate those observations.
    """
    if not isinstance(package, dict) or set(package) != {"manifest", "evidence"}:
        raise ValueError("promotion package requires manifest and evidence")
    plan = registration["plan"]
    manifest, evidence = package["manifest"], package["evidence"]
    review = evaluate_promotion_gate(manifest, evidence, _criteria(plan["criteria"]))
    failures = list(review.failures)
    if plan["control_kind"] != "LEGACY_REPAIRED":
        failures.append("REGISTERED_CONTROL_IS_NOT_REPAIRED_LEGACY")
    if not plan.get("portfolio"):
        failures.append("REGISTERED_PRODUCTION_PORTFOLIO_NOT_CONFIGURED")
    expected = {
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
        "prior_trial_count": plan["prior_trial_count"],
        "promotion_criteria_declared_at": registration["registered_at"],
        "policy_frozen_at": registration["registered_at"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            failures.append(f"REGISTERED_{key.upper()}_MISMATCH")
    config = manifest.get("config")
    if (
        not isinstance(config, dict)
        or config.get("policy_version") != plan["candidate_policy"]["policy_version"]
    ):
        failures.append("PROMOTED_POLICY_DIFFERS_FROM_REGISTERED_CANDIDATE")
    stage_hashes = {}
    for stage in ("oos", "holdout"):
        if not (directory / f"{stage}.result.json").exists():
            failures.append(f"REGISTERED_{stage.upper()}_NOT_COMPLETED")
            continue
        result = _stage_result(directory, registration, stage)
        stage_hashes[stage] = promotion_artifact_hash(result)
        expected_policies = (
            {"candidate", *plan["variants"]} if stage == "oos" else {"candidate"}
        )
        treatments = {
            (policy_id, scenario_id)
            for policy_id in expected_policies
            for scenario_id in plan["execution_scenarios"]
        }
        by_case = {}
        for row in result["results"]:
            by_case.setdefault((row["fold_id"], row["case_id"]), []).append(
                (row["policy_id"], row["scenario_id"])
            )
        if not by_case or any(
            set(rows) != treatments or len(rows) != len(treatments)
            for rows in by_case.values()
        ):
            failures.append(
                f"{stage.upper()}_REGISTERED_PAIRED_TREATMENTS_MISSING_OR_DUPLICATED"
            )
        expected_folds = plan["oos_folds"] if stage == "oos" else [plan["holdout"]]
        expected_portfolios = {
            (fold["fold_id"], policy_id, scenario_id)
            for fold in expected_folds
            for policy_id, scenario_id in treatments
        }
        actual_portfolios = [
            (row["fold_id"], row["policy_id"], row["scenario_id"])
            for row in result["portfolio_results"]
        ]
        if set(actual_portfolios) != expected_portfolios or len(
            actual_portfolios
        ) != len(expected_portfolios):
            failures.append(
                f"{stage.upper()}_REGISTERED_PORTFOLIO_TREATMENTS_MISSING_OR_DUPLICATED"
            )
        failures.extend(
            _executed_research_failures(
                stage, result, manifest, evidence, plan["criteria"]
            )
        )
        if result["empty_folds"] or not result["results"]:
            failures.append(f"REGISTERED_{stage.upper()}_HAS_EMPTY_FOLDS")
        if (
            stage == "oos"
            and manifest.get("research_started_at") != result["started_at"]
        ):
            failures.append("RESEARCH_START_DIFFERS_FROM_RECORDED_ACCESS")
        if stage == "oos":
            counts = {
                fold["fold_id"]: sum(
                    len(row["report"]["candidate"]["trades"])
                    for row in result["results"]
                    if row["fold_id"] == fold["fold_id"]
                    and row["policy_id"] == "candidate"
                    and row["scenario_id"] == "base"
                )
                for fold in plan["oos_folds"]
            }
            fold_results = manifest.get("fold_results")
            valid = isinstance(fold_results, list) and len(fold_results) == len(counts)
            for item, fold in zip(fold_results if valid else [], plan["oos_folds"]):
                if (
                    not isinstance(item, dict)
                    or item.get("selected_policy_id") != "candidate"
                    or not isinstance(item.get("metrics"), dict)
                    or item["metrics"].get("trade_count") != counts[fold["fold_id"]]
                ):
                    valid = False
            if not valid or evidence.get("completed_trade_count") != sum(
                counts.values()
            ):
                failures.append("OOS_COUNTS_OR_POLICY_DIFFER_FROM_EXECUTED_BASELINE")
        if stage == "holdout":
            declared_holdout = manifest.get("untouched_holdout", {})
            if not isinstance(declared_holdout, dict):
                declared_holdout = {}
            if any(
                declared_holdout.get(key) != value
                for key, value in {
                    "start": plan["holdout"]["test_start"],
                    "end_exclusive": plan["holdout"]["test_end"],
                    "evaluated_at": result["completed_at"],
                }.items()
            ):
                failures.append("HOLDOUT_DIFFERS_FROM_REGISTERED_EXECUTION")
    if manifest.get("acceptance_stage_hashes") != stage_hashes:
        failures.append("REPORTS_NOT_BOUND_TO_EXECUTED_STAGES")
    supplied_folds = manifest.get("folds", [])
    if (
        not isinstance(supplied_folds, list)
        or [
            {
                key: fold.get(key)
                for key in ("fold_id", "warmup_start", "test_start", "test_end")
            }
            for fold in supplied_folds
            if isinstance(fold, dict)
        ]
        != plan["oos_folds"]
    ):
        failures.append("OOS_FOLDS_DIFFER_FROM_REGISTRATION")
    for mode, gate in (
        ("live_shadow", "shadow_operational_passed"),
        ("isolated_paper", "isolated_paper_operational_passed"),
    ):
        assessed = _operational_result(directory, registration, mode)
        if not assessed["passed"]:
            failures.append(f"REGISTERED_{mode.upper()}_NOT_PASSED")
        reports = evidence.get("reports")
        report = reports.get(gate) if isinstance(reports, dict) else None
        report = report.get("artifact") if isinstance(report, dict) else None
        observations = report.get("observations") if isinstance(report, dict) else None
        if (
            not isinstance(observations, dict)
            or observations.get("operational_export_sha256")
            != assessed.get("report_sha256")
            or not assessed.get("report_sha256")
        ):
            failures.append(f"{mode.upper()}_REPORT_NOT_BOUND_TO_CAPTURE")
    return {
        "passed": not failures,
        "failures": list(dict.fromkeys(failures)),
        "evidence_summary": dict(review.evidence_summary),
        "live_activation": "DISABLED",
    }


def _execution_coverage_summary(result: Mapping) -> dict:
    """Distinguish entry cancellation stress from management of actual exposure."""
    return {
        "portfolio_treatments": [
            {
                **{key: row[key] for key in ("fold_id", "policy_id", "scenario_id")},
                **{
                    branch: row["report"][branch].get("execution_coverage")
                    for branch in ("candidate", "control")
                },
            }
            for row in result.get("portfolio_results", [])
        ],
        "paired_stress_treatments_with_entry_exposure": sum(
            row["scenario_id"] != "base"
            and bool(
                row["report"]["artifacts"]["inputs"]["payload"]["case"]["positions"]
            )
            and all(
                position["thesis"]["fill_binding"]["filled_quantity"] > 0
                for position in row["report"]["artifacts"]["inputs"]["payload"]["case"][
                    "positions"
                ]
            )
            for row in result["results"]
        ),
    }


def _executed_research_failures(
    stage, result, manifest, evidence, criteria
) -> list[str]:
    """Reject reviewed claims that cannot be joined to measured run outputs."""
    failures = []
    prefix = stage.upper()
    statistics = result["statistics"]
    bound = manifest.get("acceptance_statistics_hashes", {})
    if not isinstance(bound, dict) or bound.get(stage) != promotion_artifact_hash(
        statistics
    ):
        failures.append(f"{prefix}_STATISTICS_NOT_BOUND_TO_EXECUTED_RESULTS")
    if stage == "oos":
        for metric, value in statistics["metrics"].items():
            if evidence.get(metric) != value:
                failures.append(f"EXECUTED_{metric.upper()}_MISMATCH")
    else:
        metrics = statistics["metrics"]
        improvements = (
            ("capture_improvement_r_lower_bound", "minimum_capture_improvement_r"),
            (
                "premature_exit_reduction_lower_bound",
                "minimum_premature_exit_reduction",
            ),
        )
        if not any(
            metrics.get(metric) is not None
            and criteria.get(limit) is not None
            and metrics[metric] >= criteria[limit]
            for metric, limit in improvements
        ):
            failures.append("HOLDOUT_MATERIALITY_NOT_ESTABLISHED")
        for metric, limit in (
            ("unresolved_execution_rate", "maximum_unresolved_execution_rate"),
            ("ambiguous_execution_rate", "maximum_ambiguous_execution_rate"),
            ("tail_adverse_r", "maximum_tail_adverse_r"),
            (
                "delayed_invalidation_increase_upper_bound",
                "maximum_delayed_invalidation_increase",
            ),
            (
                "cost_stressed_loss_increase_r_upper_bound",
                "maximum_cost_stressed_loss_increase_r",
            ),
        ):
            if (
                metrics.get(metric) is None
                or criteria.get(limit) is None
                or metrics[metric] > criteria[limit]
            ):
                failures.append(f"HOLDOUT_{metric.upper()}_NOT_ESTABLISHED")
    # Both windows must establish uncertainty, not merely have been accessed.
    for name in ("capture", "delay", "stressed_loss"):
        if statistics["intervals"][name]["status"] != "ESTIMATED":
            failures.append(f"{prefix}_{name.upper()}_INFERENCE_INCONCLUSIVE")
    paired = result["results"]
    if any(row["report"].get("control_mode") != "LEGACY_REPAIRED" for row in paired):
        failures.append(f"{prefix}_EXECUTED_CONTROL_IS_NOT_REPAIRED_LEGACY")
    portfolios = result.get("portfolio_results", [])
    expected = {
        (row["fold_id"], row["policy_id"], row["scenario_id"]) for row in paired
    }
    observed = {
        (row["fold_id"], row["policy_id"], row["scenario_id"]) for row in portfolios
    }
    if not portfolios or observed != expected or len(observed) != len(portfolios):
        failures.append(f"{prefix}_PORTFOLIO_TREATMENT_COVERAGE_INCOMPLETE")
    for row in portfolios:
        report = row["report"]
        if report.get("production_admission_parity") is not True:
            failures.append(f"{prefix}_PRODUCTION_ADMISSION_PARITY_UNVERIFIED")
        for branch in ("candidate", "control"):
            account = report[branch]
            coverage = account.get("execution_coverage")
            if not isinstance(coverage, dict) or coverage.get("scope") not in {
                "HELD_EXPOSURE",
                "ENTRY_ADMISSION_ONLY",
                "NO_ENTRY_ORDERS",
            }:
                failures.append(f"{prefix}_PORTFOLIO_EXECUTION_COVERAGE_UNKNOWN")
            if account["status"] != "COMPLETE":
                failures.append(f"{prefix}_PORTFOLIO_HAS_UNRESOLVED_EXPOSURE")
            if account.get("excluded_entry_checkpoints"):
                failures.append(f"{prefix}_PORTFOLIO_HAS_UNPAIRED_ENTRIES")
            if account["parity"]["mismatches"]:
                failures.append(f"{prefix}_PORTFOLIO_DECISION_PARITY_FAILED")
    reports = evidence.get("reports")
    stress = (
        reports.get("execution_and_data_stress_passed")
        if isinstance(reports, dict)
        else None
    )
    artifact = stress.get("artifact") if isinstance(stress, dict) else None
    observations = artifact.get("observations") if isinstance(artifact, dict) else None
    declared = (
        observations.get("execution_coverage")
        if isinstance(observations, dict)
        else None
    )
    if not isinstance(declared, dict) or declared.get(
        stage
    ) != _execution_coverage_summary(result):
        failures.append(f"{prefix}_EXECUTION_STRESS_COVERAGE_NOT_BOUND_TO_RUNS")
    return failures


def review_promotion_package(directory: str | Path, package_path: str | Path) -> dict:
    """Retain a content-bound review without changing any trading configuration."""
    directory = Path(directory).resolve()
    registration = _registration(directory)
    package = _read(_input_path(Path.cwd(), str(package_path)))
    result = _review_package(directory, registration, package)
    payload = {
        "registration_sha256": promotion_artifact_hash(registration),
        "package": package,
        "assessment": result,
    }
    _write_once(directory / "promotion.review.json", payload)
    _write_once(
        directory / "promotion.review.hash.json",
        {"sha256": promotion_artifact_hash(payload)},
    )
    return result


def assess_study(directory: str | Path) -> dict:
    """Report measured progress; missing real evidence remains an explicit gate."""
    directory = Path(directory).resolve()
    registration = _registration(directory)
    stages = {}
    for stage in ("oos", "holdout"):
        result_path = directory / f"{stage}.result.json"
        if result_path.exists():
            result = _stage_result(directory, registration, stage)
            baseline = [
                row
                for row in result["results"]
                if row["policy_id"] == "candidate" and row["scenario_id"] == "base"
            ]
            comparisons = [
                item for row in baseline for item in row["report"]["comparisons"]
            ]
            complete = [
                item for item in comparisons if item.get("net_r_delta") is not None
            ]
            stages[stage] = {
                "status": result["status"],
                "baseline_cases": len(baseline),
                "complete_pairs": len(complete),
                "censored_pairs": len(comparisons) - len(complete),
                "total_paired_net_r_delta": sum(
                    item["net_r_delta"] for item in complete
                )
                if complete
                else None,
                "registered_treatments_executed": result["trial_count"],
                "empty_folds": result["empty_folds"],
                "statistics": result["statistics"],
                "execution_coverage": result["execution_coverage"],
                "portfolio_treatments_executed": result["portfolio_trial_count"],
                "portfolio_accounts": [
                    {
                        "fold_id": row["fold_id"],
                        "policy_id": row["policy_id"],
                        "scenario_id": row["scenario_id"],
                        **{
                            branch: {
                                "status": row["report"][branch]["status"],
                                "metrics": row["report"][branch]["metrics"],
                                "accepted_entries": sum(
                                    bool(item["accepted"])
                                    for item in row["report"][branch]["admissions"]
                                ),
                                "rejected_opportunities": len(
                                    row["report"][branch]["rejected_opportunities"]
                                ),
                            }
                            for branch in ("candidate", "control")
                        },
                    }
                    for row in result["portfolio_results"]
                ],
            }
        else:
            stages[stage] = {
                "status": "ATTEMPT_FAILED_OR_INTERRUPTED"
                if (directory / f"{stage}.access.json").exists()
                else "NOT_RUN"
            }
    operational = {}
    for mode in ("live_shadow", "isolated_paper"):
        operational[mode] = _operational_result(directory, registration, mode)
    review = {"passed": False, "failures": ["NO_BOUND_PROMOTION_REVIEW_PACKAGE"]}
    if (directory / "promotion.review.json").exists():
        payload = _read(directory / "promotion.review.json")
        if promotion_artifact_hash(payload) != _read(
            directory / "promotion.review.hash.json"
        )["sha256"] or payload.get("registration_sha256") != promotion_artifact_hash(
            registration
        ):
            raise ValueError("promotion review changed or belongs to another study")
        review = _review_package(directory, registration, payload["package"])
    return {
        "study_id": registration["plan"]["study_id"],
        "registration_sha256": promotion_artifact_hash(registration),
        "stages": stages,
        "operational": operational,
        "data_classification": registration["plan"]["data_classification"],
        "scope": registration["scope"],
        "promotion_ready": review["passed"],
        "remaining_acceptance": review["failures"],
        "live_activation": "DISABLED",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--plan", required=True)
    register.add_argument("--directory", required=True)
    for command in (
        "run",
        "status",
        "claim-operational",
        "record-operational",
        "review-promotion",
    ):
        sub = commands.add_parser(command)
        sub.add_argument("--directory", required=True)
        if command == "run":
            sub.add_argument("--stage", choices=("oos", "holdout"), required=True)
            sub.add_argument("--workers", type=int, choices=range(1, 5), default=1)
        if command == "record-operational":
            sub.add_argument("--report", required=True)
        if command == "claim-operational":
            sub.add_argument(
                "--mode", choices=sorted(_OPERATIONAL_MODES), required=True
            )
            sub.add_argument("--slot", required=True)
        if command == "review-promotion":
            sub.add_argument("--package", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            record = register_study(args.plan, args.directory)
            result = {
                "study_id": record["plan"]["study_id"],
                "registered": True,
                "live_activation": "DISABLED",
            }
        elif args.command == "run":
            record = run_study_stage(args.directory, args.stage, workers=args.workers)
            result = {
                "stage": args.stage,
                "status": record["status"],
                "trial_count": record["trial_count"],
                "empty_folds": record["empty_folds"],
            }
        elif args.command == "record-operational":
            result = record_operational_evidence(args.directory, args.report)
        elif args.command == "claim-operational":
            result = claim_operational_capture(args.directory, args.mode, args.slot)
        elif args.command == "review-promotion":
            result = review_promotion_package(args.directory, args.package)
        else:
            result = assess_study(args.directory)
        print(_json_bytes(result).decode(), end="")
        return 0
    except (ValueError, TypeError, KeyError, FileNotFoundError, FileExistsError) as exc:
        # Do not echo exported payloads or arbitrary file content into logs.
        print(
            f"Acceptance command failed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
