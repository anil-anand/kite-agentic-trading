"""Cache immutable row hashes without caching causal data eligibility."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

import backend.market_context as module
from backend.replay import serialize_replay_artifact
from backend.tests.test_market_context import _at, _candles


def legacy_revision_hash(start, opening, high, low, close, volume, revision):
    # Exact pre-optimization path: the normalized row also included end and
    # receipt columns, and the hash selector discarded the un-hashed fields.
    row = pd.Series(
        {
            "date": start,
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "received_at": start + timedelta(minutes=5),
            "available_at": start + timedelta(minutes=5),
            "end": start + timedelta(minutes=5),
            "revision": revision,
        }
    )
    return module._input_hash(
        pd.DataFrame([row]).drop(columns=["available_at", "received_at"])
    )[:16]


@pytest.mark.parametrize("numeric", [int, float, np.int64, np.float64])
@pytest.mark.parametrize("volume", [1000, 1000.0, np.nan, None, 0.0, -0.0])
def test_memoized_hash_is_identical_to_prior_dataframe_bytes(numeric, volume):
    start = pd.Timestamp(_at(0))
    args = (
        start,
        numeric(100),
        numeric(101),
        numeric(99),
        numeric(100),
        volume,
        "vendor-revision",
    )
    expected = legacy_revision_hash(*args)
    cached_args = (*args[:5], None if pd.isna(volume) else volume, args[-1])
    assert module._content_revision_hash(*cached_args) == expected
    assert module._content_revision_hash(*cached_args) == expected


def test_integer_and_float_payloads_cannot_alias_the_same_cache_entry():
    module._content_revision_hash.cache_clear()
    base = (pd.Timestamp(_at(0)), 100, 101, 99, 100, 1000, "source-v1")
    floating = (base[0], 100.0, 101.0, 99.0, 100.0, 1000.0, base[-1])
    assert module._content_revision_hash(*base) == legacy_revision_hash(*base)
    assert module._content_revision_hash(*floating) == legacy_revision_hash(*floating)
    assert module._content_revision_hash.cache_info().misses == 2


def test_warm_cache_changes_neither_context_artifact_nor_revision_identity(monkeypatch):
    frame = _candles(30)
    optimized = module.build_market_context("TEST", frame, _at(155))
    # Call the old implementation at precisely the same chronology and compare
    # the entire immutable context, including IDs, quality and confirmed regime.
    monkeypatch.setattr(module, "_content_revision_hash", legacy_revision_hash)
    reference = module.build_market_context("TEST", frame, _at(155))
    assert serialize_replay_artifact(optimized) == serialize_replay_artifact(reference)


def test_cached_future_row_remains_ineligible_and_correction_gets_new_hash():
    module._content_revision_hash.cache_clear()
    frame = _candles(4)
    later = module.build_market_context("TEST", frame, _at(25))
    earlier = module.build_market_context("TEST", frame, _at(12))
    assert len(later.primary_bars) == 4
    assert len(earlier.primary_bars) == 2
    assert earlier.primary_bar.end == _at(10)
    changed = frame.copy(deep=True)
    changed.loc[3, "high"] += 1
    revised = module.build_market_context("TEST", changed, _at(25))
    assert revised.primary_bar.bar_id != later.primary_bar.bar_id
    assert revised.primary_bars[0].bar_id == later.primary_bars[0].bar_id


def test_repeated_contexts_hash_only_new_or_revised_content():
    module._content_revision_hash.cache_clear()
    frame = _candles(20)
    module.build_market_context("FIRST", frame.iloc[:19], _at(100))
    first = module._content_revision_hash.cache_info()
    module.build_market_context("SECOND", frame, _at(105))
    second = module._content_revision_hash.cache_info()
    assert second.hits - first.hits == 19
    assert second.misses - first.misses == 1
    assert second.maxsize == 65536
