"""DC-006 observation slice (#1574): one Repair per power sensor whose unit
the household must declare, its fix flow, and `active` waiting for it.

Household, 19 Oct 2026, on #1893: "tell nimbus in the repair i think is
easier than correcting the whole sensor... but it is good to have a
choice... and i do agree with strict direction"."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.nimbus_load import device_contract_ha, repairs
from custom_components.nimbus_load.const import DOMAIN
from tests.hass_integration.test_1574_device_contract_active import (
    _device,
    _setup,
    _store,
)

GRID = "sensor.meter_power"


def _unit_issues(hass, entry):
    prefix = f"{device_contract_ha.ISSUE_UNIT}_{entry.entry_id}_"
    return sorted(
        i
        for (d, i) in ir.async_get(hass).issues
        if d == DOMAIN and i.startswith(prefix)
    )


async def test_a_unitless_grid_sensor_raises_one_repair_in_shadow_only(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    hass.states.async_set(GRID, "1500")  # no unit at all
    # legacy: nothing reads the contract, so nothing is asked.
    assert await device_contract_ha.async_check_units(hass, entry) == 0
    assert (await device_contract_ha.async_set_mode(hass, entry, "shadow"))["ok"]
    assert await device_contract_ha.async_check_units(hass, entry) == 1
    (issue_id,) = _unit_issues(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable
    assert issue.translation_placeholders == {"entity_id": GRID, "stated": "no unit"}
    # Fixing the sensor itself clears it at the next check.
    hass.states.async_set(GRID, "1500", {"unit_of_measurement": "W"})
    assert await device_contract_ha.async_check_units(hass, entry) == 0
    assert _unit_issues(hass, entry) == []


async def test_active_waits_for_the_unit_and_the_repair_settles_it(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    hass.states.async_set(GRID, "1500", {"unit_of_measurement": "kw"})
    assert (await device_contract_ha.async_set_mode(hass, entry, "shadow"))["ok"]
    refused = await device_contract_ha.async_set_mode(hass, entry, "active")
    assert refused["ok"] is False and refused["code"] == "units_undeclared"
    assert refused["sensors"] == [GRID]

    await device_contract_ha.async_check_units(hass, entry)
    (issue_id,) = _unit_issues(hass, entry)
    data = ir.async_get(hass).async_get_issue(DOMAIN, issue_id).data
    flow = await repairs.async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "init"
    done = await flow.async_step_init({"unit": "kW"})
    assert done["type"] == "create_entry"
    state = await _store(hass, entry).async_read()
    power = _device(state["document"], "grid")["bindings"]["power"]
    assert power["normalization"]["native_unit"] == "kW"
    assert power["provenance"]["overridden"] is True
    assert state["origin"] == "unit_declared:grid.power"
    assert _unit_issues(hass, entry) == []

    # The declaration survives the re-import on the way to active.
    with patch("homeassistant.core.ServiceRegistry.async_call"):
        result = await device_contract_ha.async_set_mode(hass, entry, "active")
    assert result["ok"] is True and result["mode"] == "active"
    state = await _store(hass, entry).async_read()
    power = _device(state["document"], "grid")["bindings"]["power"]
    assert power["normalization"]["native_unit"] == "kW"


async def test_an_unknown_unit_choice_is_refused(hass: HomeAssistant, hass_storage):
    entry, _numbers = await _setup(hass)
    hass.states.async_set(GRID, "1500")
    assert (await device_contract_ha.async_set_mode(hass, entry, "shadow"))["ok"]
    problem = await device_contract_ha.async_declare_unit(
        hass, entry, "grid", "power", "MW"
    )
    assert problem == "no_longer_applies"
    assert await device_contract_ha.async_check_units(hass, entry) == 1
