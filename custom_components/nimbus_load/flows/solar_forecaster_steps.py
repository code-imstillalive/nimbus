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

import logging
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
_LOGGER = logging.getLogger(__name__)

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


#: An array input counts as producing when its hourly mean reached this (kW) in the lookback.
PRODUCING_KW = 0.05
PRODUCING_LOOKBACK_DAYS = 14


async def producing(hass, ids: list[str]) -> set[str]:
    """The inputs that actually made power: an hourly mean of at least PRODUCING_KW in the last
    PRODUCING_LOOKBACK_DAYS of long-term statistics; without statistics, the reading now. An MPPT
    input with no panels on it never gets there, so Find my panels leaves it unticked."""
    import datetime as dt

    from homeassistant.util import dt as dt_util

    from ..power_units import is_power_unit, power_to_kw

    def kw(eid: str, v: float) -> float:
        st = hass.states.get(eid)
        unit = st.attributes.get("unit_of_measurement") if st else None
        return power_to_kw(v, unit) if unit and is_power_unit(unit) else v

    peak: dict[str, float] = {}
    try:  # recorder imports here: the options flow stays importable without the recorder
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        end = dt_util.utcnow()
        start = end - dt.timedelta(days=PRODUCING_LOOKBACK_DAYS)
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, end, set(ids), "hour", None, {"mean"}
        )
        for eid, rs in rows.items():
            vals = [r.get("mean") for r in rs if r.get("mean") is not None]
            if vals:
                peak[eid] = kw(eid, max(vals))
    except Exception as err:  # noqa: BLE001 -- no recorder or no statistics: the reading now
        _LOGGER.debug("Find my panels: no statistics (%s); using the readings now", err)
    out = set()
    for eid in ids:
        if eid in peak:
            if peak[eid] >= PRODUCING_KW:
                out.add(eid)
            continue
        st = hass.states.get(eid)
        try:
            if st and kw(eid, float(st.state)) >= PRODUCING_KW:
                out.add(eid)
        except (TypeError, ValueError):
            pass
    return out


#: Two power sensors are the same panels when their hourly means move together this closely, over
#: at least DUPLICATE_MIN_HOURS daylight hours, at the same size (a device's sensor and an older
#: Modbus copy of it, say). Measured on the reference household: each real pair 0.999-1.000 at a
#: size ratio of 1.00-1.02, while two separate roof arrays facing the same way reach 0.993 -- so
#: the bar sits between, and a second identical array is never mistaken for a copy.
DUPLICATE_CORR = 0.998
DUPLICATE_RATIO = (0.9, 1.1)
DUPLICATE_MIN_HOURS = 24


def panels_match(a: dict[float, float], b: dict[float, float]) -> float | None:
    """How closely two sensors' hourly means {hour start: kW} track (their correlation over the
    daylight hours either reports) when they look like the same panels; None when they do not."""
    import numpy as np

    hours = sorted(set(a) & set(b))
    x = np.array([a[h] for h in hours], dtype=float)
    y = np.array([b[h] for h in hours], dtype=float)
    sun = (x > PRODUCING_KW) | (y > PRODUCING_KW)
    if sun.sum() < DUPLICATE_MIN_HOURS or x[sun].std() == 0 or y[sun].std() == 0:
        return None
    corr = float(np.corrcoef(x[sun], y[sun])[0, 1])
    ratio = float(x[sun].sum() / max(y[sun].sum(), 1e-9))
    same = corr >= DUPLICATE_CORR and DUPLICATE_RATIO[0] <= ratio <= DUPLICATE_RATIO[1]
    return corr if same else None


async def hourly_means(hass, ids: list[str]) -> dict[str, dict[float, float]]:
    """{entity: {hour start (UTC seconds): mean kW}} over the last PRODUCING_LOOKBACK_DAYS of
    long-term statistics; {} where the recorder has none."""
    import datetime as dt

    from homeassistant.util import dt as dt_util

    from ..power_units import is_power_unit, power_to_kw

    out: dict[str, dict[float, float]] = {}
    try:  # recorder imports here: the options flow stays importable without the recorder
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        end = dt_util.utcnow()
        start = end - dt.timedelta(days=PRODUCING_LOOKBACK_DAYS)
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, end, set(ids), "hour", None, {"mean"}
        )
    except Exception as err:  # noqa: BLE001 -- no recorder or no statistics
        _LOGGER.debug("Find my panels: no statistics (%s)", err)
        return out
    for eid, rs in rows.items():
        st = hass.states.get(eid)
        unit = st.attributes.get("unit_of_measurement") if st else None
        series = {}
        for r in rs:
            m, start_ = r.get("mean"), r.get("start")
            if m is None or start_ is None:
                continue
            ts = start_ if isinstance(start_, int | float) else start_.timestamp()
            series[float(ts)] = (
                power_to_kw(float(m), unit)
                if unit and is_power_unit(unit)
                else float(m)
            )
        out[eid] = series
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
        made = await producing(self.hass, [f["value"] for f in found])
        # a sensor that reads the same panels as an existing PV String (another integration's
        # copy, or per-MPPT next to per-string) is offered but never ticked: adding it would
        # count those panels twice
        strings = {
            s.data.get(CONF_PV_STRING_ENTITY): s.title
            for s in self._pv_strings()
            if s.data.get(CONF_PV_STRING_ENTITY)
        }
        means = await hourly_means(
            self.hass, [f["value"] for f in found] + list(strings)
        )
        twin = {}
        for f in found:  # the PV String it tracks most closely, if any
            scores = {
                title: panels_match(means.get(f["value"], {}), means.get(eid, {}))
                for eid, title in strings.items()
            }
            scores = {k: v for k, v in scores.items() if v is not None}
            if scores:
                twin[f["value"]] = max(scores, key=scores.get)
        for f in found:  # an input that never made power is offered, but not ticked
            if f["value"] in twin:
                f["label"] += f" — same panels as {twin[f['value']]}?"
            elif f["value"] not in made:
                f["label"] += f" — no output in the last {PRODUCING_LOOKBACK_DAYS} days"
        made -= set(twin)
        return self.async_show_form(
            step_id="solar_forecaster_find",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "arrays",
                        default=[f["value"] for f in found if f["value"] in made],
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
