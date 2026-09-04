"""Tests for the deterministic strategy selector core (issue #62, Phase 1).

These are pure-function tests — no engine, no broker, no network.
"""

from backend.scanner import scanner
from backend.strategy_selector import (
    REGIME_FAMILIES,
    STRATEGY_FAMILIES,
    MarketContext,
    all_strategy_ids,
    assemble_context,
    classify_regime,
    select_strategies,
)


class TestFamilyMap:
    def test_families_match_the_scanner_registry_exactly(self):
        # Every registered strategy must be classified into exactly one family,
        # and no family may reference an unknown strategy.
        registry = set(scanner.strategies.keys())
        mapped = all_strategy_ids()
        assert len(mapped) == len(set(mapped)), "a strategy is in two families"
        assert set(mapped) == registry

    def test_regime_families_reference_known_families(self):
        for families in REGIME_FAMILIES.values():
            for fam in families:
                assert fam in STRATEGY_FAMILIES


class TestClassifyRegime:
    def test_trending_up(self):
        ctx = MarketContext(
            net_move_pct=0.8, avg_abs_move_pct=0.9, breadth_up_pct=72, sample_size=90
        )
        assert classify_regime(ctx) == "TRENDING"

    def test_trending_down(self):
        ctx = MarketContext(
            net_move_pct=-0.7, avg_abs_move_pct=0.9, breadth_up_pct=28, sample_size=90
        )
        assert classify_regime(ctx) == "TRENDING"

    def test_range_bound(self):
        ctx = MarketContext(
            net_move_pct=0.05, avg_abs_move_pct=0.4, breadth_up_pct=51, sample_size=90
        )
        assert classify_regime(ctx) == "RANGE_BOUND"

    def test_high_volatility_dominates(self):
        # Even with a directional move, a wide average move => high-vol.
        ctx = MarketContext(
            net_move_pct=0.9, avg_abs_move_pct=2.2, breadth_up_pct=70, sample_size=90
        )
        assert classify_regime(ctx) == "HIGH_VOLATILITY"

    def test_directional_but_flat_breadth_is_not_trending(self):
        # Directional net move but breadth not skewed => not a trend.
        ctx = MarketContext(
            net_move_pct=0.6, avg_abs_move_pct=0.8, breadth_up_pct=50, sample_size=90
        )
        assert classify_regime(ctx) == "RANGE_BOUND"

    def test_directional_with_no_breadth_input_is_trending(self):
        # Breadth unavailable => don't block the trend call on it.
        ctx = MarketContext(
            net_move_pct=0.6, avg_abs_move_pct=0.8, breadth_up_pct=None, sample_size=90
        )
        assert classify_regime(ctx) == "TRENDING"

    def test_unknown_on_small_sample(self):
        ctx = MarketContext(
            net_move_pct=0.8, avg_abs_move_pct=0.9, breadth_up_pct=72, sample_size=3
        )
        assert classify_regime(ctx) == "UNKNOWN"

    def test_unknown_when_core_inputs_missing(self):
        ctx = MarketContext(net_move_pct=None, avg_abs_move_pct=None, sample_size=90)
        assert classify_regime(ctx) == "UNKNOWN"


