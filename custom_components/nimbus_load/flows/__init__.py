"""Shared helpers for Nimbus's config-subentry flows."""

from __future__ import annotations

from typing import Any

from ..const import CONF_LOAD_SENSOR, SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL

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
