import numpy as np
import pandas as pd
import pytest

from backend.config import config_manager
from backend.kite_client import kite_client
from backend.scanner import Scanner, scanner
from backend.tests.conftest import build_candles


@pytest.fixture
def mock_scanner_config(monkeypatch):
    monkeypatch.setattr(scanner, "last_scanned_candle", {})
    monkeypatch.setattr(
        config_manager,
        "get_families_config",
        lambda: {
            "trend": {"enabled": True, "weight": 1.0},
            "mean_reversion": {"enabled": True, "weight": 1.0},
            "breakout": {"enabled": True, "weight": 1.0},
        },
    )
    # Make sure strategies are enabled
    strat_cfg = {k: {"enabled": True} for k in scanner.strategies.keys()}
    monkeypatch.setattr(config_manager, "get_strategy_config", lambda: strat_cfg)
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 12345}],
    )


def test_scanner_aggregation_trending(mock_scanner_config, monkeypatch, uptrend):
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (uptrend, False))
    _set_decision_clock(monkeypatch, uptrend)

    # Force EMA Crossover (Trend family) to return a BUY signal
    def mock_calc_signals(self, df, symbol):
        return [
            {
                "strategy": "EMA Crossover",
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 120.0,
                "stopLoss": 110.0,
                "target": 140.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["ema_crossover"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["ema_crossover"]),
    )

    signals = scanner.scan_watchlist(["TEST"])
    # Should yield family_trend signals
    assert len(signals) > 0
    assert any(sig["strategy"] == "Trend Pullback" for sig in signals)

    # Check that regime was correctly identified
    assert any(sig["regime"] == "TRENDING" for sig in signals)


def test_scanner_aggregation_uncertain(mock_scanner_config, monkeypatch):
    # Small number of candles
    closes = np.linspace(100, 110, 30)
    df = build_candles(closes)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))
    _set_decision_clock(monkeypatch, df)

    signals = scanner.scan_watchlist(["TEST"])
    # Regime classifier needs 50 candles, so it returns UNCERTAIN
    # Aggregation skips if regime is UNCERTAIN
    assert len(signals) == 0


def test_scanner_aggregation_breakout(mock_scanner_config, monkeypatch):
    # Normal volatility then sudden expansion
    closes = list(np.linspace(100, 102, 45))  # tight range
    closes += [105, 110, 118, 130, 150]  # sudden breakout
    df = build_candles(closes)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))
    _set_decision_clock(monkeypatch, df)

    # Force Bollinger Breakout (Breakout family) to return a BUY signal
    def mock_calc_signals(self, df, symbol):
        return [
            {
                "strategy": "Bollinger Breakout",
                "direction": "BUY",
                "signal_score": 90,
                "entryPrice": 145.0,
                "stopLoss": 135.0,
                "target": 160.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["bollinger_breakout"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["bollinger_breakout"]),
    )

    signals = scanner.scan_watchlist(["TEST"])
    assert any(sig["regime"] == "BREAKOUT" for sig in signals)
    # Breakout regime allows breakout and trend families
    assert any(sig["strategy"] in ("Breakout", "Trend Pullback") for sig in signals)


def test_scanner_rejects_out_of_session_candles(mock_scanner_config, monkeypatch):
    import pandas as pd

    from backend.tests.conftest import build_candles

    # Saturday candles must not become a valid signal source merely because the
    # frame has plausible OHLCV values.
    dates = pd.date_range(
        "2026-09-19 09:15:00", periods=55, freq="5min", tz="Asia/Kolkata"
    )
    df = build_candles(np.linspace(100, 140, 55), dates=dates)

    # Force _fetch_candles to return this df
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))

    called_df_length = []

    def mock_calc_signals(self, df_in, symbol):
        called_df_length.append(len(df_in))
        return [
            {
                "strategy": "EMA Crossover",
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 120.0,
                "stopLoss": 110.0,
                "target": 140.0,
                "reasoning": "mock",
                "family": "trend",
                "timestamp": "2026-09-06",
                "indicators": {},
            }
        ]

    monkeypatch.setattr(
        scanner.strategies["ema_crossover"],
        "calculate_signals",
        mock_calc_signals.__get__(scanner.strategies["ema_crossover"]),
    )

    signals = scanner.scan_watchlist(["TEST"])

    assert signals == []
    assert called_df_length == []


