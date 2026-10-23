"""A clean install, set up the way a household sets up Nimbus (household,
23 Oct 2026, devhub reinstall): real inverters, strings and towers plus an
EV, and only the dashboard Nimbus generates.

- The EV must be drawn on the Topology card beside the real inverters, not
  only on an install with no Topology subentries at all.
- The Solver tab's "Battery, now" card and "At a glance" ring show each
  battery's own SoC where there is more than one, not the fleet figure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from test_sensor_topology_config import (
    _fake_entry,
    _fake_subentry,
    _sensor_with_hass,
)

from custom_components.nimbus_load import sensor, solver_tab
from custom_components.nimbus_load.const import (
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
    CONF_BATTERY_PARTICIPANT_NAME,
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_POWER_SOURCE,
)


def _attrs(subentries: dict) -> dict:
    s = _sensor_with_hass(_fake_entry(subentries, {}))
    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        return s.extra_state_attributes


def test_an_ev_is_drawn_beside_real_inverters() -> None:
    attrs = _attrs(
        {
            "inv1": _fake_subentry(
                "inv1",
                SUBENTRY_TYPE_POWER_SOURCE,
                {CONF_POWER_SOURCE_NAME: "Inverter 1"},
            ),
            "t1": _fake_subentry(
                "t1",
                SUBENTRY_TYPE_BATTERY_TOWER,
                {
                    CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.tower_1_soc",
                    CONF_BATTERY_TOWER_POWER_SOURCE: "inv1",
                },
            ),
            "ev": _fake_subentry(
                "ev",
                SUBENTRY_TYPE_BATTERY_PARTICIPANT,
                {
                    CONF_BATTERY_PARTICIPANT_NAME: "Our EV",
                    CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.ev_soc",
                    CONF_BATTERY_PARTICIPANT_POWER_SENSOR: "sensor.ev_power",
                    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 60.0,
                },
            ),
        }
    )
    names = [p[CONF_POWER_SOURCE_NAME] for p in attrs["power_sources"]]
    assert names == ["Inverter 1", "Our EV"]
    # The real inverter is untouched: no home stand-in is derived beside it.
    assert "derived" not in attrs["power_sources"][0]
    towers = {t["title"]: t for t in attrs["battery_towers"]}
    assert towers["Our EV"][CONF_BATTERY_TOWER_SOC_SENSOR] == "sensor.ev_soc"
    assert towers["Our EV"]["capacity_kwh"] == 60.0
    assert towers["Our EV"][CONF_BATTERY_TOWER_POWER_SOURCE] == "derived_ev"


def test_without_an_ev_the_real_topology_is_exactly_as_before() -> None:
    attrs = _attrs(
        {
            "inv1": _fake_subentry(
                "inv1",
                SUBENTRY_TYPE_POWER_SOURCE,
                {CONF_POWER_SOURCE_NAME: "Inverter 1"},
            ),
        }
    )
    assert [p[CONF_POWER_SOURCE_NAME] for p in attrs["power_sources"]] == ["Inverter 1"]
    assert attrs["battery_towers"] == []


def _lookup(batteries: int):
    """Every Nimbus entity exists; per-battery SoC sensors only for the
    slots a site with `batteries` batteries has (none on a single one)."""

    def lookup(key: str) -> str | None:
        if key.startswith("name:nimbus_solver_battery_"):
            slot = int(key.split("_")[3])
            return "SoC Home battery" if slot == 0 else f"SoC EV {slot}"
        if key.startswith("nimbus_solver_battery_") and key.endswith("_soc_pct"):
            slot = int(key.split("_")[3])
            if batteries < 2 or slot >= batteries:
                return None
        if key.startswith(("option:", "card:")):
            return None
        return f"sensor.{key}"

    return lookup


def _battery_now(batteries: int) -> list:
    view = solver_tab.resolve(solver_tab.template(), _lookup(batteries))
    for section in view:
        for card in section.get("cards", []):
            if card.get("title") == "Battery, now":
                return card["entities"]
    raise AssertionError("no Battery, now card")


def test_a_single_battery_site_shows_the_one_state_of_charge() -> None:
    text = json.dumps(_battery_now(1))
    assert "sensor.nimbus_solver_current_soc_pct" in text
    assert "nimbus_solver_battery_0_soc_pct" not in text


def test_a_site_with_an_ev_shows_each_battery_not_the_fleet() -> None:
    text = json.dumps(_battery_now(2))
    assert "sensor.nimbus_solver_current_soc_pct" not in text
    assert "sensor.nimbus_solver_battery_0_soc_pct" in text
    assert "sensor.nimbus_solver_battery_1_soc_pct" in text
    assert "SoC Home battery" in text and "SoC EV 1" in text
    assert "nimbus_solver_battery_2_soc_pct" not in text


def test_the_glance_ring_follows_the_same_rule() -> None:
    def ring(batteries: int) -> str:
        return json.dumps(
            solver_tab.resolve(solver_tab._glance_chart(), _lookup(batteries))
        )

    assert "current_soc_pct" in ring(1)
    assert "battery_0_soc_pct" not in ring(1)
    assert "current_soc_pct" not in ring(2)
    assert "battery_0_soc_pct" in ring(2) and "battery_1_soc_pct" in ring(2)


def test_controllable_loads_are_listed_with_their_own_entities() -> None:
    """Household, 23 Oct 2026: a heat pump or pool pump under Nimbus's
    control was missing from the Topology card (it publishes no forecast,
    the only thing the card listed loads by)."""
    from custom_components.nimbus_load.const import (
        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
        CONF_CONTROLLABLE_LOAD_NAME,
        CONF_DEFERRABLE_MAX_POWER_KW,
        SUBENTRY_TYPE_CONTROLLABLE_LOAD,
    )

    s = _sensor_with_hass(
        _fake_entry(
            {
                "pp": _fake_subentry(
                    "pp",
                    SUBENTRY_TYPE_CONTROLLABLE_LOAD,
                    {
                        CONF_CONTROLLABLE_LOAD_NAME: "Pool Pump",
                        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY: "switch.pool_pump",
                        CONF_DEFERRABLE_MAX_POWER_KW: 1.5,
                    },
                )
            },
            {},
        )
    )
    registered = {
        "pp_commanded_state": "sensor.nimbus_pool_pump_commanded_state",
        "pp_delivered_today": "sensor.nimbus_pool_pump_delivered_today",
        "pp_target_today": "sensor.nimbus_pool_pump_target_today",
    }
    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.side_effect = (
            lambda _domain, _platform, uid: registered.get(uid)
        )
        attrs = s.extra_state_attributes
    (load,) = attrs["controllable_loads"]
    assert load["name"] == "Pool Pump"
    assert load["device_entity"] == "switch.pool_pump"
    assert load["max_power_kw"] == 1.5
    assert load["commanded_state"] == "sensor.nimbus_pool_pump_commanded_state"
    assert load["delivered_today"] == "sensor.nimbus_pool_pump_delivered_today"
    assert load["target_today"] == "sensor.nimbus_pool_pump_target_today"
    assert load["status"] is None  # not registered here: unresolved, not guessed


def test_forecaster_meters_are_published_apart_from_the_switchboard() -> None:
    """Household, 23 Oct 2026: a clean install whose signals were left at
    role "other" drew an empty Grid box. The Forecaster's Grid and Battery
    sensors are published for the card's fallback, outside `switchboard` so
    they cannot make the wizard look like it holds Topology (#1702)."""
    from custom_components.nimbus_load.const import (
        CONF_BATTERY_SENSOR,
        CONF_GRID_SENSOR,
    )

    s = _sensor_with_hass(
        _fake_entry(
            {},
            {
                CONF_GRID_SENSOR: "sensor.grid_meter",
                CONF_BATTERY_SENSOR: "sensor.battery_power",
            },
        )
    )
    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        attrs = s.extra_state_attributes
    assert attrs["forecaster_meters"] == {
        "grid_meter": "sensor.grid_meter",
        "battery_power": "sensor.battery_power",
    }
    assert not any(attrs["switchboard"].values())
