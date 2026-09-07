import os
from typing import Any, Dict

import pandas as pd

from ..strategies.base import BaseStrategy
from .backtest_engine import BacktestEngine
from .metrics_evaluator import MetricsEvaluator


class RegressionSuite:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    def run_regression_test(
        self, test_name: str, strategy: BaseStrategy, expected_metrics: Dict[str, Any]
    ) -> bool:
        """
        Runs a regression test using a known dataset and compares metrics.
        Raises AssertionError if metrics deviate significantly.
        """
        data_path = os.path.join(self.data_dir, f"{test_name}.csv")
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"Regression data not found at {data_path}")

        df = pd.read_csv(data_path)

        engine = BacktestEngine(strategy)
        engine.load_data("TEST_SYM", df)
        engine.run()

        metrics = MetricsEvaluator.evaluate(
            engine.broker.trades, engine.broker.initial_capital
        )

        # Compare key metrics
        for key, expected_value in expected_metrics.items():
            if key not in metrics:
                raise AssertionError(f"Expected metric {key} not found in results.")

            actual_value = metrics[key]

            # Allow minor floating point deviations
            if isinstance(expected_value, (int, float)) and isinstance(
                actual_value, (int, float)
            ):
                if abs(expected_value - actual_value) > 0.05:
                    raise AssertionError(
                        f"Metric {key} mismatch. Expected: {expected_value}, Actual: {actual_value}"
                    )
            else:
                if expected_value != actual_value:
                    raise AssertionError(
                        f"Metric {key} mismatch. Expected: {expected_value}, Actual: {actual_value}"
                    )

        return True

    def generate_regression_baseline(
        self, test_name: str, strategy: BaseStrategy
    ) -> Dict[str, Any]:
        """
        Utility to generate the expected metrics for a new regression test dataset.
        """
        data_path = os.path.join(self.data_dir, f"{test_name}.csv")
        df = pd.read_csv(data_path)

        engine = BacktestEngine(strategy)
        engine.load_data("TEST_SYM", df)
        engine.run()

        metrics = MetricsEvaluator.evaluate(
            engine.broker.trades, engine.broker.initial_capital
        )

        # We might only care about high-level metrics for regression
        important_keys = ["trade_count", "win_rate", "net_profit", "max_drawdown"]
        return {k: metrics[k] for k in important_keys if k in metrics}