class TestSelectStrategies:
    def test_trending_enables_trend_breakout_ma_disables_mean_reversion(self):
        ctx = MarketContext(
            net_move_pct=0.8, avg_abs_move_pct=0.9, breadth_up_pct=72, sample_size=90
        )
        d = select_strategies(ctx, all_strategy_ids())
        assert d["applied"] is True
        assert d["regime"] == "TRENDING"
        assert "supertrend" in d["enabled"]  # trend/momentum
        assert "donchian_breakout" in d["enabled"]  # breakout
        assert "ema_crossover" in d["enabled"]  # MA
        assert "rsi_reversal" in d["disabled"]  # mean-reversion off
        assert "vwap_bounce" in d["disabled"]  # volume off

    def test_range_bound_enables_mean_reversion_and_volume(self):
        ctx = MarketContext(
            net_move_pct=0.05, avg_abs_move_pct=0.4, breadth_up_pct=51, sample_size=90
        )
        d = select_strategies(ctx, all_strategy_ids())
        assert d["regime"] == "RANGE_BOUND"
        assert "rsi_reversal" in d["enabled"]
        assert "vwap_bounce" in d["enabled"]
        assert "supertrend" in d["disabled"]

    def test_high_vol_restricts_to_breakout_and_volume(self):
        ctx = MarketContext(
            net_move_pct=0.1, avg_abs_move_pct=2.2, breadth_up_pct=55, sample_size=90
        )
        d = select_strategies(ctx, all_strategy_ids())
        assert d["regime"] == "HIGH_VOLATILITY"
        assert set(d["enabled"]) == {
            "bollinger_breakout",
            "keltner_breakout",
            "donchian_breakout",
            "vwap_bounce",
            "mfi_exhaustion",
        }

    def test_enabled_disabled_are_always_subsets_of_available(self):
        ctx = MarketContext(
            net_move_pct=0.8, avg_abs_move_pct=0.9, breadth_up_pct=72, sample_size=90
        )
        # Only permit a handful of strategies.
        available = ["supertrend", "rsi_reversal", "ema_crossover"]
        d = select_strategies(ctx, available)
        assert set(d["enabled"]) | set(d["disabled"]) <= set(available)
        # supertrend + ema_crossover suit a trend; rsi_reversal does not.
        assert set(d["enabled"]) == {"supertrend", "ema_crossover"}
        assert d["disabled"] == ["rsi_reversal"]

    def test_never_enables_an_unknown_id(self):
        ctx = MarketContext(
            net_move_pct=0.8, avg_abs_move_pct=0.9, breadth_up_pct=72, sample_size=90
        )
        d = select_strategies(ctx, ["supertrend", "not_a_real_strategy"])
        assert "not_a_real_strategy" not in d["enabled"]

    def test_unknown_regime_is_a_noop(self):
        ctx = MarketContext(sample_size=2)
        d = select_strategies(ctx, all_strategy_ids())
        assert d["applied"] is False
        assert d["enabled"] == []
        assert d["disabled"] == []
        assert d["regime"] == "UNKNOWN"


class TestAssembleContext:
    def _quotes(self, moves):
        # moves: dict symbol -> (open, ltp)
        return {
            f"NSE:{sym}": {"last_price": ltp, "ohlc": {"open": op}}
            for sym, (op, ltp) in moves.items()
        }

    def test_computes_breadth_net_and_avg(self):
        # 3 up, 1 down.
        quotes = self._quotes(
            {
                "A": (100, 101),  # +1%
                "B": (100, 102),  # +2%
                "C": (100, 100.5),  # +0.5%
                "D": (100, 98),  # -2%
            }
        )
        ctx = assemble_context(lambda instr: quotes, ["A", "B", "C", "D"])
        assert ctx.sample_size == 4
        assert ctx.breadth_up_pct == 75.0
        assert round(ctx.net_move_pct, 2) == 0.38
        assert round(ctx.avg_abs_move_pct, 2) == 1.38
        assert "relative_volume" in ctx.missing_inputs  # never faked in Phase 1

    def test_empty_quotes_is_unknown_sample(self):
        ctx = assemble_context(lambda instr: {}, ["A", "B"])
        assert ctx.sample_size == 0
        assert classify_regime(ctx) == "UNKNOWN"

    def test_quote_fetch_error_degrades_gracefully(self):
        def boom(_instr):
            raise RuntimeError("network")

        ctx = assemble_context(boom, ["A", "B"])
        assert ctx.sample_size == 0
        assert "market_quotes" in ctx.missing_inputs
        assert classify_regime(ctx) == "UNKNOWN"

    def test_skips_rows_missing_price_or_open(self):
        quotes = {
            "NSE:A": {"last_price": 101, "ohlc": {"open": 100}},
            "NSE:B": {"last_price": None, "ohlc": {"open": 100}},
            "NSE:C": {"ohlc": {"open": 100}},  # no last_price
            "NSE:D": {"last_price": 100},  # no ohlc
        }
        ctx = assemble_context(lambda instr: quotes, ["A", "B", "C", "D"])
        assert ctx.sample_size == 1
