"""Engine-level wiring of the LLM advisory layer (issue #62, Phase 2).

Verifies the opt-in flag, the API-key gate, and that a live LLM adjustment is
applied — while a failure or a disabled flag leaves the deterministic decision
in place.
"""

import datetime

import pytest

import backend.analytics as analytics_mod
import backend.llm_client as llm_mod
import backend.trading_engine as te
from backend.config import config_manager
from backend.scanner import scanner
from backend.trading_engine import TradingEngine


class FakeKite:
    """Quotes that yield a TRENDING (applied) deterministic decision."""

    def get_quote(self, instruments):
        return {
            instr: {"last_price": 100.8, "ohlc": {"open": 100.0}}
            for instr in instruments
        }


def _fake_llm(text_or_exc):
    class FakeClient:
        def generate(self, **kwargs):
            if isinstance(text_or_exc, Exception):
                raise text_or_exc
            return text_or_exc

    return FakeClient


@pytest.fixture
def engine(tmp_path, monkeypatch):
    config_manager.config = {
        "strategies": {sid: {"enabled": True} for sid in scanner.strategies},
        "watchlist": [],
        "aiStrategyAdvisor": {"enabled": False},
    }
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    config_manager.clear_strategy_selection()
    monkeypatch.setattr(te, "kite_client", FakeKite())
    # Keep the advisor off the DB.
    monkeypatch.setattr(analytics_mod.analytics, "get_strategy_expectancy", lambda: [])
    monkeypatch.setattr(
        config_manager, "get_credentials", lambda: {"llmApiKey": "test-key"}
    )
    monkeypatch.setattr(
        config_manager,
        "get_llm_settings",
        lambda: {
            "provider": "Gemini",
            "baseUrl": "http://x",
            "model": "m",
            "openCodePlan": "zen",
        },
    )
    return TradingEngine()


NOW = datetime.datetime(2026, 9, 4, 10, 0)


def test_flag_off_uses_deterministic_and_never_calls_llm(engine, monkeypatch):
    called = {"n": 0}

    class Spy:
        def generate(self, **kwargs):
            called["n"] += 1
            return '{"enabled": ["supertrend"], "rationale": "x"}'

    monkeypatch.setattr(llm_mod, "OpenAICompatibleClient", Spy)

    rec = engine.run_strategy_selection(now=NOW)

    assert rec["source"] == "deterministic"
    assert rec["regime"] == "TRENDING"
    assert called["n"] == 0


def test_flag_on_applies_llm_adjustment(engine, monkeypatch):
    config_manager.config["aiStrategyAdvisor"]["enabled"] = True
    monkeypatch.setattr(
        llm_mod,
        "OpenAICompatibleClient",
        _fake_llm('{"enabled": ["supertrend", "ema_crossover"], "rationale": "edge"}'),
    )

    rec = engine.run_strategy_selection(now=NOW)

    assert rec["source"] == "llm_advisory"
    assert rec["enabled"] == ["supertrend", "ema_crossover"]
    assert rec["rationale"].startswith("[LLM advisory]")
    # Persisted overlay reflects the LLM decision.
    eff = config_manager.get_effective_strategy_config()
    on = {sid for sid, cfg in eff.items() if cfg["enabled"]}
    assert on == {"supertrend", "ema_crossover"}


def test_flag_on_but_no_key_falls_back(engine, monkeypatch):
    config_manager.config["aiStrategyAdvisor"]["enabled"] = True
    monkeypatch.setattr(config_manager, "get_credentials", lambda: {"llmApiKey": ""})
    called = {"n": 0}

    class Spy:
        def generate(self, **kwargs):
            called["n"] += 1
            return "{}"

    monkeypatch.setattr(llm_mod, "OpenAICompatibleClient", Spy)

    rec = engine.run_strategy_selection(now=NOW)

    assert rec["source"] == "deterministic"
    assert called["n"] == 0


def test_flag_on_llm_error_falls_back(engine, monkeypatch):
    config_manager.config["aiStrategyAdvisor"]["enabled"] = True
    monkeypatch.setattr(
        llm_mod, "OpenAICompatibleClient", _fake_llm(RuntimeError("HTTP 500"))
    )

    rec = engine.run_strategy_selection(now=NOW)

    assert rec["source"] == "deterministic"
    assert rec["llm_error"] == "HTTP 500"
    # Deterministic overlay still applied normally.
    assert rec["applied"] is True
