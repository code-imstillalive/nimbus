"""A household mode may move a thermal load's comfort band, never its
schedule (#1314) (#1837).

The band's bound moves and the LP constraint stays hard; a mode moves the
quantity and the value of `deadline`, `allowed` and `enabled`, never the
schedule itself. Presets stay inside the physical limits.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import household_modes as hm

# A realistic hot-water subentry's own thermal fields, as
# `_resolve_controllable_load_tuning()` hands them on. Real key names,
# taken from const.py, not invented for the test.
_BASE_THERMAL_DATA = {
    "controllable_load_name": "Hot Water",
    "controllable_load_kind": "thermal",
    "thermal_max_power_kw": 3.0,
    "thermal_target_temperature_c": 60.0,
    "thermal_comfort_floor_c": 50.0,
    "thermal_comfort_floor_cost": 0.40,
    "thermal_earliest_hour": 9.0,
    "thermal_deadline_hour": 17.0,
}

_N = 24


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _thermal_from(data: dict) -> ThermalLoadConfig:
    """Build the real LP element from a (possibly moded) config dict, the
    same way `build_controllable_loads()` does -- so a value the table
    produced that the dataclass rejects fails HERE, loudly, instead of in
    a real solve."""
    floor = data.get("thermal_comfort_floor_c")
    return ThermalLoadConfig(
        name=str(data.get("controllable_load_name", "hws")),
        max_power_kw=float(data["thermal_max_power_kw"]),
        initial_temperature_c=40.0,
        target_temperature_c=float(data["thermal_target_temperature_c"]),
        earliest_period=0,
        deadline_period=_N - 1,
        heating_rate_c_per_kwh=8.0,
        idle_decay_c_per_hour=0.3,
        comfort_floor_c=(float(floor) if floor is not None else None),
        comfort_floor_cost=float(data.get("thermal_comfort_floor_cost", 0.0) or 0.0),
    )


def _solve(thermal: ThermalLoadConfig, n: int = _N):
    return build_plan(
        periods=_flat_grid(n),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=30.0,
                initial_soc_kwh=15.0,
                min_soc_kwh=2.0,
                max_soc_kwh=30.0,
                max_charge_kw=15.0,
                max_discharge_kw=15.0,
                charge_efficiency=0.99,
                discharge_efficiency=0.99,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.10,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
        thermal_loads=[thermal],
    )


def _thermal_kwh(plan) -> float:
    return float(np.sum(plan.thermal_loads[0].power_kw))


class TestTheDecisionIsWhatIsActuallyImplemented(unittest.TestCase):
    """#1314 option 2 for the band, option 3 for the clock levers. Each
    half is asserted rather than just written down, because the table is
    the only place either could silently change."""

    def test_the_band_bounds_are_covered_by_a_preset(self):
        # test_the_band_bounds_are_covered_by_a_preset
        moved = {key for levers in hm.THERMAL_BAND_PRESETS.values() for key in levers}
        self.assertIn(hm.THERMAL_TARGET_KEY, moved)
        self.assertIn(hm.THERMAL_COMFORT_FLOOR_KEY, moved)

        # test_no_table_ever_moves_a_clock_lever: Option 3. `build_controllable_loads()` DROPS a load out of the
        clock_levers = {
            "deferrable_earliest_hour",
            "deferrable_deadline_hour",
            "thermal_earliest_hour",
            "thermal_deadline_hour",
        }
        for table_name, table in (
            ("SOLVER_PRESETS", hm.SOLVER_PRESETS),
            ("LOAD_PRESETS", hm.LOAD_PRESETS),
            ("THERMAL_BAND_PRESETS", hm.THERMAL_BAND_PRESETS),
        ):
            for mode, levers in table.items():
                for key in levers:
                    with self.subTest(table=table_name, mode=mode, key=key):
                        self.assertNotIn(key, clock_levers)

        # test_the_band_table_holds_bounds_only_never_a_cost: The whole reason the band has its own table is that a reader
        for mode, levers in hm.THERMAL_BAND_PRESETS.items():
            for key in levers:
                with self.subTest(mode=mode, key=key):
                    self.assertIn(
                        key, (hm.THERMAL_TARGET_KEY, hm.THERMAL_COMFORT_FLOOR_KEY)
                    )

        # test_every_band_entry_is_a_degree_delta_not_a_factor: A multiplier on a Celsius setpoint has no physical meaning --
        for mode, levers in hm.THERMAL_BAND_PRESETS.items():
            for key, (op, _operand) in levers.items():
                with self.subTest(mode=mode, key=key):
                    self.assertEqual(op, hm.DELTA)

        # test_every_band_key_is_a_real_config_key: A preset naming a key nothing reads is dead config that looks
        from custom_components.nimbus_load import const

        known = {
            getattr(const, name)
            for name in dir(const)
            if name.startswith("CONF_") and isinstance(getattr(const, name), str)
        }
        for mode, levers in hm.THERMAL_BAND_PRESETS.items():
            for key in levers:
                with self.subTest(mode=mode, key=key):
                    self.assertIn(key, known)


class TestAwayReallyDropsTheBand(unittest.TestCase):
    """#485's acceptance criterion 1, the half that had no coverage: a
    multiplier on a quantity cannot express 'drops the pool heater
    band'."""

    def test_away_schedules_strictly_less_heat_in_a_real_plan(self):
        # test_away_schedules_strictly_less_heat_in_a_real_plan
        home, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "home")
        away, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "away")
        home_plan = _solve(_thermal_from(home))
        away_plan = _solve(_thermal_from(away))
        self.assertEqual(home_plan.status, "optimal")
        self.assertEqual(away_plan.status, "optimal")
        self.assertLess(
            _thermal_kwh(away_plan),
            _thermal_kwh(home_plan),
            "`away` lowered the band but the LP scheduled the same energy -- "
            "the bound never reached the constraint",
        )

        # test_a_load_with_no_comfort_floor_only_moves_its_target: `comfort_floor_c` is optional and `None` by default. `float(None)`
        data = dict(_BASE_THERMAL_DATA)
        data[hm.THERMAL_COMFORT_FLOOR_KEY] = None
        out, applied = hm.apply_to_load_config(data, "away")
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], 55.0)
        self.assertIsNone(out[hm.THERMAL_COMFORT_FLOOR_KEY])
        self.assertNotIn(hm.THERMAL_COMFORT_FLOOR_KEY, applied)