def _set_decision_clock(monkeypatch, frame, *, delay_seconds=0):
    decision = (
        frame["date"].iloc[-1]
        + pd.Timedelta(minutes=5)
        + pd.Timedelta(seconds=delay_seconds)
    ).to_pydatetime()
    monkeypatch.setattr("backend.scanner.now_utc", lambda: decision)
    return decision


@pytest.mark.parametrize("defect", ["stale", "gap", "invalid", "warmup", "volume"])
def test_unusable_position_assessment_does_not_manufacture_votes(
    monkeypatch, uptrend, defect
):
    local = Scanner()
    frame = uptrend.copy()
    if defect == "gap":
        frame = frame.drop(index=20)
    elif defect == "invalid":
        frame.loc[20, "close"] = -1
    elif defect == "warmup":
        frame = frame.iloc[-10:]
    elif defect == "volume":
        frame = frame.drop(columns="volume")
    _set_decision_clock(
        monkeypatch, frame, delay_seconds=601 if defect == "stale" else 0
    )
    monkeypatch.setattr(local, "_fetch_candles", lambda *args: (frame, False))
    called = []
    monkeypatch.setattr(
        config_manager,
        "get_strategy_config",
        lambda: {"ema_crossover": {"enabled": True}},
    )
    monkeypatch.setattr(
        local.strategies["ema_crossover"],
        "calculate_signals",
        lambda *args: called.append(True) or [],
    )

    result = local.evaluate_position("TEST", 12345)

    assert result["assessment_available"] is False
    assert result["buy_signals"] is None
    assert result["sell_signals"] is None
    assert called == []


def test_stale_entry_data_does_not_emit_or_consume_a_decision(
    mock_scanner_config, monkeypatch, uptrend
):
    _set_decision_clock(monkeypatch, uptrend, delay_seconds=601)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda *args: (uptrend, False))

    assert scanner.scan_watchlist(["TEST"]) == []
    assert "TEST" not in scanner.last_scanned_candle


def test_strategy_failure_does_not_mean_no_support(monkeypatch, uptrend):
    local = Scanner()
    _set_decision_clock(monkeypatch, uptrend)
    monkeypatch.setattr(local, "_fetch_candles", lambda *args: (uptrend, False))
    monkeypatch.setattr(
        config_manager,
        "get_strategy_config",
        lambda: {"ema_crossover": {"enabled": True}},
    )

    def unavailable(*args):
        raise ValueError("indicator unavailable")

    monkeypatch.setattr(
        local.strategies["ema_crossover"], "calculate_signals", unavailable
    )

    result = local.evaluate_position("TEST", 12345)

    assert result["assessment_available"] is False
    assert result["buy_signals"] is None
    assert result["sell_signals"] is None
    assert result["strategy_errors"] == ["ema_crossover"]


def test_strategy_frames_are_isolated(monkeypatch, uptrend):
    local = Scanner()
    _set_decision_clock(monkeypatch, uptrend)
    monkeypatch.setattr(local, "_fetch_candles", lambda *args: (uptrend, False))
    monkeypatch.setattr(
        config_manager,
        "get_strategy_config",
        lambda: {
            "ema_crossover": {"enabled": True},
            "rsi_reversal": {"enabled": True},
        },
    )
    observed = []

    def mutate(frame, symbol):
        frame.loc[:, "close"] = -1.0
        return []

    def observe(frame, symbol):
        observed.append(float(frame["close"].iloc[-1]))
        return []

    monkeypatch.setattr(local.strategies["ema_crossover"], "calculate_signals", mutate)
    monkeypatch.setattr(local.strategies["rsi_reversal"], "calculate_signals", observe)

    assert local.evaluate_position("TEST", 12345)["assessment_available"] is True
    assert observed == [140.0]
    assert uptrend["close"].iloc[-1] == 140.0


