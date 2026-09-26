"""Battery Participant subentry flow -- adds one ADDITIONAL, independently-
metered battery/EV to an already-installed Nimbus hub (nimbus issue #563,
the config surface for #467 stage 1's own `batteries: list[BatteryConfig]`
solver support).

Distinct from a Battery Tower subentry (flows/battery_tower_subentry.py):
a Battery Tower is pure topology/wiring metadata for the dashboard's
topology card, never read by the Solver. A Battery Participant is a real
LP dispatch participant -- see const.py's own SUBENTRY_TYPE_BATTERY_
PARTICIPANT comment for the full "why a separate type" reasoning.

The household's own single, existing hub-level battery (the Solver
settings wizard's own capacity/SoC/power fields) always stays the "home"
participant -- these subentries are ADDITIONAL participants on top of it,
never a replacement. Zero subentries is a real no-op (see solver_writer.py's
own build_extra_batteries()).
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
    CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
    CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
    CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
    CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
    CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
    CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
    CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
    CONF_BATTERY_PARTICIPANT_NAME,
    CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY,
    CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
    CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
    CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
    CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
    CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
    DEFAULT_PARTICIPANT_KWH_PER_100KM,
)

_KW_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kW"
    )
)
_KWH_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh"
    )
)
_PERCENT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=100,
        step=0.1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="%",
    )
)
# nimbus issue #1253, fourth site. Was a literal "$/kWh". Unlike the flattened
# children this is a SELECTOR LABEL -- the unit beside a number input -- so
# there is no entity, no registry row and no long-term statistic behind it,
# and none of that issue's migration risk. It is also the site a NEW household
# sees FIRST, before any sensor exists.
#
# Built per-call because the currency is not known at import time. The unit is
# omitted entirely when HA has no currency configured: an absent unit is
# honest, and a default would re-introduce the hardcode with a different
# string (the posture sensor.py's own currency call sites already take).


def _currency_per_kwh_selector(currency: str | None) -> selector.NumberSelector:
    config: dict[str, Any] = {"min": 0, "mode": selector.NumberSelectorMode.BOX}
    if currency:
        config["unit_of_measurement"] = f"{currency}/kWh"
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


# Same convention as the existing Solver P2P block start/end hour
# fields (number.py's own P2P Block N Start/End Hour) -- a plain
# whole-hour integer, 0-23.
# nimbus issue #467 item 4. 5-40 covers every real road vehicle -- a light
# hybrid sits near 12, a large electric SUV near 30, a van higher -- and the
# bound is there to catch a units mistake (entering Wh/km, which would be ~180)
# rather than to police a household's own figure.
_KWH_PER_100KM_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=5.0,
        max=40.0,
        step=0.5,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="kWh/100km",
    )
)

_HOUR_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=23,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="hour",
    )
)


def _optional_field(
    schema_dict: dict[Any, Any], key: str, default: Any, field_selector: Any
) -> None:
    """Same None-default-crashes-the-frontend fix as controllable_load_
    subentry.py's own helper -- see that module's comment (ha-selector-
    number calls .toString() on the default with no null-check)."""
    if default is not None:
        schema_dict[vol.Optional(key, default=default)] = field_selector
    else:
        schema_dict[vol.Optional(key)] = field_selector


def _schema(defaults: dict[str, Any], currency: str | None = None) -> vol.Schema:
    schema_dict: dict[Any, Any] = {
        vol.Required(
            CONF_BATTERY_PARTICIPANT_NAME,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_NAME),
        ): selector.TextSelector(),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH),
        ): _KWH_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR),
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_POWER_SENSOR),
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        # nimbus issue #1131: this default is the OPPOSITE of the home
        # battery's, and why is not established.
        #
        # `CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE` in
        # flows/hub_options.py is `vol.Optional` with no default, so it
        # resolves falsy -- positive means DISCHARGE -- and it carries a
        # five-line comment naming #299, the vendor, and the
        # backward-compatibility reasoning. This one is `vol.Required`
        # with `default=True` -- positive means CHARGE -- and carried
        # nothing at all until this comment.
        #
        # So a household accepting what both forms offer ends up with
        # opposite conventions for its home battery and its EV.
        #
        # **That may well be correct**: an EV charger sensor plausibly
        # reports positive while power flows INTO the car, where an
        # inverter reports positive while discharging. If so, differing
        # defaults is a good decision. No claim either way is made here,
        # deliberately -- inventing a justification for a choice nobody
        # recorded would be worse than the silence it replaces, and this
        # comment exists to stop the next reader re-deriving the
        # asymmetry rather than to explain it away.
        #
        # What IS established is the cost of getting it wrong. It does
        # not error; it inverts the charge/discharge split in the
        # scorer's own history reconstruction (see solver_writer.py's
        # `sign = -1.0 if ... else 1.0`), which is the #535/#843 class --
        # convention errors that produce plausible-looking numbers rather
        # than failures. Worse, the resulting energy balance trips
        # #1073/#1098's guards, so the symptom looks like one of the
        # known confounds rather than a configuration error.
        vol.Required(
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            default=defaults.get(
                CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE, True
            ),
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW),
        ): _KW_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW),
        ): _KW_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT, 10.0),
        ): _PERCENT_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT, 100.0),
        ): _PERCENT_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT, 95.0),
        ): _PERCENT_SELECTOR,
    }
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
        defaults.get(CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE),
        _currency_per_kwh_selector(currency),
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
        defaults.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH),
        _currency_per_kwh_selector(currency),
    )
    # #563 item 4: an optional live number entity whose current value
    # overrides this participant's own max_soc_percent for that solve --
    # see const.py's own comment on this field for the real "car usually
    # charged to 80%, occasionally set to 100%" reasoning.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="number")),
    )
    # #563 item 2: availability gating -- an optional binary_sensor
    # (e.g. "located at home", "charge cable connected") whose CURRENT
    # state gates this participant's whole-solve charge/discharge -- see
    # const.py's own comment on this field for the real EV case this
    # exists for.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
    )
    # #563 item 2, the departure-deadline half -- both fields optional,
    # and must be set together (see build_extra_batteries()'s own
    # reconciliation -- setting only one is treated as "neither set",
    # not an error, since BatteryConfig.__post_init__ itself requires
    # both-or-neither).
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
        defaults.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR),
        _HOUR_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
        defaults.get(CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT),
        _PERCENT_SELECTOR,
    )
    # nimbus issue #467 item 4: a CALENDAR-driven departure, as an alternative
    # to the fixed hour/percent pair directly above -- placed here so the form
    # reads as "a fixed time, or a calendar" in one place rather than scattering
    # the two mechanisms.
    #
    # The fixed pair answers "this car leaves at 07:00 and should have 60% in
    # it". A calendar answers a different question -- when does it ACTUALLY
    # leave, and how far is it going -- and sizes the requirement from real
    # distance instead of a percentage the household converts by hand.
    #
    # Precedence, resolved in build_extra_batteries(): the calendar wins when it
    # resolves a trip inside this horizon, and the fixed pair remains the
    # fallback for every solve where it does not (an empty calendar, a trip
    # beyond the horizon, an event with no distance in its text). Configuring
    # only the fixed pair is completely unaffected.
    #
    # Three entries, ONE real decision: kwh_per_100km carries a default, so the
    # calendar entity is the only thing a household has to choose. That is the
    # "sensible defaults" half of Mark's own #449/#485 principle rather than a
    # fourth thing to go and research.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar")),
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_KWH_PER_100KM,
        defaults.get(CONF_BATTERY_PARTICIPANT_KWH_PER_100KM)
        or DEFAULT_PARTICIPANT_KWH_PER_100KM,
        _KWH_PER_100KM_SELECTOR,
    )
    # Optional, and genuinely optional: without it the FULL trip distance is
    # required, which is conservative in the right direction -- the pack ends up
    # fuller than strictly needed, never emptier. With it, a trip already under
    # way only requires the charge for the distance remaining.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_ODOMETER_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
    )
    # #563 item 3: the shared-charger power constraint -- a free-text
    # group name (two participants with the SAME name share one real
    # physical charger) plus that group's own kW ceiling. See const.py's
    # own comment on these fields for the real "two Teslas on one Sigen
    # DC charger" case.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
        defaults.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP),
        selector.TextSelector(),
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
        defaults.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW),
        _KW_SELECTOR,
    )
    return vol.Schema(schema_dict)


class NimbusBatteryParticipantSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one additional battery/EV participant under
    the Nimbus hub. Same self.source-driven reconfigure-vs-fresh-add
    pattern as every other subentry flow in this project -- see
    load_subentry.py's own async_step_user docstring for the real
    HA-behavior finding this is built on."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = (
            self._get_reconfigure_subentry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        return await self._async_step(user_input, subentry=subentry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self.async_step_user(user_input)

    async def _async_step(
        self, user_input: dict[str, Any] | None, subentry: Any
    ) -> SubentryFlowResult:
        current_data = dict(subentry.data) if subentry is not None else {}

        if user_input is not None:
            title = user_input[CONF_BATTERY_PARTICIPANT_NAME]
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(current_data, self.hass.config.currency),
        )
