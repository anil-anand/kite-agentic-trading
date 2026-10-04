"""Adversarial quote bursts, stale epochs and independent risk supervision."""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.models import (
    LifecycleEvent,
    ManagementState,
    reduce_lifecycle,
)
from backend.journal import journal
from backend.tests.test_exit_live_integration import START, _managed_engine
from backend.ticker import TickerManager


def _quote(mark, at, **changes):
    return {
        "tradingsymbol": "RELIANCE",
        "instrumentToken": "1",
        "exchange": "NSE",
        "lastPrice": mark,
        "observedAt": at.isoformat(),
        "receivedAt": at.isoformat(),
        "timestampQuality": "EXCHANGE",
        **changes,
    }


def _pin_confirmation(engine, key, mode="shadow"):
    state = engine._phase5_state_for(key)
    memory = ManagementState(failure_count=1, eligible_completed_bars=2)
    candidate = replace(state, thesis_health="WEAKENING").to_dict()
    engine._commit_phase5_checkpoint(
        position_key=key,
        state=reduce_lifecycle(
            state,
            LifecycleEvent.STATE_OBSERVED,
            event_id="test:confirmation",
            occurred_at=START,
        ),
        event_id="test:confirmation",
        event_type="TEST_OBSERVATION",
        details={},
        counters={
            "exit_policy": memory.to_dict(),
            "shadow_candidate_position_state": candidate,
            "exit_shadow_assessment_key": "completed-bar",
            "exit_policy_mode": mode,
        },
    )
    return candidate


def test_quote_burst_coalesces_policy_events_without_advancing_candles(monkeypatch):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    _pin_confirmation(engine, thesis.position_key)
    at = START + timedelta(minutes=1)
    monkeypatch.setattr(module, "now_utc", lambda: at)

    def forbidden(*args, **kwargs):
        pytest.fail("shadow quotes must not fetch candles or submit orders")

    monkeypatch.setattr(engine, "_get_tick_size", lambda *a: 0.05)
    monkeypatch.setattr(module.scanner, "get_market_context", forbidden)
    monkeypatch.setattr(engine._order_lifecycle, "submit", forbidden)
    for mark in [104.0, 96.0, 100.0] * 1000:
        engine.enqueue_quote_event(_quote(mark, at))
    assert len(engine._quote_events) == 1
    engine._consume_quote_events()
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    memory = checkpoint["counters"]["exit_policy"]
    assert memory["observed_mfe_r"] == 0.8
    assert memory["observed_mae_r"] == 0.8
    assert memory["failure_count"] == 1
    assert memory["eligible_completed_bars"] == 2
    assert (
        checkpoint["counters"]["shadow_candidate_position_state"][
            "latched_exit_intent_id"
        ]
        is None
    )
    assert checkpoint["counters"]["exit_shadow_assessment_key"] == "completed-bar"
    assert checkpoint["state"]["exposure"] == "OPEN"
    decisions = journal.get_exit_decisions(thesis.position_key)
    assert len(decisions) == 2
    assert all(row["payload"]["action"] == "HOLD" for row in decisions)
    assert len(journal.get_position_quote_observations(thesis.position_key)) == 2

    # Identical quote events must not consume another durable checkpoint.
    before = checkpoint["state_version"]
    for mark in (104.0, 96.0):
        engine.enqueue_quote_event(_quote(mark, at))
    engine._consume_quote_events()
    assert journal.get_managed_position(thesis.position_key)["state_version"] == before


@pytest.mark.parametrize(
    "changes",
    [
        {"instrumentToken": "999"},
        {"instrumentToken": None},
        {"exchange": "BSE"},
        {"observedAt": None},
        {"observedAt": (START - timedelta(minutes=3)).isoformat()},
        {"observedAt": (START + timedelta(minutes=3)).isoformat()},
        {"lastPrice": float("nan")},
    ],
)
def test_unusable_quotes_do_not_enter_observation_buffer(monkeypatch, changes):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    at = START + timedelta(minutes=1)
    monkeypatch.setattr(module, "now_utc", lambda: at)
    engine.enqueue_quote_event(_quote(120.0, at, **changes))
    assert engine._quote_events == {}
    assert journal.get_managed_position(thesis.position_key)["state_version"] == 0


