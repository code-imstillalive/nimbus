"""nimbus issue #843, option A (Mark Purcell's own A/B/C steer,
2026-09-13: "Option C -- both ... then A (per-row unit scaling, dropping
no_attributes/minimal_response on the participant power-history fetch)
scoped only to that path").

`_kw_scale_factor()` reads a sensor's CURRENT LIVE `unit_of_measurement`
exactly once and applies that single scale across a whole day of fetched
history. That is structurally unable to see a sensor whose unit changes
mid-window -- which is real, not hypothetical. Mark's EV pack sensor
reports `W` for ~80 seconds as the car wakes from sleep, then `kW`:

    08:51:04.410  state=1514.417   unit="W"     <- really 1.514417 kW
    08:51:30.004  state=-774.818   (still W)    <- really -0.774818 kW
    08:52:27.078  state=-0.774994  unit="kW"
    08:52:30.002  state=-0.42282   unit="kW"

A live check returning "kW" scaled that 1514.417 by 1.0, and the quality
report published ~1,500 kW of achieved battery power against a real ~80 kW
fleet ceiling.

`fetch_entity_power_history_kw()` fixes it at source by scaling each row
by its own recorded unit. These tests drive the REST branch, which is the
half reachable without a live `hass` object.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY = datetime(2026, 9, 13, 0, 0, tzinfo=BRISBANE)


def _iso(hour: float) -> str:
    return (DAY + timedelta(hours=hour)).isoformat()


def _row(hour: float, state, unit="kW"):
    row = {"state": str(state), "last_changed": _iso(hour)}
    if unit is not None:
        row["attributes"] = {"unit_of_measurement": unit}
    return row


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _fetch(rows):
    """Drive the REST branch of fetch_entity_power_history_kw()."""
    with (
        mock.patch.object(solver_writer, "_NATIVE_HASS", None),
        mock.patch.object(solver_writer, "_load_token", lambda: "tok"),
        mock.patch.object(
            solver_writer.urllib.request, "urlopen", lambda *a, **k: _Resp([rows])
        ),
    ):
        return solver_writer.fetch_entity_power_history_kw(
            "sensor.garage_my_t_my_pack_power", DAY, DAY + timedelta(days=1)
        )


class TestMarksRealWakeTransient(unittest.TestCase):
    """The exact four rows from the confirmed 13 Sep incident."""

    def test_w_rows_are_scaled_and_kw_rows_are_not(self):
        got = _fetch(
            [
                _row(8.851, 1514.417, "W"),
                _row(8.858, -774.818, "W"),
                _row(8.874, -0.774994, "kW"),
                _row(8.875, -0.42282, "kW"),
            ]
        )
        values = [round(v, 6) for _t, v in got]
        self.assertEqual(
            values,
            [1.514417, -0.774818, -0.774994, -0.42282],
            "the two W rows must become kW; the two genuine kW rows must "
            "pass through untouched -- a single whole-window scale cannot "
            "produce this result, which is the entire point of #843 option A",
        )

    def test_the_headline_row_lands_within_a_real_ev_envelope(self):
        # 1514.417 W == 1.514417 kW, comfortably inside a 25 kW EV --
        # where the old single-live-scale path made it 1514.417 kW.
        ((_t, v),) = _fetch([_row(8.851, 1514.417, "W")])
        self.assertAlmostEqual(v, 1.514417, places=6)
        self.assertLess(v, 25.0)


class TestUnitHandling(unittest.TestCase):
    def test_a_row_with_no_unit_is_taken_as_kw(self):
        # Matches _kw_scale_factor()'s own documented default for the
        # same case -- kW was always the original assumption.
        ((_t, v),) = _fetch([_row(9, 7.5, None)])
        self.assertEqual(v, 7.5)

    def test_an_unrecognised_unit_is_taken_as_kw(self):
        # Only the one real, confirmed mismatch (W) is corrected; this
        # does not guess at every unit HA's power device class allows.
        ((_t, v),) = _fetch([_row(9, 7.5, "kWh")])
        self.assertEqual(v, 7.5)

    def test_negative_w_values_scale_correctly(self):
        ((_t, v),) = _fetch([_row(9, -774.818, "W")])
        self.assertAlmostEqual(v, -0.774818, places=6)


class TestDegradesSafely(unittest.TestCase):
    def test_unusable_states_are_skipped_not_crashed_on(self):
        got = _fetch(
            [
                _row(9, "unavailable", "W"),
                _row(10, "unknown", "kW"),
                _row(11, 5.0, "kW"),
            ]
        )
        self.assertEqual([v for _t, v in got], [5.0])

    def test_non_numeric_state_is_skipped(self):
        got = _fetch([_row(9, "heating", "W"), _row(10, 5.0, "kW")])
        self.assertEqual([v for _t, v in got], [5.0])

    def test_empty_payload_returns_empty(self):
        self.assertEqual(_fetch([]), [])

    def test_a_failed_request_returns_empty_rather_than_raising(self):
        def _boom(*_a, **_k):
            raise solver_writer.urllib.error.URLError("down")

        with (
            mock.patch.object(solver_writer, "_NATIVE_HASS", None),
            mock.patch.object(solver_writer, "_load_token", lambda: "tok"),
            mock.patch.object(solver_writer.urllib.request, "urlopen", _boom),
        ):
            self.assertEqual(
                solver_writer.fetch_entity_power_history_kw(
                    "sensor.x", DAY, DAY + timedelta(days=1)
                ),
                [],
            )

    def test_rows_come_back_chronologically(self):
        got = _fetch([_row(11, 3.0, "kW"), _row(9, 1.0, "kW"), _row(10, 2.0, "kW")])
        self.assertEqual([v for _t, v in got], [1.0, 2.0, 3.0])


class TestScopedToThisPathOnly(unittest.TestCase):
    """The scoping is the reason option A was affordable at all."""

    def test_the_rest_url_keeps_attributes(self):
        seen = {}

        def _capture(req, *_a, **_k):
            seen["url"] = req.full_url
            return _Resp([[]])

        with (
            mock.patch.object(solver_writer, "_NATIVE_HASS", None),
            mock.patch.object(solver_writer, "_load_token", lambda: "tok"),
            mock.patch.object(solver_writer.urllib.request, "urlopen", _capture),
        ):
            solver_writer.fetch_entity_power_history_kw(
                "sensor.x", DAY, DAY + timedelta(days=1)
            )
        self.assertNotIn(
            "minimal_response",
            seen["url"],
            "this fetch must NOT strip attributes -- per-row units are "
            "the whole reason it exists",
        )

    def test_the_cheap_shared_fetch_still_strips_attributes(self):
        # fetch_entity_history_range() is used by every other history
        # read in this file and must stay on the cheap path.
        seen = {}

        def _capture(req, *_a, **_k):
            seen["url"] = req.full_url
            return _Resp([[]])

        with (
            mock.patch.object(solver_writer, "_NATIVE_HASS", None),
            mock.patch.object(solver_writer, "_load_token", lambda: "tok"),
            mock.patch.object(solver_writer.urllib.request, "urlopen", _capture),
        ):
            solver_writer.fetch_entity_history_range(
                "sensor.x", DAY, DAY + timedelta(days=1)
            )
        self.assertIn("minimal_response", seen["url"])

    def test_kw_scale_factor_itself_is_untouched(self):
        # Still the right answer for the seven other callers that do not
        # fetch per-row units.
        with mock.patch.object(
            solver_writer,
            "ha_get",
            lambda _e: {"attributes": {"unit_of_measurement": "W"}},
        ):
            self.assertEqual(solver_writer._kw_scale_factor("sensor.x"), 0.001)
        with mock.patch.object(
            solver_writer,
            "ha_get",
            lambda _e: {"attributes": {"unit_of_measurement": "kW"}},
        ):
            self.assertEqual(solver_writer._kw_scale_factor("sensor.x"), 1.0)


if __name__ == "__main__":
    unittest.main()
