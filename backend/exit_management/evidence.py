"""Evidence observations and deterministic, dependency-aware predicates.

An observation is deliberately richer than an indicator boolean.  In particular,
missing values stay ``UNKNOWN`` and several oscillators describing the same move
cannot manufacture independent confirmation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from ta.volatility import AverageTrueRange

from backend.market_context import (
    KnownLevel,
    MarketContext,
    OHLCVBar,
    _contiguous_same_session,
    _known_structure,
)
from backend.time_utils import EXCHANGE_TIMEZONE, as_utc

from .models import ManagementState, _freeze, thaw
from .profiles import ManagementProfile, ManagementProfileName
from .thesis import EntryThesis


class EvidenceFamily(str, Enum):
    STRUCTURE = "STRUCTURE"
    VALUE = "VALUE"
    DYNAMICS = "DYNAMICS"
    PARTICIPATION = "PARTICIPATION"
    VOLATILITY = "VOLATILITY"
    HIGHER_TIMEFRAME = "HIGHER_TIMEFRAME"
    REGIME = "REGIME"
    TRADE_CONTEXT = "TRADE_CONTEXT"


class EvidenceDirection(str, Enum):
    SUPPORTING = "SUPPORTING"
    OPPOSING = "OPPOSING"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class EvidenceSeverity(str, Enum):
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"
    WATCH = "WATCH"
    MATERIAL = "MATERIAL"
    DECISIVE = "DECISIVE"

    @property
    def rank(self) -> int:
        return {
            EvidenceSeverity.UNKNOWN: -1,
            EvidenceSeverity.NONE: 0,
            EvidenceSeverity.WATCH: 1,
            EvidenceSeverity.MATERIAL: 2,
            EvidenceSeverity.DECISIVE: 3,
        }[self]


@dataclass(frozen=True)
class EvidenceObservation:
    """One reproducible observation relative to the frozen entry thesis."""

    observation_id: str
    family: EvidenceFamily
    dependency_group: str
    direction: EvidenceDirection
    severity: EvidenceSeverity
    predicate: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    source_bar_ids: tuple[str, ...] = ()
    known_at: Optional[str] = None
    freshness: Optional[str] = None
    quality: str = "VALID"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", EvidenceFamily(self.family))
        object.__setattr__(self, "direction", EvidenceDirection(self.direction))
        object.__setattr__(self, "severity", EvidenceSeverity(self.severity))
        if any(
            not isinstance(value, str) or not value
            for value in (self.observation_id, self.dependency_group, self.predicate)
        ):
            raise ValueError(
                "evidence identity, dependency group and predicate are required"
            )
        for name in ("value", "threshold"):
            current = getattr(self, name)
            if current is not None and (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not math.isfinite(float(current))
            ):
                raise ValueError(f"{name} must be finite when present")
        object.__setattr__(self, "source_bar_ids", tuple(self.source_bar_ids))
        if any(not isinstance(item, str) or not item for item in self.source_bar_ids):
            raise ValueError("source bar identities must be nonempty strings")
        if len(set(self.source_bar_ids)) != len(self.source_bar_ids):
            raise ValueError("source bar identities must be unique")
        if self.known_at is not None:
            try:
                timestamp = datetime.fromisoformat(self.known_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("timestamp is naive")
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("known_at must be an aware ISO timestamp") from exc
            object.__setattr__(self, "known_at", as_utc(timestamp).isoformat())
        object.__setattr__(self, "details", _freeze(self.details))

    @property
    def usable(self) -> bool:
        return self.quality == "VALID" and self.freshness not in {
            "STALE",
            "UNKNOWN",
            "UNAVAILABLE",
            "INVALID",
            "GAP",
            "INCOMPLETE",
        }

    @property
    def materially_opposing(self) -> bool:
        return (
            self.usable
            and self.direction is EvidenceDirection.OPPOSING
            and self.severity.rank >= EvidenceSeverity.MATERIAL.rank
        )

    @property
    def materially_supporting(self) -> bool:
        return (
            self.usable
            and self.direction is EvidenceDirection.SUPPORTING
            and self.severity.rank >= EvidenceSeverity.MATERIAL.rank
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "family": self.family.value,
            "dependency_group": self.dependency_group,
            "direction": self.direction.value,
            "severity": self.severity.value,
            "predicate": self.predicate,
            "value": self.value,
            "threshold": self.threshold,
            "source_bar_ids": list(self.source_bar_ids),
            "known_at": self.known_at,
            "freshness": self.freshness,
            "quality": self.quality,
            "details": thaw(self.details),
        }


@dataclass(frozen=True)
class EvidenceReport:
    """Raw and dependency-collapsed observations plus predicate outcomes."""

    observations: tuple[EvidenceObservation, ...]
    collapsed: tuple[EvidenceObservation, ...]
    predicates: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(
            self, "collapsed", collapse_dependency_groups(self.observations)
        )
        object.__setattr__(self, "predicates", _freeze(self.predicates))

    @property
    def supporting(self) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item
            for item in self.collapsed
            if item.usable and item.direction is EvidenceDirection.SUPPORTING
        )

    @property
    def opposing(self) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item
            for item in self.collapsed
            if item.usable and item.direction is EvidenceDirection.OPPOSING
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "collapsed": [item.to_dict() for item in self.collapsed],
            "predicates": thaw(self.predicates),
        }


def collapse_dependency_groups(
    observations: Iterable[EvidenceObservation],
) -> tuple[EvidenceObservation, ...]:
    """Keep one strongest observation for each economic dependency group.

    An adverse tie wins deliberately: a trace must not suppress a real warning
    because an alias of the same input says neutral.  It still counts only once.
    Stable sorting makes the result reproducible regardless of caller ordering.
    """

    grouped: dict[str, list[EvidenceObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.dependency_group, []).append(observation)

    def priority(item: EvidenceObservation) -> tuple[bool, int, int, bool, str]:
        direction_rank = {
            EvidenceDirection.OPPOSING: 3,
            EvidenceDirection.SUPPORTING: 2,
            EvidenceDirection.NEUTRAL: 1,
            EvidenceDirection.UNKNOWN: 0,
        }[item.direction]
        # A value alias of an explicit price premise must not become a fresh
        # corroborator merely because its observation ID sorts after the premise.
        return (
            item.usable,
            item.severity.rank,
            direction_rank,
            item.family is EvidenceFamily.STRUCTURE,
            item.observation_id,
        )

    return tuple(max(grouped[group], key=priority) for group in sorted(grouped))


def evidence_report(
    observations: Sequence[EvidenceObservation],
    *,
    predicates: Optional[Mapping[str, Any]] = None,
) -> EvidenceReport:
    return EvidenceReport(
        observations=tuple(observations),
        collapsed=collapse_dependency_groups(observations),
        predicates=predicates or {},
    )


def material_opposing_families(
    report: EvidenceReport,
) -> frozenset[EvidenceFamily]:
    return frozenset(
        item.family for item in report.collapsed if item.materially_opposing
    )


def has_independent_corroborator(report: EvidenceReport) -> bool:
    """Implement Route B's nonduplicated value/dynamics/participation check."""

    price = [
        item
        for item in report.collapsed
        if item.family is EvidenceFamily.STRUCTURE and item.materially_opposing
    ]
    corroborators = [
        item
        for item in report.collapsed
        if item.family
        in {
            EvidenceFamily.VALUE,
            EvidenceFamily.DYNAMICS,
            EvidenceFamily.PARTICIPATION,
        }
        and item.materially_opposing
        and all(item.dependency_group != source.dependency_group for source in price)
    ]
    return bool(price and corroborators)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _direction(thesis: EntryThesis) -> float:
    return 1.0 if thesis.direction == "BUY" else -1.0


