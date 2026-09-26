"""IV&V finding (22205ad..88ddd26 pass, 2026-09-27, head issue #1319, this
finding #1320): `_async_report_orphaned_forecast_entities()` (`__init__.py`,
fixed for #1294 by `be60afa`) gained a real `persistent_notification.create`
call, closing the "invisible in the default HA UI" half of #1294's finding.

But it has no matching dismiss. The function's own `if not orphans: return`
early-exit only prevents the notification from being RE-CREATED on a later
setup where the condition no longer holds -- it never calls
`persistent_notification.dismiss()` to clear a notification already on
screen from an earlier setup where orphans WERE found. A household that saw
the notification, didn't act on it, and then had the underlying condition
resolve keeps seeing a stale notification indefinitely.

This is exactly the gap the fix's own code comment claims to have closed by
following the sibling pattern (`_notify_load_forecast_error_once()` /
`_clear_load_forecast_error_notification_if_needed()`, `solver_writer.py`
lines 4275-4367) -- that pair is matched: one creates, the other actively
calls `persistent_notification.dismiss()` when the condition clears.
`_async_report_orphaned_forecast_entities()` only has the create half.

Extends the same fake-registry/fake-services harness as
`tests/test_1294_orphaned_forecast_report_has_no_persistent_notification.py`,
run twice: once with a real orphan present (fires create), then again with
it gone (must fire dismiss for the same notification_id -- it doesn't).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init

CLEANUP = nimbus_init._async_report_orphaned_forecast_entities
NOTIFICATION_ID = "nimbus_orphaned_forecast_entities"


@dataclass
class FakeRegistryEntry:
    entity_id: str
    unique_id: str


@dataclass
class FakeRegistry:
    entries: list[FakeRegistryEntry]


@dataclass
class FakeEntry:
    entry_id: str
    subentries: dict


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain, service, data, *args, **kwargs):
        self.calls.append((domain, service, data))


@dataclass
class FakeHass:
    services: FakeServices = field(default_factory=FakeServices)


def _run(hass, entries, subentry_ids, entry_id="hub1"):
    registry = FakeRegistry(entries=list(entries))
    entry = FakeEntry(entry_id=entry_id, subentries={s: object() for s in subentry_ids})

    er = nimbus_init.er
    _MISSING = object()
    real_get = getattr(er, "async_get", _MISSING)
    real_for_entry = getattr(er, "async_entries_for_config_entry", _MISSING)
    er.async_get = lambda _hass: registry
    er.async_entries_for_config_entry = lambda _reg, _eid: list(registry.entries)
    try:
        asyncio.run(CLEANUP(hass, entry))
    finally:
        for name, original in (
            ("async_get", real_get),
            ("async_entries_for_config_entry", real_for_entry),
        ):
            if original is _MISSING:
                delattr(er, name)
            else:
                setattr(er, name, original)


LIVE = FakeRegistryEntry(
    "sensor.nimbus_archerfield_temp_forecast", "sub_live_signal_forecast"
)
ORPHAN = FakeRegistryEntry(
    "sensor.nimbus_mirror_temperature_forecast", "sub_gone_signal_forecast"
)


class TestAResolvedOrphanDismissesItsNotification(unittest.TestCase):
    def test_a_second_setup_with_the_orphan_gone_dismisses_the_notification(self):
        hass = FakeHass()

        # First setup: a real orphan is present, must fire create.
        _run(hass, [LIVE, ORPHAN], subentry_ids=["sub_live"])
        creates = [
            c
            for c in hass.services.calls
            if c[0] == "persistent_notification" and c[1] == "create"
        ]
        self.assertTrue(creates, "first setup with a real orphan must notify")

        # Second setup: the orphan is gone (e.g. the entity was cleaned up
        # through some other path). The condition has resolved -- the
        # earlier notification must be actively dismissed, not left stale.
        _run(hass, [LIVE], subentry_ids=["sub_live"])
        dismisses = [
            c
            for c in hass.services.calls
            if c[0] == "persistent_notification"
            and c[1] == "dismiss"
            and c[2].get("notification_id") == NOTIFICATION_ID
        ]
        self.assertTrue(
            dismisses,
            "a resolved orphan condition must actively dismiss the earlier "
            "persistent_notification (matching the sibling "
            "_clear_load_forecast_error_notification_if_needed() pattern), "
            "not just skip re-creating it",
        )


if __name__ == "__main__":
    unittest.main()
