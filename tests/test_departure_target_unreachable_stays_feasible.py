"""A battery participant's departure target it cannot physically reach must not
make the whole plan infeasible.

Seen on a test install on 17 Oct 2026, 04:40 to past 08:00 AEST: a test EV at
5% SoC (floor 20%) with an 80%-by-08:00 departure target. 45 kWh at 12 kW is
about 3.9 hours, so from about 04:10 the hard row `soc[deadline] >= target` was
unsatisfiable. HiGHS rejected the model in presolve (`simplex_iterations=0`):
the calibrated solve's `phase1_primary` logged the #773 ERROR, and the plain
single-objective fallback came back infeasible too, so the published plan was
`status=infeasible` every cycle -- no dispatch for any battery or load. From
08:00 the deadline resolved to period 0 with the car away, which is the same
failure.

The fix keeps the hard row whenever the target is reachable (byte-identical
model) and makes it a dominant-priced soft row only when the target exceeds the
most the battery can hold by the deadline charging flat out. These tests pin
both halves.
"""

from __future__ import annotations

import logging
import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver import lp, network
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

_N = 12
_HOURS = 0.5
_EFF = 0.95**0.5  # extra_batteries.py's sqrt of a 95% round trip
_EV_CAP = 60.0
_EV_KW = 12.0


def _periods() -> PeriodGrid:
    return PeriodGrid(
        hours=np.full(_N, _HOURS), start=datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    )


def _grid() -> GridConfig:
    return GridConfig(
        import_price=np.full(_N, 0.30),
        export_price=np.full(_N, 0.05),
        import_limit_kw=200.0,
        export_limit_kw=200.0,
    )


def _home() -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=20.0,
        min_soc_kwh=4.0,
        max_soc_kwh=40.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.05,
    )


def _ev(
    *,
    deadline: int,
    target_kwh: float,
    initial_kwh: float = 3.0,  # 5% of 60 kWh, below the 20% floor
    **extra,
) -> BatteryConfig:
    return BatteryConfig(
        name="Test EV",
        capacity_kwh=_EV_CAP,
        initial_soc_kwh=initial_kwh,
        min_soc_kwh=12.0,
        max_soc_kwh=_EV_CAP,
        max_charge_kw=_EV_KW,
        max_discharge_kw=_EV_KW,
        charge_efficiency=_EFF,
        discharge_efficiency=_EFF,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.0,
        must_have_soc_by_period_index=deadline,
        must_have_soc_kwh=target_kwh,
        **extra,
    )


def _reach(initial_kwh: float, periods: int, kw: float = _EV_KW) -> float:
    return min(initial_kwh + periods * kw * _EFF * _HOURS, _EV_CAP)


def _solve(ev: BatteryConfig, *, options=None, adequacy=None):
    # lp.py's #773 cooldown is module state; reset it so an earlier failure
    # can never make this solve skip the phased path it is testing.
    lp._lex_calibration_failed_until = 0.0
    return build_plan(
        periods=_periods(),
        grid=_grid(),
        batteries=[_home(), ev],
        solar=SolarConfig(forecast_kw=np.zeros(_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(_N, 1.0))],
        adequacy_loads=adequacy,
        solve_options=options,
    )


def _ev_plan(plan):
    return next(b for b in plan.batteries if b.name == "Test EV")


