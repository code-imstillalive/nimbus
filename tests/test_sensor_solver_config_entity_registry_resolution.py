"""Regression test for nimbus issue #343 (Mark Purcell, codebase review),
the "wrong-entity read" defect: NimbusSolverConfigSensor._resolve() used
to guess a hardware-limit entity's entity_id as the literal string
f"number.nimbus_{key}" (or f"switch.nimbus_{key}") -- but number.py/
switch.py only pin THEIR OWN self.entity_id to that same non-entry-
scoped literal; their real unique_id is entry-scoped
(f"{entry.entry_id}_{key}"). If anything else in the same HA instance
claims the literal name first (a remote_homeassistant mirror of another
Nimbus install using the identical convention -- confirmed live on
devhub; an orphaned registry row), HA's own dedup bumps Nimbus's real
entity to `_2`/`_3`, and the guessed literal then silently resolves to
the FOREIGN entity's state. The LP would plan against another
install's battery capacity / grid limits with zero error.

Fix: resolve via the entity registry's own async_get_entity_id(domain,
DOMAIN, unique_id) -- which HA tracks correctly regardless of any name
collision -- falling back to the guessed literal only when the registry
has no match.

Real, direct construction of NimbusSolverConfigSensor (bypassing HA's
add-to-hass lifecycle, same technique as test_sensor_solver_config_
flap.py) against tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import CONF_SOLVER_BATTERY_CAPACITY_KWH


def _entry_with_id(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {}
    return entry


def _construct_bridge_sensor(entry: MagicMock) -> sensor.NimbusSolverConfigSensor:
    instance = sensor.NimbusSolverConfigSensor.__new__(sensor.NimbusSolverConfigSensor)
    sensor.NimbusSolverConfigSensor.__init__(instance, entry, sw_version="0.94.72")
    return instance


def test_falls_back_to_the_guessed_literal_when_registry_has_no_match():
    """The default/common case -- no collision, entity registered exactly
    where expected. Registry lookup misses (returns None, matching a
    fresh/never-collided install), so behaviour is byte-identical to
    before this fix."""
    entry = _entry_with_id("entry-1")
    instance = _construct_bridge_sensor(entry)
    instance.hass = MagicMock()
    instance.hass.states.get = MagicMock(return_value=MagicMock(state="122.2"))

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        value = instance._resolve(CONF_SOLVER_BATTERY_CAPACITY_KWH)

    assert value == 122.2
    instance.hass.states.get.assert_called_once_with(
        f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}"
    )


def test_resolves_through_a_renamed_entity_when_the_literal_name_is_claimed():
    """The real bug: something else (a mirror, an orphaned row) has
    already claimed number.nimbus_battery_capacity_kwh, so HA's own
    dedup registered Nimbus's REAL entity as the _2-suffixed name. The
    registry lookup (keyed by the entry-scoped unique_id, which nothing
    else can collide with) must resolve to that real name -- not the
    guessed literal, which would read the FOREIGN entity's state."""
    entry = _entry_with_id("entry-1")
    instance = _construct_bridge_sensor(entry)
    instance.hass = MagicMock()

    real_state = MagicMock(state="99.9")
    foreign_state = MagicMock(state="1.0")

    def fake_states_get(entity_id: str):
        return {
            f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}": foreign_state,
            f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}_2": real_state,
        }.get(entity_id)

    instance.hass.states.get = MagicMock(side_effect=fake_states_get)

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = (
            f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}_2"
        )
        value = instance._resolve(CONF_SOLVER_BATTERY_CAPACITY_KWH)

    assert value == 99.9, (
        "resolved the FOREIGN entity's value instead of following the "
        "entity registry to Nimbus's own real (renamed) entity"
    )


def test_registry_lookup_uses_the_real_entry_scoped_unique_id_convention():
    """Locks in the exact unique_id shape number.py/switch.py themselves
    use (f"{entry.entry_id}_{key}") -- if either side of this convention
    ever drifts, this test catches it instead of the registry lookup
    silently always missing."""
    entry = _entry_with_id("my-real-entry-id")
    instance = _construct_bridge_sensor(entry)
    instance.hass = MagicMock()
    instance.hass.states.get = MagicMock(return_value=MagicMock(state="1"))

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        instance._resolve(CONF_SOLVER_BATTERY_CAPACITY_KWH)

    mock_async_get.return_value.async_get_entity_id.assert_called_once_with(
        "number",
        sensor.DOMAIN,
        f"my-real-entry-id_{CONF_SOLVER_BATTERY_CAPACITY_KWH}",
    )


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
