"""Backtest measurements with explicit financial and observation coverage."""

from collections import Counter, defaultdict
from datetime import datetime
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
    try:
        return float(value) if isfinite(value) else None
    except OverflowError:
        return None


def _average(values: list[float], digits: int = 2) -> float | None:
    return round(mean(values), digits) if values else None


def _aware_timestamp(value: Any) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if (
        not isinstance(parsed, datetime)
        or parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed != parsed
    ):
        raise ValueError("timestamp must be finite and timezone-aware")
    return as_utc(parsed)


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
            "avg_r": None,
            "total_net_r": None,
            "total_gross_r": None,
            "r_coverage": {"available": 0, "trades": len(eligible)},
            "gross_r_coverage": {"available": 0, "trades": len(eligible)},
            "cohorts": [],
            "cohort_coverage": {},
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
        captured_r, gross_r, mfe_prices, mae_prices, mfe_rs, mae_rs = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        excursion_quality: Counter[str] = Counter()
        reason_distribution: Counter[str] = Counter()
        playbook_distribution: Counter[str] = Counter()
        symbol_distribution: Counter[str] = Counter()
        entry_time_of_day: Counter[str] = Counter()
        exit_time_of_day: Counter[str] = Counter()
        cohort_trades: dict[tuple[str, str], list[dict]] = defaultdict(list)
        cohort_counts: Counter[str] = Counter()
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
            value = gross_value = None
            if risk is not None and risk > 0 and budget is not None and budget > 0:
                value = _number(trade["net_pnl"] / budget)
                if value is not None:
                    captured_r.append(value)
                gross_value = _number(trade["gross_pnl"] / budget)
                if gross_value is not None:
                    gross_r.append(gross_value)
            else:
                risk = None
            reason = trade.get("exit_reason") or trade.get("reason")
            if isinstance(reason, str) and reason:
                reason_distribution[reason] += 1
            symbol = trade.get("symbol") or trade.get("tradingsymbol")
            if isinstance(symbol, str) and symbol:
                symbol_distribution[symbol] += 1
            playbook = trade.get("playbook")
            if not isinstance(playbook, str):
                playbook = signal.get("playbook") if isinstance(signal, dict) else None
            if isinstance(playbook, str) and playbook:
                playbook_distribution[playbook] += 1
            entry_bucket = (
                trade["entry_time"].astimezone(EXCHANGE_TIMEZONE).strftime("%H:%M")
            )
            exit_bucket = (
                trade["exit_time"].astimezone(EXCHANGE_TIMEZONE).strftime("%H:%M")
            )
            entry_time_of_day[entry_bucket] += 1
            exit_time_of_day[exit_bucket] += 1
            dimensions = {
                "reason": reason,
                "playbook": playbook,
                "symbol": symbol,
                "entry_time_bucket": entry_bucket,
                "exit_time_bucket": exit_bucket,
                **{
                    field: trade.get(field)
                    for field in (
                        "entry_regime",
                        "exit_regime",
                        "regime_transition",
                        "setup_variant",
                        "sector",
                        "liquidity",
                        "direction",
                    )
                },
            }
            for dimension, label in dimensions.items():
                # Keep missing metadata and unavailable R visible; silently
                # dropping them would overstate breadth and sample coverage.
                cohort_counts.setdefault(dimension, 0)
                if not isinstance(label, str) or not label.strip():
                    continue
                cohort_counts[dimension] += 1
                cohort_trades[(dimension, label)].append(
                    {
                        "net_pnl": trade["net_pnl"],
                        "net_r": value,
                        "gross_r": gross_value,
                    }
                )
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
            "total_net_r": round(sum(captured_r), 4) if captured_r else None,
            "total_gross_r": round(sum(gross_r), 4) if gross_r else None,
            "r_coverage": {"available": len(captured_r), "trades": len(eligible)},
            "gross_r_coverage": {"available": len(gross_r), "trades": len(eligible)},
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
            "reason_distribution": dict(sorted(reason_distribution.items())),
            "playbook_distribution": dict(sorted(playbook_distribution.items())),
            "symbol_distribution": dict(sorted(symbol_distribution.items())),
            "entry_time_of_day_distribution": dict(sorted(entry_time_of_day.items())),
            "exit_time_of_day_distribution": dict(sorted(exit_time_of_day.items())),
            "cohort_coverage": {
                dimension: {"available": count, "trades": len(eligible)}
                for dimension, count in sorted(cohort_counts.items())
            },
            "cohorts": [
                {
                    "dimension": dimension,
                    "value": label,
                    "trade_count": len(records),
                    "net_profit": round(sum(row["net_pnl"] for row in records), 2),
                    "average_net_r": _average(
                        [row["net_r"] for row in records if row["net_r"] is not None],
                        4,
                    ),
                    "average_gross_r": _average(
                        [
                            row["gross_r"]
                            for row in records
                            if row["gross_r"] is not None
                        ],
                        4,
                    ),
                    "r_coverage": sum(row["net_r"] is not None for row in records),
                    "gross_r_coverage": sum(
                        row["gross_r"] is not None for row in records
                    ),
                }
                for (dimension, label), records in sorted(cohort_trades.items())
            ],
        }

    @staticmethod
    def validate_walk_forward_trade_window(
        trades: List[Dict[str, Any]], start: datetime, end: datetime
    ) -> None:
        """Reject results whose realized outcome was unavailable within a fold."""

        for trade in trades:
            timestamps = []
            for field in ("entry_time", "exit_time"):
                try:
                    at = _aware_timestamp(trade.get(field))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        "walk-forward trade requires valid aware entry/exit timestamps"
                    ) from exc
                timestamps.append(at)
            entered, exited = timestamps
            if not start <= entered <= exited < end:
                raise ValueError(
                    "warmup or out-of-window trade leaked into OOS scoring"
                )

    @staticmethod
    def evaluate_walk_forward(
        fold_reports: List[Dict[str, Any]], initial_capital: float
    ) -> Dict[str, Any]:
        """Aggregate disjoint OOS folds without inventing a continuous account.

        Each fold conventionally starts from a fresh declared account state. A
        concatenated closed-trade curve would therefore misstate drawdown and
        session return risk. Trade aggregates are useful diagnostics; continuous
        MTM statistics remain unavailable unless a separately declared
        continuous-state study supplies one account equity path.
        """

        previous_end = None
        all_trades = []
        windows = []
        for report in fold_reports:
            fold = report.get("fold") if isinstance(report, dict) else None
            if not isinstance(fold, dict):
                raise ValueError("walk-forward report requires fold metadata")
            try:
                start = _aware_timestamp(fold.get("test_start"))
                end = _aware_timestamp(fold.get("test_end"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "walk-forward fold has invalid test boundaries"
                ) from exc
            if end <= start:
                raise ValueError("walk-forward fold has invalid test boundaries")
            if previous_end is not None and start < previous_end:
                raise ValueError("walk-forward OOS windows overlap")
            trades = [dict(trade) for trade in report.get("trades", ())]
            MetricsEvaluator.validate_walk_forward_trade_window(trades, start, end)
            all_trades.extend(trades)
            windows.append(
                {
                    "fold_id": fold.get("fold_id"),
                    "test_start": start.isoformat(),
                    "test_end": end.isoformat(),
                    "trade_count": len(trades),
                    "selected_policy_id": report.get("selected_policy_id"),
                }
            )
            previous_end = end
        trade_metrics = MetricsEvaluator.evaluate(all_trades, initial_capital)
        # Even a completed-trade curve and trade-day Sharpe depend on account
        # continuity. Retain them only on individual fold reports, where the
        # declared reset state is meaningful.
        trade_metrics.update(
            metrics_basis="DISJOINT_OOS_COMPLETED_TRADES_NO_CONTINUOUS_ACCOUNT",
            completed_trade_drawdown=None,
            legacy_trade_day_sharpe=None,
            path_dependent_metrics_basis="UNAVAILABLE_ACROSS_ACCOUNT_RESETS",
        )
        return {
            "metrics_basis": "DISJOINT_OOS_FOLD_AGGREGATE_NO_CONTINUOUS_MTM",
            "fold_count": len(windows),
            "windows": windows,
            "aggregate_trade_metrics": trade_metrics,
            "continuous_mtm": {
                "available": False,
                "reason": "FOLDS_USE_SEPARATE_DECLARED_ACCOUNT_STATE",
            },
        }
