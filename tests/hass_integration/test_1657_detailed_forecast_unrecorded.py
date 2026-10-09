"""nimbus #1657 against a real Home Assistant and a real recorder: the price
entities carry the full resolved series live as `detailedForecast`, and the
recorder stores the state and the small metadata but not the series.

What the stub suite cannot prove: that HA builds `state_info` from the
entity's `_unrecorded_attributes` on this publish path, and that the
recorder honours it. #944 is the reason to check rather than assume: on the
`states.async_set()` and REST paths that set does not apply at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nimbus_load import solver_writer
from custom_components.nimbus_load.const import DOMAIN

PARENT = "sensor.nimbus_solver_battery_forecast"
IMPORT = "sensor.nimbus_solver_current_import_price"


def _rows(n: int = 200) -> list[dict]:
    """A plan long enough that its price series alone is well over the
    recorder's 16 KiB attribute cap."""
    start = datetime(2026, 10, 9, 10, 0, tzinfo=dt_util.get_default_time_zone())
    rows = []
    t = start
    for i in range(n):
        hours = 1 / 60 if i < 30 else 5 / 60
        rows.append(
            {
                "time": t.isoformat(),
                "hours": round(hours, 4),
                "battery_kw": 0.0,
                "import_price": round(0.30 + i * 0.001, 4),
                "import_price_raw": round(0.25 + i * 0.001, 4),
                "import_price_source": "primary",
                "export_price": -0.02 if i % 7 == 0 else 0.08,
                "export_price_raw": 0.08,
                "export_price_source": "primary",
            }
        )
        t += timedelta(hours=hours)
    return rows


async def test_detailed_forecast_is_live_and_not_recorded(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus", data={}, options={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    rows = _rows()
    solver_writer.ha_post_state(
        PARENT,
        0.0,
        {"forecast": rows, "status": "optimal", "unit_of_measurement": "kW"},
    )
    await hass.async_block_till_done()

    live = hass.states.get(IMPORT)
    assert live is not None
    detailed = live.attributes.get("detailedForecast")
    assert detailed is not None and len(detailed) == len(rows)
    assert detailed[0]["value"] == rows[0]["import_price"]
    assert live.attributes["detailed_forecast_meta"]["periods"] == len(rows)

    await async_wait_recording_done(hass)
    start = dt_util.utcnow() - timedelta(hours=1)
    recorded = await hass.async_add_executor_job(
        history.get_significant_states, hass, start, None, [IMPORT]
    )
    states = recorded.get(IMPORT, [])
    assert states, "the price entity's state was not recorded at all"
    last = states[-1]
    assert "detailedForecast" not in last.attributes
    # The small metadata survives, so history can still say which plan a
    # recorded price belonged to.
    assert last.attributes.get("detailed_forecast_meta", {}).get("periods") == len(rows)
