"""Integrated regressions for the live-trading branch review.

All broker endpoints are scripted and all persistence uses temporary storage.
"""

import sqlite3
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.analytics import TradeAnalytics
from backend.broker_models import OrderSubmissionRejected
from backend.calibration import ProbabilityCalibrator, bucket_success_probability
from backend.config import config_manager
from backend.exit_quality import calculate_exit_quality
from backend.risk_rules import HardRiskReason
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_phase2_residual_safety import _stop_fill
from backend.tests.test_phase3_controls import fill_reduction, terminal_cancellation
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import as_utc, now_utc
from backend.trading_engine import TradingEngine


def _open(env):
    env.cancelled = []

    def fill():
        env.sdk.fill_entry()
        if env.signal["direction"] == "SELL":
            env.sdk.executions[0]["transaction_type"] = "SELL"
            env.sdk.position_rows[0].update(
                quantity=-10,
                day_buy_quantity=0,
                day_sell_quantity=10,
                buy_quantity=0,
                sell_quantity=10,
                buy_value=0,
                sell_value=1010,
            )

    env.sdk.after_entry = fill
    assert env.engine.execute_signal(env.signal)
    return env.engine.active_trades["RELIANCE"]


def _mark(env, monkeypatch, price):
    monkeypatch.setattr(
        env.sdk,
        "quote",
        lambda names: {
            name: {"last_price": price, "timestamp": now_utc()} for name in names
        },
    )
    for row in env.sdk.position_rows:
        row["last_price"] = price


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("filled,status", [(10, "COMPLETE"), (4, "CANCELLED")])
@pytest.mark.parametrize("lag_fills", [False, True])
def test_terminal_stop_replacement_waits_for_consistent_residual(
    lifecycle, restart, filled, status, lag_fills
):
    env = lifecycle
    _open(env)
    _stop_fill(env, filled, status)
    if lag_fills:
        env.sdk.executions.pop()
    if restart:
        env.engine = TradingEngine()
        env.engine.mode = "confirm"
        env.engine.reconcile_active_trades()
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 2
    env.sdk.position_rows[0]["quantity"] = 10 - filled
    env.engine.monitor_positions()
    if filled < 10:
        assert len(env.sdk.calls) == 3
        assert env.sdk.calls[-1]["quantity"] == 6
        env.engine.monitor_positions()
        assert len(env.sdk.calls) == 3
    else:
        assert len(env.sdk.calls) == 2


@pytest.mark.parametrize("cnc_first", [False, True])
def test_monitor_and_restart_close_only_managed_product(lifecycle, cnc_first):
    env = lifecycle
    trade = _open(env)
    cnc = deepcopy(env.sdk.position_rows[0])
    cnc.update(product="CNC", quantity=100)
    env.sdk.position_rows.insert(0 if cnc_first else 1, cnc)
    env.engine.mode = "auto"
    env.engine.monitor_positions()
    assert not trade.get("ownership_quarantined")
    terminal_cancellation(env)
    # The owned stop closes MIS while the delivery holding stays open.
    _stop_fill(env, 10)
    next(p for p in env.sdk.position_rows if p["product"] == "MIS")["quantity"] = 0
    env.engine = TradingEngine()
    env.engine.reconcile_active_trades()
    env.engine._external_close_grace_seconds = 0
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert env.journal.get_trades()[0]["status"] == "CLOSED"
    assert cnc["quantity"] == 100
    assert len(env.sdk.calls) == 2


@pytest.mark.parametrize(
    "exchange,product", [("NSE", "CNC"), ("NFO", "NRML"), ("BSE", "MIS")]
)
def test_automatic_adoption_does_not_mutate_unsupported_exposure(
    lifecycle, exchange, product
):
    env = lifecycle
    _open(env)
    unsupported = deepcopy(env.sdk.position_rows[0])
    unsupported.update(exchange=exchange, product=product)
    env.sdk.position_rows = [unsupported]
    env.engine.active_trades = {}
    config_manager.save_active_trades({})
    env.engine.mode = "auto"
    before = len(env.journal.get_trades())
    env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert len(env.sdk.calls) == 2
    assert len(env.journal.get_trades()) == before


