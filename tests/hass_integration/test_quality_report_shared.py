"""`nimbus_load.compute_quality_report` runs one oracle solve per window,
not one per caller (5 Nov 2026).

The Regret tab's three cards each ask for their day on every render: a page
load, and every save while a household edits the dashboard. Each request was
a full oracle MILP. On the test box three at once took 15 s of solver time;
a tester's smaller box stalled for about five minutes while they replaced
their tabs, and came back when they closed the Nimbus browser tabs.

Called the way the cards call it, three times at once for the same day, the
scorer now runs once and all three get its answer; a different day is still
scored; a re-score drops the remembered answers.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import solver_shared, solver_writer
from custom_components.nimbus_load.const import DOMAIN


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (quality report)", data={}, options={}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_one_solve_per_window_however_many_cards_ask(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry, monkeypatch
):
    scored: list[float] = []
    running = 0
    most_at_once = 0
    guard = threading.Lock()

    def fake_report(cfg, start, end, *, allow_partial):
        nonlocal running, most_at_once
        with guard:
            running += 1
            most_at_once = max(most_at_once, running)
        time.sleep(0.2)  # a solve takes a while; callers overlap
        with guard:
            running -= 1
            scored.append(start.timestamp())
        return {"epr_pct": 95.0}

    monkeypatch.setattr(solver_shared, "fetch_solver_config", dict)
    monkeypatch.setattr(solver_writer, "_compute_report_for_window", fake_report)
    monkeypatch.setattr(
        solver_writer, "rescore_quality_history", lambda *a, **k: {"rows": 0}
    )

    today = dt_util.start_of_local_day()
    day = today - timedelta(days=1)
    earlier = today - timedelta(days=2)

    async def ask(start, end):
        return await hass.services.async_call(
            DOMAIN,
            "compute_quality_report",
            {"start": start, "end": end, "allow_partial": False},
            blocking=True,
            return_response=True,
        )

    answers = await asyncio.gather(*(ask(day, today) for _ in range(3)))
    assert scored == [day.timestamp()], "each card ran its own solve"
    assert all(a["epr_pct"] == 95.0 for a in answers)

    await asyncio.gather(ask(earlier, day), ask(day, today))
    assert scored == [day.timestamp(), earlier.timestamp()]
    assert most_at_once == 1, "two solves ran at once"

    await hass.services.async_call(
        DOMAIN, "rescore_history", {"days": 1}, blocking=True, return_response=True
    )
    await ask(day, today)
    assert scored.count(day.timestamp()) == 2, "a re-score kept the old answer"
