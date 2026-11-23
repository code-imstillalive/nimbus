"""A regional spot sensor that stopped updating must not teach a markup.

A tester's plan showed price spikes at the same three times every day past
their retailer's horizon, and buy and sell at the same bare wholesale price
everywhere else. Their regional current-price sensor had stopped updating:
it recorded a value only a few times a day. The markup (retail minus that
sensor) was learned in just those few 5-minute slots, as nearly the whole
retail price (the spikes), and every other slot had no markup at all, so the
extension past the feed added nothing. Synthetic prices only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from solver_inputs import price_blend, price_consolidation

T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)  # 10:00 Brisbane
FIVE = timedelta(minutes=5)
DAY = 288


def bucket(t):
    local = price_blend.solver_shared._local(t)
    return local.hour * 12 + local.minute // 5


def _retail(days=3, price=0.17):
    return [(T0 + i * FIVE + timedelta(seconds=20), price) for i in range(days * DAY)]


def test_a_regional_spot_that_stopped_updating_teaches_no_markup(monkeypatch):
    # The sensor recorded something three times a day, always the same
    # stuck value.
    stuck = [
        (T0 + d * timedelta(days=1) + slot * FIVE + timedelta(seconds=40), -0.005)
        for d in range(3)
        for slot in (6, 42, 72)
    ]
    monkeypatch.setattr(price_blend, "fetch_price_history", lambda *_a, **_k: stuck)
    monkeypatch.setattr(price_blend, "_STUCK_SPOT", {})
    assert (
        price_blend.compute_5min_offset(
            _retail(), regional_spot_sensor="sensor.regional_spot"
        )
        == {}
    )
    # ...and it is not left to a log line: setup_health raises a Repair
    # naming the sensor from this finding, cleared once it updates again.
    assert price_blend.stuck_regional_spot() == ("sensor.regional_spot", 9, 3 * DAY)


def test_a_live_regional_spot_with_flat_spells_still_teaches_the_markup(monkeypatch):
    # A live sensor that holds the same price for a while records no change
    # then; most intervals still carry a value.
    live = [
        (T0 + i * FIVE + timedelta(seconds=40), 0.08)
        for i in range(3 * DAY)
        if i % 10 not in (3, 4, 5)
    ]
    monkeypatch.setattr(price_blend, "fetch_price_history", lambda *_a, **_k: live)
    monkeypatch.setattr(
        price_blend, "_STUCK_SPOT", {"sensor.regional_spot": (9, 3 * DAY)}
    )
    offset = price_blend.compute_5min_offset(
        _retail(), regional_spot_sensor="sensor.regional_spot"
    )
    assert len(offset) == DAY
    assert all(v == pytest.approx(0.09) for v in offset.values())
    assert price_blend.stuck_regional_spot() is None


def test_past_the_feed_a_slot_without_markup_takes_the_average_markup():
    forecast = [{"time": T0.isoformat(), "per_kwh": 0.20}]
    later = T0 + 3 * FIVE
    regional = [(T0, 0.05)]
    offset = {bucket(T0): 0.10, bucket(T0 + FIVE): 0.12}
    values, real = price_blend.resample_price_with_extrapolation(
        forecast, "per_kwh", [T0, later], regional, offset
    )
    assert values == [0.20, pytest.approx(0.05 + 0.11)]
    assert real == [True, False]
    assert price_consolidation.aemo_markup_series(
        [later], regional, offset, bucket
    ) == [pytest.approx(0.05 + 0.11)]


def test_with_no_markup_learned_the_tail_keeps_the_retail_level_on_wholesale_shape(
    monkeypatch,
):
    # A regional sensor the recorder keeps no history for teaches nothing,
    # and is named for the Repair like a stuck one.
    monkeypatch.setattr(price_blend, "fetch_price_history", lambda *_a, **_k: [])
    monkeypatch.setattr(price_blend, "_STUCK_SPOT", {})
    assert (
        price_blend.compute_5min_offset(
            _retail(), regional_spot_sensor="sensor.regional_spot"
        )
        == {}
    )
    assert price_blend.stuck_regional_spot() == ("sensor.regional_spot", 0, 3 * DAY)

    # Past the feed, the tail is wholesale plus the gap at the feed's last
    # real price (0.20 - 0.05), not bare wholesale.
    forecast = [{"time": T0.isoformat(), "per_kwh": 0.20}]
    regional = [(T0, 0.05), (T0 + 2 * FIVE, 0.09)]
    values, real = price_blend.resample_price_with_extrapolation(
        forecast, "per_kwh", [T0, T0 + 3 * FIVE], regional, {}
    )
    assert values == [0.20, pytest.approx(0.09 + 0.15)]
    assert real == [True, False]
