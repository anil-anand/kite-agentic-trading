"""Full research traces remain immutable when the stage index is compacted."""

import gzip
import json
from copy import deepcopy

import pytest

from backend.backtesting.retained_report import (
    compact_report,
    retain_report,
    validate_retained_report,
)


def sample():
    branch = {
        "status": "COMPLETE",
        "metrics": {"net_pnl": 12.0},
        "parity": {"recorded_decisions": 1, "mismatches": 0},
        "trades": [{"net_pnl": 12.0, "ambiguity": False, "signal_info": {"large": []}}],
        "entry_checkpoints": [{"case_id": "entry-1", "positions": [{"thesis": {}}]}],
        "excluded_entry_checkpoints": [],
        "recorded_decisions": [{"full_decision": ["context"] * 1000}],
        "admissions": [{"accepted": True, "reason": "ACCEPTED", "signal": {}}],
        "rejected_opportunities": [],
        "fills": [{"quantity": 5}],
    }
    return {
        "schema_version": "paired-research-case-v1",
        "control_mode": "LEGACY_REPAIRED",
        "comparisons": [{"net_r_delta": 0.2}],
        "paired_counts": {"complete": 1},
        "artifacts": {
            "inputs": {"sha256": "input", "payload": {"case": {"positions": []}}},
            "policies": {"sha256": "policies", "payload": {"context": {}}},
            "data": {"sha256": "data", "payload": {"ABC": [{"close": 123.0}]}},
            "source": {"sha256": "source", "payload": {"source_files": {}}},
        },
        "candidate": branch,
        "control": deepcopy(branch),
    }


def test_full_report_roundtrip_and_compact_measured_outcomes(tmp_path):
    report = sample()
    original = deepcopy(report)
    result = retain_report(tmp_path, "oos-portfolio-0001", report)
    with gzip.open(tmp_path / result["retained_report"]["file"], "rt") as stream:
        assert json.load(stream) == original
    validate_retained_report(tmp_path, result["retained_report"], result["report"])
    compact = result["report"]
    assert compact["artifacts"]["inputs"] == original["artifacts"]["inputs"]
    assert compact["artifacts"]["data"] == {"sha256": "data"}
    assert compact["candidate"]["metrics"] == report["candidate"]["metrics"]
    assert compact["candidate"]["parity"] == report["candidate"]["parity"]
    assert (
        compact["candidate"]["entry_checkpoints"]
        == report["candidate"]["entry_checkpoints"]
    )
    assert compact["candidate"]["trades"] == [{"net_pnl": 12.0, "ambiguity": False}]
    assert compact["candidate"]["retained_record_counts"]["recorded_decisions"] == 1
    assert "recorded_decisions" not in compact["candidate"]
    assert compact["candidate"]["admissions"] == [
        {"accepted": True, "reason": "ACCEPTED"}
    ]
    assert report == original
    compact["artifacts"]["inputs"]["payload"]["case"]["positions"].append({})
    assert report == original


def test_retention_does_not_serialize_full_report_into_one_string(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("must stream encoding")

    monkeypatch.setattr(json, "dumps", forbidden)
    result = retain_report(tmp_path, "streamed", sample())
    validate_retained_report(tmp_path, result["retained_report"], result["report"])


def test_duplicate_name_cannot_replace_or_remove_retained_evidence(tmp_path):
    result = retain_report(tmp_path, "once", sample())
    path = tmp_path / result["retained_report"]["file"]
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        retain_report(tmp_path, "once", {"different": True})
    assert path.read_bytes() == original


def test_failed_encoding_removes_only_its_partial_file(tmp_path):
    report = sample()
    report["invalid"] = float("nan")
    with pytest.raises(ValueError):
        retain_report(tmp_path, "invalid", report)
    assert not (tmp_path / "reports/invalid.json.gz").exists()


@pytest.mark.parametrize(
    "field", ["sha256", "uncompressed_sha256", "uncompressed_bytes", "summary_sha256"]
)
def test_corrupt_hash_or_size_cannot_validate(tmp_path, field):
    result = retain_report(tmp_path, "corrupt", sample())
    result["retained_report"][field] = "wrong"
    with pytest.raises(ValueError):
        validate_retained_report(tmp_path, result["retained_report"], result["report"])


def test_compact_outcome_mutation_cannot_validate(tmp_path):
    result = retain_report(tmp_path, "summary", sample())
    result["report"]["candidate"]["metrics"]["net_pnl"] = 999999.0
    with pytest.raises(ValueError, match="summary changed"):
        validate_retained_report(tmp_path, result["retained_report"], result["report"])


@pytest.mark.parametrize("name", ["../escape", "/absolute", "", "a/b", ".hidden"])
def test_names_cannot_escape_reports(tmp_path, name):
    with pytest.raises(ValueError):
        retain_report(tmp_path, name, sample())


def test_symlink_reports_cannot_escape_study(tmp_path):
    study = tmp_path / "study"
    study.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (study / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        retain_report(study, "escape", sample())
    assert list(outside.iterdir()) == []


def test_compaction_handles_minimal_empty_branch_without_invented_trades():
    assert compact_report({"candidate": {}})["candidate"]["trades"] == []
