"""main() takes the flex telemetry record's house load from the helper that
knows a load forecast's fallback zero is no measurement (#1665) (#1837).
"""

from __future__ import annotations

import contextlib
import copy

import pytest
import solver_publish
import solver_shared
import solver_writer as sw
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_hands_the_record_the_helpers_answer(freezer):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    base = guardrail._make_ha_get()

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        return base(entity_id)

    seen = {}
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(sw, "ha_get", ha_get)
        mp.setattr(solver_shared, "ha_get", ha_get)
        mp.setattr(sw, "ha_post_state", lambda *a, **k: None)
        mp.setattr(sw, "_measured_house_load_kw", lambda *a: 12.34)
        mp.setattr(
            solver_publish,
            "publish_flex_telemetry_record",
            lambda *a, **k: seen.update(k),
        )
        stack.enter_context(isolated_state_paths(sw))
        sw.main()
    assert seen["house_load_kw"] == 12.34
