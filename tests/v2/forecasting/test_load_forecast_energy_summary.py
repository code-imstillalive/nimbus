"""What a load's power forecast adds up to: energy over the next 24 h and the
whole forecast, and the next 24 h peak (#1837).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import forecast_stats as fs

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


def _fc(values, minutes=30, start=NOW):
    return [
        {"time": (start + timedelta(minutes=minutes * i)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]


def test_a_flat_kilowatt_for_a_day_is_24_kwh():
    s = fs.energy_summary(_fc([1.0] * 48), NOW)
    assert s[fs.ATTR_ENERGY_NEXT_24H_KWH] == pytest.approx(24.0)
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(24.0)


def test_only_the_next_24_hours_count_and_the_rest_is_in_the_whole():
    s = fs.energy_summary(_fc([2.0] * 96), NOW)  # 48 h at 2 kW
    assert s[fs.ATTR_ENERGY_NEXT_24H_KWH] == pytest.approx(48.0)
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(96.0)


def test_the_past_does_not_count():
    """A forecast that started an hour ago: only from now on."""
    s = fs.energy_summary(_fc([1.0] * 4, start=NOW - timedelta(hours=1)), NOW)
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(1.0)


def test_the_peak_and_when():
    values = [0.5] * 48
    values[20] = 3.2
    s = fs.energy_summary(_fc(values), NOW)
    assert s[fs.ATTR_PEAK_NEXT_24H_KW] == pytest.approx(3.2)
    assert s[fs.ATTR_PEAK_NEXT_24H_AT] == (NOW + timedelta(minutes=600)).isoformat()


def test_a_missing_value_is_skipped_not_zero():
    fc = _fc([1.0, None, 1.0])
    s = fs.energy_summary(fc, NOW)
    # 1.0 held 0-60 min (its span runs to the next valued point), 1.0 for the
    # last point's held 60 min: 2 kWh, not 1.5.
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(2.0)


def test_no_points_no_summary():
    assert fs.energy_summary([], NOW) == {}
    assert fs.energy_summary([{"time": "not a time", "value": 1}], NOW) == {}
