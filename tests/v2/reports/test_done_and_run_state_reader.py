"""When a heater's tank counts as done, and the run-state reader every
controllable-load sensor shares (#1837).

Covers what lived in #482 (done_when), #828 (the reader before its first
refresh) and the done-condition files.
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

dc = importlib.import_module("custom_components.nimbus_load.done_condition")
lrc = importlib.import_module(
    "custom_components.nimbus_load.load_run_state_coordinator"
)
lrs = importlib.import_module("custom_components.nimbus_load.load_run_state")


@pytest.mark.parametrize(
    ("expr", "temp", "done"),
    [
        (">= 60", 60.0, True),
        (">=60", 59.9, False),
        ("<= 40", 40.0, True),
        ("> 55", 55.0, False),
        ("< 55", 54.0, True),
        ("== 50", 50.0, True),
        ("!= 50", 50.0, False),
    ],
)
def test_a_done_condition_is_an_operator_and_a_threshold(expr, temp, done):
    op, threshold = dc.parse_done_when(expr)
    assert op(temp, threshold) is done
    assert dc.is_tank_done(temp, expr) is done


@pytest.mark.parametrize("expr", ["60", "about 60", "=> 60", ">= hot"])
def test_a_condition_that_cannot_be_read_is_refused_or_unknown(expr):
    with pytest.raises(ValueError):
        dc.parse_done_when(expr)
    assert dc.is_tank_done(55.0, expr) is None


@pytest.mark.parametrize(
    ("temp", "setpoint", "done"),
    [
        (None, 60, None),
        (61.0, 60, True),
        (59.0, 60.0, False),
        (59.0, None, None),
        (59.0, "warm", None),
    ],
)
def test_without_a_condition_the_heaters_own_setpoint_decides(temp, setpoint, done):
    assert dc.is_tank_done(temp, None, setpoint_temperature=setpoint) is done


@pytest.mark.parametrize(
    ("entity", "state", "attrs", "temp"),
    [
        ("water_heater.tank", "eco", {"current_temperature": "58.5"}, 58.5),
        ("climate.ac", "cool", {"current_temperature": 22}, 22.0),
        ("water_heater.tank", "unavailable", {"current_temperature": 58.5}, None),
        ("water_heater.tank", "eco", {}, None),
        ("water_heater.tank", "eco", {"current_temperature": "hot"}, None),
        (
            "sensor.tank_temp",
            "58",
            {"current_temperature": 58},
            None,
        ),  # not a heater: no attribute read
    ],
)
def test_the_tank_temperature_comes_from_a_heaters_own_attribute(
    entity, state, attrs, temp
):
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get={entity: SimpleNamespace(state=state, attributes=attrs)}.get
        )
    )
    assert dc.read_current_temperature(hass, entity) == temp
    assert dc.read_current_temperature(hass, "water_heater.none") is None


# --- the run-state reader ---------------------------------------------------------------


class Disk:
    def __init__(self, data=None, broken=False):
        self.data, self.broken = data, broken

    async def async_load(self):
        if self.broken:
            raise OSError("disk")
        return self.data


@pytest.fixture
def reader(monkeypatch):
    disk = Disk()
    monkeypatch.setattr(lrc, "Store", lambda hass, version, key: disk)
    coord = lrc.NimbusLoadRunStateCoordinator(
        SimpleNamespace(), SimpleNamespace(entry_id="hub1")
    )
    return coord, disk


def test_every_load_reads_its_own_state_and_an_unknown_load_reads_fresh(reader):
    coord, disk = reader
    assert coord.get("hw") == lrs.LoadRunState()  # before the first refresh
    disk.data = {
        "hw": {"commanded_state": True, "delivered_today_kwh": 1.5},
        "bad": {"delivered_today_kwh": "lots"},
    }
    coord.data = asyncio.run(coord._async_update_data())
    assert (
        coord.get("hw").commanded_state is True
        and coord.get("hw").delivered_today_kwh == 1.5
    )
    assert coord.get("bad") == lrs.LoadRunState()
    assert coord.get("other") == lrs.LoadRunState()


@pytest.mark.parametrize("disk_state", [None, "broken"])
def test_an_empty_or_unreadable_store_means_every_load_starts_fresh(
    reader, caplog, disk_state
):
    coord, disk = reader
    disk.broken = disk_state == "broken"
    with caplog.at_level(logging.WARNING, logger=lrc._LOGGER.name):
        assert asyncio.run(coord._async_update_data()) == {}
    assert ("read failed" in caplog.text) is disk.broken
