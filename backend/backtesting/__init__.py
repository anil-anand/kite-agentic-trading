"""Research-only execution adapters.

The raw strategy lab and the candidate exit replay are deliberately separate
entry points.  Neither imports or reaches the live trading journal/broker.
"""

from .backtest_engine import BacktestEngine
from .paper_broker import PaperBroker
from .simulated_broker import SimulatedBroker, SimulationExecutionPolicy

__all__ = [
    "BacktestEngine",
    "PaperBroker",
    "SimulatedBroker",
    "SimulationExecutionPolicy",
]
