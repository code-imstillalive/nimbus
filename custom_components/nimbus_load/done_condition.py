"""nimbus issue #534 (Mark Purcell, real SG-Ready heat-pump HWS install)
and #590 (Mark, "what will the tank temperature do?"): the shared
current_temperature read for a water_heater's/climate's own done_entity
-- their own *state* is a mode string ("eco"), never a number, so a
done condition (or a schedule-view status string) reading one of these
two domains needs the current_temperature ATTRIBUTE instead.

Deliberately its own module, with zero project-internal or heavy
third-party imports -- no numpy/highspy, unlike solver_writer.py, which
imports both at module level and is therefore NOT safe to import from a
plain synchronous/event-loop context before the first real solve cycle
has already paid that import cost once (see solver_writer.py's own
#349 fix, and sensor.py's own deferred/executor-job imports of
solver_writer for the same reason). solver_writer.py's own
_evaluate_done_condition() and sensor.py's schedule-view sensors (#590)
both import from here instead of one importing the other, so there is
exactly one real implementation of "how do we read a water_heater's/
climate's own live temperature" for the whole project, and neither call
site pays the other's own import cost to reach it.
"""

from __future__ import annotations

import operator
from collections.abc import Callable

ATTRIBUTE_DONE_DOMAINS = ("water_heater", "climate")

# nimbus issue #639: moved here (unchanged) from solver_writer.py's own
# module-private _DONE_WHEN_OPERATORS/_DONE_WHEN_OPERATOR_ORDER/
# _parse_done_when -- sensor.py's schedule-view sensors need to evaluate
# the identical done_when comparison (to tell a genuine done_when-fired
# "done" apart from a merely energy-target-met release, see
# is_tank_done() below) without importing solver_writer's heavy numpy/
# highspy module, same reasoning ATTRIBUTE_DONE_DOMAINS above already
# documents. solver_writer.py now imports these from here instead of
# keeping its own copy, so there is exactly one implementation.
_DONE_WHEN_OPERATOR_ORDER = (">=", "<=", "==", "!=", ">", "<")
_DONE_WHEN_OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}


def parse_done_when(done_when: str) -> tuple[Callable[[float, float], bool], float]:
    """Parses done_when into (operator_fn, threshold). Raises ValueError
    for anything that doesn't match `<op><number>` (whitespace-tolerant)
    -- the caller treats that as a misconfiguration, not a crash."""
    stripped = done_when.strip()
    for op_str in _DONE_WHEN_OPERATOR_ORDER:
        if stripped.startswith(op_str):
            threshold_str = stripped[len(op_str) :].strip()
            return _DONE_WHEN_OPERATORS[op_str], float(threshold_str)
    msg = (
        f"done_when {done_when!r} doesn't start with a recognized operator "
        f"({', '.join(_DONE_WHEN_OPERATOR_ORDER)})"
    )
    raise ValueError(msg)


def is_tank_done(
    current_temperature: float | None,
    done_when: str | None,
    setpoint_temperature: float | None = None,
) -> bool | None:
    """nimbus issue #639 (Mark Purcell, live verification on the #534
    heat pump): whether a water_heater/climate done_entity's own
    done_when condition is genuinely satisfied right now -- distinct
    from a deferrable load's energy target merely being met. Mark's real
    case: a load released because its kWh target was reached showed
    `status: "done (tank 52 °C)"` right next to a device-page "done at
    60 °C" line -- 52 is below 60, so the tank was never actually done;
    only the energy target was. sensor.py's schedule-view status text
    uses this to pick the right wording for the two genuinely different
    release reasons.

    Same fail-open contract as read_current_temperature()/
    _evaluate_done_condition(): None (never a raised exception or a
    guessed True/False) for a missing reading or a malformed done_when.
    An unset done_when falls back to the entity's own setpoint
    attribute, same #534 convention _evaluate_done_condition() itself
    uses -- the caller passes `setpoint_temperature` for that case.
    """
    if current_temperature is None:
        return None
    if done_when is None:
        if setpoint_temperature is None:
            return None
        try:
            return current_temperature >= float(setpoint_temperature)
        except (TypeError, ValueError):
            return None
    try:
        op_fn, threshold = parse_done_when(done_when)
        return bool(op_fn(current_temperature, threshold))
    except (ValueError, TypeError):
        return None


def read_current_temperature(hass, done_entity: str) -> float | None:
    """Real live reading for a water_heater/climate done_entity's own
    current_temperature attribute. None for any other domain, a missing
    or unavailable entity, or a missing/non-numeric attribute -- fail
    open, same posture as every other done-condition read in this
    project (a household mid-restart, or a device briefly offline,
    should never be treated as an error)."""
    state_obj = hass.states.get(done_entity)
    if state_obj is None or state_obj.state in (None, "unknown", "unavailable"):
        return None
    domain = done_entity.split(".", 1)[0]
    if domain not in ATTRIBUTE_DONE_DOMAINS:
        return None
    current = state_obj.attributes.get("current_temperature")
    if current is None:
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None
