import datetime
import math
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from .accounting import (
    SUPPORTED_EXCHANGE,
    SUPPORTED_PRODUCT,
    AccountingQuality,
    accounting_service,
)
from .broker_models import (
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    OrderRole,
    PositionSnapshot,
    SnapshotQuality,
    normalize_position,
    utc_now,
)
from .config import config_manager
from .nifty_universe import get_sector
from .time_utils import as_utc


def get_ist_now():
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(ist)


@dataclass(frozen=True)
class EntryReservation:
    reservation_id: str
    symbol: str
    exchange: str
    product: str
    direction: str
    quantity: int
    reference_price: float
    baseline_signed_quantity: int
    created_at: datetime.datetime
    broker_order_id: Optional[str] = None
    namespace: str = "LIVE"
    account_id: str = "UNKNOWN"
    instrument_id: Optional[str] = None


@dataclass
class _ExposureState:
    gross: float
    net: float
    pending_buys: float
    pending_sells: float
    symbol_gross: Dict[str, float]
    sector_gross: Dict[str, float]
    occupied_keys: set
    marks: Dict[Tuple[str, ...], float]


class RiskManager:
    def __init__(self):
        self.win_count = 0
        self.loss_count = 0
        self.open_positions = 0

        self.daily_pnl = 0.0
        self.incurred_fees = 0.0
        self.accounting_quality = AccountingQuality.UNAVAILABLE.value
        self.accounting_policy_version = accounting_service.calculator.rate_version
        self.cost_model_version = accounting_service.calculator.rate_version
        self.rounding_version = accounting_service.calculator.rounding_version
        self.kill_switch_active = False
        self.reconciliation_status = "RECONCILIATION_PENDING"
        self.date_str = get_ist_now().strftime("%Y-%m-%d")

        self._correlation_cache: Dict[Tuple[str, str], Optional[float]] = {}
        self._last_corr_date = None
        self._admission_lock = threading.RLock()
        self._entry_reservations: Dict[str, EntryReservation] = {}

        self._load_state()

    def _load_state(self):
        state = config_manager.load_daily_risk_state()
        if state.get("date") == self.date_str:
            self.daily_pnl = float(state.get("daily_pnl", 0.0))
            self.incurred_fees = float(state.get("incurred_fees", 0.0))
            self.accounting_quality = state.get(
                "accounting_quality", AccountingQuality.UNAVAILABLE.value
            )
            self.accounting_policy_version = state.get(
                "accounting_policy_version", self.accounting_policy_version
            )
            self.cost_model_version = state.get(
                "cost_model_version", self.cost_model_version
            )
            self.rounding_version = state.get("rounding_version", self.rounding_version)
            self.kill_switch_active = bool(state.get("kill_switch_active", False))
            self.reconciliation_status = state.get(
                "reconciliation_status", "RECONCILIATION_PENDING"
            )
        else:
            self.daily_pnl = 0.0
            self.incurred_fees = 0.0
            self.accounting_quality = AccountingQuality.UNAVAILABLE.value
            self.kill_switch_active = False
            self.reconciliation_status = "RECONCILIATION_PENDING"
            self.accounting_policy_version = accounting_service.calculator.rate_version
            self.cost_model_version = accounting_service.calculator.rate_version
            self.rounding_version = accounting_service.calculator.rounding_version
            self._save_state()

    def _save_state(self):
        state = {
            "date": self.date_str,
            "daily_pnl": self.daily_pnl,
            "incurred_fees": self.incurred_fees,
            "accounting_quality": self.accounting_quality,
            "accounting_policy_version": self.accounting_policy_version,
            "cost_model_version": self.cost_model_version,
            "rounding_version": self.rounding_version,
            "kill_switch_active": self.kill_switch_active,
            "reconciliation_status": self.reconciliation_status,
        }
        config_manager.save_daily_risk_state(state)

    def _apply_accounting(self, accounting) -> bool:
        # Even an incomplete execution set establishes an incurred-cost floor.
        # Preserve it through subsequent missing/position-only observations.
        if accounting.incurred_fees is not None:
            self.incurred_fees = max(self.incurred_fees, accounting.incurred_fees)
        if accounting.net_risk_pnl is None:
            self.accounting_quality = AccountingQuality.UNAVAILABLE.value
            return False

        # A later position-only/degraded view is not allowed to make already
        # incurred, order-grouped costs disappear.  Recompute the net from the
        # gross components when we retain that higher fee floor.
        observed_fees = accounting.incurred_fees or 0.0
        fees = max(self.incurred_fees, observed_fees)
        if (
            accounting.realised_gross is not None
            and accounting.unrealised_gross is not None
        ):
            self.daily_pnl = round(
                accounting.realised_gross + accounting.unrealised_gross - fees, 2
            )
        else:
            self.daily_pnl = accounting.net_risk_pnl
        self.incurred_fees = fees
        self.accounting_quality = accounting.quality.value
        self.accounting_policy_version = accounting.policy_version
        self.cost_model_version = accounting.cost_model_version
        self.rounding_version = accounting.rounding_version
        config = config_manager.get_risk_config()
        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True
        return True

    def _apply_conservative_hard_loss(
        self,
        positions,
        fetched_at,
        *,
        positions_complete: bool,
    ) -> bool:
        """Latch hard loss from fresh gross position evidence.

        Fill-reconciled net accounting can be unavailable while the broker's
        position P&L is still fresh and its incurred-cost floor is known.  That
        evidence is sufficient to stop new risk, but it never gets promoted to
        ``RECONCILED`` accounting quality.
        """

        # A subset of position rows cannot establish the account aggregate.
        # It can contain only losers (or only winners), so use it to preserve an
        # existing latch but never to create a new account-loss conclusion.
        if self.kill_switch_active or not positions_complete or not positions:
            return False
        fetched_utc = as_utc(fetched_at)
        if fetched_utc is None:
            return False
        age = (utc_now() - fetched_utc).total_seconds()
        config = config_manager.get_risk_config()
        if age < -5 or age > int(config.get("brokerSnapshotMaxAgeSeconds", 120)):
            return False
        gross_values = []
        for position in positions:
            gross = None
            if (
                position.realised_gross is not None
                and position.unrealised_gross is not None
            ):
                gross = position.realised_gross + position.unrealised_gross
            elif position.pnl is not None:
                gross = position.pnl
            if gross is None or not math.isfinite(gross):
                return False
            gross_values.append(gross)

        estimated = accounting_service.estimated_session_accounting_from_positions(
            positions
        )
        incurred_costs = estimated.incurred_fees
        if math.isfinite(self.incurred_fees):
            incurred_costs = max(incurred_costs or 0.0, self.incurred_fees)
        if incurred_costs is None or not math.isfinite(incurred_costs):
            return False
        conservative_net = sum(gross_values) - incurred_costs
        if conservative_net <= -config["maxDailyLoss"]:
            # This is a latch: a later degraded/partial read must not clear it.
            self.kill_switch_active = True
            return True
        return False

    def reconcile_state(self):
        from .kite_client import kite_client
        from .utils import push_log

        current_date = get_ist_now().strftime("%Y-%m-%d")
        if current_date != self.date_str:
            self.date_str = current_date
            self.daily_pnl = 0.0
            self.incurred_fees = 0.0
            self.accounting_quality = AccountingQuality.UNAVAILABLE.value
            self.kill_switch_active = False
            self.reconciliation_status = "RECONCILIATION_PENDING"

        snapshot = kite_client.get_broker_snapshot()
        self._apply_conservative_hard_loss(
            snapshot.day_positions,
            snapshot.positions_fetched_at or snapshot.fetched_at,
            positions_complete=snapshot.positions_quality is SnapshotQuality.COMPLETE,
        )
        if not snapshot.entry_ready:
            self.reconciliation_status = "RECONCILIATION_FAILED"
            self._save_state()
            push_log(
                "RiskManager: broker reconciliation snapshot unavailable: "
                + "; ".join(snapshot.errors),
                level="error",
            )
            return

        accounting = accounting_service.session_accounting(
            snapshot.day_positions, snapshot.fills, orders=snapshot.current_orders
        )
        if not self._apply_accounting(accounting):
            self._apply_conservative_hard_loss(
                snapshot.day_positions,
                snapshot.positions_fetched_at or snapshot.fetched_at,
                positions_complete=True,
            )
            self.reconciliation_status = "RECONCILIATION_FAILED"
            self._save_state()
            push_log(
                "RiskManager: broker accounting is incomplete; new risk remains blocked",
                level="error",
            )
            return

        self.open_positions = sum(
            1 for position in snapshot.positions if position.signed_quantity != 0
        )
        self.reconciliation_status = "RECONCILED"
        if self.kill_switch_active:
            push_log(
                "RiskManager: Daily loss limit breached. Kill switch ACTIVATED.",
                level="error",
            )
        self._save_state()

    def can_trade(self) -> Tuple[bool, str]:
        if self.kill_switch_active:
            return False, "Kill switch is active due to daily loss limit breach"

        if self.reconciliation_status != "RECONCILED":
            return (
                False,
                f"Broker risk state is not reconciled ({self.reconciliation_status})",
            )

        config = config_manager.get_risk_config()
        now = get_ist_now().time()

        start_trade_after_str = config.get("startTradeAfter", "09:45")
        try:
            start_trade_after = datetime.datetime.strptime(
                start_trade_after_str, "%H:%M"
            ).time()
        except ValueError:
            start_trade_after = datetime.time(9, 15)

        market_open = max(datetime.time(9, 15), start_trade_after)
        if now < market_open:
            return False, f"Trading starts at {market_open.strftime('%H:%M')}"

        try:
            no_new_trades_after = datetime.datetime.strptime(
                config["noNewTradesAfter"], "%H:%M"
            ).time()
        except (KeyError, ValueError):
            return False, "Invalid noNewTradesAfter risk setting"
        if now >= no_new_trades_after:
            return False, "Time is past noNewTradesAfter limit"

        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True
            self._save_state()
            return False, f"Max daily loss ({-config['maxDailyLoss']}) exceeded"

        return True, "OK"

    def calculate_position_size(
        self, price: float, stop_loss: float, available_margin: float = None
    ) -> int:
        if (
            isinstance(price, bool)
            or isinstance(stop_loss, bool)
            or not isinstance(price, (int, float))
            or not isinstance(stop_loss, (int, float))
            or not math.isfinite(price)
            or not math.isfinite(stop_loss)
            or price <= 0
            or stop_loss <= 0
            or price == stop_loss
        ):
            return 0
        if available_margin is not None and (
            isinstance(available_margin, bool)
            or not isinstance(available_margin, (int, float))
            or not math.isfinite(available_margin)
        ):
            return 0

        config = config_manager.get_risk_config()
        max_capital = float(config.get("maxCapitalPerTrade", 10000))
        leverage = float(config.get("leverageMultiplier", 5))
        max_buying_power = max_capital * leverage

        if available_margin is not None:
            usable_margin = min(max_capital, max(0.0, available_margin))
            max_buying_power = usable_margin * leverage

        risk_per_trade = float(config.get("riskPerTrade", max_buying_power * 0.01))
        quantity_by_capital = int(max_buying_power / price)
        stop_distance = abs(price - stop_loss)
        quantity_by_risk = (
            int(risk_per_trade / stop_distance) if risk_per_trade > 0 else 0
        )
        quantity = min(quantity_by_capital, quantity_by_risk)
        if available_margin is not None:
            return max(0, quantity)
        return max(1, quantity)

    def cap_advisory_quantity(
        self,
        requested_quantity: int,
        price: float,
        available_margin: float,
        stop_loss: Optional[float] = None,
    ) -> int:
        if isinstance(requested_quantity, bool) or requested_quantity <= 0:
            return 0
        if stop_loss is not None:
            maximum = self.calculate_position_size(
                price, stop_loss, available_margin=available_margin
            )
        else:
            config = config_manager.get_risk_config()
            leverage = float(config.get("leverageMultiplier", 5))
            max_capital = float(config.get("maxCapitalPerTrade", 10000))
            usable_margin = min(max_capital, max(0.0, available_margin))
            maximum = int(usable_margin * leverage / price) if price > 0 else 0
        return min(requested_quantity, max(0, maximum))

    def check_daily_loss_limit(self) -> bool:
        if self.kill_switch_active:
            return True
        config = config_manager.get_risk_config()
        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True
            self._save_state()
            return True
        return False

    def should_square_off(self) -> bool:
        config = config_manager.get_risk_config()
        now = get_ist_now().time()
        max_loss_hit = self.check_daily_loss_limit()

        time_to_square_off = False
        if config.get("autoSquareOff", True):
            try:
                square_off_time = datetime.datetime.strptime(
                    config.get("squareOffTime", "15:15"), "%H:%M"
                ).time()
                time_to_square_off = now >= square_off_time and now <= datetime.time(
                    15, 30
                )
            except ValueError:
                pass

        if self.open_positions > 0:
            return time_to_square_off or max_loss_hit
        return False

    def update_from_position_snapshot(self, snapshot: PositionSnapshot):
        self._apply_conservative_hard_loss(
            snapshot.day,
            snapshot.fetched_at,
            positions_complete=snapshot.quality is SnapshotQuality.COMPLETE,
        )
        if snapshot.quality is not SnapshotQuality.COMPLETE:
            self.reconciliation_status = "RECONCILIATION_STALE"
            self.accounting_quality = AccountingQuality.UNAVAILABLE.value
            self._save_state()
            return

        accounting = accounting_service.estimated_session_accounting_from_positions(
            snapshot.day
        )
        # This is intentionally only a degraded fallback.  It is never the
        # primary calculation when cumulative executions are available.
        if (
            accounting.incurred_fees is not None
            and self.incurred_fees > accounting.incurred_fees
        ):
            fees = self.incurred_fees
            accounting = replace(
                accounting,
                incurred_fees=fees,
                net_risk_pnl=round(
                    (accounting.realised_gross or 0.0)
                    + (accounting.unrealised_gross or 0.0)
                    - fees,
                    2,
                ),
            )
        if not self._apply_accounting(accounting):
            self.reconciliation_status = "RECONCILIATION_STALE"
        self.open_positions = sum(
            1 for position in snapshot.net if position.signed_quantity != 0
        )
        self._save_state()

    def update_from_broker_snapshot(self, snapshot: BrokerSnapshot) -> None:
        """Refresh risk from one cumulative, order-grouped broker snapshot."""

        self._apply_conservative_hard_loss(
            snapshot.day_positions,
            snapshot.positions_fetched_at or snapshot.fetched_at,
            positions_complete=snapshot.positions_quality is SnapshotQuality.COMPLETE,
        )
        if not snapshot.entry_ready:
            self.reconciliation_status = "RECONCILIATION_STALE"
            self.accounting_quality = AccountingQuality.UNAVAILABLE.value
            self._save_state()
            return
        accounting = accounting_service.session_accounting(
            snapshot.day_positions, snapshot.fills, orders=snapshot.current_orders
        )
        if not self._apply_accounting(accounting):
            # Keep known cumulative fees as an explicit conservative floor, but
            # do not call the position-only estimate fully reconciled.
            self._apply_conservative_hard_loss(
                snapshot.day_positions,
                snapshot.positions_fetched_at or snapshot.fetched_at,
                positions_complete=True,
            )
            self.reconciliation_status = "RECONCILIATION_STALE"
            self._save_state()
            return
        self.open_positions = sum(
            position.signed_quantity != 0 for position in snapshot.positions
        )
        self.reconciliation_status = "RECONCILED"
        self._save_state()

    def update_from_positions(self, positions: list):
        """Compatibility wrapper; production callers should pass a typed snapshot."""

        normalized = []
        try:
            for raw in positions:
                if isinstance(raw, BrokerPosition):
                    normalized.append(raw)
                    continue
                payload = dict(raw)
                payload.setdefault("exchange", "NSE")
                payload.setdefault("product", "MIS")
                payload.setdefault("tradingsymbol", "UNKNOWN")
                normalized.append(normalize_position(payload))
        except Exception:
            self.reconciliation_status = "RECONCILIATION_STALE"
            self._save_state()
            return
        snapshot = PositionSnapshot(
            net=tuple(normalized),
            day=tuple(normalized),
            quality=SnapshotQuality.COMPLETE,
            fetched_at=utc_now(),
        )
        self.update_from_position_snapshot(snapshot)

    def set_open_positions(self, count: int):
        self.open_positions = count

    def get_risk_status(self) -> Dict[str, Any]:
        return {
            "daily_pnl": self.daily_pnl,
            "incurred_fees": self.incurred_fees,
            "accounting_quality": self.accounting_quality,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "open_positions": self.open_positions,
            "pending_entry_reservations": self.pending_entry_reservation_count,
            "can_trade": self.can_trade()[0],
            "kill_switch_active": self.kill_switch_active,
            "reconciliation_status": self.reconciliation_status,
        }

    def _get_correlation(self, symbol_a: str, symbol_b: str) -> Optional[float]:
        today = get_ist_now().date()
        if self._last_corr_date != today:
            self._correlation_cache.clear()
            self._last_corr_date = today

        pair_key = tuple(sorted([symbol_a, symbol_b]))
        if pair_key in self._correlation_cache:
            return self._correlation_cache[pair_key]

        from .kite_client import kite_client

        config = config_manager.get_risk_config()
        lookback_days = int(config.get("correlationLookbackDays", 30))
        min_samples = int(config.get("correlationMinSamples", 5))

        try:
            now = utc_now()
            from_date = now - datetime.timedelta(days=lookback_days + 15)
            instruments = kite_client.get_instruments("NSE")
            token_by_symbol = {
                item["tradingsymbol"]: item["instrument_token"]
                for item in instruments
                if item.get("tradingsymbol") in {symbol_a, symbol_b}
            }
            token_a = token_by_symbol.get(symbol_a)
            token_b = token_by_symbol.get(symbol_b)
            if not token_a or not token_b:
                return None

            hist_a = kite_client.get_historical_data(token_a, from_date, now, "day")
            hist_b = kite_client.get_historical_data(token_b, from_date, now, "day")
            if not hist_a or not hist_b:
                return None

            df_a = pd.DataFrame(hist_a)[["date", "close"]].set_index("date")
            df_b = pd.DataFrame(hist_b)[["date", "close"]].set_index("date")
            joined = df_a.join(df_b, lsuffix="_a", rsuffix="_b", how="inner")
            joined = joined.sort_index().tail(lookback_days + 1)
            returns = joined[["close_a", "close_b"]].pct_change().dropna()
            if len(returns) < min_samples:
                return None

            corr = returns["close_a"].corr(returns["close_b"])
            if pd.isna(corr) or not math.isfinite(float(corr)):
                return None
            value = float(corr)
            self._correlation_cache[pair_key] = value
            return value
        except Exception as exc:
            from .utils import push_log

            push_log(
                f"RiskManager: Error computing correlation between "
                f"{symbol_a} and {symbol_b}: {exc}",
                level="warning",
            )
            return None

    @staticmethod
    def _add_exposure(state: _ExposureState, symbol: str, signed_value: float) -> None:
        gross_value = abs(signed_value)
        state.gross += gross_value
        state.net += signed_value
        state.symbol_gross[symbol] = state.symbol_gross.get(symbol, 0.0) + gross_value
        sector = get_sector(symbol)
        state.sector_gross[sector] = state.sector_gross.get(sector, 0.0) + gross_value

    def _position_quantity(
        self,
        positions: Tuple[BrokerPosition, ...],
        reservation: EntryReservation,
    ) -> int:
        return sum(
            position.signed_quantity
            for position in positions
            if position.key.tradingsymbol == reservation.symbol
            and position.key.exchange == reservation.exchange
            and position.key.product == reservation.product
            and (
                reservation.instrument_id is None
                or position.key.instrument_id == reservation.instrument_id
            )
            and (
                reservation.account_id in {"", "UNKNOWN"}
                or position.key.account_id == reservation.account_id
            )
            and position.key.namespace.value == reservation.namespace
        )

    def _reservation_outstanding_quantity(
        self,
        reservation: EntryReservation,
        positions: Tuple[BrokerPosition, ...],
        orders_by_id: Dict[str, BrokerOrder],
    ) -> int:
        direction_sign = 1 if reservation.direction == "BUY" else -1
        current_quantity = self._position_quantity(positions, reservation)
        reflected = max(
            0,
            direction_sign * (current_quantity - reservation.baseline_signed_quantity),
        )
        expected_quantity = reservation.quantity
        order = (
            orders_by_id.get(reservation.broker_order_id)
            if reservation.broker_order_id
            else None
        )
        if order and order.status in {
            "COMPLETE",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "REJECTED AMO",
        }:
            expected_quantity = order.filled_quantity
        return max(0, expected_quantity - reflected)

    def _build_exposure_state(
        self,
        broker_snapshot,
    ) -> Tuple[Optional[_ExposureState], str]:
        if not broker_snapshot.entry_ready:
            return None, "BROKER_STATE_UNAVAILABLE"

        state = _ExposureState(0.0, 0.0, 0.0, 0.0, {}, {}, set(), {})
        position_records = {}
        position_quantities = {}
        identity_keys = {}
        for position in broker_snapshot.positions:
            if position.signed_quantity and (
                position.key.exchange != SUPPORTED_EXCHANGE
                or position.key.product != SUPPORTED_PRODUCT
                or position.multiplier != 1.0
            ):
                return None, f"UNSUPPORTED_EXPOSURE: {position.key.tradingsymbol}"
            identity = self._position_identity(position)
            position_records[identity] = position
            position_quantities[identity] = position.signed_quantity
            identity_keys[identity] = position.key

        orders_by_id = {
            order.broker_order_id: order for order in broker_snapshot.current_orders
        }

        # HTTP receipt time is not an execution watermark: a fill can happen
        # while the positions response is in transit, even with the same broker
        # timestamp second.  Reconcile fills against broker-reported day
        # quantities instead.  Any signed execution not demonstrably included
        # in that quantity is conservatively added to residual exposure.
        fills_by_identity = {}
        for fill in broker_snapshot.fills:
            if (
                fill.key.exchange != SUPPORTED_EXCHANGE
                or fill.key.product != SUPPORTED_PRODUCT
            ):
                return None, f"UNSUPPORTED_EXPOSURE: {fill.key.tradingsymbol}"
            identity = self._fill_identity(fill, position_records)
            values = fills_by_identity.setdefault(
                identity,
                {
                    "BUY": 0,
                    "SELL": 0,
                },
            )
            values[fill.side] += fill.quantity
            order = orders_by_id.get(fill.broker_order_id)
            if order and (
                fill.side != order.side
                or identity != self._fill_identity(order, position_records)
            ):
                return None, f"ORDER_FILL_IDENTITY_CONFLICT: {fill.broker_order_id}"
            identity_keys.setdefault(identity, fill.key)

        # The order book can confirm executions before /trades exposes them.
        # Include that quantity for every status, then apply the same broker
        # day-quantity watermark used for fills (never add it a second time).
        for order in broker_snapshot.current_orders:
            if (order.is_working or order.filled_quantity) and (
                order.key.exchange != SUPPORTED_EXCHANGE
                or order.key.product != SUPPORTED_PRODUCT
            ):
                return None, f"UNSUPPORTED_EXPOSURE: {order.key.tradingsymbol}"
            observed = sum(
                fill.quantity
                for fill in broker_snapshot.fills
                if fill.broker_order_id == order.broker_order_id
            )
            if observed > order.filled_quantity:
                return None, f"ORDER_FILL_QUANTITY_CONFLICT: {order.broker_order_id}"
            if order.is_working and (
                order.filled_quantity + order.remaining_quantity
                != order.original_quantity
            ):
                return None, f"ORDER_QUANTITY_CONFLICT: {order.broker_order_id}"
            missing = order.filled_quantity - observed
            if missing:
                identity = self._fill_identity(order, position_records)
                values = fills_by_identity.setdefault(identity, {"BUY": 0, "SELL": 0})
                values[order.side] += missing
                identity_keys.setdefault(identity, order.key)

        for identity, values in fills_by_identity.items():
            position = position_records.get(identity)
            reflected_buys = position.day_buy_quantity if position else 0
            reflected_sells = position.day_sell_quantity if position else 0
            unreflected_buys = max(0, values["BUY"] - reflected_buys)
            unreflected_sells = max(0, values["SELL"] - reflected_sells)
            signed_delta = unreflected_buys - unreflected_sells
            if signed_delta == 0:
                continue
            position_quantities[identity] = position_quantities.get(identity, 0) + (
                signed_delta
            )

        for identity, quantity in position_quantities.items():
            if quantity == 0:
                continue
            position = position_records.get(identity)
            key = position.key if position else identity_keys[identity]
            # Reconciliation can turn an absent/flat row into actual exposure.
            # Execution/limit prices are not current marks. Require the same
            # timestamped market observation as an originally nonzero row;
            # otherwise wait for a fresh coherent broker snapshot.
            mark = position.last_price if position else None
            mark_time = position.mark_time if position else None
            if (
                isinstance(mark, bool)
                or mark is None
                or not math.isfinite(mark)
                or mark <= 0
                or mark_time is None
                or not -5
                <= (utc_now() - as_utc(mark_time)).total_seconds()
                <= broker_snapshot.max_age_seconds
            ):
                return None, f"MARK_UNAVAILABLE: {key.tradingsymbol}"
            multiplier = position.multiplier
            if multiplier != 1.0:
                return None, f"UNSUPPORTED_EXPOSURE: {key.tradingsymbol}"
            signed_value = quantity * mark * multiplier
            self._add_exposure(state, key.tradingsymbol, signed_value)
            state.occupied_keys.add(identity)
            state.marks[identity] = mark

        reducing_allowance = {
            (identity, "SELL" if quantity > 0 else "BUY"): abs(quantity)
            for identity, quantity in position_quantities.items()
            if quantity
        }
        for order in broker_snapshot.current_orders:
            if order.role is OrderRole.UNKNOWN and order.is_working:
                return None, f"UNKNOWN_WORKING_ORDER_ROLE: {order.broker_order_id}"
            if (
                order.status
                in {
                    "REJECTED",
                    "CANCELLED",
                    "EXPIRED",
                    "REJECTED AMO",
                }
                and order.filled_quantity <= 0
            ):
                continue
            additional_quantity = order.remaining_quantity if order.is_working else 0
            if additional_quantity <= 0:
                continue
            # Opposite-side reducers consume shared residual capacity and no
            # new gross. They still change possible net exposure. Excess
            # reducers block admission; wrong-side orders add directional risk.
            order_identity = self._fill_identity(order, position_records)
            allowance_key = (order_identity, order.side)
            residual = position_quantities.get(order_identity, 0)
            if order.role in {OrderRole.PROTECTION, OrderRole.REDUCTION} and (
                (residual > 0 and order.side == "SELL")
                or (residual < 0 and order.side == "BUY")
            ):
                allowance = reducing_allowance.get(allowance_key, 0)
                if additional_quantity > allowance:
                    # Concurrent reducers can reverse the position. Suspend
                    # admission until their aggregate quantity is reconciled.
                    return None, f"CONFLICTING_REDUCERS: {order.key.tradingsymbol}"
                reducing_allowance[allowance_key] = allowance - additional_quantity
                # No new gross, but the hedge can disappear independently of
                # another entry filling. Value that net change at the current
                # position mark, not the reducer's trigger/limit proceeds.
                position = position_records[order_identity]
                reduction_value = (
                    additional_quantity
                    * state.marks[order_identity]
                    * position.multiplier
                )
                if order.side == "BUY":
                    state.pending_buys += reduction_value
                else:
                    state.pending_sells += reduction_value
                continue
            reference_price = order.risk_reference_price or state.marks.get(
                order_identity
            )
            if reference_price is None:
                return None, f"ORDER_PRICE_UNAVAILABLE: {order.broker_order_id}"
            signed_value = additional_quantity * reference_price
            if order.side == "SELL":
                signed_value = -signed_value
            self._add_exposure(state, order.key.tradingsymbol, signed_value)
            state.net -= signed_value
            if order.side == "BUY":
                state.pending_buys += abs(signed_value)
            else:
                state.pending_sells += abs(signed_value)
            state.occupied_keys.add(order_identity)

        for reservation in self._entry_reservations.values():
            if reservation.broker_order_id in orders_by_id:
                continue
            outstanding = self._reservation_outstanding_quantity(
                reservation, broker_snapshot.positions, orders_by_id
            )
            if outstanding <= 0:
                continue
            signed_value = outstanding * reservation.reference_price
            if reservation.direction == "SELL":
                signed_value = -signed_value
            self._add_exposure(state, reservation.symbol, signed_value)
            state.net -= signed_value
            if reservation.direction == "BUY":
                state.pending_buys += abs(signed_value)
            else:
                state.pending_sells += abs(signed_value)
            state.occupied_keys.add(
                (
                    reservation.namespace,
                    reservation.account_id,
                    reservation.exchange,
                    reservation.instrument_id or reservation.symbol,
                    reservation.symbol,
                    reservation.product,
                )
            )

        return state, "OK"

    @staticmethod
    def _position_identity(position: BrokerPosition) -> tuple[str, ...]:
        return (
            position.key.namespace.value,
            position.key.account_id,
            position.key.exchange,
            position.key.instrument_id,
            position.key.tradingsymbol,
            position.key.product,
        )

    @staticmethod
    def _position_identity_from_key(key) -> tuple[str, ...]:
        return (
            key.namespace.value,
            key.account_id,
            key.exchange,
            key.instrument_id,
            key.tradingsymbol,
            key.product,
        )

    @classmethod
    def _fill_identity(cls, fill, positions_by_identity) -> tuple[str, ...]:
        """Use a matching position identity when a legacy fill omits its token."""

        identity = cls._position_identity_from_key(fill.key)
        if identity in positions_by_identity:
            return identity
        if fill.key.instrument_id != fill.key.tradingsymbol:
            return identity
        matches = [
            candidate
            for candidate in positions_by_identity
            if candidate[0] == identity[0]
            and candidate[1] == identity[1]
            and candidate[2] == identity[2]
            and candidate[4] == identity[4]
            and candidate[5] == identity[5]
        ]
        return matches[0] if len(matches) == 1 else identity

    def _evaluate_position_admission(
        self,
        symbol: str,
        direction: str,
        qty: int,
        price: float,
        exchange: str,
        product: str,
        broker_snapshot,
    ) -> Tuple[bool, str]:
        if self.kill_switch_active:
            return False, "DAILY_LOSS_LIMIT"
        if self.reconciliation_status != "RECONCILED":
            return False, f"RECONCILIATION_REQUIRED: {self.reconciliation_status}"
        if exchange != SUPPORTED_EXCHANGE or product != SUPPORTED_PRODUCT:
            return False, "UNSUPPORTED_PRODUCT_OR_EXCHANGE"
        if direction not in {"BUY", "SELL"}:
            return False, "INVALID_DIRECTION"
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            return False, "INVALID_QUANTITY"
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            return False, "INVALID_PRICE"

        state, reason = self._build_exposure_state(broker_snapshot)
        if state is None:
            return False, reason

        if any(
            key[2] == exchange and key[4] == symbol and key[5] == product
            for key in state.occupied_keys
            if len(key) == 6
        ):
            return False, f"DUPLICATE_POSITION_OR_ENTRY: {symbol}"

        config = config_manager.get_risk_config()
        from .journal import journal

        counts = journal.get_todays_trade_counts()
        journal_order_ids = set(counts.get("entry_order_ids", ()))
        local_order_ids = {
            reservation.broker_order_id
            for reservation in self._entry_reservations.values()
            if reservation.broker_order_id
        }
        terminal = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED", "REJECTED AMO"}
        broker_pending_entries = [
            order
            for order in broker_snapshot.current_orders
            if order.role is OrderRole.ENTRY
            and (
                order.is_working
                or (order.status in terminal and order.filled_quantity > 0)
            )
            and order.broker_order_id not in local_order_ids
            and order.broker_order_id not in journal_order_ids
        ]
        pending_by_symbol = {}
        for order in broker_pending_entries:
            pending_by_symbol[order.key.tradingsymbol] = (
                pending_by_symbol.get(order.key.tradingsymbol, 0) + 1
            )
        if counts["total"] + self._reserved_count_slots() + len(
            broker_pending_entries
        ) >= int(config.get("maxDailyTrades", 10)):
            return False, "MAX_DAILY_TRADES_LIMIT"
        symbol_reservations = sum(
            reservation.symbol == symbol
            for reservation in self._entry_reservations.values()
        )
        if counts["by_symbol"].get(
            symbol, 0
        ) + symbol_reservations + pending_by_symbol.get(symbol, 0) >= int(
            config.get("maxTradesPerSymbolPerDay", 2)
        ):
            return False, f"MAX_SYMBOL_TRADES_LIMIT: {symbol}"
        max_positions = int(config.get("maxSimultaneousPositions", 5))
        if len(state.occupied_keys) + 1 > max_positions:
            return False, f"MAX_POSITIONS_LIMIT: {max_positions}"

        max_gross = float(config.get("maxGrossExposure", 200000))
        max_net = float(config.get("maxNetExposure", 100000))
        max_single = float(config.get("maxSingleSymbolExposure", 50000))
        max_sector = float(config.get("maxSectorExposure", 75000))
        max_corr_exposure = float(config.get("maxCorrelatedExposure", 75000))
        corr_threshold = float(config.get("correlationThreshold", 0.70))

        proposed_value = qty * price
        proposed_signed = proposed_value if direction == "BUY" else -proposed_value
        if state.gross + proposed_value > max_gross:
            return False, (
                f"GROSS_EXPOSURE_LIMIT: {state.gross + proposed_value:.2f} "
                f"> {max_gross}"
            )
        pending_net_values = (
            state.net,
            state.net + state.pending_buys,
            state.net - state.pending_sells,
            state.net + state.pending_buys - state.pending_sells,
        )
        # The proposal may not fill while an existing order does.  Include
        # both outcomes instead of adding the proposed signed value to every
        # branch; this catches an already-over-capacity pending-fill state.
        possible_net_values = pending_net_values + tuple(
            value + proposed_signed for value in pending_net_values
        )
        worst_net = max(abs(value) for value in possible_net_values)
        if worst_net > max_net:
            return False, (
                f"NET_EXPOSURE_LIMIT: worst-case abs({worst_net:.2f}) > {max_net}"
            )
        if state.symbol_gross.get(symbol, 0.0) + proposed_value > max_single:
            return False, f"SINGLE_SYMBOL_LIMIT: {symbol} would exceed {max_single}"

        proposed_sector = get_sector(symbol)
        if state.sector_gross.get(proposed_sector, 0.0) + proposed_value > max_sector:
            return False, (
                f"SECTOR_EXPOSURE_LIMIT: {proposed_sector} would exceed {max_sector}"
            )

        correlated_exposure = proposed_value
        correlated_symbols = [symbol]
        for active_symbol, exposure in state.symbol_gross.items():
            if active_symbol == symbol:
                continue
            corr = self._get_correlation(symbol, active_symbol)
            if corr is None:
                return False, f"CORRELATION_UNAVAILABLE: {symbol}/{active_symbol}"
            if abs(corr) >= corr_threshold:
                correlated_exposure += exposure
                correlated_symbols.append(active_symbol)
        if correlated_exposure > max_corr_exposure:
            return False, (
                f"CORRELATED_EXPOSURE_LIMIT: {correlated_symbols} exposure "
                f"{correlated_exposure:.2f} > {max_corr_exposure}"
            )
        return True, "OK"

    def can_accept_position(
        self,
        symbol: str,
        direction: str,
        qty: int,
        price: float,
        broker_snapshot: BrokerSnapshot,
        exchange: str = "NSE",
        product: str = "MIS",
    ) -> Tuple[bool, str]:
        with self._admission_lock:
            return self._evaluate_position_admission(
                symbol=symbol,
                direction=direction,
                qty=qty,
                price=price,
                exchange=exchange,
                product=product,
                broker_snapshot=broker_snapshot,
            )

    def reserve_entry(
        self,
        symbol: str,
        direction: str,
        qty: int,
        price: float,
        broker_snapshot: BrokerSnapshot,
        exchange: str = "NSE",
        product: str = "MIS",
        instrument_id: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        with self._admission_lock:
            # The snapshot used for capacity can reveal losses or executions
            # newer than the monitor's last poll. Reconcile its daily controls
            # before committing a new reservation against that same snapshot.
            self.update_from_broker_snapshot(broker_snapshot)
            accepted, reason = self._evaluate_position_admission(
                symbol=symbol,
                direction=direction,
                qty=qty,
                price=price,
                exchange=exchange,
                product=product,
                broker_snapshot=broker_snapshot,
            )
            if not accepted:
                return None, reason
            baseline = sum(
                position.signed_quantity
                for position in broker_snapshot.positions
                if position.key.tradingsymbol == symbol
                and position.key.exchange == exchange
                and position.key.product == product
            )
            reservation_id = str(uuid.uuid4())
            self._entry_reservations[reservation_id] = EntryReservation(
                reservation_id=reservation_id,
                symbol=symbol,
                exchange=exchange,
                product=product,
                direction=direction,
                quantity=qty,
                reference_price=price,
                baseline_signed_quantity=baseline,
                created_at=utc_now(),
                namespace=broker_snapshot.namespace.value,
                account_id=broker_snapshot.account_id,
                instrument_id=instrument_id,
            )
            return reservation_id, "OK"

    def bind_entry_order(self, reservation_id: str, broker_order_id: str) -> bool:
        with self._admission_lock:
            reservation = self._entry_reservations.get(reservation_id)
            if not reservation:
                return False
            self._entry_reservations[reservation_id] = replace(
                reservation,
                broker_order_id=str(broker_order_id),
            )
            return True

    def restore_entry_reservation(self, symbol: str, trade: dict) -> None:
        """Restore a persisted pending admission even if its order is invisible.

        Current broker orders normally restore count capacity on their own;
        absent/unknown submissions also need their original local owner. The
        engine calls this only for a record with verified canonical identity.
        """
        reservation_id = trade["reservation_id"]
        with self._admission_lock:
            if reservation_id in self._entry_reservations:
                return
            price = (
                trade.get("reservation_price")
                or trade.get("signal_entry_price")
                or trade.get("entry_price")
            )
            quantity = trade.get("requested_quantity", trade.get("quantity"))
            if (
                not isinstance(price, (int, float))
                or not math.isfinite(price)
                or price <= 0
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                self.reconciliation_status = "RECONCILIATION_PENDING"
                return
            self._entry_reservations[reservation_id] = EntryReservation(
                reservation_id=reservation_id,
                symbol=symbol,
                exchange=trade["exchange"],
                product=trade["product"],
                direction=trade["direction"],
                quantity=quantity,
                reference_price=price,
                baseline_signed_quantity=trade.get("baseline_signed_quantity", 0),
                created_at=as_utc(trade.get("entry_time")) or utc_now(),
                broker_order_id=trade.get("entry_order_id"),
                namespace=trade["namespace"],
                account_id=trade["account_id"],
                instrument_id=str(trade["instrument_id"]),
            )

    def release_entry_reservation(self, reservation_id: Optional[str]) -> None:
        if not reservation_id:
            return
        with self._admission_lock:
            self._entry_reservations.pop(reservation_id, None)

    def reconcile_entry_reservations(self, broker_snapshot: BrokerSnapshot) -> None:
        """Release only exposure from verified zero-fill termination.

        A completed or partially filled entry still owns its daily/per-symbol
        count slot until the journal insertion consumes it exactly once.
        """

        terminal = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED", "REJECTED AMO"}
        with self._admission_lock:
            orders = {
                order.broker_order_id: order for order in broker_snapshot.current_orders
            }
            for reservation_id, reservation in list(self._entry_reservations.items()):
                if not reservation.broker_order_id:
                    continue
                order = orders.get(reservation.broker_order_id)
                if order and order.status in terminal and order.filled_quantity == 0:
                    self._entry_reservations.pop(reservation_id, None)

    def complete_entry_reservation(self, reservation_id: Optional[str]) -> None:
        """Consume capacity once the entry is journaled/owned."""

        self.release_entry_reservation(reservation_id)

    def _reserved_count_slots(self) -> int:
        with self._admission_lock:
            return len(self._entry_reservations)

    def validate_entry_reservation(
        self,
        reservation_id: str,
        symbol: str,
        direction: str,
        quantity: int,
        price: float,
    ) -> bool:
        with self._admission_lock:
            reservation = self._entry_reservations.get(reservation_id)
            return bool(
                reservation
                and reservation.symbol == symbol
                and reservation.direction == str(direction).upper()
                and reservation.quantity == quantity
                and math.isclose(reservation.reference_price, price, abs_tol=0.01)
            )

    @property
    def pending_entry_reservation_count(self) -> int:
        with self._admission_lock:
            return len(self._entry_reservations)


risk_manager = RiskManager()
