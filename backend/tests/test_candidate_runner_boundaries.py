"""Independent adversarial checks of candidate adapter causality and protection."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.backtesting.candidate_runner import CandidateRunner
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.broker_models import OrderRole
from backend.exit_management.models import ExitAction, ExposureState, ProtectionState
from backend.journal import TradeJournal
from backend.order_lifecycle import OrderLifecycleCoordinator
from backend.tests.exit_management.test_engine import _state, _thesis
from backend.tests.exit_management.test_review_engine import _trail_context

UTC = timezone.utc
ENTRY_AT = datetime(2026, 9, 21, 4, 20, tzinfo=UTC)


def _candle(start=ENTRY_AT, close=100.0):
    return {
        "date": start,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000.0,
    }


def _candidate(tmp_path, *, register=True, stop_limit=False, partial_entry=False):
    broker = SimulatedBroker(
        execution_policy=SimulationExecutionPolicy(
            slippage_bps=0.0, max_fill_fraction=0.5 if partial_entry else 1.0
        )
    )
    symbol = _thesis().symbol
    if partial_entry:
        broker.submit_coordinator_order(
            "entry-opportunity",
            {
                "tradingsymbol": symbol,
                "side": "BUY",
                "quantity": 20,
                "timestamp": ENTRY_AT.isoformat(),
                "role": OrderRole.ENTRY.value,
                "signal_info": {"stopLoss": 95.0},
            },
        )
        broker.process_candle(symbol, pd.Series(_candle()))
        assert broker.positions[symbol]["quantity"] == 10
    else:
        broker.place_market_order(
            symbol, "BUY", 10, 100.0, ENTRY_AT, {"stopLoss": 95.0}
        )
    if stop_limit:
        broker.set_protective_stop(
            symbol, 95.0, ENTRY_AT, stop_limit=True, limit_price=94.0
        )
    key = broker.position_snapshot(ENTRY_AT).net[0].key
    thesis = replace(
        _thesis(),
        position_key=key.as_string(),
        instrument_id=key.instrument_id,
        fill_binding=replace(
            _thesis().fill_binding,
            source_fill_ids=tuple(fill["fill_id"] for fill in broker.fills),
        ),
    )
    state = replace(_state(), position_key=key.as_string())
    coordinator = OrderLifecycleCoordinator(TradeJournal(str(tmp_path / "study.db")))
    runner = CandidateRunner(broker=broker, coordinator=coordinator)
    if register:
        runner.register_position(thesis=thesis, state=state)
    return runner, thesis, state


@pytest.mark.parametrize("field", ["available_at", "received_at"])
def test_candidate_rejects_future_candle_information_before_mutation(tmp_path, field):
    runner, thesis, _ = _candidate(tmp_path)
    at = ENTRY_AT + timedelta(minutes=5)
    candle = {**_candle(), field: at + timedelta(seconds=1)}
    before = tuple(runner.broker.events)
    with pytest.raises(ValueError):
        runner.on_event(at, candles={thesis.symbol: candle})
    assert tuple(runner.broker.events) == before
    assert runner.evaluations == []


def test_candidate_rejects_naive_event_times(tmp_path):
    runner, _, _ = _candidate(tmp_path)
    with pytest.raises(ValueError):
        runner.on_event((ENTRY_AT + timedelta(minutes=5)).replace(tzinfo=None))


@pytest.mark.parametrize(
    "exposure", [ExposureState.CLOSED, ExposureState.ENTRY_ABORTED, ExposureState.NEW]
)
def test_filled_candidate_cannot_register_a_nonopen_checkpoint(tmp_path, exposure):
    runner, thesis, state = _candidate(tmp_path, register=False)
    with pytest.raises(ValueError):
        runner.register_position(thesis=thesis, state=replace(state, exposure=exposure))
    assert runner.positions == {}


def test_immutable_candidate_thesis_cannot_bind_a_working_entry_remainder(tmp_path):
    with pytest.raises(ValueError):
        runner, thesis, state = _candidate(tmp_path, register=False, partial_entry=True)
        runner.register_position(thesis=thesis, state=state)


def test_candidate_binding_quantity_must_match_actual_entry_fills(tmp_path):
    runner, thesis, state = _candidate(tmp_path, register=False)
    bad_binding = replace(
        thesis.fill_binding, filled_quantity=11, initial_risk_budget=55.0
    )
    with pytest.raises(ValueError):
        runner.register_position(
            thesis=replace(thesis, fill_binding=bad_binding), state=state
        )


@pytest.mark.parametrize("defect", ["cancelled", "wrong_side", "undercovered"])
def test_candidate_registration_requires_real_residual_protection(tmp_path, defect):
    runner, thesis, state = _candidate(tmp_path, register=False)
    broker = runner.broker
    stop = broker.orders[broker.positions[thesis.symbol]["stop_order_id"]]
    if defect == "cancelled":
        broker.cancel_order(stop["order_id"], timestamp=ENTRY_AT)
    elif defect == "wrong_side":
        stop["side"] = stop["transaction_type"] = "BUY"
    else:
        stop["remaining_quantity"] = 9
    with pytest.raises(ValueError):
        runner.register_position(thesis=thesis, state=state)


def test_candidate_rejects_unregistered_exposure_before_advancing_events(tmp_path):
    runner, _, _ = _candidate(tmp_path)
    runner.broker.place_market_order(
        "UNMANAGED", "BUY", 1, 100.0, ENTRY_AT, {"stopLoss": 95.0}
    )
    with pytest.raises(ValueError):
        runner.on_event(ENTRY_AT + timedelta(minutes=5))


def _trail_event(runner, thesis):
    context = replace(_trail_context(), instrument_id=thesis.instrument_id)
    bar = context.primary_bar
    return runner.on_event(
        context.decision_event_time,
        candles={
            thesis.symbol: {
                "date": bar.start,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        },
        contexts={thesis.symbol: context},
    )


def test_candidate_structural_ratchet_preserves_stop_limit_execution(tmp_path):
    runner, thesis, _ = _candidate(tmp_path, stop_limit=True)
    result = _trail_event(runner, thesis)
    assert result[0].decision.action is ExitAction.TIGHTEN_STOP
    position = runner.broker.positions[thesis.symbol]
    stop = runner.broker.get_order(position["stop_order_id"])
    assert stop["order_type"] == "SL"
    assert stop["price"] < stop["trigger_price"]
    assert stop["remaining_quantity"] == position["quantity"]
    assert (
        runner.positions[thesis.symbol].management.confirmed_stop
        == stop["trigger_price"]
    )


def test_failed_structural_ratchet_cannot_claim_confirmed_protection(
    tmp_path, monkeypatch
):
    runner, thesis, _ = _candidate(tmp_path, stop_limit=True)

    def modification_failed(*args, **kwargs):
        raise RuntimeError("broker modification acknowledgement unavailable")

    monkeypatch.setattr(runner.broker, "set_protective_stop", modification_failed)
    result = _trail_event(runner, thesis)
    assert result[0].decision.action is ExitAction.TIGHTEN_STOP
    managed = runner.positions[thesis.symbol]
    assert managed.management.confirmed_stop == 95.0
    assert managed.state.protection is ProtectionState.UPDATE_PENDING
    assert managed.management.requested_stop == result[0].proposed_intent.stop_price
    stop = runner.broker.get_order(
        runner.broker.positions[thesis.symbol]["stop_order_id"]
    )
    assert stop["trigger_price"] == 95.0


def test_definitely_rejected_ratchet_releases_pending_update_for_future_management(
    tmp_path, monkeypatch
):
    from backend.broker_models import OrderSubmissionRejected

    runner, thesis, _ = _candidate(tmp_path, stop_limit=True)

    def rejected(*args, **kwargs):
        raise OrderSubmissionRejected("invalid stop modification")

    monkeypatch.setattr(runner.broker, "set_protective_stop", rejected)
    _trail_event(runner, thesis)
    managed = runner.positions[thesis.symbol]
    assert managed.management.confirmed_stop == 95.0
    assert managed.management.requested_stop is None
    assert managed.state.protection is ProtectionState.ACTIVE


def test_candidate_daily_loss_uses_synchronized_long_and_short_net_equity(tmp_path):
    runner, long_thesis, _ = _candidate(tmp_path)
    runner.daily_loss_limit = 20.0
    broker = runner.broker
    broker.place_market_order("SHORT", "SELL", 10, 100.0, ENTRY_AT, {"stopLoss": 105.0})
    key = broker._key_for("SHORT")
    short_thesis = replace(
        long_thesis,
        thesis_id="short-thesis",
        trade_id="short-trade",
        symbol="SHORT",
        position_key=key.as_string(),
        instrument_id=key.instrument_id,
        direction="SELL",
        initial_stop=105.0,
        objective=90.0,
        fill_binding=replace(
            long_thesis.fill_binding,
            source_fill_ids=tuple(
                fill["fill_id"] for fill in broker.fills if fill["symbol"] == "SHORT"
            ),
        ),
    )
    runner.register_position(
        thesis=short_thesis, state=replace(_state(), position_key=key.as_string())
    )
    # Both contemporaneous marks give zero aggregate gross P&L. Evaluating the
    # losing leg before marking the profitable leg must not latch account risk.
    first = runner.on_event(
        ENTRY_AT + timedelta(minutes=5),
        candles={
            long_thesis.symbol: _candle(close=102.0),
            "SHORT": _candle(close=102.0),
        },
    )
    assert runner.daily_loss_latched is False
    assert all(
        result.decision.primary_reason_code != "RISK_DAILY_LOSS" for result in first
    )
    assert runner.equity_curve[-1]["equity"] == broker.current_equity({})
    assert broker.initial_capital - broker.current_equity({}) < 20.0

    second = runner.on_event(
        ENTRY_AT + timedelta(minutes=10),
        candles={
            long_thesis.symbol: _candle(ENTRY_AT + timedelta(minutes=5), close=100.0),
            "SHORT": _candle(ENTRY_AT + timedelta(minutes=5), close=104.0),
        },
    )
    assert runner.daily_loss_latched is True
    assert len(second) == 2
    assert all(
        result.decision.primary_reason_code == "RISK_DAILY_LOSS" for result in second
    )
    assert all(result.decision.action is ExitAction.REQUEST_EXIT for result in second)
    assert broker.initial_capital - broker.current_equity({}) >= 40.0


@pytest.mark.parametrize("defect", [None, "wrong_side", "undercovered"])
def test_lost_ratchet_ack_requires_complete_correct_broker_stop_facts(
    tmp_path, monkeypatch, defect
):
    from backend.broker_models import OrderSubmissionUnknown
    from backend.tests.exit_management.test_engine import _context

    runner, thesis, _ = _candidate(tmp_path, stop_limit=True)
    amend = runner.broker.set_protective_stop
    submissions = []

    def accepted_without_ack(*args, **kwargs):
        submissions.append(True)
        amend(*args, **kwargs)
        raise OrderSubmissionUnknown("modification applied but response was lost")

    monkeypatch.setattr(runner.broker, "set_protective_stop", accepted_without_ack)
    first = _trail_event(runner, thesis)
    requested_stop = first[0].proposed_intent.stop_price
    assert runner.positions[thesis.symbol].management.confirmed_stop == 95.0
    stop = runner.broker.orders[runner.broker.positions[thesis.symbol]["stop_order_id"]]
    if defect == "wrong_side":
        stop["side"] = stop["transaction_type"] = "BUY"
    elif defect == "undercovered":
        stop["remaining_quantity"] = 9
    previous = _trail_context()
    context = replace(
        _context(
            previous.primary_bar.end,
            close=109.0,
            bars=previous.primary_bars,
            known_structure=previous.known_structure,
        ),
        instrument_id=thesis.instrument_id,
    )
    second = runner.on_event(
        context.decision_event_time,
        candles={thesis.symbol: _candle(context.primary_bar.start, close=109.0)},
        contexts={thesis.symbol: context},
    )
    managed = runner.positions[thesis.symbol]
    if defect is None:
        assert managed.management.confirmed_stop == requested_stop
        assert managed.management.requested_stop is None
        assert managed.state.protection is ProtectionState.ACTIVE
    else:
        assert managed.management.confirmed_stop == 95.0
        assert second[0].decision.primary_reason_code == "RISK_PROTECTION_FAILURE"
        assert second[0].decision.action is ExitAction.REQUEST_EXIT
    assert submissions == [True]


def test_late_candle_receipt_does_not_refresh_its_old_price_mark(tmp_path):
    runner, thesis, _ = _candidate(tmp_path)
    at = ENTRY_AT + timedelta(minutes=30)
    runner.on_event(
        at,
        candles={
            thesis.symbol: {
                **_candle(),
                "available_at": at,
                "received_at": at,
            }
        },
    )
    assert runner.equity_curve[-1]["stale_symbols"] == [thesis.symbol]
    snapshot = runner.broker.position_snapshot(at)
    assert snapshot.net[0].mark_time == ENTRY_AT + timedelta(minutes=5)
    assert runner.broker.trades == []
