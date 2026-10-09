"""nimbus issue #1661 (Mark Purcell): a price input must be a LIVE price.

LocalVolts' "Rate All Var" (`amountVar / volume`) and the dollar fields behind
it (`amountVar`, `amountAll`) are written when the forecast for an interval is
built and are not revised when it settles. On 9 Oct 2026 they stayed near 13c
export / 32c import through a $1.32/kWh spike while Flex Up followed it. A
price field pointed at one of these removes a spike from the solve with no
error, so it is reported:

- a Repair (setup_health.py, `frozen_price_input`) names every configured price
  field whose entity is one of these;
- the Solver's Grid step refuses one in the import/export price fields.

Recognised from the entity, never guessed from a value: the LocalVolts v2 feed
sensors carry a `source_field` attribute, and their unique ids end
`_rate_all_var`. HA-import-free apart from the registry lookup, which goes
through `hass`.
"""

from __future__ import annotations

from typing import Any

FROZEN_SOURCE_FIELDS = frozenset({"rateAllVar", "amountVar", "amountAll"})
_FROZEN_SUFFIX = "_rate_all_var"

# Every Solver setting that feeds a price into the solve, and the name the
# household sees for it.
PRICE_FIELDS: tuple[tuple[str, str], ...] = (
    ("solver_import_price_sensor", "Solver settings: import price"),
    ("solver_import_price_sensor_2", "Solver settings: import price (second source)"),
    ("solver_import_price_sensor_3", "Solver settings: import price (third source)"),
    ("solver_export_price_sensor", "Solver settings: export price"),
    ("solver_export_price_sensor_2", "Solver settings: export price (second source)"),
    ("solver_export_price_sensor_3", "Solver settings: export price (third source)"),
    ("solver_price_forecast_array_sensor", "Solver settings: price forecast array"),
)


def _unique_id(hass: Any, entity_id: str) -> str | None:
    try:
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(hass).async_get(entity_id)
        unique_id = getattr(entry, "unique_id", None)
        return unique_id if isinstance(unique_id, str) else None
    except Exception:  # noqa: BLE001 -- a registry lookup must never break setup
        return None


def frozen_rate_field(hass: Any, entity_id: str | None) -> str | None:
    """Why `entity_id` is a frozen-at-forecast-build rate, or None.

    The returned text names the evidence: the `source_field` attribute, or a
    unique id / entity id ending `_rate_all_var`. An entity that does not
    exist yet is not known to be wrong and answers None."""
    if not entity_id:
        return None
    try:
        state = hass.states.get(entity_id)
    except Exception:  # noqa: BLE001 -- a lookup failure must never break setup
        state = None
    if state is not None:
        field = state.attributes.get("source_field")
        if field in FROZEN_SOURCE_FIELDS:
            return f"source_field {field}"
    unique_id = _unique_id(hass, entity_id) or ""
    if unique_id.endswith(_FROZEN_SUFFIX):
        return f"unique id ends {_FROZEN_SUFFIX}"
    if entity_id.endswith(_FROZEN_SUFFIX):
        return f"entity id ends {_FROZEN_SUFFIX}"
    return None


def find_frozen_price_inputs(hass: Any, options: dict) -> list[tuple[str, str, str]]:
    """`(setting, entity_id, evidence)` for every configured price field whose
    entity is a frozen rate."""
    found = []
    for key, label in PRICE_FIELDS:
        entity_id = options.get(key)
        why = frozen_rate_field(hass, entity_id)
        if why is not None:
            found.append((label, entity_id, why))
    return found


def frozen_price_field_errors(
    hass: Any, user_input: dict[str, Any], fields: list[str]
) -> dict[str, str]:
    """Form errors for every field in `fields` pointed at a frozen rate."""
    return {
        field: "frozen_price_signal"
        for field in fields
        if frozen_rate_field(hass, user_input.get(field)) is not None
    }
