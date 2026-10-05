"""Crash ordering and account isolation for the durable execution ledger."""

import sqlite3

import pytest

from backend.journal import TradeJournal

POSITION_KEY = "LIVE:account-a:NSE:123:RELIANCE:MIS:epoch-1"


def prepare(journal, suffix="1", position_key=POSITION_KEY):
    intent_id, attempt_id = f"intent-{suffix}", f"attempt-{suffix}"
    journal.create_order_intent(
        intent_id=intent_id,
        position_key=position_key,
        intent_type="ENTER",
        role="ENTRY",
        side="BUY",
        quantity=10,
    )
    journal.prepare_order_attempt(
        intent_id=intent_id, attempt_id=attempt_id, attempt_tag=f"tag-{suffix}"
    )
    return intent_id, attempt_id


def record_fill(journal, position_key=POSITION_KEY, **overrides):
    fields = {
        "broker_fill_id": "fill-1",
        "broker_order_id": "order-1",
        "position_key": position_key,
        "side": "BUY",
        "quantity": 10,
        "fill_price": 100.0,
    }
    fields.update(overrides)
    return journal.record_order_fill(**fields)


def test_fill_before_ack_is_allocated_without_another_broker_fill_read(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    intent_id, attempt_id = prepare(journal)
    journal.record_order_attempt_state(attempt_id, "UNKNOWN")
    assert record_fill(journal)
    restarted = TradeJournal(str(tmp_path / "journal.db"))
    restarted.record_order_attempt_state(
        attempt_id, "FILLED", broker_order_id="order-1"
    )
    projection = restarted.get_order_intent_projection(intent_id)
    assert projection["filled_quantity"] == 10
    assert projection["residual_quantity"] == 0
    assert not record_fill(restarted)
    assert restarted.get_order_intent_projection(intent_id)["filled_quantity"] == 10


@pytest.mark.parametrize(
    "other_key",
    [
        "PAPER:account-a:NSE:123:RELIANCE:MIS:epoch-1",
        "LIVE:account-b:NSE:123:RELIANCE:MIS:epoch-1",
    ],
)
def test_identical_broker_ids_are_independent_across_accounts_and_modes(
    tmp_path, other_key
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    for suffix, key in [("1", POSITION_KEY), ("2", other_key)]:
        intent_id, attempt_id = prepare(journal, suffix, key)
        journal.record_order_attempt_state(
            attempt_id, "FILLED", broker_order_id="order-1"
        )
        assert record_fill(journal, key)
        assert journal.get_order_intent_projection(intent_id)["filled_quantity"] == 10
        assert (
            journal.get_order_attempt_by_broker_order("order-1", position_key=key)[
                "attempt_id"
            ]
            == attempt_id
        )
    assert journal.get_order_attempt_by_broker_order("order-1") is None


def test_replayed_fill_enriches_missing_metadata_but_rejects_conflicts(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    assert record_fill(journal, fill_price=None)
    timestamp = "2026-09-01T04:00:00+00:00"
    assert not record_fill(journal, exchange_time=timestamp)
    row = journal._get_conn().execute("SELECT * FROM order_fill_ledger").fetchone()
    assert row["fill_price"] == 100
    assert row["exchange_time"] == timestamp
    with pytest.raises(ValueError, match="conflicting broker fill"):
        record_fill(journal, quantity=20)
    row = journal._get_conn().execute("SELECT * FROM order_fill_ledger").fetchone()
    assert row["quantity"] == 10


def test_conflicting_late_allocation_rolls_back_acknowledgement(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    intent_id, attempt_id = prepare(journal)
    record_fill(journal, side="SELL")
    with pytest.raises(ValueError, match="conflicts with its lifecycle owner"):
        journal.record_order_attempt_state(
            attempt_id, "FILLED", broker_order_id="order-1"
        )
    attempt = journal.get_order_intent_projection(intent_id)["latest_attempt"]
    assert attempt["state"] == "SUBMITTING"
    assert attempt["broker_order_id"] is None


@pytest.mark.parametrize("terminal", ["FILLED", "CANCELLED", "REJECTED"])
def test_delayed_ack_cannot_revive_terminal_attempt(tmp_path, terminal):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    intent_id, attempt_id = prepare(journal)
    journal.record_order_attempt_state(attempt_id, terminal)
    record_fill(journal)
    journal.record_order_attempt_state(
        attempt_id, "ACKNOWLEDGED", broker_order_id="order-1"
    )
    projection = journal.get_order_intent_projection(intent_id)
    assert projection["latest_attempt"]["state"] == terminal
    assert projection["filled_quantity"] == 10


def test_attempt_compare_and_swap_blocks_a_stale_completed_handoff(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    intent_id, attempt_id = prepare(journal)
    journal.record_order_attempt_state(attempt_id, "CANCELLED")
    journal.prepare_order_attempt(
        intent_id=intent_id, attempt_id="replacement", attempt_tag="replacement-tag"
    )
    journal.record_order_attempt_state("replacement", "FILLED")
    observed = journal.prepare_order_attempt(
        intent_id=intent_id,
        attempt_id="stale-handoff",
        attempt_tag="stale-tag",
        enforce_previous_attempt=True,
        expected_previous_attempt_id=attempt_id,
    )
    assert observed["attempt_id"] == "replacement"
    assert (
        journal._get_conn().execute("SELECT COUNT(*) FROM order_attempts").fetchone()[0]
        == 2
    )


def test_old_attempt_observations_do_not_replace_current_or_completed_intent(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    intent_id, attempt_id = prepare(journal)
    journal.record_order_attempt_state(attempt_id, "CANCELLED")
    journal.prepare_order_attempt(
        intent_id=intent_id, attempt_id="replacement", attempt_tag="replacement-tag"
    )
    journal.record_order_attempt_state(attempt_id, "ACKNOWLEDGED")
    assert journal.get_order_intent(intent_id)["state"] == "SUBMITTING"
    journal.complete_order_intent(intent_id, "CLOSED")
    journal.record_order_attempt_state("replacement", "FILLED")
    assert journal.get_order_intent(intent_id)["state"] == "CLOSED"


def test_hard_exit_escalation_survives_restart_and_preserves_initiating_reason(
    tmp_path,
):
    path = tmp_path / "journal.db"
    journal = TradeJournal(str(path))
    journal.create_order_intent(
        intent_id="exit-intent",
        position_key=POSITION_KEY,
        intent_type="EXIT",
        role="REDUCTION",
        side="SELL",
        quantity=10,
        reason="Target",
        payload={"recovery_trade": {"exit_reason": "Target"}},
    )
    journal.escalate_order_intent("exit-intent", "Stop Loss")
    restarted = TradeJournal(str(path))
    intent = restarted.get_order_intent("exit-intent")
    assert intent["intent_type"] == "FLATTEN"
    assert intent["latched"] is True
    assert intent["reason"] == "Stop Loss"
    assert intent["payload"]["initiating_reason"] == "Target"
    assert intent["payload"]["recovery_trade"]["exit_market_required"] is True
    version = intent["state_version"]
    restarted.escalate_order_intent("exit-intent", "Stop Loss")
    assert restarted.get_order_intent("exit-intent")["state_version"] == version
    restarted.complete_order_intent("exit-intent")
    restarted.escalate_order_intent("exit-intent", "Emergency")
    assert restarted.get_order_intent("exit-intent")["state"] == "CLOSED"


@pytest.mark.parametrize(
    ("side", "original", "requested", "looser"),
    [("SELL", 95, 100, 94), ("BUY", 105, 100, 106)],
)
def test_protection_request_and_confirmation_survive_restart_without_loosening(
    tmp_path, side, original, requested, looser
):
    path = tmp_path / "journal.db"
    journal = TradeJournal(str(path))
    journal.create_order_intent(
        intent_id="protect",
        position_key=POSITION_KEY,
        intent_type="PROTECT",
        role="PROTECTION",
        side=side,
        quantity=10,
        payload={"trigger_price": original, "recovery_trade": {"sl": original}},
    )
    journal.update_protection_trigger("protect", requested)
    restarted = TradeJournal(str(path))
    recovery = restarted.get_order_intent("protect")["payload"]["recovery_trade"]
    assert recovery["sl"] == original
    assert recovery["requested_stop_trigger"] == requested
    with pytest.raises(ValueError, match="cannot loosen"):
        restarted.update_protection_trigger("protect", looser, confirmed=True)
    restarted.update_protection_trigger("protect", requested, confirmed=True)
    payload = restarted.get_order_intent("protect")["payload"]
    assert payload["trigger_price"] == requested
    assert payload["recovery_trade"]["sl"] == requested
    assert "requested_stop_trigger" not in payload["recovery_trade"]
    restarted.complete_order_intent("protect", "PROTECTION_TERMINAL")
    with pytest.raises(ValueError, match="active protection"):
        restarted.update_protection_trigger("protect", requested)


def test_v1_fill_migration_preserves_execution_and_allocation(tmp_path):
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE order_fill_ledger ("
        "broker_fill_id TEXT PRIMARY KEY, intent_id TEXT, attempt_id TEXT, "
        "broker_order_id TEXT NOT NULL, position_key TEXT NOT NULL, side TEXT NOT NULL, "
        "quantity INTEGER NOT NULL, fill_price REAL, exchange_time TIMESTAMP, "
        "recorded_at TIMESTAMP NOT NULL, raw_fill TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO order_fill_ledger VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)",
        ("fill-1", "order-1", POSITION_KEY, "BUY", 10, 100, "2026-09-01", "{}"),
    )
    conn.commit()
    conn.close()
    journal = TradeJournal(str(path))
    assert not record_fill(journal)
    assert record_fill(journal, "PAPER:account-a:NSE:123:RELIANCE:MIS:epoch-1")
    assert (
        journal._get_conn()
        .execute("SELECT COUNT(*) FROM order_fill_ledger")
        .fetchone()[0]
        == 2
    )


def test_terminal_broker_facts_remain_scoped_after_late_working_observation(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _, attempt_id = prepare(journal)
    terminal = {"order_id": "order-1", "status": "CANCELLED", "filled_quantity": 4}
    journal.record_order_attempt_state(
        attempt_id, "CANCELLED", broker_order_id="order-1", details={"order": terminal}
    )
    journal.record_order_attempt_state(
        attempt_id,
        "WORKING",
        broker_order_id="order-1",
        details={"order": {**terminal, "status": "OPEN"}},
    )
    restarted = TradeJournal(str(tmp_path / "journal.db"))
    assert (
        restarted.get_terminal_order_fact(
            "order-1", namespace="LIVE", account_id="account-a"
        )
        == terminal
    )
    assert (
        restarted.get_terminal_order_fact(
            "order-1", namespace="PAPER", account_id="account-a"
        )
        is None
    )
    assert (
        restarted.get_terminal_order_fact(
            "order-1", namespace="LIVE", account_id="account-b"
        )
        is None
    )
