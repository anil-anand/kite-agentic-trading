"""Pure, deterministic thesis and profit-management policy.

This module intentionally has no broker, database, configuration singleton, or
wall-clock access.  It consumes explicit snapshots and proposes intents for the
phase-7 coordinator; it cannot submit, cancel, or modify an order itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from backend.market_context import ContextQuality, MarketContext
from backend.risk_rules import (
    HardRiskAction,
    HardRiskPolicy,
    HardRiskSnapshot,
    evaluate_hard_risk,
    is_fresh_mark,
)
from backend.time_utils import as_utc

from .evidence import (
    EvidenceFamily,
    EvidenceObservation,
    EvidenceReport,
    build_evidence,
    evidence_report,
    has_independent_corroborator,
)
from .models import (
    DevelopmentPhase,
    ExitAction,
    ExitDecision,
    ExitIntentType,
    ExitReasonCode,
    ExposureState,
    LifecycleEvent,
    ManagementState,
    PositionState,
    ProposedIntent,
    ProtectionState,
    ThesisHealth,
    reduce_lifecycle,
)
from .profiles import ManagementProfile, ObjectiveMode, resolve_profile
from .thesis import EntryThesis, ThesisBindingStatus

EXIT_POLICY_VERSION = "deterministic-exit-v1"


@dataclass(frozen=True)
class ExitPolicy:
    """Pinned inputs for a single policy evaluation.

    The profile defaults are deliberately small research defaults.  A caller may
    provide preconstructed profile overrides only when their version is recorded
    in this policy; no global configuration is consulted.
    """

    policy_version: str = EXIT_POLICY_VERSION
    tick_size: float = 0.05
    hard_risk_policy: HardRiskPolicy = HardRiskPolicy()
    profile_overrides: Mapping[str, ManagementProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("exit policy version is required")
        if not isinstance(self.hard_risk_policy, HardRiskPolicy):
            raise TypeError("hard_risk_policy must be a HardRiskPolicy")
        risk_policy = self.hard_risk_policy
        if (
            not isinstance(risk_policy.policy_version, str)
            or not risk_policy.policy_version
            or not isinstance(risk_policy.require_protection, bool)
            or isinstance(risk_policy.mark_max_age_seconds, bool)
            or not isinstance(risk_policy.mark_max_age_seconds, (int, float))
            or not math.isfinite(risk_policy.mark_max_age_seconds)
            or risk_policy.mark_max_age_seconds < 0
        ):
            raise ValueError(
                "hard-risk policy needs valid version, protection and freshness"
            )
        if (
            isinstance(self.tick_size, bool)
            or not isinstance(self.tick_size, (int, float))
            or not math.isfinite(float(self.tick_size))
            or self.tick_size <= 0
        ):
            raise ValueError("tick_size must be a finite positive price")
        for name, profile in self.profile_overrides.items():
            if not isinstance(profile, ManagementProfile) or name != profile.name.value:
                raise ValueError("profile override identity must match its lookup key")
        object.__setattr__(
            self, "profile_overrides", MappingProxyType(dict(self.profile_overrides))
        )


@dataclass(frozen=True)
class ExitEvaluation:
    """The pure transition result consumed by persistence and coordination."""

    next_position_state: PositionState
    next_management_state: ManagementState
    decision: ExitDecision
    proposed_intent: Optional[ProposedIntent]
    evidence: EvidenceReport

    @property
    def next_state(self) -> PositionState:
        """Compatibility with the architecture's abbreviated transition form."""

        return self.next_position_state


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _timestamp(value: datetime | str) -> str:
    converted = as_utc(value)
    if converted is None:
        raise ValueError("exit evaluation needs an aware event time")
    return converted.isoformat()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _event_identity(
    position_key: str, kind: str, occurred_at: str, discriminator: str
) -> str:
    return f"exit:{kind}:{_canonical_hash({'p': position_key, 't': occurred_at, 'd': discriminator})}"


def _json_value(value: Any) -> Any:
    """Canonical, secret-free domain inputs, including immutable mappings."""
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def _empty_report(reason: str) -> EvidenceReport:
    return evidence_report((), predicates={"evaluation": reason})


def _latch_exit(
    state: PositionState,
    *,
    reason: str,
    occurred_at: str,
    health: Optional[ThesisHealth] = None,
    flatten: bool = False,
    recovery: bool = False,
) -> tuple[PositionState, ProposedIntent]:
    intent_id = state.latched_exit_intent_id or _event_identity(
        state.position_key, "intent", occurred_at, reason
    )
    intent = ProposedIntent(
        intent_type=ExitIntentType.FLATTEN if flatten else ExitIntentType.EXIT,
        position_key=state.position_key,
        reason_code=reason,
        intent_id=intent_id,
        urgency="CRITICAL"
        if flatten or reason.startswith(("RISK_", "SESSION_", "OPERATOR_"))
        else "NORMAL",
        quantity=state.known_quantity
        if state.known_quantity and not recovery
        else None,
        parent_intent_id=state.latched_exit_intent_id,
        details={"latched": True, "requires_reconciliation": recovery},
    )
    if state.exposure in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}:
        return state, intent
    if (
        recovery
        or state.exposure is ExposureState.RECOVERY_REQUIRED
        or not state.known_quantity
    ):
        event = LifecycleEvent.RECONCILIATION_REQUIRED
    else:
        event = LifecycleEvent.EXIT_REQUESTED
    return reduce_lifecycle(
        state,
        event,
        event_id=_event_identity(state.position_key, "exit", occurred_at, intent_id),
        occurred_at=occurred_at,
        thesis_health=health,
        exit_intent_id=intent_id,
    ), intent


