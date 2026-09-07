import numpy as np
import pandas as pd

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
