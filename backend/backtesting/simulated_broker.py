"""Deterministic canonical broker used by backtest, paper and replay adapters.

This is deliberately an execution model, not a second exit-policy engine.  It
models declared order/fill assumptions and emits canonical facts; candidate
thesis decisions are supplied by :mod:`backend.replay` and the common order
lifecycle coordinator.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from ..broker_models import (
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerPositionKey,
    BrokerSnapshot,
    ExecutionNamespace,
    FillSnapshot,
    OrderRole,
    OrderSnapshot,
    PositionSnapshot,
    SnapshotQuality,
)
from ..trading_costs import TradingCostCalculator, cost_calculator


@dataclass(frozen=True)
class SimulationExecutionPolicy:
    """Pinned, intentionally small OHLC execution-assumption artifact."""

    policy_version: str = "simulation-execution-v1"
    slippage_bps: float = 5.0
    order_latency_bars: int = 0
    max_fill_fraction: float = 1.0
    max_volume_participation: Optional[float] = None
    ambiguity_policy: str = "STOP_FIRST"
    stop_limit_offset_fraction: float = 0.01
    price_precision: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("simulation policy version is required")
        for name in ("slippage_bps", "max_fill_fraction", "stop_limit_offset_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not 0 < self.max_fill_fraction <= 1:
            raise ValueError("max_fill_fraction must be in (0, 1]")
        participation = self.max_volume_participation
        if participation is not None and (
            isinstance(participation, bool)
            or not isinstance(participation, (int, float))
            or not math.isfinite(participation)
            or not 0 < participation <= 1
        ):
            raise ValueError("max_volume_participation must be in (0, 1]")
        if self.slippage_bps >= 10_000:
            raise ValueError("slippage_bps must be less than 10000")
        if self.stop_limit_offset_fraction >= 1:
            raise ValueError("stop_limit_offset_fraction must be less than 1")
        if (
            isinstance(self.order_latency_bars, bool)
            or not isinstance(self.order_latency_bars, int)
            or self.order_latency_bars < 0
        ):
            raise ValueError("order_latency_bars must be a nonnegative integer")
        if self.ambiguity_policy not in {"STOP_FIRST", "REPORT_ONLY"}:
            raise ValueError("unsupported OHLC ambiguity policy")
        if (
            isinstance(self.price_precision, bool)
            or not isinstance(self.price_precision, int)
            or self.price_precision < 0
        ):
            raise ValueError("price_precision must be a nonnegative integer")


class SimulatedBroker:
    """An isolated event broker with canonical orders, fills and positions.

    The previous implementation mixed a one-off cost calculation with a second
    slippage application on stops and rejected partial exits.  Here every fill
    has one executable price, one fill event, and a fee delta calculated from
    its parent order's aggregate turnover.  Residual exposure retains correctly
    sized protection.
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        *,
        execution_policy: Optional[SimulationExecutionPolicy] = None,
        cost_service: Optional[TradingCostCalculator] = None,
        namespace: ExecutionNamespace | str = ExecutionNamespace.REPLAY,
        account_id: str = "simulation",
    ):
        if not math.isfinite(float(initial_capital)) or initial_capital <= 0:
            raise ValueError("initial capital must be positive and finite")
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.execution_policy = execution_policy or SimulationExecutionPolicy()
        self.cost_service = deepcopy(cost_service or cost_calculator)
        self.namespace = ExecutionNamespace(namespace)
        if self.namespace == ExecutionNamespace.LIVE:
            raise ValueError("simulated execution cannot use the LIVE namespace")
        self.account_id = str(account_id)
        if not self.account_id:
            raise ValueError("simulation account id is required")

        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trades: list[Dict[str, Any]] = []
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.fills: list[Dict[str, Any]] = []
        self.events: list[Dict[str, Any]] = []
        self.pending_orders: list[Dict[str, Any]] = []  # compatibility view
        self.ambiguous_events: list[Dict[str, Any]] = []
        self.censored_positions: list[Dict[str, Any]] = []
        self.reserved_cash = 0.0
        self._prices: Dict[str, float] = {}
        self._mark_times: Dict[str, datetime] = {}
        self._last_candle_times: Dict[str, datetime] = {}
        self._candle_fill_budget: dict[str, int] = {}
        self._position_keys: Dict[str, BrokerPositionKey] = {}
        self._order_sequence = 0
        self._fill_sequence = 0

    @property
    def execution_manifest(self) -> dict[str, Any]:
        """Versioned assumptions stored alongside a run, never inferred later."""

        return {
            "namespace": self.namespace.value,
            "account_id": self.account_id,
            "execution_policy": asdict(self.execution_policy),
            "cost_rate_version": self.cost_service.rate_version,
            "cost_rounding_version": self.cost_service.rounding_version,
            "cost_rates": {
                name: getattr(self.cost_service, name)
                for name in (
                    "brokerage_pct",
                    "max_brokerage",
                    "stt_sell_pct",
                    "exchange_txn_pct",
                    "sebi_pct",
                    "stamp_buy_pct",
                    "gst_pct",
                )
            },
            "cost_schedule_basis": "INJECTED_SCENARIO_NOT_HISTORICAL_RATE_LOOKUP",
            "candle_timestamp_convention": "BAR_START",
            "partial_fill_model": "FRACTION_OF_REMAINING_PER_CANDLE",
            "missing_volume_policy": "NO_FILLS"
            if self.execution_policy.max_volume_participation is not None
            else "UNCONSTRAINED_SYNTHETIC_LIQUIDITY",
            "zero_volume_policy": "NO_FILLS",
            "protection_activation": "IMMEDIATE_CONFIRMED",
            "cancellation_ack_latency_bars": 0,
            "target_order_type": "RESTING_LIMIT",
            "quantity_lot_size": 1,
            "price_tick_size": 10**-self.execution_policy.price_precision,
            "immediate_helpers": "FIXED_EXECUTION_EVENT_IGNORES_LATENCY_AND_FILL_FRACTION",
        }

    @staticmethod
    def _finite_price(value: Any, name: str = "price") -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be a finite positive price")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive price")
        return value

    @staticmethod
    def _quantity(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("quantity must be a positive integer")
        return value

    @staticmethod
    def _time(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError("simulation events need datetime timestamps")
        # The legacy raw-strategy lab used naive pandas timestamps.  Treat
        # those historical inputs as UTC deterministically rather than reading
        # host-local time.  Production/paper/replay callers still pass aware
        # timestamps and retain their explicit exchange conversion upstream.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _next_order_id(self) -> str:
        self._order_sequence += 1
        return f"SIM-ORDER-{self._order_sequence:08d}"

    def _next_fill_id(self) -> str:
        self._fill_sequence += 1
        return f"SIM-FILL-{self._fill_sequence:08d}"

    def _key_for(self, symbol: str) -> BrokerPositionKey:
        return self._position_keys.setdefault(
            symbol,
            BrokerPositionKey(
                namespace=self.namespace,
                account_id=self.account_id,
                exchange="NSE",
                instrument_id=f"SIM-{symbol}",
                tradingsymbol=symbol,
                product="MIS",
            ),
        )

    def _new_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        timestamp: datetime,
        order_type: str,
        role: OrderRole,
        tag: Optional[str] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        signal_info: Optional[Mapping[str, Any]] = None,
        reason: Optional[str] = None,
        latency_bars: Optional[int] = None,
    ) -> Dict[str, Any]:
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("simulated orders require a symbol")
        self._quantity(quantity)
        timestamp = self._time(timestamp)
        signal_info = dict(signal_info or {})
        for field in ("stopLoss", "target"):
            if signal_info.get(field) is not None:
                signal_info[field] = self._finite_price(signal_info[field], field)
        if price is not None:
            price = self._finite_price(price)
        if trigger_price is not None:
            trigger_price = self._finite_price(trigger_price, "trigger price")
        order_type = str(order_type).upper()
        if order_type == "SL-LIMIT":
            order_type = "SL"  # Read the old research alias; emit Kite's contract.
        if order_type not in {"MARKET", "LIMIT", "SL", "SL-M"}:
            raise ValueError("unsupported simulated order type")
        if order_type in {"LIMIT", "SL"} and price is None:
            raise ValueError("limit orders require a price")
        if order_type in {"SL", "SL-M"} and trigger_price is None:
            raise ValueError("stop orders require a trigger price")
        order = {
            "order_id": self._next_order_id(),
            "symbol": symbol,
            "position_key": self._key_for(symbol).as_string(),
            "transaction_type": side,
            "side": side,
            "quantity": quantity,
            "filled_quantity": 0,
            "remaining_quantity": quantity,
            "average_price": None,
            "price": price,
            "trigger_price": trigger_price,
            "order_type": order_type,
            "role": role.value,
            "tag": tag,
            "status": "OPEN",
            "reason": reason,
            "submitted_at": timestamp,
            "updated_at": timestamp,
            "latency_remaining": self.execution_policy.order_latency_bars
            if latency_bars is None
            else latency_bars,
            "signal_info": dict(signal_info or {}),
            "fees": 0.0,
            "charge_components": {},
            "triggered": False,
        }
        self.orders[order["order_id"]] = order
        self.events.append(
            {
                "type": "ORDER_SUBMITTED",
                "order_id": order["order_id"],
                "at": timestamp.isoformat(),
                "role": role.value,
            }
        )
        self._refresh_pending_orders()
        return order

    def _refresh_pending_orders(self) -> None:
        self.pending_orders = [
            dict(order)
            for order in self.orders.values()
            if order["status"] not in {"COMPLETE", "CANCELLED", "REJECTED"}
        ]

    def _slipped(self, raw_price: float, side: str) -> float:
        raw_price = self._finite_price(raw_price)
        impact = self.execution_policy.slippage_bps / 10_000.0
        value = raw_price * (1 + impact if side == "BUY" else 1 - impact)
        return self._finite_price(
            round(value, self.execution_policy.price_precision), "executable price"
        )

    def _apply_order_fee(self, order: Dict[str, Any]) -> float:
        turnover = sum(
            float(fill["price"]) * int(fill["quantity"])
            for fill in self.fills
            if fill["order_id"] == order["order_id"]
        )
        charges = self.cost_service.calculate_turnover_charges(turnover, order["side"])
        new_total = float(charges["total"])
        delta = round(new_total - float(order["fees"]), 2)
        order["fees"] = new_total
        order["charge_components"] = charges
        self.cash = round(self.cash - delta, 2)
        return delta

    def _update_protection_quantity(
        self, position: Dict[str, Any], timestamp: datetime
    ) -> None:
        stop_id = position.get("stop_order_id")
        if not stop_id or stop_id not in self.orders:
            return
        stop = self.orders[stop_id]
        if stop["status"] in {"COMPLETE", "CANCELLED", "REJECTED"}:
            return
        stop["quantity"] = stop["filled_quantity"] + position["quantity"]
        stop["remaining_quantity"] = position["quantity"]
        stop["updated_at"] = timestamp

    def _ensure_protection(self, position: Dict[str, Any], timestamp: datetime) -> None:
        stop_price = position.get("sl")
        if stop_price is None:
            return
        stop_id = position.get("stop_order_id")
        if stop_id in self.orders and self.orders[stop_id]["status"] not in {
            "COMPLETE",
            "CANCELLED",
            "REJECTED",
        }:
            self._update_protection_quantity(position, timestamp)
            return
        side = "SELL" if position["direction"] == "BUY" else "BUY"
        stop = self._new_order(
            symbol=position["symbol"],
            side=side,
            quantity=position["quantity"],
            timestamp=timestamp,
            order_type="SL" if position.get("stop_limit_price") is not None else "SL-M",
            role=OrderRole.PROTECTION,
            price=position.get("stop_limit_price"),
            trigger_price=stop_price,
            reason="protective_stop",
            latency_bars=0,
        )
        stop["status"] = "TRIGGER PENDING"
        position["stop_order_id"] = stop["order_id"]

    def _record_trade(
        self, position: Dict[str, Any], timestamp: datetime, reason: str
    ) -> None:
        quantity = position["initial_quantity"]
        exit_price = position["exit_notional"] / quantity
        gross_pnl = position["realized_gross"]
        total_fees = round(position["entry_fees"] + position["exit_fees"], 2)
        self.trades.append(
            {
                "symbol": position["symbol"],
                "direction": position["direction"],
                "entry_time": position["entry_time"],
                "exit_time": timestamp,
                "entry_price": position["entry_notional"] / quantity,
                "exit_price": round(exit_price, self.execution_policy.price_precision),
                "quantity": quantity,
                "mfe": position["mfe"],
                "mae": position["mae"],
                "signal_info": dict(position.get("signal_info", {})),
                "exit_reason": reason,
                "gross_pnl": round(gross_pnl, 2),
                "total_fees": total_fees,
                "net_pnl": round(gross_pnl - total_fees, 2),
                "execution_policy_version": self.execution_policy.policy_version,
                "ambiguity": bool(position.get("ambiguity", False)),
                "excursion_quality": position.get("excursion_quality", "COMPLETE_BARS"),
            }
        )

    def _apply_fill(
        self,
        order: Dict[str, Any],
        *,
        price: float,
        quantity: int,
        timestamp: datetime,
        reason: Optional[str],
    ) -> None:
        symbol = order["symbol"]
        side = order["side"]
        existing = self.positions.get(symbol)
        if existing is None and order["role"] != OrderRole.ENTRY.value:
            raise ValueError("a simulated reduction cannot open a position")
        if existing is not None:
            expected_exit = "SELL" if existing["direction"] == "BUY" else "BUY"
            if side == expected_exit and quantity > existing["quantity"]:
                raise ValueError("simulated reduction cannot reverse a position")
            if (
                side == existing["direction"]
                and order["order_id"] not in existing["entry_order_ids"]
            ):
                raise ValueError("pyramiding is outside the simulation policy")

        fill = {
            "fill_id": self._next_fill_id(),
            "order_id": order["order_id"],
            "symbol": symbol,
            "position_key": order["position_key"],
            "side": side,
            "quantity": quantity,
            "price": price,
            "exchange_time": timestamp,
            "received_at": timestamp,
        }
        self.fills.append(fill)
        self._prices[symbol] = price
        self._mark_times[symbol] = timestamp
        self.cash = round(
            self.cash + (-price * quantity if side == "BUY" else price * quantity),
            2,
        )
        fee_delta = self._apply_order_fee(order)

        if existing is None:
            position = {
                "symbol": symbol,
                "direction": side,
                "quantity": quantity,
                "initial_quantity": quantity,
                "entry_price": price,
                "entry_notional": price * quantity,
                "entry_time": timestamp,
                "entry_order_ids": {order["order_id"]},
                "entry_fees": fee_delta,
                "exit_fees": 0.0,
                "exit_notional": 0.0,
                "realized_gross": 0.0,
                "signal_info": dict(order.get("signal_info") or {}),
                "sl": (order.get("signal_info") or {}).get("stopLoss"),
                "target": (order.get("signal_info") or {}).get("target"),
                "mfe": price,
                "mae": price,
                "ambiguity": False,
                "excursion_quality": "COMPLETE_BARS",
            }
            if (order.get("signal_info") or {}).get("stopOrderType") == "SL":
                stop_price = position["sl"]
                if stop_price is not None:
                    offset = self.execution_policy.stop_limit_offset_fraction
                    position["stop_limit_price"] = round(
                        stop_price * (1 - offset if side == "BUY" else 1 + offset),
                        self.execution_policy.price_precision,
                    )
            self.positions[symbol] = position
            self._ensure_protection(position, timestamp)
            return

        if side == existing["direction"]:
            old_quantity = existing["quantity"]
            new_quantity = old_quantity + quantity
            existing["entry_price"] = (
                existing["entry_price"] * old_quantity + price * quantity
            ) / new_quantity
            existing["quantity"] = new_quantity
            existing["initial_quantity"] += quantity
            existing["entry_notional"] += price * quantity
            existing["entry_fees"] += fee_delta
            existing["entry_order_ids"].add(order["order_id"])
            self._ensure_protection(existing, timestamp)
            return

        # An opposite fill is a reduction of actual residual quantity, never a
        # new reverse position.  Partial exits stay in the same position epoch.
        gross = (
            (price - existing["entry_price"]) * quantity
            if existing["direction"] == "BUY"
            else (existing["entry_price"] - price) * quantity
        )
        existing["quantity"] -= quantity
        existing["exit_notional"] += price * quantity
        existing["realized_gross"] += gross
        existing["exit_fees"] += fee_delta
        if existing["quantity"]:
            # A partially filled protective order will update its own residual
            # below.  Updating it here would subtract its fill twice.
            if existing.get("stop_order_id") != order["order_id"]:
                self._update_protection_quantity(existing, timestamp)
            target = self.orders.get(existing.get("target_order_id"))
            if (
                target
                and target["order_id"] != order["order_id"]
                and target["status"] not in {"COMPLETE", "CANCELLED", "REJECTED"}
            ):
                target["quantity"] = target["filled_quantity"] + existing["quantity"]
                target["remaining_quantity"] = existing["quantity"]
            return

        self._record_trade(existing, timestamp, reason or order.get("reason") or "exit")
        for pending in list(self.orders.values()):
            if pending["symbol"] == symbol and pending["order_id"] != order["order_id"]:
                self.cancel_order(pending["order_id"], timestamp=timestamp)
        del self.positions[symbol]

    def _fill_order(
        self,
        order_id: str,
        *,
        raw_price: float,
        quantity: int,
        timestamp: datetime,
        reason: Optional[str] = None,
    ) -> None:
        order = self.orders[order_id]
        if order["status"] in {"COMPLETE", "CANCELLED", "REJECTED"}:
            return
        timestamp = self._time(timestamp)
        if timestamp < order["submitted_at"]:
            raise ValueError("an order cannot fill before submission")
        if order["role"] != OrderRole.ENTRY.value:
            position = self.positions.get(order["symbol"])
            if position is None:
                self.cancel_order(order_id, timestamp=timestamp)
                return
            if position.get("execution_censored"):
                return
            expected_side = "SELL" if position["direction"] == "BUY" else "BUY"
            if order["side"] != expected_side:
                raise ValueError("simulated reduction side disagrees with residual")
            order["remaining_quantity"] = min(
                order["remaining_quantity"], position["quantity"]
            )
            order["quantity"] = order["filled_quantity"] + order["remaining_quantity"]
        quantity = min(self._quantity(quantity), order["remaining_quantity"])
        if order["symbol"] in self._candle_fill_budget:
            quantity = min(quantity, self._candle_fill_budget[order["symbol"]])
        if not quantity:
            return
        price = self._slipped(raw_price, order["side"])
        if order["order_type"] in {"LIMIT", "SL"}:
            limit = order["price"]
            price = min(price, limit) if order["side"] == "BUY" else max(price, limit)
        position = self.positions.get(order["symbol"])
        if position is not None:
            # Only the observed executable print is certainly inside exposure;
            # the exit bar's other extrema may occur after the reduction.
            self._update_excursions(position, high=price, low=price)
        self._apply_fill(
            order, price=price, quantity=quantity, timestamp=timestamp, reason=reason
        )
        if order["symbol"] in self._candle_fill_budget:
            self._candle_fill_budget[order["symbol"]] -= quantity
        previous = order["filled_quantity"]
        order["filled_quantity"] += quantity
        order["remaining_quantity"] -= quantity
        order["average_price"] = (
            price
            if not previous
            else (order["average_price"] * previous + price * quantity)
            / order["filled_quantity"]
        )
        order["updated_at"] = timestamp
        order["status"] = "COMPLETE" if not order["remaining_quantity"] else "OPEN"
        self.events.append(
            {
                "type": "FILL",
                "fill_id": self.fills[-1]["fill_id"],
                "order_id": order_id,
                "at": timestamp.isoformat(),
                "reason": reason,
            }
        )
        self._refresh_pending_orders()

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        price: float,
        timestamp: datetime,
        signal_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Immediately execute a MARKET order at one slippage-adjusted price."""

        order = self._new_order(
            symbol=symbol,
            side=direction,
            quantity=quantity,
            timestamp=timestamp,
            order_type="MARKET",
            role=OrderRole.ENTRY
            if symbol not in self.positions
            else OrderRole.REDUCTION,
            signal_info=signal_info,
            latency_bars=0,
        )
        self._fill_order(
            order["order_id"],
            raw_price=price,
            quantity=quantity,
            timestamp=self._time(timestamp),
            reason="market",
        )
        return order["order_id"]

    def close_position(
        self,
        symbol: str,
        price: float,
        timestamp: datetime,
        reason: str = "forced_close",
    ) -> None:
        """Close only the known residual; no synthetic reverse order is allowed."""

        position = self.positions.get(symbol)
        if position is None:
            return
        if position.get("execution_censored"):
            return
        order = self._new_order(
            symbol=symbol,
            side="SELL" if position["direction"] == "BUY" else "BUY",
            quantity=position["quantity"],
            timestamp=timestamp,
            order_type="MARKET",
            role=OrderRole.REDUCTION,
            reason=reason,
            latency_bars=0,
        )
        self._fill_order(
            order["order_id"],
            raw_price=price,
            quantity=position["quantity"],
            timestamp=self._time(timestamp),
            reason=reason,
        )

    def set_protective_stop(
        self,
        symbol: str,
        trigger_price: float,
        timestamp: datetime,
        *,
        stop_limit: bool = False,
        limit_price: Optional[float] = None,
    ) -> str:
        """Install or ratchet a residual protective stop in simulation.

        This mirrors the coordinator-facing safety invariant: a live position
        never receives looser confirmed protection merely because a later
        policy evaluation changed its mind.  Stop-limit orders intentionally
        retain their triggered-but-unfilled state across a gap.
        """

        position = self.positions.get(symbol)
        if position is None:
            raise ValueError("cannot protect a flat simulated position")
        timestamp = self._time(timestamp)
        trigger_price = self._finite_price(trigger_price, "trigger price")
        current = position.get("sl")
        if current is not None:
            if position["direction"] == "BUY" and trigger_price < current:
                raise ValueError("long protective stop cannot loosen")
            if position["direction"] == "SELL" and trigger_price > current:
                raise ValueError("short protective stop cannot loosen")
        if stop_limit:
            if limit_price is None:
                fraction = self.execution_policy.stop_limit_offset_fraction
                limit_price = trigger_price * (
                    1 - fraction if position["direction"] == "BUY" else 1 + fraction
                )
            limit_price = self._finite_price(limit_price, "stop limit price")
            if (position["direction"] == "BUY" and limit_price > trigger_price) or (
                position["direction"] == "SELL" and limit_price < trigger_price
            ):
                raise ValueError("stop-limit price must be on the executable side")
        position["sl"] = trigger_price
        position["stop_limit_price"] = limit_price if stop_limit else None
        self._ensure_protection(position, timestamp)
        stop = self.orders[position["stop_order_id"]]
        stop.update(
            {
                "trigger_price": trigger_price,
                "price": limit_price if stop_limit else None,
                "order_type": "SL" if stop_limit else "SL-M",
                "updated_at": timestamp,
            }
        )
        return stop["order_id"]

    def cancel_order(
        self, order_id: str, *, timestamp: Optional[datetime] = None
    ) -> None:
        order = self.orders.get(order_id)
        if order is None or order["status"] in {"COMPLETE", "CANCELLED", "REJECTED"}:
            return
        order["status"] = "CANCELLED"
        order["updated_at"] = self._time(timestamp or order["updated_at"])
        self.events.append({"type": "ORDER_CANCELLED", "order_id": order_id})
        self._refresh_pending_orders()

    def _update_excursions(
        self, position: Dict[str, Any], *, high: float, low: float
    ) -> None:
        if position["direction"] == "BUY":
            position["mfe"] = max(position["mfe"], high)
            position["mae"] = min(position["mae"], low)
        else:
            position["mfe"] = min(position["mfe"], low)
            position["mae"] = max(position["mae"], high)

    def mark_price(self, symbol: str, price: float, observed_at: datetime) -> None:
        """Record an observed mark without inventing freshness at snapshot time."""

        observed_at = self._time(observed_at)
        price = self._finite_price(price)
        previous = self._mark_times.get(symbol)
        if previous is not None and observed_at < previous:
            raise ValueError("simulated price marks must be chronological")
        self._prices[symbol] = price
        self._mark_times[symbol] = observed_at

    def _fill_quantity(self, remaining: int) -> int:
        return max(1, math.floor(remaining * self.execution_policy.max_fill_fraction))

    def _process_working_orders(
        self, symbol: str, candle: pd.Series, timestamp: datetime
    ) -> None:
        for order in list(self.orders.values()):
            if (
                order["symbol"] != symbol
                or order["status"] not in {"OPEN", "TRIGGERED"}
                or order["order_type"] not in {"MARKET", "LIMIT"}
                or order["reason"] == "target"
                or order["submitted_at"] > timestamp
            ):
                continue
            if order["latency_remaining"]:
                order["latency_remaining"] -= 1
                continue
            raw = float(candle["open"])
            if order["order_type"] == "LIMIT" and order["price"] is not None:
                if order["side"] == "BUY" and float(candle["low"]) > order["price"]:
                    continue
                if order["side"] == "SELL" and float(candle["high"]) < order["price"]:
                    continue
                raw = (
                    min(raw, order["price"])
                    if order["side"] == "BUY"
                    else max(raw, order["price"])
                )
            self._fill_order(
                order["order_id"],
                raw_price=raw,
                quantity=self._fill_quantity(order["remaining_quantity"]),
                timestamp=timestamp,
            )
            if (
                order["role"] == OrderRole.ENTRY.value
                and raw != float(candle["open"])
                and symbol in self.positions
            ):
                self.positions[symbol]["excursion_quality"] = "PARTIAL_BAR_BOUNDS"
                self.positions[symbol]["intrabar_entry_at"] = timestamp

    def process_candle(self, symbol: str, candle: pd.Series):
        """Advance one OHLC event using declared conservative ambiguity rules."""

        for name in ("open", "high", "low", "close", "date"):
            if name not in candle:
                raise ValueError(f"candle is missing {name}")
        timestamp = self._time(candle["date"])
        last_time = self._last_candle_times.get(symbol)
        if last_time is not None and timestamp <= last_time:
            raise ValueError("simulated candles must be unique and chronological")
        opening = self._finite_price(candle["open"], "open")
        high = self._finite_price(candle["high"], "high")
        low = self._finite_price(candle["low"], "low")
        close = self._finite_price(candle["close"], "close")
        if low > min(opening, close, high) or high < max(opening, close, low):
            raise ValueError("invalid OHLC price envelope")
        volume = candle.get("volume")
        if volume is not None:
            if (
                isinstance(volume, bool)
                or not math.isfinite(float(volume))
                or volume < 0
            ):
                raise ValueError("candle volume must be finite and nonnegative")
        mark_time = self._time(
            candle.get("mark_time", candle.get("available_at", timestamp))
        )
        if mark_time < timestamp:
            raise ValueError("candle cannot be available before its start")
        if self._mark_times.get(symbol, timestamp) > mark_time:
            raise ValueError("candle is older than the latest price mark")
        self._last_candle_times[symbol] = timestamp
        participation = self.execution_policy.max_volume_participation
        if participation is not None:
            self._candle_fill_budget[symbol] = (
                math.floor(float(volume) * participation) if volume is not None else 0
            )
        # An empty-volume interval supplies no executable event. In particular
        # it cannot manufacture a guaranteed stop/target or ordinary-order fill.
        if volume == 0:
            self._candle_fill_budget.pop(symbol, None)
            self.mark_price(symbol, close, mark_time)
            self.events.append(
                {"type": "NO_LIQUIDITY", "symbol": symbol, "at": timestamp.isoformat()}
            )
            return

        try:
            self._execute_candle(symbol, candle, timestamp, opening, high, low)
        finally:
            self._candle_fill_budget.pop(symbol, None)
            # Fills have their own executable prints; MTM uses the observed
            # closing mark, never the first/last fill from within the bar.
            self.mark_price(symbol, close, mark_time)

    def _execute_candle(
        self,
        symbol: str,
        candle: pd.Series,
        timestamp: datetime,
        opening: float,
        high: float,
        low: float,
    ) -> None:
        if self.positions.get(symbol, {}).get("execution_censored"):
            return
        # A previously submitted ordinary order is only executable at this
        # newly available event, never at the close that generated it.
        self._process_working_orders(symbol, candle, timestamp)
        position = self.positions.get(symbol)
        if position is None:
            return

        stop = position.get("sl")
        stop_order = self.orders.get(position.get("stop_order_id"))
        stop_active = bool(
            stop_order
            and stop_order["status"] not in {"COMPLETE", "CANCELLED", "REJECTED"}
            and stop_order["updated_at"] <= timestamp
        )
        target = position.get("target")
        if position["direction"] == "BUY":
            stop_hit = stop is not None and low <= stop
            target_hit = target is not None and high >= target
        else:
            stop_hit = stop is not None and high >= stop
            target_hit = target is not None and low <= target
        if position.get("intrabar_entry_at") == timestamp:
            # A bar's favorable extreme can precede a limit entry. Its close
            # is known to follow the entry, so only that print proves a target
            # crossing within the actual exposure window.
            target_hit = target is not None and (
                float(candle["close"]) >= target
                if position["direction"] == "BUY"
                else float(candle["close"]) <= target
            )
        stop_hit = stop_active and (stop_hit or stop_order["triggered"])
        # If the opening print already satisfies an active target, a later
        # stop touch is not ambiguous. The order at the open precedes it.
        target_at_open = (
            target_hit
            and position.get("intrabar_entry_at") != timestamp
            and (
                opening >= target
                if position["direction"] == "BUY"
                else opening <= target
            )
        )
        stop_at_open = stop_active and (
            stop_order["triggered"]
            or (opening <= stop if position["direction"] == "BUY" else opening >= stop)
        )
        if target_at_open and not stop_at_open:
            stop_hit = False
        if stop_at_open:
            target_hit = False
        if stop_hit and target_hit:
            position["ambiguity"] = True
            event = {
                "symbol": symbol,
                "timestamp": timestamp.isoformat(),
                "type": "STOP_TARGET_AMBIGUITY",
                "resolution": self.execution_policy.ambiguity_policy,
                "first_fill_price_bounds": {
                    "stop_first": self._slipped(stop, stop_order["side"]),
                    "target_first": target,
                },
            }
            if stop_order["order_type"] == "SL":
                bound = event["first_fill_price_bounds"]["stop_first"]
                event["first_fill_price_bounds"]["stop_first"] = (
                    max(bound, stop_order["price"])
                    if position["direction"] == "BUY"
                    else min(bound, stop_order["price"])
                )
            self.ambiguous_events.append(event)
            self.events.append(event)
            if self.execution_policy.ambiguity_policy == "REPORT_ONLY":
                position["excursion_quality"] = "CENSORED_AMBIGUOUS_EXECUTION"
                position["execution_censored"] = True
                self.censored_positions.append(
                    {
                        "symbol": symbol,
                        "reason": "STOP_TARGET_AMBIGUITY",
                        "at": timestamp,
                    }
                )
                return

        if stop_hit:
            stop_id = stop_order["order_id"]
            already_triggered = stop_order["triggered"]
            stop_order["triggered"] = True
            stop_order["status"] = "TRIGGERED"
            # A stop-limit gap can trigger without a fill.  It stays a visible
            # protection/recovery obligation rather than becoming an ideal stop.
            if stop_id and self.orders[stop_id]["order_type"] == "SL":
                limit = self.orders[stop_id].get("price")
                executable = (
                    high >= limit if position["direction"] == "BUY" else low <= limit
                )
                if not executable:
                    self.orders[stop_id]["status"] = "TRIGGERED"
                    self.events.append(
                        {
                            "type": "STOP_LIMIT_UNFILLED",
                            "order_id": stop_id,
                            "at": timestamp.isoformat(),
                        }
                    )
                    self._update_excursions(position, high=high, low=low)
                    return
            raw = (
                opening
                if already_triggered
                else (
                    min(opening, stop)
                    if position["direction"] == "BUY"
                    else max(opening, stop)
                )
            )
            if stop_order["order_type"] == "SL":
                raw = (
                    max(raw, limit)
                    if position["direction"] == "BUY"
                    else min(raw, limit)
                )
            if not stop_at_open:
                position["excursion_quality"] = "PARTIAL_BAR_BOUNDS"
            self._fill_order(
                stop_id,
                raw_price=raw,
                quantity=self._fill_quantity(position["quantity"]),
                timestamp=timestamp,
                reason="stop_loss",
            )
            if symbol in self.positions:
                self._update_excursions(position, high=high, low=low)
            return

        if target_hit:
            raw = (
                max(opening, target)
                if position["direction"] == "BUY"
                else min(opening, target)
            )
            if position.get("intrabar_entry_at") == timestamp:
                raw = target
            order = self.orders.get(position.get("target_order_id"))
            if not order or order["status"] in {"COMPLETE", "CANCELLED", "REJECTED"}:
                order = self._new_order(
                    symbol=symbol,
                    side="SELL" if position["direction"] == "BUY" else "BUY",
                    quantity=position["quantity"],
                    timestamp=timestamp,
                    order_type="LIMIT",
                    role=OrderRole.REDUCTION,
                    price=target,
                    reason="target",
                    latency_bars=0,
                )
                position["target_order_id"] = order["order_id"]
            if not target_at_open:
                position["excursion_quality"] = "PARTIAL_BAR_BOUNDS"
            self._fill_order(
                order["order_id"],
                raw_price=raw,
                quantity=self._fill_quantity(position["quantity"]),
                timestamp=timestamp,
                reason="target",
            )
            if symbol in self.positions:
                self._update_excursions(position, high=high, low=low)
            return

        if position.get("intrabar_entry_at") != timestamp:
            self._update_excursions(position, high=high, low=low)

    def submit_coordinator_order(
        self, attempt_tag: str, payload: Mapping[str, Any]
    ) -> str:
        """Coordinator mutation callback; submission itself is never a fill."""

        timestamp = payload.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(timestamp, datetime):
            raise ValueError("simulated coordinator submission needs timestamp")
        side = payload.get("transaction_type") or payload.get("side")
        symbol = str(payload["tradingsymbol"])
        role = OrderRole(str(payload.get("role", "REDUCTION")).upper())
        order_type = str(payload.get("order_type", "MARKET")).upper()
        if order_type == "SL-LIMIT":
            order_type = "SL"
        if role is OrderRole.PROTECTION:
            position = self.positions.get(symbol)
            if (
                position is None
                or order_type not in {"SL", "SL-M"}
                or side != ("SELL" if position["direction"] == "BUY" else "BUY")
                or self._quantity(payload["quantity"]) != position["quantity"]
            ):
                raise ValueError("protective order must cover the actual residual")
            if order_type == "SL" and payload.get("price") is None:
                raise ValueError("SL stop-limit orders require a price")
            order_id = self.set_protective_stop(
                symbol,
                payload["trigger_price"],
                timestamp,
                stop_limit=order_type == "SL",
                limit_price=payload.get("price"),
            )
            self.orders[order_id]["tag"] = attempt_tag
            return order_id
        if order_type in {"SL", "SL-M"}:
            raise ValueError("simulated stop orders require the PROTECTION role")
        order = self._new_order(
            symbol=symbol,
            side=str(side),
            quantity=payload["quantity"],
            timestamp=timestamp,
            order_type=order_type,
            role=role,
            tag=attempt_tag,
            price=payload.get("price"),
            trigger_price=payload.get("trigger_price"),
            reason=payload.get("reason"),
            signal_info=payload.get("signal_info"),
        )
        return order["order_id"]

    def coordinator_callbacks(self, position_key: str) -> dict[str, Any]:
        """Return the exact callbacks consumed by ``OrderLifecycleCoordinator``."""

        return {
            "cancel_stop": self.cancel_order,
            "read_order": self.get_order,
            "read_residual": lambda _stop: self.read_residual(position_key),
            "submit_order": self.submit_coordinator_order,
        }

    def reconcile_fills_with(self, coordinator: Any, position_key: str) -> int:
        """Feed canonical simulated fills through the common durable ledger.

        The method is intentionally explicit: replay drivers decide when a
        broker event is observed, while this adapter guarantees the same
        idempotent ``record_fill`` contract used in live recovery.
        """

        recorded = 0
        for fill in self.fills:
            if fill["position_key"] != position_key:
                continue
            if coordinator.record_fill(
                position_key=position_key,
                broker_fill_id=fill["fill_id"],
                broker_order_id=fill["order_id"],
                side=fill["side"],
                quantity=fill["quantity"],
                fill_price=fill["price"],
                exchange_time=fill["exchange_time"],
                raw_fill=dict(fill),
            ):
                recorded += 1
        return recorded

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        order = self.orders.get(order_id)
        return dict(order) if order is not None else None

    def read_residual(self, position_key: str) -> Optional[dict[str, Any]]:
        for symbol, key in self._position_keys.items():
            if key.as_string() == position_key:
                position = self.positions.get(symbol)
                if position is None:
                    return {"quantity": 0}
                signed = (
                    position["quantity"]
                    if position["direction"] == "BUY"
                    else -position["quantity"]
                )
                return {
                    "quantity": signed,
                    "tradingsymbol": symbol,
                    "exchange": key.exchange,
                    "product": key.product,
                    "last_price": self._prices.get(symbol),
                }
        return None

    @staticmethod
    def _session_date(timestamp: datetime):
        return timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date()

    def position_snapshot(self, timestamp: datetime) -> PositionSnapshot:
        """Cumulative session facts include closed symbols and actual turnover.

        Broker receipt never refreshes an old market mark. Closed-session fills
        do not contaminate today's accounting; an overnight residual is exposed
        explicitly so the caller can refuse an unsupported carried-state study.
        """
        timestamp = self._time(timestamp)
        day = self._session_date(timestamp)
        fills = [
            f
            for f in self.fills
            if self._session_date(f["exchange_time"]) == day
            and f["exchange_time"] <= timestamp
        ]
        rows = []
        for symbol in sorted(set(self.positions) | {f["symbol"] for f in fills}):
            current = self.positions.get(symbol)
            symbol_fills = [f for f in fills if f["symbol"] == symbol]
            buy = [f for f in symbol_fills if f["side"] == "BUY"]
            sell = [f for f in symbol_fills if f["side"] == "SELL"]
            bought = sum(f["quantity"] for f in buy)
            sold = sum(f["quantity"] for f in sell)
            buy_value = sum(f["quantity"] * f["price"] for f in buy)
            sell_value = sum(f["quantity"] * f["price"] for f in sell)
            quantity = (
                current["quantity"] * (1 if current["direction"] == "BUY" else -1)
                if current
                else 0
            )
            average = current["entry_price"] if current else 0.0
            mark = self._prices.get(symbol, average)
            unrealised = quantity * (mark - average)
            gross = sell_value - buy_value + quantity * mark
            overnight = quantity - bought + sold
            rows.append(
                BrokerPosition(
                    key=self._key_for(symbol),
                    signed_quantity=quantity,
                    average_price=average,
                    last_price=mark,
                    realised_gross=gross - unrealised if not overnight else None,
                    unrealised_gross=unrealised,
                    pnl=gross if not overnight else None,
                    buy_quantity=bought,
                    sell_quantity=sold,
                    buy_price=buy_value / bought if bought else 0.0,
                    sell_price=sell_value / sold if sold else 0.0,
                    buy_value=buy_value,
                    sell_value=sell_value,
                    day_buy_quantity=bought,
                    day_sell_quantity=sold,
                    overnight_quantity=overnight,
                    mark_time=self._mark_times.get(symbol),
                )
            )
        return PositionSnapshot(
            net=tuple(rows),
            day=tuple(rows),
            quality=SnapshotQuality.COMPLETE,
            fetched_at=timestamp,
        )

    def broker_snapshot(self, timestamp: datetime) -> BrokerSnapshot:
        """One internally coherent canonical snapshot for shared risk admission."""
        positions = self.position_snapshot(timestamp)
        orders = self.order_snapshot(timestamp)
        fills = self.fill_snapshot(timestamp)
        return BrokerSnapshot(
            namespace=self.namespace,
            account_id=self.account_id,
            positions=positions.net,
            day_positions=positions.day,
            current_orders=orders.orders,
            fills=fills.fills,
            positions_quality=positions.quality,
            orders_quality=orders.quality,
            fills_quality=fills.quality,
            fetched_at=self._time(timestamp),
        )

    def order_snapshot(self, timestamp: datetime) -> OrderSnapshot:
        timestamp = self._time(timestamp)
        orders = []
        for item in self.orders.values():
            if item["submitted_at"] > timestamp or (
                self._session_date(item["submitted_at"])
                != self._session_date(timestamp)
                and item["status"] in {"COMPLETE", "CANCELLED", "REJECTED"}
            ):
                continue
            orders.append(
                BrokerOrder(
                    broker_order_id=item["order_id"],
                    key=self._key_for(item["symbol"]),
                    side=item["side"],
                    order_type=item["order_type"],
                    original_quantity=item["quantity"],
                    filled_quantity=item["filled_quantity"],
                    remaining_quantity=item["remaining_quantity"],
                    price=item["price"],
                    average_fill_price=item["average_price"],
                    trigger_price=item["trigger_price"],
                    status=item["status"],
                    variety="regular",
                    validity="DAY",
                    status_message=None,
                    client_intent_tag=item["tag"],
                    role=OrderRole(item["role"]),
                    exchange_time=item["updated_at"],
                    order_time=item["submitted_at"],
                    received_at=timestamp,
                )
            )
        return OrderSnapshot(
            orders=tuple(orders), quality=SnapshotQuality.COMPLETE, fetched_at=timestamp
        )

    def fill_snapshot(self, timestamp: datetime) -> FillSnapshot:
        timestamp = self._time(timestamp)
        return FillSnapshot(
            fills=tuple(
                BrokerFill(
                    broker_fill_id=item["fill_id"],
                    broker_order_id=item["order_id"],
                    key=self._key_for(item["symbol"]),
                    side=item["side"],
                    quantity=item["quantity"],
                    fill_price=item["price"],
                    exchange_time=item["exchange_time"],
                    received_at=item["received_at"],
                )
                for item in self.fills
                if item["exchange_time"] <= timestamp
                and self._session_date(item["exchange_time"])
                == self._session_date(timestamp)
            ),
            quality=SnapshotQuality.COMPLETE,
            fetched_at=timestamp,
        )

    def current_equity(self, current_prices: Dict[str, float]) -> float:
        equity = self.cash
        for symbol, position in self.positions.items():
            mark = current_prices.get(
                symbol, self._prices.get(symbol, position["entry_price"])
            )
            mark = self._finite_price(mark)
            signed_quantity = (
                position["quantity"]
                if position["direction"] == "BUY"
                else -position["quantity"]
            )
            equity += signed_quantity * mark
        return round(equity, 2)
