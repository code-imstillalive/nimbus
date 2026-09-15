"""nimbus issue #769's "separate, smaller observation", settled.

That issue reports, on a real install:

    the chosen start (07:20, 4.33 c/kWh) isn't even the cheapest nearby
    point -- 07:15 (3.32 c/kWh) is cheaper. The deferral isn't finding a
    clean global minimum either; it's landing on *a* later point, not
    *the* later point.

It reads like a second defect sitting underneath the earliness-incentive
design question that issue is really about. It is not one, and this file
demonstrates that rather than arguing it -- which matters, because the
main #769 question changes real hot-water dispatch and should not be
decided on top of a phantom.

**The comparison is invalid, and the reason is structural.** An
`AdequacyLoadConfig` carries `target_kwh`, `max_power_kw`,
`earliest_period` and `deadline_period` -- and no contiguity or
minimum-run field. The LP is not choosing a *start time*; it is choosing
a *set* of periods that delivers the energy. For #769's own load that
set is about 6.15 hours wide (4.0 kWh at 0.65 kW). A single period's
lambda says what one more kW costs *there*, given everything else the
plan is already doing; it says nothing about the cost of the whole set.

So a lone cheap period next to expensive neighbours is not an
opportunity for a load that needs six hours, and a fully optimal LP will
correctly decline it.

The fixture below reproduces exactly the reported shape -- an unused
in-window period whose lambda is strictly lower than a period the plan
does use -- in a plain `optimal` solve, and then shows that forcing the
load into that cheaper-looking region makes the plan measurably worse.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver import elements, network

_N = 24
_HOURS = np.full(_N, 0.5)  # 12 h at 30-minute resolution

# One genuinely cheap period (index 4) stranded among expensive ones,
# and a long cheap-ish block later (indices 8-15). The stranded period
# is CHEAPER than anything in the block -- which is precisely the shape
# #769 reported, and precisely the shape that misleads.
_IMPORT_PRICE = np.array([0.40] * 4 + [0.10] + [0.35] * 3 + [0.12] * 8 + [0.45] * 8)

_TARGET_KWH = 2.0
_MAX_POWER_KW = 0.65  # needs ~3.1 h == ~6.2 periods, far more than one


def _solve(earliest_period: int, deadline_period: int):
    grid = elements.GridConfig(
        import_price=_IMPORT_PRICE,
        export_price=_IMPORT_PRICE * 0.2,
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    battery = elements.BatteryConfig(
        name="home",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=18.0,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.02,
        discharge_cost=0.02,
        salvage_value=0.05,
    )
    adequacy = elements.AdequacyLoadConfig(
        name="hws",
        max_power_kw=_MAX_POWER_KW,
        target_kwh=_TARGET_KWH,
        deadline_period=deadline_period,
        shortfall_price=5.0,
        earliest_period=earliest_period,
    )
    return network.build_plan(
        periods=elements.PeriodGrid(hours=_HOURS),
        grid=grid,
        batteries=[battery],
        solar=elements.SolarConfig(forecast_kw=np.zeros(_N)),
        loads=[elements.LoadConfig(name="base", forecast_kw=np.full(_N, 1.0))],
        adequacy_loads=[adequacy],
    )


def _run_periods(plan) -> list[int]:
    return [t for t, kw in enumerate(plan.adequacy_loads[0].power_kw) if kw > 0.01]


def _lambda_per_kwh(plan) -> list[float]:
    """The same quantity the published per-period `shadow_price` carries
    -- the power-balance dual, divided by the period's own hours (the
    #662 correction)."""
    return [
        plan.duals.get(f"power_balance_t{t}", 0.0) / float(_HOURS[t]) for t in range(_N)
    ]


class TestTheReportedShapeOccursInAnOptimalPlan(unittest.TestCase):
    """First half: the observation is real and reproducible. If this
    stopped holding, the test below would be proving nothing."""

    def setUp(self):
        self.plan = _solve(0, _N - 1)
        self.run = _run_periods(self.plan)
        self.lam = _lambda_per_kwh(self.plan)

    def test_the_solve_is_genuinely_optimal(self):
        self.assertEqual(self.plan.status, "optimal")
        self.assertTrue(
            self.run, "the load must actually run for this to mean anything"
        )

    def test_an_unused_period_is_cheaper_than_a_used_one(self):
        """#769's own observation, reproduced from first principles in a
        plan nothing is wrong with."""
        worst_used = max(self.lam[t] for t in self.run)
        cheaper_unused = [
            t
            for t in range(_N)
            if t not in self.run and self.lam[t] < worst_used - 1e-9
        ]
        self.assertTrue(
            cheaper_unused,
            "this fixture no longer reproduces the reported shape, so the "
            "demonstration below is vacuous -- fix the fixture, not this "
            "assertion",
        )


class TestMovingTheLoadThereIsWorse(unittest.TestCase):
    """Second half, and the actual point: the cheaper-looking period is
    not an opportunity. A load needing ~6 periods cannot use one cheap
    period stranded among expensive ones, and the LP is right to decline
    it."""

    def test_forcing_the_load_into_the_cheaper_region_costs_more(self):
        free = _solve(0, _N - 1)
        # Deadline pulled in so the load must finish inside the stretch
        # containing the cheap stranded period, instead of using the
        # long block the free solve chose.
        forced = _solve(0, 7)

        self.assertEqual(forced.status, "optimal")
        self.assertGreater(
            forced.total_cost,
            free.total_cost,
            "forcing the load through the period with the LOWEST lambda in "
            "the whole horizon should cost more, not less -- if it ever "
            "costs less, the free placement was genuinely suboptimal and "
            "#769's smaller observation is a real defect after all",
        )

    def test_the_two_solves_really_chose_different_periods(self):
        """Guards against both variants coincidentally landing on the
        same schedule, which would make the cost comparison meaningless."""
        self.assertNotEqual(_run_periods(_solve(0, _N - 1)), _run_periods(_solve(0, 7)))


if __name__ == "__main__":
    unittest.main()
