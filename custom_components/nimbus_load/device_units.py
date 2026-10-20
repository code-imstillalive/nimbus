"""Power sensors whose unit the household must declare (DC-006 observation
slice, #1574; household decision on #1893, 19 Oct 2026).

The contract refuses a power sensor that states no unit, or a spelling other
than Home Assistant's exact one ("kw", " kW "); today's readers take both as
kW. Before a household chooses `active`, each such sensor gets a Repair
where they declare W or kW for it (or fix the sensor). The declaration is
the binding's `normalization.native_unit`, which DC-002 applies in exactly
those two cases (`device_bindings._entity_unit_scale`).

Pure: no Home Assistant import. `device_contract_ha` owns the Repairs, the
store and the mode change; this module only computes.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

try:
    from . import device_bindings as db
except ImportError:  # imported directly by the device-contract tests
    import device_bindings as db  # type: ignore[no-redef]

#: What a household may declare. HA's exact power spellings, the two a
#: house sensor actually uses.
DECLARABLE_UNITS = ("W", "kW")

#: The sensor-side refusals a declaration settles (`device_bindings`).
_SETTLED_BY_DECLARING = frozenset({"unit_missing", "unit_not_exact"})


def undeclared(
    document: Mapping[str, Any] | None,
    read_state: Callable[[str], Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    """One entry per power binding read from an entity's state whose sensor
    states no unit or a non-exact one, and that has no declared unit yet:
    `{device_id, role, entity_id, stated_unit, reason}`. A sensor that is
    missing or unavailable right now is left out: its unit is unknown, and
    the next check sees it."""
    out: list[dict[str, Any]] = []
    for device in (document or {}).get("devices") or []:
        for role, binding in sorted((device.get("bindings") or {}).items()):
            if db.DIMENSION.get(role) != "power" or not isinstance(binding, dict):
                continue
            source = binding.get("source") or {}
            if source.get("kind") != "entity_state" or not source.get("entity_id"):
                continue
            if (binding.get("normalization") or {}).get("native_unit"):
                continue
            entity_id = str(source["entity_id"])
            st = read_state(entity_id)
            if st is None or st.get("state") in (None, "", "unknown", "unavailable"):
                continue
            stated = (st.get("attributes") or {}).get("unit_of_measurement")
            _scale, _unit, reason = db.unit_scale("power", stated)
            if reason in _SETTLED_BY_DECLARING:
                out.append(
                    {
                        "device_id": str(device.get("id")),
                        "role": role,
                        "entity_id": entity_id,
                        "stated_unit": stated,
                        "reason": reason,
                    }
                )
    return out


def declare(
    document: Mapping[str, Any], device_id: str, role: str, unit: str
) -> dict[str, Any] | None:
    """`document` with `unit` declared for one power binding, recorded as the
    household's override; None when the binding is not there or `unit` is
    not one a household may declare. The input is not changed."""
    if unit not in DECLARABLE_UNITS:
        return None
    out = copy.deepcopy(dict(document))
    for device in out.get("devices") or []:
        if str(device.get("id")) != device_id:
            continue
        binding = (device.get("bindings") or {}).get(role)
        if not isinstance(binding, dict):
            return None
        binding.setdefault("normalization", {})["native_unit"] = unit
        prov = binding.setdefault("provenance", {})
        prov["overridden"] = True
        return out
    return None


def carry_declared(
    old: Mapping[str, Any] | None, new: Mapping[str, Any]
) -> dict[str, Any]:
    """`new` with every unit `old` declared kept, where the same device and
    role still read the same entity. Moving to `active` re-imports the
    document from today's settings; without this a declaration made in
    `shadow` would be lost on the way."""
    out = copy.deepcopy(dict(new))
    kept: dict[tuple[str, str, str], str] = {}
    for device in (old or {}).get("devices") or []:
        for role, binding in (device.get("bindings") or {}).items():
            if not isinstance(binding, dict):
                continue
            unit = (binding.get("normalization") or {}).get("native_unit")
            entity = (binding.get("source") or {}).get("entity_id")
            if unit and entity:
                kept[(str(device.get("id")), role, str(entity))] = unit
    for device in out.get("devices") or []:
        for role, binding in (device.get("bindings") or {}).items():
            if not isinstance(binding, dict):
                continue
            entity = (binding.get("source") or {}).get("entity_id")
            unit = kept.get((str(device.get("id")), role, str(entity)))
            if unit and not (binding.get("normalization") or {}).get("native_unit"):
                binding.setdefault("normalization", {})["native_unit"] = unit
                binding.setdefault("provenance", {})["overridden"] = True
    return out
