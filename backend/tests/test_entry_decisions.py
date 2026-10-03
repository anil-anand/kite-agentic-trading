"""Injected production-entry evaluation contracts."""

from datetime import datetime, timezone

import pandas as pd

from backend.entry_decisions import evaluate_production_entries
from backend.tests.exit_management.test_engine import _context


class _MutatingStrategy:
    def calculate_signals(self, frame, symbol):
        frame.loc[:, "close"] = 0.0
        return [
            {
                "id": "strategy-random-id",
                "timestamp": "wall-clock-value",
                "direction": "BUY",
                "entryPrice": 100.0,
                "stopLoss": 95.0,
                "target": 110.0,
                "signal_score": 80,
                "strategy": "Fixture",
            }
        ]


class _Playbook:
    def get_name(self):
        return "fixture-playbook"

    def applicable_regimes(self):
        return {"TRENDING"}

    def evaluate_entry(self, raw_signals, regime_state):
        if not raw_signals:
            return None
        return {
            **raw_signals[0],
            "playbook": self.get_name(),
            "raw_signals": raw_signals,
        }


def test_production_entry_evaluation_is_injected_and_reproducible():
    decision_at = datetime(2026, 9, 21, 4, 25, tzinfo=timezone.utc)
    context = _context(decision_at.replace(minute=20), close=100.0)
    raw_frame = pd.DataFrame(
        {
            "date": [decision_at],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1_000.0],
        }
    )
    args = {
        "symbol": "TEST",
        "raw_frame": raw_frame,
        "market_context": context,
        "strategies": {"fixture": _MutatingStrategy()},
        "playbooks": (_Playbook(),),
        "strategy_config": {"fixture": {"enabled": True}},
        "family_mapping": {"fixture": "trend"},
        "calibration_lookup": lambda playbook, score: (0.6, 10),
        "decision_at": decision_at,
    }

    first = evaluate_production_entries(**args)
    second = evaluate_production_entries(**args)

    assert first == second
    assert first[0]["timestamp"] == decision_at.isoformat()
    assert "id" not in first[0]
    assert first[0]["raw_signals"][0]["timestamp"] == decision_at.isoformat()
    assert raw_frame.loc[0, "close"] == 100.0


def _entry_args(context, frame, *, strategy=None):
    return {
        "symbol": "TEST",
        "raw_frame": frame,
        "market_context": context,
        "strategies": {"fixture": strategy or _MutatingStrategy()},
        "playbooks": (_Playbook(),),
        "strategy_config": {"fixture": {"enabled": True}},
        "family_mapping": {"fixture": "trend"},
        "calibration_lookup": None,
        "decision_at": context.decision_event_time,
    }


def test_real_strategy_uses_injected_risk_and_clock_without_live_access(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from backend.config import config_manager
    from backend.market_context import build_market_context
    from backend.playbooks import TrendPullbackPlaybook
    from backend.strategies.ema_crossover import EMACrossoverStrategy
    from backend.tests.conftest import build_candles

    frame = build_candles([100.0] * 59 + [102.0], volumes=[1000.0] * 59 + [4000.0])
    event_time = (frame["date"].iloc[-1] + pd.Timedelta(minutes=5)).to_pydatetime()
    context = replace(
        build_market_context("1", frame, event_time), raw_regime="TRENDING"
    )
    strategy = EMACrossoverStrategy()
    risk_settings = {"defaultStopLossPercent": 2.0, "defaultTargetPercent": 4.0}
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: risk_settings)
    baseline = strategy.calculate_signals(context.primary_frame(), "TEST")[0]

    def unavailable(*args, **kwargs):
        raise AssertionError("production entry evaluation accessed a live dependency")

    monkeypatch.setattr(config_manager, "get_risk_config", unavailable)
    monkeypatch.setattr("backend.strategies.base.now_utc", unavailable)
    monkeypatch.setattr("backend.calibration.calibrator.get_probability", unavailable)
    args = _entry_args(context, frame, strategy=strategy)
    args["playbooks"] = (TrendPullbackPlaybook(),)
    args["risk_config"] = risk_settings
    first = evaluate_production_entries(**args)[0]
    for field in (
        "direction",
        "entryPrice",
        "stopLoss",
        "target",
        "riskReward",
        "signal_score",
    ):
        assert first[field] == baseline[field]
    assert first["entry_selection_config"]["risk"] == risk_settings
    assert (
        first["selected_evidence"][0]["timestamp"]
        == event_time.astimezone(timezone.utc).isoformat()
    )
    assert not hasattr(strategy, "_evaluation_risk_config")

    # The scanner shares strategy instances across threads. An evaluation must
    # never overwrite the other thread's stop/target settings.
    with ThreadPoolExecutor(max_workers=2) as executor:
        configured = executor.submit(evaluate_production_entries, **args)
        defaults = executor.submit(
            evaluate_production_entries, **{**args, "risk_config": {}}
        )
    assert configured.result()[0] == first
    assert defaults.result()[0]["stopLoss"] == 100.47
    assert defaults.result()[0]["target"] == 105.06


