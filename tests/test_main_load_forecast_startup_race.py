"""End-to-end regression test for nimbus issue #370 (Mark Purcell,
codebase review): main() must not publish a confidently-wrong zero-load
"optimal" plan when the load-forecast sensor is a transient startup-race
away (unavailable, no attributes) rather than genuinely misconfigured.

Drives the REAL main() (not a reimplementation) with a full ha_get mock
covering every entity it touches on the way to the load-forecast branch,
matching the review's own suggested test shape: "drive main() with
ha_get returning an unavailable load-forecast entity and assert that
nothing is pushed via ha_post_state (or that a not-ready exception
propagates)."
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer

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


def _make_ha_get(load_sensor_state: dict):
    """A single ha_get stand-in covering every entity main() touches
    before reaching the load-forecast branch, plus the load-forecast
    entity itself (parameterised so callers can make it look healthy or
    startup-race-unavailable). Anything NOT explicitly listed raises a
    404 HTTPError -- entity_exists() (used to gate every genuinely
    optional integration: Open-Meteo, Solcast, LocalVolts/AEMO extras)
    already treats a 404 as "not configured, skip", so this safely
    no-ops every optional path rather than crashing on it.
    """
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: load_sensor_state,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


class TestStartupRaceLoadForecastNeverPublishesAZeroLoadPlan:
    def test_unavailable_load_sensor_raises_instead_of_publishing(self):
        # The exact live repro: entity restored into the state machine
        # as unavailable, attributes wiped entirely -- no 'forecast'
        # key, no other list-valued attribute either.
        unavailable_state = {"state": "unavailable", "attributes": {}}
        ha_get_mock = _make_ha_get(unavailable_state)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "ha_post_state") as mock_post_state,
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
        ):
            with pytest.raises(RuntimeError, match="not ready"):
                solver_writer.main()

            # The real regression this test guards against: NOTHING gets
            # published this cycle -- no confidently-wrong zero-load
            # "optimal" plan reaches the state machine at all.
            mock_post_state.assert_not_called()

    def test_a_genuinely_healthy_load_sensor_does_not_raise_here(self):
        # Sanity counterpart -- a real, populated forecast must NOT hit
        # the new raise path at all (this test only needs to get PAST
        # the load-forecast branch without the new RuntimeError; a full
        # successful solve needs a real LP feasibility setup well beyond
        # this fix's own scope, so this only asserts the raise didn't
        # fire for the wrong reason).
        healthy_state = {
            "state": "1.5",
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {"time": "2026-09-04T20:30:00+10:00", "value": 1.5},
                    {"time": "2026-09-04T20:45:00+10:00", "value": 1.4},
                    {"time": "2026-09-04T21:00:00+10:00", "value": 1.3},
                ],
            },
        }
        ha_get_mock = _make_ha_get(healthy_state)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
        ):
            try:
                solver_writer.main()
            except RuntimeError as e:
                assert "not ready" not in str(e)
            except Exception as e:  # noqa: BLE001
                # Anything else (e.g. a downstream LP/config gap this
                # minimal fixture doesn't fully cover) is out of scope
                # for this test -- it only asserts the NEW raise path
                # specifically didn't fire on healthy data.
                assert "not ready" not in str(e)
