"""Real test of number.py's _DESCRIPTIONS table and NimbusSolverNumber's
entity-attribute wiring.

Imports and exercises the REAL module (not a reimplementation) against
mock hass/entry objects, via tests/_ha_stubs.py's stand-in homeassistant.*
modules -- the real `homeassistant` package isn't installed in this
project's local dev environment. Guards two real, previously-live-bitten
invariants: entity-device-class staying correct field-by-field (2026-08-22
Quality Scale work -- see number.py's own _SolverNumberDescription.
device_class comment for the exact "why POWER here, why None there"
reasoning this test locks in), and every field's default value staying
within its own min/max bounds (a config typo here would silently misbehave
in real HA, not raise anything obvious).
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
from homeassistant.components.number import NumberDeviceClass
from homeassistant.helpers.storage import Store

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.number import (
    _DESCRIPTIONS,
    NimbusSolverNumber,
    _SharedNumberStore,
)


def _fresh_shared_store(key: str = "test") -> _SharedNumberStore:
    return _SharedNumberStore(store=Store(MagicMock(), 1, key))


# Units with no real matching NumberDeviceClass (verified 2026-08-22 against
# HA core's own current DEVICE_CLASS_UNITS table) -- device_class must be
# None on every field carrying one of these.
_NO_DEVICE_CLASS_UNITS = {"$/kWh", "%", "hour", None}


def test_no_duplicate_keys():
    keys = [d.key for d in _DESCRIPTIONS]
    assert len(keys) == len(set(keys)), (
        f"duplicate _SolverNumberDescription.key found: {keys}"
    )


def test_every_default_is_within_its_own_bounds():
    offenders = [
        d.key for d in _DESCRIPTIONS if not (d.min_value <= d.default <= d.max_value)
    ]
    assert not offenders, f"default outside min/max bounds: {offenders}"


def test_kw_fields_are_device_class_power():
    kw_fields = [d for d in _DESCRIPTIONS if d.unit == "kW"]
    assert kw_fields, "expected at least one kW field to exist"
    for d in kw_fields:
        assert d.device_class == NumberDeviceClass.POWER, (
            f"{d.key}: expected POWER, got {d.device_class}"
        )


def test_battery_capacity_is_energy_storage():
    (d,) = [
        d
        for d in _DESCRIPTIONS
        if d.key == "battery_capacity_kwh" or "capacity" in d.key
    ]
    assert d.device_class == NumberDeviceClass.ENERGY_STORAGE


def test_p2p_bonus_volume_is_energy_not_energy_storage():
    (d,) = [
        d
        for d in _DESCRIPTIONS
        if d.unit == "kWh"
        and d.key != "battery_capacity_kwh"
        and "capacity" not in d.key
    ]
    assert d.device_class == NumberDeviceClass.ENERGY


def test_no_device_class_units_have_no_device_class():
    offenders = [
        d.key
        for d in _DESCRIPTIONS
        if d.unit in _NO_DEVICE_CLASS_UNITS and d.device_class is not None
    ]
    assert not offenders, (
        f"these fields have a unit with no real matching NumberDeviceClass, "
        f"but device_class isn't None: {offenders}"
    )


def test_every_kwh_or_kw_field_has_a_device_class():
    # The inverse check -- every field that DOES have a valid unit should
    # actually be using it, not left None by oversight.
    offenders = [
        d.key
        for d in _DESCRIPTIONS
        if d.unit in ("kW", "kWh") and d.device_class is None
    ]
    assert not offenders, f"kW/kWh field(s) missing a device_class: {offenders}"


def test_entity_attribute_wiring():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    desc = _DESCRIPTIONS[0]  # Battery Capacity, per the table's own real ordering

    entity = NimbusSolverNumber(
        entry, desc, sw_version="9.9.9-test", shared_store=_fresh_shared_store()
    )

    assert entity._attr_unique_id == f"test_entry_id_{desc.key}"
    assert entity.entity_id == f"number.nimbus_{desc.key}"
    assert entity._attr_name == desc.name
    assert entity._attr_native_min_value == desc.min_value
    assert entity._attr_native_max_value == desc.max_value
    assert entity._attr_native_step == desc.step
    assert entity._attr_native_unit_of_measurement == desc.unit
    assert entity._attr_device_class == desc.device_class
    assert entity._attr_native_value == desc.default
    assert (DOMAIN, entry.entry_id) in entity._attr_device_info["identifiers"]


def test_entity_wiring_carries_through_for_a_field_with_no_device_class():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    # Several fields share unit "%" (SoH, Min/Max SoC, Efficiency) -- any
    # one works for this test, just needs to be unambiguous, unlike the
    # single-match asserts above.
    desc = next(d for d in _DESCRIPTIONS if d.unit == "%")

    entity = NimbusSolverNumber(
        entry, desc, sw_version=None, shared_store=_fresh_shared_store()
    )

    assert entity._attr_device_class is None
    assert entity._attr_native_unit_of_measurement == "%"


def test_every_solver_number_is_entity_category_config():
    """Gold entity-category (2026-08-23): every one of these 38 entities
    IS a Solver tuning knob by NimbusSolverNumber's own class definition
    -- unlike device_class (a real per-field judgment call, tested
    above), CONFIG applies uniformly here with no exceptions."""
    from homeassistant.const import EntityCategory

    assert NimbusSolverNumber._attr_entity_category == EntityCategory.CONFIG


def _make_entry(options=None):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = options or {}
    return entry


# Real regression tests for the 2026-09-02 incident: RestoreNumber's own
# restore-state has a genuine startup timing race with zero real fallback
# for any field never set via the wizard (every P2P/network-fee/risk-
# aversion field). The Store added to number.py is the fix -- these tests
# exercise the REAL fallback chain (RestoreNumber -> Store -> entry.options
# -> class default), not a reimplementation.


def _last_state_at(timestamp: float) -> MagicMock:
    """A fake restored State whose own last_updated.timestamp() returns a
    given epoch float -- what NimbusSolverNumber.async_added_to_hass()
    now compares against the Store's own written_at (nimbus issue #342)."""
    state = MagicMock()
    state.last_updated.timestamp.return_value = timestamp
    return state


