"""Frozen correctness-repaired legacy research control, never a live service.

Normal rules mirror the causal legacy level rejection and aggregated-strategy
review. The shared runner still owns hard supervision and execution handoffs.
No corrupt prices, fabricated fills, or wall-clock strategy configuration enter
this comparator. Its limitations are policy behavior, not accounting defects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import import_module
from math import isfinite

from ..broker_models import OrderRole
from ..exit_management.models import ExposureState, LifecycleEvent, reduce_lifecycle
from ..order_lifecycle import IntentType
from ..strategies.base import BaseStrategy
from ..strategies.breakout_evidence import BreakoutEvidence
from ..strategies.oscillator_evidence import OscillatorEvidence
from .candidate_runner import CandidateRunner

# Frozen production strategy catalog; source artifact pins these implementations.
_CATALOG = {
    "ema_crossover": "EMACrossoverStrategy",
    "rsi_reversal": "RSIReversalStrategy",
    "vwap_bounce": "VWAPBounceStrategy",
    "supertrend": "SupertrendStrategy",
    "macd_cross": "MACDCrossStrategy",
    "bollinger_breakout": "BollingerBreakoutStrategy",
    "stochastic_reversal": "StochasticReversalStrategy",
    "adx_momentum": "ADXMomentumStrategy",
    "psar_trend": "PSARTrendStrategy",
    "donchian_breakout": "DonchianBreakoutStrategy",
    "cci_reversal": "CCIReversalStrategy",
    "williams_r": "WilliamsRStrategy",
    "mfi_exhaustion": "MFIExhaustionStrategy",
    "keltner_breakout": "KeltnerBreakoutStrategy",
    "awesome_oscillator": "AwesomeOscillatorStrategy",
    "tsi_cross": "TSICrossStrategy",
    "stoc_rsi": "StochRSIStrategy",
}
FAMILIES = {
    key: (
        "breakout"
        if key in {"bollinger_breakout", "keltner_breakout", "donchian_breakout"}
        else "trend"
        if key
        in {
            "ema_crossover",
            "macd_cross",
            "supertrend",
            "psar_trend",
            "tsi_cross",
            "adx_momentum",
            "awesome_oscillator",
        }
        else "mean_reversion"
    )
    for key in _CATALOG
}


def production_strategies():
    return {
        key: getattr(import_module(f"backend.strategies.{key}"), name)()
        for key, name in _CATALOG.items()
    }


@dataclass(frozen=True)
class LegacyControlPolicy:
    policy_version: str = "correctness-repaired-legacy-v1"
    level_lookback: int = 20
    review_interval_minutes: float = 30
    weak_exit_minutes: float = 15
    breakeven_minutes: float = 45

    def __post_init__(self):
        if (
            not self.policy_version
            or isinstance(self.level_lookback, bool)
            or not isinstance(self.level_lookback, int)
            or self.level_lookback < 1
        ):
            raise ValueError("legacy policy requires version and positive lookback")
        for value in (
            self.review_interval_minutes,
            self.weak_exit_minutes,
            self.breakeven_minutes,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError("legacy review durations must be finite and positive")


class LegacyControlRunner(CandidateRunner):
    def __init__(
        self,
        *,
        legacy_policy=None,
        strategy_config=None,
        risk_config=None,
        strategies=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.strategy_config = dict(strategy_config or {})
        self.risk_config = dict(risk_config or {})
        review_policy = {
            "review_interval_minutes": self.risk_config.get(
                "positionRevalIntervalMins", 30
            ),
            "weak_exit_minutes": self.risk_config.get("positionRevalWeakExitMins", 15),
            "breakeven_minutes": self.risk_config.get("positionRevalBreakevenMins", 45),
            **dict(legacy_policy or {}),
        }
        self.legacy_policy = LegacyControlPolicy(**review_policy)
        self.strategies = (
            strategies if strategies is not None else production_strategies()
        )
        self.legacy_records = []
        self._legacy_last_review = {}
        self._legacy_last_bar = {}

    def _fixed_objective(self, thesis, policy):
        return thesis.objective

    def on_event(self, at, *, candles=None, contexts=None):
        # No candidate structural context enters the comparator. Hard risk and
        # existing immutable fixed objectives still use the same coordinator.
        result = super().on_event(at, candles=candles, contexts={})
        for symbol, managed in sorted(self.positions.items()):
            context = (contexts or {}).get(symbol)
            position = self.broker.positions.get(symbol)
            if (
                not position
                or managed.state.exposure is not ExposureState.OPEN
                or managed.coordinator_intent_id
            ):
                continue
            mark_time = self.broker._mark_times.get(symbol)
            if (
                mark_time is None
                or not 0
                <= (at - mark_time).total_seconds()
                <= managed.policy.hard_risk_policy.mark_max_age_seconds
            ):
                continue
            key = managed.state.position_key
            close = self.broker._prices[symbol]
            entry, target = position["entry_price"], managed.thesis.objective
            long = position["direction"] == "BUY"
            reason = None
            frame = None
            if target and (close >= target if long else close <= target):
                reason = "LEGACY_CONTROL_TARGET"
            else:
                if (
                    context is None
                    or not context.normal_decision_eligible
                    or context.primary_bar.start < position["entry_time"]
                ):
                    continue
                if self._legacy_last_bar.get(key) == context.primary_bar.start:
                    continue
                self._legacy_last_bar[key] = context.primary_bar.start
                frame = context.primary_frame()
                close = float(frame.iloc[-1]["close"])
            lookback = self.legacy_policy.level_lookback
            if reason is None and target and len(frame) >= lookback + 1:
                past, last = frame.iloc[-lookback - 1 : -1], frame.iloc[-1]
                level = float(past["high"].max() if long else past["low"].min())
                if (
                    entry < level < target and last["high"] >= level > close
                    if long
                    else target < level < entry and last["low"] <= level < close
                ):
                    reason = "LEGACY_CONTROL_LEVEL_REJECTION"
            last_review = self._legacy_last_review.get(key, position["entry_time"])
            supporting = opposing = None
            held = (at - position["entry_time"]).total_seconds() / 60
            tighten = False
            if (
                reason is None
                and (at - last_review).total_seconds() / 60
                >= self.legacy_policy.review_interval_minutes
            ):
                enabled = [
                    (name, strategy)
                    for name, strategy in self.strategies.items()
                    if self.strategy_config.get(name, {}).get("enabled", False)
                ]
                if (
                    len(frame) >= 50
                    and enabled
                    and "VOLUME_UNAVAILABLE" not in context.primary_quality.issues
                ):
                    raw = []
                    try:
                        for name, strategy in enabled:
                            signals = (
                                strategy.calculate_signals_with_context(
                                    frame.copy(deep=True),
                                    symbol,
                                    risk_config=self.risk_config,
                                    decision_at=at,
                                )
                                if isinstance(strategy, BaseStrategy)
                                else strategy.calculate_signals(
                                    frame.copy(deep=True), symbol
                                )
                            )
                            raw.extend(
                                dict(
                                    signal, strategy_id=name, family=FAMILIES.get(name)
                                )
                                for signal in signals
                            )
                    except Exception:
                        self.legacy_records.append(
                            {
                                "at": at,
                                "position_key": key,
                                "action": "UNAVAILABLE",
                                "reason": "STRATEGY_FAILURE",
                            }
                        )
                        continue
                    aggregated = BreakoutEvidence.aggregate(
                        OscillatorEvidence.aggregate(raw), frame.copy(deep=True)
                    )
                    supporting = sum(
                        s.get("direction") == position["direction"] for s in aggregated
                    )
                    opposing = sum(
                        s.get("direction") == ("SELL" if long else "BUY")
                        for s in aggregated
                    )
                    if opposing >= 2 and supporting == 0:
                        reason = "LEGACY_CONTROL_OPPOSING_SIGNALS"
                    elif (
                        supporting == 0
                        and (close < entry if long else close > entry)
                        and held >= self.legacy_policy.weak_exit_minutes
                    ):
                        reason = "LEGACY_CONTROL_WEAK_CONVICTION"
                    elif held >= self.legacy_policy.breakeven_minutes:
                        tighten = (
                            entry > position["sl"] if long else entry < position["sl"]
                        )
                    self._legacy_last_review[key] = at
            record = {
                "at": at,
                "position_key": key,
                "action": "REQUEST_EXIT"
                if reason
                else "TIGHTEN_STOP"
                if tighten
                else "HOLD",
                "reason": reason or "LEGACY_CONTROL_REVIEW",
                "supporting": supporting,
                "opposing": opposing,
            }
            self.legacy_records.append(record)
            if reason:
                response = self._submit_reduction(symbol, at, reason)
                managed.coordinator_intent_id = response.intent_id
                managed.state = reduce_lifecycle(
                    managed.state,
                    LifecycleEvent.EXIT_REQUESTED,
                    event_id=f"legacy:{response.intent_id}",
                    occurred_at=at,
                    exit_intent_id=response.intent_id,
                )
                self.execution_results.append(
                    {
                        "legacy_record_index": len(self.legacy_records) - 1,
                        "intent_id": response.intent_id,
                        "order_id": response.broker_order_id,
                        "state": response.state,
                    }
                )
            elif tighten:
                # Frozen legacy break-even may tighten only; shared protection
                # adapter confirms the full residual before memory advances.
                stop = self.broker.get_order(position.get("stop_order_id"))

                def amend(tag):
                    order_id = self.broker.set_protective_stop(
                        symbol, entry, at, stop_limit=stop["order_type"] == "SL"
                    )
                    self.broker.orders[order_id]["tag"] = tag
                    return order_id

                response = self.coordinator.submit(
                    position_key=managed.broker_position_key,
                    intent_type=IntentType.TIGHTEN,
                    role=OrderRole.PROTECTION,
                    side="SELL" if long else "BUY",
                    quantity=position["quantity"],
                    payload={"stop_price": entry, "timestamp": at.isoformat()},
                    submit_order=amend,
                    reason="LEGACY_CONTROL_TIME_BREAKEVEN",
                )
                observed = self.broker.get_order(response.broker_order_id)
                if (
                    observed
                    and observed["status"] in {"OPEN", "TRIGGER PENDING"}
                    and observed["remaining_quantity"] == position["quantity"]
                ):
                    self.coordinator.observe_order(response.intent_id, observed)
                    self.coordinator.journal.complete_order_intent(response.intent_id)
                    managed.management = replace(
                        managed.management, confirmed_stop=entry
                    )
                self.execution_results.append(
                    {
                        "legacy_record_index": len(self.legacy_records) - 1,
                        "intent_id": response.intent_id,
                        "order_id": response.broker_order_id,
                        "state": response.state,
                    }
                )
        return result

    def finish(self):
        result = super().finish()
        result["legacy_control_decisions"] = list(self.legacy_records)
        result["manifest"]["control_policy"] = dict(vars(self.legacy_policy))
        return result
