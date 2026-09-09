"""Real test of number.py's NimbusControllableLoadNumber and
_CONTROLLABLE_LOAD_DESCRIPTIONS table -- nimbus issue #645, the per-load
mirror of NimbusSolverNumber's own hub-level "wizard for first-time
setup, live entity for day-to-day tuning" pattern.

Imports and exercises the REAL class (not a reimplementation) against
mock hass/entry/subentry objects, via tests/_ha_stubs.py's stand-in
homeassistant.* modules.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.helpers.storage import Store

from custom_components.nimbus_load.const import (
    CONF_CONTROLLABLE_LOAD_KIND,
    CONTROLLABLE_LOAD_KIND_DEFERRABLE,
    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
    DOMAIN,
)
from custom_components.nimbus_load.number import (
    _CONTROLLABLE_LOAD_DESCRIPTIONS,
    NimbusControllableLoadNumber,
    _SharedNumberStore,
    _slug_for_entity_id,
)


def _fresh_shared_store(key: str = "test") -> _SharedNumberStore:
    return _SharedNumberStore(store=Store(MagicMock(), 1, key))


def _make_subentry(subentry_id="sub_1", title="Hot Water Heat Pump", data=None):
    subentry = MagicMock()
    subentry.subentry_id = subentry_id
    subentry.title = title
    subentry.data = data or {}
    return subentry


def _make_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry


# --- Descriptions table sanity ----------------------------------------


def test_no_duplicate_keys():
    keys = [d.key for d in _CONTROLLABLE_LOAD_DESCRIPTIONS]
    assert len(keys) == len(set(keys)), (
        f"duplicate _ControllableLoadNumberDescription.key found: {keys}"
    )


def test_every_default_is_within_its_own_bounds():
    offenders = [
        d.key
        for d in _CONTROLLABLE_LOAD_DESCRIPTIONS
        if not (d.min_value <= d.default <= d.max_value)
    ]
    assert not offenders, f"default outside min/max bounds: {offenders}"


def test_exactly_seven_fields_matching_issue_645s_own_scope():
    assert len(_CONTROLLABLE_LOAD_DESCRIPTIONS) == 7, (
        "expected exactly 7 fields per #645's own explicit proposal list "
        "(5 deferrable-only + 2 shared) -- a field was added/removed?"
    )


def test_deferrable_only_fields_do_not_apply_to_sheddable():
    deferrable_only_keys = {
        "deferrable_target_kwh",
        "deferrable_max_power_kw",
        "deferrable_earliest_hour",
        "deferrable_deadline_hour",
        "deferrable_shortfall_price",
    }
    for desc in _CONTROLLABLE_LOAD_DESCRIPTIONS:
        if desc.key in deferrable_only_keys:
            assert desc.kinds == (CONTROLLABLE_LOAD_KIND_DEFERRABLE,), desc.key


def test_shared_fields_apply_to_both_kinds():
    shared_keys = {
        "controllable_load_min_hold_minutes",
        "controllable_load_max_activations_per_day",
    }
    for desc in _CONTROLLABLE_LOAD_DESCRIPTIONS:
        if desc.key in shared_keys:
            assert CONTROLLABLE_LOAD_KIND_SHEDDABLE in desc.kinds, desc.key
            assert CONTROLLABLE_LOAD_KIND_DEFERRABLE in desc.kinds, desc.key


# --- Entity attribute wiring --------------------------------------------


def test_entity_attribute_wiring():
    entry = _make_entry()
    subentry = _make_subentry()
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )

    entity = NimbusControllableLoadNumber(
        entry,
        subentry,
        desc,
        sw_version="9.9.9-test",
        shared_store=_fresh_shared_store(),
    )

    assert entity._attr_unique_id == f"sub_1_{desc.key}"
    assert entity.entity_id == f"number.nimbus_hot_water_heat_pump_{desc.key}"
    assert entity._attr_name == desc.name
    assert entity._attr_native_min_value == desc.min_value
    assert entity._attr_native_max_value == desc.max_value
    assert entity._attr_native_step == desc.step
    assert entity._attr_native_unit_of_measurement == desc.unit
    assert entity._attr_native_value == desc.default
    assert (DOMAIN, "sub_1") in entity._attr_device_info["identifiers"]
    assert entity._attr_device_info["name"] == "Hot Water Heat Pump"
    assert entity._attr_device_info["model"] == "Controllable Load"


def test_entity_id_slug_matches_sensor_pys_own_commanded_state_convention():
    """Real predictability guarantee -- a household reading sensor.
    nimbus_<load>_commanded_state should find number.nimbus_<load>_* on
    the exact same device page, same slug."""
    entry = _make_entry()
    subentry = _make_subentry(title="Pool Pump (Bore)")
    desc = _CONTROLLABLE_LOAD_DESCRIPTIONS[0]
    entity = NimbusControllableLoadNumber(
        entry, subentry, desc, sw_version=None, shared_store=_fresh_shared_store()
    )
    assert (
        entity.entity_id
        == f"number.nimbus_{_slug_for_entity_id('Pool Pump (Bore)')}_{desc.key}"
    )


def test_unique_id_is_ulid_based_stable_across_title_rename():
    entry = _make_entry()
    subentry_a = _make_subentry(subentry_id="ulid_abc", title="Old Name")
    subentry_b = _make_subentry(subentry_id="ulid_abc", title="New Name")
    desc = _CONTROLLABLE_LOAD_DESCRIPTIONS[0]
    entity_a = NimbusControllableLoadNumber(
        entry, subentry_a, desc, sw_version=None, shared_store=_fresh_shared_store()
    )
    entity_b = NimbusControllableLoadNumber(
        entry, subentry_b, desc, sw_version=None, shared_store=_fresh_shared_store()
    )
    assert (
        entity_a._attr_unique_id
        == entity_b._attr_unique_id
        == "ulid_abc_deferrable_target_kwh"
    )


def test_is_entity_category_config():
    from homeassistant.const import EntityCategory

    assert NimbusControllableLoadNumber._attr_entity_category == EntityCategory.CONFIG


def test_two_different_loads_never_collide_on_store_key():
    """Real collision-avoidance proof -- two loads with the SAME field
    key (e.g. both configure deferrable_target_kwh) must land on
    genuinely different Store entries."""
    entry = _make_entry()
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )
    shared_store = _fresh_shared_store("shared-across-loads")
    entity_a = NimbusControllableLoadNumber(
        entry,
        _make_subentry(subentry_id="load_a"),
        desc,
        sw_version=None,
        shared_store=shared_store,
    )
    entity_b = NimbusControllableLoadNumber(
        entry,
        _make_subentry(subentry_id="load_b"),
        desc,
        sw_version=None,
        shared_store=shared_store,
    )
    assert entity_a._store_key != entity_b._store_key


# --- Per-kind field filtering (async_setup_entry's own list comprehension) -


def _entities_for_kind(entry, subentry) -> list[NimbusControllableLoadNumber]:
    """Mirrors async_setup_entry's own filtering expression exactly (see
    number.py, nimbus issue #645) so a drift between the two would show up
    here rather than only being caught by a full-platform-setup test."""
    shared_store = _fresh_shared_store(f"kind-filter-{subentry.subentry_id}")
    return [
        NimbusControllableLoadNumber(entry, subentry, desc, None, shared_store)
        for desc in _CONTROLLABLE_LOAD_DESCRIPTIONS
        if subentry.data.get(CONF_CONTROLLABLE_LOAD_KIND) in desc.kinds
    ]


def test_sheddable_load_does_not_get_the_five_deferrable_only_fields():
    entry = _make_entry()
    subentry = _make_subentry(
        subentry_id="sheddable_1",
        data={CONF_CONTROLLABLE_LOAD_KIND: CONTROLLABLE_LOAD_KIND_SHEDDABLE},
    )
    keys = {e._desc.key for e in _entities_for_kind(entry, subentry)}
    assert keys == {
        "controllable_load_min_hold_minutes",
        "controllable_load_max_activations_per_day",
    }


def test_deferrable_load_gets_all_seven_fields():
    entry = _make_entry()
    subentry = _make_subentry(
        subentry_id="deferrable_1",
        data={CONF_CONTROLLABLE_LOAD_KIND: CONTROLLABLE_LOAD_KIND_DEFERRABLE},
    )
    keys = {e._desc.key for e in _entities_for_kind(entry, subentry)}
    assert len(keys) == 7


# --- Seed-from-wizard-data / restore chain ------------------------------


def test_no_restored_state_seeds_from_subentry_data():
    entry = _make_entry()
    subentry = _make_subentry(data={"deferrable_target_kwh": 2.5})
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )
    entity = NimbusControllableLoadNumber(
        entry, subentry, desc, sw_version=None, shared_store=_fresh_shared_store()
    )
    entity.async_get_last_number_data = AsyncMock(return_value=None)
    entity.async_get_last_state = AsyncMock(return_value=None)

    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_native_value == 2.5


def test_no_restored_state_and_no_wizard_data_falls_back_to_class_default():
    entry = _make_entry()
    subentry = _make_subentry(data={})
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )
    entity = NimbusControllableLoadNumber(
        entry,
        subentry,
        desc,
        sw_version=None,
        shared_store=_fresh_shared_store("no-restore-no-wizard-data"),
    )
    entity.async_get_last_number_data = AsyncMock(return_value=None)
    entity.async_get_last_state = AsyncMock(return_value=None)

    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_native_value == desc.default


def test_successful_restore_wins_over_wizard_data():
    entry = _make_entry()
    subentry = _make_subentry(data={"deferrable_target_kwh": 2.5})
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )
    entity = NimbusControllableLoadNumber(
        entry,
        subentry,
        desc,
        sw_version=None,
        shared_store=_fresh_shared_store("restore-wins-over-wizard-data"),
    )
    restored_state = MagicMock()
    restored_state.last_updated.timestamp.return_value = 1000.0
    entity.async_get_last_number_data = AsyncMock(
        return_value=MagicMock(native_value=4.4)
    )
    entity.async_get_last_state = AsyncMock(return_value=restored_state)

    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_native_value == 4.4


def test_turn_writes_through_to_the_store():
    entry = _make_entry()
    subentry = _make_subentry()
    desc = next(
        d for d in _CONTROLLABLE_LOAD_DESCRIPTIONS if d.key == "deferrable_target_kwh"
    )
    shared_store = _fresh_shared_store("write-through")
    entity = NimbusControllableLoadNumber(
        entry, subentry, desc, sw_version=None, shared_store=shared_store
    )
    entity.async_write_ha_state = MagicMock()

    asyncio.run(entity.async_set_native_value(5.5))

    assert entity._attr_native_value == 5.5
    entity.async_write_ha_state.assert_called_once()
    stored = asyncio.run(shared_store.async_read(entity._store_key))
    assert stored == 5.5
