"""nimbus #1578: AEMO NEM Data's regional forecast, read by the real pricing
path and proposed where empty.

AEMO NEM Data (`aemo_nem`, cabberley/HA_AemoNemData) publishes its regional
30-minute forecast as `forecast: [{start_time, end_time, price}]` in $/kWh.
Nimbus's regional spot forecast reader only knew PD7DAY's
`{time, calibrated}`, so this sensor in that field parsed to nothing and the
far-horizon extrapolation silently did not run.

The rows below are Mark Purcell's live capture from #1550 (6 Oct 2026),
loaded from `tests/fixtures/pricing/` and run through `solver_writer`, not a
copy of it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
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
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
AEMO = json.loads((FIXTURES / "1550_aemo_nem_qld.json").read_text())
PD7 = json.loads((FIXTURES / "1550_pd7day_qld_wholesale.json").read_text())


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _fetch(rows):
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
    ):
        return solver_writer.fetch_aemo_forecast("sensor.any_regional_forecast")


# --- the real pricing path ------------------------------------------------


def test_real_aemo_rows_reach_the_regional_spot_forecast():
    assert _fetch(AEMO["selected_native_rows"]) == [
        (_t("2026-10-06T13:00:00+10:00"), 0.0297),
        (_t("2026-10-06T13:30:00+10:00"), 0.0267),
        (_t("2026-10-08T03:30:00+10:00"), 0.1176),
    ]


def test_price_is_not_divided_by_a_thousand_again():
    # $/kWh as published: 0.0297 is 29.70 $/MWh, a normal QLD midday price.
    assert _fetch(AEMO["selected_native_rows"])[0][1] == 0.0297


def test_pd7day_rows_read_exactly_as_before():
    assert _fetch(PD7["selected_native_rows"]) == [
        (_t("2026-10-06T12:30:00+10:00"), 0.043006),
        (_t("2026-10-14T03:30:00+10:00"), 0.081885),
    ]


def test_extrapolation_past_the_retail_feed_uses_aemo():
    """The one consumer: periods past the retail array's last point take
    AEMO's wholesale price plus the learned 5-minute-of-day offset."""
    aemo_pts = _fetch(AEMO["selected_native_rows"])
    retail = [{"time": "2026-10-06T13:00:00+10:00", "costsflexup": 0.20}]
    grid = [_t("2026-10-06T13:00:00+10:00"), _t("2026-10-06T13:40:00+10:00")]
    bucket = 13 * 12 + 40 // 5
    vals, real = solver_writer.resample_price_with_extrapolation(
        retail, "costsflexup", grid, aemo_pts, {bucket: 0.05}
    )
    assert vals[0] == 0.20 and real[0] is True
    assert abs(vals[1] - (0.0267 + 0.05)) < 1e-12 and real[1] is False


def test_without_aemo_the_last_retail_value_is_held():
    retail = [{"time": "2026-10-06T13:00:00+10:00", "costsflexup": 0.20}]
    grid = [_t("2026-10-06T13:00:00+10:00"), _t("2026-10-06T13:40:00+10:00")]
    vals, _ = solver_writer.resample_price_with_extrapolation(
        retail, "costsflexup", grid, _fetch([]), {}
    )
    assert vals == [0.20, 0.20]


# --- interval semantics ---------------------------------------------------


def _row(start, mins, price):
    s = _t(start)
    return {
        "start_time": s.isoformat(),
        "end_time": (s + timedelta(minutes=mins)).isoformat(),
        "price": price,
    }


def test_negative_and_zero_prices_are_kept():
    iv = price_intervals.aemo_nem_intervals(
        [
            _row("2026-10-06T11:00:00+10:00", 30, -0.0412),
            _row("2026-10-06T11:30:00+10:00", 30, 0.0),
        ]
    )
    assert [i.value for i in iv] == [-0.0412, 0.0]


