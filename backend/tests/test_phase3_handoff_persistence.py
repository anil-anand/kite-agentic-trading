"""External cancellation fill constraints survive loss of JSON checkpoints."""

import sqlite3
from copy import deepcopy

import pytest

from backend.journal import TradeJournal


@pytest.fixture
def handoff(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    initial = {
        "namespace": "LIVE",
        "account_id": "account-a",
        "exchange": "NSE",
        "instrument_id": "111",
        "tradingsymbol": "RELIANCE",
        "product": "MIS",
        "direction": "BUY",
        "quantity": 7,
        "entry_price": 100,
    }
    journal.create_order_intent(
        intent_id="close",
        position_key="LIVE:account-a:NSE:111:RELIANCE:MIS:epoch",
        intent_type="FLATTEN",
        role="REDUCTION",
        side="SELL",
        quantity=7,
        payload={"recovery_trade": initial, "reason": "OPERATOR_POSITION_CLOSE"},
        latched=True,
    )
    order = {
        **{
            key: value
            for key, value in initial.items()
            if key not in {"direction", "entry_price"}
        },
        "order_id": "external-stop",
        "transaction_type": "SELL",
        "filled_quantity": 0,
        "status": "TRIGGER PENDING",
    }
    recovery = {
        **initial,
        "external_handoff_baselines": {"external-stop": 0},
        "external_handoff_orders": {"external-stop": order},
    }
    return journal, recovery


def test_sqlite_only_restart_retains_baseline_and_terminal_fill_evidence(handoff):
    journal, recovery = handoff
    journal.record_external_handoff("close", recovery)
    recovery["external_handoff_orders"]["external-stop"].update(
        status="CANCELLED", filled_quantity=3
    )
    journal.record_external_handoff("close", recovery)
    restored = TradeJournal(str(journal.db_path)).get_order_intent_projection("close")
    durable = restored["payload"]["recovery_trade"]
    assert durable["quantity"] == 7
    assert durable["external_handoff_baselines"] == {"external-stop": 0}
    assert durable["external_handoff_orders"]["external-stop"]["filled_quantity"] == 3
    assert durable["external_handoff_orders"]["external-stop"]["status"] == "CANCELLED"
    assert restored["state_version"] == 3


def test_handoff_amendment_preserves_other_recovery_fields_and_initial_quantity(
    handoff,
):
    journal, recovery = handoff
    recovery.update(quantity=4, entry_price=999, direction="SELL", account_id="other")
    result = journal.record_external_handoff("close", recovery)
    stored = result["payload"]["recovery_trade"]
    assert stored["quantity"] == 7
    assert stored["entry_price"] == 100
    assert stored["direction"] == "BUY"
    assert stored["account_id"] == "account-a"
    assert result["payload"]["reason"] == "OPERATOR_POSITION_CLOSE"


def test_repeated_handoff_and_stale_order_observation_do_not_regress_evidence(handoff):
    journal, recovery = handoff
    stale = deepcopy(recovery)
    recovery["external_handoff_orders"]["external-stop"].update(
        status="CANCELLED", filled_quantity=3
    )
    current = journal.record_external_handoff("close", recovery)
    assert (
        journal.record_external_handoff("close", recovery)["state_version"]
        == current["state_version"]
    )
    result = journal.record_external_handoff("close", stale)
    assert result["state_version"] == current["state_version"]
    assert (
        result["payload"]["recovery_trade"]["external_handoff_orders"]["external-stop"][
            "filled_quantity"
        ]
        == 3
    )


def test_handoff_cannot_replace_its_original_fill_baseline(handoff):
    journal, recovery = handoff
    journal.record_external_handoff("close", recovery)
    recovery["external_handoff_baselines"]["external-stop"] = 3
    recovery["external_handoff_orders"]["external-stop"]["filled_quantity"] = 3
    with pytest.raises(ValueError, match="baseline cannot change"):
        journal.record_external_handoff("close", recovery)


def test_handoff_requires_an_active_reduction_intent(handoff):
    journal, recovery = handoff
    journal.complete_order_intent("close")
    with pytest.raises(ValueError, match="active reduction"):
        journal.record_external_handoff("close", recovery)
    with pytest.raises(ValueError, match="unknown lifecycle"):
        journal.record_external_handoff("absent", recovery)


def test_handoff_event_failure_rolls_back_payload_and_propagates_before_cancel(
    handoff, monkeypatch
):
    journal, recovery = handoff
    before = journal.get_order_intent("close")
    real_event = journal._lifecycle_event_inner
    cancelled = []

    def fail_event(conn, intent_id, event_type, details, *args):
        if event_type == "external_handoff_recorded":
            raise sqlite3.OperationalError("injected event write failure")
        return real_event(conn, intent_id, event_type, details, *args)

    monkeypatch.setattr(journal, "_lifecycle_event_inner", fail_event)
    with pytest.raises(sqlite3.OperationalError, match="event write failure"):
        journal.record_external_handoff("close", recovery)
        cancelled.append("external-stop")
    assert not cancelled
    assert journal.get_order_intent("close") == before


def test_sqlite_write_rejection_propagates_before_external_cancel(handoff):
    journal, recovery = handoff
    journal._get_conn().execute(
        "CREATE TRIGGER reject_handoff BEFORE UPDATE ON order_intents "
        "BEGIN SELECT RAISE(ABORT, 'injected write rejection'); END"
    )
    cancelled = []
    with pytest.raises(sqlite3.IntegrityError, match="write rejection"):
        journal.record_external_handoff("close", recovery)
        cancelled.append("external-stop")
    assert not cancelled
    assert (
        "external_handoff_orders"
        not in journal.get_order_intent("close")["payload"]["recovery_trade"]
    )
