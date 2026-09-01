import datetime
from typing import Any, Dict, Tuple

from .config import config_manager


class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.open_positions = 0

    def can_trade(self) -> Tuple[bool, str]:
        config = config_manager.get_risk_config()

        # Check time
        now = datetime.datetime.now().time()
        no_new_trades_after = datetime.datetime.strptime(
            config["noNewTradesAfter"], "%H:%M"
        ).time()
        if now >= no_new_trades_after:
            return False, "Time is past noNewTradesAfter limit"

        # Check max positions
        if self.open_positions >= config["maxSimultaneousPositions"]:
            return (
                False,
                f"Max simultaneous positions ({config['maxSimultaneousPositions']}) reached",
            )

        # Check max daily loss
        if self.daily_pnl <= -config["maxDailyLoss"]:
            return False, f"Max daily loss ({-config['maxDailyLoss']}) exceeded"

        return True, "OK"

    def calculate_position_size(self, price: float, stop_loss: float) -> int:
        config = config_manager.get_risk_config()
        max_capital = config["maxCapitalPerTrade"]
        risk_per_trade = config.get("riskPerTrade", max_capital * 0.01)

        quantity_by_capital = int(max_capital / price) if price > 0 else 0
        stop_distance = abs(price - stop_loss)

        if stop_distance > 0 and risk_per_trade > 0:
            quantity_by_risk = int(risk_per_trade / stop_distance)
            quantity = (
                min(quantity_by_capital, quantity_by_risk)
                if quantity_by_capital > 0
                else quantity_by_risk
            )
        else:
            quantity = quantity_by_capital
        return max(1, quantity)

    def check_daily_loss_limit(self) -> bool:
        config = config_manager.get_risk_config()
        return self.daily_pnl <= -config["maxDailyLoss"]

    def should_square_off(self) -> bool:
        config = config_manager.get_risk_config()
        now = datetime.datetime.now().time()

        # Check max daily loss first, as this should trigger regardless of time
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
                pass  # Fallback if invalid time string

        # Only square off if we have positions AND we hit max loss or time
        if self.open_positions > 0:
            return time_to_square_off or max_loss_hit

        return False

    def update_pnl(self, pnl: float):
        self.daily_pnl += pnl
        if pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

    def set_open_positions(self, count: int):
        self.open_positions = count

    def get_risk_status(self) -> Dict[str, Any]:
        return {
            "daily_pnl": self.daily_pnl,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "open_positions": self.open_positions,
            "can_trade": self.can_trade()[0],
        }


risk_manager = RiskManager()
