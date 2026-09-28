"""The oracle can re-time a day's Controllable Loads, and only the oracle
(nimbus issue #1357, the second half of #768).

`compute_quality_report()`'s oracle called `build_plan(loads=[load])` with no
`adequacy_loads=`/`sheddable_loads=`, though `build_plan()` has accepted both
since #791. So a day with a Controllable Load was scored against an oracle that
could not move it — the oracle was denied a freedom the household actually has,
which flatters the achieved dispatch.

## What these tests pin, and why each one

1. **`controllable_loads=None` changes nothing.** This is the default, and it is
   the property that makes the change inert until a caller opts in. Asserted as
   exact equality of every scalar the report publishes, against a report computed
   with the parameter absent entirely.

2. **The bundle cannot be constructed inconsistently.** Loads without
   `delivered_kwh_by_period` double-counts their energy; the reverse removes
   energy and never gives it back. Both raise rather than silently producing a
   number, because both produce a *plausible* number — that is what makes them
   dangerous.

3. **The subtraction reaches the oracle and nothing else.** The achieved-side
   quantities (`p2p_commitment_shortfall_kwh`, and the export-pin widening that
   feeds the oracle's own grid bounds) are computed from the REAL house load,
   because the real house really drew that energy at the time it drew it.
   Subtracting there would rewrite history rather than model an alternative to
   it. This is the test that would catch the natural wrong implementation —
   reducing `load` once, at the top, and letting it flow everywhere.

4. **A load the oracle was given makes it no worse.** An `AdequacyLoadConfig`
   adds both a freedom (re-time) and an obligation (deliver `target_kwh`), and
   #768's thread records that the net direction is not determinable from the code
   alone. What *is* determinable: handing the oracle the same energy it already
   had to serve, but re-timeable, cannot make the optimum worse — the original
   timing remains feasible. So `j_star` must not increase. That is a real
   invariant rather than a snapshot of a number.

5. **Clipping.** A reconstruction can exceed the measured house total in a
   period (different meters; #1231 records a 5–8% measurement-plane offset on the
   reference install), and a negative base load would hand the oracle free energy.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from solver.quality_report import OracleControllableLoads, compute_quality_report

N = 24
HOURS = np.full(N, 1.0)
START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
STARTS = [START + timedelta(hours=i) for i in range(N)]

HOUSE_KW = 2.0


def _battery() -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=100.0,
        initial_soc_kwh=50.0,
        min_soc_kwh=2.0,
        max_soc_kwh=100.0,
        max_charge_kw=20.0,
        max_discharge_kw=20.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=np.full(N, 0.01),
        salvage_value=0.0,
    )


def _grid(*, cheap_overnight: bool) -> GridConfig:
    """A price shape with a real gradient when asked for one, so re-timing has
    something to gain and the oracle's answer is decided by cost."""
    imp = np.full(N, 0.30)
    if cheap_overnight:
        imp[1:5] = 0.10
    return GridConfig(
        import_price=imp,
        export_price=np.full(N, 0.05),
        import_limit_kw=42.0,
        export_limit_kw=42.0,
    )


def _report(*, controllable=..., cheap_overnight: bool = True, house_kw=HOUSE_KW):
    """`controllable=...` (Ellipsis) omits the parameter entirely, which is how
    the inertness test distinguishes "passed None" from "not passed"."""
    zero = np.zeros(N)
    battery = _battery()
    solar = np.zeros(N)
    solar[9:15] = 6.0
    kwargs = dict(
        periods=PeriodGrid(hours=HOURS, start=START),
        grid_residual=_grid(cheap_overnight=cheap_overnight),
        grid_oracle=_grid(cheap_overnight=cheap_overnight),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=solar),
        load=LoadConfig(name="house", forecast_kw=np.full(N, house_kw)),
        timestamps=STARTS,
        real_p2p_dollars_earned=0.0,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[zero],
        actual_discharge_kw=[zero],
        final_soc_kwh_actual=[battery.initial_soc_kwh],
    )
    if controllable is not ...:
        kwargs["controllable_loads"] = controllable
    return compute_quality_report(**kwargs)


def _delivered(*, periods: tuple[int, ...], kwh_each: float) -> np.ndarray:
    a = np.zeros(N)
    for p in periods:
        a[p] = kwh_each
    return a


