"""Load subentry flow -- adds one load to an already-installed Nimbus hub.

Reached via the "+ Add" button on the Nimbus hub's own device page, not via
"Add Integration" -- this is what makes adding many loads (18 circuit
breakers, in the case this was built for) fast: pick a sensor, submit, done,
repeat as many times as needed, no restart between them.

Deliberately just one field. Everything else a load used to ask for
(temperature sensors, forecast horizon, retrain hour, training window) is
now set once at the hub level (flows/hub_options.py, reached via the hub's
own "Configure") -- those are the same for every load in the same house, so
re-asking for them 18 times was pure friction, not a real choice each load
needed to make.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import CONF_LOAD_SENSOR, CONF_SCHEDULE_END_HOUR, CONF_SCHEDULE_START_HOUR


# A real HH:MM time picker, not a decimal-hour number box -- much more
# natural to enter (e.g. "12:30" instead of "12.5"). Stored as HA's own
# "HH:MM:SS" string; converted to the decimal hour ml/features.py's
# in_schedule comparison actually uses right where it's read
# (coordinator.py's _schedule_start_hour/_schedule_end_hour properties,
# via _parse_time_to_hour), not here -- this flow only handles the UI
# side, the stored value stays a plain time string.
_TIME_SELECTOR = selector.TimeSelector(selector.TimeSelectorConfig())


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    schema_dict: dict[Any, Any] = {
        vol.Required(
            CONF_LOAD_SENSOR, default=defaults.get(CONF_LOAD_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
    }
    # Optional, genuinely per-load (unlike everything on the hub's own
    # shared form) -- for a load with a real fixed daily timer (e.g. a
    # pool pump running 8am-3pm every day), a dedicated schedule-window
    # feature lets the model learn the sharp on/off boundary directly
    # instead of only approximating it through hour-of-day sin/cos
    # splits. Left blank for any load without a real fixed schedule --
    # a no-op, not an error.
    #
    # Confirmed live 2026-08-15: passing `default=None` (i.e. whatever
    # defaults.get(...) returns for a never-configured field, which is
    # every load's first time seeing this field) crashes the frontend's
    # ha-selector-number component -- it calls `.toString()` on the
    # default value while rendering, with no null-check, throwing
    # "Cannot read properties of null (reading 'toString')" and
    # silently failing to render that field at all, no error visible in
    # the HA UI itself, only in the browser's own JS console. The field
    # must be added with NO `default=` kwarg at all when unset, not
    # `default=None` -- vol.Optional() with no default cleanly omits
    # the key from user_input if left blank, which the frontend renders
    # as a genuinely empty box instead of trying to stringify null.
    start_default = defaults.get(CONF_SCHEDULE_START_HOUR)
    if start_default is not None:
        schema_dict[vol.Optional(CONF_SCHEDULE_START_HOUR, default=start_default)] = _TIME_SELECTOR
    else:
        schema_dict[vol.Optional(CONF_SCHEDULE_START_HOUR)] = _TIME_SELECTOR

    end_default = defaults.get(CONF_SCHEDULE_END_HOUR)
    if end_default is not None:
        schema_dict[vol.Optional(CONF_SCHEDULE_END_HOUR, default=end_default)] = _TIME_SELECTOR
    else:
        schema_dict[vol.Optional(CONF_SCHEDULE_END_HOUR)] = _TIME_SELECTOR

    return vol.Schema(schema_dict)


class NimbusLoadSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one load under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point for both a fresh "+ Add" and an edit of an existing
        load -- confirmed live 2026-08-14 that Home Assistant routes a
        reconfigure through this same method (async_step_reconfigure is not
        actually a separate invocation path for subentry flows the way it
        is for top-level config entries). The real signal for which case
        this is is `self.source`, not which method got called -- assuming
        "user" always meant "brand new" raised
        `ValueError: Source is reconfigure, expected user` from
        async_create_entry the moment someone tried to edit an existing
        load rather than add one.
        """
        subentry = self._get_reconfigure_subentry() if self.source == SOURCE_RECONFIGURE else None
        return await self._async_step(user_input, subentry=subentry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Kept as an explicit alias in case a future HA version does route
        reconfigure here separately -- same self.source-driven logic either
        way, so it's correct regardless of which method actually fires.
        """
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

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))

    def _derive_title(self, load_sensor: str) -> str:
        """Use the source sensor's own friendly name as the device title
        ("Logger Load Power") rather than a raw entity_id wrapped in text
        ("Load (sensor.logger_load_power)") -- confirmed live 2026-08-14
        that the raw-entity-id version combines with the entity's own name
        into an unusable auto-generated entity_id downstream. Falls back to
        the entity_id itself if the sensor has no friendly_name for some
        reason (still functional, just less pretty).
        """
        state = self.hass.states.get(load_sensor)
        if state is not None:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return friendly
        return load_sensor
