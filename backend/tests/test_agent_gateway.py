import json
from unittest.mock import patch

from backend.agent_gateway import agent_gateway


def test_malformed_json():
    res = agent_gateway.validate_and_route_proposal("not json", {})
    assert res["status"] == "REJECTED"
    assert "Malformed" in res["reason"]


def test_missing_fields():
    proposal = json.dumps({"tradingsymbol": "RELIANCE", "direction": "BUY"})
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "Incomplete" in res["reason"]


def test_missing_quantity():
    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "Incomplete output: Missing quantity field" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
def test_symbol_not_in_allowlist(mock_watchlist, mock_nifty):
    mock_nifty.return_value = ["TCS"]
    mock_watchlist.return_value = []

    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "allowlist" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
def test_invalid_direction(mock_watchlist, mock_nifty):
    mock_nifty.return_value = ["RELIANCE"]
    mock_watchlist.return_value = []

    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "HOLD",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "direction" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
def test_invalid_quantity(mock_watchlist, mock_nifty):
    mock_nifty.return_value = ["RELIANCE"]
    mock_watchlist.return_value = []

    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "quantity": -10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "quantity" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
def test_invalid_price_levels(mock_watchlist, mock_nifty):
    mock_nifty.return_value = ["RELIANCE"]
    mock_watchlist.return_value = []

    # BUY with stop loss above price
    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 110,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "Invalid price levels" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
@patch("backend.agent_gateway.kite_client.get_orders")
@patch("backend.agent_gateway.risk_manager.can_accept_position")
def test_risk_limit_rejected(
    mock_can_accept, mock_get_orders, mock_watchlist, mock_nifty
):
    mock_nifty.return_value = ["RELIANCE"]
    mock_watchlist.return_value = []
    mock_get_orders.return_value = []
    mock_can_accept.return_value = (False, "Max exposure reached")

    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {})
    assert res["status"] == "REJECTED"
    assert "Risk limit rejected" in res["reason"]


@patch("backend.agent_gateway.get_nifty100_universe")
@patch("backend.agent_gateway.config_manager.get_watchlist")
@patch("backend.agent_gateway.kite_client.get_orders")
@patch("backend.agent_gateway.risk_manager.can_accept_position")
@patch("backend.agent_gateway.trading_engine.execute_signal")
def test_successful_proposal(
    mock_execute, mock_can_accept, mock_get_orders, mock_watchlist, mock_nifty
):
    mock_nifty.return_value = ["RELIANCE"]
    mock_watchlist.return_value = []
    mock_get_orders.return_value = []
    mock_can_accept.return_value = (True, "OK")
    mock_execute.return_value = True

    proposal = json.dumps(
        {
            "tradingsymbol": "RELIANCE",
            "direction": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "price": 100,
            "stop_loss": 90,
            "target": 120,
            "reasoning": "Looks good",
        }
    )
    res = agent_gateway.validate_and_route_proposal(proposal, {"model": "test-model"})
    assert res["status"] == "ACCEPTED"

    mock_execute.assert_called_once()
    signal_arg = mock_execute.call_args[0][0]
    assert signal_arg["tradingsymbol"] == "RELIANCE"
    assert signal_arg["quantity"] == 10
    assert signal_arg["strategy"] == "llm_agent"
