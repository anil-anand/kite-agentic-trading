import numpy as np
import pandas as pd
import pytest
from ta.volume import VolumeWeightedAveragePrice

from backend.strategies.breakout_evidence import BreakoutEvidence


def test_breakout_evidence_aggregation():
    # Construct a sample df for ATR, VWAP, ADX
    # Need at least 25 candles
    closes = np.linspace(100, 110, 30)
    highs = closes + 2
    lows = closes - 2
    volumes = np.random.randint(100, 500, 30)
    # Give a massive volume spike at the end
    volumes[-1] = 2000

    df = pd.DataFrame(
        {
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
        }
    )

    signals = [
        {
            "strategy_id": "bollinger_breakout",
            "direction": "BUY",
            "signal_score": 75,
            "entryPrice": 110.0,
            "stopLoss": 105.0,
            "target": 120.0,
            "indicators": {"bb_high": 108.0},
            "tradingsymbol": "TEST",
        },
        {
            "strategy_id": "keltner_breakout",
            "direction": "BUY",
            "signal_score": 80,
            "entryPrice": 110.0,
            "stopLoss": 106.0,
            "target": 122.0,
            "indicators": {"kc_h": 109.0},
            "tradingsymbol": "TEST",
        },
        {
            "strategy_id": "ema_crossover",
            "direction": "BUY",
            "signal_score": 70,
            "entryPrice": 110.0,
            "stopLoss": 105.0,
            "target": 120.0,
            "indicators": {"ema": 108.0},
            "tradingsymbol": "TEST",
        },
    ]

    aggregated = BreakoutEvidence.aggregate(signals, df)

    # Should contain ema_crossover + breakout_evidence
    assert len(aggregated) == 2

    bk_ev = next(s for s in aggregated if s.get("strategy_id") == "breakout_evidence")
    ema = next(s for s in aggregated if s.get("strategy_id") == "ema_crossover")

    assert bk_ev["direction"] == "BUY"
    assert ema["direction"] == "BUY"
    assert len(bk_ev["raw_signals"]) == 2
    assert bk_ev["signal_score"] > 80  # Base 80 + bonuses
    assert bk_ev["family"] == "breakout"

    # Check bounds
    assert bk_ev["entryPrice"] == 110.0
    assert bk_ev["stopLoss"] == 105.0  # Min of 105 and 106
    assert bk_ev["target"] == 122.0  # Max of 120 and 122

    # Check metrics
    metrics = bk_ev["indicators"]["evidence_metrics"]
    assert "quality" in metrics
    assert metrics["relative_volume"] > 2.0  # Due to the volume spike


@pytest.mark.parametrize(("direction", "score_change"), [("BUY", 5), ("SELL", -5)])
def test_session_vwap_entry_score_change_is_limited_to_alignment_bonus(
    direction, score_change, monkeypatch
):
    dates = list(pd.date_range("2026-09-01 09:15", periods=28, freq="5min"))
    dates += list(pd.date_range("2026-09-02 09:15", periods=2, freq="5min"))
    closes = np.array([200.0] * 28 + [100.0, 101.0])
    frame = pd.DataFrame(
        {
            "date": dates,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": 100.0,
        }
    )
    signal = {
        "strategy_id": "bollinger_breakout",
        "direction": direction,
        "signal_score": 40,
        "entryPrice": 101,
        "stopLoss": 95,
        "target": 110,
    }
    current = BreakoutEvidence.aggregate([signal], frame)[0]

    class LegacyRollingVWAP:
        def __init__(self, df):
            self.df = df

        def vwap(self):
            return VolumeWeightedAveragePrice(
                high=self.df["high"],
                low=self.df["low"],
                close=self.df["close"],
                volume=self.df["volume"],
                window=14,
            ).volume_weighted_average_price()

    monkeypatch.setattr(
        "backend.strategies.breakout_evidence.SessionVWAP", LegacyRollingVWAP
    )
    legacy = BreakoutEvidence.aggregate([signal], frame)[0]

    # A gap followed by reclaim is above today's VWAP but below yesterday's
    # rolling anchor. Only the existing directional five-point bonus changes.
    assert current["signal_score"] - legacy["signal_score"] == score_change
    metrics = current["indicators"]["evidence_metrics"]
    assert metrics["vwap_definition"] == "session_typical_price_v1"
    assert metrics["vwap_position"] == round(101 / 100.5, 4)
    for key in ("entryPrice", "stopLoss", "target", "raw_signals", "direction"):
        assert current[key] == legacy[key]


def test_unavailable_session_vwap_is_unknown_in_breakout_evidence():
    closes = np.linspace(100, 110, 30)
    frame = pd.DataFrame(
        {"high": closes + 1, "low": closes - 1, "close": closes, "volume": 0}
    )
    signal = {
        "strategy_id": "bollinger_breakout",
        "direction": "BUY",
        "signal_score": 40,
    }

    evidence = BreakoutEvidence.aggregate([signal], frame)[0]

    assert evidence["indicators"]["evidence_metrics"]["vwap_position"] is None
