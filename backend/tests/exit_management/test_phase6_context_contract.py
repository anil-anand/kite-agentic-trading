"""Offline phase-4 context -> phase-5 thesis/checkpoint -> phase-6 decisions."""

import json

import pandas as pd
import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import (
    ExitAction,
    ExposureState,
    LifecycleEvent,
    ManagementState,
    PositionState,
    ProtectionState,
    ThesisHealth,
    reduce_lifecycle,
)
from backend.exit_management.thesis import (
    EntryThesis,
    bind_terminal_fill,
    capture_entry_thesis,
)
from backend.market_context import ContextQuality, MarketContextService
from backend.risk_rules import HardRiskSnapshot
from backend.session_clock import SessionClock, SessionPolicy
from backend.tests.conftest import build_candles

from .test_thesis import _config, _signal


def _pipeline(side, *, missing_entry_atr=False):
    sign = 1 if side == "BUY" else -1
    closes = [99.0] * 24 + [100.5, 99.1, 99.0]
    prices = [100.0 + sign * (price - 100.0) for price in closes]
    frame = build_candles(
        prices,
        opens=prices,
        highs=[price + 0.5 for price in prices],
        lows=[price - 0.5 for price in prices],
        dates=pd.date_range(
            "2026-09-21 09:15:00", periods=len(prices), freq="5min", tz="Asia/Kolkata"
        ),
    )
    # The same dataset includes future bars at each earlier decision. Real
    # normalization must apply their known availability before deriving inputs.
    frame["received_at"] = frame["date"] + pd.Timedelta(minutes=5)
    service = MarketContextService()
    entry_at = frame.iloc[24]["received_at"].to_pydatetime()
    entry_context = service.build("1", frame, entry_at)
    assert entry_context.primary_quality.status is ContextQuality.VALID
    assert len(entry_context.primary_bars) == 25
    assert entry_context.atr > 0
    snapshot = entry_context.summary()
    if missing_entry_atr:
        snapshot["atr"] = None
        snapshot["observation_quality"]["atr"] = "UNAVAILABLE"
    signal = {
        **_signal(),
        "direction": side,
        "entryPrice": prices[24],
        "stopLoss": prices[24] - sign * 5.0,
        "target": prices[24] + sign * 10.0,
        "market_context": snapshot,
        "indicators": {"atr": snapshot["atr"], "vwap": snapshot["session_vwap"]},
        "reasoning": f"Breakout: {side} breakout detected with trend confirmation",
    }
    signal["selected_evidence"] = [
        {**item, "direction": side} for item in signal["selected_evidence"]
    ]
    signal["selected_evidence"][0]["levels"] = {
        "range_high" if side == "BUY" else "range_low": 99.5 if side == "BUY" else 100.5
    }
    draft = capture_entry_thesis(
        signal,
        position_key=f"REPLAY:fixture:NSE:1:RELIANCE:MIS:{side}",
        trade_id=f"contract-{side}",
        position_epoch=side,
        instrument_id="1",
        effective_config=_config(),
        created_at=entry_at,
    )
    thesis = bind_terminal_fill(
        draft,
        entry_vwap=prices[24],
        filled_quantity=10,
        terminal_at=entry_at,
        source_fill_ids=(f"entry-fill-{side}",),
    )
    # Check the actual persisted thesis representation, not a hand-written
    # substitute for the phase-5 profile or causal anchor contract.
    thesis = EntryThesis.from_dict(json.loads(json.dumps(thesis.to_dict())))
    assert thesis.management_profile.name == "breakout_follow_through"
    boundary = thesis.management_profile.values["entry_boundary"]
    assert boundary["high" if side == "BUY" else "low"] == (
        99.5 if side == "BUY" else 100.5
    )
    assert entry_context.primary_bar.bar_id not in boundary["source_bar_ids"]
    state = reduce_lifecycle(
        PositionState(position_key=thesis.position_key),
        LifecycleEvent.ENTRY_INTENT_COMMITTED,
        event_id="entry-committed",
        occurred_at=entry_at,
    )
    state = reduce_lifecycle(
        state,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        event_id="entry-filled-protected",
        occurred_at=entry_at,
        known_quantity=10,
        protection=ProtectionState.ACTIVE,
        thesis_health=ThesisHealth.VALID,
    )
    contexts = [
        service.build("1", frame, frame.iloc[index]["received_at"].to_pydatetime())
        for index in (25, 26)
    ]
    return thesis, state, contexts


def _risk_for(thesis, context):
    return HardRiskSnapshot(
        session=SessionClock(SessionPolicy()).snapshot(context.decision_event_time),
        position_key=thesis.position_key,
        signed_quantity=10 if thesis.direction == "BUY" else -10,
        direction=thesis.direction,
        mark_price=context.primary_bar.close,
        mark_time=context.decision_event_time,
        hard_stop_price=thesis.initial_stop,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_real_context_and_persisted_thesis_confirm_a_failed_breakout(side):
    thesis, state, contexts = _pipeline(side)
    policy = ExitPolicy()
    first = evaluate_exit(
        thesis, state, contexts[0], _risk_for(thesis, contexts[0]), policy
    )

    assert first.decision.action is ExitAction.HOLD
    assert first.evidence.predicates["entry_boundary_failure"] is True
    assert first.next_management_state.failure_count == 1
    assert first.next_management_state.eligible_completed_bars == 1

    state = PositionState.from_dict(
        json.loads(json.dumps(first.next_position_state.to_dict()))
    )
    management = ManagementState.from_dict(
        json.loads(json.dumps(first.next_management_state.to_dict()))
    )
    duplicate = evaluate_exit(
        thesis,
        state,
        contexts[0],
        _risk_for(thesis, contexts[0]),
        policy,
        management_state=management,
    )
    assert duplicate.decision.primary_reason_code == "DATA_DUPLICATE_BAR"
    assert duplicate.next_management_state.failure_count == 1
    assert duplicate.next_management_state.eligible_completed_bars == 1

    second = evaluate_exit(
        thesis,
        duplicate.next_position_state,
        contexts[1],
        _risk_for(thesis, contexts[1]),
        policy,
        management_state=duplicate.next_management_state,
    )

    assert second.decision.action is ExitAction.REQUEST_EXIT
    assert second.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert second.next_position_state.thesis_health is ThesisHealth.INVALIDATED
    assert second.next_position_state.exposure is ExposureState.EXIT_PENDING
    assert second.next_management_state.failure_count == 2
    assert second.next_management_state.eligible_completed_bars == 2
    assert second.proposed_intent.quantity == 10
    assert second.next_management_state.observed_mae_r < 1.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_missing_required_entry_input_never_advances_eligible_bar_memory(side):
    thesis, state, contexts = _pipeline(side, missing_entry_atr=True)
    management = ManagementState(eligible_completed_bars=4, failure_count=1)

    result = evaluate_exit(
        thesis,
        state,
        contexts[0],
        _risk_for(thesis, contexts[0]),
        ExitPolicy(),
        management_state=management,
    )

    assert result.evidence.predicates["required_context_usable"] is False
    assert result.decision.primary_reason_code == "HOLD_DATA_DEGRADED"
    assert result.decision.action is ExitAction.HOLD
    assert result.next_management_state.eligible_completed_bars == 4
    assert result.next_management_state.failure_count == 0
    assert result.next_management_state.last_processed_primary_bar_id is None
    assert result.next_management_state.completed_mfe_r is None
