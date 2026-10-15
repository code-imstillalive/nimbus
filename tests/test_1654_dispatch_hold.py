"""nimbus #1654 (Mark Purcell, 16 Oct 2026): "IF grid is 0W, then
self-consume is correct. If battery is 0W, but grid <> 0 W then self-consume
is incorrect and should be hold or preserve command."

On 16 Oct the reference household's plan held the battery at 18.8 % from
01:00 to 04:00 and imported the house load; its dispatch automation maps a
0 kW plan to Self-Consume, which served that load from the battery instead,
so SoC reached 10.7 % at dawn against a planned 17.3 %. Each plan row now
says whether it means "hold", and period 0's flag is its own sensor.
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import pytest
import solver_publish
import solver_writer


@pytest.mark.parametrize(
    ("battery", "imp", "exp", "hold"),
    [
        (0.0, 1.1, 0.0, True),  # 16 Oct 01:00: idle, house from the grid
        (0.0, 0.0, 2.0, True),  # idle while exporting solar: hold, don't charge
        (0.0, 0.0, 0.0, False),  # nothing flows: self-consume is right
        (0.0, 0.03, 0.0, False),  # grid noise below the deadband
        (0.04, 1.0, 0.0, True),  # battery inside the deadband
        (1.0, 0.0, 0.0, False),  # discharging: not a hold
        (-5.0, 5.0, 0.0, False),  # charging from the grid: not a hold
        (0.0, 1.0, 1.0, False),  # import and export cancel: net zero
    ],
)
def test_hold_means_battery_idle_while_the_grid_flows(battery, imp, exp, hold):
    assert solver_publish._dispatch_hold(battery, imp, exp) is hold


def test_thresholds_match_the_dispatch_deadband():
    assert solver_publish.DISPATCH_HOLD_BATTERY_KW == 0.05
    assert solver_publish.DISPATCH_HOLD_GRID_KW == 0.05


def test_every_published_row_carries_the_flag():
    src = (
        __import__("pathlib").Path(solver_writer.__file__).parent / "solver_publish.py"
    ).read_text(encoding="utf-8")
    at = src.index('"dispatch_direction": dispatch_breakdown[i][0],')
    block = src[at : at + 700]
    assert '"dispatch_hold": _dispatch_hold(' in block
    # the same grid figures the row itself publishes
    assert "corrected_grid_import[i]" in block
    assert "plan.grid_export_kw[i]" in block


def test_period_zero_has_its_own_sensor():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from custom_components.nimbus_load import sensor_flattened

    specs = {s.source_key: s for s in sensor_flattened.FLATTENED_ATTRS_CURRENT}
    assert specs["dispatch_hold"].entity_id_suffix == "current_dispatch_hold"
