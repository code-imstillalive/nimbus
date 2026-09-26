"""nimbus issue #467 item 4: reading a real HA `calendar.*` entity.

## Why this needed no new mechanism

`calendar.get_events` is a response-returning service, and this module already
reaches those through `ha_call_service_with_response()` — which bridges the
native in-process path and the standalone/cron REST path, exactly as
`publish_weather_forecast_mirrors()` does for `weather.get_forecasts`. So the
fetch is a mapping job, not a plumbing job.

## What is actually worth testing here

Not the happy path on its own. **A fetch that silently returns nothing is how
this whole feature becomes inert** — which is precisely how HAEO's #361 died
("functionally inert with zero capacity" over schema that read correctly). So
the majority of these tests are about the failure shapes, and specifically about
which ones are allowed to be silent.

`fetch_calendar_trips()` returns `[]` on **any** failure and never raises. That is
deliberate: a calendar is an optional convenience, and letting a missing or
renamed entity break the solve would make it a single point of failure for real
battery dispatch. The cost of that choice is that "no trips" and "the calendar
is broken" look identical from the outside — so the *resolution* layer is where
the loud warning lives (an event with no distance is reported), and this layer
degrades quietly by design.

## The bug this file would have caught

`_safe_fromisoformat()` can return a **naive** datetime, and a calendar provider
genuinely emits offset-less strings — an all-day event especially. Every consumer
downstream compares these against tz-aware `grid_times`, and that comparison
raises `TypeError` deep inside the resolution rather than anywhere near the
parse. That is the exact shape **#363** documents for solar sources, where it
took down an entire solve cycle instead of dropping one source. So the fetch
normalises naive values to UTC, and `TestNaiveTimestampsCannotReachTheSolver`
pins it.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

_AEST = timezone(timedelta(hours=10))
_START = datetime(2026, 9, 27, 0, 0, tzinfo=_AEST)
_END = _START + timedelta(days=1)
_ENTITY = "calendar.ev_trips"


def _fetch(response):
    with mock.patch.object(
        solver_writer, "ha_call_service_with_response", return_value=response
    ) as called:
        trips = solver_writer.fetch_calendar_trips(_ENTITY, _START, _END)
    return trips, called


def _event(start: str, end: str, summary: str = "", description: str = "") -> dict:
    return {
        "start": start,
        "end": end,
        "summary": summary,
        "description": description,
    }


class TestTheHappyPath(unittest.TestCase):
    def test_events_become_trip_events(self):
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        _event(
                            "2026-09-27T08:00:00+10:00",
                            "2026-09-27T17:00:00+10:00",
                            "Trip to Brisbane 120 km",
                        )
                    ]
                }
            }
        )
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].summary, "Trip to Brisbane 120 km")
        self.assertEqual(trips[0].start.hour, 8)
        self.assertIsNotNone(trips[0].start.tzinfo)

    def test_the_service_is_asked_for_the_right_window(self):
        """The horizon bounds have to reach the service, or it returns whatever
        its own default window happens to be -- which would quietly plan for
        trips outside the solve."""
        _, called = _fetch({_ENTITY: {"events": []}})
        (domain, service, data), _kwargs = called.call_args
        self.assertEqual((domain, service), ("calendar", "get_events"))
        self.assertEqual(data["entity_id"], _ENTITY)
        self.assertEqual(data["start_date_time"], _START.isoformat())
        self.assertEqual(data["end_date_time"], _END.isoformat())

    def test_the_description_is_carried_through(self):
        """Distance may live in either field -- the resolution layer checks the
        summary first, then the description. Dropping it here would make that
        fallback unreachable."""
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        _event(
                            "2026-09-27T08:00:00+10:00",
                            "2026-09-27T09:00:00+10:00",
                            "Brisbane",
                            "220 km each way",
                        )
                    ]
                }
            }
        )
        self.assertEqual(trips[0].description, "220 km each way")


class TestNaiveTimestampsCannotReachTheSolver(unittest.TestCase):
    """#363's shape, applied to calendars."""

    def test_an_offset_less_event_is_normalised_to_utc(self):
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        _event("2026-09-27T08:00:00", "2026-09-27T09:00:00", "40 km")
                    ]
                }
            }
        )
        self.assertEqual(len(trips), 1)
        self.assertIsNotNone(
            trips[0].start.tzinfo, "a naive start would raise TypeError downstream"
        )
        self.assertIsNotNone(trips[0].end.tzinfo)
        self.assertEqual(trips[0].start.tzinfo, UTC)

    def test_a_normalised_event_is_actually_comparable(self):
        """The assertion that matters -- `tzinfo is not None` is not the point,
        surviving the comparison is. This is the exact operation that raised
        deep inside the resolution in #363's solar case."""
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        _event("2026-09-27T08:00:00", "2026-09-27T09:00:00", "40 km")
                    ]
                }
            }
        )
        # No exception is the test.
        self.assertIsInstance(trips[0].start < _END, bool)


