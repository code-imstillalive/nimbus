"""The observation consumers' inputs, as plain values, in `legacy` and in
`active` (DC-006's equivalence gate, slice 2; DC-000 addition 1; #1574).

DC-006: "On F10, in `active`, the moved consumer's inputs are compared with
its inputs on the same install in `legacy`. They must be identical except for
entries in `approved_changes.json`." This module states both sides as data,
so the gate compares values, not code paths:

* `legacy_inputs()` reads each observation input exactly as one Forecaster
  model reads it today. Which sensor: the hub's `grid_sensor`,
  `battery_sensor` and `solar_sensor`, through the coordinator's own filters
  (`coordinator._grid_sensor` and its two siblings): none at all for a power
  signal, none when the sensor is the model's own target (#1540), none when
  it reports an energy unit (#1562). Its value: as
  `coordinator._current_measured_power` takes it, the unit converted with the
  legacy rules (`power_units`), and **0.0 kW** for a missing, unavailable,
  non-numeric or non-finite reading or for no sensor.
* `contract_inputs()` reads the same input from the stored document through
  DC-002 (`device_bindings.resolve`): the binding's entity, the contract's
  units and sign, and **None** (missing) for anything it cannot resolve.
* `differences()` lists every input whose entity or value differs.

Two rules for the Forecaster in `active`, decided by Mark Purcell on #1574
(21 Oct 2026, from #1947's gate):

* **A missing reading is held, never zero** (`ObservationHold`): the last
  good value is carried, marked `held_stale`, for up to the binding's own
  max age (`normalization.freshness.max_interval_s`, else DC-002's default),
  then the input is `absent` (readiness reports that once the Forecaster
  reads these inputs; that move is its own PR). DC-002's "a stale value
  is carried and flagged, not nulled", applied to the Forecaster.
* **Each model keeps the sign it was trained with** (`feature_value`): the
  contract's battery power is discharge-positive, but a model trained on a
  charge-positive sensor is fed that sign until its next nightly retrain,
  so switching to `active` changes no model's inputs.

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
    from .power_input_check import is_energy_unit
    from .power_units import is_power_unit, power_to_kw
except ImportError:  # imported directly by the device-contract tests
    import device_bindings as db  # type: ignore[no-redef]
    from power_input_check import is_energy_unit  # type: ignore[no-redef]
    from power_units import is_power_unit, power_to_kw  # type: ignore[no-redef]

#: Observation input -> (hub option the Forecaster reads today, device, role).
OBSERVATION_INPUTS: dict[str, tuple[str, str, str]] = {
    "grid_power": ("grid_sensor", "grid", "power"),
    "battery_power": ("battery_sensor", "battery", "power"),
    "solar_power": ("solar_sensor", "solar", "power"),
}

Reader = Callable[[str], Mapping[str, Any] | None]

#: Sign conventions a Forecaster model can have been trained on.
TRAINED_NATIVE = "native"
TRAINED_CANONICAL = "canonical"

#: `const.SUBENTRY_TYPE_SIGNAL`, kept literal so this module stays importable
#: without Home Assistant.
SUBENTRY_TYPE_SIGNAL = "power_signal"


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


def legacy_entity(
    options: Mapping[str, Any],
    option: str,
    read_state: Reader,
    subentry_type: str,
    own_target: str | None,
) -> str | None:
    """The sensor a Forecaster model reads for one observation input today,
    or None: `coordinator._grid_sensor` / `_battery_sensor` /
    `_solar_sensor` through `_power_feature_or_none`."""
    if subentry_type == SUBENTRY_TYPE_SIGNAL:
        return None
    sensor = options.get(option) or None
    if sensor is None or sensor == own_target:
        return None
    state = read_state(sensor)
    unit = (state or {}).get("attributes", {}).get("unit_of_measurement")
    if is_energy_unit(unit):
        return None
    return sensor


def legacy_inputs(
    options: Mapping[str, Any],
    read_state: Reader,
    subentry_type: str,
    own_target: str | None,
) -> dict[str, dict[str, Any]]:
    """One Forecaster model's observation inputs today: the subentry's type
    and its own target decide which sensors it reads at all."""
    out: dict[str, dict[str, Any]] = {}
    for name, (option, _device, _role) in OBSERVATION_INPUTS.items():
        entity = legacy_entity(options, option, read_state, subentry_type, own_target)
        out[name] = {
            "entity_id": entity,
            "value": legacy_value(read_state(entity) if entity else None),
        }
    return out


def _binding(document: Mapping[str, Any], device: str, role: str) -> Mapping:
    for d in document.get("devices") or []:
        if d.get("id") == device:
            return ((d.get("bindings") or {}).get(role)) or {}
    return {}


def max_age_s(binding: Mapping[str, Any]) -> float:
    """How long a missing reading is held: the binding's own declared
    interval, else DC-002's default."""
    rule = (binding.get("normalization") or {}).get("freshness") or {}
    limit = rule.get("max_interval_s")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
        return float(limit)
    return float(db.DEFAULT_MAX_AGE_S)


