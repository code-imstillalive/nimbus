"""Two-step setup, stage 1: repair existing gaps from mappings the user already
confirmed (nimbus #1574; Mark's device contract on #1574: "Repair existing
gaps: reuse confirmed mappings to fix missing downstream inputs without changing
their meaning or overwriting explicit settings").

A tester's first install (#1526) had his battery and solar sensors named in
Forecaster settings, yet the Solver's own battery power and solar power fields
were empty, and so was its whole-house cross-check. The same physical sensor
had to be entered again, in another screen, for another subsystem, and nothing
said so.

This module fills those downstream inputs from what is already confirmed:

| empty Solver field | filled from |
|---|---|
| battery power | the Forecaster's battery sensor |
| solar power | the Forecaster's solar sensor |
| whole-house cross-check | the source sensor of the Solver's own load forecast |

Deliberately **not** done (Mark, #1574): creating Battery, Grid or Solar
Power Signals. A Power Signal trains an independent learned forecast, and
*"planned battery dispatch and grid exchange are normally outputs of the
coordinated plan. Do not create independent learned battery/grid forecasts
merely because their power sensors exist."* Telemetry registration belongs to
the device model (later stages), not to gap-filling.

Rules (design §10):
- **Never overwrite.** Only empty fields are filled; nothing existing is edited.
- **Same meaning.** Each fill copies a sensor already confirmed for the same
  physical quantity; nothing is inferred or derived.
- **Never twice.** Each fill is recorded under `CONF_SETUP_BUILDER_DONE`, so a
  field the user later clears is not refilled.
- **Refuse the wrong kind.** A candidate must report a power unit (#1562,
  #1570); an energy total or a unit-less sensor is skipped with a reason.
- **Never silent.** One notification lists what was filled and what was not.
- **No control.** Nothing here touches dispatch or any device.

`plan_setup_fills` is pure (no Home Assistant), so real installs' diagnostics
replay through it in tests (design §11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from .const import (
        CONF_BATTERY_SENSOR,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    )
    from .power_input_check import is_energy_unit
    from .power_units import is_power_unit
except ImportError:  # pragma: no cover - standalone path
    from const import (  # type: ignore[no-redef]
        CONF_BATTERY_SENSOR,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    )
    from power_input_check import is_energy_unit  # type: ignore[no-redef]
    from power_units import is_power_unit  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Options key recording what this module has already filled, so a field the
# user later clears is never refilled. A list of "option:<key>" strings.
CONF_SETUP_BUILDER_DONE = "setup_builder_done"

NOTIFICATION_ID = "nimbus_setup_builder"

# (Solver field to fill, Forecaster field holding the same physical sensor)
_SOLVER_FILLS: tuple[tuple[str, str], ...] = (
    (CONF_SOLVER_BATTERY_POWER_SENSOR, CONF_BATTERY_SENSOR),
    (CONF_SOLVER_SOLAR_POWER_SENSOR, CONF_SOLAR_SENSOR),
)

_LABELS = {
    CONF_SOLVER_BATTERY_POWER_SENSOR: "Solver battery power sensor",
    CONF_SOLVER_SOLAR_POWER_SENSOR: "Solver solar power sensor",
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: "Solver whole-house cross-check",
}


@dataclass
class SetupPlan:
    options: dict[str, str] = field(default_factory=dict)
    # (what, why) for each fill that was considered and NOT made
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # markers to append to CONF_SETUP_BUILDER_DONE once applied
    markers: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.options


# entity_id -> (unit_of_measurement, attributes), or None when it does not exist
StateLookup = Callable[[str], "tuple[str | None, Mapping[str, Any]] | None"]


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


def plan_setup_fills(options: Mapping[str, Any], lookup: StateLookup) -> SetupPlan:
    """What stage 1 would fill on this install. Pure: no writes."""
    done = set(options.get(CONF_SETUP_BUILDER_DONE) or [])
    plan = SetupPlan()

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
            plan.skipped.append((_LABELS[target], f"{sensor}: {problem}"))
            continue
        plan.options[target] = sensor
        plan.markers.append(marker)

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
            plan.skipped.append((_LABELS[target], f"{load_fc} names no source sensor"))
        else:
            problem = _power_problem(source, lookup)
            if problem is not None:
                plan.skipped.append((_LABELS[target], f"{source}: {problem}"))
            else:
                plan.options[target] = source
                plan.markers.append(marker)

    return plan


def _describe(plan: SetupPlan) -> str:
    lines = [
        "Nimbus filled Solver inputs it could take from sensors you had already chosen:",
        "",
    ]
    lines += [f"- {_LABELS.get(k, k)}: `{v}`" for k, v in plan.options.items()]
    if plan.skipped:
        lines += ["", "Not filled:"]
        lines += [f"- {what}: {why}" for what, why in plan.skipped]
    lines += [
        "",
        (
            "Nothing you had set was changed, and nothing was sent to any device. "
            "Any of these can be changed in Nimbus → Configure → Solver settings, "
            "and Nimbus will not fill it again."
        ),
    ]
    return "\n".join(lines)


async def async_apply_setup_fills(hass: Any, entry: Any) -> SetupPlan:
    """Compute and apply stage 1 for `entry`. Never raises.

    One options update. It reloads the hub once, via
    `__init__._async_update_listener`; the next setup finds nothing left to
    fill, so it cannot loop.
    """

    def _lookup(entity_id: str) -> tuple[str | None, Mapping[str, Any]] | None:
        state = hass.states.get(entity_id)
        if state is None:
            return None
        attrs = dict(state.attributes)
        return attrs.get("unit_of_measurement"), attrs

    try:
        plan = plan_setup_fills(dict(entry.options), _lookup)
        if plan.empty:
            return plan
        new_options = dict(entry.options)
        new_options.update(plan.options)
        new_options[CONF_SETUP_BUILDER_DONE] = sorted(
            set(new_options.get(CONF_SETUP_BUILDER_DONE) or []) | set(plan.markers)
        )
        hass.config_entries.async_update_entry(entry, options=new_options)
        _LOGGER.info(
            "Nimbus setup: filled %d Solver field(s) from sensors already configured: %s",
            len(plan.options),
            plan.options,
        )
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": NOTIFICATION_ID,
                "title": "Nimbus: Solver inputs filled from your sensors",
                "message": _describe(plan),
            },
            blocking=False,
        )
        return plan
    except Exception:
        _LOGGER.warning("Nimbus setup: could not fill setup gaps", exc_info=True)
        return SetupPlan()
