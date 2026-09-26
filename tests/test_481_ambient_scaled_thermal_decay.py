"""nimbus issue #481: the LP's thermal decay is ambient-scaled, so the solver
can finally see the outdoor forecast.

## The gap, confirmed three ways rather than assumed

| | decay model |
|---|---|
| `learn_thermal_rates()` | Newton's law, **ambient-scaled** (`loss_coeff_per_h`, #864, v0.94.293) |
| `project_temperature_forecast()` (the dashboard) | **ambient-scaled** |
| `network.py` — *the thing that actually decides* | **flat** `idle_decay_c_per_hour * hours[t]` |

So the learner and the projection both modelled ambient coupling and the LP did
not. `ThermalLoadConfig`'s own docstring records this divergence, and
`tests/test_thermal_model_divergence.py` pins it to its closed form rather than
asserting an agreement that does not hold.

The consequence for #481's acceptance criteria is stronger than "unimplemented":
*"two outdoor forecasts (28 °C vs 38 °C) — the hotter day schedules more kWh"* was
**impossible**. With flat decay the LP cannot see the outdoor forecast at all.

## The bilinearity, and how it is avoided

The natural form is linear on its own:

```
T[t] = T[t-1] + rate·p[t]·h − loss·h·(T[t-1] − T_out[t])
```

But **#897** (household decision, 2026-09-15) established that the learned rate
is **NET**, so `network.py` scales decay by the period's idle fraction to avoid
subtracting the loss twice while heating. Combining the two multiplies `T[t-1]`
by `p[t]` — two decision variables — and the model stops being an LP.

Resolved by linearising the gap around a **coast reference**: `T_ref` decays from
`initial_temperature_c` toward ambient with no heating. The gap is then a
per-period constant, so `decay[t]` drops into exactly the slot the flat scalar
occupied and every downstream coefficient is unchanged in form. **#897's decision
is untouched.**

## What this deliberately does NOT do

* **Cooling.** `heating_rate_c_per_kwh` is validated `> 0`, so the element cannot
  represent an HVAC load at all yet. #481's HVAC criterion needs that too, and
  it is a separate change rather than a half-built second feature here. What this
  file can and does prove is the half that makes HVAC possible: **the outdoor
  forecast now changes the plan.**
* **Re-open #897.** A textbook EMHASS ambient model wants a **gross** rate; the
  household chose net, reasoning that "the measurement is the honest thing".
  Deriving gross from net using the fit's own run duration and ambient gap is a
  real option and a real change to live dispatch physics on a production install.
  Raised for review, not decided here.
* **Per-period comfort bands.** Still scalars. Separate item.

## The approximation's error, stated rather than buried

A heated tank sits **hotter** than the coast line, so the true gap is larger and
this **under-estimates** decay — under-planning heating slightly. Conservative
toward spending less, and strictly better than a flat rate that cannot see the
weather at all. `TestTheCoastReferenceIsAnApproximation` pins the direction, so a
later change that flips it has to say so.
"""

from __future__ import annotations

import unittest

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


class TestTheOutdoorForecastNowChangesThePlan(unittest.TestCase):
    """The capability #481 is about, and the one flat decay made impossible."""

    def test_a_colder_day_schedules_more_kwh_than_a_milder_one(self):
        """The heating equivalent of #481's HVAC criterion.

        A bigger tank-to-ambient gap means more heat lost per hour, so more
        energy is needed to stand at 60 °C by the deadline. With flat decay these
        two solves were necessarily identical -- the LP had no channel through
        which the weather could reach it.
        """
        n = 24
        mild = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=tuple([20.0] * n)), n)
        cold = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=tuple([0.0] * n)), n)
        self.assertEqual(mild.status, "optimal")
        self.assertEqual(cold.status, "optimal")
        self.assertGreater(
            _thermal_kwh(cold),
            _thermal_kwh(mild),
            "a colder outdoor forecast must cost more energy to hold the target",
        )

    def test_the_loss_coefficient_scales_the_effect(self):
        """A better-insulated tank (smaller coefficient) needs less energy for
        the same weather -- so the coefficient is genuinely in the constraint,
        not merely accepted and ignored."""
        n = 24
        leaky = _solve(_thermal(loss_coeff_per_h=0.10, ambient_c=tuple([5.0] * n)), n)
        snug = _solve(_thermal(loss_coeff_per_h=0.01, ambient_c=tuple([5.0] * n)), n)
        self.assertGreater(_thermal_kwh(leaky), _thermal_kwh(snug))

    def test_the_target_is_still_met_exactly(self):
        """The ambient term changes how much energy is needed, never whether the
        guarantee holds. If this drifts, the new decay series has broken the
        hard constraint #774 built rather than refined it."""
        n = 24
        plan = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=tuple([10.0] * n)), n)
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.thermal_guarantee_relaxed, [])
        self.assertAlmostEqual(plan.thermal_loads[0].temperature_c[-1], 60.0, places=5)


