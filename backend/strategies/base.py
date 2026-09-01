import datetime
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import pandas as pd


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

    def calculate_stop_loss(
        self, entry: float, direction: str, percentage: float = None
    ) -> float:
        if percentage is None:
            from ..config import config_manager

            percentage = config_manager.get_risk_config().get(
                "defaultStopLossPercent", 1.5
            )

        if direction == "BUY":
            return round(entry * (1 - percentage / 100), 2)
        else:
            return round(entry * (1 + percentage / 100), 2)

    def calculate_target(
        self, entry: float, sl: float, percentage: float = None
    ) -> float:
        if percentage is None:
            from ..config import config_manager

            percentage = config_manager.get_risk_config().get(
                "defaultTargetPercent", 3.0
            )

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
        confidence: int,
        entry: float,
        sl: float,
        target: float,
        rr: float,
        reasoning: str,
        indicators: dict,
    ) -> Dict[str, Any]:
        return {
            "id": self.generate_signal_id(),
            "tradingsymbol": tradingsymbol,
            "exchange": "NSE",
            "strategy": self.get_name(),
            "direction": direction,
            "confidence": confidence,
            "entryPrice": entry,
            "stopLoss": sl,
            "target": target,
            "riskReward": rr,
            "reasoning": reasoning,
            "timestamp": datetime.datetime.now().isoformat(),
            "indicators": indicators,
        }
