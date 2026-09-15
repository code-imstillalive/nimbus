"""nimbus issue #897: the LP's thermal recursion and the temperature
projection are two different equations, and nothing compared them until
this file.

- `solver/network.py`'s recursion subtracts `idle_decay_c_per_hour *
  hours[t]` in **every** period, heating ones included.
- `thermal_forecast.project_temperature_forecast()` is a strict
  either/or: a heating period gains and does **not** decay.

Both behaviours are deliberate in their own place and both already have
passing tests. Neither test looks at the other model.

**Scope, stated up front so this file is not read as more than it is.**
The two never touch the same load today: a `kind=deferrable` load learns
a rate and consumes it in the projection, while a `kind=thermal` load
publishes the LP's own solved `temperature_c` rather than a re-derived
projection — #774 designed that out deliberately, see `ThermalLoadPlan`'s
own docstring. **Nothing here says a household is shown a curve that
disagrees with the guarantee.** It is not.

What this file compares is the two **functions**, because #897's real
concern is narrower: `learn_thermal_rates()` fits a NET rate (whatever
the tank lost during a run is inside the measured change) while the LP's
recursion only balances for a GROSS one — and #873 would route a learned
value into that recursion for the first time.

**These tests do not assert the two models agree.** Choosing which is
right changes live dispatch on a real hot water system, which is #897's
open question and not one a test should settle by fiat. What they do
instead is pin the difference to its exact closed form:

    LP trajectory = projection − idle_decay_c_per_hour × (heating hours)

Green today, and loud the moment either model moves. Whoever resolves
#897 has to come here and change this file deliberately, which is the
whole point — the failure mode being guarded against is the two models
drifting further apart silently, the way they already did once.

Why the closed form matters rather than just "they differ": it names the
term responsible. `learn_thermal_rates()` fits `rate = net_gain / kWh`
from a measured idle-before → idle-after change, so the loss during a
run is already inside the learned rate. Subtracting it again is the
double-count #897 describes, and this file is where that claim is
executable rather than argued.

Real `build_plan()`, real projection function, no reimplementation of
either — the LP's own chosen power series is fed straight into the
projection, so the comparison is between the two shipped models and not
between one of them and a restatement of it.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
import thermal_forecast as tf
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan

_N = 24
_HOURS = 1.0
_DECAY = 0.3
_RATE = 8.0
_T0 = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)


def _solve():
    """One real LP solve with a thermal load, returning its own chosen
    power series and its own guaranteed temperature trajectory."""
    periods = PeriodGrid(hours=np.full(_N, _HOURS), start=None)
    plan = build_plan(
        periods=periods,
        grid=GridConfig(
            import_price=np.full(_N, 0.30),
            export_price=np.full(_N, 0.05),
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
        solar=SolarConfig(forecast_kw=np.zeros(_N)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(_N, 1.0))],
        thermal_loads=[
            ThermalLoadConfig(
                name="hws",
                max_power_kw=3.0,
                initial_temperature_c=40.0,
                target_temperature_c=60.0,
                earliest_period=0,
                deadline_period=_N - 1,
                heating_rate_c_per_kwh=_RATE,
                idle_decay_c_per_hour=_DECAY,
            )
        ],
    )
    assert plan.status == "optimal", plan.status
    return plan.thermal_loads[0]


def _project(power_kw):
    """The published projection, driven by the LP's own power series."""
    plan_forecast = [
        {"time": (_T0 + timedelta(hours=_HOURS * i)).isoformat(), "value": float(p)}
        for i, p in enumerate(power_kw)
    ]
    return tf.project_temperature_forecast(
        plan_forecast,
        start_temperature=40.0,
        heating_rate_c_per_kwh=_RATE,
        idle_decay_c_per_hour=_DECAY,
        on_threshold_kw=0.05,
    )


class TestTheTwoModelsDisagreeByExactlyTheHeatingPeriodDecay(unittest.TestCase):
    def setUp(self):
        self.tl = _solve()
        self.projected = _project(self.tl.power_kw)
        self.on = [float(p) > 0.05 for p in self.tl.power_kw]

    def test_the_lp_actually_heats_so_the_comparison_is_not_vacuous(self):
        """If the LP chose never to run the load, both models would be
        pure decay and would agree trivially — every assertion below
        would pass while comparing nothing."""
        self.assertTrue(any(self.on), "fixture must produce real heating periods")

    def test_the_gap_is_decay_times_cumulative_heating_hours(self):
        """The closed form, period by period. This is the assertion that
        names which term is responsible."""
        heating_hours = 0.0
        for t in range(_N):
            if self.on[t]:
                heating_hours += _HOURS
            expected_gap = _DECAY * heating_hours
            actual_gap = float(self.projected[t]["value"]) - float(
                self.tl.temperature_c[t]
            )
            with self.subTest(period=t):
                self.assertAlmostEqual(actual_gap, expected_gap, places=1)

    def test_the_projection_runs_hotter_than_the_lp_trajectory(self):
        """Direction stated on its own, as a property of the two
        FUNCTIONS — not of anything published. It matters because it says
        which way a net-vs-gross rate mix-up would push a plan: toward
        believing the tank is colder than it is, and so toward demanding
        a longer heating block than reality needs."""
        final_gap = float(self.projected[-1]["value"]) - float(
            self.tl.temperature_c[-1]
        )
        self.assertGreater(final_gap, 0.0)

    def test_the_gap_never_shrinks(self):
        """Monotonic by construction — decay is only ever added to one
        side. A shrinking gap would mean one of the two models had
        changed shape, not merely drifted in magnitude."""
        gaps = [
            float(self.projected[t]["value"]) - float(self.tl.temperature_c[t])
            for t in range(_N)
        ]
        for t in range(1, _N):
            with self.subTest(period=t):
                self.assertGreaterEqual(gaps[t], gaps[t - 1] - 1e-6)

    def test_they_agree_exactly_while_the_load_is_idle(self):
        """Before the first heating period the two models are identical —
        which is why this was invisible on any install whose load had not
        run yet, and why the gap is a property of running the tank, not
        of configuring it."""
        first_on = self.on.index(True)
        for t in range(first_on):
            with self.subTest(period=t):
                self.assertAlmostEqual(
                    float(self.projected[t]["value"]),
                    float(self.tl.temperature_c[t]),
                    places=6,
                )


