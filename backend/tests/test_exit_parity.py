"""Phase-8 identical-input candidate decision contracts."""

from datetime import datetime, timedelta, timezone

from backend.backtesting.backtest_engine import BacktestEngine
from backend.backtesting.paper_broker import PaperBroker
from backend.exit_management.engine import ExitPolicy
from backend.replay import (
    ExitReplayEvent,
    ReplayMode,
    assert_identical_exit_parity,
    replay_exit_decisions,
)
from backend.tests.exit_management.test_engine import _context, _risk, _state, _thesis

UTC = timezone.utc


def test_same_candidate_facts_have_identical_hash_in_every_adapter_mode():
    start = datetime(2026, 9, 21, 4, 20, tzinfo=UTC)
    first = _context(start, close=99.0)
    second = _context(start + timedelta(minutes=5), close=98.0)
    thesis = _thesis()
    state = _state()
    events = (
        ExitReplayEvent(first, _risk(first, mark=99.0)),
        ExitReplayEvent(second, _risk(second, mark=98.0)),
    )
    results = [
        replay_exit_decisions(
            thesis=thesis,
            position_state=state,
            management_state=None,
            policy=ExitPolicy(),
            events=events,
            mode=mode,
        )
        for mode in ReplayMode
    ]

    shared = assert_identical_exit_parity(results)
    assert shared.state_hash
    assert [item.decision.to_dict() for item in results[0].evaluations] == [
        item.decision.to_dict() for item in results[-1].evaluations
    ]


def test_backtest_exit_only_adapter_uses_the_same_replay_result():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=94.0)
    events = (ExitReplayEvent(context, _risk(context, mark=94.0)),)
    thesis = _thesis()
    state = _state()
    direct = replay_exit_decisions(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=events,
        mode=ReplayMode.REPLAY,
    )
    engine = BacktestEngine(None, mode=BacktestEngine.CANDIDATE_EXIT_REPLAY)
    from_backtest = engine.run_candidate_exit_replay(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=events,
    )

    assert from_backtest.state_hash == direct.state_hash
    assert engine.run_manifest["entry_policy"] == "FIXED_ENTRY_FILL_OPPORTUNITIES"
    assert engine.run_manifest["mode"] == BacktestEngine.CANDIDATE_EXIT_REPLAY


def test_paper_adapter_uses_the_same_candidate_transition():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=UTC), close=99.0)
    events = (ExitReplayEvent(context, _risk(context, mark=99.0)),)
    thesis = _thesis()
    state = _state()
    paper = PaperBroker(data_source_id="fixed-paper-feed")
    paper_result = paper.replay_candidate_exit(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=events,
    )
    replay_result = replay_exit_decisions(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=events,
        mode=ReplayMode.REPLAY,
    )

    assert paper_result.state_hash == replay_result.state_hash


def test_retained_live_shadow_inputs_replay_identically_offline(monkeypatch):
    """Exercise the real phase-7 adapter/persistence boundary, not mode labels."""
    from backend.journal import journal
    from backend.replay import replay_recorded_exit_decision
    from backend.tests.test_exit_shadow_state import _evaluate, _live

    engine, thesis, current = _live(monkeypatch)
    _evaluate(engine, current)
    current["context"] = _context(datetime(2026, 9, 21, 4, 25, tzinfo=UTC), close=97.0)
    _evaluate(engine, current)
    records = [
        item["payload"] for item in journal.get_exit_decisions(thesis.position_key)
    ]
    assert len(records) == 2

    def unavailable(*args, **kwargs):
        raise AssertionError("offline replay must not query the live journal")

    monkeypatch.setattr(journal, "get_managed_position", unavailable)
    monkeypatch.setattr(journal, "get_exit_decisions", unavailable)
    for saved in records:
        replayed = replay_recorded_exit_decision(saved)
        assert replayed.decision.decision_id == saved["decision_id"]
        assert replayed.next_position_state.to_dict() == saved["trace"]["state_after"]
        assert (
            replayed.next_management_state.to_dict()
            == saved["trace"]["management_after"]
        )
