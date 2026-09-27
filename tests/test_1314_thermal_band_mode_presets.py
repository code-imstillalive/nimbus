"""nimbus issue #1314: may a household mode move a SCHEDULE lever, or
only a quantity and a value?

The decision taken, and confirmed by @purcell-lab on that issue, is
**option 2 for the thermal band, option 3 for `deadline`/`allowed`/
`enabled`**. The band's BOUND moves; the LP constraint stays hard. The
clock levers stay out and belong to a household's own automation.

These tests exist because every interesting property of that decision is
one a plausible re-implementation gets wrong, and two of them would fail
as a real production incident rather than as a wrong number:

- **A band preset can abort the WHOLE solve.**
  `ThermalLoadConfig.__post_init__` raises `ValueError` when
  `comfort_floor_c > target_temperature_c` or when
  `target_temperature_c > max_temperature_c`, and
  `build_controllable_loads()` constructs it with no `try` around the
  call. So an ordering the table breaks does not degrade one load -- it
  takes the battery and every other load down with it. The repair step
  is tested by constructing the real dataclass from the real moded
  values, not by asserting on a float.
- **The hard guarantee must stay hard.** #774 records a five-incident
  history behind making the daily heat a genuine LP bound rather than a
  cost term. A mode that quietly turned that into a soft penalty would
  reopen all five. Pinned via `Plan.thermal_guarantee_relaxed`.
- **`home` must remain byte-identical.** #485's own second acceptance
  criterion, now with a second table in the path that could break it.
  Tested both structurally (the same dict object comes back) and
  end-to-end (the same LP arrays come back).
- **`economy` must not lower the deadline target.** A cost mode may
  decline to pay for a mid-day reheat; it must never silently deliver a
  colder tank than the household asked for. That asymmetry is a
  deliberate design choice and is otherwise invisible.
- **The ceiling constant must not drift** from `ThermalLoadConfig`'s own
  `max_temperature_c` default, which `household_modes.py` duplicates
  rather than imports.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import _solver_path  # noqa: F401
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
        moved = {key for levers in hm.THERMAL_BAND_PRESETS.values() for key in levers}
        self.assertIn(hm.THERMAL_TARGET_KEY, moved)
        self.assertIn(hm.THERMAL_COMFORT_FLOOR_KEY, moved)

    def test_no_table_ever_moves_a_clock_lever(self):
        """Option 3. `build_controllable_loads()` DROPS a load out of the
        plan for the cycle -- warning only -- when a resolved deadline
        lands before its earliest hour, so an hour preset can silently
        remove a real load from a real plan. Different in kind from
        scaling a price, which the LP simply absorbs."""
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

    def test_the_band_table_holds_bounds_only_never_a_cost(self):
        """The whole reason the band has its own table is that a reader
        can see which entries move a constraint boundary. A cost that
        drifted in here would defeat that."""
        for mode, levers in hm.THERMAL_BAND_PRESETS.items():
            for key in levers:
                with self.subTest(mode=mode, key=key):
                    self.assertIn(
                        key, (hm.THERMAL_TARGET_KEY, hm.THERMAL_COMFORT_FLOOR_KEY)
                    )

    def test_every_band_entry_is_a_degree_delta_not_a_factor(self):
        """A multiplier on a Celsius setpoint has no physical meaning --
        0.9 x 55 C is an arbitrary 5.5 C, not '10% less heat'."""
        for mode, levers in hm.THERMAL_BAND_PRESETS.items():
            for key, (op, _operand) in levers.items():
                with self.subTest(mode=mode, key=key):
                    self.assertEqual(op, hm.DELTA)

    def test_every_band_key_is_a_real_config_key(self):
        """A preset naming a key nothing reads is dead config that looks
        live: a household switches mode, nothing happens, no error says
        why."""
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


class TestHomeIsStillByteIdentical(unittest.TestCase):
    """#485's second acceptance criterion, re-proven now that a second
    table and a repair pass sit in the same path."""

    def test_home_returns_the_caller_s_very_own_dict(self):
        out, applied = hm.apply_to_load_config(dict(_BASE_THERMAL_DATA), "home")
        self.assertEqual(applied, {})
        again, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "home")
        self.assertIs(again, _BASE_THERMAL_DATA)
        self.assertEqual(out, _BASE_THERMAL_DATA)

    def test_an_unset_or_unknown_mode_never_reaches_the_repair_pass(self):
        for mode in (None, "holiday-house", ""):
            with self.subTest(mode=mode):
                out, applied = hm.apply_to_load_config(_BASE_THERMAL_DATA, mode)
                self.assertIs(out, _BASE_THERMAL_DATA)
                self.assertEqual(applied, {})

    def test_the_lp_arrays_under_home_match_the_unmoded_solve_exactly(self):
        """The end-to-end half. Structural identity is necessary; this is
        the statement a household actually cares about."""
        raw = _solve(_thermal_from(_BASE_THERMAL_DATA))
        moded, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "home")
        under_home = _solve(_thermal_from(moded))
        self.assertEqual(raw.status, "optimal")
        np.testing.assert_array_equal(
            raw.thermal_loads[0].power_kw, under_home.thermal_loads[0].power_kw
        )
        np.testing.assert_array_equal(
            raw.thermal_loads[0].temperature_c,
            under_home.thermal_loads[0].temperature_c,
        )


class TestAwayReallyDropsTheBand(unittest.TestCase):
    """#485's acceptance criterion 1, the half that had no coverage: a
    multiplier on a quantity cannot express 'drops the pool heater
    band'."""

    def test_the_whole_band_translates_down_by_one_setback(self):
        out, applied = hm.apply_to_load_config(_BASE_THERMAL_DATA, "away")
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], 55.0)
        self.assertAlmostEqual(out[hm.THERMAL_COMFORT_FLOOR_KEY], 45.0)
        self.assertIn(hm.THERMAL_TARGET_KEY, applied)
        self.assertIn(hm.THERMAL_COMFORT_FLOOR_KEY, applied)

    def test_the_band_width_is_preserved_so_the_floor_cannot_cross(self):
        """Translating the band rather than scaling each end is what makes
        the ordering invariant hold by construction; the repair pass is
        the backstop, not the mechanism."""
        base_width = (
            _BASE_THERMAL_DATA[hm.THERMAL_TARGET_KEY]
            - _BASE_THERMAL_DATA[hm.THERMAL_COMFORT_FLOOR_KEY]
        )
        for mode in ("away", "guests"):
            with self.subTest(mode=mode):
                out, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, mode)
                self.assertAlmostEqual(
                    out[hm.THERMAL_TARGET_KEY] - out[hm.THERMAL_COMFORT_FLOOR_KEY],
                    base_width,
                )

    def test_away_schedules_strictly_less_heat_in_a_real_plan(self):
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

    def test_guests_schedules_strictly_more_heat_in_a_real_plan(self):
        home, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "home")
        guests, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "guests")
        self.assertGreater(
            _thermal_kwh(_solve(_thermal_from(guests))),
            _thermal_kwh(_solve(_thermal_from(home))),
        )

    def test_a_load_with_no_comfort_floor_only_moves_its_target(self):
        """`comfort_floor_c` is optional and `None` by default. `float(None)`
        raises, so the generic path must skip it rather than crash a real
        solve -- and the repair pass must cope with a half-present band."""
        data = dict(_BASE_THERMAL_DATA)
        data[hm.THERMAL_COMFORT_FLOOR_KEY] = None
        out, applied = hm.apply_to_load_config(data, "away")
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], 55.0)
        self.assertIsNone(out[hm.THERMAL_COMFORT_FLOOR_KEY])
        self.assertNotIn(hm.THERMAL_COMFORT_FLOOR_KEY, applied)

    def test_a_band_only_load_still_gets_a_fresh_dict(self):
        """A distinct branch, and the one a reader is most likely to miss.

        `apply_to_load_config()` runs two `_apply()` passes, and `_apply()`
        returns the caller's SAME object when nothing matches. So a load
        whose config has no `LOAD_PRESETS` key at all arrives at the band
        pass still holding the caller's own dict -- and the band pass is
        then the first thing that must copy. Mutating there would leak a
        moded band into the next solve after the mode changed back.
        """
        band_only = {
            hm.THERMAL_TARGET_KEY: 60.0,
            hm.THERMAL_COMFORT_FLOOR_KEY: 59.0,
        }
        snapshot = dict(band_only)
        out, applied = hm.apply_to_load_config(band_only, "away")
        self.assertEqual(band_only, snapshot)
        self.assertIsNot(out, band_only)
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], 55.0)
        self.assertAlmostEqual(out[hm.THERMAL_COMFORT_FLOOR_KEY], 54.0)
        self.assertEqual(
            set(applied), {hm.THERMAL_TARGET_KEY, hm.THERMAL_COMFORT_FLOOR_KEY}
        )

    def test_a_non_thermal_load_is_untouched_by_the_band_table(self):
        deferrable = {"deferrable_target_kwh": 4.0}
        out, applied = hm.apply_to_load_config(deferrable, "away")
        self.assertNotIn(hm.THERMAL_TARGET_KEY, out)
        self.assertNotIn(hm.THERMAL_TARGET_KEY, applied)