def test_replaced_position_epoch_discards_queued_quotes(monkeypatch):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    at = START + timedelta(minutes=1)
    monkeypatch.setattr(module, "now_utc", lambda: at)
    engine.enqueue_quote_event(_quote(120.0, at))
    engine.active_trades["RELIANCE"]["exit_management_position_key"] = "new-epoch"
    engine._consume_quote_events()
    assert journal.get_managed_position(thesis.position_key)["state_version"] == 0


def test_quote_persistence_never_runs_on_hard_risk_cycle(monkeypatch):
    import backend.trading_engine as module

    engine, _ = _managed_engine()
    monkeypatch.setattr(module, "now_utc", lambda: START)
    monkeypatch.setattr(
        module.risk_manager, "rotate_session_if_verified", lambda *a, **k: False
    )
    monkeypatch.setattr(engine, "_has_residual_obligations", lambda: True)
    monkeypatch.setattr(engine, "_consume_broker_events", lambda: None)
    monitored = []
    monkeypatch.setattr(engine, "monitor_positions", lambda: monitored.append(True))
    monkeypatch.setattr(
        engine,
        "_consume_quote_events",
        lambda: pytest.fail("quote IO on hard-risk worker"),
    )
    engine._supervise_hard_risk_once()
    assert monitored == [True]


def test_managed_ticker_subscription_survives_renderer_unsubscribe():
    ticker = TickerManager()
    ticker._dev = True
    ticker.subscribe([1, 2])
    ticker.set_managed_tokens({1, 3})
    ticker.unsubscribe(["1", "2"])
    assert ticker.tokens == {1, 3}
    ticker.subscribe([2])
    ticker.set_managed_tokens(set())
    assert ticker.tokens == {2}


def test_quote_worker_subscribes_every_owned_managed_position(monkeypatch):
    import backend.ticker as ticker_module

    engine, _ = _managed_engine()
    ticker = TickerManager()
    ticker._dev = True
    monkeypatch.setattr(ticker_module, "ticker_manager", ticker)
    engine._supervision_active = True

    def one_cycle():
        engine._supervision_active = False

    monkeypatch.setattr(engine, "_consume_quote_events", one_cycle)
    monkeypatch.setattr(engine._supervisor_stop, "wait", lambda seconds: None)
    engine._quote_observation_loop()
    assert ticker.tokens == {1}


def test_pre_entry_quote_does_not_establish_extrema(monkeypatch):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    at = START + timedelta(seconds=5)
    monkeypatch.setattr(module, "now_utc", lambda: at)
    engine.enqueue_quote_event(_quote(120.0, START - timedelta(seconds=5)))
    engine._consume_quote_events()
    assert journal.get_managed_position(thesis.position_key)["state_version"] == 0


@pytest.mark.parametrize("mode", ["legacy_control", "invalid", {"unhashable": "mode"}])
def test_durable_control_mode_ignores_quote_policy_observations(monkeypatch, mode):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    _pin_confirmation(engine, thesis.position_key, mode=mode)
    before = journal.get_managed_position(thesis.position_key)["state_version"]
    at = START + timedelta(minutes=1)
    monkeypatch.setattr(module, "now_utc", lambda: at)
    # An in-memory mode edit cannot override the durable per-position pin.
    engine.active_trades["RELIANCE"]["exit_policy_mode"] = "shadow"
    engine.enqueue_quote_event(_quote(120.0, at))
    engine._consume_quote_events()
    assert journal.get_managed_position(thesis.position_key)["state_version"] == before


