import datetime
import time
import uuid
from typing import Any, Dict, List, Tuple

import pandas as pd

from .calibration import calibrator
from .config import config_manager
from .kite_client import kite_client
from .regime_classifier import regime_classifier
from .strategies.adx_momentum import ADXMomentumStrategy
from .strategies.awesome_oscillator import AwesomeOscillatorStrategy
from .strategies.bollinger_breakout import BollingerBreakoutStrategy
from .strategies.cci_reversal import CCIReversalStrategy
from .strategies.donchian_breakout import DonchianBreakoutStrategy
from .strategies.ema_crossover import EMACrossoverStrategy
from .strategies.keltner_breakout import KeltnerBreakoutStrategy
from .strategies.macd_cross import MACDCrossStrategy
from .strategies.mfi_exhaustion import MFIExhaustionStrategy
from .strategies.psar_trend import PSARTrendStrategy
from .strategies.rsi_reversal import RSIReversalStrategy
from .strategies.stoc_rsi import StochRSIStrategy
from .strategies.stochastic_reversal import StochasticReversalStrategy
from .strategies.supertrend import SupertrendStrategy
from .strategies.tsi_cross import TSICrossStrategy
from .strategies.vwap_bounce import VWAPBounceStrategy
from .strategies.williams_r import WilliamsRStrategy


