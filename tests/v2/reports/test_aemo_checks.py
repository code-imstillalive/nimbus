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
    mean_realised_price,
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


@pytest.mark.parametrize(
    ("now", "start", "price"),
    [
        (at(12, 43), (12, 30), -0.01),
        (at(13, 15), (13, 0), -0.012),  # the third bucket, not forecast[0]
        (at(12, 35), (12, 30), -0.01),  # an elapsed head bucket is skipped
        (at(12, 30), (12, 30), -0.01),  # half-open: the start is in
        (at(13, 0), (13, 0), -0.012),  # and the end belongs to the next
    ],
)
def test_the_bucket_containing_now_is_chosen(now, start, price):
    got_start, _end, got_price = select_current_bucket(FORECAST, now)
    assert (got_start.hour, got_start.minute, got_price) == (*start, price)


@pytest.mark.parametrize(
    "entries",
    [
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
    ],
    ids=[
        "empty",
        "none",
        "zero_width_far",
        "naive",
        "bool_price",
        "no_price",
        "zero_width",
    ],
)
def test_nothing_usable_is_no_bucket(entries):
    assert select_current_bucket(entries, at(12, 43)) is None
    assert select_current_bucket(FORECAST, at(20, 0)) is None


def test_a_bad_entry_never_blocks_a_good_one():
    for bad in (
        {"start_time": "not a time"},
        "nonsense",
        _bucket(12, 30, -0.01, minutes=0),
    ):
        start, _e, _p = select_current_bucket([bad, *FORECAST], at(12, 43))
        assert (start.hour, start.minute) == (12, 30)


# --- the realised mean ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "mean", "n"),
    [
        (samples(12, 30, [0.10, 0.20, 0.30]), 0.20, 3),
        (samples(12, 0, [9.0] * 6) + samples(12, 30, [0.10, 0.20]), 0.15, 2),
        (
            [(at(13, 0), 99.0), *samples(12, 30, [0.10])],
            0.10,
            1,
        ),  # the end is the next's
        ([(datetime(2026, 9, 13, 12, 40), 99.0), *samples(12, 30, [0.10])], 0.10, 1),  # noqa: DTZ001
    ],
    ids=["mean", "outside_excluded", "end_boundary", "naive_skipped"],
)
def test_the_realised_mean_is_of_the_samples_inside_the_window(rows, mean, n):
    got, count = mean_realised_price(rows, at(12, 30), at(13, 0))
    assert (got, count) == (pytest.approx(mean), n)
    assert mean_realised_price([], at(12, 30), at(13, 0)) == (None, 0)
    assert mean_realised_price(None, at(12, 30), at(13, 0)) == (None, 0)


# --- the whole cross-check ------------------------------------------------------------------------


def test_agreement_is_reported_unflagged_with_its_window():
    got = compare_forecast_to_actuals(
        FORECAST, samples(12, 30, [-0.011, -0.009, -0.010, -0.010]), at(12, 50), 0.10
    )
    assert (got["flagged"], got["forecast_price"], got["n_samples"]) == (
        False,
        -0.01,
        4,
    )
    assert got["window_start"].startswith("2026-09-13T12:30")
    assert got["window_end"].startswith("2026-09-13T13:00")
    same = compare_forecast_to_actuals(
        FORECAST, samples(12, 30, [-0.01]), at(12, 50), 0.10
    )
    assert same["disagreement_dollars"] == pytest.approx(0.0)  # no retail offset


def test_a_real_disagreement_is_flagged():
    got = compare_forecast_to_actuals(
        FORECAST, samples(12, 30, [2.0] * 4), at(12, 50), 0.10
    )
    assert got["flagged"] and got["disagreement_dollars"] == pytest.approx(
        2.01, abs=1e-4
    )


def test_no_bucket_or_no_samples_is_no_answer_and_zones_align():
    assert (
        compare_forecast_to_actuals(FORECAST, samples(12, 30, [0.1]), at(20, 0), 0.1)
        is None
    )
    assert compare_forecast_to_actuals(FORECAST, [], at(12, 50), 0.1) is None
    utc = datetime(2026, 9, 13, 2, 50, tzinfo=UTC)  # 12:50 here
    got = compare_forecast_to_actuals(FORECAST, samples(12, 30, [-0.01]), utc, 0.10)
    assert got["window_start"].startswith("2026-09-13T12:30")


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