def test_position_management_ignores_entry_incomplete_candle_switch(monkeypatch):
    local = Scanner()
    frame = build_candles([*np.linspace(100, 140, 60), 70.0])
    decision = (frame["date"].iloc[-1] + pd.Timedelta(seconds=1)).to_pydatetime()
    monkeypatch.setattr("backend.scanner.now_utc", lambda: decision)
    monkeypatch.setattr(local, "_fetch_candles", lambda *args: (frame, False))
    monkeypatch.setattr(
        config_manager,
        "get_strategy_config",
        lambda: {
            "ema_crossover": {"enabled": True},
            "evaluateOnIncompleteCandle": True,
        },
    )
    seen = []

    def calculate(candles, symbol):
        seen.append((len(candles), float(candles["close"].iloc[-1])))
        return []

    monkeypatch.setattr(
        local.strategies["ema_crossover"], "calculate_signals", calculate
    )

    assert local.evaluate_position("TEST", 12345)["assessment_available"] is True
    assert seen == [(60, 140.0)]

    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 12345}],
    )
    local.scan_watchlist(["TEST"])
    # This phase deliberately preserves the opt-in legacy entry experiment.
    assert seen[-1] == (61, 70.0)


def test_entry_revision_cannot_reconsume_completed_bar(
    mock_scanner_config, monkeypatch, uptrend
):
    _set_decision_clock(monkeypatch, uptrend)
    frame = uptrend.copy()
    monkeypatch.setattr(scanner, "_fetch_candles", lambda *args: (frame, False))
    calls = []
    monkeypatch.setattr(
        scanner.strategies["ema_crossover"],
        "calculate_signals",
        lambda *args: calls.append(True) or [],
    )

    scanner.scan_watchlist(["TEST"])
    frame.loc[frame.index[-1], "close"] -= 0.01
    frame["revision"] = "corrected"
    scanner.scan_watchlist(["TEST"])
    frame = frame.iloc[:-1]
    scanner.scan_watchlist(["TEST"])

    assert calls == [True]


