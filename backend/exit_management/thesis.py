"""Capture and one-time fill binding for immutable entry theses."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from backend.time_utils import as_utc, now_utc

from .models import (
    CausalInputReference,
    EvidenceObservation,
    ManagementProfileSnapshot,
    PolicySnapshot,
    _freeze,
    thaw,
)

THESIS_SCHEMA_VERSION = "entry-thesis-v1"


class ThesisProvenance(str, Enum):
    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"
    LEGACY_PARTIAL = "LEGACY_PARTIAL"
    UNKNOWN = "UNKNOWN"


class ThesisBindingStatus(str, Enum):
    DRAFT = "DRAFT"
    BOUND = "BOUND"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finite_positive(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return result


def _positive_quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("filled_quantity must be a positive integer")
    return value


@dataclass(frozen=True)
class FillRiskBinding:
    entry_vwap: float
    filled_quantity: int
    entry_terminal_at: Optional[str]
    initial_r_per_share: float
    initial_risk_budget: float
    source_fill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("entry_vwap", "initial_r_per_share", "initial_risk_budget"):
            _finite_positive(getattr(self, name), name)
        _positive_quantity(self.filled_quantity)
        if not math.isclose(
            self.initial_risk_budget,
            self.initial_r_per_share * self.filled_quantity,
        ):
            raise ValueError("fill risk budget does not match filled quantity")
        object.__setattr__(self, "source_fill_ids", tuple(self.source_fill_ids))
        if any(
            not isinstance(item, str) or not item for item in self.source_fill_ids
        ) or len(set(self.source_fill_ids)) != len(self.source_fill_ids):
            raise ValueError("source fill identities must be nonempty and unique")
        if self.entry_terminal_at is not None:
            as_utc(self.entry_terminal_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_vwap": self.entry_vwap,
            "filled_quantity": self.filled_quantity,
            "entry_terminal_at": self.entry_terminal_at,
            "initial_r_per_share": self.initial_r_per_share,
            "initial_risk_budget": self.initial_risk_budget,
            "source_fill_ids": list(self.source_fill_ids),
        }


@dataclass(frozen=True)
class ProvisionalRisk:
    fill_price: float
    filled_quantity: int
    risk_per_share: float
    risk_budget: float

    def __post_init__(self) -> None:
        for name in ("fill_price", "risk_per_share", "risk_budget"):
            _finite_positive(getattr(self, name), name)
        _positive_quantity(self.filled_quantity)
        if not math.isclose(
            self.risk_budget, self.risk_per_share * self.filled_quantity
        ):
            raise ValueError("provisional risk budget does not match filled quantity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_price": self.fill_price,
            "filled_quantity": self.filled_quantity,
            "risk_per_share": self.risk_per_share,
            "risk_budget": self.risk_budget,
        }


@dataclass(frozen=True)
class EntryThesis:
    """Frozen premise captured at entry, with one allowed terminal fill binding."""

    thesis_id: str
    position_key: str
    trade_id: str
    schema_version: str
    revision: int
    provenance: ThesisProvenance
    binding_status: ThesisBindingStatus
    created_at: str
    direction: str
    symbol: str
    exchange: str
    product: str
    instrument_id: str
    position_epoch: str
    strategy: str
    playbook: str
    setup_variant: str
    reasoning: Optional[str]
    planned_entry_price: float
    initial_stop: float
    objective: Optional[float]
    selected_evidence: tuple[EvidenceObservation, ...]
    causal_anchors: Mapping[str, Any]
    input_reference: CausalInputReference
    policy_snapshot: PolicySnapshot
    management_profile: ManagementProfileSnapshot = ManagementProfileSnapshot()
    playbook_version: str = "playbooks-v1"
    selection_inputs: tuple[EvidenceObservation, ...] = ()
    entry_selection_config: Optional[PolicySnapshot] = None
    entry_input: Mapping[str, Any] = field(default_factory=dict)
    fill_binding: Optional[FillRiskBinding] = None
    provisional_risk: Optional[ProvisionalRisk] = None
    legacy_reason_text: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", ThesisProvenance(self.provenance))
        object.__setattr__(
            self, "binding_status", ThesisBindingStatus(self.binding_status)
        )
        if not self.thesis_id or not self.position_key or not self.trade_id:
            raise ValueError("thesis, position and trade identities are required")
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError("thesis direction must be BUY or SELL")
        if self.schema_version != THESIS_SCHEMA_VERSION or self.revision < 1:
            raise ValueError("unsupported thesis schema or revision")
        _finite_positive(self.planned_entry_price, "planned_entry_price")
        _finite_positive(self.initial_stop, "initial_stop")
        if self.objective is not None:
            _finite_positive(self.objective, "objective")
        if self.direction == "BUY" and self.planned_entry_price <= self.initial_stop:
            raise ValueError("long thesis requires a stop below planned entry")
        if self.direction == "SELL" and self.planned_entry_price >= self.initial_stop:
            raise ValueError("short thesis requires a stop above planned entry")
        if (
            self.binding_status is ThesisBindingStatus.BOUND
            and self.fill_binding is None
        ):
            raise ValueError("bound thesis requires terminal fill binding")
        if (
            self.binding_status is ThesisBindingStatus.DRAFT
            and self.fill_binding is not None
        ):
            raise ValueError("draft thesis cannot contain a terminal fill binding")
        if self.fill_binding is not None:
            risk = calculate_provisional_risk(
                self,
                fill_price=self.fill_binding.entry_vwap,
                filled_quantity=self.fill_binding.filled_quantity,
            )
            if not math.isclose(
                risk.risk_per_share, self.fill_binding.initial_r_per_share
            ):
                raise ValueError("bound initial R does not match the original stop")
        object.__setattr__(self, "selected_evidence", tuple(self.selected_evidence))
        object.__setattr__(self, "selection_inputs", tuple(self.selection_inputs))
        object.__setattr__(self, "causal_anchors", _freeze(self.causal_anchors))
        object.__setattr__(self, "entry_input", _freeze(self.entry_input))

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "position_key": self.position_key,
            "trade_id": self.trade_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "provenance": self.provenance.value,
            "binding_status": self.binding_status.value,
            "created_at": self.created_at,
            "direction": self.direction,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "product": self.product,
            "instrument_id": self.instrument_id,
            "position_epoch": self.position_epoch,
            "strategy": self.strategy,
            "playbook": self.playbook,
            "setup_variant": self.setup_variant,
            "reasoning": self.reasoning,
            "planned_entry_price": self.planned_entry_price,
            "initial_stop": self.initial_stop,
            "objective": self.objective,
            "selected_evidence": [item.to_dict() for item in self.selected_evidence],
            "causal_anchors": thaw(self.causal_anchors),
            "input_reference": self.input_reference.to_dict(),
            "policy_snapshot": self.policy_snapshot.to_dict(),
            "management_profile": self.management_profile.to_dict(),
            "playbook_version": self.playbook_version,
            "selection_inputs": [item.to_dict() for item in self.selection_inputs],
            "entry_selection_config": self.entry_selection_config.to_dict()
            if self.entry_selection_config
            else None,
            "entry_input": thaw(self.entry_input),
            "fill_binding": self.fill_binding.to_dict() if self.fill_binding else None,
            "provisional_risk": self.provisional_risk.to_dict()
            if self.provisional_risk
            else None,
            "legacy_reason_text": self.legacy_reason_text,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EntryThesis":
        """Restore recorded inputs only; never reconstruct a premise by rescanning."""

        fields = dict(payload)
        fields["selected_evidence"] = tuple(
            EvidenceObservation(**item) for item in fields["selected_evidence"]
        )
        fields["selection_inputs"] = tuple(
            EvidenceObservation(**item) for item in fields.get("selection_inputs", ())
        )
        fields["input_reference"] = CausalInputReference(**fields["input_reference"])
        fields["policy_snapshot"] = PolicySnapshot(**fields["policy_snapshot"])
        if fields["policy_snapshot"].config_hash != _hash(
            thaw(fields["policy_snapshot"].values)
        ):
            raise ValueError("restored policy values do not match pinned config hash")
        if fields.get("entry_selection_config") is not None:
            entry_config = PolicySnapshot(**fields["entry_selection_config"])
            if entry_config.config_hash != _hash(thaw(entry_config.values)):
                raise ValueError("restored entry config does not match pinned hash")
            fields["entry_selection_config"] = entry_config
        fields["management_profile"] = ManagementProfileSnapshot(
            **fields.get("management_profile", {})
        )
        if fields.get("fill_binding") is not None:
            fields["fill_binding"] = FillRiskBinding(**fields["fill_binding"])
        if fields.get("provisional_risk") is not None:
            fields["provisional_risk"] = ProvisionalRisk(**fields["provisional_risk"])
        return cls(**fields)


def policy_snapshot_from_config(config: Mapping[str, Any]) -> PolicySnapshot:
    """Pin only effective, non-secret settings that can affect management."""

    copied = thaw(_freeze(config))
    return PolicySnapshot(
        policy_version=str(
            copied.get("exitManagement", {}).get(
                "policyVersion", "exit-thesis-state-v1"
            )
        ),
        config_hash=_hash(copied),
        values=copied,
    )


def _context_reference(context: Mapping[str, Any]) -> CausalInputReference:
    primary = context.get("primary_bar") or context.get("primaryBar") or {}
    higher = context.get("higher_bar") or context.get("higherBar") or {}
    policy = context.get("policy") or {}
    primary_id = (
        primary.get("bar_id")
        or primary.get("id")
        or primary.get("end")
        or context.get("primary_bar_id")
    )
    higher_id = (
        higher.get("bar_id")
        or higher.get("id")
        or higher.get("end")
        or context.get("higher_bar_id")
    )
    return CausalInputReference(
        # Submission can be minutes after scanning, especially in confirm mode.
        # Do not relabel the scan's as-of snapshot with the order submission time.
        decision_at=context.get("decision_event_time")
        or context.get("decision_at")
        or context.get("decisionAt"),
        primary_bar_id=str(primary_id) if primary_id else None,
        higher_bar_id=str(higher_id) if higher_id else None,
        context_policy_version=policy.get("policy_version")
        or policy.get("policyVersion")
        or context.get("policy_version")
        or context.get("context_policy_version")
        or context.get("feature_version"),
        source_as_of=context.get("source_as_of") or context.get("sourceAsOf"),
        context_hash=_hash(context) if context else None,
        snapshot=context,
    )


def _evidence_from_signal(signal: Mapping[str, Any]) -> tuple[EvidenceObservation, ...]:
    selected = signal.get("selected_evidence")
    if not isinstance(selected, (list, tuple)):
        # A diagnostic scanner dump does not prove which inputs were selected.
        selected = []
    evidence = []
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        payload = thaw(_freeze(item))
        evidence.append(
            EvidenceObservation(
                family=str(item.get("family") or "unknown"),
                dependency_group=str(
                    item.get("dependency_group")
                    or item.get("dependencyGroup")
                    or (
                        "price_dynamics"
                        if item.get("family") in {"trend", "mean_reversion"}
                        else item.get("family")
                    )
                    or "unknown"
                ),
                direction=str(item.get("direction") or signal.get("direction") or ""),
                strategy_id=(
                    str(item.get("strategy_id"))
                    if item.get("strategy_id") is not None
                    else None
                ),
                score=(
                    float(item["signal_score"])
                    if isinstance(item.get("signal_score"), (int, float))
                    and not isinstance(item.get("signal_score"), bool)
                    else None
                ),
                payload=payload,
            )
        )
    return tuple(evidence)


def _management_profile(
    signal: Mapping[str, Any],
    context: Mapping[str, Any],
    anchors: dict[str, Any],
    config: Mapping[str, Any],
    captured_at: datetime,
) -> ManagementProfileSnapshot:
    """Only name a structural profile when its original premise is recoverable.

    This is provenance validation, not an entry gate or a new exit formula.
    A generic context range is not automatically the boundary of every trigger.
    """

    def timestamp(value: Any) -> Optional[datetime]:
        try:
            return as_utc(value)
        except (TypeError, ValueError):
            return None

    requested = str(signal.get("management_profile") or "unknown_legacy_bounded")
    reference = _context_reference(context)
    decision_at = timestamp(reference.decision_at)
    trigger = anchors.get("trigger_bar") or {}
    trigger_start = timestamp(trigger.get("start"))
    trigger_end = timestamp(trigger.get("end"))
    trigger_available = timestamp(trigger.get("available_at"))
    valid_context = bool(
        decision_at
        and trigger_start
        and trigger_end
        and trigger_available
        and trigger_start < trigger_end <= trigger_available <= decision_at
        and decision_at <= captured_at
        and context.get("normal_decision_eligible", True)
        and context.get("primary_quality") == "VALID"
        and (signal.get("entry_input") or {}).get("mode") != "INCOMPLETE_CANDLE"
    )
    setup_range = anchors.get("setup_range")
    known_structure = anchors.get("known_structure") or []
    valid_range = None
    valid_levels = []
    if valid_context:
        if isinstance(setup_range, Mapping):
            start = timestamp(setup_range.get("start"))
            end = timestamp(setup_range.get("end"))
            known_at = timestamp(setup_range.get("known_at"))
            low, high = setup_range.get("low"), setup_range.get("high")
            if (
                start
                and end
                and known_at
                and start < end <= trigger_start
                and end <= known_at <= decision_at
                and setup_range.get("source_bar_ids")
                and isinstance(low, (int, float))
                and isinstance(high, (int, float))
                and 0 < low < high < float("inf")
            ):
                valid_range = setup_range
        for level in known_structure:
            if not isinstance(level, Mapping):
                continue
            formed_at = timestamp(level.get("formed_at"))
            known_at = timestamp(level.get("known_at"))
            price = level.get("price")
            if (
                formed_at
                and known_at
                and formed_at <= known_at <= decision_at
                and level.get("source_bar_ids")
                and level.get("level_id")
                and isinstance(price, (int, float))
                and 0 < price < float("inf")
            ):
                valid_levels.append(level)
    # Preserve rejected raw inputs in input_reference.snapshot, but never promote
    # a future/unknown level into the usable causal premise.
    anchors["setup_range"] = valid_range
    anchors["known_structure"] = valid_levels
    direction = signal.get("direction", "").upper()
    entry = signal["entryPrice"]
    stop = signal["stopLoss"]
    objective = signal.get("target")
    boundary = None
    if requested == "trend_continuation":
        expected_kind = "SWING_LOW" if direction == "BUY" else "SWING_HIGH"
        candidates = [
            level
            for level in valid_levels
            if level.get("kind") == expected_kind
            and (
                stop <= level["price"] < entry
                if direction == "BUY"
                else entry < level["price"] <= stop
            )
        ]
        if candidates:
            boundary = max(candidates, key=lambda level: as_utc(level["known_at"]))
    elif valid_range and requested == "breakout_follow_through":
        close = trigger.get("close")
        # Keep a range only when the recorded trigger actually broke its edge.
        if isinstance(close, (int, float)) and (
            close > valid_range["high"]
            if direction == "BUY"
            else close < valid_range["low"]
        ):
            boundary = valid_range
    elif valid_range and requested == "range_convergence":
        if objective is not None and (
            stop <= valid_range["low"] <= entry < objective <= valid_range["high"]
            if direction == "BUY"
            else valid_range["low"] <= objective < entry <= valid_range["high"] <= stop
        ):
            boundary = valid_range
    name = requested if boundary is not None else "unknown_legacy_bounded"
    settings = config.get("exitManagement", {})
    return ManagementProfileSnapshot(
        name=name,
        version=str(settings.get("defaultProfileVersion", "management-profiles-v1")),
        structure_status="VALID" if boundary is not None else "MISSING_OR_UNSUPPORTED",
        values={
            "requested_profile": requested,
            "entry_boundary": boundary,
            "objective_mode": "legacy_fixed" if objective is not None else "none",
            "control_policy_version": settings.get(
                "legacyControlPolicyVersion", "legacy-control-v1"
            ),
            "normal_policy_implemented": False,
        },
    )


def capture_entry_thesis(
    signal: Mapping[str, Any],
    *,
    position_key: str,
    trade_id: str,
    position_epoch: str,
    instrument_id: str,
    effective_config: Mapping[str, Any],
    created_at: datetime | str | None = None,
    provenance: ThesisProvenance = ThesisProvenance.SYSTEM,
) -> EntryThesis:
    """Freeze the accepted signal before any order is sent to the broker."""

    timestamp = as_utc(created_at or now_utc())
    if timestamp is None:
        raise ValueError("entry thesis needs an aware creation time")
    direction = str(signal.get("direction", "")).upper()
    planned_entry = _finite_positive(signal.get("entryPrice"), "entryPrice")
    stop = _finite_positive(signal.get("stopLoss"), "stopLoss")
    objective_value = signal.get("target")
    objective = (
        _finite_positive(objective_value, "target")
        if objective_value is not None
        else None
    )
    context = signal.get("market_context")
    if not isinstance(context, Mapping):
        context = {}
    # The phase-4 context summary supplies only known-at levels.  Storing it as
    # received is intentional: neither an execution fill nor a later scan may
    # retrospectively add a swing/range rationale.
    anchors = {
        "setup_range": context.get("setup_range") or context.get("setupRange"),
        "known_structure": context.get("known_structure")
        or context.get("known_swings")
        or context.get("knownSwings"),
        "session_vwap": context.get("session_vwap") or context.get("vwap"),
        "direction_dynamics": context.get("direction_dynamics"),
        "participation": context.get("participation"),
        "volatility": {
            "atr": context.get("atr"),
            "quality": (context.get("observation_quality") or {}).get("atr"),
        },
        "regime": signal.get("regime"),
        "regime_features": signal.get("indicators", {}),
        "trigger_bar": context.get("primary_bar") or context.get("primaryBar"),
    }
    profile = _management_profile(signal, context, anchors, effective_config, timestamp)
    selected_config = signal.get("entry_selection_config")
    selection_config = None
    if isinstance(selected_config, Mapping):
        values = thaw(_freeze(selected_config))
        selection_config = PolicySnapshot(
            policy_version="entry-selection-v1",
            config_hash=_hash(values),
            values=values,
        )
    return EntryThesis(
        thesis_id=str(uuid.uuid4()),
        position_key=position_key,
        trade_id=trade_id,
        schema_version=THESIS_SCHEMA_VERSION,
        revision=1,
        provenance=provenance,
        binding_status=ThesisBindingStatus.DRAFT,
        created_at=timestamp.isoformat(),
        direction=direction,
        symbol=str(signal["tradingsymbol"]),
        exchange=str(signal.get("exchange", "NSE")).upper(),
        product=str(signal.get("product", "MIS")).upper(),
        instrument_id=str(instrument_id),
        position_epoch=str(position_epoch),
        strategy=str(signal.get("strategy", "unknown")),
        playbook=str(signal.get("playbook") or signal.get("strategy") or "unknown"),
        setup_variant=str(signal.get("setup_variant") or "unspecified"),
        reasoning=signal.get("reasoning"),
        planned_entry_price=planned_entry,
        initial_stop=stop,
        objective=objective,
        selected_evidence=_evidence_from_signal(signal),
        selection_inputs=_evidence_from_signal(
            {
                "selected_evidence": signal.get(
                    "selection_inputs", signal.get("raw_signals", [])
                )
            }
        ),
        causal_anchors=anchors,
        input_reference=_context_reference(context),
        policy_snapshot=policy_snapshot_from_config(effective_config),
        management_profile=profile,
        playbook_version=str(signal.get("playbook_version") or "playbooks-v1"),
        entry_selection_config=selection_config,
        entry_input=signal.get("entry_input") or {},
    )


def calculate_provisional_risk(
    thesis: EntryThesis, *, fill_price: float, filled_quantity: int
) -> ProvisionalRisk:
    """Expose partial-fill risk without pretending it is the immutable final R."""

    price = _finite_positive(fill_price, "fill_price")
    _positive_quantity(filled_quantity)
    directional_distance = (
        price - thesis.initial_stop
        if thesis.direction == "BUY"
        else thesis.initial_stop - price
    )
    if directional_distance <= 0:
        raise ValueError("fill is at or beyond the initial protective stop")
    return ProvisionalRisk(
        fill_price=price,
        filled_quantity=filled_quantity,
        risk_per_share=directional_distance,
        risk_budget=directional_distance * filled_quantity,
    )


def bind_terminal_fill(
    thesis: EntryThesis,
    *,
    entry_vwap: float,
    filled_quantity: int,
    terminal_at: datetime | str | None,
    source_fill_ids: tuple[str, ...] = (),
) -> EntryThesis:
    """Return the one permitted fill-bound revision of a draft thesis."""

    if thesis.binding_status is ThesisBindingStatus.BOUND:
        proposed = calculate_provisional_risk(
            thesis, fill_price=entry_vwap, filled_quantity=filled_quantity
        )
        existing = thesis.fill_binding
        if (
            existing
            and source_fill_ids
            and existing.source_fill_ids
            and set(source_fill_ids) != set(existing.source_fill_ids)
        ):
            raise ValueError("terminal fill identities are immutable once established")
        if (
            existing
            and existing.entry_vwap == proposed.fill_price
            and existing.filled_quantity == proposed.filled_quantity
        ):
            return thesis
        raise ValueError("terminal fill binding is immutable once established")
    provisional = calculate_provisional_risk(
        thesis, fill_price=entry_vwap, filled_quantity=filled_quantity
    )
    timestamp = as_utc(terminal_at)
    return replace(
        thesis,
        revision=thesis.revision + 1,
        binding_status=ThesisBindingStatus.BOUND,
        fill_binding=FillRiskBinding(
            entry_vwap=provisional.fill_price,
            filled_quantity=provisional.filled_quantity,
            entry_terminal_at=timestamp.isoformat() if timestamp else None,
            initial_r_per_share=provisional.risk_per_share,
            initial_risk_budget=provisional.risk_budget,
            source_fill_ids=tuple(str(item) for item in source_fill_ids),
        ),
        provisional_risk=provisional,
    )
