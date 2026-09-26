"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1290): `fetch_calendar_trips()` (d4fa8c0, #467 item 4) normalises
EVERY naive timestamp -- including a genuine all-day (date-only) calendar
event -- to UTC via `.replace(tzinfo=UTC)`.

That rule is correct for #363's own case (an external forecast API handing
back a naive-but-genuinely-UTC value). It is wrong for an all-day calendar
event: `calendar.get_events` returns a date-only string for those
(`"2026-09-27"`, the iCalendar `VALUE=DATE` shape HA's own calendar
platforms use), `datetime.fromisoformat("2026-09-27")` parses to naive
midnight, and reading that as UTC midnight shifts it by the household's own
UTC offset -- 10 hours for this household (Brisbane, UTC+10). An all-day
event meant to start at LOCAL midnight on the 27th resolves to 10:00 LOCAL
on the 27th instead.

`tests/test_467_calendar_fetch.py::TestNaiveTimestampsCannotReachTheSolver`
already pins the UTC-normalisation behaviour, but only for a naive *timed*
event (`"2026-09-27T08:00:00"`) -- never a genuine date-only all-day
string, which is the shape that actually triggers this bug. This test
supplies that missing shape and asserts the correct answer: a date-only
event should resolve to LOCAL midnight, not UTC midnight re-expressed in
local time.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

_ENTITY = "calendar.ev_trips"


def _fetch(response):
    with mock.patch.object(
        solver_writer, "ha_call_service_with_response", return_value=response
    ) as called:
        import datetime as _dt

        window_start = _dt.datetime(2026, 9, 26, 0, 0, tzinfo=solver_writer.LOCAL_TZ)
        window_end = window_start + timedelta(days=5)
        trips = solver_writer.fetch_calendar_trips(_ENTITY, window_start, window_end)
    return trips, called


def _all_day_event(start_date: str, end_date: str, summary: str = "") -> dict:
    # calendar.get_events' own shape for an all-day entry: date-only
    # strings, no time component at all.
    return {"start": start_date, "end": end_date, "summary": summary, "description": ""}


class TestAllDayEventResolvesToLocalMidnight(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1290: all-day calendar events are read as UTC midnight, "
        "not local midnight -- shifts by the household's own UTC offset",
        strict=True,
    )
    def test_an_all_day_event_starts_at_local_midnight_not_utc_midnight(self):
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [_all_day_event("2026-09-27", "2026-09-28", "Road trip")]
                }
            }
        )
        self.assertEqual(len(trips), 1)
        start = trips[0].start
        self.assertIsNotNone(
            start.tzinfo, "a naive start would raise TypeError downstream"
        )

        # The correct answer: local midnight on the 27th.
        local_midnight = start.astimezone(solver_writer.LOCAL_TZ)
        self.assertEqual(
            (local_midnight.hour, local_midnight.minute),
            (0, 0),
            f"an all-day event's LOCAL start should be 00:00, got "
            f"{local_midnight.hour:02d}:{local_midnight.minute:02d} -- "
            f"the naive date was read as UTC midnight ({start.isoformat()}) "
            f"and shifted by the household's own UTC offset instead of "
            f"being anchored to local midnight",
        )
        self.assertEqual(local_midnight.date().isoformat(), "2026-09-27")


if __name__ == "__main__":
    unittest.main()
