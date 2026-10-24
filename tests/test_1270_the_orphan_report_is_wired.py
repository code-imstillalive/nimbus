"""nimbus #1270: setup runs the orphaned-forecast-entity report after the
rename pass, inside its defensive try block. Checks that read source text;
the report itself is in tests/v2/entities/test_orphaned_forecast_entities.py
(#1837)."""

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

CLEANUP = nimbus_init._async_report_orphaned_forecast_entities


@dataclass
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


class TestItIsWiredIn(unittest.TestCase):
    def test_setup_calls_it(self):
        src = inspect.getsource(nimbus_init)
        self.assertIn(
            "await _async_report_orphaned_forecast_entities(hass, entry)", src
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
            src.index("await _async_report_orphaned_forecast_entities(hass, entry)"),
        )

    def test_it_is_inside_the_defensive_try_block(self):
        """Entity-registry drift is real but must never take the hub down --
        the existing block's own stated reasoning, inherited rather than
        re-argued."""
        src = inspect.getsource(nimbus_init)
        i = src.index("await _async_report_orphaned_forecast_entities(hass, entry)")
        self.assertIn("except Exception:", src[i : i + 400])
