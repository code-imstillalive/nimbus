"""A battery configuration that cannot work is refused when built (#356)
(#1837).
"""

from __future__ import annotations

import unittest

from solver.elements import BatteryConfig, DegenerateConfigError


def _base_battery(**overrides) -> dict:
    defaults = {
        "name": "battery",
        "capacity_kwh": 30.0,
        "initial_soc_kwh": 15.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 30.0,
        "max_charge_kw": 15.0,
        "max_discharge_kw": 15.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.10,
    }
    defaults.update(overrides)
    return defaults


class TestZeroCapacityRejected(unittest.TestCase):
    def test_capacity_kwh_zero_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            BatteryConfig(
                **_base_battery(
                    capacity_kwh=0.0,
                    min_soc_kwh=0.0,
                    max_soc_kwh=0.0,
                    initial_soc_kwh=0.0,
                )
            )
        self.assertIn("capacity_kwh", str(ctx.exception))

    def test_capacity_kwh_small_positive_is_accepted(self):
        cfg = BatteryConfig(
            **_base_battery(
                capacity_kwh=0.5, min_soc_kwh=0.0, max_soc_kwh=0.5, initial_soc_kwh=0.25
            )
        )
        self.assertEqual(cfg.capacity_kwh, 0.5)


class TestNegativeMaxChargeDischargeRejected(unittest.TestCase):
    def test_negative_max_charge_kw_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            BatteryConfig(**_base_battery(max_charge_kw=-5.0))
        self.assertIn("max_charge_kw", str(ctx.exception))
        self.assertIn("-5", str(ctx.exception))

    def test_zero_max_charge_kw_is_still_accepted(self):
        # A real, legitimate "this direction is physically disabled"
        # config -- must NOT be rejected (see module docstring; an
        # earlier, too-strict draft of this fix broke this exact case).
        cfg = BatteryConfig(**_base_battery(max_charge_kw=0.0))
        self.assertEqual(cfg.max_charge_kw, 0.0)

    def test_zero_max_discharge_kw_is_still_accepted(self):
        cfg = BatteryConfig(**_base_battery(max_discharge_kw=0.0))
        self.assertEqual(cfg.max_discharge_kw, 0.0)

    def test_zero_both_directions_is_still_accepted(self):
        cfg = BatteryConfig(**_base_battery(max_charge_kw=0.0, max_discharge_kw=0.0))
        self.assertEqual(cfg.max_charge_kw, 0.0)
        self.assertEqual(cfg.max_discharge_kw, 0.0)


class TestEfficiencyMessageWording(unittest.TestCase):
    def test_exactly_100_percent_charge_efficiency_still_rejected(self):
        with self.assertRaises(DegenerateConfigError) as ctx:
            BatteryConfig(**_base_battery(charge_efficiency=1.0))
        msg = str(ctx.exception)
        self.assertIn("(0, 1)", msg)
        self.assertNotIn("(0, 1]", msg)
