"""Shared helpers for Nimbus's config-subentry flows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.helpers import selector

from ..const import (
    CONF_LOAD_SENSOR,
    CONF_PART_OF,
    CONF_SIGNAL_ROLE,
    PART_OF_NONE,
    SIGNAL_ROLE_OTHER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)

# Both Load and Power Signal subentries feed their own CONF_LOAD_SENSOR
# straight into sensor.py's object_id_from_source(), which derives the
# forecast entity_id PURELY from the source sensor's own entity_id, with
# no subentry-scoping at all (nimbus issue #362 finding 4d, Mark Purcell,
# codebase review). _attr_unique_id IS already subentry-scoped and safe
# -- the real risk is entity_id itself: if a second subentry (of EITHER
# type, since both share this one entity_id namespace) points at the
# SAME source sensor, HA's entity registry silently suffixes the second
# one's real entity_id with "_2", so any code path that re-derives the
# expected entity_id via object_id_from_source() a second time (or a
# user/automation that assumes the predictable name) silently targets
# whichever subentry happened to register first instead.
_SOURCE_SENSOR_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)


def find_subentry_sharing_source_sensor(
    entry: Any,
    source_sensor: str,
    *,
    exclude_subentry_id: str | None = None,
) -> Any | None:
    """The first existing Load/Power Signal subentry on `entry` (other
    than `exclude_subentry_id`, so reconfiguring a subentry with its own
    unchanged sensor doesn't flag itself) whose own CONF_LOAD_SENSOR
    already matches `source_sensor` -- or None if there isn't one.

    Called from both load_subentry.py and signal_subentry.py's own
    _async_step() before creating/updating a subentry, since a
    collision from EITHER type collides in the SAME entity_id namespace
    (see module docstring above). Returns the real ConfigSubentry object
    (not just a bool) so the caller can build a specific, useful error
    message naming the conflicting subentry's own title.
    """
    for subentry in entry.subentries.values():
        if subentry.subentry_id == exclude_subentry_id:
            continue
        if subentry.subentry_type not in _SOURCE_SENSOR_SUBENTRY_TYPES:
            continue
        if subentry.data.get(CONF_LOAD_SENSOR) == source_sensor:
            return subentry
    return None


def form_action_placeholders(flow: Any) -> dict[str, str]:
    """`{action}` for a subentry form's title: "Edit" when the form was
    opened from an existing entry's cog (reconfigure), "Add" for a new one.
    Every subentry flow shows the same `user` step for both, so without
    this an edit was titled "Add a load" (household, 25 Oct 2026)."""
    return {"action": "Edit" if flow.source == SOURCE_RECONFIGURE else "Add"}


# nimbus #2293: "Part of another circuit" on a Load or Power Signal. The
# answer (`part_of`, the parent's subentry id) is the view of DC-003's
# `included_in_measurement`; display only until DC-006 (Mark Purcell).
def _circuit_like(sub: Any) -> bool:
    """A Load, or a plain ("other") Power Signal: what a meter can sit in."""
    if sub.subentry_type == "load":
        return True
    return (
        sub.subentry_type == "power_signal"
        and sub.data.get(CONF_SIGNAL_ROLE, SIGNAL_ROLE_OTHER) == SIGNAL_ROLE_OTHER
    )


def part_of_parents(entry: Any, own_subentry_id: str | None) -> list[tuple[str, str]]:
    """(subentry id, title) of each Load or plain Power Signal this one may
    be part of: one that is not itself part of another. Empty when this one
    already has a sub-device, so the tree stays one tier."""
    circuits = [s for s in entry.subentries.values() if _circuit_like(s)]
    if own_subentry_id and any(
        s.data.get(CONF_PART_OF) == own_subentry_id for s in circuits
    ):
        return []
    ids = {s.subentry_id for s in circuits}
    # A circuit whose parent was removed counts as top level again.
    top = [s for s in circuits if s.data.get(CONF_PART_OF) not in ids]
    return [
        (s.subentry_id, s.title)
        for s in sorted(top, key=lambda s: str(s.title).lower())
        if s.subentry_id != own_subentry_id
    ]


def part_of_field(
    defaults: dict[str, Any], parents: list[tuple[str, str]] | None
) -> dict[Any, Any]:
    """The optional "Part of another circuit" dropdown; none without parents."""
    if not parents:
        return {}
    current = defaults.get(CONF_PART_OF)
    if current not in {sid for sid, _t in parents}:
        current = PART_OF_NONE
    return {
        vol.Optional(CONF_PART_OF, default=current): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": PART_OF_NONE, "label": "Not part of another circuit"},
                    *({"value": sid, "label": title} for sid, title in parents),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }


def without_invalid_part_of(
    user_input: dict[str, Any], parents: list[tuple[str, str]], allowed: bool
) -> dict[str, Any]:
    """`user_input` with `part_of` left out unless it names an offered
    parent (and this kind may have one), so "none" saves nothing."""
    if allowed and user_input.get(CONF_PART_OF) in {sid for sid, _t in parents}:
        return user_input
    return {k: v for k, v in user_input.items() if k != CONF_PART_OF}
