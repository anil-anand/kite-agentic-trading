import json
from urllib.error import HTTPError

import pytest

from backend.llm_client import OPENCODE_PLANS, PROVIDER_PRESETS, OpenAICompatibleClient


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


def test_provider_presets_are_explicit_and_exclude_custom_and_bedrock():
    assert set(PROVIDER_PRESETS) == {
        "OpenAI",
        "Anthropic",
        "Gemini",
        "OpenRouter",
        "Ollama",
        "OpenCode",
    }
    assert PROVIDER_PRESETS["Ollama"]["requiresApiKey"] is False


@pytest.mark.parametrize(
    ("plan", "catalog", "expected"),
    [
        (
            "zen",
            [
                {
                    "id": "big-pickle",
                    "object": "model",
                    "created": 0,
                    "owned_by": "opencode",
                },
                {"id": "gpt-5", "object": "model", "created": 0, "owned_by": "openai"},
                {"id": "o3", "object": "model", "created": 0, "owned_by": "openai"},
                {
                    "id": "claude-sonnet-4-5",
                    "object": "model",
                    "created": 0,
                    "owned_by": "anthropic",
                },
                {
                    "id": "gemini-2.5-pro",
                    "object": "model",
                    "created": 0,
                    "owned_by": "google",
                },
            ],
            ["big-pickle"],
        ),
        (
            "go",
            [
                {
                    "id": "kimi-k3",
                    "object": "model",
                    "created": 0,
                    "owned_by": "moonshot",
                },
                {
                    "id": "kimi-k2.5",
                    "object": "model",
                    "created": 0,
                    "owned_by": "moonshot",
                },
                {"id": "o3", "object": "model", "created": 0, "owned_by": "openai"},
                {
                    "id": "claude-sonnet-4-5",
                    "object": "model",
                    "created": 0,
                    "owned_by": "anthropic",
                },
                {
                    "id": "gemini-2.5-pro",
                    "object": "model",
                    "created": 0,
                    "owned_by": "google",
                },
            ],
            ["kimi-k3", "kimi-k2.5"],
        ),
    ],
)
def test_opencode_discovery_filters_minimal_catalog_by_plan_allowlist(
    monkeypatch, plan, catalog, expected
):
    monkeypatch.setattr(
        "backend.llm_client.request.urlopen",
        lambda request, timeout: FakeResponse({"data": catalog}),
    )

    assert (
        OpenAICompatibleClient().discover_models(
            "OpenCode", OPENCODE_PLANS[plan]["baseUrl"], "key", plan=plan
        )
        == expected
    )


def test_opencode_zen_allowlist_excludes_responses_only_gpt5():
    assert "big-pickle" in OPENCODE_PLANS["zen"]["chatModels"]
    assert "gpt-5" not in OPENCODE_PLANS["zen"]["chatModels"]


@pytest.mark.parametrize(
    ("plan", "model"),
    [("zen", "gpt-5"), ("go", "big-pickle")],
)
def test_generate_rejects_opencode_models_outside_selected_plan_allowlist(
    monkeypatch, plan, model
):
    def fail_if_requested(*args, **kwargs):
        raise AssertionError("invalid OpenCode model reached HTTP request")

    monkeypatch.setattr("backend.llm_client.request.urlopen", fail_if_requested)

    with pytest.raises(RuntimeError, match="not available for OpenCode plan"):
        OpenAICompatibleClient().generate(
            OPENCODE_PLANS[plan]["baseUrl"],
            "opencode-key",
            model,
            "prompt",
            provider="OpenCode",
            plan=plan,
        )


