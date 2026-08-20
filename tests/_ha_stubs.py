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


def _generic_stub_class(name: str) -> type:
    """A stub base class that tolerates HA's own generic-subscript usage,
    e.g. `class Foo(CoordinatorEntity[MyCoordinator], SensorEntity):` --
    plain `object` isn't subscriptable and would raise TypeError there.
    """
    return type(name, (), {"__class_getitem__": classmethod(lambda cls, item: cls)})


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
    module(
        "homeassistant.config_entries",
        ConfigEntry=_generic_stub_class("ConfigEntry"),
        ConfigSubentry=_generic_stub_class("ConfigSubentry"),
    )
    module("homeassistant.const", UnitOfPower=MagicMock(), Platform=MagicMock())
    module("homeassistant.core", HomeAssistant=_generic_stub_class("HomeAssistant"))
    module("homeassistant.helpers")
    module("homeassistant.helpers.entity", DeviceInfo=_generic_stub_class("DeviceInfo"))
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=_generic_stub_class("AddEntitiesCallback"))
    module("homeassistant.helpers.entity_registry", async_get=MagicMock())
    module("homeassistant.helpers.event", async_track_time_change=MagicMock())
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_generic_stub_class("DataUpdateCoordinator"),
        CoordinatorEntity=_generic_stub_class("CoordinatorEntity"),
    )
    module("homeassistant.loader", async_get_integration=MagicMock())
    module("homeassistant.util", dt=MagicMock())
    module("homeassistant.util.unit_conversion", PowerConverter=MagicMock())
