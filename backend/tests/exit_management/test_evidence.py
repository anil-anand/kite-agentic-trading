from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.exit_management.evidence import (
    EvidenceDirection,
    EvidenceFamily,
    EvidenceObservation,
    EvidenceReport,
    EvidenceSeverity,
    build_evidence,
    collapse_dependency_groups,
    evidence_report,
    favorable_structure,
    has_independent_corroborator,
)
from backend.exit_management.models import ManagementProfileSnapshot, ManagementState
from backend.exit_management.profiles import (
    ManagementProfile,
    ManagementProfileName,
    default_profiles,
    resolve_profile,
)
from backend.market_context import ContextQuality, KnownLevel, SourceQuality

from .test_engine import _bar, _context, _thesis


def _observation(identifier, family, group, direction, severity):
    return EvidenceObservation(
        observation_id=identifier,
        family=family,
        dependency_group=group,
        direction=direction,
        severity=severity,
        predicate=identifier,
        known_at=datetime(2026, 9, 21, 4, 25, tzinfo=timezone.utc).isoformat(),
    )


def test_correlated_oscillators_are_one_dynamics_observation_not_three_votes():
    observations = [
        _observation(
            "rsi",
            EvidenceFamily.DYNAMICS,
            "oscillator_cluster",
            EvidenceDirection.OPPOSING,
            EvidenceSeverity.MATERIAL,
        ),
        _observation(
            "stochastic",
            EvidenceFamily.DYNAMICS,
            "oscillator_cluster",
            EvidenceDirection.OPPOSING,
            EvidenceSeverity.MATERIAL,
        ),
        _observation(
            "stoch_rsi",
            EvidenceFamily.DYNAMICS,
            "oscillator_cluster",
            EvidenceDirection.OPPOSING,
            EvidenceSeverity.MATERIAL,
        ),
    ]

    collapsed = collapse_dependency_groups(observations)

    assert len(collapsed) == 1
    assert collapsed[0].dependency_group == "oscillator_cluster"


