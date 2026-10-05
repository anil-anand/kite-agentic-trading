"""Immutable, serializable state contracts for managed positions.

These objects are intentionally independent from the broker, database and
normal-exit policy.  They make lifecycle changes explicit and reject surprising
transitions instead of repairing an in-memory dictionary silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from backend.time_utils import as_utc

EXIT_MANAGEMENT_SCHEMA_VERSION = "exit-management-state-v1"


class ExposureState(str, Enum):
    NEW = "NEW"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    FLAT_PENDING_RECONCILIATION = "FLAT_PENDING_RECONCILIATION"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CLOSED = "CLOSED"
    ENTRY_ABORTED = "ENTRY_ABORTED"


class ThesisHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    VALID = "VALID"
    WEAKENING = "WEAKENING"
    INVALIDATED = "INVALIDATED"


class DevelopmentPhase(str, Enum):
    EARLY = "EARLY"
    DEVELOPING = "DEVELOPING"
    FAVORABLE = "FAVORABLE"
    PULLBACK = "PULLBACK"
    CONSOLIDATING = "CONSOLIDATING"


class ProtectionState(str, Enum):
    UNCONFIRMED = "UNCONFIRMED"
    ACTIVE = "ACTIVE"
    UPDATE_PENDING = "UPDATE_PENDING"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    FAILED_OR_UNKNOWN = "FAILED_OR_UNKNOWN"
    NONE_FLAT = "NONE_FLAT"


class LifecycleEvent(str, Enum):
    STATE_OBSERVED = "STATE_OBSERVED"
    ENTRY_INTENT_COMMITTED = "ENTRY_INTENT_COMMITTED"
    ENTRY_UPDATE = "ENTRY_UPDATE"
    ENTRY_TERMINAL_PROTECTED = "ENTRY_TERMINAL_PROTECTED"
    ENTRY_ABORTED = "ENTRY_ABORTED"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_UPDATE = "EXIT_UPDATE"
    FLAT_OBSERVED = "FLAT_OBSERVED"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RECONCILED_ENTRY_PENDING = "RECONCILED_ENTRY_PENDING"
    RECONCILED_OPEN = "RECONCILED_OPEN"
    RECONCILED_EXIT_PENDING = "RECONCILED_EXIT_PENDING"
    RECONCILED_FLAT = "RECONCILED_FLAT"


class LifecycleTransitionError(ValueError):
    """An event would violate the documented exposure state machine."""


def _freeze(value: Any) -> Any:
    """Freeze JSON-shaped input without retaining caller-owned containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def thaw(value: Any) -> Any:
    """Return a detached JSON-compatible representation for persistence."""

    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class EvidenceObservation:
    """The exact entry evidence selected by a playbook, not a later re-scan."""

    family: str
    dependency_group: str
    direction: str
    strategy_id: Optional[str]
    score: Optional[float]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "dependency_group": self.dependency_group,
            "direction": self.direction,
            "strategy_id": self.strategy_id,
            "score": self.score,
            "payload": thaw(self.payload),
        }


@dataclass(frozen=True)
class CausalInputReference:
    """As-of identity of the market data that was available to the entry."""

    decision_at: Optional[str]
    primary_bar_id: Optional[str]
    higher_bar_id: Optional[str]
    context_policy_version: Optional[str]
    source_as_of: Optional[str]
    context_hash: Optional[str]
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot", _freeze(self.snapshot))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_at": self.decision_at,
            "primary_bar_id": self.primary_bar_id,
            "higher_bar_id": self.higher_bar_id,
            "context_policy_version": self.context_policy_version,
            "source_as_of": self.source_as_of,
            "context_hash": self.context_hash,
            "snapshot": thaw(self.snapshot),
        }


@dataclass(frozen=True)
class PolicySnapshot:
    """Effective non-secret configuration pinned at entry."""

    policy_version: str
    config_hash: str
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze(self.values))

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "config_hash": self.config_hash,
            "values": thaw(self.values),
        }


@dataclass(frozen=True)
class ManagementProfileSnapshot:
    """Pinned management identity; missing premises explicitly stay bounded."""

    name: str = "unknown_legacy_bounded"
    version: str = "entry-management-profile-v1"
    structure_status: str = "MISSING"
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise ValueError("management profile values must be a mapping")
        object.__setattr__(self, "values", _freeze(self.values))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "structure_status": self.structure_status,
            "values": thaw(self.values),
        }