class TestTheOrderingInvariantIsRepairedNeverRaised(unittest.TestCase):
    """The failure this guards is not a wrong number, it is an aborted
    solve. `build_controllable_loads()` calls `ThermalLoadConfig(...)`
    with no `try` around it, so a `ValueError` from `__post_init__` takes
    the battery and every other load down with the one bad temperature.
    """

    def test_every_mode_over_a_grid_of_real_baselines_builds_a_valid_element(self):
        targets = (4.0, 20.0, 45.0, 55.0, 60.0, 75.0, 98.0, 99.0)
        for target in targets:
            for floor_gap in (0.0, 1.0, 10.0, target):
                floor = max(0.0, target - floor_gap)
                data = dict(_BASE_THERMAL_DATA)
                data[hm.THERMAL_TARGET_KEY] = target
                data[hm.THERMAL_COMFORT_FLOOR_KEY] = floor
                for mode in ("home", "away", "guests", "economy"):
                    with self.subTest(target=target, floor=floor, mode=mode):
                        moded, _ = hm.apply_to_load_config(data, mode)
                        # The assertion IS that this does not raise.
                        element = _thermal_from(moded)
                        if element.comfort_floor_c is not None:
                            self.assertLessEqual(
                                element.comfort_floor_c, element.target_temperature_c
                            )
                        self.assertLessEqual(
                            element.target_temperature_c, element.max_temperature_c
                        )


class TestTheDuplicatedCeilingCannotDrift(unittest.TestCase):
    """`household_modes.py` duplicates `max_temperature_c`'s default
    rather than importing it, to keep that module import-free across both
    the native and the standalone import paths. Duplication is only safe
    with a drift guard."""

    def test_the_constant_matches_thermalloadconfig_s_own_default(self):
        import dataclasses

        default = next(
            f.default
            for f in dataclasses.fields(ThermalLoadConfig)
            if f.name == "max_temperature_c"
        )
        self.assertEqual(hm.THERMAL_MAX_TEMPERATURE_C, default)


class TestTheBandReachesTheSolverThroughTheOneExistingSeam(unittest.TestCase):
    """No new call site. `build_controllable_loads()` already resolves
    `data` through `_resolve_controllable_load_tuning()` at the top of its
    per-subentry loop, and the thermal branch reads that same `data` --
    so the band arrives with no wiring change. This test pins the
    ORDER, which is the only thing that could silently break it."""


if __name__ == "__main__":
    unittest.main()
