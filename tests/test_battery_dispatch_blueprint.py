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
    extra: dict[str, str] | None = None,
    plan_changed_s: float = 3600,
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
            plan,
            {"status": status, "generated_at": gen, **(attrs or {})},
            now - timedelta(seconds=plan_changed_s),
        ),
        ARM: _State("on" if armed else "off", {}, now),
    }
    if helper is not None:
        states[MODE] = _State(helper, {}, now - timedelta(seconds=helper_age_s))
    for entity, state in (extra or {}).items():
        states[entity] = _State(state, {}, now)
    r = _Run(states, values, now)
    r.ctx["trigger"] = {"id": trigger}
    for name, value in bp["variables"].items():
        r.ctx[name] = r.render(value)
    if r.holds(bp["conditions"]):
        r.walk(bp["actions"])
    return r.ran, r.ctx


FALLBACK = ["fallback_actions", "input_text.set_value=FALLBACK"]


def test_disarming_runs_the_fallback_whatever_the_plan_says():
    # test_disarming_runs_the_fallback_whatever_the_plan_says
    # test_disarming_runs_the_fallback_whatever_the_plan_says
    ran, _ = run("disarmed", plan="-4.0", armed=False, helper="CHG")
    assert ran == FALLBACK

    # test_nothing_runs_while_disarmed
    for trigger in ("tick", "plan", "start"):
        ran, _ = run(trigger, plan="-4.0", armed=False)
        assert ran == [], trigger

    # test_a_plan_that_cannot_be_followed_runs_the_fallback_not_a_power_command
    for kwargs in (
        {"plan": "unknown"},
        {"plan": "3.0", "status": "infeasible"},
        {"plan": "3.0", "plan_age_s": 11 * 60},  # default stale: 10 min
    ):
        ran, ctx = run("plan", **kwargs)
        assert ran == FALLBACK, kwargs
        assert ctx["power_kw"] == 0

    # test_power_is_clamped_to_the_maximum_and_always_positive
    ran, ctx = run("plan", plan="-12.0")
    assert ctx["power_kw"] == 5.0
    assert ran == [
        "charge_actions",
        "set_power_actions",
        "keepalive_actions",
        "input_text.set_value=CHG",
    ]

    # test_an_unchanged_mode_sends_only_the_setpoint
    ran, _ = run("tick", plan="3.0", helper="DIS")
    assert ran == ["set_power_actions", "keepalive_actions"]

    # test_the_fallback_is_resent_after_a_restart_when_the_helper_already_says_so: Mark Purcell, #1955: the helper survives a restart, the inverter's
    ran, _ = run("start", plan="unknown", helper="FALLBACK")
    assert ran == FALLBACK

    # test_every_mode_command_is_resent_at_start
    ran, _ = run("start", plan="0.0", helper="SC")
    assert ran == ["self_consume_actions"]
    ran, _ = run("start", plan="3.0", helper="DIS")
    assert ran[0] == "discharge_actions"

    # test_the_mode_command_is_resent_every_n_minutes_on_the_tick
    on_the_ten = NOW.replace(minute=20, second=0)
    ran, _ = run("tick", plan="unknown", helper="FALLBACK", now=on_the_ten)
    assert ran == FALLBACK
    ran, _ = run("tick", plan="unknown", helper="FALLBACK")  # minute 13
    assert ran == []
    ran, _ = run("tick", plan="0.0", helper="SC", now=on_the_ten)
    assert ran == ["self_consume_actions"]

    # test_resend_off_still_resends_at_start
    # test_resend_off_still_resends_at_start
    on_the_ten = NOW.replace(minute=20, second=0)
    ran, _ = run(
        "tick", plan="unknown", helper="FALLBACK", now=on_the_ten, resend_minutes=0
    )
    assert ran == []
    ran, _ = run("start", plan="unknown", helper="FALLBACK", resend_minutes=0)
    assert ran == FALLBACK

    # test_the_dwell_holds_a_small_switch_but_never_a_large_reversal
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

    # test_a_price_spike_skips_the_dwell
    ran, ctx = run(
        "plan",
        plan="1.0",
        helper="CHG",
        helper_age_s=60,
        attrs={"price_spike_active": True},
    )
    assert ctx["target_mode"] == "DIS"
    assert ran[0] == "discharge_actions"

    # test_absorb_surplus_uses_self_consume
    ran, _ = run("plan", plan="-2.0", helper="CHG", attrs={"absorb_surplus": True})
    assert ran == ["self_consume_actions", "input_text.set_value=SC"]

    # test_a_solar_only_charge_self_consumes_instead_of_forcing_it
    # nimbus #2066, 26 Oct: a forced 0.8 kW charge with 0.2 kW of sun was
    # bought from the grid. Self-Consume charges only from the sun there is.
    ran, ctx = run(
        "plan",
        plan="-0.8",
        helper="CHG",
        solar_charge_sensor=SOLAR,
        extra={SOLAR: "True"},
    )
    assert ctx["want"] == "SC"
    assert ran == ["self_consume_actions", "input_text.set_value=SC"]

    # test_a_grid_charge_is_still_forced
    ran, ctx = run(
        "plan",
        plan="-0.8",
        helper="SC",
        solar_charge_sensor=SOLAR,
        extra={SOLAR: "False"},
    )
    assert ctx["want"] == "CHG"
    assert ran[0] == "charge_actions"

    # test_without_the_solar_signal_every_charge_is_forced
    _, ctx = run("plan", plan="-0.8", helper="SC", extra={SOLAR: "True"})
    assert ctx["want"] == "CHG"

    # test_the_solar_signal_never_turns_a_discharge_into_self_consume
    _, ctx = run(
        "plan",
        plan="2.0",
        helper="SC",
        helper_age_s=3600,
        solar_charge_sensor=SOLAR,
        extra={SOLAR: "True"},
    )
    assert ctx["want"] == "DIS"

    # test_overlapping_triggers_queue_and_an_overflow_is_logged
    bp = _blueprint()
    assert bp["mode"] == "queued"
    assert bp["max_exceeded"] == "warning"

    # test_2257_a_charge_is_never_above_the_live_allowance: a 46 kW supply
    # tripped on 4 Nov 2026 with the battery grid-charging and an EV, hot water,
    # pool and a toaster on. The charge sent is capped by Nimbus's allowed charge.
    allowed = "sensor.nimbus_battery_allowed_charge_kw"
    _, ctx = run("tick", plan="-4.0", extra={allowed: "2.5"})
    assert ctx["want"] == "CHG" and ctx["power_kw"] == 2.5
    # No limit set (unavailable): the plan's charge, as before.
    _, ctx = run("tick", plan="-4.0", extra={allowed: "unavailable"})
    assert ctx["power_kw"] == 4.0
    # A discharge is not capped by the import allowance.
    _, ctx = run("tick", plan="3.0", extra={allowed: "0.0"})
    assert ctx["power_kw"] == 3.0


