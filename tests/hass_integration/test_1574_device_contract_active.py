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

import asyncio
import copy
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryDataWithId
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import (
    device_contract_ha,
    device_import,
    device_storage,
)
from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.select import NimbusDeviceSetupModeSelect

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
    with (
        patch("homeassistant.core.ServiceRegistry.async_call") as calls,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await device_contract_ha.async_commit_active(
            hass, entry, state["revision"], doc, "test_edit"
        )
    assert calls.call_count == 0
    reload.assert_called_once_with(entry.entry_id)
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


def _issue_ids(hass, entry):
    prefix = f"{device_contract_ha.ISSUE_DRIFT}_{entry.entry_id}_"
    return sorted(
        i
        for (d, i) in ir.async_get(hass).issues
        if d == DOMAIN and i.startswith(prefix)
    )


async def test_drift_raises_one_repair_per_key_and_keep_commits_it(
    hass: HomeAssistant, hass_storage
):
    """DC-004 step 5c: an owned option edited in active mode is drift, with one
    fixable Repair; *Keep my change* commits it as a new revision and clears
    the Repair. Nothing is written but the store and that option's own view."""
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "grid_sensor": "sensor.meter2"}
    )
    assert await device_contract_ha.async_check_drift(hass, entry) == 1
    (issue_id,) = _issue_ids(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable and issue.data["key"] == "grid_sensor"
    assert issue.translation_placeholders == {
        "key": "grid_sensor",
        "view": "sensor.meter_power",
        "current": "sensor.meter2",
    }
    before = _settings(hass, entry)
    with (
        patch("homeassistant.core.ServiceRegistry.async_call") as calls,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        assert (
            await device_contract_ha.async_keep(
                hass, entry, "options", None, "grid_sensor"
            )
            is None
        )
    assert calls.call_count == 0
    state = await _store(hass, entry).async_read()
    assert state["revision"] == 2 and state["origin"] == "drift_keep:grid_sensor"
    assert (
        _device(state["document"], "grid")["bindings"]["power"]["source"]["entity_id"]
        == "sensor.meter2"
    )
    after = _settings(hass, entry)
    assert after["options"] == {**before["options"], MARKER: 2}
    assert (
        after["numbers"] == before["numbers"]
        and after["subentries"] == before["subentries"]
    )
    assert _issue_ids(hass, entry) == []


async def test_restore_puts_the_documents_value_back_without_a_revision(
    hass: HomeAssistant, hass_storage
):
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    pool = entry.subentries["sub_pool"]
    hass.config_entries.async_update_subentry(
        entry, pool, data={**pool.data, "schedule_start_hour": 9}
    )
    assert await device_contract_ha.async_check_drift(hass, entry) == 1
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert (
            await device_contract_ha.async_restore(
                hass, entry, "subentries", "sub_pool", "schedule_start_hour"
            )
            is None
        )
    reload.assert_called_once_with(entry.entry_id)
    assert entry.subentries["sub_pool"].data["schedule_start_hour"] == 8
    assert (await _store(hass, entry).async_read())["revision"] == 1
    assert _issue_ids(hass, entry) == []


async def test_a_slider_commits_a_revision_in_active_and_nothing_otherwise(
    hass: HomeAssistant, hass_storage
):
    """Number write-through: in active, the slider's new value becomes a new
    revision and is not written back (it already is the value); in legacy
    nothing is stored at all."""
    entry, numbers = await _setup(hass)
    reg = er.async_get(hass)
    cap = reg.async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_solver_battery_capacity_kwh"
    )
    await device_contract_ha.async_number_changed(
        hass, entry, "solver_battery_capacity_kwh", 55.0
    )
    assert device_storage.store_key(entry.entry_id) not in hass_storage
    await _activate(hass, entry)
    hass.states.async_set(cap, "55.0")  # the entity writes its state first
    await device_contract_ha.async_number_changed(
        hass, entry, "solver_battery_capacity_kwh", 55.0
    )
    state = await _store(hass, entry).async_read()
    assert (
        state["revision"] == 2
        and state["origin"] == "number:solver_battery_capacity_kwh"
    )
    assert _device(state["document"], "battery")["capacity"]["value"] == 55.0
    assert numbers["solver_battery_capacity_kwh"].written == []
    assert entry.options[MARKER] == 2
    # A number the document doesn't own commits nothing.
    await device_contract_ha.async_number_changed(
        hass, entry, "solver_price_spike_threshold", 1.0
    )
    assert (await _store(hass, entry).async_read())["revision"] == 2


async def test_two_sliders_at_once_commit_one_after_the_other(
    hass: HomeAssistant, hass_storage
):
    """The per-entry lock (Mark Purcell, #1878): two commits started together
    run one after the other, each on the revision the other left, and the
    reload counter ends where it started."""
    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)

    async def listener(hass_, entry_):
        device_contract_ha.writing_views(hass_, entry_.entry_id)

    entry.add_update_listener(listener)
    reg = er.async_get(hass)
    for key, value in (
        ("solver_max_charge_kw", 7.0),
        ("solver_grid_max_import_kw", 15.0),
    ):
        hass.states.async_set(
            reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}"),
            str(value),
        )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await asyncio.gather(
            device_contract_ha.async_number_changed(
                hass, entry, "solver_max_charge_kw", 7.0
            ),
            device_contract_ha.async_number_changed(
                hass, entry, "solver_grid_max_import_kw", 15.0
            ),
        )
        await hass.async_block_till_done()
    state = await _store(hass, entry).async_read()
    assert state["revision"] == 3 and state["view_revision"] == 3
    view = device_import.legacy_view(state["document"])["numbers"]
    assert (
        view["solver_max_charge_kw"] == 7.0
        and view["solver_grid_max_import_kw"] == 15.0
    )
    assert (
        hass.data.get(f"{DOMAIN}_device_contract_own_updates", {}).get(
            entry.entry_id, 0
        )
        == 0
    )


