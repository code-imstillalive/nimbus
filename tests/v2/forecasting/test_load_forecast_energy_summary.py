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
    # test_a_missing_value_is_skipped_not_zero
    fc = _fc([1.0, None, 1.0])
    s = fs.energy_summary(fc, NOW)
    # 1.0 held 0-60 min (its span runs to the next valued point), 1.0 for the
    # last point's held 60 min: 2 kWh, not 1.5.
    assert s[fs.ATTR_ENERGY_FORECAST_KWH] == pytest.approx(2.0)

    # test_no_points_no_summary
    assert fs.energy_summary([], NOW) == {}
    assert fs.energy_summary([{"time": "not a time", "value": 1}], NOW) == {}

    # test_a_load_plan_sums_energy_cost_runtime_and_average_price
    # #2128: a controllable load's plan over the next 24 h and the whole plan.
    # 2 kW for two half-hours costing 0.30 and 0.10, then off.
    s = fs.plan_summary(_fc([2.0, 2.0, 0.0, 0.0]), _fc([0.30, 0.10, 0.0, 0.0]), NOW)
    for key in ("next_24h", "plan"):
        assert s[key] == {
            "energy_kwh": 2.0,
            "cost": 0.4,
            "runtime_hours": 1.0,
            "average_price": 0.2,
        }
    # A window edge inside a period takes the part of its cost inside it:
    # 12-hour periods at 2 kW costing 1.00 each, seen 6 hours in.
    day = fs.plan_summary(
        _fc([2.0] * 4, minutes=720),
        _fc([1.0] * 4, minutes=720),
        NOW + timedelta(hours=6),
    )
    assert day["next_24h"]["cost"] == pytest.approx(2.0)
    assert day["next_24h"]["energy_kwh"] == pytest.approx(48.0)
    assert day["plan"]["cost"] == pytest.approx(3.5)
    assert day["plan"]["runtime_hours"] == pytest.approx(42.0)
    # Without a cost plan the energy and runtime stand and the prices are
    # unknown, never zero; without a power plan there is nothing.
    unpriced = fs.plan_summary(_fc([2.0, 2.0]), [], NOW)["plan"]
    assert (unpriced["cost"], unpriced["average_price"]) == (None, None)
    assert fs.plan_summary([], _fc([1.0]), NOW) == {}
