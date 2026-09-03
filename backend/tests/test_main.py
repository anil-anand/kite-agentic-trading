from unittest.mock import patch

from backend.main import handle_request


def test_save_settings_uses_config_manager_profile_path():
    with patch("backend.main.config_manager.save_settings") as save_settings:
        result = handle_request(
            {
                "id": 1,
                "method": "save_settings",
                "params": {"llm": {"provider": "OpenAI", "model": "gpt-4o-mini"}},
            }
        )

    assert result["result"] == {"status": "saved"}
    save_settings.assert_called_once_with(
        {"llm": {"provider": "OpenAI", "model": "gpt-4o-mini"}}
    )


def test_discover_models_uses_llm_discovery_service():
    with (
        patch("backend.main.config_manager.get_llm_settings") as get_settings,
        patch("backend.main.config_manager.get_credentials") as get_credentials,
        patch("backend.main.OpenAICompatibleClient.discover_models") as discover,
    ):
        get_settings.return_value = {
            "provider": "Ollama",
            "baseUrl": "http://localhost:11434",
        }
        get_credentials.return_value = {"llmApiKey": ""}
        discover.return_value = ["llama3.2"]

        result = handle_request({"id": 2, "method": "discover_models", "params": {}})

    assert result["result"] == ["llama3.2"]


def test_discover_models_forwards_unsaved_key_without_persisting_it():
    with (
        patch("backend.main.config_manager.get_llm_settings") as get_settings,
        patch("backend.main.config_manager.get_credentials") as get_credentials,
        patch("backend.main.OpenAICompatibleClient.discover_models") as discover,
        patch("backend.main.config_manager.save_llm_api_key") as save_key,
    ):
        get_settings.return_value = {
            "provider": "Ollama",
            "baseUrl": "http://localhost:11434",
        }
        get_credentials.return_value = {"llmApiKey": "persisted-key"}
        discover.return_value = ["cloud-model"]

        result = handle_request(
            {
                "id": 3,
                "method": "discover_models",
                "params": {
                    "provider": "Ollama",
                    "baseUrl": "http://localhost:11434",
                    "apiKey": "unsaved-key",
                },
            }
        )

    assert result["result"] == ["cloud-model"]
    discover.assert_called_once_with("Ollama", "http://localhost:11434", "unsaved-key")
    save_key.assert_not_called()


def test_discover_models_uses_persisted_key_when_unsaved_key_is_absent():
    with (
        patch("backend.main.config_manager.get_llm_settings") as get_settings,
        patch("backend.main.config_manager.get_credentials") as get_credentials,
        patch("backend.main.OpenAICompatibleClient.discover_models") as discover,
    ):
        get_settings.return_value = {
            "provider": "OpenRouter",
            "baseUrl": "https://example.test/v1",
        }
        get_credentials.return_value = {"llmApiKey": "persisted-key"}
        discover.return_value = ["model"]

        handle_request({"id": 4, "method": "discover_models", "params": {}})

    discover.assert_called_once_with(
        "OpenRouter", "https://example.test/v1", "persisted-key"
    )
