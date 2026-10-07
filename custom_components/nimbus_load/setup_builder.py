"""Two-step setup, stage 1: offer to fill existing gaps from mappings the user
already confirmed (nimbus #1574; Mark's device contract on #1574: "Repair
existing gaps: reuse confirmed mappings to fix missing downstream inputs
without changing their meaning or overwriting explicit settings").

A tester's first install (#1526) had his battery and solar sensors named in
Forecaster settings, yet the Solver's own battery power and solar power fields
were empty, and so was its whole-house cross-check. The same physical sensor
had to be entered again, in another screen, for another subsystem, and nothing
said so.

This module finds those gaps and **offers** each fill as a one-click Repair
(Settings → Repairs → Submit):

| empty Solver field | offered from |
|---|---|
| battery power | the Forecaster's battery sensor |
| solar power | the Forecaster's solar sensor |
| whole-house cross-check | the source sensor of the Solver's own load forecast, when that forecast is a whole-house Power Signal |

Why an offer and not a silent fill (Mark's review of #1587, 6 Oct 2026): a
power unit proves neither the sign convention, nor the AC/DC boundary, nor
that a solar sensor is the whole site's total. Filling a live Solver input
can change later plans on an install that already dispatches, so the change
is shown before it is made -- with the sensor's current reading and the sign
setting in force -- and is made only when the household submits it. Nothing
is written without that click.

Rules (design §10):
- **Never overwrite.** Only empty fields are offered; the click re-checks.
- **Same meaning.** Each offer copies a sensor already confirmed for the same
  physical quantity; nothing is inferred or derived. An ambiguous role -- a
  load forecast that is not a whole-house Power Signal -- is not offered.
- **Never twice.** A confirmed fill is recorded under `CONF_SETUP_BUILDER_DONE`,
  so a field the user later clears is not offered again.
- **Refuse the wrong kind, visibly.** A candidate must report a power unit
  (#1562, #1570). An energy total or a unit-less sensor becomes a Repair that
  says why and clears itself once fixed. A sensor that does not exist yet is
  given `MISSING_GRACE` to appear (a later-loading integration) before it is
  reported.
- **Sign settings are never touched.** The offer states the sign the Solver
  will apply; changing it stays the household's call.
- **No control.** Nothing here touches dispatch or any device; it creates no
  Power Signal (Mark, #1574: no learned battery/grid forecasts merely because
  their power sensors exist).

`plan_setup_fills` is pure (no Home Assistant), so real installs' diagnostics
replay through it in tests (design §11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from .const import (
        ATTR_SIGNAL_ROLE,
        ATTR_SUBENTRY_TYPE,
        CONF_BATTERY_SENSOR,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        DOMAIN,
        SIGNAL_ROLE_OTHER,
        SUBENTRY_TYPE_SIGNAL,
    )
    from .power_input_check import is_energy_unit
    from .power_units import is_power_unit
except ImportError:  # pragma: no cover - standalone path
    from const import (  # type: ignore[no-redef]
        ATTR_SIGNAL_ROLE,
        ATTR_SUBENTRY_TYPE,
        CONF_BATTERY_SENSOR,
        CONF_SOLAR_SENSOR,
        CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,
        CONF_SOLVER_BATTERY_POWER_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_POWER_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        DOMAIN,
        SIGNAL_ROLE_OTHER,
        SUBENTRY_TYPE_SIGNAL,
    )
    from power_input_check import is_energy_unit  # type: ignore[no-redef]
    from power_units import is_power_unit  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Options key recording what the household has confirmed, so a field they
# later clear is never offered again. A list of "option:<key>" strings.
CONF_SETUP_BUILDER_DONE = "setup_builder_done"

# How long a configured sensor may not exist before that is reported: a
# sensor from a later-loading integration appears some time after startup.
MISSING_GRACE = timedelta(minutes=30)

FIX_FLOW_KIND = "setup_fill"
KIND_BLOCKED = "setup_fill_blocked"

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

# The translation_key of each field's offer (strings.json "issues").
_OFFER_KIND = {
    CONF_SOLVER_BATTERY_POWER_SENSOR: "setup_fill_battery_power",
    CONF_SOLVER_SOLAR_POWER_SENSOR: "setup_fill_solar_power",
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: "setup_fill_cross_check",
}

MISSING = "it does not exist (yet)"


@dataclass(frozen=True)
class Skip:
    target: str
    sensor: str
    reason: str

    @property
    def missing(self) -> bool:
        return self.reason == MISSING


@dataclass
class SetupPlan:
    # target option -> sensor it would be filled from
    options: dict[str, str] = field(default_factory=dict)
    # each fill that was considered and could NOT be offered
    skipped: list[Skip] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.options and not self.skipped


# entity_id -> (unit_of_measurement, attributes), or None when it does not exist
StateLookup = Callable[[str], "tuple[str | None, Mapping[str, Any]] | None"]


def _power_problem(sensor: str, lookup: StateLookup) -> str | None:
    """Why `sensor` cannot be used as a power input, or None when it can."""
    found = lookup(sensor)
    if found is None:
        return MISSING
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
    """What stage 1 would offer on this install. Pure: no writes."""
    done = set(options.get(CONF_SETUP_BUILDER_DONE) or [])
    plan = SetupPlan()

    for target, source in _SOLVER_FILLS:
        if options.get(target) or f"option:{target}" in done:
            continue
        sensor = options.get(source)
        if not isinstance(sensor, str) or not sensor:
            continue
        problem = _power_problem(sensor, lookup)
        if problem is not None:
            plan.skipped.append(Skip(target, sensor, problem))
            continue
        plan.options[target] = sensor

    target = CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
    load_fc = options.get(CONF_SOLVER_LOAD_FORECAST_SENSOR)
    if (
        not options.get(target)
        and f"option:{target}" not in done
        and isinstance(load_fc, str)
        and load_fc
    ):
        found = lookup(load_fc)
        attrs = found[1] if found is not None else {}
        source = attrs.get("source_sensor")
        whole_house = (
            attrs.get(ATTR_SUBENTRY_TYPE) == SUBENTRY_TYPE_SIGNAL
            and attrs.get(ATTR_SIGNAL_ROLE) == SIGNAL_ROLE_OTHER
        )
        if found is None:
            plan.skipped.append(Skip(target, load_fc, MISSING))
        elif not whole_house:
            # Ambiguous role (Mark, #1587): a single Load's forecast is one
            # circuit, not the whole house. Left unresolved, not offered.
            pass
        elif not isinstance(source, str) or not source:
            plan.skipped.append(Skip(target, load_fc, "it names no source sensor"))
        else:
            problem = _power_problem(source, lookup)
            if problem is not None:
                plan.skipped.append(Skip(target, source, problem))
            else:
                plan.options[target] = source

    return plan


def _reading(sensor: str, hass: Any) -> str:
    state = hass.states.get(sensor)
    if state is None:
        return "no reading"
    unit = state.attributes.get("unit_of_measurement") or ""
    return f"{state.state} {unit}".strip()


def _sign_note(options: Mapping[str, Any]) -> str:
    positive_is_charge = bool(options.get(CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE))
    return (
        "The Solver reads a **positive** value as **charging** "
        if positive_is_charge
        else "The Solver reads a **positive** value as **discharging** "
    ) + (
        "(Configure → Solver settings → battery sign). If this sensor reads the "
        "other way round, change that setting first; submitting here does not "
        "change it."
    )


def _issue_id(entry_id: str, target: str, blocked: bool = False) -> str:
    return f"{entry_id}_{KIND_BLOCKED if blocked else FIX_FLOW_KIND}_{target}"


async def async_refresh_setup_fills(
    hass: Any, entry: Any, *, now: datetime | None = None
) -> SetupPlan:
    """Bring this hub's setup-fill Repairs in line with the plan. Writes no
    options; never raises. Runs at startup and on every health refresh, so a
    corrected sensor is offered without a restart and a fixed one clears."""
    try:
        from homeassistant.helpers import issue_registry as ir

        now = now or datetime.now(UTC)
        store = hass.data.setdefault(DOMAIN, {}).setdefault("setup_fills", {})
        state = store.setdefault(entry.entry_id, {"ids": set(), "missing_since": {}})

        def _lookup(entity_id: str) -> tuple[str | None, Mapping[str, Any]] | None:
            found = hass.states.get(entity_id)
            if found is None:
                return None
            attrs = dict(found.attributes)
            return attrs.get("unit_of_measurement"), attrs

        options = dict(entry.options)
        plan = plan_setup_fills(options, _lookup)
        current: set[str] = set()

        for target, sensor in plan.options.items():
            issue_id = _issue_id(entry.entry_id, target)
            current.add(issue_id)
            placeholders = {
                "field": _LABELS[target],
                "sensor": sensor,
                "reading": _reading(sensor, hass),
            }
            if target == CONF_SOLVER_BATTERY_POWER_SENSOR:
                placeholders["sign"] = _sign_note(options)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_OFFER_KIND[target],
                translation_placeholders=placeholders,
                data={
                    "kind": FIX_FLOW_KIND,
                    "entry_id": entry.entry_id,
                    "target": target,
                    "sensor": sensor,
                },
            )

        missing_since = state["missing_since"]
        for skip in plan.skipped:
            if skip.missing:
                first = missing_since.setdefault(skip.target, now)
                if now - first < MISSING_GRACE:
                    continue  # may still be loading
            else:
                missing_since.pop(skip.target, None)
            issue_id = _issue_id(entry.entry_id, skip.target, blocked=True)
            current.add(issue_id)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=KIND_BLOCKED,
                translation_placeholders={
                    "field": _LABELS[skip.target],
                    "sensor": skip.sensor,
                    "reason": skip.reason,
                },
            )
        for target in list(missing_since):
            if not any(s.target == target and s.missing for s in plan.skipped):
                missing_since.pop(target)

        for stale in state["ids"] - current:
            ir.async_delete_issue(hass, DOMAIN, stale)
        state["ids"] = current
        return plan
    except Exception:
        _LOGGER.debug("Nimbus setup: setup-fill refresh failed", exc_info=True)
        return SetupPlan()


async def async_confirm_setup_fill(
    hass: Any, entry_id: str, target: str, sensor: str
) -> bool:
    """Apply one offer the household submitted. Re-checks that the field is
    still empty and the sensor still a power sensor, so a stale Repair can
    never overwrite anything. One options update, which reloads the hub."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or target not in _LABELS:
        return False
    options = dict(entry.options)
    if options.get(target):
        return False

    def _lookup(entity_id: str) -> tuple[str | None, Mapping[str, Any]] | None:
        found = hass.states.get(entity_id)
        if found is None:
            return None
        attrs = dict(found.attributes)
        return attrs.get("unit_of_measurement"), attrs

    if plan_setup_fills(options, _lookup).options.get(target) != sensor:
        return False
    options[target] = sensor
    options[CONF_SETUP_BUILDER_DONE] = sorted(
        set(options.get(CONF_SETUP_BUILDER_DONE) or []) | {f"option:{target}"}
    )
    hass.config_entries.async_update_entry(entry, options=options)
    _LOGGER.info(
        "Nimbus setup: %s set to %s, confirmed by the household",
        _LABELS[target],
        sensor,
    )
    return True
