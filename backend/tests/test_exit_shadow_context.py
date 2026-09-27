"""Live shadow decisions retain causal receipt times and frozen feature policy."""

from datetime import timedelta

import pandas as pd

from backend.journal import journal
from backend.market_context import ContextPolicy
from backend.scanner import Scanner
from backend.tests.conftest import build_candles
from backend.tests.test_exit_live_integration import START, _managed_engine, _position


def _frame(closes):
    return build_candles(
        closes,
        dates=pd.date_range(
            "2026-09-21 09:15", periods=len(closes), freq="5min", tz="Asia/Kolkata"
        ),
    )


def test_live_fetch_decides_after_receipt_without_rewriting_request_cutoff(monkeypatch):
    import backend.scanner as scanner_module

    scanner = Scanner()
    frame = _frame([100.0] * 8)
    requested_at = START + timedelta(minutes=5, seconds=1)
    clock = [requested_at]
    monkeypatch.setattr(scanner_module, "now_utc", lambda: clock[0])

    def fetch(*args):
        clock[0] += timedelta(seconds=2)
        return frame.to_dict("records")

    monkeypatch.setattr(scanner_module.kite_client, "get_historical_data", fetch)
    context = scanner.get_market_context(1, "RELIANCE", context_settings={})

    assert context.normal_decision_eligible
    assert context.decision_event_time == requested_at + timedelta(seconds=2)
    assert context.received_at == context.decision_event_time
    assert context.source_as_of == requested_at
    assert context.primary_bar.available_at == context.received_at


def test_explicit_asof_time_never_sees_a_response_received_later(monkeypatch):
    import backend.scanner as scanner_module

    scanner = Scanner()
    frame = _frame([100.0] * 8)
    requested_at = START + timedelta(minutes=5, seconds=1)
    clock = [requested_at]
    monkeypatch.setattr(scanner_module, "now_utc", lambda: clock[0])

    def fetch(*args):
        clock[0] += timedelta(seconds=2)
        return frame.to_dict("records")

    monkeypatch.setattr(scanner_module.kite_client, "get_historical_data", fetch)
    context = scanner.get_market_context(1, "RELIANCE", decision_at=requested_at)

    assert not context.normal_decision_eligible
    assert context.primary_bar is None
    assert context.decision_event_time == requested_at


def test_real_fetch_does_not_interrupt_consecutive_live_failure_confirmation(
    monkeypatch,
):
    import backend.scanner as scanner_module
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    scanner = Scanner()
    current = {"frame": _frame([100.0] * 7 + [98.0])}
    clock = [START + timedelta(minutes=5, seconds=1)]
    monkeypatch.setattr(scanner_module, "now_utc", lambda: clock[0])
    monkeypatch.setattr(engine_module, "now_utc", lambda: clock[0])
    monkeypatch.setattr(engine_module, "scanner", scanner)
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)

    def fetch(*args):
        clock[0] += timedelta(seconds=2)
        return current["frame"].to_dict("records")

    monkeypatch.setattr(scanner_module.kite_client, "get_historical_data", fetch)
    engine._evaluate_shadow_position(
        _position(100.0, clock[0]), engine.active_trades["RELIANCE"]
    )
    first = journal.get_managed_position(thesis.position_key)["state"]
    assert first["counters"]["exit_policy"]["failure_count"] == 1

    current["frame"] = _frame([100.0] * 7 + [98.0, 97.0])
    clock[0] = START + timedelta(minutes=10, seconds=1)
    engine._evaluate_shadow_position(
        _position(100.0, clock[0]), engine.active_trades["RELIANCE"]
    )

    decisions = journal.get_exit_decisions(thesis.position_key)
    assert len(decisions) == 2
    assert decisions[-1]["payload"]["primary_reason_code"] == "THESIS_BREAKOUT_FAILED"
    assert decisions[-1]["payload"]["action"] == "REQUEST_EXIT"


def test_pinned_context_settings_and_regime_survive_entry_policy_change(monkeypatch):
    import backend.market_context as context_module
    import backend.scanner as scanner_module

    scanner = Scanner()
    current = {"frame": _frame([100.0] * 8), "regime": "TRENDING"}
    clock = [START + timedelta(minutes=5)]
    pinned = {"setupRangeBars": 2, "regimeTransitionConfirmBars": 2}
    monkeypatch.setattr(scanner_module, "now_utc", lambda: clock[0])
    monkeypatch.setattr(
        scanner, "_fetch_candles", lambda *args: (current["frame"], False)
    )
    monkeypatch.setattr(
        context_module, "_raw_regime", lambda frame: (current["regime"], {})
    )
    first = scanner.get_market_context(1, "RELIANCE", context_settings=pinned)
    assert first.confirmed_regime == "TRENDING"

    current["regime"] = "RANGING"
    current["frame"] = _frame([100.0] * 9)
    clock[0] += timedelta(minutes=5)
    second = scanner.get_market_context(1, "RELIANCE", context_settings=pinned)
    assert second.transition_candidate == "RANGING"
    assert second.transition_age == 1

    monkeypatch.setattr(
        scanner_module.config_manager,
        "get_market_context_config",
        lambda: {"setupRangeBars": 99, "regimeTransitionConfirmBars": 8},
    )
    latest_entry_context = scanner.get_market_context(1, "RELIANCE")
    assert latest_entry_context.context_policy.setup_range_bars == 99

    current["frame"] = _frame([100.0] * 10)
    clock[0] += timedelta(minutes=5)
    third = scanner.get_market_context(1, "RELIANCE", context_settings=pinned)
    assert third.context_policy == ContextPolicy.from_config(pinned)
    assert third.confirmed_regime == "RANGING"
    assert third.transition_candidate is None
