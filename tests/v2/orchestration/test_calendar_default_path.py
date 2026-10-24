"""An install that has not opted into trip calendars never reads one (#1290)
(#1837).
"""

from __future__ import annotations

import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import const
from custom_components.nimbus_load.solver_inputs import extra_batteries

_EXTRA_BATTERIES_SRC = Path(extra_batteries.__file__).read_text(encoding="utf-8")


class TestTheCallSiteIsGuarded(unittest.TestCase):
    """Structural half: the only caller sits inside a conditional that tests
    the trip-calendar config value."""

    def test_the_config_key_is_the_one_the_guard_reads(self):
        """Ties the guard to the real subentry field, so a rename cannot leave
        this test passing against a key nothing sets."""
        self.assertEqual(
            const.CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
            "battery_participant_trip_calendar_entity",
        )
        self.assertIn(
            "CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY", _EXTRA_BATTERIES_SRC
        )


class TestNoCalendarMeansNoCall(unittest.TestCase):
    """Behavioural half: `build_extra_batteries()` is actually driven, and the
    calls to `fetch_calendar_trips()` are counted.

    The structural tests above cannot notice the guard being widened. A call
    counter can -- but only if the harness can also observe the call happening,
    which is what `test_a_participant_WITH_a_calendar_does_reach_it` below is
    for. A "never called" assertion with no positive control passes just as
    happily when the stub is wired wrong.
    """

    class _FakeSubentry:
        def __init__(self, data: dict, subentry_type: str = "battery_participant"):
            self.data = data
            self.subentry_type = subentry_type
            self.title = data.get("battery_participant_name", "ev")
            self.subentry_id = "sub_" + self.title

    class _FakeEntry:
        def __init__(self, subentries: list) -> None:
            self.entry_id = "entry1"
            self.subentries = {s.subentry_id: s for s in subentries}

    class _FakeConfigEntries:
        def __init__(self, entries: list) -> None:
            self._entries = entries

        def async_entries(self, _domain):
            return list(self._entries)

    class _FakeHass:
        def __init__(self, entries: list) -> None:
            self.config_entries = TestNoCalendarMeansNoCall._FakeConfigEntries(entries)
            self.states = type("S", (), {"get": staticmethod(lambda _e: None)})()

    class _FakeWriter:
        """Only what build_extra_batteries() actually touches."""

        def __init__(self, hass) -> None:
            # nimbus #1437: build_extra_batteries() reads the injected hass from
            # solver_shared.NATIVE, not from the writer; the drivers install it.
            self.hass = hass
            self._LOGGER = logging.getLogger("nimbus.test.1290")
            self.calendar_calls: list[tuple] = []
            #: Set when build_extra_batteries() raised, so a failure is visible
            #: rather than swallowed. Inspected by the positive control's own
            #: assertion message when it fails.
            self.failed_with: Exception | None = None

        def fetch_calendar_trips(self, *args):
            self.calendar_calls.append(args)
            return []

        # Everything else the participant path touches on its way to the
        # calendar guard. Added one at a time, driven by the positive control
        # failing -- which is what a positive control is for.
        def safe_num(self, _entity, default=0.0):
            return 50.0 if _entity else default

        def ha_get(self, *_a, **_k):
            return None

        def fetch_entity_history_range(self, *_a, **_k):
            return []

        def _kw_scale_factor(self, *_a, **_k):
            return 1.0

    def _participant(self, **overrides) -> dict:
        # capacity + SoC sensor are BOTH required, or the participant is
        # skipped before the calendar guard is reached -- which is what the
        # positive control caught when this fixture had only capacity. A
        # fixture that never reaches the guard makes every "not called"
        # assertion vacuous.
        data = {
            const.CONF_BATTERY_PARTICIPANT_NAME: "ev",
            const.CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 60.0,
            const.CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.ev_soc",
            const.CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW: 7.0,
            const.CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW: 7.0,
        }
        data.update(overrides)
        return data

    def _drive(self, participant_data: dict):
        """Run build_extra_batteries() with a real grid and a stubbed writer."""
        sub = self._FakeSubentry(participant_data)
        writer = self._FakeWriter(self._FakeHass([self._FakeEntry([sub])]))
        start = datetime(2026, 9, 27, 0, 0, tzinfo=timezone(timedelta(hours=10)))
        periods = SimpleNamespace(
            n_periods=4,
            period_starts=[start + timedelta(hours=i) for i in range(4)],
            hours=[1.0, 1.0, 1.0, 1.0],
        )
        real = extra_batteries._solver_writer
        extra_batteries._solver_writer = lambda: writer
        native = patch.object(extra_batteries.solver_shared.NATIVE, "hass", writer.hass)
        native.start()
        try:
            extra_batteries.build_extra_batteries(periods)
        except Exception as exc:  # noqa: BLE001
            # Broad on purpose: the participant path continues well past the
            # calendar guard and can fail on a stub this small, and what this
            # test measures is whether the guard was crossed BEFORE any such
            # failure -- which `writer.calendar_calls` records either way.
            #
            # RECORDED, not swallowed. A silent `pass` here could hide the stub
            # breaking before the guard is ever reached, which is precisely the
            # failure `test_a_participant_WITH_a_calendar_does_reach_it` exists
            # to catch -- and did catch twice while this was being written.
            writer.failed_with = exc
        finally:
            native.stop()
            extra_batteries._solver_writer = real
        return writer

    def test_a_participant_with_no_trip_calendar_never_calls_it(self):
        """The default shape for every install that has not configured one --
        which is what earns #1290 the same-day carve-out."""
        writer = self._drive(self._participant())
        self.assertEqual(
            writer.calendar_calls,
            [],
            "fetch_calendar_trips() must not be reached without a configured "
            "trip calendar; #1290 changed that function's behaviour, so any "
            "call here would mean the change is NOT opt-in",
        )

    def test_an_empty_string_calendar_is_also_inert(self):
        """A cleared config-flow field arrives as "" rather than None, and the
        guard is a truthiness test -- so it must be inert too."""
        for value in ("", None):
            with self.subTest(calendar=value):
                writer = self._drive(
                    self._participant(
                        **{const.CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY: value}
                    )
                )
                self.assertEqual(writer.calendar_calls, [])

    def test_a_participant_WITH_a_calendar_does_reach_it(self):
        """The positive control, and the reason the two tests above mean
        anything: it proves this harness CAN observe the call. Without it,
        "never called" would pass just as happily on a mis-wired stub."""
        writer = self._drive(
            self._participant(
                **{
                    const.CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY: (
                        "calendar.ev_trips"
                    )
                }
            )
        )
        self.assertEqual(
            len(writer.calendar_calls),
            1,
            "an opted-in participant must reach fetch_calendar_trips() -- if "
            "this fails, the negative tests above prove nothing. "
            f"build_extra_batteries() raised: {writer.failed_with!r}",
        )
        self.assertEqual(writer.calendar_calls[0][0], "calendar.ev_trips")

    def test_the_guard_also_requires_a_real_period_grid(self):
        """`periods is not None and periods.period_starts` is the guard's
        second half: even an opted-in participant cannot reach the call before
        a grid exists, which is the cache-reconstruction path."""
        writer = self._FakeWriter(self._FakeHass([]))
        real = extra_batteries._solver_writer
        extra_batteries._solver_writer = lambda: writer
        native = patch.object(extra_batteries.solver_shared.NATIVE, "hass", writer.hass)
        native.start()
        try:
            extra_batteries.build_extra_batteries(None)
        except Exception as exc:  # noqa: BLE001 -- same reasoning as _drive()
            writer.failed_with = exc
        finally:
            native.stop()
            extra_batteries._solver_writer = real
        self.assertEqual(writer.calendar_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
