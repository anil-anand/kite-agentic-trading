from copy import deepcopy
from datetime import timedelta

from backend.backtesting.reference_diagnostics import derive_entry_boundary_references
from backend.replay import serialize_replay_artifact
from backend.tests.test_research_study import END, SYMBOL, case, data


def report():
    frame = data()[SYMBOL].iloc[:-1]
    return serialize_replay_artifact(
        {
            "artifacts": {
                "inputs": {"payload": {"case": case(), "test_end": END}},
                "data": {"payload": {SYMBOL: frame.to_dict("records")}},
                "policies": {"payload": {"context": {"primary_interval_minutes": 5}}},
            }
        }
    )


def test_two_consecutive_closed_bars_confirm_frozen_entry_boundary():
    result = derive_entry_boundary_references(report(), {})
    assert len(result) == 1
    assert result[0]["invalidated_at"] == "2026-09-21T04:30:00+00:00"
    assert result[0]["threshold_price"] == 99.3
    assert result[0]["assessed_no_invalidation"] is False


def test_temporary_wick_and_single_adverse_close_do_not_confirm():
    value = report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    bars[8]["close"] = 100
    result = derive_entry_boundary_references(value, {})
    assert result[0]["invalidated_at"] is None
    assert result[0]["assessed_no_invalidation"] is True


def test_missing_interval_censors_reference_instead_of_imputing_health():
    value = report()
    value["artifacts"]["data"]["payload"][SYMBOL].pop(8)
    assert derive_entry_boundary_references(value, {}) == []


def test_future_pnl_or_candidate_decisions_do_not_change_reference():
    value = report()
    baseline = derive_entry_boundary_references(value, {})
    changed = deepcopy(value)
    changed["candidate"] = {"trades": [{"net_pnl": -1000000}]}
    changed["control"] = {"trades": [{"net_pnl": 1000000}]}
    assert derive_entry_boundary_references(changed, {}) == baseline


def test_post_cutoff_bars_cannot_complete_reference_confirmation():
    value = report()
    from datetime import datetime

    at = datetime.fromisoformat(
        value["artifacts"]["inputs"]["payload"]["case"]["checkpoint_at"]
    )
    value["artifacts"]["inputs"]["payload"]["test_end"] = (
        at + timedelta(minutes=10)
    ).isoformat()
    result = derive_entry_boundary_references(value, {})
    assert result[0]["invalidated_at"] is None
    assert (
        result[0]["observed_through"]
        < value["artifacts"]["inputs"]["payload"]["test_end"]
    )


def test_reference_uses_the_frozen_primary_interval_instead_of_five_minutes():
    from datetime import datetime

    value = report()
    start = datetime.fromisoformat(
        value["artifacts"]["inputs"]["payload"]["case"]["checkpoint_at"]
    )
    value["artifacts"]["policies"]["payload"]["context"]["primary_interval_minutes"] = (
        10
    )
    value["artifacts"]["data"]["payload"][SYMBOL] = [
        {"date": (start + timedelta(minutes=10 * index)).isoformat(), "close": 98}
        for index in range(3)
    ]
    result = derive_entry_boundary_references(value, {})
    assert result[0]["invalidated_at"] == (start + timedelta(minutes=20)).isoformat()
    assert result[0]["primary_interval_minutes"] == 10


def test_delayed_first_bar_cannot_be_known_before_confirmation():
    value = report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    bars[7]["available_at"] = "2026-09-21T04:40:00+00:00"
    bars[9]["close"] = 98
    result = derive_entry_boundary_references(value, {})
    # The first two breaches were not both visible at 04:30. The next two
    # consecutive bars establish the first knowable reference at 04:35.
    assert result[0]["invalidated_at"] == "2026-09-21T04:35:00+00:00"


def test_late_old_breach_cannot_invalidate_structure_already_reclaimed():
    value = report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    bars[7]["available_at"] = "2026-09-21T04:40:00+00:00"
    result = derive_entry_boundary_references(value, {})
    assert result[0]["invalidated_at"] is None


def test_receipt_time_cannot_extend_observed_price_coverage():
    value = report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    for bar in bars:
        bar["close"] = 101
    bars[-1]["received_at"] = "2026-09-21T06:00:00+00:00"
    result = derive_entry_boundary_references(value, {})
    assert result[0]["observed_through"] == "2026-09-21T04:40:00+00:00"


def test_missing_frozen_interval_and_duplicate_revisions_are_rejected():
    import pytest

    value = report()
    value["artifacts"].pop("policies")
    with pytest.raises(ValueError, match="frozen primary"):
        derive_entry_boundary_references(value, {})
    value = report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    bars.append(deepcopy(bars[8]))
    with pytest.raises(ValueError, match="duplicate reference"):
        derive_entry_boundary_references(value, {})


