"""Isolated real-market-data paper adapter.

It accepts already acquired market events from a caller but has no Kite client,
journal, calibration service, or credential path.  PAPER orders therefore can
never reach a live account merely because a process happens to have one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

import pandas as pd

from ..broker_models import ExecutionNamespace
from ..exit_management.engine import ExitPolicy
from ..exit_management.models import ManagementState, PositionState
from ..exit_management.thesis import EntryThesis
from ..order_lifecycle import OrderLifecycleCoordinator
from ..replay import (
    ExitReplayEvent,
    ExitReplayResult,
    ReplayMode,
    replay_exit_decisions,
)
from ..session_clock import SessionPolicy
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy


class PaperBroker(SimulatedBroker):
    """Execution simulator for externally supplied, timestamped market data."""

    def __init__(
        self,
        initial_capital: float = 100000.0,
        *,
        data_source_id: str = "external-market-data",
        execution_policy: Optional[SimulationExecutionPolicy] = None,
        account_id: str = "paper",
    ):
        if not isinstance(data_source_id, str) or not data_source_id.strip():
            raise ValueError("paper runs need a nonempty data source id")
        super().__init__(
            initial_capital,
            execution_policy=execution_policy,
            namespace=ExecutionNamespace.PAPER,
            account_id=account_id,
        )
        self.data_source_id = data_source_id

    @property
    def execution_manifest(self) -> dict[str, Any]:
        manifest = super().execution_manifest
        manifest.update(
            {
                "adapter": "paper-broker-v1",
                "data_source_id": self.data_source_id,
                "live_broker_reachable": False,
            }
        )
        return manifest

    def ingest_candle(self, symbol: str, candle: Mapping[str, Any]) -> None:
        """Consume one caller-provided real-data candle without fetching it."""

        candle = dict(candle)
        start = candle.get("date")
        available_at = candle.get("available_at")
        if any(
            not isinstance(value, datetime) or value.utcoffset() is None
            for value in (start, available_at)
        ):
            raise ValueError("paper candles require aware start and availability times")
        if available_at < start + timedelta(minutes=5):
            raise ValueError("paper candles must be completed before ingestion")
        # A delayed candle's close was observed at bar end. Receiving it later
        # cannot refresh its price for the hard-risk freshness gate.
        candle["mark_time"] = start + timedelta(minutes=5)
        self.process_candle(symbol, pd.Series(candle))

    def mark(self, symbol: str, price: float, observed_at: datetime) -> None:
        """Record a quote mark for paper MTM without advancing normal policy."""

        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("paper marks require an aware observation time")
        observed_at = self._time(observed_at)
        price = self._finite_price(price)
        self.mark_price(symbol, price, observed_at)
        self.events.append(
            {
                "type": "PAPER_MARK",
                "symbol": symbol,
                "price": price,
                "at": observed_at.isoformat(),
            }
        )

    def create_candidate_runner(
        self,
        *,
        coordinator: OrderLifecycleCoordinator,
        session_policy: SessionPolicy = SessionPolicy(),
        daily_loss_limit: Optional[float] = None,
    ):
        """Manage real-data paper positions with the shared execution coordinator.

        The caller supplies an explicitly isolated journal-backed coordinator,
        registers verified paper entry fills, and feeds market/clock events.
        """
        from .candidate_runner import CandidateRunner

        return CandidateRunner(
            broker=self,
            coordinator=coordinator,
            session_policy=session_policy,
            daily_loss_limit=daily_loss_limit,
        )

    def replay_candidate_exit(
        self,
        *,
        thesis: EntryThesis | None,
        position_state: PositionState,
        management_state: ManagementState | None,
        policy: ExitPolicy,
        events: tuple[ExitReplayEvent, ...],
    ) -> ExitReplayResult:
        """Evaluate paper facts with the shared candidate decision adapter."""

        return replay_exit_decisions(
            thesis=thesis,
            position_state=position_state,
            management_state=management_state,
            policy=policy,
            events=events,
            mode=ReplayMode.PAPER,
        )
