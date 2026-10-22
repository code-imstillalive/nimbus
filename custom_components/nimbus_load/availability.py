"""Which states of an availability entity mean "available" (EV / battery
participant availability, #563 item 2; widened 22 Oct 2026).

The availability entity was a binary_sensor, read as available only when
"on". A car's device_tracker ("home"), a person, or a charger status sensor
("Charging", "Connected") needed a template binary_sensor first. Now:

* the household may list the states that mean available
  (`CONF_BATTERY_PARTICIPANT_AVAILABLE_STATES`, comma-separated, matched
  ignoring case and surrounding spaces);
* left blank, each kind of entity keeps its standard meaning: "on" for a
  binary_sensor, input_boolean or switch, "home" for a device_tracker or
  person. Any other entity (a plain sensor) is available only in the states
  listed -- with none listed, never, the conservative reading the gate has
  always had for anything it cannot confirm.

Missing, unknown and unavailable states are never available. One helper for
the live gate, the history mask and diagnostics, so the three cannot read the
same entity differently. Pure: no Home Assistant import.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The standard "available" state of each kind of entity, used when the
#: household lists none.
DEFAULT_STATES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "binary_sensor": ("on",),
    "input_boolean": ("on",),
    "switch": ("on",),
    "device_tracker": ("home",),
    "person": ("home",),
}

#: Entity kinds the availability field accepts.
DOMAINS: tuple[str, ...] = (
    "binary_sensor",
    "device_tracker",
    "person",
    "sensor",
    "input_boolean",
    "switch",
)


def _normal(value: object) -> str:
    return str(value).strip().casefold()


def available_states(
    entity_id: str, configured: str | Iterable[str] | None
) -> frozenset[str]:
    """The states that mean available for `entity_id`: the household's own
    list when given, else its kind's standard one (empty for a plain
    sensor). The list is comma-separated, so a state whose own text holds a
    comma cannot be listed (Home Assistant states practically never do)."""
    if isinstance(configured, str):
        configured = configured.split(",")
    listed = frozenset(_normal(s) for s in configured or () if _normal(s))
    if listed:
        return listed
    domain = entity_id.split(".", 1)[0] if entity_id else ""
    return frozenset(DEFAULT_STATES_BY_DOMAIN.get(domain, ()))


def is_available(state: object, states: frozenset[str]) -> bool:
    """True when `state` is one of `states`. None is never available."""
    return state is not None and _normal(state) in states
