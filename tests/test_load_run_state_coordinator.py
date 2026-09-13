"""nimbus issue #828: real tests for
load_run_state_coordinator.NimbusLoadRunStateCoordinator -- the single
shared per-hub coordinator that replaces the old 13xN independent
Store reads every Controllable Load sensor used to do on its own. See
that module's own docstring for the full incident/root-cause story.

Same stub-based pattern as the other sensor test files -- exercises the
REAL coordinator class against tests/_ha_stubs.py's stand-in
homeassistant.* modules, including its own real _StubStore for the
Store.async_load() read this coordinator performs in
_async_update_data().
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import load_run_state, load_run_state_coordinator

NimbusLoadRunStateCoordinator = load_run_state_coordinator.NimbusLoadRunStateCoordinator


def _fake_entry(entry_id: str = "entry_x") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def test_get_before_any_refresh_returns_a_safe_default():
    entry = _fake_entry()
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    assert coordinator.data is None
    result = coordinator.get("some_subentry")
    assert result == load_run_state.LoadRunState()


def test_get_with_unknown_subentry_id_returns_a_safe_default():
    entry = _fake_entry()
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    coordinator.data = {"known": load_run_state.LoadRunState(commanded_state=True)}
    assert coordinator.get("unknown") == load_run_state.LoadRunState()


def test_get_with_known_subentry_id_returns_its_real_state():
    entry = _fake_entry()
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    real_state = load_run_state.LoadRunState(
        commanded_state=True, delivered_today_kwh=2.5
    )
    coordinator.data = {"s1": real_state}
    assert coordinator.get("s1") is real_state


def test_async_update_data_with_empty_store_returns_empty_dict():
    load_run_state_coordinator.Store._shared_data.clear()
    entry = _fake_entry("entry_empty")
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    result = asyncio.run(coordinator._async_update_data())
    assert result == {}


def test_async_update_data_reads_and_parses_every_configured_load():
    # nimbus issue #828's own core claim: ONE read serves every load in
    # this hub, not one read per load.
    load_run_state_coordinator.Store._shared_data.clear()
    entry = _fake_entry("entry_multi")
    raw_store = load_run_state_coordinator.Store(
        MagicMock(), 1, "nimbus_load_entry_multi_load_run_state"
    )
    asyncio.run(
        raw_store.async_save(
            {
                "load_a": load_run_state.LoadRunState(
                    commanded_state=True, delivered_today_kwh=1.5
                ).to_dict(),
                "load_b": load_run_state.LoadRunState(
                    commanded_state=False, delivered_today_kwh=0.0
                ).to_dict(),
            }
        )
    )
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    result = asyncio.run(coordinator._async_update_data())
    assert set(result.keys()) == {"load_a", "load_b"}
    assert result["load_a"].commanded_state is True
    assert result["load_a"].delivered_today_kwh == 1.5
    assert result["load_b"].commanded_state is False


def test_async_update_data_survives_a_corrupt_entry_for_one_load():
    # A malformed entry for ONE load must not take down the whole hub's
    # worth of Controllable Load sensors -- same "never crash the whole
    # solve over one bad load" posture as every other defensive read in
    # this project.
    load_run_state_coordinator.Store._shared_data.clear()
    entry = _fake_entry("entry_corrupt")
    raw_store = load_run_state_coordinator.Store(
        MagicMock(), 1, "nimbus_load_entry_corrupt_load_run_state"
    )
    asyncio.run(
        raw_store.async_save(
            {
                "good_load": load_run_state.LoadRunState(
                    commanded_state=True
                ).to_dict(),
                "bad_load": "not a real state dict",
            }
        )
    )
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)
    result = asyncio.run(coordinator._async_update_data())
    assert result["good_load"].commanded_state is True
    assert result["bad_load"] == load_run_state.LoadRunState()


def test_async_update_data_survives_a_read_failure():
    entry = _fake_entry("entry_boom")
    coordinator = NimbusLoadRunStateCoordinator(MagicMock(), entry)

    async def _raise():
        raise OSError("disk gone")

    coordinator._store.async_load = _raise
    result = asyncio.run(coordinator._async_update_data())
    assert result == {}
