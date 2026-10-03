"""One versioned accounting projection for risk, journal, and research inputs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from itertools import groupby
from typing import Dict, Iterable, Optional, Union

from .broker_models import BrokerFill, BrokerOrder, BrokerPosition, BrokerPositionKey
from .trading_costs import TradingCostCalculator, cost_calculator

ACCOUNTING_POLICY_VERSION = "equity-mis-accounting-v1"
SUPPORTED_EXCHANGE = "NSE"
SUPPORTED_PRODUCT = "MIS"


class AccountingQuality(str, Enum):
    RECONCILED = "RECONCILED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FeeBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    gst: float
    stamp: float
    total: float
    cost_model_version: str
    rounding_version: str


@dataclass(frozen=True)
class SessionAccounting:
    realised_gross: Optional[float]
    unrealised_gross: Optional[float]
    incurred_fees: Optional[float]
    net_risk_pnl: Optional[float]
    quality: AccountingQuality
    policy_version: str = ACCOUNTING_POLICY_VERSION
    cost_model_version: str = "unknown"
    rounding_version: str = "unknown"


@dataclass(frozen=True)
class FillProjection:
    quantity: int
    vwap: Optional[float]
    turnover: Optional[float]
    fees: Optional[FeeBreakdown]
    quality: AccountingQuality


def _sum_optional(values: Iterable[Optional[float]]) -> Optional[float]:
    materialized = list(values)
    if any(value is None or not math.isfinite(value) for value in materialized):
        return None
    return sum(materialized)


class AccountingService:
    def __init__(self, calculator: TradingCostCalculator = cost_calculator):
        self.calculator = calculator

    def fees_for_fills(self, fills: Iterable[BrokerFill]) -> Optional[FeeBreakdown]:
        """Estimate fees once per broker order, not once per partial fill."""

        def order_identity(fill: BrokerFill) -> tuple[str, str, str]:
            return (
                fill.key.namespace.value,
                fill.key.account_id,
                fill.broker_order_id,
            )

        records = sorted(
            fills,
            key=lambda fill: (*order_identity(fill), fill.broker_fill_id),
        )
        if any(
            fill.key.exchange != SUPPORTED_EXCHANGE
            or fill.key.product != SUPPORTED_PRODUCT
            for fill in records
        ):
            return None
        totals = {
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "gst": 0.0,
            "stamp": 0.0,
            "total": 0.0,
        }
        for _, grouped in groupby(records, key=order_identity):
            order_fills = list(grouped)
            # Legacy fills may omit a token. The account's broker order ID
            # still owns one brokerage cap, provided all known identity facts
            # agree. Conflicts cannot be repaired by charging separate orders.
            identities = {
                (
                    fill.key.exchange,
                    fill.key.tradingsymbol,
                    fill.key.product,
                    fill.side,
                )
                for fill in order_fills
            }
            known_instruments = {
                fill.key.instrument_id
                for fill in order_fills
                if fill.key.instrument_id != fill.key.tradingsymbol
            }
            if len(identities) != 1 or len(known_instruments) > 1:
                return None
            turnover = _sum_optional(fill.turnover for fill in order_fills)
            if turnover is None:
                return None
            side = order_fills[0].side
            charges = self.calculator.calculate_turnover_charges(turnover, side)
            for key in totals:
                totals[key] += charges[key]

        return FeeBreakdown(
            brokerage=round(totals["brokerage"], 2),
            stt=round(totals["stt"], 2),
            exchange_txn=round(totals["exchange_txn"], 2),
            sebi=round(totals["sebi"], 2),
            gst=round(totals["gst"], 2),
            stamp=round(totals["stamp"], 2),
            total=round(totals["total"], 2),
            cost_model_version=self.calculator.rate_version,
            rounding_version=self.calculator.rounding_version,
        )

    def project_fills(self, fills: Iterable[BrokerFill]) -> FillProjection:
        records = list(fills)
        quantity = sum(fill.quantity for fill in records)
        turnover = _sum_optional(fill.turnover for fill in records)
        if quantity <= 0 or turnover is None:
            return FillProjection(
                quantity=quantity,
                vwap=None,
                turnover=turnover,
                fees=None,
                quality=AccountingQuality.UNAVAILABLE,
            )
        fees = self.fees_for_fills(records)
        return FillProjection(
            quantity=quantity,
            vwap=round(turnover / quantity, 4),
            turnover=round(turnover, 2),
            fees=fees,
            quality=(
                AccountingQuality.RECONCILED
                if fees is not None
                else AccountingQuality.UNAVAILABLE
            ),
        )

    def session_accounting(
        self,
        day_positions: Iterable[BrokerPosition],
        fills: Iterable[BrokerFill],
        *,
        orders: Optional[Iterable[BrokerOrder]] = None,
    ) -> SessionAccounting:
        positions = list(day_positions)
        records = list(fills)
        realised = _sum_optional(position.realised_gross for position in positions)
        unrealised = _sum_optional(position.unrealised_gross for position in positions)
        fees = self.fees_for_fills(records)
        coherent = self._session_executions_match(positions, records, orders)
        if realised is None or unrealised is None or fees is None or not coherent:
            return SessionAccounting(
                realised_gross=realised,
                unrealised_gross=unrealised,
                incurred_fees=fees.total if fees else None,
                net_risk_pnl=None,
                quality=AccountingQuality.UNAVAILABLE,
                cost_model_version=self.calculator.rate_version,
                rounding_version=self.calculator.rounding_version,
            )
        return SessionAccounting(
            realised_gross=round(realised, 2),
            unrealised_gross=round(unrealised, 2),
            incurred_fees=fees.total,
            net_risk_pnl=round(realised + unrealised - fees.total, 2),
            quality=AccountingQuality.RECONCILED,
            cost_model_version=self.calculator.rate_version,
            rounding_version=self.calculator.rounding_version,
        )

    @staticmethod
    def _session_executions_match(
        positions: list[BrokerPosition],
        fills: list[BrokerFill],
        orders: Optional[Iterable[BrokerOrder]],
    ) -> bool:
        """Do cumulative position/order quantities support this fee projection?

        A successfully parsed /trades response can lag /positions or /orders.
        Its fees remain known incurred costs, but cannot be called the complete
        session total until both sides of every execution are accounted for.
        """

        positions_by_key = {position.key: position for position in positions}
        if len(positions_by_key) != len(positions) or any(
            position.key.exchange != SUPPORTED_EXCHANGE
            or position.key.product != SUPPORTED_PRODUCT
            or position.multiplier != 1.0
            for position in positions
        ):
            return False

        def resolve_key(key: BrokerPositionKey) -> BrokerPositionKey:
            if key in positions_by_key or key.instrument_id != key.tradingsymbol:
                return key
            matches = [
                candidate
                for candidate in positions_by_key
                if candidate.namespace == key.namespace
                and candidate.account_id == key.account_id
                and candidate.exchange == key.exchange
                and candidate.tradingsymbol == key.tradingsymbol
                and candidate.product == key.product
            ]
            return matches[0] if len(matches) == 1 else key

        quantities = {}
        turnovers = {}
        fills_by_order = {}
        for fill in fills:
            key = resolve_key(fill.key)
            if key not in positions_by_key:
                return False
            sides = quantities.setdefault(key, {"BUY": 0, "SELL": 0})
            sides[fill.side] += fill.quantity
            values = turnovers.setdefault(key, {"BUY": 0.0, "SELL": 0.0})
            if fill.turnover is None:
                return False
            values[fill.side] += fill.turnover
            fills_by_order.setdefault(fill.broker_order_id, []).append(fill)

        for key, position in positions_by_key.items():
            sides = quantities.get(key, {"BUY": 0, "SELL": 0})
            if sides["BUY"] != max(
                position.day_buy_quantity, position.buy_quantity
            ) or sides["SELL"] != max(
                position.day_sell_quantity, position.sell_quantity
            ):
                return False
            if position.signed_quantity != (
                position.overnight_quantity + sides["BUY"] - sides["SELL"]
            ):
                return False
            values = turnovers.get(key, {"BUY": 0.0, "SELL": 0.0})
            for side, reported in (
                ("BUY", position.buy_value),
                ("SELL", position.sell_value),
            ):
                if reported is not None and not math.isclose(
                    reported, values[side], rel_tol=1e-9, abs_tol=0.02
                ):
                    return False

        for order in orders or ():
            order_fills = fills_by_order.get(order.broker_order_id, ())
            if sum(fill.quantity for fill in order_fills) != order.filled_quantity:
                return False
            if any(
                fill.side != order.side
                or resolve_key(fill.key) != resolve_key(order.key)
                for fill in order_fills
            ):
                return False
        return True

    def estimated_session_accounting_from_positions(
        self, day_positions: Iterable[BrokerPosition]
    ) -> SessionAccounting:
        """Fast fallback when a fresh fill book is temporarily unavailable.

        Position turnover is broker-reported, but order-level brokerage grouping
        is unavailable, so the result is explicitly ESTIMATED rather than being
        presented as fill-reconciled accounting.
        """

        positions = list(day_positions)
        realised = _sum_optional(position.realised_gross for position in positions)
        unrealised = _sum_optional(position.unrealised_gross for position in positions)
        if realised is None or unrealised is None:
            return SessionAccounting(
                realised_gross=realised,
                unrealised_gross=unrealised,
                incurred_fees=None,
                net_risk_pnl=None,
                quality=AccountingQuality.UNAVAILABLE,
                cost_model_version=self.calculator.rate_version,
                rounding_version=self.calculator.rounding_version,
            )

        fees = 0.0
        for position in positions:
            if (
                position.key.exchange != SUPPORTED_EXCHANGE
                or position.key.product != SUPPORTED_PRODUCT
                or position.multiplier != 1.0
            ):
                return SessionAccounting(
                    realised_gross=realised,
                    unrealised_gross=unrealised,
                    incurred_fees=None,
                    net_risk_pnl=None,
                    quality=AccountingQuality.UNAVAILABLE,
                    cost_model_version=self.calculator.rate_version,
                    rounding_version=self.calculator.rounding_version,
                )
            for turnover, side in (
                (position.buy_value, "BUY"),
                (position.sell_value, "SELL"),
            ):
                if turnover is None:
                    return SessionAccounting(
                        realised_gross=realised,
                        unrealised_gross=unrealised,
                        incurred_fees=None,
                        net_risk_pnl=None,
                        quality=AccountingQuality.UNAVAILABLE,
                        cost_model_version=self.calculator.rate_version,
                        rounding_version=self.calculator.rounding_version,
                    )
                if turnover > 0:
                    fees += self.calculator.calculate_turnover_charges(turnover, side)[
                        "total"
                    ]
        return SessionAccounting(
            realised_gross=round(realised, 2),
            unrealised_gross=round(unrealised, 2),
            incurred_fees=round(fees, 2),
            net_risk_pnl=round(realised + unrealised - fees, 2),
            quality=AccountingQuality.ESTIMATED,
            cost_model_version=self.calculator.rate_version,
            rounding_version=self.calculator.rounding_version,
        )

    def calculate_trade(
        self,
        *,
        direction: str,
        entry_price: Optional[float],
        exit_price: Optional[float],
        quantity: int,
        signal_entry_price: Optional[float] = None,
        signal_exit_price: Optional[float] = None,
        exchange: str = SUPPORTED_EXCHANGE,
        product: str = SUPPORTED_PRODUCT,
    ) -> Optional[Dict[str, Union[float, str]]]:
        if (
            entry_price is None
            or exit_price is None
            or isinstance(entry_price, bool)
            or isinstance(exit_price, bool)
            or isinstance(quantity, bool)
            or quantity <= 0
            or not math.isfinite(entry_price)
            or not math.isfinite(exit_price)
            or entry_price <= 0
            or exit_price <= 0
            or exchange != SUPPORTED_EXCHANGE
            or product != SUPPORTED_PRODUCT
        ):
            return None
        result = self.calculator.calculate_trade_charges(
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            signal_entry_price=signal_entry_price,
            signal_exit_price=signal_exit_price,
        )
        result["accounting_policy_version"] = ACCOUNTING_POLICY_VERSION
        result["cost_model_version"] = self.calculator.rate_version
        result["rounding_version"] = self.calculator.rounding_version
        return result


accounting_service = AccountingService()
