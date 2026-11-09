"""nimbus #2393: device_contract_ha's two recorder reads, and setup_health's, on a real recorder.

The other device-contract tests patch `_async_history` and the statistics
read, so the reads themselves were never run. Here nothing is patched:
states and statistics go into the real recorder and come back out.
Synthetic only (household: "100% safe")."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nimbus_load import device_contract_ha, setup_health


async def test_history_comes_back_as_a_series_with_gaps_as_none(
    hass: HomeAssistant,
):
    start = dt_util.utcnow() - timedelta(minutes=5)
    for value in ("1.5", "unavailable", "2.0"):
        hass.states.async_set("sensor.bat_power", value, {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    out = await device_contract_ha._async_history(
        hass, ["sensor.bat_power"], start, dt_util.utcnow() + timedelta(minutes=5)
    )

    assert [v for _t, v in out["sensor.bat_power"]] == [1.5, None, 2.0]

    # test_setup_health_reads_every_state_the_entity_had: the availability
    # check (#2014) reads the same recorder, states as written, by entity
    seen = await setup_health._async_states_seen(
        hass,
        ["sensor.bat_power", "sensor.never_recorded"],
        start,
        dt_util.utcnow() + timedelta(minutes=5),
    )
    assert seen == {"sensor.bat_power": ["1.5", "unavailable", "2.0"]}


async def test_statistics_give_the_latest_row_and_its_unit(hass: HomeAssistant):
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        mean = {"mean_type": StatisticMeanType.NONE}
    except ImportError:  # pragma: no cover - older recorder
        mean = {"has_mean": False}
    async_import_statistics(
        hass,
        {
            **mean,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.bat_out_kwh",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": hour - timedelta(hours=3), "sum": 1.0},
            {"start": hour - timedelta(hours=2), "sum": 3.0},
        ],
    )
    await async_wait_recording_done(hass)

    out = await device_contract_ha._async_statistics(
        hass, {"sensor.bat_out_kwh", "sensor.never_recorded"}
    )

    # A row with no state reports its sum; an unknown id is left out.
    assert list(out) == ["sensor.bat_out_kwh"]
    row = out["sensor.bat_out_kwh"]
    assert (row["value"], row["unit"]) == (3.0, "kWh")
    assert row["start"] == (hour - timedelta(hours=2)).isoformat()
    assert row["end"] == (hour - timedelta(hours=1)).isoformat()

    # No ids needs no read; a recorder that cannot be read is None, so the
    # binding is flagged as not validated rather than refused.
    assert await device_contract_ha._async_statistics(hass, set()) == {}
    with patch.object(
        device_contract_ha, "_async_statistics_rows", side_effect=RuntimeError
    ):
        assert (
            await device_contract_ha._async_statistics(hass, {"sensor.bat_out_kwh"})
            is None
        )
