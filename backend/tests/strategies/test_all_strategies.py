"""Cross-cutting tests that every strategy must satisfy.

These run each of the 17 strategies through the same scenarios so a newly
added strategy is automatically covered for the universal invariants:
insufficient data is handled, and any emitted signal is well-formed.
"""

import pytest

from backend.scanner import scanner
from backend.tests.conftest import assert_valid_signal, build_candles

# Pull the live strategy registry so new strategies are picked up automatically.
STRATEGY_ITEMS = list(scanner.strategies.items())
STRATEGY_IDS = [sid for sid, _ in STRATEGY_ITEMS]


def _params():
    return [pytest.param(strat, id=sid) for sid, strat in STRATEGY_ITEMS]


def test_registry_has_all_seventeen():
    assert len(STRATEGY_ITEMS) == 17


@pytest.mark.parametrize("strategy", _params())
def test_returns_list(strategy, uptrend):
    result = strategy.calculate_signals(uptrend.copy(), "TEST")
    assert isinstance(result, list)


@pytest.mark.parametrize("strategy", _params())
def test_empty_on_insufficient_data(strategy):
    # Every strategy guards on a minimum bar count; 3 bars is below all of them.
    tiny = build_candles([100, 101, 102])
    assert strategy.calculate_signals(tiny, "TEST") == []


@pytest.mark.parametrize("strategy", _params())
def test_empty_dataframe_is_safe(strategy):
    empty = build_candles([])
    # Should not raise; returns no signals.
    assert strategy.calculate_signals(empty, "TEST") == []


@pytest.mark.parametrize("strategy", _params())
@pytest.mark.parametrize("regime", ["uptrend", "downtrend", "choppy"])
def test_any_emitted_signal_is_well_formed(strategy, regime, request):
    df = request.getfixturevalue(regime)
    signals = strategy.calculate_signals(df.copy(), "TEST")
    for sig in signals:
        assert_valid_signal(sig)
        assert sig["strategy"] == strategy.get_name()
        assert sig["tradingsymbol"] == "TEST"


@pytest.mark.parametrize("strategy", _params())
def test_has_name_and_description(strategy):
    assert isinstance(strategy.get_name(), str) and strategy.get_name()
    assert isinstance(strategy.get_description(), str) and strategy.get_description()
