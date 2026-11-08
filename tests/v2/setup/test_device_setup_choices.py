"""Configure -> Device setup -> Review: each rediscovery difference in plain words, and what ticking it
confirms (#2393: closing flows/device_setup.py's coverage gap, one behaviour per test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.flows.device_setup import _choice, _describe

FILL = {
    "kind": "fill",
    "device": "battery_1",
    "role": "state_of_charge",
    "binding": {"source": {"entity_id": "sensor.battery_soc"}},
}
FILL_STATISTIC = {
    "kind": "fill",
    "device": "site",
    "role": "grid_import_energy",
    "binding": {"source": {"statistic_id": "sensor.grid_import_energy"}},
}
RENAMED = {
    "kind": "renamed",
    "device": "inverter_1",
    "role": "pv_power",
    "stored_entity_id": "sensor.pv_old",
    "entity_id": "sensor.pv_new",
}
ALTERNATIVE = {
    "kind": "alternative",
    "device": "site",
    "role": "grid_power",
    "candidate": {"source": "sensor.meter_power"},
}
NEW_BATTERY = {
    "kind": "new_device",
    "device": {"id": "battery_2", "kind": "battery", "name": "Second battery"},
}
NEW_INVERTER = {
    "kind": "new_device",
    "device": {"id": "inverter_2", "kind": "inverter"},
}


@pytest.mark.parametrize(
    ("difference", "words"),
    [
        (FILL, "Fill battery_1 state of charge: sensor.battery_soc"),
        (FILL_STATISTIC, "Fill site grid import energy: sensor.grid_import_energy"),
        (RENAMED, "Renamed inverter_1 pv power: sensor.pv_old is now sensor.pv_new"),
        (
            ALTERNATIVE,
            "Alternative for site grid power: sensor.meter_power (yours stays unless you tick this)",
        ),
        (NEW_BATTERY, "New device: Second battery"),
        (NEW_INVERTER, "New device: inverter_2"),
        ({"kind": "conflict", "device": "site", "role": "x_y"}, "conflict: site x y"),
    ],
)
def test_review_says_each_difference_in_plain_words(difference, words):
    # test_review_says_each_difference_in_plain_words
    assert _describe(difference) == words


@pytest.mark.parametrize(
    ("difference", "capacity", "confirmed"),
    [
        (
            FILL,
            None,
            {
                "kind": "fill",
                "device": "battery_1",
                "role": "state_of_charge",
                "entity_id": "sensor.battery_soc",
            },
        ),
        (
            FILL_STATISTIC,
            None,
            {
                "kind": "fill",
                "device": "site",
                "role": "grid_import_energy",
                "entity_id": "sensor.grid_import_energy",
            },
        ),
        (
            RENAMED,
            None,
            {
                "kind": "renamed",
                "device": "inverter_1",
                "role": "pv_power",
                "entity_id": "sensor.pv_new",
            },
        ),
        (
            ALTERNATIVE,
            None,
            {
                "kind": "alternative",
                "device": "site",
                "role": "grid_power",
                "entity_id": "sensor.meter_power",
            },
        ),
        # a new battery carries the usable capacity the household typed; without one, none is invented
        (
            NEW_BATTERY,
            13.5,
            {
                "kind": "new_device",
                "device": "battery_2",
                "capacity": {"value": 13.5, "unit": "kWh", "basis": "usable"},
            },
        ),
        (NEW_BATTERY, None, {"kind": "new_device", "device": "battery_2"}),
        (NEW_INVERTER, 13.5, {"kind": "new_device", "device": "inverter_2"}),
    ],
)
def test_ticking_a_difference_confirms_exactly_what_it_named(
    difference, capacity, confirmed
):
    # test_ticking_a_difference_confirms_exactly_what_it_named
    assert _choice(difference, capacity) == confirmed
