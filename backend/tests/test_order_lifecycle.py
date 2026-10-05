"""Phase-2 execution recovery contracts with a temporary SQLite journal."""

import pytest

from backend.broker_models import (
    OrderRole,
    OrderSubmissionRejected,
    OrderSubmissionUnknown,
)
from backend.journal import TradeJournal
from backend.order_lifecycle import AttemptState, IntentType, OrderLifecycleCoordinator

POSITION_KEY = "LIVE:acct:NSE:123:RELIANCE:MIS:epoch-1"


def test_unknown_submission_keeps_one_durable_tag_and_never_blind_retries(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    tags = []

    def timeout(tag):
        tags.append(tag)
        raise OrderSubmissionUnknown("timeout after write")

    first = coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.ENTER,
        role=OrderRole.ENTRY,
        side="BUY",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        submit_order=timeout,
    )
    second = coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.ENTER,
        role=OrderRole.ENTRY,
        side="BUY",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        submit_order=timeout,
    )

    assert first.state == AttemptState.UNKNOWN.value
    assert second.state == "RECONCILE_REQUIRED"
    assert tags == [first.attempt_tag]
    projection = journal.get_order_intent_projection(first.intent_id)
    assert projection["latest_attempt"]["attempt_tag"] == first.attempt_tag
    assert projection["latest_attempt"]["state"] == AttemptState.UNKNOWN.value
    restarted = TradeJournal(str(tmp_path / "journal.db"))
    recovered = restarted.list_unresolved_order_intents()
    assert recovered[0]["intent_id"] == first.intent_id
    assert recovered[0]["latest_attempt"]["attempt_tag"] == first.attempt_tag
    resumed = OrderLifecycleCoordinator(restarted).submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.ENTER,
        role=OrderRole.ENTRY,
        side="BUY",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        submit_order=timeout,
    )
    assert resumed.state == "RECONCILE_REQUIRED"
    assert tags == [first.attempt_tag]


def test_definite_rejection_is_distinct_from_unknown_and_can_be_replaced(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    calls = []

    def rejected(tag):
        calls.append(tag)
        raise OrderSubmissionRejected("input rejected")

    first = coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.PROTECT,
        role=OrderRole.PROTECTION,
        side="SELL",
        quantity=10,
        payload={"trigger_price": 95},
        submit_order=rejected,
    )
    second = coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.PROTECT,
        role=OrderRole.PROTECTION,
        side="SELL",
        quantity=10,
        payload={"trigger_price": 95},
        submit_order=lambda tag: calls.append(tag) or "STOP-2",
    )

    assert first.state == AttemptState.REJECTED.value
    assert second.state == AttemptState.ACKNOWLEDGED.value
    assert second.broker_order_id == "STOP-2"
    assert len(calls) == 2


