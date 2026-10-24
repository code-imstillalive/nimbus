"""Setting up the sensor platform: every entity a hub, its forecasters and
its controllable loads get, each once, and every Solver push wired to the
entity that publishes it (#1837).

Covers what lived in #85, #343, #757, the 2026-09-08 duplicate unique-id
incident, #828 and the sensor-setup files.
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
_device_registry = sys.modules["homeassistant.helpers.device_registry"]
if not hasattr(_device_registry, "DeviceInfo"):
    _device_registry.DeviceInfo = dict  # a TypedDict in Home Assistant

sm = importlib.import_module("custom_components.nimbus_load.sensor")
sw = importlib.import_module("custom_components.nimbus_load.solver_writer")
p5 = importlib.import_module("custom_components.nimbus_load.sensor_p5min")
const = importlib.import_module("custom_components.nimbus_load.const")

PUSHED = {
    "sensor.nimbus_solver_battery_forecast",
    "sensor.nimbus_household_load_total_forecast",
    "sensor.nimbus_solver_dispatch_dry_run",
    "sensor.nimbus_offer_curve",
    "sensor.nimbus_mirror_temperature_forecast",
    "sensor.nimbus_mirror_humidity_forecast",
    "sensor.nimbus_solver_quality_report",
    "sensor.nimbus_efficiency_backtest",
    "sensor.nimbus_counterfactual_soc",
    "sensor.nimbus_flex_signals",
    "sensor.nimbus_flex_report",
    "sensor.nimbus_flex_telemetry",
}


async def _value(v):
    return v


class Coordinator:
    def __init__(self, hass, entry):
        self.data = {}

    async def async_config_entry_first_refresh(self):
        pass

    def get(self, subentry_id):
        return importlib.import_module(
            "custom_components.nimbus_load.load_run_state"
        ).LoadRunState()


@pytest.fixture
def world(monkeypatch, tmp_path):
    # Home Assistant's Entity.unique_id reads _attr_unique_id; the stub base has none.
    base = sys.modules["homeassistant.components.sensor"].SensorEntity
    monkeypatch.setattr(
        base, "unique_id", property(lambda self: self._attr_unique_id), raising=False
    )
    handlers, migrated, tasks = {}, [], []
    states, registry = {}, {}
    monkeypatch.setattr(
        sm,
        "async_get_integration",
        lambda hass, domain: _value(SimpleNamespace(version="0.94.485")),
    )
    monkeypatch.setattr(sm, "_resolve_hub_device_id", lambda hass, entry: "dev-hub")
    monkeypatch.setattr(sm, "NimbusLoadRunStateCoordinator", Coordinator)
    monkeypatch.setattr(
        sm,
        "_migrate_stale_commanded_state_entity_id",
        lambda hass, sub: migrated.append(sub.subentry_id),
    )
    monkeypatch.setattr(
        sm.er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get_entity_id=lambda d, p, uid: registry.get(uid)
        ),
    )
    monkeypatch.setattr(
        sw,
        "register_entity_handler",
        lambda eid, handler, real: handlers.__setitem__(eid, (handler, real)),
    )
    region = {"value": None}
    monkeypatch.setattr(p5, "resolve_region", lambda hass, entry: region["value"])
    runtime = importlib.import_module("custom_components.nimbus_load.solver_runtime")
    monkeypatch.setattr(runtime, "set_default_env_vars", lambda hass: None)

    async def import_job(fn, *a):
        return fn(*a)

    hass = SimpleNamespace(
        states=SimpleNamespace(get=states.get, async_all=lambda domain=None: []),
        config=SimpleNamespace(
            currency="AUD", path=lambda *p: str(tmp_path.joinpath(*p))
        ),
        async_create_task=lambda coro: tasks.append(coro) or coro.close(),
        async_add_import_executor_job=import_job,
    )
    return SimpleNamespace(
        hass=hass,
        handlers=handlers,
        migrated=migrated,
        tasks=tasks,
        states=states,
        registry=registry,
        region=region,
    )


def _entry(subs=(), runtime=None):
    return SimpleNamespace(
        entry_id="hub1",
        options={},
        data={},
        subentries={s.subentry_id: s for s in subs},
        runtime_data=runtime or {},
    )


def _sub(sid, kind, title, **data):
    return SimpleNamespace(subentry_id=sid, subentry_type=kind, title=title, data=data)


def _setup(world, entry):
    added = []
    asyncio.run(
        sm.async_setup_entry(
            world.hass,
            entry,
            lambda ents, config_subentry_id=None: added.append(
                (list(ents), config_subentry_id)
            ),
        )
    )
    return added


def test_a_bare_hub_gets_its_own_sensors_once_each_and_every_push_wired(world):
    added = _setup(world, _entry())
    ids = [e.entity_id for ents, _ in added for e in ents]
    assert len(ids) == len(set(ids)), "every entity is added once"
    for hub_sensor in (
        "sensor.nimbus_solver_config",
        "sensor.nimbus_topology_config",
        "sensor.nimbus_health_report",
        "sensor.nimbus_status",
        "sensor.nimbus_solver_price_response_latency",
    ):
        assert hub_sensor in ids
    assert "sensor.nimbus_p5min_forecast" not in ids  # no NEM region
    assert set(world.handlers) == PUSHED
    by_id = {e.entity_id: e for ents, _ in added for e in ents}
    for eid, (handler, real) in world.handlers.items():
        assert real == eid and handler.__self__ is by_id[eid]
    # The plan's flattened figures hang off the battery forecast.
    battery = by_id["sensor.nimbus_solver_battery_forecast"]
    assert battery._flattened_entities and battery._flattened_current_entities


def test_each_forecaster_and_controllable_load_gets_its_own_sensors_under_its_subentry(
    world,
):
    subs = [
        _sub(
            "pool",
            const.SUBENTRY_TYPE_LOAD,
            "Pool",
            **{const.CONF_LOAD_SENSOR: "sensor.pool_w"},
        ),
        _sub(
            "orphan",
            const.SUBENTRY_TYPE_SIGNAL,
            "Orphan",
            **{const.CONF_LOAD_SENSOR: "sensor.x"},
        ),
        _sub(
            "hw",
            const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
            "Hot Water",
            **{const.CONF_CONTROLLABLE_LOAD_KIND: "deferrable"},
        ),
    ]
    coord = SimpleNamespace(last_update_success=True, data=None)
    added = _setup(world, _entry(subs, {"pool": coord}))
    by_subentry = {}
    for ents, sid in added:
        by_subentry.setdefault(sid, []).extend(e.entity_id for e in ents)
    assert by_subentry["pool"] == [
        f"sensor.{sm.object_id_from_source('sensor.pool_w')}"
    ]
    assert "orphan" not in by_subentry  # no coordinator yet: nothing to publish
    assert len(by_subentry["hw"]) == 13 and all(
        e.startswith("sensor.nimbus_hot_water_") for e in by_subentry["hw"]
    )
    assert world.migrated == ["hw"]
    assert len(world.tasks) == 1  # the forecast's statistics unit is checked once


def test_a_nem_region_adds_the_pre_dispatch_sensor(world):
    world.region["value"] = "QLD1"
    ids = [e.entity_id for ents, _ in _setup(world, _entry()) for e in ents]
    assert "sensor.nimbus_p5min_forecast" in ids


def test_a_conflicting_registry_entry_is_reported_before_it_collides(world, caplog):
    world.registry["hub1_nimbus_solver_quality_report"] = (
        "sensor.nimbus_solver_quality_report_2"
    )
    world.states["sensor.nimbus_flex_report"] = SimpleNamespace(
        state="1", attributes={"restored": True}, last_updated=None
    )
    with caplog.at_level(logging.WARNING, logger=sm._LOGGER.name):
        _setup(world, _entry())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "sensor.nimbus_solver_quality_report" in w and "_2" in w for w in warnings
    )
    assert not any(
        "sensor.nimbus_flex_report " in w for w in warnings
    )  # restored: a routine reload
