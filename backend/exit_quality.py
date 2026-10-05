"""Deterministic, unit-explicit exit-quality measurements.

This module is deliberately independent of the live journal, broker, and wall
clock.  It measures recorded facts and simulates only clearly labelled,
risk-constrained counterfactuals.  In particular, it never treats a later high
as available after a retained stop or a forced-flat deadline has ended a path.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Mapping, Sequence

from .time_utils import EXCHANGE_TIMEZONE, as_utc


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _positive(value: Any) -> float | None:
    value = _finite(value)
    return value if value is not None and value > 0 else None


def _timestamp(value: Any) -> datetime | None:
    try:
        return as_utc(value)
    except (TypeError, ValueError):
        return None


def _direction(value: Any) -> int | None:
    normalized = str(value).upper()
    if normalized in {"BUY", "LONG", "+1", "1"}:
        return 1
    if normalized in {"SELL", "SHORT", "-1"}:
        return -1
    return None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if _finite(value) is not None else None


def _counterfactual_timestamp(value: Any) -> datetime | None:
    """Research clocks require an offset, unlike explicitly legacy journal time."""

    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            return None
        return _timestamp(parsed)
    except (TypeError, ValueError):
        return None


def _unavailable_quality(
    quality: str, reason_code: str | None, execution_outcome_code: str | None
) -> dict[str, Any]:
    available = dict.fromkeys(
        (
            "initial_risk",
            "captured_gross_r",
            "captured_net_r",
            "mfe_mae",
            "exposure_aware_peak",
            "holding_time",
            "decision_to_intent_latency",
            "intent_to_fill_latency",
            "invalidation_to_intent_latency",
        ),
        False,
    )
    return {
        "eligible": False,
        "exclusion_reason": "INVALID_INITIAL_RISK_INPUT",
        "quality": quality,
        "reason_code": reason_code,
        "execution_outcome_code": execution_outcome_code,
        "coverage": {
            "available": available,
            "available_count": 0,
            "total_count": len(available),
            "extrema_quality": "UNAVAILABLE",
            "exposure_quality": "UNAVAILABLE",
            "exposure_valid_points": 0,
            "exposure_invalid_points": 0,
        },
        "metrics": dict.fromkeys(
            (
                "risk_per_share_price",
                "initial_risk_currency",
                "mfe_price",
                "mae_price",
                "mfe_r",
                "mae_r",
                "mfe_currency_initial_quantity_proxy",
                "mae_currency_initial_quantity_proxy",
                "captured_gross",
                "captured_net",
                "captured_gross_r",
                "captured_net_r",
                "mfe_capture_pct",
                "r_given_back",
                "exposure_peak_gross",
                "exposure_peak_r",
                "exposure_mae_currency",
                "exposure_mae_r",
                "exposure_aware_r_given_back",
                "holding_time_seconds",
                "holding_completed_bars",
                "decision_to_intent_seconds",
                "invalidation_to_intent_seconds",
                "intent_to_fill_seconds",
            )
        ),
    }


def _price_path_extrema(
    direction: int,
    entry_price: float,
    observations: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None, str]:
    """Return favorable/adverse price excursions and their data quality."""

    favorable: float | None = None
    adverse: float | None = None
    valid = 0
    invalid = 0
    for observation in observations:
        high = _positive(observation.get("high"))
        low = _positive(observation.get("low"))
        if high is None or low is None or low > high:
            invalid += 1
            continue
        valid += 1
        if direction > 0:
            favorable = max(
                favorable if favorable is not None else 0.0, high - entry_price
            )
            adverse = max(adverse if adverse is not None else 0.0, entry_price - low)
        else:
            favorable = max(
                favorable if favorable is not None else 0.0, entry_price - low
            )
            adverse = max(adverse if adverse is not None else 0.0, high - entry_price)
    if not valid:
        return None, None, "UNAVAILABLE"
    return (
        max(0.0, favorable or 0.0),
        max(0.0, adverse or 0.0),
        "PARTIAL_PRICE_PATH" if invalid else "OBSERVED_PRICE_PATH",
    )


def calculate_exit_quality(
    *,
    direction: str,
    entry_price: float,
    initial_stop: float,
    initial_quantity: int,
    realized_gross: float | None = None,
    realized_net: float | None = None,
    price_path: Sequence[Mapping[str, Any]] = (),
    exposure_path: Sequence[Mapping[str, Any]] = (),
    observed_mfe_r: float | None = None,
    observed_mae_r: float | None = None,
    entry_at: Any = None,
    decision_at: Any = None,
    intent_at: Any = None,
    invalidation_at: Any = None,
    fill_at: Any = None,
    exit_at: Any = None,
    completed_bars: int | None = None,
    reason_code: str | None = None,
    execution_outcome_code: str | None = None,
    quality: str = "RECONCILED",
) -> dict[str, Any]:
    """Measure one trade using immutable initial risk and recorded observations.

    ``price_path`` must contain only intervals observed while the position was
    open.  ``exposure_path`` is optional and supplies exposure-aware marked P&L
    for partial exits.  Without it, the ordinary price-path giveback remains
    available but exposure-aware giveback is explicitly unavailable.
    """

    sign = _direction(direction)
    entry = _positive(entry_price)
    stop = _positive(initial_stop)
    quantity = (
        initial_quantity
        if isinstance(initial_quantity, int) and not isinstance(initial_quantity, bool)
        else 0
    )
    if sign is None or entry is None or stop is None or quantity <= 0:
        return _unavailable_quality(quality, reason_code, execution_outcome_code)
    risk_per_share = abs(entry - stop)
    initial_risk_currency = (
        _finite(risk_per_share * quantity) if _finite(quantity) is not None else None
    )
    if (
        risk_per_share <= 0
        or sign * (entry - stop) <= 0
        or initial_risk_currency is None
        or initial_risk_currency <= 0
    ):
        return _unavailable_quality(quality, reason_code, execution_outcome_code)
    gross = _finite(realized_gross)
    net = _finite(realized_net)
    mfe_price, mae_price, extrema_quality = _price_path_extrema(sign, entry, price_path)

    # Phase-7 state stores R magnitudes even where the raw observed bar is no
    # longer retained.  Use them only when a richer path is unavailable.
    checkpoint_mfe = _finite(observed_mfe_r)
    checkpoint_mae = _finite(observed_mae_r)
    if mfe_price is None and checkpoint_mfe is not None and checkpoint_mfe >= 0:
        mfe_price = checkpoint_mfe * risk_per_share
        extrema_quality = "CHECKPOINT_R_ONLY"
    if mae_price is None and checkpoint_mae is not None and checkpoint_mae >= 0:
        mae_price = checkpoint_mae * risk_per_share
        extrema_quality = "CHECKPOINT_R_ONLY"

    mfe_r = mfe_price / risk_per_share if mfe_price is not None else None
    mae_r = mae_price / risk_per_share if mae_price is not None else None
    gross_r = gross / initial_risk_currency if gross is not None else None
    net_r = net / initial_risk_currency if net is not None else None
    capture_pct = (
        100.0 * gross_r / mfe_r
        if gross_r is not None and mfe_r is not None and mfe_r > 0
        else None
    )
    given_back_r = (
        mfe_r - gross_r if mfe_r is not None and gross_r is not None else None
    )

    peak_exposure_gross: float | None = None
    trough_exposure_gross: float | None = None
    valid_exposure_points = 0
    invalid_exposure_points = 0
    for point in exposure_path:
        realized = _finite(point.get("realized_gross"))
        residual = _finite(point.get("residual_quantity"))
        mark = _positive(point.get("mark_price"))
        if (
            realized is None
            or residual is None
            or mark is None
            or not residual.is_integer()
            or not 0 <= residual <= quantity
        ):
            invalid_exposure_points += 1
            continue
        marked = realized + sign * residual * (mark - entry)
        if not math.isfinite(marked):
            invalid_exposure_points += 1
            continue
        # The trade starts at zero gross P&L. Never discard a zero peak because
        # it is falsey, and include the known final realized endpoint.
        peak_exposure_gross = max(
            peak_exposure_gross if peak_exposure_gross is not None else 0.0,
            marked,
            gross if gross is not None else 0.0,
        )
        trough_exposure_gross = min(
            trough_exposure_gross if trough_exposure_gross is not None else 0.0,
            marked,
            gross if gross is not None else 0.0,
        )
        valid_exposure_points += 1
    exposure_peak_r = (
        peak_exposure_gross / initial_risk_currency
        if peak_exposure_gross is not None
        else None
    )
    exposure_given_back_r = (
        exposure_peak_r - gross_r
        if exposure_peak_r is not None and gross_r is not None
        else None
    )

    def duration(start: Any, end: Any) -> float | None:
        start_at, end_at = _timestamp(start), _timestamp(end)
        if start_at is None or end_at is None or end_at < start_at:
            return None
        return (end_at - start_at).total_seconds()

    available = {
        "initial_risk": True,
        "captured_gross_r": gross_r is not None,
        "captured_net_r": net_r is not None,
        "mfe_mae": mfe_r is not None and mae_r is not None,
        "exposure_aware_peak": valid_exposure_points > 0,
        "holding_time": duration(entry_at, exit_at) is not None,
        "decision_to_intent_latency": duration(decision_at, intent_at) is not None,
        "intent_to_fill_latency": duration(intent_at, fill_at) is not None,
        "invalidation_to_intent_latency": duration(invalidation_at, intent_at)
        is not None,
    }
    return {
        "eligible": gross is not None and net is not None and quality == "RECONCILED",
        "exclusion_reason": (
            None
            if gross is not None and net is not None and quality == "RECONCILED"
            else "UNRESOLVED_OR_NON_RECONCILED_EXECUTION"
        ),
        "quality": quality,
        "reason_code": reason_code,
        "execution_outcome_code": execution_outcome_code,
        "coverage": {
            "available": available,
            "available_count": sum(available.values()),
            "total_count": len(available),
            "extrema_quality": extrema_quality,
            "exposure_quality": (
                "PARTIAL_EXPOSURE_PATH"
                if valid_exposure_points and invalid_exposure_points
                else "OBSERVED_EXPOSURE_PATH"
                if valid_exposure_points
                else "UNAVAILABLE"
            ),
            "exposure_valid_points": valid_exposure_points,
            "exposure_invalid_points": invalid_exposure_points,
        },
        "metrics": {
            "risk_per_share_price": _round(risk_per_share),
            "initial_risk_currency": _round(initial_risk_currency),
            "mfe_price": _round(mfe_price),
            "mae_price": _round(mae_price),
            "mfe_r": _round(mfe_r),
            "mae_r": _round(mae_r),
            "mfe_currency_initial_quantity_proxy": _round(
                mfe_price * quantity if mfe_price is not None else None
            ),
            "mae_currency_initial_quantity_proxy": _round(
                mae_price * quantity if mae_price is not None else None
            ),
            "captured_gross": _round(gross),
            "captured_net": _round(net),
            "captured_gross_r": _round(gross_r),
            "captured_net_r": _round(net_r),
            "mfe_capture_pct": _round(capture_pct),
            "r_given_back": _round(given_back_r),
            "exposure_peak_gross": _round(peak_exposure_gross),
            "exposure_peak_r": _round(exposure_peak_r),
            "exposure_mae_currency": _round(
                -trough_exposure_gross if trough_exposure_gross is not None else None
            ),
            "exposure_mae_r": _round(
                -trough_exposure_gross / initial_risk_currency
                if trough_exposure_gross is not None
                else None
            ),
            "exposure_aware_r_given_back": _round(exposure_given_back_r),
            "holding_time_seconds": _round(duration(entry_at, exit_at)),
            "holding_completed_bars": (
                completed_bars
                if isinstance(completed_bars, int)
                and not isinstance(completed_bars, bool)
                and completed_bars >= 0
                else None
            ),
            "decision_to_intent_seconds": _round(duration(decision_at, intent_at)),
            "invalidation_to_intent_seconds": _round(
                duration(invalidation_at, intent_at)
            ),
            "intent_to_fill_seconds": _round(duration(intent_at, fill_at)),
        },
    }


def simulate_risk_constrained_hold_n(
    *,
    direction: str,
    entry_price: float,
    initial_stop: float,
    confirmed_stop: float,
    quantity: int,
    path: Sequence[Mapping[str, Any]],
    horizon_bars: int,
    entry_fees: float | None = None,
    exit_fees: float | None = None,
    forced_deadline_at: Any = None,
    account_scenario: str = "PRICE_PATH_ONLY",
    bar_duration_seconds: float | None = None,
    start_at: Any = None,
    actual_exit_price: float | None = None,
    initial_quantity: int | None = None,
    realized_gross_before_hold: float = 0.0,
    prior_exit_fees: float = 0.0,
    cost_model_version: str | None = None,
    dataset_version: str | None = None,
) -> dict[str, Any]:
    """Hold the residual with the recorded stop under an explicit OHLC proxy.

    Timestamps denote *interval ends*. Supply ``interval_start``/``interval_end``
    or ``bar_duration_seconds`` to establish continuous exposure. A deadline
    inside a coarse candle is censored: its later close/extremes were not an
    executable deadline price. Gap stops use the observed open. This market-stop
    proxy does not assert stop-limit liquidity or account-level daily-loss
    feasibility; those need the portfolio execution driver. Accordingly every
    result is labelled PRICE_PATH_ONLY, even if the caller names an account
    scenario. Omitted costs leave net outcomes unavailable, never zero-cost.

    ``quantity`` is the residual. For a partial exit, supply the immutable
    ``initial_quantity``, earlier realized gross and fees, so both the actual
    and hypothetical outcome use the original total-trade R denominator.
    """

    sign = _direction(direction)
    entry, initial, confirmed = (
        _positive(entry_price),
        _positive(initial_stop),
        _positive(confirmed_stop),
    )
    original_quantity = quantity if initial_quantity is None else initial_quantity
    realized_before = _finite(realized_gross_before_hold)
    prior_fees = _finite(prior_exit_fees)
    fees = [_finite(value) for value in (entry_fees, exit_fees)]
    duration = _positive(bar_duration_seconds)
    deadline, start = (
        _counterfactual_timestamp(forced_deadline_at),
        _counterfactual_timestamp(start_at),
    )
    reference_exit = _positive(actual_exit_price)
    if (
        sign is None
        or entry is None
        or initial is None
        or confirmed is None
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity <= 0
        or not isinstance(original_quantity, int)
        or isinstance(original_quantity, bool)
        or original_quantity < quantity
        or not isinstance(horizon_bars, int)
        or isinstance(horizon_bars, bool)
        or horizon_bars <= 0
        or sign * (entry - initial) <= 0
        or sign * (confirmed - initial) < 0
        or realized_before is None
        or prior_fees is None
        or prior_fees < 0
        or any(
            supplied is not None and (fee is None or fee < 0)
            for supplied, fee in zip((entry_fees, exit_fees), fees)
        )
        or (bar_duration_seconds is not None and duration is None)
        or (forced_deadline_at is not None and deadline is None)
        or (start_at is not None and start is None)
        or (actual_exit_price is not None and reference_exit is None)
    ):
        raise ValueError("hold-N requires valid risk, quantities, clocks and costs")
    risk_price = abs(entry - initial)
    risk = (
        _finite(risk_price * original_quantity)
        if _finite(original_quantity) is not None
        else None
    )
    if risk is None or risk <= 0:
        raise ValueError("initial risk must be finite and positive")
    observed = 0
    previous_end = start
    continuous = True
    maximum_continuation = 0.0 if reference_exit is not None else None
    base = {
        "quality": "PRICE_PATH_ONLY",
        "account_scenario": account_scenario,
        "account_risk_recomputed": False,
        "execution_assumption": "OHLC_MARKET_STOP_PROXY_STOP_FIRST",
        "liquidity_assumption": "EXECUTABLE_OPEN_STOP_OR_CLOSE_NO_IMPACT",
        "cost_model_version": cost_model_version,
        "cost_quality": (
            "DECLARED_FEES" if all(fee is not None for fee in fees) else "UNAVAILABLE"
        ),
        "dataset_version": dataset_version,
        "retained_stop": confirmed,
        "forced_deadline_at": deadline.isoformat() if deadline else None,
        "horizon_bars": horizon_bars,
        "starting_state": {
            "entry_price": entry,
            "initial_stop": initial,
            "initial_quantity": original_quantity,
            "residual_quantity": quantity,
            "initial_risk_currency": risk,
            "realized_gross_before_hold": realized_before,
            "start_at": start.isoformat() if start else None,
        },
    }

    def censored(reason: str) -> dict[str, Any]:
        return {
            **base,
            "status": "CENSORED",
            "censor_reason": reason,
            "observed_bars": observed,
        }

    if not path:
        return censored("INSUFFICIENT_RETAINED_PATH")
    if start is not None and deadline is not None and start >= deadline:
        return censored("START_AT_OR_AFTER_FORCED_DEADLINE")
    for point in path:
        open_price, high, low, close = (
            _positive(point.get(field)) for field in ("open", "high", "low", "close")
        )
        at = _counterfactual_timestamp(
            point.get(
                "interval_end",
                point.get("timestamp", point.get("time", point.get("at"))),
            )
        )
        interval_start = _counterfactual_timestamp(point.get("interval_start"))
        if interval_start is None and at is not None and duration is not None:
            interval_start = at - timedelta(seconds=duration)
        if (
            open_price is None
            or high is None
            or low is None
            or close is None
            or not low <= min(open_price, close) <= max(open_price, close) <= high
            or at is None
            or ("interval_start" in point and interval_start is None)
            or (interval_start is not None and interval_start >= at)
        ):
            return censored("MISSING_OR_INVALID_RETAINED_PATH")
        if interval_start is None:
            return censored("RETAINED_INTERVAL_GEOMETRY_UNAVAILABLE")
        if previous_end is not None:
            if at <= previous_end:
                return censored("NON_CHRONOLOGICAL_RETAINED_PATH")
            if interval_start is not None and interval_start != previous_end:
                return censored("GAP_OR_OVERLAPPING_RETAINED_PATH")
        continuous = continuous and interval_start is not None
        if point.get("executable") is False or (
            "volume" in point and _positive(point.get("volume")) is None
        ):
            return censored("NO_EXECUTABLE_RETAINED_PRICE")

        # Do not inspect a bar's later extremes once the forced boundary has
        # arrived. Only an observed open at that exact boundary is usable.
        if deadline is not None and at > deadline:
            if interval_start == deadline:
                exit_price = open_price
                termination = "SESSION_FORCED_FLAT"
            elif (
                interval_start is not None
                and interval_start < deadline
                and sign * (open_price - confirmed) <= 0
            ):
                exit_price = open_price
                termination = "RETAINED_STOP_TRIGGERED"
            else:
                return censored("FORCED_DEADLINE_PRICE_UNAVAILABLE")
            terminal_extrema_ambiguous = True
        else:
            observed += 1
            stop_hit = low <= confirmed if sign > 0 else high >= confirmed
            if stop_hit:
                exit_price = (
                    min(open_price, confirmed)
                    if sign > 0
                    else max(open_price, confirmed)
                )
                termination = "RETAINED_STOP_TRIGGERED"
            elif deadline is not None and at == deadline:
                exit_price = close
                termination = "SESSION_FORCED_FLAT"
            elif observed >= horizon_bars:
                exit_price = close
                termination = "HORIZON_EXIT"
            else:
                exit_price = None
                termination = None
            terminal_extrema_ambiguous = stop_hit
            if reference_exit is not None:
                # An intrabar high/low after a stopped trade is unreachable.
                # Its open is a safe lower bound; disclose terminal ambiguity.
                favorable = open_price if stop_hit else high if sign > 0 else low
                maximum_continuation = max(
                    maximum_continuation,
                    sign * (favorable - reference_exit) / risk_price,
                )
        previous_end = at
        if termination is None:
            continue
        gross = realized_before + sign * (exit_price - entry) * quantity
        net = (
            gross - fees[0] - prior_fees - fees[1]
            if all(fee is not None for fee in fees)
            else None
        )
        return {
            **base,
            "status": "COMPLETE",
            "termination": termination,
            "observed_bars": observed,
            "path_continuity": "VERIFIED" if continuous else "UNVERIFIED",
            "terminal_extrema_quality": (
                "INTRABAR_ORDER_UNKNOWN" if terminal_extrema_ambiguous else "OBSERVED"
            ),
            "exit_price": _round(exit_price),
            "gross_pnl": _round(gross),
            "net_pnl": _round(net),
            "gross_r": _round(gross / risk),
            "net_r": _round(net / risk if net is not None else None),
            "maximum_additional_favorable_r_lower_bound": _round(maximum_continuation),
            "close_to_actual_exit_r": _round(
                sign * (exit_price - reference_exit) / risk_price
                if reference_exit is not None
                else None
            ),
        }
    return censored("RETAINED_PATH_ENDED_BEFORE_HORIZON")


def compare_actual_exit_to_hold_n(
    actual_net_r: float | None,
    hold_n: Mapping[str, Any],
    *,
    normal_exit: bool = False,
    materiality_r: float = 0.5,
) -> dict[str, Any]:
    """Separate opportunity cost from loss avoided for a completed hold-N run."""

    threshold = _finite(materiality_r)
    if threshold is None or threshold < 0:
        raise ValueError("materiality_r must be finite and nonnegative")
    actual = _finite(actual_net_r)
    simulated = _finite(hold_n.get("net_r"))
    if actual is None or simulated is None or hold_n.get("status") != "COMPLETE":
        return {
            "comparison_quality": "CENSORED_OR_UNAVAILABLE",
            "profit_forgone_r": None,
            "reversal_loss_avoided_r": None,
            "hold_n_delta_r": None,
            "premature_exit_diagnostic": None,
            "price_path_continuation_diagnostic": None,
        }
    delta = simulated - actual
    price_only = hold_n.get("quality") == "PRICE_PATH_ONLY"
    safe_horizon = hold_n.get("termination") == "HORIZON_EXIT"
    return {
        "comparison_quality": "PRICE_PATH_ONLY" if price_only else "COMPLETE",
        "profit_forgone_r": _round(max(0.0, delta)),
        "reversal_loss_avoided_r": _round(max(0.0, -delta)),
        "hold_n_delta_r": _round(delta),
        "materiality_r": threshold,
        "premature_exit_diagnostic": (
            delta >= threshold and safe_horizon
            if normal_exit
            and not price_only
            and hold_n.get("account_risk_recomputed") is True
            and hold_n.get("path_continuity") == "VERIFIED"
            else None
        ),
        "price_path_continuation_diagnostic": (
            delta >= threshold and safe_horizon if normal_exit else None
        ),
    }


def simulate_alternative_exit_policy(**kwargs: Any) -> dict[str, Any]:
    """Run the phase-8 pure policy branch with its hard-risk guard intact.

    This is a decision simulation, not a fill simulator.  It is intentionally
    kept separate from :func:`simulate_risk_constrained_hold_n`, whose output
    has a declared OHLC execution assumption.  Callers must provide retained
    replay events and cannot swap the original hard-risk policy or tick size.
    """

    from .replay import replay_alternative_exit_policy

    result = replay_alternative_exit_policy(**kwargs)
    return {
        "status": "DECISION_REPLAY_COMPLETE",
        "mode": result.mode.value,
        "state_hash": result.state_hash,
        "decisions": [
            evaluation.decision.to_dict() for evaluation in result.evaluations
        ],
        "outcome": "EXECUTION_NOT_SIMULATED_BY_POLICY_REPLAY",
    }


def simulate_alternative_exit_execution(**kwargs: Any) -> dict[str, Any]:
    """Execute paired policy branches through the isolated phase-8 driver."""

    from .backtesting.alternative_policy import simulate_alternative_exit_execution

    return simulate_alternative_exit_execution(**kwargs)


def calculate_delayed_exit_diagnostic(
    *,
    invalidation_at: Any,
    intent_at: Any,
    fill_at: Any,
    grace_seconds: float,
    direction: str,
    invalidation_price: float,
    fill_price: float,
    risk_per_share: float,
) -> dict[str, Any]:
    """Measure a predeclared independent invalidation, never infer one from P&L.

    The caller supplies the frozen reference invalidation event. Policy delay
    and execution delay remain separate even if the latter dominates the loss.
    Adverse R is the change at the actual fill, not an invented intrabar extreme.
    """

    invalidation, intent, fill = map(_timestamp, (invalidation_at, intent_at, fill_at))
    grace, sign = _finite(grace_seconds), _direction(direction)
    mark, exit_price, risk = map(
        _positive, (invalidation_price, fill_price, risk_per_share)
    )
    if grace is None or grace < 0:
        raise ValueError("grace_seconds must be finite and nonnegative")
    if (
        invalidation is None
        or intent is None
        or fill is None
        or not invalidation <= intent <= fill
        or sign is None
        or mark is None
        or exit_price is None
        or risk is None
    ):
        return {"quality": "UNAVAILABLE", "delayed_exit_diagnostic": None}
    policy_delay = (intent - invalidation).total_seconds()
    execution_delay = (fill - intent).total_seconds()
    return {
        "quality": "DECLARED_REFERENCE_INVALIDATION",
        "grace_seconds": grace,
        "delayed_exit_diagnostic": policy_delay + execution_delay > grace,
        "invalidation_to_intent_seconds": policy_delay,
        "intent_to_fill_seconds": execution_delay,
        "policy_delay_beyond_grace_seconds": max(0.0, policy_delay - grace),
        "exposure_beyond_grace_seconds": max(
            0.0, policy_delay + execution_delay - grace
        ),
        "additional_adverse_r_at_fill": _round(
            max(0.0, sign * (mark - exit_price) / risk)
        ),
    }


def calculate_mtm_drawdown(
    equity_curve: Sequence[Mapping[str, Any]], initial_capital: float
) -> dict[str, Any]:
    """Calculate drawdown and session returns from full marked equity samples."""

    capital = _positive(initial_capital)
    if capital is None:
        raise ValueError("initial_capital must be positive and finite")
    samples: list[tuple[datetime, float]] = []
    invalid = 0
    for point in equity_curve:
        at, equity = (
            _timestamp(point.get("timestamp", point.get("time"))),
            _finite(point.get("equity")),
        )
        if at is None or equity is None:
            invalid += 1
            continue
        samples.append((at, equity))
    samples.sort(key=lambda item: item[0])
    if not samples:
        return {
            "basis": "MTM_EQUITY_UNAVAILABLE",
            "max_drawdown_currency": None,
            "max_drawdown_pct": None,
            "session_returns": [],
            "coverage": {"valid_samples": 0, "invalid_samples": invalid},
        }
    peak = capital
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    sessions: dict[str, list[float]] = defaultdict(list)
    for at, equity in samples:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        max_drawdown_pct = max(max_drawdown_pct, 100.0 * (peak - equity) / peak)
        sessions[at.astimezone(EXCHANGE_TIMEZONE).date().isoformat()].append(equity)
    session_returns = []
    previous = capital
    for session_id in sorted(sessions):
        closing = sessions[session_id][-1]
        session_returns.append(
            {
                "session_id": session_id,
                "opening_equity": _round(previous),
                "closing_equity": _round(closing),
                "return_currency": _round(closing - previous),
                "return_pct": _round(100.0 * (closing - previous) / previous)
                if previous > 0
                else None,
            }
        )
        previous = closing
    return {
        "basis": (
            "PARTIAL_MARK_TO_MARKET_EQUITY" if invalid else "FULL_MARK_TO_MARKET_EQUITY"
        ),
        "max_drawdown_currency": _round(max_drawdown),
        "max_drawdown_pct": _round(max_drawdown_pct),
        "max_drawdown_initial_capital_pct": _round(100.0 * max_drawdown / capital),
        "session_returns": session_returns,
        "coverage": {"valid_samples": len(samples), "invalid_samples": invalid},
    }


def build_exit_quality_report(
    records: Sequence[Mapping[str, Any]],
    *,
    equity_curve: Sequence[Mapping[str, Any]] = (),
    initial_capital: float | None = None,
    decisions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate already-measured records without hiding excluded/censored rows."""

    eligible = [record for record in records if record.get("eligible")]
    excluded = [record for record in records if not record.get("eligible")]
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    values_by_metric: dict[str, list[float]] = defaultdict(list)
    reason_distribution: dict[tuple[str, str], int] = defaultdict(int)
    cohorts: dict[tuple[str, str], list[float]] = defaultdict(list)
    for record in eligible:
        metrics = record.get("metrics", {})
        for key in (
            "captured_gross_r",
            "captured_net_r",
            "mfe_r",
            "mae_r",
            "r_given_back",
        ):
            value = _finite(metrics.get(key))
            if value is not None:
                totals[key] += value
                counts[key] += 1
                values_by_metric[key].append(value)
        reason_distribution[
            (
                str(record.get("reason_code") or "UNKNOWN"),
                str(record.get("execution_outcome_code") or "UNKNOWN"),
            )
        ] += 1
        for field in (
            "playbook",
            "regime",
            "entry_regime",
            "exit_regime",
            "regime_transition",
            "setup_variant",
            "symbol",
            "direction",
            "liquidity",
            "entry_time_bucket",
            "exit_time_bucket",
        ):
            value = record.get(field)
            net_r = _finite(metrics.get("captured_net_r"))
            if value is not None and net_r is not None:
                cohorts[(field, str(value))].append(net_r)

    def percentile(values: Sequence[float], proportion: float) -> float:
        ordered = sorted(values)
        index = (len(ordered) - 1) * proportion
        lower, upper = math.floor(index), math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    action_reasons: dict[tuple[str, str], int] = defaultdict(int)
    for decision in decisions:
        action_reasons[
            (
                str(decision.get("action") or "UNKNOWN"),
                str(decision.get("primary_reason_code") or "UNKNOWN"),
            )
        ] += 1
    exclusion_counts: dict[str, int] = defaultdict(int)
    for record in excluded:
        exclusion_counts[str(record.get("exclusion_reason") or "UNKNOWN")] += 1

    return {
        "records_total": len(records),
        "records_eligible": len(eligible),
        "records_excluded": len(excluded),
        "excluded_by_reason": dict(sorted(exclusion_counts.items())),
        "distributions": {
            key: {
                "count": len(values),
                "median": _round(median(values)),
                "p10": _round(percentile(values, 0.1)),
                "p90": _round(percentile(values, 0.9)),
                "minimum": _round(min(values)),
                "maximum": _round(max(values)),
            }
            for key, values in values_by_metric.items()
        },
        "averages": {
            key: _round(totals[key] / counts[key]) if counts[key] else None
            for key in (
                "captured_gross_r",
                "captured_net_r",
                "mfe_r",
                "mae_r",
                "r_given_back",
            )
        },
        "coverage": {
            key: {"available": counts[key], "eligible": len(eligible)}
            for key in (
                "captured_gross_r",
                "captured_net_r",
                "mfe_r",
                "mae_r",
                "r_given_back",
            )
        },
        "reason_distribution": [
            {
                "initiating_reason_code": reason,
                "execution_outcome_code": outcome,
                "count": count,
            }
            for (reason, outcome), count in sorted(reason_distribution.items())
        ],
        "action_reason_distribution": [
            {"action": action, "reason_code": reason, "count": count}
            for (action, reason), count in sorted(action_reasons.items())
        ],
        "decision_coverage": {"retained_decisions": len(decisions)},
        "counterfactual_coverage": {
            "complete": sum(
                (record.get("hold_n") or {}).get("status") == "COMPLETE"
                for record in records
            ),
            "censored": sum(
                (record.get("hold_n") or {}).get("status") == "CENSORED"
                for record in records
            ),
            "unavailable": sum(not record.get("hold_n") for record in records),
        },
        "cohorts": [
            {
                "dimension": dimension,
                "value": value,
                "count": len(values),
                "average_net_r": _round(sum(values) / len(values)),
            }
            for (dimension, value), values in sorted(cohorts.items())
        ],
        "mtm": (
            calculate_mtm_drawdown(equity_curve, initial_capital)
            if initial_capital is not None
            else {
                "basis": "MTM_EQUITY_UNAVAILABLE_FOR_JOURNAL_RECORDS",
                "max_drawdown_currency": None,
                "max_drawdown_pct": None,
                "session_returns": [],
            }
        ),
    }
