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


def _stub_async_on_remove(self, func) -> None:
    """Real Entity.async_on_remove(func) contract: store func, call it
    (in reverse-registration order, in the real base class) when the
    entity is removed. Test code that needs to VERIFY cancellation just
    reads self._on_remove_callbacks directly and invokes whichever entry
    it wants -- no stub-level auto-invocation is provided, since no
    current test needs the stub to simulate a real removal lifecycle,
    only to observe that a real entity correctly registered its own
    cleanup. Added 2026-08-23 alongside _NimbusSolverPushSensor's own
    self-driven periodic re-check timer (Silver entity-unavailable) --
    the first real code in this tree to call async_on_remove at all."""
    if not hasattr(self, "_on_remove_callbacks"):
        self._on_remove_callbacks = []
    self._on_remove_callbacks.append(func)


class _StubSelectorConfig(dict):
    """Real HA *SelectorConfig classes are typed dataclasses that also
    serialize like a dict -- a plain dict subclass captures everything a
    schema-building test needs to inspect (which domain/min/max/mode got
    attached) without replicating the exact dataclass machinery."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class _StubSelectorBase:
    """Real HA selectors are themselves voluptuous-schema-compatible
    CALLABLES (used directly as the value-validator in
    vol.Schema({key: selector_instance})) -- this stores its own config
    for inspection and passes any value through unchanged when called,
    which is all a schema-building/merge-logic test needs. This
    integration's own tested logic never depends on real selector-side
    UI validation/coercion, only on which selector type + config ended
    up attached to which field.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config

    def __call__(self, value):
        return value


class _StubEntitySelector(_StubSelectorBase):
    pass


class _StubNumberSelector(_StubSelectorBase):
    pass


class _StubTextSelector(_StubSelectorBase):
    pass


class _StubSelectSelector(_StubSelectorBase):
    pass


class _StubFlowBase:
    """Bare stand-in for HA's real ConfigSubentryFlow / OptionsFlowWith-
    ConfigEntry. Real flow-control methods (async_create_entry /
    async_update_and_abort / async_show_form / async_show_menu) return a
    plain, inspectable dict describing what was decided, instead of
    running HA's own real flow-manager machinery (multi-step state
    tracking, translation lookup) underneath -- a test only needs to see
    WHAT a flow step decided (create vs. update vs. show a form for step
    X with schema Y), not HA's own already-proven plumbing for actually
    presenting it.

    __init__ is deliberately NOT overridden here -- real tests construct
    instances via ClassName.__new__(ClassName) (same bypass-heavy-
    __init__ technique already used for NimbusCoordinator) and set only
    the specific attributes (self.hass, self.source, self.config_entry,
    self._solver_data, ...) each test's own method under test actually
    reads.
    """

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, config_entry=None) -> None:
        # Matches real HA's OptionsFlowWithConfigEntry.__init__(self,
        # config_entry) signature/behavior closely enough for
        # NimbusHubOptionsFlow's own __init__ (which calls
        # super().__init__(*args, **kwargs) before setting its own
        # self._solver_data) to actually work when constructed the real
        # way (NimbusHubOptionsFlow(config_entry)), not just via the
        # __new__()-bypass technique used elsewhere in these tests.
        self.config_entry = config_entry

    def async_create_entry(self, *, title: str, data: dict):
        return {"type": "create_entry", "title": title, "data": data}

    def async_update_and_abort(self, entry, subentry, *, title: str, data: dict):
        return {"type": "update_and_abort", "title": title, "data": data}

    def async_show_form(self, *, step_id: str, data_schema):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}

    def async_show_menu(self, *, step_id: str, menu_options: list):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}


