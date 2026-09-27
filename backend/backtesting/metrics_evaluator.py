"""Backtest measurements with explicit financial and observation coverage."""

from collections import Counter, defaultdict
from math import isfinite, sqrt
from numbers import Real
from statistics import mean, stdev
from typing import Any, Dict, List

from ..exit_quality import calculate_mtm_drawdown
from ..financial_eligibility import verified_outcome
from ..time_utils import EXCHANGE_TIMEZONE, as_utc


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value) if isfinite(value) else None


def _average(values: list[float], digits: int = 2) -> float | None:
    return round(mean(values), digits) if values else None


def _sharpe(returns: list[float]) -> float | None:
    # A constant or one-session sample does not establish risk-adjusted return.
    if (
        len(returns) < 2
        or not all(isfinite(value) for value in returns)
        or stdev(returns) <= 0
    ):
        return None
    return round(sqrt(252) * mean(returns) / stdev(returns), 2)


class MetricsEvaluator:
    @staticmethod
    def evaluate(
        trades: List[Dict[str, Any]],
        initial_capital: float,
        *,
        equity_curve: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        mtm = calculate_mtm_drawdown(equity_curve or [], initial_capital)
        eligible = []
        excluded: Counter[str] = Counter()
        for trade in trades:
            # SimulatedBroker supplies final finite fills with deterministic
            # costs. Explicit unresolved/legacy journal quality must not be
            # silently upgraded to that contract.
            if any(
                field in trade
                for field in ("status", "financial_quality", "financial_provenance")
            ) and not verified_outcome(
                {"status": "CLOSED", "financial_quality": "RECONCILED", **trade}
            ):
                excluded["UNRECONCILED_FINANCIALS"] += 1
                continue
            numeric = {
                key: _number(trade.get(key))
                for key in (
                    "entry_price",
                    "quantity",
                    "gross_pnl",
                    "net_pnl",
                    "total_fees",
                )
            }
            exit_price = _number(trade.get("exit_price"))
            if (
                any(value is None for value in numeric.values())
                or numeric["entry_price"] <= 0
                or numeric["quantity"] <= 0
                or not numeric["quantity"].is_integer()
                or numeric["total_fees"] < 0
                or ("exit_price" in trade and (exit_price is None or exit_price <= 0))
            ):
                excluded["INVALID_OR_MISSING_FINANCIAL_INPUT"] += 1
                continue
            discrepancy = _number(
                numeric["gross_pnl"] - numeric["total_fees"] - numeric["net_pnl"]
            )
            if discrepancy is None or abs(discrepancy) > 0.02:
                excluded["INCONSISTENT_GROSS_NET_COSTS"] += 1
                continue
            try:
                entry_at = as_utc(trade.get("entry_time"))
                exit_at = as_utc(trade.get("exit_time"))
            except (TypeError, ValueError):
                excluded["INVALID_TRADE_TIMESTAMPS"] += 1
                continue
            if (
                entry_at is None
                or exit_at is None
                or entry_at != entry_at
                or exit_at != exit_at
                or exit_at < entry_at
            ):
                excluded["INVALID_TRADE_TIMESTAMPS"] += 1
                continue
            eligible.append(
                {**trade, **numeric, "entry_time": entry_at, "exit_time": exit_at}
            )

        # Portfolio statistics remain available even with open exposure and no
        # completed trades. Zero-trade sessions are present in marked returns.
        session_returns = [
            value / 100
            for session in mtm["session_returns"]
            if (value := _number(session.get("return_pct"))) is not None
        ]
        full_mtm = mtm["basis"] == "FULL_MARK_TO_MARKET_EQUITY"
        base = {
            "trade_count": len(eligible),
            "trade_records_total": len(trades),
            "trade_records_excluded": sum(excluded.values()),
            "excluded_by_reason": dict(sorted(excluded.items())),
            "metrics_basis": mtm["basis"]
            if mtm["session_returns"]
            else "COMPLETED_TRADES_ONLY_LEGACY",
            "mtm": mtm,
            # Never substitute the closed-trade curve for portfolio drawdown.
            "max_drawdown": mtm["max_drawdown_currency"],
            "sharpe_ratio": _sharpe(session_returns) if full_mtm else None,
            "sharpe_basis": "MTM_OBSERVED_SESSION_RETURNS"
            if full_mtm
            else "UNAVAILABLE_WITHOUT_COMPLETE_MTM",
            "sharpe_session_count": len(session_returns),
            "sharpe_risk_free_rate": 0.0,
            "sharpe_annualization_sessions": 252,
        }
        if not eligible:
            return base

        eligible.sort(key=lambda trade: trade["exit_time"])
        pnl = [trade["net_pnl"] for trade in eligible]
        wins = [value for value in pnl if value > 0]
        losses = [value for value in pnl if value <= 0]
        win_rate = len(wins) / len(pnl)
        avg_win = mean(wins) if wins else 0.0
        avg_loss = abs(mean(losses)) if losses else 0.0
        profit_factor = sum(wins) / abs(sum(losses)) if sum(losses) else None
        equity = peak = initial_capital
        completed_drawdown = 0.0
        daily_pnl: dict[str, float] = defaultdict(float)
        captured_r, mfe_prices, mae_prices, mfe_rs, mae_rs = [], [], [], [], []
        excursion_quality: Counter[str] = Counter()
        for trade in eligible:
            equity += trade["net_pnl"]
            peak = max(peak, equity)
            completed_drawdown = max(completed_drawdown, peak - equity)
            session = (
                trade["exit_time"].astimezone(EXCHANGE_TIMEZONE).date().isoformat()
            )
            daily_pnl[session] += trade["net_pnl"]
            entry = trade["entry_price"]
            sign = {"BUY": 1, "SELL": -1}.get(trade.get("direction"))
            signal = trade.get("signal_info")
            stop = _number(signal.get("stopLoss")) if isinstance(signal, dict) else None
            risk = (
                sign * (entry - stop)
                if sign is not None and stop is not None and stop > 0
                else None
            )
            budget = _number(risk * trade["quantity"]) if risk is not None else None
            if risk is not None and risk > 0 and budget is not None and budget > 0:
                value = _number(trade["net_pnl"] / budget)
                if value is not None:
                    captured_r.append(value)
            else:
                risk = None
            mfe, mae = _number(trade.get("mfe")), _number(trade.get("mae"))
            quality = str(trade.get("excursion_quality") or "UNSPECIFIED")
            excursion_quality[quality] += 1
            if (
                sign is None
                or mfe is None
                or mae is None
                or mfe <= 0
                or mae <= 0
                or quality == "CENSORED_AMBIGUOUS_EXECUTION"
            ):
                continue
            mfe_price, mae_price = (
                max(0.0, sign * (mfe - entry)),
                max(0.0, sign * (entry - mae)),
            )
            mfe_prices.append(mfe_price)
            mae_prices.append(mae_price)
            if risk is not None:
                mfe_rs.append(mfe_price / risk)
                mae_rs.append(mae_price / risk)

        gross, fees = (
            sum(t["gross_pnl"] for t in eligible),
            sum(t["total_fees"] for t in eligible),
        )
        return {
            **base,
            "win_rate": round(win_rate, 4),
            "expectancy": round(mean(pnl), 2),
            "profit_factor": round(profit_factor, 2)
            if profit_factor is not None
            else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_r": _average(captured_r),
            "r_coverage": {"available": len(captured_r), "trades": len(eligible)},
            "completed_trade_drawdown": round(completed_drawdown, 2),
            "legacy_trade_day_sharpe": _sharpe(
                [value / initial_capital for value in daily_pnl.values()]
            ),
            # Price distances and price-path R are initial-quantity opportunity
            # proxies. Partial-bar observations are lower bounds, not exact MFE.
            "avg_mfe": _average(mfe_prices),
            "avg_mae": _average(mae_prices),
            "avg_mfe_price": _average(mfe_prices),
            "avg_mae_price": _average(mae_prices),
            "avg_mfe_r": _average(mfe_rs, 4),
            "avg_mae_r": _average(mae_rs, 4),
            "excursion_basis": "INITIAL_QUANTITY_PRICE_PATH_OBSERVED_LOWER_BOUNDS",
            "excursion_coverage": {
                "price": len(mfe_prices),
                "r": len(mfe_rs),
                "trades": len(eligible),
            },
            "excursion_quality_distribution": dict(sorted(excursion_quality.items())),
            "avg_holding_time_mins": _average(
                [
                    (t["exit_time"] - t["entry_time"]).total_seconds() / 60
                    for t in eligible
                ]
            ),
            "total_fees_paid": round(fees, 2),
            "cost_contribution_pct": round(fees / abs(gross) * 100, 2)
            if gross
            else None,
            "net_profit": round(sum(pnl), 2),
        }
