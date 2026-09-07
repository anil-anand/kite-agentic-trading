import uuid
from typing import Any, Dict, List, Optional

from .base import BasePlaybook


class BreakoutPlaybook(BasePlaybook):
    def get_name(self) -> str:
        return "Breakout"

    def get_description(self) -> str:
        return "Enter on price breaking out of a range with high momentum"

    def required_features(self) -> List[str]:
        return ["breakout", "volatility"]

    def applicable_regimes(self) -> List[str]:
        return ["BREAKOUT"]

    def evaluate_entry(
        self, evidence: List[Dict[str, Any]], regime_state: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        breakout_signals = [s for s in evidence if s.get("family") == "breakout"]
        if not breakout_signals:
            return None

        # Determine dominant direction
        buy_score = sum(
            s.get("signal_score", 0)
            for s in breakout_signals
            if s.get("direction") == "BUY"
        )
        sell_score = sum(
            s.get("signal_score", 0)
            for s in breakout_signals
            if s.get("direction") == "SELL"
        )

        if (
            buy_score < 75 and sell_score < 75
        ):  # Require higher conviction for breakouts
            return None

        direction = "BUY" if buy_score > sell_score else "SELL"
        base_sig = max(
            [s for s in breakout_signals if s.get("direction") == direction],
            key=lambda s: s.get("signal_score", 0),
        )

        # Check for trend confirmation
        trend_signals = [s for s in evidence if s.get("family") == "trend"]
        trend_confirmed = any(s.get("direction") == direction for s in trend_signals)

        final_score = base_sig.get("signal_score", 0)
        reasoning = f"{self.get_name()}: {direction} breakout detected"
        if trend_confirmed:
            final_score = min(100, final_score + 10)
            reasoning += " with trend confirmation"

        if final_score < 75:
            return None

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
        # Invalidated if regime shifts to ranging (fakeout) or strong counter signals
        if evaluation.get("regime") == "RANGING":
            return True

        direction = position.get("direction")
        opp_signals = evaluation.get(
            "sell_signals" if direction == "BUY" else "buy_signals", 0
        )

        return opp_signals >= 2  # Less tolerant of counter signals on breakouts
