"""Spec DC-000 side-effect trap for DC-R15, against a real Home Assistant:
setting Nimbus up, and accepting a setup-fill Repair, command no equipment.

`tests/device_contract/test_inventory.py` already enforces DC-R15 statically
(every equipment-capable service call lives in `solver_dispatch/`). That
reads the source; it cannot see a call made at runtime through a path the
inventory does not model. This runs the real startup -- dashboard creation,
pricing discovery, setup-fill offers -- and the real Repair fix flow, with
actuators present for discovery to find, and fails on any call to them.

Equipment services are registered as mocks so a call is recorded rather than
raising ServiceNotFound, which a caller might swallow. The negative control
proves the trap can fire.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.setup_builder import async_refresh_setup_fills

# The equipment domains are the static scanner's own CONTROL_DOMAINS
# (scripts/device_contract_baseline.py), so the runtime trap and the
# inventory gate cannot drift apart (nimbus #1624 follow-up, IV&V #1622).
_spec = importlib.util.spec_from_file_location(
    "_dc_baseline_trap",
    Path(__file__).resolve().parents[2] / "scripts" / "device_contract_baseline.py",
)
_baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_baseline)
CONTROL_DOMAINS: set[str] = _baseline.CONTROL_DOMAINS

# Services that change equipment state, for every control domain. Read-only
# actions (forecast lookups) are deliberately not here: DC-R15 is about
# commands.
SERVICES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "switch": ("turn_on", "turn_off", "toggle"),
    "number": ("set_value",),
    "select": ("select_option",),
    "script": ("turn_on", "turn_off", "toggle"),
    "climate": ("set_hvac_mode", "set_temperature", "turn_on", "turn_off"),
    "input_number": ("set_value",),
    "input_select": ("select_option",),
    "input_boolean": ("turn_on", "turn_off", "toggle"),
    "button": ("press",),
    "water_heater": ("set_operation_mode", "set_temperature", "turn_on", "turn_off"),
    "light": ("turn_on", "turn_off", "toggle"),
    "fan": ("turn_on", "turn_off", "toggle", "set_percentage"),
    "cover": ("open_cover", "close_cover", "stop_cover", "set_cover_position"),
    "lock": ("lock", "unlock", "open"),
    "modbus": ("write_register", "write_coil"),
}
EQUIPMENT_SERVICES = tuple(
    (domain, service)
    for domain, services in sorted(SERVICES_BY_DOMAIN.items())
    for service in services
)


def test_the_trap_covers_exactly_the_scanners_control_domains():
    """One concept, one list: a domain added to CONTROL_DOMAINS must be
    trapped here too, and nothing is trapped that the scanner ignores."""
    assert set(SERVICES_BY_DOMAIN) == CONTROL_DOMAINS


# Actuators of the kinds Nimbus discovers or controls, so discovery has
# something it could mistakenly operate.
ACTUATORS = {
    "switch.pool_pump": ("off", {}),
    "water_heater.hot_water": ("eco", {"operation_list": ["eco", "performance"]}),
    "number.inverter_battery_power": ("0", {"min": -40, "max": 40, "step": 0.1}),
    "select.inverter_ems_mode": ("Self-consume", {"options": ["Self-consume", "VPP"]}),
    "sensor.test_battery_power": ("2.5", {"unit_of_measurement": "kW"}),
    "sensor.test_battery_soc": ("55", {"unit_of_measurement": "%"}),
    "sensor.test_grid_power": ("1.2", {"unit_of_measurement": "kW"}),
}


def _trap(hass: HomeAssistant) -> dict[tuple[str, str], list]:
    return {
        (domain, service): async_mock_service(hass, domain, service)
        for domain, service in EQUIPMENT_SERVICES
    }


def _commanded(trap: dict[tuple[str, str], list]) -> dict[str, int]:
    return {f"{d}.{s}": len(calls) for (d, s), calls in trap.items() if calls}


async def test_setup_and_a_setup_fill_repair_command_no_equipment(
    hass: HomeAssistant,
):
    assert await async_setup_component(hass, "repairs", {})
    trap = _trap(hass)
    for entity_id, (state, attrs) in ACTUATORS.items():
        hass.states.async_set(entity_id, state, attrs)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus",
        data={},
        options={"battery_sensor": "sensor.test_battery_power"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _commanded(trap) == {}, "startup commanded equipment"

    await async_refresh_setup_fills(hass, entry)
    await hass.async_block_till_done()
    issue_id = f"{entry.entry_id}_setup_fill_solver_battery_power_sensor"
    # Same configuration as test_1587's, which proves this offer is made;
    # asserted here too so the Repair half can never pass by not running.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    manager = hass.data["repairs"]["flow_manager"]
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert _commanded(trap) == {}, "a setup-fill Repair commanded equipment"


async def test_negative_control_the_trap_fires(hass: HomeAssistant):
    """Without this, a trap that never records would pass every run."""
    trap = _trap(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.pool_pump"}, blocking=True
    )
    assert _commanded(trap) == {"switch.turn_on": 1}
