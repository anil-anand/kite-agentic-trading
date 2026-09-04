"""Best-effort market-news enrichment for the LLM advisory layer (issue #62,
Phase 3).

Deliberately kept **off the critical path**. Market news is fragile to source
(NSE endpoints are rate-limited and change), so this provider:

- is opt-in and only consulted by the LLM advisory layer;
- caches results and only re-fetches every ``CACHE_TTL_MINUTES``;
- drops items (and whole cached batches) older than ``STALE_AFTER_MINUTES``;
- returns ``[]`` on *any* failure — a missing or broken feed never blocks or
  changes the strategy decision, it only removes an optional prompt input.

The network fetch is injected (``fetch_fn``) so the provider is fully testable
without hitting the network. The default fetch targets NSE's public corporate
announcements endpoint but is wrapped so any error degrades to ``[]``.
"""

from __future__ import annotations

import datetime
from typing import Callable, List, Optional

CACHE_TTL_MINUTES = 30  # don't re-fetch more often than this
STALE_AFTER_MINUTES = 180  # ignore news older than this
DEFAULT_TIMEOUT = 6  # seconds; short — this is a nice-to-have, not a dependency


def _default_nse_fetch(limit: int) -> List[dict]:
    """Best-effort fetch of NSE public corporate announcements. Returns a list
    of ``{"headline": str, "timestamp": iso_str}``. Raises on any problem; the
    caller swallows it."""
    import requests

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; kite-agentic-trading/1.0)",
        "Accept": "application/json",
    }
    # Prime cookies from the homepage (NSE rejects cold API hits).
    session.get("https://www.nseindia.com", headers=headers, timeout=DEFAULT_TIMEOUT)
    resp = session.get(
        "https://www.nseindia.com/api/latest-announcements?index=equities",
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []

    items = []
    for row in rows[:limit]:
        headline = (row.get("subject") or row.get("desc") or "").strip()
        if not headline:
            continue
        items.append(
            {
                "headline": headline,
                "timestamp": row.get("an_dt") or row.get("dt") or "",
            }
        )
    return items


class MarketNewsProvider:
    def __init__(
        self,
        fetch_fn: Optional[Callable[[int], List[dict]]] = None,
        now_fn: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self._fetch_fn = fetch_fn or _default_nse_fetch
        self._now = now_fn or datetime.datetime.now
        self._cache: List[dict] = []
        self._fetched_at: Optional[datetime.datetime] = None

    def _age_minutes(self) -> float:
        if self._fetched_at is None:
            return float("inf")
        return (self._now() - self._fetched_at).total_seconds() / 60.0

    def get_news(self, limit: int = 10) -> List[dict]:
        """Return recent market-news items, best-effort. Never raises."""
        age = self._age_minutes()

        # Fresh cache: reuse without touching the network.
        if self._fetched_at is not None and age < CACHE_TTL_MINUTES:
            return self._cache[:limit]

        # Time to (re)fetch.
        try:
            items = self._fetch_fn(limit) or []
            if not isinstance(items, list):
                raise ValueError("news fetch did not return a list")
            self._cache = items
            self._fetched_at = self._now()
            return self._cache[:limit]
        except Exception:
            # Fetch failed: fall back to the last batch only if it isn't stale.
            if self._fetched_at is not None and age < STALE_AFTER_MINUTES:
                return self._cache[:limit]
            return []

    def headlines(self, limit: int = 10) -> List[str]:
        return [
            item["headline"] for item in self.get_news(limit) if item.get("headline")
        ]


market_news = MarketNewsProvider()
