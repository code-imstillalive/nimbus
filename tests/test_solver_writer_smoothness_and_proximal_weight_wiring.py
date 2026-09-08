"""Real household finding, 2026-09-08 (NUC1's own first day of live
dispatch, the household's new "Nimbus Battery Automation (LIVE)"): a
single solve's own battery_kw jumped mid-band (e.g. -16.2 -> -25.9 kW)
while the real import price hadn't actually changed yet -- exactly the
LP-degeneracy symptom network.py's own intra-plan-smoothness mechanism
(mechanism 4, added 2026-08-20) and cross-solve proximal-regularization
mechanism (mechanism 1) both exist to fix. tests/test_solver_intraplan_
smoothness.py already proves that LP mechanism eliminates a reconstructed
burst at zero extra cost -- what was still real and broken is that
solver_writer.py's own real build_plan() call site (main(), the writer
that actually produces sensor.nimbus_solver_battery_forecast) ALWAYS
passed network.py's own hardcoded DEFAULT_SMOOTHNESS_WEIGHT_KW/
DEFAULT_PROXIMAL_WEIGHT_KW module constants, with no household-visible
entity, no way to tune it, and (until this fix) no way to prove it was
ever actually reading anything live at all.

This file is the wiring proof: patches solver_writer.network.build_plan
(same harness as test_main_infeasible_plan_status_line.py) to CAPTURE the
kwargs main() actually calls it with, then confirms those kwargs equal
whatever number.nimbus_solver_intraplan_smoothness_weight_kw/
_proximal_weight_kw's live cfg value says -- not a hardcoded constant.
The companion no-op tests confirm an already-configured household (or a
config missing these two keys entirely, matching every install before
this fix shipped) sees BYTE-IDENTICAL behaviour: the exact same
DEFAULT_SMOOTHNESS_WEIGHT_KW/DEFAULT_PROXIMAL_WEIGHT_KW values as before,
not a silent behaviour change on upgrade.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
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
        patch.object(solver_writer, "ha_post_state"),
        patch.object(solver_writer, "acquire_lock", return_value=True),
        patch.object(solver_writer, "release_lock"),
        patch.object(
            solver_writer.network,
            "build_plan",
            side_effect=_capture_and_return_infeasible,
        ),
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


class TestSmoothnessWeightWiring:
    def test_a_configured_smoothness_weight_reaches_build_plan(self):
        """The real fix: a household's own live
        number.nimbus_solver_intraplan_smoothness_weight_kw value must
        actually reach build_plan()'s own smoothness_weight kwarg --
        before this fix, main() always passed network.py's own hardcoded
        DEFAULT_SMOOTHNESS_WEIGHT_KW regardless of what any entity said."""
        attrs = dict(_BASE_CONFIG_ATTRS, solver_intraplan_smoothness_weight_kw=0.02)
        kwargs = _run_main_and_capture_build_plan_kwargs(attrs)
        assert kwargs.get("smoothness_weight") == 0.02

    def test_missing_smoothness_weight_falls_back_to_the_original_constant(self):
        """No-op guarantee: a config with this key entirely absent (every
        real install before this fix shipped) must produce the EXACT same
        value main() always silently used before -- not a different
        default, not zero."""
        kwargs = _run_main_and_capture_build_plan_kwargs(dict(_BASE_CONFIG_ATTRS))
        assert (
            kwargs.get("smoothness_weight")
            == solver_writer.network.DEFAULT_SMOOTHNESS_WEIGHT_KW
        )


class TestProximalWeightWiring:
    def test_a_configured_proximal_weight_reaches_build_plan(self):
        """Same real fix, sibling mechanism: a household's own live
        number.nimbus_solver_proximal_weight_kw value must actually reach
        build_plan()'s own proximal_weight kwarg."""
        attrs = dict(_BASE_CONFIG_ATTRS, solver_proximal_weight_kw=0.03)
        kwargs = _run_main_and_capture_build_plan_kwargs(attrs)
        assert kwargs.get("proximal_weight") == 0.03

    def test_missing_proximal_weight_falls_back_to_the_original_constant(self):
        """No-op guarantee, mirroring the smoothness-weight test above."""
        kwargs = _run_main_and_capture_build_plan_kwargs(dict(_BASE_CONFIG_ATTRS))
        assert (
            kwargs.get("proximal_weight")
            == solver_writer.network.DEFAULT_PROXIMAL_WEIGHT_KW
        )