@pytest.mark.parametrize("restart", [False, True])
def test_supervised_entry_retains_accepted_journal_metadata(lifecycle, restart):
    env = lifecycle
    env.signal.update(
        signal_score=85,
        estimated_probability=0.7,
        calibration_sample_size=20,
        reasoning="accepted premise",
        universe_version="u1",
        screener_ranking=3,
    )
    env.engine.running = env.engine._supervision_active = True
    env.sdk.after_entry = env.sdk.fill_entry
    assert env.engine.execute_signal(env.signal)
    env.signal.update(signal_score=5, reasoning="later mutation")
    if restart:
        env.engine = TradingEngine()
        env.engine.reconcile_active_trades()
    env.engine.monitor_positions()
    row = env.journal.get_trades()[0]
    assert row["confidence"] == 85
    assert row["estimated_probability"] == 0.7
    assert row["calibration_sample_size"] == 20
    assert row["reasoning"] == "accepted premise"
    assert row["universe_version"] == "u1"
    assert row["screener_ranking"] == 3
    assert row["confluence_snapshot"] is not None
    env.engine.monitor_positions()
    assert len(env.journal.get_trades()) == 1
    terminal_cancellation(env)
    _stop_fill(env, 10)
    env.sdk.position_rows[0]["quantity"] = 0
    env.engine._external_close_grace_seconds = 0
    for _ in range(3):
        env.engine.monitor_positions()
    calibrator = ProbabilityCalibrator(str(env.journal.db_path))
    assert calibrator.get_probability("unknown", 85) == (None, 1)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_rejected_breakeven_can_retry_after_restart(lifecycle, monkeypatch, direction):
    env = lifecycle
    if direction == "SELL":
        env.signal.update(direction="SELL", stopLoss=105, target=90)
        env.sdk.quote_price = 102
    trade = _open(env)
    modifications = []

    def reject(**kwargs):
        modifications.append(kwargs)
        raise OrderSubmissionRejected("trigger no longer below market")

    monkeypatch.setattr(env.sdk, "modify_order", reject, raising=False)
    # No directional room: no request is dispatched.
    assert not env.engine._tighten_to_breakeven("RELIANCE")
    assert modifications == []
    _mark(env, monkeypatch, 103 if direction == "BUY" else 99)
    assert not env.engine._tighten_to_breakeven("RELIANCE")
    assert len(modifications) == 1
    assert trade["sl"] == (95 if direction == "BUY" else 105)
    assert "requested_stop_trigger" not in trade
    durable = env.journal.get_order_intent(trade["protection_intent_id"])
    assert "requested_stop_trigger" not in durable["payload"]["recovery_trade"]
    env.engine = TradingEngine()
    env.engine.reconcile_active_trades()

    def accept(**kwargs):
        modifications.append(kwargs)
        env.sdk.book[1].update(
            trigger_price=kwargs["trigger_price"], price=kwargs["price"]
        )
        return "O2"

    monkeypatch.setattr(env.sdk, "modify_order", accept)
    assert env.engine._tighten_to_breakeven("RELIANCE")
    assert env.engine.active_trades["RELIANCE"]["sl"] == 101
    assert not env.engine._tighten_to_breakeven("RELIANCE")
    assert len(modifications) == 2


