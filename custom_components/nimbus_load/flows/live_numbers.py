"""Ask for the battery and grid numbers in the Solver wizard (nimbus #1800).

A fresh install starts battery capacity, max charge, max discharge and grid
max import at number.py's 0.1 placeholder. They live only on number
entities, so a new user had to find four entities on a dashboard after
setup, with a Repair (#1833) the only pointer. #1800's bar is "a valid plan
without editing number entities by hand".

The wizard now shows them, pre-filled from the entity's current value (blank
while it still sits on the placeholder). On submit they are written through
each entity's own `number.set_value`, so its bounds and its storage stay the
only path. They are never stored in the config entry's options: a second copy
there would be two masters for one setting, the conflict DC-004 names
(`conflict_setting_copies`).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..const import (
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    DOMAIN,
)

#: Form-field prefix: keeps these out of `_SOLVER_WIZARD_SCHEMA_KEYS`, so the
#: options merge never sees them.
PREFIX = "live_"

#: Which wizard step shows which live number: (key, unit, min, max, step).
#: The bounds are number.py's own for these four keys.
STEP_NUMBERS: dict[str, tuple[tuple[str, str, float, float, float], ...]] = {
    "solver_battery": (
        (CONF_SOLVER_BATTERY_CAPACITY_KWH, "kWh", 0.1, 2000.0, 0.1),
        (CONF_SOLVER_MAX_CHARGE_KW, "kW", 0.1, 1000.0, 0.1),
        (CONF_SOLVER_MAX_DISCHARGE_KW, "kW", 0.1, 1000.0, 0.1),
    ),
    "solver_grid": ((CONF_SOLVER_GRID_MAX_IMPORT_KW, "kW", 0.1, 1000.0, 0.1),),
}

#: The grid step collapses its optional fields; this one stays visible.
GRID_IMPORT_FIELD = PREFIX + CONF_SOLVER_GRID_MAX_IMPORT_KW

#: number.py starts every one of these at its minimum: "not set yet".
PLACEHOLDER = 0.1


def _entity_id(hass: Any, entry_id: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("number", DOMAIN, f"{entry_id}_{key}")


def current_value(hass: Any, entry_id: str, key: str) -> float | None:
    """The live entity's value, or None while it is missing, unavailable or
    still on the placeholder (a blank field reads better than 0.1)."""
    eid = _entity_id(hass, entry_id, key)
    state = hass.states.get(eid) if eid else None
    try:
        value = float(state.state) if state is not None else None
    except (TypeError, ValueError):
        return None
    if value is None or abs(value - PLACEHOLDER) < 1e-9:
        return None
    return value


def schema_fields(hass: Any, entry_id: str, step: str) -> dict[Any, Any]:
    """The extra fields for `step`, each pre-filled from its live entity."""
    fields: dict[Any, Any] = {}
    for key, unit, lo, hi, step_size in STEP_NUMBERS.get(step, ()):
        fields[
            vol.Optional(
                PREFIX + key,
                description={"suggested_value": current_value(hass, entry_id, key)},
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=lo,
                max=hi,
                step=step_size,
                unit_of_measurement=unit,
                mode=selector.NumberSelectorMode.BOX,
            )
        )
    return fields


def pop_values(user_input: dict[str, Any]) -> dict[str, float]:
    """Take this step's live-number answers out of `user_input` (in place),
    keyed by the number key. A blank field is not an answer."""
    out: dict[str, float] = {}
    for form_key in [k for k in user_input if k.startswith(PREFIX)]:
        value = user_input.pop(form_key)
        if value is not None and value != "":
            out[form_key[len(PREFIX) :]] = float(value)
    return out


async def async_apply(hass: Any, entry_id: str, values: dict[str, float]) -> list[str]:
    """Write each value through its entity's own `number.set_value`. Returns
    the keys whose entity was not found (nothing is written for those)."""
    missing: list[str] = []
    for key, value in values.items():
        eid = _entity_id(hass, entry_id, key)
        if eid is None:
            missing.append(key)
            continue
        await hass.services.async_call(
            "number", "set_value", {"entity_id": eid, "value": value}, blocking=True
        )
    return missing
