"""`absorb_surplus` on the plan sensor: where the plan would put a surplus it
cannot export (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load.solver_publish import absorb_surplus

# The 09:47 shape: a small solar charge planned, solar well above the load,
# 4c export against a 20c floor.
BASE = {
    "battery_kw": -0.8,
    "grid_to_battery_kw": 0.0,
    "solar_kw": 6.2,
    "load_kw": 3.0,
    "export_price": 0.04,
    "export_floor": 0.20,
    "committed_export_kw": 0.0,
    "price_spike": False,
}


def _call(**over):
    return absorb_surplus(**{**BASE, **over})


def test_a_hold_with_spare_solar_is_absorbed_too():
    # Battery idle in the plan, spare solar, cheap export: soak it up.
    assert _call(battery_kw=0.0) == (True, "spare_solar_below_floor")


def test_grid_charging_is_left_to_the_plan():
    # Self-consume cannot charge from the grid.
    assert _call(battery_kw=-40.0, grid_to_battery_kw=36.0) == (
        False,
        "grid_charging",
    )


def test_a_p2p_block_is_left_to_the_plan():
    assert _call(committed_export_kw=11.5) == (False, "p2p_block")
