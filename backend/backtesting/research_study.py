"""Bounded paired execution from retained, simultaneous entry checkpoints.

This is an executable fixed-entry account experiment, not an admission simulator
or evidence of live/paper operation. All broker and journal state is temporary.
The original entry premise is supplied by the study, never reconstructed from
later bars. Caller artifacts are detached and hashed before execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from ..exit_management.engine import ExitPolicy
from ..exit_management.models import (
    DevelopmentPhase,
    ManagementState,
    PositionState,
    ThesisHealth,
)
from ..exit_management.thesis import EntryThesis
from ..journal import TradeJournal
from ..market_context import ContextPolicy
from ..order_lifecycle import OrderLifecycleCoordinator
from ..replay import _restore, replay_recorded_exit_decision, serialize_replay_artifact
from ..session_clock import SessionClock, SessionPolicy
from ..trading_costs import TradingCostCalculator
from .alternative_policy import simulate_alternative_exit_execution
from .candidate_runner import CandidateRunner
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy


def _detached(value: Any) -> Any:
    return json.loads(
        json.dumps(serialize_replay_artifact(value), sort_keys=True, allow_nan=False)
    )


def _artifact(value: Any) -> dict:
    payload = _detached(value)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {"sha256": hashlib.sha256(encoded.encode()).hexdigest(), "payload": payload}


def _at(value: Any, name: str) -> datetime:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an aware timestamp") from exc
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"{name} must be an aware timestamp")
    return result.to_pydatetime().astimezone(timezone.utc)


def restore_exit_policy(value: ExitPolicy | Mapping[str, Any]) -> ExitPolicy:
    """Restore only the existing replay adapter's declared policy field types."""
    return _restore(_detached(value), ExitPolicy)


def _causal_snapshot(value: Any, checkpoint_at: datetime) -> None:
    """Reject future timestamps in the retained entry context and anchors.

    This verifies declared event chronology; it cannot certify the source of
    arbitrary supplied indicator values or retroactively prove a thesis existed.
    """
    timestamps = {
        "known_at",
        "formed_at",
        "available_at",
        "received_at",
        "sourceAsOf",
        "source_as_of",
        "decision_at",
        "decision_event_time",
        "start",
        "end",
    }
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name in timestamps and item is not None:
                if _at(item, f"entry snapshot {name}") > checkpoint_at:
                    raise ValueError("checkpoint contains a future entry snapshot")
            else:
                _causal_snapshot(item, checkpoint_at)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _causal_snapshot(item, checkpoint_at)


def restore_session_policy(
    value: SessionPolicy | Mapping[str, Any] | None,
) -> SessionPolicy:
    payload = _detached(value or SessionPolicy())
    allowed = {item.name for item in fields(SessionPolicy)}
    if set(payload) - allowed:
        raise ValueError("unknown session policy fields")
    for key in (
        "open_time",
        "close_time",
        "no_new_entries_after",
        "forced_flatten_time",
    ):
        if key in payload:
            payload[key] = time.fromisoformat(payload[key])
    if "exchange_timezone" in payload:
        payload["exchange_timezone"] = ZoneInfo(payload["exchange_timezone"])
    if "holidays" in payload:
        payload["holidays"] = frozenset(
            date.fromisoformat(v) for v in payload["holidays"]
        )
    policy = SessionPolicy(**payload)
    if not (
        policy.open_time
        < policy.no_new_entries_after
        <= policy.forced_flatten_time
        < policy.close_time
    ):
        raise ValueError("session deadlines must be ordered inside the session")
    return policy


def _costs(value: Mapping[str, Any] | None) -> TradingCostCalculator:
    calculator = TradingCostCalculator(**dict(value or {}))
    for name, item in vars(calculator).items():
        if name.endswith("version"):
            if not isinstance(item, str) or not item.strip():
                raise ValueError("cost schedule versions must be nonempty")
        elif (
            isinstance(item, bool)
            or not isinstance(item, Real)
            or not isfinite(item)
            or item < 0
        ):
            raise ValueError("cost rates must be finite nonnegative numbers")
    return calculator


