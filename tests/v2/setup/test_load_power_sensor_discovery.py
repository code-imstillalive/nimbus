"""A controllable load's device power sensor is found by discovery when none is
configured (#768) (#1837).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import solver_writer


class _FakeRegistryEntry:
    def __init__(self, entity_id, device_id, device_class=None, original=None):
        self.entity_id = entity_id
        self.device_id = device_id
        self.device_class = device_class
        self.original_device_class = original

    @property
    def domain(self):
        return self.entity_id.split(".", 1)[0]


class _FakeRegistry:
    def __init__(self, entries):
        self._by_id = {e.entity_id: e for e in entries}
        self._entries = entries

    def async_get(self, entity_id):
        return self._by_id.get(entity_id)

    def entries_for_device(self, device_id):
        return [e for e in self._entries if e.device_id == device_id]


class _FakeErModule:
    """Stands in for homeassistant.helpers.entity_registry."""

    def __init__(self, registry):
        self._registry = registry

    def async_get(self, _hass):
        return self._registry

    def async_entries_for_device(self, registry, device_id, include_disabled_entities):
        assert include_disabled_entities is False, (
            "disabled entities must be excluded -- a disabled power sensor "
            "publishes no history to learn from"
        )
        return registry.entries_for_device(device_id)


class _DiscoveryTestBase(unittest.TestCase):
    def setUp(self):
        solver_writer._POWER_SENSOR_DISCOVERY_LOGGED.clear()

    def _run(self, entries, device_entity):
        """Drive the real resolver against a fake registry, with
        _NATIVE_HASS present so the native-only guard passes."""
        registry = _FakeRegistry(entries)
        fake_er = _FakeErModule(registry)
        import sys
        import types

        helpers = types.ModuleType("homeassistant.helpers")
        helpers.entity_registry = fake_er
        ha = types.ModuleType("homeassistant")
        ha.helpers = helpers
        saved = {
            k: sys.modules.get(k) for k in ("homeassistant", "homeassistant.helpers")
        }
        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.helpers"] = helpers
        try:
            with patch.object(solver_writer.NATIVE, "hass", object()):
                return solver_writer._discover_power_sensor_for_device(device_entity)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestDiscoversTheRealDevicePair(_DiscoveryTestBase):
    def test_marks_real_case_water_heater_to_its_own_power_sensor(self):
        """The exact shape from #768: a water_heater and its power sensor
        on one MQTT-discovered device."""
        entries = [
            _FakeRegistryEntry("water_heater.wwk302", "dev_hws"),
            _FakeRegistryEntry("sensor.wwk302_power", "dev_hws", original="power"),
            _FakeRegistryEntry(
                "sensor.wwk302_temperature", "dev_hws", original="temperature"
            ),
        ]
        self.assertEqual(
            self._run(entries, "water_heater.wwk302"), "sensor.wwk302_power"
        )

    def test_discovery_is_by_device_not_by_name(self):
        """The core anti-regression: a sensor named exactly like the
        device but belonging to a DIFFERENT device must not be picked.
        A name-shaped implementation would pass every other test in this
        file and fail this one."""
        entries = [
            _FakeRegistryEntry("water_heater.wwk302", "dev_hws"),
            # Same naming, different physical device.
            _FakeRegistryEntry(
                "sensor.wwk302_power", "dev_somethingelse", original="power"
            ),
        ]
        self.assertIsNone(self._run(entries, "water_heater.wwk302"))


class TestRefusesToGuess(_DiscoveryTestBase):
    def test_no_power_sensor_on_the_device_returns_none(self):
        entries = [
            _FakeRegistryEntry("water_heater.hws", "dev"),
            _FakeRegistryEntry("sensor.hws_temperature", "dev", original="temperature"),
        ]
        self.assertIsNone(self._run(entries, "water_heater.hws"))

    def test_multiple_power_sensors_refuses_rather_than_picking_one(self):
        """A device exposing per-phase power is a real shape. Silently
        picking one would be the same confidently-wrong class as #118's
        $46/day misplan -- refuse and say so."""
        entries = [
            _FakeRegistryEntry("water_heater.hws", "dev"),
            _FakeRegistryEntry("sensor.hws_power_l1", "dev", original="power"),
            _FakeRegistryEntry("sensor.hws_power_l2", "dev", original="power"),
        ]
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as caught:
            result = self._run(entries, "water_heater.hws")
        self.assertIsNone(result)
        self.assertTrue(
            any("refusing to guess" in m for m in caught.output), caught.output
        )

    def test_an_entity_with_no_device_returns_none(self):
        entries = [_FakeRegistryEntry("water_heater.hws", None)]
        self.assertIsNone(self._run(entries, "water_heater.hws"))

    def test_non_sensor_domains_are_ignored(self):
        """A `switch` or `binary_sensor` carrying device_class=power would
        not be a power MEASUREMENT, so the domain filter matters."""
        entries = [
            _FakeRegistryEntry("water_heater.hws", "dev"),
            _FakeRegistryEntry("binary_sensor.hws_power", "dev", original="power"),
        ]
        self.assertIsNone(self._run(entries, "water_heater.hws"))


class TestNativeOnlyAndExplicitOverride(unittest.TestCase):
    def setUp(self):
        solver_writer._POWER_SENSOR_DISCOVERY_LOGGED.clear()

    def test_an_explicit_setting_always_wins_over_discovery(self):
        """Discovery fills a gap; it must never override a household's own
        stated answer. Asserted by making discovery raise -- if the
        resolver consulted it at all, this test would error rather than
        pass."""

        def _boom(_entity):
            raise AssertionError("discovery must not run when the field is set")

        with patch.object(solver_writer, "_discover_power_sensor_for_device", _boom):
            resolved = solver_writer.resolve_controllable_load_power_sensor(
                {
                    "controllable_load_power_sensor": "sensor.explicitly_chosen",
                    "controllable_load_device_entity": "water_heater.hws",
                }
            )
        self.assertEqual(resolved, "sensor.explicitly_chosen")

    def test_falls_through_to_discovery_when_unset(self):
        with patch.object(
            solver_writer,
            "_discover_power_sensor_for_device",
            lambda e: "sensor.discovered",
        ):
            resolved = solver_writer.resolve_controllable_load_power_sensor(
                {"controllable_load_device_entity": "water_heater.hws"}
            )
        self.assertEqual(resolved, "sensor.discovered")


if __name__ == "__main__":
    unittest.main()
