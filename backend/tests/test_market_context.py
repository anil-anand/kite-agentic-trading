"""Deterministic contracts for causal completed-candle market context."""

from datetime import timedelta

import numpy as np
import pandas as pd

from backend.market_context import (
    ContextPolicy,
    ContextQuality,
    MarketContextService,
    build_market_context,
)
from backend.tests.conftest import build_candles

SESSION_START = pd.Timestamp("2026-09-21 09:15:00", tz="Asia/Kolkata")


def _candles(count=12, *, dates=None, volumes=None):
    if dates is None:
        dates = pd.date_range(SESSION_START, periods=count, freq="5min")
    closes = np.arange(100, 100 + len(dates), dtype=float)
    return build_candles(closes, dates=dates, volumes=volumes)


def _at(minutes):
    return (SESSION_START + pd.Timedelta(minutes=minutes)).to_pydatetime()


def test_primary_context_excludes_an_incomplete_final_bar():
    frame = _candles(4)

    context = build_market_context("101", frame, _at(17))

    assert [bar.start for bar in context.primary_bars] == [
        _at(0),
        _at(5),
        _at(10),
    ]
    assert context.primary_bar.start == _at(10)
    assert context.primary_quality.status is ContextQuality.VALID


def test_normalization_sorts_and_deduplicates_identical_bars():
    frame = _candles(4)
    duplicated_and_unsorted = pd.concat(
        [
            frame.iloc[[2]],
            frame.iloc[[0]],
            frame.iloc[[1]],
            frame.iloc[[1]],
            frame.iloc[[3]],
        ],
        ignore_index=True,
    )

    context = build_market_context("101", duplicated_and_unsorted, _at(25))

    assert [bar.start for bar in context.primary_bars] == [
        _at(0),
        _at(5),
        _at(10),
        _at(15),
    ]
    assert "DUPLICATE_DEDUPLICATED" in context.primary_quality.issues
    assert "OUT_OF_ORDER_NORMALIZED" in context.primary_quality.issues
    assert context.primary_quality.status is ContextQuality.VALID


def test_conflicting_candle_revision_is_explicitly_invalid():
    frame = _candles(4)
    correction = frame.iloc[[1]].copy()
    correction.loc[:, "close"] = 999.0
    correction.loc[:, "high"] = 1_000.0
    conflicted = pd.concat([frame, correction], ignore_index=True)

    context = build_market_context("101", conflicted, _at(25))

    assert context.primary_quality.status is ContextQuality.INVALID
    assert "CONFLICTING_DUPLICATE" in context.primary_quality.issues


def test_session_gap_is_not_filled_or_interpreted_as_market_evidence():
    frame = _candles(4).drop(index=1).reset_index(drop=True)

    context = build_market_context("101", frame, _at(25))

    assert context.primary_quality.status is ContextQuality.GAP
    assert context.primary_quality.missing_bars == (_at(5),)
    assert not context.normal_decision_eligible


def test_invalid_ohlcv_is_unavailable_to_normal_decisions():
    frame = _candles(4)
    frame.loc[2, "low"] = 10_000.0

    context = build_market_context("101", frame, _at(25))

    assert context.primary_quality.status is ContextQuality.INVALID
    assert "INVALID_OHLCV" in context.primary_quality.issues
    assert not context.normal_decision_eligible


def test_missing_required_price_field_fails_closed_without_an_exception():
    frame = _candles(4).drop(columns=["close"])

    context = build_market_context("101", frame, _at(25))

    assert context.primary_quality.status is ContextQuality.INVALID
    assert "MISSING_COLUMN:close" in context.primary_quality.issues
    assert context.primary_bar is None


def test_zero_volume_makes_vwap_unknown_without_making_data_directional():
    frame = _candles(4, volumes=[0, 0, 0, 0])

    context = build_market_context("101", frame, _at(25))

    assert context.primary_quality.status is ContextQuality.VALID
    assert context.session_vwap is None
    assert context.participation["relative_volume_20"] is None


