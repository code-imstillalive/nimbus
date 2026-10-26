"""The "+ Add" wizards for a hub's parts: each one creates its part, opens
again on reconfigure with what was saved, names the part sensibly, and
refuses what cannot work (#1837).

Covers what lived in #467, #563, #809/#811, #1067, #1562 and the subentry
flow files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

const = importlib.import_module("custom_components.nimbus_load.const")
flows = importlib.import_module("custom_components.nimbus_load.flows")
RECONFIGURE = sys.modules["homeassistant.config_entries"].SOURCE_RECONFIGURE


def _mod(name):
    return importlib.import_module(f"custom_components.nimbus_load.flows.{name}")


PARTICIPANT = {
    const.CONF_BATTERY_PARTICIPANT_NAME: "Car",
    const.CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 60.0,
    const.CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.car_soc",
    const.CONF_BATTERY_PARTICIPANT_POWER_SENSOR: "sensor.car_power",
    const.CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE: True,
    const.CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW: 7.0,
    const.CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW: 5.0,
    const.CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT: 20.0,
    const.CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT: 100.0,
    const.CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT: 92.0,
}

# module, handler, a complete answer, the title it gets
WIZARDS = {
    "load": (
        "load_subentry",
        "NimbusLoadSubentryFlowHandler",
        {const.CONF_LOAD_SENSOR: "sensor.pool_power"},
        "Pool pump power",
    ),
    "signal": (
        "signal_subentry",
        "NimbusSignalSubentryFlowHandler",
        {
            const.CONF_LOAD_SENSOR: "sensor.pool_power",
            const.CONF_SIGNAL_ROLE: "battery",
        },
        "Pool pump power",
    ),
    "controllable_load": (
        "controllable_load_subentry",
        "NimbusControllableLoadSubentryFlowHandler",
        {
            const.CONF_CONTROLLABLE_LOAD_NAME: "Hot water",
            const.CONF_CONTROLLABLE_LOAD_KIND: "deferrable",
        },
        "Hot water",
    ),
    "battery_participant": (
        "battery_participant_subentry",
        "NimbusBatteryParticipantSubentryFlowHandler",
        PARTICIPANT,
        "Car",
    ),
    "pv_string": (
        "pv_string_subentry",
        "NimbusPvStringSubentryFlowHandler",
        {const.CONF_PV_STRING_ENTITY: "sensor.pool_power"},
        "Pool pump power",
    ),
    "power_source": (
        "power_source_subentry",
        "NimbusPowerSourceSubentryFlowHandler",
        {const.CONF_POWER_SOURCE_NAME: "Generator"},
        "Generator",
    ),
    "battery_tower": (
        "battery_tower_subentry",
        "NimbusBatteryTowerSubentryFlowHandler",
        {const.CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.tower_soc"},
        "Tower 1",
    ),
}

STATES = {
    "sensor.pool_power": SimpleNamespace(
        state="1",
        attributes={"friendly_name": "Pool pump power", "unit_of_measurement": "W"},
    ),
    "sensor.tower_soc": SimpleNamespace(
        state="50", attributes={"friendly_name": "Tower 1 SoC"}
    ),
}


def _flow(kind, *, subentries=None, reconfigure=None, states=None):
    module, handler, _answer, _title = WIZARDS[kind]
    m = _mod(module)
    flow = getattr(m, handler)()
    entry = SimpleNamespace(entry_id="hub1", options={}, subentries=subentries or {})
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=(states or STATES).get),
        config=SimpleNamespace(currency="AUD"),
    )
    flow.source = RECONFIGURE if reconfigure is not None else "user"
    flow._get_entry = lambda: entry
    flow._get_reconfigure_subentry = lambda: reconfigure
    return flow, m


@pytest.fixture(autouse=True)
def quiet_checks(monkeypatch):
    for kind in ("load", "signal"):
        monkeypatch.setattr(
            _mod(WIZARDS[kind][0]),
            "energy_unit_field_errors",
            lambda hass, ui, fields: {},
        )

    async def no_suggestion(hass):
        return None

    monkeypatch.setattr(
        _mod("battery_tower_subentry"),
        "_energy_dashboard_soc_suggestion",
        no_suggestion,
    )


@pytest.mark.parametrize("kind", sorted(WIZARDS))
def test_each_wizard_creates_its_part_with_a_sensible_name(kind):
    flow, _m = _flow(kind)
    form = asyncio.run(flow.async_step_user())
    assert form["type"] == "form" and form["step_id"] == "user"
    answer, title = WIZARDS[kind][2], WIZARDS[kind][3]
    created = asyncio.run(flow.async_step_user(dict(answer)))
    assert created == {"type": "create_entry", "title": title, "data": answer}


@pytest.mark.parametrize("kind", sorted(WIZARDS))
def test_reconfigure_opens_with_what_was_saved_and_updates_in_place(kind):
    answer = WIZARDS[kind][2]
    saved = SimpleNamespace(subentry_id="s1", data=dict(answer), subentry_type=kind)
    flow, _m = _flow(kind, reconfigure=saved)
    form = asyncio.run(flow.async_step_reconfigure())
    defaults = {}
    for marker in form["data_schema"].schema:
        default = getattr(marker, "default", vol.UNDEFINED)
        suggested = (getattr(marker, "description", None) or {}).get("suggested_value")
        value = default() if default is not vol.UNDEFINED else suggested
        if value is not None:
            defaults[str(marker)] = value
    assert {k: v for k, v in answer.items() if k in defaults} == {
        k: defaults[k] for k in answer if k in defaults
    }
    assert set(answer) <= set(defaults) or kind in ("load", "signal", "pv_string")
    updated = asyncio.run(flow.async_step_reconfigure(dict(answer)))
    assert updated["type"] == "update_and_abort"


@pytest.mark.parametrize("kind", ["load", "signal"])
def test_a_sensor_another_forecaster_already_uses_is_refused(kind):
    other = SimpleNamespace(
        subentry_id="other",
        subentry_type=const.SUBENTRY_TYPE_LOAD,
        data={const.CONF_LOAD_SENSOR: "sensor.pool_power"},
    )
    flow, _m = _flow(kind, subentries={"other": other})
    refused = asyncio.run(flow.async_step_user(dict(WIZARDS[kind][2])))
    assert refused["errors"] == {"load_sensor": "duplicate_source_sensor"}
    # Reconfiguring that same forecaster with its own sensor is fine.
    flow, _m = _flow(kind, subentries={"other": other}, reconfigure=other)
    assert (
        asyncio.run(flow.async_step_user(dict(WIZARDS[kind][2])))["type"]
        == "update_and_abort"
    )


@pytest.mark.parametrize(
    ("kind", "answer", "states", "title"),
    [
        ("load", {const.CONF_LOAD_SENSOR: "sensor.unnamed"}, {}, "sensor.unnamed"),
        (
            "pv_string",
            {
                const.CONF_PV_STRING_ENTITY: "sensor.pool_power",
                const.CONF_PV_STRING_LABEL: "East roof",
            },
            STATES,
            "East roof",
        ),
        (
            "pv_string",
            {const.CONF_PV_STRING_ENTITY: "sensor.unnamed"},
            {},
            "sensor.unnamed",
        ),
        ("battery_tower", {}, {}, None),
        (
            "battery_tower",
            {const.CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.x"},
            {"sensor.x": SimpleNamespace(attributes={})},
            None,
        ),
        (
            "battery_tower",
            {const.CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.x"},
            {"sensor.x": SimpleNamespace(attributes={"friendly_name": "Garage soc"})},
            "Garage",
        ),
        (
            "battery_tower",
            {const.CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.x"},
            {"sensor.x": SimpleNamespace(attributes={"friendly_name": "Garage pack"})},
            "Garage pack",
        ),
    ],
)
def test_names_fall_back_sensibly(kind, answer, states, title):
    flow, m = _flow(kind, states=states)
    created = asyncio.run(flow.async_step_user(dict(answer)))
    assert created["title"] == (title or m._TITLE_FALLBACK)


def test_only_load_and_signal_forecasters_can_collide():
    subs = {
        "a": SimpleNamespace(
            subentry_id="a",
            subentry_type=const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
            data={const.CONF_LOAD_SENSOR: "sensor.p"},
        ),
        "b": SimpleNamespace(
            subentry_id="b",
            subentry_type=const.SUBENTRY_TYPE_SIGNAL,
            data={const.CONF_LOAD_SENSOR: "sensor.p"},
        ),
    }
    entry = SimpleNamespace(subentries=subs)
    assert (
        flows.find_subentry_sharing_source_sensor(entry, "sensor.p").subentry_id == "b"
    )
    assert (
        flows.find_subentry_sharing_source_sensor(
            entry, "sensor.p", exclude_subentry_id="b"
        )
        is None
    )
    assert flows.find_subentry_sharing_source_sensor(entry, "sensor.q") is None


def test_a_participant_can_be_here_by_any_kind_of_entity_and_listed_states():
    availability = importlib.import_module("custom_components.nimbus_load.availability")
    schema = _mod("battery_participant_subentry")._schema({})
    fields = {str(k): k for k in schema.schema}
    entity = fields[const.CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY]
    assert list(schema.schema[entity].config["domain"]) == list(availability.DOMAINS)
    assert "battery_participant_available_states" in fields
    root = Path(availability.__file__).resolve().parent
    for name in ("strings.json", "translations/en.json"):
        assert '"battery_participant_available_states"' in (root / name).read_text(
            encoding="utf-8"
        )