def test_completed_breakeven_review_advances_schedule(lifecycle, monkeypatch):
    import backend.trading_engine as module
    from backend.backtesting.legacy_control import LegacyControlRunner
    from backend.tests.exit_management.test_engine import _bar, _context
    from backend.tests.test_candidate_execution import START, SYMBOL, _runner

    env = lifecycle
    trade = _open(env)
    start = START
    trade["entry_time"] = start
    reviews = []
    exits = []
    clock = [start]
    _mark(env, monkeypatch, 103)
    monkeypatch.setattr(module, "now_utc", lambda: clock[0])
    monkeypatch.setattr(env.engine, "_has_fresh_position_mark", lambda p: True)

    def evaluate(*args):
        reviews.append(clock[0])
        opposing = (clock[0] - start).total_seconds() >= 65 * 60
        return {
            "assessment_available": True,
            "buy_signals": 0 if opposing else 1,
            "sell_signals": 2 if opposing else 0,
        }

    monkeypatch.setattr(module.scanner, "evaluate_position", evaluate)
    monkeypatch.setattr(env.engine, "_tighten_to_breakeven", lambda symbol: False)
    monkeypatch.setattr(
        env.engine, "_exit_position", lambda *args: exits.append(clock[0])
    )
    seed = _runner(env.journal.db_path.parent)
    managed = seed.positions[SYMBOL]
    research = LegacyControlRunner(
        broker=seed.broker,
        coordinator=seed.coordinator,
        legacy_policy={"level_lookback": 100},
        strategies={
            "fixture": SimpleNamespace(
                calculate_signals=lambda *_: (
                    [{"direction": "SELL"}, {"direction": "SELL"}]
                    if (clock[0] - start).total_seconds() >= 65 * 60
                    else [{"direction": "BUY"}]
                )
            )
        },
        strategy_config={"fixture": {"enabled": True}},
    )
    research.register_position(thesis=managed.thesis, state=managed.state)
    for minute, second in ((30, 0), (60, 0), (60, 1), (65, 0), (90, 0)):
        clock[0] = start + timedelta(minutes=minute, seconds=second)
        env.engine._reevaluate_positions()
        bar_start = start + timedelta(minutes=minute - 5)
        context = replace(
            _context(
                bar_start,
                103,
                bars=tuple(
                    _bar(bar_start - timedelta(minutes=5 * i), 103)
                    for i in range(49, 0, -1)
                ),
            ),
            instrument_id=f"SIM-{SYMBOL}",
        )
        research.broker.mark_price(SYMBOL, 103, clock[0])
        research.on_event(clock[0], contexts={SYMBOL: context})
    assert reviews == [start + timedelta(minutes=m) for m in (30, 60, 90)]
    assert exits == [start + timedelta(minutes=90)]
    assert [
        r["at"] for r in research.legacy_records if r["supporting"] is not None
    ] == reviews
    assert [
        r["at"] for r in research.legacy_records if r["action"] == "REQUEST_EXIT"
    ] == exits


def test_supervised_loss_changes_next_admission_like_research(lifecycle, monkeypatch):
    import backend.trading_engine as module

    env = lifecycle
    research_rows = []
    for index in range(11):
        exit_price = 110 if index < 7 else 99
        env.journal.open_trade(
            trade_id=f"history-{index}",
            tradingsymbol="HISTORY",
            exchange="NSE",
            direction="BUY",
            product="MIS",
            strategy="unknown",
            entry_price=100,
            quantity=10,
            stop_loss=95,
            target=110,
            signal_score=85,
        )
        pnl = 10 * (exit_price - 100)
        env.journal.close_trade(
            f"history-{index}",
            exit_price,
            "Target",
            cost_details={
                "gross_pnl": pnl,
                "net_pnl": pnl,
                "financial_quality": "RECONCILED",
            },
        )
        research_rows.append(
            dict(direction="BUY", entry_price=100, stop_loss=95, exit_price=exit_price)
        )
    calibrator = ProbabilityCalibrator(str(env.journal.db_path))
    probability, samples = calibrator.get_probability("unknown", 85)
    assert probability > 0.6
    env.signal.update(
        signal_score=85,
        estimated_probability=probability,
        calibration_sample_size=samples,
    )
    env.engine.running = env.engine._supervision_active = True
    _open(env)
    env.engine.monitor_positions()
    fill_reduction(env, env.engine.active_trades["RELIANCE"]["stop_order_id"])
    env.engine._external_close_grace_seconds = 0
    for _ in range(3):
        env.engine.monitor_positions()
    research_rows.append(
        dict(direction="BUY", entry_price=101, stop_loss=95, exit_price=100)
    )
    expected = bucket_success_probability(research_rows)
    assert calibrator.get_probability("unknown", 85) == expected
    assert expected == (7 / 12, 12)
    assert expected[0] < 0.6

    # Feed the recalibrated signal through the real live batch admission gate.
    admission = TradingEngine()
    admission.dynamic_watchlist = ["RELIANCE"]
    admission.last_universe_refresh_time = now_utc()
    monkeypatch.setattr(admission, "_get_current_refresh_interval", lambda: 60)
    submitted = []
    monkeypatch.setattr(admission, "execute_signal", submitted.append)

    def scan(symbols, on_signal, **kwargs):
        on_signal(
            {
                **env.signal,
                "estimated_probability": expected[0],
                "calibration_sample_size": expected[1],
            }
        )

    monkeypatch.setattr(module.scanner, "scan_watchlist", scan)
    admission.scan_and_trade()
    assert submitted == []