async def test_the_drift_repair_offers_keep_and_restore_and_applies_the_choice(
    hass: HomeAssistant, hass_storage
):
    from custom_components.nimbus_load import repairs

    entry, _numbers = await _setup(hass)
    await _activate(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "grid_sensor": "sensor.meter2"}
    )
    await device_contract_ha.async_check_drift(hass, entry)
    (issue_id,) = _issue_ids(hass, entry)
    data = ir.async_get(hass).async_get_issue(DOMAIN, issue_id).data
    flow = await repairs.async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] == "menu" and menu["menu_options"] == ["keep", "restore"]
    with patch.object(hass.config_entries, "async_schedule_reload"):
        done = await flow.async_step_restore()
    assert done["type"] == "create_entry"
    assert entry.options["grid_sensor"] == "sensor.meter_power"


async def test_the_device_setup_mode_select_changes_mode_and_says_why_not(
    hass: HomeAssistant, hass_storage
):
    """Household, 19 Oct 2026: "where will these be toggled on or off from?"
    -- `select.nimbus_device_setup_mode`, on the Solver tab. It goes through
    the same checked mode change as the service, so a refusal changes
    nothing and says why."""
    entry, _numbers = await _setup(hass)
    before = _settings(hass, entry)
    select = NimbusDeviceSetupModeSelect(entry, "test")
    select.hass = hass
    with patch.object(select, "async_write_ha_state"):
        await select._async_refresh()
        assert select.options == ["legacy", "shadow", "active"]
        assert select.current_option == "legacy"

        with pytest.raises(HomeAssistantError, match="only be entered from shadow"):
            await select.async_select_option("active")
        assert select.current_option == "legacy"
        assert _settings(hass, entry) == before

        await select.async_select_option("shadow")
        assert select.current_option == "shadow"
        attrs = select.extra_state_attributes
        assert attrs["revision"] == 1 and attrs["dry_run_clean"] is True
        assert attrs["dry_run_changes"] == attrs["dry_run_conflicts"] == 0
        assert attrs["drift_repairs"] == 0
        assert _settings(hass, entry) == before

        await select.async_select_option("active")
        assert select.current_option == "active"
        assert _settings(hass, entry)["options"] == {**before["options"], MARKER: 1}

        pool = entry.subentries["sub_pool"]
        hass.config_entries.async_update_subentry(
            entry, pool, data={**pool.data, "schedule_start_hour": 9}
        )
        assert await device_contract_ha.async_check_drift(hass, entry) == 1
        await select._async_refresh()
        assert select.extra_state_attributes["drift_repairs"] == 1

        await select.async_select_option("legacy")
        assert select.current_option == "legacy"
    assert (await _store(hass, entry).async_read())["mode"] == "legacy"


