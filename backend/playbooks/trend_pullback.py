import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional

from .base import BasePlaybook


class TrendPullbackPlaybook(BasePlaybook):
    def management_profile(self) -> str:
        return "trend_continuation"

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
        pullback_signal = next(
            (signal for signal in mr_signals if signal.get("direction") == direction),
            None,
        )
        pullback_confirmed = pullback_signal is not None

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
            "playbook_version": "playbooks-v1",
            "management_profile": self.management_profile(),
            "setup_variant": (
                "trend_with_mean_reversion_signal"
                if pullback_confirmed
                else "trend_continuation_trigger"
            ),
            # Preserve precisely the inputs this playbook actually used.  The
            # scanner may include broader raw evidence for diagnostics, but it
            # must not be mistaken for selected confirmation later.
            "selected_evidence": [
                deepcopy(base_sig),
                *([deepcopy(pullback_signal)] if pullback_signal else []),
            ],
            # Opposing trend scores participated in direction selection too.
            # Retain them separately from supporting confirmation evidence.
            "selection_inputs": deepcopy(trend_signals + mr_signals),
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
