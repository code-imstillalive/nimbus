"""nimbus issue #937, stage 1: capture what the forecast SAID.

## Why this comes before the analysis that consumes it

Normally a capability should be wired before it ships — I argued exactly that
earlier today on #1267 and #1270. This is the documented exception, and the
reason is a measurement, not a preference.

`research/forecast_capture.py`:

    NONE of the day-ahead forecasts this project has -- solar (Solcast) or
    price (nem_pd7day) -- have ANY recorder history at all (0 history points
    over a 2-day check)... Conclusion: there is no way to reconstruct what any
    of these forecasts said for a PAST day. This isn't a data-quality gap,
    it's a data-existence gap -- the only honest fix is to start capturing
    forward from today.

**There is nothing to wire until something has been captured**, and every day
the capture does not exist is a day of evidence permanently lost. #937's own
top-priority action is "more days", and it has been unachievable since the
reference household moved to the native path — no HACS install has ever
produced a day-ahead `forecast_regret` record.

## The distinction this file mostly exists to pin

**A missing forecast is not a zero forecast.**

`resample_snapshot_to_grid()` returns `None`, never zeros, for anything
unusable. A zero-filled forecast scores as one that confidently predicted
darkness and no load — the *worst possible* forecast — where the truth is that
there is no forecast to judge. The standalone writer's own
`load_forecast_snapshot()` makes the same demand in its docstring: *"treat None
as 'skip the forecast-regret decomposition for this day entirely', never as
'assume zero forecast error'."*

`TestAMissingForecastIsNotAZeroForecast` covers every route to unusable.

## Tiered grids

Points carry an explicit `time` each rather than a start plus a step, because
this solver's grid is tiered — the first periods are minutes wide and later
ones an hour. A start-and-step encoding would silently mis-place every point
past the tier boundary, and the mis-placement would read downstream as
forecast error.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
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
    `test_1241_detect_a_wrong_power_sign_convention.py`.
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

    def test_every_point_carries_its_own_time(self):
        """Not a start plus a step -- the grid is tiered, so a step would
        mis-place every point past the boundary."""
        snap = _snapshot()
        times = [p["time"] for p in snap["points"]]
        self.assertEqual(len(times), len(set(times)))
        self.assertIn("00:25", times[5])
        self.assertIn("01:00", times[6])

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
    def test_exact_grid_match_round_trips(self):
        grid = _tiered_grid()
        got = fs.resample_snapshot_to_grid(_snapshot(grid), grid)
        self.assertIsNotNone(got)
        solar, load = got
        self.assertEqual(solar, [float(i) for i in range(len(grid))])
        self.assertEqual(load, [1.0 + i * 0.5 for i in range(len(grid))])

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

    def test_no_points_is_skipped(self):
        snap = _snapshot()
        snap["points"] = []
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
    def test_it_keeps_the_newest(self):
        snaps = {f"2026-09-{d:02d}": {"version": 1} for d in range(1, 21)}
        kept = fs.prune_snapshots(snaps, keep_days=5)
        self.assertEqual(sorted(kept), [f"2026-09-{d:02d}" for d in range(16, 21)])

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

    def test_it_does_not_guess_a_timezone(self):
        """Takes the datetime as given. Silently converting would file a
        Brisbane evening's forecast under the wrong day, and a wrongly-filed
        forecast is indistinguishable downstream from a wrong forecast."""
        utc_evening = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)
        self.assertEqual(fs.day_key_for(utc_evening), "2026-09-26")
        self.assertEqual(fs.day_key_for(utc_evening.astimezone(BNE)), "2026-09-27")


class TestItIsPure(unittest.TestCase):
    def test_no_homeassistant_import(self):
        src = _MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("homeassistant", src)

    def test_it_persists_nothing_itself(self):
        """Storage belongs to the integration, which has a Store. A pure
        module that wrote files would be untestable by path and would make
        two owners of one artefact."""
        import ast

        tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called.add(getattr(f, "id", None) or getattr(f, "attr", None))
        # Asserted on the CALL GRAPH, not the text. Three function docstrings
        # discuss `Store` by name -- they explain WHY persistence lives
        # elsewhere -- and a text search cannot tell that from doing it. An
        # assertion that punished the explanation would push the reasoning out
        # of the file, which is the same trade this session already got wrong
        # twice on other guards.
        for forbidden in ("open", "Store", "async_save", "dump", "dumps"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
