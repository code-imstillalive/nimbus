"""Translate a solved Plan into this project's own real, documented
Sungrow battery-mode language -- Self-Consume / VPP Discharge / VPP
Charge / VPP Stop -- purely for OBSERVATION and comparison against what
the real inverter is actually doing.

This module never writes anything anywhere either (see network.py's own
module docstring for the same point). Its entire purpose is answering
"if this plan were live, what mode/command/setpoint would it correspond
to" so a human can look at it side by side with the real inverter state
-- the same comparison shape this project's own monitor_haeo.py has
already proven useful for exactly this purpose (comparing HAEO's own
plan sensors against real measured battery state).

Mode semantics and the 0.05kW threshold are taken directly from this
project's own long-documented battery mode table (see the sibling
116KAT-HA-AI repo's own CLAUDE.md, "Battery Inverter Modes" section) --
not invented here, reused so a shadow-mode reading is directly, exactly
comparable to a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .network import Plan

# Same threshold this project's own real battery automation
# (`automation.battery_automation_haeo`, and every automation since) has
# used consistently across its entire documented history -- below this,
# a nonzero charge/discharge value is treated as noise/trickle, not a
# genuine dispatch decision.
_DISPATCH_THRESHOLD_KW: float = 0.05

ShadowMode = Literal["self_consume", "vpp_discharge", "vpp_charge", "vpp_stop"]


@dataclass(frozen=True)
class ShadowModeReading:
    """One period's worth of "what mode would this be, if live." Mirrors
    the real Sungrow register language this project already uses
    end-to-end (EMS mode 1=Self-Consume/4=VPP, CMD Stop/Charge/Discharge)
    so a side-by-side comparison against real telemetry never needs a
    translation step of its own.
    """

    mode: ShadowMode
    ems_code: int  # 1 = Self-Consume, 4 = VPP (matches real Sungrow registers)
    command: str  # "Stop" | "Charge" | "Discharge" (matches real Sungrow command language)
    setpoint_kw: float  # what would be written to the charge/discharge power register


def shadow_modes_for_plan(plan: Plan) -> list[ShadowModeReading]:
    """Compute the shadow mode for every period in a solved Plan. Raises
    if the plan isn't optimal -- there is no meaningful "mode" for a
    period whose values are just zero-filled placeholders (see Plan's own
    _infeasible_plan() docstring), and silently returning a Self-Consume
    reading for every period of a genuinely failed solve would misrepresent
    what actually happened.
    """
    if not plan.is_optimal:
        msg = f"Cannot compute shadow modes for a non-optimal plan (status={plan.status!r})"
        raise ValueError(msg)

    readings: list[ShadowModeReading] = []
    for t in range(plan.periods.n_periods):
        charge = float(plan.battery_charge_kw[t])
        discharge = float(plan.battery_discharge_kw[t])
        if discharge > _DISPATCH_THRESHOLD_KW:
            readings.append(
                ShadowModeReading(mode="vpp_discharge", ems_code=4, command="Discharge", setpoint_kw=discharge)
            )
        elif charge > _DISPATCH_THRESHOLD_KW:
            readings.append(ShadowModeReading(mode="vpp_charge", ems_code=4, command="Charge", setpoint_kw=charge))
        else:
            # Matches this project's own confirmed-correct default (see
            # CLAUDE.md's own "Battery Control Strategy" section): neither
            # discharge nor charge above the dispatch threshold means
            # "hands off, let the inverter self-manage" -- Self-Consume,
            # never VPP Stop, which is more restrictive than intended for
            # a genuine zero-dispatch period.
            readings.append(ShadowModeReading(mode="self_consume", ems_code=1, command="Stop", setpoint_kw=0.0))
    return readings


def summarize_mode_transitions(readings: list[ShadowModeReading]) -> list[tuple[int, ShadowMode, ShadowMode]]:
    """Return (period_index, from_mode, to_mode) for every period where the
    shadow mode actually CHANGES from the previous period -- a compact way
    to see the plan's real dispatch shape (a handful of transitions across
    a 96-period horizon) instead of scanning every single period by hand.
    """
    transitions: list[tuple[int, ShadowMode, ShadowMode]] = []
    for i in range(1, len(readings)):
        prev, curr = readings[i - 1].mode, readings[i].mode
        if prev != curr:
            transitions.append((i, prev, curr))
    return transitions
