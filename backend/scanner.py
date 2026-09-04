import datetime
import time
from typing import Any, Dict, List, Tuple

import pandas as pd

from .config import config_manager
from .kite_client import kite_client
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
        strategy_config = config_manager.get_effective_strategy_config()

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

            symbol_signals = []
            for strat_id, strategy in self.strategies.items():
                config = strategy_config.get(strat_id, {})
                if config.get("enabled", False):
                    signals = strategy.calculate_signals(df, symbol)
                    symbol_signals.extend(signals)

            # Protect Kite API limits (max 3 historical requests per second total)
            # Only sleep if we actually hit the API. If we used cache, run instantly!
            if not was_cached:
                time.sleep(1.1)

            return symbol_signals

        # Run with max_workers=3 to safely fetch data in parallel without hitting 429 Too Many Requests
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

        # Sort by confidence descending
        all_signals.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return all_signals

    def evaluate_position(
        self, tradingsymbol: str, instrument_token: int
    ) -> Dict[str, Any]:
        """
        Re-evaluate a single symbol against all enabled strategies.
        Returns a directional summary for thesis invalidation checks.
        """
        strategy_config = config_manager.get_effective_strategy_config()

        df, _ = self._fetch_candles(instrument_token, tradingsymbol)
        if df.empty:
            return {"buy_signals": 0, "sell_signals": 0, "strategies": []}

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
                    if sig.get("direction") == "BUY":
                        buy_signals += 1
                        triggered_strategies.append(
                            {
                                "strategy": strat_id,
                                "direction": "BUY",
                                "confidence": sig.get("confidence", 0),
                            }
                        )
                    elif sig.get("direction") == "SELL":
                        sell_signals += 1
                        triggered_strategies.append(
                            {
                                "strategy": strat_id,
                                "direction": "SELL",
                                "confidence": sig.get("confidence", 0),
                            }
                        )
            except Exception:
                pass  # Skip individual strategy failures silently

        return {
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "strategies": triggered_strategies,
        }


scanner = Scanner()
