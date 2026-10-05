"""Opt-in operational recording. No network, credentials or order dispatch.

Wall-clock receipts are recorded at acquisition, never inferred from bars. The
append-only chain detects truncation within retained records and mutations;
external provenance remains a trust boundary. An injected clock is always a
synthetic fixture and cannot become acceptance evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..dev_mode import is_dev_mode
from ..replay import serialize_replay_artifact
from .promotion import promotion_artifact_hash
from .research_study import restore_exit_policy


def _json(value):
    return json.loads(json.dumps(serialize_replay_artifact(value), allow_nan=False))


def _at(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("capture timestamps must be aware")
    return result.astimezone(timezone.utc)


def _isolated(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to((Path.home() / ".kite-agentic-trading").resolve()):
        raise ValueError("operational captures require explicitly isolated storage")
    return path


def _runtime_source_revision():
    from .acceptance import _source_identity

    return _source_identity()["source_tree_sha256"]


class _DiskEventLog:
    """Validated file-backed sequence; retain only the last event and ID index."""

    def __init__(self, path, initial_hash):
        self.path = path
        self.initial_hash = initial_hash
        self.head = initial_hash
        self.count = 0
        self.last = None
        self.decision_ids = set()
        self.failed = False
        for event, digest in self._verified_records():
            self.remember(event, digest)

    def _verified_records(self):
        head, sequence, ended = self.initial_hash, 0, False
        with self.path.open() as source:
            for line in source:
                try:
                    event = json.loads(line)
                    digest = event.pop("sha256")
                    valid = (
                        not ended
                        and event["previous_sha256"] == head
                        and event["sequence"] == sequence
                        and digest == promotion_artifact_hash(event)
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("operational capture chain is corrupt") from exc
                if not valid:
                    raise ValueError("operational capture chain is corrupt")
                head, sequence = digest, sequence + 1
                ended = event["kind"] == "END"
                yield event, digest

    def remember(self, event, digest):
        if event["kind"] == "DECISION":
            decision_id = event["payload"]["record"]["decision_id"]
            if decision_id in self.decision_ids:
                raise ValueError("duplicate operational decision")
            self.decision_ids.add(decision_id)
        self.count += 1
        self.head, self.last = digest, event
        self.failed |= event["kind"] == "FAILURE" or (
            event["kind"] == "END" and bool(event["payload"].get("failed"))
        )

    def __len__(self):
        return self.count

    def __iter__(self):
        expected_count, expected_head = self.count, self.head
        count, head = 0, self.initial_hash
        for event, digest in self._verified_records():
            count, head = count + 1, digest
            yield event
        if count != expected_count or head != expected_head:
            raise ValueError("operational capture chain changed after opening")

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        if index == self.count - 1:
            return self.last
        for sequence, event in enumerate(self):
            if sequence == index:
                return event
        raise IndexError(index)


class OperationalRecorder:
    """A single-writer append-only capture, explicitly selected by the operator."""

    def __init__(self, directory, *, plan=None, fixture_clock=None):
        self.directory = _isolated(directory)
        self._read_only = plan is None
        self._clock = fixture_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._durable_sequences = {}
        self.failed = False
        if plan is not None:
            required = {
                "study_id",
                "mode",
                "data_source_id",
                "source_revision",
                "policy",
            }
            cohort_fields = {"capture_slot", "capture_attempt_id"}
            if set(plan) not in (required, required | cohort_fields) or plan[
                "mode"
            ] not in {
                "LIVE_SHADOW",
                "ISOLATED_PAPER",
            }:
                raise ValueError("capture plan requires explicit immutable identity")
            for name in set(plan) - {"policy"}:
                if not isinstance(plan[name], str) or not plan[name].strip():
                    raise ValueError("capture identity must be nonempty text")
            synthetic = bool(fixture_clock or is_dev_mode())
            runtime_revision = None if synthetic else _runtime_source_revision()
            if (
                runtime_revision is not None
                and plan["source_revision"] != runtime_revision
            ):
                raise ValueError(
                    "capture source_revision differs from running source tree"
                )
            self.directory.mkdir(parents=True, exist_ok=False)
            identity = _json(plan)
            identity["policy"] = _json(restore_exit_policy(plan["policy"]))
            identity.update(
                schema_version="operational-capture-v1",
                capture_id=uuid.uuid4().hex,
                source_classification=(
                    "SYNTHETIC_FIXTURE" if synthetic else "REAL_MARKET_DATA"
                ),
                runtime_source_tree_sha256=runtime_revision,
                started_at=self.now().isoformat(),
            )
            with (self.directory / "identity.json").open("x") as target:
                json.dump(identity, target, sort_keys=True, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            (self.directory / "events.jsonl").touch(exist_ok=False)
        self.identity = json.loads((self.directory / "identity.json").read_text())
        if (
            fixture_clock
            and self.identity["source_classification"] != "SYNTHETIC_FIXTURE"
        ):
            raise ValueError("a fixture clock cannot reopen an empirical capture")
        self.events = _DiskEventLog(
            self.directory / "events.jsonl", promotion_artifact_hash(self.identity)
        )
        self._head = self.events.head
        self._sealed = bool(self.events and self.events[-1]["kind"] == "END")
        self.failed = self.events.failed

    def now(self):
        return _at(self._clock())

    def append(self, kind, payload):
        with self._lock:
            if self._sealed:
                raise ValueError("operational capture has already ended")
            if self._read_only:
                raise ValueError(
                    "reopened captures are read-only; begin a new capture after restart"
                )
            if (
                kind == "DECISION"
                and payload["record"]["decision_id"] in self.events.decision_ids
            ):
                raise ValueError("duplicate operational decision")
            event = {
                "sequence": len(self.events),
                "previous_sha256": self._head,
                "kind": kind,
                "payload": _json(payload),
                "written_at": self.now().isoformat(),
            }
            digest = promotion_artifact_hash(event)
            with (self.directory / "events.jsonl").open("a") as target:
                target.write(
                    json.dumps(
                        {**event, "sha256": digest}, sort_keys=True, allow_nan=False
                    )
                    + "\n"
                )
                target.flush()
                os.fsync(target.fileno())
            self._head = digest
            self.events.remember(event, digest)
            return event

    def failure(self, reason):
        self.failed = True
        self.append("FAILURE", {"reason": str(reason)})

    def received(self):
        """Call before evaluation, using the acquisition wall clock."""
        return self.now()

    def decision(self, record, *, received_at):
        record = _json(record)
        policy = record["trace"]["input_snapshot"]["policy"]["policy"]
        if policy != self.identity["policy"]:
            self.failure("DECISION_POLICY_CHANGED")
            return
        at = _at(record["occurred_at"])
        if not _at(self.identity["started_at"]) <= _at(received_at) <= at <= self.now():
            self.failure("DECISION_IS_NOT_CONTEMPORANEOUS")
            return
        self.append(
            "DECISION", {"record": record, "received_at": _at(received_at).isoformat()}
        )
        # This timestamp follows the durable append, not its preceding write.
        self.append(
            "PERSISTED",
            {
                "decision_id": record["decision_id"],
                "persisted_at": self.now().isoformat(),
            },
        )

    def facts(self, *, positions=(), intents=(), fills=()):
        self.append(
            "FACTS", {"positions": positions, "intents": intents, "fills": fills}
        )

    def capture_live_position(self, journal, key):
        """Read exact epoch joins from the durable ledger, never symbol history."""
        facts = journal.get_operational_position_facts(key)
        if facts is None:
            raise ValueError("observed position has unavailable durable epoch facts")
        state = facts["state"]
        previous_sequence = self._durable_sequences.get(key, -1)
        for item in facts.get("state_history", []):
            if item["sequence"] > previous_sequence:
                self.append("DURABLE_POSITION_STATE", {"position_key": key, **item})
                self._durable_sequences[key] = item["sequence"]
        intents, fills = [], []
        for intent in facts["intents"]:
            intents.append(
                {
                    "intent_id": intent["intent_id"],
                    "position_key": key,
                    "origin": {"ENTER": "ENTRY", "PROTECT": "PROTECTION"}.get(
                        intent["intent_type"], "LEGACY_CONTROL"
                    ),
                    "decision_ids": [],
                    "order_ids": intent["order_ids"],
                    "status": intent["state"],
                }
            )
        for fill in facts["fills"]:
            fills.append(
                {
                    "fill_id": fill["broker_fill_id"],
                    "order_id": fill["broker_order_id"],
                    "position_key": key,
                    "side": fill["side"],
                    "quantity": fill["quantity"],
                    "price": fill["fill_price"],
                    "exchange_time": fill["exchange_time"],
                    "received_at": fill["recorded_at"],
                }
            )
        direction = facts["direction"]
        entry_fills = [fill for fill in fills if fill["side"] == direction]
        if not entry_fills:
            self.append(
                "UNFILLED_ENTRY_OBLIGATION", {"position_key": key, "intents": intents}
            )
            return
        # State becomes flat only through the production reconciliation reducer.
        working = sorted(
            {
                order
                for intent in intents
                if intent["status"]
                not in {
                    "CLOSED",
                    "COMPLETE",
                    "FILLED",
                    "CANCELLED",
                    "REJECTED",
                    "EXPIRED",
                }
                for order in intent["order_ids"]
            }
        )
        self.facts(
            positions=[
                {
                    "position_key": key,
                    "namespace": key.split(":", 1)[0],
                    "direction": direction,
                    "initial_quantity": sum(fill["quantity"] for fill in entry_fills),
                    "final_quantity": state["known_quantity"],
                    "reconciliation_complete": state["exposure"]
                    in {"CLOSED", "ENTRY_ABORTED"},
                    "opened_at": min(fill["exchange_time"] for fill in entry_fills),
                    "reconciled_at": self.now().isoformat(),
                    "working_order_ids": working,
                }
            ],
            intents=intents,
            fills=fills,
        )

    def finish(self, *, materialize=True):
        with self._lock:
            if not self._sealed:
                if self.identity.get("runtime_source_tree_sha256") is not None:
                    try:
                        unchanged = (
                            _runtime_source_revision()
                            == self.identity["runtime_source_tree_sha256"]
                        )
                    except Exception:
                        unchanged = False
                    if not unchanged:
                        self.failure("RUNTIME_SOURCE_CHANGED_DURING_CAPTURE")
                self.append(
                    "END", {"ended_at": self.now().isoformat(), "failed": self.failed}
                )
                self._sealed = True
        return self.export() if materialize else None

    def export(self):
        decisions, receipts, positions, intents, fills = {}, {}, {}, {}, {}
        position_observations, intent_observations = [], []
        candidate_links, latest_intent_observation = {}, {}
        unfilled = {}
        durable_state_history = []
        terminal = {"CLOSED", "COMPLETE", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        for event in self.events:
            payload = event["payload"]
            if event["kind"] == "DECISION":
                record = payload["record"]
                decisions[record["decision_id"]] = record
                receipts[record["decision_id"]] = {
                    "decision_id": record["decision_id"],
                    "received_at": payload["received_at"],
                    "persisted_at": None,
                }
            elif event["kind"] == "PERSISTED":
                receipts[payload["decision_id"]]["persisted_at"] = payload[
                    "persisted_at"
                ]
            elif event["kind"] == "PAPER_EXECUTION_RESULTS":
                for result in payload["results"]:
                    decision_id, intent_id = result["decision_id"], result["intent_id"]
                    record = decisions.get(decision_id)
                    if record is None:
                        raise ValueError("execution result has no preceding decision")
                    key = record["trace"]["input_snapshot"]["state"]["position_key"]
                    previous = intents.get(intent_id)
                    if previous and (
                        previous["position_key"] != key
                        or previous["origin"] != "CANDIDATE"
                    ):
                        raise ValueError("captured intent rebound to another epoch")
                    orders = set(previous["order_ids"] if previous else ())
                    if result.get("order_id"):
                        orders.add(result["order_id"])
                    row = {
                        "intent_id": intent_id,
                        "position_key": key,
                        "origin": "CANDIDATE",
                        "decision_ids": [decision_id],
                        "order_ids": sorted(orders),
                        "status": result["state"],
                    }
                    candidate_links.setdefault(intent_id, {})[decision_id] = None
                    intents[intent_id] = row
                    latest_intent_observation[intent_id] = len(intent_observations)
                    intent_observations.append(
                        {"observed_at": event["written_at"], **row}
                    )
            elif event["kind"] == "DURABLE_POSITION_STATE":
                durable_state_history.append(
                    {"captured_at": event["written_at"], **payload}
                )
            elif event["kind"] == "UNFILLED_ENTRY_OBLIGATION":
                key = payload["position_key"]
                working = [i for i in payload["intents"] if i["status"] not in terminal]
                if working:
                    unfilled[key] = {
                        "position_key": key,
                        "intents": working,
                        "observed_at": event["written_at"],
                    }
                else:
                    unfilled.pop(key, None)
            elif event["kind"] == "FACTS":
                for row in payload["positions"]:
                    unfilled.pop(row["position_key"], None)
                    position_observations.append(
                        {"observed_at": event["written_at"], **row}
                    )
                for row in payload["intents"]:
                    if row["origin"] == "CANDIDATE":
                        links = candidate_links.setdefault(row["intent_id"], {})
                        for decision_id in row["decision_ids"]:
                            links[decision_id] = None
                    latest_intent_observation[row["intent_id"]] = len(
                        intent_observations
                    )
                    intent_observations.append(
                        {"observed_at": event["written_at"], **row}
                    )
                for name, target, field in (
                    ("positions", positions, "position_key"),
                    ("intents", intents, "intent_id"),
                    ("fills", fills, "fill_id"),
                ):
                    for row in payload[name]:
                        previous = target.get(row[field])
                        if previous and name == "fills" and previous != row:
                            raise ValueError("conflicting immutable captured fill")
                        if previous and previous["position_key"] != row["position_key"]:
                            raise ValueError(
                                "captured identity rebound to another epoch"
                            )
                        target[row[field]] = row
        # Expand each intent's complete join once, at its final observation.
        # Prior observation rows retain their actual contemporaneous deltas;
        # older captures containing full cumulative FACTS remain compatible.
        for intent_id, links in candidate_links.items():
            decision_ids = list(links)
            intents[intent_id]["decision_ids"] = decision_ids
            intent_observations[latest_intent_observation[intent_id]][
                "decision_ids"
            ] = decision_ids
        unresolved = {
            row["position_key"]
            for row in intents.values()
            if row["status"]
            not in {"CLOSED", "COMPLETE", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        }
        censored = [
            {
                "position_key": key,
                "quantity": row["final_quantity"],
                "reason": "CAPTURE_ENDED_WITH_UNRESOLVED_EXECUTION",
            }
            for key, row in positions.items()
            if row["final_quantity"]
            or row["working_order_ids"]
            or key in unresolved
            or row.get("reconciliation_complete") is False
        ]
        end = (
            self.events[-1]["payload"]["ended_at"]
            if self._sealed
            else self.now().isoformat()
        )
        return {
            "schema_version": "operational-run-v1",
            "study_id": self.identity["study_id"],
            "mode": self.identity["mode"],
            "policy_artifact_hash": promotion_artifact_hash(self.identity["policy"]),
            "provenance": {
                "acquisition_mode": "REAL_TIME",
                "source_classification": self.identity["source_classification"],
                "data_source_id": self.identity["data_source_id"],
                "capture_artifact_ref": self.identity["capture_id"],
                "source_revision": self.identity["source_revision"],
                "runtime_source_tree_sha256": self.identity.get(
                    "runtime_source_tree_sha256"
                ),
                "capture_chain_sha256": self._head,
                "capture_complete": self._sealed and not self.failed,
                "captured_started_at": self.identity["started_at"],
                "captured_ended_at": end,
                "source_decision_count": len(decisions),
                "source_position_count": len(positions),
                "source_intent_count": len(intents),
                "source_fill_count": len(fills),
                **{
                    field: self.identity[field]
                    for field in ("capture_slot", "capture_attempt_id")
                    if field in self.identity
                },
            },
            "recorded_decisions": list(decisions.values()),
            "capture_receipts": list(receipts.values()),
            "positions": list(positions.values()),
            "intents": list(intents.values()),
            "fills": list(fills.values()),
            "censored_positions": censored,
            "durable_position_state_history": durable_state_history,
            "position_observations": position_observations,
            "intent_observations": intent_observations,
            "unfilled_entry_obligations": list(unfilled.values()),
        }


class LiveOperationalObserver:
    """Bounded background recorder; disk I/O never runs on the risk thread."""

    def __init__(self, recorder, journal):
        if recorder.identity["mode"] != "LIVE_SHADOW":
            raise ValueError("live observation requires LIVE_SHADOW mode")
        self.recorder, self.journal = recorder, journal
        self._closed = False
        self._positions = set()
        self.pending = queue.Queue(maxsize=1000)
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="exit-evidence-capture"
        )
        self.thread.start()

    def received(self):
        return self.recorder.received()

    def _enqueue(self, item):
        try:
            self.pending.put_nowait(item)
        except queue.Full:
            self.recorder.failed = True

    def decision(self, record, *, received_at):
        self._enqueue(("decision", _json(record), received_at))

    def position(self, key):
        self._enqueue(("position", key, None))

    def _run(self):
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                kind, value, received_at = item
                if kind == "decision":
                    self.recorder.decision(value, received_at=received_at)
                    key = value["trace"]["input_snapshot"]["state"]["position_key"]
                else:
                    key = value
                self._positions.add(key)
                self.recorder.capture_live_position(self.journal, key)
            except Exception:
                self.recorder.failed = True
                try:
                    self.recorder.failure("OBSERVATION_CAPTURE_FAILED")
                except Exception:
                    pass
            finally:
                self.pending.task_done()

    def close(self, *, drain_timeout_seconds=5.0, materialize=False):
        if self._closed:
            return self.recorder.export() if materialize else None
        self._closed = True
        deadline = time.monotonic() + drain_timeout_seconds
        while self.pending.unfinished_tasks:
            if time.monotonic() >= deadline:
                self.recorder.failed = True
                return self.recorder.export() if materialize else None
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        for key in sorted(self._positions):
            try:
                self.recorder.capture_live_position(self.journal, key)
            except Exception:
                self.recorder.failed = True
        self.pending.put(None)
        self.thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.thread.is_alive():
            self.recorder.failed = True
            return self.recorder.export() if materialize else None
        return self.recorder.finish(materialize=materialize)


def live_observer_from_environment(journal):
    """Explicit opt-in plan; importing this module does not read live storage."""
    path = os.environ.get("KITE_EXIT_CAPTURE_PLAN")
    if not path:
        return None
    plan = json.loads(_isolated(path).read_text())
    directory = plan.pop("directory")
    recorder = OperationalRecorder(directory, plan=plan)
    observer = LiveOperationalObserver(recorder, journal)
    import atexit

    atexit.register(observer.close)
    return observer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    recorder = OperationalRecorder(args.directory)
    # Only the process that observed the run may mark it complete. An export of
    # an unclean shutdown remains incomplete even when every position looks flat.
    with _isolated(args.output).open("x") as target:
        json.dump(recorder.export(), target, sort_keys=True, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
