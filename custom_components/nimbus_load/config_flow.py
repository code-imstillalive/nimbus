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

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_CONTROLLABLE_LOAD,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
    SUBENTRY_TYPE_SIGNAL,
)
from .flows.battery_participant_subentry import (
    NimbusBatteryParticipantSubentryFlowHandler,
)
from .flows.battery_tower_subentry import NimbusBatteryTowerSubentryFlowHandler
from .flows.controllable_load_subentry import (
    NimbusControllableLoadSubentryFlowHandler,
)
from .flows.hub_options import NimbusHubOptionsFlow
from .flows.load_subentry import NimbusLoadSubentryFlowHandler
from .flows.power_source_subentry import NimbusPowerSourceSubentryFlowHandler
from .flows.pv_string_subentry import NimbusPvStringSubentryFlowHandler
from .flows.signal_subentry import NimbusSignalSubentryFlowHandler

_LOGGER = logging.getLogger(__name__)

#: DC-007 slice 1: the entry data key a confirmed new-install document waits
#: in until the hub's first setup commits it (device_contract_ha).
PENDING_DEVICE_DOCUMENT = "pending_device_document"

#: The order devices are shown in (DC-007, the new-install path, step 2).
_KIND_ORDER = {"grid": 0, "load": 1, "solar": 2, "battery": 3}
#: How many characters of a source the binding list shows.
_SHOWN = 80


class NimbusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle creation of the single Nimbus hub."""

    VERSION = 1

    def __init__(self) -> None:
        self._proposal: Any = None
        self._devices: list[dict[str, Any]] = []
        self._device_index = 0
        self._keep: set[str] = set()
        self._questions: list[int] = []
        self._question_index = 0
        self._answers: dict[int, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Only one Nimbus hub is meaningful per HA instance.

        DC-007 slice 1 (#1574): Nimbus first looks at what Home Assistant
        already knows (the Energy Dashboard, recognised price providers) and
        shows the site it found, one device per step, then one question per
        thing it could not decide. With nothing found, the hub is created at
        once, as before."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        try:
            from . import device_contract_ha

            self._proposal, _regs = await device_contract_ha.async_propose(self.hass)
        except Exception:  # discovery must never stop an install
            _LOGGER.debug("Nimbus: discovery at install failed", exc_info=True)
            self._proposal = None
        if not _found_anything(self._proposal):
            return await self._async_create_hub(None)
        self._devices = sorted(
            self._proposal.document["devices"],
            key=lambda d: (_KIND_ORDER.get(d.get("kind"), 9), d["id"]),
        )
        return await self.async_step_device()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Here is the site Nimbus found: one device per step, pre-ticked."""
        if user_input is not None:
            if user_input.get("keep", True):
                self._keep.add(self._devices[self._device_index]["id"])
            self._device_index += 1
        if self._device_index < len(self._devices):
            device = self._devices[self._device_index]
            return self.async_show_form(
                step_id="device",
                data_schema=vol.Schema(
                    {vol.Optional("keep", default=True): selector.BooleanSelector()}
                ),
                description_placeholders={
                    "name": str(device.get("name") or device["id"]),
                    "number": str(self._device_index + 1),
                    "count": str(len(self._devices)),
                    "bindings": _describe_bindings(device),
                },
            )
        kept = self._keep | {"grid", "load_house"}
        self._questions = [
            i
            for i, q in enumerate(self._proposal.questions)
            if q.get("device") in kept and _askable(q)
        ]
        return await self.async_step_question()

    async def async_step_question(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One question per thing discovery could not decide, one field each."""
        if user_input is not None:
            index = self._questions[self._question_index]
            question = self._proposal.questions[index]
            if question.get("code") == "fact_needed":
                value = user_input.get("capacity_kwh")
                if value:
                    self._answers[index] = {
                        "value": float(value),
                        "basis": user_input.get("basis") or "usable",
                    }
            else:
                choice = user_input.get("source")
                self._answers[index] = None if choice in (None, "", "skip") else choice
            self._question_index += 1
        if self._question_index < len(self._questions):
            index = self._questions[self._question_index]
            question = self._proposal.questions[index]
            device = next(
                (d for d in self._devices if d["id"] == question.get("device")), {}
            )
            placeholders = {
                "name": str(device.get("name") or question.get("device")),
                "role": str(question.get("role") or question.get("fact") or ""),
            }
            if question.get("code") == "fact_needed":
                schema = vol.Schema(
                    {
                        vol.Optional("capacity_kwh"): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0.1,
                                max=1000,
                                step=0.1,
                                unit_of_measurement="kWh",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional(
                            "basis", default="usable"
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=["usable", "nominal"],
                                translation_key="capacity_basis",
                            )
                        ),
                    }
                )
                return self.async_show_form(
                    step_id="capacity",
                    data_schema=schema,
                    description_placeholders=placeholders,
                )
            options = [
                selector.SelectOptionDict(value=c["source"], label=c["source"])
                for c in question.get("candidates") or ()
                if c.get("source")
            ] + [
                selector.SelectOptionDict(
                    value="skip", label="Leave as Nimbus proposed"
                )
            ]
            return self.async_show_form(
                step_id="question",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "source", default=options[0]["value"]
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=options)
                        )
                    }
                ),
                description_placeholders=placeholders,
            )
        return await self.async_step_finish()

    async def async_step_capacity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A fact the plan needs (the battery's capacity): same handling as a
        question, its own step id so it has its own fields' labels."""
        return await self.async_step_question(user_input)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The summary; confirming creates the hub with the document, which
        its first setup commits (device_contract_ha.async_adopt_new_install)."""
        from . import device_discovery

        if user_input is not None:
            document = device_discovery.answer_proposal(
                self._proposal, self._keep, self._answers
            )
            return await self._async_create_hub(document)
        kept = [d for d in self._devices if d["id"] in self._keep]
        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema({}),
            description_placeholders={
                "devices": "\n".join(f"- {d.get('name') or d['id']}" for d in kept)
                or "- the grid connection and the house",
            },
        )

    async def _async_create_hub(
        self, document: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Create the hub. With a confirmed document, its first setup commits
        it and the notification points at the Device setup card; without one
        the hub is empty, as before, and the notification says what to set."""
        if document is not None:
            message = (
                "Nimbus has saved the site you confirmed and filled in the "
                "settings it implies. **What works now, and what still needs "
                "one more fact, is on the Solver tab's Device setup card.** "
                "The device setup is kept in shadow beside those settings; "
                "move it to active in Configure > Device setup when you choose."
            )
        else:
            message = (
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
            )
        # Real gap found live, 2026-08-22: a fresh install landed on the hub
        # with every hardware number at a placeholder and nothing saying so.
        # Wrapped -- hub creation must succeed whether or not this does.
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Nimbus: one more step",
                    "message": message,
                    "notification_id": "nimbus_setup_incomplete",
                },
            )
        except Exception:  # noqa: BLE001, S110 -- a notification failure must never block hub setup
            pass
        data = {} if document is None else {PENDING_DEVICE_DOCUMENT: document}
        return self.async_create_entry(title="Nimbus", data=data)

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
            SUBENTRY_TYPE_CONTROLLABLE_LOAD: NimbusControllableLoadSubentryFlowHandler,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT: NimbusBatteryParticipantSubentryFlowHandler,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The hub's own "Configure" -- shared settings (temperature
        sensors, forecast horizon, retrain hour, training window) that
        apply to every load, set once instead of re-entered per load."""
        return NimbusHubOptionsFlow(config_entry)


def _found_anything(proposal: Any) -> bool:
    """True when discovery proposed a source for anything: a bare grid and
    house with nothing bound is the empty hub of before, not a site."""
    if proposal is None:
        return False
    return any(
        (d.get("bindings") or {}) for d in proposal.document.get("devices") or ()
    )


def _askable(question: dict[str, Any]) -> bool:
    """The questions slice 1 asks: pick one source, confirm a match, or the
    battery's capacity. Others stay for Configure -> Device setup."""
    code = question.get("code")
    if code == "fact_needed":
        return question.get("fact") == "capacity"
    return code in ("several_sources", "confirm_history_match") and bool(
        question.get("candidates")
    )


def _describe_bindings(device: dict[str, Any]) -> str:
    """A device's bindings in plain words, one per line."""
    lines = []
    for role, binding in sorted((device.get("bindings") or {}).items()):
        source = binding.get("source") or {}
        what = (
            source.get("entity_id")
            or source.get("statistic_id")
            or source.get("value")
            or source.get("kind")
        )
        lines.append(f"- {role.replace('_', ' ')}: {str(what)[:_SHOWN]}")
    return "\n".join(lines) or "- nothing bound yet"
