"""Stream full research evidence to disk while keeping stage summaries small.

The retained gzip contains every input, decision and execution record. Compaction
only affects the in-memory index; it must happen after independent reference
diagnostics have read the original price paths.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Mapping

SCHEMA = "retained-research-report-v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_ENCODER = json.JSONEncoder(
    sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
)


def _digest(value: Mapping) -> str:
    digest = hashlib.sha256()
    for chunk in _ENCODER.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_report(report: Mapping) -> dict:
    """Preserve measured outcomes and provenance without repeated raw traces."""
    result = deepcopy(
        {
            key: value
            for key, value in report.items()
            if key not in {"candidate", "control", "artifacts"}
        }
    )
    result["artifacts"] = {
        name: deepcopy(artifact)
        if name in {"inputs", "policies"}
        else {
            key: deepcopy(value) for key, value in artifact.items() if key != "payload"
        }
        for name, artifact in report.get("artifacts", {}).items()
    }
    for name in ("candidate", "control"):
        if name not in report:
            continue
        branch = report[name]
        summary = deepcopy(
            {
                key: value
                for key, value in branch.items()
                if key
                in {
                    "status",
                    "metrics",
                    "parity",
                    "entry_checkpoints",
                    "excluded_entry_checkpoints",
                    "censored_positions",
                    "execution_coverage",
                }
            }
        )
        summary["trades"] = [
            deepcopy(
                {key: value for key, value in trade.items() if key != "signal_info"}
            )
            for trade in branch.get("trades", [])
        ]
        for key in ("admissions", "rejected_opportunities"):
            if key in branch:
                summary[key] = [
                    {
                        field: value
                        for field, value in record.items()
                        if value is None or isinstance(value, (str, bool, int, float))
                    }
                    for record in branch[key]
                ]
        summary["retained_record_counts"] = {
            key: len(value) for key, value in branch.items() if isinstance(value, list)
        }
        result[name] = summary
    return result


def retain_report(directory: str | Path, name: str, report: Mapping) -> dict:
    """Exclusively write one complete report; return its compact index and hashes."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("retained report name must be a simple unique identifier")
    directory = Path(directory).resolve()
    reports = directory / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if reports.is_symlink():
        raise ValueError("retained report directory must not be a symlink")
    path = reports / f"{name}.json.gz"
    summary = compact_report(report)
    digest = hashlib.sha256()
    size = 0
    # The exclusive open is outside the cleanup block: an existing immutable
    # report must never be deleted when a caller accidentally reuses its name.
    with path.open("xb") as raw:
        try:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=6
            ) as compressed:
                with io.BufferedWriter(compressed, buffer_size=1024 * 1024) as stream:
                    for chunk in _ENCODER.iterencode(report):
                        encoded = chunk.encode("utf-8")
                        digest.update(encoded)
                        size += len(encoded)
                        stream.write(encoded)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return {
        "report": summary,
        "retained_report": {
            "schema_version": SCHEMA,
            "file": path.relative_to(directory).as_posix(),
            "sha256": _file_digest(path),
            "uncompressed_sha256": digest.hexdigest(),
            "uncompressed_bytes": size,
            "summary_sha256": _digest(summary),
        },
    }


def validate_retained_report(
    directory: str | Path, reference: Mapping, report: Mapping | None = None
) -> None:
    """Check the immutable link and gzip integrity with bounded read buffers."""
    if reference.get("schema_version") != SCHEMA:
        raise ValueError("unsupported retained report schema")
    relative = reference.get("file")
    if (
        not isinstance(relative, str)
        or not relative.startswith("reports/")
        or len(Path(relative).parts) != 2
        or not relative.endswith(".json.gz")
        or not _NAME.fullmatch(Path(relative).name)
    ):
        raise ValueError("retained report path must stay within the reports directory")
    directory = Path(directory).resolve()
    path = directory / relative
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("retained report links cannot be symlinks")
    if _file_digest(path) != reference.get("sha256"):
        raise ValueError("retained report compressed bytes changed")
    digest, size = hashlib.sha256(), 0
    with gzip.open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if digest.hexdigest() != reference.get(
        "uncompressed_sha256"
    ) or size != reference.get("uncompressed_bytes"):
        raise ValueError("retained report contents changed")
    if report is not None and _digest(report) != reference.get("summary_sha256"):
        raise ValueError("retained report summary changed")
