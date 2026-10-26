"""A thermal load configuration that cannot work is refused when built (#774)
(#1837).
"""

from __future__ import annotations

import unittest

from solver.elements import ThermalLoadConfig


def _base_thermal(**overrides) -> dict:
    defaults = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 45.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": 23,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.5,
    }
    defaults.update(overrides)
    return defaults


class TestMaxPowerKwValidation(unittest.TestCase):
    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            ThermalLoadConfig(**_base_thermal(max_power_kw=-1.0))


class TestHeatingRateValidation(unittest.TestCase):
    def test_negative_heating_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            ThermalLoadConfig(**_base_thermal(heating_rate_c_per_kwh=-2.0))


class TestIdleDecayValidation(unittest.TestCase):
    def test_negative_idle_decay_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(idle_decay_c_per_hour=-0.1))
        self.assertIn("idle_decay_c_per_hour", str(ctx.exception))


class TestPeriodOrderingValidation(unittest.TestCase):
    def test_deadline_before_earliest_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(earliest_period=10, deadline_period=5))
        self.assertIn("deadline_period", str(ctx.exception))

    def test_negative_earliest_period_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(earliest_period=-1, deadline_period=5))
        self.assertIn("earliest_period", str(ctx.exception))


class TestTemperatureBoundsValidation(unittest.TestCase):
    def test_zero_max_temperature_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(max_temperature_c=0.0))
        self.assertIn("max_temperature_c", str(ctx.exception))

    def test_target_above_max_temperature_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(
                **_base_thermal(target_temperature_c=100.0, max_temperature_c=99.0)
            )
        self.assertIn("target_temperature_c", str(ctx.exception))

    def test_negative_initial_temperature_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(initial_temperature_c=-1.0))
        self.assertIn("initial_temperature_c", str(ctx.exception))


class TestComfortFloorValidation(unittest.TestCase):
    def test_comfort_floor_above_target_is_rejected(self):
        # The comfort floor is tier 3, subordinate to the hard target
        # (tier 1) -- it must never ask for more than the real guarantee
        # itself demands.
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(
                **_base_thermal(target_temperature_c=60.0, comfort_floor_c=65.0)
            )
        self.assertIn("comfort_floor_c", str(ctx.exception))

    def test_negative_comfort_floor_is_rejected(self):
        with self.assertRaises(ValueError):
            ThermalLoadConfig(**_base_thermal(comfort_floor_c=-1.0))

    def test_negative_comfort_floor_cost_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(
                **_base_thermal(comfort_floor_c=40.0, comfort_floor_cost=-1.0)
            )
        self.assertIn("comfort_floor_cost", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
