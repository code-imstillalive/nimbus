"""Regression test for nimbus issue #363 (Mark Purcell, codebase review),
the naive-timestamp crash finding: parse_iso() attaches UTC to a naive
datetime *object* (the isinstance(s, datetime) branch), but used to
return datetime.fromisoformat(s) completely unchanged for a STRING --
naive if the source's own ISO string omits a UTC offset (e.g.
"2026-09-04T12:00:00", no "Z"/"+00:00" suffix). resample_forecast() then
compares that naive value against timezone-aware grid_times, raising
TypeError: can't compare offset-naive and offset-aware datetimes deep
inside the sort/comparison -- and fetch_solar_source_safe()'s own except
clause only caught HTTPError/URLError/KeyError/JSONDecodeError, so a
real third-party source publishing offset-less timestamps took down the
ENTIRE solve cycle with a traceback instead of being safely dropped.

Fixed by giving the string branch the exact same "assume UTC for a
genuinely naive value" treatment the datetime-object branch already had,
plus adding TypeError/ValueError to fetch_solar_source_safe()'s own
except clause as defense-in-depth for a genuinely unparseable (non-ISO)
string, which still raises ValueError even after this fix.

Real, direct calls into the actual parse_iso()/resample_forecast() --
not a reimplementation -- same "import solver_writer directly"
convention as this project's other solver_writer test files.

nimbus #2008 (Mark Purcell, 23 Oct 2026): a naive value is now read in the
install's own zone (LOCAL_TZ), not UTC; #363 chose UTC only to stop the
crash. The four cases he asked for -- naive, `Z`, an explicit offset, an
aware datetime -- are pinned below, and a value that carries its offset
reads exactly as before.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import _solver_path  # noqa: F401
import solver_shared
import solver_writer


class TestParseIsoNaiveTimestamps(unittest.TestCase):
    def setUp(self):
        self._zone = solver_shared.LOCAL_TZ

    def tearDown(self):
        solver_shared.LOCAL_TZ = self._zone

    def test_offset_less_string_is_read_in_the_installs_zone(self):
        """#2008: wall-clock 12:00 where the install is, not 12:00 UTC."""
        result = solver_writer.parse_iso("2026-09-04T12:00:00")
        self.assertIs(result.tzinfo, solver_shared.LOCAL_TZ)
        self.assertEqual((result.year, result.hour), (2026, 12))

    def test_the_zone_followed_is_the_installs_not_brisbanes(self):
        solver_shared.LOCAL_TZ = ZoneInfo("Europe/London")
        result = solver_writer.parse_iso("2026-09-04T12:00:00")
        self.assertEqual(result.utcoffset(), timedelta(hours=1))  # BST
        self.assertEqual(result.astimezone(UTC).hour, 11)

    def test_a_value_with_its_own_offset_reads_as_before_in_any_zone(self):
        """Every source measured on 22 Oct carries an offset: for them
        nothing changes, whatever the install's zone."""
        for zone in ("Australia/Brisbane", "Europe/London", "America/New_York"):
            solver_shared.LOCAL_TZ = ZoneInfo(zone)
            self.assertEqual(
                solver_writer.parse_iso("2026-09-04T12:00:00+10:00"),
                datetime(2026, 9, 4, 2, 0, tzinfo=UTC),
            )
            self.assertEqual(
                solver_writer.parse_iso("2026-09-04T12:00:00Z"),
                datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
            )

    def test_z_suffixed_string_still_parses_as_utc(self):
        # Existing, already-working shape -- must stay byte-identical.
        result = solver_writer.parse_iso("2026-09-04T12:00:00Z".replace("Z", "+00:00"))
        self.assertEqual(result, datetime(2026, 9, 4, 12, 0, tzinfo=UTC))

    def test_explicit_offset_string_is_preserved_not_overridden(self):
        result = solver_writer.parse_iso("2026-09-04T12:00:00+10:00")
        self.assertEqual(result.utcoffset(), timedelta(hours=10))

    def test_naive_datetime_object_is_read_in_the_installs_zone(self):
        # The isinstance(s, datetime) branch follows the same rule (#2008).
        result = solver_writer.parse_iso(datetime(2026, 9, 4, 12, 0))  # noqa: DTZ001
        self.assertEqual(
            result, datetime(2026, 9, 4, 12, 0, tzinfo=solver_shared.LOCAL_TZ)
        )

    def test_aware_datetime_object_is_passed_through_unchanged(self):
        aware = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        result = solver_writer.parse_iso(aware)
        self.assertIs(result, aware)


class TestResampleForecastWithMixedNaiveAndAwareSources(unittest.TestCase):
    def test_does_not_raise_when_source_timestamps_are_offset_less(self):
        """The real crash this issue describes: before the fix, this
        raised TypeError from inside the sort/comparison the moment ANY
        forecast point's own "time" field lacked a UTC offset."""
        forecast = [
            {"time": "2026-09-04T00:00:00", "value": 1.0},
            {"time": "2026-09-04T01:00:00", "value": 2.0},
            {"time": "2026-09-04T02:00:00", "value": 3.0},
        ]
        grid_times = [datetime(2026, 9, 4, h, 0, tzinfo=UTC) for h in range(4)]
        # Must not raise.
        result = solver_writer.resample_forecast(forecast, "value", grid_times)
        self.assertEqual(len(result), 4)

    def test_resamples_correctly_once_naive_timestamps_are_normalized(self):
        forecast = [
            {"time": "2026-09-04T00:00:00", "value": 1.0},
            {"time": "2026-09-04T02:00:00", "value": 3.0},
        ]
        # #2008: naive points are the install's wall-clock times, so the grid
        # they line up with is the install's too (as Nimbus builds it).
        zone = solver_shared.LOCAL_TZ
        grid_times = [datetime(2026, 9, 4, h, 0, tzinfo=zone) for h in range(4)]
        result = solver_writer.resample_forecast(forecast, "value", grid_times)
        # Nearest-at-or-before: 00:00->1.0, 01:00->1.0 (still before 02:00),
        # 02:00->3.0, 03:00->3.0.
        self.assertEqual(result, [1.0, 1.0, 3.0, 3.0])
