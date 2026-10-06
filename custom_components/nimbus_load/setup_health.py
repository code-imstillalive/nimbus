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
- **fees counted twice on top of LocalVolts Flex Up:** from
  `pricing_autodetect` (#1564).
- **a required Solver input is empty:** battery SoC, load forecast, solar
  forecast, import price, export price.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from .const import (
        CONF_SOLVER_BATTERY_SOC_SENSOR,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_FORECAST_SENSOR,
        DOMAIN,
    )
except ImportError:  # pragma: no cover - standalone path
    from const import (  # type: ignore[no-redef]
        CONF_SOLVER_BATTERY_SOC_SENSOR,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_LOAD_FORECAST_SENSOR,
        CONF_SOLVER_SOLAR_FORECAST_SENSOR,
        DOMAIN,
    )

_LOGGER = logging.getLogger(__name__)

TRAIN_GRACE = timedelta(minutes=30)
REFRESH_INTERVAL = timedelta(minutes=15)

KIND_NOT_TRAINED = "forecast_not_trained"
KIND_ENERGY_UNIT = "energy_unit_power_input"
KIND_FEES_DOUBLED = "fees_on_top_of_flex_up"
KIND_SOLVER_INPUT = "solver_input_missing"

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
    fees_doubled: tuple[str, str] | None = None,
) -> list[HealthIssue]:
    """Every current setup gap. Pure.

    `forecasts` is (subentry_id, title, coordinator data or None) for each Load
    and Power Signal. `energy_unit_inputs` is power_input_check's
    (setting, entity_id, unit) list. `fees_doubled` is (flex_up entity, listed
    fees) from pricing_autodetect, or None.
    """
    issues: list[HealthIssue] = []

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

    missing = [label for key, label in _REQUIRED_SOLVER_INPUTS if not options.get(key)]
    if missing:
        issues.append(
            HealthIssue(
                key=KIND_SOLVER_INPUT,
                kind=KIND_SOLVER_INPUT,
                placeholders={"inputs": ", ".join(missing)},
            )
        )
    return issues


def _issue_id(entry_id: str, key: str) -> str:
    return f"{entry_id}_{key}"


async def async_refresh_health(
    hass: Any, entry: Any, *, now: datetime | None = None
) -> list[HealthIssue]:
    """Bring this hub's Repairs in line with `evaluate_health`. Never raises."""
    try:
        from homeassistant.helpers import issue_registry as ir

        from .power_input_check import find_energy_unit_inputs
        from .pricing_autodetect import detect_fees_doubled

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
            fees_doubled=detect_fees_doubled(hass, dict(entry.options)),
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
