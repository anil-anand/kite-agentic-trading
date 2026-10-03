from datetime import datetime, timezone

import pytest

from backend.analytics import TradeAnalytics
from backend.exit_quality import (
    build_exit_quality_report,
    calculate_delayed_exit_diagnostic,
    calculate_exit_quality,
    calculate_mtm_drawdown,
    compare_actual_exit_to_hold_n,
    simulate_risk_constrained_hold_n,
)
from backend.journal import TradeJournal

START = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)


def test_long_quality_uses_immutable_initial_r_and_explicit_units():
    result = calculate_exit_quality(
        direction="BUY",
        entry_price=100,
        initial_stop=95,
        initial_quantity=10,
        realized_gross=80,
        realized_net=70,
        price_path=[{"high": 115, "low": 98}],
        entry_at=START,
        exit_at=START.replace(minute=30),
        quality="RECONCILED",
    )

    assert result["eligible"] is True
    metrics = result["metrics"]
    assert metrics["risk_per_share_price"] == 5.0
    assert metrics["initial_risk_currency"] == 50.0
    assert metrics["mfe_price"] == 15.0
    assert metrics["mae_price"] == 2.0
    assert metrics["mfe_r"] == 3.0
    assert metrics["mae_r"] == 0.4
    assert metrics["captured_gross_r"] == 1.6
    assert metrics["captured_net_r"] == 1.4
    assert metrics["mfe_capture_pct"] == pytest.approx(53.333333)
    assert metrics["r_given_back"] == 1.4
    assert metrics["holding_time_seconds"] == 900.0


def test_short_no_mfe_does_not_invent_capture_percentage():
    result = calculate_exit_quality(
        direction="SELL",
        entry_price=100,
        initial_stop=105,
        initial_quantity=10,
        realized_gross=-10,
        realized_net=-12,
        price_path=[{"high": 104, "low": 100}],
        quality="RECONCILED",
    )

    assert result["metrics"]["mfe_r"] == 0.0
    assert result["metrics"]["mae_r"] == 0.8
    assert result["metrics"]["mfe_capture_pct"] is None


def test_partial_exposure_peak_is_not_treated_as_full_quantity_hold():
    result = calculate_exit_quality(
        direction="BUY",
        entry_price=100,
        initial_stop=95,
        initial_quantity=10,
        realized_gross=30,
        realized_net=28,
        exposure_path=[
            {"realized_gross": 0, "residual_quantity": 10, "mark_price": 110},
            {"realized_gross": 20, "residual_quantity": 5, "mark_price": 108},
        ],
        quality="RECONCILED",
    )

    assert result["metrics"]["exposure_peak_gross"] == 100.0
    assert result["metrics"]["exposure_peak_r"] == 2.0
    assert result["metrics"]["exposure_aware_r_given_back"] == 1.4


def test_hold_n_reports_continuation_only_when_retained_stop_survives():
    path = [
        {
            "timestamp": START.replace(minute=20).isoformat(),
            "open": 101,
            "high": 106,
            "low": 100,
            "close": 106,
        }
    ]
    held = simulate_risk_constrained_hold_n(
        direction="BUY",
        entry_price=100,
        initial_stop=98,
        confirmed_stop=98,
        quantity=10,
        path=path,
        horizon_bars=1,
        bar_duration_seconds=300,
        entry_fees=0,
        exit_fees=0,
    )
    comparison = compare_actual_exit_to_hold_n(0.5, held)

    assert held["termination"] == "HORIZON_EXIT"
    assert held["net_r"] == 3.0
    assert comparison["profit_forgone_r"] == 2.5
    assert comparison["reversal_loss_avoided_r"] == 0.0

    stopped = simulate_risk_constrained_hold_n(
        direction="BUY",
        entry_price=100,
        initial_stop=98,
        confirmed_stop=98,
        quantity=10,
        path=[{**path[0], "low": 97}],
        horizon_bars=1,
        bar_duration_seconds=300,
        entry_fees=0,
        exit_fees=0,
    )
    stopped_comparison = compare_actual_exit_to_hold_n(0.5, stopped)
    assert stopped["termination"] == "RETAINED_STOP_TRIGGERED"
    assert stopped_comparison["profit_forgone_r"] == 0.0
    assert stopped_comparison["reversal_loss_avoided_r"] == 1.5


