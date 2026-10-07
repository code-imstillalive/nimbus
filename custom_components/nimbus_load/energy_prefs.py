"""Reading Home Assistant's Energy Dashboard preferences across schema versions
(nimbus #1589).

Home Assistant 2026.3 replaced the grid source's `flow_from` / `flow_to` lists
with a unified grid connection: one source per import/export pair, its fields
flat on the source (`stat_energy_from`, `stat_energy_to`, `entity_energy_price`,
`entity_energy_price_export`, ...). Existing preferences are migrated by HA
itself (`energy/data.py`, `_migrate_legacy_grid_to_unified`, read in HA
2026.3.0, 2026.7.4 and 2026.9.3). Code written against the lists silently finds
nothing on current HA.

`as_flow_lists` restates a unified grid source in the list form, mapping each
field back exactly as HA's migration mapped it forward, so the existing
list-reading code works on both.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# unified field -> (list, field in that list's entry), the inverse of HA's
# _migrate_legacy_grid_to_unified.
_IMPORT_FIELDS = {
    "stat_energy_from": "stat_energy_from",
    "stat_cost": "stat_cost",
    "entity_energy_price": "entity_energy_price",
    "number_energy_price": "number_energy_price",
}
_EXPORT_FIELDS = {
    "stat_energy_to": "stat_energy_to",
    "stat_compensation": "stat_compensation",
    "entity_energy_price_export": "entity_energy_price",
    "number_energy_price_export": "number_energy_price",
}


def as_flow_lists(source: Any) -> Any:
    """A grid source in the legacy `flow_from` / `flow_to` list form, whichever
    schema it arrived in. A legacy source, and every non-grid source, passes
    through unchanged. A unified source becomes one `flow_from` entry when it
    has an import meter and one `flow_to` entry when it has an export meter."""
    if not isinstance(source, Mapping) or source.get("type") != "grid":
        return source
    if "flow_from" in source or "flow_to" in source:
        return source
    flow_from = {k: source.get(u) for u, k in _IMPORT_FIELDS.items()}
    flow_to = {k: source.get(u) for u, k in _EXPORT_FIELDS.items()}
    return {
        **source,
        "flow_from": [flow_from] if source.get("stat_energy_from") else [],
        "flow_to": [flow_to] if source.get("stat_energy_to") else [],
    }


def energy_sources(prefs: Mapping[str, Any] | None) -> list[Any]:
    """The preferences' `energy_sources`, each grid source in list form."""
    return [as_flow_lists(s) for s in (prefs or {}).get("energy_sources") or []]
