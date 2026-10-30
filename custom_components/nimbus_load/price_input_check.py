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

from .const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    CONF_SOLVER_LOSS_FACTOR_DLF_CODE_SENSOR,
    CONF_SOLVER_LOSS_FACTOR_TNI_SENSOR,
)

FROZEN_SOURCE_FIELDS = frozenset({"rateAllVar", "amountVar", "amountAll"})
_FROZEN_SUFFIX = "_rate_all_var"

# nimbus #2191 (Mark Purcell): "source 1/buy" and "source 1/sell" by
# position -- never cross-position, since source 1's import compared
# against source 2's export is two unrelated, legitimately independent
# fields.
_IMPORT_EXPORT_PAIRS: tuple[tuple[str, str], ...] = (
    (CONF_SOLVER_IMPORT_PRICE_SENSOR, CONF_SOLVER_EXPORT_PRICE_SENSOR),
    (CONF_SOLVER_IMPORT_PRICE_SENSOR_2, CONF_SOLVER_EXPORT_PRICE_SENSOR_2),
    (CONF_SOLVER_IMPORT_PRICE_SENSOR_3, CONF_SOLVER_EXPORT_PRICE_SENSOR_3),
)

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


def price_combination_errors(user_input: dict[str, Any]) -> dict[str, str]:
    """Form errors for price-field *combinations* that can never be right,
    independent of whether any one entity is itself readable (nimbus #2191,
    Mark Purcell: "Nimbus should do some error checking if incorrect
    combinations have been made").

    Two shapes, both checkable from the submitted values alone:

    - AEMO's two loss-factor sensors (TNI, DLF code) are a pair -- the Grid
      step's own field text says "both are needed" to turn the regional
      price into a retail one (nimbus #1786). One set without the other can
      never produce a usable AEMO-based price, so it is refused here rather
      than silently solving with an incomplete AEMO source.
    - The same entity pointed at a source's import field AND that SAME
      source's export field is always wrong -- a buy price and a sell price
      are never the same live sensor. Checked by position only (source 1
      import vs source 1 export, source 2 vs source 2, ...): source 1's
      import compared against source 2's export is two unrelated fields,
      not a combination this function has any opinion on.
    """
    errors: dict[str, str] = {}
    tni = user_input.get(CONF_SOLVER_LOSS_FACTOR_TNI_SENSOR)
    dlf = user_input.get(CONF_SOLVER_LOSS_FACTOR_DLF_CODE_SENSOR)
    if bool(tni) != bool(dlf):
        errors[
            CONF_SOLVER_LOSS_FACTOR_DLF_CODE_SENSOR
            if tni
            else CONF_SOLVER_LOSS_FACTOR_TNI_SENSOR
        ] = "loss_factor_pair_incomplete"
    for import_key, export_key in _IMPORT_EXPORT_PAIRS:
        import_entity = user_input.get(import_key)
        export_entity = user_input.get(export_key)
        if import_entity and export_entity and import_entity == export_entity:
            errors[export_key] = "import_export_same_entity"
    return errors
