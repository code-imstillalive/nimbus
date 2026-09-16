"""nimbus issue #940: the echoed thermal rates must be REPORTING ONLY.

#940 publishes the two rates the LP was actually built with, plus where
each came from, so a household can finally see which numbers drove a real
dispatch decision. Today every `kind=thermal` load silently runs on
`thermal_forecast`'s 8.0 C/kWh and 0.5 C/h module defaults (#873 -- the
learner never runs for this kind), while the published learned fields
read `None` and `thermal_rates_source` reads `""`.

**This file is the #594 release-timing proof.** The change touches
`solver/network.py`, so the overnight hold applies unless the carve-out
is earned, and #940's own filing names the shape the proof has to take:
that the addition is purely additive to a result dataclass, with nothing
in the LP reading the new fields and no constraint or objective built
from them.

The test is therefore not "the new fields have the right values" -- that
is covered in `test_solver_writer_thermal_load_resolution.py`, against
the one site that resolves the precedence. The test here is that varying
them changes **nothing** about what the solver decides: same plan, to the
bit, under three different origin labels and with the fields left unset
entirely.

Why that is worth a file of its own rather than an assertion tacked onto
an existing thermal test: a field echoed onto a config object is exactly
the kind of thing a later refactor can start *reading* without anyone
noticing, at which point a display value silently becomes an input to
dispatch. This fails if that ever happens.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

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

_N = 12


def _periods() -> PeriodGrid:
    return PeriodGrid(
        hours=np.array([1.0] * _N), start=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )


def _grid() -> GridConfig:
    # A real cheap/expensive swing, so the LP has genuine scheduling
    # freedom and the thermal block's placement is a real decision rather
    # than forced -- a scenario with only one feasible answer would make
    # this test pass no matter what the new fields did.
    import_price = np.array([0.05] * 6 + [0.60] * 6)
    return GridConfig(
        import_price=import_price,
        export_price=import_price - 0.02,
        import_limit_kw=100.0,
        export_limit_kw=100.0,
    )


def _battery() -> BatteryConfig:
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
        salvage_value=0.10,
    )


def _thermal(**overrides) -> ThermalLoadConfig:
    kwargs = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 42.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": _N - 1,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.5,
    }
    kwargs.update(overrides)
    return ThermalLoadConfig(**kwargs)


def _solve(thermal: ThermalLoadConfig):
    return build_plan(
        periods=_periods(),
        grid=_grid(),
        batteries=[_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(_N, 1.0))],
        thermal_loads=[thermal],
    )


def _dispatch_fingerprint(plan):
    """Every number a household or a downstream consumer could act on."""
    tl = plan.thermal_loads[0]
    return {
        "status": plan.status,
        "total_cost": plan.total_cost,
        "battery_charge_kw": plan.battery_charge_kw.tobytes(),
        "battery_discharge_kw": plan.battery_discharge_kw.tobytes(),
        "battery_soc_kwh": plan.battery_soc_kwh.tobytes(),
        "grid_import_kw": plan.grid_import_kw.tobytes(),
        "grid_export_kw": plan.grid_export_kw.tobytes(),
        "thermal_power_kw": tl.power_kw.tobytes(),
        "thermal_temperature_c": tl.temperature_c.tobytes(),
    }


class TestTheEchoedRatesDoNotReachTheLP(unittest.TestCase):
    def setUp(self):
        self.baseline_plan = _solve(_thermal())
        self.baseline = _dispatch_fingerprint(self.baseline_plan)

    def test_the_scenario_actually_exercises_the_thermal_model(self):
        """Guard on the premise. If the thermal load never runs, or the
        battery never moves, every identity assertion below would hold
        trivially and prove nothing.
        """
        self.assertEqual(self.baseline_plan.status, "optimal")
        tl = self.baseline_plan.thermal_loads[0]
        self.assertGreater(
            float(np.sum(tl.power_kw)),
            0.0,
            "the thermal load must actually heat in this scenario",
        )
        self.assertGreater(
            float(np.sum(self.baseline_plan.battery_charge_kw))
            + float(np.sum(self.baseline_plan.battery_discharge_kw)),
            0.0,
            "the battery must actually move in this scenario",
        )
        # Deliberately not "last > first": with cheap power in the first
        # half the LP heats early and then lets the tank COAST DOWN to
        # land on target exactly at the deadline, so the final
        # temperature is genuinely below the peak. Asserting a rising
        # trajectory would be asserting a worse plan.
        self.assertGreater(
            float(np.max(tl.temperature_c)),
            42.0,
            "the tank must actually warm up above its initial 42 C, or "
            "temperature_c is not being solved",
        )
        self.assertGreaterEqual(
            float(tl.temperature_c[_N - 1]),
            60.0 - 1e-6,
            "the deadline constraint must actually bind at the target",
        )

    def test_every_origin_label_produces_a_bit_identical_plan(self):
        for heating_origin, decay_origin in (
            ("override", "override"),
            ("learned", "learned"),
            ("fallback", "fallback"),
            ("override", "fallback"),
            (None, None),
        ):
            with self.subTest(heating=heating_origin, decay=decay_origin):
                plan = _solve(
                    _thermal(
                        heating_rate_origin=heating_origin,
                        idle_decay_origin=decay_origin,
                    )
                )
                self.assertEqual(
                    _dispatch_fingerprint(plan),
                    self.baseline,
                    "the origin labels are reporting-only -- if changing one moves "
                    "any dispatch number, a display field has become an input to "
                    "the LP, which is exactly what #940 said it must not be",
                )

    def test_the_labels_survive_onto_the_plan_unchanged(self):
        """The other half: inert is not the same as ignored. The values
        have to arrive on the plan exactly as given, or the publish site
        would report something the LP was not built with -- the precise
        failure #940 exists to prevent.
        """
        plan = _solve(
            _thermal(
                heating_rate_c_per_kwh=11.25,
                idle_decay_c_per_hour=0.375,
                heating_rate_origin="override",
                idle_decay_origin="learned",
            )
        )
        tl = plan.thermal_loads[0]
        self.assertEqual(tl.heating_rate_c_per_kwh, 11.25)
        self.assertEqual(tl.idle_decay_c_per_hour, 0.375)
        self.assertEqual(tl.heating_rate_origin, "override")
        self.assertEqual(tl.idle_decay_origin, "learned")

    def test_an_unset_origin_echoes_as_none_not_as_fallback(self):
        """`None` means nobody recorded a provenance; `"fallback"` means
        the module default was actually selected. Collapsing the two
        would have the plan assert a provenance no code established --
        and every direct-construction test in this repo builds a
        ThermalLoadConfig without origins.
        """
        tl = _solve(_thermal()).thermal_loads[0]
        self.assertIsNone(tl.heating_rate_origin)
        self.assertIsNone(tl.idle_decay_origin)

    def test_the_echoed_rates_match_the_config_the_lp_was_built_with(self):
        """Echoed, not re-derived. Uses `replace()` on the very object
        handed to build_plan(), so a divergence could only come from the
        echo itself.
        """
        cfg = _thermal(heating_rate_c_per_kwh=6.5, idle_decay_c_per_hour=0.8)
        tl = _solve(replace(cfg)).thermal_loads[0]
        self.assertEqual(tl.heating_rate_c_per_kwh, cfg.heating_rate_c_per_kwh)
        self.assertEqual(tl.idle_decay_c_per_hour, cfg.idle_decay_c_per_hour)

    def test_a_genuinely_different_rate_does_still_change_dispatch(self):
        """The control. The fields above are inert because they are
        labels; the RATES themselves are real physics and must still move
        the plan. Without this, a bug that ignored the rates entirely
        would sail through every identity assertion in this file.
        """
        faster = _solve(_thermal(heating_rate_c_per_kwh=20.0))
        self.assertNotEqual(
            _dispatch_fingerprint(faster),
            self.baseline,
            "a tank that heats 2.5x faster must produce a different plan -- if "
            "not, heating_rate_c_per_kwh is not reaching the LP either",
        )


if __name__ == "__main__":
    unittest.main()
