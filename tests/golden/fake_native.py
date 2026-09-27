"""Native-mode driver for the golden master (nimbus issue #1335).

Spec 000 Part A drives ``solver_writer.main()`` through the standalone
(REST) path, where ``_NATIVE_HASS is None``. Three functions return
early on exactly that condition -- ``build_controllable_loads``,
``apply_commanded_state_guard`` and the ``_update_all`` coroutine nested
inside the second -- so no golden scenario could reach them at all.
Spec 000's own measurement: 2.4% and 1.1% of their statements.

This module is the other half of ``fake_ha.FakeHA``. That one fakes the
eight REST endpoints ``solver_writer`` calls when it has no ``hass``;
this one fakes the in-process Home Assistant objects it calls when it
does:

    hass.states.get / states.async_set
    hass.services.async_call            (incl. blocking + return_response)
    hass.config_entries.async_entries   (the ConfigSubentry surface)
    hass.add_job                        (ha_post_state / ha_call_service)
    hass.loop                           (asyncio.run_coroutine_threadsafe)
    homeassistant.helpers.storage.Store        (LoadRunState persistence)
    homeassistant.helpers.entity_registry      (power-sensor discovery)
    homeassistant.components.recorder[.history]

Two deliberate decisions, because both affect what a snapshot records:

- **One source of truth for entity state.** ``FakeNativeHass`` reads and
  writes the same ``FakeHA`` instance the REST fake uses, and records
  every read into its ``requests`` list (method ``NATIVE_GET`` /
  ``NATIVE_HISTORY``, so a native read is never mistaken for a REST one)
  and every write into ``posted``. The record shape is therefore
  unchanged, and "which entities did main() ask for" is pinned in native
  mode exactly as it is in standalone mode.

- **``add_job`` runs its target to completion instead of scheduling it.**
  Real HA's ``add_job`` is fire-and-forget, which would make the order of
  ``posted`` depend on event-loop timing -- unusable for an exact
  snapshot. The fake blocks, which is the only difference between this
  and real native behaviour that a caller could observe.

Nothing here has an import-time side effect. ``install_native_modules``
mutates ``sys.modules`` and is called by ``golden.child`` inside its own
fresh interpreter, never at collection time in the pytest process: a
fake ``homeassistant`` package installed there would shadow the real one
for every other test in the run, which is the cross-contamination
``tests/test_1329_*`` already exists for.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import threading
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from golden.fake_ha import FakeHA, HistoryPoint

# ---------------------------------------------------------------------------
# State objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _State:
    """The three attributes solver_writer reads off a hass State."""

    entity_id: str
    state: Any
    attributes: Mapping[str, Any]
    last_changed: datetime | None = None


@dataclass
class _States:
    """hass.states, over FakeHA's own dict."""

    fake: FakeHA

    def get(self, entity_id: str) -> _State | None:
        self.fake.requests.append(
            {"method": "NATIVE_GET", "path": f"/api/states/{entity_id}", "query": ""}
        )
        raw = self.fake.states.get(entity_id)
        if raw is None:
            return None
        return _State(
            entity_id=entity_id,
            state=raw.get("state"),
            attributes=dict(raw.get("attributes") or {}),
        )

    def async_set(self, entity_id: str, state: Any, attributes: dict) -> None:
        self.fake.posted.append(
            {"entity_id": entity_id, "state": state, "attributes": attributes}
        )
        self.fake.states[entity_id] = {
            "state": state,
            "attributes": dict(attributes or {}),
        }


@dataclass
class _Services:
    """hass.services. ``return_response=True`` returns the payload itself,
    not the REST endpoint's ``{"service_response": ...}`` envelope."""

    fake: FakeHA

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        blocking: bool = False,
        return_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.fake.service_calls.append(
            {
                "domain": domain,
                "service": service,
                "return_response": return_response,
                "data": data,
            }
        )
        if not return_response:
            return None
        key = f"{domain}.{service}"
        if key not in self.fake.service_responses:
            raise RuntimeError(f"golden native: no fake response for {key}")
        return self.fake.service_responses[key]


# ---------------------------------------------------------------------------
# Config entries / subentries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeSubentry:
    """The ConfigSubentry surface build_controllable_loads() reads:
    ``subentry_id``, ``subentry_type``, ``data``, ``title``."""

    subentry_id: str
    subentry_type: str
    data: Mapping[str, Any]
    title: str = ""


