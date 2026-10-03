"""Slow external reads cannot consume the capacity for retained local views."""

import importlib
import json
import threading

import pytest

main = importlib.import_module("backend.main")


@pytest.mark.parametrize(
    ("blocked_method", "workers"),
    [
        ("get_positions", main._BROKER_WORKERS),
        ("get_historical", main._RESEARCH_WORKERS),
    ],
)
def test_local_views_and_controls_survive_external_pool_saturation(
    monkeypatch, blocked_method, workers
):
    release = threading.Event()
    started = [threading.Event() for _ in range(workers)]
    done = {i: threading.Event() for i in range(workers)}
    responses = {}
    local_methods = [
        "journal_get_trades",
        "journal_get_events",
        "get_settings",
        "analytics_strategy_expectancy",
        "analytics_confluence_validation",
        "analytics_signal_score_calibration",
        "analytics_exit_reason_effectiveness",
        "analytics_exit_quality_report",
        "analytics_exit_quality_trade",
        "analytics_exit_management_replay",
        "analytics_active_position_explanations",
    ]

    def handle(req):
        if req["method"] == blocked_method:
            started[req["id"]].set()
            assert release.wait(5)
        return {"id": req["id"], "result": {"method": req["method"]}}

    def write(response):
        responses[response["id"]] = response
        done[response["id"]].set()

    def requests():
        try:
            for i in range(workers):
                yield json.dumps({"id": i, "method": blocked_method})
                assert started[i].wait(1)
            # Preserve the external pool's bounded admission under real load.
            done[99] = threading.Event()
            yield json.dumps({"id": 99, "method": blocked_method})
            assert done[99].wait(1)
            assert responses[99]["error"]["code"] == -32005
            for i, method in enumerate(
                [*local_methods, "agent_emergency_flatten", "stop_agent"], start=100
            ):
                done[i] = threading.Event()
                yield json.dumps({"id": i, "method": method})
                assert done[i].wait(1), method
                assert responses[i]["result"]["method"] == method
            assert not any(done[i].is_set() for i in range(workers))
        finally:
            release.set()
        assert all(done[i].wait(1) for i in range(workers))

    monkeypatch.setattr(main, "_handle_request", handle)
    monkeypatch.setattr(main, "_write_response", write)
    monkeypatch.setattr(main.sys, "stdin", requests())
    main.main()


def test_local_queue_is_bounded_and_does_not_block_positions_or_controls(monkeypatch):
    count = main._LOCAL_WORKERS + main._LOCAL_QUEUE_SIZE
    release = threading.Event()
    started = [threading.Event() for _ in range(main._LOCAL_WORKERS)]
    done = {i: threading.Event() for i in range(count + 3)}
    responses = {}

    def handle(req):
        if req["method"] == "journal_get_trades":
            if req["id"] < main._LOCAL_WORKERS:
                started[req["id"]].set()
            assert release.wait(5)
        return {"id": req["id"], "result": []}

    def write(response):
        responses[response["id"]] = response
        done[response["id"]].set()

    def requests():
        try:
            for i in range(count):
                yield json.dumps({"id": i, "method": "journal_get_trades"})
                if i < main._LOCAL_WORKERS:
                    assert started[i].wait(1)
            yield json.dumps({"id": count, "method": "journal_get_trades"})
            assert done[count].wait(1)
            assert responses[count]["error"]["code"] == -32005
            assert "local worker" in responses[count]["error"]["message"]
            yield json.dumps({"id": count + 1, "method": "get_positions"})
            yield json.dumps({"id": count + 2, "method": "agent_emergency_flatten"})
            assert done[count + 1].wait(1) and done[count + 2].wait(1)
            assert responses[count + 1]["result"] == []
            assert responses[count + 2]["result"] == []
        finally:
            release.set()
        assert all(done[i].wait(1) for i in range(count))
        assert all(responses[i]["result"] == [] for i in range(count))

    monkeypatch.setattr(main, "_handle_request", handle)
    monkeypatch.setattr(main, "_write_response", write)
    monkeypatch.setattr(main.sys, "stdin", requests())
    main.main()