class TestUnreachableTargetStaysFeasible(unittest.TestCase):
    def setUp(self):
        network._DEPARTURE_UNREACHABLE_WARNED.clear()

    def test_calibrated_solve_no_longer_fails_phase1_and_plans_recovery(self):
        """The production path: CalibratedOptions, target 48 kWh at the end
        of period 1 from 3 kWh. Before the fix phase1_primary logged the #773
        ERROR and the plain fallback returned `infeasible`."""
        ev = _ev(deadline=1, target_kwh=48.0)
        with self.assertNoLogs("solver.lp", level=logging.ERROR):
            plan = _solve(ev, options=lp.CalibratedOptions())
        self.assertEqual(plan.status, "optimal")
        evp = _ev_plan(plan)
        # Charges flat out in every period before the deadline.
        np.testing.assert_allclose(evp.charge_kw[:2], [_EV_KW, _EV_KW], atol=1e-6)
        reach = _reach(3.0, 2)
        self.assertAlmostEqual(float(evp.soc_kwh[1]), reach, places=5)
        self.assertAlmostEqual(
            plan.departure_target_shortfall_kwh["Test EV"], 48.0 - reach, places=5
        )

    def test_plain_solve_is_feasible_too(self):
        plan = _solve(_ev(deadline=1, target_kwh=48.0), options=None)
        self.assertEqual(plan.status, "optimal")
        self.assertIn("Test EV", plan.departure_target_shortfall_kwh)

    def test_with_binaries_in_the_model(self):
        """Production's model carried ~1,360 binaries (controllable loads).
        Semi-continuous adequacy loads put binaries in this one."""
        adequacy = [
            AdequacyLoadConfig(
                name=f"load{i}",
                max_power_kw=2.0,
                target_kwh=3.0,
                deadline_period=_N - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
            for i in range(3)
        ]
        with self.assertNoLogs("solver.lp", level=logging.ERROR):
            plan = _solve(
                _ev(deadline=1, target_kwh=48.0),
                options=lp.CalibratedOptions(),
                adequacy=adequacy,
            )
        self.assertEqual(plan.status, "optimal")

    def test_deadline_in_period_zero_while_away(self):
        """From 08:00 the 08:00 departure resolved to period 0, with the car
        reported away (charging gated for the next hour). Nothing can charge,
        so the whole target is short -- and the plan still solves."""
        ev = _ev(
            deadline=0,
            target_kwh=48.0,
            available=False,
            unavailable_until_period_index=2,
        )
        plan = _solve(ev, options=lp.CalibratedOptions())
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(
            plan.departure_target_shortfall_kwh["Test EV"], 45.0, places=5
        )

    def test_shortfall_is_counted_as_soc_penalty_not_terminal_credit(self):
        """cost_breakdown() takes terminal_value_credit as the residual of
        total_cost; an uncounted penalty would land there (#781)."""
        plan = _solve(_ev(deadline=1, target_kwh=48.0))
        shortfall = plan.departure_target_shortfall_kwh["Test EV"]
        self.assertGreater(shortfall, 0.0)
        self.assertGreater(plan.soc_penalty_cost, 0.0)
        # The penalty rate is the default soft-SoC rate: 10x the max price.
        rate = network.DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER * 0.30
        underfill = sum(
            max(0.0, 12.0 - float(s)) for s in _ev_plan(plan).soc_kwh
        ) + sum(max(0.0, 4.0 - float(s)) for s in plan.batteries[0].soc_kwh)
        self.assertAlmostEqual(
            plan.soc_penalty_cost, rate * (underfill + shortfall), places=4
        )

    def test_shared_charger_ceiling_counts_toward_reach(self):
        """A 12 kW car on a 7 kW shared charger reaches less than its own
        max_charge_kw suggests. A target between the two reaches must be soft,
        or the plan is infeasible exactly as before."""
        target = 0.5 * (_reach(3.0, 2, 7.0) + _reach(3.0, 2, _EV_KW))
        ev = _ev(
            deadline=1,
            target_kwh=target,
            shared_charger_group="garage",
            shared_charger_max_kw=7.0,
        )
        plan = _solve(ev, options=lp.CalibratedOptions())
        self.assertEqual(plan.status, "optimal")
        self.assertIn("Test EV", plan.departure_target_shortfall_kwh)

    def test_warns_once_per_departure(self):
        ev = _ev(deadline=1, target_kwh=48.0)
        with self.assertLogs("solver.network", level=logging.DEBUG) as cm:
            _solve(ev)
            _solve(ev)
        unreachable = [r for r in cm.records if "cannot reach" in r.getMessage()]
        self.assertEqual(
            [r.levelno for r in unreachable], [logging.WARNING, logging.DEBUG]
        )


class TestReachableTargetIsUnchanged(unittest.TestCase):
    def setUp(self):
        network._DEPARTURE_UNREACHABLE_WARNED.clear()

    def _rows(self, plan) -> set[str]:
        return set(plan.duals or {})

    def test_reachable_target_keeps_the_hard_row_below_floor_start(self):
        """Below the floor at the start but with time to recover: the hard
        row, no shortfall variable, target met."""
        plan = _solve(_ev(deadline=10, target_kwh=48.0), options=lp.CalibratedOptions())
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.departure_target_shortfall_kwh, {})
        self.assertGreaterEqual(float(_ev_plan(plan).soc_kwh[10]), 48.0 - 1e-6)
        rows = self._rows(plan)
        self.assertIn("battery_departure_deadline_Test EV", rows)
        self.assertNotIn("battery_departure_deadline_soft_Test EV", rows)

    def test_target_exactly_at_reach_stays_hard_and_feasible(self):
        target = _reach(3.0, 2)
        plan = _solve(_ev(deadline=1, target_kwh=target))
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.departure_target_shortfall_kwh, {})
        self.assertIn("battery_departure_deadline_Test EV", self._rows(plan))

    def test_in_range_soc_keeps_the_hard_row(self):
        """In-range SoC, reachable target: the original hard row, no soft
        row, no shortfall."""
        ev = _ev(deadline=6, target_kwh=40.0, initial_kwh=30.0)
        plan = _solve(ev, options=lp.CalibratedOptions())
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.departure_target_shortfall_kwh, {})
        self.assertGreaterEqual(float(_ev_plan(plan).soc_kwh[6]), 40.0 - 1e-6)
        self.assertNotIn("battery_departure_deadline_soft_Test EV", self._rows(plan))

    def test_no_deadline_is_untouched(self):
        ev = BatteryConfig(
            **{
                k: getattr(_ev(deadline=0, target_kwh=0.0), k)
                for k in (
                    "name",
                    "capacity_kwh",
                    "initial_soc_kwh",
                    "min_soc_kwh",
                    "max_soc_kwh",
                    "max_charge_kw",
                    "max_discharge_kw",
                    "charge_efficiency",
                    "discharge_efficiency",
                    "charge_cost",
                    "discharge_cost",
                    "salvage_value",
                )
            }
        )
        plan = _solve(ev)
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.departure_target_shortfall_kwh, {})


if __name__ == "__main__":
    unittest.main()