def test_stop_handoff_rereads_and_reduces_only_actual_residual(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    submitted = []

    result = coordinator.handoff_protection_and_submit_reduction(
        position_key=POSITION_KEY,
        role=OrderRole.REDUCTION,
        side="SELL",
        requested_quantity=10,
        payload={"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS"},
        stop_order_id="STOP-1",
        cancel_stop=lambda order_id: order_id,
        read_order=lambda order_id: {
            "order_id": order_id,
            "status": "COMPLETE",
            "filled_quantity": 6,
        },
        read_residual=lambda observed_stop: {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 4,
            "last_price": 99,
        },
        submit_order=lambda tag, payload: submitted.append((tag, payload)) or "EXIT-1",
        reason="hard-stop-recovery",
        hard=True,
    )

    assert result.state == AttemptState.ACKNOWLEDGED.value
    assert result.broker_order_id == "EXIT-1"
    assert submitted[0][1]["quantity"] == 4
    assert submitted[0][1]["transaction_type"] == "SELL"
    assert journal.get_order_intent_projection(result.intent_id)["quantity"] == 10


def test_live_stop_handoff_never_places_an_opposing_reduction(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    submitted = []

    result = coordinator.handoff_protection_and_submit_reduction(
        position_key=POSITION_KEY,
        role=OrderRole.REDUCTION,
        side="SELL",
        requested_quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        stop_order_id="STOP-1",
        cancel_stop=lambda order_id: order_id,
        read_order=lambda order_id: {"order_id": order_id, "status": "TRIGGER PENDING"},
        read_residual=lambda observed_stop: {"quantity": 10},
        submit_order=lambda tag, payload: submitted.append((tag, payload)) or "EXIT-1",
    )

    assert result.state == "HANDOFF_PENDING"
    assert submitted == []
    assert journal.list_unresolved_order_intents()[0]["latched"] is True


def test_fill_ledger_deduplicates_a_replayed_broker_fill(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    submission = coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.ENTER,
        role=OrderRole.ENTRY,
        side="BUY",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        submit_order=lambda tag: "ENTRY-1",
    )

    first = coordinator.record_fill(
        position_key=POSITION_KEY,
        broker_fill_id="FILL-1",
        broker_order_id="ENTRY-1",
        side="BUY",
        quantity=4,
        fill_price=100.5,
    )
    duplicate = coordinator.record_fill(
        position_key=POSITION_KEY,
        broker_fill_id="FILL-1",
        broker_order_id="ENTRY-1",
        side="BUY",
        quantity=4,
        fill_price=100.5,
    )

    assert first is True
    assert duplicate is False
    projection = journal.get_order_intent_projection(submission.intent_id)
    assert projection["filled_quantity"] == 4
    assert projection["residual_quantity"] == 6


def _reduction(coordinator, **overrides):
    args = {
        "position_key": POSITION_KEY,
        "role": OrderRole.REDUCTION,
        "side": "SELL",
        "requested_quantity": 10,
        "payload": {"tradingsymbol": "RELIANCE"},
        "stop_order_id": "STOP-1",
        "cancel_stop": lambda order_id: order_id,
        "read_order": lambda order_id: {"order_id": order_id, "status": "CANCELLED"},
        "read_residual": lambda observed_stop: {"quantity": 10},
        "submit_order": lambda tag, payload: "EXIT-1",
    }
    args.update(overrides)
    return coordinator.handoff_protection_and_submit_reduction(**args)


def test_handoff_resumes_past_an_already_terminal_stop(tmp_path):
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "journal.db")))

    def cancel_terminal(_):
        raise AssertionError("terminal stops must not be cancelled again")

    result = _reduction(coordinator, cancel_stop=cancel_terminal)
    assert result.broker_order_id == "EXIT-1"


def test_handoff_reconciles_stop_fill_after_cancel_timeout(tmp_path):
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "journal.db")))
    observations = iter(
        [
            {"order_id": "STOP-1", "status": "TRIGGER PENDING"},
            {"order_id": "STOP-1", "status": "COMPLETE", "filled_quantity": 10},
        ]
    )
    submissions = []

    def cancel(_):
        raise OrderSubmissionUnknown("cancel timed out while stop filled")

    result = _reduction(
        coordinator,
        cancel_stop=cancel,
        read_order=lambda _: next(observations),
        read_residual=lambda observed_stop: {},
        submit_order=lambda tag, payload: submissions.append(payload),
    )
    assert result.state == "FLAT_PENDING_RECONCILIATION"
    assert submissions == []