def _adequacy(*, target_kwh: float) -> AdequacyLoadConfig:
    return AdequacyLoadConfig(
        name="dishwasher",
        max_power_kw=2.0,
        target_kwh=target_kwh,
        earliest_period=0,
        deadline_period=N - 1,
        shortfall_price=4.0,
    )


SCALARS = (
    "j_ref",
    "j_ach",
    "j_star",
    "tracking_cost",
    "p2p_commitment_shortfall_kwh",
    "j_star_evaluator",
    "j_star_path_delta",
)


class TestTheDefaultChangesNothing(unittest.TestCase):
    def test_passing_none_is_identical_to_not_passing_it(self):
        omitted = _report()
        explicit_none = _report(controllable=None)
        for name in SCALARS:
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(omitted, name),
                    getattr(explicit_none, name),
                    f"{name} moved between an omitted controllable_loads and an "
                    f"explicit None. The default has to be a true no-op or every "
                    f"install without Controllable Loads silently gets a new "
                    f"score.",
                )

    def test_an_empty_bundle_is_also_a_no_op(self):
        """An install whose every load is unscorable for the window builds an
        empty bundle rather than None, and must still score identically."""
        empty = _report(controllable=OracleControllableLoads())
        omitted = _report()
        for name in SCALARS:
            with self.subTest(field=name):
                self.assertEqual(getattr(empty, name), getattr(omitted, name))

    def test_the_report_says_it_scored_nothing(self):
        r = _report()
        self.assertEqual(r.oracle_controllable_loads_scored, ())
        self.assertEqual(r.oracle_controllable_loads_skipped, ())


class TestTheBundleCannotBeInconsistent(unittest.TestCase):
    def test_loads_without_the_energy_to_subtract_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            OracleControllableLoads(adequacy=(_adequacy(target_kwh=4.0),))
        self.assertIn("double-count", str(cm.exception))

    def test_energy_to_subtract_without_loads_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            OracleControllableLoads(
                delivered_kwh_by_period=_delivered(periods=(18, 19), kwh_each=2.0)
            )
        self.assertIn("never give it back", str(cm.exception))

    def test_a_consistent_bundle_constructs(self):
        b = OracleControllableLoads(
            adequacy=(_adequacy(target_kwh=4.0),),
            delivered_kwh_by_period=_delivered(periods=(18, 19), kwh_each=2.0),
            skipped=(("tank", "kind=thermal"),),
        )
        self.assertEqual(len(b.adequacy), 1)
        self.assertEqual(b.skipped, (("tank", "kind=thermal"),))


class TestOnlyTheOracleSeesTheSubtraction(unittest.TestCase):
    """The test that catches the natural wrong implementation: reducing `load`
    once at the top and letting it flow into the achieved-side quantities too."""

    def _both(self):
        delivered = _delivered(periods=(18, 19), kwh_each=2.0)
        bundle = OracleControllableLoads(
            adequacy=(_adequacy(target_kwh=4.0),),
            delivered_kwh_by_period=delivered,
        )
        return _report(), _report(controllable=bundle)

    def test_the_achieved_cost_is_unchanged(self):
        """j_ach prices what the house really did. The dishwasher really ran at
        18:00-19:00 and that energy was really bought then."""
        plain, wired = self._both()
        self.assertAlmostEqual(
            plain.j_ach,
            wired.j_ach,
            places=6,
            msg="j_ach moved. The achieved trajectory is history -- wiring the "
            "oracle must not reprice what already happened.",
        )

    def test_the_idle_reference_is_unchanged(self):
        """j_ref is 'battery idle, house still running'. The house still ran the
        dishwasher, so its load is unchanged too."""
        plain, wired = self._both()
        self.assertAlmostEqual(plain.j_ref, wired.j_ref, places=6)

    def test_the_p2p_shortfall_is_unchanged(self):
        """Measured from the achieved export, which is derived from the real
        house load. A reduced load here would invent export that never left."""
        plain, wired = self._both()
        self.assertAlmostEqual(
            plain.p2p_commitment_shortfall_kwh,
            wired.p2p_commitment_shortfall_kwh,
            places=6,
        )


