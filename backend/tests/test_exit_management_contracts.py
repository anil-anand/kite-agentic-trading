import datetime

from backend.broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    FillSnapshot,
    OrderRole,
    SnapshotQuality,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
)
from backend.config import config_manager
from backend.execution_gateway import execution_gateway
from backend.risk_manager import RiskManager
from backend.time_utils import EXCHANGE_TIMEZONE, now_utc
from backend.trading_engine import TradingEngine


class _FillClient:
    def __init__(self, fills):
        normalized = []
        for account_id in {item.get("accountId", "acct-1") for item in fills}:
            normalized.extend(
                normalize_fills_response(
                    [
                        item
                        for item in fills
                        if item.get("accountId", "acct-1") == account_id
                    ],
                    namespace=ExecutionNamespace.LIVE,
                    account_id=account_id,
                ).fills
            )
        self.snapshot = FillSnapshot(
            fills=tuple(normalized),
            quality=SnapshotQuality.COMPLETE,
            fetched_at=normalized[0].received_at if normalized else None,
        )

    def get_fills_snapshot(self):
        return self.snapshot


def _fill(order_id, fill_id, side, quantity, price, account_id="acct-1"):
    return {
        "orderId": order_id,
        "tradeId": fill_id,
        "transactionType": side,
        "quantity": quantity,
        "averagePrice": price,
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "MIS",
        "accountId": account_id,
        "fillTimestamp": now_utc().replace(microsecond=0),
    }


def _trade(**overrides):
    trade = {
        "direction": "BUY",
        "entry_price": 100.0,
        "quantity": 10,
        "entry_time": datetime.datetime.now(datetime.timezone.utc),
        "entry_order_id": "ENTRY1",
        "exit_order_id": "EXIT1",
        "stop_order_id": None,
        "exchange": "NSE",
        "product": "MIS",
        "account_id": "acct-1",
        "exit_reason": "Target",
    }
    trade.update(overrides)
    return trade


def test_exit_allocation_requires_full_quantity_and_supports_mixed_manual_close(
    monkeypatch,
):
    import backend.trading_engine as module

    fills = [
        _fill("ENTRY1", "E1", "BUY", 10, 100),
        _fill("EXIT1", "X1", "SELL", 4, 99),
        _fill("MANUAL1", "M1", "SELL", 6, 98),
        _fill("EXIT1", "FOREIGN", "SELL", 10, 50, account_id="other"),
    ]
    monkeypatch.setattr(module, "kite_client", _FillClient(fills))
    engine = TradingEngine()

    exit_price, reason, _, costs = engine._reconcile_execution("RELIANCE", _trade())
    assert exit_price == 98.4
    assert reason == "Target"
    assert costs["financial_quality"] == "RECONCILED"

    partial_price, partial_reason, _, _ = engine._reconcile_execution(
        "RELIANCE", _trade(quantity=11)
    )
    assert partial_price is None
    assert partial_reason == "UNRECONCILED"


def test_timeout_with_unavailable_position_is_unknown_and_not_flattened(monkeypatch):
    import backend.trading_engine as module

    class CancelClient:
        def cancel_order(self, **kwargs):
            return "cancel-requested"

    monkeypatch.setattr(module, "execution_gateway", CancelClient())
    engine = TradingEngine()
    engine._entry_fill_timeout_seconds = 0
    engine._entry_fill_poll_seconds = 0
    engine._find_live_position = lambda symbol, direction: None

    result = engine._wait_for_entry_fill(
        {"tradingsymbol": "RELIANCE", "direction": "BUY"}, "ENTRY1"
    )
    assert result is None
    assert engine._entry_recovery["ENTRY1"] == "UNKNOWN"


def test_late_partial_fill_is_retained_after_cancel(monkeypatch):
    import backend.trading_engine as module

    class CancelClient:
        def cancel_order(self, **kwargs):
            return "cancel-requested"

    monkeypatch.setattr(module, "execution_gateway", CancelClient())
    engine = TradingEngine()
    engine._entry_fill_timeout_seconds = 0
    engine._entry_fill_poll_seconds = 0
    engine._find_live_position = lambda symbol, direction: {
        "tradingsymbol": symbol,
        "quantity": 4,
        "exchange": "NSE",
        "product": "MIS",
    }
    engine._find_order = lambda order_id: {
        "order_id": order_id,
        "status": "CANCELLED",
        "filled_quantity": 4,
    }

    result = engine._wait_for_entry_fill(
        {"tradingsymbol": "RELIANCE", "direction": "BUY"}, "ENTRY1"
    )
    assert result["quantity"] == 4
    assert "ENTRY1" not in engine._entry_recovery