def test_mtm_drawdown_uses_marked_equity_and_session_returns():
    report = calculate_mtm_drawdown(
        [
            {"timestamp": "2026-09-01T09:15:00+00:00", "equity": 1000},
            {"timestamp": "2026-09-01T10:00:00+00:00", "equity": 900},
            {"timestamp": "2026-09-02T09:15:00+00:00", "equity": 950},
        ],
        initial_capital=1000,
    )

    assert report["basis"] == "FULL_MARK_TO_MARKET_EQUITY"
    assert report["max_drawdown_currency"] == 100.0
    assert len(report["session_returns"]) == 2
    assert report["session_returns"][0]["return_currency"] == -100.0


def test_legacy_or_unreconciled_journal_rows_are_visible_but_excluded(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    journal.open_trade(
        trade_id="legacy-trade",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        product="MIS",
        strategy="legacy",
        entry_price=100,
        quantity=10,
        stop_loss=95,
        target=110,
    )
    journal.close_trade(
        "legacy-trade",
        101,
        "legacy_exit",
        cost_details={
            "gross_pnl": 10,
            "net_pnl": 8,
            "brokerage": 1,
            "taxes": 1,
            "exchange_charges": 0,
            "other_fees": 0,
            "slippage": 0,
            "financial_quality": "ESTIMATED",
            "financial_provenance": "fixture",
        },
    )

    report = TradeAnalytics(str(journal.db_path)).get_exit_quality_report()
    assert report["records_total"] == 1
    assert report["records_eligible"] == 0
    assert report["records_excluded"] == 1
    assert report["records"][0]["replay_status"] == "LEGACY_OR_UNMANAGED"


def _hold(**overrides):
    arguments = {
        "direction": "BUY",
        "entry_price": 100,
        "initial_stop": 98,
        "confirmed_stop": 98,
        "quantity": 10,
        "horizon_bars": 1,
        "entry_fees": 1,
        "exit_fees": 1,
        "start_at": START,
        "bar_duration_seconds": 300,
        "path": [
            {
                "timestamp": START.replace(minute=20).isoformat(),
                "open": 101,
                "high": 106,
                "low": 100,
                "close": 105,
            }
        ],
    }
    arguments.update(overrides)
    return simulate_risk_constrained_hold_n(**arguments)


def test_invalid_initial_risk_keeps_renderable_unavailable_contract():
    result = calculate_exit_quality(
        direction="BUY",
        entry_price=100,
        initial_stop=0,
        initial_quantity=10,
        reason_code="LEGACY_REASON_UNRESOLVED",
    )
    valid = calculate_exit_quality(
        direction="BUY", entry_price=100, initial_stop=95, initial_quantity=10
    )
    assert result["eligible"] is False
    assert result["coverage"]["available_count"] == 0
    assert result["reason_code"] == "LEGACY_REASON_UNRESOLVED"
    assert result["metrics"].keys() == valid["metrics"].keys()
    assert set(result["metrics"].values()) == {None}


def test_nonfinite_initial_risk_product_is_excluded():
    result = calculate_exit_quality(
        direction="BUY", entry_price=1e308, initial_stop=1, initial_quantity=10
    )
    assert result["eligible"] is False
    assert result["coverage"]["available_count"] == 0


def test_exposure_zero_peak_is_not_overwritten_by_subsequent_loss():
    result = calculate_exit_quality(
        direction="BUY",
        entry_price=100,
        initial_stop=95,
        initial_quantity=10,
        realized_gross=-20,
        realized_net=-22,
        exposure_path=[
            {"realized_gross": 0, "residual_quantity": 10, "mark_price": 100},
            {"realized_gross": 0, "residual_quantity": 10, "mark_price": 99},
        ],
    )
    metrics = result["metrics"]
    assert metrics["exposure_peak_gross"] == 0
    assert metrics["exposure_aware_r_given_back"] == 0.4
    assert metrics["exposure_mae_currency"] == 20
    assert metrics["exposure_mae_r"] == 0.4


def test_exposure_marks_reject_impossible_or_fractional_residuals():
    result = calculate_exit_quality(
        direction="BUY",
        entry_price=100,
        initial_stop=95,
        initial_quantity=10,
        exposure_path=[
            {"realized_gross": 0, "residual_quantity": 100, "mark_price": 150},
            {"realized_gross": 0, "residual_quantity": 0.5, "mark_price": 150},
        ],
    )
    assert result["metrics"]["exposure_peak_r"] is None
    assert result["coverage"]["exposure_invalid_points"] == 2


def test_hold_censors_deadline_inside_bar_before_later_stop_or_rally():
    result = _hold(forced_deadline_at=START.replace(minute=18))
    assert result["status"] == "CENSORED"
    assert result["censor_reason"] == "FORCED_DEADLINE_PRICE_UNAVAILABLE"
    assert "net_r" not in result


def test_hold_preserves_gap_stop_known_before_intrabar_deadline():
    result = _hold(
        forced_deadline_at=START.replace(minute=18),
        path=[
            {
                "timestamp": START.replace(minute=20).isoformat(),
                "open": 96,
                "high": 106,
                "low": 94,
                "close": 105,
            }
        ],
    )
    assert result["termination"] == "RETAINED_STOP_TRIGGERED"
    assert result["exit_price"] == 96
    assert result["net_r"] == -2.1


@pytest.mark.parametrize(
    "path,reason",
    [
        (
            [
                {
                    "timestamp": "2026-09-01T09:20:00+00:00",
                    "open": 101,
                    "high": 106,
                    "low": 100,
                    "close": 105,
                },
                {
                    "timestamp": "2026-09-01T09:30:00+00:00",
                    "open": 105,
                    "high": 110,
                    "low": 100,
                    "close": 108,
                },
            ],
            "GAP_OR_OVERLAPPING_RETAINED_PATH",
        ),
        (
            [
                {
                    "timestamp": "2026-09-01T09:20:00+00:00",
                    "open": 101,
                    "high": 106,
                    "low": 100,
                    "close": 105,
                },
                {
                    "timestamp": "2026-09-01T09:20:00+00:00",
                    "open": 105,
                    "high": 110,
                    "low": 100,
                    "close": 108,
                },
            ],
            "NON_CHRONOLOGICAL_RETAINED_PATH",
        ),
        (
            [
                {
                    "timestamp": "2026-09-01T09:20:00+00:00",
                    "open": 101,
                    "high": 106,
                    "low": 100,
                    "close": 110,
                }
            ],
            "MISSING_OR_INVALID_RETAINED_PATH",
        ),
        (
            [
                {
                    "timestamp": "2026-09-01T09:20:00",
                    "open": 101,
                    "high": 106,
                    "low": 100,
                    "close": 105,
                }
            ],
            "MISSING_OR_INVALID_RETAINED_PATH",
        ),
        (
            [
                {
                    "timestamp": "2026-09-01T09:20:00+00:00",
                    "open": 101,
                    "high": 106,
                    "low": 100,
                    "close": 105,
                    "volume": 0,
                }
            ],
            "NO_EXECUTABLE_RETAINED_PRICE",
        ),
    ],
)
def test_hold_censors_missing_duplicate_naive_or_nonexecutable_data(path, reason):
    result = _hold(path=path, horizon_bars=2)
    assert result["status"] == "CENSORED"
    assert result["censor_reason"] == reason


def test_hold_does_not_claim_account_risk_or_free_execution():
    result = _hold(
        entry_fees=None,
        exit_fees=None,
        account_scenario="POSITION_CONDITIONAL_RECORDED_OTHER_TRADES",
    )
    assert result["quality"] == "PRICE_PATH_ONLY"
    assert result["account_risk_recomputed"] is False
    assert result["gross_r"] == 2.5
    assert result["net_r"] is None
    assert compare_actual_exit_to_hold_n(0.5, result)["profit_forgone_r"] is None


def test_hold_censors_missing_interval_geometry():
    result = _hold(bar_duration_seconds=None)
    assert result["status"] == "CENSORED"
    assert result["censor_reason"] == "RETAINED_INTERVAL_GEOMETRY_UNAVAILABLE"


def test_partial_hold_preserves_initial_r_and_prior_realized_costs():
    result = _hold(
        quantity=5,
        initial_quantity=10,
        realized_gross_before_hold=10,
        entry_fees=2,
        prior_exit_fees=1,
        exit_fees=1,
    )
    assert result["gross_pnl"] == 35
    assert result["net_pnl"] == 31
    assert result["gross_r"] == 1.75
    assert result["net_r"] == 1.55


def test_plus_two_r_exit_before_reversal_reports_loss_avoided_without_later_high():
    result = _hold(
        actual_exit_price=104,
        path=[
            {
                "timestamp": START.replace(minute=20).isoformat(),
                "open": 104,
                "high": 120,
                "low": 96,
                "close": 119,
            }
        ],
    )
    comparison = compare_actual_exit_to_hold_n(1.9, result, normal_exit=True)
    assert result["maximum_additional_favorable_r_lower_bound"] == 0
    assert result["terminal_extrema_quality"] == "INTRABAR_ORDER_UNKNOWN"
    assert comparison["reversal_loss_avoided_r"] == 3
    assert comparison["profit_forgone_r"] == 0
    assert comparison["price_path_continuation_diagnostic"] is False
    assert comparison["premature_exit_diagnostic"] is None


def test_short_hold_applies_gap_risk_symmetrically():
    result = _hold(
        direction="SELL",
        initial_stop=102,
        confirmed_stop=102,
        path=[
            {
                "timestamp": START.replace(minute=20).isoformat(),
                "open": 104,
                "high": 106,
                "low": 94,
                "close": 95,
            }
        ],
    )
    assert result["exit_price"] == 104
    assert result["net_r"] == -2.1


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_fees": -1},
        {"exit_fees": float("nan")},
        {"horizon_bars": True},
        {"forced_deadline_at": "2026-09-01T09:18:00"},
    ],
)
def test_hold_rejects_invalid_cost_horizon_and_deadline(overrides):
    with pytest.raises(ValueError):
        _hold(**overrides)


