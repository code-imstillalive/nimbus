"""The AEMO cross-check refuses rather than guesses (#2393: aemo_crosscheck.py's coverage, one behaviour
per test): a time without a timezone, a price that is not a number, a window with no realised sample.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aemo_crosscheck import _parse_price, _parse_time, mean_realised_price

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        (T0, T0),  # already a datetime with its zone
        (T0.replace(tzinfo=None), None),  # no zone: never assumed
        (
            "2026-11-08T10:00:00+10:00",
            datetime.fromisoformat("2026-11-08T10:00:00+10:00"),
        ),
        ("2026-11-08T10:00:00", None),
        ("not a time", None),
        (1731060000, None),  # a number is not a time here
    ],
)
def test_a_forecast_time_without_its_zone_is_refused_not_guessed(value, parsed):
    # test_a_forecast_time_without_its_zone_is_refused_not_guessed
    assert _parse_time(value) == parsed


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        (0.123, 0.123),
        (5, 5.0),
        ("-0.0085", -0.0085),
        ("n/a", None),
        (True, None),
        (None, None),
    ],
)
def test_a_price_that_is_not_a_number_is_refused(value, parsed):
    # test_a_price_that_is_not_a_number_is_refused
    assert _parse_price(value) == parsed


def test_a_window_with_no_realised_sample_has_no_mean():
    # test_a_window_with_no_realised_sample_has_no_mean
    before = [
        (T0 - timedelta(minutes=5), 0.2),
        (T0.replace(tzinfo=None), 0.3),
    ]  # outside, and zone-less
    assert mean_realised_price(before, T0, T0 + timedelta(minutes=30)) == (None, 0)
    assert mean_realised_price([], T0, T0 + timedelta(minutes=30)) == (None, 0)
