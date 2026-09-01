"""Tests for loading and caching the live NIFTY 50 universe."""

import io
import json
from urllib.error import HTTPError

import backend.nifty_universe as nifty_universe


class FakeResponse:
    def __init__(self, payload, headers=None):
        self._body = io.BytesIO(json.dumps(payload).encode())
        self.headers = headers or FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, *args, **kwargs):
        return self._body.read(*args, **kwargs)


class FakeHeaders:
    def __init__(self, values=None):
        self.values = values or []

    def get_all(self, name, default=None):
        return self.values if name == "Set-Cookie" else (default or [])


def live_symbols():
    return [f"SYMBOL{index:02d}" for index in range(50)]


def test_fetches_and_parses_nifty50_from_nse(monkeypatch):
    symbols = live_symbols()
    responses = iter(
        [
            FakeResponse({}, FakeHeaders(["nse cookie=value; Path=/"])),
            FakeResponse(
                {
                    "data": [
                        {"symbol": "NIFTY 50"},
                        *[{"symbol": symbol} for symbol in symbols],
                    ]
                }
            ),
        ]
    )
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(nifty_universe, "urlopen", fake_urlopen)

    result = nifty_universe._fetch_nifty50_from_nse()

    assert result == symbols
    assert len(requests) == 2
    assert requests[1].full_url.endswith("index=NIFTY%2050")
    assert requests[1].headers["Cookie"] == "nse cookie=value"


def test_uses_second_nse_endpoint_after_first_returns_404(monkeypatch):
    symbols = live_symbols()
    not_found = HTTPError(
        "https://www.nseindia.com/api/equity-stock-indices?index=NIFTY%2050",
        404,
        "Not Found",
        {},
        None,
    )
    responses = iter(
        [
            FakeResponse({}),
            not_found,
            FakeResponse({"records": [{"symbol": symbol} for symbol in symbols]}),
        ]
    )

    def fake_urlopen(request, timeout):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(nifty_universe, "urlopen", fake_urlopen)

    assert nifty_universe._fetch_nifty50_from_nse() == symbols


def test_falls_back_to_bundled_list_and_caches_result(monkeypatch):
    monkeypatch.setattr(nifty_universe, "_cached_nifty50", None)
    monkeypatch.setattr(nifty_universe, "_cached_on", None)
    monkeypatch.setattr(
        nifty_universe,
        "_fetch_nifty50_from_nse",
        lambda: (_ for _ in ()).throw(RuntimeError("NSE unavailable")),
    )

    first = nifty_universe.get_nifty50_universe()
    second = nifty_universe.get_nifty50_universe()

    assert first == nifty_universe.NIFTY_50
    assert second == first
    assert second is not first
