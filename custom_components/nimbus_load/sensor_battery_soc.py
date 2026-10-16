"""One SoC sensor per battery, on sites with more than one (household, 16 Oct
2026).

`sensor.nimbus_solver_current_soc_pct` and the plan's `forecast[].soc_pct`
are the FLEET figure: every battery's stored kWh over every battery's
capacity (nimbus #569). On Chris's site, a home battery at 92.99 % and an EV
participant at 73.0 % published ~86 %, a number no meter on the site reads,
and the Solver tab's chart drew it next to nothing it could be checked
against. The plan already carries each battery's own SoC in its `batteries`
attribute; these sensors put each one on an entity of its own, so its history
records and the chart can draw it.

Slot 0 is the home battery (the plan names it "home"); slot 1 onward are the
`battery_participant` subentries in subentry order, matched to the plan by
the participant's name, which is the name the plan gives that battery
(solver_inputs/battery_participants.py). A site with no participant gets
none of these entities, so a single-battery install's entity list, and the
Solver tab that is resolved against it, are exactly what they were.

Each sensor also carries its forward SoC (`forecast`: time, hours and
soc_pct per plan period) for the chart's "ahead" line. That list changes every solve and
is excluded from the recorder; the state is what history keeps.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from .const import (
    CONF_BATTERY_PARTICIPANT_NAME,
    SUBENTRY_TYPE_BATTERY_PARTICIPANT,
)
from .sensor_flattened import FlattenedAttrSpec, _FlattenedAttributeSensor

# The plan's own name for the home battery (solver_writer's BatteryConfig).
HOME_BATTERY_NAME = "home"
# Slots are positional; past this many the chart has no further lines.
MAX_SLOTS = 4


def battery_names(entry: Any) -> list[str]:
    """The plan's battery names for this install, home first, or [] when the
    site has only the home battery."""
    names = [
        sub.data.get(CONF_BATTERY_PARTICIPANT_NAME) or sub.subentry_id
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_BATTERY_PARTICIPANT
    ]
    if not names:
        return []
    return [HOME_BATTERY_NAME, *names][:MAX_SLOTS]


def _label(name: str) -> str:
    return "Home battery" if name == HOME_BATTERY_NAME else name


class NimbusBatterySlotSocSensor(_FlattenedAttributeSensor):
    """One battery's own SoC from the plan's `batteries` attribute."""

    _unrecorded_attributes = _FlattenedAttributeSensor._unrecorded_attributes | {
        "forecast",
        "battery_name",
    }

    def __init__(
        self, entry: Any, sw_version: str | None, slot: int, battery_name: str
    ) -> None:
        super().__init__(
            entry,
            sw_version,
            FlattenedAttrSpec(
                source_key="soc_pct",
                name=f"SoC {_label(battery_name)}",
                entity_id_suffix=f"battery_{slot}_soc_pct",
                entity_category=None,
                device_class=SensorDeviceClass.BATTERY,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement="%",
                suggested_display_precision=1,
            ),
        )
        self._battery_name = battery_name

    def update_from_parent(
        self, attributes: dict, parent_attributes: dict | None = None
    ) -> None:
        """Take this battery's row from the plan's `batteries` list. Leaves
        the previous value when the plan does not carry this battery (a
        participant the solve skipped, e.g. no capacity configured), so the
        staleness contract takes it to unavailable rather than a wrong
        number."""
        source = parent_attributes if parent_attributes is not None else attributes
        rows = _forecast_of(source, self._battery_name)
        plan = (
            source.get("forecast") if isinstance(source.get("forecast"), list) else []
        )
        if not rows or rows[0].get("soc_pct") is None:
            return
        self._extra_attrs = {
            "battery_name": self._battery_name,
            # Each row's period length from the plan's own rows (same index),
            # so the chart draws soc_pct at the period's END as the fleet line
            # does (solver_tab._forecast_at_period_end).
            "forecast": [
                {
                    "time": r.get("time"),
                    "hours": _hours(plan, i),
                    "soc_pct": r.get("soc_pct"),
                }
                for i, r in enumerate(rows)
            ],
        }
        # Sets the state and writes it once, extras included.
        super().update_from_parent({"soc_pct": rows[0]["soc_pct"]})


def _hours(plan: list[Any], i: int) -> float | None:
    row = plan[i] if i < len(plan) else None
    return row.get("hours") if isinstance(row, dict) else None


def _forecast_of(attributes: dict, name: str) -> list[dict[str, Any]]:
    for battery in attributes.get("batteries") or []:
        if isinstance(battery, dict) and battery.get("name") == name:
            rows = battery.get("forecast")
            return (
                [r for r in rows if isinstance(r, dict)]
                if isinstance(rows, list)
                else []
            )
    return []


def create(entry: Any, sw_version: str | None) -> list[NimbusBatterySlotSocSensor]:
    """The per-battery SoC sensors for this install; [] on a single-battery
    site."""
    return [
        NimbusBatterySlotSocSensor(entry, sw_version, slot, name)
        for slot, name in enumerate(battery_names(entry))
    ]
