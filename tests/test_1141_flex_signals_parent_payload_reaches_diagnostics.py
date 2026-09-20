"""IV&V finding (5035f90..c881b52 pass, 2026-09-19): #496's own
`_flex_diagnostics()` claims `sensor.nimbus_flex_signals` carries no
parent payload -- it does, and three real fields never reach the
diagnostics dump.

`diagnostics.py:238-243`'s own docstring:

    `sensor.nimbus_flex_signals` carries **no payload attributes at
    all** -- measured live, its parent holds only entity metadata, and
    every per-signal value lives on a flattened child.

That measurement does not hold against the sensor as currently
implemented. `publish_flex_signals()` (`solver_writer.py:2877-2953`)
posts a real attribute payload on `sensor.nimbus_flex_signals` itself:
10 scalars (all covered by `FLATTENED_ATTRS_FLEX`'s flattened children,
so those DO reach diagnostics via the registry lookup) plus
`battery_signals`, `load_signals` and `generated_at` -- none of which
are flattened children and none of which `_flex_diagnostics()` reads,
because it never calls `hass.states.get("sensor.nimbus_flex_signals")`
at all.

This is the #116 failure class ("a diagnostics block quietly stops
reflecting real published output") this same commit was written to
avoid, relocated to the boundary between "spread the report whole" and
"resolve signals via the registry" rather than living inside either
mechanism.

This test constructs a fake `sensor.nimbus_flex_signals` state carrying
the real shape `publish_flex_signals()` posts and asserts
`_flex_diagnostics()`'s result includes `battery_signals`/`load_signals`/
`generated_at`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import diagnostics

_ENTRY_ID = "01TESTENTRY"


def _fake_state(state, attributes: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state, attributes=attributes)


def _fake_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    return entry


def _registry(mapping: dict[str, str | None]) -> MagicMock:
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
        mapping.get(unique_id)
    )
    return registry


def _hass(states: dict[str, SimpleNamespace | None]) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    return hass


def _flex(hass, registry):
    with patch(
        "homeassistant.helpers.entity_registry.async_get", return_value=registry
    ):
        return diagnostics._flex_diagnostics(hass, _fake_entry())


@pytest.mark.xfail(
    reason=(
        "nimbus IV&V (5035f90..c881b52, 2026-09-19): _flex_diagnostics() "
        "(diagnostics.py:296-321) never reads hass.states.get("
        "'sensor.nimbus_flex_signals') -- its own docstring's claim that "
        "this entity 'carries no payload attributes at all' is wrong for "
        "the real publish_flex_signals() shape (solver_writer.py:2877-"
        "2953), which posts battery_signals/load_signals/generated_at as "
        "real parent attributes. All three are silently absent from the "
        "diagnostics dump. See nimbus issue #1141."
    ),
    strict=True,
)
def test_the_flex_signals_parents_own_payload_reaches_diagnostics():
    real_attrs = {
        "unit_of_measurement": "kW",
        "friendly_name": "Nimbus Flex Signals",
        "grid_import_headroom_kw": 5.2,
        "grid_import_headroom_kwh": 2.6,
        "grid_export_headroom_kw": 1.1,
        "grid_export_headroom_kwh": 0.55,
        "forced_import_cost": 0.0,
        "forced_export_cost": 0.0,
        "flex_available_up_kw": 3.0,
        "flex_available_down_kw": 2.0,
        "load_headroom_up_kwh": 1.2,
        "load_headroom_down_kwh": 0.8,
        "battery_signals": [
            {
                "name": "home",
                "available_up_kw": 5.0,
                "available_down_kw": 4.0,
                "available_up_ranging_kw": None,
                "available_down_ranging_kw": None,
            }
        ],
        "load_signals": [
            {
                "name": "pool_pump",
                "intent": "shed",
                "band_min_kw": 0.0,
                "band_max_kw": 1.5,
                "reduced_cost_per_kwh": 0.12,
                "degenerate": False,
            }
        ],
        "generated_at": "2026-09-19T06:00:00+10:00",
    }
    hass = _hass({"sensor.nimbus_flex_signals": _fake_state("3.0", real_attrs)})

    result = _flex(hass, _registry({}))

    for key in ("battery_signals", "load_signals", "generated_at"):
        assert key in result, (
            f"{key!r} is a real attribute publish_flex_signals() posts on "
            "sensor.nimbus_flex_signals' own parent, but _flex_diagnostics() "
            "never reads that entity's state at all -- this field is "
            "silently missing from every diagnostics dump."
        )
        assert result[key] == real_attrs[key]


if __name__ == "__main__":
    import unittest

    unittest.main()
