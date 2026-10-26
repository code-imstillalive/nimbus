"""The forecaster's parts, each against what it is for (#1837).

Feature rows, the gradient-boosted trees (they learn a shape, quantile
models bracket it, early stopping keeps the best round), source blending,
last-value resampling with its staleness rule, and the calibrated band.
Covers what lived in #110, #216, #353, #375 and the ml/ unit files.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import _ml_path  # noqa: F401  (ml/ reaches up to const.py)
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nimbus_load.ml import blend
from nimbus_load.ml.features import build_features

BNE = ZoneInfo("Australia/Brisbane")
SYD = ZoneInfo("Australia/Sydney")
SETTINGS = settings(max_examples=40, derandomize=True, deadline=None)


# --- features ---------------------------------------------------------------------


def test_a_feature_row_encodes_time_cyclically_and_carries_every_input():
    row = build_features(
        datetime(2026, 9, 19, 18, 30, tzinfo=BNE),  # a Saturday
        21.5,
        humidity_pct=60.0,
        lag_short=1.1,
        lag_long=0.9,
        curtailment=0.5,
        schedule_start_hour=17.0,
        schedule_end_hour=19.0,
        battery_kw=-2.0,
        grid_kw=3.0,
        solar_kw=0.4,
    )
    assert len(row) == 16
    assert row[0] == pytest.approx(math.sin(2 * math.pi * 18.5 / 24))
    assert row[1] == pytest.approx(math.cos(2 * math.pi * 18.5 / 24))
    assert row[2] == pytest.approx(math.sin(2 * math.pi * 5 / 7))
    assert row[4] == pytest.approx(math.sin(2 * math.pi * 9 / 12))
    assert row[6] == 1.0  # weekend
    assert row[7:] == [21.5, 60.0, 1.1, 0.9, 0.5, 1.0, -2.0, 3.0, 0.4]


def test_midnight_and_the_hour_before_it_are_neighbours_not_opposites():
    late = np.array(build_features(datetime(2026, 9, 21, 23, 45, tzinfo=BNE), 20.0)[:2])
    early = np.array(build_features(datetime(2026, 9, 22, 0, 15, tzinfo=BNE), 20.0)[:2])
    noon = np.array(build_features(datetime(2026, 9, 22, 12, 0, tzinfo=BNE), 20.0)[:2])
    assert np.linalg.norm(late - early) < 0.2 < np.linalg.norm(late - noon)


# --- gradient-boosted trees ---------------------------------------------------------


def _data(n=400, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, 1, size=(n, 2))
    y = np.where(x[:, 0] > 0.5, 3.0, 1.0) + 0.5 * x[:, 1] + rng.normal(0, 0.1, n)
    return x, y


# --- blending sources ----------------------------------------------------------------


@SETTINGS
@given(
    st.lists(
        st.one_of(st.none(), st.just(0.0), st.floats(1e-6, 10.0)),
        min_size=1,
        max_size=6,
    )
)
def test_blend_weights_sum_to_one_and_favour_the_more_accurate(maes):
    w = blend.weights_from_mae(maes)
    assert sum(w) == pytest.approx(1.0)
    known = [(m, wi) for m, wi in zip(maes, w, strict=True) if m is not None and m > 0]
    for (m1, w1), (m2, w2) in itertools.pairwise(known):
        if m1 < m2:
            assert w1 >= w2
    if known:
        worst = max(m for m, _ in known)
        unknown_w = [wi for m, wi in zip(maes, w, strict=True) if m is None or m <= 0]
        assert all(
            u
            == pytest.approx(
                1 / worst / sum(1 / (m if m and m > 0 else worst) for m in maes)
            )
            for u in unknown_w
        )
    else:
        assert w == pytest.approx([1 / len(maes)] * len(maes))


def test_blending_is_a_weighted_mean_and_the_spread_is_the_range():
    assert blend.blend_point_estimate([1.0, 3.0]) == 2.0
    assert blend.blend_point_estimate([1.0, 3.0], [0.75, 0.25]) == 1.5
    a, b = np.array([1.0, 2.0]), np.array([3.0, 2.0])
    np.testing.assert_allclose(
        blend.blend_forecast_array([a, b], [0.25, 0.75]), [2.5, 2.0]
    )
    np.testing.assert_allclose(
        blend.cross_source_spread([a, b, np.array([0.0, 2.5])]), [3.0, 0.5]
    )
    np.testing.assert_allclose(blend.cross_source_spread([a]), [0.0, 0.0])


@pytest.mark.parametrize(
    "call",
    [
        lambda: blend.weights_from_mae([]),
        lambda: blend.blend_point_estimate([]),
        lambda: blend.blend_point_estimate([1.0], [0.5, 0.5]),
        lambda: blend.blend_forecast_array([]),
        lambda: blend.blend_forecast_array([np.zeros(2), np.zeros(3)]),
        lambda: blend.blend_forecast_array([np.zeros(2)], [0.5, 0.5]),
        lambda: blend.cross_source_spread([]),
        lambda: blend.cross_source_spread([np.zeros(2), np.zeros(3)]),
    ],
)
def test_misaligned_or_empty_inputs_are_refused(call):
    with pytest.raises(ValueError):
        call()


# --- resampling recorded events ---------------------------------------------------------

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


# --- the calibrated band -------------------------------------------------------------------
