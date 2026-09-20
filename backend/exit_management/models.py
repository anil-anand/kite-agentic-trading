"""Immutable, serializable state contracts for managed positions.

These objects are intentionally independent from the broker, database and
normal-exit policy.  They make lifecycle changes explicit and reject surprising
transitions instead of repairing an in-memory dictionary silently.
"""

from __future__ import annotations

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