@pytest.mark.parametrize("failure", ["intent", "attempt", "accepted"])
@pytest.mark.parametrize("restart", [False, True])
def test_storage_failure_keeps_one_hard_reduction_through_rebound(
    lifecycle, monkeypatch, failure, restart
):
    env = lifecycle
    _open(env)
    terminal_cancellation(env)
    name = {
        "intent": "create_order_intent",
        "attempt": "prepare_order_attempt",
        "accepted": "record_order_attempt_state",
    }[failure]
    original = getattr(env.journal, name)

    def disk_full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(env.journal, name, disk_full)
    position = env.engine._positions()[0]
    env.engine._place_exit_order(position, "RELIANCE", "Stop Loss")
    assert env.engine.active_trades["RELIANCE"]["exit_pending"]
    assert len(env.sdk.calls) == 3
    assert env.sdk.calls[-1]["quantity"] == 10
    assert env.sdk.calls[-1]["order_type"] == "MARKET"
    env.engine._place_exit_order(position, "RELIANCE", "Stop Loss")
    assert len(env.sdk.calls) == 3
    monkeypatch.setattr(env.journal, name, original)
    _mark(env, monkeypatch, 103)
    if restart:
        env.engine = TradingEngine()
        env.engine.mode = "confirm"
        env.engine.reconcile_active_trades()
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    assert env.engine.active_trades["RELIANCE"]["exit_reason"] == "Stop Loss"
    fill_reduction(env, "O3")
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert len(env.sdk.calls) == 3


def test_no_storage_retains_preparation_and_retries_after_rebound(
    lifecycle, monkeypatch
):
    env = lifecycle
    _open(env)
    terminal_cancellation(env)
    with monkeypatch.context() as failing:

        def fail(*args, **kwargs):
            raise OSError("disk full")

        failing.setattr(env.journal, "create_order_intent", fail)
        failing.setattr(config_manager, "save_active_trades", fail)
        env.engine._place_exit_order(
            env.engine._positions()[0], "RELIANCE", "Stop Loss"
        )
        env.engine._sync_exit_pending_status("RELIANCE")
        assert env.engine.active_trades["RELIANCE"]["exit_pending"]
        assert env.engine.active_trades["RELIANCE"]["exit_preparation_failed"]
        assert len(env.sdk.calls) == 2
    _mark(env, monkeypatch, 103)
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    assert env.sdk.calls[-1]["order_type"] == "MARKET"


