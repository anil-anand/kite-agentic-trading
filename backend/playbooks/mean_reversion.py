import uuid
from typing import Any, Dict, List, Optional

from .base import BasePlaybook


class MeanReversionPlaybook(BasePlaybook):
    def get_name(self) -> str:
        return "Mean Reversion"

    def get_description(self) -> str:
        return "Fade extremes in ranging markets"

    def required_features(self) -> List[str]:
        return ["mean_reversion"]

    def applicable_regimes(self) -> List[str]:
        return ["RANGING"]

    def evaluate_entry(
        self, evidence: List[Dict[str, Any]], regime_state: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        mr_signals = [s for s in evidence if s.get("family") == "mean_reversion"]
        if not mr_signals:
            return None

        # Determine dominant direction
        buy_score = sum(
            s.get("signal_score", 0) for s in mr_signals if s.get("direction") == "BUY"
        )
        sell_score = sum(
            s.get("signal_score", 0) for s in mr_signals if s.get("direction") == "SELL"
        )

        if buy_score < 70 and sell_score < 70:
            return None

        direction = "BUY" if buy_score > sell_score else "SELL"
        base_sig = max(
            [s for s in mr_signals if s.get("direction") == direction],
            key=lambda s: s.get("signal_score", 0),
        )

        # Require multiple indicators for mean reversion to avoid catching falling knives
        dir_signals = [s for s in mr_signals if s.get("direction") == direction]
        if len(dir_signals) < 2:
            return None

        final_score = min(
            100, base_sig.get("signal_score", 0) + (len(dir_signals) - 1) * 5
        )
        reasoning = f"{self.get_name()}: {direction} exhaustion with {len(dir_signals)} indicators"

        return {
            "id": str(uuid.uuid4()),
            "tradingsymbol": base_sig.get("tradingsymbol"),
            "exchange": base_sig.get("exchange", "NSE"),
            "strategy": self.get_name(),
            "direction": direction,
            "signal_score": final_score,
            "entryPrice": base_sig.get("entryPrice"),
            "stopLoss": base_sig.get("stopLoss"),
            "target": base_sig.get("target"),
            "riskReward": base_sig.get("riskReward", 0),
            "reasoning": reasoning,
            "playbook": self.get_name(),
        }

    def evaluate_invalidation(
        self, position: Dict[str, Any], evaluation: Dict[str, Any]
    ) -> bool:
        # Invalidated if regime shifts to trending against us
        if evaluation.get("regime") == "TRENDING":
            return True

        direction = position.get("direction")
        opp_signals = evaluation.get(
            "sell_signals" if direction == "BUY" else "buy_signals", 0
        )

        return opp_signals > 2
