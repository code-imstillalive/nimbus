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

ATTRIBUTE_DONE_DOMAINS = ("water_heater", "climate")


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
