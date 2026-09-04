"""Real test of solver_writer._dispatch_source_breakdown() -- the
merit-order per-period source/destination decomposition added
2026-08-28, direct ask: "the plan table should also say where it is
coming from -- such as solar, grid, battery... not just charging... it
should say direction, and then from/to what source."

The LP itself has no per-source flow variables to read back
(BatteryConfig is a single aggregate on a single copper-plate bus), so
this is an honest merit-order decomposition of the same flow balance
the LP already solved: solar serves load first, any surplus charges
the battery, anything still short comes from grid import; symmetrically
on discharge, the battery serves load before any of it is attributed
to export.

Imports the REAL function directly (not a reimplementation) --
solver_writer.py has no homeassistant.* imports at module scope for
this function's own dependencies, so no HA stubs are needed here,
matching every other pure-function test in this directory (see
test_resample_generic_price_forecast.py).
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import solver_writer


def test_charging_entirely_from_solar_surplus():
    # Solar exceeds load by more than the charge rate -- 100% solar.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=-5.0, solar_kw_i=10.0, load_kw_i=2.0
        )
    )
    assert direction == "charge"
    assert a_label == "Solar"
    assert a_pct == 100.0
    assert b_label == "Grid"
    assert b_pct == 0.0


def test_charging_entirely_from_grid_when_no_solar():
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=-6.964, solar_kw_i=0.0, load_kw_i=1.5
        )
    )
    assert direction == "charge"
    assert a_label == "Solar"
    assert a_pct == 0.0
    assert b_label == "Grid"
    assert b_pct == 100.0


def test_charging_split_between_solar_surplus_and_grid_topup():
    # Solar surplus (10 - 2 = 8kW) covers only part of an 12kW charge --
    # the remaining 4kW must come from grid.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=-12.0, solar_kw_i=10.0, load_kw_i=2.0
        )
    )
    assert direction == "charge"
    assert a_label == "Solar"
    assert a_pct == round(8.0 / 12.0 * 100, 1)
    assert b_label == "Grid"
    assert b_pct == round(4.0 / 12.0 * 100, 1)
    assert round(a_pct + b_pct, 1) == 100.0


def test_discharging_entirely_to_load_when_load_exceeds_discharge():
    # Household load (10kW) exceeds solar (0) + discharge (4kW) --
    # every bit of the discharge stays local, nothing exported.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=4.0, solar_kw_i=0.0, load_kw_i=10.0
        )
    )
    assert direction == "discharge"
    assert a_label == "Load"
    assert a_pct == 100.0
    assert b_label == "Grid"
    assert b_pct == 0.0


def test_discharging_entirely_to_grid_when_solar_already_covers_load():
    # Solar alone already covers the whole load -- every bit of
    # discharge is a real export, none of it serves local load.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=6.0, solar_kw_i=5.0, load_kw_i=3.0
        )
    )
    assert direction == "discharge"
    assert a_label == "Load"
    assert a_pct == 0.0
    assert b_label == "Grid"
    assert b_pct == 100.0


def test_discharging_split_between_load_and_export():
    # Remaining (unmet-by-solar) load is 3kW; a 10kW discharge covers
    # that 3kW locally, the other 7kW is a real export.
    direction, a_label, a_pct, b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=10.0, solar_kw_i=2.0, load_kw_i=5.0
        )
    )
    assert direction == "discharge"
    assert a_label == "Load"
    assert a_pct == round(3.0 / 10.0 * 100, 1)
    assert b_label == "Grid"
    assert b_pct == round(7.0 / 10.0 * 100, 1)
    assert round(a_pct + b_pct, 1) == 100.0


def test_idle_battery_returns_idle_direction_and_zero_pcts():
    direction, _a_label, a_pct, _b_label, b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=0.0, solar_kw_i=5.0, load_kw_i=5.0
        )
    )
    assert direction == "idle"
    assert a_pct == 0.0
    assert b_pct == 0.0


def test_tiny_near_zero_battery_kw_within_eps_treated_as_idle():
    # Sub-watt LP degeneracy noise shouldn't read as a real charge/
    # discharge decision.
    direction, _a_label, _a_pct, _b_label, _b_pct = (
        solver_writer._dispatch_source_breakdown(
            battery_kw=0.0005, solar_kw_i=5.0, load_kw_i=5.0
        )
    )
    assert direction == "idle"


def test_forecast_periods_carry_the_new_fields():
    # End-to-end sanity: the real forecast list (built inside main(),
    # not reimplemented here) uses this function per-period via the
    # module-level dispatch_breakdown list comprehension -- this test
    # only re-confirms the pure function's own output shape, since
    # main() itself needs a full HA/solver config to exercise (covered
    # by this repo's existing hass_integration tests instead).
    result = solver_writer._dispatch_source_breakdown(-3.0, 3.0, 1.0)
    assert len(result) == 5
    assert isinstance(result[0], str)
    assert isinstance(result[2], float)
    assert isinstance(result[4], float)
