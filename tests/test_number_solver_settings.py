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

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.components.number import NumberDeviceClass  # noqa: E402
from custom_components.nimbus_load.const import DOMAIN  # noqa: E402
from custom_components.nimbus_load.number import _DESCRIPTIONS, NimbusSolverNumber  # noqa: E402

# Units with no real matching NumberDeviceClass (verified 2026-08-22 against
# HA core's own current DEVICE_CLASS_UNITS table) -- device_class must be
# None on every field carrying one of these.
_NO_DEVICE_CLASS_UNITS = {"$/kWh", "%", "hour", None}


def test_no_duplicate_keys():
    keys = [d.key for d in _DESCRIPTIONS]
    assert len(keys) == len(
        set(keys)
    ), f"duplicate _SolverNumberDescription.key found: {keys}"


def test_every_default_is_within_its_own_bounds():
    offenders = [
        d.key for d in _DESCRIPTIONS if not (d.min_value <= d.default <= d.max_value)
    ]
    assert not offenders, f"default outside min/max bounds: {offenders}"


def test_kw_fields_are_device_class_power():
    kw_fields = [d for d in _DESCRIPTIONS if d.unit == "kW"]
    assert kw_fields, "expected at least one kW field to exist"
    for d in kw_fields:
        assert (
            d.device_class == NumberDeviceClass.POWER
        ), f"{d.key}: expected POWER, got {d.device_class}"


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

    entity = NimbusSolverNumber(entry, desc, sw_version="9.9.9-test")

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

    entity = NimbusSolverNumber(entry, desc, sw_version=None)

    assert entity._attr_device_class is None
    assert entity._attr_native_unit_of_measurement == "%"


def test_every_solver_number_is_entity_category_config():
    """Gold entity-category (2026-08-23): every one of these 38 entities
    IS a Solver tuning knob by NimbusSolverNumber's own class definition
    -- unlike device_class (a real per-field judgment call, tested
    above), CONFIG applies uniformly here with no exceptions."""
    from homeassistant.const import EntityCategory

    assert NimbusSolverNumber._attr_entity_category == EntityCategory.CONFIG


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
