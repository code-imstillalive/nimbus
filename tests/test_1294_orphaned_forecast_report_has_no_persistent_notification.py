"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1294, FIXED 2026-09-26): `_async_report_orphaned_forecast_entities()` (`__init__.py`,
nimbus #1270) reports a candidate orphan with a bare `_LOGGER.warning(...)`
and nothing else.

This repo already has an established convention for exactly this class of
situation -- "a real misconfiguration a household cannot fix without being
told about it, but must not spam every cycle" -- twice over:
`solver_writer.py`'s own `_notify_load_forecast_error_once()` (nimbus #66)
and `solver_runtime.py`'s missing-dependency notifier both fire a real
`persistent_notification.create` service call, deliberately not just a log
line, with the log-vs-notification distinction stated explicitly in
`solver_runtime.py`'s own comment: *"a real, clear persistent_notification,
fired once ... Fixed the same way solver_writer.py's own
_notify_load_forecast_error_once() already handles a different class of
setup failure."*

`_async_report_orphaned_forecast_entities()`'s own docstring gives the exact
same justification for needing to be found -- "a household cannot fix it by
hand, because HA greys out delete for an entity belonging to a loaded config
entry" -- but ships only a WARNING line, which requires the household to
already be watching the HA log (this project's own #594 standing directive
names a noisy-but-silent-by-default log as a known failure class). A real
instance is invisible in the default HA UI, exactly the gap the two sibling
mechanisms above already exist to close.

Uses the same fake-registry harness as
`tests/test_1270_orphaned_forecast_entities_are_reported.py`, extended with
a fake `hass.services.async_call` to observe whether a notification fires.
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


def _run(entries, subentry_ids, entry_id="hub1"):
    registry = FakeRegistry(entries=list(entries))
    hass = FakeHass()
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
    return hass.services.calls


LIVE = FakeRegistryEntry(
    "sensor.nimbus_archerfield_temp_forecast", "sub_live_signal_forecast"
)
ORPHAN = FakeRegistryEntry(
    "sensor.nimbus_mirror_temperature_forecast", "sub_gone_signal_forecast"
)


class TestAConfirmedOrphanRaisesAPersistentNotification(unittest.TestCase):
    def test_an_orphan_fires_a_persistent_notification(self):
        calls = _run([LIVE, ORPHAN], subentry_ids=["sub_live"])
        notification_calls = [
            c for c in calls if c[0] == "persistent_notification" and c[1] == "create"
        ]
        self.assertTrue(
            notification_calls,
            "a confirmed orphan (holding a live entity_id reserved, with no "
            "user-side remedy -- per this function's own docstring) must "
            "raise a real persistent_notification, the same as this repo's "
            "two sibling one-time-misconfiguration notifiers (#66, the "
            "missing-dependency notifier), not only a log line",
        )


if __name__ == "__main__":
    unittest.main()