class ObservationHold:
    """The last good value of each observation input, for `active`."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[float, datetime]] = {}

    def apply(
        self, name: str, value: float | None, now: datetime, max_age: float
    ) -> tuple[float | None, str]:
        """(value to feed, state): `valid` for a fresh reading, `held_stale`
        for the last good one within `max_age` seconds, else `absent`."""
        if value is not None:
            self._last[name] = (value, now)
            return value, "valid"
        last = self._last.get(name)
        if last is not None and (now - last[1]).total_seconds() <= max_age:
            return last[0], "held_stale"
        return None, "absent"


def contract_inputs(
    document: Mapping[str, Any],
    read_state: Reader,
    now: datetime,
    hold: ObservationHold | None = None,
) -> dict[str, dict[str, Any]]:
    readers = db.Readers(state=read_state)
    out: dict[str, dict[str, Any]] = {}
    for name, (_option, device, role) in OBSERVATION_INPUTS.items():
        binding = _binding(document, device, role)
        entity = (binding.get("source") or {}).get("entity_id")
        got = db.resolve(document, device, role, readers, now)
        value = getattr(got, "value", None)
        row = {
            "entity_id": entity,
            "value": value,
            "reason": getattr(got, "reason", None),
        }
        if hold is not None:
            row["value"], row["state"] = hold.apply(
                name, value, now, max_age_s(binding)
            )
        out[name] = row
    return out


def feature_value(
    document: Mapping[str, Any], name: str, value: float | None, trained: str
) -> float | None:
    """`value` (canonical) in the sign a model was `trained` on: a model
    trained on the sensor's native sign gets it back in that sign."""
    if value is None or trained == TRAINED_CANONICAL:
        return value
    _option, device, role = OBSERVATION_INPUTS[name]
    norm = _binding(document, device, role).get("normalization") or {}
    native, canonical = (
        norm.get("native_positive_direction"),
        norm.get("positive_direction"),
    )
    flipped = bool(native and canonical and native != canonical)
    return -value + 0.0 if flipped else value


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


#: Observation input -> the Forecaster's feature name (`ml.features`).
FEATURE_OF: dict[str, str] = {
    "grid_power": "grid_kw",
    "battery_power": "battery_kw",
    "solar_power": "solar_kw",
}


def sign_flip(document: Mapping[str, Any], name: str) -> float:
    """-1.0 when the binding's native sign is the opposite of the contract's
    canonical sign for this input, else 1.0: native x flip = canonical."""
    _option, device, role = OBSERVATION_INPUTS[name]
    norm = _binding(document, device, role).get("normalization") or {}
    native, canonical = (
        norm.get("native_positive_direction"),
        norm.get("positive_direction"),
    )
    return -1.0 if (native and canonical and native != canonical) else 1.0


def forecaster_inputs(
    mode: str,
    document: Mapping[str, Any] | None,
    legacy: Mapping[str, Mapping[str, Any]],
    read_state: Reader,
    now: datetime,
    hold: ObservationHold,
    trained_sign: str,
    feature_means: Mapping[str, float],
) -> dict[str, dict[str, Any]]:
    """The value each observation feature takes for one Forecaster cycle.

    `legacy` is `legacy_inputs()` for this model. Outside `active` (or with
    no stored document) the value is exactly the legacy one, so nothing
    changes. In `active`, an input the model does not use today (no entity
    in `legacy`: not configured, the model's own target, an energy unit, or
    a power signal) stays 0.0 as before: a feature that was never part of
    the model is not missing data. Every other input comes from the
    contract: held for up to its max age when missing (`ObservationHold`),
    fed in the sign the model was trained with (`feature_value`), and once
    `absent`, set to the model's training mean for that feature (neutral,
    never a fabricated 0; Mark Purcell, #1574, 21 Oct 2026).

    Returns `{input: {"value", "state", "entity_id"}}`; `state` is `legacy`,
    `unused`, `valid`, `held_stale` or `absent`."""
    out: dict[str, dict[str, Any]] = {}
    active = mode == "active" and document is not None
    for name in OBSERVATION_INPUTS:
        row = legacy.get(name) or {}
        entity = row.get("entity_id")
        if not active:
            out[name] = {
                "value": row.get("value", 0.0),
                "state": "legacy",
                "entity_id": entity,
            }
            continue
        if entity is None:
            out[name] = {"value": 0.0, "state": "unused", "entity_id": None}
    if not active:
        return out
    contract = contract_inputs(document, read_state, now, hold)
    for name in OBSERVATION_INPUTS:
        if name in out:
            continue
        got = contract.get(name) or {}
        value = feature_value(document, name, got.get("value"), trained_sign)
        state = got.get("state", "absent")
        if value is None:
            value = float(feature_means.get(FEATURE_OF[name], 0.0))
            state = "absent"
        out[name] = {
            "value": float(value),
            "state": state,
            "entity_id": got.get("entity_id"),
        }
    return out
