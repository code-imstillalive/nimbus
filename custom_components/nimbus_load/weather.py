"""weather.nimbus_weather: the Solar Lab weather strip as a standard Home Assistant weather entity.

It exists while Nimbus's own solar forecaster is switched on (Configure -> Solar forecaster) and
reads the same ECMWF weather the forecaster already downloads (no extra download, no account):
condition, cloud, temperature and rain now, a forecast every 3 hours ("hourly", ECMWF's own step)
and one per local day. So the standard weather card and automations ("thunderstorms in the next
few hours") work from it. Storm energy (CAPE) and shower rain stay on sensor.nimbus_solar_forecast's
`weather` attribute, where the Solar Lab card reads them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfPrecipitationDepth, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import DOMAIN

#: The strip's pictures (solar_forecaster/weather.py) as Home Assistant's own conditions.
CONDITION = {
    "clear": "sunny",
    "clear_night": "clear-night",
    "mostly_sunny": "partlycloudy",
    "partly_cloudy": "partlycloudy",
    "partly_cloudy_night": "partlycloudy",
    "cloudy": "cloudy",
    "sprinkle": "rainy",
    "showers_sun": "rainy",
    "showers_night": "rainy",
    "rain": "rainy",
    "heavy_rain": "pouring",
    "storm": "lightning-rainy",
}
#: How bad each condition is: a day's condition is its worst daytime one (the strip's own rule,
#: so a storm afternoon is never hidden behind a sunny morning).
SEVERITY = {
    "clear-night": 0,
    "sunny": 0,
    "partlycloudy": 1,
    "cloudy": 2,
    "rainy": 3,
    "pouring": 4,
    "lightning-rainy": 5,
}
STEP_S = 3 * 3600  # one ECMWF interval


def _entries(weather: list[dict]) -> list[dict]:
    """The strip's entries with their interval start and end (UTC seconds)."""
    out = []
    for e in weather or []:
        try:
            mid = dt.datetime.fromisoformat(e["time"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        out.append({**e, "start": mid - STEP_S / 2, "end": mid + STEP_S / 2})
    return out


def now_entry(weather: list[dict], now: float) -> dict | None:
    """The interval covering `now` (None when the weather does not reach it)."""
    for e in _entries(weather):
        if e["start"] <= now < e["end"]:
            return e
    return None


def hourly(weather: list[dict], now: float) -> list[Forecast]:
    """One forecast per 3-hour interval, from the one covering now onward."""
    out: list[Forecast] = []
    for e in _entries(weather):
        if e["end"] <= now:
            continue
        f: Forecast = {
            "datetime": dt_util.utc_from_timestamp(e["start"]).isoformat(),
            "condition": CONDITION.get(e.get("sky"), "cloudy"),
            "cloud_coverage": e.get("cloud"),
            "native_temperature": e.get("temperature"),
            # mm in the interval (the strip carries mm per hour)
            "native_precipitation": None
            if e.get("rain") is None
            else round(e["rain"] * STEP_S / 3600, 1),
        }
        out.append(f)
    return out


def daily(weather: list[dict], now: float, tz) -> list[Forecast]:
    """One forecast per local day from today: its worst daytime condition, its highest and lowest
    temperature, its rain in mm and its average cloud."""
    days: dict[dt.date, list[dict]] = {}
    today = dt.datetime.fromtimestamp(now, tz).date()
    for e in _entries(weather):
        d = dt.datetime.fromtimestamp(e["start"] + STEP_S / 2, tz).date()
        if d >= today:
            days.setdefault(d, []).append(e)
    out: list[Forecast] = []
    for d, es in sorted(days.items()):
        if len(es) < 4:  # the horizon's cut-off last day
            continue
        lit = [e for e in es if e.get("sun_up")] or es
        conds = [CONDITION.get(e.get("sky"), "cloudy") for e in lit]
        worst = max(conds, key=lambda c: SEVERITY.get(c, 0))
        temps = [e["temperature"] for e in es if e.get("temperature") is not None]
        rain = [e["rain"] for e in es if e.get("rain") is not None]
        cloud = [e["cloud"] for e in es if e.get("cloud") is not None]
        out.append(
            {
                "datetime": dt.datetime.combine(d, dt.time(), tz).isoformat(),
                "condition": worst,
                "native_temperature": max(temps) if temps else None,
                "native_templow": min(temps) if temps else None,
                "native_precipitation": round(sum(rain) * STEP_S / 3600, 1)
                if rain
                else None,
                "cloud_coverage": round(sum(cloud) / len(cloud)) if cloud else None,
            }
        )
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    from .const import CONF_SOLAR_FORECASTER_ENABLED

    if not entry.options.get(CONF_SOLAR_FORECASTER_ENABLED):
        return
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    async_add_entities([NimbusWeather(entry, sw_version)])


class NimbusWeather(WeatherEntity):
    """The forecaster's ECMWF weather: now, every 3 hours and per day."""

    _attr_has_entity_name = True
    _attr_translation_key = "weather"
    _attr_should_poll = False
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    @property
    def attribution(self) -> str:
        from .solar_forecaster import sources

        return sources.SPECS["ecmwf"].attribution

    def __init__(self, entry, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self.entity_id = "weather.nimbus_weather"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )

    async def async_added_to_hass(self) -> None:
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        from . import solar_forecaster_ha

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                solar_forecaster_ha.SIGNAL_UPDATED.format(self._entry.entry_id),
                self._updated,
            )
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(("hourly", "daily")))

    def _weather(self) -> list[dict]:
        from . import solar_forecaster_ha

        rt = solar_forecaster_ha.runtime_for(self._entry.entry_id)
        return list(rt.weather) if rt is not None else []

    def _now(self) -> dict | None:
        return now_entry(self._weather(), dt_util.utcnow().timestamp())

    @property
    def available(self) -> bool:
        return self._now() is not None

    @property
    def condition(self) -> str | None:
        e = self._now()
        return CONDITION.get(e.get("sky"), "cloudy") if e else None

    @property
    def native_temperature(self) -> float | None:
        e = self._now()
        return e.get("temperature") if e else None

    @property
    def cloud_coverage(self) -> float | None:
        e = self._now()
        return e.get("cloud") if e else None

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        return hourly(self._weather(), dt_util.utcnow().timestamp())

    async def async_forecast_daily(self) -> list[Forecast] | None:
        return daily(
            self._weather(),
            dt_util.utcnow().timestamp(),
            dt_util.get_time_zone(self.hass.config.time_zone),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        e = self._now()
        if not e:
            return {}
        return {
            "rain_rate": e.get("rain"),  # mm/h, this 3-hour interval
            "shower_rate": e.get("shower"),
            "storm_energy": e.get("cape"),  # CAPE, J/kg
        }
