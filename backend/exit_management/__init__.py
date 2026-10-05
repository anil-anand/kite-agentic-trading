"""Versioned, broker-independent contracts for exit-management decisions."""

from .engine import ExitEvaluation, ExitPolicy, evaluate_exit
from .evidence import (
    EvidenceDirection,
    EvidenceFamily,
    EvidenceReport,
    EvidenceSeverity,
)
from .evidence import (
    EvidenceObservation as MarketEvidenceObservation,
)
from .models import (
    DecisionRecord,
    DevelopmentPhase,
    ExitAction,
    ExitDecision,
    ExitIntentType,
    ExitReasonCode,
    ExposureState,
    ManagementState,
    PositionCheckpoint,
    PositionState,
    ProposedIntent,
    ProtectionState,
    ThesisHealth,
)
from .thesis import EntryThesis, bind_terminal_fill, capture_entry_thesis

__all__ = [
    "DecisionRecord",
    "DevelopmentPhase",
    "EntryThesis",
    "EvidenceDirection",
    "EvidenceFamily",
    "EvidenceReport",
    "EvidenceSeverity",
    "ExitAction",
    "ExitDecision",
    "ExitEvaluation",
    "ExitIntentType",
    "ExitPolicy",
    "ExitReasonCode",
    "ExposureState",
    "ManagementState",
    "MarketEvidenceObservation",
    "PositionCheckpoint",
    "PositionState",
    "ProtectionState",
    "ProposedIntent",
    "ThesisHealth",
    "bind_terminal_fill",
    "capture_entry_thesis",
    "evaluate_exit",
]
