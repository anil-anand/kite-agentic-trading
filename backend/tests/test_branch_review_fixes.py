"""Integration regressions from the performance branch review."""

import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import backend.trading_engine as te
from backend.analytics import TradeAnalytics
from backend.broker_models import ExecutionNamespace
from backend.risk_manager import RiskManager
from backend.risk_rules import HardRiskReason
from backend.tests.test_phase2_engine_recovery import cancel_terminal, open_trade
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.tests.test_review5_lifecycle import unknown_entry
from backend.time_utils import now_utc
from backend.trading_engine import TradingEngine


def test_blocked_fills_do_not_delay_initial_stop_or_new_daily_loss(lifecycle):
    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.sdk.position_rows[0]["unrealised"] = -100_000
    entered, release, protected = (threading.Event() for _ in range(3))
    trades, place = e.sdk.trades, e.sdk.place_order

    def blocked():
        entered.set()
        assert release.wait(5)
        return trades()

    def observe_protection(**kwargs):
        result = place(**kwargs)
        if kwargs["order_type"] == "SL":
            protected.set()
        return result

    e.sdk.trades = blocked
    e.sdk.place_order = observe_protection
    worker = threading.Thread(target=e.engine.monitor_positions)
    worker.start()
    try:
        assert protected.wait(2)
        assert entered.wait(2)
        assert e.risk.kill_switch_active
        assert not release.is_set()
        assert e.risk.reconciliation_status != "RECONCILED"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_blocked_quotes_do_not_delay_emergency_reduction(lifecycle):
    e = lifecycle
    open_trade(e)
    e.sdk.cancel_order = cancel_terminal(e, [])
    entered, release, reduced = (threading.Event() for _ in range(3))
    quote, place = e.sdk.quote, e.sdk.place_order

    def blocked(instruments):
        entered.set()
        assert release.wait(5)
        return quote(instruments)

    def observe_reduction(**kwargs):
        result = place(**kwargs)
        if kwargs["order_type"] == "MARKET":
            reduced.set()
        return result

    e.sdk.quote = blocked
    e.sdk.place_order = observe_reduction
    e.engine._hard_flatten_reason = HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value
    worker = threading.Thread(target=e.engine._dispatch_hard_reductions)
    worker.start()
    try:
        assert entered.wait(1)
        assert reduced.wait(3)
        assert not release.is_set()
        assert e.sdk.calls[-1]["quantity"] == 10
    finally:
        release.set()
        worker.join(5)
        pending = e.client._quote_read
        if pending:
            assert pending["done"].wait(2)
            e.client._quote_read = None


@pytest.mark.parametrize("cnc_first", [True, False])
def test_normal_worker_uses_exact_owner_before_consuming_rejection(
    lifecycle, monkeypatch, cnc_first
):
    e = lifecycle
    trade = open_trade(e)
    e.sdk.quote_price = 103
    cnc = dict(e.sdk.position_rows[0], product="CNC", quantity=3)
    e.sdk.position_rows.insert(0 if cnc_first else 1, cnc)
    e.sdk.cancel_order = cancel_terminal(e, [])
    frame = pd.DataFrame([{"high": 105, "low": 100, "close": 103}] * 21)
    frame.loc[20, "high"] = 106
    context = SimpleNamespace(
        normal_decision_eligible=True,
        primary_frame=lambda: frame,
        primary_bar=SimpleNamespace(start=now_utc()),
    )
    monkeypatch.setattr(te.scanner, "get_market_context", lambda *a: context)
    monkeypatch.setattr(
        e.engine, "_record_legacy_control_observation", lambda *a, **k: None
    )
    monkeypatch.setattr(
        e.engine, "_reevaluate_positions", e.engine._supervisor_stop.set
    )
    e.engine._last_positions = e.engine._positions()
    e.engine._supervision_active = True
    e.engine._normal_management_loop()
    assert not trade.get("ownership_quarantined")
    assert e.sdk.calls[-1]["product"] == "MIS"
    assert e.sdk.calls[-1]["order_type"] == "LIMIT"
    assert len(e.sdk.calls) == 3
    trade["exit_reason"] = HardRiskReason.RISK_CATASTROPHIC_STOP.value
    e.engine._dispatch_hard_reductions()
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
    assert e.sdk.calls[-1]["product"] == "MIS"
    assert cnc["quantity"] == 3


