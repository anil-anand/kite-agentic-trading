from typing import Any, Dict, List


class OscillatorEvidence:
    """
    Groups redundant oscillator signals (e.g. RSI, Stochastic, Williams %R)
    into a single `oscillator_evidence` signal to prevent confluence inflation.
    """

    OSCILLATOR_STRATEGIES = {
        "rsi_reversal",
        "stochastic_reversal",
        "stoc_rsi",
        "cci_reversal",
        "williams_r",
        "mfi_exhaustion",
    }

    @classmethod
    def aggregate(
        cls, signals: List[Dict[str, Any]], cap: int = 15
    ) -> List[Dict[str, Any]]:
        """
        Takes raw signals, extracts oscillator signals, groups them by direction,
        and returns non-oscillator signals + aggregated oscillator signals.
        """
        other_signals = [
            s for s in signals if s.get("strategy_id") not in cls.OSCILLATOR_STRATEGIES
        ]
        oscillator_signals = [
            s for s in signals if s.get("strategy_id") in cls.OSCILLATOR_STRATEGIES
        ]

        if not oscillator_signals:
            return other_signals

        buy_oscillators = [s for s in oscillator_signals if s.get("direction") == "BUY"]
        sell_oscillators = [
            s for s in oscillator_signals if s.get("direction") == "SELL"
        ]

        aggregated = list(other_signals)

        if buy_oscillators:
            aggregated.append(cls._create_evidence(buy_oscillators, "BUY", cap))

        if sell_oscillators:
            aggregated.append(cls._create_evidence(sell_oscillators, "SELL", cap))

        return aggregated

    @classmethod
    def _create_evidence(
        cls, osc_list: List[Dict[str, Any]], direction: str, cap: int
    ) -> Dict[str, Any]:
        """
        Merges multiple oscillator signals into one.
        """
        base_signal = max(osc_list, key=lambda x: x.get("signal_score", 0))
        base_score = base_signal.get("signal_score", 0)

        additional = min((len(osc_list) - 1) * 5, cap)
        final_score = min(100, base_score + additional)

        raw_readings = {s["strategy_id"]: s.get("indicators", {}) for s in osc_list}

        return {
            "strategy_id": "oscillator_evidence",
            "family": "mean_reversion",
            "direction": direction,
            "signal_score": final_score,
            "timestamp": base_signal.get("timestamp"),
            "entryPrice": base_signal.get("entryPrice"),
            "stopLoss": base_signal.get("stopLoss"),
            "target": base_signal.get("target"),
            "indicators": raw_readings,
            "raw_signals": osc_list,
            "tradingsymbol": base_signal.get("tradingsymbol"),
            "exchange": base_signal.get("exchange", "NSE"),
            "reasoning": f"Aggregated {len(osc_list)} oscillators ({direction}). Base: {base_signal.get('strategy_id')}",
        }
