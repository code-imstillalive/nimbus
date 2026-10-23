"""nimbus #1583: OpenADR 3 VEN prices through the real pricing path.

An OpenADR 3 VEN price sensor (grid-coordination/openadr3-ven-hass) publishes
only a summary. Its full forecast exists only in the entity action
`openadr3_ven.get_forecast`, as `[{datetime, value, interval_minutes}]`. Before
this, such a sensor as the import or export price read as no forecast at all,
and its current value was held flat across the whole horizon.

Rows are Mark Purcell's live #1550 capture (6 Oct 2026), extracted from the
published comment into `tests/fixtures/pricing/1550_openadr.json`, served
through a stubbed action response and read by `solver_writer` itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import price_intervals
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load.const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
)

_S = json.loads(
    (Path(__file__).parent / "fixtures" / "pricing" / "1550_openadr.json").read_text(
        encoding="utf-8"
    )
)["samples"]
IMPORT = _S["openadr_import"]["selected_native_rows"]
EXPORT = _S["openadr_export"]["selected_native_rows"]
HOURLY = _S["openadr_second_program"]["selected_native_rows"]

EID = "sensor.qld_program_price"
SUMMARY = {
    "payload_type": "PRICE",
    "forecast_rows": 338,
    "forecast_start": "2026-10-06T13:10:00+10:00",
    "forecast_end": "2026-10-13T13:00:00+10:00",  # the last START, not the end
    "unit_of_measurement": "$/kWh",
}


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _read(rows, grid, payload="PRICE", attrs=None, response=None):
    attrs = dict(SUMMARY if attrs is None else attrs)
    if response is None:
        response = {EID: {"payload_type": payload, "unit": "$/kWh", "forecast": rows}}
    calls = []

    def _call(domain, service, data):
        calls.append((domain, service, data))
        return response

    with (
        patch.object(solver_writer, "ha_get", return_value={"attributes": attrs}),
        patch.object(solver_writer, "ha_call_service_with_response", side_effect=_call),
    ):
        out = solver_writer.resample_generic_price_forecast_with_coverage(EID, grid)
    return out, calls


# --- reading -------------------------------------------------------------------


def test_the_forecast_comes_from_the_action_for_this_entity():
    (values, real), calls = _read(IMPORT, [_t("2026-10-06T13:12:00+10:00")])
    assert values == [0.062571] and real == [True]
    assert calls == [("openadr3_ven", "get_forecast", {"entity_id": EID})]


def test_mixed_5_15_30_minute_rows_keep_their_own_ends():
    grid = [
        _t("2026-10-06T13:14:59+10:00"),  # inside the 5-minute row
        _t("2026-10-06T13:20:00+10:00"),  # inside the 15-minute row
        _t("2026-10-06T13:45:00+10:00"),  # inside the first 30-minute row
    ]
    (values, real), _ = _read(IMPORT, grid)
    assert values == [0.062571, 0.081069, 0.053818]
    assert real == [True, True, True]


def test_coverage_ends_at_the_last_row_plus_its_length_not_forecast_end():
    grid = [_t("2026-10-13T13:20:00+10:00"), _t("2026-10-13T13:30:00+10:00")]
    (_, real), _ = _read(IMPORT, grid)
    assert real == [True, False]  # 13:00 + 30 min; forecast_end said 13:00


def test_the_hourly_programs_24_hour_hole_is_not_coverage():
    attrs = {**SUMMARY, "forecast_rows": 144}
    grid = [_t("2026-10-10T16:30:00+10:00"), _t("2026-10-11T03:00:00+10:00")]
    (_, real), _ = _read(HOURLY, grid, attrs=attrs)
    assert real == [True, False]


def test_export_price_reads_too():
    attrs = {**SUMMARY, "payload_type": "EXPORT_PRICE"}
    (values, _), _ = _read(
        EXPORT, [_t("2026-10-06T13:12:00+10:00")], payload="EXPORT_PRICE", attrs=attrs
    )
    assert values == [0.027605]


def test_a_missing_length_is_the_integrations_own_60_minutes():
    rows = [{"datetime": "2026-10-06T13:00:00+10:00", "value": 0.05}]
    iv = price_intervals.openadr_intervals(rows)
    assert iv[0].end == _t("2026-10-06T14:00:00+10:00")


def test_negative_and_zero_kept_unusable_dropped():
    rows = [
        {
            "datetime": "2026-10-06T13:00:00+10:00",
            "value": -0.02,
            "interval_minutes": 30,
        },
        {"datetime": "2026-10-06T13:30:00+10:00", "value": 0.0, "interval_minutes": 30},
        {
            "datetime": "2026-10-06T14:00:00+10:00",
            "value": None,
            "interval_minutes": 30,
        },
        {"datetime": "garbage", "value": 0.1, "interval_minutes": 30},
        {"datetime": "2026-10-06T15:00:00+10:00", "value": float("nan")},
    ]
    assert [i.value for i in price_intervals.openadr_intervals(rows)] == [-0.02, 0.0]


def test_carbon_is_never_read_as_a_price():
    ghg = {**SUMMARY, "payload_type": "GHG"}
    assert not price_intervals.is_provider_shape(ghg)
    response = {EID: {"payload_type": "GHG", "unit": "g CO2/kWh", "forecast": IMPORT}}
    assert price_intervals.openadr_rows_from_response(response, EID) is None


def test_a_failed_action_or_another_entitys_response_gives_no_forecast():
    out, _ = _read(IMPORT, [_t("2026-10-06T13:12:00+10:00")], response=None or {})
    assert out is None
    other = {"sensor.other": {"payload_type": "PRICE", "forecast": IMPORT}}
    out, _ = _read(IMPORT, [_t("2026-10-06T13:12:00+10:00")], response=other)
    assert out is None


def test_other_sensors_never_call_the_action():
    rows = [{"time": "2026-10-06T13:15:00+10:00", "value": 0.0848}]
    calls = []
    with (
        patch.object(
            solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
        ),
        patch.object(
            solver_writer,
            "ha_call_service_with_response",
            side_effect=lambda *a: calls.append(a),
        ),
    ):
        solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.x", [_t("2026-10-06T13:15:00+10:00")]
        )
    assert calls == []


# --- detection: reported, never pre-filled ---------------------------------------


def _reg(uid, entity_id):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=None, domain="sensor"
    )


REGS = [
    _reg("E1_qld_price", "sensor.qld_program_price"),
    _reg("E1_qld_export_price", "sensor.qld_program_export_price"),
    _reg("E1_qld_ghg", "sensor.qld_program_ghg"),
]


def _hass():
    hass = MagicMock()
    entry = SimpleNamespace(state=SimpleNamespace(value="loaded"), entry_id="E1")
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: [entry] if d == "openadr3_ven" else []
    )
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(side_effect=lambda eid: object())
    hass.services.async_call = AsyncMock()
    return hass


def _run(hass, fn):
    with (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(pa.er, "async_entries_for_config_entry", return_value=REGS),
        patch.object(pa, "detect_localvolts_v2_profile", return_value={}),
    ):
        return fn()


def test_price_sensors_are_found_by_unique_id_and_ghg_is_not():
    hass = _hass()
    assert _run(hass, lambda: pa.detect_openadr_price_sensors(hass)) == {
        "import": ["sensor.qld_program_price"],
        "export": ["sensor.qld_program_export_price"],
    }


def test_never_pre_filled():
    hass = _hass()
    out = _run(hass, lambda: pa.with_detected_profile(hass, {}))
    assert CONF_SOLVER_IMPORT_PRICE_SENSOR not in out
    assert CONF_SOLVER_EXPORT_PRICE_SENSOR not in out


def test_reported_only_while_a_field_is_empty():
    def notified(options):
        hass = _hass()
        _run(hass, lambda: asyncio.run(pa.async_notify_pricing_setup(hass, options)))
        return [
            c.args[2]["notification_id"]
            for c in hass.services.async_call.call_args_list
            if c.args[1] == "create"
        ]

    assert pa.NOTIFY_OPENADR_DETECTED_ID in notified({})
    full = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.a",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.b",
    }
    assert pa.NOTIFY_OPENADR_DETECTED_ID not in notified(full)
