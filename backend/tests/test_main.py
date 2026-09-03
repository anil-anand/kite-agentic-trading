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