def test_mtm_percentage_uses_peak_of_each_drawdown_not_initial_capital():
    result = calculate_mtm_drawdown(
        [
            {"timestamp": "2026-09-01T04:00:00+00:00", "equity": 800},
            {"timestamp": "2026-09-01T04:05:00+00:00", "equity": 2000},
            {"timestamp": "2026-09-01T04:10:00+00:00", "equity": 1700},
        ],
        1000,
    )
    assert result["max_drawdown_currency"] == 300
    assert result["max_drawdown_pct"] == 20
    assert result["max_drawdown_initial_capital_pct"] == 30


def test_mtm_invalid_sample_does_not_claim_full_coverage():
    result = calculate_mtm_drawdown(
        [
            {"timestamp": "2026-09-01T04:00:00+00:00", "equity": 1000},
            {"timestamp": "2026-09-01T04:05:00+00:00", "equity": float("nan")},
        ],
        1000,
    )
    assert result["basis"] == "PARTIAL_MARK_TO_MARKET_EQUITY"
    assert result["coverage"]["invalid_samples"] == 1


def test_report_preserves_unknown_exclusions_and_nonexit_decisions():
    report = build_exit_quality_report(
        [{"eligible": False}, {"eligible": False, "exclusion_reason": None}],
        decisions=[
            {"action": "HOLD", "primary_reason_code": "HOLD_HEALTHY_PULLBACK"},
            {},
        ],
    )
    assert report["excluded_by_reason"] == {"UNKNOWN": 2}
    assert report["action_reason_distribution"] == [
        {"action": "HOLD", "reason_code": "HOLD_HEALTHY_PULLBACK", "count": 1},
        {"action": "UNKNOWN", "reason_code": "UNKNOWN", "count": 1},
    ]


def test_delayed_exit_separates_prompt_policy_from_broker_delay():
    result = calculate_delayed_exit_diagnostic(
        invalidation_at=START,
        intent_at=START.replace(second=1),
        fill_at=START.replace(minute=16),
        grace_seconds=10,
        direction="BUY",
        invalidation_price=100,
        fill_price=98,
        risk_per_share=2,
    )
    assert result["delayed_exit_diagnostic"] is True
    assert result["policy_delay_beyond_grace_seconds"] == 0
    assert result["intent_to_fill_seconds"] == 59
    assert result["additional_adverse_r_at_fill"] == 1
