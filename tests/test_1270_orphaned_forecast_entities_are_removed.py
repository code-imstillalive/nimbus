"""nimbus issue #1270: a forecast entity whose subentry no longer exists is
removed, instead of sitting in the registry forever holding its entity_id.

## Why Home Assistant's own cleanup does not already cover this

`ConfigEntries.async_remove_subentry()` calls
`ent_reg.async_clear_config_subentry(...)`, so a subentry removed *today* takes
its entities with it. `test_stale_devices_cleanup` states that mechanism's own
limit exactly:

> HA's automatic cleanup can't recover devices it was never told belong to a
> subentry.

Entities registered before `config_subentry_id` was threaded through (#645/#680
document the number.py side of the same gap) carry no subentry link. When their
subentry went away, core had nothing to clear.

## Measured, on two independent installs

2026-09-26, production and devhub both carry
`sensor.nimbus_mirror_{temperature,humidity}_forecast` reading `unknown`, left
behind by signal subentries that no longer exist. A registry read on one orphan
returns `config_entry_id: null` and no subentry link; a healthy sibling returns
`unique_id: 01M0YEZP7YDF5S4BDP8T9RQ126_signal_forecast` with both fields set.

## The decision this file exists to pin

`TestTheDiscriminatorIsTheUniqueId` is the important one. The fix originally
proposed on the issue filtered on `config_subentry_id not in entry.subentries`,
which would have matched **nothing** — the field these orphans are missing *is*
`config_subentry_id`. The unique_id embeds the owning subentry id verbatim
(`f"{subentry.subentry_id}{suffix}"`), so it still identifies the owner of an
entity that has lost its link. A future refactor "simplifying" this to the
subentry link would silently reopen the whole bug.

## Why removal rather than a warning

An orphan keeps its `entity_id` reserved, so the entity that should own that
name is bumped to `_2` — an id change on a live entity, which is what
dashboards and automations reference. The rename pass cannot repair it either:
it explicitly skips when the target name is taken, and an orphan is exactly
something taking it. And there is no user-side remedy at all — HA greys out
delete for an entity belonging to a loaded config entry, which the reference
household confirmed on both installs.

## Approach

A fake registry, because the real one needs a running HA. The fake records what
was removed, which is the only observable that matters here.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init

CLEANUP = nimbus_init._async_remove_orphaned_forecast_entities


@dataclass
class FakeRegistryEntry:
    entity_id: str
    unique_id: str


@dataclass
class FakeRegistry:
    entries: list[FakeRegistryEntry]
    removed: list[str] = field(default_factory=list)

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self.entries = [e for e in self.entries if e.entity_id != entity_id]


@dataclass
class FakeEntry:
    entry_id: str
    subentries: dict


def _run(entries, subentry_ids, entry_id="hub1"):
    """Drive the cleanup against a fake registry; return what it removed."""
    registry = FakeRegistry(entries=list(entries))
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
    return registry.removed


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


class TestTheOrphanIsRemoved(unittest.TestCase):
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

    def test_the_source_matches_on_unique_id(self):
        src = inspect.getsource(CLEANUP)
        self.assertIn("unique_id", src)
        self.assertIn("_signal_forecast", src)
        self.assertIn("_load_forecast", src)

    def test_the_source_does_NOT_filter_on_config_subentry_id(self):
        body = inspect.getsource(CLEANUP).split('"""')[-1]
        self.assertNotIn(
            "config_subentry_id",
            body,
            "filtering on the subentry link would miss every orphan this "
            "exists to remove -- the missing link IS the symptom (#1270)",
        )

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


class TestItIsWiredIn(unittest.TestCase):
    def test_setup_calls_it(self):
        src = inspect.getsource(nimbus_init)
        self.assertIn(
            "await _async_remove_orphaned_forecast_entities(hass, entry)", src
        )

    def test_it_runs_AFTER_the_rename_pass(self):
        """Ordering is deliberate and documented: clearing an orphan frees the
        entity_id it squatted, and the freed name is taken up on the NEXT
        reload rather than this one. A rename and a removal racing over the
        same id in a single pass is precisely the collision the rename pass
        already refuses to risk."""
        src = inspect.getsource(nimbus_init)
        self.assertLess(
            src.index("await _async_rename_stale_forecast_entities(hass, entry)"),
            src.index("await _async_remove_orphaned_forecast_entities(hass, entry)"),
        )

    def test_it_is_inside_the_defensive_try_block(self):
        """Entity-registry drift is real but must never take the hub down --
        the existing block's own stated reasoning, inherited rather than
        re-argued."""
        src = inspect.getsource(nimbus_init)
        i = src.index("await _async_remove_orphaned_forecast_entities(hass, entry)")
        self.assertIn("except Exception:", src[i : i + 400])


if __name__ == "__main__":
    unittest.main(verbosity=2)
