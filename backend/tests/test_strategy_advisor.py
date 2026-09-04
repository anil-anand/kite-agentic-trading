"""Tests for the LLM advisory layer (issue #62, Phase 2).

Pure prompt/parse tests plus advise() orchestration with an injected generate
function — no network. The invariant under test throughout: the advisor may
confirm or adjust within the permitted set, but any problem falls back to the
deterministic decision unchanged.
"""

import pytest

from backend.strategy_advisor import advise, build_prompt, parse_response


def _deterministic():
    return {
        "regime": "TRENDING",
        "applied": True,
        "source": "deterministic",
        "enabled": ["supertrend", "ema_crossover", "donchian_breakout"],
        "disabled": ["rsi_reversal"],
        "rationale": "trend day",
        "inputs_used": {"net_move_pct": 0.8},
    }


AVAILABLE = ["supertrend", "ema_crossover", "donchian_breakout", "rsi_reversal"]


class TestBuildPrompt:
    def test_lists_available_with_family_and_history(self):
        exp = [
            {
                "strategy": "supertrend",
                "total_trades": 20,
                "win_rate_pct": 60,
                "profit_factor": 1.8,
                "avg_r_multiple": 0.5,
            }
        ]
        prompt = build_prompt({"net_move_pct": 0.8}, _deterministic(), AVAILABLE, exp)
        assert "supertrend" in prompt
        assert "family: trend_momentum" in prompt
        assert "win%=60" in prompt  # history injected
        assert "no history yet" in prompt  # for strategies without stats
        assert "TRENDING" in prompt
        assert "JSON" in prompt

    def test_pure_without_expectancy(self):
        prompt = build_prompt({}, _deterministic(), AVAILABLE)
        assert "no history yet" in prompt

    def test_includes_news_when_provided(self):
        prompt = build_prompt(
            {}, _deterministic(), AVAILABLE, None, ["RBI holds rates", "Infy cut"]
        )
        assert "Market news headlines" in prompt
        assert "RBI holds rates" in prompt

    def test_no_news_section_when_absent(self):
        assert "Market news headlines" not in build_prompt(
            {}, _deterministic(), AVAILABLE
        )


class TestParseResponse:
    def test_valid_object(self):
        out = parse_response(
            '{"enabled": ["supertrend", "ema_crossover"], "rationale": "edge"}',
            AVAILABLE,
        )
        assert out["enabled"] == ["supertrend", "ema_crossover"]
        assert out["rationale"] == "edge"

    def test_extracts_json_embedded_in_prose(self):
        out = parse_response(
            'Sure!\n{"enabled": ["supertrend"], "rationale": "x"}\nHope that helps',
            AVAILABLE,
        )
        assert out["enabled"] == ["supertrend"]

    def test_drops_unpermitted_ids(self):
        out = parse_response(
            '{"enabled": ["supertrend", "not_real"], "rationale": "x"}', AVAILABLE
        )
        assert out["enabled"] == ["supertrend"]

    def test_dedupes_preserving_order(self):
        out = parse_response(
            '{"enabled": ["ema_crossover", "supertrend", "ema_crossover"], "rationale": "x"}',
            AVAILABLE,
        )
        assert out["enabled"] == ["ema_crossover", "supertrend"]

    def test_defaults_rationale_when_missing(self):
        out = parse_response('{"enabled": ["supertrend"]}', AVAILABLE)
        assert out["rationale"]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "no json here",
            '{"enabled": []}',  # empty
            '{"enabled": "supertrend"}',  # not a list
            '{"enabled": ["not_real", "also_fake"]}',  # none permitted
            '{"rationale": "forgot enabled"}',
            "{bad json",
        ],
    )
    def test_invalid_inputs_raise(self, text):
        with pytest.raises(ValueError):
            parse_response(text, AVAILABLE)


class TestAdvise:
    def test_confirms_and_adjusts_within_permitted(self):
        out = advise(
            {},
            _deterministic(),
            AVAILABLE,
            lambda p: (
                '{"enabled": ["supertrend", "ema_crossover"], "rationale": "hist edge"}'
            ),
        )
        assert out["source"] == "llm_advisory"
        assert out["enabled"] == ["supertrend", "ema_crossover"]
        assert set(out["disabled"]) == {"donchian_breakout", "rsi_reversal"}
        assert out["rationale"].startswith("[LLM advisory]")
        # Deterministic recommendation preserved for the audit trail.
        assert out["deterministic"]["enabled"] == _deterministic()["enabled"]

    def test_malformed_response_falls_back(self):
        out = advise({}, _deterministic(), AVAILABLE, lambda p: "cannot help")
        assert out["source"] == "deterministic"
        assert out["enabled"] == _deterministic()["enabled"]
        assert "llm_error" in out

    def test_unknown_ids_fall_back(self):
        out = advise(
            {},
            _deterministic(),
            AVAILABLE,
            lambda p: '{"enabled": ["nope"], "rationale": "x"}',
        )
        assert out["source"] == "deterministic"
        assert out["enabled"] == _deterministic()["enabled"]

    def test_api_error_falls_back(self):
        def boom(_p):
            raise RuntimeError("HTTP 429")

        out = advise({}, _deterministic(), AVAILABLE, boom)
        assert out["source"] == "deterministic"
        assert out["llm_error"] == "HTTP 429"

    def test_does_not_run_when_deterministic_not_applied(self):
        det = {"applied": False, "regime": "UNKNOWN"}
        calls = {"n": 0}

        def gen(_p):
            calls["n"] += 1
            return '{"enabled": ["supertrend"], "rationale": "x"}'

        out = advise({}, det, AVAILABLE, gen)
        assert out == det
        assert calls["n"] == 0  # LLM not even called