class TestTheHardGuaranteeStaysHard(unittest.TestCase):
    """#774's five incidents are the reason the daily heat is an LP bound
    and not a cost term. A mode may move the bound; it may never turn the
    bound into a penalty."""

    def test_the_moded_target_is_met_exactly_in_every_mode(self):
        for mode in ("home", "away", "guests", "economy"):
            with self.subTest(mode=mode):
                moded, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, mode)
                thermal = _thermal_from(moded)
                plan = _solve(thermal)
                self.assertEqual(plan.status, "optimal")
                self.assertEqual(
                    plan.thermal_guarantee_relaxed,
                    [],
                    "the hard deadline constraint was relaxed -- a mode must "
                    "move the bound, never soften the constraint",
                )
                self.assertAlmostEqual(
                    plan.thermal_loads[0].temperature_c[-1],
                    thermal.target_temperature_c,
                    places=5,
                )


class TestEconomyNeverLowersTheDeadlineTarget(unittest.TestCase):
    """Deliberate asymmetry, and invisible without a test. A cost mode may
    decline to pay for a mid-day reheat; it must not quietly deliver a
    colder tank than the household asked for."""

    def test_economy_moves_the_floor_and_not_the_target(self):
        out, applied = hm.apply_to_load_config(_BASE_THERMAL_DATA, "economy")
        self.assertAlmostEqual(
            out[hm.THERMAL_TARGET_KEY],
            _BASE_THERMAL_DATA[hm.THERMAL_TARGET_KEY],
        )
        self.assertNotIn(hm.THERMAL_TARGET_KEY, applied)
        self.assertLess(
            out[hm.THERMAL_COMFORT_FLOOR_KEY],
            _BASE_THERMAL_DATA[hm.THERMAL_COMFORT_FLOOR_KEY],
        )

    def test_economy_also_discounts_the_reheat_price(self):
        out, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "economy")
        self.assertAlmostEqual(out["thermal_comfort_floor_cost"], 0.20)

    def test_guests_pays_more_for_a_reheat_and_away_pays_less(self):
        guests, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "guests")
        away, _ = hm.apply_to_load_config(_BASE_THERMAL_DATA, "away")
        self.assertGreater(
            guests["thermal_comfort_floor_cost"],
            _BASE_THERMAL_DATA["thermal_comfort_floor_cost"],
        )
        self.assertLess(
            away["thermal_comfort_floor_cost"],
            _BASE_THERMAL_DATA["thermal_comfort_floor_cost"],
        )


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

    def test_a_floor_pushed_past_the_ceiling_is_clamped_to_the_target(self):
        """Directly exercises the repair branch rather than reaching it by
        luck: a band table that moved only the floor upward would produce
        exactly this, and so would a future entry that did."""
        values = {
            hm.THERMAL_TARGET_KEY: 60.0,
            hm.THERMAL_COMFORT_FLOOR_KEY: 65.0,
        }
        out, repaired = hm._repair_thermal_band(values)
        self.assertAlmostEqual(out[hm.THERMAL_COMFORT_FLOOR_KEY], 60.0)
        self.assertEqual(repaired, {hm.THERMAL_COMFORT_FLOOR_KEY: 60.0})

    def test_a_target_pushed_past_the_physical_ceiling_is_clamped(self):
        values = {hm.THERMAL_TARGET_KEY: 120.0, hm.THERMAL_COMFORT_FLOOR_KEY: 118.0}
        out, repaired = hm._repair_thermal_band(values)
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], hm.THERMAL_MAX_TEMPERATURE_C)
        self.assertAlmostEqual(
            out[hm.THERMAL_COMFORT_FLOOR_KEY], hm.THERMAL_MAX_TEMPERATURE_C
        )
        self.assertEqual(
            set(repaired), {hm.THERMAL_TARGET_KEY, hm.THERMAL_COMFORT_FLOOR_KEY}
        )

    def test_the_repair_is_copy_on_write_and_a_no_op_returns_the_same_object(self):
        values = {hm.THERMAL_TARGET_KEY: 60.0, hm.THERMAL_COMFORT_FLOOR_KEY: 50.0}
        out, repaired = hm._repair_thermal_band(values)
        self.assertIs(out, values)
        self.assertEqual(repaired, {})

    def test_the_repair_never_mutates_the_caller_s_dict(self):
        values = {hm.THERMAL_TARGET_KEY: 60.0, hm.THERMAL_COMFORT_FLOOR_KEY: 65.0}
        out, _ = hm._repair_thermal_band(values)
        self.assertEqual(values[hm.THERMAL_COMFORT_FLOOR_KEY], 65.0)
        self.assertIsNot(out, values)

    def test_a_repaired_value_is_what_gets_reported_not_the_pre_clamp_one(self):
        """The `applied` map is the only observability the per-load half
        has (it is what the DEBUG log prints). Reporting the pre-clamp
        figure would describe a value the LP never saw."""
        data = dict(_BASE_THERMAL_DATA)
        data[hm.THERMAL_TARGET_KEY] = 98.5
        data[hm.THERMAL_COMFORT_FLOOR_KEY] = 98.0
        out, applied = hm.apply_to_load_config(data, "guests")
        self.assertAlmostEqual(out[hm.THERMAL_TARGET_KEY], hm.THERMAL_MAX_TEMPERATURE_C)
        self.assertAlmostEqual(
            applied[hm.THERMAL_TARGET_KEY], hm.THERMAL_MAX_TEMPERATURE_C
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

    def test_the_tuning_resolve_precedes_the_thermal_target_read(self):
        import inspect

        from custom_components.nimbus_load import solver_writer as sw

        src = inspect.getsource(sw.build_controllable_loads)
        resolve_at = src.index("data = _resolve_controllable_load_tuning(subentry.data")
        # The READ, not the name: this function opens with a local import
        # block that mentions every CONF_ constant it uses, so a bare
        # name match finds the import and proves nothing. Found the hard
        # way -- the first version of this assertion failed on exactly
        # that.
        for read in (
            "data.get(CONF_THERMAL_TARGET_TEMPERATURE_C)",
            "data.get(CONF_THERMAL_COMFORT_FLOOR_C)",
            "data.get(CONF_THERMAL_COMFORT_FLOOR_COST)",
        ):
            with self.subTest(read=read):
                self.assertLess(
                    resolve_at,
                    src.index(read),
                    "the thermal branch reads this field before the "
                    "household-mode seam has run -- the band preset would "
                    "never reach the LP",
                )


if __name__ == "__main__":
    unittest.main()