class Scanner:
    def __init__(self):
        self.strategies = {
            "ema_crossover": EMACrossoverStrategy(),
            "rsi_reversal": RSIReversalStrategy(),
            "vwap_bounce": VWAPBounceStrategy(),
            "supertrend": SupertrendStrategy(),
            "macd_cross": MACDCrossStrategy(),
            "bollinger_breakout": BollingerBreakoutStrategy(),
            "stochastic_reversal": StochasticReversalStrategy(),
            "adx_momentum": ADXMomentumStrategy(),
            "psar_trend": PSARTrendStrategy(),
            "donchian_breakout": DonchianBreakoutStrategy(),
            "cci_reversal": CCIReversalStrategy(),
            "williams_r": WilliamsRStrategy(),
            "mfi_exhaustion": MFIExhaustionStrategy(),
            "keltner_breakout": KeltnerBreakoutStrategy(),
            "awesome_oscillator": AwesomeOscillatorStrategy(),
            "tsi_cross": TSICrossStrategy(),
            "stoc_rsi": StochRSIStrategy(),
        }
        self.family_mapping = {
            "ema_crossover": "trend",
            "macd_cross": "trend",
            "supertrend": "trend",
            "psar_trend": "trend",
            "tsi_cross": "trend",
            "adx_momentum": "trend",
            "awesome_oscillator": "trend",
            "rsi_reversal": "mean_reversion",
            "stochastic_reversal": "mean_reversion",
            "stoc_rsi": "mean_reversion",
            "cci_reversal": "mean_reversion",
            "williams_r": "mean_reversion",
            "vwap_bounce": "mean_reversion",
            "mfi_exhaustion": "mean_reversion",
            "bollinger_breakout": "breakout",
            "keltner_breakout": "breakout",
            "donchian_breakout": "breakout",
        }
        self.candle_cache = {}
        self.last_cache_time = {}

    def _fetch_candles(
        self, instrument_token: int, tradingsymbol: str
    ) -> Tuple[pd.DataFrame, bool]:
        now = datetime.datetime.now()

        # Use cache if less than 1 minute old
        if (
            instrument_token in self.candle_cache
            and (
                now - self.last_cache_time.get(instrument_token, datetime.datetime.min)
            ).seconds
            < 60
        ):
            return self.candle_cache[instrument_token], True

        from_date = now - datetime.timedelta(days=5)
        to_date = now

        try:
            records = kite_client.get_historical_data(
                instrument_token, from_date, to_date, "5minute"
            )
            if not records:
                return pd.DataFrame(), False

            df = pd.DataFrame(records)
            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    df[col] = df[col].astype(float)
            self.candle_cache[instrument_token] = df
            self.last_cache_time[instrument_token] = now
            return df, False
        except Exception as e:
            print(f"Error fetching candles for {tradingsymbol}: {e}")
            return pd.DataFrame(), False

    def scan_watchlist(
        self, symbols: List[str], on_signal=None
    ) -> List[Dict[str, Any]]:
        import concurrent.futures

        all_signals = []
        strategy_config = config_manager.get_strategy_config()
        family_config = config_manager.get_families_config()

        instruments = kite_client.get_instruments("NSE")
        instrument_map = {
            i["tradingsymbol"]: i["instrument_token"] for i in instruments
        }

        def process_symbol(symbol: str) -> List[Dict[str, Any]]:
            token = instrument_map.get(symbol)
            if not token:
                return []

            df, was_cached = self._fetch_candles(token, symbol)
            if df.empty:
                return []

            # 1. Classify Regime
            regime_info = regime_classifier.classify(df)
            regime = regime_info["regime"]

            raw_signals = []
            for strat_id, strategy in self.strategies.items():
                config = strategy_config.get(strat_id, {})
                if config.get("enabled", False):
                    signals = strategy.calculate_signals(df, symbol)
                    for s in signals:
                        s["strategy_id"] = strat_id
                        s["family"] = self.family_mapping.get(strat_id)
                    raw_signals.extend(signals)

            # 2. Gating and Aggregation
            allowed_families = []
            if regime == "TRENDING":
                allowed_families = ["trend"]
            elif regime == "RANGING":
                allowed_families = ["mean_reversion"]
            elif regime == "BREAKOUT":
                allowed_families = ["breakout", "trend"]

            symbol_aggregated_signals = []

            for family in allowed_families:
                if not family_config.get(family, {}).get("enabled", True):
                    continue

                f_signals = [s for s in raw_signals if s["family"] == family]
                buys = [s for s in f_signals if s["direction"] == "BUY"]
                sells = [s for s in f_signals if s["direction"] == "SELL"]

                for direction, dir_signals in [("BUY", buys), ("SELL", sells)]:
                    if not dir_signals:
                        continue

                    # Base signal for entry, sl, target (use highest signal_score)
                    base_sig = max(dir_signals, key=lambda s: s["signal_score"])

                    avg_conf = sum(s["signal_score"] for s in dir_signals) / len(
                        dir_signals
                    )
                    bonus = 5 * (len(dir_signals) - 1)
                    weight = family_config.get(family, {}).get("weight", 1.0)
                    family_signal_score = min(100, int((avg_conf + bonus) * weight))

                    est_prob, sample_size = calibrator.get_probability(
                        f"family_{family}", family_signal_score
                    )

                    agg_sig = {
                        "id": str(uuid.uuid4()),
                        "tradingsymbol": symbol,
                        "exchange": base_sig.get("exchange", "NSE"),
                        "strategy": f"family_{family}",
                        "direction": direction,
                        "signal_score": family_signal_score,
                        "estimated_probability": est_prob,
                        "calibration_sample_size": sample_size,
                        "entryPrice": base_sig["entryPrice"],
                        "stopLoss": base_sig["stopLoss"],
                        "target": base_sig["target"],
                        "riskReward": base_sig.get("riskReward", 0),
                        "reasoning": f"{regime} regime active. {len(dir_signals)} {family} indicators aligned. Base: {base_sig['reasoning']}",
                        "timestamp": base_sig.get("timestamp"),
                        "indicators": regime_info["features"],
                        "raw_signals": dir_signals,
                        "regime": regime,
                    }
                    symbol_aggregated_signals.append(agg_sig)

            if not was_cached:
                time.sleep(1.1)

            return symbol_aggregated_signals

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(process_symbol, symbol) for symbol in symbols]
            for future in concurrent.futures.as_completed(futures):
                try:
                    signals = future.result()
                    if signals:
                        if on_signal:
                            for sig in signals:
                                on_signal(sig)
                        all_signals.extend(signals)
                except Exception as e:
                    import sys

                    print(f"Error in parallel processing: {e}", file=sys.stderr)

        all_signals.sort(key=lambda x: x.get("signal_score", 0), reverse=True)
        return all_signals

    def evaluate_position(
        self, tradingsymbol: str, instrument_token: int
    ) -> Dict[str, Any]:
        """
        Re-evaluate a single symbol against all enabled strategies and regime.
        Returns a directional summary for thesis invalidation checks.
        """
        strategy_config = config_manager.get_strategy_config()

        df, _ = self._fetch_candles(instrument_token, tradingsymbol)
        if df.empty:
            return {
                "regime": "UNCERTAIN",
                "buy_signals": 0,
                "sell_signals": 0,
                "strategies": [],
            }

        regime_info = regime_classifier.classify(df)

        buy_signals = 0
        sell_signals = 0
        triggered_strategies = []

        for strat_id, strategy in self.strategies.items():
            config = strategy_config.get(strat_id, {})
            if not config.get("enabled", False):
                continue

            try:
                signals = strategy.calculate_signals(df, tradingsymbol)
                for sig in signals:
                    family = self.family_mapping.get(strat_id, "unknown")
                    direction = sig.get("direction")
                    if direction in ("BUY", "SELL"):
                        if direction == "BUY":
                            buy_signals += 1
                        else:
                            sell_signals += 1
                        triggered_strategies.append(
                            {
                                "strategy": strat_id,
                                "family": family,
                                "direction": direction,
                                "signal_score": sig.get("signal_score", 0),
                                "timestamp": sig.get("timestamp"),
                                "indicator_snapshot": sig.get("indicators", {}),
                            }
                        )
            except Exception:
                pass  # Skip individual strategy failures silently

        return {
            "regime": regime_info["regime"],
            "regime_features": regime_info["features"],
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "strategies": triggered_strategies,
        }


scanner = Scanner()
