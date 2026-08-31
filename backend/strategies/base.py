from abc import ABC, abstractmethod
import pandas as pd
from typing import Dict, Any, List
import uuid
import datetime

class BaseStrategy(ABC):
    
    @abstractmethod
    def get_name(self) -> str:
        pass
        
    @abstractmethod
    def get_description(self) -> str:
        pass
        
    @abstractmethod
    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str) -> List[Dict[str, Any]]:
        pass
        
    def calculate_stop_loss(self, entry: float, direction: str, percentage: float = 0.5) -> float:
        if direction == "BUY":
            return round(entry * (1 - percentage/100), 2)
        else:
            return round(entry * (1 + percentage/100), 2)
            
    def calculate_target(self, entry: float, sl: float, rr_ratio: float = 2.0) -> float:
        risk = abs(entry - sl)
        if entry > sl: # BUY
            return round(entry + (risk * rr_ratio), 2)
        else: # SELL
            return round(entry - (risk * rr_ratio), 2)
            
    def generate_signal_id(self) -> str:
        return str(uuid.uuid4())
        
    def format_signal(self, tradingsymbol: str, direction: str, confidence: int, entry: float, sl: float, target: float, rr: float, reasoning: str, indicators: dict) -> Dict[str, Any]:
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
            "indicators": indicators
        }
