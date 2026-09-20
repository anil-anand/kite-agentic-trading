import importlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.main import handle_request

main_module = importlib.import_module("backend.main")


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


def test_discover_models_uses_selected_opencode_plan():
    with (
        patch("backend.main.config_manager.get_llm_settings") as get_settings,
        patch("backend.main.config_manager.get_credentials") as get_credentials,
        patch("backend.main.OpenAICompatibleClient.discover_models") as discover,
    ):
        get_settings.return_value = {
            "provider": "OpenCode",
            "baseUrl": "https://opencode.ai/zen/v1",
            "openCodePlan": "go",
        }
        get_credentials.return_value = {"llmApiKey": "key"}
        discover.return_value = ["kimi-k3"]

        result = handle_request({"id": 5, "method": "discover_models", "params": {}})

    assert result["result"] == ["kimi-k3"]
    discover.assert_called_once_with(
        "OpenCode", "https://opencode.ai/zen/go/v1", "key", plan="go"
    )


def test_discover_models_allows_opencode_without_api_key():
    with (
        patch("backend.main.config_manager.get_llm_settings") as get_settings,
        patch("backend.main.config_manager.get_credentials") as get_credentials,
        patch("backend.main.OpenAICompatibleClient.discover_models") as discover,
    ):
        get_settings.return_value = {
            "provider": "OpenCode",
            "baseUrl": "https://opencode.ai/zen/v1",
            "openCodePlan": "zen",
        }
        get_credentials.return_value = {"llmApiKey": ""}
        discover.return_value = ["big-pickle"]

        result = handle_request({"id": 6, "method": "discover_models", "params": {}})

    assert result["result"] == ["big-pickle"]
    discover.assert_called_once_with(
        "OpenCode", "https://opencode.ai/zen/v1", "", plan="zen"
    )


def test_set_credentials_updates_config_manager():
    with patch("backend.main.config_manager.set_credentials") as set_creds:
        result = handle_request(
            {
                "id": 7,
                "method": "set_credentials",
                "params": {"credentials": {"apiKey": "abc"}},
            }
        )

    assert result["result"] == {"status": "credentials_set"}
    set_creds.assert_called_once_with({"apiKey": "abc"})


def test_migrate_credentials_calls_config_manager():
    with patch("backend.main.config_manager.get_legacy_credentials") as get_legacy:
        get_legacy.return_value = {"apiKey": "legacy-key"}
        result = handle_request(
            {"id": 8, "method": "migrate_credentials", "params": {}}
        )

    assert result["result"] == {"apiKey": "legacy-key"}
    get_legacy.assert_called_once()


def test_clear_legacy_credentials_calls_config_manager():
    with patch("backend.main.config_manager.clear_legacy_credentials") as clear_legacy:
        result = handle_request(
            {"id": 9, "method": "clear_legacy_credentials", "params": {}}
        )

    assert result["result"] == {"status": "legacy_credentials_cleared"}
    clear_legacy.assert_called_once()


def test_operator_close_and_emergency_routes_require_backend_acknowledgement():
    with patch("backend.main.trading_engine") as engine:
        engine.request_operator_close.return_value = {
            "accepted": True,
            "pending": True,
            "intentId": "exit-1",
        }
        engine.request_emergency_flatten.return_value = {
            "accepted": True,
            "scope": "account",
            "pending": True,
        }

        close = handle_request(
            {
                "id": 10,
                "method": "agent_close_position",
                "params": {"positionKey": "LIVE:acct:NSE:1:ABC:MIS"},
            }
        )
        emergency = handle_request(
            {
                "id": 11,
                "method": "agent_emergency_flatten",
                "params": {"scope": "account"},
            }
        )

    assert close["result"]["pending"] is True
    assert emergency["result"]["scope"] == "account"
    engine.request_operator_close.assert_called_once_with("LIVE:acct:NSE:1:ABC:MIS")
    engine.request_emergency_flatten.assert_called_once_with("account")