def test_entry_context_cannot_be_relabelled_with_an_earlier_decision_time():
    import pytest

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    args = _entry_args(context, context.primary_frame())
    args["decision_at"] -= pd.Timedelta(minutes=5)
    with pytest.raises(ValueError, match="as-of market context"):
        evaluate_production_entries(**args)


def test_entry_requires_an_aware_decision_time():
    import pytest

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    args = _entry_args(context, context.primary_frame())
    args["decision_at"] = args["decision_at"].replace(tzinfo=None)
    with pytest.raises(ValueError, match="aware decision time"):
        evaluate_production_entries(**args)


def test_entry_rejects_future_bars_even_in_a_mislabelled_context():
    from dataclasses import replace

    import pytest

    from backend.tests.exit_management.test_engine import _bar

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    future = _bar(context.decision_event_time)
    context = replace(context, primary_bars=(*context.primary_bars, future))
    with pytest.raises(ValueError, match="unavailable at decision time"):
        evaluate_production_entries(**_entry_args(context, context.primary_frame()))


def test_incomplete_experiment_excludes_future_bars_and_late_revisions(monkeypatch):
    from backend.tests.conftest import build_candles

    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    event_time = context.decision_event_time
    frame = build_candles(
        [100.0, 200.0, 300.0],
        dates=[
            event_time - pd.Timedelta(minutes=5),
            event_time,
            event_time + pd.Timedelta(minutes=5),
        ],
    )
    frame["received_at"] = [
        event_time,
        event_time + pd.Timedelta(seconds=1),
        event_time,
    ]
    seen = []

    class ObservingStrategy(_MutatingStrategy):
        def calculate_signals(self, frame, symbol):
            seen.append(frame["close"].tolist())
            return super().calculate_signals(frame, symbol)

    monkeypatch.setattr(
        "backend.regime_classifier.regime_classifier.classify",
        lambda frame: {"regime": "TRENDING", "features": {}},
    )
    args = _entry_args(context, frame, strategy=ObservingStrategy())
    args["evaluate_on_incomplete_candle"] = True
    result = evaluate_production_entries(**args)
    assert seen == [[100.0]]
    assert result[0]["entry_input"]["last_input_bar"]["close"] == 100.0


def test_entry_selection_configuration_and_strategy_frames_are_isolated():
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    config = {"mutating": {"enabled": True}, "observing": {"enabled": True}}
    seen = []

    class MutatingStrategy(_MutatingStrategy):
        def calculate_signals(self, frame, symbol):
            config["observing"]["enabled"] = False
            return super().calculate_signals(frame, symbol)

    class ObservingStrategy:
        def calculate_signals(self, frame, symbol):
            seen.append(frame["close"].tolist())
            return []

    args = _entry_args(context, context.primary_frame())
    args["strategies"] = {
        "mutating": MutatingStrategy(),
        "observing": ObservingStrategy(),
    }
    args["strategy_config"] = config
    result = evaluate_production_entries(**args)
    assert seen == [[100.0]]
    assert (
        result[0]["entry_selection_config"]["strategies"]["observing"]["enabled"]
        is True
    )
    assert context.primary_frame()["close"].tolist() == [100.0]


def test_scanner_restores_live_signal_identity_and_retries_volume_recovery(monkeypatch):
    from dataclasses import replace
    from uuid import UUID

    from backend.config import config_manager
    from backend.kite_client import kite_client
    from backend.scanner import Scanner

    scanner = Scanner()
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc))
    incomplete = replace(
        context,
        primary_quality=replace(
            context.primary_quality, issues=("VOLUME_UNAVAILABLE",)
        ),
    )
    contexts = iter([incomplete, context])
    scanner.strategies = {"fixture": _MutatingStrategy()}
    scanner.playbooks = [_Playbook()]
    monkeypatch.setattr(scanner, "_market_context", lambda *args, **kw: next(contexts))
    monkeypatch.setattr(
        scanner, "_fetch_candles", lambda *args: (context.primary_frame(), False)
    )
    monkeypatch.setattr(
        config_manager, "get_strategy_config", lambda: {"fixture": {"enabled": True}}
    )
    monkeypatch.setattr(
        "backend.calibration.calibrator.get_probability", lambda *args: (None, None)
    )
    monkeypatch.setattr(
        kite_client,
        "get_instruments",
        lambda *args: [{"tradingsymbol": "TEST", "instrument_token": 1}],
    )
    assert scanner.scan_watchlist(["TEST"]) == []
    assert "TEST" not in scanner.last_scanned_candle
    result = scanner.scan_watchlist(["TEST"])
    assert len(result) == 1
    assert str(UUID(result[0]["id"])) == result[0]["id"]
    assert scanner.last_scanned_candle["TEST"] == context.primary_bar.end
