"""Operational plumbing fixtures are always explicitly synthetic evidence."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from backend.backtesting.operational_capture import (
    LiveOperationalObserver,
    OperationalRecorder,
    live_observer_from_environment,
)
from backend.backtesting.operational_evidence import assess_operational_run
from backend.backtesting.paper_session import RealtimePaperSession
from backend.exit_management.engine import ExitPolicy
from backend.replay import serialize_replay_artifact
from backend.tests.test_candidate_execution import START
from backend.tests.test_operational_evidence import LIMITS, _contract_fixture


class Clock:
    def __init__(self):
        self.at = START

    def __call__(self):
        return self.at


def plan(mode="ISOLATED_PAPER"):
    return {
        "study_id": "fixture-only",
        "mode": mode,
        "data_source_id": "fixture",
        "source_revision": "fixture-source",
        "policy": serialize_replay_artifact(ExitPolicy()),
    }


def test_round_trip_includes_all_receipts_and_cannot_promote_fixture(tmp_path):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    fixture = _contract_fixture()
    for record in fixture["recorded_decisions"]:
        at = START.fromisoformat(record["occurred_at"])
        clock.at = at + timedelta(seconds=1)
        recorder.decision(record, received_at=at)
    clock.at = START + timedelta(minutes=12)
    recorder.facts(
        positions=fixture["positions"],
        intents=fixture["intents"],
        fills=fixture["fills"],
    )
    report = recorder.finish()
    restored = OperationalRecorder(tmp_path / "capture").export()
    assert report == restored
    assert report["provenance"]["capture_complete"]
    assert report["intents"] == fixture["intents"]
    assert [
        {key: value for key, value in row.items() if key != "observed_at"}
        for row in report["intent_observations"]
    ] == fixture["intents"]
    assert report["provenance"]["source_classification"] == "SYNTHETIC_FIXTURE"
    result = assess_operational_run(report, LIMITS)
    assert result["failures"] == ["MISSING_REAL_TIME_MARKET_PROVENANCE"]
    assert result["observations"]["replay_verified_count"] == 2
    with pytest.raises(ValueError, match="already ended"):
        recorder.facts()


def test_modified_and_truncated_append_record_fails_reopen(tmp_path):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    recorder.facts()
    path = tmp_path / "capture" / "events.jsonl"
    text = path.read_text().replace('"FACTS"', '"OTHER"')
    path.write_text(text)
    with pytest.raises(ValueError, match="corrupt"):
        OperationalRecorder(tmp_path / "capture")


def test_abrupt_stop_and_failed_capture_stay_ineligible_after_export(tmp_path):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    clock.at += timedelta(seconds=1)
    assert not recorder.export()["provenance"]["capture_complete"]
    recorder.failed = True  # E.g. a full background queue.
    recorder.finish()
    restored = OperationalRecorder(tmp_path / "capture").export()
    assert not restored["provenance"]["capture_complete"]
    assert (
        "CAPTURE_INCOMPLETE_OR_FAILED"
        in assess_operational_run(restored, LIMITS)["failures"]
    )


def test_policy_change_and_historical_relabeling_are_recorded_failures(tmp_path):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    record = _contract_fixture()["recorded_decisions"][0]
    clock.at = START + timedelta(days=10)
    recorder.decision(record, received_at=clock.at)
    assert recorder.failed
    assert not recorder.export()["recorded_decisions"]
    record = deepcopy(record)
    record["trace"]["input_snapshot"]["policy"]["policy"]["policy_version"] = "other"
    recorder.decision(record, received_at=clock.at)
    assert [
        event["payload"]["reason"]
        for event in recorder.events
        if event["kind"] == "FAILURE"
    ] == ["DECISION_IS_NOT_CONTEMPORANEOUS", "DECISION_POLICY_CHANGED"]


def test_no_default_live_capture_or_live_storage_destination(tmp_path, monkeypatch):
    monkeypatch.delenv("KITE_EXIT_CAPTURE_PLAN", raising=False)
    assert live_observer_from_environment(None) is None
    from pathlib import Path

    with pytest.raises(ValueError, match="isolated"):
        OperationalRecorder(
            Path.home() / ".kite-agentic-trading" / "capture", plan=plan()
        )


def test_live_observer_failure_does_not_raise_into_risk_thread(tmp_path):
    class BrokenJournal:
        def get_operational_position_facts(self, key):
            raise RuntimeError("fixture failure")

    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan("LIVE_SHADOW"), fixture_clock=clock
    )
    observer = LiveOperationalObserver(recorder, BrokenJournal())
    observer.position("LIVE:fixture")
    result = observer.close(materialize=True)
    assert not result["provenance"]["capture_complete"]


def test_capture_and_reopen_do_not_retain_or_read_the_entire_event_log(
    tmp_path, monkeypatch
):
    import tracemalloc
    from pathlib import Path

    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    tracemalloc.start()
    try:
        for index in range(128):
            recorder.append("OBSERVED_SNAPSHOT", {"index": index, "raw": "x" * 65536})
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (tmp_path / "capture" / "events.jsonl").stat().st_size > 8_000_000
    assert retained < 2_000_000
    assert peak < 4_000_000
    assert len(recorder.events) == 128
    assert recorder.events[-1]["payload"]["index"] == 127

    original_read = Path.read_text

    def no_whole_log_read(path, *args, **kwargs):
        assert path.name != "events.jsonl"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_whole_log_read)
    tracemalloc.start()
    try:
        reopened = OperationalRecorder(tmp_path / "capture")
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert retained < 2_000_000
    assert peak < 4_000_000
    assert len(reopened.events) == 128
    assert reopened.events[-1] == recorder.events[-1]
    assert reopened._head == recorder._head


def test_decision_duplicate_checks_use_identity_index_without_scanning_log(
    tmp_path, monkeypatch
):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    records = _contract_fixture()["recorded_decisions"]
    for record in records:
        clock.at = START.fromisoformat(record["occurred_at"])
        recorder.decision(record, received_at=clock.at)
    count = len(recorder.events)

    def no_scan(_events):
        raise AssertionError("acquisition must not scan previous events")

    monkeypatch.setattr(type(recorder.events), "__iter__", no_scan)
    with pytest.raises(ValueError, match="duplicate operational decision"):
        recorder.decision(records[-1], received_at=clock.at)
    assert len(recorder.events) == count
    next_record = deepcopy(records[-1])
    next_record["decision_id"] = "new-decision-without-history-scan"
    recorder.decision(next_record, received_at=clock.at)
    assert len(recorder.events) == count + 2
    assert len(recorder.events.decision_ids) == 3


def test_live_shutdown_seals_without_materializing_report(tmp_path, monkeypatch):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan("LIVE_SHADOW"), fixture_clock=Clock()
    )
    observer = LiveOperationalObserver(recorder, None)

    def no_materialization():
        raise AssertionError("live shutdown must not materialize the capture")

    monkeypatch.setattr(recorder, "export", no_materialization)
    assert observer.close() is None
    assert observer.close() is None
    assert recorder._sealed
    assert recorder.events[-1]["kind"] == "END"
    report = OperationalRecorder(tmp_path / "capture").export()
    assert report["provenance"]["capture_complete"]


def test_export_revalidates_file_after_capture_was_opened(tmp_path):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    recorder.facts()
    recorder.finish(materialize=False)
    path = tmp_path / "capture" / "events.jsonl"
    path.write_text(path.read_text().replace('"FACTS"', '"OTHER"'))
    with pytest.raises(ValueError, match="corrupt"):
        recorder.export()


def test_export_rejects_whole_record_truncation_after_opening(tmp_path):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    recorder.facts()
    recorder.finish(materialize=False)
    path = tmp_path / "capture" / "events.jsonl"
    path.write_text(path.read_text().splitlines(keepends=True)[0])
    with pytest.raises(ValueError, match="changed after opening"):
        recorder.export()
    reopened = OperationalRecorder(tmp_path / "capture").export()
    assert not reopened["provenance"]["capture_complete"]


def entry(clock):
    return {
        "type": "entry",
        "quote_observed_at": clock.at.isoformat(),
        "quote_price": 100,
        "quantity": 10,
        "signal": {
            "tradingsymbol": "TEST",
            "direction": "BUY",
            "entryPrice": 100,
            "stopLoss": 95,
            "strategy": "fixture",
        },
    }


def test_realtime_paper_retains_entry_residual_and_rejects_replayed_feed(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    report = session.finish()
    assert report["positions"][0]["final_quantity"] == 10
    assert report["censored_positions"][0]["quantity"] == 10
    assert len(report["fills"]) == 1
    assert len(report["recorded_decisions"]) == 1
    assert all(row["position_key"].startswith("PAPER:") for row in report["fills"])


def test_old_paper_quote_fails_before_any_execution(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    event = entry(clock)
    clock.at += timedelta(days=1)
    with pytest.raises(ValueError, match="not real-time"):
        session.ingest(event)
    assert not session.broker.fills
    assert not session.finish()["provenance"]["capture_complete"]


def test_paper_stop_fill_is_exactly_linked_and_reconciled(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    clock.at = START + timedelta(minutes=5)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": START.isoformat(),
                "open": 100,
                "high": 101,
                "low": 94,
                "close": 94,
                "volume": 10000,
            },
        }
    )
    report = session.finish()
    assert report["positions"][0]["final_quantity"] == 0
    assert not report["censored_positions"]
    assert len(report["fills"]) == 2
    order_ids = {order for row in report["intents"] for order in row["order_ids"]}
    assert all(fill["order_id"] in order_ids for fill in report["fills"])
    json.dumps(report, allow_nan=False)


def test_paper_candidate_deadline_executes_and_export_measures_complete_account(
    tmp_path,
):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    clock.at = START.replace(hour=9, minute=45)
    session.ingest({"type": "clock"})
    candle_start = clock.at
    clock.at += timedelta(minutes=5)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": candle_start.isoformat(),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 10000,
            },
        }
    )
    report = session.finish()
    result = assess_operational_run(report, LIMITS)
    assert result["failures"] == ["MISSING_REAL_TIME_MARKET_PROVENANCE"]
    assert result["observations"]["candidate_execution_decision_count"] == 1
    assert result["observations"]["closed_position_count"] == 1
    assert result["observations"]["linked_fill_count"] == 2


def test_paper_spools_complete_history_and_keeps_exact_pending_intent_links(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    for _ in range(32):
        clock.at += timedelta(seconds=1)
        session.ingest({"type": "clock"})
    clock.at = START.replace(hour=9, minute=45)
    for _ in range(16):
        session.ingest({"type": "clock"})
        clock.at += timedelta(seconds=1)
    assert session.decision_count == 48
    for history in (
        session.runner.recorded_decisions,
        session.runner.execution_results,
        session.runner.evaluations,
        session.runner.equity_curve,
    ):
        assert len(history) == 1
    assert len(session.broker.pending_orders) == 1
    clock.at = START.replace(hour=9, minute=55)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": (clock.at - timedelta(minutes=5)).isoformat(),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 10000,
            },
        }
    )
    report = session.finish()
    assert len(report["recorded_decisions"]) == session.decision_count == 48
    linked = [row for row in report["intents"] if row["origin"] == "CANDIDATE"]
    assert len(linked) == 1
    assert len(linked[0]["decision_ids"]) == 16
    # The first working market attempt hits the shared 15-second deadline.
    assert len(linked[0]["order_ids"]) == 2
    assert len(report["fills"]) == 2
    assert not report["censored_positions"]
    events = list(session.recorder.events)
    assert all(
        not intent["decision_ids"]
        for event in events
        if event["kind"] == "FACTS"
        for intent in event["payload"]["intents"]
    )
    assert (
        sum(
            len(event["payload"]["results"])
            for event in events
            if event["kind"] == "PAPER_EXECUTION_RESULTS"
        )
        == 16
    )
    assert sum(len(row["decision_ids"]) for row in report["intent_observations"]) <= 32
    assert all(
        "decisions" not in link
        for links in session._candidate_links.values()
        for link in links.values()
    )
    result = assess_operational_run(report, LIMITS)
    assert result["failures"] == ["MISSING_REAL_TIME_MARKET_PROVENANCE"]
    assert result["observations"]["replay_verified_count"] == 48
    assert result["observations"]["candidate_execution_decision_count"] == 16
    reopened = OperationalRecorder(tmp_path / "paper").export()
    assert reopened == report


def test_abrupt_stop_retains_execution_result_before_following_facts(tmp_path):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    record = _contract_fixture()["recorded_decisions"][-1]
    clock.at = START.fromisoformat(record["occurred_at"])
    recorder.decision(record, received_at=clock.at)
    recorder.append(
        "PAPER_EXECUTION_RESULTS",
        {
            "results": [
                {
                    "decision_id": record["decision_id"],
                    "intent_id": "pending-fixture-intent",
                    "order_id": "pending-fixture-order",
                    "state": "WORKING",
                }
            ]
        },
    )
    report = recorder.export()
    assert not report["provenance"]["capture_complete"]
    assert report["intents"] == [
        {
            "intent_id": "pending-fixture-intent",
            "position_key": record["trace"]["input_snapshot"]["state"]["position_key"],
            "origin": "CANDIDATE",
            "decision_ids": [record["decision_id"]],
            "order_ids": ["pending-fixture-order"],
            "status": "WORKING",
        }
    ]
    assert report["provenance"]["source_intent_count"] == 1
    assert {
        key: value
        for key, value in report["intent_observations"][-1].items()
        if key != "observed_at"
    } == report["intents"][0]


def test_execution_result_cannot_reference_an_unobserved_decision(tmp_path):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    recorder.append(
        "PAPER_EXECUTION_RESULTS",
        {"results": [{"decision_id": "unseen", "intent_id": "unjoined"}]},
    )
    with pytest.raises(ValueError, match="no preceding decision"):
        recorder.export()


def test_live_commit_hook_runs_only_after_durable_decision_and_suppresses_dispatch(
    monkeypatch,
):
    from backend.journal import journal
    from backend.tests.test_exit_shadow_state import _evaluate, _live

    engine, thesis, current = _live(monkeypatch)
    seen = []

    class Observer:
        def received(self):
            return current["context"].primary_bar.end

        def decision(self, record, *, received_at):
            durable = journal.get_latest_exit_decision(thesis.position_key)
            assert durable["payload"]["decision_id"] == record["decision_id"]
            assert received_at == current["context"].primary_bar.end
            seen.append(record)

    engine._operational_observer = Observer()
    _evaluate(engine, current)
    assert len(seen) == 1
    assert seen[0]["trace"]["orchestration"]["dispatch"] == "SUPPRESSED_PHASE7"


def test_dev_capture_is_never_real_market_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_DEV_MODE", "1")
    recorder = OperationalRecorder(tmp_path / "capture", plan=plan("LIVE_SHADOW"))
    assert recorder.identity["source_classification"] == "SYNTHETIC_FIXTURE"


def test_existing_capture_cannot_be_resumed_as_a_second_writer(tmp_path):
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=Clock()
    )
    recorder.facts()
    reader = OperationalRecorder(tmp_path / "capture")
    with pytest.raises(ValueError, match="read-only"):
        reader.facts()
    assert not reader.export()["provenance"]["capture_complete"]


def test_live_journal_export_uses_exact_epoch_including_all_order_attempts(monkeypatch):
    from backend.journal import journal
    from backend.tests.test_exit_live_integration import _managed_engine

    _, thesis = _managed_engine()
    snapshot = journal.get_operational_position_facts(thesis.position_key)
    assert snapshot["state"]["position_key"] == thesis.position_key
    assert snapshot["direction"] == "BUY"
    assert (
        journal.get_operational_position_facts(thesis.position_key + ":another-epoch")
        is None
    )


def test_capture_failure_cannot_interrupt_the_live_checkpoint_path(monkeypatch):
    from backend.tests.test_exit_live_integration import _managed_engine

    engine, thesis = _managed_engine()

    class BrokenObserver:
        recorder = type("State", (), {"failed": False})()

        def position(self, key):
            raise OSError("fixture recorder unavailable")

    engine._operational_observer = BrokenObserver()
    engine._capture_operational_position(thesis.position_key)
    assert engine._operational_observer.recorder.failed


def test_realistic_positive_quote_lag_does_not_fail_after_entry(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    message = entry(clock)
    clock.at += timedelta(milliseconds=25)
    session.ingest(message)
    assert len(session.broker.fills) == 1
    assert session.broker._mark_times["TEST"] == clock.at
    assert any(e["kind"] == "ENTRY_REFERENCE_QUOTE" for e in session.recorder.events)
    session.finish()


def test_partial_entry_bar_never_imports_preentry_extremes(tmp_path):
    clock = Clock()
    clock.at += timedelta(minutes=2)
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    clock.at = START + timedelta(minutes=5)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": START.isoformat(),
                "open": 100,
                "high": 300,
                "low": 1,
                "close": 100,
                "volume": 1000,
            },
        }
    )
    position = session.broker.positions["TEST"]
    assert position["mfe"] < 101 and position["mae"] > 99
    assert position["excursion_quality"] == "PARTIAL_BAR_BOUNDS"
    assert len(session.broker.fills) == 1
    session.finish()


def test_invalid_gap_quote_rejects_before_entry_mutation(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    message = entry(clock)
    message["quote_price"] = 90
    with pytest.raises(ValueError, match="protective stop"):
        session.ingest(message)
    assert not session.broker.fills
    assert not session.finish()["provenance"]["capture_complete"]


def test_malformed_stream_message_cannot_be_omitted_from_capture_failure(tmp_path):
    session = RealtimePaperSession(
        tmp_path / "paper", plan=plan(), fixture_clock=Clock()
    )
    with pytest.raises(KeyError):
        session.ingest({})
    assert not session.finish()["provenance"]["capture_complete"]


def test_current_projection_does_not_erase_unresolved_observations(tmp_path):
    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan(), fixture_clock=clock
    )
    fixture = _contract_fixture()
    first = deepcopy(fixture["positions"][0])
    first.update(final_quantity=10, reconciliation_complete=False)
    recorder.facts(positions=[first])
    clock.at += timedelta(seconds=1)
    final = deepcopy(first)
    final.update(final_quantity=0, reconciliation_complete=True)
    recorder.facts(positions=[final])
    report = recorder.finish()
    assert report["positions"][0]["final_quantity"] == 0
    assert [r["final_quantity"] for r in report["position_observations"]] == [10, 0]


def test_unfilled_live_entry_obligation_survives_export(tmp_path):
    class Journal:
        def get_operational_position_facts(self, key):
            return {
                "state": {},
                "direction": "BUY",
                "fills": [],
                "intents": [
                    {
                        "intent_id": "pending",
                        "intent_type": "ENTER",
                        "order_ids": ["order"],
                        "state": "UNKNOWN",
                    }
                ],
            }

    clock = Clock()
    recorder = OperationalRecorder(
        tmp_path / "capture", plan=plan("LIVE_SHADOW"), fixture_clock=clock
    )
    recorder.capture_live_position(Journal(), "LIVE:test:NSE:TEST:TEST:MIS:epoch")
    clock.at += timedelta(seconds=1)
    report = recorder.finish()
    assert report["unfilled_entry_obligations"]
    assert (
        "UNRESOLVED_UNFILLED_ENTRY_OBLIGATIONS"
        in assess_operational_run(report, LIMITS)["failures"]
    )


def test_supported_paper_thesis_enables_normal_management(tmp_path):
    from backend.exit_management.models import ThesisHealth
    from backend.tests.exit_management.test_thesis import _signal

    clock = Clock()
    signal = _signal()
    context = signal["market_context"]
    context["decision_event_time"] = START.isoformat()
    context["sourceAsOf"] = START.isoformat()
    context["primaryBar"].update(
        start=(START - timedelta(minutes=5)).isoformat(),
        end=START.isoformat(),
        available_at=START.isoformat(),
    )
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    message = entry(clock)
    message["signal"] = signal
    session.ingest(message)
    assert (
        session.runner.positions["RELIANCE"].state.thesis_health is ThesisHealth.VALID
    )
    session.finish()


def test_busy_paper_input_does_not_starve_independent_clock(tmp_path, monkeypatch):
    import io
    import itertools

    from backend.backtesting import paper_session

    events = []

    class Recorder:
        def received(self):
            return START

        def failure(self, reason):
            raise AssertionError(reason)

    class Session:
        recorder = Recorder()

        def __init__(self, *args, **kwargs):
            pass

        def ingest(self, message, **kwargs):
            events.append(message["type"])

        def finish(self):
            return {}

    path = tmp_path / "plan.json"
    path.write_text("{}")
    monkeypatch.setattr(paper_session, "RealtimePaperSession", Session)
    monkeypatch.setattr(
        paper_session.sys, "stdin", io.StringIO('{"type":"history"}\n' * 3)
    )
    ticks = itertools.count(step=2)
    monkeypatch.setattr(paper_session.time, "monotonic", lambda: next(ticks))
    paper_session.main(
        [
            "--plan",
            str(path),
            "--directory",
            str(tmp_path / "paper"),
            "--output",
            str(tmp_path / "output.json"),
        ]
    )
    assert events.count("history") == 3
    assert events.count("clock") >= 3


def test_quote_only_event_marks_without_inventing_fills(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    clock.at += timedelta(seconds=1)
    session.ingest(
        {
            "type": "quote",
            "symbol": "TEST",
            "price": 101,
            "observed_at": clock.at.isoformat(),
        }
    )
    assert session.broker._prices["TEST"] == 101
    assert len(session.broker.fills) == 1
    session.finish()


def test_real_capture_verifies_running_source_before_creation_and_at_finish(
    tmp_path, monkeypatch
):
    from backend.backtesting import operational_capture

    monkeypatch.setattr(operational_capture, "is_dev_mode", lambda: False)
    monkeypatch.setattr(
        operational_capture, "_runtime_source_revision", lambda: "a" * 64
    )
    with pytest.raises(ValueError, match="running source tree"):
        OperationalRecorder(tmp_path / "wrong", plan=plan())
    assert not (tmp_path / "wrong").exists()
    declared = plan()
    declared["source_revision"] = "a" * 64
    recorder = OperationalRecorder(tmp_path / "real", plan=declared)
    assert recorder.identity["runtime_source_tree_sha256"] == "a" * 64
    monkeypatch.setattr(
        operational_capture, "_runtime_source_revision", lambda: "b" * 64
    )
    report = recorder.finish()
    assert not report["provenance"]["capture_complete"]
    assert any(
        e["kind"] == "FAILURE"
        and e["payload"]["reason"] == "RUNTIME_SOURCE_CHANGED_DURING_CAPTURE"
        for e in recorder.events
    )


def test_paper_history_receipts_and_regime_memory_survive_new_bars(
    tmp_path, monkeypatch
):
    from backend import market_context

    raw_regime = ["RANGING"]
    monkeypatch.setattr(
        market_context, "_raw_regime", lambda frame: (raw_regime[0], {})
    )
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    history = [
        {
            "date": (START - timedelta(minutes=5 * offset)).isoformat(),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
        }
        for offset in range(7, 0, -1)
    ]
    session.ingest({"type": "history", "symbol": "TEST", "candles": history})
    assert all(row["received_at"] == START for row in session.history["TEST"])
    session.ingest(entry(clock))
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    context = session.runner.recorded_decisions[-1]["trace"]["input_snapshot"][
        "context"
    ]
    assert len(context["primary_bars"]) == 7
    assert context["confirmed_regime"] == "RANGING"
    raw_regime[0] = "TRENDING"
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    assert session.context_service._regime_state["SIM-TEST"].candidate_age == 0
    clock.at = START + timedelta(minutes=5)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": START.isoformat(),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
        }
    )
    context = session.runner.recorded_decisions[-1]["trace"]["input_snapshot"][
        "context"
    ]
    assert len(context["primary_bars"]) == 8
    assert context["primary_quality"]["status"] == "VALID"
    assert context["confirmed_regime"] == "RANGING"
    assert context["transition_age"] == 1
    clock.at += timedelta(seconds=1)
    session.ingest({"type": "clock"})
    assert session.context_service._regime_state["SIM-TEST"].candidate_age == 1
    clock.at = START + timedelta(minutes=10)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": (START + timedelta(minutes=5)).isoformat(),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
        }
    )
    assert (
        session.context_service._regime_state["SIM-TEST"].confirmed_regime == "TRENDING"
    )
    session.finish()


def test_late_completed_bar_does_not_rewind_quote_or_fill_new_exit(tmp_path):
    clock = Clock()
    session = RealtimePaperSession(tmp_path / "paper", plan=plan(), fixture_clock=clock)
    session.ingest(entry(clock))
    bar_start = START
    clock.at = START + timedelta(minutes=5, seconds=5)
    session.ingest(
        {
            "type": "quote",
            "symbol": "TEST",
            "price": 94,
            "observed_at": clock.at.isoformat(),
        }
    )
    assert session.runner.execution_results  # Hard stop at new quote
    assert len(session.broker.fills) == 1
    latest = clock.at
    clock.at += timedelta(seconds=15)
    session.ingest(
        {
            "type": "candle",
            "symbol": "TEST",
            "candle": {
                "date": bar_start.isoformat(),
                "open": 100,
                "high": 101,
                "low": 96,
                "close": 100,
                "volume": 1000,
            },
        }
    )
    assert len(session.broker.fills) == 1
    assert session.broker._prices["TEST"] == 94
    assert session.broker._mark_times["TEST"] == latest
    assert any(e["kind"] == "NONEXECUTABLE_LATE_BAR" for e in session.recorder.events)
    session.finish()
