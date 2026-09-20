"""Versioned contracts for durable exit-management state.

The package deliberately contains no broker calls and no discretionary exit
policy.  Phase 5 uses it to preserve the entry premise and lifecycle history;
the pure decision engine is introduced in phase 6.
"""

from .models import (
    DecisionRecord,
    DevelopmentPhase,
    ExposureState,
    PositionCheckpoint,
    PositionState,
    ProtectionState,
    ThesisHealth,
)
from .thesis import EntryThesis, bind_terminal_fill, capture_entry_thesis

__all__ = [
    "DecisionRecord",
    "DevelopmentPhase",
    "EntryThesis",
    "ExposureState",
    "PositionCheckpoint",
    "PositionState",
    "ProtectionState",
    "ThesisHealth",
    "bind_terminal_fill",
    "capture_entry_thesis",
]
