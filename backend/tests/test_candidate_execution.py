"""Execute candidate decisions through broker facts, not mode-label wrappers."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.backtesting.backtest_engine import BacktestEngine
from backend.backtesting.candidate_runner import CandidateRunner
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.exit_management.models import ExposureState
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator
from backend.tests.exit_management.test_engine import _context, _state, _thesis

START = datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc)
SYMBOL = "RELIANCE"


def _runner(tmp_path, *, fraction=1.0, loss_limit=None):
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(
            slippage_bps=0, max_fill_fraction=fraction
        )
    )
    broker.place_market_order(
        SYMBOL, "BUY", 10, 100, START, {"stopLoss": 95, "target": 110}
    )
    key = broker._key_for(SYMBOL).as_string()
    thesis = replace(_thesis(), position_key=key, instrument_id=f"SIM-{SYMBOL}")
    runner = CandidateRunner(
        broker=broker,
        coordinator=OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "study.db"))),
        daily_loss_limit=loss_limit,
    )
    runner.register_position(thesis=thesis, state=replace(_state(), position_key=key))
    return runner


def _event(runner, index, close, *, opening=None, volume=1000, context=True):
    start = START + timedelta(minutes=5 * index)
    candle = {
        "date": start,
        "open": opening or close,
        "high": max(opening or close, close) + 0.2,
        "low": min(opening or close, close) - 0.2,
        "close": close,
        "volume": volume,
    }
    contexts = {SYMBOL: replace(_context(start, close), instrument_id=f"SIM-{SYMBOL}")}
    return runner.on_event(
        start + timedelta(minutes=5),
        candles={SYMBOL: candle},
        contexts=contexts if context else {},
    )


def test_confirmed_failure_dispatches_once_then_partial_fills_reconcile(tmp_path):
    runner = _runner(tmp_path, fraction=0.5)
    first = _event(runner, 0, 98)
    assert first[-1].decision.action.value == "HOLD"
    second = _event(runner, 1, 98)
    assert second[-1].decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert runner.broker.positions[SYMBOL]["quantity"] == 10
    _event(runner, 2, 99)
    assert runner.positions[SYMBOL].state.known_quantity == 5
    for index in range(3, 8):
        _event(runner, index, 100)
    assert runner.positions[SYMBOL].state.exposure is ExposureState.CLOSED
    reductions = [o for o in runner.broker.orders.values() if o["role"] == "REDUCTION"]
    assert len(reductions) == 1
    assert runner.broker.trades[0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert runner.broker.cash == pytest.approx(
        runner.broker.initial_capital + runner.broker.trades[0]["net_pnl"]
    )


def test_intact_retest_then_recovery_does_not_execute_exit(tmp_path):
    runner = _runner(tmp_path)
    _event(runner, 0, 99.4)
    _event(runner, 1, 100.1)
    _event(runner, 2, 100.5)
    assert runner.positions[SYMBOL].state.exposure is ExposureState.OPEN
    assert not runner.execution_results


def test_daily_loss_uses_open_marked_pnl_and_remains_latched_after_rebound(tmp_path):
    runner = _runner(tmp_path, loss_limit=15)
    result = _event(runner, 0, 98, context=False)
    assert result[0].decision.primary_reason_code == "RISK_DAILY_LOSS"
    assert runner.daily_loss_latched
    # No liquidity: acknowledgement does not close exposure on the rebound.
    _event(runner, 1, 102, volume=0, context=False)
    assert runner.daily_loss_latched
    assert runner.broker.positions[SYMBOL]["quantity"] == 10
    assert runner.positions[SYMBOL].state.exposure is ExposureState.EXIT_PENDING


def test_clock_only_deadline_creates_obligation_with_no_fictional_fill(tmp_path):
    runner = _runner(tmp_path)
    at = START.replace(hour=9, minute=45)  # 15:15 IST
    result = runner.on_event(at)
    assert result[0].decision.primary_reason_code == "SESSION_FORCED_FLAT"
    assert runner.positions[SYMBOL].state.exposure is ExposureState.EXIT_PENDING
    assert not runner.broker.trades
    assert runner.finish()["censored_positions"][0]["quantity"] == 10


def test_paper_quote_observes_hard_stop_without_normal_candle(tmp_path):
    runner = _runner(tmp_path)
    at = START + timedelta(seconds=20)
    runner.broker.mark_price(SYMBOL, 94, at)
    result = runner.on_event(at)
    assert result[0].decision.primary_reason_code == "RISK_CATASTROPHIC_STOP"
    assert runner.positions[SYMBOL].management.eligible_completed_bars == 0


def test_backtest_driver_emits_independent_deadline_and_manifest(tmp_path):
    runner = _runner(tmp_path)
    engine = BacktestEngine(
        None, broker=runner.broker, mode=BacktestEngine.CANDIDATE_EXIT_REPLAY
    )
    engine.load_data(
        SYMBOL,
        pd.DataFrame(
            [
                {
                    "date": START,
                    "open": 100,
                    "high": 101,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1000,
                }
            ]
        ),
    )
    result = engine.run_candidate_execution(runner)
    assert result["manifest"]["datasets"][SYMBOL]
    assert (
        result["manifest"]["entry_policy"] == "FIXED_ADMITTED_ENTRY_FILL_OPPORTUNITIES"
    )
    assert (
        result["evaluations"][-1].decision.primary_reason_code == "SESSION_FORCED_FLAT"
    )
    assert result["censored_positions"][0]["pending_intent_id"]


def test_raw_lab_respects_declared_latency_and_final_equity(tmp_path):
    from backend.tests.test_backtester import MockStrategy

    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(order_latency_bars=1)
    )
    engine = BacktestEngine(MockStrategy(), broker=broker)
    engine.load_data(
        "TEST",
        pd.DataFrame(
            [
                {
                    "date": START + timedelta(minutes=5 * i),
                    "open": 115 + i,
                    "high": 120 + i,
                    "low": 114 + i,
                    "close": 116 + i,
                    "volume": 1000,
                }
                for i in range(4)
            ]
        ),
    )
    result = engine.run()
    assert broker.fills[0]["exchange_time"] == START + timedelta(minutes=10)
    assert result["equity_curve"][-1]["equity"] == broker.current_equity({})
    assert result["censored_positions"][0]["quantity"] > 0
    assert not broker.trades