def test_vwap_aliases_cannot_satisfy_multi_family_confirmation():
    report = evidence_report(
        (
            _observation(
                "price_failure",
                EvidenceFamily.STRUCTURE,
                "entry_boundary",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
            _observation(
                "vwap_structure_alias",
                EvidenceFamily.VALUE,
                "entry_boundary",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
        )
    )

    assert not has_independent_corroborator(report)


def test_independent_participation_can_corroborate_price_led_failure():
    report = evidence_report(
        (
            _observation(
                "price_failure",
                EvidenceFamily.STRUCTURE,
                "lost_favorable_swing",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
            _observation(
                "volume_displacement",
                EvidenceFamily.PARTICIPATION,
                "adverse_displacement_volume",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
        )
    )

    assert has_independent_corroborator(report)


def test_unknown_observation_is_not_a_hidden_negative_vote():
    report = evidence_report(
        (
            _observation(
                "missing-volume",
                EvidenceFamily.PARTICIPATION,
                "adverse_displacement_volume",
                EvidenceDirection.UNKNOWN,
                EvidenceSeverity.UNKNOWN,
            ),
        )
    )

    assert not report.opposing
    assert not has_independent_corroborator(report)


def _frozen_thesis(direction="BUY", profile="breakout_follow_through", **values):
    thesis = _thesis()
    boundary = {"high": 99.5, "low": 96.0}
    if direction == "SELL":
        boundary = {"high": 104.0, "low": 100.5}
    return replace(
        thesis,
        direction=direction,
        initial_stop=95.0 if direction == "BUY" else 105.0,
        objective=110.0 if direction == "BUY" else 90.0,
        causal_anchors={**thesis.causal_anchors, "volatility": {"atr": 2.0}},
        management_profile=ManagementProfileSnapshot(
            name=profile,
            version="management-profiles-v1",
            structure_status="VALID",
            values={"entry_boundary": boundary, **values},
        ),
    )


@pytest.mark.parametrize("direction,close", [("BUY", 99.1), ("SELL", 100.9)])
def test_entry_failure_buffer_is_frozen_across_volatility_shocks(direction, close):
    thesis = _frozen_thesis(direction)
    profile = resolve_profile(thesis.management_profile)
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), close)
    for current_atr in (0.01, 2.0, 100.0, None):
        report = build_evidence(thesis, replace(context, atr=current_atr), profile)
        assert report.predicates["buffer"] == 0.2
        assert report.predicates["entry_boundary_failure"] is True


def test_missing_entry_atr_never_borrows_later_volatility_to_invent_a_boundary():
    thesis = replace(_frozen_thesis(), causal_anchors={"volatility": {"atr": None}})
    context = _context(datetime(2026, 9, 21, 4, 20, tzinfo=timezone.utc), 98.0)
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["entry_boundary_failure"] is None
    assert report.predicates["required_context_usable"] is False


def _structure_context(close=101.0):
    start = datetime(2026, 9, 21, 3, 45, tzinfo=timezone.utc)
    bars = [_bar(start + timedelta(minutes=5 * index), 104.0) for index in range(24)]
    bars[17] = replace(bars[17], low=101.5)
    level = KnownLevel(
        level_id="favorable-low",
        kind="SWING_LOW",
        price=101.5,
        formed_at=bars[17].end,
        known_at=bars[19].available_at,
        source_bar_ids=tuple(bar.bar_id for bar in bars[15:20]),
    )
    return _context(
        start + timedelta(minutes=120), close, bars=bars, known_structure=(level,)
    )


def test_favorable_structure_failure_is_built_without_injected_observations():
    thesis = _frozen_thesis()
    context = _structure_context()
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["entry_boundary_failure"] is False
    assert report.predicates["failed_favorable_structure"] is True
    assert report.predicates["favorable_structure_id"] == "favorable-low"


def test_favorable_structure_buffer_uses_only_confirmation_prefix():
    thesis = _frozen_thesis()
    context = _structure_context(105.0)
    profile = resolve_profile(thesis.management_profile)
    baseline = favorable_structure(thesis, context, profile)
    changed = list(context.primary_bars)
    changed[-1] = replace(changed[-1], high=1000.0, low=1.0)
    shock = replace(
        context, atr=100.0, primary_bars=tuple(changed), primary_bar=changed[-1]
    )
    assert favorable_structure(thesis, shock, profile) == baseline
    assert baseline[1] < 0.2


@pytest.mark.parametrize("change", ["future", "pre_entry", "missing_history"])
def test_unproven_or_future_structure_cannot_create_invalidation(change):
    thesis = _frozen_thesis()
    context = _structure_context()
    level = context.known_structure[0]
    if change == "future":
        context = replace(
            context,
            known_structure=(
                replace(
                    level, known_at=context.decision_event_time + timedelta(minutes=5)
                ),
            ),
        )
    elif change == "pre_entry":
        context = replace(
            context,
            known_structure=(
                replace(
                    level, formed_at=datetime(2026, 9, 21, 4, 15, tzinfo=timezone.utc)
                ),
            ),
        )
    else:
        context = replace(context, primary_bars=context.primary_bars[-3:])
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["failed_favorable_structure"] is None


def test_volume_spike_without_adverse_price_response_is_neutral():
    thesis = _frozen_thesis()
    context = replace(
        _structure_context(105.0), participation={"relative_volume_20": 3.0}
    )
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["volume_backed_adverse_move"] is False


def test_missing_volume_baseline_is_unknown_even_with_a_numeric_ratio():
    thesis = _frozen_thesis()
    context = replace(
        _structure_context(),
        primary_bars=_structure_context().primary_bars[-3:],
        participation={"relative_volume_20": 3.0},
    )
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["volume_backed_adverse_move"] is None


def test_unusable_indicator_quality_cannot_supply_a_route_b_corroborator():
    thesis = _frozen_thesis()
    context = replace(
        _structure_context(),
        participation={"relative_volume_20": 3.0},
        observation_quality={"relative_volume_20": ContextQuality.STALE},
    )
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert not has_independent_corroborator(report)


def test_vwap_is_only_a_contradiction_for_an_explicit_acceptance_premise():
    thesis = _frozen_thesis(profile="trend_continuation")
    context = replace(_structure_context(), session_vwap=120.0)
    default_report = build_evidence(
        thesis, context, resolve_profile(thesis.management_profile)
    )
    assert "vwap_acceptance_failure" not in default_report.predicates
    explicit = replace(
        thesis,
        management_profile=replace(
            thesis.management_profile,
            values={
                **thesis.management_profile.values,
                "requires_vwap_acceptance": True,
            },
        ),
    )
    report = build_evidence(
        explicit, context, resolve_profile(explicit.management_profile)
    )
    assert report.predicates["vwap_acceptance_failure"] is True


def test_report_recomputes_dependency_collapse_and_rejects_stale_votes():
    stale = replace(
        _observation(
            "old",
            EvidenceFamily.STRUCTURE,
            "price",
            EvidenceDirection.OPPOSING,
            EvidenceSeverity.DECISIVE,
        ),
        quality="STALE",
    )
    fake = _observation(
        "fake",
        EvidenceFamily.PARTICIPATION,
        "volume",
        EvidenceDirection.OPPOSING,
        EvidenceSeverity.MATERIAL,
    )
    report = EvidenceReport(observations=(stale,), collapsed=(stale, fake))
    assert report.collapsed == (stale,)
    assert not report.opposing
    assert not has_independent_corroborator(report)


def test_unknown_profile_version_uses_bounded_management_without_false_version_label():
    snapshot = replace(
        _frozen_thesis().management_profile,
        version="unimplemented-v99",
        values={"objective_mode": "legacy_fixed"},
    )
    profile = resolve_profile(snapshot)
    assert profile.name is ManagementProfileName.UNKNOWN_LEGACY_BOUNDED
    assert not profile.normal_thesis_management
    assert profile.version == "management-profiles-v1"
    assert profile.values["requested_profile_version"] == "unimplemented-v99"
    assert profile.objective_mode.value == "fixed_objective"


def test_profile_defaults_and_nested_values_cannot_drift_between_replays():
    with pytest.raises(TypeError):
        default_profiles()[ManagementProfileName.TREND_CONTINUATION] = None
    values = {"nested": {"setting": 2}}
    profile = ManagementProfile(ManagementProfileName.TREND_CONTINUATION, values=values)
    values["nested"]["setting"] = 3
    assert profile.values["nested"]["setting"] == 2
    with pytest.raises(TypeError):
        profile.values["nested"]["setting"] = 3


def test_missing_higher_timeframe_structure_remains_unknown():
    thesis = _frozen_thesis()
    context = replace(
        _structure_context(), higher_quality=SourceQuality(ContextQuality.STALE)
    )
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["higher_timeframe_support"] is None
    assert report.predicates["higher_timeframe_failure"] is None


def test_completed_higher_swing_contradicts_a_local_break_without_an_extra_vote():
    thesis = _frozen_thesis()
    context = _structure_context()
    higher = []
    for offset in range(0, 24, 3):
        group = context.primary_bars[offset : offset + 3]
        higher.append(
            replace(
                group[-1],
                bar_id=f"15m:{group[-1].end.isoformat()}",
                start=group[0].start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                volume=sum(bar.volume for bar in group),
            )
        )
    context = replace(
        context,
        higher_bar=higher[-1],
        higher_bars=tuple(higher),
        higher_quality=SourceQuality(ContextQuality.VALID),
    )
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["failed_favorable_structure"] is True
    assert report.predicates["higher_timeframe_support"] is True
    assert not has_independent_corroborator(report)


def test_higher_structure_acquired_together_uses_formed_time_before_id(monkeypatch):
    from backend.exit_management import evidence

    thesis = _frozen_thesis()
    context = _structure_context()
    context = replace(context, higher_bar=context.primary_bar)
    old = replace(
        context.known_structure[0],
        level_id="z-old",
        price=90,
        formed_at=context.decision_event_time - timedelta(minutes=60),
        known_at=context.decision_event_time,
    )
    new = replace(
        old,
        level_id="a-new",
        price=110,
        formed_at=context.decision_event_time - timedelta(minutes=30),
    )
    monkeypatch.setattr(evidence, "_usable_higher_bars", lambda context: True)
    monkeypatch.setattr(evidence, "_known_structure", lambda *args: (old, new))
    monkeypatch.setattr(evidence, "_confirmation_buffer", lambda *args: 0.1)
    observation, supportive, failed = evidence._higher_timeframe_evidence(
        thesis, context, resolve_profile(thesis.management_profile), 0.05
    )
    assert observation.details["level_id"] == "a-new"
    assert failed is True
    assert supportive is False


def test_explicit_vwap_boundary_can_fail_without_counting_its_alias_twice():
    thesis = _frozen_thesis(
        profile="trend_continuation",
        entry_boundary={
            "kind": "SESSION_VWAP",
            "price": 99.0,
            "level_id": "vwap-premise",
        },
    )
    context = replace(_structure_context(100.0), session_vwap=101.0)
    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))
    assert report.predicates["entry_boundary_failure"] is True
    assert report.predicates["entry_boundary_id"] == "vwap-premise"
    assert report.predicates["vwap_acceptance_failure"] is True
    assert not has_independent_corroborator(report)
    assert (
        sum(item.dependency_group == "entry_boundary" for item in report.collapsed) == 1
    )