def test_other_product_close_partial_restart_keeps_mis_protected(
    lifecycle, monkeypatch
):
    e = lifecycle
    trade = open_trade(e)
    cnc = dict(e.sdk.position_rows[0], product="CNC", quantity=6)
    e.sdk.position_rows.append(cnc)
    key = next(
        p["position_key"] for p in e.engine._positions() if p["product"] == "CNC"
    )
    monkeypatch.setattr(e.engine, "_activate_supervision", lambda: None)
    assert e.engine.request_operator_close(key)["accepted"]
    e.engine._dispatch_hard_reductions()
    assert e.sdk.calls[-1]["product"] == "CNC"
    assert e.sdk.calls[-1]["quantity"] == 6
    e.sdk.book[-1].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
    cnc["quantity"] = 4
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    restarted._dispatch_hard_reductions()
    assert e.sdk.calls[-1]["quantity"] == 4
    assert e.sdk.calls[-1]["product"] == "CNC"
    e.sdk.book[-1].update(status="COMPLETE", filled_quantity=4, pending_quantity=0)
    cnc["quantity"] = 0
    restarted._dispatch_hard_reductions()
    restarted._settle_control_obligations()
    assert key not in restarted._operator_close_keys
    assert not restarted._scoped_reductions.owners
    assert (
        restarted.active_trades["RELIANCE"]["position_epoch"] == trade["position_epoch"]
    )
    assert not restarted.active_trades["RELIANCE"].get("ownership_quarantined")
    assert e.sdk.book[1]["status"] == "TRIGGER PENDING"
    intents = e.journal.get_position_order_intents(
        key
        + ":"
        + next(
            row["position_key"].rsplit(":", 1)[1]
            for row in e.journal._get_conn().execute(
                "SELECT position_key FROM order_intents WHERE reason = ?",
                (HardRiskReason.OPERATOR_POSITION_CLOSE.value,),
            )
        )
    )
    assert len(intents) == 1
    assert len(e.sdk.calls) == 4


def test_account_switch_selects_independent_latch_fees_counts_and_cooldown(
    lifecycle, monkeypatch
):
    import backend.main as main

    e = lifecycle
    e.risk.bind_account(ExecutionNamespace.LIVE, "acct-A")
    e.risk.kill_switch_active = True
    e.risk.incurred_fees = 123
    e.risk._save_state()
    now = now_utc()
    e.journal.open_trade(
        trade_id="A-trade",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        direction="BUY",
        strategy="test",
        entry_price=100,
        quantity=1,
        stop_loss=95,
        target=110,
        entry_time=now,
        namespace="LIVE",
        account_id="acct-A",
        instrument_id="111",
    )
    e.journal._get_conn().execute(
        "UPDATE trades SET status='CLOSED', exit_time=? WHERE id='A-trade'",
        (now.isoformat(),),
    )
    e.journal._get_conn().commit()
    e.engine._latch_account_flatten(HardRiskReason.RISK_DAILY_LOSS.value)
    e.engine._settle_control_obligations()
    e.engine._control_state_scope = ("LIVE", "acct-A")
    e.engine._control_state_loaded = True
    e.engine._supervision_active = True
    monkeypatch.setattr(main, "kite_client", e.client)
    monkeypatch.setattr(main, "risk_manager", e.risk)
    monkeypatch.setattr(main, "trading_engine", e.engine)
    monkeypatch.setattr(e.client, "access_token", None)
    main._install_candidate_session(e.sdk, "fixture-session", "acct-B")
    assert e.engine._hard_flatten_reason is None
    # A queued assessment made for A also cannot latch a liquidation for B.
    e.engine._latch_account_flatten(HardRiskReason.RISK_DAILY_LOSS.value)
    assert e.engine._hard_flatten_reason is None
    e.engine._latch_account_flatten(
        HardRiskReason.OPERATOR_EMERGENCY_FLATTEN.value,
        expected_scope=("LIVE", "acct-A"),
    )
    assert e.engine._hard_flatten_reason is None
    e.risk.reconcile_state(e.client.get_broker_snapshot())
    assert not e.risk.kill_switch_active
    assert e.risk.incurred_fees == 0
    assert e.risk._trade_counts()["total"] == 0
    assert e.journal.get_last_exit_time("RELIANCE", **e.risk.journal_scope()) is None
    restarted = RiskManager()
    restarted.bind_account(ExecutionNamespace.LIVE, "acct-B")
    assert not restarted.kill_switch_active
    assert restarted.incurred_fees == 0
    restarted.bind_account(ExecutionNamespace.LIVE, "acct-A")
    assert restarted.kill_switch_active
    assert restarted.incurred_fees == 123
    assert restarted._trade_counts()["total"] == 1
    assert e.journal.get_last_exit_time("RELIANCE", **restarted.journal_scope()) == now


