"""Causal portfolio research with shared production entries, risk and exits.

Universe events are retained point-in-time screener outputs, including rank.
This adapter does not reconstruct an unobserved historical screener. Orders are
LIMIT, queued after decisions; one completed execution bar is the declared entry
expiry approximation. A partially filled expired entry becomes a protected
terminal allocation. The approximation is pinned, not claimed as broker parity.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping, Sequence

import pandas as pd

from ..accounting import AccountingService
from ..broker_models import BrokerPositionKey, OrderRole
from ..calibration import bucket_success_probability
from ..entry_decisions import evaluate_production_entries
from ..entry_ordering import (
    ENTRY_ORDERING_VERSION,
    PRODUCTION_CANDLE_HISTORY_DAYS,
    entry_signal_order_key,
    round_entry_price_to_tick,
)
from ..exit_management.engine import ExitPolicy
from ..exit_management.models import (
    ExposureState,
    PositionState,
    ProtectionState,
    ThesisHealth,
)
from ..exit_management.thesis import bind_terminal_fill, capture_entry_thesis
from ..financial_eligibility import verified_outcome
from ..journal import TradeJournal
from ..market_context import ContextPolicy, MarketContextService
from ..order_lifecycle import IntentType, OrderLifecycleCoordinator
from ..playbooks import BreakoutPlaybook, MeanReversionPlaybook, TrendPullbackPlaybook
from ..replay import replay_recorded_exit_decision
from ..risk_manager import RiskManager
from ..session_clock import SessionClock
from .candidate_runner import CandidateRunner
from .legacy_control import FAMILIES, LegacyControlRunner, production_strategies
from .metrics_evaluator import MetricsEvaluator
from .research_study import (
    _artifact,
    _at,
    _costs,
    _detached,
    _frames,
    _source_artifact,
    restore_exit_policy,
    restore_session_policy,
)
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy


def _universe(events, metadata, end):
    result = []
    for event in events:
        if set(event) - {"at", "available_at", "symbols"}:
            raise ValueError("unknown universe event fields")
        at = _at(event["at"], "universe at")
        available = _at(event.get("available_at", at), "universe availability")
        if available < at:
            raise ValueError("universe cannot be known before selection")
        symbols = event["symbols"]
        if (
            not isinstance(symbols, list)
            or len(symbols) != len(set(symbols))
            or set(symbols) - set(metadata)
        ):
            raise ValueError("universe needs unique symbols with instrument metadata")
        if max(at, available) < end:
            result.append(
                {"at": at, "available_at": available, "symbols": list(symbols)}
            )
    result.sort(key=lambda e: e["at"])
    if len({e["at"] for e in result}) != len(result):
        raise ValueError("duplicate universe revisions require a separate study")
    return result


def run_portfolio_study(
    *,
    market_data: Mapping[str, pd.DataFrame],
    universe_events: Sequence[Mapping],
    instrument_metadata: Mapping,
    strategy_config: Mapping,
    risk_config: Mapping,
    candidate_policy: ExitPolicy | Mapping,
    test_start: datetime,
    test_end: datetime,
    source_revision: str,
    warmup_start: datetime | None = None,
    initial_capital: float = 100000,
    execution_policy=None,
    cost_policy=None,
    context_policy=None,
    session_policy=None,
    legacy_policy=None,
    calibration_history: Sequence[Mapping] = (),
) -> dict:
    """Execute two independently evolving accounts from the same opportunity data.

    Scores, entry counts, risk capacity, cooldowns and later admissions react to
    each branch's actual exits. Rejections and censored orders remain in output.
    Empty trading days receive explicit marked session equity observations.
    """
    start, end = _at(test_start, "test_start"), _at(test_end, "test_end")
    warmup = _at(warmup_start, "warmup_start") if warmup_start is not None else start
    if not warmup <= start < end:
        raise ValueError("invalid portfolio study boundaries")
    session = restore_session_policy(session_policy)
    context = ContextPolicy(**_detached(context_policy or ContextPolicy()))
    execution = SimulationExecutionPolicy(
        **_detached(execution_policy or SimulationExecutionPolicy())
    )
    policy = restore_exit_policy(candidate_policy)
    costs = _costs(cost_policy)
    prior_calibration = _detached(calibration_history)
    for row in prior_calibration:
        if not isinstance(row, dict) or not verified_outcome(row):
            raise ValueError(
                "calibration history requires financially verified closed outcomes"
            )
        if (
            not _at(row["exit_time"], "calibration exit_time") < start
            or not _at(row["recorded_at"], "calibration recorded_at") < start
        ):
            raise ValueError(
                "initial calibration outcomes must be known before scoring"
            )
        if (
            not isinstance(row.get("strategy"), str)
            or isinstance(row.get("confidence"), bool)
            or not isinstance(row.get("confidence"), (int, float))
            or not isfinite(row["confidence"])
        ):
            raise ValueError(
                "calibration history requires strategy and finite confidence score"
            )
    metadata = _detached(instrument_metadata)
    if not metadata or any(
        not {"instrument_id", "sector"} <= set(item)
        or set(item) - {"instrument_id", "sector", "tick_size"}
        or not all(
            isinstance(v, str) and v.strip()
            for v in (item.get("instrument_id"), item.get("sector"))
        )
        for item in metadata.values()
    ):
        raise ValueError(
            "explicit immutable instrument_id and sector metadata required"
        )
    for item in metadata.values():
        tick = item.setdefault("tick_size", policy.tick_size)
        if (
            isinstance(tick, bool)
            or not isinstance(tick, (int, float))
            or not isfinite(tick)
            or tick <= 0
        ):
            raise ValueError("instrument tick_size must be finite and positive")
    universe = _universe(universe_events, metadata, end)
    if not any(e["available_at"] <= start and e["at"] <= start for e in universe):
        raise ValueError("point-in-time universe must be known at scoring start")
    if strategy_config.get("evaluateOnIncompleteCandle"):
        raise ValueError(
            "historical final candles cannot reconstruct incomplete entry snapshots"
        )
    strategies = production_strategies()
    if set(strategy_config) - set(strategies) - {"evaluateOnIncompleteCandle"}:
        raise ValueError("unknown production strategy configuration")
    config = _detached(risk_config)
    required = {
        "maxDailyLoss",
        "noNewTradesAfter",
        "maxCapitalPerTrade",
        "leverageMultiplier",
    }
    if not required <= set(config):
        raise ValueError("explicit portfolio risk and sizing settings required")
    for name in ("maxDailyLoss", "maxCapitalPerTrade", "leverageMultiplier"):
        value = config[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ValueError("risk limits must be finite positive numbers")
    frames = _frames(
        market_data,
        symbols=set(metadata),
        warmup_start=warmup,
        test_end=end,
        checkpoint_at=end - timedelta(microseconds=1),
        session=session,
        context=context,
    )
    timeline = {start: {}}
    for symbol, frame in frames.items():
        for row in frame.to_dict("records"):
            available = max(
                [
                    row["date"]
                    + timedelta(minutes=5, seconds=context.availability_delay_seconds),
                    *[row[k] for k in ("available_at", "received_at") if k in row],
                ],
            )
            row = dict(row, available_at=available, received_at=available)
            if symbol in timeline.setdefault(available, {}):
                raise ValueError(
                    "multiple bars received together require finer event input"
                )
            timeline[available][symbol] = row
    clock = SessionClock(session)
    day = start.astimezone(session.exchange_timezone).date()
    while (
        day
        <= (end - timedelta(microseconds=1))
        .astimezone(session.exchange_timezone)
        .date()
    ):
        for wall in (
            session.open_time,
            session.forced_flatten_time,
            session.close_time,
        ):
            at = datetime.combine(day, wall, tzinfo=session.exchange_timezone)
            if start <= at < end and clock.snapshot(at).is_trading_day:
                timeline.setdefault(at, {})
        day += timedelta(days=1)
    branches = {}
    context_cache = {}
    drivers = {}
    with TemporaryDirectory(prefix="kite-portfolio-research-") as directory:
        for name in ("candidate", "control"):
            drivers[name] = _branch(
                name=name,
                directory=directory,
                timeline=timeline,
                frames=frames,
                metadata=metadata,
                universe=universe,
                start=start,
                end=end,
                capital=initial_capital,
                execution=execution,
                costs=costs,
                session=session,
                context=context,
                config=config,
                strategy_config=_detached(strategy_config),
                policy=policy,
                legacy_policy=legacy_policy,
                calibration_history=prior_calibration,
                context_cache=context_cache,
            )
        try:
            for at in sorted(timeline):
                for driver in drivers.values():
                    if next(driver) != at:
                        raise AssertionError("portfolio branch clocks diverged")
                context_cache.clear()
            for name, driver in drivers.items():
                try:
                    next(driver)
                except StopIteration as complete:
                    branches[name] = complete.value
                else:
                    raise AssertionError("portfolio branch did not complete")
        finally:
            for driver in drivers.values():
                driver.close()
    return _detached(
        {
            "schema_version": "portfolio-study-v1",
            "scope": "FULL_PORTFOLIO_POINT_IN_TIME_UNIVERSE",
            "production_admission_parity": True,
            "operational_evidence": False,
            "missing_market_symbols": sorted(set(metadata) - set(frames)),
            "universe_provenance": "PREREGISTERED_POINT_IN_TIME_SELECTION_EVENTS",
            "artifacts": {
                "inputs": _artifact(
                    {
                        "universe_events": universe,
                        "instrument_metadata": metadata,
                        "strategy_config": strategy_config,
                        "risk_config": config,
                        "start": start,
                        "end": end,
                        "warmup_start": warmup,
                        "initial_capital": initial_capital,
                        "calibration_history": prior_calibration,
                    }
                ),
                "policies": _artifact(
                    {
                        "candidate": policy,
                        "legacy": legacy_policy or {},
                        "execution": execution,
                        "context": context,
                        "session": session,
                        "costs": vars(costs),
                    }
                ),
                "data": _artifact({s: f.to_dict("records") for s, f in frames.items()}),
                "source": _source_artifact(source_revision),
            },
            **branches,
        }
    )


def _branch(
    *,
    name,
    directory,
    timeline,
    frames,
    metadata,
    universe,
    start,
    end,
    capital,
    execution,
    costs,
    session,
    context,
    config,
    strategy_config,
    policy,
    legacy_policy,
    calibration_history,
    context_cache,
):
    broker = SimulatedBroker(
        capital,
        execution_policy=execution,
        cost_service=costs,
        account_id=f"portfolio-{name}",
    )
    for symbol, item in metadata.items():
        broker._position_keys[symbol] = BrokerPositionKey(
            broker.namespace,
            broker.account_id,
            "NSE",
            item["instrument_id"],
            symbol,
            "MIS",
        )
    journal = TradeJournal(str(Path(directory) / f"{name}.db"))
    try:
        coordinator = OrderLifecycleCoordinator(journal)
        kwargs = dict(
            broker=broker,
            coordinator=coordinator,
            session_policy=session,
            daily_loss_limit=config["maxDailyLoss"],
            dynamic_entries=True,
        )
        runner = (
            CandidateRunner(**kwargs)
            if name == "candidate"
            else LegacyControlRunner(
                **kwargs,
                legacy_policy=legacy_policy,
                strategy_config=strategy_config,
                risk_config=config,
            )
        )
        now = start
        histories = {s: [] for s in frames}
        entries = {}
        pending = {}
        admissions = []
        entry_assessments = []
        entry_checkpoints = []
        excluded_entry_checkpoints = []
        service = MarketContextService(context, session)
        strategies = production_strategies()
        playbooks = [
            TrendPullbackPlaybook(),
            BreakoutPlaybook(),
            MeanReversionPlaybook(),
        ]

        def counts(at):
            ids = {
                oid
                for oid, e in entries.items()
                if broker._session_date(e["at"]) == broker._session_date(at)
            }
            return {
                "total": len(ids),
                "by_symbol": dict(Counter(entries[o]["symbol"] for o in ids)),
                "entry_order_ids": sorted(ids),
            }

        def correlation(left, right, at):
            series = []
            for symbol in (left, right):
                observed = histories.get(symbol, [])
                previous = [
                    row
                    for row in observed
                    if broker._session_date(row["date"]) < broker._session_date(at)
                ]
                if not previous:
                    return None
                frame = pd.DataFrame(previous)
                frame["session"] = frame["date"].map(broker._session_date)
                closes = (
                    frame.groupby("session")["close"]
                    .last()
                    .tail(int(config.get("correlationLookbackDays", 30)) + 1)
                )
                series.append(closes.pct_change().dropna())
            joined = pd.concat(series, axis=1, join="inner").dropna()
            if len(joined) < int(config.get("correlationMinSamples", 5)):
                return None
            value = joined.iloc[:, 0].corr(joined.iloc[:, 1])
            return float(value) if isfinite(value) else None

        risk = RiskManager.for_research(
            risk_config=config,
            clock=lambda: now,
            trade_counts_provider=counts,
            correlation_provider=correlation,
            sector_provider=lambda symbol: metadata[symbol]["sector"],
            accounting=AccountingService(costs),
        )

        def calibration_lookup(strategy, score):
            bucket_min = (score // 10) * 10
            bucket_max = bucket_min + 9
            outcomes = list(calibration_history)
            for trade in broker.trades:
                signal = trade["signal_info"]
                outcomes.append(
                    {
                        "strategy": signal.get("strategy"),
                        "confidence": signal.get("signal_score", 0),
                        "direction": trade["direction"],
                        "entry_price": trade["entry_price"],
                        "stop_loss": signal.get("stopLoss"),
                        "exit_price": trade["exit_price"],
                    }
                )
            return bucket_success_probability(
                row
                for row in outcomes
                if row["strategy"] == strategy
                and bucket_min <= row["confidence"] <= bucket_max
            )

        epoch = 0
        for at, candles in sorted(timeline.items()):
            now = at
            for symbol, row in sorted(candles.items()):
                histories[symbol].append(row)
            if at < start:
                yield at
                continue
            if at >= end:
                raise AssertionError("event escaped scoring interval")
            # Execute previously submitted ordinary orders against an ensuing bar.
            # A late receipt cannot grant a new order access to an earlier print.
            for symbol, row in sorted(candles.items()):
                if row["date"] >= start:
                    broker.process_candle(
                        symbol,
                        pd.Series(
                            dict(row, available_at=row["date"] + timedelta(minutes=5))
                        ),
                    )
            # Coarse-bar entry timeout: terminate remainder after its first
            # observed bar, even if latency prevented execution on that bar.
            # Any filled fraction stays immediately protected.
            for symbol, item in list(pending.items()):
                order = broker.get_order(item["order_id"])
                row = candles.get(symbol)
                expired = (
                    row is not None
                    and row["date"] >= item["submitted_at"]
                    and (at - item["submitted_at"]).total_seconds() >= 300
                )
                if expired and order["status"] not in {
                    "COMPLETE",
                    "CANCELLED",
                    "REJECTED",
                }:
                    broker.cancel_order(order["order_id"], timestamp=at)
                    order = broker.get_order(order["order_id"])
                if order["status"] not in {"COMPLETE", "CANCELLED", "REJECTED"}:
                    continue
                coordinator.observe_order(item["intent_id"], order)
                broker.reconcile_fills_with(
                    coordinator, broker._key_for(symbol).as_string()
                )
                coordinator.journal.complete_order_intent(item["intent_id"])
                if order["filled_quantity"]:
                    entries[order["order_id"]] = {
                        "at": order["updated_at"],
                        "symbol": symbol,
                    }
                    if symbol in broker.positions:
                        position = broker.positions[symbol]
                        valid_initial_r = (
                            order["average_price"] > item["thesis"].initial_stop
                            if item["thesis"].direction == "BUY"
                            else order["average_price"] < item["thesis"].initial_stop
                        )
                        thesis = (
                            bind_terminal_fill(
                                item["thesis"],
                                entry_vwap=order["average_price"],
                                filled_quantity=order["filled_quantity"],
                                terminal_at=at,
                                source_fill_ids=tuple(
                                    f["fill_id"]
                                    for f in broker.fills
                                    if f["order_id"] == order["order_id"]
                                ),
                            )
                            if valid_initial_r
                            else item["thesis"]
                        )
                        health = (
                            ThesisHealth.VALID
                            if valid_initial_r
                            and thesis.management_profile.name
                            != "unknown_legacy_bounded"
                            else ThesisHealth.UNKNOWN
                        )
                        if (
                            valid_initial_r
                            and position["quantity"] == order["filled_quantity"]
                            and not runner.clock.snapshot(at).forced_flatten_due
                        ):
                            entry_checkpoints.append(
                                {
                                    "case_id": f"control-entry-{len(entry_checkpoints) + 1}",
                                    "checkpoint_at": at.isoformat(),
                                    "initial_capital": capital,
                                    "daily_loss_limit": config["maxDailyLoss"],
                                    "positions": [
                                        {
                                            "first_entry_fill_at": position[
                                                "entry_time"
                                            ].isoformat(),
                                            "thesis": thesis.to_dict(),
                                            "state": PositionState(
                                                thesis.position_key,
                                                exposure=ExposureState.OPEN,
                                                thesis_health=health,
                                                protection=ProtectionState.ACTIVE,
                                                known_quantity=position["quantity"],
                                            ).to_dict(),
                                        }
                                    ],
                                }
                            )
                        else:
                            excluded_entry_checkpoints.append(
                                {
                                    "symbol": symbol,
                                    "order_id": order["order_id"],
                                    "reason": "INVALID_INITIAL_R_AFTER_GAP"
                                    if not valid_initial_r
                                    else "TERMINAL_ENTRY_AFTER_FORCED_DEADLINE"
                                    if runner.clock.snapshot(at).forced_flatten_due
                                    else "REDUCED_BEFORE_TERMINAL_ENTRY_CHECKPOINT",
                                }
                            )
                        runner.register_position(
                            thesis=thesis,
                            state=PositionState(
                                thesis.position_key,
                                exposure=ExposureState.OPEN,
                                thesis_health=health,
                                protection=ProtectionState.ACTIVE,
                                known_quantity=position["quantity"],
                            ),
                            policy=policy,
                        )
                if order["filled_quantity"] and symbol not in broker.positions:
                    excluded_entry_checkpoints.append(
                        {
                            "symbol": symbol,
                            "order_id": order["order_id"],
                            "reason": "CLOSED_WITHIN_ENTRY_BAR",
                        }
                    )
                risk.complete_entry_reservation(item["reservation_id"])
                del pending[symbol]
            contexts = {}
            for symbol in candles:
                if not histories[symbol]:
                    continue
                cache_key = (symbol, at)
                if cache_key not in context_cache:
                    context_cache[cache_key] = service.build(
                        metadata[symbol]["instrument_id"],
                        [
                            row
                            for row in histories[symbol]
                            if row["date"]
                            >= at - timedelta(days=PRODUCTION_CANDLE_HISTORY_DAYS)
                        ],
                        at,
                        received_at=at,
                    )
                contexts[symbol] = context_cache[cache_key]
            runner.on_event(
                at,
                contexts={s: c for s, c in contexts.items() if s in runner.positions},
            )
            snapshot = broker.broker_snapshot(at)
            risk.rotate_session_if_verified(
                runner.clock.snapshot(at),
                reconciliation_verified=snapshot.entry_ready_at(at),
                has_residual_obligations=bool(
                    broker.positions or broker.pending_orders
                ),
            )
            risk.update_from_broker_snapshot(snapshot)
            # Preserve prior-session hard obligations rather than reopening an
            # overnight residual under freshly reset daily limits.
            if any(p.overnight_quantity for p in snapshot.positions):
                runner.daily_loss_latched = True
            known = [e for e in universe if e["at"] <= at and e["available_at"] <= at]
            selected = max(known, key=lambda e: e["at"])["symbols"] if known else []
            opportunities = []
            for rank, symbol in enumerate(selected):
                ctx = contexts.get(symbol)
                if ctx is None:
                    entry_assessments.append(
                        {
                            "at": at,
                            "symbol": symbol,
                            "status": "UNAVAILABLE",
                            "reason": "NO_NEW_OBSERVED_BAR",
                        }
                    )
                    continue
                entry_assessments.append(
                    {
                        "at": at,
                        "symbol": symbol,
                        "status": "AVAILABLE"
                        if ctx.normal_decision_eligible
                        else "UNAVAILABLE",
                        "reason": ctx.primary_quality.status.value,
                    }
                )
                decisions = evaluate_production_entries(
                    symbol=symbol,
                    raw_frame=pd.DataFrame(
                        [
                            row
                            for row in histories[symbol]
                            if row["date"]
                            >= at - timedelta(days=PRODUCTION_CANDLE_HISTORY_DAYS)
                        ]
                    ),
                    market_context=ctx,
                    strategies=strategies,
                    playbooks=playbooks,
                    strategy_config=strategy_config,
                    family_mapping=FAMILIES,
                    calibration_lookup=calibration_lookup,
                    decision_at=at,
                    risk_config=config,
                )
                opportunities.extend(
                    (rank, index, symbol, signal)
                    for index, signal in enumerate(decisions)
                )
            for rank, index, symbol, signal in sorted(
                opportunities,
                key=lambda v: entry_signal_order_key(
                    v[3], {symbol: rank for rank, symbol in enumerate(selected)}
                ),
            ):
                record = {
                    "at": at,
                    "symbol": symbol,
                    "rank": rank + 1,
                    "signal": signal,
                    "accepted": False,
                }
                admissions.append(record)
                if signal.get("signal_score", 0) < 70:
                    record["reason"] = "SIGNAL_SCORE_BELOW_70"
                    continue
                probability = signal.get("estimated_probability")
                if probability is not None and probability < 0.60:
                    record["reason"] = "ESTIMATED_PROBABILITY_BELOW_0_60"
                    continue
                allowed, reason = risk.can_trade()
                if (
                    not allowed
                    or not runner.clock.snapshot(at).entries_allowed
                    or runner.daily_loss_latched
                ):
                    record["reason"] = (
                        reason if not allowed else "HARD_SESSION_OR_ACCOUNT_OBLIGATION"
                    )
                    continue
                if symbol in pending or symbol in broker.positions:
                    record["reason"] = "DUPLICATE_POSITION_OR_ENTRY"
                    continue
                previous = [t for t in broker.trades if t["symbol"] == symbol]
                if previous and (
                    at - previous[-1]["exit_time"]
                ).total_seconds() / 60 < config.get("tradeCooldownMins", 15):
                    record["reason"] = "TRADE_COOLDOWN"
                    continue
                signal = dict(signal)
                signal["entryPrice"] = round_entry_price_to_tick(
                    float(signal["entryPrice"]), metadata[symbol]["tick_size"]
                )
                signal["stopLoss"] = round_entry_price_to_tick(
                    float(signal["stopLoss"]), metadata[symbol]["tick_size"]
                )
                record["signal"] = signal
                price, stop = signal["entryPrice"], signal["stopLoss"]
                if signal["direction"] not in {"BUY", "SELL"} or not (
                    stop < price if signal["direction"] == "BUY" else stop > price
                ):
                    record["reason"] = "INVALID_DIRECTIONAL_STOP"
                    continue
                gross = sum(
                    abs(p.signed_quantity) * p.last_price
                    for p in snapshot.positions
                    if p.signed_quantity
                )
                pending_value = sum(
                    o["remaining_quantity"] * (o["price"] or 0)
                    for o in broker.pending_orders
                    if o["role"] == "ENTRY"
                )
                margin = max(
                    0,
                    broker.current_equity({})
                    - (gross + pending_value) / config["leverageMultiplier"],
                )
                quantity = risk.calculate_position_size(
                    price, stop, available_margin=margin
                )
                reservation, reason = risk.reserve_entry(
                    symbol,
                    signal["direction"],
                    quantity,
                    price,
                    broker.broker_snapshot(at),
                    instrument_id=metadata[symbol]["instrument_id"],
                )
                if not reservation:
                    record["reason"] = reason
                    continue
                epoch += 1
                key = broker._key_for(symbol).as_string() + f":entry-{epoch}"
                thesis = capture_entry_thesis(
                    signal,
                    position_key=key,
                    trade_id=f"portfolio-{epoch}",
                    position_epoch=f"entry-{epoch}",
                    instrument_id=metadata[symbol]["instrument_id"],
                    effective_config={
                        "risk": config,
                        "strategies": strategy_config,
                        "marketContext": dict(vars(context)),
                        "exitManagement": {
                            "policyVersion": policy.policy_version,
                            "defaultProfileVersion": "management-profiles-v1",
                            "legacyControlPolicyVersion": "correctness-repaired-legacy-v1",
                        },
                    },
                    created_at=at,
                )
                payload = {
                    "tradingsymbol": symbol,
                    "side": signal["direction"],
                    "quantity": quantity,
                    "timestamp": at.isoformat(),
                    "order_type": "LIMIT",
                    "price": price,
                    "role": "ENTRY",
                    "signal_info": dict(
                        signal,
                        target=None,
                        stopOrderType=config.get("stopOrderType", "SL"),
                    ),
                }
                response = coordinator.submit(
                    position_key=broker._key_for(symbol).as_string(),
                    intent_type=IntentType.ENTER,
                    role=OrderRole.ENTRY,
                    side=signal["direction"],
                    quantity=quantity,
                    payload=payload,
                    submit_order=lambda tag, payload=payload: (
                        broker.submit_coordinator_order(tag, payload)
                    ),
                    reason="PRODUCTION_ENTRY",
                )
                if not response.broker_order_id:
                    raise RuntimeError(
                        "unknown entry submission: study cannot invent broker resolution"
                    )
                risk.bind_entry_order(reservation, response.broker_order_id)
                pending[symbol] = {
                    "order_id": response.broker_order_id,
                    "intent_id": response.intent_id,
                    "reservation_id": reservation,
                    "thesis": thesis,
                    "submitted_at": at,
                }
                record.update(
                    accepted=True,
                    reason="ACCEPTED",
                    quantity=quantity,
                    position_key=key,
                    order_id=response.broker_order_id,
                )
            yield at
        result = runner.finish()
        for record in result["recorded_decisions"]:
            replay_recorded_exit_decision(record)
        result.pop("evaluations")
        entry_orders = [
            order for order in broker.orders.values() if order["role"] == "ENTRY"
        ]
        filled_entries = [order for order in entry_orders if order["filled_quantity"]]
        result.update(
            entry_assessments=entry_assessments,
            entry_checkpoints=entry_checkpoints,
            excluded_entry_checkpoints=excluded_entry_checkpoints,
            status="CENSORED"
            if broker.positions or broker.pending_orders
            else "COMPLETE",
            admissions=admissions,
            rejected_opportunities=[r for r in admissions if not r["accepted"]],
            pending_orders=list(broker.pending_orders),
            fills=broker.fills,
            orders=list(broker.orders.values()),
            execution_coverage={
                "scope": "HELD_EXPOSURE"
                if filled_entries
                else "ENTRY_ADMISSION_ONLY"
                if entry_orders
                else "NO_ENTRY_ORDERS",
                "entry_orders_submitted": len(entry_orders),
                "entry_orders_with_fills": len(filled_entries),
                "entry_quantity_filled": sum(
                    order["filled_quantity"] for order in filled_entries
                ),
                "completed_trades": len(broker.trades),
            },
            ambiguity_events=broker.ambiguous_events,
            parity={
                "recorded_decisions": len(result["recorded_decisions"]),
                "replayed_decisions": len(result["recorded_decisions"]),
                "mismatches": 0,
            },
            metrics=MetricsEvaluator.evaluate(
                broker.trades, capital, equity_curve=runner.equity_curve
            ),
        )
        result["manifest"].update(
            entry_policy="PRODUCTION_ENTRY_V1_WITH_SHARED_RISK_RESERVATIONS",
            selection_policy=ENTRY_ORDERING_VERSION,
            production_candle_history_days=PRODUCTION_CANDLE_HISTORY_DAYS,
            entry_execution="LIMIT_NEXT_AVAILABLE_BAR_ONE_OBSERVED_BAR_TTL",
            calibration="SHARED_GROSS_R_BUCKETS_CAUSAL_COMPLETED_OUTCOMES",
            warmup="FEATURES_ONLY",
            end_of_data="CENSOR_RESIDUALS_AND_PENDING_ORDERS",
        )
        return _detached(result)
    finally:
        journal._get_conn().close()
