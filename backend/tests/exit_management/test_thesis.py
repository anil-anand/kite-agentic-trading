import pytest

from backend.exit_management.thesis import (
    EntryThesis,
    ThesisBindingStatus,
    bind_terminal_fill,
    calculate_provisional_risk,
    capture_entry_thesis,
)


def _signal():
    return {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "MIS",
        "direction": "BUY",
        "strategy": "Breakout",
        "playbook": "Breakout",
        "setup_variant": "breakout_with_trend_confirmation",
        "management_profile": "breakout_follow_through",
        "entryPrice": 100.0,
        "stopLoss": 95.0,
        "target": 110.0,
        "reasoning": "Breakout: BUY breakout detected with trend confirmation",
        "selected_evidence": [
            {
                "strategy_id": "donchian_breakout",
                "family": "breakout",
                "direction": "BUY",
                "signal_score": 80,
                "levels": {"range_high": 99.5},
            },
            {
                "strategy_id": "ema_crossover",
                "family": "trend",
                "direction": "BUY",
                "signal_score": 70,
            },
        ],
        "indicators": {"atr": 2.0, "vwap": 98.5},
        "regime": "BREAKOUT",
        "market_context": {
            "primary_quality": "VALID",
            "decision_event_time": "2026-09-20T04:25:00+00:00",
            "policy": {"policyVersion": "market-context-v1"},
            "primaryBar": {
                "start": "2026-09-20T04:20:00+00:00",
                "end": "2026-09-20T04:25:00+00:00",
                "available_at": "2026-09-20T04:25:00+00:00",
                "close": 100.0,
            },
            "higherBar": {"end": "2026-09-20T04:15:00+00:00"},
            "sourceAsOf": "2026-09-20T04:25:00+00:00",
            "setupRange": {
                "high": 99.5,
                "low": 96.0,
                "start": "2026-09-20T04:00:00+00:00",
                "end": "2026-09-20T04:20:00+00:00",
                "known_at": "2026-09-20T04:20:00+00:00",
                "source_bar_ids": ["range-bar-1"],
            },
            "knownSwings": [],
            "vwap": 98.5,
        },
    }


def _config():
    return {
        "exitManagement": {"policyVersion": "exit-thesis-state-v1"},
        "risk": {"maxDailyLoss": 2000},
        "strategies": {"ema_crossover": {"enabled": True}},
        "marketContext": {"policyVersion": "market-context-v1"},
    }


def _draft():
    return capture_entry_thesis(
        _signal(),
        position_key="LIVE:acct-1:NSE:1:RELIANCE:MIS:epoch-1",
        trade_id="trade-1",
        position_epoch="epoch-1",
        instrument_id="1",
        effective_config=_config(),
        created_at="2026-09-20T04:25:01+00:00",
    )


def test_entry_thesis_keeps_exact_pre_submission_evidence_and_context():
    signal = _signal()
    thesis = capture_entry_thesis(
        signal,
        position_key="LIVE:acct-1:NSE:1:RELIANCE:MIS:epoch-1",
        trade_id="trade-1",
        position_epoch="epoch-1",
        instrument_id="1",
        effective_config=_config(),
        created_at="2026-09-20T04:25:01+00:00",
    )

    # A later scanner result cannot rewrite the original premise.
    signal["selected_evidence"][0]["levels"]["range_high"] = 120.0
    signal["market_context"]["setupRange"]["high"] = 120.0
    signal["indicators"]["atr"] = 9.0

    saved = thesis.to_dict()
    assert saved["selected_evidence"][0]["payload"]["levels"]["range_high"] == 99.5
    assert saved["causal_anchors"]["setup_range"]["high"] == 99.5
    assert thesis.input_reference.primary_bar_id == "2026-09-20T04:25:00+00:00"
    assert thesis.binding_status is ThesisBindingStatus.DRAFT
    assert thesis.policy_snapshot.values["risk"]["maxDailyLoss"] == 2000


def test_terminal_fill_pins_immutable_r_and_partial_r_is_explicitly_provisional():
    thesis = _draft()
    provisional = calculate_provisional_risk(
        thesis, fill_price=101.0, filled_quantity=4
    )
    assert provisional.risk_per_share == 6.0
    assert provisional.risk_budget == 24.0

    bound = bind_terminal_fill(
        thesis,
        entry_vwap=101.0,
        filled_quantity=10,
        terminal_at="2026-09-20T04:26:00+00:00",
        source_fill_ids=("fill-1", "fill-2"),
    )
    assert bound.binding_status is ThesisBindingStatus.BOUND
    assert bound.revision == thesis.revision + 1
    assert bound.fill_binding.initial_r_per_share == 6.0
    assert bound.fill_binding.initial_risk_budget == 60.0
    assert (
        bind_terminal_fill(
            bound,
            entry_vwap=101.0,
            filled_quantity=10,
            terminal_at="2026-09-20T04:26:00+00:00",
        )
        is bound
    )
    with pytest.raises(ValueError, match="immutable"):
        bind_terminal_fill(
            bound,
            entry_vwap=102.0,
            filled_quantity=10,
            terminal_at="2026-09-20T04:26:00+00:00",
        )


def test_fill_at_or_beyond_original_stop_cannot_be_hidden_by_a_wider_risk():
    thesis = _draft()
    with pytest.raises(ValueError, match="at or beyond"):
        calculate_provisional_risk(thesis, fill_price=95.0, filled_quantity=1)


def _capture(signal):
    return capture_entry_thesis(
        signal,
        position_key="LIVE:account:NSE:1:MIS:epoch",
        trade_id="trade-1",
        position_epoch="epoch",
        instrument_id="1",
        effective_config=_config(),
        created_at="2026-09-20T04:29:00+00:00",
    )


