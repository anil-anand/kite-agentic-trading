"""Scripted candidate execution through the production recovery coordinator.

Only injected in-memory broker functions execute here. Live shadow orchestration
still has no dispatch path; these tests exercise the phase-7 integration gate.
"""

from datetime import timedelta

import pytest

from backend.broker_models import OrderRole, OrderSubmissionUnknown
from backend.journal import TradeJournal, journal
from backend.order_lifecycle import IntentType, OrderLifecycleCoordinator
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_exit_live_integration import START, _managed_engine, _position


@pytest.fixture
def candidate(monkeypatch):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    current = {
        "now": START + timedelta(minutes=5),
        "context": _context(START, close=98),
    }
    monkeypatch.setattr(module, "now_utc", lambda: current["now"])
    monkeypatch.setattr(
        module.scanner, "get_market_context", lambda *args, **kwargs: current["context"]
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    engine._evaluate_shadow_position(
        _position(98, current["now"]), engine.active_trades["RELIANCE"]
    )
    current.update(
        now=START + timedelta(minutes=10),
        context=_context(START + timedelta(minutes=5), close=97),
    )
    engine._evaluate_shadow_position(
        _position(97, current["now"]), engine.active_trades["RELIANCE"]
    )
    decision = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    proposal = decision["trace"]["orchestration"]["suppressed_intent"]
    assert proposal["intent_type"] == "EXIT"
    assert proposal["reason_code"] == "THESIS_BREAKOUT_FAILED"
    assert journal.list_unresolved_order_intents() == []
    return engine, thesis, proposal, decision["decision_id"]


def _dispatch(candidate, *, coordinator=None, **broker):
    engine, thesis, proposal, decision_id = candidate
    coordinator = coordinator or engine._order_lifecycle
    payload = {
        "tradingsymbol": thesis.symbol,
        "exchange": thesis.exchange,
        "product": "MIS",
        "candidate_decision_id": decision_id,
        "candidate_intent_id": proposal["intent_id"],
    }
    prepared = coordinator.prepare_intent(
        position_key=proposal["position_key"],
        intent_type=IntentType(proposal["intent_type"]),
        role=OrderRole.REDUCTION,
        side="SELL" if thesis.direction == "BUY" else "BUY",
        quantity=proposal["quantity"],
        payload=payload,
        trade_id=thesis.trade_id,
        reason=proposal["reason_code"],
        latched=True,
    )
    return coordinator.handoff_protection_and_submit_reduction(
        position_key=proposal["position_key"],
        role=OrderRole.REDUCTION,
        side=prepared["side"],
        requested_quantity=proposal["quantity"],
        payload=payload,
        stop_order_id="SL-1",
        trade_id=thesis.trade_id,
        reason=proposal["reason_code"],
        existing_intent_id=prepared["intent_id"],
        **broker,
    )


@pytest.mark.parametrize("stop_filled", [0, 6, 10])
def test_candidate_stop_cancel_fill_race_submits_only_reconciled_residual(
    candidate, stop_filled
):
    observations = []
    submitted = []
    stop = {"order_id": "SL-1", "status": "TRIGGER PENDING", "filled_quantity": 0}

    def cancel(order_id):
        observations.append(("cancel", order_id))
        stop.update(
            status="COMPLETE" if stop_filled == 10 else "CANCELLED",
            filled_quantity=stop_filled,
        )
        raise OrderSubmissionUnknown("cancel acknowledgement lost while stop filled")

    def residual(observed_stop):
        assert observed_stop["filled_quantity"] == stop_filled
        assert observed_stop["status"] in {"COMPLETE", "CANCELLED"}
        observations.append(("residual", 10 - stop_filled))
        return {"quantity": 10 - stop_filled, "last_price": 97.0}

    broker = {
        "cancel_stop": cancel,
        "read_order": lambda order_id: dict(stop),
        "read_residual": residual,
        "submit_order": lambda tag, payload: (
            submitted.append((tag, dict(payload))) or "EXIT-1"
        ),
    }
    first = _dispatch(candidate, **broker)
    second = _dispatch(candidate, **broker)
    if stop_filled == 10:
        assert first.state == second.state == "FLAT_PENDING_RECONCILIATION"
        assert submitted == []
    else:
        assert first.state == "ACKNOWLEDGED"
        assert second.state == "RECONCILE_REQUIRED"
        assert len(submitted) == 1
        assert submitted[0][1]["quantity"] == 10 - stop_filled
        assert submitted[0][1]["transaction_type"] == "SELL"
        assert submitted[0][1]["candidate_decision_id"] == candidate[3]
    assert observations.count(("cancel", "SL-1")) == 1
    projection = journal.get_order_intent_projection(first.intent_id)
    assert projection["reason"] == candidate[2]["reason_code"]
    assert projection["latched"] is True


def test_candidate_unknown_submission_keeps_one_attempt_across_restart(candidate):
    submitted = []

    def timeout(tag, payload):
        submitted.append((tag, dict(payload)))
        raise OrderSubmissionUnknown("broker accepted reduction; response lost")

    broker = {
        "cancel_stop": lambda _: pytest.fail("terminal stop was cancelled again"),
        "read_order": lambda order_id: {
            "order_id": order_id,
            "status": "CANCELLED",
            "filled_quantity": 6,
        },
        "read_residual": lambda stop: {"quantity": 4, "last_price": 97.0},
        "submit_order": timeout,
    }
    first = _dispatch(candidate, **broker)
    restarted = OrderLifecycleCoordinator(TradeJournal(str(journal.db_path)))
    second = _dispatch(candidate, coordinator=restarted, **broker)
    third = _dispatch(candidate, coordinator=restarted, **broker)
    assert first.state == "UNKNOWN"
    assert second.state == third.state == "RECONCILE_REQUIRED"
    assert first.intent_id == second.intent_id == third.intent_id
    assert len(submitted) == 1
    assert submitted[0][1]["quantity"] == 4
    projection = restarted.journal.get_order_intent_projection(first.intent_id)
    assert projection["latest_attempt"]["attempt_tag"] == submitted[0][0]
    assert projection["latest_attempt"]["state"] == "UNKNOWN"


def test_candidate_partial_exit_then_cancel_resumes_only_remaining_quantity(candidate):
    engine, thesis, _, _ = candidate
    remaining = {"quantity": 4}
    submitted = []

    def submit(tag, payload):
        submitted.append((tag, dict(payload)))
        return f"EXIT-{len(submitted)}"

    broker = {
        "cancel_stop": lambda _: pytest.fail("terminal stop was cancelled again"),
        "read_order": lambda order_id: {
            "order_id": order_id,
            "status": "CANCELLED",
            "filled_quantity": 6,
        },
        "read_residual": lambda stop: dict(remaining),
        "submit_order": submit,
    }
    first = _dispatch(candidate, **broker)
    fill = {
        "position_key": thesis.position_key,
        "broker_fill_id": "EXIT-FILL-1",
        "broker_order_id": "EXIT-1",
        "side": "SELL",
        "quantity": 2,
        "fill_price": 96.9,
    }
    assert engine._order_lifecycle.record_fill(**fill) is True
    assert engine._order_lifecycle.record_fill(**fill) is False
    engine._order_lifecycle.observe_order(
        first.intent_id,
        {"order_id": "EXIT-1", "status": "OPEN", "filled_quantity": 2},
    )
    remaining["quantity"] = 2
    pending = _dispatch(candidate, **broker)
    assert pending.state == "RECONCILE_REQUIRED"
    assert len(submitted) == 1
    engine._order_lifecycle.observe_order(
        first.intent_id,
        {"order_id": "EXIT-1", "status": "CANCELLED", "filled_quantity": 2},
    )
    replacement = _dispatch(candidate, **broker)
    assert replacement.state == "ACKNOWLEDGED"
    assert replacement.intent_id == first.intent_id
    assert [payload["quantity"] for _, payload in submitted] == [4, 2]
    assert submitted[0][0] != submitted[1][0]
    assert all(payload["transaction_type"] == "SELL" for _, payload in submitted)
