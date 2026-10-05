"""Daily risk requires matching cumulative executions at actual admission."""

from dataclasses import replace

import pytest

from backend.accounting import AccountingQuality, accounting_service
from backend.config import config_manager
from backend.tests.test_phase1_review_corrections import _snapshot
from backend.tests.test_review5_admission import fill, order, position
from backend.tests.test_review5_admission import risk as risk


def executed_position(quantity=800, unrealised=0):
    return {
        **position(quantity, quantity),
        "buy_quantity": quantity,
        "buy_value": quantity * 100,
        "sell_value": 0,
        "realised": 0,
        "unrealised": unrealised,
    }


@pytest.mark.parametrize("missing_tokens", [0, 1, 2])
def test_one_order_keeps_one_fee_cap_when_partial_fill_tokens_are_missing(
    missing_tokens,
):
    records = [fill("ENTRY", 300), dict(fill("ENTRY", 500), trade_id="ENTRY-FINAL")]
    for record in records[:missing_tokens]:
        record.pop("instrument_token")
    fills = _snapshot(fills=records).fills
    fees = accounting_service.fees_for_fills(fills)
    expected = accounting_service.calculator.calculate_turnover_charges(80000, "BUY")
    assert fees.brokerage == expected["brokerage"]
    assert fees.total == expected["total"]


@pytest.mark.parametrize("conflict", ["side", "instrument_id", "tradingsymbol"])
def test_same_order_with_conflicting_execution_identity_has_unknown_fees(conflict):
    fills = _snapshot(
        fills=[fill("ENTRY", 300), dict(fill("ENTRY", 500), trade_id="ENTRY-FINAL")]
    ).fills
    if conflict == "side":
        conflicting = replace(fills[1], side="SELL")
    else:
        conflicting = replace(
            fills[1], key=replace(fills[1].key, **{conflict: "DIFFERENT"})
        )
    assert accounting_service.fees_for_fills([fills[0], conflicting]) is None


def test_identical_order_ids_in_distinct_accounts_keep_distinct_fee_caps():
    first = _snapshot(fills=[fill("ENTRY", 800)]).fills[0]
    second = replace(first, key=replace(first.key, account_id="acct-B"))
    fees = accounting_service.fees_for_fills([first, second])
    one_order = accounting_service.calculator.calculate_turnover_charges(80000, "BUY")
    assert fees.brokerage == one_order["brokerage"] * 2
    assert fees.total == one_order["total"] * 2


@pytest.mark.parametrize("observed_quantity", [0, 100, 799, 801])
def test_incomplete_fill_book_is_not_reconciled_accounting(risk, observed_quantity):
    snapshot = _snapshot(
        positions=[executed_position()],
        orders=[order("ENTRY", quantity=800, status="COMPLETE")],
        fills=[fill("ENTRY", observed_quantity)] if observed_quantity else [],
    )
    projection = accounting_service.session_accounting(
        snapshot.day_positions, snapshot.fills, orders=snapshot.current_orders
    )
    assert projection.quality is AccountingQuality.UNAVAILABLE
    assert projection.net_risk_pnl is None
    assert (
        projection.incurred_fees
        == accounting_service.fees_for_fills(snapshot.fills).total
    )

    reservation_id, reason = risk.reserve_entry("INFY", "BUY", 1, 100, snapshot)
    assert reservation_id is None
    assert reason.startswith("RECONCILIATION_REQUIRED")
    assert risk.pending_entry_reservation_count == 0
    assert risk.incurred_fees == projection.incurred_fees

    coherent = _snapshot(
        positions=[executed_position()],
        orders=[order("ENTRY", quantity=800, status="COMPLETE")],
        fills=[fill("ENTRY", 800)],
    )
    reservation_id, reason = risk.reserve_entry("INFY", "BUY", 1, 100, coherent)
    assert reservation_id is not None, reason
    assert risk.accounting_quality == "RECONCILED"
    assert risk.daily_pnl == -risk.incurred_fees


@pytest.mark.parametrize("fault", ["quantity", "side", "identity", "new_execution"])
def test_order_book_must_agree_with_otherwise_complete_session_fills(fault):
    record = order("ENTRY", quantity=800, status="COMPLETE")
    if fault == "quantity":
        record.update(quantity=801, filled_quantity=801)
    elif fault == "side":
        record["transaction_type"] = "SELL"
    elif fault == "identity":
        record["instrument_token"] = 999
    records = [record]
    if fault == "new_execution":
        records.append(order("NEW", quantity=1, filled=1, status="COMPLETE"))
    snapshot = _snapshot(
        positions=[executed_position()],
        orders=records,
        fills=[fill("ENTRY", 800)],
    )
    projection = accounting_service.session_accounting(
        snapshot.day_positions, snapshot.fills, orders=snapshot.current_orders
    )
    assert projection.quality is AccountingQuality.UNAVAILABLE


