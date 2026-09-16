"""nimbus issue #983: the daily sensors survive a restart.

Reported from a real install: the counterfactual and its siblings
"vanish way too much". The cause is that `_NimbusSolverPushSensor`
starts every life with `_state = None` and waits for a push. For the
~30-second solver sensors that self-heals in seconds. For the four
sensors computed once per SCORED DAY it does not -- a restart can blank
a perfectly valid figure until the next daily run, up to 24 hours later.

The fix is opt-in rather than universal, and that asymmetry is the whole
design:

* **Daily sensors restore.** Their value is a property of *yesterday*,
  so re-adopting it after a restart is honest.
* **Live solver sensors do not.** A value that outlives the solver is
  exactly what `_STALE_AFTER_SECONDS` exists to expose; pre-filling one
  at startup would hide a solver that never came back.

The restore also deliberately leaves `_last_updated` as `None`. A
restored value is genuinely old, and stamping it fresh would let it
satisfy a staleness check it has not earned.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor as sensor_module


class _FakeLastState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


def _build(cls, last_state=None):
    obj = object.__new__(cls)
    obj._state = None
    obj._attrs = {}
    obj._last_updated = None
    obj._sw_version = "0.94.341"

    async def _get_last_state():
        return last_state

    obj.async_get_last_state = _get_last_state
    return obj


async def _restore(obj):
    await obj._async_restore_last_value()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestWhichSensorsOptIn(unittest.TestCase):
    """The asymmetry is the design, so it is asserted rather than left
    to the comments."""

    def test_the_four_daily_sensors_restore(self):
        for cls in (
            sensor_module.NimbusSolverQualityReportSensor,
            sensor_module.NimbusCounterfactualSocSensor,
            sensor_module.NimbusEfficiencyBacktestSensor,
            sensor_module.NimbusFlexReportSensor,
        ):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls._RESTORE_ACROSS_RESTART)

    def test_the_live_solver_sensors_do_not(self):
        """Restoring these would hide a solver that never came back --
        the precise failure _STALE_AFTER_SECONDS exists to surface."""
        for cls in (
            sensor_module.NimbusSolverBatteryForecastSensor,
            sensor_module.NimbusHouseholdLoadTotalForecastSensor,
            sensor_module.NimbusDispatchDryRunSensor,
        ):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(cls._RESTORE_ACROSS_RESTART)

    def test_the_default_is_off(self):
        """A sensor added later must not silently inherit restoring."""
        self.assertFalse(sensor_module._NimbusSolverPushSensor._RESTORE_ACROSS_RESTART)


class TestRestoringARealValue(unittest.TestCase):
    def setUp(self):
        self.obj = _build(
            sensor_module.NimbusSolverQualityReportSensor,
            _FakeLastState(
                "103.66",
                {
                    "latest_date": "2026-09-15",
                    "regret_dollars": -0.7307,
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                    "friendly_name": "stale name",
                    "nimbus_version": "0.94.330",
                },
            ),
        )
        _run(_restore(self.obj))

    def test_the_value_comes_back(self):
        self.assertEqual(self.obj._state, 103.66)

    def test_the_report_attributes_come_back(self):
        """An EPR with no latest_date or regret beside it is not much use
        to a dashboard that was showing all three a moment earlier."""
        self.assertEqual(self.obj._attrs["latest_date"], "2026-09-15")
        self.assertEqual(self.obj._attrs["regret_dollars"], -0.7307)

    def test_ha_managed_metadata_is_not_restored(self):
        """Unit, classes and name come from the class definition.
        Restoring a stale copy is how a renamed sensor ends up
        advertising last week's metadata."""
        for key in ("unit_of_measurement", "state_class", "friendly_name"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.obj._attrs)

    def test_the_stale_version_stamp_is_not_restored(self):
        """#972's whole point is that nimbus_version describes the
        RUNNING install. Restoring 0.94.330 into a 0.94.341 process would
        make the one field built to be trustworthy lie."""
        self.assertNotIn("nimbus_version", self.obj._attrs)

    def test_the_freshness_stamp_is_left_unset(self):
        """A restored value is old. Stamping it fresh would let it pass a
        staleness check it has not earned."""
        self.assertIsNone(self.obj._last_updated)


class TestRestoreRefusesWhatItCannotTrust(unittest.TestCase):
    def _state_after(self, last_state):
        obj = _build(sensor_module.NimbusCounterfactualSocSensor, last_state)
        _run(_restore(obj))
        return obj._state

    def test_no_previous_state_leaves_the_sensor_blank(self):
        self.assertIsNone(self._state_after(None))

    def test_unknown_and_unavailable_are_not_restored(self):
        for bad in ("unknown", "unavailable", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(self._state_after(_FakeLastState(bad)))

    def test_a_non_numeric_state_is_not_restored(self):
        self.assertIsNone(self._state_after(_FakeLastState("not a number")))

    def test_a_failing_restore_never_blocks_setup(self):
        """Pre-#983 behaviour was 'start blank'. A restore that throws
        must degrade to exactly that, not to a broken entity."""
        obj = _build(sensor_module.NimbusCounterfactualSocSensor)

        async def _boom():
            raise RuntimeError("recorder unavailable")

        obj.async_get_last_state = _boom
        _run(_restore(obj))
        self.assertIsNone(obj._state)


if __name__ == "__main__":
    unittest.main()