SOLAR = "sensor.nimbus_solver_current_dispatch_solar_charge"


def test_the_house_inverter_follows_the_home_battery_not_the_fleet():
    # #2573: a car participant charging 7 kW while the home battery holds.
    # The plan's state is the fleet (-7); home_battery_kw is the home's own (0).
    _, ctx = run("plan", plan="-7.0", attrs={"home_battery_kw": 0.0})
    assert ctx["plan_kw"] == 0.0
    assert ctx["want"] != "CHG"
    # The home battery's own discharge is followed even when the fleet nets
    # out at zero.
    _, ctx = run("plan", plan="0.0", attrs={"home_battery_kw": 3.0})
    assert ctx["want"] == "DIS"
    # A plan from before #2573 (no home_battery_kw): the state, as before.
    _, ctx = run("plan", plan="-4.0")
    assert ctx["want"] == "CHG"
    # A refused plan stays refused whatever home_battery_kw says.
    ran, _ = run(
        "plan", plan="-4.0", status="infeasible", attrs={"home_battery_kw": 2.0}
    )
    assert ran == FALLBACK


def test_a_manual_mode_overrides_the_plan_until_automatic():
    # Household, 21 Nov 2026: the Control Panel card's manual buttons set an
    # input_select; the blueprint obeys it. "Automatic" (or no select) is the
    # plan.
    sel = "input_select.dispatch_mode"
    for mode, want in (
        ("Charge", "CHG"),
        ("Discharge", "DIS"),
        ("Preserve", "HOLD"),
        ("Self-Consume", "SC"),
    ):
        _, ctx = run("tick", plan="2.0", mode_select=sel, extra={sel: mode})
        assert ctx["want"] == want, mode
    # A manual charge or discharge runs at the maximum battery power...
    _, ctx = run("tick", plan="0.3", mode_select=sel, extra={sel: "Discharge"})
    assert ctx["power_kw"] == 5.0
    # ...a manual charge never above the live allowance...
    _, ctx = run(
        "tick",
        plan="0.0",
        mode_select=sel,
        allowed_charge_sensor="sensor.allowed",
        extra={sel: "Charge", "sensor.allowed": "2.5"},
    )
    assert ctx["power_kw"] == 2.5
    # ...is never held back by the dwell, and works on a stale plan.
    _, ctx = run(
        "tick",
        plan="2.0",
        helper="DIS",
        helper_age_s=10,
        mode_select=sel,
        extra={sel: "Charge"},
    )
    assert ctx["target_mode"] == "CHG"
    _, ctx = run(
        "tick", plan="2.0", plan_age_s=3600, mode_select=sel, extra={sel: "Charge"}
    )
    assert ctx["want"] == "CHG"
    # Automatic, or no select: the plan, as before.
    _, ctx = run("tick", plan="2.0", mode_select=sel, extra={sel: "Automatic"})
    assert ctx["want"] == "DIS"
    _, ctx = run("tick", plan="2.0")
    assert ctx["want"] == "DIS"
    # Disarming still runs the fallback whatever the select says.
    ran, _ = run(
        "disarmed", plan="2.0", armed=False, mode_select=sel, extra={sel: "Charge"}
    )
    assert ran == FALLBACK


