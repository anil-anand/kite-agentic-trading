"""Best-effort market-news enrichment for the LLM advisory layer (issue #62,
Phase 3).

Deliberately kept **off the critical path**. News sources are fragile, so this
provider:

- is opt-in and only consulted by the LLM advisory layer;
- caches results and only re-fetches every ``CACHE_TTL_MINUTES``;
- drops whole cached batches older than ``STALE_AFTER_MINUTES``;
- returns ``[]`` on *any* failure — a missing or broken feed never blocks or
  changes the strategy decision, it only removes an optional prompt input.

The source is pluggable via ``provider``:

- ``google_news_rss`` (default): a keyless, India-localized Google News RSS
  search. RSS is meant to be consumed programmatically, so it's the least
  fragile default and needs no credentials.
- ``nse_announcements``: NSE's public corporate-announcements endpoint (filings,
  not general news). Requires browser-like session priming and is more brittle.

A custom ``fetch_fn`` can be injected (used by tests) to bypass the network
entirely. Parsing is factored into pure helpers so it's testable offline.
"""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from typing import Callable, List, Optional

CACHE_TTL_MINUTES = 30  # don't re-fetch more often than this
STALE_AFTER_MINUTES = 180  # ignore news older than this
DEFAULT_TIMEOUT = 6  # seconds; short — this is a nice-to-have, not a dependency

# A market-wide query for the regime-context use case (not per-stock).
DEFAULT_NEWS_QUERY = "NSE OR Nifty OR Sensex Indian stock market"
# A browser-like UA — plain/library UAs get blocked by some sources.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


# --- Pure parsers ------------------------------------------------------------
def _parse_rss(content: bytes, limit: int) -> List[dict]:
    """Parse an RSS document into ``{"headline", "timestamp"}`` items. Pure."""
    root = ET.fromstring(content)
    items: List[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        items.append({"headline": title, "timestamp": item.findtext("pubDate") or ""})
        if len(items) >= limit:
            break
    return items


def _parse_nse_announcements(payload: dict, limit: int) -> List[dict]:
    """Parse NSE's announcements JSON payload. Pure."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    items: List[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        headline = (row.get("subject") or row.get("desc") or "").strip()
        if not headline:
            continue
        items.append(
            {"headline": headline, "timestamp": row.get("an_dt") or row.get("dt") or ""}
        )
    return items


# --- Network fetchers (raise on failure; caller swallows) --------------------
def _google_news_rss_fetch(limit: int) -> List[dict]:
    """Keyless Google News RSS search, India-localized."""
    import requests

    resp = requests.get(
        "https://news.google.com/rss/search",
        params={
            "q": DEFAULT_NEWS_QUERY,
            "hl": "en-IN",
            "gl": "IN",
            "ceid": "IN:en",
        },
        headers={"User-Agent": _BROWSER_UA},
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return _parse_rss(resp.content, limit)


def _nse_announcements_fetch(limit: int) -> List[dict]:
    """NSE public corporate announcements (filings). Browser-like session
    priming; still brittle from datacenter IPs."""
    import requests

    session = requests.Session()
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
        "Authority": "www.nseindia.com",
    }
    # Prime cookies from the homepage (NSE rejects cold API hits).
    session.get("https://www.nseindia.com", headers=headers, timeout=DEFAULT_TIMEOUT)
    resp = session.get(
        "https://www.nseindia.com/api/latest-announcements?index=equities",
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return _parse_nse_announcements(resp.json(), limit)


_FETCHERS: dict = {
    "google_news_rss": _google_news_rss_fetch,
    "nse_announcements": _nse_announcements_fetch,
}
DEFAULT_PROVIDER = "google_news_rss"


class MarketNewsProvider:
    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        fetch_fn: Optional[Callable[[int], List[dict]]] = None,
        now_fn: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.provider = provider
        # An explicit fetch (tests) overrides provider selection entirely.
        self._explicit_fetch = fetch_fn
        self._now = now_fn or datetime.datetime.now
        self._cache: List[dict] = []
        self._fetched_at: Optional[datetime.datetime] = None

    def set_provider(self, provider: str):
        """Switch the news source. Changing source invalidates the cache so we
        don't serve one provider's items under another's name."""
        if provider and provider != self.provider:
            self.provider = provider
            self._fetched_at = None
            self._cache = []

    def _fetch(self, limit: int) -> List[dict]:
        if self._explicit_fetch is not None:
            return self._explicit_fetch(limit)
        fetcher = _FETCHERS.get(self.provider, _google_news_rss_fetch)
        return fetcher(limit)

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
            items = self._fetch(limit) or []
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