def _entry_boundary(
    thesis: EntryThesis, profile: ManagementProfile
) -> tuple[Optional[float], Optional[str]]:
    boundary = thesis.management_profile.values.get("entry_boundary")
    if not isinstance(boundary, Mapping):
        return None, None
    if str(boundary.get("kind", "")).upper() in {"VWAP", "SESSION_VWAP"}:
        value = _finite(boundary.get("price")) or _finite(
            thesis.causal_anchors.get("session_vwap")
        )
        return value, str(boundary.get("level_id") or "session_vwap_premise")
    if profile.name is ManagementProfileName.BREAKOUT_FOLLOW_THROUGH:
        field = "high" if thesis.direction == "BUY" else "low"
    elif profile.name is ManagementProfileName.RANGE_CONVERGENCE:
        field = "low" if thesis.direction == "BUY" else "high"
    else:
        field = "price"
    value = _finite(boundary.get(field))
    identifier = boundary.get("level_id") or boundary.get("id") or field
    return value, str(identifier) if value is not None else None


def _buffer(atr: Any, multiple: float, minimum_ticks: int, tick_size: float):
    atr = _finite(atr)
    tick = _finite(tick_size)
    if atr is None or atr <= 0 or tick is None or tick <= 0:
        return None
    return max(minimum_ticks * tick, multiple * atr)