def test_successful_but_old_response_is_marked_stale():
    frame = _candles(4)

    context = build_market_context("101", frame, _at(60))

    assert context.primary_quality.status is ContextQuality.STALE
    assert not context.normal_decision_eligible
    assert context.primary_quality.age_seconds == 2_400.0


def test_higher_timeframe_is_anchored_to_0915_and_never_uses_partial_bar():
    frame = _candles(5)

    context = build_market_context("101", frame, _at(14))

    assert len(context.primary_bars) == 2
    assert context.higher_bar is None
    assert context.higher_quality.status is ContextQuality.INCOMPLETE

    complete = build_market_context("101", frame, _at(15))
    assert len(complete.primary_bars) == 3
    assert complete.higher_bar.start == _at(0)
    assert complete.higher_bar.end == _at(15)
    assert complete.higher_bar.bar_id.startswith("15m:")


def test_availability_delay_excludes_a_completed_but_not_yet_available_bar():
    frame = _candles(4)
    policy = ContextPolicy(availability_delay_seconds=60)

    context = build_market_context(
        "101", frame, _at(20), received_at=_at(20), policy=policy
    )

    assert context.primary_bar.start == _at(10)
    assert context.primary_bar.end == _at(15)


def test_known_at_swing_requires_its_right_confirmation_bars():
    frame = _candles(5)
    frame.loc[:, "high"] = [101.0, 102.0, 110.0, 103.0, 104.0]
    frame.loc[:, "low"] = [99.0, 100.0, 101.0, 100.0, 99.0]

    before_known = build_market_context("101", frame, _at(24))
    after_known = build_market_context("101", frame, _at(25))

    assert not [
        level for level in before_known.known_structure if level.kind == "SWING_HIGH"
    ]
    highs = [
        level for level in after_known.known_structure if level.kind == "SWING_HIGH"
    ]
    assert len(highs) == 1
    assert highs[0].price == 110.0
    assert highs[0].formed_at == _at(15)
    assert highs[0].known_at == _at(25)


def test_future_prefix_cannot_change_an_already_available_context():
    prefix = _candles(5)
    future = _candles(
        dates=pd.date_range(
            SESSION_START + timedelta(minutes=25), periods=4, freq="5min"
        )
    )
    decision_at = _at(25)

    prefix_context = build_market_context("101", prefix, decision_at)
    appended_context = build_market_context(
        "101", pd.concat([prefix, future], ignore_index=True), decision_at
    )

    assert appended_context.primary_bar == prefix_context.primary_bar
    assert appended_context.higher_bar == prefix_context.higher_bar
    assert appended_context.known_structure == prefix_context.known_structure
    assert appended_context.session_vwap == prefix_context.session_vwap
    assert appended_context.atr == prefix_context.atr


def test_context_exposes_causal_range_and_continuous_observations():
    frame = _candles(55)
    frame.loc[54, "high"] = 1_000.0  # the decision bar cannot rewrite its range
    context = build_market_context("101", frame, _at(275), received_at=_at(275))

    assert context.setup_range is not None
    assert context.setup_range.high < 1_000.0
    assert context.session_vwap is not None
    assert context.atr is not None
    assert context.direction_dynamics["ema_20"] is not None
    assert context.direction_dynamics["ema_50"] is not None
    assert context.participation["relative_volume_20"] is not None


def test_cached_frames_are_revision_keyed_and_not_shared_mutably():
    service = MarketContextService()
    frame = _candles(4)

    first = service.cache_frame("101", "5minute", frame)
    first.loc[:, "close"] = -1
    second = service.cache_frame("101", "5minute", frame)

    assert (second["close"] > 0).all()
    revised = frame.copy()
    revised.loc[3, "close"] = 123.45
    third = service.cache_frame("101", "5minute", revised)
    assert third.loc[3, "close"] == 123.45


