from backend.accounting import accounting_service
from backend.broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    OrderRole,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
)
from backend.config import config_manager
from backend.risk_manager import RiskManager
from backend.time_utils import now_utc


def _snapshot(*, positions=None, orders=None, fills=None):
    fetched = now_utc()
    positions = [
        {
            **position,
            "timestamp": position.get("timestamp", fetched.isoformat()),
        }
        if position.get("lastPrice") is not None
        else position
        for position in (positions or [])
    ]
    positions_result = normalize_positions_response(
        {"net": positions or [], "day": positions or []},
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-1",
        fetched_at=fetched,
    )
    order_roles = {
        str(item.get("orderId", item.get("order_id"))): item.get(
            "role", OrderRole.UNKNOWN
        )
        for item in (orders or [])
    }
    orders_result = normalize_orders_response(
        orders or [],
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-1",
        roles_by_order_id=order_roles,
        fetched_at=fetched,
    )
    fills_result = normalize_fills_response(
        fills or [],
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-1",
        fetched_at=fetched,
    )
    return BrokerSnapshot(
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-1",
        positions=positions_result.net,
        day_positions=positions_result.day,
        current_orders=orders_result.orders,
        fills=fills_result.fills,
        positions_quality=positions_result.quality,
        orders_quality=orders_result.quality,
        fills_quality=fills_result.quality,
        fetched_at=fetched,
        positions_fetched_at=fetched,
        orders_fetched_at=fetched,
        fills_fetched_at=fetched,
    )


def _risk_config(**overrides):
    config = {
        "maxDailyLoss": 1000,
        "maxDailyTrades": 10,
        "maxTradesPerSymbolPerDay": 2,
        "maxSimultaneousPositions": 5,
        "maxGrossExposure": 300000,
        "maxNetExposure": 100000,
        "maxSingleSymbolExposure": 100000,
        "maxSectorExposure": 300000,
        "maxCorrelatedExposure": 300000,
        "correlationThreshold": 0.7,
        "startTradeAfter": "00:00",
        "noNewTradesAfter": "23:59",
    }
    config.update(overrides)
    return config


def test_partial_fills_are_charged_once_per_order_group():
    fills = normalize_fills_response(
        [
            {
                "tradeId": "F1",
                "orderId": "O1",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 100,
                "averagePrice": 100,
            },
            {
                "tradeId": "F2",
                "orderId": "O1",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 100,
                "averagePrice": 100,
            },
        ],
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-1",
    ).fills
    grouped = accounting_service.fees_for_fills(fills)
    expected = accounting_service.calculator.calculate_turnover_charges(20000, "BUY")
    assert grouped is not None
    assert grouped.brokerage == expected["brokerage"]
    assert grouped.total == expected["total"]


def test_real_risk_manager_accepts_and_reserves_only_from_canonical_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: _risk_config())
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    snapshot = _snapshot()

    accepted, reason = risk.can_accept_position(
        "RELIANCE",
        "BUY",
        10,
        100,
        broker_snapshot=snapshot,
    )
    assert accepted is True, reason

    reservation_id, reason = risk.reserve_entry(
        "RELIANCE",
        "BUY",
        10,
        100,
        broker_snapshot=snapshot,
    )
    assert reservation_id is not None, reason
    assert risk.pending_entry_reservation_count == 1

    second_id, second_reason = risk.reserve_entry(
        "RELIANCE",
        "BUY",
        10,
        100,
        broker_snapshot=snapshot,
    )
    assert second_id is None
    assert second_reason == "DUPLICATE_POSITION_OR_ENTRY: RELIANCE"


def test_daily_capacity_is_atomic_across_reservations(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "get_risk_config",
        lambda: _risk_config(maxDailyTrades=1),
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    snapshot = _snapshot()
    first, _ = risk.reserve_entry("RELIANCE", "BUY", 10, 100, broker_snapshot=snapshot)
    second, reason = risk.reserve_entry("INFY", "BUY", 10, 100, snapshot)
    assert first is not None
    assert second is None
    assert reason == "MAX_DAILY_TRADES_LIMIT"


def test_pending_and_actual_exposure_use_both_sides_conservatively(monkeypatch):
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: _risk_config())
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 800,
                "averagePrice": 100,
                "lastPrice": 100,
            }
        ],
        orders=[
            {
                "orderId": "O1",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "SELL",
                "quantity": 800,
                "filledQuantity": 0,
                "pendingQuantity": 800,
                "price": 100,
                "orderType": "LIMIT",
                "status": "OPEN",
                "role": OrderRole.ENTRY,
            }
        ],
    )
    accepted, reason = risk.can_accept_position(
        "INFY", "BUY", 800, 100, broker_snapshot=snapshot
    )
    assert accepted is False
    assert reason.startswith("NET_EXPOSURE_LIMIT")


def test_filled_order_not_in_positions_is_not_treated_as_flat(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "get_risk_config",
        lambda: _risk_config(maxGrossExposure=50000),
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    snapshot = _snapshot(
        orders=[
            {
                "orderId": "O1",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 1000,
                "filledQuantity": 1000,
                "pendingQuantity": 0,
                "price": 100,
                "orderType": "MARKET",
                "status": "COMPLETE",
                "role": OrderRole.ENTRY,
            }
        ]
    )
    accepted, reason = risk.can_accept_position(
        "INFY", "BUY", 1, 100, broker_snapshot=snapshot
    )
    assert accepted is False
    assert reason.startswith("MARK_UNAVAILABLE")


def test_order_grouped_accounting_drives_the_daily_loss_latch(monkeypatch):
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: _risk_config())
    risk = RiskManager()
    fills = []
    for index in range(4):
        fills.extend(
            [
                {
                    "tradeId": f"B{index}",
                    "orderId": f"BUY{index}",
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "transactionType": "BUY",
                    "quantity": 1000,
                    "averagePrice": 100,
                },
                {
                    "tradeId": f"S{index}",
                    "orderId": f"SELL{index}",
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "transactionType": "SELL",
                    "quantity": 1000,
                    "averagePrice": 99.825,
                },
            ]
        )
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 0,
                "day_buy_quantity": 4000,
                "day_sell_quantity": 4000,
                "realised": -700,
                "unrealised": 0,
            }
        ],
        fills=fills,
    )
    risk.update_from_broker_snapshot(snapshot)
    accounting = accounting_service.session_accounting(
        snapshot.day_positions, snapshot.fills
    )
    assert accounting.quality.value == "RECONCILED"
    assert risk.incurred_fees == accounting.incurred_fees
    assert risk.daily_pnl == accounting.net_risk_pnl
    assert risk.kill_switch_active is True
    assert risk.can_trade()[0] is False
