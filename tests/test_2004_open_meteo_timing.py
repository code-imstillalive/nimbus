"""nimbus #2004: Open-Meteo Solar Forecast's `watts` is the power at its key
+ 15 min (the library stores each instantaneous reading under the start of
its 15-minute step), so each value is placed at its own instant and each plan
period reads the power at its midpoint. Synthetic forecasts only."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()
from solver_inputs import solar

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)


def _ramp_watts(steps=8):
    """1 kW more every 15-minute key: a straight morning ramp."""
    return {
        (T0 + timedelta(minutes=15 * i)).isoformat(): 1000.0 * i for i in range(steps)
    }


def _grid(minutes, n, start=T0):
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


def _old_reading(watts, grid):
    """The reading before #2004: each value held from its own key."""
    entries = [{"time": ts, "value": w / 1000.0} for ts, w in watts.items()]
    return solver_writer.resample_forecast(entries, "value", grid)


def _new_reading(watts, grid):
    entries = solver_writer._solar_entries_from_attributes({"watts": watts})
    return solar._resampler(entries)(entries, "value", grid)


def test_each_watts_value_is_placed_fifteen_minutes_after_its_key():
    """Pins the library's keying: `time_start = time - 15 min`. If
    open-meteo-solar-forecast ever re-keys `watts`, this offset must come
    out and this test is where that shows."""
    entries = solver_writer._solar_entries_from_attributes({"watts": _ramp_watts(2)})
    assert [e["time"] for e in entries] == [
        T0 + timedelta(minutes=15),
        T0 + timedelta(minutes=30),
    ]
    assert all(e["instant"] for e in entries)
    assert solver_writer.OPEN_METEO_WATTS_LEAD == timedelta(minutes=15)


def test_a_half_hour_period_reads_exactly_what_it_read_before():
    """A 30-minute period starting on the half hour: its midpoint is the
    key at its start + 15 min, the very reading it had before."""
    watts, grid = _ramp_watts(), _grid(30, 3)
    assert _new_reading(watts, grid) == pytest.approx(_old_reading(watts, grid))


def test_a_five_minute_period_reads_the_power_at_its_midpoint():
    """On the ramp, true power at minute m after T0 is (m - 15) / 15 kW.
    Before, the three 5-minute periods inside a step all read the step's
    end; now each reads its own midpoint."""
    watts, grid = _ramp_watts(), _grid(5, 6, start=T0 + timedelta(minutes=30))
    true_mean = [((30 + 5 * i + 2.5) - 15) / 15 for i in range(6)]
    assert _new_reading(watts, grid) == pytest.approx(true_mean)
    before = _old_reading(watts, grid)
    assert before == pytest.approx([2.0, 2.0, 2.0, 3.0, 3.0, 3.0])
    assert all(b > n for b, n in zip(before, true_mean, strict=True))


def test_outside_the_readings_the_nearest_one_is_held():
    watts = _ramp_watts(2)  # instants at +15 (0 kW) and +30 (1 kW)
    before = _grid(5, 3, start=T0 - timedelta(minutes=30))
    after = _grid(5, 2, start=T0 + timedelta(hours=2))
    assert _new_reading(watts, before) == pytest.approx([0.0, 0.0, 0.0])
    assert _new_reading(watts, after) == pytest.approx([1.0, 1.0])


def test_solcast_and_the_generic_shape_are_unchanged():
    detailed = {
        "detailedForecast": [
            {
                "period_start": (T0 + timedelta(minutes=30 * i)).isoformat(),
                "pv_estimate": float(i),
                "pv_estimate10": 0.0,
                "pv_estimate90": 9.0,
            }
            for i in range(4)
        ]
    }
    entries = solver_writer._solar_entries_from_attributes(detailed)
    assert solar._resampler(entries) is solver_writer.resample_forecast
    generic = {"forecast": [{"time": T0.isoformat(), "value": 1.0}]}
    entries = solver_writer._solar_entries_from_attributes(generic)
    assert solar._resampler(entries) is solver_writer.resample_forecast


def test_the_solver_s_open_meteo_member_reads_midpoints():
    """End to end through build_solar_arrays under auto-include, the only
    member being Open-Meteo, with the running-low gate and live anchor out
    of the way."""
    states = {"sensor.om_today": {"attributes": {"watts": _ramp_watts()}}}
    cfg = {
        "solver_auto_include_known_solar": True,
        "known_sources": {"open_meteo": ["sensor.om_today"]},
    }
    grid = _grid(5, 6, start=T0 + timedelta(minutes=30))
    with (
        patch.object(solver_writer, "entity_exists", lambda e: e in states),
        patch.object(solver_writer, "ha_get", lambda e: states[e]),
        patch.object(solar, "_apply_running_low_gate", lambda *a, **k: None),
    ):
        value, lower, upper = solar.build_solar_arrays(
            cfg, grid, len(grid), now=grid[0]
        )
    true_mean = [((30 + 5 * i + 2.5) - 15) / 15 for i in range(6)]
    assert value == pytest.approx(true_mean)
    assert lower == pytest.approx(true_mean) and upper == pytest.approx(true_mean)
