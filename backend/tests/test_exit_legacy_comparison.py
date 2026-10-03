"""The shadow comparator reports actual, timestamped legacy assessments."""

from datetime import timedelta

import pandas as pd
import pytest

import backend.trading_engine as engine_module
from backend.journal import journal
from backend.market_context import build_market_context
from backend.tests.conftest import build_candles
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_exit_live_integration import (
    START as SHADOW_START,
)
from backend.tests.test_exit_live_integration import (
    _managed_engine,
    _position,
)
from backend.trading_engine import TradingEngine

START = pd.Timestamp("2026-09-21 09:15", tz="Asia/Kolkata")


def test_unobserved_control_is_unknown_and_pending_execution_stays_visible():
    summary = TradingEngine._legacy_control_summary({}, None)
    assert summary["reason"] == "LEGACY_CONTROL_NOT_YET_EVALUATED"
    assert summary["action"] == "NOT_EVALUATED"
    assert summary["evaluated_at"] is None

    summary = TradingEngine._legacy_control_summary(
        {"exit_pending": True, "exit_reason": "broker-confirmed risk obligation"}, None
    )
    assert summary["action"] == "MANAGE_PENDING_INTENT"
    assert summary["reason"] == "broker-confirmed risk obligation"


def test_resistance_control_records_hold_rejection_and_missing_data(monkeypatch):
    frame = build_candles(
        [100.0] * 21,
        dates=pd.date_range(START, periods=21, freq="5min"),
    )
    frame.loc[20, ["high", "close"]] = [100.1, 100.0]
    decision_at = (START + timedelta(minutes=105)).to_pydatetime()
    context = build_market_context("111", frame, decision_at)
    engine = TradingEngine()
    engine._instrument_map = {"TEST": 111}
    trade = {
        "direction": "BUY",
        "entry_time": START.to_pydatetime(),
        "entry_price": 98.0,
        "target": 110.0,
    }
    engine.active_trades["TEST"] = trade
    monkeypatch.setattr(engine_module, "now_utc", lambda: decision_at)
    monkeypatch.setattr(
        engine_module.scanner, "get_market_context", lambda *args: context
    )

    assert not engine._check_resistance_exit("TEST", 100.0, "BUY")
    summary = engine._legacy_control_summary(trade, None)
    assert summary["action"] == "HOLD"
    assert summary["reason"] == "LEGACY_CONTROL_NO_LEVEL_REJECTION"
    assessment = summary["assessments"]["resistance_support"]
    assert assessment["evaluated_at"] == decision_at.isoformat()
    assert assessment["market_context"]["snapshot_id"] == context.snapshot_id

    frame.loc[20, ["high", "close"]] = [101.0, 100.1]
    context = build_market_context("111", frame, decision_at)
    trade.pop("last_resistance_bar_start")
    assert engine._check_resistance_exit("TEST", 100.0, "BUY")
    summary = engine._legacy_control_summary(trade, None)
    assert summary["action"] == "REQUEST_EXIT"
    assert summary["reason"] == "LEGACY_CONTROL_RESISTANCE_REJECTION"

    context = build_market_context("111", frame, decision_at + timedelta(minutes=30))
    assert not engine._check_resistance_exit("TEST", 100.0, "BUY")
    summary = engine._legacy_control_summary(trade, None)
    assert summary["action"] == "UNAVAILABLE"
    assert summary["reason"] == "LEGACY_CONTROL_DATA_UNAVAILABLE"