def test_regime_transition_requires_distinct_completed_primary_bars(monkeypatch):
    from backend.regime_classifier import regime_classifier

    raw_regimes = iter(["TRENDING", "RANGING", "RANGING"])
    monkeypatch.setattr(
        regime_classifier,
        "classify",
        lambda frame: {"regime": next(raw_regimes), "features": {"adx": 25.0}},
    )
    service = MarketContextService(
        ContextPolicy(regime_transition_confirm_bars=2, max_primary_age_seconds=0)
    )
    frame = _candles(6)

    established = service.build("101", frame.iloc[:4], _at(20), _at(20))
    candidate = service.build("101", frame.iloc[:5], _at(25), _at(25))
    confirmed = service.build("101", frame, _at(30), _at(30))

    assert established.confirmed_regime == "TRENDING"
    assert candidate.confirmed_regime == "TRENDING"
    assert candidate.transition_candidate == "RANGING"
    assert candidate.transition_age == 1
    assert confirmed.confirmed_regime == "RANGING"
    assert confirmed.transition_candidate is None


def test_future_invalid_rows_and_gaps_cannot_change_current_quality_or_identity():
    prefix = _candles(5)
    future = _candles(dates=[SESSION_START + pd.Timedelta(minutes=100)])
    future.loc[0, "low"] = -1.0
    first = build_market_context("101", prefix, _at(25))
    appended = build_market_context(
        "101", pd.concat([prefix, future], ignore_index=True), _at(25)
    )
    assert appended.primary_quality == first.primary_quality
    assert appended.input_hash == first.input_hash
    assert appended.snapshot_id == first.snapshot_id


def test_cached_partial_bar_cannot_mature_after_its_receipt():
    context = build_market_context("101", _candles(4), _at(20), received_at=_at(17))
    assert context.primary_bar.start == _at(10)
    assert context.primary_bar.available_at == _at(17)


def test_request_cutoff_prevents_response_crossing_close_from_finalizing_bar():
    context = build_market_context(
        "101", _candles(4), _at(20), received_at=_at(20), source_as_of=_at(17)
    )
    assert context.primary_bar.start == _at(10)
    assert context.source_as_of == _at(17)


def test_delayed_payload_cannot_be_replayed_before_it_was_received():
    context = build_market_context("101", _candles(4), _at(20), received_at=_at(25))
    assert context.primary_bar is None
    assert not context.normal_decision_eligible


def test_future_revision_is_excluded_before_duplicate_quarantine():
    original = _candles(4)
    original["received_at"] = _at(20)
    correction = original.iloc[[-1]].copy()
    correction["close"] = 150.0
    correction["high"] = 151.0
    correction["received_at"] = _at(25)
    frame = pd.concat([original, correction], ignore_index=True)
    context = build_market_context("101", frame, _at(20))
    assert context.primary_quality.status is ContextQuality.VALID
    assert context.primary_bar.close == original.iloc[-1]["close"]


def test_content_revisions_change_both_primary_and_derived_higher_identity():
    original = _candles(6)
    correction = original.copy()
    correction.loc[4, "high"] += 10
    first = build_market_context("101", original, _at(30))
    second = build_market_context("101", correction, _at(30))
    assert first.primary_bars[4].bar_id != second.primary_bars[4].bar_id
    assert first.higher_bar.bar_id != second.higher_bar.bar_id
    assert first.input_hash != second.input_hash
    assert first.primary_bar.bar_id == second.primary_bar.bar_id


def test_refetch_receipt_does_not_revise_unchanged_bar_identity():
    first = build_market_context("101", _candles(6), _at(30))
    refetch = build_market_context("101", _candles(6), _at(31))
    assert first.input_bar_ids == refetch.input_bar_ids
    assert first.higher_bar.bar_id == refetch.higher_bar.bar_id
    assert first.input_hash != refetch.input_hash


