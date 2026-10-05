"""Order semantics shared by live supervision and research adapters."""

import math
from dataclasses import dataclass

from .entry_ordering import round_entry_price_to_tick
from .time_utils import as_utc


@dataclass(frozen=True)
class ReductionOrderPolicy:
    buffer_fraction: float = 0.01
    tick_size: float = 0.05
    working_timeout_seconds: float = 15.0

    def __post_init__(self):
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (
                self.buffer_fraction,
                self.tick_size,
                self.working_timeout_seconds,
            )
        ):
            raise ValueError("reduction policy values must be finite numbers")
        if not (
            0 <= self.buffer_fraction < 1
            and self.tick_size > 0
            and self.working_timeout_seconds > 0
        ):
            raise ValueError("invalid reduction execution policy")

    def order_fields(self, *, side, mark, hard=False, market_required=False):
        if side not in {"BUY", "SELL"}:
            raise ValueError("reduction side must be BUY or SELL")
        valid_mark = (
            isinstance(mark, (int, float))
            and not isinstance(mark, bool)
            and math.isfinite(mark)
            and mark > 0
        )
        if hard or market_required or not valid_mark:
            return {"order_type": "MARKET"}
        factor = 1 + self.buffer_fraction if side == "BUY" else 1 - self.buffer_fraction
        return {
            "order_type": "LIMIT",
            "price": round_entry_price_to_tick(mark * factor, self.tick_size),
        }

    def cancellation_due(self, *, order_type, submitted_at, now, market_required=False):
        submitted_at, now = as_utc(submitted_at), as_utc(now)
        return bool(
            (market_required and order_type == "LIMIT")
            or (
                submitted_at is not None
                and now is not None
                and (now - submitted_at).total_seconds() >= self.working_timeout_seconds
            )
        )
