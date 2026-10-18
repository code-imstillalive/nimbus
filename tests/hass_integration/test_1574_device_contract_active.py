"""nimbus #1574, DC-004 step 5b on real Home Assistant: the device model's
`active` mode, its commit order and its start-up recovery.

Household, 18 Oct 2026: "Make sure u do not break nimbus. Test deep and
thoroughly." What this pins:
- entering `active` from a clean `shadow` changes no setting; it records
  only `device_contract_view_revision`;
- a commit in `active` writes exactly the owned keys that changed (a number,
  an option, a subentry field) and then the marker;
- an interrupted commit (store written, views not) is repaired at the next
  start, and a second start finds nothing to do;
- options ahead of the store fall back to `shadow` with a Repair, changing
  nothing else;
- `active` -> `legacy` leaves the settings as they are;
- no service is called at any point (DC-R15).
Synthetic settings only (household: "100% safe").
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryDataWithId
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha, device_storage
from custom_components.nimbus_load.const import DOMAIN

MARKER = device_contract_ha.VIEW_REVISION_KEY
OPTIONS = {
    "grid_sensor": "sensor.meter_power",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
    "solver_battery_soc_sensor": "sensor.bat_soc",
    "solver_battery_power_sensor": "sensor.bat_power",
    "battery_sensor": "sensor.bat_power",
    "solver_battery_power_positive_is_charge": True,
    "solar_sensor": "sensor.pv_power",
    "solver_solar_forecast_sensor": "sensor.pv_forecast",
    "solver_load_forecast_sensor": "sensor.house_forecast",
    "solver_offer_curve_enabled": True,
}
NUMBERS = {
    "solver_battery_capacity_kwh": "40.0",
    "solver_battery_min_soc_percent": "5.0",
    "solver_battery_max_soc_percent": "100.0",
    "solver_max_charge_kw": "10.0",
    "solver_max_discharge_kw": "10.0",
    "solver_grid_max_import_kw": "20.0",
    "solver_grid_max_export_kw": "10.0",
}
SUBENTRIES = [
    ConfigSubentryDataWithId(
        data={
            "load_sensor": "sensor.pool_power",
            "schedule_start_hour": 8,
            "schedule_end_hour": 15,
            "expected_load_kw": 1.3,
        },
        subentry_id="sub_pool",
        subentry_type="load",
        title="Pool pump",
        unique_id=None,
    ),
    ConfigSubentryDataWithId(
        data={
            "controllable_load_name": "Hot water",
            "controllable_load_device_entity": "switch.hws",
        },
        subentry_id="sub_hws",
        subentry_type="controllable_load",
        title="Hot water",
        unique_id=None,
    ),
]


class _Number:
    """Stands in for a loaded Nimbus number entity: a write lands in its state,
    which is where the snapshot reads it back."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass, self.entity_id, self.written = hass, entity_id, []

    async def async_set_native_value(self, value: float) -> None:
        self.written.append(value)
        self.hass.states.async_set(self.entity_id, str(value))


class _Component:
    def __init__(self, entities: dict[str, _Number]) -> None:
        self._entities = entities

    def get_entity(self, entity_id: str):
        return self._entities.get(entity_id)