@pytest.mark.parametrize("trigger", ["emergency", "deadline"])
def test_hard_dispatch_proceeds_while_accounting_history_is_blocked(
    lifecycle, monkeypatch, trigger
):

    env = lifecycle
    _open(env)
    terminal_cancellation(env)
    entered, release, dispatched = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_trades = env.sdk.trades
    original_place = env.sdk.place_order

    def blocked_fills():
        entered.set()
        assert release.wait(5)
        return original_trades()

    def place(**kwargs):
        result = original_place(**kwargs)
        if kwargs["transaction_type"] == "SELL" and kwargs["order_type"] == "MARKET":
            dispatched.set()
        return result

    monkeypatch.setattr(env.sdk, "trades", blocked_fills)
    monkeypatch.setattr(env.sdk, "place_order", place)
    monkeypatch.setattr(env.engine, "_activate_supervision", lambda: None)
    observed = SessionClock(SessionPolicy()).snapshot(as_utc("2026-09-21T09:44:00Z"))
    clock = [observed]
    monkeypatch.setattr(
        env.engine,
        "_session_clock",
        lambda: SimpleNamespace(snapshot=lambda _: clock[0]),
    )
    risk_config = dict(config_manager.get_risk_config(), supervisorIntervalSeconds=0.25)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: risk_config)
    env.engine._supervision_active = True
    supervisor = threading.Thread(target=env.engine._supervisor_loop)
    supervisor.start()
    try:
        assert entered.wait(1)
        if trigger == "emergency":
            assert env.engine.request_emergency_flatten("account")["accepted"]
        else:
            clock[0] = SessionClock(SessionPolicy()).snapshot(
                as_utc("2026-09-21T09:46:00Z")
            )
        env.engine._supervisor_wakeup.set()
        assert dispatched.wait(1.5)
        assert not release.is_set()
        assert env.sdk.calls[-1]["quantity"] == 10
        assert env.sdk.book[1]["status"] == "CANCELLED"
        assert len(env.sdk.calls) == 3
        if trigger == "deadline":
            assert (
                env.engine._hard_flatten_reason
                == HardRiskReason.SESSION_FORCED_FLAT.value
            )
    finally:
        env.engine._supervision_active = False
        env.engine._supervisor_stop.set()
        env.engine._supervisor_wakeup.set()
        release.set()
        supervisor.join(2)
        for worker in (
            env.engine._management_thread,
            env.engine._critical_management_thread,
        ):
            if worker:
                worker.join(2)


@pytest.mark.parametrize("restart", [False, True])
def test_partial_entry_quotes_survive_binding_and_preserve_exposed_quantity(
    lifecycle, monkeypatch, restart
):
    env = lifecycle
    env.cancelled = []
    config = config_manager.get_effective_exit_management_config()
    config = deepcopy(config)
    config.setdefault("exitManagement", {})["livePolicyMode"] = "shadow"
    monkeypatch.setattr(
        config_manager, "get_effective_exit_management_config", lambda: config
    )
    env.engine.running = env.engine._supervision_active = True
    env.sdk.after_entry = lambda: env.sdk.fill_entry(4, "OPEN")
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    key = trade["exit_management_position_key"]
    first = as_utc(
        env.journal.get_position_thesis(key)["payload"]["created_at"]
    ) + timedelta(seconds=1)
    env.sdk.executions[0]["fill_timestamp"] = first
    env.engine.monitor_positions()
    observations = [
        {"mark": 200, "observed_at": first - timedelta(milliseconds=500)},
        {"mark": 107, "observed_at": first + timedelta(seconds=1)},
        {"mark": 89, "observed_at": first + timedelta(seconds=2)},
    ]
    env.engine._record_shadow_quote_observation(
        key, trade=trade, observations=observations
    )
    if restart:
        env.engine = TradingEngine()
        env.engine.mode = "confirm"
        env.engine.reconcile_active_trades()
    env.sdk.fill_entry(10)
    terminal = first + timedelta(seconds=3)
    env.sdk.executions[-1]["fill_timestamp"] = terminal
    env.sdk.book[0]["exchange_timestamp"] = terminal
    terminal_cancellation(env)
    env.engine.monitor_positions()
    checkpoint = env.journal.get_managed_position(key)["state"]
    memory = checkpoint["counters"]["exit_policy"]
    assert memory["observed_mfe_r"] == 1
    assert memory["observed_mae_r"] == 2
    assert memory["eligible_completed_bars"] == 0
    thesis = env.journal.get_position_thesis(key)["payload"]
    path = TradeAnalytics._retained_exposure_path(
        env.journal, env.journal.get_managed_position(key), thesis, []
    )
    quality = calculate_exit_quality(
        direction="BUY",
        entry_price=101,
        initial_stop=95,
        initial_quantity=10,
        exposure_path=path,
        observed_mfe_r=memory["observed_mfe_r"],
        observed_mae_r=memory["observed_mae_r"],
    )
    assert quality["metrics"]["mfe_r"] == 1
    assert quality["metrics"]["exposure_peak_r"] == 0.4
    assert quality["metrics"]["exposure_mae_r"] == 0.8


