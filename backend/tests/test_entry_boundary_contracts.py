"""Malformed agent proposals must fail before broker reads or risk sizing."""

import importlib
import json
from unittest.mock import Mock

import pytest

gateway_module = importlib.import_module("backend.agent_gateway")


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_nifty100_universe", lambda: ["RELIANCE"])
    monkeypatch.setattr(gateway_module.config_manager, "get_watchlist", lambda: [])
    sizing = Mock(return_value=10)
    snapshot = Mock(return_value=object())
    execute = Mock(return_value=True)
    monkeypatch.setattr(gateway_module.risk_manager, "calculate_position_size", sizing)
    monkeypatch.setattr(gateway_module.kite_client, "get_broker_snapshot", snapshot)
    monkeypatch.setattr(
        gateway_module.risk_manager, "can_accept_position", lambda **_: (True, "OK")
    )
    monkeypatch.setattr(gateway_module.trading_engine, "execute_signal", execute)
    return sizing, snapshot, execute


def proposal(**changes):
    return json.dumps(
        dict(
            tradingsymbol="RELIANCE",
            direction="BUY",
            quantity=10,
            order_type="LIMIT",
            price=100,
            stop_loss=90,
            target=120,
        )
        | changes
    )


@pytest.mark.parametrize("field", ["price", "stop_loss", "target"])
@pytest.mark.parametrize(
    "value",
    [True, False, "100", [100], {"price": 100}, float("nan"), float("inf"), 10**400],
    ids=["true", "false", "string", "list", "object", "nan", "infinity", "overflow"],
)
def test_invalid_agent_prices_are_rejected_before_sizing_or_broker_io(
    boundary, field, value
):
    result = gateway_module.agent_gateway.validate_and_route_proposal(
        proposal(**{field: value}), {}
    )
    assert result["status"] == "REJECTED"
    for mock in boundary:
        mock.assert_not_called()


def test_numeric_agent_proposal_keeps_the_deterministic_sizing_and_execution_path(
    boundary,
):
    result = gateway_module.agent_gateway.validate_and_route_proposal(proposal(), {})
    assert result["status"] == "ACCEPTED"
    sizing, snapshot, execute = boundary
    sizing.assert_called_once_with(100.0, 90.0)
    snapshot.assert_called_once_with()
    signal = execute.call_args.args[0]
    assert (signal["entryPrice"], signal["stopLoss"], signal["target"]) == (
        100.0,
        90.0,
        120.0,
    )
    assert all(
        isinstance(signal[field], float)
        for field in ("entryPrice", "stopLoss", "target")
    )
