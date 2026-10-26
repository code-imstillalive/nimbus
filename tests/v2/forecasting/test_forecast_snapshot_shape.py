"""What the forecast SAID, captured: the day-ahead snapshot's shape and
what it keeps, so tomorrow's regret is scored against the forecast as it was
made (#937) (#1837).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "forecast_snapshot.py"
)


def _load_module():
    """Load by path, with no Home Assistant import anywhere.

    Deliberately not a package import: that executes the package `__init__`,
    which pulls in HA's config-entry machinery. This module has zero HA
    dependencies and this loader is what PROVES it. Same pattern as
    `test_467_calendar_trip_windows.py` and
    `tests/v2/reports/test_power_sign_check.py`.
    """
    spec = importlib.util.spec_from_file_location("forecast_snapshot", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["forecast_snapshot"] = module
    spec.loader.exec_module(module)
    return module


fs = _load_module()

BNE = timezone(timedelta(hours=10))
T0 = datetime(2026, 9, 26, 0, 0, tzinfo=BNE)


def _tiered_grid() -> list[datetime]:
    """Five-minute periods for the first half hour, then hourly -- the real
    shape this solver produces, not a uniform stand-in."""
    times = [T0 + timedelta(minutes=5 * i) for i in range(6)]
    times += [T0 + timedelta(hours=h) for h in range(1, 6)]
    return times


def _snapshot(grid: list[datetime] | None = None) -> dict:
    grid = grid or _tiered_grid()
    solar = [float(i) for i in range(len(grid))]
    load = [1.0 + i * 0.5 for i in range(len(grid))]
    return fs.build_snapshot(grid, solar, load, captured_at=T0, horizon_hours=96.0)


class TestAMissingForecastIsNotAZeroForecast(unittest.TestCase):
    """The distinction the whole mechanism rests on.

    Zeros would score as a forecast that confidently predicted darkness and no
    load -- the worst possible forecast -- when the truth is there is nothing
    to judge. Every route to unusable must return None.
    """

    def _assert_none(self, snapshot, grid=None):
        self.assertIsNone(
            fs.resample_snapshot_to_grid(snapshot, grid or _tiered_grid())
        )

    def test_an_unknown_version_is_skipped(self):
        # test_an_unknown_version_is_skipped
        snap = _snapshot()
        snap["version"] = fs.SNAPSHOT_VERSION + 99
        self._assert_none(snap)

        # test_a_grid_starting_BEFORE_the_snapshot_is_skipped: A partial grid filled from the first point would attribute the
        grid = [T0 - timedelta(hours=2), T0, T0 + timedelta(hours=1)]
        self._assert_none(_snapshot(), grid)

        # test_a_non_dict_is_skipped
        self._assert_none(None)
        self._assert_none([1, 2, 3])

        # test_an_empty_target_grid_is_skipped
        self.assertIsNone(fs.resample_snapshot_to_grid(_snapshot(), []))

        # test_corrupt_individual_points_are_dropped_not_fatal
        snap = _snapshot()
        snap["points"].insert(0, {"time": "not-a-date", "solar_kw": 1, "load_kw": 1})
        snap["points"].insert(1, {"solar_kw": 1})
        snap["points"].insert(2, "not even a dict")
        self.assertIsNotNone(fs.resample_snapshot_to_grid(snap, _tiered_grid()))


class TestRetention(unittest.TestCase):
    def test_zero_or_negative_keeps_nothing(self):
        self.assertEqual(fs.prune_snapshots({"2026-09-01": {}}, keep_days=0), {})
        self.assertEqual(fs.prune_snapshots({"2026-09-01": {}}, keep_days=-3), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
