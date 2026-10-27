"""Two-step setup, stage 2: setup gaps as Home Assistant Repairs
(nimbus #1574, design docs/design/two-step-setup.md §7).

A tester's first install (#1526) had six forecasts that never trained, a Wh
total used as a power sensor, and fees counted twice. Every one of those was
visible only to someone reading diagnostics or a notification they had already
dismissed. Repairs fix that: an entry under Settings → Repairs says what is
wrong and where to fix it, and **it goes away on its own once the condition
clears**, which a notification cannot do.

`evaluate_health` is pure, so a real install's state replays through it in
tests. `async_refresh_health` turns its answer into Repairs: it creates or
updates each current issue and deletes every Nimbus issue that no longer
applies. It runs at startup and every `REFRESH_INTERVAL`.

Conditions (stage 2):
- **forecast not trained:** a Load or Power Signal with no model once the hub
  has been up for `TRAIN_GRACE` (training runs at startup, so a fresh forecast
  is not reported while it is still being trained). The reason comes from the
  coordinator's own `last_retrain_error` when it has one.
- **energy sensor on a power input:** from `power_input_check` (#1562).
- **a non-power unit (A, kVA, ...) on a power input:** from `power_input_check`
  (#1643).
- **a price input frozen at forecast build** (LocalVolts Rate All Var): from
  `price_input_check` (#1661).
- **fees counted twice on top of LocalVolts Flex Up:** from
  `pricing_autodetect` (#1564).
- **a required Solver input is empty:** battery SoC, load forecast, solar
  forecast, import price, export price.
- **no regional spot forecast on an Australian-NEM install** (16 Oct 2026):
  from `pricing_autodetect.detect_missing_regional_forecast`.
- **battery or grid numbers still at their placeholder** (#1800): capacity,
  max charge, max discharge or max import still at the 0.1 floor a fresh
  install starts on (number.py). Grid max export is left out on purpose: a
  zero-export site can genuinely set it to the floor.
- **contradictory battery settings** (#1800): Min SoC at or above Max SoC, or
  a Charge By target above Max SoC.
- **an availability list that never matches** (#2014): a battery
  participant's availability entity reported states over the last
  `AVAILABILITY_WINDOW` and none of them is in the household's "States that
  mean available", so the participant reads as away on every cycle and is
  never planned. Checked only where a list is set or the entity is a plain
  sensor (see `availability.needs_listed_match`).
- **no whole-house sensor set** (#1998): the hub has a Load forecast but the
  whole-house cross-check field is blank, so no load gets the Forecaster's
  whole-house fixes (#6, #7). Since #1994 that field is the only way the
  Forecaster knows which load is the whole house.

Both read the live number entities (the Solver tab's values), found by
unique_id `<entry_id>_<key>`, never by entity_id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from .const import (
        CONF_SOLVER_BATTERY_CAPACITY_KWH,
        CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
        CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
        CONF_SOLVER_BATTERY_SOC_SENSOR,
        CONF_SOLVER_CHARGE_BY_PERCENT,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_GRID_MAX_IMPORT_KW,
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_MAX_CHARGE_KW,
        CONF_SOLVER_MAX_DISCHARGE_KW,
        CONF_SOLVER_SOLAR_FORECAST_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        DOMAIN,
        SUBENTRY_TYPE_LOAD,
    )
except ImportError:  # pragma: no cover - standalone path
    from const import (  # type: ignore[no-redef]
        CONF_SOLVER_BATTERY_CAPACITY_KWH,
        CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
        CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
        CONF_SOLVER_BATTERY_SOC_SENSOR,
        CONF_SOLVER_CHARGE_BY_PERCENT,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_GRID_MAX_IMPORT_KW,
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_MAX_CHARGE_KW,
        CONF_SOLVER_MAX_DISCHARGE_KW,
        CONF_SOLVER_SOLAR_FORECAST_SENSOR,
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
        DOMAIN,
        SUBENTRY_TYPE_LOAD,
    )

_LOGGER = logging.getLogger(__name__)

TRAIN_GRACE = timedelta(minutes=30)
REFRESH_INTERVAL = timedelta(minutes=15)
# #2014: how far back an availability entity's states are read, and how often.
# A week: a correct list for a car kept at home matches at least once in it.
AVAILABILITY_WINDOW = timedelta(days=7)
AVAILABILITY_RECHECK = timedelta(hours=6)

KIND_NOT_TRAINED = "forecast_not_trained"
KIND_ENERGY_UNIT = "energy_unit_power_input"
KIND_NON_POWER_UNIT = "non_power_unit_power_input"
KIND_FROZEN_PRICE = "frozen_price_input"
KIND_FEES_DOUBLED = "fees_on_top_of_flex_up"
KIND_FEES_ON_TARIFF = "fees_on_top_of_pd7day_tariff"
KIND_SOLVER_INPUT = "solver_input_missing"
KIND_NO_REGIONAL_FORECAST = "regional_forecast_missing"
KIND_WHOLE_HOUSE_UNSET = "whole_house_sensor_unset"
KIND_AVAILABILITY_MISMATCH = "availability_states_never_match"
KIND_PLACEHOLDER_SETTINGS = "placeholder_settings"
KIND_CONTRADICTORY_SETTINGS = "contradictory_settings"
# Nimbus could not make a plan for PLAN_FAILING_AFTER cycles in a row
# (household, 22 Oct 2026): the solver gave up, or proved no plan meets the
# settings. One Repair, worded by which; it clears on the next good plan.
KIND_PLAN_FAILING = "plan_failing"
KIND_PLAN_FAILING_ERROR = "plan_failing_error"
KIND_PLAN_FAILING_INFEASIBLE = "plan_failing_infeasible"
#: Five one-minute cycles: when the battery forecast also turns unavailable
#: on a solver that keeps giving up (solver_publish, #757).
PLAN_FAILING_AFTER = 5

# nimbus #1800: number.py's floor for these, which a fresh install starts on.
PLACEHOLDER_FLOOR = 0.1
# (key, what the household sees, where it is on the Solver tab).
_PLACEHOLDER_SETTINGS: tuple[tuple[str, str, str], ...] = (
    (CONF_SOLVER_BATTERY_CAPACITY_KWH, "Battery capacity", "Battery card"),
    (CONF_SOLVER_MAX_CHARGE_KW, "Max charge power", "Battery card"),
    (CONF_SOLVER_MAX_DISCHARGE_KW, "Max discharge power", "Battery card"),
    (CONF_SOLVER_GRID_MAX_IMPORT_KW, "Grid max import", "Grid and fees card"),
)
# Every number the two checks read, for async_refresh_health.
SETTING_KEYS: tuple[str, ...] = (
    *(key for key, _label, _where in _PLACEHOLDER_SETTINGS),
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_CHARGE_BY_PERCENT,
)

# Required Solver inputs and the name the household sees for each.
_REQUIRED_SOLVER_INPUTS: tuple[tuple[str, str], ...] = (
    (CONF_SOLVER_BATTERY_SOC_SENSOR, "battery state of charge"),
    (CONF_SOLVER_LOAD_FORECAST_SENSOR, "household load forecast"),
    (CONF_SOLVER_SOLAR_FORECAST_SENSOR, "solar forecast"),
    (CONF_SOLVER_IMPORT_PRICE_SENSOR, "import price"),
    (CONF_SOLVER_EXPORT_PRICE_SENSOR, "export price"),
)


@dataclass(frozen=True)
class HealthIssue:
    """One Repair. `key` is unique within the hub (forms the issue_id)."""

    key: str
    kind: str  # the translation_key
    placeholders: Mapping[str, str] = field(default_factory=dict)


def evaluate_health(
    *,
    options: Mapping[str, Any],
    forecasts: Iterable[tuple[str, str, Mapping[str, Any] | None]],
    hub_up_for: timedelta,
    energy_unit_inputs: Iterable[tuple[str, str, str]] = (),
    non_power_unit_inputs: Iterable[tuple[str, str, str]] = (),
    frozen_price_inputs: Iterable[tuple[str, str, str]] = (),
    fees_doubled: tuple[str, str] | None = None,
    fees_on_tariff: tuple[str, str] | None = None,
    regional_forecast_missing: tuple[str, str] | None = None,
    settings: Mapping[str, float] | None = None,
    has_load_forecast: bool = False,
    failed_streak: Mapping[str, Any] | None = None,
    availability_mismatches: Iterable[tuple[str, str, str, tuple[str, ...], str]] = (),
) -> list[HealthIssue]:
    """Every current setup gap. Pure.

    `forecasts` is (subentry_id, title, coordinator data or None) for each Load
    and Power Signal. `energy_unit_inputs` is power_input_check's
    (setting, entity_id, unit) list. `fees_doubled` is (flex_up entity, listed
    fees) from pricing_autodetect, or None; `fees_on_tariff` the same for a
    NEM PD7DAY import tariff (#1581). `settings` is the live value of each
    `SETTING_KEYS` number that exists; a missing one is not checked (#1800).
    `failed_streak` is solver_publish.failed_streak(): consecutive cycles
    without an optimal plan. `availability_mismatches` is (subentry_id,
    participant name, availability entity, states it reported, the configured
    list) for each participant whose list matched none of them (#2014).
    """
    issues: list[HealthIssue] = []

    for subentry_id, name, entity_id, seen, listed in availability_mismatches:
        issues.append(
            HealthIssue(
                key=f"{KIND_AVAILABILITY_MISMATCH}_{subentry_id}",
                kind=KIND_AVAILABILITY_MISMATCH,
                placeholders={
                    "name": name,
                    "entity": entity_id,
                    "seen": ", ".join(seen),
                    "listed": listed or "(none)",
                    "days": str(AVAILABILITY_WINDOW.days),
                },
            )
        )

    if hub_up_for >= TRAIN_GRACE:
        for subentry_id, title, data in forecasts:
            data = data or {}
            if (
                data.get("trained_at")
                or data.get("forecast")
                or data.get("forecast_readiness") == "ready"
            ):
                continue  # trained, or a deterministic load that needs no model
            reason = data.get("last_retrain_error")
            if not reason and data.get("forecast_readiness") == "incomplete_rule":
                # nimbus #1575: an expected power without both schedule hours
                # is a configuration gap, not missing history.
                reason = (
                    "it has an expected power but not both schedule hours, so "
                    "its fixed-hours rule cannot run; set the start and end "
                    "hours, or remove the expected power to let it learn"
                )
            issues.append(
                HealthIssue(
                    key=f"{KIND_NOT_TRAINED}_{subentry_id}",
                    kind=KIND_NOT_TRAINED,
                    placeholders={
                        "name": title,
                        "reason": str(reason).strip()
                        if reason
                        else "it has not found enough usable history yet",
                    },
                )
            )

    frozen = list(frozen_price_inputs)
    if frozen:
        issues.append(
            HealthIssue(
                key=KIND_FROZEN_PRICE,
                kind=KIND_FROZEN_PRICE,
                placeholders={
                    "inputs": "\n".join(
                        f"- {label}: `{eid}` ({why})" for label, eid, why in frozen
                    )
                },
            )
        )

    non_power = list(non_power_unit_inputs)
    if non_power:
        issues.append(
            HealthIssue(
                key=KIND_NON_POWER_UNIT,
                kind=KIND_NON_POWER_UNIT,
                placeholders={
                    "inputs": "\n".join(
                        f"- {label}: `{eid}` reports {unit}"
                        for label, eid, unit in non_power
                    )
                },
            )
        )

    energy = list(energy_unit_inputs)
    if energy:
        issues.append(
            HealthIssue(
                key=KIND_ENERGY_UNIT,
                kind=KIND_ENERGY_UNIT,
                placeholders={
                    "inputs": "\n".join(
                        f"- {label}: `{eid}` reports {unit}"
                        for label, eid, unit in energy
                    )
                },
            )
        )

    if fees_doubled is not None:
        flex_up, listed = fees_doubled
        issues.append(
            HealthIssue(
                key=KIND_FEES_DOUBLED,
                kind=KIND_FEES_DOUBLED,
                placeholders={"flex_up": flex_up, "fees": listed},
            )
        )

    if fees_on_tariff is not None:
        tariff, listed = fees_on_tariff
        issues.append(
            HealthIssue(
                key=KIND_FEES_ON_TARIFF,
                kind=KIND_FEES_ON_TARIFF,
                placeholders={"tariff": tariff, "fees": listed},
            )
        )

    if regional_forecast_missing is not None:
        found, where = regional_forecast_missing
        issues.append(
            HealthIssue(
                key=KIND_NO_REGIONAL_FORECAST,
                kind=KIND_NO_REGIONAL_FORECAST,
                placeholders={"found": found, "where": where},
            )
        )

    issues += _settings_issues(settings or {})

    if has_load_forecast and not options.get(
        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
    ):
        issues.append(
            HealthIssue(key=KIND_WHOLE_HOUSE_UNSET, kind=KIND_WHOLE_HOUSE_UNSET)
        )

    missing = [label for key, label in _REQUIRED_SOLVER_INPUTS if not options.get(key)]
    if missing:
        issues.append(
            HealthIssue(
                key=KIND_SOLVER_INPUT,
                kind=KIND_SOLVER_INPUT,
                placeholders={"inputs": ", ".join(missing)},
            )
        )

    plan_failing = _plan_failing_issue(failed_streak)
    if plan_failing is not None:
        issues.append(plan_failing)
    return issues


def _plan_failing_issue(streak: Mapping[str, Any] | None) -> HealthIssue | None:
    """The Repair for a solver that has not made a plan for
    PLAN_FAILING_AFTER cycles in a row, worded by why."""
    count = int((streak or {}).get("count") or 0)
    if count < PLAN_FAILING_AFTER:
        return None
    since = str((streak or {}).get("since") or "")
    try:
        since = datetime.fromisoformat(since).strftime("%H:%M")
    except ValueError:
        pass
    infeasible = (streak or {}).get("status") == "infeasible"
    placeholders = {
        "cycles": str(count),
        "since": since or "unknown",
        "reason": str((streak or {}).get("raw_status") or "not reported"),
    }
    if infeasible:
        placeholders["causes"] = _infeasible_causes_text((streak or {}).get("causes"))
    return HealthIssue(
        key=KIND_PLAN_FAILING,
        kind=KIND_PLAN_FAILING_INFEASIBLE if infeasible else KIND_PLAN_FAILING_ERROR,
        placeholders=placeholders,
    )


#: The Repair's advice when no diagnosis has run yet.
_CAUSES_UNKNOWN = (
    "Check for a recent change that cannot all be met at once: battery Min or "
    "Max SoC, charge or discharge limits, grid import or export limits, or a "
    "load that must run within a window."
)


def _infeasible_causes_text(causes: Any) -> str:
    """nimbus #2127: the settings the solver's diagnosis named, each of which
    on its own makes a plan possible when relaxed."""
    if causes is None:
        return _CAUSES_UNKNOWN
    if not causes:
        return (
            "Nimbus re-planned with each setting relaxed on its own, and none "
            "alone makes a plan possible: more than one setting conflicts. "
            + _CAUSES_UNKNOWN
        )
    return (
        "Nimbus re-planned with each setting relaxed on its own. A plan becomes "
        "possible when any one of these is relaxed: "
        + "; ".join(f"**{c}**" for c in causes)
        + ". Check which of them changed recently."
    )


def _solver_publish_module() -> Any:
    """The solver_publish the running solver loaded, or None: importing it
    here would load the whole solver on the event loop."""
    import sys

    return sys.modules.get(f"{__package__}.solver_publish") or sys.modules.get(
        "solver_publish"
    )


def _solver_failed_streak() -> Mapping[str, Any] | None:
    """solver_publish's streak, only if the solver has already loaded it."""
    module = _solver_publish_module()
    try:
        return module.failed_streak() if module is not None else None
    except Exception:  # noqa: BLE001 - a health read must never fail the refresh
        return None


async def async_note_solve_outcome(hass: Any, entry: Any) -> None:
    """After each solve: refresh the Repairs at once when the "cannot make a
    plan" state flips, instead of waiting up to REFRESH_INTERVAL. Never
    raises."""
    try:
        failing = _plan_failing_issue(_solver_failed_streak()) is not None
        store = hass.data.setdefault(DOMAIN, {}).setdefault("setup_health", {})
        key = f"{entry.entry_id}:plan_failing"
        if store.get(key, False) != failing:
            store[key] = failing
            await async_refresh_health(hass, entry)
    except Exception:
        _LOGGER.debug("Nimbus: plan-failing check failed", exc_info=True)


def _settings_issues(settings: Mapping[str, float]) -> list[HealthIssue]:
    """nimbus #1800: placeholder and contradictory battery / grid numbers."""
    out: list[HealthIssue] = []
    placeholders = [
        f"- {label} ({where}) is {settings[key]:g}"
        for key, label, where in _PLACEHOLDER_SETTINGS
        if key in settings and settings[key] <= PLACEHOLDER_FLOOR
    ]
    if placeholders:
        out.append(
            HealthIssue(
                key=KIND_PLACEHOLDER_SETTINGS,
                kind=KIND_PLACEHOLDER_SETTINGS,
                placeholders={"settings": "\n".join(placeholders)},
            )
        )

    found: list[str] = []
    low = settings.get(CONF_SOLVER_BATTERY_MIN_SOC_PERCENT)
    high = settings.get(CONF_SOLVER_BATTERY_MAX_SOC_PERCENT)
    if low is not None and high is not None and low >= high:
        found.append(
            f"- Min SoC ({low:g}%) is not below Max SoC ({high:g}%): the "
            "battery has no room to work in."
        )
    target = settings.get(CONF_SOLVER_CHARGE_BY_PERCENT)
    if target is not None and high is not None and 0 < high < target:
        found.append(
            f"- Charge By's target ({target:g}%) is above Max SoC ({high:g}%): "
            "it can never be reached."
        )
    if found:
        out.append(
            HealthIssue(
                key=KIND_CONTRADICTORY_SETTINGS,
                kind=KIND_CONTRADICTORY_SETTINGS,
                placeholders={"settings": "\n".join(found)},
            )
        )
    return out


def _live_settings(hass: Any, entry: Any) -> dict[str, float]:
    """The live value of each SETTING_KEYS number, found by unique_id. Empty
    when they cannot be read: the other checks still run."""
    out: dict[str, float] = {}
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        for key in SETTING_KEYS:
            entity_id = registry.async_get_entity_id(
                "number", DOMAIN, f"{entry.entry_id}_{key}"
            )
            state = hass.states.get(entity_id) if entity_id else None
            try:
                out[key] = float(state.state)
            except (AttributeError, TypeError, ValueError):
                continue  # absent, unknown or unavailable: not checked
    except Exception:  # the settings checks are skipped, nothing else
        _LOGGER.debug("Nimbus #1800: settings unreadable", exc_info=True)
        return {}
    return out


async def _async_availability_mismatches(
    hass: Any, entry: Any, state: dict[str, Any], now: datetime
) -> list[tuple[str, str, str, tuple[str, ...], str]]:
    """#2014: each battery participant whose availability list matched none of
    the states its entity reported over `AVAILABILITY_WINDOW`. Read from the
    recorder every `AVAILABILITY_RECHECK`; only that read is kept between
    refreshes, and the lists are matched against it on every refresh, so an
    edited list clears its Repair at once (Mark Purcell's review of #2055).
    An entity with no history yet is not reported."""
    try:
        from .availability import (
            available_states,
            needs_listed_match,
            unmatched_states,
        )
        from .const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_AVAILABLE_STATES,
            CONF_BATTERY_PARTICIPANT_NAME,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )

        checks = []
        for subentry_id, sub in entry.subentries.items():
            if getattr(sub, "subentry_type", None) != SUBENTRY_TYPE_BATTERY_PARTICIPANT:
                continue
            data = dict(sub.data)
            entity_id = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
            listed = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_STATES) or ""
            if entity_id and needs_listed_match(entity_id, listed):
                name = data.get(CONF_BATTERY_PARTICIPANT_NAME) or sub.title
                checks.append((subentry_id, str(name), entity_id, listed))
        found: list[tuple[str, str, str, tuple[str, ...], str]] = []
        if checks:
            entity_ids = tuple(sorted({c[2] for c in checks}))
            cached = state.get("availability")
            if (
                cached is not None
                and cached[1] == entity_ids
                and now - cached[0] < AVAILABILITY_RECHECK
            ):
                seen = cached[2]
            else:
                seen = await _async_states_seen(
                    hass, list(entity_ids), now - AVAILABILITY_WINDOW, now
                )
                state["availability"] = (now, entity_ids, seen)
            for subentry_id, name, entity_id, listed in checks:
                unmatched = unmatched_states(
                    seen.get(entity_id, ()), available_states(entity_id, listed)
                )
                if unmatched:
                    found.append((subentry_id, name, entity_id, unmatched, listed))
        return found
    except Exception:
        _LOGGER.debug("Nimbus: availability check failed", exc_info=True)
        return []


async def _async_states_seen(
    hass: Any, entity_ids: list[str], start: datetime, end: datetime
) -> dict[str, list[Any]]:
    from homeassistant.components.recorder import get_instance, history

    def _read() -> dict[str, list[Any]]:
        return history.get_significant_states(
            hass,
            start,
            end,
            entity_ids=entity_ids,
            significant_changes_only=False,
            no_attributes=True,
        )

    raw = await get_instance(hass).async_add_executor_job(_read)
    return {
        eid: [getattr(st, "state", None) for st in states]
        for eid, states in raw.items()
    }


def _issue_id(entry_id: str, key: str) -> str:
    return f"{entry_id}_{key}"


async def async_refresh_health(
    hass: Any, entry: Any, *, now: datetime | None = None
) -> list[HealthIssue]:
    """Bring this hub's Repairs in line with `evaluate_health`. Never raises."""
    try:
        from homeassistant.helpers import issue_registry as ir

        from .power_input_check import (
            find_energy_unit_inputs,
            find_non_power_unit_inputs,
        )
        from .price_input_check import find_frozen_price_inputs
        from .pricing_autodetect import (
            detect_fees_doubled,
            detect_fees_on_pd7day_tariff,
            detect_missing_regional_forecast,
        )

        store = hass.data.setdefault(DOMAIN, {}).setdefault("setup_health", {})
        state = store.setdefault(
            entry.entry_id, {"since": now or datetime.now(UTC), "ids": set()}
        )
        up_for = (now or datetime.now(UTC)) - state["since"]

        coordinators = getattr(entry, "runtime_data", None) or {}
        forecasts = []
        for subentry_id, coordinator in coordinators.items():
            sub = entry.subentries.get(subentry_id)
            title = getattr(sub, "title", None) or subentry_id
            forecasts.append((subentry_id, title, getattr(coordinator, "data", None)))

        issues = evaluate_health(
            options=dict(entry.options),
            forecasts=forecasts,
            hub_up_for=up_for,
            energy_unit_inputs=find_energy_unit_inputs(hass, entry),
            non_power_unit_inputs=find_non_power_unit_inputs(hass, entry),
            frozen_price_inputs=find_frozen_price_inputs(hass, dict(entry.options)),
            fees_doubled=detect_fees_doubled(hass, dict(entry.options)),
            fees_on_tariff=detect_fees_on_pd7day_tariff(hass, dict(entry.options)),
            regional_forecast_missing=detect_missing_regional_forecast(
                hass, dict(entry.options)
            ),
            settings=_live_settings(hass, entry),
            has_load_forecast=any(
                getattr(sub, "subentry_type", None) == SUBENTRY_TYPE_LOAD
                for sub in entry.subentries.values()
            ),
            failed_streak=_solver_failed_streak(),
            availability_mismatches=await _async_availability_mismatches(
                hass, entry, state, now or datetime.now(UTC)
            ),
        )

        current = set()
        for issue in issues:
            issue_id = _issue_id(entry.entry_id, issue.key)
            current.add(issue_id)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue.kind,
                translation_placeholders=dict(issue.placeholders),
            )
        for stale in state["ids"] - current:
            ir.async_delete_issue(hass, DOMAIN, stale)
        state["ids"] = current
        return issues
    except Exception:
        _LOGGER.debug("Nimbus: setup health refresh failed", exc_info=True)
        return []
