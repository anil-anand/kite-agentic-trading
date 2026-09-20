"""Causal, completed-candle market context for normal trading decisions.

The scanner historically passed whatever the historical endpoint returned to
strategies.  This module is the narrow boundary between that mutable broker
payload and a decision input: it validates and normalizes five-minute OHLCV,
selects only bars that were available at the decision time, and derives a
session-aligned fifteen-minute view.  It deliberately has no broker, database,
or wall-clock dependency; callers provide both event and receipt times.

The objects returned here are immutable value objects.  A caller that needs a
DataFrame receives a fresh one through :meth:`MarketContext.primary_frame`, so
indicator implementations cannot mutate another consumer's cached input.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from .indicators import SessionVWAP
from .session_clock import SessionClock, SessionPolicy
from .time_utils import EXCHANGE_TIMEZONE, as_utc


class ContextQuality(str, Enum):
    """Availability/validity of one market-data source.

    These values are intentionally not directional.  In particular, STALE,
    GAP and INVALID must never be converted into bearish evidence by a caller.
    """

    VALID = "VALID"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    INVALID = "INVALID"
    GAP = "GAP"

    @property
    def usable(self) -> bool:
        return self is ContextQuality.VALID


@dataclass(frozen=True)
class ContextPolicy:
    """Versioned, deliberately small market-context policy."""

    policy_version: str = "market-context-v1"
    primary_interval_minutes: int = 5
    higher_interval_minutes: int = 15
    availability_delay_seconds: int = 0
    max_primary_age_seconds: int = 600
    max_higher_age_seconds: int = 1_200
    setup_range_bars: int = 20
    swing_confirmation_bars: int = 2
    regime_transition_confirm_bars: int = 2

    def __post_init__(self) -> None:
        if (self.primary_interval_minutes, self.higher_interval_minutes) != (5, 15):
            raise ValueError("market context requires 5m primary and 15m higher bars")
        for name in (
            "setup_range_bars",
            "swing_confirmation_bars",
            "regime_transition_confirm_bars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "availability_delay_seconds",
            "max_primary_age_seconds",
            "max_higher_age_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")

    @classmethod
    def from_config(cls, values: Optional[Mapping[str, Any]]) -> "ContextPolicy":
        values = values if isinstance(values, Mapping) else {}

        def positive_int(name: str, default: int, minimum: int = 1) -> int:
            value = values.get(name, default)
            if isinstance(value, bool):
                return default
            try:
                parsed = float(value)
                if not math.isfinite(parsed) or not parsed.is_integer():
                    return default
                value = int(parsed)
            except (TypeError, ValueError, OverflowError):
                return default
            return value if value >= minimum else default

        return cls(
            policy_version=str(values.get("policyVersion") or "market-context-v1"),
            # The broker adapter fetches 5m bars. Relabeling them through
            # configuration cannot turn them into a different source interval.
            primary_interval_minutes=5,
            higher_interval_minutes=15,
            availability_delay_seconds=positive_int(
                "availabilityDelaySeconds", 0, minimum=0
            ),
            max_primary_age_seconds=positive_int(
                "maxPrimaryAgeSeconds", 600, minimum=0
            ),
            max_higher_age_seconds=positive_int(
                "maxHigherAgeSeconds", 1_200, minimum=0
            ),
            setup_range_bars=positive_int("setupRangeBars", 20),
            swing_confirmation_bars=positive_int("swingConfirmationBars", 2),
            regime_transition_confirm_bars=positive_int(
                "regimeTransitionConfirmBars", 2
            ),
        )


@dataclass(frozen=True)
class OHLCVBar:
    """One normalized, source-identifiable OHLCV bar."""

    bar_id: str
    start: datetime
    end: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]
    revision: str


@dataclass(frozen=True)
class SourceQuality:
    status: ContextQuality
    issues: Tuple[str, ...] = ()
    expected_bars: int = 0
    missing_bars: Tuple[datetime, ...] = ()
    latest_bar_end: Optional[datetime] = None
    latest_available_at: Optional[datetime] = None
    age_seconds: Optional[float] = None

    @property
    def usable(self) -> bool:
        return self.status.usable


@dataclass(frozen=True)
class KnownLevel:
    level_id: str
    kind: str
    price: float
    formed_at: datetime
    known_at: datetime
    source_bar_ids: Tuple[str, ...]


@dataclass(frozen=True)
class SetupRange:
    low: float
    high: float
    start: datetime
    end: datetime
    known_at: datetime
    source_bar_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RegimeTransition:
    raw_regime: str = "UNCERTAIN"
    confirmed_regime: str = "UNCERTAIN"
    transition_candidate: Optional[str] = None
    transition_age: int = 0


@dataclass(frozen=True)
class MarketContext:
    """A reproducible market snapshot for one explicit decision event."""

    snapshot_id: str
    instrument_id: str
    session_id: Optional[str]
    decision_event_time: datetime
    received_at: datetime
    source_as_of: datetime
    feature_version: str
    primary_bar: Optional[OHLCVBar]
    higher_bar: Optional[OHLCVBar]
    primary_bars: Tuple[OHLCVBar, ...]
    higher_bars: Tuple[OHLCVBar, ...]
    primary_quality: SourceQuality
    higher_quality: SourceQuality
    input_bar_ids: Tuple[str, ...]
    input_hash: str
    session_vwap: Optional[float]
    atr: Optional[float]
    direction_dynamics: Mapping[str, Optional[float]]
    participation: Mapping[str, Optional[float]]
    observation_quality: Mapping[str, ContextQuality]
    known_structure: Tuple[KnownLevel, ...]
    setup_range: Optional[SetupRange]
    raw_regime: str
    raw_regime_features: Mapping[str, Any]
    confirmed_regime: str
    transition_candidate: Optional[str]
    transition_age: int
    context_policy: Optional[ContextPolicy] = None

    @property
    def normal_decision_eligible(self) -> bool:
        """Whether normal completed-candle management can consume this input."""

        return self.primary_quality.usable and self.primary_bar is not None

    def primary_frame(self) -> pd.DataFrame:
        """Return a new DataFrame; no cached frame is shared with strategies."""

        return _bars_to_frame(self.primary_bars)

    def higher_frame(self) -> pd.DataFrame:
        """Return a new DataFrame for the completed higher-timeframe bars."""

        return _bars_to_frame(self.higher_bars)

    def summary(self) -> Dict[str, Any]:
        """JSON-friendly provenance intended for current scanner consumers."""

        def bar_summary(bar: Optional[OHLCVBar]) -> Optional[Dict[str, Any]]:
            if bar is None:
                return None
            return {
                "bar_id": bar.bar_id,
                "start": bar.start.isoformat(),
                "end": bar.end.isoformat(),
                "available_at": bar.available_at.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "revision": bar.revision,
            }

        setup_range = None
        if self.setup_range is not None:
            setup_range = {
                "low": self.setup_range.low,
                "high": self.setup_range.high,
                "start": self.setup_range.start.isoformat(),
                "end": self.setup_range.end.isoformat(),
                "known_at": self.setup_range.known_at.isoformat(),
                "source_bar_ids": list(self.setup_range.source_bar_ids),
            }

        return {
            "snapshot_id": self.snapshot_id,
            "instrument_id": self.instrument_id,
            "session_id": self.session_id,
            "decision_event_time": self.decision_event_time.isoformat(),
            "received_at": self.received_at.isoformat(),
            "source_as_of": self.source_as_of.isoformat(),
            "feature_version": self.feature_version,
            "context_policy_version": self.feature_version,
            "policy": asdict(self.context_policy) if self.context_policy else None,
            "primary_bar": bar_summary(self.primary_bar),
            "higher_bar": bar_summary(self.higher_bar),
            "primary_bar_id": self.primary_bar.bar_id if self.primary_bar else None,
            "primary_bar_start": self.primary_bar.start.isoformat()
            if self.primary_bar
            else None,
            "primary_bar_end": self.primary_bar.end.isoformat()
            if self.primary_bar
            else None,
            "primary_available_at": self.primary_bar.available_at.isoformat()
            if self.primary_bar
            else None,
            "higher_bar_id": self.higher_bar.bar_id if self.higher_bar else None,
            "higher_bar_start": self.higher_bar.start.isoformat()
            if self.higher_bar
            else None,
            "higher_bar_end": self.higher_bar.end.isoformat()
            if self.higher_bar
            else None,
            "higher_available_at": self.higher_bar.available_at.isoformat()
            if self.higher_bar
            else None,
            "primary_quality": self.primary_quality.status.value,
            "higher_quality": self.higher_quality.status.value,
            "primary_issues": list(self.primary_quality.issues),
            "higher_issues": list(self.higher_quality.issues),
            "input_hash": self.input_hash,
            "session_vwap": self.session_vwap,
            "atr": self.atr,
            "direction_dynamics": dict(self.direction_dynamics),
            "participation": dict(self.participation),
            "setup_range": setup_range,
            "known_structure": [
                {
                    "level_id": level.level_id,
                    "kind": level.kind,
                    "price": level.price,
                    "formed_at": level.formed_at.isoformat(),
                    "known_at": level.known_at.isoformat(),
                    "source_bar_ids": list(level.source_bar_ids),
                }
                for level in self.known_structure
            ],
            "observation_quality": {
                key: value.value for key, value in self.observation_quality.items()
            },
            "raw_regime": self.raw_regime,
            "confirmed_regime": self.confirmed_regime,
            "transition_candidate": self.transition_candidate,
            "transition_age": self.transition_age,
        }


@dataclass(frozen=True)
class _NormalizedCandles:
    frame: pd.DataFrame
    issues: Tuple[str, ...]
    missing_bars: Tuple[datetime, ...]
    expected_bars: int
    pending_bars: bool = False


@dataclass
class _RegimeState:
    last_decision_at: Optional[datetime] = None
    last_bar_end: Optional[datetime] = None
    session_id: Optional[str] = None
    confirmed_regime: str = "UNCERTAIN"
    candidate: Optional[str] = None
    candidate_age: int = 0


class MarketContextService:
    """Builds contexts and owns only immutable cache/transition bookkeeping."""

    def __init__(
        self,
        policy: Optional[ContextPolicy] = None,
        session_policy: Optional[SessionPolicy] = None,
    ) -> None:
        self.policy = policy or ContextPolicy()
        self.session_clock = SessionClock(session_policy or SessionPolicy())
        self._frame_cache: OrderedDict[Tuple[str, str, str, str], pd.DataFrame] = (
            OrderedDict()
        )
        self._regime_state: Dict[str, _RegimeState] = {}
        self._state_lock = RLock()

    def build(
        self,
        instrument_id: Any,
        candles: Any,
        decision_event_time: datetime,
        received_at: Optional[datetime] = None,
        *,
        source_as_of: Optional[datetime] = None,
    ) -> MarketContext:
        """Build as of a decision, preserving observation and request cutoffs.

        ``source_as_of`` is the historical query cutoff; a response arriving
        after a close cannot finalize a bar fetched before that close. Per-row
        ``received_at`` values retain first receipt of unchanged bar versions.
        Replay may provide an entire dataset: only rows available by the
        explicit decision are validated and consumed.
        """

        decision_at = _require_timestamp(decision_event_time, "decision_event_time")
        received = _require_timestamp(received_at or decision_at, "received_at")
        source_time = min(
            _require_timestamp(source_as_of or received, "source_as_of"), received
        )
        normalized = _normalize_candles(
            candles,
            self.policy,
            self.session_clock,
            received,
            decision_at,
            source_time,
        )
        raw_frame = normalized.frame
        complete_frame = raw_frame.loc[
            raw_frame["available_at"] <= pd.Timestamp(decision_at)
        ].copy()
        primary_bars = _frame_to_bars(complete_frame)
        primary_quality = _primary_quality(
            normalized,
            complete_frame,
            decision_at,
            self.policy,
        )
        higher_frame, higher_issues = _build_higher_timeframe(
            complete_frame,
            self.policy,
            self.session_clock,
        )
        available_higher = higher_frame.loc[
            higher_frame["available_at"] <= pd.Timestamp(decision_at)
        ].copy()
        higher_bars = _frame_to_bars(available_higher)
        higher_quality = _higher_quality(
            primary_quality,
            higher_frame,
            available_higher,
            higher_issues,
            decision_at,
            self.policy,
        )

        session_id = None
        if primary_bars:
            session_id = (
                primary_bars[-1].start.astimezone(EXCHANGE_TIMEZONE).date().isoformat()
            )
        primary_bar = primary_bars[-1] if primary_bars else None
        higher_bar = higher_bars[-1] if higher_bars else None
        input_hash = _input_hash(complete_frame)
        setup_range = _setup_range(primary_bars, self.policy.setup_range_bars)
        known_structure = _known_structure(
            primary_bars,
            self.policy.swing_confirmation_bars,
            decision_at,
        )
        session_vwap, atr, dynamics, participation = _continuous_observations(
            complete_frame
        )
        raw_regime, raw_features = _raw_regime(complete_frame)
        if not primary_quality.usable:
            session_vwap, atr, dynamics, participation = _continuous_observations(
                _empty_frame()
            )
            setup_range, known_structure = None, ()
            raw_regime, raw_features = "UNCERTAIN", {}
        transition = self._observe_regime(
            str(instrument_id),
            primary_bar,
            raw_regime,
            primary_quality.usable,
            decision_at,
        )
        snapshot_id = _snapshot_id(
            str(instrument_id),
            decision_at,
            input_hash,
            json.dumps(
                {
                    "policy": asdict(self.policy),
                    "session_policy": {
                        **asdict(self.session_clock.policy),
                        "holidays": sorted(
                            str(day) for day in self.session_clock.policy.holidays
                        ),
                    },
                    "received_at": received.isoformat(),
                    "source_as_of": source_time.isoformat(),
                    "quality": asdict(primary_quality),
                    "transition": asdict(transition),
                },
                default=str,
                sort_keys=True,
            ),
        )
        return MarketContext(
            snapshot_id=snapshot_id,
            instrument_id=str(instrument_id),
            session_id=session_id,
            decision_event_time=decision_at,
            received_at=received,
            source_as_of=source_time,
            feature_version=self.policy.policy_version,
            primary_bar=primary_bar,
            higher_bar=higher_bar,
            primary_bars=primary_bars,
            higher_bars=higher_bars,
            primary_quality=primary_quality,
            higher_quality=higher_quality,
            input_bar_ids=tuple(bar.bar_id for bar in primary_bars),
            input_hash=input_hash,
            session_vwap=session_vwap,
            atr=atr,
            direction_dynamics=MappingProxyType(dynamics),
            participation=MappingProxyType(participation),
            observation_quality=MappingProxyType(
                {
                    key: ContextQuality.VALID
                    if value is not None
                    else ContextQuality.UNAVAILABLE
                    for key, value in {
                        "session_vwap": session_vwap,
                        "atr": atr,
                        **participation,
                        **dynamics,
                    }.items()
                }
            ),
            known_structure=known_structure,
            setup_range=setup_range,
            raw_regime=raw_regime,
            raw_regime_features=MappingProxyType(raw_features),
            confirmed_regime=transition.confirmed_regime,
            transition_candidate=transition.transition_candidate,
            transition_age=transition.transition_age,
            context_policy=self.policy,
        )

    def cache_frame(
        self, instrument_id: Any, interval: str, candles: Any
    ) -> pd.DataFrame:
        with self._state_lock:
            return self._cache_frame(instrument_id, interval, candles)

    def _cache_frame(
        self, instrument_id: Any, interval: str, candles: Any
    ) -> pd.DataFrame:
        """Store/retrieve a deep copy keyed by content revision, never a live frame."""

        frame = _coerce_frame(candles)
        revision = _input_hash(frame)
        session = "unknown"
        if "date" in frame.columns and not frame.empty:
            parsed = _parse_timestamps(frame["date"])
            if parsed.notna().any():
                first = parsed.dropna().iloc[0]
                if first.tzinfo is None:
                    first = first.tz_localize(EXCHANGE_TIMEZONE)
                session = first.tz_convert(EXCHANGE_TIMEZONE).date().isoformat()
        last = "empty"
        if "date" in frame.columns and not frame.empty:
            last = str(frame["date"].iloc[-1])
        key = (str(instrument_id), interval, session, f"{last}:{revision}")
        if key not in self._frame_cache:
            self._frame_cache[key] = frame.copy(deep=True)
        self._frame_cache.move_to_end(key)
        while len(self._frame_cache) > 256:
            self._frame_cache.popitem(last=False)
        return self._frame_cache[key].copy(deep=True)

    def clear(self) -> None:
        with self._state_lock:
            self._frame_cache.clear()
            self._regime_state.clear()

    def _observe_regime(
        self,
        instrument_id: str,
        bar: Optional[OHLCVBar],
        raw_regime: str,
        usable: bool,
        decision_at: datetime,
    ) -> RegimeTransition:
        with self._state_lock:
            return self._observe_regime_locked(
                instrument_id, bar, raw_regime, usable, decision_at
            )

    def _observe_regime_locked(
        self,
        instrument_id: str,
        bar: Optional[OHLCVBar],
        raw_regime: str,
        usable: bool,
        decision_at: datetime,
    ) -> RegimeTransition:
        state = self._regime_state.setdefault(instrument_id, _RegimeState())
        if state.last_decision_at is not None and decision_at < state.last_decision_at:
            return RegimeTransition(raw_regime=raw_regime)
        state.last_decision_at = decision_at
        if bar is not None:
            session_id = bar.start.astimezone(EXCHANGE_TIMEZONE).date().isoformat()
            if state.last_bar_end is not None and bar.end < state.last_bar_end:
                # An as-of query must not inherit a later decision's state.
                return RegimeTransition(raw_regime=raw_regime)
            if session_id != state.session_id:
                state = _RegimeState(
                    session_id=session_id, last_decision_at=decision_at
                )
                self._regime_state[instrument_id] = state
        if not usable or bar is None:
            state.candidate, state.candidate_age = None, 0
        elif state.last_bar_end != bar.end:
            if state.last_bar_end is not None and bar.start != state.last_bar_end:
                state.candidate, state.candidate_age = None, 0
            state.last_bar_end = bar.end
            # Persistence is a run of adjacent, usable, qualifying closes.
            # Uncertain observations interrupt that run without changing the
            # last confirmed regime. Revisions are not additional closes.
            if raw_regime == "UNCERTAIN":
                state.candidate, state.candidate_age = None, 0
            elif state.confirmed_regime == "UNCERTAIN":
                state.confirmed_regime = raw_regime
            elif raw_regime == state.confirmed_regime:
                state.candidate, state.candidate_age = None, 0
            elif state.candidate == raw_regime:
                state.candidate_age += 1
            else:
                state.candidate, state.candidate_age = raw_regime, 1
            if state.candidate_age >= self.policy.regime_transition_confirm_bars:
                state.confirmed_regime = state.candidate or state.confirmed_regime
                state.candidate, state.candidate_age = None, 0
        return RegimeTransition(
            raw_regime=raw_regime,
            confirmed_regime=state.confirmed_regime,
            transition_candidate=state.candidate,
            transition_age=state.candidate_age,
        )


def build_market_context(
    instrument_id: Any,
    candles: Any,
    decision_event_time: datetime,
    received_at: Optional[datetime] = None,
    policy: Optional[ContextPolicy] = None,
    session_policy: Optional[SessionPolicy] = None,
    *,
    source_as_of: Optional[datetime] = None,
) -> MarketContext:
    """Convenience API for deterministic tests/replay without a shared service."""

    return MarketContextService(policy, session_policy).build(
        instrument_id,
        candles,
        decision_event_time,
        received_at,
        source_as_of=source_as_of,
    )


def _require_timestamp(value: datetime, name: str) -> datetime:
    converted = as_utc(value)
    if converted is None:
        raise ValueError(f"{name} is required")
    return converted


def _coerce_frame(candles: Any) -> pd.DataFrame:
    if isinstance(candles, pd.DataFrame):
        return candles.copy(deep=True)
    if candles is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(list(candles))
    except (TypeError, ValueError):
        return pd.DataFrame()


def _parse_timestamps(values: pd.Series) -> pd.Series:
    """Broker-naive means exchange local; accept mixed offsets without host time."""

    def parse(value: Any) -> Any:
        try:
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                return pd.NaT
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize(EXCHANGE_TIMEZONE)
            return stamp.tz_convert("UTC")
        except (TypeError, ValueError, OverflowError):
            return pd.NaT

    return pd.to_datetime(values.map(parse), utc=True).dt.tz_convert(EXCHANGE_TIMEZONE)


def _normalize_candles(
    candles: Any,
    policy: ContextPolicy,
    session_clock: SessionClock,
    received_at: datetime,
    decision_at: datetime,
    source_as_of: datetime,
) -> _NormalizedCandles:
    frame = _coerce_frame(candles)
    if frame.empty:
        return _NormalizedCandles(_empty_frame(), (), (), 0)
    required = ("date", "open", "high", "low", "close")
    missing_columns = tuple(
        column for column in required if column not in frame.columns
    )
    if missing_columns:
        return _NormalizedCandles(
            _empty_frame(),
            tuple(f"MISSING_COLUMN:{column}" for column in missing_columns),
            (),
            0,
        )
    frame = frame.loc[
        :,
        list(required)
        + [
            name
            for name in ("volume", "revision", "available_at", "received_at")
            if name in frame
        ],
    ].copy()
    frame["date"] = _parse_timestamps(frame["date"])
    interval = timedelta(minutes=policy.primary_interval_minutes)
    frame["end"] = frame["date"] + interval
    receipt = (
        _parse_timestamps(frame["received_at"])
        if "received_at" in frame
        else pd.Series(pd.Timestamp(received_at), index=frame.index)
    )
    available = frame["end"] + pd.Timedelta(seconds=policy.availability_delay_seconds)
    available = available.where(available >= receipt, receipt)
    if "available_at" in frame:
        explicit = _parse_timestamps(frame["available_at"])
        available = available.where(available >= explicit, explicit)
    frame["available_at"] = available
    # Filter time eligibility BEFORE validating values, duplicates or gaps.
    # Unobserved future rows cannot contaminate today's quality or identity.
    # A partially fetched bar never becomes final simply by aging in a cache.
    eligible = (
        (frame["available_at"] <= pd.Timestamp(decision_at))
        & (frame["end"] <= pd.Timestamp(source_as_of))
        & (frame["end"] <= receipt)
    )
    issues: List[str] = []
    if received_at <= decision_at:
        observed = receipt.isna() | (receipt <= pd.Timestamp(decision_at))
        if frame.loc[observed, "date"].isna().any():
            issues.append("INVALID_TIMESTAMP")
        if (
            frame.loc[
                observed & (frame["end"] <= pd.Timestamp(decision_at)), "available_at"
            ]
            .isna()
            .any()
        ):
            issues.append("INVALID_AVAILABILITY")
    pending_bars = bool((~eligible).any())
    frame = frame.loc[eligible].copy()
    if frame.empty:
        return _NormalizedCandles(_empty_frame(), tuple(issues), (), 0, pending_bars)
    if "volume" not in frame:
        frame["volume"] = float("nan")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "revision" not in frame:
        frame["revision"] = "source-v1"
    frame["revision"] = frame["revision"].fillna("source-v1").astype(str)
    if not frame["date"].is_monotonic_increasing:
        issues.append("OUT_OF_ORDER_NORMALIZED")
    frame = frame.sort_values("date", kind="stable").reset_index(drop=True)
    duplicate_dates = frame.duplicated("date", keep=False)
    conflicting_dates = []
    if duplicate_dates.any():
        for timestamp, group in frame.loc[duplicate_dates].groupby("date", sort=False):
            if group[list(required) + ["volume"]].nunique(dropna=False).max() > 1:
                issues.append("CONFLICTING_DUPLICATE")
                conflicting_dates.append(timestamp)
            else:
                issues.append("DUPLICATE_DEDUPLICATED")
        # Conflicts have no trustworthy value. Do not choose an arbitrary winner.
        frame = frame.loc[~frame["date"].isin(conflicting_dates)]
        frame = frame.sort_values(["date", "available_at", "revision"], kind="stable")
        frame = frame.drop_duplicates("date", keep="first").copy()
    finite_prices = pd.Series(True, index=frame.index)
    for column in ("open", "high", "low", "close"):
        finite_prices &= frame[column].map(_finite_positive)
    valid_volume = frame["volume"].map(_finite_nonnegative)
    if not valid_volume.all():
        issues.append("VOLUME_UNAVAILABLE")
        frame.loc[~valid_volume, "volume"] = float("nan")
    valid_geometry = (
        finite_prices
        & (frame["high"] >= frame[["open", "close"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close"]].min(axis=1))
    )
    if not valid_geometry.all():
        issues.append("INVALID_OHLCV")
        frame = frame.loc[valid_geometry].copy()
    if not frame.empty:
        in_session = frame["date"].map(
            lambda value: _is_session_bar(value.to_pydatetime(), session_clock)
        )
        if not in_session.all():
            issues.append("OUTSIDE_EXCHANGE_SESSION")
            frame = frame.loc[in_session].copy()
        aligned = pd.Series(
            [
                _aligned_session_bar(value.to_pydatetime(), interval, session_clock)
                for value in frame["date"]
            ],
            index=frame.index,
            dtype=bool,
        )
        if not aligned.all():
            issues.append("MISALIGNED_INTERVAL")
            frame = frame.loc[aligned].copy()
    frame = frame.reset_index(drop=True)
    if frame.empty:
        return _NormalizedCandles(_empty_frame(), tuple(dict.fromkeys(issues)), (), 0)
    missing_bars = _missing_session_bars(frame["date"], interval, session_clock)
    if missing_bars:
        issues.append("SESSION_GAP")
    # Broker revision labels are optional and often unchanged on corrections.
    # Content identifies the actual bar version, including corrected OHLCV.
    frame["revision"] = frame.apply(
        lambda row: (
            str(row["revision"])
            + ":"
            + _input_hash(
                pd.DataFrame([row]).drop(
                    columns=["available_at", "received_at"], errors="ignore"
                )
            )[:16]
        ),
        axis=1,
    )
    frame["bar_id"] = frame.apply(
        lambda row: _bar_id(
            row["date"], row["revision"], policy.primary_interval_minutes
        ),
        axis=1,
    )
    return _NormalizedCandles(
        frame,
        tuple(dict.fromkeys(issues)),
        missing_bars,
        len(frame) + len(missing_bars),
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "end",
            "available_at",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "revision",
            "bar_id",
        ]
    )


def _finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _finite_nonnegative(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _is_session_bar(value: datetime, session_clock: SessionClock) -> bool:
    snapshot = session_clock.snapshot(value)
    return snapshot.is_trading_day and snapshot.is_open


def _aligned_session_bar(
    value: datetime, interval: timedelta, session_clock: SessionClock
) -> bool:
    local = value.astimezone(EXCHANGE_TIMEZONE)
    anchor = datetime.combine(
        local.date(), session_clock.policy.open_time, EXCHANGE_TIMEZONE
    )
    close = datetime.combine(
        local.date(), session_clock.policy.close_time, EXCHANGE_TIMEZONE
    )
    return (local - anchor) % interval == timedelta(0) and local + interval <= close


def _missing_session_bars(
    starts: pd.Series, interval: timedelta, session_clock: SessionClock
) -> Tuple[datetime, ...]:
    missing: List[datetime] = []
    if starts.empty:
        return ()
    ordered = list(starts)
    latest_session = ordered[-1].date()
    current_session = [value for value in ordered if value.date() == latest_session]
    expected = datetime.combine(
        latest_session, session_clock.policy.open_time, EXCHANGE_TIMEZONE
    )
    while expected < current_session[0]:
        missing.append(expected)
        expected += interval
    for previous, current in zip(ordered, ordered[1:]):
        previous_local = previous.to_pydatetime().astimezone(EXCHANGE_TIMEZONE)
        current_local = current.to_pydatetime().astimezone(EXCHANGE_TIMEZONE)
        if previous_local.date() != current_local.date():
            continue
        expected = previous_local + interval
        while expected < current_local:
            missing.append(expected.astimezone(EXCHANGE_TIMEZONE))
            expected += interval
    return tuple(missing)


def _frame_to_bars(frame: pd.DataFrame) -> Tuple[OHLCVBar, ...]:
    bars: List[OHLCVBar] = []
    for row in frame.itertuples(index=False):
        start = _as_datetime(getattr(row, "date"))
        bars.append(
            OHLCVBar(
                bar_id=str(getattr(row, "bar_id")),
                start=start,
                end=_as_datetime(getattr(row, "end")),
                available_at=_as_datetime(getattr(row, "available_at")),
                open=float(getattr(row, "open")),
                high=float(getattr(row, "high")),
                low=float(getattr(row, "low")),
                close=float(getattr(row, "close")),
                volume=_optional_float(getattr(row, "volume")),
                revision=str(getattr(row, "revision")),
            )
        )
    return tuple(bars)


def _bars_to_frame(bars: Sequence[OHLCVBar]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": bar.start,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "revision": bar.revision,
                "bar_id": bar.bar_id,
                "end": bar.end,
                "available_at": bar.available_at,
            }
            for bar in bars
        ]
    )


def _as_datetime(value: Any) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(EXCHANGE_TIMEZONE)
    return as_utc(timestamp.to_pydatetime())


def _primary_quality(
    normalized: _NormalizedCandles,
    complete_frame: pd.DataFrame,
    decision_at: datetime,
    policy: ContextPolicy,
) -> SourceQuality:
    issues = normalized.issues
    if complete_frame.empty:
        status = (
            ContextQuality.INVALID
            if issues
            else ContextQuality.INCOMPLETE
            if normalized.pending_bars
            else ContextQuality.UNAVAILABLE
        )
        return SourceQuality(
            status=status,
            issues=issues,
            expected_bars=normalized.expected_bars,
            missing_bars=normalized.missing_bars,
        )
    latest = _as_datetime(complete_frame["end"].iloc[-1])
    latest_available = _as_datetime(complete_frame["available_at"].iloc[-1])
    age = max(0.0, (decision_at - latest).total_seconds())
    if any(
        issue
        in {
            "INVALID_TIMESTAMP",
            "INVALID_OHLCV",
            "CONFLICTING_DUPLICATE",
            "OUTSIDE_EXCHANGE_SESSION",
            "MISALIGNED_INTERVAL",
            "INVALID_AVAILABILITY",
        }
        for issue in issues
    ):
        status = ContextQuality.INVALID
    elif normalized.missing_bars:
        status = ContextQuality.GAP
    elif (
        latest.astimezone(EXCHANGE_TIMEZONE).date()
        != decision_at.astimezone(EXCHANGE_TIMEZONE).date()
    ):
        status = ContextQuality.STALE
    elif policy.max_primary_age_seconds and age > policy.max_primary_age_seconds:
        status = ContextQuality.STALE
    else:
        status = ContextQuality.VALID
    return SourceQuality(
        status=status,
        issues=issues,
        expected_bars=normalized.expected_bars,
        missing_bars=normalized.missing_bars,
        latest_bar_end=latest,
        latest_available_at=latest_available,
        age_seconds=age,
    )


def _build_higher_timeframe(
    primary: pd.DataFrame, policy: ContextPolicy, session_clock: SessionClock
) -> Tuple[pd.DataFrame, Tuple[str, ...]]:
    if primary.empty:
        return _empty_frame(), ()
    bars_per_higher = policy.higher_interval_minutes // policy.primary_interval_minutes
    rows: List[Dict[str, Any]] = []
    issues: List[str] = []
    frame = primary.copy()
    local_dates = frame["date"].dt.tz_convert(EXCHANGE_TIMEZONE)
    session_dates = local_dates.dt.date
    anchor_minutes = (
        session_clock.policy.open_time.hour * 60 + session_clock.policy.open_time.minute
    )
    minutes_from_open = (
        local_dates.dt.hour * 60 + local_dates.dt.minute - anchor_minutes
    )
    bucket = minutes_from_open // policy.higher_interval_minutes
    frame["_session"] = session_dates.astype(str)
    frame["_bucket"] = bucket
    interval = timedelta(minutes=policy.primary_interval_minutes)
    higher_interval = timedelta(minutes=policy.higher_interval_minutes)
    for _, group in frame.groupby(["_session", "_bucket"], sort=True):
        group = group.sort_values("date", kind="stable")
        # Pandas's wall-clock floor is not session aligned for a 15m interval
        # in every exchange/timezone combination.  Compute from the 09:15 anchor.
        local_start = group["date"].iloc[0].tz_convert(EXCHANGE_TIMEZONE)
        offset_minutes = (
            (local_start.hour * 60 + local_start.minute) - anchor_minutes
        ) % policy.higher_interval_minutes
        expected_start = local_start - pd.Timedelta(minutes=offset_minutes)
        expected_times = [expected_start + i * interval for i in range(bars_per_higher)]
        actual_times = list(group["date"])
        if len(group) != bars_per_higher or actual_times != expected_times:
            issues.append("INCOMPLETE_HIGHER_TIMEFRAME")
            continue
        end = expected_start + higher_interval
        revision = hashlib.sha256(
            "|".join(group["bar_id"].astype(str)).encode()
        ).hexdigest()[:12]
        rows.append(
            {
                "date": expected_start,
                "end": end,
                "available_at": group["available_at"].max(),
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": _optional_float(group["volume"].sum(min_count=len(group))),
                "revision": revision,
                "bar_id": _bar_id(
                    expected_start, revision, policy.higher_interval_minutes
                ),
            }
        )
    if not rows:
        return _empty_frame(), tuple(dict.fromkeys(issues))
    return pd.DataFrame(rows).sort_values("date", kind="stable").reset_index(
        drop=True
    ), tuple(dict.fromkeys(issues))


def _higher_quality(
    primary_quality: SourceQuality,
    all_higher: pd.DataFrame,
    available_higher: pd.DataFrame,
    higher_issues: Tuple[str, ...],
    decision_at: datetime,
    policy: ContextPolicy,
) -> SourceQuality:
    if not primary_quality.usable:
        return SourceQuality(
            status=primary_quality.status,
            issues=primary_quality.issues,
            expected_bars=primary_quality.expected_bars,
            missing_bars=primary_quality.missing_bars,
        )
    if available_higher.empty:
        status = (
            ContextQuality.INCOMPLETE
            if not all_higher.empty or higher_issues
            else ContextQuality.UNAVAILABLE
        )
        return SourceQuality(status=status, issues=higher_issues)
    latest = _as_datetime(available_higher["end"].iloc[-1])
    latest_available = _as_datetime(available_higher["available_at"].iloc[-1])
    age = max(0.0, (decision_at - latest).total_seconds())
    if (
        latest.astimezone(EXCHANGE_TIMEZONE).date()
        != decision_at.astimezone(EXCHANGE_TIMEZONE).date()
    ):
        status = ContextQuality.STALE
    elif policy.max_higher_age_seconds and age > policy.max_higher_age_seconds:
        status = ContextQuality.STALE
    else:
        status = ContextQuality.VALID
    return SourceQuality(
        status=status,
        issues=higher_issues,
        latest_bar_end=latest,
        latest_available_at=latest_available,
        age_seconds=age,
    )


def _setup_range(bars: Sequence[OHLCVBar], lookback: int) -> Optional[SetupRange]:
    # The just-completed decision bar is deliberately excluded.  A breakout bar
    # must not move the range it is being compared against.
    if len(bars) <= lookback:
        return None
    window = tuple(bars[-(lookback + 1) : -1])
    if not window or not _contiguous_same_session(tuple(window) + (bars[-1],)):
        return None
    return SetupRange(
        low=min(bar.low for bar in window),
        high=max(bar.high for bar in window),
        start=window[0].start,
        end=window[-1].end,
        known_at=max(bar.available_at for bar in window),
        source_bar_ids=tuple(bar.bar_id for bar in window),
    )


def _known_structure(
    bars: Sequence[OHLCVBar], width: int, decision_at: datetime
) -> Tuple[KnownLevel, ...]:
    levels: List[KnownLevel] = []
    if len(bars) < (2 * width + 1):
        return ()
    for index in range(width, len(bars) - width):
        window = bars[index - width : index + width + 1]
        # Never form a pivot across a session seam or a time gap.
        if not _contiguous_same_session(window):
            continue
        candidate = bars[index]
        known_at = max(bar.available_at for bar in window)
        if known_at > decision_at:
            continue
        highs = [bar.high for bar in window]
        lows = [bar.low for bar in window]
        source_ids = tuple(bar.bar_id for bar in window)
        confirmation_revision = hashlib.sha256(
            "|".join(source_ids).encode()
        ).hexdigest()[:16]
        if candidate.high == max(highs) and highs.count(candidate.high) == 1:
            levels.append(
                KnownLevel(
                    level_id=f"swing_high:{candidate.bar_id}:{confirmation_revision}",
                    kind="SWING_HIGH",
                    price=candidate.high,
                    formed_at=candidate.end,
                    known_at=known_at,
                    source_bar_ids=source_ids,
                )
            )
        if candidate.low == min(lows) and lows.count(candidate.low) == 1:
            levels.append(
                KnownLevel(
                    level_id=f"swing_low:{candidate.bar_id}:{confirmation_revision}",
                    kind="SWING_LOW",
                    price=candidate.low,
                    formed_at=candidate.end,
                    known_at=known_at,
                    source_bar_ids=source_ids,
                )
            )
    return tuple(levels)


def _contiguous_same_session(bars: Sequence[OHLCVBar]) -> bool:
    if not bars:
        return False
    session = bars[0].start.astimezone(EXCHANGE_TIMEZONE).date()
    expected = bars[0].start
    for bar in bars:
        if bar.start.astimezone(EXCHANGE_TIMEZONE).date() != session:
            return False
        if bar.start != expected:
            return False
        expected = bar.end
    return True


def _continuous_observations(
    frame: pd.DataFrame,
) -> Tuple[
    Optional[float],
    Optional[float],
    Dict[str, Optional[float]],
    Dict[str, Optional[float]],
]:
    dynamics: Dict[str, Optional[float]] = {
        "ema_20": None,
        "ema_50": None,
        "ema_20_slope": None,
        "close_to_close": None,
    }
    participation: Dict[str, Optional[float]] = {
        "volume": None,
        "relative_volume_20": None,
    }
    if frame.empty:
        return None, None, dynamics, participation
    vwap = SessionVWAP(frame).vwap()
    latest_vwap = _optional_float(vwap.iloc[-1]) if len(vwap) else None
    session_dates = frame["date"].dt.tz_convert(EXCHANGE_TIMEZONE).dt.date
    if frame.loc[session_dates == session_dates.iloc[-1], "volume"].isna().any():
        latest_vwap = None
    atr = None
    if len(frame) >= 14:
        value = (
            AverageTrueRange(
                high=frame["high"], low=frame["low"], close=frame["close"], window=14
            )
            .average_true_range()
            .iloc[-1]
        )
        atr = _optional_float(value)
    if len(frame) >= 20:
        ema_20 = EMAIndicator(close=frame["close"], window=20).ema_indicator()
        dynamics["ema_20"] = _optional_float(ema_20.iloc[-1])
        if len(ema_20) >= 2:
            dynamics["ema_20_slope"] = _optional_float(
                ema_20.iloc[-1] - ema_20.iloc[-2]
            )
    if len(frame) >= 50:
        dynamics["ema_50"] = _optional_float(
            EMAIndicator(close=frame["close"], window=50).ema_indicator().iloc[-1]
        )
    if len(frame) >= 2:
        dynamics["close_to_close"] = _optional_float(
            frame["close"].iloc[-1] - frame["close"].iloc[-2]
        )
    participation["volume"] = _optional_float(frame["volume"].iloc[-1])
    if len(frame) >= 21:
        baseline = frame["volume"].iloc[-21:-1]
        if len(baseline) == 20 and baseline.notna().all() and baseline.mean() > 0:
            participation["relative_volume_20"] = _optional_float(
                frame["volume"].iloc[-1] / baseline.mean()
            )
    return latest_vwap, atr, dynamics, participation


def _raw_regime(frame: pd.DataFrame) -> Tuple[str, Dict[str, Any]]:
    # Import locally to keep this data module usable without a global singleton
    # during low-level validation tests.
    from .regime_classifier import regime_classifier

    result = regime_classifier.classify(frame.copy(deep=True))
    return str(result.get("regime", "UNCERTAIN")), dict(result.get("features", {}))


def _optional_float(value: Any) -> Optional[float]:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _bar_id(start: Any, revision: Any, interval_minutes: int = 5) -> str:
    timestamp = _as_datetime(start).isoformat()
    return f"{interval_minutes}m:{timestamp}:{revision}"


def _input_hash(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return hashlib.sha256(b"empty").hexdigest()
    columns = [
        column
        for column in [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "revision",
            "available_at",
            "received_at",
        ]
        if column in frame
    ]
    canonical = frame.loc[:, columns].copy().reset_index(drop=True)
    for column in ("date", "available_at", "received_at"):
        if column in canonical:
            canonical[column] = canonical[column].map(str)
    encoded = canonical.to_json(orient="split", date_format="iso", double_precision=15)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _snapshot_id(
    instrument_id: str, decision_at: datetime, input_hash: str, version: str
) -> str:
    material = f"{instrument_id}|{decision_at.isoformat()}|{input_hash}|{version}"
    return hashlib.sha256(material.encode()).hexdigest()