def test_a_nimbus_reload_does_not_trip_the_fallback():
    """A tester, 21 Nov 2026: saving a Nimbus setting reloads Nimbus, the
    plan sensor is unavailable for a few seconds, and a minute tick landing
    in that gap ran the fallback (the inverter's own mode) mid-export. A plan
    that has only just gone unavailable keeps the last command; once the gap
    outlasts the grace, the fallback runs as before."""
    for gone in ("unavailable", "unknown"):
        ran, _ = run("tick", plan=gone, helper="DIS", plan_changed_s=20)
        assert ran == [], gone
        ran, _ = run("plan", plan=gone, helper="DIS", plan_changed_s=20)
        assert ran == [], gone
        ran, _ = run("tick", plan=gone, helper="DIS", plan_changed_s=150)
        assert ran == FALLBACK, gone
    # Grace 0: the fallback at once, as before.
    ran, _ = run(
        "tick",
        plan="unavailable",
        helper="DIS",
        plan_changed_s=20,
        reload_grace_minutes=0,
    )
    assert ran == FALLBACK
    # Disarming never waits.
    ran, _ = run("disarmed", plan="unavailable", armed=False, plan_changed_s=20)
    assert ran[0] == "fallback_actions"


def _row(t: str, kw: float, price: float, **extra: Any) -> dict[str, Any]:
    return {
        "time": t,
        "battery_kw": kw,
        "import_price": price,
        "export_price": price - 0.05,
        "p2p_rate": 0.0,
        "dispatch_hold": False,
        "dispatch_solar_charge": False,
        **extra,
    }


def test_a_price_change_is_followed_on_the_minute_not_after_the_next_solve():
    """A tester, 22 Nov 2026: discharge planned for the 17:00 price rise
    started at 17:01:45, two days running. The plan solved at 16:59 already
    had a 17:00 row; the blueprint followed its 16:59 row until a new plan
    landed. At a price change the plan foresaw, the minute tick follows the
    plan's row for the current minute."""
    at = datetime(2026, 11, 22, 17, 0, 2, tzinfo=TZ)
    rows = [
        _row("2026-11-22T16:59:00+10:00", 0.0, 0.20),
        _row("2026-11-22T17:00:00+10:00", 4.0, 0.60),
        _row("2026-11-22T17:01:00+10:00", 4.0, 0.60),
    ]
    attrs = {"forecast": rows, "home_battery_kw": 0.0}
    _, ctx = run("tick", plan="0.0", now=at, plan_age_s=62, attrs=attrs)
    assert ctx["want"] == "DIS"
    assert ctx["power_kw"] == 4.0
    # The home battery's own row, when the plan carries participants (#2573).
    home = {
        "name": "home",
        "forecast": [
            {"time": r["time"], "charge_kw": 0.0, "discharge_kw": d}
            for r, d in zip(rows, (0.0, 3.0, 3.0))
        ],
    }
    _, ctx = run(
        "tick",
        plan="0.0",
        now=at,
        plan_age_s=62,
        attrs={**attrs, "batteries": [home]},
    )
    assert ctx["power_kw"] == 3.0
    # Before 17:00 the plan's period 0, as before.
    _, ctx = run(
        "tick",
        plan="0.0",
        now=at - timedelta(seconds=10),
        plan_age_s=52,
        attrs=attrs,
    )
    assert ctx["want"] == "SC"
    # No price change: period 0 still rules between solves, so its
    # hysteresis (#1654) is never bypassed.
    flat = [dict(r, import_price=0.20, export_price=0.15) for r in rows]
    _, ctx = run(
        "tick",
        plan="0.0",
        now=at,
        plan_age_s=62,
        attrs={"forecast": flat, "home_battery_kw": 0.0},
    )
    assert ctx["want"] == "SC"
    # The row's own hold and solar-charge flags, not period 0's.
    held = [rows[0], dict(rows[1], battery_kw=0.0, dispatch_hold=True)]
    _, ctx = run(
        "tick",
        plan="0.0",
        now=at,
        plan_age_s=62,
        hold_sensor="sensor.hold",
        attrs={"forecast": held},
        extra={"sensor.hold": "False"},
    )
    assert ctx["want"] == "HOLD"
    # A refused or stale plan is never rolled forward.
    _, ctx = run("tick", plan="0.0", now=at, status="infeasible", attrs=attrs)
    assert ctx["want"] == "FALLBACK"


