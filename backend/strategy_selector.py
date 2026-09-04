"""Deterministic, session-start strategy selection (Phase 1).

At the start of a trading session we look at the first ~15 minutes of market
behaviour, classify the day's *regime* (trending / range-bound / high-volatility
chop), and enable the families of strategies that suit that regime — disabling
the ones that tend to bleed in it.

Design decisions (see issue #62):

- The classifier is **deterministic and authoritative**. There is no LLM here;
  an LLM advisory layer is deferred to a later phase. That keeps the decision
  reproducible, testable, and gives a conservative fallback for free.
- Every input is **optional**. When an input is missing the classifier records
  it and degrades to a more conservative call rather than assuming it.
- Selection only decides which strategies are *eligible to be evaluated*. It is
  never a trade authorisation — the engine's existing entry gates (confidence
  threshold, risk checks, confirmations) remain the sole authority.

The public surface is intentionally pure so it can be unit-tested without a
broker or network:

    context = assemble_context(get_quote_fn, universe)   # I/O at the edge
    decision = select_strategies(context, available_ids)  # pure
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# --- Strategy families -------------------------------------------------------
# Every strategy id in the scanner registry maps to exactly one family. The
# regime -> family mapping below is what actually drives enable/disable.
STRATEGY_FAMILIES: Dict[str, List[str]] = {
    "trend_momentum": [
        "supertrend",
        "adx_momentum",
        "psar_trend",
        "macd_cross",
        "tsi_cross",
        "awesome_oscillator",
    ],
    "mean_reversion": [
        "rsi_reversal",
        "cci_reversal",
        "williams_r",
        "stochastic_reversal",
        "stoc_rsi",
    ],
    "breakout": [
        "bollinger_breakout",
        "keltner_breakout",
        "donchian_breakout",
    ],
    "volume": [
        "vwap_bounce",
        "mfi_exhaustion",
    ],
    "moving_average": [
        "ema_crossover",
    ],
}

# Which families to enable in each regime.
#   TRENDING         -> ride it: trend/momentum + breakout + MA. Mean-reversion
#                       fights the trend, so it's disabled.
#   RANGE_BOUND      -> fade the edges: mean-reversion + volume + MA. Trend and
#                       breakout whipsaw in a range, so they're disabled.
#   HIGH_VOLATILITY  -> conservative: only breakout + volume, which are built for
#                       expansion. Trend-following and mean-reversion both get
#                       chopped up in high-vol days.
#   UNKNOWN          -> insufficient inputs; make no change (handled by the
#                       engine as "leave the user's baseline untouched").
REGIME_FAMILIES: Dict[str, List[str]] = {
    "TRENDING": ["trend_momentum", "breakout", "moving_average"],
    "RANGE_BOUND": ["mean_reversion", "volume", "moving_average"],
    "HIGH_VOLATILITY": ["breakout", "volume"],
}

# --- Classification thresholds (documented, tunable) -------------------------
# All figures are percentages of price, computed across the scan universe from
# the first ~15 minutes of the session.
HIGH_VOL_AVG_ABS_MOVE = 1.5  # mean |open->now| move at/above this => high-vol
TREND_NET_MOVE = 0.4  # mean signed move magnitude at/above this => directional
TREND_BREADTH_UP = 60.0  # >= this % of names up => bullish breadth
TREND_BREADTH_DOWN = 40.0  # <= this % of names up => bearish breadth
MIN_SAMPLE = 10  # fewer valid quotes than this => inputs insufficient (UNKNOWN)


@dataclass
class MarketContext:
    """First-15-minute market context. Every field is optional; a field left
    as ``None`` means the input was unavailable and the classifier must not
    assume it."""

    net_move_pct: Optional[float] = None  # mean signed open->now move (trend)
    avg_abs_move_pct: Optional[float] = None  # mean |open->now| move (volatility)
    breadth_up_pct: Optional[float] = None  # % of names trading above their open
    relative_volume: Optional[float] = None  # today vs baseline; not computed yet
    sample_size: int = 0
    missing_inputs: List[str] = field(default_factory=list)


def all_strategy_ids() -> List[str]:
    """Flattened list of every strategy id known to the family map."""
    return [sid for ids in STRATEGY_FAMILIES.values() for sid in ids]


def _enabled_ids_for_regime(regime: str, available_ids: List[str]) -> List[str]:
    families = REGIME_FAMILIES.get(regime, [])
    wanted = {sid for fam in families for sid in STRATEGY_FAMILIES.get(fam, [])}
    # Only ever return ids the caller actually has configured/registered.
    return [sid for sid in available_ids if sid in wanted]


def classify_regime(context: MarketContext) -> str:
    """Map a MarketContext to one of TRENDING / RANGE_BOUND / HIGH_VOLATILITY /
    UNKNOWN. Pure and deterministic."""
    # Insufficient data -> don't guess.
    if (
        context.sample_size < MIN_SAMPLE
        or context.avg_abs_move_pct is None
        or context.net_move_pct is None
    ):
        return "UNKNOWN"

    # High volatility dominates: a wide average move means expansion/chop.
    if context.avg_abs_move_pct >= HIGH_VOL_AVG_ABS_MOVE:
        return "HIGH_VOLATILITY"

    # Directional + confirmed by breadth (when breadth is available) => trend.
    directional = abs(context.net_move_pct) >= TREND_NET_MOVE
    breadth_ok = True
    if context.breadth_up_pct is not None:
        breadth_ok = (
            context.breadth_up_pct >= TREND_BREADTH_UP
            or context.breadth_up_pct <= TREND_BREADTH_DOWN
        )
    if directional and breadth_ok:
        return "TRENDING"

    # Otherwise treat it as range-bound.
    return "RANGE_BOUND"


def _rationale(regime: str, context: MarketContext) -> str:
    if regime == "UNKNOWN":
        return (
            "Insufficient market context to classify the session "
            f"(sample_size={context.sample_size}); leaving the baseline "
            "strategy set unchanged."
        )
    bits = []
    if context.net_move_pct is not None:
        direction = "up" if context.net_move_pct >= 0 else "down"
        bits.append(f"net move {context.net_move_pct:+.2f}% ({direction})")
    if context.avg_abs_move_pct is not None:
        bits.append(f"avg abs move {context.avg_abs_move_pct:.2f}%")
    if context.breadth_up_pct is not None:
        bits.append(f"breadth {context.breadth_up_pct:.0f}% up")
    detail = ", ".join(bits) if bits else "limited inputs"
    reason = {
        "TRENDING": "directional session with confirming breadth — favouring "
        "trend/momentum, breakout and moving-average strategies.",
        "RANGE_BOUND": "range-bound session — favouring mean-reversion and "
        "volume strategies.",
        "HIGH_VOLATILITY": "high-volatility session — restricting to breakout "
        "and volume strategies.",
    }[regime]
    return f"{regime}: {detail}. {reason}"


def select_strategies(
    context: MarketContext, available_ids: List[str]
) -> Dict[str, object]:
    """Pure decision function. Returns an audit record describing the regime and
    which of ``available_ids`` to enable/disable for the session.

    ``enabled``/``disabled`` are always subsets of ``available_ids`` — an
    unknown or unconfigured id can never be enabled. When the regime is UNKNOWN
    the decision is a no-op (``applied=False``) so the caller leaves the user's
    baseline untouched.
    """
    available = list(dict.fromkeys(available_ids))  # de-dupe, preserve order
    regime = classify_regime(context)

    if regime == "UNKNOWN":
        return {
            "regime": "UNKNOWN",
            "applied": False,
            "enabled": [],
            "disabled": [],
            "rationale": _rationale(regime, context),
            "inputs_used": _inputs_used(context),
            "inputs_missing": list(context.missing_inputs),
            "decided_at": datetime.datetime.now().isoformat(),
        }

    enabled = _enabled_ids_for_regime(regime, available)
    enabled_set = set(enabled)
    disabled = [sid for sid in available if sid not in enabled_set]
    return {
        "regime": regime,
        "applied": True,
        "enabled": enabled,
        "disabled": disabled,
        "rationale": _rationale(regime, context),
        "inputs_used": _inputs_used(context),
        "inputs_missing": list(context.missing_inputs),
        "decided_at": datetime.datetime.now().isoformat(),
    }


def _inputs_used(context: MarketContext) -> Dict[str, object]:
    return {
        "net_move_pct": context.net_move_pct,
        "avg_abs_move_pct": context.avg_abs_move_pct,
        "breadth_up_pct": context.breadth_up_pct,
        "relative_volume": context.relative_volume,
        "sample_size": context.sample_size,
    }


def assemble_context(
    get_quote_fn: Callable[[List[str]], Dict[str, dict]],
    universe: List[str],
    exchange: str = "NSE",
) -> MarketContext:
    """Build a MarketContext from a single batched quote call over the universe.

    Reuses the same open->now / breadth math the screener already relies on. It
    deliberately computes only what today's data actually supports: directional
    move, volatility proxy, and breadth. Relative volume needs a historical
    baseline we don't build in Phase 1, so it is reported as missing rather than
    faked.
    """
    missing: List[str] = ["relative_volume"]
    instruments = [f"{exchange}:{sym}" for sym in universe]
    try:
        quotes = get_quote_fn(instruments) or {}
    except Exception:
        return MarketContext(sample_size=0, missing_inputs=missing + ["market_quotes"])

    signed_moves: List[float] = []
    for _sym, data in quotes.items():
        if not isinstance(data, dict):
            continue
        ltp = data.get("last_price")
        ohlc = data.get("ohlc") or {}
        open_price = ohlc.get("open")
        if not ltp or not open_price:
            continue
        signed_moves.append((ltp - open_price) / open_price * 100.0)

    n = len(signed_moves)
    if n == 0:
        return MarketContext(sample_size=0, missing_inputs=missing + ["market_breadth"])

    net_move = sum(signed_moves) / n
    avg_abs_move = sum(abs(m) for m in signed_moves) / n
    breadth_up = 100.0 * sum(1 for m in signed_moves if m > 0) / n

    return MarketContext(
        net_move_pct=round(net_move, 4),
        avg_abs_move_pct=round(avg_abs_move, 4),
        breadth_up_pct=round(breadth_up, 2),
        relative_volume=None,
        sample_size=n,
        missing_inputs=missing,
    )
