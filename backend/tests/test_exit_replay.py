"""Replay isolation and deterministic state contracts."""

from datetime import datetime, timezone

from backend.exit_management.engine import ExitPolicy
from backend.replay import ExitReplayEvent, ReplayMode, replay_exit_decisions
from backend.tests.exit_management.test_engine import _context, _risk, _state, _thesis


def test_replay_is_deterministic_and_does_not_need_live_services():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=99.0)
    event = ExitReplayEvent(context, _risk(context, mark=99.0), label="fixture")
    thesis = _thesis()
    state = _state()
    first = replay_exit_decisions(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=(event,),
        mode=ReplayMode.REPLAY,
    )
    second = replay_exit_decisions(
        thesis=thesis,
        position_state=state,
        management_state=None,
        policy=ExitPolicy(),
        events=(event,),
        mode=ReplayMode.REPLAY,
    )

    assert first.state_hash == second.state_hash
    assert first.decisions[0].to_dict() == second.decisions[0].to_dict()


def test_retained_trace_replay_restores_nested_artifacts_and_detects_tampering():
    import copy
    from dataclasses import replace

    import pytest

    from backend.exit_management.engine import evaluate_exit
    from backend.exit_management.profiles import ManagementProfile
    from backend.market_context import KnownLevel, SetupRange
    from backend.replay import replay_recorded_exit_decision

    at = datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc)
    context = _context(at, close=94.0)
    context = replace(
        context,
        higher_bar=context.primary_bar,
        higher_bars=context.primary_bars,
        known_structure=(KnownLevel("pivot", "swing_low", 96.0, at, at, ("source",)),),
        setup_range=SetupRange(95.0, 103.0, at, at, at, ("source",)),
        primary_quality=replace(
            context.primary_quality,
            missing_bars=(at,),
            latest_bar_end=at,
            latest_available_at=at,
        ),
    )
    profile = ManagementProfile(name="breakout_follow_through")
    policy = ExitPolicy(profile_overrides={profile.name.value: profile})
    original = evaluate_exit(_thesis(), _state(), context, _risk(context, 94), policy)
    record = original.decision.to_dict()
    replayed = replay_recorded_exit_decision(record)
    assert replayed == original
    corrupt = copy.deepcopy(record)
    corrupt["trace"]["state_after"]["known_quantity"] = 9
    with pytest.raises(AssertionError, match="state_after"):
        replay_recorded_exit_decision(corrupt)


def test_replay_rejects_reversed_clocks_and_future_context():
    from dataclasses import replace
    from datetime import timedelta

    import pytest

    start = datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc)
    early = _context(start)
    late = _context(start + timedelta(minutes=5))
    kwargs = dict(
        thesis=_thesis(),
        position_state=_state(),
        management_state=None,
        policy=ExitPolicy(),
    )
    with pytest.raises(ValueError, match="causal clock order"):
        replay_exit_decisions(
            **kwargs,
            events=(
                ExitReplayEvent(late, _risk(late)),
                ExitReplayEvent(early, _risk(early)),
            ),
        )
    with pytest.raises(ValueError, match="not available"):
        replay_exit_decisions(
            **kwargs,
            events=(
                ExitReplayEvent(
                    replace(early, received_at=late.received_at), _risk(early)
                ),
            ),
        )


def test_hard_risk_events_do_not_advance_normal_bar_confirmation():
    from dataclasses import replace
    from datetime import timedelta

    import pytest

    from backend.replay import ReplayEventKind

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=98.0)
    first = ExitReplayEvent(context, _risk(context, 98))
    risk = first.risk_snapshot
    quote_risk = replace(
        risk,
        session=replace(
            risk.session, observed_at=risk.session.observed_at + timedelta(seconds=1)
        ),
    )
    with pytest.raises(ValueError, match="completed-bar"):
        ExitReplayEvent(context, quote_risk, kind=ReplayEventKind.HARD_RISK)
    result = replay_exit_decisions(
        thesis=_thesis(),
        position_state=_state(),
        management_state=None,
        policy=ExitPolicy(),
        events=(
            first,
            ExitReplayEvent(None, quote_risk, kind=ReplayEventKind.HARD_RISK),
        ),
    )
    assert result.management_state.eligible_completed_bars == 1
    assert result.management_state.failure_count == 1
    assert result.position_state.known_quantity == 10