def test_concurrent_entry_scans_consume_bar_once(
    mock_scanner_config, monkeypatch, uptrend
):
    import concurrent.futures
    import threading

    _set_decision_clock(monkeypatch, uptrend)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda *args: (uptrend, False))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def calculate(*args):
        calls.append(True)
        started.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(
        scanner.strategies["ema_crossover"], "calculate_signals", calculate
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(scanner.scan_watchlist, ["TEST"])
        assert started.wait(timeout=5)
        second = executor.submit(scanner.scan_watchlist, ["TEST"])
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert calls == [True]


def test_cached_context_preserves_fetch_receipt(monkeypatch, uptrend):
    local = Scanner()
    request = uptrend["date"].iloc[-1] + pd.Timedelta(minutes=5, seconds=1)
    clock = [request.to_pydatetime()]
    monkeypatch.setattr("backend.scanner.now_utc", lambda: clock[0])
    calls = []

    def fetch(*args):
        calls.append(True)
        clock[0] += pd.Timedelta(seconds=2)
        return uptrend.to_dict("records")

    monkeypatch.setattr(kite_client, "get_historical_data", fetch)
    first = local.get_market_context(12345, "TEST")
    clock[0] += pd.Timedelta(seconds=20)
    cached = local.get_market_context(12345, "TEST")

    assert len(calls) == 1
    assert first.received_at == cached.received_at
    assert cached.decision_event_time > first.decision_event_time
    assert cached.primary_bar.available_at == first.primary_bar.available_at


def test_response_crossing_candle_close_cannot_finalize_partial_payload(
    monkeypatch, uptrend
):
    local = Scanner()
    close_time = uptrend["date"].iloc[-1] + pd.Timedelta(minutes=5)
    clock = [(close_time - pd.Timedelta(seconds=1)).to_pydatetime()]
    monkeypatch.setattr("backend.scanner.now_utc", lambda: clock[0])

    def fetch(*args):
        clock[0] += pd.Timedelta(seconds=2)
        return uptrend.to_dict("records")

    monkeypatch.setattr(kite_client, "get_historical_data", fetch)
    context = local.get_market_context(12345, "TEST")

    assert context.primary_bar.end == close_time - pd.Timedelta(minutes=5)
    # Refresh immediately after a close even though the receipt is under 60s old.
    refreshed = local.get_market_context(12345, "TEST")
    assert refreshed.primary_bar.end == close_time


def test_operator_trading_hours_do_not_change_exchange_candle_session(monkeypatch):
    from backend.indicators import SessionVWAP

    local = Scanner()
    frame = build_candles([*([50.0] * 3), *([100.0] * 66), *([150.0] * 6)])
    _set_decision_clock(monkeypatch, frame)
    monkeypatch.setattr(local, "_fetch_candles", lambda *args: (frame, False))
    monkeypatch.setattr(
        config_manager,
        "get_risk_config",
        lambda: {
            "marketOpenTime": "09:30",
            "marketCloseTime": "15:00",
            "noNewTradesAfter": "14:45",
            "squareOffTime": "14:50",
        },
    )

    context = local.get_market_context(12345, "TEST")

    assert context.normal_decision_eligible is True
    assert len(context.primary_bars) == 75
    assert context.higher_bars[0].start == frame["date"].iloc[0]
    assert context.higher_bars[-1].end == frame["date"].iloc[-1] + pd.Timedelta(
        minutes=5
    )
    assert context.session_vwap == pytest.approx(SessionVWAP(frame).vwap().iloc[-1])


def test_refresh_preserves_known_at_for_unchanged_completed_versions(monkeypatch):
    local = Scanner()
    frame = build_candles([100.0, 101.0, 110.0, 104.0, 103.0, 102.0])
    frame.loc[2, "high"] = 115.0
    clock = [_set_decision_clock(monkeypatch, frame)]
    monkeypatch.setattr("backend.scanner.now_utc", lambda: clock[0])
    monkeypatch.setattr(
        kite_client, "get_historical_data", lambda *args: frame.to_dict("records")
    )

    first = local.get_market_context(12345, "TEST")
    clock[0] += pd.Timedelta(seconds=61)
    refreshed = local.get_market_context(12345, "TEST")

    assert first.known_structure
    assert refreshed.received_at > first.received_at
    assert refreshed.input_bar_ids == first.input_bar_ids
    assert refreshed.known_structure == first.known_structure
    assert [bar.available_at for bar in refreshed.primary_bars] == [
        bar.available_at for bar in first.primary_bars
    ]

    # A corrected confirmation bar changes the structure's knowable time even
    # though the pivot candle itself is unchanged.
    frame.loc[4, "close"] -= 0.01
    clock[0] += pd.Timedelta(seconds=61)
    corrected = local.get_market_context(12345, "TEST")

    assert corrected.primary_bars[4].bar_id != first.primary_bars[4].bar_id
    assert corrected.primary_bars[4].available_at == corrected.received_at
    assert corrected.primary_bars[0].available_at == first.primary_bars[0].available_at
    assert corrected.known_structure[0].known_at == corrected.received_at


def test_unchanged_partial_candle_gets_final_receipt_after_close(monkeypatch):
    local = Scanner()
    frame = build_candles(np.linspace(100, 110, 6))
    close_time = frame["date"].iloc[-1] + pd.Timedelta(minutes=5)
    clock = [(close_time - pd.Timedelta(seconds=1)).to_pydatetime()]
    monkeypatch.setattr("backend.scanner.now_utc", lambda: clock[0])
    monkeypatch.setattr(
        kite_client, "get_historical_data", lambda *args: frame.to_dict("records")
    )

    first = local.get_market_context(12345, "TEST")
    clock[0] += pd.Timedelta(seconds=2)
    final = local.get_market_context(12345, "TEST")

    assert len(first.primary_bars) == 5
    assert len(final.primary_bars) == 6
    assert final.primary_bar.available_at == final.received_at
    assert final.primary_bars[0].available_at == first.primary_bars[0].available_at


def test_broker_explicit_bar_receipts_are_preserved(monkeypatch):
    local = Scanner()
    frame = build_candles(np.linspace(100, 110, 6))
    frame["received_at"] = frame["date"] + pd.Timedelta(minutes=5)
    _set_decision_clock(monkeypatch, frame, delay_seconds=1)
    monkeypatch.setattr(
        kite_client, "get_historical_data", lambda *args: frame.to_dict("records")
    )

    context = local.get_market_context(12345, "TEST")

    assert [bar.available_at for bar in context.primary_bars] == list(
        frame["received_at"]
    )