@dataclass
class _ConfigEntry:
    entry_id: str
    subentries: dict[str, FakeSubentry]
    title: str = "Nimbus (golden)"
    state: Any = None


@dataclass
class _ConfigEntries:
    entries: list[_ConfigEntry]

    def async_entries(self, domain: str) -> list[_ConfigEntry]:
        return list(self.entries)


# ---------------------------------------------------------------------------
# homeassistant.helpers.storage.Store
# ---------------------------------------------------------------------------

# Where a fake Store writes. Set by install_native_modules(); a real HA
# Store writes under .storage/, and putting these in the scenario's own
# workdir is what gets them into the golden record's `files` map, so the
# persisted LoadRunState (commanded_state, activations, the whole
# day-ahead plan_forecast) is snapshotted rather than invisible.
_STORE_DIR: Path | None = None


class FakeStore:
    """``Store(hass, version, key)`` with the two methods
    ``load_run_state.LoadRunStateStore`` actually calls."""

    def __init__(self, hass: Any, version: int, key: str) -> None:
        self._key = key
        self._version = version

    @property
    def _path(self) -> Path:
        assert _STORE_DIR is not None, "install_native_modules() was not called"
        return _STORE_DIR / f"store.{self._key}.json"

    async def async_load(self) -> dict | None:
        path = self._path
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    async def async_save(self, data: dict) -> None:
        self._path.write_text(
            json.dumps(data, sort_keys=True, indent=1), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# homeassistant.helpers.entity_registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeRegistryEntry:
    entity_id: str
    device_id: str | None = None
    device_class: str | None = None
    original_device_class: str | None = None
    disabled_by: Any = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass
class _EntityRegistry:
    entries: tuple[FakeRegistryEntry, ...] = ()

    def async_get(self, entity_id: str) -> FakeRegistryEntry | None:
        for entry in self.entries:
            if entry.entity_id == entity_id:
                return entry
        return None


# ---------------------------------------------------------------------------
# The hass object
# ---------------------------------------------------------------------------


@dataclass
class FakeNativeHass:
    """Enough of ``hass`` for ``solver_writer``'s native seam."""

    fake: FakeHA
    subentries: Sequence[FakeSubentry] = ()
    registry_entries: Sequence[FakeRegistryEntry] = ()
    entry_id: str = "golden_hub_entry"
    time_zone: str = "Australia/Brisbane"

    states: _States = field(init=False)
    services: _Services = field(init=False)
    config_entries: _ConfigEntries = field(init=False)
    config: Any = field(init=False)
    entity_registry: _EntityRegistry = field(init=False)
    loop: asyncio.AbstractEventLoop = field(init=False)
    data: dict = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.states = _States(self.fake)
        self.services = _Services(self.fake)
        self.config_entries = _ConfigEntries(
            [
                _ConfigEntry(
                    entry_id=self.entry_id,
                    subentries={s.subentry_id: s for s in self.subentries},
                )
            ]
        )
        self.config = types.SimpleNamespace(time_zone=self.time_zone)
        self.entity_registry = _EntityRegistry(tuple(self.registry_entries))
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, daemon=True, name="golden-native-loop"
        )
        self._thread.start()

    # -- hass.add_job --------------------------------------------------------

    def add_job(self, target: Any, *args: Any) -> None:
        """Run ``target`` now, awaiting it if it is a coroutine.

        Deliberately blocking -- see this module's own docstring. A call
        that originates ON the loop thread cannot block on it, so it is
        scheduled there instead; nothing in solver_writer does that today,
        but a deadlock would be a much worse failure than a slightly
        looser ordering guarantee.
        """
        result = target(*args)
        if not inspect.isawaitable(result):
            return
        if threading.current_thread() is self._thread:
            self.loop.create_task(result)
            return
        asyncio.run_coroutine_threadsafe(_drain(result), self.loop).result(timeout=30)

    # -- teardown ------------------------------------------------------------

    def close(self) -> None:
        """Stop, join and close the loop. An event loop's own self-pipe
        holds real OS sockets that only ``run_forever``'s thread releases
        -- the same leak ``test_solver_writer_controllable_loads.py``'s
        ``_make_running_loop`` documents."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        self.loop.close()


async def _drain(awaitable: Any) -> Any:
    return await awaitable


# ---------------------------------------------------------------------------
# sys.modules installation
# ---------------------------------------------------------------------------


def _history_states(fake: FakeHA, entity_id: str, start, end) -> list[_State]:
    """FakeHA's recorded history as recorder-shaped State objects.

    Attributes carry forward from the last row that declared them, which
    is what a real ``state_changes_during_period(no_attributes=False)``
    read gives and what ``fake_ha``'s REST branch models with its
    "row 0 and any row with attributes" rule.
    """
    from golden.fake_ha import _parse_utc

    out: list[_State] = []
    carried: Mapping[str, Any] = {}
    points: Sequence[HistoryPoint] = fake.history.get(entity_id, ())
    for point in points:
        if point.attributes is not None:
            carried = dict(point.attributes)
        stamp = _parse_utc(point.last_changed)
        if stamp < start or stamp >= end:
            continue
        out.append(
            _State(
                entity_id=entity_id,
                state=point.state,
                attributes=dict(carried),
                last_changed=stamp,
            )
        )
    return out


def install_native_modules(hass: FakeNativeHass, store_dir: Path) -> None:
    """Register the fake ``homeassistant.*`` modules ``solver_writer``
    imports lazily inside its native branches.

    Called from the child process only. Overwrites rather than
    ``setdefault``s, so the result does not depend on whether a real
    ``homeassistant`` happens to be installed in the environment -- an
    env-dependent golden master is not a golden master.
    """
    global _STORE_DIR
    _STORE_DIR = store_dir

    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = FakeStore  # type: ignore[attr-defined]

    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    def _async_get(_hass: Any) -> _EntityRegistry:
        return hass.entity_registry

    def _async_entries_for_device(
        registry: _EntityRegistry,
        device_id: str,
        include_disabled_entities: bool = False,
    ) -> list[FakeRegistryEntry]:
        return [
            e
            for e in registry.entries
            if e.device_id == device_id
            and (include_disabled_entities or e.disabled_by is None)
        ]

    entity_registry.async_get = _async_get  # type: ignore[attr-defined]
    entity_registry.async_entries_for_device = (  # type: ignore[attr-defined]
        _async_entries_for_device
    )

    components = types.ModuleType("homeassistant.components")
    recorder = types.ModuleType("homeassistant.components.recorder")
    history = types.ModuleType("homeassistant.components.recorder.history")

    def _state_changes_during_period(
        _hass: Any,
        start: datetime,
        end: datetime,
        entity_id: str,
        no_attributes: bool = False,
        **kwargs: Any,
    ) -> dict[str, list[_State]]:
        hass.fake.requests.append(
            {
                "method": "NATIVE_HISTORY",
                "path": f"/recorder/{entity_id}",
                "query": f"start={start.isoformat()}&end={end.isoformat()}",
            }
        )
        return {entity_id: _history_states(hass.fake, entity_id, start, end)}

    history.state_changes_during_period = (  # type: ignore[attr-defined]
        _state_changes_during_period
    )

    class _Instance:
        async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
            return func(*args)

    def _get_instance(_hass: Any) -> _Instance:
        return _Instance()

    recorder.get_instance = _get_instance  # type: ignore[attr-defined]
    recorder.history = history  # type: ignore[attr-defined]
    components.recorder = recorder  # type: ignore[attr-defined]
    helpers.storage = storage  # type: ignore[attr-defined]
    helpers.entity_registry = entity_registry  # type: ignore[attr-defined]
    ha.helpers = helpers  # type: ignore[attr-defined]
    ha.components = components  # type: ignore[attr-defined]

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.storage": storage,
            "homeassistant.helpers.entity_registry": entity_registry,
            "homeassistant.components": components,
            "homeassistant.components.recorder": recorder,
            "homeassistant.components.recorder.history": history,
        }
    )


def register_managed_entity_handlers(solver_writer: Any, hass: FakeNativeHass) -> None:
    """Register a handler for every entity_id in
    ``_NATIVE_MANAGED_ENTITY_IDS``, as ``sensor.py``'s own
    ``async_setup_entry`` does on a real native install.

    Without this, ``ha_post_state`` takes its "no handler registered yet"
    branch for those eleven entity_ids and drops the write entirely (the
    #312 ghost-state guard) -- so the scenario would solve correctly and
    record nothing, and the snapshot would pin an empty ``posted`` list.
    """
    for entity_id in solver_writer._NATIVE_MANAGED_ENTITY_IDS:
        solver_writer.register_entity_handler(
            entity_id, _handler_for(hass, entity_id), real_entity_id=entity_id
        )


def _handler_for(hass: FakeNativeHass, entity_id: str):
    def _handler(state: Any, attributes: dict) -> None:
        hass.states.async_set(entity_id, state, attributes)

    return _handler
