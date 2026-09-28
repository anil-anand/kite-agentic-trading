"""Paired, executable policy experiments from an isolated fixed-entry account.

Only simulation checkpoints are accepted. The original runner and journal are
never advanced; each branch receives its own broker and temporary REPLAY ledger.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping, Sequence

import pandas as pd

from ..broker_models import ExecutionNamespace
from ..exit_management.engine import ExitPolicy
from ..journal import TradeJournal
from ..market_context import ContextPolicy
from ..order_lifecycle import OrderLifecycleCoordinator
from ..replay import serialize_replay_artifact
from .backtest_engine import BacktestEngine
from .candidate_runner import CandidateRunner
from .metrics_evaluator import MetricsEvaluator
from .simulated_broker import SimulatedBroker


def simulate_alternative_exit_execution(
    *,
    checkpoint: CandidateRunner,
    policies: Mapping[str, ExitPolicy],
    market_data: Mapping[str, pd.DataFrame],
    context_policy: ContextPolicy = ContextPolicy(),
    clock_events: Sequence[datetime] = (),
    legacy_control: Mapping | None = None,
) -> dict:
    """Execute original and alternative policies with identical fill assumptions.

    ``policies`` is keyed by symbol and changes only normal management. Supply
    the complete simulated account before its first management event; previous
    confirmation counters and outstanding intents cannot be transplanted to a
    different policy. Entry fills, confirmed stops, daily-loss limit, exchange
    calendar, forced deadline, slippage, fees and liquidity are retained.

    This is a conditional fixed-entry account study. It does not claim that a
    production portfolio would admit the same future opportunities. Missing
    executable data produces censored residuals, never a fabricated close.
    """

    if (
        type(checkpoint) is not CandidateRunner
        or type(checkpoint.broker) is not SimulatedBroker
        or checkpoint.broker.namespace is not ExecutionNamespace.REPLAY
    ):
        raise ValueError(
            "alternative execution requires a REPLAY simulation checkpoint"
        )
    if (
        checkpoint._last_event is not None
        or checkpoint.broker.trades
        or not checkpoint.positions
        or set(checkpoint.positions) != set(checkpoint.broker.positions)
    ):
        raise ValueError(
            "alternative execution must start from a complete untouched entry checkpoint"
        )
    if not policies or set(policies).difference(checkpoint.positions):
        raise ValueError(
            "alternative policies must identify registered checkpoint symbols"
        )
    for symbol, managed in checkpoint.positions.items():
        policy = policies.get(symbol, managed.policy)
        if not isinstance(policy, ExitPolicy):
            raise TypeError("alternative policy must be an ExitPolicy")
        if (
            policy.hard_risk_policy != managed.policy.hard_risk_policy
            or policy.tick_size != managed.policy.tick_size
        ):
            raise ValueError(
                "alternative execution must preserve hard risk and tick size"
            )
        if (
            managed.management.eligible_completed_bars
            or managed.management.last_processed_primary_bar_id is not None
            or managed.coordinator_intent_id is not None
            or managed.tighten_intent_id is not None
        ):
            raise ValueError(
                "alternative execution cannot transplant prior normal management"
            )

    def branch(name: str, directory: str, alternative: bool) -> dict:
        broker = deepcopy(checkpoint.broker)
        journal = TradeJournal(str(Path(directory) / f"{name}.db"))
        try:
            from .legacy_control import LegacyControlRunner

            runner_type = (
                LegacyControlRunner
                if alternative and legacy_control is not None
                else CandidateRunner
            )
            extra = (
                dict(legacy_control or {}) if runner_type is LegacyControlRunner else {}
            )
            runner = runner_type(
                **extra,
                broker=broker,
                coordinator=OrderLifecycleCoordinator(journal),
                session_policy=checkpoint.clock.policy,
                daily_loss_limit=checkpoint.daily_loss_limit,
            )
            # A previously latched account obligation is irreversible within the
            # declared session, including when the latest mark has recovered.
            runner.daily_loss_latched = checkpoint.daily_loss_latched
            runner._session_start_equity = checkpoint._session_start_equity
            runner._session_id = checkpoint._session_id
            for symbol, managed in sorted(checkpoint.positions.items()):
                runner.register_position(
                    thesis=managed.thesis,
                    state=managed.state,
                    management=replace(managed.management, policy_fingerprint=None),
                    policy=policies.get(symbol, managed.policy)
                    if alternative
                    else managed.policy,
                )
            engine = BacktestEngine(
                None, broker=broker, mode=BacktestEngine.CANDIDATE_EXIT_REPLAY
            )
            for symbol, frame in sorted(market_data.items()):
                engine.load_data(symbol, frame)
            result = engine.run_candidate_execution(
                runner, context_policy=context_policy, clock_events=clock_events
            )
            outcomes = {}
            by_symbol = {trade["symbol"]: trade for trade in result["trades"]}
            for symbol, managed in checkpoint.positions.items():
                trade = by_symbol.get(symbol)
                budget = managed.thesis.fill_binding.initial_risk_budget
                outcomes[symbol] = {
                    "status": "COMPLETE" if trade is not None else "CENSORED",
                    "censor_reason": None
                    if trade is not None
                    else "UNRESOLVED_EXPOSURE_AT_END_OF_DATA",
                    "initial_risk_currency": budget,
                    "captured_gross_r": trade["gross_pnl"] / budget
                    if trade is not None
                    else None,
                    "captured_net_r": trade["net_pnl"] / budget
                    if trade is not None
                    else None,
                }
            return serialize_replay_artifact(
                {
                    "status": "CENSORED"
                    if result["censored_positions"]
                    else "COMPLETE",
                    "manifest": result["manifest"],
                    "trades": result["trades"],
                    "outcomes": outcomes,
                    "censored_positions": result["censored_positions"],
                    "equity_curve": result["equity_curve"],
                    "metrics": MetricsEvaluator.evaluate(
                        result["trades"],
                        broker.initial_capital,
                        equity_curve=result["equity_curve"],
                    ),
                    "recorded_decisions": result["recorded_decisions"],
                    "legacy_control_decisions": result.get(
                        "legacy_control_decisions", []
                    ),
                    "execution_results": result["execution_results"],
                    "fills": broker.fills,
                    "ambiguity_events": broker.ambiguous_events,
                }
            )
        finally:
            journal._get_conn().close()

    with TemporaryDirectory(prefix="kite-alternative-replay-") as directory:
        original = branch("original", directory, False)
        alternative = branch("alternative", directory, True)
    comparisons = []
    for symbol in sorted(checkpoint.positions):
        actual, simulated = (
            original["outcomes"][symbol],
            alternative["outcomes"][symbol],
        )
        complete = actual["status"] == simulated["status"] == "COMPLETE"
        comparisons.append(
            {
                "symbol": symbol,
                "status": "COMPLETE" if complete else "CENSORED",
                "net_r_delta": simulated["captured_net_r"] - actual["captured_net_r"]
                if complete
                else None,
                "gross_r_delta": simulated["captured_gross_r"]
                - actual["captured_gross_r"]
                if complete
                else None,
            }
        )
    return {
        "status": "COMPLETE"
        if all(item["status"] == "COMPLETE" for item in comparisons)
        else "CENSORED",
        "mode": "ALTERNATIVE_POLICY_EXECUTION_REPLAY",
        "account_scenario": "CONDITIONAL_FIXED_ENTRY_SIMULATED_ACCOUNT",
        "seed": 0,
        "randomness": "NONE_DETERMINISTIC_EXECUTION_MODEL",
        "horizon": "SUPPLIED_DATA_AND_RETAINED_FORCED_SESSION_DEADLINES",
        "original": original,
        "alternative": alternative,
        "comparisons": comparisons,
    }
