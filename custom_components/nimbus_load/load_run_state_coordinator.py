"""nimbus issue #828: one shared DataUpdateCoordinator per hub for every
Controllable Load's own persisted `load_run_state.LoadRunState`.

Confirmed live (2026-09-13, a real devhub incident): sensor.py's own 13
Controllable Load sensor classes (NimbusControllableLoadStateSensor,
NimbusControllableLoadTemperatureForecastSensor, and the eleven
_NimbusControllableLoadScheduleSensorBase subclasses) each used to do
their OWN independent `Store(...).async_load()` inside their own
`async_update()`, every poll -- and since `LoadRunStateStore`'s own file
is scoped per HUB, not per load (every configured load's state lives in
the SAME JSON file, keyed by subentry_id), that meant 13xN completely
redundant reads of the identical file every poll cycle, for N configured
loads. On a resource-constrained host, that amplification factor turned
an already-slow moment into dozens of entities each blocking for 10+
seconds simultaneously -- see issue #828's own investigation for the
full log evidence.

This coordinator replaces all of that with exactly ONE `Store.async_load()`
per hub per poll, shared by every one of that hub's Controllable Load
sensor entities via the normal CoordinatorEntity push mechanism (no
polling on the entity side at all -- see sensor.py's own conversion).

Deliberately NOT added to coordinator.py: that file's own NimbusCoordinator
is instantiated once per Load/Signal SUBENTRY (see __init__.py's own
`coordinators = {subentry_id: NimbusCoordinator(...)}` dict, comment: "one
coordinator per load/power_signal subentry") for genuinely unrelated ML
forecast/retrain concerns. load_run_state's own Store is scoped per HUB
(one config entry), not per subentry, so it doesn't fit that existing
per-subentry dict/pattern -- a second, smaller, purpose-built coordinator
here is a cleaner fit than forcing this into that unrelated shape.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import load_run_state
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import NimbusConfigEntry

_LOGGER = logging.getLogger(__name__)

# nimbus issue #828: matches this platform's own pre-existing implicit
# poll cadence (HA's default entity scan interval) -- a deliberate,
# explicit choice rather than inheriting whatever that default happened
# to be, but not a change to how fresh the published data is. Dispatch
# itself is unaffected either way: apply_commanded_state_guard() still
# writes to this same Store every solve cycle regardless of how often
# the SENSOR side re-reads it.
UPDATE_INTERVAL = timedelta(seconds=30)


class NimbusLoadRunStateCoordinator(
    DataUpdateCoordinator[dict[str, load_run_state.LoadRunState]]
):
    """One instance per hub, shared by every Controllable Load's own
    sensor entities under that hub. `get()` is the real, safe accessor
    every sensor calls -- mirrors LoadRunStateStore.async_read()'s own
    "never None, a fresh LoadRunState is a safe fallback" contract
    exactly, so converting a sensor from its own async_update() to
    reading this coordinator changes nothing about its own behaviour
    for a load with no data yet.
    """

    def __init__(self, hass: HomeAssistant, entry: NimbusConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_load_run_state_{entry.entry_id}",
            update_interval=UPDATE_INTERVAL,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_{entry.entry_id}_load_run_state"
        )

    async def _async_update_data(self) -> dict[str, load_run_state.LoadRunState]:
        # Same "never crash, a fresh/empty state is a safe fallback"
        # posture LoadRunStateStore.async_read() already has -- a read
        # failure here must not mark every Controllable Load sensor
        # under this hub unavailable; that would be a real regression
        # from each sensor's own previous independent-read behaviour,
        # which already tolerated a corrupt/unreadable store file the
        # same way.
        try:
            raw = await self._store.async_load()
        except Exception as e:  # noqa: BLE001 -- see comment above
            _LOGGER.warning(
                "Nimbus: load_run_state coordinator read failed -- every "
                "Controllable Load under this hub reports a fresh/empty "
                "state this cycle: %s",
                e,
            )
            return {}
        if not raw:
            return {}
        result: dict[str, load_run_state.LoadRunState] = {}
        for key, value in raw.items():
            try:
                result[key] = load_run_state.LoadRunState.from_dict(value)
            except (TypeError, ValueError, AttributeError):
                # nimbus issue #828: a genuinely malformed entry (e.g. a
                # bare string instead of a dict) raises AttributeError
                # inside from_dict()'s own data.get(...) calls, not
                # TypeError/ValueError -- same fix applied to
                # LoadRunStateStore.async_read()'s own matching except
                # clause in load_run_state.py.
                result[key] = load_run_state.LoadRunState()
        return result

    def get(self, subentry_id: str) -> load_run_state.LoadRunState:
        """Never None, never raises -- a load with no persisted state
        yet (or a coordinator that hasn't completed its first refresh)
        reads as a fresh, safe-default LoadRunState, matching every
        sensor's own previous per-read behaviour exactly."""
        data = self.data or {}
        return data.get(subentry_id) or load_run_state.LoadRunState()