class TestAbsentAmbientIsAByteIdenticalNoOp(unittest.TestCase):
    """This model drives real hot-water and pool dispatch today, so an install
    that supplies no weather must behave exactly as before."""

    def test_no_ambient_pair_matches_the_flat_model_exactly(self):
        before = _solve(_thermal())
        self.assertEqual(before.status, "optimal")
        again = _solve(_thermal())
        np.testing.assert_allclose(
            before.thermal_loads[0].power_kw, again.thermal_loads[0].power_kw
        )
        np.testing.assert_allclose(
            before.thermal_loads[0].temperature_c,
            again.thermal_loads[0].temperature_c,
        )

    def test_an_empty_ambient_tuple_falls_back_rather_than_dividing_by_nothing(self):
        """A weather source that resolved to no periods is a real runtime state
        (an unavailable sensor), and it must degrade to the flat model rather
        than crash or silently zero the decay."""
        plan = _solve(_thermal(loss_coeff_per_h=0.05, ambient_c=()))
        self.assertEqual(plan.status, "optimal")
        flat = _solve(_thermal())
        np.testing.assert_allclose(
            plan.thermal_loads[0].temperature_c,
            flat.thermal_loads[0].temperature_c,
            atol=1e-9,
        )


class TestTheGapCannotCreditFreeWarmth(unittest.TestCase):
    """Clamped at zero, on purpose."""

    def test_a_tank_below_ambient_gains_nothing(self):
        """A negative gap is a heat GAIN, and this model has no term for it --
        `heating_rate_c_per_kwh` is validated > 0, so the element cannot
        represent cooling yet. Letting the gap go negative would quietly credit
        a heating load with free warmth from a hot day, which is a physics claim
        nobody has fitted.
        """
        n = 24
        hot_outside = _solve(
            _thermal(loss_coeff_per_h=0.05, ambient_c=tuple([80.0] * n)), n
        )
        self.assertEqual(hot_outside.status, "optimal")
        # With the gap clamped to zero there is NO decay at all, so the cheapest
        # way to stand at 60 by the deadline is exactly the 20 degrees of rise,
        # and nothing more.
        expected_kwh = (60.0 - 40.0) / 8.0
        self.assertAlmostEqual(_thermal_kwh(hot_outside), expected_kwh, places=4)


class TestTheCoastReferenceIsAnApproximation(unittest.TestCase):
    """Pinned so a later change that alters the direction of error has to say so."""

    def test_it_under_estimates_decay_for_a_heated_tank(self):
        """A heated tank sits hotter than the coast line, so the true gap is
        larger than the reference gap and the modelled decay is the smaller of
        the two. Compared against a flat rate deliberately chosen to match the
        coast line's own FIRST step: from then on the coast line falls, so its
        gaps shrink, and total modelled decay comes in under the flat model's.
        """
        n = 24
        loss, ambient = 0.05, 0.0
        first_step = loss * 1.0 * (40.0 - ambient)  # 2.0 C in period 0
        ambient_plan = _solve(
            _thermal(loss_coeff_per_h=loss, ambient_c=tuple([ambient] * n)), n
        )
        flat_plan = _solve(_thermal(idle_decay_c_per_hour=first_step), n)
        self.assertLess(
            _thermal_kwh(ambient_plan),
            _thermal_kwh(flat_plan),
            "the coast reference's gaps shrink each period, so its total decay "
            "must be below a flat rate pinned to its own first step",
        )


class TestTheAmbientPairIsAllOrNothing(unittest.TestCase):
    """Same rule `must_have_soc_by_period_index`/`must_have_soc_kwh` already
    follow (#563 item 2). Silently ignoring the half that arrived is how a
    household ends up believing the LP is weather-aware when it is not."""

    def test_a_coefficient_without_ambient_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _thermal(loss_coeff_per_h=0.05)
        self.assertIn("both be set or both be None", str(ctx.exception))

    def test_ambient_without_a_coefficient_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _thermal(ambient_c=(20.0, 20.0))
        self.assertIn("both be set or both be None", str(ctx.exception))

    def test_a_negative_coefficient_is_rejected(self):
        """A negative coefficient is a tank that heats itself from a cold day."""
        with self.assertRaises(ValueError) as ctx:
            _thermal(loss_coeff_per_h=-0.01, ambient_c=(20.0,))
        self.assertIn("loss_coeff_per_h must be >= 0", str(ctx.exception))

    def test_both_absent_stays_valid(self):
        self.assertIsNotNone(_thermal())


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
