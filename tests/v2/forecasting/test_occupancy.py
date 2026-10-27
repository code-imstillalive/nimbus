"""Occupancy for the load forecast (#1847): the away mask, the forward away
window, the away profile and its two rules. Synthetic inputs only."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import _ml_path  # noqa: F401  (ml/ reaches up to const.py)
import numpy as np
from nimbus_load.ml import occupancy as occ

T0 = datetime(
    2026, 10, 1, 0, 0, tzinfo=ZoneInfo("Australia/Brisbane")
)  # local midnight
SLOT = 15


def _grid(days):
    return [T0 + timedelta(minutes=SLOT * i) for i in range(days * 24 * 60 // SLOT)]


def test_occupancy_reads_away_only_from_a_real_away_event():
    events = [(T0 + timedelta(hours=8), 0.0), (T0 + timedelta(hours=20), 1.0)]
    assert not occ.away_at(events, T0)  # before the first event: home
    assert occ.away_at(events, T0 + timedelta(hours=8))
    assert not occ.away_at(events, T0 + timedelta(hours=20))
    # Unknown (NaN) counts as home, so the forecast stays as it is.
    assert not occ.away_at([(T0, float("nan"))], T0 + timedelta(hours=1))
    assert occ.away_periods(events, T0 + timedelta(days=1)) == [
        (T0 + timedelta(hours=8), T0 + timedelta(hours=20))
    ]
    # Still away at the end: the span runs to `end`.
    end = T0 + timedelta(days=2)
    assert occ.away_periods([(T0, 0.0)], end) == [(T0, end)]


def test_away_spans_become_gaps_training_leaves_out():
    load = [(T0 + timedelta(hours=h), float(h)) for h in range(24)]
    spans = [(T0 + timedelta(hours=8), T0 + timedelta(hours=20))]
    out = occ.mask_away(load, spans)
    kept = [t.hour for t, v in out if not math.isnan(v)]
    assert kept == list(range(8)) + list(range(20, 24))
    # One NaN marker, at the departure (#1556's gap convention).
    assert [t for t, v in out if math.isnan(v)] == [T0 + timedelta(hours=8)]
    assert occ.mask_away(load, []) == load


def test_the_forward_window_follows_departure_and_return():
    times = [T0 + timedelta(hours=h) for h in range(48)]
    # Away now, no return: away for the whole horizon.
    assert occ.forward_away(times, away_now=True).all()
    # Away now, back at 30 h: away until then, home from then.
    back = occ.forward_away(times, away_now=True, return_at=T0 + timedelta(hours=30))
    assert back[:30].all() and not back[30:].any()
    # A return already past is ignored.
    assert occ.forward_away(
        times, away_now=True, return_at=T0 - timedelta(hours=1)
    ).all()
    # Home now: home, unless a departure ahead is set.
    assert not occ.forward_away(times, away_now=False).any()
    trip = occ.forward_away(
        times,
        away_now=False,
        departure=T0 + timedelta(hours=10),
        return_at=T0 + timedelta(hours=20),
    )
    assert list(np.flatnonzero(trip)) == list(range(10, 20))
    # A departure with no return (or a return before it): away to the end.
    open_ended = occ.forward_away(
        times, away_now=False, departure=T0 + timedelta(hours=10), return_at=T0
    )
    assert not open_ended[:10].any() and open_ended[10:].all()
    assert occ.forward_away([], away_now=True).size == 0


def _history(days, away_days=()):
    """1 kW base, plus 2 kW from 07:00 to 22:00 on home days."""
    times = _grid(days)
    events = []
    for d in range(days):
        day = T0 + timedelta(days=d)
        events.append((day, 0.0 if d in away_days else 1.0))
    values = []
    for t in times:
        d = (t - T0).days
        home = d not in away_days
        values.append(1.0 + (2.0 if home and 7 <= t.hour < 22 else 0.0))
    return times, values, events


def test_with_little_away_history_the_profile_is_the_base_load():
    times, values, events = _history(14, away_days=(13,))
    p = occ.build_profile(
        times,
        values,
        events,
        now=times[-1] + timedelta(minutes=SLOT),
        slot_minutes=SLOT,
    )
    assert p.kind == "base_load" and p.away_days == 1
    # The 20th percentile of each slot over the last 14 days: evenings are
    # mostly home days (3 kW), so the base there is still 3 kW; the low
    # quantile only reaches the 1 kW floor where it is the floor.
    assert p.at(T0 + timedelta(hours=3)) == 1.0
    assert len(p.kw) == 24 * 60 // SLOT


def test_with_enough_away_days_the_profile_is_the_learned_away_mean():
    times, values, events = _history(20, away_days=(15, 16, 17, 18))
    p = occ.build_profile(
        times,
        values,
        events,
        now=times[-1] + timedelta(minutes=SLOT),
        slot_minutes=SLOT,
    )
    assert p.kind == "learned_away" and p.away_days == 4
    # Away days never had the 2 kW daytime load.
    assert p.at(T0 + timedelta(hours=12)) == 1.0
    assert p.at(T0 + timedelta(hours=3)) == 1.0


def test_applying_the_profile_replaces_only_the_away_times():
    times = [T0 + timedelta(hours=h) for h in range(4)]
    p = occ.AwayProfile(
        kind="base_load", slot_minutes=60, kw=tuple([0.5] * 24), away_days=0
    )
    out = occ.apply(
        times, [3.0, 3.0, 3.0, 3.0], np.array([False, True, True, False]), p
    )
    assert list(out) == [3.0, 0.5, 0.5, 3.0]
    assert occ.build_profile([], [], [], now=T0, slot_minutes=SLOT) is None