def test_two_failure_bars_confirm_despite_interspersed_quote_burst(monkeypatch):
    import backend.trading_engine as module
    from backend.tests.exit_management.test_engine import _context
    from backend.tests.test_exit_live_integration import _position

    engine, thesis = _managed_engine()
    current = {
        "now": START + timedelta(minutes=5),
        "context": _context(START, close=98.0),
    }
    monkeypatch.setattr(module, "now_utc", lambda: current["now"])
    monkeypatch.setattr(
        module.scanner, "get_market_context", lambda *a, **k: current["context"]
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *a: 0.05)
    trade = engine.active_trades["RELIANCE"]
    engine._evaluate_shadow_position(_position(98.0, current["now"]), trade)
    memory = journal.get_managed_position(thesis.position_key)["state"]["counters"][
        "exit_policy"
    ]
    assert memory["failure_count"] == 1
    current["now"] += timedelta(seconds=30)
    for mark in (99.0, 101.0, 97.5, 98.0):
        engine.enqueue_quote_event(_quote(mark, current["now"]))
    engine._consume_quote_events()
    memory = journal.get_managed_position(thesis.position_key)["state"]["counters"][
        "exit_policy"
    ]
    assert memory["failure_count"] == 1
    current["now"] = START + timedelta(minutes=10)
    current["context"] = _context(START + timedelta(minutes=5), close=97.0)
    engine._evaluate_shadow_position(_position(97.0, current["now"]), trade)
    decisions = journal.get_exit_decisions(thesis.position_key)
    assert len(decisions) == 4
    assert decisions[-1]["payload"]["primary_reason_code"] == "THESIS_BREAKOUT_FAILED"
    assert decisions[-1]["payload"]["action"] == "REQUEST_EXIT"


@pytest.mark.parametrize("event", ["quote", "poll_target", "daily_loss", "operator"])
def test_same_candle_price_and_hard_risk_events_reach_shadow_policy(monkeypatch, event):
    import backend.trading_engine as module
    from backend.tests.exit_management.test_engine import _context
    from backend.tests.test_exit_live_integration import _position

    engine, thesis = _managed_engine()
    at = START + timedelta(minutes=5)
    context = _context(START, close=98)
    monkeypatch.setattr(module, "now_utc", lambda: at)
    monkeypatch.setattr(module.risk_manager, "kill_switch_active", False)
    monkeypatch.setattr(module.scanner, "get_market_context", lambda *a, **k: context)
    monkeypatch.setattr(engine, "_get_tick_size", lambda *a: 0.05)
    monkeypatch.setattr(
        engine._order_lifecycle,
        "submit",
        lambda *a, **k: pytest.fail("shadow dispatch"),
    )
    trade = engine.active_trades["RELIANCE"]
    engine._evaluate_shadow_position(_position(98, at), trade)
    assert (
        journal.get_managed_position(thesis.position_key)["state"]["counters"][
            "exit_policy"
        ]["failure_count"]
        == 1
    )
    at += timedelta(seconds=1)
    if event == "daily_loss":
        monkeypatch.setattr(module.risk_manager, "kill_switch_active", True)
    if event == "operator":
        engine._operator_close_keys.add(thesis.position_key.rsplit(":", 1)[0])

    def observe():
        if event == "quote":
            engine.enqueue_quote_event(_quote(111, at))
            engine._consume_quote_events()
        else:
            engine._evaluate_shadow_position(
                _position(111 if event == "poll_target" else 98, at), trade
            )

    observe()
    decisions = journal.get_exit_decisions(thesis.position_key)
    assert len(decisions) == 2
    assert decisions[-1]["payload"]["action"] == "REQUEST_EXIT"
    expected = {
        "daily_loss": "RISK_DAILY_LOSS",
        "operator": "OPERATOR_POSITION_CLOSE",
    }.get(event, "PROFIT_FIXED_OBJECTIVE_REACHED")
    assert decisions[-1]["payload"]["primary_reason_code"] == expected
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["state"]["exposure"] == "OPEN"
    assert checkpoint["counters"]["exit_policy"]["failure_count"] == 1
    assert checkpoint["counters"]["exit_policy"]["eligible_completed_bars"] == 1
    observe()
    assert len(journal.get_exit_decisions(thesis.position_key)) == 2