def test_retained_structure_and_its_buffers_survive_a_truncated_context():
    thesis = _frozen_thesis()
    context = _structure_context()
    profile = resolve_profile(thesis.management_profile)
    report = build_evidence(thesis, context, profile)
    management = ManagementState(
        favorable_structure_id=report.predicates["favorable_structure_id"],
        favorable_structure_price=report.predicates["favorable_structure_price"],
        favorable_structure_buffer=0.4,
        favorable_structure_failure_buffer=0.25,
        favorable_structure_known_at=report.predicates["favorable_structure_known_at"],
    )
    trimmed = replace(
        context, primary_bars=context.primary_bars[-3:], known_structure=()
    )
    restored = build_evidence(thesis, trimmed, profile, management_state=management)
    assert restored.predicates["failed_favorable_structure"] is True
    assert restored.predicates["favorable_structure_id"] == "favorable-low"
    assert restored.predicates["favorable_structure_buffer"] == 0.25
    assert restored.predicates["favorable_structure_trail_buffer"] == 0.4


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_entry_straddling_pivot_cannot_supply_post_entry_structure(direction):
    thesis = _frozen_thesis(direction)
    context = _structure_context()
    level = context.known_structure[0]
    if direction == "SELL":
        level = replace(level, kind="SWING_HIGH", price=98.5)
        context = replace(context, known_structure=(level,))
    thesis = replace(
        thesis,
        fill_binding=replace(
            thesis.fill_binding,
            entry_terminal_at=(level.formed_at - timedelta(minutes=2)).isoformat(),
        ),
    )

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["failed_favorable_structure"] is None


