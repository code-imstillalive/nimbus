"""Regression tests for nimbus issue #356 (Mark Purcell, codebase review),
item 3: `BatteryConfig.__post_init__` had two real validation gaps.

1. `capacity_kwh<=0.0` (a genuinely degenerate "battery" with zero usable
   capacity) sailed straight through validation and only surfaced much
   later, deep inside network.py's own LP construction, as an opaque
   HiGHS-level error.
2. `max_charge_kw`/`max_discharge_kw` were never checked for a negative
   value at all -- `max_charge_kw=-5` was accepted here and only surfaced
   later as a raw `ValueError: Variable 'battery_charge_0' has lb=0.0 >
   ub=-5` from deep inside lp.py, giving no hint the real problem was this
   config field.

The fix deliberately rejects NEGATIVE max_charge_kw/max_discharge_kw only
(`< 0.0`), not exactly zero -- several existing tests
(test_solver_backtest.py, test_solver_rolling.py) rely on
max_charge_kw=max_discharge_kw=0.0 ("this direction is physically
disabled") to construct a genuinely infeasible-but-validly-shaped
scenario on purpose, and an over-strict `<= 0.0` first draft of this fix
broke both of them (caught by running the real suite, not assumed).

Also covers the accompanying efficiency-message wording fix: the
DegenerateConfigError text used to say efficiencies must be in "(0, 1]"
-- implying exactly 1.0 is allowed -- while the guard itself has always
enforced a strict `< 1.0` on both sides, rejecting exactly 1.0. The
message contradicted the rule it was defending.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from solver.elements import BatteryConfig, DegenerateConfigError


def _base_battery(**overrides) -> dict:
    defaults = {
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

    def test_capacity_kwh_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            BatteryConfig(
                **_base_battery(
                    capacity_kwh=-5.0,
                    min_soc_kwh=0.0,
                    max_soc_kwh=0.0,
                    initial_soc_kwh=0.0,
                )
            )

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

    def test_negative_max_discharge_kw_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            BatteryConfig(**_base_battery(max_discharge_kw=-5.0))
        self.assertIn("max_discharge_kw", str(ctx.exception))

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

    def test_exactly_100_percent_discharge_efficiency_still_rejected(self):
        with self.assertRaises(DegenerateConfigError) as ctx:
            BatteryConfig(**_base_battery(discharge_efficiency=1.0))
        msg = str(ctx.exception)
        self.assertIn("(0, 1)", msg)
        self.assertNotIn("(0, 1]", msg)


if __name__ == "__main__":
    unittest.main()
