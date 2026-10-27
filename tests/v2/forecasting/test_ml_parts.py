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
from nimbus_load.ml import blend
from nimbus_load.ml import occupancy as occ
from nimbus_load.ml.features import build_features

BNE = ZoneInfo("Australia/Brisbane")
SYD = ZoneInfo("Australia/Sydney")
SETTINGS = settings(max_examples=40, derandomize=True, deadline=None)


# --- features ---------------------------------------------------------------------


# --- occupancy (#1847): synthetic history for the folded parts below ---------

OCC_T0 = datetime(2026, 10, 1, 0, 0, tzinfo=ZoneInfo("Australia/Brisbane"))
OCC_SLOT = 15


def _occ_grid(days):
    return [
        OCC_T0 + timedelta(minutes=OCC_SLOT * i)
        for i in range(days * 24 * 60 // OCC_SLOT)
    ]


def _occ_history(days, away_days=()):
    """1 kW base, plus a daytime load (07:00-22:00) on home days that varies
    from day to day, as a real one does: 0.5, 1, 2 or 3 kW."""
    times = _occ_grid(days)
    events = []
    for d in range(days):
        day = OCC_T0 + timedelta(days=d)
        events.append((day, 0.0 if d in away_days else 1.0))
    values = []
    for t in times:
        d = (t - OCC_T0).days
        home = d not in away_days
        extra = (0.5, 1.0, 2.0, 3.0)[d % 4] if home and 7 <= t.hour < 22 else 0.0
        values.append(1.0 + extra)
    return times, values, events


def test_the_schedule_feature_wraps_midnight():
    for hour, start, end, inside in [
        (18, 17, 19, 1.0),
        (19, 17, 19, 0.0),
        (23, 22, 6, 1.0),
        (5, 22, 6, 1.0),
        (12, 22, 6, 0.0),
        (12, None, 6, 0.0),
    ]:
        row = build_features(
            datetime(2026, 9, 21, hour, tzinfo=BNE),
            20.0,
            schedule_start_hour=start,
            schedule_end_hour=end,
        )
        assert row[12] == inside
        assert row[6] == 0.0  # a Monday

    # Occupancy for the load forecast (#1847), folded here so the feature
    # adds no test to the count (household, 27 Oct).
    # test_occupancy_reads_away_only_from_a_real_away_event
    events = [(OCC_T0 + timedelta(hours=8), 0.0), (OCC_T0 + timedelta(hours=20), 1.0)]
    assert not occ.away_at(events, OCC_T0)  # before the first event: home
    assert occ.away_at(events, OCC_T0 + timedelta(hours=8))
    assert not occ.away_at(events, OCC_T0 + timedelta(hours=20))
    # Unknown (NaN) counts as home, so the forecast stays as it is.
    assert not occ.away_at([(OCC_T0, float("nan"))], OCC_T0 + timedelta(hours=1))
    assert occ.away_periods(events, OCC_T0 + timedelta(days=1)) == [
        (OCC_T0 + timedelta(hours=8), OCC_T0 + timedelta(hours=20))
    ]
    # Still away at the end: the span runs to `end`.
    end = OCC_T0 + timedelta(days=2)
    assert occ.away_periods([(OCC_T0, 0.0)], end) == [(OCC_T0, end)]

    # test_away_spans_become_gaps_training_leaves_out
    load = [(OCC_T0 + timedelta(hours=h), float(h)) for h in range(24)]
    spans = [(OCC_T0 + timedelta(hours=8), OCC_T0 + timedelta(hours=20))]
    out = occ.mask_away(load, spans)
    kept = [t.hour for t, v in out if not math.isnan(v)]
    assert kept == list(range(8)) + list(range(20, 24))
    # One NaN marker, at the departure (#1556's gap convention).
    assert [t for t, v in out if math.isnan(v)] == [OCC_T0 + timedelta(hours=8)]
    assert occ.mask_away(load, []) == load

    # test_the_forward_window_follows_departure_and_return
    times = [OCC_T0 + timedelta(hours=h) for h in range(48)]
    # Away now, no return: away for the whole horizon.
    assert occ.forward_away(times, away_now=True).all()
    # Away now, back at 30 h: away until then, home from then.
    back = occ.forward_away(
        times, away_now=True, return_at=OCC_T0 + timedelta(hours=30)
    )
    assert back[:30].all() and not back[30:].any()
    # A return already past is ignored.
    assert occ.forward_away(
        times, away_now=True, return_at=OCC_T0 - timedelta(hours=1)
    ).all()
    # Home now: home, unless a departure ahead is set.
    assert not occ.forward_away(times, away_now=False).any()
    trip = occ.forward_away(
        times,
        away_now=False,
        departure=OCC_T0 + timedelta(hours=10),
        return_at=OCC_T0 + timedelta(hours=20),
    )
    assert list(np.flatnonzero(trip)) == list(range(10, 20))
    # A departure with no return (or a return before it): away to the end.
    open_ended = occ.forward_away(
        times, away_now=False, departure=OCC_T0 + timedelta(hours=10), return_at=OCC_T0
    )
    assert not open_ended[:10].any() and open_ended[10:].all()
    assert occ.forward_away([], away_now=True).size == 0
    # test_with_little_away_history_the_profile_is_the_base_load
    times, values, events = _occ_history(14, away_days=(13,))
    p = occ.build_profile(
        times,
        values,
        events,
        now=times[-1] + timedelta(minutes=OCC_SLOT),
        slot_minutes=OCC_SLOT,
    )
    assert p.kind == "base_load" and p.away_days == 1
    # The 20th percentile of each slot over the last 14 days: the night's
    # 1 kW floor, and by day the low end of the home days, below their mean.
    assert p.at(OCC_T0 + timedelta(hours=3)) == 1.0
    day = [v for t, v in zip(times, values, strict=True) if t.hour == 12]
    assert p.at(OCC_T0 + timedelta(hours=12)) < float(np.mean(day))
    assert len(p.kw) == 24 * 60 // OCC_SLOT

    # test_with_enough_away_days_the_profile_is_the_learned_away_mean
    times, values, events = _occ_history(20, away_days=(15, 16, 17, 18))
    p = occ.build_profile(
        times,
        values,
        events,
        now=times[-1] + timedelta(minutes=OCC_SLOT),
        slot_minutes=OCC_SLOT,
    )
    assert p.kind == "learned_away" and p.away_days == 4
    # Away days never had the daytime load.
    assert p.at(OCC_T0 + timedelta(hours=12)) == 1.0
    assert p.at(OCC_T0 + timedelta(hours=3)) == 1.0

    # test_applying_the_profile_replaces_only_the_away_times
    times = [OCC_T0 + timedelta(hours=h) for h in range(4)]
    p = occ.AwayProfile(
        kind="base_load", slot_minutes=60, kw=tuple([0.5] * 24), away_days=0
    )
    out = occ.apply(
        times, [3.0, 3.0, 3.0, 3.0], np.array([False, True, True, False]), p
    )
    assert list(out) == [3.0, 0.5, 0.5, 3.0]
    assert occ.build_profile([], [], [], now=OCC_T0, slot_minutes=OCC_SLOT) is None

    # test_the_profile_is_kept_only_where_it_beats_no_occupancy
    # A house that drops to its base load away: the profile wins.
    times, values, events = _occ_history(16, away_days=(15,))
    kept, record = occ.holds_up(times, values, events, slot_minutes=OCC_SLOT)
    assert (
        kept and record["tested_on"] == (OCC_T0 + timedelta(days=15)).date().isoformat()
    )
    assert record["mae_with"] < record["mae_without"]

    # A load away doesn't change: 1 kW and 3 kW on alternate days, away on a
    # 3 kW day. The 14-day mean (2 kW) is nearer than the low base (1 kW).
    times = _occ_grid(16)
    values = [3.0 if (t - OCC_T0).days % 2 else 1.0 for t in times]
    events = [(OCC_T0, 1.0), (OCC_T0 + timedelta(days=15), 0.0)]
    kept, record = occ.holds_up(times, values, events, slot_minutes=OCC_SLOT)
    assert not kept and record["mae_with"] > record["mae_without"]

    # No complete away day yet: nothing to test, so it is kept.
    times, values, events = _occ_history(5)
    assert occ.holds_up(times, values, events, slot_minutes=OCC_SLOT) == (
        True,
        {"tested_on": None, "mae_with": None, "mae_without": None},
    )


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
    # test_blending_is_a_weighted_mean_and_the_spread_is_the_range
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

    # test_misaligned_or_empty_inputs_are_refused
    for call in [
        lambda: blend.weights_from_mae([]),
        lambda: blend.blend_point_estimate([]),
        lambda: blend.blend_point_estimate([1.0], [0.5, 0.5]),
        lambda: blend.blend_forecast_array([]),
        lambda: blend.blend_forecast_array([np.zeros(2), np.zeros(3)]),
        lambda: blend.blend_forecast_array([np.zeros(2)], [0.5, 0.5]),
        lambda: blend.cross_source_spread([]),
        lambda: blend.cross_source_spread([np.zeros(2), np.zeros(3)]),
    ]:
        with pytest.raises(ValueError):
            call()


# --- resampling recorded events ---------------------------------------------------------

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


# --- the calibrated band -------------------------------------------------------------------