@pytest.mark.parametrize(
    "defect", ["early_known_at", "missing_source", "naive", "invalid"]
)
def test_unprovable_swing_does_not_create_failure_or_block_entry_boundary(defect):
    thesis = _frozen_thesis()
    context = _structure_context(close=99.0)
    level = context.known_structure[0]
    if defect == "early_known_at":
        level = replace(level, known_at=level.known_at - timedelta(minutes=5))
    elif defect == "missing_source":
        # The input-ID manifest alone is not enough to reconstruct confirmation ATR.
        context = replace(
            context,
            primary_bars=tuple(
                bar
                for bar in context.primary_bars
                if bar.bar_id != level.source_bar_ids[0]
            ),
        )
    elif defect == "naive":
        level = replace(level, known_at=level.known_at.replace(tzinfo=None))
    else:
        level = replace(level, formed_at="invalid")
    context = replace(context, known_structure=(level,))

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["entry_boundary_failure"] is True
    assert report.predicates["required_context_usable"] is True
    assert report.predicates["failed_favorable_structure"] is None


def test_missing_vwap_corroboration_cannot_veto_known_structural_failure():
    thesis = _frozen_thesis(requires_vwap_acceptance=True)
    context = replace(_structure_context(close=99.0), session_vwap=None)

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["entry_boundary_failure"] is True
    assert report.predicates["vwap_acceptance_failure"] is None
    assert report.predicates["required_context_usable"] is True
    assert report.predicates["recovery_context_usable"] is False


