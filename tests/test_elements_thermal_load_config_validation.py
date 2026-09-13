"""nimbus issue #774: real validation tests for the new ThermalLoadConfig
(solver/elements.py) -- the hard-constrained thermal LP redesign for the
Hot Water Heat Pump controllable load's own "must heat every day"
guarantee (the maintainer's own EMHASS-`docs/thermal_model.md`-inspired
proposal on #774, made real). Mirrors test_elements_battery_config_
validation.py's own style: one class per real, distinct validation gap.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
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


class TestThermalLoadConfigValidPasses(unittest.TestCase):
    def test_a_well_formed_config_constructs_cleanly(self):
        cfg = ThermalLoadConfig(**_base_thermal())
        self.assertEqual(cfg.name, "hws")
        self.assertIsNone(cfg.comfort_floor_c)
        self.assertEqual(cfg.comfort_floor_cost, 0.0)
        self.assertEqual(cfg.max_temperature_c, 99.0)

    def test_comfort_floor_equal_to_target_is_allowed(self):
        # "subordinate to, never above" the hard target -- equal is the
        # boundary case, still valid.
        cfg = ThermalLoadConfig(**_base_thermal(comfort_floor_c=60.0))
        self.assertEqual(cfg.comfort_floor_c, 60.0)


class TestMaxPowerKwValidation(unittest.TestCase):
    def test_zero_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(max_power_kw=0.0))
        self.assertIn("max_power_kw", str(ctx.exception))

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            ThermalLoadConfig(**_base_thermal(max_power_kw=-1.0))


class TestHeatingRateValidation(unittest.TestCase):
    def test_zero_heating_rate_is_rejected(self):
        # A zero rate would mean this load can NEVER raise temperature at
        # all -- the hard deadline constraint would be unconditionally
        # infeasible for any real deficit, a real modelling error worth
        # catching here rather than as an opaque LP infeasibility.
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(heating_rate_c_per_kwh=0.0))
        self.assertIn("heating_rate_c_per_kwh", str(ctx.exception))

    def test_negative_heating_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            ThermalLoadConfig(**_base_thermal(heating_rate_c_per_kwh=-2.0))


class TestIdleDecayValidation(unittest.TestCase):
    def test_negative_idle_decay_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(idle_decay_c_per_hour=-0.1))
        self.assertIn("idle_decay_c_per_hour", str(ctx.exception))

    def test_zero_idle_decay_is_allowed(self):
        # A genuinely perfectly-insulated tank is a real (if idealized)
        # case -- zero decay, never negative (which would mean the tank
        # spontaneously heats itself while idle).
        cfg = ThermalLoadConfig(**_base_thermal(idle_decay_c_per_hour=0.0))
        self.assertEqual(cfg.idle_decay_c_per_hour, 0.0)


class TestPeriodOrderingValidation(unittest.TestCase):
    def test_deadline_before_earliest_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ThermalLoadConfig(**_base_thermal(earliest_period=10, deadline_period=5))
        self.assertIn("deadline_period", str(ctx.exception))

    def test_deadline_equal_to_earliest_is_allowed(self):
        cfg = ThermalLoadConfig(**_base_thermal(earliest_period=5, deadline_period=5))
        self.assertEqual(cfg.deadline_period, 5)

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
