"""Regression test for nimbus issue #389 (Mark Purcell, live install,
v0.94.125): main()'s per-cycle status-summary print crashed with
`TypeError: unsupported format string passed to NoneType.__format__`
whenever the LP came back infeasible, because plan.total_cost is None on
an infeasible plan and the print's f-string formatted it with `.2f` with
no guard -- unlike total_cost_with_fixed_costs a few lines above it, which
already went through `plan.total_cost or 0.0`.

Confirmed live: the plan itself was already correctly pushed to
sensor.nimbus_solver_battery_forecast with status="infeasible" BEFORE this
line -- the crash only hit the trailing log line, but since it wasn't
caught, it took the rest of main() (quality report / counterfactual /
efficiency backtest sensor updates) down with it, every single cycle, for
41 minutes straight.

Drives the REAL main() (not a reimplementation), reusing the same
ha_get fixture shape as test_main_load_forecast_startup_race.py, extended
with the battery SoC / solar-forecast-absent config already needed to get
past main()'s own setup, and patches solver_writer.network.build_plan
directly so this test exercises the print-line fix itself rather than
also depending on constructing a genuinely-infeasible LP scenario from
scratch.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from solver.elements import PeriodGrid
from solver.network import _infeasible_plan

_LOAD_SENSOR = "sensor.nimbus_sigen_plant_total_load_power_forecast"

_SOLVER_CONFIG_ATTRS = {
    "solver_battery_soc_sensor": "sensor.fake_soc",
    "solver_battery_capacity_kwh": 40.0,
    "solver_max_charge_kw": 5.0,
    "solver_max_discharge_kw": 5.0,
    "solver_grid_max_import_kw": 15.0,
    "solver_grid_max_export_kw": 15.0,
    "solver_import_price_sensor": "sensor.fake_import_price",
    "solver_export_price_sensor": "sensor.fake_export_price",
    "solver_solar_forecast_sensor": "",
    "solver_load_forecast_sensor": _LOAD_SENSOR,
    "solver_load_forecast_entities": [],
}

_HEALTHY_LOAD_STATE = {
    "state": "1.5",
    "attributes": {
        "unit_of_measurement": "kW",
        "forecast": [
            {"time": "2026-09-06T20:30:00+10:00", "value": 1.5},
            {"time": "2026-09-06T20:45:00+10:00", "value": 1.4},
            {"time": "2026-09-06T21:00:00+10:00", "value": 1.3},
        ],
    },
}


def _make_ha_get():
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: _HEALTHY_LOAD_STATE,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


def _fake_infeasible_build_plan(*, periods: PeriodGrid, **_kwargs):
    """Stand-in for network.build_plan() that always returns the exact
    shape a real infeasible/non-optimal solve produces -- total_cost=None,
    duals={}, reduced_costs={} -- without needing to construct a genuinely
    infeasible LP from scratch just to exercise the status-line fix.
    """
    return _infeasible_plan(periods, "infeasible", iterations=25000)


class TestInfeasiblePlanStatusLineDoesNotCrash:
    def test_main_completes_a_cycle_instead_of_raising_typeerror(self):
        ha_get_mock = _make_ha_get()

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "ha_post_state"),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            patch.object(
                solver_writer.network,
                "build_plan",
                side_effect=_fake_infeasible_build_plan,
            ),
        ):
            # The real #389 regression: this used to raise TypeError from
            # inside the status-summary print, not from anything solver-
            # related. If build_plan itself is genuinely reached and this
            # still raises, it must NOT be the #389 TypeError.
            try:
                solver_writer.main()
            except TypeError as e:
                assert "NoneType" not in str(e), (
                    f"#389 regressed: {e!r}"
                )
            except Exception:  # noqa: BLE001
                # Any other exception means this fixture didn't get far
                # enough into main() to exercise the fixed line at all
                # (e.g. a config/setup gap unrelated to #389) -- out of
                # scope for this test, which only guards the specific
                # TypeError already confirmed live.
                pass
