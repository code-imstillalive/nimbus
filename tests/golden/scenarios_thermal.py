"""Native-mode golden scenarios for `kind=thermal` (nimbus issue #1358).

`scenarios_native.py` covers `kind=sheddable` and `kind=deferrable` and says in
its own docstring why it leaves thermal out. The short version: both of its
loads force a unique LP answer by removing the load's slack -- a target the
window cannot quite meet, so every period inside it must run flat out. A
thermal load's target temperature is a **hard** LP constraint with an
infeasibility-relaxation retry behind it (`elements.ThermalLoadConfig`,
`Plan.thermal_guarantee_relaxed`), so that same trick puts the scenario a
rounding error away from taking the relaxation path instead of the ordinary
one. A snapshot that can flip between two structurally different solves on a
float is worse than no snapshot.

## The energy the constraint demands, derived rather than tuned

Over a window of `H` hours, with

    T[t] = T[t-1] + rate * power[t] * hours[t] - decay * hours[t]
    T[deadline] >= target                                        (hard)

the decay term does not depend on *when* the heating happens, so the energy the
constraint demands is fixed wherever in the window it is spent:

    (target - initial + decay * H) / rate
  = (50.4  -  50.0   +  0.5  * 1.0) / 1.0   =  0.90 kWh

against a window capacity of `3.0 kW * 1.0 h = 3.0 kWh`. **70% of the window
goes unused** -- which is the point. The LP has real freedom about where to
heat, and nothing here sits near infeasible, so a float error cannot reach the
relaxation path from either of the two ordinary scenarios.

## What the record can actually observe, which took a correction

An earlier draft of this module tried to pin the LP's chosen *periods* by
running a second cycle 20 minutes later, expecting the dispatch to turn on once
the clock reached the cheapest period. **Measured: it does not, and the
expectation was wrong about the fixture rather than about the solver.**

Two things came out of those first recorded snapshots:

1. **The household-load forecast this scenario publishes does not include the
   controllable load's own scheduled power** -- its delta against the baseline
   scenario is 0.0 in every period. So the only per-load thing a cycle records
   is whether a dispatch call was made.
2. **Period 0 always takes the LIVE price**, from `sensor.fake_import_price`,
   not the forecast array's own first entry. The recorded series begins `[0.30,
   0.345, 0.325, ...]` where the array's first entry is 0.365. The fixture's
   live price is a single static value, so at *every* cycle "now" costs 0.30
   while later periods can be cheaper -- which means a thermal load with slack
   defers at every instant, and a later cycle never catches it starting. The
   two-cycle idea could not have worked.

So the placement is pinned the way the record can actually see it: **as a pair
of scenarios identical in every respect except the shape of the price forecast,
whose dispatch decisions differ.**

- `native_thermal_load_defers` -- a V-shaped ladder with its trough mid-window
  (cheapest 0.265 at 20:25), so the cheapest energy is in the future and the
  optimum is to wait. Records **no dispatch call**.
- `native_thermal_load_heats_now` -- the same load against a ladder entirely
  *above* the live price (0.40 rising to 0.51), so "now" is the cheapest energy
  in the window and the optimum is to start immediately. Records
  `water_heater.set_operation_mode(performance)`.

Neither is worth much alone: "did not dispatch" is also what a load that was
never built at all produces, and "dispatched immediately" is also what a solver
ignoring price entirely would do. **The pair is the evidence** -- the only
difference between them is the price series, so a regression that stopped price
reaching the thermal decision breaks one of the two whichever way it failed.

`native_thermal_relaxed` is the third, and covers the retry path itself: a
target needing 4.5 kWh of a 3.0 kWh window. Unreachable by a wide margin rather
than by a rounding error, which is the whole difference between it and the knife
edge described above.

**What it cannot prove, stated because the distinction matters.** It reaches the
retry path, so it counts for coverage -- but the record cannot show that the
guarantee was relaxed. `Plan.thermal_guarantee_relaxed` is set by
`solver/network.py` and read by **nothing outside the test suite**: it is not
published on a sensor, not logged, and not notified. So this snapshot's only
observable difference from `native_thermal_load_heats_now` is the plan's own
numbers, not a relaxation marker. Filed separately -- a hard guarantee being
silently relaxed is a gap for the household, not only for this fixture, and
#774's whole argument for making the guarantee hard was that it is the part you
can rely on.

## Ties, and why the prices look over-specified

All twelve prices the LP sees in the defer scenario are distinct, so
cheapest-first is a total order. The values avoid 0.30 as well as each other: an
earlier draft used a round 0.01 ladder whose index 3 was exactly 0.30 and
therefore tied with the live price at index 0, which is a tie in the solve for
no reason at all. Distinct prices also make the rest of the solve *more*
determinate than the flat `BASE_CONFIG` price does -- the battery fills
cheapest-first through the same window for the same reason, where a flat price
left it indifferent between periods.

## Why the price array and not a second mechanism

The gradient is served through `solver_price_forecast_array_sensor` in the same
`costsflexup`/`earningsflexup` shape the Part D NEMWEB scenarios already use, so
this exercises the committed price-array path rather than something invented for
one test. Periods past the twelfth hold the array's last value, exactly as they
would on a real install whose retail forecast runs out mid-horizon.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from golden.fake_ha import FakeHA
from golden.scenarios import Scenario, register
from golden.scenarios_native import INSTANT
from golden.scenarios_synthetic import BASE_CONFIG, base_states

TANK = "water_heater.golden_tank"
TANK_POWER = "sensor.golden_tank_power"
PRICE_ARRAY = "sensor.golden_thermal_price_array"

# Local wall clock of INSTANT (2026-09-06T10:00:00+00:00 == 20:00 Brisbane),
# which is also period 0. The grid starts at 5-minute resolution.
INSTANT_LOCAL = "2026-09-06T20:00:00+10:00"
PERIOD_MINUTES = 5

# The live price base_states() publishes on sensor.fake_import_price, which is
# what period 0 costs however the array is shaped. Named here because both
# ladders below are defined relative to it.
LIVE_IMPORT_PRICE = 0.30

THERMAL_MAX_POWER_KW = 3.0
THERMAL_HEATING_RATE_C_PER_KWH = 1.0
THERMAL_IDLE_DECAY_C_PER_HOUR = 0.5
THERMAL_INITIAL_C = 50.0
THERMAL_TARGET_C = 50.4
THERMAL_EARLIEST_HOUR = 20.0
THERMAL_DEADLINE_HOUR = 21.0

# Needs 4.5 kWh of a 3.0 kWh window: no placement can meet it, so the hard
# guarantee must be relaxed. A margin, deliberately, not a near miss.
THERMAL_UNREACHABLE_TARGET_C = 54.0

# 20:00..20:55. A V with its trough at 20:25, every value distinct and none
# equal to LIVE_IMPORT_PRICE -- so the cheapest energy is in the future and the
# load defers.
DEFER_LADDER = (
    0.365,
    0.345,
    0.325,
    0.305,
    0.285,
    0.265,
    0.275,
    0.295,
    0.315,
    0.335,
    0.355,
    0.375,
)

# Every value above LIVE_IMPORT_PRICE, rising -- so "now" is the cheapest
# energy in the window and the load starts immediately.
HEAT_NOW_LADDER = tuple(round(0.40 + 0.01 * i, 4) for i in range(12))


def energy_the_constraint_demands(target_c: float) -> float:
    """kWh the hard temperature constraint demands over the whole window.

    The module docstring derives this; it is code so those numbers are
    checkable, and `tests/test_1358_thermal_scenario_is_determinate.py` checks
    them rather than trusting the comment.
    """
    window_h = THERMAL_DEADLINE_HOUR - THERMAL_EARLIEST_HOUR
    rise_c = target_c - THERMAL_INITIAL_C + THERMAL_IDLE_DECAY_C_PER_HOUR * window_h
    return rise_c / THERMAL_HEATING_RATE_C_PER_KWH


def window_capacity_kwh() -> float:
    return THERMAL_MAX_POWER_KW * (THERMAL_DEADLINE_HOUR - THERMAL_EARLIEST_HOUR)


def prices_the_lp_sees(ladder: tuple[float, ...]) -> list[float]:
    """The import price per period as the solve actually receives it.

    Period 0 is the live price, not `ladder[0]` -- measured from the recorded
    snapshot, see the module docstring. Exposed so the determinacy test can
    assert the ordering is total without re-deriving this rule.
    """
    return [LIVE_IMPORT_PRICE, *ladder[1:]]


def price_array(ladder: tuple[float, ...]) -> list[dict]:
    t0 = datetime.fromisoformat(INSTANT_LOCAL)
    return [
        {
            "time": (t0 + timedelta(minutes=PERIOD_MINUTES * i)).isoformat(),
            "costsflexup": import_price,
            "earningsflexup": round(import_price - 0.25, 6),
        }
        for i, import_price in enumerate(ladder)
    ]


def _config() -> dict:
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["solver_price_forecast_array_sensor"] = PRICE_ARRAY
    return cfg


def _states(ladder: tuple[float, ...]) -> dict:
    states = base_states(_config())
    array = price_array(ladder)
    states[PRICE_ARRAY] = {
        "state": array[0]["costsflexup"],
        "attributes": {"forecast": array},
    }
    states[TANK_POWER] = {
        "state": "0",
        "attributes": {"unit_of_measurement": "kW", "device_class": "power"},
    }
    states[TANK] = {
        "state": "eco",
        "attributes": {
            # build_controllable_loads takes initial_temperature_c from here
            # via done_condition.read_current_temperature(), so this and
            # THERMAL_INITIAL_C must agree -- pinned by the #1358 test.
            "current_temperature": THERMAL_INITIAL_C,
            "temperature": 60.0,
            "min_temp": 30.0,
            "max_temp": 65.0,
            "operation_list": ["eco", "performance"],
        },
    }
    return states


def _subentries(target_c: float) -> list:
    from golden.fake_native import FakeSubentry

    return [
        FakeSubentry(
            subentry_id="golden_thermal",
            subentry_type="controllable_load",
            title="Tank",
            data={
                "controllable_load_name": "Tank",
                "controllable_load_kind": "thermal",
                "controllable_load_device_entity": TANK,
                "controllable_load_power_sensor": TANK_POWER,
                "thermal_max_power_kw": THERMAL_MAX_POWER_KW,
                "thermal_target_temperature_c": target_c,
                "thermal_earliest_hour": THERMAL_EARLIEST_HOUR,
                "thermal_deadline_hour": THERMAL_DEADLINE_HOUR,
                # Explicit, so heating_rate_origin and idle_decay_origin are
                # both "override" and these snapshots do not move when a
                # learned LoadRunState or thermal_forecast's own module
                # defaults change.
                "thermal_heating_rate_c_per_kwh": THERMAL_HEATING_RATE_C_PER_KWH,
                "thermal_idle_decay_c_per_hour": THERMAL_IDLE_DECAY_C_PER_HOUR,
                # No comfort floor: it is a SOFT cost term, and leaving it
                # unset keeps price the only thing deciding placement.
            },
        )
    ]


def _registry() -> list:
    from golden.fake_native import FakeRegistryEntry

    return [
        FakeRegistryEntry(entity_id=TANK, device_id="golden_tank_device"),
        FakeRegistryEntry(
            entity_id=TANK_POWER,
            device_id="golden_tank_device",
            original_device_class="power",
        ),
    ]


def _native(target_c: float):
    def build(fake: FakeHA) -> Any:
        from golden.fake_native import FakeNativeHass

        return FakeNativeHass(
            fake=fake,
            subentries=_subentries(target_c),
            registry_entries=_registry(),
        )

    return build


register(
    Scenario(
        name="native_thermal_load_defers",
        instant=INSTANT,
        build=lambda: FakeHA(states=_states(DEFER_LADDER)),
        native=_native(THERMAL_TARGET_C),
        purpose=(
            "kind=thermal with the cheapest energy later in its window: the "
            "hard temperature constraint, the T[t] recursion and the "
            "heating_rate/idle_decay override path all run, and the optimum is "
            "to wait -- so NO dispatch call is recorded. Half of the pair that "
            "pins price as the thing deciding placement (nimbus issue #1358)"
        ),
    )
)


register(
    Scenario(
        name="native_thermal_load_heats_now",
        instant=INSTANT,
        build=lambda: FakeHA(states=_states(HEAT_NOW_LADDER)),
        native=_native(THERMAL_TARGET_C),
        purpose=(
            "the same kind=thermal load against a price ladder entirely above "
            "the live price, so the cheapest energy in the window is now and "
            "the optimum is to start immediately -- set_operation_mode is "
            "recorded. The other half of the pair (nimbus issue #1358)"
        ),
    )
)


register(
    Scenario(
        name="native_thermal_relaxed",
        instant=INSTANT,
        build=lambda: FakeHA(states=_states(DEFER_LADDER)),
        native=_native(THERMAL_UNREACHABLE_TARGET_C),
        purpose=(
            "the infeasibility-relaxation retry itself: a thermal target no "
            "placement in the window can reach, so the hard guarantee is "
            "relaxed -- a real path with no tie to balance on (nimbus #1358)"
        ),
    )
)
