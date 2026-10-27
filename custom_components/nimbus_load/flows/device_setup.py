"""Configure -> Device setup: the existing-install path (DC-007 slice 2, #1574).

One menu on the hub's own Configure, offering what DC-007's spec lists for an
install that already exists:

- in `legacy`: the dry run, and the move to `shadow` when it is clean
  (DC-005 decision 7: confirming needs a stored document);
- in `shadow` and `active`: **Review what Nimbus finds** (DC-005's
  rediscovery as one choice per difference: a fill, a rename or a new device
  pre-ticked, an alternative to a confirmed binding never), and **Mode**.

Every step goes through the same services the actions use
(`device_contract_ha`): nothing here writes a setting or the store itself.
An options flow ends by saving the entry's options, so each step saves them
as they are *after* the step's own commit: a view written by the commit is
never put back.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

#: The difference kinds pre-ticked on Review (DC-007, the existing-install path).
_PRE_TICKED = ("fill", "renamed", "new_device")


def _describe(difference: dict[str, Any]) -> str:
    """One rediscovery difference in plain words."""
    kind = difference.get("kind")
    where = f"{difference.get('device')} {str(difference.get('role') or '').replace('_', ' ')}".strip()
    if kind == "fill":
        source = (difference.get("binding") or {}).get("source") or {}
        found = source.get("entity_id") or source.get("statistic_id")
        return f"Fill {where}: {found}"
    if kind == "renamed":
        return (
            f"Renamed {where}: {difference.get('stored_entity_id')} is now "
            f"{difference.get('entity_id')}"
        )
    if kind == "alternative":
        found = (difference.get("candidate") or {}).get("source")
        return f"Alternative for {where}: {found} (yours stays unless you tick this)"
    if kind == "new_device":
        device = difference.get("device") or {}
        return f"New device: {device.get('name') or device.get('id')}"
    return f"{kind}: {where}"


def _choice(difference: dict[str, Any], capacity: float | None) -> dict[str, Any]:
    """The `device_contract_confirm` choice for one ticked difference."""
    kind = difference["kind"]
    if kind == "new_device":
        device = difference["device"]
        out: dict[str, Any] = {"kind": kind, "device": device["id"]}
        if device.get("kind") == "battery" and capacity:
            out["capacity"] = {"value": capacity, "unit": "kWh", "basis": "usable"}
        return out
    entity_id = {
        "fill": lambda d: (
            (d.get("binding") or {}).get("source", {}).get("entity_id")
            or (d.get("binding") or {}).get("source", {}).get("statistic_id")
        ),
        "renamed": lambda d: d.get("entity_id"),
        "alternative": lambda d: (d.get("candidate") or {}).get("source"),
    }[kind](difference)
    return {
        "kind": kind,
        "device": difference.get("device"),
        "role": difference.get("role"),
        "entity_id": entity_id,
    }


class DeviceSetupSteps:
    """Mixed into the hub's options flow: Configure -> Device setup."""

    hass: Any
    config_entry: Any
    _review: dict[str, Any] | None = None

    def _finish(self) -> Any:
        # The options as they are now, after any commit this step made.
        return self.async_create_entry(data=dict(self.config_entry.options))  # type: ignore[attr-defined]

    async def _mode(self) -> str:
        from .. import device_contract_ha

        return (await device_contract_ha.async_preview(self.hass, self.config_entry))[
            "mode"
        ]

    async def async_step_device_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """The Device setup menu, by mode."""
        mode = await self._mode()
        options = (
            ["device_setup_dry_run"]
            if mode == "legacy"
            else ["device_setup_review", "device_setup_mode"]
        )
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="device_setup",
            menu_options=options,
            description_placeholders={"mode": mode},
        )

    async def async_step_device_setup_dry_run(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """What the device setup would build from today's settings, and the
        move to `shadow` when the dry run is clean."""
        from .. import device_contract_ha

        preview = await device_contract_ha.async_preview(self.hass, self.config_entry)
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("go_shadow"):
                return self._finish()
            result = await device_contract_ha.async_set_mode(
                self.hass, self.config_entry, "shadow"
            )
            if result.get("ok"):
                return self._finish()
            errors["base"] = "mode_refused"
        lines = [
            f"- {c['code']}: {c['path']}" for c in preview.get("conflicts") or ()
        ] + [
            f"- {d['path']}: {d['now']} -> {d['after']}"
            for d in preview.get("effective_diff") or ()
        ]
        schema = (
            vol.Schema(
                {vol.Optional("go_shadow", default=False): selector.BooleanSelector()}
            )
            if preview.get("clean")
            else vol.Schema({})
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device_setup_dry_run",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "result": "clean: nothing would change"
                if preview.get("clean")
                else "not clean",
                "details": "\n".join(lines) or "- nothing",
            },
        )

    async def async_step_device_setup_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Legacy, shadow or active, through the same checks as the select."""
        from .. import device_contract_ha

        preview = await device_contract_ha.async_preview(self.hass, self.config_entry)
        errors: dict[str, str] = {}
        if user_input is not None:
            target = user_input["mode"]
            if target == preview["mode"]:
                return self._finish()
            result = await device_contract_ha.async_set_mode(
                self.hass, self.config_entry, target
            )
            if result.get("ok"):
                return self._finish()
            errors["base"] = "mode_refused"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device_setup_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "mode", default=preview["mode"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["legacy", "shadow", "active"],
                            translation_key="device_setup_mode",
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "mode": preview["mode"],
                "result": "clean" if preview.get("clean") else "not clean",
            },
        )

    async def async_step_device_setup_review(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Review what Nimbus finds: one tick per difference."""
        from .. import device_contract_ha

        errors: dict[str, str] = {}
        if user_input is not None and self._review is not None:
            differences = self._review["differences"]
            ticked = [int(i) for i in user_input.get("choices") or ()]
            if not ticked:
                return self._finish()
            capacity = user_input.get("capacity_kwh")
            choices = [_choice(differences[i], capacity) for i in ticked]
            result = await device_contract_ha.async_confirm(
                self.hass, self.config_entry, self._review["revision"], choices
            )
            if result.get("ok"):
                return self._finish()
            errors["base"] = str(result.get("code") or "confirm_refused")
        self._review = await device_contract_ha.async_discover(
            self.hass, self.config_entry
        )
        differences = [
            d
            for d in self._review.get("differences") or ()
            if d.get("kind") in ("fill", "renamed", "alternative", "new_device")
        ]
        self._review["differences"] = differences
        if not differences:
            return self.async_show_form(  # type: ignore[attr-defined]
                step_id="device_setup_nothing", data_schema=vol.Schema({})
            )
        options = [
            selector.SelectOptionDict(value=str(i), label=_describe(d))
            for i, d in enumerate(differences)
        ]
        schema: dict[Any, Any] = {
            vol.Optional(
                "choices",
                default=[
                    str(i)
                    for i, d in enumerate(differences)
                    if d["kind"] in _PRE_TICKED
                ],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            )
        }
        if any(
            d["kind"] == "new_device"
            and (d.get("device") or {}).get("kind") == "battery"
            for d in differences
        ):
            schema[vol.Optional("capacity_kwh")] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=1000,
                    step=0.1,
                    unit_of_measurement="kWh",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device_setup_review",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_device_setup_nothing(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Rediscovery found nothing new."""
        return self._finish()
