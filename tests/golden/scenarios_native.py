"""Native-mode golden scenarios (nimbus issue #1335).

The Part A scenarios in ``scenarios_synthetic.py`` drive ``main()`` down
the standalone/REST path, where ``_NATIVE_HASS is None`` and
``build_controllable_loads()`` / ``apply_commanded_state_guard()`` both
return on their first line. These run the same ``main()`` with a real
``hass`` injected (``golden.fake_native``), and with real
``controllable_load`` subentries behind it, so both functions do actual
work: the LP gets a sheddable and a deferrable load, the guard debounces
their commanded state, persists it, and dispatches to a real device
entity.

Both loads here are configured so the LP's answer is FORCED, not merely
optimal:

- the sheddable load has ``min_fraction = 1.0``, so it must be served in
  full every period;
- the deferrable load's ``target_kwh`` is more than its ``max_power_kw``
  can deliver between ``earliest_hour`` and ``deadline_hour``, and its
  ``shortfall_price`` ($4.00/kWh) is far above the import price
  ($0.30/kWh), so every period inside the window must run at the cap --
  the residual shortfall is unavoidable and identical whatever the LP
  does with the rest of the day.

That matters for a snapshot. With the flat prices ``BASE_CONFIG`` uses, a
load with real slack has many equally-optimal placements, and which one
HiGHS returns is not something a golden master should pin: an earlier
draft of this scenario gave the deferrable load exactly enough window to
meet its target and HiGHS placed it 25 minutes in, on a tie.

``kind=thermal`` is deliberately NOT covered here. Its target temperature
is a HARD LP constraint with an infeasibility-relaxation retry behind it
(``elements.ThermalLoadConfig``), so the only configuration that forces a
unique answer sits within a rounding error of infeasible -- exactly the
knife edge a snapshot must not be balanced on. ``scenarios_thermal``
covers it a different way instead -- a price gradient, so the answer is
unique with real slack still left in the window (nimbus issue #1358).
"""

from __future__ import annotations

import copy
from typing import Any

from golden.fake_ha import FakeHA
from golden.scenarios import Scenario, register
from golden.scenarios_synthetic import BASE_CONFIG, base_states

# Frozen instant of every scenario below: 20:00 Australia/Brisbane. The
# grid starts at 5-minute resolution, so period 0 is 20:00, and coarsens
# to 30 minutes later in the ~4-day horizon.
INSTANT = "2026-09-06T10:00:00+00:00"

POOL_PUMP = "switch.golden_pool_pump"
POOL_PUMP_POWER = "sensor.golden_pool_pump_power"
HWS = "water_heater.golden_hws"
HWS_POWER = "sensor.golden_hws_power"

# 20:00-20:30 is periods 0..6 inclusive (35 minutes at 5-minute
# resolution), so 3.0 kW can deliver 1.75 kWh. The target is deliberately
# above that -- see the module docstring on why the scenario wants no
# slack.
HWS_MAX_POWER_KW = 3.0
HWS_TARGET_KWH = 3.25
HWS_EARLIEST_HOUR = 20.0
HWS_DEADLINE_HOUR = 20.5


def _config() -> dict:
    cfg = copy.deepcopy(BASE_CONFIG)
    return cfg


def _states() -> dict:
    states = base_states(_config())
    # Both loads' own measured power. The pool pump's sensor is named
    # explicitly on its subentry; the HWS's is discovered off the device
    # registry (#768's own auto-discovery), which is the path a real
    # MQTT heat pump takes.
    states[POOL_PUMP_POWER] = {
        "state": "2.0",
        "attributes": {"unit_of_measurement": "kW", "device_class": "power"},
    }
    states[HWS_POWER] = {
        "state": "2950",
        "attributes": {"unit_of_measurement": "W", "device_class": "power"},
    }
    states[POOL_PUMP] = {"state": "off", "attributes": {}}
    states[HWS] = {
        "state": "eco",
        "attributes": {
            "current_temperature": 48.0,
            "temperature": 60.0,
            "min_temp": 30.0,
            "max_temp": 65.0,
            "operation_list": ["eco", "performance"],
        },
    }
    return states