def test_export_kw_follows_the_plans_grid_export_for_sell_power_inverters():
    """A tester, 23 Nov 2026: an inverter whose setpoint is battery-side
    exported ~14.7 kW for a 15 kW plan. export_kw gives an export-target
    ("sell power") action the plan's grid export itself; power_kw is
    unchanged, so nobody else's dispatch moves."""
    rows = [
        _row("2026-11-22T17:59:00+10:00", 15.54, 0.60, grid_export_kw=15.0),
        _row("2026-11-22T18:00:00+10:00", 15.54, 0.60, grid_export_kw=15.0),
    ]
    at = datetime(2026, 11, 22, 17, 59, 30, tzinfo=TZ)
    _, ctx = run("tick", plan="15.54", now=at, plan_age_s=20, attrs={"forecast": rows})
    assert ctx["export_kw"] == 15.0
    assert ctx["power_kw"] == 5.0  # clamped to max_power_kw, as before
    # No export in the plan, or a plan that cannot be followed: 0.
    _, ctx = run(
        "tick",
        plan="0.0",
        now=at,
        plan_age_s=20,
        attrs={"forecast": [dict(r, grid_export_kw=0.0) for r in rows]},
    )
    assert ctx["export_kw"] == 0
    _, ctx = run(
        "tick", plan="15.54", status="infeasible", now=at, attrs={"forecast": rows}
    )
    assert ctx["export_kw"] == 0
    # A plan from before forecast rows existed: 0, never an error.
    _, ctx = run("tick", plan="3.0")
    assert ctx["export_kw"] == 0
    # A manual Discharge exports the discharge power.
    sel = "input_select.dispatch_mode"
    _, ctx = run("tick", plan="0.0", mode_select=sel, extra={sel: "Discharge"})
    assert ctx["export_kw"] == 5.0


def test_the_discharge_setpoint_option_sends_the_export_when_chosen():
    """Household, 23 Nov 2026: a setting the user picks, not a template to
    edit. With "Grid export" chosen, power_kw carries the plan's export while
    discharging; charging, and the default, are battery power as before."""
    rows = [_row("2026-11-22T17:59:00+10:00", 4.6, 0.60, grid_export_kw=4.0)]
    at = datetime(2026, 11, 22, 17, 59, 30, tzinfo=TZ)
    attrs = {"forecast": rows}
    _, ctx = run("tick", plan="4.6", now=at, plan_age_s=20, attrs=attrs)
    assert ctx["power_kw"] == 4.6  # default: battery power
    _, ctx = run(
        "tick",
        plan="4.6",
        now=at,
        plan_age_s=20,
        attrs=attrs,
        discharge_setpoint="export",
    )
    assert ctx["target_mode"] == "DIS"
    assert ctx["power_kw"] == 4.0
    # Charging always sends battery power.
    charge = [_row("2026-11-22T17:59:00+10:00", -3.0, 0.10, grid_export_kw=0.0)]
    _, ctx = run(
        "tick",
        plan="-3.0",
        now=at,
        plan_age_s=20,
        attrs={"forecast": charge},
        discharge_setpoint="export",
    )
    assert ctx["target_mode"] == "CHG"
    assert ctx["power_kw"] == 3.0
