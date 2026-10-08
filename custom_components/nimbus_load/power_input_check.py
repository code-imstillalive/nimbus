"""nimbus issue #1562: catch an ENERGY counter configured where a POWER sensor
is expected.

Every power input Nimbus reads -- a Load or Power Signal's own sensor, and the
hub's shared Battery/Grid/Solar sensors -- is converted to kW by
power_units.py. An energy unit (Wh, kWh, ...) is not a power unit, so that
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


# nimbus #1643: the Solver's own power inputs. power_units.power_scale_to_kw()
# reads any unit it does not know as kW, so a current (A), apparent-power (kVA)
# or unknown unit on one of these is used as kW, silently. The Forecaster gates
# on `is_power_unit`; these did not. Labels are what the household sees.
_SOLVER_POWER_FIELDS: tuple[tuple[str, str], ...] = (
    ("solver_solar_power_sensor", "Solver settings: solar power sensor"),
    ("solver_battery_power_sensor", "Solver settings: battery power sensor"),
    (
        "solver_whole_house_cross_check_sensor",
        "Solver settings: whole-house load sensor",
    ),
    ("solver_load_forecast_sensor", "Solver settings: household load forecast"),
    ("switchboard_grid_meter_sensor", "Topology: grid meter"),
    ("switchboard_battery_power_sensor", "Topology: battery power sensor"),
)
# Subentry power fields: (subentry type, field, label prefix).
_SUBENTRY_POWER_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("controllable_load", "controllable_load_power_sensor", "Controllable load"),
    ("battery_participant", "battery_participant_power_sensor", "Battery"),
)

# One WARNING per entity per process, for the runtime read (`warn_non_power_once`).
_WARNED_NON_POWER: set[str] = set()


def is_non_power_unit(unit: object) -> bool:
    """True when `unit` is stated but is neither a power nor an energy unit:
    `A`, `kVA`, `%`, or anything unknown. No unit at all is not: it is read as
    kW by design (DC-000 F09, `open_decision`). Energy units have their own
    check (#1562) and are not reported twice."""
    try:
        from .power_units import is_power_unit
    except ImportError:  # pragma: no cover - standalone/cron path
        from power_units import is_power_unit  # type: ignore[no-redef]
    if not isinstance(unit, str) or not unit.strip():
        return False
    return not is_power_unit(unit) and not is_energy_unit(unit)


def _unit_of(hass: Any, entity_id: str | None) -> object:
    if not entity_id:
        return None
    try:
        state = hass.states.get(entity_id)
        return state.attributes.get("unit_of_measurement") if state else None
    except Exception:  # noqa: BLE001 -- a lookup failure must never break setup
        return None


def warn_non_power_once(entity_id: str, unit: object) -> None:
    """Log, once per entity, that a power input's unit is not a power unit and
    its value is being read as kW (nimbus #1643)."""
    if entity_id in _WARNED_NON_POWER:
        return
    _WARNED_NON_POWER.add(entity_id)
    _LOGGER.warning(
        "Nimbus: power sensor %s reports '%s', which is not a power unit (W or "
        "kW), so its value is being read as kW. Point the setting at a power "
        "sensor (nimbus #1643).",
        entity_id,
        unit,
    )


def find_non_power_unit_inputs(hass: Any, entry: Any) -> list[tuple[str, str, str]]:
    """`(setting, entity_id, unit)` for every configured power input whose
    unit is stated but is not a power unit (`is_non_power_unit`): the
    Solver's and Topology's power sensors, the Forecaster's Battery/Grid/Solar
    sensors, and each Load, Power Signal, controllable load and battery's own
    power sensor. An ENERGY unit on the Solver/Topology fields is listed too,
    since #1562's check does not cover them."""
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
    for key, label in _SOLVER_POWER_FIELDS:
        eid = options.get(key)
        unit = _unit_of(hass, eid)
        if is_non_power_unit(unit) or is_energy_unit(unit):
            found.append((label, eid, str(unit)))
    for key, label in (
        (CONF_BATTERY_SENSOR, "Forecaster: battery sensor"),
        (CONF_GRID_SENSOR, "Forecaster: grid sensor"),
        (CONF_SOLAR_SENSOR, "Forecaster: solar sensor"),
    ):
        eid = options.get(key)
        unit = _unit_of(hass, eid)
        if is_non_power_unit(unit):
            found.append((label, eid, str(unit)))
    fields = [(t, f, p) for t, f, p in _SUBENTRY_POWER_FIELDS]
    fields += [(SUBENTRY_TYPE_LOAD, CONF_LOAD_SENSOR, "Load")]
    fields += [(SUBENTRY_TYPE_SIGNAL, CONF_LOAD_SENSOR, "Power Signal")]
    for subentry in (getattr(entry, "subentries", {}) or {}).values():
        data = getattr(subentry, "data", None) or {}
        for sub_type, field, prefix in fields:
            if getattr(subentry, "subentry_type", None) != sub_type:
                continue
            eid = data.get(field)
            unit = _unit_of(hass, eid)
            energy_reported_elsewhere = sub_type in (
                SUBENTRY_TYPE_LOAD,
                SUBENTRY_TYPE_SIGNAL,
            )
            if is_non_power_unit(unit) or (
                is_energy_unit(unit) and not energy_reported_elsewhere
            ):
                title = getattr(subentry, "title", "")
                found.append((f"{prefix} '{title}'", eid, str(unit)))
    return found


async def async_notify_energy_unit_inputs(hass: Any, entry: Any) -> None:
    """Log each energy-unit power input, and dismiss the notification earlier
    releases raised for it. Never raises.

    nimbus #1574 stage 2: the household-facing report is now a Home Assistant
    Repair (setup_health.py, `energy_unit_power_input`), which clears itself
    once fixed. The notification is dismissed so an install upgrading from
    0.94.440 is not left with a stale one.
    """
    try:
        for label, eid, unit in find_energy_unit_inputs(hass, entry):
            _LOGGER.warning(
                "Nimbus: %s is %s, which reports '%s' -- an energy counter, "
                "not a power sensor (W or kW)",
                label,
                eid,
                unit,
            )
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": NOTIFICATION_ID},
            blocking=False,
        )
    except Exception:
        _LOGGER.debug("Nimbus: energy-unit input check failed", exc_info=True)