class _StubConfigFlow(_StubFlowBase):
    """Adds just enough of ConfigFlow's own class-level contract for
    `class NimbusConfigFlow(ConfigFlow, domain=DOMAIN):` to import
    successfully -- real ConfigFlow.__init_subclass__ does real domain-
    registry bookkeeping HA needs at runtime; a test has no such registry
    to register into, so this simply swallows the domain= (and any other
    future) class kwarg rather than replicate that bookkeeping.
    """

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id: str) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
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
            raise ValueError(
                f"unsupported unit in stub converter: {from_unit!r} -> {to_unit!r}"
            )
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

    A permissive __init__ (2026-08-23, real crash found testing
    NimbusForecastSensor directly): a real entity's own __init__ calling
    `super().__init__(coordinator)` -- CoordinatorEntity's real
    contract -- walks the MRO to whichever stub base actually defines
    __init__, and with none of them overriding it, that resolved to
    plain object.__init__, which raises TypeError on any extra
    positional/keyword arg. A no-op *args/**kwargs sink is the correct
    stand-in for the same reason async_added_to_hass above is a no-op,
    not a reimplementation of the real base class's own bookkeeping.
    """
    return type(
        name,
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "async_added_to_hass": _noop_async_added_to_hass,
            "async_on_remove": _stub_async_on_remove,
            "__init__": lambda self, *args, **kwargs: None,
        },
    )


class _StubCoordinatorEntity:
    """Dedicated (not the generic no-op factory above) -- real
    CoordinatorEntity.__init__(self, coordinator) genuinely stores
    self.coordinator, and several real entity classes' own properties
    (e.g. NimbusForecastSensor.extra_state_attributes reading
    self.coordinator.data) depend on that actually happening, not just
    on construction not crashing. Found live 2026-08-23 constructing
    NimbusForecastSensor directly for the first time in this test
    suite's history -- every entity test before this one only ever
    exercised bare SensorEntity/NumberEntity/SwitchEntity subclasses,
    which never needed this."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator, *args, **kwargs) -> None:
        self.coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        pass


def install_ha_stubs() -> None:
    def module(name: str, **attrs) -> types.ModuleType:
        # First-call-wins per attribute (2026-08-23): this module's own
        # docstring promises "idempotent -- safe to call from more than
        # one test module," but the previous unconditional setattr()
        # broke that promise for any attrs value built fresh at call
        # time (MagicMock(), etc.) -- every test FILE that calls
        # install_ha_stubs() re-evaluates e.g. SensorDeviceClass=
        # MagicMock() below, producing a NEW mock object each time,
        # while any class that already imported the OLD mock (e.g.
        # custom_components.nimbus_load.sensor's own class-attribute
        # `_attr_device_class = SensorDeviceClass.POWER`, bound once at
        # that module's own first import and cached by Python's normal
        # module system) stays pinned to the stale identity. A later
        # test file's `is SensorDeviceClass.POWER` assertion then
        # compares against a DIFFERENT MagicMock instance and fails --
        # reproducible only when the full suite runs in an order that
        # imports custom_components.nimbus_load.sensor before this
        # particular test file's own install_ha_stubs() call, which is
        # exactly why it passed in isolation and failed in the full
        # suite. install_ha_stubs() takes no arguments, so every call
        # already passes the exact same literal kwargs -- "first call
        # wins, later calls are true no-ops for already-set attributes"
        # changes nothing about intended behaviour, it just makes the
        # docstring's own claim actually true.
        mod = sys.modules.get(name) or types.ModuleType(name)
        for k, v in attrs.items():
            if not hasattr(mod, k):
                setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    module("homeassistant")
    module("homeassistant.components")

    # Real (not mocked) redaction, matching HA core's actual documented
    # behaviour (real HA's async_redact_data is, despite the name, a
    # plain synchronous function -- HA's own naming convention for a
    # helper meant to be called from an async context, not because it's
    # itself a coroutine). diagnostics.py's own tests need this to
    # genuinely redact, not just confirm "was this called."
    def _real_async_redact_data(data, to_redact):
        if isinstance(data, dict):
            return {
                k: "**REDACTED**"
                if k in to_redact
                else _real_async_redact_data(v, to_redact)
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [_real_async_redact_data(v, to_redact) for v in data]
        return data

    module(
        "homeassistant.components.diagnostics",
        async_redact_data=_real_async_redact_data,
    )
    module("homeassistant.components.recorder", get_instance=MagicMock())
    module("homeassistant.components.energy")

    # async_get_manager is a placeholder here -- real tests monkeypatch
    # it per-test via unittest.mock.patch("homeassistant.components.
    # energy.data.async_get_manager", ...) to control what fake energy
    # config a given test scenario returns. Registered as a real
    # (awaitable) async function, not a bare MagicMock, so an
    # un-monkeypatched call still returns something await-able instead
    # of raising -- matches this project's own "degrade gracefully by
    # default" convention.
    async def _default_async_get_manager(hass):
        return types.SimpleNamespace(data={})

    module(
        "homeassistant.components.energy.data",
        async_get_manager=_default_async_get_manager,
    )
    module(
        "homeassistant.components.recorder.history", get_significant_states=MagicMock()
    )
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
        ConfigFlow=_StubConfigFlow,
        ConfigFlowResult=dict,
        ConfigSubentryFlow=_StubFlowBase,
        OptionsFlow=_StubFlowBase,
        OptionsFlowWithConfigEntry=_StubFlowBase,
        # Real string values, verified 2026-08-22 against HA core's actual
        # current source (homeassistant/config_entries.py) rather than
        # guessed -- this integration's own real logic branches on
        # `self.source == SOURCE_RECONFIGURE`, so a wrong stand-in value
        # here would make a test pass for the wrong reason.
        SOURCE_USER="user",
        SOURCE_RECONFIGURE="reconfigure",
        SubentryFlowResult=dict,
    )
    module(
        "homeassistant.helpers.selector",
        EntitySelector=_StubEntitySelector,
        EntitySelectorConfig=_StubSelectorConfig,
        NumberSelector=_StubNumberSelector,
        NumberSelectorConfig=_StubSelectorConfig,
        NumberSelectorMode=types.SimpleNamespace(BOX="box", SLIDER="slider"),
        # TextSelector (2026-08-23, Power Source's own "name" field and PV
        # String's own "label" field) and SelectSelector (2026-08-23, the
        # live-built "which Power Source" dropdown on PV String/Battery
        # Tower) -- same real, minimal stub pattern as EntitySelector/
        # NumberSelector above.
        TextSelector=_StubTextSelector,
        SelectSelector=_StubSelectSelector,
        SelectSelectorConfig=_StubSelectorConfig,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"),
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
    module(
        "homeassistant.core",
        HomeAssistant=_generic_stub_class("HomeAssistant"),
        # Real HA's @callback just marks a function as event-loop-safe and
        # returns it unchanged -- a plain identity decorator is a faithful
        # stand-in, not a simplification that loses real behavior.
        callback=lambda func: func,
    )
    module("homeassistant.helpers")
    # DeviceInfo is a TypedDict in real HA -- calling it like DeviceInfo(x=1)
    # just returns a plain dict at runtime (TypedDict's own __call__
    # behaviour), NOT a class instance needing __init__ to accept kwargs.
    # A _generic_stub_class here would break on the very first real call
    # site (confirmed live 2026-08-22: number.py's own DeviceInfo(...)
    # call raised "takes no arguments" before this fix).
    module("homeassistant.helpers.entity", DeviceInfo=dict)
    module(
        "homeassistant.helpers.entity_platform",
        AddEntitiesCallback=_generic_stub_class("AddEntitiesCallback"),
    )
    module("homeassistant.helpers.entity_registry", async_get=MagicMock())
    module(
        "homeassistant.helpers.restore_state",
        RestoreEntity=_generic_stub_class("RestoreEntity"),
    )
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
        CoordinatorEntity=_StubCoordinatorEntity,
    )
    module("homeassistant.loader", async_get_integration=MagicMock())
    module("homeassistant.util", dt=MagicMock())
    module("homeassistant.util.unit_conversion", PowerConverter=_StubPowerConverter)
