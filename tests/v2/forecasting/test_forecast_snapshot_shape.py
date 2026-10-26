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


class TestBuildingASnapshot(unittest.TestCase):
    def test_it_is_json_serialisable(self):
        """It goes into a Store, which serialises whole. A datetime left in
        would fail at write time, on the one path nobody watches."""
        import json

        json.dumps(_snapshot())

    def test_it_records_when_it_was_captured(self):
        self.assertEqual(_snapshot()["captured_at"], T0.isoformat())

    def test_it_is_versioned(self):
        self.assertEqual(_snapshot()["version"], fs.SNAPSHOT_VERSION)

    def test_mismatched_input_lengths_truncate_rather_than_raise(self):
        """A solve that produced fewer load points than solar points is a
        real shape here. Refusing to capture anything trades a partial
        record for none, and the record cannot be recreated later."""
        grid = _tiered_grid()
        snap = fs.build_snapshot(grid, [1.0] * len(grid), [2.0, 3.0], captured_at=T0)
        self.assertEqual(len(snap["points"]), 2)


class TestResamplingOntoAGrid(unittest.TestCase):
    def test_it_holds_the_last_value_between_points(self):
        """Sample-and-hold, matching `resample_history_mean`'s documented
        principle for state-like series: a forecast point states a level
        expected to persist, so averaging would invent a value the forecast
        never made."""
        grid = _tiered_grid()
        snap = _snapshot(grid)
        between = [T0 + timedelta(minutes=7), T0 + timedelta(minutes=8)]
        got = fs.resample_snapshot_to_grid(snap, between)
        self.assertIsNotNone(got)
        solar, _load = got
        self.assertEqual(solar, [1.0, 1.0])

    def test_a_coarser_target_grid_works(self):
        grid = _tiered_grid()
        hourly = [T0 + timedelta(hours=h) for h in range(1, 4)]
        got = fs.resample_snapshot_to_grid(_snapshot(grid), hourly)
        self.assertIsNotNone(got)

    def test_unsorted_stored_points_are_handled(self):
        snap = _snapshot()
        snap["points"].reverse()
        self.assertIsNotNone(fs.resample_snapshot_to_grid(snap, _tiered_grid()))


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
        snap = _snapshot()
        snap["version"] = fs.SNAPSHOT_VERSION + 99
        self._assert_none(snap)

    def test_a_grid_starting_BEFORE_the_snapshot_is_skipped(self):
        """A partial grid filled from the first point would attribute the
        forecast a prediction it never made for those earlier periods."""
        grid = [T0 - timedelta(hours=2), T0, T0 + timedelta(hours=1)]
        self._assert_none(_snapshot(), grid)

    def test_a_non_dict_is_skipped(self):
        self._assert_none(None)
        self._assert_none([1, 2, 3])

    def test_an_empty_target_grid_is_skipped(self):
        self.assertIsNone(fs.resample_snapshot_to_grid(_snapshot(), []))

    def test_corrupt_individual_points_are_dropped_not_fatal(self):
        snap = _snapshot()
        snap["points"].insert(0, {"time": "not-a-date", "solar_kw": 1, "load_kw": 1})
        snap["points"].insert(1, {"solar_kw": 1})
        snap["points"].insert(2, "not even a dict")
        self.assertIsNotNone(fs.resample_snapshot_to_grid(snap, _tiered_grid()))

    def test_it_never_returns_zeros_for_an_unusable_snapshot(self):
        """The failure this guards against, stated directly: no route may
        produce a list of zeros."""
        snap = _snapshot()
        snap["version"] = 999
        got = fs.resample_snapshot_to_grid(snap, _tiered_grid())
        self.assertIsNot(got, [0.0] * len(_tiered_grid()))
        self.assertIsNone(got)


class TestRetention(unittest.TestCase):
    def test_keeping_more_than_exist_is_a_no_op(self):
        snaps = {"2026-09-01": {}, "2026-09-02": {}}
        self.assertEqual(fs.prune_snapshots(snaps, keep_days=90), snaps)

    def test_zero_or_negative_keeps_nothing(self):
        self.assertEqual(fs.prune_snapshots({"2026-09-01": {}}, keep_days=0), {})
        self.assertEqual(fs.prune_snapshots({"2026-09-01": {}}, keep_days=-3), {})

    def test_retention_must_cover_the_rescore_window(self):
        """The re-score service accepts 1-30 days back. A shorter retention
        would create days re-scorable for EPR but not for forecast regret --
        an asymmetry a household would reasonably read as a bug."""
        snaps = {f"2026-09-{d:02d}": {} for d in range(1, 31)}
        self.assertEqual(len(fs.prune_snapshots(snaps, keep_days=30)), 30)


class TestDayKeying(unittest.TestCase):
    def test_it_is_a_plain_local_date(self):
        self.assertEqual(fs.day_key_for(T0), "2026-09-26")


if __name__ == "__main__":
    unittest.main(verbosity=2)
