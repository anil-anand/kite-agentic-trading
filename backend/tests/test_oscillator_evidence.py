import numpy as np
import pytest

from backend.config import config_manager
from backend.kite_client import kite_client
from backend.scanner import scanner
from backend.strategies.oscillator_evidence import OscillatorEvidence
from backend.tests.conftest import build_candles


@pytest.fixture
def mock_scanner_config(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "get_families_config",
        lambda: {
            "trend": {"enabled": True, "weight": 1.0},
            "mean_reversion": {"enabled": True, "weight": 1.0},
            "breakout": {"enabled": True, "weight": 1.0},
        },
    )
    strat_cfg = {k: {"enabled": True} for k in scanner.strategies.keys()}
    monkeypatch.setattr(config_manager, "get_strategy_config", lambda: strat_cfg)
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda exchange: [{"tradingsymbol": "TEST", "instrument_token": 12345}],
    )


def test_oscillator_evidence_aggregation_scan_watchlist(
    mock_scanner_config, monkeypatch
):
    # Simulate a ranging market that triggers 5 oscillators
    closes = list(np.linspace(100, 102, 50))
    df = build_candles(closes)
    monkeypatch.setattr(scanner, "_fetch_candles", lambda t, s: (df, False))

    def mock_calc_signals(strategy_id):
        def _calc(self, df_in, symbol):
            return [
                {
                    "strategy_id": strategy_id,
                    "direction": "BUY",
                    "signal_score": 80,
                    "entryPrice": 101.0,
                    "stopLoss": 99.0,
                    "target": 105.0,
                    "family": "mean_reversion",
                    "timestamp": "2026-09-06",
                    "indicators": {"value": 20},
                }
            ]

        return _calc

    # Mock all oscillators to return a BUY signal
    for osc in OscillatorEvidence.OSCILLATOR_STRATEGIES:
        monkeypatch.setattr(
            scanner.strategies[osc],
            "calculate_signals",
            mock_calc_signals(osc).__get__(scanner.strategies[osc]),
        )

    # Disable all other strategies to isolate
    def mock_empty_signals(self, df_in, symbol):
        return []

    for strat_id in scanner.strategies.keys():
        if strat_id not in OscillatorEvidence.OSCILLATOR_STRATEGIES:
            monkeypatch.setattr(
                scanner.strategies[strat_id],
                "calculate_signals",
                mock_empty_signals.__get__(scanner.strategies[strat_id]),
            )

    # Mock playbook to just return what it gets, or verify the signals before playbook
    # Actually, we can just intercept the signals after aggregation but before playbooks
    # Or, we can just call evaluate_position which directly exposes the signals without playbook gating.

    position_eval = scanner.evaluate_position("TEST", 12345)

    # We mocked all 6 oscillators. They should combine into 1.
    assert position_eval["buy_signals"] == 1
    assert position_eval["sell_signals"] == 0

    strats = position_eval["strategies"]
    assert len(strats) == 1
    assert strats[0]["strategy"] == "oscillator_evidence"

    # Base score = 80. +5 for each of the other 5 oscillators (25 total), but capped at 15.
    # So final score should be 80 + 15 = 95.
    assert strats[0]["signal_score"] == 95


def test_oscillator_evidence_standalone():
    # Test the classmethod directly
    raw = []
    for osc in OscillatorEvidence.OSCILLATOR_STRATEGIES:
        raw.append(
            {
                "strategy_id": osc,
                "direction": "BUY",
                "signal_score": 70,
                "indicators": {"val": 10},
            }
        )

    # Add one non-oscillator
    raw.append(
        {"strategy_id": "ema_crossover", "direction": "SELL", "signal_score": 60}
    )

    aggregated = OscillatorEvidence.aggregate(raw, cap=15)

    # Should contain ema_crossover + oscillator_evidence
    assert len(aggregated) == 2

    osc_evidence = next(
        s for s in aggregated if s.get("strategy_id") == "oscillator_evidence"
    )
    ema = next(s for s in aggregated if s.get("strategy_id") == "ema_crossover")

    assert osc_evidence["direction"] == "BUY"
    assert len(osc_evidence["raw_signals"]) == 6
    # 70 + min(5 * 5, 15) = 85
    assert osc_evidence["signal_score"] == 85
    assert len(osc_evidence["indicators"]) == 6  # Should preserve all 6 raw readings

    assert ema["direction"] == "SELL"


def test_oscillator_evidence_mixed_directions():
    raw = [
        {"strategy_id": "rsi_reversal", "direction": "BUY", "signal_score": 80},
        {"strategy_id": "mfi_exhaustion", "direction": "BUY", "signal_score": 70},
        {"strategy_id": "stochastic_reversal", "direction": "SELL", "signal_score": 90},
    ]

    aggregated = OscillatorEvidence.aggregate(raw, cap=15)

    # Should create 1 BUY evidence and 1 SELL evidence
    assert len(aggregated) == 2

    buy_ev = next(s for s in aggregated if s["direction"] == "BUY")
    sell_ev = next(s for s in aggregated if s["direction"] == "SELL")

    assert buy_ev["signal_score"] == 85  # 80 + 5
    assert sell_ev["signal_score"] == 90  # 90 + 0