def entry_noise_buffer(
    thesis: EntryThesis, profile: ManagementProfile, tick_size: float
) -> Optional[float]:
    """Entry premise tolerance cannot expand with an adverse volatility shock."""

    volatility = thesis.causal_anchors.get("volatility")
    if not isinstance(volatility, Mapping):
        return None
    quality = volatility.get("quality")
    if quality is not None and quality != "VALID":
        return None
    return _buffer(
        volatility.get("atr"),
        profile.boundary_buffer_atr_multiple,
        profile.minimum_buffer_ticks,
        tick_size,
    )


def _confirmation_buffer(
    level: KnownLevel,
    context: MarketContext,
    profile: ManagementProfile,
    tick_size: float,
    *,
    trail: bool = False,
) -> Optional[float]:
    """Reconstruct the existing ATR definition at the level's availability time."""

    if not _causal_level(level, context):
        return None
    indices = [
        index
        for index, bar in enumerate(context.primary_bars)
        if bar.available_at <= level.known_at
    ]
    if len(indices) < 14 or indices != list(range(indices[0], indices[-1] + 1)):
        return None
    # The current primary source validates session gaps, but a historical bar
    # may only have arrived after this level became known. Do not bridge that
    # unavailable history when reconstructing the confirmation-time ATR.
    frame = context.primary_frame().iloc[indices]
    atr = (
        AverageTrueRange(
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            window=14,
        )
        .average_true_range()
        .iloc[-1]
    )
    return _buffer(
        float(atr),
        profile.structural_trail_buffer_atr_multiple
        if trail
        else profile.boundary_buffer_atr_multiple,
        profile.minimum_buffer_ticks,
        tick_size,
    )


def _aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _causal_level(level: KnownLevel, context: MarketContext) -> bool:
    """A level must be backed by bars actually available at its known-at time."""

    if (
        not _aware_datetime(level.formed_at)
        or not _aware_datetime(level.known_at)
        or not level.formed_at <= level.known_at <= context.decision_event_time
        or _finite(level.price) is None
        or level.price <= 0
        or not level.source_bar_ids
    ):
        return False
    sources = {bar.bar_id: bar for bar in (*context.primary_bars, *context.higher_bars)}
    return all(
        isinstance(identifier, str)
        and identifier in sources
        and _aware_datetime(sources[identifier].available_at)
        and sources[identifier].available_at <= level.known_at
        for identifier in level.source_bar_ids
    )


