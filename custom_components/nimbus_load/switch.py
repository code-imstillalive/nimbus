"""Switch platform for Nimbus -- live, dashboard-editable Solver toggles.

2026-08-22, direct household ask, after a sharp catch: nimbus_solver_
forecast_writer.py (the sibling 116KAT-HA-AI repo's own Solver writer)
was silently including two hardcoded, known-integration solar sources
(Open-Meteo, Solcast) OUTSIDE the 3 real solar_forecast_sensor_1/2/3
config fields entirely -- dressed up as "auto-detect", but with no way
to see it happening or turn it off. "then what is the purposed of
having 3 inputs since it forces user ot autodetect... that feels
wrong." Household's own design, verbatim: "user should pick 1... or
1+2... or 1+2+3 to get their desired blend... or yes can tick auto
detect from existing... for ease... otherwise it should be just a
setting they chose."

Exactly one switch entity today -- see const.py's own comment on
CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR for the full "default False, not
True" and "these two anchors aren't equally portable" reasoning.

Same restore-and-seed-once pattern as number.py's NimbusSolverNumber
(see that module's own docstring for the full "why not entry.options"
reasoning -- a full hub reload on every dashboard toggle would be bad
UX for something this light). HA has no built-in RestoreSwitch the way
it has RestoreNumber for numbers -- SwitchEntity + RestoreEntity
together is the standard equivalent.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    DEFAULT_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Same independent sw_version read as number.py/sensor.py -- see
    # number.py's own async_setup_entry for why this isn't passed
    # between platform modules.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    async_add_entities(
        [
            NimbusSolverSwitch(
                entry,
                CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
                "Auto-Include Known Solar Integrations",
                DEFAULT_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
                sw_version,
            )
        ]
    )


class NimbusSolverSwitch(SwitchEntity, RestoreEntity):
    """One live, dashboard-editable Solver on/off toggle. See this
    module's own docstring for why these are plain restored local
    state, never written back into entry.options."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        name: str,
        default: bool,
        sw_version: str | None,
    ) -> None:
        self._entry = entry
        self._key = key
        self._default = default
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Fixed entity_id, same technique/reasoning as NimbusSolverNumber's
        # own entity_id assignment in number.py -- one of these per hub
        # per field, a fixed, predictable name is correct here.
        self.entity_id = f"switch.nimbus_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._attr_is_on = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == "on"
            return
        # No restored state -- this entity has never existed before on
        # this install. Seed from whatever's already in entry.options
        # (same convention as number.py), falling through to _default
        # (set in __init__ above) for a genuinely fresh install.
        seeded = self._entry.options.get(self._key)
        if isinstance(seeded, bool):
            self._attr_is_on = seeded

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ARG002
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ARG002
        self._attr_is_on = False
        self.async_write_ha_state()
