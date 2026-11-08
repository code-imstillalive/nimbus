"""The wizard's live numbers (#2393: flows/live_numbers.py's coverage, one behaviour per test): a value
Home Assistant cannot read as a number shows as blank, and a write goes only to a loaded Nimbus number,
everything else coming back as not written.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
const = importlib.import_module("custom_components.nimbus_load.const")
live = importlib.import_module("custom_components.nimbus_load.flows.live_numbers")

IDS = {
    "hub_capacity": "number.capacity",
    "hub_import": "number.import",
    "hub_foreign": "number.foreign",
}


def _registry():
    platforms = {"number.capacity": const.DOMAIN, "number.foreign": "other_integration"}
    return SimpleNamespace(
        async_get_entity_id=lambda domain, platform, uid: IDS.get(uid),
        async_get=lambda eid: (
            SimpleNamespace(platform=platforms[eid]) if eid in platforms else None
        ),
    )


def test_a_live_value_that_is_not_a_number_shows_blank():
    # test_a_live_value_that_is_not_a_number_shows_blank
    states = {
        "number.capacity": SimpleNamespace(state="unknown"),
        "number.import": SimpleNamespace(state="13.5"),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    with patch.object(live.er, "async_get", lambda h: _registry(), create=True):
        assert live.current_value(hass, "hub", "capacity") is None
        assert live.current_value(hass, "hub", "import") == 13.5


def test_only_a_loaded_nimbus_number_is_written():
    # test_only_a_loaded_nimbus_number_is_written
    capacity = SimpleNamespace(async_set_native_value=AsyncMock())
    component = SimpleNamespace(
        get_entity=lambda eid: capacity if eid == "number.capacity" else None
    )
    hass = SimpleNamespace(data={"entity_components": {"number": component}})
    with patch.object(live.er, "async_get", lambda h: _registry(), create=True):
        skipped = asyncio.run(
            live.async_apply(
                hass, "hub", {"capacity": 12, "import": 9, "foreign": 5, "missing": 1}
            )
        )
    capacity.async_set_native_value.assert_awaited_once_with(12.0)
    # "import" has no registry entry, "foreign" belongs to another integration, "missing" has no id
    assert skipped == ["import", "foreign", "missing"]
