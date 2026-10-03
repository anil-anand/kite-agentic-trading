"""Adversarial phase-7 state, concurrency and retained-input contracts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from backend.exit_management.engine import ExitPolicy, evaluate_exit
from backend.exit_management.models import ManagementState, PositionState
from backend.journal import journal
from backend.risk_rules import HardRiskPolicy, HardRiskSnapshot
from backend.session_clock import SessionClock, SessionPolicy, SessionSnapshot
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_exit_live_integration import START, _managed_engine, _position
from backend.trading_engine import TradingEngine


def _live(monkeypatch, close=98.0):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    monkeypatch.setattr(module.risk_manager, "kill_switch_active", False)
    monkeypatch.setattr(engine, "_session_clock", lambda: SessionClock(SessionPolicy()))
    current = {"context": _context(START, close=close)}
    monkeypatch.setattr(module, "now_utc", lambda: current["context"].primary_bar.end)
    monkeypatch.setattr(
        module.scanner, "get_market_context", lambda *a, **k: current["context"]
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *a: 0.05)
    return engine, thesis, current


def _evaluate(engine, current, quantity=10):
    context = current["context"]
    engine._evaluate_shadow_position(
        {
            **_position(context.primary_bar.close, context.primary_bar.end),
            "quantity": quantity,
        },
        engine.active_trades["RELIANCE"],
    )


def test_concurrent_same_bar_commits_one_decision(monkeypatch):
    import backend.trading_engine as module

    engine, thesis, current = _live(monkeypatch)
    barrier = threading.Barrier(2)

    def fetch(*args, **kwargs):
        barrier.wait(timeout=2)
        return current["context"]

    monkeypatch.setattr(module.scanner, "get_market_context", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(_evaluate, engine, current) for _ in range(2)]
        for result in results:
            result.result(timeout=3)
    assert len(journal.get_exit_decisions(thesis.position_key)) == 1
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    assert checkpoint["counters"]["exit_policy"]["failure_count"] == 1


def test_partial_fill_during_fetch_discards_old_quantity_without_poisoning_state(
    monkeypatch,
):
    import backend.trading_engine as module

    engine, thesis, current = _live(monkeypatch)

    def fetch(*args, **kwargs):
        state = engine._phase5_state_for(thesis.position_key)
        engine._commit_phase5_checkpoint(
            position_key=thesis.position_key,
            state=replace(
                state,
                known_quantity=6,
                version=state.version + 1,
                last_event_id="external-partial-fill",
            ),
            event_id="external-partial-fill",
            event_type="BROKER_MANAGEMENT_OBSERVED",
            details={"quantity": 6},
        )
        return current["context"]

    monkeypatch.setattr(module.scanner, "get_market_context", fetch)
    _evaluate(engine, current)
    assert journal.get_exit_decisions(thesis.position_key) == []
    monkeypatch.setattr(
        module.scanner, "get_market_context", lambda *a, **k: current["context"]
    )
    _evaluate(engine, current, quantity=6)
    assert (
        journal.get_exit_decisions(thesis.position_key)[0]["payload"]["action"]
        == "HOLD"
    )


def test_shadow_latch_survives_partial_fill_and_pending_stop_update(monkeypatch):
    engine, thesis, current = _live(monkeypatch)
    _evaluate(engine, current)
    current["context"] = _context(START + timedelta(minutes=5), close=98)
    _evaluate(engine, current)
    checkpoint = journal.get_managed_position(thesis.position_key)["state"]
    candidate = checkpoint["counters"]["shadow_candidate_position_state"]
    assert candidate["thesis_health"] == "INVALIDATED"
    state = engine._phase5_state_for(thesis.position_key)
    engine._commit_phase5_checkpoint(
        position_key=thesis.position_key,
        state=replace(
            state,
            known_quantity=6,
            protection="UPDATE_PENDING",
            version=state.version + 1,
            last_event_id="partial-with-stop-update",
        ),
        event_id="partial-with-stop-update",
        event_type="BROKER_MANAGEMENT_OBSERVED",
        details={},
        protection={"requested_stop": 96.0},
    )
    current["context"] = _context(START + timedelta(minutes=10), close=101)
    _evaluate(engine, current, quantity=6)
    result = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    assert result["action"] == "MANAGE_PENDING_INTENT"
    after = result["trace"]["orchestration"]["candidate_state_after"]
    assert after["latched_exit_intent_id"] == candidate["latched_exit_intent_id"]
    assert after["thesis_health"] == "INVALIDATED"
    assert after["known_quantity"] == 6
    assert after["protection"] == "UPDATE_PENDING"
    assert result["state_after"]["exposure"] == "OPEN"


def test_broker_unknown_can_recover_and_process_same_unconsumed_bar(monkeypatch):
    engine, thesis, current = _live(monkeypatch)
    engine._reconciliation_pending = True
    _evaluate(engine, current)
    assert (
        journal.get_exit_decisions(thesis.position_key)[-1]["payload"]["action"]
        == "RECONCILE_REQUIRED"
    )
    engine._reconciliation_pending = False
    _evaluate(engine, current)
    result = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    assert result["action"] == "HOLD"
    assert result["trace"]["management_after"]["failure_count"] == 1

    # Vendor/source clocks can collide or regress; the committed state sequence
    # is the authoritative replay order even with deliberately inverted times.
    rows = journal.get_exit_decisions(thesis.position_key)
    assert rows[0]["payload"]["occurred_at"] == rows[1]["payload"]["occurred_at"]
    journal._get_conn().execute(
        "UPDATE exit_decision_records SET created_at = ? WHERE decision_id = ?",
        ("2099-01-01T00:00:00+00:00", rows[0]["decision_id"]),
    )
    assert [
        row["state_after_version"]
        for row in journal.get_exit_decisions(thesis.position_key)
    ] == [1, 2]


def test_restart_and_outage_preserve_memory_without_retroactive_confirmation(
    monkeypatch,
):
    engine, thesis, current = _live(monkeypatch)
    _evaluate(engine, current)
    restarted = TradingEngine()
    restarted.active_trades = engine.active_trades.copy()
    monkeypatch.setattr(restarted, "_get_tick_size", lambda *a: 0.05)
    _evaluate(restarted, current)
    assert len(journal.get_exit_decisions(thesis.position_key)) == 1
    current["context"] = _context(START + timedelta(minutes=15), close=98)
    _evaluate(restarted, current)
    result = journal.get_exit_decisions(thesis.position_key)[-1]["payload"]
    assert result["action"] == "HOLD"
    assert result["trace"]["management_after"]["failure_count"] == 1
    assert result["trace"]["orchestration"]["missed_primary_intervals"] == 2


@pytest.mark.parametrize(
    "changed", [{"product": "CNC"}, {"account_id": "other"}, {"instrument_token": "9"}]
)
def test_same_symbol_other_identity_is_not_candidate_input(monkeypatch, changed):
    engine, thesis, current = _live(monkeypatch)
    engine._evaluate_shadow_position(
        {**_position(98, current["context"].primary_bar.end), **changed},
        engine.active_trades["RELIANCE"],
    )
    assert journal.get_exit_decisions(thesis.position_key) == []


def test_trace_retains_full_inputs_and_reproduces_pure_decision(monkeypatch):
    from backend.market_context import (
        ContextQuality,
        MarketContext,
        OHLCVBar,
        SourceQuality,
    )
    from backend.time_utils import as_utc

    engine, thesis, current = _live(monkeypatch)
    _evaluate(engine, current)
    saved = journal.get_exit_decisions(thesis.position_key)[0]["payload"]
    inputs = saved["trace"]["input_snapshot"]
    data = inputs["context"]

    def bar(raw):
        return OHLCVBar(
            **{
                key: as_utc(value) if key in {"start", "end", "available_at"} else value
                for key, value in raw.items()
            }
        )

    replay_context = MarketContext(
        **{
            **data,
            **{
                name: as_utc(data[name])
                for name in ("decision_event_time", "received_at", "source_as_of")
            },
            "primary_bar": bar(data["primary_bar"]),
            "higher_bar": None,
            "primary_bars": tuple(bar(item) for item in data["primary_bars"]),
            "higher_bars": (),
            "primary_quality": SourceQuality(
                **{
                    **data["primary_quality"],
                    "status": ContextQuality(data["primary_quality"]["status"]),
                }
            ),
            "higher_quality": SourceQuality(
                **{
                    **data["higher_quality"],
                    "status": ContextQuality(data["higher_quality"]["status"]),
                }
            ),
            "input_bar_ids": tuple(data["input_bar_ids"]),
            "known_structure": (),
        }
    )
    risk_data = inputs["risk"]
    session_data = risk_data["session"]
    # Session datetimes are the only typed values beyond the raw clock flags.
    session = SessionSnapshot(
        **{
            **session_data,
            "observed_at": datetime.fromisoformat(session_data["observed_at"]),
            "exchange_time": datetime.fromisoformat(session_data["exchange_time"]),
            "session_date": date.fromisoformat(session_data["session_date"]),
        }
    )
    policy_data = inputs["policy"]["policy"]
    replay = evaluate_exit(
        thesis,
        PositionState.from_dict(inputs["state"]),
        replay_context,
        HardRiskSnapshot(
            **{
                **risk_data,
                "session": session,
                "mark_time": as_utc(risk_data["mark_time"]),
            }
        ),
        ExitPolicy(
            **{
                **policy_data,
                "hard_risk_policy": HardRiskPolicy(**policy_data["hard_risk_policy"]),
            }
        ),
        management_state=ManagementState.from_dict(inputs["management"]),
    )
    assert replay.decision.decision_id == saved["decision_id"]
    assert replay.decision.trace["input_hash"] == saved["trace"]["input_hash"]
