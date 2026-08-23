"""Real tests for solver/modes.py -- shadow_modes_for_plan() and
summarize_mode_transitions(). Zero prior coverage (never imported by
any production code path yet -- see its own module docstring, "purely
for OBSERVATION and comparison"), but it's real, shipped, documented
behaviour translating a Plan into this project's own exact Sungrow
battery-mode language, so it's worth locking in the same way any other
solver module is.

A minimal, hand-constructed Plan is used throughout rather than a real
build_plan() solve -- this module only ever reads
plan.{is_optimal,status,periods.n_periods,battery_charge_kw,
battery_discharge_kw}, so a full LP solve would be slower and less
targeted than directly constructing the handful of fields this module
actually touches.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: E402, F401  -- side-effect: puts solver/ + ml/ on sys.path

from solver.elements import PeriodGrid  # noqa: E402
from solver.modes import (  # noqa: E402
    ShadowModeReading,
    shadow_modes_for_plan,
    summarize_mode_transitions,
)
from solver.network import Plan  # noqa: E402


def _plan(
    charge_kw: list[float],
    discharge_kw: list[float],
    status: str = "optimal",
) -> Plan:
    n = len(charge_kw)
    periods = PeriodGrid(hours=np.full(n, 1.0))
    z = np.zeros(n)
    return Plan(
        status=status,
        periods=periods,
        battery_charge_kw=np.array(charge_kw, dtype=np.float64),
        battery_discharge_kw=np.array(discharge_kw, dtype=np.float64),
        battery_soc_kwh=z,
        grid_import_kw=z,
        grid_export_kw=z,
        export_bonus_kw=z,
        solar_used_kw=z,
        solar_curtailed_kw=z,
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        iterations=0,
    )


# --- shadow_modes_for_plan: the three real modes ---------------------------


def test_discharge_above_threshold_is_vpp_discharge():
    plan = _plan(charge_kw=[0.0], discharge_kw=[12.5])
    readings = shadow_modes_for_plan(plan)
    assert readings == [
        ShadowModeReading(
            mode="vpp_discharge", ems_code=4, command="Discharge", setpoint_kw=12.5
        )
    ]


def test_charge_above_threshold_is_vpp_charge():
    plan = _plan(charge_kw=[8.0], discharge_kw=[0.0])
    readings = shadow_modes_for_plan(plan)
    assert readings == [
        ShadowModeReading(
            mode="vpp_charge", ems_code=4, command="Charge", setpoint_kw=8.0
        )
    ]


def test_neither_above_threshold_is_self_consume_not_vpp_stop():
    """Real, deliberate project rule (see this project's own CLAUDE.md,
    "Battery Control Strategy"): a genuine zero-dispatch period must map
    to Self-Consume, never VPP Stop -- VPP Stop is more restrictive than
    a real zero-plan intends."""
    plan = _plan(charge_kw=[0.0], discharge_kw=[0.0])
    readings = shadow_modes_for_plan(plan)
    assert readings == [
        ShadowModeReading(
            mode="self_consume", ems_code=1, command="Stop", setpoint_kw=0.0
        )
    ]


def test_trickle_below_threshold_on_both_sides_is_still_self_consume():
    """0.05kW is the real dispatch threshold this project's own battery
    automation has used consistently -- confirms the boundary is
    actually enforced, not just "any nonzero value counts"."""
    plan = _plan(charge_kw=[0.02], discharge_kw=[0.03])
    readings = shadow_modes_for_plan(plan)
    assert readings[0].mode == "self_consume"


def test_discharge_takes_priority_when_somehow_both_are_above_threshold():
    """A degenerate case (shouldn't happen from a real LP solve, since
    charge/discharge are mutually exclusive at optimality for a
    non-zero-cost battery, but the function's own real branch order
    still has defined behaviour) -- discharge is checked first."""
    plan = _plan(charge_kw=[5.0], discharge_kw=[5.0])
    readings = shadow_modes_for_plan(plan)
    assert readings[0].mode == "vpp_discharge"


def test_setpoint_kw_reflects_the_real_dispatched_value_not_a_flag():
    plan = _plan(charge_kw=[0.0, 0.0], discharge_kw=[3.3, 40.0])
    readings = shadow_modes_for_plan(plan)
    assert readings[0].setpoint_kw == 3.3
    assert readings[1].setpoint_kw == 40.0


# --- shadow_modes_for_plan: honest failure on a non-optimal plan ----------


def test_raises_on_non_optimal_plan_rather_than_fabricate_a_reading():
    """A non-optimal Plan's arrays are meaningless zero-fills (see Plan's
    own docstring) -- silently returning Self-Consume readings for every
    period would misrepresent a genuinely failed solve as "nothing to
    dispatch," which is a different, false claim."""
    plan = _plan(charge_kw=[0.0], discharge_kw=[0.0], status="infeasible")
    with pytest.raises(ValueError, match="infeasible"):
        shadow_modes_for_plan(plan)


# --- summarize_mode_transitions ---------------------------------------------


def test_no_transitions_when_mode_never_changes():
    plan = _plan(charge_kw=[0.0] * 4, discharge_kw=[0.0] * 4)
    readings = shadow_modes_for_plan(plan)
    assert summarize_mode_transitions(readings) == []


def test_detects_a_single_transition_at_the_right_index():
    plan = _plan(
        charge_kw=[0.0, 0.0, 0.0, 0.0],
        discharge_kw=[0.0, 0.0, 12.0, 12.0],
    )
    readings = shadow_modes_for_plan(plan)
    transitions = summarize_mode_transitions(readings)
    assert transitions == [(2, "self_consume", "vpp_discharge")]


def test_detects_multiple_transitions_in_order():
    plan = _plan(
        charge_kw=[8.0, 0.0, 0.0, 0.0, 0.0],
        discharge_kw=[0.0, 0.0, 15.0, 15.0, 0.0],
    )
    readings = shadow_modes_for_plan(plan)
    transitions = summarize_mode_transitions(readings)
    assert transitions == [
        (1, "vpp_charge", "self_consume"),
        (2, "self_consume", "vpp_discharge"),
        (4, "vpp_discharge", "self_consume"),
    ]


def test_empty_readings_list_has_no_transitions():
    assert summarize_mode_transitions([]) == []


def test_single_reading_has_no_transitions():
    plan = _plan(charge_kw=[0.0], discharge_kw=[0.0])
    readings = shadow_modes_for_plan(plan)
    assert summarize_mode_transitions(readings) == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