@dataclass(frozen=True)
class PositionState:
    """Orthogonal lifecycle axes for one broker-position epoch."""

    position_key: str
    exposure: ExposureState = ExposureState.NEW
    thesis_health: ThesisHealth = ThesisHealth.UNKNOWN
    development: DevelopmentPhase = DevelopmentPhase.EARLY
    protection: ProtectionState = ProtectionState.UNCONFIRMED
    version: int = 0
    last_event_id: Optional[str] = None
    latched_exit_intent_id: Optional[str] = None
    known_quantity: Optional[int] = None
    last_transition_at: Optional[str] = None
    recovery_from: Optional[ExposureState] = None
    had_fills: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "exposure", ExposureState(self.exposure))
        object.__setattr__(self, "thesis_health", ThesisHealth(self.thesis_health))
        object.__setattr__(self, "development", DevelopmentPhase(self.development))
        object.__setattr__(self, "protection", ProtectionState(self.protection))
        if not isinstance(self.position_key, str) or not self.position_key:
            raise ValueError("position_key is required")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 0
        ):
            raise ValueError("state version must be a nonnegative integer")
        if not isinstance(self.had_fills, bool):
            raise ValueError("had_fills must be a boolean")
        if self.known_quantity is not None and (
            isinstance(self.known_quantity, bool)
            or not isinstance(self.known_quantity, int)
            or self.known_quantity < 0
        ):
            raise ValueError("known quantity must be a nonnegative integer")
        if self.recovery_from is not None:
            object.__setattr__(self, "recovery_from", ExposureState(self.recovery_from))
        if self.known_quantity:
            object.__setattr__(self, "had_fills", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "exposure": self.exposure.value,
            "thesis_health": self.thesis_health.value,
            "development": self.development.value,
            "protection": self.protection.value,
            "version": self.version,
            "last_event_id": self.last_event_id,
            "latched_exit_intent_id": self.latched_exit_intent_id,
            "known_quantity": self.known_quantity,
            "last_transition_at": self.last_transition_at,
            "recovery_from": self.recovery_from.value if self.recovery_from else None,
            "had_fills": self.had_fills,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PositionState":
        state = cls(
            position_key=payload["position_key"],
            exposure=ExposureState(payload["exposure"]),
            thesis_health=ThesisHealth(payload["thesis_health"]),
            development=DevelopmentPhase(payload["development"]),
            protection=ProtectionState(payload["protection"]),
            version=payload["version"],
            last_event_id=payload["last_event_id"],
            latched_exit_intent_id=payload["latched_exit_intent_id"],
            known_quantity=payload["known_quantity"],
            last_transition_at=payload["last_transition_at"],
            recovery_from=payload.get("recovery_from"),
            had_fills=payload.get("had_fills", False),
        )
        if state.exposure in {ExposureState.OPEN, ExposureState.EXIT_PENDING}:
            if not state.known_quantity:
                raise ValueError(
                    "restored open exposure requires known positive quantity"
                )
        if state.exposure is ExposureState.OPEN and (
            state.latched_exit_intent_id
            or state.protection
            not in {ProtectionState.ACTIVE, ProtectionState.UPDATE_PENDING}
        ):
            raise ValueError(
                "restored OPEN state has an exit obligation or lacks protection"
            )
        if (
            state.exposure is ExposureState.EXIT_PENDING
            and not state.latched_exit_intent_id
        ):
            raise ValueError("restored EXIT_PENDING requires a latched exit obligation")
        if (
            state.exposure
            in {
                ExposureState.FLAT_PENDING_RECONCILIATION,
                ExposureState.CLOSED,
                ExposureState.ENTRY_ABORTED,
            }
            and state.known_quantity != 0
        ):
            raise ValueError("restored flat state requires known zero quantity")
        if state.exposure in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}:
            if state.protection is not ProtectionState.NONE_FLAT:
                raise ValueError("restored terminal state requires order cleanup")
            if state.exposure is ExposureState.ENTRY_ABORTED and state.had_fills:
                raise ValueError("restored ENTRY_ABORTED cannot have fills")
        if state.thesis_health is ThesisHealth.INVALIDATED and (
            not state.latched_exit_intent_id
            or state.exposure in {ExposureState.OPEN, ExposureState.ENTRY_PENDING}
        ):
            raise ValueError("restored invalidation requires a latched exit obligation")
        return state


