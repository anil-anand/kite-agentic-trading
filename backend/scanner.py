import datetime
import time
from typing import Any, Dict, List, Tuple

import pandas as pd

from .calibration import calibrator
from .config import config_manager
from .kite_client import kite_client
from .playbooks import BreakoutPlaybook, MeanReversionPlaybook, TrendPullbackPlaybook
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
        self.last_scanned_candle = {}
        self.playbooks = [
            TrendPullbackPlaybook(),
            BreakoutPlaybook(),
            MeanReversionPlaybook(),
        ]

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

            evaluate_on_incomplete = strategy_config.get(
                "evaluateOnIncompleteCandle", False
            )
            if not evaluate_on_incomplete:
                if "date" in df.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                        df["date"] = pd.to_datetime(df["date"])

                    if df["date"].dt.tz is None:
                        df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")

                    now = pd.Timestamp.now(tz="Asia/Kolkata")
                    df = df[df["date"] + pd.Timedelta(minutes=5) <= now].copy()

            if df.empty:
                return []

            if not evaluate_on_incomplete and "date" in df.columns:
                latest_candle_time = df["date"].iloc[-1]
                if self.last_scanned_candle.get(symbol) == latest_candle_time:
                    return []
                self.last_scanned_candle[symbol] = latest_candle_time

            # 1. Classify Regime
            regime_info = regime_classifier.classify(df)
            regime = regime_info["regime"]
            regime_state = regime_info

            raw_signals = []
            for strat_id, strategy in self.strategies.items():
                config = strategy_config.get(strat_id, {})
                if config.get("enabled", False):
                    signals = strategy.calculate_signals(df, symbol)
                    for s in signals:
                        s["strategy_id"] = strat_id
                        s["family"] = self.family_mapping.get(strat_id)
                    raw_signals.extend(signals)

            from .strategies.oscillator_evidence import OscillatorEvidence

            raw_signals = OscillatorEvidence.aggregate(raw_signals)

            # 2. Gating and Aggregation
            symbol_aggregated_signals = []
            for playbook in self.playbooks:
                if regime not in playbook.applicable_regimes():
                    continue

                decision = playbook.evaluate_entry(raw_signals, regime_state)
                if decision:
                    est_prob, sample_size = calibrator.get_probability(
                        f"playbook_{playbook.get_name()}", decision["signal_score"]
                    )

                    decision.update(
                        {
                            "estimated_probability": est_prob,
                            "calibration_sample_size": sample_size,
                            "indicators": regime_info["features"],
                            "raw_signals": raw_signals,
                            "regime": regime,
                        }
                    )
                    symbol_aggregated_signals.append(decision)

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

        evaluate_on_incomplete = strategy_config.get(
            "evaluateOnIncompleteCandle", False
        )
        if not evaluate_on_incomplete:
            if "date" in df.columns:
                if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                    df["date"] = pd.to_datetime(df["date"])

                if df["date"].dt.tz is None:
                    df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")

                now = pd.Timestamp.now(tz="Asia/Kolkata")
                df = df[df["date"] + pd.Timedelta(minutes=5) <= now].copy()

        if df.empty:
            return {
                "regime": "UNCERTAIN",
                "buy_signals": 0,
                "sell_signals": 0,
                "strategies": [],
            }

        regime_info = regime_classifier.classify(df)

        raw_signals = []
        for strat_id, strategy in self.strategies.items():
            config = strategy_config.get(strat_id, {})
            if not config.get("enabled", False):
                continue

            try:
                signals = strategy.calculate_signals(df, tradingsymbol)
                for sig in signals:
                    sig["strategy_id"] = strat_id
                    sig["family"] = self.family_mapping.get(strat_id, "unknown")
                raw_signals.extend(signals)
            except Exception:
                pass  # Skip individual strategy failures silently

        from .strategies.oscillator_evidence import OscillatorEvidence

        aggregated_signals = OscillatorEvidence.aggregate(raw_signals)

        buy_signals = 0
        sell_signals = 0
        triggered_strategies = []

        for sig in aggregated_signals:
            direction = sig.get("direction")
            if direction in ("BUY", "SELL"):
                if direction == "BUY":
                    buy_signals += 1
                else:
                    sell_signals += 1
                triggered_strategies.append(
                    {
                        "strategy": sig.get("strategy_id"),
                        "family": sig.get("family", "unknown"),
                        "direction": direction,
                        "signal_score": sig.get("signal_score", 0),
                        "timestamp": sig.get("timestamp"),
                        "indicator_snapshot": sig.get("indicators", {}),
                    }
                )

        return {
            "regime": regime_info["regime"],
            "regime_features": regime_info["features"],
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "strategies": triggered_strategies,
        }


scanner = Scanner()
