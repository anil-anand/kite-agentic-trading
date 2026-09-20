import datetime
import threading
from typing import Any, Dict, List, Tuple

import pandas as pd

from .calibration import calibrator
from .config import config_manager
from .kite_client import kite_client
from .market_context import ContextPolicy, MarketContext, MarketContextService
from .playbooks import BreakoutPlaybook, MeanReversionPlaybook, TrendPullbackPlaybook
from .regime_classifier import regime_classifier
from .session_clock import SessionPolicy
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
from .time_utils import as_utc, now_utc
from .utils import push_log


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
        self._market_context_service = MarketContextService()
        self._market_context_policy = self._market_context_service.policy
        self._market_session_policy = SessionPolicy()
        self._market_context_lock = threading.Lock()
        self._scan_locks = {}
        self._scan_locks_lock = threading.Lock()
        self.playbooks = [
            TrendPullbackPlaybook(),
            BreakoutPlaybook(),
            MeanReversionPlaybook(),
        ]

    def _fetch_candles(
        self, instrument_token: int, tradingsymbol: str
    ) -> Tuple[pd.DataFrame, bool]:
        now = now_utc()

        # Cache only within the same candle interval: a payload fetched before
        # the close cannot become a completed candle merely by waiting.
        last_fetch = self.last_cache_time.get(instrument_token)
        source_as_of = self.candle_cache.get(
            instrument_token, pd.DataFrame()
        ).attrs.get("source_as_of", last_fetch)
        if (
            instrument_token in self.candle_cache
            and last_fetch is not None
            and 0 <= (now - last_fetch).total_seconds() < 60
            and source_as_of is not None
            and int(now.timestamp()) // 300 == int(source_as_of.timestamp()) // 300
        ):
            return self.candle_cache[instrument_token].copy(deep=True), True

        from_date = now - datetime.timedelta(days=5)
        to_date = now

        try:
            records = kite_client.get_historical_data(
                instrument_token, from_date, to_date, "5minute"
            )
            received_at = now_utc()
            if not records:
                return pd.DataFrame(), False

            df = self._market_context_service.cache_frame(
                instrument_token, "5minute", pd.DataFrame(records)
            )
            self._preserve_candle_receipts(
                df, self.candle_cache.get(instrument_token), received_at
            )
            # Retain request cutoff and actual receipt separately. A request
            # made before a close may return after it with an unfinished bar.
            df.attrs["source_as_of"] = to_date
            df.attrs["received_at"] = received_at
            self.candle_cache[instrument_token] = df.copy(deep=True)
            self.last_cache_time[instrument_token] = received_at
            return df.copy(deep=True), False
        except Exception as e:
            push_log(
                f"Error fetching candles for {tradingsymbol}: {e}", level="warning"
            )
            return pd.DataFrame(), False

    @staticmethod
    def _preserve_candle_receipts(
        frame: pd.DataFrame, previous: pd.DataFrame | None, received_at
    ) -> None:
        """Keep first observation times for unchanged, already-final versions."""

        # Explicit source observation times take precedence, including invalid
        # values which the context validator must surface rather than replace.
        if "received_at" in frame:
            return

        def version_key(row):
            try:
                stamp = pd.Timestamp(row["date"])
                if pd.isna(stamp):
                    return None
                start = as_utc(stamp.to_pydatetime())
                values = tuple(
                    None if pd.isna(row.get(name)) else float(row[name])
                    for name in ("open", "high", "low", "close", "volume")
                )
                revision = row.get("revision")
                revision = "source-v1" if pd.isna(revision) else str(revision)
                return (start, *values, revision)
            except (KeyError, TypeError, ValueError, OverflowError):
                return None

        observations = {}
        conflicting_starts = set()
        versions = {}
        if previous is not None:
            cutoff = as_utc(previous.attrs.get("source_as_of"))
            for row in previous.to_dict("records"):
                key = version_key(row)
                if key is None or cutoff is None:
                    continue
                prior_version = versions.setdefault(key[0], key)
                if prior_version != key:
                    conflicting_starts.add(key[0])
                try:
                    receipt = as_utc(
                        row.get("received_at", previous.attrs.get("received_at"))
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                end = key[0] + datetime.timedelta(minutes=5)
                if (
                    receipt is not None
                    and not pd.isna(receipt)
                    and end <= min(cutoff, receipt)
                    and receipt <= received_at
                ):
                    observations[key] = min(receipt, observations.get(key, receipt))

        frame["received_at"] = [
            observations.get(key, received_at)
            if key is not None and key[0] not in conflicting_starts
            else received_at
            for key in (version_key(row) for row in frame.to_dict("records"))
        ]

    def _market_context(
        self, instrument_token: int, df: pd.DataFrame, decision_at=None
    ) -> MarketContext:
        """Build a causal context using the current versioned configuration.

        Entry strategy formulas retain their existing incomplete-candle setting
        for this phase.  Position assessment and all context provenance use the
        completed-candle path below, independently of that legacy switch.
        """

        policy = ContextPolicy.from_config(config_manager.get_market_context_config())
        # Operator trading hours constrain admission/supervision, not the
        # exchange's candle grid or full-session VWAP history.
        with self._market_context_lock:
            if policy != self._market_context_policy:
                self._market_context_service = MarketContextService(
                    policy, self._market_session_policy
                )
                self._market_context_policy = policy
            service = self._market_context_service
        event_time = decision_at or now_utc()
        return service.build(
            instrument_token,
            df,
            decision_event_time=event_time,
            received_at=df.attrs.get("received_at", event_time),
            source_as_of=df.attrs.get(
                "source_as_of", df.attrs.get("received_at", event_time)
            ),
        )

    def get_market_context(
        self, instrument_token: int, tradingsymbol: str, *, decision_at=None
    ) -> MarketContext:
        """Fetch a snapshot with original provenance for normal management."""

        df, _ = self._fetch_candles(instrument_token, tradingsymbol)
        return self._market_context(instrument_token, df, decision_at)

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

        def evaluate_symbol(symbol: str) -> List[Dict[str, Any]]:
            token = instrument_map.get(symbol)
            if not token:
                return []

            df, _ = self._fetch_candles(token, symbol)
            if df.empty:
                return []

            context = self._market_context(token, df)
            completed_df = context.primary_frame()
            if (
                not context.normal_decision_eligible
                or "VOLUME_UNAVAILABLE" in context.primary_quality.issues
                or completed_df.empty
            ):
                return []

            evaluate_on_incomplete = strategy_config.get(
                "evaluateOnIncompleteCandle", False
            )
            if not evaluate_on_incomplete:
                df = completed_df

            if df.empty:
                return []

            if not evaluate_on_incomplete and context.primary_bar:
                # Corrections change provenance, not the decision event. Never
                # trade an already-consumed candle again on a later revision.
                latest_candle_id = context.primary_bar.end
                last_scanned = self.last_scanned_candle.get(symbol)
                if last_scanned is not None and latest_candle_id <= last_scanned:
                    return []

            # 1. Classify Regime
            # Keep entry gating on the raw stateless classifier so this phase
            # does not silently change existing strategy selection.  The
            # legacy entry-only incomplete-candle experiment retains its old
            # regime input; position assessment never uses that path.
            if evaluate_on_incomplete:
                regime_state = regime_classifier.classify(df)
            else:
                regime_state = {
                    "regime": context.raw_regime,
                    "features": dict(context.raw_regime_features),
                }
            regime = regime_state["regime"]

            raw_signals = []
            for strat_id, strategy in self.strategies.items():
                config = strategy_config.get(strat_id, {})
                if config.get("enabled", False):
                    signals = strategy.calculate_signals(df.copy(deep=True), symbol)
                    for s in signals:
                        s["strategy_id"] = strat_id
                        s["family"] = self.family_mapping.get(strat_id)
                    raw_signals.extend(signals)

            from .strategies.breakout_evidence import BreakoutEvidence
            from .strategies.oscillator_evidence import OscillatorEvidence

            raw_signals = OscillatorEvidence.aggregate(raw_signals)
            raw_signals = BreakoutEvidence.aggregate(raw_signals, df)

            # 2. Gating and Aggregation
            symbol_aggregated_signals = []
            for playbook in self.playbooks:
                if regime not in playbook.applicable_regimes():
                    continue

                decision = playbook.evaluate_entry(raw_signals, regime_state)
                if decision:
                    est_prob, sample_size = calibrator.get_probability(
                        playbook.get_name(), decision["signal_score"]
                    )

                    raw = decision.get("raw_signals", raw_signals)
                    strategy_ids = {
                        s.get("strategy_id")
                        for s in raw
                        if s.get("strategy_id")
                        and s.get("strategy_id")
                        not in ("breakout_evidence", "oscillator_evidence")
                    }

                    for s in raw:
                        if s.get("strategy_id") in (
                            "breakout_evidence",
                            "oscillator_evidence",
                        ):
                            strategy_ids.add(s["strategy_id"])

                    decision.update(
                        {
                            "estimated_probability": est_prob,
                            "calibration_sample_size": sample_size,
                            "indicators": regime_state["features"],
                            "raw_signals": raw_signals,
                            "regime": regime,
                            "strategy_count": len(strategy_ids),
                            "market_context": context.summary(),
                        }
                    )
                    symbol_aggregated_signals.append(decision)

            if not evaluate_on_incomplete and context.primary_bar:
                self.last_scanned_candle[symbol] = context.primary_bar.end
            return symbol_aggregated_signals

        def process_symbol(symbol: str) -> List[Dict[str, Any]]:
            with self._scan_locks_lock:
                symbol_lock = self._scan_locks.setdefault(symbol, threading.Lock())
            # Manual scans and the entry worker share one decision high-water
            # mark. Unrelated symbols still run concurrently.
            with symbol_lock:
                return evaluate_symbol(symbol)

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

        context = self.get_market_context(instrument_token, tradingsymbol)
        df = context.primary_frame()

        # The legacy assessment depends on the 50-bar regime warmup. Missing
        # history and unusable source data cannot mean zero supporting votes.
        volume_available = "VOLUME_UNAVAILABLE" not in context.primary_quality.issues
        if not context.normal_decision_eligible or not volume_available or len(df) < 50:
            return {
                "regime": "UNCERTAIN",
                "regime_features": {},
                "buy_signals": None,
                "sell_signals": None,
                "strategies": [],
                "assessment_available": False,
                "assessment_issue": (
                    "MARKET_CONTEXT_UNAVAILABLE"
                    if not context.normal_decision_eligible
                    else "VOLUME_UNAVAILABLE"
                    if not volume_available
                    else "INSUFFICIENT_HISTORY"
                ),
                "market_context": context.summary(),
            }

        regime_info = {
            "regime": context.raw_regime,
            "features": dict(context.raw_regime_features),
        }

        raw_signals = []
        strategy_errors = []
        enabled_strategies = 0
        for strat_id, strategy in self.strategies.items():
            config = strategy_config.get(strat_id, {})
            if not config.get("enabled", False):
                continue

            enabled_strategies += 1
            try:
                signals = strategy.calculate_signals(df.copy(deep=True), tradingsymbol)
                for sig in signals:
                    sig["strategy_id"] = strat_id
                    sig["family"] = self.family_mapping.get(strat_id, "unknown")
                raw_signals.extend(signals)
            except Exception:
                strategy_errors.append(strat_id)

        if strategy_errors or not enabled_strategies:
            return {
                "regime": regime_info["regime"],
                "regime_features": regime_info["features"],
                "buy_signals": None,
                "sell_signals": None,
                "strategies": [],
                "assessment_available": False,
                "assessment_issue": (
                    "STRATEGY_FAILURE" if strategy_errors else "NO_ENABLED_STRATEGIES"
                ),
                "strategy_errors": strategy_errors,
                "market_context": context.summary(),
            }

        from .strategies.breakout_evidence import BreakoutEvidence
        from .strategies.oscillator_evidence import OscillatorEvidence

        aggregated_signals = OscillatorEvidence.aggregate(raw_signals)
        aggregated_signals = BreakoutEvidence.aggregate(aggregated_signals, df)

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
            "assessment_available": context.normal_decision_eligible,
            "market_context": context.summary(),
        }


scanner = Scanner()