def test_successful_restore_backfills_the_store():
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("backfill")
    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    entity.async_get_last_number_data = AsyncMock(
        return_value=MagicMock(native_value=999.0)
    )
    entity.async_get_last_state = AsyncMock(return_value=_last_state_at(1000.0))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_native_value == 999.0
    assert asyncio.run(shared_store.async_read(desc.key)) == 999.0


def test_restore_older_than_store_does_not_overwrite_the_newer_store_value():
    """The real bug nimbus issue #342 fixes: a value set at 10:00, last
    restore-state dump at 09:55, container killed at 10:05 -- restoring
    the stale 09:55 value must NOT overwrite the Store's own correct,
    newer 10:00 value."""
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("store-newer-than-restore")
    # Simulate the Store's own more recent write (the real 10:00 edit).
    asyncio.run(shared_store.async_write(desc.key, 42.0))
    stored_at = asyncio.run(shared_store._async_read_entry(desc.key))[1]

    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    # The stale 09:55 restore -- older than the Store's own write.
    entity.async_get_last_number_data = AsyncMock(
        return_value=MagicMock(native_value=40.0)
    )
    entity.async_get_last_state = AsyncMock(
        return_value=_last_state_at(stored_at - 300)
    )
    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_native_value == 42.0, (
        "the newer Store value must win over a genuinely staler restore"
    )
    # And the Store itself must still hold the real, newer value -- not
    # have been clobbered by the stale restore.
    assert asyncio.run(shared_store.async_read(desc.key)) == 42.0


def test_restore_newer_than_store_wins_and_backfills():
    """The normal, common case: RestoreNumber genuinely has the freshest
    data (e.g. a clean shutdown just before this restart) -- it must
    still win and refresh the Store, same as before this fix."""
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("restore-newer-than-store")
    asyncio.run(shared_store.async_write(desc.key, 40.0))
    stored_at = asyncio.run(shared_store._async_read_entry(desc.key))[1]

    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    entity.async_get_last_number_data = AsyncMock(
        return_value=MagicMock(native_value=42.0)
    )
    entity.async_get_last_state = AsyncMock(
        return_value=_last_state_at(stored_at + 300)
    )
    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_native_value == 42.0
    assert asyncio.run(shared_store.async_read(desc.key)) == 42.0


def test_restore_miss_falls_back_to_the_store():
    """The actual bug this fix closes: RestoreNumber returns nothing (the
    real, live timing race), but the Store already has a real value from
    an earlier successful restore/edit -- that value must win, NOT the
    class default."""
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("restore-miss")
    asyncio.run(shared_store.async_write(desc.key, 42.0))

    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    entity.async_get_last_number_data = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_native_value == 42.0


def test_restore_and_store_both_miss_falls_back_to_options_seed():
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("both-miss")
    entity = NimbusSolverNumber(
        _make_entry(options={desc.key: 7.0}),
        desc,
        sw_version=None,
        shared_store=shared_store,
    )
    entity.async_get_last_number_data = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_native_value == 7.0
    # The options-seed path backfills the Store too, same reasoning as a
    # successful RestoreNumber restore.
    assert asyncio.run(shared_store.async_read(desc.key)) == 7.0


def test_everything_misses_keeps_the_constructor_default():
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("everything-misses")
    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    entity.async_get_last_number_data = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_native_value == desc.default


def test_set_native_value_writes_through_to_the_store():
    desc = _DESCRIPTIONS[0]
    shared_store = _fresh_shared_store("set-value")
    entity = NimbusSolverNumber(
        _make_entry(), desc, sw_version=None, shared_store=shared_store
    )
    entity.async_write_ha_state = MagicMock()
    asyncio.run(entity.async_set_native_value(55.5))
    assert asyncio.run(shared_store.async_read(desc.key)) == 55.5


def test_store_is_genuinely_shared_across_sibling_entities():
    """The real reason _SharedNumberStore exists rather than one Store per
    entity: all 38 fields must live in the same JSON file, so a write
    from one entity is immediately visible to a sibling entity reading
    the same key."""
    shared_store = _fresh_shared_store("shared-across-siblings")
    desc_a, desc_b = _DESCRIPTIONS[0], _DESCRIPTIONS[1]
    entity_a = NimbusSolverNumber(
        _make_entry(), desc_a, sw_version=None, shared_store=shared_store
    )
    entity_a.async_write_ha_state = MagicMock()
    asyncio.run(entity_a.async_set_native_value(11.0))

    entity_b = NimbusSolverNumber(
        _make_entry(), desc_b, sw_version=None, shared_store=shared_store
    )
    entity_b.async_get_last_number_data = AsyncMock(return_value=None)
    asyncio.run(entity_b.async_added_to_hass())
    # entity_b's own key was never written -- must not see entity_a's
    # value under its own key, only its own.
    assert entity_b._attr_native_value == desc_b.default
    # But entity_a's key IS visible to a fresh read through the same
    # shared store, proving it's one shared file, not per-entity.
    assert asyncio.run(shared_store.async_read(desc_a.key)) == 11.0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
