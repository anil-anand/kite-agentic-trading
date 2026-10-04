"""Executable, isolated candidate policy adapter for fixed entry opportunities.

The caller supplies actual entry fills and a research coordinator.  Completed
market events and independent clock events drive the same policy and lifecycle
reducer as live; every proposed reduction uses the common cancel/reconcile
handoff.  No live service or default journal is imported here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

import pandas as pd

from ..broker_models import ExecutionNamespace, OrderRole
from ..exit_management.engine import ExitEvaluation, ExitPolicy, evaluate_exit
from ..exit_management.models import (
    ExitAction,
    ExposureState,
    LifecycleEvent,
    ManagementState,
    PositionState,
    ProtectionState,
    reduce_lifecycle,
)
from ..exit_management.profiles import ObjectiveMode, resolve_profile
from ..exit_management.thesis import EntryThesis
from ..market_context import MarketContext
from ..order_lifecycle import IntentType, OrderLifecycleCoordinator
from ..reduction_policy import ReductionOrderPolicy
from ..replay import serialize_replay_artifact
from ..risk_rules import HardRiskSnapshot
from ..session_clock import SessionClock, SessionPolicy
from ..time_utils import as_utc
from .simulated_broker import SimulatedBroker


@dataclass
class CandidatePosition:
    thesis: EntryThesis
    state: PositionState
    management: ManagementState
    policy: ExitPolicy
    broker_position_key: str
    coordinator_intent_id: str | None = None
    tighten_intent_id: str | None = None
    exit_market_required: bool = False


class CandidateRunner:
    """One research account, with deterministic symbol ordering and MTM risk.

    This is an exit-only experiment.  Entry opportunities must already have
    passed the study's declared admission policy.  It does not claim portfolio
    entry-selection parity from a set of fixed opportunities.
    """

    def __init__(
        self,
        *,
        broker: SimulatedBroker,
        coordinator: OrderLifecycleCoordinator,
        session_policy: SessionPolicy = SessionPolicy(),
        daily_loss_limit: float | None = None,
        dynamic_entries: bool = False,
        reduction_policy: ReductionOrderPolicy = ReductionOrderPolicy(),
    ):
        if not isinstance(broker, SimulatedBroker) or broker.namespace not in {
            ExecutionNamespace.REPLAY,
            ExecutionNamespace.PAPER,
        }:
            raise ValueError("candidate research requires an isolated simulated broker")
        # A caller must deliberately choose research storage; the default live
        # journal must never become an accidental replay destination.
        journal_path = Path(coordinator.journal.db_path).resolve()
        live_root = (Path.home() / ".kite-agentic-trading").resolve()
        if journal_path.is_relative_to(live_root):
            raise ValueError("candidate research cannot use the live storage directory")
        if daily_loss_limit is not None:
            broker._finite_price(daily_loss_limit, "daily loss limit")
        self.broker = broker
        self.broker.target_execution_model = (
            "PRECOMMITTED_LIMIT_WITH_COORDINATOR_HANDOFF"
        )
        self.coordinator = coordinator
        self.clock = SessionClock(session_policy)
        self.daily_loss_limit = daily_loss_limit
        self.dynamic_entries = dynamic_entries
        self.reduction_policy = reduction_policy
        self.daily_loss_latched = False
        self.positions: dict[str, CandidatePosition] = {}
        self.evaluations: list[ExitEvaluation] = []
        self.recorded_decisions: list[dict] = []
        self._position_artifacts: dict[str, dict] = {}
        self._session_artifact: dict | None = None
        self._execution_artifact: dict | None = None
        self.execution_results: list[dict] = []
        self.equity_curve: list[dict] = []
        self._mark_times = broker._mark_times
        self._last_event: datetime | None = None
        self._session_id: str | None = None
        self._session_start_equity = broker.initial_capital

    def register_position(
        self,
        *,
        thesis: EntryThesis,
        state: PositionState,
        management: ManagementState | None = None,
        policy: ExitPolicy = ExitPolicy(),
    ) -> None:
        """Bind an immutable thesis/checkpoint to verified simulated entry fills."""
        symbol = thesis.symbol
        position = self.broker.positions.get(symbol)
        previous = self.positions.get(symbol)
        if (
            previous is not None
            and not (
                self.dynamic_entries
                and previous.state.exposure
                in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}
                and previous.state.position_key != state.position_key
            )
        ) or position is None:
            raise ValueError("register a filled position exactly once per runner")
        if (
            state.position_key
            not in {
                self.broker._key_for(symbol).as_string(),
                self.broker._key_for(symbol).as_string() + ":" + thesis.position_epoch,
            }
            or thesis.position_key != state.position_key
            or state.known_quantity != position["quantity"]
            or thesis.direction != position["direction"]
            or (
                thesis.fill_binding is None
                and not (
                    self.dynamic_entries and state.thesis_health.value == "UNKNOWN"
                )
            )
            or (
                thesis.fill_binding is not None
                and (
                    abs(thesis.fill_binding.entry_vwap - position["entry_price"]) > 1e-8
                    or thesis.fill_binding.filled_quantity
                    != position["initial_quantity"]
                )
            )
            or state.exposure is not ExposureState.OPEN
            or state.protection is not ProtectionState.ACTIVE
            or state.latched_exit_intent_id is not None
        ):
            raise ValueError("candidate checkpoint must match actual entry fills")
        if any(
            order["symbol"] == symbol and order["role"] == OrderRole.ENTRY.value
            for order in self.broker.pending_orders
        ):
            raise ValueError("fixed entry registration requires a terminal entry")
        stop = self.broker.get_order(position.get("stop_order_id"))
        if (
            not stop
            or stop["remaining_quantity"] != position["quantity"]
            or stop["status"]
            not in (
                {"OPEN", "TRIGGER PENDING", "TRIGGERED"}
                if self.dynamic_entries
                else {"OPEN", "TRIGGER PENDING"}
            )
            or stop["side"] == thesis.direction
        ):
            raise ValueError(
                "registered position requires confirmed residual protection"
            )
        memory = management or ManagementState()
        self.positions[symbol] = CandidatePosition(
            thesis,
            state,
            replace(memory, confirmed_stop=position["sl"]),
            policy,
            self.broker._key_for(symbol).as_string(),
        )
        self._position_artifacts[state.position_key] = {
            "broker_position_key": self.broker._key_for(symbol).as_string(),
            "thesis": thesis.to_dict(),
            "initial_position_state": state.to_dict(),
            "initial_management_state": self.positions[symbol].management.to_dict(),
            "exit_policy": serialize_replay_artifact(policy),
            "resolved_profile": serialize_replay_artifact(
                resolve_profile(
                    thesis.management_profile, overrides=policy.profile_overrides
                )
            ),
        }
        # Freeze the same price barrier used by quote evaluation. OHLC touches
        # enter the common coordinator before the simulator can execute them.
        position["target"] = self._fixed_objective(thesis, policy)
        self.coordinator_record_fills(symbol)

    def _fixed_objective(self, thesis, policy):
        profile = resolve_profile(
            thesis.management_profile, overrides=policy.profile_overrides
        )
        return (
            thesis.objective
            if profile.objective_mode is ObjectiveMode.FIXED_OBJECTIVE
            else None
        )

    def _on_fixed_objective_touch(self, symbol, at, target):
        managed = self.positions[symbol]
        if managed.coordinator_intent_id:
            return None
        reason = "PROFIT_FIXED_OBJECTIVE_REACHED"
        response = self._submit_reduction(symbol, at, reason, fixed_limit=target)
        managed.coordinator_intent_id = response.intent_id
        managed.state = reduce_lifecycle(
            managed.state,
            LifecycleEvent.EXIT_REQUESTED,
            event_id=f"objective:{response.intent_id}",
            occurred_at=at,
            exit_intent_id=response.intent_id,
        )
        managed.management = replace(
            managed.management,
            latched_exit_reason_code=reason,
            latched_exit_urgency="NORMAL",
        )
        self.execution_results.append(
            {
                "source": "PRECOMMITTED_FIXED_OBJECTIVE",
                "intent_id": response.intent_id,
                "order_id": response.broker_order_id,
                "state": response.state,
            }
        )
        if response.broker_order_id:
            self.broker.positions[symbol]["target"] = None
        return response.broker_order_id

    def coordinator_record_fills(self, symbol: str) -> None:
        managed = self.positions[symbol]
        self.broker.reconcile_fills_with(self.coordinator, managed.broker_position_key)

    def _reconcile(self, symbol: str, at: datetime) -> None:
        managed = self.positions[symbol]
        state = managed.state
        if state.exposure in {ExposureState.CLOSED, ExposureState.ENTRY_ABORTED}:
            return
        self.coordinator_record_fills(symbol)
        position = self.broker.positions.get(symbol)
        quantity = position["quantity"] if position else 0
        if managed.tighten_intent_id and position:
            stop = self.broker.get_order(position.get("stop_order_id"))
            requested = managed.management.requested_stop
            if (
                stop
                and stop["status"] in {"OPEN", "TRIGGER PENDING"}
                and stop["position_key"] == managed.broker_position_key
                and stop["side"] != managed.thesis.direction
                and stop["remaining_quantity"] == quantity
                and (requested is not None and stop["trigger_price"] == requested)
            ):
                managed.management = replace(
                    managed.management, confirmed_stop=requested, requested_stop=None
                )
                state = reduce_lifecycle(
                    state,
                    LifecycleEvent.STATE_OBSERVED,
                    event_id=f"stop-reconciled:{managed.tighten_intent_id}",
                    occurred_at=at,
                    protection=ProtectionState.ACTIVE,
                )
                managed.state = state
                self.coordinator.journal.complete_order_intent(
                    managed.tighten_intent_id
                )
                managed.tighten_intent_id = None
        if managed.coordinator_intent_id:
            projection = self.coordinator.journal.get_order_intent_projection(
                managed.coordinator_intent_id
            )
            attempt = projection.get("latest_attempt") if projection else None
            if attempt:
                order = self.broker.get_order(attempt.get("broker_order_id"))
                if (
                    order
                    and order["status"]
                    not in {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}
                    and self.reduction_policy.cancellation_due(
                        order_type=order["order_type"],
                        submitted_at=order["submitted_at"],
                        now=at,
                        market_required=managed.exit_market_required,
                    )
                ):
                    managed.exit_market_required = True
                    self.broker.cancel_order(order["order_id"], timestamp=at)
                    order = self.broker.get_order(order["order_id"])
                self.coordinator.observe_order(
                    managed.coordinator_intent_id,
                    order,
                )
        event_id = f"broker:{symbol}:{at.isoformat()}:{len(self.broker.fills)}"
        if quantity == 0:
            # Terminal means flat AND no working orders.  Cancel entry remainders
            # before acknowledging closure; late fills must not reopen the book.
            for order in list(self.broker.pending_orders):
                if order["symbol"] == symbol:
                    self.broker.cancel_order(order["order_id"], timestamp=at)
            event = (
                LifecycleEvent.RECONCILED_FLAT
                if state.exposure is ExposureState.RECOVERY_REQUIRED
                else LifecycleEvent.FLAT_OBSERVED
            )
            if state.exposure is not ExposureState.FLAT_PENDING_RECONCILIATION:
                state = reduce_lifecycle(
                    state,
                    event,
                    event_id=event_id,
                    occurred_at=at,
                    known_quantity=0,
                    protection=ProtectionState.NONE_FLAT,
                )
            managed.state = reduce_lifecycle(
                state,
                LifecycleEvent.FLAT_CONFIRMED,
                event_id=event_id + ":clean",
                occurred_at=at,
                known_quantity=0,
                protection=ProtectionState.NONE_FLAT,
            )
            if managed.coordinator_intent_id:
                self.coordinator.journal.complete_order_intent(
                    managed.coordinator_intent_id
                )
        elif quantity != state.known_quantity:
            managed.state = reduce_lifecycle(
                state,
                LifecycleEvent.STATE_OBSERVED,
                event_id=event_id,
                occurred_at=at,
                known_quantity=quantity,
            )

    def _submit_reduction(self, symbol, at, reason, *, hard=False, fixed_limit=None):
        managed = self.positions[symbol]
        position = self.broker.positions[symbol]
        side = "SELL" if position["direction"] == "BUY" else "BUY"
        managed.exit_market_required |= hard
        if managed.coordinator_intent_id:
            if hard:
                self.coordinator.journal.escalate_order_intent(
                    managed.coordinator_intent_id, reason
                )
            projection = self.coordinator.journal.get_order_intent_projection(
                managed.coordinator_intent_id
            )
            latest = (projection or {}).get("latest_attempt") or {}
            order = self.broker.get_order(latest.get("broker_order_id"))
            if (
                order
                and order["status"]
                not in {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}
                and self.reduction_policy.cancellation_due(
                    order_type=order["order_type"],
                    submitted_at=order["submitted_at"],
                    now=at,
                    market_required=managed.exit_market_required,
                )
            ):
                managed.exit_market_required = True
                self.broker.cancel_order(order["order_id"], timestamp=at)
                self.coordinator.observe_order(
                    managed.coordinator_intent_id,
                    self.broker.get_order(order["order_id"]),
                )
            if not hard:
                reason = projection.get("reason") or reason
        return self.coordinator.handoff_with_broker_adapter(
            broker=self.broker,
            position_key=managed.broker_position_key,
            role=OrderRole.REDUCTION,
            side=side,
            requested_quantity=position["quantity"],
            payload={
                "tradingsymbol": symbol,
                "timestamp": at.isoformat(),
                "reason": reason,
                "role": OrderRole.REDUCTION.value,
                **(
                    {"order_type": "LIMIT", "price": fixed_limit}
                    if fixed_limit is not None
                    else self.reduction_policy.order_fields(
                        side=side,
                        mark=self.broker._prices.get(symbol),
                        hard=hard,
                        market_required=managed.exit_market_required,
                    )
                ),
                "reduction_policy": None
                if fixed_limit is not None
                else asdict(self.reduction_policy),
                "market_required": managed.exit_market_required,
            },
            stop_order_id=position.get("stop_order_id"),
            reason=reason,
            hard=hard,
            existing_intent_id=managed.coordinator_intent_id,
        )

    def on_event(
        self,
        at: datetime,
        *,
        candles: Mapping[str, Mapping] | None = None,
        contexts: Mapping[str, MarketContext] | None = None,
    ) -> tuple[ExitEvaluation, ...]:
        """Process synchronized completed bars or an independent clock event.

        ``date`` is candle start; ``at`` is actual availability.  Orders produced
        here cannot fill against this bar's earlier OHLC path.  Clock-only events
        create pending hard obligations even when the data feed has stopped.
        """
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("candidate events require increasing aware timestamps")
        at = as_utc(at)
        if at is None or (self._last_event is not None and at <= self._last_event):
            raise ValueError("candidate events require increasing aware timestamps")
        candles = dict(candles or {})
        contexts = dict(contexts or {})
        if set(self.broker.positions).difference(self.positions) or (
            not self.dynamic_entries
            and any(
                order["role"] == OrderRole.ENTRY.value
                for order in self.broker.pending_orders
            )
        ):
            raise ValueError(
                "exit-only runner requires registered terminal entry fills"
            )
        for symbol, context in contexts.items():
            if (
                symbol not in self.positions
                or context.decision_event_time != at
                or str(context.instrument_id)
                != self.positions[symbol].thesis.instrument_id
            ):
                raise ValueError("context must belong to this position/event")
        for candle in candles.values():
            if (
                not isinstance(candle["date"], datetime)
                or candle["date"].tzinfo is None
            ):
                raise ValueError("candidate candle start must be aware")
            start = as_utc(candle["date"])
            if start is None or (at - start).total_seconds() < 300:
                raise ValueError("candidate candles must be complete before evaluation")
            for field in ("received_at", "available_at"):
                if candle.get(field) is not None:
                    timestamp = as_utc(candle[field])
                    if timestamp is None or timestamp > at:
                        raise ValueError("candidate candle is not yet available")
        session_artifact = serialize_replay_artifact(self.clock.policy)
        execution_artifact = {
            "broker": self.broker.execution_manifest,
            "daily_loss_limit": self.daily_loss_limit,
            "reduction_policy": asdict(self.reduction_policy),
            "timeout_observation": "PROVIDED_EVENTS_AFTER_CANDLE_EXECUTION",
        }
        if (
            self._execution_artifact is not None
            and execution_artifact != self._execution_artifact
        ):
            raise ValueError(
                "candidate execution/risk settings cannot change during a run"
            )
        if (
            self._session_artifact is not None
            and session_artifact != self._session_artifact
        ):
            raise ValueError("candidate session policy cannot change during a run")
        for managed in self.positions.values():
            pinned = self._position_artifacts[managed.state.position_key]
            if serialize_replay_artifact(managed.policy) != pinned["exit_policy"]:
                raise ValueError("candidate exit policy cannot change during a run")
        self._session_artifact = session_artifact
        self._execution_artifact = deepcopy(execution_artifact)
        self._last_event = at
        for symbol in sorted(candles):
            candle = dict(candles[symbol])
            # Receipt of an old bar does not make its historical close fresh.
            candle["available_at"] = as_utc(candle["date"]) + pd.Timedelta(minutes=5)
            self.broker.process_candle(
                symbol, pd.Series(candle), target_handler=self._on_fixed_objective_touch
            )
        for symbol in sorted(self.positions):
            self._reconcile(symbol, at)
        equity = self.broker.current_equity({})
        session = self.clock.snapshot(at)
        if self._session_id is None:
            self._session_id = session.session_id
        elif self._session_id != session.session_id:
            # A fresh session resets the latch only after all old exposure and
            # orders are verified clean.  Overnight residuals retain urgency.
            if not self.broker.positions and not self.broker.pending_orders:
                self.daily_loss_latched = False
                self._session_start_equity = equity
                self._session_id = session.session_id
        if self.daily_loss_limit is not None:
            self.daily_loss_latched |= (
                equity - self._session_start_equity <= -self.daily_loss_limit
            )
        account_flatten = self.daily_loss_latched or session.forced_flatten_due
        if account_flatten:
            for order in list(self.broker.pending_orders):
                if order["role"] == OrderRole.ENTRY.value:
                    self.broker.cancel_order(order["order_id"], timestamp=at)
        results = []
        for symbol, managed in sorted(self.positions.items()):
            if managed.state.exposure in {
                ExposureState.CLOSED,
                ExposureState.ENTRY_ABORTED,
            }:
                continue
            position = self.broker.positions[symbol]
            direction = position["direction"]
            stop = self.broker.get_order(position.get("stop_order_id"))
            protection_failed = (
                not stop
                or stop["status"] not in {"OPEN", "TRIGGER PENDING"}
                or stop["position_key"] != managed.broker_position_key
                or stop["side"] == direction
                or stop["remaining_quantity"] != position["quantity"]
            )
            risk = HardRiskSnapshot(
                session=session,
                position_key=managed.state.position_key,
                signed_quantity=position["quantity"]
                * (1 if direction == "BUY" else -1),
                direction=direction,
                mark_price=self.broker._prices.get(symbol),
                mark_time=self._mark_times.get(symbol),
                hard_stop_price=managed.thesis.initial_stop,
                daily_loss_latched=self.daily_loss_latched,
                protection_failed=protection_failed
                and not managed.coordinator_intent_id,
            )
            evaluation = evaluate_exit(
                managed.thesis,
                managed.state,
                contexts.get(symbol),
                risk,
                managed.policy,
                management_state=managed.management,
            )
            # evaluate_exit freezes the full input snapshot before reducing it.
            # Retain its complete pure output before dispatch: a broker failure
            # or later protection acknowledgement cannot overwrite this record.
            self.recorded_decisions.append(evaluation.decision.to_dict())
            managed.state = evaluation.next_position_state
            managed.management = evaluation.next_management_state
            self.evaluations.append(evaluation)
            results.append(evaluation)
            if evaluation.decision.action in {
                ExitAction.REQUEST_EXIT,
                ExitAction.MANAGE_PENDING_INTENT,
            }:
                response = self._submit_reduction(
                    symbol,
                    at,
                    evaluation.decision.primary_reason_code,
                    hard=evaluation.decision.urgency == "CRITICAL",
                )
                managed.coordinator_intent_id = response.intent_id
                self.execution_results.append(
                    {
                        "decision_id": evaluation.decision.decision_id,
                        "intent_id": response.intent_id,
                        "order_id": response.broker_order_id,
                        "state": response.state,
                    }
                )
            elif evaluation.decision.action is ExitAction.TIGHTEN_STOP:
                proposal = evaluation.proposed_intent
                stop_limit = stop["order_type"] in {"SL", "SL-LIMIT"}

                def amend(tag):
                    order_id = self.broker.set_protective_stop(
                        symbol, proposal.stop_price, at, stop_limit=stop_limit
                    )
                    self.broker.orders[order_id]["tag"] = tag
                    return order_id

                response = self.coordinator.submit(
                    position_key=managed.broker_position_key,
                    intent_type=IntentType.TIGHTEN,
                    role=OrderRole.PROTECTION,
                    side="SELL" if direction == "BUY" else "BUY",
                    quantity=position["quantity"],
                    payload={
                        "stop_price": proposal.stop_price,
                        "timestamp": at.isoformat(),
                    },
                    submit_order=amend,
                    reason=proposal.reason_code,
                )
                self.execution_results.append(
                    {
                        "decision_id": evaluation.decision.decision_id,
                        "intent_id": response.intent_id,
                        "order_id": response.broker_order_id,
                        "state": response.state,
                    }
                )
                acknowledged = self.broker.get_order(response.broker_order_id)
                if response.state == "REJECTED":
                    managed.management = replace(
                        managed.management, requested_stop=None
                    )
                    managed.state = reduce_lifecycle(
                        managed.state,
                        LifecycleEvent.STATE_OBSERVED,
                        event_id=evaluation.decision.decision_id + ":stop-rejected",
                        occurred_at=at,
                        protection=ProtectionState.ACTIVE,
                    )
                    self.coordinator.journal.complete_order_intent(
                        response.intent_id, state="REJECTED"
                    )
                    continue
                if (
                    not acknowledged
                    or acknowledged["status"] not in {"OPEN", "TRIGGER PENDING"}
                    or acknowledged["position_key"] != managed.broker_position_key
                    or acknowledged["side"] == direction
                    or acknowledged["remaining_quantity"] != position["quantity"]
                    or acknowledged["trigger_price"] != proposal.stop_price
                ):
                    managed.tighten_intent_id = response.intent_id
                    continue
                self.coordinator.observe_order(response.intent_id, acknowledged)
                self.coordinator.journal.complete_order_intent(response.intent_id)
                managed.management = replace(
                    managed.management,
                    confirmed_stop=proposal.stop_price,
                    requested_stop=None,
                )
                managed.state = reduce_lifecycle(
                    managed.state,
                    LifecycleEvent.STATE_OBSERVED,
                    event_id=evaluation.decision.decision_id + ":stop-ack",
                    occurred_at=at,
                    protection=ProtectionState.ACTIVE,
                )
        self.equity_curve.append(
            {
                "timestamp": at,
                "equity": equity,
                "daily_loss_latched": self.daily_loss_latched,
                "stale_symbols": sorted(
                    symbol
                    for symbol in self.broker.positions
                    if symbol not in self._mark_times
                    or (at - self._mark_times[symbol]).total_seconds() > 120
                ),
            }
        )
        return tuple(results)

    def finish(self) -> dict:
        """Censor unresolved exposure; end of data is never a fictional fill."""
        censored = [
            {
                "symbol": symbol,
                "reason": "RESEARCH_END_OF_DATA",
                "quantity": position["quantity"],
                "pending_intent_id": self.positions[symbol].coordinator_intent_id,
            }
            for symbol, position in sorted(self.broker.positions.items())
        ]
        return {
            "manifest": {
                "runner_version": "candidate-execution-v3",
                "entry_policy": "FIXED_ADMITTED_ENTRY_FILL_OPPORTUNITIES",
                "execution": deepcopy(self._execution_artifact["broker"])
                if self._execution_artifact
                else self.broker.execution_manifest,
                "session_policy_version": self.clock.policy.policy_version,
                "session_policy": deepcopy(
                    self._session_artifact
                    or serialize_replay_artifact(self.clock.policy)
                ),
                "position_artifacts": deepcopy(self._position_artifacts),
                "daily_loss_limit": self._execution_artifact["daily_loss_limit"]
                if self._execution_artifact
                else self.daily_loss_limit,
                "exit_policy_versions": sorted(
                    {p.policy.policy_version for p in self.positions.values()}
                ),
                "reduction_policy": deepcopy(
                    self._execution_artifact["reduction_policy"]
                )
                if self._execution_artifact
                else asdict(self.reduction_policy),
                "timeout_observation": "PROVIDED_EVENTS_AFTER_CANDLE_EXECUTION",
                "normal_fill_timing": "NEXT_AVAILABLE_BAR_LIMIT_EXECUTION",
                "objective_observation": "PRECOMMITTED_OHLC_BARRIER_OR_EXPLICIT_QUOTE",
            },
            "trades": self.broker.trades,
            "censored_positions": censored,
            "equity_curve": self.equity_curve,
            "evaluations": self.evaluations,
            "recorded_decisions": deepcopy(self.recorded_decisions),
            "execution_results": self.execution_results,
            "objective_events": deepcopy(
                [
                    event
                    for event in self.broker.events
                    if event["type"]
                    in {
                        "TARGET_BARRIER_TOUCH",
                        "ENTRY_TARGET_AMBIGUITY",
                        "STOP_TARGET_AMBIGUITY",
                    }
                ]
            ),
        }