@pytest.mark.parametrize("fault", ["missing_row", "residual", "turnover"])
def test_unmatched_day_position_evidence_cannot_report_complete_fees(fault):
    row = executed_position()
    if fault == "residual":
        row["quantity"] = 700
    elif fault == "turnover":
        row["buy_value"] = 90000
    snapshot = _snapshot(
        positions=[] if fault == "missing_row" else [row],
        fills=[fill("ENTRY", 800)],
    )
    projection = accounting_service.session_accounting(
        snapshot.day_positions, snapshot.fills
    )
    assert projection.quality is AccountingQuality.UNAVAILABLE
    assert projection.net_risk_pnl is None


def test_fresh_admission_snapshot_enforces_new_daily_loss_and_preserves_latch(risk):
    assert risk.daily_pnl == 0
    snapshot = _snapshot(
        positions=[executed_position(unrealised=-1100)],
        orders=[order("ENTRY", quantity=800, status="COMPLETE")],
        fills=[fill("ENTRY", 800)],
    )
    reservation_id, reason = risk.reserve_entry("INFY", "BUY", 1, 100, snapshot)
    assert reservation_id is None
    assert reason == "DAILY_LOSS_LIMIT"
    assert risk.kill_switch_active
    assert risk.pending_entry_reservation_count == 0

    rebound = replace(
        snapshot,
        day_positions=tuple(
            replace(row, unrealised_gross=1000) for row in snapshot.day_positions
        ),
    )
    assert risk.reserve_entry("INFY", "BUY", 1, 100, rebound)[0] is None
    assert risk.can_accept_position("INFY", "BUY", 1, 100, rebound)[0] is False


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_coherent_partial_orders_keep_fees_and_remaining_capacity(risk, side):
    buys = 800 if side == "BUY" else 0
    sells = 800 if side == "SELL" else 0
    records = [fill("ENTRY", 300, side), fill("ENTRY", 500, side)]
    records[1]["trade_id"] = "ENTRY-FINAL"
    for record in records:
        # Legacy executions may omit a token while the position/order know it.
        record.pop("instrument_token")
    snapshot = _snapshot(
        positions=[
            {
                **position(buys - sells, buys, sells),
                "buy_value": buys * 100,
                "sell_value": sells * 100,
                "realised": 0,
                "unrealised": 0,
            }
        ],
        orders=[order("ENTRY", side=side)],
        fills=records,
    )
    direction = "SELL" if side == "BUY" else "BUY"
    reservation_id, reason = risk.reserve_entry("INFY", direction, 1, 100, snapshot)
    assert reservation_id, reason
    assert risk.accounting_quality == "RECONCILED"
    expected = accounting_service.calculator.calculate_turnover_charges(80000, side)
    assert risk.incurred_fees == expected["total"]
    assert risk.daily_pnl == -expected["total"]
    assert risk._build_exposure_state(snapshot)[0].gross == 100100


def test_partial_fees_remain_a_loss_floor_when_later_reads_lose_fills(risk):
    rows = [fill(str(i), 200) for i in range(14)]
    snapshot = _snapshot(
        positions=[executed_position(quantity=4000, unrealised=-900)], fills=rows
    )
    risk.update_from_broker_snapshot(snapshot)
    known_fees = accounting_service.fees_for_fills(snapshot.fills).total
    assert known_fees > 100
    assert risk.accounting_quality == "UNAVAILABLE"
    assert risk.incurred_fees == known_fees
    assert risk.kill_switch_active
    risk.update_from_broker_snapshot(replace(snapshot, fills=()))
    assert risk.incurred_fees == known_fees
    assert risk.kill_switch_active


@pytest.mark.parametrize(
    "scope", [{"exchange": "BSE"}, {"product": "CNC"}, {"multiplier": 25}]
)
def test_unsupported_position_scope_blocks_equity_mis_accounting_and_admission(
    risk, scope
):
    snapshot = _snapshot(positions=[{**executed_position(), **scope}])
    projection = accounting_service.session_accounting(snapshot.day_positions, [])
    assert projection.quality is AccountingQuality.UNAVAILABLE
    assert projection.net_risk_pnl is None
    assert risk._build_exposure_state(snapshot)[1].startswith("UNSUPPORTED_EXPOSURE")
    assert risk.reserve_entry("INFY", "BUY", 1, 100, snapshot)[0] is None


@pytest.mark.parametrize("exchange,product", [("BSE", "MIS"), ("NSE", "CNC")])
def test_unsupported_entry_scope_is_not_sized_as_equity_mis(risk, exchange, product):
    reservation_id, reason = risk.reserve_entry(
        "INFY", "BUY", 1, 100, _snapshot(), exchange=exchange, product=product
    )
    assert reservation_id is None
    assert reason == "UNSUPPORTED_PRODUCT_OR_EXCHANGE"


def test_legacy_order_identity_occupies_its_existing_position_once(risk, monkeypatch):
    config = dict(config_manager.get_risk_config(), maxSimultaneousPositions=2)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    pending = order("PENDING", quantity=1, filled=0)
    pending.pop("instrument_token")
    snapshot = _snapshot(positions=[position(10, 10)], orders=[pending])
    state, reason = risk._build_exposure_state(snapshot)
    assert reason == "OK"
    assert state.gross == 1100
    assert len(state.occupied_keys) == 1
    assert risk.can_accept_position("INFY", "BUY", 1, 100, snapshot)[0]
