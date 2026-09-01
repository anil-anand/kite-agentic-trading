"""Tests for the shared BaseStrategy helpers used by every strategy."""
import pytest

from backend.strategies.ema_crossover import EMACrossoverStrategy


@pytest.fixture
def strategy():
    # Any concrete strategy exposes the BaseStrategy helper methods.
    return EMACrossoverStrategy()


class TestCalculateStopLoss:
    def test_buy_stop_is_below_entry(self, strategy):
        assert strategy.calculate_stop_loss(100.0, "BUY", 1.5) == 98.5

    def test_sell_stop_is_above_entry(self, strategy):
        assert strategy.calculate_stop_loss(100.0, "SELL", 1.5) == 101.5

    def test_uses_config_default_when_percentage_missing(self, strategy):
        # Default defaultStopLossPercent is 1.5 -> 1.5% below entry for a BUY.
        assert strategy.calculate_stop_loss(200.0, "BUY") == 197.0


class TestCalculateTarget:
    def test_buy_target_is_above_entry(self, strategy):
        # entry > sl implies BUY direction inside calculate_target.
        assert strategy.calculate_target(100.0, 98.0, 3.0) == 103.0

    def test_sell_target_is_below_entry(self, strategy):
        # entry < sl implies SELL direction.
        assert strategy.calculate_target(100.0, 102.0, 3.0) == 97.0


class TestFormatSignal:
    def test_signal_has_all_required_fields(self, strategy):
        sig = strategy.format_signal(
            "RELIANCE", "BUY", 82, 100.0, 98.5, 103.0, 2.0,
            "unit test", {"foo": 1},
        )
        assert sig["tradingsymbol"] == "RELIANCE"
        assert sig["exchange"] == "NSE"
        assert sig["strategy"] == "EMA Crossover"
        assert sig["direction"] == "BUY"
        assert sig["confidence"] == 82
        assert sig["entryPrice"] == 100.0
        assert sig["stopLoss"] == 98.5
        assert sig["target"] == 103.0
        assert sig["riskReward"] == 2.0
        assert sig["indicators"] == {"foo": 1}
        assert "timestamp" in sig

    def test_generate_signal_id_is_unique(self, strategy):
        ids = {strategy.generate_signal_id() for _ in range(1000)}
        assert len(ids) == 1000
