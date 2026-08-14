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

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, ConfigSubentryFlow
from homeassistant.core import callback

from .const import DOMAIN, SUBENTRY_TYPE_LOAD
from .flows.load_subentry import NimbusLoadSubentryFlowHandler


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
        return self.async_create_entry(title="Nimbus", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Register the "load" subentry type -- this is what puts a "+ Add"
        button on the Nimbus hub's device page in the HA UI."""
        return {SUBENTRY_TYPE_LOAD: NimbusLoadSubentryFlowHandler}