@pytest.mark.parametrize(
    ("evaluation", "action", "reason"),
    [
        (
            {"assessment_available": True, "buy_signals": 1, "sell_signals": 0},
            "HOLD",
            "LEGACY_CONTROL_HOLD",
        ),
        (
            {"assessment_available": True, "buy_signals": 0, "sell_signals": 2},
            "REQUEST_EXIT",
            "LEGACY_CONTROL_OPPOSING_SIGNALS",
        ),
        (
            {"assessment_available": False, "buy_signals": 0, "sell_signals": 0},
            "UNAVAILABLE",
            "LEGACY_CONTROL_DATA_UNAVAILABLE",
        ),
    ],
)
def test_strategy_comparison_records_actual_action_and_source_inputs(
    monkeypatch, evaluation, action, reason
):
    engine = TradingEngine()
    decision_at = (START + timedelta(hours=2)).to_pydatetime()
    trade = {
        "direction": "BUY",
        "entry_time": decision_at - timedelta(minutes=35),
        "entry_price": 100.0,
        "sl": 95.0,
    }
    engine.active_trades["TEST"] = trade
    engine._instrument_map = {"TEST": 111}
    position = {"tradingsymbol": "TEST", "quantity": 10, "last_price": 101.0}
    evaluation = {
        **evaluation,
        "market_context": {"snapshot_id": "legacy-input", "input_hash": "causal-input"},
    }
    monkeypatch.setattr(engine_module, "now_utc", lambda: decision_at)
    monkeypatch.setattr(engine, "_positions", lambda: [position])
    monkeypatch.setattr(engine, "_has_fresh_position_mark", lambda p: True)
    monkeypatch.setattr(engine, "_trade_matches_position", lambda t, p: True)
    monkeypatch.setattr(engine, "_persist_trades", lambda: None)
    monkeypatch.setattr(engine, "_exit_position", lambda *args: None)
    monkeypatch.setattr(
        engine_module.scanner, "evaluate_position", lambda *args: evaluation
    )

    engine._reevaluate_positions()

    summary = engine._legacy_control_summary(trade, None)
    assert summary["action"] == action
    assert summary["reason"] == reason
    assert summary["comparison_basis"] == "LATEST_OBSERVED_AS_OF_SHADOW_EVALUATION"
    assessment = summary["assessments"]["strategy_reevaluation"]
    assert assessment["evaluated_at"] == decision_at.isoformat()
    assert assessment["market_context"]["input_hash"] == "causal-input"


def test_completed_review_cannot_attach_to_a_replaced_position_epoch(monkeypatch):
    engine = TradingEngine()
    former_trade = {"position_epoch": "previous"}
    engine.active_trades["TEST"] = {"position_epoch": "replacement"}
    engine._record_legacy_control_observation(
        "TEST",
        source="strategy_reevaluation",
        action="REQUEST_EXIT",
        reason="LEGACY_CONTROL_OPPOSING_SIGNALS",
        expected_trade=former_trade,
    )
    assert "legacy_control_assessments" not in engine.active_trades["TEST"]


def test_actual_comparator_is_durable_with_its_distinct_as_of_inputs(monkeypatch):
    engine, thesis = _managed_engine()
    trade = engine.active_trades["RELIANCE"]
    trade.update(namespace="LIVE", account_id="acct-1")
    decision_at = SHADOW_START + timedelta(minutes=5)
    clock = {"now": decision_at - timedelta(minutes=1)}
    monkeypatch.setattr(engine_module, "now_utc", lambda: clock["now"])
    engine._record_legacy_control_observation(
        "RELIANCE",
        source="strategy_reevaluation",
        action="HOLD",
        reason="LEGACY_CONTROL_HOLD",
        context={"snapshot_id": "previous-control-input", "primary_bar_id": "earlier"},
        expected_trade=trade,
    )
    clock["now"] = decision_at
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    monkeypatch.setattr(
        engine_module.scanner,
        "get_market_context",
        lambda *args, **kwargs: _context(SHADOW_START, close=100.0),
    )
    engine._evaluate_shadow_position(
        {
            **_position(100.0, decision_at),
            "namespace": "LIVE",
            "account_id": "acct-1",
        },
        trade,
    )

    persisted = journal.get_exit_decisions(thesis.position_key)[0]["payload"]
    control = persisted["trace"]["orchestration"]["legacy_control"]
    assert control["reason"] == "LEGACY_CONTROL_HOLD"
    assert control["evaluated_at"] == (decision_at - timedelta(minutes=1)).isoformat()
    assert control["assessments"]["strategy_reevaluation"]["market_context"] == {
        "snapshot_id": "previous-control-input",
        "primary_bar_id": "earlier",
    }