class TestTheSizeOfIt(unittest.TestCase):
    def test_the_gap_is_material_not_a_rounding_difference(self):
        """A guard against anyone reading #897 as pedantry. On this
        fixture the divergence is whole degrees, and it scales linearly
        with how long the tank runs."""
        tl = _solve()
        projected = _project(tl.power_kw)
        gap = float(projected[-1]["value"]) - float(tl.temperature_c[-1])
        self.assertGreater(gap, 0.5)


if __name__ == "__main__":
    unittest.main()


def _project_ambient(power_kw, ambient_c, loss_coeff_per_h):
    """The same projection with #481's ambient-scaled decay in force —
    what an install with `solver_weather_forecast_sensor` configured
    actually gets."""
    plan_forecast = [
        {"time": (_T0 + timedelta(hours=_HOURS * i)).isoformat(), "value": float(p)}
        for i, p in enumerate(power_kw)
    ]
    ambient_forecast = [
        {"time": (_T0 + timedelta(hours=_HOURS * i)).isoformat(), "value": ambient_c}
        for i in range(len(power_kw))
    ]
    return tf.project_temperature_forecast(
        plan_forecast,
        start_temperature=40.0,
        heating_rate_c_per_kwh=_RATE,
        idle_decay_c_per_hour=_DECAY,
        on_threshold_kw=0.05,
        loss_coeff_per_h=loss_coeff_per_h,
        ambient_forecast=ambient_forecast,
    )


class TestTheAmbientCovariateWidensTheDivergence(unittest.TestCase):
    """nimbus issue #897, second divergence — found 2026-09-15.

    `ThermalLoadConfig`'s own docstring justifies the flat LP model by
    claiming the LP and the display projection *"share the IDENTICAL
    physics model, by construction — they can never disagree"*, and that
    an ambient forecast series *"does not exist anywhere in this codebase
    today"*.

    Both were true when written (#774/#800, 2026-09-13) and **false one
    day later**: #481/#864 (v0.94.293, 2026-09-14) learns
    `loss_coeff_per_h` and applies Newton's-law decay in the projection
    whenever a weather sensor is configured. The LP recursion still
    subtracts a flat `idle_decay_c_per_hour * hours[t]` with no ambient
    term anywhere.

    So the warned-against shape arrived with the roles swapped: an
    ambient-scaled projection against a flat LP. Nobody erred — #864 is
    correctly scoped to the projection and says so — the invariant broke
    because the two changes sat on different issues and nothing checked
    the claim.

    This only bites on an install with `solver_weather_forecast_sensor`
    set. The reference household has one; devhub does not, which is
    exactly why devhub cannot surface it.

    As with the tests above, these pin the disagreement rather than
    asserting agreement: which model is right changes live dispatch on a
    real hot water system, and that is #897's open question.
    """

    def test_the_lp_config_has_no_ambient_input_at_all(self):
        """Structural, and the reason the projection can never be matched
        by tuning: `ThermalLoadConfig` carries nowhere to put a loss
        coefficient or an ambient series. Closing #897 toward the
        ambient model means adding a field here, not changing a number."""
        import dataclasses

        from solver.elements import ThermalLoadConfig

        fields = {f.name for f in dataclasses.fields(ThermalLoadConfig)}
        self.assertNotIn("loss_coeff_per_h", fields)
        self.assertNotIn("ambient_forecast", fields)
        self.assertIn("idle_decay_c_per_hour", fields)

    def test_configuring_a_weather_sensor_changes_the_projection(self):
        """The two decay models genuinely differ on the same plan. If this
        ever stops being true, either #481's covariate stopped applying or
        the LP grew an ambient term — both worth knowing."""
        tl = _solve()
        flat = _project(tl.power_kw)
        # A 20 C ambient against a 40-60 C tank: the real gap range the
        # reference household's own 4-segment fit was taken over.
        ambient = _project_ambient(tl.power_kw, 20.0, 0.02)
        self.assertNotAlmostEqual(
            float(flat[-1]["value"]), float(ambient[-1]["value"]), places=2
        )

    def test_the_ambient_projection_also_disagrees_with_the_lp(self):
        """The point of #897, restated for the second divergence: on an
        install with a weather sensor, what the household is shown and
        what the LP guaranteed are computed from different physics, not
        merely different bookkeeping."""
        tl = _solve()
        ambient = _project_ambient(tl.power_kw, 20.0, 0.02)
        gap = float(ambient[-1]["value"]) - float(tl.temperature_c[-1])
        self.assertNotAlmostEqual(gap, 0.0, places=2)

    def test_without_a_weather_sensor_the_projection_stays_flat(self):
        """The degradation path #864 promised: no coefficient, no ambient
        series, byte-identical to the pre-#481 behaviour — which is why
        devhub sees none of this."""
        tl = _solve()
        flat = _project(tl.power_kw)
        none_configured = _project_ambient(tl.power_kw, 20.0, None)
        for a, b in zip(flat, none_configured, strict=True):
            self.assertAlmostEqual(float(a["value"]), float(b["value"]), places=9)