def test_unusable_rows_are_dropped_never_read_as_zero():
    good = _row("2026-10-06T13:00:00+10:00", 30, 0.0297)
    bad = [
        _row("2026-10-06T13:30:00+10:00", 30, None),
        _row("2026-10-06T14:00:00+10:00", 30, "n/a"),
        _row("2026-10-06T14:30:00+10:00", 30, float("nan")),
        _row("2026-10-06T15:00:00+10:00", 30, float("inf")),
        {"start_time": "garbage", "end_time": "2026-10-06T16:00:00+10:00", "price": 1},
        _row("2026-10-06T16:00:00+10:00", 0, 0.03),  # end not after start
        "not a row",
    ]
    iv = price_intervals.aemo_nem_intervals([good, *bad])
    assert [(i.start, i.value) for i in iv] == [(_t(good["start_time"]), 0.0297)]


def test_duplicates_and_overlaps_keep_the_first_row():
    iv = price_intervals.aemo_nem_intervals(
        [
            _row("2026-10-06T13:30:00+10:00", 30, 0.02),
            _row("2026-10-06T13:00:00+10:00", 30, 0.01),
            _row("2026-10-06T13:00:00+10:00", 30, 0.99),
            _row("2026-10-06T13:15:00+10:00", 30, 0.98),
        ]
    )
    assert [i.value for i in iv] == [0.01, 0.02]


def test_coverage_uses_the_final_end_and_shows_the_hole():
    cov = price_intervals.coverage(
        price_intervals.aemo_nem_intervals(AEMO["selected_native_rows"])
    )
    assert cov.first_start == _t(AEMO["full_array"]["first_start"])
    assert cov.final_end == _t(AEMO["full_array"]["final_end"])  # not the last START
    assert cov.covered_hours == 1.5
    assert cov.gaps == (
        (_t("2026-10-06T14:00:00+10:00"), _t("2026-10-08T03:30:00+10:00")),
    )


def test_shape_check():
    assert price_intervals.is_aemo_nem_rows(AEMO["selected_native_rows"])
    assert not price_intervals.is_aemo_nem_rows(PD7["selected_native_rows"])
    assert not price_intervals.is_aemo_nem_rows([])
    assert not price_intervals.is_aemo_nem_rows(None)


# --- detection and proposal -----------------------------------------------

QLD_UID = "sensor.aemo_nem_qld1_current_30min_forecast"
NSW_UID = "sensor.aemo_nem_nsw1_current_30min_forecast"
ARRAY = {CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.array"}


def _reg(uid, entity_id=None, disabled=None, domain="sensor"):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id or uid, disabled_by=disabled, domain=domain
    )


def _hass(regs, state="loaded"):
    hass = MagicMock()
    entry = SimpleNamespace(state=SimpleNamespace(value=state), entry_id="E1")
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: [entry] if d == "aemo_nem" else []
    )
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(side_effect=lambda eid: object())
    hass.services.async_call = AsyncMock()
    hass.regs = regs
    return hass


def _patched(hass):
    return (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(pa.er, "async_entries_for_config_entry", return_value=hass.regs),
        patch.object(pa, "detect_localvolts_v2_profile", return_value={}),
    )


def _run(hass, fn):
    a, b, c = _patched(hass)
    with a, b, c:
        return fn()


def _detect(hass):
    return _run(hass, lambda: pa.detect_aemo_nem_forecast(hass))


def test_one_region_is_proposed():
    assert _detect(_hass([_reg(QLD_UID)])) == {
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: QLD_UID
    }


def test_found_by_unique_id_after_a_rename():
    hass = _hass([_reg(QLD_UID, entity_id="sensor.my_wholesale_forecast")])
    assert _detect(hass) == {
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.my_wholesale_forecast"
    }