def test_replay_applies_broker_residual_and_flat_facts_before_next_decision():
    from dataclasses import replace
    from datetime import timedelta

    from backend.exit_management.models import (
        ExposureState,
        LifecycleEvent,
        ProtectionState,
    )
    from backend.replay import ReplayEventKind, ReplayLifecycleEvent

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=94.0)
    risk = _risk(context, 94)
    at = risk.session.observed_at + timedelta(seconds=1)
    partial_risk = replace(
        risk, signed_quantity=4, session=replace(risk.session, observed_at=at)
    )
    partial = ReplayLifecycleEvent(LifecycleEvent.EXIT_UPDATE, "partial", at, 4)
    flat_at = at + timedelta(seconds=1)
    flat_risk = replace(
        risk, signed_quantity=0, session=replace(risk.session, observed_at=flat_at)
    )
    result = replay_exit_decisions(
        thesis=_thesis(),
        position_state=_state(),
        management_state=None,
        policy=ExitPolicy(),
        events=(
            ExitReplayEvent(context, risk),
            ExitReplayEvent(
                None,
                partial_risk,
                kind=ReplayEventKind.BROKER_RECONCILIATION,
                lifecycle_events=(partial,),
            ),
            ExitReplayEvent(
                None,
                flat_risk,
                kind=ReplayEventKind.BROKER_RECONCILIATION,
                lifecycle_events=(
                    partial,  # a repeated broker delivery is idempotent
                    ReplayLifecycleEvent(
                        LifecycleEvent.FLAT_OBSERVED, "flat", flat_at, 0
                    ),
                    ReplayLifecycleEvent(
                        LifecycleEvent.FLAT_CONFIRMED,
                        "closed",
                        flat_at,
                        0,
                        ProtectionState.NONE_FLAT,
                    ),
                ),
            ),
        ),
    )
    assert result.evaluations[1].next_position_state.known_quantity == 4
    assert result.position_state.exposure is ExposureState.CLOSED
    assert result.position_state.known_quantity == 0
    assert result.decisions[-1].primary_reason_code == "BROKER_EXTERNAL_CLOSE"


def test_alternative_policy_replay_preserves_hard_risk_and_latched_protection():
    from dataclasses import replace

    import pytest

    from backend.exit_management.models import ManagementState
    from backend.replay import replay_alternative_exit_policy

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=97.0)
    original = ExitPolicy()
    alternative = replace(original, policy_version="predeclared-alternative")
    kwargs = dict(
        thesis=_thesis(),
        position_state=_state(),
        management_state=ManagementState(
            confirmed_stop=98.0, policy_fingerprint="original"
        ),
        original_policy=original,
        events=(ExitReplayEvent(context, _risk(context, 97.0)),),
    )
    result = replay_alternative_exit_policy(**kwargs, policy=alternative)
    assert result.decisions[0].primary_reason_code == "RISK_CATASTROPHIC_STOP"
    assert result.management_state.confirmed_stop == 98.0
    unsafe = replace(
        alternative,
        hard_risk_policy=replace(original.hard_risk_policy, require_protection=False),
    )
    with pytest.raises(ValueError, match="preserve hard risk"):
        replay_alternative_exit_policy(**kwargs, policy=unsafe)
    with pytest.raises(ValueError, match="before normal management"):
        replay_alternative_exit_policy(
            **{
                **kwargs,
                "management_state": ManagementState(eligible_completed_bars=2),
            },
            policy=alternative,
        )