@pytest.mark.parametrize(
    "payload,code",
    [
        (None, -32600),
        ([], -32600),
        ({"method": []}, -32600),
        ({"method": "agent_status", "params": None}, -32602),
        ({"method": "agent_status", "params": []}, -32602),
        ({"method": "agent_status", "jsonrpc": "1.0"}, -32600),
    ],
)
def test_malformed_requests_fail_without_crashing_dispatch(payload, code):
    assert handle_request(payload)["error"]["code"] == code


@pytest.mark.parametrize(
    "blocking_method",
    ["execute_signal", "get_historical", "scan_now", "discover_models"],
)
def test_blocked_rpc_does_not_delay_emergency_admission(monkeypatch, blocking_method):
    started = threading.Event()
    release = threading.Event()
    emergency_done = threading.Event()
    blocked_done = threading.Event()
    responses = {}

    def handle(req):
        if req["method"] == blocking_method:
            started.set()
            assert release.wait(2)
        return {"jsonrpc": "2.0", "id": req["id"], "result": {"accepted": True}}

    def write(response):
        responses[response["id"]] = response
        if response["id"] == 2:
            emergency_done.set()
        if response["id"] == 1:
            blocked_done.set()

    def requests():
        yield json.dumps({"id": 1, "method": blocking_method})
        assert started.wait(1)
        yield json.dumps({"id": 2, "method": "agent_emergency_flatten"})
        try:
            assert emergency_done.wait(0.5)
            assert not blocked_done.is_set()
        finally:
            release.set()
        assert blocked_done.wait(1)

    monkeypatch.setattr(main_module, "_handle_request", handle)
    monkeypatch.setattr(main_module, "_write_response", write)
    monkeypatch.setattr(main_module.sys, "stdin", requests())
    main_module.main()
    assert set(responses) == {1, 2}


def test_saturated_research_pool_rejects_without_queuing_operator_control(monkeypatch):
    workers_started = [threading.Event(), threading.Event()]
    release = threading.Event()
    workers_done = [threading.Event(), threading.Event()]
    responses = {}

    def handle(req):
        if req["method"] == "discover_models":
            workers_started[req["id"] - 1].set()
            assert release.wait(2)
        return {"id": req["id"], "result": {"accepted": True}}

    def write(response):
        responses[response["id"]] = response
        if response["id"] in {1, 2}:
            workers_done[response["id"] - 1].set()

    def requests():
        for request_id in (1, 2):
            yield json.dumps({"id": request_id, "method": "discover_models"})
            assert workers_started[request_id - 1].wait(1)
        yield json.dumps({"id": 3, "method": "discover_models"})
        yield json.dumps({"id": 4, "method": "agent_emergency_flatten"})
        try:
            assert responses[3]["error"]["code"] == -32005
            assert responses[4]["result"]["accepted"] is True
        finally:
            release.set()
        assert all(worker.wait(1) for worker in workers_done)

    monkeypatch.setattr(main_module, "_handle_request", handle)
    monkeypatch.setattr(main_module, "_write_response", write)
    monkeypatch.setattr(main_module.sys, "stdin", requests())
    main_module.main()


@pytest.mark.parametrize(
    "obligation", ["local", "durable", "order", "position", "unknown"]
)
def test_logout_preserves_authentication_when_obligations_are_not_settled(obligation):
    with (
        patch("backend.main.trading_engine") as engine,
        patch("backend.main.kite_client") as client,
        patch("backend.main.config_manager") as config,
        patch("backend.main.ticker_manager") as ticker,
    ):
        engine._has_residual_obligations.return_value = obligation == "local"
        engine._pending_lifecycle_obligations.return_value = (
            [1] if obligation == "durable" else []
        )
        engine._order_snapshot.return_value.require_complete.return_value = (
            SimpleNamespace(
                orders=[SimpleNamespace(status="OPEN")] if obligation == "order" else []
            )
        )
        engine._position_snapshot.return_value.require_complete.return_value = (
            SimpleNamespace(
                net=[SimpleNamespace(signed_quantity=1)]
                if obligation == "position"
                else []
            )
        )
        if obligation == "unknown":
            engine._order_snapshot.return_value.require_complete.side_effect = (
                RuntimeError("broker unavailable")
            )

        response = handle_request({"id": 1, "method": "logout"})

        assert "error" in response
        engine.stop.assert_called_once()
        config.clear_access_token.assert_not_called()
        client.set_access_token.assert_not_called()
        ticker.stop.assert_not_called()


