"""nimbus issue #614 (Mark Purcell, real finding, re-filed from a
comment on the closed #579): a Controllable Load created before #579's
own fix (v0.94.185, entity_id built directly from the raw subentry_id)
keeps its stale sensor.nimbus_<ulid>_commanded_state entity_id forever,
even after upgrading past v0.94.186 -- self.entity_id on a SensorEntity
is only a suggestion on an entity's FIRST-ever registration; once a
registry row exists for a unique_id, HA's own entity_platform reuses
that row's stored entity_id on every later add. Confirmed live on
Mark's install, still true on v0.94.196.

Tests sensor._migrate_stale_commanded_state_entity_id() directly against
a minimal real (not MagicMock) fake entity registry -- same "build a
small real fake rather than lean on MagicMock" philosophy this project's
other stub-based tests already use (_FakeRunStateStore, _FakeServiceCalls,
etc.), needed here because tests/_ha_stubs.py's own homeassistant.helpers.
entity_registry stub is a bare MagicMock() with no real get/update
semantics to assert against.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor


class _FakeEntityRegistry:
    """Minimal real stand-in for homeassistant.helpers.entity_registry's
    EntityRegistry -- only the three methods
    _migrate_stale_commanded_state_entity_id() actually calls."""

    def __init__(self):
        self._by_unique_id: dict[tuple[str, str, str], str] = {}
        self._entity_ids: set[str] = set()

    def seed(self, domain: str, platform: str, unique_id: str, entity_id: str) -> None:
        self._by_unique_id[(domain, platform, unique_id)] = entity_id
        self._entity_ids.add(entity_id)

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        return self._by_unique_id.get((domain, platform, unique_id))

    def async_get(self, entity_id: str):
        return (
            SimpleNamespace(entity_id=entity_id)
            if entity_id in self._entity_ids
            else None
        )

    def async_update_entity(self, old_entity_id: str, new_entity_id: str) -> None:
        for key, value in list(self._by_unique_id.items()):
            if value == old_entity_id:
                self._by_unique_id[key] = new_entity_id
        self._entity_ids.discard(old_entity_id)
        self._entity_ids.add(new_entity_id)


def _fake_subentry(subentry_id: str, title: str):
    return SimpleNamespace(subentry_id=subentry_id, title=title)


class TestMigrateStaleCommandedStateEntityId(unittest.TestCase):
    _ULID = "01M20H3DYJ8DRBGP04KFSDBN6Z"  # real shape from Mark's own report

    def setUp(self):
        self._orig_async_get = sensor.er.async_get
        self._registry = _FakeEntityRegistry()
        sensor.er.async_get = lambda hass: self._registry

    def tearDown(self):
        sensor.er.async_get = self._orig_async_get

    def test_stale_pre_579_entity_id_is_migrated_to_the_slug_based_one(self):
        subentry = _fake_subentry(self._ULID, "Hot Water Heat Pump")
        unique_id = f"{self._ULID}_commanded_state"
        stale_entity_id = f"sensor.nimbus_{self._ULID.lower()}_commanded_state"
        self._registry.seed("sensor", sensor.DOMAIN, unique_id, stale_entity_id)

        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)

        self.assertEqual(
            self._registry.async_get_entity_id("sensor", sensor.DOMAIN, unique_id),
            "sensor.nimbus_hot_water_heat_pump_commanded_state",
        )
        # The stale id no longer resolves to anything.
        self.assertIsNone(self._registry.async_get(stale_entity_id))

    def test_a_household_renamed_entity_id_is_left_alone(self):
        # Mark's own explicit caution: "guarded so only ids matching the
        # sensor.nimbus_<ulid>_commanded_state shape are migrated,
        # leaving a household-renamed entity alone."
        subentry = _fake_subentry(self._ULID, "Hot Water Heat Pump")
        unique_id = f"{self._ULID}_commanded_state"
        custom_entity_id = "sensor.my_own_hws_name"
        self._registry.seed("sensor", sensor.DOMAIN, unique_id, custom_entity_id)

        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)

        self.assertEqual(
            self._registry.async_get_entity_id("sensor", sensor.DOMAIN, unique_id),
            custom_entity_id,
        )

    def test_no_existing_registry_row_is_a_no_op(self):
        # A genuinely fresh load (never registered before) -- must not
        # raise, must not create a phantom row.
        subentry = _fake_subentry(self._ULID, "Pool Pump")
        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)
        unique_id = f"{self._ULID}_commanded_state"
        self.assertIsNone(
            self._registry.async_get_entity_id("sensor", sensor.DOMAIN, unique_id)
        )

    def test_desired_entity_id_already_claimed_by_something_else_is_left_alone(self):
        # Defensive: never force a collision. A different unique_id
        # already owns the desired slug-based entity_id.
        subentry = _fake_subentry(self._ULID, "Hot Water Heat Pump")
        unique_id = f"{self._ULID}_commanded_state"
        stale_entity_id = f"sensor.nimbus_{self._ULID.lower()}_commanded_state"
        desired_entity_id = "sensor.nimbus_hot_water_heat_pump_commanded_state"
        self._registry.seed("sensor", sensor.DOMAIN, unique_id, stale_entity_id)
        self._registry.seed(
            "sensor", sensor.DOMAIN, "some_other_unique_id", desired_entity_id
        )

        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)

        # Unchanged -- migrating would have collided with the other row.
        self.assertEqual(
            self._registry.async_get_entity_id("sensor", sensor.DOMAIN, unique_id),
            stale_entity_id,
        )

    def test_already_migrated_is_idempotent(self):
        subentry = _fake_subentry(self._ULID, "Hot Water Heat Pump")
        unique_id = f"{self._ULID}_commanded_state"
        desired_entity_id = "sensor.nimbus_hot_water_heat_pump_commanded_state"
        self._registry.seed("sensor", sensor.DOMAIN, unique_id, desired_entity_id)

        # Must not raise or touch anything on a load that's already
        # correct (the normal case for every load created post-#579).
        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)
        sensor._migrate_stale_commanded_state_entity_id(hass=None, subentry=subentry)

        self.assertEqual(
            self._registry.async_get_entity_id("sensor", sensor.DOMAIN, unique_id),
            desired_entity_id,
        )


if __name__ == "__main__":
    unittest.main()
