"""A car charging inside the whole-house meter is counted once, not twice.

A tester, 22 Nov 2026 (0.109.0): the plan's period 0 takes the live whole-
house meter (13.0 kW, the car's 11 kW charging included) and then plans the
car's 11 kW charge again as the participant's own battery. The plan saw no
spare solar, held the house battery, and solar was exported at -0.1c every
minute the car charged. Period 0's house load is the meter less what the
participants are charging now, read from each participant's own (required)
power sensor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import solver_shared as shared
import solver_writer
from solver_inputs import load as load_inputs

N = 3
_NOW = datetime(2026, 11, 22, 2, 31, tzinfo=UTC)
_GRID = [_NOW + timedelta(minutes=i) for i in range(N)]
HOUSE = "sensor.whole_house"
EV_POWER = "sensor.ev_charger_power"


def _site(monkeypatch, participants):
    subentries = {
        f"p{i}": SimpleNamespace(
            subentry_id=f"p{i}", subentry_type="battery_participant", data=data
        )
        for i, data in enumerate(participants)
    }
    entry = SimpleNamespace(subentries=subentries)
    monkeypatch.setattr(
        shared.NATIVE,
        "hass",
        SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry])
        ),
    )


def _build(states):
    cfg = {
        "solver_load_forecast_entities": ["sensor.a"],
        "solver_load_forecast_sensor": "",
        "solver_whole_house_cross_check_sensor": HOUSE,
    }
    summed = ([1.5, 1.5, 1.5], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [], [], 48.0)

    def ha_get(eid):
        state, unit = states[eid]
        return {"state": state, "attributes": {"unit_of_measurement": unit}}

    with (
        patch.object(load_inputs, "sum_load_forecasts", return_value=summed),
        patch.object(
            load_inputs, "resolve_load_forecast_source_label", return_value="summed"
        ),
        patch.object(solver_writer, "entity_exists", return_value=True),
        patch.object(solver_writer, "ha_get", side_effect=ha_get),
        patch.object(shared, "ha_get", side_effect=ha_get),
        patch.object(solver_writer, "resample_forecast", return_value=[1.5]),
        patch.object(load_inputs, "_clear_load_forecast_error_notification_if_needed"),
    ):
        return load_inputs.build_load_arrays(cfg, _GRID, N, _NOW)


CAR = {
    "battery_participant_name": "Our EV",
    "battery_participant_power_sensor": EV_POWER,
    "battery_participant_power_positive_is_charge": True,
}


@pytest.mark.parametrize(
    ("house", "car", "participant", "expected"),
    [
        # The tester's minute: 13.0 kW on the meter, 11 kW of it the car.
        (("13.0", "kW"), ("11.0", "kW"), CAR, 2.0),
        # A charger reporting watts.
        (("13.0", "kW"), ("11000", "W"), CAR, 2.0),
        # Not charging: nothing taken off.
        (("13.0", "kW"), ("0", "kW"), CAR, 13.0),
        # An unreadable car sensor: nothing taken off, never a crash.
        (("13.0", "kW"), ("unavailable", "kW"), CAR, 13.0),
        # A car discharging (V2H) is not charging: nothing taken off.
        (
            ("13.0", "kW"),
            ("11.0", "kW"),
            {**CAR, "battery_participant_power_positive_is_charge": False},
            13.0,
        ),
        # Never below zero.
        (("5.0", "kW"), ("11.0", "kW"), CAR, 0.0),
    ],
)
def test_period_zero_house_load_leaves_out_the_car_charging(
    monkeypatch, house, car, participant, expected
):
    _site(monkeypatch, [participant])
    got = _build({HOUSE: house, EV_POWER: car})
    assert got.load_kw[0] == pytest.approx(expected)
    # The meter's own reading is still published as it is.
    assert got.live_load_kw == pytest.approx(float(house[0]))
