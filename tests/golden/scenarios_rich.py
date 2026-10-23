"""Recorded days with every input a configured household feeds (#1837).

The synthetic and NEMWEB scenarios reach the plan with a minimal site: one
price pair and one load forecast. These two reach the rest of ``main()``: a
provider price forecast array, a regional spot forecast with its band, P2P
matched rate and settlement history, a P2P export block, a reserve window, a
charge-by target, solar and weather forecasts, measured power history for
the battery, solar, grid and whole house, the offer curve and the flex
signals, and (second scenario) the first cycle after midnight, which scores
the day before.

Every value is synthetic: smooth deterministic curves, no recorded household
data. The shapes follow the attributes the providers publish.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta

from golden.fake_ha import FakeHA, HistoryPoint
from golden.scenarios import Scenario, register

TZ = "+10:00"

LOAD = "sensor.nimbus_fake_house_power_forecast"
SOLAR_FC = "sensor.nimbus_fake_solar_power_forecast"

CONFIG = {
    "solver_battery_soc_sensor": "sensor.fake_battery_soc",
    "solver_battery_power_sensor": "sensor.fake_battery_power",
    "solver_battery_power_positive_is_charge": False,
    "solver_battery_capacity_kwh": 30.0,
    "solver_battery_soh_percent": 97.0,
    "solver_battery_min_soc_percent": 5.0,
    "solver_battery_max_soc_percent": 100.0,
    "solver_battery_reserve_percent": 15.0,
    "solver_battery_reserve_start_hour": 0.0,
    "solver_battery_reserve_end_hour": 6.0,
    "solver_battery_reserve_release_price": 0.6,
    "solver_charge_by_hour": 15.0,
    "solver_charge_by_percent": 90.0,
    "solver_charge_taper_start_percent": 90.0,
    "solver_charge_power_at_full_percent": 20.0,
    "solver_max_charge_kw": 10.0,
    "solver_max_discharge_kw": 10.0,
    "solver_efficiency_percent": 90.0,
    "solver_charge_cost": 0.005,
    "solver_discharge_cost": 0.02,
    "solver_salvage_value": 0.12,
    "solver_grid_max_import_kw": 20.0,
    "solver_grid_max_export_kw": 15.0,
    "solver_import_price_sensor": "sensor.fake_buy_flex",
    "solver_export_price_sensor": "sensor.fake_sell_flex",
    "solver_price_forecast_array_sensor": "sensor.fake_flex_forecast",
    "solver_regional_spot_forecast_sensor": "sensor.fake_spot_forecast",
    "solver_regional_spot_current_price_sensor": "sensor.fake_spot_now",
    "solver_p2p_matched_rate_forecast_sensor": "sensor.fake_sell_rate",
    "solver_p2p_settlement_history_sensor": "sensor.fake_p2p_settlement",
    "solver_p2p_block_1_rate_kw": 6.0,
    "solver_p2p_block_1_start_hour": 17.0,
    "solver_p2p_block_1_end_hour": 19.0,
    "solver_p2p_block_lead_time_minutes": 1.0,
    "solver_network_fee_default_rate": 0.02,
    "solver_network_fee_1_rate": 0.09,
    "solver_network_fee_1_start_hour": 16.0,
    "solver_network_fee_1_end_hour": 21.0,
    "solver_fixed_daily_charge": 1.10,
    "solver_export_floor_price": 0.01,
    "solver_solar_forecast_sensor": SOLAR_FC,
    "solver_solar_power_sensor": "sensor.fake_solar_power",
    "solver_load_forecast_sensor": LOAD,
    "solver_load_forecast_entities": [],
    "solver_whole_house_cross_check_sensor": "sensor.fake_house_power",
    "solver_weather_forecast_sensor": "sensor.fake_weather_hourly",
    "grid_sensor": "sensor.fake_grid_power",
    "solver_offer_curve_enabled": True,
    "solver_flex_signals_enabled": True,
    "solver_calibrated_objective_enabled": True,
    "solver_price_spike_override_armed": True,
    "solver_price_spike_threshold": 0.85,
    "solver_price_spike_discharge_kw": 10.0,
    "solver_risk_aversion": 0.25,
    "solver_proximal_weight_kw": 0.045,
    "solver_intraplan_smoothness_weight_kw": 0.01,
    "solver_period0_hysteresis": 0.01,
    "household_mode": "home",
}


def _t(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + TZ


def _local(instant_utc: str) -> datetime:
    """The scenario instant as naive local (UTC+10) time."""
    return datetime.fromisoformat(instant_utc).replace(tzinfo=None) + timedelta(
        hours=10
    )


def _buy(hour: float) -> float:
    """A synthetic flex-up buy price, $/kWh: a cheap midday, an evening peak."""
    return round(
        0.18
        + 0.12 * math.sin((hour - 12.0) / 24.0 * 2 * math.pi) ** 2
        + (0.25 if 17.0 <= hour % 24 < 19.0 else 0.0),
        4,
    )


def _sell(hour: float) -> float:
    return round(max(0.0, _buy(hour) - 0.11), 4)


def _solar_kw(hour: float) -> float:
    h = hour % 24
    return round(
        max(0.0, 6.0 * math.sin((h - 6.0) / 12.0 * math.pi)) if 6 <= h <= 18 else 0.0, 3
    )


def _load_kw(hour: float) -> float:
    h = hour % 24
    return round(
        0.9
        + 0.6 * math.exp(-((h - 7.5) ** 2) / 2)
        + 1.4 * math.exp(-((h - 19.0) ** 2) / 4),
        3,
    )


def _soc_pct(hour: float) -> float:
    return round(50 + 30 * math.sin((hour - 9) / 24 * 2 * math.pi), 1)


def _soc_rate_kw(hour: float) -> float:
    """kW into the battery that moves the SoC curve above: d(SoC%)/dt x capacity."""
    dpct_per_h = 30 * math.cos((hour - 9) / 24 * 2 * math.pi) * 2 * math.pi / 24
    return dpct_per_h / 100 * CONFIG["solver_battery_capacity_kwh"]


def states(now: datetime) -> dict:
    hour = now.hour + now.minute / 60
    five = [now + timedelta(minutes=5 * i) for i in range(288)]
    half = [
        now.replace(minute=(now.minute // 30) * 30) + timedelta(minutes=30 * i)
        for i in range(48 * 7)
    ]
    quarter = [
        now.replace(minute=(now.minute // 15) * 15) + timedelta(minutes=15 * i)
        for i in range(4 * 96)
    ]

    def hr(d: datetime) -> float:
        return d.hour + d.minute / 60

    return {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": copy.deepcopy(CONFIG),
        },
        "sensor.fake_battery_soc": {
            "state": str(_soc_pct(hour)),
            "attributes": {"unit_of_measurement": "%"},
        },
        "sensor.fake_battery_power": {
            "state": str(round(-_soc_rate_kw(hour), 3)),
            "attributes": {"unit_of_measurement": "kW"},
        },
        "sensor.fake_solar_power": {
            "state": str(round(_solar_kw(hour) * 1000)),
            "attributes": {"unit_of_measurement": "W"},
        },
        "sensor.fake_grid_power": {
            "state": "0.4",
            "attributes": {"unit_of_measurement": "kW"},
        },
        "sensor.fake_house_power": {
            "state": str(_load_kw(hour)),
            "attributes": {"unit_of_measurement": "kW"},
        },
        "sensor.fake_buy_flex": {
            "state": str(_buy(hour)),
            "attributes": {
                "unit_of_measurement": "$/kWh",
                "interval_minutes": 5,
                "interpolation_mode": "previous",
                "source_field": "flexUp",
                "direction": "Buy",
                "forecast": [{"time": _t(d), "value": _buy(hr(d))} for d in five],
            },
        },
        "sensor.fake_sell_flex": {
            "state": str(_sell(hour)),
            "attributes": {
                "unit_of_measurement": "$/kWh",
                "interval_minutes": 5,
                "interpolation_mode": "previous",
                "source_field": "flexUp",
                "direction": "Sell",
                "forecast": [{"time": _t(d), "value": _sell(hr(d))} for d in five],
            },
        },
        "sensor.fake_flex_forecast": {
            "state": str(_buy(hour)),
            "attributes": {
                "unit_of_measurement": "$/kWh",
                "interval_minutes": 5,
                "interpolation_mode": "previous",
                "source_field": "flexUp",
                "forecast_start": _t(five[0]),
                "forecast_end": _t(five[-1]),
                "forecast": [
                    {
                        "time": _t(d),
                        "costsflexup": _buy(hr(d)),
                        "earningsflexup": _sell(hr(d)),
                        "quality": "Fcst",
                    }
                    for d in five
                ],
            },
        },
        "sensor.fake_spot_now": {
            "state": str(round(_buy(hour) - 0.15, 4)),
            "attributes": {"unit_of_measurement": "$/kWh"},
        },
        "sensor.fake_spot_forecast": {
            "state": str(round(_buy(hour) - 0.15, 4)),
            "attributes": {
                "region": "QLD1",
                "interval_minutes": 30,
                "is_stale": False,
                "forecast": [
                    {
                        "nemtime": _t(d),
                        "time": _t(d),
                        "raw_value": round(_buy(hr(d)) - 0.15, 4),
                        "calibrated": round(_buy(hr(d)) - 0.15, 4),
                        "p10": round(_buy(hr(d)) - 0.20, 4),
                        "p50": round(_buy(hr(d)) - 0.15, 4),
                        "p90": round(_buy(hr(d)) - 0.05, 4),
                        "horizon_hours": round(i * 0.5, 2),
                    }
                    for i, d in enumerate(half)
                ],
            },
        },
        "sensor.fake_sell_rate": {
            "state": str(round(_sell(hour) * 100, 3)),
            "attributes": {
                "unit_of_measurement": "c/kWh",
                "volume": 0.02,
                "proportionP2P": 0.4,
                "matchedCost": 0.003,
                "spotCost": 0.002,
                "forecast": [
                    {"time": _t(d), "value": round(_sell(hr(d)) * 100, 3)} for d in five
                ],
            },
        },
        "sensor.fake_p2p_settlement": {
            "state": "3",
            "attributes": {
                "history": {
                    (now - timedelta(days=k)).strftime("%Y-%m-%d"): {
                        "export_cost": round(1.2 + 0.1 * k, 4),
                        "export_volume": round(9.0 + k, 3),
                        "spot_export_volume": round(4.0 + 0.5 * k, 3),
                        "settlement_state": "complete",
                        "intervals": 288,
                    }
                    for k in (1, 2, 3)
                },
            },
        },
        SOLAR_FC: {
            "state": str(_solar_kw(hour)),
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {"time": _t(d), "value": _solar_kw(hr(d))} for d in quarter
                ],
            },
        },
        LOAD: {
            "state": str(_load_kw(hour)),
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {"time": _t(d), "value": _load_kw(hr(d))} for d in quarter
                ],
            },
        },
        "sensor.fake_weather_hourly": {
            "state": _t(now),
            "attributes": {
                "forecast": [
                    {
                        "datetime": _t(now + timedelta(hours=i)),
                        "temperature": round(
                            22 + 6 * math.sin((i + hour - 9) / 24 * 2 * math.pi), 1
                        ),
                        "cloud_coverage": 20.0,
                        "condition": "sunny",
                    }
                    for i in range(96)
                ],
            },
        },
    }


def history(now: datetime) -> dict:
    """Two days of 15-minute readings for every measured sensor."""
    pts = [now - timedelta(minutes=15 * i) for i in range(2 * 96, 0, -1)]

    def series(fn, unit: str) -> list:
        return [
            HistoryPoint(
                last_changed=(d - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
                + "+00:00",
                state=str(fn(d)),
                attributes={"unit_of_measurement": unit} if i == 0 else None,
            )
            for i, d in enumerate(pts)
        ]

    def h(d: datetime) -> float:
        return d.hour + d.minute / 60

    return {
        "sensor.fake_solar_power": series(lambda d: round(_solar_kw(h(d)) * 1000), "W"),
        "sensor.fake_house_power": series(lambda d: _load_kw(h(d)), "kW"),
        # Discharge positive, and the derivative of the SoC curve below, so
        # the achieved SoC the quality report integrates stays in range.
        "sensor.fake_battery_power": series(
            lambda d: round(-_soc_rate_kw(h(d)), 3), "kW"
        ),
        "sensor.fake_grid_power": series(
            lambda d: round(0.3 * math.sin(h(d)), 3), "kW"
        ),
        "sensor.fake_battery_soc": series(lambda d: _soc_pct(h(d)), "%"),
        "sensor.fake_buy_flex": series(lambda d: _buy(h(d)), "$/kWh"),
        "sensor.fake_sell_flex": series(lambda d: _sell(h(d)), "$/kWh"),
    }


def _build(instant: str) -> FakeHA:
    now = _local(instant)
    return FakeHA(states=states(now), history=history(now))


register(
    Scenario(
        name="rich_site_afternoon",
        instant="2026-09-20T05:40:00+00:00",
        build=lambda: _build("2026-09-20T05:40:00+00:00"),
        purpose="every configured input, an hour before a P2P block, offer curve and flex signals on",
    )
)

register(
    Scenario(
        name="rich_site_after_midnight",
        instant="2026-09-20T14:05:00+00:00",
        build=lambda: _build("2026-09-20T14:05:00+00:00"),
        purpose="the first cycles after local midnight: inside the reserve window, scoring the day before",
        cycles=2,
    )
)


CIRCUITS = (
    "sensor.nimbus_fake_circuit_a_power_forecast",
    "sensor.nimbus_fake_circuit_b_power_forecast",
    "sensor.nimbus_fake_circuit_c_power_forecast",
)


def _build_variant(instant: str) -> FakeHA:
    """The rich site with the inputs the first two leave out: a load summed
    from circuits, a price event, envelope limits (one unreadable), a spot
    forecast shorter than the plan, P2P rate triples, a switchboard grid
    meter, a live max-discharge limit and an active price spike alert."""
    now = _local(instant)
    st = states(now)
    hist = history(now)
    cfg = st["sensor.nimbus_solver_config"]["attributes"]
    cfg.update(
        {
            "solver_load_forecast_entities": list(CIRCUITS),
            "solver_price_event_enabled": True,
            "solver_price_event_sensor": "sensor.fake_price_event",
            "solver_envelope_import_limit_entity": "sensor.fake_import_envelope",
            "solver_envelope_export_limit_entity": "sensor.fake_export_envelope",
            "switchboard_grid_meter_sensor": "sensor.fake_grid_meter",
            "solver_max_discharge_live_entity": "number.fake_max_discharge",
            "solver_price_spike_alert_entity": "binary_sensor.fake_spike_alert",
            # Within the live 8 kW limit: a spike discharge above it raises in
            # BatteryConfig and stops the cycle (reported on #1837, 23 Oct).
            "solver_price_spike_discharge_kw": 8.0,
        }
    )
    quarter = [
        now.replace(minute=(now.minute // 15) * 15) + timedelta(minutes=15 * i)
        for i in range(4 * 96)
    ]
    for k, ent in enumerate(CIRCUITS):
        share = (0.5, 0.3, 0.2)[k]
        st[ent] = {
            "state": str(round(_load_kw(now.hour + now.minute / 60) * share, 3)),
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {
                        "time": _t(d),
                        "value": round(_load_kw(d.hour + d.minute / 60) * share, 3),
                    }
                    for d in quarter
                ],
            },
        }
    event_start = now.replace(minute=0) + timedelta(hours=3)
    st["sensor.fake_price_event"] = {
        "state": "0",
        "attributes": {
            "forecast": [
                {"time": _t(event_start), "value": 15.0},
                {"time": _t(event_start + timedelta(minutes=30)), "value": 0.0},
            ]
        },
    }
    st["sensor.fake_import_envelope"] = {
        "state": "8.0",
        "attributes": {"unit_of_measurement": "kW"},
    }
    st["sensor.fake_export_envelope"] = {
        "state": "unavailable",
        "attributes": {"unit_of_measurement": "kW"},
    }
    st["sensor.fake_grid_meter"] = {
        "state": "-1500",
        "attributes": {"unit_of_measurement": "W"},
    }
    st["number.fake_max_discharge"] = {
        "state": "8",
        "attributes": {"min": 0, "max": 8.0, "unit_of_measurement": "kW"},
    }
    st["binary_sensor.fake_spike_alert"] = {"state": "on", "attributes": {}}
    spot = st["sensor.fake_spot_forecast"]["attributes"]
    spot["forecast"] = spot["forecast"][:24]  # 12 h, shorter than the plan
    five = [now + timedelta(minutes=5 * i) for i in range(288)]
    st["sensor.fake_sell_rate"]["attributes"]["forecast"] = [
        {
            "intervalEnd": _t(d + timedelta(minutes=5)),
            "intervalDuration": 5,
            "volume": 0.02,
            "proportionP2P": round(0.3 + 0.2 * math.sin(i / 12), 3),
            "matchedCost": round(0.002 + 0.001 * math.sin(i / 12), 5),
        }
        for i, d in enumerate(five)
    ]
    hist["sensor.fake_grid_meter"] = hist["sensor.fake_grid_power"]
    return FakeHA(states=st, history=hist)


register(
    Scenario(
        name="rich_site_circuits_and_limits",
        instant="2026-09-21T03:10:00+00:00",
        build=lambda: _build_variant("2026-09-21T03:10:00+00:00"),
        purpose="circuit load sum, price event, envelopes, short spot tail, P2P triples, grid meter, live max discharge, spike alert",
        cycles=2,
    )
)


# --- native mode: the same site inside Home Assistant, with an EV ----------

EV_SOC = "sensor.fake_ev_soc"
EV_POWER = "sensor.fake_ev_power"
EV_HOME = "binary_sensor.fake_ev_home"
EV_LIMIT = "number.fake_ev_charge_limit"
GARAGE_SOC = "sensor.fake_garage_soc"


def _participants() -> list:
    from golden.fake_native import FakeSubentry

    ev = {
        "battery_participant_name": "Car",
        "battery_participant_capacity_kwh": 60.0,
        "battery_participant_min_soc_percent": 20.0,
        "battery_participant_max_soc_percent": 100.0,
        "battery_participant_max_charge_kw": 7.0,
        "battery_participant_max_discharge_kw": 5.0,
        "battery_participant_soc_sensor": EV_SOC,
        "battery_participant_power_sensor": EV_POWER,
        "battery_participant_power_positive_is_charge": True,
        "battery_participant_efficiency_percent": 92.0,
        "battery_participant_degradation_cost_per_kwh": 0.04,
        "battery_participant_salvage_value": 0.10,
        "battery_participant_charge_limit_entity": EV_LIMIT,
        "battery_participant_available_entity": EV_HOME,
        "battery_participant_departure_hour": 7,
        "battery_participant_must_have_soc_by_departure_percent": 80.0,
        "battery_participant_shared_charger_group": "garage",
        "battery_participant_shared_charger_max_kw": 7.0,
    }
    garage = {
        "battery_participant_name": "Garage battery",
        "battery_participant_capacity_kwh": 10.0,
        "battery_participant_max_charge_kw": 5.0,
        "battery_participant_max_discharge_kw": 5.0,
        "battery_participant_soc_sensor": GARAGE_SOC,
        "battery_participant_shared_charger_group": "garage",
        "battery_participant_shared_charger_max_kw": 7.0,
    }
    return [
        FakeSubentry("golden_ev", "battery_participant", ev, "Car"),
        FakeSubentry("golden_garage", "battery_participant", garage, "Garage battery"),
    ]


def _build_native(instant: str) -> FakeHA:
    """The rich site's states and history, plus an EV that is home and a
    second battery sharing its charger."""
    fake = _build(instant)
    now = _local(instant)
    # A resolved location, so the flex telemetry record is built (#1634).
    fake.states["sensor.nimbus_solver_config"]["attributes"].update(
        {"region": "QLD1", "postcode_prefix": "410"}
    )
    fake.states.update(
        {
            EV_SOC: {"state": "46.5", "attributes": {"unit_of_measurement": "%"}},
            EV_POWER: {"state": "0.0", "attributes": {"unit_of_measurement": "kW"}},
            EV_HOME: {"state": "on", "attributes": {}},
            EV_LIMIT: {"state": "90", "attributes": {"min": 50, "max": 100}},
            GARAGE_SOC: {"state": "70", "attributes": {"unit_of_measurement": "%"}},
        }
    )
    stamps = [now - timedelta(minutes=15 * i) for i in range(2 * 96, 0, -1)]
    fake.history[EV_POWER] = [
        HistoryPoint(
            last_changed=(d - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
            + "+00:00",
            state=str(7.0 if 1 <= d.hour < 5 else 0.0),
            attributes={"unit_of_measurement": "kW"},
        )
        for d in stamps
    ]
    return fake


def _native(fake: FakeHA):
    from golden.fake_native import FakeNativeHass

    return FakeNativeHass(fake=fake, subentries=_participants())


register(
    Scenario(
        name="rich_site_native_with_ev",
        instant="2026-09-20T09:20:00+00:00",
        build=lambda: _build_native("2026-09-20T09:20:00+00:00"),
        native=_native,
        purpose="the rich site in native mode: recorder reads, an EV due out at 07:00 and a battery sharing its charger",
        cycles=2,
    )
)
