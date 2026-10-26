"""Two checks of the price against AEMO's own numbers (#452) (#1837).

The wholesale cross-check compares the forecast's current half-open bucket
with the realised dispatch samples so far inside it, with no retail offset.
The retail check compares the current retail price with AEMO's live P5MIN
price plus this time of day's usual markup, and warns once per period when
they disagree past the threshold, either way. Synthetic prices only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest
import solver_writer as sw
from aemo_crosscheck import (
    compare_forecast_to_actuals,
    select_current_bucket,
)

AEST = timezone(timedelta(hours=10))


def at(h, m, s=0):
    return datetime(2026, 9, 13, h, m, s, tzinfo=AEST)


def _bucket(h, m, price, minutes=30):
    start = at(h, m)
    return {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=minutes)).isoformat(),
        "price": price,
    }


FORECAST = [
    _bucket(12, 0, -0.0085),
    _bucket(12, 30, -0.01),
    _bucket(13, 0, -0.012),
    _bucket(13, 30, -0.012),
]


def samples(h, m, values, step=5):
    return [(at(h, m) + timedelta(minutes=i * step), v) for i, v in enumerate(values)]


# --- which bucket is now ------------------------------------------------------------------------


def test_nothing_usable_is_no_bucket():
    # test_nothing_usable_is_no_bucket
    for entries in [
        [],
        None,
        FORECAST[:1] and [_bucket(20, 0, 0.1, minutes=0)],
        [
            {
                "start_time": "2026-09-13T12:30:00",
                "end_time": "2026-09-13T13:00:00",
                "price": -0.01,
            }
        ],
        [dict(_bucket(12, 30, -0.01), price=True)],  # a bool is not a price
        [{k: v for k, v in _bucket(12, 30, -0.01).items() if k != "price"}],
        [_bucket(12, 30, -0.01, minutes=0)],
    ]:
        assert select_current_bucket(entries, at(12, 43)) is None
        assert select_current_bucket(FORECAST, at(20, 0)) is None

    # test_a_bad_entry_never_blocks_a_good_one
    for bad in (
        {"start_time": "not a time"},
        "nonsense",
        _bucket(12, 30, -0.01, minutes=0),
    ):
        start, _e, _p = select_current_bucket([bad, *FORECAST], at(12, 43))
        assert (start.hour, start.minute) == (12, 30)

    # test_no_bucket_or_no_samples_is_no_answer_and_zones_align
    assert (
        compare_forecast_to_actuals(FORECAST, samples(12, 30, [0.1]), at(20, 0), 0.1)
        is None
    )
    assert compare_forecast_to_actuals(FORECAST, [], at(12, 50), 0.1) is None
    utc = datetime(2026, 9, 13, 2, 50, tzinfo=UTC)  # 12:50 here
    got = compare_forecast_to_actuals(FORECAST, samples(12, 30, [-0.01]), utc, 0.10)
    assert got["window_start"].startswith("2026-09-13T12:30")


# --- the realised mean ----------------------------------------------------------------------------


# --- the whole cross-check ------------------------------------------------------------------------


# --- retail against P5MIN plus the usual markup -----------------------------------------------------

P5 = "sensor.region_5min_price"


@pytest.fixture
def aemo_now(monkeypatch):
    def set_state(state):
        if isinstance(state, Exception):

            def boom(_e):
                raise state

            monkeypatch.setattr(sw, "ha_get", boom)
        else:
            monkeypatch.setattr(
                sw,
                "ha_get",
                lambda e: {"entity_id": e, "state": state, "attributes": {}},
            )

    return set_state


@pytest.mark.parametrize(
    ("retail", "flagged", "gap"),
    [(0.30, False, 0.0), (0.55, True, 0.25), (0.40, False, 0.10), (0.05, True, -0.25)],
    ids=["agrees", "above", "at_threshold", "below"],
)
def test_retail_is_flagged_past_the_threshold_either_way(
    aemo_now, retail, flagged, gap
):
    aemo_now("0.10")
    got = sw.check_aemo_p5min_disagreement(P5, retail, 10, {10: 0.20}, 0.10)
    assert got["flagged"] is flagged
    assert got["disagreement_dollars"] == pytest.approx(gap)
    assert got["expected_retail"] == pytest.approx(0.30)


def test_a_disagreement_is_warned_once_per_period(monkeypatch, caplog):
    monkeypatch.setattr(sw, "_AEMO_P5MIN_LAST_WARNED_PERIOD", None)
    detail = {
        "retail_now": 0.55,
        "aemo_p5min_now": 0.10,
        "expected_retail": 0.30,
        "disagreement_dollars": 0.25,
        "threshold_dollars": 0.10,
    }
    a, b = at(12, 0), at(12, 5)
    with caplog.at_level(logging.WARNING):
        for period in (a, a, b):
            sw._warn_aemo_p5min_disagreement_once(period, detail)
    assert sum("Nimbus #452" in r.getMessage() for r in caplog.records) == 2
