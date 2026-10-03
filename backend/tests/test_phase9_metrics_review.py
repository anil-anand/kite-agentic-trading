"""Adversarial financial/coverage regressions for phase-9 backtest reports."""

from math import sqrt
from statistics import mean, stdev

import pytest

from backend.backtesting.metrics_evaluator import MetricsEvaluator


def trade(**overrides):
    return {
        "entry_time": "2026-09-01T04:00:00+00:00",
        "exit_time": "2026-09-01T05:00:00+00:00",
        "entry_price": 100,
        "exit_price": 101,
        "quantity": 10,
        "direction": "BUY",
        "gross_pnl": 10,
        "net_pnl": 8,
        "total_fees": 2,
        "signal_info": {"stopLoss": 95},
        "mfe": 105,
        "mae": 98,
        **overrides,
    }


def marked_equity():
    return [
        {"timestamp": "2026-09-01T10:00:00+00:00", "equity": 1100},
        {"timestamp": "2026-09-02T10:00:00+00:00", "equity": 1100},
        {"timestamp": "2026-09-03T10:00:00+00:00", "equity": 1045},
    ]


def test_full_mtm_sharpe_includes_zero_trade_sessions_and_open_exposure():
    metrics = MetricsEvaluator.evaluate([trade()], 1000, equity_curve=marked_equity())
    returns = [0.1, 0.0, -0.05]
    assert metrics["sharpe_ratio"] == round(
        sqrt(252) * mean(returns) / stdev(returns), 2
    )
    assert metrics["sharpe_session_count"] == 3
    assert metrics["max_drawdown"] == 55
    assert metrics["completed_trade_drawdown"] == 0
    assert metrics["legacy_trade_day_sharpe"] is None


def test_open_only_portfolio_still_has_session_risk_statistics():
    metrics = MetricsEvaluator.evaluate([], 1000, equity_curve=marked_equity())
    assert metrics["trade_count"] == 0
    assert metrics["sharpe_ratio"] is not None
    assert metrics["max_drawdown"] == 55


def test_closed_trade_only_report_does_not_impersonate_portfolio_statistics():
    metrics = MetricsEvaluator.evaluate([trade()], 1000)
    assert metrics["max_drawdown"] is None
    assert metrics["sharpe_ratio"] is None
    assert metrics["metrics_basis"] == "COMPLETED_TRADES_ONLY_LEGACY"


@pytest.mark.parametrize(
    "signal", [None, {}, {"stopLoss": 105}, {"stopLoss": 0}, {"stopLoss": True}]
)
def test_unknown_or_wrong_side_initial_risk_does_not_become_zero_r(signal):
    metrics = MetricsEvaluator.evaluate([trade(signal_info=signal)], 1000)
    assert metrics["avg_r"] is None
    assert metrics["avg_mfe_r"] is None
    assert metrics["r_coverage"] == {"available": 0, "trades": 1}
    assert metrics["avg_mfe_price"] == 5


def test_unknown_risk_is_not_included_in_known_risk_mean():
    metrics = MetricsEvaluator.evaluate([trade(), trade(signal_info=None)], 1000)
    assert metrics["avg_r"] == 0.16
    assert metrics["r_coverage"]["available"] == 1


def test_unknown_and_fictitious_financials_are_excluded_with_visible_counts():
    metrics = MetricsEvaluator.evaluate(
        [
            trade(),
            trade(exit_price=0),
            trade(net_pnl=float("nan")),
            trade(financial_quality="ESTIMATED"),
            trade(net_pnl=99),
        ],
        1000,
    )
    assert metrics["trade_count"] == 1
    assert metrics["trade_records_total"] == 5
    assert metrics["trade_records_excluded"] == 4
    assert metrics["net_profit"] == 8
    assert sum(metrics["excluded_by_reason"].values()) == 4


def test_short_excursions_and_ambiguous_exclusion_retain_units_and_coverage():
    metrics = MetricsEvaluator.evaluate(
        [
            trade(direction="SELL", mfe=90, mae=103, signal_info={"stopLoss": 105}),
            trade(excursion_quality="CENSORED_AMBIGUOUS_EXECUTION"),
        ],
        1000,
    )
    assert metrics["avg_mfe_price"] == 10
    assert metrics["avg_mae_price"] == 3
    assert metrics["avg_mfe_r"] == 2
    assert metrics["avg_mae_r"] == 0.6
    assert metrics["excursion_coverage"] == {"price": 1, "r": 1, "trades": 2}
    assert (
        metrics["excursion_quality_distribution"]["CENSORED_AMBIGUOUS_EXECUTION"] == 1
    )


def test_zero_gross_does_not_imply_zero_cost_impact():
    metrics = MetricsEvaluator.evaluate([trade(gross_pnl=0, net_pnl=-2)], 1000)
    assert metrics["cost_contribution_pct"] is None
    assert metrics["total_fees_paid"] == 2


@pytest.mark.parametrize(
    "unresolved",
    [
        {"status": "OPEN", "financial_quality": "RECONCILED"},
        {
            "financial_quality": "RECONCILED",
            "financial_provenance": "legacy_zero_price_placeholder",
        },
        {
            "financial_quality": "RECONCILED",
            "financial_provenance": "pending_broker_fill_reconciliation",
        },
        {"financial_provenance": "unknown"},
    ],
)
def test_explicit_unresolved_status_or_provenance_cannot_enter_completed_metrics(
    unresolved,
):
    metrics = MetricsEvaluator.evaluate([trade(), trade(**unresolved)], 1000)
    assert metrics["trade_count"] == 1
    assert metrics["trade_records_excluded"] == 1
    assert metrics["excluded_by_reason"] == {"UNRECONCILED_FINANCIALS": 1}
    assert metrics["net_profit"] == 8


@pytest.mark.parametrize("timestamp", [None, "", "invalid"])
def test_missing_invalid_timestamps_are_counted_and_excluded(timestamp):
    metrics = MetricsEvaluator.evaluate([trade(exit_time=timestamp)], 1000)
    assert metrics["trade_count"] == 0
    assert metrics["excluded_by_reason"] == {"INVALID_TRADE_TIMESTAMPS": 1}


def test_overflowed_initial_risk_budget_is_unavailable_not_zero():
    metrics = MetricsEvaluator.evaluate(
        [trade(entry_price=1e200, quantity=1e200)], 1000
    )
    assert metrics["avg_r"] is None
    assert metrics["r_coverage"]["available"] == 0
