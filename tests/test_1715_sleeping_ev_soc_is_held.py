"""nimbus #1715 (a tester's install, 11 Oct 2026): a sleeping car's SoC sensor
reads `unavailable`. The participant used to be planned at its MIN SoC, as if
empty, and that model ran for minutes or failed in HiGHS until the car woke.

Now the last SoC read is held for up to _SOC_HOLD_SECONDS; with none to hold
the participant sits out the solve rather than being planned empty.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import extra_batteries as eb

_SOC = "sensor.herbie_battery"
_DATA = {
    "battery_participant_name": "herbie",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": _SOC,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
}


def _hass(soc_state: str | None):
    entry = SimpleNamespace(
        subentries={
            "car": SimpleNamespace(
                subentry_id="car", subentry_type="battery_participant", data=_DATA
            )
        }
    )
    states = {}
    if soc_state is not None:
        states[_SOC] = SimpleNamespace(entity_id=_SOC, state=soc_state, attributes={})
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        states=SimpleNamespace(get=lambda eid: states.get(eid)),
    )


class TestSleepingCar(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer.NATIVE.hass
        eb._LAST_GOOD_SOC.clear()
        eb._SOC_UNREADABLE_WARNED.clear()

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig
        eb._LAST_GOOD_SOC.clear()
        eb._SOC_UNREADABLE_WARNED.clear()

    def _build(self, soc_state):
        solver_writer.NATIVE.hass = _hass(soc_state)
        return eb.build_extra_batteries()

    def test_an_unreadable_soc_holds_the_last_good_one_not_min(self):
        (awake,) = self._build("70")
        self.assertAlmostEqual(awake.initial_soc_kwh, 42.0)
        with self.assertLogs(eb.solver_shared._LOGGER, "WARNING") as log:
            (asleep,) = self._build("unavailable")
        self.assertAlmostEqual(asleep.initial_soc_kwh, 42.0)  # not 12.0 (min)
        self.assertTrue(any("#1715" in m and "holding" in m for m in log.output))

    def test_with_nothing_to_hold_the_car_sits_out_rather_than_planned_empty(self):
        with self.assertLogs(eb.solver_shared._LOGGER, "WARNING") as log:
            batteries = self._build("unavailable")
        self.assertEqual(batteries, [])
        self.assertTrue(any("leaving it out" in m for m in log.output))

    def test_a_missing_state_is_treated_the_same(self):
        self.assertEqual(self._build(None), [])

    def test_the_hold_expires(self):
        self._build("70")
        pct, read_at = eb._LAST_GOOD_SOC["car"]
        eb._LAST_GOOD_SOC["car"] = (pct, read_at - eb._SOC_HOLD_SECONDS - 1)
        self.assertEqual(self._build("unavailable"), [])

    def test_it_warns_once_per_unreadable_spell(self):
        self._build("70")
        with self.assertLogs(eb.solver_shared._LOGGER, "WARNING") as first:
            self._build("unavailable")
        self.assertEqual(len([m for m in first.output if "#1715" in m]), 1)
        # safe_num() still logs its own line each cycle, so capture and count.
        with self.assertLogs(eb.solver_shared._LOGGER, "WARNING") as again:
            self._build("unavailable")
        self.assertEqual([m for m in again.output if "#1715" in m], [])
        # Waking clears the spell, so the next sleep warns again.
        self._build("65")
        self.assertNotIn("car", eb._SOC_UNREADABLE_WARNED)


if __name__ == "__main__":
    unittest.main()
