"""nimbus #1589: Energy Dashboard grid sources are read on both schemas.

HA 2026.3 replaced the grid source's flow_from / flow_to lists with one flat
source per import/export pair, migrating existing preferences itself. The
Solver's price suggestions and the switchboard's grid suggestions read only
the lists, so on current HA they silently found nothing.

`_ha_migrate` below is HA's own `_migrate_legacy_grid_to_unified`
(homeassistant/components/energy/data.py, identical in 2026.3.0, 2026.7.4 and
2026.9.3), copied for the power-free fields: `as_flow_lists` must be its
inverse, so a source HA migrated reads back as the lists it came from.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
)
from custom_components.nimbus_load.flows.hub_options import (
    _energy_dashboard_switchboard_suggestions,
)

_SPEC = importlib.util.spec_from_file_location(
    "_energy_prefs",
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "energy_prefs.py",
)
ep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ep)


def _ha_migrate(old_grid):
    """HA's _migrate_legacy_grid_to_unified, without the power fields."""
    flow_from = old_grid.get("flow_from", [])
    flow_to = old_grid.get("flow_to", [])
    out = []
    for i in range(max(len(flow_from), len(flow_to))):
        source = {"type": "grid", "cost_adjustment_day": 0.0}
        if i < len(flow_from):
            ff = flow_from[i]
            source["stat_energy_from"] = ff.get("stat_energy_from") or None
            source["stat_cost"] = ff.get("stat_cost")
            source["entity_energy_price"] = ff.get("entity_energy_price")
            source["number_energy_price"] = ff.get("number_energy_price")
        else:
            source["stat_energy_from"] = None
            source["stat_cost"] = None
            source["entity_energy_price"] = None
            source["number_energy_price"] = None
        if i < len(flow_to):
            ft = flow_to[i]
            source["stat_energy_to"] = ft.get("stat_energy_to")
            source["stat_compensation"] = ft.get("stat_compensation")
            source["entity_energy_price_export"] = ft.get("entity_energy_price")
            source["number_energy_price_export"] = ft.get("number_energy_price")
        else:
            source["stat_energy_to"] = None
            source["stat_compensation"] = None
            source["entity_energy_price_export"] = None
            source["number_energy_price_export"] = None
        out.append(source)
    return out


LEGACY = {
    "type": "grid",
    "flow_from": [
        {
            "stat_energy_from": "sensor.import_kwh",
            "stat_cost": None,
            "entity_energy_price": "sensor.buy_price",
            "number_energy_price": None,
        }
    ],
    "flow_to": [
        {
            "stat_energy_to": "sensor.export_kwh",
            "stat_compensation": None,
            "entity_energy_price": None,
            "number_energy_price": 0.05,
        }
    ],
}


def test_a_migrated_source_reads_back_as_the_lists_it_came_from():
    (unified,) = _ha_migrate(LEGACY)
    back = ep.as_flow_lists(unified)
    assert back["flow_from"] == LEGACY["flow_from"]
    assert back["flow_to"] == LEGACY["flow_to"]


def test_an_import_only_or_export_only_connection_has_one_list():
    imp, exp = _ha_migrate(
        {
            "type": "grid",
            "flow_from": [
                {"stat_energy_from": "sensor.a"},
                {"stat_energy_from": "sensor.b"},
            ],
            "flow_to": [{"stat_energy_to": "sensor.c"}],
        }
    )
    assert [f["stat_energy_from"] for f in ep.as_flow_lists(exp)["flow_from"]] == [
        "sensor.b"
    ]
    assert ep.as_flow_lists(exp)["flow_to"] == []
    assert [f["stat_energy_to"] for f in ep.as_flow_lists(imp)["flow_to"]] == [
        "sensor.c"
    ]


def test_legacy_and_non_grid_sources_pass_through_unchanged():
    assert ep.as_flow_lists(LEGACY) is LEGACY
    solar = {"type": "solar", "stat_energy_from": "sensor.pv"}
    assert ep.as_flow_lists(solar) is solar
    assert ep.as_flow_lists(None) is None


def test_energy_sources_handles_missing_prefs():
    assert ep.energy_sources(None) == []
    assert ep.energy_sources({}) == []
    assert ep.energy_sources({"energy_sources": None}) == []


def _state(device_class, state_class="total_increasing"):
    st = MagicMock()
    st.attributes = {"device_class": device_class, "state_class": state_class}
    return st


def test_switchboard_suggestions_on_the_unified_grid_schema():
    """Found nothing before the fix: the switchboard read only flow_from /
    flow_to."""
    states = {
        "sensor.import_kwh": _state("energy"),
        "sensor.export_kwh": _state("energy"),
        "sensor.buy_price": _state("monetary", "measurement"),
        "sensor.sell_price": _state("monetary", "measurement"),
    }
    hass = MagicMock()
    hass.states.get = states.get
    manager = MagicMock(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.import_kwh",
                    "stat_energy_to": "sensor.export_kwh",
                    "entity_energy_price": "sensor.buy_price",
                    "entity_energy_price_export": "sensor.sell_price",
                }
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result[CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR] == "sensor.import_kwh"
    assert result[CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR] == "sensor.export_kwh"
    assert result[CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR] == "sensor.buy_price"
    assert result[CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR] == "sensor.sell_price"
