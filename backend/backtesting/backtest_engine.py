import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Dict, Mapping, Optional, Sequence

import pandas as pd

from ..exit_management.engine import ExitPolicy
from ..exit_management.models import ManagementState, PositionState
from ..exit_management.thesis import EntryThesis
from ..market_context import ContextPolicy, MarketContextService
from ..replay import (
    ExitReplayEvent,
    ExitReplayResult,
    ReplayMode,
    replay_exit_decisions,
)
from ..strategies.base import BaseStrategy
from ..time_utils import as_utc
from .candidate_runner import CandidateRunner
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy


class BacktestEngine:
    """Historical event driver.

    ``raw_strategy_lab`` is intentionally retained as the repository's old
    strategy-only experiment.  It is labelled in the run manifest and cannot be
    mistaken for a production-entry/candidate-exit study.  Candidate exit-only
    studies call :meth:`run_candidate_exit_replay`, which delegates directly to
    the same pure adapter used by paper/live shadow.
    """

    RAW_STRATEGY_LAB = "raw_strategy_lab"
    CANDIDATE_EXIT_REPLAY = "candidate_exit_replay"

    def __init__(
        self,
        strategy: Optional[BaseStrategy],
        initial_capital: float = 100000.0,
        *,
        execution_policy: Optional[SimulationExecutionPolicy] = None,
        mode: str = RAW_STRATEGY_LAB,
        broker: Optional[SimulatedBroker] = None,
        risk_config: Optional[Mapping] = None,
    ):
        if mode not in {self.RAW_STRATEGY_LAB, self.CANDIDATE_EXIT_REPLAY}:
            raise ValueError("unknown backtest mode")
        if mode == self.RAW_STRATEGY_LAB and strategy is None:
            raise ValueError("raw strategy lab requires a strategy")
        self.risk_config = deepcopy(
            dict(
                risk_config
                or {"defaultStopLossPercent": 1.5, "defaultTargetPercent": 3.0}
            )
        )
        self.strategy = strategy
        self.mode = mode
        self.broker = broker or SimulatedBroker(
            initial_capital=initial_capital, execution_policy=execution_policy
        )
        self.market_data: Dict[str, pd.DataFrame] = {}  # symbol -> OHLCV DataFrame
        self.run_manifest: dict = {}
        self.equity_curve: list[dict] = []

    def load_data(self, symbol: str, df: pd.DataFrame):
        """
        Loads OHLCV data for a symbol.
        DataFrame must contain: open, high, low, close, volume, date
        """
        df = df.copy()
        if "date" in df.columns and not pd.api.types.is_datetime64_any_dtype(
            df["date"]
        ):
            df["date"] = pd.to_datetime(df["date"])

        df = df.sort_values("date").reset_index(drop=True)
        self.market_data[symbol] = df

    def _build_manifest(self) -> dict:
        datasets = {}
        for symbol, frame in sorted(self.market_data.items()):
            datasets[symbol] = hashlib.sha256(
                frame.to_json(
                    orient="split", date_format="iso", double_precision=15
                ).encode("utf-8")
            ).hexdigest()
        return {
            "runner_version": "backtest-event-driver-v1",
            "mode": self.mode,
            "strategy": self.strategy.get_name() if self.strategy else None,
            "datasets": datasets,
            "execution": self.broker.execution_manifest,
            "risk_config": self.risk_config,
            "end_of_data": "CENSOR_RESIDUALS_AND_PENDING_ORDERS",
            "entry_policy": "RAW_STRATEGY_LAB_ONLY"
            if self.mode == self.RAW_STRATEGY_LAB
            else "INJECTED_PRODUCTION_ENTRY_OR_FIXED_OPPORTUNITIES",
        }

    def run(self):
        """
        Runs the backtest by iterating through the timeline chronologically across all symbols.

        Execution model (avoids look-ahead bias):
          - Signals generated from the slice ending at bar T are queued.
          - They are filled at the OPEN of bar T+1, not at T's close which is
            unknowable until the bar completes.
        End-of-test policy:
          - Residual exposure is censored; an observed close is not a new
            executable event for an end-of-data order.
        """
        if self.mode != self.RAW_STRATEGY_LAB:
            raise RuntimeError(
                "candidate policy studies must call run_candidate_exit_replay with "
                "fixed/reproducible entry and fill opportunities"
            )
        self.run_manifest = self._build_manifest()
        for frame in self.market_data.values():
            if frame["date"].duplicated().any():
                raise ValueError("raw lab requires unique candle starts")
            for row in frame.to_dict("records"):
                completed = self.broker._time(row["date"]) + timedelta(minutes=5)
                for field in ("available_at", "received_at"):
                    if row.get(field) is not None and as_utc(row[field]) > completed:
                        raise ValueError(
                            "delayed data requires the candidate event driver"
                        )

        # Find the global timeline
        all_dates = []
        for df in self.market_data.values():
            all_dates.extend(df["date"].tolist())

        if not all_dates:
            return

        unique_dates = sorted(list(set(all_dates)))

        # Pre-align dataframes
        aligned_data = {}
        for symbol, df in self.market_data.items():
            aligned_data[symbol] = df.set_index("date")

        # Submit at completed-bar availability; the broker applies its declared
        # latency and partial-fill model at the next executable event.
        for current_time in unique_dates:
            for symbol in sorted(aligned_data):
                frame = aligned_data[symbol]
                if current_time in frame.index:
                    candle = frame.loc[current_time].copy()
                    candle["date"] = current_time
                    self.broker.process_candle(symbol, candle)
            pending = {
                order["symbol"]: order
                for order in self.broker.pending_orders
                if order["role"] == "ENTRY"
            }
            reservations = {
                symbol: order["remaining_quantity"] * order["signal_info"]["entryPrice"]
                for symbol, order in pending.items()
            }
            for symbol in sorted(aligned_data):
                frame = aligned_data[symbol]
                if current_time not in frame.index:
                    continue
                signals = self.strategy.calculate_signals_with_context(
                    frame.loc[:current_time].reset_index().copy(deep=True),
                    symbol,
                    risk_config=self.risk_config,
                    decision_at=self.broker._time(current_time) + timedelta(minutes=5),
                )
                for signal in signals:
                    if symbol in self.broker.positions or symbol in pending:
                        break
                    entry_price = self.broker._finite_price(signal["entryPrice"])
                    occupied = sum(
                        position["quantity"]
                        * self.broker._prices.get(name, position["entry_price"])
                        for name, position in self.broker.positions.items()
                    )
                    available = max(
                        0.0,
                        self.broker.current_equity({})
                        - occupied
                        - sum(reservations.values()),
                    )
                    qty = int(available * 0.1 / entry_price)
                    if qty <= 0:
                        continue
                    submitted_at = current_time + timedelta(minutes=5)
                    payload = {
                        "tradingsymbol": symbol,
                        "transaction_type": signal["direction"],
                        "quantity": qty,
                        "timestamp": submitted_at,
                        "role": "ENTRY",
                        "signal_info": signal,
                    }
                    order_id = self.broker.submit_coordinator_order(
                        f"raw-{symbol}-{submitted_at.isoformat()}", payload
                    )
                    pending[symbol] = self.broker.get_order(order_id)
                    reservations[symbol] = qty * entry_price
            self.broker.reserved_cash = round(sum(reservations.values()), 2)
            self.equity_curve.append(
                {
                    "timestamp": current_time + timedelta(minutes=5),
                    "equity": self.broker.current_equity({}),
                    "reserved_cash": self.broker.reserved_cash,
                }
            )
        for order in list(self.broker.pending_orders):
            if order["role"] == "ENTRY":
                self.broker.cancel_order(order["order_id"])
                self.broker.events.append(
                    {
                        "type": "ENTRY_CENSORED_END_OF_DATA",
                        "symbol": order["symbol"],
                        "remaining_quantity": order["remaining_quantity"],
                    }
                )
        self.broker.reserved_cash = 0.0

        for symbol, position in sorted(self.broker.positions.items()):
            self.broker.censored_positions.append(
                {
                    "symbol": symbol,
                    "quantity": position["quantity"],
                    "reason": "RESEARCH_END_OF_DATA",
                    "last_available_at": self.broker._mark_times[symbol].isoformat(),
                }
            )

        if self.equity_curve:
            self.equity_curve[-1].update(
                equity=self.broker.current_equity({}), reserved_cash=0.0
            )
        return {
            "manifest": self.run_manifest,
            "trades": self.broker.trades,
            "censored_positions": self.broker.censored_positions,
            "equity_curve": self.equity_curve,
        }

    def run_candidate_exit_replay(
        self,
        *,
        thesis: EntryThesis | None,
        position_state: PositionState,
        management_state: ManagementState | None,
        policy: ExitPolicy,
        events: Sequence[ExitReplayEvent],
    ) -> ExitReplayResult:
        """Run fixed historical facts through the common candidate reducer.

        Entry/fill opportunities are deliberately supplied by the caller.  This
        prevents the old raw strategy lab from impersonating the production
        scanner/playbook/admission stack while still enabling exact exit-only
        matched comparisons.
        """

        self.mode = self.CANDIDATE_EXIT_REPLAY
        self.run_manifest = self._build_manifest()
        self.run_manifest.update(
            {
                "candidate_policy_version": policy.policy_version,
                "entry_policy": "FIXED_ENTRY_FILL_OPPORTUNITIES",
            }
        )
        return replay_exit_decisions(
            thesis=thesis,
            position_state=position_state,
            management_state=management_state,
            policy=policy,
            events=events,
            mode=ReplayMode.BACKTEST,
        )

    def run_candidate_execution(
        self,
        runner: CandidateRunner,
        *,
        context_policy: ContextPolicy = ContextPolicy(),
        clock_events: Sequence[datetime] = (),
    ) -> dict:
        """Execute fixed admitted entries through causal bars and shared risk.

        All symbols precede account MTM and policy evaluation. Deadlines are
        clock events even with absent candles. Candidate bar starts must be aware.
        """
        if runner.broker is not self.broker:
            raise ValueError("candidate runner must use this backtest's broker")
        self.mode = self.CANDIDATE_EXIT_REPLAY
        service = MarketContextService(context_policy, runner.clock.policy)
        timeline = {}
        histories = {}
        for symbol, frame in sorted(self.market_data.items()):
            histories[symbol] = []
            for row in frame.to_dict("records"):
                if row["date"].tzinfo is None:
                    raise ValueError("candidate data needs aware bar timestamps")
                start = as_utc(row["date"])
                row["date"] = start
                available = start + timedelta(
                    minutes=5, seconds=context_policy.availability_delay_seconds
                )
                for field in ("received_at", "available_at"):
                    if row.get(field) is not None:
                        observed = as_utc(row[field])
                        if observed is None:
                            raise ValueError(
                                "candle availability needs an aware timestamp"
                            )
                        available = max(available, observed)
                # Preserve the first availability of each immutable bar. Older
                # bars must not inherit every later context build's event time.
                row["received_at"] = available
                row["available_at"] = available
                if symbol in timeline.setdefault(available, {}):
                    raise ValueError(
                        "duplicate symbol availability requires a revised-data study"
                    )
                timeline[available][symbol] = row
        for at in clock_events:
            if at.tzinfo is None:
                raise ValueError("clock event requires an aware timestamp")
            timeline.setdefault(as_utc(at), {})
        checkpoint_at = max(
            [
                *self.broker._mark_times.values(),
                *(
                    self.broker._time(fill["exchange_time"])
                    for fill in self.broker.fills
                ),
            ],
            default=min(timeline) if timeline else None,
        )
        if checkpoint_at is not None:
            final_at = max([checkpoint_at, *timeline])
            day = runner.clock.snapshot(checkpoint_at).session_date
            final_day = runner.clock.snapshot(final_at).session_date
            while day <= final_day:
                deadline = datetime.combine(
                    day,
                    runner.clock.policy.forced_flatten_time,
                    tzinfo=runner.clock.policy.exchange_timezone,
                )
                if runner.clock.snapshot(deadline).is_trading_day:
                    timeline.setdefault(as_utc(deadline), {})
                day += timedelta(days=1)
        for at, candles in sorted(timeline.items()):
            contexts = {}
            for symbol, row in sorted(candles.items()):
                histories[symbol].append(row)
                if symbol in runner.positions:
                    managed = runner.positions[symbol]
                    contexts[symbol] = service.build(
                        managed.thesis.instrument_id,
                        histories[symbol],
                        at,
                        received_at=at,
                    )
            if at <= checkpoint_at:
                continue
            executable = {
                symbol: row
                for symbol, row in candles.items()
                if row["date"] >= checkpoint_at
            }
            runner.on_event(at, candles=executable, contexts=contexts)
        result = runner.finish()
        self.run_manifest = self._build_manifest() | result["manifest"]
        self.run_manifest["context_policy"] = dict(vars(context_policy))
        self.run_manifest["timestamp_convention"] = "BAR_START_AWARE"
        self.run_manifest["checkpoint_at"] = (
            checkpoint_at.isoformat() if checkpoint_at else None
        )
        self.run_manifest["warmup"] = "FEATURES_ONLY_BEFORE_CHECKPOINT"
        self.run_manifest["end_of_data"] = "CENSOR_RESIDUALS_AND_PENDING_ORDERS"
        result["manifest"] = self.run_manifest
        self.equity_curve = runner.equity_curve
        return result
