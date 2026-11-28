"""Configure -> Solar forecaster: switch Nimbus's own solar forecaster on, choose what the plan does
with it, and find the panels (PV Strings) it learns and forecasts.

A mixin for the hub's options flow (like DeviceSetupSteps): two steps.

- `solar_forecaster`: on/off, how the Solver uses it (forecast only / blend / replace), days of
  weather history to start from; the PV Strings it will use are listed, with their tilt and
  direction or "learned".
- `solar_forecaster_find`: per-string or per-MPPT power sensors found on this Home Assistant,
  pre-ticked, from the inverter's own integration (not template copies); the ticked ones become
  PV Strings. Nothing is created without the household's tick.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..const import (
    CONF_PV_STRING_AZIMUTH,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_TILT,
    CONF_SOLAR_FORECASTER_ENABLED,
    CONF_SOLAR_FORECASTER_HISTORY_DAYS,
    CONF_SOLAR_FORECASTER_USE,
    DEFAULT_SOLAR_FORECASTER_ENABLED,
    DEFAULT_SOLAR_FORECASTER_HISTORY_DAYS,
    DEFAULT_SOLAR_FORECASTER_USE,
    SOLAR_FORECASTER_USES,
    SUBENTRY_TYPE_PV_STRING,
)

# Names that mark a per-array power sensor, and names that mark everything else that is in kW.
_ARRAY_WORDS = re.compile(
    r"mppt|string|\bpv\d*\b|_pv_|pv_|solar|array|panel|dc_power|dc power", re.IGNORECASE
)
_NOT_ARRAY = re.compile(
    r"forecast|nimbus|battery|grid|load|house|export|import|consumption|temp|voltage|current|energy|daily|"
    r"total|limit|setpoint|reactive|apparent|meter|backup|charger|ev_|rated|nominal|max|min",
    re.IGNORECASE,
)


def find_array_sensors(hass, taken: set[str]) -> list[dict[str, str]]:
    """Per-MPPT (else per-string) power sensors, from the one integration that has most of them.
    A device's own integration wins over template copies. `taken` = entities already PV Strings."""
    reg = er.async_get(hass)
    fam: dict[str, list[str]] = {}
    for st in hass.states.async_all("sensor"):
        a = st.attributes
        if not (
            a.get("device_class") == "power"
            or a.get("unit_of_measurement") in ("W", "kW", "MW")
        ):
            continue
        name = f"{st.entity_id} {a.get('friendly_name', '')}"
        if not _ARRAY_WORDS.search(name) or _NOT_ARRAY.search(name):
            continue
        entry = reg.async_get(st.entity_id)
        fam.setdefault(entry.platform if entry else "template", []).append(st.entity_id)
    if not fam:
        return []

    def score(item):
        platform, ids = item
        return (
            platform != "template",
            sum("mppt" in i for i in ids),
            sum("string" in i for i in ids),
        )

    _, ids = max(fam.items(), key=score)
    per = [i for i in ids if "mppt" in i] or [i for i in ids if "string" in i] or ids
    out = []
    for eid in sorted(per):
        if eid in taken:
            continue
        st = hass.states.get(eid)
        out.append(
            {
                "value": eid,
                "label": f"{st.attributes.get('friendly_name', eid)} ({eid})",
            }
        )
    return out


class SolarForecasterSteps:
    """Options-flow steps; mixed into the hub's options flow handler."""

    def _pv_strings(self) -> list[Any]:
        return [
            s
            for s in self.config_entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_PV_STRING
        ]

    def _strings_summary(self) -> str:
        rows = []
        for s in self._pv_strings():
            tilt, az = (
                s.data.get(CONF_PV_STRING_TILT),
                s.data.get(CONF_PV_STRING_AZIMUTH),
            )
            where = (
                f"{tilt:g}° tilt, facing {az:g}°"
                if tilt is not None and az is not None
                else "direction learned"
            )
            rows.append(f"- {s.title}: {where}")
        return "\n".join(rows) if rows else "- none yet"

    def _solar_forecaster_schema(self, o: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_SOLAR_FORECASTER_ENABLED,
                    default=bool(
                        o.get(
                            CONF_SOLAR_FORECASTER_ENABLED,
                            DEFAULT_SOLAR_FORECASTER_ENABLED,
                        )
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SOLAR_FORECASTER_USE,
                    default=o.get(
                        CONF_SOLAR_FORECASTER_USE, DEFAULT_SOLAR_FORECASTER_USE
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SOLAR_FORECASTER_USES),
                        translation_key="solar_forecaster_use",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_SOLAR_FORECASTER_HISTORY_DAYS,
                    default=int(
                        o.get(
                            CONF_SOLAR_FORECASTER_HISTORY_DAYS,
                            DEFAULT_SOLAR_FORECASTER_HISTORY_DAYS,
                        )
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=7,
                        max=60,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                vol.Optional(
                    "find_panels", default=not self._pv_strings()
                ): selector.BooleanSelector(),
            }
        )

    async def async_step_solar_forecaster(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            find = bool(user_input.pop("find_panels", False))
            self._solar_pending = dict(user_input)
            self._solar_pending[CONF_SOLAR_FORECASTER_HISTORY_DAYS] = int(
                user_input[CONF_SOLAR_FORECASTER_HISTORY_DAYS]
            )
            if find:
                return await self.async_step_solar_forecaster_find()
            return self._solar_save()
        return self.async_show_form(
            step_id="solar_forecaster",
            data_schema=self._solar_forecaster_schema(dict(self.config_entry.options)),
            description_placeholders={"strings": self._strings_summary()},
        )

    async def async_step_solar_forecaster_find(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        taken = {s.data.get(CONF_PV_STRING_ENTITY) for s in self._pv_strings()}
        found = find_array_sensors(self.hass, taken)
        if user_input is not None:
            for eid in user_input.get("arrays", []):
                st = self.hass.states.get(eid)
                title = st.attributes.get("friendly_name", eid) if st else eid
                self.hass.config_entries.async_add_subentry(
                    self.config_entry,
                    ConfigSubentry(
                        data={CONF_PV_STRING_ENTITY: eid, CONF_PV_STRING_LABEL: ""},
                        subentry_type=SUBENTRY_TYPE_PV_STRING,
                        title=title,
                        unique_id=None,
                    ),
                )
            return self._solar_save()
        if not found:
            return self.async_show_form(
                step_id="solar_forecaster_find",
                data_schema=vol.Schema({}),
                errors={"base": "no_arrays_found"},
                description_placeholders={"count": "0"},
            )
        return self.async_show_form(
            step_id="solar_forecaster_find",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "arrays", default=[f["value"] for f in found]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=found,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(found))},
        )

    def _solar_save(self) -> Any:
        merged = dict(self.config_entry.options)
        merged.update(getattr(self, "_solar_pending", {}) or {})
        return self.async_create_entry(title="", data=merged)
