from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BasePlaybook(ABC):
    @abstractmethod
    def get_name(self) -> str:
        pass

    @abstractmethod
    def get_description(self) -> str:
        pass

    @abstractmethod
    def required_features(self) -> List[str]:
        """Features required to be present in the indicator snapshot."""
        pass

    @abstractmethod
    def applicable_regimes(self) -> List[str]:
        """List of regimes (e.g. TRENDING, RANGING, BREAKOUT) where this playbook applies."""
        pass

    @abstractmethod
    def evaluate_entry(
        self, evidence: List[Dict[str, Any]], regime_state: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate raw strategy evidence and regime state to decide on entry.
        Returns a structured trade decision dictionary if conditions are met, else None.
        """
        pass

    @abstractmethod
    def evaluate_invalidation(
        self, position: Dict[str, Any], evaluation: Dict[str, Any]
    ) -> bool:
        """
        Evaluate if the thesis is invalidated based on current evaluation.
        """
        pass
