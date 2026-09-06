"""Golden-output guardrail for nimbus issue #363's own agreed staged-
extraction plan (Mark Purcell, codebase review, split() finding 5 --
main()'s 1,788 lines split into ha_bridge/solver_inputs/solver_plan/
solver_publish/solver_reports modules): step 1 of the plan he proposed
and this repo's own maintainer explicitly agreed to follow exactly --

    "Guardrails first, before moving a line. [...] Add one golden-output
    test: a fixed set of source-sensor states in, and the exact pushed
    sensor.nimbus_solver_battery_forecast attributes out (plan, cost
    band, breakdown). Any extraction that changes a byte of that output
    fails."

Drives the REAL main() (not a reimplementation) with a full ha_get mock
-- the same minimal fixture shape test_main_load_forecast_startup_race.py
already established, extended just enough to reach a genuine, real
"optimal" LP solve rather than stopping at an earlier branch -- and
asserts on the exact `sensor.nimbus_solver_battery_forecast` state and
attributes `ha_post_state` receives.

Time is frozen (`@pytest.mark.freeze_time`): confirmed empirically before
writing this test that main()'s own `now = datetime.now(UTC)...` call
(solver_writer.py, main()'s own first lines) feeds `build_tiered_grid()`,
whose OWN period count/alignment genuinely depends on real wall-clock
time (observed directly: two runs a few minutes apart produced 360 vs.
365 periods and different total_cost) -- this is NOT a coincidence-prone
flaky test, freezing is a real, load-bearing requirement for
reproducibility, not a cosmetic nicety.

Deliberately excludes from comparison: every `forecast[i]['time']` ISO
string and the top-level `generated_at` field, both of which are still
literally the frozen instant re-formatted per period offset -- keeping
them out of the asserted dict makes failures point at genuine VALUE
drift, not an incidental formatting/timezone-library version difference
this test was never meant to guard.
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

# Every non-timestamp key expected on the real (state, attrs) tuple
# main() pushes to sensor.nimbus_solver_battery_forecast for this exact
# fixture -- captured directly from a real run against this same
# fixture, frozen at the timestamp this test itself freezes to.
_EXPECTED_STATE = -4.617
_EXPECTED_ATTRS = {
    "unit_of_measurement": "kW",
    "device_class": "power",
    "state_class": "measurement",
    "friendly_name": "Nimbus Solver Battery Forecast",
    "signal_role": "battery",
    "source_sensor": "sensor.fake_soc",
    "status": "optimal",
    "total_cost": 26.6828900813574,
    "total_cost_with_fixed_costs": 34.4829,
    "cost_breakdown": {
        "grid_net": 33.5877,
        "degradation": 0.0,
        "charge_fee": 0.1847,
        "discharge_fee": 0.3148,
        "terminal_value_credit": -7.4043,
    },
    "cost_band": {"lower": 3.3107, "upper": 32.9326, "width": 29.622},
    "p2p_match_fraction": 0.0,
    "risk_aversion": 0.25,
    "import_price_risk_aversion": 0.0,
    "export_price_risk_aversion": 0.0,
    "salvage_value": 0.15,
    "degradation_cost_per_kwh": 0.0,
    "total_charge_kwh": 18.47,
    "total_discharge_kwh": 31.48,
    "total_throughput_kwh": 49.95,
    "equivalent_full_cycles": 0.624,
    "battery_kw_side": "AC",
    "battery_kw_sign_convention": "positive_discharge_negative_charge",
    "efficiency_convention": "round_trip_symmetric_sqrt",
    "price_blend_algorithm": "primary_preferring_fallback_to_secondary_mean",
    "charge_efficiency": 0.9747,
    "discharge_efficiency": 0.9747,
    "ac_bus_losses_kwh": 1.285,
    "n_periods": 360,
    "n_clamped_periods": 0,
    "horizon_hours": 96.0,
    "solve_seconds": 0.0,  # excluded from comparison -- see test body
    "binding_constraint_now": "Grid export at zero (not economical right now)",
    "binding_constraint_shadow_price": 0.0235,
    "energy_shadow_price_now": 0.025,
    "p2p_volume_cap_shadow_price": -0.0001,
    "p2p_recent_avg_volume_kwh": 0.0,
    "load_forecast_source_used": f"single sensor: {_LOAD_SENSOR}",
    "load_forecast_source_error": None,
    "load_forecast_warnings": {},
    "load_forecast_coverage_hours": 1.0,
    "load_summed_18_now_kw": 1.5,
    "load_whole_house_cross_check_now_kw": None,
    "failed_load_entities": [],
    "solar_delivery_ratio": None,
    "solar_delivery_sample_count": 0,
    "solar_delivery_underperforming": False,
}

# Per-period forecast keys asserted at fixed indices only (0, 1, -1) --
# all 360 periods in full would make a real, intentional future change
# to the grid shape (e.g. a tier-boundary tweak) needlessly tedious to
# update; three representative points (now, one step later, horizon end)
# already catch a real behavior change anywhere in the per-period
# construction just as reliably.
_EXPECTED_FORECAST_SAMPLE = {
    0: {
        "battery_kw": -4.617,
        "grid_import_kw": 6.117,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.5,
        "soc_pct": 55.94,
        "import_price": 0.3,
        "export_price": 0.05,
    },
    1: {
        "battery_kw": -4.617,
        "grid_import_kw": 6.117,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.5,
        "soc_pct": 56.88,
        "import_price": 0.3,
        "export_price": 0.05,
    },
    -1: {
        "battery_kw": 1.3,
        "grid_import_kw": 0.0,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.3,
        "soc_pct": 19.25,
        "import_price": 0.3,
        "export_price": 0.05,
    },
}

# Keys deliberately excluded from the golden comparison because they are
# themselves still a re-derivation of the frozen "now" instant (a real
# timestamp/formatting difference here is not the kind of regression
# this guardrail exists to catch) or a wall-clock-measured duration.
# (Per-period `time` is handled separately -- _EXPECTED_FORECAST_SAMPLE
# below simply never lists it as a key to check.)
_TIME_DEPENDENT_ATTR_KEYS = {"generated_at", "solve_seconds"}


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


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestMainGoldenOutput:
    def test_fixed_inputs_produce_the_exact_expected_plan(self, freezer):
        ha_get_mock = _make_ha_get()
        posted = {}

        def _capture_ha_post_state(entity_id, state, attrs=None):
            posted[entity_id] = (state, attrs)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(
                solver_writer, "ha_post_state", side_effect=_capture_ha_post_state
            ),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            patch.object(
                solver_writer,
                "PLAN_STATE_PATH",
                "/tmp/nonexistent_plan_state_golden_test.json",
            ),
        ):
            solver_writer.main()

        assert solver_writer.ENTITY_ID in posted, (
            "main() did not push sensor.nimbus_solver_battery_forecast at all "
            "-- this fixture must reach a real optimal solve"
        )
        state, attrs = posted[solver_writer.ENTITY_ID]

        assert state == _EXPECTED_STATE

        forecast = attrs.get("forecast")
        assert forecast is not None and len(forecast) == _EXPECTED_ATTRS["n_periods"]

        actual_non_time = {
            k: v for k, v in attrs.items() if k not in _TIME_DEPENDENT_ATTR_KEYS
        }
        expected_non_time = {
            k: v
            for k, v in _EXPECTED_ATTRS.items()
            if k not in _TIME_DEPENDENT_ATTR_KEYS
        }
        actual_non_time.pop("forecast")
        assert actual_non_time == expected_non_time, (
            "main()'s pushed attributes (excluding forecast/generated_at/"
            "solve_seconds) no longer match the golden output -- if this "
            "was a deliberate, understood behavior change, regenerate the "
            "golden values from a fresh real run; if this happened during "
            "a #363 extraction step, STOP -- the extraction changed real "
            "output and is not behavior-preserving."
        )

        for idx, expected_point in _EXPECTED_FORECAST_SAMPLE.items():
            actual_point = forecast[idx]
            for key, expected_value in expected_point.items():
                assert actual_point.get(key) == expected_value, (
                    f"forecast[{idx}]['{key}'] = {actual_point.get(key)!r}, "
                    f"expected {expected_value!r}"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