class TestEveryFailureShapeDegradesToNoTrips(unittest.TestCase):
    """A calendar must never be able to break a solve. The cost of that choice
    is that these all look alike from outside, which is why the loud warning
    lives in the resolution layer instead."""

    def test_no_response_at_all(self):
        trips, _ = _fetch(None)
        self.assertEqual(trips, [])

    def test_a_response_that_is_not_a_dict(self):
        trips, _ = _fetch(["unexpected"])
        self.assertEqual(trips, [])

    def test_a_response_for_a_different_entity(self):
        """A renamed calendar, which is a real and easy mistake to make."""
        trips, _ = _fetch({"calendar.something_else": {"events": []}})
        self.assertEqual(trips, [])

    def test_a_payload_with_no_events_key(self):
        trips, _ = _fetch({_ENTITY: {}})
        self.assertEqual(trips, [])

    def test_events_that_are_not_a_list(self):
        trips, _ = _fetch({_ENTITY: {"events": "nope"}})
        self.assertEqual(trips, [])

    def test_the_service_raising_is_already_swallowed_upstream(self):
        """`ha_call_service_with_response()` returns None rather than raising --
        its own documented contract. This pins that this function relies on that
        rather than adding a second try/except that would hide a real change in
        that contract."""
        with mock.patch.object(
            solver_writer, "ha_call_service_with_response", return_value=None
        ):
            self.assertEqual(
                solver_writer.fetch_calendar_trips(_ENTITY, _START, _END), []
            )


class TestOneBadEventDoesNotDiscardTheRest(unittest.TestCase):
    """The difference between a provider quirk and a lost trip."""

    def test_a_malformed_event_is_skipped_individually(self):
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        _event("not-a-date", "also-not", "broken 50 km"),
                        _event(
                            "2026-09-27T08:00:00+10:00",
                            "2026-09-27T09:00:00+10:00",
                            "good 120 km",
                        ),
                    ]
                }
            }
        )
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].summary, "good 120 km")

    def test_a_non_dict_entry_is_skipped(self):
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        "junk",
                        _event(
                            "2026-09-27T08:00:00+10:00",
                            "2026-09-27T09:00:00+10:00",
                            "good 40 km",
                        ),
                    ]
                }
            }
        )
        self.assertEqual(len(trips), 1)

    def test_an_event_missing_its_times_is_skipped_not_defaulted(self):
        """Defaulting a missing start to "now" would invent a departure."""
        trips, _ = _fetch({_ENTITY: {"events": [{"summary": "60 km"}]}})
        self.assertEqual(trips, [])

    def test_a_missing_summary_becomes_empty_rather_than_None(self):
        """The resolution layer does `if not text: continue`, so None would work
        by luck; an empty string is what its signature actually says."""
        trips, _ = _fetch(
            {
                _ENTITY: {
                    "events": [
                        {
                            "start": "2026-09-27T08:00:00+10:00",
                            "end": "2026-09-27T09:00:00+10:00",
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].summary, "")
        self.assertEqual(trips[0].description, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