def test_other_sensors_disabled_and_unloaded_are_ignored():
    regs = [
        _reg("sensor.aemo_nem_qld1_current_5min_period_price"),
        _reg(QLD_UID, disabled="user"),
        _reg(
            "binary_sensor.aemo_nem_qld1_current_30min_forecast",
            domain="binary_sensor",
        ),
    ]
    assert _detect(_hass(regs)) == {}
    assert _detect(_hass([_reg(QLD_UID)], state="setup_error")) == {}


def test_several_regions_narrow_to_the_home_region():
    hass = _hass([_reg(QLD_UID), _reg(NSW_UID)])
    with patch.object(
        pa.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        return_value=("QLD1", "400"),
    ):
        assert _detect(hass) == {CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: QLD_UID}


def test_several_regions_and_no_home_region_propose_nothing():
    hass = _hass([_reg(QLD_UID), _reg(NSW_UID)])
    with patch.object(
        pa.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        return_value=(None, None),
    ):
        assert _detect(hass) == {}


def _with_profile(hass, defaults):
    return _run(hass, lambda: pa.with_detected_profile(hass, defaults))


def test_prefills_only_an_empty_field_on_the_array_path():
    hass = _hass([_reg(QLD_UID)])
    filled = _with_profile(hass, dict(ARRAY))
    assert filled[CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR] == QLD_UID
    kept = {
        **ARRAY,
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: (
            "sensor.nem_pd7day_qld1_nem_spot_price_forecast"
        ),
    }
    assert _with_profile(hass, dict(kept)) == kept


def test_not_proposed_where_nothing_reads_it():
    hass = _hass([_reg(QLD_UID)])
    assert CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR not in _with_profile(hass, {})


def _notified(hass, options):
    _run(hass, lambda: asyncio.run(pa.async_notify_pricing_setup(hass, options)))
    return [
        c.args[2]["notification_id"]
        for c in hass.services.async_call.call_args_list
        if c.args[1] == "create"
    ]


def test_startup_notice_only_when_empty_and_used():
    aemo = pa.NOTIFY_AEMO_DETECTED_ID
    assert aemo in _notified(_hass([_reg(QLD_UID)]), dict(ARRAY))
    set_already = {**ARRAY, CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.x"}
    assert aemo not in _notified(_hass([_reg(QLD_UID)]), set_already)
    assert aemo not in _notified(_hass([_reg(QLD_UID)]), {})


# --- AEMO as an import or export price (Mark Purcell, #1578) ---------------


def _generic(rows, grid):
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
    ):
        return solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.aemo_nem_qld1_current_30min_forecast", grid
        )


def test_aemo_reads_as_a_feed_in_or_buy_price():
    """The generic import/export path reads AEMO's rows as published; the
    configured network/flat fees are added to the import price downstream,
    so this is all a spot-plus-tariff buy price needs."""
    grid = [
        _t("2026-10-06T13:00:00+10:00"),
        _t("2026-10-06T13:45:00+10:00"),
        _t("2026-10-08T03:45:00+10:00"),
    ]
    values, real = _generic(AEMO["selected_native_rows"], grid)
    assert values == [0.0297, 0.0267, 0.1176]
    assert real == [True, True, True]


def test_a_hole_between_aemo_rows_is_not_real_coverage():
    grid = [_t("2026-10-07T12:00:00+10:00"), _t("2026-10-08T04:00:00+10:00")]
    values, real = _generic(AEMO["selected_native_rows"], grid)
    assert real == [False, False]  # inside the hole; at the final END
    assert values == [0.0267, 0.1176]  # held, as every generic source is


def test_a_negative_aemo_feed_in_price_stays_negative():
    rows = [_row("2026-10-06T11:00:00+10:00", 30, -0.0412)]
    values, real = _generic(rows, [_t("2026-10-06T11:10:00+10:00")])
    assert values == [-0.0412] and real == [True]


def test_aemo_rows_with_no_usable_price_give_no_forecast():
    assert (
        _generic(
            [_row("2026-10-06T11:00:00+10:00", 30, None)],
            [_t("2026-10-06T11:10:00+10:00")],
        )
        is None
    )
