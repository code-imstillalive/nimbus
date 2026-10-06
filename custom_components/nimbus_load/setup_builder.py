"""Two-step setup, stage 1: fill the gaps from what is already entered
(nimbus #1574, design in docs/design/two-step-setup.md §12).

A tester's first install (#1526) showed the pattern this closes: each physical
sensor had to be entered in up to four places, nothing linked them, and every
gap was silent. He set a battery and a solar sensor in Forecaster settings,
which made them *features*, but got no Battery or Solar forecast (that needs a
separate "Add Power Signal" the wizard never mentions), and the Solver's own
battery and solar power fields stayed empty.

This module derives the missing pieces from sensors the user has ALREADY named,
so nothing is guessed:

- a **Power Signal** for each Forecaster battery / grid / solar sensor, and for
  each Solver battery / solar power sensor, when no Power Signal or Load already
  forecasts that sensor (matched by sensor, never by role: the reference
  household's signals all carry role "other");
- the Solver's **battery power** and **solar power** fields from the Forecaster's
  battery and solar sensors, when empty;
- the Solver's **whole-house cross-check** from the sensor behind the Solver's
  own load forecast, when empty.

Rules (design §10):
- **Never overwrite.** Only empty fields are filled; nothing existing is edited.
- **Never twice.** Each fill is recorded in the entry's options under
  `CONF_SETUP_BUILDER_DONE`, so an auto-created signal the user deletes, or a
  field the user clears, is not recreated.
- **Refuse the wrong kind.** A candidate must report a power unit (#1562,
  #1570); an energy total or a unit-less sensor is skipped with a reason.
- **Never silent.** `async_apply_setup_fills` lists everything it did in one
  notification.

`plan_setup_fills` is pure (no Home Assistant) so real installs' diagnostics
replay through it in tests (design §11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from .const import (
        CONF_BATTERY_SENSOR,
        CONF_GRID_SENSOR,
        CONF_LOAD_SENSOR,
        CONF_SIGNAL_ROLE,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        SIGNAL_ROLE_BATTERY,
        SIGNAL_ROLE_GRID,
        SIGNAL_ROLE_SOLAR,
        SUBENTRY_TYPE_LOAD,
        SUBENTRY_TYPE_SIGNAL,
    )
    from .power_input_check import is_energy_unit
    from .power_units import is_power_unit
except ImportError:  # pragma: no cover - standalone path
    from const import (  # type: ignore[no-redef]
        CONF_BATTERY_SENSOR,
        CONF_GRID_SENSOR,
        CONF_LOAD_SENSOR,
        CONF_SIGNAL_ROLE,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        SIGNAL_ROLE_BATTERY,
        SIGNAL_ROLE_GRID,
        SIGNAL_ROLE_SOLAR,
        SUBENTRY_TYPE_LOAD,
        SUBENTRY_TYPE_SIGNAL,
    )
    from power_input_check import is_energy_unit  # type: ignore[no-redef]
    from power_units import is_power_unit  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Options key recording what this module has already done, so nothing it
# created and the user later removed is ever put back. A list of strings:
# "signal:<sensor>" and "option:<key>".
CONF_SETUP_BUILDER_DONE = "setup_builder_done"

NOTIFICATION_ID = "nimbus_setup_builder"

# (options key holding a sensor, role of the Power Signal it should have)
_SIGNAL_SOURCES: tuple[tuple[str, str], ...] = (
    (CONF_BATTERY_SENSOR, SIGNAL_ROLE_BATTERY),
    (CONF_SOLVER_BATTERY_POWER_SENSOR, SIGNAL_ROLE_BATTERY),
    (CONF_SOLAR_SENSOR, SIGNAL_ROLE_SOLAR),
    (CONF_SOLVER_SOLAR_POWER_SENSOR, SIGNAL_ROLE_SOLAR),
    (CONF_GRID_SENSOR, SIGNAL_ROLE_GRID),
)

# (Solver field to fill, Forecaster field it is filled from)
_SOLVER_FILLS: tuple[tuple[str, str], ...] = (
    (CONF_SOLVER_BATTERY_POWER_SENSOR, CONF_BATTERY_SENSOR),
    (CONF_SOLVER_SOLAR_POWER_SENSOR, CONF_SOLAR_SENSOR),
)

_ROLE_LABEL = {
    SIGNAL_ROLE_BATTERY: "Battery",
    SIGNAL_ROLE_SOLAR: "Solar",
    SIGNAL_ROLE_GRID: "Grid",
}


@dataclass
class SignalToCreate:
    sensor: str
    role: str
    title: str
    because: str  # which setting named the sensor


@dataclass
class SetupPlan:
    signals: list[SignalToCreate] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    # (what, why) for each fill that was considered and NOT made
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # markers to append to CONF_SETUP_BUILDER_DONE once applied
    markers: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.signals and not self.options


# A state lookup: entity_id -> (unit_of_measurement, attributes) or None when
# the entity does not exist (yet).
StateLookup = Callable[[str], "tuple[str | None, Mapping[str, Any]] | None"]


def _forecasted_sensors(subentries: Iterable[Any]) -> set[str]:
    """Every sensor a Load or Power Signal already forecasts."""
    out: set[str] = set()
    for sub in subentries:
        if getattr(sub, "subentry_type", None) in (
            SUBENTRY_TYPE_LOAD,
            SUBENTRY_TYPE_SIGNAL,
        ):
            sensor = (getattr(sub, "data", None) or {}).get(CONF_LOAD_SENSOR)
            if sensor:
                out.add(sensor)
    return out


def _power_problem(sensor: str, lookup: StateLookup) -> str | None:
    """Why `sensor` cannot be used as a power input, or None when it can."""
    found = lookup(sensor)
    if found is None:
        return "it does not exist (yet)"
    unit, _attrs = found
    if is_energy_unit(unit):
        return f"it reports '{unit}', an energy total, not power"
    if not is_power_unit(unit):
        return (
            "it reports no power unit"
            if not unit
            else f"it reports '{unit}', not a power unit"
        )
    return None


def _friendly(sensor: str, lookup: StateLookup) -> str:
    found = lookup(sensor)
    if found is not None:
        name = found[1].get("friendly_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return sensor


def plan_setup_fills(
    options: Mapping[str, Any],
    subentries: Iterable[Any],
    lookup: StateLookup,
) -> SetupPlan:
    """What stage 1 would create or fill on this install. Pure: no writes."""
    subentries = list(subentries)
    done = set(options.get(CONF_SETUP_BUILDER_DONE) or [])
    plan = SetupPlan()

    # --- Power Signals for sensors already named in settings ---------------
    forecasted = _forecasted_sensors(subentries)
    planned: set[str] = set()
    for key, role in _SIGNAL_SOURCES:
        sensor = options.get(key)
        if not isinstance(sensor, str) or not sensor:
            continue
        if sensor in forecasted or sensor in planned:
            continue
        marker = f"signal:{sensor}"
        if marker in done:
            continue  # created before and removed by the user: respect that
        problem = _power_problem(sensor, lookup)
        if problem is not None:
            plan.skipped.append((f"{_ROLE_LABEL[role]} forecast for {sensor}", problem))
            continue
        plan.signals.append(
            SignalToCreate(
                sensor=sensor,
                role=role,
                title=_friendly(sensor, lookup),
                because=key,
            )
        )
        plan.markers.append(marker)
        planned.add(sensor)

    # --- Solver power fields from the Forecaster's sensors -----------------
    for target, source in _SOLVER_FILLS:
        if options.get(target):
            continue
        sensor = options.get(source)
        if not isinstance(sensor, str) or not sensor:
            continue
        marker = f"option:{target}"
        if marker in done:
            continue
        problem = _power_problem(sensor, lookup)
        if problem is not None:
            plan.skipped.append((target, f"{sensor}: {problem}"))
            continue
        plan.options[target] = sensor
        plan.markers.append(marker)

    # --- whole-house cross-check from the Solver's load forecast ------------
    target = CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
    marker = f"option:{target}"
    load_fc = options.get(CONF_SOLVER_LOAD_FORECAST_SENSOR)
    if (
        not options.get(target)
        and marker not in done
        and isinstance(load_fc, str)
        and load_fc
    ):
        found = lookup(load_fc)
        source = found[1].get("source_sensor") if found is not None else None
        if not isinstance(source, str) or not source:
            plan.skipped.append((target, f"{load_fc} names no source sensor"))
        else:
            problem = _power_problem(source, lookup)
            if problem is not None:
                plan.skipped.append((target, f"{source}: {problem}"))
            else:
                plan.options[target] = source
                plan.markers.append(marker)

    return plan


def _describe(plan: SetupPlan) -> str:
    lines = [
        "Nimbus filled in setup it could derive from sensors you had already chosen:",
        "",
    ]
    labels = {
        CONF_SOLVER_BATTERY_POWER_SENSOR: "Solver battery power sensor",
        CONF_SOLVER_SOLAR_POWER_SENSOR: "Solver solar power sensor",
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: "Solver whole-house cross-check",
    }
    for sig in plan.signals:
        lines.append(
            f"- Added a **{_ROLE_LABEL[sig.role]}** forecast (Power Signal) for `{sig.sensor}`"
        )
    for key, value in plan.options.items():
        lines.append(f"- Set the {labels.get(key, key)} to `{value}`")
    if plan.skipped:
        lines += ["", "Not filled:"]
        lines += [f"- {what}: {why}" for what, why in plan.skipped]
    lines += [
        "",
        (
            "Nothing you had set was changed. Anything above can be removed or changed "
            "in Nimbus → Configure, and Nimbus will not add it back."
        ),
    ]
    return "\n".join(lines)


async def async_apply_setup_fills(hass: Any, entry: Any) -> SetupPlan:
    """Compute and apply stage 1 for `entry`. Never raises.

    Adds each Power Signal first, then writes the options (the fills plus the
    `CONF_SETUP_BUILDER_DONE` markers) in ONE update. `async_add_subentry`
    called outside a flow does not reload the hub (`services.py`'s
    `set_controllable_load` notes the same), but an options update does, via
    `__init__._async_update_listener`. So the hub reloads exactly once and the
    new signals get their entities. The next setup finds nothing to do, so it
    cannot loop.
    """

    def _lookup(entity_id: str) -> tuple[str | None, Mapping[str, Any]] | None:
        state = hass.states.get(entity_id)
        if state is None:
            return None
        attrs = dict(state.attributes)
        return attrs.get("unit_of_measurement"), attrs

    try:
        plan = plan_setup_fills(dict(entry.options), entry.subentries.values(), _lookup)
        if plan.empty:
            return plan
        from homeassistant.config_entries import ConfigSubentry

        for sig in plan.signals:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data={CONF_LOAD_SENSOR: sig.sensor, CONF_SIGNAL_ROLE: sig.role},
                    subentry_type=SUBENTRY_TYPE_SIGNAL,
                    title=sig.title,
                    unique_id=None,
                ),
            )
        new_options = dict(entry.options)
        new_options.update(plan.options)
        new_options[CONF_SETUP_BUILDER_DONE] = sorted(
            set(new_options.get(CONF_SETUP_BUILDER_DONE) or []) | set(plan.markers)
        )
        hass.config_entries.async_update_entry(entry, options=new_options)
        _LOGGER.info(
            "Nimbus setup: added %d Power Signal(s) and filled %d Solver field(s) "
            "from sensors already configured: %s",
            len(plan.signals),
            len(plan.options),
            [s.sensor for s in plan.signals] + list(plan.options.values()),
        )
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": NOTIFICATION_ID,
                "title": "Nimbus: setup completed from your sensors",
                "message": _describe(plan),
            },
            blocking=False,
        )
        return plan
    except Exception:
        _LOGGER.warning("Nimbus setup: could not fill setup gaps", exc_info=True)
        return SetupPlan()