def test_unknown_degraded_submission_is_reconciled_by_same_tag_after_restart(
    lifecycle, monkeypatch
):
    env = lifecycle
    _open(env)
    terminal_cancellation(env)
    original_create = env.journal.create_order_intent
    original_place = env.sdk.place_order

    def fail_write(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    def accepted_without_ack(**kwargs):
        original_place(**kwargs)
        raise TimeoutError("ack lost")

    monkeypatch.setattr(env.journal, "create_order_intent", fail_write)
    monkeypatch.setattr(env.sdk, "place_order", accepted_without_ack)
    env.engine._place_exit_order(env.engine._positions()[0], "RELIANCE", "Stop Loss")
    outbox = env.engine.active_trades["RELIANCE"]["degraded_exit"]
    assert outbox["state"] == "UNKNOWN"
    assert env.sdk.book[-1]["tag"] == outbox["attempt_tag"]
    accepted_order = env.sdk.book.pop()
    monkeypatch.setattr(env.journal, "create_order_intent", original_create)
    monkeypatch.setattr(env.sdk, "place_order", original_place)
    env.engine = TradingEngine()
    env.engine.mode = "confirm"
    env.engine.reconcile_active_trades()
    env.engine.monitor_positions()
    assert len(env.sdk.calls) == 3
    env.sdk.book.append(accepted_order)
    env.engine.monitor_positions()
    assert env.engine.active_trades["RELIANCE"]["exit_order_id"] == "O3"
    assert len(env.sdk.calls) == 3


@pytest.mark.parametrize("cnc_first", [False, True])
@pytest.mark.parametrize("lag_fills", [False, True])
def test_manual_mis_close_cancels_live_stop_despite_cnc_holding(
    lifecycle, cnc_first, lag_fills
):
    env = lifecycle
    trade = _open(env)
    terminal_cancellation(env)
    cnc = deepcopy(env.sdk.position_rows[0])
    cnc.update(product="CNC", quantity=100)
    env.sdk.position_rows.insert(0 if cnc_first else 1, cnc)
    env.engine.mode = "auto"
    env.engine._external_close_grace_seconds = 0
    env.engine.monitor_positions()
    assert not trade.get("ownership_quarantined")
    env.sdk.place_order(
        variety="regular",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="SELL",
        quantity=10,
        product="MIS",
        order_type="MARKET",
    )
    fill_reduction(env, "O3")
    fill = env.sdk.executions.pop() if lag_fills else None
    for _ in range(3):
        env.engine.monitor_positions()
    assert env.sdk.book[1]["status"] == "CANCELLED"
    assert cnc["quantity"] == 100
    assert len(env.sdk.calls) == 3
    if fill:
        env.sdk.executions.append(fill)
        env.engine.monitor_positions()
    assert env.engine.active_trades == {}
    assert env.journal.get_trades()[0]["status"] == "CLOSED"
