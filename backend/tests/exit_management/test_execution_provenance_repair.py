"""Missing timestamps can be repaired without rewriting economic risk."""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.thesis import EntryThesis, bind_terminal_fill
from backend.replay import replay_recorded_exit_decision
from backend.time_utils import as_utc

from .test_engine import _context, _risk, _state, _thesis


def test_repaired_terminal_boundary_enables_confirmation_and_exact_replay():
    original = _thesis()
    timestamp = original.fill_binding.entry_terminal_at
    missing = replace(
        original, fill_binding=replace(original.fill_binding, entry_terminal_at=None)
    )
    # Round-trip through the persisted representation before metadata arrives.
    restored = EntryThesis.from_dict(missing.to_dict())
    repaired = bind_terminal_fill(
        restored,
        entry_vwap=100,
        filled_quantity=10,
        terminal_at=timestamp,
        source_fill_ids=("entry-fill",),
    )
    assert (
        repaired.fill_binding.initial_r_per_share
        == original.fill_binding.initial_r_per_share
    )
    assert (
        repaired.fill_binding.initial_risk_budget
        == original.fill_binding.initial_risk_budget
    )
    state, memory = _state(), None
    for index in range(2):
        context = _context(as_utc(timestamp) + timedelta(minutes=5 * index), close=98)
        result = evaluate_exit(
            repaired,
            state,
            context,
            _risk(context, mark=98),
            ExitPolicy(),
            management_state=memory,
        )
        assert (
            replay_recorded_exit_decision(result.decision.to_dict()).decision
            == result.decision
        )
        state, memory = result.next_position_state, result.next_management_state
        assert memory.failure_count == index + 1
    assert result.decision.primary_reason_code == "THESIS_BREAKOUT_FAILED"
    assert result.decision.action.value == "REQUEST_EXIT"


@pytest.mark.parametrize("change", ["fills", "price", "timestamp"])
def test_provenance_repair_rejects_changed_identity_or_economics(change):
    original = _thesis()
    args = dict(
        entry_vwap=100,
        filled_quantity=10,
        terminal_at=original.fill_binding.entry_terminal_at,
        first_fill_at=original.fill_binding.entry_terminal_at,
        source_fill_ids=("entry-fill",),
    )
    if change == "fills":
        args["source_fill_ids"] = ("another-fill",)
    elif change == "price":
        args["entry_vwap"] = 101
    else:
        args["terminal_at"] = as_utc(args["terminal_at"]) + timedelta(seconds=1)
    with pytest.raises(ValueError):
        bind_terminal_fill(original, **args)
