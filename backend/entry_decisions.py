"""Pure production entry-decision assembly for research and live adapters.

The strategy implementations and playbooks remain the source of entry formulas.
This module only owns their deterministic composition: completed-input selection,
evidence aggregation, playbook selection, calibration lookup, and reproducible
provenance.  It deliberately has no scanner cache, configuration singleton,
broker, journal, or wall-clock dependency.

Keeping this boundary small is important for phase 8: a paper/backtest adapter
can use the same production selection semantics without importing a live broker
or the current journal/calibrator.  The old ``BacktestEngine(strategy)`` raw
strategy runner stays a separately labelled lab tool.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

import pandas as pd

from .market_context import MarketContext
from .strategies.base import BaseStrategy
from .time_utils import as_utc


class CalibrationLookup(Protocol):
    """The read-only, as-of calibration artifact contract.

    A caller may inject the legacy descriptive lookup for compatibility, a
    frozen research artifact, or ``None``.  This protocol intentionally does
    not imply that the returned value is an exit probability.
    """

    def __call__(self, playbook: str, signal_score: float) -> tuple[Any, Any]: ...


def _canonical_frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        frame.to_json(orient="split", date_format="iso", double_precision=15).encode(
            "utf-8"
        )
    ).hexdigest()


def _normalise_signal_metadata(
    signals: Iterable[Mapping[str, Any]], *, decision_at: datetime
) -> list[dict[str, Any]]:
    """Remove strategy-generated wall-clock/random metadata from research output.

    Existing strategies use :meth:`BaseStrategy.format_signal`, which was
    historically allowed to generate a UUID and call ``now_utc``.  Neither is
    entry evidence.  Retaining either in a replay input makes otherwise
    identical historical decisions hash differently.  We copy and normalise
    before aggregation so aggregate raw evidence is reproducible too.
    """

    timestamp = decision_at.isoformat()
    normalised: list[dict[str, Any]] = []
    for item in signals:
        signal = deepcopy(dict(item))
        signal.pop("id", None)
        signal["timestamp"] = timestamp
        normalised.append(signal)
    return normalised


def _probability(
    lookup: CalibrationLookup | None, playbook: str, score: Any
) -> tuple[Any, Any]:
    if lookup is None:
        return None, None
    result = lookup(playbook, score)
    if not isinstance(result, tuple) or len(result) != 2:
        raise ValueError("calibration lookup must return (value, sample_size)")
    return result


def _observed_incomplete_frame(
    frame: pd.DataFrame, context: MarketContext, event_time: datetime
) -> pd.DataFrame:
    """Restrict the legacy experiment to a snapshot observed by this event.

    An incomplete candle is permitted; a future candle or later revision is
    not. Historical callers must supply actual as-of snapshots to this mode,
    since final OHLC alone cannot reconstruct an incomplete candle's path.
    """

    def timestamp(value: Any) -> datetime | None:
        try:
            parsed = pd.Timestamp(value)
            return None if pd.isna(parsed) else as_utc(parsed.to_pydatetime())
        except (TypeError, ValueError, OverflowError):
            return None

    cutoff = timestamp(frame.attrs.get("source_as_of", context.source_as_of))
    receipt = timestamp(frame.attrs.get("received_at", context.received_at))
    if cutoff is None or receipt is None or max(cutoff, receipt) > event_time:
        raise ValueError("incomplete entry snapshot was not available at decision time")
    if "date" not in frame:
        raise ValueError("incomplete entry snapshot requires candle timestamps")
    starts = frame["date"].map(timestamp)
    observed = starts.notna() & (starts <= min(cutoff, event_time))
    for column in ("received_at", "available_at"):
        if column in frame:
            times = frame[column].map(timestamp)
            observed &= times.notna() & (times <= event_time)
    return frame.loc[observed].copy(deep=True)


def evaluate_production_entries(
    *,
    symbol: str,
    raw_frame: pd.DataFrame,
    market_context: MarketContext,
    strategies: Mapping[str, Any],
    playbooks: Sequence[Any],
    strategy_config: Mapping[str, Any],
    family_mapping: Mapping[str, str],
    calibration_lookup: CalibrationLookup | None,
    decision_at: datetime,
    risk_config: Mapping[str, Any] | None = None,
    evaluate_on_incomplete_candle: bool = False,
    entry_policy_version: str = "playbooks-v1",
) -> list[dict[str, Any]]:
    """Evaluate the production entry stack with fully injected dependencies.

    ``raw_frame`` and the frame provided to every strategy are copied deeply;
    strategy indicator helpers are therefore unable to mutate a scanner cache
    or another strategy's input.  ``decision_at`` is explicit and becomes the
    only decision timestamp in returned records.
    """

    if not isinstance(decision_at, datetime) or decision_at.utcoffset() is None:
        raise ValueError("entry evaluation needs an aware decision time")
    event_time = as_utc(decision_at)
    if event_time != market_context.decision_event_time:
        raise ValueError("entry decision time must match the as-of market context")
    if not isinstance(raw_frame, pd.DataFrame):
        raise TypeError("raw_frame must be a DataFrame")
    if not market_context.entry_history_ready or not (
        market_context.normal_decision_eligible
        or market_context.analysis_decision_eligible
    ):
        return []
    if any(
        bar.end > event_time or bar.available_at > event_time
        for bar in (*market_context.primary_bars, *market_context.higher_bars)
    ):
        raise ValueError("entry context contains bars unavailable at decision time")
    # Preserve the scanner's established admission behavior.  Phase 8 only
    # extracts it; broadening the incomplete-candle experiment is an entry
    # policy change and belongs to a separately measured task.
    if "VOLUME_UNAVAILABLE" in market_context.primary_quality.issues:
        return []

    strategy_config = deepcopy(dict(strategy_config))
    family_mapping = dict(family_mapping)
    evaluate_on_incomplete_candle = (
        evaluate_on_incomplete_candle and not market_context.analysis_only
    )
    decision_frame = (
        _observed_incomplete_frame(raw_frame, market_context, event_time)
        if evaluate_on_incomplete_candle
        else market_context.primary_frame()
    )
    if decision_frame.empty:
        return []

    if evaluate_on_incomplete_candle:
        # This remains an explicitly labelled legacy entry experiment.  Normal
        # candidate exits always receive completed context in the exit engine.
        from .regime_classifier import regime_classifier

        regime_state = regime_classifier.classify(decision_frame.copy(deep=True))
    else:
        regime_state = {
            "regime": market_context.raw_regime,
            "features": dict(market_context.raw_regime_features),
        }
    regime = regime_state["regime"]

    raw_signals: list[dict[str, Any]] = []
    # Omitted risk settings mean the declared legacy percentage defaults, never
    # whatever happens to be configured in today's live account.
    frozen_risk_config = deepcopy(dict(risk_config or {}))
    for strategy_id, strategy in strategies.items():
        settings = strategy_config.get(strategy_id, {})
        if not isinstance(settings, Mapping) or not settings.get("enabled", False):
            continue
        frame = decision_frame.copy(deep=True)
        if isinstance(strategy, BaseStrategy):
            generated = strategy.calculate_signals_with_context(
                frame, symbol, risk_config=frozen_risk_config, decision_at=event_time
            )
        else:
            generated = strategy.calculate_signals(frame, symbol)
        for signal in _normalise_signal_metadata(generated, decision_at=event_time):
            signal["strategy_id"] = strategy_id
            signal["family"] = family_mapping.get(strategy_id)
            raw_signals.append(signal)

    from .strategies.breakout_evidence import BreakoutEvidence
    from .strategies.oscillator_evidence import OscillatorEvidence

    aggregated = OscillatorEvidence.aggregate(raw_signals)
    aggregated = BreakoutEvidence.aggregate(aggregated, decision_frame.copy(deep=True))

    context_summary = market_context.summary()
    entry_input = {
        "mode": "INCOMPLETE_CANDLE"
        if evaluate_on_incomplete_candle
        else "COMPLETED_CANDLES",
        "last_input_bar": json.loads(
            decision_frame.tail(1).to_json(orient="records", date_format="iso")
        )[0],
        "frame_hash": _canonical_frame_hash(decision_frame),
        "hash_format": "pandas-split-iso-v1",
        "decision_at": event_time.isoformat(),
    }
    selection_config = {
        "strategies": deepcopy(dict(strategy_config)),
        "risk": frozen_risk_config,
        "family_mapping": dict(family_mapping),
        "context_policy": context_summary.get("policy"),
        "entry_policy_version": entry_policy_version,
    }

    decisions: list[dict[str, Any]] = []
    for playbook in playbooks:
        if regime not in playbook.applicable_regimes():
            continue
        decision = playbook.evaluate_entry(deepcopy(aggregated), deepcopy(regime_state))
        if not decision:
            continue
        decision = deepcopy(decision)
        decision["timestamp"] = event_time.isoformat()
        decision.pop("id", None)
        probability, sample_size = _probability(
            calibration_lookup, playbook.get_name(), decision.get("signal_score")
        )
        raw = decision.get("raw_signals", aggregated)
        raw = _normalise_signal_metadata(raw, decision_at=event_time)
        strategy_ids = {
            item.get("strategy_id")
            for item in raw
            if item.get("strategy_id")
            and item.get("strategy_id")
            not in {"breakout_evidence", "oscillator_evidence"}
        }
        strategy_ids.update(
            item["strategy_id"]
            for item in raw
            if item.get("strategy_id") in {"breakout_evidence", "oscillator_evidence"}
        )
        decision.update(
            {
                "estimated_probability": probability,
                "calibration_sample_size": sample_size,
                "indicators": deepcopy(regime_state["features"]),
                "raw_signals": raw,
                "regime": regime,
                "strategy_count": len(strategy_ids),
                "market_context": context_summary,
                "entry_selection_config": selection_config,
                "entry_input": entry_input,
                "entry_decision_version": "production-entry-v1",
            }
        )
        decisions.append(decision)

        if market_context.analysis_only:
            decision["analysisOnly"] = True
            decision["analysisAsOf"] = market_context.primary_bar.end.isoformat()

    return decisions
