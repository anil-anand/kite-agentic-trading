"""Phase-7 live-orchestration contracts using fixed live-like facts.

These tests use the production journal/checkpoint boundary but never create an
authenticated broker.  They verify that the pure candidate policy receives a
managed position outside the entry watchlist, leaves broker mutation authority
with the existing controller, and retains every decision's input/state joins.
"""

from datetime import datetime, timedelta, timezone

from backend.exit_management.models import PositionCheckpoint
from backend.journal import journal
from backend.order_lifecycle import IntentType
from backend.tests.exit_management.test_engine import _context, _state, _thesis
from backend.trading_engine import TradingEngine

UTC = timezone.utc
START = datetime(2026, 9, 21, 4, 20, tzinfo=UTC)


def _managed_engine():
    """Create one reconciled, protected position with no screener membership."""

    engine = TradingEngine()
    thesis = _thesis()
    state = _state()
    journal.create_managed_position(
        thesis,
        PositionCheckpoint(
            position_key=thesis.position_key,
            state_version=state.version,
            sequence=0,
            state=state,
            counters={
                "exit_policy_mode": "shadow",
                "exit_policy": {},
            },
            protection={
                "confirmed_stop": 95.0,
                "confirmed_stop_order_id": "SL-1",
                "protected_quantity": 10,
            },
        ),
    )
    engine.active_trades["RELIANCE"] = {
        "tradingsymbol": "RELIANCE",
        "exit_management_position_key": thesis.position_key,
        "exit_policy_mode": "shadow",
        "direction": "BUY",
        "quantity": 10,
        "residual_quantity": 10,
        "entry_state": "OPEN",
        "entry_price": 100.0,
        "sl": 95.0,
        "target": 110.0,
        "exchange": "NSE",
        "product": "MIS",
        "instrument_id": "1",
        "namespace": "LIVE",
        "account_id": "acct-1",
        "position_epoch": "epoch-1",
    }
    return engine, thesis


def _position(mark, at):
    return {
        "position_key": "LIVE:acct-1:NSE:1:RELIANCE:MIS",
        "tradingsymbol": "RELIANCE",
        "quantity": 10,
        "last_price": mark,
        "mark_time": at.isoformat(),
        "exchange": "NSE",
        "product": "MIS",
        "instrument_token": "1",
        "namespace": "LIVE",
        "account_id": "acct-1",
    }


def test_shadow_exit_is_persisted_but_cannot_call_broker_mutation(monkeypatch):
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    context = _context(START, close=94.0)
    submitted = []

    monkeypatch.setattr(engine_module, "now_utc", lambda: START + timedelta(minutes=5))
    monkeypatch.setattr(
        engine_module.scanner, "get_market_context", lambda *args, **kwargs: context
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    monkeypatch.setattr(
        engine._order_lifecycle,
        "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )

    # The position is absent from the dynamic entry watchlist, yet its own
    # completed-bar context is assessed.
    assert engine.dynamic_watchlist == []
    engine._evaluate_shadow_position(
        _position(94.0, START + timedelta(minutes=5)), engine.active_trades["RELIANCE"]
    )

    decisions = journal.get_exit_decisions(thesis.position_key)
    assert len(decisions) == 1
    decision = decisions[0]["payload"]
    assert decision["action"] == "REQUEST_EXIT"
    assert decision["primary_reason_code"] == "RISK_CATASTROPHIC_STOP"
    assert decision["input_references"]["shadow"]["mode"] == "shadow"
    assert decision["trace"]["orchestration"]["dispatch"] == "SUPPRESSED_PHASE7"
    assert (
        decision["trace"]["orchestration"]["suppressed_intent"]["intent_type"] == "EXIT"
    )
    assert decision["trace"]["orchestration"]["legacy_control"]["reason"] == (
        "LEGACY_CONTROL_NOT_YET_EVALUATED"
    )
    assert submitted == []

    # The actual broker lifecycle remains OPEN; only the durable candidate
    # state has EXIT_PENDING. A shadow decision cannot block the approved
    # normal/hard reduction coordinator.
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["state"]["exposure"] == "OPEN"
    assert checkpoint["state"]["latched_exit_intent_id"] is None
    assert (
        checkpoint["counters"]["shadow_candidate_position_state"]["exposure"]
        == "EXIT_PENDING"
    )
    assert engine.active_trades["RELIANCE"]["shadow_exit_action"] == "REQUEST_EXIT"


def test_shadow_evaluates_once_per_completed_bar_and_records_quote_extrema(monkeypatch):
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    first_context = _context(START, close=100.0)
    second_context = _context(START + timedelta(minutes=5), close=101.0)
    current = {"context": first_context, "now": START + timedelta(minutes=5)}

    monkeypatch.setattr(engine_module, "now_utc", lambda: current["now"])
    monkeypatch.setattr(
        engine_module.scanner,
        "get_market_context",
        lambda *args, **kwargs: current["context"],
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)

    position = _position(100.0, current["now"])
    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])
    # Polling the same completed interval must not create another decision.
    engine._evaluate_shadow_position(position, engine.active_trades["RELIANCE"])
    assert len(journal.get_exit_decisions(thesis.position_key)) == 1

    current["context"] = second_context
    current["now"] = START + timedelta(minutes=10)
    engine._evaluate_shadow_position(
        _position(101.0, current["now"]), engine.active_trades["RELIANCE"]
    )
    assert len(journal.get_exit_decisions(thesis.position_key)) == 2

    # A timestamped tick is independently persisted as an extrema observation;
    # it does not require another completed-candle evaluation and never sends
    # an order.
    quote_at = START + timedelta(minutes=11)
    current["now"] = quote_at
    engine.enqueue_quote_event(
        {
            "tradingsymbol": "RELIANCE",
            "instrumentToken": "1",
            "lastPrice": 104.0,
            "observedAt": quote_at.isoformat(),
            "timestampQuality": "EXCHANGE",
        }
    )
    engine._consume_quote_events()
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["counters"]["exit_policy"]["observed_mfe_r"] > 0
    assert checkpoint["counters"]["exit_policy"]["eligible_completed_bars"] == 2
    assert len(journal.get_exit_decisions(thesis.position_key)) == 2


def test_candidate_intent_shape_is_accepted_by_the_common_coordinator(monkeypatch):
    """A scripted candidate proposal uses the same durable intent contract.

    Phase 7 does not dispatch this intent in live mode. Preparing it here proves
    that candidate output already fits the phase-2 coordinator without giving
    the candidate a broker mutation capability.
    """

    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    context = _context(START, close=94.0)
    monkeypatch.setattr(engine_module, "now_utc", lambda: START + timedelta(minutes=5))
    monkeypatch.setattr(
        engine_module.scanner, "get_market_context", lambda *args, **kwargs: context
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *args: 0.05)
    engine._evaluate_shadow_position(
        _position(94.0, START + timedelta(minutes=5)), engine.active_trades["RELIANCE"]
    )

    suppressed = journal.get_exit_decisions(thesis.position_key)[0]["payload"]["trace"][
        "orchestration"
    ]["suppressed_intent"]
    prepared = engine._order_lifecycle.prepare_intent(
        position_key=suppressed["position_key"],
        intent_type=IntentType(suppressed["intent_type"]),
        role="REDUCTION",
        side="SELL",
        quantity=10,
        payload={"source": "phase7-scripted-candidate"},
        trade_id=thesis.trade_id,
        reason=suppressed["reason_code"],
        latched=True,
    )
    assert prepared["intent_type"] == "EXIT"
    assert prepared["state"] == "PREPARED"
