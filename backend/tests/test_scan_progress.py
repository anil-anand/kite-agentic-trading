"""Worker visibility must explain empty scans without changing entry decisions."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import pytest

import backend.trading_engine as te
from backend.kite_client import kite_client
from backend.scan_progress import ScanProgress
from backend.scanner import Scanner
from backend.tests.test_offline_scanner import offline_scanner as offline_scanner
from backend.tests.test_scan_only_recovery import engine as engine


def test_three_workers_report_current_stocks_and_queue(offline_scanner, monkeypatch):
    scanner, frame, _ = offline_scanner
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda *args: [
            {"tradingsymbol": symbol, "instrument_token": index + 1}
            for index, symbol in enumerate(symbols)
        ],
    )
    entered = threading.Barrier(4)
    release = threading.Event()

    def fetch(token, symbol):
        if symbol != "DDD":
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
        return frame, False

    monkeypatch.setattr(scanner, "_fetch_candles", fetch)
    updates = []
    progress = ScanProgress(on_update=lambda: updates.append(progress.snapshot()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            scanner.scan_watchlist, symbols, analysis_only=True, progress=progress
        )
        try:
            entered.wait(timeout=5)
            state = progress.snapshot()
            assert state["phase"] == "scanning"
            assert state["totalSymbols"] == 4 and state["completedSymbols"] == 0
            assert state["queuedSymbols"] == ["DDD"]
            assert {worker["symbol"] for worker in state["workers"]} == set(symbols[:3])
            assert all(
                worker["stage"] == "fetching_candles" for worker in state["workers"]
            )
            # Slow requests must be visible before they return, even if all
            # three workers started within the notification throttle interval.
            assert updates[-1]["workers"] == state["workers"]
            state["workers"][0]["symbol"] = "MUTATED"
            assert progress.snapshot()["workers"][0]["symbol"] != "MUTATED"
        finally:
            release.set()
        assert len(result.result(timeout=5)) == 4

    finished = progress.snapshot()
    assert finished["phase"] == "completed"
    assert finished["completedSymbols"] == finished["evaluatedSymbols"] == 4
    assert finished["signalsFound"] == 4
    assert finished["queuedSymbols"] == []
    assert all(worker["symbol"] is None for worker in finished["workers"])
    assert len(finished["results"]) == 4
    assert finished["enabledStrategies"] == ["fixture"]


@pytest.mark.parametrize(
    ("defect", "outcome", "counter"),
    [
        ("unknown", "unknown_symbol", "skippedSymbols"),
        ("no_data", "unavailable", "skippedSymbols"),
        ("gap", "unavailable", "skippedSymbols"),
        ("fetch_error", "error", "failedSymbols"),
        ("strategy_error", "error", "failedSymbols"),
        ("no_match", "no_match", "evaluatedSymbols"),
    ],
)
def test_empty_scans_preserve_the_reason(
    offline_scanner, monkeypatch, defect, outcome, counter
):
    scanner, frame, _ = offline_scanner
    if defect == "unknown":
        monkeypatch.setattr(kite_client, "get_instruments", lambda *args: [])
    elif defect == "no_data":
        monkeypatch.setattr(
            scanner, "_fetch_candles", lambda *args: (pd.DataFrame(), False)
        )
    elif defect == "gap":
        frame.drop(index=frame.index[-2], inplace=True)
    elif defect == "fetch_error":

        def fail(*args):
            raise RuntimeError("Synthetic offline failure")

        monkeypatch.setattr(kite_client, "get_historical_data", fail)
        monkeypatch.setattr(
            scanner, "_fetch_candles", Scanner._fetch_candles.__get__(scanner)
        )
    elif defect == "strategy_error":

        def fail(*args, **kwargs):
            raise RuntimeError("Synthetic calculation failure")

        monkeypatch.setattr("backend.scanner.evaluate_production_entries", fail)
    else:
        monkeypatch.setattr(
            "backend.scanner.evaluate_production_entries", lambda **kw: []
        )

    progress = ScanProgress()
    assert scanner.scan_watchlist(["TEST"], analysis_only=True, progress=progress) == []
    state = progress.snapshot()
    assert state["completedSymbols"] == 1 and state[counter] == 1
    assert state["signalsFound"] == 0
    assert state["results"][0]["outcome"] == outcome
    assert state["results"][0]["detail"]
    assert state["phase"] == "completed"
    assert all(worker["symbol"] is None for worker in state["workers"])


def test_weekend_repeat_reports_unchanged_real_candle(offline_scanner):
    scanner, frame, _ = offline_scanner
    first = ScanProgress()
    signals = scanner.scan_watchlist(["TEST"], analysis_only=True, progress=first)
    repeat = ScanProgress()
    assert scanner.scan_watchlist(["TEST"], analysis_only=True, progress=repeat) == []
    result = repeat.snapshot()["results"][0]
    assert result["outcome"] == "unchanged"
    assert pd.Timestamp(result["candleTime"]) == frame["date"].iloc[-1] + pd.Timedelta(
        minutes=5
    )
    assert result["candleTime"] == signals[0]["analysisAsOf"]
    assert scanner.last_scanned_candle == {}
    assert repeat.snapshot()["evaluatedSymbols"] == 0


def test_progress_delivery_failure_cannot_suppress_signals(offline_scanner):
    scanner, _, _ = offline_scanner

    def broken_delivery():
        raise RuntimeError("Synthetic observer failure")

    progress = ScanProgress(on_update=broken_delivery)
    assert (
        len(scanner.scan_watchlist(["TEST"], analysis_only=True, progress=progress))
        == 1
    )
    assert progress.snapshot()["phase"] == "completed"


def test_weekend_engine_state_survives_polling_and_never_admits_orders(
    engine, offline_scanner, monkeypatch
):
    scanner, _, _ = offline_scanner
    monkeypatch.setattr(te, "scanner", scanner)
    monkeypatch.setattr(
        te, "now_utc", lambda: datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    )
    published = []
    monkeypatch.setattr(engine, "_push_signal", published.append)
    monkeypatch.setattr(
        engine, "execute_signal", lambda *args: pytest.fail("Analysis placed an order")
    )
    engine.start("auto")
    engine.dynamic_watchlist = ["TEST"]
    engine.last_universe_refresh_time = te.now_utc()
    engine.scan_and_trade()

    for _ in range(2):
        state = engine.status()
        assert state["status"] == "monitoring"
        assert state["marketSession"] == {
            "isOpen": False,
            "isTradingDay": False,
            "isWeekend": True,
        }
        assert state["entryPaused"] and state["scanOnly"]
        assert state["scanProgress"]["phase"] == "completed"
        assert state["scanProgress"]["signalsPublished"] == 1
        assert state["lastScanTime"] == state["scanProgress"]["completedAt"]
    assert len(published) == 1 and published[0]["analysisOnly"]
    assert engine.stop()["status"] == "supervising"
    engine._push_state_update()
    assert not engine.status()["running"]


def test_failed_instrument_load_is_an_error_not_an_empty_match(
    engine, offline_scanner, monkeypatch
):
    scanner, _, _ = offline_scanner
    monkeypatch.setattr(te, "scanner", scanner)

    def fail(*args):
        raise RuntimeError("Synthetic instrument failure")

    monkeypatch.setattr(kite_client, "get_instruments", fail)
    engine.start("auto")
    engine.dynamic_watchlist = ["TEST"]
    engine.last_universe_refresh_time = te.now_utc()
    with pytest.raises(RuntimeError, match="instrument failure"):
        engine.scan_and_trade()
    state = engine.status()
    assert state["status"] == "error"
    assert state["scanProgress"]["phase"] == "error"
    assert state["scanProgress"]["completedSymbols"] == 0


def test_no_screener_candidates_are_reported_without_loading_instruments(
    engine, monkeypatch
):
    monkeypatch.setattr(
        "backend.nifty_universe.get_nifty100_universe", lambda: ["AAA", "BBB"]
    )
    monkeypatch.setattr(te.config_manager, "get_watchlist", lambda: ["CCC"])
    monkeypatch.setattr(
        "backend.screener.screener_engine.generate_daily_watchlist", lambda **kw: []
    )
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda *args: pytest.fail("Empty scan fetched instruments"),
    )
    updates = []
    monkeypatch.setattr(
        engine, "_push_state_update", lambda **kw: updates.append(engine.status())
    )
    engine.start("auto")
    engine.scan_and_trade()
    progress = engine.status()["scanProgress"]
    assert progress["phase"] == "completed" and progress["totalSymbols"] == 0
    assert progress["universeSize"] == 3
    assert any(
        state["scanProgress"] and state["scanProgress"]["phase"] == "screening"
        for state in updates
    )
