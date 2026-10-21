"""The battery dispatch blueprint's safety properties, checked in CI (#1955
review, Mark Purcell, 21 Oct 2026: "A future edit to want/dwell_exempt/
mode_changed could silently regress any of the properties above with
nothing in CI to catch it").

The blueprint's own variables, conditions and action branches are run here:
its templates rendered through Jinja with Home Assistant's helpers stubbed,
its `choose`/`if` structure walked, and the inputs whose actions would run
recorded. Each test is one property the going-live guide promises.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
import yaml

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "automation"
    / "nimbus"
    / "battery_dispatch.yaml"
)
TZ = ZoneInfo("Australia/Brisbane")
NOW = datetime(2026, 10, 21, 9, 13, 30, tzinfo=TZ)  # minute 13: not a resend
PLAN = "sensor.nimbus_solver_battery_forecast"
ARM = "input_boolean.armed"
MODE = "input_text.mode"


class _Input(str):
    """`!input name`."""


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: _Input(node.value))


def _blueprint() -> dict[str, Any]:
    return yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=_Loader)


def _input_defaults(bp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section in bp["blueprint"]["input"].values():
        for name, spec in section["input"].items():
            if "action" in spec.get("selector", {}):
                out[name] = [name]  # a non-empty action list, recorded by name
            elif "default" in spec:
                out[name] = spec["default"]
    return out


class _State:
    def __init__(self, state: str, attrs: dict, changed: datetime) -> None:
        self.state = state
        self.attributes = attrs
        self.last_changed = changed
        self.last_updated = changed


def _native(text: str) -> Any:
    """What Home Assistant makes of a rendered template."""
    t = text.strip()
    if t in ("True", "False"):
        return t == "True"
    for cast in (int, float):
        try:
            return cast(t)
        except ValueError:
            pass
    return t


class _Run:
    """One trigger of the automation against a set of entity states."""

    def __init__(self, states: dict[str, _State], inputs: dict[str, Any], now):
        self.states = states
        self.inputs = inputs
        self.ran: list[str] = []
        env = jinja2.Environment()
        env.globals.update(
            states=self._states_proxy(),
            state_attr=lambda e, a: (
                self.states[e].attributes.get(a) if e in self.states else None
            ),
            is_number=lambda v: _is_number(v),
            as_datetime=lambda v: datetime.fromisoformat(v),
            now=lambda: now,
        )
        self.env = env
        self.ctx: dict[str, Any] = {}

    def _states_proxy(self):
        states = self.states

        class Proxy:
            def __call__(self, e):
                return states[e].state if e in states else "unknown"

            def __getitem__(self, e):
                return states.get(e)

        return Proxy()

    def render(self, value: Any) -> Any:
        if isinstance(value, _Input):
            return self.inputs[str(value)]
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            return _native(self.env.from_string(value).render(**self.ctx))
        return value

    def holds(self, conditions: list[dict]) -> bool:
        return all(self.condition(c) for c in conditions)

    def condition(self, c: dict) -> bool:
        kind = c["condition"]
        if kind == "template":
            return self.render(c["value_template"]) is True
        if kind == "or":
            return any(self.condition(x) for x in c["conditions"])
        if kind == "trigger":
            return self.ctx["trigger"]["id"] == c["id"]
        if kind == "state":
            entity = self.render(c["entity_id"])
            return entity in self.states and self.states[entity].state == c["state"]
        raise AssertionError(f"condition {kind} not modelled")

    def walk(self, node: Any) -> None:
        if isinstance(node, _Input):
            if self.inputs[str(node)]:
                self.ran.append(str(node))
            return
        if isinstance(node, list):
            for step in node:
                self.walk(step)
            return
        if "choose" in node:
            for option in node["choose"]:
                if self.holds(option["conditions"]):
                    self.walk(option["sequence"])
                    return
            self.walk(node.get("default", []))
        elif "if" in node:
            if self.holds(node["if"]):
                self.walk(node["then"])
            elif "else" in node:
                self.walk(node["else"])
        elif "sequence" in node:
            self.walk(node["sequence"])
        elif "action" in node:
            value = self.render(node.get("data", {}).get("value"))
            self.ran.append(f"{node['action']}={value}")
        else:
            raise AssertionError(f"step {node} not modelled")


def _is_number(v: Any) -> bool:
    try:
        float(v)
    except (TypeError, ValueError):
        return False
    return v not in ("nan", "inf")


def run(
    trigger: str,
    *,
    plan: str = "0.0",
    status: str = "optimal",
    plan_age_s: float = 30,
    armed: bool = True,
    helper: str | None = "SC",
    helper_age_s: float = 3600,
    now: datetime = NOW,
    attrs: dict | None = None,
    **inputs: Any,
) -> tuple[list[str], dict[str, Any]]:
    """Fire `trigger`; return the actions that ran and the variables."""
    bp = _blueprint()
    values = {
        **_input_defaults(bp),
        "plan_sensor": PLAN,
        "arm_switch": ARM,
        "mode_helper": MODE if helper is not None else "",
        "max_power_kw": 5.0,
        **inputs,
    }
    gen = (now - timedelta(seconds=plan_age_s)).isoformat()
    states = {
        PLAN: _State(
            plan, {"status": status, "generated_at": gen, **(attrs or {})}, now
        ),
        ARM: _State("on" if armed else "off", {}, now),
    }
    if helper is not None:
        states[MODE] = _State(helper, {}, now - timedelta(seconds=helper_age_s))
    r = _Run(states, values, now)
    r.ctx["trigger"] = {"id": trigger}
    for name, value in bp["variables"].items():
        r.ctx[name] = r.render(value)
    if r.holds(bp["conditions"]):
        r.walk(bp["actions"])
    return r.ran, r.ctx


FALLBACK = ["fallback_actions", "input_text.set_value=FALLBACK"]


def test_disarming_runs_the_fallback_whatever_the_plan_says():
    ran, _ = run("disarmed", plan="-4.0", armed=False, helper="CHG")
    assert ran == FALLBACK


def test_nothing_runs_while_disarmed():
    for trigger in ("tick", "plan", "start"):
        ran, _ = run(trigger, plan="-4.0", armed=False)
        assert ran == [], trigger


def test_a_plan_that_cannot_be_followed_runs_the_fallback_not_a_power_command():
    for kwargs in (
        {"plan": "unknown"},
        {"plan": "3.0", "status": "infeasible"},
        {"plan": "3.0", "plan_age_s": 11 * 60},  # default stale: 10 min
    ):
        ran, ctx = run("plan", **kwargs)
        assert ran == FALLBACK, kwargs
        assert ctx["power_kw"] == 0


def test_power_is_clamped_to_the_maximum_and_always_positive():
    ran, ctx = run("plan", plan="-12.0")
    assert ctx["power_kw"] == 5.0
    assert ran == [
        "charge_actions",
        "set_power_actions",
        "keepalive_actions",
        "input_text.set_value=CHG",
    ]


def test_an_unchanged_mode_sends_only_the_setpoint():
    ran, _ = run("tick", plan="3.0", helper="DIS")
    assert ran == ["set_power_actions", "keepalive_actions"]


def test_the_fallback_is_resent_after_a_restart_when_the_helper_already_says_so():
    """Mark Purcell, #1955: the helper survives a restart, the inverter's
    state may not. Before the fix this ran nothing."""
    ran, _ = run("start", plan="unknown", helper="FALLBACK")
    assert ran == FALLBACK


def test_every_mode_command_is_resent_at_start():
    ran, _ = run("start", plan="0.0", helper="SC")
    assert ran == ["self_consume_actions"]
    ran, _ = run("start", plan="3.0", helper="DIS")
    assert ran[0] == "discharge_actions"


def test_the_mode_command_is_resent_every_n_minutes_on_the_tick():
    on_the_ten = NOW.replace(minute=20, second=0)
    ran, _ = run("tick", plan="unknown", helper="FALLBACK", now=on_the_ten)
    assert ran == FALLBACK
    ran, _ = run("tick", plan="unknown", helper="FALLBACK")  # minute 13
    assert ran == []
    ran, _ = run("tick", plan="0.0", helper="SC", now=on_the_ten)
    assert ran == ["self_consume_actions"]


def test_resend_off_still_resends_at_start():
    on_the_ten = NOW.replace(minute=20, second=0)
    ran, _ = run(
        "tick", plan="unknown", helper="FALLBACK", now=on_the_ten, resend_minutes=0
    )
    assert ran == []
    ran, _ = run("start", plan="unknown", helper="FALLBACK", resend_minutes=0)
    assert ran == FALLBACK


def test_the_dwell_holds_a_small_switch_but_never_a_large_reversal():
    # Charging for a minute; the plan now discharges 1 kW: held in Charge.
    ran, ctx = run("plan", plan="1.0", helper="CHG", helper_age_s=60)
    assert ctx["target_mode"] == "CHG"
    assert "discharge_actions" not in ran
    # The same at 6 kW (reversal default 5 kW): switches at once.
    ran, ctx = run("plan", plan="6.0", helper="CHG", helper_age_s=60)
    assert ctx["target_mode"] == "DIS"
    assert ran[0] == "discharge_actions"
    # Self-Consume to Discharge at 6 kW is not a reversal: held.
    ran, ctx = run("plan", plan="6.0", helper="SC", helper_age_s=60)
    assert ctx["target_mode"] == "SC"


def test_a_price_spike_skips_the_dwell():
    ran, ctx = run(
        "plan",
        plan="1.0",
        helper="CHG",
        helper_age_s=60,
        attrs={"price_spike_active": True},
    )
    assert ctx["target_mode"] == "DIS"
    assert ran[0] == "discharge_actions"


def test_absorb_surplus_uses_self_consume():
    ran, _ = run("plan", plan="-2.0", helper="CHG", attrs={"absorb_surplus": True})
    assert ran == ["self_consume_actions", "input_text.set_value=SC"]


def test_overlapping_triggers_queue_and_an_overflow_is_logged():
    bp = _blueprint()
    assert bp["mode"] == "queued"
    assert bp["max_exceeded"] == "warning"
