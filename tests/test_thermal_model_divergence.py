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


class TestTheModelsNowAgreeWhereverTheLoadRunsAtFullPower(unittest.TestCase):
    """nimbus issue #897, resolved 2026-09-15: the household decided the
    learned rate is NET, so the LP no longer subtracts the loss a second
    time while heating.

    This class used to pin the opposite -- `LP = projection - decay x
    cumulative heating hours` -- and its own docstring said it existed so
    that whoever resolved #897 would have to change it deliberately
    rather than by accident. This is that deliberate change.

    What replaced the always-subtract term is the period's IDLE FRACTION,
    `decay * (1 - power/max_power)`. That is linear, so it needs no
    per-period binary (which #773 shows this model cannot afford), and it
    is physically truer than either extreme: heat for the whole period
    and the net rate already carries the loss; sit idle and you lose the
    lot; run at half duty and you lose half.

    The consequence worth stating plainly, because it is the useful one:
    **the two models now agree exactly wherever the load runs at full
    power**, which is how a resistive element or a heat pump actually
    runs. The residual gap is only ever the partial-duty periods the LP
    uses to land precisely on its target.
    """

    def setUp(self):
        self.tl = _solve()
        self.projected = _project(self.tl.power_kw)
        self.on = [float(p) > 0.05 for p in self.tl.power_kw]
        self.max_power_kw = 3.0  # matches _solve()'s own fixture

    def test_the_lp_actually_heats_so_the_comparison_is_not_vacuous(self):
        """If the LP chose never to run the load, both models would be
        pure decay and would agree trivially -- every assertion below
        would pass while comparing nothing."""
        self.assertTrue(any(self.on), "fixture must produce real heating periods")

    def test_a_full_power_heating_period_adds_no_gap_at_all(self):
        """#897's fix, stated as directly as it can be. Before it, every
        heating period drove the two models apart by a full period's
        decay; now a full-power one contributes nothing."""
        full = [
            t
            for t in range(_N)
            if abs(float(self.tl.power_kw[t]) - self.max_power_kw) < 1e-6
        ]
        self.assertTrue(
            full,
            "fixture no longer contains a full-power heating period, so this "
            "assertion proves nothing -- fix the fixture, not the assertion",
        )
        for t in full:
            prev_gap = (
                0.0
                if t == 0
                else float(self.projected[t - 1]["value"])
                - float(self.tl.temperature_c[t - 1])
            )
            gap = float(self.projected[t]["value"]) - float(self.tl.temperature_c[t])
            with self.subTest(period=t):
                self.assertAlmostEqual(gap, prev_gap, places=6)

    def test_the_remaining_gap_is_exactly_the_idle_fraction_of_each_period(self):
        """The closed form of what is left. Names the term responsible,
        the same way the old assertion did for the term that went."""
        expected = 0.0
        for t in range(_N):
            p_kw = float(self.tl.power_kw[t])
            lp_decay = _DECAY * _HOURS * (1.0 - p_kw / self.max_power_kw)
            proj_decay = 0.0 if self.on[t] else _DECAY * _HOURS
            expected += lp_decay - proj_decay
            actual = float(self.projected[t]["value"]) - float(self.tl.temperature_c[t])
            with self.subTest(period=t):
                # places=1, not 6: both series are ROUNDED at publish (the
                # LP trajectory to 1dp, the projection to 2dp), so a tighter
                # comparison measures that rounding rather than either
                # model. Same tolerance the pre-#897 version of this
                # assertion used, for the same reason.
                self.assertAlmostEqual(actual, expected, places=1)

    def test_they_still_agree_exactly_while_the_load_is_idle(self):
        """Unchanged by #897, and still the reason this was invisible on
        an install whose load had not run yet."""
        first_on = self.on.index(True)
        for t in range(first_on):
            with self.subTest(period=t):
                self.assertAlmostEqual(
                    float(self.projected[t]["value"]),
                    float(self.tl.temperature_c[t]),
                    places=6,
                )


class TestTheSizeOfIt(unittest.TestCase):
    def test_the_gap_is_now_much_smaller_than_the_model_it_replaced(self):
        """A guard on the direction of the change. The pre-#897 model
        diverged by a full period's decay for EVERY heating period; the
        replacement only ever diverges on partial-duty ones, so on this
        fixture the gap must be strictly smaller than the old closed
        form. If it is not, the fix did not do what it claims."""
        tl = _solve()
        projected = _project(tl.power_kw)
        heating_hours = sum(_HOURS for p in tl.power_kw if float(p) > 0.05)
        old_model_gap = _DECAY * heating_hours

        gap = float(projected[-1]["value"]) - float(tl.temperature_c[-1])
        self.assertLess(gap, old_model_gap - 1e-9)
        self.assertGreaterEqual(gap, 0.0)


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

    def test_the_lp_config_now_carries_the_ambient_pair(self):
        """INVERTED by nimbus issue #481, and this test predicted its own
        inversion: it used to assert the fields were absent, noting that
        *"closing #897 toward the ambient model means adding a field here,
        not changing a number"*. That is exactly what happened -- the LP
        recursion now scales decay by the load-to-ambient gap when the pair
        is supplied.

        Updated rather than deleted, and deliberately so: #897's own closing
        comment set the rule that "closing this issue means that difference
        changes deliberately, so that file gets updated as part of the
        change, not around it".

        The pair is still OPTIONAL, which is why the three tests below keep
        passing unchanged -- they drive the flat path, and an install that
        supplies no weather behaves exactly as it did before. So the
        divergence this class is named for is now CLOSABLE rather than
        closed: it disappears for a load carrying the pair, and remains for
        one that does not.
        """
        import dataclasses

        from solver.elements import ThermalLoadConfig

        fields = {f.name: f for f in dataclasses.fields(ThermalLoadConfig)}
        self.assertIn("loss_coeff_per_h", fields)
        self.assertIn("ambient_c", fields)
        # Optional on both, or an existing install's behaviour would change
        # the moment it upgraded -- this model drives real hot-water and pool
        # dispatch today.
        self.assertIsNone(fields["loss_coeff_per_h"].default)
        self.assertIsNone(fields["ambient_c"].default)
        # The flat rate stays, as the fallback for every install without a
        # weather sensor -- #864's own promised degradation path.
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
