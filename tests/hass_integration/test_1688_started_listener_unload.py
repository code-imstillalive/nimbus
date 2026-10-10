"""nimbus #1688 (Mark Purcell): a reload after a cold start logged
"Unable to remove unknown job listener" as an ERROR, twice, on every reload.

`_pricing_check` and `_energy_unit_check` were deferred to HA's
"homeassistant_started" with `entry.async_on_unload(hass.bus.async_listen_once(...))`.
A once-listener removes itself when the event fires, so the unload hook later
removed it a second time and HA core logged the failure.

Real HA event bus and real config-entry unload callbacks, because the stub
tree does not model either. The first test is the control: it shows this
harness reproduces the ERROR with the old pattern, so the others mean
something.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import _run_once_started
from custom_components.nimbus_load.const import DOMAIN

_ERROR = "Unable to remove unknown job listener"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus (#1688)", data={}, options={})
    entry.add_to_hass(hass)
    return entry


async def test_control_the_old_pattern_logs_the_error(hass: HomeAssistant, caplog):
    entry = _entry(hass)
    calls = []

    async def job(_event=None):
        calls.append(1)

    entry.async_on_unload(hass.bus.async_listen_once("homeassistant_started", job))
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    with caplog.at_level(logging.ERROR):
        await entry._async_process_on_unload(hass)
    assert calls == [1]
    assert _ERROR in caplog.text


async def test_unload_after_the_event_fired_logs_nothing(hass: HomeAssistant, caplog):
    entry = _entry(hass)
    calls = []

    async def job(_event=None):
        calls.append(1)

    _run_once_started(hass, entry, job)
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    with caplog.at_level(logging.ERROR):
        await entry._async_process_on_unload(hass)
    assert calls == [1]
    assert _ERROR not in caplog.text


async def test_unload_before_the_event_cancels_the_job(hass: HomeAssistant, caplog):
    entry = _entry(hass)
    calls = []

    async def job(_event=None):
        calls.append(1)

    _run_once_started(hass, entry, job)
    with caplog.at_level(logging.ERROR):
        await entry._async_process_on_unload(hass)
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    assert calls == []
    assert _ERROR not in caplog.text


async def test_the_job_runs_exactly_once(hass: HomeAssistant):
    entry = _entry(hass)
    calls = []

    async def job(_event=None):
        calls.append(1)

    _run_once_started(hass, entry, job)
    hass.bus.async_fire("homeassistant_started")
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    assert calls == [1]
