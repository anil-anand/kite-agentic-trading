import hashlib
import io
import json
import math
import os
from typing import Any, Dict

import pandas as pd

from ..strategies.base import BaseStrategy
from .backtest_engine import BacktestEngine
from .metrics_evaluator import MetricsEvaluator


class RegressionSuite:
    FIXTURE_MANIFEST_SCHEMA_VERSION = "research-fixture-v1"

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.last_manifest: Dict[str, Any] | None = None

    def load_fixture_manifest(self, test_name: str) -> Dict[str, Any] | None:
        """Load and verify a non-sensitive regression fixture manifest.

        Older local raw-lab CSVs remain readable, but a versioned fixture
        manifest is required for any result presented as curated exit research.
        The manifest pins the bytes, source label and declared model versions
        instead of trusting a mutable filename alone.
        """

        if (
            not isinstance(test_name, str)
            or not test_name
            or os.path.basename(test_name) != test_name
        ):
            raise ValueError("research fixture name must be a local basename")
        manifest_path = os.path.join(self.data_dir, f"{test_name}.manifest.json")
        if not os.path.exists(manifest_path):
            return None
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise ValueError("research fixture manifest must be an object")
        if manifest.get("schema_version") != self.FIXTURE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported research fixture manifest schema")
        if manifest.get("fixture_name") != test_name:
            raise ValueError("research fixture manifest name does not match file")
        if manifest.get("data_classification") not in {
            "SYNTHETIC_NON_SENSITIVE",
            "CURATED_NON_SENSITIVE",
        }:
            raise ValueError("research fixture data classification is required")
        if manifest.get("mode") not in {"REPLAY", "RAW_STRATEGY_LAB"}:
            raise ValueError("research fixture requires an explicit supported mode")
        for field in ("dataset_sha256", "policy_version", "execution_model_version"):
            if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                raise ValueError(f"research fixture manifest requires {field}")
        if manifest.get("timestamp_convention") != "BAR_START_UTC":
            raise ValueError("research fixture requires BAR_START_UTC timestamps")
        data_path = os.path.join(self.data_dir, f"{test_name}.csv")
        with open(data_path, "rb") as handle:
            actual_hash = hashlib.sha256(handle.read()).hexdigest()
        if actual_hash != manifest["dataset_sha256"]:
            raise ValueError("research fixture dataset hash does not match manifest")
        return manifest

    def _raw_fixture(self, test_name: str):
        """Read exactly the verified bytes and check the actual runner contract."""

        self.last_manifest = None
        manifest = self.load_fixture_manifest(test_name)
        with open(os.path.join(self.data_dir, f"{test_name}.csv"), "rb") as handle:
            dataset = handle.read()
        if manifest is not None:
            if hashlib.sha256(dataset).hexdigest() != manifest["dataset_sha256"]:
                raise ValueError("research fixture dataset changed after verification")
            if (
                manifest["mode"] != "RAW_STRATEGY_LAB"
                or manifest["policy_version"] != "raw-strategy-lab-v1"
            ):
                raise ValueError(
                    "raw-strategy regression cannot execute a candidate/replay fixture"
                )
        return pd.read_csv(io.BytesIO(dataset)), manifest

    @staticmethod
    def _verify_execution_manifest(engine, fixture_manifest):
        if (
            fixture_manifest is not None
            and fixture_manifest["execution_model_version"]
            != engine.broker.execution_policy.policy_version
        ):
            raise ValueError("fixture execution model does not match the actual runner")

    def run_regression_test(
        self, test_name: str, strategy: BaseStrategy, expected_metrics: Dict[str, Any]
    ) -> bool:
        """
        Runs a regression test using a known dataset and compares metrics.
        Raises AssertionError if metrics deviate significantly.
        """
        self.last_manifest = None
        data_path = os.path.join(self.data_dir, f"{test_name}.csv")
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"Regression data not found at {data_path}")

        df, fixture_manifest = self._raw_fixture(test_name)

        engine = BacktestEngine(strategy)
        self._verify_execution_manifest(engine, fixture_manifest)
        engine.load_data("TEST_SYM", df)
        engine.run()
        self.last_manifest = {
            **engine.run_manifest,
            "research_fixture": fixture_manifest,
        }

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
                if not math.isfinite(expected_value) or not math.isfinite(actual_value):
                    raise AssertionError(f"Metric {key} must be finite.")
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
        df, fixture_manifest = self._raw_fixture(test_name)

        engine = BacktestEngine(strategy)
        self._verify_execution_manifest(engine, fixture_manifest)
        engine.load_data("TEST_SYM", df)
        engine.run()
        self.last_manifest = {
            **engine.run_manifest,
            "research_fixture": fixture_manifest,
        }

        metrics = MetricsEvaluator.evaluate(
            engine.broker.trades, engine.broker.initial_capital
        )

        # We might only care about high-level metrics for regression
        important_keys = ["trade_count", "win_rate", "net_profit", "max_drawdown"]
        return {k: metrics[k] for k in important_keys if k in metrics}
