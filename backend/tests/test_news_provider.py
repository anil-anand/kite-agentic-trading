"""Tests for the best-effort market-news provider (issue #62, Phase 3).

The invariant: news is a nice-to-have that never raises and never blocks — it
returns cached results within the TTL, degrades to a fresh-enough cache on
fetch failure, and returns [] when there's nothing usable.
"""

import datetime

from backend.news_provider import (
    CACHE_TTL_MINUTES,
    STALE_AFTER_MINUTES,
    MarketNewsProvider,
)


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def advance(self, minutes):
        self.t += datetime.timedelta(minutes=minutes)


def _items(*headlines):
    return [{"headline": h, "timestamp": "2026-09-04"} for h in headlines]


def test_first_call_fetches_and_caches():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    calls = {"n": 0}

    def fetch(limit):
        calls["n"] += 1
        return _items("A", "B")

    p = MarketNewsProvider(fetch_fn=fetch, now_fn=clock.now)
    assert p.headlines() == ["A", "B"]
    assert calls["n"] == 1


def test_within_ttl_uses_cache():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    calls = {"n": 0}

    def fetch(limit):
        calls["n"] += 1
        return _items(f"batch{calls['n']}")

    p = MarketNewsProvider(fetch_fn=fetch, now_fn=clock.now)
    p.get_news()
    clock.advance(CACHE_TTL_MINUTES - 1)
    p.get_news()
    assert calls["n"] == 1  # not re-fetched


def test_refetches_after_ttl():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    calls = {"n": 0}

    def fetch(limit):
        calls["n"] += 1
        return _items(f"batch{calls['n']}")

    p = MarketNewsProvider(fetch_fn=fetch, now_fn=clock.now)
    assert p.headlines() == ["batch1"]
    clock.advance(CACHE_TTL_MINUTES + 1)
    assert p.headlines() == ["batch2"]
    assert calls["n"] == 2


def test_fetch_failure_falls_back_to_fresh_cache():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    state = {"boom": False}

    def fetch(limit):
        if state["boom"]:
            raise RuntimeError("nse down")
        return _items("cached")

    p = MarketNewsProvider(fetch_fn=fetch, now_fn=clock.now)
    p.get_news()  # populate cache
    state["boom"] = True
    clock.advance(CACHE_TTL_MINUTES + 1)  # forces a re-fetch, which fails
    assert p.headlines() == ["cached"]  # served from fresh-enough cache


def test_fetch_failure_returns_empty_when_cache_is_stale():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    state = {"boom": False}

    def fetch(limit):
        if state["boom"]:
            raise RuntimeError("nse down")
        return _items("old")

    p = MarketNewsProvider(fetch_fn=fetch, now_fn=clock.now)
    p.get_news()
    state["boom"] = True
    clock.advance(STALE_AFTER_MINUTES + 1)
    assert p.headlines() == []


def test_non_list_fetch_is_treated_as_failure():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))

    p = MarketNewsProvider(fetch_fn=lambda limit: {"not": "a list"}, now_fn=clock.now)
    assert p.get_news() == []


def test_empty_fetch_is_fine():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    p = MarketNewsProvider(fetch_fn=lambda limit: [], now_fn=clock.now)
    assert p.headlines() == []


def test_limit_is_applied():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    p = MarketNewsProvider(
        fetch_fn=lambda limit: _items("A", "B", "C", "D"), now_fn=clock.now
    )
    assert p.headlines(limit=2) == ["A", "B"]


def test_headlines_skips_blank():
    clock = Clock(datetime.datetime(2026, 9, 4, 10, 0))
    p = MarketNewsProvider(
        fetch_fn=lambda limit: [
            {"headline": "A", "timestamp": "x"},
            {"headline": "", "timestamp": "x"},
        ],
        now_fn=clock.now,
    )
    assert p.headlines() == ["A"]
