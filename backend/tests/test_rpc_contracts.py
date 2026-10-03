import importlib

import pytest

from backend.broker_models import normalize_positions_response
from backend.main import handle_request


def test_manual_order_rpc_is_disabled_with_actionable_message():
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "place_order",
            "params": {
                "tradingsymbol": "RELIANCE",
                "quantity": 1,
            },
        }
    )
    assert response["error"]["code"] == -32004
    assert "validated signal" in response["error"]["message"]


@pytest.mark.parametrize("margin", [True, False, float("nan"), float("inf"), "100"])
def test_dashboard_malformed_margin_is_explicitly_unavailable(monkeypatch, margin):
    main = importlib.import_module("backend.main")
    monkeypatch.setattr(
        main.kite_client,
        "get_margins",
        lambda: {"equity": {"available": {"live_balance": margin}}},
    )
    monkeypatch.setattr(
        main.kite_client,
        "get_positions_snapshot",
        lambda: normalize_positions_response({"net": [], "day": []}),
    )
    monkeypatch.setattr(main.risk_manager, "reconciliation_status", "RECONCILED")
    monkeypatch.setattr(main.risk_manager, "accounting_quality", "UNAVAILABLE")
    monkeypatch.setattr(main.journal, "get_verified_todays_outcomes", lambda: [])
    response = handle_request({"id": 8, "method": "dashboard_summary"})
    assert "error" not in response
    assert response["result"]["availableMargin"] is None
