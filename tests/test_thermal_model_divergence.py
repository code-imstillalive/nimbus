"""nimbus issue #897: the LP's thermal recursion and the published
temperature projection are two different models of the same tank, and
nothing compared them until this file.

- `solver/network.py`'s recursion subtracts `idle_decay_c_per_hour *
  hours[t]` in **every** period, heating ones included.
- `thermal_forecast.project_temperature_forecast()` is a strict
  either/or: a heating period gains and does **not** decay.

Both behaviours are deliberate in their own place and both already have
passing tests. Neither test looks at the other model, which is exactly
how a 3 °C disagreement over a single four-hour run went unnoticed.

**These tests do not assert the two models agree.** They cannot — the
disagreement is real today, and choosing which model is right changes
live dispatch on a real hot water system, which is #897's open question
and not one a test should settle by fiat. What they do instead is pin
the disagreement to its exact closed form:

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

    def test_the_projection_runs_hotter_than_the_guaranteed_trajectory(self):
        """Direction stated on its own: what the household is shown is
        the optimistic one, and the LP's hard guarantee is against the
        colder number."""
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
