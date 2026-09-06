import datetime
from typing import Any, Dict, Tuple

from .config import config_manager


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

        estimated_charges = len(trades) * 20.0
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
        now = datetime.datetime.now().time()

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

        # We need an estimate of charges.
        # The trades API shouldn't be hit every second.
        # We can approximate: if there are N executed trades today in our journal, use that.
        from .journal import journal

        trades_today = journal.get_todays_trade_counts()["total"]
        # Since an order usually has 2 trades (entry/exit), maybe 2 * trades_today * 20
        # Actually, to be safe, just use a known margin.
        estimated_charges = trades_today * 40.0  # roughly 40 rs per round trip trade

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


risk_manager = RiskManager()
