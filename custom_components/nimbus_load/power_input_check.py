"""nimbus issue #1562: catch an ENERGY counter configured where a POWER sensor
is expected.

Every power input Nimbus reads -- a Load or Power Signal's own sensor, and the
hub's shared Battery/Grid/Solar sensors -- is converted to kW with HA's
PowerConverter. An energy unit (Wh, kWh, ...) is not a power unit, so that
conversion raises, and until this module the coordinator logged
"unconvertible unit 'Wh' -- treating as kW as-is" and carried on using the
number. For an energy counter that is never right: a cumulative total is not a
power reading under any unit change, so the model trained on garbage while the
only sign was a repeated log line. Seen on a tester install, 2026-10-06:
`sensor.solar_production_total` (Wh) as the hub's solar sensor, 52 warnings in
an hour.

Three layers, each cheap and each covering a case the others cannot:

- the setup forms refuse an energy sensor in a power field
  (`energy_unit_field_errors`), so a new mistake never gets saved;
- at startup a persistent notification names any saved power input that is an
  energy counter (`async_notify_energy_unit_inputs`), so an install configured
  before this check is told;
- at runtime the coordinator ignores an energy-counter Battery/Grid/Solar
  feature, the same as not configured, instead of reading it as kW
  (`energy_unit_of`). A Load or Power Signal's OWN sensor cannot be ignored --
  it is the thing being forecast -- so for that one the notification is the
  remedy.

HA-import-free apart from the service call, which goes through `hass`.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Every energy unit HA's EnergyConverter accepts (homeassistant/const.py
# UnitOfEnergy), compared case-insensitively so a hand-typed "wh" or "KWH" on a
# template sensor is caught too. Listed rather than imported so this module
# stays importable without Home Assistant, like solver/ and ml/.
_ENERGY_UNITS = frozenset(
    u.casefold()
    for u in (
        "J",
        "kJ",
        "MJ",
        "GJ",
        "mWh",
        "Wh",
        "kWh",
        "MWh",
        "GWh",
        "TWh",
        "cal",
        "kcal",
        "Mcal",
        "Gcal",
    )
)

NOTIFICATION_ID = "nimbus_energy_unit_power_inputs"

# One WARNING per entity per process: the coordinator asks on every cycle.
_WARNED: set[str] = set()


def is_energy_unit(unit: object) -> bool:
    """True when `unit` is an energy unit. Anything that is not a string
    (None, a test double) is not."""
    return isinstance(unit, str) and unit.strip().casefold() in _ENERGY_UNITS


def energy_unit_of(hass: Any, entity_id: str | None) -> str | None:
    """The entity's unit when it is an energy unit, else None.

    Reads the live state, so it answers for what the sensor reports now. An
    entity that does not exist yet (still loading) answers None -- it is not
    known to be wrong, and the startup notification runs after HA has started.
    """
    if not entity_id:
        return None
    try:
        state = hass.states.get(entity_id)
        unit = (
            state.attributes.get("unit_of_measurement") if state is not None else None
        )
    except Exception:  # noqa: BLE001 -- a lookup failure must never break a forecast cycle
        return None
    return unit if is_energy_unit(unit) else None


def warn_ignored_once(entity_id: str, unit: str, role: str) -> None:
    """Log, once per entity, that an energy-counter feature is being ignored."""
    if entity_id in _WARNED:
        return
    _WARNED.add(entity_id)
    _LOGGER.warning(
        "Nimbus: %s sensor %s reports '%s', an energy unit -- it is an energy "
        "counter, not a power reading, so it is ignored as a feature. Point the "
        "%s sensor at a power sensor (W or kW) in Nimbus settings -> Forecaster.",
        role,
        entity_id,
        unit,
        role,
    )


def energy_unit_field_errors(
    hass: Any, user_input: dict[str, Any], fields: list[str]
) -> dict[str, str]:
    """Form errors for every field in `fields` whose chosen entity reports an
    energy unit. `{}` when there are none."""
    return {
        field: "energy_sensor_not_power"
        for field in fields
        if energy_unit_of(hass, user_input.get(field)) is not None
    }


def find_energy_unit_inputs(hass: Any, entry: Any) -> list[tuple[str, str, str]]:
    """`(setting, entity_id, unit)` for every configured power input that
    reports an energy unit: the hub's Battery/Grid/Solar sensors, and each
    Load and Power Signal's own sensor."""
    try:
        from .const import (
            CONF_BATTERY_SENSOR,
            CONF_GRID_SENSOR,
            CONF_LOAD_SENSOR,
            CONF_SOLAR_SENSOR,
            SUBENTRY_TYPE_LOAD,
            SUBENTRY_TYPE_SIGNAL,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (  # type: ignore[no-redef]
            CONF_BATTERY_SENSOR,
            CONF_GRID_SENSOR,
            CONF_LOAD_SENSOR,
            CONF_SOLAR_SENSOR,
            SUBENTRY_TYPE_LOAD,
            SUBENTRY_TYPE_SIGNAL,
        )

    found: list[tuple[str, str, str]] = []
    options = dict(getattr(entry, "options", {}) or {})
    for key, label in (
        (CONF_BATTERY_SENSOR, "Forecaster: battery sensor"),
        (CONF_GRID_SENSOR, "Forecaster: grid sensor"),
        (CONF_SOLAR_SENSOR, "Forecaster: solar sensor"),
    ):
        entity_id = options.get(key)
        unit = energy_unit_of(hass, entity_id)
        if unit is not None:
            found.append((label, entity_id, unit))
    for subentry in (getattr(entry, "subentries", {}) or {}).values():
        if subentry.subentry_type not in (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL):
            continue
        entity_id = subentry.data.get(CONF_LOAD_SENSOR)
        unit = energy_unit_of(hass, entity_id)
        if unit is not None:
            kind = (
                "Load"
                if subentry.subentry_type == SUBENTRY_TYPE_LOAD
                else "Power Signal"
            )
            found.append((f"{kind} '{subentry.title}'", entity_id, unit))
    return found


async def async_notify_energy_unit_inputs(hass: Any, entry: Any) -> None:
    """Create, or dismiss, the startup notification. Never raises."""
    try:
        found = find_energy_unit_inputs(hass, entry)
        if not found:
            await hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": NOTIFICATION_ID},
                blocking=False,
            )
            return
        listed = "\n".join(
            f"- {label}: `{eid}` reports **{unit}**" for label, eid, unit in found
        )
        for label, eid, unit in found:
            _LOGGER.warning(
                "Nimbus: %s is %s, which reports '%s' -- an energy counter, "
                "not a power sensor (W or kW)",
                label,
                eid,
                unit,
            )
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": NOTIFICATION_ID,
                "title": "Nimbus: energy sensor where a power sensor is needed",
                "message": (
                    "These settings point at an energy counter (Wh/kWh), but "
                    "Nimbus needs a power sensor (W or kW):\n\n"
                    f"{listed}\n\n"
                    "An energy total cannot be read as power. A Battery, Grid "
                    "or Solar sensor like this is ignored until it is changed; "
                    "a Load or Power Signal cannot forecast correctly from it. "
                    "Pick the matching power sensor (most inverters and meters "
                    "expose both) in Nimbus settings, or reconfigure the Load / "
                    "Power Signal."
                ),
            },
            blocking=False,
        )
    except Exception:
        _LOGGER.debug("Nimbus: energy-unit input check failed", exc_info=True)
