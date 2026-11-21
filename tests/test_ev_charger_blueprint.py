"""The EV charger dispatch blueprint's safety properties (household, 21 Nov
2026: a car's charger follows Nimbus's plan for that car).

Uses the battery blueprint test's harness: the blueprint's templates rendered
through Jinja with Home Assistant's helpers stubbed, its structure walked, and
the inputs whose actions would run recorded.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from tests.test_battery_dispatch_blueprint import (
    NOW,
    _input_defaults,
    _Loader,
    _Run,
    _State,
)

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "automation"
    / "nimbus"
    / "ev_charger_dispatch.yaml"
)
CAR = "sensor.nimbus_solver_battery_1_kw"
PLAN = "sensor.nimbus_solver_battery_forecast"
ARM = "input_boolean.ev_armed"
STATE = "input_text.ev_state"


def run(
    trigger: str,
    *,
    car: str = "0.0",
    status: str = "optimal",
    plan_age_s: float = 30,
    armed: bool = True,
    helper: str | None = "STOPPED",
    **inputs: Any,
) -> tuple[list[str], dict[str, Any]]:
    bp = yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=_Loader)
    values = {
        **_input_defaults(bp),
        "car_power_sensor": CAR,
        "plan_sensor": PLAN,
        "arm_switch": ARM,
        "state_helper": STATE if helper is not None else "",
        **inputs,
    }
    gen = (NOW - timedelta(seconds=plan_age_s)).isoformat()
    states = {
        CAR: _State(car, {}, NOW),
        PLAN: _State("0", {"status": status, "generated_at": gen}, NOW),
        ARM: _State("on" if armed else "off", {}, NOW),
    }
    if helper is not None:
        states[STATE] = _State(helper, {}, NOW)
    r = _Run(states, values, NOW)
    r.ctx["trigger"] = {"id": trigger}
    for name, value in bp["variables"].items():
        r.ctx[name] = r.render(value)
    if r.holds(bp["conditions"]):
        r.walk(bp["actions"])
    return r.ran, r.ctx


def test_the_charger_follows_the_cars_plan_and_stops_safely():
    # A 7.36 kW planned charge on 230 V single phase: start, then 32 A.
    ran, ctx = run("plan", car="-7.36")
    assert ctx["want"] == "CHARGING" and ctx["amps"] == 32
    assert ran[:2] == ["start_actions", "set_current_actions"]
    assert ran[-1] == "input_text.set_value=CHARGING"
    # Already charging: only the current is set, no second start.
    ran, ctx = run("tick", car="-3.0", helper="CHARGING")
    assert ran == ["set_current_actions"] and ctx["amps"] == 13
    # Three phase: 11 kW is 16 A.
    _, ctx = run("tick", car="-11.0", phases="3")
    assert ctx["amps"] == 16
    # Never above the charger's maximum.
    _, ctx = run("tick", car="-20.0")
    assert ctx["amps"] == 32
    # Below the minimum current: stop, never ask for less than it can do.
    ran, ctx = run("tick", car="-1.0", helper="CHARGING")
    assert ctx["want"] == "STOPPED" and ran[0] == "stop_actions"
    # A planned discharge (V2G) is not a charge: stop.
    _, ctx = run("tick", car="2.0")
    assert ctx["want"] == "STOPPED"
    # A failed or stale plan, or an unreadable car figure: the fallback.
    for kwargs in (
        {"status": "infeasible", "car": "-7.0"},
        {"plan_age_s": 11 * 60, "car": "-7.0"},
        {"car": "unavailable"},
    ):
        ran, ctx = run("tick", helper="CHARGING", **kwargs)
        assert ctx["want"] == "FALLBACK", kwargs
        assert ran[0] == "fallback_actions", kwargs
    # Disarmed: nothing runs but the fallback, on the disarm itself.
    ran, _ = run("tick", car="-7.0", armed=False)
    assert ran == []
    ran, _ = run("disarmed", car="-7.0", armed=False, helper="CHARGING")
    assert ran[0] == "fallback_actions"