def _source_artifact(revision: str) -> dict:
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("source_revision is required")
    backend = Path(__file__).resolve().parents[1]
    paths = sorted(path for path in backend.rglob("*.py") if "tests" not in path.parts)
    paths += [backend.parent / name for name in ("pyproject.toml", "uv.lock")]
    return _artifact(
        {
            "declared_revision": revision,
            "file_sha256": {
                str(path.relative_to(backend.parent)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in paths
                if path.is_file()
            },
        }
    )


def _frames(
    data: Mapping[str, pd.DataFrame],
    *,
    symbols: set[str],
    warmup_start: datetime,
    test_end: datetime,
    checkpoint_at: datetime,
    session: SessionPolicy,
    context: ContextPolicy,
) -> dict[str, pd.DataFrame]:
    if set(data) - symbols:
        raise ValueError("market data contains symbols outside the checkpoint account")
    required = {"date", "open", "high", "low", "close", "volume"}
    allowed = required | {"received_at", "available_at"}
    frames = {}
    clock = SessionClock(session)
    entry_day = checkpoint_at.astimezone(session.exchange_timezone).date()
    for symbol, source in sorted(data.items()):
        if not isinstance(source, pd.DataFrame) or not required.issubset(
            source.columns
        ):
            raise ValueError("research candles require date and OHLCV columns")
        if set(source.columns) - allowed or source.columns.duplicated().any():
            raise ValueError(
                "research candles cannot contain labels or undeclared columns"
            )
        rows = []
        for row in source.to_dict("records"):
            start = _at(row["date"], "candle date")
            if not warmup_start <= start < test_end:
                raise ValueError("candle lies outside the declared study interval")
            snapshot = clock.snapshot(start)
            opened_at = datetime.combine(
                snapshot.session_date,
                session.open_time,
                tzinfo=session.exchange_timezone,
            )
            bar_end = start + timedelta(minutes=context.primary_interval_minutes)
            if (
                not snapshot.is_open
                or not clock.snapshot(bar_end - timedelta(microseconds=1)).is_open
                or (start - opened_at).total_seconds()
                % (60 * context.primary_interval_minutes)
            ):
                raise ValueError(
                    "candles must align wholly inside a declared trading session"
                )
            if start.astimezone(session.exchange_timezone).date() > entry_day:
                raise ValueError(
                    "fixed-entry cases may execute only their entry session"
                )
            available = start + timedelta(
                minutes=5, seconds=context.availability_delay_seconds
            )
            row["date"] = start
            for name in ("available_at", "received_at"):
                if name in row:
                    observed = _at(row[name], name)
                    if observed < start:
                        raise ValueError("candle availability cannot precede its start")
                    available = max(available, observed)
                    row[name] = observed
            if available >= test_end:
                raise ValueError("candle is unavailable before the study cutoff")
            if available.astimezone(session.exchange_timezone).date() > entry_day:
                raise ValueError("case availability extends beyond the entry session")
            for name in ("open", "high", "low", "close", "volume"):
                item = row[name]
                if (
                    isinstance(item, bool)
                    or not isinstance(item, Real)
                    or not isfinite(item)
                ):
                    raise ValueError("OHLCV values must be finite numbers")
                if item < 0 or (name != "volume" and item == 0):
                    raise ValueError("prices must be positive and volume nonnegative")
            if (
                not row["low"]
                <= min(row["open"], row["close"])
                <= max(row["open"], row["close"])
                <= row["high"]
            ):
                raise ValueError("invalid OHLC price range")
            rows.append(row)
        frame = (
            pd.DataFrame(rows, columns=source.columns)
            .sort_values("date")
            .reset_index(drop=True)
        )
        if frame["date"].duplicated().any():
            raise ValueError(
                "duplicate bars require a separately declared revision study"
            )
        frames[symbol] = frame
    return frames


def run_paired_case(
    *,
    case: Mapping[str, Any],
    market_data: Mapping[str, pd.DataFrame],
    candidate_policy: ExitPolicy | Mapping[str, Any],
    control_policy: ExitPolicy | Mapping[str, Any],
    test_start: datetime,
    test_end: datetime,
    source_revision: str,
    warmup_start: datetime | None = None,
    execution_policy: SimulationExecutionPolicy | Mapping[str, Any] | None = None,
    cost_policy: Mapping[str, Any] | None = None,
    context_policy: ContextPolicy | Mapping[str, Any] | None = None,
    session_policy: SessionPolicy | Mapping[str, Any] | None = None,
    control_mode: str = "SHARED_POLICY",
    legacy_policy: Mapping[str, Any] | None = None,
    strategy_config: Mapping[str, Any] | None = None,
    risk_config: Mapping[str, Any] | None = None,
) -> dict:
    """Run and replay-verify candidate/control policies on one bounded account.

    ``case`` contains ``case_id``, ``checkpoint_at``, ``initial_capital``, optional
    ``account_id``/``daily_loss_limit``, and ``positions``. Each position contains
    complete retained ``thesis``/``state`` dicts and optional fresh ``management``.
    Terminal entry fills must all equal the checkpoint time. Source identities
    are retained and explicitly rebound to the isolated REPLAY account.

    The half-open scoring interval must contain the complete entry session's
    forced deadline. Warmup may precede scoring but is feature-only. Input rows
    outside the declared interval are rejected, including late availability.
    This function never reads or slices a larger holdout-bearing data source.
    Declared execution stress applies after the fixed fills; entry charges use
    the declared schedule, which is not a historical rate lookup.
    """
    if control_mode not in {"SHARED_POLICY", "LEGACY_REPAIRED"}:
        raise ValueError("unknown paired control mode")
    if control_mode == "LEGACY_REPAIRED" and strategy_config is None:
        raise ValueError("legacy comparator requires frozen strategy configuration")
    source = _source_artifact(source_revision)
    retained_case = _detached(case)
    allowed = {
        "case_id",
        "checkpoint_at",
        "initial_capital",
        "account_id",
        "daily_loss_limit",
        "positions",
    }
    if set(retained_case) - allowed:
        raise ValueError("unknown fixed-entry case fields")
    if (
        not isinstance(retained_case.get("case_id"), str)
        or not retained_case["case_id"].strip()
    ):
        raise ValueError("case_id is required")
    start, end = _at(test_start, "test_start"), _at(test_end, "test_end")
    warmup = _at(warmup_start, "warmup_start") if warmup_start is not None else start
    checkpoint_at = _at(retained_case["checkpoint_at"], "checkpoint_at")
    if not warmup <= start <= checkpoint_at < end:
        raise ValueError("checkpoint must belong to the scoring interval")
    capital = retained_case["initial_capital"]
    if (
        isinstance(capital, bool)
        or not isinstance(capital, Real)
        or not isfinite(capital)
        or capital <= 0
    ):
        raise ValueError("initial capital must be finite and positive")
    account_id = retained_case.get("account_id", "simulation")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be nonempty text")
    session = restore_session_policy(session_policy)
    clock = SessionClock(session)
    snapshot = clock.snapshot(checkpoint_at)
    if not snapshot.is_open or snapshot.forced_flatten_due:
        raise ValueError(
            "terminal checkpoint must be inside the session before forced flatten"
        )
    deadline = datetime.combine(
        snapshot.session_date,
        session.forced_flatten_time,
        tzinfo=session.exchange_timezone,
    )
    if deadline >= end:
        raise ValueError("study cutoff must contain the session's forced deadline")
    candidate, control = (
        restore_exit_policy(candidate_policy),
        restore_exit_policy(control_policy),
    )
    execution = SimulationExecutionPolicy(
        **_detached(execution_policy or SimulationExecutionPolicy())
    )
    context = ContextPolicy(**_detached(context_policy or ContextPolicy()))
    costs = _costs(cost_policy)
    positions = retained_case.get("positions")
    if not isinstance(positions, list) or not positions:
        raise ValueError("case requires a nonempty position checkpoint list")
    seeds = []
    first_entry_fills = {}
    symbols = set()
    for item in positions:
        if set(item) - {"thesis", "state", "management", "first_entry_fill_at"}:
            raise ValueError("unknown position checkpoint fields")
        thesis = EntryThesis.from_dict(item["thesis"])
        state = PositionState.from_dict(item["state"])
        memory = ManagementState.from_dict(item.get("management", {}))
        if thesis.symbol in symbols or state.position_key != thesis.position_key:
            raise ValueError(
                "checkpoint requires unique symbols and matching source keys"
            )
        symbols.add(thesis.symbol)
        first_fill = _at(
            item.get("first_entry_fill_at", checkpoint_at), "first_entry_fill_at"
        )
        if not start <= first_fill <= checkpoint_at:
            raise ValueError(
                "first entry fill must be inside scoring and no later than terminal checkpoint"
            )
        first_entry_fills[thesis.symbol] = first_fill
        if (
            state.development is not DevelopmentPhase.EARLY
            or state.thesis_health
            not in {
                ThesisHealth.VALID,
                ThesisHealth.UNKNOWN,
            }
        ):
            raise ValueError("paired cases require a fresh entry lifecycle state")
        if thesis.exchange != "NSE" or thesis.product != "MIS":
            raise ValueError("the shared simulator supports NSE MIS checkpoints")
        if (
            thesis.fill_binding is None
            or _at(thesis.fill_binding.entry_terminal_at, "terminal entry time")
            != checkpoint_at
        ):
            raise ValueError("all terminal entry fills must equal checkpoint_at")
        for name, timestamp in {
            "thesis creation": thesis.created_at,
            "entry decision": thesis.input_reference.decision_at,
            "entry source": thesis.input_reference.source_as_of,
            "state transition": state.last_transition_at,
        }.items():
            if timestamp is not None and _at(timestamp, name) > checkpoint_at:
                raise ValueError("checkpoint contains a future entry premise or state")
        created_at = _at(thesis.created_at, "thesis creation")
        for timestamp in (
            thesis.input_reference.decision_at,
            thesis.input_reference.source_as_of,
        ):
            if timestamp is not None and _at(timestamp, "entry input") > created_at:
                raise ValueError(
                    "entry input was unavailable when the thesis was frozen"
                )
        for snapshot_input in (
            thesis.causal_anchors,
            thesis.input_reference.snapshot,
            thesis.entry_input,
        ):
            _causal_snapshot(snapshot_input, created_at)
        if replace(memory, confirmed_stop=None) != ManagementState():
            raise ValueError("paired cases require fresh normal-management state")
        if memory.confirmed_stop not in (None, thesis.initial_stop):
            raise ValueError("entry checkpoint must retain the original confirmed stop")
        seeds.append((thesis, state, memory))
    frames = _frames(
        market_data,
        symbols=symbols,
        warmup_start=warmup,
        test_end=end,
        checkpoint_at=checkpoint_at,
        session=session,
        context=context,
    )
    data_artifact = _artifact(
        {symbol: frame.to_dict("records") for symbol, frame in sorted(frames.items())}
    )
    policy_artifact = _artifact(
        {
            "candidate": candidate,
            "control": control,
            "control_mode": control_mode,
            "legacy_policy": legacy_policy,
            "strategy_config": strategy_config,
            "risk_config": risk_config,
            "execution": execution,
            "costs": vars(costs),
            "context": context,
            "session": session,
        }
    )
    input_artifact = _artifact(
        {
            "case": retained_case,
            "test_start": start,
            "test_end": end,
            "warmup_start": warmup,
            "entry_fill_model": "RETAINED_VWAP_NO_ADDITIONAL_SLIPPAGE",
            "entry_fee_model": "SINGLE_TERMINAL_ORDER_DECLARED_COST_SCHEDULE",
        }
    )
    with TemporaryDirectory(prefix="kite-research-study-") as directory:
        journal = TradeJournal(str(Path(directory) / "checkpoint.db"))
        try:
            broker = SimulatedBroker(
                initial_capital=capital,
                execution_policy=replace(execution, slippage_bps=0),
                cost_service=costs,
                account_id=account_id,
            )
            runner = CandidateRunner(
                broker=broker,
                coordinator=OrderLifecycleCoordinator(journal),
                session_policy=session,
                daily_loss_limit=retained_case.get("daily_loss_limit"),
            )
            identities = []
            for thesis, state, memory in sorted(seeds, key=lambda seed: seed[0].symbol):
                binding = thesis.fill_binding
                broker.place_market_order(
                    thesis.symbol,
                    thesis.direction,
                    binding.filled_quantity,
                    binding.entry_vwap,
                    first_entry_fills[thesis.symbol],
                    {
                        "stopLoss": thesis.initial_stop,
                        "strategy": thesis.strategy,
                        "playbook": thesis.playbook,
                        "setup_variant": thesis.setup_variant,
                    },
                )
                # Preserve the actual first-fill holding clock, while the fresh
                # management checkpoint begins only at terminal allocation.
                broker.mark_price(thesis.symbol, binding.entry_vwap, checkpoint_at)
                key = (
                    broker._key_for(thesis.symbol).as_string()
                    + ":"
                    + thesis.position_epoch
                )
                identities.append(
                    {
                        "source_position_key": thesis.position_key,
                        "replay_position_key": key,
                    }
                )
                runner.register_position(
                    thesis=replace(thesis, position_key=key),
                    state=replace(state, position_key=key),
                    management=memory,
                    policy=candidate,
                )
            broker.execution_policy = execution
            paired = simulate_alternative_exit_execution(
                checkpoint=runner,
                policies={symbol: control for symbol in symbols},
                market_data=frames,
                context_policy=context,
                legacy_control={
                    "legacy_policy": legacy_policy,
                    "strategy_config": strategy_config,
                    "risk_config": risk_config,
                }
                if control_mode == "LEGACY_REPAIRED"
                else None,
            )
        finally:
            journal._get_conn().close()
    parity = {}
    branches = {"candidate": paired["original"], "control": paired["alternative"]}
    for name, branch in branches.items():
        records = branch["recorded_decisions"]
        for record in records:
            replay_recorded_exit_decision(record)
            event_at = _at(
                record["trace"]["input_snapshot"]["risk"]["session"]["observed_at"],
                "decision time",
            )
            if not start <= event_at < end:
                raise AssertionError(
                    "shared execution emitted a decision outside the study interval"
                )
        for trade in branch["trades"]:
            if (
                not start
                <= _at(trade["entry_time"], "entry time")
                <= _at(trade["exit_time"], "exit time")
                < end
            ):
                raise AssertionError(
                    "shared execution emitted a trade outside the study interval"
                )
        parity[name] = {
            "recorded_decisions": len(records),
            "replayed_decisions": len(records),
            "mismatches": 0,
        }
    comparisons = [
        {
            **item,
            "net_r_delta": -item["net_r_delta"]
            if item["net_r_delta"] is not None
            else None,
            "gross_r_delta": -item["gross_r_delta"]
            if item["gross_r_delta"] is not None
            else None,
        }
        for item in paired["comparisons"]
    ]
    complete = sum(item["status"] == "COMPLETE" for item in comparisons)
    return _detached(
        {
            "schema_version": "paired-research-case-v1",
            "case_id": retained_case["case_id"],
            "control_mode": control_mode,
            "status": paired["status"],
            "mode": "SHARED_CANDIDATE_PAIRED_EXECUTION",
            "scope": "CONDITIONAL_FIXED_ENTRY_SIMULATED_ACCOUNT",
            "production_admission_parity": False,
            "operational_evidence": False,
            "delta_direction": "CANDIDATE_MINUS_DECLARED_CONTROL",
            "entry_cost_limit": "AGGREGATED_TERMINAL_FILL_SINGLE_ORDER_FEES",
            "reproducibility": "DETERMINISTIC_ECONOMICS_WITH_OPAQUE_COORDINATOR_IDENTITIES",
            "artifacts": {
                "inputs": input_artifact,
                "data": data_artifact,
                "policies": policy_artifact,
                "source": source,
            },
            "identities": identities,
            "missing_market_symbols": sorted(symbols - set(frames)),
            "paired_counts": {
                "total": len(comparisons),
                "complete": complete,
                "censored": len(comparisons) - complete,
            },
            "parity": parity,
            "comparisons": comparisons,
            **branches,
        }
    )
