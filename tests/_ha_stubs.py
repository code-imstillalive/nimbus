"""Minimal homeassistant.* stub modules -- just enough surface area for
custom_components/nimbus_load/{__init__,coordinator,sensor}.py to import
successfully, so their real, non-HA-dependent logic can be exercised
against real-shaped mock objects rather than reimplemented for a test.

The real `homeassistant` package is not installed in this project's local
dev environment (a full HA install is a heavy dependency this repo has
deliberately never needed before -- every existing test under tests/
covers solver/, which has zero HA imports at all). Call install_ha_stubs()
once, before importing anything under custom_components.nimbus_load, to
register these stand-ins in sys.modules. Idempotent -- safe to call from
more than one test module without double-registering anything odd.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


async def _noop_async_added_to_hass(self) -> None:
    pass


class _StubPowerConverter:
    """Real W/kW/MW conversion, not an opaque mock -- pairs with the real
    UnitOfPower stub above. Deliberately narrow (only the 3 units this
    integration could plausibly ever see from a real power sensor) rather
    than a faithful reimplementation of HA's own full unit-conversion
    framework, which isn't what's under test here.
    """

    _TO_WATTS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}

    @classmethod
    def convert(cls, value: float, from_unit: str, to_unit: str) -> float:
        if from_unit not in cls._TO_WATTS or to_unit not in cls._TO_WATTS:
            raise ValueError(f"unsupported unit in stub converter: {from_unit!r} -> {to_unit!r}")
        return value * cls._TO_WATTS[from_unit] / cls._TO_WATTS[to_unit]


def _generic_stub_class(name: str) -> type:
    """A stub base class that tolerates HA's own generic-subscript usage,
    e.g. `class Foo(CoordinatorEntity[MyCoordinator], SensorEntity):` --
    plain `object` isn't subscriptable and would raise TypeError there.

    Also carries a no-op async_added_to_hass() -- any real entity class
    calling `await super().async_added_to_hass()` inside its own override
    (e.g. NimbusSolverSwitch's real restore-state logic) needs SOMETHING
    at that point in the MRO to resolve to, or the call raises
    AttributeError before ever reaching the entity's own real code being
    tested. A no-op is the correct stand-in -- the real base class's own
    restore-state-loading internals are exactly the black box a test here
    shouldn't need to know about, only the entity's own logic that runs
    around it.
    """
    return type(
        name,
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "async_added_to_hass": _noop_async_added_to_hass,
        },
    )


def install_ha_stubs() -> None:
    def module(name: str, **attrs) -> types.ModuleType:
        mod = sys.modules.get(name) or types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.recorder", get_instance=MagicMock())
    module("homeassistant.components.recorder.history", get_significant_states=MagicMock())
    module(
        "homeassistant.components.sensor",
        SensorDeviceClass=MagicMock(),
        SensorEntity=_generic_stub_class("SensorEntity"),
        SensorStateClass=MagicMock(),
    )
    # Real string values, not MagicMock -- number.py's own tests assert
    # "the right device_class landed on the right field," which only
    # means something if these compare/hash like the real StrEnum members
    # they stand in for (plain strings do, since HA's own NumberDeviceClass
    # IS a StrEnum -- confirmed 2026-08-22 against HA core's real current
    # source before this project ever used these values).
    module(
        "homeassistant.components.number",
        NumberDeviceClass=types.SimpleNamespace(
            POWER="power", ENERGY="energy", ENERGY_STORAGE="energy_storage"
        ),
        NumberEntity=_generic_stub_class("NumberEntity"),
        NumberMode=types.SimpleNamespace(BOX="box"),
        RestoreNumber=_generic_stub_class("RestoreNumber"),
    )
    module(
        "homeassistant.components.switch",
        SwitchEntity=_generic_stub_class("SwitchEntity"),
    )
    module(
        "homeassistant.config_entries",
        ConfigEntry=_generic_stub_class("ConfigEntry"),
        ConfigSubentry=_generic_stub_class("ConfigSubentry"),
    )
    # Real string values + a REAL converter, not opaque MagicMocks -- this
    # exact area (a solar sensor reporting W while battery/grid sensors
    # report kW) has real, documented live bug history (confirmed
    # 2026-08-15), so a test exercising _current_measured_power()'s unit
    # handling needs genuine numeric conversion to mean anything, not just
    # "was PowerConverter.convert() called."
    module(
        "homeassistant.const",
        UnitOfPower=types.SimpleNamespace(WATT="W", KILO_WATT="kW", MEGA_WATT="MW"),
        Platform=MagicMock(),
    )
    module("homeassistant.core", HomeAssistant=_generic_stub_class("HomeAssistant"))
    module("homeassistant.helpers")
    # DeviceInfo is a TypedDict in real HA -- calling it like DeviceInfo(x=1)
    # just returns a plain dict at runtime (TypedDict's own __call__
    # behaviour), NOT a class instance needing __init__ to accept kwargs.
    # A _generic_stub_class here would break on the very first real call
    # site (confirmed live 2026-08-22: number.py's own DeviceInfo(...)
    # call raised "takes no arguments" before this fix).
    module("homeassistant.helpers.entity", DeviceInfo=dict)
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=_generic_stub_class("AddEntitiesCallback"))
    module("homeassistant.helpers.entity_registry", async_get=MagicMock())
    module("homeassistant.helpers.restore_state", RestoreEntity=_generic_stub_class("RestoreEntity"))
    module(
        "homeassistant.helpers.event",
        async_track_time_change=MagicMock(),
        # Added 2026-08-22, real gap this exact bug hit: __init__.py's own
        # Solver-runtime scheduling imports this at module level, and this
        # stub not having it made EVERY test importing custom_components.
        # nimbus_load (not just __init__.py's own tests) fail to collect
        # at all with a raw ImportError -- not a hypothetical, this is what
        # broke test_init_forecast_entity_rename.py's real, already-passing
        # test suite the moment that import landed.
        async_track_time_interval=MagicMock(),
    )
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_generic_stub_class("DataUpdateCoordinator"),
        CoordinatorEntity=_generic_stub_class("CoordinatorEntity"),
    )
    module("homeassistant.loader", async_get_integration=MagicMock())
    module("homeassistant.util", dt=MagicMock())
    module("homeassistant.util.unit_conversion", PowerConverter=_StubPowerConverter)