def test_cache_preserves_distinct_source_receipt_versions():
    service = MarketContextService()
    first = _candles(4)
    first["received_at"] = _at(20)
    later = first.copy()
    later["received_at"] = _at(25)
    service.cache_frame("101", "5minute", first)
    cached = service.cache_frame("101", "5minute", later)
    assert (cached["received_at"] == _at(25)).all()


def test_mixed_naive_and_offset_timestamps_normalize_to_exchange_session():
    frame = _candles(4)
    frame["date"] = [
        "2026-09-21T09:15:00+05:30",
        "2026-09-21T03:50:00Z",
        "2026-09-21 09:25:00",
        _at(15),
    ]
    context = build_market_context("101", frame, _at(20))
    assert context.primary_quality.status is ContextQuality.VALID
    assert context.primary_bar.start == _at(15)
    assert context.primary_bar.start.utcoffset() == timedelta(0)


def test_off_grid_and_out_of_session_rows_fail_closed_without_exception():
    for dates, issue in [
        ([SESSION_START + pd.Timedelta(minutes=1)], "MISALIGNED_INTERVAL"),
        ([SESSION_START - pd.Timedelta(days=1)], "OUTSIDE_EXCHANGE_SESSION"),
    ]:
        context = build_market_context("101", _candles(dates=dates), _at(20))
        assert context.primary_quality.status is ContextQuality.INVALID
        assert issue in context.primary_quality.issues
        assert context.primary_bar is None


def test_missing_session_open_is_a_gap_and_cannot_claim_session_vwap():
    context = build_market_context("101", _candles(4).iloc[1:], _at(20))
    assert context.primary_quality.status is ContextQuality.GAP
    assert context.primary_quality.missing_bars == (_at(0),)
    assert context.session_vwap is None


def test_setup_range_does_not_cross_overnight_session_seam():
    previous = _candles(21)
    today = _candles(
        dates=pd.date_range(SESSION_START + timedelta(days=1), periods=2, freq="5min")
    )
    context = build_market_context(
        "101",
        pd.concat([previous, today], ignore_index=True),
        _at(10) + timedelta(days=1),
    )
    assert context.normal_decision_eligible
    assert context.setup_range is None


def test_optional_volume_does_not_hide_usable_price_structure():
    frame = _candles(55).drop(columns=["volume"])
    context = build_market_context("101", frame, _at(275))
    assert context.normal_decision_eligible
    assert "VOLUME_UNAVAILABLE" in context.primary_quality.issues
    assert context.primary_bar.volume is None
    assert context.higher_bar.volume is None
    assert context.setup_range is not None
    assert context.atr is not None
    assert context.session_vwap is None
    assert context.participation["relative_volume_20"] is None
    assert context.observation_quality["session_vwap"] is ContextQuality.UNAVAILABLE


def test_missing_one_volume_makes_cumulative_vwap_and_baseline_unknown():
    frame = _candles(25)
    frame.loc[10, "volume"] = float("nan")
    context = build_market_context("101", frame, _at(125))
    assert context.normal_decision_eligible
    assert context.session_vwap is None
    assert context.participation["relative_volume_20"] is None


def test_relative_volume_baseline_includes_known_zero_volume_bars():
    frame = _candles(21, volumes=[0] * 19 + [100, 100])
    context = build_market_context("101", frame, _at(105))
    assert context.participation["relative_volume_20"] == 20.0


def test_known_at_uses_latest_receipt_of_all_pivot_dependencies():
    frame = _candles(5)
    frame["high"] = [101.0, 102.0, 110.0, 104.0, 106.0]
    frame["received_at"] = frame["date"] + pd.Timedelta(minutes=5)
    frame.loc[2, "received_at"] = _at(30)
    context = build_market_context("101", frame, _at(30))
    high = next(
        level for level in context.known_structure if level.kind == "SWING_HIGH"
    )
    assert high.known_at == _at(30)


