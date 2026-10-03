"""Independent frozen entry-boundary diagnostic, used only after execution.

This mechanical reference is not an assertion that every boundary breach is a
trader's true thesis invalidation. It supplies a reproducible noninferiority
diagnostic against a predeclared structural reference; it never affects orders.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from itertools import groupby
from math import isfinite
from typing import Mapping

from .promotion import promotion_artifact_hash


@dataclass(frozen=True)
class EntryBoundaryReferencePolicy:
    policy_version: str = "static-entry-boundary-reference-v2"
    confirmation_bars: int = 2
    atr_buffer: float = 0.1
    buffer_units: str = "FROZEN_ENTRY_ATR_MULTIPLE"

    def __post_init__(self):
        if self.policy_version not in {
            "static-entry-boundary-reference-v1",
            "static-entry-boundary-reference-v2",
        }:
            raise ValueError("unsupported structural reference policy")
        if self.buffer_units != "FROZEN_ENTRY_ATR_MULTIPLE":
            raise ValueError("reference buffer must use frozen entry ATR units")
        if (
            isinstance(self.confirmation_bars, bool)
            or not isinstance(self.confirmation_bars, int)
            or self.confirmation_bars < 1
        ):
            raise ValueError("confirmation_bars must be a positive integer")
        if (
            isinstance(self.atr_buffer, bool)
            or not isinstance(self.atr_buffer, (int, float))
            or not isfinite(self.atr_buffer)
            or self.atr_buffer < 0
        ):
            raise ValueError("atr_buffer must be finite and nonnegative")


def _at(value):
    at = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError("reference events require explicit aware times")
    return at


def _positive(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(value)
        and value > 0
    )


def derive_entry_boundary_references(
    report: Mapping, policy: EntryBoundaryReferencePolicy | Mapping
) -> list[dict]:
    """Label retained price paths using entry-time anchors and frozen rules.

    Breakouts use the broken range edge; other range premises use the adverse
    edge. Version 2 also supports the directional swing frozen as the entry
    boundary. Later swings never replace that anchor. Consecutive closes confirm
    the diagnostic; gaps censor absence claims. Buffer units are frozen entry ATR.
    """
    if isinstance(policy, Mapping):
        policy = EntryBoundaryReferencePolicy(**dict(policy))
    if not isinstance(policy, EntryBoundaryReferencePolicy):
        raise TypeError("an explicit frozen reference policy is required")
    inputs = report["artifacts"]["inputs"]["payload"]
    data = report["artifacts"]["data"]["payload"]
    case = inputs["case"]
    checkpoint, cutoff = _at(case["checkpoint_at"]), _at(inputs["test_end"])
    if checkpoint >= cutoff:
        raise ValueError("reference checkpoint must precede its scoring cutoff")
    context = (
        report["artifacts"].get("policies", {}).get("payload", {}).get("context", {})
    )
    minutes = context.get("primary_interval_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise ValueError("reference requires its frozen primary bar interval")
    interval = timedelta(minutes=minutes)
    references = []
    for position in case["positions"]:
        thesis = position["thesis"]
        anchors = thesis["causal_anchors"]
        boundary = thesis["management_profile"].get("values", {}).get(
            "entry_boundary"
        ) or anchors.get("setup_range")
        volatility = anchors.get("volatility") or {}
        atr = volatility.get("atr")
        if (
            not isinstance(boundary, Mapping)
            or not _positive(atr)
            or volatility.get("quality") != "VALID"
        ):
            continue
        if thesis["direction"] not in {"BUY", "SELL"}:
            raise ValueError("reference direction must be BUY or SELL")
        long = thesis["direction"] == "BUY"
        swing = boundary.get("kind") in {"SWING_LOW", "SWING_HIGH"}
        if swing:
            if policy.policy_version == "static-entry-boundary-reference-v1":
                continue
            sources = boundary.get("source_bar_ids")
            if (
                not _positive(boundary.get("price"))
                or not isinstance(boundary.get("level_id"), str)
                or not boundary["level_id"]
                or not isinstance(sources, (list, tuple))
                or not sources
                or any(not isinstance(source, str) or not source for source in sources)
            ):
                continue
            if boundary["kind"] != ("SWING_LOW" if long else "SWING_HIGH"):
                raise ValueError(
                    "reference swing must be the directional entry premise"
                )
            if _at(boundary["formed_at"]) > _at(boundary["known_at"]):
                raise ValueError("reference swing cannot be known before formation")
            edge = boundary["price"]
        else:
            if not all(_positive(boundary.get(key)) for key in ("low", "high")):
                continue
            if boundary["low"] >= boundary["high"]:
                raise ValueError("reference range must have ordered price boundaries")
            breakout = thesis["playbook"] == "Breakout"
            edge = boundary["high"] if long == breakout else boundary["low"]
        created_at = _at(thesis["created_at"])
        if (
            _at(boundary["known_at"]) > created_at
            or created_at > checkpoint
            or (
                volatility.get("known_at") is not None
                and _at(volatility["known_at"]) > created_at
            )
        ):
            raise ValueError(
                "reference boundary and ATR must be known at thesis creation"
            )
        threshold = (
            edge - policy.atr_buffer * atr if long else edge + policy.atr_buffer * atr
        )
        invalidated, previous, through, continuous = None, None, None, True
        bars = sorted(data.get(thesis["symbol"], []), key=lambda row: _at(row["date"]))
        events = []
        for bar in bars:
            start = _at(bar["date"])
            available = max(
                [
                    start + interval,
                    *[
                        _at(bar[key])
                        for key in ("available_at", "received_at")
                        if bar.get(key) is not None
                    ],
                ],
            )
            if start < checkpoint or available >= cutoff:
                continue
            if previous is None:
                continuous &= start - checkpoint < interval
            elif start == previous:
                raise ValueError("duplicate reference bar requires a resolved revision")
            elif start != previous + interval:
                continuous = False
            previous = start
            if not _positive(bar["close"]):
                raise ValueError("invalid reference price")
            events.append((available, start, bar["close"]))
            # Receipt of a stale bar does not extend observed price coverage.
            through = start + interval
        if through is None or not continuous:
            continue
        known = {}
        # Late older bars cannot invalidate a subsequently reclaimed structure.
        # Batch simultaneous arrivals so file order cannot choose the result.
        for available, arrivals in groupby(sorted(events), key=lambda item: item[0]):
            for _, start, close in arrivals:
                known[start] = close
            latest = max(known)
            confirmed = True
            for offset in range(policy.confirmation_bars):
                close = known.get(latest - offset * interval)
                if close is None or not (
                    close < threshold if long else close > threshold
                ):
                    confirmed = False
                    break
            if invalidated is None and confirmed:
                invalidated = available
        if invalidated is not None and invalidated > through:
            # No observed prices cover exposure after this delayed arrival.
            continue
        reference = {
            "case_id": case["case_id"],
            "symbol": thesis["symbol"],
            "reference_policy_version": policy.policy_version,
            "reference_policy": asdict(policy),
            "reference_boundary": dict(boundary),
            "frozen_entry_atr": atr,
            "buffer_price_distance": policy.atr_buffer * atr,
            "threshold_price": threshold,
            "primary_interval_minutes": minutes,
            "observed_through": through.isoformat(),
            "invalidated_at": invalidated.isoformat() if invalidated else None,
            "assessed_no_invalidation": invalidated is None,
            "interpretation": "PREDECLARED_STATIC_BOUNDARY_DIAGNOSTIC_NOT_ADJUDICATED_GROUND_TRUTH",
        }
        reference["reference_event_id"] = promotion_artifact_hash(reference)
        references.append(reference)
    return references