async def test_only_an_active_setup_hands_the_solver_its_solar_forecast(
    hass: HomeAssistant, hass_storage
):
    """DC-006 slice 3: the Solver reads its solar forecast through the
    document's binding only while the device setup is `active`; in every
    other mode it reads its settings as today."""
    entry, _numbers = await _setup(hass)
    assert await device_contract_ha.async_active_solar_forecast(hass, entry) is None
    assert (await device_contract_ha.async_set_mode(hass, entry, "shadow"))["ok"]
    assert await device_contract_ha.async_active_solar_forecast(hass, entry) is None
    assert (await device_contract_ha.async_set_mode(hass, entry, "active"))["ok"]
    binding = await device_contract_ha.async_active_solar_forecast(hass, entry)
    assert binding is not None
    assert binding["source"]["kind"] == "entity_attribute"
    assert binding["source"]["entity_id"] == "sensor.pv_forecast"
    assert binding["source"]["adapter"]["name"] == "nimbus_forecast"

    # test_configure_device_setup_changes_the_mode_through_the_same_checks
    # DC-007 slice 2 (#1574): Configure -> Device setup. In `active` it offers
    # Review and Mode; Mode goes through async_set_mode's own checks; in
    # `legacy` it offers only the dry run, whose tick moves to `shadow`.
    from homeassistant.data_entry_flow import FlowResultType

    async def _open(step):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "device_setup" in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "device_setup"}
        )
        assert flow["type"] == FlowResultType.MENU
        menu = flow["menu_options"]
        if step is None:
            return flow, menu
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": step}
        )
        return flow, menu

    # test_edit_a_source_switches_one_role_and_its_setting_follows
    # DC-007 slice 3 (#1574), Advanced: a new grid power sensor, validated by
    # DC-002 first, is committed; in `active` the setting it fills follows.
    hass.states.async_set("sensor.new_meter", "0.8", {"unit_of_measurement": "kW"})
    flow, menu = await _open("device_setup_edit")
    assert menu == ["device_setup_review", "device_setup_edit", "device_setup_mode"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"device": "grid"}
    )
    assert flow["step_id"] == "device_setup_edit_source"
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"role": "power", "entity_id": "sensor.new_meter"}
    )
    assert done["type"] == FlowResultType.CREATE_ENTRY, done
    stored = await _store(hass, entry).async_read()
    grid = next(d for d in stored["document"]["devices"] if d["id"] == "grid")
    assert grid["bindings"]["power"]["source"]["entity_id"] == "sensor.new_meter"
    assert entry.options.get("grid_sensor") == "sensor.new_meter"
    # A sensor that doesn't exist is refused, and nothing changes.
    before = await _store(hass, entry).async_read()
    refused = await device_contract_ha.async_set_source(
        hass, entry, before["revision"], "grid", "power", "sensor.nowhere"
    )
    assert refused["ok"] is False and refused["code"] == "binding_refused"
    assert await _store(hass, entry).async_read() == before

    flow, menu = await _open("device_setup_mode")
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"mode": "legacy"}
    )
    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert (await _store(hass, entry).async_read())["mode"] == "legacy"

    flow, menu = await _open("device_setup_dry_run")
    assert menu == ["device_setup_dry_run"]
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"go_shadow": True}
    )
    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert (await _store(hass, entry).async_read())["mode"] == "shadow"
    # The edits above reloaded the entry (a view write in `active`); let the
    # reload finish and unload it, so no timer outlives the test.
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
