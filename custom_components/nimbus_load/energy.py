"""Home Assistant's Energy dashboard: Nimbus's own solar forecast as a solar production forecast.

Settings -> Dashboards -> Energy -> your solar panels -> Forecast production -> Nimbus. Offered while
the solar forecaster is switched on (Configure -> Solar forecaster); expected Wh per hour, today's
earlier hours as they were forecast."""

from __future__ import annotations

from homeassistant.core import HomeAssistant


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    from . import solar_forecaster_ha

    rt = solar_forecaster_ha.runtime_for(config_entry_id)
    if rt is None or not rt.wh_hours:
        return None
    return {"wh_hours": dict(rt.wh_hours)}
