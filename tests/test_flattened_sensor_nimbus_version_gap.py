"""Red test pinning a real gap found reviewing PR #973 ("Publish
nimbus_version on every push sensor"): `_FlattenedAttributeSensor.
extra_state_attributes` (custom_components/nimbus_load/sensor_flattened.py,
backs ~77 entities via create_flattened_entities()/create_flattened_entities_
current()/create_flattened_entities_flex()/etc.) never merges in
`nimbus_version`, even though that PR's stated goal was covering every push
sensor.

Contrast: `_NimbusSolverPushSensor.extra_state_attributes` (sensor.py) does
    return {**self._attrs, "nimbus_version": self._sw_version}
so every one of ITS entities carries a `nimbus_version` state attribute an
automation/script/API caller can read directly off
`states.get(entity_id).attributes`.

`_FlattenedAttributeSensor` has no equivalent -- `extra_state_attributes`
just returns `self._extra_attrs` (None unless the spec sets
`attrs_source_key` and the parent published a dict there; see the property's
own docstring). The sw_version passed to `__init__` is only ever folded into
`self._attr_device_info["sw_version"]` (the HA *device registry* field,
visible on the device page to a human) -- never into the entity's own state
attributes.

This test constructs a real `_FlattenedAttributeSensor` the same way
tests/test_sensor_flattened.py does (via `create_flattened_entities()`,
passing a real `sw_version`) and asserts `nimbus_version` is present in
`extra_state_attributes` and matches the constructor's `sw_version` --
mirroring `_NimbusSolverPushSensor`'s own contract. It is EXPECTED TO FAIL
against the current code: `extra_state_attributes` is `None` for every
freshly-constructed flattened entity (no `attrs_source_key` spec row has
been dispatched yet), so the assertion below fails clearly, naming the
missing `nimbus_version` key, rather than erroring out on construction or
import. This is a deliberate red test documenting the gap for whoever picks
up the fix -- do not "fix" this file by loosening the assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor_flattened


def _fake_entry(entry_id: str = "test-entry-flat-version-gap") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


@pytest.mark.xfail(
    reason=(
        "nimbus issue #997: _FlattenedAttributeSensor.extra_state_attributes "
        "never merges in nimbus_version, unlike _NimbusSolverPushSensor. "
        "strict=True so this flips to a loud XPASS failure the moment the "
        "fix lands, as a reminder to delete this marker."
    ),
    strict=True,
)
def test_flattened_entity_extra_state_attributes_should_include_nimbus_version():
    """Expected/correct behavior: every _FlattenedAttributeSensor's
    extra_state_attributes should carry `nimbus_version` matching the
    sw_version passed to create_flattened_entities(), the same contract
    _NimbusSolverPushSensor already honours. Currently FAILS: the real
    extra_state_attributes is None, so the dict never gains the key at all.
    """
    entry = _fake_entry()
    sw_version = "0.94.330"
    entities = sensor_flattened.create_flattened_entities(entry, sw_version=sw_version)
    for e in entities:
        e.hass = None

    entity = entities[0]
    attrs = entity.extra_state_attributes

    assert attrs is not None and "nimbus_version" in attrs, (
        f"{entity.entity_id}.extra_state_attributes is missing the "
        "'nimbus_version' key entirely (got "
        f"{attrs!r}) -- _FlattenedAttributeSensor.extra_state_attributes "
        "never merges nimbus_version in, unlike "
        "_NimbusSolverPushSensor.extra_state_attributes in sensor.py, "
        'which does `return {**self._attrs, "nimbus_version": '
        "self._sw_version}`. The sw_version passed to __init__ only ever "
        "reaches self._attr_device_info['sw_version'] (the HA device-"
        "registry field, visible on the device page) -- never a state "
        "attribute an automation/script/API caller reading "
        "states.get(entity_id).attributes would see."
    )
    assert attrs["nimbus_version"] == sw_version