def test_missing_acknowledged_order_does_not_authorize_replacement(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    submissions = []
    first = _reduction(
        coordinator,
        submit_order=lambda tag, payload: submissions.append(tag) or "EXIT-1",
    )
    observation = coordinator.observe_order(first.intent_id, {})
    second = _reduction(coordinator)
    assert observation.state == "RECONCILE_REQUIRED"
    assert second.state == "RECONCILE_REQUIRED"
    assert len(submissions) == 1
    assert (
        journal.get_order_intent_projection(first.intent_id)["latest_attempt"]["state"]
        == "ACKNOWLEDGED"
    )


def test_unrelated_order_and_delayed_working_fact_do_not_replace_terminal_state(
    tmp_path,
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    first = _reduction(coordinator)
    unrelated = coordinator.observe_order(
        first.intent_id,
        {"order_id": "OTHER", "status": "COMPLETE", "filled_quantity": 10},
    )
    assert unrelated.state == "RECONCILE_REQUIRED"
    coordinator.observe_order(
        first.intent_id,
        {"order_id": "EXIT-1", "status": "COMPLETE", "filled_quantity": 10},
    )
    delayed = coordinator.observe_order(
        first.intent_id, {"order_id": "EXIT-1", "status": "OPEN"}
    )
    assert delayed.state == "FILLED"
    assert (
        journal.get_order_intent_projection(first.intent_id)["latest_attempt"][
            "broker_order_id"
        ]
        == "EXIT-1"
    )


def test_unknown_protection_blocks_exit_even_without_checkpoint_stop_id(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)

    def unknown(_):
        raise OrderSubmissionUnknown("stop accepted but response lost")

    coordinator.submit(
        position_key=POSITION_KEY,
        intent_type=IntentType.PROTECT,
        role=OrderRole.PROTECTION,
        side="SELL",
        quantity=10,
        payload={"tradingsymbol": "RELIANCE"},
        submit_order=unknown,
    )
    submissions = []
    result = _reduction(
        coordinator,
        stop_order_id=None,
        submit_order=lambda tag, payload: submissions.append(payload),
    )
    assert result.state == "HANDOFF_PENDING"
    assert submissions == []


def test_second_handoff_cannot_submit_snapshot_from_before_first_exit_fill(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    submissions = []

    def stale_residual(observed_stop):
        # Another caller completes the exit while this caller's earlier
        # position read is in flight. The outer caller must discard that read.
        first = _reduction(
            coordinator,
            stop_order_id=None,
            submit_order=lambda tag, payload: submissions.append(payload) or "EXIT-1",
        )
        coordinator.observe_order(
            first.intent_id,
            {"order_id": "EXIT-1", "status": "COMPLETE", "filled_quantity": 10},
        )
        return {"quantity": 10}

    second = _reduction(
        coordinator,
        stop_order_id=None,
        read_residual=stale_residual,
        submit_order=lambda tag, payload: submissions.append(payload) or "EXIT-2",
    )
    assert second.state == "RECONCILE_REQUIRED"
    assert len(submissions) == 1


def test_late_submit_ack_cannot_overwrite_observed_fill(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)

    def submit(tag, payload):
        intent = journal.find_active_order_intent(POSITION_KEY, {"EXIT"})
        coordinator.observe_order(
            intent["intent_id"],
            {
                "order_id": "EXIT-1",
                "tag": tag,
                "status": "COMPLETE",
                "filled_quantity": 10,
            },
        )
        return "EXIT-1"

    result = _reduction(coordinator, submit_order=submit)
    assert (
        journal.get_order_intent_projection(result.intent_id)["latest_attempt"]["state"]
        == "FILLED"
    )


@pytest.mark.parametrize("response", [None, "", False])
def test_malformed_acknowledgement_retains_unknown_attempt(tmp_path, response):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    first = _reduction(coordinator, submit_order=lambda tag, payload: response)
    second = _reduction(coordinator)
    assert first.state == "UNKNOWN"
    assert first.broker_order_id is None
    assert second.state == "RECONCILE_REQUIRED"


def test_reduction_does_not_assume_ownership_of_an_opposite_residual(tmp_path):
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "journal.db")))
    submissions = []
    result = _reduction(
        coordinator,
        read_residual=lambda observed_stop: {"quantity": -3},
        submit_order=lambda tag, payload: submissions.append(payload),
    )
    assert result.state == "RECONCILE_REQUIRED"
    assert submissions == []


def test_matching_broker_order_id_from_another_account_cannot_complete_attempt(
    tmp_path,
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    coordinator = OrderLifecycleCoordinator(journal)
    first = _reduction(coordinator)
    result = coordinator.observe_order(
        first.intent_id,
        {
            "order_id": "EXIT-1",
            "account_id": "other-account",
            "status": "COMPLETE",
            "filled_quantity": 10,
        },
    )
    assert result.state == "RECONCILE_REQUIRED"
    assert (
        journal.get_order_intent_projection(first.intent_id)["latest_attempt"]["state"]
        == "ACKNOWLEDGED"
    )