def test_late_arriving_history_cannot_supply_a_confirmation_time_atr():
    thesis = _frozen_thesis()
    context = _structure_context()
    bars = list(context.primary_bars)
    # The swing's five source candles existed at confirmation, but this earlier
    # ATR input was missing until today's evaluation. Its omission is not a
    # valid compressed price series at the original confirmation time.
    bars[7] = replace(bars[7], available_at=context.decision_event_time)
    context = replace(context, primary_bars=tuple(bars))

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["failed_favorable_structure"] is None
    assert report.predicates["required_context_usable"] is True


def test_missing_vwap_still_prevents_a_vwap_defined_boundary_failure():
    thesis = _frozen_thesis(entry_boundary={"kind": "SESSION_VWAP", "price": 100.0})
    context = replace(_structure_context(close=99.0), session_vwap=None)

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["entry_boundary_failure"] is None
    assert report.predicates["required_context_usable"] is False


@pytest.mark.parametrize("defect", ["future", "naive", "invalid_price", "stale"])
def test_optional_invalid_higher_context_is_unknown_without_vetoing_boundary(defect):
    thesis = _frozen_thesis()
    context = _structure_context(close=99.0)
    higher = []
    for offset in range(0, 24, 3):
        group = context.primary_bars[offset : offset + 3]
        higher.append(
            replace(
                group[-1],
                bar_id=f"15m:{group[-1].end.isoformat()}",
                start=group[0].start,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                volume=sum(bar.volume for bar in group),
            )
        )
    if defect == "future":
        higher[-1] = replace(
            higher[-1], available_at=context.decision_event_time + timedelta(minutes=5)
        )
    elif defect == "naive":
        higher[-1] = replace(
            higher[-1], available_at=higher[-1].available_at.replace(tzinfo=None)
        )
    elif defect == "invalid_price":
        higher[-1] = replace(higher[-1], close=float("nan"))
    else:
        # The quality label alone must not grant an expired context a veto.
        context = replace(
            context,
            decision_event_time=context.decision_event_time + timedelta(minutes=30),
        )
    context = replace(
        context,
        higher_bar=higher[-1],
        higher_bars=tuple(higher),
        higher_quality=SourceQuality(ContextQuality.VALID),
    )

    report = build_evidence(thesis, context, resolve_profile(thesis.management_profile))

    assert report.predicates["higher_timeframe_support"] is None
    assert report.predicates["higher_timeframe_failure"] is None
    assert report.predicates["required_context_usable"] is True
    assert report.predicates["entry_boundary_failure"] is True


@pytest.mark.parametrize("timestamp", ["2026-09-21T04:25:00", "invalid", 12])
def test_observation_availability_requires_an_aware_timestamp(timestamp):
    with pytest.raises(ValueError, match="aware ISO timestamp"):
        replace(
            _observation(
                "swing",
                EvidenceFamily.STRUCTURE,
                "price",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
            known_at=timestamp,
        )


def test_observation_source_identity_cannot_retain_mutable_objects():
    with pytest.raises(ValueError, match="source bar identities"):
        replace(
            _observation(
                "swing",
                EvidenceFamily.STRUCTURE,
                "price",
                EvidenceDirection.OPPOSING,
                EvidenceSeverity.MATERIAL,
            ),
            source_bar_ids=(["mutable"],),
        )
