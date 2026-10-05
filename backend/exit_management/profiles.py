"""Small, pinned management-profile definitions for the phase-6 policy.

Profiles select documented expectations and tolerances; they do not add entry
signals, symbol-specific thresholds, or a weighted exit score.  The entry thesis
stores the profile identity and objective mode, while this module supplies the
versioned candidate defaults used by replay/scenario tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .models import ManagementProfileSnapshot, _freeze


class ManagementProfileName(str, Enum):
    TREND_CONTINUATION = "trend_continuation"
    BREAKOUT_FOLLOW_THROUGH = "breakout_follow_through"
    RANGE_CONVERGENCE = "range_convergence"
    UNKNOWN_LEGACY_BOUNDED = "unknown_legacy_bounded"


class ObjectiveMode(str, Enum):
    NONE = "none"
    FIXED_OBJECTIVE = "fixed_objective"
    STRUCTURE_RUNNER = "structure_runner"


@dataclass(frozen=True)
class ManagementProfile:
    """All policy knobs allowed in the deliberately small v1 parameter budget."""

    name: ManagementProfileName
    version: str = "management-profiles-v1"
    objective_mode: ObjectiveMode = ObjectiveMode.NONE
    normal_thesis_management: bool = True
    requires_vwap_acceptance: bool = False
    failure_confirmation_bars: int = 2
    recovery_confirmation_bars: int = 2
    review_horizon_bars: Optional[int] = None
    boundary_buffer_atr_multiple: float = 0.1
    structural_trail_buffer_atr_multiple: float = 0.1
    minimum_buffer_ticks: int = 2
    profit_reversal_min_mfe_r: float = 2.0
    profit_reversal_min_giveback_r: float = 1.0
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", ManagementProfileName(self.name))
        object.__setattr__(self, "objective_mode", ObjectiveMode(self.objective_mode))
        if not self.version:
            raise ValueError("management profile version is required")
        for name in ("normal_thesis_management", "requires_vwap_acceptance"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if not isinstance(self.values, Mapping):
            raise ValueError("management profile values must be a mapping")
        object.__setattr__(self, "values", _freeze(self.values))
        for name in (
            "failure_confirmation_bars",
            "recovery_confirmation_bars",
            "minimum_buffer_ticks",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.review_horizon_bars is not None and (
            isinstance(self.review_horizon_bars, bool)
            or not isinstance(self.review_horizon_bars, int)
            or self.review_horizon_bars < 1
        ):
            raise ValueError("review_horizon_bars must be a positive integer")
        for name in (
            "boundary_buffer_atr_multiple",
            "structural_trail_buffer_atr_multiple",
            "profit_reversal_min_mfe_r",
            "profit_reversal_min_giveback_r",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite nonnegative number")


_DEFAULTS = {
    ManagementProfileName.TREND_CONTINUATION: ManagementProfile(
        name=ManagementProfileName.TREND_CONTINUATION,
        objective_mode=ObjectiveMode.STRUCTURE_RUNNER,
        review_horizon_bars=12,
    ),
    ManagementProfileName.BREAKOUT_FOLLOW_THROUGH: ManagementProfile(
        name=ManagementProfileName.BREAKOUT_FOLLOW_THROUGH,
        objective_mode=ObjectiveMode.FIXED_OBJECTIVE,
        review_horizon_bars=6,
    ),
    ManagementProfileName.RANGE_CONVERGENCE: ManagementProfile(
        name=ManagementProfileName.RANGE_CONVERGENCE,
        objective_mode=ObjectiveMode.FIXED_OBJECTIVE,
        review_horizon_bars=6,
    ),
    ManagementProfileName.UNKNOWN_LEGACY_BOUNDED: ManagementProfile(
        name=ManagementProfileName.UNKNOWN_LEGACY_BOUNDED,
        objective_mode=ObjectiveMode.NONE,
        normal_thesis_management=False,
    ),
}


def default_profiles() -> Mapping[ManagementProfileName, ManagementProfile]:
    """Return immutable profile objects keyed by stable profile identity."""

    return MappingProxyType(_DEFAULTS)


def _objective_mode(snapshot: ManagementProfileSnapshot) -> Optional[ObjectiveMode]:
    # Omission selects the versioned profile default. An explicit unsupported
    # mode carries no target-touch authority: treating it as omission could
    # silently turn a recorded runner into a fixed-objective exit.
    if "objective_mode" not in snapshot.values:
        return None
    value = snapshot.values.get("objective_mode")
    aliases = {
        "legacy_fixed": ObjectiveMode.FIXED_OBJECTIVE,
        "fixed": ObjectiveMode.FIXED_OBJECTIVE,
        "fixed_objective": ObjectiveMode.FIXED_OBJECTIVE,
        "structure_runner": ObjectiveMode.STRUCTURE_RUNNER,
        "runner": ObjectiveMode.STRUCTURE_RUNNER,
        "none": ObjectiveMode.NONE,
    }
    return aliases.get(str(value).lower(), ObjectiveMode.NONE)


def resolve_profile(
    snapshot: ManagementProfileSnapshot,
    *,
    overrides: Optional[Mapping[str, ManagementProfile]] = None,
) -> ManagementProfile:
    """Resolve only a recorded profile identity; unsupported premises stay bounded."""

    try:
        name = ManagementProfileName(snapshot.name)
    except ValueError:
        name = ManagementProfileName.UNKNOWN_LEGACY_BOUNDED
    profile = (overrides or {}).get(name.value) or _DEFAULTS[name]
    if profile.name is not name:
        raise ValueError("profile override identity must match its lookup key")
    objective_mode = _objective_mode(snapshot)
    boundary = snapshot.values.get("entry_boundary")
    vwap_boundary = isinstance(boundary, Mapping) and str(
        boundary.get("kind", "")
    ).upper() in {"VWAP", "SESSION_VWAP"}
    # An unknown version must never be relabeled as today's implementation. Keep
    # existing explicit objectives, but disable discretionary thesis rules.
    supported = snapshot.version == profile.version
    if not supported:
        profile = _DEFAULTS[ManagementProfileName.UNKNOWN_LEGACY_BOUNDED]
    if objective_mode is None:
        objective_mode = profile.objective_mode
    return ManagementProfile(
        name=profile.name,
        version=profile.version,
        objective_mode=objective_mode,
        normal_thesis_management=profile.normal_thesis_management,
        requires_vwap_acceptance=(
            profile.normal_thesis_management
            and (
                profile.requires_vwap_acceptance
                or snapshot.values.get("requires_vwap_acceptance") is True
                or vwap_boundary
            )
        ),
        failure_confirmation_bars=profile.failure_confirmation_bars,
        recovery_confirmation_bars=profile.recovery_confirmation_bars,
        review_horizon_bars=profile.review_horizon_bars,
        boundary_buffer_atr_multiple=profile.boundary_buffer_atr_multiple,
        structural_trail_buffer_atr_multiple=(
            profile.structural_trail_buffer_atr_multiple
        ),
        minimum_buffer_ticks=profile.minimum_buffer_ticks,
        profit_reversal_min_mfe_r=profile.profit_reversal_min_mfe_r,
        profit_reversal_min_giveback_r=profile.profit_reversal_min_giveback_r,
        values={**snapshot.values, "requested_profile_version": snapshot.version},
    )
