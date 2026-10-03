"""Adversarial ownership and coverage checks for disjoint OOS reporting."""

import pandas as pd
import pytest

from backend.backtesting.metrics_evaluator import MetricsEvaluator


def _trade(day=1, **overrides):
    return {
        "symbol": "FIXTURE",
        "playbook": "BREAKOUT",
        "entry_time": f"2026-09-{day:02}T04:00:00+00:00",
        "exit_time": f"2026-09-{day:02}T05:00:00+00:00",
        "entry_price": 100.0,
        "exit_price": 99.0,
        "quantity": 10,
        "direction": "BUY",
        "gross_pnl": -10.0,
        "net_pnl": -12.0,
        "total_fees": 2.0,
        "signal_info": {"stopLoss": 95.0},
        **overrides,
    }


def _fold(day=1, trades=None):
    return {
        "fold": {
            "fold_id": f"oos-{day}",
            "test_start": f"2026-09-{day:02}T00:00:00+00:00",
            "test_end": f"2026-09-{day + 1:02}T00:00:00+00:00",
        },
        "trades": [_trade(day)] if trades is None else trades,
    }


def test_independent_fold_accounts_have_no_concatenated_drawdown_or_sharpe():
    reports = [_fold(1), _fold(2)]
    individual = [
        MetricsEvaluator.evaluate(report["trades"], 1000) for report in reports
    ]
    assert all(metrics["completed_trade_drawdown"] == 12 for metrics in individual)

    result = MetricsEvaluator.evaluate_walk_forward(reports, 1000)
    aggregate = result["aggregate_trade_metrics"]
    assert aggregate["net_profit"] == -24
    assert aggregate["total_net_r"] == -0.48
    assert aggregate["completed_trade_drawdown"] is None
    assert aggregate["legacy_trade_day_sharpe"] is None
    assert aggregate["max_drawdown"] is None
    assert aggregate["path_dependent_metrics_basis"] == (
        "UNAVAILABLE_ACROSS_ACCOUNT_RESETS"
    )


@pytest.mark.parametrize(
    "exit_time",
    [
        "2026-09-02T00:00:00+00:00",
        "2026-09-03T05:00:00+00:00",
        "2026-09-01T03:59:59+00:00",
    ],
)
def test_outcome_must_be_known_inside_its_half_open_oos_window(exit_time):
    with pytest.raises(ValueError, match="out-of-window"):
        MetricsEvaluator.evaluate_walk_forward(
            [_fold(trades=[_trade(exit_time=exit_time)])], 1000
        )


@pytest.mark.parametrize("field", ["entry_time", "exit_time"])
@pytest.mark.parametrize("invalid", [pd.NaT, None, "invalid", "2026-09-01T05:00:00"])
def test_unknown_and_timezone_ambiguous_timestamps_cannot_bypass_oos_checks(
    field, invalid
):
    with pytest.raises(ValueError, match="timestamps"):
        MetricsEvaluator.evaluate_walk_forward(
            [_fold(trades=[_trade(**{field: invalid})])], 1000
        )


@pytest.mark.parametrize("field", ["test_start", "test_end"])
def test_nat_fold_boundary_cannot_bypass_window_checks(field):
    report = _fold()
    report["fold"][field] = pd.NaT
    with pytest.raises(ValueError, match="boundaries"):
        MetricsEvaluator.evaluate_walk_forward([report], 1000)


def test_cohorts_show_outcomes_sample_sizes_and_missing_r_without_inventing_values():
    metrics = MetricsEvaluator.evaluate(
        [
            _trade(),
            _trade(signal_info={}),
            _trade(symbol=None, playbook=None),
            _trade(net_pnl=99),
        ],
        1000,
    )
    assert metrics["trade_records_total"] == 4
    assert metrics["trade_records_excluded"] == 1
    symbol = next(row for row in metrics["cohorts"] if row["dimension"] == "symbol")
    assert symbol == {
        "dimension": "symbol",
        "value": "FIXTURE",
        "trade_count": 2,
        "net_profit": -24,
        "average_net_r": -0.24,
        "average_gross_r": -0.2,
        "r_coverage": 1,
        "gross_r_coverage": 1,
    }
    assert metrics["cohort_coverage"]["symbol"] == {"available": 2, "trades": 3}
    assert metrics["cohort_coverage"]["entry_regime"] == {"available": 0, "trades": 3}
    assert metrics["r_coverage"] == {"available": 2, "trades": 3}


def test_unavailable_financial_and_r_inputs_have_explicit_empty_coverage():
    metrics = MetricsEvaluator.evaluate([_trade(total_fees=None)], 1000)
    assert metrics["trade_count"] == 0
    assert metrics["total_net_r"] is None
    assert metrics["total_gross_r"] is None
    assert metrics["r_coverage"] == {"available": 0, "trades": 0}
    assert metrics["cohorts"] == []


def test_net_and_gross_r_report_separate_finite_coverage():
    metrics = MetricsEvaluator.evaluate(
        [
            _trade(
                entry_price=1e-200,
                quantity=1,
                gross_pnl=1e200,
                total_fees=1e200,
                net_pnl=0,
                signal_info={"stopLoss": 5e-201},
            )
        ],
        1000,
    )
    assert metrics["total_net_r"] == 0
    assert metrics["total_gross_r"] is None
    assert metrics["r_coverage"]["available"] == 1
    assert metrics["gross_r_coverage"]["available"] == 0
