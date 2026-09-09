import datetime
from typing import Any, Dict, Tuple

import pandas as pd

from .config import config_manager
from .nifty_universe import get_sector


def get_ist_now():
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(ist)


class RiskManager:
    def __init__(self):
        self.win_count = 0
        self.loss_count = 0
        self.open_positions = 0

        self.daily_pnl = 0.0
        self.kill_switch_active = False
        self.reconciliation_status = "RECONCILIATION_PENDING"
        self.date_str = get_ist_now().strftime("%Y-%m-%d")

        self._correlation_cache = {}
        self._last_corr_date = None

        self._load_state()

    def _load_state(self):
        state = config_manager.load_daily_risk_state()
        if state.get("date") == self.date_str:
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.kill_switch_active = state.get("kill_switch_active", False)
            self.reconciliation_status = state.get(
                "reconciliation_status", "RECONCILIATION_PENDING"
            )
        else:
            self.daily_pnl = 0.0
            self.kill_switch_active = False
            self.reconciliation_status = "RECONCILIATION_PENDING"
            self._save_state()

    def _save_state(self):
        state = {
            "date": self.date_str,
            "daily_pnl": self.daily_pnl,
            "kill_switch_active": self.kill_switch_active,
            "reconciliation_status": self.reconciliation_status,
        }
        config_manager.save_daily_risk_state(state)

    def reconcile_state(self):
        from .kite_client import kite_client
        from .utils import push_log

        current_date = get_ist_now().strftime("%Y-%m-%d")
        if current_date != self.date_str:
            self.date_str = current_date
            self.daily_pnl = 0.0
            self.kill_switch_active = False
            self.reconciliation_status = "RECONCILIATION_PENDING"

        try:
            positions_res = kite_client.get_positions()
            positions = positions_res.get("day", [])
            trades = kite_client.get_trades()
        except Exception as e:
            self.reconciliation_status = "RECONCILIATION_FAILED"
            self._save_state()
            push_log(
                f"RiskManager: Reconciliation failed to fetch broker data: {e}",
                level="error",
            )
            return

        realized_gross = sum(p.get("realised", 0.0) for p in positions)
        unrealized = sum(p.get("unrealised", 0.0) for p in positions)

        from .trading_costs import cost_calculator

        estimated_charges = 0.0
        for t in trades:
            estimated_charges += cost_calculator.calculate_leg_charges(
                float(t.get("average_price", 0)),
                int(t.get("quantity", 0)),
                t.get("transaction_type", "BUY"),
            )["total"]
        net_realized = realized_gross - estimated_charges
        total_intraday = net_realized + unrealized

        drift = total_intraday - self.daily_pnl
        if abs(drift) > 0.01:
            push_log(
                f"RiskManager: Correcting P&L drift. Broker: {total_intraday:.2f}, Local: {self.daily_pnl:.2f}, Drift: {drift:.2f}",
                level="warning",
            )

        self.daily_pnl = total_intraday
        self.reconciliation_status = "RECONCILED"

        config = config_manager.get_risk_config()
        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True
            push_log(
                "RiskManager: Daily loss limit breached. Kill switch ACTIVATED.",
                level="error",
            )

        self._save_state()

    def can_trade(self) -> Tuple[bool, str]:
        if self.kill_switch_active:
            return False, "Kill switch is active due to daily loss limit breach"

        if self.reconciliation_status == "RECONCILIATION_FAILED":
            return False, "Reconciliation with broker failed"

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

        no_new_trades_after = datetime.datetime.strptime(
            config["noNewTradesAfter"], "%H:%M"
        ).time()
        if now >= no_new_trades_after:
            return False, "Time is past noNewTradesAfter limit"

        if self.open_positions >= config["maxSimultaneousPositions"]:
            return (
                False,
                f"Max simultaneous positions ({config['maxSimultaneousPositions']}) reached",
            )

        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True
            self._save_state()
            return False, f"Max daily loss ({-config['maxDailyLoss']}) exceeded"

        return True, "OK"

    def calculate_position_size(
        self, price: float, stop_loss: float, available_margin: float = None
    ) -> int:
        config = config_manager.get_risk_config()
        max_capital = config.get("maxCapitalPerTrade", 10000)
        leverage = config.get("leverageMultiplier", 5)

        max_buying_power = max_capital * leverage

        if available_margin is not None:
            usable_margin = min(max_capital, max(0, available_margin))
            max_buying_power = usable_margin * leverage

        risk_per_trade = config.get("riskPerTrade", max_buying_power * 0.01)

        quantity_by_capital = int(max_buying_power / price) if price > 0 else 0
        stop_distance = abs(price - stop_loss)

        if stop_distance > 0 and risk_per_trade > 0:
            quantity_by_risk = int(risk_per_trade / stop_distance)
            if quantity_by_capital > 0:
                quantity = min(quantity_by_capital, quantity_by_risk)
            elif available_margin is not None:
                quantity = 0
            else:
                quantity = quantity_by_risk
        else:
            quantity = quantity_by_capital
        if available_margin is not None:
            return max(0, quantity)
        return max(1, quantity)

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
        now = datetime.datetime.now().time()

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

    def update_from_positions(self, positions: list):
        # A fast, lightweight update without fetching trades.
        # We use the previous estimated charges (implied by current daily_pnl difference, or we can just fetch it?
        # Actually, let's just do a proper reconcile without trades. Wait, we can't get trades.
        # Let's just track estimated_charges in the class)

        realized_gross = sum(p.get("realised", 0.0) for p in positions)
        unrealized = sum(p.get("unrealised", 0.0) for p in positions)

        # We can approximate charges using actual position turnover
        estimated_charges = 0.0
        for p in positions:
            buy_val = p.get("buy_value", 0.0)
            sell_val = p.get("sell_value", 0.0)
            # Rough estimate: ~0.04% of total turnover, but bounded by typical flat ₹40 max brokerage + ~0.03% STT/Txn
            turnover = buy_val + sell_val
            # Brokerage is max 40 per symbol round trip, plus variable taxes ~0.03% of turnover
            estimated_charges += min(turnover * 0.0003, 40.0) + (turnover * 0.0003)

        net_realized = realized_gross - estimated_charges
        total_intraday = net_realized + unrealized

        self.daily_pnl = total_intraday

        config = config_manager.get_risk_config()
        if self.daily_pnl <= -config["maxDailyLoss"]:
            self.kill_switch_active = True

        self._save_state()

    def set_open_positions(self, count: int):
        self.open_positions = count

    def get_risk_status(self) -> Dict[str, Any]:
        return {
            "daily_pnl": self.daily_pnl,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "open_positions": self.open_positions,
            "can_trade": self.can_trade()[0],
            "kill_switch_active": self.kill_switch_active,
            "reconciliation_status": self.reconciliation_status,
        }

    def _get_correlation(self, symbol_a: str, symbol_b: str) -> float:
        today = get_ist_now().date()
        if self._last_corr_date != today:
            self._correlation_cache.clear()
            self._last_corr_date = today

        pair_key = tuple(sorted([symbol_a, symbol_b]))
        if pair_key in self._correlation_cache:
            return self._correlation_cache[pair_key]

        from .kite_client import kite_client

        config = config_manager.get_risk_config()
        lookback_days = config.get("correlationLookbackDays", 30)

        try:
            now = datetime.datetime.now()
            from_date = now - datetime.timedelta(days=lookback_days + 15)

            token_a = None
            token_b = None
            instruments = kite_client.get_instruments("NSE")
            for i in instruments:
                if i["tradingsymbol"] == symbol_a:
                    token_a = i["instrument_token"]
                if i["tradingsymbol"] == symbol_b:
                    token_b = i["instrument_token"]
                if token_a and token_b:
                    break

            if not token_a or not token_b:
                return 0.0

            hist_a = kite_client.get_historical_data(token_a, from_date, now, "day")
            hist_b = kite_client.get_historical_data(token_b, from_date, now, "day")

            if not hist_a or not hist_b:
                return 0.0

            df_a = pd.DataFrame(hist_a)[["date", "close"]].set_index("date")
            df_b = pd.DataFrame(hist_b)[["date", "close"]].set_index("date")

            df = df_a.join(df_b, lsuffix="_a", rsuffix="_b", how="inner")
            if len(df) < 5:
                return 0.0

            returns_a = df["close_a"].pct_change().dropna()
            returns_b = df["close_b"].pct_change().dropna()

            corr = returns_a.corr(returns_b)
            if pd.isna(corr):
                corr = 0.0

            self._correlation_cache[pair_key] = corr
            return corr

        except Exception as e:
            from .utils import push_log

            push_log(
                f"RiskManager: Error computing correlation between {symbol_a} and {symbol_b}: {e}",
                level="warning",
            )
            return 0.0

    def can_accept_position(
        self,
        symbol: str,
        direction: str,
        qty: int,
        price: float,
        active_trades: dict,
        open_orders: list,
    ) -> Tuple[bool, str]:

        config = config_manager.get_risk_config()
        max_gross = config.get("maxGrossExposure", 200000)
        max_net = config.get("maxNetExposure", 100000)
        max_single = config.get("maxSingleSymbolExposure", 50000)
        max_sector = config.get("maxSectorExposure", 75000)
        max_corr_exposure = config.get("maxCorrelatedExposure", 75000)
        corr_threshold = config.get("correlationThreshold", 0.70)

        proposed_value = qty * price
        proposed_signed = proposed_value if direction == "BUY" else -proposed_value

        current_gross = 0.0
        current_net = 0.0
        symbol_exposures = {}
        sector_exposures = {}

        def add_exposure(sym, val, is_buy):
            nonlocal current_gross, current_net
            current_gross += val
            current_net += val if is_buy else -val
            symbol_exposures[sym] = symbol_exposures.get(sym, 0.0) + val

            sec = get_sector(sym)
            sector_exposures[sec] = sector_exposures.get(sec, 0.0) + val

        for sym, trade in active_trades.items():
            val = trade.get("quantity", 0) * trade.get("entry_price", 0.0)
            if val > 0:
                is_buy = trade.get("direction", "BUY") == "BUY"
                add_exposure(sym, val, is_buy)

        for order in open_orders:
            if order.get("status", "").upper() not in ["OPEN", "TRIGGER PENDING"]:
                continue
            sym = order.get("tradingsymbol")
            q = float(order.get("quantity", 0)) - float(order.get("filled_quantity", 0))
            p = float(order.get("price", 0.0))
            if q > 0 and p > 0:
                val = q * p
                is_buy = order.get("transaction_type", "BUY") == "BUY"
                add_exposure(sym, val, is_buy)

        if current_gross + proposed_value > max_gross:
            return (
                False,
                f"GROSS_EXPOSURE_LIMIT: {current_gross + proposed_value:.2f} > {max_gross}",
            )

        if abs(current_net + proposed_signed) > max_net:
            return (
                False,
                f"NET_EXPOSURE_LIMIT: abs({current_net + proposed_signed:.2f}) > {max_net}",
            )

        if symbol_exposures.get(symbol, 0.0) + proposed_value > max_single:
            return (
                False,
                f"SINGLE_SYMBOL_LIMIT: {symbol} exposure would exceed {max_single}",
            )

        proposed_sector = get_sector(symbol)
        if sector_exposures.get(proposed_sector, 0.0) + proposed_value > max_sector:
            return (
                False,
                f"SECTOR_EXPOSURE_LIMIT: Sector {proposed_sector} exposure would exceed {max_sector}",
            )

        correlated_exposure = proposed_value
        correlated_symbols = [symbol]
        for active_sym in symbol_exposures.keys():
            if active_sym != symbol:
                corr = self._get_correlation(symbol, active_sym)
                if corr >= corr_threshold:
                    correlated_exposure += symbol_exposures[active_sym]
                    correlated_symbols.append(active_sym)

        if correlated_exposure > max_corr_exposure:
            return (
                False,
                f"CORRELATED_EXPOSURE_LIMIT: Group {correlated_symbols} exposure {correlated_exposure:.2f} > {max_corr_exposure}",
            )

        return True, "OK"


risk_manager = RiskManager()
