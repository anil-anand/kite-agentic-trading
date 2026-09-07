from typing import Any, Dict, List

import pandas as pd
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice


class BreakoutEvidence:
    """
    Groups redundant breakout signals (Bollinger, Keltner, Donchian)
    into a single `breakout_evidence` signal to prevent confluence inflation.
    Adds confirmation from volume, volatility (ATR), trend (ADX), and VWAP.
    """

    BREAKOUT_STRATEGIES = {
        "bollinger_breakout",
        "keltner_breakout",
        "donchian_breakout",
    }

    @classmethod
    def aggregate(
        cls, signals: List[Dict[str, Any]], df: pd.DataFrame
    ) -> List[Dict[str, Any]]:
        """
        Groups breakout signals by direction and enriches them with context.
        """
        other_signals = [
            s for s in signals if s.get("strategy_id") not in cls.BREAKOUT_STRATEGIES
        ]
        breakout_signals = [
            s for s in signals if s.get("strategy_id") in cls.BREAKOUT_STRATEGIES
        ]

        if not breakout_signals:
            return other_signals

        buy_breakouts = [s for s in breakout_signals if s.get("direction") == "BUY"]
        sell_breakouts = [s for s in breakout_signals if s.get("direction") == "SELL"]

        aggregated = list(other_signals)

        if buy_breakouts:
            aggregated.append(cls._create_evidence(buy_breakouts, "BUY", df))

        if sell_breakouts:
            aggregated.append(cls._create_evidence(sell_breakouts, "SELL", df))

        return aggregated

    @classmethod
    def _create_evidence(
        cls, bk_list: List[Dict[str, Any]], direction: str, df: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Merges multiple breakout signals into one evidence model.
        """
        base_signal = max(bk_list, key=lambda x: x.get("signal_score", 0))
        base_score = base_signal.get("signal_score", 0)

        df_calc = df.copy()

        # 1. Relative Volume (20-period)
        vol_sma = df_calc["volume"].rolling(window=20).mean()
        rel_vol = (
            df_calc["volume"].iloc[-1] / vol_sma.iloc[-1]
            if not pd.isna(vol_sma.iloc[-1]) and vol_sma.iloc[-1] > 0
            else 1.0
        )

        # 2. ATR Expansion (14-period)
        atr_ind = AverageTrueRange(
            high=df_calc["high"],
            low=df_calc["low"],
            close=df_calc["close"],
            window=14,
        )
        atr = atr_ind.average_true_range()
        atr_expansion = (
            atr.iloc[-1] / atr.iloc[-2]
            if len(atr) > 1 and not pd.isna(atr.iloc[-2]) and atr.iloc[-2] > 0
            else 1.0
        )

        # 3. ADX Trend Strength (14-period)
        adx_ind = ADXIndicator(
            high=df_calc["high"], low=df_calc["low"], close=df_calc["close"], window=14
        )
        adx = adx_ind.adx()
        adx_val = adx.iloc[-1] if not pd.isna(adx.iloc[-1]) else 0.0

        # 4. VWAP Position
        vwap_ind = VolumeWeightedAveragePrice(
            high=df_calc["high"],
            low=df_calc["low"],
            close=df_calc["close"],
            volume=df_calc["volume"],
            window=14,
        )
        vwap = vwap_ind.volume_weighted_average_price()
        close_price = df_calc["close"].iloc[-1]
        vwap_pos = (
            close_price / vwap.iloc[-1]
            if not pd.isna(vwap.iloc[-1]) and vwap.iloc[-1] > 0
            else 1.0
        )

        # Scoring Logic
        score_modifier = 0

        # Multiple indicators firing gives a small boost
        score_modifier += min((len(bk_list) - 1) * 5, 10)

        # Volume Confirmation
        if rel_vol > 2.0:
            score_modifier += 10
        elif rel_vol > 1.5:
            score_modifier += 5

        # ATR Expansion
        if atr_expansion > 1.1:
            score_modifier += 5

        # Strong Trend Support
        if adx_val > 25:
            score_modifier += 5

        # VWAP Alignment
        if direction == "BUY" and vwap_pos > 1.0:
            score_modifier += 5
        elif direction == "SELL" and vwap_pos < 1.0:
            score_modifier += 5

        final_score = min(100, base_score + score_modifier)

        if final_score >= 80:
            quality = "STRONG"
        elif final_score >= 60:
            quality = "MODERATE"
        else:
            quality = "WEAK"

        raw_readings = {s["strategy_id"]: s.get("indicators", {}) for s in bk_list}
        raw_readings["evidence_metrics"] = {
            "relative_volume": float(round(rel_vol, 2)),
            "atr_expansion": float(round(atr_expansion, 2)),
            "adx": float(round(adx_val, 2)),
            "vwap_position": float(round(vwap_pos, 4)),
            "quality": quality,
        }

        # Derive reasonable stop loss and target bounds
        if direction == "BUY":
            entry = max(s.get("entryPrice", 0) for s in bk_list)
            sl = min(s.get("stopLoss", float("inf")) for s in bk_list)
            target = max(s.get("target", 0) for s in bk_list)
        else:
            entry = min(s.get("entryPrice", float("inf")) for s in bk_list)
            sl = max(s.get("stopLoss", 0) for s in bk_list)
            target = min(s.get("target", float("inf")) for s in bk_list)

        return {
            "strategy_id": "breakout_evidence",
            "family": "breakout",
            "direction": direction,
            "signal_score": final_score,
            "timestamp": base_signal.get("timestamp"),
            "entryPrice": entry,
            "stopLoss": sl if sl != float("inf") else 0,
            "target": target if target != float("inf") else 0,
            "indicators": raw_readings,
            "raw_signals": bk_list,
            "tradingsymbol": base_signal.get("tradingsymbol"),
            "exchange": base_signal.get("exchange", "NSE"),
            "reasoning": f"Aggregated {len(bk_list)} breakout(s) ({direction}). Quality: {quality}. Base: {base_signal.get('strategy_id')}",
        }