def _subentries() -> list:
    from golden.fake_native import FakeSubentry

    return [
        FakeSubentry(
            subentry_id="golden_sheddable",
            subentry_type="controllable_load",
            title="Pool pump",
            data={
                "controllable_load_name": "Pool pump",
                "controllable_load_kind": "sheddable",
                "controllable_load_device_entity": POOL_PUMP,
                "controllable_load_power_sensor": POOL_PUMP_POWER,
                "sheddable_nominal_kw": 2.0,
                "sheddable_shed_cost": 0.45,
                # Forced: served in full every period. See the module
                # docstring on why a golden scenario avoids LP slack.
                "sheddable_min_fraction": 1.0,
            },
        ),
        FakeSubentry(
            subentry_id="golden_deferrable",
            subentry_type="controllable_load",
            title="Hot water",
            data={
                "controllable_load_name": "Hot water",
                "controllable_load_kind": "deferrable",
                "controllable_load_device_entity": HWS,
                # No controllable_load_power_sensor: discovery resolves
                # it from the device registry below (#768).
                "deferrable_max_power_kw": HWS_MAX_POWER_KW,
                "deferrable_target_kwh": HWS_TARGET_KWH,
                "deferrable_earliest_hour": HWS_EARLIEST_HOUR,
                "deferrable_deadline_hour": HWS_DEADLINE_HOUR,
                "deferrable_shortfall_price": 4.0,
                "controllable_load_min_hold_minutes": 15.0,
                "controllable_load_max_activations_per_day": 4,
            },
        ),
    ]


def _registry_entries() -> list:
    from golden.fake_native import FakeRegistryEntry

    return [
        FakeRegistryEntry(entity_id=HWS, device_id="golden_hws_device"),
        FakeRegistryEntry(
            entity_id=HWS_POWER,
            device_id="golden_hws_device",
            original_device_class="power",
        ),
        # Same name shape as the HWS's own sensor, on a DIFFERENT device:
        # discovery must not pick it. The mutation guard #768 shipped with
        # its own unit test, restated here where a whole solve can see it.
        FakeRegistryEntry(
            entity_id="sensor.golden_hws_power_elsewhere",
            device_id="some_other_device",
            original_device_class="power",
        ),
        FakeRegistryEntry(entity_id=POOL_PUMP, device_id="golden_pump_device"),
    ]


def _native(fake: FakeHA) -> Any:
    from golden.fake_native import FakeNativeHass

    return FakeNativeHass(
        fake=fake,
        subentries=_subentries(),
        registry_entries=_registry_entries(),
    )


register(
    Scenario(
        name="native_controllable_loads",
        instant=INSTANT,
        build=lambda: FakeHA(states=_states()),
        native=_native,
        purpose=(
            "native mode with a sheddable and a deferrable controllable "
            "load: build_controllable_loads() builds both, the LP schedules "
            "them, apply_commanded_state_guard() debounces, persists and "
            "dispatches (nimbus issue #1335)"
        ),
    )
)


# Two cycles, five minutes apart, over one workdir and one hass: cycle 2
# reads back the LoadRunState cycle 1 persisted, so the debounce
# (min_hold_minutes / DEFAULT_MIN_HYSTERESIS_PERIODS) and the
# already-commanded no-re-dispatch path are inside the golden master too,
# not just the first-transition one.
register(
    Scenario(
        name="native_controllable_loads_two_cycles",
        instant=INSTANT,
        build=lambda: FakeHA(states=_states()),
        native=_native,
        purpose=(
            "cycle 2 reads back the LoadRunState cycle 1 wrote: the "
            "commanded-state debounce and the no-re-dispatch-when-unchanged "
            "path (nimbus issue #1335)"
        ),
        cycles=2,
    )
)