def _predicate(report: EvidenceReport, name: str) -> Optional[bool]:
    value = report.predicates.get(name)
    return value if isinstance(value, bool) else None


def _contiguous(previous_end: Optional[str], current_start: datetime) -> bool:
    return previous_end is not None and as_utc(previous_end) == current_start


def _clear_confirmation(management: ManagementState) -> ManagementState:
    return replace(
        management,
        failure_count=0,
        last_failure_bar_end=None,
        local_failure_count=0,
        last_local_failure_bar_end=None,
        weakening_count=0,
        last_weakening_bar_end=None,
        recovery_count=0,
        last_recovery_bar_end=None,
        stagnation_count=0,
        last_stagnation_bar_end=None,
    )


def _update_extrema(thesis, management, price, at, *, completed=False):
    if (
        thesis is None
        or thesis.binding_status is not ThesisBindingStatus.BOUND
        or price is None
    ):
        return management
    binding = thesis.fill_binding
    direction = 1 if thesis.direction == "BUY" else -1
    value = direction * (price - binding.entry_vwap) / binding.initial_r_per_share
    prefix = "completed" if completed else "observed"
    previous_mfe = getattr(management, f"{prefix}_mfe_r")
    previous_mae = getattr(management, f"{prefix}_mae_r")
    favorable, adverse = max(0.0, value), max(0.0, -value)
    changes = {}
    if previous_mfe is None or favorable > previous_mfe:
        changes.update({f"{prefix}_mfe_r": favorable, f"{prefix}_mfe_at": at})
    if previous_mae is None or adverse > previous_mae:
        changes.update({f"{prefix}_mae_r": adverse, f"{prefix}_mae_at": at})
    return replace(management, **changes)


def _profit_context(thesis, management, mark):
    """Unit-explicit diagnostics; no estimated fees or unobserved peaks invented."""
    if thesis is None or thesis.binding_status is not ThesisBindingStatus.BOUND:
        return {"available": False}
    binding = thesis.fill_binding
    direction = 1 if thesis.direction == "BUY" else -1
    risk = binding.initial_r_per_share
    current_r = direction * (mark - binding.entry_vwap) / risk if mark else None
    observed_mfe = management.observed_mfe_r
    giveback = (
        max(0.0, observed_mfe - current_r)
        if observed_mfe is not None and current_r is not None
        else None
    )
    return {
        "available": True,
        "initial_r_per_share": risk,
        "unrealized_r": current_r,
        "observed_mfe_r": observed_mfe,
        "observed_mae_r": management.observed_mae_r,
        "completed_mfe_r": management.completed_mfe_r,
        "completed_mae_r": management.completed_mae_r,
        "observed_mfe_price_distance": observed_mfe * risk
        if observed_mfe is not None
        else None,
        "observed_mae_price_distance": management.observed_mae_r * risk
        if management.observed_mae_r is not None
        else None,
        "giveback_r": giveback,
        "giveback_fraction": giveback / observed_mfe
        if giveback is not None and observed_mfe > 0
        else None,
        "confirmed_stop_gross_r": (
            direction * (management.confirmed_stop - binding.entry_vwap) / risk
            if management.confirmed_stop is not None
            else None
        ),
        "bars_since_favorable_progress": (
            management.eligible_completed_bars
            - management.last_favorable_progress_bar_count
            if management.last_favorable_progress_bar_count is not None
            else None
        ),
    }


def _is_post_entry_bar(thesis: EntryThesis, context: MarketContext) -> bool:
    primary = context.primary_bar
    terminal = (
        as_utc(thesis.fill_binding.entry_terminal_at) if thesis.fill_binding else None
    )
    return bool(
        primary
        and terminal
        and primary.start >= terminal
        and primary.bar_id != thesis.input_reference.primary_bar_id
    )


