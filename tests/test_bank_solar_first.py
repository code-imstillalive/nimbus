"""Bank solar first (household, 18 Oct 2026).

At 08:28 the reference household's battery sat at 10.5% and idle while
4.1 kW of solar was exported at 2.6c: the plan sold now because LocalVolts
forecast 1.3c from 09:30, cheap enough to refill from the grid. Correct on
the forecast, but it leaves the battery waiting on that forecast coming true.
The household asked for a switch: solar the battery can take goes into it
before any is exported.

EMHASS has no equivalent: `set_nocharge_from_grid` stops grid charging but
still lets the plan export solar now and fill from solar later, and
`set_battery_first_priority` is the opposite direction (discharge before
importing). HAEO has none either.

The mechanism is a soft floor on period 0's charge: min(solar - load, max
charge, room to max SoC). Only period 0 is dispatched and the plan re-solves
every cycle, so one row banks solar in practice. These tests drive the real
`assemble_and_solve_plan` through the LP.
"""

from __future__ import annotations

import inspect
import pathlib
import unittest

import _solver_path  # noqa: F401
import solver_plan

from tests.test_1303_plan_assembly_extraction import _Env, _kwargs

TOL = 1e-6
PKG = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)

# No risk dials: the bands do not enter, and the trade is the plain one.
CFG = {
    "solver_risk_aversion": 0.0,
    "solver_import_price_risk_aversion": 0.0,
    "solver_export_price_risk_aversion": 0.0,
}


def _morning() -> dict:
    """The 18 Oct shape: solar surplus now, export 2.6c now; from the next
    period grid power at 1.3c and export worth nothing; a dear evening."""
    kw = _kwargs()
    n = kw["n_periods"]
    solar = [9.0, 6.0, 6.0, 6.0, 1.0, 0.0, 0.0, 0.0]
    load = [2.6] * n
    imp = [0.092, 0.013, 0.013, 0.013, 0.18, 0.18, 0.18, 0.18]
    exp = [0.026, 0.0, 0.0, 0.0, 0.09, 0.09, 0.09, 0.09]
    kw.update(
        solar_kw=solar,
        solar_lower_kw=solar,
        solar_upper_kw=solar,
        load_kw=load,
        load_lower_kw=load,
        load_upper_kw=load,
        import_price=imp,
        import_price_upper=imp,
        export_price=exp,
        export_price_lower=exp,
        export_bonus_price=[0.0] * n,
        # The household's 40 kW: refilling later is genuinely possible.
        max_charge_kw=40.0,
    )
    return kw


def _plan(bank: bool, **overrides):
    kw = _morning()
    kw.update(overrides)
    cfg = dict(CFG, solver_bank_solar_first=bank)
    return solver_plan.assemble_and_solve_plan(cfg, **kw).plan


class TestTheFloor(unittest.TestCase):
    def test_it_is_the_least_of_surplus_max_charge_and_room(self):
        f = solver_plan.bank_solar_floor_kw
        common = {"charge_efficiency": 0.95, "hours0": 0.5}
        # surplus binds
        self.assertAlmostEqual(
            f(solar_kw=9, load_kw=2.6, max_charge_kw=40, headroom_kwh=100, **common),
            6.4,
        )
        # max charge binds
        self.assertAlmostEqual(
            f(solar_kw=60, load_kw=2.6, max_charge_kw=40, headroom_kwh=100, **common),
            40,
        )
        # room binds: 1 kWh of room in half an hour at 95% is 2.105 kW
        self.assertAlmostEqual(
            f(solar_kw=9, load_kw=2.6, max_charge_kw=40, headroom_kwh=1, **common),
            1 / (0.95 * 0.5),
        )

    def test_it_is_zero_with_no_surplus_no_room_or_no_period(self):
        f = solver_plan.bank_solar_floor_kw
        self.assertEqual(
            f(
                solar_kw=2,
                load_kw=3,
                max_charge_kw=40,
                headroom_kwh=10,
                charge_efficiency=0.95,
                hours0=0.5,
            ),
            0.0,
        )
        self.assertEqual(
            f(
                solar_kw=9,
                load_kw=2,
                max_charge_kw=40,
                headroom_kwh=-1,
                charge_efficiency=0.95,
                hours0=0.5,
            ),
            0.0,
        )
        self.assertEqual(
            f(
                solar_kw=9,
                load_kw=2,
                max_charge_kw=40,
                headroom_kwh=10,
                charge_efficiency=0.95,
                hours0=0.0,
            ),
            0.0,
        )


