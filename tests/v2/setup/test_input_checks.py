"""The checks a setting must pass before Nimbus trusts it, and the small
pieces setup is built from: power units, energy counters offered as power,
frozen price signals, the household's NEM region, the pricing source
profiles, and the first-install flow (#1837).

Covers what lived in #1562, #1570, #1643, #1825, the frozen-price, pricing
source, NEM region and config-flow files.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

pic = importlib.import_module("custom_components.nimbus_load.power_input_check")
prc = importlib.import_module("custom_components.nimbus_load.price_input_check")
pu = importlib.import_module("custom_components.nimbus_load.power_units")
nem = importlib.import_module("custom_components.nimbus_load.nem_region")
ps = importlib.import_module("custom_components.nimbus_load.pricing_source")
cf = importlib.import_module("custom_components.nimbus_load.config_flow")
const = importlib.import_module("custom_components.nimbus_load.const")


def _hass(units: dict, extra_attrs=None):
    def get(eid):
        if eid not in units:
            return None
        return SimpleNamespace(
            state="1",
            attributes={
                "unit_of_measurement": units[eid],
                **(extra_attrs or {}).get(eid, {}),
            },
        )

    return SimpleNamespace(states=SimpleNamespace(get=get))


# --- power units -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "power", "kw"),
    [
        ("W", True, 0.001),
        ("kW", True, 1.0),
        ("MW", True, 1000.0),
        (" kw ", True, 1.0),
        ("kWh", False, 1.0),
        ("A", False, 1.0),
        (None, False, 1.0),
        (5, False, 1.0),
    ],
)
def test_a_power_unit_converts_to_kw_and_anything_else_is_read_as_kw(unit, power, kw):
    assert pu.is_power_unit(unit) is power
    assert pu.power_scale_to_kw(unit) == pytest.approx(kw)
    assert pu.power_to_kw(2.0, unit) == pytest.approx(2.0 * kw)


@pytest.mark.parametrize(
    ("unit", "energy", "non_power"),
    [
        ("kWh", True, False),
        ("wh", True, False),
        ("W", False, False),
        ("A", False, True),
        ("kVA", False, True),
        ("%", False, True),
        ("", False, False),
        (None, False, False),
    ],
)
def test_energy_and_non_power_units_are_told_apart_and_no_unit_is_neither(
    unit, energy, non_power
):
    assert pic.is_energy_unit(unit) is energy
    assert pic.is_non_power_unit(unit) is non_power


# --- energy counters offered as power ------------------------------------------------------


def _entry(options=None, subentries=()):
    return SimpleNamespace(
        options=options or {}, subentries={s.subentry_id: s for s in subentries}
    )


def _sub(sid, kind, title, **data):
    return SimpleNamespace(subentry_id=sid, subentry_type=kind, title=title, data=data)


def test_an_energy_counter_set_as_a_power_input_is_found_wherever_it_is_set():
    hass = _hass(
        {
            "sensor.bat_kwh": "kWh",
            "sensor.pool_kwh": "kWh",
            "sensor.ok": "W",
            "sensor.temp": "°C",
        }
    )
    entry = _entry(
        {
            const.CONF_BATTERY_SENSOR: "sensor.bat_kwh",
            const.CONF_SOLAR_SENSOR: "sensor.ok",
        },
        [
            _sub(
                "l",
                const.SUBENTRY_TYPE_LOAD,
                "Pool",
                **{const.CONF_LOAD_SENSOR: "sensor.pool_kwh"},
            ),
            _sub(
                "c",
                const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
                "HW",
                **{const.CONF_LOAD_SENSOR: "sensor.pool_kwh"},
            ),
        ],
    )
    assert pic.find_energy_unit_inputs(hass, entry) == [
        ("Forecaster: battery sensor", "sensor.bat_kwh", "kWh"),
        ("Load 'Pool'", "sensor.pool_kwh", "kWh"),
    ]
    assert pic.energy_unit_field_errors(
        hass, {"a": "sensor.bat_kwh", "b": "sensor.ok"}, ["a", "b", "c"]
    ) == {"a": "energy_sensor_not_power"}
    assert (
        pic.energy_unit_of(hass, None) is None
        and pic.energy_unit_of(hass, "sensor.ok") is None
    )


def test_a_non_power_unit_is_found_in_every_power_field_but_not_on_a_temperature_signal():
    units = {
        "sensor.amps": "A",
        "sensor.kva": "kVA",
        "sensor.kwh": "kWh",
        "sensor.temp": "°C",
        "sensor.w": "W",
    }
    entry = _entry(
        {
            "solver_solar_power_sensor": "sensor.amps",
            "solver_battery_power_sensor": "sensor.kwh",
            const.CONF_GRID_SENSOR: "sensor.kva",
            const.CONF_SOLAR_SENSOR: "sensor.kwh",
        },
        [
            _sub(
                "t",
                const.SUBENTRY_TYPE_SIGNAL,
                "Outside",
                **{
                    const.CONF_LOAD_SENSOR: "sensor.temp",
                    const.CONF_SIGNAL_ROLE: const.SIGNAL_ROLE_TEMPERATURE,
                },
            ),
            _sub(
                "s",
                const.SUBENTRY_TYPE_SIGNAL,
                "Inverter",
                **{
                    const.CONF_LOAD_SENSOR: "sensor.amps",
                    const.CONF_SIGNAL_ROLE: "battery",
                },
            ),
            _sub(
                "l",
                const.SUBENTRY_TYPE_LOAD,
                "Pool",
                **{const.CONF_LOAD_SENSOR: "sensor.kwh"},
            ),
            _sub(
                "b",
                "battery_participant",
                "Car",
                battery_participant_power_sensor="sensor.kwh",
            ),
            _sub(
                "c",
                "controllable_load",
                "HW",
                controllable_load_power_sensor="sensor.w",
            ),
        ],
    )
    found = pic.find_non_power_unit_inputs(_hass(units), entry)
    assert found == [
        ("Solver settings: solar power sensor", "sensor.amps", "A"),
        ("Solver settings: battery power sensor", "sensor.kwh", "kWh"),
        ("Forecaster: grid sensor", "sensor.kva", "kVA"),
        ("Power Signal 'Inverter'", "sensor.amps", "A"),
        ("Battery 'Car'", "sensor.kwh", "kWh"),
    ]


def test_each_warning_is_logged_once_per_entity(caplog, monkeypatch):
    monkeypatch.setattr(pic, "_WARNED", set())
    monkeypatch.setattr(pic, "_WARNED_NON_POWER", set())
    monkeypatch.setattr(pic, "_WARNED_REFUSED", set())
    with caplog.at_level(logging.WARNING, logger=pic._LOGGER.name):
        for _ in range(2):
            pic.warn_ignored_once("sensor.a", "kWh", "battery")
            pic.warn_non_power_once("sensor.b", "A")
            pic.warn_refused_input_once("sensor.c", "kVA")
    assert len(caplog.records) == 3


def test_the_energy_unit_notice_is_cleared_and_logs_each_input(caplog):
    calls = []

    async def call(domain, service, data, blocking):
        calls.append((service, data["notification_id"]))

    hass = _hass({"sensor.bat_kwh": "kWh"})
    hass.services = SimpleNamespace(async_call=call)
    with caplog.at_level(logging.WARNING, logger=pic._LOGGER.name):
        asyncio.run(
            pic.async_notify_energy_unit_inputs(
                hass, _entry({const.CONF_BATTERY_SENSOR: "sensor.bat_kwh"})
            )
        )
    assert calls == [("dismiss", pic.NOTIFICATION_ID)]
    assert "an energy counter" in caplog.text

    async def broken(*a, **k):
        raise RuntimeError("x")

    hass.services.async_call = broken
    asyncio.run(pic.async_notify_energy_unit_inputs(hass, _entry({})))


# --- frozen price signals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity_id", "attrs", "unique_id", "why"),
    [
        (
            "sensor.lv_rate",
            {"source_field": "rateAllVar"},
            None,
            "source_field rateAllVar",
        ),
        ("sensor.lv_rate", {}, "x_rate_all_var", "unique id ends _rate_all_var"),
        ("sensor.lv_rate_all_var", {}, None, "entity id ends _rate_all_var"),
        ("sensor.flex_up", {"source_field": "costsFlexUp"}, "x_buy_flex_up", None),
    ],
)
def test_a_price_signal_that_never_changes_is_named_with_how_it_was_recognised(
    monkeypatch, entity_id, attrs, unique_id, why
):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get=lambda eid: SimpleNamespace(unique_id=unique_id)
        ),
        raising=False,
    )
    hass = _hass({entity_id: "$/kWh"}, {entity_id: attrs})
    assert prc.frozen_rate_field(hass, entity_id) == why
    options = {const.CONF_SOLVER_IMPORT_PRICE_SENSOR: entity_id}
    assert prc.find_frozen_price_inputs(hass, options) == (
        [] if why is None else [("Solver settings: import price", entity_id, why)]
    )
    assert prc.frozen_price_field_errors(
        hass, options, [const.CONF_SOLVER_IMPORT_PRICE_SENSOR]
    ) == (
        {}
        if why is None
        else {const.CONF_SOLVER_IMPORT_PRICE_SENSOR: "frozen_price_signal"}
    )
    assert prc.frozen_rate_field(hass, None) is None


def test_a_broken_registry_never_breaks_the_price_check(monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: 1 / 0, raising=False)
    assert prc.frozen_rate_field(_hass({"sensor.x": "$/kWh"}), "sensor.x") is None


# --- the NEM region from an address -----------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "region", "prefix"),
    [
        ("12 Smith St, Brisbane City QLD 4000, Australia", "QLD1", "400"),
        ("1 Main Rd, Canberra ACT 2600", "NSW1", "260"),
        ("Hobart tas 7000", "TAS1", "700"),
        ("Perth WA 6000", None, "600"),  # not in the NEM
        ("Somewhere", None, None),
        (None, None, None),
    ],
)
def test_the_region_and_postcode_prefix_come_from_the_geocoded_address(
    address, region, prefix
):
    assert nem.resolve_region_and_prefix(address) == (region, prefix)


def test_a_malformed_postcode_has_no_prefix():
    assert (
        nem.postcode_prefix("40001") is None and nem.nem_region_for_state(None) is None
    )


# --- the pricing source profiles ------------------------------------------------------------------


@pytest.fixture
def providers(monkeypatch):
    found = {"lv": {}, "express": {}, "core": {}}
    monkeypatch.setattr(ps.pa, "detect_localvolts_v2_profile", lambda hass: found["lv"])
    monkeypatch.setattr(
        ps.pa, "detect_amber_express_profile", lambda hass: found["express"]
    )
    monkeypatch.setattr(ps.pa, "detect_amber_core_profile", lambda hass: found["core"])
    return found


IMPORT, EXPORT, ARRAY = (
    const.CONF_SOLVER_IMPORT_PRICE_SENSOR,
    const.CONF_SOLVER_EXPORT_PRICE_SENSOR,
    const.CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
)


def test_only_complete_profiles_are_offered(providers):
    providers["lv"] = {f: f"sensor.lv_{i}" for i, f in enumerate(ps.pa.LV_V2_PROFILE)}
    providers["express"] = {IMPORT: "sensor.ae_buy"}  # no export: not offered
    providers["core"] = {IMPORT: "sensor.amber_buy", EXPORT: "sensor.amber_sell"}
    profiles = ps.detected_profiles(None)
    assert list(profiles) == ["LocalVolts v2", "Amber Electric"]
    assert profiles["Amber Electric"] == {
        IMPORT: "sensor.amber_buy",
        EXPORT: "sensor.amber_sell",
        ARRAY: None,
    }


def test_switching_profile_sets_its_sensors_and_clears_what_it_does_not_use(providers):
    profile = {IMPORT: "sensor.amber_buy", EXPORT: "sensor.amber_sell", ARRAY: None}
    options = ps.applied({IMPORT: "x", ARRAY: "sensor.lv_array", "other": 1}, profile)
    assert options == {
        IMPORT: "sensor.amber_buy",
        EXPORT: "sensor.amber_sell",
        "other": 1,
    }
    assert ps.active_label(options, {"Amber Electric": profile}) == "Amber Electric"
    assert (
        ps.active_label({IMPORT: "sensor.mine"}, {"Amber Electric": profile})
        == ps.MANUAL
    )
    status = ps.status(options, {"Amber Electric": profile})
    assert (
        status["import_price_sensor"] == "sensor.amber_buy"
        and "Amber Electric" in status["detected_providers"]
    )


def test_a_profile_with_a_missing_or_unavailable_sensor_lists_the_problem():
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get={
                "sensor.b": SimpleNamespace(state="unavailable"),
                "sensor.c": SimpleNamespace(state="0.2"),
            }.get
        )
    )
    problems = ps.problems(
        hass, {IMPORT: "sensor.a", EXPORT: "sensor.b", ARRAY: None, "x": "sensor.c"}
    )
    assert problems == [
        f"{ps.pa.PRICING_FIELD_LABELS[IMPORT]}: sensor.a does not exist",
        f"{ps.pa.PRICING_FIELD_LABELS[EXPORT]}: sensor.b is unavailable",
    ]


# --- the first install -------------------------------------------------------------------------------


def test_installing_creates_the_hub_once_and_points_at_the_settings():
    calls, flow = [], cf.NimbusConfigFlow()

    async def call(domain, service, data):
        calls.append(data["notification_id"])

    async def unique(uid):
        flow.uid = uid

    flow.hass = SimpleNamespace(services=SimpleNamespace(async_call=call))
    flow.async_set_unique_id = unique
    flow._abort_if_unique_id_configured = lambda: None
    created = asyncio.run(flow.async_step_user())
    assert created == {"type": "create_entry", "title": "Nimbus", "data": {}}
    assert flow.uid == const.DOMAIN and calls == ["nimbus_setup_incomplete"]
    kinds = cf.NimbusConfigFlow.async_get_supported_subentry_types(None)
    assert set(kinds) == {
        const.SUBENTRY_TYPE_LOAD,
        const.SUBENTRY_TYPE_SIGNAL,
        const.SUBENTRY_TYPE_POWER_SOURCE,
        const.SUBENTRY_TYPE_PV_STRING,
        const.SUBENTRY_TYPE_BATTERY_TOWER,
        const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    }
    assert (
        type(
            cf.NimbusConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
        ).__name__
        == "NimbusHubOptionsFlow"
    )
