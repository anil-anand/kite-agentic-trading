"""Latest completed charts remain available for non-executable after-hours scans."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.config import config_manager
from backend.kite_client import kite_client
from backend.market_context import ContextQuality
from backend.scanner import Scanner
from backend.tests.conftest import build_candles
from backend.tests.test_entry_decisions import _Playbook


@pytest.fixture
def offline_scanner(monkeypatch):
    scanner = Scanner()
    frame = build_candles(
        np.linspace(100, 140, 75),
        dates=pd.date_range(
            "2026-09-25 09:15", periods=75, freq="5min", tz="Asia/Kolkata"
        ),
    )
    scanned = []

    def strategy(candles, symbol):
        scanned.append(candles.copy(deep=True))
        return [
            {
                "tradingsymbol": symbol,
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 140,
                "stopLoss": 135,
                "target": 150,
                "strategy": "Fixture",
                "reasoning": "Synthetic trend",
            }
        ]

    scanner.strategies = {"fixture": SimpleNamespace(calculate_signals=strategy)}
    scanner.playbooks = [_Playbook()]
    scanner.family_mapping = {"fixture": "trend"}
    monkeypatch.setattr(scanner, "_fetch_candles", lambda *args: (frame, False))
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda *args: [{"tradingsymbol": "TEST", "instrument_token": 12345}],
    )
    monkeypatch.setattr(
        config_manager,
        "get_strategy_config",
        lambda: {"fixture": {"enabled": True}, "evaluateOnIncompleteCandle": True},
    )
    monkeypatch.setattr(
        "backend.scanner.now_utc",
        lambda: pd.Timestamp("2026-09-26 16:00", tz="Asia/Kolkata").to_pydatetime(),
    )
    return scanner, frame, scanned


def test_weekend_analysis_keeps_real_timestamps_and_live_freshness_rules(
    offline_scanner, monkeypatch
):
    scanner, frame, scanned = offline_scanner
    assert scanner.scan_watchlist(["TEST"]) == []
    assert not scanned
    signals = scanner.scan_watchlist(["TEST"], analysis_only=True)
    assert len(signals) == 1
    signal = signals[0]
    last_close = frame["date"].iloc[-1] + pd.Timedelta(minutes=5)
    assert signal["analysisOnly"]
    assert pd.Timestamp(signal["analysisAsOf"]) == last_close
    assert signal["market_context"]["analysis_only"]
    assert signal["market_context"]["primary_quality"] == "STALE"
    assert signal["entry_input"]["mode"] == "COMPLETED_CANDLES"
    assert len(scanned[0]) == len(frame)
    assert scanner.last_scanned_candle == {}
    assert scanner.scan_watchlist(["TEST"], analysis_only=True) == []

    # Offline observation never consumes the live decision or regime state.
    monkeypatch.setattr("backend.scanner.now_utc", lambda: last_close.to_pydatetime())
    live = scanner.scan_watchlist(["TEST"])
    assert len(live) == 1
    assert not live[0].get("analysisOnly")
    assert live[0]["market_context"]["primary_quality"] == "VALID"


@pytest.mark.parametrize("defect", ["gap", "invalid", "volume", "weekend"])
def test_analysis_never_relaxes_chart_integrity_checks(offline_scanner, defect):
    scanner, frame, scanned = offline_scanner
    if defect == "gap":
        frame.drop(index=20, inplace=True)
    elif defect == "invalid":
        frame.loc[20, "close"] = -1
    elif defect == "volume":
        frame.drop(columns="volume", inplace=True)
    else:
        frame["date"] += pd.Timedelta(days=1)
    assert scanner.scan_watchlist(["TEST"], analysis_only=True) == []
    assert not scanned


def test_analysis_context_cannot_be_used_for_live_position_management(offline_scanner):
    scanner, frame, _ = offline_scanner
    analysis = scanner._market_context(12345, frame, analysis_only=True)
    live = scanner._market_context(12345, frame)
    assert analysis.primary_quality.status is ContextQuality.STALE
    assert analysis.analysis_decision_eligible
    assert not analysis.normal_decision_eligible
    assert analysis.atr is not None
    assert not live.analysis_decision_eligible
    assert not live.normal_decision_eligible
    assert live.atr is None


def test_analysis_never_finishes_a_bar_fetched_before_its_close(offline_scanner):
    scanner, frame, _ = offline_scanner
    # Cached data fetched during the final candle is still incomplete tomorrow.
    receipt = frame["date"].iloc[-1] + pd.Timedelta(minutes=2)
    frame.attrs.update(received_at=receipt, source_as_of=receipt)
    signals = scanner.scan_watchlist(["TEST"], analysis_only=True)
    assert len(signals) == 1
    assert pd.Timestamp(signals[0]["analysisAsOf"]) == frame["date"].iloc[-1]
    assert signals[0]["entry_input"]["mode"] == "COMPLETED_CANDLES"
