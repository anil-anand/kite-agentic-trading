"""Tests for the session state machine and strategy-selection wiring in the
trading engine (issue #62, Phase 1).

Covers the four scenarios called out on the ticket: late start, unavailable
inputs, restart mid-session, and manual override — plus the observation-only
guarantee that no trade is placed until the session is ACTIVE.
"""

import datetime

import pytest

import backend.trading_engine as te
from backend.config import config_manager
from backend.scanner import scanner
from backend.trading_engine import TradingEngine


class FakeKite:
    """Minimal quote source. `moves` is applied uniformly to every instrument."""

    def __init__(self, open_price=100.0, ltp=100.0, empty=False, raises=False):
        self.open_price = open_price
        self.ltp = ltp
        self.empty = empty
        self.raises = raises

    def get_quote(self, instruments):
        if self.raises:
            raise RuntimeError("network")
        if self.empty:
            return {}
        return {
            instr: {"last_price": self.ltp, "ohlc": {"open": self.open_price}}
            for instr in instruments
        }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Deterministic baseline: every registered strategy permitted/enabled.
    config_manager.config = {
        "strategies": {sid: {"enabled": True} for sid in scanner.strategies},
        "watchlist": [],
    }
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    config_manager.clear_strategy_selection()
    eng = TradingEngine()
    return eng


def _at(hour, minute):
    return datetime.datetime(2026, 9, 4, hour, minute)


class TestObservationWindow:
    def test_before_deadline_is_observing_and_blocks_trades(self, engine, monkeypatch):
        monkeypatch.setattr(engine, "_now", lambda: _at(9, 20))  # inside window
        engine.update_session_state()
        assert engine.session_state == "observing"
        assert engine._auto_execute_allowed() is False
        # No decision written during observation -> baseline untouched.
        assert config_manager.get_strategy_selection() == {}

    def test_auto_execute_allowed_only_when_active(self, engine):
        for state, allowed in [
            ("warmup", False),
            ("observing", False),
            ("active", True),
            ("halted", False),
        ]:
            engine.session_state = state
            assert engine._auto_execute_allowed() is allowed


class TestLateStart:
    def test_past_deadline_runs_selection_and_goes_active(self, engine, monkeypatch):
        monkeypatch.setattr(te, "kite_client", FakeKite(open_price=100, ltp=100.8))
        monkeypatch.setattr(engine, "_now", lambda: _at(10, 0))  # well past 09:30

        engine.update_session_state()

        assert engine.session_state == "active"
        assert engine._auto_execute_allowed() is True
        rec = config_manager.get_strategy_selection()
        assert rec["applied"] is True
        assert rec["regime"] == "TRENDING"
        assert rec["session_date"] == "2026-09-04"
        # Overlay narrows the baseline to the trend-suited set.
        eff = config_manager.get_effective_strategy_config()
        assert eff["supertrend"]["enabled"] is True
        assert eff["rsi_reversal"]["enabled"] is False  # mean-reversion off in trend


class TestUnavailableInputs:
    def test_no_quotes_leaves_baseline_unchanged(self, engine, monkeypatch):
        monkeypatch.setattr(te, "kite_client", FakeKite(empty=True))
        monkeypatch.setattr(engine, "_now", lambda: _at(10, 0))

        engine.update_session_state()

        # Observation is over regardless, but the decision is a conservative
        # no-op: UNKNOWN, not applied, baseline fully intact.
        assert engine.session_state == "active"
        rec = config_manager.get_strategy_selection()
        assert rec["regime"] == "UNKNOWN"
        assert rec["applied"] is False
        eff = config_manager.get_effective_strategy_config()
        assert all(cfg["enabled"] for cfg in eff.values())  # nothing disabled

    def test_quote_error_is_swallowed_as_unknown(self, engine, monkeypatch):
        monkeypatch.setattr(te, "kite_client", FakeKite(raises=True))
        monkeypatch.setattr(engine, "_now", lambda: _at(10, 0))

        rec = engine.run_strategy_selection(now=_at(10, 0))

        assert rec["regime"] == "UNKNOWN"
        assert rec["applied"] is False


class TestRestartMidSession:
    def test_existing_decision_is_not_recomputed(self, engine, monkeypatch):
        # A decision already exists for today (as if made before the restart).
        config_manager.save_strategy_selection(
            {
                "regime": "TRENDING",
                "applied": True,
                "enabled": ["supertrend", "ema_crossover"],
                "disabled": [],
                "session_date": "2026-09-04",
                "source": "deterministic",
            }
        )
        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            raise AssertionError("selection must not be recomputed on restart")

        monkeypatch.setattr(engine, "run_strategy_selection", _spy)
        monkeypatch.setattr(engine, "_now", lambda: _at(11, 0))

        engine.update_session_state()

        assert calls["n"] == 0
        assert engine.session_state == "active"
        # Re-hydrated from the persisted record.
        assert engine.selection_record["enabled"] == ["supertrend", "ema_crossover"]

    def test_stale_decision_from_a_previous_day_is_ignored(self, engine):
        config_manager.save_strategy_selection(
            {"applied": True, "enabled": ["supertrend"], "session_date": "2020-01-01"}
        )
        # A previous day's overlay must not affect today's effective config.
        eff = config_manager.get_effective_strategy_config()
        assert all(cfg["enabled"] for cfg in eff.values())


class TestManualOverride:
    def test_override_is_an_overlay_that_preserves_baseline(self, engine):
        rec = engine.set_strategy_override(["supertrend", "ema_crossover"])

        assert rec["applied"] is True
        assert rec["source"] == "manual"
        assert set(rec["enabled"]) == {"supertrend", "ema_crossover"}

        eff = config_manager.get_effective_strategy_config()
        on = {sid for sid, cfg in eff.items() if cfg["enabled"]}
        assert on == {"supertrend", "ema_crossover"}

        # Baseline (the user's permitted set) is untouched — still all enabled.
        baseline = config_manager.get_strategy_config()
        assert all(cfg["enabled"] for cfg in baseline.values())

    def test_override_ignores_ids_the_user_does_not_permit(self, engine):
        # User disables one strategy at the baseline level.
        config_manager.config["strategies"]["supertrend"]["enabled"] = False

        rec = engine.set_strategy_override(["supertrend", "ema_crossover"])

        # supertrend isn't permitted, so the override can't turn it on.
        assert "supertrend" not in rec["enabled"]
        assert rec["enabled"] == ["ema_crossover"]

    def test_reevaluate_forces_a_fresh_decision(self, engine, monkeypatch):
        monkeypatch.setattr(te, "kite_client", FakeKite(open_price=100, ltp=100.8))
        engine.session_state = "observing"

        rec = engine.reevaluate_strategies()

        assert rec["regime"] == "TRENDING"
        assert engine.session_state == "active"
