"""One deterministic admission priority for a completed production scan batch.

A worker finishing first is not an economic priority. Symbols follow the frozen
point-in-time screener rank; absent ranks sort after the declared universe.
Within a symbol, score then immutable signal fields break ties without random UI
IDs, callback scheduling, or generated receipt timestamps.
"""

from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

ENTRY_ORDERING_VERSION = "pit-universe-rank-symbol-score-v1"
PRODUCTION_CANDLE_HISTORY_DAYS = 5


def entry_signal_order_key(signal: Mapping, rankings: Mapping[str, int]) -> tuple:
    symbol = str(signal["tradingsymbol"])

    def number(name):
        value = signal.get(name)
        if value is None:
            return 0.0
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError(f"entry priority requires finite {name}")
        return float(value)

    return (
        rankings.get(symbol, len(rankings)),
        symbol,
        -number("signal_score"),
        *(
            str(signal.get(name) or "")
            for name in ("playbook", "strategy", "setup_variant", "direction")
        ),
        *(number(name) for name in ("entryPrice", "stopLoss", "target")),
    )


def ordered_entry_signals(
    signals: Sequence[Mapping], universe_symbols: Sequence[str]
) -> list:
    if len(set(universe_symbols)) != len(universe_symbols):
        raise ValueError("entry universe must contain unique symbols")
    rankings = {symbol: index for index, symbol in enumerate(universe_symbols)}
    return sorted(signals, key=lambda signal: entry_signal_order_key(signal, rankings))


def round_entry_price_to_tick(price: float, tick_size: float) -> float:
    """Preserve the production tick-normalization contract in every adapter."""
    return round(round(price / tick_size) * tick_size, 2)
