"""An export that is almost all solar is a solar export (self-consume).

A tester, 26 Nov 2026, 15:15: battery full, the plan selling spare solar with
a small battery discharge. Any discharge above the deadband made the dispatch
force Discharge, and in "Grid export" (sell power) mode it sent the whole
planned export as the target, so when the sun came in under forecast the
battery made up the gap. `dispatch_solar_export` is now true while the home
battery's discharge is at most 10 % of the planned export: a dispatcher should
self-consume, which exports the spare solar that is really there. A real
battery sale (a P2P block) is not a solar export.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import solver_publish


def _flag(home_kw: float, grid_export_kw: float) -> bool:
    return solver_publish._dispatch_solar_export(home_kw, grid_export_kw)


def test_a_small_discharge_while_selling_solar_is_a_solar_export():
    assert _flag(0.4, 8.0) is True  # 5 %
    assert _flag(0.9, 9.5) is True  # under 10 %


def test_a_real_battery_sale_is_not():
    assert _flag(14.6, 14.0) is False  # a P2P block
    assert _flag(2.0, 8.0) is False  # 25 %


def test_no_export_or_no_discharge_is_not():
    assert _flag(0.4, 0.3) is False  # covering the house, not selling
    assert _flag(0.0, 8.0) is False  # idle battery: not a discharge
    assert _flag(-3.0, 8.0) is False  # charging
