"""The load wizard's "Suggested from your Energy Dashboard" line (#2393: flows/__init__.py's coverage,
one behaviour per test): a parent with no power sensor never matches, a missing upstream row suggests
nothing, the suggestion names the parent found through the device, and a failure only drops the hint.
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
flows = importlib.import_module("custom_components.nimbus_load.flows")
device_contract_ha = importlib.import_module(
    "custom_components.nimbus_load.device_contract_ha"
)
device_resolver = importlib.import_module(
    "custom_components.nimbus_load.device_resolver"
)

row = SimpleNamespace
ITEMS = [
    row(stat="sensor.c1_kwh", rate="sensor.c1_w", included_in=None),
    row(stat="sensor.pool_kwh", rate=None, included_in="sensor.c1_kwh"),
]
DEVICES = {"sensor.pool_kwh": "d_pool", "sensor.pool_w": "d_pool"}
OFFERED = [("nosensor", "No sensor"), ("c1", "Circuit 1")]


def test_a_parent_without_a_sensor_never_matches_and_a_missing_upstream_suggests_nothing():
    # test_a_parent_without_a_sensor_never_matches_and_a_missing_upstream_suggests_nothing
    suggest = flows.energy_parent_suggestion
    sensors = {"c1": "sensor.c1_w"}  # "nosensor" has none
    assert suggest(ITEMS, "sensor.pool_w", OFFERED, sensors, DEVICES.get) == "c1"
    orphan = [ITEMS[1]]  # the upstream "sensor.c1_kwh" row is not in the dashboard
    assert suggest(orphan, "sensor.pool_w", OFFERED, sensors, DEVICES.get) is None


def _hint(prefs_side_effect=None):
    entry = SimpleNamespace(
        subentries={"c1": SimpleNamespace(data={const.CONF_LOAD_SENSOR: "sensor.c1_w"})}
    )
    registry = SimpleNamespace(
        async_get=lambda eid: SimpleNamespace(device_id=DEVICES.get(eid))
    )
    with (
        patch.object(
            device_contract_ha,
            "_async_energy_prefs",
            AsyncMock(return_value={}, side_effect=prefs_side_effect),
        ),
        patch.object(device_resolver, "device_consumption", lambda prefs: ITEMS),
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            lambda hass: registry,
            create=True,
        ),
    ):
        return asyncio.run(
            flows.async_part_of_hint(object(), entry, "sensor.pool_w", OFFERED)
        )


def test_the_hint_names_the_parent_and_a_failure_only_drops_it():
    # test_the_hint_names_the_parent_and_a_failure_only_drops_it
    assert _hint() == " Suggested from your Energy Dashboard: **Circuit 1**."
    assert _hint(prefs_side_effect=RuntimeError("energy prefs unreadable")) == ""
    assert (
        asyncio.run(flows.async_part_of_hint(object(), None, "sensor.pool_w", [])) == ""
    )
