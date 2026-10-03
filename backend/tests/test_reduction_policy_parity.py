"""Live and research adapters must submit and replace the same reductions."""

from datetime import timedelta

import pandas as pd
import pytest

import backend.trading_engine as te
from backend.backtesting.legacy_control import LegacyControlRunner
from backend.tests.test_candidate_execution import START, SYMBOL, _runner
from backend.tests.test_phase2_engine_recovery import cancel_terminal, open_trade
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import now_utc


@pytest.mark.parametrize("hard", [False, True])
def test_live_and_research_payloads_partial_timeout_and_late_cancel(
    lifecycle, tmp_path, monkeypatch, hard
):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.sdk.quote_price = 98
    reason = "Stop Loss" if hard else "THESIS_BREAKOUT_FAILED"
    before = now_utc()
    e.engine._place_exit_order(e.engine._positions()[0], SYMBOL, reason)
    live_id = trade["exit_order_id"]
    live_order = e.sdk.book[-1]

    runner = _runner(tmp_path)
    runner.broker.mark_price(SYMBOL, 98, START)
    managed = runner.positions[SYMBOL]
    result = runner._submit_reduction(SYMBOL, START, reason, hard=hard)
    managed.coordinator_intent_id = result.intent_id
    simulated = runner.broker.orders[result.broker_order_id]
    assert (
        simulated["order_type"]
        == live_order["order_type"]
        == ("MARKET" if hard else "LIMIT")
    )
    assert simulated.get("price") == live_order.get("price")
    assert simulated["quantity"] == live_order["quantity"] == 10
    if not hard:
        assert simulated["price"] == 97
        runner.broker.process_candle(
            SYMBOL,
            pd.Series(
                {
                    "date": START,
                    "open": 96,
                    "high": 96.5,
                    "low": 96,
                    "close": 96,
                    "volume": 1000,
                }
            ),
        )
        assert simulated["filled_quantity"] == 0

    # A partial fill can arrive while cancellation is still unacknowledged.
    live_order.update(status="OPEN", filled_quantity=4, pending_quantity=6)
    e.sdk.position_rows[0]["quantity"] = 6
    runner.broker._fill_order(
        simulated["order_id"],
        raw_price=98,
        quantity=4,
        timestamp=START + timedelta(minutes=5),
    )
    sim_cancel = runner.broker.cancel_order
    monkeypatch.setattr(runner.broker, "cancel_order", lambda *a, **k: None)
    e.sdk.cancel_order = lambda *a, **k: live_id
    monkeypatch.setattr(te, "now_utc", lambda: before + timedelta(minutes=6))
    e.engine._sync_exit_pending_status(SYMBOL, e.engine._orders())
    runner._reconcile(SYMBOL, START + timedelta(minutes=6))
    response = runner._submit_reduction(
        SYMBOL, START + timedelta(minutes=6), reason, hard=hard
    )
    assert response.broker_order_id == simulated["order_id"]
    assert len(e.sdk.calls) == 3
    assert (
        len([o for o in runner.broker.orders.values() if o["role"] == "REDUCTION"]) == 1
    )

    live_order.update(status="CANCELLED", pending_quantity=0)
    sim_cancel(simulated["order_id"], timestamp=START + timedelta(minutes=6, seconds=1))
    e.engine._sync_exit_pending_status(SYMBOL, e.engine._orders())
    e.engine._place_exit_order(e.engine._positions()[0], SYMBOL, reason)
    runner._reconcile(SYMBOL, START + timedelta(minutes=6, seconds=1))
    response = runner._submit_reduction(
        SYMBOL, START + timedelta(minutes=6, seconds=1), reason, hard=hard
    )
    replacement = runner.broker.orders[response.broker_order_id]
    assert e.sdk.calls[-1]["order_type"] == replacement["order_type"] == "MARKET"
    assert e.sdk.calls[-1]["quantity"] == replacement["quantity"] == 6
    assert len(e.sdk.calls) == 4


def test_repaired_control_uses_shared_normal_order_policy(tmp_path):
    seed = _runner(tmp_path)
    managed = seed.positions[SYMBOL]
    runner = LegacyControlRunner(broker=seed.broker, coordinator=seed.coordinator)
    runner.register_position(thesis=managed.thesis, state=managed.state)
    runner.broker.mark_price(SYMBOL, 110, START)
    runner.on_event(START)
    reduction = next(
        o for o in runner.broker.orders.values() if o["role"] == "REDUCTION"
    )
    assert reduction["order_type"] == "LIMIT"
    assert reduction["price"] == 108.9
    manifest = runner.finish()["manifest"]
    assert manifest["reduction_policy"]["working_timeout_seconds"] == 15
    assert manifest["timeout_observation"] == "PROVIDED_EVENTS_AFTER_CANDLE_EXECUTION"


def test_hard_escalation_replaces_working_normal_limit_before_deadline(
    lifecycle, tmp_path
):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.sdk.quote_price = 98
    position = e.engine._positions()[0]
    e.engine._place_exit_order(position, SYMBOL, "THESIS_BREAKOUT_FAILED")
    live_intent = trade["exit_intent_id"]
    runner = _runner(tmp_path)
    runner.broker.mark_price(SYMBOL, 98, START)
    result = runner._submit_reduction(SYMBOL, START, "THESIS_BREAKOUT_FAILED")
    runner.positions[SYMBOL].coordinator_intent_id = result.intent_id
    e.engine._place_exit_order(position, SYMBOL, "Stop Loss")
    escalated = runner._submit_reduction(
        SYMBOL, START + timedelta(seconds=1), "Stop Loss", hard=True
    )
    assert escalated.intent_id == result.intent_id
    assert trade["exit_intent_id"] == live_intent
    assert (
        runner.coordinator.journal.get_order_intent(result.intent_id)["intent_type"]
        == "FLATTEN"
    )
    assert e.journal.get_order_intent(live_intent)["intent_type"] == "FLATTEN"
    assert (
        runner.broker.orders[escalated.broker_order_id]["order_type"]
        == e.sdk.calls[-1]["order_type"]
        == "MARKET"
    )
    assert (
        runner.broker.orders[result.broker_order_id]["status"]
        == e.sdk.book[2]["status"]
        == "CANCELLED"
    )
