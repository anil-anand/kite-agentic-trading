"""Assess retained operational exports without fetching data or touching a broker.

This is an evidence validator, not an operational run generator. A PAPER adapter
label, a replay parity test, and a historical run are not real-time observations.
Source attestations remain an external trust boundary: hashes verify retained
content and cannot authenticate the market-data vendor or the recorder's clock.

``operational-run-v1`` input contract (all times are aware ISO-8601 strings):

* ``mode`` is LIVE_SHADOW or ISOLATED_PAPER; ``provenance`` contains
  acquisition_mode=REAL_TIME, source_classification=REAL_MARKET_DATA,
  data_source_id, capture_artifact_ref, captured_started_at, captured_ended_at,
  source_decision_count, source_position_count, source_intent_count, and
  source_fill_count.
* ``recorded_decisions`` holds complete original decision payloads, including
  HOLD, with their retained input snapshots. ``capture_receipts`` has one
  {decision_id, received_at, persisted_at} per decision. These are recorder wall
  times, never reconstructed from historical candle timestamps.
* ``positions`` has {position_key, namespace, direction, initial_quantity,
  final_quantity, opened_at, reconciled_at, working_order_ids}. The position key
  is the exact epoch key retained in decisions; joins must not guess by symbol.
* ``intents`` has {intent_id, position_key, origin, decision_ids, order_ids,
  status}. origin is ENTRY, PROTECTION, LEGACY_CONTROL or CANDIDATE. Candidate
  decision IDs join to coordinator intents without assuming their IDs match.
* ``fills`` has {fill_id, order_id, position_key, side, quantity, price,
  exchange_time, received_at}. Exporters normalize broker/coordinator records
  and bind each fill to its actual position epoch, including all entry fills.
* ``censored_positions`` has {position_key, quantity, reason} for every residual
  or unresolved working-order position. Acknowledgements never imply fills.

An exporter must retain source counts before filtering, and all execution facts
for each observed position. Missing, malformed or incomplete exports fail closed.
The result grants no authority to activate a candidate or send broker orders.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Any, Mapping

from ..replay import replay_recorded_exit_decision, serialize_replay_artifact
from ..time_utils import EXCHANGE_TIMEZONE
from .promotion import promotion_artifact_hash


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (OverflowError, ValueError):
        return None


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            result.astimezone(timezone.utc) if result.utcoffset() is not None else None
        )
    except (ValueError, TypeError, OverflowError):
        return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _one_of(value: Any, choices: set[str]) -> bool:
    return isinstance(value, str) and value in choices


@dataclass(frozen=True)
class OperationalLimits:
    """Required sample and reliability limits, declared before a run is observed."""

    minimum_sessions: int
    minimum_decisions: int
    minimum_hold_decisions: int
    minimum_closed_positions: int
    maximum_observation_lag_seconds: float
    maximum_unresolved_position_rate: float

    def __post_init__(self) -> None:
        for field in (
            "minimum_sessions",
            "minimum_decisions",
            "minimum_hold_decisions",
            "minimum_closed_positions",
        ):
            if not _integer(getattr(self, field), minimum=1):
                raise ValueError(f"{field} must be a positive integer")
        for field in (
            "maximum_observation_lag_seconds",
            "maximum_unresolved_position_rate",
        ):
            number = _number(getattr(self, field))
            if number is None or number < 0:
                raise ValueError(f"{field} must be finite and nonnegative")
        if self.maximum_unresolved_position_rate > 1:
            raise ValueError("maximum_unresolved_position_rate must not exceed one")


def assess_operational_run(
    report: Mapping[str, Any],
    limits: OperationalLimits | Mapping[str, Any],
    *,
    expected_policy: Any = None,
) -> dict[str, Any]:
    """Measure replay, timing and execution completeness of one retained export.

    Invalid declarations raise ValueError/TypeError. Invalid or insufficient
    evidence returns a JSON-friendly failed assessment, with concrete reasons.
    A successful result verifies consistency under supplied provenance; external
    provenance authentication and security reviews are separate acceptance gates.
    """
    if not isinstance(report, Mapping):
        raise TypeError("operational evidence must be a mapping")
    if isinstance(limits, Mapping):
        limits = OperationalLimits(**dict(limits))
    if not isinstance(limits, OperationalLimits):
        raise TypeError("operational evidence requires declared OperationalLimits")
    failures: list[str] = []

    def fail(reason: str) -> None:
        if reason not in failures:
            failures.append(reason)

    def rows(name: str) -> list[Mapping[str, Any]]:
        items = report.get(name)
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) for item in items
        ):
            fail(f"MISSING_OR_INVALID_{name.upper()}")
            return []
        return items

    def index(items, field, label):
        result = {}
        for item in items:
            key = item.get(field)
            if not _text(key) or key in result:
                fail(f"INVALID_OR_DUPLICATE_{label}")
                continue
            result[key] = item
        return result

    try:
        report_hash = promotion_artifact_hash(dict(report))
    except (TypeError, ValueError, OverflowError):
        fail("REPORT_IS_NOT_FINITE_JSON")
        report_hash = None
    if not _text(report.get("study_id")):
        fail("MISSING_STUDY_IDENTITY")
    policy_hash = report.get("policy_artifact_hash")
    if (
        not isinstance(policy_hash, str)
        or len(policy_hash) != 64
        or any(char not in "0123456789abcdef" for char in policy_hash)
    ):
        fail("MISSING_POLICY_ARTIFACT_IDENTITY")
    expected_policy = serialize_replay_artifact(expected_policy)
    if (
        expected_policy is not None
        and promotion_artifact_hash(expected_policy) != policy_hash
    ):
        fail("POLICY_IDENTITY_DIFFERS_FROM_PREDECLARED_POLICY")
    if report.get("schema_version") != "operational-run-v1":
        fail("UNSUPPORTED_OPERATIONAL_REPORT_SCHEMA")
    mode = report.get("mode")
    namespace = {"LIVE_SHADOW": "LIVE", "ISOLATED_PAPER": "PAPER"}.get(
        mode if isinstance(mode, str) else ""
    )
    if namespace is None:
        fail("INVALID_OPERATIONAL_MODE")
    provenance = report.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    if (
        provenance.get("acquisition_mode") != "REAL_TIME"
        or provenance.get("source_classification") != "REAL_MARKET_DATA"
        or not _text(provenance.get("data_source_id"))
        or not _text(provenance.get("capture_artifact_ref"))
    ):
        fail("MISSING_REAL_TIME_MARKET_PROVENANCE")
    if provenance.get("capture_complete") is not True:
        fail("CAPTURE_INCOMPLETE_OR_FAILED")
    if report.get("unfilled_entry_obligations"):
        fail("UNRESOLVED_UNFILLED_ENTRY_OBLIGATIONS")
    capture_start = _time(provenance.get("captured_started_at"))
    capture_end = _time(provenance.get("captured_ended_at"))
    if capture_start is None or capture_end is None or capture_start >= capture_end:
        fail("INVALID_CAPTURE_INTERVAL")
    # Even a relabeled historical CandidateRunner export retains its REPLAY
    # namespace/adapter. Requiring receipt provenance prevents PAPER alone from
    # upgrading that historical run to operational evidence.
    manifest = report.get("manifest", {})
    execution = manifest.get("execution", {}) if isinstance(manifest, Mapping) else {}
    if (
        isinstance(execution, Mapping)
        and execution.get("namespace") is not None
        and execution.get("namespace") != namespace
    ):
        fail("EXECUTION_MANIFEST_NAMESPACE_MISMATCH")
    decisions = rows("recorded_decisions")
    receipts = rows("capture_receipts")
    positions = rows("positions")
    intents = rows("intents")
    fills = rows("fills")
    censored = rows("censored_positions")
    for field, items in (
        ("source_decision_count", decisions),
        ("source_position_count", positions),
        ("source_fill_count", fills),
        ("source_intent_count", intents),
    ):
        if not _integer(provenance.get(field)) or provenance[field] != len(items):
            fail(f"EXPORT_COUNT_MISMATCH_{field.upper()}")
    decision_index = index(decisions, "decision_id", "DECISION_ID")
    receipt_index = index(receipts, "decision_id", "RECEIPT_DECISION_ID")
    position_index = index(positions, "position_key", "POSITION_KEY")
    intent_index = index(intents, "intent_id", "INTENT_ID")
    fill_index = index(fills, "fill_id", "FILL_ID")
    censor_index = index(censored, "position_key", "CENSORED_POSITION_KEY")
    if set(receipt_index) != set(decision_index):
        fail("CAPTURE_RECEIPTS_DO_NOT_COVER_ALL_DECISIONS")
    sessions: set[str] = set()
    actions: Counter[str] = Counter()
    decision_positions = {}
    replay_count = timing_count = shadow_suppressed_count = 0
    max_lag = 0.0
    last_times = {}
    policy_fingerprints = set()
    for decision_id, record in decision_index.items():
        try:
            replay_recorded_exit_decision(record)
            replay_count += 1
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            AssertionError,
            AttributeError,
        ):
            fail("RECORDED_DECISION_REPLAY_MISMATCH")
        trace = record.get("trace")
        trace = trace if isinstance(trace, Mapping) else {}
        fingerprint = trace.get("policy_fingerprint")
        if _text(fingerprint):
            policy_fingerprints.add(fingerprint)
        inputs = trace.get("input_snapshot")
        inputs = inputs if isinstance(inputs, Mapping) else {}
        policy_inputs = inputs.get("policy")
        actual_policy = (
            policy_inputs.get("policy") if isinstance(policy_inputs, Mapping) else None
        )
        if actual_policy is None or (
            expected_policy is not None and actual_policy != expected_policy
        ):
            fail("DECISION_POLICY_DIFFERS_FROM_PREDECLARED_POLICY")
        else:
            try:
                if promotion_artifact_hash(actual_policy) != policy_hash:
                    fail("DECISION_POLICY_HASH_DOES_NOT_MATCH_REPORT")
            except (TypeError, ValueError, OverflowError):
                fail("DECISION_POLICY_HASH_DOES_NOT_MATCH_REPORT")
        state = inputs.get("state")
        state = state if isinstance(state, Mapping) else {}
        key = state.get("position_key")
        if not _text(key) or key not in position_index:
            fail("DECISION_POSITION_NOT_EXPORTED")
            continue
        decision_positions[decision_id] = key
        at = _time(record.get("occurred_at"))
        receipt = receipt_index.get(decision_id, {})
        received = _time(receipt.get("received_at"))
        persisted = _time(receipt.get("persisted_at"))
        valid_timing = all(
            value is not None
            for value in (at, received, persisted, capture_start, capture_end)
        )
        if valid_timing:
            valid_timing = capture_start <= received <= at <= persisted <= capture_end
            lag = (persisted - received).total_seconds()
            max_lag = max(max_lag, lag)
            valid_timing = (
                valid_timing and lag <= limits.maximum_observation_lag_seconds
            )
            if key in last_times and at < last_times[key]:
                valid_timing = False
            last_times[key] = at
            risk = inputs.get("risk")
            session = risk.get("session") if isinstance(risk, Mapping) else None
            if (
                isinstance(session, Mapping)
                and session.get("is_open") is True
                and session.get("is_trading_day") is True
            ):
                sessions.add(at.astimezone(EXCHANGE_TIMEZONE).date().isoformat())
        context = inputs.get("context")
        if isinstance(context, Mapping):
            context_at = _time(context.get("decision_event_time"))
            context_received = _time(context.get("received_at"))
            if (
                context_at is None
                or context_received is None
                or at is None
                or context_received > context_at
                or context_at > at
            ):
                valid_timing = False
            for field in ("primary_bars", "higher_bars"):
                bars = context.get(field)
                if not isinstance(bars, list):
                    valid_timing = False
                    continue
                for bar in bars:
                    end = _time(bar.get("end")) if isinstance(bar, Mapping) else None
                    available = (
                        _time(bar.get("available_at"))
                        if isinstance(bar, Mapping)
                        else None
                    )
                    if (
                        end is None
                        or available is None
                        or context_at is None
                        or end > available
                        or available > context_at
                    ):
                        valid_timing = False
        if valid_timing:
            timing_count += 1
        else:
            fail("DECISION_OR_CONTEXT_CAPTURE_TIMING_INVALID")
        action = record.get("action")
        if _text(action):
            actions[action] += 1
        if mode == "LIVE_SHADOW":
            orchestration = trace.get("orchestration")
            if (
                not isinstance(orchestration, Mapping)
                or orchestration.get("phase") != "phase7_shadow"
                or orchestration.get("dispatch") != "SUPPRESSED_PHASE7"
                or orchestration.get("candidate_activation_enabled") is not False
            ):
                fail("SHADOW_DISPATCH_SUPPRESSION_NOT_RETAINED")
            else:
                shadow_suppressed_count += 1

    order_intents = {}
    candidate_decisions = set()
    unresolved_intent_positions = set()
    for intent_id, intent in intent_index.items():
        key = intent.get("position_key")
        origin = intent.get("origin")
        decision_ids = intent.get("decision_ids")
        order_ids = intent.get("order_ids")
        if (
            not _text(key)
            or key not in position_index
            or not _one_of(
                origin, {"ENTRY", "PROTECTION", "LEGACY_CONTROL", "CANDIDATE"}
            )
            or not _text(intent.get("status"))
            or not isinstance(decision_ids, list)
            or any(not _text(item) for item in decision_ids)
            or not isinstance(order_ids, list)
            or any(not _text(item) for item in order_ids)
        ):
            fail("INVALID_EXECUTION_INTENT")
            continue
        if intent["status"] not in {
            "CLOSED",
            "COMPLETE",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
        }:
            unresolved_intent_positions.add(key)
        for decision_id in decision_ids:
            if decision_positions.get(decision_id) != key:
                fail("INTENT_DECISION_POSITION_MISMATCH")
        if origin == "CANDIDATE":
            if mode == "LIVE_SHADOW":
                fail("SHADOW_CANDIDATE_WAS_DISPATCHED")
            if not decision_ids:
                fail("CANDIDATE_INTENT_HAS_NO_DECISION")
            for decision_id in decision_ids:
                if decision_id in candidate_decisions:
                    fail("DECISION_BOUND_TO_MULTIPLE_CANDIDATE_INTENTS")
                if not _one_of(
                    decision_index.get(decision_id, {}).get("action"),
                    {"REQUEST_EXIT", "MANAGE_PENDING_INTENT", "TIGHTEN_STOP"},
                ):
                    fail("CANDIDATE_DISPATCH_HAS_NO_MUTATION_DECISION")
                candidate_decisions.add(decision_id)
        for order_id in order_ids:
            if order_id in order_intents:
                fail("ORDER_BOUND_TO_MULTIPLE_INTENTS")
            order_intents[order_id] = (key, intent_id)
    if mode == "ISOLATED_PAPER":
        if not candidate_decisions:
            fail("PAPER_CANDIDATE_EXECUTION_NOT_OBSERVED")
        for decision_id, record in decision_index.items():
            if (
                _one_of(
                    record.get("action"),
                    {
                        "REQUEST_EXIT",
                        "MANAGE_PENDING_INTENT",
                        "TIGHTEN_STOP",
                    },
                )
                and decision_id not in candidate_decisions
            ):
                fail("PAPER_ACTION_MISSING_COORDINATOR_INTENT")

    signed_fills: Counter[str] = Counter()
    entry_fills: Counter[str] = Counter()
    linked_fill_count = 0
    position_fills = {}
    for fill in fill_index.values():
        key = fill.get("position_key")
        order_id = fill.get("order_id")
        position = position_index.get(key, {}) if _text(key) else {}
        exchange_at = _time(fill.get("exchange_time"))
        received_at = _time(fill.get("received_at"))
        quantity = fill.get("quantity")
        price = _number(fill.get("price"))
        side = fill.get("side")
        order = order_intents.get(order_id) if _text(order_id) else None
        if (
            not position
            or order is None
            or order[0] != key
            or not _one_of(side, {"BUY", "SELL"})
            or not _integer(quantity, minimum=1)
            or price is None
            or price <= 0
            or exchange_at is None
            or received_at is None
            or exchange_at > received_at
            or capture_end is None
            or received_at > capture_end
        ):
            fail("INVALID_OR_UNLINKED_FILL")
            continue
        opened_at = _time(position.get("opened_at"))
        reconciled_at = _time(position.get("reconciled_at"))
        if (
            opened_at is None
            or reconciled_at is None
            or exchange_at < opened_at
            or received_at > reconciled_at
        ):
            fail("FILL_OUTSIDE_POSITION_RECONCILIATION_INTERVAL")
        entry_side = side == position.get("direction")
        if (intent_index[order[1]]["origin"] == "ENTRY") != entry_side:
            fail("FILL_DIRECTION_DISAGREES_WITH_INTENT_ORIGIN")
        position_fills.setdefault(key, []).append((received_at, side, quantity))
        signed_fills[key] += quantity if side == "BUY" else -quantity
        if side == position.get("direction"):
            entry_fills[key] += quantity
        linked_fill_count += 1

    closed_count = unresolved_count = 0
    observed_positions = set(decision_positions.values())
    for key, position in position_index.items():
        initial = position.get("initial_quantity")
        residual = position.get("final_quantity")
        working = position.get("working_order_ids")
        opened = _time(position.get("opened_at"))
        reconciled = _time(position.get("reconciled_at"))
        if (
            position.get("namespace") != namespace
            or key.split(":", 1)[0] != namespace
            or not _one_of(position.get("direction"), {"BUY", "SELL"})
            or not _integer(initial, minimum=1)
            or not _integer(residual)
            or not isinstance(working, list)
            or any(not _text(item) for item in working)
            or opened is None
            or reconciled is None
            or capture_end is None
            or opened > reconciled
            or reconciled > capture_end
        ):
            fail("INVALID_POSITION_RECONCILIATION")
            unresolved_count += 1
            continue
        if key not in observed_positions:
            fail("POSITION_HAS_NO_RETAINED_DECISIONS")
        signed_residual = residual if position["direction"] == "BUY" else -residual
        fill_reconciled = (
            entry_fills[key] == initial and signed_fills[key] == signed_residual
        )
        if not fill_reconciled:
            fail("FILLS_DO_NOT_RECONCILE_TO_POSITION")
        if any(order_intents.get(item, (None,))[0] != key for item in working):
            fail("WORKING_ORDER_MISSING_INTENT_LINK")
        if (
            residual
            or working
            or key in unresolved_intent_positions
            or position.get("reconciliation_complete") is False
            or not fill_reconciled
        ):
            unresolved_count += 1
            item = censor_index.get(key, {})
            if item.get("quantity") != residual or not _text(item.get("reason")):
                fail("UNRESOLVED_POSITION_NOT_CENSORED")
        else:
            closed_count += 1
            if key in censor_index:
                fail("CENSOR_RECORD_FOR_RECONCILED_FLAT_POSITION")
    for decision_id, key in decision_positions.items():
        record = decision_index[decision_id]
        at = _time(record.get("occurred_at"))
        if at is None:
            continue
        state = record["trace"]["input_snapshot"]["state"]
        quantity_at_decision = sum(
            quantity if side == "BUY" else -quantity
            for received_at, side, quantity in position_fills.get(key, [])
            if received_at <= at
        )
        direction = position_index[key].get("direction")
        expected_quantity = quantity_at_decision * (1 if direction == "BUY" else -1)
        if state.get("known_quantity") != expected_quantity:
            fail("DECISION_QUANTITY_NOT_SUPPORTED_BY_OBSERVED_FILLS")
    if any(key not in position_index for key in censor_index):
        fail("CENSOR_POSITION_NOT_EXPORTED")
    unresolved_rate = unresolved_count / len(position_index) if position_index else None
    for actual, minimum, label in (
        (len(sessions), limits.minimum_sessions, "SESSIONS"),
        (len(decision_index), limits.minimum_decisions, "DECISIONS"),
        (actions["HOLD"], limits.minimum_hold_decisions, "HOLD_DECISIONS"),
        (closed_count, limits.minimum_closed_positions, "CLOSED_POSITIONS"),
    ):
        if actual < minimum:
            fail(f"INSUFFICIENT_{label}")
    if (
        unresolved_rate is None
        or unresolved_rate > limits.maximum_unresolved_position_rate
    ):
        fail("UNRESOLVED_POSITION_RATE_OUTSIDE_DECLARED_LIMIT")
    durable_history = report.get("durable_position_state_history", [])
    durable_recovery_events = 0
    durable_last_sequence = {}
    if not isinstance(durable_history, list):
        fail("INVALID_DURABLE_POSITION_HISTORY")
        durable_history = []
    for event in durable_history:
        if not isinstance(event, Mapping):
            fail("INVALID_DURABLE_POSITION_HISTORY")
            continue
        key = event.get("position_key")
        recorded = _time(event.get("recorded_at"))
        captured = _time(event.get("captured_at"))
        sequence = event.get("sequence")
        state = event.get("state")
        if (
            not _text(key)
            or key.split(":", 1)[0] != namespace
            or not _integer(sequence)
            or recorded is None
            or captured is None
            or capture_start is None
            or capture_end is None
            or not recorded <= captured <= capture_end
            or captured < capture_start
            or not isinstance(state, Mapping)
            or state.get("position_key") != key
        ):
            fail("INVALID_DURABLE_POSITION_HISTORY")
            continue
        if sequence <= durable_last_sequence.get(key, -1):
            fail("DURABLE_POSITION_HISTORY_SEQUENCE_REGRESSED")
        durable_last_sequence[key] = sequence
        if (
            state.get("exposure") == "RECOVERY_REQUIRED"
            or state.get("protection") == "FAILED_OR_UNKNOWN"
        ):
            durable_recovery_events += 1
    recovery_observation_count = 0
    for field, key_field, final_index in (
        ("position_observations", "position_key", position_index),
        ("intent_observations", "intent_id", intent_index),
    ):
        history = report.get(field, [])
        if not isinstance(history, list):
            fail("INVALID_OPERATIONAL_STATE_HISTORY")
            continue
        latest = {}
        for row in history:
            if not isinstance(row, Mapping):
                fail("INVALID_OPERATIONAL_STATE_HISTORY")
                continue
            at = _time(row.get("observed_at"))
            key = row.get(key_field)
            if (
                key not in final_index
                or at is None
                or capture_start is None
                or capture_end is None
                or not capture_start <= at <= capture_end
            ):
                fail("INVALID_OPERATIONAL_STATE_HISTORY")
                continue
            if key in latest and _time(latest[key]["observed_at"]) > at:
                fail("NONCHRONOLOGICAL_OPERATIONAL_STATE_HISTORY")
            latest[key] = row
            if (
                field == "position_observations"
                and row.get("reconciliation_complete") is False
            ):
                recovery_observation_count += 1
        for key, row in latest.items():
            if {k: v for k, v in row.items() if k != "observed_at"} != final_index[key]:
                fail("FINAL_STATE_DIFFERS_FROM_OBSERVATION_HISTORY")
    observations = {
        "unreconciled_position_observation_count": recovery_observation_count,
        "durable_recovery_event_count": durable_recovery_events,
        "durable_position_event_count": len(durable_history),
        "mode": mode,
        "namespace": namespace,
        "policy_fingerprints": sorted(policy_fingerprints),
        "acquisition_mode": provenance.get("acquisition_mode"),
        "source_classification": provenance.get("source_classification"),
        "session_count": len(sessions),
        "sessions": sorted(sessions),
        "decision_count": len(decision_index),
        "hold_decision_count": actions["HOLD"],
        "action_counts": dict(actions),
        "replay_verified_count": replay_count,
        "timing_verified_count": timing_count,
        "shadow_suppressed_count": shadow_suppressed_count,
        "maximum_observed_lag_seconds": max_lag,
        "position_count": len(position_index),
        "closed_position_count": closed_count,
        "unresolved_position_count": unresolved_count,
        "unresolved_position_rate": unresolved_rate,
        "intent_count": len(intent_index),
        "candidate_execution_decision_count": len(candidate_decisions),
        "unresolved_intent_position_count": len(unresolved_intent_positions),
        "fill_count": len(fill_index),
        "linked_fill_count": linked_fill_count,
        "censored_position_count": len(censor_index),
    }
    return {
        "schema_version": "operational-assessment-v1",
        "passed": not failures,
        "failures": failures,
        "report_sha256": report_hash,
        "limits": asdict(limits),
        "observations": observations,
        "verification_scope": "RETAINED_FACTS_WITH_EXTERNALLY_ATTESTED_PROVENANCE",
        "candidate_live_activation": "NOT_PERFORMED",
    }


def _cohort_slots(plan):
    if not isinstance(plan, Mapping) or not all(
        _text(plan.get(field))
        for field in ("study_id", "source_revision", "policy_artifact_hash")
    ):
        raise ValueError("operational cohort requires frozen study/source/policy")
    if not _one_of(plan.get("mode"), {"LIVE_SHADOW", "ISOLATED_PAPER"}):
        raise ValueError("operational cohort requires an explicit mode")
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("operational cohort requires predeclared slots")
    result = {}
    for slot in slots:
        if not isinstance(slot, Mapping) or not _text(slot.get("slot_id")):
            raise ValueError("invalid operational cohort slot")
        dates = slot.get("session_dates")
        if not isinstance(dates, list) or not dates:
            raise ValueError("operational slots require actual declared session dates")
        try:
            valid_dates = all(
                isinstance(value, str)
                and date.fromisoformat(value).isoformat() == value
                for value in dates
            )
        except (TypeError, ValueError):
            valid_dates = False
        if (
            not valid_dates
            or len(set(dates)) != len(dates)
            or slot["slot_id"] in result
        ):
            raise ValueError("invalid or duplicate operational cohort slot/date")
        result[slot["slot_id"]] = set(dates)
    return result


def _cohort_rows(report, field):
    values = report.get(field)
    if isinstance(values, list):
        yield from (row for row in values if isinstance(row, Mapping))


def _cohort_position_states(report):
    """Conservative unique-position closure counts, independent of ID reuse."""
    terminal = {"CLOSED", "COMPLETE", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
    unresolved = {
        row.get("position_key")
        for row in _cohort_rows(report, "intents")
        if not _one_of(row.get("status"), terminal) and _text(row.get("position_key"))
    }
    unresolved.update(
        row["position_key"]
        for row in _cohort_rows(report, "censored_positions")
        if _text(row.get("position_key"))
    )
    signed, bought, sold = Counter(), Counter(), Counter()
    for row in _cohort_rows(report, "fills"):
        if not _text(row.get("position_key")):
            continue
        quantity, side = row.get("quantity"), row.get("side")
        if not _integer(quantity, minimum=1) or not _one_of(side, {"BUY", "SELL"}):
            continue
        key = row["position_key"]
        signed[key] += quantity * (1 if side == "BUY" else -1)
        (bought if side == "BUY" else sold)[key] += quantity
    for row in _cohort_rows(report, "positions"):
        if not _text(row.get("position_key")):
            continue
        key = row["position_key"]
        direction = row.get("direction")
        initial, residual = row.get("initial_quantity"), row.get("final_quantity")
        closed = (
            key not in unresolved
            and _one_of(direction, {"BUY", "SELL"})
            and _integer(initial, minimum=1)
            and _integer(residual)
            and residual == 0
            and row.get("working_order_ids") == []
            and row.get("reconciliation_complete") is not False
            and (bought if direction == "BUY" else sold)[key] == initial
            and signed[key] == 0
        )
        yield key, closed


def assess_operational_cohort(
    reports_by_attempt: Mapping[str, Mapping[str, Any]],
    limits: OperationalLimits | Mapping[str, Any],
    cohort_plan: Mapping[str, Any],
    claims: list[Mapping[str, Any]],
    *,
    expected_policy: Any = None,
) -> dict[str, Any]:
    """Assess every preclaimed capture without rewriting its replay identities.

    The acceptance store must create immutable claims before capture and provide
    its complete claim inventory. Pending, failed and unimported attempts cannot
    be replaced by selecting another report. A lazy report mapping is supported:
    only one full report is materialized at a time. PAPER identities are scoped
    to their actual capture; LIVE account identities cannot be counted twice.
    """
    slots = _cohort_slots(cohort_plan)
    if isinstance(limits, Mapping):
        limits = OperationalLimits(**dict(limits))
    if not isinstance(limits, OperationalLimits):
        raise TypeError("operational cohort requires declared limits")
    if not isinstance(reports_by_attempt, Mapping) or not isinstance(claims, list):
        raise TypeError("operational cohort requires report mapping and claim list")
    failures = []

    def fail(reason):
        if reason not in failures:
            failures.append(reason)

    claim_index = {}
    for claim in claims:
        if not isinstance(claim, Mapping) or not _text(claim.get("attempt_id")):
            fail("INVALID_OPERATIONAL_CAPTURE_CLAIM")
            continue
        attempt = claim["attempt_id"]
        if attempt in claim_index:
            fail("DUPLICATE_OPERATIONAL_CAPTURE_CLAIM")
            continue
        claim_index[attempt] = claim
        if (
            not _text(claim.get("slot_id"))
            or claim.get("slot_id") not in slots
            or _time(claim.get("claimed_at")) is None
        ):
            fail(f"INVALID_OPERATIONAL_CAPTURE_CLAIM:{attempt}")
    for slot in slots:
        if not any(claim.get("slot_id") == slot for claim in claim_index.values()):
            fail(f"UNCLAIMED_OPERATIONAL_SLOT:{slot}")
    report_attempts = set(reports_by_attempt)
    for attempt in claim_index:
        if attempt not in report_attempts:
            fail(f"MISSING_CLAIMED_OPERATIONAL_CAPTURE:{attempt}")
    for attempt in report_attempts:
        if attempt not in claim_index:
            fail(f"UNCLAIMED_OPERATIONAL_CAPTURE:{attempt}")

    sample_failures = {
        "INSUFFICIENT_SESSIONS",
        "INSUFFICIENT_DECISIONS",
        "INSUFFICIENT_HOLD_DECISIONS",
        "INSUFFICIENT_CLOSED_POSITIONS",
    }
    mode = cohort_plan["mode"]
    sessions, capture_ids, policy_fingerprints = set(), set(), set()
    slot_sessions = {slot: set() for slot in slots}
    classifications, acquisition_modes = set(), set()
    intervals, assessments, report_hashes = [], {}, {}
    identities = {
        name: {} for name in ("position", "decision", "intent", "fill", "order")
    }
    closed_positions, held_decisions, candidate_decisions, censored_positions = (
        {},
        set(),
        set(),
        set(),
    )
    actions, totals = Counter(), Counter()
    maximum_lag = 0.0

    def identity(kind, value, capture_id, attempt, position_key=None):
        if not _text(value):
            return None
        scope = (
            capture_id
            if mode == "ISOLATED_PAPER"
            else (
                ":".join(position_key.split(":")[:2]) if _text(position_key) else "LIVE"
            )
        )
        key = (scope, value)
        previous = identities[kind].get(key)
        if previous is not None and previous != attempt:
            fail(f"DUPLICATE_OPERATIONAL_{kind.upper()}_IDENTITY")
        identities[kind].setdefault(key, attempt)
        return key

    expected = serialize_replay_artifact(expected_policy)
    if (
        expected is not None
        and promotion_artifact_hash(expected) != cohort_plan["policy_artifact_hash"]
    ):
        fail("COHORT_POLICY_DIFFERS_FROM_PREDECLARED_POLICY")
    for attempt in sorted(reports_by_attempt, key=str):
        report = reports_by_attempt[attempt]
        if not isinstance(report, Mapping):
            fail(f"INVALID_OPERATIONAL_CAPTURE:{attempt}")
            continue
        assessed = assess_operational_run(
            report, limits, expected_policy=expected_policy
        )
        assessments[attempt] = assessed
        report_hashes[attempt] = assessed["report_sha256"]
        for reason in assessed["failures"]:
            if reason not in sample_failures:
                fail(f"CAPTURE_FAILED:{attempt}:{reason}")
        observation = assessed["observations"]
        provenance = report.get("provenance", {})
        provenance = provenance if isinstance(provenance, Mapping) else {}
        claim = claim_index.get(attempt, {})
        slot = claim.get("slot_id")
        if (
            report.get("study_id") != cohort_plan["study_id"]
            or report.get("mode") != mode
            or report.get("policy_artifact_hash") != cohort_plan["policy_artifact_hash"]
            or provenance.get("source_revision") != cohort_plan["source_revision"]
            or (
                provenance.get("runtime_source_tree_sha256") is not None
                and provenance.get("runtime_source_tree_sha256")
                != cohort_plan["source_revision"]
            )
        ):
            fail(f"CAPTURE_IDENTITY_DIFFERS_FROM_COHORT:{attempt}")
        if (
            provenance.get("capture_slot") != slot
            or provenance.get("capture_attempt_id") != attempt
        ):
            fail(f"CAPTURE_DOES_NOT_MATCH_CLAIM:{attempt}")
        start, end = (
            _time(provenance.get("captured_started_at")),
            _time(provenance.get("captured_ended_at")),
        )
        claimed = _time(claim.get("claimed_at"))
        if claimed is None or start is None or claimed > start:
            fail(f"CAPTURE_WAS_NOT_CLAIMED_BEFORE_OBSERVATION:{attempt}")
        if start is not None and end is not None:
            intervals.append((start, end, attempt))
        capture_id = provenance.get("capture_artifact_ref")
        if not _text(capture_id):
            capture_id = f"invalid:{attempt}"
        if capture_id in capture_ids:
            fail("DUPLICATE_OPERATIONAL_CAPTURE_IDENTITY")
        capture_ids.add(capture_id)
        observed_sessions = set(observation["sessions"])
        sessions.update(observed_sessions)
        if _text(slot) and slot in slots:
            slot_sessions[slot].update(observed_sessions)
            if not observed_sessions <= slots[slot]:
                fail(f"CAPTURE_OUTSIDE_DECLARED_SESSION_DATES:{attempt}")
        policy_fingerprints.update(observation["policy_fingerprints"])
        classifications.add(str(provenance.get("source_classification")))
        acquisition_modes.add(str(provenance.get("acquisition_mode")))
        maximum_lag = max(maximum_lag, observation["maximum_observed_lag_seconds"])
        for key in (
            "replay_verified_count",
            "timing_verified_count",
            "shadow_suppressed_count",
            "linked_fill_count",
            "unreconciled_position_observation_count",
            "durable_recovery_event_count",
            "durable_position_event_count",
        ):
            totals[key] += observation[key]
        for row in _cohort_rows(report, "recorded_decisions"):
            decision_id = row.get("decision_id")
            existing = len(identities["decision"])
            key = identity("decision", decision_id, capture_id, attempt)
            if key is not None and len(identities["decision"]) > existing:
                actions[str(row.get("action"))] += 1
                if row.get("action") == "HOLD":
                    held_decisions.add(key)
        for key, closed in _cohort_position_states(report):
            scoped = identity("position", key, capture_id, attempt, key)
            closed_positions[scoped] = closed_positions.get(scoped, True) and closed
        for row in _cohort_rows(report, "intents"):
            key = row.get("position_key")
            identity("intent", row.get("intent_id"), capture_id, attempt, key)
            for order_id in (
                row["order_ids"] if isinstance(row.get("order_ids"), list) else ()
            ):
                identity("order", order_id, capture_id, attempt, key)
            if row.get("origin") == "CANDIDATE":
                for decision_id in (
                    row["decision_ids"]
                    if isinstance(row.get("decision_ids"), list)
                    else ()
                ):
                    if _text(decision_id):
                        candidate_decisions.add(
                            (
                                capture_id if mode == "ISOLATED_PAPER" else "LIVE",
                                decision_id,
                            )
                        )
        for row in _cohort_rows(report, "fills"):
            identity(
                "fill",
                row.get("fill_id"),
                capture_id,
                attempt,
                row.get("position_key"),
            )
        for row in _cohort_rows(report, "censored_positions"):
            if _text(row.get("position_key")):
                scope = (
                    capture_id
                    if mode == "ISOLATED_PAPER"
                    else ":".join(row["position_key"].split(":")[:2])
                )
                censored_positions.add((scope, row["position_key"]))
        # A lazy mapping may load a large report on each access. Do not retain
        # any input row/trace reference across attempts.
        del report

    previous_end = None
    for start, end, _ in sorted(intervals):
        if previous_end is not None and start < previous_end:
            fail("OVERLAPPING_OPERATIONAL_CAPTURES")
        previous_end = max(previous_end, end) if previous_end is not None else end
    for slot, expected_sessions in slots.items():
        if slot_sessions[slot] != expected_sessions:
            fail(f"OPERATIONAL_SLOT_SESSION_COVERAGE_INCOMPLETE:{slot}")
    position_count = len(closed_positions)
    closed_count = sum(closed_positions.values())
    unresolved_count = position_count - closed_count
    unresolved_rate = unresolved_count / position_count if position_count else None
    for actual, minimum, name in (
        (len(sessions), limits.minimum_sessions, "SESSIONS"),
        (len(identities["decision"]), limits.minimum_decisions, "DECISIONS"),
        (len(held_decisions), limits.minimum_hold_decisions, "HOLD_DECISIONS"),
        (closed_count, limits.minimum_closed_positions, "CLOSED_POSITIONS"),
    ):
        if actual < minimum:
            fail(f"INSUFFICIENT_{name}")
    if (
        unresolved_rate is None
        or unresolved_rate > limits.maximum_unresolved_position_rate
    ):
        fail("UNRESOLVED_POSITION_RATE_OUTSIDE_DECLARED_LIMIT")
    try:
        cohort_hash = promotion_artifact_hash(
            {
                "cohort_plan": dict(cohort_plan),
                "claims": sorted(claims, key=lambda item: str(item.get("attempt_id")))
                if all(isinstance(item, Mapping) for item in claims)
                else claims,
                "report_hashes": report_hashes,
            }
        )
    except (TypeError, ValueError, OverflowError):
        cohort_hash = None
        fail("COHORT_IS_NOT_FINITE_JSON")
    return {
        "schema_version": "operational-assessment-v1",
        "aggregation": "PRECLAIMED_OPERATIONAL_COHORT_V1",
        "passed": not failures,
        "failures": failures,
        "report_sha256": cohort_hash,
        "limits": asdict(limits),
        "observations": {
            **totals,
            "mode": mode,
            "namespace": "PAPER" if mode == "ISOLATED_PAPER" else "LIVE",
            "policy_fingerprints": sorted(policy_fingerprints),
            "acquisition_mode": next(iter(acquisition_modes))
            if len(acquisition_modes) == 1
            else "MIXED_OR_MISSING",
            "source_classification": next(iter(classifications))
            if len(classifications) == 1
            else "MIXED_OR_MISSING",
            "capture_count": len(reports_by_attempt),
            "claimed_capture_count": len(claim_index),
            "required_slot_count": len(slots),
            "session_count": len(sessions),
            "sessions": sorted(sessions),
            "decision_count": len(identities["decision"]),
            "hold_decision_count": len(held_decisions),
            "action_counts": dict(actions),
            "replay_verified_count": min(
                totals["replay_verified_count"], len(identities["decision"])
            ),
            "timing_verified_count": min(
                totals["timing_verified_count"], len(identities["decision"])
            ),
            "maximum_observed_lag_seconds": maximum_lag,
            "position_count": position_count,
            "closed_position_count": closed_count,
            "unresolved_position_count": unresolved_count,
            "unresolved_position_rate": unresolved_rate,
            "intent_count": len(identities["intent"]),
            "candidate_execution_decision_count": len(candidate_decisions),
            "fill_count": len(identities["fill"]),
            "linked_fill_count": min(
                totals["linked_fill_count"], len(identities["fill"])
            ),
            "censored_position_count": len(censored_positions),
        },
        "capture_assessments": assessments,
        "verification_scope": "RETAINED_FACTS_WITH_EXTERNALLY_ATTESTED_PROVENANCE",
        "candidate_live_activation": "NOT_PERFORMED",
    }
