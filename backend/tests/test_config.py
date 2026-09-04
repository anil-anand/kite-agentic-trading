import json

from backend.config import ConfigManager


def make_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return ConfigManager()


def test_default_llm_profile_is_gemini_compatible(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)

    assert manager.config["llm"] == {
        "provider": "Gemini",
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
        "openCodePlan": "zen",
        "apiKey": "",
        "temperature": 0.2,
        "maxTokens": 1024,
    }


def test_legacy_llm_key_is_migrated_and_not_returned(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    manager.config_file.write_text(
        json.dumps(
            {
                "credentials": {"llmApiKey": manager._encrypt("legacy-key")},
            }
        )
    )
    manager.load()

    assert manager.get_credentials()["llmApiKey"] == "legacy-key"
    assert manager.get_settings()["llm"]["apiKey"] == ""
    assert "legacy-key" not in json.dumps(manager.get_settings())
    assert manager.config["llm"]["apiKey"] != "legacy-key"


def test_save_settings_encrypts_llm_key_and_preserves_it_when_masked(
    tmp_path, monkeypatch
):
    manager = make_manager(tmp_path, monkeypatch)
    manager.save_settings(
        {
            "llm": {
                "provider": "OpenAI",
                "baseUrl": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "apiKey": "secret-key",
            }
        }
    )
    manager.save_settings(
        {
            "llm": {
                "provider": "OpenAI",
                "baseUrl": "https://api.openai.com/v1",
                "model": "gpt-4.1-mini",
                "apiKey": "",
            }
        }
    )

    assert manager.get_credentials()["llmApiKey"] == "secret-key"
    assert manager.get_settings()["llm"]["model"] == "gpt-4.1-mini"
    assert manager.get_settings()["llm"]["apiKey"] == ""


def test_save_settings_does_not_clear_kite_credentials(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    manager.save_credentials("kite-key", "kite-secret")
    manager.save_settings({"credentials": {"apiKey": "", "apiSecret": ""}})

    assert manager.get_credentials()["apiKey"] == "kite-key"
    assert manager.get_credentials()["apiSecret"] == "kite-secret"
