"""Source prerequisites are checked offline without using runtime credentials."""

from copy import deepcopy
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from backend.analytics import TradeAnalytics
from backend.calibration import ProbabilityCalibrator
from backend.config import ConfigManager
from backend.dev_mode import runtime_data_dir
from backend.journal import TradeJournal
from backend.llm_client import (
    PROVIDER_PRESETS,
    OpenAICompatibleClient,
    _NoCredentialRedirect,
    validate_provider_url,
)


def test_dev_persistence_never_imports_or_changes_live_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KITE_DEV_MODE", raising=False)
    live = ConfigManager()
    live.save_active_trades({"LIVE_SENTINEL": {"quantity": 7}})
    live.save_daily_risk_state({"kill_switch_active": True, "date": "2020-01-06"})
    live_journal = TradeJournal()
    root = runtime_data_dir()
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}

    monkeypatch.setenv("KITE_DEV_MODE", " true ")
    dev = ConfigManager()
    assert dev.config_dir == root / "dev"
    assert dev.load_active_trades() == {}
    assert not dev.load_daily_risk_state()
    assert TradeJournal().db_path == root / "dev" / "journal.db"
    assert ProbabilityCalibrator().db_path == root / "dev" / "journal.db"
    assert TradeAnalytics().db_path == root / "dev" / "journal.db"
    dev.save_active_trades({"DEV_SENTINEL": {"quantity": 99}})
    dev.save_daily_risk_state({"kill_switch_active": False})
    assert before == {
        path.name: path.read_bytes() for path in root.iterdir() if path.is_file()
    }
    assert live_journal.db_path == root / "journal.db"
    monkeypatch.delenv("KITE_DEV_MODE")
    restored = ConfigManager()
    assert "LIVE_SENTINEL" in restored.load_active_trades()
    assert restored.load_daily_risk_state()["kill_switch_active"] is True
    assert restored.config_dir == root


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.invalid/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com.attacker.invalid/v1",
        "https://api.openai.com@attacker.invalid/v1",
        "https://api.openai.com/v1?next=https://attacker.invalid",
        "https://api.openai.com/v1#fragment",
        "https://api.openai.com/v1/../other",
        "https://api.openai.com:8443/v1",
    ],
)
def test_credential_requests_reject_nonpreset_endpoints_before_transport(
    monkeypatch, url
):
    def unexpected(*args, **kwargs):
        pytest.fail("a rejected endpoint reached transport")

    monkeypatch.setattr("backend.llm_client._urlopen", unexpected)
    with pytest.raises(ValueError, match="preset"):
        OpenAICompatibleClient().generate(url, "offline-sentinel", "model", "prompt")
    with pytest.raises(ValueError, match="preset"):
        OpenAICompatibleClient().discover_models("OpenAI", url, "offline-sentinel")


@pytest.mark.parametrize("provider", PROVIDER_PRESETS)
def test_documented_provider_presets_remain_supported(provider):
    base = PROVIDER_PRESETS[provider]["baseUrl"]
    assert validate_provider_url(provider, base + "/") == base


def test_invalid_provider_settings_are_rejected_without_partial_save(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = ConfigManager()
    before = deepcopy(cfg.config)
    with pytest.raises(ValueError, match="preset"):
        cfg.save_settings(
            {
                "risk": {"maxDailyLoss": 1},
                "llm": {
                    "provider": "OpenAI",
                    "baseUrl": "https://attacker.invalid/v1",
                    "apiKey": "offline-sentinel",
                },
            }
        )
    assert cfg.config == before
    assert cfg.get_credentials()["llmApiKey"] == ""


def test_transport_will_not_forward_a_credential_bearing_redirect():
    req = Request(
        "https://api.openai.com/v1/models", headers={"Authorization": "sentinel"}
    )
    with pytest.raises(HTTPError, match="redirects are disabled"):
        _NoCredentialRedirect().redirect_request(
            req, None, 302, "Moved", {}, "https://attacker.invalid"
        )


def test_gemini_secret_uses_header_not_request_url(monkeypatch):
    captured = []

    def request(url, api_key, payload=None, headers=None, method="GET"):
        captured.append((url, headers))
        return {"models": []}

    client = OpenAICompatibleClient()
    monkeypatch.setattr(client, "_request", request)
    client.discover_models("Gemini", PROVIDER_PRESETS["Gemini"]["baseUrl"], "sentinel")
    assert "sentinel" not in captured[0][0]
    assert captured[0][1]["x-goog-api-key"] == "sentinel"


def test_saved_llm_key_cannot_follow_a_changed_provider_or_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = ConfigManager()
    cfg.save_llm_api_key("gemini-sentinel")
    assert cfg.get_credentials()["llmApiKey"] == "gemini-sentinel"
    cfg.save_settings(
        {"llm": {"provider": "OpenAI", "baseUrl": "https://api.openai.com/v1"}}
    )
    assert cfg.get_credentials()["llmApiKey"] == ""
    assert cfg.get_settings()["llm"]["apiKeyConfigured"] is False
    restarted = ConfigManager()
    restarted.set_credentials({"llmApiKey": "gemini-sentinel", "llmProvider": "Gemini"})
    assert restarted.get_credentials()["llmApiKey"] == ""
    restarted.set_credentials({"llmApiKey": "openai-sentinel", "llmProvider": "OpenAI"})
    assert restarted.get_credentials()["llmApiKey"] == "openai-sentinel"


def test_unsaved_provider_discovery_cannot_borrow_another_providers_key(monkeypatch):
    from backend import main

    monkeypatch.setattr(
        main.config_manager,
        "get_llm_settings",
        lambda: {"provider": "OpenAI", "baseUrl": "https://api.openai.com/v1"},
    )
    monkeypatch.setattr(
        main.config_manager,
        "get_credentials",
        lambda: {"llmApiKey": "openai-sentinel", "llmProvider": "OpenAI"},
    )

    def unexpected(*args, **kwargs):
        pytest.fail("stored key reached a different provider")

    monkeypatch.setattr(main.OpenAICompatibleClient, "discover_models", unexpected)
    response = main.handle_request(
        {
            "id": 1,
            "method": "discover_models",
            "params": {
                "provider": "Anthropic",
                "baseUrl": "https://api.anthropic.com/v1",
            },
        }
    )
    assert response["error"]["code"] == -32602