def test_unavailable_frozen_volatility_cannot_impute_a_structural_reference():
    value = report()
    thesis = value["artifacts"]["inputs"]["payload"]["case"]["positions"][0]["thesis"]
    thesis["causal_anchors"]["volatility"]["quality"] = "STALE"
    assert derive_entry_boundary_references(value, {}) == []


def swing_report(direction="BUY"):
    value = report()
    thesis = value["artifacts"]["inputs"]["payload"]["case"]["positions"][0]["thesis"]
    thesis["direction"] = direction
    thesis["playbook"] = "Trend Pullback"
    thesis["management_profile"]["name"] = "trend_continuation"
    thesis["management_profile"]["values"]["entry_boundary"] = {
        "kind": "SWING_LOW" if direction == "BUY" else "SWING_HIGH",
        "price": 99.5 if direction == "BUY" else 100.5,
        "level_id": "frozen-entry-swing",
        "formed_at": "2026-09-20T04:10:00+00:00",
        "known_at": "2026-09-20T04:20:00+00:00",
        "source_bar_ids": ["confirmed-left", "pivot", "confirmed-right"],
    }
    thesis["causal_anchors"]["volatility"]["known_at"] = "2026-09-20T04:20:00+00:00"
    if direction == "SELL":
        for bar in value["artifacts"]["data"]["payload"][SYMBOL]:
            bar["close"] = 200 - bar["close"]
    return value


def test_version_two_references_long_and_short_frozen_swings_in_entry_atr_units():
    from backend.backtesting.reference_diagnostics import EntryBoundaryReferencePolicy

    assert EntryBoundaryReferencePolicy().policy_version.endswith("-v2")
    for direction, threshold in (("BUY", 99.3), ("SELL", 100.7)):
        value = swing_report(direction)
        result = derive_entry_boundary_references(value, {})
        assert len(result) == 1
        assert result[0]["threshold_price"] == threshold
        assert result[0]["invalidated_at"] == "2026-09-21T04:30:00+00:00"
        assert result[0]["buffer_price_distance"] == 0.2
        assert (
            result[0]["reference_policy"]["buffer_units"] == "FROZEN_ENTRY_ATR_MULTIPLE"
        )
        # Old registered range-only contracts retain their original coverage.
        assert (
            derive_entry_boundary_references(
                value, {"policy_version": "static-entry-boundary-reference-v1"}
            )
            == []
        )


def test_invalid_swing_never_falls_back_to_an_unrelated_valid_entry_range():
    value = swing_report()
    thesis = value["artifacts"]["inputs"]["payload"]["case"]["positions"][0]["thesis"]
    assert thesis["causal_anchors"]["setup_range"]["high"] > 0
    thesis["management_profile"]["values"]["entry_boundary"]["source_bar_ids"] = []
    assert derive_entry_boundary_references(value, {}) == []


def test_swing_and_frozen_atr_must_be_known_at_thesis_creation():
    import pytest

    for defect in ("future_swing", "before_formation", "future_atr", "wrong_side"):
        value = swing_report()
        thesis = value["artifacts"]["inputs"]["payload"]["case"]["positions"][0][
            "thesis"
        ]
        boundary = thesis["management_profile"]["values"]["entry_boundary"]
        if defect == "future_swing":
            boundary["known_at"] = "2026-09-20T04:30:00+00:00"
        elif defect == "before_formation":
            boundary["formed_at"] = "2026-09-20T04:30:00+00:00"
        elif defect == "future_atr":
            thesis["causal_anchors"]["volatility"]["known_at"] = (
                "2026-09-20T04:30:00+00:00"
            )
        else:
            boundary["kind"] = "SWING_HIGH"
        with pytest.raises(ValueError, match="reference"):
            derive_entry_boundary_references(value, {})


def test_frozen_swing_reference_keeps_receipt_causality_and_gap_censoring():
    value = swing_report()
    bars = value["artifacts"]["data"]["payload"][SYMBOL]
    bars[7]["received_at"] = "2026-09-21T04:40:00+00:00"
    assert derive_entry_boundary_references(value, {})[0]["invalidated_at"] is None
    bars.pop(8)
    assert derive_entry_boundary_references(value, {}) == []


def test_later_structural_levels_cannot_rewrite_frozen_swing_reference():
    value = swing_report()
    baseline = derive_entry_boundary_references(value, {})
    thesis = value["artifacts"]["inputs"]["payload"]["case"]["positions"][0]["thesis"]
    thesis["causal_anchors"]["known_structure"] = [
        {"kind": "SWING_LOW", "price": 500, "known_at": "2026-09-21T04:40:00+00:00"}
    ]
    assert derive_entry_boundary_references(value, {}) == baseline