async def _setup(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus (1574 active)",
        data={},
        options=copy.deepcopy(OPTIONS),
        subentries_data=copy.deepcopy(SUBENTRIES),
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    numbers = {}
    for key, value in NUMBERS.items():
        ent = reg.async_get_or_create(
            "number", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
        )
        hass.states.async_set(ent.entity_id, value)
        numbers[key] = _Number(hass, ent.entity_id)
    hass.data.setdefault("entity_components", {})["number"] = _Component(
        {n.entity_id: n for n in numbers.values()}
    )
    return entry, numbers


def _settings(hass: HomeAssistant, entry) -> dict:
    reg = er.async_get(hass)
    return {
        "options": copy.deepcopy(dict(entry.options)),
        "subentries": {sid: dict(s.data) for sid, s in entry.subentries.items()},
        "numbers": {
            key: hass.states.get(
                reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
            ).state
            for key in NUMBERS
        },
    }


def _store(hass, entry):
    return device_storage.ContractStore(device_storage.ha_backend(hass, entry.entry_id))


def _device(document, device_id):
    return next(d for d in document["devices"] if d["id"] == device_id)


async def _activate(hass, entry):
    assert (await device_contract_ha.async_set_mode(hass, entry, "shadow"))["ok"]
    return await device_contract_ha.async_set_mode(hass, entry, "active")


async def test_entering_active_writes_only_the_marker(
    hass: HomeAssistant, hass_storage
):
    entry, numbers = await _setup(hass)
    before = _settings(hass, entry)
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        refused = await device_contract_ha.async_set_mode(hass, entry, "active")
        assert refused["code"] == "mode_transition_not_allowed"
        result = await _activate(hass, entry)
    assert calls.call_count == 0
    assert result == {"ok": True, "code": None, "mode": "active", "revision": 1}
    after = _settings(hass, entry)
    assert after["options"] == {**before["options"], MARKER: 1}
    assert after["subentries"] == before["subentries"]
    assert after["numbers"] == before["numbers"]
    assert all(not n.written for n in numbers.values())
    state = await _store(hass, entry).async_read()
    assert state["mode"] == "active" and state["view_revision"] == 1


async def test_a_commit_in_active_writes_what_changed_then_the_marker(
    hass: HomeAssistant, hass_storage
):
    entry, numbers = await _setup(hass)
    await _activate(hass, entry)
    state = await _store(hass, entry).async_read()
    doc = copy.deepcopy(state["document"])
    _device(doc, "battery")["capacity"]["value"] = 50.0
    _device(doc, "grid")["bindings"]["power"]["source"]["entity_id"] = "sensor.meter2"
    _device(doc, "pool_pump")["constraints"]["fixed_hours"]["start"] = 9
    before = _settings(hass, entry)
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        result = await device_contract_ha.async_commit_active(
            hass, entry, state["revision"], doc, "test_edit"
        )
    assert calls.call_count == 0
    assert result["ok"] is True and result["revision"] == 2
    after = _settings(hass, entry)
    assert after["numbers"] == {
        **before["numbers"],
        "solver_battery_capacity_kwh": "50.0",
    }
    assert numbers["solver_battery_capacity_kwh"].written == [50.0]
    assert all(
        not n.written for k, n in numbers.items() if k != "solver_battery_capacity_kwh"
    )
    assert after["options"] == {
        **before["options"],
        "grid_sensor": "sensor.meter2",
        MARKER: 2,
    }
    assert after["subentries"]["sub_pool"] == {
        **before["subentries"]["sub_pool"],
        "schedule_start_hour": 9,
    }
    assert after["subentries"]["sub_hws"] == before["subentries"]["sub_hws"]
    assert (await _store(hass, entry).async_read())["view_revision"] == 2


async def test_a_commit_outside_active_is_refused(hass: HomeAssistant, hass_storage):
    entry, _numbers = await _setup(hass)
    await device_contract_ha.async_set_mode(hass, entry, "shadow")
    state = await _store(hass, entry).async_read()
    before = _settings(hass, entry)
    result = await device_contract_ha.async_commit_active(
        hass, entry, state["revision"], state["document"], "test_edit"
    )
    assert result["ok"] is False and result["code"] == "not_active"
    assert _settings(hass, entry) == before


async def test_an_interrupted_commit_is_repaired_at_the_next_start(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    store = _store(hass, entry)
    state = await store.async_read()
    doc = copy.deepcopy(state["document"])
    _device(doc, "battery")["capacity"]["value"] = 60.0
    # Step 1 only: the store moves on, the views and the marker do not.
    committed = await store.async_commit(
        state["revision"], doc, origin="test", now=datetime(2026, 10, 19, tzinfo=UTC)
    )
    assert committed.ok and committed.revision == 2
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        assert await device_contract_ha.async_recover(hass, entry) == "regenerated"
        assert await device_contract_ha.async_recover(hass, entry) == "none"
    assert calls.call_count == 0
    after = _settings(hass, entry)
    assert after["numbers"]["solver_battery_capacity_kwh"] == "60.0"
    assert after["options"][MARKER] == 2
    assert (await store.async_read())["view_revision"] == 2


async def test_views_written_but_store_marker_behind_only_catches_the_marker_up(
    hass: HomeAssistant, hass_storage
):
    entry, numbers = await _setup(hass)
    await _activate(hass, entry)
    store = _store(hass, entry)
    raw = hass_storage[device_storage.store_key(entry.entry_id)]["data"]
    raw.pop("view_revision")  # step 3 lost
    before = _settings(hass, entry)
    assert await device_contract_ha.async_recover(hass, entry) == "marked"
    assert _settings(hass, entry) == before
    assert all(not n.written for n in numbers.values())
    assert (await store.async_read())["view_revision"] == 1


async def test_options_ahead_of_the_store_fall_back_to_shadow_with_a_repair(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    hass.config_entries.async_update_entry(entry, options={**entry.options, MARKER: 7})
    before = _settings(hass, entry)
    assert await device_contract_ha.async_recover(hass, entry) == "fell_back"
    assert _settings(hass, entry) == before
    assert (await _store(hass, entry).async_read())["mode"] == "shadow"
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{device_contract_ha.ISSUE_INCONSISTENT}_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "options_revision": "7",
        "store_revision": "1",
    }


async def test_leaving_active_leaves_the_settings_as_they_are(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    before = _settings(hass, entry)
    result = await device_contract_ha.async_set_mode(hass, entry, "legacy")
    assert result["ok"] is True and result["mode"] == "legacy"
    assert _settings(hass, entry) == before
    assert await device_contract_ha.async_recover(hass, entry) == "none"


async def test_a_legacy_entry_recovers_nothing(hass: HomeAssistant, hass_storage):
    entry, _numbers = await _setup(hass)
    before = _settings(hass, entry)
    assert await device_contract_ha.async_recover(hass, entry) == "none"
    assert _settings(hass, entry) == before
    assert device_storage.store_key(entry.entry_id) not in hass_storage
