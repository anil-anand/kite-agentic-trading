"""Independent spawned research accounts retain complete, ordered evidence."""

import gzip
import json
import multiprocessing
import os
import re
import shutil
import time
from concurrent.futures.process import BrokenProcessPool
from copy import deepcopy

import pytest

from backend.backtesting import acceptance
from backend.backtesting.promotion import promotion_artifact_hash
from backend.backtesting.retained_report import validate_retained_report
from backend.tests.test_acceptance_workflow import _plan, _read, _write
from backend.tests.test_portfolio_study import config
from backend.tests.test_research_study import DAY, SYMBOL


def _registered(tmp_path, **options):
    path, plan = _plan(tmp_path, **options)
    plan["reference_policy"] = {}
    plan["execution_scenarios"]["stress"] = {"execution_policy": {"slippage_bps": 8}}
    plan["portfolio"] = {
        "universe_events": [{"at": DAY.isoformat(), "symbols": [SYMBOL]}],
        "instrument_metadata": {
            SYMBOL: {"instrument_id": "SIM-TEST", "sector": "TEST"}
        },
        "strategy_config": {},
        "risk_config": config(),
    }
    _write(path, plan)
    directory = tmp_path / "serial"
    acceptance.register_study(path, directory)
    return directory


_OPAQUE_ID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\bol[0-9a-f]{18}\b"
)


def _execution_content(value):
    """Keep events/links; production coordinator IDs are intentionally uuid4.

    Normalize only opaque IDs bijectively. Artifact digests derived from those
    IDs are checked against their own retained bytes rather than each other.
    """
    identifiers = {}

    def normalize(item):
        if isinstance(item, dict):
            return {
                key: normalize(value)
                for key, value in sorted(item.items())
                if key not in {"sha256", "manifest_hash"}
            }
        if isinstance(item, list):
            return [normalize(value) for value in item]
        if isinstance(item, str):
            return _OPAQUE_ID.sub(
                lambda match: identifiers.setdefault(
                    match.group(), f"opaque-{len(identifiers)}"
                ),
                item,
            )
        return item

    return normalize(value)


def test_spawned_treatments_preserve_order_inputs_outcomes_and_complete_artifacts(
    tmp_path,
):
    serial = _registered(tmp_path, variants=True, multi_session=True, poison="holdout")
    parallel = tmp_path / "parallel"
    shutil.copytree(serial, parallel)
    expected = acceptance.run_study_stage(serial, "oos", workers=1)
    actual = acceptance.run_study_stage(parallel, "oos", workers=2)
    assert actual["trial_count"] == expected["trial_count"] == 8
    assert actual["portfolio_trial_count"] == expected["portfolio_trial_count"] == 4
    for result in (actual, expected):
        assert result["statistics"]["source_results_sha256"] == promotion_artifact_hash(
            result["results"]
        )
    assert {
        key: value
        for key, value in actual["statistics"].items()
        if key != "source_results_sha256"
    } == {
        key: value
        for key, value in expected["statistics"].items()
        if key != "source_results_sha256"
    }
    assert actual["reference_diagnostics"] == expected["reference_diagnostics"]
    assert not (parallel / "holdout.access.json").exists()
    for category in ("portfolio_results", "results"):
        for left, right in zip(expected[category], actual[category]):
            assert {
                key: value
                for key, value in left.items()
                if key not in {"report", "retained_report"}
            } == {
                key: value
                for key, value in right.items()
                if key not in {"report", "retained_report"}
            }
            assert left["retained_report"]["file"] == right["retained_report"]["file"]
            reports = []
            for directory, row in ((serial, left), (parallel, right)):
                validate_retained_report(
                    directory, row["retained_report"], row["report"]
                )
                with gzip.open(directory / row["retained_report"]["file"], "rt") as f:
                    reports.append(json.load(f))
            for name in ("inputs", "policies", "data"):
                assert reports[0]["artifacts"][name] == reports[1]["artifacts"][name]
            assert _execution_content(reports[0]) == _execution_content(reports[1])
    assert acceptance.assess_study(parallel)["stages"]["oos"]["status"] == "COMPLETED"


@pytest.mark.parametrize("workers", [True, False, 0, 5, -1, 1.0, "2", None])
def test_invalid_worker_count_never_claims_an_attempt(tmp_path, workers):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="workers"):
        acceptance.run_study_stage(tmp_path / "not-registered", "oos", workers=workers)
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("legacy_cleanup", [False, True])
def test_spawned_task_failure_preserves_consumed_attempt_and_stops_children(
    tmp_path, monkeypatch, legacy_cleanup
):
    if legacy_cleanup:
        monkeypatch.setattr(
            acceptance.ProcessPoolExecutor, "terminate_workers", None, raising=False
        )
    path, plan = _plan(tmp_path, variants=True, multi_session=True)
    bad_case_path = tmp_path / plan["cases"][0]
    bad_case = _read(bad_case_path)
    bad_case["initial_capital"] = 0
    _write(bad_case_path, bad_case)
    directory = tmp_path / "registered"
    acceptance.register_study(path, directory)
    previous_children = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ValueError):
        acceptance.run_study_stage(directory, "oos", workers=2)
    assert (directory / "oos.access.json").exists()
    assert (directory / "oos.failure.json").exists()
    assert not (directory / "oos.result.json").exists()
    assert {
        child.pid for child in multiprocessing.active_children()
    } == previous_children
    with pytest.raises(FileExistsError):
        acceptance.run_study_stage(directory, "oos", workers=2)


def test_worker_initialization_failure_is_reported_without_replacement_loop(
    tmp_path, monkeypatch
):
    actual_source = acceptance._source_identity()
    fake_source = deepcopy(actual_source)
    fake_source["source_tree_sha256"] = "a" * 64
    # A fresh spawned process does not inherit this parent's changed globals.
    monkeypatch.setattr(acceptance, "_source_identity", lambda: fake_source)
    path, _ = _plan(tmp_path)
    directory = tmp_path / "registered"
    registration = acceptance.register_study(path, directory)
    with pytest.raises(ValueError, match="worker could not verify"):
        acceptance.run_study_stage(directory, "oos", workers=2)
    failure = _read(directory / "oos.failure.json")
    assert failure["registration_sha256"] == promotion_artifact_hash(registration)
    assert not (directory / "oos.result.json").exists()


def _abrupt_worker_exit(job):
    os._exit(7)


def test_abrupt_worker_death_fails_promptly_and_consumes_the_stage(
    tmp_path, monkeypatch
):
    path, _ = _plan(tmp_path, variants=True)
    directory = tmp_path / "registered"
    acceptance.register_study(path, directory)
    monkeypatch.setattr(acceptance, "_execute_study_worker", _abrupt_worker_exit)
    before = time.monotonic()
    with pytest.raises(BrokenProcessPool):
        acceptance.run_study_stage(directory, "oos", workers=2)
    assert time.monotonic() - before < 20
    assert (directory / "oos.access.json").exists()
    assert _read(directory / "oos.failure.json")["error_type"] == "BrokenProcessPool"
    assert not (directory / "oos.result.json").exists()
    with pytest.raises(FileExistsError):
        acceptance.run_study_stage(directory, "oos", workers=2)
