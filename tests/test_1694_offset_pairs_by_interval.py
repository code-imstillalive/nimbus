"""nimbus #1694: compute_5min_offset() pairs each interval's SETTLED retail
price with the SAME interval's wholesale price, and follows a tariff change.

Synthetic histories shaped like the reference install's feeds (measured
10 Oct 2026): the retailer posts a provisional price ~3 s into an interval and
the real one ~20 s in; AEMO's current-price sensor posts ~78 s in.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer as sw

AEST = timezone(timedelta(hours=10))
DAY0 = datetime(2026, 10, 5, tzinfo=AEST)


def _wholesale(days):
    """A price that differs between every pair of adjacent intervals, so a
    one-interval pairing error always shows."""
    return [
        (DAY0 + timedelta(minutes=5 * k), 0.05 + 0.01 * (k % 7))
        for k in range(days * 288)
    ]


def _feeds(days, margin, provisional=0.182):
    """(retail history, wholesale history) as the sensors record them."""
    retail, wholesale = [], []
    for t, x in _wholesale(days):
        retail.append((t + timedelta(seconds=3), provisional))
        retail.append((t + timedelta(seconds=20), x + margin(t)))
        wholesale.append((t + timedelta(seconds=78), x))
    return retail, wholesale


def _offsets(retail, wholesale, monkeypatch):
    monkeypatch.setattr(sw, "fetch_price_history", lambda _e, days=5: wholesale)
    return sw.compute_5min_offset(retail, regional_spot_sensor="sensor.qld1")


def test_each_interval_is_paired_with_its_own_wholesale(monkeypatch):
    got = _offsets(*_feeds(3, lambda t: 0.075), monkeypatch)
    assert len(got) == 288
    assert max(abs(v - 0.075) for v in got.values()) < 1e-12


def test_the_provisional_post_never_counts(monkeypatch):
    # A provisional 0.182 posted at :03 of every interval would drag a mean
    # toward it; only the settled post is used.
    got = _offsets(*_feeds(2, lambda t: 0.0, provisional=0.9), monkeypatch)
    assert max(abs(v) for v in got.values()) < 1e-12


def test_a_tariff_change_is_followed_not_averaged(monkeypatch):
    """Evening network 22.3c until day 3, a flat 7.5c from then on."""
    cut = DAY0 + timedelta(days=3)

    def margin(t):
        if t < cut and 16 <= t.hour < 21:
            return 0.2231
        return 0.075

    got = _offsets(*_feeds(5, margin), monkeypatch)
    assert abs(got[18 * 12] - 0.075) < 1e-12  # 18:00 now flat
    assert abs(got[12 * 12] - 0.075) < 1e-12


def test_one_glitched_interval_does_not_move_the_offset(monkeypatch):
    retail, wholesale = _feeds(3, lambda t: 0.075)
    t = DAY0 + timedelta(days=2, hours=10, minutes=5)
    retail.append((t + timedelta(seconds=40), 5.0))  # a corrupt late post
    got = _offsets(retail, wholesale, monkeypatch)
    assert abs(got[10 * 12 + 1] - 0.075) < 1e-12


def test_no_regional_sensor_learns_nothing(monkeypatch):
    retail, wholesale = _feeds(1, lambda t: 0.075)
    monkeypatch.setattr(sw, "fetch_price_history", lambda _e, days=5: wholesale)
    assert sw.compute_5min_offset(retail, regional_spot_sensor=None) == {}


def test_the_old_pairing_would_have_been_wrong(monkeypatch):
    """The regression this fixes, pinned: pairing a retail sample with the
    wholesale sample at or before it reads the PREVIOUS interval's price."""
    retail, wholesale = _feeds(1, lambda t: 0.075)
    settled = [(t, v) for t, v in retail if t.second == 20]
    prev_errors = []
    for t, v in settled[1:]:
        before = [x for wt, x in wholesale if wt <= t][-1]
        prev_errors.append(abs((v - before) - 0.075))
    assert max(prev_errors) > 0.009  # off by the interval-to-interval move
    got = _offsets(retail, wholesale, monkeypatch)
    assert max(abs(v - 0.075) for v in got.values()) < 1e-12
