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

from datetime import UTC, datetime, timedelta

from custom_components.nimbus_load import solver_publish
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
    # test_a_hold_with_spare_solar_is_absorbed_too
    # Battery idle in the plan, spare solar, cheap export: soak it up.
    assert _call(battery_kw=0.0) == (True, "spare_solar_below_floor")

    # test_grid_charging_is_left_to_the_plan
    # Self-consume cannot charge from the grid.
    assert _call(battery_kw=-40.0, grid_to_battery_kw=36.0) == (
        False,
        "grid_charging",
    )

    # test_a_p2p_block_is_left_to_the_plan
    assert _call(committed_export_kw=11.5) == (False, "p2p_block")

    # test_a_marginal_hold_right_after_solar_charge_stays_solar_charge
    # Household, 28 Oct 2026 (option 2): solar about equal to the load made
    # the plan flip between PV-only charge and hold every minute, and the
    # inverter followed. Inside 5 minutes of a solar charge, a hold short by
    # at most 2 kW is published as solar charge; outside either, the plan's.
    t0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    solver_publish._LAST_SOLAR_CHARGE["at"] = None

    def hold(shortfall_kw, seconds):
        row = {"dispatch_hold": True, "dispatch_solar_charge": False}
        row["grid_import_kw"] = shortfall_kw
        solver_publish._steady_solar_charge(row, t0 + timedelta(seconds=seconds))
        return row["dispatch_hold"], row["dispatch_solar_charge"]

    assert hold(1.9, 0) == (True, False)  # no solar charge seen yet
    solver_publish._steady_solar_charge({"dispatch_solar_charge": True}, t0)
    assert hold(1.9, 60) == (False, True)
    assert hold(3.0, 60) == (True, False)
    assert hold(0.0, 60) == (True, False)
    assert hold(1.9, 400) == (True, False)
    solver_publish._LAST_SOLAR_CHARGE["at"] = None
