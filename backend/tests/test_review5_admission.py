"""Admission scenarios with SDK quantities across successive broker snapshots."""

import pytest

from backend.config import config_manager
from backend.risk_manager import RiskManager
from backend.tests.test_phase1_review_corrections import _risk_config, _snapshot
from backend.time_utils import now_utc


@pytest.fixture
def risk(monkeypatch):
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda _: None)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: _risk_config())
    manager = RiskManager()
    manager.reconciliation_status = "RECONCILED"
    manager._get_correlation = lambda *_: 0
    return manager


def order(order_id, side="BUY", quantity=1000, filled=800, status="OPEN", role="ENTRY"):
    return dict(
        order_id=order_id,
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        instrument_token=111,
        transaction_type=side,
        quantity=quantity,
        filled_quantity=filled,
        pending_quantity=quantity - filled if status == "OPEN" else 0,
        status=status,
        price=100,
        average_price=100,
        order_type="LIMIT",
        role=role,
    )


def fill(order_id, quantity, side="BUY"):
    return dict(
        trade_id="F-" + order_id,
        order_id=order_id,
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        instrument_token=111,
        transaction_type=side,
        quantity=quantity,
        average_price=100,
    )


def position(quantity=0, buys=0, sells=0):
    return dict(
        tradingsymbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        instrument_token=111,
        quantity=quantity,
        day_buy_quantity=buys,
        day_sell_quantity=sells,
        last_price=100,
        timestamp=now_utc(),
    )


@pytest.mark.parametrize("status", ["OPEN", "CANCELLED", "REJECTED", "COMPLETE"])
def test_unobserved_execution_survives_all_order_statuses_and_reconciles_once(
    risk, status
):
    qty = 800 if status == "COMPLETE" else 1000
    orders = [order("ENTRY", quantity=qty, status=status)]
    initial = _snapshot(
        positions=[position()], orders=orders, fills=[fill("ENTRY", 100)]
    )
    assert risk.can_accept_position("INFY", "BUY", 700, 100, initial)[0] is False
    state, reason = risk._build_exposure_state(initial)
    assert reason == "OK"
    assert state.gross == (100000 if status == "OPEN" else 80000)
    # A fresh position proves all 800 executions reflected, even while the fill
    # endpoint still exposes only 100. No duplicated execution notional.
    later = _snapshot(
        positions=[position(800, 800)], orders=orders, fills=[fill("ENTRY", 100)]
    )
    state, reason = risk._build_exposure_state(later)
    assert reason == "OK"
    assert state.gross == (100000 if status == "OPEN" else 80000)
    complete = _snapshot(
        positions=[position(800, 800)], orders=orders, fills=[fill("ENTRY", 800)]
    )
    assert risk._build_exposure_state(complete)[0].gross == state.gross


@pytest.mark.parametrize("conflict", ["quantity", "side", "working_remainder"])
def test_order_fill_conflicts_suspend_admission(risk, conflict):
    record = order("ENTRY")
    trade = fill(
        "ENTRY",
        801 if conflict == "quantity" else 100,
        "SELL" if conflict == "side" else "BUY",
    )
    if conflict == "working_remainder":
        record["pending_quantity"] = 10
    snapshot = _snapshot(positions=[position()], orders=[record], fills=[trade])
    assert risk.can_accept_position("INFY", "BUY", 1, 100, snapshot)[0] is False
    assert "CONFLICT" in risk._build_exposure_state(snapshot)[1]


@pytest.mark.parametrize("later_role", [None, "ENTRY", "MANUAL"])
def test_old_signed_round_trip_does_not_erase_later_exposure(risk, later_role):
    orders = [
        order("OLD_ENTRY", quantity=800, status="COMPLETE"),
        order(
            "OLD_EXIT", side="SELL", quantity=800, status="COMPLETE", role="REDUCTION"
        ),
    ]
    fills = [fill("OLD_ENTRY", 800), fill("OLD_EXIT", 800, "SELL")]
    old = _snapshot(positions=[position(0, 800, 800)], orders=orders, fills=fills)
    assert risk._build_exposure_state(old)[0].gross == 0
    assert risk.can_accept_position("INFY", "BUY", 800, 100, old)[0] is True
    if later_role is not None:
        fills.append(fill("NEW", 800))
        if later_role == "ENTRY":
            orders.append(order("NEW", quantity=800, status="COMPLETE"))
        later = _snapshot(positions=[position(0, 800, 800)], orders=orders, fills=fills)
        assert risk._build_exposure_state(later)[0].gross == 80000
        assert risk.can_accept_position("INFY", "BUY", 800, 100, later)[0] is False
        reflected = _snapshot(
            positions=[position(800, 1600, 800)], orders=orders, fills=fills
        )
        assert risk._build_exposure_state(reflected)[0].gross == 80000


def test_buy_entry_and_buy_reduction_do_not_cancel_economically(risk):
    snapshot = _snapshot(
        positions=[position()],
        orders=[
            order("ENTRY", quantity=800, status="COMPLETE"),
            order("REDUCTION", quantity=800, status="COMPLETE", role="REDUCTION"),
        ],
        fills=[fill("ENTRY", 800), fill("REDUCTION", 800)],
    )
    assert risk._build_exposure_state(snapshot)[0].gross == 160000
    assert risk.can_accept_position("INFY", "BUY", 1, 100, snapshot)[0] is False


@pytest.mark.parametrize(
    "quantities,roles,side,allowed",
    [
        ([800, 800], ["PROTECTION", "REDUCTION"], "SELL", False),
        ([200, 300, 300], ["PROTECTION", "REDUCTION", "REDUCTION"], "SELL", True),
        ([400, 401], ["REDUCTION", "REDUCTION"], "SELL", False),
        ([800, 800], ["PROTECTION", "PROTECTION"], "SELL", False),
        ([800, 800], ["PROTECTION", "REDUCTION"], "BUY", False),
    ],
)
def test_reducing_capacity_is_shared_across_the_whole_live_order_set(
    risk, quantities, roles, side, allowed
):
    orders = [
        order(str(i), side=side, quantity=qty, filled=0, role=roles[i])
        for i, qty in enumerate(quantities)
    ]
    snapshot = _snapshot(positions=[position(800, 800)], orders=orders)
    accepted, reason = risk.can_accept_position("INFY", "SELL", 800, 100, snapshot)
    assert accepted is allowed, reason
    if not allowed and side == "SELL":
        assert reason.startswith("CONFLICTING_REDUCERS")
    # Terminal reducers no longer consume a second allowance after cancellation.
    for record in orders:
        record.update(status="CANCELLED", pending_quantity=0)
    later = _snapshot(positions=[position(800, 800)], orders=orders)
    assert risk.can_accept_position("INFY", "SELL", 800, 100, later)[0] is True