def test_confirmation_delay_preserves_original_scan_time_and_profile():
    thesis = _capture(_signal())
    assert thesis.created_at == "2026-09-20T04:29:00+00:00"
    assert thesis.input_reference.decision_at == "2026-09-20T04:25:00+00:00"
    assert thesis.management_profile.name == "breakout_follow_through"
    assert thesis.management_profile.values["entry_boundary"]["high"] == 99.5
    assert EntryThesis.from_dict(thesis.to_dict()).to_dict() == thesis.to_dict()


@pytest.mark.parametrize("field", ["known_at", "end"])
def test_future_or_trigger_contaminated_range_cannot_become_entry_premise(field):
    signal = _signal()
    signal["market_context"]["setupRange"][field] = "2026-09-20T04:30:00+00:00"
    thesis = _capture(signal)
    assert thesis.management_profile.name == "unknown_legacy_bounded"
    assert thesis.causal_anchors["setup_range"] is None
    # The rejected original source is retained for diagnosis, not silently lost.
    assert thesis.input_reference.snapshot["setupRange"][field].endswith(
        "04:30:00+00:00"
    )


@pytest.mark.parametrize("quality", ["STALE", "GAP", "UNAVAILABLE", None])
def test_unusable_context_cannot_activate_structural_management(quality):
    signal = _signal()
    signal["market_context"]["primary_quality"] = quality
    assert _capture(signal).management_profile.name == "unknown_legacy_bounded"


def test_breakout_inside_unrelated_context_range_remains_bounded():
    signal = _signal()
    signal["market_context"]["setupRange"]["high"] = 105
    assert _capture(signal).management_profile.name == "unknown_legacy_bounded"


def test_raw_scanner_dump_is_not_fabricated_selected_confirmation():
    signal = _signal()
    signal["raw_signals"] = signal.pop("selected_evidence")
    thesis = _capture(signal)
    assert thesis.selected_evidence == ()
    assert len(thesis.selection_inputs) == 2


def test_unknown_decision_time_is_not_relabelled_as_order_submission():
    signal = _signal()
    signal["market_context"].pop("decision_event_time")
    thesis = _capture(signal)
    assert thesis.input_reference.decision_at is None
    assert thesis.management_profile.name == "unknown_legacy_bounded"
    assert thesis.entry_selection_config is None


def test_future_snapshot_cannot_be_used_at_an_earlier_entry_submission():
    signal = _signal()
    signal["market_context"]["decision_event_time"] = "2026-09-20T04:30:00+00:00"
    assert _capture(signal).management_profile.name == "unknown_legacy_bounded"


def test_bound_thesis_round_trip_rejects_tampered_original_r():
    bound = bind_terminal_fill(
        _draft(),
        entry_vwap=101.0,
        filled_quantity=10,
        terminal_at="2026-09-20T04:26:00+00:00",
        source_fill_ids=("fill-1",),
    )
    assert EntryThesis.from_dict(bound.to_dict()).to_dict() == bound.to_dict()
    invalid = bound.to_dict()
    invalid["fill_binding"]["initial_r_per_share"] = 3.0
    invalid["fill_binding"]["initial_risk_budget"] = 30.0
    with pytest.raises(ValueError, match="original stop"):
        EntryThesis.from_dict(invalid)


def test_equal_vwap_and_quantity_cannot_hide_a_different_initial_fill_allocation():
    bound = bind_terminal_fill(
        _draft(),
        entry_vwap=101.0,
        filled_quantity=10,
        terminal_at="2026-09-20T04:26:00+00:00",
        source_fill_ids=("fill-1",),
    )
    with pytest.raises(ValueError, match="identities are immutable"):
        bind_terminal_fill(
            bound,
            entry_vwap=101.0,
            filled_quantity=10,
            terminal_at="2026-09-20T04:26:00+00:00",
            source_fill_ids=("fill-2",),
        )


def test_restored_policy_values_must_match_the_pinned_hash():
    saved = _draft().to_dict()
    saved["policy_snapshot"]["values"]["risk"]["maxDailyLoss"] = 999999
    with pytest.raises(ValueError, match="pinned config hash"):
        EntryThesis.from_dict(saved)


def test_trend_oscillator_coincidence_does_not_claim_a_confirmed_pullback():
    from backend.playbooks.trend_pullback import TrendPullbackPlaybook

    trend = _signal()["selected_evidence"][1]
    trend.update(entryPrice=100, stopLoss=95, target=110, tradingsymbol="RELIANCE")
    oscillator = {
        "strategy_id": "oscillator_evidence",
        "family": "mean_reversion",
        "direction": "BUY",
        "signal_score": 70,
        "indicators": {"rsi": 40},
    }
    opposing = {**trend, "direction": "SELL", "signal_score": 10}
    decision = TrendPullbackPlaybook().evaluate_entry(
        [trend, oscillator, opposing], {"regime": "TRENDING"}
    )
    assert decision["setup_variant"] == "trend_with_mean_reversion_signal"
    assert decision["signal_score"] == 80  # Existing selection arithmetic unchanged.
    assert len(decision["selection_inputs"]) == 3
    oscillator["indicators"]["rsi"] = 99
    assert decision["selected_evidence"][1]["indicators"]["rsi"] == 40
    thesis = _capture(decision)
    assert thesis.management_profile.name == "unknown_legacy_bounded"
    assert {item.dependency_group for item in thesis.selected_evidence} == {
        "price_dynamics"
    }
