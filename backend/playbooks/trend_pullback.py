import uuid
from typing import Any, Dict, List, Optional

from .base import BasePlaybook


class TrendPullbackPlaybook(BasePlaybook):
    def get_name(self) -> str:
        return "Trend Pullback"

    def get_description(self) -> str:
        return "Enter in direction of strong trend on minor pullbacks"

    def required_features(self) -> List[str]:
        return ["trend", "momentum"]

    def applicable_regimes(self) -> List[str]:
        return ["TRENDING"]

    def evaluate_entry(
        self, evidence: List[Dict[str, Any]], regime_state: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        # Requires at least one trend signal indicating direction
        trend_signals = [s for s in evidence if s.get("family") == "trend"]
        if not trend_signals:
            return None

        # Determine dominant direction
        buy_score = sum(
            s.get("signal_score", 0)
            for s in trend_signals
            if s.get("direction") == "BUY"
        )
        sell_score = sum(
            s.get("signal_score", 0)
            for s in trend_signals
            if s.get("direction") == "SELL"
        )

        if buy_score < 70 and sell_score < 70:
            return None

        direction = "BUY" if buy_score > sell_score else "SELL"
        base_sig = max(
            [s for s in trend_signals if s.get("direction") == direction],
            key=lambda s: s.get("signal_score", 0),
        )

        # Check for pullback confirmation from mean reversion indicators
        mr_signals = [s for s in evidence if s.get("family") == "mean_reversion"]
        pullback_confirmed = any(s.get("direction") == direction for s in mr_signals)

        # We can enter either on pure strong trend or trend + pullback
        final_score = base_sig.get("signal_score", 0)
        reasoning = f"{self.get_name()}: Strong {direction} trend"
        if pullback_confirmed:
            final_score = min(100, final_score + 10)
            reasoning += " with mean reversion pullback confirmation"

        if final_score < 70:
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
        # Invalidated if regime shifts out of trend or strong counter-trend signals appear
        if evaluation.get("regime") != "TRENDING":
            return True

        direction = position.get("direction")
        opp_signals = evaluation.get(
            "sell_signals" if direction == "BUY" else "buy_signals", 0
        )
        dir_signals = evaluation.get(
            "buy_signals" if direction == "BUY" else "sell_signals", 0
        )

        return opp_signals > dir_signals + 1