def favorable_structure(
    thesis: EntryThesis,
    context: MarketContext,
    profile: ManagementProfile,
    *,
    tick_size: float = 0.05,
    trail: bool = False,
) -> Optional[tuple[KnownLevel, float]]:
    """Latest favorable post-entry swing and its confirmation-frozen tolerance."""

    if thesis.fill_binding is None or not context.primary_quality.usable:
        return None
    terminal = as_utc(thesis.fill_binding.entry_terminal_at)
    boundary, _ = _entry_boundary(thesis, profile)
    noise = entry_noise_buffer(thesis, profile, tick_size)
    if terminal is None or boundary is None or noise is None:
        return None
    kind = "SWING_LOW" if thesis.direction == "BUY" else "SWING_HIGH"
    candidates = [
        level
        for level in context.known_structure
        if level.kind == kind
        and _causal_level(level, context)
        # A pivot whose candle straddled terminal entry can contain an extreme
        # from before ownership. It is not post-entry structural progress.
        and level.formed_at - timedelta(minutes=5) >= terminal
        and _direction(thesis) * (level.price - boundary) > noise
    ]
    if not candidates:
        return None
    # A lower long swing / higher short swing is not a new favorable base. It
    # cannot erase the stronger base whose loss is still under evaluation.
    level = max(
        candidates,
        key=lambda item: (
            _direction(thesis) * item.price,
            item.known_at,
            item.formed_at,
            item.level_id,
        ),
    )
    buffer = _confirmation_buffer(level, context, profile, tick_size, trail=trail)
    return (level, buffer) if buffer is not None else None


def _feature(context: MarketContext, name: str, value: Any) -> Optional[float]:
    quality = context.observation_quality.get(name)
    if quality is not None and not quality.usable:
        return None
    return _finite(value)


def _usable_higher_bars(context: MarketContext) -> bool:
    """Optional HTF defects suppress HTF evidence, never a known primary failure."""

    higher = context.higher_bar
    bars = context.higher_bars
    if (
        not context.higher_quality.usable
        or not higher
        or not bars
        or bars[-1] != higher
    ):
        return False
    delay = (
        context.context_policy.availability_delay_seconds
        if context.context_policy
        else 0
    )
    max_age = (
        context.context_policy.max_higher_age_seconds
        if context.context_policy
        else 1_200
    )

    def valid(bar: OHLCVBar) -> bool:
        return (
            all(
                _aware_datetime(value)
                for value in (bar.start, bar.end, bar.available_at)
            )
            and bar.end - bar.start == timedelta(minutes=15)
            and bar.end + timedelta(seconds=delay)
            <= bar.available_at
            <= context.decision_event_time
            and all(
                _finite(value) is not None and value > 0
                for value in (bar.open, bar.high, bar.low, bar.close)
            )
            and bar.low <= min(bar.open, bar.close)
            and bar.high >= max(bar.open, bar.close)
        )

    return (
        all(valid(bar) for bar in bars)
        and all(a.end <= b.start for a, b in zip(bars, bars[1:]))
        and (context.decision_event_time - higher.end).total_seconds() <= max_age
    )


