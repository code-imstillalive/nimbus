"""Part A synthetic scenarios.

Each one is built to reach a named path in ``main()``; the golden test
asserts that it does (non-vacuity), so a scenario that silently stops
reaching its path fails rather than pinning an early return.
"""

from __future__ import annotations

import copy

from golden.fake_ha import FakeHA
from golden.scenarios import Scenario, register

LOAD = "sensor.nimbus_sigen_plant_total_load_power_forecast"

BASE_CONFIG = {
    "solver_battery_soc_sensor": "sensor.fake_soc",
    "solver_battery_capacity_kwh": 40.0,
    "solver_max_charge_kw": 5.0,
    "solver_max_discharge_kw": 5.0,
    "solver_grid_max_import_kw": 15.0,
    "solver_grid_max_export_kw": 15.0,
    "solver_import_price_sensor": "sensor.fake_import_price",
    "solver_export_price_sensor": "sensor.fake_export_price",
    "solver_solar_forecast_sensor": "",
    "solver_load_forecast_sensor": LOAD,
    "solver_load_forecast_entities": [],
    # The snapshots were taken with the old 1.95 fallback; stated here so a
    # change to the default does not move reporting-only golden output.
    "solver_fixed_daily_charge": 1.95,
}


def base_states(config: dict | None = None) -> dict:
    return {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": copy.deepcopy(config or BASE_CONFIG),
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        LOAD: {
            "state": "1.5",
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {"time": "2026-09-06T20:30:00+10:00", "value": 1.5},
                    {"time": "2026-09-06T20:45:00+10:00", "value": 1.4},
                    {"time": "2026-09-06T21:00:00+10:00", "value": 1.3},
                ],
            },
        },
    }


# The fixture of tests/test_main_golden_output_guardrail.py, unchanged, so
# the new golden master starts from the one output Nimbus already pins.
register(
    Scenario(
        name="guardrail_baseline",
        instant="2026-09-06T10:00:00+00:00",
        build=lambda: FakeHA(states=base_states()),
        purpose="the existing #363 guardrail fixture, now snapshotted whole",
    )
)


# Two cycles five minutes apart over one workdir: the second cycle reads
# the plan_state.json the first one wrote, so the proximal (plan-stability)
# term and the plan-state reload are inside the golden master.
register(
    Scenario(
        name="guardrail_two_cycles",
        instant="2026-09-06T10:00:00+00:00",
        build=lambda: FakeHA(states=base_states()),
        purpose="plan_state.json written by cycle 1 and read back by cycle 2",
        cycles=2,
    )
)