class TestTheLP(unittest.TestCase):
    def test_off_the_plan_sells_solar_now_and_refills_later(self):
        """The case the household saw: the scenario genuinely defers."""
        plan = _plan(False)
        self.assertEqual(plan.status, "optimal")
        self.assertLess(float(plan.battery_charge_kw[0]), TOL)
        self.assertGreater(float(plan.grid_export_kw[0]), 1.0)

    def test_on_period_0_banks_the_surplus(self):
        plan = _plan(True)
        self.assertEqual(plan.status, "optimal")
        # surplus 6.4 kW; max charge 40; room (38 - 20) kWh is ample
        self.assertAlmostEqual(float(plan.battery_charge_kw[0]), 6.4, places=4)
        self.assertLess(float(plan.grid_export_kw[0]), TOL)
        self.assertLess(float(plan.grid_import_kw[0]), TOL)

    def test_on_only_period_0_is_floored(self):
        """Later periods stay free; the next solve floors its own period 0."""
        free, banked = _plan(False), _plan(True)
        self.assertEqual(len(free.battery_charge_kw), len(banked.battery_charge_kw))
        self.assertGreater(
            float(banked.battery_charge_kw[0]), float(free.battery_charge_kw[0])
        )

    def test_on_a_full_battery_changes_nothing(self):
        env = _Env()
        env.initial_soc_kwh = env.max_soc_kwh
        free = _plan(False, soc_envelope=env)
        banked = _plan(True, soc_envelope=env)
        self.assertEqual(
            [round(float(x), 6) for x in free.battery_charge_kw],
            [round(float(x), 6) for x in banked.battery_charge_kw],
        )
        self.assertAlmostEqual(float(free.total_cost), float(banked.total_cost), 6)

    def test_on_a_spike_override_still_sells(self):
        """The spike override pins period 0 to discharge; the floor yields."""
        plan = _plan(True, spike_override_kw=5.0)
        self.assertEqual(plan.status, "optimal")
        self.assertLess(float(plan.battery_charge_kw[0]), TOL)
        self.assertAlmostEqual(float(plan.battery_discharge_kw[0]), 5.0, places=4)

    def test_off_by_default(self):
        kw = _morning()
        default = solver_plan.assemble_and_solve_plan(dict(CFG), **kw).plan
        off = _plan(False)
        self.assertEqual(
            [round(float(x), 6) for x in default.battery_charge_kw],
            [round(float(x), 6) for x in off.battery_charge_kw],
        )


class TestTheSwitchIsWired(unittest.TestCase):
    """A switch missing from sensor.py's bridge tables is a silent no-op
    (#538, #283): the solve would never see it."""

    def test_switch_bridge_and_solve_all_name_it(self):
        for rel in ("switch.py", "sensor.py", "solver_tab.py", "help_tab.py"):
            src = (PKG / rel).read_text(encoding="utf-8")
            self.assertTrue(
                "CONF_SOLVER_BANK_SOLAR_FIRST" in src
                or "solver_bank_solar_first" in src,
                rel,
            )
        self.assertEqual(
            (PKG / "sensor.py")
            .read_text(encoding="utf-8")
            .count("CONF_SOLVER_BANK_SOLAR_FIRST,"),
            3,  # the import and both bridge tables
        )
        self.assertIn('"solver_bank_solar_first"', inspect.getsource(solver_plan))
