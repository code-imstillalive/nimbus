"""The household load total's state is the same number as its own first
forecast period, after a live whole-house reading has replaced that period,
and it says which sensor it came from (#100, #187) (#1837).
"""

from __future__ import annotations

import contextlib
import copy

import pytest
import solver_shared
import solver_writer as sw
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths

LIVE = "sensor.real_whole_house_power"
TOTAL = "sensor.nimbus_household_load_total_forecast"


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_the_state_is_the_anchored_first_period_not_the_snapshot(freezer):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    attrs["solver_whole_house_cross_check_sensor"] = LIVE
    base = guardrail._make_ha_get()

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        if entity_id == LIVE:
            return {"state": "7.25", "attributes": {"unit_of_measurement": "kW"}}
        return base(entity_id)

    posted = {}
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(sw, "ha_get", ha_get)
        mp.setattr(solver_shared, "ha_get", ha_get)
        mp.setattr(sw, "entity_exists", lambda e: e == LIVE or base(e) is not None)
        mp.setattr(
            sw, "ha_post_state", lambda e, s, a=None: posted.__setitem__(e, (s, a))
        )
        stack.enter_context(isolated_state_paths(sw))
        sw.main()
    state, published = posted[TOTAL]
    assert published["forecast"][0]["value"] == pytest.approx(7.25)
    assert state == pytest.approx(published["forecast"][0]["value"])
    assert published["signal_role"] == "other"
    assert published["source_sensor"] == attrs["solver_load_forecast_sensor"]
    # The cross-check diagnostic keeps the forecast as summed, before the live
    # reading: it compares two forecasts, not a forecast with reality.
    plan = posted["sensor.nimbus_solver_battery_forecast"][1]
    diag = plan.get(
        "load_summed_18_now_kw",
        plan.get("solve_diagnostics", {}).get("load_summed_18_now_kw"),
    )
    assert diag is not None and diag != pytest.approx(7.25)