def _higher_timeframe_evidence(
    thesis: EntryThesis,
    context: MarketContext,
    profile: ManagementProfile,
    tick_size: float,
) -> tuple[EvidenceObservation, Optional[bool], Optional[bool]]:
    """A completed 15m swing is context, never another independent vote."""

    higher = context.higher_bar
    level = None
    buffer = None
    if _usable_higher_bars(context):
        width = (
            context.context_policy.swing_confirmation_bars
            if context.context_policy
            else 2
        )
        levels = _known_structure(
            context.higher_bars, width, context.decision_event_time
        )
        kind = "SWING_LOW" if thesis.direction == "BUY" else "SWING_HIGH"
        candidates = [item for item in levels if item.kind == kind]
        if candidates:
            level = max(candidates, key=lambda item: (item.known_at, item.level_id))
            buffer = _confirmation_buffer(level, context, profile, tick_size)
    known = level is not None and buffer is not None
    distance = _direction(thesis) * (higher.close - level.price) if known else None
    supportive = distance > buffer if known else None
    failed = distance < -buffer if known else None
    return (
        EvidenceObservation(
            observation_id=f"higher-structure:{higher.bar_id if higher else context.snapshot_id}",
            family=EvidenceFamily.HIGHER_TIMEFRAME,
            # Resampling 5m prices supplies context, not independent market evidence.
            dependency_group="higher_timeframe_price_context",
            direction=EvidenceDirection.SUPPORTING
            if supportive
            else EvidenceDirection.OPPOSING
            if failed
            else EvidenceDirection.NEUTRAL
            if known
            else EvidenceDirection.UNKNOWN,
            severity=EvidenceSeverity.MATERIAL
            if supportive or failed
            else EvidenceSeverity.NONE
            if known
            else EvidenceSeverity.UNKNOWN,
            predicate="HIGHER_TIMEFRAME_STRUCTURE_HELD"
            if supportive
            else "HIGHER_TIMEFRAME_STRUCTURE_FAILED"
            if failed
            else "HIGHER_TIMEFRAME_STRUCTURE_NEUTRAL"
            if known
            else "HIGHER_TIMEFRAME_STRUCTURE_UNAVAILABLE",
            value=distance,
            threshold=buffer,
            source_bar_ids=tuple(dict.fromkeys((*level.source_bar_ids, higher.bar_id)))
            if known
            else (),
            known_at=higher.available_at.isoformat()
            if higher and _aware_datetime(higher.available_at)
            else None,
            quality="VALID" if known else "UNAVAILABLE",
            details={"level_id": level.level_id, "level": level.price} if known else {},
        ),
        supportive,
        failed,
    )