def test_logout_reads_terminal_orders_before_confirming_flat_positions():
    calls = []
    with (
        patch("backend.main.trading_engine") as engine,
        patch("backend.main.kite_client") as client,
        patch("backend.main.config_manager") as config,
        patch("backend.main.ticker_manager") as ticker,
    ):
        engine._has_residual_obligations.return_value = False
        engine._pending_lifecycle_obligations.return_value = []
        engine.stop.side_effect = lambda: calls.append("pause")
        engine._order_snapshot.return_value.require_complete.side_effect = lambda: (
            calls.append("orders") or SimpleNamespace(orders=[])
        )
        engine._position_snapshot.return_value.require_complete.side_effect = lambda: (
            calls.append("positions") or SimpleNamespace(net=[])
        )
        config.clear_access_token.side_effect = lambda: calls.append("clear")

        response = handle_request({"id": 1, "method": "logout"})

        assert response["result"] == {"status": "logged_out"}
        assert calls == ["pause", "orders", "positions", "clear"]
        engine._order_snapshot.assert_called_once_with(critical=True)
        engine._position_snapshot.assert_called_once_with(critical=True)
        client.set_access_token.assert_called_once_with(None)
        ticker.stop.assert_called_once()


@pytest.mark.parametrize("already_supervising", [False, True])
def test_authenticated_session_starts_supervision_without_pausing_running_engine(
    already_supervising,
):
    with (
        patch("backend.main.is_dev_mode", return_value=True),
        patch("backend.main.ticker_manager"),
        patch("backend.main.trading_engine") as engine,
    ):
        engine.status.return_value = {"supervisionActive": already_supervising}
        response = handle_request({"id": 1, "method": "check_session"})
        assert response["result"]["is_valid"] is True
        assert engine.resume_supervision.call_count == (0 if already_supervising else 1)


def test_first_login_resumes_paused_supervision_before_returning_success():
    with (
        patch("backend.main.kite_client") as client,
        patch("backend.main.KiteConnect") as factory,
        patch(
            "backend.main.broker_gateway.execute",
            return_value={"user_id": "TEST_ACCOUNT"},
        ),
        patch("backend.main.config_manager"),
        patch("backend.main.ticker_manager"),
        patch("backend.main.trading_engine") as engine,
    ):
        factory.return_value.generate_session.return_value = {
            "access_token": "test-session"
        }
        client.account_id = "UNKNOWN"
        engine._control_state_scope = None
        engine.active_trades = {}
        response = handle_request({"id": 1, "method": "generate_session", "params": {}})
        assert "result" in response
        engine.resume_supervision.assert_called_once()


