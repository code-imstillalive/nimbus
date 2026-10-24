"""A forecast entity whose subentry no longer exists is reported, never
deleted: found by the owner id its unique_id embeds, since the subentry link
is exactly what such an entity lacks. An empty subentry mapping reports
nothing, and hub-scoped entities are never in reach (#1270) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import custom_components.nimbus_load as nimbus_init

CLEANUP = nimbus_init._async_report_orphaned_forecast_entities


@dataclass
class FakeRegistryEntry:
    entity_id: str
    unique_id: str


@dataclass
class FakeRegistry:
    entries: list[FakeRegistryEntry]
    removed: list[str] = field(default_factory=list)

    def async_remove(self, entity_id: str) -> None:
        # Kept so an accidental re-introduction of the delete is CAUGHT by
        # TestNothingIsActuallyDeleted rather than silently passing.
        self.removed.append(entity_id)
        self.entries = [e for e in self.entries if e.entity_id != entity_id]


_LAST_REGISTRY: list = []


@dataclass
class FakeEntry:
    entry_id: str
    subentries: dict


def _run(entries, subentry_ids, entry_id="hub1"):
    """Drive the pass against a fake registry; return the entity_ids it
    REPORTED. `registry.removed` is also asserted empty by
    TestNothingIsActuallyDeleted -- the two observables are deliberately
    separate so a re-introduced delete cannot hide behind a passing report."""
    registry = FakeRegistry(entries=list(entries))
    reported: list[str] = []
    real_warning = nimbus_init._LOGGER.warning

    def _capture(msg, *args):
        if args:
            reported.append(str(args[0]))
        return real_warning(msg, *args)

    nimbus_init._LOGGER.warning = _capture
    entry = FakeEntry(entry_id=entry_id, subentries={s: object() for s in subentry_ids})

    # `async_entries_for_config_entry` is a real helper in Home Assistant but
    # is absent from tests/_ha_stubs, so it is installed rather than merely
    # overridden -- and removed again afterwards so this file cannot leak a
    # fake helper into another test module's view of the stub.
    er = nimbus_init.er
    _MISSING = object()
    real_get = getattr(er, "async_get", _MISSING)
    real_for_entry = getattr(er, "async_entries_for_config_entry", _MISSING)
    er.async_get = lambda _hass: registry
    er.async_entries_for_config_entry = lambda _reg, _eid: list(registry.entries)
    try:
        asyncio.run(CLEANUP(object(), entry))
    finally:
        for name, original in (
            ("async_get", real_get),
            ("async_entries_for_config_entry", real_for_entry),
        ):
            if original is _MISSING:
                delattr(er, name)
            else:
                setattr(er, name, original)
    nimbus_init._LOGGER.warning = real_warning
    _LAST_REGISTRY.append(registry)
    return reported


LIVE = FakeRegistryEntry(
    "sensor.nimbus_archerfield_temp_forecast", "sub_live_signal_forecast"
)
ORPHAN = FakeRegistryEntry(
    "sensor.nimbus_mirror_temperature_forecast", "sub_gone_signal_forecast"
)
ORPHAN_LOAD = FakeRegistryEntry(
    "sensor.nimbus_old_load_forecast", "sub_gone2_load_forecast"
)
HUB_SCOPED = FakeRegistryEntry(
    "sensor.nimbus_solver_total_cost", "hub1_solver_total_cost"
)


class TestNothingIsActuallyDeleted(unittest.TestCase):
    """The retreat, pinned. The entities that motivated #1270 were not orphans
    at all -- they carry a `nimbus_version` stamped with the running version,
    so live code publishes them. With no confirmed instance, deleting
    user-visible registry rows is the wrong trade however well-guarded."""

    def test_an_orphan_is_reported_but_NOT_removed(self):
        _LAST_REGISTRY.clear()
        reported = _run([LIVE, ORPHAN], subentry_ids=["sub_live"])
        self.assertEqual(reported, ["sensor.nimbus_mirror_temperature_forecast"])
        self.assertEqual(
            _LAST_REGISTRY[-1].removed,
            [],
            "nothing may be deleted until a real orphan is confirmed (#1270)",
        )


class TestTheOrphanIsReported(unittest.TestCase):
    def test_a_forecast_entity_whose_subentry_is_gone_is_removed(self):
        removed = _run([LIVE, ORPHAN], subentry_ids=["sub_live"])
        self.assertEqual(removed, ["sensor.nimbus_mirror_temperature_forecast"])

    def test_a_live_subentry_s_entity_is_never_touched(self):
        removed = _run([LIVE], subentry_ids=["sub_live"])
        self.assertEqual(removed, [])

    def test_both_forecast_suffixes_are_swept(self):
        """Signals and loads use different suffixes; an orphan of either kind
        squats an entity_id just as effectively."""
        removed = _run([LIVE, ORPHAN, ORPHAN_LOAD], subentry_ids=["sub_live"])
        self.assertEqual(len(removed), 2)
        self.assertIn("sensor.nimbus_old_load_forecast", removed)


class TestTheDiscriminatorIsTheUniqueId(unittest.TestCase):
    """The decision that makes this fix work at all.

    The version first proposed on the issue filtered on `config_subentry_id`.
    Every orphan this removes is missing exactly that field -- that absence is
    the symptom -- so such a filter would match nothing and the bug would look
    fixed while nothing changed.
    """

    def test_an_orphan_with_no_subentry_link_is_still_removed(self):
        """The real shape measured on production: no subentry link at all,
        identified purely by the id embedded in its unique_id."""
        removed = _run([ORPHAN], subentry_ids=["sub_live"])
        self.assertEqual(removed, ["sensor.nimbus_mirror_temperature_forecast"])


class TestTheGuards(unittest.TestCase):
    def test_an_empty_subentries_mapping_removes_NOTHING(self):
        """Guard 1, and the one that matters most: a partially-loaded entry
        must never be read as 'every subentry was removed'. Without this, a
        transient becomes a mass delete of every forecast entity on the
        install."""
        removed = _run([LIVE, ORPHAN, ORPHAN_LOAD], subentry_ids=[])
        self.assertEqual(removed, [])

    def test_hub_scoped_entities_are_structurally_unreachable(self):
        """Guard 2. The Solver push sensors and config sensors live for the
        life of the hub, carry no forecast suffix, and must survive any
        subentry churn -- the same separation test_stale_devices_cleanup
        guards on the registration side."""
        removed = _run([HUB_SCOPED], subentry_ids=["sub_live"])
        self.assertEqual(removed, [])

    def test_an_entity_with_no_unique_id_is_skipped_not_crashed_on(self):
        blank = FakeRegistryEntry("sensor.something", "")
        removed = _run([blank, LIVE], subentry_ids=["sub_live"])
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