def test_policy_values_are_validated_without_relabeling_broker_interval():
    policy = ContextPolicy.from_config(
        {
            "primaryIntervalMinutes": 1,
            "higherIntervalMinutes": 7,
            "availabilityDelaySeconds": 30,
            "setupRangeBars": float("inf"),
            "swingConfirmationBars": 1.5,
        }
    )
    assert (policy.primary_interval_minutes, policy.higher_interval_minutes) == (5, 15)
    assert policy.availability_delay_seconds == 30
    assert policy.setup_range_bars == 20
    assert policy.swing_confirmation_bars == 2


def test_effective_policy_changes_snapshot_even_with_same_policy_version():
    first = build_market_context("101", _candles(5), _at(25))
    changed = build_market_context(
        "101", _candles(5), _at(25), policy=ContextPolicy(setup_range_bars=10)
    )
    assert first.feature_version == changed.feature_version
    assert first.snapshot_id != changed.snapshot_id


def _regime_service(monkeypatch):
    from backend.regime_classifier import regime_classifier

    # The observations below use a deterministic length classifier because
    # normalization deliberately carries only canonical data columns.
    regimes = {}
    monkeypatch.setattr(
        regime_classifier,
        "classify",
        lambda frame: {"regime": regimes.get(len(frame), "TRENDING"), "features": {}},
    )
    return MarketContextService(), regimes


