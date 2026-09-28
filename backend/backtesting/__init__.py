"""Research-only execution adapters.

The raw strategy lab and the candidate exit replay are deliberately separate
entry points.  Neither imports or reaches the live trading journal/broker.
"""

from .backtest_engine import BacktestEngine
from .paper_broker import PaperBroker
from .promotion import PromotionCriteria, PromotionGateResult, evaluate_promotion_gate
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy
from .walk_forward import WalkForwardConfig, WalkForwardValidator

__all__ = [
    "BacktestEngine",
    "PaperBroker",
    "PromotionCriteria",
    "PromotionGateResult",
    "SimulatedBroker",
    "SimulationExecutionPolicy",
    "WalkForwardConfig",
    "WalkForwardValidator",
    "evaluate_promotion_gate",
]
