"""The RRP cross-check reads every provider's rows defensively (#2393: solver_inputs/rrp_crosscheck.py's
coverage, one behaviour per test): a row it cannot read is skipped, never a zero price, and a current
price that is not a number is "no value".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from solver_inputs import rrp_crosscheck as rc

NOW = datetime(2026, 10, 31, 0, 2, tzinfo=UTC)
HOUR = NOW.replace(minute=0)


def test_unreadable_provider_rows_are_skipped_never_read_as_zero():
    # test_unreadable_provider_rows_are_skipped_never_read_as_zero
    end = HOUR + timedelta(minutes=5)
    amber = rc.read_source(
        "amber",
        None,
        {
            "detailedForecast": [
                "not a row",
                {"spot_per_kwh": "n/a", "duration": 5, "end_time": end.isoformat()},
                {"spot_per_kwh": 0.077, "duration": 5},  # no end time
                {"spot_per_kwh": 0.077, "duration": 5, "end_time": end.isoformat()},
            ]
        },
        1.0,
        NOW,
    )
    assert list(amber.points.values()) == [pytest.approx(0.07)]  # only the readable row
    pd7 = rc.read_source(
        "pd7",
        None,
        {
            "forecast": [
                "not a row",
                {"time": HOUR.isoformat(), "nemtime": "not a time", "raw_value": 0.08},
                {
                    "time": HOUR.isoformat(),
                    "nemtime": (HOUR + timedelta(minutes=30)).isoformat(),
                    "raw_value": None,
                },
            ]
        },
        1.0,
        NOW,
    )
    assert pd7.kind == rc.KIND_PD7DAY and pd7.points == {}


def test_a_bad_interval_length_falls_back_to_five_minutes():
    # test_a_bad_interval_length_falls_back_to_five_minutes
    lv = {
        "source_field": "spotCost",
        "direction": "Sell",
        "interval_minutes": "five",
        "forecast": [{"time": HOUR.isoformat(), "value": 0.1}],
    }
    read = rc.read_source("lv", None, lv, 1.0, NOW)
    assert len(read.points) == 1  # one 5-minute interval, not none and not an hour


@pytest.mark.parametrize("state", [None, "unavailable", "nan", "inf"])
def test_a_current_price_that_is_not_a_number_has_no_value(state):
    # test_a_current_price_that_is_not_a_number_has_no_value
    read = rc.read_source("aemo", state, {}, 1.0, NOW)
    assert (
        read.kind == rc.KIND_CURRENT and read.reason == "no_value" and read.points == {}
    )
