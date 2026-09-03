import json
from urllib.error import HTTPError

import pytest

from backend.llm_client import OpenAICompatibleClient


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_generate_normalizes_url_headers_payload_and_response(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"choices": [{"message": {"content": "analysis"}}]})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    result = OpenAICompatibleClient().generate(
        "https://example.test/v1/",
        "secret-key",
        "model-name",
        "prompt text",
    )

    assert result == "analysis"
    assert captured["request"].full_url == "https://example.test/v1/chat/completions"
    assert captured["request"].headers["Authorization"] == "Bearer secret-key"
    assert captured["request"].headers["Content-type"] == "application/json"
    assert json.loads(captured["request"].data) == {
        "model": "model-name",
        "messages": [{"role": "user", "content": "prompt text"}],
    }


def test_generate_reports_http_errors(monkeypatch):
    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        OpenAICompatibleClient().generate("https://example.test", "key", "model", "p")


def test_generate_rejects_malformed_response(monkeypatch):
    monkeypatch.setattr(
        "backend.llm_client.request.urlopen",
        lambda request, timeout: FakeResponse({"choices": []}),
    )

    with pytest.raises(RuntimeError, match="malformed"):
        OpenAICompatibleClient().generate("https://example.test", "key", "model", "p")