def test_bound_entry_timestamp_repairs_after_restart(lifecycle):
    e = lifecycle
    trade = open_trade(e)
    key = trade["exit_management_position_key"]
    original = e.engine._phase5_thesis_for(key)
    assert original.fill_binding.entry_terminal_at is None
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    terminal_at = now_utc()
    e.sdk.book[0]["exchange_update_timestamp"] = terminal_at
    restarted.monitor_positions()
    repaired = restarted._phase5_thesis_for(key)
    assert repaired.fill_binding.entry_terminal_at == terminal_at.isoformat()
    assert (
        replace(repaired.fill_binding, entry_terminal_at=None) == original.fill_binding
    )
    assert repaired.revision == original.revision + 1
    restarted.monitor_positions()
    assert restarted._phase5_thesis_for(key) == repaired


def test_post_execution_quotes_are_excluded_from_closed_excursions(
    lifecycle, monkeypatch
):
    e = lifecycle
    trade = open_trade(e)
    key = trade["exit_management_position_key"]
    # The fill is already retained; keep its authoritative timestamp as origin.
    entered = e.sdk.executions[0]["fill_timestamp"]
    current = [entered + timedelta(seconds=1)]
    monkeypatch.setattr(te, "now_utc", lambda: current[0])

    def quote(mark):
        e.engine.enqueue_quote_event(
            {
                "tradingsymbol": "RELIANCE",
                "instrumentToken": "111",
                "lastPrice": mark,
                "observedAt": current[0],
                "receivedAt": current[0],
            }
        )
        e.engine._consume_quote_events()

    quote(103)
    e.sdk.cancel_order = cancel_terminal(e, [])
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Stop Loss")
    exit_at = entered + timedelta(seconds=2)
    e.sdk.book[-1].update(
        status="COMPLETE",
        filled_quantity=10,
        pending_quantity=0,
        average_price=100,
        exchange_timestamp=exit_at,
    )
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "EXIT-FILL",
            "order_id": e.sdk.book[-1]["order_id"],
            "transaction_type": "SELL",
            "average_price": 100,
            "fill_timestamp": exit_at,
        }
    )
    e.sdk.position_rows[0].update(
        quantity=0, sell_quantity=10, day_sell_quantity=10, sell_value=1000
    )
    current[0] = entered + timedelta(seconds=3)
    quote(125)
    current[0] += timedelta(seconds=1)
    quote(50)
    assert e.engine._phase5_state_for(key).known_quantity == 10
    e.engine.monitor_positions()
    analytics = TradeAnalytics(str(e.journal.db_path))
    row = e.journal.get_trade(trade["trade_id"])
    report = analytics._exit_quality_record(e.journal, row)
    assert report["metrics"]["mfe_r"] == pytest.approx(2 / 6, abs=1e-6)
    assert report["metrics"]["mae_r"] == 0
    assert (
        report["coverage"]["excursion_coverage"] == "PARTIAL_FILL_BOUNDED_OBSERVATIONS"
    )
    assert (
        TradeAnalytics(str(e.journal.db_path))._exit_quality_record(e.journal, row)[
            "metrics"
        ]
        == report["metrics"]
    )