def test_importing_research_storage_does_not_open_live_database(tmp_path):
    import subprocess
    import sys

    script = """
import sys
from pathlib import Path
root = Path(sys.argv[1])
Path.home = classmethod(lambda cls: root)
from backend.journal import TradeJournal
assert not (root / '.kite-agentic-trading').exists()
research = TradeJournal(str(root / 'research' / 'journal.db'))
assert (root / 'research' / 'journal.db').is_file()
assert not (root / '.kite-agentic-trading').exists()
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True)


def test_replay_requires_aware_event_and_broker_fact_times():
    from dataclasses import replace

    import pytest

    from backend.replay import ReplayLifecycleEvent

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    risk = _risk(context)
    with pytest.raises(ValueError, match="aware risk clock"):
        ExitReplayEvent(
            context,
            replace(
                risk,
                session=replace(risk.session, observed_at=datetime(2026, 9, 21, 4, 25)),
            ),
        )
    with pytest.raises(ValueError, match="aware time"):
        ReplayLifecycleEvent("STATE_OBSERVED", "fact", datetime(2026, 9, 21, 4, 25))


def test_paper_quotes_cannot_overwrite_newer_marks_or_refresh_stale_data():
    from datetime import timedelta

    import pytest

    from backend.backtesting.paper_broker import PaperBroker

    paper = PaperBroker(data_source_id="recorded-market-feed")
    at = datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc)
    paper.mark("RELIANCE", 100.0, at)
    with pytest.raises(ValueError, match="chronological"):
        paper.mark("RELIANCE", 1.0, at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="aware observation"):
        paper.mark("RELIANCE", 1.0, at.replace(tzinfo=None))
    assert paper._prices["RELIANCE"] == 100.0
    assert paper._mark_times["RELIANCE"] == at


def test_paper_ingestion_requires_completed_causal_bars():
    from datetime import timedelta

    import pytest

    from backend.backtesting.paper_broker import PaperBroker

    paper = PaperBroker(data_source_id="recorded-market-feed")
    at = datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc)
    candle = dict(date=at, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000)
    with pytest.raises(ValueError, match="availability"):
        paper.ingest_candle("RELIANCE", candle)
    with pytest.raises(ValueError, match="completed"):
        paper.ingest_candle("RELIANCE", {**candle, "available_at": at})
    available_at = at + timedelta(minutes=5)
    paper.ingest_candle(
        "RELIANCE", {**candle, "available_at": available_at + timedelta(minutes=20)}
    )
    assert paper._mark_times["RELIANCE"] == available_at


def test_replayed_protection_ack_remains_the_hard_stop_floor():
    import pytest

    from backend.exit_management.models import ManagementState
    from backend.replay import ReplayEventKind, ReplayLifecycleEvent

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close=97.0)
    risk = _risk(context, 97)
    kwargs = dict(
        thesis=_thesis(),
        position_state=_state(),
        management_state=ManagementState(confirmed_stop=95.0, requested_stop=98.0),
        policy=ExitPolicy(),
    )
    fact = ReplayLifecycleEvent(
        "STATE_OBSERVED",
        "confirmed-tightening",
        risk.session.observed_at,
        protection="ACTIVE",
        confirmed_stop=98.0,
    )
    result = replay_exit_decisions(
        **kwargs,
        events=(
            ExitReplayEvent(
                None,
                risk,
                kind=ReplayEventKind.BROKER_RECONCILIATION,
                lifecycle_events=(fact,),
            ),
        ),
    )
    assert result.management_state.confirmed_stop == 98.0
    assert result.management_state.requested_stop is None
    assert result.decisions[0].primary_reason_code == "RISK_CATASTROPHIC_STOP"
    with pytest.raises(ValueError, match="loosen"):
        replay_exit_decisions(
            **kwargs,
            events=(
                ExitReplayEvent(
                    None,
                    risk,
                    kind=ReplayEventKind.BROKER_RECONCILIATION,
                    lifecycle_events=(
                        ReplayLifecycleEvent(
                            "STATE_OBSERVED",
                            "looser",
                            risk.session.observed_at,
                            protection="ACTIVE",
                            confirmed_stop=94.0,
                        ),
                    ),
                ),
            ),
        )
