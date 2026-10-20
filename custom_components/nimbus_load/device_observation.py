"""The observation consumers' inputs, as plain values, in `legacy` and in
`active` (DC-006's equivalence gate, slice 2; DC-000 addition 1; #1574).

DC-006: "On F10, in `active`, the moved consumer's inputs are compared with
its inputs on the same install in `legacy`. They must be identical except for
entries in `approved_changes.json`." This module states both sides as data,
so the gate compares values, not code paths:

* `legacy_inputs()` reads each observation input exactly as the Forecaster
  reads it today (`coordinator._current_measured_power`): the hub's
  `grid_sensor`, `battery_sensor` and `solar_sensor`, the unit converted with
  the legacy rules (`power_units`), and **0.0 kW** for a missing,
  unavailable, non-numeric or non-finite reading.
* `contract_inputs()` reads the same input from the stored document through
  DC-002 (`device_bindings.resolve`): the binding's entity, the contract's
  units and sign, and **None** (missing) for anything it cannot resolve.
* `differences()` lists every input whose entity or value differs.

Pure: no Home Assistant import. Nothing here changes what any consumer
reads; the slice that moves the Forecaster uses this to show, before it
merges, exactly what would change.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

try:
    from . import device_bindings as db
    from .power_units import is_power_unit, power_to_kw
except ImportError:  # imported directly by the device-contract tests
    import device_bindings as db  # type: ignore[no-redef]
    from power_units import is_power_unit, power_to_kw  # type: ignore[no-redef]

#: Observation input -> (hub option the Forecaster reads today, device, role).
OBSERVATION_INPUTS: dict[str, tuple[str, str, str]] = {
    "grid_power": ("grid_sensor", "grid", "power"),
    "battery_power": ("battery_sensor", "battery", "power"),
    "solar_power": ("solar_sensor", "solar", "power"),
}

Reader = Callable[[str], Mapping[str, Any] | None]


def legacy_value(state: Mapping[str, Any] | None) -> float:
    """One reading as `coordinator._current_measured_power` takes it."""
    if state is None:
        return 0.0
    try:
        value = float(state.get("state"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    if unit and unit != "kW" and is_power_unit(unit):
        value = power_to_kw(value, unit)
    return value


def legacy_inputs(
    options: Mapping[str, Any], read_state: Reader
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, (option, _device, _role) in OBSERVATION_INPUTS.items():
        entity = options.get(option) or None
        out[name] = {
            "entity_id": entity,
            "value": legacy_value(read_state(entity) if entity else None),
        }
    return out


def contract_inputs(
    document: Mapping[str, Any], read_state: Reader, now: datetime
) -> dict[str, dict[str, Any]]:
    devices = {d.get("id"): d for d in document.get("devices") or []}
    readers = db.Readers(state=read_state)
    out: dict[str, dict[str, Any]] = {}
    for name, (_option, device, role) in OBSERVATION_INPUTS.items():
        binding = ((devices.get(device) or {}).get("bindings") or {}).get(role) or {}
        entity = (binding.get("source") or {}).get("entity_id")
        got = db.resolve(document, device, role, readers, now)
        out[name] = {
            "entity_id": entity,
            "value": getattr(got, "value", None),
            "reason": getattr(got, "reason", None),
        }
    return out


def differences(
    legacy: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Mapping[str, Any]],
    tolerance: float = 1e-9,
) -> list[dict[str, Any]]:
    """`{input, field, legacy, contract}` for each entity or value that
    differs. A value is the same only when both are numbers within
    `tolerance`: 0.0 in legacy against None (missing) is a difference."""
    out: list[dict[str, Any]] = []
    for name in OBSERVATION_INPUTS:
        a, b = legacy.get(name) or {}, contract.get(name) or {}
        if a.get("entity_id") != b.get("entity_id"):
            out.append(
                {
                    "input": name,
                    "field": "entity_id",
                    "legacy": a.get("entity_id"),
                    "contract": b.get("entity_id"),
                }
            )
            continue
        va, vb = a.get("value"), b.get("value")
        same = (
            isinstance(va, float)
            and isinstance(vb, float)
            and abs(va - vb) <= tolerance
        )
        if not same:
            out.append({"input": name, "field": "value", "legacy": va, "contract": vb})
    return out
