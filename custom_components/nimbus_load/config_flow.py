"""Config flow for Nimbus.

Two-tier structure, mirroring HAEO's own proven pattern (confirmed against
haeo_repo's own config_flow.py / flows/hub.py directly, not guessed):

  - A single "hub" config entry, created once -- there's nothing meaningful
    to configure at this level, it exists purely as the container that owns
    every load you add.
  - Any number of "load" subentries, added via the hub device page's own
    "+" button -- no repeated "Add Integration" flow, no restart, for the
    2nd through Nth load. See flows/load_subentry.py for the actual load
    configuration (sensor pickers, horizon/retrain settings).

This is what makes adding 18 separate circuit-breaker loads fast instead of
repeating full integration setup 18 times.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
    SUBENTRY_TYPE_SIGNAL,
)
from .flows.battery_tower_subentry import NimbusBatteryTowerSubentryFlowHandler
from .flows.hub_options import NimbusHubOptionsFlow
from .flows.load_subentry import NimbusLoadSubentryFlowHandler
from .flows.power_source_subentry import NimbusPowerSourceSubentryFlowHandler
from .flows.pv_string_subentry import NimbusPvStringSubentryFlowHandler
from .flows.signal_subentry import NimbusSignalSubentryFlowHandler


class NimbusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle creation of the single Nimbus hub."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Only one Nimbus hub is meaningful per HA instance -- create it
        immediately, no fields to ask for. Loads are added afterward as
        subentries on the hub's own device page."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        # Real gap found live, 2026-08-22 (a genuine third-party install,
        # not a hypothetical): a fresh install lands straight on the hub's
        # own device page with no prompt at all -- and number.py's own
        # Solver number entities (battery capacity, max charge/discharge,
        # grid import/export limits) start there at a clearly-a-placeholder
        # default (their own min bound, e.g. 0.1 kWh), with nothing on
        # screen distinguishing that from a real configured value. Nothing
        # here crashes or produces a malformed entry -- this is a silent-
        # wrong-value trap, not a broken flow: an installer who doesn't
        # notice can have the Solver running against a comically undersized
        # battery model with zero error, zero warning. A loud, hard-to-miss
        # nudge instead. Wrapped -- hub creation must succeed regardless of
        # whether this notification does.
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Nimbus: one more step",
                    "message": (
                        "Nimbus is installed, but not yet configured for your "
                        "own system. Go to **Settings > Devices & services > "
                        "Nimbus > Configure > Solver settings** now, in this "
                        "same session, and step through Battery -> Grid -> "
                        "Sources -- the Solver won't produce a meaningful plan "
                        "until this is done (right now every hardware number is "
                        "sitting at a placeholder minimum, not your real "
                        "battery/grid values).\n\n"
                        "If you restart Home Assistant before doing this, the "
                        'placeholder values become "sticky" and won\'t '
                        "auto-update from the wizard afterward -- in that case, "
                        "edit the `number.nimbus_solver_*` entities directly "
                        "instead (Settings > Devices & services > Nimbus > "
                        "entities)."
                    ),
                    "notification_id": "nimbus_setup_incomplete",
                },
            )
        except Exception:  # noqa: BLE001, S110 -- a notification failure must never block real hub setup; nothing to log or react to beyond that
            pass
        return self.async_create_entry(title="Nimbus", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Register every subentry type -- this is what puts a "+ Add"
        menu on the Nimbus hub's device page in the HA UI. Power signal
        (2026-08-15): forecasts Battery/Solar/Grid/etc directly as its
        own target, not just as a load-model input -- see flows/
        signal_subentry.py. Power Source / PV String / Battery Tower
        (2026-08-23): pure wiring/topology metadata for the topology
        dashboard card, no forecasting at all -- see const.py's own
        comment above SUBENTRY_TYPE_POWER_SOURCE."""
        return {
            SUBENTRY_TYPE_LOAD: NimbusLoadSubentryFlowHandler,
            SUBENTRY_TYPE_SIGNAL: NimbusSignalSubentryFlowHandler,
            SUBENTRY_TYPE_POWER_SOURCE: NimbusPowerSourceSubentryFlowHandler,
            SUBENTRY_TYPE_PV_STRING: NimbusPvStringSubentryFlowHandler,
            SUBENTRY_TYPE_BATTERY_TOWER: NimbusBatteryTowerSubentryFlowHandler,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The hub's own "Configure" -- shared settings (temperature
        sensors, forecast horizon, retrain hour, training window) that
        apply to every load, set once instead of re-entered per load."""
        return NimbusHubOptionsFlow(config_entry)