_ALLOWED_EXPOSURE_TRANSITIONS = {
    ExposureState.NEW: {
        LifecycleEvent.ENTRY_INTENT_COMMITTED: ExposureState.ENTRY_PENDING,
        LifecycleEvent.ENTRY_ABORTED: ExposureState.ENTRY_ABORTED,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.ENTRY_PENDING: {
        LifecycleEvent.ENTRY_UPDATE: ExposureState.ENTRY_PENDING,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED: ExposureState.OPEN,
        LifecycleEvent.EXIT_REQUESTED: ExposureState.EXIT_PENDING,
        LifecycleEvent.ENTRY_ABORTED: ExposureState.ENTRY_ABORTED,
        LifecycleEvent.FLAT_OBSERVED: ExposureState.FLAT_PENDING_RECONCILIATION,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.OPEN: {
        LifecycleEvent.ENTRY_UPDATE: ExposureState.OPEN,
        LifecycleEvent.EXIT_REQUESTED: ExposureState.EXIT_PENDING,
        LifecycleEvent.FLAT_OBSERVED: ExposureState.FLAT_PENDING_RECONCILIATION,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.EXIT_PENDING: {
        LifecycleEvent.EXIT_UPDATE: ExposureState.EXIT_PENDING,
        LifecycleEvent.EXIT_REQUESTED: ExposureState.EXIT_PENDING,
        LifecycleEvent.FLAT_OBSERVED: ExposureState.FLAT_PENDING_RECONCILIATION,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.FLAT_PENDING_RECONCILIATION: {
        LifecycleEvent.FLAT_CONFIRMED: ExposureState.CLOSED,
        LifecycleEvent.EXIT_REQUESTED: ExposureState.EXIT_PENDING,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.RECOVERY_REQUIRED: {
        LifecycleEvent.RECONCILED_ENTRY_PENDING: ExposureState.ENTRY_PENDING,
        LifecycleEvent.RECONCILED_OPEN: ExposureState.OPEN,
        LifecycleEvent.RECONCILED_EXIT_PENDING: ExposureState.EXIT_PENDING,
        LifecycleEvent.RECONCILED_FLAT: ExposureState.FLAT_PENDING_RECONCILIATION,
        LifecycleEvent.ENTRY_ABORTED: ExposureState.ENTRY_ABORTED,
        LifecycleEvent.RECONCILIATION_REQUIRED: ExposureState.RECOVERY_REQUIRED,
    },
    ExposureState.CLOSED: {},
    ExposureState.ENTRY_ABORTED: {},
}

for _exposure in (
    ExposureState.NEW,
    ExposureState.ENTRY_PENDING,
    ExposureState.OPEN,
    ExposureState.EXIT_PENDING,
    ExposureState.FLAT_PENDING_RECONCILIATION,
    ExposureState.RECOVERY_REQUIRED,
):
    _ALLOWED_EXPOSURE_TRANSITIONS[_exposure][LifecycleEvent.STATE_OBSERVED] = _exposure

_PROTECTION_TRANSITIONS = {
    ProtectionState.UNCONFIRMED: {
        ProtectionState.ACTIVE,
        ProtectionState.FAILED_OR_UNKNOWN,
        ProtectionState.NONE_FLAT,
    },
    ProtectionState.ACTIVE: {
        ProtectionState.UPDATE_PENDING,
        ProtectionState.HANDOFF_PENDING,
        ProtectionState.FAILED_OR_UNKNOWN,
        ProtectionState.NONE_FLAT,
    },
    ProtectionState.UPDATE_PENDING: {
        ProtectionState.ACTIVE,
        ProtectionState.HANDOFF_PENDING,
        ProtectionState.FAILED_OR_UNKNOWN,
        ProtectionState.NONE_FLAT,
    },
    ProtectionState.HANDOFF_PENDING: {
        ProtectionState.ACTIVE,
        ProtectionState.FAILED_OR_UNKNOWN,
        ProtectionState.NONE_FLAT,
    },
    ProtectionState.FAILED_OR_UNKNOWN: {
        ProtectionState.ACTIVE,
        ProtectionState.HANDOFF_PENDING,
        ProtectionState.NONE_FLAT,
    },
    ProtectionState.NONE_FLAT: set(),
}


def reduce_lifecycle(
    state: PositionState,
    event: LifecycleEvent | str,
    *,
    event_id: str,
    occurred_at: datetime | str,
    known_quantity: Optional[int] = None,
    protection: Optional[ProtectionState | str] = None,
    thesis_health: Optional[ThesisHealth | str] = None,
    development: Optional[DevelopmentPhase | str] = None,
    exit_intent_id: Optional[str] = None,
) -> PositionState:
    """Apply a documented lifecycle edge, or an idempotent duplicate event."""

    event = LifecycleEvent(event)
    if not event_id:
        raise LifecycleTransitionError("lifecycle events require a stable event id")
    if state.last_event_id == event_id:
        return state
    target = _ALLOWED_EXPOSURE_TRANSITIONS[state.exposure].get(event)
    if target is None:
        raise LifecycleTransitionError(
            f"{event.value} is not allowed from {state.exposure.value}"
        )
    effective_quantity = (
        known_quantity if known_quantity is not None else state.known_quantity
    )
    effective_intent = exit_intent_id or state.latched_exit_intent_id
    if (
        exit_intent_id
        and state.latched_exit_intent_id
        and exit_intent_id != state.latched_exit_intent_id
    ):
        raise LifecycleTransitionError("a latched exit intent cannot be replaced")
    if target is ExposureState.EXIT_PENDING and not effective_intent:
        raise LifecycleTransitionError("EXIT_PENDING requires a latched exit intent")
    if effective_intent and target in {ExposureState.OPEN, ExposureState.ENTRY_PENDING}:
        raise LifecycleTransitionError("a latched exit cannot be cleared by recovery")

    normalized_protection = (
        ProtectionState(protection) if protection is not None else state.protection
    )
    normalized_health = (
        ThesisHealth(thesis_health)
        if thesis_health is not None
        else state.thesis_health
    )
    normalized_development = (
        DevelopmentPhase(development) if development is not None else state.development
    )
    if target is ExposureState.OPEN:
        allowed_protection = {ProtectionState.ACTIVE}
        if state.exposure is ExposureState.OPEN:
            # While a tighter stop is being acknowledged, the previous verified
            # order remains effective; an ordinary update must not erase it.
            allowed_protection.add(ProtectionState.UPDATE_PENDING)
        if normalized_protection not in allowed_protection:
            raise LifecycleTransitionError("OPEN requires confirmed active protection")
    if target in {ExposureState.OPEN, ExposureState.EXIT_PENDING} and (
        effective_quantity is None or effective_quantity <= 0
    ):
        raise LifecycleTransitionError("open exposure requires known positive quantity")
    if (
        target
        in {
            ExposureState.FLAT_PENDING_RECONCILIATION,
            ExposureState.CLOSED,
            ExposureState.ENTRY_ABORTED,
        }
        and effective_quantity != 0
    ):
        raise LifecycleTransitionError("flat transitions require known zero quantity")
    if target in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}:
        if normalized_protection is not ProtectionState.NONE_FLAT:
            raise LifecycleTransitionError("terminal flat state requires order cleanup")
        if target is ExposureState.ENTRY_ABORTED and state.had_fills:
            raise LifecycleTransitionError("a filled entry cannot be ENTRY_ABORTED")
    if normalized_protection is not state.protection:
        late_fill = (
            state.protection is ProtectionState.NONE_FLAT
            and normalized_protection is ProtectionState.UNCONFIRMED
            and state.exposure
            in {
                ExposureState.FLAT_PENDING_RECONCILIATION,
                ExposureState.RECOVERY_REQUIRED,
            }
            and effective_quantity is not None
            and effective_quantity > 0
        )
        if (
            not late_fill
            and normalized_protection not in _PROTECTION_TRANSITIONS[state.protection]
        ):
            raise LifecycleTransitionError("undocumented protection transition")
    if normalized_protection is ProtectionState.NONE_FLAT and effective_quantity != 0:
        raise LifecycleTransitionError("NONE_FLAT requires known zero quantity")
    if normalized_health is ThesisHealth.UNKNOWN and state.thesis_health in {
        ThesisHealth.VALID,
        ThesisHealth.WEAKENING,
    }:
        raise LifecycleTransitionError("missing data cannot erase a known thesis")
    if state.thesis_health is ThesisHealth.UNKNOWN and normalized_health in {
        ThesisHealth.WEAKENING,
        ThesisHealth.INVALIDATED,
    }:
        raise LifecycleTransitionError("unknown thesis requires validation first")
    if state.thesis_health is ThesisHealth.INVALIDATED:
        normalized_health = ThesisHealth.INVALIDATED
    if normalized_health is ThesisHealth.INVALIDATED and (
        not effective_intent
        or target in {ExposureState.OPEN, ExposureState.ENTRY_PENDING}
    ):
        raise LifecycleTransitionError(
            "invalidated thesis requires a simultaneous latched exit obligation"
        )
    if (
        state.development is not DevelopmentPhase.EARLY
        and normalized_development is DevelopmentPhase.EARLY
    ):
        raise LifecycleTransitionError("development cannot return to EARLY")
    if normalized_health is ThesisHealth.INVALIDATED:
        normalized_development = state.development
    timestamp = as_utc(occurred_at)
    if timestamp is None:
        raise LifecycleTransitionError("lifecycle events require an aware timestamp")
    return replace(
        state,
        exposure=target,
        thesis_health=normalized_health,
        development=normalized_development,
        protection=normalized_protection,
        version=state.version + 1,
        last_event_id=event_id,
        last_transition_at=timestamp.isoformat(),
        known_quantity=effective_quantity,
        latched_exit_intent_id=effective_intent,
        recovery_from=(
            state.recovery_from
            if state.exposure is ExposureState.RECOVERY_REQUIRED
            else state.exposure
        )
        if target is ExposureState.RECOVERY_REQUIRED
        else None,
    )


@dataclass(frozen=True)
class PositionCheckpoint:
    """Replay checkpoint distinct from the immutable entry thesis."""

    position_key: str
    state_version: int
    sequence: int
    state: PositionState
    counters: Mapping[str, Any] = field(default_factory=dict)
    extrema: Mapping[str, Any] = field(default_factory=dict)
    intents: Mapping[str, Any] = field(default_factory=dict)
    protection: Mapping[str, Any] = field(default_factory=dict)
    input_references: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.position_key != self.state.position_key:
            raise ValueError("checkpoint and state position keys differ")
        if self.state_version != self.state.version:
            raise ValueError("checkpoint state version must match its state")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("checkpoint sequence must be a nonnegative integer")
        for field_name in (
            "counters",
            "extrema",
            "intents",
            "protection",
            "input_references",
        ):
            object.__setattr__(self, field_name, _freeze(getattr(self, field_name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "state_version": self.state_version,
            "sequence": self.sequence,
            "state": self.state.to_dict(),
            "counters": thaw(self.counters),
            "extrema": thaw(self.extrema),
            "intents": thaw(self.intents),
            "protection": thaw(self.protection),
            "input_references": thaw(self.input_references),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PositionCheckpoint":
        state = PositionState.from_dict(payload["state"])
        return cls(
            position_key=payload["position_key"],
            state_version=payload["state_version"],
            sequence=payload["sequence"],
            state=state,
            counters=payload.get("counters", {}),
            extrema=payload.get("extrema", {}),
            intents=payload.get("intents", {}),
            protection=payload.get("protection", {}),
            input_references=payload.get("input_references", {}),
        )


@dataclass(frozen=True)
class DecisionRecord:
    """Structured decision trace storage contract; phase 6 supplies policy data."""

    decision_id: str
    position_key: str
    occurred_at: str
    action: str
    primary_reason_code: str
    policy_version: str
    state_before: PositionState
    state_after: PositionState
    input_references: Mapping[str, Any] = field(default_factory=dict)
    supporting_evidence: tuple[EvidenceObservation, ...] = ()
    opposing_evidence: tuple[EvidenceObservation, ...] = ()
    contributing_reason_codes: tuple[str, ...] = ()
    suppressed_candidates: tuple[str, ...] = ()
    trace: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_before.position_key != self.position_key:
            raise ValueError("decision before-state position key differs")
        if self.state_after.position_key != self.position_key:
            raise ValueError("decision after-state position key differs")
        object.__setattr__(self, "input_references", _freeze(self.input_references))
        object.__setattr__(self, "trace", _freeze(self.trace))
        for field_name in (
            "supporting_evidence",
            "opposing_evidence",
            "contributing_reason_codes",
            "suppressed_candidates",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "position_key": self.position_key,
            "occurred_at": self.occurred_at,
            "action": self.action,
            "primary_reason_code": self.primary_reason_code,
            "policy_version": self.policy_version,
            "state_before": self.state_before.to_dict(),
            "state_after": self.state_after.to_dict(),
            "input_references": thaw(self.input_references),
            "supporting_evidence": [
                item.to_dict() for item in self.supporting_evidence
            ],
            "opposing_evidence": [item.to_dict() for item in self.opposing_evidence],
            "contributing_reason_codes": list(self.contributing_reason_codes),
            "suppressed_candidates": list(self.suppressed_candidates),
            "trace": thaw(self.trace),
        }


class ExitAction(str, Enum):
    """A broker-independent action proposed by the deterministic policy."""

    HOLD = "HOLD"
    REQUEST_EXIT = "REQUEST_EXIT"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    MANAGE_PENDING_INTENT = "MANAGE_PENDING_INTENT"


class ExitIntentType(str, Enum):
    """The small set of mutations the policy may propose, never execute."""

    EXIT = "EXIT"
    FLATTEN = "FLATTEN"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    RECONCILE = "RECONCILE"


class ExitReasonCode(str, Enum):
    """Stable phase-6 reason-code vocabulary from EXIT_REASON_CODES.md."""

    RISK_CATASTROPHIC_STOP = "RISK_CATASTROPHIC_STOP"
    RISK_PROTECTIVE_STOP_TRIGGERED = "RISK_PROTECTIVE_STOP_TRIGGERED"
    RISK_DAILY_LOSS = "RISK_DAILY_LOSS"
    RISK_PROTECTION_FAILURE = "RISK_PROTECTION_FAILURE"
    RISK_ENTRY_BUDGET_EXCEEDED = "RISK_ENTRY_BUDGET_EXCEEDED"
    RISK_DATA_BLINDNESS = "RISK_DATA_BLINDNESS"
    SESSION_FORCED_FLAT = "SESSION_FORCED_FLAT"
    OPERATOR_EMERGENCY_FLATTEN = "OPERATOR_EMERGENCY_FLATTEN"
    OPERATOR_POSITION_CLOSE = "OPERATOR_POSITION_CLOSE"
    EXEC_UNRECOVERABLE_FAILURE = "EXEC_UNRECOVERABLE_FAILURE"
    EXEC_BROKER_STATE_UNKNOWN = "EXEC_BROKER_STATE_UNKNOWN"
    THESIS_STRUCTURE_ACCEPTANCE_FAILED = "THESIS_STRUCTURE_ACCEPTANCE_FAILED"
    THESIS_BREAKOUT_FAILED = "THESIS_BREAKOUT_FAILED"
    THESIS_VWAP_ACCEPTANCE_FAILED = "THESIS_VWAP_ACCEPTANCE_FAILED"
    THESIS_MULTI_FAMILY_FAILURE = "THESIS_MULTI_FAMILY_FAILURE"
    THESIS_REGIME_INCOMPATIBLE = "THESIS_REGIME_INCOMPATIBLE"
    PROFIT_FIXED_OBJECTIVE_REACHED = "PROFIT_FIXED_OBJECTIVE_REACHED"
    PROFIT_CONVERGENCE_OBJECTIVE = "PROFIT_CONVERGENCE_OBJECTIVE"
    PROFIT_REVERSAL_CONFIRMED = "PROFIT_REVERSAL_CONFIRMED"
    PROFIT_STRUCTURE_TRAIL = "PROFIT_STRUCTURE_TRAIL"
    PROFIT_COST_AWARE_PROTECTION = "PROFIT_COST_AWARE_PROTECTION"
    TIME_NO_PROGRESS_CONFIRMED = "TIME_NO_PROGRESS_CONFIRMED"
    SESSION_LATE_MANAGEMENT = "SESSION_LATE_MANAGEMENT"
    HOLD_THESIS_VALID = "HOLD_THESIS_VALID"
    HOLD_EARLY_DEVELOPMENT = "HOLD_EARLY_DEVELOPMENT"
    HOLD_HEALTHY_PULLBACK = "HOLD_HEALTHY_PULLBACK"
    HOLD_CONSOLIDATION = "HOLD_CONSOLIDATION"
    HOLD_TREND_CONTINUATION = "HOLD_TREND_CONTINUATION"
    HOLD_THESIS_WEAKENING = "HOLD_THESIS_WEAKENING"
    HOLD_CONFIRMATION_PENDING = "HOLD_CONFIRMATION_PENDING"
    HOLD_HIGHER_TIMEFRAME_SUPPORT = "HOLD_HIGHER_TIMEFRAME_SUPPORT"
    HOLD_OBJECTIVE_REVIEW_ZONE = "HOLD_OBJECTIVE_REVIEW_ZONE"
    HOLD_NO_VALID_STOP_IMPROVEMENT = "HOLD_NO_VALID_STOP_IMPROVEMENT"
    HOLD_UNKNOWN_THESIS = "HOLD_UNKNOWN_THESIS"
    HOLD_DATA_DEGRADED = "HOLD_DATA_DEGRADED"
    DATA_INCOMPLETE_CANDLE = "DATA_INCOMPLETE_CANDLE"
    DATA_DUPLICATE_BAR = "DATA_DUPLICATE_BAR"
    DATA_STALE_CONTEXT = "DATA_STALE_CONTEXT"
    DATA_GAP_OR_INVALID_OHLCV = "DATA_GAP_OR_INVALID_OHLCV"
    DATA_HIGHER_TIMEFRAME_UNAVAILABLE = "DATA_HIGHER_TIMEFRAME_UNAVAILABLE"
    EXEC_INTENT_ALREADY_PENDING = "EXEC_INTENT_ALREADY_PENDING"
    EXEC_SUBMISSION_UNKNOWN = "EXEC_SUBMISSION_UNKNOWN"
    EXEC_CANCEL_UNKNOWN = "EXEC_CANCEL_UNKNOWN"
    EXEC_PARTIAL_FILL = "EXEC_PARTIAL_FILL"
    EXEC_ORDER_REJECTED = "EXEC_ORDER_REJECTED"
    EXEC_PROTECTION_UPDATE_PENDING = "EXEC_PROTECTION_UPDATE_PENDING"
    EXEC_EXTERNAL_POSITION_CHANGE = "EXEC_EXTERNAL_POSITION_CHANGE"
    EXEC_FILL_ATTRIBUTION_PENDING = "EXEC_FILL_ATTRIBUTION_PENDING"
    BROKER_STOP_FILLED = "BROKER_STOP_FILLED"
    BROKER_APP_EXIT_FILLED = "BROKER_APP_EXIT_FILLED"
    BROKER_EXTERNAL_CLOSE = "BROKER_EXTERNAL_CLOSE"
    RESEARCH_END_OF_DATA = "RESEARCH_END_OF_DATA"
    LEGACY_REASON_UNRESOLVED = "LEGACY_REASON_UNRESOLVED"


@dataclass(frozen=True)
class ManagementState:
    """Replayable policy memory kept separately from broker lifecycle state.

    The generic phase-5 checkpoint already has ``counters`` and ``extrema``
    storage.  This typed projection makes the phase-6 decision memory explicit
    without making a fill, acknowledgement, or poll look like a candle vote.
    """

    last_processed_primary_bar_id: Optional[str] = None
    last_processed_primary_bar_end: Optional[str] = None
    policy_fingerprint: Optional[str] = None
    latched_exit_reason_code: Optional[str] = None
    latched_exit_urgency: Optional[str] = None
    failure_episode: Optional[str] = None
    failure_count: int = 0
    last_failure_bar_end: Optional[str] = None
    local_failure_episode: Optional[str] = None
    local_failure_count: int = 0
    last_local_failure_bar_end: Optional[str] = None
    weakening_count: int = 0
    last_weakening_bar_end: Optional[str] = None
    recovery_count: int = 0
    last_recovery_bar_end: Optional[str] = None
    stagnation_count: int = 0
    last_stagnation_bar_end: Optional[str] = None
    eligible_completed_bars: int = 0
    last_favorable_progress_bar_id: Optional[str] = None
    last_favorable_progress_bar_count: Optional[int] = None
    last_close_progress_bar_count: Optional[int] = None
    observed_mfe_r: Optional[float] = None
    observed_mae_r: Optional[float] = None
    completed_mfe_r: Optional[float] = None
    completed_mae_r: Optional[float] = None
    observed_mfe_at: Optional[str] = None
    observed_mae_at: Optional[str] = None
    completed_mfe_at: Optional[str] = None
    completed_mae_at: Optional[str] = None
    observed_extrema_source: Optional[str] = None
    progress_close_r: Optional[float] = None
    favorable_structure_id: Optional[str] = None
    favorable_structure_price: Optional[float] = None
    favorable_structure_buffer: Optional[float] = None
    favorable_structure_failure_buffer: Optional[float] = None
    favorable_structure_known_at: Optional[str] = None
    confirmed_stop: Optional[float] = None
    requested_stop: Optional[float] = None

    def __post_init__(self) -> None:
        if self.latched_exit_urgency is not None and (
            not isinstance(self.latched_exit_urgency, str)
            or self.latched_exit_urgency not in {"NORMAL", "CRITICAL"}
        ):
            raise ValueError("latched exit urgency must be NORMAL or CRITICAL")
        for name in (
            "last_processed_primary_bar_id",
            "policy_fingerprint",
            "latched_exit_reason_code",
            "failure_episode",
            "local_failure_episode",
            "last_favorable_progress_bar_id",
            "observed_extrema_source",
            "favorable_structure_id",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty text when present")
        for name in (
            "failure_count",
            "local_failure_count",
            "weakening_count",
            "recovery_count",
            "stagnation_count",
            "eligible_completed_bars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "last_favorable_progress_bar_count",
            "last_close_progress_bar_count",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "observed_mfe_r",
            "observed_mae_r",
            "completed_mfe_r",
            "completed_mae_r",
            "progress_close_r",
            "favorable_structure_price",
            "favorable_structure_buffer",
            "favorable_structure_failure_buffer",
            "confirmed_stop",
            "requested_stop",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite when present")
        # MFE and MAE are magnitudes, not signed returns.  Keep the management
        # checkpoint on the same unit convention as research/accounting traces.
        for name in (
            "observed_mfe_r",
            "observed_mae_r",
            "completed_mfe_r",
            "completed_mae_r",
            "favorable_structure_buffer",
            "favorable_structure_failure_buffer",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be nonnegative when present")
        for name in ("confirmed_stop", "requested_stop", "favorable_structure_price"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when present")
        for name in (
            "last_processed_primary_bar_end",
            "last_failure_bar_end",
            "last_local_failure_bar_end",
            "last_weakening_bar_end",
            "last_recovery_bar_end",
            "last_stagnation_bar_end",
            "observed_mfe_at",
            "observed_mae_at",
            "completed_mfe_at",
            "completed_mae_at",
            "favorable_structure_known_at",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{name} must be an aware ISO timestamp")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{name} must be an aware ISO timestamp") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"{name} must be an aware ISO timestamp")
            object.__setattr__(self, name, as_utc(parsed).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_processed_primary_bar_id": self.last_processed_primary_bar_id,
            "last_processed_primary_bar_end": self.last_processed_primary_bar_end,
            "policy_fingerprint": self.policy_fingerprint,
            "latched_exit_reason_code": self.latched_exit_reason_code,
            "latched_exit_urgency": self.latched_exit_urgency,
            "failure_episode": self.failure_episode,
            "failure_count": self.failure_count,
            "last_failure_bar_end": self.last_failure_bar_end,
            "local_failure_episode": self.local_failure_episode,
            "local_failure_count": self.local_failure_count,
            "last_local_failure_bar_end": self.last_local_failure_bar_end,
            "weakening_count": self.weakening_count,
            "last_weakening_bar_end": self.last_weakening_bar_end,
            "recovery_count": self.recovery_count,
            "last_recovery_bar_end": self.last_recovery_bar_end,
            "stagnation_count": self.stagnation_count,
            "last_stagnation_bar_end": self.last_stagnation_bar_end,
            "eligible_completed_bars": self.eligible_completed_bars,
            "last_favorable_progress_bar_id": self.last_favorable_progress_bar_id,
            "last_favorable_progress_bar_count": self.last_favorable_progress_bar_count,
            "last_close_progress_bar_count": self.last_close_progress_bar_count,
            "observed_mfe_r": self.observed_mfe_r,
            "observed_mae_r": self.observed_mae_r,
            "completed_mfe_r": self.completed_mfe_r,
            "completed_mae_r": self.completed_mae_r,
            "observed_mfe_at": self.observed_mfe_at,
            "observed_mae_at": self.observed_mae_at,
            "completed_mfe_at": self.completed_mfe_at,
            "completed_mae_at": self.completed_mae_at,
            "observed_extrema_source": self.observed_extrema_source,
            "progress_close_r": self.progress_close_r,
            "favorable_structure_id": self.favorable_structure_id,
            "favorable_structure_price": self.favorable_structure_price,
            "favorable_structure_buffer": self.favorable_structure_buffer,
            "favorable_structure_failure_buffer": self.favorable_structure_failure_buffer,
            "favorable_structure_known_at": self.favorable_structure_known_at,
            "confirmed_stop": self.confirmed_stop,
            "requested_stop": self.requested_stop,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ManagementState":
        return cls(**dict(payload))


@dataclass(frozen=True)
class ProposedIntent:
    """A serializable proposal for the lifecycle coordinator."""

    intent_type: ExitIntentType
    position_key: str
    reason_code: str
    intent_id: Optional[str] = None
    urgency: str = "NORMAL"
    quantity: Optional[int] = None
    stop_price: Optional[float] = None
    parent_intent_id: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_type", ExitIntentType(self.intent_type))
        if not isinstance(self.position_key, str) or not self.position_key:
            raise ValueError("proposed intent needs a position key")
        if self.quantity is not None and (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity <= 0
        ):
            raise ValueError("proposed quantity must be a positive integer")
        if self.stop_price is not None and (
            isinstance(self.stop_price, bool)
            or not isinstance(self.stop_price, (int, float))
            or not math.isfinite(float(self.stop_price))
            or self.stop_price <= 0
        ):
            raise ValueError("proposed stop must be a finite positive price")
        object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type.value,
            "position_key": self.position_key,
            "reason_code": self.reason_code,
            "intent_id": self.intent_id,
            "urgency": self.urgency,
            "quantity": self.quantity,
            "stop_price": self.stop_price,
            "parent_intent_id": self.parent_intent_id,
            "details": thaw(self.details),
        }


@dataclass(frozen=True)
class ExitDecision:
    """Complete, deterministic explanation for one policy evaluation."""

    decision_id: str
    action: ExitAction
    primary_reason_code: str
    policy_version: str
    occurred_at: str
    supporting_evidence: tuple[Any, ...] = ()
    opposing_evidence: tuple[Any, ...] = ()
    contributing_reason_codes: tuple[str, ...] = ()
    suppressed_candidates: tuple[str, ...] = ()
    trace: Mapping[str, Any] = field(default_factory=dict)
    urgency: str = "NORMAL"

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ExitAction(self.action))
        if not isinstance(self.urgency, str) or self.urgency not in {
            "NORMAL",
            "CRITICAL",
        }:
            raise ValueError("decision urgency must be NORMAL or CRITICAL")
        for name in (
            "supporting_evidence",
            "opposing_evidence",
            "contributing_reason_codes",
            "suppressed_candidates",
        ):
            object.__setattr__(
                self, name, tuple(_freeze(item) for item in getattr(self, name))
            )
        object.__setattr__(self, "trace", _freeze(self.trace))

    def to_dict(self) -> dict[str, Any]:
        def serialize(item: Any) -> Any:
            return item.to_dict() if hasattr(item, "to_dict") else thaw(item)

        return {
            "decision_id": self.decision_id,
            "action": self.action.value,
            "urgency": self.urgency,
            "primary_reason_code": self.primary_reason_code,
            "policy_version": self.policy_version,
            "occurred_at": self.occurred_at,
            "supporting_evidence": [
                serialize(item) for item in self.supporting_evidence
            ],
            "opposing_evidence": [serialize(item) for item in self.opposing_evidence],
            "contributing_reason_codes": list(self.contributing_reason_codes),
            "suppressed_candidates": list(self.suppressed_candidates),
            "trace": thaw(self.trace),
        }
