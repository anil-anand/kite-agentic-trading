"""Production risk capacity must not be allocated by scanner worker latency."""

from itertools import permutations
from types import SimpleNamespace

import pytest

from backend.entry_ordering import ordered_entry_signals


def signal(symbol, score=80, **kw):
    return {
        "tradingsymbol": symbol,
        "signal_score": score,
        "direction": "BUY",
        "entryPrice": 100,
        "stopLoss": 95,
        "target": 110,
        "playbook": "trend",
        **kw,
    }


def test_priority_is_independent_of_callback_order_and_random_ui_metadata():
    candidates = [signal("AAA"), signal("BBB", 95), signal("AAA", 90)]
    for items in permutations(candidates):
        ordered = ordered_entry_signals(items, ["BBB", "AAA"])
        assert [(s["tradingsymbol"], s["signal_score"]) for s in ordered] == [
            ("BBB", 95),
            ("AAA", 90),
            ("AAA", 80),
        ]
    candidates[0].update(id="random-ui-id", timestamp="unrelated")
    assert ordered_entry_signals(candidates, ["BBB", "AAA"])[-1] is candidates[0]


def test_unknown_symbols_follow_ranked_universe_and_ties_use_signal_identity():
    items = [
        signal("ZZZ"),
        signal("AAA", playbook="z"),
        signal("AAA", playbook="a"),
        signal("CCC"),
    ]
    result = ordered_entry_signals(items, ["CCC"])
    assert [(s["tradingsymbol"], s["playbook"]) for s in result] == [
        ("CCC", "trend"),
        ("AAA", "a"),
        ("AAA", "z"),
        ("ZZZ", "trend"),
    ]
    with pytest.raises(ValueError, match="unique"):
        ordered_entry_signals(items, ["CCC", "CCC"])
    with pytest.raises(ValueError, match="finite"):
        ordered_entry_signals([signal("CCC", float("nan"))], ["CCC"])


@pytest.mark.parametrize("reverse", [False, True])
def test_live_streams_ui_then_admits_completed_batch_in_declared_priority(
    monkeypatch, reverse
):
    import backend.trading_engine as module

    engine = module.TradingEngine()
    engine.dynamic_watchlist = ["AAA", "BBB"]
    engine.watchlist_rankings = {"BBB": 1, "AAA": 2}
    engine.last_universe_refresh_time = module.now_utc()
    monkeypatch.setattr(engine, "_get_current_refresh_interval", lambda: 60)
    monkeypatch.setattr(module.risk_manager, "can_trade", lambda: (True, "OK"))
    published, submitted = [], []
    monkeypatch.setattr(
        engine, "_push_signal", lambda item: published.append(item["tradingsymbol"])
    )
    monkeypatch.setattr(
        engine, "execute_signal", lambda item: submitted.append(item["tradingsymbol"])
    )

    def scan(symbols, on_signal, *, progress=None):
        assert symbols == ["BBB", "AAA"]
        candidates = [signal("AAA", 90), signal("BBB", 75)]
        for item in reversed(candidates) if reverse else candidates:
            on_signal(item)
            assert not submitted
            assert published
        return candidates

    monkeypatch.setattr(module, "scanner", SimpleNamespace(scan_watchlist=scan))
    engine.scan_and_trade()
    assert submitted == ["BBB", "AAA"]
    assert len(published) == 2


def test_live_probability_and_duplicate_guards_survive_batching(monkeypatch):
    import backend.trading_engine as module

    engine = module.TradingEngine()
    engine.dynamic_watchlist = ["AAA", "BBB", "CCC"]
    engine.watchlist_rankings = {"AAA": 1, "BBB": 2, "CCC": 3}
    engine.last_universe_refresh_time = module.now_utc()
    engine.active_trades["CCC"] = {}
    monkeypatch.setattr(engine, "_get_current_refresh_interval", lambda: 60)
    monkeypatch.setattr(module.risk_manager, "can_trade", lambda: (True, "OK"))
    monkeypatch.setattr(engine, "_push_signal", lambda item: None)
    monkeypatch.setattr(engine, "_push_log", lambda *args, **kwargs: None)
    submitted = []
    monkeypatch.setattr(engine, "execute_signal", lambda item: submitted.append(item))

    def scan(symbols, on_signal, *, progress=None):
        for item in [
            signal("AAA", estimated_probability=0.59),
            signal("BBB", estimated_probability=float("nan")),
            signal("CCC"),
        ]:
            on_signal(item)
        return []

    monkeypatch.setattr(module, "scanner", SimpleNamespace(scan_watchlist=scan))
    engine.scan_and_trade()
    assert not submitted


def test_bad_optional_receipt_clock_cannot_skip_durable_shadow_decision(monkeypatch):
    from backend.journal import journal
    from backend.tests.test_exit_shadow_state import _evaluate, _live

    engine, thesis, current = _live(monkeypatch)

    class BrokenObserver:
        recorder = SimpleNamespace(failed=False)

        def received(self):
            raise RuntimeError("recorder clock unavailable")

    engine._operational_observer = BrokenObserver()
    _evaluate(engine, current)
    assert engine._operational_observer.recorder.failed
    assert len(journal.get_exit_decisions(thesis.position_key)) == 1


def test_shared_tick_rounding_preserves_production_prices():
    from backend.entry_ordering import round_entry_price_to_tick
    from backend.trading_engine import TradingEngine

    for price in (100.01, 100.02, 100.03, 99.975, 1.005):
        for tick in (0.01, 0.05, 0.10):
            expected = round(round(price / tick) * tick, 2)
            assert round_entry_price_to_tick(price, tick) == expected
            assert TradingEngine._round_to_tick(None, price, tick) == expected


def test_live_entry_and_exit_context_share_five_calendar_day_history(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import backend.scanner as scanner_module
    from backend.entry_ordering import PRODUCTION_CANDLE_HISTORY_DAYS

    observed = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
    requests = []
    local = scanner_module.Scanner()
    monkeypatch.setattr(scanner_module, "now_utc", lambda: observed)

    def history(token, start, end, interval):
        requests.append((token, start, end, interval))
        return []

    monkeypatch.setattr(scanner_module.kite_client, "get_historical_data", history)
    local._fetch_candles(123, "TEST")
    local.get_market_context(123, "TEST")
    assert PRODUCTION_CANDLE_HISTORY_DAYS == 5
    assert len(requests) == 2
    for token, start, end, interval in requests:
        assert token == 123 and interval == "5minute"
        assert start == observed - timedelta(days=5)
        assert end == observed
