import datetime
from typing import Any, Dict

import pandas as pd

from .backtest_engine import BacktestEngine
from .metrics_evaluator import MetricsEvaluator


class WalkForwardValidator:
    def __init__(self, strategy_class, initial_capital: float = 100000.0):
        self.strategy_class = strategy_class
        self.initial_capital = initial_capital

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        train_days: int = 30,
        test_days: int = 10,
        step_days: int = 10,
    ) -> Dict[str, Any]:
        """
        Runs a walk-forward validation on a dataset.
        Splits data into multiple rolling windows of train/test.
        Evaluates out-of-sample (test) performance.
        """
        if "date" in df.columns and not pd.api.types.is_datetime64_any_dtype(
            df["date"]
        ):
            df["date"] = pd.to_datetime(df["date"])

        df = df.sort_values("date").reset_index(drop=True)
        start_date = df["date"].min()
        end_date = df["date"].max()

        results = []
        all_test_trades = []

        current_start = start_date

        while current_start + datetime.timedelta(days=train_days) < end_date:
            train_end = current_start + datetime.timedelta(days=train_days)
            test_end = train_end + datetime.timedelta(days=test_days)

            # test slice bounds
            if test_end > end_date:
                test_end = end_date

            test_df = df[(df["date"] >= train_end) & (df["date"] <= test_end)]

            if len(test_df) > 0:
                # Initialize engine for OOS backtest
                strategy_instance = self.strategy_class()
                engine = BacktestEngine(
                    strategy_instance, initial_capital=self.initial_capital
                )

                # To simulate properly, the engine needs some history before the test_df for indicators.
                # So we pass train_df + test_df but only record trades opened during test_df?
                # Actually, BacktestEngine loops over all dates in the provided data.
                # A rigorous walk-forward would pass a warm-up period.
                warmup_start = train_end - datetime.timedelta(
                    days=10
                )  # 10 days warm-up
                eval_df = df[(df["date"] >= warmup_start) & (df["date"] <= test_end)]

                engine.load_data(symbol, eval_df)
                engine.run()

                # Filter trades to only those entered during the test period
                test_trades = [
                    t for t in engine.broker.trades if t["entry_time"] >= train_end
                ]
                all_test_trades.extend(test_trades)

                window_metrics = MetricsEvaluator.evaluate(
                    test_trades, self.initial_capital
                )
                results.append(
                    {
                        "window_start": train_end.strftime("%Y-%m-%d"),
                        "window_end": test_end.strftime("%Y-%m-%d"),
                        "metrics": window_metrics,
                    }
                )

            current_start += datetime.timedelta(days=step_days)

        # Overall OOS Metrics
        overall_metrics = MetricsEvaluator.evaluate(
            all_test_trades, self.initial_capital
        )

        return {
            "symbol": symbol,
            "strategy": self.strategy_class().get_name(),
            "overall_oos_metrics": overall_metrics,
            "windows": results,
        }
