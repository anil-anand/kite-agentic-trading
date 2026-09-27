import uuid
from abc import ABC, abstractmethod
from copy import copy, deepcopy
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from ..time_utils import now_utc


class BaseStrategy(ABC):
    @abstractmethod
    def get_name(self) -> str:
        pass

    @abstractmethod
    def get_description(self) -> str:
        pass

    @abstractmethod
    def calculate_signals(
        self, df: pd.DataFrame, tradingsymbol: str
    ) -> List[Dict[str, Any]]:
        pass

    def calculate_signals_with_context(
        self,
        df: pd.DataFrame,
        tradingsymbol: str,
        *,
        risk_config: Mapping[str, Any],
        decision_at: datetime,
    ) -> List[Dict[str, Any]]:
        """Run the unchanged formula with isolated settings and event time.

        Scanner strategies are shared across symbol worker threads. Bind inputs
        on a private instance, never on that shared instance or the live config
        singleton. Direct raw-strategy callers retain their legacy defaults.
        """

        strategy = copy(self)
        strategy._evaluation_risk_config = deepcopy(dict(risk_config))
        strategy._evaluation_decision_at = decision_at
        return strategy.calculate_signals(df, tradingsymbol)

    def _risk_config(self) -> Mapping[str, Any]:
        injected = getattr(self, "_evaluation_risk_config", None)
        if injected is not None:
            return injected
        from ..config import config_manager

        return config_manager.get_risk_config()

    def calculate_stop_loss(
        self, entry: float, direction: str, percentage: float = None
    ) -> float:
        if percentage is None:
            percentage = self._risk_config().get("defaultStopLossPercent", 1.5)

        if direction == "BUY":
            return round(entry * (1 - percentage / 100), 2)
        else:
            return round(entry * (1 + percentage / 100), 2)

    def calculate_target(
        self, entry: float, sl: float, percentage: float = None
    ) -> float:
        if percentage is None:
            percentage = self._risk_config().get("defaultTargetPercent", 3.0)

        if entry > sl:  # BUY
            return round(entry * (1 + percentage / 100), 2)
        else:  # SELL
            return round(entry * (1 - percentage / 100), 2)

    def generate_signal_id(self) -> str:
        return str(uuid.uuid4())

    def format_signal(
        self,
        tradingsymbol: str,
        direction: str,
        signal_score: int,
        entry: float,
        sl: float,
        target: float,
        rr: float,
        reasoning: str,
        indicators: dict,
        *,
        decision_at: Optional[datetime] = None,
        evaluation_metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Format a strategy event with optionally injected reproducible metadata.

        Existing live strategy callers retain their historic timestamp behavior.
        Production-entry/replay adapters may supply ``decision_at`` so a
        wall-clock never leaks into a deterministic historical output.  The
        metadata is provenance only; it cannot alter a strategy formula.
        """

        timestamp = decision_at or getattr(self, "_evaluation_decision_at", None)
        if timestamp is None:
            timestamp = now_utc()
        return {
            "id": self.generate_signal_id(),
            "tradingsymbol": tradingsymbol,
            "exchange": "NSE",
            "strategy": self.get_name(),
            "direction": direction,
            "signal_score": signal_score,
            "entryPrice": entry,
            "stopLoss": sl,
            "target": target,
            "riskReward": rr,
            "reasoning": reasoning,
            "timestamp": timestamp.isoformat(),
            "indicators": indicators,
            "evaluation_metadata": dict(evaluation_metadata or {}),
        }