class TestTheOracleActuallyGetsTheLoad(unittest.TestCase):
    def test_the_report_names_what_it_scored(self):
        delivered = _delivered(periods=(18, 19), kwh_each=2.0)
        r = _report(
            controllable=OracleControllableLoads(
                adequacy=(_adequacy(target_kwh=4.0),),
                delivered_kwh_by_period=delivered,
                skipped=(("tank", "kind=thermal"),),
            )
        )
        self.assertEqual(r.oracle_controllable_loads_scored, ("dishwasher",))
        self.assertEqual(
            r.oracle_controllable_loads_skipped, (("tank", "kind=thermal"),)
        )

    def test_re_timeable_energy_cannot_make_the_oracle_worse(self):
        """The invariant, rather than a recorded number.

        The oracle is handed the same 4 kWh it already had to serve, but free to
        place it. The original placement stays feasible, so the optimum cannot
        get worse: j_star must not increase. With a cheap overnight window
        available it should actually fall, but "not worse" is the part that holds
        for every price shape and is therefore what is asserted.
        """
        delivered = _delivered(periods=(18, 19), kwh_each=2.0)
        plain, wired = (
            _report(),
            _report(
                controllable=OracleControllableLoads(
                    adequacy=(_adequacy(target_kwh=4.0),),
                    delivered_kwh_by_period=delivered,
                )
            ),
        )
        self.assertLessEqual(
            wired.j_star,
            plain.j_star + 1e-6,
            f"j_star rose from {plain.j_star} to {wired.j_star} when the oracle "
            f"was GIVEN freedom it did not have before. Either the energy is "
            f"being double-counted, or the obligation to deliver is being priced "
            f"against a target larger than what was actually delivered.",
        )

    def test_a_sheddable_load_is_accepted_too(self):
        profile = np.zeros(N)
        profile[12:16] = 1.5
        r = _report(
            controllable=OracleControllableLoads(
                sheddable=(
                    SheddableLoadConfig(
                        name="pool", forecast_kw=profile, shed_cost=0.45
                    ),
                ),
                delivered_kwh_by_period=profile * HOURS,
            )
        )
        self.assertEqual(r.oracle_controllable_loads_scored, ("pool",))


class TestTheSubtractionIsClippedAndTheShortfallIsPublished(unittest.TestCase):
    """Found by this test failing, which is why it is written this way.

    Clipping at zero is necessary -- a negative base load hands the oracle free
    energy -- but clipping ALONE is wrong and silently deflates EPR. The clip
    removes at most what the house drew, while the adequacy config re-introduces
    the reconstruction's full figure, so the oracle ends up serving MORE than the
    real house did and scores worse than the day it is grading.

    Measured on this fixture: 12 kWh claimed, only 4 kWh removable, and `j_star`
    rose from -0.851 to -0.439 -- the oracle made worse by a meter disagreement
    rather than by anything about dispatch. So the residue is published instead
    of absorbed, and the not-worse invariant is asserted only where the
    subtraction is actually honoured.
    """

    def test_an_over_large_reconstruction_publishes_what_it_could_not_remove(self):
        delivered = _delivered(periods=(18, 19), kwh_each=HOUSE_KW * 3)
        r = _report(
            controllable=OracleControllableLoads(
                adequacy=(_adequacy(target_kwh=float(delivered.sum())),),
                delivered_kwh_by_period=delivered,
            )
        )
        # 6 kWh claimed in each of two periods, 2 kWh of house load available in
        # each, so 4 kWh of the 12 is removable and 8 kWh is not.
        self.assertAlmostEqual(
            r.oracle_controllable_loads_unsubtracted_kwh,
            8.0,
            places=4,
            msg="the residue the clip refused to remove has to be reported, or a "
            "meter disagreement deflates EPR with nothing on the record saying so.",
        )

    def test_a_reconstruction_within_the_house_load_reports_no_residue(self):
        """The ordinary case: nothing clipped, nothing to report, and the
        not-worse invariant holds."""
        delivered = _delivered(periods=(18, 19), kwh_each=2.0)
        r = _report(
            controllable=OracleControllableLoads(
                adequacy=(_adequacy(target_kwh=4.0),),
                delivered_kwh_by_period=delivered,
            )
        )
        self.assertEqual(r.oracle_controllable_loads_unsubtracted_kwh, 0.0)
        self.assertLessEqual(r.j_star, _report().j_star + 1e-6)

    def test_the_default_reports_no_residue(self):
        self.assertEqual(_report().oracle_controllable_loads_unsubtracted_kwh, 0.0)


if __name__ == "__main__":
    unittest.main()
