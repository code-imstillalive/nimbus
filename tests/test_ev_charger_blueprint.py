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
CAR2 = "sensor.nimbus_solver_battery_2_kw"
PLUG1 = "binary_sensor.car1_plugged"
PLUG2 = "binary_sensor.car2_plugged"
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
    helper_age_s: float = 0,
    extra: dict[str, str] | None = None,
    **inputs: Any,
) -> tuple[list[str], dict[str, Any]]:
    bp = yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=_Loader)
    values = {
        **_input_defaults(bp),
        "car_power_sensors": [CAR],
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
    for entity, value in (extra or {}).items():
        states[entity] = _State(value, {}, NOW)
    if helper is not None:
        states[STATE] = _State(helper, {}, NOW - timedelta(seconds=helper_age_s))
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
    # Below the minimum current: never ask for less than it can do.
    ran, ctx = run("tick", car="-1.0")
    assert ctx["want"] == "STOPPED" and ran == []
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


def test_below_the_minimum_it_holds_the_minimum_then_stops():
    """Mark Purcell's review of #2583: a plan moving either side of the
    charger's minimum must not start and stop the charger every cycle."""
    # Charging, the plan drops below 6 A: hold at 6 A, no second start.
    ran, ctx = run("tick", car="-1.0", helper="CHARGING")
    assert ctx["want"] == "HOLDING" and ctx["amps"] == 6
    assert ran == ["set_current_actions", "input_text.set_value=HOLDING"]
    # Still below after 4 minutes: still holding, nothing recorded again.
    ran, ctx = run("tick", car="-1.0", helper="HOLDING", helper_age_s=240)
    assert ctx["want"] == "HOLDING" and ran == ["set_current_actions"]
    # After the 5-minute hold: stop.
    ran, ctx = run("tick", car="-1.0", helper="HOLDING", helper_age_s=301)
    assert ctx["want"] == "STOPPED" and ran[0] == "stop_actions"
    # Back above the minimum while holding: charge, no second start.
    ran, ctx = run("tick", car="-3.0", helper="HOLDING", helper_age_s=120)
    assert ctx["want"] == "CHARGING" and ran[0] == "set_current_actions"
    assert "start_actions" not in ran
    # A plan of no charge, or a discharge, stops at once.
    for car in ("0.0", "2.0"):
        ran, ctx = run("tick", car=car, helper="CHARGING")
        assert ctx["want"] == "STOPPED" and ran[0] == "stop_actions", car
    # Hold set to 0, or no state helper to time it: stop at once.
    _, ctx = run("tick", car="-1.0", helper="CHARGING", below_min_hold_minutes=0)
    assert ctx["want"] == "STOPPED"
    _, ctx = run("tick", car="-1.0", helper=None)
    assert ctx["want"] == "STOPPED"


def test_a_shared_charger_follows_the_car_plugged_in_and_never_two():
    """Mark Purcell's review of #2583: two cars taking turns on one charger
    are one automation for the charger, following the connected car."""
    shared = {
        "car_power_sensors": [CAR, CAR2],
        "connected_sensors": [PLUG1, PLUG2],
    }
    plans = {CAR2: "-11.0"}  # car 1 plans 7.36 kW (car=), car 2 11 kW
    # Car 2 plugged in: the charger follows car 2's plan.
    _, ctx = run(
        "tick", car="-7.36", extra={**plans, PLUG1: "off", PLUG2: "on"}, **shared
    )
    assert ctx["car_entity"] == CAR2 and ctx["want"] == "CHARGING"
    assert ctx["amps"] == 32  # 11 kW single phase, capped at the maximum
    # Car 1 plugged in.
    _, ctx = run(
        "tick", car="-3.0", extra={**plans, PLUG1: "on", PLUG2: "off"}, **shared
    )
    assert ctx["car_entity"] == CAR and ctx["amps"] == 13
    # Neither plugged in: stopped, not the fallback.
    ran, ctx = run(
        "tick",
        car="-7.0",
        helper="CHARGING",
        extra={**plans, PLUG1: "off", PLUG2: "off"},
        **shared,
    )
    assert ctx["want"] == "STOPPED" and ran[0] == "stop_actions"
    # Both read as plugged in, sensors that do not pair up with the cars, or
    # several cars and no plugged-in sensors: it cannot tell, so the fallback.
    for case in (
        {"extra": {**plans, PLUG1: "on", PLUG2: "on"}, **shared},
        {
            "extra": {**plans, PLUG1: "on"},
            "car_power_sensors": [CAR, CAR2],
            "connected_sensors": [PLUG1],
        },
        {"extra": plans, "car_power_sensors": [CAR, CAR2]},
    ):
        ran, ctx = run("tick", car="-7.0", helper="CHARGING", **case)
        assert ctx["want"] == "FALLBACK", case
        assert ran[0] == "fallback_actions", case
    # One car with its plugged-in sensor off: stopped.
    _, ctx = run(
        "tick",
        car="-7.0",
        extra={PLUG1: "off"},
        connected_sensors=[PLUG1],
    )
    assert ctx["want"] == "STOPPED"
