"""Unknown legacy premises remain bounded without disabling existing objectives."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import as_utc, now_utc


def _seed_manual_position(env):
    env.sdk.position_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "quantity": 10,
            "average_price": 100,
            "last_price": 100,
            "day_buy_quantity": 10,
            "day_sell_quantity": 0,
            "buy_quantity": 10,
            "sell_quantity": 0,
            "buy_value": 1000,
            "sell_value": 0,
            "realised": 0,
            "unrealised": 0,
        }
    ]
    return env.engine._positions()[0]


def test_adoption_commits_unknown_checkpoint_before_protection_without_a_thesis(
    lifecycle, monkeypatch
):
    e = lifecycle
    position = _seed_manual_position(e)
    observed = []

    def establish_protection(symbol, broker_position, trade):
        key = trade["exit_management_position_key"]
        record = e.journal.get_managed_position(key)
        checkpoint = record["state"]
        assert checkpoint["state"]["exposure"] == "RECOVERY_REQUIRED"
        assert checkpoint["state"]["thesis_health"] == "UNKNOWN"
        assert checkpoint["state"]["known_quantity"] is None
        assert checkpoint["state"]["protection"] == "UNCONFIRMED"
        assert e.journal.get_position_thesis(key) is None
        assert record["thesis_id"] is None
        assert checkpoint["extrema"] == {}
        assert checkpoint["protection"]["legacy_stop_loss"] == trade["sl"]
        assert checkpoint["protection"]["legacy_target"] == trade["target"]
        observed.append(key)
        return True

    monkeypatch.setattr(e.engine, "_ensure_recovery_protection", establish_protection)
    e.engine._adopt_position(position)

    trade = e.engine.active_trades["RELIANCE"]
    assert observed == [trade["exit_management_position_key"]]
    assert trade["legacy_bounded_management"] is True
    assert trade["entry_time"] is None
    assert trade["entry_state"] == "OPEN"
    assert e.sdk.calls == []


@pytest.mark.parametrize("legacy", [False, True])
def test_bounded_legacy_suppresses_normal_votes_while_system_control_is_retained(
    lifecycle, monkeypatch, legacy
):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry()
    assert e.engine.execute_signal(e.signal)
    trade = e.engine.active_trades["RELIANCE"]
    key = trade["exit_management_position_key"]
    assert e.journal.get_position_thesis(key)["payload"]["provenance"] == "SYSTEM"
    assert not trade.get("legacy_bounded_management")
    trade["legacy_bounded_management"] = legacy
    trade["entry_time"] = now_utc() - timedelta(minutes=90)
    trade["last_reeval_time"] = trade["entry_time"]
    evaluations = []
    normal_exits = []
    contexts = []

    def evaluate(*args):
        evaluations.append(args)
        return {"assessment_available": True, "buy_signals": 0, "sell_signals": 3}

    monkeypatch.setattr("backend.trading_engine.scanner.evaluate_position", evaluate)
    monkeypatch.setattr(
        "backend.trading_engine.scanner.get_market_context",
        lambda *args: (
            contexts.append(args) or SimpleNamespace(normal_decision_eligible=False)
        ),
    )
    monkeypatch.setattr(
        e.engine, "_exit_position", lambda *args: normal_exits.append(args)
    )
    assert e.engine._check_resistance_exit("RELIANCE", 102, "BUY") is False
    e.engine._reevaluate_positions()
    assert len(contexts) == (0 if legacy else 1)
    assert len(evaluations) == (0 if legacy else 1)
    assert len(normal_exits) == (0 if legacy else 1)
    if normal_exits:
        assert "Thesis invalidated" in normal_exits[0][2]


def test_adopted_bounded_position_still_exits_at_its_fixed_target(
    lifecycle, monkeypatch
):
    e = lifecycle
    position = _seed_manual_position(e)
    monkeypatch.setattr(e.engine, "_ensure_recovery_protection", lambda *args: True)
    e.engine._adopt_position(position)
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["legacy_bounded_management"] is True
    e.sdk.position_rows[0]["last_price"] = trade["target"] + 1
    monkeypatch.setattr(
        e.sdk,
        "quote",
        lambda instruments: {
            name: {"last_price": trade["target"] + 1, "timestamp": now_utc()}
            for name in instruments
        },
    )
    target_exits = []
    monkeypatch.setattr(
        e.engine, "_place_exit_order", lambda *args: target_exits.append(args)
    )
    session = SessionClock(SessionPolicy()).snapshot(
        as_utc("2026-09-21T10:30:00+05:30")
    )
    session = replace(session, observed_at=now_utc())
    monkeypatch.setattr(
        e.engine, "_session_clock", lambda: SimpleNamespace(snapshot=lambda _: session)
    )

    e.engine.monitor_positions()

    assert len(target_exits) == 1
    assert target_exits[0][1:] == ("RELIANCE", "Target")
    assert target_exits[0][0]["quantity"] == 10
