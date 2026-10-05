from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.config import config_manager
from backend.exit_management.thesis import capture_entry_thesis
from backend.kite_client import kite_client
from backend.scanner import Scanner
from backend.tests.conftest import build_candles


@pytest.mark.parametrize("incomplete", [False, True])
def test_scanner_pins_selection_config_and_records_the_actual_signal_bar(
    monkeypatch, incomplete
):
    scanner = Scanner()
    frame = build_candles(np.linspace(100, 140, 60))
    decision_at = (
        frame["date"].iloc[-1]
        + (pd.Timedelta(seconds=1) if incomplete else pd.Timedelta(minutes=5))
    ).to_pydatetime()
    live_config = {
        "ema_crossover": {"enabled": True},
        "macd_cross": {"enabled": True},
        "evaluateOnIncompleteCandle": incomplete,
    }
    calls = []

    def first_strategy(candles, symbol):
        # Simulate a Settings save while a scan is calculating.  The remaining
        # strategies and the later operator confirmation must see one snapshot.
        live_config["macd_cross"]["enabled"] = False
        return [
            {
                "tradingsymbol": symbol,
                "direction": "BUY",
                "signal_score": 85,
                "entryPrice": 140.0,
                "stopLoss": 130.0,
                "target": 160.0,
            }
        ]

    def second_strategy(candles, symbol):
        calls.append(True)
        return []

    monkeypatch.setattr(
        scanner,
        "strategies",
        {
            "ema_crossover": SimpleNamespace(calculate_signals=first_strategy),
            "macd_cross": SimpleNamespace(calculate_signals=second_strategy),
        },
    )
    monkeypatch.setattr(config_manager, "get_strategy_config", lambda: live_config)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda *args: (frame, False))
    monkeypatch.setattr("backend.scanner.now_utc", lambda: decision_at)
    monkeypatch.setattr(
        "backend.regime_classifier.regime_classifier.classify",
        lambda candles: {"regime": "TRENDING", "features": {}},
    )
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 1}],
    )

    signal = scanner.scan_watchlist(["TEST"])[0]
    assert calls == [True]
    assert (
        signal["entry_selection_config"]["strategies"]["macd_cross"]["enabled"] is True
    )
    assert signal["entry_selection_config"]["context_policy"]["setup_range_bars"] == 20
    assert signal["entry_input"]["last_input_bar"]["close"] == 140.0
    assert signal["entry_input"]["mode"] == (
        "INCOMPLETE_CANDLE" if incomplete else "COMPLETED_CANDLES"
    )
    if incomplete:
        assert signal["market_context"]["primary_bar"]["close"] < 140.0

    thesis = capture_entry_thesis(
        signal,
        position_key="PAPER:acct:NSE:1:MIS:epoch",
        trade_id="trade",
        position_epoch="epoch",
        instrument_id="1",
        effective_config={"strategies": live_config},
        created_at=decision_at + pd.Timedelta(minutes=1),
    )
    # Entry selection and effective management settings have different clocks.
    assert (
        thesis.entry_selection_config.values["strategies"]["macd_cross"]["enabled"]
        is True
    )
    assert thesis.policy_snapshot.values["strategies"]["macd_cross"]["enabled"] is False
    if incomplete:
        assert thesis.management_profile.name == "unknown_legacy_bounded"
