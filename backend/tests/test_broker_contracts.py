from datetime import timedelta

import pytest

from backend.broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    OrderRole,
    SnapshotQuality,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
    order_to_renderer_dto,
)
from backend.time_utils import now_utc


def _snapshot(*, positions=None, orders=None, fills=None, account_id="acct"):
    fetched = now_utc()
    position_snapshot = normalize_positions_response(
        {"net": positions or [], "day": positions or []},
        namespace=ExecutionNamespace.LIVE,
        account_id=account_id,
        fetched_at=fetched,
    )
    order_snapshot = normalize_orders_response(
        orders or [],
        namespace=ExecutionNamespace.LIVE,
        account_id=account_id,
        roles_by_order_id={
            str(order.get("orderId", order.get("order_id"))): order.get(
                "role", OrderRole.UNKNOWN
            )
            for order in (orders or [])
        },
        fetched_at=fetched,
    )
    fill_snapshot = normalize_fills_response(
        fills or [],
        namespace=ExecutionNamespace.LIVE,
        account_id=account_id,
        fetched_at=fetched,
    )
    return BrokerSnapshot(
        namespace=ExecutionNamespace.LIVE,
        account_id=account_id,
        positions=position_snapshot.net,
        day_positions=position_snapshot.day,
        current_orders=order_snapshot.orders,
        fills=fill_snapshot.fills,
        positions_quality=position_snapshot.quality,
        orders_quality=order_snapshot.quality,
        fills_quality=fill_snapshot.quality,
        fetched_at=fetched,
        positions_fetched_at=fetched,
        orders_fetched_at=fetched,
        fills_fetched_at=fetched,
    )


def test_normalization_handles_sdk_aliases_and_keeps_unknown_financial_placeholders():
    positions = normalize_positions_response(
        {
            "net": [
                {
                    "tradingsymbol": "reliance",
                    "exchange": "nse",
                    "product": "mis",
                    "instrument_token": 738561,
                    "quantity": 10,
                    "averagePrice": 100,
                    "lastPrice": 101,
                    "buyValue": 0,
                    "sellValue": 0,
                }
            ],
            "day": [],
        },
        namespace=ExecutionNamespace.LIVE,
        account_id="U1",
    )
    assert positions.quality is SnapshotQuality.COMPLETE
    position = positions.net[0]
    assert position.key.tradingsymbol == "RELIANCE"
    assert position.key.instrument_id == "738561"
    assert position.last_price == 101
    assert position.buy_value == 0

    orders = normalize_orders_response(
        [
            {
                "orderId": "O1",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 10,
                "filledQuantity": 0,
                "pendingQuantity": 10,
                "price": 100,
                "orderType": "LIMIT",
                "status": "OPEN PENDING",
                "role": "ENTRY",
            }
        ],
        namespace=ExecutionNamespace.LIVE,
        account_id="U1",
        roles_by_order_id={"O1": OrderRole.ENTRY},
    )
    order = orders.orders[0]
    assert order.role is OrderRole.ENTRY
    assert order.is_working is True
    assert order_to_renderer_dto(order)["status"] == "OPEN PENDING"


def test_invalid_negative_price_degrades_the_snapshot_instead_of_entering_risk():
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 10,
                "averagePrice": -100,
                "lastPrice": 100,
            }
        ]
    )
    assert snapshot.positions == ()
    assert snapshot.positions_quality is SnapshotQuality.PARTIAL


def test_complete_is_not_entry_ready_when_stale_or_reads_are_skewed():
    snapshot = _snapshot()
    old = now_utc() - timedelta(seconds=121)
    stale = BrokerSnapshot(
        **{
            **snapshot.__dict__,
            "fetched_at": old,
            "positions_fetched_at": old,
            "orders_fetched_at": old,
            "fills_fetched_at": old,
        }
    )
    assert stale.entry_ready is False

    skewed = BrokerSnapshot(
        **{
            **snapshot.__dict__,
            "positions_fetched_at": now_utc() - timedelta(seconds=8),
            "orders_fetched_at": now_utc(),
            "fills_fetched_at": now_utc(),
        }
    )
    assert skewed.entry_ready is False


def test_unknown_account_is_not_entry_ready_even_when_payloads_are_complete():
    assert _snapshot(account_id="UNKNOWN").entry_ready is False


@pytest.mark.parametrize(
    "value", [True, False, 10**400], ids=["true", "false", "overflow"]
)
@pytest.mark.parametrize("field", ["last_price", "pnl", "multiplier"])
def test_malformed_numeric_position_values_cannot_become_account_truth(field, value):
    snapshot = _snapshot(
        positions=[
            dict(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                product="MIS",
                quantity=10,
                **{field: value},
            )
        ]
    )
    assert snapshot.positions_quality is SnapshotQuality.PARTIAL
    assert snapshot.positions == ()
    assert not snapshot.entry_ready


@pytest.mark.parametrize(
    "value", [True, False, 10**400], ids=["true", "false", "overflow"]
)
def test_malformed_execution_prices_degrade_order_and_fill_snapshots(value):
    identity = dict(
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        quantity=10,
        order_id="E1",
        transaction_type="BUY",
        average_price=value,
    )
    orders = normalize_orders_response(
        [dict(identity, order_type="LIMIT", status="COMPLETE", filled_quantity=10)]
    )
    fills = normalize_fills_response([dict(identity, trade_id="F1")])
    assert orders.quality is SnapshotQuality.PARTIAL
    assert orders.orders == ()
    assert fills.quality is SnapshotQuality.PARTIAL
    assert fills.fills == ()


@pytest.mark.parametrize(
    "status,working",
    [
        ("PUT ORDER REQ RECEIVED", True),
        ("AMO REQ RECEIVED", True),
        ("UNRECOGNIZED BROKER STATE", True),
        ("EXPIRED", False),
        ("REJECTED AMO", False),
    ],
)
def test_renderer_order_working_state_comes_from_canonical_contract(status, working):
    order = normalize_orders_response(
        [
            dict(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                product="MIS",
                quantity=10,
                order_id="E1",
                transaction_type="BUY",
                order_type="LIMIT",
                status=status,
            )
        ]
    ).orders[0]
    assert order_to_renderer_dto(order)["isWorking"] is working
