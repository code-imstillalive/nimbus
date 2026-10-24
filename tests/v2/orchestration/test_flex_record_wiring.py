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


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_a_deferred_ranging_reaches_both_flex_publishers_as_hold_last(freezer):
    """#489: when this cycle's ranging was deferred, both the flex signals
    and the telemetry record hold their last values."""
    import dataclasses

    import solver_plan

    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    base = guardrail._make_ha_get()

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        return base(entity_id)

    real = solver_plan.assemble_and_solve_plan
    held = {}
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(sw, "ha_get", ha_get)
        mp.setattr(solver_shared, "ha_get", ha_get)
        mp.setattr(sw, "ha_post_state", lambda *a, **k: None)
        mp.setattr(
            solver_plan,
            "assemble_and_solve_plan",
            lambda *a, **k: dataclasses.replace(
                real(*a, **k), flex_ranging_deferred=True
            ),
        )
        mp.setattr(
            sw,
            "publish_flex_signals",
            lambda plan, *, hold_last=False: held.__setitem__("signals", hold_last),
        )
        mp.setattr(
            solver_publish,
            "publish_flex_telemetry_record",
            lambda *a, **k: held.__setitem__("record", k.get("hold_last")),
        )
        stack.enter_context(isolated_state_paths(sw))
        sw.main()
    assert held == {"signals": True, "record": True}


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_an_armed_price_event_moves_import_and_export_alike_every_period(freezer):
    """#1213: the armed delta is added to both prices, last, after the blend
    and the settled block, so period 0 moves too."""
    from datetime import UTC, datetime, timedelta

    def run(armed):
        attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
        if armed:
            attrs["solver_price_event_enabled"] = True
            attrs["solver_price_event_sensor"] = "sensor.price_event"
        base = guardrail._make_ha_get()
        start = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
        event = {
            "state": "20.0",
            "attributes": {
                "forecast": [
                    {"time": start.isoformat(), "value": 20.0},
                    {"time": (start + timedelta(days=5)).isoformat(), "value": 0.0},
                ]
            },
        }

        def ha_get(entity_id):
            if entity_id == "sensor.nimbus_solver_config":
                return {"state": "configured", "attributes": attrs}
            return event if entity_id == "sensor.price_event" else base(entity_id)

        posted = {}
        with contextlib.ExitStack() as stack:
            mp = stack.enter_context(pytest.MonkeyPatch.context())
            mp.setattr(sw, "ha_get", ha_get)
            mp.setattr(solver_shared, "ha_get", ha_get)
            mp.setattr(
                sw, "ha_post_state", lambda e, s, a=None: posted.__setitem__(e, a)
            )
            stack.enter_context(isolated_state_paths(sw))
            sw.main()
        return posted["sensor.nimbus_solver_battery_forecast"]["forecast"]

    plain, armed = run(False), run(True)
    for p, a in zip(plain, armed, strict=True):
        assert a["import_price"] == pytest.approx(p["import_price"] + 20.0)
        assert a["export_price"] == pytest.approx(p["export_price"] + 20.0)
