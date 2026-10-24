"""The Help tab's Start here shows the basic inputs live: what is configured
now and what still needs the household (#1800, #1823) (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
from custom_components.nimbus_load import help_tab


def test_every_basic_input_has_a_live_row():
    text = help_tab._BASIC_INPUTS
    for ref in (
        "${@option:solver_battery_soc_sensor}",
        "${@option:solver_import_price_sensor}",
        "${@option:solver_export_price_sensor}",
        "${@option:solver_solar_forecast_sensor}",
        "${@solver_battery_capacity_kwh}",
        "${@solver_max_charge_kw}",
        "${@solver_max_discharge_kw}",
        "${@solver_grid_max_import_kw}",
    ):
        assert f"set e = '{ref}'" in text, ref
    assert "Review pricing suggestions" in text and "Settings → Repairs" in text


def test_a_number_on_its_placeholder_is_not_ticked():
    row = help_tab._input_row("Battery capacity", "number.x", number=True, unit="kWh")
    assert "float(0) > 0.1" in row and "placeholder 0.1" in row


def test_it_sits_right_after_the_step_checklist():
    md = [
        c["cards"][0].get("content")
        for c in help_tab.template()
        if c.get("cards") and c["cards"][0].get("type") == "markdown"
    ]
    assert help_tab._BASIC_INPUTS in md
    assert md.index(help_tab._BASIC_INPUTS) == md.index(help_tab._START_HERE) + 1


def test_step_2_no_longer_sends_the_user_to_the_dashboard_first():
    assert "Solver settings screen 1 asks for capacity" in help_tab._START_HERE
