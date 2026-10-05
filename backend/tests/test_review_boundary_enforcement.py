"""Offline regressions for the nine integration review findings."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.tests.test_phase2_engine_recovery import cancel_terminal, open_trade
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.tests.test_review5_lifecycle import unknown_entry


@pytest.mark.parametrize("phase", ["entry", "protected", "exit_handoff"])
def test_generic_amendment_cannot_change_owned_orders(lifecycle, monkeypatch, phase):
    import backend.main as main

    env = lifecycle
    monkeypatch.setattr(
        env.client,
        "modify_order",
        lambda **kwargs: pytest.fail("generic RPC reached broker amendment"),
    )
    if phase == "entry":
        unknown_entry(env)
        order_id = "O1"
    else:
        open_trade(env)
        order_id = "O2"
    trade = env.engine.active_trades["RELIANCE"]
    intent_id = trade["entry_intent_id" if phase == "entry" else "protection_intent_id"]
    before = env.journal.get_order_intent_projection(intent_id)
    entered, release = threading.Event(), threading.Event()
    worker = None
    if phase == "exit_handoff":
        cancel = cancel_terminal(env, [])

        def blocked_cancel(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return cancel(*args, **kwargs)

        env.sdk.cancel_order = blocked_cancel
        worker = threading.Thread(
            target=env.engine._place_exit_order,
            args=(env.engine._positions()[0], "RELIANCE", "Target"),
        )
        worker.start()
    try:
        if worker:
            assert entered.wait(2)
        response = main.handle_request(
            {
                "id": 1,
                "method": "modify_order",
                "params": {
                    "variety": "regular",
                    "order_id": order_id,
                    "quantity": 100,
                    "trigger_price": 90,
                },
            }
        )
        assert response["error"]["code"] == -32004
        assert "managed ownership" in response["error"]["message"]
        assert env.sdk.book[int(order_id[1:]) - 1]["quantity"] == 10
        assert (
            env.journal.get_order_intent_projection(intent_id)["quantity"]
            == before["quantity"]
            == 10
        )
    finally:
        release.set()
        if worker:
            worker.join(5)
            assert not worker.is_alive()


@pytest.mark.parametrize("cnc_first", [False, True])
@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_target_exit_keeps_owned_product_identity(lifecycle, cnc_first, direction):
    env = lifecycle
    env.signal.update(
        direction=direction,
        stopLoss=95 if direction == "BUY" else 105,
        target=110 if direction == "BUY" else 90,
    )

    def fill():
        env.sdk.fill_entry()
        if direction == "SELL":
            env.sdk.position_rows[0].update(
                quantity=-10,
                buy_quantity=0,
                day_buy_quantity=0,
                buy_value=0,
                sell_quantity=10,
                day_sell_quantity=10,
                sell_value=1010,
            )
            env.sdk.executions[0]["transaction_type"] = "SELL"

    env.sdk.after_entry = fill
    assert env.engine.execute_signal(env.signal)
    cnc = dict(env.sdk.position_rows[0], product="CNC", quantity=3)
    env.sdk.position_rows.insert(0 if cnc_first else 1, cnc)
    env.sdk.quote_price = 111 if direction == "BUY" else 89
    env.sdk.cancel_order = cancel_terminal(env, [])
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    reduction = env.sdk.calls[-1]
    assert reduction["product"] == "MIS"
    assert reduction["quantity"] == 10
    assert reduction["transaction_type"] == ("SELL" if direction == "BUY" else "BUY")
    assert cnc["quantity"] == 3


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("progress,age", [(-1, 0), (0, 0), (1, 0), (1, 180)])
def test_live_and_research_breakeven_require_room_beyond_fresh_mark(
    lifecycle, monkeypatch, direction, progress, age
):
    from backend.backtesting.legacy_control import LegacyControlRunner
    from backend.tests.exit_management.test_engine import _bar, _context
    from backend.tests.test_fixed_objective_barriers import _runner
    from backend.tests.test_integrated_review import _open
    from backend.time_utils import now_utc

    env = lifecycle
    sign = 1 if direction == "BUY" else -1
    env.signal.update(
        direction=direction, stopLoss=100 - sign * 5, target=100 + sign * 10
    )
    _open(env)
    expected = progress > 0 and age == 0

    def amend(**kwargs):
        env.sdk.book[1].update(
            trigger_price=kwargs["trigger_price"], price=kwargs["price"]
        )
        return "O2"

    monkeypatch.setattr(env.sdk, "modify_order", amend, raising=False)
    monkeypatch.setattr(
        env.sdk,
        "quote",
        lambda names: {
            name: {
                "last_price": 101 + sign * progress,
                "timestamp": now_utc() - timedelta(seconds=age),
            }
            for name in names
        },
    )
    assert env.engine._tighten_to_breakeven("RELIANCE") is expected

    runner = _runner(env.journal.db_path.parent, direction, LegacyControlRunner)
    runner.legacy_policy = replace(runner.legacy_policy, level_lookback=100)
    runner.strategies = {
        "fixture": SimpleNamespace(
            calculate_signals=lambda *_: [{"direction": direction}]
        )
    }
    runner.strategy_config = {"fixture": {"enabled": True}}
    start = runner.broker.positions["RELIANCE"]["entry_time"]
    at = start + timedelta(minutes=60)
    mark = 100 + sign * progress
    bar_start = at - timedelta(minutes=5)
    context = replace(
        _context(
            bar_start,
            mark,
            bars=tuple(
                _bar(bar_start - timedelta(minutes=5 * i), mark)
                for i in range(49, 0, -1)
            ),
        ),
        instrument_id="SIM-RELIANCE",
    )
    runner.broker.mark_price("RELIANCE", mark, at - timedelta(seconds=age))
    runner.on_event(at, contexts={"RELIANCE": context})
    assert (
        any(record["action"] == "TIGHTEN_STOP" for record in runner.legacy_records)
        is expected
    )
    assert runner.broker.positions["RELIANCE"]["sl"] == (
        100 if expected else 100 - sign * 5
    )
    runner.on_event(
        at + timedelta(minutes=5),
        candles={
            "RELIANCE": {
                "date": at,
                "open": mark,
                "high": mark + 0.2,
                "low": mark - 0.2,
                "close": mark,
                "volume": 1000,
            }
        },
    )
    assert not runner.broker.trades


@pytest.mark.parametrize("manual_first", [True, False])
def test_manual_scan_race_preserves_one_automatic_entry(
    lifecycle, monkeypatch, manual_first
):
    import pandas as pd

    import backend.main as main
    import backend.scanner as scanner_module
    import backend.trading_engine as engine_module
    from backend.market_context import ContextQuality
    from backend.scanner import Scanner
    from backend.screener import screener_engine
    from backend.time_utils import now_utc

    env = lifecycle
    local = Scanner()
    env.sdk.after_entry = env.sdk.fill_entry
    env.engine.mode = "auto"
    env.engine.dynamic_watchlist = ["RELIANCE"]
    env.engine.last_universe_refresh_time = now_utc()
    monkeypatch.setattr(env.engine, "_get_current_refresh_interval", lambda: 60)
    monkeypatch.setattr(local, "evaluate_position", lambda *a, **k: {})
    monkeypatch.setattr(main, "scanner", local)
    monkeypatch.setattr(engine_module, "scanner", local)
    monkeypatch.setattr(scanner_module, "kite_client", env.client)
    monkeypatch.setattr(main.config_manager, "get_watchlist", lambda: [])
    monkeypatch.setattr(main.config_manager, "get_strategy_config", lambda: {})
    monkeypatch.setattr(
        screener_engine, "generate_daily_watchlist", lambda **k: ["RELIANCE"]
    )
    at = now_utc()
    frame = pd.DataFrame([{"close": 100}])
    context = SimpleNamespace(
        primary_bar=SimpleNamespace(end=at),
        normal_decision_eligible=True,
        analysis_decision_eligible=False,
        entry_history_ready=True,
        primary_frame=lambda: frame,
        primary_quality=SimpleNamespace(issues=(), status=ContextQuality.VALID),
        decision_event_time=at,
    )
    monkeypatch.setattr(local, "_market_context", lambda *a, **k: context)
    started, release = threading.Event(), threading.Event()

    def fetch(*args):
        started.set()
        assert release.wait(5)
        return frame, False

    monkeypatch.setattr(local, "_fetch_candles", fetch)
    calculations, published = [], []

    def calculate(**kwargs):
        calculations.append(True)
        return [{**env.signal, "signal_score": 80}]

    monkeypatch.setattr(scanner_module, "evaluate_production_entries", calculate)
    monkeypatch.setattr(
        env.engine, "_push_signal", lambda signal: published.append(signal)
    )

    def manual():
        result = main.handle_request({"id": 1, "method": "scan_now"})
        assert "error" not in result
        return result["result"]

    first, second = (
        (manual, env.engine.scan_and_trade)
        if manual_first
        else (env.engine.scan_and_trade, manual)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        try:
            assert started.wait(2)
            two = pool.submit(second)
        finally:
            release.set()
        results = one.result(5), two.result(5)
    manual_signals = results[0 if manual_first else 1]
    assert len(manual_signals) == 1
    assert calculations == [True]
    assert len(published) == 1
    assert manual_signals[0]["id"] == published[0]["id"]
    manual_signals[0]["quantity"] = 1000
    assert local.scan_watchlist(["RELIANCE"])[0]["quantity"] == 10
    env.engine.scan_and_trade()
    assert len(published) == 1
    assert len(env.sdk.calls) == 2  # One entry and its native protective stop.
    assert env.sdk.calls[0]["quantity"] == 10
