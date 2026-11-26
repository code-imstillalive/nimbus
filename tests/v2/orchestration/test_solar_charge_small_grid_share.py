"""A charge that is almost all solar stays a solar charge (self-consume).

A tester, 26 Nov 2026, 10:27-10:31: the plan charged the battery at
16.5-17.8 kW with about 1 kW of it from the grid (about 6 %, buy 4.3c). Any
grid share above 0.05 kW turned the solar-charge flag off, so the dispatch
forced a charge at the full 16.5-17.8 kW for four minutes. A grid share up to
10 % of the charge now keeps the flag on: self-consume soaks up the real
solar. A real grid charge (the same plan's 13:00 row: 14.1 kW of grid in a
26.2 kW charge) still forces the charge, and a solar-only charge (#2066) is
unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import solver_publish


def _flag(battery_kw: float, grid_to_battery_kw: float, export_kw: float = 0.0):
    return solver_publish._dispatch_solar_charge(
        battery_kw, grid_to_battery_kw, export_kw
    )


def test_a_small_grid_share_keeps_the_solar_charge():
    assert _flag(-17.0, 1.0) is True  # 6 %: the tester's 10:27
    assert _flag(-13.4, 0.3) is True  # 2 %: the same plan's 11:30 row


def test_a_real_grid_charge_still_forces_the_charge():
    assert _flag(-26.2, 14.1) is False  # 54 %: the 13:00 row
    assert _flag(-10.0, 1.5) is False  # 15 %


def test_solar_only_and_exporting_cases_are_unchanged():
    assert _flag(-0.8, 0.0) is True  # #2066: solar only
    assert _flag(-0.02, 0.0) is False  # not charging
    assert _flag(-5.0, 0.0, export_kw=2.0) is False  # charging while exporting
