"""The forecaster's parts, each against what it is for (#1837).

Feature rows, the gradient-boosted trees (they learn a shape, quantile
models bracket it, early stopping keeps the best round), source blending,
last-value resampling with its staleness rule, and the calibrated band.
Covers what lived in #110, #216, #353, #375 and the ml/ unit files.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import _ml_path  # noqa: F401  (ml/ reaches up to const.py)
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nimbus_load.ml import blend, model
from nimbus_load.ml.features import build_features
from nimbus_load.ml.gbrt import GBRT

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


@pytest.mark.parametrize(
    ("hour", "start", "end", "inside"),
    [
        (18, 17, 19, 1.0),
        (19, 17, 19, 0.0),
        (23, 22, 6, 1.0),
        (5, 22, 6, 1.0),
        (12, 22, 6, 0.0),
        (12, None, 6, 0.0),
    ],
)
def test_the_schedule_feature_wraps_midnight(hour, start, end, inside):
    row = build_features(
        datetime(2026, 9, 21, hour, tzinfo=BNE),
        20.0,
        schedule_start_hour=start,
        schedule_end_hour=end,
    )
    assert row[12] == inside
    assert row[6] == 0.0  # a Monday


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


def test_the_trees_learn_a_step_the_mean_cannot():
    x, y = _data()
    fitted = GBRT().fit(x, y)
    err = np.mean(np.abs(fitted.predict(x) - y))
    assert err < 0.2 < np.mean(np.abs(y - y.mean()))
    assert (
        fitted.predict(np.array([[0.9, 0.0]]))[0]
        > fitted.predict(np.array([[0.1, 0.0]]))[0] + 1.5
    )


def test_a_constant_target_predicts_that_constant():
    x, _ = _data(60)
    fitted = GBRT().fit(x, np.full(60, 2.5))
    np.testing.assert_allclose(fitted.predict(x), 2.5)


def test_quantile_models_bracket_the_median():
    x, y = _data(600)
    lo = GBRT(quantile=0.1).fit(x, y).predict(x)
    mid = GBRT().fit(x, y).predict(x)
    hi = GBRT(quantile=0.9).fit(x, y).predict(x)
    assert np.mean(lo <= mid) > 0.9 and np.mean(mid <= hi) > 0.9
    covered = np.mean((y >= lo) & (y <= hi))
    assert 0.65 < covered < 0.95  # an 80% band, near enough on its own data
    assert GBRT(quantile=0.1).fit(x, y).init_value == pytest.approx(
        np.percentile(y, 10)
    )


def test_early_stopping_keeps_the_best_validation_round():
    x, y = _data(300)
    xv, yv = _data(100, seed=2)
    full = GBRT(n_estimators=200).fit(x, y)
    stopped = GBRT(n_estimators=200).fit(
        x, y, x_val=xv, y_val=yv, early_stopping_rounds=5
    )
    assert 0 < len(stopped.trees) < len(full.trees) == 200
    # Without the validation set the rounds are not cut.
    assert len(GBRT(n_estimators=30).fit(x, y, early_stopping_rounds=5).trees) == 30


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


def test_each_grid_point_takes_the_last_reading_unless_it_is_stale_or_not_finite():
    events = [
        (T0, 1.0),
        (T0 + timedelta(minutes=10), float("nan")),
        (T0 + timedelta(minutes=20), 3.0),
    ]
    grid = [T0 - timedelta(minutes=5)] + [
        T0 + timedelta(minutes=m) for m in (0, 5, 15, 25, 60)
    ]
    assert model.resample_last_value(events, grid) == [None, 1.0, 1.0, None, 3.0, 3.0]
    stale = model.resample_last_value(events, grid, max_staleness=timedelta(minutes=30))
    assert stale[-1] is None and stale[4] == 3.0


def test_a_reading_counts_as_observed_only_in_the_first_grid_point_after_it():
    events = [
        (T0, 1.0),
        (T0 + timedelta(minutes=10), float("inf")),
        (T0 + timedelta(minutes=20), 3.0),
    ]
    grid = [T0 - timedelta(minutes=5)] + [
        T0 + timedelta(minutes=m) for m in (0, 5, 10, 15, 20, 25)
    ]
    assert model.resample_observed_mask(events, grid) == [
        False,
        True,
        False,
        False,
        False,
        True,
        False,
    ]


def test_resampling_across_a_daylight_saving_change_keeps_real_elapsed_time():
    # Sydney springs forward at 02:00 on 4 Oct 2026: 01:30 to 03:30 is one hour.
    before = datetime(2026, 10, 4, 1, 30, tzinfo=SYD)
    after = model._dst_safe_add(before, timedelta(hours=1))
    assert after.hour == 3 and after.minute == 30
    got = model.resample_last_value(
        [(before, 5.0)], [after], max_staleness=timedelta(minutes=61)
    )
    assert got == [5.0]


# --- the calibrated band -------------------------------------------------------------------


def test_a_band_is_cold_start_until_there_are_enough_residuals_then_widens_with_lead():
    few = [0.5] * (model.MIN_RESIDUALS_FOR_CALIBRATION - 1)
    assert model.calibration_half_width(few) is None
    assert model.calibrated_band(few, 2.0, 0.0) == pytest.approx(
        2.0 * model.COLD_START_BAND_FRACTION
    )
    assert model.calibrated_band(few, 0.0, 0.0) == model.COLD_START_BAND_MIN_KW
    residuals = list(np.linspace(0.0, 1.0, 11))
    half = model.calibration_half_width(residuals)
    assert half == pytest.approx(0.8)
    assert model.calibrated_band(residuals, 9.0, 0.0) == pytest.approx(half)
    assert model.calibrated_band(residuals, 9.0, 3.0) == pytest.approx(half * 2)
    assert model.calibrated_band(residuals, 9.0, -1.0) == pytest.approx(half)
    assert model.calibrated_band(
        residuals, 9.0, 3.0, near_term_half_width=0.5
    ) == pytest.approx(1.0)


def test_a_scheduled_load_is_its_expected_power_inside_the_window_and_zero_outside():
    stamps = [datetime(2026, 9, 21, h, tzinfo=BNE) for h in (21, 23, 2, 6, 12)]
    assert model.deterministic_values(stamps, 1.8, 22.0, 6.0) == [
        0.0,
        1.8,
        1.8,
        0.0,
        0.0,
    ]
