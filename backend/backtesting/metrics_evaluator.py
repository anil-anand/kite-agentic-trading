from typing import Any, Dict, List

import numpy as np
import pandas as pd


class MetricsEvaluator:
    @staticmethod
    def evaluate(
        trades: List[Dict[str, Any]], initial_capital: float
    ) -> Dict[str, Any]:
        if not trades:
            return {}

        df = pd.DataFrame(trades)

        # Ensure datetimes
        df["entry_time"] = pd.to_datetime(df["entry_time"])
        df["exit_time"] = pd.to_datetime(df["exit_time"])
        df["holding_time"] = (
            df["exit_time"] - df["entry_time"]
        ).dt.total_seconds() / 60.0  # in minutes

        # PnL
        df["gross_pnl"] = pd.to_numeric(df["gross_pnl"])
        df["net_pnl"] = pd.to_numeric(df["net_pnl"])
        df["total_fees"] = pd.to_numeric(df["total_fees"])

        # Wins / Losses
        wins = df[df["net_pnl"] > 0]
        losses = df[df["net_pnl"] <= 0]

        win_rate = len(wins) / len(df) if len(df) > 0 else 0

        avg_win = wins["net_pnl"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["net_pnl"].mean()) if len(losses) > 0 else 0

        profit_factor: float | None = (
            (wins["net_pnl"].sum() / abs(losses["net_pnl"].sum()))
            if abs(losses["net_pnl"].sum()) > 0
            else None  # No losses: profit_factor is undefined; use None for JSON safety
        )

        # Expectancy = (Win Rate * Average Win) - (Loss Rate * Average Loss)
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        # Equity Curve & Drawdown
        df = df.sort_values("exit_time")
        df["equity"] = initial_capital + df["net_pnl"].cumsum()
        # Peak must start at initial_capital so that any first-trade loss
        # is captured in drawdown rather than silently discarded.
        df["peak_equity"] = df["equity"].cummax().clip(lower=initial_capital)
        df["drawdown"] = df["peak_equity"] - df["equity"]
        max_drawdown = df["drawdown"].max()

        # Sharpe Ratio (Rough approximation: daily returns over risk-free rate)
        # Group by day to get daily returns
        daily_pnl = df.groupby(df["exit_time"].dt.date)["net_pnl"].sum()
        # Convert to daily returns percentage relative to initial capital
        daily_returns = daily_pnl / initial_capital

        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = np.sqrt(252) * (daily_returns.mean() / daily_returns.std())
        else:
            sharpe = 0.0

        # R-Multiple (Reward to Risk ratio per trade)
        # Assuming signal_info contains 'riskReward' which is target/stop distance
        # Alternatively, Calculate empirical R = net_pnl / abs(entry_price - sl) * quantity
        df["empirical_r"] = 0.0
        for idx, row in df.iterrows():
            sig = row.get("signal_info", {})
            sl = sig.get("stopLoss")
            if sl and row["entry_price"] != sl:
                risk_per_share = abs(row["entry_price"] - sl)
                df.at[idx, "empirical_r"] = row["net_pnl"] / (
                    risk_per_share * row["quantity"]
                )

        avg_r = df["empirical_r"].mean()

        # MAE / MFE (Max Adverse Excursion / Max Favorable Excursion)
        # Requires simulated broker to record these.
        # Wait, SimulatedBroker doesn't currently output MFE/MAE in trades list. Let's assume it might.
        avg_mfe = (
            df.get("mfe", pd.Series(dtype=float)).mean() if "mfe" in df.columns else 0.0
        )
        avg_mae = (
            df.get("mae", pd.Series(dtype=float)).mean() if "mae" in df.columns else 0.0
        )

        return {
            "trade_count": len(df),
            "win_rate": round(win_rate, 4),
            "expectancy": round(expectancy, 2),
            "profit_factor": round(profit_factor, 2)
            if profit_factor is not None
            else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_r": round(avg_r, 2),
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "avg_mfe": round(avg_mfe, 2),
            "avg_mae": round(avg_mae, 2),
            "avg_holding_time_mins": round(df["holding_time"].mean(), 2),
            "total_fees_paid": round(df["total_fees"].sum(), 2),
            "cost_contribution_pct": round(
                (df["total_fees"].sum() / abs(df["gross_pnl"].sum())) * 100, 2
            )
            if df["gross_pnl"].sum() != 0
            else 0.0,
            "net_profit": round(df["net_pnl"].sum(), 2),
        }
