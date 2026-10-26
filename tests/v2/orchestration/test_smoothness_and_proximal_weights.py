"""The intra-plan smoothness and proximal weights reach the solver, so a plan
does not jump mid-band on an unchanged price (#1837).
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import solver_shared
import solver_writer
from _isolated_state import isolated_state_paths
from solver.elements import PeriodGrid
from solver.network import _infeasible_plan

_LOAD_SENSOR = "sensor.nimbus_sigen_plant_total_load_power_forecast"

_BASE_CONFIG_ATTRS = {
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
            {"time": "2026-09-08T20:30:00+10:00", "value": 1.5},
            {"time": "2026-09-08T20:45:00+10:00", "value": 1.4},
            {"time": "2026-09-08T21:00:00+10:00", "value": 1.3},
        ],
    },
}


def _make_ha_get(config_attrs: dict):
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": config_attrs,
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


def _run_main_and_capture_build_plan_kwargs(config_attrs: dict) -> dict:
    """Drives the REAL main() (not a reimplementation), patching
    solver_writer.network.build_plan to record exactly what it was
    called with, then returning an infeasible-plan stub so main()
    completes its cycle without needing a genuinely solvable LP -- same
    harness as test_main_infeasible_plan_status_line.py.
    """
    captured: dict = {}

    def _capture_and_return_infeasible(*, periods: PeriodGrid, **kwargs):
        captured.update(kwargs)
        return _infeasible_plan(periods, "infeasible", iterations=1)

    ha_get_mock = _make_ha_get(config_attrs)
    with (
        patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
        # nimbus #1302 (spec 003 step 2): safe_num moved to
        # solver_shared.py and resolves ha_get from THAT namespace, so
        # patching only solver_writer.ha_get lets its call escape to a
        # real HA. main()'s own reads still go through solver_writer's
        # alias, so BOTH targets are needed -- not one instead of the
        # other.
        patch.object(solver_shared, "ha_get", side_effect=ha_get_mock),
        patch.object(solver_writer, "ha_post_state"),
        patch.object(solver_writer, "acquire_lock", return_value=True),
        patch.object(solver_writer, "release_lock"),
        patch.object(
            solver_writer.network,
            "build_plan",
            side_effect=_capture_and_return_infeasible,
        ),
        # nimbus issue #1330: main() writes real /opt state paths by default.
        isolated_state_paths(solver_writer),
    ):
        try:
            solver_writer.main()
        except Exception:  # noqa: BLE001, S110
            # Same "test scope boundary reached" allowance as
            # test_main_infeasible_plan_status_line.py -- this test only
            # cares whether build_plan() was reached and what it was
            # called with, captured via the patch above regardless of
            # what main() does afterward.
            pass
    return captured


class TestBatteryChargeEarlinessBudgetWiring:
    """nimbus issue #692: same real gap, same fix, for the battery's own
    new charge-earliness tie-break -- a household directly asked to be
    able to tune this themselves after watching its own magnitude get
    hand-corrected in code, same "must actually reach build_plan(), not
    a silently-hardcoded constant" requirement as its two siblings above.
    """

    def test_a_configured_earliness_budget_reaches_build_plan(self):
        attrs = dict(_BASE_CONFIG_ATTRS, solver_battery_charge_earliness_budget_kw=0.02)
        kwargs = _run_main_and_capture_build_plan_kwargs(attrs)
        assert kwargs.get("battery_charge_earliness_budget_kw") == 0.02
