import datetime

import pytest

from backend.broker_models import OrderSubmissionRejected
from backend.execution_gateway import execution_gateway
from backend.time_utils import EXCHANGE_TIMEZONE


class FakeRisk:
    def __init__(self, can_trade=True, reason="OK"):
        self._can_trade = can_trade
        self._reason = reason

    def can_trade(self):
        return self._can_trade, self._reason

    def validate_entry_reservation(
        self, reservation_id, symbol, direction, quantity, price
    ):
        return True

    def bind_entry_order(self, reservation_id, broker_order_id):
        return True

    def release_entry_reservation(self, reservation_id):
        pass


class FakeConfig:
    def get_risk_config(self):
        return {
            "maxDailyTrades": 2,
            "maxTradesPerSymbolPerDay": 1,
            "tradeCooldownMins": 5,
        }


class FakeJournal:
    def __init__(self, total_trades=0, by_symbol=None, last_exit=None):
        self._total = total_trades
        self._by_symbol = by_symbol or {}
        self._last_exit = last_exit

    def get_todays_trade_counts(self):
        return {"total": self._total, "by_symbol": self._by_symbol}

    def get_last_exit_time(self, symbol):
        return self._last_exit


class FakeKite:
    def place_order(self, **kwargs):
        return "12345"


def test_gateway_rejects_if_risk_manager_fails(monkeypatch):
    monkeypatch.setattr(
        "backend.execution_gateway.risk_manager", FakeRisk(False, "Max Loss Reached")
    )
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    with pytest.raises(
        OrderSubmissionRejected, match="Risk check failed: Max Loss Reached"
    ):
        execution_gateway.place_order(
            tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
        )


def test_gateway_allows_if_risk_manager_passes(monkeypatch):
    monkeypatch.setattr("backend.execution_gateway.risk_manager", FakeRisk(True, "OK"))
    monkeypatch.setattr("backend.execution_gateway.config_manager", FakeConfig())
    monkeypatch.setattr("backend.execution_gateway.journal", FakeJournal())
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    order_id = execution_gateway.place_order(
        tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
    )
    assert order_id == "12345"


def test_gateway_max_daily_trades(monkeypatch):
    monkeypatch.setattr("backend.execution_gateway.risk_manager", FakeRisk(True, "OK"))
    monkeypatch.setattr("backend.execution_gateway.config_manager", FakeConfig())
    monkeypatch.setattr(
        "backend.execution_gateway.journal", FakeJournal(total_trades=2)
    )
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    with pytest.raises(Exception, match="Max daily trades"):
        execution_gateway.place_order(
            tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
        )


def test_gateway_max_symbol_trades(monkeypatch):
    monkeypatch.setattr("backend.execution_gateway.risk_manager", FakeRisk(True, "OK"))
    monkeypatch.setattr("backend.execution_gateway.config_manager", FakeConfig())
    monkeypatch.setattr(
        "backend.execution_gateway.journal",
        FakeJournal(total_trades=1, by_symbol={"RELIANCE": 1}),
    )
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    with pytest.raises(Exception, match="Max trades per symbol"):
        execution_gateway.place_order(
            tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
        )


@pytest.mark.parametrize("timestamp_kind", ["utc", "exchange", "legacy_exchange"])
@pytest.mark.parametrize("minutes_since_exit", [2, 5, 6])
def test_gateway_cooldown(monkeypatch, timestamp_kind, minutes_since_exit):
    monkeypatch.setattr("backend.execution_gateway.risk_manager", FakeRisk(True, "OK"))
    monkeypatch.setattr("backend.execution_gateway.config_manager", FakeConfig())

    now = datetime.datetime(2026, 10, 1, 5, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr("backend.execution_gateway.now_utc", lambda: now)
    last_exit = now - datetime.timedelta(minutes=minutes_since_exit)
    if timestamp_kind != "utc":
        last_exit = last_exit.astimezone(EXCHANGE_TIMEZONE)
    if timestamp_kind == "legacy_exchange":
        # Offset-free legacy journal timestamps represent IST, not host time.
        last_exit = last_exit.replace(tzinfo=None)
    monkeypatch.setattr(
        "backend.execution_gateway.journal", FakeJournal(last_exit=last_exit)
    )
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    if minutes_since_exit < 5:
        with pytest.raises(OrderSubmissionRejected, match="Cooldown period active"):
            execution_gateway.place_order(
                tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
            )
    else:
        order_id = execution_gateway.place_order(
            tradingsymbol="RELIANCE", is_entry=True, entry_reservation_id="res1"
        )
        assert order_id == "12345"


def test_gateway_emergency_exit(monkeypatch):
    # Emergency should bypass everything (we don't even patch config/journal)
    monkeypatch.setattr(
        "backend.execution_gateway.risk_manager", FakeRisk(False, "Max Loss Reached")
    )
    monkeypatch.setattr("backend.execution_gateway.kite_client", FakeKite())

    order_id = execution_gateway.emergency_flatten_position(tradingsymbol="RELIANCE")
    assert order_id == "12345"
