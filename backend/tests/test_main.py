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