def build_evidence(
    thesis: EntryThesis,
    context: MarketContext,
    profile: ManagementProfile,
    *,
    tick_size: float = 0.05,
    management_state: Optional[ManagementState] = None,
) -> EvidenceReport:
    """Derive the small v1 predicate set from completed, causal context only."""

    primary = context.primary_bar
    if (
        primary is None
        or not context.primary_quality.usable
        or not primary.start
        < primary.end
        <= primary.available_at
        <= context.decision_event_time
        or not context.primary_bars
        or context.primary_bars[-1] != primary
        or any(
            bar.available_at > context.decision_event_time
            for bar in context.primary_bars
        )
    ):
        quality = context.primary_quality.status.value
        observation = EvidenceObservation(
            observation_id=f"primary-quality:{context.snapshot_id}",
            family=EvidenceFamily.STRUCTURE,
            dependency_group="primary_market_data",
            direction=EvidenceDirection.UNKNOWN,
            severity=EvidenceSeverity.UNKNOWN,
            predicate="PRIMARY_CONTEXT_UNAVAILABLE",
            quality=quality,
            details={"issues": context.primary_quality.issues},
        )
        return evidence_report(
            (observation,),
            predicates={
                "primary_context_usable": False,
                "required_context_usable": False,
                "reason": quality,
            },
        )

    direction = _direction(thesis)
    close = primary.close
    buffer = entry_noise_buffer(thesis, profile, tick_size)
    observations: list[EvidenceObservation] = []
    predicates: dict[str, Any] = {
        "primary_context_usable": True,
        "primary_bar_id": primary.bar_id,
        "close": close,
        "buffer": buffer,
        "noise_buffer": buffer,
    }
    boundary, boundary_id = _entry_boundary(thesis, profile)
    boundary_spec = thesis.management_profile.values.get("entry_boundary") or {}
    if not isinstance(boundary_spec, Mapping):
        boundary_spec = {}
    predicates["entry_boundary_kind"] = str(
        boundary_spec.get("kind", "STRUCTURE")
    ).upper()
    if str(boundary_spec.get("kind", "")).upper() in {"VWAP", "SESSION_VWAP"}:
        boundary = _feature(context, "session_vwap", context.session_vwap)
    if boundary is None or buffer is None:
        observations.append(
            EvidenceObservation(
                observation_id=f"entry-boundary:unknown:{primary.bar_id}",
                family=EvidenceFamily.STRUCTURE,
                dependency_group="entry_boundary",
                direction=EvidenceDirection.UNKNOWN,
                severity=EvidenceSeverity.UNKNOWN,
                predicate="ENTRY_BOUNDARY_UNAVAILABLE",
                source_bar_ids=(primary.bar_id,),
                known_at=primary.available_at.isoformat(),
                quality="UNAVAILABLE",
            )
        )
        predicates["entry_boundary_failure"] = None
        predicates["entry_boundary_recovery"] = None
    else:
        signed_distance = direction * (close - boundary)
        failed = signed_distance < -buffer
        recovered = signed_distance > buffer
        predicates.update(
            {
                "entry_boundary_id": boundary_id,
                "entry_boundary": boundary,
                "entry_boundary_failure": failed,
                "entry_boundary_recovery": recovered,
                "entry_boundary_signed_distance": signed_distance,
            }
        )
        observations.append(
            EvidenceObservation(
                observation_id=f"entry-boundary:{boundary_id}:{primary.bar_id}",
                family=EvidenceFamily.STRUCTURE,
                dependency_group="entry_boundary",
                direction=(
                    EvidenceDirection.OPPOSING
                    if failed
                    else EvidenceDirection.SUPPORTING
                    if recovered
                    else EvidenceDirection.NEUTRAL
                ),
                severity=(
                    EvidenceSeverity.MATERIAL if failed else EvidenceSeverity.NONE
                ),
                predicate=(
                    "ENTRY_BOUNDARY_FAILURE"
                    if failed
                    else "ENTRY_BOUNDARY_RECOVERY"
                    if recovered
                    else "ENTRY_BOUNDARY_NEUTRAL"
                ),
                value=signed_distance,
                threshold=buffer,
                source_bar_ids=(primary.bar_id,),
                known_at=primary.available_at.isoformat(),
                details={"level_id": boundary_id, "level": boundary},
            )
        )

    favorable = favorable_structure(thesis, context, profile, tick_size=tick_size)
    trail_buffer = (
        _confirmation_buffer(favorable[0], context, profile, tick_size, trail=True)
        if favorable
        else None
    )
    retained = False
    if management_state is not None:
        known_at = as_utc(management_state.favorable_structure_known_at)
        pinned_price = management_state.favorable_structure_price
        pinned_failure_buffer = getattr(
            management_state, "favorable_structure_failure_buffer", None
        )
        if (
            management_state.favorable_structure_id
            and known_at is not None
            and known_at <= context.decision_event_time
            and pinned_price is not None
            and pinned_failure_buffer is not None
            and (
                favorable is None
                or favorable[0].known_at <= known_at
                or favorable[0].level_id == management_state.favorable_structure_id
                or direction * (favorable[0].price - pinned_price) <= 0
                or direction * (favorable[0].price - pinned_price)
                < favorable[1] - pinned_failure_buffer
            )
        ):
            favorable = (
                KnownLevel(
                    level_id=management_state.favorable_structure_id,
                    kind="SWING_LOW" if thesis.direction == "BUY" else "SWING_HIGH",
                    price=pinned_price,
                    formed_at=known_at,
                    known_at=known_at,
                    source_bar_ids=(),
                ),
                pinned_failure_buffer,
            )
            trail_buffer = management_state.favorable_structure_buffer
            retained = True
    predicates.update(
        {
            "failed_favorable_structure": None,
            "favorable_structure_recovery": None,
            "favorable_structure_id": None,
        }
    )
    if favorable is not None:
        level, level_buffer = favorable
        distance = direction * (close - level.price)
        failed = distance < -level_buffer
        recovered = distance > level_buffer
        predicates.update(
            {
                "failed_favorable_structure": failed,
                "favorable_structure_recovery": recovered,
                "favorable_structure_id": level.level_id,
                "favorable_structure_price": level.price,
                "favorable_structure_buffer": level_buffer,
                "favorable_structure_known_at": level.known_at.isoformat(),
                "favorable_structure_trail_buffer": trail_buffer,
            }
        )
        observations.append(
            EvidenceObservation(
                observation_id=f"favorable-structure:{level.level_id}:{primary.bar_id}",
                family=EvidenceFamily.STRUCTURE,
                dependency_group="favorable_structure",
                direction=EvidenceDirection.OPPOSING
                if failed
                else EvidenceDirection.SUPPORTING
                if recovered
                else EvidenceDirection.NEUTRAL,
                severity=EvidenceSeverity.MATERIAL if failed else EvidenceSeverity.NONE,
                predicate="FAILED_FAVORABLE_STRUCTURE"
                if failed
                else "FAVORABLE_STRUCTURE_HELD"
                if recovered
                else "FAVORABLE_STRUCTURE_NEUTRAL",
                value=distance,
                threshold=level_buffer,
                source_bar_ids=tuple(
                    dict.fromkeys((*level.source_bar_ids, primary.bar_id))
                ),
                known_at=primary.available_at.isoformat(),
                details={
                    "level_id": level.level_id,
                    "level": level.price,
                    "level_known_at": level.known_at.isoformat(),
                    "retained_from_management_state": retained,
                },
            )
        )

    dynamics = context.direction_dynamics
    slope = _feature(context, "ema_20_slope", dynamics.get("ema_20_slope"))
    bars = context.primary_bars
    two_adverse_changes = False
    if len(bars) >= 3:
        changes = [
            direction * (bars[-1].close - bars[-2].close),
            direction * (bars[-2].close - bars[-3].close),
        ]
        two_adverse_changes = all(change < 0 for change in changes)
    dynamics_known = (
        slope is not None and len(bars) >= 3 and _contiguous_same_session(bars[-3:])
    )
    dynamics_adverse = bool(
        dynamics_known and two_adverse_changes and direction * slope < 0
    )
    observations.append(
        EvidenceObservation(
            observation_id=f"dynamics:{primary.bar_id}",
            family=EvidenceFamily.DYNAMICS,
            dependency_group="directional_dynamics",
            direction=(
                EvidenceDirection.OPPOSING
                if dynamics_adverse
                else EvidenceDirection.NEUTRAL
                if dynamics_known
                else EvidenceDirection.UNKNOWN
            ),
            severity=(
                EvidenceSeverity.MATERIAL
                if dynamics_adverse
                else EvidenceSeverity.NONE
                if dynamics_known
                else EvidenceSeverity.UNKNOWN
            ),
            predicate=(
                "MATERIAL_ADVERSE_DYNAMICS"
                if dynamics_adverse
                else "DYNAMICS_NEUTRAL"
                if dynamics_known
                else "DYNAMICS_UNAVAILABLE"
            ),
            value=slope,
            source_bar_ids=tuple(bar.bar_id for bar in bars[-3:]),
            known_at=primary.available_at.isoformat(),
        )
    )
    predicates["material_adverse_dynamics"] = (
        dynamics_adverse if dynamics_known else None
    )

    relative_volume = _feature(
        context, "relative_volume_20", context.participation.get("relative_volume_20")
    )
    volume_window = bars[-21:]
    participation_known = (
        relative_volume is not None
        and relative_volume >= 0
        and buffer is not None
        and len(volume_window) == 21
        and all(
            _finite(bar.volume) is not None and bar.volume >= 0 for bar in volume_window
        )
        and sum(bar.volume for bar in volume_window[:-1]) > 0
        and _contiguous_same_session(bars[-2:])
    )
    adverse_move = False
    if len(bars) >= 2 and buffer is not None:
        adverse_move = direction * (bars[-1].close - bars[-2].close) < -buffer
    volume_backed = bool(
        participation_known and adverse_move and relative_volume >= 1.5
    )
    pullback_contracting = bool(
        participation_known
        and adverse_move
        and relative_volume < 1.0
        and predicates.get("entry_boundary_failure") is False
    )
    observations.append(
        EvidenceObservation(
            observation_id=f"participation:{primary.bar_id}",
            family=EvidenceFamily.PARTICIPATION,
            dependency_group="adverse_displacement_volume",
            direction=(
                EvidenceDirection.OPPOSING
                if volume_backed
                else EvidenceDirection.SUPPORTING
                if pullback_contracting
                else EvidenceDirection.NEUTRAL
                if participation_known
                else EvidenceDirection.UNKNOWN
            ),
            severity=(
                EvidenceSeverity.MATERIAL
                if volume_backed
                else EvidenceSeverity.WATCH
                if pullback_contracting
                else EvidenceSeverity.NONE
                if participation_known
                else EvidenceSeverity.UNKNOWN
            ),
            predicate=(
                "VOLUME_BACKED_ADVERSE_MOVE"
                if volume_backed
                else "CONTRACTING_PULLBACK_PARTICIPATION"
                if pullback_contracting
                else "PARTICIPATION_NEUTRAL"
                if participation_known
                else "PARTICIPATION_UNAVAILABLE"
            ),
            value=relative_volume,
            threshold=1.5,
            source_bar_ids=tuple(bar.bar_id for bar in volume_window),
            known_at=primary.available_at.isoformat(),
            quality="VALID" if participation_known else "UNAVAILABLE",
            details={
                "baseline_type": "ROLLING_20_TRADING_BARS",
                "baseline_sample_count": min(20, max(0, len(bars) - 1)),
                "baseline_spans_session": len(
                    {
                        bar.start.astimezone(EXCHANGE_TIMEZONE).date()
                        for bar in volume_window
                    }
                )
                > 1,
            },
        )
    )
    predicates["volume_backed_adverse_move"] = (
        volume_backed if participation_known else None
    )

    if profile.requires_vwap_acceptance:
        vwap = _feature(context, "session_vwap", context.session_vwap)
        if vwap is None or buffer is None:
            direction_value = EvidenceDirection.UNKNOWN
            severity = EvidenceSeverity.UNKNOWN
            predicate = "VWAP_UNAVAILABLE"
            distance = None
            failed = None
        else:
            distance = direction * (close - vwap)
            failed = distance < -buffer
            direction_value = (
                EvidenceDirection.OPPOSING if failed else EvidenceDirection.NEUTRAL
            )
            severity = EvidenceSeverity.MATERIAL if failed else EvidenceSeverity.NONE
            predicate = "VWAP_ACCEPTANCE_FAILURE" if failed else "VWAP_NEUTRAL"
        observations.append(
            EvidenceObservation(
                observation_id=f"vwap:{primary.bar_id}",
                family=EvidenceFamily.VALUE,
                dependency_group=(
                    "entry_boundary"
                    if str(boundary_spec.get("kind", "")).upper()
                    in {"VWAP", "SESSION_VWAP"}
                    else "session_vwap_acceptance"
                ),
                direction=direction_value,
                severity=severity,
                predicate=predicate,
                value=distance,
                threshold=buffer,
                source_bar_ids=(primary.bar_id,),
                known_at=primary.available_at.isoformat(),
            )
        )
        predicates["vwap_acceptance_failure"] = failed

    higher_observation, higher_support, higher_failure = _higher_timeframe_evidence(
        thesis, context, profile, tick_size
    )
    observations.append(higher_observation)
    predicates.update(
        {
            "higher_timeframe_support": higher_support,
            "higher_timeframe_failure": higher_failure,
            # VWAP/volume/dynamics corroboration is optional for Route A. A
            # VWAP-defined boundary itself remains unknown when VWAP is missing.
            "required_context_usable": boundary is not None and buffer is not None,
            "recovery_context_usable": all(
                item.usable
                and item.direction is not EvidenceDirection.UNKNOWN
                and item.severity is not EvidenceSeverity.UNKNOWN
                for item in observations
                if item.family
                in {
                    EvidenceFamily.VALUE,
                    EvidenceFamily.DYNAMICS,
                    EvidenceFamily.PARTICIPATION,
                }
            ),
        }
    )
    return evidence_report(observations, predicates=predicates)