def test_uncertain_bar_interrupts_regime_confirmation_episode(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes.update({5: "RANGING", 6: "UNCERTAIN", 7: "RANGING", 8: "RANGING"})
    states = [service.build("101", _candles(n), _at(n * 5)) for n in range(4, 9)]
    assert states[1].transition_age == 1
    assert states[2].transition_candidate is None
    assert states[3].confirmed_regime == "TRENDING"
    assert states[3].transition_age == 1
    assert states[4].confirmed_regime == "RANGING"


def test_regime_revisions_and_repeated_polling_do_not_count_as_new_closes(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes[5] = "RANGING"
    service.build("101", _candles(4), _at(20))
    candidate = service.build("101", _candles(5), _at(25))
    revised = _candles(5)
    revised.loc[4, "high"] += 1
    repeated = service.build("101", revised, _at(26))
    assert candidate.primary_bar.bar_id != repeated.primary_bar.bar_id
    assert repeated.confirmed_regime == "TRENDING"
    assert repeated.transition_age == 1


def test_invalid_or_skipped_bar_interrupts_regime_confirmation(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes.update({5: "RANGING", 6: "RANGING", 7: "RANGING", 8: "RANGING"})
    service.build("101", _candles(4), _at(20))
    service.build("101", _candles(5), _at(25))
    invalid = _candles(6)
    invalid.loc[5, "close"] = -1.0
    state = service.build("101", invalid, _at(30))
    assert state.confirmed_regime == "TRENDING"
    assert state.transition_candidate is None
    resumed = service.build("101", _candles(7), _at(35))
    assert resumed.confirmed_regime == "TRENDING"
    assert resumed.transition_age == 1
    # A skipped decision cannot be assumed to be qualifying evidence.
    skipped = service.build("101", _candles(9), _at(45))
    assert skipped.confirmed_regime == "TRENDING"


def test_older_asof_query_neither_leaks_future_regime_nor_rewinds_state(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes.update({5: "RANGING", 6: "RANGING"})
    service.build("101", _candles(4), _at(20))
    service.build("101", _candles(5), _at(25))
    newer = service.build("101", _candles(6), _at(30))
    assert newer.confirmed_regime == "RANGING"
    older = service.build("101", _candles(4), _at(20))
    assert older.confirmed_regime == "UNCERTAIN"
    repeated = service.build("101", _candles(6), _at(30))
    assert repeated.confirmed_regime == "RANGING"
    assert repeated.snapshot_id == newer.snapshot_id


def test_new_session_starts_a_new_regime_episode(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes[5] = "RANGING"
    service.build("101", _candles(4), _at(20))
    service.build("101", _candles(5), _at(25))
    next_day = _candles(
        dates=pd.date_range(SESSION_START + timedelta(days=1), periods=6, freq="5min")
    )
    regimes[6] = "BREAKOUT"
    context = service.build("101", next_day, _at(30) + timedelta(days=1))
    assert context.confirmed_regime == "BREAKOUT"
    assert context.transition_candidate is None


def test_future_malformed_revision_does_not_change_current_quality():
    frame = _candles(4)
    frame["received_at"] = _at(20)
    frame["available_at"] = _at(20)
    first = build_market_context("101", frame, _at(25))
    future = frame.iloc[[1]].copy()
    future["received_at"] = _at(30)
    future["available_at"] = "bad"
    appended = build_market_context(
        "101", pd.concat([frame, future], ignore_index=True), _at(25)
    )
    assert appended.primary_quality == first.primary_quality
    assert appended.snapshot_id == first.snapshot_id


def test_identical_duplicate_preserves_earliest_known_receipt_independent_of_order():
    frame = _candles(4)
    frame["received_at"] = _at(20)
    earlier = frame.iloc[[0]].copy()
    earlier["received_at"] = _at(5)
    left = build_market_context(
        "101", pd.concat([frame, earlier], ignore_index=True), _at(25)
    )
    right = build_market_context(
        "101", pd.concat([earlier, frame], ignore_index=True), _at(25)
    )
    assert left.primary_bars[0].available_at == _at(5)
    assert left.primary_bars == right.primary_bars
    assert left.input_hash == right.input_hash


def test_older_empty_query_cannot_read_or_mutate_future_regime_state(monkeypatch):
    service, regimes = _regime_service(monkeypatch)
    regimes[5] = "RANGING"
    service.build("101", _candles(4), _at(20))
    candidate = service.build("101", _candles(5), _at(25))
    older = service.build("101", None, _at(0))
    assert older.confirmed_regime == "UNCERTAIN"
    assert older.transition_candidate is None
    repeated = service.build("101", _candles(5), _at(25))
    assert repeated.transition_candidate == "RANGING"
    assert repeated.snapshot_id == candidate.snapshot_id


def test_disabling_numerical_age_limit_never_makes_yesterday_current():
    context = build_market_context(
        "101",
        _candles(4),
        _at(25) + timedelta(days=1),
        policy=ContextPolicy(max_primary_age_seconds=0, max_higher_age_seconds=0),
    )
    assert context.primary_quality.status is ContextQuality.STALE
    assert not context.normal_decision_eligible


def test_non_mapping_configuration_falls_back_to_safe_defaults():
    for values in ([1, 2], "invalid", 123):
        assert ContextPolicy.from_config(values) == ContextPolicy()


def test_first_current_session_bars_cannot_inherit_prior_session_higher_context():
    previous = _candles(3)
    today = _candles(
        dates=pd.date_range(SESSION_START + timedelta(days=1), periods=2, freq="5min")
    )
    context = build_market_context(
        "101",
        pd.concat([previous, today], ignore_index=True),
        _at(10) + timedelta(days=1),
        policy=ContextPolicy(max_higher_age_seconds=0),
    )
    assert context.normal_decision_eligible
    assert context.higher_quality.status is ContextQuality.STALE


def test_corrected_confirmation_bar_versions_structure_identity():
    frame = _candles(5)
    frame["high"] = [101.0, 102.0, 110.0, 104.0, 106.0]
    frame["received_at"] = _at(25)
    original = build_market_context("101", frame, _at(25))
    corrected = frame.copy()
    corrected.loc[4, "high"] += 0.5
    corrected.loc[4, "received_at"] = _at(30)
    later = build_market_context("101", corrected, _at(30))
    before = next(
        level for level in original.known_structure if level.kind == "SWING_HIGH"
    )
    after = next(level for level in later.known_structure if level.kind == "SWING_HIGH")
    assert before.price == after.price
    assert before.formed_at == after.formed_at
    assert before.level_id != after.level_id
    assert before.known_at == _at(25)
    assert after.known_at == _at(30)
