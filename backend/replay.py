"""Shared, side-effect-free candidate-exit replay adapter.

Live shadow orchestration, paper trading, the historical simulator and scenario
tests have different clocks and execution assumptions.  They must not have
different thesis rules. This module feeds research facts into the same
:func:`evaluate_exit` used by live orchestration.

It imports neither the live scanner nor the journal/calibrator, which makes it
safe to use for counterfactual and offline research runs.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, tzinfo
from enum import Enum
from types import UnionType
from typing import Any, Optional, Sequence, Union, get_args, get_origin, get_type_hints

from .exit_management.engine import ExitEvaluation, ExitPolicy, evaluate_exit
from .exit_management.evidence import EvidenceObservation, EvidenceReport
from .exit_management.models import (
    LifecycleEvent,
    ManagementState,
    PositionState,
    ProtectionState,
    reduce_lifecycle,
)
from .exit_management.thesis import EntryThesis
from .market_context import MarketContext
from .risk_rules import HardRiskSnapshot
from .time_utils import as_utc


class ReplayMode(str, Enum):
    """Adapter labels only; none changes candidate decision semantics."""

    LIVE_SHADOW = "LIVE_SHADOW"
    PAPER = "PAPER"
    BACKTEST = "BACKTEST"
    SCENARIO = "SCENARIO"
    REPLAY = "REPLAY"


class ReplayEventKind(str, Enum):
    """Fact cadence labels retained by adapters without changing policy rules."""

    COMPLETED_BAR = "COMPLETED_BAR"
    HARD_RISK = "HARD_RISK"
    SESSION_DEADLINE = "SESSION_DEADLINE"
    BROKER_RECONCILIATION = "BROKER_RECONCILIATION"


@dataclass(frozen=True)
class ReplayLifecycleEvent:
    """A recorded broker fact, never an acknowledgement inferred to be a fill."""

    event: LifecycleEvent | str
    event_id: str
    occurred_at: datetime
    known_quantity: Optional[int] = None
    protection: Optional[ProtectionState | str] = None
    confirmed_stop: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event", LifecycleEvent(self.event))
        if (
            not self.event_id
            or not isinstance(self.occurred_at, datetime)
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("replay lifecycle facts require identity and aware time")
        if self.protection is not None:
            object.__setattr__(self, "protection", ProtectionState(self.protection))
        if self.confirmed_stop is not None and (
            isinstance(self.confirmed_stop, bool)
            or not isinstance(self.confirmed_stop, (int, float))
            or not math.isfinite(self.confirmed_stop)
            or self.confirmed_stop <= 0
            or self.protection is not ProtectionState.ACTIVE
        ):
            raise ValueError("confirmed stop requires a positive verified active price")


@dataclass(frozen=True)
class ExitReplayEvent:
    """One ordered, already-available set of candidate-policy facts."""

    market_context: Optional[MarketContext]
    risk_snapshot: HardRiskSnapshot
    observed_mark_price: Optional[float] = None
    observed_mark_time: Optional[datetime] = None
    label: Optional[str] = None
    kind: ReplayEventKind | str = ReplayEventKind.COMPLETED_BAR
    lifecycle_events: tuple[ReplayLifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ReplayEventKind(self.kind))
        object.__setattr__(self, "lifecycle_events", tuple(self.lifecycle_events))
        if not isinstance(self.risk_snapshot, HardRiskSnapshot):
            raise TypeError("replay requires a typed hard-risk snapshot")
        if (
            not isinstance(self.risk_snapshot.session.observed_at, datetime)
            or self.risk_snapshot.session.observed_at.utcoffset() is None
        ):
            raise ValueError("replay events require an aware risk clock")
        if self.kind is not ReplayEventKind.COMPLETED_BAR and self.market_context:
            raise ValueError("only a completed-bar event can carry normal context")


@dataclass(frozen=True)
class ExitReplayResult:
    """The exact policy transitions; execution remains an adapter concern."""

    mode: ReplayMode
    position_state: PositionState
    management_state: ManagementState
    evaluations: tuple[ExitEvaluation, ...]
    events: tuple[ExitReplayEvent, ...]
    state_hash: str

    @property
    def decisions(self):
        return tuple(item.decision for item in self.evaluations)


def serialize_replay_artifact(value: Any) -> Any:
    """Detach typed replay inputs into deterministic JSON-compatible values."""
    if hasattr(value, "to_dict"):
        return serialize_replay_artifact(value.to_dict())
    if hasattr(value, "value"):
        return serialize_replay_artifact(value.value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, tzinfo):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(
            (serialize_replay_artifact(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, Mapping):
        return {
            str(key): serialize_replay_artifact(item) for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [serialize_replay_artifact(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: serialize_replay_artifact(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value


def _state_hash(
    state: PositionState,
    management: ManagementState,
    evaluations: Sequence[ExitEvaluation],
) -> str:
    payload = {
        "position_state": state.to_dict(),
        "management_state": management.to_dict(),
        "decisions": [item.decision.to_dict() for item in evaluations],
        "intents": [
            item.proposed_intent.to_dict() if item.proposed_intent else None
            for item in evaluations
        ],
    }
    return hashlib.sha256(
        json.dumps(
            serialize_replay_artifact(payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def replay_exit_decisions(
    *,
    thesis: Optional[EntryThesis],
    position_state: PositionState,
    management_state: Optional[ManagementState],
    policy: ExitPolicy,
    events: Sequence[ExitReplayEvent],
    mode: ReplayMode | str = ReplayMode.REPLAY,
) -> ExitReplayResult:
    """Run ordered observations through the one candidate transition function.

    The mode is intentionally only retained in the result.  A new condition in
    one adapter must be represented as a different market/risk event, never as
    a mode branch inside the policy.  This makes same-input parity a direct
    byte-for-byte decision/state-hash assertion.
    """

    replay_mode = ReplayMode(mode)
    state = position_state
    management = management_state or ManagementState()
    evaluations: list[ExitEvaluation] = []
    events = tuple(events)
    previous_at = as_utc(state.last_transition_at)
    seen_facts: dict[str, ReplayLifecycleEvent] = {}
    for index, event in enumerate(events):
        if not isinstance(event, ExitReplayEvent):
            raise TypeError(f"replay event {index} is not an ExitReplayEvent")
        if event.risk_snapshot.position_key not in (None, state.position_key):
            raise ValueError("replay risk snapshot belongs to another position")
        event_at = as_utc(event.risk_snapshot.session.observed_at)
        if previous_at is not None and event_at < previous_at:
            raise ValueError("replay events must be in causal clock order")
        context = event.market_context
        if context is not None and (
            context.decision_event_time > event_at
            or context.received_at > event_at
            or context.source_as_of > event_at
        ):
            raise ValueError("replay context was not available at its risk event")
        fact_at = previous_at
        for fact in event.lifecycle_events:
            if not isinstance(fact, ReplayLifecycleEvent):
                raise TypeError("replay lifecycle facts must be typed")
            if fact.event_id in seen_facts:
                if seen_facts[fact.event_id] != fact:
                    raise ValueError("conflicting replay lifecycle event identity")
                continue
            occurred_at = as_utc(fact.occurred_at)
            if occurred_at > event_at or (fact_at and occurred_at < fact_at):
                raise ValueError("replay lifecycle facts must precede evaluation")
            state = reduce_lifecycle(
                state,
                fact.event,
                event_id=fact.event_id,
                occurred_at=fact.occurred_at,
                known_quantity=fact.known_quantity,
                protection=fact.protection,
            )
            if fact.confirmed_stop is not None:
                if thesis is None:
                    raise ValueError(
                        "stop replay needs the recorded position direction"
                    )
                floor = management.confirmed_stop or thesis.initial_stop
                if floor is not None and (
                    fact.confirmed_stop < floor
                    if thesis.direction == "BUY"
                    else fact.confirmed_stop > floor
                ):
                    raise ValueError("replay cannot loosen confirmed protection")
                management = replace(
                    management,
                    confirmed_stop=fact.confirmed_stop,
                    requested_stop=None,
                )
            seen_facts[fact.event_id] = fact
            fact_at = occurred_at
        evaluation = evaluate_exit(
            thesis,
            state,
            event.market_context,
            event.risk_snapshot,
            policy,
            management_state=management,
            observed_mark_price=event.observed_mark_price,
            observed_mark_time=event.observed_mark_time,
        )
        evaluations.append(evaluation)
        state = evaluation.next_position_state
        management = evaluation.next_management_state
        previous_at = event_at
    return ExitReplayResult(
        mode=replay_mode,
        position_state=state,
        management_state=management,
        evaluations=tuple(evaluations),
        events=tuple(events),
        state_hash=_state_hash(state, management, evaluations),
    )


def assert_identical_exit_parity(
    results: Sequence[ExitReplayResult],
) -> ExitReplayResult:
    """Return the shared result or raise a concise parity-contract failure."""

    if not results:
        raise ValueError("at least one replay result is required")
    expected = results[0]
    for actual in results[1:]:
        if actual.state_hash != expected.state_hash:
            raise AssertionError(
                "candidate exit parity failed: "
                f"{expected.mode.value}={expected.state_hash}, "
                f"{actual.mode.value}={actual.state_hash}"
            )
    return expected


def _restore(value: Any, annotation: Any) -> Any:
    """Restore only declared domain field types, with no imports from payloads."""

    if value is None or annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        options = [item for item in args if item is not type(None)]
        if len(options) != 1:
            raise ValueError("ambiguous retained replay field type")
        return _restore(value, options[0])
    if origin in (tuple, list):
        return origin(_restore(item, args[0]) for item in value)
    if origin in (dict, Mapping):
        return {key: _restore(item, args[1]) for key, item in value.items()}
    if annotation in (datetime, date):
        result = annotation.fromisoformat(value)
        if annotation is datetime and result.utcoffset() is None:
            raise ValueError("retained replay timestamps must be aware")
        return result
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        allowed = {item.name for item in fields(annotation)}
        if set(value) - allowed:
            raise ValueError(f"unknown retained {annotation.__name__} fields")
        return annotation(
            **{name: _restore(item, hints[name]) for name, item in value.items()}
        )
    return value


def replay_recorded_exit_decision(record: Mapping[str, Any]) -> ExitEvaluation:
    """Reproduce one retained live/research decision and verify every pure output.

    A copy of a journal decision payload is sufficient. No live journal, current
    config, calibration, wall clock or broker is consulted. Orchestration-only
    annotations may be added by live persistence; all pure trace fields must
    still reproduce, including the original input hash and state transitions.
    """

    trace = record["trace"]
    if trace.get("schema_version") != 1:
        raise ValueError("unsupported retained exit trace schema")
    inputs = trace["input_snapshot"]
    evidence = inputs.get("supplied_evidence")
    if isinstance(evidence, Mapping):
        evidence = _restore(evidence, EvidenceReport)
    elif evidence is not None:
        evidence = tuple(_restore(item, EvidenceObservation) for item in evidence)
    evaluation = evaluate_exit(
        EntryThesis.from_dict(inputs["thesis"]) if inputs["thesis"] else None,
        PositionState.from_dict(inputs["state"]),
        _restore(inputs["context"], MarketContext),
        _restore(inputs["risk"], HardRiskSnapshot),
        _restore(inputs["policy"]["policy"], ExitPolicy),
        management_state=ManagementState.from_dict(inputs["management"]),
        observed_mark_price=inputs.get("observed_mark_price"),
        observed_mark_time=_restore(inputs.get("observed_mark_time"), datetime),
        evidence=evidence,
    )
    actual = evaluation.decision.to_dict()
    for key, value in actual.items():
        if key == "trace":
            for field_name, expected_value in value.items():
                if serialize_replay_artifact(
                    trace.get(field_name)
                ) != serialize_replay_artifact(expected_value):
                    raise AssertionError(f"retained exit trace mismatch: {field_name}")
        elif (
            key == "urgency"
            and key not in record
            and trace.get("orchestration", {}).get("phase") == "phase7_shadow"
        ):
            # Phase-7 DecisionRecord predates top-level urgency. The retained
            # proposed intent and management urgency are both verified above.
            continue
        elif serialize_replay_artifact(record.get(key)) != serialize_replay_artifact(
            value
        ):
            raise AssertionError(f"retained exit decision mismatch: {key}")
    return evaluation


def replay_alternative_exit_policy(
    *,
    thesis: Optional[EntryThesis],
    position_state: PositionState,
    management_state: Optional[ManagementState],
    original_policy: ExitPolicy,
    policy: ExitPolicy,
    events: Sequence[ExitReplayEvent],
) -> ExitReplayResult:
    """Branch a fixed-entry study under a new policy with frozen hard risk.

    Start before normal management has run: historical confirmation counters
    depend on the old thresholds and cannot be transplanted to another policy.
    Existing verified protection and latched obligations remain authoritative.
    Actual counterfactual fills must be supplied by an isolated execution driver;
    this function does not invent executions from a decision or broker ack.
    """

    if (
        original_policy.hard_risk_policy != policy.hard_risk_policy
        or original_policy.tick_size != policy.tick_size
    ):
        raise ValueError("alternative exit replay must preserve hard risk and ticks")
    management = management_state or ManagementState()
    if (
        management.eligible_completed_bars
        or management.last_processed_primary_bar_id is not None
    ):
        raise ValueError(
            "alternative policy replay must start before normal management"
        )
    return replay_exit_decisions(
        thesis=thesis,
        position_state=position_state,
        management_state=replace(management, policy_fingerprint=None),
        policy=policy,
        events=events,
    )