@pytest.mark.parametrize("candidate_account", ["TEST_ACCOUNT", "OTHER_ACCOUNT", None])
@pytest.mark.parametrize("method", ["generate_session", "check_session"])
def test_reauthentication_preserves_supervised_account_until_candidate_is_verified(
    candidate_account,
    method,
):
    old_connection = object()
    with (
        patch("backend.main.kite_client") as client,
        patch("backend.main.is_dev_mode", return_value=False),
        patch("backend.main.KiteConnect") as factory,
        patch(
            "backend.main.broker_gateway.execute",
            return_value={"user_id": candidate_account},
        ),
        patch("backend.main.config_manager") as config,
        patch("backend.main.ticker_manager") as ticker,
        patch("backend.main.trading_engine") as engine,
    ):
        client.kite = old_connection
        client.account_id = "TEST_ACCOUNT"
        client.access_token = "expired-test-session"
        engine._control_state_scope = ("LIVE", "TEST_ACCOUNT")
        engine.active_trades = {"ABC": {"account_id": "TEST_ACCOUNT"}}
        engine._has_residual_obligations.return_value = True
        config.get_credentials.return_value = {
            "apiKey": "test-api",
            "accessToken": "renewed-test-session",
        }
        candidate = factory.return_value
        candidate.generate_session.return_value = {
            "access_token": "renewed-test-session"
        }

        response = handle_request({"id": 1, "method": method, "params": {}})

        if candidate_account == "TEST_ACCOUNT":
            assert "result" in response
            assert client.kite is candidate
            assert client.account_id == "TEST_ACCOUNT"
            assert client.access_token == "renewed-test-session"
            client.init.assert_not_called()
            client.set_access_token.assert_not_called()
            engine.resume_supervision.assert_called_once()
        else:
            if method == "generate_session":
                assert "error" in response
            else:
                assert response["result"]["is_valid"] is False
            assert client.kite is old_connection
            assert client.account_id == "TEST_ACCOUNT"
            assert client.access_token == "expired-test-session"
            config.save_credentials.assert_not_called()
            ticker.start.assert_not_called()
            engine.resume_supervision.assert_not_called()


def test_login_url_does_not_replace_active_client_or_credentials():
    old_connection = object()
    with (
        patch("backend.main.KiteConnect") as factory,
        patch("backend.main.kite_client") as client,
        patch("backend.main.config_manager") as config,
    ):
        client.kite = old_connection
        config.get_credentials.return_value = {"apiKey": "test-api"}
        factory.return_value.login_url.return_value = "https://example.test/login"
        response = handle_request({"id": 1, "method": "login", "params": {}})
        assert response["result"]["login_url"] == "https://example.test/login"
        assert client.kite is old_connection
        client.init.assert_not_called()
        config.save_credentials.assert_not_called()


def test_session_status_refresh_preserves_verified_broker_identity():
    with (
        patch("backend.main.is_dev_mode", return_value=False),
        patch("backend.main.kite_client") as client,
        patch("backend.main.config_manager") as config,
        patch("backend.main.ticker_manager"),
        patch("backend.main.trading_engine") as engine,
    ):
        config.get_credentials.return_value = {
            "apiKey": "test-api",
            "accessToken": "test-session",
        }
        client.kite.api_key = "test-api"
        client.access_token = "test-session"
        client.account_id = "TEST_ACCOUNT"
        engine.status.return_value = {"supervisionActive": True}

        response = handle_request({"id": 1, "method": "check_session"})

        assert response["result"]["is_valid"] is True
        client.init.assert_not_called()
        client.set_access_token.assert_not_called()
        client.refresh_account_id.assert_not_called()
        engine.resume_supervision.assert_not_called()


def test_entry_rpc_requires_supervision_before_executing_signal():
    with patch("backend.main.trading_engine") as engine:
        engine.status.return_value = {"supervisionActive": False}
        response = handle_request(
            {"id": 1, "method": "execute_signal", "params": {"signal": {}}}
        )
        assert response["error"]["code"] == -32004
        engine.execute_signal.assert_not_called()


def test_cancel_command_uses_server_owned_order_validation():
    cancel = Mock(
        return_value={"accepted": True, "pending": True, "orderId": "order-1"}
    )
    with patch.object(
        main_module.trading_engine, "request_operator_cancel", cancel, create=True
    ):
        response = handle_request(
            {
                "id": 1,
                "method": "cancel_order",
                "params": {"orderId": "order-1", "variety": "untrusted"},
            }
        )
    assert response["result"]["pending"] is True
    cancel.assert_called_once_with("order-1")
