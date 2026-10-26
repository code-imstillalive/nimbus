"""The LP's thermal decay is scaled by the gap to ambient, so the plan sees the
outdoor forecast (#481) (#1837).
"""

from __future__ import annotations

import unittest

import numpy as np
from solver import lp as _solver_lp
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 30.0,
        "initial_soc_kwh": 15.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 30.0,
        "max_charge_kw": 15.0,
        "max_discharge_kw": 15.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.10,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _thermal(**overrides) -> ThermalLoadConfig:
    defaults = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 40.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": 23,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.3,
    }
    defaults.update(overrides)
    return ThermalLoadConfig(**defaults)


def _solve(thermal: ThermalLoadConfig, n: int = 24):
    periods = _flat_grid(n)
    grid = GridConfig(
        import_price=np.full(n, 0.30),
        export_price=np.full(n, 0.05),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    return build_plan(
        periods=periods,
        grid=grid,
        batteries=[_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
        thermal_loads=[thermal],
    )


def _thermal_kwh(plan, hours: float = 1.0) -> float:
    return float(np.sum(plan.thermal_loads[0].power_kw) * hours)


class TestTheAmbientPairIsAllOrNothing(unittest.TestCase):
    """Same rule `must_have_soc_by_period_index`/`must_have_soc_kwh` already
    follow (#563 item 2). Silently ignoring the half that arrived is how a
    household ends up believing the LP is weather-aware when it is not."""

    def test_a_coefficient_without_ambient_is_rejected(self):
        # test_a_coefficient_without_ambient_is_rejected
        _solver_lp._lex_calibration_failed_until = 0.0
        with self.assertRaises(ValueError) as ctx:
            _thermal(loss_coeff_per_h=0.05)
        self.assertIn("both be set or both be None", str(ctx.exception))

        # test_a_negative_coefficient_is_rejected: A negative coefficient is a tank that heats itself from a cold day.
        _solver_lp._lex_calibration_failed_until = 0.0
        with self.assertRaises(ValueError) as ctx:
            _thermal(loss_coeff_per_h=-0.01, ambient_c=(20.0,))
        self.assertIn("loss_coeff_per_h must be >= 0", str(ctx.exception))


class TestAShortAmbientSeriesHoldsRatherThanReverting(unittest.TestCase):
    def test_a_series_shorter_than_the_horizon_holds_its_last_value(self):
        """Sample-and-hold, the same thing every other per-period series in this
        solver does when a source runs short. Reverting to the flat model
        mid-horizon would make the decay discontinuous for a reason that has
        nothing to do with the physics."""
        n = 24
        short = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=(0.0, 0.0, 0.0)), n)
        full = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=tuple([0.0] * n)), n)
        self.assertEqual(short.status, "optimal")
        np.testing.assert_allclose(
            short.thermal_loads[0].temperature_c,
            full.thermal_loads[0].temperature_c,
            atol=1e-6,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