def test_real_risk_and_execution_gateway_accept_a_valid_entry(monkeypatch):
    import backend.execution_gateway as gateway_module
    import backend.risk_manager as risk_module
    import backend.trading_engine as engine_module

    class ScriptedBroker:
        def __init__(self):
            self.account_id = "acct-1"
            self.namespace = ExecutionNamespace.LIVE
            self.positions = []
            self.orders = []
            self.fills = []
            self.roles = {}
            self.next_id = 0

        def _positions(self):
            return normalize_positions_response(
                {"net": self.positions, "day": self.positions},
                namespace=ExecutionNamespace.LIVE,
                account_id="acct-1",
            )

        def get_positions_snapshot(self):
            return self._positions()

        def get_current_orders_snapshot(self):
            return normalize_orders_response(
                self.orders,
                namespace=ExecutionNamespace.LIVE,
                account_id="acct-1",
                roles_by_order_id=self.roles,
            )

        def get_fills_snapshot(self):
            return normalize_fills_response(
                self.fills,
                namespace=ExecutionNamespace.LIVE,
                account_id="acct-1",
            )

        def get_broker_snapshot(self):
            positions = self.get_positions_snapshot()
            orders = self.get_current_orders_snapshot()
            fills = self.get_fills_snapshot()
            return BrokerSnapshot(
                namespace=ExecutionNamespace.LIVE,
                account_id="acct-1",
                positions=positions.net,
                day_positions=positions.day,
                current_orders=orders.orders,
                fills=fills.fills,
                positions_quality=SnapshotQuality.COMPLETE,
                orders_quality=SnapshotQuality.COMPLETE,
                fills_quality=SnapshotQuality.COMPLETE,
                fetched_at=now_utc(),
            )

        def get_margins(self):
            return {"equity": {"available": {"live_balance": 10000}}}

        def get_instruments(self, exchange):
            return [
                {"tradingsymbol": "RELIANCE", "instrument_token": 1, "tick_size": 0.05}
            ]

        def place_order(self, **kwargs):
            self.next_id += 1
            order_id = f"O{self.next_id}"
            role = kwargs.get("order_role", OrderRole.UNKNOWN)
            self.roles[order_id] = role
            status = "TRIGGER PENDING" if role is OrderRole.PROTECTION else "COMPLETE"
            quantity = kwargs["quantity"]
            self.orders.append(
                {
                    "orderId": order_id,
                    "tradingsymbol": kwargs["tradingsymbol"],
                    "instrument_token": 1,
                    "exchange": kwargs["exchange"],
                    "product": kwargs["product"],
                    "transactionType": kwargs["transaction_type"],
                    "quantity": quantity,
                    "filledQuantity": quantity if status == "COMPLETE" else 0,
                    "pendingQuantity": 0 if status == "COMPLETE" else quantity,
                    "price": kwargs.get("price", 95),
                    "triggerPrice": kwargs.get("trigger_price"),
                    "orderType": kwargs["order_type"],
                    "status": status,
                }
            )
            if status == "COMPLETE":
                signed = quantity if kwargs["transaction_type"] == "BUY" else -quantity
                self.positions = [
                    {
                        "tradingsymbol": "RELIANCE",
                        "instrument_token": 1,
                        "exchange": "NSE",
                        "product": "MIS",
                        "quantity": signed,
                        "averagePrice": kwargs.get("price", 100),
                        "lastPrice": 100,
                        "realised": 0,
                        "unrealised": 0,
                    }
                ]
                self.fills.append(
                    {
                        "tradeId": f"F{self.next_id}",
                        "orderId": order_id,
                        "tradingsymbol": "RELIANCE",
                        "instrument_token": 1,
                        "exchange": "NSE",
                        "product": "MIS",
                        "transactionType": kwargs["transaction_type"],
                        "quantity": quantity,
                        "averagePrice": kwargs.get("price", 100),
                    }
                )
            return order_id

        def cancel_order(self, **kwargs):
            return kwargs["order_id"]

    broker = ScriptedBroker()
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    risk.kill_switch_active = False
    risk.daily_pnl = 0
    risk.incurred_fees = 0
    risk.date_str = now_utc().astimezone(EXCHANGE_TIMEZONE).strftime("%Y-%m-%d")
    config = dict(config_manager.get_risk_config())
    config.update({"startTradeAfter": "00:00", "noNewTradesAfter": "23:59"})
    monkeypatch.setattr(
        risk_module,
        "get_ist_now",
        lambda: datetime.datetime(2026, 9, 20, 10, 0, tzinfo=EXCHANGE_TIMEZONE),
    )
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(engine_module, "kite_client", broker)
    monkeypatch.setattr(engine_module, "execution_gateway", execution_gateway)
    monkeypatch.setattr(engine_module, "risk_manager", risk)
    monkeypatch.setattr(gateway_module, "kite_client", broker)
    monkeypatch.setattr(gateway_module, "risk_manager", risk)
    monkeypatch.setattr(engine_module.scanner, "evaluate_position", lambda *args: {})

    engine = TradingEngine()
    signal = {
        "id": "valid-1",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "MIS",
        "direction": "BUY",
        "entryPrice": 100,
        "stopLoss": 95,
        "target": 110,
        "quantity": 10,
        "timestamp": now_utc().isoformat(),
        "strategy": "contract-test",
    }
    assert engine.execute_signal(signal) is True
    assert "RELIANCE" in engine.active_trades
    assert engine.active_trades["RELIANCE"]["stop_order_id"] == "O2"
    assert risk.pending_entry_reservation_count == 0
