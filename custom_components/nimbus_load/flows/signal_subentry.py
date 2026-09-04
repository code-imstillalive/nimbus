"""Power-signal subentry flow -- forecasts a real measured power signal
(Battery, Solar, Grid, or anything else) directly, as its own genuine
Nimbus forecast target, rather than only as an input feature for load
models. Reached via the "+ Add" button on the Nimbus hub's own device
page, same as a load, just a second entry in the "add" menu.

Also the only place to add a standalone Temperature/Humidity forecast
signal (2026-09-03) -- despite the "power signal" name, the Temperature/
Humidity roles tell sensor.py/coordinator.py to build a genuinely
non-power entity (°C/%, no PowerConverter conversion attempted), not to
force a weather signal through kW/POWER semantics. See const.py's own
SIGNAL_ROLE_TEMPERATURE/SIGNAL_ROLE_HUMIDITY comment for the real
household-hit bug this fixes.

Deliberately just one field -- no schedule/expected-load "deterministic
mode" concept here (that's specific to a load with a real fixed daily
timer, e.g. a pool pump running 8am-3pm). Temperature/humidity/
curtailment/battery/grid/solar sensors used as INPUT features are still
the shared hub-level settings (flows/hub_options.py) -- unchanged,
applies here too.

REAL MEASURED entities only -- never an optimizer's own plan/forecast
output, see this repo's own CLAUDE.md PRIME DIRECTIVE. Reuses
CONF_LOAD_SENSOR as the underlying config key (functionally identical
meaning across both subentry types: "the sensor to forecast") so
coordinator.py needs zero changes to support this -- only this form's
own strings.json label differs from the load subentry's.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import selector

from ..const import (
    CONF_LOAD_SENSOR,
    CONF_SIGNAL_ROLE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_GRID,
    SIGNAL_ROLE_HUMIDITY,
    SIGNAL_ROLE_OTHER,
    SIGNAL_ROLE_SOLAR,
    SIGNAL_ROLE_TEMPERATURE,
)


# nimbus issue #360 (Mark Purcell, codebase review): extracted from
# _async_step()'s own inline vol.Schema(...) construction, matching the
# already-established module-level `_schema(defaults)` convention every
# sibling subentry flow file (load_subentry.py, power_source_subentry.py,
# etc.) already uses -- this was the one flow file in the set that never
# had one, silently blocking any test file from validating a fixture
# against the REAL schema without also driving the whole async handler
# method. Pure extraction, zero behaviour change: _async_step() below
# calls this with the exact same `current_data` dict it always built the
# schema from inline.
def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_LOAD_SENSOR, default=defaults.get(CONF_LOAD_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Explicit role (2026-08-23) -- see const.py's own comment
            # on CONF_SIGNAL_ROLE for why this can't be inferred from
            # naming. default=, not suggested_value -- a SelectSelector
            # with a real, always-valid default value is safe (unlike
            # an EntitySelector, which needs the "genuinely clearable"
            # suggested_value pattern -- this field can never be
            # cleared to nothing, "other" always is a valid choice).
            vol.Optional(
                CONF_SIGNAL_ROLE,
                default=defaults.get(CONF_SIGNAL_ROLE, SIGNAL_ROLE_OTHER),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        SIGNAL_ROLE_OTHER,
                        SIGNAL_ROLE_BATTERY,
                        SIGNAL_ROLE_SOLAR,
                        SIGNAL_ROLE_GRID,
                        SIGNAL_ROLE_TEMPERATURE,
                        SIGNAL_ROLE_HUMIDITY,
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="signal_role",
                )
            ),
        }
    )


class NimbusSignalSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one power signal (Battery/Solar/Grid/etc) under
    the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point for both a fresh "+ Add" and an edit of an existing
        signal -- see load_subentry.py's own identical comment: Home
        Assistant routes a reconfigure through this same method, not a
        separate async_step_reconfigure invocation, for subentry flows.
        """
        subentry = (
            self._get_reconfigure_subentry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        return await self._async_step(user_input, subentry=subentry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Kept as an explicit alias in case a future HA version does route
        reconfigure here separately -- same self.source-driven logic either
        way, so it's correct regardless of which method actually fires."""
        return await self.async_step_user(user_input)

    async def _async_step(
        self, user_input: dict[str, Any] | None, subentry: Any
    ) -> SubentryFlowResult:
        current_data = dict(subentry.data) if subentry is not None else {}

        if user_input is not None:
            title = self._derive_title(user_input[CONF_LOAD_SENSOR])
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        # EntitySelector, unlike NumberSelector, has no null-default crash
        # (confirmed live via load_subentry.py's own identical, already-
        # proven pattern) -- safe to always pass default=, even None.
        return self.async_show_form(step_id="user", data_schema=_schema(current_data))

    def _derive_title(self, sensor_entity_id: str) -> str:
        """Same convention as load_subentry.py's own _derive_title -- use
        the source sensor's own friendly name as the device title, falling
        back to the entity_id itself if it has no friendly_name."""
        state = self.hass.states.get(sensor_entity_id)
        if state is not None:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return friendly
        return sensor_entity_id
