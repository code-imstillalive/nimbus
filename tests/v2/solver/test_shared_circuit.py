"""A circuit's headroom shared by two or more adequacy loads, which must never
draw together beyond it (#1837).
"""

from __future__ import annotations

import unittest

import numpy as np
from solver import lp as _solver_lp
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SharedCircuitConfig,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 20.0,
        "initial_soc_kwh": 10.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 20.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestSharedCircuitConfigValidation(unittest.TestCase):
    def test_single_member_rejected(self):
        # test_single_member_rejected
        _solver_lp._lex_calibration_failed_until = 0.0
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a",), max_combined_power_kw=3.7
            )

        # test_duplicate_member_rejected
        _solver_lp._lex_calibration_failed_until = 0.0
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a", "a"), max_combined_power_kw=3.7
            )

        # test_non_positive_cap_rejected
        _solver_lp._lex_calibration_failed_until = 0.0
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a", "b"), max_combined_power_kw=0.0
            )


class TestSharedCircuitCapReproducesAndFixesRealCollision(unittest.TestCase):
    """Two adequacy loads, overlapping windows, a genuine price signal
    (period 0 far cheaper than period 1) giving the LP a real economic
    reason to put BOTH loads in period 0 if nothing stops it -- exactly
    the real, physical scenario a widened HWS L1/L3 window would create
    without a shared-circuit cap."""

    def _run(self, shared_circuits=None):
        n = 2  # a real 2h window, both loads free to run in either hour
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.array([0.01, 1.00]),  # period 0 far cheaper
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=0.5)
        adequacy = [
            AdequacyLoadConfig(
                name="hws_l1",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=1,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            ),
            AdequacyLoadConfig(
                name="hws_l3",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=1,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            ),
        ]
        return build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            shared_circuits=shared_circuits,
        )