def test_anthropic_request_uses_provider_auth_and_native_messages_api(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse({"content": [{"type": "text", "text": "analysis"}]})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    result = OpenAICompatibleClient().generate(
        "https://api.anthropic.com/v1",
        "secret-key",
        "claude-sonnet",
        "prompt",
        provider="Anthropic",
    )

    assert result == "analysis"
    assert captured["request"].full_url == "https://api.anthropic.com/v1/messages"
    assert captured["request"].headers["X-api-key"] == "secret-key"
    assert json.loads(captured["request"].data)["messages"] == [
        {"role": "user", "content": "prompt"}
    ]


@pytest.mark.parametrize(
    ("provider", "base_url", "api_key", "model", "response", "expected", "path"),
    [
        (
            "Gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-key",
            "gemini-2.5-flash",
            {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]},
            "gemini",
            "/models/gemini-2.5-flash:generateContent?key=gemini-key",
        ),
        (
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "router-key",
            "openai/gpt-4o-mini",
            {"choices": [{"message": {"content": "router"}}]},
            "router",
            "/chat/completions",
        ),
        (
            "OpenCode",
            "https://opencode.ai/zen/v1",
            "opencode-key",
            "big-pickle",
            {"choices": [{"message": {"content": "opencode"}}]},
            "opencode",
            "/chat/completions",
        ),
    ],
)
def test_generate_supports_provider_specific_paths(
    monkeypatch, provider, base_url, api_key, model, response, expected, path
):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse(response)

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    result = OpenAICompatibleClient().generate(
        base_url, api_key, model, "prompt", provider=provider
    )

    assert result == expected
    assert captured["request"].full_url == f"{base_url}{path}"
    if provider == "Gemini":
        assert "Authorization" not in captured["request"].headers
    else:
        assert captured["request"].headers["Authorization"] == f"Bearer {api_key}"


def test_ollama_request_has_no_authorization_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse({"message": {"content": "analysis"}})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    assert (
        OpenAICompatibleClient().generate(
            "http://localhost:11434", "", "llama3.2", "prompt", provider="Ollama"
        )
        == "analysis"
    )
    assert "Authorization" not in captured["request"].headers
    assert captured["request"].full_url == "http://localhost:11434/api/chat"


def test_ollama_cloud_request_uses_api_key_and_cloud_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse({"message": {"content": "cloud analysis"}})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    result = OpenAICompatibleClient().generate(
        "http://localhost:11434", "cloud-key", "llama3.2", "prompt", provider="Ollama"
    )

    assert result == "cloud analysis"
    assert captured["request"].full_url == "https://ollama.com/api/chat"
    assert captured["request"].headers["Authorization"] == "Bearer cloud-key"


def test_ollama_cloud_model_discovery_validates_api_key(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse({"models": [{"name": "cloud-model"}]})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    assert OpenAICompatibleClient().discover_models(
        "Ollama", "http://localhost:11434", "cloud-key"
    ) == ["cloud-model"]
    assert captured["request"].full_url == "https://ollama.com/api/tags"
    assert captured["request"].headers["Authorization"] == "Bearer cloud-key"


def test_ollama_cloud_rejects_invalid_api_key(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        OpenAICompatibleClient().discover_models(
            "Ollama", "http://localhost:11434", "invalid-key"
        )


def test_discover_models_uses_provider_endpoint_and_normalizes_models(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    assert OpenAICompatibleClient().discover_models(
        "OpenAI", "https://api.openai.com/v1", "secret-key"
    ) == ["gpt-4o", "gpt-4o-mini"]
    assert captured["request"].full_url == "https://api.openai.com/v1/models"
    assert captured["request"].headers["Authorization"] == "Bearer secret-key"


@pytest.mark.parametrize(
    ("provider", "base_url", "api_key", "body", "expected", "path"),
    [
        (
            "Anthropic",
            "https://api.anthropic.com/v1",
            "anthropic-key",
            {"data": [{"id": "claude-sonnet"}]},
            ["claude-sonnet"],
            "/models",
        ),
        (
            "Gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-key",
            {"models": [{"name": "models/gemini-2.5-flash"}]},
            ["gemini-2.5-flash"],
            "/models?key=gemini-key",
        ),
        (
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "router-key",
            {"data": [{"id": "openai/gpt-4o-mini"}]},
            ["openai/gpt-4o-mini"],
            "/models",
        ),
        (
            "OpenCode",
            "https://opencode.ai/zen/v1",
            "opencode-key",
            {"data": [{"id": "big-pickle"}]},
            ["big-pickle"],
            "/models",
        ),
    ],
)
def test_discover_models_supports_all_provider_protocols(
    monkeypatch, provider, base_url, api_key, body, expected, path
):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["request"] = req
        return FakeResponse(body)

    monkeypatch.setattr("backend.llm_client.request.urlopen", fake_urlopen)

    assert (
        OpenAICompatibleClient().discover_models(provider, base_url, api_key)
        == expected
    )
    assert captured["request"].full_url == f"{base_url}{path}"
    if provider == "Anthropic":
        assert captured["request"].headers["X-api-key"] == api_key
    elif provider == "Gemini":
        assert "Authorization" not in captured["request"].headers
    else:
        assert captured["request"].headers["Authorization"] == f"Bearer {api_key}"
