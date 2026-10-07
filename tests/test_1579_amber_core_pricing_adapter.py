"""nimbus #1579: Home Assistant core Amber Electric forecasts through the real
pricing path.

HA core's Amber forecast sensors publish the PLURAL `forecasts` attribute
(`homeassistant/components/amberelectric/sensor.py`, `AmberForecastSensor`).
Nimbus only read `forecast`, so an Amber core forecast sensor set as the import
or export price read as no forecast at all and its current price was held flat
for the whole horizon.

Rows are Mark Purcell's live #1550 capture (6 Oct 2026), extracted from the
published comment. They came from the `amberelectric.get_forecasts` action;
#1550 records the sensor attribute carried the same rows without the
`advanced_price_*` fields, so the sensor shape is the capture minus those.
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

_SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "pricing" / "1550_amber_core.json").read_text(
        encoding="utf-8"
    )
)["samples"]


def _sensor_rows(alias):
    return [
        {k: v for k, v in r.items() if not k.startswith("advanced_price")}
        for r in _SAMPLES[alias]["selected_native_rows"]
    ]


GENERAL = _sensor_rows("amber_import_action")
FEED_IN = _sensor_rows("amber_export_action")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _read(rows, grid, key="forecasts"):
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {key: rows}}
    ):
        return solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.home_general_forecast", grid
        )


def test_the_plural_attribute_is_read():
    values, real = _read(GENERAL, [_t("2026-10-06T13:22:00+10:00")])
    assert values == [0.0620461] and real == [True]


def test_per_kwh_is_the_price_never_spot_or_advanced():
    values, _ = _read(GENERAL, [_t("2026-10-06T14:10:00+10:00")])
    assert values == [0.039132099999999996]  # spot_per_kwh is 0.0059...
    # the action rows' advanced prediction is not read even when present
    raw = _SAMPLES["amber_import_action"]["selected_native_rows"]
    values, _ = _read(raw, [_t("2026-10-06T14:10:00+10:00")])
    assert values == [0.039132099999999996]


def test_feed_in_sign_is_kept_as_published():
    """HA already negates feed-in so positive is earnings; the 7 Oct 12:30
    row is a NEGATIVE feed-in (paying to export) and stays negative."""
    values, _ = _read(
        FEED_IN,
        [_t("2026-10-06T13:22:00+10:00"), _t("2026-10-07T12:45:00+10:00")],
    )
    assert values == [0.0263986, -0.0038316]


def test_boundaries_are_canonical_and_widths_mixed():
    iv = {i.start: i.end for i in price_intervals.amber_core_intervals(GENERAL)}
    assert iv[_t("2026-10-06T13:20:00+10:00")] == _t("2026-10-06T13:25:00+10:00")
    assert iv[_t("2026-10-06T14:00:00+10:00")] == _t("2026-10-06T14:30:00+10:00")


def test_coverage_runs_to_the_final_end_and_not_across_holes():
    _, real = _read(
        GENERAL,
        [
            _t("2026-10-07T12:59:00+10:00"),
            _t("2026-10-07T13:00:00+10:00"),  # final END
            _t("2026-10-06T18:00:00+10:00"),  # between selected rows
        ],
    )
    assert real == [True, False, False]


def test_unusable_rows_are_dropped_and_zero_kept():
    rows = [dict(r) for r in GENERAL]
    rows[0]["per_kwh"] = 0.0
    rows[1]["per_kwh"] = None
    rows[2]["per_kwh"] = float("nan")
    iv = {i.start: i.value for i in price_intervals.amber_core_intervals(rows)}
    assert iv[_t("2026-10-06T13:20:00+10:00")] == 0.0
    assert _t("2026-10-06T13:25:00+10:00") not in iv
    assert _t("2026-10-06T13:55:00+10:00") not in iv


def test_a_sensor_without_forecasts_still_has_none():
    assert _read([], [_t("2026-10-06T13:22:00+10:00")]) is None
    assert _read([{"time": "x"}], [_t("2026-10-06T13:22:00+10:00")]) is None


# --- proposal -------------------------------------------------------------------

SITE = "01K2F0S9Y9AJJTF2115TMNR2BM"


def _reg(uid, entity_id):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=None, domain="sensor"
    )


FULL = [
    _reg(f"{SITE}-forecasts-general", "sensor.home_general_forecast"),
    _reg(f"{SITE}-forecasts-feed_in", "sensor.home_feed_in_forecast"),
    _reg(f"{SITE}-forecasts-controlled_load", "sensor.home_cl_forecast"),
    _reg(f"{SITE}-current-general", "sensor.home_general_price"),
]


def _hass(by_domain):
    hass = MagicMock()
    entries = {
        d: [SimpleNamespace(state=SimpleNamespace(value="loaded"), entry_id=d)]
        for d in by_domain
    }
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: entries.get(d, [])
    )
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(side_effect=lambda eid: object())
    hass.services.async_call = AsyncMock()
    hass.by_domain = by_domain
    return hass


def _run(hass, fn):
    with (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(
            pa.er,
            "async_entries_for_config_entry",
            side_effect=lambda _r, entry_id: hass.by_domain[entry_id],
        ),
        patch.object(pa, "detect_localvolts_v2_profile", return_value={}),
    ):
        return fn()


def test_forecast_sensors_are_proposed_not_current_or_controlled_load():
    hass = _hass({"amberelectric": FULL})
    assert _run(hass, lambda: pa.detect_amber_core_profile(hass)) == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.home_general_forecast",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.home_feed_in_forecast",
    }


def test_two_sites_propose_nothing():
    hass = _hass(
        {"amberelectric": [*FULL, _reg("OTHER-forecasts-general", "sensor.o")]}
    )
    assert _run(hass, lambda: pa.detect_amber_core_profile(hass)) == {}


def test_amber_express_comes_first():
    ax = [
        _reg(f"{SITE}_general_price", "sensor.amber_express_general_price"),
        _reg(f"{SITE}_feed_in_price", "sensor.amber_express_feed_in_price"),
    ]
    hass = _hass({"amberelectric": FULL, "amber_express": ax})
    out = _run(hass, lambda: pa.with_detected_profile(hass, {}))
    assert out[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.amber_express_general_price"
    assert out[CONF_SOLVER_EXPORT_PRICE_SENSOR] == "sensor.amber_express_feed_in_price"


def _notified(hass, options):
    _run(hass, lambda: asyncio.run(pa.async_notify_pricing_setup(hass, options)))
    return [
        c.args[2]["notification_id"]
        for c in hass.services.async_call.call_args_list
        if c.args[1] == "create"
    ]


def test_notice_only_when_no_richer_source_and_a_field_is_empty():
    nid = pa.NOTIFY_AMBER_CORE_DETECTED_ID
    assert nid in _notified(_hass({"amberelectric": FULL}), {})
    ax = [_reg(f"{SITE}_general_price", "sensor.ax")]
    assert nid not in _notified(_hass({"amberelectric": FULL, "amber_express": ax}), {})
    full = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.a",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.b",
    }
    assert nid not in _notified(_hass({"amberelectric": FULL}), full)
