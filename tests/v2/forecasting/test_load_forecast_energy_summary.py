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


def test_a_missing_value_is_skipped_not_zero():
    fc = _fc([1.0, None, 1.0])
    s = fs.energy_summary(fc, NOW)
    # 1.0 held 0-60 min (its span runs to the next valued point), 1.0 for the
    # last point's held 60 min: 2 kWh, not 1.5.
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(2.0)


def test_no_points_no_summary():
    assert fs.energy_summary([], NOW) == {}
    assert fs.energy_summary([{"time": "not a time", "value": 1}], NOW) == {}
