"""Causal, manifest-backed walk-forward research helpers.

The historical raw-strategy lab is retained for compatibility, but it is not a
candidate-policy promotion tool. This module plans half-open, non-overlapping
outer test folds and passes feature warmup separately from scored data so a
caller cannot let warmup orders or P&L alter an OOS result.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite
from numbers import Real
from typing import Any, Callable, Dict, Mapping, Sequence

import pandas as pd

from ..replay import serialize_replay_artifact
from ..time_utils import as_utc
from .backtest_engine import BacktestEngine
from .metrics_evaluator import MetricsEvaluator


@dataclass(frozen=True)
class WalkForwardConfig:
    """Predeclared temporal split and selection policy for one research run."""

    train_days: int = 30
    warmup_days: int = 10
    test_days: int = 10
    step_days: int = 10
    purge_days: int = 0
    embargo_days: int = 0
    final_holdout_days: int = 0
    selection_mode: str = "fixed"
    selection_criterion_id: str | None = None
    inner_validation_days: int = 0
    max_label_horizon_days: int = 0
    max_policy_candidates: int = 8
    policy_version: str = "fixed-policy-v1"
    study_id: str = "walk-forward-v1"
    prior_trial_count: int = 0
    label_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class WalkForwardFold:
    """One outer fold with data owned by explicit half-open intervals."""

    fold_id: str
    train_start: datetime
    train_end: datetime
    warmup_start: datetime
    test_start: datetime
    test_end: datetime
    inner_training_end: datetime | None = None
    inner_validation_start: datetime | None = None
    inner_validation_end: datetime | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "warmup_start": self.warmup_start.isoformat(),
            "warmup_end": self.test_start.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "inner_training_end": self.inner_training_end.isoformat()
            if self.inner_training_end
            else None,
            "inner_validation_start": self.inner_validation_start.isoformat()
            if self.inner_validation_start
            else None,
            "inner_validation_end": self.inner_validation_end.isoformat()
            if self.inner_validation_end
            else None,
        }


@dataclass(frozen=True)
class SelectionInput:
    """The only data supplied to a nested policy selector.

    Test data intentionally are not present in this object. The selector's
    returned artifact is frozen in the run manifest before the fold callback
    receives a selected policy.
    """

    fold: WalkForwardFold
    train_data: pd.DataFrame
    inner_validation_data: pd.DataFrame
    candidate_policies: Mapping[str, Any]


@dataclass(frozen=True)
class SelectionResult:
    policy_id: str
    artifact: Mapping[str, Any]
    criterion_id: str


FoldRunner = Callable[
    [WalkForwardFold, pd.DataFrame, pd.DataFrame, Any], Mapping[str, Any]
]
PolicySelector = Callable[[SelectionInput], SelectionResult]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        serialize_replay_artifact(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _dataset_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_json(
        orient="split", date_format="iso", date_unit="ns", double_precision=15
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WalkForwardValidator:
    """Run fixed or narrowly selected policies over causal OOS folds.

    ``validate`` retains the historical strategy-class interface. New studies
    should use :meth:`validate_study` with a common candidate-policy runner.
    That runner must execute the shared decision/coordinator stack; this class
    supplies timing isolation and immutable research metadata, not exit logic.
    """

    MANIFEST_SCHEMA_VERSION = "walk-forward-manifest-v1"

    def __init__(self, strategy_class=None, initial_capital: float = 100000.0):
        if (
            isinstance(initial_capital, bool)
            or not isinstance(initial_capital, Real)
            or not isfinite(initial_capital)
            or initial_capital <= 0
        ):
            raise ValueError("walk-forward initial_capital must be finite and positive")
        self.strategy_class = strategy_class
        self.initial_capital = initial_capital

    @staticmethod
    def _normalise_frame(df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame) or "date" not in df.columns:
            raise ValueError("walk-forward data requires a date column")
        frame = df.copy(deep=True)
        # pandas' deep copy does not detach dict/list object cells. Keep this
        # research table scalar-only so callbacks cannot mutate a later fold's
        # inputs through aliases. Unspecified attrs can also carry full future
        # datasets and are deliberately outside the callback data contract.
        if any(not pd.api.types.is_scalar(value) for value in frame.to_numpy().flat):
            raise ValueError("walk-forward input cells must contain scalar values")
        frame.attrs = {}
        for column in ("date", "available_at", "received_at", "label_end_at"):
            if column not in frame:
                continue
            normalised = []
            for value in frame[column]:
                timestamp = pd.Timestamp(value)
                if pd.isna(timestamp) or timestamp.tzinfo is None:
                    raise ValueError(
                        f"walk-forward {column} requires nonmissing aware timestamps"
                    )
                normalised.append(timestamp.tz_convert("UTC"))
            frame[column] = pd.to_datetime(normalised, utc=True)
            if column != "date" and (frame[column] < frame["date"]).any():
                raise ValueError(f"walk-forward {column} cannot precede date")
        frame = frame.sort_values("date").reset_index(drop=True)
        if frame.empty:
            raise ValueError("walk-forward data cannot be empty")
        if frame["date"].duplicated().any():
            raise ValueError("walk-forward data requires unique timestamps")
        return frame

    @staticmethod
    def _validate_config(config: WalkForwardConfig, policy_count: int) -> None:
        numeric = {
            "train_days": config.train_days,
            "warmup_days": config.warmup_days,
            "test_days": config.test_days,
            "step_days": config.step_days,
            "purge_days": config.purge_days,
            "embargo_days": config.embargo_days,
            "final_holdout_days": config.final_holdout_days,
            "inner_validation_days": config.inner_validation_days,
            "max_label_horizon_days": config.max_label_horizon_days,
            "max_policy_candidates": config.max_policy_candidates,
            "prior_trial_count": config.prior_trial_count,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in numeric.values()
        ):
            raise ValueError("walk-forward durations must be integer days")
        if config.train_days <= 0 or config.test_days <= 0 or config.step_days <= 0:
            raise ValueError("walk-forward train/test/step durations must be positive")
        nonnegative = (
            "warmup_days",
            "purge_days",
            "embargo_days",
            "final_holdout_days",
            "inner_validation_days",
            "max_label_horizon_days",
            "prior_trial_count",
        )
        if any(numeric[name] < 0 for name in nonnegative):
            raise ValueError(
                "walk-forward purge, embargo, warmup and holdout are nonnegative"
            )
        if config.max_policy_candidates <= 0:
            raise ValueError("max_policy_candidates must be positive")
        if config.step_days < config.test_days:
            raise ValueError(
                "step_days must be at least test_days for disjoint OOS windows"
            )
        if config.purge_days < config.max_label_horizon_days:
            raise ValueError("purge_days must cover max_label_horizon_days")
        if config.selection_mode not in {"fixed", "nested"}:
            raise ValueError("selection_mode must be fixed or nested")
        if not isinstance(config.policy_version, str) or not config.policy_version:
            raise ValueError("walk-forward policy_version is required")
        if not isinstance(config.study_id, str) or not config.study_id:
            raise ValueError("walk-forward study_id is required")
        if policy_count == 0 or policy_count > config.max_policy_candidates:
            raise ValueError("policy candidate count is outside the predeclared budget")
        if config.selection_mode == "fixed" and policy_count != 1:
            raise ValueError("fixed-policy validation requires exactly one policy")
        if config.selection_mode == "nested":
            if (
                not isinstance(config.selection_criterion_id, str)
                or not config.selection_criterion_id.strip()
            ):
                raise ValueError("nested selection requires a predeclared criterion")
            if config.inner_validation_days <= 0:
                raise ValueError("nested selection requires an inner validation window")
            if (
                config.inner_validation_days + config.purge_days + config.embargo_days
                >= config.train_days
            ):
                raise ValueError(
                    "inner validation, purge and embargo must fit inside training"
                )
        if not isinstance(config.label_columns, tuple) or not all(
            isinstance(name, str)
            and name
            and name not in {"date", "available_at", "received_at", "label_end_at"}
            for name in config.label_columns
        ):
            raise ValueError("label_columns must name supervised outcome columns")

    @staticmethod
    def _slice(
        frame: pd.DataFrame,
        start: datetime,
        end: datetime,
        *,
        include_labels: bool = False,
        label_columns: Sequence[str] = (),
    ) -> pd.DataFrame:
        mask = (frame["date"] >= start) & (frame["date"] < end)
        # A bar belongs to this interval only if it was actually observable by
        # its cutoff. Supplied label horizons override date-only assumptions.
        for column in ("available_at", "received_at"):
            if column in frame:
                mask &= frame[column] < end
        if include_labels and "label_end_at" in frame:
            mask &= frame["label_end_at"] < end
        result = frame.loc[mask].copy(deep=True)
        if not include_labels:
            result = result.drop(
                columns=["label_end_at", *label_columns], errors="ignore"
            )
        return result

    def plan_folds(
        self, df: pd.DataFrame, config: WalkForwardConfig
    ) -> tuple[list[WalkForwardFold], datetime | None, datetime]:
        """Build outer folds without ever adding a boundary bar to two tests."""

        self._validate_config(config, policy_count=1)
        frame = self._normalise_frame(df)
        first = frame["date"].iloc[0]
        final_exclusive = frame["date"].iloc[-1] + pd.Timedelta(nanoseconds=1)
        holdout_start = (
            final_exclusive - timedelta(days=config.final_holdout_days)
            if config.final_holdout_days
            else final_exclusive
        )
        # A purge plus embargo sits between the last selectable training outcome
        # and the first scored OOS event. Warmup can read earlier bars only as
        # features; it has no account/order state in this validator.
        separation = timedelta(days=config.purge_days + config.embargo_days)
        test_start = first + timedelta(days=config.train_days) + separation
        folds: list[WalkForwardFold] = []
        previous_end: datetime | None = None
        while test_start < holdout_start:
            test_end = test_start + timedelta(days=config.test_days)
            # A tiny terminal slice must not count as another independent OOS
            # fold or borrow outcomes from the reserved holdout to complete it.
            if test_end > holdout_start:
                break
            train_end = test_start - separation
            train_start = train_end - timedelta(days=config.train_days)
            if train_start < first:
                test_start += timedelta(days=config.step_days)
                continue
            if previous_end is not None and test_start < previous_end:
                raise ValueError(
                    "walk-forward planner produced overlapping OOS windows"
                )
            inner_training_end = inner_start = inner_end = None
            if config.selection_mode == "nested":
                inner_end = train_end
                inner_start = inner_end - timedelta(days=config.inner_validation_days)
                inner_training_end = inner_start - separation
            fold = WalkForwardFold(
                fold_id=f"oos-{len(folds) + 1:03d}",
                train_start=train_start,
                train_end=train_end,
                warmup_start=max(
                    first, test_start - timedelta(days=config.warmup_days)
                ),
                test_start=test_start,
                test_end=test_end,
                inner_training_end=inner_training_end,
                inner_validation_start=inner_start,
                inner_validation_end=inner_end,
            )
            folds.append(fold)
            previous_end = test_end
            test_start += timedelta(days=config.step_days)
        return (
            folds,
            (holdout_start if config.final_holdout_days else None),
            final_exclusive,
        )

    @staticmethod
    def _validate_selection(
        result: SelectionResult,
        policies: Mapping[str, Any],
        criterion_id: str,
    ) -> None:
        if not isinstance(result, SelectionResult):
            raise TypeError("nested policy selector must return SelectionResult")
        if result.policy_id not in policies:
            raise ValueError(
                "nested selector chose a policy outside the preregistered set"
            )
        if not isinstance(result.criterion_id, str) or not result.criterion_id.strip():
            raise ValueError("nested selector must identify its predeclared criterion")
        # A direct best-P&L choice is intentionally not an acceptable promotion
        # criterion. Callers may use a documented multi-objective/stability rule.
        prohibited = {"pnl", "net_pnl", "net_profit", "profit"}
        if result.criterion_id.strip().lower() in prohibited:
            raise ValueError("nested selection cannot optimize historical P&L directly")
        if result.criterion_id != criterion_id:
            raise ValueError("nested selector changed the predeclared criterion")
        if not isinstance(result.artifact, Mapping):
            raise TypeError("nested selector artifact must be a mapping")

    @staticmethod
    def _assert_scored_trades(
        trades: Sequence[Mapping[str, Any]], fold: WalkForwardFold
    ) -> None:
        MetricsEvaluator.validate_walk_forward_trade_window(
            trades, fold.test_start, fold.test_end
        )

    @staticmethod
    def _assert_scored_equity(
        equity_curve: Sequence[Mapping[str, Any]], fold: WalkForwardFold
    ) -> None:
        previous = None
        for point in equity_curve:
            raw_timestamp = point.get("timestamp")
            timestamp = pd.Timestamp(raw_timestamp)
            if pd.isna(timestamp) or timestamp.tzinfo is None:
                raise ValueError("walk-forward equity requires aware timestamps")
            timestamp = as_utc(timestamp)
            # An end-boundary snapshot is permitted, but it must represent
            # only fills/marks already available inside the half-open window.
            if timestamp < fold.test_start or timestamp > fold.test_end:
                raise ValueError("warmup or out-of-window equity leaked into OOS")
            if previous is not None and timestamp < previous:
                raise ValueError("walk-forward equity timestamps must be ordered")
            previous = timestamp

    def validate_study(
        self,
        df: pd.DataFrame,
        symbol: str,
        *,
        config: WalkForwardConfig,
        policies: Mapping[str, Any],
        runner: FoldRunner,
        selector: PolicySelector | None = None,
        source_commit: str | None = None,
    ) -> Dict[str, Any]:
        """Execute a predeclared fixed/nested study through an injected runner.

        The runner receives ``(fold, warmup_data, test_data, selected_policy)``.
        It must use warmup solely to build features and start a fresh account at
        the scoring boundary; returned warmup trades/equity are rejected. The
        callback remains responsible for causal processing inside the test
        interval and for reporting unresolved exposure. Unverified callbacks
        cannot establish production decision parity or promotion eligibility.
        Nested selectors receive only training and inner-validation frames.
        """

        if not isinstance(symbol, str) or not symbol:
            raise ValueError("walk-forward symbol is required")
        if not callable(runner):
            raise TypeError("walk-forward runner must be callable")
        if not isinstance(policies, Mapping) or not all(
            isinstance(policy_id, str) and policy_id for policy_id in policies
        ):
            raise TypeError("walk-forward policies must be keyed by nonempty strings")
        self._validate_config(config, len(policies))
        if config.selection_mode == "nested" and not callable(selector):
            raise ValueError("nested selection requires a selector")
        if config.selection_mode == "fixed" and selector is not None:
            raise ValueError("fixed-policy validation does not accept a selector")

        frame = self._normalise_frame(df)
        if config.label_columns and (
            "label_end_at" not in frame
            or any(column not in frame for column in config.label_columns)
        ):
            raise ValueError("declared labels require columns and label_end_at")
        folds, holdout_start, final_exclusive = self.plan_folds(frame, config)
        if not folds:
            raise ValueError("insufficient data for a complete walk-forward OOS fold")
        # Freeze the complete preregistered candidate set before an inner
        # selector runs. Selectors and runners receive independent copies, so
        # an in-place experiment cannot silently alter a later fold's policy.
        frozen_policies = deepcopy(dict(policies))
        policy_artifact = json.loads(_canonical_json(frozen_policies))
        policy_hash = hashlib.sha256(
            _canonical_json(policy_artifact).encode()
        ).hexdigest()
        fold_reports = []
        all_trades: list[dict[str, Any]] = []
        trial_count = config.prior_trial_count

        for fold in folds:
            train_data = self._slice(
                frame, fold.train_start, fold.train_end, include_labels=True
            )
            warmup_data = self._slice(
                frame,
                fold.warmup_start,
                fold.test_start,
                label_columns=config.label_columns,
            )
            test_data = self._slice(
                frame,
                fold.test_start,
                fold.test_end,
                label_columns=config.label_columns,
            )
            if train_data.empty or test_data.empty:
                raise ValueError(
                    f"{fold.fold_id} has an empty training or test interval"
                )
            if config.selection_mode == "nested":
                selection_train_data = self._slice(
                    frame,
                    fold.train_start,
                    fold.inner_training_end,
                    include_labels=True,
                )
                inner_data = self._slice(
                    frame,
                    fold.inner_validation_start,
                    fold.inner_validation_end,
                    include_labels=True,
                )
                if selection_train_data.empty or inner_data.empty:
                    raise ValueError(f"{fold.fold_id} has an empty inner split")
                selection = selector(
                    SelectionInput(
                        fold=fold,
                        train_data=selection_train_data.copy(deep=True),
                        inner_validation_data=inner_data.copy(deep=True),
                        candidate_policies=deepcopy(frozen_policies),
                    )
                )
                self._validate_selection(
                    selection, frozen_policies, config.selection_criterion_id
                )
                selected_policy_id = selection.policy_id
                # JSON round-trip both validates and detaches the frozen
                # artifact before the runner can mutate caller-owned state.
                selection_artifact = json.loads(_canonical_json(selection.artifact))
                criterion_id = selection.criterion_id
                trial_count += len(frozen_policies)
            else:
                selected_policy_id = next(iter(frozen_policies))
                selection_artifact = {"selection": "PREDECLARED_FIXED_POLICY"}
                criterion_id = "PREDECLARED_FIXED_POLICY"
                trial_count += 1

            outcome = dict(
                runner(
                    fold,
                    warmup_data.copy(deep=True),
                    test_data.copy(deep=True),
                    deepcopy(frozen_policies[selected_policy_id]),
                )
            )
            trades = deepcopy([dict(trade) for trade in outcome.get("trades", ())])
            self._assert_scored_trades(trades, fold)
            equity_curve = deepcopy(
                [dict(point) for point in outcome.get("equity_curve", ())]
            )
            self._assert_scored_equity(equity_curve, fold)
            metrics = MetricsEvaluator.evaluate(
                trades, self.initial_capital, equity_curve=equity_curve
            )
            all_trades.extend(trades)
            fold_reports.append(
                {
                    "fold": fold.to_dict(),
                    "selected_policy_id": selected_policy_id,
                    "selected_policy_artifact_hash": hashlib.sha256(
                        _canonical_json(frozen_policies[selected_policy_id]).encode()
                    ).hexdigest(),
                    "selection_criterion_id": criterion_id,
                    "selection_artifact": selection_artifact,
                    "selection_artifact_hash": hashlib.sha256(
                        _canonical_json(selection_artifact).encode()
                    ).hexdigest(),
                    "warmup": {
                        "mode": "FEATURES_ONLY",
                        "rows": len(warmup_data),
                        "dataset_hash": _dataset_hash(warmup_data),
                    },
                    "training": {
                        "rows": len(train_data),
                        "dataset_hash": _dataset_hash(train_data),
                    },
                    "test": {
                        "rows": len(test_data),
                        "dataset_hash": _dataset_hash(test_data),
                    },
                    "runner_artifact": json.loads(
                        _canonical_json(outcome.get("artifact", {}))
                    ),
                    "execution_state": json.loads(
                        _canonical_json(
                            {
                                "coverage": outcome.get(
                                    "execution_coverage", "UNVERIFIED_CALLBACK"
                                ),
                                "censored_positions": outcome.get("censored_positions"),
                                "pending_orders": outcome.get("pending_orders"),
                            }
                        )
                    ),
                    "metrics": metrics,
                    "trades": trades,
                    "equity_curve": equity_curve,
                }
            )

        aggregate = MetricsEvaluator.evaluate_walk_forward(
            fold_reports, self.initial_capital
        )
        manifest = {
            "schema_version": self.MANIFEST_SCHEMA_VERSION,
            "study_id": config.study_id,
            "symbol": symbol,
            "source_commit": source_commit,
            "dataset_hash": _dataset_hash(frame),
            "dataset_rows": len(frame),
            "dataset_start": frame["date"].iloc[0].isoformat(),
            "dataset_end_exclusive": final_exclusive.isoformat(),
            "config": asdict(config),
            "initial_account_state": {
                "initial_capital": self.initial_capital,
                "positions": [],
                "pending_orders": [],
                "daily_counters": "RESET_AT_SCORING_BOUNDARY",
            },
            "runner_contract": "EXTERNAL_CALLBACK_UNVERIFIED",
            "timestamp_policy": {
                "timezone": "UTC_NORMALIZED_FROM_EXPLICIT_OFFSET",
                "date": "ROW_EVENT_OR_BAR_START_AS_DECLARED_BY_RUNNER",
                "availability": "MAX_AVAILABLE_AT_RECEIVED_AT_BEFORE_INTERVAL_END",
                "labels": "LABEL_END_AT_BEFORE_SELECTION_CUTOFF_WHEN_SUPPLIED",
                "test_processing": "CALLBACK_MUST_HONOR_EVENT_AVAILABILITY",
            },
            "candidate_policy_ids": list(frozen_policies),
            "candidate_policy_artifacts": policy_artifact,
            "policy_artifact_hash": policy_hash,
            "trial_count": trial_count,
            "trial_count_basis": (
                "DECLARED_PRIOR_TRIALS_PLUS_PREREGISTERED_FOLD_CANDIDATES"
            ),
            "incomplete_terminal_window": "EXCLUDED_FROM_OOS_FOLD_COUNT",
            "folds": [report["fold"] for report in fold_reports],
            "fold_results": [
                {
                    key: deepcopy(report[key])
                    for key in (
                        "fold",
                        "selected_policy_id",
                        "selected_policy_artifact_hash",
                        "selection_criterion_id",
                        "selection_artifact",
                        "selection_artifact_hash",
                        "warmup",
                        "training",
                        "test",
                        "runner_artifact",
                        "execution_state",
                        "metrics",
                    )
                }
                for report in fold_reports
            ],
            "untouched_holdout": {
                "start": holdout_start.isoformat() if holdout_start else None,
                "end_exclusive": final_exclusive.isoformat() if holdout_start else None,
                "status": "RESERVED_UNOBSERVED" if holdout_start else "NOT_CONFIGURED",
            },
            "promotion_status": "RESEARCH_EVIDENCE_ONLY",
            "candidate_live_activation": "FORBIDDEN_BY_LIVE_ORCHESTRATOR",
        }
        return {
            "symbol": symbol,
            "strategy": None,
            "mode": "WALK_FORWARD_RESEARCH",
            "manifest": manifest,
            "overall_oos_metrics": aggregate,
            "windows": fold_reports,
            "all_oos_trades": all_trades,
        }

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        train_days: int = 30,
        test_days: int = 10,
        step_days: int = 10,
    ) -> Dict[str, Any]:
        """Compatibility wrapper for the explicitly labelled raw-strategy lab.

        It no longer executes warmup orders. The raw lab remains unsuitable for
        candidate-policy promotion because it does not model production entry
        selection/admission; the manifest records that limitation.
        """

        if self.strategy_class is None:
            raise ValueError("raw-strategy walk-forward validation needs a strategy")
        config = WalkForwardConfig(
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            policy_version="raw-strategy-lab-v1",
            study_id="raw-strategy-walk-forward-v1",
        )

        def raw_runner(
            fold: WalkForwardFold,
            warmup_data: pd.DataFrame,
            test_data: pd.DataFrame,
            _policy: Any,
        ) -> Mapping[str, Any]:
            # The engine receives causal feature history, but its trading gate
            # prevents any warmup signal/order from affecting the fold account.
            engine = BacktestEngine(
                self.strategy_class(), initial_capital=self.initial_capital
            )
            engine.load_data(
                symbol,
                pd.concat([warmup_data, test_data], ignore_index=True),
            )
            result = engine.run(trading_start_at=fold.test_start)
            return {
                "trades": result["trades"],
                "equity_curve": result["equity_curve"],
                "artifact": result["manifest"],
                "censored_positions": result.get("censored_positions"),
                "pending_orders": deepcopy(list(engine.broker.pending_orders)),
                "execution_coverage": "RAW_STRATEGY_LAB_ONLY",
            }

        result = self.validate_study(
            df,
            symbol,
            config=config,
            policies={"raw-strategy-lab-v1": {"mode": "RAW_STRATEGY_LAB"}},
            runner=raw_runner,
        )
        result["strategy"] = self.strategy_class().get_name()
        result["manifest"]["mode"] = BacktestEngine.RAW_STRATEGY_LAB
        result["manifest"]["runner_contract"] = "RAW_STRATEGY_LAB_ONLY"
        result["manifest"]["entry_policy"] = "RAW_STRATEGY_LAB_ONLY"
        result["manifest"]["promotion_status"] = "NOT_ELIGIBLE_RAW_STRATEGY_LAB"
        return result