def _context_problem(
    context: Optional[MarketContext], event_time: datetime
) -> Optional[ExitReasonCode]:
    if context is None:
        return ExitReasonCode.HOLD_DATA_DEGRADED
    status = context.primary_quality.status
    if status is ContextQuality.INCOMPLETE:
        return ExitReasonCode.DATA_INCOMPLETE_CANDLE
    if status is ContextQuality.STALE:
        return ExitReasonCode.DATA_STALE_CONTEXT
    if status in {ContextQuality.GAP, ContextQuality.INVALID}:
        return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    if not context.normal_decision_eligible:
        return ExitReasonCode.HOLD_DATA_DEGRADED
    primary = context.primary_bar
    if any(
        not isinstance(t, datetime) or t.tzinfo is None
        for t in (primary.start, primary.end, primary.available_at)
    ):
        return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    delay = (
        context.context_policy.availability_delay_seconds
        if context.context_policy
        else 0
    )
    max_age = (
        context.context_policy.max_primary_age_seconds
        if context.context_policy
        else 600
    )
    if any(
        not isinstance(t, datetime) or t.tzinfo is None
        for t in (
            context.decision_event_time,
            context.received_at,
            context.source_as_of,
        )
    ):
        return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    if any(
        t > event_time
        for t in (
            context.decision_event_time,
            context.received_at,
            context.source_as_of,
        )
    ):
        return ExitReasonCode.DATA_INCOMPLETE_CANDLE
    if (event_time - primary.end).total_seconds() > max_age:
        return ExitReasonCode.DATA_STALE_CONTEXT
    bars = context.primary_bars
    if not bars or bars[-1] != primary:
        return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    for bar in bars:
        if any(
            not isinstance(t, datetime) or t.tzinfo is None
            for t in (bar.start, bar.end, bar.available_at)
        ):
            return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
        if (
            bar.available_at > context.decision_event_time
            or bar.end + timedelta(seconds=delay) > context.decision_event_time
        ):
            return ExitReasonCode.DATA_INCOMPLETE_CANDLE
        prices = (bar.open, bar.high, bar.low, bar.close)
        if (
            bar.end - bar.start != timedelta(minutes=5)
            or any(_finite(price) is None or price <= 0 for price in prices)
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    if any(a.end > b.start for a, b in zip(bars, bars[1:])):
        return ExitReasonCode.DATA_GAP_OR_INVALID_OHLCV
    # Optional structure and HTF observations validate their own sources. They
    # cannot veto an otherwise known, frozen entry-boundary failure.
    return None


def _structural_trail(thesis, profile, management, tick_size, mark, hard_stop):
    if (
        management.confirmed_stop is None
        or mark is None
        or management.favorable_structure_price is None
    ):
        return None
    buffer = management.favorable_structure_buffer
    if buffer is None:
        return None
    direction = 1 if thesis.direction == "BUY" else -1
    proposed = management.favorable_structure_price - direction * buffer
    # Decimal avoids binary float rounding a valid tick down/up twice.
    from decimal import ROUND_CEILING, ROUND_FLOOR

    tick = Decimal(str(tick_size))
    rounding = ROUND_FLOOR if direction == 1 else ROUND_CEILING
    proposed = float(
        (Decimal(str(proposed)) / tick).to_integral_value(rounding=rounding) * tick
    )
    stops = [management.confirmed_stop]
    if _finite(hard_stop) is not None and hard_stop > 0:
        stops.append(hard_stop)
    if proposed <= 0 or any(direction * (proposed - stop) <= 0 for stop in stops):
        return None
    if direction * (mark - proposed) <= profile.minimum_buffer_ticks * tick_size:
        return None
    return proposed


def evaluate_exit(
    thesis: Optional[EntryThesis],
    position_state: PositionState,
    market_context: Optional[MarketContext],
    risk_snapshot: HardRiskSnapshot,
    policy: ExitPolicy,
    *,
    management_state: Optional[ManagementState] = None,
    observed_mark_price: Optional[float] = None,
    observed_mark_time: Optional[datetime] = None,
    evidence: Optional[EvidenceReport | Sequence[EvidenceObservation]] = None,
) -> ExitEvaluation:
    """Propose a deterministic transition; execution remains the coordinator's job.

    The risk event is the authoritative clock. Untimestamped price observations
    cannot execute objectives or establish extrema. Ordinary rules consume only
    new, fully post-entry completed bars; no poll advances their confirmation.
    Optional supplied evidence is retained for diagnostics; it cannot override
    the shared builder's authoritative observations and predicates.
    """
    event_time = risk_snapshot.session.observed_at
    if event_time.tzinfo is None:
        raise ValueError("exit evaluation needs an aware risk event time")
    occurred_at = _timestamp(event_time)
    original_management = management_state or ManagementState()
    management = original_management
    profile = (
        resolve_profile(thesis.management_profile, overrides=policy.profile_overrides)
        if thesis
        else None
    )
    artifact = _json_value({"policy": policy, "profile": profile})
    policy_fingerprint = _canonical_hash(artifact)
    policy_matches = management.policy_fingerprint in {None, policy_fingerprint}
    if management.policy_fingerprint is None:
        management = replace(management, policy_fingerprint=policy_fingerprint)
    input_payload = _json_value(
        {
            "thesis": thesis,
            "state": position_state,
            "management": original_management,
            "context": market_context,
            "risk": risk_snapshot,
            "policy": artifact,
            "observed_mark_price": observed_mark_price,
            "observed_mark_time": observed_mark_time,
            "supplied_evidence": evidence,
        }
    )
    inputs_hash = _canonical_hash(input_payload)
    report = _empty_report("not_evaluated")
    candidates = {}
    mark = None
    next_state = position_state

    def finish(action, reason, *, intent=None, contributing=(), trace=None):
        nonlocal management, next_state
        reason = reason.value if isinstance(reason, ExitReasonCode) else str(reason)
        if (
            next_state.latched_exit_intent_id
            and management.latched_exit_reason_code is None
            and position_state.latched_exit_intent_id is None
            and intent is not None
            and intent.intent_type in {ExitIntentType.EXIT, ExitIntentType.FLATTEN}
        ):
            management = replace(management, latched_exit_reason_code=reason)
        if (
            next_state.latched_exit_intent_id
            and intent is not None
            and intent.intent_type in {ExitIntentType.EXIT, ExitIntentType.FLATTEN}
        ):
            management = replace(
                management,
                latched_exit_urgency="CRITICAL"
                if "CRITICAL" in {management.latched_exit_urgency, intent.urgency}
                else "NORMAL",
            )
        if position_state.exposure in {
            ExposureState.CLOSED,
            ExposureState.ENTRY_ABORTED,
        }:
            management = original_management
        elif management != original_management and next_state == position_state:
            # Phase-5 checkpoints use a lifecycle version compare-and-swap.
            # Quote extrema and data-gap counter resets need the same durable
            # version advance as a completed-bar decision.
            next_state = reduce_lifecycle(
                position_state,
                LifecycleEvent.STATE_OBSERVED,
                event_id=_event_identity(
                    position_state.position_key, "management", occurred_at, inputs_hash
                ),
                occurred_at=occurred_at,
            )
        identity = {
            "position": position_state.position_key,
            "policy": policy.policy_version,
            "inputs": inputs_hash,
            "action": action.value,
            "reason": reason,
        }
        decision = ExitDecision(
            decision_id=f"exit:decision:{_canonical_hash(identity)}",
            action=action,
            urgency=intent.urgency
            if intent
            else management.latched_exit_urgency or "NORMAL",
            primary_reason_code=reason,
            policy_version=policy.policy_version,
            occurred_at=occurred_at,
            supporting_evidence=report.supporting,
            opposing_evidence=report.opposing,
            contributing_reason_codes=tuple(contributing),
            suppressed_candidates=tuple(
                name for name, passed in candidates.items() if passed and name != reason
            ),
            trace={
                "schema_version": 1,
                "rule_id": reason,
                "input_hash": inputs_hash,
                # Hashes alone cannot reproduce a live decision once the
                # scanner cache expires or the vendor revises a candle.
                "input_snapshot": input_payload,
                "thesis_id": thesis.thesis_id if thesis else None,
                "thesis_hash": _canonical_hash(thesis.to_dict()) if thesis else None,
                "context": market_context.summary() if market_context else None,
                "context_content_hash": _canonical_hash(_json_value(market_context))
                if market_context
                else None,
                "risk": _json_value(risk_snapshot),
                "policy_artifact": artifact,
                "policy_fingerprint": policy_fingerprint,
                "state_before": position_state.to_dict(),
                "state_after": next_state.to_dict(),
                "management_before": original_management.to_dict(),
                "management_after": management.to_dict(),
                "evidence": report.to_dict(),
                "candidates": candidates,
                "intent": intent.to_dict() if intent else None,
                "initiating_reason_code": management.latched_exit_reason_code,
                "supplied_evidence": _json_value(evidence),
                "supplied_evidence_authority": "diagnostic_only",
                "mark_price": mark,
                "profit_context": _profit_context(thesis, management, mark),
                "late_session_review": not risk_snapshot.session.entries_allowed,
                **(trace or {}),
            },
        )
        return ExitEvaluation(next_state, management, decision, intent, report)

    def reconcile(reason=ExitReasonCode.EXEC_BROKER_STATE_UNKNOWN, *, details=None):
        nonlocal next_state
        if position_state.exposure not in {
            ExposureState.CLOSED,
            ExposureState.ENTRY_ABORTED,
        }:
            next_state = reduce_lifecycle(
                position_state,
                LifecycleEvent.RECONCILIATION_REQUIRED,
                event_id=_event_identity(
                    position_state.position_key, "reconcile", occurred_at, str(reason)
                ),
                occurred_at=occurred_at,
            )
        intent = ProposedIntent(
            intent_type=ExitIntentType.RECONCILE,
            position_key=position_state.position_key,
            reason_code=reason.value if isinstance(reason, ExitReasonCode) else reason,
            intent_id=_event_identity(
                position_state.position_key,
                "reconcile-intent",
                occurred_at,
                str(reason),
            ),
            urgency="CRITICAL",
            parent_intent_id=position_state.latched_exit_intent_id,
        )
        return finish(
            ExitAction.RECONCILE_REQUIRED, reason, intent=intent, trace=details
        )

    def exit_result(reason, *, health=None):
        nonlocal next_state
        next_state, intent = _latch_exit(
            position_state, reason=reason.value, occurred_at=occurred_at, health=health
        )
        return finish(ExitAction.REQUEST_EXIT, reason, intent=intent)

    risk_identity_ok = risk_snapshot.position_key == position_state.position_key
    thesis_identity_ok = (
        thesis is None or thesis.position_key == position_state.position_key
    )
    context_identity_ok = (
        thesis is None
        or market_context is None
        or str(market_context.instrument_id) == str(thesis.instrument_id)
    )
    # An unrelated position snapshot cannot authorize this position's reduction.
    effective_risk = (
        risk_snapshot
        if risk_identity_ok
        else replace(
            risk_snapshot,
            broker_state_known=False,
            signed_quantity=0,
            operator_close_requested=False,
            protection_failed=False,
        )
    )
    exposure_at = (
        as_utc(
            thesis.fill_binding.entry_first_fill_at
            or thesis.fill_binding.entry_terminal_at
        )
        if thesis_identity_ok and thesis and thesis.fill_binding
        else None
    )

    def causal_mark(snapshot):
        at = snapshot.mark_time
        return (
            isinstance(at, datetime)
            and at.tzinfo is not None
            and at <= event_time
            and (exposure_at is None or at >= exposure_at)
            and is_fresh_mark(snapshot, policy.hard_risk_policy)
        )

    if not causal_mark(effective_risk):
        effective_risk = replace(effective_risk, mark_price=None, mark_time=None)
    observation = replace(
        effective_risk, mark_price=observed_mark_price, mark_time=observed_mark_time
    )
    if causal_mark(observation) and (
        effective_risk.mark_time is None
        or observed_mark_time >= effective_risk.mark_time
    ):
        effective_risk = observation
    quantity_matches = (
        risk_identity_ok
        and risk_snapshot.broker_state_known
        and (position_state.known_quantity == abs(risk_snapshot.signed_quantity))
    )
    direction_matches = (
        thesis is None
        or not risk_snapshot.signed_quantity
        or (
            thesis.direction == risk_snapshot.direction
            and (risk_snapshot.signed_quantity > 0) == (thesis.direction == "BUY")
        )
    )
    if (
        quantity_matches
        and direction_matches
        and thesis_identity_ok
        and position_state.known_quantity
        and position_state.exposure
        not in {
            ExposureState.CLOSED,
            ExposureState.ENTRY_ABORTED,
            ExposureState.FLAT_PENDING_RECONCILIATION,
        }
        and effective_risk.mark_price is not None
    ):
        mark = float(effective_risk.mark_price)
        management = _update_extrema(
            thesis, management, mark, _timestamp(effective_risk.mark_time)
        )
        management = replace(management, observed_extrema_source="timestamped_mark")
    if (
        risk_identity_ok
        and direction_matches
        and management.confirmed_stop is not None
        and effective_risk.direction in {"BUY", "SELL"}
    ):
        stops = [management.confirmed_stop]
        if (
            _finite(effective_risk.hard_stop_price) is not None
            and effective_risk.hard_stop_price > 0
        ):
            stops.append(effective_risk.hard_stop_price)
        effective_risk = replace(
            effective_risk,
            hard_stop_price=(
                max(stops) if effective_risk.direction == "BUY" else min(stops)
            ),
        )
    if (
        quantity_matches
        and position_state.known_quantity == 0
        and position_state.exposure
        in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}
    ):
        # An account obligation can still cancel other pending entries. A stale
        # position-close command cannot create a new reduction on a closed epoch.
        effective_risk = replace(effective_risk, operator_close_requested=False)
    hard = evaluate_hard_risk(effective_risk, policy.hard_risk_policy)
    if hard.action is not HardRiskAction.HOLD:
        report = _empty_report("hard_risk")
        reason = hard.primary_reason_code.value
        contributors = tuple(item.value for item in hard.contributing_reason_codes)
        has_obligation = any(
            code.startswith(("RISK_", "SESSION_", "OPERATOR_"))
            for code in (reason, *contributors)
        )
        if has_obligation:
            recovery = (
                not quantity_matches
                or not direction_matches
                or position_state.exposure is ExposureState.RECOVERY_REQUIRED
            )
            next_state, intent = _latch_exit(
                position_state,
                reason=reason,
                occurred_at=occurred_at,
                flatten=hard.action is HardRiskAction.FLATTEN_ACCOUNT,
                recovery=recovery,
            )
            action = (
                ExitAction.RECONCILE_REQUIRED
                if hard.action is HardRiskAction.RECONCILE_REQUIRED
                or (recovery and hard.action is not HardRiskAction.FLATTEN_ACCOUNT)
                else ExitAction.REQUEST_EXIT
            )
            return finish(
                action,
                reason,
                intent=intent,
                contributing=contributors,
                trace={
                    "hard_risk_action": hard.action.value,
                    "effective_risk": _json_value(effective_risk),
                },
            )
        return reconcile(reason)

    if (
        not risk_identity_ok
        or not thesis_identity_ok
        or not context_identity_ok
        or not quantity_matches
        or not direction_matches
    ):
        return reconcile(details={"input_identity_or_exposure_mismatch": True})
    if position_state.exposure in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}:
        return finish(
            ExitAction.HOLD,
            ExitReasonCode.BROKER_EXTERNAL_CLOSE,
            trace={"terminal_no_exposure": True},
        )
    if (
        position_state.exposure is ExposureState.FLAT_PENDING_RECONCILIATION
        or position_state.known_quantity == 0
    ):
        return reconcile(ExitReasonCode.EXEC_FILL_ATTRIBUTION_PENDING)
    if (
        position_state.latched_exit_intent_id
        or position_state.exposure is ExposureState.EXIT_PENDING
    ):
        if position_state.exposure is ExposureState.RECOVERY_REQUIRED:
            return reconcile()
        return finish(
            ExitAction.MANAGE_PENDING_INTENT, ExitReasonCode.EXEC_INTENT_ALREADY_PENDING
        )
    if position_state.exposure is ExposureState.RECOVERY_REQUIRED:
        return reconcile()
    if position_state.thesis_health is ThesisHealth.INVALIDATED:
        next_state, intent = _latch_exit(
            position_state,
            reason=ExitReasonCode.THESIS_STRUCTURE_ACCEPTANCE_FAILED.value,
            occurred_at=occurred_at,
            recovery=True,
        )
        return finish(
            ExitAction.RECONCILE_REQUIRED,
            ExitReasonCode.EXEC_BROKER_STATE_UNKNOWN,
            intent=intent,
        )
    if position_state.exposure is not ExposureState.OPEN:
        return finish(ExitAction.HOLD, ExitReasonCode.HOLD_EARLY_DEVELOPMENT)
    if position_state.protection not in {
        ProtectionState.ACTIVE,
        ProtectionState.UPDATE_PENDING,
    }:
        return reconcile(ExitReasonCode.EXEC_PROTECTION_UPDATE_PENDING)

    objective_reached = bool(
        thesis
        and thesis.objective
        and mark is not None
        and (
            mark >= thesis.objective
            if thesis.direction == "BUY"
            else mark <= thesis.objective
        )
    )
    fixed_objective = bool(
        profile
        and profile.objective_mode is ObjectiveMode.FIXED_OBJECTIVE
        and objective_reached
    )
    objective_reason = (
        ExitReasonCode.PROFIT_CONVERGENCE_OBJECTIVE
        if profile and profile.name.value == "range_convergence"
        else ExitReasonCode.PROFIT_FIXED_OBJECTIVE_REACHED
    )
    candidates[objective_reason.value] = fixed_objective

    def bounded_hold(reason, **kwargs):
        if fixed_objective:
            return exit_result(objective_reason)
        return finish(ExitAction.HOLD, reason, **kwargs)

    if not policy_matches:
        # Only an objective explicitly frozen in the thesis remains authoritative
        # across an incompatible policy artifact. New defaults/overrides cannot
        # turn an existing runner into a fixed-target trade.
        frozen_mode = (
            thesis.management_profile.values.get("objective_mode") if thesis else None
        )
        if objective_reached and frozen_mode in {
            "fixed",
            "fixed_objective",
            "legacy_fixed",
        }:
            return exit_result(objective_reason)
        return finish(
            ExitAction.HOLD,
            ExitReasonCode.HOLD_UNKNOWN_THESIS,
            trace={"policy_artifact_mismatch": True},
        )
    if (
        thesis is None
        or thesis.binding_status is not ThesisBindingStatus.BOUND
        or not profile.normal_thesis_management
        or position_state.thesis_health is ThesisHealth.UNKNOWN
    ):
        return bounded_hold(ExitReasonCode.HOLD_UNKNOWN_THESIS)
    if market_context and market_context.session_id != risk_snapshot.session.session_id:
        return bounded_hold(ExitReasonCode.DATA_STALE_CONTEXT)
    primary = market_context.primary_bar if market_context else None
    # Deduplicate by interval, including revisions and out-of-order older bars.
    last_end = as_utc(management.last_processed_primary_bar_end)
    if primary and (
        primary.bar_id == management.last_processed_primary_bar_id
        or (
            last_end
            and isinstance(primary.end, datetime)
            and primary.end.tzinfo is not None
            and primary.end <= last_end
        )
    ):
        return bounded_hold(ExitReasonCode.DATA_DUPLICATE_BAR)
    problem = _context_problem(market_context, event_time)
    if problem:
        # Fast quote-only polls must not erase the previous completed-bar vote.
        if market_context is not None:
            management = _clear_confirmation(management)
        return bounded_hold(problem)
    if not _is_post_entry_bar(thesis, market_context):
        return bounded_hold(ExitReasonCode.HOLD_EARLY_DEVELOPMENT)

    evidence_context = market_context
    higher = market_context.higher_bar
    higher_max_age = (
        market_context.context_policy.max_higher_age_seconds
        if market_context.context_policy
        else 1_200
    )
    if (
        higher is not None
        and isinstance(higher.end, datetime)
        and higher.end.tzinfo is not None
        and (event_time - higher.end).total_seconds() > higher_max_age
    ):
        evidence_context = replace(
            market_context,
            higher_quality=replace(
                market_context.higher_quality, status=ContextQuality.STALE
            ),
        )
    report = build_evidence(
        thesis,
        evidence_context,
        profile,
        tick_size=policy.tick_size,
        management_state=management,
    )
    if _predicate(report, "required_context_usable") is not True:
        management = _clear_confirmation(management)
        return bounded_hold(ExitReasonCode.HOLD_DATA_DEGRADED)
    # Caller-supplied observations are diagnostic only. The common causal
    # builder is the single authority for rules in live, replay and tests.

    previous_close_mfe = management.completed_mfe_r or 0.0
    management = _update_extrema(
        thesis, management, primary.close, primary.end.isoformat(), completed=True
    )
    direction = 1 if thesis.direction == "BUY" else -1
    binding = thesis.fill_binding
    close_r = (
        direction * (primary.close - binding.entry_vwap) / binding.initial_r_per_share
    )
    noise = _finite(
        report.predicates.get("noise_buffer", report.predicates.get("buffer"))
    )
    structure_id = report.predicates.get("favorable_structure_id")
    structure_recovered = _predicate(report, "favorable_structure_recovery") is True
    new_structure = bool(
        structure_id and structure_id != management.favorable_structure_id
    )
    if new_structure:
        management = replace(
            management,
            favorable_structure_id=str(structure_id),
            favorable_structure_price=report.predicates.get(
                "favorable_structure_price"
            ),
            favorable_structure_buffer=report.predicates.get(
                "favorable_structure_trail_buffer"
            ),
            favorable_structure_failure_buffer=report.predicates.get(
                "favorable_structure_buffer"
            ),
            favorable_structure_known_at=report.predicates.get(
                "favorable_structure_known_at"
            ),
        )
    previous_progress = management.progress_close_r or 0.0
    progress = bool(
        noise is not None
        and structure_recovered
        and close_r
        > max(previous_progress, previous_close_mfe)
        + noise / binding.initial_r_per_share
    )
    count = management.eligible_completed_bars + 1
    if close_r > previous_close_mfe:
        management = replace(management, last_close_progress_bar_count=count)
    if progress:
        management = replace(
            management,
            progress_close_r=close_r,
            last_favorable_progress_bar_id=primary.bar_id,
            last_favorable_progress_bar_count=count,
        )
    elif (
        new_structure
        and structure_recovered
        and noise is not None
        and previous_close_mfe
        > max(previous_progress, 0.0) + noise / binding.initial_r_per_share
        and management.completed_mfe_at
        and management.last_close_progress_bar_count is not None
    ):
        # A newly known base can confirm earlier price progress while price is
        # retracing. Retain that peak's time/count; do not describe this lower
        # close as renewed favorable progress or restart its clock.
        peak_id = next(
            (
                bar.bar_id
                for bar in market_context.primary_bars
                if bar.end == as_utc(management.completed_mfe_at)
            ),
            f"completed-close:{management.completed_mfe_at}",
        )
        management = replace(
            management,
            progress_close_r=previous_close_mfe,
            last_favorable_progress_bar_id=peak_id,
            last_favorable_progress_bar_count=management.last_close_progress_bar_count,
        )
    boundary_failure = _predicate(report, "entry_boundary_failure") is True
    boundary_recovery = _predicate(report, "entry_boundary_recovery") is True
    boundary_episode = str(
        report.predicates.get("entry_boundary_id") or "entry_boundary"
    )
    local_failure = _predicate(report, "failed_favorable_structure") is True
    corroborated = local_failure and has_independent_corroborator(report)
    local_episode = str(structure_id) if structure_id else None
    material = any(
        item.materially_opposing
        for item in report.collapsed
        if item.family
        in {
            EvidenceFamily.STRUCTURE,
            EvidenceFamily.VALUE,
            EvidenceFamily.DYNAMICS,
            EvidenceFamily.PARTICIPATION,
        }
    )
    higher_support = _predicate(report, "higher_timeframe_support") is True
    recovery = (
        boundary_recovery
        and not material
        and _predicate(report, "recovery_context_usable") is True
        and (not structure_id or structure_recovered)
    )

    def advance(qualifies, old_count, previous_end, same_episode=True):
        return (
            (
                old_count + 1
                if same_episode and _contiguous(previous_end, primary.start)
                else 1
            )
            if qualifies
            else 0
        )

    failures = advance(
        boundary_failure,
        management.failure_count,
        management.last_failure_bar_end,
        management.failure_episode == boundary_episode,
    )
    local_failures = advance(
        corroborated,
        management.local_failure_count,
        management.last_local_failure_bar_end,
        management.local_failure_episode == local_episode,
    )
    weak = advance(
        material, management.weakening_count, management.last_weakening_bar_end
    )
    recoveries = advance(
        recovery, management.recovery_count, management.last_recovery_bar_end
    )
    # Time requires an adverse price/context episode and no favorable base.
    # Momentum alone cannot close a sound old trade.
    adverse_context = (
        _predicate(report, "vwap_acceptance_failure") is True
        or _predicate(report, "higher_timeframe_failure") is True
    )
    stagnation = bool(
        not structure_id
        and adverse_context
        and material
        and not higher_support
        and not progress
    )
    stagnant = advance(
        stagnation, management.stagnation_count, management.last_stagnation_bar_end
    )
    end = primary.end.isoformat()
    management = replace(
        management,
        eligible_completed_bars=count,
        last_processed_primary_bar_id=primary.bar_id,
        last_processed_primary_bar_end=end,
        failure_count=failures,
        failure_episode=boundary_episode if boundary_failure else None,
        last_failure_bar_end=end if failures else None,
        local_failure_count=local_failures,
        local_failure_episode=local_episode if corroborated else None,
        last_local_failure_bar_end=end if local_failures else None,
        weakening_count=weak,
        last_weakening_bar_end=end if weak else None,
        recovery_count=recoveries,
        last_recovery_bar_end=end if recoveries else None,
        stagnation_count=stagnant,
        last_stagnation_bar_end=end if stagnant else None,
    )
    confirmations = profile.failure_confirmation_bars
    boundary_reason = (
        ExitReasonCode.THESIS_VWAP_ACCEPTANCE_FAILED
        if report.predicates.get("entry_boundary_kind") in {"VWAP", "SESSION_VWAP"}
        else ExitReasonCode.THESIS_BREAKOUT_FAILED
        if profile.name.value == "breakout_follow_through"
        else ExitReasonCode.THESIS_STRUCTURE_ACCEPTANCE_FAILED
    )
    candidates[boundary_reason.value] = failures >= confirmations
    giveback = max(0.0, (management.completed_mfe_r or 0.0) - close_r)
    reversal = bool(
        local_failures >= confirmations
        and not higher_support
        and management.last_favorable_progress_bar_id
        and (management.completed_mfe_r or 0) >= profile.profit_reversal_min_mfe_r
        and giveback >= profile.profit_reversal_min_giveback_r
    )
    candidates[ExitReasonCode.PROFIT_REVERSAL_CONFIRMED.value] = reversal
    candidates[ExitReasonCode.THESIS_MULTI_FAMILY_FAILURE.value] = (
        local_failures >= confirmations and not higher_support and not reversal
    )
    no_progress = bool(
        profile.review_horizon_bars is not None
        and count >= profile.review_horizon_bars
        and (
            management.last_close_progress_bar_count is None
            or count - management.last_close_progress_bar_count
            >= profile.review_horizon_bars
        )
    )
    timed_out = no_progress and stagnant >= confirmations
    candidates[ExitReasonCode.TIME_NO_PROGRESS_CONFIRMED.value] = timed_out
    if failures >= confirmations:
        return exit_result(boundary_reason, health=ThesisHealth.INVALIDATED)
    if local_failures >= confirmations and not higher_support:
        reason = (
            ExitReasonCode.PROFIT_REVERSAL_CONFIRMED
            if reversal
            else ExitReasonCode.THESIS_MULTI_FAMILY_FAILURE
        )
        return exit_result(reason, health=ThesisHealth.INVALIDATED)
    if fixed_objective:
        return exit_result(objective_reason)
    if timed_out:
        reason = (
            ExitReasonCode.SESSION_LATE_MANAGEMENT
            if not risk_snapshot.session.entries_allowed
            else ExitReasonCode.TIME_NO_PROGRESS_CONFIRMED
        )
        return exit_result(reason, health=ThesisHealth.INVALIDATED)

    health = position_state.thesis_health
    if (
        health is ThesisHealth.WEAKENING
        and recoveries >= profile.recovery_confirmation_bars
    ):
        health = ThesisHealth.VALID
    elif weak >= confirmations:
        health = ThesisHealth.WEAKENING
    terminal = as_utc(binding.entry_terminal_at)
    bars = tuple(
        bar for bar in market_context.primary_bars[-3:] if bar.start >= terminal
    )
    consolidating = (
        len(bars) == 3
        and all(a.end == b.start for a, b in zip(bars, bars[1:]))
        and max(bar.low for bar in bars) <= min(bar.high for bar in bars)
        and not boundary_failure
        and not local_failure
        and not progress
    )
    pullback = (
        not boundary_failure
        and not local_failure
        and any(
            item.predicate == "CONTRACTING_PULLBACK_PARTICIPATION"
            for item in report.supporting
        )
    )
    development = (
        DevelopmentPhase.FAVORABLE
        if progress
        else DevelopmentPhase.PULLBACK
        if pullback
        else DevelopmentPhase.CONSOLIDATING
        if consolidating
        else DevelopmentPhase.DEVELOPING
    )
    next_state = reduce_lifecycle(
        position_state,
        LifecycleEvent.STATE_OBSERVED,
        event_id=_event_identity(
            position_state.position_key, "bar", occurred_at, primary.bar_id
        ),
        occurred_at=occurred_at,
        thesis_health=health,
        development=development,
    )
    pending_stop = (
        position_state.protection is ProtectionState.UPDATE_PENDING
        or management.requested_stop is not None
    )
    trail_considered = bool(
        (progress or new_structure)
        and management.last_favorable_progress_bar_id
        and structure_recovered
    )
    if trail_considered and not pending_stop:
        trail = _structural_trail(
            thesis,
            profile,
            management,
            policy.tick_size,
            mark,
            effective_risk.hard_stop_price,
        )
        if trail is not None:
            management = replace(management, requested_stop=trail)
            next_state = replace(next_state, protection=ProtectionState.UPDATE_PENDING)
            intent = ProposedIntent(
                intent_type=ExitIntentType.TIGHTEN_STOP,
                position_key=position_state.position_key,
                reason_code=ExitReasonCode.PROFIT_STRUCTURE_TRAIL.value,
                intent_id=_event_identity(
                    position_state.position_key,
                    "tighten-stop",
                    occurred_at,
                    primary.bar_id,
                ),
                stop_price=trail,
                details={
                    "confirmed_stop": management.confirmed_stop,
                    "level_id": management.favorable_structure_id,
                },
            )
            return finish(
                ExitAction.TIGHTEN_STOP,
                ExitReasonCode.PROFIT_STRUCTURE_TRAIL,
                intent=intent,
            )
    if pending_stop:
        reason = ExitReasonCode.EXEC_PROTECTION_UPDATE_PENDING
    elif corroborated and higher_support:
        reason = ExitReasonCode.HOLD_HIGHER_TIMEFRAME_SUPPORT
    elif profile.objective_mode is ObjectiveMode.STRUCTURE_RUNNER and objective_reached:
        reason = ExitReasonCode.HOLD_OBJECTIVE_REVIEW_ZONE
    elif health is ThesisHealth.WEAKENING:
        reason = ExitReasonCode.HOLD_THESIS_WEAKENING
    elif failures or local_failures or weak:
        reason = ExitReasonCode.HOLD_CONFIRMATION_PENDING
    elif pullback:
        reason = ExitReasonCode.HOLD_HEALTHY_PULLBACK
    elif consolidating:
        reason = ExitReasonCode.HOLD_CONSOLIDATION
    elif trail_considered:
        reason = ExitReasonCode.HOLD_NO_VALID_STOP_IMPROVEMENT
    elif progress:
        reason = ExitReasonCode.HOLD_TREND_CONTINUATION
    elif position_state.development is DevelopmentPhase.EARLY:
        reason = ExitReasonCode.HOLD_EARLY_DEVELOPMENT
    else:
        reason = ExitReasonCode.HOLD_THESIS_VALID
    return finish(ExitAction.HOLD, reason)
