"""nimbus issue #769 (Mark Purcell) -- "don't defer unless it saves more
than $X", per load.

The reported morning, live on the reference household: hot water still
not started an hour past its window opening, `plan_status_reason` reading
*"deferred to 07:20, saves 3.29 c/kWh"*, against a real risk of not
reaching 60 degC in time. Mark's own re-solve then showed the actual
dollar difference between starting now and deferring was **$0.0000** --
the marginal shadow price was being extrapolated across a discrete
six-hour decision it could not price.

#684 found why the existing counterweight cannot help:
`adequacy_earliness_budget_kw` (#613) is capped at 0.01 $/kWh *and*
normalised across the whole 72-96 h solve horizon, so its pull at a
tens-of-minutes timescale is vanishingly small. Turning that constant up
is not the fix -- it would raise the ceiling for every adequacy load
globally, and it is denominated in a quantity no household reasons in.

**Household decision, 2026-09-15**, in their own words: *"each load can
have its own urgency then."* A per-load dollar threshold. Hot water set
to $1/day; a pool pump left chasing two cents.

**Prior-art line (#603), checked against both sources rather than
recalled:** EMHASS has `def_start_penalty`, which is anti-cycling and
carries no early/late preference -- if anything it biases *against*
starting. HAEO has no deferrable load element at all; its own user guide
says to schedule such loads externally. So this **originates** the
mechanism rather than porting one, and the semantics are the
household's.

The semantics, which these tests pin exactly:

    rate = (X / target_kwh) / window_hours
    cost(t) = rate * (elapsed[t] - elapsed[window_start]) * hours[t]

- the whole target delivered at the window opening -> penalty **0**
- the whole target delivered at the deadline -> penalty **exactly X**
- anything between -> proportional

**Where the shorthand and the mechanism part company**, found by
computing a test's expected values rather than eyeballing them. "Don't
defer unless it saves more than $X" is the intent; what the LP weighs is
the *lateness-proportional share* of X, since the full X is only charged
when the entire target sits at the deadline. A load shifted partway down
its window pays proportionally less, so the real flip point sits above
the naive `saving > X` reading -- in the sweep below, $0.6875 against a
$0.50 saving rather than $0.50.

That is correct behaviour (a partial deferral should cost less than a
full one) and it is *not* what the one-line description says, so it is
written down here and in the field's own comment rather than left for
someone tuning the number with a stopwatch to discover.

**This one soft cost is allowed to beat real prices**, which every other
member of that family is forbidden from doing. On a 4 kWh target a $1
threshold is 25 c/kWh of spread -- twenty-five times the smallest
difference this codebase treats as economically meaningful. That is what
was asked for, knowingly, and a test below pins it so nobody "fixes" it
back into a tie-break.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

N = 12
HOURS = np.ones(N)


def _grid(import_price: np.ndarray) -> GridConfig:
    return GridConfig(
        import_price=import_price,
        export_price=np.zeros(N),
        import_limit_kw=100.0,
        export_limit_kw=100.0,
    )


def _load(**overrides) -> AdequacyLoadConfig:
    kwargs = {
        "name": "hws",
        "max_power_kw": 1.0,
        "target_kwh": 2.0,
        "earliest_period": 0,
        "deadline_period": N - 1,
        "shortfall_price": 10.0,
    }
    kwargs.update(overrides)
    return AdequacyLoadConfig(**kwargs)


def _inert_battery() -> BatteryConfig:
    """`build_plan()` requires at least one battery, so give it one that
    cannot influence anything: zero charge and discharge power, so every
    flexibility decision in these tests belongs to the adequacy load.

    Same device as #999's own scenario builder uses for the same reason.
    """
    return BatteryConfig(
        name="inert",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=1.0,
        max_soc_kwh=10.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _solve(load: AdequacyLoadConfig, import_price: np.ndarray):
    return build_plan(
        periods=PeriodGrid(hours=HOURS),
        grid=_grid(import_price),
        batteries=[_inert_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(N))],
        adequacy_loads=[load],
    )


def _run_periods(plan) -> list[int]:
    served = plan.adequacy_loads[0].power_kw
    return [t for t, v in enumerate(served) if v > 1e-9]


class TestTheFieldIsANoOpUntilSet(unittest.TestCase):
    """The load-bearing claim for every install that never touches it."""

    def test_zero_is_byte_identical_to_absent(self):
        cheap_late = np.full(N, 0.30)
        cheap_late[8:10] = 0.05

        absent = _solve(_load(), cheap_late)
        explicit_zero = _solve(_load(min_deferral_saving_dollars=0.0), cheap_late)

        self.assertEqual(absent.total_cost, explicit_zero.total_cost)
        self.assertEqual(_run_periods(absent), _run_periods(explicit_zero))

    def test_without_it_the_load_chases_the_cheap_hours(self):
        """The behaviour #769 is about, reproduced: a real saving late in
        the window pulls the load late, which is correct today and is
        what the threshold is meant to be weighed against."""
        cheap_late = np.full(N, 0.30)
        cheap_late[8:10] = 0.05
        self.assertEqual(_run_periods(_solve(_load(), cheap_late)), [8, 9])


class TestTheThresholdDecidesByRealDollars(unittest.TestCase):
    def test_a_threshold_above_the_saving_keeps_the_load_early(self):
        """2 kWh moved from 30 c/kWh to 5 c/kWh saves $0.50. A $1.00
        threshold says that is not worth waiting for."""
        cheap_late = np.full(N, 0.30)
        cheap_late[8:10] = 0.05
        plan = _solve(_load(min_deferral_saving_dollars=1.0), cheap_late)
        self.assertEqual(
            _run_periods(plan),
            [0, 1],
            "a $0.50 saving should not buy an eight-period wait against a "
            "$1.00 threshold (nimbus #769)",
        )

    def test_a_threshold_below_the_saving_still_lets_it_wait(self):
        """The other half, and the one that matters for trust: this is a
        threshold, not a ban. A genuinely large saving still wins."""
        cheap_late = np.full(N, 0.30)
        cheap_late[8:10] = 0.05
        plan = _solve(_load(min_deferral_saving_dollars=0.10), cheap_late)
        self.assertEqual(_run_periods(plan), [8, 9])

    def test_the_flip_happens_where_the_arithmetic_says_it_should(self):
        """Sweeping the threshold across the real saving flips the
        decision exactly once, and the flip point is computed rather
        than eyeballed.

        **This is where the shorthand and the mechanism part company,
        and it is worth being exact about because the household was told
        the shorthand.** "Don't defer unless it saves more than $X" is
        the intent; what the LP actually weighs is the *lateness-
        proportional share* of X, because the full X is only charged
        when the ENTIRE target sits at the deadline.

        Here: 12 unit-hour periods, window 0..11 so `window_hours = 11`,
        `target_kwh = 2.0`, so `rate = X / 22`. Running at periods 8-9
        rather than 0-1 moves the lateness sum from 1 to 17, so the
        penalty difference is `16X/22 = 0.727X` -- not X. Against a real
        $0.50 saving the load therefore waits until

            0.727 * X >= 0.50   ->   X >= 0.6875

        not at X >= 0.50. A partial deferral costing proportionally less
        than a full one is the right behaviour; describing it as "saves
        more than $X" is a simplification, and someone tuning the number
        against a stopwatch deserves to find that written down rather
        than discover it.
        """
        cheap_late = np.full(N, 0.30)
        cheap_late[8:10] = 0.05  # real saving = 2 kWh x $0.25 = $0.50
        waits = {
            x: _run_periods(_solve(_load(min_deferral_saving_dollars=x), cheap_late))
            == [8, 9]
            for x in (0.0, 0.10, 0.40, 0.60, 0.70, 1.00)
        }
        self.assertEqual(
            waits,
            {0.0: True, 0.10: True, 0.40: True, 0.60: True, 0.70: False, 1.00: False},
            f"the flip should straddle the computed 0.6875 boundary, got {waits}",
        )


class TestThePenaltyHasTheStatedMagnitude(unittest.TestCase):
    """'Deliver at the deadline and it costs exactly X' is the promise
    made to the household. Measured against a flat price, where the only
    thing that can move cost is the penalty itself."""

    def test_the_earliest_placement_costs_the_minimum_possible(self):
        """Against a flat price the threshold's own pull decides, and it
        pulls as early as the load can physically go.

        Note what that is NOT: zero. Only period 0 carries zero
        lateness, and a 2 kWh target at 1 kW needs two periods, so the
        second one is genuinely one hour late and priced accordingly --
        `rate * 1 * 1 = X / 22`. A test asserting "costs nothing extra"
        would be asserting something the mechanism cannot deliver for
        any load that runs longer than one period, which is all of them.
        """
        flat = np.full(N, 0.20)
        baseline = _solve(_load(), flat)
        with_threshold = _solve(_load(min_deferral_saving_dollars=1.0), flat)

        self.assertEqual(_run_periods(with_threshold), [0, 1])
        # rate = (1.0 / 2.0) / 11 = 1/22; lateness sum over [0, 1] is 1.
        self.assertAlmostEqual(
            with_threshold.total_cost - baseline.total_cost, 1.0 / 22.0, places=6
        )

    def test_forced_to_the_deadline_it_costs_about_x(self):
        """Pin the magnitude by giving the load nowhere else to go: a
        window of exactly its own length at the END of the horizon still
        anchors lateness to that window's own start, so the penalty is
        measured, not assumed."""
        flat = np.full(N, 0.20)
        # Two periods of delivery inside a two-period window -> the
        # window's whole span, so the penalty is the full X only if the
        # load runs at the very last period. With window_hours = 1 and
        # both periods used, half the energy sits at lateness 0 and half
        # at lateness 1, so the expected penalty is X/2.
        late = _load(earliest_period=10, deadline_period=11)
        baseline = _solve(late, flat)
        with_threshold = _solve(
            _load(
                earliest_period=10,
                deadline_period=11,
                min_deferral_saving_dollars=1.0,
            ),
            flat,
        )
        self.assertAlmostEqual(
            with_threshold.total_cost - baseline.total_cost, 0.5, places=4
        )


class TestItIsAllowedToBeatRealPrices(unittest.TestCase):
    """The principle this field deliberately breaks, pinned so it is not
    quietly restored. Every other soft cost in this codebase is capped
    below MIN_CHARGE_DISCHARGE_COST_SPREAD (0.01 $/kWh) precisely so it
    can never override a genuine economic signal. This one must."""

    def test_the_effective_spread_far_exceeds_the_tie_break_ceiling(self):
        target_kwh, threshold = 4.0, 1.0
        effective_spread_per_kwh = threshold / target_kwh
        self.assertAlmostEqual(effective_spread_per_kwh, 0.25, places=9)
        self.assertGreater(
            effective_spread_per_kwh,
            0.01 * 20,
            "if this ever falls back under the tie-break ceiling the "
            "field stops doing the only thing it was asked to do",
        )

    def test_it_overrides_a_saving_the_613_budget_could_never_touch(self):
        """#613's whole-horizon budget is at most 0.01 $/kWh of pull. A
        3.29 c/kWh signal -- the exact figure #769 reported -- beats it
        comfortably, and must not beat this."""
        price = np.full(N, 0.30)
        price[8:10] = 0.30 - 0.0329
        saving = 2.0 * 0.0329  # about 6.6 cents
        plan = _solve(_load(min_deferral_saving_dollars=0.50), price)
        self.assertEqual(_run_periods(plan), [0, 1])
        self.assertLess(saving, 0.50)


class TestWindowedLoadsGetTheSameSemanticsPerWindow(unittest.TestCase):
    """nimbus #612 loads have several windows. The field has to mean the
    same thing inside each of them, or its meaning drifts as windows are
    added."""

    def test_lateness_is_measured_from_each_windows_own_start(self):
        flat = np.full(N, 0.20)
        windows = (
            AdequacyWindow(earliest_period=0, deadline_period=3, target_kwh=1.0),
            AdequacyWindow(earliest_period=8, deadline_period=11, target_kwh=1.0),
        )
        load = AdequacyLoadConfig(
            name="hws",
            max_power_kw=1.0,
            target_kwh=1.0,
            earliest_period=0,
            deadline_period=3,
            shortfall_price=10.0,
            windows=windows,
            min_deferral_saving_dollars=1.0,
        )
        plan = _solve(load, flat)
        # Each window pulls to its OWN opening, not to period 0 -- if
        # lateness were measured from the horizon start, the second
        # window's periods would all carry a large constant penalty and
        # the load would prefer to under-deliver there instead.
        self.assertEqual(_run_periods(plan), [0, 8])


class TestValidation(unittest.TestCase):
    def test_a_negative_threshold_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            _load(min_deferral_saving_dollars=-1.0)
        self.assertIn("must be >= 0", str(ctx.exception))

    def test_zero_is_accepted_because_it_is_the_off_switch(self):
        self.assertEqual(
            _load(min_deferral_saving_dollars=0.0).min_deferral_saving_dollars, 0.0
        )


if __name__ == "__main__":
    unittest.main()
